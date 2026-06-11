# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.54 Track D (Theme 5) regression tests for the three deferral
handlers added to ``install.py::_apply_deferred_entries``:

  * ``schema_drift_rebuild_required`` — re-probes current drift via
    ``_detect_kg_schema_drift``; the entry clears when drift is gone
    (pre-fix: blind [skip] + re-add resurrected the entry even right
    after a successful ``--rebuild-collections``).
  * ``update_resume_required`` — re-probes the launcher's resume
    sentinel; gone → resolved (pre-fix: no handler on either side, the
    entry survived every --apply-deferred run forever).
  * ``launcher_restart_required`` — consumes the documented
    ``launcher-restart-marker`` or verifies the recorded old launcher
    PID has exited (pre-fix: protocol documented, implemented nowhere).

Every handler follows the v0.2.46 re-probe discipline: act ONLY when
the live state confirms the premise changed; keep the entry on
uncertainty (probe failure, PID unparseable, sentinel present).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402


def _entry(condition_id: str, detected: str = "synthetic") -> DeferralEntry:
    return DeferralEntry(
        condition_id=condition_id,
        title=f"synthetic {condition_id}",
        detected=detected,
        why_deferred="test fixture",
        command_to_apply="echo noop",
        severity="warning",
        kg_node_refs=[],
    )


def _persist(folder: Path, *entries: DeferralEntry) -> None:
    report = DeferralReport.read(folder)
    for e in entries:
        report.add_entry(e)
    report.write(folder)


def _apply(folder: Path) -> DeferralReport:
    """Run _apply_deferred_entries against the persisted report and
    return the post-run report (what would be re-written)."""
    current = DeferralReport.read(Path(folder) / "nonexistent-empty")
    install._apply_deferred_entries(current, folder, args=None)
    return current


def _cids(report: DeferralReport) -> list:
    return [e.condition_id for e in report.entries]


class TestSchemaDriftReprobe(unittest.TestCase):
    def setUp(self):
        self._kg = os.environ.get("KG_COLLECTION")
        os.environ["KG_COLLECTION"] = "TestProj_KnowledgeGraph"

    def tearDown(self):
        if self._kg is None:
            os.environ.pop("KG_COLLECTION", None)
        else:
            os.environ["KG_COLLECTION"] = self._kg

    def test_cleared_when_drift_gone(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("schema_drift_rebuild_required"))
            with mock.patch.object(install, "_detect_kg_schema_drift",
                                   return_value=(False, [])):
                result = _apply(folder)
            self.assertNotIn("schema_drift_rebuild_required", _cids(result),
                             "entry must clear once drift is resolved")

    def test_kept_when_drift_persists(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("schema_drift_rebuild_required"))
            with mock.patch.object(install, "_detect_kg_schema_drift",
                                   return_value=(True, ["index_null_state"])):
                result = _apply(folder)
            self.assertIn("schema_drift_rebuild_required", _cids(result))

    def test_kept_on_probe_failure(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("schema_drift_rebuild_required"))
            with mock.patch.object(install, "_detect_kg_schema_drift",
                                   side_effect=OSError("weaviate down")):
                result = _apply(folder)
            self.assertIn("schema_drift_rebuild_required", _cids(result),
                          "probe failure must NOT clear the entry")

    def test_kept_when_no_kg_collection(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("schema_drift_rebuild_required"))
            os.environ.pop("KG_COLLECTION", None)
            probe = mock.MagicMock()
            with mock.patch.object(install, "_detect_kg_schema_drift", probe):
                result = _apply(folder)
            self.assertIn("schema_drift_rebuild_required", _cids(result))
            probe.assert_not_called()


class TestUpdateResumeReconciliation(unittest.TestCase):
    def test_cleared_when_sentinel_gone(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("update_resume_required"))
            result = _apply(folder)
            self.assertNotIn("update_resume_required", _cids(result),
                             "no sentinel = resume completed = resolved")

    def test_kept_while_sentinel_present(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            sentinel = folder / ".claude" / "state" / \
                "orchestrator-update-resume-needed.json"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("{}", encoding="utf-8")
            _persist(folder, _entry("update_resume_required"))
            result = _apply(folder)
            self.assertIn("update_resume_required", _cids(result),
                          "sentinel present = resume still pending = keep")
            self.assertTrue(sentinel.is_file(),
                            "handler must not delete the launcher's sentinel")


class TestLauncherRestartSelfClear(unittest.TestCase):
    def test_marker_consumed_and_cleared(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            marker = folder / ".claude" / "context" / "launcher-restart-marker"
            marker.parent.mkdir(parents=True)
            marker.write_text("restarted", encoding="utf-8")
            _persist(folder, _entry("launcher_restart_required"))
            result = _apply(folder)
            self.assertNotIn("launcher_restart_required", _cids(result))
            self.assertFalse(marker.exists(), "marker must be consumed")

    def test_dead_pid_clears(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry(
                "launcher_restart_required",
                detected="binary swapped (running launcher PID: 4194304).",
            ))
            with mock.patch.object(install, "_pid_is_alive_for_deferral",
                                   return_value=False):
                result = _apply(folder)
            self.assertNotIn("launcher_restart_required", _cids(result))

    def test_alive_pid_keeps(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry(
                "launcher_restart_required",
                detected=f"binary swapped (running launcher PID: {os.getpid()}).",
            ))
            result = _apply(folder)
            self.assertIn("launcher_restart_required", _cids(result),
                          "old launcher still running = restart pending")

    def test_no_pid_no_marker_keeps(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("launcher_restart_required",
                                    detected="no pid recorded here"))
            result = _apply(folder)
            self.assertIn("launcher_restart_required", _cids(result),
                          "uncertainty must keep the entry")


class TestPidProbe(unittest.TestCase):
    def test_own_pid_alive(self):
        self.assertTrue(install._pid_is_alive_for_deferral(os.getpid()))

    def test_nonpositive_pid_conservative(self):
        self.assertTrue(install._pid_is_alive_for_deferral(0))
        self.assertTrue(install._pid_is_alive_for_deferral(-5))

    @unittest.skipIf(sys.platform == "win32", "POSIX-only PID space probe")
    def test_unused_pid_dead(self):
        # PID 4194304 is above the default Linux pid_max (and far above
        # macOS's 99999); if the host has an exotic pid_max this still
        # holds because we probe an actual ESRCH.
        self.assertFalse(install._pid_is_alive_for_deferral(2 ** 22))

    def test_never_uses_os_kill_on_windows_path(self):
        # Guard the Windows footgun: os.kill(pid, 0) on Windows KILLS
        # the process. The win32 branch must go through ctypes, never
        # os.kill. We simulate by flipping sys.platform.
        with mock.patch.object(install.sys, "platform", "win32"), \
             mock.patch.object(install.os, "kill",
                               side_effect=AssertionError(
                                   "os.kill must not run on win32")):
            # ctypes.windll doesn't exist on Linux → the except branch
            # returns the conservative True. The assertion is that
            # os.kill was never reached.
            self.assertTrue(install._pid_is_alive_for_deferral(1234))


class TestUnknownConditionsStillPreserved(unittest.TestCase):
    def test_unknown_cid_preserved(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("some_future_condition"))
            result = _apply(folder)
            self.assertIn("some_future_condition", _cids(result))


if __name__ == "__main__":
    unittest.main()
