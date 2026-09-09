# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco project move`` — re-point + re-materialize a registered project at a
new folder. (v0.2.92 WP-17 / W3.)

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
This is **re-point + re-materialize**, not a general folder mover. The user (or
git) puts their own project content at the destination D; VCO moves only what
VCO manages plus the user-owned VCO-adjacent state that would otherwise be
stranded (``knowledge/**``, ``.claude/context/**``, the disabled agent/skill
dirs, user-modified bundle files). The source S is **never deleted** and
nothing at D is ever overwritten. "Never auto-destroy user data" is satisfied
by construction rather than by care: the engine performs ZERO deletions and
ZERO overwrites at D, and a full-tree hash of S is asserted equal before and
after in the test suite.

THE ONE INVARIANT THAT MATTERS MOST
-----------------------------------
**Identity is row-keyed, not path-derived.** A move changes where a project
LIVES; it must not change WHO it is. This engine never derives a collection
name, a code-graph prefix, a slug or a project name from the destination's
basename — it reads them from the project ROW and carries them across
unchanged. That is not a stylistic preference: this machine currently has
``codegraph-prefix-generation.json`` files naming prefixes that match ZERO
live Weaviate classes, produced by exactly that folder-basename derivation. A
move that re-derived identity would reproduce that defect deliberately — the
project keeps its data and loses the ability to find it. If you are editing
this module and find yourself computing a name from ``dst.name``, stop.

FAILURE SEMANTICS — RESUMABLE, WITH ONE DURABLE COMMIT POINT (argued)
---------------------------------------------------------------------
A move touches a filesystem, a SQLite database and a set of collection
bindings. There is no transaction spanning those, and pretending otherwise
would be the promise this codebase keeps finding in its own comments. So the
guarantee is stated honestly and the phases are ordered to make it hold:

* **Phases 1–4 (copy, bundle, git hygiene) are PRE-COMMIT.** They are additive
  at D and touch S not at all. If ANY of them fails, the move refuses: the DB
  is untouched, the project still lives at S and still works, and D holds only
  files that were added, never files that were changed. Nothing is half-moved
  because nothing has moved yet. Re-running is safe (every step is idempotent).
* **The commit is ONE SQL transaction** in a sanctioned writer (the launcher's
  ``Db`` for the GUI, the hub route for the CLI): flip ``folder_path``,
  re-point the ``targeted-update`` columns, enqueue the code-graph rebuild,
  mark the move row ``flipped``. It succeeds entirely or rolls back entirely.
* **Phases 6–7 are POST-COMMIT reconciliation** — env re-projection, kg-sync
  parity, the stale scans, the ledger. If the process dies here, the project
  is at D and works; what is owed is idempotent and re-runnable via
  ``vco project move --verify``. The ``project_moves`` row stays ``flipped``
  (not ``completed``), which is what surfaces the unfinished move at next boot.

The alternative — copy, flip, and hope — produces the state this feature
exists to repair. A refused move costs the user a message; a half-moved
project costs them their project.

WHY THE ENGINE NEVER WRITES ``launcher.db``
-------------------------------------------
Three sanctioned writer surfaces already exist (launcher ``Db``, vct-hub's
``LauncherDbHandle``, ``vco_lib.launcher_db_writer`` for ``app_state``). Adding
a fourth that writes ``projects`` rows from Python would be a new home for the
single-writer discipline, and its "probe the hub, then write if it is down"
guard is a time-of-check-to-time-of-use race by construction. Under R20 the hub
may be assumed running (the launcher and every Claude session ensure it), so
this engine instead REFUSES at preflight when no sanctioned writer answers —
before a single byte is copied. Reads are read-only (``mode=ro``) through
:mod:`vco_lib.launcher_db_reader`.

CHILD PROCESSES GET A CONSTRUCTED ENV, NEVER AN INHERITED ONE
--------------------------------------------------------------
The field move's first sync ran with the OPERATOR's session env, so a foreign
``KG_COLLECTION`` from the operator's own project reached the child. It was
harmless there and silently wrong in general. Every child this engine spawns
gets ``os.environ`` scrubbed of the projection-owned key set, then the TARGET
project's resolved config overlaid, then argv pins where the child supports
them. See :func:`build_child_env`.
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from vco_lib import remedy_shell

from vco_lib import git_exclude as _git_exclude
from vco_lib import path_bearing_keys as _pbk
from vco_lib.atomic import atomic_copy_file, atomic_write_text
from vco_lib.hashing import sha256_file
from vco_lib.paths import to_posix_rel

__all__ = [
    "MoveError",
    "MoveRefused",
    "REFUSAL_REASONS",
    "PathOverlap",
    "check_path_overlap",
    "path_compare_key",
    "paths_equal",
    "is_ancestor",
    "SourceFile",
    "Conflict",
    "MovePlan",
    "classify_move_sources",
    "classify_conflicts",
    "plan_move",
    "plan_for_selector",
    "registered_projects",
    "execute_pre_flip",
    "execute_post_flip",
    "verify_move",
    "build_child_env",
    "sweep_db_for_path",
    "scan_files_for_path",
    "SENTINEL_REL",
    "MOVED_SIBLING_SUFFIX",
    "main",
]


# ───────────────────────────────────────────────────────────────────────────
# Constants
# ───────────────────────────────────────────────────────────────────────────

#: In-progress sentinel, written under BOTH S and D so an interrupted move is
#: visible from whichever folder the user opens next. Shaped after the bundle
#: update's resume sentinel (same idea, its own file and schema — there is no
#: byte-parity claim between the two and no test pinning one).
SENTINEL_REL = Path(".claude") / "context" / ".vco-move-in-progress.json"

#: Suffix for a divergent file's adjacent sibling at D. The ``.vco-new``
#: sibling from the adopt flow is the precedent; a distinct suffix keeps the
#: two provenances distinguishable in a tree that has seen both.
MOVED_SIBLING_SUFFIX = ".vco-moved"

#: Relative path of the bundle manifest (mirrors ``project_init._MANIFEST_REL``
#: — read-only use, and the constant is one path segment, not logic).
_MANIFEST_REL = Path(".claude") / ".vco-manifest.json"

#: Deferral condition ids. Declared in ``vco_lib/deferral_conditions.toml``;
#: the completeness test scans these literals.
CID_CONFLICT_PREFIX = "project_move_conflict_"
CID_STALE_PATH_REFERENCE = "project_move_stale_path_reference"
CID_STALE_DB_PATH = "project_move_stale_db_path"
CID_EXTRA_CODEGRAPH_PREFIX = "project_move_extra_codegraph_path_"
CID_CODEGRAPH_REANALYZE = "project_move_codegraph_reanalyze_pending"
CID_OLD_FOLDER_RETAINED = "project_move_old_folder_retained"
CID_HARNESS_STATE_REVIEW = "project_move_harness_state_review"

#: Every refusal the preflight can produce, with the wording that reaches the
#: user. Each is DISTINCT: "that path is inside another project" and "that
#: path is already registered" call for different actions, and collapsing
#: them into one "invalid destination" would hide which.
REFUSAL_REASONS: dict[str, str] = {
    "dst_not_absolute": (
        "The destination must be an absolute path. A relative path would "
        "resolve against whatever directory the command happened to run in."
    ),
    "dst_is_file": "The destination exists and is a file, not a directory.",
    "dst_parent_missing": (
        "The destination's parent directory does not exist. Create it first — "
        "VCO does not create parent directories for a move, because a typo'd "
        "path would silently materialize a new tree (the failure that "
        "produced the scaffold this feature exists to undo)."
    ),
    "dst_equals_src": "The destination is the folder the project already uses.",
    "dst_inside_src": (
        "The destination is INSIDE the project's current folder. Moving a "
        "project into itself would make the old tree an ancestor of the new "
        "one and every path-bearing value ambiguous."
    ),
    "dst_contains_src": (
        "The destination CONTAINS the project's current folder. The move "
        "would register a root that already holds the old root."
    ),
    "dst_registered_to_another_project": (
        "That exact path is already registered to another project. Two "
        "projects cannot share a folder — `folder_path` is UNIQUE, and the "
        "second one would silently inherit the first one's bundle."
    ),
    "dst_inside_registered_project": (
        "The destination is inside another registered project's folder. The "
        "inner project's `.claude/` would be materialized inside the outer "
        "project's tree, and every hook would resolve the wrong root."
    ),
    "dst_contains_registered_project": (
        "The destination contains another registered project's folder. This "
        "project's bundle would be installed over the other project's parent."
    ),
    "dst_not_empty": (
        "The destination directory is not empty. Re-run with --into-existing "
        "once you have reviewed the dry-run preview: existing files at the "
        "destination are NEVER overwritten, but files that differ from the "
        "source will land beside them as `.vco-moved` siblings for you to "
        "merge."
    ),
    "src_registered_path_missing": (
        "The project's currently-registered folder does not exist on disk. "
        "Re-run with --from-missing to re-point the registration anyway "
        "(nothing can be copied — the destination must already hold the "
        "project's content)."
    ),
    "move_in_flight": (
        "Another move of this project is already in progress. Wait for it to "
        "finish, or resolve it with `vco project move --verify`."
    ),
    "writer_unavailable": (
        "No sanctioned database writer answered. The move refuses BEFORE "
        "copying anything rather than copying first and discovering it "
        "cannot commit. Start the launcher (or vct-hub) and retry."
    ),
    "project_not_found": "No registered project matches that id or slug.",
}


class MoveError(RuntimeError):
    """A move failed after preflight (I/O, subprocess, writer error)."""


class MoveRefused(MoveError):
    """Preflight refused the move. ``reason`` is a key of :data:`REFUSAL_REASONS`."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        base = REFUSAL_REASONS.get(reason, reason)
        super().__init__(f"{base} {detail}".strip())


# ───────────────────────────────────────────────────────────────────────────
# Path comparison — tri-OS (R14)
# ───────────────────────────────────────────────────────────────────────────
#
# Three distinct hazards, each of which this repo has already paid for once:
#
#   1. Case. Windows and macOS default to case-INSENSITIVE filesystems, so
#      `C:\Proj` and `c:\proj` are the same directory. A case-sensitive
#      comparison would let a user "move" a project onto itself, or register a
#      second project at a path that collides on disk.
#   2. Separators. A Windows path may arrive with `/` (from JSON, a shell, or
#      a hand-typed value) or `\`. v0.2.81 shipped a mass-delete from exactly
#      this. `os.path.normcase` converts `/`→`\` on Windows; `PurePath`
#      comparison alone does not.
#   3. Ancestry by string prefix. `/a/proj` is NOT an ancestor of
#      `/a/project`, but `startswith` says it is. Ancestry is decided on PARTS.

def _case_insensitive_fs(platform: Optional[str] = None) -> bool:
    """Whether the host filesystem should be treated as case-insensitive.

    ``platform`` defaults to :data:`sys.platform` and is injectable so the
    tri-OS shape can be unit-tested on one machine (R14: "only verifiable on
    <one OS>" is a disallowed acceptance criterion).
    """
    plat = platform if platform is not None else sys.platform
    return plat.startswith("win") or plat == "darwin"


def path_compare_key(
    path: "str | Path", *, platform: Optional[str] = None
) -> str:
    """A comparison key for ``path`` that is correct on all three OSes.

    Applies ``os.path.normpath`` + ``os.path.normcase``, then case-folds when
    the host filesystem is case-insensitive. macOS needs the explicit fold:
    ``os.path.normcase`` is the IDENTITY function on darwin, so relying on it
    alone would make macOS behave like Linux and silently accept a
    case-variant collision.

    ``normpath`` also collapses ``.`` and ``..`` LEXICALLY, and that is
    load-bearing rather than incidental: ``/a/b/../..`` is ``/`` and is
    therefore NOT under ``/a``. Treating it as under ``/a`` would make
    :func:`is_ancestor` claim ancestry over a path that resolves outside the
    root — a false-positive that
    ``launcher/src-tauri/vct-launcher-core/src/db/bindings_writer.rs
    ::repoint_kg_dir_path`` would turn into a rewrite of a user-owned column.
    The Rust mirror collapses ``..`` for the same reason; the two are locked
    together by ``tests/fixtures/path_ancestry_parity.json``.

    Deliberately does NOT call ``resolve()``: resolution touches the
    filesystem and follows symlinks, which a pure comparison helper must not
    do. Callers that need a resolved path resolve it first and pass the
    result — :func:`_resolve_for_compare` is that step. Lexical ``..``
    collapsing is the same trade: it can disagree with a symlinked reality,
    and it is what a pure comparison helper is allowed to do.
    """
    text = str(path)
    if platform is not None and platform.startswith("win"):
        # Emulate Windows normcase+normpath on a non-Windows host so the shape
        # is testable everywhere: separators unify to `\`, `.`/`..` collapse,
        # case folds. `ntpath` is pure Python and imports on every OS, so this
        # is the REAL Windows rule rather than an approximation of it.
        #
        # v0.2.92 MAJOR-12: this branch used to skip normpath entirely, so the
        # emulated-Windows shape kept `..` as a component while the branch
        # below collapsed it — `C:\a\b\..\..` compared as five components on
        # the Windows shape and as one on every other. That was Python
        # disagreeing with ITSELF across shapes, underneath a Rust mirror that
        # claimed to match "the Python home".
        text = ntpath.normpath(text.replace("/", "\\"))
        return text.rstrip("\\").casefold() or "\\"
    normalised = os.path.normcase(os.path.normpath(text))
    if _case_insensitive_fs(platform):
        normalised = normalised.casefold()
    return normalised


def _resolve_for_compare(path: "str | Path") -> Path:
    """``Path.resolve()`` with a non-existent-path fallback.

    ``strict=False`` resolution is what we want: the destination frequently
    does not exist yet, and refusing to compare an unborn path would make the
    overlap check useless exactly when it matters.
    """
    p = Path(path)
    try:
        return p.resolve()
    except OSError:
        return Path(os.path.abspath(str(p)))


def _parts(path: "str | Path", *, platform: Optional[str] = None) -> tuple[str, ...]:
    """Comparison-normalised path components.

    A lone ``.`` contributes NO component: ``normpath`` collapses a path that
    lands back on the current directory to the single string ``"."``, and
    "here" is zero components, not one named ``.``. Keeping it made
    ``is_ancestor(".", "./a")`` False (``('.',)`` is not shorter than
    ``('a',)``) — v0.2.92 MAJOR-12, found by the shared parity corpus in
    ``tests/fixtures/path_ancestry_parity.json``, where the Rust mirror
    already dropped ``.`` and this side did not.
    """
    key = path_compare_key(path, platform=platform)
    sep = "\\" if (platform or sys.platform).startswith("win") else os.sep
    raw = key.replace("/", sep).split(sep) if sep == "\\" else key.split(sep)
    return tuple(p for p in raw if p and p != ".")


def paths_equal(
    a: "str | Path", b: "str | Path", *, platform: Optional[str] = None
) -> bool:
    """Whether ``a`` and ``b`` name the same directory on this host."""
    return path_compare_key(a, platform=platform) == path_compare_key(
        b, platform=platform
    )


def is_ancestor(
    ancestor: "str | Path", descendant: "str | Path", *, platform: Optional[str] = None
) -> bool:
    """Whether ``ancestor`` strictly contains ``descendant``.

    Component-wise, never ``startswith``: ``/a/proj`` does not contain
    ``/a/project``.
    """
    a = _parts(ancestor, platform=platform)
    d = _parts(descendant, platform=platform)
    return len(a) < len(d) and d[: len(a)] == a


@dataclass(frozen=True)
class PathOverlap:
    """One registered project that overlaps a candidate path."""

    project_id: str
    project_name: str
    folder_path: str
    #: ``same`` | ``inside`` (candidate is inside this project) |
    #: ``contains`` (candidate contains this project).
    relation: str


def check_path_overlap(
    candidate: "str | Path",
    registered: Iterable[Mapping[str, Any]],
    *,
    exclude_project_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> tuple[PathOverlap, ...]:
    """Every registered project whose folder overlaps ``candidate``.

    Args:
        candidate: The path being proposed.
        registered: Project rows — mappings with ``id``, ``name``,
            ``folder_path``.
        exclude_project_id: The project doing the moving; its own row is not
            an overlap with itself.
        platform: Override for tri-OS shape tests.

    Returns:
        Overlaps ordered ``same`` first, then ``inside``, then ``contains`` —
        the order the caller reports them in, so the most specific refusal is
        the one the user reads.

    This helper is W1's, built here because W1 is not in this tag (EXTENSION
    §0 item 7). It is deliberately pure: no DB, no filesystem, so the
    adopt/create flows can adopt it without inheriting a dependency.
    """
    cand = _resolve_for_compare(candidate)
    out: list[PathOverlap] = []
    for row in registered:
        pid = str(row.get("id") or "")
        if exclude_project_id is not None and pid == exclude_project_id:
            continue
        folder = str(row.get("folder_path") or "")
        if not folder:
            continue
        other = _resolve_for_compare(folder)
        name = str(row.get("name") or pid)
        if paths_equal(cand, other, platform=platform):
            relation = "same"
        elif is_ancestor(other, cand, platform=platform):
            relation = "inside"
        elif is_ancestor(cand, other, platform=platform):
            relation = "contains"
        else:
            continue
        out.append(PathOverlap(pid, name, folder, relation))
    rank = {"same": 0, "inside": 1, "contains": 2}
    return tuple(sorted(out, key=lambda o: (rank[o.relation], o.folder_path)))


# ───────────────────────────────────────────────────────────────────────────
# Phase 0 — source classification
# ───────────────────────────────────────────────────────────────────────────

#: Non-manifest paths that are VCO-ADJACENT: not shipped by the bundle, but
#: created by the user (or by VCO on the user's behalf) inside VCO's own
#: namespace, so a move that left them behind would strand real work.
#:
#: Each entry is a POSIX-shaped relative path. A trailing ``/**`` means "this
#: directory, recursively".
USER_ADJACENT_SPECS: tuple[str, ...] = (
    # Knowledge graph nodes. ALWAYS user-owned (v0.2.84 R2 carve-out) — the
    # bundle classifier already refuses to overwrite these, and a move that
    # dropped them would lose the embeddings' source of truth.
    "knowledge/**",
    # Working memory, plans, the deferral ledger, the pre-compact snapshot.
    ".claude/context/**",
    # Long-lived launcher/hook state. Named in the house rules as expensive
    # to regenerate.
    ".claude/state/**",
    # THE M-1 SET. A disable MOVES the file to `.disabled/` and flips only the
    # DB flag (`set_enabled_with_fs_move`), so the `.disabled/` copy is in NO
    # manifest (`skip-disabled` deliberately records no preservation entry).
    # Copying these dirs in Phase 1 is precisely what makes the Phase-2
    # skip-disabled guard FIRE at D. Without them, the guard finds nothing,
    # the bundle recreates the ENABLED-side file, and the harness loads an
    # agent the GUI shows as disabled.
    ".claude/agents.disabled/**",
    ".claude/skills.disabled/**",
    # Root-level working documents, when present.
    "CONTEXT_STATE.md",
    "MEMORY.md",
)

#: Paths that are explicitly NOT copied even though they sit inside VCO's
#: namespace, each for a stated reason. Being explicit here is the difference
#: between a decision and an oversight.
NOT_COPIED_SPECS: dict[str, str] = {
    ".claude/.vco-manifest.json": (
        "Phase 2 writes a FRESH manifest at D describing what was actually "
        "materialized there. Copying S's would claim D holds files it may "
        "not, and would make every later update classify from a lie. S's copy "
        "stays frozen for forensics."
    ),
    ".claude/logs/**": (
        "Run logs and the auto-resolution trail are historical records of "
        "what happened at the OLD root — the same reasoning that keeps "
        "`kg_syncs.log_tail` out of the DB rewrite."
    ),
    ".env": (
        "May hold secrets. Copying credentials to a new location is an "
        "explicit act, not a side effect of a move. Named in the completion "
        "summary as staying behind so the user decides."
    ),
}


@dataclass(frozen=True)
class SourceFile:
    """One classified path under S."""

    rel: str
    #: ``bundle-clean`` | ``user-modified`` | ``user-adjacent``
    bucket: str
    reason: str = ""

    @property
    def copied(self) -> bool:
        return self.bucket != "bundle-clean"


def _iter_spec_matches(src: Path, spec: str) -> Iterable[str]:
    """Every existing relative path under ``src`` matching one spec."""
    if spec.endswith("/**"):
        base = spec[:-3]
        root = src / base
        if not root.is_dir():
            return
        for p in sorted(root.rglob("*")):
            if p.is_file() or p.is_symlink():
                yield to_posix_rel(p.relative_to(src))
        return
    candidate = src / spec
    if candidate.is_file() or candidate.is_symlink():
        yield spec


def _not_copied_match(rel: str) -> Optional[str]:
    """The NOT_COPIED reason for ``rel``, or ``None``."""
    for spec, reason in NOT_COPIED_SPECS.items():
        if spec.endswith("/**"):
            if rel == spec[:-3] or rel.startswith(spec[:-3] + "/"):
                return reason
        elif rel == spec:
            return reason
    return None


def classify_move_sources(
    src: Path,
    manifest: Mapping[str, Any],
    *,
    adjacent_specs: Sequence[str] = USER_ADJACENT_SPECS,
) -> tuple[SourceFile, ...]:
    """Classify every VCO-relevant path under ``src`` (Phase 0, pure read).

    The MANIFEST HASH DISCIPLINE is the mechanism that decides copy vs
    re-materialize:

    * ``bundle-clean`` — a ``files`` entry whose on-disk sha256 still equals
      the recorded SHIPPED sha. The bundle re-materializes it at D with
      D-substituted transforms, so copying it would carry S-substituted
      content across and defeat the substitution.
    * ``user-modified`` — a ``files`` entry whose on-disk bytes differ, or any
      ``preserved_files`` entry. Copied.
    * ``user-adjacent`` — not in the manifest at all, but in the declared
      adjacent set. Copied.

    Everything else under S (the user's own source, ``.git``, venvs) is out of
    scope: untouched, not copied, reported to the user as staying behind.
    """
    files = manifest.get("files") or {}
    preserved = manifest.get("preserved_files") or {}
    out: list[SourceFile] = []
    seen: set[str] = set()

    for raw_rel, meta in sorted(files.items()):
        rel = to_posix_rel(raw_rel)
        if rel in seen:
            continue
        on_disk = src / PurePosixPath(rel)
        if not on_disk.is_file():
            # Shipped file the user deleted. Nothing to copy; the bundle at D
            # will recreate it (first-install mode), which is the same end
            # state a fresh install would produce.
            continue
        shipped = str((meta or {}).get("sha256") or "")
        try:
            actual = sha256_file(on_disk)
        except OSError:
            actual = ""
        seen.add(rel)
        if shipped and actual == shipped:
            out.append(
                SourceFile(rel, "bundle-clean", "on-disk sha matches shipped sha")
            )
        else:
            out.append(
                SourceFile(
                    rel,
                    "user-modified",
                    "on-disk sha differs from the shipped sha recorded at install",
                )
            )

    for raw_rel in sorted(preserved):
        rel = to_posix_rel(raw_rel)
        if rel in seen:
            continue
        if not (src / PurePosixPath(rel)).is_file():
            continue
        seen.add(rel)
        out.append(
            SourceFile(rel, "user-modified", "recorded in the manifest's preserved_files")
        )

    for spec in adjacent_specs:
        for rel in _iter_spec_matches(src, spec):
            if rel in seen:
                continue
            skip_reason = _not_copied_match(rel)
            if skip_reason is not None:
                continue
            seen.add(rel)
            out.append(SourceFile(rel, "user-adjacent", f"matches adjacent spec {spec}"))

    return tuple(sorted(out, key=lambda s: s.rel))


# ───────────────────────────────────────────────────────────────────────────
# Phase 0 — conflict classification at D
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Conflict:
    """A to-copy path that already exists at D."""

    rel: str
    #: ``identical`` | ``divergent``
    kind: str


def classify_conflicts(
    to_copy: Sequence[SourceFile], src: Path, dst: Path
) -> tuple[Conflict, ...]:
    """Partition the to-copy set against what already exists at D.

    Reuses the CATEGORY SCHEME (``identical`` / ``divergent``) from the
    untracked-collision classifier, not its code: that one compares git blobs
    and lives in Rust; this one compares filesystem hashes and lives here.
    Mirroring it across languages to share three lines of vocabulary would be
    a C-class mirror where no sharing is needed.

    ``identical`` is a no-op — D already has those exact bytes. ``divergent``
    is NEVER auto-merged: D's file is what the user has been working in.
    """
    out: list[Conflict] = []
    for entry in to_copy:
        if not entry.copied:
            continue
        target = dst / PurePosixPath(entry.rel)
        if not target.exists():
            continue
        source = src / PurePosixPath(entry.rel)
        try:
            same = sha256_file(source) == sha256_file(target)
        except OSError:
            same = False
        out.append(Conflict(entry.rel, "identical" if same else "divergent"))
    return tuple(out)


def sibling_path_for(dst: Path, rel: str, *, now: Optional[float] = None) -> Path:
    """Where a divergent file's bytes land at D.

    ``<name>.vco-moved``; if that is taken, a timestamped variant. Never an
    overwrite — the sibling exists precisely because overwriting is forbidden,
    so clobbering a previous sibling would violate the same rule one level
    down.
    """
    base = dst / PurePosixPath(rel)
    candidate = base.with_name(base.name + MOVED_SIBLING_SUFFIX)
    if not candidate.exists():
        return candidate
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now if now is not None else time.time()))
    return base.with_name(f"{base.name}{MOVED_SIBLING_SUFFIX}.{stamp}")


# ───────────────────────────────────────────────────────────────────────────
# Env construction for spawned children (B-as-run #3)
# ───────────────────────────────────────────────────────────────────────────


def projection_owned_keys() -> frozenset[str]:
    """The projection-owned key names, from their canonical home.

    ``vco_lib.config_projection.list_canonical_keys`` OWNS this list; the
    literals in :data:`vco_lib.path_bearing_keys.PROJECTION_OWNED_SCRUB_KEYS`
    are a floor for the case where that import is unavailable, plus the
    non-canonical channels (``KG_SYNC_PROJECT_ROOT``, ``CLAUDE_PROJECT_DIR``)
    the projection does not own but a child must not inherit.
    """
    keys = set(_pbk.PROJECTION_OWNED_SCRUB_KEYS)
    try:
        from vco_lib.config_projection import list_canonical_keys
    except Exception:  # noqa: BLE001 — floor stands; never a hard failure here
        return frozenset(keys)
    keys.update(list_canonical_keys())
    return frozenset(keys)


def build_child_env(
    target_env: Mapping[str, str],
    *,
    base: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Construct the env for a child process the move spawns.

    1. Start from ``base`` (default ``os.environ``) SCRUBBED of every
       projection-owned key. Any of those present in the ambient environment
       belongs to SOME project — possibly the operator's, not the target's.
    2. Overlay the TARGET project's resolved config.

    Unrelated variables (``PATH``, ``HOME``, the user's own exports) pass
    through untouched: this scrubs a named, closed set, it does not build an
    allowlist. A child that lost ``PATH`` would fail in a far more confusing
    way than one that inherited a stale ``KG_COLLECTION``.
    """
    source = dict(os.environ if base is None else base)
    for key in projection_owned_keys():
        source.pop(key, None)
    for key, value in target_env.items():
        if value is None:
            continue
        source[str(key)] = str(value)
    return source


def resolve_target_env(
    project_id: str,
    dst: Path,
    *,
    resolver: Optional[Callable[[str], Mapping[str, str]]] = None,
) -> dict[str, str]:
    """The TARGET project's canonical env, never the operator's.

    Source of record is :func:`vco_lib.config_projection.project_env_from_db` —
    the SAME resolver that WRITES the project's env surfaces, reading
    ``launcher.db`` read-only. Using the writer's own resolver means the child
    sees exactly what the surfaces will say, with no second opinion to drift.

    Fallback is the destination's own ``.claude/env``: file-backed, needs no
    daemon, and inherits nothing from the shell. If both are unavailable the
    result is EMPTY — a child with no project keys fails loudly at its own
    resolver, which is strictly better than a child running with the
    operator's project's keys and silently succeeding against the wrong tree.
    """
    if resolver is not None:
        try:
            return dict(resolver(project_id))
        except Exception:  # noqa: BLE001 — fall through to the file channel
            pass
    elif project_id:
        try:
            from vco_lib.config_projection import project_env_from_db

            bundle = project_env_from_db(project_id)
            return dict(bundle.get("canonical_env") or {})
        except Exception:  # noqa: BLE001 — DB unavailable / project absent
            pass
    env_file = dst / ".claude" / "env"
    if env_file.is_file():
        try:
            from vco_lib.envfile import parse_env_lines

            return dict(parse_env_lines(env_file.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return {}
    return {}


# ───────────────────────────────────────────────────────────────────────────
# Stale-reference scans
# ───────────────────────────────────────────────────────────────────────────

#: Managed surfaces at D that are re-derived by the move and must therefore
#: hold NO reference to the old root once it is done. A hit means a rewrite
#: pass missed an entry class — surfaced, never sed-ed.
VERIFY_SCAN_RELS: tuple[str, ...] = (
    ".claude/env",
    ".claude/settings.json",
    ".env",
    ".vscode/settings.json",
    ".claude/context/UPDATE_DEFERRED.md",
    ".claude/context/UPDATE_DEFERRED.json",
)


def scan_files_for_path(root: Path, needle: str) -> tuple[str, ...]:
    """Managed surfaces under ``root`` that still mention ``needle``.

    Both separator shapes of the needle are searched: a JSON surface written
    on Windows carries ``C:\\\\Proj`` (escaped) while the same value in
    ``.claude/env`` carries ``C:\\Proj``, and a value that round-tripped
    through a POSIX-normalising writer carries ``C:/Proj``. Missing any of
    those would make the verify pass a false negative — the worst outcome for
    a check whose only job is to notice what the rewrite missed.
    """
    if not needle:
        return ()
    variants = {needle, needle.replace("\\", "/"), needle.replace("\\", "\\\\")}
    hits: list[str] = []
    for rel in VERIFY_SCAN_RELS:
        path = root / PurePosixPath(rel)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(v in text for v in variants if v):
            hits.append(rel)
    return tuple(hits)


@dataclass(frozen=True)
class DbSweepHit:
    """One ``launcher.db`` column still carrying the old root."""

    table: str
    column: str
    policy: str
    rows: int

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


def sweep_db_for_path(
    needle: str,
    *,
    db_path: Optional[Path] = None,
    project_id: Optional[str] = None,
) -> tuple[DbSweepHit, ...]:
    """READ-ONLY sweep of every registered TEXT column for ``needle``.

    This is the catch-all that would have caught the field gap: 97 rows across
    two columns kept pointing at the old root and nothing noticed, because
    nothing looked.

    Uses ``instr()``, NOT ``LIKE``. ``LIKE`` treats ``_`` as a single-character
    wildcard, and project folders very often contain underscores — a
    ``LIKE '%/VCO_dev%'`` sweep matches ``/VCOxdev`` and reports rows that do
    not carry the path at all. ``instr`` is the literal-substring primitive
    and needs no escaping dance.

    Matching is case-insensitive via SQLite's ASCII ``lower()`` when the host
    filesystem is case-insensitive, and both separator shapes are probed.
    ``lower()``'s ASCII-only limitation is accepted and named: a path whose
    ONLY difference is the case of a non-ASCII character would be missed, and
    for a best-effort surfacing sweep that is a better trade than shipping a
    custom collation into a database three binaries share.
    """
    from vco_lib.launcher_db_reader import _open_db_readonly

    conn = _open_db_readonly(db_path)
    if conn is None:
        return ()
    fold = _case_insensitive_fs()
    needles = {needle, needle.replace("\\", "/"), needle.replace("/", "\\")}
    needles = {n for n in needles if n}
    hits: list[DbSweepHit] = []
    try:
        live_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for entry in _pbk.PATH_BEARING_DB_COLUMNS:
            if entry.table not in live_tables:
                continue  # older DB without this migration — not an error
            col = f'"{entry.column}"'
            expr = f"lower({col})" if fold else col
            clauses = " OR ".join(
                f"instr({expr}, ?) > 0" for _ in needles
            )
            params: list[Any] = [n.lower() if fold else n for n in sorted(needles)]
            sql = f'SELECT COUNT(*) FROM "{entry.table}" WHERE {col} IS NOT NULL AND ({clauses})'
            if project_id and _has_column(conn, entry.table, "project_id"):
                sql += " AND project_id = ?"
                params.append(project_id)
            try:
                count = int(conn.execute(sql, params).fetchone()[0])
            except Exception:  # noqa: BLE001 — a per-column failure is not fatal
                continue
            if count:
                hits.append(DbSweepHit(entry.table, entry.column, entry.policy, count))
    finally:
        conn.close()
    return tuple(hits)


def _has_column(conn: Any, table: str, column: str) -> bool:
    try:
        return any(
            row[1] == column for row in conn.execute(f'PRAGMA table_info("{table}")')
        )
    except Exception:  # noqa: BLE001
        return False


# ───────────────────────────────────────────────────────────────────────────
# Harness per-path state (M-5) — SURFACE, never migrate
# ───────────────────────────────────────────────────────────────────────────


def harness_slug(folder: "str | Path") -> str:
    """Claude Code's per-project directory name for ``folder``.

    DELEGATES to :func:`vco_lib.project_config.claude_session_dir_for`, which
    is THE home for the slug rule on the Python side — that docstring records
    that inline copies have already drifted once (the RL citation monitor
    handled ``/`` but not ``_``, producing zero-citation telemetry). Taking
    the basename of its result rather than re-deriving keeps this a reader of
    that rule, not a second definition.

    Used for REPORTING only. Nothing in VCO writes under ``~/.claude/``
    (W7 policy) and nothing writes ``~/.claude.json`` (house rule 4).
    """
    from vco_lib.project_config import claude_session_dir_for

    return claude_session_dir_for(_resolve_for_compare(folder)).name


def harness_state_report(src: Path, dst: Path, home: Optional[Path] = None) -> dict:
    """Old/new harness state directories and the COPY commands for them.

    Claude Code keys per-project state by absolute path and none of it follows
    a move: the auto-memory directory and the session transcripts stay keyed
    to S. A plain relocation therefore starts sessions at D with empty memory
    while the S-keyed state lingers orphaned.

    VCO's answer is SURFACING, not silent migration — ``~/.claude/`` is
    harness-owned. The emitted command is a COPY, never a move: the
    zero-data-loss rule applies to the user's memory as much as to their KG.
    """
    base = (home or Path.home()) / ".claude" / "projects"
    old_dir = base / harness_slug(src)
    new_dir = base / harness_slug(dst)
    return {
        "old_slug_dir": str(old_dir),
        "new_slug_dir": str(new_dir),
        "old_present": old_dir.is_dir(),
        "new_present": new_dir.is_dir(),
        # v0.2.92 (R42 sweep): rendered for the LOCAL shell. The literal this
        # replaces was `mkdir -p '<dst>' && cp -rn '<src>/.' '<dst>/'` — three
        # POSIX-isms (`&&`, single quotes, the tools themselves) in the one
        # command that carries a user's auto-memory across a move. Moving a
        # project folder is if anything MORE common on Windows, so this was a
        # remedy printed exactly where it could not be pasted.
        "memory_copy_command": remedy_shell.copy_tree_command(
            old_dir / "memory", new_dir / "memory",
        ),
        "transcripts_note": (
            f"Session transcripts stay under {old_dir} and remain resumable by "
            "opening Claude in the old folder. They are not relocated."
        ),
        "harness_settings_note": (
            "Per-project permission choices recorded in ~/.claude.json are "
            "keyed by the old path. They do NOT carry over and will be "
            "re-prompted at the new folder; the old entry is a harmless orphan."
        ),
    }


# ───────────────────────────────────────────────────────────────────────────
# The plan
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class MovePlan:
    """Everything Phase 0 decided. JSON-serializable for ``--dry-run``/GUI."""

    project_id: str
    project_name: str
    project_slug: str
    src: str
    dst: str
    src_exists: bool
    dst_exists: bool
    dst_empty: bool
    dst_has_manifest: bool
    sources: tuple[SourceFile, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    overlaps: tuple[PathOverlap, ...] = ()
    extra_codegraph_paths_under_src: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def to_copy(self) -> tuple[SourceFile, ...]:
        return tuple(s for s in self.sources if s.copied)

    @property
    def divergent(self) -> tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if c.kind == "divergent")

    @property
    def identical(self) -> tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if c.kind == "identical")

    def to_json(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_slug": self.project_slug,
            "src": self.src,
            "dst": self.dst,
            "src_exists": self.src_exists,
            "dst_exists": self.dst_exists,
            "dst_empty": self.dst_empty,
            "dst_has_manifest": self.dst_has_manifest,
            "counts": {
                "bundle_clean": sum(
                    1 for s in self.sources if s.bucket == "bundle-clean"
                ),
                "user_modified": sum(
                    1 for s in self.sources if s.bucket == "user-modified"
                ),
                "user_adjacent": sum(
                    1 for s in self.sources if s.bucket == "user-adjacent"
                ),
                "to_copy": len(self.to_copy),
                "conflicts_identical": len(self.identical),
                "conflicts_divergent": len(self.divergent),
            },
            "to_copy": [s.rel for s in self.to_copy],
            "conflicts": [
                {"rel": c.rel, "kind": c.kind} for c in self.conflicts
            ],
            "extra_codegraph_paths_under_src": list(
                self.extra_codegraph_paths_under_src
            ),
            "stays_in_old_folder": sorted(NOT_COPIED_SPECS),
            "old_folder_is_kept": True,
            "warnings": list(self.warnings),
        }


def plan_move(
    *,
    project: Mapping[str, Any],
    dst: "str | Path",
    registered: Sequence[Mapping[str, Any]],
    extra_codegraph_paths: Sequence[str] = (),
    into_existing: bool = False,
    from_missing: bool = False,
    platform: Optional[str] = None,
) -> MovePlan:
    """Phase 0: validate, classify, and return the preview. Read-only.

    Raises :class:`MoveRefused` with a DISTINCT reason for each way the
    destination can be wrong. Every refusal happens here, before anything is
    copied — that ordering is the whole "refuse rather than half-move"
    guarantee.
    """
    src_raw = str(project.get("folder_path") or "")
    if not src_raw:
        raise MoveRefused("project_not_found", "the row carries no folder_path")
    dst_path = Path(dst)
    if not dst_path.is_absolute():
        raise MoveRefused("dst_not_absolute", f"got {dst_path}")
    src_path = _resolve_for_compare(src_raw)
    dst_path = _resolve_for_compare(dst_path)

    if dst_path.exists() and not dst_path.is_dir():
        raise MoveRefused("dst_is_file", str(dst_path))
    if not dst_path.exists() and not dst_path.parent.is_dir():
        raise MoveRefused("dst_parent_missing", str(dst_path.parent))

    if paths_equal(src_path, dst_path, platform=platform):
        raise MoveRefused("dst_equals_src", str(dst_path))
    if is_ancestor(src_path, dst_path, platform=platform):
        raise MoveRefused("dst_inside_src", f"{dst_path} is inside {src_path}")
    if is_ancestor(dst_path, src_path, platform=platform):
        raise MoveRefused("dst_contains_src", f"{dst_path} contains {src_path}")

    overlaps = check_path_overlap(
        dst_path,
        registered,
        exclude_project_id=str(project.get("id") or ""),
        platform=platform,
    )
    for overlap in overlaps:
        detail = f"{overlap.project_name} ({overlap.folder_path})"
        if overlap.relation == "same":
            raise MoveRefused("dst_registered_to_another_project", detail)
        if overlap.relation == "inside":
            raise MoveRefused("dst_inside_registered_project", detail)
        raise MoveRefused("dst_contains_registered_project", detail)

    src_exists = src_path.is_dir()
    if not src_exists and not from_missing:
        raise MoveRefused("src_registered_path_missing", str(src_path))

    dst_exists = dst_path.is_dir()
    dst_entries = sorted(p.name for p in dst_path.iterdir()) if dst_exists else []
    dst_empty = not dst_entries
    if dst_exists and not dst_empty and not into_existing:
        preview = ", ".join(dst_entries[:5]) + ("…" if len(dst_entries) > 5 else "")
        raise MoveRefused("dst_not_empty", f"{dst_path} contains: {preview}")

    manifest: Mapping[str, Any] = {"files": {}, "preserved_files": {}}
    if src_exists:
        manifest = _read_manifest_at(src_path)
    sources = classify_move_sources(src_path, manifest) if src_exists else ()
    conflicts = classify_conflicts(sources, src_path, dst_path) if dst_exists else ()

    extra_under_src = tuple(
        p
        for p in extra_codegraph_paths
        if p
        and (
            paths_equal(p, src_path, platform=platform)
            or is_ancestor(src_path, p, platform=platform)
        )
    )

    warnings: list[str] = []
    if not src_exists:
        warnings.append(
            "The registered folder does not exist on disk; nothing can be "
            "copied. This re-points the registration only — the destination "
            "must already hold the project's content."
        )
    if (dst_path / _MANIFEST_REL).is_file():
        warnings.append(
            "The destination already holds a VCO manifest; the bundle step "
            "will run in UPDATE mode there rather than first-install."
        )

    return MovePlan(
        project_id=str(project.get("id") or ""),
        project_name=str(project.get("name") or ""),
        project_slug=str(project.get("slug") or ""),
        src=str(src_path),
        dst=str(dst_path),
        src_exists=src_exists,
        dst_exists=dst_exists,
        dst_empty=dst_empty,
        dst_has_manifest=(dst_path / _MANIFEST_REL).is_file(),
        sources=sources,
        conflicts=conflicts,
        overlaps=overlaps,
        extra_codegraph_paths_under_src=extra_under_src,
        warnings=warnings,
    )


# ───────────────────────────────────────────────────────────────────────────
# Read-only launcher.db access (the engine never writes it)
# ───────────────────────────────────────────────────────────────────────────


def registered_projects(db_path: Optional[Path] = None) -> list[dict]:
    """Every registered project row. Read-only (``mode=ro``).

    Lives in the ENGINE rather than in the CLI because the launcher drives the
    same planning step through ``--phase plan``: one producer, so a GUI
    preview and a CLI ``--dry-run`` can never disagree about what a move would
    do.
    """
    from vco_lib.launcher_db_reader import _open_db_readonly

    conn = _open_db_readonly(db_path)
    if conn is None:
        return []
    try:
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "slug": r["slug"],
                "folder_path": r["folder_path"],
            }
            for r in conn.execute(
                "SELECT id, name, slug, folder_path FROM projects ORDER BY id"
            )
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def extra_codegraph_paths(
    project_id: str, db_path: Optional[Path] = None
) -> list[str]:
    """The project's user-designated extra code-graph roots. Read-only."""
    from vco_lib.launcher_db_reader import _open_db_readonly

    conn = _open_db_readonly(db_path)
    if conn is None:
        return []
    try:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT path FROM project_codegraph_extra_paths WHERE project_id = ?",
                (project_id,),
            )
        ]
    except Exception:  # noqa: BLE001 — table absent on an older DB
        return []
    finally:
        conn.close()


def resolve_project_row(
    selector: str, rows: Sequence[Mapping[str, Any]]
) -> Optional[dict]:
    """Find a project by id or slug. EXACT match only — a fuzzy match on the
    selector for an operation that rewrites paths is not a convenience."""
    for row in rows:
        if row.get("id") == selector or row.get("slug") == selector:
            return dict(row)
    return None


def plan_for_selector(
    selector: str,
    dst: "str | Path",
    *,
    into_existing: bool = False,
    from_missing: bool = False,
    db_path: Optional[Path] = None,
) -> MovePlan:
    """Load the project row and plan the move. The ONE planning entry point.

    Raises :class:`MoveRefused` with ``project_not_found`` when the selector
    matches nothing, and whatever :func:`plan_move` raises otherwise.
    """
    rows = registered_projects(db_path)
    project = resolve_project_row(selector, rows)
    if project is None:
        raise MoveRefused("project_not_found", f"selector '{selector}'")
    return plan_move(
        project=project,
        dst=dst,
        registered=rows,
        extra_codegraph_paths=extra_codegraph_paths(project["id"], db_path),
        into_existing=into_existing,
        from_missing=from_missing,
    )


def _read_manifest_at(folder: Path) -> dict:
    """Read ``<folder>/.claude/.vco-manifest.json``, tolerating absence.

    A local reader rather than importing ``project_init._read_manifest``:
    ``project_init`` is a 14k-line module whose import pulls in the whole
    bundle engine, and the move's Phase-0 preview must stay a cheap read.
    The FORMAT has one owner (the schema comment in ``project_init``); this
    is a reader of that format, not a second definition of it.
    """
    target = folder / _MANIFEST_REL
    empty = {"files": {}, "preserved_files": {}}
    if not target.is_file():
        return dict(empty)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — corrupt manifest reads as absent
        return dict(empty)
    if not isinstance(data, dict):
        return dict(empty)
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    if not isinstance(data.get("preserved_files"), dict):
        data["preserved_files"] = {}
    return data


# ───────────────────────────────────────────────────────────────────────────
# Sentinel
# ───────────────────────────────────────────────────────────────────────────


def write_sentinel(folder: Path, payload: Mapping[str, Any]) -> None:
    """Write/advance the in-progress sentinel under ``folder``. Soft-fail."""
    try:
        target = folder / SENTINEL_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(dict(payload), indent=2) + "\n")
    except OSError:
        pass


def read_sentinel(folder: Path) -> Optional[dict]:
    target = folder / SENTINEL_REL
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def clear_sentinel(folder: Path) -> None:
    try:
        (folder / SENTINEL_REL).unlink()
    except OSError:
        pass


def _sentinel_payload(plan: MovePlan, phase: str, move_id: str) -> dict:
    return {
        "schema": 1,
        "move_id": move_id,
        "project_id": plan.project_id,
        "src": plan.src,
        "dst": plan.dst,
        "phase": phase,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ───────────────────────────────────────────────────────────────────────────
# Phases 1–4 (PRE-COMMIT) and 6–7 (POST-COMMIT)
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class PhaseResult:
    """What a phase group did. Buckets are always present (never None) so a
    parse failure downstream is distinguishable from an empty result — the
    same all-empty-buckets soft-fail shape the bundle envelope uses."""

    copied: list[str] = field(default_factory=list)
    siblings: list[dict] = field(default_factory=list)
    skipped_identical: list[str] = field(default_factory=list)
    bundle: dict = field(default_factory=dict)
    git_exclude: dict = field(default_factory=dict)
    deferrals: list[str] = field(default_factory=list)
    stale_file_hits: list[str] = field(default_factory=list)
    stale_db_hits: list[dict] = field(default_factory=list)
    harness: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "copied": self.copied,
            "siblings": self.siblings,
            "skipped_identical": self.skipped_identical,
            "bundle": self.bundle,
            "git_exclude": self.git_exclude,
            "deferrals": self.deferrals,
            "stale_file_hits": self.stale_file_hits,
            "stale_db_hits": self.stale_db_hits,
            "harness": self.harness,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def execute_pre_flip(
    plan: MovePlan,
    *,
    move_id: str = "",
    safe_add: bool = False,
    orchestrator_root: Optional[Path] = None,
    run_bundle: bool = True,
    runner: Optional[Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess]] = None,
) -> PhaseResult:
    """Phases 1–4. Additive at D, S untouched, DB untouched.

    Any exception raised here means the move refuses: nothing has been
    committed, so the project is exactly where it was.
    """
    src = Path(plan.src)
    dst = Path(plan.dst)
    result = PhaseResult()
    divergent = {c.rel for c in plan.divergent}
    identical = {c.rel for c in plan.identical}

    dst.mkdir(parents=True, exist_ok=True)
    write_sentinel(src, _sentinel_payload(plan, "copy", move_id))
    write_sentinel(dst, _sentinel_payload(plan, "copy", move_id))

    # ── Phase 1: copy ────────────────────────────────────────────────────
    for entry in plan.to_copy:
        rel = entry.rel
        source = src / PurePosixPath(rel)
        if not source.is_file():
            continue
        if rel in identical:
            result.skipped_identical.append(rel)
            continue
        if rel in divergent:
            sibling = sibling_path_for(dst, rel)
            sibling.parent.mkdir(parents=True, exist_ok=True)
            atomic_copy_file(source, sibling)
            result.siblings.append(
                {
                    "rel": rel,
                    "sibling": to_posix_rel(sibling.relative_to(dst)),
                    "origin": str(source),
                }
            )
            continue
        target = dst / PurePosixPath(rel)
        if target.exists():
            # Appeared between plan and execute. The zero-overwrite rule is
            # absolute, so treat it as a late divergence rather than racing.
            sibling = sibling_path_for(dst, rel)
            sibling.parent.mkdir(parents=True, exist_ok=True)
            atomic_copy_file(source, sibling)
            result.siblings.append(
                {
                    "rel": rel,
                    "sibling": to_posix_rel(sibling.relative_to(dst)),
                    "origin": str(source),
                    "late": True,
                }
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy_file(source, target)
        result.copied.append(rel)

    # ── Phase 2: bundle at D ─────────────────────────────────────────────
    write_sentinel(src, _sentinel_payload(plan, "bundle", move_id))
    write_sentinel(dst, _sentinel_payload(plan, "bundle", move_id))
    if run_bundle:
        result.bundle = _run_bundle_at(
            plan,
            dst,
            safe_add=safe_add,
            orchestrator_root=orchestrator_root,
            runner=runner,
        )
        if result.bundle.get("parse_failed"):
            result.warnings.append(
                "The bundle step's --json envelope could not be parsed; its "
                "per-file actions are unknown. The git-exclude step below "
                "therefore has no created-file list and is skipped rather "
                "than guessing a blanket glob."
            )

    # ── Phase 4: git hygiene at D ────────────────────────────────────────
    write_sentinel(src, _sentinel_payload(plan, "git-hygiene", move_id))
    write_sentinel(dst, _sentinel_payload(plan, "git-hygiene", move_id))
    if (dst / ".git").is_dir() and result.bundle and not result.bundle.get("parse_failed"):
        entries = _git_exclude.safe_add_exclude_entries(result.bundle, dst)
        # The copied user-material is VCO-adjacent too — exclude what we put
        # there, not only what the bundle created.
        entries = list(
            dict.fromkeys(
                entries
                + _git_exclude.exclude_entries_for_created_paths(result.copied, dst)
            )
        )
        result.git_exclude = _git_exclude.append_git_info_exclude(
            dst,
            tuple(entries),
            block_comment=[
                "# VCO project move: keep orchestrator-created files out of "
                "your commits.",
                "# This is .git/info/exclude (LOCAL-only) — not the tracked "
                ".gitignore.",
            ],
        )
    return result


def _run_bundle_at(
    plan: MovePlan,
    dst: Path,
    *,
    safe_add: bool,
    orchestrator_root: Optional[Path],
    runner: Optional[Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess]],
) -> dict:
    """Run ``install-bundle`` at D and return its envelope.

    FIRST-INSTALL mode unless D already carries a manifest (D4 rule). That
    ordering — copy user material first, bundle second — is what keeps the
    bundle engine UNMODIFIED: every Phase-1 copy is protected by the
    classifier's ``skip-existing`` branch and lands in the fresh manifest's
    ``preserved_files``, while every bundle-clean file re-materializes with
    D-substituted transforms.
    """
    root = orchestrator_root or _orchestrator_root_guess()
    # v0.2.94: the ONE resolver, not `sys.executable` — a move can be driven
    # from the launcher, whose bundle path spawns Python via a bare PATH probe.
    from vco_lib.python_exe import resolve_or_current

    argv = [
        resolve_or_current(install_root=root),
        "-m",
        "vco_lib.project_init",
        "install-bundle",
        "--folder",
        str(dst),
        "--json",
    ]
    if root is not None:
        argv += ["--templates", str(Path(root) / "templates")]
    if plan.dst_has_manifest:
        argv.append("--update")
    if safe_add:
        argv.append("--safe-add")

    env = build_child_env(
        {
            **resolve_target_env(plan.project_id, dst),
            "CLAUDE_PROJECT_DIR": str(dst),
            "KG_BASE_DIR": str(dst),
            "KG_SYNC_PROJECT_ROOT": str(dst),
        }
    )
    try:
        proc = (
            runner(argv, env)
            if runner is not None
            else subprocess.run(
                argv, env=env, capture_output=True, text=True, check=False
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise MoveError(f"bundle step could not be spawned: {exc}") from exc
    if proc.returncode != 0:
        raise MoveError(
            f"bundle step failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[-800:]}"
        )
    try:
        envelope = json.loads(proc.stdout or "{}")
        if not isinstance(envelope, dict):
            raise ValueError("envelope is not an object")
        return envelope
    except Exception:  # noqa: BLE001 — soft-fail shape, per the D2 contract
        return {"actions": {}, "warnings": [], "errors": [], "parse_failed": True}


def _orchestrator_root_guess() -> Optional[Path]:
    """The orchestrator root, from the env channel then the checkout layout.

    No hardcoded machine shape: ``VCT_INSTALL_ROOT`` is the launcher-provided
    canonical value, and the package-relative fallback is only valid when this
    module is running FROM a checkout (``install.py`` present), which is the
    same discriminator the hooks' venv ladder uses.
    """
    env_root = os.environ.get("VCT_INSTALL_ROOT") or os.environ.get(
        "VCT_ORCHESTRATOR_ROOT"
    )
    if env_root and (Path(env_root) / "templates").is_dir():
        return Path(env_root)
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "install.py").is_file() and (candidate / "templates").is_dir():
        return candidate
    return None


def execute_post_flip(
    plan: MovePlan,
    *,
    move_id: str = "",
    run_kg_sync: bool = True,
    codegraph_enqueued: bool = True,
    db_path: Optional[Path] = None,
    home: Optional[Path] = None,
    runner: Optional[Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess]] = None,
) -> PhaseResult:
    """Phases 6–7. Runs AFTER the DB commit; every step is idempotent.

    A failure here does NOT undo the flip. The project is at D and works; the
    reconciliation that is owed can be re-run with ``vco project move
    --verify``, and the ``project_moves`` row stays ``flipped`` until it is.
    """
    src = Path(plan.src)
    dst = Path(plan.dst)
    result = PhaseResult()
    write_sentinel(dst, _sentinel_payload(plan, "post-flip", move_id))

    # Env projection at D, derived from the NEW row.
    try:
        _reproject_env(plan, dst, runner=runner)
    except Exception as exc:  # noqa: BLE001 — surfaced, never fatal
        result.warnings.append(f"env re-projection failed: {exc}")

    # kg-sync parity pass. Embeddings are reused via content_hash and stored
    # node `file_path` values are PROJECT_ROOT-relative, so this is metadata
    # parity, not a re-embed. Weaviate down is not the move's problem — the
    # sync's own deferral + retry machinery owns it.
    if run_kg_sync:
        try:
            _run_kg_sync(plan, dst, runner=runner)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"kg-sync parity pass did not complete: {exc}")

    # VERIFY scans.
    result.stale_file_hits = list(scan_files_for_path(dst, plan.src))
    result.stale_db_hits = [
        {
            "table": h.table,
            "column": h.column,
            "policy": h.policy,
            "rows": h.rows,
            "expected": h.policy == _pbk.POLICY_HISTORICAL,
        }
        for h in sweep_db_for_path(plan.src, db_path=db_path)
    ]
    result.harness = harness_state_report(src, dst, home=home)

    result.deferrals = _emit_move_deferrals(
        plan,
        dst,
        stale_file_hits=result.stale_file_hits,
        stale_db_hits=result.stale_db_hits,
        harness=result.harness,
        codegraph_enqueued=codegraph_enqueued,
    )

    clear_sentinel(src)
    clear_sentinel(dst)
    return result


def _reproject_env(
    plan: MovePlan,
    dst: Path,
    *,
    runner: Optional[Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess]],
) -> None:
    """Re-run the canonical env projection against the NEW row.

    The projection derives every value from DB state, so a re-run after the
    flip rewrites ``KG_BASE_DIR`` and friends without this module ever owning
    a sed list. That is why :data:`PATH_BEARING_ENV_KEYS` is a drift GATE
    rather than a rewrite driver.
    """
    from vco_lib.python_exe import resolve_or_current  # v0.2.94: ONE resolver

    argv = [
        resolve_or_current(),
        "-m",
        "vco_lib.config_projection",
        "apply",
        "--project-id",
        plan.project_id,
        "--folder",
        str(dst),
    ]
    env = build_child_env(
        {"CLAUDE_PROJECT_DIR": str(dst), "KG_BASE_DIR": str(dst)}
    )
    proc = (
        runner(argv, env)
        if runner is not None
        else subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    )
    if proc.returncode != 0:
        raise MoveError((proc.stderr or "").strip()[-400:] or f"exit {proc.returncode}")


def _run_kg_sync(
    plan: MovePlan,
    dst: Path,
    *,
    runner: Optional[Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess]],
) -> None:
    """``kg-sync --all --project-root D`` through the project's own wrapper.

    ``--project-root`` is the sync script's HIGHEST-precedence channel
    (v0.2.89 ladder), pinned in argv as belt-and-braces on top of the
    constructed env: argv beats env beats file, and a move must not depend on
    which of the three the operator's machine happens to have.
    """
    wrapper = dst / ".claude" / "scripts" / ("kg-sync.ps1" if os.name == "nt" else "kg-sync")
    if not wrapper.exists():
        return
    argv = (
        ["pwsh", "-NoProfile", "-File", str(wrapper)]
        if wrapper.suffix == ".ps1"
        else [str(wrapper)]
    ) + ["--all", "--project-root", str(dst)]
    env = build_child_env(
        {
            **resolve_target_env(plan.project_id, dst),
            "KG_SYNC_PROJECT_ROOT": str(dst),
            "CLAUDE_PROJECT_DIR": str(dst),
        }
    )
    proc = (
        runner(argv, env)
        if runner is not None
        else subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    )
    if proc.returncode != 0:
        raise MoveError((proc.stderr or "").strip()[-400:] or f"exit {proc.returncode}")


# ───────────────────────────────────────────────────────────────────────────
# Deferrals
# ───────────────────────────────────────────────────────────────────────────


def _sanitize_rel(rel: str) -> str:
    """A condition-id-safe slug for a relative path."""
    out = "".join(ch if ch.isalnum() else "_" for ch in to_posix_rel(rel))
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_").lower()[:80] or "file"


def _dismiss_command(folder: Path, cid: str) -> str:
    """The dismissal command for an entry at ``folder``.

    Rooted at the NEW folder deliberately (M-4): an entry whose command names
    the OLD root dismisses in the OLD ledger, which is how a moved project's
    agent ends up "resolving" entries nobody will ever read again.
    """
    return (
        f"python -m vco_lib.project_init dismiss-deferral "
        f"--folder '{folder}' --condition-id {cid}"
    )


def _codegraph_wrapper(folder: Path) -> str:
    """The code-graph analyzer command for ``folder``, as a real path.

    There is no ``vco codegraph`` verb — the `vco` CLI ships
    ``verify-pins``, ``verify-env-projection``, ``verify-diagrams``,
    ``rebuild-diagram-index``, ``codegraph-diagram``, ``doctor`` and
    ``project``. The analyzer ships as the BUNDLED wrapper in the project's
    own ``.claude/scripts/``, so that is what the remediation names.

    A printed command is shipped code. An earlier draft of this module
    emitted ``vco codegraph analyze <path>``, which parses as an unknown
    subcommand and exits 2 — a remediation that cannot work is worse than
    none, because the user spends their attention discovering that.
    """
    name = "code-graph-analyze.ps1" if os.name == "nt" else "code-graph-analyze"
    return str(folder / ".claude" / "scripts" / name)


def _emit_move_deferrals(
    plan: MovePlan,
    dst: Path,
    *,
    stale_file_hits: Sequence[str],
    stale_db_hits: Sequence[Mapping[str, Any]],
    harness: Mapping[str, Any],
    codegraph_enqueued: bool,
) -> list[str]:
    """Emit every ledger entry the move owes, at D. Returns the condition ids."""
    from vco_lib.deferral_emit import emit_entries
    from vco_lib.deferral_report import DeferralEntry

    entries: list[DeferralEntry] = []
    # IDENTITY, carried from the project ROW — never `dst.name`. The analyzer's
    # `--project` selects the collection family, so deriving it from the new
    # folder's basename would walk the project's code into a DIFFERENT
    # collection: the precise failure this whole package is designed against.
    project_name = plan.project_name or plan.project_id

    # Per-file conflicts — the merge is delegated to the project's own agent.
    for conflict in plan.divergent:
        cid = f"{CID_CONFLICT_PREFIX}{_sanitize_rel(conflict.rel)}"
        sibling = f"{conflict.rel}{MOVED_SIBLING_SUFFIX}"
        entries.append(
            DeferralEntry(
                condition_id=cid,
                title=f"Move conflict: {conflict.rel}",
                detected=(
                    f"The file already existed here with different content, so "
                    f"nothing was overwritten. Three things now exist: this "
                    f"folder's live `{conflict.rel}`, the previous folder's "
                    f"version copied beside it as `{sibling}`, and the "
                    f"original at `{Path(plan.src) / PurePosixPath(conflict.rel)}`."
                ),
                why_deferred=(
                    "Merging two versions of a file needs judgement about "
                    "which changes matter. VCO never merges user content."
                ),
                command_to_apply=_dismiss_command(dst, cid),
                severity="warning",
            )
        )

    # Stale references the rewrite did not reach.
    if stale_file_hits:
        entries.append(
            DeferralEntry(
                condition_id=CID_STALE_PATH_REFERENCE,
                title="Managed files still mention the previous project folder",
                detected=(
                    "After the move, these managed surfaces still contain the "
                    f"old folder path `{plan.src}`: "
                    + ", ".join(stale_file_hits)
                    + ". They were NOT edited — a hit here means a rewrite "
                    "pass missed an entry class, and rewriting the file by "
                    "hand would hide that."
                ),
                why_deferred=(
                    "VCO rewrites managed values by RE-DERIVING them from the "
                    "database, never by editing text. A surface it cannot "
                    "re-derive is surfaced instead of patched."
                ),
                command_to_apply=(
                    f"vco project move --verify --folder '{dst}'   "
                    "# re-scans and clears this entry when no reference remains"
                ),
                severity="warning",
            )
        )

    actionable_db = [
        h
        for h in stale_db_hits
        if h.get("policy") in _pbk.ACTIONABLE_POLICIES
        and h.get("policy") != _pbk.POLICY_USER_OWNED_FLAG
    ]
    if actionable_db:
        listing = ", ".join(
            f"{h['table']}.{h['column']} ({h['rows']} rows, policy {h['policy']})"
            for h in actionable_db
        )
        entries.append(
            DeferralEntry(
                condition_id=CID_STALE_DB_PATH,
                title="Launcher database rows still carry the previous folder path",
                detected=(
                    f"A read-only sweep found the old path `{plan.src}` in: "
                    f"{listing}. Columns classified `historical` are expected "
                    "to carry it and are not listed."
                ),
                why_deferred=(
                    "A hit in a column with a fixer means the fixer did not "
                    "reach these rows; a hit in a column classified "
                    "`not-path-bearing` means the path-bearing registry "
                    "itself is incomplete. Both need a human to look, and "
                    "neither is safe to string-replace."
                ),
                command_to_apply=_dismiss_command(dst, CID_STALE_DB_PATH),
                severity="warning",
            )
        )

    # User-owned extra code-graph paths: surfaced, never rewritten.
    for raw in plan.extra_codegraph_paths_under_src:
        cid = f"{CID_EXTRA_CODEGRAPH_PREFIX}{_sanitize_rel(raw)}"
        entries.append(
            DeferralEntry(
                condition_id=cid,
                title="Extra code-graph path points inside the previous folder",
                detected=(
                    f"This project's code graph also indexes `{raw}`, which is "
                    f"inside the previous folder `{plan.src}`. It was left "
                    "exactly as it was."
                ),
                why_deferred=(
                    "You chose that path. It may deliberately point at the "
                    "old checkout — re-pointing it automatically would "
                    "silently change what your code graph indexes."
                ),
                command_to_apply=_dismiss_command(dst, cid),
                severity="info",
            )
        )

    # Code-graph rebuild. auto_retryable ONLY when work was actually scheduled.
    if codegraph_enqueued:
        entries.append(
            DeferralEntry(
                condition_id=CID_CODEGRAPH_REANALYZE,
                title="Code graph will be rebuilt at the new folder",
                detected=(
                    "A pending code-graph build was queued for this project so "
                    "its entities are re-walked from the new folder."
                ),
                why_deferred=(
                    "The build runs on the launcher's next build pass rather "
                    "than blocking the move."
                ),
                command_to_apply=(
                    "# Nothing to do — the queued build clears this entry.\n"
                    "# To run it now instead of waiting for the launcher:\n"
                    f"'{_codegraph_wrapper(dst)}' '{dst}' --project '{project_name}'"
                ),
                severity="info",
                disposition="auto_retryable",
            )
        )
    else:
        entries.append(
            DeferralEntry(
                condition_id=CID_CODEGRAPH_REANALYZE,
                title="Code graph rebuild could NOT be queued",
                detected=(
                    "The move could not write a pending build row, so no "
                    "rebuild is scheduled. Until one runs, the code graph "
                    "describes the previous folder."
                ),
                why_deferred=(
                    "Queuing the build failed. Calling this 'VCO will retry' "
                    "would be false: nothing is scheduled."
                ),
                command_to_apply=(
                    f"'{_codegraph_wrapper(dst)}' '{dst}' "
                    f"--project '{project_name}'"
                ),
                severity="warning",
                disposition="action_required",
            )
        )

    # Retention record.
    entries.append(
        DeferralEntry(
            condition_id=CID_OLD_FOLDER_RETAINED,
            title="The previous project folder was kept",
            detected=(
                f"Nothing was deleted. The previous folder `{plan.src}` is "
                "still on disk with all of its content, including anything "
                "VCO did not copy."
            ),
            why_deferred=(
                "VCO does not delete a folder it did not create. Remove it "
                "yourself once you have confirmed nothing is missing here."
            ),
            command_to_apply=(
                f"vco project move --verify --folder '{dst}'   "
                "# clears this record once the previous folder is gone"
            ),
            severity="info",
            disposition="informational_record",
        )
    )

    # Harness state — ONLY when the old slug dir exists.
    if harness.get("old_present"):
        entries.append(
            DeferralEntry(
                condition_id=CID_HARNESS_STATE_REVIEW,
                title="Claude Code session state did not follow this move",
                detected=(
                    "Claude Code keys per-project memory and transcripts by "
                    f"absolute path. Yours are under `{harness['old_slug_dir']}`; "
                    f"sessions here will use `{harness['new_slug_dir']}`. "
                    + str(harness.get("transcripts_note", ""))
                    + " "
                    + str(harness.get("harness_settings_note", ""))
                ),
                why_deferred=(
                    "`~/.claude/` belongs to Claude Code, not to VCO. VCO "
                    "shows you the copy command rather than writing there."
                ),
                command_to_apply=str(harness.get("memory_copy_command", "")),
                severity="warning",
            )
        )

    emit_entries(dst, entries)
    return [e.condition_id for e in entries]


# ───────────────────────────────────────────────────────────────────────────
# verify — the paired-resolution site
# ───────────────────────────────────────────────────────────────────────────


def pending_codegraph_build(
    project_id: str, *, db_path: Optional[Path] = None
) -> Optional[bool]:
    """Whether a pending/running ``code_graph_builds`` row exists (tri-state).

    ``True`` — work is still queued or in flight.
    ``False`` — nothing outstanding; the queued build has been consumed.
    ``None`` — could not determine (no DB, no table, query failed). ``None``
    must NOT collapse into either answer: "I cannot see the queue" is not
    "the queue is empty", and treating it as empty would clear a deferral
    whose work never ran.
    """
    if not project_id:
        return None
    from vco_lib.launcher_db_reader import _open_db_readonly

    conn = _open_db_readonly(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM code_graph_builds "
            "WHERE project_id = ? AND status IN ('pending','running')",
            (project_id,),
        ).fetchone()
        return int(rows[0]) > 0
    except Exception:  # noqa: BLE001 — missing table on an older DB ⇒ unknown
        return None
    finally:
        conn.close()


def verify_move(
    folder: Path,
    *,
    old_path: Optional[str] = None,
    project_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Re-run the move's verification at ``folder`` and clear what is clean.

    This is the CLEAR MECHANISM the ledger entries name (``paired-resolution``):
    it re-scans, and calls ``resolve_conditions`` for each condition that no
    longer holds. Idempotent and read-only apart from the ledger.

    ``old_path`` defaults to the sentinel's ``src`` when one is still present,
    which is what makes an interrupted move resumable without the user having
    to remember where the project used to live.
    """
    from vco_lib.deferral_emit import resolve_conditions

    sentinel = read_sentinel(folder)
    src = old_path or (sentinel or {}).get("src") or ""
    pid = project_id or (sentinel or {}).get("project_id") or ""
    report: dict = {
        "folder": str(folder),
        "old_path": src,
        "project_id": pid,
        "sentinel_phase": (sentinel or {}).get("phase"),
        "stale_file_hits": [],
        "stale_db_hits": [],
        "resolved": [],
        "old_folder_present": None,
        "codegraph_build_outstanding": None,
    }
    if not src:
        return report

    file_hits = list(scan_files_for_path(folder, src))
    db_hits = [
        {"table": h.table, "column": h.column, "policy": h.policy, "rows": h.rows}
        for h in sweep_db_for_path(src, db_path=db_path)
    ]
    report["stale_file_hits"] = file_hits
    report["stale_db_hits"] = db_hits
    report["old_folder_present"] = Path(src).is_dir()

    outstanding = pending_codegraph_build(pid, db_path=db_path)
    report["codegraph_build_outstanding"] = outstanding

    to_resolve: list[str] = []
    if not file_hits:
        to_resolve.append(CID_STALE_PATH_REFERENCE)
    if not [h for h in db_hits if h["policy"] in _pbk.ACTIONABLE_POLICIES]:
        to_resolve.append(CID_STALE_DB_PATH)
    if not report["old_folder_present"]:
        to_resolve.append(CID_OLD_FOLDER_RETAINED)
    # Tri-state, deliberately: only an explicit False clears. `None` means the
    # queue could not be read, and clearing on "I do not know" is how a
    # deferral disappears while its work never ran.
    if outstanding is False:
        to_resolve.append(CID_CODEGRAPH_REANALYZE)
    if to_resolve:
        resolve_conditions(folder, to_resolve)
        report["resolved"] = to_resolve
    if not file_hits and sentinel is not None:
        clear_sentinel(folder)
    return report


# ───────────────────────────────────────────────────────────────────────────
# CLI — the subprocess target for the Tauri command
# ───────────────────────────────────────────────────────────────────────────


def _envelope(ok: bool, **payload: Any) -> dict:
    out = {"ok": ok, "schema": 1}
    out.update(payload)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m vco_lib.project_move`` — the file-phase engine.

    Deliberately NOT the user-facing command (that is ``vco project move``,
    which owns the DB round-trips). This entry point runs ONE phase group and
    prints ONE JSON envelope, so the launcher can drive the same engine the
    CLI drives without either re-implementing the phases.
    """
    parser = argparse.ArgumentParser(prog="python -m vco_lib.project_move")
    parser.add_argument("--plan-json", help="A MovePlan rendered by `vco project move --dry-run`.")
    parser.add_argument(
        "--phase", choices=("plan", "pre-flip", "post-flip", "verify"), required=True
    )
    parser.add_argument("--move-id", default="")
    parser.add_argument("--folder", help="For --phase verify: the project folder.")
    parser.add_argument("--old-path", default=None)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--to", default=None, help="For --phase plan: the destination.")
    parser.add_argument("--into-existing", action="store_true")
    parser.add_argument("--from-missing", action="store_true")
    parser.add_argument("--safe-add", action="store_true")
    parser.add_argument(
        "--codegraph-enqueued",
        choices=("true", "false"),
        default="true",
        help="Whether the committing writer actually queued a build row.",
    )
    args = parser.parse_args(argv)

    try:
        if args.phase == "plan":
            if not args.project_id or not args.to:
                raise MoveError("--phase plan requires --project-id and --to")
            plan = plan_for_selector(
                args.project_id,
                args.to,
                into_existing=args.into_existing,
                from_missing=args.from_missing,
            )
            print(json.dumps(_envelope(True, plan=plan.to_json())))
            return 0

        if args.phase == "verify":
            if not args.folder:
                raise MoveError("--phase verify requires --folder")
            report = verify_move(
                Path(args.folder),
                old_path=args.old_path,
                project_id=args.project_id or None,
            )
            print(json.dumps(_envelope(True, verify=report)))
            return 0

        if not args.plan_json:
            raise MoveError(f"--phase {args.phase} requires --plan-json")
        raw = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        plan = _plan_from_json(raw)
        if args.phase == "pre-flip":
            result = execute_pre_flip(plan, move_id=args.move_id, safe_add=args.safe_add)
        else:
            result = execute_post_flip(
                plan,
                move_id=args.move_id,
                codegraph_enqueued=args.codegraph_enqueued == "true",
            )
        print(json.dumps(_envelope(True, phase=args.phase, result=result.to_json())))
        return 0
    except MoveRefused as exc:
        print(json.dumps(_envelope(False, refused=exc.reason, error=str(exc))))
        return 3
    except Exception as exc:  # noqa: BLE001 — one envelope, always
        print(json.dumps(_envelope(False, error=f"{type(exc).__name__}: {exc}")))
        return 1


def _plan_from_json(raw: Mapping[str, Any]) -> MovePlan:
    """Rehydrate a plan for the execute phases.

    Only the fields the execute phases READ are rehydrated; the counts and
    previews in the JSON are for humans and the GUI.
    """
    conflicts = tuple(
        Conflict(str(c["rel"]), str(c["kind"])) for c in raw.get("conflicts", [])
    )
    conflict_kinds = {c.rel: c.kind for c in conflicts}
    sources = tuple(
        SourceFile(
            rel,
            "user-adjacent" if conflict_kinds.get(rel) else "user-modified",
            "rehydrated from plan JSON",
        )
        for rel in raw.get("to_copy", [])
    )
    return MovePlan(
        project_id=str(raw.get("project_id") or ""),
        project_name=str(raw.get("project_name") or ""),
        project_slug=str(raw.get("project_slug") or ""),
        src=str(raw["src"]),
        dst=str(raw["dst"]),
        src_exists=bool(raw.get("src_exists", True)),
        dst_exists=bool(raw.get("dst_exists", False)),
        dst_empty=bool(raw.get("dst_empty", True)),
        dst_has_manifest=bool(raw.get("dst_has_manifest", False)),
        sources=sources,
        conflicts=conflicts,
        extra_codegraph_paths_under_src=tuple(
            raw.get("extra_codegraph_paths_under_src", [])
        ),
        warnings=list(raw.get("warnings", [])),
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
