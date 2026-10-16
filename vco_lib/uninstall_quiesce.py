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

Never raises; every outcome is an audit line.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

__all__ = [
    "ProcInfo", "stop_hub", "find_launchers", "stop_launchers", "plan_lines",
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


def _default_stopper(pid: int, exe: Path) -> bool:
    """Terminate ``pid`` only if it STILL runs from ``exe`` (pid reuse)."""
    import psutil

    try:
        proc = psutil.Process(pid)
        current = proc.exe()
        if not current or _norm(Path(current)) != _norm(exe):
            return not proc.is_running()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return True
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError):
        return False


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


def plan_lines(
    locations: Sequence[Path],
    *,
    hub_running: Optional[Callable[[], bool]] = None,
    lister: Optional[Callable[[], list[ProcInfo]]] = None,
) -> list[str]:
    """The ``[3]`` lines of the uninstall plan. Read-only (``--dry-run``)."""
    hub = "running" if (hub_running or _default_hub_running)() else "not running"
    out = [f"  [3] Stop vct-hub ({hub}) and the VCO launcher GUI before any state is removed"]
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
