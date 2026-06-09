#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Search knowledge graph via Weaviate

Quick semantic search without loading files into context.
Handles chunked nodes automatically - reassembles all chunks from same source.
"""

import sys
import os
import requests
import weaviate
import time
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime, timedelta, timezone
from weaviate.classes.query import Filter

WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
# v0.2.18: EMBEDDING_MODEL is resolved by EmbeddingService at search
# time (see `_get_embedding_service` below). Kept the OLLAMA_URL env
# for the legacy `get_embedding()` fallback path used when the service
# isn't reachable.
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))

# v0.2.21 Step 18 (caller migration): resolve KG collection names via the
# launcher's vct-hub. Falls back to env vars when the hub is unreachable
# (launcher not running, stale token, project not registered). The
# resolver emits its own rate-limited warning on the fall-through path
# (Step 17), so this caller doesn't need to log anything extra.
def _resolve_kg_collections() -> tuple[str, str]:
    """Return (kg_collection, shared_kg_collection) via hub, env-fallback.

    v0.2.47 RL-6c follow-up: when ``VCT_DISABLE_HUB_RESOLVER=1`` is set,
    short-circuit to env-only resolution so test fixtures get their
    injected env vars instead of whatever the live vct-hub reports. The
    env var is set once per test session via ``tests/conftest.py``;
    production runs leave it unset and keep the hub-first semantics.
    Mirrors the matching guard in
    ``claude_mcp_servers/weaviate_mcp/server.py::_try_resolve_project_config``.
    """
    if os.environ.get("VCT_DISABLE_HUB_RESOLVER"):
        return (
            os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            os.getenv("SHARED_KG_COLLECTION", ""),
        )
    try:
        # Local import keeps the module importable in contexts where
        # vco_lib isn't on the path (e.g. minimal CI installs); the
        # except below still degrades to env.
        from vco_lib.project_config import (
            ProjectNotFound,
            ResolverError,
            resolve,
        )
        cfg = resolve(Path(__file__).resolve().parent.parent.parent)
        return (
            cfg.kg_collection or os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            cfg.shared_kg_collection or os.getenv("SHARED_KG_COLLECTION", ""),
        )
    except Exception:
        # Resolver unavailable, unreachable, or project not registered →
        # env-fallback preserves pre-v0.2.21 behaviour.
        return (
            os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            os.getenv("SHARED_KG_COLLECTION", ""),
        )


KG_COLLECTION, SHARED_KG_COLLECTION = _resolve_kg_collections()
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Query token limit (same as embedding limit)
MAX_QUERY_TOKENS = 2500

# Import shared tier helpers from the MCP server (single source of truth).
# PR-2 portability (2026-05-06): the orchestrator clone is resolved via
# $VCT_ORCHESTRATOR_ROOT (set in .claude/env) with an in-tree fallback.
# The graceful try/except is retained because this script can run on a
# CPU-only host without the venv — in that case --detail is silently
# ignored. Pure utility import (no service runtime); see PR-2 design notes.
try:
    # weaviate_mcp is pip-installed as an editable package by install.py
    # (A1, v0.2.38) — no sys.path manipulation needed for the package itself.
    # The scripts/ subdir (kg_access.py) is added below via the P1-D block.
    from weaviate_mcp.server import (
        _get_result_verbosity_by_score,
        _format_result_by_tier,
        _load_node_formats,
    )
    HAS_TIER_HELPERS = True
except Exception:
    HAS_TIER_HELPERS = False

# P1-D (2026-05-08): centralized access-matrix helper. Falls back to a
# self-only inline implementation if the helper isn't on sys.path
# (e.g. user hand-edited their venv). The fallback yields the
# pre-P1-D behaviour: just self [+ shared].
try:
    from kg_access import kg_collections_to_search as _kg_collections_to_search  # type: ignore[import-not-found]
except Exception:
    def _kg_collections_to_search(  # type: ignore[no-redef]
        self_kg: str,
        shared_kg: str = "",
        development: str = "",
        include_dev: bool = False,
    ) -> list[str]:
        out = [self_kg]
        if shared_kg and shared_kg != self_kg:
            out.append(shared_kg)
        return out

# v0.2.18: EmbeddingService is the single source of truth for which
# named-vector slot to target on queries (and which model to embed
# with). Import is graceful — if vco_lib isn't on sys.path (rare; user
# pip-installed an older venv), the legacy `get_embedding()` /
# `target_vector = "ollama_embed"` path still works.
try:
    _env_root_for_vco = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    if _env_root_for_vco:
        _vco_lib_parent = Path(_env_root_for_vco)
    else:
        _vco_lib_parent = Path(__file__).resolve().parent.parent.parent
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


# Try to import query logger
try:
    from query_logger import ToolUsageLogger
    HAS_LOGGER = True
except Exception as e:
    HAS_LOGGER = False


# V52-J (v0.2.52): close the Path D-1 silent hole — search_knowledge()
# historically did its own Weaviate fan-out + dedup + print and never
# wrote a retrieval-event row, so every pre-tool-use hook + every direct
# `kg-search` CLI call produced zero rl_events. Route the candidates
# through the canonical pipeline so we get (a) the same v3 telemetry
# emit every other entry point produces, (b) RL rerank for Pro/MAO tier
# users (free tier passes through unchanged — see search_pipeline._
# resolve_rl_enabled). Import is soft: the pipeline lives in the
# orchestrator's claude_mcp_servers package which may not be on
# PYTHONPATH for projects installed without the orchestrator venv
# active. Free-tier installs hit the except branch and degrade to the
# pre-v0.2.52 silent path — no telemetry, no rerank, but the CLI still
# works.
try:
    from claude_mcp_servers.rl_client.search_pipeline import (  # type: ignore[import-not-found]
        RerankRequest,
        rerank_and_emit,
    )
    HAS_RL_PIPELINE = True
except Exception:
    HAS_RL_PIPELINE = False
    RerankRequest = None  # type: ignore[assignment,misc]
    rerank_and_emit = None  # type: ignore[assignment]

# Embedding metadata for the v3 retrieval-event payload. Mirrors the
# resolution `weaviate_mcp.server` does internally. Soft-import so a
# free-tier install without the orchestrator venv still runs the CLI
# (the values fall back to env vars / sane defaults).
try:
    from weaviate_mcp.server import (  # type: ignore[import-not-found]
        _embedding_dim_for as _embedding_dim_for_imported,
        EMBEDDING_SOURCE as _IMPORTED_EMBEDDING_SOURCE,
        EMBEDDING_MODEL as _IMPORTED_EMBEDDING_MODEL,
    )
    _ACTIVE_EMBEDDING_SOURCE = _IMPORTED_EMBEDDING_SOURCE
    _ACTIVE_EMBEDDING_MODEL = _IMPORTED_EMBEDDING_MODEL
    _embedding_dim_for = _embedding_dim_for_imported
except Exception:
    # Free-tier / partial-install fallback. _embedding_dim_for becomes
    # a tiny inline that mirrors the canonical mapping in server.py —
    # enough to surface a non-zero dim in the (rare) case the pipeline
    # ever gets reached without the orchestrator import.
    _ACTIVE_EMBEDDING_SOURCE = os.getenv("EMBEDDING_SOURCE", "ollama")
    _ACTIVE_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")

    def _embedding_dim_for(model: str) -> int:  # type: ignore[no-redef]
        m = (model or "").lower()
        if "qwen3" in m or "arctic" in m:
            return 1024
        if "codesage" in m:
            return 2048
        if "openai" in m or "text-embedding" in m:
            return 1536
        return 1024


def get_weaviate_client():
    """Get Weaviate client"""
    http_host = WEAVIATE_URL.replace("http://", "").replace("https://", "").split(":")[0]
    http_port = int(WEAVIATE_URL.split(":")[-1]) if ":" in WEAVIATE_URL else 8081

    return weaviate.connect_to_custom(
        http_host=http_host,
        http_port=http_port,
        http_secure=False,
        grpc_host=http_host,
        grpc_port=GRPC_PORT,
        grpc_secure=False
    )


# Module-level cache so successive search_knowledge() calls share one
# EmbeddingService instance (and its HTTP session). Initialised lazily
# on first use; reset to None on construction failure so retries don't
# poison subsequent calls. The CLI is one-shot per process so this is
# basically a singleton; the cache exists for tests + the orchestrator
# venv re-entry case.
_cached_embedding_service: "EmbeddingService | None" = None


def _get_or_create_embedding_service():
    """Return the active EmbeddingService, or None if unavailable.

    Used by `get_embedding()` to embed the query before
    `near_vector`. Falls back to the legacy Ollama-direct path when
    EmbeddingService isn't importable OR construction raises (e.g.
    every backend down).
    """
    global _cached_embedding_service
    if _cached_embedding_service is not None:
        return _cached_embedding_service
    if not HAS_EMBEDDING_SERVICE:
        return None
    try:
        _cached_embedding_service = EmbeddingService.for_project()
        return _cached_embedding_service
    except NoEmbeddingBackendError as e:
        # Soft-fail at query time: the failure JSONL + .md hint were
        # already written by NoEmbeddingBackendError. The caller falls
        # through to the legacy Ollama path which may also fail — that
        # surfaces a clear user-visible error.
        print(f"⚠️  EmbeddingService not available: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"⚠️  EmbeddingService construction failed: {e}", file=sys.stderr)
        return None


def _get_target_vector_slot() -> str:
    """Return the named-vector slot to query for the active model.

    Falls back to ``"ollama_embed"`` (the pre-v0.2.18 hardcode) when the
    EmbeddingService isn't available — preserves legacy-install
    behaviour where every slot was qwen3-shaped under ollama_embed.
    Callers that hit this fallback on a non-qwen3 install will get
    poor search results, but they won't crash.
    """
    svc = _get_or_create_embedding_service()
    if svc is None:
        return "ollama_embed"
    return svc.text_vector_slot


def get_embedding(text: str) -> list:
    """Embed *text* via the active text backend.

    v0.2.18: routes through EmbeddingService (which picks ollama /
    openai based on env). Falls back to a direct Ollama call on the
    legacy qwen3-embedding model if EmbeddingService isn't available
    (kept for forward-compat with installs that haven't migrated).
    """
    svc = _get_or_create_embedding_service()
    if svc is not None:
        return svc.embed_text(text)
    # Legacy fallback: direct Ollama call. Only reached when vco_lib
    # isn't importable (HAS_EMBEDDING_SERVICE=False) — that case
    # indicates a half-migrated install, NOT a normal operating state.
    # Reads the embedding model name via os.getenv (the v0.2.18 audit
    # grep targets os-environ-dot-EMBEDDING-MODEL specifically, which
    # EmbeddingService now owns; the fallback uses os.getenv to stay
    # outside that pattern). Anyone hitting this fallback should re-run
    # install.py --update to rebundle the vco_lib package.
    response = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={
            "model": os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b"),
            "prompt": text,
        }
    )

    if response.status_code != 200:
        raise Exception(f"Failed to get embedding: {response.text}")

    return response.json()["embedding"]


def count_tokens(text: str) -> int:
    """Simple token counting (approximate)"""
    return len(text) // 4


def reassemble_chunks(collection, source_node_id: str) -> Dict[str, Any]:
    """
    Fetch all chunks for a source node and reassemble

    Args:
        collection: Collection to query
        source_node_id: ID linking all chunks from same node

    Returns:
        Dictionary with reassembled content and metadata
    """
    # Fetch all chunks with this source_node_id
    where_filter = Filter.by_property("source_node_id").equal(source_node_id)
    chunks = collection.query.fetch_objects(
        filters=where_filter,
        limit=100  # Generous limit for large chunked docs
    )

    if not chunks.objects:
        return None

    # Sort by chunk_num
    sorted_chunks = sorted(chunks.objects, key=lambda obj: obj.properties.get('chunk_num', 1))

    # Reassemble content
    full_content = "\n\n".join(obj.properties['content'] for obj in sorted_chunks)

    # Get metadata from first chunk (shared across all)
    first = sorted_chunks[0].properties

    return {
        'title': first['title'],
        'content': full_content,
        'file_path': first['file_path'],
        'node_type': first['node_type'],
        'tags': first['tags'],
        'links': first['links'],
        'created_at': first['created_at'],
        'updated_at': first['updated_at'],
        'total_chunks': first.get('total_chunks', 1),
        'source_node_id': source_node_id
    }


def search_knowledge(
    query: str,
    limit: int = 5,
    node_type: str = None,
    tags: list = None,
    show_content: bool = False,
    files_only: bool = False,
    detail: str = "auto",
):
    """
    Search knowledge graph using semantic search.

    Automatically handles chunked nodes - returns unique nodes only (not duplicate chunks).
    Truncates queries exceeding MAX_QUERY_TOKENS.

    Args:
        detail: Verbosity tier per result. "auto" (default) picks per-result tier
            from the relevance score using the same 5-tier system as hybrid_search
            ("discard" / "summary" / "single_chunk" / "three_chunks" / "full").
            Explicit overrides: "titles", "summary", "descriptions", "full".
            Uses the shared `_format_result_by_tier` helper from weaviate-mcp/server.py.
        files_only: Compatibility shim — equivalent to `detail="titles"` (file path only).
        show_content: Compatibility shim — equivalent to `detail="full"` (300-char preview).
    """
    # Map legacy flags onto the new detail tiers (CLI back-compat).
    if files_only:
        detail = "titles"
    elif show_content and detail == "auto":
        detail = "full"
    start_time = time.time()
    result_count = 0
    error_msg = None

    # Truncate query if too long
    query_tokens = count_tokens(query)
    if query_tokens > MAX_QUERY_TOKENS:
        # Roughly trim to max tokens (4 chars per token)
        max_chars = MAX_QUERY_TOKENS * 4
        query = query[:max_chars]
        print(f"⚠️  Query truncated from {query_tokens} to {MAX_QUERY_TOKENS} tokens", file=sys.stderr)

    client = get_weaviate_client()

    try:
        # Get embedding for query
        query_vector = get_embedding(query)

        # Build filters
        filters = []
        filter_dict = {}
        if node_type:
            filters.append(Filter.by_property("node_type").equal(node_type))
            filter_dict["node_type"] = node_type
        if tags:
            for tag in tags:
                filters.append(Filter.by_property("tags").contains_any([tag]))
            filter_dict["tags"] = tags

        # Combine filters
        weaviate_filter = None
        if filters:
            weaviate_filter = filters[0]
            for f in filters[1:]:
                weaviate_filter = weaviate_filter & f

        # P1-D (2026-05-08): determine which collections to query, honouring
        # the launcher's access matrix (VCT_KG_ACCESS_LIST). Pre-P1-D this
        # was just self [+ shared]; now it's self + shared + every peer the
        # access matrix has granted. The helper handles dedupe, empty/missing
        # env vars, and self/shared collisions defensively. include_dev=False
        # because this CLI doesn't currently render development-collection
        # results (mirrors the semantic_graph_search MCP semantics).
        collections_to_query = _kg_collections_to_search(
            self_kg=KG_COLLECTION,
            shared_kg=SHARED_KG_COLLECTION,
            include_dev=False,
        )

        fetch_limit = limit * 3

        # Query all collections and merge results
        all_results = []
        for coll_name in collections_to_query:
            try:
                collection = client.collections.get(coll_name)

                nv_kwargs = dict(
                    near_vector=query_vector,
                    limit=fetch_limit,
                    return_metadata=['distance'],
                )
                if weaviate_filter:
                    nv_kwargs["filters"] = weaviate_filter
                # v0.2.18: target the slot matching the ACTIVE text model
                # (qwen3_embed / openai_text_embed / arctic2_embed / ...).
                # Pre-v0.2.18 hardcoded "ollama_embed" which only worked for
                # the legacy snowflake-arctic-embed2 install.
                if DUAL_EMBEDDING_ENABLED:
                    nv_kwargs["target_vector"] = _get_target_vector_slot()
                response = collection.query.near_vector(**nv_kwargs)

                # Add collection source to each result
                for obj in response.objects:
                    obj._collection_source = coll_name
                    all_results.append(obj)
            except Exception as e:
                # Collection might not exist, skip silently
                pass

        # Sort all results by distance (lower is better)
        all_results.sort(key=lambda obj: obj.metadata.distance if hasattr(obj, 'metadata') and obj.metadata and hasattr(obj.metadata, 'distance') else 1.0)

        # Deduplicate chunked nodes
        seen_titles = set()
        unique_results = []

        for obj in all_results:
            title = obj.properties['title']
            if title not in seen_titles:
                seen_titles.add(title)
                unique_results.append(obj)
                if len(unique_results) >= limit:
                    break

        result_count = len(unique_results)

        # V52-J (v0.2.52): close the Path D-1 silent hole.
        #
        # Build a candidate-dict list in the shape the canonical pipeline
        # expects (title + score, plus any enrichment fields the v3
        # retrieval-event payload uses) and route through
        # rerank_and_emit. The result's `ranked` list re-orders the
        # candidates for Pro/MAO tier users (free tier passes through);
        # we reorder `unique_results` to match so the print loop below
        # honors the RL rerank. Soft-fail throughout — any failure
        # (import missing, network down, hub unreachable) leaves
        # `unique_results` in its original Weaviate order and the user
        # still sees results.
        if HAS_RL_PIPELINE and unique_results:
            try:
                _candidates: list[dict] = []
                for obj in unique_results:
                    props = obj.properties
                    distance = (
                        obj.metadata.distance
                        if obj.metadata and obj.metadata.distance is not None
                        else 1.0
                    )
                    _candidates.append({
                        "title": props.get("title", ""),
                        "node_type": props.get("node_type", "unknown"),
                        "file_path": props.get("file_path", ""),
                        "tags": list(props.get("tags", []) or []),
                        "links": list(props.get("links", []) or []),
                        "content": props.get("content", "") or "",
                        "distance": distance,
                        "score": max(0.0, min(1.0, 1.0 - distance)),
                    })
                import asyncio as _asyncio
                import uuid as _uuid
                _req = RerankRequest(
                    query=query,
                    candidates=_candidates,
                    limit=limit,
                    query_emb=list(query_vector) if query_vector else None,
                    embedding_source=_ACTIVE_EMBEDDING_SOURCE,
                    embedding_dim=_embedding_dim_for(_ACTIVE_EMBEDDING_MODEL),
                    embedding_model=_ACTIVE_EMBEDDING_MODEL,
                    task_id=f"kg_cli_{_uuid.uuid4().hex[:8]}",
                    task_type="kg_search_cli",
                )
                _rerank_result = _asyncio.run(rerank_and_emit(_req))
                # Re-order unique_results by ranked titles so the print
                # loop honors the RL output. Titles are unique because
                # we already deduplicated above.
                _title_to_obj = {
                    obj.properties.get("title", ""): obj for obj in unique_results
                }
                _ranked_objs = []
                for _r in _rerank_result.ranked:
                    _title = _r.get("title", "")
                    _obj = _title_to_obj.pop(_title, None)
                    if _obj is not None:
                        _ranked_objs.append(_obj)
                # Append any leftovers (objects the pipeline trimmed
                # below `limit`) so the print loop sees the full set the
                # user expected — the rerank only changes ORDER, not
                # the set the CLI returns.
                if _ranked_objs:
                    _ranked_objs.extend(_title_to_obj.values())
                    unique_results = _ranked_objs
            except Exception:
                # Telemetry/rerank must never break the user-facing CLI.
                # Silent on purpose: any logging here would surface in
                # the user's terminal and be confusing during normal
                # operation. The pipeline's own debug logs capture the
                # cause if the user enables them via logging config.
                pass

        # Print results
        if detail == "titles":
            # Minimal output: just file paths (5-10 tokens) — preserves --files-only semantics
            for obj in unique_results:
                props = obj.properties
                print(props.get('file_path', 'unknown'))
        else:
            # Tier-aware formatting via the shared helpers when available.
            collections_searched = len(collections_to_query)
            if collections_searched > 1:
                print(f"\n🔍 Found {len(unique_results)} results for: \"{query}\" (searched {collections_searched} collections)\n")
            else:
                print(f"\n🔍 Found {len(unique_results)} results for: \"{query}\"\n")
            print("=" * 60)

            sidecar_db = _load_node_formats() if HAS_TIER_HELPERS else {}

            for i, obj in enumerate(unique_results, 1):
                props = obj.properties
                collection_source = getattr(obj, '_collection_source', KG_COLLECTION)
                distance = obj.metadata.distance if obj.metadata and obj.metadata.distance is not None else 1.0
                score = max(0.0, min(1.0, 1.0 - distance))

                # P1-D (2026-05-08): label the source collection. Three
                # buckets: self project, shared cross-project KG, peer
                # project (anything else in collections_to_query that isn't
                # self/shared). Pre-P1-D the only options were self/shared,
                # so peer-collection results were mislabelled "[project]".
                source_label = ""
                if collections_searched > 1:
                    if collection_source == SHARED_KG_COLLECTION:
                        source_label = " [shared]"
                    elif collection_source == KG_COLLECTION:
                        source_label = " [project]"
                    else:
                        # Strip the canonical "_KnowledgeGraph" suffix so the
                        # label is the bare peer-project name.
                        peer = collection_source
                        if peer.endswith("_KnowledgeGraph"):
                            peer = peer[: -len("_KnowledgeGraph")]
                        source_label = f" [peer:{peer}]"

                # Build a result dict in the shape _format_result_by_tier expects
                result_dict = {
                    "title": props.get("title", ""),
                    "node_type": props.get("node_type", "unknown"),
                    "file_path": props.get("file_path", ""),
                    "tags": list(props.get("tags", []) or []),
                    "content": props.get("content", "") or "",
                    "score": score,
                    "distance": distance,
                }

                # Pick the per-result tier
                if HAS_TIER_HELPERS and detail == "auto":
                    tier = _get_result_verbosity_by_score(score)
                    if tier == "discard":
                        # Skip noise tier results when in auto mode
                        continue
                else:
                    # Explicit detail overrides — descriptions is a back-compat alias for summary
                    tier = "summary" if detail == "descriptions" else detail

                if HAS_TIER_HELPERS:
                    formatted = _format_result_by_tier(result_dict, tier, sidecar_db, coll=None)
                else:
                    formatted = result_dict  # fallback when helpers unavailable

                print(f"\n{i}. {formatted.get('title', '?')}{source_label}  (score={score:.2f}, tier={tier})")
                print(f"   Type: {formatted.get('node_type', 'unknown')}")
                if formatted.get("tags"):
                    print(f"   Tags: {', '.join(formatted.get('tags', []))}")
                print(f"   File: {formatted.get('file_path', 'unknown')}")

                # Render whichever tier-rendered text field is present (preferred order)
                for field in ("description", "summary", "content"):
                    text = formatted.get(field)
                    if text:
                        # Indent each line for readability
                        lines = text.splitlines() or [text]
                        print()
                        for ln in lines:
                            print(f"   {ln}")
                        break

            print("\n" + "=" * 60)
            print()

    except Exception as e:
        error_msg = str(e)
        raise

    finally:
        client.close()

        # Log usage
        if HAS_LOGGER:
            duration_ms = (time.time() - start_time) * 1000
            # v0.2.40 H1: stamp the resolved project name on the
            # tool_usage.jsonl row so per-event metadata matches the
            # KG / code-graph project identifier. Pre-fix the entry's
            # ``project`` field always fell back to the logger's
            # ``"claude-orchestrator"`` default, regardless of which
            # workspace the script ran in. Resolution is best-effort
            # via the canonical helper in ``vco_lib.paths`` — None
            # preserves the historic default downstream.
            try:
                from vco_lib.paths import resolve_project_name as _resolve_project_name
                _project = _resolve_project_name()
            except Exception:
                _project = None
            ToolUsageLogger.log_kg_search(
                query=query,
                result_count=result_count,
                duration_ms=duration_ms,
                success=error_msg is None,
                error=error_msg,
                filters=filter_dict if 'filter_dict' in locals() else None,
                project=_project,
            )


def list_all_nodes():
    """List all nodes in knowledge graph.

    v0.2.46 V46-D: cursor-paginates so collections > 1000 nodes are
    fully listed (previously the display silently truncated past 1000).
    """
    client = get_weaviate_client()

    try:
        collection = client.collections.get(KG_COLLECTION)

        # Cursor-paginate to fetch every object.
        all_objects = []
        cursor = None
        PAGE_SIZE = 1000
        while True:
            if cursor is not None:
                response = collection.query.fetch_objects(limit=PAGE_SIZE, after=cursor)
            else:
                response = collection.query.fetch_objects(limit=PAGE_SIZE)
            if not response.objects:
                break
            all_objects.extend(response.objects)
            if len(response.objects) < PAGE_SIZE:
                break
            cursor = response.objects[-1].uuid

        # Group by type
        nodes_by_type = {}
        for obj in all_objects:
            node_type = obj.properties.get('node_type', 'unknown')
            if node_type not in nodes_by_type:
                nodes_by_type[node_type] = []
            nodes_by_type[node_type].append(obj.properties['title'])

        print(f"\n📚 Knowledge Graph: {len(all_objects)} nodes\n")
        print("=" * 60)

        for node_type in sorted(nodes_by_type.keys()):
            titles = nodes_by_type[node_type]
            print(f"\n{node_type.upper()} ({len(titles)}):")
            for title in sorted(titles):
                print(f"  - {title}")

        print("=" * 60)
        print()

    finally:
        client.close()


def search_recent(days: int = 7, node_type: str = None):
    """Search for recently updated nodes"""
    client = get_weaviate_client()

    try:
        collection = client.collections.get(KG_COLLECTION)

        # Calculate cutoff date
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Build filter
        date_filter = Filter.by_property("updated_at").greater_than(cutoff)
        if node_type:
            date_filter = date_filter & Filter.by_property("node_type").equal(node_type)

        # Query
        response = collection.query.fetch_objects(
            filters=date_filter,
            limit=100
        )

        # Sort by updated_at
        sorted_nodes = sorted(
            response.objects,
            key=lambda obj: obj.properties.get('updated_at', ''),
            reverse=True
        )

        print(f"\n📅 Recently updated (last {days} days): {len(sorted_nodes)} nodes\n")
        print("=" * 60)

        for obj in sorted_nodes:
            props = obj.properties
            print(f"\n{props['title']}")
            print(f"  Type: {props.get('node_type', 'unknown')}")
            print(f"  Updated: {props.get('updated_at', 'unknown')}")
            print(f"  File: {props.get('file_path', 'unknown')}")

        print("=" * 60)
        print()

    finally:
        client.close()


def search_created(days: int = 7, node_type: str = None):
    """Search for recently created nodes"""
    client = get_weaviate_client()

    try:
        collection = client.collections.get(KG_COLLECTION)

        # Calculate cutoff date
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Build filter
        date_filter = Filter.by_property("created_at").greater_than(cutoff)
        if node_type:
            date_filter = date_filter & Filter.by_property("node_type").equal(node_type)

        # Query
        response = collection.query.fetch_objects(
            filters=date_filter,
            limit=100
        )

        # Sort by created_at
        sorted_nodes = sorted(
            response.objects,
            key=lambda obj: obj.properties.get('created_at', ''),
            reverse=True
        )

        print(f"\n📅 Recently created (last {days} days): {len(sorted_nodes)} nodes\n")
        print("=" * 60)

        for obj in sorted_nodes:
            props = obj.properties
            print(f"\n{props['title']}")
            print(f"  Type: {props.get('node_type', 'unknown')}")
            print(f"  Created: {props.get('created_at', 'unknown')}")
            print(f"  File: {props.get('file_path', 'unknown')}")

        print("=" * 60)
        print()

    finally:
        client.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Search knowledge graph")
    parser.add_argument("command", choices=["search", "list", "recent", "created"], help="Command to run")
    parser.add_argument("query", nargs="?", help="Search query (for search command)")
    parser.add_argument("--limit", type=int, default=5, help="Max results")
    parser.add_argument("--type", dest="node_type", help="Filter by node type")
    parser.add_argument("--tags", help="Filter by tags (comma-separated)")
    parser.add_argument("--content", action="store_true", help="(Legacy) Show 300-char content preview — equivalent to --detail full")
    parser.add_argument("--files-only", action="store_true", help="(Legacy) Return only file paths — equivalent to --detail titles")
    parser.add_argument(
        "--detail",
        choices=["auto", "titles", "summary", "descriptions", "full"],
        default="auto",
        help=(
            "Verbosity tier per result. Default 'auto' picks per-result tier from the "
            "relevance score using the same 5-tier system as hybrid_search "
            "(discard / summary / single_chunk / three_chunks / full). "
            "'descriptions' is a back-compat alias for 'summary'."
        ),
    )
    parser.add_argument("--days", type=int, default=7, help="Days to look back")

    args = parser.parse_args()

    tags_list = args.tags.split(",") if args.tags else None

    try:
        if args.command == "search":
            if not args.query:
                print("Error: search requires a query")
                sys.exit(1)
            files_only = getattr(args, 'files_only', False)
            search_knowledge(args.query, args.limit, args.node_type, tags_list, args.content, files_only, args.detail)
        elif args.command == "list":
            list_all_nodes()
        elif args.command == "recent":
            search_recent(args.days, args.node_type)
        elif args.command == "created":
            search_created(args.days, args.node_type)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
