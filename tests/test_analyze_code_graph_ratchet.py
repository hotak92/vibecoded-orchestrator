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
_ANALYZER_LINES_MAX = 9855


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
