# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — one reviewer pass covers a burst of commits.

Every successful commit spawns a detached reviewer run, so a session that
commits several times in a few minutes (the common shape when a decision is
refined commit after commit) used to run SEVERAL multi-minute ``claude -p``
reviews over the same tree at once: they raced on the same snapshot, each
appended its own ``## Status after <sha>`` section, and the node collected
a chain of superseded sections (fixed on the write side in
:mod:`vco_lib.kg_reviewer_guard`).

This module is the ONE home of the coalescing state (owner suggestion
2026-11-04, "when several commits of one session touch the same node within
a short window, one reviewer pass should cover them"):

* **the lock** — ``.claude/state/kg_reviewer.lock`` (git-excluded project
  state): at most ONE reviewer run per project. :func:`acquire` answers
  ``acquired`` / ``busy`` / ``error`` — an I/O failure is never mistaken for
  "another review is running" (review V02105-KGREV2 M3). Every create,
  takeover, heartbeat and release of the lock file happens under a short
  kernel lock (:func:`vco_lib.atomic.exclusive_file_lock`), and a takeover
  CLAIMS the stale file by an atomic rename and re-reads it before creating
  its own, so two contenders can never both take a stale lock (H5). The
  holder refreshes the lock's heartbeat while it runs
  (:meth:`ReviewLock.start_heartbeat`), so a slow live review is never
  taken over (M6). A lock is stale only when its holder is provably gone —
  the pid is dead, or a recycled pid (its process started after the last
  heartbeat), judged by the repo's one cross-OS liveness rule
  (:func:`vco_lib.atomic.claim_owner_gone` over
  :func:`vco_lib.update_lock.pid_alive`: ``OpenProcess`` on Windows, never
  ``os.kill``, H4) — or, when liveness cannot be decided at all, when the
  heartbeat is older than ``stale_s``. A holder that is provably alive is
  never taken over, however old its heartbeat (a laptop that slept
  mid-review wakes up still holding it).
* **the queue** — ``.claude/state/kg_reviewer.queue.json``: the commits
  waiting for a pass, oldest first, each with its failed-pass count. A
  commit leaves it only when a pass that covered it SUCCEEDED (:func:`ack`)
  — a crash or a failed pass leaves it queued (M2) — or by a LOGGED drop:
  a reviewer commit (never reviewed), a sha git can no longer read, a
  commit whose passes failed :data:`MAX_ATTEMPTS` times, the oldest
  entries past :data:`MAX_QUEUE`, or a pass skipped because no reviewer
  rung was available (each covered commit gets its own line). Every
  read-modify-write runs under its own short kernel lock.

The run-side loop that uses this state lives in
``templates/scripts/kg_reviewer_backend.py`` (``_cmd_run``).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from vco_lib.atomic import (
    LockTimeout,
    atomic_write_bytes,
    atomic_write_json,
    claim_by_rename,
    claim_owner_gone,
    exclusive_file_lock,
    release_rename_claim,
)

__all__ = ["lock_path", "queue_path", "ReviewLock", "AcquireResult",
           "ACQUIRED", "BUSY", "ERROR", "acquire", "QueueItem", "enqueue",
           "peek", "ack", "record_failure", "report_error",
           "DEFAULT_STALE_S", "HEARTBEAT_S", "MAX_QUEUE", "MAX_ATTEMPTS"]

Log = Callable[[str], None]

#: The per-project review lock, relative to the project folder.
LOCK_REL = Path(".claude") / "state" / "kg_reviewer.lock"
#: The kernel lock serialising every mutation of the review lock file.
LOCK_OPS_NAME = "kg_reviewer.lock.ops"
#: The coalescing queue, relative to the project folder.
QUEUE_REL = Path(".claude") / "state" / "kg_reviewer.queue.json"
#: The kernel lock guarding every read-modify-write of the queue file.
QUEUE_LOCK_NAME = "kg_reviewer_queue.lock"
#: The rename mark of a claimed stale lock (``<lock><mark><pid>``).
STEAL_MARK = ".stolen."
#: How often the holder refreshes its heartbeat.
HEARTBEAT_S = 60.0
#: Heartbeat age past which a holder whose liveness cannot be decided at
#: all is treated as gone (ten missed beats).
DEFAULT_STALE_S = 600.0
#: A partially written / unparseable lock file older than this is a crashed
#: creator's (a creator writes its payload under the ops lock, in ms).
CORRUPT_GRACE_S = 30.0
#: How long a caller waits for the short ops / queue kernel locks before
#: reporting an error (they are held for milliseconds).
OPS_TIMEOUT_S = 15.0
#: Bound on the queue: past it the OLDEST entries are dropped, each logged.
MAX_QUEUE = 200
#: A queued commit whose passes failed this many times is dropped (logged).
MAX_ATTEMPTS = 3

ACQUIRED = "acquired"
BUSY = "busy"
ERROR = "error"

#: The alert kind a lock / queue failure is filed under (the reviewer's
#: existing alert channel, :mod:`vco_lib.kg_reviewer_pending`).
ERROR_ALERT_KIND = "kg_reviewer_queue_error"


def _noop_log(_line: str) -> None:
    pass


def lock_path(project_root: "str | Path") -> Path:
    return Path(project_root) / LOCK_REL


def queue_path(project_root: "str | Path") -> Path:
    return Path(project_root) / QUEUE_REL


def _ops_path(project_root: "str | Path") -> Path:
    return Path(project_root) / ".claude" / "state" / LOCK_OPS_NAME


def _queue_lock_path(project_root: "str | Path") -> Path:
    return Path(project_root) / ".claude" / "state" / QUEUE_LOCK_NAME


def _sweep_heartbeat_temps(project_root: "str | Path", log: Log) -> None:
    """Delete ``<lock>.hb.<pid>.<token8>`` heartbeat temp files left by a
    run that crashed between the temp write and its rename (review
    V02105-DS). Every LIVE writer creates and replaces its temp under the
    ops kernel lock, so anything present while the caller holds that same
    lock is a dead run's leftover. Called from :func:`acquire` and
    :meth:`ReviewLock.release`. Never raises."""
    state = Path(project_root) / ".claude" / "state"
    try:
        temps = sorted(state.glob(f"{LOCK_REL.name}.hb.*"))
    except OSError:
        return
    for tmp in temps:
        try:
            tmp.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log(f"heartbeat temp file {tmp} could not be removed ({exc})")
            continue
        log(f"removed a leftover heartbeat temp file {tmp.name}")


def _read_payload(path: Path) -> "Tuple[Optional[bytes], Optional[dict]]":
    """``(raw bytes, parsed dict)``; ``(None, None)`` when absent; raw with
    ``None`` when unparseable. Raises OSError on other read failures."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw, None
    return raw, data if isinstance(data, dict) else None


def _holder_gone(data: Optional[dict], raw: bytes, path: Path,
                 stale_s: float, now: float, own_pid: int
                 ) -> "Tuple[bool, str]":
    """``(gone, why)`` for the current lock payload."""
    if data is None:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            age = 0.0
        if age >= CORRUPT_GRACE_S:
            return True, (f"its content is unparseable ({len(raw)} bytes) "
                          f"and {int(age)}s old")
        return False, "the lock file is being written"
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    try:
        beat = float(data.get("heartbeat") or data.get("started") or 0)
    except (TypeError, ValueError):
        beat = 0.0
    age = max(0.0, now - beat)
    if pid == own_pid:
        return False, f"held by this process (pid {pid})"
    if claim_owner_gone(pid, age, stale_after_s=stale_s):
        return True, (f"holder pid {pid} is gone - dead, a recycled pid, or "
                      f"undecidable with a heartbeat {int(age)}s old")
    return False, f"holder pid {pid} is alive (heartbeat {int(age)}s old)"


@dataclass
class ReviewLock:
    """The held review lock: :meth:`heartbeat` / :meth:`start_heartbeat`
    keep it fresh, :meth:`release` drops it — each only while the file
    still carries THIS holder's token (a lock taken over by a successor is
    never refreshed or deleted by the old holder; :attr:`lost` says so)."""

    root: Path
    token: str
    pid: int
    log: Log = _noop_log
    lost: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None

    @property
    def path(self) -> Path:
        return lock_path(self.root)

    def payload(self, now: float) -> bytes:
        return json.dumps({"pid": self.pid, "token": self.token,
                           "started": int(now), "heartbeat": now}
                          ).encode("utf-8")

    def ours(self) -> bool:
        """True while the lock file carries this holder's token."""
        _raw, data = _read_payload(self.path)
        return data is not None and data.get("token") == self.token

    def heartbeat(self) -> bool:
        """Refresh the heartbeat; ``False`` (and :attr:`lost`, logged) when
        the lock is no longer ours. A failed refresh is logged and retried
        at the next beat. Never raises."""
        try:
            with exclusive_file_lock(_ops_path(self.root),
                                     timeout_s=OPS_TIMEOUT_S):
                if not self.ours():
                    if not self.lost:
                        self.log(f"review lock {self.path} is no longer held "
                                 f"by this run - no further pass starts here")
                    self.lost = True
                    return False
                # The temp file keeps the ``<lock>.hb.<pid>.<token8>`` prefix
                # so a crash leftover is still swept by
                # :func:`_sweep_heartbeat_temps`.
                atomic_write_bytes(
                    self.path, self.payload(time.time()), fsync=False,
                    tmp_prefix=(f"{self.path.name}.hb.{self.pid}."
                                f"{self.token[:8]}."))
            return True
        except (OSError, LockTimeout) as exc:
            self.log(f"review lock heartbeat failed ({exc}) - retried at the "
                     f"next beat")
            return True

    def start_heartbeat(self, interval_s: float = HEARTBEAT_S) -> None:
        """Refresh the heartbeat every ``interval_s`` on a daemon thread
        until :meth:`release` (a review's passes run for minutes)."""
        if self._thread is not None:
            return

        def _beat() -> None:
            while not self._stop.wait(interval_s):
                if not self.heartbeat():
                    return

        self._thread = threading.Thread(target=_beat, daemon=True,
                                        name="kg-reviewer-lock-heartbeat")
        self._thread.start()

    def release(self) -> None:
        """Stop the heartbeat and delete the lock — only while it is still
        OURS. A failure is logged. Never raises."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            with exclusive_file_lock(_ops_path(self.root),
                                     timeout_s=OPS_TIMEOUT_S):
                _sweep_heartbeat_temps(self.root, self.log)
                if self.ours():
                    self.path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, LockTimeout) as exc:
            self.log(f"review lock {self.path} could not be released ({exc}) "
                     f"- the next run takes it over once this process is "
                     f"gone")


@dataclass
class AcquireResult:
    #: :data:`ACQUIRED`, :data:`BUSY` or :data:`ERROR`.
    status: str
    #: the held lock (only for :data:`ACQUIRED`).
    lock: Optional[ReviewLock] = None
    #: what happened, for the log (always set for BUSY / ERROR).
    detail: str = ""


def acquire(project_root: "str | Path", *, stale_s: float = DEFAULT_STALE_S,
            log: Optional[Log] = None, pid: Optional[int] = None
            ) -> AcquireResult:
    """Become the project's one running review.

    * :data:`ACQUIRED` — the lock is ours (``result.lock``);
    * :data:`BUSY` — a live holder has it (the caller queues its commit);
    * :data:`ERROR` — the state dir or the lock cannot be used (permissions,
      a read-only file system, a path collision): the caller must surface
      it, never read it as "another review is running".

    A stale lock (:func:`_holder_gone`) is taken over atomically: under the
    ops kernel lock the file is CLAIMED by a rename (exactly one renamer
    wins), the claimed bytes are re-read and must equal what was judged
    stale (else the claim is put back and the run is BUSY), and only then
    is a fresh lock created with ``O_EXCL`` and re-read to confirm it
    carries our token. A takeover is logged. Never raises."""
    log = log or _noop_log
    root = Path(project_root)
    path = lock_path(root)
    holder = ReviewLock(root=root, token=uuid.uuid4().hex,
                        pid=os.getpid() if pid is None else pid, log=log)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # FileExistsError too: a FILE where the dir goes
        return AcquireResult(ERROR, detail=f"{path.parent}: {exc}")
    try:
        with exclusive_file_lock(_ops_path(root), timeout_s=OPS_TIMEOUT_S):
            _sweep_heartbeat_temps(root, log)
            raw, data = _read_payload(path)
            if raw is not None:
                gone, why = _holder_gone(data, raw, path, stale_s,
                                         time.time(), holder.pid)
                if not gone:
                    return AcquireResult(BUSY, detail=why)
                claimed = claim_by_rename(path, STEAL_MARK)
                if claimed is None:
                    return AcquireResult(BUSY, detail="the stale lock was "
                                                      "taken by another run")
                again = claimed.read_bytes()
                if again != raw:
                    # Not the file that was judged: put it back untouched.
                    # A lock that appeared meanwhile is newer and wins; the
                    # claim is then dropped.
                    release_rename_claim(claimed, path, newer_wins=True)
                    return AcquireResult(BUSY, detail="the lock changed "
                                                      "while it was judged")
                log(f"review lock {path} is stale ({why}) - taking it over")
                try:
                    claimed.unlink()
                except OSError:
                    pass
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o600)
            except FileExistsError:
                return AcquireResult(BUSY, detail="another run created the "
                                                  "lock first")
            try:
                os.write(fd, holder.payload(time.time()))
            finally:
                os.close(fd)
            if not holder.ours():
                return AcquireResult(BUSY, detail="the lock is not ours after "
                                                  "creating it")
    except LockTimeout as exc:
        return AcquireResult(ERROR, detail=f"lock ops timed out: {exc}")
    except OSError as exc:
        return AcquireResult(ERROR, detail=f"{path}: {exc}")
    return AcquireResult(ACQUIRED, lock=holder)


# ── the queue ─────────────────────────────────────────────────────────────


@dataclass
class QueueItem:
    sha: str
    attempts: int = 0
    queued: float = 0.0


def _load_queue(project_root: "str | Path") -> List[QueueItem]:
    """The queue (raises OSError / ValueError for the caller to report)."""
    path = queue_path(project_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    if not raw.strip():
        return []
    data = json.loads(raw)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("no 'items' list")
    out: List[QueueItem] = []
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("sha"), str) and it["sha"]:
            out.append(QueueItem(it["sha"], int(it.get("attempts") or 0),
                                 float(it.get("queued") or 0.0)))
    return out


def _save_queue(project_root: "str | Path", items: List[QueueItem]) -> None:
    atomic_write_json(queue_path(project_root),
                      {"v": 1, "items": [{"sha": i.sha, "attempts": i.attempts,
                                          "queued": i.queued} for i in items]},
                      indent=2, fsync=False, mode=0o600)


def _mutate(project_root: "str | Path",
            fn: "Callable[[List[QueueItem]], List[QueueItem]]",
            log: Log, what: str) -> bool:
    try:
        queue_path(project_root).parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(_queue_lock_path(project_root),
                                 timeout_s=OPS_TIMEOUT_S):
            items = _load_queue(project_root)
            _save_queue(project_root, fn(items))
        return True
    except (OSError, ValueError, LockTimeout) as exc:
        log(f"review queue {queue_path(project_root)}: {what} FAILED ({exc})")
        return False


def enqueue(project_root: "str | Path", sha: str,
            log: Optional[Log] = None) -> bool:
    """Append one commit (a FULL sha) to the queue, de-duplicated; past
    :data:`MAX_QUEUE` the oldest entries are dropped, each logged.
    ``False`` (logged) when the queue cannot be written. Never raises."""
    log = log or _noop_log
    sha = sha.strip()

    def _add(items: List[QueueItem]) -> List[QueueItem]:
        if not any(i.sha == sha for i in items):
            items.append(QueueItem(sha, 0, time.time()))
        while len(items) > MAX_QUEUE:
            gone = items.pop(0)
            log(f"review queue over {MAX_QUEUE} commits - DROPPED the oldest, "
                f"{gone.sha[:12]} (never reviewed)")
        return items

    return _mutate(project_root, _add, log, f"enqueue {sha[:12]}")


def peek(project_root: "str | Path", log: Optional[Log] = None
         ) -> Optional[List[QueueItem]]:
    """Every queued commit, oldest first, WITHOUT removing any (they leave
    only through :func:`ack` / :func:`record_failure`). ``None`` (logged)
    when the queue cannot be read. Never raises."""
    log = log or _noop_log
    try:
        if not queue_path(project_root).exists():
            return []
        with exclusive_file_lock(_queue_lock_path(project_root),
                                 timeout_s=OPS_TIMEOUT_S):
            return _load_queue(project_root)
    except (OSError, ValueError, LockTimeout) as exc:
        log(f"review queue {queue_path(project_root)} unreadable ({exc}) - "
            f"its commits stay queued for a later run")
        return None


def ack(project_root: "str | Path", shas: "List[str]",
        log: Optional[Log] = None, *, why: str = "") -> bool:
    """Remove ``shas`` from the queue: their pass succeeded, or ``why``
    names the reason they are dropped (logged per sha). Never raises."""
    log = log or _noop_log
    drop = set(shas)
    if not drop:
        return True
    if why:
        for sha in shas:
            log(f"review queue: dropped {sha[:12]} - {why}")
    return _mutate(project_root,
                   lambda items: [i for i in items if i.sha not in drop],
                   log, f"remove {len(drop)} commit(s)")


def record_failure(project_root: "str | Path", shas: "List[str]",
                   log: Optional[Log] = None) -> bool:
    """Count one failed pass for each of ``shas`` (they stay queued); one
    reaching :data:`MAX_ATTEMPTS` is dropped with a log line. Never
    raises."""
    log = log or _noop_log
    hit = set(shas)

    def _count(items: List[QueueItem]) -> List[QueueItem]:
        out: List[QueueItem] = []
        for i in items:
            if i.sha in hit:
                i.attempts += 1
                if i.attempts >= MAX_ATTEMPTS:
                    log(f"review queue: dropped {i.sha[:12]} after "
                        f"{i.attempts} failed passes (never reviewed)")
                    continue
            out.append(i)
        return out

    return _mutate(project_root, _count, log, "record a failed pass")


def report_error(project_root: "str | Path", session_id: str, detail: str,
                 log: Optional[Log] = None) -> str:
    """Make a lock / queue failure LOUD: the reviewer log, stderr, and one
    row through the reviewer's alert channel (the one
    :mod:`vco_lib.kg_reviewer_pending` files under), so the session that
    triggered the review sees it on its next prompt. Returns the message.
    Never raises."""
    message = (f"kg-reviewer: the review lock/queue under "
               f"{Path(project_root) / '.claude' / 'state'} cannot be used "
               f"({detail}). This commit is reviewed WITHOUT the one-review-"
               f"at-a-time lock; fix that folder (permissions, free space) "
               f"so commits are coalesced again.")
    if log is not None:
        log(message)
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    if session_id:
        try:
            from vco_lib.agent_alerts import append_alert
            from vco_lib.kg_reviewer_pending import (
                ALERT_AGENT_ID,
                ALERT_AGENT_TYPE,
            )

            append_alert(str(project_root), session_id, ALERT_AGENT_ID,
                         ALERT_AGENT_TYPE, ERROR_ALERT_KIND, message)
        except Exception:  # noqa: BLE001 — an alert is never worth a crash
            pass
    return message
