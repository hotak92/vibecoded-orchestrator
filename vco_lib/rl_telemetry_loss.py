# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RL telemetry LOSS ledger — the one home for "a training event was not written".

Why this exists (v0.2.100)
--------------------------
Standing owner rule: RL is optional, but its training logs are ALWAYS
collected. Two ways a log can go missing were invisible until now:

* ``dual_skip`` — the dual-RL-log fan-out (the ``<task_id>:<slot>`` twin event
  for the OTHER embedding slot) was wanted (dual-log on, a distinct secondary
  slot configured) but could not be produced on this call: the secondary query
  embed overran the hook-path budget, the embed failed, or the query was
  oversized. Without a record, "fewer twins" is indistinguishable from "twins
  lost".
* ``dual_partial`` — the twin was written, but some nodes of the primary
  event had no vector for the other slot (never dual-written, and the hook path
  does not backfill), so the twin carries fewer nodes. Counted apart from the
  event losses.
* ``hub_post_failed`` — the vct-hub POST of an RL event failed (hub not
  running, connection refused, timeout, a 4xx/5xx). The writer is soft-fail by
  design, and the connection-level cases used to be logged at DEBUG only, so
  the loss could not be measured.
* ``deferred_unsent`` — a deferred-emit child loaded events but never sent
  them (v0.2.101 NB-04: a loader/emit crash, a writer that raised, a validation
  failure). Recorded apart from ``hub_post_failed`` because no POST was ever
  attempted for these events, so nothing else names them.
* ``writer_unavailable`` — an event was dropped because no telemetry writer
  could be constructed for it (v0.2.102 W3R-5: the writer factory raised or
  returned None — a broken install — or the deferred-emit parent could not
  resolve the writer's construction params and dropped the event before
  spawning the child). Previously these paths were DEBUG-log-only.

Every such loss appends ONE JSON line to
``<vct_root>/metrics/rl_telemetry_loss.jsonl`` (the shared metrics home, beside
``embedding_failures.jsonl``) and logs one WARNING per (kind, reason) per
process — loud enough to be seen, rate-limited so a downed hub does not flood
an MCP's stderr. The ledger's reader is :func:`summarize`, which ``vco doctor``
renders as the ``rl_telemetry_loss`` probe.

Never raises: recording a loss must never become a second failure on the path
that is already losing an event.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

LOSS_FILE_NAME = "rl_telemetry_loss.jsonl"

#: The dual-log twin event was wanted but not produced on this call.
KIND_DUAL_SKIP = "dual_skip"
#: An RL event POST to vct-hub did not land.
KIND_HUB_POST_FAILED = "hub_post_failed"
#: A dual-log twin WAS written but some of the primary's nodes had no vector
#: for the other slot (``missing`` / ``nodes`` in the record). Not an event
#: loss — a partial one — so :func:`summarize` counts it apart (W5R-08).
KIND_DUAL_PARTIAL = "dual_partial"
#: A deferred-emit child (``claude_mcp_servers/rl_client/deferred_emit``)
#: loaded events but never sent them — a loader/emit crash, a writer that
#: raised, a validation failure (v0.2.101 NB-04). This is the ONLY deferred
#: case with no per-event line of its own: an event whose POST was ATTEMPTED
#: and failed is recorded per event under :data:`KIND_HUB_POST_FAILED`
#: instead, so the two never count the same event twice.
KIND_DEFERRED_UNSENT = "deferred_unsent"
#: An RL event was dropped because NO TELEMETRY WRITER could be constructed
#: for it — the writer factory raised or returned None (a broken install:
#: ``RLTelemetryWriter`` unimportable), or the deferred-emit parent could not
#: resolve the writer's construction params and dropped the event before the
#: child ever saw it (v0.2.102 W3R-5). Exactly the install state where
#: visibility matters most, so it is ledgered rather than DEBUG-logged.
KIND_WRITER_UNAVAILABLE = "writer_unavailable"
#: A ``bash_outcome`` emit died BEFORE the hub POST was attempted
#: (v0.2.102 W3R-4): the post-bash hook's emit child could not run (missing
#: / bogus venv), crashed, raised, or read a corrupt pairing state file —
#: pre-fix the pairing file was then deleted unconditionally and the loss
#: was invisible (``hub_post_failed`` only fires once a POST is attempted).
#: Recorded by the hook itself (the child when it can run, the wrapper via
#: the orchestrator checkout otherwise); the pairing file is only deleted
#: once the emit succeeded OR the loss is ledgered here.
KIND_OUTCOME_UNSENT = "outcome_unsent"

#: Once the ledger passes ``_MAX_BYTES`` it is cut to its newest
#: ``_KEEP_LINES`` lines (through the repo's one rotation home,
#: ``vco_lib.atomic.rotate_tail_lines``). A downed hub produces a line per
#: event; the counts that matter are recent ones.
_MAX_BYTES = 2 * 1024 * 1024
_KEEP_LINES = 10_000

_warned: set = set()
_lock = threading.Lock()


def loss_log_path() -> Path:
    """``<vct_root>/metrics/rl_telemetry_loss.jsonl`` (via the one metrics home)."""
    from vco_lib.paths import vct_metrics_dir

    return vct_metrics_dir() / LOSS_FILE_NAME


def record_loss(kind: str, reason: str, **detail: Any) -> None:
    """Append one loss line and warn once per (kind, reason) per process.

    ``detail`` carries small, non-sensitive context (task_id, task_type,
    embedding_source, http status). Never pass query text or vectors.
    """
    key = (kind, reason)
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            "RL telemetry loss: %s (%s) — recorded in %s; further occurrences "
            "in this process are counted there, not logged",
            kind, reason, LOSS_FILE_NAME,
        )
    try:
        path = loss_log_path()
        rec: dict[str, Any] = {
            "ts_ms": int(time.time() * 1000),
            "kind": kind,
            "reason": reason,
            "pid": os.getpid(),
        }
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
        if project_dir:
            rec["project_dir"] = project_dir
        for k, v in detail.items():
            if v is not None and v != "":
                rec[k] = v
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_and_append(path, line)
    except Exception as exc:  # noqa: BLE001 — a loss record never raises
        logger.debug("record_loss: could not append (%s)", exc)


#: Bound on waiting for another PROCESS's rotate+append. Appends take
#: microseconds; past this a writer SPILLS its line to a pending file (see
#: :func:`_spill`) rather than stall a hook.
_LOCK_TIMEOUT_S = 1.0

#: Suffix of a spilled line's file inside :func:`pending_dir`. Anything else in
#: that directory (``atomic_write_text``'s ``*.tmp`` staging file, a
#: ``*.drained`` file whose unlink failed) is never read as a record.
_PENDING_SUFFIX = ".jsonl"
_DRAINED_SUFFIX = ".drained"


def lock_path(path: Optional[Path] = None) -> Path:
    """The sidecar lock that serializes every rewrite of the ledger."""
    p = path or loss_log_path()
    return p.with_name(p.name + ".lock")


def pending_dir(path: Optional[Path] = None) -> Path:
    """``<ledger>.pending.d/`` — lines whose writer could not take the lock."""
    p = path or loss_log_path()
    return p.with_name(p.name + ".pending.d")


def _rotate_and_append(path: Path, line: str) -> None:
    """Rotate (if due) and append under ONE cross-process lock (W5R-15).

    Hooks are separate processes. A per-process ``threading.Lock`` let one
    process rotate (read the tail, ``os.replace``) while another appended to
    the file being replaced, dropping that line at the 2 MiB boundary. The
    sidecar lock is the repo's one file-lock home,
    ``vco_lib.atomic.exclusive_file_lock`` (POSIX flock, Windows
    ``msvcrt.locking``).

    v0.2.104: a writer that cannot take the lock within ``_LOCK_TIMEOUT_S``
    used to append to the ledger WITHOUT it. That is the very race the lock
    exists for: the holder may be between reading the tail and
    ``os.replace``-ing it, so the unlocked line went into the inode being
    replaced and vanished (measured: 16 of 12 800 lines lost over four rounds
    of 16 writers on a loaded machine). Now such a writer never touches the
    ledger: it writes its line to its own pending file, complete on arrival,
    and the next writer that DOES hold the lock moves pending lines into the
    ledger (:func:`_drain_pending`). Readers (:func:`iter_records`) count
    pending lines too, so a line is visible from the moment it is recorded.
    """
    from vco_lib.atomic import LockTimeout, exclusive_file_lock, rotate_tail_lines

    try:
        with exclusive_file_lock(lock_path(path), timeout_s=_LOCK_TIMEOUT_S):
            _drain_pending(path)
            rotate_tail_lines(path, max_bytes=_MAX_BYTES, keep_lines=_KEEP_LINES)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return
    except LockTimeout:
        pass
    _spill(path, line)


def _spill(path: Path, line: str) -> None:
    """Write ``line`` to a fresh, uniquely-named file in :func:`pending_dir`.

    One writer per file, written once through ``atomic_write_text`` (a staged
    ``*.tmp`` then ``os.replace``), so a ``*.jsonl`` file in the directory is
    always complete and never written to again — which is what lets the
    drainer read and remove it without the race the ledger itself has.
    """
    from vco_lib.atomic import atomic_write_text

    name = f"{os.getpid()}-{time.time_ns()}-{os.urandom(4).hex()}{_PENDING_SUFFIX}"
    atomic_write_text(pending_dir(path) / name, line, fsync=False)


def _pending_files(path: Path) -> list[Path]:
    try:
        entries = sorted(pending_dir(path).iterdir())
    except OSError:  # no spill ever happened (the common case), or unreadable
        return []
    return [e for e in entries if e.name.endswith(_PENDING_SUFFIX)]


def _drain_pending(path: Path) -> None:
    """Move spilled lines into the ledger. Caller MUST hold :func:`lock_path`.

    Per file: read it, rename it to ``*.drained`` (from then on no reader
    counts it), append its content, unlink it. A rename that fails (Windows:
    a reader has it open) leaves the file pending for the next drain, never
    appended twice; a failed unlink leaves an inert ``*.drained`` file that a
    later drain removes. Never raises.
    """
    try:
        pdir = pending_dir(path)
        for stale in pdir.glob("*" + _DRAINED_SUFFIX):
            try:
                stale.unlink()
            except OSError:
                pass
        for f in _pending_files(path):
            try:
                body = f.read_text(encoding="utf-8")
                drained = f.with_name(f.name + _DRAINED_SUFFIX)
                f.rename(drained)  # a pure rename of a complete file, not a write
            except OSError:
                continue
            if body and not body.endswith("\n"):
                body += "\n"
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(body)
            except OSError:
                # Ledger unwritable: put the line back so it is not lost.
                try:
                    drained.rename(f)
                except OSError:
                    pass
                return
            try:
                drained.unlink()
            except OSError:
                pass
    except OSError:
        return


def drain_pending_locked(path: Optional[Path] = None) -> None:
    """Public entry for another ledger REWRITER that already holds
    :func:`lock_path` (``rl_client.hub_writer``'s in-place episode bump): fold
    spilled lines in first, so the rewrite sees and keeps them."""
    _drain_pending(path or loss_log_path())


def iter_records(path: Optional[Path] = None) -> Iterator[dict]:
    """Every record of the ledger: its lines, then any still-pending spill.

    Lock-free: a pending file is complete from the moment it exists, and the
    drainer renames it away BEFORE appending its line to the ledger, so a
    concurrent read can at worst miss a line in flight, never count one
    twice. A missing ledger is no records; an unreadable one raises
    ``OSError`` (see :func:`summarize`).
    """
    p = path or loss_log_path()
    sources: list[Path] = [p] if p.exists() else []
    sources += _pending_files(p)
    for i, src in enumerate(sources):
        try:
            fh = open(src, encoding="utf-8")
        except FileNotFoundError:
            continue  # a pending file drained under our feet
        except OSError:
            if i == 0 and src == p:
                raise
            continue
        with fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec


def summarize(since_ms: Optional[int] = None, path: Optional[Path] = None) -> dict:
    """Aggregate the ledger: ``{"total", "by_kind": {kind: {reason: n}}, "last_ts_ms",
    "partial_twins", "partial_missing_nodes"}`` (``total`` / ``by_kind`` count
    lost EVENTS; partial twins are reported apart).

    ``since_ms`` limits the count to lines at or after that epoch-ms. Reads the
    ledger as it stands plus any not-yet-drained spilled line
    (:func:`iter_records`; older lines beyond the rotation tail are gone). A missing file is an
    empty summary; an unreadable one raises ``OSError`` so a caller can report
    UNKNOWN rather than a false "no losses".
    """
    p = path or loss_log_path()
    out: dict[str, Any] = {
        "total": 0,
        "by_kind": {},
        "last_ts_ms": None,
        # W5R-08: partial twins are not lost EVENTS; counted on their own.
        "partial_twins": 0,
        "partial_missing_nodes": 0,
    }
    for rec in iter_records(p):
        ts = rec.get("ts_ms")
        if since_ms is not None and (not isinstance(ts, int) or ts < since_ms):
            continue
        kind = str(rec.get("kind", "unknown"))
        reason = str(rec.get("reason", "unknown"))
        if isinstance(ts, int) and (out["last_ts_ms"] is None or ts > out["last_ts_ms"]):
            out["last_ts_ms"] = ts
        if kind == KIND_DUAL_PARTIAL:
            out["partial_twins"] += 1
            missing = rec.get("missing")
            if isinstance(missing, int):
                out["partial_missing_nodes"] += missing
            continue
        bucket = out["by_kind"].setdefault(kind, {})
        bucket[reason] = bucket.get(reason, 0) + 1
        out["total"] += 1
    return out


def _reset_warned_for_test() -> None:
    _warned.clear()


__all__ = [
    "KIND_DEFERRED_UNSENT",
    "KIND_DUAL_PARTIAL",
    "KIND_DUAL_SKIP",
    "KIND_HUB_POST_FAILED",
    "KIND_WRITER_UNAVAILABLE",
    "KIND_OUTCOME_UNSENT",
    "LOSS_FILE_NAME",
    "drain_pending_locked",
    "iter_records",
    "lock_path",
    "loss_log_path",
    "pending_dir",
    "record_loss",
    "summarize",
]
