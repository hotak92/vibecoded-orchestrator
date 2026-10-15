# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home of "start the container ENGINE this install uses".

Owner ruling 2026-10-15 (after a real Windows install where Docker Desktop
had to be started by hand): the engine "should be made to auto-start with
launcher/hub/VS Code (if opening a VCO project)". Before v0.2.103 only the
install pre-flight started it — ``install.py::_try_start_docker_daemon``
(Docker Desktop on macOS/Windows) and the podman heal in
:mod:`vco_lib.compose_provider`. The SessionStart hooks, the hub and the
launcher all skipped when the resolver said ``absent``.

What lives here (one home, class A of the A>B>C rule — every caller is a
session/launcher/hub path where a subprocess is fine):

* :func:`try_start_docker_daemon` — the Docker Desktop starter, moved from
  ``install.py`` (which now delegates to it);
* :func:`heal_podman_reachability` — the podman start/heal, moved from
  ``install.py::_heal_podman_reachability`` (thin over
  :func:`vco_lib.compose_provider.heal_socket`);
* :func:`ensure_engine` — the idempotent entry the hooks, the hub and the
  launcher spawn as ``python -m vco_lib.container_engine_start ensure``:
  resolve the runtime, no-op when it is already up, start ONLY the engine
  the install uses (honouring a pin — a pinned-but-down runtime is started,
  never swapped, the same BLOCKER-4 rule the resolver enforces), soft-fail
  with one log line, honour the opt-out env
  :data:`VCT_CONTAINER_ENGINE_AUTOSTART`.

Non-blocking by design: the spawn-AND-wait lives in THIS detached CLI
process, never in the caller — the hooks, the hub and the launcher spawn
the CLI detached and return immediately. The bounded readiness wait
(``wait_s``) is liveness only: it stops polling and reports, it never kills
or cancels anything.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

from vco_lib import compose_provider as _cp
from vco_lib import containers as _c
from vco_lib.install_companions import detached_child_env

__all__ = ["VCT_CONTAINER_ENGINE_AUTOSTART", "autostart_enabled", "ensure_engine",
           "heal_podman_reachability", "try_start_docker_daemon", "main"]

#: Opt-out knob (v0.2.103). No earlier env disabled engine auto-start — the
#: ``--no-containers`` install choice is a one-shot CLI flag, and
#: ``VCT_DISABLE_HOOKS`` covers only the hooks — so this is a NEW, documented
#: knob (``docs/CONFIGURATION.md`` § Container engine auto-start). Any of the
#: falsy values below disables the starter on EVERY surface (the CLI checks
#: it, and the Rust spawners check the same variable before spawning the
#: CLI). Unset or any other value: enabled.
VCT_CONTAINER_ENGINE_AUTOSTART = "VCT_CONTAINER_ENGINE_AUTOSTART"

_FALSY = {"0", "false", "no", "off"}

LogFn = Callable[[str], None]
RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
WhichFn = Callable[[str], Optional[str]]

#: Handle keep-alive for children that outlive us (the same ResourceWarning
#: fix install.py had: suppresses the Popen destructor's warning without
#: waiting on the child).
_DETACHED_CHILDREN: list = []


def _default_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def autostart_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True unless :data:`VCT_CONTAINER_ENGINE_AUTOSTART` is set to a falsy
    value. The Rust spawners mirror this check (env NAME + falsy set) before
    spawning the CLI — keep them in step."""
    source = env if env is not None else os.environ
    return (source.get(VCT_CONTAINER_ENGINE_AUTOSTART, "") or "").strip().lower() not in _FALSY


def try_start_docker_daemon(
    *,
    run: Optional[RunFn] = None,
    popen: Optional[Callable[..., subprocess.Popen]] = None,
    reachable: Optional[Callable[[], bool]] = None,
    system: Optional[str] = None,
    which: Optional[WhichFn] = None,
    env: Optional[Mapping[str, str]] = None,
    log: Optional[LogFn] = None,
    wait_s: float = 60.0,
    poll_sleep_s: float = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Best-effort auto-start of the Docker daemon (v0.2.54 container-scout
    C-RT-7; moved from ``install.py`` to this ONE home in v0.2.103).

    OS matrix (unchanged from the install.py original):
      - macOS:   ``open -a Docker`` — launches Docker Desktop. No sudo,
                 no GUI prompt beyond Docker Desktop's own startup.
      - Windows: shell-start ``Docker Desktop.exe`` from its canonical
                 install locations.
      - Linux:   NOT automated. ``sudo systemctl start docker`` needs an
                 interactive password we don't shoulder outside an
                 interactive pre-flight. Returns False with the manual
                 recipe.

    After a successful launch attempt, polls the daemon for up to
    ``wait_s`` (Docker Desktop's VM boot is slow) — liveness only, never a
    cap that kills work. Soft-fail throughout. All probes are injectable so
    the unit tests never touch a real engine.
    """
    _run = run or subprocess.run
    _popen = popen or subprocess.Popen
    _reachable = reachable or (lambda: _cp.runtime_reachable("docker", run=run, which=which))
    _system = system or platform.system()
    _env = env if env is not None else os.environ
    os_name = _system
    _which = which or shutil.which
    if not _which("docker"):
        return False, "docker binary not on PATH"

    if os_name == "Darwin":
        try:
            result = _run(
                ["open", "-a", "Docker"],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, f"`open -a Docker` failed: {e}"
        if result.returncode != 0:
            return False, (
                f"`open -a Docker` exited {result.returncode}: "
                f"{(result.stderr or '').strip()[:200]}"
            )
    elif os_name == "Windows":
        candidates = [
            Path(_env.get("ProgramFiles", r"C:\Program Files"))
            / "Docker" / "Docker" / "Docker Desktop.exe",
            Path(_env.get("LOCALAPPDATA", "")) / "Docker" / "Docker Desktop.exe",
        ]
        exe = next((c for c in candidates if c.is_file()), None)
        if exe is None:
            return False, (
                "Docker Desktop.exe not found at the canonical install "
                "locations; start Docker Desktop from the Start Menu."
            )
        try:
            # Detached start — Docker Desktop is a GUI app; don't block.
            # Handle kept in _DETACHED_CHILDREN: suppresses the Popen
            # destructor's ResourceWarning for a child that outlives us.
            _DETACHED_CHILDREN.append(
                _popen(
                    ["cmd", "/c", "start", "", str(exe)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=detached_child_env(),
                )
            )
        except OSError as e:
            return False, f"could not launch Docker Desktop: {e}"
    else:
        return False, (
            "Docker daemon auto-start is not automated on Linux "
            "(requires sudo): run `sudo systemctl start docker`."
        )

    if wait_s <= 0:
        return True, "Docker Desktop launch attempted"
    deadline = monotonic() + wait_s
    while monotonic() < deadline:
        if _reachable():
            return True, "daemon responsive"
        sleep(poll_sleep_s)
    return False, f"docker daemon did not become responsive within {wait_s:.0f}s"


def heal_podman_reachability(
    *,
    run: Optional[RunFn] = None,
    which: Optional[WhichFn] = None,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    exists: Optional[Callable[[str], bool]] = None,
    machine_start: Optional[Callable[[], tuple[bool, str]]] = None,
    daemon_responsive: Optional[Callable[[str], object]] = None,
) -> "_cp.HealResult":
    """Start / heal podman's reachability — moved from
    ``install.py::_heal_podman_reachability`` in v0.2.103 (thin over
    :func:`vco_lib.compose_provider.heal_socket`): Linux ``systemctl --user
    restart podman.socket`` when the unit is active but the socket FILE is
    gone, ``start`` when it is down; macOS / Windows the podman machine
    init-if-absent + start. Then ``podman info`` must answer (folded into
    the REAL HealResult, W1R-03)."""
    from vco_lib.compose_recovery import CID_SOCKET_HEAL_FAILED  # noqa: PLC0415 — lazy, as in install.py

    res = _cp.heal_socket(
        "podman", run=run, which=which, env=env, system=system, exists=exists,
        machine_start=machine_start or _cp.podman_machine_init_and_start,
    )
    if res.healed:
        responsive = (daemon_responsive or (lambda rt: _c.daemon_responsive(rt, run=run, which=which)))("podman")
        if responsive is not True:
            return _cp.HealResult(
                False, res.actions,
                f"{res.reason}, but `podman info` still does not answer",
                CID_SOCKET_HEAL_FAILED, res.details,
            )
    return res


def ensure_engine(
    *,
    env: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    which: Optional[WhichFn] = None,
    run: Optional[RunFn] = None,
    resolve_fn: Optional[Callable[[], "_c.RuntimeResolution"]] = None,
    start_docker: Optional[Callable[[], tuple[bool, str]]] = None,
    heal_podman: Optional[Callable[[], "_cp.HealResult"]] = None,
    log: Optional[LogFn] = None,
    wait_s: float = 0.0,
) -> tuple[bool, str]:
    """Start the container engine THIS install uses, if it is down.

    Contract (owner ruling 2026-10-15):
      * idempotent — the engine already answering (resolver state RESOLVED)
        is a no-op returning ``(True, "engine already up")``;
      * only the install's engine — the resolver's pin
        (``VCT_CONTAINER_RUNTIME`` / ``state/install/runtime.txt``) is
        honoured: a pinned-but-down runtime is started, the OTHER runtime is
        never started in its place (BLOCKER-4, same rule as everywhere);
      * soft-fail — every refusal and every failed start returns
        ``(False, detail)`` and logs ONE line, never raises;
      * opt-out — :data:`VCT_CONTAINER_ENGINE_AUTOSTART` falsy → no-op
        before any probe;
      * non-blocking — this function only LAUNCHES (the bounded liveness
        wait ``wait_s`` defaults to 0 here; the detached CLI passes its own
        ``--wait`` so the wait happens in the CLI process, not the caller).

    Returns ``(ok, detail)`` where ``ok`` is True for "up (already or
    started)" and False for every no-op-with-reason and every failure.
    """
    _log = log or (lambda msg: None)  # silent by default; the CLI logs
    _env = env if env is not None else os.environ
    if not autostart_enabled(_env):
        return False, f"engine auto-start disabled via {VCT_CONTAINER_ENGINE_AUTOSTART}"
    if resolve_fn is None:
        def _default_resolve() -> "_c.RuntimeResolution":
            return _c.resolve(env=_env, which=which, run=run)

        resolve_fn = _default_resolve
    res = resolve_fn()
    if res.state is _c.RuntimeState.RESOLVED:
        return True, "engine already up; nothing to start"
    if res.state is _c.RuntimeState.UNKNOWN:
        return False, "runtime state unknown (a probe could not run); not starting anything"
    # ABSENT: the engine may merely be DOWN. Start the one this install
    # uses — the pin when one is set and installed, else the first runtime
    # binary on PATH. A pin whose binary is missing is never swapped for
    # the other runtime.
    if res.requested is not None:
        if not res.requested_installed:
            return False, (f"pinned runtime {res.requested!r} is not installed; "
                           "not starting the other one")
        target = res.requested
    elif res.installed:
        target = res.installed
    else:
        return False, "no container engine installed; nothing to start"

    if target == "docker":
        if start_docker is not None:
            ok, detail = start_docker()
        else:
            ok, detail = try_start_docker_daemon(
                run=run, which=which, env=_env, system=system, log=_log, wait_s=wait_s)
    else:
        if heal_podman is not None:
            healed = heal_podman()
        else:
            healed = heal_podman_reachability(run=run, which=which, env=_env, system=system)
        ok, detail = healed.healed, healed.reason
    if not ok:
        _log(f"container engine auto-start ({target}) failed: {detail}")
    return ok, detail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m vco_lib.container_engine_start ensure [--wait S] [--json]``.

    Designed to be spawned DETACHED by the session hooks, the hub and the
    launcher: it does its own (bounded) waiting so the caller never blocks,
    and always exits 0 — best-effort by contract, the outcome is the log
    line / JSON on stdout.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.container_engine_start",
        description="Start the container engine this install uses, if it is down "
                    "(idempotent, soft-fail; honours "
                    f"{VCT_CONTAINER_ENGINE_AUTOSTART}=0 to disable).")
    sub = parser.add_subparsers(dest="verb", required=True)
    p_ensure = sub.add_parser(
        "ensure", help="Start the engine this install uses when it is down (no-op when up).")
    p_ensure.add_argument(
        "--wait", type=float, default=60.0, metavar="SECONDS",
        help="Bounded liveness wait AFTER a Docker Desktop launch attempt "
             "(default 60; 0 = launch and return). Liveness only — nothing is "
             "killed when the wait ends.")
    p_ensure.add_argument("--json", action="store_true", help="Print the outcome as JSON.")
    args = parser.parse_args(argv)

    if args.verb == "ensure":
        ok, detail = ensure_engine(wait_s=max(0.0, args.wait), log=_default_log)
        if args.json:
            print(json.dumps({"ok": ok, "detail": detail}, sort_keys=True))
        else:
            state = "up" if ok else "not started"
            print(f"container-engine-start: {state} — {detail}")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
