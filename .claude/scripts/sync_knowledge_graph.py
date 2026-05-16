#!/usr/bin/env python3
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

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# Add MCP server to path. We resolve relative to this script's location
# rather than a hardcoded path so the script ships portable across Linux,
# macOS, and Windows installs (audit finding 2026-04-30).
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_HOME = _SCRIPT_DIR.parent.parent  # .claude/scripts/X → .claude → project
sys.path.insert(0, str(_PROJECT_HOME / "claude_mcp_servers"))
# VCO-REWIRE-END: orchestrator-root-resolution

import weaviate
from weaviate.classes.query import Filter
from weaviate_mcp.chunking import TokenCounter, Chunker

# Try to import query logger
try:
    sys.path.insert(0, str(_PROJECT_HOME / ".claude" / "logs"))
    from query_logger import ToolUsageLogger
    HAS_LOGGER = True
except Exception as e:
    HAS_LOGGER = False

# Configuration - Read from environment variables (set by MCP servers or project settings)
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
COLLECTION_NAME = os.getenv("KG_COLLECTION", "KnowledgeGraph")
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Chunking configuration for embedding limits
# Note: Actual working limit is ~2500 tokens despite 8k model spec
MAX_EMBEDDING_TOKENS = 2500  # Conservative limit based on testing

# Project root - use KG_BASE_DIR if set (multi-project support), else
# infer from this script's location (.claude/scripts/X → project root).
# Cross-OS: Path.parent.parent works on every supported platform.
_kg_base_dir = os.getenv("KG_BASE_DIR", "")
PROJECT_ROOT = Path(_kg_base_dir) if _kg_base_dir else _PROJECT_HOME
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"

# Development docs collection (project-scoped). Uses the same chunker, named
# vectors, and `index_null_state=True` schema as the KG collection — the only
# differences are: docs may have no frontmatter, no typed WikiLinks, no
# tags/status (we synthesize a small set from the filesystem).
DEV_COLLECTION_NAME = os.getenv("DEVELOPMENT_COLLECTION", "")
DOCS_ROOT = PROJECT_ROOT / "docs"

# Named-vector slots in the KG/dev schema. The schema declares all three at
# create time so each can be populated later without recreating the collection
# (Weaviate 1.28 doesn't support post-create named-vector additions —
# Reconfigure.NamedVectors.update only mutates index settings on existing
# slots, not adds. Weaviate 1.31+ supports add via the Python client; if/when
# we upgrade, this map can grow at runtime.).
_KG_NAMED_VECTOR_SLOTS = ("qwen3_embed", "ollama_embed", "openai_embed")
_ACTIVE_EMBEDDING = os.getenv("ACTIVE_EMBEDDING", "qwen3")


def _active_named_vector_for_kg() -> str:
    """Return the named-vector slot for KG writes from this script.

    **This script is qwen3-only.** The `WeaviateWrapper._get_embedding`
    helper hardcodes qwen3-embedding:0.6b — it does NOT dispatch on
    `ACTIVE_EMBEDDING`. So we must assert here that the env says qwen3,
    otherwise we'd produce qwen3 vectors and stuff them into a slot
    labelled for arctic/openai (audit finding KG-W1, 2026-04-30).

    For multi-model writes (e.g. `ACTIVE_EMBEDDING=openai`), use the
    `store_knowledge_node` MCP tool — it calls `_get_all_kg_embeddings`
    which produces all three vectors from their proper models.
    """
    if _ACTIVE_EMBEDDING not in ("qwen3", "codesage"):
        raise RuntimeError(
            f"sync_knowledge_graph.py is qwen3-only but ACTIVE_EMBEDDING="
            f"{_ACTIVE_EMBEDDING!r}. Use the `store_knowledge_node` MCP "
            f"tool for arctic/openai writes (it dispatches to the right "
            f"model per slot). To force qwen3 sync, set ACTIVE_EMBEDDING=qwen3."
        )
    return "qwen3_embed"


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

        # `valid_until` semantics:
        #   - Frontmatter omits it OR sets it to None → "never expires"; the
        #     property is left unset (null) in the DB.
        #   - Frontmatter sets a real date → write it.
        #
        # Null is filterable because the collection is created with
        # `inverted_index_config=Configure.inverted_index(index_null_state=True)`
        # (see `_create_kg_collection`). The MCP `_stale_filter()` then uses
        # `valid_until is_none(True) | valid_until > now`. Setting that
        # config at create time is required — Weaviate doesn't allow toggling
        # it later (`Reconfigure.inverted_index` lacks `index_null_state`).
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
    Ensure the project's KG_COLLECTION (env-resolved, fallback "KnowledgeGraph")
    exists with proper schema

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
            # Named vectors must match `VECTOR_SCHEMES["kg"]` in
            # weaviate_mcp/server.py:130. Without this the collection accepts
            # only the unnamed default vector, and per-named-vector inserts
            # fail at runtime ("collection configured without multiple named
            # vectors but received named vectors: map[ollama_embed:...]").
            # Vectors are still computed manually (Configure.NamedVectors.none).
            vectorizer_config=[
                Configure.NamedVectors.none(name="qwen3_embed"),     # active
                Configure.NamedVectors.none(name="ollama_embed"),    # legacy
                Configure.NamedVectors.none(name="openai_embed"),    # optional
            ],
            # `index_null_state=True` enables `is_none(True)` filters on date
            # properties (notably `valid_until`). Required for the MCP
            # `_stale_filter` to filter out expired/archived nodes at query
            # time. CANNOT be added later via Reconfigure — must be set at
            # create time. (Weaviate 1.28; verified 2026-04-30 against the
            # python client v4.)
            inverted_index_config=Configure.inverted_index(index_null_state=True),
        )

        print(f"✓ Created collection '{COLLECTION_NAME}' (named vectors + index_null_state=True)")
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


def ensure_dev_collection_exists(server: WeaviateMCPServer) -> bool:
    """Create the development docs collection if missing.

    Schema is a **subset** of the KG schema:
      - same chunker (Chunker / TokenCounter from weaviate_mcp.chunking)
      - same three named-vector slots (qwen3_embed, ollama_embed, openai_embed)
      - same `index_null_state=True` so post-sync stale-filtering works
        (currently no docs use `valid_until`, but cheap insurance for the
        future)
      - drops KG-specific fields: tags, links, typed_links, external_links,
        node_type, status (docs don't have those)
      - keeps: title, content, file_path, created_at, updated_at, chunk_num,
        total_chunks, source_node_id

    Returns True if the collection exists or was created.
    """
    if not DEV_COLLECTION_NAME:
        print("ℹ️  DEVELOPMENT_COLLECTION env not set — skipping dev collection")
        return False
    try:
        from weaviate.classes.config import Configure, Property, DataType

        if server.client.collections.exists(DEV_COLLECTION_NAME):
            print(f"✓ Dev collection '{DEV_COLLECTION_NAME}' exists")
            return True

        print(f"Creating dev collection '{DEV_COLLECTION_NAME}'...")
        server.client.collections.create(
            name=DEV_COLLECTION_NAME,
            description="Project development documentation (docs/) — chunked, "
                        "schema-paired with KG, auto-bound when project is "
                        "given KG access via the launcher.",
            properties=[
                Property(name="title", data_type=DataType.TEXT),
                Property(name="content", data_type=DataType.TEXT),
                Property(name="file_path", data_type=DataType.TEXT),
                # Legacy filesystem timestamps (kept for back-compat; older
                # docs were ingested with these names).
                Property(name="created_at", data_type=DataType.DATE),
                Property(name="updated_at", data_type=DataType.DATE),
                # Canonical temporal metadata — mirrors KG schema +
                # vco_lib.project_init.development_class_definition.
                # Required so the MCP `_stale_filter` (valid_until is_none
                # OR > now) doesn't fail with "no such prop" on Dev
                # collections. PR-24 (2026-05-16).
                Property(name="created", data_type=DataType.DATE),
                Property(name="updated", data_type=DataType.DATE),
                Property(name="valid_from", data_type=DataType.DATE),
                Property(name="valid_until", data_type=DataType.DATE),
                # Chunking support
                Property(name="chunk_num", data_type=DataType.INT),
                Property(name="total_chunks", data_type=DataType.INT),
                Property(name="source_node_id", data_type=DataType.TEXT),
            ],
            vectorizer_config=[
                Configure.NamedVectors.none(name="qwen3_embed"),
                Configure.NamedVectors.none(name="ollama_embed"),
                Configure.NamedVectors.none(name="openai_embed"),
            ],
            inverted_index_config=Configure.inverted_index(index_null_state=True),
        )
        print(f"✓ Created dev collection '{DEV_COLLECTION_NAME}' "
              f"(named vectors + index_null_state=True)")
        return True
    except Exception as e:
        print(f"❌ Error ensuring dev collection: {e}")
        return False


def _doc_title_from_file(file_path: Path, content: str) -> str:
    """Pick a title for a doc file (no frontmatter assumed).

    Order: first H1 heading; first H2 heading; filename stem (humanized).
    """
    for line in content.splitlines()[:50]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in content.splitlines()[:50]:
        s = line.strip()
        if s.startswith("## "):
            return s[3:].strip()
    return file_path.stem.replace("-", " ").replace("_", " ").strip().title()


def parse_doc_file(content: str, file_path: Path) -> Dict:
    """Parse a docs/ file. Returns the same shape as `parse_markdown_node`
    but with KG-specific fields (tags, links, etc.) absent or empty.

    Docs lack frontmatter. We synthesize:
      - title: first H1, falling back to H2, falling back to filename stem
      - created_at / updated_at: filesystem stat, since git history is more
        expensive to compute and the chunker doesn't need exact provenance
    """
    title = _doc_title_from_file(file_path, content)
    try:
        st = file_path.stat()
        # Both timestamps from filesystem; we don't have richer provenance
        # without git. Good enough: the index respects updated_at for
        # `days=N` recency filters.
        created_at = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
        updated_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except OSError:
        now = datetime.now(timezone.utc)
        created_at = now
        updated_at = now
    rel_path = ""
    try:
        rel_path = str(file_path.relative_to(PROJECT_ROOT))
    except ValueError:
        rel_path = str(file_path)
    return {
        "title": title,
        "content": content,
        "file_path": rel_path,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        # Empty KG-specific fields — kept for symmetry with sync_node's
        # data_obj structure but never written into the dev collection
        # (its schema doesn't have them).
        "tags": [],
        "links": [],
        "typed_links": [],
        "external_links": "",
        "node_type": "doc",
    }


def sync_doc(server: WeaviateMCPServer, file_path: Path) -> bool:
    """Sync a single docs/ file to the development collection.

    Mirrors `sync_node` minus the KG-specific concerns (no frontmatter
    parsing, no WikiLink resolution, no tag-from-typed-links inference, no
    cross-references). Same chunker, same active-vector-slot logic.
    """
    if not DEV_COLLECTION_NAME:
        print(f"⊘ DEVELOPMENT_COLLECTION not set — skipping {file_path}")
        return True

    try:
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            return False

        # Same archive-skip logic as KG (path contains 'archive/' segment).
        archived, reason = _is_archived_node(file_path)
        if archived:
            print(f"⊘ Skipping archived doc: {reason}")
            try:
                title = _doc_title_from_file(
                    file_path, file_path.read_text(encoding="utf-8")
                )
                _delete_doc_by_title(server, title)
            except Exception as e:
                print(f"  ↳ Could not remove prior dev entry: {e}")
            return True

        content = file_path.read_text(encoding="utf-8")
        doc_data = parse_doc_file(content, file_path)

        print(f"🔄 Syncing doc: {doc_data['title']}")

        coll = server.client.collections.get(DEV_COLLECTION_NAME)

        # Delete old version (by file_path)
        existing = coll.query.fetch_objects(
            filters=Filter.by_property("file_path").equal(doc_data["file_path"]),
            limit=100,
        )
        for obj in existing.objects:
            coll.data.delete_by_id(obj.uuid)

        token_count = TokenCounter.count_tokens(content)
        target_vec_name = _active_named_vector_for_kg()
        source_id = str(uuid.uuid4())

        if token_count <= MAX_EMBEDDING_TOKENS:
            embedding = server._get_embedding(content)
            data_obj = {
                "title": doc_data["title"],
                "content": doc_data["content"],
                "file_path": doc_data["file_path"],
                "created_at": doc_data["created_at"],
                "updated_at": doc_data["updated_at"],
                "chunk_num": 1,
                "total_chunks": 1,
                "source_node_id": source_id,
            }
            vec_arg = {target_vec_name: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            coll.data.insert(properties=data_obj, vector=vec_arg)
            print(f"   ✓ Stored doc as single chunk (vector={target_vec_name})")
            return True

        # Chunked path — mirrors `sync_node` chunked branch.
        chunker = Chunker(
            min_tokens=1500,
            max_tokens=MAX_EMBEDDING_TOKENS,
            target_tokens=2500,
        )
        chunks = chunker.chunk_text(
            text=content,
            source_id=source_id,
            metadata={
                "title": doc_data["title"],
                "file_path": doc_data["file_path"],
            },
        )
        print(f"   Split into {len(chunks)} chunks")
        for i, chunk in enumerate(chunks):
            embedding = server._get_embedding(chunk.content)
            data_obj = {
                "title": doc_data["title"],
                "content": chunk.content,
                "file_path": doc_data["file_path"],
                "created_at": doc_data["created_at"],
                "updated_at": doc_data["updated_at"],
                "chunk_num": i + 1,
                "total_chunks": len(chunks),
                "source_node_id": source_id,
            }
            vec_arg = {target_vec_name: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            coll.data.insert(properties=data_obj, vector=vec_arg)
        print(f"   ✓ Stored {len(chunks)} chunks (vector={target_vec_name})")
        return True
    except Exception as e:
        import traceback
        print(f"❌ Error syncing doc: {e}")
        traceback.print_exc()
        return False


def _delete_doc_by_title(server: WeaviateMCPServer, title: str) -> int:
    """Mirror of _delete_node_by_title for the dev collection."""
    if not DEV_COLLECTION_NAME:
        return 0
    try:
        coll = server.client.collections.get(DEV_COLLECTION_NAME)
        existing = coll.query.fetch_objects(
            filters=Filter.by_property("title").equal(title), limit=100
        )
        n = 0
        for obj in existing.objects:
            coll.data.delete_by_id(obj.uuid)
            n += 1
        return n
    except Exception:
        return 0


def sync_all_docs(server: WeaviateMCPServer) -> Tuple[int, int]:
    """Walk DOCS_ROOT and sync every .md to the dev collection."""
    if not DEV_COLLECTION_NAME:
        print("ℹ️  DEVELOPMENT_COLLECTION not set — skipping dev sync")
        return (0, 0)
    if not DOCS_ROOT.exists():
        print(f"ℹ️  No docs/ at {DOCS_ROOT} — skipping")
        return (0, 0)
    md_files = list(DOCS_ROOT.rglob("*.md"))
    print(f"📚 Found {len(md_files)} markdown files in docs/")
    success = fail = 0
    for md in sorted(md_files):
        if sync_doc(server, md):
            success += 1
        else:
            fail += 1
    return success, fail


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


def _frontmatter_title(file_path: Path) -> str | None:
    """Cheap title extractor for archived files we don't want to fully parse.

    Reads only the YAML frontmatter `title:` line. Returns None on any error.
    Used to look up Weaviate entries to delete when a previously-synced node
    is archived (moved to `archive/` or marked `status: archived`).
    """
    try:
        with file_path.open("r", encoding="utf-8") as f:
            head = f.read(2048)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    try:
        # Stop at the closing '---' of the frontmatter block
        end = head.find("\n---", 4)
        if end < 0:
            return None
        block = head[4:end]
        for line in block.splitlines():
            if line.startswith("title:"):
                return line.split(":", 1)[1].strip().strip('"\'')
    except Exception:
        return None
    return None


def _delete_node_by_title(server: WeaviateMCPServer, title: str) -> int:
    """Remove all Weaviate entries (incl. chunks) matching the given title.

    Used when an archived node should disappear from search. Returns the
    number of objects deleted. Silent (returns 0) if the collection is
    missing or the connection is down — sync should not block on cleanup.
    """
    try:
        coll = server.client.collections.get(COLLECTION_NAME)
        existing = coll.query.fetch_objects(
            filters=Filter.by_property("title").equal(title), limit=100
        )
        n = 0
        for obj in existing.objects:
            coll.data.delete_by_id(obj.uuid)
            n += 1
        return n
    except Exception:
        return 0


def _is_archived_node(file_path: Path, frontmatter: dict | None = None) -> tuple[bool, str]:
    """Decide whether a node should be excluded from Weaviate sync.

    Returns (is_archived, reason). A node is archived if either:
      - its filesystem path contains an `archive/` segment (knowledge/archive/...
        or any docs subtree), OR
      - its frontmatter `status` is `"archived"` or `"deprecated"`.

    Archived nodes are kept on disk (so future-anyone can grep / read history)
    but skipped on Weaviate sync — they shouldn't return from KG queries.
    The `_stale_filter()` at query time provides a second layer (in case an
    archived node slips through with a real `valid_until` in the past), but
    upstream skipping is the cleaner default: it keeps the index lean and
    avoids paying embedding cost for content that won't surface.
    """
    parts = file_path.parts
    if "archive" in parts:
        return True, f"path contains 'archive/' segment ({file_path})"
    if frontmatter is not None:
        status = (frontmatter.get("status") or "").strip().lower()
        if status in ("archived", "deprecated"):
            return True, f"frontmatter status={status!r}"
    return False, ""


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

        # Skip archived nodes — see _is_archived_node docstring. Do this
        # BEFORE the timestamp-update side effect so editing an archived
        # node doesn't bump its `updated:` field for no reason.
        archived, reason = _is_archived_node(file_path)
        if archived:
            print(f"⊘ Skipping archived node: {reason}")
            # If it was previously synced (archived after sync), drop it
            # from Weaviate so stale content stops surfacing.
            try:
                title = _frontmatter_title(file_path)
                if title:
                    _delete_node_by_title(server, title)
                    print(f"  ↳ Removed prior Weaviate entry for '{title}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior Weaviate entry: {e}")
            return True  # not a sync failure — intentional skip

        # Read, auto-update `updated:` timestamp, write back, then parse
        content = file_path.read_text(encoding='utf-8')
        content = _update_frontmatter_timestamp(file_path, content)
        node_data = parse_markdown_node(content, file_path)

        # Defence in depth: in case the path-based check missed a frontmatter-only
        # archive marker (e.g. status: archived but path doesn't contain 'archive/'),
        # check again after parsing. Same skip + delete behaviour.
        archived2, reason2 = _is_archived_node(file_path, frontmatter=node_data)
        if archived2:
            print(f"⊘ Skipping (frontmatter): {reason2}")
            try:
                title = node_data.get("title") or _frontmatter_title(file_path)
                if title:
                    _delete_node_by_title(server, title)
                    print(f"  ↳ Removed prior Weaviate entry for '{title}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior Weaviate entry: {e}")
            return True

        # Validate against vocabulary (report warnings, don't block sync)
        validation_warnings = validate_node_against_vocabulary(node_data, file_path)
        if validation_warnings:
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

            # Insert into the named vector matching the active embedding
            # model. The collection schema may also carry slots for other
            # models the user might switch to (legacy arctic, openai, etc.) —
            # those stay empty until/unless the user runs a re-embed pass.
            # NEVER cross-write a vector under a name that implies a different
            # model: e.g. don't put qwen3 vectors in `ollama_embed`. They
            # have different output spaces and a future search against
            # `ollama_embed` would treat them as if produced by arctic.
            #
            # Note for Weaviate 1.31+: `Reconfigure.NamedVectors.add()` lets
            # us add new named vectors after creation without recreating the
            # collection. Until that lands here, the schema-creation block
            # in `ensure_collection_exists` lists every slot we expect to
            # use, and switching models means dropping + re-ingesting (or
            # running the migrate_embeddings MCP tool).
            target_vec_name = _active_named_vector_for_kg()
            if DUAL_EMBEDDING_ENABLED:
                vector_arg = {target_vec_name: embedding}
            else:
                vector_arg = embedding
            obj_uuid = collection.data.insert(
                properties=data_obj,
                vector=vector_arg
            )

            chunks_created = 1
            print(f"   ✓ Stored node with UUID: {str(obj_uuid)[:8]}... (vector={target_vec_name})")

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

                # Insert into only the named vector slot matching the active
                # embedding model. See single-chunk path comment for why we
                # don't cross-write.
                target_vec_name = _active_named_vector_for_kg()
                if DUAL_EMBEDDING_ENABLED:
                    vector_arg = {target_vec_name: embedding}
                else:
                    vector_arg = embedding
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
    """Main entry point.

    Routes by path:
      - file under knowledge/  → sync_node (KG collection)
      - file under docs/       → sync_doc (development collection)
      - --all                  → sync_all_nodes + sync_all_docs
      - --all-docs             → sync_all_docs only (dev collection bootstrap)
    """
    if len(sys.argv) < 2:
        print("Usage: sync_knowledge_graph.py <file_path>")
        print("       sync_knowledge_graph.py --all       (knowledge/ + docs/)")
        print("       sync_knowledge_graph.py --all-docs  (docs/ only)")
        sys.exit(1)

    try:
        # Initialize server
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            ollama_url=OLLAMA_URL,
            embedding_model=EMBEDDING_MODEL,
            grpc_port=GRPC_PORT
        )

        # Ensure both collections exist (dev one only if env var set)
        if not ensure_collection_exists(server):
            print("❌ Cannot proceed without KG collection")
            sys.exit(1)
        ensure_dev_collection_exists(server)  # graceful no-op if env unset

        print()

        # Sync files
        if sys.argv[1] == "--all":
            kg_success, kg_fail = sync_all_nodes(server)
            doc_success, doc_fail = sync_all_docs(server)
            total_success = kg_success + doc_success
            total_fail = kg_fail + doc_fail
            print(f"📊 KG:   {kg_success} succeeded, {kg_fail} failed")
            print(f"📊 Docs: {doc_success} succeeded, {doc_fail} failed")
            sys.exit(0 if total_fail == 0 else 1)
        elif sys.argv[1] == "--all-docs":
            doc_success, doc_fail = sync_all_docs(server)
            print(f"📊 Docs: {doc_success} succeeded, {doc_fail} failed")
            sys.exit(0 if doc_fail == 0 else 1)
        else:
            file_path = Path(sys.argv[1]).resolve()

            # Route by path
            try:
                file_path.relative_to(KNOWLEDGE_ROOT)
                in_knowledge = True
            except ValueError:
                in_knowledge = False
            try:
                file_path.relative_to(DOCS_ROOT)
                in_docs = True
            except ValueError:
                in_docs = False

            if in_knowledge:
                success = sync_node(server, file_path)
            elif in_docs:
                success = sync_doc(server, file_path)
            else:
                print(f"ℹ️  File not in knowledge/ or docs/ directory, skipping")
                sys.exit(0)
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
