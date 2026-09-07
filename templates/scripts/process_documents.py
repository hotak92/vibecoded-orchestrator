#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Document Processing Script for Claude Orchestrator

Processes documents (markdown, PDF, etc.) from documents/ directory:
1. Chunks document content (800-2000 tokens)
2. Stores chunks in Weaviate DocumentChunks collection
3. Creates/updates knowledge graph node with document summary
4. Links document node to relevant existing nodes
5. Maintains bidirectional links

Usage:
    python .claude/scripts/process_documents.py <file_path>
    python .claude/scripts/process_documents.py --all  # Process all documents
"""

import sys
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# VCO-SHARED-BEGIN: _resolve_orchestrator_root (verbatim across templates/scripts/*.py)
def _resolve_orchestrator_root() -> "Path | None":
    """Return the orchestrator clone root — the directory that CONTAINS
    ``claude_mcp_servers/`` — or ``None`` when it cannot be located.

    THE one shape for this question across ``templates/scripts/*.py``. It is
    copied VERBATIM into every shipped script that asks it and pinned
    byte-identical by
    ``tests/test_v0292_cli_root_resolution_and_prefix.py::test_root_resolver_bodies_identical``
    — a DOCUMENTED class-C mirror with an enforcing test rather than a silent
    copy, because these scripts must answer "where is the orchestrator?"
    BEFORE they can import anything from it (importing ``vco_lib`` to find
    ``vco_lib`` is circular).

    Candidate order — the first candidate that actually CONTAINS
    ``claude_mcp_servers/`` wins; a candidate that does not is SKIPPED, never
    returned:

      1. ``$VCT_ORCHESTRATOR_ROOT`` — canonical; written into ``.claude/env``
         and ``.claude/settings.json`` by the bundle installer
         (``vco_lib/config_projection.py``).
      2. ``$VCT_INSTALL_ROOT`` — legacy alias carrying the same value; some
         launcher subprocess spawns set only this one.
      3. ``<script>/../..`` — the in-tree layout, correct ONLY when the script
         sits in the orchestrator clone's own ``.claude/scripts/`` (or in
         ``templates/scripts/`` in the clone). On an INSTALLED project this
         resolves to the USER project root, which has no
         ``claude_mcp_servers/`` — which is exactly why every rung is
         validated and why this rung is LAST.

    Never raises. Path joins go through ``pathlib`` so no separator is
    assumed (a Windows ``\\``-separator bug shipped once already, v0.2.81).
    """
    for _candidate in (
        os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip(),
        os.environ.get("VCT_INSTALL_ROOT", "").strip(),
        str(Path(__file__).resolve().parent.parent.parent),
    ):
        if not _candidate:
            continue
        try:
            _root = Path(_candidate)
            if (_root / "claude_mcp_servers").is_dir():
                return _root
        except (OSError, ValueError):
            continue
    return None
# VCO-SHARED-END: _resolve_orchestrator_root


# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# v0.2.92 (R4/R21) — INSTALL-TIME BAKED ROOT. `vco_lib/rewire.py` substitutes
# the placeholder below when this file is installed into a project, so the
# installed script can reach its orchestrator clone with NOTHING in the
# environment (rung 3, `<script>/../..`, resolves to the USER project root on
# an install and is correctly rejected there). In the clone the placeholder
# stays literal, `Path("{{ORCHESTRATOR_ROOT}}")/"vco_lib"` is not a directory,
# and this block is inert — the validation IS the placeholder guard, so no
# separate "was it substituted?" test can drift from it.
# It is used ONLY when NEITHER env pin ($VCT_ORCHESTRATOR_ROOT,
# $VCT_INSTALL_ROOT) names a real orchestrator root — a VALID pin always
# wins, and a PROVABLY stale one (the clone moved, .claude/env still names
# the old path) is healed rather than left to fall through to the user
# project root. Same stale-pin discipline as the v0.2.91 hub-token retry.
# Writing it into os.environ rather than a local is deliberate — child
# processes this script spawns inherit the same answer.
_VCO_BAKED_ORCHESTRATOR_ROOT = "{{ORCHESTRATOR_ROOT}}"
_vco_env_pins = [os.environ.get(_k, "").strip()
                 for _k in ("VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT")]
if (Path(_VCO_BAKED_ORCHESTRATOR_ROOT) / "vco_lib").is_dir() and not any(
    _p and (Path(_p) / "vco_lib").is_dir() for _p in _vco_env_pins
):
    os.environ["VCT_ORCHESTRATOR_ROOT"] = _VCO_BAKED_ORCHESTRATOR_ROOT

# Add paths.
#
# PR-2 portability (2026-05-06): claude_mcp_servers/ ONLY exists in the
# orchestrator clone, never bundled to user projects. Resolution order:
#   1. $VCT_ORCHESTRATOR_ROOT   (canonical, set by .claude/env)  \
#   2. $VCT_INSTALL_ROOT        (legacy alias, same value)        > shared
#   3. <script>/../..           (orchestrator clone in-tree)     /  resolver
#   4. $CLAUDE_PROJECT_ROOT/claude_mcp_servers   (legacy override, below)
# Rungs 1-3 are `_resolve_orchestrator_root()` above — the ONE shape across
# templates/scripts/*.py; each is VALIDATED (must contain claude_mcp_servers/)
# so a stale env value is skipped rather than returned.
# The MCP module is imported as a pure utility (chunking + collection
# bootstrap) — no service runtime needed. See PR-2 for the design notes.
#
# PROJECT_ROOT answers a DIFFERENT question — it is the USER PROJECT root and
# is what DOCUMENTS_ROOT / KNOWLEDGE_ROOT / `resolve(...)` /
# `EmbeddingService.for_project(...)` below are keyed on. Before v0.2.92 the
# same name served both questions; on the orchestrator clone the two answers
# coincide, which is why the conflation was invisible.
PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent.parent)))


def _resolve_mcp_servers_dir() -> Path:
    """Return the Path to claude_mcp_servers/, or raise with a hint."""
    root = _resolve_orchestrator_root()
    if root is not None:
        return root / "claude_mcp_servers"
    # Legacy explicit override, RETAINED (v0.2.92): $CLAUDE_PROJECT_ROOT names
    # a tree whose claude_mcp_servers/ should be used. Nothing in VCO writes
    # this key today — only this script and maintain_knowledge_graph.py READ it
    # — so it is kept because removing a documented user-facing override is a
    # behaviour change, and it is LAST because the two canonical env keys and
    # the in-tree layout are all checked (and validated) above. It is only
    # reachable at all when it points somewhere none of those three do.
    candidate = PROJECT_ROOT / "claude_mcp_servers"
    if candidate.is_dir():
        return candidate
    raise RuntimeError(
        "claude_mcp_servers/ not found. Set VCT_ORCHESTRATOR_ROOT in your "
        "shell or .claude/env to point at the orchestrator clone."
    )


_MCP_DIR = _resolve_mcp_servers_dir()
# v0.2.81 FN-1: DO NOT insert `_MCP_DIR/"weaviate_mcp"` on sys.path.
# Doing so made `chunking` importable as a TOP-LEVEL module (`from
# chunking import ...`) while this script's `sync_knowledge_graph` import
# pulls the SAME file as the `weaviate_mcp.chunking` PACKAGE submodule —
# two distinct module objects for one source file (the dual-module-object
# hazard: `weaviate_mcp.chunking is not chunking`). weaviate_mcp is
# pip-installed as an editable package by install.py (A1, v0.2.38), so the
# package import below resolves without any weaviate_mcp/ dir on sys.path;
# adding `_MCP_DIR` (the PARENT, claude_mcp_servers/) is only needed for
# no-pip environments and is identity-safe because it makes the PACKAGE
# resolvable, not the submodule as a top-level name.
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))
# v0.2.18: make vco_lib (EmbeddingService) importable.
_VCO_LIB_PARENT = _MCP_DIR.parent
if str(_VCO_LIB_PARENT) not in sys.path:
    sys.path.insert(0, str(_VCO_LIB_PARENT))
# Expose templates/scripts/ so the local sync_knowledge_graph wrapper is
# importable (same as maintain_knowledge_graph.py).
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# VCO-REWIRE-END: orchestrator-root-resolution

# v0.2.18: pre-v0.2.18 imported `WeaviateMCPServer` from
# `claude_mcp_servers/weaviate_mcp/server.py` — that symbol never existed,
# so this script was broken at import-time. Switch to the WeaviateWrapper
# defined in `sync_knowledge_graph` + the central EmbeddingService that
# owns embed/slot decisions.
from sync_knowledge_graph import WeaviateWrapper as WeaviateMCPServer
from vco_lib.embedding_service import (
    EmbeddingService,
    NoEmbeddingBackendError,
)
from weaviate_mcp.chunking import chunk_text, TokenCounter
from weaviate.classes.query import Filter
from weaviate.classes.config import Configure, Property, DataType

# Configuration
# v0.2.18: EMBEDDING_MODEL no longer read directly here. The
# EmbeddingService resolves both the active text model and its slot
# from env. Documents go to the DocumentChunks collection (flat single
# vector, no named-vector slots), so this script always writes a flat
# vector regardless of DUAL_EMBEDDING_ENABLED — the DocumentChunks
# schema uses `Configure.Vectorizer.none()` (see
# `ensure_document_chunks_collection`).
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))

DOCUMENTS_ROOT = PROJECT_ROOT / "documents"
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"

# Collections
DOCUMENT_CHUNKS_COLLECTION = "DocumentChunks"


# v0.2.21 Step 18 (caller migration): resolve KG collection via the
# launcher's vct-hub. Falls back to env (PR-7 / v0.2.11 behaviour) when
# the hub is unreachable. Pre-v0.2.11 this was hardcoded
# "ClaudeKnowledgeGraph", which routed writes from every install into
# the legacy collection regardless of the active project.
def _resolve_kg_collection() -> str:
    try:
        from vco_lib.project_config import resolve  # type: ignore[import-not-found]
        cfg = resolve(PROJECT_ROOT)
        return cfg.kg_collection or os.getenv("KG_COLLECTION", "KnowledgeGraph")
    except Exception:
        return os.getenv("KG_COLLECTION", "KnowledgeGraph")


KNOWLEDGE_GRAPH_COLLECTION = _resolve_kg_collection()


def ensure_document_chunks_collection(server: WeaviateMCPServer) -> bool:
    """Ensure DocumentChunks collection exists"""
    try:
        if server.client.collections.exists(DOCUMENT_CHUNKS_COLLECTION):
            print(f"✓ Collection '{DOCUMENT_CHUNKS_COLLECTION}' exists")
            return True

        print(f"Creating collection '{DOCUMENT_CHUNKS_COLLECTION}'...")

        server.client.collections.create(
            name=DOCUMENT_CHUNKS_COLLECTION,
            description="Document chunks for semantic search",
            properties=[
                Property(name="content", data_type=DataType.TEXT),
                Property(name="chunk_number", data_type=DataType.INT),
                Property(name="total_chunks", data_type=DataType.INT),
                Property(name="token_count", data_type=DataType.INT),
                Property(name="source_id", data_type=DataType.TEXT),
                Property(name="source_title", data_type=DataType.TEXT),
                Property(name="source_path", data_type=DataType.TEXT),
                Property(name="document_type", data_type=DataType.TEXT),  # paper/reference/guide
                Property(name="created_at", data_type=DataType.DATE),
            ],
            vectorizer_config=Configure.Vectorizer.none()
        )

        print(f"✓ Created collection '{DOCUMENT_CHUNKS_COLLECTION}'")
        return True

    except Exception as e:
        print(f"❌ Error ensuring collection: {e}")
        return False


def process_markdown(file_path: Path) -> Tuple[str, str]:
    """
    Process markdown file

    Returns:
        (title, content)
    """
    content = file_path.read_text(encoding='utf-8')

    # Extract title from first # heading
    title = file_path.stem
    for line in content.split('\n'):
        if line.startswith('# '):
            title = line[2:].strip()
            break

    return title, content


def process_pdf(file_path: Path) -> Tuple[str, str]:
    """
    Process PDF file using docling

    Returns:
        (title, content as markdown)
    """
    try:
        from docling.document_converter import DocumentConverter

        print(f"  Parsing PDF with docling...")
        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        content = result.document.export_to_markdown()

        # Use filename as title
        title = file_path.stem.replace('_', ' ').replace('-', ' ')

        return title, content

    except ImportError:
        print(f"  ⚠️  docling not installed, cannot process PDF")
        return None, None
    except Exception as e:
        print(f"  ❌ Error processing PDF: {e}")
        return None, None


def chunk_document(
    content: str,
    source_id: str,
    source_title: str,
    source_path: str,
    document_type: str
) -> List[Dict]:
    """Chunk document and prepare for Weaviate storage"""

    # Use MCP chunking utility
    chunks = chunk_text(
        text=content,
        source_id=source_id,
        metadata={
            "source_title": source_title,
            "source_path": source_path,
            "document_type": document_type
        },
        min_tokens=800,
        max_tokens=2000
    )

    # Convert to Weaviate format
    weaviate_chunks = []
    for chunk in chunks:
        weaviate_chunks.append({
            "content": chunk.content,
            "chunk_number": chunk.chunk_number,
            "total_chunks": chunk.total_chunks,
            "token_count": chunk.token_count,
            "source_id": source_id,
            "source_title": source_title,
            "source_path": source_path,
            "document_type": document_type,
            "created_at": chunk.created_at
        })

    return weaviate_chunks


def store_document_chunks(
    server: WeaviateMCPServer,
    chunks: List[Dict],
    source_id: str
) -> bool:
    """Store document chunks in Weaviate, replacing old versions.

    v0.2.46 V46-D: cursor-paginates the delete-then-replace scan so
    large multi-chunk documents (> 1000 chunks) no longer leave stale
    chunks behind after re-ingest.
    """
    try:
        collection = server.client.collections.get(DOCUMENT_CHUNKS_COLLECTION)

        # Delete old chunks for this document — enumerate ALL of them.
        source_filter = Filter.by_property("source_id").equal(source_id)
        deleted_count = 0
        PAGE_SIZE = 1000
        cursor = None
        while True:
            if cursor is not None:
                page = collection.query.fetch_objects(
                    filters=source_filter, limit=PAGE_SIZE, after=cursor
                )
            else:
                page = collection.query.fetch_objects(
                    filters=source_filter, limit=PAGE_SIZE
                )
            if not page.objects:
                break
            for obj in page.objects:
                collection.data.delete_by_id(obj.uuid)
                deleted_count += 1
            # If we got a partial page, no more results.
            if len(page.objects) < PAGE_SIZE:
                break
            # We just deleted the previous page from the same collection,
            # so subsequent fetches naturally re-page from the top —
            # using cursor=None keeps things correct because deleted rows
            # disappear from the result set entirely.
            cursor = None

        if deleted_count > 0:
            print(f"  🗑️  Deleted {deleted_count} old chunks")

        # Store new chunks
        for chunk_data in chunks:
            # Get embedding
            embedding = server._get_embedding(chunk_data["content"])

            # Insert
            collection.data.insert(
                properties=chunk_data,
                vector=embedding
            )

        print(f"  ✓ Stored {len(chunks)} new chunks")
        return True

    except Exception as e:
        print(f"  ❌ Error storing chunks: {e}")
        return False


def generate_document_summary(server: WeaviateMCPServer, title: str, content: str) -> str:
    """
    Generate a concise summary using Ollama

    Returns summary (2-3 sentences)
    """
    try:
        import requests

        # Use first 3000 characters for summary
        sample = content[:3000]

        prompt = f"""Summarize this document in 2-3 clear sentences. Focus on the main topic and key insights.

Document Title: {title}

Content:
{sample}

Summary:"""

        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": "qwen3.5:9b",
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 150
                }
            },
            timeout=30
        )

        if response.status_code == 200:
            summary = response.json()["response"].strip()
            return summary
        else:
            print(f"  ⚠️  Failed to generate summary, using default")
            return f"Document about {title}"

    except Exception as e:
        print(f"  ⚠️  Summary generation failed: {e}")
        return f"Document about {title}"


def find_relevant_nodes(
    server: WeaviateMCPServer,
    title: str,
    content: str,
    limit: int = 5
) -> List[str]:
    """
    Find relevant knowledge nodes using semantic search

    Returns list of node titles
    """
    try:
        collection = server.client.collections.get(KNOWLEDGE_GRAPH_COLLECTION)

        # Search using document summary
        search_text = f"{title}\n\n{content[:1000]}"

        results = collection.query.near_text(
            query=search_text,
            limit=limit
        )

        relevant_titles = []
        for obj in results.objects:
            node_title = obj.properties.get("title")
            if node_title and node_title != title:
                relevant_titles.append(node_title)

        return relevant_titles

    except Exception as e:
        print(f"  ⚠️  Error finding relevant nodes: {e}")
        return []


def create_knowledge_node(
    title: str,
    summary: str,
    document_path: str,
    document_type: str,
    relevant_nodes: List[str],
    chunk_count: int
) -> Path:
    """
    Create or update knowledge node for document

    Returns path to created node
    """
    # Determine target directory
    if document_type == "paper":
        node_dir = KNOWLEDGE_ROOT / "research"
    elif document_type == "reference":
        node_dir = KNOWLEDGE_ROOT / "concepts"  # References usually are concept docs
    else:  # guide
        node_dir = KNOWLEDGE_ROOT / "tools"

    # Create filename from title
    filename = re.sub(r'[^\w\s-]', '', title.lower())
    filename = re.sub(r'[-\s]+', '-', filename)
    node_path = node_dir / f"{filename}.md"

    # Generate node content
    links_section = ""
    if relevant_nodes:
        links_section = "\n## Related Knowledge\n" + "\n".join([f"- [[{node}]]" for node in relevant_nodes])

    node_content = f"""# {title}

#document #{document_type} #research #imported

{summary}

## Source Document
- **Path**: `{document_path}`
- **Chunks**: {chunk_count} stored in Weaviate
- **Collection**: `DocumentChunks`
- **Type**: {document_type.title()}

## Access
Search Weaviate `DocumentChunks` collection with:
- `source_title` = "{title}"
- `document_type` = "{document_type}"
{links_section}

## Notes
[Add your notes and insights here]

Last updated: {datetime.now().strftime('%Y-%m-%d')}
"""

    # Write node
    node_path.write_text(node_content, encoding='utf-8')
    print(f"  ✓ Created knowledge node: {node_path.relative_to(PROJECT_ROOT)}")

    return node_path


def update_linked_nodes(relevant_titles: List[str], new_node_title: str):
    """
    Add backlink to relevant nodes

    Updates existing nodes to link back to the new document node
    """
    for title in relevant_titles:
        # Find node file
        for node_file in KNOWLEDGE_ROOT.rglob("*.md"):
            content = node_file.read_text(encoding='utf-8')

            # Check if this is the target node
            first_heading = None
            for line in content.split('\n'):
                if line.startswith('# '):
                    first_heading = line[2:].strip()
                    break

            if first_heading == title:
                # Check if link already exists
                link_text = f"[[{new_node_title}]]"
                if link_text not in content:
                    # Add link in Links or Related section
                    if "## Links" in content:
                        content = content.replace(
                            "## Links",
                            f"## Links\n- [[{new_node_title}]] - Related document"
                        )
                    elif "## Related" in content:
                        content = content.replace(
                            "## Related",
                            f"## Related\n- [[{new_node_title}]] - Related document"
                        )
                    else:
                        # Add new section before last line
                        lines = content.split('\n')
                        last_updated_idx = -1
                        for i, line in enumerate(lines):
                            if line.startswith("Last updated:"):
                                last_updated_idx = i
                                break

                        if last_updated_idx > 0:
                            lines.insert(last_updated_idx, f"\n## Related Documents\n- [[{new_node_title}]]\n")
                            content = '\n'.join(lines)

                    node_file.write_text(content, encoding='utf-8')
                    print(f"  ✓ Added backlink to {title}")
                break


def process_document(server: WeaviateMCPServer, file_path: Path) -> bool:
    """Process a single document file"""
    try:
        print(f"\n{'='*60}")
        print(f"Processing: {file_path.name}")
        print(f"{'='*60}")

        # Determine document type from directory
        rel_path = file_path.relative_to(DOCUMENTS_ROOT)
        document_type = str(rel_path.parts[0]) if len(rel_path.parts) > 1 else "guide"

        # Process based on file type
        if file_path.suffix == ".md":
            title, content = process_markdown(file_path)
        elif file_path.suffix == ".pdf":
            title, content = process_pdf(file_path)
            if title is None:
                return False
        else:
            print(f"  ⚠️  Unsupported file type: {file_path.suffix}")
            return False

        print(f"  Title: {title}")
        print(f"  Type: {document_type}")
        print(f"  Size: {len(content)} characters")

        # Generate source ID
        source_id = f"{document_type}_{file_path.stem}"
        source_path = str(file_path.relative_to(PROJECT_ROOT))

        # Chunk document
        print(f"  Chunking document...")
        chunks = chunk_document(content, source_id, title, source_path, document_type)
        print(f"  ✓ Created {len(chunks)} chunks")

        # Store chunks in Weaviate
        if not store_document_chunks(server, chunks, source_id):
            return False

        # Generate summary
        print(f"  Generating summary...")
        summary = generate_document_summary(server, title, content)
        print(f"  ✓ Summary: {summary[:80]}...")

        # Find relevant nodes
        print(f"  Finding relevant knowledge nodes...")
        relevant_nodes = find_relevant_nodes(server, title, content)
        if relevant_nodes:
            print(f"  ✓ Found {len(relevant_nodes)} relevant nodes: {', '.join(relevant_nodes[:3])}")
        else:
            print(f"  ℹ️  No relevant nodes found")

        # Create knowledge node
        print(f"  Creating knowledge node...")
        node_path = create_knowledge_node(
            title, summary, source_path, document_type, relevant_nodes, len(chunks)
        )

        # Update linked nodes with backlinks
        if relevant_nodes:
            print(f"  Updating linked nodes with backlinks...")
            update_linked_nodes(relevant_nodes, title)

        print(f"\n✅ Successfully processed {title}")
        return True

    except Exception as e:
        print(f"\n❌ Error processing document: {e}")
        import traceback
        traceback.print_exc()
        return False


def process_all_documents(server: WeaviateMCPServer) -> Tuple[int, int]:
    """Process all documents in documents/ directory"""
    success_count = 0
    fail_count = 0

    # Find all documents
    doc_files = list(DOCUMENTS_ROOT.rglob("*.md")) + list(DOCUMENTS_ROOT.rglob("*.pdf"))

    print(f"\n{'='*60}")
    print(f"Found {len(doc_files)} documents to process")
    print(f"{'='*60}")

    for doc_file in sorted(doc_files):
        if process_document(server, doc_file):
            success_count += 1
        else:
            fail_count += 1

    return success_count, fail_count


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: process_documents.py <file_path>")
        print("       process_documents.py --all")
        sys.exit(1)

    embedding_service = None
    server = None
    try:
        # v0.2.18: construct EmbeddingService at script entry. Soft-fail
        # on no-backend (write deferral + exit 0) mirrors the
        # sync_knowledge_graph.py seed path.
        try:
            embedding_service = EmbeddingService.for_project(PROJECT_ROOT)
        except NoEmbeddingBackendError as e:
            print(f"⚠️  Document processing skipped: {e}", file=sys.stderr)
            # The path is RESOLVED (v0.2.92): the literal that used to be here
            # named ~/.claude/metrics, which W7 turned into a read-only archive.
            from vco_lib.embedding_fidelity import failures_jsonl_display_path
            print("   See .claude/context/EMBEDDING_FAILURES.md + "
                  + failures_jsonl_display_path(), file=sys.stderr)
            sys.exit(0)

        # Initialize Weaviate client + bind to the embedding service
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            embedding_service=embedding_service,
            grpc_port=GRPC_PORT
        )

        # Ensure collections exist
        if not ensure_document_chunks_collection(server):
            print("❌ Cannot proceed without DocumentChunks collection")
            sys.exit(1)

        print()

        # Process documents
        if sys.argv[1] == "--all":
            success, fail = process_all_documents(server)
            print(f"\n{'='*60}")
            print(f"📊 Results: {success} succeeded, {fail} failed")
            print(f"{'='*60}")
            sys.exit(0 if fail == 0 else 1)
        else:
            file_path = Path(sys.argv[1])

            # Check if file is in documents/ directory
            try:
                file_path.relative_to(DOCUMENTS_ROOT)
            except ValueError:
                print(f"ℹ️  File not in documents/ directory, skipping")
                sys.exit(0)

            # Process single file
            success = process_document(server, file_path)
            sys.exit(0 if success else 1)

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
