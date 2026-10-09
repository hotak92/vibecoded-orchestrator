# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-G item 4 — ``detail=auto`` never labels a preview "full".

Dogfood finding: single-chunk KG nodes came back as ``_format_obj``'s
300-char display preview (ending "...") while labelled ``tier: full``,
``coverage: complete`` and "Full node provided … No further Read … needed",
so agents worked from a preview. Now the renderer returns the FULL row when
it can fetch it, and otherwise labels the preview ``partial`` with a hint
naming the file. The code-graph renderer gets the same honesty for a
``full``-tier window smaller than the entity.

In-memory fakes only.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _srv():
    return importlib.import_module("weaviate_mcp.server")


LONG = "x" * 1000
FP = "knowledge/concepts/long-node.md"


def _obj(content):
    return SimpleNamespace(
        properties={"title": "Long Node", "content": content, "file_path": FP,
                    "chunk_num": 1, "total_chunks": 1, "node_type": "concept"},
        metadata=SimpleNamespace(distance=0.1),
    )


def test_format_obj_flags_its_truncated_preview():
    srv = _srv()
    assert srv._format_obj(_obj(LONG), "C")["content_truncated"] is True
    assert srv._format_obj(_obj("short"), "C")["content_truncated"] is False


def test_truncated_single_chunk_without_fetch_is_labelled_partial():
    """Red pre-fix: labelled complete + 'No further Read'."""
    srv = _srv()
    result = srv._format_obj(_obj(LONG), "C")
    for tier in ("full", "three_chunks", "single_chunk"):
        out = srv._format_result_by_tier(result, tier, sidecar_db={}, coll=None)
        assert out["coverage"] == "partial", (tier, out)
        assert FP in out["retrieval_hint"] and "No further Read" not in out["retrieval_hint"]


def test_whole_single_chunk_node_stays_complete():
    srv = _srv()
    result = srv._format_obj(_obj("short body"), "C")
    out = srv._format_result_by_tier(result, "full", sidecar_db={}, coll=None)
    assert out["coverage"] == "complete" and out["content"] == "short body"


def test_fetched_single_chunk_row_returns_the_full_body(monkeypatch):
    srv = _srv()
    monkeypatch.setattr(srv, "_fetch_node_chunks", lambda *a, **k: [(1, LONG)])
    result = srv._format_obj(_obj(LONG), "C")
    out = srv._format_result_by_tier(result, "full", sidecar_db={}, coll=object())
    assert out["content"] == LONG
    assert out["coverage"] == "complete"


def test_hybrid_merge_keeps_the_preview_flag(monkeypatch):
    import asyncio

    srv = _srv()

    class _Q:
        def near_vector(self, **kw):
            return SimpleNamespace(objects=[_obj(LONG)])

        def bm25(self, **kw):
            return SimpleNamespace(objects=[])

    monkeypatch.setattr(srv, "get_weaviate_client", lambda: SimpleNamespace(
        collections=SimpleNamespace(get=lambda n: SimpleNamespace(query=_Q()))))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False)

    async def _vec(_q, task=None):
        return [0.1], "qwen3_embed"

    monkeypatch.setattr(srv, "_get_search_vector", _vec)
    monkeypatch.setattr(srv, "_stale_filter_for", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_enrich_with_adjacent_chunks", lambda c, f, n: f)
    monkeypatch.setattr(srv, "KG_COLLECTION", "C")
    combined = asyncio.run(srv._hybrid_search_single_collection(
        "C", "q", fetch_limit=5, weaviate_filter=None, date_filter=None))
    entry = next(iter(combined.values()))
    assert entry["content_truncated"] is True


# ─── code graph ──────────────────────────────────────────────────────────


def _code_props(total):
    return {"full_name": "m.f", "file_path": "m.py", "function_body": "body",
            "chunk_num": 1, "total_chunks": total, "signature": "def f()"}


def test_code_full_tier_partial_window_says_so():
    srv = _srv()

    def fetch(full_name, hit, total, window, fp):
        return [{"function_body": f"c{i}", "chunk_num": i} for i in range(1, window + 1)]

    out = srv._format_code_result_by_tier(_code_props(12), "CodeFunction", "full",
                                          chunk_fetcher=fetch)
    assert out["chunks_shown"] == 7
    assert "7 of 12" in out.get("note", ""), out


def test_code_full_tier_whole_entity_has_no_partial_note():
    srv = _srv()

    def fetch(full_name, hit, total, window, fp):
        return [{"function_body": f"c{i}", "chunk_num": i} for i in range(1, total + 1)]

    out = srv._format_code_result_by_tier(_code_props(3), "CodeFunction", "full",
                                          chunk_fetcher=fetch)
    assert out["chunks_shown"] == 3 and "note" not in out


def test_keyless_fetched_single_chunk_row_is_labelled_partial(monkeypatch):
    """W4-G review F3: a fetched row without a node key (or with more than one
    matching row) is not certified complete, and says so explicitly."""
    srv = _srv()
    monkeypatch.setattr(srv, "_fetch_node_chunks", lambda *a, **k: [(1, LONG)])
    result = dict(srv._format_obj(_obj(LONG), "C"), file_path="")
    out = srv._format_result_by_tier(result, "full", sidecar_db={}, coll=object())
    assert out["content"] == LONG
    assert out["coverage"] == "partial"
    assert "No further Read" not in out["retrieval_hint"]

    monkeypatch.setattr(srv, "_fetch_node_chunks", lambda *a, **k: [(1, "a"), (1, "b")])
    out = srv._format_result_by_tier(srv._format_obj(_obj(LONG), "C"), "full",
                                     sidecar_db={}, coll=object())
    assert out["coverage"] == "partial" and FP in out["retrieval_hint"]
