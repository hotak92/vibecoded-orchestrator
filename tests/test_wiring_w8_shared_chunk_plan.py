# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W8 (v0.2.92 wiring audit) — ONE chunk plan, both KG writers.

The defect
----------
The two production writers of KG rows disagreed on the plan for the SAME
node:

===================  =============================  ========================
                     MCP ``store_knowledge_node``   ``sync_knowledge_graph``
===================  =============================  ========================
single/multi gate    hardcoded 2 000 tokens         active model preset max
                     ("legacy arctic limit")        (8 192 for qwen3)
measured with        ``count_tokens_async``         ``TokenCounter``
stored chunk text    ``"[chunk i/N]\\n\\n" + text``   RAW ``chunk.content``
``source_node_id``   the TITLE                      per-write ``uuid4``
``content_hash``     not written                    written
===================  =============================  ========================

Consequences: a 2 001–8 192-token qwen3 node was multi-chunk through the
MCP and single-chunk through kg-sync; an MCP-written row was re-planned
and re-embedded by the next ``kg-sync --all``; and
``_stored_plan_matches_current`` (the ``--rechunk`` comparison that decides
what to re-embed) could never judge an MCP-written row current, because
its stored content carried a prefix the current chunker never produces.

The fix is SHARED CODE, not a patched side: both writers (and the plan
comparison, and the shipped-sidecar generator) call
``weaviate_mcp.kg_chunk_plan.plan_node_chunks``.

What these tests do
-------------------
Feed the SAME node through BOTH production entry points — ``sync_node``
and ``store_knowledge_node`` — against in-memory fake Weaviate stores, and
assert the stored plans are IDENTICAL: same chunk count, same
``chunk_num`` / ``total_chunks``, byte-identical ``content`` per chunk, and
a ``content_hash`` from both. Mutating ``plan_node_chunks`` turns both
sides red, which is the property a per-side test could not give.
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
import types
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
QWEN3_MODEL = "qwen3-embedding:0.6b"
PROJECT_KG = "W8Parity_KnowledgeGraph"


# ─── Fake Weaviate (shared by both entry points) ─────────────────────────


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


# ─── kg-sync side ────────────────────────────────────────────────────────


class _SyncEmbeddingService:
    text_model_id = QWEN3_MODEL


class _SyncServer:
    def __init__(self):
        self.client = _Client()
        self.embedding_service = _SyncEmbeddingService()
        self.text_vector_slot = "qwen3_embed"

    def _get_embedding(self, text):  # noqa: ARG002
        return [0.5, 0.5]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        return {self.text_vector_slot: [0.5, 0.5]}

    def _get_all_kg_embeddings_tagged(self, text):  # noqa: ARG002
        return {self.text_vector_slot: [0.5, 0.5]}, []


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "VCT_PROJECT_ID", "KG_SYNC_PROJECT_ROOT",
    "VCT_STATE_DIR", "WEAVIATE_URL", "EMBEDDING_MODEL", "ACTIVE_EMBEDDING",
    "VCT_ORCHESTRATOR_ROOT",
)


def _load_sync_module(project_root: Path):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = PROJECT_KG
    os.environ["SHARED_KG_COLLECTION"] = "W8Parity_Shared_KnowledgeGraph"
    os.environ["DEVELOPMENT_COLLECTION"] = "W8Parity_Development"
    os.environ["DUAL_EMBEDDING_ENABLED"] = "true"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ["VCT_STATE_DIR"] = str(project_root / ".vct-state-disposable")
    os.environ["WEAVIATE_URL"] = "http://127.0.0.1:1"
    os.environ["EMBEDDING_MODEL"] = QWEN3_MODEL
    os.environ["ACTIVE_EMBEDDING"] = "qwen3"
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)
    os.environ.pop("VCT_PROJECT_ID", None)

    mod_name = f"_sync_w8_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    mod.Filter = _Filt
    return mod


# ─── MCP side ────────────────────────────────────────────────────────────


def _mcp_server():
    return importlib.import_module("weaviate_mcp.server")


def _unwrap(tool):
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


class _SlotStub:
    text_vector_slot = "qwen3_embed"


def _mcp_store(monkeypatch, kg_base: Path, store: dict, *, title, file_path,
               content, dual=True):
    """Run ``store_knowledge_node`` against an in-memory collection."""
    srv = _mcp_server()
    coll = _Collection(store)

    class _FakeClient:
        collections = types.SimpleNamespace(get=lambda _n: coll)

    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _FakeClient())
    monkeypatch.setattr(srv, "Filter", _Filt)
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(kg_base))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", dual)
    monkeypatch.setattr(srv, "_emit_gate_skipped_metric", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_cached_embed_service", _SlotStub())
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", QWEN3_MODEL)

    async def _tagged(text):  # noqa: ARG001
        return {"qwen3_embed": [0.5, 0.5]}, []

    async def _plain(text):  # noqa: ARG001
        return [0.5, 0.5]

    monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _tagged)
    monkeypatch.setattr(srv, "get_embedding", _plain)

    fn = _unwrap(srv.store_knowledge_node)
    return json.loads(asyncio.run(fn(
        title=title, content=content, node_type="concept", tags=["w8"],
        links=[], file_path=file_path, scope="project",
    )))


# ─── content fixtures ────────────────────────────────────────────────────


def _sentences(n: int) -> str:
    return " ".join(
        f"Sentence number {i:05d} exercises the shared chunk plan today."
        for i in range(n)
    )


#: ~7 800 counter-units: OVER the retired MCP gate (2 000) and UNDER the
#: qwen3 preset max (8 192). Pre-fix this was the divergence: the MCP
#: chunked it, kg-sync stored it whole.
BODY_BETWEEN_THE_TWO_GATES = _sentences(450)

#: Comfortably over 8 192 units: BOTH writers chunk — boundaries and stored
#: text must match byte-for-byte (pre-fix the MCP prefixed "[chunk i/N]").
BODY_MULTI = _sentences(3000)


def _plan_rows(store: dict, file_path: str):
    rows = [
        o.properties for o in store.values()
        if o.properties.get("file_path") == file_path
    ]
    rows.sort(key=lambda p: p.get("chunk_num") or 0)
    return rows


def _shape(rows):
    return [
        (r.get("chunk_num"), r.get("total_chunks"), r.get("content"))
        for r in rows
    ]


class W8SharedChunkPlanTests(unittest.TestCase):
    """The SAME node through BOTH writers must produce the SAME stored plan."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in _ENV_KEYS}
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)
        # monkeypatch-equivalent for a unittest.TestCase
        from _pytest.monkeypatch import MonkeyPatch
        self.mp = MonkeyPatch()
        self.addCleanup(self.mp.undo)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- helpers ---------------------------------------------------------

    def _write_node(self, rel: str, title: str, body: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            f"title: {title}\n"
            "type: concept\n"
            "tags: [w8]\n"
            "status: active\n"
            "created: 2026-01-01T00:00:00Z\n"
            "updated: 2026-01-01T00:00:00Z\n"
            "---\n"
            f"{body}\n",
            encoding="utf-8",
        )
        return path

    def _both_plans(self, rel: str, title: str, body: str):
        """(kg-sync rows, MCP rows) for the SAME final node text.

        kg-sync runs FIRST and may rewrite the ``updated:`` frontmatter line;
        the MCP is then handed the text as it exists ON DISK afterwards, so
        both writers demonstrably plan over byte-identical input.
        """
        path = self._write_node(rel, title, body)
        mod = _load_sync_module(self.root)
        server = _SyncServer()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_node(server, path)
        self.assertTrue(bool(outcome), f"sync_node failed: {buf.getvalue()[-600:]}")
        sync_rows = _plan_rows(
            server.client.collections._store_for(PROJECT_KG), rel,
        )

        final_text = path.read_text(encoding="utf-8")
        mcp_store: dict = {}
        mcp_root = self.root / "_mcp_root"
        result = _mcp_store(
            self.mp, mcp_root, mcp_store,
            title=title, file_path=rel, content=final_text,
        )
        self.assertTrue(result.get("success"), result)
        mcp_rows = _plan_rows(mcp_store, rel)
        return sync_rows, mcp_rows, final_text

    # -- tests -----------------------------------------------------------

    def test_node_between_the_two_gates_is_single_chunk_on_both(self):
        """~7 800 units: over the retired 2 000-token MCP gate, under the
        qwen3 preset max. Pre-fix the MCP chunked it and kg-sync did not —
        the exact input that made an MCP-written node get re-planned and
        re-embedded by the next ``kg-sync --all``."""
        sync_rows, mcp_rows, final_text = self._both_plans(
            "knowledge/concepts/w8_between.md", "W8 Between", BODY_BETWEEN_THE_TWO_GATES,
        )
        from weaviate_mcp.chunking import TokenCounter
        units = TokenCounter.count_tokens(final_text)
        self.assertGreater(units, 2_000, "fixture must exceed the retired MCP gate")
        self.assertLessEqual(units, 8_192, "fixture must fit the qwen3 preset max")

        self.assertEqual(len(sync_rows), 1)
        self.assertEqual(len(mcp_rows), 1)
        self.assertEqual(_shape(sync_rows), _shape(mcp_rows))
        self.assertEqual(mcp_rows[0]["content"], final_text)

    def test_multi_chunk_node_has_identical_boundaries_and_raw_text(self):
        """Both writers chunk, with byte-identical boundaries and NO
        ``[chunk i/N]`` prefix in the stored text (the prefix is what made
        the plan comparison unable to judge an MCP row current)."""
        sync_rows, mcp_rows, _final = self._both_plans(
            "knowledge/concepts/w8_multi.md", "W8 Multi", BODY_MULTI,
        )
        self.assertGreater(len(sync_rows), 1, "fixture must chunk")
        self.assertEqual(_shape(sync_rows), _shape(mcp_rows))
        for row in mcp_rows:
            self.assertNotIn("[chunk ", row["content"][:40])
        self.assertEqual(
            [r["chunk_num"] for r in mcp_rows],
            list(range(1, len(mcp_rows) + 1)),
        )
        self.assertEqual(
            {r["total_chunks"] for r in mcp_rows}, {len(mcp_rows)},
        )

    def test_both_writers_stamp_the_same_content_hash(self):
        """The MCP wrote no ``content_hash`` at all, so kg-sync's embed-skip
        could never fast-path an MCP-written node. Both now stamp the SAME
        storage-layer signature."""
        sync_rows, mcp_rows, _final = self._both_plans(
            "knowledge/concepts/w8_hash.md", "W8 Hash", BODY_MULTI,
        )
        sync_hashes = {r.get("content_hash") for r in sync_rows}
        mcp_hashes = {r.get("content_hash") for r in mcp_rows}
        self.assertEqual(len(sync_hashes), 1)
        self.assertEqual(sync_hashes, mcp_hashes)
        self.assertTrue(next(iter(mcp_hashes)))

    def test_mcp_source_node_id_is_a_per_write_id_shared_by_its_chunks(self):
        """It was the TITLE — which collides under duplicate titles (2 live
        collisions per collection) and made ``_fetch_adjacent_chunks``'s old
        ``source_node_id == title`` filter match the wrong node."""
        _sync_rows, mcp_rows, _final = self._both_plans(
            "knowledge/concepts/w8_snid.md", "W8 Snid", BODY_MULTI,
        )
        ids = {r.get("source_node_id") for r in mcp_rows}
        self.assertEqual(len(ids), 1, "one id shared by every chunk of the write")
        self.assertNotIn("W8 Snid", ids)
        self.assertTrue(next(iter(ids)))

    def test_the_plan_comparison_judges_an_mcp_written_node_current(self):
        """End of the loop: after an MCP write, ``_stored_plan_matches_current``
        (what ``--rechunk`` consults) must judge those rows CURRENT — pre-fix
        it never could, so the remedy re-embedded them every run."""
        title, rel = "W8 Judge", "knowledge/concepts/w8_judge.md"
        path = self._write_node(rel, title, BODY_MULTI)
        final_text = path.read_text(encoding="utf-8")

        mcp_store: dict = {}
        result = _mcp_store(
            self.mp, self.root / "_mcp_root2", mcp_store,
            title=title, file_path=rel, content=final_text,
        )
        self.assertTrue(result.get("success"), result)

        mod = _load_sync_module(self.root)
        server = _SyncServer()
        # Hand the sync module the MCP's rows as its stored state.
        server.client.collections._stores[PROJECT_KG] = mcp_store

        self.assertTrue(
            mod._stored_plan_matches_current(
                server, _Collection(mcp_store), rel, final_text, len(mcp_store),
            ),
            "the shared plan must judge the MCP's own rows current",
        )


if __name__ == "__main__":
    unittest.main()
