# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 L-KGREV2 — one reviewer pass covers a burst of commits.

Every successful commit spawns a detached reviewer run, so a session that
commits several times in a few minutes (the common shape when a decision is
refined commit after commit) used to run SEVERAL multi-minute ``claude -p``
reviews over the same tree at once: they raced on the same snapshot, each
appended its own ``## Status after <sha>`` section, and the node collected
a chain of superseded sections (the finding this lane fixes on the write
side, in :mod:`vco_lib.kg_reviewer_guard`).

This module is the ONE home of the coalescing state (owner suggestion
2026-11-04, "when several commits of one session touch the same node within
a short window, one reviewer pass should cover them"):

* **the lock** — ``.claude/state/kg_reviewer.lock`` (git-excluded project
  state): at most ONE reviewer run per project. A run that cannot take it
  (another review is in flight) :func:`enqueue`s its commit and exits —
  the in-flight run picks the queue up when its pass ends. A lock whose
  holder is provably dead, or older than ``stale_s`` (default 2 h, well
  past a run's 900 s timeout), is STOLEN with a loud log line: a crashed
  run must not mute the reviewer until somebody deletes a file by hand.
  Pid liveness is a signal-0 PROBE, never a kill.
* **the queue** — ``.claude/state/kg_reviewer.queue``: one FULL sha per
  line, appended under a short lock (:func:`vco_lib.atomic.
  portable_exclusive_lock`, the same primitive the pending-file ledger
  uses). :func:`drain` reads and clears it in one step. Bounded by
  construction: every drain is consumed WHOLE by the next pass (its newest
  commit is reviewed, the rest are named in that pass's prompt), so the
  queue cannot grow past one in-flight review's arrival burst, and a
  commit is never dropped — it either got its own pass or is named in one.

The run-side loop that uses this state lives in
``templates/scripts/kg_reviewer_backend.py`` (``_cmd_run``).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, List, Optional

from vco_lib.atomic import portable_exclusive_lock

__all__ = ["lock_path", "queue_path", "acquire", "release", "enqueue",
           "drain"]

Log = Callable[[str], None]

#: The per-project review lock, relative to the project folder.
LOCK_REL = Path(".claude") / "state" / "kg_reviewer.lock"
#: The coalescing queue, relative to the project folder.
QUEUE_REL = Path(".claude") / "state" / "kg_reviewer.queue"
#: Lock guarding every read-modify-write of the queue file.
QUEUE_LOCK_NAME = "kg_reviewer_queue.lock"
#: A lock older than this is stolen even when its pid looks alive (pid
#: reuse, a hung run): 2 h is past a run's whole wall-clock budget
#: (``DEFAULT_TIMEOUT_S`` 900 s per pass, several passes), so a live
#: holder this old is not making progress.
DEFAULT_STALE_S = 7200.0


def _noop_log(_line: str) -> None:
    pass


def lock_path(project_root: "str | Path") -> Path:
    return Path(project_root) / LOCK_REL


def queue_path(project_root: "str | Path") -> Path:
    return Path(project_root) / QUEUE_REL


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness of a pid (conservative: any doubt reads alive —
    the safe direction is to keep waiting, never to steal a live lock)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 is a probe, never a signal
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def acquire(project_root: "str | Path", *, stale_s: float = DEFAULT_STALE_S,
            log: Optional[Log] = None, pid: Optional[int] = None
            ) -> bool:
    """Become the project's one running review; ``False`` when another live
    run holds the lock (the caller then :func:`enqueue`s its commit).

    A lock file whose recorded pid is dead — or whose age exceeds
    ``stale_s`` whatever the pid says — is removed and retried once, with a
    loud log line naming what was stolen. Never raises."""
    log = log or _noop_log
    path = lock_path(project_root)
    payload = json.dumps({"pid": os.getpid() if pid is None else pid,
                          "started": int(time.time())})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        holder_pid = 0
        try:
            holder = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(holder, dict):
                holder_pid = int(holder.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            holder_pid = 0
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        if _pid_alive(holder_pid) and age < stale_s:
            return False
        log(f"review lock {path} is stale (holder pid {holder_pid} dead or "
            f"lock {int(age)}s old > {int(stale_s)}s) - stealing it")
        try:
            path.unlink()
        except OSError:
            return False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return False
    except OSError:
        return False
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def release(project_root: "str | Path", *, pid: Optional[int] = None
            ) -> None:
    """Drop the lock — only when it is still OURS (a stolen-then-recreated
    lock belongs to the successor, never to this process). Never raises."""
    path = lock_path(project_root)
    mine = os.getpid() if pid is None else pid
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if isinstance(holder, dict) and holder.get("pid") == mine:
        try:
            path.unlink()
        except OSError:
            pass


def enqueue(project_root: "str | Path", sha: str,
            log: Optional[Log] = None) -> bool:
    """Append one commit sha to the coalescing queue. Never raises."""
    log = log or _noop_log
    path = queue_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with portable_exclusive_lock(
                path.parent / QUEUE_LOCK_NAME, stale_after_s=60.0):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(sha.strip() + "\n")
        return True
    except OSError:
        log(f"could not enqueue {sha} in {path}: the commit waits for the "
            f"next commit's own review")
        return False


def drain(project_root: "str | Path") -> List[str]:
    """Take every queued sha (in arrival order, de-duplicated) and clear the
    queue in the same locked step. An unreadable queue reads as empty.
    Never raises."""
    path = queue_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with portable_exclusive_lock(
                path.parent / QUEUE_LOCK_NAME, stale_after_s=60.0):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return []
            shas: List[str] = []
            for line in text.splitlines():
                sha = line.strip()
                if sha and sha not in shas:
                    shas.append(sha)
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
            return shas
    except OSError:
        return []
