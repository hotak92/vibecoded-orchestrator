# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 — a VCO stdio MCP server can no longer hang alive after shutdown.

Field case: a weaviate-kg server stayed alive 54 h after a SIGHUP env reload,
every thread parked, its stdin unread; Claude Code's ``/mcp`` still said
"connected" and every KG call of that chat hung. Two layers, both in
``vco_lib/mcp_stdio_lifetime.py``:

1. the exit backstop — any shutdown path arms a daemon timer that hard-exits
   if the process is still alive a few seconds later;
2. the liveness heartbeat — ``<vct_root>/run/mcp/<pid>.json``, touched while
   the event loop runs; the hub's watchdog (``vct-hub/src/mcp_liveness.rs``,
   its Rust tests hold the decision) ends a server whose file went stale.

RED-PROOFS (run while writing this file):
* ``arm_exit_backstop`` returning ``False`` without arming →
  ``test_backstop_ends_a_shutdown_blocked_in_a_log_write`` and
  ``test_backstop_ends_a_server_that_swallowed_the_reload_exit`` time out
  with the server still running.
* ``Heartbeat.loop_is_live`` returning ``True`` unconditionally →
  ``test_heartbeat_stops_when_the_loop_stops`` and
  ``test_heartbeat_stops_when_the_loop_is_wedged_past_the_stall_bound`` fail.
* ``_finish`` without ``_stop_heartbeat()`` →
  ``test_heartbeat_file_is_written_by_the_server_and_removed_on_exit`` fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import mcp_stdio_lifetime as lt

REPO = Path(__file__).resolve().parents[1]
RUST = REPO / "launcher/src-tauri/vct-hub/src/mcp_liveness.rs"
RUST_HEARTBEAT = REPO / "launcher/src-tauri/vct-launcher-core/src/mcp_heartbeat.rs"
RUST_CORE = REPO / "launcher/src-tauri/vct-launcher-core/src/process.rs"

def _write_server(tmp_path: Path, body: str) -> Path:
    """The server lives in a FILE (``<tmp>/fake_mcp/server.py``), as a real
    one does — the heartbeat marker is read from ``argv[0]``."""
    d = tmp_path / "fake_mcp"
    d.mkdir()
    script = d / "server.py"
    script.write_text(textwrap.dedent(body).replace("{repo}", repr(str(REPO))))
    return script


# A stdio read loop that ends on stdin EOF — FastMCP's run_stdio_async shape
# (the SDK, too, reads stdin on a worker thread). A thread read rather than
# ``connect_read_pipe`` so the same server runs on Windows.
_READ_LOOP = """
        async def serve():
            loop = asyncio.get_running_loop()
            print("ready", flush=True)
            while await loop.run_in_executor(None, sys.stdin.buffer.readline):
                pass
"""


def test_backstop_ends_a_shutdown_blocked_in_a_log_write(tmp_path):
    """stdin EOF starts the shutdown, but ``_finish``'s first log write
    blocks (a stderr pipe the client stopped draining), and a non-daemon
    thread is parked for good. Without the backstop the process never
    exits."""
    script = _write_server(tmp_path, """
        import asyncio, logging, sys, threading
        from pathlib import Path
        sys.path.insert(0, {repo})
        from vco_lib import mcp_stdio_lifetime as lt
        lt.EXIT_BACKSTOP_SECONDS = 2.0
        never = threading.Event()

        class Stuck(logging.Handler):
            def emit(self, record):
                never.wait()

        logging.getLogger().addHandler(Stuck())
        threading.Thread(target=never.wait, daemon=False).start()
    """ + _READ_LOOP + """
        lt.run_stdio_server(serve, name="t", grace_s=0.5, watchdog_interval_s=60,
                            heartbeat_directory=Path(sys.argv[1]))
    """)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "hb")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=child_env(),
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert proc.stdout.readline().strip() == b"ready"
        t0 = time.monotonic()
        proc.stdin.close()
        proc.wait(timeout=15)
        assert proc.returncode == 0
        assert time.monotonic() - t0 < 12
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signal")
def test_backstop_ends_a_server_that_swallowed_the_reload_exit(tmp_path):
    """The env-reload SIGHUP lands while code with a broad ``except
    BaseException`` runs and the ``SystemExit`` is swallowed: the server
    would serve on with a reload that never happened. The handler armed the
    backstop before raising, so the process still ends."""
    script = _write_server(tmp_path, """
        import asyncio, logging, sys, time
        from pathlib import Path
        sys.path.insert(0, {repo})
        sys.path.insert(0, {repo} + "/claude_mcp_servers")
        from vco_lib import mcp_stdio_lifetime as lt
        from _lib.sighup_handler import register_sighup_exit_handler
        lt.EXIT_BACKSTOP_SECONDS = 2.0
        assert register_sighup_exit_handler(logging.getLogger("t")) is True

        async def serve():
            # A synchronous tool call on the loop with a broad except: the
            # handler's SystemExit is raised inside it, and swallowed.
            print("ready", flush=True)
            while True:
                try:
                    time.sleep(0.05)
                except BaseException:  # the swallow under test
                    pass

        lt.run_stdio_server(serve, name="t", grace_s=0.5, watchdog_interval_s=60,
                            heartbeat_directory=Path(sys.argv[1]))
    """)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "hb")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_env(),
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == b"ready"
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_heartbeat_file_is_written_by_the_server_and_removed_on_exit(tmp_path):
    hb_dir = tmp_path / "hb"
    script = _write_server(tmp_path, """
        import asyncio, sys
        from pathlib import Path
        sys.path.insert(0, {repo})
        from vco_lib import mcp_stdio_lifetime as lt
    """ + _READ_LOOP + """
        lt.run_stdio_server(serve, name="fake-kg", grace_s=0.5, watchdog_interval_s=60,
                            heartbeat_directory=Path(sys.argv[1]))
    """)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(hb_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_env(),
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert proc.stdout.readline().strip() == b"ready"
        path = hb_dir / f"{proc.pid}.json"
        rec = json.loads(path.read_text())
        from vco_lib.deferral_probes import process_start_token

        assert rec["pid"] == proc.pid
        assert rec["server"] == "fake-kg"
        assert rec["marker"] == "fake_mcp/server.py"
        assert rec["start_token"] == process_start_token(proc.pid)
        assert isinstance(rec["started_at"], float)
        if sys.platform != "win32":
            assert (path.stat().st_mode & 0o777) == 0o600
        proc.stdin.close()
        proc.wait(timeout=15)
        assert proc.returncode == 0
        assert not path.exists(), "a clean exit removes its heartbeat file"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ── the heartbeat's two sources, in-process ────────────────────────────


class _LoopThread:
    """An event loop running on its own thread, like the server's."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    def close(self) -> None:
        if self.loop.is_running():
            self.stop()
        self.loop.close()


def _age_it(path: Path) -> None:
    os.utime(path, (1_000_000, 1_000_000))


def _refreshed(path: Path, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.stat().st_mtime > 2_000_000:
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def hb_loop(tmp_path):
    lt_ = _LoopThread()
    hb = lt.Heartbeat("t", lt_.loop, marker="fake_mcp/server.py", directory=tmp_path,
                      interval_s=0.05, stall_s=0.5)
    asyncio.run_coroutine_threadsafe(hb.loop_tick(), lt_.loop)
    hb.start()
    yield hb, lt_
    hb.stop()
    lt_.close()


def test_heartbeat_is_touched_while_the_loop_runs(hb_loop):
    hb, _ = hb_loop
    _age_it(hb.path)
    assert _refreshed(hb.path, 2.0)


def test_heartbeat_keeps_beating_through_a_long_call_below_the_stall_bound(tmp_path):
    """A tool call holding the loop (synchronous backend I/O) is not a hang:
    the beat continues while the loop's own tick is younger than stall_s."""
    lt_ = _LoopThread()
    hb = lt.Heartbeat("t", lt_.loop, marker="m/s.py", directory=tmp_path,
                      interval_s=0.05, stall_s=30.0)
    asyncio.run_coroutine_threadsafe(hb.loop_tick(), lt_.loop)
    hb.start()
    try:
        time.sleep(0.1)
        lt_.loop.call_soon_threadsafe(time.sleep, 1.0)  # the long call
        time.sleep(0.2)
        _age_it(hb.path)
        assert _refreshed(hb.path, 0.6), "beat stopped during a legitimate call"
    finally:
        hb.stop()
        lt_.close()


def test_heartbeat_stops_when_the_loop_is_wedged_past_the_stall_bound(hb_loop):
    hb, lt_ = hb_loop
    lt_.loop.call_soon_threadsafe(time.sleep, 2.0)  # wedged: 4x stall_s
    time.sleep(0.7)  # past stall_s
    _age_it(hb.path)
    assert not _refreshed(hb.path, 0.8), "a wedged loop must stop the beat"


def test_heartbeat_stops_when_the_loop_stops(hb_loop):
    """The 54 h field case: the loop has ended, the process lives on."""
    hb, lt_ = hb_loop
    lt_.stop()
    _age_it(hb.path)
    assert not _refreshed(hb.path, 0.5)


def test_heartbeat_recreates_a_removed_record_and_stop_removes_it(hb_loop):
    hb, _ = hb_loop
    hb.path.unlink()
    deadline = time.monotonic() + 2
    while not hb.path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert hb.path.exists()
    hb.stop()
    assert not hb.path.exists()


def test_marker_is_the_scripts_last_two_components_or_nothing(tmp_path):
    script = tmp_path / "weaviate_mcp" / "server.py"
    script.parent.mkdir()
    script.write_text("")
    assert lt.heartbeat_marker(str(script)) == "weaviate_mcp/server.py"
    assert lt.heartbeat_marker("-c") is None
    assert lt.heartbeat_marker("") is None


def test_no_marker_means_no_heartbeat_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lt.sys, "argv", ["-c"])
    loop = asyncio.new_event_loop()
    try:
        assert lt._start_heartbeat("t", loop, tmp_path) is None
        assert list(tmp_path.iterdir()) == []
    finally:
        loop.close()


def test_heartbeat_dir_follows_the_vct_state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    assert lt.heartbeat_dir() == tmp_path / "run" / "mcp"


# ── backstop units ─────────────────────────────────────────────────────


def test_backstop_fires_once_with_the_first_code(monkeypatch):
    monkeypatch.setattr(lt, "_backstop", None)
    fired = []
    assert lt.arm_exit_backstop("x", 3, delay_s=0.05, _exit=fired.append) is True
    assert lt.arm_exit_backstop("x", 9, delay_s=0.0, _exit=fired.append) is False
    time.sleep(0.4)
    assert fired == [3]


def test_finish_arms_the_backstop_before_anything_else(monkeypatch):
    monkeypatch.setattr(lt, "_backstop", None)
    order = []
    monkeypatch.setattr(lt, "arm_exit_backstop",
                        lambda name, code, **kw: order.append(("arm", code)))
    monkeypatch.setattr(lt, "_stop_heartbeat", lambda: order.append("hb"))
    monkeypatch.setattr(lt.atexit, "_run_exitfuncs", lambda: order.append("atexit"))
    lt._finish("x", "test", 4, _exit=lambda code: order.append(("exit", code)))
    assert order == [("arm", 4), "hb", "atexit", ("exit", 4)]


# ── parity with the hub (the reader) ───────────────────────────────────


def test_the_hub_reads_the_record_this_module_writes():
    src = RUST.read_text(encoding="utf-8")
    struct = src[src.index("pub struct McpRecord"):]
    struct = struct[:struct.index("\n}\n")]
    fields = re.findall(r"pub (\w+):", struct)
    assert sorted(fields) == sorted(["pid", "start_token", "server", "marker", "started_at"])
    # The location and the stale bound live in ONE home the hub re-exports
    # and the launcher's boot orphan reaper also reads (v0.2.108).
    assert "pub use vct_launcher_core::mcp_heartbeat::{heartbeat_dir, STALE_SECS};" in src
    home = RUST_HEARTBEAT.read_text(encoding="utf-8")
    assert 'join("run").join("mcp")' in home
    assert "MUST MATCH\n/// `vco_lib/mcp_stdio_lifetime.py::heartbeat_dir`" in home
    # The stale bound is well above the beat interval.
    stale = int(re.search(r"pub const STALE_SECS: u64 = (\d+);", home).group(1))
    assert stale >= 10 * lt.HEARTBEAT_SECONDS


def test_the_rust_token_verdict_mirrors_the_python_rule():
    """``start_token_verdict`` (vct-launcher-core) mirrors
    ``deferral_probes.process_identity_verdict``; its Rust test holds the
    same table as this one."""
    from vco_lib import deferral_probes as dp

    src = RUST_CORE.read_text(encoding="utf-8")
    assert "MUST MATCH `vco_lib/deferral_probes.py::process_identity_verdict`" in src
    m = re.search(r"pub const WALL_REUSED_MIN_DELTA_SECS: f64 = ([\d.]+);", src)
    assert m and float(m.group(1)) == dp._WALL_REUSED_MIN_DELTA_S
    table = [
        ("proc:10", "proc:10", dp.IDENTITY_SAME),
        ("proc:10", "proc:11", dp.IDENTITY_REUSED),
        ("wall:100.000", "wall:100.900", dp.IDENTITY_SAME),
        ("wall:100.000", "wall:102.000", dp.IDENTITY_BAND),
        ("wall:100.000", "wall:103.000", dp.IDENTITY_REUSED),
        ("proc:10", "wall:10.000", dp.IDENTITY_UNKNOWN),
    ]
    for recorded, current, want in table:
        orig = dp.process_start_token
        try:
            dp.process_start_token = lambda pid, _c=current: _c  # type: ignore[assignment]
            assert dp.process_identity_verdict(1, recorded) == want, (recorded, current)
        finally:
            dp.process_start_token = orig  # type: ignore[assignment]
