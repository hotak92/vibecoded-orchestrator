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
an MCP's stderr. A writer that cannot take the ledger lock in time
puts its line in ``rl_telemetry_loss.jsonl.pending.d/`` instead, and the next
writer that holds the lock moves it into the ledger exactly once (v0.2.104;
see :func:`_rotate_and_append`). The ledger's reader is :func:`summarize`, which ``vco doctor``
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
        # The loss ledger failing to ledger is itself a loss: WARNING, once
        # per process per error type (v0.2.104 R11 F3), never DEBUG.
        _warn_once(
            ("__record_failed__", type(exc).__name__),
            "RL telemetry loss: could not record a %s (%s) line in %s: %s",
            kind, reason, LOSS_FILE_NAME, exc,
        )


#: Bound on waiting for another PROCESS's rotate+append. Appends take
#: microseconds; past this a writer SPILLS its line to a pending file (see
#: :func:`_spill`) rather than stall a hook.
_LOCK_TIMEOUT_S = 1.0

#: Bound on the SECOND lock wait, taken only when the spill itself failed (the
#: pending dir cannot be written). A locked append is the one path that cannot
#: race a rotation, so it is worth waiting longer for before the last resort.
_SPILL_FAILED_LOCK_TIMEOUT_S = 5.0

#: Suffix of a spilled line's file inside :func:`pending_dir`. Anything else in
#: that directory (``atomic_write_text``'s ``*.tmp`` staging file) is never read
#: as a record.
_PENDING_SUFFIX = ".jsonl"

#: The idempotency key a spilled record carries into the ledger.
SPILL_ID_KEY = "spill_id"


def lock_path(path: Optional[Path] = None) -> Path:
    """The sidecar lock that serializes every rewrite of the ledger."""
    p = path or loss_log_path()
    return p.with_name(p.name + ".lock")


def pending_dir(path: Optional[Path] = None) -> Path:
    """``<ledger>.pending.d/`` — lines whose writer could not take the lock.

    Unbounded by design: the owner rule is that RL training logs are ALWAYS
    collected, so no cap or age-out deletes a spilled line. Every writer that
    takes the lock drains the directory (:func:`_drain_pending`), so it only
    grows while the lock stays busy for longer than ``_LOCK_TIMEOUT_S`` at a
    time, and empties at the next uncontended write. ``vco doctor`` reports
    how many files wait there and how old the oldest is
    (:func:`pending_stats`), so a directory that is not emptying is visible.
    """
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
    replaced and vanished. Now such a writer writes its line, tagged with a
    ``spill_id``, to its own pending file (:func:`_spill`); the next writer
    that holds the lock appends it to the ledger unless its ``spill_id`` is
    already there, then unlinks the file (:func:`_drain_pending`). A drain
    interrupted at any step is redone by the next one, and a line is appended
    once. Readers (:func:`iter_records`) count pending lines too, once each.

    If the spill itself fails (the pending dir cannot be written), see
    :func:`_append_after_failed_spill`.
    """
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    try:
        with exclusive_file_lock(lock_path(path), timeout_s=_LOCK_TIMEOUT_S):
            _locked_rotate_and_append(path, line)
        return
    except LockTimeout:
        pass
    try:
        _spill(path, line)
        return
    except (OSError, ValueError) as exc:
        spill_error = exc
    _append_after_failed_spill(path, line, spill_error)


def _locked_rotate_and_append(path: Path, line: str) -> None:
    """Drain, rotate, append. Caller MUST hold :func:`lock_path`."""
    from vco_lib.atomic import rotate_tail_lines

    _drain_pending(path)
    rotate_tail_lines(path, max_bytes=_MAX_BYTES, keep_lines=_KEEP_LINES)
    _append(path, line)


def _append_after_failed_spill(path: Path, line: str, spill_error: BaseException) -> None:
    """The lock was busy AND the pending dir could not be written.

    Order of preference, each step taken only when the one before failed:

    1. Wait up to ``_SPILL_FAILED_LOCK_TIMEOUT_S`` more for the lock and do a
       normal locked append — the one path that cannot race a rotation.
    2. Append WITHOUT the lock. This can lose THIS line if a rotation is in
       flight at that instant (the v0.2.104 race), but it touches no other
       line and no other copy, whereas giving up loses the line every time.
       The F1 lesson — never delete the only copy of a line — is not at stake
       here: there is no other copy to delete.

    Both are logged at WARNING (once per process): the loss ledger failing to
    ledger is itself a loss signal. An ``OSError`` from step 2 propagates to
    :func:`record_loss`, which logs it at WARNING.
    """
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    _warn_once(
        ("__spill_failed__", type(spill_error).__name__),
        "RL telemetry loss ledger: could not spill a line to %s (%s); waiting "
        "up to %.0f s more for the ledger lock",
        pending_dir(path), spill_error, _SPILL_FAILED_LOCK_TIMEOUT_S,
    )
    try:
        with exclusive_file_lock(lock_path(path), timeout_s=_SPILL_FAILED_LOCK_TIMEOUT_S):
            _locked_rotate_and_append(path, line)
        return
    except LockTimeout:
        pass
    _warn_once(
        ("__unlocked_append__",),
        "RL telemetry loss ledger: lock still busy and %s unwritable; "
        "appending without the lock (the line can be lost to a concurrent "
        "rotation)", pending_dir(path),
    )
    _append(path, line)


def _warn_once(key: tuple, msg: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(msg, *args)


def _append(path: Path, text: str) -> None:
    """Append ``text`` to the ledger, first closing a torn last line.

    A previous append cut short (a full disk, an I/O error after a partial
    write) can leave the file without a trailing newline; appending straight
    after it would glue the new record onto the fragment and make BOTH
    unparseable. A newline first keeps the fragment on its own line, which
    every reader skips.
    """
    data = text.encode("utf-8")
    with open(path, "ab+") as fh:
        if fh.seek(0, os.SEEK_END) > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                data = b"\n" + data
        fh.write(data)


def _spill(path: Path, line: str) -> None:
    """Write ``line`` to a fresh, uniquely-named file in :func:`pending_dir`.

    The name starts with the zero-padded wall-clock ``time_ns`` so a name sort
    is a time sort (:func:`_spill_sort_key`); the record gains
    ``"spill_id": <the file's stem>``, its idempotency key in the ledger. One
    writer per file, written once through ``atomic_write_text`` (a staged
    ``*.tmp`` then ``os.replace``), so a ``*.jsonl`` file in the directory is
    always complete and never written to again.
    """
    from vco_lib.atomic import atomic_write_text

    stem = f"{time.time_ns():020d}-{os.getpid()}-{os.urandom(4).hex()}"
    rec = json.loads(line)
    rec[SPILL_ID_KEY] = stem
    body = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
    atomic_write_text(pending_dir(path) / (stem + _PENDING_SUFFIX), body, fsync=False)


def _spill_stem(f: Path) -> str:
    name = f.name
    return name[: -len(_PENDING_SUFFIX)] if name.endswith(_PENDING_SUFFIX) else name


def _spill_sort_key(f: Path) -> tuple[int, str]:
    """Spill time in ns from the file name (``<time_ns:020d>-<pid>-<hex>``),
    then the name; a name of any other shape sorts first with time 0."""
    head = _spill_stem(f).split("-", 1)[0]
    if len(head) == 20 and head.isdigit():
        return int(head), f.name
    return 0, f.name


def _pending_files(path: Path) -> list[Path]:
    """Every pending spill (``*.jsonl``), oldest first."""
    try:
        entries = list(pending_dir(path).iterdir())
    except OSError:  # no spill ever happened (the common case), or unreadable
        return []
    return sorted((e for e in entries if e.name.endswith(_PENDING_SUFFIX)),
                  key=_spill_sort_key)


def _pending_records(f: Path) -> list[tuple[str, Optional[dict], str]]:
    """``(raw_line, record_or_None, spill_id)`` for each line of a pending
    file; ``record_or_None`` is None for a line that is not a JSON object.
    The id is the record's own ``spill_id``, else the file's stem (``_spill``
    names the file after the id it writes). Raises ``OSError`` if unreadable."""
    out: list[tuple[str, Optional[dict], str]] = []
    stem = _spill_stem(f)
    for raw in f.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            out.append((raw, None, stem))
            continue
        sid = rec.get(SPILL_ID_KEY)
        out.append((raw, rec, sid if isinstance(sid, str) and sid else stem))
    return out


class _LedgerIndex:
    """What the ledger already holds, for the drain's dedupe: the
    ``spill_id`` of every parseable line."""

    def __init__(self, lines: list[str]):
        self.ids: set[str] = set()
        for raw in lines:
            self.note(raw)

    def note(self, raw: str) -> None:
        if SPILL_ID_KEY not in raw:
            return
        try:
            rec = json.loads(raw)
        except ValueError:
            return  # a torn line is not a record: its spill is re-appended
        if isinstance(rec, dict):
            sid = rec.get(SPILL_ID_KEY)
            if isinstance(sid, str) and sid:
                self.ids.add(sid)

    def holds(self, sid: str) -> bool:
        return sid in self.ids


def _drain_pending(path: Path) -> None:
    """Move spilled lines into the ledger. Caller MUST hold :func:`lock_path`.

    Per pending file, oldest first: append each of its records whose
    ``spill_id`` the ledger does not already hold, THEN unlink the file. No
    intermediate state, so a drainer killed at any step leaves something the
    next drain finishes correctly: killed before the append, the file is
    still pending and is appended next time (no loss); killed after the
    append and before the unlink, the next drain finds the ``spill_id`` in
    the ledger and only unlinks (no duplicate). An append that raises (a full
    disk) stops the drain with the file still pending; if part of it reached
    the ledger, a whole line is recognised by its id and a torn one is not a
    record, so the retry neither duplicates nor loses it. A pending line
    that is not a JSON object cannot be a record (``_spill`` writes each file
    whole, atomically), so it is dropped with one WARNING per process, as
    :func:`iter_records` skips it, and never copied into the ledger.

    Residual, stated: the index is the ledger as it stands, i.e. at most the
    rotation tail (``_KEEP_LINES``). A pending file whose unlink keeps
    failing while more than that many newer lines are appended would be
    appended a second time once its first copy rotates out.

    Every locked writer runs this, so it is also the pending dir's only GC.
    Never raises.
    """
    files = _pending_files(path)
    if not files:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        return  # cannot dedupe against an unreadable ledger: leave every spill pending
    index = _LedgerIndex(lines)
    for f in files:
        try:
            recs = _pending_records(f)
        except OSError:
            continue  # vanished, or unreadable (Windows: a reader has it open)
        out: list[str] = []
        for raw, rec, sid in recs:
            if rec is None:
                _warn_once(
                    ("pending_unparseable",),
                    "RL telemetry loss ledger: dropping a pending line that is "
                    "not a record (%s): %.80r", f, raw,
                )
                continue
            if index.holds(sid):
                continue
            if not rec.get(SPILL_ID_KEY):
                rec[SPILL_ID_KEY] = sid
            out.append(json.dumps(rec, separators=(",", ":"), default=str))
        if out:
            try:
                _append(path, "\n".join(out) + "\n")
            except OSError:
                return  # ledger unwritable: every remaining spill stays pending
            for raw in out:
                index.note(raw)
        try:
            f.unlink()
        except OSError:
            pass  # inert: the next drain sees its id in the ledger and retries the unlink


def drain_pending_locked(path: Optional[Path] = None) -> None:
    """Public entry for another ledger REWRITER that already holds
    :func:`lock_path` (``rl_client.hub_writer``'s in-place episode bump): fold
    spilled lines in first, so the rewrite sees and keeps them."""
    _drain_pending(path or loss_log_path())


def pending_stats(path: Optional[Path] = None) -> dict:
    """``{"files": n, "oldest_ms": epoch-ms or None}`` for :func:`pending_dir`.

    The age comes from the spill time in the file name. Never raises."""
    return _stats_of(_pending_files(path or loss_log_path()))


def _stats_of(files: list[Path]) -> dict:
    oldest = None
    if files:
        ns = _spill_sort_key(files[0])[0]
        oldest = ns // 1_000_000 if ns else None
    return {"files": len(files), "oldest_ms": oldest}


def iter_records(path: Optional[Path] = None) -> Iterator[dict]:
    """Every record of the ledger: its lines, then any still-pending spill —
    each spilled record ONCE.

    Lock-free. Pending files are read FIRST, the ledger after: the drainer
    appends a spill to the ledger before unlinking its file, so a record is
    either still in its pending file when that is read or already in the
    ledger when the ledger is read — a concurrent drain never hides one. A
    record seen in both (read mid-drain) is yielded once, matched by its
    ``spill_id``. A missing ledger is no records; an unreadable one raises
    ``OSError`` (see :func:`summarize`).
    """
    p = path or loss_log_path()
    return _iter_records(p, _pending_files(p))


def _iter_records(p: Path, files: list[Path]) -> Iterator[dict]:
    """:func:`iter_records` over an already-listed pending dir."""
    pending: list[tuple[str, Optional[dict], str]] = []
    for f in files:
        try:
            pending += _pending_records(f)
        except OSError:
            continue  # drained under our feet: its line is in the ledger now
    seen_ids: set[str] = set()
    try:
        fh = open(p, encoding="utf-8")
    except FileNotFoundError:
        fh = None
    if fh is not None:
        with fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                sid = rec.get(SPILL_ID_KEY)
                if isinstance(sid, str) and sid:
                    seen_ids.add(sid)
                yield rec
    for _raw, rec, sid in pending:
        if rec is None or sid in seen_ids:
            continue
        seen_ids.add(sid)
        yield rec


def summarize(since_ms: Optional[int] = None, path: Optional[Path] = None) -> dict:
    """Aggregate the ledger: ``{"total", "by_kind": {kind: {reason: n}}, "last_ts_ms",
    "partial_twins", "partial_missing_nodes", "pending_files",
    "pending_oldest_ms"}`` (``total`` / ``by_kind`` count lost EVENTS; partial
    twins are reported apart; the two ``pending_*`` keys describe
    :func:`pending_dir` regardless of ``since_ms`` — spilled lines not yet
    drained, already counted in the totals).

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
    files = _pending_files(p)  # listed once for both the stats and the read
    stats = _stats_of(files)
    out["pending_files"] = stats["files"]
    out["pending_oldest_ms"] = stats["oldest_ms"]
    for rec in _iter_records(p, files):
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
    "SPILL_ID_KEY",
    "drain_pending_locked",
    "iter_records",
    "lock_path",
    "loss_log_path",
    "pending_dir",
    "pending_stats",
    "record_loss",
    "summarize",
]
