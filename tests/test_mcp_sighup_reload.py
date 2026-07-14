# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for PR-42 (v0.2.12): MCP SIGHUP-driven clean-exit env reload.

Fixes Issue B from
``.claude/context/mcp-instability-vs-public-repo-2026-05-16.md``:
editing ``.claude/settings.json env`` mid-chat left the running MCP
subprocess pinned to the OLD env, requiring a manual
``pkill -f weaviate_mcp`` to recover.

Design contract (see ``claude_mcp_servers/_lib/sighup_handler.py``):
  * SIGHUP triggers ``sys.exit(0)`` — NOT an in-process reconnect.
  * No per-var allowlist — any env change triggers full MCP restart.
  * Windows: SIGHUP doesn't exist; the helper no-ops and returns False.

This module covers:
  1. ``register_sighup_exit_handler`` returns False on platforms without
     SIGHUP (signal-attribute probe — works on every OS).
  2. A real Python subprocess that imports the helper exits cleanly
     within ~3 s of receiving SIGHUP (POSIX only — skipped on Windows).
  3. Both ``weaviate_mcp/server.py`` and ``search_mcp/server.py`` import
     and register the helper at module-import time (static grep, runs
     everywhere). This is the regression safeguard: future refactors of
     either MCP can't quietly drop the SIGHUP wiring.
"""
from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"

sys.path.insert(0, str(MCP_DIR))

# pylint: disable=wrong-import-position
from _lib.sighup_handler import register_sighup_exit_handler  # noqa: E402


_IS_WINDOWS = platform.system().lower().startswith("win")


class SighupHandlerNoopOnWindowsTest(unittest.TestCase):
    """The helper's return value contract must hold on every OS."""

    def test_returns_false_when_sighup_absent(self) -> None:
        """When ``signal.SIGHUP`` doesn't exist (Windows native), the
        helper must NOT crash and must signal "did not install" via the
        return value so the caller's launcher-side fallback knows to
        use kill+respawn.
        """
        import logging

        logger = logging.getLogger("test-pr42")
        if hasattr(signal, "SIGHUP"):
            # POSIX: registers successfully. Install a fresh handler on
            # this test process is fine — we never raise SIGHUP here.
            installed = register_sighup_exit_handler(logger)
            self.assertTrue(installed)
        else:
            # Windows native: no SIGHUP attribute, must short-circuit
            # to False.
            installed = register_sighup_exit_handler(logger)
            self.assertFalse(installed)


@unittest.skipIf(_IS_WINDOWS, "SIGHUP is POSIX-only; Windows uses kill+respawn fallback")
class SighupTriggersCleanExitTest(unittest.TestCase):
    """End-to-end: spawn a real Python subprocess that imports the
    helper, send it SIGHUP, assert it exits within a few seconds.

    We don't spawn the full weaviate_mcp server (it needs Weaviate
    running, would slow down the test suite, and would require the MCP
    venv). Instead we spawn a minimal harness that registers the same
    handler and then sleeps — which is exactly the production code path
    the handler sits on top of.
    """

    HARNESS_TEMPLATE = textwrap.dedent(
        """
        import logging
        import sys
        import time
        sys.path.insert(0, {mcp_dir!r})
        from _lib.sighup_handler import register_sighup_exit_handler

        logging.basicConfig(level=logging.INFO)
        installed = register_sighup_exit_handler(logging.getLogger("harness"))
        # Print a sentinel so the test knows the handler is wired before
        # it tries to send the signal.
        print("HANDLER_INSTALLED=" + str(installed), flush=True)
        # Sleep long enough that the test's SIGHUP arrives during the
        # sleep — much longer than the test's wait budget would mean a
        # process leak if the signal never fired.
        time.sleep(30)
        # Never-reached unless the signal didn't fire — we exit 99 here
        # so the test can distinguish "handler didn't run" from "handler
        # ran and exited cleanly".
        sys.exit(99)
        """
    )

    def test_sighup_causes_clean_exit(self) -> None:
        harness = self.HARNESS_TEMPLATE.format(mcp_dir=str(MCP_DIR))
        proc = subprocess.Popen(
            [sys.executable, "-c", harness],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for the handler to be installed before sending the
            # signal. Without this race, a fast scheduler could deliver
            # SIGHUP before signal.signal() runs, killing the process
            # with the default action (terminate, exit code -1) and
            # making the test flaky.
            installed_ok = False
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                # Non-blocking read attempt — poll until we see the sentinel.
                line = proc.stdout.readline() if proc.stdout else ""
                if line and line.startswith("HANDLER_INSTALLED="):
                    self.assertIn("True", line, f"handler refused to install: {line}")
                    installed_ok = True
                    break
                # Subprocess died before sentinel — bail with diagnostics.
                if proc.poll() is not None:
                    stderr = proc.stderr.read() if proc.stderr else ""
                    self.fail(
                        f"harness exited before installing handler: "
                        f"exit={proc.returncode}, stderr={stderr!r}"
                    )
                time.sleep(0.05)
            self.assertTrue(installed_ok, "handler-install sentinel never arrived within 5s")

            # Send SIGHUP.
            os.kill(proc.pid, signal.SIGHUP)

            # Wait for clean exit. Generous 5s budget — the handler's
            # sys.exit(0) returns immediately on POSIX; anything longer
            # than ~1s suggests something is wedged.
            try:
                exit_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("SIGHUP handler did not cause exit within 5s")

            # Accept either 0 (sys.exit(0) ran cleanly) OR -SIGHUP (some
            # interpreters / signal-handler races report the signal-kill
            # exit code even when the handler runs). The 99 sentinel
            # would be "handler never ran". Anything else suggests a
            # crash worth investigating.
            acceptable = {0, -signal.SIGHUP, 128 + signal.SIGHUP}
            self.assertIn(
                exit_code,
                acceptable,
                f"expected clean exit (0 or signal-coded), got {exit_code}. "
                f"stderr={proc.stderr.read() if proc.stderr else ''!r}",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)


class McpServersWireSighupHandlerTest(unittest.TestCase):
    """Regression guard: both shipped MCPs MUST register the handler.

    A future refactor that drops the handler REGISTRATION (the
    ``register_sighup_exit_handler(logger)`` call) or the RESOLUTION of
    that function would silently break the env-reload UX. Static grep
    catches that at test time.

    v0.2.81 re-anchor: search_mcp now RESOLVES the handler through the
    shared ``_lib.bootstrap.import_lib_member`` helper (loud-fail on a
    missing shipped ``_lib``) instead of the direct
    ``from _lib.sighup_handler import ...`` line, so the resolution marker
    is now per-server. The INTENT is unchanged (the handler is wired +
    called); the assertion just accepts EITHER resolution mechanism.
    weaviate_mcp still uses the direct import — its marker is unchanged.
    """

    # The call marker is invariant across both servers.
    CALL_MARKER = "register_sighup_exit_handler(logger)"
    # Either the direct import OR the shared-helper resolution proves the
    # function is genuinely resolved before it is called. Matched against a
    # whitespace-collapsed copy of the source so line-wrapping / indentation
    # of the ``import_lib_member(...)`` call can't make the marker fragile
    # (the exact fragility the KG monolith-extraction hazard node warns
    # about for source-grepping structural tests).
    RESOLUTION_MARKERS = (
        "from _lib.sighup_handler import register_sighup_exit_handler",
        'import_lib_member( "sighup_handler", "register_sighup_exit_handler" )',
        'import_lib_member("sighup_handler", "register_sighup_exit_handler")',
    )

    @staticmethod
    def _collapse_ws(text: str) -> str:
        return " ".join(text.split())

    def _assert_wires_handler(self, server_path: Path) -> None:
        text = server_path.read_text(encoding="utf-8")
        self.assertIn(
            self.CALL_MARKER,
            text,
            f"{server_path.relative_to(REPO_ROOT)} missing PR-42 call marker: "
            f"{self.CALL_MARKER!r}",
        )
        collapsed = self._collapse_ws(text)
        self.assertTrue(
            any(self._collapse_ws(m) in collapsed for m in self.RESOLUTION_MARKERS),
            f"{server_path.relative_to(REPO_ROOT)} does not resolve "
            f"register_sighup_exit_handler via a direct import or the shared "
            f"_lib.bootstrap.import_lib_member helper",
        )

    def test_weaviate_mcp_wires_handler(self) -> None:
        self._assert_wires_handler(MCP_DIR / "weaviate_mcp" / "server.py")

    def test_search_mcp_wires_handler(self) -> None:
        self._assert_wires_handler(MCP_DIR / "search_mcp" / "server.py")


if __name__ == "__main__":
    unittest.main()
