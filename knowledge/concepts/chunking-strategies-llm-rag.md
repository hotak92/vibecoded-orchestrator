---
title: Chunking Strategies for LLM and RAG Systems
type: concept
tags: [AI, RAG, chunking, document-processing, embeddings, text-splitting, NLP, mid-level-architecture]
created: 2026-02-27T00:00:00Z
updated: 2026-04-13T16:00:00Z
valid_from: 2026-02-27T00:00:00Z
valid_until: null
status: active
---

# Chunking Strategies for LLM and RAG Systems

## Overview

Chunking is the process of splitting documents into smaller, meaningful pieces before embedding and indexing them. The right chunking strategy directly impacts retrieval quality—poor chunking can create up to 9% gap in recall performance between best and worst approaches.

**The Chunking Tradeoff**:
- **Too small**: Lose context, more chunks to manage, noisier embeddings
- **Too large**: Reduce precision, exceed embedding model limits, slow retrieval
- **Just right**: Preserve semantic units, maintain context, optimize retrieval accuracy

## Chunking Strategies Comparison

### 1. Recursive Character Splitting

**Approach**: Split at natural language boundaries (paragraphs → sentences → words) until chunk size target is met.

**How it works**:
```
Separator hierarchy: ["\n\n", "\n", ". ", " ", ""]
- Try to split on paragraph breaks first
- If paragraph too large, split on sentence
- If sentence too large, split on space
- As last resort, split on character
```

**Configuration**:
```python
from langchain_text_splitters import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=512,           # Target 512 characters
    chunk_overlap=50,         # 10% overlap for context
    separators=["\n\n", "\n", ". ", " ", ""],
    length_function=len
)

chunks = splitter.split_text(document)
```

**Performance**: 88-89% recall with text-embedding-3-large (Chroma research)

**Pros**:
- Preserves document structure (headers, paragraphs)
- Adapts to content type via custom separators
- Reliable across document types
- Excellent default choice

**Cons**:
- Variable chunk sizes
- Requires understanding content structure
- Slightly more complex than fixed-size splitting

**Best For**: Articles, technical documentation, research papers, email threads

---

### 2. Semantic Chunking

**Approach**: Analyze meaning of consecutive sentences using embeddings, split where topic shifts (similarity drops sharply).

**Algorithm**:
```
1. Break document into sentences
2. Create embeddings for each sentence
3. Calculate similarity between consecutive sentences
4. When similarity drops below threshold → start new chunk
```

**Implementation**:
```python
from langchain_experimental.text_splitter import SemanticChunker
from langchain_openai import OpenAIEmbeddings

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

semantic_chunker = SemanticChunker(
    embeddings=embeddings,
    breakpoint_threshold_type="percentile",  # Split when top 95%
    breakpoint_threshold_amount=95
)

chunks = semantic_chunker.split_text(document)
```

**Threshold Types**:
- **Percentile**: Split when similarity difference exceeds 95th percentile
- **Standard Deviation**: Split when > 3 std devs from mean
- **Interquartile Range**: Uses middle 50% of scores (robust to outliers)

**Performance**: 2-3% better recall vs. recursive splitting, up to 9% better vs. size-based

**Pros**:
- Maintains semantic coherence
- Detects subtle topic transitions
- Highest quality chunks for dense docs
- Works on unstructured text without headers

**Cons**:
- Very expensive (embedding API calls per sentence)
- Requires threshold tuning per domain
- Slower processing
- NAACL 2025 research: gains don't always justify cost

**Cost Estimate**: 10,000-word document = 200-300 embeddings = $0.01-0.04

**Best For**: Dense research papers, long-form articles with subtle transitions, high-stakes retrieval

---

### 3. Page-Level Chunking

**Approach**: Treat each page of a document as a separate chunk. Respects document pagination.

**Implementation**:
```python
from unstructured.partition.pdf import partition_pdf
from unstructured.chunking.title import chunk_by_title

elements = partition_pdf(
    filename="report.pdf",
    strategy="hi_res"  # High-resolution for tables/images
)

chunks = chunk_by_title(
    elements,
    multipage_sections=False,  # Don't span pages
    combine_text_under_n_chars=200,
    max_characters=2000
)
```

**Performance**: **0.648 accuracy with lowest variance** (NVIDIA 2024 benchmarks)

**Pros**:
- Highest accuracy for paginated documents
- Preserves table/figure layout context
- Works with mixed content (text + tables + images)
- Pages often = logical units

**Cons**:
- Only for PDFs/presentations
- Variable chunk sizes
- Assumes pages align with semantic boundaries
- May create very small or very large chunks

**Best For**: Financial reports, legal documents, research papers, presentations

---

### 4. Sentence-Based Chunking

**Approach**: Identify sentences using NLP, group them to reach target chunk size.

**Implementation**:
```python
from llama_index.core.node_parser import SentenceSplitter

splitter = SentenceSplitter(
    chunk_size=1024,
    chunk_overlap=20
)

nodes = splitter.get_nodes_from_documents([document])
```

**Pros**:
- Maintains sentence integrity
- Natural language flow
- Variable chunk sizes
- Better for Q&A and conversational data

**Cons**:
- Long sentences create oversized chunks
- Sentence detection fails on malformed text
- More complex than size-based

**Best For**: Conversational data, Q&A datasets, customer support transcripts

---

### 5. Size-Based Chunking (Fixed)

**Approach**: Split every N characters/tokens. Simplest possible strategy.

**Variants**:

**Character-based**:
```python
from langchain_text_splitters import CharacterTextSplitter

splitter = CharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=100,
    separator="\n"
)
chunks = splitter.split_text(document)
```

**Token-based**:
```python
from langchain_text_splitters import TokenTextSplitter

splitter = TokenTextSplitter(
    chunk_size=512,
    chunk_overlap=50
)
chunks = splitter.split_text(document)
```

**Pros**:
- Fastest to implement (3 lines of code)
- Predictable chunk sizes
- No computational overhead
- Works with any content

**Cons**:
- Ignores document structure
- Fragments sentences mid-thought
- Lower recall than structure-aware methods
- Splits tables/code awkwardly

**Best For**: Prototyping, MVPs, simple homogeneous content

---

### 6. LLM-Based Chunking

**Approach**: Send document to LLM with instructions to identify optimal split points.

**Implementation**:
```python
from openai import OpenAI

client = OpenAI()

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{
        "role": "system",
        "content": "Identify logical section breaks where topics shift significantly."
    }, {
        "role": "user",
        "content": document[:8000]  # First portion
    }]
)

# Parse response for split points
```

**Pros**:
- Context-aware decisions
- Handles unusual document types
- Can generate summaries in same pass
- Adapts dynamically

**Cons**:
- Very expensive ($0.01-0.10 per document)
- Slow (LLM inference latency)
- Limited production use
- Requires prompt engineering

**Cost Example**: 100 documents × 5000 words = $3-30 total

**Best For**: High-value content, one-time batch processing, experimental projects

---

### 7. Late Chunking

**Approach**: Process full document through transformer first, then chunk embeddings. Preserves full context during embedding.

**Traditional vs Late Chunking**:
```
Traditional:
Doc → Chunk → Embed (context limited to chunk)

Late Chunking:
Doc → Embed Full (full context) → Extract Chunks from Embeddings
```

**Advantages**:
- Each chunk's embedding has full document context
- Pronouns/references preserved accurately
- Better retrieval for long-range dependencies

**Status**: Emerging technique (Jina AI 2024), less tested in production

**Best For**: Documents with important cross-chunk references

---

## Chunking Decision Framework

```
START
  ↓
What's your document type?
  ├→ Paginated (PDF, report) → Use page-level chunking
  ├→ Dense unstructured (paper, article) → Use semantic chunking
  ├→ Conversational (Q&A, chat) → Use sentence-based
  ├→ Simple homogeneous (blog posts) → Use recursive or size-based
  └→ Code files → Use function/class boundaries
  
How much time/budget?
  ├→ Prototyping → Size-based (fastest)
  ├→ Production, standard → Recursive (best default)
  ├→ High-stakes retrieval → Semantic + re-ranking
  └→ Valuable one-time → LLM-based
  
Document count?
  ├→ < 100 docs → Can afford semantic chunking
  ├→ 100-10K docs → Recursive splitting (balanced)
  └→ 10K+ docs → Fixed size (cost matters)
```

## Overlap Strategy

Industry best practice: **10-20% overlap**.

```python
# Example: 500-token chunks
chunk_size = 500
overlap = 50  # 10% overlap

# Why overlap matters:
# Without: "Yesterday was great. [CHUNK BREAK] Today is better."
# With overlap: Last sentence of chunk 1 + first sentence of chunk 2
#              = complete context preserved
```

**Trade-off**:
- More overlap: Better context, higher costs
- Less overlap: Cheaper, context loss
- January 2026 research: Overlap sometimes provides no benefit, test your data

## Evaluation Metrics

### Chunk Quality Indicators
```python
# Calculate statistics on generated chunks
avg_chunk_size = sum(len(c) for c in chunks) / len(chunks)
min_chunk_size = min(len(c) for c in chunks)
max_chunk_size = max(len(c) for c in chunks)
size_variance = np.std([len(c) for c in chunks])

# Ideally:
# - avg_chunk_size ≈ target
# - size_variance < 30% of target
# - No chunks near model limits
```

### Retrieval Quality Post-Chunking
```python
# Test retrieval accuracy with this chunking strategy
test_queries = [...]  # Known queries with ground truth
correct = 0
for q in test_queries:
    results = retrieve(q)
    if expected_doc in results[:5]:
        correct += 1
accuracy = correct / len(test_queries)  # Target: >80%
```

## Implementation Checklist

- [ ] Identify document type(s) in your corpus
- [ ] Choose primary chunking strategy
- [ ] Set chunk size and overlap parameters
- [ ] Create test dataset (50+ queries with ground truth)
- [ ] Measure retrieval accuracy baseline
- [ ] A/B test 2-3 strategies
- [ ] Evaluate embedding model choice
- [ ] Test fallback chunking for edge cases
- [ ] Document final strategy and parameters
- [ ] Set up monitoring for chunk quality metrics

## Common Pitfalls

| Pitfall | Solution |
|---------|----------|
| Chunks too large | Reduce chunk_size, increase overlap |
| Too much variance | Use recursive splitting, add structure-aware separators |
| Loses important context | Increase overlap, try semantic chunking |
| Expensive to chunk | Start with recursive, only use semantic if needed |
| Chunks break mid-sentence | Use sentence-based or recursive splitting |

## Production Recommendation

**Start here**: Recursive character splitting at 400-512 tokens with 10-20% overlap.

If retrieval accuracy < 80%:
1. Try semantic chunking on subset
2. Evaluate embedding model (switch if needed)
3. Implement hybrid search
4. Add metadata filtering
5. Use re-ranking layer

## Claude Orchestrator Implementation

Our system uses two chunking approaches, both calibrated to the snowflake-arctic-embed2 model (~2000 token context window):

**Char-based chunking** (RL similarity, general embedding, document ingestion):
- Chunk size: 6000 chars (~1500 tokens, 75% of model budget)
- Overlap: 300 chars (~5%)
- Constants centralized in `orchestrator/context/embedding.py` (`EMBED_CHUNK_SIZE`, `EMBED_CHUNK_OVERLAP`)
- Used by: `retrieval_rl.py`, `embedding.py`, `synthetic_rl_data.py`, `ingestion.py`

**Token-based chunking** (KG node storage in Weaviate):
- Target: 1500 tokens, max: 2000 tokens (model limit)
- Sentence-boundary splitting for higher quality
- Implemented in `weaviate_mcp/chunking.py` (Chunker class)

We use 5% overlap rather than the 10-20% industry recommendation because our RL cosine path takes **max** similarity over all chunks — a single matching chunk dominates the signal regardless of boundary effects. Higher overlap would only increase embedding compute without improving the max-based score.

[[relatedTo::RAG - Retrieval-Augmented Generation]]
[[relatedTo::Semantic Search and Embeddings]]
[[relatedTo::Claude Orchestrator RL Retrieval]]
[[implements::Document Processing Pipeline]]
[[uses::LangChain]]
[[uses::LlamaIndex]]
