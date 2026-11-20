# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Revision heal: re-extract ONLY the files whose code-graph rows are owed (v0.2.107, CG-3).

Before this, a revision crossing (``CODEGRAPH_EMBED_REVISION`` bumped, or rows
demoted to the vectorless sentinel by the exposure heal / the vectorless
audit) was healed by a WHOLE-REPO walk: every file hashed and gated, the
deleted-file sweep, the cross-reference pass over the whole graph. The resync
driver (:func:`vco_lib.codegraph_resync.run_resync_and_verify`) now pages the
owed rows instead and hands only their files to ONE scoped analyzer run
(``--only-files-from <list> --revision-heal``). That run is the driver's walk,
not a second scheduler: same driver, same machine slot, same per-project walk
claim, same post-walk verifier and ledger entry.

How the files are chosen
------------------------
1. **The owed rows, by cursor.** The driver's pre-walk owed count
   (:func:`vco_lib.codegraph_resync.count_stale_rows`, the ONE classifier
   ``classify_row`` == ``"owed"``) collects each owed row's stored path while
   it counts. It pages every file-anchored class through
   :func:`vco_lib.codegraph_cursor.iter_class` with three properties (revision,
   path, ``project_source``) — never a filtered Aggregate, never a Like filter,
   never a whole-row read. A class it cannot read makes the count ``None``.
2. **Files the graph may take from this checkout.** The graph follows the MAIN
   checkout's commits (CG-3): a linked worktree heals nothing, and in a git
   checkout a file is re-extracted only when it is in HEAD's tree AND has no
   uncommitted change — the commit that carries an edit brings its file in
   (the catch-up's scoped run re-walks it, its rows being stale). A path
   outside the root or missing from disk is left alone.

What it never does
------------------
On any doubt — the count or the listing undeterminable, git unable to say what
is committed, nothing eligible, the list file unwritable — no analyzer runs:
nothing is changed and nothing is deleted, and the driver's verifier keeps the
heal owed (``codegraph_embed_resync_pending``). Inside the run the analyzer
replaces each row in place by its deterministic uuid and deletes a file's
leftover rows (entities gone, a shrunk chunk tail) only after every write of
that file succeeded (``reconcile_walked_file_rows``); ``--revision-heal`` also
keeps the rows of a listed file that vanished before the run read it, and its
per-file gate bypass means the scoped run builds no stale-file set (so no
orphan-clear sweeps a class from it).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger("vco_lib.codegraph_revision_heal")

#: :class:`HealPlan` verdicts.
HEAL = "heal"  # files listed — run the scoped analyzer
NOTHING_OWED = "nothing_owed"  # positive zero: no owed row at all
NONE_ELIGIBLE = "none_eligible"  # owed rows, but no file may be re-extracted now
UNDETERMINABLE = "undeterminable"  # count / listing / git could not answer
WORKTREE = "worktree"  # a linked worktree: never indexed

#: Why an owed file was left owed (``HealPlan.held`` keys).
HELD_UNCOMMITTED = "uncommitted"
HELD_MISSING = "missing"
HELD_OUTSIDE_ROOT = "outside_root"

#: Where the driver writes the scoped run's file list (under the project's
#: git-excluded ``.claude/state``); removed once the run returns.
LIST_FILE_NAME = "codegraph_revision_heal_files.txt"


@dataclass
class HealPlan:
    """What the driver's revision heal will do — see the module docstring."""

    verdict: str
    #: Per-class owed row counts (the pre-walk baseline); None = unknown.
    counts: Optional[dict] = None
    #: Absolute paths the scoped run re-extracts (verdict ``heal`` only).
    files: list = field(default_factory=list)
    #: ``{reason: number of files}`` left owed on purpose.
    held: dict = field(default_factory=dict)
    reason: str = ""

    @property
    def runs_analyzer(self) -> bool:
        return self.verdict == HEAL

    def describe(self) -> str:
        """One log line: verdict, rows, files, held files, reason."""
        rows = "?" if self.counts is None else str(sum(self.counts.values()))
        held = ", ".join(f"{k}={v}" for k, v in sorted(self.held.items()) if v)
        parts = [f"verdict={self.verdict}", f"owed_rows={rows}",
                 f"files={len(self.files)}"]
        if held:
            parts.append(f"held: {held}")
        if self.reason:
            parts.append(self.reason)
        return " ".join(parts)


def select_files(
    repo_root: Path,
    stored_paths: Iterable[str],
    *,
    committed: Optional[Callable[[Path], "tuple[str, Optional[set]]"]] = None,
    uncommitted: Optional[Callable[[Path], Optional[set]]] = None,
) -> "tuple[Optional[list], dict, str]":
    """``(absolute files to re-extract, held counts, reason)``.

    ``None`` files = git could not tell which files are committed or changed:
    the caller heals nothing. ``committed`` / ``uncommitted`` are test seams
    (defaults: :func:`vco_lib.codegraph_path_reconcile.committed_tree`,
    :func:`vco_lib.git_meta.uncommitted_paths`).
    """
    from vco_lib import codegraph_path_reconcile as _recon
    from vco_lib.git_meta import uncommitted_paths

    root = Path(repo_root)
    verdict, tracked = (committed or _recon.committed_tree)(root)
    if verdict == _recon.GIT_UNKNOWN:
        return None, {}, "git could not list the committed files"
    changed: set = set()
    if verdict != _recon.NOT_GIT:
        listed = (uncommitted or uncommitted_paths)(root)
        if listed is None or tracked is None:
            return None, {}, "git could not list the uncommitted changes"
        changed = set(listed)
    files: list = []
    held: dict = {}
    for raw in sorted({str(p) for p in stored_paths if p}):
        rel = _recon.rel_posix(raw, root)
        if not rel:
            held[HELD_OUTSIDE_ROOT] = held.get(HELD_OUTSIDE_ROOT, 0) + 1
            continue
        if verdict != _recon.NOT_GIT and (rel not in (tracked or ()) or rel in changed):
            held[HELD_UNCOMMITTED] = held.get(HELD_UNCOMMITTED, 0) + 1
            continue
        candidate = root / rel
        try:
            present = candidate.is_file()
        except OSError:
            present = False
        if not present:
            held[HELD_MISSING] = held.get(HELD_MISSING, 0) + 1
            continue
        files.append(str(candidate))
    return files, held, ""


def plan_revision_heal(
    repo_root: Path,
    count_owed: Callable[[set], Optional[dict]],
    *,
    linked_worktree: Optional[Callable[[Path], Optional[Path]]] = None,
    select: Callable[..., "tuple[Optional[list], dict, str]"] = select_files,
) -> HealPlan:
    """Decide the driver's revision heal for ``repo_root``.

    ``count_owed(paths)`` is the driver's owed count: it returns the per-class
    counts (None = undeterminable) and adds each owed row's stored path to
    ``paths``. ``linked_worktree`` defaults to
    :func:`vco_lib.git_meta.linked_worktree_main`. Never raises.
    """
    from vco_lib.git_meta import linked_worktree_main

    root = Path(repo_root)
    try:
        main_checkout = (linked_worktree or linked_worktree_main)(root)
    except Exception as exc:  # noqa: BLE001 — cannot tell → heal nothing
        return HealPlan(UNDETERMINABLE, reason=f"worktree check failed: {exc}")
    if main_checkout is not None:
        return HealPlan(
            WORKTREE,
            reason=f"linked worktree of {main_checkout} — worktrees are never indexed",
        )
    paths: set = set()
    try:
        counts = count_owed(paths)
    except Exception as exc:  # noqa: BLE001 — an unreadable class is no verdict
        logger.warning("revision heal: owed count raised: %s", exc)
        counts = None
    if counts is None:
        return HealPlan(UNDETERMINABLE, reason="the owed rows could not be counted and listed")
    if sum(counts.values()) == 0:
        return HealPlan(NOTHING_OWED, counts=dict(counts))
    if not paths:
        return HealPlan(
            UNDETERMINABLE, counts=dict(counts),
            reason="owed rows were counted but their files were not listed",
        )
    try:
        files, held, why = select(root, paths)
    except Exception as exc:  # noqa: BLE001 — cannot tell → heal nothing
        files, held, why = None, {}, f"file selection failed: {exc}"
    if files is None:
        return HealPlan(UNDETERMINABLE, counts=dict(counts), reason=why)
    if not files:
        return HealPlan(
            NONE_ELIGIBLE, counts=dict(counts), held=held,
            reason="no owed file can be re-extracted from this checkout now",
        )
    return HealPlan(HEAL, counts=dict(counts), files=files, held=held)


def write_list_file(repo_root: Path, files: Iterable[str]) -> Optional[Path]:
    """Write the scoped run's list (one absolute path per line) under
    ``<repo_root>/.claude/state/``; None when it cannot be written."""
    target = Path(repo_root) / ".claude" / "state" / LIST_FILE_NAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(f"{f}\n" for f in files), encoding="utf-8")
    except OSError as exc:
        logger.warning("revision heal: cannot write %s: %s", target, exc)
        return None
    return target


def drop_list_file(path: Optional[Path]) -> None:
    """Remove the list file once the run returned (soft-fail)."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("revision heal: cannot remove %s: %s", path, exc)
