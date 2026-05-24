# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco`` CLI dispatcher — argparse top-level.

Wires subcommand modules onto a single ``vco`` script so users can
type ``vco verify-pins`` rather than ``python -m vco_lib.cli.verify ...``.

Until packaging lands (``pyproject.toml`` ``[project.scripts]`` entry —
see plan §3 Phase 0.C step 2), this is callable via
``python -m vco_lib.cli`` and the optional ``scripts/vco`` shim wrapper.

Subcommand modules contribute via :func:`add_subparsers(sub)` so adding
a new one is a single-line registration here.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from vco_lib.cli import rebuild_diagram_index as _rebuild_diagram_index
from vco_lib.cli import verify as _verify


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vco",
        description=(
            "VibeCoded Orchestrator CLI. Each subcommand maps to an "
            "operational verifier or maintenance helper. Phase 0 ships "
            "verify-pins + verify-env-projection; Phase 1.5 adds "
            "rebuild-diagram-index; later phases extend."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)
    _verify.add_subparsers(sub)
    _rebuild_diagram_index.add_subparsers(sub)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:  # pragma: no cover — argparse enforces required=True
        parser.print_help(sys.stderr)
        return 2
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main())
