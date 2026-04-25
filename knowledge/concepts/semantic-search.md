---
title: Semantic Search
type: concept
tags: [AI, search, embeddings, vector-database, retrieval, NLP, information-retrieval]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:52Z
status: active
---

# Semantic Search

## Overview

Semantic search is an information retrieval approach that finds documents based on **meaning** rather than exact keyword matches. Instead of checking if a query term appears in a document (lexical search), semantic search encodes both query and documents as dense vectors in a shared embedding space, then retrieves documents with the smallest vector distance to the query.

This enables retrieval of conceptually related content even when the query uses completely different words than the document.

## How It Works

### Embedding Phase
1. Each document (or chunk) is processed by an embedding model
2. The model produces a dense vector (e.g., 768 or 1024 dimensions)
3. Vectors are stored in a vector database with the original content

### Query Phase
1. User query is embedded using the same model (important: same model for both)
2. Nearest neighbors search finds documents with closest vector distance
3. Results ranked by similarity score (cosine similarity or dot product)

### Distance Metrics
- **Cosine similarity** — measures angle between vectors; robust to magnitude differences
- **Dot product** — faster; implicitly includes magnitude (good for asymmetric retrieval)
- **Euclidean distance** — less common; sensitive to vector magnitude

## Embedding Models

| Model | Dimensions | Strengths |
|---|---|---|
| snowflake-arctic-embed2 | 1024 | Strong retrieval, used in this project |
| text-embedding-3-large | 3072 | OpenAI, best general-purpose |
| BGE-M3 | 1024 | Multilingual, sparse+dense hybrid |
| Jina Embeddings v2 | 768 | Code-optimized variant |
| E5-large | 1024 | Strong for passage retrieval |
| nomic-embed-text | 768 | Open source, competitive quality |

## Hybrid Search

Pure semantic search can miss exact matches (names, codes, IDs). Hybrid search combines:
- **Dense retrieval** — semantic similarity (BM25-weighted vector search)
- **Sparse retrieval** — BM25 or TF-IDF keyword matching
- **Fusion** — Reciprocal Rank Fusion (RRF) or weighted combination

Weaviate, Pinecone, and Qdrant all support hybrid search natively.

## Implementation with Weaviate

```python
# Semantic-only search
results = client.query.get("Article", ["title", "content"]).with_near_text({
    "concepts": ["machine learning optimization"]
}).with_limit(10).do()

# Hybrid search (semantic + BM25)
results = client.query.get("Article", ["title", "content"]).with_hybrid(
    query="machine learning optimization",
    alpha=0.75  # 0=pure BM25, 1=pure semantic
).with_limit(10).do()
```

## Performance Considerations

### Approximate Nearest Neighbor (ANN)
- Exact search is O(n·d) — too slow for large datasets
- ANN algorithms (HNSW, IVF, LSH) trade accuracy for speed
- HNSW (Hierarchical Navigable Small World) is the current standard
- ~99% recall at 10–100× speedup vs. exact search

### Chunking Strategy
- Long documents must be split before embedding
- Optimal chunk size: 256–512 tokens for retrieval tasks
- Overlapping chunks (20–50 token overlap) prevents boundary loss
- Chunk size affects what "semantic unit" the model captures

## Relevance vs. Recall Trade-offs

| Strategy | Precision | Recall | Speed |
|---|---|---|---|
| Small chunks | Low | High | Fast |
| Large chunks | High | Low | Slow |
| Hybrid | Balanced | High | Medium |
| Re-ranking | Very High | Medium | Slow (2-pass) |

## Relation to RAG

Semantic search is the retrieval backbone of RAG (Retrieval-Augmented Generation):
1. Embed user question
2. Retrieve top-k relevant chunks via semantic search
3. Inject chunks into LLM prompt as context
4. LLM generates answer grounded in retrieved documents

## This Project's Usage

- **Collection**: `ClaudeKnowledgeGraph` in Weaviate at `http://localhost:8081`
- **Embedding model**: `snowflake-arctic-embed2:latest` (1024-dim) via Ollama
- **Tools**: `hybrid_search` (default), `semantic_graph_search`, `get_node_connections` (Weaviate MCP)
- **Code embeddings**: `jina-embeddings-v2-base-code` (768-dim)

## Embedding Model Selection

### Commercial APIs

| Model | Dimensions | Cost | Best For |
|---|---|---|---|
| text-embedding-3-small | 1536 | $0.02/1M | General purpose, default |
| text-embedding-3-large | 3072 | $0.13/1M | High-stakes retrieval |
| Voyage AI embedding-2 | 1024 | $0.12/1M | Production, domain-specific |
| Cohere embed-v3 | 1024 | $0.10/1M | E-commerce, code retrieval |

### Selection Criteria
1. Retrieval accuracy: Standard → text-embedding-3-small; High → text-embedding-3-large; Local → all-mpnet-base-v2
2. Cost: High volume → cheaper models; Offline → open-source (free)
3. Domain: General → OpenAI/Voyage; Code → Jina; Multilingual → multilingual-e5

## Fine-Tuning Embeddings

When generic embeddings don't capture your domain:

```python
from sentence_transformers import SentenceTransformer, InputExample, losses
from torch.utils.data import DataLoader

model = SentenceTransformer("all-mpnet-base-v2")
train_examples = [
    InputExample(texts=["patient shows symptoms", "clinical presentation"], label=0.9),
    InputExample(texts=["severe headache", "cancer diagnosis"], label=0.1),
]
train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=16)
train_loss = losses.CosineSimilarityLoss(model)
model.fit(train_objectives=[(train_dataloader, train_loss)], epochs=1)
```

## Evaluation Metrics

- **Precision@K**: % of top-K results that are relevant
- **Recall@K**: % of all relevant docs found in top-K
- **MRR (Mean Reciprocal Rank)**: Position of first relevant result
- **NDCG**: Ranking quality metric

## Production Checklist

- [ ] Choose embedding model for your domain and benchmark 2-3 options
- [ ] Implement chunking (256-512 tokens, 20-50 token overlap)
- [ ] Batch encode corpus (`model.encode(corpus, batch_size=32)`)
- [ ] Normalize embeddings for cosine similarity consistency
- [ ] Cache embeddings to avoid re-computation
- [ ] Set up monitoring for embedding drift
- [ ] Plan quarterly retraining schedule

## Related Links

[[relatedTo::Weaviate]]
[[relatedTo::Weaviate Usage Patterns]]
[[relatedTo::GraphRAG Pattern]]
[[relatedTo::RAG Pipeline]]
[[relatedTo::Knowledge Graph]]
[[relatedTo::Ollama MCP Server]]
[[relatedTo::Embedding Token Filtering Three Layer Pattern]]
