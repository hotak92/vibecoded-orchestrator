# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The one wire format for what a ``sync_knowledge_graph.py --prune-missing``
run did to a vanished node's KG rows (v0.2.103 R33 N1).

The post-commit KG reviewer's guard (``vco_lib.kg_reviewer_guard.resync_with``)
deletes a NEW node it reverts and asks the project's sync script to remove the
rows the edit-time sync already embedded for it. The guard used to log
"removed from the KG collection" whenever the script exited 0 — but on the
orchestrator root (KG collection == shared collection) the script kept every
row and still exited 0, so the log claimed a deletion that never happened.

Now the script prints ONE machine-readable line per pruned target
(:func:`format_line`) and the guard logs exactly what that line says
(:func:`parse_lines`); no line means "not confirmed", never "removed".

Statuses:

* ``deleted`` — ``deleted`` rows removed (``kept`` rows at the same path were
  left because their ``content_hash`` differs — root arm only);
* ``nothing_matched`` — no row matched, nothing removed;
* ``skipped`` — deliberately not removed (``reason`` says why);
* ``failed`` — the removal failed (``reason`` carries the error).
"""

from __future__ import annotations

import json
from typing import Dict, Optional

#: Prefix of the result line; the rest of the line is one JSON object.
PREFIX = "KG_PRUNE_RESULT "

STATUS_DELETED = "deleted"
STATUS_NOTHING_MATCHED = "nothing_matched"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUSES = (STATUS_DELETED, STATUS_NOTHING_MATCHED, STATUS_SKIPPED,
            STATUS_FAILED)


def format_line(path: str, status: str, *, deleted: int = 0, kept: int = 0,
                reason: str = "") -> str:
    """The result line for one pruned target (``path`` is its project-relative
    POSIX file_path)."""
    if status not in STATUSES:
        raise ValueError(f"unknown prune status {status!r}")
    return PREFIX + json.dumps(
        {"path": path, "status": status, "deleted": int(deleted),
         "kept": int(kept), "reason": reason},
        ensure_ascii=False, sort_keys=True)


def parse_lines(stdout: Optional[str]) -> Dict[str, dict]:
    """Every well-formed result line in ``stdout``, keyed by path (a later
    line for the same path wins). Malformed lines are ignored — the caller
    then reports that path as unconfirmed."""
    out: Dict[str, dict] = {}
    for line in (stdout or "").splitlines():
        if not line.startswith(PREFIX):
            continue
        try:
            rec = json.loads(line[len(PREFIX):])
        except ValueError:
            continue
        if (isinstance(rec, dict) and isinstance(rec.get("path"), str)
                and rec.get("status") in STATUSES):
            out[rec["path"]] = rec
    return out


def describe(rec: Optional[dict], collection_hint: str = "the KG collection"
             ) -> str:
    """A log phrase that states exactly what the result record says."""
    if rec is None:
        return ("removal NOT confirmed (the sync script reported no result "
                "for it)")
    status = rec.get("status")
    deleted = int(rec.get("deleted") or 0)
    kept = int(rec.get("kept") or 0)
    reason = str(rec.get("reason") or "")
    if status == STATUS_DELETED:
        tail = (f"; {kept} row(s) with other content at the same path kept"
                if kept else "")
        return f"removed {deleted} row(s) from {collection_hint}{tail}"
    if status == STATUS_NOTHING_MATCHED:
        tail = (f" ({kept} row(s) with other content at the same path kept)"
                if kept else "")
        return f"no rows matched in {collection_hint}, nothing removed{tail}"
    if status == STATUS_SKIPPED:
        return f"rows NOT removed: {reason or 'skipped'}"
    return f"row removal FAILED: {reason or 'unknown error'}"
