# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco fix-transcript`` — make a poisoned Claude Code session usable again.

The condition it repairs, and how a user recognises it: every request in a
session fails with ``400 … messages.<N>.content.<M>.server_tool_use.id:
String should match pattern '^srvtoolu_[a-zA-Z0-9_]+$'``, switching models
does not help, and ``/compact`` fails too. That is a vendor tool block sitting
in the HISTORY — the session cannot heal itself, because every later request
re-sends it.

This command rewrites the ``.jsonl`` with the same deterministic mapping the
gateway applies in flight (:mod:`vco_lib.transcript_repair`), so a session
repaired here and a session that never left the gateway agree byte for byte.

Exit codes (the family used by :mod:`vco_lib.cli.verify`):

* 0 — repaired, or already clean (both are success).
* 1 — the file could not be read or written.
* 2 — the argument does not name a readable file.

``--dry-run`` reports the same summary and writes nothing, which is the
recommended first run: the repair is not reversible beyond the backup it
leaves at ``<file>.bak-<timestamp>``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from vco_lib.transcript_repair import FileRepairResult, repair_file

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BAD_ARGS = 2


def _as_json(result: FileRepairResult) -> str:
    return json.dumps(
        {
            "path": str(result.path),
            "entries": result.entries_total,
            "entries_touched": result.entries_touched,
            "ids_rewritten": result.stats.ids_rewritten,
            "blocks_stripped": result.stats.blocks_stripped,
            "results_stripped": result.stats.results_stripped,
            "stripped_names": sorted(result.stats.stripped_names),
            "unparseable_lines": result.unparseable_lines,
            "backup": str(result.backup_path) if result.backup_path else None,
            "dry_run": result.dry_run,
        },
        indent=2,
    )


def run(
    path: Path,
    *,
    dry_run: bool = False,
    as_json: bool = False,
    out: Any = None,
) -> int:
    """Repair ``path``. Returns the process exit code."""
    stream = out if out is not None else sys.stdout
    if not path.is_file():
        print(
            f"fix-transcript: {path} is not a readable file. Pass the session "
            "JSONL, e.g. ~/.claude/projects/<slug>/<session-id>.jsonl",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS
    try:
        result = repair_file(path, dry_run=dry_run)
    except OSError as exc:
        print(f"fix-transcript: {path}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(_as_json(result) if as_json else result.render(), file=stream)
    return EXIT_OK


def _main(args: argparse.Namespace) -> int:
    return run(
        Path(args.session_file).expanduser(),
        dry_run=bool(args.dry_run),
        as_json=bool(args.json),
    )


def add_subparsers(sub: Any) -> None:
    """Register ``fix-transcript`` onto the parent subparsers."""
    parser = sub.add_parser(
        "fix-transcript",
        help=(
            "Repair a Claude Code session .jsonl whose history carries "
            "vendor-shaped tool ids (400 'server_tool_use.id: String should "
            "match pattern ^srvtoolu_'). Backs up to <file>.bak-<timestamp>."
        ),
    )
    parser.add_argument(
        "session_file",
        help=(
            "Path to the session JSONL "
            "(~/.claude/projects/<project-slug>/<session-id>.jsonl)."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change and write nothing.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the summary as a single JSON object.",
    )
    parser.set_defaults(func=_main)


__all__ = ["add_subparsers", "run"]
