#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Remove SETUP-ONLY blocks from CLAUDE.md and record the acknowledgement.

Serves BOTH install shapes the script ships into: the orchestrator root and
every user project (the bundle installs it under ``<root>/.claude/scripts/``
in each). Their CLAUDE.md files carry first-run help and a project-scoping
nudge wrapped in HTML-comment markers:

    <!-- BEGIN: SETUP-ONLY (...) -->
    ... content ...
    <!-- END: SETUP-ONLY -->

Once setup is done, that content becomes noise that wastes context every
session. This script strips those blocks.

Those markers live INSIDE the re-rendered managed region of the shipped
templates — the root's AUTO region (``templates/ORCHESTRATOR-CLAUDE.md.template``,
re-rendered by ``install.py`` on every update) and a project's VCO_MANAGED
region (``templates/CLAUDE.md.template``, re-rendered on every bundle update) —
so a removal on its own would be undone. This script therefore ALSO records an
acknowledgement — the content hash of every block it removes — in
``.claude/state/setup-sections-ack.json`` (written atomically). The render
paths (``vco_lib/rendered_root_files.py`` for the root,
``vco_lib/project_templates.py`` for user projects) omit any block whose hash
is acknowledged, so the removal SURVIVES future updates, and a block whose
content changes in a later release (new hash) renders again and re-arms the
``first_run_setup_pending`` deferral row this script clears.

The script is idempotent: running it twice is safe — the second run is a
no-op.

Usage:
    python .claude/scripts/cleanup-setup-sections.py [--root PATH]

The script:
  - Resolves the install root relative to this script's location
    (../../ = the root this copy ships under — the orchestrator root or a
    user project's root); ``--root`` overrides it.
  - Removes everything between matching BEGIN/END markers (inclusive)
  - Records each removed block's content hash in the acknowledgement file
  - Writes the result back, preserving the rest verbatim
  - Clears the ``first_run_setup_pending`` deferral row
  - Prints a one-line summary to stdout
  - Exits 0 on success, 1 on parse error (unmatched markers) or when the
    acknowledgement cannot be recorded

Why not auto-execute: the user must explicitly opt in. CLAUDE.md is part of
their project; we don't silently rewrite it.
"""

from __future__ import annotations

import sys
from pathlib import Path

# vco_lib is part of every healthy install; a failed import means a BROKEN
# install (never a fallback). The script ships into `<root>/.claude/scripts/`,
# so the install root (two levels up) holds `vco_lib/` in a source checkout and
# is on the venv's path for an editable install.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = SCRIPT_DIR.parent.parent
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

try:
    from vco_lib import setup_sections
except ImportError as exc:  # pragma: no cover - exercised only on a broken install
    print(
        f"error: cannot import vco_lib.setup_sections ({exc}) — this means a "
        "BROKEN install; re-run 'python install.py --update'",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


def strip_setup_blocks(text: str) -> tuple[str, int, int]:
    """Strip SETUP-ONLY blocks from text. Returns (new_text, blocks_removed, lines_removed).

    A thin wrapper over the ONE marker rule (``vco_lib.setup_sections``), shared
    with the renderer so the two cannot disagree about a block's bounds. Raises
    ``ValueError`` on an unmatched marker.
    """
    cleaned, removed = setup_sections.strip_blocks(text, lambda _block: True)
    lines_removed = sum(block.text.count("\n") for block in removed)
    return cleaned, len(removed), lines_removed


def _parse_root(argv: list[str]) -> Path:
    """The install root: ``--root PATH``, else the script's own location."""
    args = list(argv)
    root = DEFAULT_ROOT
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--root":
            if i + 1 >= len(args):
                raise SystemExit("error: --root needs a path argument")
            root = Path(args[i + 1]).expanduser().resolve()
            i += 2
            continue
        if arg.startswith("--root="):
            root = Path(arg.split("=", 1)[1]).expanduser().resolve()
            i += 1
            continue
        raise SystemExit(f"error: unrecognised argument {arg!r}")
    return root


def main(argv: list[str] | None = None) -> int:
    root = _parse_root(list(sys.argv[1:] if argv is None else argv))
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        print(f"error: {claude_md} not found", file=sys.stderr)
        return 1

    original = claude_md.read_text(encoding="utf-8")

    try:
        cleaned, blocks, removed_lines = strip_setup_blocks(original)
        removed = setup_sections.find_blocks(original)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if blocks == 0:
        # Nothing to strip — but still clear a stale row (idempotent rerun).
        setup_sections.emit_pending_deferral(root, ())
        print("No setup-only sections found — nothing to remove.")
        return 0

    # Record the acknowledgement BEFORE stripping: a removal the ack did not
    # capture would be undone by the next update, so an ack failure must leave
    # the file untouched and fail loudly rather than half-apply.
    if not setup_sections.record_acknowledged(root, [b.sha256 for b in removed]):
        print(
            f"error: could not write {setup_sections.ack_path(root)} — refusing "
            "to strip the blocks, because the removal would not survive the "
            "next update",
            file=sys.stderr,
        )
        return 1

    claude_md.write_text(cleaned, encoding="utf-8")
    # The clear probe also sees all rendered blocks acknowledged; resolving here
    # makes the ledger reminder disappear in the same action.
    setup_sections.emit_pending_deferral(root, ())
    print(
        f"Removed {blocks} setup-only section{'s' if blocks != 1 else ''} "
        f"({removed_lines} lines)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
