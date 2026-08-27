# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R1 (v0.2.91) — reader for the ``rl_events`` retention ARCHIVE sidecars.

Why this module exists
----------------------
The 90-day retention prune used to be a bare ``DELETE FROM rl_events`` with no
export path: the oldest slice of the RL training corpus was destroyed one hourly
pass at a time, and every destroyed row carried a query embedding, per-node
embeddings, ``linked_embs`` / ``answer_chunk_embs`` and a cosine-derived label
that cannot be recomputed from anything else. v0.2.91 made the prune
**archive-then-delete**: victim rows are written to a compressed sidecar, fsynced
and verified, BEFORE the first DELETE runs (the deletion authority is
``Db::prune_rl_events`` in ``launcher/src-tauri/vct-launcher-core/src/db/
rl_events.rs``; a failed archive aborts the prune).

An archive nothing can read is a delete with extra steps. This module is the
consumer that ships with the signal.

Format contract (MUST MATCH ``db/rl_events.rs``)
------------------------------------------------
* One file per prune pass, named
  ``rl_events-<UTC ts>-<scope>-<lo id>-<hi id>-<N>rows.jsonl.gz``.
* gzip-compressed JSONL; each line is ONE hub ROW object, field-for-field
  identical to ``vct-hub``'s ``RlEventOut`` — the exact JSON the offline trainer
  already consumes from ``GET /api/v1/rl/events``. So a row read back from an
  archive can be handed straight to
  ``paid-modules/vct-rl-reranker/hub_event_loader.py::_reshape_hub_row`` with no
  new parser and no field mapping:

      id, event_type, schema_version, ts_ms, project_id, project_name,
      task_id, task_type, embedding_source, embedding_dim, embedding_model,
      payload_json, quarantined_at, quarantine_reason

* ``payload_json`` is the VERBATIM writer-side string (not a re-encoded object),
  so every embedding survives the round-trip byte-for-byte.
* Retrieval↔citation pairs are never split across the archive boundary: the
  prune completes its victim set by ``task_id`` group, so both halves of a pair
  land in the SAME file.
* ``*.jsonl.gz.pending`` files are IN-FLIGHT archives from a prune that did not
  finish. Their rows are still in ``launcher.db`` — reading them would
  double-count those events, so :func:`iter_archived_rows` SKIPS them (and
  :func:`archive_report` reports how many it skipped).
* QUARANTINED rows (``quarantined_at`` set) ARE archived — the prune never
  drops a row it cannot reproduce — but they are NOT served by default, exactly
  as the live ``GET /api/v1/rl/events`` excludes them (RL-14). Opt in with
  ``include_quarantined=True`` / ``--include-quarantined`` for inspection.

Usage
-----
Library::

    from vco_lib.rl_archive import iter_archived_rows
    rows = list(iter_archived_rows(project_id="02fbc934-..."))

CLI (for the trainer container / any non-Python consumer)::

    python -m vco_lib.rl_archive --list
    python -m vco_lib.rl_archive --project-id 02fbc934-... > archived.jsonl
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

# MUST MATCH db/rl_events.rs: RL_ARCHIVE_DIR_ENV / RL_ARCHIVE_SUFFIX /
# RL_ARCHIVE_PENDING_SUFFIX. A drift here silently makes archives unreadable.
ARCHIVE_DIR_ENV = "RL_EVENTS_ARCHIVE_DIR"
ARCHIVE_SUFFIX = ".jsonl.gz"
ARCHIVE_PENDING_SUFFIX = ".jsonl.gz.pending"

__all__ = [
    "ARCHIVE_DIR_ENV",
    "ARCHIVE_SUFFIX",
    "ARCHIVE_PENDING_SUFFIX",
    "archive_dir",
    "archive_files",
    "iter_archived_rows",
    "archive_report",
]


def archive_dir() -> Path:
    """Resolve the archive directory.

    ``$RL_EVENTS_ARCHIVE_DIR`` wins; otherwise ``<vct root>/rl_archive`` — the
    sibling of ``launcher.db``. MUST MATCH ``rl_events.rs::rl_archive_dir``.
    """
    override = os.environ.get(ARCHIVE_DIR_ENV, "").strip()
    if override:
        return Path(override)
    # vco_lib.paths is the ONE Python home for the state root (mirrors the Rust
    # crate::paths::vct_root_dir). Loud-fail is not warranted here — a missing
    # vco_lib.paths would mean a broken install, and the import error surfaces
    # naturally to the caller rather than being swallowed.
    from vco_lib.paths import vct_root_dir

    return Path(vct_root_dir()) / "rl_archive"


def archive_files(directory: Optional[Path] = None) -> List[Path]:
    """Published (complete) archive sidecars, oldest-name-first.

    ``.pending`` files are EXCLUDED — see the module docstring. Sorting by name
    is chronological because the stem starts with a sortable UTC timestamp.
    """
    d = Path(directory) if directory is not None else archive_dir()
    if not d.is_dir():
        return []
    out = [
        p
        for p in d.iterdir()
        if p.is_file()
        and p.name.endswith(ARCHIVE_SUFFIX)
        and not p.name.endswith(ARCHIVE_PENDING_SUFFIX)
    ]
    return sorted(out, key=lambda p: p.name)


def _iter_file_rows(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield the row dicts in ONE archive file.

    A malformed line is SKIPPED (parity with the JSONL/hub loaders, which skip
    an unparseable row rather than failing the whole load); a file that cannot
    be opened or decompressed raises, because that is a real integrity problem
    the operator must see.
    """
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict):
                yield row


def iter_archived_rows(
    directory: Optional[Path] = None,
    *,
    project_id: Optional[str] = None,
    event_type: Optional[str] = None,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    files: Optional[Iterable[Path]] = None,
    include_quarantined: bool = False,
) -> Iterator[Dict[str, Any]]:
    """Yield archived hub rows, oldest file first, in the loader's row shape.

    The filters — INCLUDING the quarantine default — match the live trainer
    read (``GET /api/v1/rl/events`` → ``rl_events_api.rs::list_events``, whose
    ``include_quarantined`` defaults to ``false``, → ``Db::list_rl_events``),
    so an archive read is a drop-in extension of a live corpus read:

        from vco_lib.rl_archive import iter_archived_rows
        from hub_event_loader import _reshape_hub_row
        events = [_reshape_hub_row(r) for r in iter_archived_rows(project_id=pid)]

    QUARANTINE (RL-14, v0.2.75): rows marked ``quarantined_at`` are poisoned
    training data (``score_out_of_range`` and friends) and are excluded from
    the trainer's GET by default. ARCHIVING them is correct — the prune must
    never drop a row it cannot reproduce — but SERVING them is not, and the
    oldest slice (what gets archived FIRST) is exactly where the poisoned era
    lives. Without this filter the recipe above re-ingested them verbatim
    (v0.2.91 wave-4 MAJOR-4). ``_reshape_hub_row`` does not check the field
    either, so the gate has to be here.

    Args:
        directory: archive dir (default: :func:`archive_dir`).
        project_id: cohort key — the loader filters on this, so an archive read
            for training MUST pass it (a ``None`` project_id row is fixture
            residue the live loader can never select either).
        event_type: ``"retrieval"`` / ``"citation"`` / …; None = all.
        since_ms / until_ms: inclusive ``ts_ms`` window bounds.
        files: explicit file list (default: every published sidecar).
        include_quarantined: ``True`` yields quarantined rows too — for
            INSPECTION surfaces (rl-doctor, forensics), mirroring the GET's own
            opt-in. Never pass it from a training loader.
    """
    paths = list(files) if files is not None else archive_files(directory)
    for path in paths:
        for row in _iter_file_rows(path):
            if not include_quarantined and row.get("quarantined_at") is not None:
                continue
            if project_id is not None and row.get("project_id") != project_id:
                continue
            if event_type is not None and row.get("event_type") != event_type:
                continue
            ts = row.get("ts_ms")
            if since_ms is not None and (not isinstance(ts, int) or ts < since_ms):
                continue
            if until_ms is not None and (not isinstance(ts, int) or ts > until_ms):
                continue
            yield row


def archive_report(directory: Optional[Path] = None) -> Dict[str, Any]:
    """Summarize the archive: file/row counts, ts span, per-project totals.

    Also reports ``pending_files`` — in-flight sidecars deliberately skipped —
    and ``quarantined_rows``, the subset of ``rows`` that
    :func:`iter_archived_rows` skips by default (see its QUARANTINE note). The
    row counts here are the RAW totals on disk, so the two numbers together
    tell an operator both "what was preserved" and "what a training read sees".
    JSON-safe; carries no payload content (only counts + ids).
    """
    d = Path(directory) if directory is not None else archive_dir()
    published = archive_files(d)
    pending = (
        [p.name for p in sorted(d.iterdir()) if p.name.endswith(ARCHIVE_PENDING_SUFFIX)]
        if d.is_dir()
        else []
    )
    rows = 0
    quarantined = 0
    per_project: Dict[str, int] = {}
    per_type: Dict[str, int] = {}
    oldest: Optional[int] = None
    newest: Optional[int] = None
    for path in published:
        for row in _iter_file_rows(path):
            rows += 1
            if row.get("quarantined_at") is not None:
                quarantined += 1
            pid = row.get("project_id") or "<null>"
            per_project[pid] = per_project.get(pid, 0) + 1
            et = row.get("event_type") or "<none>"
            per_type[et] = per_type.get(et, 0) + 1
            ts = row.get("ts_ms")
            if isinstance(ts, int):
                oldest = ts if oldest is None or ts < oldest else oldest
                newest = ts if newest is None or ts > newest else newest
    return {
        "dir": str(d),
        "files": len(published),
        "rows": rows,
        # The subset of `rows` that iter_archived_rows() skips by default.
        "quarantined_rows": quarantined,
        "pending_files": pending,
        "oldest_ts_ms": oldest,
        "newest_ts_ms": newest,
        "rows_by_project": per_project,
        "rows_by_event_type": per_type,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: ``python -m vco_lib.rl_archive [--list] [--project-id ID] ...``

    Default (no ``--list``) streams matching rows as JSONL on stdout, so a
    consumer in any language can pipe the archive into its own loader.
    """
    ap = argparse.ArgumentParser(
        prog="python -m vco_lib.rl_archive",
        description="Read rl_events retention archive sidecars (gzip JSONL, hub-row shape).",
    )
    ap.add_argument("--dir", default=None, help="archive dir (default: $RL_EVENTS_ARCHIVE_DIR or <vct root>/rl_archive)")
    ap.add_argument("--list", action="store_true", help="print a JSON summary instead of the rows")
    ap.add_argument("--project-id", default=None, help="cohort filter (the loader's project_id)")
    ap.add_argument("--event-type", default=None, help="retrieval | citation | ...")
    ap.add_argument("--since-ms", type=int, default=None)
    ap.add_argument("--until-ms", type=int, default=None)
    ap.add_argument(
        "--include-quarantined",
        action="store_true",
        help=(
            "also emit rows marked `quarantined_at` (poisoned training data, "
            "excluded by default exactly as the hub's GET /rl/events excludes "
            "them). For inspection only — never for a training load."
        ),
    )
    args = ap.parse_args(argv)

    directory = Path(args.dir) if args.dir else None
    if args.list:
        json.dump(archive_report(directory), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    for row in iter_archived_rows(
        directory,
        project_id=args.project_id,
        event_type=args.event_type,
        since_ms=args.since_ms,
        until_ms=args.until_ms,
        include_quarantined=args.include_quarantined,
    ):
        sys.stdout.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
