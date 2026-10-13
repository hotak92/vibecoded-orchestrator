# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which source root a code-graph walk stamps and which rows it owns (v0.2.102 W6-CGD).

Every code-graph row carries ``project_source``: the absolute POSIX root it was
indexed from (the project's own folder, or one of its extra code folders). The
deterministic row UUID mixes that root in (V52-O.3), so the project's copy of
a file and an extra folder's copy of the same file are two separate row sets.
This module is the ONE home for the three per-walk decisions that must agree
with that keying:

* :func:`walk_source_stamp` — the ``project_source`` a walk writes. An extra
  folder's rows always carry the registered extra root, whoever runs the walk
  (the per-edit hook, the panel's Sync button, the automatic refresh, the
  queued Stop drain). A worktree canonical root (``--canonical-source``) is
  honoured only for the project's OWN scoped edits. Before this rule the
  per-edit hook stamped a worktree extra with its git main root while every
  other producer stamped the extra path, so one file got two identities (D-2).
* :func:`row_owned_by_walk` — does a stored row belong to the root being
  walked? The unchanged-file gate (``_get_existing_module``) skips a file only
  when the walking root's OWN row is current. It used to accept any root's row
  with the same path and hash, so a project file identical to an extra
  folder's already-indexed copy never got project rows, and removing the
  extra folder later deleted the only copy (D-1).
* :func:`preserve_keys` — the ``(source, path)`` keys the ``--prune-stale``
  preserve set records for a file skipped in one root. Keyed by path alone, a
  copy skipped in one tree shielded its twin in the other (D-3).
* :func:`stale_hit` — does the per-run stale-file set (``(source, path)``
  keys) owe the walking root a re-walk of this file? Keyed by path alone, a
  stale row of one root made the other root re-walk its copy (R11-02).

Duplicates between the project and an extra folder are EXPECTED after these
fixes: each root owns its own rows. Recognising a duplicate on purpose is the
v0.2.103 de-duplication design (an explicit per-file decision with a record on
the canonical row), never a side effect of the gate.

The ownership rule itself is :func:`vco_lib.codegraph_row_classify.source_is_owned`
(the rule every source-judged delete already uses) and the root's raw +
resolved forms come from :func:`vco_lib.codegraph_resync.primary_sources_for`;
this module only composes them, so the gate, the prune and the deletes cannot
drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional, Set, Tuple

from vco_lib.codegraph_resync import primary_sources_for
from vco_lib.codegraph_row_classify import source_is_owned

__all__ = [
    "preserve_keys",
    "refused_extra_keep_sources",
    "row_owned_by_walk",
    "stale_hit",
    "walk_own_sources",
    "walk_source_stamp",
]


def walk_source_stamp(
    source_root: str,
    *,
    canonical_source: Optional[str] = None,
    scoped: bool = False,
    extra_root: bool = False,
) -> str:
    """The ``project_source`` value a walk of ``source_root`` writes.

    ``source_root`` is the walked root's POSIX form (``Path.as_posix()``).
    ``extra_root`` is True when that root is an extra code folder: either the
    analyzer was started with ``--as-extra-path`` (the root IS the extra) or
    the root is an ``--extra-path`` of a combined walk. An extra root always
    stamps itself — the registered path every other extra producer stamps —
    and ignores ``canonical_source``. Otherwise ``canonical_source`` (the
    edited file's git main root, passed by the per-edit hook and the primary
    Stop drain) wins for a ``scoped`` (``--only-file`` / ``--only-files-from``)
    run, so a worktree edit of the project converges on the main checkout's
    rows. Everything else stamps ``source_root``.
    """
    if extra_root:
        return source_root
    if scoped and canonical_source:
        return canonical_source
    return source_root


def walk_own_sources(source_root: Any, stamp: str) -> Set[str]:
    """The non-empty ``project_source`` values that belong to this walk.

    The root's raw and resolved forms (rows written before a symlink moved
    still belong to it) plus the value this walk stamps (which differs from
    the root only for a canonicalised worktree edit of the project).
    """
    own: Set[str] = set()
    try:
        own |= primary_sources_for(Path(source_root)) or set()
    except Exception:  # noqa: BLE001 — an unresolvable root keeps the stamp only
        pass
    if stamp:
        own.add(stamp)
    return own


def refused_extra_keep_sources(
    extra_root: Any,
    extra_stamp: str,
    project_root: Any,
    *,
    project_stamp: str,
) -> Set[str]:
    """The sources whose rows ``--prune-stale`` must keep for a REFUSED extra.

    An extra folder the overlap rule refuses is not walked, so its rows are
    kept rather than judged (W6-CGD D-4). But a refused extra can resolve to
    the project folder itself (a moved or symlinked ``folder_path`` landing on
    a registered extra, or a hand-run ``--extra-path <repo>``): its source
    forms are then the PROJECT's, and keeping them would exempt every project
    row from the prune the user asked for, silently (v0.2.102 R11-01). The
    project's own sources — its raw and resolved forms plus the stamp this run
    writes for it — are therefore never part of the keep set: the project root
    IS walked this run, so its rows are judged by the visit, as always.
    """
    return walk_own_sources(extra_root, extra_stamp) - walk_own_sources(project_root, project_stamp)


def row_owned_by_walk(src: Any, own_sources: Optional[Iterable[str]], *, extra_root: bool) -> bool:
    """Does a stored row (``project_source`` = ``src``) belong to this walk?

    A non-empty source is owned iff it is one of ``own_sources``. An empty
    source (legacy rows written before v0.2.47 stamped the field) belongs to
    the project's own root and never to an extra folder — the same rule as
    :func:`~vco_lib.codegraph_row_classify.source_is_owned` with ``strict``
    set for extra roots.
    """
    own = set(own_sources) if own_sources is not None else None
    return source_is_owned(src, own, strict=extra_root)


def stale_hit(
    stale_set: Optional[Iterable[Any]],
    rel: str,
    walk_own: Optional[Tuple[Iterable[str], bool]] = None,
) -> bool:
    """Does the stale-file set owe THIS walk a re-walk of ``rel``? (v0.2.102 R11-02)

    ``stale_set`` holds ``(project_source, path)`` keys — one per stale row,
    source ``""`` for a legacy unstamped row — built by the analyzer's
    ``_build_stale_file_set``. ``walk_own`` is the walk's ``(own sources,
    extra_root)`` context (the analyzer's ``_walk_own``). A key counts when its
    source is one this walk owns: the same rule as :func:`row_owned_by_walk`
    (a non-empty source in ``own``; ``""`` for the project's root only),
    applied by exact key lookup so the per-file gate stays O(own forms).

    A bare ``str`` entry still means "this path under any source" (callers
    that cannot name the root), and with no walk context (a direct caller:
    no root is known) any source counts, as before.
    """
    if not stale_set:
        return False
    if rel in stale_set:
        return True
    if walk_own is None:
        return any(isinstance(e, tuple) and len(e) == 2 and e[1] == rel for e in stale_set)
    own, extra_root = walk_own
    if any((s, rel) in stale_set for s in own if s):
        return True
    return not extra_root and ("", rel) in stale_set


def preserve_keys(own_sources: Iterable[str], rel: str, *, extra_root: bool) -> Set[Tuple[str, str]]:
    """``(source, path)`` keys under which ``--prune-stale`` preserves the rows of
    a file the walk found on disk but did not re-write (unchanged / parse
    failure). One key per owned source form, plus ``("", rel)`` for the
    project's own root (legacy unstamped rows). A row whose ``(source, path)``
    is not among them — the other tree's copy of the same path — is judged
    on its own.
    """
    keys = {(s, rel) for s in own_sources if s}
    if not extra_root:
        keys.add(("", rel))
    return keys
