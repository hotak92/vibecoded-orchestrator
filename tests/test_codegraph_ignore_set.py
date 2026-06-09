# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for V52-O.1 (ignore-set extension) and V52-O.3 (UUID5 seed
project_source mixing).

V52-O.1 regression: ``_COMMON_IGNORE_DIRS`` must include the JS/TS
framework codegen + cache dirs (``.svelte-kit``, ``.next``, etc.) so the
analyzer no longer indexes SvelteKit Rollup chunks as CodeFunction rows.

V52-O.3 regression: ``_deterministic_uuid`` must produce DIFFERENT UUIDs
when the SAME (project, file_path_rel, full_name) tuple is seeded under
two different ``project_source`` roots (primary repo vs. --extra-path).
The pre-V52-O.3 seed had the two collide and the second walk's
replace() overwrote the first row.

Both tests are pure-Python — no Weaviate required.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loading — same pattern as tests/test_analyze_code_graph_v0_2_16.py
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    """Load the analyzer script as a module without sys.path side-effects."""
    spec = importlib.util.spec_from_file_location(
        "_v52_o1_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.skip(f"Cannot load analyzer module from {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        # The script calls sys.exit() on weaviate-client import failure.
        # When that happens, the env isn't set up for these tests anyway.
        pytest.skip("weaviate-client unavailable — analyzer cannot be loaded")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# V52-O.1 — _COMMON_IGNORE_DIRS extension
# ---------------------------------------------------------------------------


def test_svelte_kit_excluded(analyzer_mod):
    """V52-O.1: SvelteKit's `.svelte-kit/output/` produces Rollup chunks
    that look like JS source to the analyzer. Audit a58ced61 showed 93%
    of CodeFunction rows came from this single source on the public clone.
    Must be in the common ignore set."""
    assert ".svelte-kit" in analyzer_mod._COMMON_IGNORE_DIRS, (
        "V52-O.1 fix is missing — `.svelte-kit` must be in _COMMON_IGNORE_DIRS "
        "to prevent SvelteKit codegen pollution of CodeFunction rows."
    )


def test_other_framework_codegen_excluded(analyzer_mod):
    """V52-O.1: companion frameworks to SvelteKit. Each adds a build/cache
    tree the analyzer should never walk."""
    required = {
        ".next",          # Next.js codegen
        ".nuxt",          # Nuxt
        ".cache",         # generic framework cache
        ".parcel-cache",  # Parcel
        ".turbo",         # Turborepo
        ".angular",       # Angular cache
        "out",            # generic build output (alongside build/dist)
    }
    missing = required - set(analyzer_mod._COMMON_IGNORE_DIRS)
    assert not missing, (
        f"V52-O.1 fix is incomplete — these framework codegen / cache dirs "
        f"are missing from _COMMON_IGNORE_DIRS: {sorted(missing)}"
    )


def test_pre_v0252_entries_still_present(analyzer_mod):
    """Regression: the v0.2.52 extension must NOT have removed any of the
    pre-existing entries. `node_modules` (the JS dep cache) and `worktrees`
    (v0.2.16 git-worktree skip) are the two highest-stakes ones — if
    either drops out the analyzer will re-burn hours on duplicate work."""
    must_have = {"node_modules", "worktrees", "__pycache__", ".git", ".venv"}
    missing = must_have - set(analyzer_mod._COMMON_IGNORE_DIRS)
    assert not missing, (
        f"V52-O.1 extension regression — these pre-existing ignore-set "
        f"entries are missing: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# V52-O.3 — _deterministic_uuid project_source mixing
# ---------------------------------------------------------------------------


def test_uuid5_seed_different_per_source_root(analyzer_mod):
    """V52-O.3: same (project, file_path_rel, full_name) under two different
    `project_source` roots MUST produce two different UUIDs. Pre-V52-O.3
    the two collided and the second walk's replace() overwrote the first.
    """
    # Same project, same relative path, same symbol name — only the
    # absolute source root differs (primary repo vs. an --extra-path
    # second clone). Before V52-O.3, both calls returned the SAME UUID.
    uuid_primary = analyzer_mod._deterministic_uuid(
        "TestProject",
        "src/index.ts",
        "src.index.handler",
        project_source="/home/user/projects/primary",
    )
    uuid_extra = analyzer_mod._deterministic_uuid(
        "TestProject",
        "src/index.ts",
        "src.index.handler",
        project_source="/home/user/projects/sibling-clone",
    )

    assert uuid_primary != uuid_extra, (
        "V52-O.3 fix is missing — _deterministic_uuid must mix project_source "
        "into the seed so two source roots with the same relative path produce "
        "two different UUIDs. As-is, the second walk's replace() will overwrite "
        "the first row and one root's data is silently lost."
    )


def test_uuid5_seed_stable_for_same_source_root(analyzer_mod):
    """V52-O.3 back-compat: re-running the analyzer on the SAME source root
    (idempotent re-index) must produce the SAME UUID. This is the upsert
    path — if it broke, every re-analyze would duplicate every row."""
    uuid_a = analyzer_mod._deterministic_uuid(
        "TestProject",
        "src/index.ts",
        "src.index.handler",
        project_source="/home/user/projects/primary",
    )
    uuid_b = analyzer_mod._deterministic_uuid(
        "TestProject",
        "src/index.ts",
        "src.index.handler",
        project_source="/home/user/projects/primary",
    )
    assert uuid_a == uuid_b, (
        "V52-O.3 regression — same (project, source_root, file, symbol) "
        "tuple must produce the same UUID for upsert semantics."
    )


def test_uuid5_seed_empty_source_root_matches_legacy_shape(analyzer_mod):
    """V52-O.3 back-compat: when `project_source` is the empty string (the
    default, used by every call site pre-V52-O.3 and by single-root analyses
    where the dispatcher doesn't set `_current_source`), the UUID must match
    the v0.2.16-through-v0.2.51 shape so primary-repo-only installs don't
    get a spurious re-write of the entire collection on upgrade.

    Verified by checking that two calls with `project_source=""` and
    `project_source` omitted entirely produce the same UUID."""
    uuid_explicit_empty = analyzer_mod._deterministic_uuid(
        "TestProject",
        "src/index.ts",
        "src.index.handler",
        project_source="",
    )
    uuid_omitted = analyzer_mod._deterministic_uuid(
        "TestProject",
        "src/index.ts",
        "src.index.handler",
    )
    assert uuid_explicit_empty == uuid_omitted, (
        "V52-O.3 default-arg regression — omitting project_source must be "
        "equivalent to passing project_source=''."
    )
