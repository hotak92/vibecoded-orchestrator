# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 — oversized-query handling on the HOOK retrieval path (shared module).

The QUERY itself can exceed the embedding model's max chunk size. The HOOK path
(rl_kg_search.py) detects this, chunks the query via the shared model-aware
Chunker, retrieves per chunk, and combines:
  * KG: pool + dedup, rerank each node by MAX over (node_chunk × query_chunk),
    return top-N (reuses the existing _cosine scorer).
  * CodeGraph: deduplicated union, no rerank.
MCP calls do NOT do this (Weaviate handles oversize) — not tested here.
"""

from __future__ import annotations

import math

import pytest

from claude_mcp_servers.rl_client import query_chunking as qc


def _fake_cos(a, b):
    return sum(x * y for x, y in zip(a, b))


# ----------------------------------------------------------------------
# Detection + chunk-count arithmetic.
# ----------------------------------------------------------------------


class TestDetectionAndCounts:
    def test_kg_results_per_chunk_is_n_plus_1(self) -> None:
        assert qc.kg_results_per_chunk(3) == 4
        assert qc.kg_results_per_chunk(1) == 2
        assert qc.kg_results_per_chunk(0) == 2  # clamps N to >= 1

    def test_codegraph_results_per_chunk_is_ceil_n_over_q(self) -> None:
        assert qc.codegraph_results_per_chunk(3, 2) == math.ceil(3 / 2)  # 2
        assert qc.codegraph_results_per_chunk(5, 2) == 3
        assert qc.codegraph_results_per_chunk(4, 2) == 2
        assert qc.codegraph_results_per_chunk(1, 4) == 1  # never below 1

    def test_normal_size_query_not_oversized(self) -> None:
        # A short query is never oversize for a normal-context model.
        assert qc.is_oversized("short query", "qwen3-embedding:0.6b") is False

    def test_empty_query_not_oversized(self) -> None:
        assert qc.is_oversized("", "qwen3-embedding:0.6b") is False

    def test_oversized_query_detected(self, monkeypatch) -> None:
        # Force a tiny model max so a modest query trips the gate.
        monkeypatch.setattr(qc, "_model_max_tokens", lambda m: 5)

        class _TC:
            @staticmethod
            def count_tokens(text):
                return len(text.split())

        import claude_mcp_servers.weaviate_mcp.chunking as ch
        monkeypatch.setattr(ch, "TokenCounter", _TC)
        assert qc.is_oversized("one two three four five six seven", "m") is True
        assert qc.is_oversized("one two", "m") is False


# ----------------------------------------------------------------------
# chunk_query — uses the shared Chunker; degrades to whole-query on failure.
# ----------------------------------------------------------------------


class TestChunkQuery:
    def test_chunk_query_returns_list(self) -> None:
        chunks = qc.chunk_query("a moderately sized query string", "qwen3-embedding:0.6b")
        assert isinstance(chunks, list)
        assert all(isinstance(c, str) and c for c in chunks)
        assert len(chunks) >= 1

    def test_chunk_query_failure_degrades_to_whole_query(self, monkeypatch) -> None:
        import claude_mcp_servers.weaviate_mcp.chunking as ch

        class _BadChunker:
            @classmethod
            def for_model(cls, m):
                raise RuntimeError("chunker unavailable")

        monkeypatch.setattr(ch, "Chunker", _BadChunker)
        assert qc.chunk_query("whole query here", "m") == ["whole query here"]


# ----------------------------------------------------------------------
# combine_kg_results — pool + dedup + max-over-pairs + top-N.
# ----------------------------------------------------------------------


class TestCombineKG:
    def test_dedup_across_chunks_and_max_over_pairs(self) -> None:
        # A appears in both chunks; B only in chunk 1; C only in chunk 2.
        pooled = [
            [
                {"title": "A", "file_path": "a.md", "emb": [1.0, 0.0]},
                {"title": "B", "file_path": "b.md", "emb": [0.0, 1.0]},
            ],
            [
                {"title": "A", "file_path": "a.md", "emb": [1.0, 0.0]},
                {"title": "C", "file_path": "c.md", "emb": [0.6, 0.8]},
            ],
        ]
        # Two query chunks: one favors A-direction, one favors B-direction.
        q_embs = [[1.0, 0.0], [0.0, 1.0]]
        res = qc.combine_kg_results(pooled, q_embs, 3, cosine_fn=_fake_cos)
        titles = [r["title"] for r in res]
        # A (max 1.0), B (max 1.0), C (max 0.8) — all three, A/B tie at top.
        assert set(titles) == {"A", "B", "C"}
        # Dedup: A appears once.
        assert titles.count("A") == 1
        # max-over-pairs: C's score is max(0.6, 0.8)=0.8.
        c = next(r for r in res if r["title"] == "C")
        assert math.isclose(c["oversized_query_score"], 0.8, abs_tol=1e-9)

    def test_top_n_truncation(self) -> None:
        pooled = [[
            {"title": f"n{i}", "file_path": f"{i}.md", "emb": [float(i), 0.0]}
            for i in range(5)
        ]]
        res = qc.combine_kg_results(pooled, [[1.0, 0.0]], 2, cosine_fn=_fake_cos)
        assert len(res) == 2
        # Highest node-emb magnitude wins under the dot-product fake cosine.
        assert res[0]["title"] == "n4"

    def test_node_without_emb_falls_back_to_score_not_dropped(self) -> None:
        pooled = [[
            {"title": "HasEmb", "file_path": "h.md", "emb": [1.0, 0.0], "score": 0.1},
            {"title": "NoEmb", "file_path": "n.md", "score": 0.9},  # no emb
        ]]
        res = qc.combine_kg_results(pooled, [[1.0, 0.0]], 5, cosine_fn=_fake_cos)
        titles = {r["title"] for r in res}
        # NoEmb is NOT dropped — it falls back to its existing score (0.9).
        assert "NoEmb" in titles
        no_emb = next(r for r in res if r["title"] == "NoEmb")
        assert math.isclose(no_emb["oversized_query_score"], 0.9, abs_tol=1e-9)

    def test_empty_query_embs_uses_node_score(self) -> None:
        pooled = [[{"title": "A", "file_path": "a.md", "emb": [1.0], "score": 0.42}]]
        res = qc.combine_kg_results(pooled, [], 1, cosine_fn=_fake_cos)
        assert res[0]["oversized_query_score"] == pytest.approx(0.42)


# ----------------------------------------------------------------------
# combine_codegraph_results — dedup union, no rerank.
# ----------------------------------------------------------------------


class TestCombineCodegraph:
    def test_dedup_union_no_rerank(self) -> None:
        pooled = [
            [{"full_name": "mod.foo"}, {"full_name": "mod.bar"}],
            [{"full_name": "mod.foo"}, {"full_name": "mod.baz"}],
        ]
        res = qc.combine_codegraph_results(pooled)
        names = {r["full_name"] for r in res}
        assert names == {"mod.foo", "mod.bar", "mod.baz"}
        # foo deduplicated.
        assert sum(1 for r in res if r["full_name"] == "mod.foo") == 1

    def test_codegraph_keys_on_alternatives(self) -> None:
        pooled = [[
            {"endpoint": "/api/x"},
            {"path": "src/y.py"},
            {"title": "Z"},
        ]]
        res = qc.combine_codegraph_results(pooled)
        assert len(res) == 3
