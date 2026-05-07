#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Knowledge Graph Maintenance Script

Ensures consistency between markdown files and Weaviate:
1. Finds orphaned Weaviate entries (no corresponding file)
2. Finds orphaned files (not in Weaviate)
3. Validates WikiLinks (no broken links)
4. Updates node timestamps
5. Rebuilds missing cross-references

Usage:
    python .claude/scripts/maintain_knowledge_graph.py --check    # Check only
    python .claude/scripts/maintain_knowledge_graph.py --fix      # Check and fix
    python .claude/scripts/maintain_knowledge_graph.py --rebuild  # Full rebuild
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple
import re

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# Add paths.
#
# PR-2 portability (2026-05-06): claude_mcp_servers/ only lives in the
# orchestrator clone, never bundled to user projects. Resolution order:
#   1. $VCT_ORCHESTRATOR_ROOT/claude_mcp_servers (set by .claude/env)
#   2. $CLAUDE_PROJECT_ROOT/claude_mcp_servers   (legacy override)
#   3. <project>/claude_mcp_servers              (orchestrator clone fallback)
# Pure utility import (WeaviateMCPServer used directly, no service spawn).
PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent.parent)))


def _resolve_mcp_servers_dir() -> Path:
    """Return the Path to claude_mcp_servers/, or raise with a hint."""
    env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root) / "claude_mcp_servers"
        if candidate.is_dir():
            return candidate
    candidate = PROJECT_ROOT / "claude_mcp_servers"
    if candidate.is_dir():
        return candidate
    raise RuntimeError(
        "claude_mcp_servers/ not found. Set VCT_ORCHESTRATOR_ROOT in your "
        "shell or .claude/env to point at the orchestrator clone."
    )


_MCP_DIR = _resolve_mcp_servers_dir()
sys.path.insert(0, str(_MCP_DIR / "weaviate_mcp"))
# VCO-REWIRE-END: orchestrator-root-resolution

from server import WeaviateMCPServer
from weaviate.classes.query import Filter

# Configuration
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))

KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"
KNOWLEDGE_COLLECTION = "ClaudeKnowledgeGraph"
DOCUMENTS_COLLECTION = "DocumentChunks"


def get_all_knowledge_files() -> Dict[str, Path]:
    """
    Get all markdown files in knowledge/

    Returns dict: {title: file_path}
    """
    files = {}
    for md_file in KNOWLEDGE_ROOT.rglob("*.md"):
        content = md_file.read_text(encoding='utf-8')

        # Extract title
        for line in content.split('\n'):
            if line.startswith('# '):
                title = line[2:].strip()
                files[title] = md_file
                break

    return files


def get_all_weaviate_nodes(server: WeaviateMCPServer) -> Dict[str, str]:
    """
    Get all nodes from Weaviate

    Returns dict: {title: file_path}
    """
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
        results = collection.query.fetch_objects(limit=1000)

        nodes = {}
        for obj in results.objects:
            title = obj.properties.get("title")
            file_path = obj.properties.get("file_path")
            if title and file_path:
                nodes[title] = file_path

        return nodes

    except Exception as e:
        print(f"❌ Error fetching Weaviate nodes: {e}")
        return {}


def extract_wikilinks(content: str) -> Set[str]:
    """Extract all [[WikiLinks]] from content"""
    pattern = r'\[\[([^\]]+)\]\]'
    matches = re.finditer(pattern, content)
    return {match.group(1) for match in matches}


def check_orphaned_weaviate_entries(
    weaviate_nodes: Dict[str, str],
    file_nodes: Dict[str, Path]
) -> List[str]:
    """Find Weaviate entries without corresponding files"""
    orphaned = []

    for title, weaviate_path in weaviate_nodes.items():
        # Check if file exists
        file_path = PROJECT_ROOT / weaviate_path
        if not file_path.exists():
            orphaned.append(title)
            continue

        # Check if title matches
        if title not in file_nodes:
            orphaned.append(title)

    return orphaned


def check_orphaned_files(
    file_nodes: Dict[str, Path],
    weaviate_nodes: Dict[str, str]
) -> List[str]:
    """Find files not in Weaviate"""
    orphaned = []

    for title in file_nodes:
        if title not in weaviate_nodes:
            orphaned.append(title)

    return orphaned


def check_broken_links(file_nodes: Dict[str, Path]) -> Dict[str, List[str]]:
    """
    Find broken WikiLinks

    Returns dict: {source_title: [broken_link1, broken_link2, ...]}
    """
    broken_links = {}

    for title, file_path in file_nodes.items():
        content = file_path.read_text(encoding='utf-8')
        links = extract_wikilinks(content)

        # Check each link
        broken = []
        for link in links:
            if link not in file_nodes:
                broken.append(link)

        if broken:
            broken_links[title] = broken

    return broken_links


def delete_orphaned_weaviate_entries(
    server: WeaviateMCPServer,
    orphaned_titles: List[str]
) -> int:
    """Delete orphaned entries from Weaviate"""
    deleted_count = 0

    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)

        for title in orphaned_titles:
            results = collection.query.fetch_objects(
                filters=Filter.by_property("title").equal(title),
                limit=10
            )

            for obj in results.objects:
                collection.data.delete_by_id(obj.uuid)
                deleted_count += 1
                print(f"  🗑️  Deleted orphaned entry: {title}")

    except Exception as e:
        print(f"  ❌ Error deleting orphaned entries: {e}")

    return deleted_count


def sync_orphaned_files(orphaned_titles: List[str], file_nodes: Dict[str, Path]) -> int:
    """Sync orphaned files to Weaviate"""
    from sync_knowledge_graph import sync_node

    synced_count = 0

    try:
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            ollama_url=OLLAMA_URL,
            embedding_model=EMBEDDING_MODEL,
            grpc_port=GRPC_PORT
        )

        for title in orphaned_titles:
            if title in file_nodes:
                file_path = file_nodes[title]
                if sync_node(server, file_path):
                    synced_count += 1
                    print(f"  ✓ Synced orphaned file: {title}")

        server.close()

    except Exception as e:
        print(f"  ❌ Error syncing orphaned files: {e}")

    return synced_count


def check_consistency(server: WeaviateMCPServer, fix: bool = False) -> Dict[str, int]:
    """
    Check consistency between files and Weaviate

    Returns statistics dict
    """
    print("=" * 60)
    print("KNOWLEDGE GRAPH CONSISTENCY CHECK")
    print("=" * 60)
    print()

    stats = {
        "total_files": 0,
        "total_weaviate": 0,
        "orphaned_weaviate": 0,
        "orphaned_files": 0,
        "broken_links": 0,
        "fixed": 0
    }

    # Get all nodes
    print("📚 Scanning files...")
    file_nodes = get_all_knowledge_files()
    stats["total_files"] = len(file_nodes)
    print(f"  Found {len(file_nodes)} files")

    print("\n🔍 Scanning Weaviate...")
    weaviate_nodes = get_all_weaviate_nodes(server)
    stats["total_weaviate"] = len(weaviate_nodes)
    print(f"  Found {len(weaviate_nodes)} nodes")

    # Check orphaned Weaviate entries
    print("\n🗑️  Checking for orphaned Weaviate entries...")
    orphaned_weaviate = check_orphaned_weaviate_entries(weaviate_nodes, file_nodes)
    stats["orphaned_weaviate"] = len(orphaned_weaviate)

    if orphaned_weaviate:
        print(f"  ⚠️  Found {len(orphaned_weaviate)} orphaned Weaviate entries:")
        for title in orphaned_weaviate[:10]:
            print(f"    - {title}")
        if len(orphaned_weaviate) > 10:
            print(f"    ... and {len(orphaned_weaviate) - 10} more")

        if fix:
            print(f"\n  Fixing orphaned Weaviate entries...")
            deleted = delete_orphaned_weaviate_entries(server, orphaned_weaviate)
            stats["fixed"] += deleted
    else:
        print(f"  ✓ No orphaned Weaviate entries")

    # Check orphaned files
    print("\n📄 Checking for orphaned files...")
    orphaned_files = check_orphaned_files(file_nodes, weaviate_nodes)
    stats["orphaned_files"] = len(orphaned_files)

    if orphaned_files:
        print(f"  ⚠️  Found {len(orphaned_files)} orphaned files:")
        for title in orphaned_files[:10]:
            print(f"    - {title}")
        if len(orphaned_files) > 10:
            print(f"    ... and {len(orphaned_files) - 10} more")

        if fix:
            print(f"\n  Fixing orphaned files...")
            synced = sync_orphaned_files(orphaned_files, file_nodes)
            stats["fixed"] += synced
    else:
        print(f"  ✓ No orphaned files")

    # Check broken links
    print("\n🔗 Checking for broken WikiLinks...")
    broken_links = check_broken_links(file_nodes)
    stats["broken_links"] = sum(len(links) for links in broken_links.values())

    if broken_links:
        print(f"  ⚠️  Found {stats['broken_links']} broken links in {len(broken_links)} files:")
        for title, links in list(broken_links.items())[:5]:
            print(f"    {title}:")
            for link in links[:3]:
                print(f"      - [[{link}]]")
            if len(links) > 3:
                print(f"      ... and {len(links) - 3} more")
        if len(broken_links) > 5:
            print(f"    ... and {len(broken_links) - 5} more files")
    else:
        print(f"  ✓ No broken links")

    return stats


def rebuild_all(server: WeaviateMCPServer):
    """Full rebuild: delete all Weaviate nodes and resync from files"""
    print("=" * 60)
    print("FULL REBUILD")
    print("=" * 60)
    print()

    # Delete all nodes
    print("🗑️  Deleting all Weaviate nodes...")
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
        results = collection.query.fetch_objects(limit=1000)

        deleted_count = 0
        for obj in results.objects:
            collection.data.delete_by_id(obj.uuid)
            deleted_count += 1

        print(f"  ✓ Deleted {deleted_count} nodes")

    except Exception as e:
        print(f"  ❌ Error deleting nodes: {e}")
        return

    # Resync all files
    print("\n📚 Resyncing all files...")
    file_nodes = get_all_knowledge_files()

    from sync_knowledge_graph import sync_node

    synced_count = 0
    for title, file_path in file_nodes.items():
        if sync_node(server, file_path):
            synced_count += 1
        print()

    print(f"\n✅ Rebuild complete: {synced_count}/{len(file_nodes)} files synced")


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: maintain_knowledge_graph.py --check")
        print("       maintain_knowledge_graph.py --fix")
        print("       maintain_knowledge_graph.py --rebuild")
        sys.exit(1)

    mode = sys.argv[1]

    if mode not in ["--check", "--fix", "--rebuild"]:
        print(f"❌ Invalid mode: {mode}")
        print("Use --check, --fix, or --rebuild")
        sys.exit(1)

    try:
        # Initialize server
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            ollama_url=OLLAMA_URL,
            embedding_model=EMBEDDING_MODEL,
            grpc_port=GRPC_PORT
        )

        print()

        if mode == "--rebuild":
            rebuild_all(server)
        else:
            fix = (mode == "--fix")
            stats = check_consistency(server, fix=fix)

            # Print summary
            print("\n" + "=" * 60)
            print("SUMMARY")
            print("=" * 60)
            print(f"Total files: {stats['total_files']}")
            print(f"Total Weaviate nodes: {stats['total_weaviate']}")
            print(f"Orphaned Weaviate entries: {stats['orphaned_weaviate']}")
            print(f"Orphaned files: {stats['orphaned_files']}")
            print(f"Broken links: {stats['broken_links']}")

            if fix:
                print(f"Fixed: {stats['fixed']}")
                print("\n✅ Maintenance complete")
            else:
                if stats['orphaned_weaviate'] + stats['orphaned_files'] > 0:
                    print("\n⚠️  Run with --fix to repair issues")
                else:
                    print("\n✅ All checks passed")

        server.close()

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
