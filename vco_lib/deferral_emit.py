# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE emitter home for UPDATE_DEFERRED read-modify-write cycles (v0.2.83).

``UPDATE_DEFERRED.{md,json}`` has many writer families: ``install.py`` (via
:class:`vco_lib.install_deferral_flow.InstallDeferralFlow`), ``project_init``,
several Rust emitters, and detached background children (the P7 code-graph
resync driver, the embedding-failure path). Before v0.2.83 every non-install
writer hand-rolled the same triplet::

    from vco_lib.deferral_report import DeferralEntry, DeferralReport
    report = DeferralReport.read(folder)
    report.add_entry(DeferralEntry(...))     # or report.mark_resolved(cid)
    report.write(folder)

Two problems the user directed us to fix (INVESTIGATION Track B, B-F1):

1. **One-home modularity** — the triplet was duplicated across
   ``hard_cut``, ``embedding_service``, ``codegraph_resync``, and ~20 sites
   in ``project_init``. A change to the read/write contract had to be made in
   every copy.

2. **Concurrent-writer atomicity** — ``DeferralReport`` has NO locking. A
   detached child that read-modify-writes the file while ``install.py``'s
   ``finalize()`` does its own late-merge + single write can interleave and
   drop entries (the real race; the "sequential clobber" claim was wrong —
   see the investigation's correction #3). The fix is one exclusive file
   lock, held around the read → mutate → write, shared by EVERY writer.

This module is that shared home. :func:`locked_report` acquires the process-
shared file lock on ``<folder>/.claude/context/.update-deferred.lock`` (via
:func:`vco_lib.atomic.exclusive_file_lock` — real ``flock`` on POSIX,
best-effort no-lock on Windows), reads the current report, yields it for
mutation, and writes it back BEFORE releasing the lock. Callers must keep
their locked-report use FLAT — never enter :func:`locked_report` while already
inside one (the lock is not re-entrant across the same file; a nested acquire
would block forever on POSIX).

``vco_lib`` is part of every healthy install, so imports here are module-top
and LOUD-FAIL (an ``ImportError`` means a broken install — it must reach the
user, never a silent inline-copy degrade). The only local imports are inside
``record_auto_resolution`` for the stdlib logging bits, which are always
present.

B-F9: every auto-resolution (a VCO update deciding a deferred condition is now
provably safe to fix) MUST leave a visible trail —
:func:`record_auto_resolution` writes a loud log line AND appends a parseable
JSONL row to ``<folder>/.claude/logs/auto-resolutions.jsonl`` (``.claude/logs``
is git-ignored / user-owned — safe to write). No silent mutations.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from vco_lib.atomic import exclusive_file_lock
from vco_lib.deferral_report import DeferralEntry, DeferralReport

logger = logging.getLogger(__name__)

#: The shared lock token for the deferral read-modify-write cycle, relative to
#: a managed project folder. Sits beside ``UPDATE_DEFERRED.{md,json}`` under
#: ``.claude/context/`` — which is git-ignored (matching UPDATE_DEFERRED.md's
#: posture), so the lockfile never enters version control.
LOCK_REL = Path(".claude") / "context" / ".update-deferred.lock"

#: Where auto-resolution records land, relative to a managed project folder.
#: ``.claude/logs`` is in the USER_OWNED_PATHS set and git-ignored, so the
#: JSONL trail is safe to write and survives bundle updates.
_AUTO_RESOLUTIONS_REL = Path(".claude") / "logs" / "auto-resolutions.jsonl"


def _log(log: Any, level: str, msg: str) -> None:
    """Emit ``msg`` at ``level`` via the caller's logger (if given) else ours.

    ``log`` may be a stdlib ``logging.Logger`` (has the level methods) — the
    common case. Anything falsy routes to this module's logger. A logging
    failure is swallowed: a deferral write must never break on a log line.
    """
    target = log if log is not None else logger
    try:
        getattr(target, level, target.info)(msg)
    except Exception:  # noqa: BLE001 — logging must never raise into the caller
        try:
            logger.info(msg)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def locked_report(folder: Path) -> Iterator[DeferralReport]:
    """Read → yield → write the deferral report under the shared file lock.

    The whole cycle runs inside the exclusive lock on
    ``<folder>/.claude/context/.update-deferred.lock`` so a concurrent writer
    (detached child vs install run) cannot interleave a read/write pair and
    drop entries. The report is READ after the lock is held (so it reflects
    any prior writer's committed state) and WRITTEN before the lock releases.

    Usage::

        with locked_report(folder) as report:
            report.add_entry(DeferralEntry(...))
            # or report.mark_resolved("some_cid")
        # write + lock-release happen here, automatically.

    The report's :meth:`DeferralReport.write` decides file presence: entries
    present ⇒ ``UPDATE_DEFERRED.{md,json}`` written + CLAUDE.md reminder
    injected; no entries ⇒ both files deleted + reminder stripped.

    NOT re-entrant: never call :func:`locked_report` (or any function here
    that uses it — :func:`emit`, :func:`emit_entries`,
    :func:`resolve_conditions`) from inside another ``locked_report`` block on
    the SAME folder; on POSIX the nested ``flock(LOCK_EX)`` would deadlock the
    process against itself. Keep call-sites flat.

    Args:
        folder: The managed project folder (contains ``.claude/``).

    Yields:
        A :class:`DeferralReport` seeded from the on-disk state, ready to
        mutate. The write is performed on exit.
    """
    folder = Path(folder)
    with exclusive_file_lock(folder / LOCK_REL):
        report = DeferralReport.read(folder)
        yield report
        report.write(folder)


def emit_entries(
    folder: Path,
    entries: Sequence[DeferralEntry],
    *,
    log: Any = None,
) -> bool:
    """Add ``entries`` to the on-disk report under the shared lock.

    Pre-existing FOREIGN entries (any condition the caller did not just add)
    are PRESERVED — this reads the current report and only adds/overwrites the
    given entries' condition IDs (``DeferralReport.add_entry`` is last-write-
    wins per condition_id). Batch-safe: all entries land in one locked cycle
    (one read, one write).

    Args:
        folder: The managed project folder.
        entries: The entries to add. Empty ⇒ a no-op read/write (the report is
            rewritten unchanged; still returns whether it holds any entries).
        log: Optional logger for the soft-fail path.

    Returns:
        ``True`` when the report holds at least one entry after the write
        (i.e. the file was written), ``False`` when it is now empty (files
        deleted). Mirrors :meth:`DeferralReport.write`'s return.
    """
    try:
        with locked_report(folder) as report:
            for entry in entries:
                report.add_entry(entry)
            wrote = bool(report)
        return wrote
    except Exception as exc:  # noqa: BLE001 — deferral I/O is best-effort
        cids = ", ".join(e.condition_id for e in entries) or "(none)"
        _log(log, "warning", f"[vct] deferral emit_entries failed ({cids}): {exc}")
        return False


def emit(folder: Path, entry: DeferralEntry, *, log: Any = None) -> bool:
    """Single-entry sugar over :func:`emit_entries`."""
    return emit_entries(folder, (entry,), log=log)


def resolve_conditions(
    folder: Path,
    condition_ids: Sequence[str],
    *,
    log: Any = None,
) -> int:
    """Mark ``condition_ids`` resolved on the on-disk report under the lock.

    Each ID is passed to :meth:`DeferralReport.mark_resolved` (drops the entry
    if present AND tombstones the ID for this instance's run so a within-cycle
    merge cannot resurrect it). The single write at the end of the locked cycle
    deletes ``UPDATE_DEFERRED.{md,json}`` when no entries remain.

    Args:
        folder: The managed project folder.
        condition_ids: The condition IDs to resolve. Resolving an ID that is
            not present is a safe no-op (counts 0).
        log: Optional logger for the soft-fail path.

    Returns:
        The number of condition IDs that were actually PRESENT (and therefore
        removed) — computed before mutation. IDs absent from the report count
        0. Returns 0 on any I/O error (soft-fail).
    """
    try:
        removed = 0
        with locked_report(folder) as report:
            for cid in condition_ids:
                if report.has_condition(cid):
                    removed += 1
                report.mark_resolved(cid)
        return removed
    except Exception as exc:  # noqa: BLE001 — deferral I/O is best-effort
        ids = ", ".join(condition_ids) or "(none)"
        _log(log, "warning", f"[vct] deferral resolve_conditions failed ({ids}): {exc}")
        return 0


def record_auto_resolution(
    folder: Path,
    condition_id: str,
    action: str,
    detail: str,
    *,
    log: Any = None,
) -> None:
    """Record that a deferred condition was AUTO-RESOLVED by a VCO update.

    B-F9 (no silent mutations): whenever an update-flow automation decides a
    previously-deferred condition is now provably safe to fix on the user's
    behalf, it MUST call this so the decision leaves a visible trail. Two
    surfaces:

    1. A loud log line — ``[vct] auto-resolved: <condition_id> — <action>:
       <detail>`` — at INFO on the caller's logger (or this module's).
    2. A parseable JSONL row appended to
       ``<folder>/.claude/logs/auto-resolutions.jsonl`` (``.claude/logs`` is
       git-ignored / user-owned — safe to write). Row shape::

           {"ts": "<ISO-8601 UTC>", "condition_id": "...",
            "action": "...", "detail": "..."}

    Best-effort throughout: this is observability, never a gate — a failure to
    write the JSONL (unwritable dir, disk full) is logged and swallowed so it
    can never crash the automation that already did the real work.

    Args:
        folder: The managed project folder the resolution applies to.
        condition_id: The deferral condition that was auto-resolved.
        action: A short verb phrase for WHAT was done (e.g.
            ``"re-mirrored legacy compose override"``, ``"removed searxng dir"``).
        detail: Any extra context (paths, counts, reasons).
        log: Optional logger for the loud line + soft-fail path.
    """
    line = f"[vct] auto-resolved: {condition_id} — {action}: {detail}"
    _log(log, "info", line)

    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "condition_id": condition_id,
        "action": action,
        "detail": detail,
    }
    try:
        target = Path(folder) / _AUTO_RESOLUTIONS_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — the JSONL trail is best-effort
        _log(
            log,
            "warning",
            f"[vct] could not append auto-resolution record for {condition_id}: {exc}",
        )


# DeferralEntry is re-exported so a migrated call-site can build entries and
# emit them with a single ``from vco_lib.deferral_emit import ...`` import.
__all__ = [
    "LOCK_REL",
    "DeferralEntry",
    "emit",
    "emit_entries",
    "locked_report",
    "record_auto_resolution",
    "resolve_conditions",
]
