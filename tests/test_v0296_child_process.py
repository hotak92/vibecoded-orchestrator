# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-1: tests for ``vco_lib.child_process.run_child_logged`` and the
two install.py seed spawn sites it replaces (per-project sync + shared-KG
seed — the inherited-stdio children that deadlocked the 2026-09-20 update
when the launcher's reader stopped draining the pipes).

Test map (plan WP-1):
* (a) unit — log redirect + argv header; TTY-tee gated on isatty; relay
  forwards ONLY ``^[VCO-EVENT]`` lines and survives a parent-stdout write
  error; ``subprocess.run`` semantics (check=, FileNotFoundError).
* (b) integration — a child emitting 1 MB to stderr completes while the
  parent's stdout/stderr are deliberately undrained ``os.pipe()`` fds
  (the old launcher reader shape). A timeout here IS the failure: with
  inherited stdio the child blocks on the ~64 KB pipe buffer forever.
* (c) the parent's stdout receives relayed ``[VCO-EVENT]`` lines WHILE
  the child floods stderr (mirroring not starved — one merged stream).
* install.py pin — both seed sites call the helper, no legacy
  ``subprocess.run`` spawn of ``sync_kg`` remains.
* (d) the ratchet stays green in its own file (tests/
  test_install_main_ratchet.py) — nothing to re-pin: this WP only
  shrank install.py.
* (e) ship-gate MAJOR-1: the relay fed by the REAL producer
  (sync_knowledge_graph.py's emitter) resets a watchdog-shaped
  consumer — the existing relay tests used hand-printed event lines
  (a fictional emitter); these load the shipped script in a real
  child and run its actual emitter, while a Python mirror of
  update_pipeline.rs's stall watchdog watches the relayed lines. The
  silent leg proves the other half: a child that goes quiet still
  trips it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402
from vco_lib.child_process import run_child_logged  # noqa: E402

INSTALL_PY = REPO_ROOT / "install.py"


class _FakeStdout:
    """Stands in for ``sys.stdout``: records writes, fakes the TTY flag,
    and can fail every write/flush to simulate a slow or broken reader."""

    def __init__(self, *, tty: bool, fail_writes: bool = False) -> None:
        self.lines: List[str] = []
        self._tty = tty
        self._fail_writes = fail_writes

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        if self._fail_writes:
            raise OSError("simulated slow/broken parent stdout")
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        if self._fail_writes:
            raise OSError("simulated flush failure")


def _run_py(code: str, **kwargs):
    return run_child_logged([sys.executable, "-c", code], **kwargs)


# ── (a) unit: redirect + header + TTY gate + relay shape ────────────────────


def test_output_lands_in_log_file_with_argv_header(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    fake = _FakeStdout(tty=False)
    monkeypatch.setattr(sys, "stdout", fake)

    code = (
        "print('wp1-out-line')\n"
        "import sys; sys.stderr.write('wp1-err-line\\n')\n"
    )
    result = _run_py(code, log_stem="unit")

    assert result.returncode == 0
    # Uncaptured semantics, exactly like the previous bare subprocess.run.
    assert result.stdout is None and result.stderr is None
    logs = list((tmp_path / "logs").glob("unit-*.log"))
    assert len(logs) == 1, "one log per run"
    data = logs[0].read_bytes()
    assert b"# argv:" in data, "the header names the argv (2026-09-09 lesson)"
    assert b"-c" in data and b"wp1-out-line" in data
    assert b"wp1-err-line" in data, "stderr is captured into the same log"
    assert fake.lines == [], "non-TTY, no events: nothing may reach stdout"


def test_tty_parent_gets_tee_and_non_tty_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    code = "print('tee-me')\nimport sys; sys.stderr.write('tee-err\\n')\n"

    tty_fake = _FakeStdout(tty=True)
    monkeypatch.setattr(sys, "stdout", tty_fake)
    assert _run_py(code, log_stem="tty").returncode == 0
    joined = "".join(tty_fake.lines)
    assert "tee-me" in joined and "tee-err" in joined, "TTY sees everything"

    quiet_fake = _FakeStdout(tty=False)
    monkeypatch.setattr(sys, "stdout", quiet_fake)
    assert _run_py(code, log_stem="quiet", relay_events=False).returncode == 0
    assert quiet_fake.lines == [], (
        "non-TTY with relay off: the terminal contract is silence"
    )


def test_relay_forwards_only_vco_event_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    fake = _FakeStdout(tty=False)
    monkeypatch.setattr(sys, "stdout", fake)

    code = (
        "import sys\n"
        "print('[VCO-EVENT] 7c/10 start seeding shared KG')\n"
        "print('ordinary stdout progress line')\n"
        "sys.stderr.write('[VCO-EVENT] 7c/10 ok batch-0\\n')\n"
        "sys.stderr.write('ordinary stderr noise\\n')\n"
    )
    result = _run_py(code, log_stem="relay")

    assert result.returncode == 0
    event_lines = [ln for ln in fake.lines if "[VCO-EVENT]" in ln]
    assert len(event_lines) == 2, "events from BOTH merged streams relay"
    joined = "".join(fake.lines)
    assert "ordinary" not in joined, "non-event lines stay log-only"
    # Line-oriented + terminated: every relayed write is a complete line.
    assert all(ln.endswith("\n") for ln in fake.lines)


def test_relay_survives_parent_stdout_write_error(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    fake = _FakeStdout(tty=False, fail_writes=True)
    monkeypatch.setattr(sys, "stdout", fake)

    code = (
        "import sys\n"
        "print('[VCO-EVENT] 7c/10 start will-be-dropped')\n"
        "print('more output')\n"
    )
    result = _run_py(code, log_stem="broken")

    assert result.returncode == 0, (
        "a broken parent stdout must be dropped, never propagated — "
        "wedging the drain loop reintroduces the deadlock this fixes"
    )
    assert fake.lines == []


def test_check_semantics_match_subprocess_run(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    quiet = _FakeStdout(tty=False)
    monkeypatch.setattr(sys, "stdout", quiet)

    cmd = [sys.executable, "-c", "import sys; sys.exit(7)"]
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        run_child_logged(cmd, log_stem="fail")
    assert excinfo.value.returncode == 7
    assert list(excinfo.value.cmd) == cmd

    result = run_child_logged(cmd, log_stem="fail-unchecked", check=False)
    assert result.returncode == 7


def test_missing_executable_still_raises_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        run_child_logged(["/nonexistent/wp1-probe-binary"], log_stem="ghost")


# ── (b) integration: undrained parent pipes must not deadlock ───────────────

_FLOOD_CHILD = textwrap.dedent("""
    import sys
    chunk = "x" * 65536 + "\\n"
    for _ in range(16):
        sys.stderr.write(chunk)
""")


def test_child_flood_completes_with_undrained_parent_pipes(tmp_path):
    """1 MB to stderr while the parent's OWN stdout/stderr are os.pipe()
    write-ends nobody reads (the old launcher reader shape).

    With the pre-WP-1 inherited stdio the child blocks once the ~64 KB
    pipe buffer fills and this test times out; with the log-routing
    helper the flood lands in the log file and both processes exit.
    """
    state = tmp_path / "vct-state"
    flood_py = tmp_path / "flood_child.py"
    flood_py.write_text(_FLOOD_CHILD, encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(
        textwrap.dedent(f"""
            import os, sys
            os.environ["VCT_STATE_DIR"] = {str(state)!r}
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from vco_lib.child_process import run_child_logged
            run_child_logged([sys.executable, {str(flood_py)!r}],
                             log_stem="flood")
        """),
        encoding="utf-8",
    )

    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.run(
            [sys.executable, str(runner)],
            # child_env() pins the runner's import of vco_lib to THIS
            # checkout (the runner inserts REPO_ROOT itself, but the pin
            # is the convention every spawn in this suite follows).
            env=child_env(),
            stdout=write_fd,
            stderr=write_fd,
            timeout=45,
        )
        assert proc.returncode == 0
        logs = list((state / "logs").glob("flood-*.log"))
        assert len(logs) == 1
        assert logs[0].stat().st_size >= 1024 * 1024, (
            "the flood must be IN the log, not discarded"
        )
    finally:
        # Closing the read end first: if anything is still blocked writing
        # into the undrained pipe (the pre-fix shape), it gets EPIPE and
        # dies instead of lingering past the test.
        os.close(read_fd)
        os.close(write_fd)


# ── (c) relay not starved by the flood ──────────────────────────────────────

_EVENT_FLOOD_CHILD = textwrap.dedent("""
    import sys
    sys.stdout.write("[VCO-EVENT] 7c/10 start seed\\n"); sys.stdout.flush()
    for i in range(8):
        sys.stderr.write("z" * 8192 + "\\n")
        sys.stdout.write(f"[VCO-EVENT] 7c/10 ok batch-{i}\\n"); sys.stdout.flush()
    sys.stdout.write("[VCO-EVENT] 7c/10 ok seed-complete\\n"); sys.stdout.flush()
""")


def test_event_relay_flows_while_child_floods_stderr(tmp_path, monkeypatch):
    """WP-8's heartbeat source: events must arrive DURING the flood, in
    order — not buffered behind it (one merged stream, one reader)."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    fake = _FakeStdout(tty=False)
    monkeypatch.setattr(sys, "stdout", fake)

    result = _run_py(_EVENT_FLOOD_CHILD, log_stem="relay-flood")

    assert result.returncode == 0
    joined = "".join(fake.lines)
    batch_positions = [joined.index(f"batch-{i}") for i in range(8)]
    assert batch_positions == sorted(batch_positions), "in-order delivery"
    assert joined.index("start seed") < batch_positions[0]
    assert batch_positions[-1] < joined.index("seed-complete")
    assert joined.count("[VCO-EVENT]") == 10
    assert "zzz" not in joined, "the flood itself stays log-only"


# ── install.py spawn-site pins (both sites, per review D) ───────────────────


def _references_sync_kg(call: ast.Call) -> bool:
    if not call.args:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == "sync_kg"
        for node in ast.walk(call.args[0])
    )


def test_install_seed_spawn_sites_use_run_child_logged():
    tree = ast.parse(INSTALL_PY.read_text(encoding="utf-8"))
    helper_calls: List[ast.Call] = []
    legacy_runs: List[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _references_sync_kg(node):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "run_child_logged":
            helper_calls.append(node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            legacy_runs.append(node)

    assert len(helper_calls) == 2, (
        "WP-1 owns EXACTLY the per-project sync and the shared-KG seed; "
        f"found {len(helper_calls)} run_child_logged seed spawns"
    )
    assert legacy_runs == [], (
        "an inherited-stdio subprocess.run spawn of sync_knowledge_graph "
        "survived WP-1 — that is the deadlock class this work package "
        "exists to remove"
    )
    stems = set()
    for call in helper_calls:
        for kw in call.keywords:
            if kw.arg == "log_stem" and isinstance(kw.value, ast.Constant):
                stems.add(kw.value.value)
    assert stems == {"kg-sync", "shared-kg-seed"}


def test_failure_paths_name_the_log_file(tmp_path, monkeypatch, capsys):
    """WP-1 review M-1: a failing child must tell the user WHERE its full
    output went. The note goes to stderr (never stdout — a library cannot
    know its caller's stdout contract, the v0.2.84 rule) and names the
    per-run log path; a degraded run (no log) says so instead of inventing
    a path."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    fake = _FakeStdout(tty=False)
    monkeypatch.setattr(sys, "stdout", fake)

    child = [sys.executable, "-c", "import sys; sys.exit(3)"]
    with pytest.raises(subprocess.CalledProcessError):
        run_child_logged(child, log_stem="failnote")
    err = capsys.readouterr().err
    assert "full output log:" in err, err
    logs = list((tmp_path / "logs").glob("failnote-*.log"))
    assert len(logs) == 1
    assert str(logs[0]) in err, "the note must name the run's own log file"
    assert capsys.readouterr().out == "", "the note must never touch stdout"

    missing = [str(tmp_path / "no-such-interpreter")]
    with pytest.raises(FileNotFoundError):
        run_child_logged(missing, log_stem="failnote2")
    err2 = capsys.readouterr().err
    assert "spawn failed" in err2 and "full output log:" in err2, err2


# ── (e) ship-gate MAJOR-1: real producer × relay × watchdog consumer ────────

SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"


class _WatchdogShape:
    """Python mirror of update_pipeline.rs's stall watchdog state: ANY
    relayed line is ``note_progress()``; ``notice_due`` fires at most once
    per stall window of CONTINUED silence. The pre-first-line grace (no
    notice before any line has arrived) stands in for the real pipeline's
    own 7c/10 start event, which precedes the seed spawn."""

    def __init__(self, stall_s: float) -> None:
        self.stall_s = stall_s
        self.last_progress: "float | None" = None
        self.last_notice: "float | None" = None
        self.notices: List[str] = []

    def note_progress(self) -> None:
        self.last_progress = time.monotonic()

    def check(self) -> None:
        if self.last_progress is None:
            return
        now = time.monotonic()
        if now - self.last_progress < self.stall_s:
            return
        if self.last_notice is not None and now - self.last_notice < self.stall_s:
            return
        self.last_notice = now
        self.notices.append("update may be stalled")


class _WatchdogStdout(_FakeStdout):
    """The consumer side of the seam: every line the relay writes is a
    heartbeat, exactly like read_stdout's note_progress-before-parse."""

    def __init__(self, watchdog: _WatchdogShape) -> None:
        super().__init__(tty=False)
        self._watchdog = watchdog

    def write(self, text: str) -> int:
        result = super().write(text)
        self._watchdog.note_progress()
        return result


def _run_watchdog_child(tmp_path: Path, monkeypatch, *, mode: str):
    """Spawn a REAL child that loads the shipped sync script and runs its
    ACTUAL emitter through run_child_logged, while a watchdog-shaped
    consumer watches the parent's relayed stdout.

    ``mode="healthy"``: emitter beats every ~0.4 s (throttle relaxed in
    the child), well inside the 1.5 s stall window — no notice may fire.
    ``mode="silent"``: one beat, then a wedged sleep — the notice MUST
    fire (a genuinely hung child still trips the watchdog).
    """
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("VCO_PROGRESS_STREAM", raising=False)

    beat_s, silent_s = 0.4, 2.6
    driver = textwrap.dedent(f"""
        import importlib.util, os, sys, time
        spec = importlib.util.spec_from_file_location(
            "skg_child", os.environ["SKG_PATH"])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["skg_child"] = mod
        spec.loader.exec_module(mod)
        mod._HEARTBEAT_MIN_INTERVAL_S = 0.05
        mod._emit_sync_event("start", "syncing knowledge: 4 nodes")
        if os.environ.get("SKG_MODE") == "silent":
            time.sleep({silent_s!r})
        else:
            for i in range(1, 5):
                time.sleep({beat_s!r})
                mod._heartbeat_note_node(i, 4, "knowledge", mod.SyncTally())
            mod._emit_sync_event("ok", "synced knowledge: 4 nodes")
    """)

    watchdog = _WatchdogShape(stall_s=1.5)
    fake = _WatchdogStdout(watchdog)
    monkeypatch.setattr(sys, "stdout", fake)
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            watchdog.check()
            time.sleep(0.05)

    watcher = threading.Thread(target=_loop, daemon=True)
    watcher.start()
    try:
        result = run_child_logged(
            [sys.executable, "-c", driver],
            log_stem="watchdog",
            env=child_env(
                SKG_PATH=str(SYNC_SCRIPT),
                SKG_MODE=mode,
                # The launcher→install.py→seed_env chain in miniature:
                # VCO_PROGRESS_STREAM=1 is what the real GUI path threads
                # into this child.
                VCO_PROGRESS_STREAM="1",
                KG_BASE_DIR=str(tmp_path),
                KG_COLLECTION="TestProject_KnowledgeGraph",
                DEVELOPMENT_COLLECTION="TestProject_Development",
                DUAL_EMBEDDING_ENABLED="false",
                VCT_DISABLE_HUB_RESOLVER="1",
            ),
        )
    finally:
        stop.set()
        watcher.join(timeout=2)
    return result, fake.lines, watchdog


def test_real_producer_events_reset_a_watchdog_shaped_consumer(
        tmp_path, monkeypatch):
    result, lines, watchdog = _run_watchdog_child(
        tmp_path, monkeypatch, mode="healthy")

    assert result.returncode == 0
    events = [ln for ln in lines if ln.startswith("[VCO-EVENT]")]
    assert len(events) >= 4, (
        f"the shipped emitter's beats must reach the parent: {lines!r}"
    )
    assert all(ln.startswith("[VCO-EVENT] kg-sync ") for ln in events)
    assert watchdog.notices == [], (
        "a child beating well inside the stall window must never look "
        "stalled — this is the false-positive the MAJOR-1 producer exists "
        "to remove"
    )


def test_a_silent_child_trips_the_watchdog_shaped_consumer(
        tmp_path, monkeypatch):
    result, lines, watchdog = _run_watchdog_child(
        tmp_path, monkeypatch, mode="silent")

    assert result.returncode == 0
    events = [ln for ln in lines if ln.startswith("[VCO-EVENT]")]
    assert len(events) == 1, "the wedged child emitted only its start beat"
    assert len(watchdog.notices) >= 1, (
        "total silence past the stall window must still fire the notice — "
        "the producer must not mask a genuinely hung child"
    )
