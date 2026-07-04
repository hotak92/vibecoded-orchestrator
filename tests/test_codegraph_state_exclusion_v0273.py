# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 (READ-amp origin fix): `.claude/state/` is excluded on EVERY walk.

The bug this pins
-----------------
On the orchestrator clone (and on any project whose codegraph `index_dot_claude`
resolves True) the analyzer's directory walk descends into `.claude/`. Because
`state` / `tool_backups` were in NO ignore set, the walk indexed every `.py`
under `.claude/state/tool_backups/` — timestamped SNAPSHOT copies of source
files (many of them backups of `.wt/` worktree files, so they ALSO defeated the
`.wt` exclusion). Live-measured 2026-07-04: **16,143 such backup functions =
43% of a real project's CodeFunction collection**, and the churn from re-indexing
them across sessions was a primary driver of a 120 GB / 333-segment collection
whose live data is a few MB.

The single-file guard (`_analyze_single_file`) already skipped `.claude/state/`,
but its own comment asserted "the directory walk doesn't reach state/ in
practice" — FALSE on the orchestrator clone. The fix adds a shared
`_is_under_transient_state` predicate consumed by BOTH the directory walk (via
`_keep_source_file`, wired into all 14 `_find_*_files` walkers) and the
single-file path. These tests pin BOTH paths, at BOTH `index_dot_claude` values,
so the regression cannot come back through either entry vector (primary repo OR
an `--extra-path` root — extras walk through the SAME `_find_*_files`).

Pure unit — no Weaviate, no embeddings. The predicate + walkers are filesystem
logic; a real DB is not needed to prove the exclusion.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest


_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_state_excl_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module missing — CI regression: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


def _make_analyzer(analyzer_mod: types.ModuleType, index_dot_claude: bool):
    """Bare analyzer with just the state needed by the `_find_*_files` walkers."""
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.index_dot_claude = index_dot_claude
    return inst


def _seed_repo_with_state_backups(root: Path) -> None:
    """A repo with real source + a `.claude/state/tool_backups/` snapshot tree
    that mirrors the live-observed flattened-backup filename shape."""
    # Real first-party source under .claude/ (indexed when index_dot_claude=True).
    (root / ".claude" / "scripts").mkdir(parents=True)
    (root / ".claude" / "scripts" / "real_tool.py").write_text(
        "def real_tool():\n    return 1\n"
    )
    # Real top-level source.
    (root / "pkg").mkdir()
    (root / "pkg" / "app.py").write_text("def app():\n    return 2\n")

    # The transient scratch that must NEVER be indexed. Flattened backup names
    # exactly as tool_backups produces them (incl. a `.wt` worktree backup).
    backups = root / ".claude" / "state" / "tool_backups"
    backups.mkdir(parents=True)
    (backups / "20260701_200951___repo_.wt_v0272-floor_scripts_query.py").write_text(
        "def query():\n    return 3\n"
    )
    (backups / "20260702_044842___repo_launcher_src_project_state.py").write_text(
        "def default_source():\n    return 'project'\n"
    )
    # Other .claude/state scratch (session state), also a .py, also must skip.
    (root / ".claude" / "state").joinpath("scratch.py").write_text(
        "def scratch():\n    return 4\n"
    )


# ─── The predicate itself ────────────────────────────────────────────────────


def test_predicate_flags_state_paths(analyzer_mod):
    f = analyzer_mod._is_under_transient_state
    assert f(Path(".claude/state/tool_backups/x.py").parts) is True
    assert f(Path("/abs/repo/.claude/state/scratch.py").parts) is True
    # A nested .claude/state deeper in the tree (extra-path root) still flagged.
    assert f(Path("proj/.claude/state/tool_backups/y.rs").parts) is True


def test_predicate_does_not_flag_legit_paths(analyzer_mod):
    f = analyzer_mod._is_under_transient_state
    # First-party .claude source is NOT transient.
    assert f(Path(".claude/scripts/real_tool.py").parts) is False
    assert f(Path(".claude/hooks/foo.sh").parts) is False
    # A legitimate top-level `state/` package (NOT under .claude) is NOT flagged —
    # this is why the fix is a two-segment predicate, not a bare 'state' ignore.
    assert f(Path("src/state/store.py").parts) is False
    assert f(Path("state/machine.py").parts) is False


# ─── The directory walk — BOTH index_dot_claude values ───────────────────────


@pytest.mark.parametrize("index_dot_claude", [True, False])
def test_walk_never_indexes_state_backups(analyzer_mod, tmp_path, index_dot_claude):
    """The Python walker returns ZERO files under `.claude/state/`, whether or
    not `.claude/` itself is indexed. When index_dot_claude=True (orchestrator
    clone) the real `.claude/scripts` source IS returned but state/ is not."""
    _seed_repo_with_state_backups(tmp_path)
    analyzer = _make_analyzer(analyzer_mod, index_dot_claude=index_dot_claude)

    found = analyzer._find_python_files(tmp_path)
    found_rel = {p.relative_to(tmp_path).as_posix() for p in found}

    # No `.claude/state/**` file is EVER returned.
    assert not any(".claude/state/" in r for r in found_rel), (
        f"state/ leaked into the walk (index_dot_claude={index_dot_claude}): "
        f"{[r for r in found_rel if '.claude/state/' in r]}"
    )
    # The real top-level source is always found.
    assert "pkg/app.py" in found_rel

    if index_dot_claude:
        # First-party .claude source IS indexed on the orchestrator clone —
        # proves we excluded ONLY state/, not all of .claude/.
        assert ".claude/scripts/real_tool.py" in found_rel
    else:
        # User project: all of .claude/ is excluded (state/ included).
        assert not any(r.startswith(".claude/") for r in found_rel)


@pytest.mark.parametrize(
    "finder",
    [
        "_find_python_files",
        "_find_shell_files",
        "_find_rust_files",
        "_find_js_files",
        "_find_ts_files",
    ],
)
def test_all_walkers_share_the_state_exclusion(analyzer_mod, tmp_path, finder):
    """Every walker routes through `_keep_source_file`, so a state/ backup in
    that walker's language is excluded too (mirror-don't-fork guarantee)."""
    ext = {
        "_find_python_files": "py",
        "_find_shell_files": "sh",
        "_find_rust_files": "rs",
        "_find_js_files": "js",
        "_find_ts_files": "ts",
    }[finder]
    backups = tmp_path / ".claude" / "state" / "tool_backups"
    backups.mkdir(parents=True)
    (backups / f"20260701_snap.{ext}").write_text("x = 1\n")
    (tmp_path / f"real.{ext}").write_text("y = 2\n")

    analyzer = _make_analyzer(analyzer_mod, index_dot_claude=True)
    found = {p.relative_to(tmp_path).as_posix() for p in getattr(analyzer, finder)(tmp_path)}

    assert not any(".claude/state/" in r for r in found), (
        f"{finder} leaked a state/ backup: {found}"
    )


# ─── The single-file / drain (--only-files-from) path ────────────────────────


def test_single_file_dispatch_skips_state_backup(analyzer_mod, tmp_path):
    """A direct `--only-file .claude/state/...` (or a drain queue entry pointing
    at one) is a clean no-op — the same predicate guards it."""
    backups = tmp_path / ".claude" / "state" / "tool_backups"
    backups.mkdir(parents=True)
    victim = backups / "20260701_200951___repo_scripts_query.py"
    victim.write_text("def query():\n    return 3\n")

    analyzer = _make_analyzer(analyzer_mod, index_dot_claude=True)
    lang_dispatch = [
        ("python", analyzer._find_python_files, analyzer._analyze_python_file),
    ]
    out = analyzer._single_file_dispatch(victim, tmp_path, lang_dispatch, None)
    assert out == [], "single-file/drain path indexed a .claude/state backup"


# ─── One shared decision (mirror-don't-fork) ─────────────────────────────────


def test_walk_and_single_file_share_one_exclusion_predicate(analyzer_mod):
    """The walk (`_keep_source_file`) and the single-file/drain path both defer
    to `_path_is_excluded` — pin that they agree on representative inputs so the
    two entry vectors can never drift apart."""
    excl = analyzer_mod._path_is_excluded
    keep = analyzer_mod._keep_source_file
    ignore = frozenset({"node_modules", ".wt", "vendor"})

    for rel in (
        ".claude/state/tool_backups/x.py",
        ".wt/v/y.py",
        "node_modules/z.js",
        "vendor/lib.rb",
    ):
        p = Path(rel)
        assert excl(p.parts, ignore) is True
        assert keep(p, ignore) is False, f"walk kept an excluded path: {rel}"

    for rel in (".claude/scripts/real.py", "pkg/app.py", "src/state/store.py"):
        p = Path(rel)
        assert excl(p.parts, ignore) is False
        assert keep(p, ignore) is True, f"walk dropped a legit path: {rel}"


# ─── Extra-path roots exclude their OWN .claude/ by default (v0.2.73) ─────────


def test_finder_gate_indexes_dot_claude_only_when_flag_set(analyzer_mod, tmp_path):
    """The finder's `.claude` verdict is driven solely by `self.index_dot_claude`
    (the value the walk loop sets PER ROOT). With the flag True a `.claude/scripts`
    source is returned; with it False the same file is excluded. This is the exact
    lever the per-root gate flips for extras — pinned on the real finder, not a
    re-implementation of the loop."""
    (tmp_path / ".claude" / "scripts").mkdir(parents=True)
    (tmp_path / ".claude" / "scripts" / "tool.py").write_text("def t():\n    return 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "main.py").write_text("def m():\n    return 2\n")

    inst_true = _make_analyzer(analyzer_mod, index_dot_claude=True)
    names_true = {p.name for p in inst_true._find_python_files(tmp_path)}
    assert names_true == {"tool.py", "main.py"}  # root indexes its own .claude/

    inst_false = _make_analyzer(analyzer_mod, index_dot_claude=False)
    names_false = {p.name for p in inst_false._find_python_files(tmp_path)}
    assert names_false == {"main.py"}  # .claude/ excluded → extra-path default


def test_walk_loop_forces_extras_to_exclude_dot_claude_source_guard(analyzer_mod):
    """Source-anchored guard (matches the codebase's
    `test_analyze_code_graph_source_carries_env_lookup` idiom): the
    `analyze_repository` walk loop MUST set `index_dot_claude=False` for extra
    roots regardless of the primary's value. Anchors on the stable per-root gate
    so a refactor that drops it (re-opening the extra-`.claude` entry vector) is
    caught even without a live Weaviate E2E."""
    src = _ANALYZER_PATH.read_text(encoding="utf-8")
    # The per-root gate: extras get False, primary keeps the resolved value.
    assert "False if is_extra_root else _primary_index_dot_claude" in src, (
        "the per-root index_dot_claude gate for extra paths is missing — an "
        "extra-path root's .claude/ would be indexed when the primary indexes its own"
    )
    # And it is restored after the walk so downstream readers see the primary's value.
    assert "self.index_dot_claude = _primary_index_dot_claude" in src
