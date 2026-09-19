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
    """v0.2.95 F1 REWRITE. The v0.2.89 contract ("sidecar version >= source
    AND live /health") lived in a hand-written install.py branch that ran
    BEFORE the hub-restart step of the same update — so it could never see
    the hub it had just restarted, and the row was immortal (the 0.2.94
    dogfood). The branch is GONE (arch review §7: collapse the duplicate
    deciders); the lifecycle is now the STATE-keyed registry probe
    `hub_back_after_restart_failure` declared in deferral_conditions.toml —
    "does the hub answer /api/v1/health on the resolved port?" — settled by
    the generic probe-first dispatch wherever a re-probe pass runs. The
    version AND was dropped deliberately: the entry records a HEALTH failure
    ("did not come back within 30 s"), so a hub that is up NOW is the
    premise changing, whatever version it runs (R26: state-keyed, never
    "this release fixed it").

    Hermeticity: the hub here is a local http.server on an ephemeral port,
    pinned via VCT_HUB_PORT (the ONE resolution chain). The old tests mocked
    `install._probe_vct_hub_health`, which no longer exists as a dispatch
    point — the probe lives in vco_lib.deferral_probes and consults neither
    install.py nor the machine's real port when VCT_HUB_PORT is pinned.
    """

    CID = "hub_restart_failed_after_abort"

    def test_hub_up_clears_through_the_registry_probe(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — http.server API
                body = json.dumps({"status": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 — base API name
                pass

        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry(self.CID))
            server = HTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.dict(
                    os.environ, {"VCT_HUB_PORT": str(server.server_address[1])}
                ):
                    result = _apply(folder)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertNotIn(
                self.CID, _cids(result),
                "hub answering /health = the recorded failure is over")
            trail = folder / ".claude" / "logs" / "auto-resolutions.jsonl"
            self.assertTrue(trail.is_file(), "a clear must leave a B-F9 trail")
            rows = [json.loads(ln) for ln in
                    trail.read_text(encoding="utf-8").splitlines() if ln]
            self.assertTrue(any(
                r.get("condition_id") == self.CID
                and r.get("action") == "resolved_by_registry_probe"
                for r in rows))

    def test_hub_down_keeps_the_actionable_failure(self):
        """Nothing answering on the resolved port is exactly the state the
        entry records — PRESERVE it."""
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry(self.CID))
            with mock.patch.dict(os.environ, {"VCT_HUB_PORT": str(port)}):
                result = _apply(folder)
            self.assertIn(
                self.CID, _cids(result),
                "hub down = the failure stands; only positive evidence clears")

    def test_unresolvable_port_keeps_the_entry(self):
        """Could-not-look is not a verdict: the probe must decline (None),
        never clear on an unanswerable question."""
        from vco_lib import access_resolver

        def _boom():
            raise RuntimeError("no port in this universe")

        with TemporaryDirectory() as td:
            folder = Path(td)
            _persist(folder, _entry(self.CID))
            with mock.patch.object(access_resolver, "_hub_port", _boom):
                result = _apply(folder)
            self.assertIn(self.CID, _cids(result),
                          "probe failure must NOT clear the entry")

    def test_the_hand_written_branch_is_gone_and_the_probe_declared(self):
        """The old branch is unreachable by construction (probe-first
        dispatch) and its clear condition could never fire in-run; pin that
        it was COLLAPSED into the declared probe, not kept as dead code."""
        from vco_lib import deferral_probes

        src = (Path(__file__).resolve().parent.parent
               / "install.py").read_text(encoding="utf-8")
        self.assertNotIn(f'elif cid == "{self.CID}":', src)
        self.assertEqual(
            deferral_probes.registry_probe_name(self.CID),
            "hub_back_after_restart_failure",
        )

    def test_foreign_cid_not_in_owned_set(self):
        """FOREIGN (Rust-emitted): must NOT be in _INSTALL_OWNED_CONDITION_IDS
        (else the A-2 data-loss clobber). It clears via the registry probe's
        mark_resolved leg, same as before."""
        self.assertNotIn(
            "hub_restart_failed_after_abort",
            install._INSTALL_OWNED_CONDITION_IDS,
        )


if __name__ == "__main__":
    unittest.main()
