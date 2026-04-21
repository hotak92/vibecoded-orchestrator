# RAG System Design Template

## RAG System Design: [Use Case Name]

**Date**: [YYYY-MM-DD]
**Project**: [Project Name]
**Requester**: [Name]

---

## Requirements Analysis

### Use Case
**Primary Use Case**: [Specific application - e.g., "Technical documentation Q&A"]

**Document Types**:
- [X] Markdown documentation
- [X] PDFs (papers, manuals)
- [ ] Code repositories
- [ ] Conversations/chat logs
- [ ] Other: [specify]

**Query Types**:
- [X] Factual ("What is X?")
- [X] Conceptual ("How does X work?")
- [ ] Procedural ("How do I do X?")
- [ ] Comparison ("X vs Y?")

**Scale**:
- Number of documents: [X]
- Total size: [Y] GB
- Expected queries: [Z] per day
- Concurrent users: [W]

**Performance Targets**:
- Retrieval latency: < [X]ms
- End-to-end latency: < [Y]s
- Accuracy: > [Z]% relevant results in top-5

---

## Recommended Architecture

### Pattern: [Simple RAG / Iterative RAG / Agentic RAG / GraphRAG]

**Flow Diagram**:
```
User Query
    ↓
[Step 1]: Embed Query
    ↓
[Step 2]: Vector Search (top-k chunks)
    ↓
[Step 3]: [Optional: Re-ranking]
    ↓
[Step 4]: LLM Generation
    ↓
Answer
```

**Why This Pattern?**:
- [Reason 1 based on use case]
- [Reason 2 based on complexity]
- [Reason 3 based on performance needs]

**Tradeoffs Accepted**:
- [Tradeoff 1]: [Why acceptable]
- [Tradeoff 2]: [Why acceptable]

---

## Chunking Strategy

### Recommended Method: [Semantic / Fixed-Size / Hierarchical / Recursive]

**Configuration**:
- **Chunk size**: [512-1024] tokens
- **Overlap**: [50-100] tokens ([10-20]%)
- **Method**: [Sentence boundaries / Paragraphs / Section headers / Function boundaries]

**Why This Strategy?**:
- [Document structure consideration]
- [Query type alignment]
- [Performance tradeoff explanation]

**Implementation**:
```python
from langchain.text_splitter import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=1024,
    chunk_overlap=100,
    separators=["\n\n", "\n", ". ", " "],
    length_function=len
)

chunks = splitter.split_text(document)
```

---

## Embedding Model

### Recommended Model: [Model Name]

**Specifications**:
- **Parameters**: [X]M
- **Dimensions**: [1024/768/512]
- **Context length**: [8192] tokens
- **MTEB score**: [X.X]
- **VRAM**: ~[Y]GB
- **Domain**: [General / Code / Multilingual]

**Why This Model?**:
- [Domain fit reasoning]
- [Performance characteristics]
- [VRAM constraints consideration]

**Alternative**: [Fallback model] if [condition]

**Loading**:
```python
from ollama import embeddings

def embed(text: str) -> list[float]:
    response = embeddings(
        model='snowflake-arctic-embed2',
        prompt=text
    )
    return response['embedding']
```

---

## Retrieval Method

### Primary Method: [Semantic / Hybrid / GraphRAG]

**Configuration**:
- **Top-k**: [10] initial retrieval
- **Similarity metric**: Cosine similarity
- **Threshold**: > [0.7] relevance score

**If Hybrid**:
- Vector weight: [70]%
- Keyword weight: [30]%
- Keyword method: BM25

**Re-Ranking**: [Yes/No]
- Model: [bge-reranker-v2-m3]
- Top-k after re-rank: [5]

**Why This Method?**:
- [Query characteristics reasoning]
- [Corpus characteristics reasoning]
- [Accuracy requirements justification]

---

## Vector Database

### Recommended: [Weaviate / Pinecone / Chroma / FAISS / Qdrant]

**Reasons**:
- [Feature 1]: [Why needed]
- [Feature 2]: [Why needed]
- [Scale capability]: [Handles X vectors]
- [Cost]: [Self-hosted free / $X per month]
- [Operational complexity]: [Acceptable level]

**Schema Design**:
```python
{
    "class": "DocumentChunk",
    "properties": [
        {"name": "content", "dataType": ["text"]},
        {"name": "chunk_index", "dataType": ["int"]},
        {"name": "document_id", "dataType": ["string"]},
        {"name": "metadata", "dataType": ["object"]},
        {"name": "created_at", "dataType": ["date"]}
    ],
    "vectorizer": "none",  # Using custom embeddings
}
```

**Indexing Strategy**:
- Index type: HNSW
- Parameters: M=[16], efConstruction=[128]
- Distance metric: Cosine
- Sharding: [Strategy if >10M vectors]

---

## LLM Integration

### Model: [Qwen2.5-7B / Llama-3-8B / etc.]

**Prompt Template**:
```python
prompt = f"""You are a helpful assistant. Answer the question based on the context below.

Context:
{retrieved_chunks}

Question: {user_query}

Answer based only on the context. If the answer isn't in the context, say "I don't know based on the provided information."

Answer:"""
```

**Generation Parameters**:
- Temperature: [0.1] (factual) / [0.7] (creative)
- Max tokens: [512]
- Top-p: [0.9]

---

## Performance Optimization

### Caching Strategy
- **Query embedding cache**: [5] min TTL
- **Frequent query cache**: [15] min TTL
- **Expected hit rate**: [10-15]%

### Batch Processing
- Embed [10-50] chunks per batch
- Reduces API calls by [80]%

### Async Retrieval
- Parallel retrieval from multiple sources
- Latency reduction: [40-60]%

---

## Evaluation Metrics

### Retrieval Quality
- **Recall@5**: > [80]% (relevant doc in top-5)
- **Precision@5**: > [60]% (5 results mostly relevant)
- **MRR**: > [0.7] (Mean Reciprocal Rank)

### End-to-End Quality
- **Answer accuracy**: > [85]% (human eval)
- **Answer completeness**: > [80]%
- **Hallucination rate**: < [5]%

### Performance
- **Retrieval latency**: < [200]ms
- **End-to-end latency**: < [3]s
- **Throughput**: > [10] queries/sec

---

## Testing Strategy

### Create Test Set
1. [50-100] representative queries
2. Label expected top-5 chunks for each
3. Human-labeled correct answers

### Evaluation Loop
1. Run queries through RAG system
2. Compare retrieved chunks vs expected (Recall@5)
3. Compare generated answers vs labels (accuracy)
4. Iterate on chunking/retrieval/prompting

**Test Queries**:
```
1. [Example query 1]
   Expected chunks: [chunk IDs or titles]
   Expected answer: [reference answer]

2. [Example query 2]
   Expected chunks: [chunk IDs or titles]
   Expected answer: [reference answer]

[Add 5-10 test queries here]
```

---

## Cost Estimation

### Storage
- Documents: [X] docs × [Y] chunks/doc × [Z]KB/chunk = [Total]GB
- Vector DB storage: $[amount]/month or free (self-hosted)

### Compute
- Embedding: [X] queries/day × $[cost]/1K tokens (or free if self-hosted)
- LLM generation: [X] queries/day × $[cost]/1K tokens
- **Total monthly cost**: $[amount]

### Optimization Opportunities
- Caching reduces embedding cost by [10-15]%
- Self-hosted embeddings: FREE (if VRAM available)
- Batch processing reduces overhead by [20-30]%

---

## Implementation Roadmap

### Phase 1: Prototype (Week 1)
- [ ] Set up vector database
- [ ] Implement chunking pipeline
- [ ] Test embedding generation
- [ ] Simple retrieval + generation

### Phase 2: Optimization (Week 2)
- [ ] Tune chunk size (test 512/1024/2048)
- [ ] Test retrieval methods (semantic/hybrid)
- [ ] Add metadata filtering
- [ ] Implement caching layer

### Phase 3: Evaluation (Week 3)
- [ ] Create test set (50+ queries)
- [ ] Measure baseline metrics
- [ ] Iterate on improvements
- [ ] A/B test configurations

### Phase 4: Production (Week 4)
- [ ] Scale testing (load, stress tests)
- [ ] Monitoring and alerting setup
- [ ] Documentation (API docs, runbooks)
- [ ] Deploy to production

---

## Monitoring

### Metrics to Track
- Query latency (p50, p95, p99)
- Retrieval recall and precision
- Cache hit rate
- Error rate (4xx, 5xx)
- Cost per query

### Alerts
- Latency > [threshold]ms
- Error rate > [1]%
- Cache hit rate < [5]%
- Cost spike > [2]x baseline

### Dashboards
- Real-time query volume
- Latency trends
- Quality metrics (if feedback available)
- Cost breakdown (storage, compute)

---

## Common Issues & Solutions

### Problem: Poor retrieval quality (irrelevant results)
**Solutions**:
- Try hybrid search (semantic + keyword)
- Tune chunk size (test 512/1024/2048)
- Improve embedding model (domain-specific)
- Add metadata filtering

### Problem: High latency
**Solutions**:
- Reduce top-k (10 → 5)
- Enable caching (query + embedding)
- Use faster embedding model
- Optimize vector DB index

### Problem: Hallucinations (LLM invents facts)
**Solutions**:
- Lower temperature (0.7 → 0.1)
- Stricter prompt ("ONLY from context")
- Add citation requirement
- Use retrieval confidence scores

### Problem: Missing relevant info (low recall)
**Solutions**:
- Increase top-k (5 → 20)
- Try multi-query (generate variants)
- Check chunking strategy (may split concepts)
- Add query expansion

---

## Sign-Off

**Designed by**: [Name]
**Reviewed by**: [Name]
**Approved**: [Yes/No]
**Date**: [YYYY-MM-DD]

**Next Steps**:
1. [Action item 1]
2. [Action item 2]
3. [Action item 3]
