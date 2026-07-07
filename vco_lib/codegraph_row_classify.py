# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE row-level convergence classifier for code-graph rows (v0.2.75 P1b).

Why this module exists
----------------------
The resync owed-probe (``vco_lib/codegraph_resync.py::_count_stale_in_collection``)
and the analyzer's orphan-clear predicate
(``templates/scripts/analyze_code_graph.py::_build_stale_file_set``) each grew
their own partial notion of "which stale rows can a re-walk actually
converge?". Every class the two disagreed on became an IMMORTAL convergence
loop: the probe counted the row as owed forever while nothing could ever
re-stamp or delete it, so every ``install.py --update`` re-triggered a
whole-repo resync. Live-confirmed immortal classes:

* **pathless rows** in the file-anchored collections (empty/missing
  ``path``/``file_path``): the stale-file set skipped them (``if fp:``) and
  the orphan-clear kept them (``if not raw_path: return False``) — never
  re-walked, never purged, counted owed forever.
* **ignore-set rows whose file still exists** (e.g. a ``target/`` or
  ``coverage/`` file indexed before the walk exclusions shipped): reachable
  on disk → kept and counted, but the walk never descends there → never
  re-stamped.

This module is the single source of truth for that decision:
:func:`classify_row` returns ``"owed"`` / ``"not_owed"`` / ``"purgeable"``
and BOTH consumers route through it (the analyzer via a byte-identical
mirrored copy — see below).

Sharing mechanism (template boundary)
-------------------------------------
``analyze_code_graph.py`` is a TEMPLATE script copied into user projects'
``.claude/scripts/`` where ``vco_lib`` does not exist — it cannot import this
module (same reason ``codegraph_resync`` regex-parses the analyzer's
``CODEGRAPH_EMBED_REVISION`` instead of importing it). So the analyzer
carries a MIRRORED copy of the classifier functions between the
BEGIN/END ``MUST-MATCH codegraph-row-classify`` marker comments, and
``tests/test_codegraph_row_classify_parity.py`` byte-compares the marked
regions (plus value-compares the constants) so the two can never drift —
the parity test IS the lock.

The vectorless / rev-0 ruling (documented decision, v0.2.75)
------------------------------------------------------------
Rows with ``embed_revision == 0`` (vectorless sentinel R-2, or the R-3
module-row invalidation stamp) are classified **owed** when reachable and
non-ignored: a re-walk CAN heal them (the per-file gate already re-walks
rev-0 files — verified in source; the live pair that never healed was a
deterministic PowerShell parser crash, fixed alongside this module, NOT a
gate bug). They follow the same purgeable rules as any other stale row when
pathless / ignored / unreachable. No special-casing by value: ``0`` is simply
"not the current revision".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "CODEGRAPH_IGNORE_PARTS",
    "CODEGRAPH_SKIP_SUFFIXES",
    "TRANSIENT_STATE_MARKER",
    "classify_row",
    "path_is_ignored",
    "path_reachable_on_disk",
]

# ---------------------------------------------------------------------------
# The ONE derived ignore-set.
#
# Union of the analyzer's `_COMMON_IGNORE_DIRS` and EVERY
# `_LANGUAGE_IGNORE_DIRS_EXTRAS` entry (vendor/target/.gradle/.bundle/obj/
# bin/.vs/coverage). v0.2.75 (P1b-2): the per-language application of the
# extras made the walk and the prune/probe disagree per row class — e.g. a
# `.py` file under `coverage/` was WALKED (python's set lacked `coverage`)
# while the resync prune deleted its rows, a delete/recreate churn; and a
# stale row under `target/` was counted owed forever because no walk ever
# descended there. One union set, applied by the walk, the resync prune AND
# the classifier, removes the whole divergence class. Deliberate trade-off:
# directories like `bin/` or `vendor/` are now excluded for EVERY language
# (they are build-output / vendored-dep dirs in the ecosystems that use
# them; first-party scripts living in a top-level `bin/` stop being indexed
# — acceptable against the immortal-row class this kills).
#
# MUST MATCH templates/scripts/analyze_code_graph.py::_ALL_IGNORE_PARTS
# (value-compared by tests/test_codegraph_row_classify_parity.py) and stay a
# superset-consistent sibling of the Rust language-detection list
# `launcher/src-tauri/src/commands/codegraph.rs::ignored_dirs()` (the same
# parity test encodes the documented Rust delta).
# ---------------------------------------------------------------------------
CODEGRAPH_IGNORE_PARTS: frozenset = frozenset({
    # Version-control internals
    '.git', '.svn', '.hg',
    # Python virtualenv variants
    '.venv', 'venv', 'env', '.env',
    'virtualenv', '.tox', 'site-packages',
    # Python build / cache artefacts
    '__pycache__', '.pytest_cache',
    # Generic build outputs
    'build', 'dist', 'out',
    # JS/TS dependency cache
    'node_modules',
    # git-worktree containers
    'worktrees', '.wt',
    # JS/TS framework codegen + cache dirs
    '.svelte-kit', '.next', '.nuxt', '.cache', '.parcel-cache', '.turbo',
    '.angular',
    # Language-extras union (previously applied per-language only):
    'vendor',            # go / ruby / js / ts vendored deps
    'target',            # rust / maven build output
    '.gradle',           # gradle
    '.bundle',           # ruby bundler cache
    'obj', 'bin', '.vs', # .NET / VS workspace artefacts
    'coverage',          # js/ts test-coverage reports
})

#: Build-output / config / type-stub filename suffixes. MUST MATCH the union
#: of analyze_code_graph.py::_JS_SKIP_SUFFIXES + _TS_SKIP_SUFFIXES.
CODEGRAPH_SKIP_SUFFIXES: tuple = (
    '.min.js', '.bundle.js', '.chunk.js', '.config.js', '.config.mjs',
    '.d.ts', '.bundle.ts', '.chunk.ts', '.config.ts', '.config.mts',
)

#: Exact substring identifying orchestrator transient-scratch rows — MUST
#: match ``migrations/codegraph_collection/6_to_7.py::_TRANSIENT_MARKER``.
#: Paths are stored ``as_posix()`` on every OS, so forward slashes always
#: match. Purgeable regardless of revision or source (the marker itself is
#: the proof — orchestrator scratch is never legitimate code).
TRANSIENT_STATE_MARKER: str = ".claude/state/"


# BEGIN MUST-MATCH codegraph-row-classify (mirrored in
# templates/scripts/analyze_code_graph.py — the analyzer is a template that
# cannot import vco_lib at user sites. Edit BOTH copies;
# tests/test_codegraph_row_classify_parity.py byte-compares this region.)
def path_is_ignored(file_path: str, *, index_dot_claude: bool = True) -> bool:
    """True when a stored row path falls in the CURRENT ignore set.

    Path-PART match (not substring) for directories — ``my_vendor_tools/x.py``
    is NOT ignored; ``vendor/x.py`` is. Suffix match for build-output
    filenames. ``.claude`` joins the set only when ``index_dot_claude`` is
    False (user projects; the orchestrator root indexes ``.claude/`` as
    first-party source).
    """
    if not file_path:
        return False
    norm = str(file_path).replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    ignore = CODEGRAPH_IGNORE_PARTS
    if not index_dot_claude:
        ignore = ignore | frozenset({'.claude'})
    if any(p in ignore for p in parts[:-1]):
        return True
    name = parts[-1]
    if name.startswith('vite.config'):
        return True
    return any(name.endswith(s) for s in CODEGRAPH_SKIP_SUFFIXES)


def path_reachable_on_disk(rel_or_abs_path: str, repo_root: "Path") -> bool:
    """True when ``rel_or_abs_path`` resolves to a real on-disk file INSIDE
    ``repo_root`` (mirrors ``install.py::_path_resolves_on_disk`` safety).

    Fail-SAFE toward KEEPING data: an empty path, or ANY OS/value error while
    probing (NUL bytes, invalid chars, unreadable segment), returns ``True``
    ("treat as exists"). Only a determinate "resolved fine and is genuinely
    absent, or resolved fine and escapes the root" yields ``False`` —
    uncertainty must never authorise a delete, and on the probe side a wrong
    "reachable" only over-counts owed work (conservative, never a wrong
    "converged").
    """
    if not rel_or_abs_path:
        return True
    try:
        root_resolved = repo_root.resolve()
    except (OSError, ValueError):
        return True
    try:
        candidate = (root_resolved / rel_or_abs_path).resolve()
    except (OSError, ValueError):
        return True
    try:
        inside = candidate.is_relative_to(root_resolved)
    except (OSError, ValueError):
        return True
    if not inside:
        return False
    try:
        return candidate.exists()
    except (OSError, ValueError):
        return True


def classify_row(
    props: "Optional[Mapping[str, Any]]",
    repo_root: "Optional[Path]",
    *,
    path_prop: str = "file_path",
    current_revision: int = 1,
    index_dot_claude: bool = True,
    primary_sources: "Optional[set]" = None,
    reachable_fn=None,
) -> str:
    """Classify one code-graph row for convergence purposes.

    Returns exactly one of:

    * ``"owed"`` — a reachable, non-ignored stale row: a re-walk of its file
      re-stamps/re-embeds it. The owed-probe counts these; nothing deletes
      them. Includes ``embed_revision == 0`` rows (vectorless sentinel /
      module-row invalidation): the re-walk heals them — see the module
      docstring for the documented ruling.
    * ``"not_owed"`` — leave alone AND don't count: rows already at the
      current revision, and extra-path rows (``project_source`` set to a
      root outside ``primary_sources``) which converge on their OWN root's
      walk — never touch them from this walk (B1 tenant scoping).
    * ``"purgeable"`` — a stale row NO walk can ever converge; deleting it
      (via the tokenization-safe ``_delete_file_rows_exact`` primitive ONLY)
      is the only way the owed state can reach zero: transient
      ``.claude/state/`` scratch (purgeable regardless of revision — 6_to_7
      purge semantics), pathless rows in file-anchored collections, rows
      whose path falls in the ignore set (walk-excluded whether or not the
      file still exists), and deleted-file orphans (stored path gone from
      disk / escaping the root).

    ``reachable_fn`` optionally injects a memoized reachability predicate
    (one syscall per unique path across a whole probe run); when neither it
    nor ``repo_root`` is given the deleted-file rule is SKIPPED and such
    rows classify ``owed`` (fail-open: never authorise a purge without a
    positively-known root; over-counting owed is the conservative error).
    """
    p = props or {}
    raw_path = str(p.get(path_prop) or "")
    # 1. Transient scratch — the marker itself is the proof; regardless of
    #    revision or source (matches the 6_to_7 purge + F2/F4 semantics).
    if TRANSIENT_STATE_MARKER in raw_path:
        return "purgeable"
    # 2. Extra-path rows — a different source root owns them; this walk can
    #    neither re-stamp nor judge them (B1 scoping: never delete, never
    #    count — they converge on their own extra-path walk).
    if primary_sources is not None:
        src = str(p.get("project_source") or "").strip()
        if src and src not in primary_sources:
            return "not_owed"
    # 3. Converged rows — never touch, never count.
    rev = p.get("embed_revision")
    try:
        if rev is not None and int(rev) == int(current_revision):
            return "not_owed"
    except (TypeError, ValueError):
        pass  # unparseable revision → stale → fall through
    # 4. Pathless stale rows — no walk keys on an empty path: nothing can
    #    ever re-stamp them (immortal pre-v0.2.75), and no data purpose
    #    survives without the file anchor.
    if not raw_path:
        return "purgeable"
    # 5. Ignore-set rows — the walk never descends there (whether or not
    #    the file still exists), so a stale row under an ignored dir can
    #    never re-stamp. Derived, regenerable data: if a future ignore-set
    #    change re-includes the path, the next walk simply re-creates it.
    if path_is_ignored(raw_path, index_dot_claude=index_dot_claude):
        return "purgeable"
    # 6. Deleted-file orphans — nothing re-walks a file that no longer
    #    exists (D1 orphan-clear semantics, kept). Skipped entirely when no
    #    root/predicate is available (fail-open toward "owed").
    if reachable_fn is not None or repo_root is not None:
        if reachable_fn is not None:
            reachable = reachable_fn(raw_path)
        else:
            reachable = path_reachable_on_disk(raw_path, repo_root)
        if not reachable:
            return "purgeable"
    # 7. Reachable, non-ignored, stale → a re-walk converges it.
    return "owed"
# END MUST-MATCH codegraph-row-classify
