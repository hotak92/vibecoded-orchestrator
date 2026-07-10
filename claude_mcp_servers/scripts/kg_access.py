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
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

# Best-effort delegation to the writer SSOT sanitizers so this CLI reader
# stays byte-behaviour-identical to them (and to the MCP mirrors). The
# import is graceful: ``kg_access`` is deliberately importable from a bare
# user-project layout where ``vco_lib`` may not be on ``sys.path`` and the
# MCP venv may be absent. When the import fails, each sanitizer uses an
# inline fallback that re-implements the SAME rule (see their bodies).
#
# TWO distinct rules (the same split the MCP server keeps, v0.2.74
# BLOCKER-1):
#   * KG / Development / Diagrams collections → underscore-DROPPING
#     ``sanitize_for_weaviate_class`` (``'My Cool App'`` → ``MyCoolApp``,
#     ``'Foo-Bar'`` → ``FooBar``). Mirror of
#     ``server._sanitize_collection_prefix``.
#   * Code-graph (``Code*``) collections → underscore-PRESERVING
#     ``canonical_class_prefix`` (``'My Cool App'`` → ``MyCoolApp`` but
#     ``'Foo-Bar'`` → ``Foo_Bar``, ``'Camel_Case'`` → ``Camel_Case``).
#     Mirror of ``server._code_sanitize_collection_prefix``. The analyzer
#     WRITES ``Code*`` classes with this rule, so the reader must match it
#     or it queries a class the writer never created (silent 0-results).
#
# vco_lib parent resolution matches the CLI consumers
# (``templates/scripts/search_knowledge.py``): ``$VCT_ORCHESTRATOR_ROOT``
# (set in ``.claude/env``) with an in-tree fallback for the orchestrator's
# own clone.
_canonical_sanitize_for_weaviate_class: Optional[Callable[[str], str]]
_canonical_class_prefix: Optional[Callable[[str], str]]
try:
    _env_root_for_vco = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    if _env_root_for_vco:
        _vco_lib_parent = Path(_env_root_for_vco)
    else:
        # kg_access.py lives at <root>/claude_mcp_servers/scripts/ — the
        # vco_lib package parent is two levels up.
        _vco_lib_parent = Path(__file__).resolve().parent.parent.parent
    if str(_vco_lib_parent) not in sys.path:
        sys.path.insert(0, str(_vco_lib_parent))
    from vco_lib.codegraph_naming import (
        canonical_class_prefix as _canonical_class_prefix,
        sanitize_for_weaviate_class as _canonical_sanitize_for_weaviate_class,
    )
except Exception:  # pragma: no cover (rare bare / half-install layout)
    _canonical_sanitize_for_weaviate_class = None
    _canonical_class_prefix = None

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
    """Sanitize a project name into the canonical Weaviate KG-collection
    prefix — the underscore-DROPPING PascalCase rule.

    **Canonical rule** (cross-language SSOT, locked 2026-05-25 by cr-b2,
    re-affirmed here 2026-07-10 by v0.2.77 7a-bis):
      1. Split on any non-alphanumeric run (``[^A-Za-z0-9]+``).
      2. PascalCase each surviving part (uppercase first char, keep rest).
      3. Concatenate (NO joiner — no underscore between parts).
      4. If nothing survives OR the result starts with a non-letter,
         fall back to ``"vct"`` (Weaviate uppercases on POST regardless).

    This is the SAME rule the WRITER uses to name the
    ``<prefix>_KnowledgeGraph`` collections
    (``vco_lib.codegraph_naming.sanitize_for_weaviate_class``, re-exported
    as ``vco_lib.project_init.sanitize_for_weaviate_class``). Converged in
    v0.2.77 7a-bis: the pre-7a-bis implementation used the divergent
    ``re.sub([^a-zA-Z0-9_], "_") + upper-first`` rule (underscore-
    PRESERVING), so a spaced project name like ``'My Cool App'`` resolved
    to ``My_Cool_App`` and this CLI reader fanned out to
    ``My_Cool_App_KnowledgeGraph`` — a DIFFERENT collection than the
    writer-created ``MyCoolApp_KnowledgeGraph`` (silent 0-results, or a
    wrong-tenant read if a legacy underscore collection happened to
    exist). Launcher-managed access lists carry canonical prefixes so were
    safe; hand-built ``VCT_*_ACCESS_LIST`` values with raw names were not.

    Idempotent — passing in an already-canonical prefix returns it
    unchanged, and the legacy underscore form COLLAPSES onto the canonical
    prefix (``'My_Cool_App'`` → ``'MyCoolApp'``). Because the rule is
    idempotent on the underscore form there is no two-collection ambiguity
    to disambiguate: converging onto this rule resolves both the raw and
    the legacy-underscore forms onto the single writer-created collection.

    **Byte-behaviour mirror** of
    ``weaviate_mcp.server._sanitize_collection_prefix`` — the two copies
    are kept identical (parity pinned by
    ``tests/test_kg_access_sanitizer_convergence.py``). ``kg_access`` is
    deliberately pure-stdlib + cheaply importable from a bare user-project
    layout, so the ``vco_lib`` delegation below is best-effort: it is used
    when importable, otherwise the inline fallback (behaviour-identical to
    ``sanitize_for_weaviate_class``) keeps the CLI working without the MCP
    venv.
    """
    canonical = _canonical_sanitize_for_weaviate_class
    if canonical is not None:
        try:
            return canonical(name)
        except Exception:
            # Defensive: never let a sanitiser exception break a CLI read.
            pass

    # Inline fallback — behaviour-identical to
    # ``vco_lib.codegraph_naming.sanitize_for_weaviate_class``. Kept so the
    # helper stays importable/usable on a bare layout with no vco_lib on
    # sys.path.
    base = name or ""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", base) if p]
    if not parts:
        return "vct"
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    if not pascal or not pascal[0].isalpha():
        return "vct"
    return pascal


def code_sanitize_collection_prefix(name: str) -> str:
    """CODE-GRAPH-ONLY prefix sanitizer — the underscore-PRESERVING
    ``canonical_class_prefix`` rule the ANALYZER writes ``Code*`` classes
    with (and launcher.db ``project_codegraph_bindings.collection_prefix``
    records).

    DELIBERATELY DIFFERENT from ``sanitize_collection_prefix`` (the
    underscore-DROPPING KG/Development/Diagrams rule): the code-graph rule
    PascalCases whitespace-separated words but PRESERVES underscores and
    maps other non-``[A-Za-z0-9_]`` characters to a single underscore —
    ``'Foo-Bar'`` → ``'Foo_Bar'``, ``'Camel_Case'`` → ``'Camel_Case'``,
    while the KG rule drops both to ``'FooBar'`` / ``'CamelCase'``. Routing
    the code-graph collection-name construction through THIS resolver keeps
    the CLI reader's class name equal to the writer's for ANY project name,
    including underscored/hyphenated ones. Mirror of
    ``weaviate_mcp.server._code_sanitize_collection_prefix`` (v0.2.74
    BLOCKER-1); parity pinned by
    ``tests/test_kg_access_sanitizer_convergence.py``.

    Fallback: when ``canonical_class_prefix`` isn't importable
    (bare/half-install), fall back to the underscore-DROPPING
    ``sanitize_collection_prefix`` — correct only for non-underscored /
    non-hyphenated names, but keeps the CLI working rather than crashing
    (same posture as the MCP mirror).
    """
    canonical = _canonical_class_prefix
    if canonical is not None:
        try:
            return canonical(name)
        except Exception:
            # ``canonical_class_prefix`` RAISES on names that can't form a
            # valid class prefix (empty / leading-digit). Never let that
            # break a CLI read — fall through to the dropping rule, which
            # returns the ``"vct"`` sentinel for such input.
            pass
    # Half-install fallback: the dropping rule (parity holds for names with
    # no underscore/hyphen; those degrade to the pre-fix behaviour until
    # vco_lib is refreshed).
    return sanitize_collection_prefix(name)


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

    # v0.2.74 BLOCKER-1 / v0.2.77 7a-bis: code-graph fan-out uses the
    # underscore-PRESERVING sanitizer (matches the analyzer's write class),
    # NOT the diagrams/KG dropping rule. Mirror of the `_code_sanitize_*`
    # loop in `server.search_code_graph`.
    self_prefix = code_sanitize_collection_prefix(self_project)
    seen_prefixes: set[str] = {self_prefix}
    prefixes: list[tuple[str, str]] = [(self_prefix, self_project)]
    for peer in parse_csv_env(env_var):
        peer_prefix = code_sanitize_collection_prefix(peer)
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
    "code_sanitize_collection_prefix",
    "kg_peer_collections",
    "kg_collections_to_search",
    "code_graph_collections_to_query",
]
