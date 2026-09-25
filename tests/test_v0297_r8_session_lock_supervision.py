# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review R8 G3 — the session lock outlives the work it guards.

The container session hooks (`ensure-containers`, `verify-container-ports`)
take ONE per-user lock around "reconcile → plan → act". The hook runner kills
a hook at its registered `timeout`; before this fix the lock holder was the
process that kill reached, so the lock was released while the hook's
`compose up` ran on, unlocked. Now:

* the holder forwards SIGTERM/SIGINT/SIGHUP to the child's process group and
  releases the lock only after the child has exited (SIGKILL after a grace);
* the hooks start the locked part DETACHED and only relay its output within
  a budget that fits the registered timeout — so neither a timeout kill of
  the hook's whole process group nor a slow start can split lock and work.

Every test runs real processes against a temp VCT_STATE_DIR; nothing
reaches a container runtime (the hook test uses the lifecycle-hook harness's
fakes).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_v0297_lifecycle_hooks import (
    PWSH,
    SENTINEL_MANAGED,
    _HeldSessionLock,
    _Machine,
    _run_hook,
)
from tests.common.child_env import child_env
from vco_lib import service_lifecycle as sl

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups and fcntl")


def _env(state: Path) -> dict:
    env = child_env(VCT_STATE_DIR=str(state))
    env.pop(sl.SESSION_LOCK_HELD_ENV, None)
    env.pop(sl.SESSION_DETACHED_ENV, None)
    return env


def _lock_is_held(state: Path) -> bool:
    import fcntl

    lock = state / "locks" / "container-session.lock"
    with open(lock, "a", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it is not running.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def _wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return True
        time.sleep(0.05)
    return False


#: The hook stand-in: records its own PID and a grandchild's (the
#: `compose up` in flight), and on SIGTERM takes a second to wind down —
#: the window in which the lock must still be held.
_CHILD = r"""
trap 'echo TERM >> "$W/events"; sleep 1; echo EXIT >> "$W/events"; exit 0' TERM
sleep 60 &
echo $! > "$W/grandchild"
echo $$ > "$W/child"
wait
"""


def _start_holder(tmp: Path, child: str) -> subprocess.Popen:
    state = tmp / "state"
    env = child_env(_env(state), W=str(tmp))
    return subprocess.Popen(
        [sys.executable, "-m", "vco_lib.service_lifecycle", "with-session-lock", "--wait", "2",
         "--", "bash", "-c", child],
        env=env, cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_a_signalled_lock_holder_stops_its_child_before_it_releases_the_lock(tmp_path):
    holder = _start_holder(tmp_path, _CHILD)
    try:
        assert _wait_for(tmp_path / "grandchild", 15), "the locked child never started"
        state = tmp_path / "state"
        child = int((tmp_path / "child").read_text())
        grandchild = int((tmp_path / "grandchild").read_text())
        assert _lock_is_held(state)
        holder.send_signal(signal.SIGTERM)  # what a hook runner's timeout sends
        assert _wait_for(tmp_path / "events", 5), "SIGTERM was not forwarded to the child"
        # The child is winding down: the lock is still held.
        assert _alive(child)
        assert _lock_is_held(state), "the lock was released while the child still ran"
        assert holder.wait(timeout=15) == 128 + signal.SIGTERM
        # Once the holder is gone, nothing it started under the lock runs on.
        assert not _alive(child)
        assert not _alive(grandchild), "the grandchild (the compose in flight) outlived the lock"
        assert not _lock_is_held(state)
        assert (tmp_path / "events").read_text().split() == ["TERM", "EXIT"]
    finally:
        if holder.poll() is None:
            holder.kill()


def test_a_child_that_ignores_the_signal_is_killed_before_the_lock_is_released(tmp_path):
    stubborn = r"""
trap '' TERM
sleep 60 &
echo $! > "$W/grandchild"
echo $$ > "$W/child"
wait
"""
    holder = _start_holder(tmp_path, stubborn)
    try:
        assert _wait_for(tmp_path / "grandchild", 15)
        child = int((tmp_path / "child").read_text())
        grandchild = int((tmp_path / "grandchild").read_text())
        holder.send_signal(signal.SIGTERM)
        time.sleep(1.0)
        assert _alive(child) and _lock_is_held(tmp_path / "state")
        holder.wait(timeout=sl_grace() + 10)
        assert not _alive(child) and not _alive(grandchild)
        assert not _lock_is_held(tmp_path / "state")
    finally:
        if holder.poll() is None:
            holder.kill()


def sl_grace() -> float:
    from vco_lib.hook_supervisor import SUPERVISE_GRACE_S

    return SUPERVISE_GRACE_S


_RELAY = r"""
import sys
from vco_lib.hook_supervisor import relay_detached
from vco_lib.service_lifecycle import detached_session_hook_argv
argv = detached_session_hook_argv(["bash", "-c", sys.argv[2]], lock_wait_s=2)
sys.exit(relay_detached(argv, name="ensure-containers", budget_s=float(sys.argv[1])))
"""

_SLOW_WORK = r"""
echo early
echo started > "$W/started"
sleep 3
echo done > "$W/done"
echo late
"""


def _start_relay(tmp: Path, budget: float) -> subprocess.Popen:
    env = child_env(_env(tmp / "state"), W=str(tmp))
    # Its own process group, like a hook under its runner: the test kills
    # that WHOLE group, the harshest form a timeout kill can take.
    return subprocess.Popen([sys.executable, "-c", _RELAY, str(budget), _SLOW_WORK],
                            env=env, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)


def test_a_timeout_kill_of_the_hook_group_leaves_the_lock_with_the_work(tmp_path):
    relay = _start_relay(tmp_path, budget=30)
    try:
        assert _wait_for(tmp_path / "started", 15), "the detached work never started"
        os.killpg(relay.pid, signal.SIGKILL)
        relay.wait(timeout=5)
        assert _lock_is_held(tmp_path / "state"), "the kill released the lock mid-work"
        assert _wait_for(tmp_path / "done", 15), "the kill stopped the work"
        deadline = time.monotonic() + 10
        while _lock_is_held(tmp_path / "state") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _lock_is_held(tmp_path / "state"), "the lock outlived the work"
    finally:
        if relay.poll() is None:
            relay.kill()


def test_the_relay_returns_within_its_budget_and_names_the_log(tmp_path):
    started = time.monotonic()
    relay = _start_relay(tmp_path, budget=1.0)
    out, _ = relay.communicate(timeout=30)
    elapsed = time.monotonic() - started
    assert elapsed < 2.9, f"the relay waited for the work ({elapsed:.1f}s)"
    assert "early" in out and "late" not in out, out
    assert "still working in the background" in out, out
    log = Path(out.rsplit("continues in ", 1)[1].strip())
    assert log.is_file() and log.parent == tmp_path / "state" / "logs" / "session-hooks"
    assert _wait_for(tmp_path / "done", 15)
    time.sleep(0.3)
    assert "late" in log.read_text(encoding="utf-8")


def test_a_relay_whose_work_ends_inside_the_budget_relays_all_of_it(tmp_path):
    relay = _start_relay(tmp_path, budget=20)
    out, _ = relay.communicate(timeout=30)
    assert relay.returncode == 0
    assert out.split() == ["early", "late"], out
    assert not list((tmp_path / "state" / "logs" / "session-hooks").glob("*.log"))


# ─── the budget: every session hook returns inside its registered timeout ──


def _registered_timeouts() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for template in sorted((REPO_ROOT / "templates").glob("settings.json.*.template")):
        data = json.loads(template.read_text(encoding="utf-8"))
        for group in data["hooks"]["SessionStart"]:
            for hook in group["hooks"]:
                for name in sl.SESSION_HOOK_RELAY_BUDGET_S:
                    if f"/{name}." in hook["command"]:
                        found[(template.name, name)] = int(hook["timeout"])
    return found


#: What a hook does in the foreground before it detaches (interpreter
#: start-up, the venv resolution; the watchdog's unlocked detection pass).
_FOREGROUND_ALLOWANCE_S = 5.0


def test_every_relay_budget_fits_its_registered_timeout():
    timeouts = _registered_timeouts()
    assert {name for _t, name in timeouts} == set(sl.SESSION_HOOK_RELAY_BUDGET_S), timeouts
    assert len({t for t, _n in timeouts}) >= 2, timeouts  # linux + windows
    for (template, name), timeout in timeouts.items():
        budget = sl.SESSION_HOOK_RELAY_BUDGET_S[name]
        assert budget + _FOREGROUND_ALLOWANCE_S <= timeout, (template, name, budget, timeout)


def _slow_compose(m: _Machine) -> str:
    script = m.bin / "slow-compose"
    script.write_text(f'#!/usr/bin/env bash\nsleep 6\nprintf "%s\\n" "$*" >> "{m.compose_log}"\n',
                      encoding="utf-8")
    script.chmod(0o755)
    return str(script)


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_ensure_containers_returns_inside_its_timeout_while_the_work_finishes(tmp_path, shell):
    """Lock wait (held 5 s by the "other hook") + a 7 s reconcile + a 6 s
    `compose up` = 18 s of work against a 15 s timeout: the hook returns
    inside the timeout, and the work — compose included — still completes,
    under the lock."""
    if shell == "pwsh" and not PWSH:
        pytest.skip("no PowerShell")
    m = _Machine(tmp_path, SENTINEL_MANAGED, {"vco_weaviate": "missing"})
    held = _HeldSessionLock(m, 5.0)
    started = time.monotonic()
    proc = _run_hook(m, shell, FAKE_RECONCILE_SLEEP="7", VCT_COMPOSE_CMD=_slow_compose(m))
    elapsed = time.monotonic() - started
    held.join()
    timeout = _registered_timeouts()[("settings.json.linux.template", "ensure-containers")]
    assert elapsed < timeout, f"{elapsed:.1f}s\n{proc.stdout}\n{proc.stderr}"
    assert "still working in the background" in proc.stdout, proc.stdout + proc.stderr
    deadline = time.monotonic() + 40
    while not m.compose_calls() and time.monotonic() < deadline:
        time.sleep(0.2)
    calls = m.compose_calls()
    assert len(calls) == 1 and "weaviate" in calls[0], f"{calls}\n{proc.stdout}"
    rt = m.runtime_calls()
    assert rt.index(["RELEASED"]) < rt.index(["RECONCILE"]), rt
    deadline = time.monotonic() + 10
    while _lock_is_held(m.state) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _lock_is_held(m.state)
