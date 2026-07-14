# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
search-mcp — Academic paper search for multi-agent workflows.

Single tool exposed to claude -p agents:

  search_papers(query, source, limit, year_from)
      → OpenAlex (CC0, 240M works) + arXiv (CS/ML preprints)

This MCP was simplified in v0.2.11: web search, GitHub code search, and
generic URL fetching were dropped because Claude itself ships those
capabilities natively (WebSearch, WebSearch with `site:github.com`
qualifiers, and WebFetch respectively). Academic paper search has no
native Claude equivalent — OpenAlex's structured query DSL, citation
graphs, and arXiv's preprint corpus require a dedicated upstream call,
which is what this MCP preserves.

Rate limiting — asyncio token bucket per upstream API:
  arXiv:      0.333/sec  (= 1 req / 3s mandated by ToS)
  OpenAlex:   1 req/sec  (conservative; free tier = 100k credits/day)

Environment variables:
  OPENALEX_EMAIL   Email for OpenAlex polite pool (optional, improves
                   rate limits and reliability).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

import aiohttp
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# PR-42 (v0.2.12): SIGHUP-driven clean exit so the launcher (or the user
# running `kill -HUP <pid>`) can ask this MCP to pick up an updated
# `.claude/settings.json env`. The handler exits cleanly with code 0;
# Claude Code respawns us on the next request with fresh env. See
# claude_mcp_servers/_lib/sighup_handler.py for the full design rationale.
#
# `_lib` is a SHIPPED component of every healthy install. When this file
# runs as a bare script (`python <install>/claude_mcp_servers/search_mcp/
# server.py`) sys.path[0] is the server's OWN dir, so the parent dir
# (claude_mcp_servers/) must be inserted first for `_lib` to resolve at
# all. That minimal insert is the ONLY thing done inline; the shared
# `_lib.bootstrap.import_lib_member` then owns the retry+LOUD-FAIL for the
# real member import (a missing `_lib` = broken install; the pre-fix
# silent `register_sighup_exit_handler → False` stub masked that and
# disabled SIGHUP env-reload with no signal).
_mcp_root = str(Path(__file__).resolve().parent.parent)  # …/claude_mcp_servers
if _mcp_root not in sys.path:
    sys.path.insert(0, _mcp_root)
from _lib.bootstrap import import_lib_member  # noqa: E402
register_sighup_exit_handler = import_lib_member(
    "sighup_handler", "register_sighup_exit_handler"
)
register_sighup_exit_handler(logger)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL", "")

mcp = FastMCP(
    "search-mcp",
    instructions=(
        "Academic paper search across OpenAlex (240M works, CC0 license, "
        "citation-sorted) and arXiv (CS/ML/physics preprints, relevance-sorted). "
        "Use search_papers() for literature search, discovering influential "
        "papers, and locating the latest preprints. Rate-limited: OpenAlex 1 req/s, "
        "arXiv 0.333 req/s. For general web search, GitHub code search, or "
        "fetching arbitrary URLs use Claude's built-in WebSearch and WebFetch — "
        "those tools were dropped from this MCP in v0.2.11 to avoid duplication."
    )
)


# ---------------------------------------------------------------------------
# Rate limiting — token bucket
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Async token bucket. Blocks callers until a token is available."""

    def __init__(self, rate: float) -> None:
        """
        Args:
            rate: Maximum requests per second.
        """
        self._rate = rate
        self._tokens = float(rate)   # start full
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# Module-level singletons — one per upstream API
_arxiv_rl    = _TokenBucket(rate=0.333)  # 1/3s (arXiv ToS)
_openalex_rl = _TokenBucket(rate=1.0)    # conservative


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _get_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """GET → parse JSON. Returns None on any error."""
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers or {}) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.debug("GET %s → HTTP %d", url, resp.status)
                    return None
                return await resp.json(content_type=None)
    except Exception as exc:
        logger.debug("GET %s failed: %s", url, exc)
        return None


async def _get_text(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
) -> Optional[str]:
    """GET → return text body. Returns None on any error."""
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers or {}) as session:
            async with session.get(url, params=params, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.debug("GET %s → HTTP %d", url, resp.status)
                    return None
                return await resp.text(errors="replace")
    except Exception as exc:
        logger.debug("GET %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Tool: search_papers
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_papers(
    query: str,
    limit: int = 10,
    source: str = "openalex",
    year_from: int = 0,
) -> str:
    """
    Search academic papers (free, no subscription required).

    Sources:
      openalex — 240M works, CC0 license, citation graphs, sorted by citations.
                 Best for: comprehensive literature search, discovering influential papers.
      arxiv    — CS/ML/physics preprints, sorted by relevance.
                 Best for: latest research, technical reports not yet peer-reviewed.

    Args:
        query:     Natural language search query.
        limit:     Max results (1-25, default 10).
        source:    'openalex' (default) or 'arxiv'.
        year_from: Include only papers from this year onwards (0 = no filter).

    Returns:
        JSON with keys:
          source  (str)  — which source was queried
          query   (str)  — query as sent
          results (list) — [{title, authors, year, abstract, doi, url, citations}]
          error   (str)  — present only on failure
    """
    limit = max(1, min(limit, 25))
    if source == "arxiv":
        return await _search_arxiv(query, limit, year_from)
    return await _search_openalex(query, limit, year_from)


async def _search_openalex(query: str, limit: int, year_from: int) -> str:
    params: dict[str, Any] = {
        "search":   query,
        "per-page": limit,
        "select":   (
            "id,title,abstract_inverted_index,doi,"
            "publication_year,cited_by_count,authorships,primary_location"
        ),
        "sort": "cited_by_count:desc",
    }
    if year_from:
        params["filter"] = f"publication_year:>{year_from - 1}"
    if OPENALEX_EMAIL:
        params["mailto"] = OPENALEX_EMAIL

    await _openalex_rl.acquire()
    data = await _get_json("https://api.openalex.org/works", params=params)

    if not data:
        return json.dumps({"error": "OpenAlex unavailable", "source": "openalex", "query": query, "results": []})

    results = []
    for work in data.get("results", []):
        abstract = _reconstruct_abstract(work.get("abstract_inverted_index") or {})
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in (work.get("authorships") or [])[:5]
        ]
        doi = work.get("doi") or ""
        url = doi if doi.startswith("http") else (f"https://doi.org/{doi}" if doi else work.get("id", ""))
        results.append({
            "title":     (work.get("title") or "").strip(),
            "authors":   authors,
            "year":      work.get("publication_year"),
            "abstract":  abstract[:500],
            "doi":       doi,
            "url":       url,
            "citations": work.get("cited_by_count", 0),
        })

    return json.dumps({"source": "openalex", "query": query, "results": results})


async def _search_arxiv(query: str, limit: int, year_from: int) -> str:
    await _arxiv_rl.acquire()

    # Fetch extra when year filtering to avoid empty results after filtering
    fetch_limit = limit * 2 if year_from else limit
    text = await _get_text(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{query}",
            "start":        0,
            "max_results":  fetch_limit,
            "sortBy":       "relevance",
            "sortOrder":    "descending",
        },
        timeout=aiohttp.ClientTimeout(total=25),
    )

    if not text:
        return json.dumps({"error": "arXiv API unavailable", "source": "arxiv", "query": query, "results": []})

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        logger.debug("arXiv XML parse error: %s", exc)
        return json.dumps({"error": "arXiv XML parse error", "source": "arxiv", "query": query, "results": []})

    ns = {
        "atom":  "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    results: list[dict] = []
    for entry in root.findall("atom:entry", ns):
        published = (entry.findtext("atom:published", namespaces=ns) or "")[:4]
        if year_from and published:
            try:
                if int(published) < year_from:
                    continue
            except ValueError:
                pass

        authors = [
            (a.findtext("atom:name", namespaces=ns) or "")
            for a in entry.findall("atom:author", ns)[:5]
        ]
        results.append({
            "title":     (entry.findtext("atom:title",   namespaces=ns) or "").strip(),
            "authors":   authors,
            "year":      int(published) if published and published.isdigit() else None,
            "abstract":  (entry.findtext("atom:summary", namespaces=ns) or "").strip()[:500],
            "doi":       "",
            "url":       (entry.findtext("atom:id",      namespaces=ns) or "").strip(),
            "citations": None,
        })
        if len(results) >= limit:
            break

    return json.dumps({"source": "arxiv", "query": query, "results": results})


def _reconstruct_abstract(inv_index: dict) -> str:
    """Reconstruct text from OpenAlex inverted index: {word: [pos, ...]}."""
    if not inv_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, pos_list in inv_index.items():
        for pos in pos_list:
            positions.append((pos, word))
    positions.sort()
    return " ".join(w for _, w in positions)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # V52-AI (v0.2.52): exit cleanly if an orchestrator update is in
    # progress. Breaks the Windows MCP fork-bomb (~97 python +
    # ~77 node processes the user reported on 2026-06-09) by making
    # every respawn during the update window exit immediately.
    #
    # `_lib.update_gate` is SHIPPED; import_lib_member LOUD-FAILS if it's
    # missing. The pre-fix silent `exit_if_update_in_progress = None` stub
    # was especially dangerous here: it disabled the fork-bomb guard on the
    # exact broken-install path most likely to be mid-update, silently
    # re-arming the loop the gate exists to break. (`_lib` is already on
    # sys.path from the SIGHUP bootstrap at module import; the insert is
    # idempotent-safe if this block is ever reached first.)
    _mcp_root = str(Path(__file__).resolve().parent.parent)
    if _mcp_root not in sys.path:
        sys.path.insert(0, _mcp_root)
    exit_if_update_in_progress = import_lib_member(
        "update_gate", "exit_if_update_in_progress"
    )
    exit_if_update_in_progress("search MCP")

    mcp.run()
