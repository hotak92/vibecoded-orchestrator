# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Single source of truth for the KG / code-graph access matrix.

The launcher GUI's per-project access matrix lands at runtime as two
comma-separated env vars:

* ``VCT_KG_ACCESS_LIST=Foo,Bar``         — peer projects whose
  ``<Peer>_KnowledgeGraph`` collection this process is allowed to fan
  out READ queries to.
* ``VCT_CODE_GRAPH_ACCESS_LIST=Foo,Bar`` — peer projects whose
  ``<Peer>_CodeFunction``, ``<Peer>_CodeClass`` (etc) collections this
  process is allowed to fan out READ queries to.

Both vars are emitted by the Rust ``write_project_env_files`` writer to
all 3 install surfaces (``.claude/env``, ``.claude/settings.json``'s
``env`` block, ``.vscode/settings.json``'s ``claude-code.env`` block) so
EVERY surface that spawns a Python subprocess inherits them.

Pre-2026-05-08 the matrix was a launcher-internal feature with no
runtime effect — the GUI claimed to grant access but no consumer
honoured it. Fix #4 of PR #171 wired it into the MCP server side.
P1-D follow-up (this module, 2026-05-08) wires it into every CLI Python
script that reads KG/codegraph, so hooks shelling out to those CLIs
also fan out across peer collections.

Read paths in scope here:

* ``hybrid_search`` / ``semantic_graph_search`` / ``search_code_graph``
  MCP tools  →  already wired in
  ``claude_mcp_servers/weaviate_mcp/server.py`` via the in-module
  ``_kg_collections_to_search`` / ``_parse_csv_env`` helpers (Fix #4).
* ``rl_kg_search.py`` (called by ``pre-edit-context-inject.sh``) →
  re-imports those helpers from the MCP server.
* ``templates/scripts/{search_knowledge.py, query_code_graph.py,
  get_node_info.py}``  →  these standalone CLIs are this module's
  primary consumers.

Write paths are deliberately out of scope: writes always target the
project's own collection (or the shared collection when explicitly
opted in via ``store_knowledge_node(scope="shared")``). Granting peer
WRITE access would defeat the whole point of per-project KG isolation.

Why a separate module instead of importing from
``weaviate_mcp.server``?
==================================================================

Standalone CLI scripts run from two layouts:

1. The orchestrator's own clone — ``.claude/scripts/`` runs alongside
   ``claude_mcp_servers/``, so ``sys.path`` insertion of
   ``claude_mcp_servers/`` resolves cleanly.
2. A user project — ``.claude/scripts/`` is a copy dropped by
   ``vco_lib.project_init install-bundle``, but ``claude_mcp_servers/``
   lives back in the orchestrator clone. Resolution requires
   ``$VCT_ORCHESTRATOR_ROOT`` (set in ``.claude/env``).

Importing the full ``weaviate_mcp.server`` module pulls in
``weaviate``, ``aiohttp``, the ``mcp`` SDK, and a ~3500-line file. For a
small CSV-parsing helper that's massive overhead and creates a hard
runtime dependency on the venv being healthy. This module is pure
stdlib + no side effects — CLIs can import it cheaply without
spinning up the MCP venv at all.

The MCP server's in-module helpers stay where they are (the tests
``test_kg_access_list.py`` pin them; rewriting that path is out of
scope). This module is a thin re-export-friendly twin: same semantics,
no extra deps, importable from either layout.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

# Code-graph base collection names. Mirrors the ``_SCOPES["all"]`` list
# in ``weaviate_mcp.server.search_code_graph``. Kept in lockstep manually
# (small set, rarely changes); a drift here would be caught by the
# integration tests that exercise both paths.
CODE_GRAPH_BASES: tuple[str, ...] = (
    "CodeFunction",
    "CodeClass",
    "CodeModule",
    "CodeAPI",
    "CodeInteraction",
)


def parse_csv_env(name: str) -> list[str]:
    """Parse a comma-separated env var, dropping empties and stripping
    whitespace.

    Returns ``[]`` when the var is unset, empty, or whitespace-only.
    Whitespace-padded entries (``"Foo , Bar"``) are tolerated to make
    hand-edited ``.env`` files Just Work.

    Mirrors ``weaviate_mcp.server._parse_csv_env`` exactly. Matches the
    same env-name → list semantics so callers get identical behaviour
    whether they go through the MCP tool path or the CLI path.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def sanitize_collection_prefix(name: str) -> str:
    """Sanitize a project name into the canonical Weaviate collection-prefix
    shape: alphanumeric + underscore only, leading char uppercased.

    Idempotent — passing in an already-sanitized prefix returns it
    unchanged. The launcher always emits sanitized names in
    ``VCT_*_ACCESS_LIST``, but we tolerate either form for forward-compat
    with tooling that builds its own access list (tests, scripts that
    don't go through the launcher).

    Mirrors ``weaviate_mcp.server._sanitize_collection_prefix``.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized and not sanitized[0].isupper():
        sanitized = sanitized[0].upper() + sanitized[1:]
    return sanitized


def kg_peer_collections(env_var: str = "VCT_KG_ACCESS_LIST") -> list[str]:
    """Return the list of peer-project KG collection names this process
    should search, derived from ``VCT_KG_ACCESS_LIST``.

    Each peer name is sanitized and suffixed with ``_KnowledgeGraph`` to
    recover the canonical Weaviate collection name. Order is preserved
    from the env var; duplicates (after sanitization) are dropped.

    Excludes self / shared by design — the caller is responsible for
    putting those at the head of the search list. See
    ``kg_collections_to_search`` for the full union.
    """
    out: list[str] = []
    seen: set[str] = set()
    for p in parse_csv_env(env_var):
        prefix = sanitize_collection_prefix(p)
        if not prefix:
            continue
        coll = f"{prefix}_KnowledgeGraph"
        if coll in seen:
            continue
        seen.add(coll)
        out.append(coll)
    return out


def kg_collections_to_search(
    self_kg: str,
    shared_kg: Optional[str] = None,
    development: Optional[str] = None,
    include_dev: bool = False,
) -> list[str]:
    """Return the union of KG collections this process should fan-out
    across.

    Order: ``self → shared (when configured + distinct) → peers → dev
    (when ``include_dev=True`` + configured + distinct)``. Duplicates are
    dropped. Empty / whitespace-only inputs collapse cleanly to the
    pre-access-matrix behaviour (just self [+ shared]).

    Parameters
    ----------
    self_kg
        The current project's KG collection name (typically
        ``$KG_COLLECTION``).
    shared_kg
        Cross-project shared KG collection name, if configured
        (``$SHARED_KG_COLLECTION``). Pass ``""`` or ``None`` when not
        configured. When equal to ``self_kg`` it's NOT double-listed.
    development
        Development-collection name, if configured
        (``$DEVELOPMENT_COLLECTION``). Honoured only when
        ``include_dev=True`` (i.e. ``hybrid_search`` semantics —
        graph traversal skips dev docs).
    include_dev
        If ``True`` AND ``development`` is set AND distinct from the
        rest, append ``development`` at the end of the list.

    Notes
    -----
    Mirrors ``weaviate_mcp.server._kg_collections_to_search`` exactly.
    The MCP path reads the env vars directly and binds ``self_kg`` etc.
    at module-import time; the CLI path reads them at call time so each
    CLI invocation respects the current env. Both produce the same list
    given the same env state.
    """
    out: list[str] = [self_kg]
    if shared_kg and shared_kg != self_kg:
        out.append(shared_kg)
    for coll in kg_peer_collections():
        if coll == self_kg or coll == shared_kg:
            # Defensive: launcher resolver already excludes self/shared
            # but tolerate malformed input rather than double-listing.
            continue
        if coll not in out:
            out.append(coll)
    if include_dev and development and development not in out:
        out.append(development)
    return out


def code_graph_collections_to_query(
    self_project: str,
    bases: Optional[Iterable[str]] = None,
    env_var: str = "VCT_CODE_GRAPH_ACCESS_LIST",
) -> list[tuple[str, str]]:
    """Return the list of (collection_name, project_filter_value) pairs
    for a code-graph fan-out across self + every peer in
    ``VCT_CODE_GRAPH_ACCESS_LIST``.

    The second tuple element is the value to pass to a
    ``Filter.by_property("project").equal(...)`` clause when querying the
    collection — Weaviate stores the un-sanitized project name in the
    ``project`` property, while the collection NAME uses the sanitized
    prefix. The MCP server's ``search_code_graph`` builds the same shape
    (``coll_meta``) — this helper centralises the construction.

    Parameters
    ----------
    self_project
        The current project name. When empty / falsy, returns
        ``[("", "")]`` — i.e. the bare-collections, no-filter,
        cross-tenant fallback path used by the MCP when the caller
        explicitly asks for "search all projects".
    bases
        Iterable of code-graph base names (``CodeFunction``,
        ``CodeClass``, …). When ``None``, defaults to
        ``CODE_GRAPH_BASES`` (all 5 collections).
    env_var
        Name of the access-list env var. Defaults to
        ``VCT_CODE_GRAPH_ACCESS_LIST``; overridable for tests.

    Returns
    -------
    list[tuple[str, str]]
        Each pair is ``(collection_name, project_filter_value)``.
        ``collection_name`` is what to pass to
        ``client.collections.get(...)``.
        ``project_filter_value`` is empty ``""`` to skip the filter
        (cross-tenant search) or the un-sanitized project name to apply
        the per-project filter.

    Notes
    -----
    Mirrors the prefix-list construction in
    ``weaviate_mcp.server.search_code_graph`` (lines 2917-2950 as of
    e1ba0fb). Tested side-by-side against ``test_kg_access_list.py``
    semantics for KG and against the new
    ``test_code_graph_access_list.py`` for code graph.
    """
    bases_t = tuple(bases) if bases is not None else CODE_GRAPH_BASES

    if not self_project:
        # Cross-tenant fallback: bare collection names, no filter.
        return [(base, "") for base in bases_t]

    self_prefix = sanitize_collection_prefix(self_project)
    seen_prefixes: set[str] = {self_prefix}
    prefixes: list[tuple[str, str]] = [(self_prefix, self_project)]
    for peer in parse_csv_env(env_var):
        peer_prefix = sanitize_collection_prefix(peer)
        if not peer_prefix or peer_prefix in seen_prefixes:
            continue
        seen_prefixes.add(peer_prefix)
        prefixes.append((peer_prefix, peer))

    out: list[tuple[str, str]] = []
    for prefix, project_filter in prefixes:
        for base in bases_t:
            out.append((f"{prefix}_{base}", project_filter))
    return out


# Public API — what callers import. Kept short on purpose; the
# CSV / sanitize helpers are exposed too because callers occasionally
# need them standalone (e.g. building one-off collection names).
__all__ = [
    "CODE_GRAPH_BASES",
    "parse_csv_env",
    "sanitize_collection_prefix",
    "kg_peer_collections",
    "kg_collections_to_search",
    "code_graph_collections_to_query",
]
