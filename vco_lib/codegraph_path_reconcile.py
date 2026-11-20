# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Daily path-existence reconciliation of a project's code graph (v0.2.107 CG-3, A-013).

Why: rows of code deleted OUTSIDE any walked file used to survive. The
deleted-file sweep ran only on whole-repo walks, the HEAD catch-up removes
only the files of a diffed commit range, and a folder deleted before the
catch-up existed (a live example: ≈227k function rows of a folder removed
months earlier) was never part of any range. Nothing on the ordinary path
removed those rows, and every search kept serving them.

What: at most once a day per project (:data:`INTERVAL_SECONDS`), the HEAD
catch-up (:mod:`vco_lib.codegraph_head_catchup`) calls :func:`reconcile_if_due`.
It needs no walk and no embedding:

1. list every row of the three file-anchored classes (``CodeModule.path``,
   ``CodeClass.file_path``, ``CodeFunction.file_path``) through the ONE cursor
   (:func:`vco_lib.codegraph_cursor.iter_class`) with two properties — the
   path and ``project_source`` — plus the uuid every object carries;
2. keep the rows the PRIMARY root owns (``project_source`` empty or one of the
   root's forms; rows of extra code folders are never judged here);
3. ``stat`` each distinct path once (:func:`path_reachable_on_disk`, fail-safe
   toward keeping: only ``ENOENT``/``ENOTDIR`` under a non-empty root is
   "absent");
4. delete, by id, the rows whose path is determinately absent AND, when the
   root is a git work tree, is not in HEAD's committed tree
   (:func:`vco_lib.git_meta.committed_paths`, one ``git ls-tree`` per run).
   The graph follows the main checkout's COMMITS: a tracked file that is
   missing only from the working tree (deleted or moved, not committed) keeps
   its rows — nothing would restore them when the file comes back without a
   HEAD move (WAVE2-CHECK L-1). Its rows go when the commit that deletes it
   is caught up.

The guards that make a delete possible at all (each one tested both ways):

* the root directory exists and holds at least one entry
  (:func:`root_holds_entries`) — an unmounted drive, a missing folder or an
  empty checkout deletes NOTHING (``root_missing``);
* at least one listed path still exists — when EVERY path is absent the run
  deletes NOTHING (``all_paths_missing``): that is a moved or replaced root,
  not deleted code;
* a class whose listing is undeterminable (:class:`CursorUndeterminable`)
  authorises no delete in that class;
* a git work tree whose committed tree cannot be read (git cannot run, no
  HEAD, an error) deletes NOTHING (``git_unknown``). A folder that is not a
  git work tree keeps the disk-only rule.

The root is the PROJECT folder the analyzer walked, never the toplevel of a
repository it is nested in: the rows' paths are relative to that folder
(WAVE2-CHECK M-2). Rows whose ``project_source`` is a different spelling of
the root (a symlinked path) are not owned by :func:`primary_sources` and are
never judged — the safe direction (N-2).

The run holds a free machine-wide bulk slot (:func:`vco_lib.atomic.try_machine_slot`)
and the per-checkout analyzer lock (:mod:`vco_lib.codegraph_root_lock`); when
either is taken it skips WITHOUT stamping, so the next catch-up retries. A
completed run (any verdict) stamps ``<state_dir>/codegraph_path_reconcile.ts``.
A run that could not reach Weaviate stamps nothing.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from vco_lib.codegraph_row_classify import (
    path_reachable_on_disk,
    root_holds_entries,
    source_is_owned,
)

#: One reconciliation per project per day.
INTERVAL_SECONDS = 24 * 3600
#: The per-project stamp (epoch seconds of the last completed run).
STAMP_NAME = "codegraph_path_reconcile.ts"
#: The three file-anchored classes and the property holding each row's path.
FILE_ANCHORED: "tuple[tuple[str, str], ...]" = (
    ("CodeModule", "path"),
    ("CodeClass", "file_path"),
    ("CodeFunction", "file_path"),
)


@dataclass
class ReconcileResult:
    """What one reconciliation saw and did."""

    outcome: str
    rows_listed: int = 0
    distinct_paths: int = 0
    absent_paths: int = 0
    deleted: int = 0
    failures: int = 0
    #: Absent paths kept because HEAD's tree still has them (L-1).
    kept_committed: int = 0
    unreadable: "list[str]" = field(default_factory=list)

    def line(self) -> str:
        extra = f" unreadable={','.join(self.unreadable)}" if self.unreadable else ""
        return (
            f"path-reconcile: outcome={self.outcome} rows={self.rows_listed} "
            f"paths={self.distinct_paths} absent={self.absent_paths} "
            f"kept_committed={self.kept_committed} "
            f"deleted={self.deleted} failures={self.failures}{extra}"
        )


#: :func:`committed_tree` verdicts.
NOT_GIT = "not_git"
GIT_UNKNOWN = "git_unknown"


def committed_tree(root: Path) -> "tuple[str, Optional[set[str]]]":
    """``(verdict, paths)`` for ``root``: ``("git", {posix paths of HEAD's
    tree under root})``, ``(NOT_GIT, None)`` when git ran and says ``root`` is
    not in a work tree, ``(GIT_UNKNOWN, None)`` when it cannot tell."""
    from vco_lib.git_meta import committed_paths, is_git_work_tree

    inside = is_git_work_tree(root)
    if inside is False:
        return NOT_GIT, None
    if inside is None:
        return GIT_UNKNOWN, None
    paths = committed_paths(root)
    if paths is None:
        return GIT_UNKNOWN, None
    return "git", paths


def _rel_posix(raw: str, root: Path) -> Optional[str]:
    """``raw`` (as stored: relative to the root, or absolute) as a normalised
    POSIX path relative to ``root``; None when it lies outside the root."""
    text = raw.replace("\\", "/")
    if os.path.isabs(text):
        for base in (root, root.resolve()):
            try:
                return Path(os.path.normpath(text)).relative_to(base).as_posix()
            except ValueError:
                continue
        return None
    norm = os.path.normpath(text).replace("\\", "/")
    return None if norm.startswith("../") or norm == ".." else norm


def primary_sources(repo_root: Path) -> "set[str]":
    """The primary root's ``project_source`` forms (raw + resolved)."""
    out = {Path(repo_root).as_posix()}
    try:
        out.add(Path(repo_root).resolve().as_posix())
    except (OSError, RuntimeError):
        pass
    return out


def reconcile_collections(
    collections: "Sequence[tuple[Any, str]]",
    repo_root: Path,
    *,
    reachable: Optional[Callable[[str], bool]] = None,
    committed: Callable[[Path], "tuple[str, Optional[set[str]]]"] = committed_tree,
) -> ReconcileResult:
    """Reconcile ``[(collection, path_prop), …]`` against ``repo_root``.

    ``reachable`` is a test seam (default: :func:`path_reachable_on_disk`,
    memoised per path); ``committed`` another (default :func:`committed_tree`).
    See the module docstring for the guards.
    """
    from vco_lib.codegraph_cursor import CursorUndeterminable, iter_class

    root = Path(repo_root)
    if not root_holds_entries(root):
        return ReconcileResult("root_missing")
    verdict, tracked = committed(root)
    if verdict == GIT_UNKNOWN:
        return ReconcileResult(GIT_UNKNOWN)
    sources = primary_sources(root)
    probe = reachable or (lambda p: path_reachable_on_disk(p, root))
    memo: "dict[str, bool]" = {}

    def _exists(path: str) -> bool:
        if path not in memo:
            memo[path] = bool(probe(path))
        return memo[path]

    result = ReconcileResult("ok")
    plan: "list[tuple[Any, list[tuple[str, str]]]]" = []
    for coll, path_prop in collections:
        rows: "list[tuple[str, str]]" = []
        try:
            for obj in iter_class(
                coll, "codegraph_path_reconcile",
                return_properties=[path_prop, "project_source"],
            ):
                props = getattr(obj, "properties", None) or {}
                raw = str(props.get(path_prop) or "")
                result.rows_listed += 1
                if not raw or not source_is_owned(props.get("project_source"), sources):
                    continue
                rows.append((str(obj.uuid), raw))
        except CursorUndeterminable:
            result.unreadable.append(str(getattr(coll, "name", "?")))
            continue
        plan.append((coll, rows))

    for _coll, rows in plan:
        for _uid, raw in rows:
            _exists(raw)
    result.distinct_paths = len(memo)
    result.absent_paths = sum(1 for v in memo.values() if not v)
    if result.absent_paths and result.absent_paths == result.distinct_paths:
        result.outcome = "all_paths_missing"
        return result
    if result.unreadable:
        result.outcome = "partial"

    in_head: "dict[str, bool]" = {}
    if tracked is not None:
        for raw, present in memo.items():
            if not present:
                rel = _rel_posix(raw, root)
                in_head[raw] = rel is not None and rel in tracked
        result.kept_committed = sum(1 for v in in_head.values() if v)

    for coll, rows in plan:
        for uid, raw in rows:
            if memo.get(raw, True) or in_head.get(raw, False):
                continue
            try:
                coll.data.delete_by_id(uuid=uid)
                result.deleted += 1
            except Exception:  # noqa: BLE001 — one failed row never wedges the run
                result.failures += 1
    return result


def due(state_dir: Path, now: float) -> bool:
    """True when no completed run is recorded within :data:`INTERVAL_SECONDS`.
    A stamp in the future reads as no stamp."""
    try:
        last = float((state_dir / STAMP_NAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return last > now or now - last >= INTERVAL_SECONDS


def _stamp(state_dir: Path, now: float) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / STAMP_NAME).write_text(str(int(now)), encoding="utf-8")
    except OSError:
        pass


def _open_collections(prefix: str) -> "tuple[Any, list[tuple[Any, str]]]":
    """Connect and return ``(client, [(collection, path_prop), …])`` for the
    classes of ``prefix`` that exist."""
    from vco_lib.weaviate_helpers import connect_v4

    client = connect_v4()
    out = []
    for base, prop in FILE_ANCHORED:
        name = f"{prefix}_{base}"
        if client.collections.exists(name):
            out.append((client.collections.get(name), prop))
    return client, out


def reconcile_if_due(
    project_root: str,
    prefix: str,
    state_dir: Path,
    log: Callable[[str], None],
    *,
    now: Callable[[], float] = time.time,
    open_collections: Callable[[str], "tuple[Any, Iterable[tuple[Any, str]]]"] = _open_collections,
    slot: Optional[Callable[[], Any]] = None,
) -> str:
    """Run the reconciliation for ``project_root`` when it is due. Returns the
    outcome (``not_due``, ``slot_busy``, ``busy``, ``unreachable`` or a
    :class:`ReconcileResult` outcome). Never raises."""
    from vco_lib import codegraph_root_lock as root_lock

    t = now()
    if not due(state_dir, t):
        return "not_due"
    def _machine_slot() -> Any:
        from vco_lib.atomic import bulk_slot_dir, machine_slot_count, try_machine_slot

        return try_machine_slot(bulk_slot_dir(), slots=machine_slot_count())

    take_slot = slot if slot is not None else _machine_slot
    with take_slot() as held:
        if held is None:
            log("path-reconcile: every machine-wide bulk slot is busy; retried at the next catch-up")
            return "slot_busy"
        lock = root_lock.acquire(state_dir, root_lock.path_key(str(Path(project_root).resolve())), log=log)
        if lock is None:
            log("path-reconcile: another analyzer run holds the checkout lock; retried at the next catch-up")
            return "busy"
        client = None
        try:
            try:
                client, collections = open_collections(prefix)
            except Exception as exc:  # noqa: BLE001 — Weaviate down: nothing stamped
                log(f"path-reconcile: Weaviate unreachable ({type(exc).__name__}: {exc}); nothing stamped")
                return "unreachable"
            started = time.monotonic()
            res = reconcile_collections(list(collections), Path(project_root))
            _stamp(state_dir, t)
            log(f"{res.line()} s={time.monotonic() - started:.1f} prefix={prefix}")
            return res.outcome
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            root_lock.release(lock)
