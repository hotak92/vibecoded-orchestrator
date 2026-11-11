# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A stdio MCP server's lifetime is its client's — the one home (v0.2.106).

Field report (a Windows user, RAM saturated): MCP servers missed Claude Code's
30 s connect timeout at session start and the abandoned server processes
stayed alive, each holding its memory. A stdio MCP server has exactly one
client — whoever holds the other end of its stdin/stdout — so once that
client is gone the process has nothing left to do. Two ways it could outlive
it, both closed here:

1. **The client went away without closing the pipe** — its process died or
   killed only the process it spawned. On Windows a venv's ``python.exe`` is a
   launcher that starts the real interpreter as its CHILD, and killing the
   launcher leaves that child running. Detected by a parent watchdog: the
   parent this process started with has exited (Windows: a handle opened at
   startup is signalled; POSIX: ``getppid()`` changed, i.e. we were
   reparented).
2. **stdin reached EOF but the process did not exit.** The MCP SDK's stdio
   loop returns on EOF, but ``asyncio.run`` then waits for the default
   thread-pool executor, and interpreter shutdown joins its worker threads —
   a tool call blocked in a slow Weaviate / Ollama request (shipped timeouts
   are long by design) kept a disconnected server alive for its whole
   duration. After EOF the server now gives in-flight work a bounded grace
   (:data:`EXIT_GRACE_SECONDS`), runs the ``atexit`` handlers (telemetry
   flushes such as the embedding-fidelity summary) and exits.

The reaper in :mod:`vco_lib.mcp_singleton` stays as it was: it handles
cross-workspace peers at spawn time; this module makes each server end itself.

What this cannot detect: a client that stays alive, keeps the pipe open and
simply never talks again. Nothing on the server side distinguishes that from
an idle session, so the server keeps waiting — the client owns that case.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import sys
import threading
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "EXIT_GRACE_SECONDS",
    "PARENT_POLL_SECONDS",
    "ParentWatch",
    "run_stdio_server",
    "start_parent_watchdog",
]

#: After stdin EOF, how long in-flight tool work may still run before the
#: process exits anyway. Its results can no longer be delivered (the client is
#: gone); the bound only lets a write that is already under way finish.
EXIT_GRACE_SECONDS = 30.0

#: How often the parent watchdog checks that the spawning process still lives.
PARENT_POLL_SECONDS = 5.0


class ParentWatch:
    """Answers "has the process that started us exited?" — never a guess.

    POSIX: an orphan is reparented, so ``getppid()`` differing from the value
    recorded at start means the parent is gone. Windows does not reparent and
    PIDs are reused, so a SYNCHRONIZE handle to the parent is opened at start;
    it stays valid for that exact process and is signalled when it exits.
    Any failure to set up the check yields a watch that always answers
    "alive" (conservative: never exit on uncertainty).
    """

    def __init__(self, *, _getppid: Callable[[], int] = os.getppid,
                 _platform: str = sys.platform) -> None:
        self._getppid = _getppid
        self._windows = _platform == "win32"
        self._initial_ppid: Optional[int] = None
        self._handle: Any = None
        try:
            self._initial_ppid = int(_getppid())
        except Exception:  # noqa: BLE001 — no parent id → never "gone"
            self._initial_ppid = None
        if self._windows and self._initial_ppid:
            self._handle = _win_open_process(_win_watch_pid(self._initial_ppid))

    @property
    def initial_ppid(self) -> Optional[int]:
        return self._initial_ppid

    def parent_gone(self) -> bool:
        if self._initial_ppid is None:
            return False
        if self._windows:
            return _win_process_exited(self._handle) if self._handle else False
        try:
            return int(self._getppid()) != self._initial_ppid
        except Exception:  # noqa: BLE001
            return False


def _same_path(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def pick_watch_pid(parent_pid: int, parent_image: Optional[str], own_image: Optional[str],
                   venv_executable: Optional[str], grandparent_pid: Optional[int]) -> int:
    """The pid whose exit means our client is gone (pure; unit-tested).

    On Windows a venv's ``Scripts\\python.exe`` is a launcher stub that starts
    the base interpreter as its child and WAITS for it: the stub lives exactly
    as long as we do, so watching it never fires when the client (Claude Code)
    dies — the field case of orphaned "venv launcher + child" pairs. The parent
    is that stub when its image is ``sys.executable`` (the venv's python.exe)
    while our own image is a different file; then the client is its parent.
    Same rule as vct-coordination's lifecycle module (separate project), whose
    Windows CI runs the real process tree."""
    if (grandparent_pid and _same_path(parent_image, venv_executable)
            and own_image and not _same_path(own_image, venv_executable)):
        return grandparent_pid
    return parent_pid


def _kernel32() -> Any:
    """kernel32 with typed signatures: an untyped int is passed as a 32-bit C
    int, which truncates a 64-bit HANDLE."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    return k32


def _win_image(k32: Any, pid: int) -> Optional[str]:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else None
    finally:
        k32.CloseHandle(h)


def _win_parent_of(k32: Any, pid: int) -> Optional[int]:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        k32.CloseHandle(snap)


def _win_watch_pid(parent_pid: int) -> int:
    """Climb past the venv launcher stub (Windows). Any failure → the parent."""
    try:
        k32 = _kernel32()
        return pick_watch_pid(parent_pid, _win_image(k32, parent_pid),
                              _win_image(k32, os.getpid()), sys.executable,
                              _win_parent_of(k32, parent_pid))
    except Exception:  # noqa: BLE001 — conservative: keep watching the parent
        return parent_pid


def _win_open_process(pid: int) -> Any:
    """SYNCHRONIZE handle to ``pid`` (Windows), or None."""
    try:
        handle = _kernel32().OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        return handle or None
    except Exception:  # noqa: BLE001
        return None


def _win_process_exited(handle: Any) -> bool:
    try:
        wait_object_0 = 0
        return _kernel32().WaitForSingleObject(handle, 0) == wait_object_0
    except Exception:  # noqa: BLE001
        return False


def _finish(name: str, reason: str, code: int = 0, *,
            _exit: Callable[[int], Any] = os._exit) -> None:
    """Flush what must not be lost, then end the process NOW.

    ``atexit`` handlers run first (they are how runs land their telemetry —
    e.g. ``embedding_fidelity``'s summary row); then logging is flushed and
    the process exits without joining worker threads, which is the point.
    """
    logger.warning("%s: %s — exiting", name, reason)
    try:
        atexit._run_exitfuncs()  # noqa: SLF001 — CPython's documented-by-use hook
    except Exception:  # noqa: BLE001 — a failing handler must not block the exit
        pass
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    _exit(code)


def start_parent_watchdog(
    name: str,
    *,
    interval_s: float = PARENT_POLL_SECONDS,
    watch: Optional[ParentWatch] = None,
    on_gone: Optional[Callable[[], Any]] = None,
) -> Optional[threading.Thread]:
    """Start a daemon thread that ends the process when its parent exits.

    Returns the thread, or None when no parent could be identified (then
    nothing is watched — never exit on a guess).
    """
    w = watch if watch is not None else ParentWatch()
    if w.initial_ppid is None:
        return None
    action = on_gone if on_gone is not None else (
        lambda: _finish(name, f"parent process {w.initial_ppid} exited (its client is gone)")
    )
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_s):
            if w.parent_gone():
                action()
                return

    t = threading.Thread(target=_loop, name=f"{name}-parent-watchdog", daemon=True)
    t.stop = stop  # type: ignore[attr-defined] — test seam
    t.start()
    return t


async def _serve_then_drain(serve: Callable[[], Awaitable[Any]], grace_s: float) -> None:
    await serve()
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.shutdown_default_executor(), timeout=grace_s)
    except asyncio.TimeoutError:
        logger.warning(
            "in-flight tool work did not finish within %.0f s of the client "
            "closing stdin; it is abandoned", grace_s,
        )
    except Exception:  # noqa: BLE001 — the exit below still happens
        pass


def run_stdio_server(
    serve: Callable[[], Awaitable[Any]],
    *,
    name: str,
    grace_s: float = EXIT_GRACE_SECONDS,
    watchdog_interval_s: float = PARENT_POLL_SECONDS,
    _finish_fn: Optional[Callable[[str, str, int], Any]] = None,
) -> None:
    """Run a stdio MCP server whose process ends with its client.

    ``serve`` is the server's stdio coroutine factory (FastMCP's
    ``run_stdio_async``). Arms the parent watchdog, serves until stdin EOF,
    gives in-flight work ``grace_s``, then exits via :func:`_finish`.
    """
    finish = _finish_fn if _finish_fn is not None else _finish
    start_parent_watchdog(
        name,
        interval_s=watchdog_interval_s,
        on_gone=lambda: finish(name, "parent process exited (its client is gone)", 0),
    )
    # NOT ``asyncio.run``: its teardown calls ``shutdown_default_executor``
    # again (Python 3.12: up to 300 s) and interpreter exit joins the pool's
    # threads — the very wait this function exists to bound.
    loop = asyncio.new_event_loop()
    reason, code = "stdin closed (its client is gone)", 0
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve_then_drain(serve, grace_s))
    except KeyboardInterrupt:
        reason = "interrupted"
    except Exception:  # noqa: BLE001 — logged, then a non-zero exit
        logger.exception("%s: the stdio server loop failed", name)
        reason, code = "the stdio server loop failed", 1
    finish(name, reason, code)
    # Reached only when ``finish`` returns (tests). ``close`` shuts the
    # executor down WITHOUT waiting.
    asyncio.set_event_loop(None)
    loop.close()
