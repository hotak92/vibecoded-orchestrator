#!/usr/bin/env python3
"""Remove SETUP-ONLY blocks from CLAUDE.md.

CLAUDE.md ships with first-run-setup help wrapped in HTML-comment markers:

    <!-- BEGIN: SETUP-ONLY (remove after first successful session) -->
    ... content ...
    <!-- END: SETUP-ONLY -->

Once the user has the orchestrator working, that content becomes noise that
wastes context every session. This script strips those blocks.

The script is idempotent: running it twice is safe — the second run is a
no-op.

Usage:
    python .claude/scripts/cleanup-setup-sections.py

The script:
  - Resolves CLAUDE.md relative to this script's location (../../CLAUDE.md)
  - Removes everything between matching BEGIN/END markers (inclusive)
  - Writes the result back, preserving the rest verbatim
  - Prints a one-line summary to stdout
  - Exits 0 on success, 1 on parse error (unmatched markers)

Why not auto-execute: the user must explicitly opt in. CLAUDE.md is part of
their project; we don't silently rewrite it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CLAUDE_MD = SCRIPT_DIR.parent.parent / "CLAUDE.md"

BEGIN = re.compile(r"<!--\s*BEGIN:\s*SETUP-ONLY[^>]*-->")
END = re.compile(r"<!--\s*END:\s*SETUP-ONLY[^>]*-->")


def strip_setup_blocks(text: str) -> tuple[str, int, int]:
    """Strip SETUP-ONLY blocks from text. Returns (new_text, blocks_removed, lines_removed)."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    blocks = 0
    removed_lines = 0
    while i < len(lines):
        if BEGIN.search(lines[i]):
            # Find matching END
            j = i + 1
            depth = 1
            while j < len(lines) and depth > 0:
                if BEGIN.search(lines[j]):
                    depth += 1
                elif END.search(lines[j]):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                raise ValueError(
                    f"Unmatched SETUP-ONLY BEGIN at line {i + 1} — no closing END found"
                )
            # Skip lines i..j (inclusive)
            removed_lines += j - i + 1
            blocks += 1
            i = j + 1
            # If the block was followed by a single blank separator line,
            # consume it so the document doesn't accumulate stray blank lines
            # on repeated cleanups (idempotency hygiene).
            if i < len(lines) and lines[i].strip() == "":
                i += 1
        else:
            if END.search(lines[i]):
                raise ValueError(
                    f"Unmatched SETUP-ONLY END at line {i + 1} — no opening BEGIN above"
                )
            out.append(lines[i])
            i += 1
    return "".join(out), blocks, removed_lines


def main() -> int:
    if not CLAUDE_MD.exists():
        print(f"error: {CLAUDE_MD} not found", file=sys.stderr)
        return 1

    original = CLAUDE_MD.read_text(encoding="utf-8")

    try:
        cleaned, blocks, removed_lines = strip_setup_blocks(original)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if blocks == 0:
        print("No setup-only sections found — nothing to remove.")
        return 0

    CLAUDE_MD.write_text(cleaned, encoding="utf-8")
    print(f"Removed {blocks} setup-only section{'s' if blocks != 1 else ''} ({removed_lines} lines).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
