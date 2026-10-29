# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Kill guard, layer 2: the RUNTIME check (v0.2.105).

Layer 1 (:mod:`vco_lib.kill_guard`, called by the pre-tool-use scanner)
refuses the kill commands whose danger is visible in the text. Layer 2 runs
when the command actually executes: the PreToolUse rewrite
(:func:`vco_lib.kill_guard.build_rewrite`) routes ``kill``/``pkill``/
``killall`` (Bash) and ``Stop-Process``/``kill``/``spps``/``taskkill``
(PowerShell) through a guard function that calls ``python -m
vco_lib.kill_guard check <verb> -- <args>`` first. This module is what that
check decides with: it resolves every pid the call would signal and refuses
the call when ANY of them is

* pid 1 / init, pid 0 or -1 (every process), or the Windows idle/System
  pids 0 and 4;
* the guard's own process or one of its ANCESTORS — the shell that ran the
  command, the agent (claude), the editor (code), the terminal, and
  ``systemd --user`` / launchd above them;
* the session leader of the calling shell;
* a process whose name is in :data:`PROTECTED_NAMES` (the desktop session:
  systemd*, gnome-shell, plasmashell, Xorg/Xwayland, kwin*, mutter,
  pipewire*, dbus-*, sshd, …; on Windows explorer, winlogon, csrss,
  services, lsass, dwm, …);
* for a negative pid (a process GROUP), any member of that group.

The 2026-10-23 incident this exists for: ``kill -9 $p $(ps -o ppid= -p $p)``
on an orphan — whose parent is ``systemd --user`` — SIGKILLed the user
manager and with it the whole desktop session.

Everything here is pure over an injected :data:`ProcTable`; the only I/O
is :func:`current_table` (``/proc``, ``ps``, or the Windows toolhelp
snapshot) and the ``pgrep`` call behind ``pkill``. Tests drive the pure
functions with fabricated tables and never signal a real process.

Fail-closed: a table that cannot be read, a ``pgrep`` that cannot run, or
any internal error REFUSES the call (exit 1 from the CLI). The rewrite is
only applied when the hook itself could import this package, so a refusal
for "cannot check" names its cause and the owner's switch.
"""
from __future__ import annotations

import fnmatch
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class Proc:
    """One process-table row. ``pgrp``/``sid`` are None where the platform
    does not report them (Windows, ``ps`` without a session column)."""

    pid: int
    ppid: int
    name: str
    pgrp: Optional[int] = None
    sid: Optional[int] = None
    cmdline: str = ""


#: pid -> row. Built by :func:`current_table` or injected by tests.
ProcTable = Dict[int, Proc]

#: Exact protected process names (lower-case; ``.exe`` stripped on Windows).
PROTECTED_NAMES: frozenset = frozenset({
    # Linux desktop session and its plumbing
    "init", "gnome-shell", "gnome-session", "gnome-session-binary",
    "plasmashell", "ksmserver", "kwin", "kwin_x11", "kwin_wayland", "mutter",
    "xorg", "x", "xwayland", "pipewire", "pipewire-pulse", "wireplumber",
    "pulseaudio", "dbus-daemon", "dbus-broker", "dbus-broker-launch",
    "sshd", "gdm", "gdm3", "sddm", "lightdm", "login", "xdg-desktop-portal",
    # macOS session
    "launchd", "windowserver", "loginwindow", "finder", "dock",
    # Windows session (spec 2026-10-23 + the boot-critical rest)
    "explorer", "winlogon", "csrss", "services", "lsass", "dwm", "wininit",
    "smss", "system", "registry", "svchost", "sihost",
})

#: Protected name PREFIXES (``systemd`` covers ``systemd``, ``systemd-logind``,
#: ``systemd-journald`` …; ``(sd-pam)`` is the user manager's PAM helper).
PROTECTED_PREFIXES: Tuple[str, ...] = ("systemd", "(sd-pam", "gnome-session",
                                       "kwin_", "pipewire", "dbus-")

#: Pids that are never a legitimate target (Windows: 0 idle, 4 System).
_WINDOWS_CRITICAL_PIDS = frozenset({0, 4})


def normalize_name(name: str) -> str:
    """Lower-case basename without ``.exe`` — the form the lists hold."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def is_protected_name(name: str) -> bool:
    n = normalize_name(name)
    return bool(n) and (n in PROTECTED_NAMES or n.startswith(PROTECTED_PREFIXES))


# ═══════════════════════════════════════════════════════════════════════
# Process tables
# ═══════════════════════════════════════════════════════════════════════


def _parse_proc_stat(text: str) -> Optional[Tuple[str, int, int, int]]:
    """``(comm, ppid, pgrp, session)`` from a ``/proc/<pid>/stat`` line.
    ``comm`` may itself contain spaces and parentheses, so it is cut at the
    LAST ``)``."""
    lp, rp = text.find("("), text.rfind(")")
    if lp < 0 or rp < lp:
        return None
    rest = text[rp + 1:].split()
    # rest: state ppid pgrp session ...
    if len(rest) < 4:
        return None
    try:
        return text[lp + 1:rp], int(rest[1]), int(rest[2]), int(rest[3])
    except ValueError:
        return None


def read_proc_table(proc_root: str = "/proc") -> Optional[ProcTable]:
    """The table from a ``/proc`` tree (Linux, Cygwin/MSYS). ``proc_root``
    lets tests hand in a fixture directory. None when unreadable."""
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    table: ProcTable = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        base = os.path.join(proc_root, entry)
        try:
            with open(os.path.join(base, "stat"), encoding="utf-8",
                      errors="replace") as f:
                parsed = _parse_proc_stat(f.read())
        except OSError:
            continue  # the process exited between listdir and open
        if parsed is None:
            continue
        comm, ppid, pgrp, sid = parsed
        cmdline = ""
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(
                    "utf-8", "replace").strip()
        except OSError:
            pass
        pid = int(entry)
        table[pid] = Proc(pid, ppid, comm, pgrp, sid, cmdline)
    return table or None


def _read_ps_table() -> Optional[ProcTable]:
    """BSD/macOS fallback: ``ps -axo pid=,ppid=,pgid=,comm=``."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,comm="],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    table: ProcTable = {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        table[pid] = Proc(pid, ppid, parts[3], pgid, None, parts[3])
    return table or None


def _read_windows_table() -> Optional[ProcTable]:
    """Windows: one CreateToolhelp32Snapshot walk (stdlib ctypes, no
    dependency). Gives pid, parent pid and image name."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    table: ProcTable = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            table[pid] = Proc(pid, int(entry.th32ParentProcessID),
                              entry.szExeFile, None, None, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table or None


def current_table() -> Optional[ProcTable]:
    """This machine's process table, or None when it cannot be read."""
    if sys.platform == "win32":
        return _read_windows_table()
    if os.path.isdir("/proc/self"):
        table = read_proc_table("/proc")
        if table:
            return table
    return _read_ps_table()


# ═══════════════════════════════════════════════════════════════════════
# The protected set
# ═══════════════════════════════════════════════════════════════════════


def ancestors(pid: int, table: ProcTable) -> List[int]:
    """``pid``'s ancestor chain, nearest first (pid itself excluded).
    Stops at a pid the table does not hold, at 0, or at a cycle."""
    out: List[int] = []
    seen: Set[int] = {pid}
    cur = table.get(pid)
    while cur is not None and cur.ppid not in seen and cur.ppid > 0:
        seen.add(cur.ppid)
        out.append(cur.ppid)
        cur = table.get(cur.ppid)
    return out


def _describe(pid: int, table: ProcTable) -> str:
    row = table.get(pid)
    if row is None:
        return "pid %d" % pid
    shown = row.cmdline or row.name
    if len(shown) > 60:
        shown = shown[:57] + "..."
    return "pid %d (%s)" % (pid, shown)


def protected_reason(pid: int, table: ProcTable, self_pid: int) -> Optional[str]:
    """Why ``pid`` must not be signalled from ``self_pid``'s shell, or None.

    ``self_pid`` is the guard's own process: its ancestors are the shell
    that ran the command and everything above it."""
    if pid <= 1:
        return "pid %d is init / every process" % pid
    if sys.platform == "win32" and pid in _WINDOWS_CRITICAL_PIDS:
        return "pid %d is a Windows system process" % pid
    if pid == self_pid:
        return "%s is the kill guard itself" % _describe(pid, table)
    chain = ancestors(self_pid, table)
    if pid in chain:
        return ("%s is an ancestor of the shell running this command "
                "(the shell, the agent, the editor, the terminal or the "
                "session manager)" % _describe(pid, table))
    me = table.get(self_pid)
    if me is not None and me.sid is not None and pid == me.sid:
        return "%s is this shell's session leader" % _describe(pid, table)
    row = table.get(pid)
    if row is not None and (is_protected_name(row.name)
                            or (row.cmdline and is_protected_name(
                                row.cmdline.split()[0]))):
        return "%s is a desktop/system-critical process" % _describe(pid, table)
    return None


def _group_reason(pgid: int, table: ProcTable, self_pid: int) -> Optional[str]:
    """A negative pid signals the whole process group ``pgid``."""
    leader = protected_reason(pgid, table, self_pid)
    if leader is not None:
        return "process group %d: %s" % (pgid, leader)
    for row in table.values():
        if row.pgrp == pgid:
            why = protected_reason(row.pid, table, self_pid)
            if why is not None:
                return "process group %d holds %s" % (pgid, why)
    return None


def check_pids(pids: Iterable[int], table: ProcTable, self_pid: int) -> Optional[str]:
    """The first refusal reason over ``pids`` (negative = process group)."""
    for pid in pids:
        if pid == -1 or pid == 0:
            return ("target %d signals %s" % (
                pid, "every process" if pid == -1 else "this shell's own process group"))
        why = (_group_reason(-pid, table, self_pid) if pid < 0
               else protected_reason(pid, table, self_pid))
        if why is not None:
            return why
    return None


# ═══════════════════════════════════════════════════════════════════════
# Per-verb resolution
# ═══════════════════════════════════════════════════════════════════════

_SIGNAL_NAMES = frozenset(
    s.name[3:] for s in signal.Signals if s.name.startswith("SIG")
) | {"RTMIN", "RTMAX", "IOT", "CLD", "POLL", "PWR", "STKFLT", "UNUSED"}


def is_signal_spec(tok: str) -> bool:
    """``-9`` / ``-KILL`` / ``-SIGKILL`` / ``-RTMIN+3`` style first argument."""
    if not tok.startswith("-") or len(tok) < 2:
        return False
    body = tok[1:]
    if body.isdigit():
        return True
    up = body.upper()
    if up.startswith("SIG"):
        up = up[3:]
    up = re.sub(r"[+-]\d+$", "", up)
    return up in _SIGNAL_NAMES


def kill_targets(args: Sequence[str]) -> Optional[List[str]]:
    """The target words of a ``kill`` call (bash builtin / procps
    semantics: the FIRST option is the signal, every later word is a
    target, so ``kill -9 -1`` targets -1). None for a listing call
    (``kill -l``)."""
    a = list(args)
    i = 0
    if a and a[0] in ("-l", "-L", "--list", "--table"):
        return None
    if i < len(a) and a[i] == "--":
        i += 1
    elif i < len(a) and a[i] in ("-s", "-n", "--signal"):
        i += 2
    elif i < len(a) and (a[i].startswith("--signal=") or
                         (a[i].startswith("-") and len(a[i]) > 1)):
        i += 1
    if i < len(a) and a[i] == "--":
        i += 1
    return a[i:]


def _int_or_none(word: str) -> Optional[int]:
    try:
        return int(word.strip(), 10)
    except ValueError:
        return None


def resolve_kill(args: Sequence[str]) -> Tuple[List[int], Optional[str]]:
    """Numeric targets of a ``kill`` call. Job specs (``%1``) are the
    shell's own children and are skipped; a non-numeric word is left for
    ``kill`` itself to reject. A signal-0 probe is not a kill: nothing is
    delivered, so it is never refused."""
    a = list(args)
    if a and (a[0] in ("-0", "-s0", "--signal=0")
              or (a[0] in ("-s", "-n", "--signal") and len(a) > 1 and a[1] in ("0", "EXIT"))):
        return [], None
    targets = kill_targets(a)
    if targets is None:
        return [], None
    pids: List[int] = []
    for t in targets:
        if t.startswith("%"):
            continue
        n = _int_or_none(t)
        if n is not None:
            pids.append(n)
    return pids, None


#: pkill options whose value is the NEXT word (or glued: ``-u1000``).
_PKILL_VALUE_SHORT = frozenset("gGPstuUFqrO")
_PKILL_VALUE_LONG = frozenset({
    "--signal", "--pgroup", "--group", "--parent", "--session", "--terminal",
    "--euid", "--uid", "--pidfile", "--ns", "--nslist", "--queue",
    "--runstates", "--cgroup", "--env", "--older",
})


def pgrep_args_for_pkill(args: Sequence[str]) -> List[str]:
    """``pkill``'s argv with the signal spec removed — the matching options
    ``pgrep`` shares with it, so ``pgrep`` resolves the same pids."""
    a = list(args)
    out: List[str] = []
    i = 0
    if a and is_signal_spec(a[0]):
        i = 1
    while i < len(a):
        t = a[i]
        if t == "--signal":
            i += 2
            continue
        if t.startswith("--signal="):
            i += 1
            continue
        if t in ("-e", "--echo"):  # pkill-only flag
            i += 1
            continue
        out.append(t)
        i += 1
    return out


def _run_pgrep(args: Sequence[str]) -> Optional[List[int]]:
    """Real ``pgrep``. None when it cannot run or errors (exit >= 2)."""
    try:
        res = subprocess.run(["pgrep", *args], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode == 1:
        return []
    if res.returncode != 0:
        return None
    pids: List[int] = []
    for line in res.stdout.split():
        n = _int_or_none(line)
        if n is not None:
            pids.append(n)
    return pids


#: killall options taking a value.
_KILLALL_VALUE = frozenset({"-s", "--signal", "-u", "--user", "-o",
                            "--older-than", "-y", "--younger-than", "-n",
                            "--ns", "-Z", "--context"})


def resolve_killall(args: Sequence[str], table: ProcTable) -> Tuple[List[int], Optional[str]]:
    """The pids ``killall`` (psmisc) would match, from the table: exact
    name (15-char comm, or the full argv[0] basename), ``-r`` regex,
    ``-I`` case-insensitive. ``-u`` with no name is refused (every process
    of that user)."""
    a = list(args)
    i = 0
    if a and is_signal_spec(a[0]):
        i = 1
    names: List[str] = []
    regex = icase = user = False
    while i < len(a):
        t = a[i]
        if t == "--":
            names.extend(a[i + 1:])
            break
        if t in _KILLALL_VALUE:
            if t in ("-u", "--user"):
                user = True
            i += 2
            continue
        if t.startswith("--"):
            regex = regex or t == "--regexp"
            icase = icase or t == "--ignore-case"
            user = user or t.startswith("--user=")
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            regex = regex or "r" in t
            icase = icase or "I" in t
            i += 1
            continue
        names.append(t)
        i += 1
    if not names:
        if user:
            return [], "killall -u with no name signals every process of that user"
        return [], None
    flags = re.IGNORECASE if icase else 0
    pids: List[int] = []
    for row in table.values():
        argv0 = normalize_name(row.cmdline.split()[0]) if row.cmdline else ""
        comm = row.name
        for name in names:
            if regex:
                try:
                    hit = re.search(name, comm, flags) is not None
                except re.error:
                    return [], "killall -r pattern %r does not compile" % name
            else:
                cmp = (lambda x, y: x.lower() == y.lower()) if icase else (lambda x, y: x == y)
                hit = (cmp(comm, name) or cmp(comm, name[:15])
                       or (argv0 != "" and cmp(argv0, normalize_name(name))))
            if hit:
                pids.append(row.pid)
                break
    return pids, None


def _ps_names_to_pids(names: Iterable[str], table: ProcTable) -> List[int]:
    """Windows name match (Stop-Process -Name / taskkill /IM): case-
    insensitive, ``.exe`` optional, ``*``/``?`` wildcards."""
    pats = [normalize_name(n) for n in names if n.strip()]
    pids = []
    for row in table.values():
        rn = normalize_name(row.name)
        if any(fnmatch.fnmatchcase(rn, p) for p in pats):
            pids.append(row.pid)
    return pids


def resolve_stop_process(args: Sequence[str], table: ProcTable) -> Tuple[List[int], Optional[str]]:
    """The PowerShell guard function passes ``--id N`` / ``--name X`` pairs
    (it has already bound Stop-Process's parameters, pipeline included)."""
    a = list(args)
    pids: List[int] = []
    names: List[str] = []
    i = 0
    while i + 1 < len(a):
        if a[i] == "--id":
            n = _int_or_none(a[i + 1])
            if n is None:
                return [], "unreadable process id %r" % a[i + 1]
            pids.append(n)
        elif a[i] == "--name":
            names.append(a[i + 1])
        i += 2
    return pids + _ps_names_to_pids(names, table), None


def resolve_taskkill(args: Sequence[str], table: ProcTable) -> Tuple[List[int], Optional[str]]:
    """``taskkill /PID n /IM name [/T] [/F]``. A ``/FI`` filter is refused:
    it selects by property, and this guard cannot prove what it matches."""
    a = list(args)
    pids: List[int] = []
    names: List[str] = []
    i = 0
    while i < len(a):
        opt = "/" + a[i].lstrip("/-").lower() if a[i][:1] in ("/", "-") else ""
        if opt in ("/pid", "/im", "/fi") and i + 1 < len(a):
            val = a[i + 1]
            if opt == "/pid":
                n = _int_or_none(val)
                if n is None:
                    return [], "unreadable /PID %r" % val
                pids.append(n)
            elif opt == "/im":
                names.append(val)
            else:
                return [], "taskkill /FI selects by filter; name the exact /PID or /IM"
            i += 2
            continue
        i += 1
    return pids + _ps_names_to_pids(names, table), None


# ═══════════════════════════════════════════════════════════════════════
# Entry point used by the CLI
# ═══════════════════════════════════════════════════════════════════════

#: The verbs ``check`` accepts.
VERBS: Tuple[str, ...] = ("kill", "pkill", "killall", "stop-process", "taskkill")

Pgrep = Callable[[Sequence[str]], Optional[List[int]]]


def check_call(verb: str, args: Sequence[str], *,
               table: Optional[ProcTable] = None,
               self_pid: Optional[int] = None,
               pgrep: Optional[Pgrep] = None) -> Optional[str]:
    """Why this ``verb args`` call must not run, or None to let it run.

    ``table``/``self_pid``/``pgrep`` are injectable for tests; production
    passes none of them (the real table, ``os.getpid()``, real ``pgrep``).
    Fail-closed: an unreadable table or a ``pgrep`` that cannot answer is
    a refusal."""
    if verb not in VERBS:
        return "unknown verb %r" % verb
    if self_pid is None:
        self_pid = os.getpid()
    if table is None:
        table = current_table()
    if not table:
        return ("the process table could not be read, so the targets cannot "
                "be checked")
    if verb == "kill":
        pids, err = resolve_kill(args)
    elif verb == "pkill":
        a = list(args)
        if any(t in ("-v", "--inverse") or (t.startswith("-") and not t.startswith("--")
                                            and "v" in t[1:] and not is_signal_spec(t))
               for t in a):
            return "pkill -v (inverse) signals every process NOT matching"
        found = (pgrep or _run_pgrep)(pgrep_args_for_pkill(a))
        if found is None:
            return "pgrep could not resolve which processes pkill would signal"
        pids, err = [p for p in found if p != self_pid], None
    elif verb == "killall":
        pids, err = resolve_killall(args, table)
    elif verb == "stop-process":
        pids, err = resolve_stop_process(args, table)
    else:
        pids, err = resolve_taskkill(args, table)
    if err is not None:
        return err
    return check_pids(pids, table, self_pid)


#: First-line marker of a generated PATH shim (kill_guard.ensure_shims).
SHIM_MARKER = "VCO kill guard PATH shim"


def _is_shim(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return False
    return SHIM_MARKER.encode() in head


def real_binary(verb: str) -> Optional[str]:
    """The first executable ``verb`` on PATH that is not a kill-guard shim —
    what the shim execs after an allow. Skipping by CONTENT (not by
    directory) means no PATH ordering can make a shim exec itself."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, verb)
        if os.path.isfile(cand) and os.access(cand, os.X_OK) and not _is_shim(cand):
            return cand
    return None


__all__ = [
    "Proc", "ProcTable", "PROTECTED_NAMES", "PROTECTED_PREFIXES", "VERBS",
    "ancestors", "check_call", "check_pids", "current_table",
    "is_protected_name", "is_signal_spec", "kill_targets", "normalize_name",
    "pgrep_args_for_pkill", "protected_reason", "read_proc_table",
    "real_binary", "resolve_kill", "SHIM_MARKER", "resolve_killall", "resolve_stop_process",
    "resolve_taskkill",
]
