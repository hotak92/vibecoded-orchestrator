# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``<project>/.claude/backups/`` is never committable — the ONE home.

VCO writes copies of user bytes there before it replaces them: the bundle
engine's adoption backups (``bundle-adoptions/<ts>/``,
:mod:`vco_lib.bundle_backup`) and the post-commit KG reviewer's guard backups
(``kg-reviewer/<ts>/``, :mod:`vco_lib.kg_reviewer_guard`). ``.claude/`` is
git-excluded only when a project was added with Safe add, so in any other
project a plain ``git add -A`` / ``git commit -a`` would sweep those backups
— the user's own earlier bytes, or bytes a reviewer wrote — into an
unrelated commit (review R11 A1). Every writer therefore calls
:func:`ensure_backups_dir` before writing: it makes the directory
self-ignoring with a ``.gitignore`` holding ``*`` (which also ignores the
``.gitignore`` itself), so nothing below it is ever staged, whatever the
project's own ignore rules say.

An existing ``.gitignore`` there is left untouched (a user who wants backups
tracked can say so); creating it is the only write.
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["BACKUPS_REL", "IGNORE_ALL", "ensure_backups_dir"]

BACKUPS_REL = Path(".claude") / "backups"
IGNORE_ALL = "*\n"


def ensure_backups_dir(project_root: Path) -> Path:
    """Create ``<project_root>/.claude/backups/`` with its self-ignoring
    ``.gitignore`` and return the directory. Raises ``OSError`` when either
    cannot be written — callers treat that as "no backup", never as a reason
    to replace bytes without one."""
    backups = Path(project_root) / BACKUPS_REL
    backups.mkdir(parents=True, exist_ok=True)
    ignore = backups / ".gitignore"
    try:
        with open(ignore, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(IGNORE_ALL)
    except FileExistsError:
        pass
    return backups
