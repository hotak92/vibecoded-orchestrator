# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.81 (Lens-B): close the search_code_graph silent-zero hole.

Pre-fix, ``search_code_graph``'s per-collection ``except → logger.warning``
swallowed a missing/never-created collection and the aggregate returned
``{"success": True, "count": 0, "results": []}`` — indistinguishable from
"no semantic matches". That silence is what turned GAP-CG-1 (slug-form peer
entry → wrong collection prefix) and GAP-CG-3 (divergent fallback prefix)
into invisible failures.

These tests drive ``search_code_graph`` with the network / pipeline seams
stubbed:

  * ACT   all-collections-missing → LOUD error (success:False) naming the
          project + resolved prefix + missing collections + remediation.
  * LEAVE  some-exist (partial fan-out) → success:True + a
          ``degraded.schema_missing`` note listing only the missing peers;
          the self results are intact.
  * LEAVE  all-exist-but-empty → clean success:True count:0 with NO
          ``degraded`` note (genuinely no matches, not a routing break).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp"))

import weaviate_mcp.server as srv  # noqa: E402


class _ClassNotFound(Exception):
    """A schema-missing failure — Weaviate's "could not find class" shape."""

    def __str__(self) -> str:  # ensure the classifier's pattern matches
        return "could not find class MissingPeer_CodeFunction"


class _FakeObj:
    def __init__(self, full_name: str) -> None:
        self.properties = {
            "full_name": full_name,
            "name": full_name.split(".")[-1],
            "file_path": "x.py",
            "signature": "def x()",
            "chunk_num": 0,
            "total_chunks": 1,
        }

        class _Meta:
            distance = 0.1
            score = 0.9

        self.metadata = _Meta()
        self.vector = {"codesage_embed": [0.1, 0.2, 0.3]}


class _FakeQuery:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def near_vector(self, **_kwargs):
        return self._behaviour()


class _FakeCollection:
    def __init__(self, behaviour):
        self.query = _FakeQuery(behaviour)


class _FakeCollections:
    def __init__(self, per_collection):
        self._per_collection = per_collection

    def get(self, name):
        return _FakeCollection(self._per_collection(name))


class _FakeClient:
    def __init__(self, per_collection):
        self.collections = _FakeCollections(per_collection)


def _wire(monkeypatch, *, per_collection, effective_project="MyProj",
          access_list=""):
    """Stub the seams search_code_graph touches."""
    monkeypatch.setattr(srv, "_assert_workspace_unchanged", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "CODE_GRAPH_PROJECT", effective_project, raising=False)

    async def _fake_embed(_q):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(srv, "get_code_query_embedding", _fake_embed)
    monkeypatch.setattr(
        srv, "get_weaviate_client",
        lambda *_a, **_k: _FakeClient(per_collection),
    )
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False, raising=False)
    monkeypatch.setattr(
        srv, "_parse_csv_env",
        lambda name: (access_list.split(",") if (name == "VCT_CODE_GRAPH_ACCESS_LIST" and access_list) else []),
    )

    # The shared retrieval pipeline: identity passthrough (trim to limit).
    def _fake_pipeline(rows, *_a, **_k):
        return rows[: _k.get("limit", 8)] if isinstance(rows, list) else rows

    monkeypatch.setattr(srv, "run_code_retrieval_pipeline", _fake_pipeline, raising=False)


def _run(coro):
    return asyncio.run(coro)


def _call(**kwargs):
    return json.loads(_run(srv.search_code_graph(**kwargs)))


def test_all_collections_missing_returns_loud_error(monkeypatch):
    """ACT: every targeted collection is absent → LOUD success:False naming
    the project, prefix, missing collections, and remediation — NOT a silent
    count:0 success."""
    def _per_collection(_name):
        def _boom():
            raise _ClassNotFound()
        return _boom

    _wire(monkeypatch, per_collection=_per_collection, effective_project="MyProj")
    data = _call(query="auth middleware", scope="code", limit=5, project="MyProj")

    assert data["success"] is False, data
    assert data["count"] == 0
    # Names the project + remediation vocabulary.
    assert "MyProj" in data["error"]
    assert "code-graph-analyze" in data["error"]
    assert "NAME" in data["error"]  # slug-vs-prefix trap remediation
    assert data.get("collections_missing"), "must list the missing collections"


def test_partial_fanout_surfaces_degraded_note(monkeypatch):
    """LEAVE (self intact): self collections resolve, a peer collection is
    missing → success:True with a degraded.schema_missing note listing the
    missing peer, self results intact."""
    self_prefix = srv._code_sanitize_collection_prefix("MyProj")

    def _per_collection(name):
        if name.startswith(self_prefix + "_"):
            # Self collections resolve with one row.
            def _ok():
                class _Resp:
                    objects = [_FakeObj("mod.self_fn")]
                return _Resp()
            return _ok

        def _boom():
            raise _ClassNotFound()
        return _boom

    _wire(
        monkeypatch, per_collection=_per_collection,
        effective_project="MyProj", access_list="MissingPeer",
    )
    data = _call(query="auth", scope="code", limit=5, project="MyProj")

    assert data["success"] is True, data
    # Self results present.
    assert data["count"] >= 1
    # The missing peer is surfaced in the degraded note (not silently dropped).
    degraded = data.get("degraded", {}).get("failed_collections", {})
    missing = degraded.get("schema_missing", [])
    assert any("MissingPeer" in m for m in missing), (
        f"missing peer must appear in degraded.schema_missing; got {degraded}"
    )


def test_all_exist_but_empty_is_clean_success_no_degraded(monkeypatch):
    """LEAVE (genuinely empty): every collection EXISTS but returns 0 rows →
    clean success:True count:0 with NO degraded note. An empty-but-present
    index is 'no matches', not a routing break."""
    def _per_collection(_name):
        def _empty():
            class _Resp:
                objects = []
            return _Resp()
        return _empty

    _wire(monkeypatch, per_collection=_per_collection, effective_project="MyProj")
    data = _call(query="nonexistent concept", scope="code", limit=5, project="MyProj")

    assert data["success"] is True, data
    assert data["count"] == 0
    assert "degraded" not in data, (
        f"an all-present-but-empty result must NOT carry a degraded note: {data}"
    )
