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


# ---------------------------------------------------------------------------
# v0.2.72 (P5) — scope tightening: exclude .claude(non-root)/vendor/
# .bundle.js/.chunk.js/.wt from analysis + per-project .claude toggle.
# ---------------------------------------------------------------------------


def _make_bare_analyzer(analyzer_mod, index_dot_claude: bool):
    """Construct a CodeGraphAnalyzer WITHOUT running __init__ (no Weaviate /
    embedding-service dependency), setting only the attrs the _find_* methods
    read. Mirrors the __new__ pattern in tests/test_code_graph_content_hash_skip.py."""
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.index_dot_claude = index_dot_claude
    return inst


def test_wt_worktree_container_excluded(analyzer_mod):
    """P5: the top-level `.wt/` git-worktree container (release per-track
    worktrees) is exact byte copies of main-repo source. Walking it injects
    the worktree copy's functions as retrieval noise. Complements the
    existing `worktrees` (`.claude/worktrees/`) entry."""
    assert ".wt" in analyzer_mod._COMMON_IGNORE_DIRS, (
        "P5 fix missing — `.wt` must be in _COMMON_IGNORE_DIRS so the "
        "release's per-track worktrees aren't re-indexed as duplicate noise."
    )


def test_vendor_excluded_for_js_and_ts(analyzer_mod):
    """P5: JS/TS `vendor/` holds third-party/minified libraries (e.g. the
    launcher's bundled diagram editors). Indexing them injects thousands of
    vendor functions as retrieval noise. Go/Ruby already skip vendor."""
    for lang in ("js", "ts"):
        assert "vendor" in analyzer_mod._ignore_dirs_for(lang), (
            f"P5 fix missing — `vendor` must be in the {lang} ignore set."
        )


def test_ignore_dirs_gates_dot_claude(analyzer_mod):
    """P5: `.claude` is EXCLUDED when index_dot_claude is False (default for
    every user project — `.claude/` is generated tooling, not source) and
    INCLUDED (walked) when True (orchestrator clone / opt-in)."""
    for lang in ("python", "js", "ts", "rust", "shell"):
        excluded = analyzer_mod._ignore_dirs_for(lang, False)
        included = analyzer_mod._ignore_dirs_for(lang, True)
        assert ".claude" in excluded, (
            f"P5: `.claude` must be in the {lang} ignore set when "
            f"index_dot_claude=False (user-project default)."
        )
        assert ".claude" not in included, (
            f"P5: `.claude` must NOT be in the {lang} ignore set when "
            f"index_dot_claude=True (orchestrator clone / opt-in)."
        )


def test_ignore_dirs_default_excludes_dot_claude(analyzer_mod):
    """P5: the DEFAULT (no flag arg) must exclude `.claude` — conservative,
    matches the launcher passing --no-index-dot-claude for user projects."""
    assert ".claude" in analyzer_mod._ignore_dirs_for("python"), (
        "P5: `_ignore_dirs_for(lang)` with the default flag must exclude "
        "`.claude` (conservative default for user projects)."
    )


def test_resolve_index_dot_claude_tri_state(analyzer_mod, tmp_path):
    """P5: the tri-state CLI resolver. Explicit True/False win; None falls
    back to root-autodetect (index only when the path looks like the
    orchestrator clone: has vco_lib/ + .claude/)."""
    # Explicit flags win regardless of the path shape.
    assert analyzer_mod._resolve_index_dot_claude(True, tmp_path) is True
    assert analyzer_mod._resolve_index_dot_claude(False, tmp_path) is False

    # None + a plain user project (no vco_lib/) → exclude.
    user_proj = tmp_path / "user_project"
    (user_proj / ".claude").mkdir(parents=True)
    assert analyzer_mod._resolve_index_dot_claude(None, user_proj) is False

    # None + an orchestrator-shaped root (vco_lib/ + .claude/) → index.
    orch_root = tmp_path / "orchestrator"
    (orch_root / "vco_lib").mkdir(parents=True)
    (orch_root / ".claude").mkdir(parents=True)
    assert analyzer_mod._resolve_index_dot_claude(None, orch_root) is True


def test_find_js_files_skips_bundle_and_chunk_and_vendor(analyzer_mod, tmp_path):
    """P5: `_find_js_files` must NOT discover `.bundle.js` / `.chunk.js`
    build output, nor anything under a `vendor/` dir — live-confirmed noise
    source `launcher/vendor/diagrams-editor/excalidraw/excalidraw.bundle.js`."""
    # Real first-party source — SHOULD be found.
    src = tmp_path / "src"
    src.mkdir()
    keep = src / "app.js"
    keep.write_text("function real() { return 1; }\n", encoding="utf-8")
    # Build bundles — SHOULD be skipped by suffix.
    (src / "app.bundle.js").write_text("function b(){}\n", encoding="utf-8")
    (src / "app.chunk.js").write_text("function c(){}\n", encoding="utf-8")
    (src / "app.min.js").write_text("function m(){}\n", encoding="utf-8")
    # Vendored dep — SHOULD be skipped by dir.
    vendor = tmp_path / "launcher" / "vendor" / "diagrams-editor" / "excalidraw"
    vendor.mkdir(parents=True)
    (vendor / "excalidraw.bundle.js").write_text("function _CA(){}\n", encoding="utf-8")
    (vendor / "plain.js").write_text("function v(){}\n", encoding="utf-8")

    inst = _make_bare_analyzer(analyzer_mod, index_dot_claude=False)
    found = {p.name for p in inst._find_js_files(tmp_path)}
    assert "app.js" in found, "first-party source must still be discovered"
    for skipped in ("app.bundle.js", "app.chunk.js", "app.min.js",
                    "excalidraw.bundle.js", "plain.js"):
        assert skipped not in found, (
            f"P5: {skipped} must NOT be discovered by _find_js_files "
            f"(bundle/chunk suffix or vendor/ dir)."
        )


def test_find_python_files_gates_dot_claude(analyzer_mod, tmp_path):
    """P5: a `.claude/scripts/x.py` is discovered ONLY when index_dot_claude
    is True. For a user project (False) it is excluded as generated tooling."""
    # First-party source — always found.
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("def real():\n    return 1\n", encoding="utf-8")
    # Generated tooling under .claude/ — gated.
    dot_claude = tmp_path / ".claude" / "scripts"
    dot_claude.mkdir(parents=True)
    (dot_claude / "x.py").write_text("def tooling():\n    return 2\n", encoding="utf-8")

    excluded = _make_bare_analyzer(analyzer_mod, index_dot_claude=False)
    found_excl = {p.name for p in excluded._find_python_files(tmp_path)}
    assert "main.py" in found_excl
    assert "x.py" not in found_excl, (
        "P5: `.claude/scripts/x.py` must NOT be discovered when "
        "index_dot_claude=False (user-project default)."
    )

    included = _make_bare_analyzer(analyzer_mod, index_dot_claude=True)
    found_incl = {p.name for p in included._find_python_files(tmp_path)}
    assert "x.py" in found_incl, (
        "P5: `.claude/scripts/x.py` MUST be discovered when "
        "index_dot_claude=True (orchestrator clone / opt-in)."
    )


def test_find_files_skip_wt_worktree_dir(analyzer_mod, tmp_path):
    """P5: files under a top-level `.wt/` worktree container are never
    discovered (they're byte-copies of main-repo source)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("def real():\n    return 1\n", encoding="utf-8")
    wt = tmp_path / ".wt" / "v0272-track" / "src"
    wt.mkdir(parents=True)
    (wt / "main.py").write_text("def dup():\n    return 1\n", encoding="utf-8")

    inst = _make_bare_analyzer(analyzer_mod, index_dot_claude=True)
    found = [str(p) for p in inst._find_python_files(tmp_path)]
    assert any(p.endswith("/src/main.py") and ".wt" not in p for p in found), (
        "first-party src/main.py must be found"
    )
    assert not any(".wt" in p for p in found), (
        "P5: files under `.wt/` must NOT be discovered."
    )
