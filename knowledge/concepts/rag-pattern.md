---
title: RAG Pattern
type: concept
tags: [AI, RAG, retrieval, LLM, pattern, knowledge-base, embedding]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:48Z
status: active
---

# RAG Pattern

## Overview

The RAG (Retrieval-Augmented Generation) Pattern is a software architecture pattern for building LLM applications that need to answer questions about domain-specific, private, or frequently updated knowledge. It decouples the knowledge base from the model, making knowledge updates cheap (no retraining required).

This node documents the pattern as an architectural concept. For full pipeline implementation details, see `RAG Pipeline`.

## Pattern Definition

**Intent**: Ground LLM responses in retrieved documents rather than relying on parametric knowledge (weights).

**Motivation**:
- LLMs have static knowledge cutoffs
- Fine-tuning is expensive and doesn't work well for exact fact retrieval
- Retrieval provides attribution and reduces hallucination

**Applicability**: Use when:
- Knowledge changes frequently (news, documentation)
- Answers must be traceable to source documents
- Domain is specialized and not well-covered in training data
- Factual precision matters more than creativity

## Pattern Structure

```
┌─────────────────────────────────────────────────────┐
│ RAG System                                          │
│                                                     │
│  ┌──────────┐   embed   ┌─────────────┐            │
│  │ Document │ ────────► │ Vector Store │            │
│  │   Store  │           │  (Weaviate)  │            │
│  └──────────┘           └──────┬──────┘            │
│                                │                    │
│  ┌──────────┐   embed   ┌──────▼──────┐            │
│  │  Query   │ ────────► │  Retriever  │            │
│  └──────────┘           └──────┬──────┘            │
│                                │ top-k docs         │
│                         ┌──────▼──────┐            │
│                         │   Prompt    │            │
│                         │  Builder   │            │
│                         └──────┬──────┘            │
│                                │                    │
│                         ┌──────▼──────┐            │
│                         │     LLM     │            │
│                         └──────┬──────┘            │
│                                │ response           │
└────────────────────────────────┼────────────────────┘
                                 ▼
                           Final Answer
```

## Pattern Variants

### Standard RAG
- Single retrieval step before generation
- Simple, predictable, low latency
- Fails on multi-hop questions

### Iterative RAG
- Multiple retrieval rounds during generation
- Agent interleaves retrieval with reasoning (ReAct pattern)
- Better for complex multi-step questions

### Fusion RAG
- Query is expanded into multiple sub-queries
- Results from each are fused before generation
- Reduces retrieval bias from single query phrasing

### Corrective RAG (CRAG)
- Evaluates retrieval quality before generation
- Falls back to web search if retrieved docs are irrelevant
- More robust but adds latency

### Speculative RAG
- Parallel: one model speculates answer, another verifies with retrieval
- Faster than sequential retrieval-then-generate

## Implementation Tips

1. **Chunk overlap** — 10–20% overlap prevents losing information at boundaries
2. **Metadata filtering** — filter by date, source, type before semantic search
3. **Re-ranking** — cross-encoder re-ranker improves precision (see: `Reranking Models for RTX 4080 Super 16GB`)
4. **Context length** — retrieved context + query must fit in LLM's context window
5. **Citation tracking** — include source metadata in context so LLM can cite
6. **Caching** — cache embeddings; recalculate only when documents change

## Anti-Patterns

- **Too many chunks** — context overflow, LLM loses track of key information
- **Too few chunks** — missing relevant context, incomplete answers
- **Wrong chunk size** — very small chunks lose context; very large lose precision
- **Stale index** — documents updated but embeddings not refreshed
- **Single embedding model** — using different models for indexing vs. query

## Links

[[relatedTo::RAG Pipeline]]
[[relatedTo::Semantic Search]]
[[relatedTo::GraphRAG Pattern]]
[[relatedTo::Weaviate Usage Patterns]]
[[relatedTo::Embedding Token Filtering Three Layer Pattern]]
[[relatedTo::Reranking Models for RTX 4080 Super 16GB]]
[[relatedTo::Knowledge Graph]]
