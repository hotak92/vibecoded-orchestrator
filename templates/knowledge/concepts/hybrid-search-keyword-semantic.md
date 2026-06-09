---
title: Hybrid Search - Combining Keyword and Semantic Retrieval
type: concept
tags: [AI, search, hybrid-search, BM25, semantic-search, ranking, vector-database, mid-level-architecture]
created: 2026-02-27T00:00:00Z
updated: 2026-04-05T14:33:28Z
valid_from: 2026-02-27T00:00:00Z
valid_until: null
status: active
---

# Hybrid Search - Combining Keyword and Semantic Retrieval

## Definition

Hybrid search is a retrieval technique that combines **keyword search** (exact term matching) with **semantic search** (meaning-based similarity) into a single ranked result set. Results from both methods are scored, merged, and re-ranked to provide the most relevant documents.

**Why it matters**: Neither keyword nor semantic search alone is perfect:
- **Keyword-only**: Misses paraphrases, synonyms, and conceptual relationships
- **Semantic-only**: Can return irrelevant results lacking exact terminology
- **Hybrid**: Captures both precision (keywords) and understanding (semantics)

## How Hybrid Search Works

### Three-Stage Process

**Stage 1: Parallel Retrieval**
```
User Query
    ↓
    ├→ Keyword Search (BM25)  → Top 10 docs with exact terms
    └→ Semantic Search (Vector DB) → Top 10 docs with similar meaning
```

**Stage 2: Scoring & Normalization**
- BM25 scores on term frequency/rarity (0-100 range, variable)
- Cosine similarity scores on embedding vectors (0-1 range)
- Normalize both to comparable scales

**Stage 3: Fusion & Ranking**
- Merge results using Reciprocal Rank Fusion (RRF) or weighted combination
- Return unified top-K results to LLM

### Reciprocal Rank Fusion (RRF)

RRF combines results from multiple ranking systems by rewarding documents that rank highly in either list:

```
RRF Score = Σ (1 / (k + rank_i))
where k=60 is a constant, rank_i is document position in each ranking
```

**Example**:
```
Query: "memory leaks in Python"

Keyword Search Results:
1. "Python Memory Leak Detection" (exact match)
2. "Managing Memory in Python" (contains keywords)

Semantic Search Results:  
1. "Performance Optimization Tips" (conceptually related)
2. "Garbage Collection in Python" (semantically similar)

RRF Fusion:
1. "Python Memory Leak Detection" (high in keyword, medium in semantic)
2. "Garbage Collection in Python" (medium in keyword, high in semantic)
3. "Managing Memory in Python" (high in keyword, low in semantic)
4. "Performance Optimization Tips" (low in keyword, high in semantic)
```

## Keyword Search Component

### BM25 Algorithm

BM25 is the industry standard for keyword retrieval. See [[BM25 Ranking Algorithm]] for full details. Key properties:
- **Term frequency with saturation** (k1=1.2): diminishing returns from repetition
- **Inverse document frequency** (IDF): rare terms weighted more heavily
- **Document length normalization** (b=0.75): prevents long-document bias
- **Speed**: CPU-only, sub-100ms on 100k-200k documents

```
BM25(D, Q) = Σ IDF(qi) * (f(qi, D) * (k1 + 1)) /
             (f(qi, D) + k1 * (1 - b + b * |D| / avgdl))

Default parameters: k1=1.2, b=0.75 (Elasticsearch, Weaviate, Lucene)
```

**BM25 strengths in hybrid context**: exact names, abbreviations, error codes, IDs — where vector search struggles with vocabulary mismatch.

**BM25 limitations hybrid compensates**: "spaghetti" won't match "pasta"; no synonym bridging; no semantic understanding. Vector search fills these gaps.

## Semantic Search Component

### Vector Embeddings

Documents and queries are converted to numerical vectors (embeddings) representing semantic meaning. Similar documents have similar vectors.

**Popular Embedding Models**:
- **OpenAI text-embedding-3-small**: 1536 dimensions, $0.02/1M tokens (default choice)
- **text-embedding-3-large**: 3072 dimensions, $0.13/1M tokens (better quality)
- **Voyage AI**: 1024 dimensions, domain-specialized, $0.12/1M tokens
- **Sentence Transformers**: Free, open-source, suitable for local inference

### Similarity Metrics

```python
from sentence_transformers import SentenceTransformer
import numpy as np

model = SentenceTransformer("all-MiniLM-L6-v2")

doc_embedding = model.encode("Python garbage collection prevents memory leaks")
query_embedding = model.encode("memory leaks in Python")

# Cosine similarity
similarity = np.dot(doc_embedding, query_embedding) / (
    np.linalg.norm(doc_embedding) * np.linalg.norm(query_embedding)
)  # Returns 0.85 (highly similar)
```

## Hybrid Search Implementation Patterns

### Pattern 1: Weighted Combination
```python
def hybrid_search(query, alpha=0.6):
    # alpha controls semantic/keyword balance
    keyword_scores = bm25_search(query, limit=20)
    semantic_scores = vector_search(query, limit=20)
    
    # Normalize both to 0-1 range
    keyword_norm = {doc: score/max(keyword_scores.values()) 
                    for doc, score in keyword_scores.items()}
    semantic_norm = {doc: score/max(semantic_scores.values()) 
                     for doc, score in semantic_scores.items()}
    
    # Weighted combination (60% semantic, 40% keyword default)
    combined = {}
    for doc in set(keyword_norm) | set(semantic_norm):
        combined[doc] = (alpha * semantic_norm.get(doc, 0) + 
                        (1-alpha) * keyword_norm.get(doc, 0))
    
    return sorted(combined.items(), key=lambda x: x[1], reverse=True)
```

### Pattern 2: Reciprocal Rank Fusion (RRF)
```python
def rrf_fusion(keyword_results, semantic_results, k=60):
    rrf_scores = {}
    
    # Score based on rank in each result set
    for rank, (doc, _) in enumerate(keyword_results, 1):
        rrf_scores[doc] = rrf_scores.get(doc, 0) + 1/(k + rank)
    
    for rank, (doc, _) in enumerate(semantic_results, 1):
        rrf_scores[doc] = rrf_scores.get(doc, 0) + 1/(k + rank)
    
    return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
```

### Pattern 3: Sequential Filtering
```python
def cascading_search(query):
    # Stage 1: Fast keyword search to filter
    candidates = bm25_search(query, limit=50)
    
    # Stage 2: Re-rank top candidates with semantic search
    doc_texts = [fetch_doc(doc_id) for doc_id, _ in candidates[:20]]
    embeddings = model.encode(doc_texts)
    query_emb = model.encode(query)
    
    scores = cosine_similarity(query_emb, embeddings)
    
    # Return top-5 by semantic score
    return top_k(scores, k=5)
```

## Benefits & Tradeoffs

### Benefits
| Benefit | Impact |
|---------|--------|
| Improved accuracy | Up to 9% recall improvement vs. semantic-only |
| Handles synonyms & paraphrases | "car" matches "automobile" |
| Robust to query phrasing | Works with vague or specific queries |
| Domain-specific precision | Doesn't lose technical terminology |
| Better UX | Users get faster, more relevant results |

**Caveat**: Hybrid search does NOT always outperform pure vector search. Benchmarks show pure vector can achieve 84% category precision vs 78% for hybrid in some cases. Fusion can introduce noise when semantic search alone captures intent well. Always benchmark for your specific domain.

### Challenges & Solutions

| Challenge | Solution |
|-----------|----------|
| **Scoring imbalance** | Normalize to 0-1, test weighting (40%-60% split) |
| **Latency** (2 searches) | Cache frequent queries, use ANN indices, batch |
| **Implementation complexity** | Use platforms like Meilisearch, Elasticsearch |
| **Storage overhead** | Compress embeddings, remove duplicates |
| **Outdated embeddings** | Retrain monthly/quarterly on new vocabulary |

## Best Practices

### 1. Test Different Weights
```python
# Evaluate retrieval accuracy with various alpha values
for alpha in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
    accuracy = evaluate_retrieval(test_queries, alpha)
    print(f"Alpha={alpha}: {accuracy:.2%}")
# Pick the weight with best accuracy for your domain
```

### 2. Use Metadata Filtering
```python
# Combine hybrid search with metadata constraints
hybrid_results = hybrid_search(query)
filtered = [doc for doc in hybrid_results 
            if doc['date'] >= '2026-01-01' and doc['category'] == 'technical']
```

### 3. Implement Re-ranking
```python
# Coarse-to-fine for speed + quality
candidates = hybrid_search(query, limit=50)  # Fast
reranked = expensive_reranker(query, candidates[:20], limit=5)  # Accurate
```

### 4. Monitor & Evaluate
```python
# Track key metrics over time
metrics = {
    'precision@5': compute_precision(results[:5], expected),
    'recall@10': compute_recall(results[:10], expected),
    'mrr': mean_reciprocal_rank(results, expected),
    'latency_ms': time_retrieval()
}
```

## Hybrid Search vs Alternatives

| Approach | Precision | Recall | Speed | Use Case |
|----------|-----------|--------|-------|----------|
| **Keyword Only** | High | Low | Very fast | Known terminology |
| **Semantic Only** | Medium | High | Fast | Conceptual matching |
| **Hybrid (Balanced)** | High | High | Good | **Most general cases** |
| **Hybrid + Re-rank** | Highest | Highest | Slower | High-stakes retrieval |

## Implementation Checklist

- [ ] Choose embedding model (recommend text-embedding-3-small)
- [ ] Set up BM25 index (Elasticsearch, Meilisearch, or custom)
- [ ] Implement vector database (Weaviate, Pinecone, Qdrant)
- [ ] Test fusion strategy (RRF or weighted combination)
- [ ] Create evaluation dataset (50+ test queries with ground truth)
- [ ] Measure precision, recall, MRR on test set
- [ ] Fine-tune weight balance (keyword/semantic split)
- [ ] Add metadata filtering logic
- [ ] Implement caching for frequent queries
- [ ] Monitor latency (target: <500ms end-to-end)
- [ ] Set up monitoring dashboard
- [ ] Document weighting decisions for your domain

[[relatedTo::RAG Pipeline]]
[[relatedTo::Semantic Search and Text Embeddings]]
[[relatedTo::Semantic Search]]
[[relatedTo::BM25 Ranking Algorithm]]
[[relatedTo::Vector Database - Architecture and Applications]]
[[uses::Weaviate]]
