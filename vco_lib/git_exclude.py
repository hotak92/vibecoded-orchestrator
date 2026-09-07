# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``.git/info/exclude`` helpers — the ONE home (v0.2.92 W3, §3 item 11).

``.git/info/exclude`` is git's LOCAL-only ignore file: it is never committed,
unlike the tracked ``.gitignore``. Writing VCO-created paths there keeps the
orchestrator's files out of the user's commits without modifying anything the
user's repo tracks. That is why every VCO surface that wants to hide its own
files reaches for this file and never for ``.gitignore``.

Extracted from ``vco_lib/project_init.py`` (where it lived as
``_safe_add_exclude_entries`` + ``_append_git_info_exclude``) when the project
MOVE engine became a third caller: three call sites in two modules is exactly
the "extract before you duplicate" trigger. ``project_init`` now imports these
names; its private aliases remain as thin re-exports so existing tests and any
out-of-tree caller keep working.

The collision-safety rule is the load-bearing part and predates the move:
entries are computed PER-RUN from the files VCO ACTUALLY created, never a
blanket ``/.vscode/`` or ``/knowledge/`` dir-glob — those names are common in
existing projects, and a blanket glob would silently hide the USER's own
same-named files. Only namespaces a user cannot own (``.claude/``,
``.vco-manifest.json``) collapse to a single glob.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from vco_lib.atomic import atomic_write_bytes
from vco_lib.paths import to_posix_rel

__all__ = [
    "SAFE_ADD_SIDECAR_SUFFIX",
    "VCO_EXCLUSIVE_TOPLEVEL",
    "exclude_entries_for_created_paths",
    "safe_add_exclude_entries",
    "append_git_info_exclude",
]


#: Suffix appended to a live file path to form its safe-add reference sidecar
#: (e.g. ``.env`` -> ``.env.vco.reference``). The Rust launcher uses the same
#: string when it writes the ``.env`` reference; kept here so the git-exclude
#: pattern below matches it too.
SAFE_ADD_SIDECAR_SUFFIX = ".vco.reference"

#: Top-level entries that are UNAMBIGUOUSLY VCO's (a user cannot own them) →
#: collapse to a single dir/file glob instead of listing every created file
#: underneath. Everything else is excluded as its SPECIFIC created path.
#:
#: v0.2.63 (C1 fix — "check if files are VCO's or the user's"): a blanket
#: ``/.vscode/`` / ``/infrastructure/`` / ``/knowledge/`` would hide the user's
#: OWN same-named directory, which is the exact opposite of what safe-add is
#: for.
VCO_EXCLUSIVE_TOPLEVEL: dict[str, str] = {
    ".claude": "/.claude/",
    ".vco-manifest.json": "/.vco-manifest.json",
}


def exclude_entries_for_created_paths(
    created: Iterable[str],
    folder: Path,
    *,
    include_manifest: bool = False,
) -> list[str]:
    """Compute ``.git/info/exclude`` entries for the paths VCO just created.

    Args:
        created: Project-relative paths VCO created/overwrote in this run.
            Separator-agnostic — Windows-shaped ``\\`` values are normalised
            through :func:`vco_lib.paths.to_posix_rel`, because git's exclude
            patterns are POSIX-shaped on every OS (a ``\\`` in an exclude
            pattern is git's ESCAPE character, not a separator, so an
            un-normalised Windows path would silently match nothing — the
            v0.2.81 separator lesson applied to a second file format).
        folder: The project folder, probed for the Rust-written
            ``.env.vco.reference`` sidecar.
        include_manifest: Force ``/.vco-manifest.json`` in even when the
            caller's created-list did not enumerate it.

    Returns:
        Deduplicated, order-preserving entries. Anchored with a leading ``/``
        so each matches ONLY that exact path, never a same-named file deeper
        in the tree.
    """
    entries: list[str] = []
    seen: set[str] = set()

    def _add(entry: str) -> None:
        if entry and entry not in seen:
            seen.add(entry)
            entries.append(entry)

    for rel in created:
        rel_posix = to_posix_rel(rel).lstrip("/")
        if not rel_posix:
            continue
        top = rel_posix.split("/", 1)[0]
        glob = VCO_EXCLUSIVE_TOPLEVEL.get(top)
        if glob is not None:
            _add(glob)
        else:
            # A specific VCO-created path (a root file like CLAUDE.md, or a
            # file inside a possibly-user-owned dir like .vscode/ or
            # infrastructure/). Anchored so it matches ONLY this exact path
            # the user did not author.
            _add("/" + rel_posix)

    if include_manifest:
        _add("/.vco-manifest.json")

    # The Rust launcher writes the `.env` reference sidecar before this step.
    sidecar = ".env" + SAFE_ADD_SIDECAR_SUFFIX
    if (folder / sidecar).exists():
        _add("/" + sidecar)
    return entries


def safe_add_exclude_entries(result: dict, folder: Path) -> list[str]:
    """Bundle-envelope adapter over :func:`exclude_entries_for_created_paths`.

    ``result`` is an ``install_project_bundle`` envelope; the created set is
    the union of its ``create`` / ``overwrite`` / ``always-overwrite`` action
    buckets, and ``manifest_written`` forces the manifest entry in.
    """
    actions = result.get("actions", {}) or {}
    created: list[str] = []
    for key in ("create", "overwrite", "always-overwrite"):
        created.extend(actions.get(key, []) or [])
    return exclude_entries_for_created_paths(
        created,
        folder,
        include_manifest=bool(result.get("manifest_written")),
    )


def append_git_info_exclude(
    folder: Path,
    paths: Sequence[str],
    *,
    write_bytes: Optional[Callable[[Path, bytes], object]] = None,
    block_comment: Optional[Sequence[str]] = None,
) -> dict:
    """Idempotently append ``paths`` to ``<folder>/.git/info/exclude``.

    Soft-fail + idempotent:
      - No ``.git`` DIRECTORY (not a git repo, or a bare/worktree layout where
        ``.git`` is a FILE) -> ``action="not_a_git_repo"``, no-op. Resolving a
        worktree's real gitdir is deliberately out of scope: guessing wrong
        would write into an unrelated repo's exclude file.
      - Entry already present (exact-line match) -> not re-added.
      - Write failure -> ``action="write_failed:<ErrorClass>"``, no raise.

    Args:
        folder: The project folder.
        paths: Exclude entries, already computed.
        write_bytes: Atomic writer, ``(path, data) -> ignored``. The return
            value is DISCARDED, as it was before this extraction: the
            project_init writer returns the redirect target when it refuses to
            write through a symlink, and neither call site has ever consumed
            it. Typed as ``object`` so that stays true without a cast. Defaults
            to
            :func:`vco_lib.atomic.atomic_write_bytes`. ``project_init`` injects
            its own ``_write_file_atomic`` so the extraction is behaviour-
            preserving at the existing call sites: that writer additionally
            REFUSES to write through a symlinked target or parent (v0.2.53
            NEW-8/B3), and losing that on an extraction would be a silent
            security regression.
        block_comment: Header lines written above a NEW block. Defaults to the
            safe-add wording; the move engine passes its own.

    Returns:
        ``{"action": str, "added": [str, ...], "path": str}``. Actions:
        ``appended`` | ``noop`` | ``not_a_git_repo`` | ``write_failed:*``.
    """
    writer = write_bytes if write_bytes is not None else _default_write_bytes
    git_dir = folder / ".git"
    result: dict = {"action": "not_a_git_repo", "added": [], "path": ""}
    if not git_dir.is_dir():
        return result

    info_dir = git_dir / "info"
    exclude_path = info_dir / "exclude"
    result["path"] = str(exclude_path)

    try:
        existing = (
            exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        )
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    # Exact-line membership check (strip trailing whitespace per line).
    present = {line.strip() for line in existing.splitlines()}
    to_add = [p for p in paths if p not in present]
    if not to_add:
        result["action"] = "noop"
        return result

    header = (
        list(block_comment)
        if block_comment is not None
        else [
            "# VCO safe-add (v0.2.63): keep orchestrator-created files out of "
            "your commits.",
            "# This is .git/info/exclude (LOCAL-only) — not the tracked "
            ".gitignore.",
        ]
    )
    block_lines = ["", *header, *to_add]
    block = "\n".join(block_lines) + "\n"

    # Ensure we don't glue onto a non-newline-terminated last line.
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"

    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        writer(exclude_path, (prefix + block).encode("utf-8"))
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "appended"
    result["added"] = to_add
    return result


def _default_write_bytes(path: Path, data: bytes) -> None:
    atomic_write_bytes(path, data)
