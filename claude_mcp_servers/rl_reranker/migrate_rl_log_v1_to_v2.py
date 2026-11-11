#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Migrate RL data log files from v1 (pre-2026-05-05) to v2 schema.

v1 schema (legacy):
  retrieval event has no embedding metadata. query_emb / per-node emb are
  raw float lists with no annotation of which embedding model produced them.
  This made per-embedding-source RL training impossible: the offline trainer
  could not filter to events whose embeddings matched its target network.

v2 schema (current — see rl_server/rl_logger.py::SCHEMA_VERSION = 2):
  retrieval and citation events both carry `schema_version: 2`.
  retrieval events additionally carry:
    - embedding_source: one of {qwen3, arctic, openai, codesage, jina, unknown}
    - embedding_dim:    int (1024, 1536, 2048, 768, ...)
    - embedding_model:  human-readable identifier (e.g. "qwen3-embedding:0.6b")

Inference rules for legacy events (lossless where possible):
  - 768-dim          → ("jina",     "unclemusclez/jina-embeddings-v2-base-code")
  - 1024-dim + ts ≥ 2026-04-14 → ("qwen3",  "qwen3-embedding:0.6b")
  - 1024-dim + ts < 2026-04-14 → ("arctic", "snowflake-arctic-embed2:latest")
  - 1536-dim         → ("openai",   "text-embedding-3-small")
  - 2048-dim         → ("codesage", "codesage/codesage-large-v2")
  - missing/unknown  → ("unknown",  "")  (offline_trainer skips these)

The cutoff date 2026-04-14 is the day commit 814c4ae shipped the qwen3
migration (`Embedding model migration: qwen3-embedding + CodeSage-Large-v2`).
Before that day, 1024-dim KG embeddings were always snowflake-arctic-embed2.

Citation events have no embeddings — they inherit `embedding_source` by
joining on `task_id`. If the matching retrieval event is not in the same
file (rotation), the citation event is stamped with `embedding_source: ""`
and offline_trainer joins by task_id at training time.

Usage:
    # Dry run — print stats without touching the file
    python migrate_rl_log_v1_to_v2.py path/to/rl_events.jsonl --dry-run

    # In-place migration (writes path.v1.bak then atomically replaces)
    python migrate_rl_log_v1_to_v2.py path/to/rl_events.jsonl

    # Explicit output path (no in-place rewrite)
    python migrate_rl_log_v1_to_v2.py path/to/rl_events.jsonl \\
        --out path/to/rl_events_v2.jsonl

Idempotency:
  Events already at schema_version >= 2 are written through unchanged.
  Running the script twice on the same file is safe.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from vco_lib.atomic import atomic_text_stream

# Day qwen3-embedding became default for 1024-dim KG vectors. Pre-cutoff
# 1024-dim events are inferred to be snowflake-arctic-embed2.
QWEN3_CUTOVER = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)

# Inference table by embedding dim. The 1024-dim case is resolved by ts
# inside _infer_source_for_event below.
_DIM_TO_SOURCE_AND_MODEL: dict[int, tuple[str, str]] = {
    768:  ("jina",     "unclemusclez/jina-embeddings-v2-base-code:latest"),
    1536: ("openai",   "text-embedding-3-small"),
    2048: ("codesage", "codesage/codesage-large-v2"),
}


def _parse_ts(ts: str) -> datetime | None:
    """Parse ISO-8601 ts, returning None if malformed."""
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _infer_source_for_event(event: dict) -> tuple[str, int, str]:
    """
    Return (embedding_source, embedding_dim, embedding_model) for a v1
    retrieval event by inspecting query_emb length + ts.

    Returns ("unknown", 0, "") when no query_emb is present — those events
    cannot be assigned to any RL network.
    """
    query_emb = event.get("query_emb")
    if not query_emb or not isinstance(query_emb, list):
        return ("unknown", 0, "")

    dim = len(query_emb)

    # Non-1024 dims are unambiguous.
    if dim in _DIM_TO_SOURCE_AND_MODEL:
        source, model = _DIM_TO_SOURCE_AND_MODEL[dim]
        return (source, dim, model)

    # 1024-dim: split on ts vs cutover.
    if dim == 1024:
        ts_str = event.get("ts", "")
        ts = _parse_ts(ts_str)
        if ts is None:
            # No ts → conservative fallback to qwen3 (current default), but
            # mark as inferred so reviewer knows to re-check.
            return ("qwen3", 1024, "qwen3-embedding:0.6b")
        if ts >= QWEN3_CUTOVER:
            return ("qwen3", 1024, "qwen3-embedding:0.6b")
        return ("arctic", 1024, "snowflake-arctic-embed2:latest")

    # Unknown dim — should never happen with our embeddings but guard.
    return ("unknown", dim, "")


def _migrate_event(event: dict, task_to_source: dict[str, tuple[str, int, str]]) -> tuple[dict, str]:
    """
    Migrate one event in-place (returns the same dict with v2 fields added).

    Returns (migrated_event, classification_tag). classification_tag is one
    of: "already_v2" | "retrieval_<source>" | "citation_matched" |
    "citation_unmatched" | "unknown_event".
    """
    # Already v2? Pass through.
    if event.get("schema_version") and event.get("schema_version", 0) >= 2:
        return event, "already_v2"

    ev_type = event.get("event")
    if ev_type == "retrieval":
        source, dim, model = _infer_source_for_event(event)
        event["schema_version"] = 2
        event["embedding_source"] = source
        event["embedding_dim"] = dim
        event["embedding_model"] = model
        # Cache for citation events that come later in the same file.
        task_id = event.get("task_id")
        if task_id:
            task_to_source[task_id] = (source, dim, model)
        return event, f"retrieval_{source}"

    if ev_type == "citation":
        event["schema_version"] = 2
        task_id = event.get("task_id")
        if task_id and task_id in task_to_source:
            source, dim, model = task_to_source[task_id]
            event["embedding_source"] = source
            event["embedding_dim"] = dim
            event["embedding_model"] = model
            return event, "citation_matched"
        # Could not find matching retrieval (probably rotated out). Mark
        # source empty — offline_trainer will join by task_id at runtime.
        event["embedding_source"] = ""
        event["embedding_dim"] = 0
        event["embedding_model"] = ""
        return event, "citation_unmatched"

    # Unknown event type — pass through without touching.
    return event, "unknown_event"


def migrate_file(
    in_path: Path,
    out_path: Path,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Migrate a JSONL file from v1 to v2. Returns stats dict.

    If dry_run=True, no file is written; stats reflect what would have
    been written.
    """
    stats: dict[str, int] = {
        "lines_total":           0,
        "lines_skipped_invalid": 0,
        "already_v2":            0,
        "retrieval_qwen3":       0,
        "retrieval_arctic":      0,
        "retrieval_openai":      0,
        "retrieval_codesage":    0,
        "retrieval_jina":        0,
        "retrieval_unknown":     0,
        "citation_matched":      0,
        "citation_unmatched":    0,
        "unknown_event":         0,
    }
    task_to_source: dict[str, tuple[str, int, str]] = {}

    # The output is written through vco_lib.atomic's streaming writer (the
    # one home of the tmp+rename write): a crash or a bad line leaves the
    # previous output intact and no .tmp behind. A dry run writes nothing.
    out_ctx = (
        contextlib.nullcontext(None) if dry_run else atomic_text_stream(out_path)
    )
    with out_ctx as out_fh, in_path.open("r", encoding="utf-8") as in_fh:
        for line in in_fh:
            stats["lines_total"] += 1
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                stats["lines_skipped_invalid"] += 1
                continue

            migrated, tag = _migrate_event(event, task_to_source)
            if tag in stats:
                stats[tag] += 1
            else:
                stats[tag] = 1

            if out_fh is not None:
                out_fh.write(json.dumps(migrated, ensure_ascii=False) + "\n")

    return stats


def _format_stats(stats: dict[str, int]) -> str:
    width = max(len(k) for k in stats)
    lines = []
    for k in (
        "lines_total", "lines_skipped_invalid", "already_v2",
        "retrieval_qwen3", "retrieval_arctic", "retrieval_openai",
        "retrieval_codesage", "retrieval_jina", "retrieval_unknown",
        "citation_matched", "citation_unmatched", "unknown_event",
    ):
        v = stats.get(k, 0)
        lines.append(f"  {k:<{width}}  {v:>10}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", type=Path, help="Path to JSONL file to migrate")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output path (default: in-place with .v1.bak backup)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print stats without writing")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip .v1.bak creation when doing in-place rewrite")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 1

    in_place = args.out is None and not args.dry_run
    out_path = args.out if args.out else args.input

    if args.dry_run:
        print(f"DRY RUN — analysing {args.input} (no files will be written)")
    elif in_place:
        backup = args.input.with_suffix(args.input.suffix + ".v1.bak")
        if not args.no_backup:
            if backup.exists():
                print(f"ERROR: backup already exists at {backup}. Move or "
                      f"delete it before re-running, or use --no-backup.",
                      file=sys.stderr)
                return 1
            print(f"Creating backup: {backup}")
            shutil.copy2(args.input, backup)
        print(f"In-place migration: {args.input}")
    else:
        print(f"Migrating {args.input} → {out_path}")

    stats = migrate_file(args.input, out_path, dry_run=args.dry_run)

    print("\nResults:")
    print(_format_stats(stats))

    sources_total = sum(
        v for k, v in stats.items()
        if k.startswith("retrieval_") and not k.endswith("_unknown")
    )
    if stats["retrieval_unknown"] > 0:
        pct = 100.0 * stats["retrieval_unknown"] / max(1, sources_total + stats["retrieval_unknown"])
        print(f"\nWARNING: {stats['retrieval_unknown']} retrieval events ({pct:.1f}%) "
              f"have no query_emb — they will be skipped by offline_trainer.")

    if stats["citation_unmatched"] > 0:
        print(f"\nNOTE: {stats['citation_unmatched']} citation events could not be "
              f"matched to a retrieval event by task_id (probably rotated out). "
              f"They are stamped with embedding_source='' and offline_trainer "
              f"will resolve them by task_id join at training time.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
