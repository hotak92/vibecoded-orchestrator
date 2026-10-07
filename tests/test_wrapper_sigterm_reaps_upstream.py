# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A SIGTERM to a wrapper MCP must take its upstream down with it (v0.2.102).

The launcher's pre-update MCP kill sweep matches the wrapper
(``python -m claude_mcp_servers.wrappers.*``) but not the upstream it hosts
(e.g. the excalidraw node server). Before v0.2.102 a SIGTERM took Python's
default action: the wrapper died without running ``WrapperMCP.run``'s
reaping ``finally`` and the upstream was orphaned, surviving until it
happened to notice stdin EOF.

These tests run a real wrapper process around a dummy upstream that never
reads stdin (so stdin EOF cannot be what stops it), SIGTERM the wrapper,
and assert the upstream is gone — plus the leave-alone arm: without a
signal, the upstream keeps running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent

# The wrapper process: a bare WrapperMCP around a dummy upstream that writes
# its pid and sleeps, ignoring stdin entirely.
_WRAPPER_SCRIPT = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from claude_mcp_servers.wrappers._base import WrapperMCP

pidfile = sys.argv[2]
ignore_term = len(sys.argv) > 3 and sys.argv[3] == "ignore-term"
upstream = [
    sys.executable, "-c",
    ("import os, signal, time, sys; "
     + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
     + "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(300)"),
    pidfile,
]
asyncio.run(WrapperMCP("w1b-sigterm-test", upstream).run())
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@unittest.skipIf(sys.platform == "win32", "POSIX SIGTERM path (Windows tree-kills instead)")
class WrapperSigtermReapsUpstreamTests(unittest.TestCase):
    #: extra wrapper-script argv (subclasses switch the upstream's behaviour)
    upstream_mode: list[str] = []

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pidfile = Path(self._tmp.name) / "upstream.pid"
        self.upstream_pid: int | None = None
        self.wrapper = subprocess.Popen(
            [sys.executable, "-c", _WRAPPER_SCRIPT, str(REPO_ROOT), str(self.pidfile), *self.upstream_mode],
            env=child_env(),
            stdin=subprocess.PIPE,  # held open: no stdin EOF during the test
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._cleanup)
        self.assertTrue(
            _wait(lambda: self.pidfile.exists() and self.pidfile.read_text().strip().isdigit()),
            "the wrapper never started its upstream",
        )
        self.upstream_pid = int(self.pidfile.read_text().strip())

    def _cleanup(self) -> None:
        if self.wrapper.poll() is None:
            self.wrapper.kill()
        self.wrapper.wait(timeout=10)
        if self.wrapper.stdin is not None:
            self.wrapper.stdin.close()
        if self.upstream_pid is not None and _pid_alive(self.upstream_pid):
            os.kill(self.upstream_pid, signal.SIGKILL)

    def test_sigterm_to_wrapper_terminates_the_upstream(self) -> None:
        assert self.upstream_pid is not None
        upstream_pid = self.upstream_pid
        self.assertTrue(_pid_alive(upstream_pid))

        self.wrapper.send_signal(signal.SIGTERM)

        rc = self.wrapper.wait(timeout=15)
        self.assertTrue(
            _wait(lambda: not _pid_alive(upstream_pid)),
            "the upstream survived a SIGTERM to its wrapper (orphaned)",
        )
        self.assertEqual(rc, 0, "the wrapper should shut down cleanly on SIGTERM")

    def test_without_a_signal_the_upstream_keeps_running(self) -> None:
        assert self.upstream_pid is not None
        time.sleep(0.5)
        self.assertIsNone(self.wrapper.poll())
        self.assertTrue(_pid_alive(self.upstream_pid))


@unittest.skipIf(sys.platform == "win32", "POSIX SIGTERM path (Windows tree-kills instead)")
class SecondSigtermDuringReapTests(WrapperSigtermReapsUpstreamTests):
    """A second SIGTERM arriving while the wrapper is reaping must not kill the
    wrapper mid-reap (default action) and orphan the upstream. The upstream
    here IGNORES SIGTERM, so the reap sits in its 3 s terminate→kill window
    when the second signal lands; the wrapper must still escalate to SIGKILL."""

    upstream_mode = ["ignore-term"]

    def test_sigterm_to_wrapper_terminates_the_upstream(self) -> None:
        assert self.upstream_pid is not None
        upstream_pid = self.upstream_pid

        self.wrapper.send_signal(signal.SIGTERM)
        time.sleep(0.8)  # inside the reap's terminate→kill window
        self.assertIsNone(self.wrapper.poll(), "wrapper should still be reaping")
        self.wrapper.send_signal(signal.SIGTERM)

        rc = self.wrapper.wait(timeout=15)
        self.assertTrue(
            _wait(lambda: not _pid_alive(upstream_pid)),
            "a second SIGTERM mid-reap orphaned the upstream",
        )
        self.assertEqual(rc, 0, "the wrapper should finish its reap and exit cleanly")


if __name__ == "__main__":
    unittest.main()
