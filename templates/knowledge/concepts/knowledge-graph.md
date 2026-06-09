---
title: Knowledge Graph
type: concept
tags: [AI, knowledge-graph, graph-database, entities, relationships, ontology, semantic-web, high-level-plan]
created: 2026-03-29T00:00:00Z
updated: 2026-05-16T18:39:18Z
status: active
---

# Knowledge Graph

## Overview

A knowledge graph is a structured representation of real-world entities and the relationships between them, stored as a graph of nodes (entities) and edges (relationships). Unlike flat document stores, knowledge graphs encode **semantic meaning** in the structure itself — enabling traversal, inference, and multi-hop reasoning over interconnected facts.

First popularized by Google's Knowledge Graph (2012) for search enrichment, knowledge graphs now underpin enterprise AI, RAG systems, and multi-agent coordination.

## Core Components

### Entities (Nodes)

Real-world objects, concepts, or abstractions:
- People, organizations, technologies, concepts
- Each entity has properties (attributes) and a type
- Uniquely identified (URI, UUID, or title)

### Relationships (Edges)

Typed, directed connections between entities:
- `Redis --[USES]--> Caching` (directed, typed)
- Relationships carry semantics: "uses" vs "implements" vs "extends"
- Can have properties (weight, confidence, temporal metadata)

### Schema / Ontology

Defines allowed entity types, relationship types, and constraints:
- **Property graph model**: Nodes and edges have key-value properties (Neo4j, Weaviate)
- **RDF triple model**: Subject-predicate-object triples (SPARQL endpoints)
- **Hybrid**: Weaviate combines vector embeddings with property graph cross-references

## Knowledge Graph vs Other Data Structures

| Structure | Strengths | Weaknesses |
|---|---|---|
| **Relational DB** | ACID, mature tooling | Rigid schema, expensive joins at depth |
| **Document store** | Flexible schema, fast reads | No native relationships |
| **Vector DB** | Semantic similarity | No explicit relationships |
| **Knowledge graph** | Relationships are first-class, multi-hop queries | Schema design complexity, ingestion cost |

## Representations

### Property Graphs

Nodes and edges carry arbitrary key-value properties. Used by Neo4j, Amazon Neptune, Weaviate cross-references.

```
(:Technology {name: "Redis", type: "cache"})
  -[:USES {since: "2024"}]->
(:Pattern {name: "Session Caching"})
```

### RDF (Resource Description Framework)

Everything expressed as subject-predicate-object triples. Standard for the semantic web.

```turtle
<Redis> <rdf:type> <Technology> .
<Redis> <uses> <SessionCaching> .
```

### WikiLink Graphs (This Project)

Typed WikiLinks in markdown files, indexed into Weaviate:
```markdown
[[uses::Weaviate]]
[[implements::GraphRAG Pattern]]
[[relatedTo::Semantic Search]]
```

Lightweight, human-readable, version-controlled in git.

## Construction Methods

### Manual Curation
- Domain experts define entities and relationships
- High quality but expensive and slow
- Used by this project's markdown-based KG

### LLM-Powered Extraction
- LLM reads text and extracts structured entities/relationships
- Scalable but requires quality control
- Used by GraphRAG (Microsoft), Graphiti, and similar systems

### Hybrid
- LLM extraction + human validation
- Best quality-cost tradeoff at scale

## Role in RAG Systems

Knowledge graphs enhance RAG by providing **structured context** beyond flat chunk retrieval:

1. **Graph traversal** replaces or augments vector similarity for multi-hop questions
2. **Community detection** enables corpus-level summarization (GraphRAG global search)
3. **Typed relationships** allow relationship-specific queries ("What tools does X use?")
4. **Explainability** — answers traceable to specific entities and paths

See [[GraphRAG Pattern]] for detailed implementation.

## This Project's Implementation

- **Storage**: Weaviate at `localhost:8081` (default). Per-project collection: `<ProjectBasename>_KnowledgeGraph`. Shared cross-project collection: `VibecodedOrchestrator_KnowledgeGraph` (renamed from `VibeCodedTools_KnowledgeGraph` in v0.2.12 PR-26; legacy alias kept for ~3 releases).
- **Nodes**: Markdown files with YAML frontmatter in `knowledge/` directories
- **Relationships**: Typed WikiLinks (`[[relatedTo::Node]]`, `[[uses::Tool]]`)
- **Traversal**: `semantic_graph_search` MCP tool (BFS up to depth 3)
- **Search**: `hybrid_search` combines semantic + keyword across per-project KG + shared `VibecodedOrchestrator_KnowledgeGraph` + project docs
- **Default embeddings**: `qwen3-embedding:0.6b` via Ollama (1024-dim). Legacy named vectors (`snowflake-arctic-embed2`, `ollama_embed`) are preserved in existing collections for backward compatibility but `qwen3_embed` is the active search vector.

## Graph Databases

| Database | Model | Strengths |
|---|---|---|
| **Neo4j** | Property graph | Cypher queries, mature, fast traversal via index-free adjacency |
| **Weaviate** | Vector + cross-refs | Hybrid search, native embeddings, 1-2 hop traversal |
| **Amazon Neptune** | Property graph + RDF | Managed, multi-model |
| **FalkorDB** | Property graph | Redis-based, fast |
| **Kuzu** | Property graph | Embedded, lightweight |

## Related Links

[[relatedTo::GraphRAG Pattern]]
[[relatedTo::Graph Traversal]]
[[relatedTo::Semantic Search]]
[[relatedTo::RAG Pipeline]]
[[relatedTo::Weaviate]]
[[relatedTo::Graphiti Evaluation - Temporal Knowledge Graphs for AI Agents]]
