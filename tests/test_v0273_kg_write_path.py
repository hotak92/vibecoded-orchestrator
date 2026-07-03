# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 D-1 / D-2 — store_knowledge_node write-path data-loss fixes.

D-1: the MCP write path deleted existing rows by TITLE only. Title is NOT
unique (v0.2.70 established this for sync_knowledge_graph.py and fixed it
there via ``_delete_node_by_file_path``); the MCP tool was never given the
same fix. Upserting node A silently deleted the rows of a same-titled node B
at a different file_path. Fix: scope the delete to ``title AND file_path``.

D-2: delete-before-embed was non-atomic. Old rows were deleted, THEN
embeddings were fetched; an embed failure (Ollama down — a routine condition)
returned ``success: false`` with the pre-existing rows already gone. Fix:
embed + insert the new rows FIRST, delete the old rows LAST, so an embed
failure leaves the previous version of the node intact in Weaviate.

The tests drive the real ``store_knowledge_node`` body against a fake
Weaviate collection whose ``fetch_objects`` evaluates an injected fake
``Filter`` (predicate-based), so the assertions exercise actual filter
semantics rather than string-matching the source.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import uuid as _uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _server():
    return importlib.import_module("weaviate_mcp.server")


def _unwrap(tool):
    """Unwrap an @mcp.tool()-decorated function to its plain callable."""
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


# ---------------------------------------------------------------------------
# Fake Filter — predicate-based stand-in for weaviate.classes.query.Filter
# ---------------------------------------------------------------------------


class _FakePredicate:
    def __init__(self, fn):
        self._fn = fn

    def matches(self, props: dict) -> bool:
        return bool(self._fn(props))

    def __and__(self, other: "_FakePredicate") -> "_FakePredicate":
        return _FakePredicate(lambda p: self.matches(p) and other.matches(p))


class _FakeByProperty:
    def __init__(self, name: str):
        self._name = name

    def equal(self, value) -> _FakePredicate:
        return _FakePredicate(lambda p, n=self._name, v=value: p.get(n) == v)


class _FakeFilter:
    @staticmethod
    def by_property(name: str) -> _FakeByProperty:
        return _FakeByProperty(name)


# ---------------------------------------------------------------------------
# Fake Weaviate collection / client
# ---------------------------------------------------------------------------


class _FakeObj:
    def __init__(self, properties: dict):
        self.uuid = str(_uuid.uuid4())
        self.properties = properties


class _FakeQuery:
    def __init__(self, coll: "_FakeCollection"):
        self._coll = coll

    def fetch_objects(self, filters=None, limit=100):
        objs = [
            o for o in self._coll.objects
            if filters is None or filters.matches(o.properties)
        ]

        class _Resp:
            pass

        resp = _Resp()
        resp.objects = objs[:limit]
        return resp


class _FakeData:
    def __init__(self, coll: "_FakeCollection"):
        self._coll = coll

    def insert(self, properties=None, vector=None):
        self._coll.objects.append(_FakeObj(dict(properties or {})))
        self._coll.event_log.append(("insert", (properties or {}).get("title", "")))

    def delete_by_id(self, uid):
        self._coll.objects = [o for o in self._coll.objects if o.uuid != uid]
        self._coll.event_log.append(("delete", uid))


class _FakeCollection:
    def __init__(self):
        self.objects: list[_FakeObj] = []
        self.event_log: list[tuple] = []
        self.query = _FakeQuery(self)
        self.data = _FakeData(self)


class _FakeCollections:
    def __init__(self, coll: _FakeCollection):
        self._coll = coll

    def get(self, name: str) -> _FakeCollection:
        return self._coll


class _FakeClient:
    def __init__(self, coll: _FakeCollection):
        self.collections = _FakeCollections(coll)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _patch_server_for_store(monkeypatch, tmp_path, coll: _FakeCollection,
                            *, embed_raises: bool = False):
    """Patch the server module so store_knowledge_node runs against the fake
    collection with a deterministic single-chunk embed path."""
    srv = _server()

    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _FakeClient(coll))
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False)
    # Keep the access-matrix gate quiet (no VCT_PROJECT_ID → silent-allow
    # emits a metric + deferral; stub those side-effect writers out).
    monkeypatch.setattr(srv, "_emit_gate_skipped_metric", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", lambda *a, **k: None)
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)

    async def _count_tokens(_content):
        return 10  # force the single-chunk path

    monkeypatch.setattr(srv, "count_tokens_async", _count_tokens)

    async def _embed(_text):
        if embed_raises:
            raise RuntimeError("simulated embed outage (Ollama down)")
        return [0.1] * 8

    monkeypatch.setattr(srv, "get_embedding", _embed)
    return srv


def _store(srv, **kwargs) -> dict:
    fn = _unwrap(srv.store_knowledge_node)
    defaults = dict(
        title="Sample Title",
        content="Sample body content.",
        node_type="concept",
        tags=["sample"],
        links=[],
        file_path="knowledge/concepts/sample_a.md",
        scope="project",
    )
    defaults.update(kwargs)
    raw = asyncio.run(fn(**defaults))
    return json.loads(raw)


# ---------------------------------------------------------------------------
# D-1 — delete scoped to title AND file_path
# ---------------------------------------------------------------------------


def test_d1_same_title_other_file_survives_upsert(monkeypatch, tmp_path):
    """Two nodes share a title at different file_paths; upserting one must
    NOT delete the other's rows (the v0.2.70 real-world shape)."""
    coll = _FakeCollection()
    coll.objects.append(_FakeObj({
        "title": "Sample Title", "file_path": "knowledge/concepts/sample_a.md",
    }))
    other = _FakeObj({
        "title": "Sample Title", "file_path": "knowledge/archive/sample_b.md",
    })
    coll.objects.append(other)

    srv = _patch_server_for_store(monkeypatch, tmp_path, coll)
    result = _store(srv, file_path="knowledge/concepts/sample_a.md")

    assert result.get("success") is True
    surviving = {o.properties.get("file_path") for o in coll.objects}
    # The OTHER node's row must survive.
    assert "knowledge/archive/sample_b.md" in surviving
    # The upserted node's row was replaced (old deleted, new inserted).
    deleted = [uid for ev, uid in coll.event_log if ev == "delete"]
    assert other.uuid not in deleted
    assert len(deleted) == 1


def test_d1_old_rows_of_same_node_are_replaced(monkeypatch, tmp_path):
    """The upsert still replaces the node's OWN previous rows (no
    accumulation of duplicates for the same (title, file_path))."""
    coll = _FakeCollection()
    coll.objects.append(_FakeObj({
        "title": "Sample Title", "file_path": "knowledge/concepts/sample_a.md",
    }))

    srv = _patch_server_for_store(monkeypatch, tmp_path, coll)
    result = _store(srv, file_path="knowledge/concepts/sample_a.md")

    assert result.get("success") is True
    same = [
        o for o in coll.objects
        if o.properties.get("file_path") == "knowledge/concepts/sample_a.md"
    ]
    assert len(same) == 1  # exactly the fresh row


# ---------------------------------------------------------------------------
# D-2 — embed+insert first, delete last (embed failure preserves old rows)
# ---------------------------------------------------------------------------


def test_d2_embed_failure_leaves_old_row_intact(monkeypatch, tmp_path):
    """An embedding outage mid-upsert must NOT destroy the existing node:
    the tool errors AND the previous version stays searchable."""
    coll = _FakeCollection()
    old = _FakeObj({
        "title": "Sample Title", "file_path": "knowledge/concepts/sample_a.md",
    })
    coll.objects.append(old)

    srv = _patch_server_for_store(monkeypatch, tmp_path, coll, embed_raises=True)
    result = _store(srv, file_path="knowledge/concepts/sample_a.md")

    assert result.get("success") is False
    # Old row intact — no delete ever ran.
    assert any(o.uuid == old.uuid for o in coll.objects)
    assert not [ev for ev in coll.event_log if ev[0] == "delete"]


def test_d2_success_path_inserts_before_deleting(monkeypatch, tmp_path):
    """On success the new rows are inserted BEFORE the old rows are deleted
    (the non-atomic window closes on the insert side, not the delete side)."""
    coll = _FakeCollection()
    coll.objects.append(_FakeObj({
        "title": "Sample Title", "file_path": "knowledge/concepts/sample_a.md",
    }))

    srv = _patch_server_for_store(monkeypatch, tmp_path, coll)
    result = _store(srv, file_path="knowledge/concepts/sample_a.md")

    assert result.get("success") is True
    kinds = [ev[0] for ev in coll.event_log]
    assert "insert" in kinds and "delete" in kinds
    assert kinds.index("insert") < kinds.index("delete")
