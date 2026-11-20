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
its pid into ``<lock>/pid`` and its process start token
(:func:`vco_lib.deferral_probes.process_start_token`) into ``<lock>/token``.
A lock is broken (and the acquire retried once) only when its holder is
PROVABLY gone:

* the recorded pid is dead (:func:`vco_lib.update_lock.pid_alive` answers
  ``False``), or
* the pid is alive but names a DIFFERENT process than the one that took the
  lock — the recorded token compared by
  :func:`vco_lib.deferral_probes.process_identity_verdict` reads ``reused``
  (WAVE2-CHECK L-2: after a hard kill or a reboot, the pid of a dead holder is
  handed to another long-lived process, and a bare-pid check kept every
  code-graph writer ``busy`` forever).

A lock without a readable pid is broken only after
:data:`STALE_NO_PID_SECONDS` (a partial acquire). A live holder, one whose
identity cannot be compared (no token recorded, the process cannot be
inspected, the cannot-tell band), keeps its lock whatever its age — a slow
first walk on a slow embedder can legitimately run for hours, and breaking a
live lock would start a second analyzer on the same collection.

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


def _default_reused(pid: int, recorded: str) -> bool:
    """True only when ``pid`` provably names a different process than the
    one whose start token was ``recorded``."""
    from vco_lib.deferral_probes import IDENTITY_REUSED, process_identity_verdict

    return process_identity_verdict(pid, recorded) == IDENTITY_REUSED


def _default_token(pid: int) -> Optional[str]:
    from vco_lib.deferral_probes import process_start_token

    return process_start_token(pid)


def holder_is_stale(
    lock: Path,
    *,
    alive: Callable[[int], Optional[bool]] = _default_alive,
    reused: Callable[[int, str], bool] = _default_reused,
    now: Callable[[], float] = time.time,
) -> bool:
    """True only when the lock's holder is PROVABLY gone.

    Recorded pid → stale iff ``alive(pid)`` is ``False`` (``None`` = cannot
    tell = not stale), or the pid is alive and the recorded start token
    proves it was reused. No readable pid → stale iff the directory is older
    than :data:`STALE_NO_PID_SECONDS`.
    """
    try:
        raw = (lock / "pid").read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if raw.isdigit():
        pid = int(raw)
        state = alive(pid)
        if state is False:
            return True
        if state is True:
            try:
                token = (lock / "token").read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
            return bool(token) and reused(pid, token)
        return False
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
    reused: Callable[[int, str], bool] = _default_reused,
    token: Callable[[int], Optional[str]] = _default_token,
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
            if attempt == 1 and holder_is_stale(lock, alive=alive, reused=reused, now=now):
                if log is not None:
                    log(f"lock: breaking the stale lock {lock.name} (its holder is gone)")
                shutil.rmtree(lock, ignore_errors=True)
                continue
            return None
        except OSError:
            return None
        # The token first: a reader that sees the pid then also sees the
        # token that proves its identity.
        mine = token(os.getpid())
        try:
            if mine:
                (lock / "token").write_text(mine, encoding="utf-8")
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
        (lock / "token").unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        pass
