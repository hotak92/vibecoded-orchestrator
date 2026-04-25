---
title: Weaviate Usage Patterns
type: tool
tags:
- tool
- weaviate
- vector-store
- semantic-search
- best-practices
- AI
- python
created: 2026-01-28 19:00:00+00:00
updated: 2026-04-05T14:34:58Z
status: superseded
superseded_by: "Weaviate"
---

# Weaviate Usage Patterns

**Purpose**: Practical patterns for using Weaviate with Ollama embeddings
**Last Updated**: 2026-01-14

## Links

- [[relatedTo::Weaviate]]
- [[relatedTo::Ollama]]

## Critical Constraints

### Embedding Model Limits

**Model**: `snowflake-arctic-embed2:latest` via Ollama

**Token Limits**:
- Documented: 8192 tokens
- **Actual working limit: 2500 tokens**
- Conservative chunking: 2500 tokens max per chunk

**Why this matters**:
- Both storage AND queries must respect this limit
- Exceeding causes: `{"error":"the input length exceeds the context length"}`
- Must implement chunking for content >2500 tokens
- Must truncate queries >2500 tokens

## Storage Patterns

### Pattern 1: Small Documents (<2500 tokens)

**Use case**: Knowledge graph nodes, short documents, small content

**Implementation**:
```python
from chunking import TokenCounter

token_count = TokenCounter.count_tokens(content)
if token_count <= 2500:
    # Store as single object
    embedding = server._get_embedding(content)
    collection.data.insert(properties=data, vector=embedding)
```

**When to use**: Most knowledge nodes, configs, short docs

### Pattern 2: Large Documents (>2500 tokens)

**Use case**: Long documentation, research papers, large knowledge nodes

**Implementation**:
```python
from chunking import Chunker, TokenCounter
import uuid

token_count = TokenCounter.count_tokens(content)
if token_count > 2500:
    # Generate source_node_id for linking chunks
    source_node_id = str(uuid.uuid4())

    # Chunk with safe limits
    chunker = Chunker(min_tokens=1000, max_tokens=2500, target_tokens=2000)
    chunks = chunker.chunk_text(content, source_node_id, metadata)

    # Store each chunk with shared metadata
    for chunk in chunks:
        embedding = server._get_embedding(chunk.content)
        data = {
            'title': title,
            'content': chunk.content,
            'tags': tags,  # Shared across all chunks
            'links': links,  # Shared across all chunks
            'chunk_num': chunk.chunk_number + 1,
            'total_chunks': chunk.total_chunks,
            'source_node_id': source_node_id
        }
        collection.data.insert(properties=data, vector=embedding)
```

**Schema requirements**:
- `chunk_num` (int) - 1-indexed chunk position
- `total_chunks` (int) - Total chunks for this document
- `source_node_id` (text) - UUID linking all chunks

**When to use**: Documentation, long configs, detailed guides

## Query Patterns

### Pattern 1: Basic Semantic Search

**Use case**: Find relevant documents by meaning

**Implementation**:
```python
from chunking import TokenCounter

# Truncate query if needed
query_tokens = TokenCounter.count_tokens(query)
if query_tokens > 2500:
    max_chars = 2500 * 4
    query = query[:max_chars]
    print(f"⚠️ Query truncated to 2500 tokens")

# Get embedding
query_embedding = server._get_embedding(query)

# Search with near_vector (NOT near_text - no vectorizer configured)
results = collection.query.near_vector(
    near_vector=query_embedding,
    limit=10
)
```

**When to use**: Any semantic search operation

### Pattern 2: Search with Deduplication (Chunked Content)

**Use case**: Find documents that may be split into chunks

**Implementation**:
```python
# Search with higher limit to account for chunks
results = collection.query.near_vector(
    near_vector=query_embedding,
    limit=limit * 5,  # Fetch more for deduplication
    return_properties=['title', 'source_node_id', 'chunk_num', 'total_chunks', ...]
)

# Deduplicate by source_node_id (keep best match per document)
seen_nodes = {}
for obj in results.objects:
    source_id = obj.properties.get('source_node_id')
    if source_id not in seen_nodes:
        seen_nodes[source_id] = obj

unique_results = list(seen_nodes.values())[:limit]
```

**When to use**: Knowledge graph search, document search

### Pattern 3: Chunk Reassembly

**Use case**: Retrieve full content from chunked document

**Implementation**:
```python
def reassemble_chunks(collection, source_node_id: str) -> Dict:
    """Fetch and reassemble all chunks"""
    # Get all chunks with this source_node_id
    where_filter = Filter.by_property("source_node_id").equal(source_node_id)
    chunks = collection.query.fetch_objects(filters=where_filter, limit=100)

    # Sort by chunk_num
    sorted_chunks = sorted(chunks.objects,
                          key=lambda obj: obj.properties.get('chunk_num', 1))

    # Reassemble content
    full_content = "\n\n".join(obj.properties['content']
                               for obj in sorted_chunks)

    return {
        'content': full_content,
        'total_chunks': sorted_chunks[0].properties.get('total_chunks', 1),
    }
```

**When to use**: After finding chunked document, need full content

### Pattern 4: Filtered Search

**Use case**: Semantic search with metadata filters

**Implementation**:
```python
from weaviate.classes.query import Filter

# Build filters
filters = None
if node_type:
    filters = Filter.by_property("node_type").equal(node_type)
if tags:
    tag_filter = Filter.by_property("tags").contains_any(tags)
    filters = filters & tag_filter if filters else tag_filter

# Search with filters
results = collection.query.near_vector(
    near_vector=query_embedding,
    limit=10,
    filters=filters
)
```

**When to use**: Need to narrow search by type, tags, or other metadata

## Collection Schema Patterns

### Pattern 1: Knowledge Graph Collection

**Use case**: Semantic search over interconnected knowledge nodes

**Schema**:
```python
from weaviate.classes.config import Configure, Property, DataType

collection = client.collections.create(
    name="ClaudeKnowledgeGraph",
    description="Knowledge graph with chunking support",
    properties=[
        Property(name="title", data_type=DataType.TEXT),
        Property(name="content", data_type=DataType.TEXT),
        Property(name="file_path", data_type=DataType.TEXT),
        Property(name="node_type", data_type=DataType.TEXT),
        Property(name="tags", data_type=DataType.TEXT_ARRAY),
        Property(name="links", data_type=DataType.TEXT_ARRAY),
        Property(name="created_at", data_type=DataType.DATE),
        Property(name="updated_at", data_type=DataType.DATE),
        # Chunking support
        Property(name="chunk_num", data_type=DataType.INT),
        Property(name="total_chunks", data_type=DataType.INT),
        Property(name="source_node_id", data_type=DataType.TEXT)
    ],
    vectorizer_config=Configure.Vectorizer.none()  # Manual embeddings
)
```

**When to use**: Knowledge bases, documentation collections

## Anti-Patterns

### ❌ Don't: Use near_text Without Vectorizer

**Problem**:
```python
# This fails if no vectorizer configured
results = collection.query.near_text(query="search term")
# Error: "Make sure a vectorizer module is configured"
```

**Solution**: Use `near_vector` with manual embeddings

### ❌ Don't: Ignore Token Limits

**Problem**:
```python
# This fails for content >2500 tokens
embedding = server._get_embedding(large_content)
# Error: "input length exceeds context length"
```

**Solution**: Check token count, chunk if needed

### ❌ Don't: Store Chunks Without source_node_id

**Problem**: Can't reassemble chunks later

**Solution**: Always use source_node_id to link chunks

### ❌ Don't: Return Duplicate Chunks in Search

**Problem**: User sees same document multiple times (once per chunk)

**Solution**: Deduplicate by source_node_id

### ❌ Don't: Use curl for Health Checks

**Problem**: Services don't respond correctly to curl

**Solution**: Use Python client:
```python
import weaviate
client = weaviate.connect_to_local(host='localhost', port=8080, grpc_port=50051)
is_healthy = client.is_ready()
```

## Performance Considerations

### Token Counting Performance

**Approximation method** (fast):
```python
tokens = len(text) // 4  # 1 token ≈ 4 characters
```

**Accurate method** (slower, use for critical operations):
```python
from langchain_ollama import ChatOllama
llm = ChatOllama(model="qwen3:0.6b")
tokens = llm.get_num_tokens(text)
```

**Recommendation**: Use approximation for chunking decisions, accurate for verification

### Chunking Strategy

**Sentence boundary splitting** (best quality):
- Preserves meaning
- Clean breaks
- Slightly more expensive

**Character boundary splitting** (faster):
- May split mid-sentence
- Simpler logic
- Use for very large documents

**Recommendation**: Use sentence boundary for <10k tokens, character for larger

### Batch Operations

**Insert multiple chunks**:
```python
# Prepare all chunks first
chunk_data = []
for chunk in chunks:
    embedding = server._get_embedding(chunk.content)
    chunk_data.append({'properties': data, 'vector': embedding})

# Batch insert (if supported by client)
collection.data.insert_many(chunk_data)
```

**Recommendation**: Batch when inserting >10 objects

## Testing Checklist

Before deploying Weaviate integration:

- [ ] Verify embedding model loaded: `ollama list | grep arctic`
- [ ] Test with 1000 token content (should work)
- [ ] Test with 2500 token content (should work)
- [ ] Test with 3000 token content (should chunk or fail gracefully)
- [ ] Verify query truncation for long queries
- [ ] Test chunk reassembly
- [ ] Test deduplication in search results
- [ ] Verify tags/links shared across chunks
- [ ] Test filtered search (by type, tags)
- [ ] Check health using Python client, not curl

## Common Issues

**Issue**: "input length exceeds context length"
- **Cause**: Content or query >2500 tokens
- **Fix**: Implement chunking/truncation

**Issue**: "Make sure a vectorizer module is configured"
- **Cause**: Using `near_text` without vectorizer
- **Fix**: Use `near_vector` with manual embeddings

**Issue**: Duplicate results in search
- **Cause**: Returning multiple chunks from same document
- **Fix**: Deduplicate by `source_node_id`

**Issue**: Can't reassemble chunked content
- **Cause**: Missing `source_node_id` or `chunk_num`
- **Fix**: Always include chunking metadata in schema

**Issue**: Health check fails with curl
- **Cause**: Services don't respond to curl correctly
- **Fix**: Use Python client `.is_ready()`
