# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Synthetic install roots for materializer tests (v0.2.100, review R18-03).

The materializer checks every COMPOSITE path a template bakes in
(``{{ORCHESTRATOR_ROOT}}/tools/vct-secrets/vct``), not only the root itself.
A test that renders a shipped template into an empty temp directory would
therefore see "missing path" rows no real install has. A real install root IS
a clone of this tree, so :func:`mirror_repo_tree` reproduces it: every tracked
path exists (as an empty file — existence is all the check reads), except the
files an install RENDERS, which the test under way is about to create.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable, List

REPO_ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", ".venv", "node_modules", "target", "__pycache__", "dist"}


def _tracked_paths() -> List[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True,
            check=True, timeout=60,
        ).stdout.decode("utf-8", errors="replace")
        paths = [p for p in out.split("\0") if p]
        if paths:
            return paths
    except (OSError, subprocess.SubprocessError):
        pass
    found: List[str] = []  # no git (tarball checkout): walk the tree instead
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            found.append(Path(dirpath, name).relative_to(REPO_ROOT).as_posix())
    return found


def mirror_repo_tree(dest: Path, *, exclude: Iterable[str] = ()) -> None:
    """Create every tracked repo path under ``dest`` as an EMPTY file.

    The orchestrator-root rendered files (``rendered_root_files.toml``) are
    skipped by default, plus ``exclude`` (POSIX, repo-relative)."""
    from vco_lib import rendered_root_files as rrf

    skip = {p.lower() for p in rrf.rendered_paths()} | {p.lower() for p in exclude}
    for rel in _tracked_paths():
        if rel.lower() in skip:
            continue
        target = dest.joinpath(*rel.split("/"))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.touch()
        except OSError:
            continue  # an unrepresentable name on this OS: not a baked path
