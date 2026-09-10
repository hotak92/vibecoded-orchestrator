# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2f (v0.2.76): monolith ratchet on templates/scripts/analyze_code_graph.py.

The analyzer is the code-graph mega-file (~11k lines; the modularity rule in
CLAUDE.md caps additions at ~50 contiguous lines before extraction is
required). Every feature that lands INSIDE it — rather than in a ``vco_lib``
module the analyzer imports — makes the next audit/refactor more expensive;
the file grew 8,836 → 10,538 → 11,055 across the releases that nominally
"split" it (CG-1), which is exactly what P2f reverses.

This is a RATCHET test: the pinned ceiling may only ever be updated DOWNWARD.
If it fails you added bulk to the analyzer — put the new logic in a ``vco_lib``
module (per-language extractor / the CodeEntity IR / a shared helper) that the
analyzer imports, not inline. P2f stage 1 (the CodeEntity IR + store_entity
wiring) took the FIRST bite; later stages (per-language extractor modules) will
lower the pin much further — re-pin DOWNWARD each time.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

# ── Pinned ceiling — update only DOWNWARD ──────────────────────────────────
#
# Measured 2026-07-09, P2f stage 2 in flight (per-language extractor moves
# into vco_lib/codegraph_lang/ — re-pinned DOWNWARD at every move commit so
# the pin tracks the shrink; stage-1 pin was 11,010, v0.2.76 base was 11,055).
#
# P2f stage 3 (v0.2.77 Part 6): the pure-producer split added the
# WRITE/CACHE LIFECYCLE owner ``write_file_extraction`` (a genuine analyzer-
# resident consolidation — it owns the sinks) and REMOVED the python
# entity-building (``_extract_class`` / ``_extract_function`` bodies moved to
# vco_lib/codegraph_lang/python.py as pure builders; the analyzer keeps only
# thin write+cache shims). Net after the full Part: 7335 -> 7363 (the writer
# outweighs the python removal by ~28 lines — the ONE small justified net
# increase in this ratchet's history, for a consolidation that makes the write
# lifecycle single-homed and reviewable).
#
# WP-C (v0.2.91): the per-file entity reconcile landed as a vco_lib engine
# (``codegraph_resync.reconcile_walked_file_rows``) with only the scope
# bookkeeping + one call site left analyzer-side, and the ONE file-row delete
# primitive (``_delete_file_rows_exact``, ~105 lines incl. its banner) MOVED to
# the same vco_lib module — the engine needs the same deleter, so one home. Net
# 7363 -> 7361. Re-pinned DOWNWARD to the measured value.
#
# v0.2.91 wave-3 (MAJOR-1): the analyzer needed a THIRD ledger operation — the
# NARROW paired clear its two backend skip paths never had (both `return 0`
# after re-emitting, so no retry dispatcher can infer success from the exit
# code). Rather than grow the monolith, the whole deferral family moved to the
# new `vco_lib/codegraph_deferrals.py` (both emitters + the clear); the
# analyzer keeps three thin wrappers whose only analyzer-side knowledge is how
# to read `code_vector_slot` / `code_model_id` off the EmbeddingService. Net
# 7361 -> 7314 INCLUDING the new operation. Re-pinned DOWNWARD to the measured
# value.
#
# v0.2.92: the cross-reference family moved to `vco_lib/codegraph_references.py`
# — the pure edge RESOLUTION (short-name / base-class / import-name -> target
# id), the add-if-absent WRITE, and the beacon READ that
# `create_cross_references` needs before it can decide what is missing. The
# analyzer keeps the loops that own the caches and the collections. Net
# 7314 -> 7292. Re-pinned DOWNWARD to the measured value, per this ratchet's
# own rule that an extraction must tighten the pin (the extraction landed
# without the re-pin, leaving 22 lines of stale slack).
#
# v0.2.92 code-graph write-path (the two reported-but-untaken defects). Both
# fixes ADD analyzer lines — the Defect-A chunk-plan hoist + seam and the
# Defect-B per-file identity disambiguation + its once-per-walk accounting —
# so the ratchet was answered by EXTRACTION, not by re-pinning upward. Moved to
# `vco_lib/codegraph_guards.py` (which already owned `chunk_identities` /
# `build_chunk_write_params`, so the two identity key spaces `::i` and `#n` now
# sit under one banner where a future editor sees both):
#   * `plan_chunk_texts`        — the chunk DECISION, split from the WRITE
#   * `dispatch_deferred_embed` — the 3-way deferred-embed routing
#   * `fan_out_chunk_writes`    — the N-object chunk write loop
#   * `skip_or_stamp_all_chunks`— the all-SKIP-then-STAMP ordering + memoized
#     reads that `_maybe_stamp_all_chunks` used to hold inline
#   * `stamp_single_chunk_props`— the chunk_num=0/total=1 stamp
#   * `all_chunks_skippable`    — MOVED from `codegraph_extractor_generation`
#     (it lived there only because this file was held by another lane in
#     v0.2.91; its own docstring named guards as the natural home)
# Net, including both fixes: 7292 measured 7227 before the change and 7227
# after. Re-pinned DOWNWARD from 7292 to the measured value.
# v0.2.92 BLOCKER-2 (the force-drop identity guard). The fix ADDS analyzer
# lines — the guard call at the `create_collections` chokepoint, the
# `project_prefix` field the guard checks, `repo_path` threading, and the
# refusal's distinct exit — so, per this ratchet's own rule, it was answered by
# EXTRACTION rather than an upward re-pin:
#   * the decision itself, its `DropVerdict` type and `CodeGraphDropRefused`
#     live in the new `vco_lib/codegraph_drop_guard.py`; the analyzer holds
#     ONE call (`_enforce_drop_guard`) and nothing else;
#   * `_WORKTREE_PATH_SEGMENTS` + `_worktree_segment_in_value` MOVED to
#     `vco_lib/codegraph_naming.py` — they answer "is this string a canonical
#     project name?", which is that module's whole subject, and they are pure.
#     The analyzer imports both under their historical private aliases.
# Net, including that fix: 7227. v0.2.94 (templates ruff sweep): a dead
# `import requests` and a 3→2-line except block → 7225, and the maximum moved
# down with it, as this ratchet's rule demands. This is the ONE pin:
# tests/test_v0292_n35_rewire_transform.py::TestTheRatchetHeld imports it.
_ANALYZER_LINES_MAX = 7225


def _measure() -> int:
    return len(_ANALYZER.read_text(encoding="utf-8").splitlines())


class TestAnalyzeCodeGraphRatchet(unittest.TestCase):
    def test_total_lines_ratchet(self) -> None:
        total = _measure()
        self.assertLessEqual(
            total, _ANALYZER_LINES_MAX,
            f"analyze_code_graph.py grew to {total} lines "
            f"(pin: {_ANALYZER_LINES_MAX}). The monolith must shrink, not grow"
            " — extract the new logic into a vco_lib module (per-language"
            " extractor / CodeEntity IR / shared helper) the analyzer imports."
            " Update the pin only DOWNWARD after extracting.",
        )

    def test_pin_is_not_slack(self) -> None:
        """Keep the ratchet honest: if the analyzer shrinks, tighten the pin
        (fails when the measured value drifts far below the pin, which would
        let regrowth hide under stale slack)."""
        total = _measure()
        self.assertGreater(
            total, _ANALYZER_LINES_MAX - 800,
            f"analyze_code_graph.py is {total} lines, far below the "
            f"{_ANALYZER_LINES_MAX} pin — tighten _ANALYZER_LINES_MAX to the"
            " new measured value (the per-language extractor split is expected"
            " to lower it; re-pin then).",
        )


if __name__ == "__main__":
    unittest.main()
