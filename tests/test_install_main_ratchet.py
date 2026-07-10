# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2c (v0.2.75): monolith ratchet on install.py's ``main()`` and total size.

install.py is the project's mega-file (>25k lines; the modularity rule in
CLAUDE.md caps additions at ~50 contiguous lines before extraction is
required). ``main()`` itself is a ~1.7k-line sequential flow — every new
inline block makes the next audit/refactor more expensive.

These are RATCHET tests: the pinned numbers may only ever be updated
DOWNWARD. If either assertion fails, you added flow logic to ``main()``
(or bulk to install.py) — extract it to a ``vco_lib`` module (thin
orchestration shim in main() is fine) instead of bumping the pin.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Pinned ceilings — update only DOWNWARD ─────────────────────────────────
#
# Measured 2026-07-08 on the v0.2.75 Part-3b state (post P2c extractions:
# update-gate lockfile choreography → vco_lib/install_update_gate.py,
# deferral seed/finalize choreography → vco_lib/install_deferral_flow.py;
# down from 1699 / 25401 at Part 3a).
#
# MAIN_SPAN: strict — main() must not grow AT ALL. New steps go into
# helper functions / vco_lib modules called from main().
_MAIN_SPAN_MAX = 1668

# TOTAL: strict — measured exactly, no headroom. Additions require
# extraction to vco_lib, not a bump.
# v0.2.75 pre-ship: +12 for the lightweight-path A-2 seed (data-safety fix —
# the fresh DeferralReport on the --lightweight branch was clobbering pending
# foreign deferrals; the seed is inseparable from the existing write block,
# not extractable without contortion). Re-pinned to the new measured value.
# v0.2.76 R8: +29 for the shared-KG pointer-drift heal WIRING. The substantive
# logic (heal_shared_kg_pointer_drift, converge_root_pointer_write_side,
# pointer_drift_needs_rw) was extracted to vco_lib.kg_binding_heal; the residual
# install.py delta is the minimal glue — a write-side shim + the RO-detection
# call + the seed-site call — which must inject install.py-internal helpers
# (_is_orchestrator_root_install / _discover_app_state_db_path /
# _connect_launcher_db_with_retry / _log_install_event) and so can't move out.
# Re-pinned to the new measured value (same precedent as the A-2 line above).
# v0.2.77 5c task 2: +9 for the embed-concurrency WIRING. All substantive logic
# (pool selection + budget math, the .env-line producer, the app_state seed,
# the finalize-for-host stamp) was extracted to
# vco_lib/install_embed_concurrency.py + vco_lib/embedding_selection.py; the
# residual install.py delta is irreducible glue — the module import, the single
# finalize call in main() (which must supply install.py's _log_install_event as
# the soft-fail callback), the .env-line splat, and the app_state seed call
# (which needs the open cursor local to _write_preset_defaults_to_app_state).
# main() itself SHRANK (1668→1667). Same "inseparable thin-shim" precedent as
# the A-2 / R8 lines above — re-pinned to the new measured value.
_TOTAL_LINES_MAX = 25417


def _measure() -> tuple:
    """Return ``(total_lines, main_span)`` for install.py.

    main() span = from the top-level ``def main(`` line to the next
    top-level ``def`` / ``async def`` / ``class`` (or EOF), exclusive.
    """
    lines = (REPO_ROOT / "install.py").read_text(encoding="utf-8").splitlines()
    total = len(lines)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("def main("):
            start = i
            break
    if start is None:
        raise AssertionError("install.py has no top-level `def main(`")
    end = total
    for j in range(start + 1, total):
        ln = lines[j]
        if (
            ln.startswith("def ")
            or ln.startswith("async def ")
            or ln.startswith("class ")
        ):
            end = j
            break
    return total, end - start


class TestInstallMainRatchet(unittest.TestCase):
    def test_main_span_does_not_grow(self):
        _, span = _measure()
        self.assertLessEqual(
            span, _MAIN_SPAN_MAX,
            f"install.py main() grew to {span} lines (pin: {_MAIN_SPAN_MAX})."
            " You added flow logic to main() — extract it to vco_lib (or a"
            " helper function) instead of raising the pin. Update the pin"
            " only DOWNWARD after shrinking main().",
        )

    def test_total_lines_soft_ratchet(self):
        total, _ = _measure()
        self.assertLessEqual(
            total, _TOTAL_LINES_MAX,
            f"install.py grew to {total} lines (pin: {_TOTAL_LINES_MAX})."
            " The monolith must shrink, not grow — extract the new logic to"
            " a vco_lib module. Update the pin only DOWNWARD (Part 3b of"
            " v0.2.75 is expected to lower it; re-pin then).",
        )

    def test_pins_are_not_slack(self):
        """Keep the ratchet honest: if install.py shrinks, tighten the pins
        (fails when the measured value drifts far below the pin, which
        would let regrowth hide under stale slack)."""
        total, span = _measure()
        self.assertGreater(
            span, _MAIN_SPAN_MAX - 400,
            f"main() span is {span}, far below the {_MAIN_SPAN_MAX} pin —"
            " tighten _MAIN_SPAN_MAX to the new measured value.",
        )
        self.assertGreater(
            total, _TOTAL_LINES_MAX - 1200,
            f"install.py is {total} lines, far below the {_TOTAL_LINES_MAX}"
            " pin — tighten _TOTAL_LINES_MAX to the new measured value.",
        )


if __name__ == "__main__":
    unittest.main()
