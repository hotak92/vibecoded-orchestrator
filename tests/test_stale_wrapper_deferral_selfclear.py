# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 L4-1: `stale_codegraph_wrapper_pending` deferral self-clear.

The Rust launcher emits `stale_codegraph_wrapper_pending` when it falls back
to the orchestrator copy of a project-local codegraph/kg-sync wrapper that
lacks the resilient `$VCT_INSTALL_ROOT` ladder. Before this fix the entry had
no re-probe / self-clear: it persisted in UPDATE_DEFERRED.md until hand-deleted
even after the user refreshed the wrapper. `_reconcile_bundle_deferrals` now
re-probes the wrapper on bundle update and clears the entry once healthy.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402


def _seed_stale_wrapper_deferral(folder: Path) -> None:
    """Write an UPDATE_DEFERRED.md carrying the stale-wrapper entry."""
    report = DeferralReport()
    report.add_entry(
        DeferralEntry(
            condition_id="stale_codegraph_wrapper_pending",
            title="Stale code-graph analyzer wrapper",
            detected="The project-local code-graph-analyze wrapper is pre-RT-4.",
            why_deferred="The launcher fell back to the orchestrator copy.",
            command_to_apply=(
                "python install.py --install-bundle --update --force "
                "--only .claude/scripts/code-graph-analyze"
            ),
            severity="info",
        )
    )
    report.write(folder)


def _write_wrapper(folder: Path, basename: str, *, resilient: bool) -> Path:
    scripts = folder / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    wrapper = scripts / basename
    if resilient:
        body = '#!/usr/bin/env bash\n: "${VCT_INSTALL_ROOT:?}"\nexec python -m x\n'
    else:
        body = "#!/usr/bin/env bash\nexec /old/hardcoded/venv/bin/python -m x\n"
    wrapper.write_text(body, encoding="utf-8")
    return wrapper


class StaleWrapperProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-stale-wrapper-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_true_when_wrapper_lacks_marker(self):
        _write_wrapper(self.tmp, "code-graph-analyze", resilient=False)
        self.assertTrue(project_init._codegraph_wrapper_still_stale(self.tmp))

    def test_probe_false_when_wrapper_has_marker(self):
        _write_wrapper(self.tmp, "code-graph-analyze", resilient=True)
        self.assertFalse(project_init._codegraph_wrapper_still_stale(self.tmp))

    def test_probe_false_when_no_wrapper(self):
        # No wrapper on disk at all → nothing stale.
        self.assertFalse(project_init._codegraph_wrapper_still_stale(self.tmp))

    def test_probe_true_when_ps1_sibling_stale(self):
        # Only the .ps1 sibling is stale — still flagged.
        _write_wrapper(self.tmp, "code-graph-analyze.ps1", resilient=False)
        self.assertTrue(project_init._codegraph_wrapper_still_stale(self.tmp))

    def test_probe_true_when_kg_sync_stale(self):
        _write_wrapper(self.tmp, "kg-sync", resilient=False)
        self.assertTrue(project_init._codegraph_wrapper_still_stale(self.tmp))


class StaleWrapperReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-stale-reconcile-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_leave_alone_when_wrapper_still_stale(self):
        """LEAVE-ALONE: a still-stale wrapper keeps the deferral entry."""
        _seed_stale_wrapper_deferral(self.tmp)
        _write_wrapper(self.tmp, "code-graph-analyze", resilient=False)

        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        report = DeferralReport.read(self.tmp)
        self.assertTrue(
            report.has_condition("stale_codegraph_wrapper_pending"),
            "a still-stale wrapper must keep the deferral",
        )

    def test_act_clears_when_wrapper_healthy(self):
        """ACT: once the wrapper carries the marker, the entry self-clears."""
        _seed_stale_wrapper_deferral(self.tmp)
        _write_wrapper(self.tmp, "code-graph-analyze", resilient=True)

        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        report = DeferralReport.read(self.tmp)
        self.assertFalse(
            report.has_condition("stale_codegraph_wrapper_pending"),
            "a healthy wrapper must clear the stale-wrapper deferral",
        )

    def test_act_clears_when_wrapper_deleted(self):
        """ACT: no wrapper on disk (user removed it) → entry self-clears."""
        _seed_stale_wrapper_deferral(self.tmp)
        # No wrapper written at all.
        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        report = DeferralReport.read(self.tmp)
        self.assertFalse(
            report.has_condition("stale_codegraph_wrapper_pending")
        )

    def test_explicit_override_wins_over_probe(self):
        """When the caller passes still_stale_wrapper explicitly, it is used
        instead of the disk probe."""
        _seed_stale_wrapper_deferral(self.tmp)
        # Wrapper is healthy on disk, but caller asserts still-stale=True.
        _write_wrapper(self.tmp, "code-graph-analyze", resilient=True)
        project_init._reconcile_bundle_deferrals(
            self.tmp,
            still_user_modified=False,
            still_skipped_existing=False,
            still_stale_wrapper=True,
        )
        report = DeferralReport.read(self.tmp)
        self.assertTrue(
            report.has_condition("stale_codegraph_wrapper_pending"),
            "explicit still_stale_wrapper=True must override the disk probe",
        )


if __name__ == "__main__":
    unittest.main()
