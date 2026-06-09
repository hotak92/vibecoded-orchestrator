# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AI — update-in-progress lockfile gate tests.

Background: during ``update orchestrator`` on Windows, launcher restart
+ MCP supervisor restart + Claude Code's reconnection attempts overlap.
On Windows mandatory file locks, every MCP-spawn-against-an-updating-
binary fails → Claude Code retries → respawn loop. The user's reproduction
showed ~97 python processes (MCP search + vct-coordination self-spawning)
and ~77 node processes (npx @upstash/context7 + @modelcontextprotocol/*),
CPU 100% for hours, requiring manual taskkill.

The fix is :mod:`vco_lib.update_gate` — a lockfile at
``<vct_root>/.update-in-progress.json`` that the launcher writes BEFORE
the git pull and deletes AFTER install.py succeeds. MCP servers check
this lockfile at startup and exit cleanly with EXIT_UPDATE_IN_PROGRESS
(75) when active, breaking the respawn loop.

These tests pin the lockfile lifecycle + the exit-on-active contract.
The Rust side (launcher/src-tauri/src/commands/update_gate.rs) has its
own cargo-test parity suite that exercises the same contract.

See v0.2.52 backlog § V52-AI.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from vco_lib import update_gate  # noqa: E402


class LockfileRoundTripTests(unittest.TestCase):
    """Component 1: lockfile write/read/delete round-trip."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.lockfile = self.tmp_root / update_gate.LOCKFILE_BASENAME

    def test_write_then_read_returns_same_payload(self) -> None:
        update_gate.write_lockfile(
            phase="git_pull", expected_duration_min=15, path=self.lockfile
        )
        data = update_gate.read_lockfile(path=self.lockfile)
        self.assertIsNotNone(data)
        assert data is not None  # for type checker
        self.assertEqual(data["phase"], "git_pull")
        self.assertEqual(data["started_by_pid"], os.getpid())
        self.assertIn("started_at", data)
        self.assertIn("expected_completion_by", data)

    def test_write_advances_phase(self) -> None:
        # Initial write claims git_pull phase.
        update_gate.write_lockfile(phase="git_pull", path=self.lockfile)
        first = update_gate.read_lockfile(path=self.lockfile)
        assert first is not None
        self.assertEqual(first["phase"], "git_pull")

        # Subsequent write advances to install_py.
        update_gate.write_lockfile(phase="install_py", path=self.lockfile)
        second = update_gate.read_lockfile(path=self.lockfile)
        assert second is not None
        self.assertEqual(second["phase"], "install_py")

    def test_delete_removes_lockfile(self) -> None:
        update_gate.write_lockfile(path=self.lockfile)
        self.assertTrue(self.lockfile.exists())
        self.assertTrue(update_gate.delete_lockfile(path=self.lockfile))
        self.assertFalse(self.lockfile.exists())

    def test_delete_when_absent_returns_true(self) -> None:
        # Idempotent: deleting an already-absent lockfile must not error.
        self.assertFalse(self.lockfile.exists())
        self.assertTrue(update_gate.delete_lockfile(path=self.lockfile))

    def test_read_when_absent_returns_none(self) -> None:
        self.assertFalse(self.lockfile.exists())
        self.assertIsNone(update_gate.read_lockfile(path=self.lockfile))

    def test_read_corrupt_json_returns_none(self) -> None:
        # Partial/corrupt files must NOT raise — MCPs and hooks treat
        # this as "no lockfile" and proceed.
        self.lockfile.write_text("{ not valid json", encoding="utf-8")
        self.assertIsNone(update_gate.read_lockfile(path=self.lockfile))

    def test_write_creates_parent_dir(self) -> None:
        # mkdir(parents=True, exist_ok=True) — works on first-ever boot
        # where ~/.vct/ may not yet exist.
        deep_lockfile = self.tmp_root / "nested" / "dir" / "lock.json"
        self.assertFalse(deep_lockfile.parent.exists())
        update_gate.write_lockfile(path=deep_lockfile)
        self.assertTrue(deep_lockfile.exists())


class IsUpdateInProgressTests(unittest.TestCase):
    """Component 2 contract: is_update_in_progress() decision matrix."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lockfile = Path(self._tmp.name) / update_gate.LOCKFILE_BASENAME

    def test_no_lockfile_returns_false(self) -> None:
        self.assertFalse(update_gate.is_update_in_progress(path=self.lockfile))

    def test_fresh_lockfile_returns_true(self) -> None:
        update_gate.write_lockfile(
            expected_duration_min=15, path=self.lockfile
        )
        self.assertTrue(update_gate.is_update_in_progress(path=self.lockfile))

    def test_stale_lockfile_returns_false(self) -> None:
        # Write a lockfile whose deadline is in the past.
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
        self.lockfile.write_text(
            json.dumps(
                {
                    "started_at": past.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "started_by_pid": 1,
                    "phase": "git_pull",
                    "expected_completion_by": past.strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            )
        )
        self.assertFalse(update_gate.is_update_in_progress(path=self.lockfile))

    def test_lockfile_missing_deadline_returns_false(self) -> None:
        # If expected_completion_by is missing/malformed, treat as stale
        # so we don't block forever on a half-written lockfile.
        self.lockfile.write_text(
            json.dumps(
                {"started_at": "2026-06-09T10:00:00Z", "phase": "git_pull"}
            )
        )
        self.assertFalse(update_gate.is_update_in_progress(path=self.lockfile))


class CleanupIfStaleTests(unittest.TestCase):
    """Component 4: boot-time stale-lockfile cleanup."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lockfile = Path(self._tmp.name) / update_gate.LOCKFILE_BASENAME

    def test_cleanup_removes_stale_lockfile(self) -> None:
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=30)
        self.lockfile.write_text(
            json.dumps(
                {
                    "started_at": past.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "started_by_pid": 1,
                    "phase": "binary_refresh",
                    "expected_completion_by": past.strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            )
        )
        self.assertTrue(update_gate.cleanup_if_stale(path=self.lockfile))
        self.assertFalse(self.lockfile.exists())

    def test_cleanup_leaves_fresh_lockfile(self) -> None:
        update_gate.write_lockfile(
            expected_duration_min=15, path=self.lockfile
        )
        self.assertFalse(update_gate.cleanup_if_stale(path=self.lockfile))
        self.assertTrue(self.lockfile.exists())

    def test_cleanup_when_absent_is_noop(self) -> None:
        self.assertFalse(update_gate.cleanup_if_stale(path=self.lockfile))
        self.assertFalse(self.lockfile.exists())


class ExitIfUpdateInProgressTests(unittest.TestCase):
    """Component contract: MCP-startup gate must exit cleanly when locked."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lockfile = Path(self._tmp.name) / update_gate.LOCKFILE_BASENAME

    def test_no_lockfile_does_not_exit(self) -> None:
        # Normal path: no lockfile, function returns without raising.
        update_gate.exit_if_update_in_progress(
            "weaviate-kg MCP", path=self.lockfile
        )

    def test_fresh_lockfile_exits_with_75(self) -> None:
        update_gate.write_lockfile(
            expected_duration_min=10, path=self.lockfile
        )
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            update_gate.exit_if_update_in_progress(
                "search MCP", path=self.lockfile, stream=buf
            )
        self.assertEqual(cm.exception.code, update_gate.EXIT_UPDATE_IN_PROGRESS)
        # Log message must mention the component + the exit code so the
        # user can diagnose from Claude Code logs.
        self.assertIn("search MCP", buf.getvalue())
        self.assertIn("75", buf.getvalue())

    def test_stale_lockfile_does_not_exit(self) -> None:
        # Stale lockfile must NOT trigger the MCP exit — boot-time
        # cleanup will clear it but in the meantime MCPs should proceed.
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
        self.lockfile.write_text(
            json.dumps(
                {
                    "started_at": past.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "started_by_pid": 1,
                    "phase": "git_pull",
                    "expected_completion_by": past.strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            )
        )
        # No exception expected.
        update_gate.exit_if_update_in_progress(
            "vct-coordination MCP", path=self.lockfile
        )


class LockfilePathTests(unittest.TestCase):
    """Lockfile location resolution honours VCT_STATE_DIR."""

    def test_lockfile_under_vct_root(self) -> None:
        from vco_lib.paths import vct_root_dir

        path = update_gate.lockfile_path()
        self.assertEqual(path.parent, vct_root_dir())
        self.assertEqual(path.name, update_gate.LOCKFILE_BASENAME)

    def test_lockfile_honours_vct_state_dir_env(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": tmp}):
                path = update_gate.lockfile_path()
                self.assertEqual(path.parent, Path(tmp))


class LifecycleIntegrationTests(unittest.TestCase):
    """End-to-end: simulate a full update cycle through the gate states."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lockfile = Path(self._tmp.name) / update_gate.LOCKFILE_BASENAME

    def test_full_update_lifecycle(self) -> None:
        # 0) Initially no update in progress.
        self.assertFalse(update_gate.is_update_in_progress(path=self.lockfile))

        # 1) Launcher writes lockfile pre-git-pull.
        update_gate.write_lockfile(phase="git_pull", path=self.lockfile)
        self.assertTrue(update_gate.is_update_in_progress(path=self.lockfile))

        # 2) install.py advances phase.
        update_gate.write_lockfile(phase="install_py", path=self.lockfile)
        self.assertTrue(update_gate.is_update_in_progress(path=self.lockfile))

        # 3) Binary refresh phase.
        update_gate.write_lockfile(
            phase="binary_refresh", path=self.lockfile
        )
        self.assertTrue(update_gate.is_update_in_progress(path=self.lockfile))

        # 4) Launcher deletes lockfile on success.
        self.assertTrue(update_gate.delete_lockfile(path=self.lockfile))
        self.assertFalse(update_gate.is_update_in_progress(path=self.lockfile))

    def test_crashed_update_recovers_via_stale_cleanup(self) -> None:
        # Simulate a launcher crash mid-update: lockfile present, but
        # never deleted because the launcher died before completion.
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=30)
        self.lockfile.write_text(
            json.dumps(
                {
                    "started_at": past.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "started_by_pid": 99999,
                    "phase": "git_pull",
                    "expected_completion_by": past.strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            )
        )
        # On next launcher boot, stale lockfile is cleaned up.
        self.assertTrue(update_gate.cleanup_if_stale(path=self.lockfile))
        # And MCP spawns proceed normally.
        self.assertFalse(update_gate.is_update_in_progress(path=self.lockfile))


if __name__ == "__main__":
    unittest.main()
