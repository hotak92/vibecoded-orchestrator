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
  next bundle update; a user's own lines are never touched (the one case
  that reads both ways is stated in :func:`_block_members`). Every block now
  ends with :data:`VCO_BLOCK_END_MARKER` (older blocks are closed on the next
  write), and a retired shipped file's line is dropped with the file.
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
    "VCO_BLOCK_END_MARKER",
    "VCO_RUNTIME_EXCLUDES",
    "LEGACY_VCO_EXCLUDE_LINES",
    "exclude_pattern_for",
    "summarize_entries",
    "exclude_entries_for_created_paths",
    "safe_add_exclude_entries",
    "retired_exclude_entries",
    "bundle_exclude_plan",
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

#: The line that closes every block VCO writes (v0.2.104 on). Without it a
#: block's end had to be guessed, and a line the user appended right after it
#: (``echo '/.claude/' >> .git/info/exclude``) read as VCO's. Blocks from
#: older releases are closed with it on the next write.
VCO_BLOCK_END_MARKER = "# VCO end"

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
#: ``.claude/specializations/`` is deliberately NOT here: users add their own
#: review-kind / topic docs beside the shipped ones, so each shipped doc is
#: excluded by its own path like a hook or an agent.
_VCO_ONLY_SUBTREES: tuple[str, ...] = (
    ".claude/hooks/_lib/",
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


#: Bundle-envelope buckets of shipped files a release STOPPED shipping:
#: ``orphan-deleted`` (VCO deleted its unmodified copy) and ``orphan-retired``
#: (a user-modified copy kept on disk — it is the user's file now).
_RETIRED_ACTION_BUCKETS: tuple[str, ...] = ("orphan-deleted", "orphan-retired")


def retired_exclude_entries(result: dict, still_listed: Iterable[str]) -> list[str]:
    """Exclude lines to drop for shipped files this bundle run retired.

    ``result`` is an ``install_project_bundle`` envelope; ``still_listed`` is
    this run's entry set (:func:`safe_add_exclude_entries`). A retired file's
    pattern is dropped only when no file still shipped maps to it (a shipped
    skill's directory stays while any of its files ships). Without this a
    retired hook's line outlived it, and a user's own file later written at
    that path was silently hidden. :func:`append_git_info_exclude` removes
    these only from VCO's own blocks.
    """
    keep = set(still_listed)
    actions = result.get("actions", {}) or {}
    out: list[str] = []
    for key in _RETIRED_ACTION_BUCKETS:
        for rel in actions.get(key, []) or []:
            pattern = exclude_pattern_for(rel)
            if pattern and pattern not in keep and pattern not in out:
                out.append(pattern)
    return out


def bundle_exclude_plan(result: dict, folder: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(entries, retired)`` for one bundle run — the arguments of
    :func:`append_git_info_exclude` (``paths``, ``retired``) at every bundle
    call site, computed in one place."""
    entries = tuple(safe_add_exclude_entries(result, folder))
    return entries, tuple(retired_exclude_entries(result, entries))


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


def _is_end_marker(stripped: str) -> bool:
    return stripped == VCO_BLOCK_END_MARKER


#: The legacy glob VCO wrote up to v0.2.103 (one of
#: :data:`LEGACY_VCO_EXCLUDE_LINES`).
_LEGACY_CLAUDE_GLOB = "/.claude/"

#: The safe-add ``.env`` reference sidecar's exclude line.
_SIDECAR_EXCLUDE_LINE = "/.env" + SAFE_ADD_SIDECAR_SUFFIX


def _is_legacy_vco_entry(stripped: str) -> bool:
    """Could a pre-v0.2.104 release have written ``stripped`` as an entry?

    Those releases wrote ``"/" + <created file>`` for every path outside
    ``.claude/`` and collapsed the whole ``.claude/`` tree to the ONE glob
    ``/.claude/``. So an entry is root-anchored, has no whitespace, never
    names a path under ``.claude/`` (``/.claude/<path>`` is v0.2.104's shape)
    and is never a directory pattern other than that glob.
    """
    if not stripped.startswith("/") or any(c.isspace() for c in stripped):
        return False
    if stripped == _LEGACY_CLAUDE_GLOB:
        return True
    return not stripped.startswith(_LEGACY_CLAUDE_GLOB) and not stripped.endswith("/")


def _block_members(lines: Sequence[str]) -> tuple[dict[int, str], list[int]]:
    """Map each line VCO wrote inside one of its blocks to that block's kind.

    Returns ``(members, legacy_block_ends)``: ``members`` maps a line index to
    ``"ended"`` (a block closed by :data:`VCO_BLOCK_END_MARKER`, written whole
    by v0.2.104 or later — every line from the header to the end marker is
    VCO's) or ``"legacy"`` (a block from an older release, which has no end
    marker); ``legacy_block_ends`` lists, per legacy block, the index of its
    last member line — where the end marker goes when the block is closed.

    **An ended block is exact.** VCO writes it whole, header to end marker, in
    one write; a line after the end marker is NEVER VCO's, whatever it says.

    **A legacy block's end is INFERRED**, because the user's own
    ``echo '…' >> .git/info/exclude`` lands directly after VCO's last line
    with no blank line between them. A line belongs to a legacy block only
    while it is one a pre-v0.2.104 release could have written there:

    * a ``#`` comment, only in the header run right after the marker;
    * an entry of the legacy shape (:func:`_is_legacy_vco_entry`): a
      ``/.claude/<path>`` line or a directory pattern other than ``/.claude/``
      is the user's (L3);
    * not a repeat of a line earlier in the same block or in an EARLIER VCO
      block — VCO de-duplicated against them. A repeat of a line the user
      keeps OUTSIDE VCO's blocks does not count: the user may have put it
      above the block with an editor after VCO wrote the block, and VCO's own
      copy must still be found (L1);
    * the legacy ``/.claude/`` glob never AFTER the ``/.env.vco.reference``
      line (L2): the glob was only ever a bundle created-file entry and VCO
      added the sidecar after all of those. A project move appended its
      copied files after the sidecar, but every pre-v0.2.104 bundle run
      listed ``.claude/hooks/_lib/`` (always overwritten) among its created
      files, so the glob was already in that block or earlier in the file.

    The first line that fails ends the block; it and everything after it up
    to the next marker are the user's.

    Stated residual ambiguity: a ``/.claude/`` the user appends directly after
    a legacy block that has no glob and does not end with the sidecar line is
    read as VCO's and removed. A pre-v0.2.104 bundle omitted the glob only
    when the file already held one, so the user still has that earlier line
    unless they deleted it; the reading never leaves VCO's own glob in place,
    which would hide ``.claude/`` with no later update able to repair it.
    """
    members: dict[int, str] = {}
    legacy_ends: list[int] = []
    vco_seen: set[str] = set()  # entries inside VCO blocks so far
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped.startswith(VCO_BLOCK_MARKER) or _is_end_marker(stripped):
            i += 1
            continue
        # A block header. Closed by an end marker before the next blank line
        # or the next header? Then every line up to the marker is VCO's.
        j = i + 1
        end_at = None
        while j < n:
            s = lines[j].strip()
            if _is_end_marker(s):
                end_at = j
                break
            if not s or s.startswith(VCO_BLOCK_MARKER):
                break
            j += 1
        if end_at is not None:
            for k in range(i, end_at + 1):
                members[k] = "ended"
                s = lines[k].strip()
                if s and not s.startswith("#"):
                    vco_seen.add(s)
            i = end_at + 1
            continue
        members[i] = "legacy"
        last = i
        in_header = True
        after_sidecar = False
        k = i + 1
        while k < n:
            s = lines[k].strip()
            if not s or s.startswith(VCO_BLOCK_MARKER):
                break
            if s.startswith("#"):
                if not in_header:
                    break
            else:
                in_header = False
                if not _is_legacy_vco_entry(s) or s in vco_seen:
                    break
                if s == _LEGACY_CLAUDE_GLOB and after_sidecar:
                    break
                if s == _SIDECAR_EXCLUDE_LINE:
                    after_sidecar = True
                vco_seen.add(s)
            members[k] = "legacy"
            last = k
            k += 1
        legacy_ends.append(last)
        i = k
    return members, legacy_ends


def _rewrite_vco_blocks(
    existing: str, retired: Iterable[str] = (),
) -> tuple[str, list[str]]:
    """Repair VCO's own blocks; never touch a line of the user's.

    * Removes :data:`LEGACY_VCO_EXCLUDE_LINES` from LEGACY blocks (v0.2.104 G4
      migration). An ended block never carries them, so one found there was
      put there by the user and stays.
    * Removes the ``retired`` patterns — lines for shipped files a release
      stopped shipping — from any VCO block.
    * Closes every legacy block with :data:`VCO_BLOCK_END_MARKER`, so a line
      the user appends afterwards is unambiguously outside it.

    See :func:`_block_members` for what counts as a VCO line. Returns
    ``(new_text, removed_lines)``; ``new_text is existing`` when nothing
    changed.
    """
    retired_set = set(retired)
    lines = existing.splitlines(keepends=True)
    members, legacy_ends = _block_members(lines)
    close_after = set(legacy_ends)
    out: list[str] = []
    removed: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        kind = members.get(idx)
        drop = kind is not None and (
            (kind == "legacy" and stripped in LEGACY_VCO_EXCLUDE_LINES)
            or stripped in retired_set
        )
        if drop:
            removed.append(stripped)
        else:
            if idx in close_after and not line.endswith("\n"):
                line += "\n"
            out.append(line)
        if idx in close_after:
            out.append(VCO_BLOCK_END_MARKER + "\n")
    if not removed and not close_after:
        return existing, []
    return "".join(out), removed


def append_git_info_exclude(
    folder: Path,
    paths: Sequence[str],
    *,
    write_bytes: Optional[Callable[[Path, bytes], object]] = None,
    block_comment: Optional[Sequence[str]] = None,
    retired: Sequence[str] = (),
) -> dict:
    """Idempotently append ``paths`` to ``<folder>/.git/info/exclude``.

    Soft-fail + idempotent:
      - No ``.git`` DIRECTORY (not a git repo, or a bare/worktree layout where
        ``.git`` is a FILE) -> ``action="not_a_git_repo"``, no-op. Resolving a
        worktree's real gitdir is deliberately out of scope: guessing wrong
        would write into an unrelated repo's exclude file.
      - Entry already present (exact-line match) -> not re-added.
      - Legacy VCO lines (:data:`LEGACY_VCO_EXCLUDE_LINES`) inside VCO's own
        pre-v0.2.104 blocks, and the ``retired`` patterns inside any VCO
        block, are removed in the same write, and pre-v0.2.104 blocks are
        closed with :data:`VCO_BLOCK_END_MARKER` (see
        :func:`_rewrite_vco_blocks`); the user's own lines never are touched
        (the one ambiguous legacy case is stated in :func:`_block_members`).
      - Every new block ends with :data:`VCO_BLOCK_END_MARKER`, so a line the
        user appends later is never mistaken for VCO's.
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
        retired: Patterns of shipped files a release retired
            (:func:`retired_exclude_entries`); removed from VCO's blocks only.

    Returns:
        ``{"action": str, "added": [str, ...], "removed": [str, ...],
        "path": str}``. Actions: ``appended`` (new entries written, any
        legacy/retired lines removed alongside) | ``migrated`` (only VCO's
        existing blocks repaired: lines removed and/or old blocks closed) |
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

    original = existing
    existing, removed = _rewrite_vco_blocks(existing, retired)
    rewritten = existing is not original

    # Exact-line membership check (strip trailing whitespace per line).
    present = {line.strip() for line in existing.splitlines()}
    # A legacy line is never (re)written: the next run would only strip it.
    to_add = list(
        dict.fromkeys(
            p for p in paths
            if p not in present and p not in LEGACY_VCO_EXCLUDE_LINES
        )
    )
    if not to_add and not rewritten:
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
        block = "\n".join(["", *header, *to_add, VCO_BLOCK_END_MARKER]) + "\n"

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
