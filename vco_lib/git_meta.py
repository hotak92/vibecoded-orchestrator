# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Git HEAD / rev resolution (vco_lib.git_meta — v0.2.53).

Consolidates two sites with different resolution strategies:
* install.py:15445 — file-read ``.git/HEAD`` + ``refs/heads/<branch>``
  (subprocess-less; works pre-venv).
* project_init.py:2610 — subprocess ``git rev-parse``.

Per docs/INSTALL_ARCHITECTURE_v2.md §7.6.

v0.2.53 lands the module with the canonical resolver functions; the
two callsites migrate onto it in v0.2.54.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def resolve_vco_version(orchestrator_root: Path) -> str:
    """Resolve the VCO version string for ``orchestrator_root``.

    Resolution chain:
      1. ``VERSION`` file at repo root (canonical for release tarballs
         where .git/ is absent).
      2. git ``rev-parse --short HEAD`` (development clone).
      3. ``"unknown"`` sentinel (no .git, no VERSION).

    Returns:
        Either a ``"vX.Y.Z"``-style tag string OR a 7-char short SHA OR
        the literal ``"unknown"``.
    """
    version_file = orchestrator_root / "VERSION"
    if version_file.is_file():
        try:
            raw = version_file.read_text(encoding="utf-8", errors="replace").strip()
            if raw:
                return raw if raw.startswith("v") else "v" + raw
        except OSError:
            pass
    sha = git_short_sha(orchestrator_root)
    return sha or "unknown"


def git_short_sha(repo: Path) -> Optional[str]:
    """Return ``git rev-parse --short HEAD`` of ``repo``, or None on error."""
    git_dir = repo / ".git"
    if not git_dir.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def git_branch(repo: Path) -> Optional[str]:
    """Return the current branch name of ``repo``, or None.

    Soft-fails on detached HEAD (returns None) and on subprocess errors.
    """
    git_dir = repo / ".git"
    if not git_dir.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    branch = (result.stdout or "").strip()
    if branch == "HEAD":
        # Detached HEAD — no branch.
        return None
    return branch or None
