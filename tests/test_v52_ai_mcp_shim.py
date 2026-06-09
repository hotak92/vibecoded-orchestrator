# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AI — MCP-side update_gate shim tests.

The shim at ``claude_mcp_servers/_lib/update_gate.py`` is a deliberate
duplicate of :mod:`vco_lib.update_gate` because MCPs ship with a
self-contained venv and we don't want to hard-depend on the orchestrator
clone's path. These tests pin the shim against the schema produced by
the canonical vco_lib helpers — both sides MUST agree.

See v0.2.52 backlog § V52-AI.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "claude_mcp_servers"))


class ShimParityTests(unittest.TestCase):
    """The MCP shim must agree with vco_lib on schema + exit code."""

    def test_exit_code_matches(self) -> None:
        from vco_lib import update_gate as canonical
        from _lib import update_gate as shim

        self.assertEqual(
            canonical.EXIT_UPDATE_IN_PROGRESS, shim.EXIT_UPDATE_IN_PROGRESS
        )

    def test_lockfile_basename_matches(self) -> None:
        from vco_lib import update_gate as canonical
        from _lib import update_gate as shim

        self.assertEqual(canonical.LOCKFILE_BASENAME, shim.LOCKFILE_BASENAME)

    def test_shim_reads_canonical_lockfile(self) -> None:
        """Lockfile written by vco_lib.update_gate.write_lockfile is
        readable by the MCP-side shim's is_update_in_progress."""
        from vco_lib import update_gate as canonical

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["VCT_STATE_DIR"] = tmp
            try:
                # Re-import the shim to pick up the env var fresh.
                from _lib import update_gate as shim
                importlib.reload(shim)

                # No lockfile → False on both.
                self.assertFalse(canonical.is_update_in_progress())
                self.assertFalse(shim.is_update_in_progress())

                # Write via canonical, read via shim → True.
                canonical.write_lockfile(
                    phase="git_pull", expected_duration_min=15
                )
                self.assertTrue(canonical.is_update_in_progress())
                self.assertTrue(shim.is_update_in_progress())

                # Delete via canonical, both report False.
                canonical.delete_lockfile()
                self.assertFalse(canonical.is_update_in_progress())
                self.assertFalse(shim.is_update_in_progress())
            finally:
                os.environ.pop("VCT_STATE_DIR", None)


class MCPServerGateIntegrationTests(unittest.TestCase):
    """Smoke-test that each shipped MCP server actually invokes the gate.

    Rather than importing the server module (which would also try to
    connect to Weaviate / Ollama / etc), we grep the source for the
    expected pattern. Cheap, fast, catches accidental regressions where
    a contributor removes the gate without realising why it's there.
    """

    def _assert_server_calls_gate(self, rel_path: str, component_label: str) -> None:
        p = PROJECT_ROOT / rel_path
        src = p.read_text(encoding="utf-8")
        self.assertIn("update_gate", src, f"{rel_path} missing update_gate import")
        self.assertIn(
            "exit_if_update_in_progress", src,
            f"{rel_path} missing exit_if_update_in_progress() call",
        )
        self.assertIn(
            component_label, src,
            f"{rel_path} should pass '{component_label}' as the component name "
            f"so Claude Code logs identify the source clearly",
        )

    def test_weaviate_mcp_calls_gate(self) -> None:
        self._assert_server_calls_gate(
            "claude_mcp_servers/weaviate_mcp/server.py",
            "weaviate-kg MCP",
        )

    def test_search_mcp_calls_gate(self) -> None:
        self._assert_server_calls_gate(
            "claude_mcp_servers/search_mcp/server.py",
            "search MCP",
        )

    def test_code_embedding_service_calls_gate(self) -> None:
        self._assert_server_calls_gate(
            "claude_mcp_servers/code_embedding_service/server.py",
            "code-embedding service",
        )


class ShimSubprocessExitCodeTests(unittest.TestCase):
    """End-to-end: a tiny script that calls the shim must exit with 75
    when the lockfile is fresh, and 0 when absent."""

    def _run_script(self, env: dict) -> int:
        script = (
            "import sys, os\n"
            f"sys.path.insert(0, {str(PROJECT_ROOT / 'claude_mcp_servers')!r})\n"
            "from _lib.update_gate import exit_if_update_in_progress\n"
            "exit_if_update_in_progress('test MCP')\n"
            "sys.exit(0)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        return completed.returncode

    def test_no_lockfile_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run_script({"VCT_STATE_DIR": tmp})
            self.assertEqual(rc, 0)

    def test_fresh_lockfile_exits_75(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".update-in-progress.json"
            future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(
                minutes=10
            )
            lock.write_text(
                json.dumps(
                    {
                        "started_at": "2026-06-09T10:00:00Z",
                        "started_by_pid": 1,
                        "phase": "git_pull",
                        "expected_completion_by": future.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    }
                )
            )
            rc = self._run_script({"VCT_STATE_DIR": tmp})
            self.assertEqual(rc, 75)

    def test_stale_lockfile_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".update-in-progress.json"
            past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
            lock.write_text(
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
            rc = self._run_script({"VCT_STATE_DIR": tmp})
            # Stale lockfile = no active update; MCP proceeds (exit 0).
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
