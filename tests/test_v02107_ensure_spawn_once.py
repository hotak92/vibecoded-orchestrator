# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""LAUNCH-ONCE (v0.2.107 wave 2): the three service ensures spawn ONCE.

Field report (Windows, v0.2.106): two launchers started together at boot and
both ran the whole start-up. The ensures checked "is it running?" and then
spawned with nothing between the two, so two callers in the same instant both
spawned. :mod:`vco_lib.ensure_lock` is the one lock all three take around
check + spawn, held until the started process answers or a bound expires.

The races below run in REAL separate processes — the lock is a cross-process
kernel lock, and a thread-only test would not prove that.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import ensure_lock as el  # noqa: E402
from vco_lib import gateway_ensure as ge  # noqa: E402
from vco_lib import launcher_ensure as le  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "vct"
    root.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(root))
    return root


# ---------------------------------------------------------------------------
# The two primitives
# ---------------------------------------------------------------------------


def test_lock_path_is_per_service_under_the_state_root(state):
    assert el.ensure_lock_path("hub") == state / "state" / "ensure-hub.lock"
    assert el.ensure_lock_path("launcher", Path("/x")) == Path("/x/state/ensure-launcher.lock")
    with pytest.raises(ValueError):
        el.ensure_lock_path("weaviate")


def test_wait_until_up_returns_as_soon_as_the_probe_answers():
    answers = iter([False, False, True])
    slept: list[float] = []
    assert el.wait_until_up(lambda: next(answers), bound_s=60, sleep=slept.append)
    assert len(slept) == 2


def test_wait_until_up_gives_up_at_the_bound_and_survives_a_raising_probe():
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(s: float) -> None:
        now[0] += s

    def probe() -> bool:
        raise OSError("not yet")

    assert el.wait_until_up(probe, bound_s=1.0, poll_s=0.25, clock=clock, sleep=sleep) is False
    assert now[0] == pytest.approx(1.0)


def test_wait_until_up_asks_once_even_with_a_zero_bound():
    calls: list[int] = []
    assert el.wait_until_up(lambda: calls.append(1) or False, bound_s=0) is False
    assert calls == [1]


def test_an_exception_in_the_locked_block_propagates_and_releases(state):
    with pytest.raises(OSError, match="body"):
        with el.spawn_lock("hub", settle_s=0) as held:
            assert held is True
            raise OSError("body")
    with el.spawn_lock("hub", settle_s=0) as held:
        assert held is True


# ---------------------------------------------------------------------------
# Stale and wedged holders never block forever
# ---------------------------------------------------------------------------

_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from vco_lib.atomic import exclusive_file_lock
    with exclusive_file_lock(Path(sys.argv[1])):
        Path(sys.argv[2]).write_text("held")
        time.sleep(600)
    """
)


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.02)


def test_a_dead_holder_does_not_block_the_next_ensure(state, tmp_path):
    """The kernel releases the lock with its holder: a crashed (SIGKILLed)
    ensure leaves no stale-lock state behind."""
    lock = el.ensure_lock_path("launcher")
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock), str(ready)], env=child_env(VCT_STATE_DIR=str(state)),
    )
    try:
        _wait_for(ready)
        if os.name == "nt":
            holder.kill()
        else:
            os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=20)
        started = time.monotonic()
        with el.spawn_lock("launcher", settle_s=30) as held:
            assert held is True
        assert time.monotonic() - started < 5, "a dead holder must not cost the waiter its bound"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=20)


def test_a_live_holder_past_the_bound_lets_the_waiter_proceed_unlocked(
    state, tmp_path, monkeypatch
):
    """A holder wedged inside its spawn must not leave the service down: the
    waiter proceeds without the lock (each service's own single-instance
    guard is the backstop) — held=False, and it does not wait forever."""
    monkeypatch.setattr(el, "LOCK_MARGIN_S", 0.3)
    lock = el.ensure_lock_path("hub")
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock), str(ready)], env=child_env(VCT_STATE_DIR=str(state)),
    )
    try:
        _wait_for(ready)
        started = time.monotonic()
        with el.spawn_lock("hub", settle_s=0) as held:
            assert held is False
        assert time.monotonic() - started < 5
    finally:
        holder.kill()
        holder.wait(timeout=20)


# ---------------------------------------------------------------------------
# Two concurrent ensures -> ONE spawn (launcher and hub, real processes)
# ---------------------------------------------------------------------------

# Each child: wait for the start signal, then run the REAL ensure_running with
# only the outside world faked through files — "running" is a marker file, the
# spawn appends one line to a counter and takes 0.5 s to "come up". Without the
# lock both children see "not running" inside that 0.5 s and both spawn.
_LAUNCHER_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from vco_lib import launcher_ensure as le
    d = Path(sys.argv[1]); binary = sys.argv[2]
    while not (d / "go").exists():
        time.sleep(0.005)
    def scanner(_name):
        return 4242 if (d / "running").exists() else None
    def spawner(argv, cwd):
        with open(d / "spawns", "a") as fh:
            fh.write("x\\n")
        time.sleep(0.5)
        (d / "running").write_text("up")
    res = le.ensure_running(
        scanner=scanner, pref_reader=lambda _k: None,
        env={"DISPLAY": ":0", le.LAUNCHER_BIN_ENV: binary}, system="Linux",
        spawner=spawner, repo_root=Path("/nonexistent-checkout-for-tests"),
    )
    print(res.state.value)
    """
)

_HUB_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from vco_lib import hub_ensure as he
    d = Path(sys.argv[1])
    while not (d / "go").exists():
        time.sleep(0.005)
    he.hub_pid = lambda: 4242 if (d / "running").exists() else None
    he.is_running = lambda: (d / "running").exists()
    def fake_spawn(binary, wait):
        with open(d / "spawns", "a") as fh:
            fh.write("x\\n")
        time.sleep(0.5)
        (d / "running").write_text("up")
        return he.EnsureResult(state=he.EnsureState.STARTED, binary=str(binary), reason="spawned")
    he._spawn = fake_spawn
    print(he.ensure_running(probe_repo_dist=False).state.value)
    """
)


def _good_launcher(tmp_path: Path) -> Path:
    binary = tmp_path / "vct-launcher"
    binary.write_bytes(b"ELF\x00" + le.HIDDEN_START_FLAG.encode() + b"\x00")
    binary.chmod(0o755)
    return binary


def _race(script: str, args: list[str], workdir: Path, state: Path, *, n: int = 2,
          extra_env: dict | None = None) -> tuple[list[str], int]:
    env = child_env(VCT_STATE_DIR=str(state))
    env.update(extra_env or {})
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(workdir), *args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(n)
    ]
    time.sleep(1.0)  # let every child import and park at the start line
    (workdir / "go").write_text("go")
    states = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        states.append(out.strip())
    spawns_file = workdir / "spawns"
    spawns = len(spawns_file.read_text().splitlines()) if spawns_file.exists() else 0
    return sorted(states), spawns


def test_two_concurrent_launcher_ensures_spawn_one_launcher(state, tmp_path):
    """THE ACT CASE: nothing running, two ensures at once -> exactly one spawn;
    the other waits on the lock, then finds the launcher running."""
    work = tmp_path / "race"
    work.mkdir()
    states, spawns = _race(_LAUNCHER_CHILD, [str(_good_launcher(tmp_path))], work, state)
    assert spawns == 1, f"two concurrent ensures must spawn ONE launcher, got {spawns}"
    assert states == ["running", "started"]


def test_two_concurrent_launcher_ensures_leave_a_running_launcher_alone(state, tmp_path):
    """THE LEAVE-ALONE CASE: a launcher already running -> no spawn at all."""
    work = tmp_path / "race"
    work.mkdir()
    (work / "running").write_text("up")
    states, spawns = _race(_LAUNCHER_CHILD, [str(_good_launcher(tmp_path))], work, state)
    assert spawns == 0
    assert states == ["running", "running"]


def test_two_concurrent_hub_ensures_spawn_one_hub(state, tmp_path):
    work = tmp_path / "race"
    work.mkdir()
    hub = tmp_path / "vct-hub"
    hub.write_text("#!/bin/sh\nexit 0\n")
    hub.chmod(0o755)
    states, spawns = _race(_HUB_CHILD, [], work, state, extra_env={"VCT_HUB_BIN": str(hub)})
    assert spawns == 1, f"two concurrent ensures must spawn ONE hub, got {spawns}"
    assert states == ["already_running", "started"]


def test_two_concurrent_hub_ensures_leave_a_running_hub_alone(state, tmp_path):
    work = tmp_path / "race"
    work.mkdir()
    (work / "running").write_text("up")
    hub = tmp_path / "vct-hub"
    hub.write_text("#!/bin/sh\nexit 0\n")
    hub.chmod(0o755)
    states, spawns = _race(_HUB_CHILD, [], work, state, extra_env={"VCT_HUB_BIN": str(hub)})
    assert spawns == 0
    assert states == ["already_running", "already_running"]


# ---------------------------------------------------------------------------
# Each ensure runs its spawn UNDER the lock and keeps it until the service is up
# ---------------------------------------------------------------------------


def _lock_is_held_elsewhere(service: str, root: Path | None = None) -> bool:
    """Probe from another thread (its own open file description)."""
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    out: list[bool] = []

    def probe() -> None:
        try:
            with exclusive_file_lock(el.ensure_lock_path(service, root), timeout_s=0):
                out.append(False)
        except LockTimeout:
            out.append(True)

    t = threading.Thread(target=probe)
    t.start()
    t.join(timeout=10)
    return out[0]


def test_the_launcher_spawn_runs_under_the_lock_until_the_launcher_shows_up(state, tmp_path):
    binary = _good_launcher(tmp_path)
    seen: dict = {}
    up = {"pid": None}
    polls = {"n": 0}

    def spawner(argv, cwd):
        seen["during_spawn"] = _lock_is_held_elsewhere("launcher")

    def scanner(_name):
        if "during_spawn" in seen:
            polls["n"] += 1
            if polls["n"] == 2:  # first poll after the spawn: still coming up
                seen["while_settling"] = _lock_is_held_elsewhere("launcher")
            if polls["n"] >= 3:
                up["pid"] = 4242
        return up["pid"]

    res = le.ensure_running(
        scanner=scanner, pref_reader=lambda _k: None,
        env={"DISPLAY": ":0", le.LAUNCHER_BIN_ENV: str(binary)}, system="Linux",
        spawner=spawner, repo_root=Path("/nonexistent-checkout-for-tests"), settle_s=30,
    )
    assert res.state is le.LauncherState.STARTED
    assert seen == {"during_spawn": True, "while_settling": True}
    assert _lock_is_held_elsewhere("launcher") is False, "released once the launcher answered"


def test_the_gateway_start_runs_under_the_lock(state, monkeypatch):
    """Gateway: the init-system start happens while the lock is held, and the
    lock is kept until the daemon's pid file names a live process."""
    from vco_lib import boot_service as bs

    monkeypatch.setattr(
        ge, "gateway_status",
        lambda **_k: ge.GatewayEnsureResult(state=ge.GatewayState.REGISTERED_NOT_RUNNING),
    )
    seen: dict = {}

    def start_if_registered(spec, **_k):
        seen["during_start"] = _lock_is_held_elsewhere("gateway", state)
        (state / bs.GATEWAY_PID_BASENAME).write_text(f"{os.getpid()}\n")
        return True, [["start"]], ""

    monkeypatch.setattr(bs, "start_if_registered", start_if_registered)
    res = ge.ensure_running(system="Linux", state_dir=state, settle_s=30)
    assert res.state is ge.GatewayState.STARTED
    assert seen == {"during_start": True}
    assert _lock_is_held_elsewhere("gateway", state) is False


def test_the_gateway_leave_alone_path_spawns_nothing_under_the_lock(state, monkeypatch):
    from vco_lib import boot_service as bs

    monkeypatch.setattr(
        ge, "gateway_status",
        lambda **_k: ge.GatewayEnsureResult(state=ge.GatewayState.RUNNING),
    )
    monkeypatch.setattr(
        bs, "start_if_registered",
        lambda *a, **k: pytest.fail("a running gateway must not be started"),
    )
    assert ge.ensure_running(system="Linux", state_dir=state).state is ge.GatewayState.RUNNING


def test_the_uninstaller_plans_the_ensure_locks(state, tmp_path):
    from vco_lib import uninstall_artifacts as ua

    paths = [el.ensure_lock_path(s) for s in el.SERVICES]
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    plan = ua.build_plan(os_name="Linux", home=tmp_path / "h", vct_root=state, env={})
    assert set(paths) <= {r.path for r in plan.remove}
