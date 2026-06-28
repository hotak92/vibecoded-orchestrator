# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 — content-identity dedup on the retrieval/injection layer (concern-2).

The maintainer's bar: chunks with the SAME content (name + content-hash) must
not reach Claude's context twice — across query chunks, retrieval paths, or
cross-collection duplicates (same node in project KG + shared KG).

The OVER-COLLAPSE GUARD is equally load-bearing: two LEGITIMATELY-DISTINCT items
that merely share a body (two real files, or two distinct code entities) have
DIFFERENT names and must NOT be merged. These tests pin both directions.

ONE shared helper (``claude_mcp_servers.rl_client.content_dedup``) owns the
content-hash + content-identity dedup; the KG-pool path (``combine_kg_results``),
the codegraph union (``combine_codegraph_results``), and the MCP collapse
(``server._collapse_to_one_per_node``) all route through it.
"""

from __future__ import annotations

import pytest

from claude_mcp_servers.rl_client import content_dedup as cd
from claude_mcp_servers.rl_client import query_chunking as qc


def _fake_cos(a, b):
    return sum(x * y for x, y in zip(a, b))


# ----------------------------------------------------------------------
# content_sha — mirrors the seen-store sha1[:12] convention.
# ----------------------------------------------------------------------


class TestContentSha:
    def test_sha_matches_seen_store_convention(self) -> None:
        import hashlib

        body = "some node body text"
        expected = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
        assert cd.content_sha(body) == expected
        assert len(cd.content_sha(body)) == 12

    def test_empty_text_is_empty_sha(self) -> None:
        assert cd.content_sha("") == ""
        assert cd.content_sha(None) == ""

    def test_same_text_same_sha_distinct_text_distinct_sha(self) -> None:
        assert cd.content_sha("alpha") == cd.content_sha("alpha")
        assert cd.content_sha("alpha") != cd.content_sha("beta")


# ----------------------------------------------------------------------
# dedup_by_content_identity — the core collapse + over-collapse guard.
# ----------------------------------------------------------------------


class TestDedupByContentIdentity:
    def test_identical_name_and_content_collapse_to_one(self) -> None:
        # Same title + same body, different file_path (project KG + shared KG).
        nodes = [
            {"title": "Auth", "file_path": "knowledge/auth.md", "content": "BODY"},
            {"title": "Auth", "file_path": "shared/auth.md", "content": "BODY"},
        ]
        out = cd.dedup_by_content_identity(nodes, kind="kg")
        assert len(out) == 1
        # First-seen representative kept.
        assert out[0]["file_path"] == "knowledge/auth.md"

    def test_distinct_name_same_content_BOTH_kept(self) -> None:
        # OVER-COLLAPSE GUARD: two distinct-title nodes with the SAME body are
        # distinct logical nodes — must NOT merge.
        nodes = [
            {"title": "NodeA", "file_path": "a.md", "content": "IDENTICAL"},
            {"title": "NodeB", "file_path": "b.md", "content": "IDENTICAL"},
        ]
        out = cd.dedup_by_content_identity(nodes, kind="kg")
        assert len(out) == 2
        assert {n["title"] for n in out} == {"NodeA", "NodeB"}

    def test_same_name_distinct_content_BOTH_kept(self) -> None:
        # Same title, genuinely different bodies → distinct (kept).
        nodes = [
            {"title": "Spec", "file_path": "v1.md", "content": "version one"},
            {"title": "Spec", "file_path": "v2.md", "content": "version two"},
        ]
        out = cd.dedup_by_content_identity(nodes, kind="kg")
        assert len(out) == 2

    def test_content_less_entries_keyed_by_identity_not_dropped(self) -> None:
        # No content on either → identity fallback; distinct file_paths kept.
        nodes = [
            {"title": "X", "file_path": "x1.md"},
            {"title": "X", "file_path": "x2.md"},
        ]
        out = cd.dedup_by_content_identity(nodes, kind="kg")
        assert len(out) == 2

    def test_non_dict_passthrough(self) -> None:
        out = cd.dedup_by_content_identity([{"title": "A", "content": "b"}, "stray"], kind="kg")
        assert "stray" in out

    def test_code_identical_name_and_body_collapse(self) -> None:
        nodes = [
            {"full_name": "mod.foo", "function_body": "return 1"},
            {"full_name": "mod.foo", "function_body": "return 1"},
        ]
        out = cd.dedup_by_content_identity(nodes, kind="code")
        assert len(out) == 1

    def test_code_distinct_names_same_body_BOTH_kept(self) -> None:
        # OVER-COLLAPSE GUARD for code: two DISTINCT entities with the same
        # one-line body are distinct funcs — must NOT merge.
        nodes = [
            {"full_name": "mod.foo", "function_body": "pass"},
            {"full_name": "mod.bar", "function_body": "pass"},
        ]
        out = cd.dedup_by_content_identity(nodes, kind="code")
        assert len(out) == 2
        assert {n["full_name"] for n in out} == {"mod.foo", "mod.bar"}


# ----------------------------------------------------------------------
# combine_kg_results — content-dedup is PRE-rerank (refinement #1).
# ----------------------------------------------------------------------


class TestCombineKGContentDedup:
    def test_cross_collection_duplicate_collapsed_before_rerank(self) -> None:
        # Same title + body under two file_paths (project + shared KG). Identity
        # dedup keeps both (file_path differs); content-dedup must collapse to 1.
        pooled = [[
            {"title": "Dup", "file_path": "p.md", "content": "SAME", "emb": [1.0, 0.0]},
            {"title": "Dup", "file_path": "s.md", "content": "SAME", "emb": [1.0, 0.0]},
            {"title": "Other", "file_path": "o.md", "content": "DIFF", "emb": [0.0, 1.0]},
        ]]
        res = qc.combine_kg_results(pooled, [[1.0, 0.0]], 5, cosine_fn=_fake_cos)
        titles = [r["title"] for r in res]
        assert titles.count("Dup") == 1
        assert "Other" in titles

    def test_distinct_titles_same_body_not_over_collapsed(self) -> None:
        pooled = [[
            {"title": "A", "file_path": "a.md", "content": "SHARED", "emb": [1.0, 0.0]},
            {"title": "B", "file_path": "b.md", "content": "SHARED", "emb": [1.0, 0.0]},
        ]]
        res = qc.combine_kg_results(pooled, [[1.0, 0.0]], 5, cosine_fn=_fake_cos)
        assert {r["title"] for r in res} == {"A", "B"}

    def test_content_dedup_runs_before_rerank_no_double_cosine(self) -> None:
        # Instrument the cosine to count calls; a collapsed duplicate must NOT be
        # scored twice (proves dedup precedes the rerank).
        calls = {"n": 0}

        def _counting_cos(a, b):
            calls["n"] += 1
            return _fake_cos(a, b)

        pooled = [[
            {"title": "Dup", "file_path": "p.md", "content": "SAME", "emb": [1.0, 0.0]},
            {"title": "Dup", "file_path": "s.md", "content": "SAME", "emb": [1.0, 0.0]},
        ]]
        qc.combine_kg_results(pooled, [[1.0, 0.0]], 5, cosine_fn=_counting_cos)
        # One surviving node × one query chunk → exactly one cosine call.
        assert calls["n"] == 1


# ----------------------------------------------------------------------
# combine_codegraph_results — identity union + content-identity.
# ----------------------------------------------------------------------


class TestCombineCodegraphContentDedup:
    def test_same_entity_body_collapsed(self) -> None:
        pooled = [
            [{"full_name": "mod.foo", "function_body": "return 1"}],
            [{"full_name": "mod.foo", "function_body": "return 1"}],
        ]
        res = qc.combine_codegraph_results(pooled)
        assert sum(1 for r in res if r["full_name"] == "mod.foo") == 1

    def test_distinct_entities_same_body_both_kept(self) -> None:
        pooled = [[
            {"full_name": "mod.foo", "function_body": "pass"},
            {"full_name": "mod.bar", "function_body": "pass"},
        ]]
        res = qc.combine_codegraph_results(pooled)
        assert {r["full_name"] for r in res} == {"mod.foo", "mod.bar"}

    def test_body_less_entities_dedup_by_full_name(self) -> None:
        # No bodies → identity fallback on full_name (legacy behaviour preserved).
        pooled = [
            [{"full_name": "mod.foo"}, {"full_name": "mod.bar"}],
            [{"full_name": "mod.foo"}, {"full_name": "mod.baz"}],
        ]
        res = qc.combine_codegraph_results(pooled)
        assert {r["full_name"] for r in res} == {"mod.foo", "mod.bar", "mod.baz"}


# ----------------------------------------------------------------------
# server._collapse_to_one_per_node — content pass collapses cross-collection.
# ----------------------------------------------------------------------


class TestServerCollapseContentPass:
    def test_cross_collection_same_node_collapsed(self) -> None:
        from claude_mcp_servers.weaviate_mcp.server import _collapse_to_one_per_node

        results = [
            {"title": "Node", "file_path": "k.md", "content": "BODY",
             "combined_score": 0.9, "chunk_number": 1},
            {"title": "Node", "file_path": "shared/k.md", "content": "BODY",
             "combined_score": 0.7, "chunk_number": 1},
        ]
        out = _collapse_to_one_per_node(results)
        assert len(out) == 1
        # Higher-scoring representative survives (score-sorted before collapse).
        assert out[0]["combined_score"] == 0.9

    def test_distinct_titles_same_body_kept(self) -> None:
        from claude_mcp_servers.weaviate_mcp.server import _collapse_to_one_per_node

        results = [
            {"title": "A", "file_path": "a.md", "content": "SAME", "combined_score": 0.9},
            {"title": "B", "file_path": "b.md", "content": "SAME", "combined_score": 0.8},
        ]
        out = _collapse_to_one_per_node(results)
        assert {r["title"] for r in out} == {"A", "B"}

    def test_multichunk_same_node_still_collapses_and_counts(self) -> None:
        # Two chunks of ONE node (same file_path+title) → existing identity pass
        # collapses to one with chunks_matched=2; content pass is a no-op here.
        from claude_mcp_servers.weaviate_mcp.server import _collapse_to_one_per_node

        results = [
            {"title": "N", "file_path": "n.md", "content": "c1",
             "combined_score": 0.5, "chunk_number": 1},
            {"title": "N", "file_path": "n.md", "content": "c2",
             "combined_score": 0.8, "chunk_number": 2},
        ]
        out = _collapse_to_one_per_node(results)
        assert len(out) == 1
        assert out[0]["chunks_matched"] == 2
        assert out[0]["combined_score"] == 0.8


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
