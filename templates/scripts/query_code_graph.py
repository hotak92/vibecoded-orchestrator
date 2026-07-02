#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Query Code Graph

Command-line interface for querying the code graph with semantic and structural search.

Usage:
    # Semantic search
    python query_code_graph.py search "authentication middleware"
    python query_code_graph.py search "file upload" --collection CodeFunction --limit 3
    python query_code_graph.py search "HTTP calls to external API" --collection CodeInteraction

    # Find similar code
    python query_code_graph.py similar "api.auth.validate_token" --limit 5

    # Structural queries
    python query_code_graph.py structure dependencies "api/routes.py"
    python query_code_graph.py structure callers "utils.validate_input"
    python query_code_graph.py structure methods "api.UserManager"
    python query_code_graph.py structure extends "api.BaseHandler"
    python query_code_graph.py structure interactions "api/routes.py"       # all outbound calls from a module
    python query_code_graph.py structure interactions "api.users.create_user"  # calls from a function
"""

import argparse
import json
import os
import re
import sys
import requests
from pathlib import Path
from typing import Optional, List

try:
    import weaviate
    from weaviate.classes.query import Filter, MetadataQuery, QueryReference
except ImportError:
    print("Error: weaviate-client not installed. Install with: pip install weaviate-client", file=sys.stderr)
    sys.exit(1)

# Import the shared rank-tier formatter from the MCP server module so the
# CLI emits results identically to `search_code_graph` MCP. The script
# lives at .claude/scripts/query_code_graph.py and the MCP module at
# claude_mcp_servers/weaviate_mcp/server.py — derive the project root
# from this file's location so the layout works without hardcoded paths.
#
# v0.2.37 (Gap 6c): when this script ships into a 3rd-party project
# via install-bundle, parents[2] resolves to the USER PROJECT root,
# which has no `claude_mcp_servers/` directory. Honor
# $VCT_ORCHESTRATOR_ROOT (set by install-bundle's .claude/env writer)
# and $VCT_INSTALL_ROOT (legacy alias) before falling back to the
# script-relative location. Mirrors sync_knowledge_graph.py's
# `_resolve_mcp_servers_dir()` pattern.
# A1 (v0.2.38): weaviate_mcp is pip-installed as an editable package by
# install.py, so `from weaviate_mcp.server import ...` works without a
# sys.path entry.  _MCP_SERVERS_PATH is still resolved for the `scripts/`
# subdirectory (used below by kg_access.py via the P1-D block) and for the
# fallback error message.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MCP_SERVERS_PATH: Optional[Path] = None
for _env_var in ("VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT"):
    _env_root = os.environ.get(_env_var, "").strip()
    if _env_root:
        _candidate = Path(_env_root) / "claude_mcp_servers"
        if _candidate.is_dir():
            _MCP_SERVERS_PATH = _candidate
            break
if _MCP_SERVERS_PATH is None:
    _MCP_SERVERS_PATH = _PROJECT_ROOT / "claude_mcp_servers"

# v0.2.72 (P1/P2/P4 CLI parity): besides the rank/tier FORMATTERS, import the
# shared pipeline ADAPTER factories (`make_code_collapse_fn` / `make_code_tier_fn`)
# from the SERVER module. Both this CLI and `search_code_graph` (MCP) build their
# `run_code_retrieval_pipeline` collapse_fn/tier_fn from THESE factories, so the
# two surfaces cannot diverge on collapse/tier behaviour (the hard invariant).
# Do NOT reimplement them per-surface.
try:
    from weaviate_mcp.server import (
        _format_code_result_by_rank,
        _format_code_result_by_tier,
        _format_code_result_ref,
        _self_project_chunk_fetcher,
        make_code_collapse_fn,
        make_code_tier_fn,
        CODE_SIBLINGS_RANK_1,
        CODE_SIBLINGS_RANK_2,
    )
except ImportError as exc:  # pragma: no cover — surface a clear error
    print(
        f"Error: could not import weaviate_mcp.server rank-tier helper: {exc}\n"
        f"  Expected module at: {_MCP_SERVERS_PATH}/weaviate_mcp/server.py\n"
        "  Ensure install.py has run (pip install -e claude_mcp_servers/) or set\n"
        "  VCT_ORCHESTRATOR_ROOT to the orchestrator clone root.",
        file=sys.stderr,
    )
    sys.exit(1)

# P1-D (2026-05-08): centralized access-matrix helper. Resolved via
# $VCT_ORCHESTRATOR_ROOT (the orchestrator clone is where
# claude_mcp_servers/scripts/kg_access.py lives) with an in-tree
# fallback for the orchestrator self path. The graceful fallback is a
# self-only no-op so the CLI keeps working on a hand-edited venv.
try:
    # VCO-REWIRE-BEGIN: orchestrator-root-resolution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "claude_mcp_servers" / "scripts"))
    # VCO-REWIRE-END: orchestrator-root-resolution
    from kg_access import code_graph_collections_to_query as _code_graph_collections_to_query  # type: ignore[import-not-found]
except Exception:
    def _code_graph_collections_to_query(  # type: ignore[no-redef]
        self_project: str,
        bases=None,
    ):
        bases_t = tuple(bases) if bases is not None else (
            "CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction",
        )
        if not self_project:
            return [(b, "") for b in bases_t]
        # Mirror sanitize_collection_prefix's contract for the fallback
        # path. Best-effort only — this branch fires when the helper
        # isn't on sys.path, which means the user is in a degraded
        # environment anyway.
        import re as _re
        prefix = _re.sub(r"[^a-zA-Z0-9_]", "_", self_project)
        if prefix and not prefix[0].isupper():
            prefix = prefix[0].upper() + prefix[1:]
        return [(f"{prefix}_{b}", self_project) for b in bases_t]

# Load MCP config
CONFIG_PATH = Path.home() / ".claude/workflow/config/mcp-config.json"

if CONFIG_PATH.exists():
    config = json.loads(CONFIG_PATH.read_text())
    WEAVIATE_URL = config["weaviate"]["url"]
    GRPC_PORT = config["weaviate"]["grpc_port"]
    OLLAMA_URL = config.get("ollama", {}).get("url", "http://localhost:11435")
else:
    WEAVIATE_URL = "http://localhost:8081"
    GRPC_PORT = 50052
    OLLAMA_URL = "http://localhost:11435"


def _sanitize_collection_prefix(name: str) -> str:
    """Sanitize project name for use as Weaviate collection prefix."""
    import re
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if sanitized and not sanitized[0].isupper():
        sanitized = sanitized[0].upper() + sanitized[1:]
    return sanitized


def _collection_name(base: str, project: str = None) -> str:
    """Return per-project collection name if project is set."""
    if not project:
        return base
    return f"{_sanitize_collection_prefix(project)}_{base}"


# Code embedding configuration — v0.2.18: centralised via
# EmbeddingService. Pre-v0.2.18 read CODE_EMBED_BACKEND / CODE_EMBED_-
# SERVICE_URL / CODE_EMBED_MODEL directly and hardcoded the slot.
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")

# Import EmbeddingService — graceful fallback for half-installed venvs.
try:
    _vco_lib_parent = _PROJECT_ROOT
    if str(_vco_lib_parent) not in sys.path:
        sys.path.insert(0, str(_vco_lib_parent))
    from vco_lib.embedding_service import (
        EmbeddingService,
        NoEmbeddingBackendError,
    )
    HAS_EMBEDDING_SERVICE = True
except Exception:
    HAS_EMBEDDING_SERVICE = False
    EmbeddingService = None  # type: ignore[assignment]
    NoEmbeddingBackendError = Exception  # type: ignore[assignment]


_cached_embedding_service: "EmbeddingService | None" = None


def _get_or_create_embedding_service():
    """Lazy-construct EmbeddingService, cached for the CLI's lifetime."""
    global _cached_embedding_service
    if _cached_embedding_service is not None:
        return _cached_embedding_service
    if not HAS_EMBEDDING_SERVICE:
        return None
    try:
        _cached_embedding_service = EmbeddingService.for_project()
        return _cached_embedding_service
    except Exception as e:
        print(f"⚠️  EmbeddingService construction failed: {e}", file=sys.stderr)
        return None


def _active_code_vector_slot() -> str:
    """Return the active code-vector slot. Falls back to codesage_embed
    when EmbeddingService isn't available (pre-v0.2.18 default)."""
    svc = _get_or_create_embedding_service()
    if svc is None:
        return "codesage_embed"
    return svc.code_vector_slot


# v0.2.72 T-FLOOR (P1) + integration: the two-stage per-slot floor table,
# resolvers AND the retrieval pipeline live in the SINGLE SHARED home
# ``weaviate_mcp.code_ranking`` so the CLI path and the MCP path
# (server.py::search_code_graph) cannot diverge. This CLI already imports shared
# helpers from weaviate_mcp via the pip-editable install (see the
# ``from weaviate_mcp.server import ...`` block above) — mirror that style here.
#
# ``CODE_FLOOR_BY_SLOT`` (measured CodeSage 0.16/0.22, jina 0.16/0.22, qwen3
# conservative 0.20/0.30) is the (retrieval_floor, post_rerank_floor) contract;
# ``resolve_retrieval_floor`` / ``resolve_post_rerank_floor`` own the env-
# override + empty-string-coercion discipline.
#
# MUST MATCH (3-way mirror): the floor VALUES in code_ranking.py are the
# contract between this CLI, the MCP server
# (claude_mcp_servers/weaviate_mcp/server.py::search_code_graph), and any hook
# that pre-filters code-graph results. v0.2.72 moved the table to the shared
# module so the three surfaces cannot drift; changing a value re-opens the
# cross-scale-floor bug unless every surface + the experiment re-run agree.
#
# Hard-required (no fallback reimplementation): reimplementing the pipeline or
# the floor table per-surface is exactly the divergence this module exists to
# prevent. If `weaviate_mcp.server` imported above, `weaviate_mcp.code_ranking`
# is importable too (server.py imports it at module scope), so this branch can
# only fail alongside the server import — which already sys.exit(1)s with the
# remediation message.
try:
    from weaviate_mcp.code_ranking import (
        CODE_FLOOR_BY_SLOT,
        resolve_post_rerank_floor,
        resolve_retrieval_floor,
        run_code_retrieval_pipeline,
    )
except ImportError as exc:  # pragma: no cover — surface a clear error
    print(
        f"Error: could not import weaviate_mcp.code_ranking pipeline: {exc}\n"
        f"  Expected module at: {_MCP_SERVERS_PATH}/weaviate_mcp/code_ranking.py\n"
        "  Ensure install.py has run (pip install -e claude_mcp_servers/) or set\n"
        "  VCT_ORCHESTRATOR_ROOT to the orchestrator clone root.",
        file=sys.stderr,
    )
    sys.exit(1)


def generate_code_embedding(text: str) -> Optional[List[float]]:
    """Generate code embedding via EmbeddingService.

    v0.2.18: routes through EmbeddingService.embed_code (which picks
    CodeEmbed / Ollama / OpenAI from env). Falls back to direct
    CodeEmbed-service HTTP call when the service isn't available.
    """
    svc = _get_or_create_embedding_service()
    if svc is not None:
        try:
            return svc.embed_code(text)
        except Exception as e:
            print(f"⚠️  EmbeddingService.embed_code failed: {e}", file=sys.stderr)
    # Legacy fallback: direct CodeEmbed HTTP call.
    try:
        response = requests.post(
            f"{CODE_EMBED_SERVICE_URL}/api/embeddings",
            json={"model": "", "prompt": text},
            timeout=60,
        )
        if response.status_code == 200:
            return response.json()["embedding"]
        print(f"❌ Embedding generation failed: HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"❌ Embedding error: {e}")
        return None


class CodeGraphQuery:
    """Query interface for code graph."""

    def __init__(self, project: Optional[str] = None):
        self.project = project
        self.client = None

    def _coll(self, base: str) -> str:
        """Get per-project collection name."""
        return _collection_name(base, self.project)

    def connect(self):
        """Connect to Weaviate."""
        try:
            self.client = weaviate.connect_to_custom(
                http_host='localhost',
                http_port=8081,
                http_secure=False,
                grpc_host='localhost',
                grpc_port=50052,
                grpc_secure=False
            )
            return True
        except Exception as e:
            print(f"❌ Failed to connect to Weaviate: {e}", file=sys.stderr)
            return False

    def _resolve_anchor_props(self, anchor: str) -> Optional[dict]:
        """Resolve an ``--anchor`` value (edited file path OR symbol full_name)
        to the anchor entity's Weaviate props.

        The anchor is the edit/grep seed the hook path passes so the shared
        pipeline's relationship rerank (call-linked / same-module /
        shared-type — code_ranking.rerank_score) fires relative to it. Queries
        CodeFunction + CodeClass in the SELF project only (peers are search
        targets, not anchors), attempting in priority order:
          1. full_name == anchor          (exact symbol)
          2. file_path == anchor          (exact path, as passed)
          3. file_path LIKE *<tail>       (path-shaped anchor — an absolute
             editor path still hits the analyzer's repo-relative file_path)
          4. full_name LIKE *.<anchor>    (bare symbol — qualified leaf)
        Among matches, the lowest chunk_num wins (entity-level props like
        call_names / type_uses are replicated per chunk row).

        Failure-soft by contract: empty anchor / no client / no match / any
        Weaviate error → None (pure semantic ordering, byte-identical to a
        direct MCP call). Never raises into the search path.
        """
        if not anchor or self.client is None:
            return None
        try:
            anchor = str(anchor).strip()
            if not anchor:
                return None
            attempts = [
                Filter.by_property("full_name").equal(anchor),
                Filter.by_property("file_path").equal(anchor),
            ]
            norm = anchor.replace("\\", "/")
            if "/" in norm:
                parts = [p for p in norm.split("/") if p]
                tail = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")
                if tail:
                    attempts.append(Filter.by_property("file_path").like(f"*{tail}"))
            elif all(ch.isalnum() or ch in "_.:" for ch in anchor):
                # Bare / qualified symbol: also match full_names ending in the
                # anchor's LEAF (e.g. anchor "validate_token" hits
                # "api.auth.validate_token"; Rust "mod::my_fn" leaf-matches on
                # "my_fn" — split on `.` OR `::`, F10 pre-gate audit).
                #
                # F10 tightening: SKIP the bare-leaf LIKE fallback for
                # over-generic leaves (len < 4 or a ubiquitous name like
                # "run"/"main") — `LIKE *.run` matches half the codebase and
                # anchors the rerank to an arbitrary entity. All failure
                # paths still resolve to None (pure semantic ordering).
                leaf = re.split(r"::|\.", anchor)[-1]
                _generic = {"run", "main", "init", "new", "get", "set"}
                if len(leaf) >= 4 and leaf.lower() not in _generic:
                    attempts.append(Filter.by_property("full_name").like(f"*.{leaf}"))
                    if "::" in anchor:
                        # Rust-style storage may qualify with `::` too.
                        attempts.append(Filter.by_property("full_name").like(f"*::{leaf}"))

            for flt in attempts:
                best: Optional[dict] = None
                best_chunk = 0
                for base in ("CodeFunction", "CodeClass"):
                    try:
                        coll_obj = self.client.collections.get(self._coll(base))
                        q = flt
                        if self.project:
                            q = flt & Filter.by_property("project").equal(self.project)
                        resp = coll_obj.query.fetch_objects(filters=q, limit=4)
                    except Exception:
                        continue
                    for obj in resp.objects:
                        p = obj.properties or {}
                        try:
                            cn = int(p.get("chunk_num") or 0)
                        except (TypeError, ValueError):
                            cn = 0
                        if best is None or cn < best_chunk:
                            best, best_chunk = p, cn
                if best is not None:
                    return best
            return None
        except Exception:
            return None

    def search_by_concept(self, query: str, collection: str = "CodeFunction", limit: int = 5, detail: str = "auto", hook_format: bool = False, anchor: str = None, exclude_file: str = None):
        """Semantic search for code by concept.

        P1-D (2026-05-08): when ``self.project`` is set, fan out across
        self + every peer in ``VCT_CODE_GRAPH_ACCESS_LIST``. Each
        per-collection query over-fetches ``2*limit`` candidates (the
        shared-pipeline pool, matching the MCP); results are merged and
        ranked by the shared pipeline before truncation. When
        ``self.project`` is unset (cross-tenant "search all projects"
        path), behaviour is unchanged: a single bare-collection query.

        v0.2.72 (P1/P2/P4 CLI parity): ranking runs through the SHARED
        ``run_code_retrieval_pipeline`` (weaviate_mcp.code_ranking) with the
        SAME adapter factories (`make_code_collapse_fn` / `make_code_tier_fn`
        from weaviate_mcp.server) and the SAME per-slot floor resolution the
        MCP uses — two-stage floor + relationship rerank + multi-chunk
        collapse + score-tier allocation. Rendering matches the MCP loop:
        pipeline-tiered candidates go through `_format_code_result_by_tier`
        (1/3/7-chunk assembly); explicit detail goes through the rank-based
        `_format_code_result_by_rank`.

        detail:
          "auto"   — score-tier verbosity from the shared pipeline's tier_fn
                     (summary / single_chunk / three_chunks / full per score)
          "titles" — metadata refs for every result
          "full"   — full untruncated for every result

        anchor: edited file path OR symbol full_name (the hook path passes
          this). Resolved to the anchor entity's Weaviate props and passed as
          ``anchor_props`` so the pipeline's relationship boost (call-linked /
          same-module / shared-type — code_ranking.rerank_score) fires
          relative to it. None / unresolvable → anchor_props=None → pure
          semantic ordering, byte-identical to a direct MCP call. That is
          data, not behaviour — the pipeline code is identical either way.

        exclude_file (B2, design audit): drop candidates whose source file IS
          this path BEFORE the pipeline trims to `limit`. The Read/Edit hook
          passes the edited file as BOTH anchor and exclude — the anchor's
          +0.03 same-file boost used to promote the edited file's own entities
          into the top-2, and the hook's old line-wise `grep -v` then stripped
          only their header lines, leaving orphaned body lines. Filtering here
          (pre-trim, on the normalized candidates — the pipeline itself stays
          pure) keeps the trimmed top-K full of OTHER files' context. The MCP
          body has no exclude concept (no edited-file context on a direct
          tool call) — documented there as deliberately N/A, not drift.

        hook_format: when True, emit a stable per-result header line
          'CODE: <full_name> | <collection> | distance=<d>' (with optional
          ' | source=<peer>' suffix for cross-tenant fan-out) and skip the
          human banner. The pre-edit hook uses this header to dedup repeat
          injections by entity name across a session via the regex
          ``^(KG|CODE):\\ (.+)$``. Body lines follow as ordinary indented
          content; blank lines separate blocks.
        """
        try:
            # Generate query embedding
            query_embedding = generate_code_embedding(query)
            if not query_embedding:
                print("❌ Failed to generate query embedding")
                return

            # Build per-(collection_name, project_filter_value) fan-out
            # list. Self comes first; peers appear in
            # VCT_CODE_GRAPH_ACCESS_LIST order. The helper handles
            # dedupe + the cross-tenant fallback.
            pairs = _code_graph_collections_to_query(
                self_project=self.project or "",
                bases=(collection,),
            )

            # Header emit MOVED to AFTER `top` is built — see below.
            # v0.2.21 (audit fix, 2026-05-20): emitting the banner BEFORE
            # the query means a zero-result run still prints the
            # "🔍 Semantic search... Project filter: X" header, which the
            # pre-edit hook captures into $CODE_RESULT and treats as a
            # non-empty result (HAS_CODE=1), bypassing its own 0-result
            # short-circuit and injecting useless context every Edit.
            # See docs/HOOK_TOKEN_AUDIT_2026-05-20.md §3 and the
            # query_code_graph.py side of the fix.

            # Fan-out: query each (collection, filter) pair, merge into one
            # candidate pool. v0.2.72 (P1/P2): over-fetch 2N per collection so
            # the shared `run_code_retrieval_pipeline` has a pool to floor-cull
            # + rerank + collapse before trimming to `limit`. Matches the MCP
            # over-fetch (server.py::search_code_graph).
            _fetch_limit = max(1, 2 * limit)
            merged: list[tuple[float, str, object]] = []  # (distance, source_label, obj)
            for coll_name, project_filter in pairs:
                try:
                    coll = self.client.collections.get(coll_name)
                except Exception:
                    # Peer never indexed this base — skip silently.
                    continue
                nv_kwargs = dict(
                    near_vector=query_embedding,
                    limit=_fetch_limit,
                    return_metadata=MetadataQuery(distance=True),
                    target_vector=_active_code_vector_slot(),
                )
                if project_filter:
                    nv_kwargs["filters"] = Filter.by_property("project").equal(project_filter)
                try:
                    response = coll.query.near_vector(**nv_kwargs)
                except Exception:
                    # Collection exists but query failed (e.g. wrong
                    # vector dim from a stale schema). Skip and keep
                    # going so peer issues don't break self queries.
                    continue
                for obj in response.objects:
                    distance = obj.metadata.distance if obj.metadata.distance is not None else 1.0
                    merged.append((distance, project_filter or "", obj))

            # v0.2.72 (P1/P2/P3/P4 CLI parity): normalise the fan-out into the
            # shared candidate shape and run the SAME
            # `run_code_retrieval_pipeline` the MCP calls
            # (server.py::search_code_graph) with the SAME adapter factories —
            # two-stage per-slot floor (retrieval 0.16 / post-rerank 0.22 for
            # CodeSage) + relationship rerank + multi-chunk collapse +
            # score-tier allocation — then trim to `limit`. This replaced the
            # v0.2.21/v0.2.70 single-stage floor break-loop; floor history
            # lives in weaviate_mcp/code_ranking.py. The ONLY per-surface
            # difference is `anchor_props`: the hook path passes a resolved
            # anchor entity (edited file / grep symbol) so the P2 relationship
            # boost fires; a direct MCP call passes None. That is data, not
            # behaviour — the pipeline code is identical (the hard
            # non-divergence invariant).
            candidates: list[dict] = []
            for distance, source, obj in merged:
                candidates.append({
                    "_c": collection,
                    "_s": max(0.0, 1.0 - distance),
                    "_d": distance,
                    "_p": obj.properties,
                    "_src": source,
                })

            # B2 (design audit): cull the excluded file's own entities BEFORE
            # the pipeline (pre-trim), so the +0.03 same-file anchor boost
            # cannot fill the top-K with the file being edited. Pipeline stays
            # pure — this is caller-side candidate normalisation. Matching is
            # separator-normalised and boundary-aware so an absolute editor
            # path still culls the analyzer's repo-relative file_path.
            if exclude_file:
                _ex = str(exclude_file).replace("\\", "/").strip()

                def _is_excluded(props: dict) -> bool:
                    fp = (props.get("file_path") or props.get("path") or "")
                    fp = str(fp).replace("\\", "/")
                    if not fp or not _ex:
                        return False
                    return (
                        fp == _ex
                        or _ex.endswith("/" + fp)
                        or fp.endswith("/" + _ex)
                    )

                candidates = [
                    c for c in candidates if not _is_excluded(c.get("_p") or {})
                ]

            try:
                _slot = _active_code_vector_slot()
            except Exception:
                _slot = "codesage_embed"

            anchor_props = self._resolve_anchor_props(anchor) if anchor else None

            # Score-tier allocation only runs in "auto" mode; explicit detail
            # values want uniform output, so we skip the budget allocator and
            # let the format loop honour `detail` directly (tier_fn=None → no
            # `_tier` set). Same rule as the MCP. F4 (pre-gate audit): the
            # tier `min` gate DERIVES from the resolved post-rerank floor so a
            # GUI/env floor override changes what renders in auto mode —
            # identical wiring in the MCP (the hard invariant).
            _post_floor = resolve_post_rerank_floor(_slot)
            _tier_fn = make_code_tier_fn(min_gate=_post_floor) if detail == "auto" else None
            survivors = run_code_retrieval_pipeline(
                candidates,
                retrieval_floor=resolve_retrieval_floor(_slot),
                post_rerank_floor=_post_floor,
                anchor_props=anchor_props,
                limit=limit,
                collapse_fn=make_code_collapse_fn(),
                tier_fn=_tier_fn,
                key_fields=("file_path", "full_name"),
            )

            # Rebuild `top` in the (distance, source, candidate) shape the
            # downstream banner + format loop consume. `_src` / `_d` were
            # preserved through the pipeline (it never strips caller keys).
            top = [(c.get("_d", 1.0), c.get("_src", ""), c) for c in survivors]

            # Sibling fetcher: closes over self.client + self.project so
            # the shared helper stays Weaviate-agnostic. Only invoked for
            # top-2 ranks in auto mode by `_format_code_result_by_rank`.
            # Note: siblings come from the SELF project only — peer
            # projects don't expose their full source files for the
            # sibling enrichment path. This matches the MCP behaviour
            # where _project_collection scopes the sibling lookup.
            def _sibling_fetcher(file_path: str, hit_start_line: int, max_total: int, exclude_full_name: str) -> list[dict]:
                if not file_path or max_total <= 1:
                    return []
                try:
                    fn_coll = self.client.collections.get(self._coll("CodeFunction"))
                    cls_coll = self.client.collections.get(self._coll("CodeClass"))
                except Exception:
                    return []
                collected = []  # (start_line, c_name, properties)
                for coll_obj, c_name in ((fn_coll, "CodeFunction"), (cls_coll, "CodeClass")):
                    try:
                        sib_filter = Filter.by_property("file_path").equal(file_path)
                        if self.project:
                            sib_filter = sib_filter & Filter.by_property("project").equal(self.project)
                        # v0.2.46 V46-D: limit=64 is an intentional top-N cap
                        # (siblings are sorted by proximity AFTER the fetch).
                        # We don't emit a per-call truncation signal here
                        # because the caller (`_format_code_result_by_rank`)
                        # already only uses up to `max_total - 1` results.
                        SIBLING_FETCH_LIMIT = 64
                        sib_resp = coll_obj.query.fetch_objects(filters=sib_filter, limit=SIBLING_FETCH_LIMIT)
                        for obj in sib_resp.objects:
                            sp = obj.properties or {}
                            if sp.get("full_name") == exclude_full_name:
                                continue
                            sl = sp.get("start_line")
                            try:
                                sl_int = int(sl) if sl is not None else 0
                            except (TypeError, ValueError):
                                sl_int = 0
                            collected.append((sl_int, c_name, sp))
                    except Exception:
                        continue
                if not collected:
                    return []
                collected.sort(key=lambda t: abs(t[0] - hit_start_line))
                picked = collected[: max_total - 1]
                picked.sort(key=lambda t: t[0])
                siblings = []
                for sl_int, c_name, sp in picked:
                    ref = _format_code_result_ref(c_name, sp)
                    ref["sibling"] = True
                    ref["start_line"] = sl_int
                    ref["collection"] = c_name
                    siblings.append(ref)
                return siblings

            # v0.2.21 audit fix: emit the per-result banner ONLY when there
            # is at least one result. The pre-fix code emitted the
            # "🔍 Semantic search... Project filter: X / Found 0 results:"
            # block BEFORE the query ran, so even zero-result runs produced
            # non-empty stdout — the pre-edit hook's HAS_CODE=0 guard then
            # never fired and every Edit injected ~300 bytes of useless
            # "Found 0 results" context.
            #
            # For zero-result runs we now emit a SHORT identifying line
            # (one for hook-format, one human-readable). This is deliberate
            # over emitting nothing: per user direction 2026-05-20,
            # "multiple empty might be worse than multiple short texts to
            # let [the model] understand where it's coming from". The
            # model sees the hook fired AND knows the search scope, so it
            # can judge whether the absence of results is meaningful for
            # the task at hand. Cost: ~50-80 bytes per Edit vs ~300 bytes
            # for the pre-fix banner-on-empty, vs 0 bytes for full
            # suppression that would leave the model wondering.
            if not hook_format:
                if top:
                    print(f"\n🔍 Semantic search in {collection}: '{query}'  (detail={detail})")
                    if self.project:
                        peer_count = len(pairs) - 1
                        if peer_count > 0:
                            peer_names = ", ".join(p_filter for _, p_filter in pairs[1:] if p_filter)
                            print(f"   Project filter: {self.project} (+ {peer_count} peer(s): {peer_names})")
                        else:
                            print(f"   Project filter: {self.project}")
                    print(f"   Found {len(top)} results:\n")
                else:
                    # One short line, no banner block, so caller (human or
                    # hook) sees the search WAS attempted and its scope.
                    proj_part = f" project={self.project}" if self.project else ""
                    print(f"   No matches in {collection} for '{query}'{proj_part}")
            else:
                # --hook-format path. On empty, emit a single stable-format
                # line the pre-edit hook can capture; its in-session dedup
                # keyed by "title" treats this as a normal entry and so
                # rate-limits identical (collection, project, query) tuples.
                # Distinct (collection, project, query) → distinct dedup
                # keys → unique short lines reach the model.
                if not top:
                    proj_part = f" | project={self.project}" if self.project else ""
                    print(f"CODE: no-results | collection={collection}{proj_part} | query='{query}'")

            # Code-chunk fetcher: closes over self.client + self._coll so the
            # shared tier formatter stays Weaviate-agnostic. Mirrors the MCP's
            # `_fetch_code_chunks` closure (keys on full_name — code's node
            # identity; CodeFunction + CodeClass are the only chunked code
            # collections). Returns [] on any failure or for a single-chunk
            # entity; only invoked for the three_chunks / full tiers.
            def _code_chunk_fetcher(full_name: str, hit_chunk: int, total: int, max_chunks: int) -> list[dict]:
                if not full_name or total <= 1 or max_chunks <= 1:
                    return []
                collected_chunks: list[tuple[int, dict]] = []
                for base in ("CodeFunction", "CodeClass"):
                    try:
                        coll_obj = self.client.collections.get(self._coll(base))
                        flt = Filter.by_property("full_name").equal(full_name)
                        if self.project:
                            flt = flt & Filter.by_property("project").equal(self.project)
                        resp = coll_obj.query.fetch_objects(filters=flt, limit=max(total, max_chunks) + 4)
                        for obj in resp.objects:
                            cp = obj.properties or {}
                            cn = cp.get("chunk_num", 0) or 0
                            try:
                                collected_chunks.append((int(cn), cp))
                            except (TypeError, ValueError):
                                collected_chunks.append((0, cp))
                    except Exception:
                        continue
                if not collected_chunks:
                    return []
                # Centre a window of max_chunks around the hit, ordered by chunk_num.
                collected_chunks.sort(key=lambda t: abs(t[0] - (hit_chunk or 0)))
                picked = collected_chunks[:max_chunks]
                picked.sort(key=lambda t: t[0])
                return [cp for _, cp in picked]

            # Render each survivor through the shared helpers — SAME split as
            # the MCP loop: in "auto" mode every candidate carries a `_tier`
            # (from the shared pipeline's tier_fn) → score-tier renderer
            # (`_format_code_result_by_tier`, 1/3/7-chunk assembly); explicit
            # "titles"/"full" (tier_fn was None → no `_tier`) → the rank-based
            # formatter honours `detail` uniformly. i is 0-based for the
            # helper; human output uses 1-based numbering.
            for i, (distance, source, cand) in enumerate(top):
                props = cand.get("_p") or {}
                score = cand.get("_s", 0.0)
                tier = cand.get("_tier")
                if tier is not None:
                    rendered = _format_code_result_by_tier(
                        props,
                        cand.get("_c", collection),
                        tier,
                        score=score,
                        distance=distance,
                        # F5: peer rows must not assemble chunks from the SELF
                        # project's collections — the shared gate (imported
                        # from weaviate_mcp.server, same as the MCP loop)
                        # returns None so the tier degrades to single_chunk.
                        chunk_fetcher=_self_project_chunk_fetcher(
                            cand, self.project, _code_chunk_fetcher,
                        ),
                    )
                else:
                    rendered = _format_code_result_by_rank(
                        props,
                        cand.get("_c", collection),
                        rank=i,
                        detail=detail,
                        score=score,
                        distance=distance,
                        sibling_fetcher=_sibling_fetcher,
                    )
                # Source-project annotation for fan-out clarity. Empty
                # for self-only queries (the pre-P1-D shape).
                src_label = ""
                src_suffix = ""
                if self.project and source and source != self.project:
                    src_label = f"  [peer:{source}]"
                    src_suffix = f" | source={source}"
                self._print_code_result(
                    rendered, rank=i, hook_format=hook_format,
                    src_label=src_label, src_suffix=src_suffix,
                )

        except Exception as e:
            print(f"❌ Search error: {e}", file=sys.stderr)

    @staticmethod
    def _print_code_result(rendered: dict, rank: int, hook_format: bool,
                           src_label: str = "", src_suffix: str = "") -> None:
        """Print one rank-tier-formatted code-graph result.

        Both surfaces (hook + human) walk the same rendered dict so the
        body content is identical; only the per-block header differs.

        Hook format: ``CODE: <full_name> | <collection> | distance=<d>``
        first line (with optional ``| source=<peer>`` suffix), body lines
        indented two spaces, terminating blank line. The pre-edit hook
        regex ``^(KG|CODE):\\ (.+)$`` matches the header; indented body
        lines fall through to the block-content accumulator.

        Human format: ``<rank>. <full_name>`` with optional ``[peer:<src>]``
        and similarity / tier annotation, body lines indented three
        spaces, terminating blank line.
        """
        collection = rendered.get("collection", "")
        tier = rendered.get("tier", "ref")
        score_str = rendered.get("score", "")
        distance_str = rendered.get("distance", "")

        # Identifier for the dedup key: full_name when present, else the
        # closest substitute per collection. Mirrors the MCP _format_ref
        # priorities so the dedup key is stable across surfaces.
        full_name = rendered.get("full_name", "")
        if not full_name:
            if collection == "CodeModule":
                full_name = rendered.get("path", "Unknown")
            elif collection in ("CodeAPI", "CodeInteraction"):
                ep = rendered.get("endpoint", "")
                method = rendered.get("method", "")
                full_name = f"{method} {ep}".strip() or rendered.get("interaction_type", "Unknown")
            else:
                full_name = "Unknown"

        if hook_format:
            # v0.2.70 Stream E: append a "| src=<file_path>" trailer (LAST
            # field) so the shared seen-store can suppress a CODE block whose
            # source the model already Read explicitly (reads-ledger match). The
            # seen-store extracts the src via the last "| src=" occurrence, so it
            # MUST be last. Empty file_path -> no src trailer (key-only dedup).
            _fp = rendered.get("file_path", "") or ""
            _src_trailer = f" | src={_fp}" if _fp else ""
            print(f"CODE: {full_name} | {collection} | distance={distance_str}{src_suffix}{_src_trailer}")
            CodeGraphQuery._print_body(rendered, indent="  ", hook_format=True)
            # Blank line terminates the block (matches the KG block
            # contract that pre-edit-context-inject.sh _filter_seen
            # parses).
            print()
        else:
            similarity = 0.0
            try:
                similarity = 1.0 - float(distance_str) if distance_str else 0.0
            except (TypeError, ValueError):
                similarity = 0.0
            tier_suffix = f"  [tier={tier}]"
            print(f"{rank + 1}. {full_name}{src_label}{tier_suffix}")
            print(f"   Distance: {distance_str} (similarity: {similarity:.3f}, score: {score_str})")
            if tier == "ref":
                print()
                return
            CodeGraphQuery._print_body(rendered, indent="   ", hook_format=False)
            print()

    @staticmethod
    def _print_body(rendered: dict, indent: str, hook_format: bool) -> None:
        """Render the per-collection body section of a code-graph result.

        Uses the same fields the shared helper populates so the output
        is byte-identical between MCP JSON consumers and CLI human
        consumers (after stripping the prefix). Sibling rows render
        underneath the seed for top-2 ranks.
        """
        collection = rendered.get("collection", "")
        if collection == "CodeFunction":
            sig = rendered.get("signature", "")
            if sig:
                print(f"{indent}Signature: {sig}")
            doc = rendered.get("doc", "")
            if doc:
                if hook_format:
                    print(f"{indent}Doc: {doc}")
                else:
                    snippet = doc[:200] + ("..." if len(doc) > 200 else "")
                    print(f"{indent}Doc: {snippet}")
            # B1 (design audit): the summary tier carries its content under
            # `summary` — previously only the CodeModule branch printed it,
            # so summary-tier functions rendered as a bare header (content
            # silently dropped on the CLI while the MCP JSON carried it).
            # Skip when identical to the doc already printed above (R1 makes
            # the summary tier prefer doc — avoid the duplicate line).
            summary = rendered.get("summary", "")
            if summary and summary != doc:
                if hook_format:
                    print(f"{indent}Summary: {summary}")
                else:
                    snippet = summary[:400] + ("..." if len(summary) > 400 else "")
                    print(f"{indent}Summary: {snippet}")
            loc = rendered.get("location", "")
            if loc:
                print(f"{indent}Location: {loc}")
            body = rendered.get("function_body", "")
            if body and hook_format:
                print(f"{indent}Body:")
                for body_line in body.splitlines():
                    print(f"{indent}  {body_line}")
        elif collection == "CodeClass":
            sig = rendered.get("signature", "")
            if sig:
                print(f"{indent}Signature: {sig}")
            doc = rendered.get("doc", "")
            if doc:
                if hook_format:
                    print(f"{indent}Doc: {doc}")
                else:
                    snippet = doc[:200] + ("..." if len(doc) > 200 else "")
                    print(f"{indent}Doc: {snippet}")
            # B1: same summary-tier rendering as the CodeFunction branch.
            summary = rendered.get("summary", "")
            if summary and summary != doc:
                if hook_format:
                    print(f"{indent}Summary: {summary}")
                else:
                    snippet = summary[:400] + ("..." if len(summary) > 400 else "")
                    print(f"{indent}Summary: {snippet}")
            method_count = rendered.get("method_count")
            if method_count is not None:
                print(f"{indent}Methods: {method_count} methods")
            loc = rendered.get("location", "")
            if loc:
                print(f"{indent}Location: {loc}")
            body = rendered.get("class_body", "")
            if body and hook_format:
                print(f"{indent}Body:")
                for body_line in body.splitlines():
                    print(f"{indent}  {body_line}")
        elif collection == "CodeModule":
            path = rendered.get("path", "")
            if path:
                print(f"{indent}Path: {path}")
            lang = rendered.get("language", "")
            loc = rendered.get("loc", 0)
            if lang or loc:
                print(f"{indent}Language: {lang}, LOC: {loc}")
            summary = rendered.get("summary", "")
            if summary:
                if hook_format:
                    print(f"{indent}Summary: {summary}")
                else:
                    snippet = summary[:200] + ("..." if len(summary) > 200 else "")
                    print(f"{indent}Summary: {snippet}")
        elif collection == "CodeAPI":
            ep = rendered.get("endpoint", "")
            method = rendered.get("method", "")
            if ep or method:
                print(f"{indent}Endpoint: {method} {ep}".rstrip())
            desc = rendered.get("description", "")
            if desc:
                if hook_format:
                    print(f"{indent}Description: {desc}")
                else:
                    snippet = desc[:200] + ("..." if len(desc) > 200 else "")
                    print(f"{indent}Description: {snippet}")
        elif collection == "CodeInteraction":
            itype = rendered.get("interaction_type", "")
            direction = rendered.get("direction", "")
            if itype or direction:
                print(f"{indent}Type: {itype} | {direction}")
            proto = rendered.get("protocol", "")
            ep = rendered.get("endpoint", "")
            if proto or ep:
                print(f"{indent}{proto} -> {ep}")
            confidence = rendered.get("confidence", "")
            if confidence:
                print(f"{indent}Confidence: {confidence}")
            desc = rendered.get("description", "")
            if desc:
                if hook_format:
                    print(f"{indent}Description: {desc}")
                else:
                    snippet = desc[:200] + ("..." if len(desc) > 200 else "")
                    print(f"{indent}Description: {snippet}")

        # Sibling rows: only present for top-2 ranks in auto mode. Render
        # as one indented line each so the pre-edit hook treats them as
        # body content of the parent CODE: block.
        siblings = rendered.get("siblings") or []
        if siblings:
            print(f"{indent}Siblings ({len(siblings)}):")
            for sib in siblings:
                sib_coll = sib.get("collection", "")
                sib_name = (
                    sib.get("full_name")
                    or sib.get("path")
                    or sib.get("endpoint", "")
                )
                start_line = sib.get("start_line", "?")
                print(f"{indent}  - [{sib_coll}] {sib_name} (line {start_line})")

    def find_similar(self, reference_name: str, collection: str = "CodeFunction", limit: int = 5):
        """Find code similar to reference."""
        try:
            coll = self.client.collections.get(self._coll(collection))

            # Get reference object
            ref_query = coll.query.fetch_objects(
                filters=Filter.by_property("full_name").equal(reference_name),
                limit=1
            )

            if not ref_query.objects:
                print(f"❌ Reference '{reference_name}' not found in {collection}")
                return

            ref_obj = ref_query.objects[0]

            # Find similar
            similar_query = coll.query.near_object(
                near_object=ref_obj.uuid,
                limit=limit + 1
            )

            if self.project:
                similar_query = similar_query.where(
                    Filter.by_property("project").equal(self.project)
                )

            response = similar_query.do()

            # Format and print results
            print(f"\n🔍 Finding code similar to: '{reference_name}'")
            print(f"   Found {len(response.objects) - 1} similar items:\n")  # -1 for reference itself

            for i, obj in enumerate(response.objects, 1):
                if obj.uuid == ref_obj.uuid:
                    continue  # Skip reference itself

                props = obj.properties
                distance = obj.metadata.distance if obj.metadata.distance is not None else -1.0
                similarity = 1.0 - distance if distance >= 0 else 0.0

                print(f"{i}. {props.get('full_name')}")
                print(f"   Similarity: {similarity:.3f} (distance: {distance:.3f})")
                print(f"   Signature: {props.get('signature')}")
                if props.get('doc'):
                    print(f"   Doc: {props.get('doc')[:100]}...")
                print()

        except Exception as e:
            print(f"❌ Error finding similar code: {e}", file=sys.stderr)

    def query_structure(self, query_type: str, target: str):
        """Structural query (dependencies, callers, etc.)."""
        try:
            if query_type == "dependencies":
                # Module imports
                coll = self.client.collections.get(self._coll("CodeModule"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("path").equal(target),
                    limit=1,
                    return_references=QueryReference(link_on="imports")
                )

                if not response.objects:
                    print(f"❌ Module '{target}' not found")
                    return

                # v0.2.70 C1c: `references` is None when the object carries no
                # linked refs — guard before .get to avoid 'NoneType' has no
                # attribute 'get'. Soft-fall to an empty dict.
                _refs = response.objects[0].references or {}
                imports = _refs.get("imports", [])
                print(f"\n🔗 Dependencies of module '{target}':")
                print(f"   Imports {len(imports)} modules:\n")

                for imp in imports:
                    print(f"   - {imp.properties.get('path')}")

            elif query_type == "callers":
                # Find callers of function
                coll = self.client.collections.get(self._coll("CodeFunction"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1
                )

                if not response.objects:
                    print(f"❌ Function '{target}' not found")
                    return

                func_uuid = response.objects[0].uuid

                # Find references — Pattern B (intentional top-N cap, but
                # the truncation signal is emitted so the user can
                # re-run with --limit). v0.2.46 V46-D: previously
                # `limit=50` was hard-coded and the user had no signal
                # that the candidate-callers pool was capped at 50
                # total functions.
                CALLERS_FETCH_LIMIT = 50
                caller_response = coll.query.fetch_objects(
                    limit=CALLERS_FETCH_LIMIT
                )

                # Filter for functions that call target
                callers = []
                for obj in caller_response.objects:
                    # v0.2.70 C1c: guard None references before .get.
                    calls_refs = (obj.references or {}).get("calls", [])
                    if any(ref.uuid == func_uuid for ref in calls_refs):
                        callers.append(obj)

                fetched_count = len(caller_response.objects)
                truncated = fetched_count >= CALLERS_FETCH_LIMIT

                print(f"\n🔗 Callers of function '{target}':")
                print(f"   Found {len(callers)} callers:\n")

                for caller in callers:
                    print(f"   - {caller.properties.get('full_name')}")
                    print(f"     {caller.properties.get('signature')}")

                if truncated:
                    print(
                        f"\n⚠️  Searched only the first {CALLERS_FETCH_LIMIT} "
                        f"candidate functions. Some callers may be missing."
                    )
                    print(
                        "   For a thorough scan, use the MCP "
                        "`query_code_structure(\"callers\", ...)` tool or "
                        "raise the limit in this script."
                    )

            elif query_type == "methods":
                # List class methods
                coll = self.client.collections.get(self._coll("CodeClass"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1
                )

                if not response.objects:
                    print(f"❌ Class '{target}' not found")
                    return

                methods = response.objects[0].properties.get("methods", [])
                print(f"\n🔗 Methods in class '{target}':")
                print(f"   {len(methods)} methods:\n")

                for method in methods:
                    print(f"   - {method}")

            elif query_type == "extends":
                # Find base classes
                coll = self.client.collections.get(self._coll("CodeClass"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1,
                    return_references=QueryReference(link_on="extends")
                )

                if not response.objects:
                    print(f"❌ Class '{target}' not found")
                    return

                # v0.2.70 C1c: guard None references before .get.
                extends = (response.objects[0].references or {}).get("extends", [])
                print(f"\n🔗 Base classes of '{target}':")
                print(f"   Extends {len(extends)} classes:\n")

                for base in extends:
                    print(f"   - {base.properties.get('full_name')}")

            elif query_type == "interactions":
                # Find outbound cross-service interactions for a function or module
                from weaviate.classes.query import QueryReference as QR
                interactions_coll = self.client.collections.get(self._coll("CodeInteraction"))
                func_coll = self.client.collections.get(self._coll("CodeFunction"))
                func_resp = func_coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1
                )
                # v0.2.46 V46-D: emit truncation signal (Pattern B). The
                # `limit=50` is an intentional top-N cap, but previously
                # the user had no way to know when the cap was hit.
                INTERACTIONS_FETCH_LIMIT = 50
                if func_resp.objects:
                    source_uuid = str(func_resp.objects[0].uuid)
                    ix_resp = interactions_coll.query.fetch_objects(
                        filters=Filter.by_ref("source_function").by_id().equal(source_uuid),
                        limit=INTERACTIONS_FETCH_LIMIT
                    )
                else:
                    mod_coll = self.client.collections.get(self._coll("CodeModule"))
                    mod_resp = mod_coll.query.fetch_objects(
                        filters=Filter.by_property("path").equal(target),
                        limit=1
                    )
                    if not mod_resp.objects:
                        print(f"❌ Function or module '{target}' not found")
                        return
                    source_uuid = str(mod_resp.objects[0].uuid)
                    ix_resp = interactions_coll.query.fetch_objects(
                        filters=Filter.by_ref("source_module").by_id().equal(source_uuid),
                        limit=INTERACTIONS_FETCH_LIMIT
                    )

                truncated = len(ix_resp.objects) >= INTERACTIONS_FETCH_LIMIT
                print(f"\n🔗 Cross-service interactions from '{target}':")
                print(f"   Found {len(ix_resp.objects)} interactions:\n")
                for obj in ix_resp.objects:
                    p = obj.properties
                    print(f"   [{p.get('confidence','?')}] {p.get('interaction_type','')} {p.get('protocol','')} → {p.get('endpoint','')}")
                    print(f"     Direction: {p.get('direction','')} | Raw: {p.get('raw_target','')}")
                    if p.get('description'):
                        print(f"     {p.get('description','')}")
                    print()

                if truncated:
                    print(
                        f"⚠️  Result list capped at {INTERACTIONS_FETCH_LIMIT}. "
                        f"Some interactions from '{target}' may be missing."
                    )

            else:
                print(f"❌ Unknown query type: {query_type}")
                print("   Supported: dependencies, callers, methods, extends, interactions")

        except Exception as e:
            print(f"❌ Structure query error: {e}", file=sys.stderr)

    def close(self):
        """Close Weaviate connection."""
        if self.client:
            self.client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Query code graph with semantic and structural search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest='command', help='Query type')

    # Semantic search
    search_parser = subparsers.add_parser('search', help='Semantic search for code')
    search_parser.add_argument('query', type=str, help='Search query')
    search_parser.add_argument('--collection', '-c', type=str, default="CodeFunction",
                              choices=["CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction"],
                              help='Collection to search (default: CodeFunction)')
    search_parser.add_argument('--limit', '-l', type=int, default=5,
                              help='Maximum results (default: 5)')
    search_parser.add_argument('--project', '-p', type=str,
                              help='Filter by project name')
    search_parser.add_argument('--detail', type=str, default='auto',
                              choices=['auto', 'titles', 'full'],
                              help=("Verbosity per result. 'auto' (default) = score-tiered "
                                    "per result (summary / single_chunk / three_chunks / "
                                    "full as score rises; the min gate derives from the "
                                    "post-rerank floor — matches search_code_graph MCP). "
                                    "'titles' = name+score only. 'full' = full details for all."))
    search_parser.add_argument('--hook-format', action='store_true',
                              help=("Emit one-line 'CODE: <full_name> | <collection> | "
                                    "distance=<d>' header per result so the pre-edit hook "
                                    "can dedup by entity name. Suppresses banner lines."))
    search_parser.add_argument('--anchor', type=str, default=None,
                              help=('Edited file path or symbol full_name — biases rerank '
                                    'toward call-linked / same-module / shared-type code'))
    search_parser.add_argument('--exclude-file', type=str, default=None,
                              help=('Drop candidates whose source file is this path BEFORE '
                                    'trimming to --limit (the Read/Edit hook passes the '
                                    'edited file here to avoid self-injection)'))

    # Similar code
    similar_parser = subparsers.add_parser('similar', help='Find similar code')
    similar_parser.add_argument('reference', type=str, help='Reference code full name')
    similar_parser.add_argument('--collection', '-c', type=str, default="CodeFunction",
                               choices=["CodeFunction", "CodeClass"],
                               help='Collection type (default: CodeFunction)')
    similar_parser.add_argument('--limit', '-l', type=int, default=5,
                               help='Maximum results (default: 5)')
    similar_parser.add_argument('--project', '-p', type=str,
                               help='Filter by project name')

    # Structural queries
    structure_parser = subparsers.add_parser('structure', help='Structural queries')
    structure_parser.add_argument('query_type', type=str,
                                 choices=['dependencies', 'callers', 'methods', 'extends', 'interactions'],
                                 help='Query type')
    structure_parser.add_argument('target', type=str, help='Target entity')
    structure_parser.add_argument('--project', '-p', type=str,
                                 help='Filter by project name')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Resolve project: explicit --project wins; otherwise route through
    # the launcher's vct-hub for the canonical per-project value, falling
    # back to CODE_GRAPH_PROJECT / PROJECT_NAME env vars when the hub is
    # unreachable (v0.2.21 Step 18 caller migration). Without this, the
    # CLI always queries unprefixed `CodeFunction` etc., which never
    # exist in multi-project Weaviate setups — every project ships its
    # own `<Project>_CodeFunction` collection. Pre-2026-05-08 the CLI
    # was effectively dead unless the user remembered to pass --project.
    effective_project = getattr(args, 'project', None)
    if not effective_project:
        try:
            from vco_lib.project_config import resolve as _vco_resolve  # type: ignore[import-not-found]
            from pathlib import Path as _Path
            _cfg = _vco_resolve(_Path.cwd())
            # v0.2.70 Bug C1a: use the canonical binding-row prefix
            # (code_graph_collection_prefix), NOT the slug alias
            # code_graph_project. The slug sanitises to a nonexistent
            # `<Slug>_CodeFunction` (e.g. Orchestrator_root_CodeFunction) so the
            # CLI returned no-results / crashed `structure` since v0.2.21.
            # Mirrors server.py:2293-2294 (W3) and post-file-edit.sh:445; keep
            # code_graph_project as a secondary fallback for legacy resolver
            # shapes that populate only the slug. MUST MATCH those siblings.
            effective_project = (
                _cfg.code_graph_collection_prefix
                or _cfg.code_graph_project
                or None
            )
        except Exception:
            effective_project = None
    if not effective_project:
        effective_project = os.getenv("CODE_GRAPH_PROJECT") or os.getenv("PROJECT_NAME") or None

    # Create query interface
    querier = CodeGraphQuery(project=effective_project)

    # Connect to Weaviate
    if not querier.connect():
        return 1

    try:
        # Execute command
        if args.command == 'search':
            querier.search_by_concept(args.query, args.collection, args.limit, args.detail,
                                       hook_format=getattr(args, 'hook_format', False),
                                       anchor=getattr(args, 'anchor', None),
                                       exclude_file=getattr(args, 'exclude_file', None))
        elif args.command == 'similar':
            querier.find_similar(args.reference, args.collection, args.limit)
        elif args.command == 'structure':
            querier.query_structure(args.query_type, args.target)

        return 0

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        return 1
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        querier.close()


if __name__ == "__main__":
    sys.exit(main())
