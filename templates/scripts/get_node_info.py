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

def get_node_info(title: str):
    """Get node info from Weaviate"""
    start_time = time.time()
    error_msg = None
    client = get_weaviate_client()

    try:
        collection = client.collections.get("ClaudeKnowledgeGraph")
        results = collection.query.fetch_objects(
            filters=Filter.by_property("title").equal(title),
            limit=1
        )

        if not results.objects:
            print(f"❌ Node not found: {title}")
            error_msg = "Node not found"
            return None

        props = results.objects[0].properties

        print(f"\n📝 {props['title']}")
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

        return props

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
    """Find nodes connected to this one"""
    client = get_weaviate_client()

    try:
        collection = client.collections.get("ClaudeKnowledgeGraph")

        # Get target node
        results = collection.query.fetch_objects(
            filters=Filter.by_property("title").equal(title),
            limit=1
        )

        if not results.objects:
            print(f"❌ Node not found: {title}")
            return

        target_links = results.objects[0].properties.get('links', [])

        # Find connected nodes
        print(f"\n🔗 Connections for: {title}")
        print(f"{'='*60}\n")

        print(f"OUTBOUND ({len(target_links)}):")
        for link in target_links:
            print(f"  → {link}")

        # Find inbound links (nodes that link to this one)
        all_nodes = collection.query.fetch_objects(limit=200)
        inbound = []
        seen_source_ids = set()  # Deduplicate by source_node_id for chunked nodes

        for obj in all_nodes.objects:
            if title in obj.properties.get('links', []):
                source_id = obj.properties.get('source_node_id')
                # Only add if we haven't seen this source_node_id yet
                if source_id not in seen_source_ids:
                    inbound.append(obj.properties['title'])
                    seen_source_ids.add(source_id)

        print(f"\nINBOUND ({len(inbound)}):")
        for link in inbound:
            print(f"  ← {link}")

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
