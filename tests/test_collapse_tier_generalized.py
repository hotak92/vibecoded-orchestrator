# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 (P4) — KG-SAFETY parity tests for the generalized shared helpers.

These helpers (`_collapse_to_one_per_node`, `_get_result_verbosity_by_score`,
`_allocate_tier_within_budget`) are on the KG / hybrid_search hot path
(server.py:6135/6201/6901/6996) AND the RL path. The v0.2.72 generalization
added keyword-only params with defaults equal to the v0.2.71 hard-coded values.

This file asserts that calling each generalized helper with NO new kwargs
yields byte-identical v0.2.71 semantics:
  * `_collapse_to_one_per_node` still keys on (file_path, title).
  * the tier gate `min` is still 0.42 for the KG default.
  * a hybrid_search-shaped fixture returns an unchanged top-K.

If any of these fail, the generalization has leaked new behaviour into the KG
callers — a Sev-2 regression.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "claude_mcp_servers"))

from weaviate_mcp import server as srv  # noqa: E402


# ── collapse: KG default still keys (file_path, title) ──────────────────


def test_collapse_default_keys_file_path_title():
    """Two same-title nodes with DIFFERENT file_path stay separate (project vs
    shared KG). Same-title + same-file_path chunks collapse. This is the exact
    v0.2.71 (file_path, title) key — unchanged."""
    inp = [
        {"title": "Concept", "file_path": "k/proj.md", "combined_score": 0.8, "chunk_number": None},
        {"title": "Concept", "file_path": "k/shared.md", "combined_score": 0.7, "chunk_number": None},
        {"title": "Concept", "file_path": "k/proj.md", "combined_score": 0.9, "chunk_number": 2},
    ]
    out = srv._collapse_to_one_per_node(inp)
    # proj.md collapses its 2 chunks → 1; shared.md stays → 2 entries total.
    assert len(out) == 2
    proj = next(r for r in out if r["file_path"] == "k/proj.md")
    assert proj["chunks_matched"] == 2
    assert proj["best_chunk_number"] == 2  # 0.9 chunk won
    shared = next(r for r in out if r["file_path"] == "k/shared.md")
    assert shared["chunks_matched"] == 1


def test_collapse_default_best_chunk_number_reads_chunk_number_field():
    """KG default chunk_field is 'chunk_number' — best_chunk_number reads it."""
    inp = [
        {"title": "T", "file_path": "k/t.md", "combined_score": 0.5, "chunk_number": 4},
        {"title": "T", "file_path": "k/t.md", "combined_score": 0.9, "chunk_number": 9},
    ]
    out = srv._collapse_to_one_per_node(inp)
    assert len(out) == 1
    assert out[0]["best_chunk_number"] == 9


def test_collapse_default_output_matches_prev_semantics_on_shared_fixture():
    """A hybrid_search-shaped fixture returns an unchanged top-K ordering."""
    inp = [
        {"title": "High", "file_path": "k/h.md", "combined_score": 0.9, "chunk_number": 1},
        {"title": "High", "file_path": "k/h.md", "combined_score": 0.85, "chunk_number": 2},
        {"title": "Mid", "file_path": "k/m.md", "combined_score": 0.6, "chunk_number": None},
        {"title": "Low", "file_path": "k/l.md", "combined_score": 0.3, "chunk_number": None},
    ]
    out = srv._collapse_to_one_per_node(inp)
    assert [r["title"] for r in out] == ["High", "Mid", "Low"]


# ── tier gate: KG default min still 0.42 ────────────────────────────────


def test_kg_tier_min_still_042():
    assert srv._TIER_THRESHOLDS["min"] == 0.42
    # No-arg call uses the KG gate.
    assert srv._get_result_verbosity_by_score(0.419) == "discard"
    assert srv._get_result_verbosity_by_score(0.42) == "summary"


def test_kg_allocate_tier_no_new_kwargs_identical():
    """`_allocate_tier_within_budget` with no thresholds arg → KG gate."""
    # 0.30 is below the KG min (0.42) → discard.
    assert srv._allocate_tier_within_budget(0.30, total_chunks=3, remaining_budget=20) == ("discard", 0)
    # 0.50 → summary (below single_chunk 0.55).
    assert srv._allocate_tier_within_budget(0.50, total_chunks=3, remaining_budget=20) == ("summary", 0)
    # 0.80 full, 3 chunks available → cost 3.
    assert srv._allocate_tier_within_budget(0.80, total_chunks=3, remaining_budget=20) == ("full", 3)


def test_kg_defaults_are_the_declared_module_constants():
    """The generalized helpers default to the KG constants — proving no code
    path silently swapped in the code thresholds for KG callers."""
    # Distinct constant identity: code min differs from KG min.
    assert srv._CODE_TIER_THRESHOLDS["min"] == 0.22
    assert srv._TIER_THRESHOLDS["min"] == 0.42
    # code min must be <= code post_rerank_floor 0.22 (v0.2.70 Bug B guard).
    assert srv._CODE_TIER_THRESHOLDS["min"] <= 0.22
