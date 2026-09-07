# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Empty-directory pruning — the ONE home for "remove it only if it is empty".

Two callers needed this rule and they walk the tree differently:

* :func:`prune_now_empty_parents` walks UP from a deleted file's parent,
  bounded by the project folder (orphan deletion during a bundle update).
* ``knowledge_residue._prune_empty_curated_subdirs`` walks DOWN a known set of
  curated subdirs, deepest-first (bundled-knowledge cleanup).

The TRAVERSALS are genuinely different and are deliberately NOT unified — a
shared "walk" abstraction parameterised by direction would be harder to read
than either loop. What IS shared, and what lives here, is the decision each
loop makes at every node: :func:`rmdir_if_empty`.

Extracted from ``vco_lib/project_init.py`` (v0.2.92): that module is
ratchet-capped and must shrink rather than grow, and the ratchet's own failure
message prescribes exactly this — "extract new logic into a vco_lib module and
lower this ceiling."

Safety property both callers rely on: ``rmdir`` succeeds ONLY on an empty
directory, so no user content can be removed by any of this, whatever the
traversal. Every error path leaves the directory alone.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["rmdir_if_empty", "prune_now_empty_parents"]


def rmdir_if_empty(directory: Path) -> bool:
    """Remove ``directory`` iff it is empty. Never raises.

    Returns True only when the directory was empty AND the removal
    succeeded. Every other outcome — not empty, unreadable, removal refused
    — returns False and leaves the directory untouched. "Unreadable" is
    deliberately grouped with "leave alone": an unlistable directory is not
    evidence of emptiness, and guessing in the destructive direction is the
    one mistake this primitive must never make.
    """
    try:
        next(directory.iterdir())
        return False  # non-empty
    except StopIteration:
        pass  # empty — proceed
    except OSError:
        return False  # unreadable → default to safety
    try:
        directory.rmdir()
        return True
    except OSError:
        return False


def prune_now_empty_parents(folder: Path, start: Path) -> "list[str]":
    """Remove now-empty parent directories left by an orphan deletion.

    Walks UP from ``start``, stopping at — and never removing — ``folder``.
    Returns the pruned paths relative to ``folder``.

    Why it exists (v0.2.92 delivery audit, m1): deleting
    ``.claude/skills/<name>/SKILL.md`` when upstream drops the skill left the
    empty ``.claude/skills/<name>/`` behind. Because :func:`rmdir_if_empty`
    can only remove empty directories, a user file anywhere in an ancestor
    stops the walk, so this can never remove user content.

    Best-effort throughout: any per-directory refusal ends the walk silently.
    A prune must never fail an install.
    """
    pruned: list[str] = []
    d = start
    folder_abs = folder.resolve()
    while True:
        try:
            cur = d.resolve()
        except OSError:
            break
        if cur == folder_abs or folder_abs not in cur.parents:
            break  # never remove the target folder (or escape above it)
        if not rmdir_if_empty(cur):
            break  # non-empty, unreadable, or refused — ancestors are fuller
        pruned.append(str(cur.relative_to(folder_abs)))
        d = cur.parent
    return pruned
