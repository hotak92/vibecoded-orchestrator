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
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
KG_COLLECTION = os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph")
SHARED_KG_COLLECTION = os.getenv("SHARED_KG_COLLECTION", "")
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
    # VCO-REWIRE-BEGIN: orchestrator-root-resolution
    _env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    if _env_root and (Path(_env_root) / "claude_mcp_servers").is_dir():
        sys.path.insert(0, str(Path(_env_root) / "claude_mcp_servers"))
        sys.path.insert(0, str(Path(_env_root) / "claude_mcp_servers" / "scripts"))
    else:
        _local_mcp = Path(__file__).resolve().parent.parent.parent / "claude_mcp_servers"
        sys.path.insert(0, str(_local_mcp))
        sys.path.insert(0, str(_local_mcp / "scripts"))
    # VCO-REWIRE-END: orchestrator-root-resolution
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

# Try to import query logger
try:
    from query_logger import ToolUsageLogger
    HAS_LOGGER = True
except Exception as e:
    HAS_LOGGER = False


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


def get_embedding(text: str) -> list:
    """Get embedding from Ollama"""
    response = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={
            "model": EMBEDDING_MODEL,
            "prompt": text
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
                # Use named vector target when dual embedding is enabled
                if DUAL_EMBEDDING_ENABLED:
                    nv_kwargs["target_vector"] = "ollama_embed"
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
            ToolUsageLogger.log_kg_search(
                query=query,
                result_count=result_count,
                duration_ms=duration_ms,
                success=error_msg is None,
                error=error_msg,
                filters=filter_dict if 'filter_dict' in locals() else None
            )


def list_all_nodes():
    """List all nodes in knowledge graph"""
    client = get_weaviate_client()

    try:
        collection = client.collections.get(KG_COLLECTION)
        response = collection.query.fetch_objects(limit=1000)

        # Group by type
        nodes_by_type = {}
        for obj in response.objects:
            node_type = obj.properties.get('node_type', 'unknown')
            if node_type not in nodes_by_type:
                nodes_by_type[node_type] = []
            nodes_by_type[node_type].append(obj.properties['title'])

        print(f"\n📚 Knowledge Graph: {len(response.objects)} nodes\n")
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
