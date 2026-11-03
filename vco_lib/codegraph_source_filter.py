# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which files a code-graph walk keeps — the ONE source-exclusion decision
(moved out of ``templates/scripts/analyze_code_graph.py`` in v0.2.105, which
keeps thin aliases ``_path_is_excluded`` / ``_keep_source_file`` /
``_is_under_transient_state``).

Shared by the directory walk AND the single-file / drain path
(mirror-don't-fork, v0.2.73): a per-edit ``--only-file`` must never index
something the whole-tree walk skips, or vice versa.

v0.2.105 (silent zero-module walk, Windows field report: 146 source files,
exit 0, zero modules, nothing logged): the walk judged the FULL absolute path
of every file, on the claim that "the walk root is a real ancestor, never a
skip-dir itself". False whenever the project lives under a folder whose NAME
is an ignored segment — ``D:\\build\\proj``, ``~/env/proj``,
``C:\\work\\out\\proj``, ``…\\bin\\…``, a checkout inside a ``worktrees`` /
``.wt`` container — and every file was then excluded. The walk now judges
only the part of the path BELOW its root (:func:`walk_rel_parts`), exactly as
the single-file path always did. Every exclusion also carries its rule
(:func:`exclusion_reason`), so a walk that keeps nothing can say why
(``vco_lib.codegraph_walk_diagnosis``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple


def is_under_transient_state(parts: tuple) -> bool:
    """True if a path lives under ``.claude/state/`` (transient scratch).

    v0.2.73 (READ-amp origin fix). ``.claude/state/`` holds ``tool_backups/``
    snapshot copies + per-session scratch — NEVER first-party source. It must
    be skipped by BOTH the directory walk and the single-file path.

    WHY this is its own predicate and NOT an ignore-dir entry: the ignore set
    is a flat directory-NAME set. Adding a bare ``'state'`` there would wrongly
    skip a legitimate top-level ``state/`` package in a user project. The skip
    must be anchored to the two-segment ``.claude`` → ``state`` sequence, which
    a name-set can't express.

    Origin (live-measured 2026-07-04): on the orchestrator clone
    ``index_dot_claude=True`` made the walk reach
    ``.claude/state/tool_backups/``; ~16k backup-snapshot ``.py`` copies (43% of
    the CodeFunction collection) were indexed as real functions.

    Args:
        parts: ``Path.parts`` of the candidate file (or its ``.resolve()``).

    Returns:
        True when some ``.claude`` segment is immediately followed by ``state``.
    """
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "state":
            return True
    return False


def exclusion_reason(parts: tuple, ignore_dirs: frozenset) -> Optional[str]:
    """WHY ``parts`` is excluded (``None`` = kept). The first matching ignored
    directory segment wins, then the ``.claude/state`` rule.

    Args:
        parts: the path segments to test — the parts BELOW the walk root
            (:func:`walk_rel_parts`; the single-file path passes the
            repo-relative parts). The ``.claude/state`` check works on either.
        ignore_dirs: the directory-NAME set to reject (already merged with
            language extras + unconditional additions by the caller).
    """
    for part in parts:
        if part in ignore_dirs:
            return f"directory '{part}'"
    if is_under_transient_state(parts):
        return "directory '.claude/state'"
    return None


def path_is_excluded(parts: tuple, ignore_dirs: frozenset) -> bool:
    """The yes/no form of :func:`exclusion_reason`."""
    return exclusion_reason(parts, ignore_dirs) is not None


def walk_rel_parts(path: Path, root: Path) -> tuple:
    """``path``'s parts RELATIVE to the walk root (its full parts when it does
    not sit under ``root``). Only the part of a path the walk OWNS is judged —
    see the module docstring for the field defect this closes."""
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def keep_source_file(path: Path, ignore_dirs: frozenset, root: Optional[Path] = None) -> bool:
    """Keep ``path`` iff it is not excluded. ``root`` = the walk root (only
    the parts below it are judged); without one the full parts are tested,
    for callers that pass an already-relative path."""
    parts = walk_rel_parts(path, root) if root is not None else path.parts
    return not path_is_excluded(parts, ignore_dirs)


def classify_finder_files(
    root: Path,
    extensions: Sequence[str],
    name_skip_suffixes: Optional[Sequence[str]],
    ignore_dirs: frozenset,
) -> Iterator[Tuple[Path, Optional[str]]]:
    """Every file the ``extensions`` globs match under ``root``, paired with
    the rule that EXCLUDES it (``None`` = walked).

    The ONE per-file decision behind the analyzer's finders and the
    empty-walk census — they cannot disagree. ``name_skip_suffixes`` (JS/TS,
    config) drops bundle/minified names and ``vite.config*``.
    """
    for ext in extensions:
        for f in root.rglob(ext):
            why = exclusion_reason(walk_rel_parts(f, root), ignore_dirs)
            if why is None and name_skip_suffixes is not None:
                if any(f.name.endswith(s) for s in name_skip_suffixes):
                    why = "generated/bundled file name"
                elif f.name.startswith("vite.config"):
                    why = "build-config file name"
            yield f, why


__all__ = [
    "classify_finder_files", "exclusion_reason", "is_under_transient_state",
    "keep_source_file", "path_is_excluded", "walk_rel_parts",
]
