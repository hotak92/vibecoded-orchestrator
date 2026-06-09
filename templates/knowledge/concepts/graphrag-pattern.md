---
title: GraphRAG Pattern
type: concept
tags: [AI, RAG, knowledge-graph, retrieval, NLP, Microsoft, graph-traversal, mid-level-architecture]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:26Z
status: active
---

# GraphRAG Pattern

## Overview

GraphRAG (Graph Retrieval-Augmented Generation) is an approach introduced by Microsoft Research (Edge et al., 2024) that augments standard RAG with graph-structured knowledge to answer complex, global questions that span an entire document corpus — not just locally similar passages.

Standard RAG retrieves semantically similar chunks but fails at questions requiring synthesis across many sources ("What are the main themes in this corpus?"). GraphRAG builds an LLM-generated knowledge graph over the corpus, enabling community-level summarization and multi-hop reasoning.

## How It Works

### Indexing Phase

1. **Entity and Relationship Extraction** — LLM reads document chunks and extracts:
   - Named entities (people, organizations, concepts)
   - Relationships between entities (typed edges)
   - Claims associated with entities

2. **Community Detection** — Leiden algorithm (improvement over Louvain) clusters entities into communities; adds a refinement phase guaranteeing well-connected clusters. Produces hierarchical multi-level communities (coarse-to-fine)

3. **Community Summaries** — LLM generates summaries for each community at multiple resolution levels (coarse-to-fine hierarchy)

4. **Report Generation** — hierarchical reports stored in vector index and graph store

### Query Phase

**Local Search** — for specific entity questions:
- Retrieve entity neighborhood from graph
- Combine with vector-similar text chunks
- Generate focused answer

**Global Search** — for holistic questions about the corpus:
- Generate answers for each community summary in parallel (map)
- Reduce parallel answers into final response (reduce)
- Enables answers that synthesize the entire corpus

## Comparison with Standard RAG

| Dimension | Standard RAG | GraphRAG |
|---|---|---|
| Retrieval unit | Text chunks | Graph communities + chunks |
| Global questions | Poor | Strong |
| Local questions | Good | Good |
| Build cost | Low | High (LLM entity extraction) |
| Latency | Low | Higher |
| Explainability | Limited | High (traceable to entities) |

## Implementation (Microsoft GraphRAG)

```bash
pip install graphrag
graphrag init --root ./ragtest
# Edit settings.yaml with LLM and embedding config
graphrag index --root ./ragtest
graphrag query --root ./ragtest --method global "What are the themes?"
```

## Integration with Weaviate

GraphRAG is complementary to vector search in Weaviate:
- Weaviate stores both text chunks AND graph community summaries
- Graph traversal via cross-references (WikiLink-style)
- Hybrid retrieval: vector similarity + graph neighborhood
- The Weaviate MCP `semantic_graph_search` tool uses this pattern

## Variants and Extensions

- **Local GraphRAG** — focuses on specific entity neighborhoods
- **DRIFT Search** — progressive community traversal with hypothesis generation
- **HopRAG** (2025) — passage graphs with text chunks as vertices and LLM-generated pseudo-queries as edges; retrieve-reason-prune mechanism for logic-aware multi-hop retrieval
- **Agentic RAG** — LLM-based agents with multi-tool architectures for iterative graph exploration; handles complex multi-hop reasoning at higher latency/cost
- **Graphiti** — temporal knowledge graph for streaming data
- **LightRAG** — lightweight alternative with simpler graph construction

## Known Limitations

- High indexing cost: requires many LLM calls for entity extraction
- Graph quality depends on extraction model quality
- Best for static corpora; incremental update is expensive
- Overkill for small document sets (<100 docs)

## Related Links

[[relatedTo::GraphRAG Validation]]
[[relatedTo::Knowledge Graph]]
[[relatedTo::Semantic Search]]
[[relatedTo::RAG Pipeline]]
[[relatedTo::Weaviate]]
[[relatedTo::Graphiti Evaluation - Temporal Knowledge Graphs for AI Agents]]
[[relatedTo::Graph Traversal]]
