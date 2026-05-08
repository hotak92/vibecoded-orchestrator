#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Get detailed info about a knowledge node (minimal context)

Returns structured info without full content
"""

import sys
import os
import weaviate
import time
from pathlib import Path
from weaviate.classes.query import Filter

WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
# P1-D (2026-05-08): respect KG_COLLECTION env, not the
# pre-2026-05-08 hardcoded "ClaudeKnowledgeGraph". The hardcoded value
# only worked on the orchestrator-self path; user projects ship with a
# different KG_COLLECTION. The hardcoded fallback kept it from breaking
# the orchestrator outright but silently broke every other project's
# kg-info CLI.
KG_COLLECTION = os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph")
SHARED_KG_COLLECTION = os.getenv("SHARED_KG_COLLECTION", "")

# P1-D (2026-05-08): centralized access-matrix helper. Resolved via
# $VCT_ORCHESTRATOR_ROOT (the orchestrator clone is where
# claude_mcp_servers/scripts/kg_access.py lives) with an in-tree
# fallback. Self-only fallback keeps the CLI functional even on a
# hand-edited venv that doesn't ship the helper.
try:
    # VCO-REWIRE-BEGIN: orchestrator-root-resolution
    _env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    if _env_root and (Path(_env_root) / "claude_mcp_servers" / "scripts").is_dir():
        sys.path.insert(0, str(Path(_env_root) / "claude_mcp_servers" / "scripts"))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "claude_mcp_servers" / "scripts"))
    # VCO-REWIRE-END: orchestrator-root-resolution
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

def _collections_to_search() -> list[str]:
    """Compute KG collections to fan out across.

    Single source of truth: ``kg_access.kg_collections_to_search`` (P1-D
    centralization, 2026-05-08). Self → shared → peers from
    ``VCT_KG_ACCESS_LIST``. Pre-P1-D this CLI hardcoded
    ``ClaudeKnowledgeGraph`` and ignored both shared + access matrix.
    """
    return _kg_collections_to_search(
        self_kg=KG_COLLECTION,
        shared_kg=SHARED_KG_COLLECTION,
        include_dev=False,
    )


def _collection_label(coll_name: str) -> str:
    """Render a short ``[self|shared|peer:Name]`` tag for a collection
    when fan-out covers >1 collection. Empty string when there's only
    one collection (the pre-P1-D cosmetic shape).
    """
    if coll_name == KG_COLLECTION:
        return "[self]"
    if coll_name == SHARED_KG_COLLECTION:
        return "[shared]"
    if coll_name.endswith("_KnowledgeGraph"):
        return f"[peer:{coll_name[:-len('_KnowledgeGraph')]}]"
    return f"[{coll_name}]"


def get_node_info(title: str):
    """Get node info from Weaviate.

    P1-D (2026-05-08): fan out across the access matrix. If the title
    exists in multiple collections (e.g. self + a peer that copied a
    shared concept), all are listed in order: self → shared → peers.
    Pre-P1-D this CLI hardcoded ``ClaudeKnowledgeGraph`` and missed
    nodes in shared / peer KGs entirely.
    """
    start_time = time.time()
    error_msg = None
    client = get_weaviate_client()

    try:
        collections = _collections_to_search()
        hits: list[tuple[str, dict]] = []  # (collection_name, props)

        for coll_name in collections:
            try:
                collection = client.collections.get(coll_name)
                results = collection.query.fetch_objects(
                    filters=Filter.by_property("title").equal(title),
                    limit=1,
                )
            except Exception:
                # Peer collection may not exist (peer never indexed) —
                # skip silently, don't fail the whole CLI.
                continue
            if results.objects:
                hits.append((coll_name, results.objects[0].properties))

        if not hits:
            print(f"❌ Node not found: {title}")
            error_msg = "Node not found"
            return None

        # Render each hit; with multi-collection fan-out the user gets
        # the per-collection breakdown they need to disambiguate.
        for idx, (coll_name, props) in enumerate(hits):
            label = _collection_label(coll_name) if len(collections) > 1 else ""
            sep = " " if label else ""
            if idx > 0:
                print()
            print(f"\n📝 {props['title']}{sep}{label}")
            print(f"{'='*60}")
            print(f"Type: {props.get('node_type', 'unknown')}")
            print(f"File: {props.get('file_path', 'unknown')}")
            print(f"Tags: {', '.join(props.get('tags', []))}")
            print(f"Links: {len(props.get('links', []))}")
            if props.get('links'):
                print(f"  Connections:")
                for link in props['links'][:10]:
                    print(f"    - {link}")
                if len(props['links']) > 10:
                    print(f"    ... and {len(props['links']) - 10} more")
            print(f"Created: {props.get('created_at', 'unknown')}")
            print(f"Updated: {props.get('updated_at', 'unknown')}")
            print(f"\nContent length: {len(props.get('content', ''))} chars")
            print(f"Content preview: {props.get('content', '')[:200]}...")

        print()
        # Back-compat return shape: callers historically got the props
        # dict for the single-collection lookup. With fan-out we
        # return the FIRST hit (self / shared / first peer) so existing
        # callers keep working; multi-collection hits are still
        # rendered to stdout above.
        return hits[0][1]

    except Exception as e:
        error_msg = str(e)
        raise

    finally:
        client.close()

        # Log usage
        if HAS_LOGGER:
            duration_ms = (time.time() - start_time) * 1000
            ToolUsageLogger.log_kg_info(
                node_title=title,
                duration_ms=duration_ms,
                success=error_msg is None,
                error=error_msg
            )

def find_connections(title: str):
    """Find nodes connected to this one.

    P1-D (2026-05-08): fan out across self + shared + peer KGs from the
    access matrix. Outbound connections are read from each collection
    where the target node exists; inbound connections (nodes that link
    TO this one) are scanned in every collection.
    """
    client = get_weaviate_client()

    try:
        collections = _collections_to_search()
        # Per-collection target lookup: build a list of (coll_name,
        # outbound_links) pairs for collections where the target exists.
        target_hits: list[tuple[str, list]] = []

        for coll_name in collections:
            try:
                collection = client.collections.get(coll_name)
                results = collection.query.fetch_objects(
                    filters=Filter.by_property("title").equal(title),
                    limit=1,
                )
            except Exception:
                continue
            if results.objects:
                outbound = results.objects[0].properties.get('links', []) or []
                target_hits.append((coll_name, list(outbound)))

        if not target_hits:
            print(f"❌ Node not found: {title}")
            return

        # Find connected nodes
        print(f"\n🔗 Connections for: {title}")
        print(f"{'='*60}\n")

        # Outbound: render once per collection that has the node, with
        # a [self|shared|peer:X] label when fan-out is >1.
        multi = len(collections) > 1
        all_outbound_count = 0
        for coll_name, outbound in target_hits:
            label = f" {_collection_label(coll_name)}" if multi else ""
            print(f"OUTBOUND ({len(outbound)}){label}:")
            for link in outbound:
                print(f"  → {link}")
            all_outbound_count += len(outbound)
            print()
        target_links = [link for _, links in target_hits for link in links]

        # Find inbound links (nodes that link to this one) — scan every
        # collection in the access matrix. Pre-P1-D this scanned only
        # ClaudeKnowledgeGraph, missing peer-project inbound references.
        inbound: list[tuple[str, str]] = []  # (collection, source_title)
        seen_keys: set[tuple[str, str]] = set()  # (collection, source_node_id)

        for coll_name in collections:
            try:
                collection = client.collections.get(coll_name)
                all_nodes = collection.query.fetch_objects(limit=200)
            except Exception:
                continue
            for obj in all_nodes.objects:
                if title in (obj.properties.get('links', []) or []):
                    source_id = obj.properties.get('source_node_id')
                    key = (coll_name, source_id)
                    # Dedupe per-collection by source_node_id (chunked
                    # nodes share an id across chunks).
                    if key not in seen_keys:
                        inbound.append((coll_name, obj.properties.get('title', '')))
                        seen_keys.add(key)

        print(f"INBOUND ({len(inbound)}):")
        for coll_name, link_title in inbound:
            label = f" {_collection_label(coll_name)}" if multi else ""
            print(f"  ← {link_title}{label}")

        # Show exploration hint
        total_connections = len(target_links) + len(inbound)
        if total_connections > 0:
            print(f"\n💡 Tip: Found {total_connections} connections.")
            print(f"   Use 'kg-info info \"<title>\"' to explore any connected node")
            if total_connections > 5:
                print(f"   Consider asking: \"Which of these connections are most relevant for [your task]?\"")
        print()

    finally:
        client.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage:")
        print("  get_node_info.py info <title>")
        print("  get_node_info.py connections <title>")
        sys.exit(1)

    command = sys.argv[1]
    title = sys.argv[2]

    if command == "info":
        get_node_info(title)
    elif command == "connections":
        find_connections(title)
