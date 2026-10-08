# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-C S2 — per-node ``cross_project_access`` is ENFORCED on reads.

The write side (launcher ``kg_set_node_access`` / ``kg_set_node_access_bulk``,
Rust ``commands/kg.rs``) PATCHes ``cross_project_access: text[]`` onto Weaviate
objects: ``["*"]`` = every project, ``[<launcher project id>…]`` = exactly
those, ``[]`` = private. Until v0.2.102 NOTHING read the property (the
v0.2.91 hooks-placebo class) — the editor's restrictions were saved and
ignored.

These tests pin the read side — the ONE filter builder
(``server._cross_project_access_allows`` / ``_filter_cross_project_access``)
plus its first fan-out site (``_hybrid_search_single_collection``) against a
fake Weaviate — with both back-compat arms (legacy rows without the property
stay unrestricted; the SELF collection is never filtered).

In-memory fakes only — no Weaviate, no Ollama, no hub.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import uuid as _uuid
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _server():
    return importlib.import_module("weaviate_mcp.server")


class _FakeObj:
    def __init__(self, properties: dict):
        self.uuid = str(_uuid.uuid4())
        self.properties = properties
        self.vector = {}
        self.metadata = SimpleNamespace(distance=0.2)


class _FakeResults:
    def __init__(self, objects):
        self.objects = list(objects)


class _FakeQuery:
    def __init__(self, objects):
        self._objects = objects

    def near_vector(self, **kwargs):  # noqa: ARG002
        return _FakeResults(self._objects)

    def bm25(self, query="", **kwargs):  # noqa: ARG002
        return _FakeResults([])  # empty keyword arm — the semantic arm decides


class _FakeCollection:
    def __init__(self, objects):
        self.query = _FakeQuery(objects)


_UNSET = object()


def _row(title: str, access=_UNSET) -> _FakeObj:
    props = {
        "title": title, "content": f"body of {title}",
        "file_path": f"knowledge/peer/{title}.md", "chunk_num": 1,
        "total_chunks": 1, "tags": [], "node_type": "concept",
    }
    if access is not _UNSET:
        props["cross_project_access"] = access
    return _FakeObj(props)


# ─── The verdict table (pure unit) ────────────────────────────────────────


def test_verdict_table():
    srv = _server()
    allows = srv._cross_project_access_allows
    # Legacy / absent → unrestricted (back-compat arm 1).
    assert allows({}, "me")
    assert allows({"other": 1}, "me")
    assert allows(None, "me")
    # "*" → unrestricted.
    assert allows({"cross_project_access": ["*"]}, "me")
    assert allows({"cross_project_access": ["you", "*"]}, "me")
    # A list names exactly its members.
    assert allows({"cross_project_access": ["me", "you"]}, "me")
    assert not allows({"cross_project_access": ["you"]}, "me")
    # [] = the writer's private mode.
    assert not allows({"cross_project_access": []}, "me")
    # Unresolvable requester + a real restriction → DENY (never guess onto
    # the list).
    assert not allows({"cross_project_access": ["me"]}, "")
    assert allows({"cross_project_access": []}, "") is False
    # A malformed value is still a restriction.
    assert not allows({"cross_project_access": "me"}, "me")


def test_self_collection_is_never_filtered():
    srv = _server()
    rows = [_row("Private", []), _row("OtherOnly", ["you"])]
    out = srv._filter_cross_project_access(rows, is_self=True)
    assert out == rows


# ─── The fan-out site: _hybrid_search_single_collection ──────────────────


def _run_hybrid(monkeypatch, coll_name, objects, *, own_id: str = "proj-self"):
    srv = _server()
    monkeypatch.setattr(srv, "KG_COLLECTION", "Alpha_KnowledgeGraph")
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(
        srv, "get_weaviate_client",
        lambda: SimpleNamespace(
            collections=SimpleNamespace(
                get=lambda name: _FakeCollection(objects),
            ),
        ),
    )
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False)

    async def _vec(_query):
        return [0.1, 0.2], "qwen3_embed"

    monkeypatch.setattr(srv, "_get_search_vector", _vec)
    monkeypatch.setattr(
        srv, "_stale_filter_for", lambda coll_name_, include_stale=False: None,
    )
    monkeypatch.setattr(
        srv, "_enrich_with_adjacent_chunks",
        lambda coll, formatted, name: formatted,
    )
    monkeypatch.setattr(
        srv, "_try_resolve_project_config",
        lambda: SimpleNamespace(project_id=own_id or None),
    )
    combined = asyncio.run(
        srv._hybrid_search_single_collection(
            coll_name, "query", fetch_limit=10,
            weaviate_filter=None, date_filter=None,
        )
    )
    return sorted(str(e["title"]) for e in combined.values())


def test_peer_rows_restricted_to_another_project_are_absent(monkeypatch):
    """The S2 red-proof: a peer row restricted to another project is
    filtered out of the fan-out (returned pre-fix); unrestricted peer rows,
    self-granted rows and legacy rows survive."""
    objects = [
        _row("OtherOnly", ["proj-other"]),   # restricted to another project
        _row("Granted", ["proj-self"]),      # restricted, but to THIS project
        _row("Shared", ["*"]),               # explicitly shared with all
        _row("Legacy"),                      # no property (back-compat arm 2)
        _row("Private", []),                 # the writer's private mode
    ]
    titles = _run_hybrid(
        monkeypatch, "Peer_KnowledgeGraph", objects, own_id="proj-self",
    )
    assert titles == ["Granted", "Legacy", "Shared"]


def test_self_rows_are_never_filtered(monkeypatch):
    """Leave-alone: the SELF collection keeps even private rows (its rows are
    this project's own data; the collection-level matrix governs peers)."""
    objects = [
        _row("OtherOnly", ["proj-other"]),
        _row("Private", []),
        _row("Plain"),
    ]
    titles = _run_hybrid(
        monkeypatch, "Alpha_KnowledgeGraph", objects, own_id="proj-self",
    )
    assert titles == ["OtherOnly", "Plain", "Private"]


def test_unresolvable_requester_denies_only_restricted_rows(monkeypatch):
    """Hub down (no project id): unrestricted and legacy peer rows still
    flow; rows carrying a restriction are denied — never guessed onto."""
    objects = [_row("Legacy"), _row("Shared", ["*"]), _row("Granted", ["proj-self"])]
    titles = _run_hybrid(
        monkeypatch, "Peer_KnowledgeGraph", objects, own_id="",
    )
    assert titles == ["Legacy", "Shared"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(importlib.import_module("pytest").main([__file__]))
