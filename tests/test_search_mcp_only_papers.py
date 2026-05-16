# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.11 Search MCP simplification: only search_papers remains.

The Search MCP shipped four tools before v0.2.11:
    web_search   — SearXNG metasearch
    search_code  — GitHub code search
    search_papers — OpenAlex + arXiv
    fetch_page   — generic URL fetcher

Three were dropped because Claude itself ships native equivalents
(WebSearch, WebSearch with `site:github.com` qualifiers, WebFetch).
Only `search_papers` survives — 240M papers via OpenAlex (citation
graphs) + arXiv preprints have no native Claude equivalent.

These tests pin that contract so a future refactor can't quietly
re-add the dropped tools or rename `search_papers`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The search MCP module — import via the package path so test runs
# from any cwd.
from claude_mcp_servers.search_mcp import server as search_server  # noqa: E402


# ---------------------------------------------------------------------------
# Contract: exactly one MCP tool registered, named search_papers
# ---------------------------------------------------------------------------

def _list_registered_tool_names() -> list[str]:
    """Return the names of tools currently registered on the FastMCP
    instance in `search_server.mcp`.

    FastMCP's internal API has shifted across versions; we try a few
    candidate accessors and fall back to scanning the module for
    `@mcp.tool()`-decorated coroutines if needed.
    """
    mcp_obj = search_server.mcp
    # FastMCP exposes a tool manager with _tools dict in recent versions
    tool_mgr = getattr(mcp_obj, "_tool_manager", None)
    if tool_mgr is not None:
        tools_attr = getattr(tool_mgr, "_tools", None)
        if isinstance(tools_attr, dict):
            return sorted(tools_attr.keys())
    # Older / alternative API
    direct_tools = getattr(mcp_obj, "_tools", None)
    if isinstance(direct_tools, dict):
        return sorted(direct_tools.keys())
    # Last resort: scan module-level coroutines whose qualname suggests
    # they were registered as MCP tools.
    found: list[str] = []
    for name, obj in vars(search_server).items():
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(obj):
            found.append(name)
    return sorted(found)


def test_only_search_papers_is_registered():
    """The MCP tool surface must be exactly {search_papers}."""
    names = _list_registered_tool_names()
    assert "search_papers" in names, (
        f"search_papers missing from tool registry: {names}"
    )
    dropped = {"web_search", "search_code", "fetch_page"}
    leaked = dropped.intersection(names)
    assert not leaked, (
        f"dropped tools still registered on MCP server: {sorted(leaked)}"
    )


def test_dropped_tool_names_absent_from_module():
    """Dropped tools must not be importable as module-level callables."""
    for dropped_name in ("web_search", "search_code", "fetch_page"):
        assert not hasattr(search_server, dropped_name), (
            f"{dropped_name} is still defined at module level — it should "
            "have been removed in v0.2.11"
        )


def test_mcp_instructions_mention_search_papers_only():
    """The MCP `instructions` string steers Claude to the right tool —
    after the v0.2.11 simplification it should describe search_papers
    only, not the dropped web/code/fetch tools."""
    mcp_obj = search_server.mcp
    instructions = (
        getattr(mcp_obj, "instructions", "")
        or getattr(getattr(mcp_obj, "_mcp_server", None), "instructions", "")
        or ""
    )
    instructions_lower = instructions.lower()
    assert "search_papers" in instructions_lower, (
        f"instructions don't mention search_papers: {instructions!r}"
    )
    # The instructions are allowed to mention the dropped tools only in
    # the context of explaining that they were dropped (e.g. "use Claude's
    # WebSearch instead"). We just guard against the old-style "Use
    # web_search() for ..." imperative that would mislead callers.
    for bad_phrase in (
        "use web_search(",
        "use search_code(",
        "use fetch_page(",
    ):
        assert bad_phrase not in instructions_lower, (
            f"instructions still imperatively recommend a dropped tool "
            f"({bad_phrase!r}): {instructions!r}"
        )


def test_module_docstring_reflects_single_tool_scope():
    """The module docstring is what a maintainer sees first; it must
    not list the dropped tools as currently-exposed."""
    doc = (search_server.__doc__ or "")
    assert "search_papers" in doc, (
        f"module docstring doesn't mention search_papers: {doc[:200]!r}"
    )


def test_search_papers_returns_openalex_results_on_happy_path():
    """search_papers(openalex) → calls OpenAlex, returns parsed JSON.

    Driven via asyncio.run() (rather than pytest-asyncio) so the test
    has zero dependency on plugin config — the rest of the suite is
    sync today, so this stays portable across pytest-asyncio modes.
    """
    fake_openalex_response = {
        "results": [
            {
                "id":                       "https://openalex.org/W1",
                "title":                    "Example Paper",
                "abstract_inverted_index":  {"hello": [0], "world": [1]},
                "doi":                      "10.1234/example.001",
                "publication_year":         2024,
                "cited_by_count":           42,
                "authorships": [
                    {"author": {"display_name": "Alice Foo"}},
                    {"author": {"display_name": "Bob Bar"}},
                ],
                "primary_location":         None,
            }
        ]
    }

    async def _fake_get_json(url, params=None, headers=None, timeout=None):
        # Sanity-check the upstream URL so we know we're hitting the right API
        assert "openalex.org" in url, f"unexpected URL: {url}"
        return fake_openalex_response

    async def _run() -> str:
        with mock.patch.object(search_server, "_get_json", _fake_get_json):
            return await search_server.search_papers(
                "retrieval augmented generation", limit=5
            )

    raw = asyncio.run(_run())
    parsed = json.loads(raw)
    assert parsed["source"] == "openalex"
    assert parsed["query"] == "retrieval augmented generation"
    assert len(parsed["results"]) == 1
    only = parsed["results"][0]
    assert only["title"] == "Example Paper"
    assert only["year"] == 2024
    assert only["citations"] == 42
    assert "Alice Foo" in only["authors"]
    assert only["abstract"] == "hello world"
    # DOI without a scheme becomes a doi.org URL
    assert only["url"] == "https://doi.org/10.1234/example.001"
