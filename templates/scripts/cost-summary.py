#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""cost-summary.py — Claude token / cost summary from the metrics `costs.jsonl`.

Portable entry point (v0.2.54 Track G, G-3): runs identically on
Linux / macOS / Windows with any Python 3.9+, no venv needed (stdlib only).
The records are written by the cost-tracker Stop hook
(templates/hooks/cost-tracker.sh / cost-tracker.ps1 — both emit the same
JSONL shape).

Usage:
    python .claude/scripts/cost-summary.py
    python .claude/scripts/cost-summary.py --days 7
    python .claude/scripts/cost-summary.py --session <SESSION_ID>

**Where the data lives (v0.2.92 W7).** The metrics home is
``$VCT_STATE_DIR/metrics`` (default ``~/.vct/metrics``). It used to be
``~/.claude/metrics``, which is now a FROZEN ARCHIVE: VCO copies out of it and
never writes to it or deletes it.

This reader consults BOTH, most-current first, and merges them —
deduplicating rows that appear in both (the copy leaves the archive intact, so
every migrated row exists twice by design and counting it twice would inflate
every total on this page). Three real states need that:

1. the copy has not run yet on this machine, so ALL current rows are still in
   the archive;
2. a hook is still writing to the archive. The common shape of this resolves
   itself: since v0.2.84 D7 (ruling R2), ``install-bundle --update`` **ADOPTS**
   a ``cost-tracker.sh`` the user edited — it backs their bytes up to
   ``.claude/backups/bundle-adoptions/<ts>/`` and writes the shipped version —
   so that user DOES get the new home and can still recover their edit. The
   split history is what is left when the backup write FAILS (full disk,
   read-only ``.claude/``): only then does the file stay ``preserve`` +
   ``bundle_user_modified_preserved`` and keep appending to the archive
   indefinitely. (This paragraph claimed a plain "PRESERVED" until v0.2.92,
   which had been false since v0.2.84.)
3. a downgrade wrote to the archive again after the move.

A reader that looked at one directory would show such a user a silently
partial total, which is the worst possible failure for a cost report.

Stdlib-only is a hard constraint here (this ships into every project's
``.claude/scripts/`` and runs on a bare interpreter), so the two directories
are resolved inline rather than by importing ``vco_lib.paths``. The rule is
the same one ``vco_lib/paths.py::vct_metrics_dir`` /
``legacy_claude_metrics_dir`` implement, and
``tests/test_v0292_wp8_cost_summary_reader.py`` pins this file's answer
against those functions so the two cannot drift.

History: this logic used to live as an inline heredoc in the bash-only
`cost-summary` wrapper, which (a) didn't run on native Windows at all and
(b) had a broken argument path — it read positional $1/$2 but the Python
heredoc looked for a DAYS env var that was never exported, so `--days N`
silently did nothing. The bash wrapper now delegates here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Basename of the stream this tool reads, in both directories.
COSTS_BASENAME = "costs.jsonl"


def metrics_dirs() -> "list[Path]":
    """Metrics directories to read, most-current first.

    Mirrors ``vco_lib.paths.metrics_read_dirs()``:
    ``$VCT_STATE_DIR/metrics`` (default ``~/.vct/metrics``) then
    ``$VCT_CLAUDE_DIR/metrics`` (default ``~/.claude/metrics``, the frozen
    archive). Inline rather than imported because this script must run on a
    bare stdlib interpreter with no VCO packages installed; the parity test
    named in the module docstring is what keeps the two honest.

    ``os.path.expanduser`` handles ``~`` on all three OSes; no separator is
    ever hardcoded (``Path`` joins natively).
    """
    state_dir = os.environ.get("VCT_STATE_DIR", "").strip()
    claude_dir = os.environ.get("VCT_CLAUDE_DIR", "").strip()
    new_home = Path(state_dir) if state_dir else Path.home() / ".vct"
    archive = Path(claude_dir) if claude_dir else Path.home() / ".claude"
    return [new_home / "metrics", archive / "metrics"]


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


def _dedup_key(record: dict) -> str:
    """Canonical identity of a record. Key order never changes the answer."""
    try:
        return json.dumps(record, sort_keys=True)
    except (TypeError, ValueError):
        return repr(record)


def load_all_records(costs_files: "list[Path]") -> list[dict]:
    """Merge several ``costs.jsonl`` files without counting a copied row twice.

    The merge is a **max-multiplicity union**, not a set union, and the
    distinction is load-bearing in both directions:

    * **Across files** a repeat is a COPY, not a second event. The migration
      duplicates history by design (it leaves the archive intact), so after it
      runs every historical row exists in both directories and a naive sum
      would double this report's headline number.
    * **Within one file** a repeat is REAL. Two responses can legitimately
      produce byte-identical rows — same second, same session, same model,
      same token counts — and collapsing them would under-report the user's
      spend. A plain ``set`` did exactly that and
      ``tests/test_cost_summary_py.py::test_session_filter`` caught it: three
      rows, two of them identical, reported as one.

    So each file contributes up to its OWN count of a given row, and the
    result holds the maximum any single file had. Same rule the migration's
    multiset dedup uses, which is why a merged read of a migrated pair equals
    a read of either side alone.
    """
    emitted: defaultdict = defaultdict(int)
    merged: list[dict] = []
    for path in costs_files:
        local: defaultdict = defaultdict(int)
        already = dict(emitted)
        for record in load_records(path):
            key = _dedup_key(record)
            local[key] += 1
            if local[key] <= already.get(key, 0):
                continue  # this occurrence is already represented
            merged.append(record)
        for key, count in local.items():
            if count > emitted[key]:
                emitted[key] = count
    return merged


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
        description=(
            "Show Claude API cost summary from the metrics costs.jsonl "
            "($VCT_STATE_DIR/metrics, default ~/.vct/metrics — plus the "
            "frozen pre-v0.2.92 archive under ~/.claude/metrics, merged and "
            "de-duplicated)"
        )
    )
    parser.add_argument("--days", type=int, default=None,
                        help="Only include records from the last N days")
    parser.add_argument("--session", default=None,
                        help="Only include records for this session_id")
    parser.add_argument("--costs-file", type=Path, default=None,
                        help=argparse.SUPPRESS)  # test hook
    args = parser.parse_args(argv)

    if args.costs_file is not None:
        candidates = [args.costs_file]
    else:
        candidates = [d / COSTS_BASENAME for d in metrics_dirs()]
    present = [p for p in candidates if p.is_file()]

    if not present:
        # Name the WRITE target, not every path probed: telling the user to
        # look in a directory the current code no longer writes to would be a
        # printed instruction that does not help.
        print("No cost data yet. Costs are tracked per response in "
              f"{candidates[0]}")
        return 0

    records = filter_records(load_all_records(present), args.days, args.session)
    if not records:
        print("No cost records found.")
        return 0

    if len(present) > 1:
        # Surface the split rather than silently merging: a user seeing this
        # line after v0.2.92 has either not been migrated yet or is running a
        # preserved user-modified cost-tracker hook, and both are worth
        # knowing about when reading a cost total.
        print(
            "Note: merging "
            + " + ".join(str(p) for p in present)
            + " (rows present in both are counted once)."
        )
    print(summarize(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
