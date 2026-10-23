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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

__all__ = [
    "ProcInfo", "GatewayProc", "stop_hub", "find_launchers", "stop_launchers",
    "is_vco_gateway", "find_gateway", "stop_gateway", "quiesce", "still_running", "plan_lines",
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

def _cim_json(query: str, *, run: Optional[Callable[..., object]] = None) -> Any:
    """Run one ``Get-CimInstance Win32_Process`` pipeline and parse its JSON.

    PowerShell 5.1 writes a REDIRECTED stdout in ``[Console]::OutputEncoding``
    (the OEM code page — cp850 on it-IT/de-DE), which ``text=True`` would
    decode with the ANSI code page and raise on bytes such as cp850 ``ü``
    (review V02103-FINAL4 M1). The command forces UTF-8 output and the bytes
    are decoded as UTF-8 with ``errors="replace"``: an odd byte becomes U+FFFD
    in that one row, never an exception that hides the whole table. Raises
    when PowerShell cannot run or exits non-zero."""
    import json
    import subprocess

    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + query]
    res = (run or subprocess.run)(cmd, capture_output=True, timeout=60,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rc = getattr(res, "returncode", 0)
    if rc:
        raise OSError(f"PowerShell process query exited {rc}")
    raw = getattr(res, "stdout", b"") or b""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = text.lstrip("\ufeff").strip()
    return json.loads(text) if text else None


def _stdlib_lister(*, platform: Optional[str] = None,
                   run: Optional[Callable[..., object]] = None) -> list[ProcInfo]:
    """The process table without psutil — the uninstaller may run on the base
    Python of a clone whose venv is gone or was never built (Windows field run,
    v0.2.103). Windows: CIM via PowerShell; Linux: /proc; macOS: ps. Raises
    when the table cannot be read (callers treat that as "may be running")."""
    import subprocess
    import sys

    out: list[ProcInfo] = []
    if (platform or sys.platform) == "win32":
        rows = _cim_json("Get-CimInstance Win32_Process | Select-Object ProcessId,Name,"
                         "ExecutablePath | ConvertTo-Json -Compress", run=run) or []
        for row in rows if isinstance(rows, list) else [rows]:
            exe = row.get("ExecutablePath")
            out.append(ProcInfo(int(row["ProcessId"]), str(row.get("Name") or ""),
                                Path(exe) if exe else None))
        return out
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for d in proc_root.iterdir():
            if not d.name.isdigit():
                continue
            try:
                exe: Optional[Path] = Path(os.readlink(d / "exe"))
            except OSError:
                exe = None
            try:
                name = (d / "comm").read_text().strip()
            except OSError:
                continue
            out.append(ProcInfo(int(d.name), name, exe))
        return out
    res = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True,
                         text=True, timeout=60, check=True)
    for line in res.stdout.splitlines():
        pid, _, comm = line.strip().partition(" ")
        if pid.isdigit():
            comm = comm.strip()
            out.append(ProcInfo(int(pid), Path(comm).name, Path(comm) if comm else None))
    return out


def _default_lister() -> list[ProcInfo]:
    try:
        import psutil
    except ImportError:
        return _stdlib_lister()
    out: list[ProcInfo] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        info = proc.info
        exe = info.get("exe")
        out.append(ProcInfo(int(info["pid"]), str(info.get("name") or ""),
                            Path(exe) if exe else None))
    return out


def _psutil():
    """psutil when importable, else ``None`` — the base-Python shape (no venv)
    takes the standard-library paths below (review V02103-FINAL4 H1)."""
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _gone(pid: int) -> bool:
    """No live process has ``pid`` (an exited, not-yet-reaped zombie counts
    as gone). Raises when it cannot tell on Windows (process table unreadable)."""
    import sys

    if sys.platform == "win32":
        return all(p.pid != pid for p in _stdlib_lister())
    if Path("/proc").is_dir():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return stat.rpartition(")")[2].split()[:1] in (["Z"], ["X"])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    import subprocess

    res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                         text=True, timeout=30)
    return res.returncode != 0 or res.stdout.strip().startswith("Z")


def _wait_gone(pid: int, seconds: float, sleep: Callable[[float], None] = time.sleep) -> bool:
    waited = 0.0
    while True:
        if _gone(pid):
            return True
        if waited >= seconds:
            return False
        sleep(0.25)
        waited += 0.25


def _signal_stdlib(pid: int, *, force: bool) -> bool:
    """POSIX: SIGTERM (graceful) / SIGKILL. Windows: ``taskkill /PID`` (asks
    the process to close) / ``taskkill /PID /F``. False when the request
    itself failed."""
    import signal
    import subprocess
    import sys

    if sys.platform == "win32":
        argv = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
        res = subprocess.run(argv, capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return res.returncode == 0
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return True
    return True


def _terminate_if_stdlib(pid: int, still_ours: Callable[[], Optional[bool]], *,
                         grace: float, sleep: Callable[[float], None] = time.sleep) -> bool:
    """The psutil-free stop. ``still_ours()`` re-identifies the pid from the
    standard-library process readers (``None`` = it is gone); it is checked
    before the graceful request AND again before the forced one, so a pid
    reused during the grace wait is never killed. True when it is gone."""
    try:
        if _gone(pid):
            return True
        verdict = still_ours()
        if verdict is None:
            return True
        if not verdict:
            return False
        _signal_stdlib(pid, force=False)  # bounded wait even if the request failed
        if _wait_gone(pid, grace, sleep):
            return True
        verdict = None if _gone(pid) else still_ours()
        if verdict is None:
            return True
        if not verdict:
            return False
        _signal_stdlib(pid, force=True)
        return _wait_gone(pid, 5.0, sleep)
    except Exception:  # noqa: BLE001 — "could not stop" is an audit line, never a crash
        return False


def _terminate_if(pid: int, still_ours: Callable[..., bool],
                  still_ours_stdlib: Callable[[], Optional[bool]], *, grace: float = 10.0) -> bool:
    """Terminate ``pid`` only if it is still the identified process at that
    moment (guards a pid reused since it was identified). True when it is
    gone. ``still_ours(proc)`` is the psutil check; without psutil the stop
    goes through :func:`_terminate_if_stdlib` with ``still_ours_stdlib``."""
    psutil = _psutil()
    if psutil is None:
        return _terminate_if_stdlib(pid, still_ours_stdlib, grace=grace)
    try:
        proc = psutil.Process(pid)
        if not still_ours(proc):
            return not proc.is_running()
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except psutil.TimeoutExpired:
            # Re-identify before forcing, as the stdlib leg does (psutil's own
            # pid-reuse check covers a reused pid; this also covers a process
            # that exec'd into something else during the grace wait).
            if not proc.is_running() or not still_ours(proc):
                return not proc.is_running()
            proc.kill()
            proc.wait(timeout=5)
        return True
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError):
        return False


def _launcher_still_ours_stdlib(pid: int, exe: Path) -> Optional[bool]:
    """``None`` gone / True same exe / False reused — through the same stdlib
    process table :func:`find_launchers` identified it from."""
    for p in _stdlib_lister():
        if p.pid == pid:
            return p.exe is not None and _norm(p.exe) == _norm(exe)
    return None


def _default_stopper(pid: int, exe: Path) -> bool:
    """Terminate ``pid`` only if it STILL runs from ``exe`` (pid reuse)."""
    def ours(proc) -> bool:
        current = proc.exe()
        return bool(current) and _norm(Path(current)) == _norm(exe)

    return _terminate_if(pid, ours, lambda: _launcher_still_ours_stdlib(pid, exe))


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
        try:
            stopped = stop(p.pid, p.exe)
        except Exception as exc:  # noqa: BLE001 — the uninstall continues; [8] re-checks
            audit.append(f"WARN: could not stop VCO launcher ({_describe(p)}): {exc} — close it by hand")
            continue
        if stopped:
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
    #: The UNSPLIT command line, when the source could only give one (macOS
    #: ``ps -o args=``). ``cmdline`` is then a naive whitespace split that
    #: breaks a path containing spaces; :func:`is_vco_gateway` falls back to
    #: candidate-anchored matching on this line for exactly that case.
    raw_line: Optional[str] = None


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


_PYTHON_NAME = re.compile(r"pythonw?(\d+(\.\d+)*)?(\.exe)?", re.IGNORECASE)


def _is_python(token: str) -> bool:
    """``token`` names a Python interpreter (``python3.12``, ``pythonw.exe`` …)."""
    return bool(_PYTHON_NAME.fullmatch(re.split(r"[\\/]", token)[-1]))


def _python_script_arg(toks: Sequence[str]) -> Optional[str]:
    """The script a Python command line runs — the first argument after the
    interpreter's own options (``python -S <script>``) — or ``None`` when
    argv[0] is not a Python interpreter or it runs ``-m`` / ``-c`` / stdin."""
    if not toks or not _is_python(toks[0]):
        return None
    i = 1
    while i < len(toks) and toks[i].startswith("-") and toks[i] not in ("-m", "-c", "-"):
        i += 2 if toks[i] in ("-W", "-X") else 1
    if i >= len(toks) or toks[i] in ("-m", "-c", "-"):
        return None
    return toks[i]


def _python_invocation_prefix(prefix: str, known_interps: Sequence[str] = ()) -> bool:
    """True when *prefix* — everything on a raw command line BEFORE the
    gateway script — is a Python interpreter followed only by its own
    options. Mirrors :func:`_python_script_arg`'s rule without tokenising,
    so an interpreter path containing spaces still qualifies (``_is_python``
    judges the basename).

    *known_interps* are interpreter paths the caller KNOWS (the venv python
    of the install being uninstalled). A prefix that STARTS with one of them
    is split exactly there, so a known interpreter path containing `` -``
    (``/Users/u/Proj -beta/.venv/bin/python``) is still recognised; an
    unknown interpreter keeps the first-``\\s+-\\S`` cut, so nothing else is
    loosened."""
    prefix = prefix.strip()
    if not prefix:
        return False
    interp = optstr = ""
    for known in known_interps:
        if prefix == known or prefix.startswith(known + " "):
            interp, optstr = known, prefix[len(known):]
            break
    else:
        m = re.search(r"\s+-\S", prefix)
        interp, optstr = (prefix, "") if m is None else (prefix[: m.start()],
                                                         prefix[m.start():])
    if not _is_python(interp):
        return False
    toks = optstr.split()
    i = 0
    while i < len(toks):
        if toks[i] in ("-m", "-c", "-") or not toks[i].startswith("-"):
            return False
        if toks[i] in ("-W", "-X"):
            if i + 1 >= len(toks):
                return False  # a value option with no value is malformed
            i += 2
        else:
            i += 1
    return True


def _raw_line_match(raw: str, cand: Sequence[str]) -> bool:
    """Candidate-anchored match on an UNSPLIT command line — the macOS
    ``ps -o args=`` arm, where a naive whitespace split breaks a gateway
    path containing spaces (review FINAL4 "not done", v0.2.104).

    The rule is exactly the token path's: for a console-script candidate the
    script is argv[0] or the first argument after a Python interpreter and
    its options; for a module candidate the line starts with that
    interpreter followed by ``-m <module>``. The script anywhere else on the
    line (``cp <script> /tmp``, an editor) still does NOT match."""
    cand = list(cand)
    if len(cand) == 1:
        script = cand[0]
        if raw == script or raw.startswith(script + " "):
            return True  # argv[0] (== the executable on a shebang run)
        # The install's own venv python sits beside its console script.
        interps = [str(Path(script).parent / n) for n in ("python", "python3")]
        for m in re.finditer(re.escape(script) + r"(?=\s|$)", raw):
            if (m.start() > 0 and raw[m.start() - 1].isspace()
                    and _python_invocation_prefix(raw[: m.start()], interps)):
                return True
        return False
    if len(cand) == 3 and cand[1] == "-m":
        tail = f"{cand[1]} {cand[2]}"
        for m in re.finditer(re.escape(tail) + r"(?=\s|$)", raw):
            if (m.start() > 0 and raw[m.start() - 1].isspace()
                    and _same_path(raw[: m.start()].rstrip(), cand[0])):
                return True
    return False


def is_vco_gateway(proc: GatewayProc, candidates: Sequence[Sequence[str]]) -> bool:
    """Does ``proc`` run one of VCO's gateway argvs?

    * console-script candidate ``[script]``: the script is the executable,
      argv[0] (Windows runs ``<script>.exe serve``), or the first argument
      after a Python interpreter and its options (a POSIX shebang runs
      ``<venv python> <script> serve``; the Windows launcher's child runs
      ``<python.exe> <script>.exe serve``). The script anywhere else on the
      line (``cp <script> /tmp``, an editor) is NOT the gateway (review R39 L2);
    * module candidate ``[python, "-m", "model_router"]``: the command line
      starts with that interpreter followed by ``-m model_router``.

    When ``proc.raw_line`` is set (macOS ``ps -o args=`` — its argv is only
    available as one joined string) the same rule is applied
    candidate-anchored on the raw line (:func:`_raw_line_match`), so a
    gateway whose PATH contains spaces is still recognised; the token path
    above stays the primary check everywhere else.
    """
    if not proc.cmdline:
        return False
    toks = list(proc.cmdline)
    for cand in candidates:
        cand = list(cand)
        if len(cand) == 1:
            if (proc.exe is not None and _same_path(str(proc.exe), cand[0])) or _same_path(
                toks[0], cand[0]
            ) or ((script := _python_script_arg(toks)) is not None and _same_path(script, cand[0])):
                return True
        elif len(cand) == 3 and cand[1] == "-m":
            if len(toks) >= 3 and _same_path(toks[0], cand[0]) and toks[1:3] == cand[1:]:
                return True
    if proc.raw_line:
        for cand in candidates:
            if _raw_line_match(proc.raw_line, cand):
                return True
    return False


def _split_windows_cmdline(line: str) -> tuple[str, ...]:
    """Tokens of a Windows command line: whitespace splits outside double
    quotes, quotes are dropped. Enough for the paths VCO starts the gateway
    with; a line it splits differently simply does not match (not stopped)."""
    toks: list[str] = []
    cur: list[str] = []
    quoted = started = False
    for ch in line:
        if ch == '"':
            quoted, started = not quoted, True
        elif ch.isspace() and not quoted:
            if started:
                toks.append("".join(cur))
            cur, started = [], False
        else:
            cur.append(ch)
            started = True
    if started:
        toks.append("".join(cur))
    return tuple(toks)


def _stdlib_identity(pid: int, *, platform: Optional[str] = None,
                     run: Optional[Callable[..., object]] = None) -> Optional[GatewayProc]:
    """``pid``'s command line and executable without psutil: ``None`` when no
    such process; ``cmdline=None`` when it exists but will not say. Linux
    ``/proc/<pid>/cmdline``; macOS ``ps -o args=``; Windows the same CIM
    query :func:`_stdlib_lister` uses, filtered to the pid. Raises when it
    cannot look at all."""
    import subprocess
    import sys

    plat = platform or sys.platform
    if plat == "win32":
        row = _cim_json(f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
                        "Select-Object CommandLine,ExecutablePath | ConvertTo-Json -Compress", run=run)
        if isinstance(row, list):
            row = row[0] if row else None
        if not isinstance(row, dict):
            return None
        line, exe = row.get("CommandLine"), row.get("ExecutablePath")
        toks = _split_windows_cmdline(line) if line else ()
        return GatewayProc(pid, toks or None, Path(exe) if exe else None)
    if plat != "darwin" and Path("/proc").is_dir():
        if _gone(pid):
            return None
        d = Path(f"/proc/{pid}")
        try:
            raw = (d / "cmdline").read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return GatewayProc(pid, None)
        toks = tuple(os.fsdecode(t) for t in raw.split(b"\0") if t)
        try:
            exe: Optional[Path] = Path(os.readlink(d / "exe"))
        except OSError:
            exe = None
        return GatewayProc(pid, toks or None, exe)
    runner = run or subprocess.run
    res: Any = runner(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True,
                      text=True, errors="replace", timeout=30)
    line = res.stdout.strip()
    if res.returncode != 0 or not line:
        return None  # ps exits 1 for an unknown pid
    comm_res: Any = runner(["ps", "-ww", "-o", "comm=", "-p", str(pid)], capture_output=True,
                           text=True, errors="replace", timeout=30)
    comm = comm_res.stdout.strip()
    # `ps` gives ONE joined line, not argv: the whitespace split below breaks
    # a gateway path containing spaces, so the raw line travels alongside for
    # `is_vco_gateway`'s candidate-anchored fallback (which keeps the same
    # argv0 / first-arg-after-python rule — see `_raw_line_match`).
    return GatewayProc(pid, tuple(line.split()), Path(comm) if comm else None,
                       raw_line=line)


def _default_gateway_reader() -> Optional[GatewayProc]:
    """The live process the daemon's own pid file names, or ``None``. A pid
    file whose pid now belongs to another process yields that process's
    command line (so it reads "not ours"), with or without psutil."""
    from vco_lib import gateway_ensure

    pid = gateway_ensure.gateway_pid()
    if pid is None or not gateway_ensure.is_running():
        return None
    psutil = _psutil()
    if psutil is None:
        return _stdlib_identity(pid)
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

    def ours_stdlib() -> Optional[bool]:
        proc = _stdlib_identity(pid)
        return None if proc is None else is_vco_gateway(proc, candidates)

    # its graceful shutdown drains for up to 60 s; an uninstall waits 15
    return _terminate_if(pid, ours, ours_stdlib, grace=15.0)


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
    try:
        stopped = (stopper or _default_gateway_stopper)(proc.pid, cands)
    except Exception as exc:  # noqa: BLE001 — the uninstall continues; [8] re-checks
        return [f"WARN: could not stop the VCO model gateway (pid {proc.pid}): {exc} — stop it by hand"]
    if stopped:
        return [f"stopped VCO model gateway (pid {proc.pid})"]
    return [f"WARN: could not stop the VCO model gateway (pid {proc.pid}) — stop it by hand"]


def quiesce(
    launcher_exes: Sequence[Path],
    hub_binaries: Iterable[Optional[Path]],
    *,
    interactive: bool,
    confirm: Callable[[str], bool],
    install_root: Optional[Path] = None,
) -> list[str]:
    """Uninstall step [3]: the gateway, then vct-hub, then the launcher GUI.
    Each stop is guarded: an unexpected failure in one becomes a WARN line and
    the uninstall goes on (step [8]'s :func:`still_running` re-check still
    refuses to remove the state dir while anything of VCO runs)."""
    steps: list[tuple[str, Callable[[], list[str]]]] = [
        ("the model gateway", lambda: stop_gateway(
            interactive=interactive, confirm=confirm, install_root=install_root)),
        ("vct-hub", lambda: stop_hub(hub_binaries)),
        ("the VCO launcher", lambda: stop_launchers(
            launcher_exes, interactive=interactive, confirm=confirm, wait_for_user=confirm)),
    ]
    audit: list[str] = []
    for what, step in steps:
        try:
            audit.extend(step())
        except Exception as exc:  # noqa: BLE001
            audit.append(f"WARN: could not stop {what} ({type(exc).__name__}: {exc}) — "
                         "stop it yourself; the state dir is removed only once nothing of VCO runs")
    return audit


def still_running(
    launcher_exes: Sequence[Path],
    *,
    install_root: Optional[Path] = None,
    hub_running: Optional[Callable[[], bool]] = None,
    lister: Optional[Callable[[], list[ProcInfo]]] = None,
    gateway_reader: Optional[Callable[[], Optional[GatewayProc]]] = None,
    gateway_candidates: Optional[Sequence[Sequence[str]]] = None,
) -> list[str]:
    """What of VCO still runs (same identification as the stops). Step [8]
    removes nothing while this is non-empty (review R39 M2); a check that
    cannot run counts as running."""
    out: list[str] = []
    try:  # unlike step [3]'s lenient probe, an unreadable hub state here blocks [8]
        if hub_running is not None:
            hub_up = hub_running()
        else:
            from vco_lib import hub_ensure

            hub_up = hub_ensure.is_running()
        if hub_up:
            out.append("vct-hub")
    except Exception as exc:  # noqa: BLE001
        out.append(f"possibly vct-hub (cannot check: {exc})")
    try:
        out += [f"the VCT Launcher ({_describe(p)})" for p in find_launchers(launcher_exes, lister=lister)[0]]
    except Exception as exc:  # noqa: BLE001
        out.append(f"possibly the VCT Launcher (process list unreadable: {exc})")
    try:
        gw, why, _ = find_gateway(install_root=install_root, candidates=gateway_candidates, reader=gateway_reader)
        if gw is not None and why is None:
            out.append(f"the model gateway (pid {gw.pid})")
    except Exception as exc:  # noqa: BLE001
        out.append(f"possibly the model gateway (cannot check: {exc})")
    return out


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
