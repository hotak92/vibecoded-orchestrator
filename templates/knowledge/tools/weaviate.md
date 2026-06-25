---
title: "Weaviate"
type: tool
tags: [tool, database, vector-store, semantic-search, low-level-implementation]
created: 2026-01-28T19:00:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Weaviate

Open-source vector database for semantic search and AI applications.

## Purpose
Store and search high-dimensional vector embeddings with metadata filtering, enabling semantic search and knowledge graph capabilities.

## Key Features

### Vector Similarity Search
- HNSW algorithm for efficient ANN search
- Cosine similarity, L2 distance, etc.
- Sub-100ms query times

### Property Filtering
- Combine vector search with metadata filters
- GraphQL and gRPC APIs
- Complex boolean queries

### Knowledge Graph
- Cross-references between objects
- Graph traversal capabilities
- Relationship modeling

### Multi-tenancy
- Namespace isolation
- Collection-based organization
- Fine-grained access control

## Schema Design Patterns

### Document Chunks
```python
{
    "content": str,           # Chunk text
    "source_id": str,         # Original file path
    "chunk_index": int,       # Position in document
    "total_chunks": int,      # Document size
    "metadata": dict          # Custom metadata
}
```

### Knowledge Nodes
```python
{
    "title": str,             # Node name
    "content": str,           # Full content
    "node_type": str,         # Category
    "tags": List[str],        # Classifications
    "links": List[str],       # Connections
    "created_at": datetime,   # Creation time
    "updated_at": datetime    # Modification time
}
```

## Query Patterns

### Semantic Search
```python
# Use near_vector with manual embeddings (NOT near_text — no vectorizer configured)
query_embedding = get_embedding("your search query")
results = collection.query.near_vector(
    near_vector=query_embedding,
    limit=10
)
```

### Filtered Search
```python
query_embedding = get_embedding("your search query")
results = collection.query.near_vector(
    near_vector=query_embedding,
    filters=Filter.by_property("category").equal("value"),
    limit=10
)
```

### Graph Traversal
```python
# Get node
node = collection.query.fetch_objects(
    filters=Filter.by_property("title").equal("Node Name")
)

# Get connected nodes
links = node.objects[0].properties["links"]
connected = collection.query.fetch_objects(
    filters=Filter.by_property("title").contains_any(links)
)
```

## Performance Considerations
- Batch inserts for efficiency
- Index configuration impacts speed
- Vector dimensions affect memory
- gRPC faster than REST for high-throughput

## Best Practices
1. **Chunking**: Chunk against the active embedder's token budget — the orchestrator's `Chunker.for_model()` resolves per-model presets (e.g. ~10k for qwen3-embedding:0.6b, ~4k for snowflake-arctic-embed2), not a single fixed limit
2. **Metadata**: Rich metadata enables better filtering
3. **Embeddings**: Use consistent model across collection
4. **Batch operations**: Reduce API calls
5. **Schema design**: Plan properties upfront

## Critical Constraints

### Embedding Token Budget

Ollama silently truncates input beyond an embedder's effective context, so chunk against a per-model budget rather than the documented maximum. The orchestrator's `chunking.py` sets these budgets: `qwen3-embedding:0.6b` ~10k tokens (the active default; model arch supports 32k), `snowflake-arctic-embed2` ~4k tokens (documents 8k). Both storage AND queries must respect the active model's budget.

### Chunking for Large Documents

```python
from chunking import Chunker, TokenCounter
import uuid

# for_model() resolves the per-model budget; the Chunker derives min/max/target from it
chunker = Chunker.for_model("qwen3-embedding:0.6b")
if TokenCounter.count_tokens(content) > chunker.max_tokens:
    source_node_id = str(uuid.uuid4())
    chunks = chunker.chunk_text(content, source_node_id, metadata)
    for chunk in chunks:
        embedding = server._get_embedding(chunk.content)
        collection.data.insert(properties={**data, 'chunk_num': chunk.chunk_number + 1,
                                           'total_chunks': chunk.total_chunks,
                                           'source_node_id': source_node_id}, vector=embedding)
```

Schema requirements for chunking: `chunk_num` (int), `total_chunks` (int), `source_node_id` (text UUID).

### Search with Deduplication

When chunked content is stored, search with higher limit and deduplicate by `source_node_id`:

```python
results = collection.query.near_vector(near_vector=query_embedding, limit=limit * 5,
    return_properties=['title', 'source_node_id', 'chunk_num', ...])
seen_nodes = {}
for obj in results.objects:
    source_id = obj.properties.get('source_node_id')
    if source_id not in seen_nodes:
        seen_nodes[source_id] = obj
unique_results = list(seen_nodes.values())[:limit]
```

## Anti-Patterns

- **Do NOT** use `near_text` — no vectorizer configured; always use `near_vector` with manual embeddings
- **Do NOT** ignore token limits — check content length before embedding
- **Do NOT** store chunks without `source_node_id` — can't reassemble later
- **Do NOT** return multiple chunks in search results — deduplicate by `source_node_id`
- **Do NOT** use curl for health checks — use Python client `.is_ready()` instead

## Testing Checklist

- [ ] Verify the active embedding model is loaded: `ollama list`
- [ ] Test with content under the model's chunk budget (should store as one object)
- [ ] Test with content over the budget (should chunk)
- [ ] Test chunk reassembly and deduplication
- [ ] Test filtered search by type, tags
- [ ] Check health using Python client, not curl

## Links

- [[uses::Ollama]] - Provides embeddings via text2vec-ollama
- [[uses::HNSW Algorithm]] - Primary vector index
- [[relatedTo::Semantic Search]] - Search pattern
