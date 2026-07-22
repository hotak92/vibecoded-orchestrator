# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R3-2 — the secondary-truncated tag is PERSISTED end-to-end and reconstructable.

Before this fix the ``secondary_truncated`` fact lived only in-process
(``EmbeddingService.last_secondary_truncated``) + an INFO log — the Weaviate chunk
row carried no truncated property, so a sub-window arctic vector was
INDISTINGUISHABLE from a full-fidelity one in the DB, and the stated purpose
("per-model dataset assembly can partition the tagged-truncated arctic vectors
cleanly") was impossible from stored data.

Fix (this file pins it):
  1. ``EmbeddingService.embed_text_all_configured_tagged`` returns the truncated
     slot names ATOMICALLY with the vectors (no cross-task race on the per-instance
     record).
  2. ``store_knowledge_node`` persists that list as the ``secondary_truncated_slots``
     Weaviate chunk property on the DUAL-embedding write path.

So the partition is reconstructable from STORED DATA ALONE: a chunk row whose
``secondary_truncated_slots`` contains ``arctic2_embed`` had its arctic vector
embedded from a bounded sub-window; a row with an empty list is full-fidelity on
every stored secondary.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import uuid as _uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.embedding_service import (  # noqa: E402
    EmbeddingService,
    _CHARS_PER_TOKEN,
)

_ARCTIC_MODEL = "snowflake-arctic-embed2:latest"
_ARCTIC_NUM_CTX = 4096
_ARCTIC_CHAR_BUDGET = _ARCTIC_NUM_CTX * _CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Part 1 — the service tags truncation ATOMICALLY with the vectors
# ---------------------------------------------------------------------------


class _StubOllama:
    """Ollama adapter stub: embeds anything (deterministic vector), reachable."""

    def __init__(self) -> None:
        self.embed_calls: list[tuple[str, int]] = []

    def is_reachable(self) -> bool:
        return True

    def embed(self, model, text, num_ctx=None):
        self.embed_calls.append((model, len(text)))
        return [0.1, 0.2, 0.3, 0.4]

    def embed_batch(self, model, texts, num_ctx=None):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _arctic_secondary_service(monkeypatch, stub: _StubOllama) -> EmbeddingService:
    """qwen3 ACTIVE + arctic SECONDARY (write-all + arctic gate on)."""
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
    svc = EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id="qwen3-embedding:0.6b",
        code_model_id="qwen3-embedding:0.6b",
        openai_api_key="",
        ollama_adapter=stub,  # type: ignore[arg-type]
    )
    svc._text_slot = "qwen3_embed"
    return svc


def test_tagged_returns_truncated_slot_atomically(monkeypatch):
    """A chunk larger than arctic's num_ctx budget → the arctic secondary is
    embedded from a sub-window and ``embed_text_all_configured_tagged`` reports
    ``arctic2_embed`` truncated, in the SAME return as the vectors."""
    stub = _StubOllama()
    svc = _arctic_secondary_service(monkeypatch, stub)
    big = "x" * (_ARCTIC_CHAR_BUDGET + 5000)  # exceeds arctic, within qwen3
    slots, truncated = svc.embed_text_all_configured_tagged(big)
    assert "qwen3_embed" in slots, "active slot always written"
    assert "arctic2_embed" in slots, "arctic secondary written"
    assert truncated == ["arctic2_embed"], (
        "the arctic secondary was sub-windowed → tagged truncated atomically"
    )


def test_tagged_empty_when_within_all_windows(monkeypatch):
    """A small chunk fits every window → no secondary truncated → empty tag."""
    stub = _StubOllama()
    svc = _arctic_secondary_service(monkeypatch, stub)
    small = "y" * 200
    slots, truncated = svc.embed_text_all_configured_tagged(small)
    assert "arctic2_embed" in slots
    assert truncated == [], "nothing exceeded a secondary's num_ctx"


# ---------------------------------------------------------------------------
# Part 2 — store_knowledge_node PERSISTS the tag; partition reconstructable
# ---------------------------------------------------------------------------


def _server():
    return importlib.import_module("weaviate_mcp.server")


def _unwrap(tool):
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


class _FakeObj:
    def __init__(self, properties: dict):
        self.uuid = str(_uuid.uuid4())
        self.properties = properties


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

        class _Resp:
            pass

        resp = _Resp()
        start = offset or 0
        resp.objects = objs[start:start + limit]
        return resp


class _FakeData:
    def __init__(self, coll):
        self._coll = coll

    def insert(self, properties=None, vector=None):
        self._coll.objects.append(_FakeObj(dict(properties or {})))

    def delete_by_id(self, uid):
        self._coll.objects = [o for o in self._coll.objects if o.uuid != uid]


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


def _big_multichunk_text() -> str:
    # Large enough to split into multiple qwen3 chunks; the leading chunk exceeds
    # arctic's num_ctx (so its arctic secondary truncates) and the small trailing
    # chunk fits (so its arctic secondary does NOT) — a mixed corpus that PROVES
    # per-chunk partitioning.
    sentence = "The retrieval model scores each candidate node against the query. "
    return " ".join(
        f"{sentence}Item {i} details here and more words to pad this nicely."
        for i in range(700)
    )


def _patch_server_for_dual_write(monkeypatch, tmp_path, coll, tag_map):
    """Patch server so store_knowledge_node runs the DUAL multi-chunk write,
    with ``_get_all_kg_embeddings_tagged`` returning a per-chunk truncated tag
    from ``tag_map`` (a predicate on chunk text → truncated slot list)."""
    srv = _server()
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _FakeClient(coll))
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", True)
    monkeypatch.setattr(srv, "_emit_gate_skipped_metric", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", lambda *a, **k: None)
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")

    async def _count_tokens(content):
        from claude_mcp_servers.weaviate_mcp.chunking import TokenCounter
        return TokenCounter.count_tokens(content)

    monkeypatch.setattr(srv, "count_tokens_async", _count_tokens)

    async def _tagged(chunk_text):
        # Deterministic vectors + a per-chunk truncated tag from tag_map.
        truncated = tag_map(chunk_text)
        vectors = {"qwen3_embed": [0.1, 0.2], "arctic2_embed": [0.3, 0.4]}
        return vectors, list(truncated)

    monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _tagged)
    return srv


def _store(srv, **kwargs) -> dict:
    fn = _unwrap(srv.store_knowledge_node)
    defaults = dict(
        title="R3-2 Persist Tag",
        content=_big_multichunk_text(),
        node_type="concept",
        tags=["sample"],
        links=[],
        file_path="knowledge/concepts/r3_2_persist.md",
        scope="project",
    )
    defaults.update(kwargs)
    raw = asyncio.run(fn(**defaults))
    return json.loads(raw)


def test_store_persists_secondary_truncated_slots_per_chunk(monkeypatch, tmp_path):
    """The DUAL write persists ``secondary_truncated_slots`` on every chunk row,
    and the truncated-vs-full partition is reconstructable from STORED DATA."""
    from claude_mcp_servers.weaviate_mcp.chunking import Chunker

    body = _big_multichunk_text()
    chunks = Chunker.for_model("qwen3-embedding:0.6b").chunk_text(body, source_id="s")
    assert len(chunks) >= 2, "need a multi-chunk body to prove per-chunk partition"

    # Tag map: a chunk whose token count exceeds arctic's num_ctx truncates its
    # arctic secondary; a smaller chunk does not. This mirrors the real
    # _bounded_for_model rule the service applies (char budget ≈ num_ctx).
    def tag_map(chunk_text: str) -> list[str]:
        # The stored chunk text is prefixed with the "[chunk N/M]\n\n" header at
        # the site AFTER this call, so here we see the raw chunk content.
        if len(chunk_text) > _ARCTIC_CHAR_BUDGET:
            return ["arctic2_embed"]
        return []

    coll = _FakeCollection()
    srv = _patch_server_for_dual_write(monkeypatch, tmp_path, coll, tag_map)
    result = _store(srv)
    assert result.get("success") is True

    # Every stored chunk row carries the property (additive, always present).
    stored = coll.objects
    assert stored, "chunks were written"
    for obj in stored:
        assert "secondary_truncated_slots" in obj.properties, (
            "every dual-write chunk row must carry the truncated tag property"
        )

    # Partition reconstructable from stored data alone.
    truncated_rows = [
        o for o in stored
        if "arctic2_embed" in o.properties.get("secondary_truncated_slots", [])
    ]
    full_rows = [
        o for o in stored
        if "arctic2_embed" not in o.properties.get("secondary_truncated_slots", [])
    ]
    assert truncated_rows, (
        "the large leading chunk's arctic vector must be tagged truncated in the DB"
    )
    assert full_rows, (
        "the small trailing chunk's arctic vector must be tagged full-fidelity"
    )
    # The partition matches the real chunk sizes (cross-check against the chunker).
    expected_truncated = sum(
        1 for c in chunks
        if len(c.content) > _ARCTIC_CHAR_BUDGET
    )
    assert len(truncated_rows) == expected_truncated, (
        "the stored truncated-vs-full partition must match the actual per-chunk "
        "arctic-overflow reality"
    )


def test_store_no_truncation_yields_empty_lists(monkeypatch, tmp_path):
    """When nothing overflows a secondary, every row's tag is an EMPTY list —
    a clean 'all full-fidelity' signal in the DB (no None / missing property)."""
    coll = _FakeCollection()
    srv = _patch_server_for_dual_write(
        monkeypatch, tmp_path, coll, tag_map=lambda _t: []
    )
    result = _store(srv)
    assert result.get("success") is True
    assert coll.objects
    for obj in coll.objects:
        assert obj.properties.get("secondary_truncated_slots") == [], (
            "no overflow → empty truncated list on every row"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
