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


# ----------------------------------------------------------------------
# OVER-COLLAPSE FIX (v0.2.70) — truncated display body must NOT collapse two
# distinct same-title nodes that share their first 300 chars but differ in the
# tail. _format_obj attaches a `content_sha` computed from the FULL body; the
# dedup key prefers it over re-hashing the truncated `content` field.
# ----------------------------------------------------------------------


# Mimic _format_obj's display truncation so the tests use the exact field shape
# that reaches _collapse_to_one_per_node at runtime.
def _truncate_display(body: str) -> str:
    return body[:300] + "..." if len(body) > 300 else body


class TestTruncatedPrefixOverCollapse:
    def test_same_title_shared_300_prefix_different_tail_BOTH_kept(self) -> None:
        # The real-world over-collapse scenario: two genuinely-distinct KG nodes
        # share a (common) title AND an identical first 300 chars, but their
        # bodies diverge AFTER char 300. At collapse time the dict carries only
        # the truncated `content[:300] + "..."`, so keying on that field would
        # wrongly merge them and silently drop one from Claude's context.
        shared_prefix = "X" * 300
        body_a = shared_prefix + " TAIL-ALPHA distinguishing content here"
        body_b = shared_prefix + " TAIL-BETA a completely different ending"
        # Same display body (both truncate to the same first-300 + "..."):
        assert _truncate_display(body_a) == _truncate_display(body_b)

        # Producer (_format_obj) attaches the FULL-body fingerprint.
        nodes = [
            {
                "title": "Architecture",
                "file_path": "a.md",
                "content": _truncate_display(body_a),
                "content_sha": cd.content_sha(body_a),
            },
            {
                "title": "Architecture",
                "file_path": "b.md",
                "content": _truncate_display(body_b),
                "content_sha": cd.content_sha(body_b),
            },
        ]
        out = cd.dedup_by_content_identity(nodes, kind="kg")
        # Distinct full bodies → BOTH kept (no over-collapse).
        assert len(out) == 2
        assert {n["file_path"] for n in out} == {"a.md", "b.md"}

    def test_same_title_identical_full_body_still_collapses(self) -> None:
        # The legit dedup target must still fire: same title + IDENTICAL full
        # body (e.g. one node living in project KG + shared KG) → collapse to 1.
        body = "Y" * 500
        nodes = [
            {
                "title": "Score-Driven Retrieval Tiers",
                "file_path": "project/k.md",
                "content": _truncate_display(body),
                "content_sha": cd.content_sha(body),
            },
            {
                "title": "Score-Driven Retrieval Tiers",
                "file_path": "shared/k.md",
                "content": _truncate_display(body),
                "content_sha": cd.content_sha(body),
            },
        ]
        out = cd.dedup_by_content_identity(nodes, kind="kg")
        assert len(out) == 1

    def test_key_prefers_precomputed_content_sha_over_truncated_field(self) -> None:
        # Directly assert the key uses the full-body fingerprint, not content.
        long_body_a = "Z" * 300 + "alpha"
        long_body_b = "Z" * 300 + "beta"
        node_a = {
            "title": "T",
            "content": _truncate_display(long_body_a),
            "content_sha": cd.content_sha(long_body_a),
        }
        node_b = {
            "title": "T",
            "content": _truncate_display(long_body_b),
            "content_sha": cd.content_sha(long_body_b),
        }
        # Truncated display fields are identical...
        assert node_a["content"] == node_b["content"]
        # ...but the keys differ because the precomputed sha is honoured.
        assert cd.content_identity_key(node_a, kind="kg") != cd.content_identity_key(
            node_b, kind="kg"
        )

    def test_fallback_to_field_hash_when_no_precomputed_sha(self) -> None:
        # Callers that build dicts WITHOUT _format_obj pass the real (untruncated)
        # body in `content` and no `content_sha`; the helper falls back to
        # hashing the field, preserving the legacy behaviour the other tests pin.
        nodes = [
            {"title": "T", "file_path": "a.md", "content": "full body one"},
            {"title": "T", "file_path": "b.md", "content": "full body two"},
        ]
        out = cd.dedup_by_content_identity(nodes, kind="kg")
        assert len(out) == 2


class TestServerCollapseTruncationGuard:
    def test_format_obj_attaches_full_body_content_sha(self) -> None:
        # _format_obj must expose `content_sha` derived from the UNTRUNCATED
        # body so the collapse content pass keys on the real body.
        from claude_mcp_servers.weaviate_mcp import server as srv

        long_body = "Q" * 800
        obj = _FakeObj({"title": "N", "content": long_body})
        formatted = srv._format_obj(obj, "SomeKG", distance=0.1)
        # Display content is truncated...
        assert formatted["content"].endswith("...")
        assert len(formatted["content"]) == 303  # 300 + "..."
        # ...but content_sha is the FULL-body fingerprint.
        assert formatted["content_sha"] == cd.content_sha(long_body)
        assert formatted["content_sha"] != cd.content_sha(formatted["content"])

    def test_collapse_keeps_distinct_tails_under_shared_prefix(self) -> None:
        # End-to-end through the MCP collapse: two distinct same-title nodes
        # with a shared 300-char prefix + different tails (as _format_obj would
        # produce them) → BOTH survive the content pass.
        from claude_mcp_servers.weaviate_mcp.server import _collapse_to_one_per_node

        prefix = "P" * 300
        body_a = prefix + " unique-tail-A"
        body_b = prefix + " unique-tail-B"
        results = [
            {
                "title": "Overview",
                "file_path": "a.md",
                "content": _truncate_display(body_a),
                "content_sha": cd.content_sha(body_a),
                "combined_score": 0.9,
                "chunk_number": 1,
            },
            {
                "title": "Overview",
                "file_path": "b.md",
                "content": _truncate_display(body_b),
                "content_sha": cd.content_sha(body_b),
                "combined_score": 0.8,
                "chunk_number": 1,
            },
        ]
        out = _collapse_to_one_per_node(results)
        # Distinct full bodies → both kept; the truncated `content` is identical
        # so a naive content-field hash would have wrongly merged them.
        assert {r["file_path"] for r in out} == {"a.md", "b.md"}


class _FakeObj:
    """Minimal stand-in for a Weaviate object for _format_obj unit tests."""

    class _Meta:
        def __init__(self, distance):
            self.distance = distance

    def __init__(self, properties: dict, distance: float | None = 0.1):
        self.properties = properties
        self.metadata = self._Meta(distance)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
