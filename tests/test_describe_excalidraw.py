# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the ``describe_excalidraw`` MCP tool (Phase 1.5.C).

The MCP wrapper lives in ``claude_mcp_servers/weaviate_mcp/server.py``
right after ``hybrid_search``. It's a thin wrapper around
:func:`vco_lib.diagram_indexer.parse_excalidraw` (Phase 1.5.A STUB,
shipped alongside this branch).

We import the function directly and call it as a coroutine — that
exercises every code path the FastMCP wrapper takes, including the
``_large_result`` JSON serialisation that wraps the return.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def describe_excalidraw():
    """Import the tool lazily — the MCP module pulls in weaviate / mcp
    at import time which is heavy. Skip the whole file if those aren't
    importable on this host (CI without the MCP venv).

    v0.2.73 (M-1): import server as the PACKAGE module ``weaviate_mcp.server``,
    NOT via ``spec_from_file_location`` on the bare file. The monolith split
    added relative imports (``from .rl_enrichment import …`` /
    ``from .embeddings import …``) to server.py, which raise "attempted
    relative import with no known parent package" when the file is loaded under
    a package-less synthetic module name. A proper package import resolves the
    siblings correctly (and matches how the module is loaded in production)."""
    try:
        import importlib
        # ``claude_mcp_servers`` must be on sys.path so ``weaviate_mcp`` is a
        # resolvable package (the extracted sibling modules import relatively).
        pkg_parent = REPO_ROOT / "claude_mcp_servers"
        if str(pkg_parent) not in sys.path:
            sys.path.insert(0, str(pkg_parent))
        module = importlib.import_module("weaviate_mcp.server")
    except Exception as exc:
        pytest.fail(f"weaviate_mcp.server import failed — CI env regression (shipped module must be importable): {exc}")
    fn = getattr(module, "describe_excalidraw", None)
    if fn is None:
        pytest.fail("describe_excalidraw missing from weaviate_mcp.server — CI env regression (shipped function must be present)")
    # FastMCP decorates the function — unwrap to the original.
    inner = getattr(fn, "fn", None) or getattr(fn, "__wrapped__", None) or fn
    return inner


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _write_excalidraw(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_scene_returns_metadata(describe_excalidraw, tmp_path: Path):
    scene = {
        "type": "excalidraw",
        "version": 2,
        "appState": {"name": "Auth Flow", "viewBackgroundColor": "#fff"},
        "elements": [
            {"type": "rectangle", "id": "r1"},
            {"type": "rectangle", "id": "r2"},
            {"type": "text", "id": "t1", "text": "Login"},
            {"type": "text", "id": "t2", "text": "Submit"},
            {"type": "arrow", "id": "a1"},
        ],
    }
    path = tmp_path / "auth.excalidraw"
    _write_excalidraw(path, scene)

    raw = _run(describe_excalidraw(str(path)))
    result = json.loads(raw)
    assert result["success"] is True
    assert result["scene_name"] == "Auth Flow"
    assert sorted(result["text_labels"]) == ["Login", "Submit"]
    assert result["element_counts"]["rectangle"] == 2
    assert result["element_counts"]["text"] == 2
    assert result["element_counts"]["arrow"] == 1


def test_missing_file_clear_error(describe_excalidraw, tmp_path: Path):
    raw = _run(describe_excalidraw(str(tmp_path / "ghost.excalidraw")))
    result = json.loads(raw)
    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_malformed_json_clear_error(describe_excalidraw, tmp_path: Path):
    path = tmp_path / "broken.excalidraw"
    path.write_text("this is not json {{{", encoding="utf-8")

    raw = _run(describe_excalidraw(str(path)))
    result = json.loads(raw)
    assert result["success"] is False
    assert "json" in result["error"].lower()


def test_wrong_suffix_clear_error(describe_excalidraw, tmp_path: Path):
    path = tmp_path / "auth.mmd"
    path.write_text("flowchart TD\n  A --> B\n", encoding="utf-8")

    raw = _run(describe_excalidraw(str(path)))
    result = json.loads(raw)
    assert result["success"] is False
    # Should mention Mermaid / .mmd suggestion (the tool description's
    # promise: don't use this on .mmd, just Read it).
    assert "mermaid" in result["error"].lower() or "mmd" in result["error"].lower()


def test_scene_with_no_name_returns_null(describe_excalidraw, tmp_path: Path):
    scene = {
        "type": "excalidraw",
        "appState": {},
        "elements": [{"type": "rectangle", "id": "r1"}],
    }
    path = tmp_path / "anon.excalidraw"
    _write_excalidraw(path, scene)

    raw = _run(describe_excalidraw(str(path)))
    result = json.loads(raw)
    assert result["success"] is True
    assert result["scene_name"] is None
    assert result["text_labels"] == []
    assert result["element_counts"] == {"rectangle": 1}


def test_non_dict_top_level_clear_error(describe_excalidraw, tmp_path: Path):
    path = tmp_path / "list.excalidraw"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    raw = _run(describe_excalidraw(str(path)))
    result = json.loads(raw)
    assert result["success"] is False
    assert "object" in result["error"].lower()
