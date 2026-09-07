# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W3 (v0.2.92 wiring audit) — every KG-row writer reaches the ONE tag home.

The defect
----------
The per-slot truncation record was designed, implemented, unit-tested and
documented — and reached Weaviate from **one of four** writers (the MCP
``store_knowledge_node`` MULTI-chunk branch). The decisive evidence was one
query: ``secondary_truncated_slots`` was not in the live collections'
schema **at all** (VCODev_KnowledgeGraph 765 rows / VCODev_Development 338
rows, 2026-09-05) — no writer had ever written it on this machine, because
the writers that produce essentially every row are:

  * ``sync_knowledge_graph.py`` — the install seed, every post-file-edit
    hook sync, and the ``--rechunk`` remedy;
  * the MCP ``store_knowledge_node`` SINGLE-chunk branch (most nodes);
  * ``vco_lib.embedding_enrichment`` — the launcher's enrich-slot backfill;
  * ``rl_client.embed_regen.ensure_slot_embedding`` — the dual-log backfill.

The fix is NOT four hand-copied tag assignments (four copies of one rule,
five when the next writer lands). It is ONE writer-side home,
``vco_lib.kg_truncation_tags``, with two operations:

  * ``truncation_tag_properties`` — the FULL-WRITE stamper (kg-sync, both
    MCP branches): derives all THREE stored properties from ONE atomic
    capture — the complete record, the frozen secondary-only view, and
    ``truncation_measured_slots`` (the slots the row can answer for);
  * ``merge_slot_truncation`` — the SINGLE-SLOT PATCH merger (enrichment
    flush, dual-log backfill): records a verdict only when the caller can
    PROVE one, and otherwise patches nothing so the slot stays unmeasured
    and reads UNKNOWN — a patch adds a vector the full write never looked
    at, and absence from the truncated list is not evidence of fidelity.

What these tests do
-------------------
Drive each writer through its PRODUCTION entry point and assert the tag
lands. Removing any one writer's routing turns exactly its test red — the
property a helper unit test cannot give.

Invariants pinned throughout:
  * the legacy ``secondary_truncated_slots`` keeps its EXACT prior meaning
    (the complete list minus the active slot) — never widened;
  * a row that cannot be answered for honestly gets NO record, so the
    tri-state reader resolves UNKNOWN — never a guessed ``False``;
  * additive only — nothing here re-embeds, migrates or deletes.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.kg_truncation_tags import (  # noqa: E402
    MEASURED_SLOTS_PROP,
    SECONDARY_TRUNCATED_SLOTS_PROP,
    TRUNCATED_SLOTS_PROP,
    merge_slot_truncation,
    truncation_tag_properties,
)

SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
QWEN3_MODEL = "qwen3-embedding:0.6b"
PROJECT_KG = "W3Tag_KnowledgeGraph"
DEV_COLL = "W3Tag_Development"
ACTIVE_SLOT = "qwen3_embed"


# ═════════════════════════════════════════════════════════════════════════
# Part 0 — the ONE home's own semantics
# ═════════════════════════════════════════════════════════════════════════


def test_stamper_derives_all_three_properties_from_one_capture():
    props = truncation_tag_properties(
        ["arctic2_embed", ACTIVE_SLOT], ACTIVE_SLOT,
        measured_slots=[ACTIVE_SLOT, "arctic2_embed", "openai_text_embed"],
    )
    assert props[TRUNCATED_SLOTS_PROP] == ["arctic2_embed", ACTIVE_SLOT]
    assert props[SECONDARY_TRUNCATED_SLOTS_PROP] == ["arctic2_embed"], (
        "the ACTIVE slot must never enter the legacy property — widening it "
        "would silently reinterpret every row written before v0.2.92"
    )
    assert props[MEASURED_SLOTS_PROP] == [
        "arctic2_embed", "openai_text_embed", ACTIVE_SLOT,
    ], "a full write can answer for every slot it stored, truncated or not"


def test_stamper_none_capture_writes_nothing():
    """A writer that cannot produce an honest capture stamps NO property, so
    the row resolves UNKNOWN rather than carrying a `[]` it cannot prove."""
    assert truncation_tag_properties(None, ACTIVE_SLOT) == {}


def test_stamper_empty_capture_is_a_real_full_fidelity_answer():
    props = truncation_tag_properties(
        [], ACTIVE_SLOT, measured_slots=[ACTIVE_SLOT],
    )
    assert props == {
        TRUNCATED_SLOTS_PROP: [],
        SECONDARY_TRUNCATED_SLOTS_PROP: [],
        MEASURED_SLOTS_PROP: [ACTIVE_SLOT],
    }


def test_merge_on_a_pre_record_row_returns_none_never_a_guessed_false():
    assert merge_slot_truncation({}, "arctic2_embed", False, ACTIVE_SLOT) is None
    assert merge_slot_truncation(None, "arctic2_embed", True, ACTIVE_SLOT) is None
    assert merge_slot_truncation(
        {"title": "x"}, "arctic2_embed", True, ACTIVE_SLOT,
    ) is None


def test_merge_adds_the_fresh_slot_and_keeps_every_other_verdict():
    merged = merge_slot_truncation(
        {TRUNCATED_SLOTS_PROP: [ACTIVE_SLOT],
         MEASURED_SLOTS_PROP: [ACTIVE_SLOT]},
        "arctic2_embed", True, ACTIVE_SLOT,
    )
    assert merged[TRUNCATED_SLOTS_PROP] == ["arctic2_embed", ACTIVE_SLOT]
    assert merged[SECONDARY_TRUNCATED_SLOTS_PROP] == ["arctic2_embed"]
    assert merged[MEASURED_SLOTS_PROP] == ["arctic2_embed", ACTIVE_SLOT]


def test_merge_records_a_proven_false_as_measured_but_not_truncated():
    merged = merge_slot_truncation(
        {TRUNCATED_SLOTS_PROP: [ACTIVE_SLOT],
         MEASURED_SLOTS_PROP: [ACTIVE_SLOT]},
        "arctic2_embed", False, ACTIVE_SLOT,
    )
    assert merged[TRUNCATED_SLOTS_PROP] == [ACTIVE_SLOT]
    assert merged[MEASURED_SLOTS_PROP] == ["arctic2_embed", ACTIVE_SLOT], (
        "a PROVEN full-fidelity patch must make the row able to answer for "
        "that slot — otherwise the answer is thrown away"
    )


def test_merge_refuses_an_unprovable_verdict_so_the_slot_stays_unmeasured():
    """The hole this closes: a patch adds a VECTOR for a slot the original
    full write never measured. Without an explicit measured set, the reader's
    ``slot in truncated_slots`` would answer a confident False for it."""
    assert merge_slot_truncation(
        {TRUNCATED_SLOTS_PROP: [ACTIVE_SLOT],
         MEASURED_SLOTS_PROP: [ACTIVE_SLOT]},
        "arctic2_embed", None, ACTIVE_SLOT,
    ) is None


def test_merge_with_no_active_slot_name_refuses_rather_than_widening():
    assert merge_slot_truncation(
        {TRUNCATED_SLOTS_PROP: [], MEASURED_SLOTS_PROP: []},
        "arctic2_embed", True, "",
    ) is None


def test_reader_answers_unknown_for_a_slot_the_row_never_measured():
    """The shared reader is the other half of the contract."""
    from claude_mcp_servers.weaviate_mcp.rl_enrichment import (
        _stored_slot_truncation_state,
    )

    class _Row:
        properties = {
            TRUNCATED_SLOTS_PROP: [ACTIVE_SLOT],
            SECONDARY_TRUNCATED_SLOTS_PROP: [],
            MEASURED_SLOTS_PROP: [ACTIVE_SLOT],
        }

    assert _stored_slot_truncation_state(
        _Row(), ACTIVE_SLOT, legacy_property_covers_slot=False,
    ) is True
    assert _stored_slot_truncation_state(
        _Row(), "arctic2_embed", legacy_property_covers_slot=True,
    ) is None, (
        "a slot outside the measured set must resolve UNKNOWN — absence from "
        "the truncated list is not evidence of full fidelity"
    )


# ═════════════════════════════════════════════════════════════════════════
# Fake Weaviate + fake sync server
# ═════════════════════════════════════════════════════════════════════════


class _Prop:
    def __init__(self, name):
        self.name = name

    def equal(self, value):
        return _Filt([(self.name, value)])


class _Filt:
    def __init__(self, matchers=None, subfilters=None):
        self.matchers = matchers or []
        self.subfilters = subfilters

    @staticmethod
    def by_property(name):
        return _Prop(name)

    @staticmethod
    def any_of(filters):
        f = _Filt()
        f.subfilters = list(filters)
        return f

    def __and__(self, other):
        return _Filt(self.matchers + other.matchers)

    def matches(self, props):
        if self.subfilters is not None:
            return any(sf.matches(props) for sf in self.subfilters)
        return all(props.get(n) == v for n, v in self.matchers)


class _Obj:
    def __init__(self, uid, props, vector=None):
        self.uuid = uid
        self.properties = props
        self.vector = vector if vector is not None else {}


class _Query:
    def __init__(self, store):
        self._store = store

    def fetch_objects(self, filters=None, limit=100, offset=0,
                      return_properties=None, include_vector=False):
        objs = list(self._store.values())
        if filters is not None:
            objs = [o for o in objs if filters.matches(o.properties)]
        start = offset or 0
        return types.SimpleNamespace(objects=objs[start:start + limit])


class _Data:
    def __init__(self, store):
        self._store = store

    def insert(self, properties=None, vector=None):
        uid = str(uuid.uuid4())
        self._store[uid] = _Obj(uid, dict(properties or {}), vector)
        return uid

    def delete_by_id(self, uid):
        self._store.pop(str(uid), None)

    def reference_add(self, **kwargs):  # noqa: ARG002
        pass


class _Collection:
    def __init__(self, store):
        self._store = store
        self.query = _Query(store)
        self.data = _Data(store)


class _Collections:
    def __init__(self):
        self._stores: dict[str, dict] = {}

    def _store_for(self, name):
        return self._stores.setdefault(name, {})

    def get(self, name):
        return _Collection(self._store_for(name))

    def exists(self, name):  # noqa: ARG002
        return True

    def create(self, **kwargs):  # noqa: ARG002
        pass


class _Client:
    def __init__(self):
        self.collections = _Collections()


class _SyncEmbeddingService:
    text_model_id = QWEN3_MODEL


class _TaggingSyncServer:
    """kg-sync server stand-in whose tagged gather reports a fixed record.

    ``tagged_record=None`` models the gather producing NO active-slot
    vector (service hiccup) — the legacy flat path then falls back to the
    plain embed, for which no honest record exists.
    """

    def __init__(self, record=("arctic2_embed", ACTIVE_SLOT), *, active_vec=True):
        self.client = _Client()
        self.embedding_service = _SyncEmbeddingService()
        self.text_vector_slot = ACTIVE_SLOT
        self._record = list(record)
        self._active_vec = active_vec
        self.plain_embed_calls = 0

    def _get_embedding(self, text):  # noqa: ARG002
        self.plain_embed_calls += 1
        return [0.5, 0.5]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        return {self.text_vector_slot: [0.5, 0.5]}

    def _get_all_kg_embeddings_tagged(self, text):  # noqa: ARG002
        slots = {self.text_vector_slot: [0.5, 0.5]} if self._active_vec else {}
        if self._active_vec:
            slots["arctic2_embed"] = [0.6, 0.6]
        return slots, list(self._record)


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "VCT_PROJECT_ID", "KG_SYNC_PROJECT_ROOT",
    "VCT_STATE_DIR", "WEAVIATE_URL", "EMBEDDING_MODEL", "ACTIVE_EMBEDDING",
    "VCT_ORCHESTRATOR_ROOT",
)


def _load_sync_module(project_root: Path, *, dual: bool = True):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = PROJECT_KG
    os.environ["SHARED_KG_COLLECTION"] = "W3Tag_Shared_KnowledgeGraph"
    os.environ["DEVELOPMENT_COLLECTION"] = DEV_COLL
    os.environ["DUAL_EMBEDDING_ENABLED"] = "true" if dual else "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ["VCT_STATE_DIR"] = str(project_root / ".vct-state-disposable")
    os.environ["WEAVIATE_URL"] = "http://127.0.0.1:1"
    os.environ["EMBEDDING_MODEL"] = QWEN3_MODEL
    os.environ["ACTIVE_EMBEDDING"] = "qwen3"
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)
    os.environ.pop("VCT_PROJECT_ID", None)

    mod_name = f"_sync_w3_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    mod.Filter = _Filt
    return mod


def _sentences(n: int) -> str:
    return " ".join(
        f"Sentence number {i:05d} exercises the truncation tag writer path."
        for i in range(n)
    )


BODY_SMALL = _sentences(50)      # single chunk
BODY_MULTI = _sentences(3000)    # several chunks under the qwen3 preset


def _rows(store, file_path):
    rows = [
        o.properties for o in store.values()
        if o.properties.get("file_path") == file_path
    ]
    rows.sort(key=lambda p: p.get("chunk_num") or 0)
    return rows


class _KgSyncWriterBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in _ENV_KEYS}
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _node(self, rel: str, body: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\ntitle: W3 Node\ntype: concept\ntags: [w3]\nstatus: active\n"
            "created: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n---\n"
            f"{body}\n",
            encoding="utf-8",
        )
        return p

    def _doc(self, rel: str, body: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# W3 Doc\n\n{body}\n", encoding="utf-8")
        return p

    def _run(self, fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = fn(*args)
        return out, buf.getvalue()


# ═════════════════════════════════════════════════════════════════════════
# Part 1 — WRITER 1: sync_knowledge_graph.py (the dominant writer)
# ═════════════════════════════════════════════════════════════════════════


class KgSyncTagsTests(_KgSyncWriterBase):

    def test_sync_node_single_chunk_row_carries_both_properties(self):
        rel = "knowledge/concepts/w3_single.md"
        path = self._node(rel, BODY_SMALL)
        mod = _load_sync_module(self.root)
        server = _TaggingSyncServer()

        outcome, out = self._run(mod.sync_node, server, path)
        self.assertTrue(bool(outcome), out[-500:])

        rows = _rows(server.client.collections._store_for(PROJECT_KG), rel)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0][TRUNCATED_SLOTS_PROP], ["arctic2_embed", ACTIVE_SLOT],
        )
        self.assertEqual(
            rows[0][SECONDARY_TRUNCATED_SLOTS_PROP], ["arctic2_embed"],
        )

    def test_sync_node_multi_chunk_rows_each_carry_their_own_record(self):
        rel = "knowledge/concepts/w3_multi.md"
        path = self._node(rel, BODY_MULTI)
        mod = _load_sync_module(self.root)
        server = _TaggingSyncServer(record=("arctic2_embed",))

        outcome, out = self._run(mod.sync_node, server, path)
        self.assertTrue(bool(outcome), out[-500:])

        rows = _rows(server.client.collections._store_for(PROJECT_KG), rel)
        self.assertGreater(len(rows), 1, "fixture must chunk")
        for row in rows:
            self.assertEqual(row[TRUNCATED_SLOTS_PROP], ["arctic2_embed"])
            self.assertEqual(row[SECONDARY_TRUNCATED_SLOTS_PROP], ["arctic2_embed"])

    def test_sync_node_clean_run_writes_two_empty_lists_not_absence(self):
        """Nothing truncated is a REAL answer — an empty complete record, so
        the reader resolves False rather than UNKNOWN for those rows."""
        rel = "knowledge/concepts/w3_clean.md"
        path = self._node(rel, BODY_SMALL)
        mod = _load_sync_module(self.root)
        server = _TaggingSyncServer(record=())

        self._run(mod.sync_node, server, path)

        rows = _rows(server.client.collections._store_for(PROJECT_KG), rel)
        self.assertEqual(rows[0][TRUNCATED_SLOTS_PROP], [])
        self.assertEqual(rows[0][SECONDARY_TRUNCATED_SLOTS_PROP], [])

    def test_sync_doc_single_and_multi_chunk_rows_carry_the_record(self):
        """The development-collection writer is the same ``_build_vector_arg``
        consumer and was equally untagged."""
        mod = _load_sync_module(self.root)
        for rel, body in (
            ("docs/w3_doc_single.md", BODY_SMALL),
            ("docs/w3_doc_multi.md", BODY_MULTI),
        ):
            with self.subTest(rel=rel):
                path = self._doc(rel, body)
                server = _TaggingSyncServer(record=("arctic2_embed",))
                outcome, out = self._run(mod.sync_doc, server, path)
                self.assertTrue(bool(outcome), out[-500:])
                rows = _rows(
                    server.client.collections._store_for(DEV_COLL), rel,
                )
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(
                        row[TRUNCATED_SLOTS_PROP], ["arctic2_embed"],
                    )

    def test_legacy_flat_mode_records_only_the_stored_active_slot(self):
        """DUAL off stores ONE vector. The record must therefore name only
        the ACTIVE slot — a secondary the fan-out happened to embed has no
        vector on this row and must not appear in its truncation record."""
        rel = "knowledge/concepts/w3_legacy.md"
        path = self._node(rel, BODY_SMALL)
        mod = _load_sync_module(self.root, dual=False)
        server = _TaggingSyncServer(record=("arctic2_embed", ACTIVE_SLOT))

        outcome, out = self._run(mod.sync_node, server, path)
        self.assertTrue(bool(outcome), out[-500:])

        rows = _rows(server.client.collections._store_for(PROJECT_KG), rel)
        self.assertEqual(rows[0][TRUNCATED_SLOTS_PROP], [ACTIVE_SLOT])
        self.assertEqual(rows[0][SECONDARY_TRUNCATED_SLOTS_PROP], [])

    def test_legacy_flat_fallback_embed_records_nothing_at_all(self):
        """When the tagged gather yields no active vector and the plain
        embed supplies it, the stored vector has NO captured verdict — the
        row must carry NO record (UNKNOWN), never an unprovable ``[]``."""
        rel = "knowledge/concepts/w3_legacy_fallback.md"
        path = self._node(rel, BODY_SMALL)
        mod = _load_sync_module(self.root, dual=False)
        server = _TaggingSyncServer(record=(), active_vec=False)

        outcome, out = self._run(mod.sync_node, server, path)
        self.assertTrue(bool(outcome), out[-500:])
        self.assertEqual(server.plain_embed_calls, 1)

        rows = _rows(server.client.collections._store_for(PROJECT_KG), rel)
        self.assertNotIn(TRUNCATED_SLOTS_PROP, rows[0])
        self.assertNotIn(SECONDARY_TRUNCATED_SLOTS_PROP, rows[0])


# ═════════════════════════════════════════════════════════════════════════
# Part 2 — WRITER 2: the MCP store's SINGLE-chunk + legacy branches
# ═════════════════════════════════════════════════════════════════════════


def _mcp():
    return importlib.import_module("weaviate_mcp.server")


def _unwrap(tool):
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


class _SlotStub:
    text_vector_slot = ACTIVE_SLOT


def _drive_mcp_store(monkeypatch, tmp_path, store, *, content, record,
                     dual=True, active_vec=True):
    srv = _mcp()
    coll = _Collection(store)

    class _FakeClient:
        collections = types.SimpleNamespace(get=lambda _n: coll)

    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _FakeClient())
    monkeypatch.setattr(srv, "Filter", _Filt)
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", dual)
    monkeypatch.setattr(srv, "_emit_gate_skipped_metric", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_cached_embed_service", _SlotStub())
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", QWEN3_MODEL)

    plain_calls = []

    async def _tagged(text):  # noqa: ARG001
        slots = {ACTIVE_SLOT: [0.5, 0.5]} if active_vec else {}
        return slots, list(record)

    async def _plain(text):  # noqa: ARG001
        plain_calls.append(text)
        return [0.5, 0.5]

    monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _tagged)
    monkeypatch.setattr(srv, "get_embedding", _plain)

    fn = _unwrap(srv.store_knowledge_node)
    result = json.loads(asyncio.run(fn(
        title="W3 MCP", content=content, node_type="concept", tags=["w3"],
        links=[], file_path="knowledge/concepts/w3_mcp.md", scope="project",
    )))
    return result, plain_calls


def test_mcp_single_chunk_dual_branch_now_tags(monkeypatch, tmp_path):
    """The single-chunk branch — the overwhelming majority of MCP writes —
    used the UNTAGGED gather, so every such row resolved UNKNOWN."""
    store: dict = {}
    result, _ = _drive_mcp_store(
        monkeypatch, tmp_path, store,
        content="short node body", record=["arctic2_embed", ACTIVE_SLOT],
    )
    assert result.get("success") is True
    rows = list(store.values())
    assert len(rows) == 1
    props = rows[0].properties
    assert props[TRUNCATED_SLOTS_PROP] == ["arctic2_embed", ACTIVE_SLOT]
    assert props[SECONDARY_TRUNCATED_SLOTS_PROP] == ["arctic2_embed"]


def test_mcp_legacy_single_chunk_records_only_the_stored_slot(
    monkeypatch, tmp_path,
):
    store: dict = {}
    result, _ = _drive_mcp_store(
        monkeypatch, tmp_path, store, dual=False,
        content="short node body", record=["arctic2_embed", ACTIVE_SLOT],
    )
    assert result.get("success") is True
    props = list(store.values())[0].properties
    assert props[TRUNCATED_SLOTS_PROP] == [ACTIVE_SLOT]
    assert props[SECONDARY_TRUNCATED_SLOTS_PROP] == []


def test_mcp_legacy_multi_chunk_tags_every_chunk(monkeypatch, tmp_path):
    """The legacy MULTI-chunk branch embedded through the bare
    ``get_embedding`` and stamped nothing — the last untagged store leg."""
    store: dict = {}
    body = " ".join(
        f"Sentence number {i:05d} exercises the legacy multi-chunk leg."
        for i in range(3000)
    )
    result, _ = _drive_mcp_store(
        monkeypatch, tmp_path, store, dual=False,
        content=body, record=[ACTIVE_SLOT],
    )
    assert result.get("success") is True
    rows = list(store.values())
    assert len(rows) > 1, "fixture must chunk"
    for obj in rows:
        assert obj.properties[TRUNCATED_SLOTS_PROP] == [ACTIVE_SLOT]
        assert obj.properties[SECONDARY_TRUNCATED_SLOTS_PROP] == []


def test_mcp_legacy_fallback_embed_leaves_the_row_unknown(monkeypatch, tmp_path):
    """No active vector from the capture → the plain embed supplies it →
    no honest record exists → NO property (UNKNOWN, never a guessed []).."""
    store: dict = {}
    result, plain_calls = _drive_mcp_store(
        monkeypatch, tmp_path, store, dual=False, active_vec=False,
        content="short node body", record=[],
    )
    assert result.get("success") is True
    assert plain_calls, "the plain embed must be the vector source here"
    props = list(store.values())[0].properties
    assert TRUNCATED_SLOTS_PROP not in props
    assert SECONDARY_TRUNCATED_SLOTS_PROP not in props


# ═════════════════════════════════════════════════════════════════════════
# Part 3 — WRITER 3: the launcher's enrich-slot backfill
# ═════════════════════════════════════════════════════════════════════════


class _EnrichCollection:
    """Weaviate collection stand-in for ``_run_enrichment_loop``."""

    def __init__(self, objects):
        self._objects = objects
        self.updates: list[dict] = []
        self.data = self
        self.name = "W3Tag_KnowledgeGraph"

    def iterator(self, include_vector=False):  # noqa: ARG002
        return iter(self._objects)

    def update(self, uuid=None, vector=None, properties=None):
        self.updates.append(
            {"uuid": uuid, "vector": vector, "properties": properties},
        )


class _EnrichObj:
    def __init__(self, props, vector=None):
        self.uuid = str(uuid.uuid4())
        self.properties = props
        self.vector = vector or {}


class _EnrichService:
    text_vector_slot = ACTIVE_SLOT
    text_model_id = QWEN3_MODEL
    code_model_id = QWEN3_MODEL

    def embed_text_batch(self, texts):
        return [[0.1, 0.2] for _ in texts]


def _run_enrichment(objects):
    import vco_lib.embedding_enrichment as ee

    coll = _EnrichCollection(objects)

    class _Client:
        class collections:  # noqa: N801
            @staticmethod
            def get(_name):
                return coll

    ee._run_enrichment_loop(
        client=_Client(),
        collection_name="W3Tag_KnowledgeGraph",
        new_slot="arctic2_embed",
        kind="text",
        slot_def=types.SimpleNamespace(name="arctic2_embed", dimensions=1024),
        embedding_service=_EnrichService(),
        progress_callback=lambda *_a: None,
        dry_run=False,
        same_active=False,
    )
    return coll


def test_enrichment_merges_the_fresh_slot_into_an_existing_record():
    """A row that already carries a complete record gains the freshly-filled
    slot's verdict; the vector write is unchanged."""
    huge = "x" * (200_000)   # over EMBED_INPUT_CHAR_CAP → provably a window
    obj = _EnrichObj(
        {"content": huge, TRUNCATED_SLOTS_PROP: [ACTIVE_SLOT],
         MEASURED_SLOTS_PROP: [ACTIVE_SLOT]},
    )
    coll = _run_enrichment([obj])

    assert len(coll.updates) == 1
    upd = coll.updates[0]
    assert upd["vector"] == {"arctic2_embed": [0.1, 0.2]}
    assert upd["properties"][TRUNCATED_SLOTS_PROP] == [
        "arctic2_embed", ACTIVE_SLOT,
    ]
    assert upd["properties"][SECONDARY_TRUNCATED_SLOTS_PROP] == ["arctic2_embed"]
    assert upd["properties"][MEASURED_SLOTS_PROP] == [
        "arctic2_embed", ACTIVE_SLOT,
    ]


def test_enrichment_on_a_pre_record_row_writes_the_vector_only():
    """LEAVE-ALONE twin: the row predates the complete record, so no honest
    single-slot merge exists — the vector still lands, the row stays
    UNKNOWN."""
    obj = _EnrichObj({"content": "x" * 200_000})
    coll = _run_enrichment([obj])

    assert len(coll.updates) == 1
    assert coll.updates[0]["properties"] is None
    assert coll.updates[0]["vector"] == {"arctic2_embed": [0.1, 0.2]}


def test_enrichment_short_content_leaves_the_slot_unmeasured():
    """No positive evidence either way — the batch API hides the per-item
    shrink that runs after a whole-batch refusal — so the patch writes
    NOTHING and the slot resolves UNKNOWN rather than a wrong False."""
    obj = _EnrichObj({"content": "tiny", TRUNCATED_SLOTS_PROP: [ACTIVE_SLOT],
                      MEASURED_SLOTS_PROP: [ACTIVE_SLOT]})
    coll = _run_enrichment([obj])

    upd = coll.updates[0]
    assert upd["vector"] == {"arctic2_embed": [0.1, 0.2]}
    assert upd["properties"] is None


# ═════════════════════════════════════════════════════════════════════════
# Part 4 — WRITER 4: the dual-log backfill store-back
# ═════════════════════════════════════════════════════════════════════════


def test_backfill_store_back_merges_the_verdict_into_the_row():
    """``ensure_slot_embedding`` patches ONE slot on an existing row; the
    store-back must carry the merged record alongside the vector."""
    import claude_mcp_servers.rl_client.embed_regen as er

    captured: dict = {}

    class _Coll:
        name = "W3Tag_KnowledgeGraph"

        class data:  # noqa: N801
            @staticmethod
            def update(uuid=None, vector=None, properties=None):
                captured.update(
                    {"uuid": uuid, "vector": vector, "properties": properties},
                )

    class _Svc:
        text_vector_slot = ACTIVE_SLOT

    async def _drive():
        er._stored_slots.clear()
        # A long text sized down to arctic's preset → provably a leading window.
        vec = er.ensure_slot_embedding(
            "row-uuid",
            "y" * 400_000,
            "arctic2_embed",
            "snowflake-arctic-embed2:latest",
            _Coll(),
            _Svc(),
            embed_fn=lambda _t: [0.7, 0.7],
            existing_props={TRUNCATED_SLOTS_PROP: [ACTIVE_SLOT],
                            MEASURED_SLOTS_PROP: [ACTIVE_SLOT]},
        )
        assert vec == [0.7, 0.7]
        for task in list(er._store_back_tasks):
            await task

    asyncio.run(_drive())

    assert captured["vector"] == {"arctic2_embed": [0.7, 0.7]}
    assert captured["properties"][TRUNCATED_SLOTS_PROP] == [
        "arctic2_embed", ACTIVE_SLOT,
    ]
    assert captured["properties"][SECONDARY_TRUNCATED_SLOTS_PROP] == [
        "arctic2_embed",
    ]
    assert captured["properties"][MEASURED_SLOTS_PROP] == [
        "arctic2_embed", ACTIVE_SLOT,
    ]


def test_backfill_on_a_pre_record_row_stores_the_vector_only():
    import claude_mcp_servers.rl_client.embed_regen as er

    captured: dict = {}

    class _Coll:
        name = "W3Tag_KnowledgeGraph"

        class data:  # noqa: N801
            @staticmethod
            def update(uuid=None, vector=None, properties=None):
                captured.update(
                    {"uuid": uuid, "vector": vector, "properties": properties},
                )

    class _Svc:
        text_vector_slot = ACTIVE_SLOT

    async def _drive():
        er._stored_slots.clear()
        er.ensure_slot_embedding(
            "row-uuid-2", "y" * 400_000, "arctic2_embed",
            "snowflake-arctic-embed2:latest", _Coll(), _Svc(),
            embed_fn=lambda _t: [0.7, 0.7],
            existing_props={"title": "no record here"},
        )
        for task in list(er._store_back_tasks):
            await task

    asyncio.run(_drive())

    assert captured["vector"] == {"arctic2_embed": [0.7, 0.7]}
    assert captured["properties"] is None


# ═════════════════════════════════════════════════════════════════════════
# Part 5 — the failure pointer kg-sync prints must name a file with the row
# ═════════════════════════════════════════════════════════════════════════


def test_total_embed_failure_points_at_a_file_it_actually_wrote(monkeypatch,
                                                                tmp_path):
    """``_build_vector_arg``'s raise told the user to read
    ``~/.claude/metrics/embedding_failures.jsonl``. Two things were wrong:
    v0.2.92 W7 froze that directory as an archive (the live stream is under
    ``$VCT_STATE_DIR/metrics``), and the file only ever received rows from
    the CONSTRUCTION-time ``NoEmbeddingBackendError`` capture — never from
    this path, where construction succeeded and every slot failed at call
    time. The message now names the live file AND puts the row in it."""
    import vco_lib.paths as paths
    # `_load_sync_module` pins VCT_STATE_DIR under the project root, which is
    # the same channel production uses — resolve the expected path AFTER it.
    mod = _load_sync_module(tmp_path / "proj")
    jsonl = paths.vct_metrics_dir() / "embedding_failures.jsonl"
    assert not jsonl.exists()

    server = _TaggingSyncServer(record=(), active_vec=False)
    server._get_all_kg_embeddings_tagged = lambda _t: ({}, [])

    with pytest.raises(RuntimeError) as excinfo:
        mod._build_vector_arg(server, "hello")

    message = str(excinfo.value)
    assert str(jsonl) in message, (
        f"the message must name the LIVE stream, got: {message}"
    )
    assert jsonl.is_file(), "and the named file must actually carry the row"
    rows = [
        json.loads(line)
        for line in jsonl.read_text(encoding="utf-8").splitlines() if line
    ]
    assert any(r.get("kind") == "outage" for r in rows)


if __name__ == "__main__":
    unittest.main()
