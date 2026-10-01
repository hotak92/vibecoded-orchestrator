# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py honours the launcher's machine-wide update claim (v0.2.100, W2R-06).

While the launcher updates this orchestrator it holds ``<vct_root>/update.lock``
(``launcher/src-tauri/src/commands/single_flight.rs::acquire_update_lock``).
Before this module only the launcher read that file, so ``python install.py
--update`` from a shell could run straight into the middle of a GUI update —
two processes interleaving ``git pull`` and ``install.py`` on one tree.

This is the ONE Python reader of that file. Its format MUST match the Rust
writer exactly (pinned by ``tests/test_v02100_update_lock_honoured.py`` against
the literal ``format!("{}\\n{}\\n", own_pid, timestamp)`` in single_flight.rs):

    <holder pid>\\n<unix seconds when claimed>\\n

Decision (:func:`decide`), in order:

* no file / empty / unparseable → PROCEED (install.py cannot tell, and a guard
  that cannot confirm a concurrent update must not block one — the launcher,
  the lock's owner, refuses malformed claims on its side);
* holder dead, or alive but started AFTER the claim was written (a reused pid —
  the success path leaves a claim naming a dead pid, W2R-06) → PROCEED (stale;
  the file is the launcher's to reap, never deleted here);
* holder is THIS process or one of its ancestors → PROCEED: this install.py is
  the launcher's own child (``update_run.rs`` step 8 spawns it). Recognition by
  ancestry needs no cooperation from the launcher — no env token to forward
  through the venv relaunch, nothing a stale environment could replay;
* holder alive, not an ancestor → REFUSE with a clear message (exit 1);
* liveness or ancestry cannot be determined → PROCEED with a warning (the
  pre-0.2.100 behaviour; never wedge an update on an unknowable). The warning
  names WHY (:func:`unknowable_cause`) and how to enforce the lock.

Process facts come from ``psutil`` (a hard dependency of the orchestrator
venv). Without it — ``python install.py --update`` run by a system
interpreter, before the venv relaunch — POSIX reads ``os.kill(pid, 0)`` /
``/proc`` / ``ps``, and Windows (W4R-08) asks the Win32 API through ``ctypes``
(``OpenProcess`` + ``GetExitCodeProcess`` for liveness, ``GetProcessTimes``
for the start time, a ``CreateToolhelp32Snapshot`` walk for the parent), so
the guard is enforced there too instead of degrading to advisory.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

LOCK_BASENAME = "update.lock"  # must match single_flight.rs UPDATE_LOCK_BASENAME

PROCEED = "proceed"
REFUSE = "refuse"

#: A process whose start time is within this many seconds AFTER the claim is
#: still treated as the claimant (clock granularity between the two readers).
_START_SLACK_S = 2.0


@dataclass(frozen=True)
class Claim:
    pid: int
    claimed_at: Optional[float]


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str


def lock_path(vct_root: Optional[Path] = None) -> Path:
    if vct_root is None:
        from vco_lib.paths import vct_root_dir

        vct_root = vct_root_dir()
    return Path(vct_root) / LOCK_BASENAME


def parse_claim(text: str) -> Optional[Claim]:
    """Parse the Rust writer's two-line body; ``None`` when it names no pid."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    if not lines or not lines[0].isdigit():
        return None
    ts: Optional[float] = None
    if len(lines) > 1:
        try:
            ts = float(lines[1])
        except ValueError:
            ts = None
    return Claim(pid=int(lines[0]), claimed_at=ts)


# ── process facts (psutil when importable — a hard dependency of the venv —
#    with POSIX and Windows-ctypes fallbacks; every probe is tri-state:
#    None = could not tell) ──

_IS_WINDOWS = os.name == "nt"

# Win32 constants (winnt.h / winerror.h / tlhelp32.h).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_TH32CS_SNAPPROCESS = 0x00000002
#: FILETIME epoch (1601-01-01) → Unix epoch, in seconds.
_FILETIME_UNIX_OFFSET_S = 11644473600


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    return k32


def _win_pid_alive(pid: int) -> Optional[bool]:
    """Liveness via ``OpenProcess`` + ``GetExitCodeProcess`` (Windows, no psutil)."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = _kernel32()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err == _ERROR_INVALID_PARAMETER:
                return False  # no process with that id
            if err == _ERROR_ACCESS_DENIED:
                return True  # it exists; we may not query it
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            # A process that EXITED with code 259 reads as alive — the one
            # documented ambiguity of this API; it errs toward refusing.
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — unknowable, never a crash
        return None


def _win_start_time(pid: int) -> Optional[float]:
    """Process creation time (Unix seconds) via ``GetProcessTimes``."""
    try:
        import ctypes

        k32 = _kernel32()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
            if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                       ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return created.value / 10_000_000 - _FILETIME_UNIX_OFFSET_S
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _win_parent_of(pid: int) -> Optional[int]:
    """Parent pid from a ``CreateToolhelp32Snapshot`` process walk.

    Windows keeps a dead parent's pid in the child's record, and pids are
    reused: a "parent" that started AFTER the child is not its parent, and
    the chain ends there (``0``) rather than climbing into a stranger."""
    try:
        import ctypes
        from ctypes import wintypes

        class _ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = _kernel32()
        entry_ptr = ctypes.POINTER(_ProcessEntry32W)
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, entry_ptr]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, entry_ptr]
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return None
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            parent: Optional[int] = None
            while ok:
                if entry.th32ProcessID == pid:
                    parent = int(entry.th32ParentProcessID)
                    break
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        if parent is None:
            return None
        child_start, parent_start = _win_start_time(pid), _win_start_time(parent)
        if child_start is not None and parent_start is not None and parent_start > child_start:
            return 0  # the recorded parent exited and its pid was reused
        return parent
    except Exception:  # noqa: BLE001
        return None



def _psutil():
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil
    except Exception:  # noqa: BLE001 — absent/broken → the fallbacks
        return None


def pid_alive(pid: int) -> Optional[bool]:
    ps = _psutil()
    if ps is not None:
        try:
            return bool(ps.pid_exists(pid))
        except Exception:  # noqa: BLE001
            return None
    if _IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def process_start_time(pid: int) -> Optional[float]:
    ps = _psutil()
    if ps is None:
        return _win_start_time(pid) if _IS_WINDOWS else None
    try:
        return float(ps.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return None


def _parent_of(pid: int) -> Optional[int]:
    ps = _psutil()
    if ps is not None:
        try:
            return int(ps.Process(pid).ppid())
        except Exception:  # noqa: BLE001
            return None
    if _IS_WINDOWS:
        return _win_parent_of(pid)
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():
        try:
            # comm may hold spaces/parens: the fields after the LAST ')' are fixed.
            return int(stat.read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def ancestors(pid: Optional[int] = None, *, parent_of: Callable[[int], Optional[int]] = _parent_of
              ) -> Optional[list]:
    """``[pid, parent, grandparent, …]`` up to the root; ``None`` when the
    chain could not be read at all (bounded at 64 hops)."""
    cur = os.getpid() if pid is None else pid
    chain = [cur]
    for _ in range(64):
        parent = parent_of(cur)
        if parent is None:
            return chain if len(chain) > 1 else None
        if parent <= 0 or parent in chain:
            return chain
        chain.append(parent)
        cur = parent
    return chain


def decide(claim: Optional[Claim], *, alive: Callable[[int], Optional[bool]] = pid_alive,
           started: Callable[[int], Optional[float]] = process_start_time,
           lineage: Callable[[], Optional[list]] = ancestors) -> Decision:
    """Pure over its probes — see the module docstring for the rule."""
    if claim is None:
        return Decision(PROCEED, "no update claim")
    is_alive = alive(claim.pid)
    if is_alive is False:
        return Decision(PROCEED, f"stale claim (pid {claim.pid} is not running)")
    if is_alive is True and claim.claimed_at is not None:
        began = started(claim.pid)
        if began is not None and began > claim.claimed_at + _START_SLACK_S:
            return Decision(PROCEED, f"stale claim (pid {claim.pid} was reused by a newer process)")
    chain = lineage()
    if chain is not None and claim.pid in chain:
        return Decision(PROCEED, f"this install.py is the child of the update holder (pid {claim.pid})")
    if is_alive is None or chain is None:
        return Decision(PROCEED, f"could not tell whether pid {claim.pid} holds a live update — "
                                 "proceeding (warning)")
    return Decision(REFUSE, f"pid {claim.pid} is updating this orchestrator")


def unknowable_cause() -> str:
    """Why a probe could not tell, and how to make the lock enforced — the
    text of the warning :func:`refuse_if_foreign_update_running` prints."""
    if _psutil() is None:
        fallback = ("the Windows process query (OpenProcess / Toolhelp) failed"
                    if _IS_WINDOWS else "/proc and `ps` could not answer")
        return (f"process facts are unavailable under this interpreter ({sys.executable}: "
                f"psutil is not importable and {fallback}); run install.py with the "
                "orchestrator venv's python to enforce the update lock")
    return ("psutil could not query that process (permissions?); the update lock was "
            "not enforced for this run")


def check(vct_root: Optional[Path] = None, **probes) -> Decision:
    path = lock_path(vct_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Decision(PROCEED, "no update claim")
    return decide(parse_claim(text), **probes)


def refuse_if_foreign_update_running(vct_root: Optional[Path] = None, **probes) -> None:
    """install.py ``--update`` entry: exit 1 with a clear message while ANOTHER
    process (not this run's own parent launcher) holds the update claim."""
    decision = check(vct_root, **probes)
    if decision.verdict == REFUSE:
        path = lock_path(vct_root)
        print(
            f"ERROR: another update of this orchestrator is running ({decision.reason}; claim "
            f"at {path}). Refusing to start a second one — wait for it to finish (the "
            "launcher's update window shows its progress). If no update is running, that "
            "process is not an update: delete the claim file and re-run.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if "warning" in decision.reason:
        print(f"  [!] update.lock: {decision.reason} — {unknowable_cause()}", file=sys.stderr)
