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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Set, Tuple

from vco_lib.codegraph_resync import primary_sources_for
from vco_lib.codegraph_row_classify import source_is_owned

__all__ = [
    "PruneScope",
    "preserve_keys",
    "prune_scope_for_walk",
    "project_has_registered_extras",
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


# ── v0.2.105 (prune lane): the --prune-stale source SCOPE ──────────────────
#
# The 2026-10-29 incident: a primary-only `--prune-stale` walk (the
# `codegraph_orphan_interactions_pending` remedy) pruned 33,464 rows of an
# ENABLED extra code folder — 727 orphans were expected — because the stale
# set was judged over EVERY row of the prefix, not only the walked root's.
# Nothing re-added the rows (the extras refresh is commit-diff based), so the
# extra folder's graph was silently gone. The scope below is the ONE home of
# the rule that fixes it; the analyzer passes it through to
# ``vco_lib.codegraph_prune.prune_collection``.


@dataclass(frozen=True)
class PruneScope:
    """Which rows a ``--prune-stale`` pass may judge, by ``project_source``."""

    #: The owned source forms of every root this walk covered (the primary
    #: root, plus this run's ``--extra-path`` roots).
    own_sources: Set[str]
    #: True when legacy unstamped rows must be preserved: the project HAS a
    #: registered extra code folder, so an empty ``project_source`` cannot be
    #: attributed to the primary root.
    legacy_unowned: bool
    #: ``--as-extra-path``: the walked root IS an extra folder, so an
    #: unstamped row is most likely the primary project's and is never judged.
    strict: bool
    #: The walked primary root, POSIX (for the run's log line).
    primary_root: str
    #: How many extra roots this run walked (for the run's log line).
    walked_extras: int = 0

    def summary(self) -> str:
        """One human-readable line naming what the prune is scoped to."""
        extra = f" (+{self.walked_extras} extra root(s) walked this run)" if self.walked_extras else ""
        return f"{self.primary_root}{extra}"


def project_has_registered_extras(project_root: Any) -> bool:
    """Does THIS project have any registered extra code folder (enabled or
    disabled)? Resolved through the ONE config resolver (hub-first).

    Soft-fail False (hub down / unregistered project): a hub outage is
    transient, and a ``--prune-stale`` walk only ever runs behind an explicit
    user-consented command — the alternative (fail toward preservation on
    every hub-less machine, which can never have extras at all) would keep
    primary legacy rows stale forever for no reason.
    """
    root = str(project_root or "").strip()
    if not root:
        return False
    try:
        from vco_lib import project_config

        cfg = project_config.resolve(root)
        return bool(getattr(cfg, "code_graph_extra_paths", ()))
    except Exception:  # noqa: BLE001 — hub down / unregistered project
        return False


def prune_scope_for_walk(
    primary_root: Any,
    walked_extras: "Iterable[Any]" = (),
    *,
    strict_source: bool = False,
) -> Optional[PruneScope]:
    """The ``--prune-stale`` scope for one walk (v0.2.105 prune lane).

    ``primary_root`` is the walked root's RAW POSIX form (rows are stamped
    with it); ``walked_extras`` are the extra roots THIS run also walks
    (``--extra-path`` argv — their rows are in the visited set and their
    sources join the judged set); ``strict_source`` marks an
    ``--as-extra-path`` walk (the walked root IS an extra). Returns ``None``
    when no root is known — a direct caller without a root keeps the
    previous unscoped semantics.

    The legacy-row rule: an unstamped row (no ``project_source``) is
    preserved (``legacy_unowned``) when the project has ANY registered extra
    folder — attribution is unknowable — and judged as the primary root's
    when it has none.
    """
    raw = str(primary_root or "").strip()
    if not raw:
        return None
    own: Set[str] = set(walk_own_sources(raw, raw))
    extras = list(walked_extras or ())
    for extra in extras:
        own |= walk_own_sources(extra, str(extra))
    return PruneScope(
        own_sources=own,
        legacy_unowned=(
            not strict_source and project_has_registered_extras(raw)
        ),
        strict=strict_source,
        primary_root=raw,
        walked_extras=len(extras),
    )
