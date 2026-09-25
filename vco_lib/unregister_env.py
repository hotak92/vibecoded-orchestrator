# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The unregister's strip of VCO's routing keys from ``.claude/env`` and the
two JSON settings env blocks — the evidence rule (v0.2.97 review R6).

An unregister removes what VCO wrote and nothing else:

* ``.claude/env``: VCO's managed block (``# vco-managed-begin`` …
  ``# vco-managed-end``) goes WHOLE — its markers prove it is VCO's. Outside
  it, a routing key's line goes only when its value EQUALS what VCO projects
  for this project; a ``# KEY=`` comment sets nothing and stays.
* ``.claude/settings.json`` ``env`` / ``.vscode/settings.json``
  ``claude-code.env`` (no block): a routing key goes only when its value
  equals VCO's.

"What VCO projects" is :func:`vco_lib.config_projection.project_env_from_db`
for the project — the same resolution the env refresh writes with, so the
unregister must run it BEFORE the project's row is deleted (the launcher's
``delete_project_v2`` strips first). A value that differs is the user's: it
is left and reported by name. When the projection cannot be computed (the row
is gone, the DB is unreadable) nothing outside the block is removed and the
reply says why.

Secret VALUES are a different step with its own evidence
(``config_projection strip-proven-secret-values``); the project ``.env`` is
``env_template strip``. Never prints a value.

CLI: ``python -m vco_lib.unregister_env strip-routing --project-folder F
[--project-id ID] [--orchestrator-root R] [--db-path P]`` with ``{"keys":
[...]}`` on stdin → ONE JSON object on stdout.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Mapping, Optional

from vco_lib import config_projection as cp
from vco_lib.atomic import atomic_rewrite_text
from vco_lib.envfile import parse_env_line

CLAUDE_ENV_REL = ".claude/env"


def projected_env(
    project_id: str,
    *,
    db_path: Optional[Path] = None,
    orchestrator_root: Optional[Path] = None,
) -> dict[str, str]:
    """The canonical env VCO projects for ``project_id`` — the values its
    refresh writes to ``.claude/env`` and the JSON env blocks."""
    bundle = cp.project_env_from_db(
        project_id, db_path=db_path, orchestrator_root=orchestrator_root,
    )
    return dict(bundle["canonical_env"])


def _without_managed_block(text: str) -> tuple[str, str]:
    """``(text without VCO's .claude/env block, the block)``. A block with no
    END runs to EOF (a crash mid-write); the line ending after END goes with
    it, CRLF or LF."""
    begin = text.find(cp.CLAUDE_ENV_MANAGED_BEGIN)
    if begin == -1:
        return text, ""
    end_off = text[begin:].find(cp.CLAUDE_ENV_MANAGED_END)
    stop = len(text) if end_off == -1 else begin + end_off + len(cp.CLAUDE_ENV_MANAGED_END)
    for ending in ("\r\n", "\n"):
        if text.startswith(ending, stop):
            stop += len(ending)
            break
    return text[:begin] + text[stop:], text[begin:stop]


def strip_claude_env(
    project_folder: Path,
    keys: Iterable[str],
    expected: Optional[Mapping[str, str]],
) -> tuple[list[str], list[str]]:
    """``.claude/env``: VCO's block goes whole; outside it, an active line
    assigning one of ``keys`` goes only when its value equals ``expected``
    (``None`` = unknown → nothing outside the block goes). Returns
    ``(removed, left)`` — key names, sorted. Kept lines are byte-for-byte
    (CRLF included), the file keeps its mode, and nothing is written when
    nothing was VCO's."""
    path = project_folder / CLAUDE_ENV_REL
    if not path.is_file():
        return [], []
    with path.open(encoding="utf-8", newline="") as handle:
        prior = handle.read()
    wanted = set(keys)
    outside, block = _without_managed_block(prior)
    removed = {
        pair[0] for pair in map(parse_env_line, block.splitlines()) if pair and pair[0] in wanted
    }
    left: set[str] = set()
    kept: list[str] = []
    for line in outside.splitlines(keepends=True):
        # The writer's own grammar (it escapes `\` and `"` inside quotes).
        pair = parse_env_line(line, writer_escapes=True)
        if pair is not None and pair[0] in wanted:
            if expected is not None and expected.get(pair[0]) == pair[1]:
                removed.add(pair[0])
                continue
            left.add(pair[0])
        kept.append(line)
    new_text = "".join(kept)
    if new_text != prior:
        atomic_rewrite_text(path, new_text)
    return sorted(removed), sorted(left)


def strip_routing_keys(
    project_folder: Path,
    keys: Iterable[str],
    *,
    project_id: Optional[str],
    db_path: Optional[Path] = None,
    orchestrator_root: Optional[Path] = None,
) -> dict[str, object]:
    """Run the strip on all three surfaces. Returns ``{"removed": {file:
    [KEY]}, "left": {file: [KEY]}, "projection": "resolved"|"unavailable",
    "projection_error": str|None, "errors": [message]}`` — names only. A
    surface that cannot be edited is refused (and recorded in the project's
    deferral ledger), reported in ``errors``; the others still run."""
    keys = sorted(set(keys))
    expected: Optional[dict[str, str]] = None
    projection_error: Optional[str] = None
    if project_id:
        try:
            expected = projected_env(
                project_id, db_path=db_path, orchestrator_root=orchestrator_root,
            )
        except (cp.ProjectNotFound, cp.DbUnreachable, cp.ConfigProjectionError,
                sqlite3.Error, OSError) as exc:
            projection_error = f"{type(exc).__name__}: {exc}"
    else:
        projection_error = "no project id was given"

    removed: dict[str, list[str]] = {}
    left: dict[str, list[str]] = {}
    errors: list[str] = []

    def record(rel: str, outcome: tuple[list[str], list[str]]) -> None:
        if outcome[0]:
            removed[rel] = outcome[0]
        if outcome[1]:
            left[rel] = outcome[1]

    try:
        record(CLAUDE_ENV_REL, strip_claude_env(project_folder, keys, expected))
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{CLAUDE_ENV_REL} left untouched: {exc}")
    for surface, (rel, _env_key) in sorted(cp._JSON_SURFACE_FILES.items()):
        try:
            record(rel, cp.strip_env_keys_holding(project_folder, surface, expected or {}, keys))
        except cp.SettingsWriteRefused as exc:
            errors.append(
                f"{rel} left untouched (env-key strip): "
                + "; ".join(r.sentence() for r in exc.refusals)
            )
        except OSError as exc:
            errors.append(f"{rel} left untouched (env-key strip): {exc}")
    return {
        "removed": removed,
        "left": left,
        "projection": "resolved" if expected is not None else "unavailable",
        "projection_error": projection_error,
        "errors": errors,
    }


def _cli_strip_routing(args: argparse.Namespace) -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
        keys = request.get("keys") if isinstance(request, dict) else None
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ValueError('stdin must be {"keys": ["KEY", ...]}')
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": "bad_request", "message": str(exc)}))
        return 2
    result = strip_routing_keys(
        Path(args.project_folder),
        keys,
        project_id=args.project_id,
        db_path=Path(args.db_path) if args.db_path else None,
        orchestrator_root=Path(args.orchestrator_root) if args.orchestrator_root else None,
    )
    print(json.dumps({"ok": True, **result}))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m vco_lib.unregister_env")
    sub = parser.add_subparsers(dest="cmd", required=True)
    strip = sub.add_parser(
        "strip-routing",
        help="remove VCO's routing keys from .claude/env and the JSON env blocks "
             "(only where the value is VCO's; keys on stdin; names only on stdout)",
    )
    strip.add_argument("--project-folder", required=True)
    strip.add_argument("--project-id", default=None)
    strip.add_argument("--orchestrator-root", default=None)
    strip.add_argument("--db-path", default=None)
    strip.set_defaults(handler=_cli_strip_routing)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
