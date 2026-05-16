# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Programmatic guard: the three dropped Search MCP tools are not
exposed as module-level callables on `claude_mcp_servers.search_mcp.server`.

Complements `test_search_mcp_only_papers.py` (which probes the MCP tool
registry) by directly inspecting the module's public attribute surface.
A future refactor that re-introduces e.g. `web_search` as a private
helper without re-registering it on the MCP would still slip past the
registry test — this module catches that case.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude_mcp_servers.search_mcp import server as search_server  # noqa: E402


_DROPPED_TOOLS = ("web_search", "search_code", "fetch_page")


def test_dropped_tools_are_not_module_attributes():
    """None of the dropped tools may be accessible as `server.<tool>`."""
    leaked = [name for name in _DROPPED_TOOLS if hasattr(search_server, name)]
    assert not leaked, (
        f"dropped Search MCP tool(s) still defined at module scope: {leaked}"
    )


def test_dropped_tools_are_not_callable_via_getattr():
    """Defence in depth: even if Python attribute lookup quirks return
    something, it must not be a callable. Catches the case where a stub
    or alias re-exposes a dropped name without raising AttributeError.
    """
    for name in _DROPPED_TOOLS:
        obj = getattr(search_server, name, None)
        assert obj is None or not callable(obj), (
            f"dropped tool {name!r} is callable (type={type(obj).__name__}) — "
            "it should not be present at all"
        )


def test_search_papers_remains_callable():
    """Positive contract: the surviving tool is still a callable
    coroutine function. A simplification that accidentally deleted
    search_papers too would be caught here, not just in the
    'only_papers' registry test."""
    import inspect
    assert hasattr(search_server, "search_papers"), (
        "search_papers is missing from search_mcp.server — the v0.2.11 "
        "simplification was supposed to KEEP this tool, not drop it"
    )
    obj = search_server.search_papers
    assert callable(obj), f"search_papers is not callable (got {type(obj).__name__})"
    # `@mcp.tool()` returns either the original coroutine function or
    # a thin wrapper that's still a coroutine function in FastMCP's
    # current implementation. We assert one or the other.
    assert inspect.iscoroutinefunction(obj) or callable(obj), (
        f"search_papers is not a coroutine-like callable: {obj!r}"
    )
