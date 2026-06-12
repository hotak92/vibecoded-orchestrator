#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""cost-summary.py — Claude token / cost summary from ~/.claude/metrics/costs.jsonl.

Portable entry point (v0.2.54 Track G, G-3): runs identically on
Linux / macOS / Windows with any Python 3.9+, no venv needed (stdlib only).
The records are written by the cost-tracker Stop hook
(templates/hooks/cost-tracker.sh / cost-tracker.ps1 — both emit the same
JSONL shape).

Usage:
    python .claude/scripts/cost-summary.py
    python .claude/scripts/cost-summary.py --days 7
    python .claude/scripts/cost-summary.py --session <SESSION_ID>

History: this logic used to live as an inline heredoc in the bash-only
`cost-summary` wrapper, which (a) didn't run on native Windows at all and
(b) had a broken argument path — it read positional $1/$2 but the Python
heredoc looked for a DAYS env var that was never exported, so `--days N`
silently did nothing. The bash wrapper now delegates here.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_records(costs_file: Path) -> list[dict]:
    records: list[dict] = []
    with open(costs_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # tolerate torn writes from concurrent hook appends
    return records


def filter_records(
    records: list[dict],
    days: int | None,
    session: str | None,
) -> list[dict]:
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        kept = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                kept.append(r)
        records = kept
    if session:
        records = [r for r in records if r.get("session_id") == session]
    return records


def summarize(records: list[dict]) -> str:
    # Split by auth mode: subscription rows have cost_usd=null (claude.ai
    # subscription tokens are included in the plan, not metered per token).
    api_records = [
        r for r in records
        if r.get("auth_mode") == "api" or r.get("cost_usd") not in (None, 0)
    ]
    sub_records = [r for r in records if r.get("auth_mode") == "subscription"]
    # Legacy rows (pre-2026-05-01 cost-tracker) had no auth_mode field.
    # Treat them as API for backward compat IFF cost_usd is non-null.
    legacy_records = [r for r in records if r.get("auth_mode") is None]

    def _safe_cost(r: dict) -> float:
        c = r.get("cost_usd")
        return c if isinstance(c, (int, float)) else 0.0

    total_cost = sum(_safe_cost(r) for r in api_records + legacy_records)
    total_input = sum(r.get("input_tokens", 0) for r in records)
    total_output = sum(r.get("output_tokens", 0) for r in records)
    total_cache = sum(r.get("cache_read_tokens", 0) for r in records)

    by_model: dict[str, dict] = defaultdict(
        lambda: {"cost": 0.0, "input": 0, "output": 0, "count": 0}
    )
    for r in records:
        m = r.get("model", "(unknown)")
        by_model[m]["cost"] += _safe_cost(r)
        by_model[m]["input"] += r.get("input_tokens", 0)
        by_model[m]["output"] += r.get("output_tokens", 0)
        by_model[m]["count"] += 1

    lines = [
        "=== Claude Token / Cost Summary ===",
        f"Records: {len(records)}  ({len(api_records)} api / "
        f"{len(sub_records)} subscription / {len(legacy_records)} legacy)",
        f"Total billable cost: ${total_cost:.4f}  (subscription tokens are free)",
        f"Total tokens: {total_input:,} in + {total_output:,} out + "
        f"{total_cache:,} cache_read",
        "",
        "By model:",
    ]
    for model, stats in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
        lines.append(
            f"  {model}: ${stats['cost']:.4f} ({stats['count']} responses, "
            f"{stats['input']:,}in/{stats['output']:,}out)"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show Claude API cost summary from ~/.claude/metrics/costs.jsonl"
    )
    parser.add_argument("--days", type=int, default=None,
                        help="Only include records from the last N days")
    parser.add_argument("--session", default=None,
                        help="Only include records for this session_id")
    parser.add_argument("--costs-file", type=Path, default=None,
                        help=argparse.SUPPRESS)  # test hook
    args = parser.parse_args(argv)

    costs_file = args.costs_file or (
        Path.home() / ".claude" / "metrics" / "costs.jsonl"
    )
    if not costs_file.is_file():
        print("No cost data yet. Costs are tracked per response in "
              "~/.claude/metrics/costs.jsonl")
        return 0

    records = filter_records(load_records(costs_file), args.days, args.session)
    if not records:
        print("No cost records found.")
        return 0

    print(summarize(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
