# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Diagram path validator — LOCAL STUB for Phase 1.5.A worktree.

The canonical implementation of this module is owned by Phase 1.2
(wrapper MCP). This local copy exists so:

  1. `vco_lib/diagram_indexer.py` can `from vco_lib.diagram_paths import
     validate_scoped_path` cleanly during parallel development.
  2. The `pre-diagram-path-validation.sh` hook can invoke
     `python -m vco_lib.diagram_paths validate <path>` without depending
     on the sibling worktree.

At integration time, the Phase 1.2 sibling's canonical module
SUPERSEDES this stub. The integrator should:
  - Diff this file against the sibling's `vco_lib/diagram_paths.py`;
  - Resolve to one canonical version (sibling's contract is the source
    of truth; this stub is a structural copy);
  - Drop the `try/except ImportError → _local_validate_scoped_path`
    fallback in `vco_lib/diagram_indexer.py::_validate_scoped_path`
    once a single module owns the contract.

Contract:
  - file_path must contain `.claude/diagrams/` (anywhere up its parents).
  - Relative to that anchor: `<category>/<name>.{mmd,excalidraw}` with
    at least one category directory (no flat-folder dumps).
  - `<name>` matches `[a-z0-9][a-z0-9-]*` (lowercase-kebab-case).
  - Path traversal (`..`) is rejected (security).
  - Returns `(diagram_type, category_path, diagram_name)` on success.
  - Raises `ValueError` with a clear, copy-pasteable corrective example
    on failure (the hook surfaces this message to Claude as the block
    reason).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Tuple

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_scoped_path(file_path: Path) -> Tuple[str, str, str]:
    """Validate a diagram file path against the scoped-path rule.

    Args:
        file_path: Absolute or relative path to the diagram file. We do
            NOT resolve symlinks — the path is inspected structurally so
            the user's intended layout (not the resolved physical layout)
            is what gets validated.

    Returns:
        (diagram_type, category_path, diagram_name) — same shape as
        `vco_lib.diagram_indexer._local_validate_scoped_path`.

    Raises:
        ValueError: with a copy-pasteable corrective message naming the
            expected layout. The hook prints this to stderr verbatim
            and exits 2 (block the write per Claude Code hook spec).
    """
    # Don't follow symlinks; structural validation is the goal.
    parts = file_path.parts

    # Find the `<...>/.claude/diagrams/` anchor. We accept the deepest
    # match so `.claude/diagrams/` inside a nested project (rare but
    # possible) wins over an outer one.
    anchor_idx = -1
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "diagrams":
            anchor_idx = i + 1

    if anchor_idx == -1:
        raise ValueError(
            f"Diagram path '{file_path}' must live under "
            f".claude/diagrams/<category>/<name>.{{mmd,excalidraw}}. "
            f"Example: .claude/diagrams/gui/auth/login-form.mmd"
        )

    rel_parts = parts[anchor_idx + 1:]

    if any(p == ".." for p in rel_parts):
        raise ValueError(
            f"Diagram path '{file_path}' contains '..' path traversal — "
            f"refused for security. Use a literal subdirectory under "
            f".claude/diagrams/<category>/."
        )

    if len(rel_parts) < 2:
        raise ValueError(
            f"Diagram path '{file_path}' is flat — must include at "
            f"least one category directory. "
            f"Example: .claude/diagrams/gui/auth/login-form.mmd"
        )

    name_with_ext = rel_parts[-1]
    suffix = Path(name_with_ext).suffix.lower()
    if suffix == ".mmd":
        diagram_type = "mermaid"
    elif suffix == ".excalidraw":
        diagram_type = "excalidraw"
    else:
        raise ValueError(
            f"Diagram path '{file_path}' has unsupported extension "
            f"'{suffix}' — must be .mmd or .excalidraw. "
            f"Example: .claude/diagrams/gui/auth/login-form.mmd"
        )

    diagram_name = name_with_ext[: -len(suffix)]
    if not _NAME_RE.match(diagram_name):
        raise ValueError(
            f"Diagram name '{diagram_name}' must match "
            f"[a-z0-9][a-z0-9-]* (lowercase-kebab-case). "
            f"Examples: 'login-form-v2' (ok), 'LoginForm' (rejected), "
            f"'login_form' (rejected), '2fa-flow' (ok)."
        )

    category_path = "/".join(rel_parts[:-1])
    return diagram_type, category_path, diagram_name


# ---------------------------------------------------------------------------
# CLI (invoked by templates/hooks/pre-diagram-path-validation.sh)
# ---------------------------------------------------------------------------


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.diagram_paths",
        description="Validate diagram paths against the scoped-path rule.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser(
        "validate",
        help="Validate a single path; exit 0 ok, exit 2 violation.",
    )
    p_val.add_argument("file_path", type=Path)
    p_val.add_argument(
        "--kind",
        default="auto",
        choices=["auto", "mermaid", "excalidraw"],
        help="Force a diagram_type expectation (default: auto from suffix).",
    )

    args = parser.parse_args(argv)

    if args.cmd != "validate":  # pragma: no cover — argparse handles this
        parser.error("unknown command")

    try:
        dtype, cat, name = validate_scoped_path(args.file_path)
    except ValueError as exc:
        # Hooks expect the corrective message on stderr — they pipe it
        # straight to Claude. Exit 2 is the documented block-the-write
        # convention for PreToolUse hooks.
        print(str(exc), file=sys.stderr)
        return 2

    if args.kind != "auto" and dtype != args.kind:
        print(
            f"Expected kind={args.kind}, got {dtype} from extension.",
            file=sys.stderr,
        )
        return 2

    # On success print the parsed fields for any caller that wants them.
    print(f"OK type={dtype} category={cat} name={name}")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry point
    raise SystemExit(_cli())
