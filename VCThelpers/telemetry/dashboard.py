# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""User-facing CLI to inspect and control local telemetry.

Wire-up (in the top-level `vibecoded` CLI script):

    from VCThelpers.telemetry import dashboard
    # subcommand dispatch:
    if args.cmd == "telemetry":
        dashboard.main(args.rest)

Or run directly:
    python -m VCThelpers.telemetry.dashboard show
    python -m VCThelpers.telemetry.dashboard show --all
    python -m VCThelpers.telemetry.dashboard clear
    python -m VCThelpers.telemetry.dashboard status

Plain stdlib only — no dependencies.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import List, Optional, Sequence

from .consent import CONFIG_FILE, OPTIN_CATEGORIES, load_consent
from .queue import get_queue


def _fmt_ts(epoch: Optional[float]) -> str:
    if not epoch:
        return "—"
    try:
        return _dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "—"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _summarize_payload(payload: dict) -> str:
    """One-line summary of an event payload for the table view."""
    if not isinstance(payload, dict):
        return _truncate(str(payload), 60)

    inner = payload.get("payload", payload)
    keys_to_show: List[str] = []
    if isinstance(inner, dict):
        for key in ("tool_name", "task_type", "chosen_agent", "outcome",
                    "license_tier", "license_valid", "result_count",
                    "latency_ms"):
            if key in inner and inner[key] not in (None, ""):
                keys_to_show.append(f"{key}={inner[key]}")
        if not keys_to_show:
            # Fall back to the first 2 keys.
            for k in list(inner.keys())[:2]:
                v = inner[k]
                if isinstance(v, (list, dict)):
                    v = f"<{type(v).__name__} len={len(v)}>"
                keys_to_show.append(f"{k}={v}")
    return _truncate(", ".join(keys_to_show) or "—", 60)


def _print_table(rows: Sequence[dict], include_uploaded: bool) -> None:
    if not rows:
        print("(no events)")
        return
    header = ("ID", "TYPE", "CREATED", "UPLOADED" if include_uploaded else "", "SUMMARY")
    widths = [6, 22, 19, 19 if include_uploaded else 0, 60]

    def _emit(cells: Sequence[str]) -> None:
        parts = []
        for cell, w in zip(cells, widths):
            if w <= 0:
                continue
            parts.append(cell.ljust(w))
        print("  ".join(parts).rstrip())

    _emit(header)
    _emit(["-" * max(1, w) for w in widths])

    for r in rows:
        uploaded_val = _fmt_ts(r.get("uploaded_at"))
        _emit([
            str(r["id"]),
            _truncate(r["event_type"], widths[1]),
            _fmt_ts(r["created_at"]),
            uploaded_val if include_uploaded else "",
            _summarize_payload(r.get("payload") or {}),
        ])


def cmd_show(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="vibecoded telemetry show")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include already-uploaded events (default: last 20 mixed).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max events to show (default 20, use a large number with --all).",
    )
    parser.add_argument(
        "--pending-only",
        action="store_true",
        help="Only show events not yet uploaded.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of the table view.",
    )
    args = parser.parse_args(argv)

    q = get_queue()
    if args.all:
        limit = max(args.limit, 10000)
        rows = q.recent_events(limit=limit, include_uploaded=True)
    elif args.pending_only:
        rows = q.recent_events(limit=args.limit, include_uploaded=False)
    else:
        rows = q.recent_events(limit=args.limit, include_uploaded=True)

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    print(f"Telemetry queue: {q.count_pending()} pending / {q.count_total()} total")
    print()
    _print_table(rows, include_uploaded=True)
    return 0


def cmd_clear(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="vibecoded telemetry clear")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    args = parser.parse_args(argv)

    q = get_queue()
    total = q.count_total()
    if total == 0:
        print("Queue already empty.")
        return 0
    if not args.yes:
        try:
            print(f"About to delete {total} telemetry event(s) from the local queue.")
            print("Continue? [y/N]: ", end="", flush=True)
            answer = sys.stdin.readline().strip().lower()
        except (OSError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1
    removed = q.clear()
    print(f"Deleted {removed} event(s).")
    return 0


def cmd_status(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="vibecoded telemetry status")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    q = get_queue()
    consent = load_consent()
    summary = {
        "consent_file": str(CONFIG_FILE),
        "consent": consent,
        "queue_pending": q.count_pending(),
        "queue_total": q.count_total(),
    }

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"Consent file: {CONFIG_FILE}")
    print(f"Consent version: {consent.get('consent_version', '<unset>')}")
    print(f"Granted at: {consent.get('granted_at', '<unset>')}")
    print("Always-on: ON (license validation, version, session timestamps)")
    for cat in OPTIN_CATEGORIES:
        flag = "ON " if consent.get(cat) else "off"
        print(f"  [{flag}] {cat}")
    print()
    print(f"Queue: {q.count_pending()} pending, {q.count_total()} total")
    return 0


_COMMANDS = {
    "show": cmd_show,
    "clear": cmd_clear,
    "status": cmd_status,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: vibecoded telemetry {show,clear,status} [options]")
        print("  show [--all] [--pending-only] [--limit N] [--json]")
        print("  clear [--yes]")
        print("  status [--json]")
        return 0 if argv else 1
    cmd = argv[0]
    rest = argv[1:]
    fn = _COMMANDS.get(cmd)
    if fn is None:
        print(f"Unknown subcommand: {cmd}", file=sys.stderr)
        print("Available: show, clear, status", file=sys.stderr)
        return 2
    return fn(rest)


if __name__ == "__main__":
    raise SystemExit(main())
