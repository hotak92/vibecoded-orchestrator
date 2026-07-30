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
# v0.2.81 Step 4c: +1 (1668→1669) for the single
# `_project_init.materialize_root_knowledge(...)` call line + its blank
# separator in main(). ALL substantive logic (enumeration gate + skip-existing/
# symlink-guard write + self-print + soft-fail + logging via callback) lives in
# vco_lib.project_init.{materialize_root_knowledge,_materialize_root_knowledge_impl};
# the residual install.py delta is the one irreducible glue line (must supply
# install.py's module-global PROJECT_ROOT + _log_install_event). Same
# "inseparable thin-shim" precedent as the codegraph-ts / 5c lines in the TOTAL
# block below. Re-pinned UP by 1 to the new measured span.
# v0.2.89 FIX 1+2: +9 (1669→1678) for irreducible glue in main()'s existing
# seed try/except + the --update MCP-remnant-check cluster. ALL substantive
# logic (the bounded Weaviate-readiness poll + the stale-.mcp.json detect/
# backup/quarantine) lives in vco_lib.install_weaviate.{wait_for_weaviate_ready,
# quarantine_stale_mcp_json_shadow,resolve_settings_weaviate_env,
# mcp_json_weaviate_env_is_stale}; the residual main() delta is the thin-shim
# glue that must supply install.py's env-resolution + PROJECT_ROOT +
# _deferral_report + the 3 short comments explaining WHY the two seed calls now
# raise on an unreachable Weaviate (the readiness gate lives in
# _ensure_collections / _seed_weaviate_impl, both OUTSIDE main()). Same
# "inseparable thin-shim" precedent as the Step-4c / codegraph-ts lines. Re-pinned
# UP to the new measured span.
# v0.2.89 wave-2 re-pin (1678 → 1687, +9) — attribution corrected per the
# wave-2 review (F2): the +9 span is NOT wave-2 work. It came from WAVE-1's
# install.py follow-up (ebbeaa7a — soft-fail quarantine reaches user
# projects + background-embed footer glue in main()), which landed WITHOUT
# re-pinning: the ratchet was already RED at the wave-2 base (measured 1687
# vs the 1678 pin). Wave-2 P1's KG_SYNC_PROJECT_ROOT seed pins added +0
# here — both seed sites live OUTSIDE main()
# (_seed_weaviate_shared_kg_only ~:15171 and _seed_weaviate_impl ~:15703;
# main() spans ~5471–7157). The original re-pin commit (332e362f)
# misattributed the growth to the P1 seed pins.
_MAIN_SPAN_MAX = 1687

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
# v0.2.77 Part 5 (CG-2): +59 for the optional codegraph-ts (tree-sitter
# call-extraction) install step _install_codegraph_treesitter. The DECISION
# (skip-env / no-pyproject / pip-target argv) was extracted to
# vco_lib.install_companions.codegraph_ts_install_plan (pure + unit-tested);
# the residual install.py body is irreducible soft-fail glue — the
# _run_logged_subprocess call with its dot-cycle animation, the _pip_install_flags
# / _pip_subprocess_env threading, and the _log_install_event lifecycle events —
# structurally identical to the sibling _install_playwright_browsers, which lives
# in install.py for the same reason (subprocess orchestration coupled to install
# internals). main() did NOT grow (single call line fit the existing 1668 budget;
# no comment block, no blank). Same "inseparable thin-shim" precedent as the
# A-2 / R8 / 5c lines above — re-pinned to the new measured value.
# v0.2.77 Part 7a (cluster F): -9 net. Four in-memory `_emit_*_deferral`
# emitters (launcher_restart / binary_swap_locked / update_resume_required /
# dual_ollama) dropped their hand-written guard + try/except/soft-fail
# boilerplate in favour of the shared vco_lib.deferral_report.safe_emit_entry
# factory - per-site emitters are now data-only. Cluster A+B (Weaviate helper
# convergence) was net-0 on install.py (import cost offset by a POST-call
# consolidation). v0.2.77 Part 7a (cluster D): -34 more. install.py's inline
# _read_app_state_key / _write_app_state_key bodies collapsed to thin
# delegators onto the new vco_lib.launcher_db_writer (canonical app_state
# read/write home). Re-pinned DOWNWARD to the new measured value.
# v0.2.77 Part 7a-bis (task 3): -520. The pinned-npm install + drift core
# (_install_pinned_npm + ~10 helpers) moved to vco_lib.install_npm to break
# the vco_lib.cli.verify -> install back-edge; install.py keeps thin
# name-stable wrappers. Re-pinned DOWNWARD to the new measured value.
# v0.2.77 Part 7a-bis (task 4): -104. Deleted the dead
# _build_canonical_env_template_text + _env_canonical_template pair (zero
# callers; the Rust build_canonical_env_text is the sole .env renderer and
# vco_lib.env_template.list_canonical_env_template_keys is the Python key
# authority). Re-pinned DOWNWARD to the new measured value.
# v0.2.81 Step 4c: the only install.py addition is the single main() call line
# + its blank separator (see MAIN_SPAN note above); materialize_root_knowledge
# + its impl helper live entirely in vco_lib.project_init. Total measured at
# 24809 = the existing pin (HEAD carried 2 lines of slack here), so the TOTAL
# pin stays UNCHANGED at 24809 — the addition fit the existing budget exactly.
# v0.2.85 (PLAN-v0285 D1): root install now DELEGATES to the one bundle engine —
# Steps 5b + 9b (the bespoke .claude/ materialize + agents/skills install, their
# classifier / manifest writer / settings merges) were DELETED (~936 LOC out).
# Measured 23,889. Re-pinned DOWNWARD to 24089 (measured + a small headroom) per
# the "update only DOWNWARD" discipline, so the slack-guard (`total > max-1200`)
# stays meaningful and regrowth can't hide under the old 24809 budget.
# v0.2.89 FIX 1+2: re-pinned UP to the measured 24316. TWO components:
#   (1) PRE-EXISTING DRIFT: at this fix's base HEAD install.py already measured
#       24186 — ~97 lines OVER the 24089 v0.2.85 pin, from commits landed between
#       v0.2.85 and now that grew install.py without re-pinning (the ratchet was
#       already RED at HEAD, independent of this change). FLAGGED for the release
#       coordinator; not introduced here.
#   (2) THIS CHANGE (+130): the field-fix additions. ALL substantive logic was
#       extracted to vco_lib.install_weaviate (wait_for_weaviate_ready +
#       quarantine_stale_mcp_json_shadow + the two pure predicates); install.py
#       keeps ONLY thin same-signature wrappers (so the `install.<name>`
#       accessors + monkeypatch contract keep resolving), the WEAVIATE_READY_
#       TIMEOUT constant, the two in-function readiness gates, and the FIELD-
#       REPORT header comment. Same "inseparable thin-shim" precedent as the
#       R8 / 5c / codegraph-ts lines above. Pinned to the measured value with no
#       headroom per the "TOTAL: strict — measured exactly" contract.
# v0.2.89 wave-2 re-pin (24316 → 24365, +49) — attribution corrected per
# the wave-2 review (F2): TWO components, misattributed as one by the
# original re-pin commit (332e362f).
#   (1) WAVE-1 follow-up, +32: the soft-fail/.mcp.json-quarantine review
#       round (ebbeaa7a — quarantine reaches user projects, background-embed
#       footer) grew install.py 24316 → 24348 WITHOUT re-pinning — a
#       base-RED episode (the ratchet was already failing at the wave-2
#       base, independent of wave-2 work).
#   (2) WAVE-2 P1, +17: the KG_SYNC_PROJECT_ROOT env pins at both seed
#       subprocess sites + their explanatory comments (BUG-3 wrong-project
#       fix; inseparable seed-site glue — the substantive root-resolution
#       logic ships in templates/scripts/sync_knowledge_graph.py + the
#       kg-sync wrappers), ALL outside main() (+0 span).
_TOTAL_LINES_MAX = 24365


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
