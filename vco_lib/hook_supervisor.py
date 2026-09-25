# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Run a hook's long work so that a hook runner's timeout cannot split it
from the lock that guards it (v0.2.97, review R8 G3).

Two pieces, used together by the container session hooks
(``vco_lib.service_lifecycle`` holds the lock itself):

* :func:`run_supervised` — run a command as a child in ITS OWN process group
  and return only once that child has exited. SIGTERM / SIGINT / SIGHUP sent
  to the supervisor are forwarded to the child's whole group, the supervisor
  waits (bounded, *grace_s*) for the child, and escalates to SIGKILL on the
  group before it returns. A ceiling (*max_run_s*) bounds a hung child the same way. So
  whatever the supervisor holds (a lock descriptor) is released after the
  work ends, never before it. SIGKILL of the supervisor itself cannot be
  forwarded by anything; that is why the session hooks never leave the
  supervisor where a hook runner's timeout could reach it — see below.
* :func:`relay_detached` — start a command DETACHED (a new session on POSIX,
  ``DETACHED_PROCESS`` on Windows: ``install_companions.detached_popen_kwargs``,
  the same discipline the hub and launcher spawns use), with its output in a
  log file, and relay that output to the caller's stdout for at most
  *budget_s*. If the work outlives the budget, the relay says so, names the
  log, and returns — the hook finishes inside its registered timeout while the
  detached work (and the lock it holds) carries on to its real end. The
  pattern is the one ``vco_lib.deferral_retry.spawn_detached`` established
  for session-start work that can take minutes: detached, output to a log
  under ``<vct_root_dir>/logs/`` whose first line the SPAWNER writes. The
  difference is the relay: a container hook's lines are what the session
  reads, so the relay forwards them while it may.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "SUPERVISE_GRACE_S",
    "detached_log_dir",
    "relay_detached",
    "run_supervised",
]

#: Seconds a forwarded signal (or the ceiling's SIGTERM) gets before SIGKILL
#: goes to the child's process group.
SUPERVISE_GRACE_S = 5.0
#: A relay log older than this is pruned when the next one is created.
_LOG_KEEP_S = 7 * 24 * 3600.0
_FORWARDED = ("SIGTERM", "SIGINT", "SIGHUP")

PopenFn = Callable[..., "subprocess.Popen[bytes]"]


def _signal_group(proc: "subprocess.Popen[Any]", sig: int) -> None:
    """*sig* to the child's whole process group (POSIX), or to the child
    (Windows: no process groups to signal; ``terminate``/``kill``)."""
    try:
        if os.name == "nt":
            if sig == getattr(signal, "SIGKILL", None):
                proc.kill()
            else:
                proc.terminate()
            return
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass  # the group is already gone


def run_supervised(
    command: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    max_run_s: Optional[float] = None,
    grace_s: float = SUPERVISE_GRACE_S,
    popen: Optional[PopenFn] = None,
    clock: Callable[[], float] = time.monotonic,
    poll_s: float = 0.1,
) -> int:
    """Run *command* in its own process group and return its exit code once
    it has exited (see the module docstring). A forwarded signal or the
    ceiling returns ``128 + signum`` (the child's own code if it exited
    cleanly within the grace)."""
    kwargs: dict[str, Any] = {"env": dict(env) if env is not None else None}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = (popen or subprocess.Popen)(list(command), **kwargs)  # noqa: S603 — argv is the hook's own

    received: list[int] = []

    def _forward(signum: int, _frame: Any) -> None:
        received.append(signum)
        _signal_group(proc, signum)

    previous: dict[int, Any] = {}
    for name in _FORWARDED:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, _forward)
        except (ValueError, OSError):
            pass  # not the main thread / not settable here: nothing to forward
    start = clock()
    rc = 0
    kill_at: Optional[float] = None
    stopped_by: Optional[int] = None
    try:
        while True:
            try:
                rc = proc.wait(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                pass
            now = clock()
            if kill_at is None:
                if received:
                    stopped_by = received[0]
                    kill_at = now + grace_s
                elif max_run_s is not None and now - start >= max_run_s:
                    stopped_by = int(signal.SIGTERM)
                    _signal_group(proc, stopped_by)
                    kill_at = now + grace_s
            elif now >= kill_at:
                _signal_group(proc, int(getattr(signal, "SIGKILL", signal.SIGTERM)))
                rc = proc.wait()
                break
        # A child that exited on its own is NOT followed by a group kill: what
        # it left running on purpose (a container runtime's monitor process)
        # is not the supervisor's to stop.
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    if stopped_by is not None:
        return 128 + stopped_by
    return int(rc)


def detached_log_dir() -> Path:
    from vco_lib.paths import vct_root_dir  # noqa: PLC0415

    return vct_root_dir() / "logs" / "session-hooks"


def _prune(log_dir: Path, now: float) -> None:
    try:
        for old in log_dir.glob("*.log"):
            try:
                if now - old.stat().st_mtime > _LOG_KEEP_S:
                    old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _open_log(name: str, argv: Sequence[str]) -> tuple[Optional[Path], Any, int]:
    """(path, handle, offset of the first line that is the child's), or
    ``(None, None, 0)`` when no log can be written."""
    try:
        log_dir = detached_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        _prune(log_dir, time.time())
        path = log_dir / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
        handle = open(path, "ab")  # noqa: SIM115 — handed to the child, closed after the spawn
        header = (f"# vco session hook {name} detached "
                  f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n# argv: {' '.join(argv)}\n")
        handle.write(header.encode("utf-8"))
        handle.flush()
        return path, handle, handle.tell()
    except OSError:
        return None, None, 0


def _read_from(path: Optional[Path], offset: int, *, whole: bool = False) -> tuple[str, int]:
    """The log's text from *offset*: whole lines only (a partial last line
    waits for the next read) unless *whole* — the writer has exited."""
    if path is None:
        return "", offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return "", offset
    cut = len(data) if whole else data.rfind(b"\n") + 1
    return data[:cut].decode("utf-8", errors="replace"), offset + cut


def _write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def relay_detached(
    argv: Sequence[str],
    *,
    name: str,
    budget_s: float,
    extra_env: Optional[Mapping[str, str]] = None,
    out: Optional[Callable[[str], None]] = None,
    popen: Optional[PopenFn] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Start *argv* detached and relay its output for up to *budget_s*
    (see the module docstring). Returns the child's exit code when it ended
    inside the budget, else 0 (a session hook never fails the session)."""
    from vco_lib.install_companions import detached_child_env, detached_popen_kwargs  # noqa: PLC0415

    out = out or _write_stdout
    env = detached_child_env()
    env.update(extra_env or {})
    log_path, handle, offset = _open_log(name, argv)
    sink: Any = handle if handle is not None else subprocess.DEVNULL
    try:
        proc = (popen or subprocess.Popen)(  # noqa: S603 — argv is the hook's own
            list(argv), env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
            **detached_popen_kwargs(),
        )
    except OSError as exc:
        out(f"{name}: could not start its container work in the background ({exc}); "
            "nothing was done this session\n")
        return 0
    finally:
        if handle is not None:
            handle.close()
    deadline = clock() + max(0.0, budget_s)
    while True:
        rc = proc.poll()
        text, offset = _read_from(log_path, offset)
        if text:
            out(text)
        if rc is not None:
            tail, offset = _read_from(log_path, offset, whole=True)
            if tail:
                out(tail if tail.endswith("\n") else tail + "\n")
            if log_path is not None:
                try:
                    log_path.unlink()
                except OSError:
                    pass
            return int(rc)
        if clock() >= deadline:
            where = f"; its output continues in {log_path}" if log_path is not None else ""
            out(f"{name}: still working in the background (it holds the container session "
                f"lock until it ends){where}\n")
            return 0
        sleep(0.1)
