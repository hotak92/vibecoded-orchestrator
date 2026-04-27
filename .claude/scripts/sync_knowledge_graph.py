#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Knowledge Graph Weaviate Sync Script

Syncs knowledge graph markdown files to Weaviate collection.
Called by Claude hooks after file edits in knowledge/ directory.

Handles chunking for large files (>6k tokens) to stay within embedding model limits.

Usage:
    python .claude/scripts/sync_knowledge_graph.py <file_path>
    python .claude/scripts/sync_knowledge_graph.py --all  # Sync all knowledge files
"""

import sys
import os
import re
import time
import yaml
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import uuid

# Add MCP server to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "claude_mcp_servers"))

import weaviate
from weaviate.classes.query import Filter
from weaviate_mcp.chunking import TokenCounter, Chunker

# Try to import query logger
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "logs"))
    from query_logger import ToolUsageLogger
    HAS_LOGGER = True
except Exception as e:
    HAS_LOGGER = False

# Configuration - Read from environment variables (set by MCP servers or project settings)
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
COLLECTION_NAME = os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph")
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Chunking configuration for embedding limits
# Note: Actual working limit is ~2500 tokens despite 8k model spec
MAX_EMBEDDING_TOKENS = 2500  # Conservative limit based on testing

# Project root - use KG_BASE_DIR if set (multi-project support), else default to orchestrator root
_kg_base_dir = os.getenv("KG_BASE_DIR", "")
PROJECT_ROOT = Path(_kg_base_dir) if _kg_base_dir else Path(os.environ.get("CLAUDE_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent.parent)))
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


# Simple wrapper to replace WeaviateMCPServer
class WeaviateWrapper:
    """Wrapper to provide simple Weaviate client access"""
    def __init__(self, weaviate_url, ollama_url=None, embedding_model=None, grpc_port=None):
        http_host = weaviate_url.replace("http://", "").replace("https://", "").split(":")[0]
        http_port = int(weaviate_url.split(":")[-1]) if ":" in weaviate_url else 8080

        self.client = weaviate.connect_to_custom(
            http_host=http_host,
            http_port=http_port,
            http_secure=False,
            grpc_host=http_host,
            grpc_port=grpc_port or 50051,
            grpc_secure=False
        )
        self.ollama_url = ollama_url or "http://localhost:11435"
        self.embedding_model = embedding_model or "qwen3-embedding:0.6b"

    def close(self) -> None:
        """Close the Weaviate connection to prevent resource leaks."""
        try:
            self.client.close()
        except Exception:
            pass

    def _get_embedding(self, text: str) -> List[float]:
        """Get embedding from Ollama"""
        import requests
        response = requests.post(
            f"{self.ollama_url}/api/embeddings",
            json={"model": self.embedding_model, "prompt": text}
        )
        response.raise_for_status()
        return response.json()["embedding"]


# For backward compatibility
WeaviateMCPServer = WeaviateWrapper


def _update_frontmatter_timestamp(file_path: Path, content: str) -> str:
    """
    Update the `updated:` field in YAML frontmatter to current UTC time.
    Writes the updated content back to the file and returns it.
    Called automatically on every sync so the timestamp reflects actual edits.
    """
    if not content.strip().startswith('---'):
        return content

    parts = content.split('---', 2)
    if len(parts) < 3:
        return content

    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    fm_text = parts[1]

    updated_pattern = re.compile(r'^updated:.*$', re.MULTILINE)
    if updated_pattern.search(fm_text):
        new_fm = updated_pattern.sub(f'updated: {now_iso}', fm_text)
    else:
        # Add after 'created:' line if present, else append before end of block
        created_pattern = re.compile(r'^(created:.*)$', re.MULTILINE)
        if created_pattern.search(fm_text):
            new_fm = created_pattern.sub(r'\1\nupdated: ' + now_iso, fm_text)
        else:
            new_fm = fm_text.rstrip('\n') + f'\nupdated: {now_iso}\n'

    new_content = '---' + new_fm + '---' + parts[2]
    file_path.write_text(new_content, encoding='utf-8')
    return new_content


def parse_frontmatter(content: str) -> Tuple[Optional[Dict], str]:
    """
    Parse YAML frontmatter from markdown content.

    Args:
        content: Markdown file content

    Returns:
        Tuple of (frontmatter_dict, content_without_frontmatter)
    """
    if not content.strip().startswith('---'):
        return None, content

    parts = content.split('---', 2)
    if len(parts) < 3:
        return None, content

    try:
        frontmatter = yaml.safe_load(parts[1])
        content_without_fm = parts[2].strip()
        return frontmatter, content_without_fm
    except yaml.YAMLError:
        return None, content


def validate_node_against_vocabulary(node_data: Dict, file_path: Path) -> List[str]:
    """
    Validate node against vocabulary and tag hierarchy rules.

    Args:
        node_data: Parsed node data
        file_path: Path to node file

    Returns:
        List of validation warnings (empty if all valid)
    """
    warnings = []

    # 1. Type validation
    valid_types = {"project", "concept", "tool", "research", "model", "hardware", "pattern", "insight", "guide"}
    node_type = node_data.get("node_type", "")
    if node_type not in valid_types:
        warnings.append(f"Invalid node type '{node_type}' (valid: {', '.join(sorted(valid_types))})")

    # 2. Tag validation
    tags = node_data.get("tags", [])

    # Check tag count (3-10 recommended)
    if len(tags) < 3:
        warnings.append(f"Too few tags ({len(tags)}) - recommended 3-10 tags")
    elif len(tags) > 10:
        warnings.append(f"Too many tags ({len(tags)}) - recommended 3-10 tags")

    # Check tag format
    for tag in tags:
        # Tags should be lowercase or UPPERCASE (acronyms)
        # Multi-word tags should use hyphens
        if " " in tag:
            warnings.append(f"Tag '{tag}' contains spaces - use hyphens instead")
        if "_" in tag:
            warnings.append(f"Tag '{tag}' uses underscores - use hyphens instead")
        # Check for camelCase (not allowed except for acronyms)
        if any(c.isupper() for c in tag) and not tag.isupper() and "-" not in tag:
            # Could be acronym like "AI" or camelCase like "MyTag"
            if len([c for c in tag if c.isupper()]) > 1 and not tag.isupper():
                warnings.append(f"Tag '{tag}' uses camelCase - use lowercase with hyphens")

    # Check for recommended tag categories (for technical nodes)
    if node_type in {"project", "concept", "tool", "pattern"}:
        # Should have at least 1 domain tag
        domain_tags = {"AI", "ML", "NLP", "CV", "database", "workflow", "tooling",
                      "infrastructure", "frontend", "backend", "security"}
        has_domain = any(tag in domain_tags for tag in tags)

        # Should have abstraction level (except for tools)
        abstraction_tags = {"high-level-plan", "mid-level-architecture",
                          "low-level-implementation", "function-description"}
        has_abstraction = any(tag in abstraction_tags for tag in tags)

        if not has_domain:
            warnings.append("No domain tag found (recommended: #AI, #database, #workflow, etc.)")

        if not has_abstraction and node_type != "tool":
            warnings.append("No abstraction level tag (recommended: #high-level-plan, #mid-level-architecture, #low-level-implementation)")

    # 3. External links validation (if present)
    external_links = node_data.get("external_links", "")
    if external_links:
        try:
            import json
            links = json.loads(external_links) if isinstance(external_links, str) else external_links
            if not isinstance(links, dict):
                warnings.append("external_links should be a dictionary")
        except (json.JSONDecodeError, TypeError):
            warnings.append("external_links is not valid JSON")

    return warnings


def parse_markdown_node(content: str, file_path: Path) -> Dict:
    """
    Parse markdown file to extract knowledge node data

    Args:
        content: Markdown file content
        file_path: Path to markdown file

    Returns:
        Dictionary with node data (title, tags, links, etc.)
    """
    # Parse YAML frontmatter (if present)
    frontmatter, content_body = parse_frontmatter(content)

    lines = content.strip().split('\n')

    # Extract title (from frontmatter or first # heading)
    if frontmatter and 'title' in frontmatter:
        title = frontmatter['title']
    else:
        title = file_path.stem  # Default to filename
        for line in lines:
            if line.startswith('# '):
                title = line[2:].strip()
                break

    # Extract tags (from frontmatter or inline)
    tags = []
    if frontmatter and 'tags' in frontmatter:
        # Frontmatter tags (array format) - convert all to strings
        raw_tags = frontmatter['tags'] if isinstance(frontmatter['tags'], list) else []
        tags = [str(tag) for tag in raw_tags]
    else:
        # Inline tags (Obsidian style: #tag)
        tag_pattern = r'#([a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)'
        for match in re.finditer(tag_pattern, content):
            tag = match.group(1)
            if tag not in tags:
                tags.append(tag)

    # Extract WikiLinks - supports both typed and untyped
    # Typed: [[uses::Redis]], [[implements::Pattern]]
    # Untyped: [[Redis]] (defaults to "relatedTo")
    links = []  # Untyped links (backward compatibility)
    typed_links = []  # New: Typed relationships

    # Updated pattern to capture optional relationship type
    # Matches: [[type::target]] or [[target]]
    link_pattern = r'\[\[(?:([a-zA-Z_]+)::)?([^\]]+)\]\]'

    for match in re.finditer(link_pattern, content):
        relation_type = match.group(1)  # None if untyped
        target_title = match.group(2).strip()

        if relation_type:
            # Typed relationship
            typed_link = {
                "relation_type": relation_type,
                "target_title": target_title
            }
            if typed_link not in typed_links:
                typed_links.append(typed_link)
        else:
            # Untyped (backward compatibility)
            if target_title not in links:
                links.append(target_title)

    # Node type (from frontmatter or directory)
    if frontmatter and 'type' in frontmatter:
        node_type = frontmatter['type']
    else:
        rel_path = file_path.relative_to(KNOWLEDGE_ROOT)
        node_type = str(rel_path.parts[0]) if len(rel_path.parts) > 1 else "general"

    # Temporal metadata from frontmatter
    temporal_data = {}
    if frontmatter:
        # Created/updated timestamps (YAML parses ISO timestamps as datetime objects)
        if 'created' in frontmatter and frontmatter['created'] != 'unknown':
            try:
                val = frontmatter['created']
                # If already a datetime object, use it directly
                if isinstance(val, datetime):
                    temporal_data['created'] = val.isoformat()
                else:
                    # Parse string format
                    val_str = str(val)
                    if 'T' in val_str:
                        created_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        created_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['created'] = created_dt.isoformat()
            except (ValueError, TypeError) as e:
                pass

        if 'updated' in frontmatter and frontmatter['updated'] != 'unknown':
            try:
                val = frontmatter['updated']
                if isinstance(val, datetime):
                    temporal_data['updated'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        updated_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        updated_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['updated'] = updated_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # Valid from/until timestamps
        if 'valid_from' in frontmatter:
            try:
                val = frontmatter['valid_from']
                if isinstance(val, datetime):
                    temporal_data['valid_from'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        valid_from_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        valid_from_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['valid_from'] = valid_from_dt.isoformat()
            except (ValueError, TypeError):
                pass

        if 'valid_until' in frontmatter and frontmatter['valid_until'] is not None:
            try:
                val = frontmatter['valid_until']
                if isinstance(val, datetime):
                    temporal_data['valid_until'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        valid_until_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        valid_until_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['valid_until'] = valid_until_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # Status
        if 'status' in frontmatter:
            temporal_data['status'] = frontmatter['status']

    # External links from frontmatter (RDF-inspired)
    external_links = ""
    if frontmatter and 'external_links' in frontmatter:
        ext_links = frontmatter['external_links']
        if isinstance(ext_links, dict):
            # Convert dict to JSON string for storage (Weaviate TEXT field)
            import json
            external_links = json.dumps(ext_links)

    # Fallback: File timestamps for old created_at/updated_at fields
    stat = file_path.stat()
    created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    result = {
        "title": title,
        "content": content,
        "file_path": str(file_path.relative_to(PROJECT_ROOT)),
        "node_type": node_type,
        "tags": tags,
        "links": links,
        "typed_links": typed_links,  # Typed relationships
        "external_links": external_links,  # External links (DBpedia, official docs, etc.)
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat()
    }

    # Add temporal metadata if present
    result.update(temporal_data)

    return result


def ensure_collection_exists(server: WeaviateMCPServer) -> bool:
    """
    Ensure ClaudeKnowledgeGraph collection exists with proper schema

    Args:
        server: Weaviate MCP server instance

    Returns:
        True if collection exists or was created
    """
    try:
        from weaviate.classes.config import Configure, Property, DataType

        if server.client.collections.exists(COLLECTION_NAME):
            print(f"✓ Collection '{COLLECTION_NAME}' exists")

            # Check if temporal properties and references exist, add them if missing
            try:
                collection = server.client.collections.get(COLLECTION_NAME)
                config = collection.config.get()
                existing_props = {prop.name for prop in config.properties}
                existing_refs = {ref.name for ref in (config.references or [])}

                # Define temporal properties
                temporal_props = {
                    'created': DataType.DATE,
                    'updated': DataType.DATE,
                    'valid_from': DataType.DATE,
                    'valid_until': DataType.DATE,
                    'status': DataType.TEXT
                }

                # Add missing properties
                for prop_name, prop_type in temporal_props.items():
                    if prop_name not in existing_props:
                        print(f"  Adding property: {prop_name}")
                        collection.config.add_property(
                            Property(name=prop_name, data_type=prop_type)
                        )

                # Add typed_links property if missing
                if 'typed_links' not in existing_props:
                    print(f"  Adding property: typed_links (nested objects)")
                    collection.config.add_property(
                        Property(
                            name="typed_links",
                            data_type=DataType.OBJECT_ARRAY,
                            nested_properties=[
                                Property(name="relation_type", data_type=DataType.TEXT),
                                Property(name="target_title", data_type=DataType.TEXT)
                            ]
                        )
                    )

                # Add external_links property if missing (RDF-inspired)
                if 'external_links' not in existing_props:
                    print(f"  Adding property: external_links (JSON text)")
                    collection.config.add_property(
                        Property(name="external_links", data_type=DataType.TEXT)
                    )

                # Add cross-reference property if missing
                from weaviate.classes.config import ReferenceProperty
                if 'linksTo' not in existing_refs:
                    print(f"  Adding cross-reference: linksTo")
                    collection.config.add_reference(
                        ReferenceProperty(
                            name="linksTo",
                            target_collection=COLLECTION_NAME
                        )
                    )

                print(f"✓ Schema up to date")
            except Exception as e:
                print(f"⚠️  Could not update schema: {e}")

            return True

        print(f"Creating collection '{COLLECTION_NAME}'...")

        # Create collection with proper schema (supports chunking + temporal + typed links)
        server.client.collections.create(
            name=COLLECTION_NAME,
            description="Claude knowledge graph nodes with semantic search (chunked for large files)",
            properties=[
                Property(name="title", data_type=DataType.TEXT),
                Property(name="content", data_type=DataType.TEXT),
                Property(name="file_path", data_type=DataType.TEXT),
                Property(name="node_type", data_type=DataType.TEXT),
                Property(name="tags", data_type=DataType.TEXT_ARRAY),
                Property(name="links", data_type=DataType.TEXT_ARRAY),
                # NEW: Typed relationships as JSON objects
                Property(
                    name="typed_links",
                    data_type=DataType.OBJECT_ARRAY,
                    nested_properties=[
                        Property(name="relation_type", data_type=DataType.TEXT),
                        Property(name="target_title", data_type=DataType.TEXT)
                    ]
                ),
                # External links (RDF-inspired: DBpedia, official docs, GitHub, etc.)
                # Stored as JSON text since Weaviate OBJECT requires nested properties
                Property(name="external_links", data_type=DataType.TEXT),
                Property(name="created_at", data_type=DataType.DATE),
                Property(name="updated_at", data_type=DataType.DATE),
                # Temporal metadata (from frontmatter)
                Property(name="created", data_type=DataType.DATE),
                Property(name="updated", data_type=DataType.DATE),
                Property(name="valid_from", data_type=DataType.DATE),
                Property(name="valid_until", data_type=DataType.DATE),
                Property(name="status", data_type=DataType.TEXT),
                # Chunking support
                Property(name="chunk_num", data_type=DataType.INT),
                Property(name="total_chunks", data_type=DataType.INT),
                Property(name="source_node_id", data_type=DataType.TEXT)
            ],
            vectorizer_config=Configure.Vectorizer.none()  # We provide vectors manually
        )

        print(f"✓ Created collection '{COLLECTION_NAME}'")
        return True

        if result["success"]:
            print(f"✓ Created collection '{COLLECTION_NAME}'")
            return True
        else:
            print(f"❌ Failed to create collection: {result.get('message')}")
            return False

    except Exception as e:
        print(f"❌ Error ensuring collection: {e}")
        return False


def infer_tags_from_typed_links(server: WeaviateMCPServer, node_data: Dict) -> List[str]:
    """
    Infer tags from typed relationships BEFORE storing to Weaviate.

    Inference rules:
    1. Inherit capability tags from used/implemented tools
    2. Propagate domain tags through relationships

    Args:
        server: Weaviate MCP server instance
        node_data: Parsed node data with typed_links

    Returns:
        List of inferred tags
    """
    typed_links = node_data.get("typed_links", [])
    existing_tags = set(node_data.get("tags", []))
    inferred_tags = []

    if not typed_links:
        return inferred_tags

    # Relationship types that propagate properties
    CAPABILITY_RELATIONS = ["uses", "implements", "buildsOn"]
    TAG_RELATIONS = ["uses", "implements", "extends", "buildsOn"]

    try:
        collection = server.client.collections.get(COLLECTION_NAME)

        for link in typed_links:
            relation = link.get("relation_type", "")
            target_title = link.get("target_title", "")

            # Query target node
            results = collection.query.fetch_objects(
                filters=Filter.by_property("title").equal(target_title) &
                       Filter.by_property("chunk_num").equal(1),
                limit=1,
                return_properties=["tags", "node_type"]
            )

            if not results.objects:
                continue

            target_props = results.objects[0].properties
            target_tags = target_props.get("tags", [])

            # Rule 1: Inherit capability tags from used/implemented tools
            if relation in CAPABILITY_RELATIONS:
                capability_tags = [t for t in target_tags if "-" in t]
                for cap in capability_tags:
                    if cap not in existing_tags and cap not in inferred_tags:
                        inferred_tags.append(cap)

            # Rule 2: Propagate domain tags through relationships
            if relation in TAG_RELATIONS:
                domain_tags = [t for t in target_tags if t.upper() == t or len(t) < 15]
                for tag in domain_tags:
                    if (tag not in existing_tags and
                        tag not in inferred_tags and
                        tag not in ["test", "project", "concept", "tool"]):
                        inferred_tags.append(tag)

    except Exception as e:
        # Inference is best-effort - don't fail sync if it errors
        pass

    return inferred_tags


def resolve_wikilinks_to_uuids(server: WeaviateMCPServer, wikilinks: List[str]) -> List[str]:
    """
    Resolve WikiLink titles to Weaviate UUIDs.

    Args:
        server: Weaviate MCP server instance
        wikilinks: List of WikiLink titles (e.g., ["Node Title 1", "Node Title 2"])

    Returns:
        List of UUIDs for matching nodes
    """
    if not wikilinks:
        return []

    try:
        collection = server.client.collections.get(COLLECTION_NAME)
        uuids = []

        for link_title in wikilinks:
            # Query for nodes with matching title (case-insensitive)
            # Note: For chunked nodes, we want the parent node, not chunks
            results = collection.query.fetch_objects(
                filters=Filter.by_property("title").equal(link_title) &
                       Filter.by_property("chunk_num").equal(1),  # Get first chunk (has full metadata)
                limit=1
            )

            if results.objects:
                uuids.append(str(results.objects[0].uuid))

        return uuids

    except Exception as e:
        print(f"    ⚠️  Could not resolve WikiLinks: {e}")
        return []


def sync_node(server: WeaviateMCPServer, file_path: Path) -> bool:
    """
    Sync a single knowledge node to Weaviate (with chunking support)

    Args:
        server: Weaviate MCP server instance
        file_path: Path to markdown file

    Returns:
        True if successful
    """
    start_time = time.time()
    chunks_created = 0
    error_msg = None

    try:
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            error_msg = "File not found"
            return False

        # Read, auto-update `updated:` timestamp, write back, then parse
        content = file_path.read_text(encoding='utf-8')
        content = _update_frontmatter_timestamp(file_path, content)
        node_data = parse_markdown_node(content, file_path)

        # Validate against vocabulary (report warnings, don't block sync).
        # Silenced by default during install (VCT_VERBOSE_VOCAB_WARNINGS=0
        # implicit) — these are contributor-facing lint about KG node tag
        # hygiene, not actionable for end users running first-install.sh.
        # Set VCT_VERBOSE_VOCAB_WARNINGS=1 (or run sync_knowledge_graph.py
        # directly with that env var) to see them when curating the KG.
        validation_warnings = validate_node_against_vocabulary(node_data, file_path)
        if validation_warnings and os.environ.get("VCT_VERBOSE_VOCAB_WARNINGS", "0") == "1":
            print(f"⚠️  Vocabulary validation warnings ({len(validation_warnings)}):")
            for warning in validation_warnings[:3]:  # Show first 3
                print(f"   - {warning}")
            if len(validation_warnings) > 3:
                print(f"   ... and {len(validation_warnings) - 3} more warnings")

        # Run inference BEFORE storing (enrich with inferred tags)
        inferred_tags = infer_tags_from_typed_links(server, node_data)
        if inferred_tags:
            # Add inferred tags to node data (will be stored with original tags)
            node_data["tags"] = list(set(node_data["tags"] + inferred_tags))
            print(f"🧠 Inferred {len(inferred_tags)} tags from typed relationships")

        print(f"🔄 Syncing node: {node_data['title']} ({node_data['node_type']})")
        print(f"   Tags: {', '.join(node_data['tags']) if node_data['tags'] else 'none'}")
        total_links = len(node_data['links']) + len(node_data['typed_links'])
        typed_count = len(node_data['typed_links'])
        print(f"   Links: {total_links} connections ({typed_count} typed)")

        # Delete old version (by file_path)
        collection = server.client.collections.get(COLLECTION_NAME)

        # Query for existing nodes with same file_path
        where_filter = Filter.by_property("file_path").equal(node_data["file_path"])
        existing = collection.query.fetch_objects(filters=where_filter, limit=100)

        deleted_count = 0
        for obj in existing.objects:
            collection.data.delete_by_id(obj.uuid)
            deleted_count += 1

        if deleted_count > 0:
            print(f"   ✓ Deleted {deleted_count} old version(s)")

        # Check if content needs chunking
        token_count = TokenCounter.count_tokens(content)
        print(f"   Content size: {token_count} tokens")

        if token_count <= MAX_EMBEDDING_TOKENS:
            # Single chunk - store as-is
            print(f"   Storing as single object")

            # Get embedding for content
            embedding = server._get_embedding(content)

            # Prepare data object
            data_obj = {
                "title": node_data["title"],
                "content": node_data["content"],
                "file_path": node_data["file_path"],
                "node_type": node_data["node_type"],
                "tags": node_data["tags"],
                "links": node_data["links"],
                "typed_links": node_data["typed_links"],  # Typed relationships
                "external_links": node_data["external_links"],  # External links (RDF)
                "created_at": node_data["created_at"],
                "updated_at": node_data["updated_at"],
                "chunk_num": 1,
                "total_chunks": 1,
                "source_node_id": str(uuid.uuid4())
            }

            # Add temporal metadata if present
            for field in ['created', 'updated', 'valid_from', 'valid_until', 'status']:
                if field in node_data:
                    data_obj[field] = node_data[field]

            # Insert with embedding (named vector if dual embedding enabled)
            vector_arg = {"ollama_embed": embedding} if DUAL_EMBEDDING_ENABLED else embedding
            obj_uuid = collection.data.insert(
                properties=data_obj,
                vector=vector_arg
            )

            chunks_created = 1
            print(f"   ✓ Stored node with UUID: {str(obj_uuid)[:8]}...")

            # Create cross-references for WikiLinks
            if node_data["links"]:
                target_uuids = resolve_wikilinks_to_uuids(server, node_data["links"])
                if target_uuids:
                    for target_uuid in target_uuids:
                        try:
                            collection.data.reference_add(
                                from_uuid=obj_uuid,
                                from_property="linksTo",
                                to=target_uuid
                            )
                        except Exception as e:
                            # Silently skip if reference already exists or target not found
                            pass
                    print(f"   ✓ Created {len(target_uuids)} cross-references")

        else:
            # Multiple chunks needed
            print(f"   ⚠️  Content exceeds {MAX_EMBEDDING_TOKENS} tokens - chunking required")

            # Generate source_node_id for all chunks
            source_node_id = str(uuid.uuid4())

            # Use chunker with max_tokens = MAX_EMBEDDING_TOKENS
            chunker = Chunker(
                min_tokens=1500,  # Half of max for better boundaries
                max_tokens=MAX_EMBEDDING_TOKENS,
                target_tokens=2500
            )

            chunks = chunker.chunk_text(
                text=content,
                source_id=source_node_id,
                metadata={
                    "title": node_data["title"],
                    "file_path": node_data["file_path"],
                    "node_type": node_data["node_type"]
                }
            )

            print(f"   Split into {len(chunks)} chunks")

            # Store each chunk
            for i, chunk in enumerate(chunks):
                # Get embedding for chunk content
                embedding = server._get_embedding(chunk.content)

                # Prepare data object (tags, links, typed_links, external_links shared across all chunks)
                data_obj = {
                    "title": node_data["title"],
                    "content": chunk.content,
                    "file_path": node_data["file_path"],
                    "node_type": node_data["node_type"],
                    "tags": node_data["tags"],
                    "links": node_data["links"],
                    "typed_links": node_data["typed_links"],  # Typed relationships
                    "external_links": node_data["external_links"],  # External links (RDF)
                    "created_at": node_data["created_at"],
                    "updated_at": node_data["updated_at"],
                    "chunk_num": chunk.chunk_number + 1,  # 1-indexed
                    "total_chunks": chunk.total_chunks,
                    "source_node_id": source_node_id
                }

                # Add temporal metadata if present
                for field in ['created', 'updated', 'valid_from', 'valid_until', 'status']:
                    if field in node_data:
                        data_obj[field] = node_data[field]

                # Insert with embedding (named vector if dual embedding enabled)
                vector_arg = {"ollama_embed": embedding} if DUAL_EMBEDDING_ENABLED else embedding
                obj_uuid = collection.data.insert(
                    properties=data_obj,
                    vector=vector_arg
                )

                # Create cross-references only from first chunk (represents the main node)
                if chunk.chunk_number == 0 and node_data["links"]:
                    target_uuids = resolve_wikilinks_to_uuids(server, node_data["links"])
                    if target_uuids:
                        for target_uuid in target_uuids:
                            try:
                                collection.data.reference_add(
                                    from_uuid=obj_uuid,
                                    from_property="linksTo",
                                    to=target_uuid
                                )
                            except Exception as e:
                                pass
                        print(f"   ✓ Created {len(target_uuids)} cross-references")

                chunks_created += 1
                print(f"   ✓ Stored chunk {chunk.chunk_number + 1}/{chunk.total_chunks} ({chunk.token_count} tokens)")

        print(f"✅ Successfully synced {node_data['title']}")
        return True

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Error syncing node: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Log usage
        if HAS_LOGGER:
            duration_ms = (time.time() - start_time) * 1000
            ToolUsageLogger.log_kg_sync(
                file_path=str(file_path),
                chunks_created=chunks_created,
                duration_ms=duration_ms,
                success=error_msg is None,
                error=error_msg
            )


def sync_all_nodes(server: WeaviateMCPServer) -> Tuple[int, int]:
    """
    Sync all knowledge graph markdown files

    Args:
        server: Weaviate MCP server instance

    Returns:
        (success_count, fail_count)
    """
    success_count = 0
    fail_count = 0

    # Find all .md files in knowledge/
    md_files = list(KNOWLEDGE_ROOT.rglob("*.md"))

    # Exclude meta files (schema/reference documentation, not searchable content)
    EXCLUDED_FILES = {'TAG_HIERARCHY.md', 'VOCABULARY.md'}
    md_files = [f for f in md_files if f.name not in EXCLUDED_FILES]

    print(f"📚 Found {len(md_files)} markdown files in knowledge/")
    print()

    for md_file in sorted(md_files):
        if sync_node(server, md_file):
            success_count += 1
        else:
            fail_count += 1
        print()  # Blank line between nodes

    return success_count, fail_count


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: sync_knowledge_graph.py <file_path>")
        print("       sync_knowledge_graph.py --all")
        sys.exit(1)

    try:
        # Initialize server
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            ollama_url=OLLAMA_URL,
            embedding_model=EMBEDDING_MODEL,
            grpc_port=GRPC_PORT
        )

        # Ensure collection exists
        if not ensure_collection_exists(server):
            print("❌ Cannot proceed without collection")
            sys.exit(1)

        print()

        # Sync files
        if sys.argv[1] == "--all":
            success, fail = sync_all_nodes(server)
            print(f"📊 Results: {success} succeeded, {fail} failed")
            sys.exit(0 if fail == 0 else 1)
        else:
            file_path = Path(sys.argv[1]).resolve()

            # Check if file is in knowledge/ directory
            try:
                file_path.relative_to(KNOWLEDGE_ROOT)
            except ValueError:
                print(f"ℹ️  File not in knowledge/ directory, skipping")
                sys.exit(0)

            # Sync single file
            success = sync_node(server, file_path)
            sys.exit(0 if success else 1)

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            server.close()
        except:
            pass


if __name__ == "__main__":
    main()
