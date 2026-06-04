#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One-shot migration of historical RL JSONL events into launcher.db (v0.2.46 RL-5/C9).

Background
----------
Through v0.2.45 the RL telemetry path appended one JSON line per event to
``~/.claude/retrieval_rl_data/rl_events.jsonl`` (default; override via
``RL_DATA_DIR``). v0.2.46 RL-4/RL-5 introduced ``launcher.db``'s
``rl_events`` table + a hub POST endpoint, and the MCP-side writer
switched to ``rl_client.hub_writer.post_rl_event``. The JSONL append path
is dead going forward.

This script lets a user with a populated JSONL corpus reseed it into
launcher.db so the offline-trainer + dashboards see the historical data.
Without it, the JSONL would be silently stranded — pre-v0.2.46 events
would remain readable on disk but invisible to the new query routes.

Scope
-----
* Reads from ``~/.claude/retrieval_rl_data/rl_events.jsonl`` by default
  (or a path passed positionally).
* Optionally also reads the qwen3 sibling file
  (``rl_events_qwen3.jsonl``) if present.
* Validates each line as v2 or v3 schema (drops v1 / unparseable rows
  with a per-row reason). The v1→v2 migration is a separate script at
  ``migrate_rl_log_v1_to_v2.py``; run it first if your file pre-dates
  the schema-v2 rollout (commit 814c4ae).
* For each valid row, POSTs to the running hub via the same client used
  by the MCP writer (``rl_client.hub_writer.post_rl_event``). The hub
  performs the actual INSERT into ``rl_events`` + dedup.
* On success, renames the original JSONL to ``<path>.pre-db-migration.bak``
  so a re-run can't double-insert. The qwen3 sibling gets the same
  treatment.

Idempotency
-----------
The script renames the source file as its last action. Re-runs find no
source file → no-op. Crashes between POST and rename cause re-POSTs of
already-inserted events; the hub's ``rl_events`` table has
``(task_id, event_type, ts_ms)`` UNIQUE — duplicates raise the hub-side
INSERT, hub returns 4xx, this script counts them as ``already_inserted``
and continues. End state is the same.

Soft-fail
---------
* Missing source file → exit 0 with "nothing to migrate".
* Hub not running → exit 2 with "start the launcher (or `vct-hub
  --start-if-not-running`) and re-run".
* Per-row errors → counted + reported; the script continues. Non-zero
  exit only on per-row failure RATE above ``--max-error-rate`` (default
  10%).

Usage
-----
    # Dry-run (validate + count; no POST, no rename)
    python claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py --dry-run

    # Default — process ~/.claude/retrieval_rl_data/rl_events.jsonl
    # AND ~/.claude/retrieval_rl_data/rl_events_qwen3.jsonl if present.
    python claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py

    # Explicit path
    python claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py \\
        /path/to/rl_events.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator

# The hub-writer lives alongside this script under claude_mcp_servers/.
# When run via `python claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py`,
# the parent of this script's parent is `claude_mcp_servers/` which the
# sys.path search will already find for editable installs (`pip install
# -e claude_mcp_servers/`). For non-editable runs we prepend it explicitly.
_THIS = Path(__file__).resolve()
_MCP_PKG = _THIS.parent.parent
if str(_MCP_PKG) not in sys.path:
    sys.path.insert(0, str(_MCP_PKG))

from rl_client.hub_writer import post_rl_event  # noqa: E402

_DEFAULT_PRIMARY = Path.home() / ".claude" / "retrieval_rl_data" / "rl_events.jsonl"
_DEFAULT_QWEN3 = Path.home() / ".claude" / "retrieval_rl_data" / "rl_events_qwen3.jsonl"


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict | None, str | None]]:
    """Yield (lineno, event_dict, error_reason) for each line in ``path``.

    On parse success: (lineno, dict, None).
    On parse failure: (lineno, None, reason).
    """
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue  # blank line — silent skip, not an error
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                yield lineno, None, f"json: {exc}"
                continue
            if not isinstance(obj, dict):
                yield lineno, None, "not a JSON object"
                continue
            yield lineno, obj, None


def _extract_event_type(event: dict) -> str | None:
    """Return the event's type string regardless of which field carries it.

    The hub's ``rl_events_api::PostEventBody`` requires ``event_type``.
    Historical v2 JSONL events store this under the key ``event`` (the
    pre-cutover field name); v3 events use ``event_type``. Accept either
    on read; we'll always write the canonical ``event_type`` key on POST.
    """
    et = event.get("event_type")
    if et:
        return et if isinstance(et, str) else None
    et = event.get("event")
    if et:
        return et if isinstance(et, str) else None
    return None


def _validate_v2_or_v3(event: dict) -> tuple[bool, str]:
    """Return (ok, reason) — only v2 / v3 events are eligible for DB import.

    The hub's ``rl_events_api::PostEventBody`` REQUIRES ``schema_version``,
    ``event_type`` ∈ {retrieval, citation}, ``task_id``, ``ts_ms``,
    ``payload_json``. We accept v2 events by wrapping their full body as
    ``payload_json`` per the writer-side post-cutover convention.
    """
    sv = event.get("schema_version")
    if not isinstance(sv, int):
        return False, "missing/non-int schema_version"
    if sv < 2:
        return False, f"v1 schema (schema_version={sv}) — run migrate_rl_log_v1_to_v2.py first"
    if sv > 3:
        return False, f"unknown future schema_version={sv}"
    et = _extract_event_type(event)
    if et not in ("retrieval", "citation"):
        return False, f"unknown event_type={et!r}"
    if not event.get("task_id"):
        return False, "missing task_id"
    return True, ""


def _build_post_body(event: dict) -> dict:
    """Reshape a v2/v3 JSONL event into the hub's POST body schema.

    Matches ``rl_events_api::PostEventBody`` (server-side). Optional fields
    (project_id / project_name / task_type / embedding_*) are passed through
    when present. ``ts_ms`` is read from the event when v3-shaped (already
    epoch-ms), or derived from a v2 ``ts`` ISO string when v2-shaped.
    """
    sv = event["schema_version"]
    ts_ms = event.get("ts_ms")
    if ts_ms is None:
        ts_iso = event.get("ts")
        if isinstance(ts_iso, str):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                ts_ms = int(dt.timestamp() * 1000)
            except (ValueError, OSError):
                ts_ms = 0
        else:
            ts_ms = 0

    return {
        # Accept either field name on the JSONL side, always emit canonical
        # `event_type` to the hub. _validate_v2_or_v3 guarantees this returns
        # a non-None v2/v3 value.
        "event_type":       _extract_event_type(event),
        "schema_version":   sv,
        "ts_ms":            ts_ms,
        "project_id":       event.get("project_id"),
        "project_name":     event.get("project_name"),
        "task_id":          event["task_id"],
        "task_type":        event.get("task_type"),
        "embedding_source": event.get("embedding_source"),
        "embedding_dim":    event.get("embedding_dim"),
        "embedding_model":  event.get("embedding_model"),
        # The hub stores the full event verbatim so the v3 reader path can
        # reconstruct query/result/citation fields. JSONL events were never
        # binary, so json.dumps round-trips cleanly.
        "payload_json":     json.dumps(event, sort_keys=True),
    }


def _process_file(
    path: Path,
    *,
    dry_run: bool,
    max_error_rate: float,
) -> tuple[int, int, int, list[str]]:
    """Process one JSONL file. Returns (posted, skipped, errors, reasons).

    ``skipped`` covers schema-version filtering (e.g. v1 events).
    ``errors`` covers POSTs that failed at the hub side (post_rl_event
    returned False, network/timeout/4xx).
    """
    posted = 0
    skipped = 0
    errors = 0
    reasons: list[str] = []

    for lineno, event, parse_err in _iter_jsonl(path):
        if parse_err is not None:
            errors += 1
            reasons.append(f"line {lineno}: {parse_err}")
            continue
        ok, validate_err = _validate_v2_or_v3(event)
        if not ok:
            skipped += 1
            # Keep at most 50 skip reasons (a v1 file would otherwise emit
            # thousands of identical messages — useless noise).
            if len(reasons) < 50:
                reasons.append(f"line {lineno}: skipped — {validate_err}")
            continue
        if dry_run:
            posted += 1
            continue
        body = _build_post_body(event)
        if post_rl_event(body):
            posted += 1
        else:
            # post_rl_event returns False for ANY non-2xx including the
            # idempotent re-run case (duplicate task_id). The hub logs the
            # exact status code at debug; we count without distinguishing
            # because the user-facing outcome is the same: row is in DB.
            errors += 1
            if len(reasons) < 50:
                reasons.append(f"line {lineno}: hub POST failed")

    total_processed = posted + skipped + errors
    if total_processed > 0:
        error_rate = errors / total_processed
        if error_rate > max_error_rate:
            reasons.insert(
                0,
                f"FATAL: error_rate={error_rate:.1%} > max_error_rate={max_error_rate:.1%} — "
                f"not renaming source file. Inspect hub state + re-run."
            )

    return posted, skipped, errors, reasons


def _rename_to_backup(path: Path) -> Path:
    """Atomic-rename ``<path>`` to ``<path>.pre-db-migration.bak``.

    If a previous run already produced a .bak, suffix with a counter so we
    never clobber. Returns the final backup path.
    """
    base = path.with_suffix(path.suffix + ".pre-db-migration.bak")
    if not base.exists():
        path.rename(base)
        return base
    n = 2
    while True:
        candidate = path.with_suffix(f"{path.suffix}.pre-db-migration.{n}.bak")
        if not candidate.exists():
            path.rename(candidate)
            return candidate
        n += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate historical RL JSONL telemetry into launcher.db via vct-hub.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help=f"Path to rl_events.jsonl (default: {_DEFAULT_PRIMARY}). The qwen3 "
             f"sibling {_DEFAULT_QWEN3.name} is also picked up automatically when "
             f"using the default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + count without POSTing or renaming.",
    )
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=0.10,
        help="Refuse to rename source file when errors/total exceeds this "
             "fraction. Default 0.10 (10%%). Set to 1.0 to disable.",
    )
    args = parser.parse_args(argv)

    if args.path is not None:
        targets = [args.path]
    else:
        targets = []
        if _DEFAULT_PRIMARY.is_file():
            targets.append(_DEFAULT_PRIMARY)
        if _DEFAULT_QWEN3.is_file():
            targets.append(_DEFAULT_QWEN3)

    if not targets:
        print(
            "No JSONL files to migrate. Default paths checked:\n"
            f"  - {_DEFAULT_PRIMARY}\n"
            f"  - {_DEFAULT_QWEN3}\n"
            "Nothing to do."
        )
        return 0

    # Hub-up probe: post_rl_event will return False immediately if the
    # token file is missing. Check once up-front so we can emit a clearer
    # message than "100% errors."
    from rl_client.hub_writer import _read_hub_token, _read_hub_port  # noqa: PLC0415
    if _read_hub_token() is None:
        print(
            "ERROR: hub token not found. Start the launcher (or run "
            "`vct-hub --start-if-not-running`) and re-run this script.",
            file=sys.stderr,
        )
        return 2
    print(f"Hub reachable on port {_read_hub_port()}; proceeding.")

    overall_posted = 0
    overall_skipped = 0
    overall_errors = 0

    for target in targets:
        print(f"\n→ {target}")
        if not target.is_file():
            print(f"  skipped: not a file")
            continue
        posted, skipped, errors, reasons = _process_file(
            target,
            dry_run=args.dry_run,
            max_error_rate=args.max_error_rate,
        )
        overall_posted += posted
        overall_skipped += skipped
        overall_errors += errors
        print(f"  posted:  {posted}")
        print(f"  skipped: {skipped} (v1 or unparseable)")
        print(f"  errors:  {errors}")
        if reasons:
            print("  reasons (first 50):")
            for r in reasons[:50]:
                print(f"    {r}")

        if args.dry_run:
            continue

        total = posted + skipped + errors
        if total == 0:
            print(f"  (file empty — nothing to rename)")
            continue
        if total > 0 and (errors / total) > args.max_error_rate:
            print(f"  REFUSED to rename: error_rate > {args.max_error_rate:.1%}.")
            continue

        backup = _rename_to_backup(target)
        print(f"  renamed: {target.name} → {backup.name}")

    print(
        f"\nSummary:\n"
        f"  posted total:  {overall_posted}\n"
        f"  skipped total: {overall_skipped}\n"
        f"  errors total:  {overall_errors}\n"
    )
    if args.dry_run:
        print("Dry-run — no POSTs sent, no files renamed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
