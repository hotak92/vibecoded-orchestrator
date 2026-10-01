# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Remove the code-graph entities of deleted files (v0.2.100).

Two callers, one home:

* the analyzer's ``--incremental`` walk (the launcher's extra-path Sync button
  and the automatic extra-path re-index in the Stop drain both run
  ``--incremental --since-commit <last indexed commit>``). The changed-files
  filter only ever ADDS work — a file deleted (or renamed away) since the last
  indexed commit is not in the walk, so its Module / Class / Function / API /
  Interaction rows used to survive and keep answering searches with code that
  no longer exists. :func:`git_deleted_paths` lists those paths from
  ``git diff --name-status -M <since>..HEAD`` (``D`` plus the OLD side of
  ``R``), and :func:`prune_file_rows` deletes their rows;
* the batched Stop-hook drain, whose edited-then-deleted paths are pruned the
  same way (``AdvancedCodeGraphAnalyzer._prune_deleted_file_objects``).

Scoping (never another project's or another source root's rows): every delete
goes through the analyzer's tokenization-safe primitive
(``vco_lib.codegraph_resync.delete_file_rows_exact``), which reads each row's
raw anchor path, ``project`` and ``project_source`` back and compares them in
Python with ``==``. The git path is repo-relative POSIX — the exact shape the
analyzer stamps into ``CodeModule.path`` / ``file_path`` — and the caller
passes the source root's ``as_posix()``, the exact value stamped into
``project_source``. A row whose stored source is a DIFFERENT root (another
extra path, or the primary) is left alone even when its relative path is the
same; so is a legacy row with no ``project_source`` at all.

Conservative on uncertainty: a path still present on disk is never pruned
(re-added, or restored in the working tree), a root without its own ``.git``
or an unknown lower-bound commit prunes nothing (the analyzer falls back to a
full walk there, which owns that case), and a ``git diff`` that fails is
reported as an ERROR so the caller can refuse to advance its commit baseline.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple

#: (analyzer collection attribute, anchor property) for every file-anchored
#: code-graph collection. CodeModule keys the file on ``path``; the rest on
#: ``file_path`` (API + Interaction joined in v0.2.82).
FILE_ANCHORED_COLLECTIONS: Tuple[Tuple[str, str], ...] = (
    ("modules_collection", "path"),
    ("functions_collection", "file_path"),
    ("classes_collection", "file_path"),
    ("apis_collection", "file_path"),
    ("interactions_collection", "file_path"),
)


@dataclass
class DeletedPaths:
    """Result of :func:`git_deleted_paths`. ``error`` non-empty ⇒ the deleted
    set could NOT be computed (``paths`` is then empty and must not be read as
    "nothing was deleted")."""

    paths: list = field(default_factory=list)
    error: str = ""


@dataclass
class PruneReport:
    """What one source root's deleted-file prune did (summed by the analyzer)."""

    files: int = 0
    deleted: int = 0
    failures: int = 0


def _parse_name_status_z(out: str) -> list:
    """Paths that no longer exist at HEAD, from ``--name-status -M -z`` output:
    ``D`` paths and the OLD side of ``R`` (rename). ``C`` (copy) keeps its
    source, so it contributes nothing."""
    tokens = out.split("\0")
    gone: list = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        kind = status[0]
        if kind in ("R", "C"):
            old = tokens[i + 1] if i + 1 < len(tokens) else ""
            if kind == "R" and old:
                gone.append(old)
            i += 3
            continue
        path = tokens[i + 1] if i + 1 < len(tokens) else ""
        if kind == "D" and path:
            gone.append(path)
        i += 2
    return gone


def filter_changed_files(
    repo_path: Path,
    files: list,
    since_commit: Optional[str] = None,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> list:
    """The ``--incremental`` changed-files filter (moved from the analyzer's
    ``_filter_changed_files``, v0.2.47 semantics unchanged): keep only
    ``files`` changed in ``<since_commit or HEAD~1>..HEAD``. Non-git roots and
    unknown SHAs fall back to the full list with one stderr notice — never a
    hard error. Shares its range rule with :func:`git_deleted_paths`, so the
    re-walk set and the delete set always describe the same diff.
    """
    if not (repo_path / ".git").exists():
        print(f"ℹ️  {repo_path} is not a git repository; analyzing all files",
              file=sys.stderr)
        return files
    lhs = "HEAD~1"
    if since_commit:
        rev_check = run(
            ['git', 'rev-parse', '--verify', f'{since_commit}^{{commit}}'],
            cwd=repo_path, capture_output=True, text=True,
        )
        if rev_check.returncode != 0:
            print(f"⚠️  --since-commit {since_commit} not found in {repo_path}; "
                  f"falling back to full scan", file=sys.stderr)
            return files
        lhs = since_commit
    try:
        result = run(['git', 'diff', '--name-only', lhs, 'HEAD'], cwd=repo_path,
                     capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        print("⚠️  Git not available or not a git repo, analyzing all files")
        return files
    changed = {repo_path / line.strip() for line in result.stdout.split('\n') if line.strip()}
    return [f for f in files if f in changed]


def git_deleted_paths(
    source_root: Path,
    since_commit: Optional[str],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> DeletedPaths:
    """Repo-relative POSIX paths deleted or renamed away in
    ``<since_commit or HEAD~1>..HEAD`` under ``source_root`` and absent on disk.

    The range and the ``.git``-at-root gate mirror the analyzer's changed-files
    filter (``_filter_changed_files``), so the delete set and the re-walk set
    always describe the same diff.
    """
    if not (source_root / ".git").exists():
        return DeletedPaths()
    lhs = (since_commit or "").strip() or "HEAD~1"
    try:
        check = run(
            ["git", "rev-parse", "--verify", "--quiet", f"{lhs}^{{commit}}"],
            cwd=source_root, capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DeletedPaths(error=f"git unavailable: {exc}")
    if check.returncode != 0:
        # Unknown lower bound (or a single-commit repo for HEAD~1): the
        # changed-files filter falls back to a FULL walk, whose deleted-file
        # sweep owns this case. Nothing to prune from a diff we cannot take.
        return DeletedPaths()
    try:
        diff = run(
            ["git", "-c", "core.quotepath=off", "diff", "--name-status", "-M",
             "-z", lhs, "HEAD"],
            cwd=source_root, capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DeletedPaths(error=f"git diff failed: {exc}")
    if diff.returncode != 0:
        tail = (diff.stderr or "").strip()[-300:]
        return DeletedPaths(error=f"git diff exit {diff.returncode}: {tail}")
    gone = []
    seen = set()
    for rel in _parse_name_status_z(diff.stdout or ""):
        rel = rel.replace("\\", "/")
        if rel in seen:
            continue
        seen.add(rel)
        try:
            present = (source_root / rel).exists()
        except OSError:
            present = True  # cannot tell → keep the rows
        if not present:
            gone.append(rel)
    return DeletedPaths(paths=sorted(gone))


def prune_file_rows(
    targets: Sequence[Tuple[Any, str]],
    rel_paths: Iterable[str],
    *,
    project: str,
    project_source: str,
    deleter: Callable[..., Tuple[int, int]],
    log_prefix: str,
) -> Tuple[int, int]:
    """Delete every row anchored to one of ``rel_paths`` in each
    ``(collection, anchor_prop)`` of ``targets`` — ONE scan per collection,
    exact-string match on the raw stored path, scoped by ``project`` /
    ``project_source`` (empty = unscoped, the drain's legacy contract).

    Returns ``(deleted, failures)``. A collection whose scan raises counts as
    ONE failure (its rows survived), so the caller never reports a clean
    prune it did not perform.
    """
    wanted = {p for p in rel_paths if p}
    if not wanted:
        return 0, 0
    deleted_total = 0
    failures_total = 0

    def _is_gone(raw_path, _props):
        return raw_path in wanted

    for coll, path_prop in targets:
        if coll is None:
            continue
        try:
            deleted, failures = deleter(
                coll, path_prop, _is_gone,
                project=project or "",
                project_source=project_source or "",
                log_prefix=log_prefix,
            )
        except Exception as exc:  # noqa: BLE001 — one collection never wedges the rest
            print(
                f"⚠️  {log_prefix} failed in {getattr(coll, 'name', '?')}: {exc}",
                file=sys.stderr,
            )
            failures_total += 1
            continue
        deleted_total += deleted
        failures_total += failures
    return deleted_total, failures_total


def prune_git_deleted_files(
    targets: Sequence[Tuple[Any, str]],
    source_root: Path,
    since_commit: Optional[str],
    *,
    project: str,
    deleter: Callable[..., Tuple[int, int]],
    run: Callable[..., Any] = subprocess.run,
) -> PruneReport:
    """The incremental walk's per-source-root step: list the deleted paths and
    prune their rows under ``project_source == source_root.as_posix()``. A
    ``git diff`` that cannot be computed is ONE failure (reported, and it
    blocks a ``--since-commit`` caller from advancing its baseline)."""
    found = git_deleted_paths(source_root, since_commit, run=run)
    if found.error:
        print(
            f"⚠️  deleted-file prune: cannot list files deleted under "
            f"{source_root}: {found.error}",
            file=sys.stderr,
        )
        return PruneReport(failures=1)
    if not found.paths:
        return PruneReport()
    deleted, failures = prune_file_rows(
        targets, found.paths,
        project=project, project_source=source_root.as_posix(),
        deleter=deleter, log_prefix="git-deleted prune",
    )
    print(
        f"   🗑️  {len(found.paths)} file(s) deleted since "
        f"{(since_commit or 'HEAD~1')[:12]} under {source_root}: removed "
        f"{deleted} entit(ies)"
        + (f"; {failures} delete(s) FAILED" if failures else "")
    )
    return PruneReport(files=len(found.paths), deleted=deleted, failures=failures)
