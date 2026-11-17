# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One machine-wide queue of owed background work and ONE worker (v0.2.107, A-003).

What it replaces
----------------
Owed work — a KG seed, a code-graph walk, a secondary-vector backfill that was
skipped because a backend was down — used to be retried by one detached
``python -m vco_lib.deferral_retry --folder <project>`` driver PER PROJECT PER
SESSION START. Nothing coordinated them: no check that Weaviate was ready, one
full ``sync_knowledge_graph.py --all`` per owed condition, and a slot pool the
driver did not use. After a crash every open project's session start fired its
driver at once — N whole-tree re-syncs against a Weaviate that was still
loading its shards, which crashed it again.

The shape now
-------------
* **A queue of files, not of processes.** ``<vct_root>/state/owed-work/queue/
  <project-key>.json`` — one entry per project, written atomically. Enqueueing
  the same project again rewrites the same file (merging its env), so three
  session starts of one project are one entry. What the entry OWES is read from
  that project's ledger when the worker gets to it, never from the entry: the
  ledger is the truth, and a condition cleared meanwhile is simply not run.
* **One worker per machine** — ``python -m vco_lib.owed_work worker`` holding
  ``owed-work/worker.lock``. A session start (``deferral_retry.spawn_detached``)
  enqueues and calls :func:`ensure_worker`, which starts the worker only when
  none is running or starting; the per-prompt hub revive path
  (``python -m vco_lib.hub_ensure revive``) calls :func:`ensure_worker` too, so a
  queue left behind by a worker that stopped waiting is picked up at the next
  prompt in any project. The worker exits as soon as the queue is empty — and
  also when nothing can run for :data:`IDLE_EXIT_S` (Weaviate not ready, every
  slot busy): the entries stay on disk, and no process sits parked on them.
* **Gated, slotted, one job at a time.** Before a job the worker needs
  Weaviate's ``/v1/.well-known/ready`` to have answered 200 continuously for
  :data:`SETTLE_S` (and the container to be older than that), and a free slot of
  the machine-wide bulk-writer pool (``vco_lib.atomic.bulk_slot_dir`` — the SAME
  directory the code-graph resync drivers use; its size follows the RAM,
  ``atomic.machine_slot_count``). Not ready ⇒ the job stays queued and NO
  attempt is consumed. Each job holds one slot while it runs.
* **Narrow jobs.** ``deferral_retry`` groups the owed conditions into jobs: a
  secondary-vector backfill runs only over rows missing the vector
  (:func:`run_secondary_backfill`), a sync failure re-syncs only the paths that
  failed (:func:`owed_failed_sync_paths`), the node-format refresh runs alone,
  and ``--all`` runs only when the condition is a whole tree owed — once, for
  every condition it covers.
* **Attempts.** The v0.2.91 rule stands: a ``STARTED`` row is written before the
  job and a job whose worker died still counts. An attempt is excused only on
  positive evidence that the MACHINE failed during it — Weaviate answered "not
  ready" right after the job, or its container start time changed
  (:func:`machine_failed_between`); ``deferral_retry`` bounds those excuses.

This module never edits ``vco_lib.codegraph_resync``: the resync drivers keep
their own entry and share the slot directory through ``atomic.machine_slot``.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

#: Under ``vct_root_dir()/state``.
STATE_DIRNAME = "owed-work"
#: A worker spawned this recently is treated as running (it has not taken its
#: lock yet) — the guard that turns N session starts within seconds into ONE
#: worker.
STARTING_GRACE_S = 20.0
#: Seconds the worker keeps polling while nothing can run before it exits
#: (the queue stays; the next session start / prompt restarts it).
IDLE_EXIT_S = 90.0
IDLE_EXIT_ENV = "VCO_OWED_WORK_IDLE_S"
#: Weaviate must have been ready for this long (and its container started at
#: least this long ago) before a job starts — `ready` alone answers 200 the
#: moment shard loading finishes, which is when a crash-restarted Weaviate is
#: still busiest.
SETTLE_S = 30.0
SETTLE_ENV = "VCO_OWED_WORK_SETTLE_S"
POLL_S = 5.0
#: Log files: ``owed-work-<project-key>-<pid>-<stamp>.log`` per job,
#: ``owed-work-worker-<stamp>-<pid>.log`` for the worker's own boot.
LOG_PREFIX = "owed-work-"

#: ``run_secondary_backfill`` exit codes (the handler in ``deferral_retry``
#: maps them).
BACKFILL_DONE = 0
BACKFILL_FAILED = 1
#: Rows the secondary refuses whole remain: only kg-sync's writer can re-lay
#: them, so the whole-tree sync is genuinely owed.
BACKFILL_NEEDS_WALK = 3

#: ``kg_sync_failures_pending``'s recorded failed paths (per project).
FAILED_PATHS_REL = Path(".claude") / "state" / "kg-sync-failed-paths.json"
SYNC_FAILURES_CID = "kg_sync_failures_pending"


# ---------------------------------------------------------------------------
# Paths and keys
# ---------------------------------------------------------------------------


def state_dir() -> Path:
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / "state" / STATE_DIRNAME


def queue_dir() -> Path:
    return state_dir() / "queue"


def worker_lock_path() -> Path:
    return state_dir() / "worker.lock"


def _spawn_lock_path() -> Path:
    return state_dir() / "spawn.lock"


def _starting_stamp() -> Path:
    return state_dir() / "worker.starting"


def logs_dir() -> Optional[Path]:
    try:
        from vco_lib.paths import vct_root_dir

        path = vct_root_dir() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:  # noqa: BLE001 — logging never blocks the work
        return None


def log_glob() -> Optional[str]:
    """The glob every owed-work log matches (install prints it)."""
    d = logs_dir()
    return str(d / f"{LOG_PREFIX}*.log") if d is not None else None


def project_key(folder: Path) -> str:
    """``<name>-<8 hex of the absolute path>``: readable in a log name, and two
    projects with the same folder name never share an entry."""
    absolute = os.path.abspath(str(folder))
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(absolute).name or "root")[:40]
    digest = hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — a name, not a secret
    return f"{name}-{digest}"


def _env_seconds(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


@dataclass
class QueueEntry:
    folder: str
    key: str
    enqueued_at: float
    env: dict = field(default_factory=dict)
    last_try: float = 0.0
    tries: int = 0
    path: Optional[Path] = None

    def to_json(self) -> dict:
        return {"folder": self.folder, "key": self.key, "enqueued_at": self.enqueued_at,
                "env": dict(self.env), "last_try": self.last_try, "tries": self.tries}


def _queue_lock() -> "contextlib.AbstractContextManager[Any]":
    from vco_lib.atomic import exclusive_file_lock

    return exclusive_file_lock(state_dir() / "queue.lock", timeout_s=10)


def _read_entry(path: Path) -> Optional[QueueEntry]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("folder"):
        return None
    raw_env = payload.get("env")
    env: dict = raw_env if isinstance(raw_env, dict) else {}
    try:
        return QueueEntry(
            folder=str(payload["folder"]),
            key=str(payload.get("key") or path.stem),
            enqueued_at=float(payload.get("enqueued_at") or 0.0),
            env={str(k): str(v) for k, v in env.items()},
            last_try=float(payload.get("last_try") or 0.0),
            tries=int(payload.get("tries") or 0),
            path=path,
        )
    except (TypeError, ValueError):
        return None


def _write_entry(entry: QueueEntry) -> Path:
    from vco_lib.atomic import atomic_write_json

    path = queue_dir() / f"{entry.key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, entry.to_json(), mode=0o600)
    entry.path = path
    return path


def enqueue(folder: Path, *, env: Optional[Mapping[str, str]] = None,
            clock: Callable[[], float] = time.time) -> Path:
    """Put ``folder`` in the machine queue (idempotent per project) and return
    its entry file. ``env`` is overlaid on the worker's environment while this
    project's jobs run (install.py carries the seed context this way); a later
    enqueue's values win key by key. Raises ``OSError`` when the queue cannot
    be written — the caller then runs the work itself."""
    folder = Path(os.path.abspath(str(folder)))
    key = project_key(folder)
    with _queue_lock():
        existing = _read_entry(queue_dir() / f"{key}.json")
        merged = dict(existing.env) if existing else {}
        merged.update({str(k): str(v) for k, v in (env or {}).items()})
        entry = QueueEntry(folder=str(folder), key=key, enqueued_at=clock(), env=merged)
        return _write_entry(entry)


def read_queue() -> "list[QueueEntry]":
    """Every readable entry, least recently tried first. Never raises."""
    try:
        paths = sorted(queue_dir().glob("*.json"))
    except OSError:
        return []
    entries = [e for e in (_read_entry(p) for p in paths) if e is not None]
    entries.sort(key=lambda e: (e.last_try, e.enqueued_at))
    return entries


def _remove_entry(entry: QueueEntry) -> bool:
    """Drop ``entry`` unless it was re-enqueued after the worker read it (a
    newer enqueue keeps the file: its project asked again while we ran)."""
    if entry.path is None:
        return False
    try:
        with _queue_lock():
            current = _read_entry(entry.path)
            if current is None:
                return True
            if current.enqueued_at != entry.enqueued_at:
                return False
            entry.path.unlink()
            return True
    except Exception:  # noqa: BLE001 — a leftover entry is re-checked, never lost
        return False


def _note_try(entry: QueueEntry, clock: Callable[[], float]) -> None:
    """Move ``entry`` to the back of the queue (kept for a later pass)."""
    try:
        with _queue_lock():
            current = _read_entry(entry.path) if entry.path else None
            if current is None:
                return
            current.last_try = clock()
            current.tries += 1
            _write_entry(current)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Machine state: is Weaviate ready, and did it restart?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MachineState:
    #: True: ``/v1/.well-known/ready`` answered 200. False: it answered
    #: non-200 (503 while loading) or refused the connection — POSITIVE
    #: evidence it is not ready. None: could not tell.
    ready: Optional[bool]
    #: The Weaviate container's start time (epoch s) — changes on every
    #: restart. None: no VCO container / no runtime / unreadable.
    started_at: Optional[float] = None


def weaviate_ready(url: Optional[str] = None, *, timeout: float = 3.0) -> Optional[bool]:
    """Tri-state readiness probe (see :class:`MachineState.ready`)."""
    import socket
    import urllib.error

    try:
        from vco_lib import weaviate_helpers as _wh

        base = (url or _wh.weaviate_url_default()).rstrip("/")
        status, _body = _wh.http_request("GET", f"{base}/v1/.well-known/ready", timeout=timeout)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (ConnectionRefusedError, ConnectionResetError)):
            return False
        return None
    except (ConnectionRefusedError, ConnectionResetError):
        return False
    except (socket.timeout, TimeoutError, OSError, Exception):  # noqa: BLE001
        return None
    return status == 200


def weaviate_started_at() -> Optional[float]:
    """Start time of VCO's Weaviate container, or None. Read-only."""
    try:
        from vco_lib import containers as _c
        from vco_lib.ollama_pull import inspect_container_run

        runtime = _c._resolve_runtime("podman")  # noqa: SLF001 — THE pin rule's lookup
        if not runtime:
            return None
        name = _c.find_existing_container("weaviate", runtime=runtime)
        if not name:
            return None
        run = inspect_container_run(runtime, name)
        return run.started_at if run is not None and run.running else None
    except Exception:  # noqa: BLE001 — unknown is not "restarted"
        return None


def machine_state() -> MachineState:
    return MachineState(ready=weaviate_ready(), started_at=weaviate_started_at())


def machine_failed_between(before: MachineState, after: MachineState) -> bool:
    """Positive evidence that the MACHINE failed during a job: Weaviate answered
    "not ready" afterwards, or its container start time moved (a restart).
    Unknown readings are never evidence."""
    if after.ready is False:
        return True
    if before.started_at is not None and after.started_at is not None:
        return abs(after.started_at - before.started_at) > 1.0
    return False


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


def worker_running() -> bool:
    """Is a worker holding its lock right now? (A peek: take + release.)"""
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    try:
        with exclusive_file_lock(worker_lock_path(), timeout_s=0):
            return False
    except LockTimeout:
        return True
    except OSError:
        return False


def _worker_starting(now: float) -> bool:
    try:
        age = now - _starting_stamp().stat().st_mtime
    except OSError:
        return False
    return -STARTING_GRACE_S < age < STARTING_GRACE_S


#: Handles of workers we started (Popen's destructor warning otherwise).
_DETACHED: list = []

#: Env keys a worker must not inherit (each names ONE project's KG target;
#: the worker serves every project) — the per-project driver's rule.
_WORKER_ENV_STRIP = frozenset({
    "KG_COLLECTION", "SHARED_KG_COLLECTION", "KG_BASE_DIR", "KG_SYNC_PROJECT_ROOT",
})


def _default_popen(argv: Sequence[str], **kwargs: Any) -> Any:
    return subprocess.Popen(list(argv), **kwargs)  # noqa: S603 — argv is ours


def ensure_worker(*, python: str = "", popen: Optional[Callable[..., Any]] = None,
                  now: Callable[[], float] = time.time, origin: str = "") -> bool:
    """Start the machine's worker when the queue holds work and no worker is
    running or starting. True when a worker is (now) on its way, False when
    the queue is empty or the spawn failed. Never raises."""
    try:
        if not read_queue():
            return False
        if _update_in_progress():
            return False  # S-2: the next prompt after the update starts it
        from vco_lib.atomic import LockTimeout, exclusive_file_lock

        try:
            with exclusive_file_lock(_spawn_lock_path(), timeout_s=2):
                if worker_running() or _worker_starting(now()):
                    return True
                stamp = _starting_stamp()
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.touch()
                os.utime(stamp, (now(), now()))
                return _spawn_worker(python=python, popen=popen, origin=origin)
        except LockTimeout:
            return True  # another caller is spawning it right now
    except Exception:  # noqa: BLE001 — a failed spawn is a no-op
        return False


def _spawn_worker(*, python: str, popen: Optional[Callable[..., Any]], origin: str) -> bool:
    from vco_lib.python_exe import resolve_or_current

    argv = [python or resolve_or_current(), "-m", "vco_lib.owed_work", "worker"]
    log_handle = None
    d = logs_dir()
    if d is not None:
        try:
            path = d / f"{LOG_PREFIX}worker-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
            log_handle = open(path, "ab")  # noqa: SIM115 — closed below
            # The SPAWNER writes the first line (v0.2.91 lesson): a worker that
            # dies before printing still leaves a record.
            log_handle.write((f"# vco owed-work worker spawned "
                              f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} by pid {os.getpid()}"
                              f"{' (' + origin + ')' if origin else ''}\n"
                              f"# argv: {' '.join(argv)}\n").encode("utf-8"))
            log_handle.flush()
        except OSError:
            log_handle = None
    out = log_handle if log_handle is not None else subprocess.DEVNULL
    from vco_lib.install_companions import detached_child_env
    from vco_lib.windowless_spawn import detached_popen_kwargs

    env = {k: v for k, v in detached_child_env().items() if k not in _WORKER_ENV_STRIP}
    kwargs: dict = {"cwd": str(state_dir()), "stdout": out, "stderr": out,
                    "stdin": subprocess.DEVNULL, "env": env}
    kwargs.update(detached_popen_kwargs())
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        _DETACHED.append((popen or _default_popen)(argv, **kwargs))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass


@contextlib.contextmanager
def _job_log(entry: QueueEntry) -> Iterator[None]:
    """Send fd 1/2 (this process's and every child's) to
    ``owed-work-<project-key>-<pid>-<stamp>.log`` while ``entry`` runs."""
    d = logs_dir()
    if d is None:
        yield
        return
    path = d / f"{LOG_PREFIX}{entry.key}-{os.getpid()}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        fh = open(path, "ab")  # noqa: SIM115 — closed below
        saved = (os.dup(1), os.dup(2))
    except OSError:
        yield
        return
    try:
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)
        print(f"# owed work for {entry.folder} (key {entry.key}), worker pid {os.getpid()}",
              flush=True)
        yield
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])
        fh.close()


@contextlib.contextmanager
def _env_overlay(env: Mapping[str, str]) -> Iterator[None]:
    prev = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@dataclass
class WorkerReport:
    exit_reason: str = ""
    #: Folders whose dispatch ran (one entry per run), in order.
    runs: "list[str]" = field(default_factory=list)
    already_running: bool = False


@dataclass
class WorkerDeps:
    """Seams; the defaults are production."""

    state: Callable[[], MachineState] = machine_state
    dispatch: Optional[Callable[..., Any]] = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    slot_dir: Optional[Path] = None
    slots: Optional[int] = None
    idle_s: Optional[float] = None
    settle_s: Optional[float] = None
    poll_s: float = POLL_S
    update_in_progress: Callable[[], bool] = lambda: _update_in_progress()
    job_log: Callable[[QueueEntry], "contextlib.AbstractContextManager[Any]"] = _job_log
    log: Callable[[str], None] = lambda line: print(f"[owed-work] {line}", flush=True)


def _update_in_progress() -> bool:
    """S-2: an orchestrator update is running (``vco_lib.update_gate``). A
    reader that cannot tell reads as "no update" — the gate's own soft-fail."""
    try:
        from vco_lib.update_gate import is_update_in_progress

        return bool(is_update_in_progress())
    except Exception:  # noqa: BLE001
        return False


def run_worker(deps: Optional[WorkerDeps] = None) -> WorkerReport:
    """The machine's one worker loop (see the module docstring)."""
    from vco_lib.atomic import (
        LockTimeout,
        bulk_slot_dir,
        exclusive_file_lock,
        machine_slot_count,
    )

    d = deps or WorkerDeps()
    report = WorkerReport()
    try:
        lock = exclusive_file_lock(worker_lock_path(), timeout_s=2)  # Q-4: ride out a status peek
        lock.__enter__()
    except LockTimeout:
        report.already_running = True
        report.exit_reason = "another worker is running"
        return report
    try:
        try:
            _starting_stamp().unlink()
        except OSError:
            pass
        slot_dir = d.slot_dir or bulk_slot_dir()
        slots = d.slots or machine_slot_count()
        idle_s = d.idle_s if d.idle_s is not None else _env_seconds(IDLE_EXIT_ENV, IDLE_EXIT_S)
        settle_s = d.settle_s if d.settle_s is not None else _env_seconds(SETTLE_ENV, SETTLE_S)
        d.log(f"worker pid={os.getpid()} slots={slots} settle={settle_s:g}s idle-exit={idle_s:g}s")
        _loop(d, report, slot_dir=slot_dir, slots=slots, idle_s=idle_s, settle_s=settle_s)
        d.log(f"worker exits: {report.exit_reason}")
        return report
    finally:
        lock.__exit__(None, None, None)


def _loop(d: WorkerDeps, report: WorkerReport, *, slot_dir: Path, slots: int,
          idle_s: float, settle_s: float) -> None:
    from vco_lib.atomic import try_machine_slot_free

    ready_since: Optional[float] = None
    idle_deadline = d.clock() + idle_s
    waiting_for = ""
    while True:
        entries = read_queue()
        if not entries:
            report.exit_reason = "queue empty"
            return
        updating = d.update_in_progress()
        st = d.state()
        if st.ready is True and not updating:
            if ready_since is None:
                ready_since = d.clock()
        else:
            ready_since = None
        young = st.started_at is not None and d.wall() - st.started_at < settle_s
        settled = ready_since is not None and d.clock() - ready_since >= settle_s and not young
        free = try_machine_slot_free(slot_dir, slots=slots) if settled else None
        if not settled or free is None:
            why = ("an orchestrator update is running" if updating else
                   "Weaviate is not ready" if st.ready is not True else
                   "Weaviate is settling after a start" if not settled else
                   f"all {slots} bulk-writer slot(s) are busy")
            if why != waiting_for:
                d.log(f"{len(entries)} project(s) queued; waiting: {why}")
                waiting_for = why
            if d.clock() >= idle_deadline:
                report.exit_reason = f"nothing could run for {idle_s:g}s ({why}); queue kept"
                return
            d.sleep(d.poll_s)
            continue
        waiting_for = ""
        entry = entries[0]
        kept = _process_entry(entry, d, report, slot_dir=slot_dir, slots=slots)
        if kept:
            _note_try(entry, d.wall)
            # Q-2: kept for a reason other than Weaviate (e.g. another retry
            # driver holds the project) — without a pause this loop would
            # re-dispatch at once, with a container probe each round.
            d.sleep(d.poll_s)
        idle_deadline = d.clock() + idle_s


def _process_entry(entry: QueueEntry, d: WorkerDeps, report: WorkerReport, *,
                   slot_dir: Path, slots: int) -> bool:
    """Run every owed job of one project. True when the entry stays queued."""
    from vco_lib import deferral_retry as dr

    folder = Path(entry.folder)
    if not folder.is_dir():
        d.log(f"{entry.key}: {folder} no longer exists — dropped from the queue")
        _remove_entry(entry)
        return False
    if not dr.owed_condition_ids(folder):
        d.log(f"{entry.key}: nothing owed any more — dropped from the queue")
        _remove_entry(entry)
        return False
    dispatch = d.dispatch or dr.dispatch
    report.runs.append(str(folder))
    with d.job_log(entry), _env_overlay(entry.env):
        try:
            results = dispatch(folder, machine_watch=d.state,
                               slot_guard=_slot_guard_factory(slot_dir, slots))
        except Exception as exc:  # noqa: BLE001 — Q-3: one project's crash must not end the worker
            import traceback

            traceback.print_exc()
            d.log(f"{entry.key}: handler raised {type(exc).__name__}: {exc} — kept queued")
            return True  # the attempt row is already written by the dispatcher
    kept = any(r.status in (dr.QUEUED, dr.INTERRUPTED) for r in results)
    summary = ", ".join(f"{r.condition_id}={r.status}" for r in results) or "nothing ran"
    d.log(f"{entry.key}: {summary}" + (" — kept queued" if kept else ""))
    if not kept:
        _remove_entry(entry)
    return kept


def _slot_guard_factory(slot_dir: Path, slots: int) -> Callable[[bool], Any]:
    """``slot_guard(own_slot)`` for ``deferral_retry.dispatch``: hold one slot of
    the bulk-writer pool around a job — unless the job's child takes its OWN
    slot from the same pool (the resync driver), where holding one here could
    leave a 1-slot machine waiting on itself."""
    from vco_lib.atomic import machine_slot

    def guard(own_slot: bool) -> "contextlib.AbstractContextManager[Any]":
        if own_slot:
            return contextlib.nullcontext()
        return machine_slot(slot_dir, slots=slots, wait_s=6 * 3600.0, poll_s=2.0)

    return guard


# ---------------------------------------------------------------------------
# Narrow job: kg_sync_failures_pending — only the failed paths
# ---------------------------------------------------------------------------


def _failures_entry_stamp(folder: Path) -> Optional[str]:
    """``detected_at`` of the ledger's ``kg_sync_failures_pending`` entry,
    ``""`` when there is none, None when the ledger cannot be read."""
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(Path(folder))
    except Exception:  # noqa: BLE001
        return None
    if not report:
        return ""
    for e in report.entries:
        if getattr(e, "condition_id", "") == SYNC_FAILURES_CID:
            return str(getattr(e, "detected_at", "") or "") or None
    return ""


def _read_failed_paths(folder: Path) -> Optional[dict]:
    try:
        payload = json.loads((Path(folder) / FAILED_PATHS_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _abs(folder: Path, p: str) -> str:
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(str(folder), p))


def owed_failed_sync_paths(folder: Path) -> Optional["list[str]"]:
    """The paths ``kg_sync_failures_pending`` owes, as absolute paths that
    still exist — or None when the entry owes the WHOLE tree: no record, a
    record written for an earlier entry (install.py's enqueued seed re-emits
    the same condition for a whole-tree seed), or every listed file gone."""
    folder = Path(folder)
    record = _read_failed_paths(folder)
    stamp = _failures_entry_stamp(folder)
    if not record or not stamp or record.get("detected_at") != stamp:
        return None
    paths = [_abs(folder, str(p)) for p in record.get("paths") or [] if str(p).strip()]
    existing = [p for p in dict.fromkeys(paths) if os.path.isfile(p)]
    return existing or None


def failed_paths_state(folder: Path) -> "tuple[bool, Optional[list[str]]]":
    """``(entry present, its recorded paths or None)`` — read BEFORE a sync
    re-emits the entry, so :func:`record_sync_failures` knows whether the
    entry it is about to overwrite owed the whole tree."""
    stamp = _failures_entry_stamp(folder)
    if not stamp:
        return False, None
    record = _read_failed_paths(folder)
    if not record or record.get("detected_at") != stamp:
        return True, None
    return True, [str(p) for p in record.get("paths") or []]


def record_sync_failures(folder: Path, failed: Sequence[str], *, whole_tree: bool,
                         prior: "tuple[bool, Optional[list[str]]]") -> None:
    """Called by ``sync_knowledge_graph.py`` right AFTER it emitted
    ``kg_sync_failures_pending``: record which paths that entry owes.

    * a whole-tree run (``--all``) retried everything, so its failures ARE the
      owed set;
    * a narrower run adds its failures to what the entry already owed — unless
      the entry it overwrote owed the whole tree, which then stays owed (no
      record ⇒ the retry runs ``--all``).
    Never raises."""
    folder = Path(folder)
    path = folder / FAILED_PATHS_REL
    try:
        stamp = _failures_entry_stamp(folder)
        had_entry, prior_paths = prior
        if not stamp or (not whole_tree and had_entry and prior_paths is None):
            path.unlink(missing_ok=True)
            return
        paths = [str(p) for p in failed if str(p).strip()]
        if not whole_tree:
            paths = list(prior_paths or []) + paths
        paths = sorted(dict.fromkeys(_abs(folder, p) for p in paths))
        if not paths:
            path.unlink(missing_ok=True)
            return
        from vco_lib.atomic import atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"detected_at": stamp, "paths": paths})
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails a sync
        print(f"   (failed-path record skipped: {exc})", file=sys.stderr)


def settle_clean_sync_paths(folder: Path, synced: Sequence[str]) -> bool:
    """A file-list sync finished with ZERO failures: drop its paths from the
    record, and when nothing the entry owes is left, resolve
    ``kg_sync_failures_pending`` — the narrow clear for the narrow retry. A
    record that does not belong to the current entry proves nothing (the entry
    owes the whole tree) and is left alone. True when the entry was resolved."""
    folder = Path(folder)
    try:
        record = _read_failed_paths(folder)
        stamp = _failures_entry_stamp(folder)
        if not record or not stamp or record.get("detected_at") != stamp:
            return False
        done = {_abs(folder, str(p)) for p in synced}
        left = [p for p in (_abs(folder, str(x)) for x in record.get("paths") or [])
                if p not in done and os.path.isfile(p)]
        if left:
            from vco_lib.atomic import atomic_write_json

            atomic_write_json(folder / FAILED_PATHS_REL, {"detected_at": stamp, "paths": left})
            return False
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(folder, (SYNC_FAILURES_CID,))
        (folder / FAILED_PATHS_REL).unlink(missing_ok=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"   (failed-path settle skipped: {exc})", file=sys.stderr)
        return False


def forget_failed_sync_paths(folder: Path) -> None:
    """A clean whole-tree sync cleared the entry: the record goes with it."""
    try:
        (Path(folder) / FAILED_PATHS_REL).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Narrow job: kg_secondary_backfill_pending — only rows missing the vector
# ---------------------------------------------------------------------------


def run_secondary_backfill(folder: Path, *,
                           service_factory: Optional[Callable[[Path], Any]] = None,
                           collections: Optional[Callable[[Path], "list[str]"]] = None,
                           backfill: Optional[Callable[..., Mapping[Any, str]]] = None,
                           settle: Optional[Callable[..., None]] = None,
                           log: Callable[[str], None] = print) -> int:
    """Backfill every owed (collection, secondary slot) of ``folder`` WITHOUT a
    tree walk: ``kg_secondary_backfill.backfill_before_walk`` alone — the scan
    embeds only rows that lack the vector — then the module's own
    ``sync_deferral`` keeps the ledger row truthful (resolved when nothing is
    owed). Returns :data:`BACKFILL_DONE`, :data:`BACKFILL_FAILED`, or
    :data:`BACKFILL_NEEDS_WALK` when rows the secondary refuses whole remain
    (only kg-sync's writer can re-lay those)."""
    from vco_lib import kg_secondary_backfill as ksb

    folder = Path(folder)
    try:
        if service_factory is None:
            from vco_lib.embedding_service import EmbeddingService

            service_factory = EmbeddingService.for_project
        svc = service_factory(folder)
    except Exception as exc:  # noqa: BLE001 — no backend ⇒ owed, not done
        log(f"[owed-work] backfill: no embedding service for {folder}: {exc}")
        return BACKFILL_FAILED
    try:
        names = (collections or _write_collections)(folder)
        if not names:
            log(f"[owed-work] backfill: no KG collection resolves for {folder} — "
                "the whole-tree sync decides instead")
            return BACKFILL_NEEDS_WALK
        outcomes = (backfill or ksb.backfill_before_walk)(names, svc, folder, log=log)
        if any(v == ksb.RELAY for v in outcomes.values()):
            return BACKFILL_NEEDS_WALK
        owed = ksb.owed_pairs(folder, names, ksb.secondary_write_models(svc))
        (settle or ksb.sync_deferral)(folder, owed, refresh=True)
        return BACKFILL_DONE if not owed else BACKFILL_FAILED
    finally:
        try:
            svc.close()
        except Exception:  # noqa: BLE001
            pass


def _write_collections(folder: Path) -> "list[str]":
    """The collections kg-sync writes for ``folder``: its KG, its development
    collection, and the shared KG unless this project may not write it."""
    from vco_lib.claude_settings_env import read_settings_env
    from vco_lib.kg_embed_model import folder_collections

    names, _target = folder_collections(folder)
    env = read_settings_env(Path(folder))
    if str(env.get("SHARED_KG_WRITE_DISABLED") or "").strip().lower() in ("1", "true", "yes", "on"):
        shared = str(env.get("SHARED_KG_COLLECTION") or "").strip()
        own = str(env.get("KG_COLLECTION") or "").strip()
        names = [n for n in names if n != shared or n == own]
    return names


# ---------------------------------------------------------------------------
# Status (the launcher's Preferences → Services line) and CLI
# ---------------------------------------------------------------------------


def status() -> dict:
    """``{ok, queued, worker_running, projects: [{key, folder, enqueued_at,
    tries}]}`` — what the launcher shows. Read-only, never raises."""
    try:
        entries = read_queue()
        return {
            "ok": True,
            "queued": len(entries),
            "worker_running": worker_running(),
            "projects": [{"key": e.key, "folder": e.folder, "enqueued_at": e.enqueued_at,
                          "tries": e.tries} for e in entries],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "queued": 0, "worker_running": False, "projects": [],
                "error": f"{type(exc).__name__}: {exc}"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.owed_work",
        description="The machine's owed-work queue and its one worker.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker", help="run the worker (exits when the queue is empty)")
    sub.add_parser("ensure", help="start the worker if work is queued and none runs")
    s = sub.add_parser("status", help="print the queue")
    s.add_argument("--json", action="store_true")
    e = sub.add_parser("enqueue", help="queue a project's owed work and start the worker")
    e.add_argument("--folder", type=Path, required=True)
    b = sub.add_parser("backfill", help="secondary-vector backfill only (a job child)")
    b.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "worker":
        run_worker()
        return 0
    if args.cmd == "ensure":
        ensure_worker(origin="ensure")
        return 0
    if args.cmd == "enqueue":
        enqueue(args.folder)
        ensure_worker(origin="enqueue")
        return 0
    if args.cmd == "backfill":
        return run_secondary_backfill(args.folder)
    st = status()
    if args.json:
        print(json.dumps(st))
    else:
        print(f"{st['queued']} project(s) queued; worker "
              f"{'running' if st['worker_running'] else 'not running'}")
        for p in st["projects"]:
            print(f"  - {p['folder']} (tries {p['tries']})")
    return 0


__all__ = [
    "BACKFILL_DONE", "BACKFILL_FAILED", "BACKFILL_NEEDS_WALK", "MachineState",
    "QueueEntry", "WorkerDeps", "WorkerReport", "enqueue", "ensure_worker",
    "failed_paths_state", "forget_failed_sync_paths", "log_glob", "machine_failed_between",
    "machine_state", "owed_failed_sync_paths", "project_key", "read_queue",
    "record_sync_failures", "run_secondary_backfill", "run_worker",
    "settle_clean_sync_paths", "status", "weaviate_ready", "worker_running",
]


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
