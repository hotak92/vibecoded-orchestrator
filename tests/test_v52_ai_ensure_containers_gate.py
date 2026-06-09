# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AI — ensure-containers hook update-gate integration tests.

The ``templates/hooks/ensure-containers.sh`` (and its .ps1 sibling) read
the same ``.update-in-progress.json`` lockfile that the MCP servers
check at startup. When the lockfile is fresh, the hook exits early
with status 0 so it doesn't race the launcher's binary refresh by
trying to start containers mid-update.

These tests invoke the bash hook against a controlled VCT_STATE_DIR
(empty / fresh-lockfile / stale-lockfile) and assert the exit behaviour.
We stub out everything after the lockfile check so the hook exits
quickly — the goal is to pin the gate, not the container logic.

PowerShell parity is a static check (assert .ps1 contains the same
phrase) since spawning PowerShell from a Linux pytest run is unreliable.

See v0.2.52 backlog § V52-AI.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _has_bash() -> bool:
    return shutil.which("bash") is not None and shutil.which("python3") is not None


@unittest.skipUnless(_has_bash(), "requires bash + python3 on PATH")
class EnsureContainersHookGateTests(unittest.TestCase):
    """The ensure-containers.sh hook must honour the lockfile."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.lockfile = self.state_dir / ".update-in-progress.json"

        # Extract just the gate prologue from the hook so we can test
        # IN ISOLATION without invoking the full container startup
        # machinery (which would need podman, _lib/stderr-cap.sh, etc).
        hook_path = PROJECT_ROOT / "templates" / "hooks" / "ensure-containers.sh"
        full_hook = hook_path.read_text(encoding="utf-8")
        # The gate prologue ends with `unset __vct_root_dir ...`.
        gate_end_marker = "unset __vct_root_dir __vct_update_lockfile __still_fresh"
        gate_end = full_hook.find(gate_end_marker)
        self.assertNotEqual(
            gate_end, -1, "gate prologue marker missing from ensure-containers.sh"
        )
        gate_end = full_hook.find("\n", gate_end) + 1
        self.gate_prologue = full_hook[:gate_end] + 'echo "PROCEED"\nexit 99\n'

    def _run_with_lockfile(self, payload: dict | None) -> int:
        """Spawn bash with the prologue + a marker echo, return exit code."""
        if payload is not None:
            self.lockfile.write_text(json.dumps(payload))
        else:
            if self.lockfile.exists():
                self.lockfile.unlink()
        env = {**os.environ, "VCT_STATE_DIR": str(self.state_dir)}
        proc = subprocess.run(
            ["bash", "-c", self.gate_prologue],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode

    def test_no_lockfile_proceeds(self) -> None:
        # Exit 99 == reached the post-gate `exit 99` => gate let us pass.
        rc = self._run_with_lockfile(None)
        self.assertEqual(rc, 99, "no lockfile should let the hook proceed")

    def test_fresh_lockfile_skips(self) -> None:
        future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=10)
        rc = self._run_with_lockfile(
            {
                "started_at": "2026-06-09T10:00:00Z",
                "started_by_pid": 1,
                "phase": "git_pull",
                "expected_completion_by": future.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        )
        # Exit 0 == hit the `exit 0` inside the gate => skipped startup.
        self.assertEqual(rc, 0, "fresh lockfile should skip container startup")

    def test_stale_lockfile_proceeds(self) -> None:
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
        rc = self._run_with_lockfile(
            {
                "started_at": past.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "started_by_pid": 1,
                "phase": "git_pull",
                "expected_completion_by": past.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        self.assertEqual(
            rc, 99, "stale lockfile should let the hook proceed"
        )

    def test_corrupt_lockfile_proceeds(self) -> None:
        # Soft-fail: malformed JSON treated as "no active update".
        self.lockfile.write_text("{ not valid json", encoding="utf-8")
        env = {**os.environ, "VCT_STATE_DIR": str(self.state_dir)}
        proc = subprocess.run(
            ["bash", "-c", self.gate_prologue],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode, 99,
            "corrupt lockfile should NOT block container startup",
        )


class PowerShellSiblingParityTests(unittest.TestCase):
    """Static parity: the .ps1 sibling must implement the same gate."""

    def test_ps1_has_lockfile_gate(self) -> None:
        ps1 = (
            PROJECT_ROOT / "templates" / "hooks" / "ensure-containers.ps1"
        ).read_text(encoding="utf-8")
        # Same lockfile basename + same env var name as the .sh sibling.
        self.assertIn(".update-in-progress.json", ps1)
        self.assertIn("VCT_STATE_DIR", ps1)
        # Same skip message so users see the same diagnostic on both OSes.
        self.assertIn(
            "orchestrator update in progress", ps1,
            "ensure-containers.ps1 must surface the same diagnostic as the "
            "bash sibling so users can grep their logs consistently",
        )

    def test_sh_has_lockfile_gate(self) -> None:
        sh = (
            PROJECT_ROOT / "templates" / "hooks" / "ensure-containers.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(".update-in-progress.json", sh)
        self.assertIn("VCT_STATE_DIR", sh)
        self.assertIn("orchestrator update in progress", sh)


if __name__ == "__main__":
    unittest.main()
