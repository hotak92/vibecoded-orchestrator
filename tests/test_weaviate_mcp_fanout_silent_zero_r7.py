# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.76 (R7): close the hybrid_search / semantic_graph_search silent-zero hole.

Pre-fix, a per-collection GENERIC (non-schema, non-unreachable, non-auth)
failure was only `logger.warning`'d and dropped. If EVERY collection failed
with a generic error the tool returned a clean `success` payload with 0
results and NO failure indication — the exact shape behind the live "0 hits on
every query" report.

These tests drive `_hybrid_search_body` (and its semantic_graph counterpart)
with the network seams stubbed:

  * ACT   all-generic-fail  → raises loudly (not a clean 0-result success).
  * ACT   mixed (some ok, some fail) → success payload carries a `degraded`
          key naming the failed collections; results intact.
  * LEAVE  all-good → no `degraded` key.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp"))

import weaviate_mcp.server as srv  # noqa: E402


class _GenericBoom(Exception):
    """A generic (non-schema, non-connection, non-auth) query failure — the
    class the fan-out used to swallow."""


def _wire_common(monkeypatch, *, single_side_effect, rerank_nodes=None):
    """Stub the network/RL seams shared by both search paths.

    `single_side_effect(coll_name)` returns a per-collection dict OR raises.
    """
    monkeypatch.setattr(srv, "_assert_workspace_unchanged", lambda *_a, **_k: None)

    async def _fake_vector(_q):
        return ([0.1, 0.2, 0.3], "qwen3_embed")

    monkeypatch.setattr(srv, "_get_search_vector", _fake_vector)

    monkeypatch.setattr(
        srv, "_kg_collections_to_search",
        lambda *_a, **_k: ["A_KG", "B_KG"],
    )

    async def _fake_single(coll_name, *_a, **_k):
        return single_side_effect(coll_name)

    monkeypatch.setattr(srv, "_hybrid_search_single_collection", _fake_single)

    async def _fake_rerank(_task, _q, nodes, _limit, **_k):
        return rerank_nodes if rerank_nodes is not None else nodes

    monkeypatch.setattr(srv, "_rl_cache_and_rerank", _fake_rerank)
    monkeypatch.setattr(srv, "get_weaviate_client", lambda *_a, **_k: None)
    # RL enrich / dual-log seams — best-effort, keep them inert.
    monkeypatch.setattr(srv, "_rl_enrich_nodes_with_linked_embs", lambda *a, **k: None)

    async def _fake_dual(*_a, **_k):
        return None

    monkeypatch.setattr(srv, "_resolve_dual_rl_log_inputs", _fake_dual)


def _run(coro):
    return asyncio.run(coro)


def test_hybrid_all_generic_fail_raises_loudly(monkeypatch):
    def _boom(_coll):
        raise _GenericBoom("gRPC deadline exceeded on query")

    _wire_common(monkeypatch, single_side_effect=_boom)
    with pytest.raises(Exception) as ei:
        _run(srv._hybrid_search_body(
            "q", 5, "", [], None, "auto", False,
        ))
    # Must NOT be silently swallowed: the message names the failure.
    assert "every configured collection failed" in str(ei.value)
    assert "gRPC deadline" in str(ei.value)


def test_hybrid_mixed_success_carries_degraded_key(monkeypatch):
    def _side(coll):
        if coll == "A_KG":
            # One good result from A_KG.
            return {
                ("Node A", 0): {
                    "combined_score": 0.9, "title": "Node A",
                    "collection": "A_KG", "content": "x",
                }
            }
        raise _GenericBoom("boom on B_KG")

    _wire_common(monkeypatch, single_side_effect=_side)
    out = _run(srv._hybrid_search_body("q", 5, "", [], None, "auto", False))
    payload = json.loads(out) if isinstance(out, str) else out
    assert payload["success"] is True
    assert "degraded" in payload, payload
    failed = payload["degraded"]["failed_collections"]
    assert "B_KG" in failed.get("errors", {}), failed
    # Results from the healthy collection survive.
    assert payload["count"] >= 1


def test_hybrid_all_good_has_no_degraded_key(monkeypatch):
    def _side(coll):
        return {
            (f"Node {coll}", 0): {
                "combined_score": 0.8, "title": f"Node {coll}",
                "collection": coll, "content": "x",
            }
        }

    _wire_common(monkeypatch, single_side_effect=_side)
    out = _run(srv._hybrid_search_body("q", 5, "", [], None, "auto", False))
    payload = json.loads(out) if isinstance(out, str) else out
    assert payload["success"] is True
    assert "degraded" not in payload, payload
