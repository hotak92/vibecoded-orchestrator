# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Uninstall step [3]: stop what holds the VCO state dir open — BEFORE any
state is removed.

Field run 2026-10-16 (Windows 11): the uninstaller unlinked ``launcher.db``
in step [3] while ``vct-hub`` (and the launcher GUI) still held it open; the
hub was only stopped in step [7]. Windows refuses to delete an open file, so
the unlink failed with a WARN and the stale database — old projects, old
install root — survived into the next install.

Two processes, two identification rules, neither a bare name match:

* **vct-hub** — VCO's own lockfile (``hub.pid``, read by
  :func:`vco_lib.hub_ensure.is_running`) says whether it runs; it is stopped
  with its own ``vct-hub --stop`` (lockfile-driven). :func:`stop_hub` is the
  ONE implementation; uninstall step [7] calls it too.
* **the launcher GUI** — it records no pid, so a process counts as VCO's
  launcher only when its executable IS one of the exact paths VCO installs the
  launcher to (:func:`vco_lib.uninstall_artifacts.launcher_exe_paths`). A
  process merely NAMED ``vct-launcher`` running from anywhere else is
  reported and never touched. Interactive: the user is asked to close it, and
  only a still-running, positively-identified launcher is offered for
  stopping. Non-interactive (``--yes``): a positively-identified launcher is
  stopped.
* **the model gateway** (``vct-model-gateway``, a host process run from the
  VCO venv; it keeps ``logs/model-gateway.log`` and its pid/port/token files
  open) — identified by BOTH the daemon's own pid file (``model-gateway.pid``,
  read by :func:`vco_lib.gateway_ensure.gateway_pid`) naming a live pid AND
  that process's command line being one of the argvs VCO starts the gateway
  with (:func:`vco_lib.boot_service.gateway_exec_candidates` — the console
  script in the install venv, or ``<venv python> -m model_router``). It has no
  shutdown endpoint, so it is terminated (SIGTERM — its graceful shutdown —
  or ``TerminateProcess``) after the command line is re-checked. Interactive:
  asked first; ``--yes`` stops it. A pid file naming any other process is
  reported, never acted on.

Never raises; every outcome is an audit line.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

__all__ = [
    "ProcInfo", "GatewayProc", "stop_hub", "find_launchers", "stop_launchers",
    "is_vco_gateway", "find_gateway", "stop_gateway", "plan_lines",
]

Runner = Callable[..., Optional[int]]
#: ``(pid, exe)`` -> True when the process is gone afterwards.
Stopper = Callable[[int, Path], bool]

#: Process names a launcher GUI shows (Windows image name / POSIX comm /
#: macOS bundle executable) — used to FIND candidates, never to decide.
_LAUNCHER_NAMES = frozenset({"vct-launcher", "vct-launcher.exe", "vct launcher"})


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str
    exe: Optional[Path]  # None when the OS would not say (access denied)


def _norm(p: Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(p))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(p))


# --------------------------------------------------------------------------
# vct-hub
# --------------------------------------------------------------------------

def _default_hub_running() -> bool:
    try:
        from vco_lib import hub_ensure

        return hub_ensure.is_running()
    except Exception:  # noqa: BLE001 — unknown reads as not running; removal reports locks
        return False


def _default_runner(cmd: Sequence[str], timeout: int = 15) -> Optional[int]:
    from vco_lib.boot_service import run_quiet

    return run_quiet(cmd, timeout=timeout)


def stop_hub(
    binaries: Iterable[Optional[Path]],
    *,
    hub_running: Optional[Callable[[], bool]] = None,
    runner: Optional[Runner] = None,
    sleep: Callable[[float], None] = time.sleep,
    wait_seconds: float = 10.0,
    report_idle: bool = True,
) -> list[str]:
    """Stop a running vct-hub with the first existing binary's ``--stop`` and
    wait (up to ``wait_seconds``) for its lockfile owner to exit."""
    is_running = hub_running or _default_hub_running
    if not is_running():
        return ["vct-hub not running"] if report_idle else []
    hubs = [Path(p) for p in binaries if p is not None and Path(p).is_file()]
    if not hubs:
        return ["WARN: vct-hub is running but no vct-hub binary was found to stop it — "
                "run `vct-hub --stop` (or end the vct-hub process), then re-run the uninstall"]
    hub = hubs[0]
    rc = (runner or _default_runner)([str(hub), "--stop"], timeout=30)
    if rc != 0:
        why = "could not be spawned / timed out" if rc is None else f"exited {rc}"
        return [f"WARN: `{hub} --stop` {why}"]
    waited = 0.0
    while is_running() and waited < wait_seconds:
        sleep(0.25)
        waited += 0.25
    if is_running():
        return [f"WARN: `{hub} --stop` returned but vct-hub is still running after {wait_seconds:.0f}s"]
    return [f"stopped vct-hub via `{hub} --stop`"]


# --------------------------------------------------------------------------
# launcher GUI
# --------------------------------------------------------------------------

def _default_lister() -> list[ProcInfo]:
    try:
        import psutil
    except ImportError:  # psutil is a declared dependency: a broken install
        raise RuntimeError("psutil is not importable — the VCO venv is broken; "
                           "re-run the installer") from None
    out: list[ProcInfo] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        info = proc.info
        exe = info.get("exe")
        out.append(ProcInfo(int(info["pid"]), str(info.get("name") or ""),
                            Path(exe) if exe else None))
    return out


def _terminate_if(pid: int, still_ours: Callable[..., bool], *, grace: float = 10.0) -> bool:
    """Terminate ``pid`` only if ``still_ours(proc)`` holds at that moment
    (guards a pid reused since it was identified). True when it is gone."""
    import psutil

    try:
        proc = psutil.Process(pid)
        if not still_ours(proc):
            return not proc.is_running()
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return True
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError):
        return False


def _default_stopper(pid: int, exe: Path) -> bool:
    """Terminate ``pid`` only if it STILL runs from ``exe`` (pid reuse)."""
    def ours(proc) -> bool:
        current = proc.exe()
        return bool(current) and _norm(Path(current)) == _norm(exe)

    return _terminate_if(pid, ours)


def find_launchers(
    locations: Iterable[Path],
    *,
    lister: Optional[Callable[[], list[ProcInfo]]] = None,
) -> tuple[list[ProcInfo], list[ProcInfo]]:
    """``(vco, unidentified)``: launcher-looking processes whose executable is
    one of ``locations``, and those that are not (or would not say).

    Raises when the process table cannot be read; callers report that (a
    launcher that cannot be seen is never assumed to be closed)."""
    exact = {_norm(p) for p in locations}
    procs = (lister or _default_lister)()  # raises when it cannot look
    own = os.getpid()
    vco: list[ProcInfo] = []
    other: list[ProcInfo] = []
    for p in procs:
        if p.pid == own:
            continue
        if p.exe is not None and _norm(p.exe) in exact:
            vco.append(p)
        elif p.name.casefold() in _LAUNCHER_NAMES or (
            p.exe is not None and p.exe.name.casefold() in _LAUNCHER_NAMES
        ):
            other.append(p)
    return vco, other


def _describe(p: ProcInfo) -> str:
    return f"pid {p.pid}, {p.exe if p.exe is not None else 'executable unknown'}"


def stop_launchers(
    locations: Sequence[Path],
    *,
    interactive: bool,
    confirm: Callable[[str], bool],
    wait_for_user: Callable[[str], object],
    lister: Optional[Callable[[], list[ProcInfo]]] = None,
    stopper: Optional[Stopper] = None,
) -> list[str]:
    """Make sure no VCO launcher GUI is running. Audit lines."""
    try:
        vco, other = find_launchers(locations, lister=lister)
    except Exception as exc:  # noqa: BLE001 — reported, never assumed closed
        return [f"WARN: could not list running processes ({exc}) — close the VCT "
                "Launcher yourself before the state dir is removed"]
    audit = [
        f"WARN: a process named like the launcher is running ({_describe(p)}) but its "
        "executable is not one of VCO's launcher paths — not stopped; close it yourself "
        "if it is VCO's launcher"
        for p in other
    ]
    if not vco:
        return audit + ["VCO launcher not running"]
    if interactive:
        wait_for_user(
            "The VCT Launcher is running ("
            + "; ".join(_describe(p) for p in vco)
            + "). Close it (tray icon -> Quit) — closed it?"
        )  # whatever the answer, the process table is re-checked
        try:
            vco, _ = find_launchers(locations, lister=lister)
        except Exception as exc:  # noqa: BLE001
            return audit + [f"WARN: could not re-check running processes ({exc})"]
        if not vco:
            return audit + ["VCO launcher closed by the user"]
        if not confirm("The VCT Launcher is still running — stop it now?"):
            return audit + ["WARN: VCO launcher left running (declined) — files it holds "
                            "open may not be removable"]
    stop = stopper or _default_stopper
    for p in vco:
        assert p.exe is not None  # identified by exe
        if stop(p.pid, p.exe):
            audit.append(f"stopped VCO launcher ({_describe(p)})")
        else:
            audit.append(f"WARN: could not stop VCO launcher ({_describe(p)}) — close it by hand")
    return audit


# --------------------------------------------------------------------------
# model gateway
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GatewayProc:
    pid: int
    #: ``None`` when the OS would not say (access denied).
    cmdline: Optional[tuple[str, ...]]
    exe: Optional[Path] = None


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


def is_vco_gateway(proc: GatewayProc, candidates: Sequence[Sequence[str]]) -> bool:
    """Does ``proc`` run one of VCO's gateway argvs?

    * console-script candidate ``[script]``: the script is the executable or
      one of the command-line tokens (a POSIX shebang runs
      ``<venv python> <script> serve``; Windows runs ``<script>.exe serve``);
    * module candidate ``[python, "-m", "model_router"]``: the command line
      starts with that interpreter followed by ``-m model_router``.
    """
    if not proc.cmdline:
        return False
    toks = list(proc.cmdline)
    for cand in candidates:
        cand = list(cand)
        if len(cand) == 1:
            if (proc.exe is not None and _same_path(str(proc.exe), cand[0])) or any(
                _same_path(t, cand[0]) for t in toks
            ):
                return True
        elif len(cand) == 3 and cand[1] == "-m":
            if len(toks) >= 3 and _same_path(toks[0], cand[0]) and toks[1:3] == cand[1:]:
                return True
    return False


def _default_gateway_reader() -> Optional[GatewayProc]:
    """The live process the daemon's own pid file names, or ``None``."""
    from vco_lib import gateway_ensure

    pid = gateway_ensure.gateway_pid()
    if pid is None or not gateway_ensure.is_running():
        return None
    import psutil

    try:
        proc = psutil.Process(pid)
        exe = proc.exe()
        return GatewayProc(pid, tuple(proc.cmdline()), Path(exe) if exe else None)
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError):
        return GatewayProc(pid, None)


def _default_candidates(install_root: Optional[Path]) -> list[list[str]]:
    from vco_lib.boot_service import gateway_exec_candidates

    return gateway_exec_candidates(install_root=install_root)


def _default_gateway_stopper(pid: int, candidates: Sequence[Sequence[str]]) -> bool:
    def ours(proc) -> bool:
        exe = proc.exe()
        return is_vco_gateway(
            GatewayProc(pid, tuple(proc.cmdline()), Path(exe) if exe else None), candidates)

    # its graceful shutdown drains for up to 60 s; an uninstall waits 15
    return _terminate_if(pid, ours, grace=15.0)


def find_gateway(
    *,
    install_root: Optional[Path] = None,
    candidates: Optional[Sequence[Sequence[str]]] = None,
    reader: Optional[Callable[[], Optional[GatewayProc]]] = None,
) -> tuple[Optional[GatewayProc], Optional[str], list[list[str]]]:
    """``(proc, why_not_ours, candidates)``: ``proc`` is the live process the
    pid file names (``None`` = not running); ``why_not_ours`` is ``None`` only
    when it is positively VCO's gateway. Raises when it cannot look."""
    cands = [list(c) for c in (candidates if candidates is not None
                               else _default_candidates(install_root))]
    proc = (reader or _default_gateway_reader)()
    if proc is None:
        return None, None, cands
    if proc.cmdline is None:
        return proc, "its command line cannot be read", cands
    if not is_vco_gateway(proc, cands):
        return proc, f"it runs `{' '.join(proc.cmdline)[:200]}`, not VCO's gateway", cands
    return proc, None, cands


def stop_gateway(
    *,
    interactive: bool,
    confirm: Callable[[str], bool],
    install_root: Optional[Path] = None,
    candidates: Optional[Sequence[Sequence[str]]] = None,
    reader: Optional[Callable[[], Optional[GatewayProc]]] = None,
    stopper: Optional[Callable[[int, Sequence[Sequence[str]]], bool]] = None,
    report_idle: bool = True,
) -> list[str]:
    """Stop VCO's model gateway when positively identified. Audit lines."""
    try:
        proc, why, cands = find_gateway(install_root=install_root, candidates=candidates, reader=reader)
    except Exception as exc:  # noqa: BLE001 — reported, never assumed stopped
        return [f"WARN: could not check the model gateway ({exc}) — stop it yourself "
                "before the state dir is removed"]
    if proc is None:
        return ["model gateway not running"] if report_idle else []
    if why is not None:
        return [f"WARN: model-gateway.pid names pid {proc.pid}, but {why} — not stopped"]
    if interactive and not confirm(
            f"Stop the VCO model gateway (pid {proc.pid})? Chats routed through it will drop"):
        return [f"WARN: VCO model gateway (pid {proc.pid}) left running (declined) — its open "
                "log/state files may not be removable"]
    if (stopper or _default_gateway_stopper)(proc.pid, cands):
        return [f"stopped VCO model gateway (pid {proc.pid})"]
    return [f"WARN: could not stop the VCO model gateway (pid {proc.pid}) — stop it by hand"]


def plan_lines(
    locations: Sequence[Path],
    *,
    hub_running: Optional[Callable[[], bool]] = None,
    lister: Optional[Callable[[], list[ProcInfo]]] = None,
    install_root: Optional[Path] = None,
    gateway_candidates: Optional[Sequence[Sequence[str]]] = None,
    gateway_reader: Optional[Callable[[], Optional[GatewayProc]]] = None,
) -> list[str]:
    """The ``[3]`` lines of the uninstall plan. Read-only (``--dry-run``)."""
    hub = "running" if (hub_running or _default_hub_running)() else "not running"
    out = [f"  [3] Stop vct-hub ({hub}), the model gateway and the VCO launcher GUI "
           "before any state is removed"]
    try:
        gw, why, _ = find_gateway(install_root=install_root, candidates=gateway_candidates,
                                  reader=gateway_reader)
        if gw is not None:
            out.append(f"      model gateway running: pid {gw.pid}" + (
                " (asked; --yes stops it)" if why is None else f" — NOT STOPPED: {why}"))
    except Exception as exc:  # noqa: BLE001
        out.append(f"      (could not check the model gateway: {exc})")
    try:
        vco, other = find_launchers(locations, lister=lister)
    except Exception as exc:  # noqa: BLE001
        return out + [f"      (could not list running processes: {exc})"]
    if vco:
        out.append("      launcher running: " + "; ".join(_describe(p) for p in vco)
                   + " (you are asked to close it; --yes stops it)")
    out += [f"      NOT STOPPED: {_describe(p)} (named like the launcher, not at a VCO path)"
            for p in other]
    return out
