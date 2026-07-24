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

import json
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

    def test_unknown_foreign_cid_still_hits_unknown_branch(self):
        """v0.2.89 guard: the new hub_restart_failed_after_abort elif must not
        accidentally swallow OTHER foreign cids — a genuinely-unknown cid still
        falls through to the [unknown] preserve branch."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("some_totally_novel_foreign_cid"))
            result = _apply(folder)
            self.assertIn("some_totally_novel_foreign_cid", _cids(result),
                          "an unknown foreign cid must be preserved, not "
                          "swallowed by a sibling handler")


def _write_hub_sidecar(folder: Path, version: "str | None") -> None:
    """Materialize launcher/dist/<os>/vct-hub[.exe].metadata.json with the
    given launcher_version. Pass version=None to intentionally OMIT the
    sidecar (missing-metadata case)."""
    subdir, fname = install._launcher_binary_relative_path()
    dist = folder / "launcher" / "dist" / subdir
    dist.mkdir(parents=True, exist_ok=True)
    hub_meta_name = (
        "vct-hub.exe.metadata.json"
        if fname.endswith(".exe")
        else "vct-hub.metadata.json"
    )
    if version is not None:
        (dist / hub_meta_name).write_text(
            json.dumps({"launcher_version": version}), encoding="utf-8"
        )


class TestGeneratedFilesReconciledSelfClears(unittest.TestCase):
    """v0.2.89: `generated_files_reconciled` is a FOREIGN (Rust-emitted)
    historical audit record with nothing to re-probe — reaching
    _apply_deferred_entries means the reconciling update already completed,
    so it must self-clear (mark_resolved). Phase 2c added the handler but no
    test; this closes that gap."""

    def test_self_clears_unconditionally(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("generated_files_reconciled"))
            result = _apply(folder)
            self.assertNotIn("generated_files_reconciled", _cids(result),
                             "historical audit record must self-clear once the "
                             "reconciling update has completed")

    def test_foreign_cid_not_in_owned_set(self):
        """It must NOT be in _INSTALL_OWNED_CONDITION_IDS — a foreign,
        Rust-emitted cid listed there is silently clobbered on the next update
        (the A-2 data-loss bug); it self-clears via mark_resolved instead."""
        self.assertNotIn(
            "generated_files_reconciled",
            install._INSTALL_OWNED_CONDITION_IDS,
        )


class TestHubRestartFailedAfterAbortReprobe(unittest.TestCase):
    """v0.2.89: `hub_restart_failed_after_abort` is an ACTIONABLE failure
    record (the abort-path hub restart's health poll failed). It self-clears
    ONLY once the on-disk hub sidecar version >= source (Step 8 refreshed the
    hub binary) AND the live /health probe confirms the hub is UP (MAJOR-2 — a
    caught-up binary is not proof the hub is running; Step 8 is soft-fail).
    Otherwise the actionable failure survives to the next run. Re-probe
    discipline: act only on a confirmed premise change."""

    def test_resolved_when_hub_caught_up_and_healthy(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("hub_restart_failed_after_abort"))
            _write_hub_sidecar(folder, "0.2.89")
            with mock.patch.object(
                install, "_read_launcher_version", return_value="0.2.89"
            ), mock.patch.object(
                install, "_probe_vct_hub_health", return_value=True
            ):
                result = _apply(folder)
            self.assertNotIn("hub_restart_failed_after_abort", _cids(result),
                             "on-disk hub >= source AND /health live = hub "
                             "refreshed + up = failure resolved")

    def test_resolved_when_hub_ahead_of_source_and_healthy(self):
        # >= not == : a hub sidecar ahead of source is still resolved (when up).
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("hub_restart_failed_after_abort"))
            _write_hub_sidecar(folder, "0.2.90")
            with mock.patch.object(
                install, "_read_launcher_version", return_value="0.2.89"
            ), mock.patch.object(
                install, "_probe_vct_hub_health", return_value=True
            ):
                result = _apply(folder)
            self.assertNotIn("hub_restart_failed_after_abort", _cids(result))

    def test_preserved_when_version_caught_up_but_health_fails(self):
        """MAJOR-2: version caught up BUT the live /health probe fails → the hub
        binary was refreshed yet the hub is NOT up (Step 8 soft-failed). This is
        exactly the 'hub down' state the entry records — PRESERVE it; clearing
        on version alone would wrongly delete an actionable failure."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("hub_restart_failed_after_abort"))
            _write_hub_sidecar(folder, "0.2.89")
            with mock.patch.object(
                install, "_read_launcher_version", return_value="0.2.89"
            ), mock.patch.object(
                install, "_probe_vct_hub_health", return_value=False
            ):
                result = _apply(folder)
            self.assertIn("hub_restart_failed_after_abort", _cids(result),
                          "version caught up but hub down = keep the actionable "
                          "failure record (MAJOR-2)")

    def test_preserved_when_hub_behind_source(self):
        """The actionable-failure-survives leg: on-disk hub < source means the
        hub has NOT caught up, so the abort-time restart failure stands. The
        health probe is not even consulted (version gate fails first), but a
        stubbed-live probe must NOT rescue a behind-version hub."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("hub_restart_failed_after_abort"))
            _write_hub_sidecar(folder, "0.2.60")
            with mock.patch.object(
                install, "_read_launcher_version", return_value="0.2.89"
            ), mock.patch.object(
                install, "_probe_vct_hub_health", return_value=True
            ):
                result = _apply(folder)
            self.assertIn("hub_restart_failed_after_abort", _cids(result),
                          "hub behind source = not caught up = keep the "
                          "actionable failure record (even if /health is live)")

    def test_preserved_when_sidecar_missing(self):
        """Missing hub sidecar → cannot POSITIVELY confirm the hub caught up →
        conservatively preserve (unlike launcher_update_diverged, which treats
        an absent hub sidecar as OK; THIS cid is specifically about the hub)."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("hub_restart_failed_after_abort"))
            _write_hub_sidecar(folder, None)  # sidecar intentionally absent
            with mock.patch.object(
                install, "_read_launcher_version", return_value="0.2.89"
            ), mock.patch.object(
                install, "_probe_vct_hub_health", return_value=True
            ):
                result = _apply(folder)
            self.assertIn("hub_restart_failed_after_abort", _cids(result),
                          "absent sidecar = unconfirmed = keep")

    def test_preserved_on_probe_exception(self):
        """Any exception during the version re-probe must preserve (never
        wrongly clear an actionable failure)."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("hub_restart_failed_after_abort"))
            _write_hub_sidecar(folder, "0.2.89")
            with mock.patch.object(
                install, "_read_launcher_version",
                side_effect=RuntimeError("boom"),
            ):
                result = _apply(folder)
            self.assertIn("hub_restart_failed_after_abort", _cids(result),
                          "probe failure must NOT clear the entry")

    def test_preserved_when_health_probe_raises(self):
        """MAJOR-2: if the live /health probe itself RAISES (rather than
        returning False), the handler must still PRESERVE — never wrongly clear
        on an unconfirmable health state."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry("hub_restart_failed_after_abort"))
            _write_hub_sidecar(folder, "0.2.89")
            with mock.patch.object(
                install, "_read_launcher_version", return_value="0.2.89"
            ), mock.patch.object(
                install, "_probe_vct_hub_health",
                side_effect=RuntimeError("probe boom"),
            ):
                result = _apply(folder)
            self.assertIn("hub_restart_failed_after_abort", _cids(result),
                          "a raising health probe = unconfirmed = keep")

    def test_foreign_cid_not_in_owned_set(self):
        """FOREIGN (Rust-emitted): must NOT be in _INSTALL_OWNED_CONDITION_IDS
        (else the A-2 data-loss clobber). It self-clears via the re-probe
        mark_resolved leg, same as launcher_update_diverged."""
        self.assertNotIn(
            "hub_restart_failed_after_abort",
            install._INSTALL_OWNED_CONDITION_IDS,
        )


if __name__ == "__main__":
    unittest.main()
