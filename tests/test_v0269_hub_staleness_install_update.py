# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.69 hub-staleness home #2 — install.py --update stops + restarts the hub.

A MANUAL ``python install.py --update`` has no launcher GUI to stop the hub
before the binary swap, so an OLD hub would stay alive and the post-swap
``--start-if-not-running`` (liveness-only) would no-op on the stale process.
These tests cover the install.py mirror of the GUI's
``ensure_hub_stopped_for_update`` flow:

  * ``_hub_pid_from_lockfile`` — reads the PID from line 1 only, tolerating a
    v0.2.69 two-line lockfile (back-compat).
  * ``_stop_running_vct_hub_for_update`` — no-hub / dead-hub / live-hub cases.
  * env-pinning of ``VCT_ORCHESTRATOR_ROOT`` / ``VCT_INSTALL_ROOT`` to the
    install clone when the hub is (re)started.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install  # type: ignore  # noqa: E402


class _StateDir:
    """Context manager isolating VCT_STATE_DIR to a tmp path."""

    def __init__(self, tmp: Path) -> None:
        self._tmp = tmp
        self._prev: str | None = None

    def __enter__(self) -> Path:
        self._prev = os.environ.get("VCT_STATE_DIR")
        os.environ["VCT_STATE_DIR"] = str(self._tmp)
        return self._tmp

    def __exit__(self, *exc: object) -> None:
        if self._prev is None:
            os.environ.pop("VCT_STATE_DIR", None)
        else:
            os.environ["VCT_STATE_DIR"] = self._prev


class HubPidFromLockfile(unittest.TestCase):
    def test_none_when_missing(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)):
                self.assertIsNone(install._hub_pid_from_lockfile())

    def test_reads_single_line_pid(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)) as root:
                (root / "hub.pid").write_text("4321\n", encoding="utf-8")
                self.assertEqual(install._hub_pid_from_lockfile(), 4321)

    def test_reads_pid_from_two_line_lockfile(self) -> None:
        # v0.2.69 format: pid on line 1, build identity on line 2. The reader
        # must take line 1 only and NOT choke on the identity line.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)) as root:
                (root / "hub.pid").write_text(
                    "9876\n0.2.69+deadbeef1234\n", encoding="utf-8"
                )
                self.assertEqual(install._hub_pid_from_lockfile(), 9876)

    def test_none_on_garbage_first_line(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)) as root:
                (root / "hub.pid").write_text("not-a-pid\nidentity\n",
                                              encoding="utf-8")
                self.assertIsNone(install._hub_pid_from_lockfile())

    def test_none_on_zero_pid(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)) as root:
                (root / "hub.pid").write_text("0\n", encoding="utf-8")
                self.assertIsNone(install._hub_pid_from_lockfile())


class StopRunningHubForUpdate(unittest.TestCase):
    def test_returns_false_when_no_lockfile(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)):
                # No hub.pid → nothing to stop.
                self.assertFalse(
                    install._stop_running_vct_hub_for_update(None)
                )

    def test_dead_pid_cleans_lockfile_and_returns_false(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)) as root:
                pid_file = root / "hub.pid"
                pid_file.write_text("12345\n", encoding="utf-8")
                with mock.patch.object(
                    install, "_pid_is_alive_for_deferral", return_value=False
                ):
                    result = install._stop_running_vct_hub_for_update(None)
                self.assertFalse(result)
                self.assertFalse(
                    pid_file.exists(),
                    "stale lockfile for a dead pid must be cleaned up",
                )

    def test_live_pid_is_stopped_via_polite_stop(self) -> None:
        # Live pid + a binary → polite `--stop` is invoked, then the pid dies
        # (we model the pid going away after the --stop call) and the lockfile
        # is cleaned. Returns True.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)) as root:
                pid_file = root / "hub.pid"
                pid_file.write_text("4242\n", encoding="utf-8")
                binary = root / "vct-hub"
                binary.write_text("#!/bin/sh\n", encoding="utf-8")
                binary.chmod(0o755)

                # First alive-check (pre-stop) returns True; after the
                # subprocess --stop runs, the poll sees the pid as dead.
                alive_sequence = iter([True, False])

                def fake_alive(pid: int) -> bool:
                    try:
                        return next(alive_sequence)
                    except StopIteration:
                        return False

                stop_calls: list[list[str]] = []

                def fake_run(cmd, **kwargs):  # noqa: ANN001
                    stop_calls.append(list(cmd))
                    return mock.Mock(returncode=0)

                with mock.patch.object(
                    install, "_pid_is_alive_for_deferral", side_effect=fake_alive
                ), mock.patch.object(install.subprocess, "run", side_effect=fake_run):
                    result = install._stop_running_vct_hub_for_update(binary)

                self.assertTrue(result, "a live hub should be stopped → True")
                self.assertTrue(
                    any("--stop" in c for c in stop_calls),
                    f"polite --stop must be invoked; calls={stop_calls}",
                )
                self.assertFalse(
                    pid_file.exists(), "lockfile must be cleaned after stop"
                )

    def test_live_pid_force_killed_when_polite_stop_does_not_take(self) -> None:
        # Polite --stop runs but the pid stays alive through the 10s poll →
        # escalate to _force_kill_pid. We mock the force-kill to succeed.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with _StateDir(Path(td)) as root:
                pid_file = root / "hub.pid"
                pid_file.write_text("5555\n", encoding="utf-8")
                binary = root / "vct-hub"
                binary.write_text("#!/bin/sh\n", encoding="utf-8")
                binary.chmod(0o755)

                # Always alive during the poll → forces escalation. Shorten
                # the poll by patching time so the test stays fast.
                with mock.patch.object(
                    install, "_pid_is_alive_for_deferral", return_value=True
                ), mock.patch.object(
                    install.subprocess, "run", return_value=mock.Mock(returncode=0)
                ), mock.patch.object(
                    install, "_force_kill_pid", return_value=True
                ) as fk, mock.patch.object(install.time, "time", side_effect=[
                    # start, then immediately past the 10s deadline so the
                    # poll loop exits at once into the escalation branch.
                    1000.0, 1000.0, 1100.0,
                ]):
                    result = install._stop_running_vct_hub_for_update(binary)

                self.assertTrue(result)
                fk.assert_called_once_with(5555)
                self.assertFalse(pid_file.exists())


class DeployStartEnvPinning(unittest.TestCase):
    """Step 8c must pin VCT_ORCHESTRATOR_ROOT / VCT_INSTALL_ROOT to the
    install clone so the restarted hub's watchdog targets the right tree."""

    def test_start_pins_orchestrator_and_install_root(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            install_root = Path(td) / "clone"
            install_root.mkdir()
            binary = install_root / "vct-hub"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)

            captured_env: dict[str, str] = {}

            def fake_run(cmd, **kwargs):  # noqa: ANN001
                captured_env.update(kwargs.get("env") or {})
                return mock.Mock(returncode=0, stdout="", stderr="")

            with _StateDir(Path(td)):
                with mock.patch.object(
                    install, "_ensure_vct_hub_binary", return_value=binary
                ), mock.patch.object(
                    install, "_write_vct_hub_cutover_sentinel", return_value=None
                ), mock.patch.object(
                    install, "_wait_for_vct_hub_health", return_value=True
                ), mock.patch.object(
                    install, "_delete_vct_hub_cutover_sentinel"
                ), mock.patch.object(
                    install.subprocess, "run", side_effect=fake_run
                ):
                    # stop_running_first=False → no prior hub stop; just the
                    # start, which is what carries the env-pinning.
                    install._deploy_and_start_vct_hub(
                        install_root, stop_running_first=False
                    )

            self.assertEqual(
                captured_env.get("VCT_ORCHESTRATOR_ROOT"),
                str(install_root.resolve()),
            )
            self.assertEqual(
                captured_env.get("VCT_INSTALL_ROOT"),
                str(install_root.resolve()),
            )

    def test_update_path_invokes_pre_swap_stop(self) -> None:
        # stop_running_first=True must call the stop helper BEFORE the start.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            install_root = Path(td) / "clone"
            install_root.mkdir()
            binary = install_root / "vct-hub"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)

            with _StateDir(Path(td)):
                with mock.patch.object(
                    install, "_ensure_vct_hub_binary", return_value=binary
                ), mock.patch.object(
                    install, "_write_vct_hub_cutover_sentinel", return_value=None
                ), mock.patch.object(
                    install, "_wait_for_vct_hub_health", return_value=True
                ), mock.patch.object(
                    install, "_delete_vct_hub_cutover_sentinel"
                ), mock.patch.object(
                    install, "_stop_running_vct_hub_for_update", return_value=True
                ) as stop_mock, mock.patch.object(
                    install.subprocess, "run",
                    return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                ):
                    install._deploy_and_start_vct_hub(
                        install_root, stop_running_first=True
                    )

            stop_mock.assert_called_once()
            # The stop helper is handed the freshly-resolved binary so its
            # polite --stop runs the NEW binary against the OLD lockfile pid.
            self.assertEqual(stop_mock.call_args.args[0], binary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
