# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for NEW-10: _collection_name bare-class-name guard.

Covers:
  - NEW-10 (2026-05-28): _collection_name raises SystemExit when project_name
    is empty, preventing silent writes to bare Weaviate class names that cause
    multi-project data collision.
  - Normal path: _collection_name returns prefixed name when project_name set.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Load the analyzer module (same pattern as test_analyze_code_graph_v0_2_16)
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_new10_analyze_code_graph", str(_ANALYZER_PATH)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"Analyzer module file missing from repo — CI env regression: {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.fail("weaviate-client package not installed — CI env regression (required dependency missing)")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer_module()


# ---------------------------------------------------------------------------
# NEW-10 — _collection_name bare-class guard
# ---------------------------------------------------------------------------


def test_collection_name_raises_on_empty_project(analyzer_mod: types.ModuleType) -> None:
    """_collection_name('CodeFunction', '') must raise SystemExit with a message
    that mentions '--project is required'.

    This guards against silent writes to bare Weaviate class names
    (e.g. 'CodeFunction') when the operator forgets --project or
    CODE_GRAPH_PROJECT env. Multiple unrelated analyses would pile
    into the same collection with no per-project isolation.
    """
    with pytest.raises(SystemExit) as exc_info:
        analyzer_mod._collection_name("CodeFunction", "")

    message = str(exc_info.value)
    assert "--project is required" in message, (
        f"SystemExit message should mention '--project is required', got: {message!r}"
    )


def test_collection_name_raises_mentions_bare_name(analyzer_mod: types.ModuleType) -> None:
    """The SystemExit message should name the bare class so the operator
    understands which collection would have been written to."""
    with pytest.raises(SystemExit) as exc_info:
        analyzer_mod._collection_name("CodeClass", "")

    message = str(exc_info.value)
    assert "CodeClass" in message, (
        f"SystemExit message should name the bare class 'CodeClass', got: {message!r}"
    )


def test_collection_name_normal_path(analyzer_mod: types.ModuleType) -> None:
    """When project_name is provided, return the prefixed collection name."""
    result = analyzer_mod._collection_name("CodeFunction", "MyProject")
    assert result == "MyProject_CodeFunction", (
        f"Expected 'MyProject_CodeFunction', got {result!r}"
    )


def test_collection_name_all_base_types_raise_on_empty(
    analyzer_mod: types.ModuleType,
) -> None:
    """Every base collection type raises SystemExit when project_name is empty."""
    base_types = ["CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"]
    for base in base_types:
        with pytest.raises(SystemExit, match="--project is required"):
            analyzer_mod._collection_name(base, "")


# ---------------------------------------------------------------------------
# v0.2.73 Q1 — G5 explicit-project worktree-segment guard
#   `_worktree_segment_in_value` must flag an explicit --project /
#   CODE_GRAPH_PROJECT value that carries a WHOLE worktree-container segment
#   ('.wt' / 'worktrees' / 'vco-wt'), and must NOT false-flag a legit name that
#   merely contains the substring "wt".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,offending",
    [
        ("vco-wt/bug1", "vco-wt"),
        ("vco-wt/pnew_safeadd_env", "vco-wt"),
        (".wt/agent-abc", ".wt"),
        ("worktrees/foo", "worktrees"),
        ("some/path/.wt/track", ".wt"),
        ("repo\\worktrees\\bug3", "worktrees"),   # windows-style separator
        ("VCO-WT/Bug1", "VCO-WT"),                # case-insensitive
        ("foo vco-wt bar", "vco-wt"),             # whitespace-split segment
    ],
)
def test_worktree_segment_in_value_flags_worktree_names(
    analyzer_mod: types.ModuleType, value: str, offending: str
) -> None:
    """A worktree-container segment (whole segment) is detected + returned."""
    result = analyzer_mod._worktree_segment_in_value(value)
    assert result is not None, f"{value!r} should be flagged as a worktree name"
    assert result.lower() == offending.lower(), (
        f"expected offending segment {offending!r}, got {result!r}"
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "MyProject",
        "SwiftUI",            # contains 'wt'? no — but a 'wt'-substring family
        "MyWtfProject",       # contains substring 'wt' but not a segment
        "Growth",             # contains 'wt' substring
        "NetworkThing",       # contains 'wt' substring
        "wt-foo",             # 'wt-foo' is a token, NOT the segment 'vco-wt'/'.wt'
        "SwiftlyTyped",       # contains 'wt' substring
        "worktreeish",        # 'worktreeish' != segment 'worktrees'
        "dotwt",              # not '.wt'
        "VibeCodedOrchestrator",
    ],
)
def test_worktree_segment_in_value_allows_legit_names(
    analyzer_mod: types.ModuleType, value,
) -> None:
    """A legit project name that merely contains the substring 'wt' (or is a
    near-miss like 'wt-foo'/'worktreeish') must NOT be flagged — only a WHOLE
    path segment equal to '.wt'/'worktrees'/'vco-wt' triggers refusal."""
    assert analyzer_mod._worktree_segment_in_value(value) is None, (
        f"{value!r} must NOT be flagged as a worktree name"
    )


def test_g5_guard_does_not_false_refuse_legit_wt_named_project(
    analyzer_mod: types.ModuleType,
) -> None:
    """N-5 (unit form): the guard helper must pass a legit project name that
    contains the substring 'wt' as part of a word. Locks segment-membership
    semantics so a future loosening to substring matching is caught."""
    for legit in ("SwiftlyTyped", "MyWtfProject", "Growth", "wt-foo"):
        assert analyzer_mod._worktree_segment_in_value(legit) is None, (
            f"guard helper FALSE-REFUSED a legit 'wt'-substring project: {legit!r}"
        )
