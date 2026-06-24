#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Code Graph Analyzer - Extract code entities and relationships into Weaviate

Creates and populates Weaviate collections for code analysis:
- CodeModule: Files/modules with imports and complexity metrics
- CodeClass: Classes with inheritance and methods
- CodeFunction: Functions with call graphs
- CodeAPI: API endpoints with handlers (for web frameworks)

Supports incremental analysis (only re-parse changed files).

Supported languages: Python (AST), Lua (regex), C++/C (regex), JavaScript/TypeScript/JSX (regex),
                    Go (regex), Rust (regex), Java (regex), Ruby (regex), Shell (regex).

Usage:
    python analyze_code_graph.py /path/to/repo
    python analyze_code_graph.py /path/to/repo --project "MyProject"
    python analyze_code_graph.py /path/to/repo --incremental
    python analyze_code_graph.py /path/to/repo --create-collections
    python analyze_code_graph.py /path/to/repo --language python
    python analyze_code_graph.py /path/to/repo --language lua
    python analyze_code_graph.py /path/to/repo --language cpp
    python analyze_code_graph.py /path/to/repo --language javascript
    python analyze_code_graph.py /path/to/repo --language typescript

v0.2.47 extras (knowledge/concepts/project-extra-codegraph-paths-2026-06-05.md):
    python analyze_code_graph.py /path/to/repo --project "MyProject" \
        --extra-path /path/to/sibling/clone --extra-path /path/to/other
    python analyze_code_graph.py /path/to/repo --project "MyProject" \
        --extra-path /path/to/sibling/clone --prune-stale  # union-of-roots
    python analyze_code_graph.py /path/to/repo --project "MyProject" \
        --incremental --since-commit abc1234   # diff vs SHA, not HEAD~1
"""

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple, Mapping
import subprocess


class _DedupInsertError(RuntimeError):
    """Exception raised by ``_dedup_insert`` to mark write failures.

    v0.2.16 (bug 0.2): the outer per-file try/except in
    ``analyze_repository`` distinguishes "this file failed to write
    to Weaviate" (counts as ``insert_errors``, triggers exit code 4)
    from "this file failed for some other reason — e.g. file read
    error, regex bug, ast parse error not caught downstream" (counts
    only as ``files_skipped``, doesn't change the exit code).
    """

    def __init__(self, original: BaseException, collection_name: str, uuid: str) -> None:
        super().__init__(
            f"_dedup_insert failed for {collection_name} (uuid={uuid}): {original}"
        )
        self.original = original
        self.collection_name = collection_name
        self.uuid = uuid


def _deterministic_uuid(project: str, file_path_rel: str = "", full_name: str = "",
                        project_source: str = "") -> str:
    """Generate a deterministic UUID from project + project_source + file_path_rel + full_name.

    Args:
        project: Project name (used as the outermost UUID namespace).
        file_path_rel: Repo-relative POSIX path of the file containing the
            entity. Default ``""`` is preserved for call sites where the
            file is not in scope (e.g. cross-reference creation). Callers
            with a Path object MUST pass ``path.as_posix()`` so Windows
            backslashes don't produce different UUIDs than the POSIX form
            on Linux/macOS (cross-machine consistency).
        full_name: Fully-qualified entity name (e.g. ``module.Class.method``).
            Also used as the only-non-empty arg when callers pass through
            the legacy two-arg form ``_deterministic_uuid(project, name)``;
            handled below.
        project_source: v0.2.52 (V52-O.3) — absolute POSIX path of the
            source root that contributed this row (primary repo OR a
            ``--extra-path`` value). Mixed into the seed so the SAME
            relative path under TWO different source roots produces
            TWO different UUIDs. Default ``""`` preserves byte-identical
            UUIDs for the v0.2.16-through-v0.2.51 single-root call shape
            (no on-disk migration needed for primary-repo-only installs).

    Why file_path_rel is part of the key (v0.2.16):
        Pre-v0.2.16 the key was just ``project::full_name``. Two files
        with the same module-stem + symbol name (e.g.
        ``server.handler`` defined in both
        ``docs/research/eval/probes/server.py`` and
        ``claude_mcp_servers/weaviate_mcp/server.py``) collided on the
        same UUID and the second one's insert was rejected. Including
        the file path eliminates this entire collision surface.

    Why project_source is part of the key (v0.2.52 / V52-O.3):
        ``--extra-path`` lets the analyzer walk a second source root and
        emit its rows into the primary project's collections (with
        ``project_source`` stamped). Pre-V52-O.3 the seed was
        ``project::file_path_rel::full_name`` — two roots sharing a
        relative path (e.g. ``src/index.ts`` exists in both) collided
        on the same UUID and the second walk's ``replace()`` overwrote
        the first. Mixing the absolute source-root path into the seed
        means each root gets its own UUID-space; the two rows coexist.
        Defaults to the empty string so single-root call sites
        (``analyze_repository`` without ``--extra-path``) keep producing
        the v0.2.16-era UUIDs and don't trigger a spurious re-write of
        the whole collection.

    Re-indexing the same entity (same project, same source root, same
    file, same symbol) still produces the same UUID, so re-runs
    continue to upsert cleanly.

    NOTE on back-compat: this helper is also called from contexts where
    only ``project`` + ``full_name`` are meaningful (cross-reference
    creation paths). When ``file_path_rel`` and ``project_source`` are
    the empty string the UUID degrades to the v0.2.15-and-earlier shape
    so those paths continue to resolve to the same UUIDs they did before.
    """
    key = f"{project}::{project_source}::{file_path_rel}::{full_name}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, key))


# v0.2.61 (Track E): per-object content-hash fields for the tombstone-skip.
# Maps the bare collection base-name to the ORDERED list of property keys
# whose values define an object's semantically-meaningful content. The hash
# is computed over ONLY these fields (in this fixed order) so that:
#   * the SAME content yields the SAME hash across runs (stable skip key), and
#   * volatile / run-derived fields (last_modified, project_source, language,
#     file_path) are EXCLUDED — backfilling those on an otherwise-unchanged
#     object must NOT change the hash, or we'd re-`replace()` (re-tombstone)
#     every row on the migration run that stamps them.
#
# Field choice rationale (only fields that drive the embedding vector or the
# searchable body, plus the identity key, so a genuine change is always
# reflected; references/UUIDs are derived from these same fields so they need
# not be hashed separately):
#   CodeModule      → path + module_summary + imports
#   CodeClass       → full_name + signature + class_body + methods + composes
#   CodeFunction    → full_name + signature + function_body + type_uses
#                     + cfg_summary + data_flow_vars
#   CodeAPI         → endpoint + method + api_description + parameters + returns
#   CodeInteraction → interaction_type + protocol + endpoint + raw_target
#                     + direction + description
# A collection whose name isn't recognised falls back to hashing ALL scalar/
# list properties (excluding the volatile set) — fail-safe toward "include
# more", which can only cause an extra (correct) write, never a wrong skip.
_CONTENT_HASH_FIELDS = {
    "CodeModule": ["path", "module_summary", "import_names"],
    "CodeClass": ["full_name", "signature", "class_body", "methods", "composes"],
    "CodeFunction": [
        "full_name", "signature", "function_body",
        "type_uses", "cfg_summary", "data_flow_vars",
    ],
    "CodeAPI": ["endpoint", "method", "api_description", "parameters", "returns"],
    "CodeInteraction": [
        "interaction_type", "protocol", "endpoint",
        "raw_target", "direction", "description",
    ],
}

# Fields that are deterministic-but-derived or volatile — NEVER part of the
# content hash even on the all-fields fallback path. `content_hash` itself is
# excluded so the hash is a fixed point (hashing-in the prior hash would make
# it unstable). `last_modified` is a filesystem mtime (changes on touch with
# no content change). `project_source` / `language` / `file_path` are stamped
# by `_dedup_insert` and are pure functions of (file, source-root) — including
# them would force a one-time re-write whenever a backfill migration first
# stamps them, defeating the skip.
_CONTENT_HASH_EXCLUDE = frozenset({
    "content_hash", "last_modified", "project_source", "language",
    "file_path", "start_line", "end_line",
})


def _stable_scalar(value: Any) -> str:
    """Render a property value into a stable, order-independent string.

    Lists are rendered element-wise (each element coerced to str) WITHOUT
    sorting — the analyzer emits these lists deterministically per parse, so
    preserving order keeps the hash byte-stable while a genuine reorder (which
    is a real content change in source) correctly changes the hash. None and
    missing values render as the empty string so an absent field and an
    explicitly-empty field hash identically (avoids spurious re-writes when a
    property is omitted vs. set to "").
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\x1e".join(_stable_scalar(v) for v in value)
    if isinstance(value, bool):
        # Render bools before the int branch (bool is a subclass of int) so
        # True/False hash distinctly from 1/0 textual collisions are avoided.
        return "true" if value else "false"
    return str(value)


def _content_hash_for_object(collection_name: str, properties: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 over an object's semantically-meaningful content.

    v0.2.61 (Track E) — mirrors the KG-sync `content_hash` discipline
    (templates/scripts/sync_knowledge_graph.py) for the code graph. Used by
    `_dedup_insert` to SKIP a `replace()` when the object is byte-identical to
    what's already indexed, eliminating needless HNSW vector tombstones.

    Args:
        collection_name: full per-project collection name (e.g.
            ``MyProject_CodeFunction``) OR a bare base name. We match on the
            base suffix so the per-project prefix is irrelevant.
        properties: the ``insert_params["properties"]`` dict for this object.

    Returns:
        Hex SHA-256 digest. Deterministic for identical content across runs,
        OSes, and machines (uses POSIX-normalized inputs the callers already
        produce). Never raises — a malformed value degrades into its ``str()``.

    Field selection: per `_CONTENT_HASH_FIELDS` for the recognised base names;
    otherwise every scalar/list property except `_CONTENT_HASH_EXCLUDE`. The
    fallback errs toward hashing MORE fields, which can only cause an extra
    (correct) write — never an incorrect skip.
    """
    base = ""
    for known in _CONTENT_HASH_FIELDS:
        if collection_name == known or collection_name.endswith(known):
            base = known
            break

    if base:
        fields = _CONTENT_HASH_FIELDS[base]
    else:
        # Unknown collection → hash all non-excluded keys in sorted order so
        # the digest is stable regardless of dict insertion order.
        fields = sorted(k for k in properties.keys() if k not in _CONTENT_HASH_EXCLUDE)

    parts = [base]
    for key in fields:
        parts.append(key)
        parts.append(_stable_scalar(properties.get(key)))
    blob = "\x1f".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Collection name helpers for per-project code graph collections
# ---------------------------------------------------------------------------

# Base collection names (used as suffixes with project prefix)
CODE_GRAPH_BASES = ["CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"]


# Import the canonical sanitizer (single source of truth shared with the
# launcher's Rust port at launcher/src-tauri/src/project_naming.rs).
# Falls back to a same-behaviour inline implementation when vco_lib is
# not importable — e.g. when the analyze script is shelled out from a
# venv that doesn't include the orchestrator repo on sys.path. The
# fallback path MUST stay byte-for-byte identical to the canonical
# implementation in vco_lib.project_naming; the parity test guards both.
#
# We attempt three sys.path candidates so the import works regardless
# of which venv the wrapper bash script activated:
#   1. Current sys.path (most setups will have vco_lib already
#      reachable because the orchestrator-clone parent dir is on PATH).
#   2. $VCT_INSTALL_ROOT (launcher always sets this to the orchestrator
#      install root — see launcher/src-tauri/src/commands/codegraph.rs
#      `looks_like_install_root`).
#   3. Walk up from this script: <root>/.claude/scripts/foo.py →
#      <root> contains vco_lib/.
def _ensure_vco_lib_on_path() -> bool:
    """Put the orchestrator root (the dir CONTAINING ``vco_lib/``) on
    ``sys.path`` so ``import vco_lib...`` works regardless of which venv
    the wrapper activated.

    v0.2.57 (codegraph bootstrap fix): this is the SINGLE bootstrap both
    ``vco_lib`` import sites in this module call. Previously there were
    TWO divergent copies — the first (for ``project_naming``) honored
    ``VCT_INSTALL_ROOT`` and validated the candidate actually contained
    ``vco_lib/``; the second (for ``embedding_service``) honored ONLY
    ``VCT_ORCHESTRATOR_ROOT`` with an UNVALIDATED ``parent.parent.parent``
    fallback. The launcher's codegraph spawn sets ``VCT_INSTALL_ROOT``
    but NOT ``VCT_ORCHESTRATOR_ROOT`` (see
    launcher/src-tauri/src/commands/codegraph.rs), so the second copy fell
    back to the user-project root — which has no ``vco_lib/`` — and the
    build died with ``ModuleNotFoundError: No module named 'vco_lib'`` (a
    user project hit this on 2026-06-14). One validated helper, both
    env-var names, no drift.

    Candidate order (first that actually contains ``vco_lib/`` wins):
      1. ``$VCT_INSTALL_ROOT`` — the launcher always sets this to the
         orchestrator install root (see ``looks_like_install_root``).
      2. ``$VCT_ORCHESTRATOR_ROOT`` — set by ``.claude/env`` (shell
         sourcing); present for CLI users, absent in launcher subprocs.
      3. ``<script_dir>/../..`` — when the script lives in the
         orchestrator clone's own ``.claude/scripts/``.

    Returns True if ``vco_lib`` is importable afterward (either already
    on path, or a candidate was inserted), False otherwise. Never raises.
    """
    # Already importable (e.g. an editable .pth in the active venv, or a
    # prior call) — nothing to do.
    if any((Path(p) / "vco_lib").is_dir() for p in sys.path if p):
        return True
    for _candidate in (
        os.environ.get("VCT_INSTALL_ROOT", "").strip(),
        os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip(),
        str(Path(__file__).resolve().parent.parent.parent),  # script_dir/../..
    ):
        if _candidate and (Path(_candidate) / "vco_lib").is_dir():
            if _candidate not in sys.path:
                sys.path.insert(0, _candidate)
            return True
    return False


_ensure_vco_lib_on_path()

try:
    from vco_lib.project_naming import canonical_class_prefix as _canonical_class_prefix
except ImportError:
    # Inline fallback — keep in sync with vco_lib/project_naming.py.
    # The launcher always passes --project, and VCT_INSTALL_ROOT/.venv
    # always has vco_lib importable, so this path is exercised only by
    # external direct invocations (e.g. user calling the script
    # standalone from a different venv).
    _NON_ALNUM_OR_UNDERSCORE_FALLBACK = re.compile(r"[^A-Za-z0-9_]")

    def _canonical_class_prefix(project_name: str) -> str:
        if not isinstance(project_name, str):
            raise ValueError(
                f"project_name must be str, got {type(project_name).__name__}"
            )
        stripped = project_name.strip()
        if not stripped:
            raise ValueError("project_name is empty (or whitespace-only)")
        parts = stripped.split()
        if not parts:
            raise ValueError(f"project_name {project_name!r} has no word parts")
        pascal_parts = [p[:1].upper() + p[1:] for p in parts]
        pascal = "".join(pascal_parts)
        cleaned = _NON_ALNUM_OR_UNDERSCORE_FALLBACK.sub("_", pascal)
        if not cleaned:
            raise ValueError(f"project_name {project_name!r} sanitizes to empty string")
        first = cleaned[0]
        if not first.isalpha():
            raise ValueError(
                f"project_name {project_name!r} sanitizes to {cleaned!r}, "
                "which starts with a non-letter character — Weaviate class "
                "names must begin with a letter [A-Z]"
            )
        return cleaned


def _sanitize_collection_prefix(name: str) -> str:
    """Sanitize project name for use as Weaviate collection prefix.

    Single source of truth: ``vco_lib.project_naming.canonical_class_prefix``.
    Kept as a thin wrapper so existing call sites in this module don't
    need to change. See vco_lib/project_naming.py for the rules-and-
    rationale write-up.

    Pre-v0.2.15 this used to be a local implementation that diverged
    from the launcher's Rust ``sanitize_kg_collection``:
        - this script replaced any non-``[A-Za-z0-9_]`` with ``_``;
        - the launcher PascalCased + stripped separators.
    For ``"VibeCoded Orchestrator"`` this produced
    ``"VibeCoded_Orchestrator"`` here but ``"VibeCodedOrchestrator"`` in
    the launcher wizard — and Weaviate's case-insensitive class-name
    collision then wedged the analyzer indefinitely (bug 0.7 / v0.2.15).
    """
    return _canonical_class_prefix(name)


def _collection_name(base: str, project_name: str) -> str:
    """Return per-project collection name, e.g. 'MyProject_CodeModule'.

    Raises SystemExit when project_name is empty to prevent silent writes to bare
    class names (e.g. 'CodeFunction') that cause multi-project data collision.
    """
    if not project_name:
        # NEW-10 (2026-05-28): refuse bare class name — multiple unrelated projects
        # piling into a single bare 'CodeFunction'/'CodeClass'/etc. collection is
        # a data-hygiene hazard that is hard to recover from (requires manual cleanup).
        raise SystemExit(
            f"analyze_code_graph: --project is required (or set CODE_GRAPH_PROJECT "
            f"in env). Refusing to write to bare class name '{base}' — would cause "
            f"multi-project data collision. See knowledge/concepts/parallel-pr-"
            f"coordination-gotchas-2026-05-10.md §1c."
        )
    prefix = _sanitize_collection_prefix(project_name)
    return f"{prefix}_{base}"


# ---------------------------------------------------------------------------
# File-walk ignore directories (v0.2.16 — addendum D)
# ---------------------------------------------------------------------------
# Centralised set of directory names that the file-walk should always
# skip. Previously each `_find_*_files` method had its own near-duplicate
# inline set; new entries (notably ``"worktrees"``) had to be added in
# 11 places. Factoring to a module-level frozenset means there's one
# place to add a new entry. Language-specific extras are still
# permitted via `_ignore_dirs_for(language)` below.
#
# The ``"worktrees"`` entry is the v0.2.16 reason for this refactor:
# `.claude/worktrees/agent-<hex>/` directories are git worktree clones
# of the main repo and are exact byte copies of main-repo source. The
# analyzer would walk them, attempt to re-analyze the same symbols,
# and trip the (now-fixed by replace()) 422 collision storm — even
# with replace(), walking worktrees is wasted work and produces
# duplicate-effort metrics. Skipping them upfront is the cleanest fix.
_COMMON_IGNORE_DIRS: frozenset = frozenset({
    # Version-control internals
    '.git', '.svn', '.hg',
    # Python virtualenv variants
    '.venv', 'venv', 'env', '.env',
    'virtualenv', '.tox', 'site-packages',
    # Python build / cache artefacts
    '__pycache__', '.pytest_cache',
    # Generic build outputs
    'build', 'dist',
    'out',                  # generic build output (alongside 'build', 'dist')
    # JS/TS dependency cache
    'node_modules',
    # v0.2.16 — git-worktree clones under .claude/worktrees/agent-*/
    'worktrees',
    # v0.2.52 (V52-O.1) — JS/TS framework codegen + cache dirs. SvelteKit
    # in particular produces a `.svelte-kit/output/` tree full of Rollup
    # chunks that look like JS source to the analyzer; in real-world
    # measurements 93% of CodeFunction rows on a SvelteKit project came
    # from chunk-*.js files. Adding the framework codegen + cache dirs
    # here prevents this entire pollution class.
    '.svelte-kit',          # SvelteKit codegen (output/, generated/)
    '.next',                # Next.js
    '.nuxt',                # Nuxt
    '.cache',               # generic framework cache
    '.parcel-cache',        # Parcel
    '.turbo',               # Turborepo
    '.angular',             # Angular cache
})


# Language-specific extensions of `_COMMON_IGNORE_DIRS`. Anything in the
# language extras is merged with the common set when `_find_*_files`
# calls `_ignore_dirs_for(language)`. Keep entries minimal and
# justify each one (most languages just inherit the common set).
_LANGUAGE_IGNORE_DIRS_EXTRAS: Dict[str, frozenset] = {
    # Go modules vendored into ./vendor/
    'go':     frozenset({'vendor'}),
    # Rust build output
    'rust':   frozenset({'target'}),
    # Gradle + Maven outputs
    'java':   frozenset({'.gradle', 'target'}),
    # Ruby vendored gems + bundler cache
    'ruby':   frozenset({'vendor', '.bundle'}),
    # .NET / VS workspace artefacts
    'csharp': frozenset({'obj', 'bin', '.vs'}),
    # JS / TS test-coverage reports
    'js':     frozenset({'coverage'}),
    'ts':     frozenset({'coverage'}),
    # Languages with no extras (placeholder — explicit is better than
    # implicit when the analyzer learns a new language family later)
    'shell':  frozenset(),
    'lua':    frozenset(),
    'cpp':    frozenset(),
    'python': frozenset(),
    'proto':  frozenset(),
}


def _ignore_dirs_for(language: str) -> frozenset:
    """Return the directory-skip set for a given language.

    The set merges :data:`_COMMON_IGNORE_DIRS` with any
    language-specific extras. Unknown languages get the common set
    only (no error — analyzer should never crash on a new language
    being added without first updating the table).
    """
    return _COMMON_IGNORE_DIRS | _LANGUAGE_IGNORE_DIRS_EXTRAS.get(language, frozenset())


# ---------------------------------------------------------------------------
# v0.2.18 (Plan C): canonical language identifier helpers.
#
# Pre-Plan-C the analyzer used display strings ("Python", "C++", "C#", ...) for
# the `language` property on CodeModule rows and lowercase canonical IDs
# ("python", "cpp", "csharp", ...) for the `--language` CLI flag + dispatcher.
# The two diverged, so a `--prune-stale --language=python` pass couldn't
# reliably filter rows: stored values were `"Python"`, the flag was `python`.
#
# Plan C unifies both onto the canonical lowercase ID (matches `--language`'s
# argparse `choices=[...]` set). All new inserts write the canonical form; the
# prune filter normalises the stored value on read so existing v0.2.17 rows
# (mixed-case display strings) also get matched correctly. The migration is
# additive — old data is not rewritten; the normaliser closes the gap on read.
#
# Single source of truth for both the display→canonical map and the canonical
# set is kept here so the analyzer's per-language paths and the hook's
# extension-to-language detection agree.
# ---------------------------------------------------------------------------
_LANGUAGE_DISPLAY_TO_CANONICAL: Dict[str, str] = {
    # Display label (as historically passed to _create_or_update_module /
    # _store_interactions / _extract_external_calls) → canonical ID.
    "python":     "python",
    "lua":        "lua",
    "javascript": "javascript",
    "js":         "javascript",
    "typescript": "typescript",
    "ts":         "typescript",
    "go":         "go",
    "rust":       "rust",
    "java":       "java",
    "ruby":       "ruby",
    "shell":      "shell",
    "csharp":     "csharp",
    "c#":         "csharp",
    "proto":      "proto",
    "cpp":        "cpp",
    "c++":        "cpp",
    "c":          "c",
    # V52-O.11.B / V52-O.11.N (v0.2.53 Track E):
    "svelte":     "svelte",
    "powershell": "powershell",
    "ps1":        "powershell",
    "ps":         "powershell",
}


def _canonical_lang_id(label: Optional[str]) -> str:
    """Map a language string to its canonical lowercase ID.

    Accepts display labels (`"Python"`, `"C#"`, `"C++"`, ...) and canonical
    IDs (`"python"`, `"csharp"`, `"cpp"`, ...) interchangeably. Unknown
    strings pass through lowercased + stripped — analyzer never crashes on
    a new language being added without first updating the table; the prune
    filter just won't recognise the row until the table is updated, which
    is the conservative behaviour.

    Returns an empty string when the input is falsy. Empty strings never
    match the `args.language` filter (argparse rejects empty), so unknown
    rows stay safe.
    """
    if not label:
        return ""
    key = str(label).strip().lower()
    if not key:
        return ""
    return _LANGUAGE_DISPLAY_TO_CANONICAL.get(key, key)


# v0.2.66 (Bug 3): file-extension → lang_dispatch name map for the
# single-file analyze path (`analyze_repository(only_file=...)`).
#
# The per-edit code-graph hook now hands the analyzer exactly ONE file
# (the file the user just edited) instead of the whole repo. To route
# that file to the right `_analyze_*_file` method WITHOUT re-walking the
# tree with rglob, we need an extension → dispatch-name lookup. The
# dispatch NAMES below match the first element of each `lang_dispatch`
# tuple in `analyze_repository` exactly — that loop turns the name back
# into the (find_fn, analyze_fn) pair. Keeping the extension lists here
# in lock-step with the per-language `_find_*_files` glob patterns is the
# single source of truth so the two cannot drift (see the parity note on
# `_dispatch_name_for_file`). A file whose extension is absent from this
# table is silently a no-op in single-file mode (matches the directory
# walk, which would never have discovered it either).
_EXT_TO_DISPATCH_NAME: Dict[str, str] = {
    ".py":     "python",
    ".lua":    "lua",
    ".cpp":    "cpp",
    ".cc":     "cpp",
    ".cxx":    "cpp",
    ".c":      "cpp",   # _find_cpp_files globs *.c into the cpp dispatch
    ".h":      "cpp",
    ".hpp":    "cpp",
    ".js":     "javascript",
    ".mjs":    "javascript",
    ".jsx":    "javascript",
    ".ts":     "typescript",
    ".tsx":    "typescript",
    ".go":     "go",
    ".rs":     "rust",
    ".java":   "java",
    ".rb":     "ruby",
    ".sh":     "shell",
    ".bash":   "shell",
    ".cs":     "csharp",
    ".proto":  "proto",
    ".svelte": "svelte",
    ".ps1":    "powershell",
    ".psm1":   "powershell",
}


def _dispatch_name_for_file(file_path: Path) -> str:
    """Return the `lang_dispatch` name for ``file_path``'s extension.

    Parity contract: the values returned here MUST equal the first
    element of a tuple in ``analyze_repository``'s ``lang_dispatch`` list,
    and the keys MUST stay aligned with the glob patterns in the matching
    ``_find_*_files`` method (e.g. ``_find_cpp_files`` globs ``*.c`` into
    the ``cpp`` dispatch, so ``.c`` maps to ``"cpp"`` here, not ``"c"``).
    Returns ``""`` for an unrecognised extension — the single-file caller
    treats that as a no-op (the directory walk would not have indexed it
    either).

    Special cases mirror the ``_find_*_files`` skip rules so single-file
    mode does not index a file the directory walk would have excluded:
      * ``*.d.ts`` declaration files (type stubs, not source).
      * ``*.min.js`` minified bundles.
    """
    name = file_path.name
    # Mirror `_find_ts_files` / `_find_js_files` name-based skips. These
    # files have a code extension but are deliberately excluded from the
    # directory walk; single-file mode must agree so an edit to one is a
    # no-op rather than an inconsistent index.
    if name.endswith(".d.ts"):
        return ""
    if name.endswith(".min.js"):
        return ""
    return _EXT_TO_DISPATCH_NAME.get(file_path.suffix.lower(), "")


# v0.2.52 (Known Issue 6, Sub-issue A): silence
# ``AuthlibDeprecationWarning: authlib.jose module is deprecated`` from
# ``weaviate-client``'s transitive ``authlib`` dep during module import.
# See ``claude_mcp_servers/weaviate_mcp/server.py`` for the matching
# filter at the MCP-server level.  MUST run BEFORE ``import weaviate``.
import warnings as _cg_warnings
try:
    from authlib.deprecate import AuthlibDeprecationWarning as _AuthlibDeprecationWarning  # type: ignore
    _cg_warnings.filterwarnings("ignore", category=_AuthlibDeprecationWarning)
except ImportError:
    _cg_warnings.filterwarnings(
        "ignore",
        message=r".*authlib.*deprecated.*",
        category=DeprecationWarning,
    )

try:
    import weaviate
    from weaviate.classes.config import Configure, Property, DataType, ReferenceProperty
    from weaviate.classes.query import Filter
except ImportError:
    print("Error: weaviate-client not installed. Install with: pip install weaviate-client", file=sys.stderr)
    sys.exit(1)

# Code embedding configuration — v0.2.18 centralised via EmbeddingService.
# Pre-v0.2.18, this script read CODE_EMBED_BACKEND / CODE_EMBED_SERVICE_URL /
# CODE_EMBED_MODEL directly and hardcoded the active slot to either
# `codesage_embed` (service) or `ollama_code_embed` (ollama). The hardcode
# silently broke OpenAI-as-code-embed installs and made the slot decision
# duplicate the same logic in 5+ places. v0.2.18: a single
# EmbeddingService instance (constructed in main()) owns the choice; this
# module's embed_* helpers route through it.
#
# Kept-but-unused env reads (CODE_EMBED_BACKEND/SERVICE_URL/MODEL) live on
# vco_lib/embedding_service.py:for_project() now. Searching this file for
# those names will turn up only the historical comment above.
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Note: We use manual vectorization (vectorizer=None) and generate embeddings via the configured backend.
# This avoids requiring Weaviate text2vec-ollama module configuration

# Add scripts directory to path for shared config
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# weaviate_mcp is pip-installed as an editable package by install.py
# (A1, v0.2.38) — no sys.path entry needed for weaviate_mcp.code_truncation.
# vco_lib (EmbeddingService) still needs its parent on sys.path because
# vco_lib is not yet a standalone package.
#
# v0.2.57: use the SINGLE validated bootstrap helper (defined near the top
# of this module). Previously this site read ONLY $VCT_ORCHESTRATOR_ROOT
# with an unvalidated parent.parent.parent fallback — which broke under
# the launcher (it sets $VCT_INSTALL_ROOT, not $VCT_ORCHESTRATOR_ROOT) and
# fell back to the user-project root, crashing the `from
# vco_lib.embedding_service import` below with ModuleNotFoundError. The
# helper honors both env-var names + validates the candidate contains
# vco_lib/, so the two sites can no longer drift.
# v0.2.61: assert the bootstrap succeeded BEFORE the bare `from
# vco_lib...` imports below (531 embedding_service, and later 6915
# project_config / 7204 deferral_report). Those are top-level imports
# with no try/except — if the helper couldn't find a dir containing
# `vco_lib/`, they crash with a bare `ModuleNotFoundError: No module
# named 'vco_lib'` deep in the file, which surfaces in the launcher as
# an opaque "Code graph: build failed". Failing here instead gives an
# actionable message naming the actual fix (the missing install root).
# The companion launcher fix (codegraph.rs resolving VCT_INSTALL_ROOT
# via resolve_orchestrator_root) makes this branch unreachable for a
# correctly-installed orchestrator; this is the defense-in-depth so a
# resolution miss never again hard-crashes mid-file.
if not _ensure_vco_lib_on_path():
    _tried = [
        ("VCT_INSTALL_ROOT", os.environ.get("VCT_INSTALL_ROOT", "").strip() or "(unset)"),
        ("VCT_ORCHESTRATOR_ROOT", os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip() or "(unset)"),
        ("<script_dir>/../..", str(Path(__file__).resolve().parent.parent.parent)),
    ]
    _detail = "; ".join(f"{name}={val}" for name, val in _tried)
    sys.stderr.write(
        "FATAL: could not locate the orchestrator root (a directory "
        "containing 'vco_lib/') — the code-graph analyzer cannot import "
        "its vco_lib dependencies.\n"
        f"  Candidates tried (none contained vco_lib/): {_detail}\n"
        "  Fix: ensure the launcher passes VCT_INSTALL_ROOT pointing at "
        "the orchestrator install root, OR run the analyzer from within "
        "the orchestrator clone, OR set VCT_ORCHESTRATOR_ROOT in the "
        "environment.\n"
    )
    sys.exit(1)
# VCO-REWIRE-END: orchestrator-root-resolution
try:
    from weaviate_mcp.code_truncation import (
        truncate_function_for_embedding,
        truncate_class_for_embedding,
        truncate_module_for_embedding,
    )
except ImportError:
    # Inline fallbacks — naive char-based truncation (no model-awareness)
    def truncate_function_for_embedding(signature, body, language="python", model=None):
        return f"{signature}\n{body[:600]}"

    def truncate_class_for_embedding(signature, class_body, methods=None, language="python", model=None):
        methods_str = ", ".join(methods[:10]) if methods else ""
        return f"{signature}\nMethods: {methods_str}\n{class_body[:500]}"

    def truncate_module_for_embedding(module_summary, model=None):
        return module_summary[:2000]


# v0.2.18: central embedding dispatcher. Replaces the inline
# CODE_EMBED_BACKEND / CODE_EMBED_SERVICE_URL HTTP calls that previously
# duplicated the same logic across `generate_embedding`,
# `embed_function`, `embed_class`, `embed_module` plus 5+ inline insert
# sites. EmbeddingService.for_project() picks the right backend
# (CodeEmbed service / Ollama / OpenAI) AND the right named-vector slot
# (codesage_embed / jina_embed / openai_code_embed / qwen3_embed
# fallback) from env, so this module no longer hardcodes any of it.
from vco_lib.embedding_service import (
    EmbeddingService,
    NoEmbeddingBackendError,
)

# Module-global EmbeddingService — initialised by main() before any
# code-graph work happens, and closed in main()'s finally block. The
# embed_* helpers below resolve it lazily on first use so unit tests that
# import the module without calling main() don't pay the cost of probing
# backends at import-time.
_embedding_service: Optional["EmbeddingService"] = None


def _set_embedding_service(svc: "EmbeddingService") -> None:
    """Inject the active EmbeddingService for this run.

    Called by main() before any embed call. Tests can also call this to
    inject a mocked service.
    """
    global _embedding_service
    _embedding_service = svc


def _get_embedding_service() -> Optional["EmbeddingService"]:
    """Return the active EmbeddingService, or None if not initialised.

    None means the embed call is happening outside the normal entrypoint
    (e.g. a unit test imported the module without setting up the service).
    In that case the embed helpers below return None — same behaviour as
    pre-v0.2.18's `generate_embedding` did on backend failure.
    """
    return _embedding_service


def _active_code_vector_slot() -> str:
    """Return the active named-vector slot for code writes.

    Resolved at-call-time so re-init doesn't strand callers on an old
    slot. Falls back to the pre-v0.2.18 default (`codesage_embed`) when
    the service isn't initialised — preserves existing behaviour for
    tests that exercise the schema-creation path without first booting
    the embedding service.
    """
    svc = _get_embedding_service()
    if svc is not None:
        return svc.code_vector_slot
    return "codesage_embed"


# Resolve which code embedding model is active (for token budget in truncation)
def _resolve_code_model_id() -> str:
    """Return the active code-embedding model id, e.g. 'codesage-large-v2'.

    Read via the EmbeddingService when available, with a CodeSage default
    so the smart-truncation token budget still applies in test contexts
    where the service isn't booted.
    """
    svc = _get_embedding_service()
    if svc is not None:
        return svc.code_model_id
    return "codesage/codesage-large-v2"


def generate_embedding(text: str) -> Optional[Any]:
    """Embed *text* via the EmbeddingService.

    Return shape:
      * ``DUAL_EMBEDDING_ENABLED=true`` (default) → ``dict[str, list[float]]``
        with one slot per reachable code backend (multi-slot enrichment).
      * ``DUAL_EMBEDDING_ENABLED=false`` (legacy) → ``list[float]``,
        single flat vector from the active backend.
      * No backend produced anything → ``None``.

    The ``if embedding:`` guard at every inline insert site works for
    every shape (truthy non-empty dict OR non-empty list OR None).

    Pre-v0.2.18 this read ``CODE_EMBED_BACKEND`` env directly and
    returned only a ``list[float]``. v0.2.18 centralises both decisions
    on EmbeddingService and adds multi-slot fan-out — see
    ``vco_lib.embedding_service.EmbeddingService.embed_code_all_configured``.
    """
    svc = _get_embedding_service()
    if svc is None:
        print("⚠️  Embedding requested but EmbeddingService not initialised", file=sys.stderr)
        return None
    try:
        if DUAL_EMBEDDING_ENABLED:
            slots = svc.embed_code_all_configured(text)
            return slots if slots else None
        return svc.embed_code(text)
    except Exception as e:
        print(f"⚠️  Embedding generation error: {e}", file=sys.stderr)
        return None


def _extract_balanced_block(
    source_lines: List[str],
    start_line: int,
    *,
    opener: str = "{",
    closer: str = "}",
    max_lookahead: int = 400,
) -> int:
    """V52-O.11.E (v0.2.52, 2026-06-09): find the real end-line of a
    code block by counting balanced ``opener``/``closer`` pairs.

    Replaces the broken ``end_line = min(start_line + N, len(source_lines))``
    heuristic used at 17 sites in this file pre-V52-O.11.E. Audit a79152
    confirmed the heuristic systematically over-clusters sequential
    functions by writing each function's ``function_body`` extending up
    to N lines past its real close brace (e.g. ``is_blocklisted_agent_file``
    in project_state_populate.rs: real end line 281, stored end line 315,
    body contains 34 lines of the NEXT function).

    Algorithm:
      1. Scan ``source_lines[start_line-1:]`` looking for the first
         ``opener``. Once found, increment a brace-counter.
      2. Continue scanning; for every additional ``opener`` increment,
         for every ``closer`` decrement. When counter reaches 0, the
         current line is the close-brace line — return its 1-indexed
         line number.
      3. Skip openers/closers inside:
         - String literals (``"..."`` and ``'...'``) — single-line only;
           multi-line raw/template strings are out of scope (caller
           accepts mild bleed when the function contains a multi-line
           string with unbalanced braces — that's a corner case).
         - Line comments (``//`` and ``#``).
         - Block comments (``/* ... */``) — single-line variant only.
      4. If no balanced close is found within ``max_lookahead`` lines,
         return ``min(start_line + max_lookahead, len(source_lines))``
         (graceful degradation — gives the caller the existing-pattern
         behavior for runaway functions).

    Returns the **1-indexed line number of the closing brace**. Callers
    consume it via the existing pattern:

        end_line = _extract_balanced_block(source_lines, start_line)
        body = '\\n'.join(source_lines[max(0, start_line - 1):end_line])

    The 1-indexed return matches the existing ``end_line`` convention
    at every caller site — drop-in replacement, no off-by-one.

    Language coverage: works for any brace-balanced language (C, C++,
    Java, JavaScript, TypeScript, Go, Rust, C#, Lua-with-end-keyword
    is handled by ``_extract_balanced_block_keyword`` instead). Doesn't
    work for indent-significant languages (Python uses AST so it
    bypasses this helper entirely; Ruby uses ``end`` keywords —
    callers there should still use this helper since Ruby's bodies are
    short enough that brace-balance over a 400-line window won't
    over-extend, but it's a less precise fit).

    Performance: ~O(end_line - start_line) lines scanned per call. With
    ``max_lookahead=400`` and typical function bodies of 10-50 lines,
    this adds ~1ms per function vs the old fixed-window approach. The
    correctness gain (no body-bleed contamination in embeddings) is
    worth the cost.
    """
    if start_line < 1 or start_line > len(source_lines):
        return min(start_line + 40, len(source_lines))  # legacy fallback

    counter = 0
    found_opener = False
    end_index = start_line - 1  # 0-indexed start
    lookahead_end = min(start_line - 1 + max_lookahead, len(source_lines))

    for line_idx in range(start_line - 1, lookahead_end):
        line = source_lines[line_idx]
        # Strip line comment + single-line block comment + string literals.
        # This is a best-effort scrub — multi-line strings + multi-line
        # block comments are out of scope (callers degrade gracefully).
        scrubbed = _scrub_for_brace_balance(line)
        for ch in scrubbed:
            if ch == opener:
                counter += 1
                found_opener = True
            elif ch == closer:
                counter -= 1
                if found_opener and counter == 0:
                    # +1 because line_idx is 0-indexed; end_line is 1-indexed
                    return line_idx + 1

    # No balanced close within lookahead — fall back to the legacy
    # behavior so callers don't crash. This is the runaway-function
    # branch; in practice almost never hit.
    return min(start_line + 40, len(source_lines))


def _scrub_for_brace_balance(line: str) -> str:
    """Best-effort: remove comments + single-line string literals from
    ``line`` so the brace-counter in ``_extract_balanced_block`` doesn't
    mis-count braces inside strings/comments.

    Order matters: comments first (so a quote inside a comment doesn't
    open a string), then strings. Multi-line constructs (raw strings,
    block comments spanning lines, template literals) are intentionally
    not handled — they're rare enough that the caller's graceful
    degradation suffices.
    """
    # Strip line comments. Handle Python ``#``, shell ``#``, C++ ``//``,
    # Lua ``--``. We strip whichever appears first.
    earliest = len(line)
    for marker in ("#", "//", "--"):
        idx = line.find(marker)
        if idx >= 0 and idx < earliest:
            earliest = idx
    line = line[:earliest]

    # Strip single-line block comments: /* ... */ on one line.
    line = re.sub(r"/\*.*?\*/", "", line)

    # Strip string literals. Single-line only — multi-line out of scope.
    line = re.sub(r"\"(?:\\.|[^\"\\])*\"", '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    # Template literals (backticks). Single-line only.
    line = re.sub(r"`(?:\\.|[^`\\])*`", "``", line)

    return line


def _go_methods_for_struct(
    content_clean: str,
    struct_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-GO (v0.2.52, 2026-06-09): extract methods declared on
    ``struct_name`` via Go's receiver syntax.

    Replaces the pre-V52-O.11.F.2-GO line that ran
    ``func_pattern.finditer(content_clean)`` unconditionally — the same
    bug as Rust's V52-O.11.F, applied to the Go parser. Audit a79152
    flagged the four parallel sites (Go, JS/TS, Java, C#); this fix
    closes the Go one. A 50-fn Go file with 3 structs produced 150
    incorrect method attributions, drowning real signal in the
    ``query_code_structure(methods, StructName)`` MCP path.

    Go's method-scoping model (CRITICAL difference from Rust):

      Go does NOT use ``impl <Type> { ... }``. Methods are functions
      with a **receiver** declared BEFORE the function name:

          func (recv *Foo) MethodName(args...) ReturnType { ... }
          func (recv  Foo) MethodName(args...) ReturnType { ... }

      The token between the receiver-paren and the close-paren is the
      type. A leading ``*`` denotes a pointer receiver; both pointer
      and value receivers are methods on the same type.

    Algorithm:

      1. Match every ``func (<recv_var> [*&]?<struct_name>) <Name>(``
         pattern in ``content_clean``. The receiver variable name is
         arbitrary (often a single letter), so we accept ``\\w+`` for it.
         Whitespace around the type is lenient because Go style varies.
      2. Collect ``<Name>`` values. Deduplicate, preserving first-seen
         order — a struct can have a method declared on both the
         pointer and value receiver in pathological code, but only one
         name lands in the methods list.
      3. We DO NOT pick up receiver-less ``func Name(...)`` declarations
         — those are package-level free functions, not methods. They
         get processed by the separate function-entry loop downstream.

    Returns empty list if no receiver-bound functions match — common
    for plain data structs (`type Config struct { ... }`) that only
    carry fields, and for interfaces whose method set is defined via
    the interface body itself (the interface body's method declarations
    don't use `func` keyword, so the regex correctly ignores them).

    Limitations:
      - Generic Go methods (Go 1.18+, ``func (s *Foo[T]) Method()...``)
        are matched: the ``[*&]?<struct_name>`` segment captures the
        bare name and the optional ``[T]`` generic parameter follows
        before the close-paren, which we tolerate via a permissive
        post-name pattern (``[^)]*``).
      - Methods declared in another file of the same package on the
        same type are correctly NOT picked up here — they only land
        in the methods list when the OTHER file is being analyzed
        (each file is parsed independently). This is consistent with
        the Rust path's per-file scoping and with how Go's tooling
        itself reports methods.
      - Doesn't follow embedded-struct method promotion (interfaces
        that embed another interface's method set, struct types that
        embed another struct). Those are intentionally out of scope
        for this regex-based parser; tree-sitter is the right tool
        for that level of fidelity (queued as V52-O.11.G in backlog).
    """
    escaped = re.escape(struct_name)
    # Receiver shape:
    #   func ( <recv_var> [*&]? <struct_name> [generics?] ) <Name> (
    # Notes on each piece:
    #   - `func\s*\(` — opening receiver paren, possibly with
    #     whitespace between `func` and `(` (rare but legal style).
    #   - `\s*\w+\s+` — the receiver variable name (e.g. `f`, `recv`,
    #     `self`). Mandatory in Go syntax: anonymous receivers don't
    #     exist (you can use `_` but it's still a word char).
    #   - `[*&]?` — optional pointer marker. `&` isn't legal Go syntax
    #     for receivers, but we include it for robustness against
    #     hand-written test fixtures and the cost of one extra char in
    #     the regex is negligible. Real Go code only uses `*`.
    #   - `{escaped}` — the literal struct name, regex-escaped.
    #   - `[^)]*` — anything else up to the closing receiver paren
    #     (covers generic parameters like `[T]`, type assertions, and
    #     trailing whitespace).
    #   - `\)` — closing receiver paren.
    #   - `\s+(\w+)\s*\(` — the method name (captured) followed by its
    #     own argument paren. The trailing `\(` anchors us to a real
    #     function declaration vs. a stray `func (...)` cast expression.
    method_pattern = re.compile(
        rf"func\s*\(\s*\w+\s+[*&]?{escaped}[^)]*\)\s+(\w+)\s*\(",
        re.MULTILINE,
    )

    methods: List[str] = []
    seen: set = set()
    for m in method_pattern.finditer(content_clean):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        methods.append(name)
    return methods


def _rust_methods_for_struct(
    content_clean: str,
    struct_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F (v0.2.52, 2026-06-09): extract methods declared inside
    every ``impl <struct_name>`` (or ``impl <Trait> for <struct_name>``)
    block in the source.

    Replaces the pre-V52-O.11.F line that ran
    ``func_pattern.finditer(content_clean)`` unconditionally — that
    attributed EVERY function in the file to EVERY struct. Audit a79152
    confirmed: a 50-fn file with 3 structs produced 150 incorrect
    method attributions, drowning the real ``methods`` signal in noise.

    Algorithm:

      1. Find every ``impl`` block whose target type matches ``struct_name``.
         Two shapes accepted:
           - ``impl <generics?> <struct_name> <generics?> {``      (inherent)
           - ``impl <generics?> <Trait> for <struct_name> <generics?> {`` (trait)
      2. For each matched impl block, find its closing brace via the
         existing ``_extract_balanced_block`` helper (already brace-balanced
         per V52-O.11.E).
      3. Scan the block body for ``fn name(...)`` patterns; collect names.
      4. Return deduplicated list, preserving first-seen order.

    Returns empty list if no impl blocks match — common for plain data
    structs that only carry fields. The caller renders ``methods: []`` in
    the CodeClass row, which is correct (no methods exist).

    Limitations:
      - Doesn't handle ``impl<T: Trait> ...`` where the type parameter
        appears literally in the type position (rare in practice).
      - Doesn't extract methods declared via macros (``impl_trait!``).
      - Doesn't follow ``impl`` in nested ``mod`` blocks transparently —
        the brace-balance helper correctly skips over them, so methods
        in nested mods that DO impl on ``struct_name`` from the outer
        scope get picked up. Methods inside nested impl blocks on a
        DIFFERENT struct are correctly excluded.
    """
    # Find every impl header line whose target type matches struct_name.
    # The regex covers both shapes:
    #   - impl <generics>? <Name> <generics>? {
    #   - impl <generics>? <Trait> for <Name> <generics>? {
    # Generics + trailing-where are non-capturing; we only need the impl
    # start position. Generics are matched with a simple ``<[^>]*>`` since
    # Rust generics rarely nest in impl headers (where-clauses move
    # complex bounds to after the type).
    escaped = re.escape(struct_name)
    # Shape A: `impl Foo` / `impl<T> Foo<T>` / `impl<T> Foo`
    # Shape B: `impl Trait for Foo` / `impl<T> Trait<T> for Foo<T>`
    impl_pattern = re.compile(
        # `impl` keyword
        r"impl"
        # Optional generic parameters on impl
        r"(?:\s*<[^>]*>)?"
        # The target type. Either Shape A (just struct_name) or
        # Shape B (Trait + `for` + struct_name).
        r"\s+(?:"
        # Shape B alternative: any token sequence + `for` + struct_name
        rf"(?:[\w:]+(?:\s*<[^>]*>)?\s+for\s+){escaped}"
        rf"|{escaped}"
        # Shape A — struct_name alone
        r")"
        # Optional generics on the target (rare but valid)
        r"(?:\s*<[^>]*>)?"
        # Optional where clause + opening brace
        r"\s*(?:where\s+[^{]*)?\{",
        re.MULTILINE,
    )

    method_pattern_inner = re.compile(
        r"(?:pub\s+(?:\([^)]*\)\s+)?)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?"
        r"fn\s+([\w]+)\s*",
    )

    methods: List[str] = []
    seen: set = set()
    for m in impl_pattern.finditer(content_clean):
        # Locate the opening brace position (the regex anchors on it).
        # m.end() is just past the `{`, so the impl body starts there.
        impl_open_pos = m.end() - 1  # position of `{`
        # Find the matching close via brace-balance on source_lines.
        # We need a source-line index: convert char-offset to line.
        impl_open_line = content_clean[:impl_open_pos].count("\n") + 1
        impl_close_line = _extract_balanced_block(
            source_lines, impl_open_line, max_lookahead=800
        )
        # Slice content_clean (NOT source_lines — content_clean has
        # comments stripped, mirroring how the original method extraction
        # worked).
        block_start_pos = impl_open_pos + 1  # skip `{`
        # Find char-offset for impl_close_line. Sum line lengths +1 for \n.
        # Cheaper: count lines from block_start_pos and stop when we've
        # passed (impl_close_line - impl_open_line) newlines.
        target_newlines = impl_close_line - impl_open_line
        if target_newlines <= 0:
            continue
        # Find the position by counting newlines from block_start_pos.
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        impl_body = content_clean[block_start_pos:block_end_pos]
        for fm in method_pattern_inner.finditer(impl_body):
            name = fm.group(1)
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)
    return methods


def _js_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-JS (v0.2.52, 2026-06-09): extract methods declared
    inside ``class <class_name> { ... }`` (with optional ``extends``) in
    JS/TS source.

    Replaces the pre-V52-O.11.F.2-JS regex (``method_inside`` over
    ``class_body``) which had three correctness defects:

      1. Missed method shapes: ``static name()``, ``get name()``,
         ``set name()``, ``*name()`` (generator), ``#name()`` (private),
         ``static async name()``, ``static *name()``.
      2. Matched ANY ``name(args) {`` pattern, including nested function
         calls inside method bodies (``otherFn(arg) { ... }`` invoked
         inside a method body would be miscounted as a method of the
         outer class).
      3. Incorrectly skipped ``constructor`` (constructor IS a method
         that should be tracked, not filtered).

    Algorithm:

      1. Find every ``class <class_name>`` header (with optional
         ``extends <Base>``). The class name must match EXACTLY — a
         class named ``Foo`` does not match ``FooBar``. Optional
         ``export`` / ``export default`` prefix supported.
      2. For each matched class block, find its closing brace via the
         existing ``_extract_balanced_block`` helper (V52-O.11.E).
      3. Walk the class body brace-by-brace tracking depth. At depth 1
         (immediate body, NOT inside any nested block), scan for
         method declarations matching the JS method-shorthand patterns:
           - ``name(args)``
           - ``async name(args)``
           - ``static name(args)``
           - ``static async name(args)``
           - ``get name()`` / ``set name(v)``
           - ``*name(args)`` (generator)
           - ``async *name(args)`` (async generator)
           - ``#name(args)`` (private field)
           - ``static #name(args)`` / ``#async name`` etc. combinations
      4. Return deduplicated list, preserving first-seen order.

    Returns empty list if no class blocks match — common for classes
    whose body is purely fields/properties without method shorthand.

    Handles ``extends``: ``class Foo extends Bar { ... }`` — the class
    block IS Foo's body, methods of Bar are NOT picked up (Bar is in a
    DIFFERENT class declaration entirely, scoped separately).

    Limitations:
      - Object-literal method shorthand inside method bodies isn't
        confused with class-level methods because of the depth-tracking
        (object literals are at depth>=2 inside any method).
      - Multi-line strings / template literals with unbalanced braces
        may confuse depth tracking — inherits the same constraint as
        ``_extract_balanced_block``. Rare in practice; callers degrade
        gracefully (under-count by 1-2 methods at worst).
      - Decorators (``@decorator``) on a method line are skipped (the
        pattern starts at the method name itself).

    Language coverage: works for both JavaScript (``.js``, ``.mjs``,
    ``.jsx``) and TypeScript (``.ts``, ``.tsx``) — TS class syntax is
    a strict superset (adds type annotations on params + return types)
    and our pattern ignores those by matching only up to the opening
    paren.
    """
    # Find every class header whose name matches class_name exactly.
    # Optional `export` / `export default` prefix; optional `extends Base`.
    # The class name capture group is constrained to NOT be followed by
    # any identifier character so e.g. searching for "Foo" doesn't match
    # `class FooBar`.
    escaped = re.escape(class_name)
    class_pattern = re.compile(
        r"(?:export\s+(?:default\s+)?)?"
        rf"class\s+{escaped}\b"
        r"(?:\s+extends\s+[\w.]+)?"
        r"\s*\{",
        re.MULTILINE,
    )

    # Inner method-shorthand patterns. Matched anchored at the start of
    # the candidate position; collectively cover the JS class method
    # shapes documented above. Each variant captures the method name in
    # group 1.
    #
    # Order matters for the alternation: we test specific (longer)
    # prefixes first so "static async foo" matches as static-async, not
    # static + "async" (would attribute the wrong name).
    method_patterns = [
        # static async generator: `static async *name(...)`
        re.compile(r"static\s+async\s+\*\s*([#\w]+)\s*\("),
        # static generator: `static *name(...)`
        re.compile(r"static\s+\*\s*([#\w]+)\s*\("),
        # static async: `static async name(...)`
        re.compile(r"static\s+async\s+([#\w]+)\s*\("),
        # static get/set: `static get name()` / `static set name(v)`
        re.compile(r"static\s+(?:get|set)\s+([#\w]+)\s*\("),
        # plain static: `static name(...)`
        re.compile(r"static\s+([#\w]+)\s*\("),
        # async generator: `async *name(...)`
        re.compile(r"async\s+\*\s*([#\w]+)\s*\("),
        # async: `async name(...)`
        re.compile(r"async\s+([#\w]+)\s*\("),
        # generator: `*name(...)`
        re.compile(r"\*\s*([#\w]+)\s*\("),
        # get/set: `get name()` / `set name(v)`
        re.compile(r"(?:get|set)\s+([#\w]+)\s*\("),
        # plain shorthand: `name(...)` — must be tested LAST since it
        # would otherwise eat any of the prefixed forms.
        re.compile(r"([#\w]+)\s*\("),
    ]

    # Keywords that match the plain shorthand pattern but are NOT methods.
    # `constructor` is INTENTIONALLY NOT in this list — it IS a method.
    keyword_skip = {
        "if", "else", "while", "for", "switch", "return",
        "class", "new", "catch", "throw", "do", "try",
        "function", "yield", "await", "void", "typeof",
        "instanceof", "in", "of", "case", "break",
        "continue", "delete", "var", "let", "const",
    }

    methods: List[str] = []
    seen: set = set()

    for m in class_pattern.finditer(content_clean):
        # Locate the opening brace position (the regex anchors on it).
        class_open_pos = m.end() - 1  # position of `{`
        class_open_line = content_clean[:class_open_pos].count("\n") + 1
        class_close_line = _extract_balanced_block(
            source_lines, class_open_line, max_lookahead=800
        )
        block_start_pos = class_open_pos + 1  # skip `{`

        # Find char-offset for class_close_line in content_clean. Same
        # newline-counting walker as _rust_methods_for_struct uses.
        target_newlines = class_close_line - class_open_line
        if target_newlines <= 0:
            continue
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        class_body_text = content_clean[block_start_pos:block_end_pos]

        # Walk the class body char-by-char tracking brace depth so we
        # only look for method declarations at depth 0 of the class
        # body (which is depth 1 of the file's brace nesting).
        # Method bodies/blocks at depth >=1 are skipped — this prevents
        # nested function-call statements like `cb(arg) { ... }`
        # inside a method body from being miscounted as class methods.
        depth = 0
        i = 0
        n = len(class_body_text)
        while i < n:
            ch = class_body_text[i]
            if ch == "{":
                depth += 1
                i += 1
                continue
            if ch == "}":
                depth -= 1
                i += 1
                continue
            # Skip strings (single-line only, mirroring the existing
            # content_clean comment-strip pre-pass which removed line +
            # block comments — backtick-delimited template literals
            # remain in content_clean and can throw off depth tracking
            # if they contain unescaped braces, but that's an inherited
            # constraint from V52-O.11.E).
            if ch in ('"', "'"):
                quote = ch
                i += 1
                while i < n and class_body_text[i] != quote:
                    if class_body_text[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    i += 1
                i += 1  # skip the closing quote
                continue
            if ch == "`":
                # Template literal — skip naively to matching backtick.
                i += 1
                while i < n and class_body_text[i] != "`":
                    if class_body_text[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    i += 1
                i += 1
                continue
            if depth != 0:
                i += 1
                continue
            # At depth 0 of class body — try every method pattern at
            # this position. Use match() (anchored) on the substring
            # from i so we don't have to write start anchors in each
            # regex.
            matched = False
            substr = class_body_text[i:]
            for mp in method_patterns:
                pm = mp.match(substr)
                if not pm:
                    continue
                name = pm.group(1)
                # Filter out reserved keywords that look like methods.
                # (`constructor` deliberately preserved.)
                if name in keyword_skip:
                    break  # don't try lower-priority patterns; this is a keyword
                if name in seen:
                    matched = True
                    i += pm.end()
                    break
                seen.add(name)
                methods.append(name)
                matched = True
                # Advance past the matched prefix so we don't re-match
                # the same method header from a substring position.
                i += pm.end()
                break
            if matched:
                continue
            i += 1

    return methods


def _java_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-JAVA (v0.2.52, 2026-06-09): extract methods declared
    inside the body of ``class <class_name> { ... }`` (or interface/enum
    with the same name).

    Replaces the pre-V52-O.11.F.2 line that ran
    ``method_pattern.finditer(content_clean)`` unconditionally inside the
    per-class loop — that attributed EVERY method in the file to EVERY
    class. Same antipattern as the Rust bug fixed in V52-O.11.F; audit
    a79152 flagged it across Rust, Go, JS/TS, Java, and C# parsers.

    Java's method-scoping model is lexical (like JS): methods are declared
    inside the class braces themselves. So we:

      1. Find the ``class <class_name>`` declaration line (handling
         optional generics on the class, ``extends Parent``, ``implements
         Iface1, Iface2`` clauses).
      2. Use the existing ``_extract_balanced_block`` helper to find the
         matching close brace (V52-O.11.E, brace-balanced).
      3. Slice the class body. Strip nested class/interface/enum blocks
         from the body so methods declared in inner classes don't leak
         into the outer class's method list.
      4. Scan the stripped body for method declarations and collect names.

    Bug #2 — package-private capture (audit a79152):
      The pre-V52-O.11.F.2 method regex used ``(?:modifiers|\\s)+`` with
      a ``+`` quantifier, requiring AT LEAST ONE modifier keyword (or
      whitespace). Package-private methods (no visibility modifier,
      common in Java for package-scoped helpers) were silently missed.
      This helper's inner method pattern uses ``*`` instead, making the
      modifier section optional.

    Java method signature shape::

        [modifiers] [<generics>] return_type method_name(args) [throws ...] {

    Modifiers handled: public, private, protected, static, final,
    abstract, synchronized, native, default, strictfp. ``@Annotation``
    lines preceding a method are stripped from the per-line scrub by
    ``_scrub_for_brace_balance`` indirectly (the regex is anchored on the
    method-shape tail, so annotation lines simply don't match).

    Returns deduplicated list of method names, preserving first-seen
    order. Returns empty list if no class with that name is found, or if
    the class has no methods.

    Excludes:
      - Control-flow keywords that look like method calls (``if``,
        ``while``, ``for``, ``switch``, ``catch``, ``try``, ``else``,
        ``return``, ``synchronized`` as a statement).
      - Constructors (no return type — the regex requires at least one
        return-type token before the method name, matching the pre-fix
        behavior at line 3675).
      - Methods declared in nested classes (stripped before scanning).

    Limitations:
      - Doesn't handle multiple classes with the same name in different
        scopes (extremely rare in Java — only legal in nested-class
        scope, and the nested-class stripping handles the common case).
      - Multi-line generic type parameters (``Map<String,\\nList<...>>``
        on a method return type) may confuse the line-anchored regex;
        these are uncommon.
    """
    escaped = re.escape(class_name)
    # `class|interface|enum` declaration with optional generics on the
    # class, optional `extends`, optional `implements`. Anchor on `{` so
    # m.end() points to the position of the opening brace + 1.
    #
    # Generics handling: Java supports nested generics on the class
    # decl (`class Service<T extends Comparable<T>>`). A simple
    # `<[^>]*>` would fail because it stops at the first `>`. We accept
    # one level of nesting via `<[^<>]*(?:<[^<>]*>[^<>]*)*>` which
    # matches outermost-balanced generic blocks. Two+ levels of nesting
    # (e.g. `Map<String, List<Map<K,V>>>` as a TYPE PARAMETER bound) is
    # rare in class-decl position and falls back to the legacy
    # behavior — methods would simply be missed for those exotic
    # declarations.
    _GENERIC_BLOCK = r"<[^<>]*(?:<[^<>]*>[^<>]*)*>"
    class_decl_pattern = re.compile(
        # `class` | `interface` | `enum` keyword
        r"\b(?:class|interface|enum)"
        # Whitespace + the target class name (escaped)
        rf"\s+{escaped}"
        # Optional generic parameters on the class declaration (allows
        # one level of nested generics).
        rf"(?:\s*{_GENERIC_BLOCK})?"
        # Optional `extends Parent` clause (Parent may be generic)
        r"(?:\s+extends\s+[\w.<>,\s]+?)?"
        # Optional `implements Iface1, Iface2<T>` clause
        r"(?:\s+implements\s+[\w.<>,\s]+?)?"
        # Whitespace + opening brace
        r"\s*\{",
        re.MULTILINE,
    )

    # Inner method pattern. Modifiers section is OPTIONAL (`*` not `+`)
    # so package-private methods are captured. After the optional
    # modifiers/annotations section we require:
    #   - optional method-level generics: <T>, <T extends Foo>
    #   - return type token(s) (1+ — excludes constructors)
    #   - method name (captured)
    #   - argument list (captured for parity with line-3694 functions)
    #   - optional `throws Foo, Bar`
    #   - opening brace
    method_pattern_inner = re.compile(
        # Optional modifiers / annotations / whitespace prefix. Note this
        # is OPTIONAL — package-private methods have no modifier.
        r"(?:(?:public|private|protected|static|final|synchronized|"
        r"native|abstract|default|strictfp)\s+)*"
        # Optional method-level generic parameters: `<T>`, `<T extends Foo>`,
        # `<K, V>`. Greedy on the inside.
        r"(?:<[^>]*>\s+)?"
        # Return-type token(s). Requires at least one — this is what
        # excludes constructors (which have no return type). Generics +
        # array brackets allowed.
        r"(?:[\w<>\[\],\s]+\s+)"
        # Method name
        r"([\w]+)\s*"
        # Argument list
        r"\(([^)]*)\)\s*"
        # Optional throws clause
        r"(?:throws\s+[\w.,\s]+)?\s*"
        # Opening brace (method body — `;` for abstract/interface is
        # excluded here; abstract methods are intentionally captured only
        # when they have a body).
        r"\{",
        re.MULTILINE,
    )

    methods: List[str] = []
    seen: set = set()
    for m in class_decl_pattern.finditer(content_clean):
        # m.end() is just past the `{`. Find the matching close.
        class_open_pos = m.end() - 1  # position of `{`
        class_open_line = content_clean[:class_open_pos].count("\n") + 1
        class_close_line = _extract_balanced_block(
            source_lines, class_open_line, max_lookahead=2000
        )
        target_newlines = class_close_line - class_open_line
        if target_newlines <= 0:
            continue
        # Find the byte offset for the close line by counting newlines.
        block_start_pos = class_open_pos + 1
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        class_body = content_clean[block_start_pos:block_end_pos]

        # Strip nested class/interface/enum blocks so their methods don't
        # leak into the outer class. We iteratively find every nested
        # declaration and excise its balanced body.
        stripped_body = _strip_nested_java_classes(class_body)

        for fm in method_pattern_inner.finditer(stripped_body):
            name = fm.group(1)
            # Skip control-flow keywords that pattern-match like methods.
            if name in (
                "if", "while", "for", "switch", "catch", "try", "else",
                "return", "synchronized", "do", "throw",
            ):
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)
    return methods


def _strip_nested_java_classes(body: str) -> str:
    """Remove nested ``class|interface|enum <Name> { ... }`` blocks from
    ``body`` so methods declared in inner classes don't leak into the
    outer class's method list.

    Iteratively scans ``body`` for nested class/interface/enum
    declarations. For each match, walks the body character-by-character
    counting braces (with a small string-literal state machine to skip
    braces inside Java string/char literals) to find the matching close
    brace, then excises the entire nested span (from the ``class``
    keyword through the closing ``}``) replacing it with a single space.

    The caller (``_java_methods_for_class``) passes ``content_clean``
    where line + block comments are already stripped, so only string
    literals can confuse the brace counter — handled by the state machine.

    Returns the body with each nested block excised. The outer-class
    method scan operates on the result.
    """
    _GENERIC_BLOCK = r"<[^<>]*(?:<[^<>]*>[^<>]*)*>"
    nested_decl_pattern = re.compile(
        r"\b(?:class|interface|enum)\s+\w+"
        rf"(?:\s*{_GENERIC_BLOCK})?"
        r"(?:\s+extends\s+[\w.<>,\s]+?)?"
        r"(?:\s+implements\s+[\w.<>,\s]+?)?"
        r"\s*\{",
    )
    out_parts: List[str] = []
    cursor = 0
    body_len = len(body)
    while cursor < body_len:
        m = nested_decl_pattern.search(body, cursor)
        if not m:
            out_parts.append(body[cursor:])
            break
        decl_start = m.start()
        decl_open_brace_pos = m.end() - 1  # position of `{`
        out_parts.append(body[cursor:decl_start])
        close_pos = _find_matching_brace(body, decl_open_brace_pos)
        if close_pos < 0:
            # Unbalanced — append the rest verbatim and stop.
            out_parts.append(body[decl_start:])
            cursor = body_len
            break
        # Replace the nested span with a single space.
        out_parts.append(" ")
        cursor = close_pos + 1
    return "".join(out_parts)


def _find_matching_brace(s: str, open_pos: int) -> int:
    """Return the position of the brace that matches the ``{`` at
    ``open_pos``, or -1 if unbalanced.

    Uses a per-character state machine that skips braces inside Java
    string literals (``"..."``) and char literals (``'...'``). Assumes
    comments have already been stripped by the caller (the Java parser
    does this before calling ``_java_methods_for_class``).
    """
    if open_pos >= len(s) or s[open_pos] != "{":
        return -1
    counter = 0
    in_string: Optional[str] = None  # which quote char opened
    i = open_pos
    n = len(s)
    while i < n:
        ch = s[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                # Escaped char — skip both.
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == '"' or ch == "'":
            in_string = ch
            i += 1
            continue
        if ch == "{":
            counter += 1
        elif ch == "}":
            counter -= 1
            if counter == 0:
                return i
        i += 1
    return -1




def _csharp_methods_for_class(
    content_clean: str,
    class_name: str,
    source_lines: List[str],
) -> List[str]:
    """V52-O.11.F.2-CSHARP (v0.2.52, 2026-06-09): extract methods declared
    inside the lexical body of a C# ``class``, ``struct``, ``record``, or
    ``interface`` named ``class_name``.

    Replaces the pre-V52-O.11.F.2-CSHARP line at ``analyze_code_graph.py:2540``
    that ran ``method_pattern.finditer(content_clean)`` unconditionally —
    the same bug Rust V52-O.11.F closed, applied to the C# parser. Audit
    a79152 confirmed: a 50-method file with 3 classes produced 150
    incorrect method attributions per the
    ``query_code_structure(methods, ClassName)`` MCP path.

    C# method-scoping model (lexical, like Java):

      Methods live between a type's opening ``{`` and its matching close.
      Unlike Rust there is no separate ``impl`` block — declarations are
      lexically inside the class body. Same shape as Java, but with C#
      specifics:
        * Modifiers: ``public``, ``private``, ``protected``, ``internal``,
          ``protected internal``, ``private protected``, ``static``,
          ``virtual``, ``override``, ``sealed``, ``abstract``, ``async``,
          ``extern``, ``partial``, ``unsafe``, ``readonly`` (on structs).
        * Properties (``public int Foo { get; set; }``) — we INCLUDE the
          property name in the methods list. Rationale: mirrors how the
          Python parser counts ``@property`` decorated members, and the
          underlying ``get_Foo`` / ``set_Foo`` are real methods at the
          CLR level. The task brief calls this out explicitly.
        * Indexers (``public T this[int i] { get; set; }``) — surfaced
          as ``Item`` per the CLR property convention (``get_Item`` /
          ``set_Item``).
        * Records: ``record Foo(int X, int Y)`` — the positional
          parameters generate auto-property accessors. We do NOT try to
          extract those from the primary-constructor signature; only
          explicit declarations inside the record body land in methods.
        * Async methods (``async Task<T> Foo()``) — picked up.
        * Generic methods (``T Method<U>(U arg)``) — picked up.
        * Partial classes (``partial class Foo { ... } partial class Foo
          { ... }``) — every matching declaration's body contributes; the
          union is returned (mirrors Rust's multi-``impl`` behavior).

    Algorithm:

      1. Locate every class/struct/record/interface declaration whose
         name matches ``class_name``. Multiple declarations are allowed
         (partial classes). The class header regex tolerates the same
         modifier mix as the parser's outer ``class_pattern`` plus an
         optional inheritance clause (``: BaseClass, IInterface``).
      2. For each match, use ``_extract_balanced_block`` to find the
         body's closing brace (already brace-balanced via V52-O.11.E).
      3. Scan the body text (sliced from ``content_clean``, NOT
         ``source_lines`` — keeps the same comment-stripped surface the
         old per-file regex used) for:
            a. Method declarations: ``[modifiers] [returntype] Name(args) {`` or
               ``[modifiers] [returntype] Name(args) =>`` (expression-bodied)
               or ``[modifiers] [returntype] Name(args);`` (abstract /
               interface).
            b. Property declarations: ``[modifiers] [type] Name { get; set; }``
               (no parens after the name) — the body starts directly with
               ``{`` containing ``get``/``set``.
            c. Indexer declarations: ``[modifiers] [type] this[...] {`` —
               normalized to the literal ``Item``.
      4. Filter out C# keywords that the method-regex could otherwise
         hit (``if``, ``while``, ``for``, ``foreach``, ``switch``, ``try``,
         ``catch``, ``return``, ``new``, ``throw``, ``using``, ``lock``,
         ``yield``).
      5. Return deduplicated list, preserving first-seen order.

    Returns empty list if no class/struct/record/interface body matches
    ``class_name`` — common when ``class_name`` was extracted from a
    forward-declaration or a partial class whose other halves live in
    different files.

    Limitations:
      * Constructors are extracted as methods (their "name" matches the
        class name). The existing Java/JS parsers behave the same way;
        the embedding pipeline doesn't distinguish.
      * Static constructors (``static Foo() { ... }``) are also captured.
      * Operator overloads (``public static Foo operator+(Foo a, Foo b)``)
        are NOT captured — the name after ``operator`` isn't a valid
        identifier. This matches the upstream parser's behavior.
      * Nested types inside the class body are correctly scoped via
        brace-balance: members of nested types fall inside the outer
        class's braces but ALSO inside the nested type's own braces, so
        we'd double-count them if not careful. We handle this by
        recognizing that the nested type's header itself doesn't match
        the method pattern, but its INNER members do — so a nested class
        ``Inner`` inside ``Outer`` would leak ``Inner``'s members into
        ``Outer``'s methods. We accept this minor over-count for now
        (matches Java's behavior); a tree-sitter rewrite is the right
        fix (queued as V52-O.11.G).
      * Doesn't follow ``partial`` declarations across files (each file
        is parsed independently — same as Java's package-private split).
    """
    escaped = re.escape(class_name)
    # Class/struct/record/interface header. Modifiers + keyword + name +
    # optional generics + optional inheritance + opening brace. The name
    # capture is anchored by the `escaped` literal so we only match THE
    # target class (not other classes that happen to start with the same
    # prefix).
    #
    # Why not reuse the parser's outer `class_pattern`? That one captures
    # the name into group(1) for class-info population. Here we need to
    # gate on a SPECIFIC name and find the body opener — a different shape.
    class_header_pattern = re.compile(
        # Optional modifiers (zero or more, repeated). C# allows any order.
        r"(?:(?:public|private|protected|internal|abstract|sealed|partial|"
        r"static|unsafe|readonly|ref)\s+)*"
        # Keyword introducing the type
        r"(?:class|struct|record|interface)\s+"
        # The target name (exact match — escaped). Trailing word boundary
        # prevents `Stress` from matching `Stress2` (a strict-prefix
        # superset name). Critical: without `\b` the regex matches BOTH
        # the literal name AND any longer name starting with it, causing
        # body extraction to fall through to the next class's brace.
        rf"{escaped}\b"
        # Optional generic parameters: <T>, <T, U>, <T : IFoo>
        r"(?:\s*<[^>]*>)?"
        # Optional primary-constructor parameter list (record Foo(int X, int Y))
        r"(?:\s*\([^)]*\))?"
        # Optional pre-body trailer: covers BOTH the inheritance clause
        # (`: Base, IFoo<T>`) AND the generic constraints (`where T : new()`).
        # We accept any sequence of non-`{` chars; the `where` clause's
        # own `()` and `<>` are safely consumed because the only stopping
        # condition is the opening brace. This single permissive trailer
        # handles all of:
        #     class Foo : Base { ... }
        #     class Foo<T> where T : new() { ... }
        #     class Foo<T> : Base where T : new() { ... }
        #     class Foo<T, U> where T : class where U : struct, new() { ... }
        r"[^{]*"
        # Opening brace
        r"\{",
        re.MULTILINE,
    )

    # Inner method/property/indexer pattern. Three shapes packed into one
    # alternation so a single pass over the class body collects all three.
    #
    # Strategy:
    #   * Anchor on start-of-line (re.MULTILINE + `^[ \t]*`) — declarations
    #     in C# are always on their own line, statements inside methods
    #     are indented further or follow other statements.
    #   * Modifiers are OPTIONAL because (a) interface members have no
    #     explicit modifier prior to C# 8 default-interface-methods, and
    #     (b) struct members can also be implicit-private.
    #   * To compensate for optional modifiers, we filter out matches whose
    #     captured name is a C# control-flow keyword (`if`, `return`,
    #     `new`, etc.) — that catches the common false-positive shapes
    #     like `return new U();` and `where U : T, new()`.
    #   * We also filter out matches where the supposed return type IS
    #     itself a control-flow keyword (`return`, `throw`, etc.) — that
    #     filters `return new U();` more aggressively.
    #
    # Shape M (method):
    #   ^[ws] [modifier]* [returntype] Name(args) ( { | => | ; )
    # Shape P (property):
    #   ^[ws] [modifier]* [type] Name { get ...|set ...|init ... }
    # Shape I (indexer):
    #   ^[ws] [modifier]* [type] this[args] {
    method_modifier_alt = (
        r"(?:public|private|protected|internal|static|virtual|override|"
        r"async|abstract|sealed|extern|partial|new|unsafe|readonly)"
    )
    # Zero or more modifier tokens (with whitespace between). Optional
    # so interface methods (no modifier) and implicit-private members
    # still match. We compensate via the keyword filter below.
    method_modifiers_opt = rf"(?:{method_modifier_alt}\s+)*"
    # Compact type pattern — matches type-shaped tokens like:
    #   T, int, string, Task, Task<int>, IList<T>, T[], int?, Dictionary<K,V>,
    #   IList<KeyValuePair<string, object>>  (nested generics)
    # The `\w` alternative handles single-letter generic params; the
    # longer alternative handles compound generics.
    #
    # CRITICAL: type_shape MUST NOT allow whitespace at the top level,
    # only inside angle brackets. Otherwise `return new U(` matches with
    # type_shape spanning `return new` (taking the whitespace) and name
    # being `U` — false positive. We allow whitespace only via nested
    # generic groups that swallow arbitrary text including commas/spaces.
    #
    # Nested generics: Python regex doesn't support true recursion, but we
    # can hand-roll a 3-level nested-generic pattern that covers all
    # practical cases. Format builds bottom-up:
    #   level0: <...> with no nested angles
    #   level1: <... level0 ...> — one level of nesting
    #   level2: <... level1 ...> — two levels of nesting (e.g.
    #     IList<KeyValuePair<string, object>>)
    #   level3: <... level2 ...> — three levels (e.g.
    #     Task<Dictionary<int, List<string>>>)
    # A 4th level (Task<Dictionary<int, List<Dictionary<...>>>>) is
    # exceedingly rare and falls back to graceful failure (method not
    # captured; doesn't break the parser).
    _ang_lvl0 = r"<[^<>]*>"
    _ang_lvl1 = rf"<(?:[^<>]|{_ang_lvl0})*>"
    _ang_lvl2 = rf"<(?:[^<>]|{_ang_lvl1})*>"
    _generic_block = _ang_lvl2
    type_shape = (
        # First char: word or dot
        r"[\w.]"
        # Then any mix of:
        #   * word/dot/?/[]
        #   * nested generic block (covers up to 2 levels of nesting)
        rf"(?:[\w.\[\]\?]|{_generic_block})*"
    )
    method_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        # Return type
        + type_shape + r"\s+"
        # Method name (captured)
        + r"([\w]+)"
        # Optional generic type parameters on the method
        + r"(?:\s*<[^>]*>)?"
        # Argument paren (anchor)
        + r"\s*\(",
        re.MULTILINE,
    )
    property_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        # Return type
        + type_shape + r"\s+"
        # Property name (captured) followed by `{` (NOT `(`)
        + r"([\w]+)\s*\{"
        # Lookahead for accessor keyword to confirm this is a property
        + r"(?=\s*(?:[\[\w]|//|/\*)*\s*(?:get|set|init)\b)",
        re.MULTILINE,
    )
    indexer_decl = re.compile(
        r"^[ \t]*"
        + method_modifiers_opt
        + type_shape + r"\s+"
        + r"(this)\s*\[[^\]]*\]\s*\{",
        re.MULTILINE,
    )

    # Tokens that look like an identifier in capture position but are
    # actually C# control-flow keywords. Filter these out.
    _CSHARP_KW_FILTER = {
        "if", "else", "while", "for", "foreach", "switch", "try", "catch",
        "finally", "return", "new", "throw", "using", "lock", "yield",
        "do", "break", "continue", "goto", "case", "default", "checked",
        "unchecked", "fixed", "stackalloc", "await", "is", "as", "in",
        "out", "ref", "params", "where", "when", "var", "true", "false",
        "null", "this", "base", "typeof", "sizeof", "nameof",
    }

    methods: List[str] = []
    seen: set = set()
    for hdr in class_header_pattern.finditer(content_clean):
        # Locate the opening brace position (regex anchors on it).
        # hdr.end() is one past `{`; the brace itself is at end()-1.
        body_open_pos = hdr.end() - 1
        body_open_line = content_clean[:body_open_pos].count("\n") + 1
        body_close_line = _extract_balanced_block(
            source_lines, body_open_line, max_lookahead=800
        )
        # Convert close-line back to a char offset in content_clean by
        # counting newlines from body_open_pos onward.
        target_newlines = body_close_line - body_open_line
        if target_newlines <= 0:
            continue
        block_start_pos = body_open_pos + 1  # skip the `{`
        seen_newlines = 0
        block_end_pos = block_start_pos
        while seen_newlines < target_newlines and block_end_pos < len(content_clean):
            if content_clean[block_end_pos] == "\n":
                seen_newlines += 1
            block_end_pos += 1
        body = content_clean[block_start_pos:block_end_pos]

        # Indexers FIRST — the indexer regex captures the literal `this`
        # which we map to `Item`. Doing this before the method regex
        # avoids the method regex over-matching on `this(` constructor
        # chains (rare, but defensive).
        for im in indexer_decl.finditer(body):
            if "Item" in seen:
                continue
            seen.add("Item")
            methods.append("Item")

        # Methods second.
        for mm in method_decl.finditer(body):
            name = mm.group(1)
            if name in _CSHARP_KW_FILTER:
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)

        # Properties third — the property regex is anchored on `{` with
        # no preceding `(`, so it doesn't double-match methods.
        for pm in property_decl.finditer(body):
            name = pm.group(1)
            if name in _CSHARP_KW_FILTER:
                continue
            if name in seen:
                continue
            seen.add(name)
            methods.append(name)

    return methods




def _strip_string_literals(content_clean: str) -> str:
    """V52-O.11.I (v0.2.52, 2026-06-09): replace the *contents* of every
    single-line single-quoted, double-quoted, and backtick-quoted string
    with same-length whitespace so downstream regex scans don't match
    against text that LOOKS like code but is actually string content.

    Audit example: ``let s = "fn foo() {}";`` produces a false-positive
    function row for a function named ``foo`` in the Rust/JS/Java/C#
    parsers. The string-stripped version reads as
    ``let s = "          ";`` and the regex correctly finds no ``fn`` to
    match.

    Line-number preservation: each stripped string is replaced with the
    same number of bytes (padded with spaces, with newlines preserved as
    newlines on the rare multi-line case where an escape sequence
    happens to include one). This keeps downstream ``content[:m.start()]
    .count('\\n') + 1`` line calculations correct.

    Quote semantics:
      - ``'..'`` and ``".."`` handle escape sequences (``\\"``, ``\\'``).
      - Backtick-quoted (JS template literals) — single-line only,
        no ``${}`` interpolation handling. A template literal containing
        ``${expr}`` collapses to blanks too, which is correct (we want
        to mask the literal text, not the embedded code).

    Caller is expected to have already stripped comments. Multi-line
    raw strings (Rust ``r#"..."#``, Python triple-quotes — those are
    handled by ``_strip_triple_quoted``) are out of scope.

    The implementation uses a single regex sweep instead of three passes
    so overlapping quote characters inside another quote type don't
    confuse the order-of-stripping (e.g. ``" 'embedded' "`` must NOT
    re-trigger the single-quote pass after the double-quote pass).
    """
    # Combined alternation: try each quote type in turn. The regex engine
    # picks the leftmost match each iteration, which is what we want.
    pattern = re.compile(
        r'"(?:\\.|[^"\\])*"'      # double-quoted (escape-aware)
        r"|'(?:\\.|[^'\\])*'"     # single-quoted (escape-aware)
        r"|`(?:\\.|[^`\\])*`",    # backtick (template literal)
    )

    def _blank(match: "re.Match[str]") -> str:
        # Replace the matched string (inclusive of quotes) with spaces of
        # the same length, but preserve any newlines inside (escape
        # sequences like ``"\n"`` are written as a literal ``\``+``n`` in
        # source, so they don't actually contain a newline — we still
        # play it safe and preserve embedded newlines).
        text = match.group(0)
        return ''.join(ch if ch == '\n' else ' ' for ch in text)

    return pattern.sub(_blank, content_clean)




def _is_rust_test_fn(content: str, fn_offset: int) -> bool:
    """V52-O.11.J (v0.2.52, 2026-06-09): return True if the Rust ``fn``
    starting at ``content[fn_offset:]`` is gated by a ``#[cfg(test)]``,
    ``#[test]``, or ``#[cfg(any(test, ...))]`` attribute on an
    immediately-preceding line (test-only code, not production).

    Pre-V52-O.11.J the Rust parser indexed every function the regex
    matched into ``CodeFunction``, including unit-test helpers and
    ``#[test]``-annotated test functions. That pollutes search results
    (``query_code_structure(callers, ...)`` returns test fixtures), wastes
    embedding budget, and conflates production behaviour with test
    scaffolding. After V52-O.11.J the analyzer skips them.

    Detection algorithm:
      1. Walk backwards from ``fn_offset`` over whitespace and
         line-continuation characters until we find the start of the line
         that contains the ``fn`` keyword (the "fn line").
      2. Walk backwards from the start of the fn line through one OR more
         immediately-preceding lines that are EITHER blank OR start with
         ``#[`` (Rust attributes). Stop at the first line that is neither.
      3. Across all the attribute lines collected, look for ``cfg(test)``,
         ``cfg(any(test``, ``cfg(all(test``, or bare ``[test]``.

    Args:
        content: The source (or content_no_strings — string-literal
            content can't contain ``#[cfg(test)]`` because attributes
            don't appear inside strings, so either input works).
        fn_offset: Byte offset where the matched ``fn`` (or its prefix
            modifier) starts.

    Returns:
        True if a test-gating attribute is found on a preceding attribute
        line, False otherwise.
    """
    # Find start-of-line for the fn-offset.
    line_start = content.rfind('\n', 0, fn_offset) + 1
    # ``rfind`` returns -1 if no newline; +1 makes it 0 — correct for
    # offset at very start of file.

    # Walk backwards collecting preceding attribute lines.
    # Each iteration: find the line PRECEDING ``line_start`` and check
    # whether it's blank or an attribute. Stop at the first non-attribute
    # non-blank line.
    cursor = line_start
    attr_lines: list[str] = []
    # Cap the scan at 16 lines back to bound worst case (test fns rarely
    # have more than 2-3 attribute lines).
    for _ in range(16):
        if cursor <= 0:
            break
        # ``cursor`` points at start of "current" line (which we've
        # already classified or want to skip past). Find the line BEFORE
        # this one.
        # Previous newline ends the previous line.
        prev_nl = content.rfind('\n', 0, cursor - 1)
        prev_line_start = prev_nl + 1  # 0 if rfind returned -1
        prev_line = content[prev_line_start: cursor - 1]
        stripped = prev_line.strip()
        if not stripped:
            # Blank line — keep walking (Rust allows blank lines between
            # attributes and the fn declaration, though it's unusual).
            cursor = prev_line_start
            continue
        if stripped.startswith('#['):
            attr_lines.append(stripped)
            cursor = prev_line_start
            continue
        # Non-blank, non-attribute line: stop scanning.
        break

    # Inspect collected attribute lines for any test gate.
    for attr in attr_lines:
        # Normalise: collapse whitespace so ``#[ cfg ( test ) ]`` etc.
        # all reduce to the same pattern.
        compact = re.sub(r'\s+', '', attr)
        # ``#[test]`` (bare test attribute)
        if '#[test]' in compact:
            return True
        # ``#[cfg(test)]`` — direct test cfg
        if '#[cfg(test)]' in compact:
            return True
        # ``#[cfg(any(test, ...))]`` and ``#[cfg(all(test, ...))]``
        if '#[cfg(any(test,' in compact or '#[cfg(all(test,' in compact:
            return True
        # cfg(...) where test appears later in the predicate
        if '#[cfg(' in compact and ('(test,' in compact or ',test)' in compact or ',test,' in compact):
            return True

    return False




def _shape_for_insert(embedding: Optional[Any]) -> Optional[Any]:
    """Shape ``embedding`` for ``collection.data.insert(vector=)``.

    Logic:
      * None → None (caller's ``if embedding:`` skips the vector= kwarg).
      * ``dict`` (multi-slot) → return as-is.
      * ``list`` AND ``DUAL_EMBEDDING_ENABLED`` → wrap as
        ``{active_slot: list}`` so Weaviate routes it to the right named
        vector.
      * ``list`` AND legacy mode → pass through (flat vector).
    """
    if not embedding:
        return None
    if isinstance(embedding, dict):
        return embedding
    if DUAL_EMBEDDING_ENABLED:
        return {_active_code_vector_slot(): embedding}
    return embedding


def embed_function(signature: str, body: str, language: str = "python") -> Optional[Any]:
    """Truncate function smartly, then generate embedding.

    Returns a slot dict (multi-slot mode), a flat vector (legacy mode),
    or None on backend failure.
    """
    text = truncate_function_for_embedding(signature, body, language=language, model=_resolve_code_model_id())
    return generate_embedding(text)


def embed_class(signature: str, class_body: str, methods: Optional[List[str]] = None,
                language: str = "python") -> Optional[Any]:
    """Truncate class smartly, then generate embedding."""
    text = truncate_class_for_embedding(signature, class_body, methods=methods,
                                        language=language, model=_resolve_code_model_id())
    return generate_embedding(text)


def embed_module(module_summary: str) -> Optional[Any]:
    """Truncate module summary, then generate embedding."""
    text = truncate_module_for_embedding(module_summary, model=_resolve_code_model_id())
    return generate_embedding(text)


# ---------------------------------------------------------------------------
# Cross-language call extraction
# ---------------------------------------------------------------------------

# HTTP client library → canonical name (used as import gate)
_HTTP_LIBS: Dict[str, str] = {
    # Python
    "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp",
    "urllib.request": "urllib", "urllib3": "urllib3",
    # JS/TS
    "axios": "axios", "node-fetch": "node-fetch", "got": "got",
    "cross-fetch": "cross-fetch",
    # Ruby
    "net/http": "net/http", "faraday": "faraday", "httparty": "httparty",
    "rest-client": "rest-client",
}
_GRPC_LIBS = {"grpc", "grpc-js", "@grpc/grpc-js", "grpc.io", "google.golang.org/grpc"}
_MQ_LIBS: Dict[str, str] = {
    "kafka-python": "kafka", "confluent-kafka": "kafka", "kafka": "kafka",
    "kafkajs": "kafka", "pika": "rabbitmq", "amqplib": "rabbitmq",
    "aio-pika": "rabbitmq", "redis": "redis",
}
_WS_LIBS = {"websocket", "websocket-client", "websockets", "socket.io-client", "ws"}


def _strip_triple_quoted(content: str) -> str:
    """Remove Python/JS triple-quoted strings to avoid extracting URLs from docstrings."""
    content = re.sub(r'""".*?"""', '""', content, flags=re.DOTALL)
    content = re.sub(r"'''.*?'''", "''", content, flags=re.DOTALL)
    return content


def _extract_external_calls(
    content_clean: str,
    imports: List[str],
    language: str,
    source_file: str = "",
) -> List[Dict[str, str]]:
    """
    Extract cross-language / cross-service communication calls from source code.

    False-positive prevention strategy:
    1. Import gate: only trigger when the relevant client library is imported.
    2. Literal gate: only extract calls where a literal string (not a plain variable)
       is used as the target. Partial templates (f"{VAR}/literal") yield medium confidence.
    3. Scope gate: strip triple-quoted strings so URLs in docstrings are ignored.

    Returns list of dicts with keys:
        interaction_type, direction, protocol, endpoint, raw_target, confidence
    """
    results: List[Dict[str, str]] = []

    # Normalise imports to a flat set of lowercase strings
    import_set = {i.lower().strip() for i in imports}

    def _has_any(lib_keys) -> bool:
        return any(k in import_set for k in lib_keys)

    # Work on comment-stripped, triple-quote-stripped content
    c = _strip_triple_quoted(content_clean)

    # -----------------------------------------------------------------------
    # HTTP calls
    # -----------------------------------------------------------------------
    http_lib = None
    for k, v in _HTTP_LIBS.items():
        if k in import_set:
            http_lib = v
            break

    # Shell: gate on literal `curl` or `wget` command
    if language == "shell":
        http_lib = "curl/wget"  # always check shell files for curl/wget

    if http_lib or language in ("csharp",):
        # Literal URL patterns — only http(s):// or ws(s):// URLs
        # Match: method("URL"  or  method('URL'  or  method(`URL`  (no ${} inside)
        literal_url = re.compile(
            r'(?:'
            # requests/httpx/aiohttp style: lib.method(["']url["']
            r'(?:requests|httpx|aiohttp|http|client|session|RestTemplate|HttpClient|'
            r'fetch|axios|got|Faraday|HTTParty|Net::HTTP|curl)\s*[.(]\s*'
            r'(?:["\']([A-Za-z][^"\'<>\s]{4,})["\']'        # literal string arg
            r'|`((?!.*\$\{)[A-Za-z][^`<>\s]{4,})`)'         # template literal, no ${
            r'|'
            # Shell: curl/wget "url" or curl url (without quotes, not $VAR)
            r'(?:curl|wget)(?:\s+-[^\s]+)*\s+'
            r'(?:["\']?(https?://[^\s"\'$<>]{5,})["\']?)'
            r')',
            re.MULTILINE,
        )
        for m in literal_url.finditer(c):
            raw = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if not raw or raw.startswith("$"):
                continue
            # Infer HTTP method from context
            ctx = c[max(0, m.start() - 60):m.start() + len(raw) + 10].lower()
            method = "GET"
            for verb in ("post", "put", "patch", "delete"):
                if verb in ctx:
                    method = verb.upper()
                    break
            # Extract just the path if it's a full URL
            try:
                from urllib.parse import urlparse as _up
                parsed = _up(raw)
                endpoint = parsed.path or raw
                if parsed.scheme in ("ws", "wss"):
                    results.append({
                        "interaction_type": "websocket", "direction": "outbound",
                        "protocol": parsed.scheme.upper(), "endpoint": endpoint,
                        "raw_target": raw, "confidence": "high",
                    })
                    continue
            except Exception:
                endpoint = raw
            results.append({
                "interaction_type": "http", "direction": "outbound",
                "protocol": method, "endpoint": endpoint,
                "raw_target": raw, "confidence": "high",
            })

        # Partial template: f"{VAR}/literal/path" or `${VAR}/literal/path`
        partial_template = re.compile(
            r'(?:f["\']|`)'                     # f-string or template literal
            r'(?:\{[^}]+\}|\$\{[^}]+\})'        # variable substitution at start
            r'(/[A-Za-z0-9/_-]{3,})'            # literal path segment follows
        )
        for m in partial_template.finditer(c):
            path = m.group(1)
            if http_lib and len(path) >= 4:
                # Only emit if there's a call context nearby
                ctx = c[max(0, m.start() - 100):m.start() + 10].lower()
                if any(k in ctx for k in ("get(", "post(", "put(", "delete(", "patch(", "fetch(", "request(")):
                    results.append({
                        "interaction_type": "http", "direction": "outbound",
                        "protocol": "HTTP", "endpoint": path,
                        "raw_target": m.group(0), "confidence": "medium",
                    })

    # -----------------------------------------------------------------------
    # gRPC calls
    # -----------------------------------------------------------------------
    if _has_any(_GRPC_LIBS):
        # Python/JS: SomeStub(channel).MethodName(request) or stub.MethodName(request)
        # Go: conn, _ := grpc.Dial("host:port", ...)
        grpc_dial = re.compile(r'grpc\.(?:Dial|dial|insecure_channel|secure_channel)\s*\(\s*["\']([^"\']+)["\']')
        for m in grpc_dial.finditer(c):
            raw = m.group(1)
            results.append({
                "interaction_type": "grpc", "direction": "outbound",
                "protocol": "gRPC", "endpoint": f"grpc:{raw}",
                "raw_target": raw, "confidence": "high",
            })

        # Stub method call: SomeServiceStub.MethodName( or stub.MethodName(
        stub_call = re.compile(r'\b(\w*(?:Stub|Client|ServiceClient))\s*\.\s*(\w+)\s*\(')
        for m in stub_call.finditer(c):
            stub, method = m.group(1), m.group(2)
            if method.lower() in ("__init__", "new", "create", "connect", "close", "init"):
                continue
            results.append({
                "interaction_type": "grpc", "direction": "outbound",
                "protocol": "gRPC", "endpoint": f"grpc:{stub}.{method}",
                "raw_target": f"{stub}.{method}()", "confidence": "medium",
            })

    # -----------------------------------------------------------------------
    # Message queue calls
    # -----------------------------------------------------------------------
    mq_lib = None
    for k, v in _MQ_LIBS.items():
        if k in import_set:
            mq_lib = v
            break

    if mq_lib == "kafka":
        # Python kafka: producer.send("topic-name", ...)
        # JS kafkajs: producer.send({ topic: "literal", ... })
        kafka_send = re.compile(
            r'(?:'
            r'(?:producer|kafka)\s*\.\s*send\s*\(\s*["\']([^"\']+)["\']'  # Python style
            r'|topic:\s*["\']([^"\']+)["\']'                               # JS object style
            r')'
        )
        for m in kafka_send.finditer(c):
            topic = (m.group(1) or m.group(2) or "").strip()
            if topic:
                results.append({
                    "interaction_type": "mq", "direction": "pubsub",
                    "protocol": "kafka", "endpoint": f"topic:{topic}",
                    "raw_target": topic, "confidence": "high",
                })

    if mq_lib == "rabbitmq":
        # Python pika: channel.basic_publish(exchange='x', routing_key='queue')
        rmq_pub = re.compile(
            r'basic_publish\s*\([^)]*routing_key\s*=\s*["\']([^"\']+)["\']'
        )
        for m in rmq_pub.finditer(c):
            key = m.group(1)
            results.append({
                "interaction_type": "mq", "direction": "pubsub",
                "protocol": "rabbitmq", "endpoint": f"queue:{key}",
                "raw_target": key, "confidence": "high",
            })
        # exchange
        rmq_exch = re.compile(
            r'basic_publish\s*\([^)]*exchange\s*=\s*["\']([^"\']+)["\']'
        )
        for m in rmq_exch.finditer(c):
            exch = m.group(1)
            if exch:  # skip empty exchange (default direct exchange)
                results.append({
                    "interaction_type": "mq", "direction": "pubsub",
                    "protocol": "rabbitmq", "endpoint": f"exchange:{exch}",
                    "raw_target": exch, "confidence": "high",
                })

    if mq_lib == "redis":
        # Redis pub/sub: r.publish("channel", message)
        redis_pub = re.compile(r'\.publish\s*\(\s*["\']([^"\']+)["\']')
        for m in redis_pub.finditer(c):
            ch = m.group(1)
            results.append({
                "interaction_type": "mq", "direction": "pubsub",
                "protocol": "redis", "endpoint": f"channel:{ch}",
                "raw_target": ch, "confidence": "high",
            })

    # -----------------------------------------------------------------------
    # WebSocket calls (when WS library imported but not caught by HTTP block)
    # -----------------------------------------------------------------------
    if _has_any(_WS_LIBS):
        ws_connect = re.compile(
            r'(?:WebSocketApp|create_connection|WebSocket|io)\s*\(\s*["\']'
            r'(wss?://[^"\'<>\s]{5,})["\']'
        )
        for m in ws_connect.finditer(c):
            raw = m.group(1)
            try:
                from urllib.parse import urlparse as _up
                parsed = _up(raw)
                endpoint = parsed.netloc + parsed.path
            except Exception:
                endpoint = raw
            results.append({
                "interaction_type": "websocket", "direction": "outbound",
                "protocol": "WS", "endpoint": endpoint,
                "raw_target": raw, "confidence": "high",
            })

    # Deduplicate by (interaction_type, protocol, endpoint)
    seen: set = set()
    deduped: List[Dict[str, str]] = []
    for r in results:
        key = (r["interaction_type"], r["protocol"], r["endpoint"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped


# =============================================================================
# V52-O.11.B (v0.2.53 Track E) — Svelte parser helpers.
#
# Before v0.2.53, the orchestrator's 244 .svelte files in launcher/src/
# were indexed as ZERO functions in the code graph (they fell through
# every language detector because no extension match existed). This
# module-level helper extracts the parsing logic from
# `_analyze_svelte_file` so it can be unit-tested in isolation.
#
# Svelte component structure:
#   * Optional top-level <script lang="ts"|"js"> block — host for
#     reactive state, lifecycle hooks, and named functions.
#   * Optional <script context="module"> block — module-level state
#     and helpers shared across component instances (own scope).
#   * Optional <style> block — CSS / preprocessor. Ignored for
#     code-graph purposes.
#   * Template body — HTML-like markup, mustaches, control flow.
#     Ignored for code-graph purposes (no semantic functions there).
#
# The parser:
#   1. Extracts the <script> block bodies (both default + module).
#   2. Parses top-level `function name(...)` declarations.
#   3. Parses top-level `export function name(...)` exports.
#   4. Parses arrow-function exports
#      (`export const name = () => ...` / `let` / `var`).
#   5. Parses `$: name = ...` reactive declarations as pseudo-functions
#      (they're function-shaped — a reactive expression — and useful
#      to surface in code-graph search even though they're not
#      callable directly).
#   6. Extracts the component name from the file stem (Svelte
#      convention; no class-name analogue inside the file).
# =============================================================================


# Match an opening <script ...> tag, capturing the full opening tag for
# attribute inspection (we look for context="module" to label module-
# scoped blocks).
_SVELTE_SCRIPT_OPEN = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_SVELTE_SCRIPT_CLOSE = re.compile(r"</script\s*>", re.IGNORECASE)
_SVELTE_MODULE_CONTEXT = re.compile(
    r"""context\s*=\s*['"]module['"]""", re.IGNORECASE
)

# `function name(...)` and `export function name(...)`. We capture the
# `export` prefix so the caller can tag exported functions.
_SVELTE_FUNCTION_DECL = re.compile(
    r"""^[ \t]*(export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(""",
    re.MULTILINE,
)

# Arrow-function exports: `export const name = (...) => ...`,
# `export let name = ...`, `export var name = ...`. Bound to const/let/var
# so we don't pick up arbitrary `name = (...) => ...` re-assignments
# inside function bodies.
_SVELTE_ARROW_EXPORT = re.compile(
    r"""^[ \t]*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"""
    r"""(async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>""",
    re.MULTILINE,
)

# Svelte reactive declarations: `$: name = ...`. We treat each as a
# pseudo-function because they're function-shaped reactive expressions
# (an effect that re-runs on dependency change). Surfaces in code-graph
# searches for "what reactive declarations exist".
_SVELTE_REACTIVE_DECL = re.compile(
    r"""^[ \t]*\$:\s*([A-Za-z_$][\w$]*)\s*=""",
    re.MULTILINE,
)


def _extract_svelte_script_blocks(
    content: str,
) -> List[Tuple[str, bool, int]]:
    """Extract <script>...</script> bodies from a Svelte source.

    Returns a list of (block_body, is_module_context, body_start_offset)
    tuples. `body_start_offset` is the absolute character offset in the
    original content of the first character of the block body — used by
    callers to translate per-block match offsets back to file-level
    line numbers.

    Multi-block tolerance: a Svelte file may have at most one default
    <script> + at most one <script context="module">; we return all
    matches we find rather than enforcing this constraint at parse
    time (let the orchestrator's downstream linters surface invalid
    Svelte structure).
    """
    blocks: List[Tuple[str, bool, int]] = []
    pos = 0
    while pos < len(content):
        open_match = _SVELTE_SCRIPT_OPEN.search(content, pos)
        if not open_match:
            break
        attrs = open_match.group(1) or ""
        is_module = bool(_SVELTE_MODULE_CONTEXT.search(attrs))
        body_start = open_match.end()
        close_match = _SVELTE_SCRIPT_CLOSE.search(content, body_start)
        if not close_match:
            # Unclosed script — treat the rest of the file as the body.
            blocks.append((content[body_start:], is_module, body_start))
            break
        blocks.append(
            (content[body_start:close_match.start()], is_module, body_start)
        )
        pos = close_match.end()
    return blocks


def _parse_svelte_functions(content: str) -> List[Dict[str, Any]]:
    """Parse top-level function-shaped declarations from a Svelte file.

    Returns a list of dicts with keys:

      * `name` (str)         — function / variable / reactive name
      * `kind` (str)         — one of `"function"`, `"export"`,
                              `"arrow_export"`, `"reactive"`
      * `is_async` (bool)    — True for `async function`, `async () =>`,
                              False for sync forms and reactive decls
      * `start_offset` (int) — character offset in the ORIGINAL file
                              content where the declaration starts
                              (callers translate this to a line
                              number via `content[:off].count('\n') + 1`)
      * `module_scope` (bool) — True when the declaration appeared
                              inside a `<script context="module">`
                              block; False for default-script and
                              fallback cases.

    Deduplication: if the same name appears as both a `function` decl
    and an `arrow_export` (unusual but possible if a file defines a
    helper and then re-exports a const-arrow with the same name), we
    keep BOTH entries — the dedup contract is the caller's job at
    insert time (the orchestrator's `_dedup_insert` keys on
    `full_name` + `file_path_rel`, which preserves the distinction).
    """
    results: List[Dict[str, Any]] = []
    blocks = _extract_svelte_script_blocks(content)

    # Empty-block fallback: no <script> at all → no functions to extract.
    if not blocks:
        return results

    for block_body, is_module, body_start in blocks:
        # `function` / `export function` / `async function`
        for m in _SVELTE_FUNCTION_DECL.finditer(block_body):
            name = m.group(2)
            kind = "export" if m.group(1) else "function"
            is_async = "async" in block_body[m.start():m.end()]
            results.append(
                {
                    "name": name,
                    "kind": kind,
                    "is_async": is_async,
                    "start_offset": body_start + m.start(),
                    "module_scope": is_module,
                }
            )

        # `export const name = (...) => ...` (+ let / var)
        for m in _SVELTE_ARROW_EXPORT.finditer(block_body):
            name = m.group(1)
            is_async = m.group(2) is not None
            results.append(
                {
                    "name": name,
                    "kind": "arrow_export",
                    "is_async": is_async,
                    "start_offset": body_start + m.start(),
                    "module_scope": is_module,
                }
            )

        # `$: name = ...` reactive declarations (Svelte-specific).
        # Only meaningful in the DEFAULT script (module context blocks
        # don't get the reactive runtime), so we skip them in
        # module-scoped blocks.
        if not is_module:
            for m in _SVELTE_REACTIVE_DECL.finditer(block_body):
                name = m.group(1)
                results.append(
                    {
                        "name": name,
                        "kind": "reactive",
                        "is_async": False,
                        "start_offset": body_start + m.start(),
                        "module_scope": False,
                    }
                )

    # Stable sort by start_offset so test assertions are deterministic
    # even when the regex iterators visit declarations in a non-source
    # order (which they don't today, but stable-sort future-proofs).
    results.sort(key=lambda r: r["start_offset"])
    return results


# =============================================================================
# V52-O.11.N (v0.2.53 Track E) — PowerShell parser helpers.
#
# Before v0.2.53, the orchestrator's 168 .ps1 files (template hooks
# and Windows-side scripts) were indexed as ZERO functions in the code
# graph — same fall-through bug as Svelte (no extension match).
#
# PowerShell function-declaration syntax:
#
#   function Name { ... }
#   function Name() { ... }
#   function Name($a, $b) { ... }
#   function Name {
#     param([Parameter()] $a, [Parameter()] $b)
#     ...
#   }
#   function global:Name { ... }                  # scope prefix
#   function script:Name { ... }                  # scope prefix
#   function Verb-Noun { ... }                    # idiomatic PS naming
#   filter Name { ... }                           # filter is also fn-like
#
# We capture `function` and `filter` declarations and tolerate the
# optional scope prefix + the parenthesised parameter list. We also
# extract any leading `param(...)` block as the function's signature
# extra — useful for code-graph search by parameter type.
#
# Comment regions:
#   `# comment`           — single-line
#   `<# ... #>`           — multi-line block comment
#   `#region` / `#endregion` — folding markers; we strip them as
#                              comments (no semantic meaning beyond
#                              IDE folding).
# =============================================================================


# `function name { ... }` / `function name(...) { ... }` / `filter name { ... }`.
# Matches both `function` and `filter`; captures the optional scope
# prefix (`global:`, `script:`, `local:`, `private:`) and the name.
# Trailing `(...)` or `{` is required so we don't pick up bare
# `function` keyword mentions.
_POWERSHELL_FUNCTION_DECL = re.compile(
    r"""^[ \t]*(?:function|filter)\s+"""
    r"""(?:(?P<scope>global|script|local|private):)?"""
    r"""(?P<name>[A-Za-z_][\w-]*)\s*"""
    r"""(?:\([^)]*\))?\s*"""
    r"""(?=\{)""",
    re.MULTILINE | re.IGNORECASE,
)

# `[Parameter(...)] $name` / `[Parameter()] [string]$name` etc. We
# capture the parameter name (`$name`) and the bracketed attributes
# above it so callers can render a richer signature in the code-graph
# entity.
_POWERSHELL_PARAM_ATTR = re.compile(
    r"""\[\s*Parameter\s*\([^)]*\)\s*\]"""
    r"""(?:\s*\[[^\]]+\])*"""
    r"""\s*\$(?P<name>[A-Za-z_][\w]*)""",
    re.IGNORECASE,
)


def _strip_powershell_comments(content: str) -> str:
    """Strip PowerShell single-line and block comments from source.

    Order matters: block comments (`<# ... #>`) come first so that
    `#` inside a block comment isn't picked up by the single-line
    pattern. `#region` / `#endregion` markers are stripped via the
    single-line pass.
    """
    # Block comments `<# ... #>` (greedy across lines).
    stripped = re.sub(r"<#.*?#>", " ", content, flags=re.DOTALL)
    # Single-line `#` to end-of-line. Includes `#region`, `#endregion`,
    # `#!` shebangs (not idiomatic in .ps1 but possible in cross-platform
    # scripts).
    stripped = re.sub(r"#.*$", "", stripped, flags=re.MULTILINE)
    return stripped


def _parse_powershell_functions(content: str) -> List[Dict[str, Any]]:
    """Parse function/filter declarations from a PowerShell source file.

    Returns a list of dicts with keys:

      * `name` (str)        — function name (without scope prefix)
      * `scope` (str|None)  — `"global"` / `"script"` / `"local"` /
                              `"private"` / None when unscoped
      * `kind` (str)        — `"function"` or `"filter"`
      * `start_offset` (int) — character offset in the ORIGINAL
                               content; callers translate to a line
                               number via `content[:off].count('\n') + 1`
      * `params` (List[str]) — parameter names from a `param(...)`
                               block if present, else []. Parameter
                               names are returned WITHOUT the leading
                               `$` sigil.

    The parser strips comments first so block-comment text doesn't
    masquerade as a function decl (a common .ps1 footgun: docstring-
    style `<#  function Foo  #>` blocks above the real declaration).
    """
    cleaned = _strip_powershell_comments(content)
    results: List[Dict[str, Any]] = []

    for m in _POWERSHELL_FUNCTION_DECL.finditer(cleaned):
        # Inspect the original source line to recover the kind
        # (function/filter) since the regex doesn't capture it.
        name = m.group("name")
        scope = m.group("scope")
        # Determine kind by looking at the keyword that opened the
        # match — we re-anchor on the start position because the
        # regex consumed it.
        line_start = cleaned.rfind("\n", 0, m.start()) + 1
        keyword = cleaned[line_start:m.start() + 8].strip().split()[0].lower()
        kind = "filter" if keyword.startswith("filter") else "function"

        # Find the function body span to look for a `param(...)` block.
        # We scan from the brace (`{`) following the decl forward to
        # the matching close brace, balancing nesting.
        body_start = cleaned.find("{", m.end())
        params: List[str] = []
        if body_start != -1:
            depth = 1
            body_end = body_start + 1
            while body_end < len(cleaned) and depth > 0:
                ch = cleaned[body_end]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                body_end += 1
            body = cleaned[body_start:body_end]

            # Look for `param( ... )` at top of body. We can't use a
            # bare `\(...\)` regex because the param block contains
            # nested parens (`[Parameter()]` attributes), and `[^)]*`
            # stops at the first close paren which truncates the
            # block before the first $-variable. Hand-roll the
            # balanced-paren scan instead.
            param_keyword = re.search(
                r"\bparam\s*\(",
                body,
                re.IGNORECASE,
            )
            if param_keyword:
                paren_start = param_keyword.end() - 1  # index of the `(`
                depth = 1
                pos = paren_start + 1
                while pos < len(body) and depth > 0:
                    ch = body[pos]
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                    pos += 1
                # `param_block` is the content BETWEEN the outer
                # parens (depth was decremented to 0 on the close,
                # so pos is one past it).
                param_block = body[paren_start + 1:pos - 1]
                # Capture `$name` occurrences. We don't constrain on
                # the leading `[Parameter()]` attribute because not
                # all params carry the attribute (positional / simple
                # params are valid too).
                for pm in re.finditer(
                    r"\$([A-Za-z_][\w]*)",
                    param_block,
                ):
                    pname = pm.group(1)
                    if pname not in params:
                        params.append(pname)

        results.append(
            {
                "name": name,
                "scope": scope,
                "kind": kind,
                "start_offset": m.start(),
                "params": params,
            }
        )

    results.sort(key=lambda r: r["start_offset"])
    return results


class CodeGraphAnalyzer:
    """Analyzes codebase and extracts entities into Weaviate code graph."""

    def __init__(self, project_name: str, weaviate_url: str = "http://localhost:8081",
                 grpc_port: int = 50052, named_vectors: bool = DUAL_EMBEDDING_ENABLED):
        self.project_name = project_name
        self.weaviate_url = weaviate_url
        self.grpc_port = grpc_port
        self.named_vectors = named_vectors
        self.client = None

        # Per-project collection names
        self.coll_module = _collection_name("CodeModule", project_name)
        self.coll_class = _collection_name("CodeClass", project_name)
        self.coll_function = _collection_name("CodeFunction", project_name)
        self.coll_api = _collection_name("CodeAPI", project_name)
        self.coll_interaction = _collection_name("CodeInteraction", project_name)

        # Collections
        self.modules_collection = None
        self.classes_collection = None
        self.functions_collection = None
        self.apis_collection = None

        # Cache for entity lookups
        self.module_cache: Dict[str, str] = {}  # path -> UUID
        self.class_cache: Dict[str, str] = {}  # full_name -> UUID
        self.function_cache: Dict[str, str] = {}  # full_name -> UUID
        self.module_imports: Dict[str, List[str]] = {}  # path -> [import names]

        # v0.2.16 — visited-UUID tracking for --prune-stale (bug 1.4 / addendum H).
        # Populated only when self._track_visited is True (set by analyze_repository
        # when called with prune_stale=True). Each entry is
        # (collection_name, uuid) so we can prune per-collection without crossing
        # the collection boundary. Empty by default — does NOT affect normal runs.
        self.visited_uuids: Set[Tuple[str, str]] = set()
        self._track_visited: bool = False

        # v0.2.18 (Plan C) — canonical language ID for the language currently
        # being analyzed. Set by the dispatcher in `analyze_repository` from the
        # `lang_dispatch` table entry, then read by `_create_or_update_module`,
        # the per-method insert paths, and `_store_interactions` so every row
        # carries a canonical-lowercase `language` property. Empty string when
        # no analyze is in progress (creates use `_canonical_lang_id()` of the
        # display label as the fallback for direct-call sites).
        self._current_language: str = ""

        # v0.2.18 (Plan C) — per-file progress emitter for the Re-analyze
        # Tauri command. main() installs a JSON-line-emitter when invoked
        # with `--json-progress`; otherwise this stays None and analyze
        # proceeds with the existing human-readable prints. The signature
        # is `(fraction: float, message: str, file: str, lang: str) -> None`.
        self._progress_emitter: Optional[Any] = None
        # Scoping for the prune pass. Set by analyze_repository from its
        # `language=` parameter (see _prune_stale_objects). Empty string =
        # legacy/global prune across every project row.
        self._prune_language: str = ""
        # v0.2.47 (extras) — POSIX absolute path of the source root that
        # owns the file currently being analyzed. The dispatcher sets it
        # before each per-source-root pass; insert sites read it to stamp
        # `project_source` on every emitted entity so the "Reindex" UI and
        # debugging tools can tell primary-repo rows from extras-path rows.
        # Empty string when no analyze is in progress.
        self._current_source: str = ""

    def connect(self):
        """Connect to Weaviate."""
        try:
            self.client = weaviate.connect_to_custom(
                http_host='localhost',
                http_port=8081,
                http_secure=False,
                grpc_host='localhost',
                grpc_port=50052,
                grpc_secure=False
            )
            print(f"✅ Connected to Weaviate at {self.weaviate_url}")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to Weaviate: {e}", file=sys.stderr)
            return False

    def _vectorizer_config(self):
        """Return appropriate vectorizer config based on named_vectors flag.

        v0.2.18: slot set is the active code slot (from EmbeddingService)
        UNION the legacy/forward-compat slots so post-create switches
        between models don't require recreating the collection.

        Pre-v0.2.18 hardcoded three slots based on `_ACTIVE_CODE_VECTOR`
        env-time resolution; that broke OpenAI-as-code and arctic-text-
        as-code-fallback installs. The Wave-A schema helper
        (vco_lib/weaviate_schema.py) carries the full slot map.
        """
        if not self.named_vectors:
            return Configure.Vectorizer.none()
        # Active slot (whichever backend EmbeddingService picked) + the
        # legacy / forward-compat slots so we can later switch models
        # via `migrate-collections` without dropping data.
        active_slot = _active_code_vector_slot()
        slot_names = {
            active_slot,
            "ollama_code_embed",   # legacy jina-v2-base-code
            "codesage_embed",      # CodeSage primary (preserved through model switches)
            "openai_code_embed",   # OpenAI text-embedding-3 reused for code
        }
        return [Configure.NamedVectors.none(name=n) for n in sorted(slot_names)]

    def _inverted_index_config(self):
        """Always return an inverted index config with `index_null_state=True`.

        Required for `is_none(True)` filters (e.g. future stale-filter on
        valid_until). Cannot be retro-added on existing collections —
        Weaviate ≤1.30 ignores `Reconfigure.inverted_index(...)` for
        index_null_state, so this MUST be set at create time. Audit
        finding Code-M2 (2026-04-30).
        """
        return Configure.inverted_index(index_null_state=True)

    def _create_class_with_retry(self, name: str, **create_kwargs) -> bool:
        """Create a Weaviate class with a bounded retry budget + fail-fast
        on case-insensitive name collisions (bug 0.6, 2026-05-17).

        Behaviour:
          - Up to 3 attempts with exponential backoff (0.5s, 1s, 2s) for
            transient errors (network blips, Weaviate restart, etc.).
          - If the error message contains the Weaviate case-insensitive
            collision marker ``"class already exists: found similar
            class"`` — fail FAST (no retry). Print an actionable error
            to stderr and re-raise. The collision is permanent: Weaviate
            will reject every subsequent attempt the same way, so
            retrying just wastes time and confuses the operator with
            a multi-minute wedge.
          - Other errors are retried up to the budget. If all attempts
            fail, the last exception propagates — caller wraps and logs.

        Returns:
          - True if creation succeeded on this call.
          - False if the class already exists with the EXACT same name
            (Weaviate returns "class already exists" without the
            "found similar class" suffix — that's the harmless "we
            already have it under the right name" case, not the
            case-collision case).

        Raises:
          - The original exception, on case-collision or budget
            exhaustion. The exception message is preserved verbatim so
            log scrapers can match on it.

        Why not let Weaviate's own client retry? The v4 Python client
        has no opinion about case-collisions — it surfaces the server's
        rejection as a plain WeaviateUnexpectedStatusCodeError. The
        wedge observed 2026-05-17 (26 minutes, 0.5% CPU, sleeping on
        `poll_schedule_timeout`) came from the script's calling code,
        not the client itself, but the lack of fail-fast meant the
        operator had to ^C to escape. This helper makes the bad case
        terminate in <5s with a clear error.
        """
        import time as _time

        # Backoff schedule for transient errors. The fixed 3-attempt
        # cap deliberately stays small so a permanent error surfaces
        # within ~3.5s of wall-clock total — fast enough that a
        # launcher IPC caller can show a failure toast before the user
        # gets impatient and refreshes.
        backoff_schedule = [0.5, 1.0, 2.0]
        last_exc: Optional[Exception] = None

        for attempt_idx, sleep_after in enumerate(backoff_schedule):
            try:
                self.client.collections.create(name=name, **create_kwargs)
                return True
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                msg_lower = msg.lower()

                # Case-insensitive collision marker. Weaviate's error
                # text on this is stable as of 1.24+ (verified against
                # Weaviate source, schema/handler.go). The exact phrase:
                #   "invalid object: class already exists: found similar
                #    class \"X\""
                # If Weaviate ever rewords this, we'll still catch the
                # "class already exists" prefix below and retry — which
                # is wrong for collisions, but no worse than the
                # pre-fix forever-loop behaviour.
                if "found similar class" in msg_lower:
                    # Try to extract the colliding class name from the
                    # error message for the operator's benefit.
                    collider = "<unknown>"
                    # Pattern: ...found similar class "X"...
                    import re as _re
                    m = _re.search(r'found similar class\s+["\']([^"\']+)["\']', msg)
                    if m:
                        collider = m.group(1)
                    print(
                        f"ERROR: Weaviate class '{name}' case-insensitively "
                        f"collides with existing class '{collider}'.\n"
                        f"This usually means a previous VCO version wrote "
                        f"codegraph data under a differently-cased prefix.\n"
                        f"To resolve:\n"
                        f"  1. Open the launcher and run the 'Legacy code-graph "
                        f"collections' wizard.\n"
                        f"  2. Or manually delete the colliding class: "
                        f"curl -X DELETE http://localhost:8081/v1/schema/{collider}",
                        file=sys.stderr,
                    )
                    # Re-raise so the caller (which wraps in try/except)
                    # records the failure and exits non-zero rather
                    # than silently continuing past the broken class.
                    raise

                # "already exists" WITHOUT the "found similar class"
                # suffix → the class is there under the EXACT requested
                # name. That's the harmless idempotent-create case; the
                # caller doesn't need to retry.
                if "already exists" in msg_lower and "found similar" not in msg_lower:
                    return False

                # Transient error: sleep and retry, unless this was
                # the final attempt.
                if attempt_idx < len(backoff_schedule) - 1:
                    _time.sleep(sleep_after)
                    continue
                # Fall through to raise after the loop.

        # All retries exhausted — propagate the last error.
        assert last_exc is not None, "loop must have set last_exc"
        raise last_exc

    def create_collections(self, force: bool = False):
        """Create Weaviate collections for code graph (per-project names)."""

        if not self.client:
            raise RuntimeError("Not connected to Weaviate")

        collections_created = []

        # CodeModule collection
        try:
            if force and self.client.collections.exists(self.coll_module):
                self.client.collections.delete(self.coll_module)
                print(f"🗑️  Deleted existing {self.coll_module} collection")

            if not self.client.collections.exists(self.coll_module):
                created = self._create_class_with_retry(
                    name=self.coll_module,
                    description="Code modules/files with imports and metrics",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="path", data_type=DataType.TEXT, description="File path relative to repo root", skip_vectorization=True),
                        Property(name="language", data_type=DataType.TEXT, description="Programming language", skip_vectorization=True),
                        Property(name="module_summary", data_type=DataType.TEXT, description="Summary of module purpose and contents (for embedding)"),
                        Property(name="loc", data_type=DataType.INT, description="Lines of code", skip_vectorization=True),
                        Property(name="complexity", data_type=DataType.NUMBER, description="Cyclomatic complexity", skip_vectorization=True),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        Property(name="last_modified", data_type=DataType.DATE, description="Last modification time", skip_vectorization=True),
                        Property(name="file_hash", data_type=DataType.TEXT, description="SHA256 hash of file content", skip_vectorization=True),
                        Property(name="import_names", data_type=DataType.TEXT_ARRAY, description="List of imported module/package names", skip_vectorization=True),
                        # v0.2.47 (extras): absolute POSIX path of the source
                        # root that contributed this row. Primary repo for
                        # native code; a `--extra-path` value for read-only
                        # references indexed into this project's collections.
                        # Empty string for pre-v0.2.47 rows (graceful fallback
                        # — Weaviate treats absent properties as null).
                        Property(name="project_source", data_type=DataType.TEXT, description="Absolute path of the source root that produced this row (primary repo OR extra-path)", skip_vectorization=True),
                        # v0.2.61 (Track E): stable SHA-256 of the object's
                        # semantically-meaningful content. Powers the per-object
                        # tombstone-skip in _dedup_insert — a byte-identical
                        # re-index reads this back, matches, and skips the
                        # replace() (no HNSW vector tombstone). Empty/absent on
                        # pre-v0.2.61 rows → treated as unknown → one-time
                        # re-write, then stable.
                        Property(name="content_hash", data_type=DataType.TEXT, description="SHA-256 of semantically-meaningful content (per-object tombstone-skip)", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="imports", target_collection=self.coll_module, description="Imported modules"),
                    ]
                )
                if created:
                    collections_created.append(self.coll_module)
                    print(f"✅ Created {self.coll_module} collection")
        except Exception as e:
            # Case-collision was already announced to stderr by the
            # helper. Re-raise to abort the whole create_collections
            # call — continuing would write inconsistent schemas.
            if "found similar class" in str(e).lower():
                raise
            print(f"⚠️  CodeModule: {e}")

        # CodeClass collection
        try:
            if force and self.client.collections.exists(self.coll_class):
                self.client.collections.delete(self.coll_class)
                print(f"🗑️  Deleted existing {self.coll_class} collection")

            if not self.client.collections.exists(self.coll_class):
                created = self._create_class_with_retry(
                    name=self.coll_class,
                    description="Classes with inheritance and methods",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="name", data_type=DataType.TEXT, description="Class name", skip_vectorization=True),
                        Property(name="full_name", data_type=DataType.TEXT, description="Fully qualified name", skip_vectorization=True),
                        Property(name="class_body", data_type=DataType.TEXT, description="Full class source code (for embedding)"),
                        Property(name="methods", data_type=DataType.TEXT_ARRAY, description="Method names", skip_vectorization=True),
                        Property(name="signature", data_type=DataType.TEXT, description="Class signature only", skip_vectorization=True),
                        Property(name="doc", data_type=DataType.TEXT, description="Docstring"),
                        Property(name="start_line", data_type=DataType.INT, description="Start line number", skip_vectorization=True),
                        Property(name="end_line", data_type=DataType.INT, description="End line number", skip_vectorization=True),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        # v0.2.18 (Plan C): canonical-lowercase language ID so a
                        # later `--prune-stale --language=<lang>` run can scope
                        # the per-row delete to only that language's entries.
                        Property(name="language", data_type=DataType.TEXT, description="Canonical language ID (python, javascript, ...) — Plan C scoped prune", skip_vectorization=True),
                        Property(name="field_types", data_type=DataType.TEXT_ARRAY, description="field_name:TypeName pairs from annotated fields", skip_vectorization=True),
                        Property(name="composes", data_type=DataType.TEXT_ARRAY, description="Class names used as field types (composition)", skip_vectorization=True),
                        Property(name="primary_layer", data_type=DataType.TEXT, description="Primary architectural layer (API, Service, Data, UI, Utility, etc.)", skip_vectorization=True),
                        Property(name="secondary_layers", data_type=DataType.TEXT_ARRAY, description="Secondary architectural layers if class spans multiple", skip_vectorization=True),
                        # v0.2.47 (extras): source-root provenance (see CodeModule).
                        Property(name="project_source", data_type=DataType.TEXT, description="Absolute path of the source root that produced this row (primary repo OR extra-path)", skip_vectorization=True),
                        # v0.2.52 (V52-O.4): repo-relative POSIX path of the
                        # source file the class was extracted from. Mirrors the
                        # `path` property on CodeModule so consumers can filter
                        # / scope by file without joining through the `module`
                        # reference. Empty for pre-V52-O.4 rows (graceful null).
                        Property(name="file_path", data_type=DataType.TEXT, description="Repo-relative POSIX path of the source file (mirrors CodeModule.path)", skip_vectorization=True),
                        # v0.2.61 (Track E): per-object content hash (see CodeModule).
                        Property(name="content_hash", data_type=DataType.TEXT, description="SHA-256 of semantically-meaningful content (per-object tombstone-skip)", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="module", target_collection=self.coll_module, description="Parent module"),
                        ReferenceProperty(name="extends", target_collection=self.coll_class, description="Base classes"),
                    ]
                )
                if created:
                    collections_created.append(self.coll_class)
                    print(f"✅ Created {self.coll_class} collection")
        except Exception as e:
            if "found similar class" in str(e).lower():
                raise
            print(f"⚠️  CodeClass: {e}")

        # CodeFunction collection
        try:
            if force and self.client.collections.exists(self.coll_function):
                self.client.collections.delete(self.coll_function)
                print(f"🗑️  Deleted existing {self.coll_function} collection")

            if not self.client.collections.exists(self.coll_function):
                created = self._create_class_with_retry(
                    name=self.coll_function,
                    description="Functions with call graphs",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="name", data_type=DataType.TEXT, description="Function name", skip_vectorization=True),
                        Property(name="full_name", data_type=DataType.TEXT, description="Fully qualified name", skip_vectorization=True),
                        Property(name="function_body", data_type=DataType.TEXT, description="Full function source code (for embedding)"),
                        Property(name="signature", data_type=DataType.TEXT, description="Function signature only", skip_vectorization=True),
                        Property(name="doc", data_type=DataType.TEXT, description="Docstring"),
                        Property(name="start_line", data_type=DataType.INT, description="Start line number", skip_vectorization=True),
                        Property(name="end_line", data_type=DataType.INT, description="End line number", skip_vectorization=True),
                        Property(name="is_async", data_type=DataType.BOOL, description="Is async function", skip_vectorization=True),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        # v0.2.18 (Plan C): canonical-lowercase language ID for scoped prune.
                        Property(name="language", data_type=DataType.TEXT, description="Canonical language ID (python, javascript, ...) — Plan C scoped prune", skip_vectorization=True),
                        Property(name="type_uses", data_type=DataType.TEXT_ARRAY, description="Type names referenced in function annotations", skip_vectorization=True),
                        Property(name="cfg_summary", data_type=DataType.TEXT, description="CFG summary: branches/loops/max_depth counts (from Joern)", skip_vectorization=True),
                        Property(name="data_flow_vars", data_type=DataType.TEXT_ARRAY, description="Variable names that flow through the function (from Joern PDG)", skip_vectorization=True),
                        Property(name="layer", data_type=DataType.TEXT, description="Architectural layer (API, Service, Data, UI, Utility, etc.)", skip_vectorization=True),
                        Property(name="call_names", data_type=DataType.TEXT_ARRAY, description="Names of called functions (for callers queries)", skip_vectorization=True),
                        # v0.2.47 (extras): source-root provenance (see CodeModule).
                        Property(name="project_source", data_type=DataType.TEXT, description="Absolute path of the source root that produced this row (primary repo OR extra-path)", skip_vectorization=True),
                        # v0.2.52 (V52-O.4): repo-relative POSIX path of the
                        # source file the function was extracted from. Mirrors
                        # the `path` property on CodeModule so consumers can
                        # filter / scope by file without joining through the
                        # `module` reference. Empty for pre-V52-O.4 rows.
                        Property(name="file_path", data_type=DataType.TEXT, description="Repo-relative POSIX path of the source file (mirrors CodeModule.path)", skip_vectorization=True),
                        # v0.2.61 (Track E): per-object content hash (see CodeModule).
                        Property(name="content_hash", data_type=DataType.TEXT, description="SHA-256 of semantically-meaningful content (per-object tombstone-skip)", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="module", target_collection=self.coll_module, description="Parent module"),
                        ReferenceProperty(name="calls", target_collection=self.coll_function, description="Called functions"),
                    ]
                )
                if created:
                    collections_created.append(self.coll_function)
                    print(f"✅ Created {self.coll_function} collection")
        except Exception as e:
            if "found similar class" in str(e).lower():
                raise
            print(f"⚠️  CodeFunction: {e}")

        # CodeAPI collection
        try:
            if force and self.client.collections.exists(self.coll_api):
                self.client.collections.delete(self.coll_api)
                print(f"🗑️  Deleted existing {self.coll_api} collection")

            if not self.client.collections.exists(self.coll_api):
                created = self._create_class_with_retry(
                    name=self.coll_api,
                    description="API endpoints with handlers",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="endpoint", data_type=DataType.TEXT, description="API endpoint path", skip_vectorization=True),
                        Property(name="method", data_type=DataType.TEXT, description="HTTP method", skip_vectorization=True),
                        Property(name="api_description", data_type=DataType.TEXT, description="Description of API endpoint and its purpose (for embedding)"),
                        Property(name="parameters", data_type=DataType.TEXT_ARRAY, description="Parameter names", skip_vectorization=True),
                        Property(name="returns", data_type=DataType.TEXT, description="Return type/description"),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        # v0.2.18 (Plan C): canonical-lowercase language ID for scoped prune.
                        Property(name="language", data_type=DataType.TEXT, description="Canonical language ID (python, javascript, ...) — Plan C scoped prune", skip_vectorization=True),
                        Property(name="proxy_target", data_type=DataType.TEXT, description="Target endpoint for proxy/forwarding routes (cross-language linking)", skip_vectorization=True),
                        # v0.2.47 (extras): source-root provenance (see CodeModule).
                        Property(name="project_source", data_type=DataType.TEXT, description="Absolute path of the source root that produced this row (primary repo OR extra-path)", skip_vectorization=True),
                        # v0.2.61 (Track E): per-object content hash (see CodeModule).
                        Property(name="content_hash", data_type=DataType.TEXT, description="SHA-256 of semantically-meaningful content (per-object tombstone-skip)", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="handler", target_collection=self.coll_function, description="Handler function"),
                    ]
                )
                if created:
                    collections_created.append(self.coll_api)
                    print(f"✅ Created {self.coll_api} collection")
        except Exception as e:
            if "found similar class" in str(e).lower():
                raise
            print(f"⚠️  CodeAPI: {e}")

        # CodeInteraction collection — cross-language / cross-service calls
        try:
            if force and self.client.collections.exists(self.coll_interaction):
                self.client.collections.delete(self.coll_interaction)
                print(f"🗑️  Deleted existing {self.coll_interaction} collection")

            if not self.client.collections.exists(self.coll_interaction):
                created = self._create_class_with_retry(
                    name=self.coll_interaction,
                    description="Cross-language and cross-service communication calls",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="source_project", data_type=DataType.TEXT, description="Project that initiates the call", skip_vectorization=True),
                        Property(name="interaction_type", data_type=DataType.TEXT, description="http | grpc | mq | websocket", skip_vectorization=True),
                        Property(name="direction", data_type=DataType.TEXT, description="outbound | inbound | pubsub", skip_vectorization=True),
                        Property(name="protocol", data_type=DataType.TEXT, description="GET/POST/... | kafka | rabbitmq | grpc | ws", skip_vectorization=True),
                        Property(name="endpoint", data_type=DataType.TEXT, description="Extracted target: /path, grpc:Service.Method, topic:name", skip_vectorization=True),
                        Property(name="raw_target", data_type=DataType.TEXT, description="Full literal string as seen in source code", skip_vectorization=True),
                        Property(name="confidence", data_type=DataType.TEXT, description="high | medium", skip_vectorization=True),
                        # v0.2.18 (Plan C): SOURCE-SIDE language. A Python file calling a
                        # Go gRPC endpoint creates an interaction row with language="python"
                        # (the caller's language). The target language might be different,
                        # but a re-analysis of Python source re-extracts all Python→X
                        # interactions, so language-scoped prune by source-language is
                        # the correct + safe primitive.
                        Property(name="language", data_type=DataType.TEXT, description="Source-side canonical language ID (caller's language) — Plan C scoped prune", skip_vectorization=True),
                        Property(name="description", data_type=DataType.TEXT, description="Human-readable summary for embedding (Python→HTTP POST /api/users via requests)"),
                        # v0.2.47 (extras): source-root provenance (see CodeModule).
                        Property(name="project_source", data_type=DataType.TEXT, description="Absolute path of the source root that produced this row (primary repo OR extra-path)", skip_vectorization=True),
                        # v0.2.61 (Track E): per-object content hash (see CodeModule).
                        Property(name="content_hash", data_type=DataType.TEXT, description="SHA-256 of semantically-meaningful content (per-object tombstone-skip)", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="source_function", target_collection=self.coll_function, description="Function that makes the call"),
                        ReferenceProperty(name="source_module", target_collection=self.coll_module, description="Module containing the call"),
                    ]
                )
                if created:
                    collections_created.append(self.coll_interaction)
                    print(f"✅ Created {self.coll_interaction} collection")
        except Exception as e:
            if "found similar class" in str(e).lower():
                raise
            print(f"⚠️  CodeInteraction: {e}")

        if collections_created:
            print(f"\n✅ Created {len(collections_created)} collections: {', '.join(collections_created)}")
        else:
            print("\n✅ All collections already exist")

        # Get collection references (per-project names)
        self.modules_collection = self.client.collections.get(self.coll_module)
        self.classes_collection = self.client.collections.get(self.coll_class)
        self.functions_collection = self.client.collections.get(self.coll_function)
        self.apis_collection = self.client.collections.get(self.coll_api)
        self.interactions_collection = self.client.collections.get(self.coll_interaction)

        # Schema migration: ensure import_names property exists on CodeModule
        self._ensure_import_names_property()

        # v0.2.18 (Plan C) schema migration: ensure `language` property exists
        # on CodeClass / CodeFunction / CodeAPI / CodeInteraction. CodeModule
        # already has it (since v0.2.16). The property enables language-scoped
        # pruning so `--prune-stale --language=<lang>` is safe on polyglot
        # repos. Idempotent — does nothing when the prop is already present.
        # Soft-fail per-collection so a single 422 doesn't wedge the whole run.
        self._ensure_language_property()

        # v0.2.47 (extras) schema migration: ensure `project_source` property
        # exists on all 5 code collections so pre-v0.2.47 installs that picked
        # up the new analyzer pick up the property on the next analyze run.
        # Idempotent + soft-fail per collection.

        # v0.2.52 (V52-O.4) schema migration: ensure `file_path` property
        # exists on CodeFunction + CodeClass so pre-V52-O.4 installs pick up
        # the property on the next analyze run and `_dedup_insert` can stamp
        # it. Idempotent + soft-fail per collection.
        self._ensure_file_path_property()

        # v0.2.61 (Track E) schema migration: ensure `content_hash` exists on
        # all 5 code collections so pre-v0.2.61 installs gain the property on
        # the next analyze run and `_dedup_insert` can stamp + read it for the
        # per-object tombstone-skip. Pre-migration rows have NULL content_hash
        # → the skip treats them as "unknown" → one-time re-write that stamps
        # the hash, after which they go stable. Idempotent + soft-fail.
        self._ensure_content_hash_property()

    def _dedup_insert(self, collection, insert_params: dict, identity_key: str,
                      file_path_rel: str = "") -> str:
        """Upsert with a deterministic UUID derived from
        ``(project, file_path_rel, identity_key)``.

        v0.2.16 change (bug 0.1): switched from ``collection.data.insert()``
        to ``collection.data.replace()``. The previous ``insert()`` call
        returned a 422 ``id 'X' already exists`` from Weaviate the second
        time we tried to re-index a previously-seen entity, which the
        outer ``analyze_*_file`` try/except then swallowed as a
        ``files_skipped += 1`` print — exiting 0 even when most objects
        never landed. ``replace()`` is idempotent (upsert semantics) and
        eliminates the failure mode entirely.

        v0.2.16 change (bug 0.7): UUID identity-key now also includes
        the repo-relative file path. Two genuinely-different files
        that happen to define the same module-stem+symbol no longer
        collide. Callers MUST pass ``file_path_rel`` as a POSIX-style
        string (``Path(...).relative_to(repo_root).as_posix()``) — the
        analyzer's per-language methods construct this once per file
        and thread it through every ``_dedup_insert`` call site.

        v0.2.16 change (1.4 / addendum H): visited UUIDs are tracked in
        ``self.visited_uuids`` so a later ``--prune-stale`` pass can
        delete entries the analyzer didn't visit this run (stale code
        that was deleted from the project since the previous analyze).

        Args:
            collection: Weaviate collection reference.
            insert_params: dict with 'properties', 'vector', 'references' keys.
            identity_key: unique key for this entity (e.g. full_name, path).
            file_path_rel: repo-relative POSIX path of the source file the
                entity was extracted from. Default ``""`` is allowed only
                for call sites where the file is genuinely not in scope
                (none today; kept for forward-compat).

        Returns:
            UUID string of the inserted/replaced object. Note that
            ``collection.data.replace()`` in weaviate-client v4 returns
            ``None``, so we synthesize and return ``det_uuid`` ourselves
            — every call site that captures the return value (``func_uuid``,
            ``class_uuid``, ``module_uuid`` etc.) continues to work.
        """
        # v0.2.52 (V52-O.3): mix `_current_source` into the UUID seed so the
        # SAME relative path under TWO different source roots (primary repo
        # vs. an --extra-path value) produces TWO distinct UUIDs and both rows
        # coexist instead of clobbering each other on `replace()`. Empty
        # `_current_source` falls back to the v0.2.16 seed shape (no upgrade
        # migration needed for primary-repo-only installs).
        current_source_for_uuid = getattr(self, "_current_source", "")
        det_uuid = _deterministic_uuid(
            self.project_name, file_path_rel, identity_key,
            project_source=current_source_for_uuid,
        )

        # v0.2.18 (Plan C): stamp the canonical language ID on every insert
        # so the language-scoped prune filter can match each row. The
        # dispatcher sets `self._current_language` for the duration of one
        # language's analyze loop; insert sites need not pass it explicitly.
        # Skips when `language` is already present (caller pre-set it, or
        # the prop doesn't belong on this collection — defensive). Empty
        # `_current_language` is also a no-op so unit tests that bypass the
        # dispatcher don't get spurious empty-string writes that would
        # confuse the prune filter's "unknown language" branch.
        #
        # `getattr` with default keeps the path safe for older test
        # fixtures that construct an analyzer without going through
        # __init__ — they may not have `_current_language` set.
        current_lang = getattr(self, "_current_language", "")
        if current_lang:
            props = insert_params.get("properties")
            if isinstance(props, dict) and not props.get("language"):
                props["language"] = current_lang

        # v0.2.47 (extras): stamp the absolute source-root path so consumers
        # can tell primary-repo rows from extras-path rows. Same defensive
        # pattern as `_current_language` above — empty value is a no-op, and
        # we don't clobber when the caller pre-set the property.
        current_source = getattr(self, "_current_source", "")
        if current_source:
            props = insert_params.get("properties")
            if isinstance(props, dict) and not props.get("project_source"):
                props["project_source"] = current_source

        # v0.2.52 (V52-O.4): stamp `file_path` on CodeFunction + CodeClass
        # rows from the per-call `file_path_rel` argument so consumers can
        # filter / scope by source file without joining through the `module`
        # reference. Scope check via the collection name: only Function /
        # Class get the property (CodeModule already has `path`; CodeAPI /
        # CodeInteraction aren't file-anchored in the same way). Defensive:
        # don't clobber if the caller pre-set the property; skip on empty
        # `file_path_rel` so the property stays NULL rather than empty
        # string for forward-compat / cross-reference paths.
        if file_path_rel:
            coll_name = getattr(collection, "name", "") or ""
            if coll_name.endswith("CodeFunction") or coll_name.endswith("CodeClass"):
                props = insert_params.get("properties")
                if isinstance(props, dict) and not props.get("file_path"):
                    props["file_path"] = file_path_rel

        # ── v0.2.61 (Track E): per-object content-hash tombstone skip ───────
        #
        # WHY: the per-FILE skip (`_get_existing_module` keyed on path +
        # file_hash, checked at the top of every analyze_*_file path) already
        # short-circuits a byte-identical FILE before any write. But when a
        # file changes by even one line, EVERY object it contains (50 funcs +
        # N classes) is re-`replace()`'d here — and `replace()` of a
        # vector-bearing object TOMBSTONES the old HNSW vector node + inserts a
        # new one, even when THIS object's body is byte-identical to what's
        # indexed. A 1-function edit therefore generated ~50 needless
        # tombstones; over time those accumulate into the cleanup-spin disk
        # leak this fix targets (e.g. SD15_CodeFunction reached 8943).
        #
        # FIX: stamp a stable `content_hash` on the object, then before
        # replacing, point-read the existing object's stored `content_hash`
        # by its deterministic UUID and SKIP the replace when it matches
        # (unchanged object → 0 tombstones, 0 write). Layered UNDER the
        # per-file fast path: this only ever runs for objects in files that
        # DID change, so it has zero cost on the unchanged-file case.
        #
        # CORRECTNESS / FAIL-SAFE: never skip a genuinely-changed object. We
        # skip ONLY when the read SUCCEEDS and the stored hash EQUALS the
        # computed hash. Every other branch — object absent, property missing
        # (pre-migration row), read error, no `.query` attr (test/mocked
        # collection), empty stored hash — FALLS THROUGH to the normal
        # replace()/insert(). A read failure must never cause a stale index.
        props = insert_params.get("properties")
        skip_replace = False
        if isinstance(props, dict):
            try:
                content_hash = _content_hash_for_object(
                    getattr(collection, "name", "") or "", props
                )
            except Exception:  # noqa: BLE001 — hashing must never wedge a write
                content_hash = ""
            if content_hash:
                # Persist for the NEXT run's comparison (and don't clobber a
                # caller-preset value, mirroring the other stamp sites).
                if not props.get("content_hash"):
                    props["content_hash"] = content_hash
                # Cheap point-read of the existing stored hash. Any failure is
                # a fall-through to write (fail-safe), NOT a skip.
                try:
                    query = getattr(collection, "query", None)
                    fetch_by_id = getattr(query, "fetch_object_by_id", None) if query else None
                    if callable(fetch_by_id):
                        existing = fetch_by_id(
                            det_uuid, return_properties=["content_hash"]
                        )
                        # fetch_object_by_id returns None when absent.
                        if existing is not None:
                            existing_props = getattr(existing, "properties", None) or {}
                            stored_hash = existing_props.get("content_hash") or ""
                            if stored_hash and stored_hash == content_hash:
                                skip_replace = True
                except Exception:  # noqa: BLE001
                    # Read error / unsupported client / mocked collection →
                    # fall through to the unconditional write. Never skip on
                    # uncertainty.
                    skip_replace = False

        if skip_replace:
            # Unchanged object: no replace(), no tombstone, no write. Still
            # record the UUID as visited so a concurrent `--prune-stale` pass
            # does NOT delete this live row just because we skipped its write.
            if self._track_visited:
                self.visited_uuids.add((collection.name, det_uuid))
            return det_uuid

        # v0.2.16 docstring (above) claimed ``replace()`` is upsert.
        # weaviate-client v4.21 actually requires the object to PRE-EXIST
        # — when called against a UUID that has never been written
        # (e.g. brand-new collections after a rename / first analysis /
        # post-cleanup), the call returns 500 "no object with id X"
        # and `_DedupInsertError` propagates up to mark the file as a
        # skip. Symptom: a full re-analyze against an empty collection
        # writes 0 objects with `insert_errors == files_analyzed`.
        #
        # v0.2.23 fix: try `replace()` first (canonical upsert path
        # when the object exists, e.g. incremental re-write of an
        # already-indexed file). On the "no object with id X" branch,
        # transparently fall through to `insert()`. Any other error
        # bubbles up as before. This makes the analyzer correct for
        # the cold-start / post-rename / post-cleanup repopulate path
        # WITHOUT changing the behaviour for genuine incremental
        # re-writes.
        try:
            collection.data.replace(uuid=det_uuid, **insert_params)
        except BaseException as exc:
            # Detect Weaviate's "object does not exist" signal. The error
            # text is stable across v4.x ("no object with id 'X'"); we
            # match conservatively on substring so a future error-prefix
            # change (e.g. "no object with uuid") still trips the branch.
            err_text = str(exc)
            is_not_found = "no object with id" in err_text or "no object with uuid" in err_text
            if is_not_found:
                try:
                    collection.data.insert(uuid=det_uuid, **insert_params)
                except BaseException as insert_exc:
                    raise _DedupInsertError(
                        insert_exc, collection.name, det_uuid
                    ) from insert_exc
            else:
                # Wrap into a distinctive exception type so the outer
                # per-file try/except can attribute the failure to a
                # write-to-Weaviate problem (vs. a parse / read / regex
                # issue elsewhere in the analyze_*_file path). bug 0.2.
                raise _DedupInsertError(exc, collection.name, det_uuid) from exc
        # Track for --prune-stale (only populated when caller opted in;
        # see main()'s argparse + analyze_repository's prune logic).
        if self._track_visited:
            self.visited_uuids.add((collection.name, det_uuid))
        return det_uuid

    def _ensure_import_names_property(self):
        """Add import_names property to CodeModule if missing (schema migration)."""
        try:
            config = self.modules_collection.config.get()
            existing_props = {p.name for p in config.properties}
            if "import_names" not in existing_props:
                self.modules_collection.config.add_property(
                    Property(name="import_names", data_type=DataType.TEXT_ARRAY,
                             description="List of imported module/package names",
                             skip_vectorization=True)
                )
                print("   Added import_names property to CodeModule schema")
        except Exception as e:
            logger.debug(f"Schema migration check failed: {e}")

    def _ensure_language_property(self):
        """Add `language` property to the 4 code collections that lack it
        on pre-v0.2.18 installs (CodeClass, CodeFunction, CodeAPI,
        CodeInteraction). CodeModule already has the property since v0.2.16.

        Plan C / v0.2.18 schema migration. The property enables language-
        scoped pruning so `--prune-stale --language=<lang>` deletes only
        rows tagged with that canonical language ID. Without this property,
        pre-Plan-C rows are invisible to the filter — they survive a
        language-scoped prune (conservative) but the next full re-analyze
        will rewrite them with the new field populated.

        Idempotent: a property that already exists is skipped silently.
        Soft-fail per collection: an HTTP error on one shouldn't wedge the
        others. The `_ensure_import_names_property` pattern is the template.
        """
        collections = [
            ("CodeClass",      self.classes_collection,
             "Canonical language ID (python, javascript, ...) — Plan C scoped prune"),
            ("CodeFunction",   self.functions_collection,
             "Canonical language ID (python, javascript, ...) — Plan C scoped prune"),
            ("CodeAPI",        self.apis_collection,
             "Canonical language ID (python, javascript, ...) — Plan C scoped prune"),
            ("CodeInteraction", self.interactions_collection,
             "Source-side canonical language ID (caller's language) — Plan C scoped prune"),
        ]
        for label, coll, desc in collections:
            if coll is None:
                continue
            try:
                config = coll.config.get()
                existing_props = {p.name for p in config.properties}
                if "language" in existing_props:
                    continue
                coll.config.add_property(
                    Property(
                        name="language",
                        data_type=DataType.TEXT,
                        description=desc,
                        skip_vectorization=True,
                    )
                )
                print(f"   Added language property to {label} schema (Plan C)")
            except Exception as e:
                # Soft-fail. A 422 here doesn't break analysis — language-
                # scoped prune just won't recognise rows in this collection
                # until the next successful migration pass.
                logger.debug(
                    f"Plan C language-property migration on {label} skipped: {e}"
                )


    def _ensure_file_path_property(self):
        """v0.2.52 (V52-O.4) schema migration: add `file_path` to CodeFunction
        + CodeClass so consumers can filter / scope by source file without
        joining through the `module` reference. The property mirrors
        ``CodeModule.path``. Pre-V52-O.4 rows show NULL until they're touched
        by a re-analyze. Idempotent + soft-fail per collection.

        Only Function + Class get the new property; Module already has `path`,
        and API / Interaction rows are not file-anchored in the same way
        (an interaction row can cross multiple files).
        """
        collections = [
            ("CodeFunction", self.functions_collection),
            ("CodeClass",    self.classes_collection),
        ]
        desc = (
            "Repo-relative POSIX path of the source file "
            "(mirrors CodeModule.path)"
        )
        for label, coll in collections:
            if coll is None:
                continue
            try:
                config = coll.config.get()
                existing_props = {p.name for p in config.properties}
                if "file_path" in existing_props:
                    continue
                coll.config.add_property(
                    Property(
                        name="file_path",
                        data_type=DataType.TEXT,
                        description=desc,
                        skip_vectorization=True,
                    )
                )
                print(f"   Added file_path property to {label} schema (v0.2.52)")
            except Exception as e:
                logger.debug(
                    f"v0.2.52 file_path migration on {label} skipped: {e}"
                )

    def _ensure_content_hash_property(self):
        """v0.2.61 (Track E) schema migration: add `content_hash` (TEXT) to all
        5 code collections that lack it on pre-v0.2.61 installs.

        The property carries a stable SHA-256 of each object's semantically-
        meaningful content, written by `_dedup_insert` and read back on the
        next run to SKIP a `replace()` of a byte-identical object (avoiding an
        HNSW vector tombstone). Without the property, pre-v0.2.61 rows return
        NULL → the skip's `stored_hash` is empty → it falls through to a normal
        replace() (fail-safe), which stamps the hash. So the first re-analyze
        after upgrade does one final full re-write (one tombstone per object,
        as today); every subsequent incremental run skips the unchanged objects
        in changed files → near-zero tombstones.

        Back-compat: adding a property to an existing Weaviate class is a
        non-destructive metadata operation (no re-index, no data loss). Rows
        written before the add simply have the property unset (treated as
        NULL). Idempotent — an already-present property is skipped silently.
        Soft-fail per collection — a 422 on one must not wedge the others.
        Mirrors `_ensure_file_path_property` exactly.
        """
        collections = [
            ("CodeModule",      self.modules_collection),
            ("CodeClass",       self.classes_collection),
            ("CodeFunction",    self.functions_collection),
            ("CodeAPI",         self.apis_collection),
            ("CodeInteraction", self.interactions_collection),
        ]
        desc = (
            "SHA-256 of semantically-meaningful content "
            "(per-object tombstone-skip)"
        )
        for label, coll in collections:
            if coll is None:
                continue
            try:
                config = coll.config.get()
                existing_props = {p.name for p in config.properties}
                if "content_hash" in existing_props:
                    continue
                coll.config.add_property(
                    Property(
                        name="content_hash",
                        data_type=DataType.TEXT,
                        description=desc,
                        skip_vectorization=True,
                    )
                )
                print(f"   Added content_hash property to {label} schema (v0.2.61)")
            except Exception as e:
                # Soft-fail. A 422 here doesn't break analysis — the per-object
                # skip just falls through to replace() (its fail-safe default)
                # for this collection until the next successful migration.
                logger.debug(
                    f"v0.2.61 content_hash migration on {label} skipped: {e}"
                )

    def analyze_repository(self, repo_path: Path, language: Optional[str] = None,
                          incremental: bool = False,
                          extract_cfg: bool = False,
                          extract_pdg: bool = False,
                          prune_stale: bool = False,
                          extra_paths: Optional[List[Path]] = None,
                          since_commit: Optional[str] = None,
                          only_file: Optional[Path] = None,
                          canonical_source: Optional[str] = None) -> Dict[str, Any]:
        """Analyze repository and extract code entities.

        Args:
            repo_path: Path to repository
            language: Specific language to analyze (None = all)
            incremental: Only analyze changed files (requires git)
            extract_cfg: Run Joern CFG extraction (requires joern in PATH)
            extract_pdg: Run Joern PDG extraction (requires joern in PATH)
            prune_stale: When True, track every UUID visited this run
                and DELETE any UUIDs in the per-project code-graph
                collections that we DIDN'T visit. Useful for projects
                whose codebase has shrunk since the previous analyze
                (deleted files leave orphan rows otherwise). Default
                False — opt-in via ``--prune-stale``. Bug 1.4 /
                addendum H. Adds ``stats['stale_pruned']``.
            extra_paths: v0.2.47 — additional source roots to walk in
                the same pass as ``repo_path``. Each extra is analyzed
                into the SAME per-project collections (no separate
                prefix), with ``project_source`` stamped to the extra's
                absolute path. ``visited_uuids`` is the UNION across
                the primary repo + all extras so ``--prune-stale`` does
                NOT delete the other roots' UUIDs.
            since_commit: v0.2.47 — when combined with ``incremental``,
                restricts the changed-files filter to
                ``git log <sha>..HEAD`` instead of the default
                ``HEAD~1..HEAD``. Per-source-root: each root that is a
                git repo uses its own diff range; non-git roots fall
                back to full scan with a stderr notice.
            only_file: v0.2.66 (Bug 3) — analyze EXACTLY this one file
                instead of walking ``repo_path``. ``repo_path`` is still
                used as the relativization root (collections key on
                repo-relative POSIX paths), so the file MUST live under
                ``repo_path``. The single file routes through the SAME
                per-file (``_get_existing_module``) and per-object
                (``_dedup_insert`` content-hash) skip paths, so an
                unchanged or trivially-edited file is a near-no-op.
                Mutually exclusive with ``extra_paths``, ``incremental``,
                and ``prune_stale`` — single-file mode never walks other
                roots and never prunes (a prune scoped to one file could
                only ever delete other files' rows). The per-edit
                code-graph hook is the primary caller: it scopes the
                analyze to the file the user just edited, killing the old
                ``HEAD~1..HEAD`` re-churn (dozens of files re-parsed per
                keystroke) AND the correctness bug (the edited file is
                uncommitted, so it was never in the diff range at all).
            canonical_source: v0.2.66 (Bug 3, part b) — override the
                ``project_source`` value (and the absolute-source component
                of every object's deterministic UUID) with this canonical
                root instead of ``source_root.as_posix()``. The per-edit
                hook resolves the edited file's git MAIN repo root (a linked
                worktree shares its main repo's object store) and passes it
                here, so a worktree edit and a main-checkout edit of the
                SAME logical file converge on ONE canonical object rather
                than minting a per-worktree duplicate. WHY this is needed:
                the ``file_path`` (``relative_to(repo_path)``) is already
                identical between a worktree and its main checkout; only the
                absolute source root differs, and that absolute root is what
                ``_dedup_insert`` mixes into the UUID (V52-O.3). Overriding
                it removes the divergence. ``None`` (default) preserves the
                per-source-root behaviour. Has no effect unless ``only_file``
                is set (whole-tree walks keep their per-root provenance).

        Returns:
            Dictionary with analysis statistics. v0.2.16 adds:
              - ``insert_errors``: count of files whose per-file
                exception came specifically from a ``_dedup_insert``
                call (bug 0.2). Distinct from ``files_skipped`` which
                covers any per-file exception.
              - ``stale_pruned``: count of orphan UUIDs deleted by the
                ``--prune-stale`` pass (always 0 when prune_stale=False).
        """

        if not repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")

        # v0.2.47 (extras): validate + canonicalise extras up-front so
        # downstream loops can assume each entry is an existing absolute
        # directory. Silently drop entries that vanished between the
        # caller's snapshot and now — soft-fail so a single mis-typed
        # extra doesn't wedge the whole analyze.
        canonical_extras: List[Path] = []
        for extra in (extra_paths or []):
            try:
                resolved = extra.resolve()
            except (OSError, RuntimeError) as exc:
                print(
                    f"⚠️  Extra path {extra} could not be resolved: {exc} — skipping",
                    file=sys.stderr,
                )
                continue
            if not resolved.exists() or not resolved.is_dir():
                print(
                    f"⚠️  Extra path {resolved} does not exist or is not a directory — skipping",
                    file=sys.stderr,
                )
                continue
            canonical_extras.append(resolved)

        # Run Joern CFG/PDG pre-pass if requested; store on instance for use by _extract_function
        if extract_cfg or extract_pdg:
            lang_hint = language or "python"
            print("🔬 Running Joern CFG/PDG extraction (this may take a while)...")
            # v0.2.66 (Bug 3): in single-file mode, point Joern at the one
            # edited file, NOT the whole repo. Building a full CPG on every
            # keystroke would re-introduce the whole-repo work this fix
            # exists to eliminate. `importCode` accepts a single file.
            cfg_pdg_target = only_file if only_file is not None else repo_path
            self._cfg_pdg_data: Dict[str, Any] = self._extract_cfg_pdg(
                cfg_pdg_target, lang_hint, extract_cfg=extract_cfg, extract_pdg=extract_pdg
            )
            if self._cfg_pdg_data:
                print(f"   Extracted data for {len(self._cfg_pdg_data)} functions")
            else:
                print("   No CFG/PDG data extracted (joern unavailable or no output)")
        else:
            self._cfg_pdg_data = {}

        # Enable visited-UUID tracking when caller wants prune-stale.
        # See _dedup_insert + _create_or_update_module — both record
        # to self.visited_uuids when self._track_visited is True.
        if prune_stale:
            self._track_visited = True
            self.visited_uuids = set()  # fresh state per run

        stats = {
            'modules': 0,
            'classes': 0,
            'functions': 0,
            'apis': 0,
            'files_analyzed': 0,
            'files_skipped': 0,
            # v0.2.16 (bug 0.2): granular error tracking so main() can
            # surface a non-zero exit code when most files fail to
            # write (was: exit 0 = silent success).
            'insert_errors': 0,
            # v0.2.16 (1.4 / H): count of stale orphans pruned at end.
            'stale_pruned': 0,
        }

        # Language dispatch: auto-detect from extensions, or filter by --language
        lang = language.lower() if language else None

        lang_dispatch = [
            ('python',     self._find_python_files, self._analyze_python_file),
            ('lua',        self._find_lua_files,    self._analyze_lua_file),
            ('cpp',        self._find_cpp_files,    self._analyze_cpp_file),
            ('javascript', self._find_js_files,     self._analyze_js_file),
            ('typescript', self._find_ts_files,     self._analyze_js_file),
            ('go',         self._find_go_files,     self._analyze_go_file),
            ('rust',       self._find_rust_files,   self._analyze_rust_file),
            ('java',       self._find_java_files,   self._analyze_java_file),
            ('ruby',       self._find_ruby_files,   self._analyze_ruby_file),
            ('shell',      self._find_shell_files,  self._analyze_shell_file),
            ('csharp',     self._find_csharp_files, self._analyze_csharp_file),
            ('proto',      self._find_proto_files,  self._analyze_proto_file),
            # V52-O.11.B / V52-O.11.N (v0.2.53 Track E): added so the
            # orchestrator's 244 .svelte and 168 .ps1 files stop showing
            # zero functions in code-graph queries.
            ('svelte',     self._find_svelte_files,     self._analyze_svelte_file),
            ('powershell', self._find_powershell_files, self._analyze_powershell_file),
        ]

        # v0.2.18 (Plan C): two-phase loop so we know the grand-total file
        # count for the progress emitter BEFORE we start analyzing. Without
        # this, the fraction shown in the Re-analyze modal would drift up
        # per-language. The find + incremental-filter pass is cheap (no
        # Weaviate calls) so doing it twice is fine; we cache the result.
        #
        # v0.2.47 (extras): each entry now records its source root too
        # (primary repo OR a specific extra path). The dispatcher loop
        # below sets `self._current_source` for the duration of each
        # source root's pass so `_dedup_insert` / `_create_or_update_module`
        # stamp `project_source` on every emitted entity. analyze_fn
        # receives the source_root as its `repo_root` argument, so
        # `file.relative_to(repo_root)` keeps producing a path that is
        # local to the source tree (no awkward "/abs/extra/src/foo.py"
        # relative paths showing up as `path` properties).
        per_lang_files: List[Tuple[str, Any, List[Path], Path]] = []
        if only_file is not None:
            # v0.2.66 (Bug 3): single-file scope. Skip the rglob walk +
            # the HEAD~1..HEAD git diff entirely; build a one-entry
            # dispatch directly from the edited file's extension. The file
            # still flows through `analyze_fn(f, repo_path)` below, so
            # `_get_existing_module` (per-file) + `_dedup_insert`
            # content-hash (per-object) make an unchanged file a no-op.
            per_lang_files = self._single_file_dispatch(
                only_file, repo_path, lang_dispatch, lang,
            )
        else:
            source_roots: List[Path] = [repo_path] + canonical_extras
            for source_root in source_roots:
                for lang_name, find_fn, analyze_fn in lang_dispatch:
                    if lang and lang != lang_name:
                        continue
                    files = find_fn(source_root)
                    if not files:
                        continue
                    if incremental:
                        files = self._filter_changed_files(
                            source_root, files, since_commit=since_commit,
                        )
                        if not files:
                            # Quiet skip — the typical case for clean repos.
                            # We still surface one line per (root, lang) so
                            # the operator sees the walk happened.
                            print(
                                f"ℹ️  No changed {lang_name} files to analyze "
                                f"under {source_root}"
                            )
                            continue
                    per_lang_files.append(
                        (lang_name, analyze_fn, list(files), source_root)
                    )

        total_files = sum(len(fs) for _, _, fs, _ in per_lang_files)
        seen_files = 0

        for lang_name, analyze_fn, files, source_root in per_lang_files:
            is_extra = source_root != repo_path
            extra_tag = f" (extra: {source_root})" if is_extra else ""
            print(f"📂 Found {len(files)} {lang_name} files to analyze{extra_tag}")

            # v0.2.18 (Plan C): every analyze_*_file path threads its canonical
            # language ID through `_current_language` so insert sites can stamp
            # the `language` property without changing each method's signature.
            # The dispatcher restores the prior value (in practice always "") on
            # exit so back-to-back lang_dispatch iterations don't leak state.
            self._current_language = lang_name
            # v0.2.47 (extras): same pattern for `project_source`. POSIX
            # path so Linux/macOS/Windows all produce a stable property
            # value (matches the file_path_rel POSIX convention).
            #
            # v0.2.66 (Bug 3, part b): in single-file mode, the per-edit hook
            # may pass a `canonical_source` (the edited file's git MAIN repo
            # root). Stamp THAT instead of the on-disk `source_root` so a
            # worktree edit converges on the canonical object (see the
            # `canonical_source` arg docstring). Only honoured for the
            # single-file path; whole-tree walks keep per-root provenance.
            if only_file is not None and canonical_source:
                self._current_source = canonical_source
            else:
                self._current_source = source_root.as_posix()

            for f in files:
                if self._progress_emitter is not None and total_files > 0:
                    try:
                        rel = str(f.relative_to(source_root).as_posix())
                    except Exception:
                        rel = str(f)
                    try:
                        self._progress_emitter(
                            float(seen_files) / float(total_files),
                            f"Analyzing {rel}",
                            rel,
                            lang_name,
                        )
                    except Exception:
                        # Progress emission must never block analysis.
                        pass
                seen_files += 1
                try:
                    result = analyze_fn(f, source_root)
                    stats['modules']  += result.get('modules', 0)
                    stats['classes']  += result.get('classes', 0)
                    stats['functions'] += result.get('functions', 0)
                    stats['apis']     += result.get('apis', 0)
                    stats['files_analyzed'] += 1
                except _DedupInsertError as e:
                    # v0.2.16 (bug 0.2): write-to-Weaviate failure.
                    # Distinct from generic parse/IO errors so main()
                    # can return exit code 4 (vs. silent success).
                    # v0.2.47: relative-to the actual source_root (could
                    # be an extra path), not always repo_path.
                    try:
                        rel_for_msg = f.relative_to(source_root)
                    except ValueError:
                        rel_for_msg = f
                    print(f"⚠️  Insert error in {rel_for_msg}: {e}")
                    stats['files_skipped'] += 1
                    stats['insert_errors'] += 1
                except Exception as e:
                    # Non-insert failure (parse error not caught
                    # downstream, regex bug, file IO, etc.). Skip the
                    # file but DON'T flag the run as broken — the
                    # exit code stays 0 unless files_analyzed == 0.
                    try:
                        rel_for_msg = f.relative_to(source_root)
                    except ValueError:
                        rel_for_msg = f
                    print(f"⚠️  Error analyzing {rel_for_msg}: {e}")
                    stats['files_skipped'] += 1
                    # Fallback: still classify as insert_error if a
                    # _DedupInsertError got chained through some
                    # unexpected re-raise (defense-in-depth so the
                    # counter never under-reports).
                    cause = e
                    seen_dedup = False
                    for _ in range(8):  # cap loop to avoid pathological cycles
                        if isinstance(cause, _DedupInsertError):
                            seen_dedup = True
                            break
                        cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
                        if cause is None:
                            break
                    if seen_dedup:
                        stats['insert_errors'] += 1

        # Clear the per-language context now that the dispatch loop is
        # done (Plan C). The prune pass below doesn't depend on it.
        self._current_language = ""
        # v0.2.47: also clear the per-source-root context so a subsequent
        # standalone insert (cross-reference creation in the post-loop)
        # doesn't accidentally re-stamp the last extra's path.
        self._current_source = ""

        # v0.2.16 (1.4 / addendum H): --prune-stale pass.
        # Walk every per-project code-graph collection and delete any
        # object whose UUID was NOT visited during this analyze run.
        # The visited-UUID set was populated as a side-effect of
        # _dedup_insert + _create_or_update_module calls when
        # self._track_visited was True.
        #
        # v0.2.18 (Plan C): when `language` was passed to analyze_repository,
        # the prune is SCOPED to that language so cross-language data in
        # other Code* rows is preserved. The dispatcher only walked files of
        # `language`, so any per-row delete must filter `language ==
        # canonical(language)` before deleting. See `_prune_stale_objects`
        # for the implementation.
        if prune_stale:
            self._prune_language = _canonical_lang_id(language) if language else ""
            stats['stale_pruned'] = self._prune_stale_objects()

        return stats

    def _prune_stale_objects(self) -> int:
        """Delete code-graph objects that were not visited this run.

        v0.2.16 (1.4 / addendum H): companion to the _dedup_insert
        replace() switch. With replace() upserts, deleted files leave
        orphan UUIDs because nothing tells Weaviate "this used to
        exist, please remove it". This pass closes that gap.

        v0.2.18 (Plan C): when `self._prune_language` is non-empty, the
        prune is SCOPED — only rows whose `language` property matches the
        canonical ID are candidates for deletion. Rows from other
        languages are preserved (their UUIDs aren't in `visited_uuids`
        either, but they're correctly out-of-scope for this run).

        Algorithm:
          For each per-project collection:
            - Enumerate every UUID where ``project == self.project_name``.
              (And, when language-scoped: ``language == self._prune_language``.)
            - Subtract the set of UUIDs we visited this run.
            - Delete the difference (the stale ones).

        Returns:
            Total number of stale objects deleted across all
            collections. Printed per-collection on stdout so the
            launcher's log shows what happened.
        """
        if not self._track_visited:
            # Defensive: should never happen (caller gates on
            # prune_stale=True which sets _track_visited=True), but
            # guard against a future code path that forgets.
            print("⚠️  _prune_stale_objects called without _track_visited — skipping")
            return 0

        total_pruned = 0
        per_collection_visited: Dict[str, Set[str]] = {}
        for coll_name, uid in self.visited_uuids:
            per_collection_visited.setdefault(coll_name, set()).add(uid)

        collections = [
            self.modules_collection,
            self.classes_collection,
            self.functions_collection,
            self.apis_collection,
            self.interactions_collection,
        ]

        scope_lang = getattr(self, "_prune_language", "") or ""
        if scope_lang:
            print(
                f"🧹 Language-scoped prune active: deleting only "
                f"language={scope_lang!r} entries not visited this run"
            )

        for coll in collections:
            if coll is None:
                continue
            visited = per_collection_visited.get(coll.name, set())
            pruned = self._prune_collection(
                coll, visited, language_scope=scope_lang,
            )
            if pruned:
                print(f"🧹 Pruned {pruned} stale objects from {coll.name}")
                total_pruned += pruned

        return total_pruned

    def _prune_collection(
        self,
        collection,
        visited_uuids: Set[str],
        language_scope: str = "",
    ) -> int:
        """Delete every object in ``collection`` whose project matches
        ``self.project_name`` AND whose UUID is not in ``visited_uuids``.

        v0.2.18 (Plan C): when ``language_scope`` is a non-empty canonical
        language ID, additionally filter by ``language == language_scope``
        (case-insensitive, after `_canonical_lang_id` normalisation so
        legacy mixed-case rows like `"Python"` are recognised as matching
        `"python"`). Rows with no `language` property are treated as
        unknown-language and PRESERVED — they predate the v0.2.18 schema
        migration and the next full re-analyze (no `--language`) will
        repopulate them.

        Why filter on the ``project`` property as well as the
        collection name: a per-project collection like
        ``MyProject_CodeFunction`` should always belong to a single
        project, but defensive in case the schema ever permits
        cross-project sharing. The double-filter is essentially free
        (no extra query roundtrip beyond the initial enumerate).
        """
        pruned = 0
        # Read `language` only when needed so a missing-property collection
        # (pre-migration) doesn't 422 the enumerate. Weaviate returns None
        # for missing-on-row props which we treat as "unknown language".
        return_props = ["project"]
        if language_scope:
            return_props.append("language")

        try:
            for obj in collection.iterator(
                return_properties=return_props,
            ):
                props = obj.properties or {}
                obj_project = props.get("project")
                # Only consider objects belonging to this project. Foreign-
                # project rows (shouldn't exist in per-project collections,
                # but defensive) are left alone.
                if obj_project not in (None, "", self.project_name):
                    continue

                # Plan C: language-scoped filter. Rows without a language
                # property (pre-v0.2.18 data) are PRESERVED — they need a
                # full re-analyze to repopulate the field. Rows with a
                # language other than the scope are out-of-scope this run.
                if language_scope:
                    row_lang = _canonical_lang_id(props.get("language"))
                    if not row_lang:
                        # Unknown / pre-migration row → preserve.
                        continue
                    if row_lang != language_scope:
                        # Different-language row → preserve (this is the
                        # entire point of language-scoped prune).
                        continue

                if str(obj.uuid) in visited_uuids:
                    continue
                try:
                    collection.data.delete_by_id(uuid=str(obj.uuid))
                    pruned += 1
                except Exception as exc:
                    logger.warning(
                        f"Failed to prune {obj.uuid} from {collection.name}: {exc}"
                    )
        except Exception as exc:
            # Iterating a freshly-created collection can fail if it
            # has no data yet; treat as zero-prune.
            logger.debug(f"Prune enumeration on {collection.name} failed: {exc}")

        return pruned

    def _single_file_dispatch(
        self,
        only_file: Path,
        repo_path: Path,
        lang_dispatch: List[Tuple[str, Any, Any]],
        lang_filter: Optional[str],
    ) -> List[Tuple[str, Any, List[Path], Path]]:
        """Build the one-entry per-language dispatch for single-file mode.

        v0.2.66 (Bug 3): the per-edit hook hands the analyzer exactly the
        file the user just edited. This maps that file to its
        ``lang_dispatch`` entry by extension (via ``_dispatch_name_for_file``,
        which mirrors the ``_find_*_files`` glob/skip rules) WITHOUT
        re-walking the tree. ``repo_path`` is the relativization root:
        every downstream ``analyze_fn(f, repo_path)`` computes
        ``f.relative_to(repo_path).as_posix()`` for the collection key, so
        the file must live under ``repo_path``. NIT (Bug 3): both
        ``only_file`` and ``repo_path`` are ``.resolve()``-normalized here so
        a symlinked root (e.g. macOS ``/tmp``→``/private/tmp``) can't make
        ``relative_to`` spuriously fail and silently drop the edit; the
        RESOLVED root is returned as the source root so the downstream
        ``relative_to`` uses the same symlink-normalized operand.

        Returns an empty list (a clean no-op for the caller's analyze loop)
        when any of the following hold — never raises, so a stray edit can
        never wedge the hook:
          * the file does not exist (deleted between the edit and the
            debounced run);
          * the file is not under ``repo_path`` (wrong relativization root
            would create duplicate/zombie rows keyed on a bad path);
          * the extension is unrecognised / explicitly skipped
            (``.d.ts``, ``.min.js``);
          * a ``--language`` filter was supplied and the file's language
            does not match it.
        """
        try:
            resolved = only_file.resolve()
        except (OSError, RuntimeError) as exc:
            print(
                f"⚠️  Single-file analyze: could not resolve {only_file}: {exc}"
                " — skipping",
                file=sys.stderr,
            )
            return []

        # NIT (Bug 3): symlink-normalize the root too, so the gate below and
        # the downstream `relative_to` compare like-for-like (macOS /tmp
        # symlink footgun). Soft-fail to the raw path if resolve() blows up.
        try:
            repo_root = repo_path.resolve()
        except (OSError, RuntimeError):
            repo_root = repo_path

        if not resolved.is_file():
            print(
                f"ℹ️  Single-file analyze: {resolved} is not a file (deleted?)"
                " — nothing to index",
                file=sys.stderr,
            )
            return []

        # v0.2.66 (Bug 3, part c): `.claude/state/` is transient scratch
        # (tool_backups snapshots, session state) — NEVER source. Skip it
        # defensively here too, so a direct `--only-file .claude/state/...`
        # invocation is a no-op even though the hook also guards it. The
        # directory walk doesn't reach state/ in practice (callers point at
        # a repo root, not state/), so this is single-file-mode-only.
        parts = resolved.parts
        for i in range(len(parts) - 1):
            if parts[i] == ".claude" and parts[i + 1] == "state":
                print(
                    f"ℹ️  Single-file analyze: {resolved} is under .claude/state/"
                    " (transient scratch) — skipping",
                    file=sys.stderr,
                )
                return []

        # Relativization-correctness gate. Collections key on repo-relative
        # POSIX paths; a file outside repo_root would either raise in
        # `relative_to` or (worse) be stamped with an absolute path,
        # creating a duplicate row that the directory walk never matches.
        try:
            resolved.relative_to(repo_root)
        except ValueError:
            print(
                f"⚠️  Single-file analyze: {resolved} is not under repo root "
                f"{repo_root} — skipping (would create a mis-keyed row)",
                file=sys.stderr,
            )
            return []

        dispatch_name = _dispatch_name_for_file(resolved)
        if not dispatch_name:
            # Unrecognised / deliberately-skipped extension. The directory
            # walk would not have indexed it either → silent no-op.
            return []

        if lang_filter and lang_filter != dispatch_name:
            # An explicit --language was passed and this file isn't it.
            return []

        for lang_name, _find_fn, analyze_fn in lang_dispatch:
            if lang_name == dispatch_name:
                # Return the RESOLVED root so the downstream
                # `analyze_fn(f, source_root)` relativizes against the same
                # symlink-normalized operand we gated on.
                return [(lang_name, analyze_fn, [resolved], repo_root)]

        # dispatch_name not present in lang_dispatch (shouldn't happen —
        # the map is kept in lock-step). Defensive no-op.
        return []

    def _find_python_files(self, repo_path: Path) -> List[Path]:
        """Find all Python files in repository."""
        ignore_dirs = _ignore_dirs_for('python')
        return sorted([
            f for f in repo_path.rglob('*.py')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_lua_files(self, repo_path: Path) -> List[Path]:
        """Find all Lua files in repository."""
        ignore_dirs = _ignore_dirs_for('lua')
        return sorted([
            f for f in repo_path.rglob('*.lua')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_cpp_files(self, repo_path: Path) -> List[Path]:
        """Find all C++/header files in repository."""
        ignore_dirs = _ignore_dirs_for('cpp')
        files = []
        for ext in ('*.cpp', '*.cc', '*.cxx', '*.c', '*.h', '*.hpp'):
            files.extend([
                f for f in repo_path.rglob(ext)
                if not any(d in f.parts for d in ignore_dirs)
            ])
        return sorted(files)

    def _find_js_files(self, repo_path: Path) -> List[Path]:
        """Find all JavaScript files in repository."""
        ignore_dirs = _ignore_dirs_for('js')
        skip_suffixes = {'.min.js', '.config.js', '.config.mjs'}
        files = []
        for ext in ('*.js', '*.mjs', '*.jsx'):
            for f in repo_path.rglob(ext):
                if any(d in f.parts for d in ignore_dirs):
                    continue
                if any(f.name.endswith(s) for s in skip_suffixes):
                    continue
                if f.name.startswith('vite.config'):
                    continue
                files.append(f)
        return sorted(files)

    def _find_ts_files(self, repo_path: Path) -> List[Path]:
        """Find all TypeScript files in repository."""
        ignore_dirs = _ignore_dirs_for('ts')
        skip_suffixes = {'.config.ts', '.config.mts'}
        files = []
        for ext in ('*.ts', '*.tsx'):
            for f in repo_path.rglob(ext):
                if any(d in f.parts for d in ignore_dirs):
                    continue
                if any(f.name.endswith(s) for s in skip_suffixes):
                    continue
                if f.name.startswith('vite.config'):
                    continue
                # Skip .d.ts declaration files (type stubs, not source)
                if f.name.endswith('.d.ts'):
                    continue
                files.append(f)
        return sorted(files)

    def _find_go_files(self, repo_path: Path) -> List[Path]:
        """Find all Go files in repository."""
        ignore_dirs = _ignore_dirs_for('go')
        return sorted([
            f for f in repo_path.rglob('*.go')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_rust_files(self, repo_path: Path) -> List[Path]:
        """Find all Rust files in repository."""
        ignore_dirs = _ignore_dirs_for('rust')
        return sorted([
            f for f in repo_path.rglob('*.rs')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_java_files(self, repo_path: Path) -> List[Path]:
        """Find all Java files in repository."""
        ignore_dirs = _ignore_dirs_for('java')
        return sorted([
            f for f in repo_path.rglob('*.java')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_ruby_files(self, repo_path: Path) -> List[Path]:
        """Find all Ruby files in repository."""
        ignore_dirs = _ignore_dirs_for('ruby')
        return sorted([
            f for f in repo_path.rglob('*.rb')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_shell_files(self, repo_path: Path) -> List[Path]:
        """Find all Shell script files in repository."""
        ignore_dirs = _ignore_dirs_for('shell')
        files = []
        for ext in ('*.sh', '*.bash'):
            files.extend([
                f for f in repo_path.rglob(ext)
                if not any(d in f.parts for d in ignore_dirs)
            ])
        return sorted(files)

    def _find_csharp_files(self, repo_path: Path) -> List[Path]:
        """Find all C# files in repository."""
        ignore_dirs = _ignore_dirs_for('csharp')
        return sorted([
            f for f in repo_path.rglob('*.cs')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_svelte_files(self, repo_path: Path) -> List[Path]:
        """Find all Svelte component files in repository.

        V52-O.11.B (v0.2.53 Track E): added so the launcher's 244 .svelte
        files stop indexing as zero functions in the code graph. Shares
        the JS/TS ignore-dir set because Svelte sits in the same npm
        tooling ecosystem (node_modules, dist, .svelte-kit codegen).
        """
        ignore_dirs = _ignore_dirs_for('js')
        return sorted([
            f for f in repo_path.rglob('*.svelte')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_powershell_files(self, repo_path: Path) -> List[Path]:
        """Find all PowerShell script files in repository.

        V52-O.11.N (v0.2.53 Track E): added so the orchestrator's 168
        .ps1 files (template hooks + Windows-side install scripts)
        stop indexing as zero functions in the code graph. Reuses the
        shell ignore-dir set since .ps1 lives in the same parts of the
        tree as .sh (templates/hooks/, scripts/).
        """
        ignore_dirs = _ignore_dirs_for('shell')
        files = []
        for ext in ('*.ps1', '*.psm1'):
            files.extend([
                f for f in repo_path.rglob(ext)
                if not any(d in f.parts for d in ignore_dirs)
            ])
        return sorted(files)

    def _find_proto_files(self, repo_path: Path) -> List[Path]:
        """Find all Protocol Buffer definition files."""
        ignore_dirs = _ignore_dirs_for('proto')
        return sorted([
            f for f in repo_path.rglob('*.proto')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _analyze_csharp_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a C# file using regex-based parsing.

        Extracts using directives, classes, interfaces, methods, and ASP.NET route attributes.
        Also populates CodeAPI for [HttpGet/Post/Put/Delete/Patch] annotated methods.
        """
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//')
                   and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # using directives
        imports = re.findall(r'^\s*using\s+([\w.]+)\s*;', content, re.MULTILINE)

        # namespace
        ns_match = re.search(r'namespace\s+([\w.]+)', content)
        ns = ns_match.group(1) if ns_match else file_path.stem

        # Classes / interfaces / records
        class_pattern = re.compile(
            r'(?:public|private|protected|internal|abstract|sealed|partial|\s)+'
            r'(?:class|interface|record|struct)\s+([\w<>, ]+?)(?:\s*:\s*[\w<>, ]+?)?\s*\{',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            raw = m.group(1).strip().split('<')[0].strip()  # strip generics
            if not raw or raw[0].islower():
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[raw] = start_line

        # Methods: access modifier + return type + name(...)
        method_pattern = re.compile(
            r'(?:public|private|protected|internal|static|virtual|override|async|abstract|\s)+'
            r'(?:[\w<>\[\]?]+\s+)+([\w]+)\s*\([^)]*\)\s*(?:\{|=>|;)',
            re.MULTILINE
        )

        # ASP.NET HTTP attributes for CodeAPI
        http_attr_pattern = re.compile(
            r'\[Http(Get|Post|Put|Delete|Patch|Options|Head)\s*(?:\([^)]*\))?\]'
            r'(?:\s*\[[^\]]*\])*'           # other attributes in between
            r'[^{]*?'                        # skip to method
            r'(?:public|private|protected)\s+'
            r'(?:async\s+)?(?:Task[<>\w]*\s+|IActionResult\s+|ActionResult[<>\w]*\s+)?'
            r'([\w]+)\s*\(',
            re.MULTILINE | re.DOTALL
        )
        # Route attribute (base route on controller or per-method)
        route_attr_pattern = re.compile(r'\[Route\s*\(\s*["\']([^"\']+)["\']')

        # Module summary
        file_comment = ''
        for line in source_lines[:20]:
            s = line.strip()
            if s.startswith('///') or s.startswith('//'):
                file_comment = s.lstrip('/').strip()
                break
        summary_parts = [f"C# module: {relative_path} (namespace {ns})"]
        if file_comment:
            summary_parts.append(file_comment)
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'while (', 'for (', 'foreach (', 'switch (', 'catch (']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="C#", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Classes
        for cname, start_line in class_info.items():
            _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 60)
            class_lines = source_lines[max(0, start_line - 1):_class_end_line]
            class_body = '\n'.join(class_lines)
            # V52-O.11.F.2-CSHARP (v0.2.52, 2026-06-09): scope `methods` to
            # method declarations INSIDE `class <cname> { ... }` (also struct,
            # record, interface). Pre-V52-O.11.F.2 this line ran
            # `method_pattern.finditer(content_clean)` over the WHOLE file —
            # attributing EVERY method to EVERY class. Same antipattern as
            # V52-O.11.F (Rust). Audit a79152.
            methods = _csharp_methods_for_class(content_clean, cname, source_lines)
            signature = f"class {cname}"
            embedding = embed_class(signature, class_body, methods=methods[:10], language="csharp")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname, "full_name": f"{ns}.{cname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        # Methods
        for m in method_pattern.finditer(content_clean):
            mname = m.group(1)
            if mname in ('if', 'while', 'for', 'foreach', 'switch', 'catch', 'try', 'return', 'new', 'throw'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 50)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            enclosing = next(
                (c for c, cl in sorted(class_info.items(), key=lambda x: x[1], reverse=True)
                 if cl <= start_line), file_path.stem
            )
            is_async = bool(re.search(r'\basync\b', body[:200]))
            full_name = f"{ns}.{enclosing}.{mname}"
            signature = f"{mname}(...)"
            embedding = embed_function(signature, body, language="csharp")
            insert_params = {
                "properties": {
                    "name": mname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": is_async, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            func_uuid = self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

            # ASP.NET route entries for HTTP-attributed methods
            # Check if this method has an [Http*] attribute in the lines just above it
            pre_lines = source_lines[max(0, start_line - 5):start_line]
            pre_ctx = '\n'.join(pre_lines)
            http_m = re.search(r'\[Http(Get|Post|Put|Delete|Patch|Options|Head)', pre_ctx, re.IGNORECASE)
            if http_m:
                http_method = http_m.group(1).upper()
                # Extract route from attribute or from class [Route] base
                route_m = re.search(r'\[Http\w+\s*\(\s*["\']([^"\']+)["\']', pre_ctx)
                route = route_m.group(1) if route_m else f"/{mname.lower()}"
                # Base controller route
                ctrl_route = ''
                base_route_m = route_attr_pattern.search(content_clean[:m.start()])
                if base_route_m:
                    ctrl_route = '/' + base_route_m.group(1).strip('/')
                full_route = ctrl_route + ('/' if ctrl_route else '') + route.lstrip('/')
                api_desc = f"C# ASP.NET {http_method} {full_route} → {ns}.{enclosing}.{mname}"
                api_embedding = generate_embedding(api_desc)
                api_params: Dict[str, Any] = {
                    "properties": {
                        "endpoint": full_route, "method": http_method,
                        "api_description": api_desc,
                        "parameters": [], "returns": "",
                        "project": self.project_name, "proxy_target": "",
                    },
                    "references": {"handler": func_uuid},
                }
                if api_embedding:
                    api_params["vector"] = _shape_for_insert(api_embedding)
                self._dedup_insert(self.apis_collection, api_params, api_params["properties"].get("endpoint", "") + ":" + api_params["properties"].get("method", ""), file_path_rel=relative_path)
                stats.setdefault('apis', 0)
                stats['apis'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "csharp", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "C#", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_proto_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Protocol Buffer (.proto) file.

        Proto files define cross-language service contracts. Each RPC method is stored
        as a CodeAPI entry (inbound contract), and each message type as a CodeClass.
        """
        stats = {'modules': 0, 'classes': 0, 'apis': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip comments
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # package name
        pkg_match = re.search(r'^package\s+([\w.]+)\s*;', content, re.MULTILINE)
        pkg = pkg_match.group(1) if pkg_match else file_path.stem

        # imports (other .proto files)
        imports = re.findall(r'import\s+["\']([^"\']+)["\']', content)

        # Message types → CodeClass
        msg_pattern = re.compile(r'^message\s+([\w]+)\s*\{', re.MULTILINE)
        message_names: List[str] = []
        for m in msg_pattern.finditer(content_clean):
            mname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 30)
            class_body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            signature = f"message {mname}"
            embedding = generate_embedding(f"Proto message: {mname}\n{class_body[:400]}")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": mname, "full_name": f"{pkg}.{mname}",
                    "class_body": class_body, "methods": [],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": end_line,
                    "project": self.project_name,
                },
                "references": {"module": ""},   # no module UUID yet; filled after
            }
            message_names.append(mname)
            # Store after module creation below

        # Service RPC methods → CodeAPI
        svc_pattern = re.compile(r'^service\s+([\w]+)\s*\{', re.MULTILINE)
        rpc_pattern = re.compile(
            r'rpc\s+([\w]+)\s*\(\s*([\w.]+)\s*\)\s*returns\s*\(\s*([\w.]+)\s*\)',
            re.MULTILINE
        )
        rpc_entries: list = []
        for svc_m in svc_pattern.finditer(content_clean):
            svc_name = svc_m.group(1)
            svc_start = svc_m.start()
            # Find matching closing brace
            depth, svc_end = 0, len(content_clean)
            for i, ch in enumerate(content_clean[svc_start:]):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        svc_end = svc_start + i
                        break
            svc_body = content_clean[svc_start:svc_end]
            for rpc_m in rpc_pattern.finditer(svc_body):
                rpc_entries.append({
                    'service': svc_name,
                    'method': rpc_m.group(1),
                    'input': rpc_m.group(2),
                    'output': rpc_m.group(3),
                })

        # Module summary
        summary_parts = [f"Proto: {relative_path} (package {pkg})"]
        if message_names:
            summary_parts.append(f"Messages: {', '.join(message_names[:8])}")
        if rpc_entries:
            svc_names = list({e['service'] for e in rpc_entries})
            summary_parts.append(f"Services: {', '.join(svc_names)}")
        module_summary = '\n'.join(summary_parts)

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Proto", loc=loc, complexity=1.0,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Now insert message classes with proper module UUID
        for m in msg_pattern.finditer(content_clean):
            mname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 30)
            class_body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            signature = f"message {mname}"
            embedding = generate_embedding(f"Proto message: {mname}\n{class_body[:400]}")
            insert_params = {
                "properties": {
                    "name": mname, "full_name": f"{pkg}.{mname}",
                    "class_body": class_body, "methods": [],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": end_line,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        # Insert RPC methods as CodeAPI entries (inbound service contract)
        for entry in rpc_entries:
            endpoint = f"grpc:{pkg}.{entry['service']}/{entry['method']}"
            api_desc = (
                f"gRPC {entry['service']}.{entry['method']} "
                f"({entry['input']}) → ({entry['output']}) [{pkg}]"
            )
            embedding = generate_embedding(api_desc)
            api_params: Dict[str, Any] = {
                "properties": {
                    "endpoint": endpoint, "method": "gRPC",
                    "api_description": api_desc,
                    "parameters": [entry['input']], "returns": entry['output'],
                    "project": self.project_name, "proxy_target": "",
                },
            }
            if embedding:
                api_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.apis_collection, api_params, api_params["properties"].get("endpoint", "") + ":" + api_params["properties"].get("method", ""), file_path_rel=relative_path)
            stats['apis'] += 1

        return stats

    def _analyze_js_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a JavaScript/TypeScript file using regex-based parsing.

        Extracts imports, functions, classes, Fastify route definitions, and
        external HTTP calls (fetch). Handles both .js/.mjs and .ts/.tsx files.
        """
        stats = {'modules': 0, 'classes': 0, 'functions': 0, 'apis': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//') and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Determine language label from extension
        suffix = file_path.suffix.lower()
        if suffix in ('.ts', '.tsx'):
            language = "TypeScript"
        else:
            language = "JavaScript"

        # Strip single-line and multi-line comments for cleaner pattern matching
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # --- Imports ---
        imports: List[str] = []
        # ES module imports: import { x } from './y' / import x from './y' / import './y'
        for m in re.finditer(r"""import\s+(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?)\s+from\s+)?['"]([^'"]+)['"]""", content):
            imports.append(m.group(1))
        # CommonJS require: require('./y') / require('y')
        for m in re.finditer(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""", content):
            imports.append(m.group(1))

        # --- Classes ---
        class_names: List[str] = []
        class_pattern = re.compile(
            r'(?:export\s+(?:default\s+)?)?class\s+([\w]+)\s*(?:extends\s+([\w.]+)\s*)?{',
            re.MULTILINE
        )
        class_info: Dict[str, Tuple[int, Optional[str]]] = {}  # name -> (start_line, base_class)
        for m in class_pattern.finditer(content_clean):
            cname = m.group(1)
            base = m.group(2)
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[cname] = (start_line, base)
            class_names.append(cname)

        # --- Functions ---
        # Covers: export async function name(, export function name(,
        #         async function name(, function name(
        func_pattern = re.compile(
            r'(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([\w]+)\s*\(',
            re.MULTILINE
        )
        all_func_names: List[str] = []
        func_matches: List[Tuple[str, int, bool]] = []  # (name, start_line, is_async)
        for m in func_pattern.finditer(content_clean):
            fname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            # Check if 'async' appears before 'function' in the matched text
            match_text = content_clean[m.start():m.end()]
            is_async = 'async' in match_text
            func_matches.append((fname, start_line, is_async))
            all_func_names.append(fname)

        # Also catch arrow function exports: export const name = async (...) =>
        arrow_pattern = re.compile(
            r'(?:export\s+)?(?:const|let|var)\s+([\w]+)\s*=\s*(async\s+)?(?:\([^)]*\)|[\w]+)\s*=>',
            re.MULTILINE
        )
        for m in arrow_pattern.finditer(content_clean):
            fname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            is_async = m.group(2) is not None
            func_matches.append((fname, start_line, is_async))
            all_func_names.append(fname)

        # --- Fastify route definitions ---
        # Pattern: { secure: true/false, method: 'POST', url: '/tx/build', handler, schema }
        # May span multiple lines
        route_pattern = re.compile(
            r'\{[^}]*method:\s*[\'"](\w+)[\'"][^}]*url:\s*[\'"]([^\'"]+)[\'"][^}]*\}',
            re.DOTALL
        )
        routes: List[Dict[str, Any]] = []
        for m in route_pattern.finditer(content):
            route_block = m.group(0)
            method = m.group(1).upper()
            url = m.group(2)
            # Extract secure flag
            secure_match = re.search(r'secure:\s*(true|false)', route_block)
            secure = secure_match.group(1) == 'true' if secure_match else False
            # Extract handler reference
            handler_match = re.search(r'handler:\s*([\w.]+)', route_block)
            handler_ref = handler_match.group(1) if handler_match else None
            routes.append({
                'method': method,
                'url': url,
                'secure': secure,
                'handler': handler_ref,
            })

        # --- External HTTP calls (fetch) ---
        # Match: fetch(`${EXAMPLE_API_URL}/api/tx/build`, ...) or fetch('http://example:8000/api/somepath', ...)
        external_calls: List[str] = []
        # Template literal with env var prefix: fetch(`${VAR}/path/here`...)
        for m in re.finditer(r'fetch\s*\(\s*`\$\{[\w]+\}(/[^`]*)`', content):
            external_calls.append(m.group(1))
        # Literal URL with protocol: fetch('http://host:port/path'...)
        for m in re.finditer(r"""fetch\s*\(\s*['"]https?://[^/'"]*(/[^'"]*?)['"]""", content):
            external_calls.append(m.group(1))
        # Template literal without env var but with http prefix: fetch(`http://host:port/path`...)
        for m in re.finditer(r'fetch\s*\(\s*`https?://[^/`]*(\/[^`]*?)`', content):
            path = m.group(1)
            if path not in external_calls:
                external_calls.append(path)

        # --- Module summary ---
        first_comments: List[str] = []
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('//'):
                first_comments.append(s.lstrip('/').strip())
            elif s.startswith('*') and not s.startswith('*/'):
                first_comments.append(s.lstrip('*').strip())
            elif s and not s.startswith('/*'):
                break

        summary_parts = [f"{language} module: {relative_path}"]
        if first_comments:
            summary_parts.append(' '.join(first_comments[:3]))
        if class_names:
            summary_parts.append(f"Classes: {', '.join(class_names[:8])}")
        if all_func_names:
            summary_parts.append(f"Functions: {', '.join(all_func_names[:8])}")
        if routes:
            route_strs = [f"{r['method']} {r['url']}" for r in routes[:5]]
            summary_parts.append(f"Routes: {', '.join(route_strs)}")
        if external_calls:
            summary_parts.append(f"External calls: {', '.join(external_calls[:5])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'if(', 'else if', '? ', 'while (', 'while(',
                                              'for (', 'for(', 'switch (', 'switch(', 'catch (', 'catch(']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language=language, loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # --- Store classes ---
        for cname, (start_line, base_class) in class_info.items():
            _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 80)
            class_lines = source_lines[max(0, start_line - 1):_class_end_line]
            class_body = '\n'.join(class_lines)
            # V52-O.11.F.2-JS (v0.2.52, 2026-06-09): scope `methods` to
            # method declarations inside this class's brace-balanced body
            # (with full method-shape coverage: static, get/set, generators,
            # private, async + combinations — plus depth-tracking that
            # prevents nested call statements like `cb(x) { ... }` inside
            # method bodies from being miscounted as class methods).
            # Pre-V52-O.11.F.2-JS this inlined a `method_inside.finditer`
            # over `class_body` with an under-covering pattern; the new
            # helper mirrors the V52-O.11.F Rust pattern (parallel fix
            # for JS/TS class method attribution).
            methods = _js_methods_for_class(content_clean, cname, source_lines)

            signature = f"class {cname}"
            if base_class:
                signature += f" extends {base_class}"

            embedding = embed_class(signature, class_body, methods=methods[:10], language="javascript")

            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname,
                    "full_name": f"{file_path.stem}.{cname}",
                    "class_body": class_body,
                    "methods": methods[:20],
                    "signature": signature,
                    "doc": "",
                    "start_line": start_line,
                    "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        # --- Store functions ---
        for fname, start_line, is_async in func_matches:
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{fname}"
            signature = f"{'async ' if is_async else ''}function {fname}()"
            embedding = embed_function(signature, body, language="javascript")

            insert_params = {
                "properties": {
                    "name": fname,
                    "full_name": full_name,
                    "function_body": body,
                    "signature": signature,
                    "doc": "",
                    "start_line": start_line,
                    "end_line": end_line,
                    "is_async": is_async,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # --- Store Fastify routes as CodeAPI ---
        for route in routes:
            # Check if route handler calls fetch to a known path (proxy detection)
            proxy_target = None
            if route['handler'] and external_calls:
                # Simple heuristic: if there are external calls in the same file and
                # the route URL has a matching path segment, link them
                for ext_call in external_calls:
                    # If external call path matches or contains the route URL
                    if route['url'] in ext_call or ext_call.endswith(route['url']):
                        proxy_target = ext_call
                        break

            description = (
                f"{route['method']} {route['url']} "
                f"({'authenticated' if route['secure'] else 'public'})"
            )
            if route['handler']:
                description += f" -> {route['handler']}"
            if proxy_target:
                description += f" [proxies to {proxy_target}]"

            embedding = generate_embedding(description)

            insert_params = {
                "properties": {
                    "endpoint": route['url'],
                    "method": route['method'],
                    "api_description": description,
                    "parameters": [],
                    "returns": "",
                    "project": self.project_name,
                    "proxy_target": proxy_target or "",
                },
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.apis_collection, insert_params, insert_params["properties"].get("endpoint", "") + ":" + insert_params["properties"].get("method", ""), file_path_rel=relative_path)
            stats['apis'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, language, relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, language, module_uuid, file_path_rel=relative_path)

        return stats

    def _filter_changed_files(self, repo_path: Path, files: List[Path],
                              since_commit: Optional[str] = None) -> List[Path]:
        """Filter files to only those changed according to git.

        v0.2.47: ``since_commit`` lets the caller specify the lower bound
        of the diff range (``<sha>..HEAD``). Default ``None`` preserves
        the legacy behaviour of ``HEAD~1..HEAD``. Non-git roots and
        unknown SHAs fall back to the full file list with one stderr
        notice — never a hard error.
        """
        # Quick git-repo check so non-git extras (e.g. a non-versioned
        # vendored folder) skip the subprocess invocation entirely. This
        # both speeds up the common case and produces a nicer log line.
        if not (repo_path / ".git").exists():
            print(
                f"ℹ️  {repo_path} is not a git repository; analyzing all files",
                file=sys.stderr,
            )
            return files

        if since_commit:
            # Validate the SHA exists before passing it to `git diff`; an
            # unknown commit ID otherwise produces a confusing error
            # message embedded in the stderr stream.
            rev_check = subprocess.run(
                ['git', 'rev-parse', '--verify', f'{since_commit}^{{commit}}'],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if rev_check.returncode != 0:
                print(
                    f"⚠️  --since-commit {since_commit} not found in {repo_path}; "
                    f"falling back to full scan",
                    file=sys.stderr,
                )
                return files
            diff_range_lhs = since_commit
        else:
            diff_range_lhs = "HEAD~1"

        try:
            # Get changed files from git
            result = subprocess.run(
                ['git', 'diff', '--name-only', diff_range_lhs, 'HEAD'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True
            )

            changed_paths = {repo_path / line.strip() for line in result.stdout.split('\n') if line.strip()}

            # Filter to only changed files
            return [f for f in files if f in changed_paths]

        except subprocess.CalledProcessError:
            print("⚠️  Git not available or not a git repo, analyzing all files")
            return files

    def _analyze_lua_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Lua file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('--')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Imports: require('mod') or require "mod"
        imports = re.findall(r"require\s*[(\s]*[\"']([^\"']+)[\"']", content)

        # Detect table-based classes: uppercase Name = {} or Name.__index = Name
        class_names: Set[str] = set()
        for m in re.finditer(r'^([A-Z][\w]*)\s*=\s*\{\}', content, re.MULTILINE):
            class_names.add(m.group(1))
        for m in re.finditer(r'^([\w]+)\.__index\s*=\s*\1', content, re.MULTILINE):
            class_names.add(m.group(1))

        # Function pattern: covers function f(), local function f(), Obj.f = function()
        func_pattern = re.compile(
            r'^(?:local\s+)?function\s+([\w.:]+)\s*\(([^)]*)\)|'
            r'^([\w.]+)\.([\w]+)\s*=\s*function\s*\(([^)]*)\)',
            re.MULTILINE
        )
        all_func_names = []
        for m in func_pattern.finditer(content):
            name = m.group(1) if m.group(1) else f'{m.group(3)}.{m.group(4)}'
            all_func_names.append(name)

        # Module summary from leading comments
        first_comments: List[str] = []
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('--'):
                first_comments.append(s.lstrip('-').strip())
            elif s:
                break

        summary_parts = [f"Lua module: {relative_path}"]
        if first_comments:
            summary_parts.append(' '.join(first_comments[:3]))
        if class_names:
            summary_parts.append(f"Classes: {', '.join(sorted(class_names))}")
        if all_func_names:
            summary_parts.append(f"Functions: {', '.join(all_func_names[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content.count(kw) for kw in ['if ', 'elseif ', 'while ', 'for ', 'repeat ']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Lua", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Extract classes (table-OOP pattern)
        for class_name in class_names:
            methods: List[str] = []
            for m in re.finditer(
                rf'(?:function\s+{re.escape(class_name)}[.:]([\w]+)\s*\(|'
                rf'{re.escape(class_name)}\.([\w]+)\s*=\s*function\s*\()',
                content
            ):
                methods.append(m.group(1) or m.group(2))

            class_match = re.search(rf'^{re.escape(class_name)}\s*=\s*\{{', content, re.MULTILINE)
            start_line = content[:class_match.start()].count('\n') + 1 if class_match else 1

            body = f"{class_name} = {{}}\n" + '\n'.join(
                f"function {class_name}.{mth}(...) end" for mth in methods
            )
            signature = f"{class_name} = {{}} -- Lua table class"
            # V52-O.11.H (v0.2.52, 2026-06-09): pass language="lua" — pre-
            # V52-O.11.H this passed language="javascript", routing Lua
            # embeddings through the JS code-path and mislabeling retrieval
            # scores by language. Embeddings stay 2048-dim either way
            # (model-agnostic), but downstream language-filtered retrieval
            # (--language=lua scopes) was silently never matching.
            embedding = embed_class(signature, "", methods=methods, language="lua")

            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": class_name, "full_name": f"{file_path.stem}.{class_name}",
                    "class_body": body, "methods": methods, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": start_line,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        # Extract standalone functions (skip class methods already indexed)
        class_prefixes = tuple(cn + '.' for cn in class_names) + tuple(cn + ':' for cn in class_names)

        for m in func_pattern.finditer(content):
            if m.group(1):
                func_name, args_str = m.group(1), m.group(2)
            else:
                func_name, args_str = f'{m.group(3)}.{m.group(4)}', m.group(5) or ''

            if any(func_name.startswith(p) for p in class_prefixes):
                continue

            start_line = content[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
            body = '\n'.join(source_lines[start_line - 1:end_line])

            func_full_name = f"{file_path.stem}.{func_name}"
            # V52-O.11.H (v0.2.52, 2026-06-09): see comment on the Lua class
            # embed_class call above — same fix for the function path.
            embedding = embed_function(f"function {func_name}({args_str})", body, language="lua")

            insert_params = {
                "properties": {
                    "name": func_name.split('.')[-1].split(':')[-1],
                    "full_name": func_full_name,
                    "function_body": body, "signature": f"{func_name}({args_str})",
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content, imports, "Lua", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Lua", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_cpp_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a C++/header file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//') and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip comments for pattern matching
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # Includes
        includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', content)

        # Classes / structs
        class_pattern = re.compile(
            r'(?:^|\n)\s*(?:class|struct)\s+([\w]+)\s*'
            r'(?::[^{]*)?\{',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            cname = m.group(1)
            if cname in ('if', 'else', 'while', 'for', 'switch', 'namespace', 'return'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[cname] = start_line

        # Method implementations: ClassName::methodName(...)
        method_pattern = re.compile(
            r'\b([\w]+)\s*::\s*([\w~]+)\s*\(([^)]*)\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{',
            re.MULTILINE
        )

        # Module summary from file-level comment
        file_comment = ''
        for line in source_lines[:20]:
            s = line.strip()
            if s.startswith('//') or (s.startswith('*') and not s.startswith('*/')):
                cleaned = s.lstrip('/*').strip()
                if cleaned:
                    file_comment = cleaned
                    break

        summary_parts = [f"C++ module: {relative_path}"]
        if file_comment:
            summary_parts.append(file_comment)
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'while (', 'for (', 'switch (', 'else if']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="C++", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=includes, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Extract classes
        for cname, start_line in class_info.items():
            methods = [m.group(2) for m in method_pattern.finditer(content_clean)
                       if m.group(1) == cname]
            _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 60)
            class_lines = source_lines[max(0, start_line - 1):_class_end_line]
            class_body = '\n'.join(class_lines)
            signature = f"class {cname}"
            embedding = generate_embedding(
                f"{signature}\nMethods: {', '.join(methods[:10])}\n{class_body[:500]}"
            )
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname, "full_name": f"{file_path.stem}.{cname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        # Extract method implementations
        for m in method_pattern.finditer(content_clean):
            class_name, method_name, args_str = m.group(1), m.group(2), m.group(3)
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 50)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{class_name}.{method_name}"
            signature = f"{class_name}::{method_name}({args_str})"
            embedding = embed_function(signature, body, language="cpp")
            insert_params = {
                "properties": {
                    "name": method_name, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # Cross-language interactions (C++ uses #include as import gate)
        ix = _extract_external_calls(content_clean, includes, "C++", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "C++", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_go_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Go file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip comments
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # Imports
        single_imports = re.findall(r'import\s+"([^"]+)"', content)
        block_imports = re.findall(r'"([^"]+)"', re.sub(r'import\s*\(([^)]*)\)', r'\1', content, flags=re.DOTALL))
        imports = list(dict.fromkeys(single_imports + block_imports))

        # Structs / interfaces as "classes"
        type_pattern = re.compile(r'type\s+([\w]+)\s+(?:struct|interface)\s*\{', re.MULTILINE)
        struct_info: Dict[str, int] = {}
        for m in type_pattern.finditer(content_clean):
            name = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            struct_info[name] = start_line

        # Functions: func Name(...) and methods: func (recv Type) Name(...)
        func_pattern = re.compile(
            r'func\s+(?:\([^)]+\)\s+)?([\w]+)\s*\(([^)]*)\)',
            re.MULTILINE
        )

        # Module summary
        pkg_match = re.search(r'^package\s+(\w+)', content, re.MULTILINE)
        pkg_name = pkg_match.group(1) if pkg_match else file_path.stem
        file_comment = ''
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('//'):
                file_comment = s.lstrip('/').strip()
                break
        summary_parts = [f"Go module: {relative_path} (package {pkg_name})"]
        if file_comment:
            summary_parts.append(file_comment)
        if struct_info:
            summary_parts.append(f"Types: {', '.join(list(struct_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if ', 'for ', 'switch ', 'select {']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Go", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Struct/interface entries
        for sname, start_line in struct_info.items():
            _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
            class_lines = source_lines[max(0, start_line - 1):_class_end_line]
            class_body = '\n'.join(class_lines)
            signature = f"type {sname} struct/interface"
            # V52-O.11.F.2-GO (v0.2.52, 2026-06-09): scope `methods` to
            # functions whose receiver is `sname`. Pre-fix this line
            # iterated func_pattern over the WHOLE file's content_clean,
            # attributing EVERY fn (including receiver-less package-level
            # functions and methods on OTHER structs) to EVERY struct —
            # audit a79152 reproduced: a 50-fn file with 3 structs
            # produced 150 incorrect method attributions, drowning real
            # signal in the `query_code_structure(methods, StructName)`
            # MCP path. Mirrors V52-O.11.F (Rust); Go uses receiver
            # syntax instead of `impl` blocks (see helper docstring).
            methods = _go_methods_for_struct(content_clean, sname, source_lines)
            embedding = embed_class(signature, class_body, language="go")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": sname, "full_name": f"{pkg_name}.{sname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        # Function entries
        for m in func_pattern.finditer(content_clean):
            fname, args_str = m.group(1), m.group(2)
            if fname[0].islower() and fname in ('if', 'for', 'switch', 'select'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{pkg_name}.{fname}"
            signature = f"func {fname}({args_str})"
            embedding = embed_function(signature, body, language="go")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Go", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Go", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_rust_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Rust file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # use statements
        imports = re.findall(r'use\s+([\w::{}, ]+);', content)

        # struct, enum, trait types
        type_pattern = re.compile(
            r'(?:pub\s+)?(?:struct|enum|trait)\s+([\w]+)', re.MULTILINE
        )
        struct_info: Dict[str, int] = {}
        for m in type_pattern.finditer(content_clean):
            name = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            struct_info[name] = start_line

        # Functions: fn name(...)
        # V52-O.11.G (v0.2.52, 2026-06-09): expand prefix regex to capture the
        # full Rust modifier set — `pub`, `pub(crate)`, `pub(super)`,
        # `pub(in path)`, `async`, `unsafe`, `const`, `extern "ABI"`,
        # `default` — in any order, any combination. Pre-V52-O.11.G this
        # regex only matched `pub` + `async`, silently dropping every
        # `unsafe fn`, `const fn`, `extern "C" fn`, `pub(crate) fn`, and
        # `default fn` in the codebase. Mirrors the modifier-set used in
        # `_rust_methods_for_struct`'s inner method pattern (V52-O.11.F).
        func_pattern = re.compile(
            # Zero or more modifier tokens, any order. Each token is one of:
            #   pub | pub(crate) | pub(super) | pub(in path::to::mod)
            #   async | unsafe | const | extern | extern "ABI" | default
            r'(?:(?:pub(?:\s*\([^)]*\))?|async|unsafe|const|default'
            r'|extern(?:\s+"[^"]*")?)\s+)*'
            r'fn\s+([\w]+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)',
            re.MULTILINE
        )

        # Module summary
        crate_comment = ''
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('//!') or s.startswith('///'):
                crate_comment = s.lstrip('/!').strip()
                break
        summary_parts = [f"Rust module: {relative_path}"]
        if crate_comment:
            summary_parts.append(crate_comment)
        if struct_info:
            summary_parts.append(f"Types: {', '.join(list(struct_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if ', 'while ', 'for ', 'match ', 'loop {']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Rust", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for sname, start_line in struct_info.items():
            _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
            class_lines = source_lines[max(0, start_line - 1):_class_end_line]
            class_body = '\n'.join(class_lines)
            signature = f"struct/enum/trait {sname}"
            # V52-O.11.F (v0.2.52, 2026-06-09): scope `methods` to functions
            # declared INSIDE `impl <sname>` blocks (or `impl <Trait> for
            # <sname>` blocks). Pre-V52-O.11.F this line iterated
            # func_pattern over the WHOLE file's content_clean, attributing
            # EVERY fn in the file to EVERY struct — audit a79152 reproduced:
            # a 50-fn file with 3 structs produced 150 incorrect method
            # attributions, drowning real signal in noise for the
            # `query_code_structure(methods, StructName)` MCP path.
            methods = _rust_methods_for_struct(content_clean, sname, source_lines)
            embedding = embed_class(signature, class_body, language="rust")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": sname, "full_name": f"{file_path.stem}.{sname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        for m in func_pattern.finditer(content_clean):
            fname, args_str = m.group(1), m.group(2)
            # V52-O.11.J (v0.2.52, 2026-06-09): skip functions gated by
            # `#[cfg(test)]` / `#[test]` / `#[cfg(any(test, ...))]` /
            # `#[cfg(all(test, ...))]` — test functions are not production
            # code and indexing them confuses the offline trainer + bloats
            # the CodeFunction collection. Audit a79152.
            if _is_rust_test_fn(content_clean, m.start()):
                continue
            is_async = bool(re.search(rf'async\s+fn\s+{re.escape(fname)}', content_clean))
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 40)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{fname}"
            signature = f"fn {fname}({args_str})"
            embedding = embed_function(signature, body, language="rust")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": is_async, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Rust", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Rust", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_java_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Java file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//')
                   and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # import statements
        imports = re.findall(r'import\s+([\w.]+);', content)

        # class / interface / enum
        class_pattern = re.compile(
            r'(?:public|private|protected|abstract|final|\s)*'
            r'(?:class|interface|enum)\s+([\w]+)'
            r'(?:\s+extends\s+[\w<>, ]+)?(?:\s+implements\s+[\w<>, ]+)?\s*\{',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            name = m.group(1)
            if not name or name[0].islower():
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[name] = start_line

        # Methods
        method_pattern = re.compile(
            r'(?:public|private|protected|static|final|synchronized|native|abstract|\s)+'
            r'(?:[\w<>\[\]]+\s+)+([\w]+)\s*\(([^)]*)\)\s*(?:throws\s+[\w, ]+)?\s*\{',
            re.MULTILINE
        )

        # Package + summary
        pkg_match = re.search(r'^package\s+([\w.]+);', content, re.MULTILINE)
        pkg_name = pkg_match.group(1) if pkg_match else ''
        summary_parts = [f"Java module: {relative_path}"]
        if pkg_name:
            summary_parts.append(f"Package: {pkg_name}")
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'while (', 'for (', 'switch (', 'catch (']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Java", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for cname, start_line in class_info.items():
            _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 60)
            class_lines = source_lines[max(0, start_line - 1):_class_end_line]
            class_body = '\n'.join(class_lines)
            # V52-O.11.F.2-JAVA (v0.2.52, 2026-06-09): scope `methods` to
            # method declarations INSIDE `class <cname> { ... }` (or the
            # matching interface/enum). Pre-V52-O.11.F.2 this line ran
            # `method_pattern.finditer(content_clean)` unconditionally —
            # that attributed EVERY method in the file to EVERY class.
            # Same antipattern audit a79152 flagged in Rust, fixed there
            # in V52-O.11.F. Java fix mirrors via `_java_methods_for_class`.
            methods = _java_methods_for_class(content_clean, cname, source_lines)
            signature = f"class {cname}"
            embedding = embed_class(signature, class_body, methods=methods[:10], language="java")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname,
                    "full_name": f"{pkg_name}.{cname}" if pkg_name else cname,
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        for m in method_pattern.finditer(content_clean):
            mname, args_str = m.group(1), m.group(2)
            if mname in ('if', 'while', 'for', 'switch', 'catch', 'try', 'else', 'return'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 50)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            # Find enclosing class
            enclosing = next(
                (c for c, cl in sorted(class_info.items(), key=lambda x: x[1], reverse=True)
                 if cl <= start_line), file_path.stem
            )
            full_name = f"{enclosing}.{mname}"
            signature = f"{mname}({args_str})"
            embedding = embed_function(signature, body, language="java")
            insert_params = {
                "properties": {
                    "name": mname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Java", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Java", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_ruby_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Ruby file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('#')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip inline comments
        content_clean = re.sub(r'#.*$', '', content, flags=re.MULTILINE)
        # Strip =begin/=end blocks
        content_clean = re.sub(r'^=begin.*?^=end', ' ', content_clean, flags=re.MULTILINE | re.DOTALL)

        # require / require_relative
        imports = re.findall(r'require(?:_relative)?\s+[\'"]([^\'"]+)[\'"]', content)

        # class / module definitions
        class_pattern = re.compile(
            r'^(?:class|module)\s+([\w:]+)(?:\s*<\s*[\w:]+)?\s*$',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            name = m.group(1).split('::')[-1]  # unqualified name
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[name] = start_line

        # methods: def name or def self.name
        func_pattern = re.compile(
            r'^[ \t]*def\s+(?:self\.)?([\w?!]+)\s*(?:\(([^)]*)\))?',
            re.MULTILINE
        )

        # Module summary
        file_comment = ''
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('#') and not s.startswith('#!'):
                file_comment = s.lstrip('#').strip()
                break
        summary_parts = [f"Ruby module: {relative_path}"]
        if file_comment:
            summary_parts.append(file_comment)
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if ', 'unless ', 'while ', 'until ', 'case ', 'rescue ']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Ruby", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for cname, start_line in class_info.items():
            _class_end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 50)
            class_lines = source_lines[max(0, start_line - 1):_class_end_line]
            class_body = '\n'.join(class_lines)
            methods = [m.group(1) for m in func_pattern.finditer(content_clean)]
            signature = f"class {cname}"
            embedding = embed_class(signature, class_body, methods=methods[:10], language="ruby")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname, "full_name": f"{file_path.stem}.{cname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['classes'] += 1

        for m in func_pattern.finditer(content_clean):
            fname = m.group(1)
            args_str = m.group(2) or ''
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 30)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            enclosing = next(
                (c for c, cl in sorted(class_info.items(), key=lambda x: x[1], reverse=True)
                 if cl <= start_line), file_path.stem
            )
            full_name = f"{enclosing}.{fname}"
            signature = f"def {fname}({args_str})"
            embedding = embed_function(signature, body, language="ruby")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Ruby", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Ruby", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_shell_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Shell script using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('#')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'#.*$', '', content, flags=re.MULTILINE)

        # source / . file
        imports = re.findall(r'(?:source|\.)\s+([\w./${}/_-]+)', content)

        # Functions: name() { or function name {
        func_pattern = re.compile(
            r'^[ \t]*(?:function\s+)?([\w:.-]+)\s*\(\s*\)\s*\{|^[ \t]*function\s+([\w:.-]+)\s*\{',
            re.MULTILINE
        )

        # Module summary from top comment
        file_comment = ''
        for line in source_lines[:20]:
            s = line.strip()
            if s.startswith('#') and not s.startswith('#!') and s.lstrip('#').strip():
                file_comment = s.lstrip('#').strip()
                break
        summary_parts = [f"Shell script: {relative_path}"]
        if file_comment:
            summary_parts.append(file_comment)
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if [', 'if [[', 'while ', 'for ', 'case ']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Shell", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for m in func_pattern.finditer(content_clean):
            fname = m.group(1) or m.group(2)
            if not fname:
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 30)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{fname}"
            signature = f"{fname}()"
            embedding = embed_function(signature, body, language="python")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)
            stats['functions'] += 1

        # Cross-language interactions (shell: gate on curl/wget presence in content)
        ix = _extract_external_calls(content_clean, imports + ["curl", "wget"], "shell", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Shell", module_uuid, file_path_rel=relative_path)

        return stats

    def _analyze_python_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a single Python file and extract entities."""

        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        # Read file
        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')

        # Parse AST
        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError as e:
            print(f"⚠️  Syntax error in {file_path.relative_to(repo_root)}: {e}")
            return stats

        # Calculate file metrics
        loc = len([line for line in source_lines if line.strip() and not line.strip().startswith('#')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()

        # Check if file already analyzed (by hash)
        relative_path = file_path.relative_to(repo_root).as_posix()
        existing_module = self._get_existing_module(relative_path, file_hash)

        if existing_module:
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Extract imports
        imports = self._extract_imports(tree)

        # Generate module summary (first docstring or file description)
        module_summary = self._generate_module_summary(tree, source_lines, relative_path)

        # Create/update module
        module_uuid = self._create_or_update_module(
            path=relative_path,
            language="Python",
            loc=loc,
            complexity=self._calculate_complexity(tree),
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash,
            imports=imports,
            module_summary=module_summary
        )

        # Cache imports for cross-reference linking
        self.module_imports[relative_path] = imports

        stats['modules'] = 1

        # Track methods to avoid double-counting
        methods_seen = set()

        # Extract classes first and track their methods
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self._extract_class(node, module_uuid, file_path, repo_root, source_lines)
                stats['classes'] += 1
                # Track all methods in this class
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods_seen.add(id(item))

        # Extract only top-level functions (not methods)
        for node in tree.body:  # Only check top-level items
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if id(node) not in methods_seen:
                    self._extract_function(node, module_uuid, file_path, repo_root, source_lines)
                    stats['functions'] += 1

        # Cross-language interactions (Python: use raw content; _strip_triple_quoted handles docstrings)
        ix = _extract_external_calls(content, imports, "Python", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Python", module_uuid, file_path_rel=relative_path)

        return stats

    # ------------------------------------------------------------------
    # Cross-language interaction storage
    # ------------------------------------------------------------------

    def _store_interactions(
        self,
        interactions: List[Dict[str, str]],
        language: str,
        module_uuid: str,
        func_uuid: Optional[str] = None,
        file_path_rel: str = "",
    ) -> int:
        """Store a list of extracted interactions into CodeInteraction collection.

        Args:
            interactions: Output of _extract_external_calls().
            language:     Source language label (Python, Go, etc.)
            module_uuid:  UUID of the CodeModule containing the calls.
            func_uuid:    UUID of the specific CodeFunction (optional).
            file_path_rel: Repo-relative POSIX path of the source file
                the interactions were extracted from. Threaded through
                to ``_dedup_insert`` so the v0.2.16 path-aware UUID
                scheme produces stable IDs across re-runs.

        Returns:
            Number of interactions stored.
        """
        count = 0
        for ix in interactions:
            description = (
                f"{language}→{ix['interaction_type'].upper()} "
                f"{ix['protocol']} {ix['endpoint']} "
                f"[{ix['confidence']}]"
            )
            embedding = generate_embedding(description)
            insert_params: Dict[str, Any] = {
                "properties": {
                    "source_project": self.project_name,
                    "interaction_type": ix["interaction_type"],
                    "direction": ix.get("direction", "outbound"),
                    "protocol": ix["protocol"],
                    "endpoint": ix["endpoint"],
                    "raw_target": ix.get("raw_target", ""),
                    "confidence": ix.get("confidence", "high"),
                    "description": description,
                },
                "references": {"source_module": module_uuid},
            }
            if func_uuid:
                insert_params["references"]["source_function"] = func_uuid
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            try:
                self._dedup_insert(
                    self.interactions_collection, insert_params,
                    f"ix::{ix.get('source','')}::{ix.get('endpoint','')}",
                    file_path_rel=file_path_rel,
                )
                count += 1
            except Exception as exc:
                # Non-fatal — log and continue
                pass
        return count

    def _get_existing_module(self, path: str, file_hash: str) -> Optional[str]:
        """Check if module already exists with same hash.

        Returns the UUID if a module with matching path and hash exists,
        so it can be skipped during incremental analysis.
        """
        try:
            result = self.modules_collection.query.fetch_objects(
                filters=Filter.by_property("path").equal(path) &
                        Filter.by_property("file_hash").equal(file_hash),
                limit=1
            )
            if result.objects:
                return result.objects[0].uuid
            return None
        except Exception:
            return None

    def _extract_imports(self, tree: ast.AST) -> List[str]:
        """Extract import statements from AST."""
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        return imports

    def _generate_module_summary(self, tree: ast.AST, source_lines: List[str], path: str) -> str:
        """Generate a summary of the module for embedding."""
        # Try to get module docstring
        docstring = ast.get_docstring(tree)
        if docstring:
            return f"Module: {path}\n{docstring}"

        # Otherwise create summary from file structure
        classes = []
        functions = []

        for node in ast.walk(tree):
            try:
                if isinstance(node, ast.ClassDef):
                    classes.append(node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append(node.name)
            except (AttributeError, TypeError):
                # Skip nodes that don't have expected attributes
                continue

        summary_parts = [f"Module: {path}"]
        if classes:
            summary_parts.append(f"Classes: {', '.join(classes[:5])}")  # Limit to first 5
        if functions:
            summary_parts.append(f"Functions: {', '.join(functions[:5])}")  # Limit to first 5

        return "\n".join(summary_parts)

    def _calculate_complexity(self, tree: ast.AST) -> float:
        """Calculate cyclomatic complexity (simplified)."""
        complexity = 1  # Base complexity

        for node in ast.walk(tree):
            # Decision points
            if isinstance(node, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(node, ast.BoolOp):
                complexity += len(node.values) - 1

        return float(complexity)

    def _create_or_update_module(self, path: str, language: str, loc: int,
                                 complexity: float, last_modified: datetime,
                                 file_hash: str, imports: List[str], module_summary: str) -> str:
        """Create or update module in Weaviate.

        Note: ``path`` is the repo-relative POSIX path of the source file
        and IS the same value we pass to ``_dedup_insert`` as
        ``file_path_rel``. The module UUID is therefore keyed on
        ``(project, path, "module::"+path)``; the redundant ``path`` in
        both positions is intentional — the file_path_rel slot
        disambiguates files, the identity_key slot disambiguates
        entity-types within the same file (module vs class vs function
        with the same module-stem name).
        """

        # v0.2.18 (Plan C): normalise the language label to the canonical
        # lowercase ID so the prune filter can match it deterministically.
        # The caller-supplied `language` may be a display string ("Python",
        # "C++", "C#", etc.); `_canonical_lang_id` collapses both forms.
        # Fall back to `self._current_language` (dispatcher-set) so direct
        # callers without a `language=` argument still get the right value.
        canonical_lang = _canonical_lang_id(language) or self._current_language or ""

        # v0.2.47 (extras): source-root tag goes onto both update + insert
        # paths so re-analyzes of pre-v0.2.47 rows backfill the property
        # idempotently. Empty value is a no-op (same pattern as language).
        current_source = getattr(self, "_current_source", "")

        # Check if exists
        if path in self.module_cache:
            # Update existing
            update_props = {
                "module_summary": module_summary,
                "loc": loc,
                "complexity": complexity,
                "last_modified": last_modified.isoformat(),
                "file_hash": file_hash,
                "import_names": imports,
                # Plan C: backfill `language` on update so pre-migration
                # rows get the canonical value the next time we touch
                # them, without needing a separate batch backfill pass.
                "language": canonical_lang,
            }
            if current_source:
                update_props["project_source"] = current_source
            self.modules_collection.data.update(
                uuid=self.module_cache[path],
                properties=update_props,
            )
            # Still record as visited so --prune-stale doesn't delete it.
            if self._track_visited:
                self.visited_uuids.add((self.modules_collection.name, self.module_cache[path]))
            return self.module_cache[path]

        # Create new - generate embedding from module_summary
        embedding = embed_module(module_summary)

        insert_params = {
            "properties": {
                "path": path,
                "language": canonical_lang,
                "module_summary": module_summary,
                "loc": loc,
                "complexity": complexity,
                "project": self.project_name,
                "last_modified": last_modified.isoformat(),
                "file_hash": file_hash,
                "import_names": imports,
            }
        }
        if current_source:
            insert_params["properties"]["project_source"] = current_source

        # Add vector if embedding generation succeeded
        if embedding:
            insert_params["vector"] = _shape_for_insert(embedding)

        uuid = self._dedup_insert(
            self.modules_collection, insert_params, f"module::{path}",
            file_path_rel=path,
        )

        self.module_cache[path] = uuid
        return uuid

    def _extract_function_calls(self, node: ast.AST) -> List[str]:
        """Extract names of functions/methods called within an AST node.

        Returns list of call target names (e.g. 'func_name', 'ClassName.method').
        Only extracts simple Name and Attribute calls, skipping builtins.
        """
        _BUILTINS = {
            'print', 'len', 'range', 'enumerate', 'zip', 'map', 'filter',
            'sorted', 'reversed', 'list', 'dict', 'set', 'tuple', 'str',
            'int', 'float', 'bool', 'bytes', 'type', 'isinstance',
            'issubclass', 'hasattr', 'getattr', 'setattr', 'delattr',
            'super', 'property', 'staticmethod', 'classmethod',
            'open', 'iter', 'next', 'id', 'hash', 'repr', 'abs',
            'min', 'max', 'sum', 'any', 'all', 'ord', 'chr', 'hex',
            'vars', 'dir', 'format', 'input', 'round',
        }
        calls: List[str] = []
        seen: Set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                # e.g. self.method() -> 'method', obj.func() -> 'func'
                name = func.attr
            if name and name not in _BUILTINS and name not in seen:
                seen.add(name)
                calls.append(name)
        return calls

    def _populate_caches_from_weaviate(self):
        """Load all existing objects into caches from Weaviate.

        This allows cross-reference creation even when no files were re-analyzed
        (e.g., all skipped as unchanged in incremental mode).
        """
        if not self.client:
            return

        print("   Loading existing entities from Weaviate (merging with analysis cache)...")

        # Load modules (including import_names for cross-ref linking)
        try:
            for obj in self.modules_collection.iterator():
                path = obj.properties.get("path", "")
                if path:
                    self.module_cache[path] = str(obj.uuid)
                    # Populate module_imports from stored import_names
                    import_names = obj.properties.get("import_names")
                    if import_names:
                        self.module_imports[path] = import_names
        except Exception as e:
            print(f"   ⚠️  Failed to load modules: {e}")

        # Load classes
        try:
            for obj in self.classes_collection.iterator():
                full_name = obj.properties.get("full_name", "")
                if full_name:
                    self.class_cache[full_name] = str(obj.uuid)
        except Exception as e:
            print(f"   ⚠️  Failed to load classes: {e}")

        # Load functions
        try:
            for obj in self.functions_collection.iterator():
                full_name = obj.properties.get("full_name", "")
                if full_name:
                    self.function_cache[full_name] = str(obj.uuid)
        except Exception as e:
            print(f"   ⚠️  Failed to load functions: {e}")

        print(f"   Loaded {len(self.module_cache)} modules, {len(self.class_cache)} classes, {len(self.function_cache)} functions")

    def create_cross_references(self) -> Dict[str, int]:
        """Post-processing pass: create cross-references between Weaviate objects.

        Uses the caches (module_cache, class_cache, function_cache) populated
        during analysis to resolve string names to UUIDs and create references:
        - CodeFunction.calls -> CodeFunction (by matching call names to full_names)
        - CodeClass.extends -> CodeClass (by matching base class names)
        - CodeModule.imports -> CodeModule (by matching import names to paths)

        Returns dict with counts of references created per type.
        """
        stats = {'calls': 0, 'extends': 0, 'imports': 0}

        if not self.client:
            print("⚠️  Not connected to Weaviate, skipping cross-references")
            return stats

        print("\n🔗 Creating cross-references...")

        # If caches are empty (all files skipped), populate from Weaviate
        # Always populate caches from Weaviate to ensure all entities are available
        # for cross-reference resolution (not just the ones analyzed this run)
        self._populate_caches_from_weaviate()

        if not self.function_cache and not self.class_cache and not self.module_cache:
            print("   ⚠️  No entities found, skipping cross-references")
            return stats

        # Build reverse lookups for matching
        # function name (short) -> list of full_names that end with that name
        func_name_to_full: Dict[str, List[str]] = {}
        for full_name in self.function_cache:
            short_name = full_name.rsplit(".", 1)[-1]
            func_name_to_full.setdefault(short_name, []).append(full_name)

        # class name (short) -> list of full_names
        class_name_to_full: Dict[str, List[str]] = {}
        for full_name in self.class_cache:
            short_name = full_name.rsplit(".", 1)[-1]
            class_name_to_full.setdefault(short_name, []).append(full_name)

        # import module name -> module path (match last component of path)
        # e.g. import "foo.bar" matches path "src/foo/bar.py" or module "bar"
        module_name_to_path: Dict[str, List[str]] = {}
        for path in self.module_cache:
            # "src/foo/bar.py" -> stem "bar"
            stem = Path(path).stem
            module_name_to_path.setdefault(stem, []).append(path)
            # Also index dotted path: "src/foo/bar.py" -> "foo.bar"
            parts = Path(path).with_suffix("").parts
            if len(parts) > 1:
                dotted = ".".join(parts[-2:])
                module_name_to_path.setdefault(dotted, []).append(path)

        # --- 1. Function calls ---
        print("   Linking function calls...")
        for full_name, func_uuid in self.function_cache.items():
            # Fetch the function to get its body and extract calls
            try:
                resp = self.functions_collection.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(full_name),
                    limit=1,
                )
                if not resp.objects:
                    continue
                body = resp.objects[0].properties.get("function_body", "")
                if not body:
                    continue

                # Parse body to extract calls
                try:
                    tree = ast.parse(body)
                except SyntaxError:
                    continue

                call_names = self._extract_function_calls(tree)
                refs_to_add = []
                for call_name in call_names:
                    # Try exact full_name match first
                    if call_name in self.function_cache:
                        refs_to_add.append(self.function_cache[call_name])
                        continue
                    # Try short name match (prefer same module)
                    candidates = func_name_to_full.get(call_name, [])
                    if len(candidates) == 1:
                        refs_to_add.append(self.function_cache[candidates[0]])
                    elif len(candidates) > 1:
                        # Prefer same module prefix
                        module_prefix = full_name.rsplit(".", 1)[0] if "." in full_name else ""
                        same_module = [c for c in candidates if c.startswith(module_prefix + ".")]
                        if same_module:
                            refs_to_add.append(self.function_cache[same_module[0]])
                        else:
                            refs_to_add.append(self.function_cache[candidates[0]])

                # Store call_names as text array for callers queries
                if call_names:
                    try:
                        self.functions_collection.data.update(
                            uuid=func_uuid,
                            properties={"call_names": list(call_names)},
                        )
                    except Exception:
                        pass

                if refs_to_add:
                    try:
                        for ref_uuid in refs_to_add:
                            self.functions_collection.data.reference_add(
                                from_uuid=func_uuid,
                                from_property="calls",
                                to=ref_uuid,
                            )
                            stats['calls'] += 1
                    except Exception as e:
                        logger.debug(f"Failed to add call refs for {full_name}: {e}")

            except Exception as e:
                logger.debug(f"Error processing calls for {full_name}: {e}")

        # --- 2. Class extends ---
        print("   Linking class inheritance...")
        for full_name, class_uuid in self.class_cache.items():
            try:
                resp = self.classes_collection.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(full_name),
                    limit=1,
                )
                if not resp.objects:
                    continue
                props = resp.objects[0].properties
                signature = props.get("signature", "")
                # Extract base class names from signature: "class Foo(Bar, Baz)"
                base_match = re.search(r'\(([^)]+)\)', signature)
                if not base_match:
                    continue
                base_names = [b.strip() for b in base_match.group(1).split(",")]

                for base_name in base_names:
                    # Skip common non-project bases (builtins, stdlib, popular libs)
                    if base_name in ('object', 'Exception', 'BaseException',
                                     'ABC', 'Protocol', 'TypedDict', 'Enum',
                                     'IntEnum', 'StrEnum', 'BaseModel',
                                     'unittest.TestCase', 'TestCase',
                                     'str', 'int', 'float', 'bytes', 'dict',
                                     'list', 'tuple', 'set', 'frozenset',
                                     'type', 'Generic', 'NamedTuple',
                                     'Thread', 'Process', 'Handler',
                                     'logging.Handler'):
                        continue
                    # Try exact match
                    if base_name in self.class_cache:
                        ref_uuid = self.class_cache[base_name]
                    else:
                        # Try short name
                        candidates = class_name_to_full.get(base_name, [])
                        if not candidates:
                            continue
                        ref_uuid = self.class_cache[candidates[0]]

                    try:
                        self.classes_collection.data.reference_add(
                            from_uuid=class_uuid,
                            from_property="extends",
                            to=ref_uuid,
                        )
                        stats['extends'] += 1
                    except Exception as e:
                        logger.debug(f"Failed to add extends ref {full_name}->{base_name}: {e}")

            except Exception as e:
                logger.debug(f"Error processing extends for {full_name}: {e}")

        # --- 3. Module imports ---
        print("   Linking module imports...")
        for mod_path, import_names in self.module_imports.items():
            mod_uuid = self.module_cache.get(mod_path)
            if not mod_uuid or not import_names:
                continue
            for imp_name in import_names:
                # Try matching import name to a module in cache
                # "os.path" -> try "path", "os.path", "os"
                target_path = None
                # Direct stem match: import "bar" -> "bar.py"
                candidates = module_name_to_path.get(imp_name, [])
                if not candidates:
                    # Try last component: "foo.bar" -> "bar"
                    last = imp_name.rsplit(".", 1)[-1]
                    candidates = module_name_to_path.get(last, [])
                if candidates:
                    target_path = candidates[0]

                if target_path and target_path != mod_path:
                    target_uuid = self.module_cache.get(target_path)
                    if target_uuid:
                        try:
                            self.modules_collection.data.reference_add(
                                from_uuid=mod_uuid,
                                from_property="imports",
                                to=target_uuid,
                            )
                            stats['imports'] += 1
                        except Exception as e:
                            logger.debug(f"Failed to add import ref {mod_path}->{target_path}: {e}")

        print(f"   Done: {stats['calls']} call refs, {stats['extends']} extends refs, {stats['imports']} import refs")
        return stats

    def _extract_annotation_type_names(self, annotation: Optional[ast.expr]) -> List[str]:
        """Recursively extract simple type names from an AST annotation node.

        Handles ast.Name ('MyClass'), ast.Attribute ('module.Type'),
        ast.Subscript ('List[X]', 'Optional[X]'), and ast.BinOp ('X | Y').
        Returns a deduplicated list of non-builtin type name strings.
        """
        if annotation is None:
            return []

        _BUILTINS = {
            'str', 'int', 'float', 'bool', 'bytes', 'None', 'NoneType',
            'list', 'dict', 'set', 'tuple', 'frozenset',
            'List', 'Dict', 'Set', 'Tuple', 'FrozenSet',
            'Optional', 'Union', 'Any', 'Callable', 'Type',
            'Sequence', 'Iterable', 'Iterator', 'Generator',
            'Awaitable', 'Coroutine', 'AsyncIterator', 'AsyncGenerator',
            'ClassVar', 'Final', 'Literal', 'Annotated', 'TypeVar',
        }

        names: List[str] = []

        def _walk(node: ast.expr) -> None:
            if isinstance(node, ast.Name):
                if node.id not in _BUILTINS:
                    names.append(node.id)
            elif isinstance(node, ast.Attribute):
                # e.g. 'module.Type' — take the leaf attribute name
                if node.attr not in _BUILTINS:
                    names.append(node.attr)
            elif isinstance(node, ast.Subscript):
                # e.g. List[MyClass] — recurse into the slice only (skip container name)
                _walk(node.slice)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                # PEP 604 union: X | Y
                _walk(node.left)
                _walk(node.right)
            elif isinstance(node, ast.Tuple):
                for elt in node.elts:
                    _walk(elt)

        _walk(annotation)

        # Deduplicate while preserving order
        seen: Set[str] = set()
        result: List[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result

    def _extract_field_types(self, node: ast.ClassDef) -> List[str]:
        """Extract annotated field declarations from a class as 'field_name:TypeName' strings.

        Covers:
        - Class-level annotated attributes: ``x: MyType``
        - Annotated assignments in __init__: ``self.x: MyType = ...``
        """
        field_pairs: List[str] = []
        seen_fields: Set[str] = set()

        def _record(field_name: str, annotation: ast.expr) -> None:
            type_names = self._extract_annotation_type_names(annotation)
            for type_name in type_names:
                pair = f"{field_name}:{type_name}"
                if pair not in seen_fields:
                    seen_fields.add(pair)
                    field_pairs.append(pair)

        # Class-level annotated attributes
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                _record(item.target.id, item.annotation)

        # Annotated assignments inside __init__
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == '__init__':
                for stmt in ast.walk(item):
                    if isinstance(stmt, ast.AnnAssign):
                        target = stmt.target
                        # self.x: Type
                        if (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == 'self'):
                            _record(target.attr, stmt.annotation)
                break

        return field_pairs

    def _extract_class(self, node: ast.ClassDef, module_uuid: str,
                      file_path: Path, repo_root: Path, source_lines: List[str]):
        """Extract class definition."""

        # Cross-OS UUID stability (v0.2.16 — bug 0.7): POSIX-normalize
        # the repo-relative path before threading it to _dedup_insert.
        # Windows backslashes would otherwise produce different UUIDs
        # than the same file analyzed on Linux/macOS.
        relative_path = file_path.relative_to(repo_root).as_posix()

        # Get methods
        methods = [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]

        # Get docstring
        doc = ast.get_docstring(node) or ""

        # Get base classes
        base_names = [self._get_name(base) for base in node.bases]

        # Extract full class body for embedding
        class_body = self._extract_source_code(node, source_lines)

        # Get signature (class definition line only)
        signature = f"class {node.name}"
        if node.bases:
            signature += f"({', '.join(base_names)})"

        # Create class - smart truncation for embedding
        # class_body is full source (includes class line + docstring + body)
        embedding = embed_class(signature, class_body, methods=methods, language="python")

        # Extract SCG-style composition edges
        field_types = self._extract_field_types(node)
        # composes = unique class names that appear as field types
        composes: List[str] = []
        seen_composes: Set[str] = set()
        for pair in field_types:
            type_name = pair.split(':', 1)[1] if ':' in pair else ''
            if type_name and type_name not in seen_composes:
                seen_composes.add(type_name)
                composes.append(type_name)

        insert_params = {
            "properties": {
                "name": node.name,
                "full_name": f"{file_path.stem}.{node.name}",
                "class_body": class_body,
                "methods": methods,
                "signature": signature,
                "doc": doc,
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "project": self.project_name,
                "field_types": field_types,
                "composes": composes,
            },
            "references": {
                "module": module_uuid,
            }
        }

        # Add vector if embedding generation succeeded
        if embedding:
            insert_params["vector"] = _shape_for_insert(embedding)

        # v0.2.16 (bug 0.8): capture the return value into class_uuid.
        # Pre-v0.2.16 the return was discarded and the next line referenced
        # `class_uuid` from nowhere — NameError on every Python file
        # containing a class. The outer try/except in analyze_repository
        # was swallowing the exception and counting it as files_skipped,
        # so the bug was invisible to the exit-code path. Mirrors the
        # `func_uuid = self._dedup_insert(...)` pattern used in
        # `_extract_function` below.
        class_uuid = self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)

        self.class_cache[f"{file_path.stem}.{node.name}"] = class_uuid

        # Extract methods
        for method in node.body:
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._extract_function(method, module_uuid, file_path, repo_root, source_lines,
                                     parent_class=node.name)

    def _extract_function(self, node: ast.FunctionDef, module_uuid: str,
                         file_path: Path, repo_root: Path, source_lines: List[str],
                         parent_class: Optional[str] = None):
        """Extract function definition."""

        # Cross-OS UUID stability (v0.2.16 — bug 0.7): POSIX-normalize.
        # See _extract_class for the same rationale.
        relative_path = file_path.relative_to(repo_root).as_posix()

        # Get signature
        args = [arg.arg for arg in node.args.args]
        signature = f"{node.name}({', '.join(args)})"

        # Get docstring
        doc = ast.get_docstring(node) or ""

        # Extract full function body for embedding
        function_body = self._extract_source_code(node, source_lines)

        # Determine full name
        if parent_class:
            full_name = f"{file_path.stem}.{parent_class}.{node.name}"
        else:
            full_name = f"{file_path.stem}.{node.name}"

        # Extract SCG-style type_uses from argument annotations and return annotation
        type_uses: List[str] = []
        seen_type_uses: Set[str] = set()

        def _add_type_names(annotation: Optional[ast.expr]) -> None:
            for t in self._extract_annotation_type_names(annotation):
                if t not in seen_type_uses:
                    seen_type_uses.add(t)
                    type_uses.append(t)

        for arg in node.args.args:
            _add_type_names(arg.annotation)
        for arg in node.args.posonlyargs:
            _add_type_names(arg.annotation)
        for arg in node.args.kwonlyargs:
            _add_type_names(arg.annotation)
        if node.args.vararg:
            _add_type_names(node.args.vararg.annotation)
        if node.args.kwarg:
            _add_type_names(node.args.kwarg.annotation)
        _add_type_names(node.returns)

        # CFG/PDG data (optional, populated by analyze_repository's pre-pass)
        cfg_pdg_store = getattr(self, '_cfg_pdg_data', {})
        cfg_pdg = cfg_pdg_store.get(full_name, {})
        cfg_summary = cfg_pdg.get("cfg_summary", "")
        data_flow_vars = cfg_pdg.get("data_flow_vars", [])

        # Create function - smart truncation for embedding
        # function_body is full source (includes def line + docstring + body)
        embedding = embed_function(signature, function_body, language="python")

        insert_params = {
            "properties": {
                "name": node.name,
                "full_name": full_name,
                "function_body": function_body,
                "signature": signature,
                "doc": doc,
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "project": self.project_name,
                "type_uses": type_uses,
                "cfg_summary": cfg_summary,
                "data_flow_vars": data_flow_vars,
            },
            "references": {
                "module": module_uuid,
            }
        }

        # Add vector if embedding generation succeeded
        if embedding:
            insert_params["vector"] = _shape_for_insert(embedding)

        func_uuid = self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]), file_path_rel=relative_path)

        self.function_cache[full_name] = func_uuid

    def _get_name(self, node: ast.AST) -> str:
        """Get name from AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_name(node.value)}.{node.attr}"
        else:
            return ""

    def _extract_source_code(self, node: ast.AST, source_lines: List[str]) -> str:
        """Extract source code for an AST node using line numbers."""
        if not hasattr(node, 'lineno') or not hasattr(node, 'end_lineno'):
            return ""

        start = node.lineno - 1  # Convert to 0-based index
        end = node.end_lineno  # end_lineno is inclusive, so we don't subtract 1

        if start < 0 or end > len(source_lines):
            return ""

        return '\n'.join(source_lines[start:end])

    def _extract_cfg_pdg(
        self,
        repo_path: Path,
        language: str,
        extract_cfg: bool = False,
        extract_pdg: bool = False,
    ) -> Dict[str, Any]:
        """Run Joern to extract CFG/PDG data for functions in a repository.

        Both flags are opt-in and require joern in PATH.
        Returns empty dict on any error (non-blocking).

        Args:
            repo_path: Path to repository root.
            language: Language hint for Joern ('python', 'cpp', etc.).
            extract_cfg: Whether to extract CFG summaries.
            extract_pdg: Whether to extract PDG data-flow variable lists.

        Returns:
            Dict mapping full_name -> {"cfg_summary": str, "data_flow_vars": list[str]}.
            Empty dict if joern unavailable or any error occurs.
        """
        if not extract_cfg and not extract_pdg:
            return {}

        if not shutil.which("joern"):
            logger.warning("joern not found in PATH; skipping CFG/PDG extraction")
            return {}

        # Joern script: export CPG, collect CFG edge counts and PDG variable names per method
        joern_script = """
importCode(inputPath="{repo_path}", projectName="tmp_cgraph")
val methods = cpg.method.l
val result = methods.map {{ m =>
  val cfg_branches = m.cfgNode.isControlStructure.l.size
  val cfg_loops = m.cfgNode.isControlStructure.filter(_.controlStructureType.matches("FOR|WHILE|DO")).l.size
  val cfg_max_depth = m.depth
  val pdg_vars = m.local.name.l.distinct
  val entry = ujson.Obj(
    "full_name" -> m.fullName,
    "cfg_summary" -> s"branches:${{cfg_branches}} loops:${{cfg_loops}} max_depth:${{cfg_max_depth}}",
    "data_flow_vars" -> ujson.Arr(pdg_vars.map(ujson.Str(_)): _*)
  )
  entry
}}
println(upickle.default.write(result))
exit
""".strip().format(repo_path=str(repo_path))

        result: Dict[str, Any] = {}
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.sc', delete=False, prefix='joern_cgraph_'
            ) as tmp:
                tmp.write(joern_script)
                tmp_path = tmp.name

            proc = subprocess.run(
                ["joern", "--script", tmp_path],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if proc.returncode != 0:
                logger.warning(f"joern exited with code {proc.returncode}: {proc.stderr[:500]}")
                return {}

            # Parse JSON array from stdout — find the first '[' to skip Joern banner lines
            stdout = proc.stdout
            bracket_idx = stdout.find('[')
            if bracket_idx == -1:
                logger.warning("joern output did not contain JSON array")
                return {}

            entries = json.loads(stdout[bracket_idx:])
            for entry in entries:
                full_name = entry.get("full_name", "")
                if not full_name:
                    continue
                result[full_name] = {
                    "cfg_summary": entry.get("cfg_summary", "") if extract_cfg else "",
                    "data_flow_vars": entry.get("data_flow_vars", []) if extract_pdg else [],
                }

        except subprocess.TimeoutExpired:
            logger.warning("joern timed out after 120s; skipping CFG/PDG extraction")
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse joern JSON output: {e}")
        except Exception as e:
            logger.warning(f"CFG/PDG extraction error: {e}")
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

        return result

    def _analyze_svelte_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Svelte component file (V52-O.11.B, v0.2.53 Track E).

        Extracts top-level functions, exports, arrow-export consts, and
        reactive declarations from <script> and <script context="module">
        blocks. The component name is taken from the file stem (Svelte
        convention; the file IS the component) and recorded as the
        module summary alongside the imports list.

        Imports come from ES module syntax in any <script> block. Class
        declarations are extracted too if present (Svelte allows
        utility classes inside a component's script block).
        """
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip()])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        component_name = file_path.stem

        # --- Imports across all <script> blocks ---
        imports: List[str] = []
        for block_body, _is_module, _start in _extract_svelte_script_blocks(content):
            for m in re.finditer(
                r"""import\s+(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?)\s+from\s+)?['"]([^'"]+)['"]""",
                block_body,
            ):
                imports.append(m.group(1))

        # --- Module summary ---
        # Component name + first non-empty HTML comment from the template,
        # if present (the Svelte convention is `<!-- ... -->` at file top).
        leading_doc = ''
        m_html_comment = re.search(r"<!--\s*(.*?)\s*-->", content, re.DOTALL)
        if m_html_comment:
            leading_doc = m_html_comment.group(1).strip().split('\n')[0][:200]
        summary_parts = [f"Svelte component: {component_name}"]
        if leading_doc:
            summary_parts.append(leading_doc)
        module_summary = '\n'.join(summary_parts)

        # Cyclomatic-ish complexity: count common branches/loops across
        # script + template (matches the JS analyser's heuristic). Cheap
        # over-approximation — exact CFG analysis would need a Svelte
        # AST parser which we don't bundle.
        complexity = float(
            1 + sum(content.count(kw) for kw in [
                'if (', 'if(', '{#if ', '{:else if ', '{#each ', '{#await ',
                '? ', 'while (', 'while(', 'for (', 'for('
            ])
        )

        module_uuid = self._create_or_update_module(
            path=relative_path,
            language="Svelte",
            loc=loc,
            complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash,
            imports=imports,
            module_summary=module_summary,
        )
        stats['modules'] = 1

        # --- Functions / arrow exports / reactive decls ---
        for decl in _parse_svelte_functions(content):
            name: str = decl["name"]
            start_line = content[:decl["start_offset"]].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])

            kind = decl["kind"]
            is_async = bool(decl["is_async"])
            if kind == "reactive":
                signature = f"$: {name} = ..."
            elif kind == "arrow_export":
                signature = (
                    f"{'async ' if is_async else ''}const {name} = (...) => ..."
                )
            else:
                # function / export function
                prefix = "export " if kind == "export" else ""
                signature = (
                    f"{prefix}{'async ' if is_async else ''}function {name}()"
                )

            full_name = f"{component_name}.{name}"
            embedding = embed_function(signature, body, language="javascript")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": name,
                    "full_name": full_name,
                    "function_body": body,
                    "signature": signature,
                    "doc": "",
                    "start_line": start_line,
                    "end_line": end_line,
                    "is_async": is_async,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(
                self.functions_collection,
                insert_params,
                insert_params["properties"].get("full_name", insert_params["properties"]["name"]),
                file_path_rel=relative_path,
            )
            stats['functions'] += 1

        return stats

    def _analyze_powershell_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a PowerShell script file (V52-O.11.N, v0.2.53 Track E).

        Extracts function / filter declarations + their `param(...)`
        blocks. Imports are best-effort: `Import-Module Foo`,
        `. .\\Path\\Common.ps1` (dot-sourcing). External calls are
        gated on `Invoke-WebRequest` / `Invoke-RestMethod` presence
        in content (same gating pattern as the shell analyser).
        """
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('#')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = file_path.relative_to(repo_root).as_posix()

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        cleaned = _strip_powershell_comments(content)

        # --- Imports ---
        imports: List[str] = []
        # `Import-Module Foo` / `Import-Module -Name Foo`
        for m in re.finditer(
            r"\bImport-Module\b\s+(?:-Name\s+)?['\"]?([\w./-]+)['\"]?",
            cleaned,
            re.IGNORECASE,
        ):
            imports.append(m.group(1))
        # Dot-source: `. .\Common.ps1` / `. $PSScriptRoot\Common.ps1`
        for m in re.finditer(
            r"^\s*\.\s+([\w$.\\/-]+\.ps1)",
            cleaned,
            re.MULTILINE,
        ):
            imports.append(m.group(1))

        # --- Module summary ---
        # Look for a leading `<# .SYNOPSIS ... #>` block; fall back to
        # the first single-line comment.
        synopsis = ''
        m_syn = re.search(
            r"<#\s*\.SYNOPSIS\s+(.*?)\s*(?:\.[A-Z]+|\#>)",
            content,
            re.DOTALL,
        )
        if m_syn:
            synopsis = m_syn.group(1).strip().split('\n')[0][:200]
        else:
            for line in source_lines[:20]:
                s = line.strip()
                if (
                    s.startswith('#')
                    and not s.startswith('#!')
                    and not s.startswith('#region')
                    and s.lstrip('#').strip()
                ):
                    synopsis = s.lstrip('#').strip()
                    break
        summary_parts = [f"PowerShell script: {relative_path}"]
        if synopsis:
            summary_parts.append(synopsis)
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(
            cleaned.count(kw) for kw in [
                'if (', 'if(', 'elseif ', 'while (', 'while(',
                'for (', 'for(', 'foreach (', 'foreach(',
                'switch (', 'switch(',
            ]
        ))

        module_uuid = self._create_or_update_module(
            path=relative_path,
            language="PowerShell",
            loc=loc,
            complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash,
            imports=imports,
            module_summary=module_summary,
        )
        stats['modules'] = 1

        # --- Functions / filters ---
        for decl in _parse_powershell_functions(content):
            name: str = decl["name"]
            scope: Optional[str] = decl["scope"]
            kind: str = decl["kind"]
            params: List[str] = decl["params"]

            # Re-derive line numbers from the ORIGINAL content (the
            # parser used a comment-stripped copy, so its offsets are
            # not directly comparable to the original; re-search by
            # name to find the start line for the orchestrator's
            # entity insertion).
            #
            # We anchor on the `function NAME` / `filter NAME` pattern
            # at start-of-line in the original content. If multiple
            # functions share a name (e.g. accidental redefinition),
            # the first match wins — the orchestrator's `_dedup_insert`
            # dedup on `full_name` + `file_path_rel` then either
            # collapses them or surfaces the dedup conflict downstream.
            scope_prefix = f"{scope}:" if scope else ""
            anchor = re.compile(
                r"^\s*(?:function|filter)\s+"
                + re.escape(scope_prefix)
                + re.escape(name)
                + r"\b",
                re.MULTILINE | re.IGNORECASE,
            )
            anchor_match = anchor.search(content)
            if not anchor_match:
                continue
            start_line = content[:anchor_match.start()].count('\n') + 1
            end_line = _extract_balanced_block(source_lines, start_line)
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])

            param_sig = ", ".join(f"${p}" for p in params)
            signature = (
                f"{kind} {scope_prefix}{name}({param_sig})"
                if params
                else f"{kind} {scope_prefix}{name}"
            )
            full_name = f"{file_path.stem}.{name}"
            embedding = embed_function(signature, body, language="python")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": name,
                    "full_name": full_name,
                    "function_body": body,
                    "signature": signature,
                    "doc": "",
                    "start_line": start_line,
                    "end_line": end_line,
                    "is_async": False,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = _shape_for_insert(embedding)
            self._dedup_insert(
                self.functions_collection,
                insert_params,
                insert_params["properties"].get("full_name", insert_params["properties"]["name"]),
                file_path_rel=relative_path,
            )
            stats['functions'] += 1

        return stats

    def close(self):
        """Close Weaviate connection."""
        if self.client:
            self.client.close()


def _migrate_from_shared(project_name: str, named_vectors: bool = False) -> int:
    """Migrate objects from shared collections to per-project collections."""
    try:
        client = weaviate.connect_to_custom(
            http_host='localhost', http_port=8081, http_secure=False,
            grpc_host='localhost', grpc_port=50052, grpc_secure=False
        )
    except Exception as e:
        print(f"❌ Failed to connect to Weaviate: {e}", file=sys.stderr)
        return 1

    try:
        # Create per-project collections first
        analyzer = CodeGraphAnalyzer(project_name, named_vectors=named_vectors)
        analyzer.client = client
        analyzer.create_collections(force=False)

        for base in CODE_GRAPH_BASES:
            shared_name = base
            target_name = _collection_name(base, project_name)

            if not client.collections.exists(shared_name):
                print(f"⚠️  Shared collection {shared_name} does not exist, skipping")
                continue

            shared_coll = client.collections.get(shared_name)
            target_coll = client.collections.get(target_name)

            # Fetch all objects filtered by project
            count = 0
            for obj in shared_coll.iterator(
                return_properties=True,
            ):
                props = obj.properties
                if props.get("project") != project_name and props.get("source_project") != project_name:
                    continue

                # Insert into per-project collection (without references for now)
                try:
                    target_coll.data.insert(
                        properties=props,
                        vector=obj.vector.get("default") if obj.vector else None,
                    )
                    count += 1
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        logger.warning(f"Failed to migrate object: {e}")

            print(f"✅ Migrated {count} objects from {shared_name} → {target_name}")

        print(f"\n✅ Migration complete for project '{project_name}'")
        print("   Note: Cross-collection references (imports, calls, extends, etc.) are NOT migrated.")
        print("   Re-run full analysis to rebuild references in the new collections.")
        return 0
    finally:
        client.close()


def _is_under_temp_dir(p: Path) -> bool:
    """True if ``p`` resolves under the system temp dir (e.g. ``/tmp``).

    Indexing a transient location into a PERSISTENT per-project code-graph
    collection is almost always accidental. Agent git-worktrees created under
    the temp dir (``/tmp/vco-track-*``) once leaked ~34k throwaway rows with
    paths like ``__tmp_vco-track-D_install._start_services`` into the shared
    CodeFunction collection (WS-4 Finding 3). The ``worktrees`` ignore-dir only
    catches ``.claude/worktrees/`` during the walk — it does NOT catch a temp
    path passed AS the analysis root or an ``--extra-path``. This guard does.
    """
    try:
        tmp = Path(tempfile.gettempdir()).resolve()
        rp = p.resolve()
        return rp == tmp or tmp in rp.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _is_ephemeral_worktree_root(p: Path) -> bool:
    """True if ``p`` is a git LINKED WORKTREE living under the system temp dir.

    This is the precise WS-4 Finding 3 pollution shape: agent worktrees created
    by ``git worktree add /tmp/vco-track-*`` (a linked worktree has ``.git`` as a
    FILE — ``gitdir: …/worktrees/<name>`` — not a directory). Indexing such a
    throwaway checkout into the persistent per-project collection leaks
    duplicate symbols keyed on temp paths.

    Deliberately NARROW: a plain temp directory, a ``git init`` repo in temp
    (``.git`` is a dir), or a ``git clone`` into temp (``.git`` dir) are all
    legitimate analysis roots and are NOT skipped — only ephemeral linked
    worktrees under temp are. (Earlier the guard skipped ALL temp roots, which
    silently no-op'd legitimate temp analysis incl. CI fixtures.)
    """
    try:
        return _is_under_temp_dir(p) and (p / ".git").is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Analyze codebase and extract entities into Weaviate code graph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('repo_path', type=Path, help='Path to repository to analyze')
    # WS-4 Finding 3: by default refuse to index a root/extra-path that lives
    # under the system temp dir (agent worktrees at /tmp/vco-track-* polluted
    # the persistent collection). Override for the rare legit temp-dir analysis.
    parser.add_argument('--allow-temp-root', action='store_true',
                        help='Permit analyzing a repo_path / --extra-path under the '
                             'system temp dir (default: skip — avoids agent-worktree pollution).')
    parser.add_argument('--project', '-p', type=str, help='Project name (default: repo directory name)')
    # v0.2.21 Step 20: read the collection prefix from the running hub
    # rather than deriving it locally from --project or the CWD. When
    # this flag is set, the analyzer queries the resolver for
    # `code_graph_project` / `code_graph_collection_prefix` so its
    # output collections match exactly what consumers see via the
    # resolver. Soft-fail: if the resolver is unreachable, we fall
    # back to the existing --project / repo-dir-name logic with a
    # stderr warning. Mutually exclusive with --project: pass one or
    # the other, never both.
    parser.add_argument(
        '--from-resolver',
        action='store_true',
        help=(
            'Query the vct-hub resolver for code_graph_project / '
            'collection_prefix (uses the project at repo_path). Mutually '
            'exclusive with --project. Falls back to repo-dir-name if '
            'hub unreachable.'
        ),
    )
    parser.add_argument('--language', '-l', type=str,
                       choices=['python', 'lua', 'cpp', 'javascript', 'typescript',
                                'go', 'rust', 'java', 'ruby', 'shell', 'csharp', 'proto'],
                       default=None,
                       help='Language to analyze (default: all supported; language inferred from file extensions)')
    parser.add_argument('--incremental', '-i', action='store_true',
                       help='Only analyze changed files (requires git)')
    # v0.2.47 (extras): additional source roots to walk in the same pass
    # as `repo_path`. Repeatable. Each extra is indexed into the SAME
    # `<--project>_Code*` collections; the analyzer stamps `project_source`
    # on every emitted row so consumers can tell primary-repo rows from
    # extras-path rows. `visited_uuids` is the UNION across primary + all
    # extras so `--prune-stale` does NOT delete the other roots' UUIDs.
    # See knowledge/concepts/project-extra-codegraph-paths-2026-06-05.md.
    parser.add_argument('--extra-path', dest='extra_paths', action='append',
                       default=[], type=Path,
                       help='Additional source root to walk in the same pass. '
                            'Repeatable. All rows land in the SAME per-project '
                            'collections; `project_source` records provenance.')
    # v0.2.47 (extras): per-source-root incremental lower bound. With
    # `--incremental`, restricts the changed-files filter to
    # `git log <sha>..HEAD` instead of the default `HEAD~1..HEAD`. Each
    # source root that is a git repo uses its OWN diff range relative to
    # this SHA; non-git roots fall back to a full scan with a one-line
    # stderr notice.
    parser.add_argument('--since-commit', type=str, default=None,
                       help='With --incremental, restrict the diff to '
                            '<sha>..HEAD instead of HEAD~1..HEAD. Per source '
                            'root; non-git roots fall back to full scan.')
    # v0.2.66 (Bug 3): scope the analyze to EXACTLY one file. `repo_path`
    # stays the relativization root (collections key on repo-relative
    # paths); the file must live under it. The per-edit code-graph hook
    # passes the file the user just edited here instead of `--incremental`
    # (which re-churned every HEAD~1..HEAD file AND missed the actual,
    # still-uncommitted edit). Routes through the same per-file +
    # per-object hash skips, so an unchanged file writes 0 objects.
    # Mutually exclusive with --incremental, --extra-path, --prune-stale.
    parser.add_argument('--only-file', type=Path, default=None,
                       help='Analyze EXACTLY this one file (under repo_path) '
                            'instead of walking the tree. Used by the per-edit '
                            'hook. Mutually exclusive with --incremental, '
                            '--extra-path, --prune-stale.')
    # v0.2.66 (Bug 3, part b): canonical source root for worktree dedup. The
    # per-edit hook resolves the edited file's git MAIN repo root and passes
    # it here so a worktree edit stamps the SAME project_source / UUID seed
    # as a main-checkout edit (one canonical object, not a per-worktree dup).
    # Only meaningful with --only-file.
    parser.add_argument('--canonical-source', type=str, default=None,
                       help='With --only-file: stamp this canonical root as '
                            'project_source (and the UUID seed) instead of the '
                            'on-disk repo_path. Used by the per-edit hook to '
                            'dedup git-worktree edits onto the main-checkout '
                            'object.')
    parser.add_argument('--create-collections', action='store_true',
                       help='Create Weaviate collections before analysis')
    parser.add_argument('--force-recreate', action='store_true',
                       help='Delete and recreate collections (WARNING: deletes all data)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    # Joern CFG/PDG: auto-on when joern is installed (or VCT_JOERN_AVAILABLE=1).
    # Opt out per-run with --no-cfg/--no-pdg, or set VCT_JOERN_AVAILABLE=0.
    _joern_default = (
        os.environ.get("VCT_JOERN_AVAILABLE", "").strip() == "1"
        or shutil.which("joern") is not None
    )
    parser.add_argument('--cfg', dest='cfg', action='store_true', default=_joern_default,
                       help='Extract CFG summaries via Joern (default: on if joern is in PATH)')
    parser.add_argument('--no-cfg', dest='cfg', action='store_false',
                       help='Skip CFG extraction (faster; useful in CI)')
    parser.add_argument('--pdg', dest='pdg', action='store_true', default=_joern_default,
                       help='Extract PDG data-flow variables via Joern (default: on if joern is in PATH)')
    parser.add_argument('--no-pdg', dest='pdg', action='store_false',
                       help='Skip PDG extraction')
    parser.add_argument('--named-vectors', action='store_true',
                       default=True,
                       help='Create collections with named vector support (default: True)')
    parser.add_argument('--migrate-from-shared', action='store_true',
                       help='Migrate objects from shared collections to per-project collections')
    # v0.2.16 (1.4 / addendum H): visited-UUID tracking + post-run pruning of
    # orphan entries. Recommended for the launcher's "Re-analyze" path
    # (wizard checkbox: "Clean stale entries during re-analysis"). Default
    # off — opt-in to avoid surprising deletes on partial-language runs
    # (e.g. `--language python` would otherwise prune all non-Python objects).
    parser.add_argument('--prune-stale', action='store_true',
                       help='After analysis, delete code-graph objects this run '
                            'did not visit (cleans up entries for deleted files). '
                            'Tracks UUIDs as they are upserted and removes the '
                            'rest from each per-project collection. Plan C / '
                            'v0.2.18: when combined with --language=<lang>, the '
                            'prune is SCOPED to that language only — entries '
                            'from other languages are preserved.')
    # v0.2.18 (Plan C): structured per-file progress for the Re-analyze
    # button's Tauri modal. Emits one JSON object per analyzed file on
    # stdout: {"progress": 0.42, "message": "Analyzing foo.py", "file":
    # "foo.py", "lang": "python"}. The final report line is also JSON:
    # {"final": true, "files_analyzed": N, ...}. Off by default to keep
    # the human-readable output for direct CLI users; the hook + Tauri
    # command opt in explicitly.
    parser.add_argument('--json-progress', action='store_true',
                       help='Emit per-file progress + final report as JSON '
                            'lines on stdout (Tauri Re-analyze modal).')

    args = parser.parse_args()

    # v0.2.18 (Plan C) — --prune-stale + --language is now the CORRECT
    # combination, not a footgun. The prune pass filters by stored
    # `language` property so only rows from the analyzed language are
    # candidates for deletion. The pre-Plan-C warning has been removed.

    # Validate repo path
    repo_path = args.repo_path.resolve()
    if not repo_path.exists():
        print(f"❌ Repository path does not exist: {repo_path}", file=sys.stderr)
        return 1

    # WS-4 Finding 3: refuse to index an EPHEMERAL agent git-worktree under the
    # system temp dir (e.g. /tmp/vco-track-*) into the persistent per-project
    # collection — that throwaway checkout's paths were the source of the ~34k
    # __tmp_* garbage rows. Narrow on purpose: a plain temp dir / git-init repo
    # / clone in temp is a legitimate root and is NOT skipped.
    # Skip (exit 0 — a deliberate no-op, NOT an error the hook should surface).
    if _is_ephemeral_worktree_root(repo_path) and not args.allow_temp_root:
        print(
            f"⚠️  Skipping code-graph analysis: repo_path is an ephemeral git "
            f"worktree under the system temp dir ({repo_path}).", file=sys.stderr,
        )
        print(
            "    Indexing a transient agent worktree would pollute the persistent "
            "collection with throwaway paths (WS-4 Finding 3). "
            "Pass --allow-temp-root to override.", file=sys.stderr,
        )
        return 0

    # v0.2.21 Step 20 — analyzer harmonization. The collection prefix
    # the analyzer writes to MUST match what consumers see via the
    # resolver, otherwise hooks query the wrong collection on every
    # Edit. Three sources of truth, in priority order:
    #   1. --from-resolver: ask the hub for code_graph_project. The
    #      resolver consults the project's project_codegraph_bindings
    #      row (Step 19's startup backfill seeds it) and returns the
    #      canonical prefix.
    #   2. --project: explicit override (the launcher's existing call
    #      sites pass this).
    #   3. Repo dir name: legacy fallback, kept for direct CLI users.
    if args.from_resolver and args.project:
        print(
            "❌ --from-resolver and --project are mutually exclusive; pass one or the other",
            file=sys.stderr,
        )
        return 1

    # v0.2.66 (Bug 3): --only-file scopes the analyze to a single file and
    # never walks other roots or prunes. Combining it with the whole-tree
    # flags is a usage error — fail fast rather than silently ignore one.
    if args.only_file is not None:
        conflicting = []
        if args.incremental:
            conflicting.append("--incremental")
        if args.extra_paths:
            conflicting.append("--extra-path")
        if args.prune_stale:
            conflicting.append("--prune-stale")
        if conflicting:
            print(
                "❌ --only-file is mutually exclusive with "
                f"{', '.join(conflicting)} (single-file mode never walks "
                "other roots or prunes)",
                file=sys.stderr,
            )
            return 1
    elif args.canonical_source:
        # --canonical-source only has meaning in single-file mode (it stamps
        # the per-object project_source / UUID seed). Reject the lone flag so
        # a caller isn't surprised it was silently ignored.
        print(
            "❌ --canonical-source requires --only-file",
            file=sys.stderr,
        )
        return 1

    project_name: Optional[str] = None
    if args.from_resolver:
        try:
            # Lazy import — vco_lib.project_config requires `requests`
            # which is a runtime-only dep for the analyzer venv. If the
            # import fails, fall through to the dir-name path.
            from vco_lib.project_config import (
                resolve, HubUnreachable, ResolverError,
            )
            cfg = resolve(repo_path)
            # v0.2.23 field switch: `code_graph_collection_prefix` is the
            # canonical Weaviate prefix sourced from the launcher's
            # `project_codegraph_bindings.collection_prefix` row — the
            # single source of truth for the write target. The previously
            # used `code_graph_project` is a legacy alias for the slug;
            # the analyzer's `_sanitize_collection_prefix` re-canonicalised
            # it and produced a prefix that diverged from the binding row,
            # silently writing to zombie collections. Always prefer the
            # explicit prefix field here. See knowledge/concepts/
            # multi-codebase-code-graph-detection.md for the diagnosis.
            project_name = cfg.code_graph_collection_prefix
            if not project_name:
                # Resolver returned an empty prefix; fall back.
                print(
                    f"⚠️  resolver returned empty code_graph_collection_prefix "
                    f"for {repo_path}; falling back to repo dir name",
                    file=sys.stderr,
                )
        except (HubUnreachable, ResolverError) as e:
            print(
                f"⚠️  resolver unreachable ({type(e).__name__}: {e}); "
                f"falling back to repo dir name",
                file=sys.stderr,
            )
        except ImportError:
            print(
                "⚠️  vco_lib.project_config not importable; "
                "falling back to repo dir name (install resolver clients to use --from-resolver)",
                file=sys.stderr,
            )

    if not project_name:
        # v0.2.37 (Gap 6e): honor $CODE_GRAPH_PROJECT env var BEFORE
        # falling back to the repo dir name. The launcher and hooks
        # already export this key (see `.claude/env` + `.claude/settings.json
        # ::env`); without this check, code-graph collections diverge from
        # the KG collection prefix on direct CLI invocations (the analyzer
        # would silently write to a Weaviate class whose name reflects the
        # cwd basename rather than the launcher-resolved project name).
        # Order: --from-resolver > --project > $CODE_GRAPH_PROJECT > repo_path.name.
        env_project = os.environ.get("CODE_GRAPH_PROJECT", "").strip()
        project_name = args.project or env_project or repo_path.name

    if args.verbose:
        print(f"📂 Repository: {repo_path}")
        print(f"📦 Project: {project_name}")
        print(f"📁 Collections: {_collection_name('Code*', project_name)}")
        print(f"🔄 Incremental: {args.incremental}")
        if args.named_vectors:
            print(f"📐 Named vectors: enabled")
        print()

    # Handle migration from shared collections
    if args.migrate_from_shared:
        return _migrate_from_shared(project_name, args.named_vectors)

    # v0.2.18: initialise the EmbeddingService BEFORE creating collections.
    # The vectorizer config + slot resolution both depend on it.
    #
    # Behaviour matrix:
    #   * code backend reachable (codeembed / ollama / openai) → proceed
    #   * NO embedding backend reachable → NoEmbeddingBackendError,
    #     write deferral + JSONL diagnostic, exit 0 (soft-fail per the
    #     KG-summary-no-backend pattern). The launcher / install.py
    #     surfaces UPDATE_DEFERRED.md to the user.
    #   * embedding service constructed, but code_backend_ready() is False
    #     (e.g. CodeEmbed container down, machine has only an ollama text
    #     model that can't serve code) → same soft-fail. Don't proceed:
    #     the embed_* helpers would just emit `None` per call and produce
    #     a code graph with no vectors at all.
    install_root = Path(os.environ.get("VCT_ORCHESTRATOR_ROOT", "")).resolve() if os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip() else repo_path
    embedding_service: Optional[EmbeddingService] = None
    try:
        embedding_service = EmbeddingService.for_project(install_root)
    except NoEmbeddingBackendError as e:
        _emit_code_graph_deferral_no_backend(install_root, e)
        print(f"⚠️  Code-graph analysis skipped: {e}", file=sys.stderr)
        print(
            "   See .claude/context/EMBEDDING_FAILURES.md + "
            "~/.claude/metrics/embedding_failures.jsonl",
            file=sys.stderr,
        )
        return 0

    if not embedding_service.code_backend_ready():
        _emit_code_graph_deferral_code_backend_down(install_root, embedding_service)
        print(
            f"⚠️  Code-graph analysis skipped: active code backend "
            f"(slot={embedding_service.code_vector_slot}, "
            f"model={embedding_service.code_model_id}) is not reachable.",
            file=sys.stderr,
        )
        print(
            "   Start CodeEmbed (`podman start vco_code_embed`) or Ollama "
            "with a code-capable model before re-running.",
            file=sys.stderr,
        )
        try:
            embedding_service.close()
        except Exception:
            pass
        return 0

    _set_embedding_service(embedding_service)

    # Create analyzer
    analyzer = CodeGraphAnalyzer(project_name, named_vectors=args.named_vectors)

    # Connect to Weaviate
    if not analyzer.connect():
        try:
            embedding_service.close()
        except Exception:
            pass
        return 1

    try:
        # Always ensure collections exist with correct schema (named vectors)
        # before getting references — Weaviate v4 client auto-creates flat-vector
        # collections on .get() if they don't exist, which breaks named vector inserts.
        #
        # Wrap separately from the analyze phase so the launcher IPC
        # sees a precise non-zero exit when create_collections hits the
        # case-collision fail-fast path (bug 0.6). Without this wrap a
        # raise from `_create_class_with_retry` would still exit
        # non-zero (good), but the print order would be confusing
        # (analyzer's "Analyzing codebase..." appears before the
        # actual error if we didn't bail explicitly here).
        try:
            analyzer.create_collections(force=args.force_recreate)
        except Exception as e:
            msg = str(e)
            if "found similar class" in msg.lower():
                # Stderr message was already emitted by
                # _create_class_with_retry. Just exit non-zero so the
                # launcher's IPC surfaces a failure toast.
                return 2  # Distinct from generic-failure 1 so callers
                          # can special-case schema collisions.
            print(f"❌ Failed to create collections: {e}", file=sys.stderr)
            return 1

        # v0.2.18 (Plan C): wire JSON-progress emitter for the Tauri
        # Re-analyze modal. Each emit prints one JSON line on stdout that
        # the parent process reads via BufReader::lines() and forwards to
        # the front-end as a `vct-reanalysis-progress` Tauri event.
        if args.json_progress:
            def _emit_progress(frac: float, msg: str, fpath: str, lang: str) -> None:
                # Bounded fraction so float drift doesn't push the bar past 1.
                try:
                    frac_f = float(frac)
                except Exception:
                    frac_f = 0.0
                if frac_f < 0.0:
                    frac_f = 0.0
                elif frac_f > 1.0:
                    frac_f = 1.0
                payload = {
                    "progress": frac_f,
                    "message": msg,
                    "file": fpath,
                    "lang": lang,
                }
                # `flush=True` so the parent's line reader doesn't wait
                # for the analyzer to finish before seeing per-file ticks.
                print(json.dumps(payload), flush=True)
            analyzer._progress_emitter = _emit_progress

        # WS-4 Finding 3: drop any --extra-path that is an ephemeral agent
        # git-worktree under the system temp dir (same pollution vector as the
        # primary-root guard; legit temp repos/clones are kept).
        extra_paths = list(args.extra_paths or [])
        if not args.allow_temp_root:
            kept = []
            for ep in extra_paths:
                if _is_ephemeral_worktree_root(ep):
                    print(
                        f"⚠️  Skipping --extra-path (ephemeral git worktree under "
                        f"the system temp dir): {ep} — would pollute the persistent "
                        "collection (WS-4 Finding 3). Pass --allow-temp-root to override.",
                        file=sys.stderr,
                    )
                else:
                    kept.append(ep)
            extra_paths = kept

        # Analyze repository
        if not args.json_progress:
            print("🔍 Analyzing codebase...")
        stats = analyzer.analyze_repository(
            repo_path,
            language=args.language,
            incremental=args.incremental,
            extract_cfg=args.cfg,
            extract_pdg=args.pdg,
            prune_stale=args.prune_stale,
            # v0.2.47 (extras): pass-through. Empty list when the flag
            # wasn't supplied — analyze_repository treats that the same
            # as the pre-v0.2.47 single-root behaviour.
            extra_paths=extra_paths or None,
            since_commit=args.since_commit,
            # v0.2.66 (Bug 3): single-file scope for the per-edit hook.
            # None (the default) preserves the whole-tree walk.
            only_file=args.only_file,
            # v0.2.66 (Bug 3, part b): canonical source root for worktree
            # dedup (only honoured alongside only_file).
            canonical_source=args.canonical_source,
        )

        # Post-processing: create cross-references
        ref_stats = analyzer.create_cross_references()

        # v0.2.18 (Plan C): final progress emit so the modal's progress bar
        # snaps to 100% before the report is rendered.
        if args.json_progress and analyzer._progress_emitter is not None:
            try:
                analyzer._progress_emitter(
                    1.0,
                    f"Analyzed {stats.get('files_analyzed', 0)} files",
                    "",
                    args.language or "",
                )
            except Exception:
                pass
            # Final report JSON line — the Tauri command's stdout reader
            # looks for `{"final": true, ...}` and returns it to the modal.
            final_payload = {
                "final": True,
                "files_analyzed": stats.get("files_analyzed", 0),
                "files_skipped": stats.get("files_skipped", 0),
                "modules": stats.get("modules", 0),
                "classes": stats.get("classes", 0),
                "functions": stats.get("functions", 0),
                "apis": stats.get("apis", 0),
                "insert_errors": stats.get("insert_errors", 0),
                "stale_pruned": stats.get("stale_pruned", 0),
                "language": args.language or "",
                "prune_stale": bool(args.prune_stale),
            }
            print(json.dumps(final_payload), flush=True)

        # Report results
        print("\n" + "="*60)
        if stats.get('insert_errors', 0) > 0:
            print("⚠️  Code Graph Analysis Complete (with errors)")
        elif stats.get('files_analyzed', 0) == 0:
            print("⚠️  Code Graph Analysis: NO FILES INDEXED")
        else:
            print("✅ Code Graph Analysis Complete")
        print("="*60)
        print(f"📊 Statistics:")
        print(f"   Modules: {stats['modules']}")
        print(f"   Classes: {stats['classes']}")
        print(f"   Functions: {stats['functions']}")
        print(f"   APIs: {stats['apis']}")
        print(f"   Files analyzed: {stats['files_analyzed']}")
        print(f"   Files skipped: {stats['files_skipped']}")
        # v0.2.16: surface granular error + prune stats so operators
        # can see at a glance whether the run was clean (and the
        # launcher's wizard polling can read the same numbers via
        # the database row's `files_analyzed` / new error_count cols).
        print(f"   Insert errors: {stats.get('insert_errors', 0)}")
        if args.prune_stale:
            print(f"   Stale entries pruned: {stats.get('stale_pruned', 0)}")
        print(f"   Cross-references: {ref_stats['calls']} calls, {ref_stats['extends']} extends, {ref_stats['imports']} imports")
        print()

        # v0.2.16 (bug 0.2): non-zero exit code on bad outcomes.
        # The launcher's `rebuild_code_graph` Tauri command should
        # map these to user-visible warning toasts:
        #   3 → "No files indexed — check repo path / filters"
        #   4 → "Partial insert failures — check logs"
        # Pre-v0.2.16 the script always returned 0 even when most
        # files failed to write, masking serious data-loss bugs.
        files_total = stats['files_analyzed'] + stats['files_skipped']
        if stats['files_analyzed'] == 0 and files_total > 0:
            print(
                "❌ No files were successfully analyzed — every file in scope "
                "failed. Check the warnings above for the root cause.",
                file=sys.stderr,
            )
            return 3
        if stats.get('insert_errors', 0) > 0:
            print(
                f"❌ {stats['insert_errors']} insert errors — analysis incomplete. "
                "The code graph for this project is missing data.",
                file=sys.stderr,
            )
            return 4

        return 0

    finally:
        analyzer.close()
        if embedding_service is not None:
            try:
                embedding_service.close()
            except Exception:
                pass


def _emit_code_graph_deferral_no_backend(install_root: Path, exc: Exception) -> None:
    """Soft-fail deferral when NO embedding backend is reachable.

    Same pattern as the KG-sync deferral helper in sync_knowledge_graph.py
    — writes ``<install_root>/.claude/context/UPDATE_DEFERRED.md`` so the
    launcher / install.py surfaces the issue. Idempotent. Soft-fail on
    any IO / import error.
    """
    try:
        from vco_lib.deferral_report import DeferralEntry, DeferralReport
        entry = DeferralEntry(
            condition_id="code_graph_no_embedding_backend",
            title="Code-graph analysis skipped: no embedding backend reachable",
            detected=(
                "analyze_code_graph.py could not reach any configured "
                "embedding backend (CodeEmbed / Ollama / OpenAI). Error: "
                f"{exc}"
            ),
            why_deferred=(
                "Soft-fail policy: install must never block on transient "
                "service unavailability. The code graph for this project "
                "will be empty until the next analysis run succeeds. See "
                "~/.claude/metrics/embedding_failures.jsonl for the "
                "per-backend diagnostic written by EmbeddingService."
            ),
            command_to_apply=(
                "# Restart embedding services then re-run analysis:\n"
                "podman start vco_code_embed vco_ollama   # or: docker start ...\n"
                ".claude/scripts/code-graph-analyze . --project <name>"
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/embedding-service-v0218.md",
            ],
        )
        report = DeferralReport.read(install_root)
        report.add_entry(entry)
        report.write(install_root)
    except Exception as inner:
        print(f"   (deferral emit failed: {inner})", file=sys.stderr)


def _emit_code_graph_deferral_code_backend_down(
    install_root: Path,
    svc: "EmbeddingService",
) -> None:
    """Soft-fail deferral when the active CODE backend specifically is down.

    Distinguishes from the no-backend-at-all case because a CodeEmbed
    container can be down while Ollama is up (or vice-versa). The
    deferral entry points at the right service to restart.
    """
    try:
        from vco_lib.deferral_report import DeferralEntry, DeferralReport
        slot = svc.code_vector_slot
        model = svc.code_model_id
        if "codesage" in slot:
            service_hint = (
                "CodeEmbed service (vco_code_embed container on port 11440)"
            )
            restart_cmd = "podman start vco_code_embed"
        elif "openai" in slot:
            service_hint = "OpenAI API"
            restart_cmd = (
                "# Check OPENAI_API_KEY is set and the key is valid:\n"
                "# Preferences → Special Secrets → OpenAI → Re-check"
            )
        else:
            service_hint = "Ollama (vco_ollama container on port 11435)"
            restart_cmd = "podman start vco_ollama"

        entry = DeferralEntry(
            condition_id="code_graph_code_backend_unreachable",
            title=f"Code-graph analysis skipped: {service_hint} not reachable",
            detected=(
                f"analyze_code_graph.py would write to slot '{slot}' "
                f"(model: {model}), but the backend serving that slot is "
                "currently unreachable. Refusing to proceed — a code graph "
                "with empty vectors is worse than no code graph (search "
                "would return all-zero scores)."
            ),
            why_deferred=(
                "Soft-fail policy: never produce a degraded code graph. "
                "Restart the service and re-run analysis."
            ),
            command_to_apply=(
                f"{restart_cmd}\n"
                ".claude/scripts/code-graph-analyze . --project <name>"
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/embedding-service-v0218.md",
            ],
        )
        report = DeferralReport.read(install_root)
        report.add_entry(entry)
        report.write(install_root)
    except Exception as inner:
        print(f"   (deferral emit failed: {inner})", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
