---
name: ai-rag-advisor
description: Expert guidance on RAG (Retrieval-Augmented Generation) system design including chunking strategies, embedding selection, retrieval methods, and vector database choices
short_desc: RAG design: chunking, retrieval, vector DB choice
keywords: [RAG, chunking, vector database, retrieval, reranker, GraphRAG, "embedding model", "vector search", "embedding search", "RAG pipeline", Weaviate, Pinecone, Qdrant, "hybrid retrieval", "semantic search"]
model: sonnet
---

# AI RAG Advisor (Sonnet)

**Purpose**: Expert guidance on RAG (Retrieval-Augmented Generation) system design including chunking strategies, embedding selection, retrieval methods, and vector database choices.

**Model**: Sonnet 4.5 (balanced reasoning for RAG architecture, up-to-date on 2026 techniques)

## This Workflow's RAG Implementation (Reference)

When providing RAG guidance, you can reference this workflow's working implementation:

**Architecture**: GraphRAG (semantic search + graph traversal)
- Combines vector similarity with graph relationships
- Typed WikiLinks create navigable knowledge graph: `[[uses::Tool]]`, `[[implements::Concept]]`
- Enables discovering related nodes beyond semantic similarity

**Vector Database**: Weaviate
- Production-ready, scalable
- Collections: `ClaudeKnowledgeGraph`, `[Project]_development`
- Auto-sync via hooks when source files change

**Embeddings**: snowflake-arctic-embed2
- Provider: Ollama (local, free, no API costs)
- Dimensions: 1024
- Quality: High performance on MTEB benchmarks
- Latency: ~500ms for semantic queries

**Chunking Strategy**:
- Knowledge nodes: Size-limited at source (<300/<200/<150 lines by abstraction level)
- Large nodes: Auto-chunked at 2500 tokens during sync
- Maintains context by keeping nodes focused and using WikiLinks for relationships

**Search Methods**:
- Keyword: `.claude/scripts/kg-search` (~100ms) for exact terms
- Semantic: Weaviate MCP `search_knowledge_graph()` (~500ms) for concepts
- Graph: Weaviate MCP `semantic_graph_search()` (~1-2s) for relationships
- Hybrid: Weaviate MCP `hybrid_search()` (~1-2s) combines all three

**Storage Pattern**:
- Source: Markdown files with YAML frontmatter (Obsidian-style)
- Truth: Files in knowledge/ and docs/ directories
- Sync: To Weaviate via scripts/hooks
- Benefit: Human-readable, git-friendly, LLM-friendly

**Collections Strategy**:
- `ClaudeKnowledgeGraph`: Concise cross-project patterns (<300 lines/node)
- `[Project]_development`: Verbose project-specific docs (no size limit)
**Performance**:
- Keyword search: ~100ms (file-based)
- Semantic search: ~500ms (Weaviate)
- Graph traversal: ~1-2s (Weaviate with WikiLink following)
- Coverage: 31 nodes, growing organically

This is a working implementation that balances performance, cost (free local embeddings), and quality. Use it as reference when advising on RAG systems.

## What This Skill Provides

### 1. Chunking Strategy Recommendations

Provides guidance on chunking methods:
- **Fixed-Size**: 512-1024 tokens, 50-100 token overlap (homogeneous documents)
- **Semantic**: Chunk at natural boundaries (structured documents)
- **Hierarchical**: Multi-level chunking (large documents with nesting)
- **Recursive**: Function/class boundaries (code repositories)
- **Sliding Window**: 50% overlap (maximum recall)
- **Conversational**: By conversation turns or topics (chat histories)

### 2. Embedding Model Selection

Recommendations across categories:
- **General Purpose**: snowflake-arctic-embed2, nomic-embed-text-v1.5
- **Code-Specific**: jina-embeddings-code
- **Multilingual**: mxbai-embed-large, multilingual-e5-large
- **Long Context**: gte-Qwen2 (32K tokens)
- **Highest Quality**: gte-Qwen2-7B-instruct (MTEB 69.8)

### 3. Retrieval Method Recommendations

Analyzes retrieval approaches:
- **Semantic Search**: Vector similarity only (clear conceptual queries)
- **Hybrid Search**: Vector + keyword (70/30 typical weighting)
- **Graph-Based**: Vector + graph traversal (interconnected content)
- **Re-Ranking**: Cross-encoder second pass (10-20% accuracy improvement)
- **Multi-Query**: Generate query variants, merge results

### 4. Vector Database Selection

Compares databases:
- **Weaviate**: GraphRAG, hybrid search, production (recommended)
- **Pinecone**: Fully managed, zero ops, auto-scaling
- **Chroma**: Prototyping, simple, embedded
- **FAISS**: High-performance local, offline
- **Qdrant**: Open-source alternative to Weaviate

### 5. RAG Architecture Patterns

Outlines patterns:
- **Simple RAG**: Query → Retrieve → Generate
- **Iterative RAG**: Multi-turn retrieval refinement
- **Agentic RAG**: LLM plans retrieval strategy
- **GraphRAG**: Semantic search + graph traversal

### 6. Optimization Techniques

Performance improvements:
- Metadata filtering (pre-filter before vector search)
- Chunk size tuning (precision vs context tradeoff)
- Top-K selection (5-10 for generation, 20-50 for re-ranking)
- Context window optimization (use 50-70% for chunks)
- Caching (embeddings, frequent queries)
- Query enhancement (expansion, rewriting, multi-query)

## Output Format

See [template.md](template.md) for complete RAG system design structure.

## Integration with Knowledge Graph

After RAG design:
1. Document strategy in `knowledge/concepts/rag-[use-case]-strategy.md`
2. Link to embedding model node
3. Link to vector database node
4. Tag with domain and techniques used

## Supporting Files

- **Template**: Use [template.md](template.md) for complete RAG system design
