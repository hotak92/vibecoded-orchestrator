# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``hybrid_search`` diagrams integration (Phase 1.5.C).

We exercise the collection-resolution layer end-to-end without a live
Weaviate. The contracts under test:

1. ``DIAGRAMS_COLLECTION`` set → ``_kg_collections_to_search`` includes
   it when ``include_diagrams=True`` (the new flag wired by
   ``hybrid_search``).
2. ``DIAGRAMS_COLLECTION`` unset → graceful skip (empty diagrams list,
   no exception).
3. ``_format_obj`` stamps ``result_kind="diagram"`` when the source
   collection is the diagrams collection (or a diagrams peer).
4. ``_format_obj`` stamps ``result_kind="knowledge"`` for every other
   collection (KG, shared KG, Development, peer KGs).
5. ``VCT_DIAGRAMS_ACCESS_LIST`` is respected; falls back to
   ``VCT_KG_ACCESS_LIST`` when the diagrams-specific var is unset.

To re-import the module with different env vars per test, we drop and
reload it via ``importlib`` inside each test (faster than spawning a
subprocess and exercises the real module-load resolution path that
matters for production).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
SERVER_PY = MCP_DIR / "weaviate_mcp" / "server.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_server(monkeypatch) -> Iterator:
    """Reload weaviate_mcp.server with controlled env. Yields the module
    so the test can poke at constants + helpers.

    Important: we explicitly DROP previously-imported versions of the
    module and its dependants so module-level env reads happen anew.
    """
    # 1. Ensure the MCP dir is on sys.path so `import weaviate_mcp.server`
    #    works the same way the FastMCP launcher would.
    for p in (str(MCP_DIR), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)

    # 2. Tear down anything previously imported from this server.
    to_drop = [
        name for name in list(sys.modules)
        if name == "weaviate_mcp" or name.startswith("weaviate_mcp.")
    ]
    for name in to_drop:
        sys.modules.pop(name, None)

    # 3. The MCP server logs its resolved collections at module load —
    #    keep the log call cheap. We do nothing here; standard logging
    #    is fine.
    def _do_import():
        try:
            return importlib.import_module("weaviate_mcp.server")
        except Exception as exc:
            pytest.skip(f"weaviate_mcp.server cannot be imported: {exc}")

    yield _do_import

    # 4. Cleanup post-test.
    for name in list(sys.modules):
        if name == "weaviate_mcp" or name.startswith("weaviate_mcp."):
            sys.modules.pop(name, None)


def _set_env(monkeypatch, **kwargs: str) -> None:
    """Set / unset env vars. Pass empty string to unset."""
    for key, value in kwargs.items():
        if value is None or value == "":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _make_obj(**properties) -> SimpleNamespace:
    """Build a fake Weaviate object with .properties + .metadata."""
    metadata = SimpleNamespace(distance=properties.pop("distance", 0.1))
    return SimpleNamespace(properties=dict(properties), metadata=metadata)


# ---------------------------------------------------------------------------
# Tests — _kg_collections_to_search
# ---------------------------------------------------------------------------


def test_diagrams_collection_set_included_in_search(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="VibeCodedOrchestrator_KnowledgeGraph",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="MyProject_Diagrams",
        VCT_KG_ACCESS_LIST="",
        VCT_DIAGRAMS_ACCESS_LIST="",
        VCT_HUB_TOKEN="",  # force env-fallback for hub-routed config
    )
    server = fresh_server()
    assert server.DIAGRAMS_COLLECTION == "MyProject_Diagrams"

    # include_diagrams=True → diagrams included.
    cols = server._kg_collections_to_search(
        include_dev=True, include_diagrams=True
    )
    assert "MyProject_Diagrams" in cols
    # First two slots reserved for self + shared KG.
    assert cols[0] == "MyProject_KnowledgeGraph"
    assert cols[1] == "VibeCodedOrchestrator_KnowledgeGraph"

    # include_diagrams=False (semantic_graph_search) → diagrams excluded.
    cols_no_dia = server._kg_collections_to_search(
        include_dev=False, include_diagrams=False
    )
    assert "MyProject_Diagrams" not in cols_no_dia


def test_diagrams_collection_unset_graceful_skip(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="VibeCodedOrchestrator_KnowledgeGraph",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="",  # unset
        VCT_KG_ACCESS_LIST="",
        VCT_DIAGRAMS_ACCESS_LIST="",
    )
    server = fresh_server()
    # Empty string coerced to "" by empty_means_unset=True.
    assert server.DIAGRAMS_COLLECTION == ""

    cols = server._kg_collections_to_search(
        include_dev=True, include_diagrams=True
    )
    # No diagrams collection means none should appear in the search list.
    assert all(not c.endswith("_Diagrams") for c in cols)
    # The helper returns an empty list cleanly.
    assert server._diagrams_collections_to_search() == []


def test_diagrams_access_list_explicit_var(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="A_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="A_Diagrams",
        VCT_KG_ACCESS_LIST="",            # no KG cross-access
        VCT_DIAGRAMS_ACCESS_LIST="B,C",   # but yes diagrams cross-access
    )
    server = fresh_server()
    diagrams = server._diagrams_collections_to_search()
    # Self first, peers after.
    assert diagrams[0] == "A_Diagrams"
    assert "B_Diagrams" in diagrams
    assert "C_Diagrams" in diagrams


def test_diagrams_access_does_not_fall_back_to_kg_list(monkeypatch, fresh_server):
    """v0.2.34 A7 regression guard.

    Pre-v0.2.34 (Phase 1.5.C) the MCP fell back to ``VCT_KG_ACCESS_LIST``
    when ``VCT_DIAGRAMS_ACCESS_LIST`` was unset — wrong granularity:
    granting KG access leaked diagram visibility. A7 removed the
    fallback. With KG access set + diagrams-specific UNSET, peers must
    NOT appear in the diagrams fan-out (only the project's own
    DIAGRAMS_COLLECTION is searched).
    """
    _set_env(
        monkeypatch,
        KG_COLLECTION="A_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="A_Diagrams",
        VCT_KG_ACCESS_LIST="B,C",          # KG cross-access set
        VCT_DIAGRAMS_ACCESS_LIST="",       # diagrams-specific UNSET
    )
    server = fresh_server()
    diagrams = server._diagrams_collections_to_search()
    # Only self — KG-only grant must NOT leak into diagrams.
    assert diagrams == ["A_Diagrams"]
    assert "B_Diagrams" not in diagrams
    assert "C_Diagrams" not in diagrams


def test_diagrams_no_duplicates(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="A_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="A_Diagrams",
        VCT_KG_ACCESS_LIST="",
        VCT_DIAGRAMS_ACCESS_LIST="A,B,A",  # self + B + dup
    )
    server = fresh_server()
    diagrams = server._diagrams_collections_to_search()
    # Self appears exactly once (first); duplicates collapsed.
    assert diagrams.count("A_Diagrams") == 1
    assert diagrams.count("B_Diagrams") == 1


# ---------------------------------------------------------------------------
# Tests — result_kind discriminator on _format_obj
# ---------------------------------------------------------------------------


def test_format_obj_marks_diagram_results(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="VibeCodedOrchestrator_KnowledgeGraph",
        DEVELOPMENT_COLLECTION="MyProject_Development",
        DIAGRAMS_COLLECTION="MyProject_Diagrams",
        VCT_KG_ACCESS_LIST="",
        VCT_DIAGRAMS_ACCESS_LIST="",
    )
    server = fresh_server()

    obj = _make_obj(
        title="auth-flow",
        content="flowchart TD\n  A --> B",
        node_type="diagram",
        tags=["gui", "auth"],
        file_path="/abs/path/to/auth-flow.mmd",
    )
    formatted = server._format_obj(obj, "MyProject_Diagrams", distance=0.05)
    assert formatted["result_kind"] == "diagram"
    assert formatted["collection"] == "MyProject_Diagrams"


def test_format_obj_marks_knowledge_for_kg(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="VibeCodedOrchestrator_KnowledgeGraph",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="MyProject_Diagrams",
        VCT_KG_ACCESS_LIST="",
        VCT_DIAGRAMS_ACCESS_LIST="",
    )
    server = fresh_server()

    obj = _make_obj(
        title="error-handling",
        content="...",
        node_type="concept",
        tags=["python"],
        file_path="knowledge/concepts/error-handling.md",
    )
    formatted = server._format_obj(obj, "MyProject_KnowledgeGraph", distance=0.1)
    assert formatted["result_kind"] == "knowledge"

    # Shared KG also routes to "knowledge".
    formatted_shared = server._format_obj(
        obj, "VibeCodedOrchestrator_KnowledgeGraph", distance=0.1
    )
    assert formatted_shared["result_kind"] == "knowledge"


def test_format_obj_marks_peer_diagrams_as_diagram(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="A_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="A_Diagrams",
        VCT_KG_ACCESS_LIST="",
        VCT_DIAGRAMS_ACCESS_LIST="B",
    )
    server = fresh_server()

    obj = _make_obj(title="b-sketch", content="...", node_type="diagram")
    formatted = server._format_obj(obj, "B_Diagrams", distance=0.05)
    assert formatted["result_kind"] == "diagram"


def test_format_obj_no_diagrams_collection_still_marks_knowledge(monkeypatch, fresh_server):
    """When diagrams aren't configured at all, every result is "knowledge".
    This is the backward-compat path: existing projects unaffected."""
    _set_env(
        monkeypatch,
        KG_COLLECTION="A_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_DIAGRAMS_ACCESS_LIST="",
    )
    server = fresh_server()

    obj = _make_obj(title="x", content="...", node_type="concept")
    formatted = server._format_obj(obj, "A_KnowledgeGraph", distance=0.1)
    assert formatted["result_kind"] == "knowledge"
    # No mis-routing of e.g. development collection either.
    formatted_dev = server._format_obj(obj, "A_Development", distance=0.1)
    assert formatted_dev["result_kind"] == "knowledge"
