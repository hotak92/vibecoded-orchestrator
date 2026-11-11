# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the weaviate-kg MCP process ends with its client.

Field report (a Windows user, RAM saturated): MCP servers that missed Claude
Code's 30 s connect timeout stayed alive after the harness gave up on them.
For VCO's own server two shapes kept a client-less process running:

* stdin EOF reached the SDK's stdio loop, but ``asyncio.run`` then waited for
  the thread-pool executor and interpreter exit joined its threads — a tool
  call blocked in a slow backend request kept the process alive for the whole
  request;
* the spawning process died (or, on Windows, only the venv launcher was
  killed) without the pipe reaching EOF — nothing noticed at all.

RED-PROOF: with the pre-fix serving shape (``asyncio.run(serve())``, no
watchdog) ``test_stdin_eof_exits_even_with_stuck_tool_work`` and
``test_parent_death_ends_the_server_without_eof`` time out with the server
still running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from vco_lib import mcp_stdio_lifetime as lt

REPO = Path(__file__).resolve().parents[1]

# A stand-in stdio server: one stuck "tool call" in the default executor (a
# backend request that will not return for 10 minutes) and a read loop that
# ends on stdin EOF — the shape of FastMCP's run_stdio_async.
_SERVER = textwrap.dedent(
    """
    import asyncio, sys, time
    sys.path.insert(0, {repo!r})
    from vco_lib.mcp_stdio_lifetime import run_stdio_server

    async def serve():
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, time.sleep, 600)
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        print("ready", flush=True)
        while await reader.read(4096):
            pass

    run_stdio_server(serve, name="test-mcp", grace_s=1.0,
                     watchdog_interval_s=0.2)
    """
)


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # A zombie still answers kill(0); read its state where we can.
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            if stat.split(")")[-1].split()[0] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process control")
def test_stdin_eof_exits_even_with_stuck_tool_work():
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER.format(repo=str(REPO))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert proc.stdout.readline().strip() == b"ready"
        proc.stdin.close()  # the client is gone
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process control")
def test_parent_death_ends_the_server_without_eof():
    """The spawning process is killed while the server's stdin stays OPEN
    (the write end is held here, the way an abandoned pipe can stay open):
    only the parent watchdog can end it."""
    r, w = os.pipe()
    middle = textwrap.dedent(
        """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", sys.argv[1]],
                                 stdin=int(sys.argv[2]), stdout=subprocess.PIPE,
                                 pass_fds=(int(sys.argv[2]),))
        assert child.stdout.readline().strip() == b"ready"
        print(child.pid, flush=True)
        time.sleep(600)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", middle, _SERVER.format(repo=str(REPO)), str(r)],
        stdout=subprocess.PIPE, pass_fds=(r,),
    )
    server_pid = None
    try:
        assert proc.stdout is not None
        server_pid = int(proc.stdout.readline().strip())
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert _wait_gone(server_pid, 15), "server outlived its parent"
    finally:
        os.close(r)
        os.close(w)
        if proc.poll() is None:
            proc.kill()
        if server_pid is not None:
            try:
                os.kill(server_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ── units ───────────────────────────────────────────────────────────────


def test_parent_watch_posix_reads_a_reparent():
    ppids = iter([100, 100, 1])
    w = lt.ParentWatch(_getppid=lambda: next(ppids), _platform="linux")
    assert w.initial_ppid == 100
    assert w.parent_gone() is False
    assert w.parent_gone() is True


def test_parent_watch_without_a_windows_handle_never_says_gone():
    # On a platform where the handle cannot be opened the answer is the
    # conservative one: alive.
    w = lt.ParentWatch(_getppid=lambda: 4242, _platform="win32")
    if sys.platform != "win32":
        assert w.parent_gone() is False


def test_parent_watch_without_a_parent_id_never_says_gone():
    def boom():
        raise OSError("no ppid")

    w = lt.ParentWatch(_getppid=boom, _platform="linux")
    assert w.initial_ppid is None
    assert w.parent_gone() is False
    assert lt.start_parent_watchdog("x", watch=w) is None


def test_watchdog_fires_once_when_the_parent_goes():
    ppids = iter([10] + [10] * 2 + [1] * 50)
    w = lt.ParentWatch(_getppid=lambda: next(ppids), _platform="linux")
    fired = []
    t = lt.start_parent_watchdog("x", interval_s=0.01, watch=w,
                                 on_gone=lambda: fired.append(1))
    assert t is not None
    t.join(timeout=5)
    assert fired == [1]


def test_finish_runs_exit_handlers_then_exits_with_the_code(monkeypatch):
    order = []
    monkeypatch.setattr(lt.atexit, "_run_exitfuncs", lambda: order.append("atexit"))
    lt._finish("x", "test", 3, _exit=lambda code: order.append(("exit", code)))
    assert order == ["atexit", ("exit", 3)]


def test_run_stdio_server_reports_the_eof_and_a_crash():
    calls = []

    async def ok():
        return None

    async def bad():
        raise RuntimeError("boom")

    lt.run_stdio_server(ok, name="x", grace_s=0.1, watchdog_interval_s=60,
                        _finish_fn=lambda n, r, c: calls.append((r, c)))
    lt.run_stdio_server(bad, name="x", grace_s=0.1, watchdog_interval_s=60,
                        _finish_fn=lambda n, r, c: calls.append((r, c)))
    assert calls[0][1] == 0 and "stdin closed" in calls[0][0]
    assert calls[1] == ("the stdio server loop failed", 1)


def test_the_weaviate_server_serves_through_the_lifetime_helper():
    """Wiring: the server's __main__ block hands FastMCP's stdio coroutine to
    ``run_stdio_server`` instead of ``asyncio.run``."""
    src = (REPO / "claude_mcp_servers/weaviate_mcp/server.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert "run_stdio_server(mcp.run_stdio_async" in main
    assert "asyncio.run(mcp.run_stdio_async())" not in main
