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
   duration. After EOF the server now gives executor work a bounded grace
   (:data:`EXIT_GRACE_SECONDS`), runs the ``atexit`` handlers (telemetry
   flushes such as the embedding-fidelity summary, bounded by
   :data:`EXIT_HANDLERS_SECONDS`) and exits. Limit: weaviate-kg's tools are
   synchronous and run ON the event loop, so the stdio loop only sees EOF once
   the running tool call returns — the grace cannot cut such a call short.
   A client that is gone is caught by the parent watchdog (1.) instead, which
   runs on its own thread and does not wait for the loop.
3. **A wrapper that exits on purpose (POSIX).** A launcher that starts the
   server and then exits while the client keeps the pipe (a double fork)
   looks exactly like a dead client: the server is reparented and exits.
   VCO registers the server so Claude Code spawns it directly; do not put an
   exiting wrapper in front of it.
4. **An exit requested on the loop's thread.** The env-reload SIGHUP handler
   (``claude_mcp_servers/_lib/sighup_handler.py``) raises ``SystemExit`` while
   the loop runs. Left to propagate, interpreter shutdown joins the SDK's
   stdin worker thread (not a daemon), which is blocked reading stdin: the
   process stayed alive with a dead loop, read every later request and
   answered none. :func:`run_stdio_server` turns that exit into
   :func:`_finish`, like the other three.

Two safety nets sit behind those four paths (v0.2.107; field case: a server
stayed alive 54 h after a SIGHUP, all threads parked, its stdin unread, while
Claude Code's ``/mcp`` still said "connected"):

5. **Exit backstop.** Every shutdown path arms :func:`arm_exit_backstop`
   FIRST: a daemon thread that ``os._exit``\\ s the process if it is still
   alive :data:`EXIT_BACKSTOP_SECONDS` later — whatever blocks the exit (a
   logging handler stuck on a full stderr pipe, a non-daemon thread joined by
   interpreter shutdown, an exception that escaped this module). Once a
   shutdown has begun the process ends; a hang there is never the outcome.
6. **Liveness heartbeat.** While the server runs, :class:`Heartbeat` keeps
   ``<vct_root>/run/mcp/<pid>.json`` (pid, process start token, server name,
   script marker, start time) and touches its mtime every
   :data:`HEARTBEAT_SECONDS` — only while the event loop is running and its
   own loop task has ticked within :data:`LOOP_STALL_SECONDS`. The hub's
   watchdog (``launcher/src-tauri/vct-hub/src/mcp_liveness.rs``) ends a
   server that is alive with a stale file (identity checked by pid + start
   token + cmdline), so Claude Code sees EOF and starts a fresh one. The
   file is removed on every exit through :func:`_finish`; one left by a hard
   exit is deleted by the hub once its pid is gone.

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
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "EXIT_BACKSTOP_SECONDS",
    "EXIT_GRACE_SECONDS",
    "HEARTBEAT_SECONDS",
    "LOOP_STALL_SECONDS",
    "PARENT_POLL_SECONDS",
    "Heartbeat",
    "ParentWatch",
    "arm_exit_backstop",
    "heartbeat_dir",
    "run_stdio_server",
    "start_parent_watchdog",
]

#: After stdin EOF, how long in-flight tool work may still run before the
#: process exits anyway. Its results can no longer be delivered (the client is
#: gone); the bound only lets a write that is already under way finish.
EXIT_GRACE_SECONDS = 30.0

#: Upper bound on the ``atexit`` handlers run at exit: a handler that blocks
#: (a flush to a hung disk or service) must not keep a dead client's server
#: alive, which is the whole point of :func:`_finish`.
EXIT_HANDLERS_SECONDS = 10.0

#: How often the parent watchdog checks that the spawning process still lives.
PARENT_POLL_SECONDS = 5.0

#: Once a shutdown path has started, the process is gone this long afterwards
#: at the latest. Above :data:`EXIT_HANDLERS_SECONDS` so :func:`_finish`'s own
#: bounded ``atexit`` run always gets its whole budget before the backstop.
EXIT_BACKSTOP_SECONDS = EXIT_HANDLERS_SECONDS + 5.0

#: How often the liveness heartbeat file is touched. The hub's stale bound
#: (``mcp_liveness.rs`` ``STALE_SECS``, 10 min) is twenty of these.
HEARTBEAT_SECONDS = 30.0

#: The heartbeat stops (and the hub may end the server) once the event loop
#: has not run its own tick for this long. weaviate-kg's tools do synchronous
#: backend I/O ON the loop — one embedding request may take up to 660 s by
#: default (``vco_lib.retry_after.CODE_EMBED_CLIENT_TIMEOUT_SECS``) and a call
#: can make several — so a legitimately long call holds the loop for tens of
#: minutes. Two hours is far past any single call, and still ends a loop that
#: is wedged for good.
LOOP_STALL_SECONDS = 7200.0


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


# Re-entrant: the SIGHUP handler arms it on the main thread, which may be
# inside this very function when the signal lands.
_BACKSTOP_LOCK = threading.RLock()
_backstop: Optional[threading.Thread] = None


def arm_exit_backstop(name: str, code: int = 0, *, delay_s: Optional[float] = None,
                      _exit: Callable[[int], Any] = os._exit) -> bool:
    """A shutdown has begun: make sure the process is gone ``delay_s`` from now.

    Starts a daemon thread that calls ``_exit(code)`` after ``delay_s``
    (default :data:`EXIT_BACKSTOP_SECONDS`). A daemon thread still runs while
    interpreter shutdown joins the non-daemon ones, which is exactly the hang
    it ends. Armed once per process — the first shutdown path to arm it sets
    the deadline; later calls return ``False`` and change nothing. The thread
    neither logs nor writes: the thing blocking the exit may be a full stderr
    pipe, and a write there would block the backstop too.
    """
    global _backstop
    wait = EXIT_BACKSTOP_SECONDS if delay_s is None else delay_s
    with _BACKSTOP_LOCK:
        if _backstop is not None:
            return False

        def _fire() -> None:
            threading.Event().wait(wait)
            _exit(code)

        _backstop = threading.Thread(target=_fire, name=f"{name}-exit-backstop", daemon=True)
        _backstop.start()
    return True


def heartbeat_dir(root: Optional[Path] = None) -> Path:
    """``<vct_root>/run/mcp`` — per user; the hub's watchdog scans it.
    ``root`` overrides the resolved vct root (the uninstaller plans against
    the root it was given).

    Must match ``launcher/src-tauri/vct-hub/src/mcp_liveness.rs::heartbeat_dir``.
    """
    if root is None:
        from vco_lib.paths import vct_root_dir  # noqa: PLC0415

        root = vct_root_dir()
    return root / "run" / "mcp"


def heartbeat_marker(argv0: Optional[str] = None) -> Optional[str]:
    """The script marker the hub looks for in the server's cmdline.

    The last two components of the running script's path, ``/``-joined
    (``weaviate_mcp/server.py``). ``None`` when ``argv[0]`` is not a file
    (``python -c``, an embedded interpreter): a process the hub could not
    identify by its cmdline gets no heartbeat file, so it is never ended.
    """
    raw = sys.argv[0] if argv0 is None else argv0
    if not raw or not os.path.isfile(raw):
        return None
    parts = Path(os.path.abspath(raw)).parts
    return "/".join(parts[-2:])


class Heartbeat:
    """The liveness file of one stdio MCP server process (see module doc, 6.).

    Two sources feed one signal, the file's mtime: a loop task records when
    the event loop last ran it (:meth:`loop_tick`), and a daemon thread
    touches the file every ``interval_s`` while the loop is running and that
    tick is younger than ``stall_s``. The thread rather than the loop does the
    touch so that a tool call holding the loop for minutes (synchronous
    backend I/O) does not read as a hang; a loop that stopped running — the
    54 h field case — or that has been wedged past ``stall_s`` stops it.

    Record fields (must match ``mcp_liveness.rs::McpRecord``): ``pid``,
    ``start_token`` (:func:`vco_lib.deferral_probes.process_start_token`),
    ``server``, ``marker`` (:func:`heartbeat_marker`), ``started_at`` (epoch s).
    """

    def __init__(self, name: str, loop: asyncio.AbstractEventLoop, *, marker: str,
                 directory: Optional[Path] = None,
                 interval_s: float = HEARTBEAT_SECONDS,
                 stall_s: float = LOOP_STALL_SECONDS) -> None:
        self.name = name
        self.marker = marker
        self.loop = loop
        self.interval_s = interval_s
        self.stall_s = stall_s
        self.directory = directory if directory is not None else heartbeat_dir()
        self.path = self.directory / f"{os.getpid()}.json"
        self.last_tick = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _write_record(self) -> None:
        from vco_lib.atomic import atomic_write_json  # noqa: PLC0415
        from vco_lib.deferral_probes import process_start_token  # noqa: PLC0415

        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(self.path, {
            "pid": os.getpid(),
            "start_token": process_start_token(os.getpid()),
            "server": self.name,
            "marker": self.marker,
            "started_at": time.time(),
        }, fsync=False, mode=0o600)

    def loop_is_live(self) -> bool:
        return (self.loop.is_running() and not self.loop.is_closed()
                and time.monotonic() - self.last_tick < self.stall_s)

    def touch(self) -> None:
        try:
            os.utime(self.path)
        except FileNotFoundError:
            try:
                self._write_record()  # removed under us (a hub sweep, a cleanup): recreate
            except OSError:
                pass
        except OSError:
            pass

    async def loop_tick(self) -> None:
        while True:
            self.last_tick = time.monotonic()
            await asyncio.sleep(self.interval_s)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            if self.loop_is_live():
                self.touch()

    def start(self) -> None:
        self._write_record()
        self._thread = threading.Thread(target=self._run, name=f"{self.name}-heartbeat",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.path.unlink()
        except OSError:
            pass


_heartbeat: Optional[Heartbeat] = None


def _start_heartbeat(name: str, loop: asyncio.AbstractEventLoop,
                     directory: Optional[Path]) -> Optional[Heartbeat]:
    """Best effort: a server that cannot write its heartbeat still serves —
    it is then simply not watched by the hub (logged)."""
    global _heartbeat
    marker = heartbeat_marker()
    if marker is None:
        logger.info("%s: no script path in argv; liveness heartbeat not written", name)
        return None
    hb = Heartbeat(name, loop, marker=marker, directory=directory)
    try:
        hb.start()
    except Exception as exc:  # noqa: BLE001 — never block serving
        logger.warning("%s: liveness heartbeat not written (%s); the hub cannot "
                       "end this server if it hangs", name, exc)
        return None
    _heartbeat = hb
    return hb


def _stop_heartbeat() -> None:
    global _heartbeat
    hb, _heartbeat = _heartbeat, None
    if hb is not None:
        hb.stop()


def _finish(name: str, reason: str, code: int = 0, *,
            _exit: Callable[[int], Any] = os._exit) -> None:
    """Flush what must not be lost, then end the process NOW.

    The exit backstop is armed before anything else (a log write below can
    itself block on a full stderr pipe), and the heartbeat file is removed.
    ``atexit`` handlers run next (they are how runs land their telemetry —
    e.g. ``embedding_fidelity``'s summary row); then logging is flushed and
    the process exits without joining worker threads, which is the point.
    """
    arm_exit_backstop(name, code, _exit=_exit)
    _stop_heartbeat()
    logger.warning("%s: %s — exiting", name, reason)
    def _handlers() -> None:
        try:
            atexit._run_exitfuncs()  # noqa: SLF001 — CPython's documented-by-use hook
        except Exception:  # noqa: BLE001 — a failing handler must not block the exit
            pass

    runner = threading.Thread(target=_handlers, name=f"{name}-exit-handlers", daemon=True)
    runner.start()
    runner.join(EXIT_HANDLERS_SECONDS)
    if runner.is_alive():
        logger.warning("%s: exit handlers still running after %.0f s; exiting without them",
                       name, EXIT_HANDLERS_SECONDS)
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


async def _serve_then_drain(serve: Callable[[], Awaitable[Any]], grace_s: float,
                            name: str = "mcp", heartbeat: Optional[Heartbeat] = None,
                            _exit: Callable[[int], Any] = os._exit) -> None:
    ticker = (asyncio.get_running_loop().create_task(heartbeat.loop_tick())
              if heartbeat is not None else None)
    try:
        await serve()
    finally:
        if ticker is not None:
            ticker.cancel()
    # stdin EOF: the shutdown has begun. The drain below is bounded by
    # ``grace_s``; the backstop covers whatever could still block after it.
    arm_exit_backstop(name, 0, delay_s=grace_s + EXIT_BACKSTOP_SECONDS, _exit=_exit)
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
    heartbeat_directory: Optional[Path] = None,
    _finish_fn: Optional[Callable[[str, str, int], Any]] = None,
    _exit_fn: Callable[[int], Any] = os._exit,
) -> None:
    """Run a stdio MCP server whose process ends with its client.

    ``serve`` is the server's stdio coroutine factory (FastMCP's
    ``run_stdio_async``). Arms the parent watchdog, writes the liveness
    heartbeat (``heartbeat_directory``, default :func:`heartbeat_dir`),
    serves until stdin EOF, gives in-flight work ``grace_s``, then exits via
    :func:`_finish`. Every way out arms :func:`arm_exit_backstop` first.
    ``_finish_fn`` / ``_exit_fn`` are test seams.
    """
    finish = _finish_fn if _finish_fn is not None else (
        lambda n, r, c: _finish(n, r, c, _exit=_exit_fn))
    start_parent_watchdog(
        name,
        interval_s=watchdog_interval_s,
        on_gone=lambda: finish(name, "parent process exited (its client is gone)", 0),
    )
    # NOT ``asyncio.run``: its teardown calls ``shutdown_default_executor``
    # again (Python 3.12: up to 300 s) and interpreter exit joins the pool's
    # threads — the very wait this function exists to bound.
    loop = asyncio.new_event_loop()
    heartbeat = _start_heartbeat(name, loop, heartbeat_directory)
    reason, code = "stdin closed (its client is gone)", 0
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve_then_drain(serve, grace_s, name, heartbeat,
                                                  _exit_fn))
    except KeyboardInterrupt:
        reason = "interrupted"
    except SystemExit as exc:
        # An exit requested on this thread while the loop ran (the SIGHUP
        # env-reload handler). Re-raising would hang: interpreter shutdown
        # joins the stdio reader thread, which is blocked on stdin.
        reason = "exit requested (env reload)"
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception:  # noqa: BLE001 — logged, then a non-zero exit
        logger.exception("%s: the stdio server loop failed", name)
        reason, code = "the stdio server loop failed", 1
    except BaseException as exc:  # noqa: BLE001 — e.g. a BaseExceptionGroup
        # Anything else that ends the loop is still an exit: letting it
        # propagate would hand the process to interpreter shutdown, which
        # joins the blocked stdin reader thread (the hang 4. describes).
        reason, code = f"the stdio server loop ended ({type(exc).__name__})", 1
    finish(name, reason, code)
    # Reached only when ``finish`` returns (tests). ``close`` shuts the
    # executor down WITHOUT waiting.
    _stop_heartbeat()
    asyncio.set_event_loop(None)
    loop.close()
