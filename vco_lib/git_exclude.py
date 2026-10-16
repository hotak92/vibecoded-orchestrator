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
same-named files.

v0.2.104 (G4): the same rule now holds INSIDE ``.claude/``. Up to v0.2.103 the
whole tree collapsed to one ``/.claude/`` glob on the premise that a user
cannot own anything there. That premise was false: VCO itself seeds
``.claude/CONTEXT_STATE.md``, ``.claude/context/plans/`` and handoffs FOR the
user, Claude Code reads a committed ``.claude/settings.json`` and
``.claude/CLAUDE.md``, and many users version all of them — the glob made
``git add .claude/CONTEXT_STATE.md`` fail with "paths are ignored". Now:

* VCO-shipped files under ``.claude/`` (hooks, scripts, agents) are excluded
  as their SPECIFIC paths, so a hook or agent the user writes beside them
  stays addable; a shipped skill is excluded as its own directory.
* Only namespaces that are genuinely VCO's alone collapse to one glob —
  :data:`VCO_RUNTIME_EXCLUDES` (runtime state, logs, adoption backups, agent
  worktrees, the launcher's ``.disabled/`` parking dirs, the manifest, and
  the machine-local files VCO writes into ``.claude/context/``) plus the
  :data:`_VCO_ONLY_SUBTREES` the bundle owns outright.
* :data:`USER_OWNED_CLAUDE_PATHS` are NEVER excluded, whoever created them.
* :func:`append_git_info_exclude` removes the legacy ``/.claude/`` (and the
  never-matching root ``/.vco-manifest.json``) line from VCO's OWN blocks on
  every write, so a project upgraded from an older release is repaired by its
  next bundle update; a user's own lines are never touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from vco_lib.atomic import atomic_write_bytes
from vco_lib.manifest_paths import MANIFEST_BASENAME, MANIFEST_REL_POSIX
from vco_lib.paths import to_posix_rel

#: Exclude pattern for the bundle manifest, composed from the one home's
#: project-relative path. A ``.git/info/exclude`` pattern containing a slash is
#: anchored at the repository root, so the pre-v0.2.104 ``/.vco-manifest.json``
#: matched only a ROOT-level file and never ``.claude/.vco-manifest.json``; it
#: was hidden only by the blanket ``/.claude/`` glob this release retires.
_MANIFEST_EXCLUDE_PATTERN = "/" + MANIFEST_REL_POSIX

__all__ = [
    "SAFE_ADD_SIDECAR_SUFFIX",
    "USER_OWNED_CLAUDE_PATHS",
    "VCO_BLOCK_MARKER",
    "VCO_RUNTIME_EXCLUDES",
    "LEGACY_VCO_EXCLUDE_LINES",
    "exclude_pattern_for",
    "summarize_entries",
    "exclude_entries_for_created_paths",
    "safe_add_exclude_entries",
    "append_git_info_exclude",
]


#: Suffix appended to a live file path to form its safe-add reference sidecar
#: (e.g. ``.env`` -> ``.env.vco.reference``). The Rust launcher uses the same
#: string when it writes the ``.env`` reference; kept here so the git-exclude
#: pattern below matches it too.
SAFE_ADD_SIDECAR_SUFFIX = ".vco.reference"

#: Every block VCO appends to ``.git/info/exclude`` starts with a comment line
#: carrying this prefix. The legacy-line migration recognises VCO's blocks by
#: it, so it can repair what VCO wrote without touching the user's own lines.
VCO_BLOCK_MARKER = "# VCO "

#: Paths under ``.claude/`` that belong to the USER even when VCO seeded them.
#: Never excluded, whatever a caller passes in. A trailing ``/`` means the
#: directory, recursively.
USER_OWNED_CLAUDE_PATHS: tuple[str, ...] = (
    ".claude/CONTEXT_STATE.md",  # working memory VCO seeds for the user
    ".claude/CLAUDE.md",  # Claude Code project instructions
    ".claude/MEMORY.md",
    ".claude/settings.json",  # VCO merges into it; teams commit it
    ".claude/settings.local.json",  # Claude Code's own; its rules, not ours
    ".claude/context/",  # plans, handoffs, reviews, per-session state
    ".claude/rules/",
    ".claude/commands/",
)

#: Namespaces and files that are VCO's ALONE — no user content can live there
#: — so they are excluded as one entry each on every bundle run, created this
#: run or not (most are written at runtime, never by the bundle copy).
VCO_RUNTIME_EXCLUDES: tuple[str, ...] = (
    _MANIFEST_EXCLUDE_PATTERN,  # the bundle manifest
    "/.claude/state/",  # hook/launcher runtime state, the lean-ctx tee dir
    "/.claude/logs/",  # run logs, the auto-resolution trail
    "/.claude/backups/",  # bundle-adoption backups
    "/.claude/worktrees/",  # isolated-subagent worktrees
    "/.claude/agents.disabled/",  # launcher's disable toggle parks agents here
    "/.claude/skills.disabled/",  # ... and skills here
    "/.claude/env",  # machine-local env file (absolute paths, hub routing)
    "/.claude/.code_formats.json",  # code-graph format sidecar
    # Machine-local files VCO writes into the otherwise user-owned
    # `.claude/context/` — named one by one, never the directory.
    "/.claude/context/UPDATE_DEFERRED.md",
    "/.claude/context/UPDATE_DEFERRED.json",
    "/.claude/context/.update-deferred.lock",
    "/.claude/context/pre-compact-snapshot.md",
    "/.claude/context/last-compact-marker",
    "/.claude/context/launcher-restart-marker",
    "/.claude/context/EMBEDDING_FAILURES.md",
)

#: Bundle subtrees VCO owns outright: every file is always-overwritten from the
#: shipped copy, so one directory entry replaces a line per file.
_VCO_ONLY_SUBTREES: tuple[str, ...] = (
    ".claude/hooks/_lib/",
    ".claude/specializations/",
)

#: Lines VCO wrote before v0.2.104 that the migration removes from VCO's own
#: blocks: the blanket ``.claude/`` glob, and the root-anchored manifest pattern
#: that never matched the real manifest.
LEGACY_VCO_EXCLUDE_LINES: frozenset[str] = frozenset(
    {"/.claude/", "/" + MANIFEST_BASENAME}
)


def _is_user_owned(rel_posix: str) -> bool:
    for owned in USER_OWNED_CLAUDE_PATHS:
        if owned.endswith("/"):
            if rel_posix.startswith(owned):
                return True
        elif rel_posix == owned:
            return True
    return False


def exclude_pattern_for(rel: str) -> Optional[str]:
    """The ``.git/info/exclude`` entry for ONE path VCO created, or ``None``.

    ``None`` for a user-owned ``.claude/`` path (never hidden) and for an empty
    path. Every pattern is root-anchored with a leading ``/`` so it matches
    only that path, never a same-named file deeper in the tree.
    """
    rel_posix = to_posix_rel(rel).lstrip("/")
    if not rel_posix:
        return None
    if rel_posix == MANIFEST_BASENAME or rel_posix == MANIFEST_REL_POSIX:
        return _MANIFEST_EXCLUDE_PATTERN
    if not rel_posix.startswith(".claude/"):
        # A root file like CLAUDE.md, or a file inside a possibly-user-owned
        # dir like .vscode/ or infrastructure/: this exact path only.
        return "/" + rel_posix
    if _is_user_owned(rel_posix):
        return None
    for runtime in VCO_RUNTIME_EXCLUDES:
        bare = runtime.lstrip("/")
        if rel_posix == bare or (bare.endswith("/") and rel_posix.startswith(bare)):
            return runtime
    for subtree in _VCO_ONLY_SUBTREES:
        if rel_posix.startswith(subtree):
            return "/" + subtree
    parts = rel_posix.split("/")
    if parts[1] == "skills" and len(parts) >= 4:
        # `.claude/skills/<name>/...` — a shipped skill is a whole directory;
        # a skill the user writes is a different directory and stays visible.
        return "/" + "/".join(parts[:3]) + "/"
    return "/" + rel_posix


def exclude_entries_for_created_paths(
    created: Iterable[str],
    folder: Path,
    *,
    include_manifest: bool = False,
    include_vco_runtime: bool = False,
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
        include_manifest: Force the manifest entry in even when the caller's
            created-list did not enumerate it.
        include_vco_runtime: Add every :data:`VCO_RUNTIME_EXCLUDES` entry (a
            bundle ran, so VCO's runtime namespaces exist or will).

    Returns:
        Deduplicated, order-preserving entries (see :func:`exclude_pattern_for`).
    """
    entries: list[str] = []
    seen: set[str] = set()

    def _add(entry: Optional[str]) -> None:
        if entry and entry not in seen:
            seen.add(entry)
            entries.append(entry)

    for rel in created:
        _add(exclude_pattern_for(rel))

    if include_manifest:
        _add(_MANIFEST_EXCLUDE_PATTERN)
    if include_vco_runtime:
        for runtime in VCO_RUNTIME_EXCLUDES:
            _add(runtime)

    # The Rust launcher writes the `.env` reference sidecar before this step.
    sidecar = ".env" + SAFE_ADD_SIDECAR_SUFFIX
    if (folder / sidecar).exists():
        _add("/" + sidecar)
    return entries


#: Bundle-envelope action buckets whose paths are VCO's shipped bytes on disk.
#: ``noop`` (unchanged since the last run) and ``adopt`` (shipped version
#: written over a local edit) are included, not only this run's writes: the
#: legacy-glob migration removes ``/.claude/`` on the first update after
#: v0.2.104, and every shipped file must get its own entry in that same run.
#: ``skip-existing`` (a pre-existing file the bundle did NOT overwrite — the
#: user's) and ``preserve`` (a user-modified file kept) are deliberately absent.
_VCO_OWNED_ACTION_BUCKETS: tuple[str, ...] = (
    "create",
    "overwrite",
    "always-overwrite",
    "adopt",
    "noop",
)


def safe_add_exclude_entries(result: dict, folder: Path) -> list[str]:
    """Bundle-envelope adapter over :func:`exclude_entries_for_created_paths`.

    ``result`` is an ``install_project_bundle`` envelope; the VCO-owned set is
    the union of its :data:`_VCO_OWNED_ACTION_BUCKETS`. A bundle ran, so the
    manifest and VCO's runtime namespaces are always included.
    """
    actions = result.get("actions", {}) or {}
    created: list[str] = []
    for key in _VCO_OWNED_ACTION_BUCKETS:
        created.extend(actions.get(key, []) or [])
    return exclude_entries_for_created_paths(
        created,
        folder,
        include_manifest=True,
        include_vco_runtime=True,
    )


def summarize_entries(entries: Sequence[str], shown: int = 12) -> str:
    """One-line human summary of exclude entries for a deferral message.

    Since v0.2.104 there is one entry per shipped hook, script and agent, so
    the first ``shown`` are named and the rest counted; the full list is one
    ``cat .git/info/exclude`` away.
    """
    if not entries:
        return "(none)"
    text = ", ".join(entries[:shown])
    if len(entries) > shown:
        text += f" and {len(entries) - shown} more"
    return text


def _strip_legacy_vco_lines(existing: str) -> tuple[str, list[str]]:
    """Remove :data:`LEGACY_VCO_EXCLUDE_LINES` from VCO's own blocks.

    A VCO block is a line starting with :data:`VCO_BLOCK_MARKER` and the
    non-blank lines after it (``append_git_info_exclude`` always writes a blank
    line, its header, then its entries). A legacy line OUTSIDE such a block is
    the user's own and is kept. Every other line is kept byte-for-byte.

    Returns ``(new_text, removed_lines)``; ``new_text is existing`` when
    nothing was removed.
    """
    lines = existing.splitlines(keepends=True)
    kept: list[str] = []
    removed: list[str] = []
    in_vco_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_vco_block = False
        elif stripped.startswith(VCO_BLOCK_MARKER):
            in_vco_block = True
        elif in_vco_block and stripped in LEGACY_VCO_EXCLUDE_LINES:
            removed.append(stripped)
            continue
        kept.append(line)
    if not removed:
        return existing, []
    return "".join(kept), removed


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
      - Legacy VCO lines (:data:`LEGACY_VCO_EXCLUDE_LINES`) inside VCO's own
        blocks are removed in the same write (v0.2.104 G4 migration — see
        :func:`_strip_legacy_vco_lines`); the user's own lines never are.
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
        ``{"action": str, "added": [str, ...], "removed": [str, ...],
        "path": str}``. Actions: ``appended`` (new entries written, any legacy
        lines removed alongside) | ``migrated`` (only legacy lines removed) |
        ``noop`` | ``not_a_git_repo`` | ``write_failed:*``.
    """
    writer = write_bytes if write_bytes is not None else _default_write_bytes
    git_dir = folder / ".git"
    result: dict = {
        "action": "not_a_git_repo", "added": [], "removed": [], "path": "",
    }
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

    existing, removed = _strip_legacy_vco_lines(existing)

    # Exact-line membership check (strip trailing whitespace per line).
    present = {line.strip() for line in existing.splitlines()}
    # A legacy line is never (re)written: the next run would only strip it.
    to_add = list(
        dict.fromkeys(
            p for p in paths
            if p not in present and p not in LEGACY_VCO_EXCLUDE_LINES
        )
    )
    if not to_add and not removed:
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
            "# Your own .claude/ files (CONTEXT_STATE.md, context/, settings.json, "
            "CLAUDE.md) are never listed here.",
        ]
    )
    if not header or not header[0].startswith(VCO_BLOCK_MARKER):
        # The migration finds VCO's blocks by this marker; a block without it
        # would leave its lines unrepairable by a later release.
        header.insert(0, VCO_BLOCK_MARKER + "exclude block (orchestrator-managed).")
    block = ""
    if to_add:
        block = "\n".join(["", *header, *to_add]) + "\n"

    # Ensure we don't glue onto a non-newline-terminated last line.
    prefix = existing
    if prefix and not prefix.endswith("\n") and block:
        prefix += "\n"

    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        writer(exclude_path, (prefix + block).encode("utf-8"))
    except OSError as e:
        result["action"] = f"write_failed:{type(e).__name__}"
        return result

    result["action"] = "appended" if to_add else "migrated"
    result["added"] = to_add
    result["removed"] = removed
    return result


def _default_write_bytes(path: Path, data: bytes) -> None:
    atomic_write_bytes(path, data)
