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
# v0.2.18: make vco_lib (EmbeddingService) importable.
_VCO_LIB_PARENT = _MCP_DIR.parent
if str(_VCO_LIB_PARENT) not in sys.path:
    sys.path.insert(0, str(_VCO_LIB_PARENT))
# Also expose templates/scripts/ so the local sync_knowledge_graph wrapper
# class is importable. PROJECT_ROOT/.claude/scripts is where these files
# live in a project install; the orchestrator clone has templates/scripts.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# VCO-REWIRE-END: orchestrator-root-resolution

# v0.2.18: pre-v0.2.18 imported `WeaviateMCPServer` from
# `claude_mcp_servers/weaviate_mcp/server.py` — that symbol never existed
# in server.py, so this script was broken at import-time on any path that
# actually exercised it. Switch to the WeaviateWrapper defined in
# `sync_knowledge_graph` (the only working WeaviateMCPServer-alike), and
# the central EmbeddingService that owns embed/slot decisions now.
from sync_knowledge_graph import WeaviateWrapper as WeaviateMCPServer
from vco_lib.embedding_service import (
    EmbeddingService,
    NoEmbeddingBackendError,
)
from weaviate.classes.query import Filter

# Configuration
# v0.2.18: EMBEDDING_MODEL is no longer read here. This script delegates
# embed calls to sync_knowledge_graph.sync_node, which constructs its own
# EmbeddingService. Keeping WEAVIATE_URL / OLLAMA_URL / GRPC_PORT for the
# Weaviate client only.
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))

KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


# v0.2.21 Step 18 (caller migration): resolve KG collection via the
# launcher's vct-hub. Falls back to env (PR-7 / v0.2.11 behaviour) when
# the hub is unreachable. Pre-v0.2.11 this was hardcoded
# "ClaudeKnowledgeGraph", which made every project's maintain run query
# the legacy cross-project collection.
def _resolve_kg_collection() -> str:
    try:
        from vco_lib.project_config import resolve  # type: ignore[import-not-found]
        cfg = resolve(PROJECT_ROOT)
        return cfg.kg_collection or os.getenv("KG_COLLECTION", "KnowledgeGraph")
    except Exception:
        return os.getenv("KG_COLLECTION", "KnowledgeGraph")


KNOWLEDGE_COLLECTION = _resolve_kg_collection()
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


def _fetch_all_objects_paginated(collection, page_size: int = 1000):
    """Fetch every object in a Weaviate v4 collection via cursor pagination.

    v0.2.46 V46-D: fixes silent-truncation footgun. Previously
    `fetch_objects(limit=1000)` silently dropped nodes 1001+ when the
    collection grew, which broke orphan-detection and rebuild logic.
    """
    all_objects = []
    cursor = None
    while True:
        if cursor is not None:
            response = collection.query.fetch_objects(limit=page_size, after=cursor)
        else:
            response = collection.query.fetch_objects(limit=page_size)
        if not response.objects:
            break
        all_objects.extend(response.objects)
        if len(response.objects) < page_size:
            break
        cursor = response.objects[-1].uuid
    return all_objects


def get_all_weaviate_nodes(server: WeaviateMCPServer) -> Dict[str, str]:
    """
    Get all nodes from Weaviate

    Returns dict: {title: file_path}

    v0.2.46 V46-D: uses cursor pagination so collections > 1000 nodes
    are fully enumerated (orphan-detection previously missed nodes 1001+).
    """
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
        objects = _fetch_all_objects_paginated(collection)

        nodes = {}
        for obj in objects:
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


def sync_orphaned_files(
    orphaned_titles: List[str],
    file_nodes: Dict[str, Path],
    server: WeaviateMCPServer,
) -> int:
    """Sync orphaned files to Weaviate.

    v0.2.18: takes the WeaviateMCPServer (sync_knowledge_graph's
    WeaviateWrapper) from the caller instead of constructing its own —
    the wrapper requires an EmbeddingService at construction time, which
    main() already owns. Avoids a second-EmbeddingService-instance
    footgun (each instance probes backends afresh).
    """
    from sync_knowledge_graph import sync_node

    synced_count = 0

    try:
        for title in orphaned_titles:
            if title in file_nodes:
                file_path = file_nodes[title]
                if sync_node(server, file_path):
                    synced_count += 1
                    print(f"  ✓ Synced orphaned file: {title}")

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
            synced = sync_orphaned_files(orphaned_files, file_nodes, server)
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
    # v0.2.46 V46-D: cursor-paginate so collections > 1000 nodes are
    # fully drained (previously rebuild left nodes 1001+ undeleted,
    # producing stale ghosts after resync).
    print("🗑️  Deleting all Weaviate nodes...")
    try:
        collection = server.client.collections.get(KNOWLEDGE_COLLECTION)
        objects = _fetch_all_objects_paginated(collection)

        deleted_count = 0
        for obj in objects:
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

    embedding_service = None
    server = None
    try:
        # v0.2.18: construct EmbeddingService at script entry. Even
        # --check (read-only) needs it because the WeaviateWrapper now
        # takes it at construction time (the wrapper's `text_vector_slot`
        # property reads from it).
        try:
            embedding_service = EmbeddingService.for_project(PROJECT_ROOT)
        except NoEmbeddingBackendError as e:
            # --check is read-only and doesn't actually need to embed
            # anything, but the WeaviateWrapper requires an
            # EmbeddingService at construction. Without a backend, we
            # can still query Weaviate (no embed needed for the
            # consistency check itself) — but --fix and --rebuild WILL
            # need to embed via sync_node, so they have to abort.
            print(f"❌ No embedding backend reachable: {e}", file=sys.stderr)
            print(
                "   See .claude/context/EMBEDDING_FAILURES.md + "
                "~/.claude/metrics/embedding_failures.jsonl",
                file=sys.stderr,
            )
            sys.exit(1)

        # Initialize Weaviate client + bind to the embedding service
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            embedding_service=embedding_service,
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
            # v0.2.18: surface the active named-vector slot so operators
            # can sanity-check which model maintain is using for write
            # paths (--fix / --rebuild). Read-only check doesn't depend
            # on it, but logging it is cheap and useful.
            print(f"Active text slot: {embedding_service.text_vector_slot}")

            if fix:
                print(f"Fixed: {stats['fixed']}")
                print("\n✅ Maintenance complete")
            else:
                if stats['orphaned_weaviate'] + stats['orphaned_files'] > 0:
                    print("\n⚠️  Run with --fix to repair issues")
                else:
                    print("\n✅ All checks passed")

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        if embedding_service is not None:
            try:
                embedding_service.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
