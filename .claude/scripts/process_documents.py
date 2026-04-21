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

# Add paths
PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent.parent)))
sys.path.insert(0, str(PROJECT_ROOT / "claude_mcp_servers/weaviate_mcp"))

from server import WeaviateMCPServer
from chunking import chunk_text, TokenCounter
from weaviate.classes.query import Filter
from weaviate.classes.config import Configure, Property, DataType

# Configuration
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))

DOCUMENTS_ROOT = PROJECT_ROOT / "documents"
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"

# Collections
DOCUMENT_CHUNKS_COLLECTION = "DocumentChunks"
KNOWLEDGE_GRAPH_COLLECTION = "ClaudeKnowledgeGraph"


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
    """Store document chunks in Weaviate, replacing old versions"""
    try:
        collection = server.client.collections.get(DOCUMENT_CHUNKS_COLLECTION)

        # Delete old chunks for this document
        existing = collection.query.fetch_objects(
            filters=Filter.by_property("source_id").equal(source_id),
            limit=1000
        )

        deleted_count = 0
        for obj in existing.objects:
            collection.data.delete_by_id(obj.uuid)
            deleted_count += 1

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
                "model": "qwen3:latest",
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

    try:
        # Initialize server
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            ollama_url=OLLAMA_URL,
            embedding_model=EMBEDDING_MODEL,
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
        try:
            server.close()
        except:
            pass


if __name__ == "__main__":
    main()
