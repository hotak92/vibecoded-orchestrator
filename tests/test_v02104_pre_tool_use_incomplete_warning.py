# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — pre-tool-use warns the model when a previous run did not finish.

A Claude Code PreToolUse command hook that times out or crashes does NOT
block: the call proceeds and the hook's output is discarded, so the Build
Anchor, SSRF guard and shell-injection scan did not apply to it. Owner ruling
2026-10-18: no ``onFailure: "block"`` — warn the model instead. A killed hook
cannot speak, so every run leaves an in-progress marker
``.claude/state/ptu_inflight_<key>.<pid>.<start-ms>`` that its normal exits
remove; the NEXT run of the same session/agent key that finds a marker whose
owner is gone (or that is older than the timeout plus a margin) reports it
once — additionalContext on an allow, stderr on a block — logs it, and
deletes it. A sibling run still at work is never reported.

These tests drive the REAL hook scripts (bash, and pwsh where installed):
a run SIGKILLed mid-scan, a run that finished past the harness timeout,
normal and blocking runs, concurrent runs, key isolation and GC.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
HAVE_PWSH = shutil.which("pwsh") is not None
PWSH = shutil.which("pwsh") or "pwsh"
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(
    not HAVE_PWSH, reason="pwsh not installed"))]
SID = "sessPTU1"
WARN = "[VCO safety check incomplete]"
# A pid no live process holds (above the default Linux pid_max range).
DEAD_PID = 4_194_999


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "logs").mkdir(parents=True)
    return proj


def _stub_scanner(proj: Path, sleep_s: float) -> None:
    """A bash_security.py that sleeps, then allows (exit 0)."""
    scripts = proj / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "bash_security.py").write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        f"time.sleep({sleep_s})\n"
        "sys.exit(0)\n", encoding="utf-8")


def _env(proj: Path, **extra: str) -> dict:
    env = dict(os.environ)
    for k in ("VCT_DISABLE_HOOKS", "VCO_PRE_TOOL_USE_TIMEOUT_S",
              "VCO_HOOK_CONTEXT_CAP", "VCT_VENV", "VCT_INSTALL_ROOT"):
        env.pop(k, None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["PYTHONPATH"] = str(REPO)
    env["WEAVIATE_URL"] = "http://127.0.0.1:9"
    env.update(extra)
    return env


def _argv(impl: str) -> list[str]:
    if impl == "sh":
        return ["bash", str(HOOKS / "pre-tool-use.sh")]
    return [PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
            str(HOOKS / "pre-tool-use.ps1")]


def _payload(tool: str, tool_input: dict, sid: str = SID, **extra: str) -> str:
    d = {"session_id": sid, "tool_name": tool, "tool_input": tool_input}
    d.update(extra)
    return json.dumps(d)


def _run(impl: str, proj: Path, payload: str, **extra: str
         ) -> subprocess.CompletedProcess:
    return subprocess.run(_argv(impl), input=payload, capture_output=True,
                          text=True, timeout=120, cwd=str(proj),
                          env=_env(proj, **extra))


def _grep(impl: str, proj: Path, sid: str = SID, **extra: str
          ) -> subprocess.CompletedProcess:
    return _run(impl, proj, _payload("Grep", {"pattern": "x"}, sid=sid), **extra)


def _markers(proj: Path) -> list[Path]:
    return sorted((proj / ".claude" / "state").glob("ptu_inflight_*"))


def _plant(proj: Path, key: str, pid: int, start_ms: int,
           tools: str = "Bash\n") -> Path:
    m = proj / ".claude" / "state" / f"ptu_inflight_{key}.{pid}.{start_ms}"
    m.write_text(tools, encoding="utf-8")
    return m


def _now_ms() -> int:
    return int(time.time() * 1000)


def _context(res: subprocess.CompletedProcess) -> str:
    """The additionalContext of the hook's ONE stdout envelope ("" if none)."""
    out = res.stdout.strip()
    if not out:
        return ""
    lines = out.splitlines()
    assert len(lines) == 1, f"more than one stdout line: {out!r}"
    env = json.loads(lines[0])
    hso = env["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    # Context injection must never approve the call (no permissionDecision).
    assert set(hso) == {"hookEventName", "additionalContext"}
    return hso["additionalContext"]


def _security_events(proj: Path) -> list[dict]:
    log = proj / ".claude" / "logs" / "security_events.jsonl"
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]


# ─── the ordinary runs leave nothing behind ──────────────────────────────


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_normal_run_leaves_no_marker_and_says_nothing(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    res = _grep(impl, proj)
    assert res.returncode == 0, res.stderr
    assert _markers(proj) == []
    assert WARN not in res.stdout + res.stderr


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_read_run_leaves_no_marker(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    res = _run(impl, proj, _payload("Read", {"file_path": str(proj / "a.txt")}))
    assert res.returncode == 0, res.stderr
    assert _markers(proj) == []


@pytest.mark.parametrize("impl", _SHELLS)
def test_an_exit_2_block_leaves_no_marker(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    target = proj / "unread.txt"
    target.write_text("x\n", encoding="utf-8")
    res = _run(impl, proj, _payload("Write", {"file_path": str(target), "content": "y"}))
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "Build Anchor" in res.stderr
    assert _markers(proj) == []


# ─── a killed run is reported by the next one, once ─────────────────────


def _start_bash_call(impl: str, proj: Path, **extra: str) -> subprocess.Popen:
    p = subprocess.Popen(_argv(impl), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(proj), env=_env(proj, **extra),
                         start_new_session=True)
    assert p.stdin is not None
    p.stdin.write(_payload("Bash", {"command": "ls -la"}).encode())
    p.stdin.close()
    return p


def _wait_for_marker(proj: Path, p: subprocess.Popen, deadline_s: float = 30.0) -> Path:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        ms = _markers(proj)
        if ms:
            return ms[0]
        assert p.poll() is None, "hook exited before writing its marker"
        time.sleep(0.02)
    raise AssertionError("no in-progress marker appeared")


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_run_killed_mid_scan_is_reported_once_by_the_next_run(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    _stub_scanner(proj, 30)
    p = _start_bash_call(impl, proj)
    marker = _wait_for_marker(proj, p)
    time.sleep(0.3)   # well inside the scan
    os.killpg(p.pid, signal.SIGKILL)   # what a harness timeout leaves
    p.wait(timeout=30)
    assert marker.exists(), "a killed run must leave its marker"

    first = _grep(impl, proj)
    assert first.returncode == 0, first.stderr
    ctx = _context(first)
    assert WARN in ctx
    assert "(Bash)" in ctx and "1 earlier tool call" in ctx
    assert "Build Anchor, SSRF guard, shell-injection scan" in ctx
    assert "ls -la" not in ctx   # no command text, ever
    assert _markers(proj) == []

    second = _grep(impl, proj)
    assert second.returncode == 0
    assert _context(second) == ""

    ev = [e for e in _security_events(proj) if e.get("event") == "pre_tool_use_incomplete"]
    assert len(ev) == 1
    assert ev[0]["tools"] == "Bash" and ev[0]["reason"] == "owner_gone"
    assert ev[0]["timestamp"]


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_run_that_finished_past_the_timeout_is_reported(tmp_path: Path, impl: str):
    # The hook outlived the harness timeout (an orphan the harness stopped
    # waiting for): its answer was discarded, so its marker must survive.
    proj = _project(tmp_path)
    _stub_scanner(proj, 1.5)
    res = _run(impl, proj, _payload("Bash", {"command": "ls"}),
               VCO_PRE_TOOL_USE_TIMEOUT_S="1")
    assert res.returncode == 0, res.stderr
    assert len(_markers(proj)) == 1
    nxt = _grep(impl, proj, VCO_PRE_TOOL_USE_TIMEOUT_S="1")
    assert "(Bash)" in _context(nxt)
    assert _markers(proj) == []


@pytest.mark.parametrize("impl", _SHELLS)
def test_the_report_rides_stderr_when_the_next_run_blocks(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    _plant(proj, SID, DEAD_PID, _now_ms() - 500, "Edit\n")
    target = proj / "unread.txt"
    target.write_text("x\n", encoding="utf-8")
    res = _run(impl, proj, _payload("Write", {"file_path": str(target), "content": "y"}))
    assert res.returncode == 2
    assert WARN in res.stderr and "(Edit)" in res.stderr
    assert res.stdout.strip() == ""
    assert _markers(proj) == []


@pytest.mark.parametrize("impl", _SHELLS)
def test_an_overdue_marker_of_a_live_pid_is_reported(tmp_path: Path, impl: str):
    # This test process is alive, but a marker 60 s old is past the 3 s
    # timeout plus margin: the harness gave up on that run long ago.
    proj = _project(tmp_path)
    _plant(proj, SID, os.getpid(), _now_ms() - 60_000, "WebFetch\n")
    res = _grep(impl, proj)
    assert "(WebFetch)" in _context(res)
    ev = [e for e in _security_events(proj) if e.get("event") == "pre_tool_use_incomplete"]
    assert ev and ev[0]["reason"] == "overdue"


@pytest.mark.parametrize("impl", _SHELLS)
def test_carried_tools_are_all_reported(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    _plant(proj, SID, DEAD_PID, _now_ms() - 500, "Write\nBash\n")
    ctx = _context(_grep(impl, proj))
    assert "2 earlier tool call(s)" in ctx and "(Write, Bash)" in ctx


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_report_lost_to_a_killed_reporter_is_carried_forward(tmp_path: Path, impl: str):
    # Run B claims A's stale marker, then is killed itself: B's own marker
    # must now carry both tool names to run C.
    proj = _project(tmp_path)
    _plant(proj, SID, DEAD_PID, _now_ms() - 500, "Write\n")
    _stub_scanner(proj, 30)
    p = _start_bash_call(impl, proj)
    end = time.monotonic() + 30
    while time.monotonic() < end:
        ms = _markers(proj)
        if len(ms) == 1 and str(DEAD_PID) not in ms[0].name \
                and ms[0].read_text(encoding="utf-8").count("\n") == 2:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"carry-over not written: {_markers(proj)}")
    os.killpg(p.pid, signal.SIGKILL)
    p.wait(timeout=30)
    ctx = _context(_grep(impl, proj))
    assert "2 earlier tool call(s)" in ctx and "(Bash, Write)" in ctx


@pytest.mark.parametrize("impl", _SHELLS)
def test_carried_names_are_capped(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    _plant(proj, SID, DEAD_PID, _now_ms() - 500, "Bash\n" * 50)
    ctx = _context(_grep(impl, proj))
    assert "20 earlier tool call(s)" in ctx


# ─── never a sibling that is still running, never another key ───────────


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_concurrent_sibling_is_not_reported(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    _stub_scanner(proj, 1.0)
    p = _start_bash_call(impl, proj)
    marker = _wait_for_marker(proj, p)
    during = _grep(impl, proj)
    assert during.returncode == 0
    assert _context(during) == ""
    assert marker.exists(), "a running sibling's marker must be left alone"
    assert p.stdout is not None and p.stderr is not None
    out, err = p.stdout.read(), p.stderr.read()
    assert p.wait(timeout=60) == 0, err
    assert WARN.encode() not in out + err
    assert _markers(proj) == []
    assert _context(_grep(impl, proj)) == ""


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_fresh_marker_of_a_live_owner_is_left_alone(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    m = _plant(proj, SID, os.getpid(), _now_ms())
    assert _context(_grep(impl, proj)) == ""
    assert m.exists()


@pytest.mark.parametrize("impl", _SHELLS)
def test_markers_are_keyed_per_session_and_agent(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    other = _plant(proj, "otherSession", DEAD_PID, _now_ms() - 500)
    sub = _plant(proj, f"{SID}__agentabc", DEAD_PID, _now_ms() - 500, "Write\n")
    assert _context(_grep(impl, proj)) == ""   # main agent of SID: nothing
    assert other.exists() and sub.exists()
    res = _run(impl, proj, _payload("Grep", {"pattern": "x"}, agent_id="agentabc"))
    assert "(Write)" in _context(res)
    assert not sub.exists() and other.exists()


@pytest.mark.parametrize("impl", _SHELLS)
def test_day_old_markers_of_any_session_are_collected(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    old = _plant(proj, "goneSession", os.getpid(), _now_ms() - 2 * 86_400_000)
    t = time.time() - 2 * 86_400
    os.utime(old, (t, t))
    _grep(impl, proj)
    assert not old.exists()


# ─── the shared liveness rule (one home: _lib/pid-live) ─────────────────


def _sh_pid_live(pid: int, stamp: int) -> bool:
    r = subprocess.run(["bash", "-c", f'. "{HOOKS}/_lib/pid-live.sh"; vco_pid_live {pid} {stamp}'],
                       capture_output=True, timeout=30)
    return r.returncode == 0


def test_sh_pid_live_rules():
    now = int(time.time())
    assert _sh_pid_live(os.getpid(), now) is True
    assert _sh_pid_live(DEAD_PID, now) is False
    # Same pid, stamped long before this process started: a recycled pid.
    assert _sh_pid_live(os.getpid(), now - 10 * 86_400) is False


def test_debounce_keeps_its_name_as_a_wrapper():
    r = subprocess.run(["bash", "-c",
                        f'. "{HOOKS}/_lib/kg-sync-debounce.sh"; '
                        f'_kg_debounce_pid_live {os.getpid()} {int(time.time())} && echo live; '
                        f'_kg_debounce_pid_live {DEAD_PID} {int(time.time())} || echo dead'],
                       capture_output=True, text=True, timeout=30)
    assert r.stdout.split() == ["live", "dead"], r.stderr


@pytest.mark.skipif(not HAVE_PWSH, reason="pwsh not installed")
def test_ps1_pid_live_rules():
    lib = HOOKS / "_lib" / "pid-live.ps1"
    deb = HOOKS / "_lib" / "kg-sync-debounce.ps1"
    script = (
        f". '{lib}'\n"
        f"$now = Get-Date\n"
        f"Write-Output (Test-VcoPidLive -OwnerPid {os.getpid()} -Stamp $now)\n"
        f"Write-Output (Test-VcoPidLive -OwnerPid {DEAD_PID} -Stamp $now)\n"
        f"Write-Output (Test-VcoPidLive -OwnerPid {os.getpid()} -Stamp $now.AddDays(-10))\n"
        f". '{deb}'\n"
        f"Write-Output (Test-KgDebouncePidLive -ClaimPid {os.getpid()} -ClaimStamp $now)\n"
    )
    r = subprocess.run([PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.split() == ["True", "False", "False", "True"], r.stderr


# ─── R13: coarse clocks, ended sessions ──────────────────────────────────


def _coarse_env(tmp_path: Path, delta_s: int) -> dict[str, str]:
    """Emulate macOS bash 3.2 + BSD date: no EPOCHREALTIME, `date +%s%N`
    prints a literal N, and `date +%s` advances `delta_s` seconds between the
    hook's first and later readings (the coarse clock path)."""
    bindir = tmp_path / "coarse-bin"
    bindir.mkdir()
    cnt = tmp_path / "coarse-cnt"
    real = shutil.which("date") or "/usr/bin/date"
    shim = bindir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  +%s%N) printf '%%N\\n' ;;\n"
        f"  +%s) n=$(cat '{cnt}' 2>/dev/null || echo 0); n=$((n+1)); echo $n > '{cnt}';"
        f" if [ $n -ge 2 ]; then echo {1_000_000 + delta_s}; else echo 1000000; fi ;;\n"
        f'  *) exec {real} "$@" ;;\n'
        "esac\n", encoding="utf-8")
    shim.chmod(0o755)
    benv = tmp_path / "bashenv.sh"
    benv.write_text("unset EPOCHREALTIME\n", encoding="utf-8")
    return {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "BASH_ENV": str(benv)}


def test_coarse_clock_successful_run_clears_its_marker(tmp_path: Path):
    # R13 MEDIUM-1: a 2 s coarse reading on a 3 s budget is a run that
    # finished in time; it must not leave a marker (false warning next call).
    proj = _project(tmp_path)
    res = _grep("sh", proj, **_coarse_env(tmp_path, 2))
    assert res.returncode == 0, res.stderr
    assert _markers(proj) == []
    assert _context(_grep("sh", proj)) == ""


def test_coarse_clock_overdue_run_keeps_its_marker(tmp_path: Path):
    # A 5 s coarse reading on a 3 s budget is past the timeout for certain:
    # the harness discarded the answer, so the marker stays and is reported.
    proj = _project(tmp_path)
    res = _grep("sh", proj, **_coarse_env(tmp_path, 5))
    assert res.returncode == 0, res.stderr
    assert len(_markers(proj)) == 1
    assert "(Grep)" in _context(_grep("sh", proj))


@pytest.mark.parametrize("impl", _SHELLS)
def test_an_ended_sessions_marker_is_logged_when_collected(tmp_path: Path, impl: str):
    # R13 LOW-1: a killed hook on a session's LAST call has no later run of
    # its key to report it; the day-old GC records it before deleting it.
    proj = _project(tmp_path)
    old = _plant(proj, "goneSession__agentx", DEAD_PID,
                 _now_ms() - 2 * 86_400_000, "Write\nBash\n")
    t = time.time() - 2 * 86_400
    os.utime(old, (t, t))
    res = _grep(impl, proj)
    assert _context(res) == ""   # not this session's call: no model warning
    assert not old.exists()
    ev = [e for e in _security_events(proj) if e.get("event") == "pre_tool_use_incomplete"]
    assert len(ev) == 1, ev
    assert ev[0]["reason"] == "session_ended"
    assert ev[0]["tools"] == "Write Bash"
    assert ev[0]["session_key"] == "goneSession__agentx"


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_young_marker_of_another_session_is_not_collected(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    m = _plant(proj, "otherSession", DEAD_PID, _now_ms() - 3_600_000)
    _grep(impl, proj)
    assert m.exists()
    assert not [e for e in _security_events(proj) if e.get("event") == "pre_tool_use_incomplete"]


@pytest.mark.parametrize("impl", _SHELLS)
def test_a_malformed_marker_name_is_still_collected(tmp_path: Path, impl: str):
    proj = _project(tmp_path)
    junk = proj / ".claude" / "state" / "ptu_inflight_bad.name.x"
    junk.write_text("Bash\n", encoding="utf-8")
    t = time.time() - 3 * 86_400
    os.utime(junk, (t, t))
    _grep(impl, proj)
    assert not junk.exists()


@pytest.mark.parametrize("impl", _SHELLS)
@pytest.mark.parametrize("name", ["otherKey.abc", "otherKey.12.34", "otherKey."])
def test_the_gc_logs_only_well_formed_marker_names(tmp_path: Path, impl: str, name: str):
    # R14A NIT: <key>.<pid>.<start-ms> with a numeric pid, the same rule in
    # both shells. Anything else is not logged; the 2-day backstop removes
    # it later (mtime here is 1.5 days, inside the backstop's window).
    proj = _project(tmp_path)
    junk = proj / ".claude" / "state" / f"ptu_inflight_{name}.{_now_ms() - 2 * 86_400_000}"
    junk.write_text("Bash\n", encoding="utf-8")
    t = time.time() - 1.5 * 86_400
    os.utime(junk, (t, t))
    _grep(impl, proj)
    assert junk.exists()
    assert not [e for e in _security_events(proj) if e.get("event") == "pre_tool_use_incomplete"]


def test_gc_and_warning_hold_under_bash_compat_32(tmp_path: Path):
    # R14A: no bash 3.2 binary is available to CI or here; BASH_COMPAT=3.2
    # is the closest executable check (bash's own 3.2 compatibility level).
    # It is NOT a 3.2 parser — the syntax review is what covers that.
    proj = _project(tmp_path)
    old = _plant(proj, "goneS", DEAD_PID, _now_ms() - 2 * 86_400_000, "Write\n")
    _plant(proj, SID, DEAD_PID, _now_ms() - 500, "Edit\n")
    res = _grep("sh", proj, BASH_COMPAT="3.2")
    assert res.returncode == 0, res.stderr
    assert "(Edit)" in _context(res)
    assert not old.exists() and _markers(proj) == []
    reasons = sorted(e["reason"] for e in _security_events(proj)
                     if e.get("event") == "pre_tool_use_incomplete")
    assert reasons == ["owner_gone", "session_ended"]
