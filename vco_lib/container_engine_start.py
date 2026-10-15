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
  the install RECORDED (review R19 F1: the persisted ``--no-containers``
  choice in ``state/install-manifest.json`` refuses outright, and with no
  recorded runtime at all nothing is started — no guessing from PATH, never
  an unattended ``podman machine init``), soft-fail with one log line, and
  honour the opt-outs: the env knob
  :data:`VCT_CONTAINER_ENGINE_AUTOSTART` and the launcher Preferences
  toggle (:data:`APP_STATE_ENGINE_AUTOSTART` in launcher.db, read through
  the same read-only reader ``launcher_ensure`` uses).

Single-flight (review R19 F2): the CLI holds ONE per-user claim
(:func:`vco_lib.atomic.exclusive_file_lock` on
``<vct_root>/state/engine-start.lock``, non-blocking) for its whole run, so
the launcher, the hub and N concurrent sessions that all spawn it during an
engine boot produce exactly ONE starter — the losers report "already in
progress" and exit. The kernel releases the claim when the holder dies, so
no TTL is needed.

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
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

from vco_lib import compose_provider as _cp
from vco_lib import containers as _c
from vco_lib import tool_search_dirs as _tsd
from vco_lib.atomic import exclusive_file_lock
from vco_lib.install_companions import detached_child_env
from vco_lib.paths import vct_root_dir

__all__ = ["VCT_CONTAINER_ENGINE_AUTOSTART", "APP_STATE_ENGINE_AUTOSTART",
           "DEFAULT_ENGINE_AUTOSTART", "autostart_enabled", "pref_enabled",
           "install_skipped_containers", "ensure_engine", "engine_start_lock_path",
           "heal_podman_reachability", "try_start_docker_daemon", "main"]

#: Opt-out knob 1 (v0.2.103), the env override. No earlier env disabled
#: engine auto-start, so this is a NEW, documented knob
#: (``docs/CONFIGURATION.md`` § Container engine auto-start). Any of the
#: falsy values below disables the starter on EVERY surface (the CLI checks
#: it, and the Rust spawners check the same variable before spawning the
#: CLI). Unset or any other value: enabled. Either opt-out is enough: this
#: env var can only turn auto-start OFF — it cannot re-enable it over a
#: Preferences toggle that is off.
VCT_CONTAINER_ENGINE_AUTOSTART = "VCT_CONTAINER_ENGINE_AUTOSTART"

#: Opt-out knob 2 (review R19 F4), the Preferences toggle: "Start the
#: container engine automatically" (default ON). Stored in launcher.db
#: ``app_state``; read here through the same soft-fail read-only reader
#: ``vco_lib.launcher_ensure`` uses, so a GUI-started launcher/hub (which
#: never sees ``.claude/env``) is covered too.
#:
#: MUST MATCH ``launcher/src-tauri/src/commands/engine_autostart_pref.rs::
#: APP_STATE_ENGINE_AUTOSTART`` (the launcher WRITES the row, this module
#: READS it — pinned by tests/test_container_engine_start.py).
APP_STATE_ENGINE_AUTOSTART = "engine.autostart"

#: Shipped default: ON (the owner ruling this whole module implements).
DEFAULT_ENGINE_AUTOSTART = True

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


#: Win32 ``CREATE_NO_WINDOW`` — the child gets a hidden console (which ITS
#: children then inherit) instead of a fresh visible one. Review R19 F3.
CREATE_NO_WINDOW = 0x0800_0000


def _windows_no_window_kwargs() -> dict:
    """``{}`` on POSIX; ``{"creationflags": CREATE_NO_WINDOW}`` on Windows —
    the kwargs shape every console child this module spawns directly wants."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def autostart_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True unless :data:`VCT_CONTAINER_ENGINE_AUTOSTART` is set to a falsy
    value. The Rust spawners mirror this check (env NAME + falsy set) before
    spawning the CLI — keep them in step."""
    source = env if env is not None else os.environ
    return (source.get(VCT_CONTAINER_ENGINE_AUTOSTART, "") or "").strip().lower() not in _FALSY


def pref_enabled(reader: Optional[Callable[[str], Optional[str]]] = None) -> bool:
    """The Preferences toggle (:data:`APP_STATE_ENGINE_AUTOSTART`), defaulting
    to :data:`DEFAULT_ENGINE_AUTOSTART`.

    An absent row, an unreadable ``launcher.db`` (fresh install, never
    booted) or any SQLite error reads as the DEFAULT rather than as "off" —
    the shipped behaviour must not depend on a database being present. The
    truthiness rule (``"true"``/``"1"``) MUST MATCH the Rust writer
    (``app_state_set_bool``) — the same pin ``launcher_ensure`` uses.
    """
    if reader is None:
        from vco_lib.launcher_db_reader import read_app_state_value  # noqa: PLC0415 — lazy: sqlite only when asked

        reader = read_app_state_value
    try:
        raw = reader(APP_STATE_ENGINE_AUTOSTART)
    except Exception:  # noqa: BLE001 — an unreadable pref is not an opt-out
        return DEFAULT_ENGINE_AUTOSTART
    if raw is None:
        return DEFAULT_ENGINE_AUTOSTART
    return raw.strip() in ("true", "1")


def install_skipped_containers(root: Optional[Path] = None) -> bool:
    """True when the install recorded ``--no-containers`` (review R19 F1).

    Reads ``<install_root>/state/install-manifest.json`` ``skipped.containers``
    (written by ``install.py::_write_install_manifest`` on EVERY install
    path). Unreadable / missing / off → ``False`` (soft-fail: a manifest a
    fresh install has not written yet must not look like an opt-out).
    """
    if root is None:
        from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

        root = resolve_install_root()
    if root is None:
        return False
    try:
        with (Path(root) / "state" / "install-manifest.json").open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return bool(data.get("skipped", {}).get("containers"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


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

    v0.2.103 review fixes: the default ``which`` is the resolver's own
    :func:`vco_lib.tool_search_dirs.which` (R19 F5 — it also searches the
    usual install locations, so a short-PATH graphical launch that the
    resolver says "installed" does not get "not on PATH" here), the
    LOCALAPPDATA candidate is skipped when the variable is unset (F8 — it
    used to become a cwd-relative path), and the Windows ``cmd`` child is
    spawned with ``CREATE_NO_WINDOW`` (F3) so no console flashes when this
    module runs windowless.
    """
    _run = run or subprocess.run
    _popen = popen or subprocess.Popen
    _reachable = reachable or (lambda: _cp.runtime_reachable("docker", run=run, which=which))
    _system = system or platform.system()
    _env = env if env is not None else os.environ
    os_name = _system
    # F5: the SAME binary lookup the resolver uses — one home, not a
    # shutil.which that only sees PATH.
    _which = which or _tsd.which
    if not _which("docker"):
        return False, "docker binary not found (PATH or the usual install locations)"

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
            Path(_env.get("ProgramFiles") or r"C:\Program Files")
            / "Docker" / "Docker" / "Docker Desktop.exe",
        ]
        # F8: an unset LOCALAPPDATA used to turn this into a cwd-relative
        # "Docker/Docker Desktop.exe" probe — skip the candidate instead.
        localappdata = (_env.get("LOCALAPPDATA") or "").strip()
        if localappdata:
            candidates.append(Path(localappdata) / "Docker" / "Docker Desktop.exe")
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
            # F3: CREATE_NO_WINDOW on the console child (``cmd``) so a
            # windowless parent (the Rust spawn) never flashes a console.
            _DETACHED_CHILDREN.append(
                _popen(
                    ["cmd", "/c", "start", "", str(exe)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=detached_child_env(),
                    **_windows_no_window_kwargs(),
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
    pref_reader: Optional[Callable[[str], Optional[str]]] = None,
    manifest_reader: Optional[Callable[[], bool]] = None,
    update_in_progress: Optional[Callable[[], bool]] = None,
) -> tuple[bool, str]:
    """Start the container engine THIS install RECORDED, if it is down.

    Contract (owner ruling 2026-10-15 + review R19):
      * idempotent — the engine already answering (resolver state RESOLVED)
        is a no-op returning ``(True, "engine already up")``;
      * only a RECORDED engine (R19 F1) — starting requires the install to
        have recorded a runtime (``state/install/runtime.txt`` — written by every
        install that detected a runtime — or the ``VCT_CONTAINER_RUNTIME``
        env pin). With NO recorded runtime nothing is started, never the
        first binary on PATH, and never an unattended ``podman machine
        init``; and an install that chose ``--no-containers`` (persisted in
        ``state/install-manifest.json``) gets nothing started either. A
        recorded-but-down runtime IS started, the other runtime never is
        (BLOCKER-4, same rule as everywhere);
      * soft-fail — every refusal and every failed start returns
        ``(False, detail)`` and logs ONE line, never raises;
      * opt-out — the env override :data:`VCT_CONTAINER_ENGINE_AUTOSTART`
        (falsy) or the Preferences toggle
        (:func:`pref_enabled`, launcher.db) off → no-op before any probe;
      * update gate (R19 F7) — a fresh ``.update-in-progress.json`` (the
        SAME lockfile the hooks honour) means no-op;
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
    if not pref_enabled(pref_reader):
        return False, ("engine auto-start disabled by the Preferences toggle "
                       f"({APP_STATE_ENGINE_AUTOSTART} in launcher.db)")
    if update_in_progress is None:
        from vco_lib.update_gate import is_update_in_progress  # noqa: PLC0415 — lazy

        update_in_progress = is_update_in_progress
    if update_in_progress():
        return False, "an orchestrator update is in progress; engine auto-start skipped"
    if manifest_reader is None:
        manifest_reader = install_skipped_containers
    if manifest_reader():
        return False, ("this install chose --no-containers "
                       "(state/install-manifest.json); nothing is started")
    if resolve_fn is None:
        def _default_resolve() -> "_c.RuntimeResolution":
            return _c.resolve(env=_env, which=which, run=run)

        resolve_fn = _default_resolve
    res = resolve_fn()
    if res.state is _c.RuntimeState.RESOLVED:
        return True, "engine already up; nothing to start"
    if res.state is _c.RuntimeState.UNKNOWN:
        return False, "runtime state unknown (a probe could not run); not starting anything"
    # ABSENT: the engine may merely be DOWN. R19 F1: starting requires a
    # RECORDED runtime — the resolver's pin (runtime.txt record or the env
    # pin, both explicit user choices). No pin → nothing is started (no
    # guessing from PATH); a pin whose binary is missing is never swapped
    # for the other runtime.
    if res.requested is None:
        return False, ("no runtime recorded for this install (state/install/runtime.txt "
                       "or VCT_CONTAINER_RUNTIME); not guessing an engine to start")
    if not res.requested_installed:
        return False, (f"recorded runtime {res.requested!r} is not installed; "
                       "not starting the other one")
    target = res.requested

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


def engine_start_lock_path(root: Optional[Path] = None) -> Path:
    """The per-user single-flight claim path (R19 F2):
    ``<vct_root>/state/engine-start.lock``."""
    return (root or vct_root_dir()) / "state" / "engine-start.lock"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m vco_lib.container_engine_start ensure [--wait S] [--json]``.

    Designed to be spawned DETACHED by the session hooks, the hub and the
    launcher: it does its own (bounded) waiting so the caller never blocks,
    and always exits 0 — best-effort by contract, the outcome is the log
    line / JSON on stdout.

    Single-flight (R19 F2): the whole run holds ONE per-user claim — a
    NON-blocking :func:`vco_lib.atomic.exclusive_file_lock` on
    :func:`engine_start_lock_path`. The launcher, the hub and every session
    hook can all spawn this CLI while an engine is still booting; exactly
    one of them starts the engine, the rest report "already in progress"
    and exit 0. The claim is a kernel-level flock/msvcrt lock released when
    the holder exits (even on crash), so there is no TTL to tune.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.container_engine_start",
        description="Start the container engine this install recorded, if it is down "
                    "(idempotent, soft-fail; honours "
                    f"{VCT_CONTAINER_ENGINE_AUTOSTART}=0 and the launcher's "
                    "engine-autostart Preferences toggle to disable).")
    sub = parser.add_subparsers(dest="verb", required=True)
    p_ensure = sub.add_parser(
        "ensure", help="Start the recorded engine when it is down (no-op when up).")
    p_ensure.add_argument(
        "--wait", type=float, default=60.0, metavar="SECONDS",
        help="Bounded liveness wait AFTER a Docker Desktop launch attempt "
             "(default 60; 0 = launch and return). Liveness only — nothing is "
             "killed when the wait ends.")
    p_ensure.add_argument("--json", action="store_true", help="Print the outcome as JSON.")
    args = parser.parse_args(argv)

    if args.verb == "ensure":
        from vco_lib.atomic import LockTimeout  # noqa: PLC0415 — lazy

        try:
            with exclusive_file_lock(engine_start_lock_path(), timeout_s=0.1):
                ok, detail = ensure_engine(wait_s=max(0.0, args.wait), log=_default_log)
        except LockTimeout:
            # Another CLI run (launcher / hub / a sibling session) is holding
            # the claim — it is already starting the engine. The loser's
            # correct behaviour is to stand down, not to race it.
            ok, detail = False, "an engine start is already in progress; standing down"
        if args.json:
            print(json.dumps({"ok": ok, "detail": detail}, sort_keys=True))
        else:
            state = "up" if ok else "not started"
            print(f"container-engine-start: {state} — {detail}")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
