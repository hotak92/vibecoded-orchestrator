# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The per-checkout analyzer lock of the code-graph writers (v0.2.107 CG-3).

ONE home for the ``<state_dir>/codegraph_drain_root_<md5(root)>.lock``
directory that serialises analyzer runs over the same checkout: the HEAD
catch-up (:mod:`vco_lib.codegraph_head_catchup`), the extras refresh
(:mod:`vco_lib.codegraph_extras_refresh`) and the daily path reconciliation
(:mod:`vco_lib.codegraph_path_reconcile`). Before v0.2.107 the Stop hook's
queue drain broke a dead holder's lock in shell; that drain is retired, so the
break lives here, beside the acquire.

The lock is a DIRECTORY created with an atomic ``mkdir``; the holder writes
its pid into ``<lock>/pid``. A lock whose recorded pid is PROVABLY dead
(:func:`vco_lib.update_lock.pid_alive` answers ``False``) is broken and the
acquire retried once. A lock without a readable pid is broken only after
:data:`STALE_NO_PID_SECONDS` (a partial acquire). A live holder, or one whose
liveness cannot be determined, keeps its lock whatever its age — a slow first
walk on a slow embedder can legitimately run for hours, and breaking a live
lock would start a second analyzer on the same collection.

The lock name keeps its historical ``codegraph_drain_root_`` prefix so an
in-flight run of the previous release still serialises with this one.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

#: A lock directory with no readable pid older than this is a partial
#: acquire that will never be released.
STALE_NO_PID_SECONDS = 30 * 60


def path_key(path: str) -> str:
    """md5 of a path string — the lock-name key (not security)."""
    return hashlib.md5(path.encode("utf-8")).hexdigest()  # noqa: S324


def lock_path(state_dir: Path, key: str) -> Path:
    """Where the lock for ``key`` lives."""
    return state_dir / f"codegraph_drain_root_{key}.lock"


def _default_alive(pid: int) -> Optional[bool]:
    from vco_lib.update_lock import pid_alive

    return pid_alive(pid)


def holder_is_stale(
    lock: Path,
    *,
    alive: Callable[[int], Optional[bool]] = _default_alive,
    now: Callable[[], float] = time.time,
) -> bool:
    """True only when the lock's holder is PROVABLY gone.

    Recorded pid → stale iff ``alive(pid)`` is ``False`` (``None`` = cannot
    tell = not stale). No readable pid → stale iff the directory is older
    than :data:`STALE_NO_PID_SECONDS`.
    """
    try:
        raw = (lock / "pid").read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if raw.isdigit():
        return alive(int(raw)) is False
    try:
        age = now() - lock.stat().st_mtime
    except OSError:
        return False
    return age > STALE_NO_PID_SECONDS


def acquire(
    state_dir: Path,
    key: str,
    *,
    alive: Callable[[int], Optional[bool]] = _default_alive,
    now: Callable[[], float] = time.time,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[Path]:
    """Take the lock for ``key``. Returns the lock path, or None when busy
    (or the state dir cannot be written). Breaks a provably dead holder's
    lock once, then retries."""
    lock = lock_path(state_dir, key)
    for attempt in (1, 2):
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            lock.mkdir()
        except FileExistsError:
            if attempt == 1 and holder_is_stale(lock, alive=alive, now=now):
                if log is not None:
                    log(f"lock: breaking the stale lock {lock.name} (its holder is gone)")
                shutil.rmtree(lock, ignore_errors=True)
                continue
            return None
        except OSError:
            return None
        try:
            (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        return lock
    return None


def release(lock: Optional[Path]) -> None:
    """Release a lock :func:`acquire` returned. Soft-fail."""
    if lock is None:
        return
    try:
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        pass
