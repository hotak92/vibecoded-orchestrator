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
        pytest.skip(f"Cannot load analyzer module from {_ANALYZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.skip("weaviate-client unavailable — analyzer cannot be loaded")
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
