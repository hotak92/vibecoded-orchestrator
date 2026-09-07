# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""m-R6-1 (v0.2.92) — the ACTIVE slot's truncation state is PERSISTED like
the secondary's, through the SAME single per-call record and the SAME shared
reader, and reaches the trainer on the MAIN retrieval event.

Pre-fix, ``EmbeddingService.last_active_truncated`` — set when the ACTIVE
slot's vector covers only a leading window after the runner refused the full
text — had NO production reader: a WARNING log was its only consumer, while
the SECONDARY slot's equivalent was persisted (``secondary_truncated_slots``,
R3-2) and carried on the RL event (``emb_truncated``, schema v4). That
inverts the user's priority ordering (P1 retrieval correctness > P2 telemetry
for the MAIN embedder > P3 telemetry for the secondary).

User ruling: "persist like secondary in this release, try to build shared
components where relevant to avoid them diverging in the future, following
the modular code guidelines". Pinned here, end-to-end through the PRODUCTION
entry points:

  Part A — ``embed_text_all_configured_tagged`` (the ONE atomic capture)
           returns the COMPLETE record — the ACTIVE slot included — and the
           two public views (``last_secondary_truncated`` /
           ``last_active_truncated``) are DERIVED from that one record.
  Part B — ``store_knowledge_node`` persists that one capture as TWO chunk
           properties: ``truncated_slots`` (complete record; its presence is
           the era marker) and ``secondary_truncated_slots`` (the active slot
           DROPPED — byte-identical to R3-2's meaning, for back-compat).
  Part C — the enrichment read (``_rl_enrich_nodes_with_linked_embs`` →
           ``_build_log_nodes`` → ``serialize_node_record`` →
           ``resolve_emb_truncation_state``): the ACTIVE slot's state is
           answered by ``truncated_slots``; a row written BEFORE this
           release (property absent) resolves UNKNOWN for the active slot —
           NEVER the False that ``active_slot in secondary_truncated_slots``
           would confidently and wrongly return. The other-slot leg keeps its
           shipped behaviour and now reads through the SAME helper.
"""
from __future__ import annotations

import importlib
import json
import sys
import types
import uuid as _uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.embedding_service import EmbeddingService  # noqa: E402

QWEN3_MODEL = "qwen3-embedding:0.6b"
ARCTIC_MODEL = "snowflake-arctic-embed2:latest"

pytest.importorskip(
    "weaviate_mcp.server",
    reason="weaviate_mcp.server must be importable for the store/enrichment legs",
)


def _srv():
    """Resolve the CURRENT server module at call time (repo convention)."""
    return importlib.import_module("weaviate_mcp.server")


def _unwrap(tool):
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


# ---------------------------------------------------------------------------
# Part A — ONE record, TWO derived views, ONE atomic capture
# ---------------------------------------------------------------------------


class _RefuseList:
    """Ollama stub: refuses per-model inputs over that model's char limit.

    Deliberately a plain stub (no truncation-aware adapter base): the plain
    leg is what a custom adapter hits, and it is where a missing retry would
    be invisible.
    """

    def __init__(self, limits: dict[str, int]) -> None:
        self.limits = limits
        self.calls: list[tuple[str, int]] = []

    def is_reachable(self) -> bool:
        return True

    def embed(self, model, text, num_ctx=None):
        self.calls.append((model, len(text)))
        limit = self.limits.get(model)
        if limit is not None and len(text) > limit:
            raise RuntimeError(
                "Ollama /api/embed HTTP 400: the input length exceeds the "
                "context length of the model"
            )
        return [0.1, 0.2, 0.3]

    def embed_batch(self, model, texts, num_ctx=None):
        return [self.embed(model, t) for t in texts]


def _dual_service(monkeypatch, adapter) -> EmbeddingService:
    """qwen3 ACTIVE + arctic SECONDARY (write-all + arctic gate on)."""
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", QWEN3_MODEL)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
    svc = EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=QWEN3_MODEL,
        code_model_id=QWEN3_MODEL,
        openai_api_key="",
        ollama_adapter=adapter,  # type: ignore[arg-type]
    )
    svc._text_slot = "qwen3_embed"
    return svc


def test_tagged_capture_includes_active_and_secondary_in_one_record(monkeypatch):
    """BOTH slots refused → the ONE capture lists BOTH, sorted; the two public
    views derive from that same record (secondary view never shows the active
    slot — its pre-v0.2.92 contract — while ``last_active_truncated`` does)."""
    adapter = _RefuseList({QWEN3_MODEL: 13_000, ARCTIC_MODEL: 3_000})
    svc = _dual_service(monkeypatch, adapter)

    slots, truncated = svc.embed_text_all_configured_tagged("z" * 20_000)

    assert "qwen3_embed" in slots and "arctic2_embed" in slots
    assert truncated == ["arctic2_embed", "qwen3_embed"], (
        "the atomic capture must return the COMPLETE per-call record — the "
        "ACTIVE slot's truncation included, else the store cannot persist it"
    )
    assert svc.last_active_truncated is True, (
        "the active view derives from the same record the capture returned"
    )
    secondary = svc.last_secondary_truncated
    assert secondary == {"arctic2_embed": True}, (
        "the secondary view is the same record minus the active slot — its "
        "pre-v0.2.92 contract is frozen for back-compat"
    )
    assert "qwen3_embed" not in secondary


def test_tagged_capture_empty_and_views_clear_when_nothing_truncates(monkeypatch):
    """LEAVE-ALONE twin: nothing refused → empty record, both views negative."""
    adapter = _RefuseList({QWEN3_MODEL: 10**9, ARCTIC_MODEL: 10**9})
    svc = _dual_service(monkeypatch, adapter)

    slots, truncated = svc.embed_text_all_configured_tagged("z" * 5_000)

    assert truncated == []
    assert svc.last_active_truncated is False
    assert svc.last_secondary_truncated == {}


def test_active_only_truncation_leaves_secondary_view_empty(monkeypatch):
    """The ACTIVE alone truncates → the record lists only it; the secondary
    view (and therefore the legacy property derived from it) stays empty.

    Sizing: a 6 000-char input fits arctic's SECONDARY budget (7 065 chars,
    so the arctic leg embeds it whole and tags nothing) while the ACTIVE
    qwen3 runner refuses anything over 3 000 → the active shrinks alone."""
    adapter = _RefuseList({QWEN3_MODEL: 3_000})
    svc = _dual_service(monkeypatch, adapter)

    slots, truncated = svc.embed_text_all_configured_tagged("z" * 6_000)

    assert truncated == ["qwen3_embed"]
    assert svc.last_active_truncated is True
    assert svc.last_secondary_truncated == {}


# ---------------------------------------------------------------------------
# Part B — the STORE persists the complete record + the derived secondary view
# ---------------------------------------------------------------------------


class _FakeObj:
    def __init__(self, properties: dict, vector=None):
        self.uuid = str(_uuid.uuid4())
        self.properties = properties
        self.vector = vector or {}


class _FakePredicate:
    def __init__(self, fn):
        self._fn = fn

    def matches(self, props: dict) -> bool:
        return bool(self._fn(props))

    def __and__(self, other):
        return _FakePredicate(lambda p: self.matches(p) and other.matches(p))


class _FakeByProperty:
    def __init__(self, name: str):
        self._name = name

    def equal(self, value):
        return _FakePredicate(lambda p, n=self._name, v=value: p.get(n) == v)


class _FakeFilter:
    @staticmethod
    def by_property(name: str):
        return _FakeByProperty(name)

    @staticmethod
    def any_of(predicates):
        preds = list(predicates)
        return _FakePredicate(lambda p: any(pr.matches(p) for pr in preds))


class _FakeQuery:
    def __init__(self, coll):
        self._coll = coll

    def fetch_objects(self, filters=None, limit=100, offset=0):
        objs = [
            o for o in self._coll.objects
            if filters is None or filters.matches(o.properties)
        ]
        start = offset or 0
        return types.SimpleNamespace(objects=objs[start:start + limit])


class _FakeData:
    def __init__(self, coll):
        self._coll = coll

    def insert(self, properties=None, vector=None):
        self._coll.objects.append(_FakeObj(dict(properties or {}), vector))


class _FakeCollection:
    def __init__(self):
        self.objects: list = []
        self.query = _FakeQuery(self)
        self.data = _FakeData(self)


class _FakeCollections:
    def __init__(self, coll):
        self._coll = coll

    def get(self, name):
        return self._coll


class _FakeClient:
    def __init__(self, coll):
        self.collections = _FakeCollections(coll)


class _SlotStub:
    """Stand-in for the session-cached EmbeddingService: slot name only."""

    text_vector_slot = "qwen3_embed"


def _patch_store(monkeypatch, tmp_path, coll, all_tag_map):
    """Patch server for the DUAL multi-chunk write with the bridge returning
    the COMPLETE truncation record (``all_tag_map``: chunk text → all-list)
    and a warm session cache exposing the active slot name."""
    srv = _srv()
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _FakeClient(coll))
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", True)
    monkeypatch.setattr(srv, "_emit_gate_skipped_metric", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", lambda *a, **k: None)
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", QWEN3_MODEL)
    # The active-slot name the writer derives the secondary view from. In
    # production this is the session-cached service the capture just ran
    # through; here a stub with the same surface.
    monkeypatch.setattr(srv, "_cached_embed_service", _SlotStub())

    async def _count_tokens(content):
        from claude_mcp_servers.weaviate_mcp.chunking import TokenCounter
        return TokenCounter.count_tokens(content)

    monkeypatch.setattr(srv, "count_tokens_async", _count_tokens)

    async def _tagged(chunk_text):
        vectors = {"qwen3_embed": [0.1, 0.2], "arctic2_embed": [0.3, 0.4]}
        return vectors, list(all_tag_map(chunk_text))

    monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _tagged)
    return srv


def _big_multichunk_text() -> str:
    # Two ~33.8k-char chunks + a ~1.4k tail under the qwen3 preset: the big
    # leading chunks "truncate" (per the tag map), the tail does not.
    sentence = "The retrieval model scores each candidate node against the query. "
    return " ".join(
        f"{sentence}Item {i} details here and more words to pad this nicely."
        for i in range(560)
    )


def _store(srv, **kwargs) -> dict:
    fn = _unwrap(srv.store_knowledge_node)
    defaults = dict(
        title="Active Persist",
        content=_big_multichunk_text(),
        node_type="concept",
        tags=["sample"],
        links=[],
        file_path="knowledge/concepts/active_persist.md",
        scope="project",
    )
    defaults.update(kwargs)
    return json.loads(__import__("asyncio").run(fn(**defaults)))


def test_store_persists_complete_record_and_derived_secondary_view(
    monkeypatch, tmp_path
):
    """ONE capture, TWO properties: ``truncated_slots`` carries the COMPLETE
    record (active included); ``secondary_truncated_slots`` is the same list
    with the active slot DROPPED — the R3-2 property's meaning is unchanged."""
    def all_tag_map(chunk_text: str) -> list[str]:
        if len(chunk_text) > 20_000:
            return ["arctic2_embed", "qwen3_embed"]
        return []

    coll = _FakeCollection()
    srv = _patch_store(monkeypatch, tmp_path, coll, all_tag_map)
    result = _store(srv)
    assert result.get("success") is True
    stored = coll.objects
    assert stored, "chunks were written"

    truncated_rows, full_rows = [], []
    for obj in stored:
        assert "truncated_slots" in obj.properties, (
            "every dual-write chunk row must carry the complete-record property "
            "(its PRESENCE is the era marker readers branch on)"
        )
        assert "secondary_truncated_slots" in obj.properties
        if "qwen3_embed" in obj.properties["truncated_slots"]:
            truncated_rows.append(obj)
        else:
            full_rows.append(obj)
    assert truncated_rows and full_rows, (
        "a mixed corpus proves the per-chunk partition on BOTH properties"
    )
    for obj in truncated_rows:
        assert obj.properties["truncated_slots"] == ["arctic2_embed", "qwen3_embed"]
        assert obj.properties["secondary_truncated_slots"] == ["arctic2_embed"], (
            "the active slot must NEVER appear in secondary_truncated_slots — "
            "widening that stored property's meaning would silently reinterpret "
            "every row written before this release"
        )
    for obj in full_rows:
        assert obj.properties["truncated_slots"] == []
        assert obj.properties["secondary_truncated_slots"] == []


def test_store_no_truncation_writes_two_empty_lists(monkeypatch, tmp_path):
    """Nothing truncates → both properties are EMPTY lists (a clean 'every
    slot full-fidelity' answer on new rows — not a missing property)."""
    coll = _FakeCollection()
    srv = _patch_store(monkeypatch, tmp_path, coll, all_tag_map=lambda _t: [])
    result = _store(srv)
    assert result.get("success") is True
    for obj in coll.objects:
        assert obj.properties.get("truncated_slots") == []
        assert obj.properties.get("secondary_truncated_slots") == []


# ---------------------------------------------------------------------------
# Part C — the enrichment read through the production fan-out
# ---------------------------------------------------------------------------


class _VecObj:
    """A fetched chunk row: properties + named vectors."""

    def __init__(self, properties: dict, vectors: dict):
        self.uuid = str(_uuid.uuid4())
        self.properties = properties
        self.vector = vectors


class _Coll:
    def __init__(self, objects):
        self._objects = objects

        class _Q:
            @staticmethod
            def fetch_objects(**kw):
                return types.SimpleNamespace(objects=list(objects))

        self.query = _Q()


def _enrich(monkeypatch, objects, nodes, **kwargs):
    """Run the production enrichment fan-out over a faked collection."""
    srv = _srv()
    monkeypatch.setattr(srv, "_rl_enrichment_gate_open", lambda: True)
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: None)
    srv._rl_enrich_nodes_with_linked_embs(
        nodes,
        query_emb=[1.0, 0.0],
        active_slot=kwargs.get("active_slot", "qwen3_embed"),
        coll_resolver=lambda name: _Coll(objects),
        other_slot=kwargs.get("other_slot", ""),
        other_query_emb=kwargs.get("other_query_emb"),
    )
    return nodes


def _node(title: str) -> dict:
    return {
        "title": title,
        "collection": "Sample_KnowledgeGraph",
        "source_id": title,
        "chunk_number": 1,
        "emb": [0.5, 0.5],
        "score": 0.9,
    }


def _serialized_state(node: dict, schema_version: int) -> str:
    from claude_mcp_servers.rl_client.rl_logger import (
        resolve_emb_truncation_state,
        serialize_node_record,
    )
    from claude_mcp_servers.rl_client.search_pipeline import _build_log_nodes

    recs = _build_log_nodes([node], limit=10)
    serialized = [serialize_node_record(r) for r in recs]
    return resolve_emb_truncation_state(serialized[0], schema_version)


def test_active_truncated_new_row_resolves_true_end_to_end(monkeypatch):
    """New row whose complete record lists the active slot → the MAIN event's
    per-node ``emb_truncated`` is True through the full production chain."""
    obj = _VecObj(
        {
            "source_node_id": "NewTrunc",
            "title": "NewTrunc",
            "chunk_num": 1,
            "truncated_slots": ["qwen3_embed"],
            "secondary_truncated_slots": [],
        },
        {"qwen3_embed": [0.5, 0.5]},
    )
    nodes = _enrich(monkeypatch, [obj], [_node("NewTrunc")])
    assert nodes[0]["emb_truncated"] is True, (
        "the enrichment site must attach the ACTIVE slot's persisted state"
    )
    assert _serialized_state(nodes[0], 4) == "true"


def test_active_full_fidelity_new_row_resolves_false_end_to_end(monkeypatch):
    """New row whose complete record is EMPTY → an explicit False — a real
    full-fidelity answer, not a default."""
    obj = _VecObj(
        {
            "source_node_id": "NewFull",
            "title": "NewFull",
            "chunk_num": 1,
            "truncated_slots": [],
            "secondary_truncated_slots": [],
        },
        {"qwen3_embed": [0.5, 0.5]},
    )
    nodes = _enrich(monkeypatch, [obj], [_node("NewFull")])
    assert nodes[0]["emb_truncated"] is False
    assert _serialized_state(nodes[0], 4) == "false"


def test_old_row_resolves_unknown_for_active_never_false(monkeypatch):
    """THE historical case: a row written BEFORE this release carries only
    ``secondary_truncated_slots``. The naive ``active_slot in
    secondary_truncated_slots`` yields False — a confident wrong answer; the
    shared reader must resolve UNKNOWN (field absent end-to-end)."""
    obj = _VecObj(
        {
            "source_node_id": "OldRow",
            "title": "OldRow",
            "chunk_num": 1,
            # Pre-v0.2.92 row: the R3-2 property, secondaries only. Note the
            # active-at-read slot is NOT in it — the exact shape whose naive
            # membership read returns False.
            "secondary_truncated_slots": ["arctic2_embed"],
        },
        {"qwen3_embed": [0.5, 0.5]},
    )
    nodes = _enrich(monkeypatch, [obj], [_node("OldRow")])
    assert "emb_truncated" not in nodes[0], (
        "an old row cannot answer for the active slot — the state must stay "
        "UNSET, never a False coerced out of the secondary-only property"
    )
    assert _serialized_state(nodes[0], 4) == "unknown"


def test_old_row_listing_read_time_active_still_unknown(monkeypatch):
    """Harder variant: the old row's secondary list NAMES the read-time
    active slot (written when it was a secondary). Still UNKNOWN — whether
    the slot was secondary at write time is not recorded, so its presence in
    the legacy list is a coincidence, not an answer the property covers."""
    obj = _VecObj(
        {
            "source_node_id": "OldCoincidence",
            "title": "OldCoincidence",
            "chunk_num": 1,
            "secondary_truncated_slots": ["qwen3_embed"],
        },
        {"qwen3_embed": [0.5, 0.5]},
    )
    nodes = _enrich(monkeypatch, [obj], [_node("OldCoincidence")])
    assert "emb_truncated" not in nodes[0], (
        "the legacy property never covers the ACTIVE slot — even a listing "
        "is not evidence, because slot roles are not recorded on old rows"
    )
    assert _serialized_state(nodes[0], 4) == "unknown"


def test_secondary_leg_reads_through_shared_reader_on_new_rows(monkeypatch):
    """The OTHER-slot leg (shipped v4 behaviour) on a NEW row: answered from
    the same complete record by the SAME helper — True where listed."""
    obj = _VecObj(
        {
            "source_node_id": "Dual",
            "title": "Dual",
            "chunk_num": 1,
            "truncated_slots": ["arctic2_embed"],
            "secondary_truncated_slots": ["arctic2_embed"],
        },
        {"qwen3_embed": [0.5, 0.5], "arctic2_embed": [0.4, 0.4]},
    )
    nodes = _enrich(
        monkeypatch, [obj], [_node("Dual")],
        other_slot="arctic2_embed", other_query_emb=[1.0, 0.0],
    )
    assert nodes[0]["emb_other_truncated"] is True
    assert nodes[0]["emb_truncated"] is False, (
        "the same row's active slot is full-fidelity — both legs answered "
        "from the ONE complete record"
    )


def test_no_representative_object_leaves_state_unknown(monkeypatch):
    """No fetched row for the node → no state attached (UNKNOWN), never a
    guess — the honest degradation."""
    nodes = _enrich(monkeypatch, [], [_node("Ghost")])
    assert "emb_truncated" not in nodes[0]
    assert _serialized_state(nodes[0], 4) == "unknown"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
