---
title: Orchestrator MCP Servers
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, mcp, tools]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Orchestrator MCP Servers

The orchestrator ships MCP (Model Context Protocol) servers that extend Claude Code with semantic search and academic paper search. All servers run as native Python processes registered in the user's MCP config and share a virtual environment at `claude_mcp_servers/.venv`.

[[implements::Model Context Protocol]] [[uses::Weaviate]] [[uses::Ollama]] [[relatedTo::Orchestrator Knowledge Graph]]

## Shipped Servers

| Server | Purpose | Key Tools |
|---|---|---|
| weaviate-kg | Semantic search + KG/code-graph management | hybrid_search, semantic_graph_search, search_code_graph, query_code_structure, store_knowledge_node, describe_excalidraw |
| search | Academic paper search | search_papers |
| mermaid | Mermaid diagram describe/extract | (registered, default-disabled per project) |
| excalidraw | Excalidraw diagram describe/extract | (registered, default-disabled per project) |
| code-embedding-service | GPU/CPU code-embedding HTTP service (port 11440) | `/embed`, `/health` (REST, not MCP) |

A separately-invoked **playwright** MCP (browser automation) is enabled by default and runs via `npx -y @playwright/mcp@latest`. The code-embedding FastAPI service on port 11440 is backend infrastructure for `weaviate-kg`, not an MCP exposed to Claude.

## Not exposed as MCP tools

| Capability | Rationale |
|---|---|
| Ollama (chat, read_document, read_image) | Covered by Claude's native reasoning, the `Read` tool, and built-in vision. Ollama keeps running as infrastructure for Weaviate text embeddings and the code-embedding CPU fallback. |
| Web search / page fetch | Covered by Claude's built-in WebFetch. Structured academic retrieval is served by `search_papers`. |

## weaviate-kg

**Script**: `claude_mcp_servers/weaviate_mcp/server.py`

**Purpose**: manages Knowledge Graph, Code Graph, and development-documentation collections in Weaviate. All semantic search routes through this server.

**Environment**:
```
WEAVIATE_URL=http://localhost:8081
OLLAMA_URL=http://localhost:11435
EMBEDDING_MODEL=qwen3-embedding:0.6b
KG_COLLECTION=<ProjectBasename>_KnowledgeGraph
SHARED_KG_COLLECTION=VibeCodedOrchestrator_KnowledgeGraph
DEVELOPMENT_COLLECTION=<ProjectBasename>_Development
GRPC_PORT=50052
SHARED_KG_READ_DISABLED=false   # excludes the shared KG from reads when true
SHARED_KG_WRITE_DISABLED=false  # refuses store_knowledge_node(scope="shared") when true
```

### hybrid_search
```python
hybrid_search(
    query: str,
    limit: int = 5,
    node_type: str = None,
    tags: list[str] = None,
    days: int = None,
    detail: str = "auto",   # "auto" | "titles" | "summary" | "single_chunk" | "three_chunks" | "full"
    include_stale: bool = False,
)
```
Combines BM25 keyword + vector similarity (hybrid fusion). Searches the per-project KG, the shared KG, and project development docs simultaneously, scoped to the active project. `detail="auto"` (default) picks a verbosity tier per result from its relevance score (discard < 0.42, summary, single_chunk, three_chunks, full ≥ 0.75). Results pass through an optional RL reranker — see [[Orchestrator RL Retrieval]].

### semantic_graph_search
```python
semantic_graph_search(
    query: str,
    limit: int = 5,
    depth: int = 2,   # max 3
    detail: str = "auto",
    include_stale: bool = False,
)
```
GraphRAG: finds seed nodes via embedding similarity, then traverses typed WikiLinks. Returns a connected subgraph rather than a flat ranked list. Primary results use per-result score tiering; connected neighbours always render at the `summary` tier.

### store_knowledge_node
```python
store_knowledge_node(
    title: str,
    content: str,
    node_type: str,
    tags: list[str],
    links: list[str],          # typed WikiLinks, "relationshipType::Target"
    file_path: str = "",
    scope: str = "project",     # "project" | "shared"
)
```
Writes the `.md` file and syncs to Weaviate. Upsert: skips if content identical. Preferred path is to write the `.md` file directly and let the PostToolUse hook sync; this tool exists for agents that cannot write files directly. `scope="shared"` writes to `SHARED_KG_COLLECTION` (falls back to the project KG if it is unset).

### search_code_graph
```python
search_code_graph(
    query: str,
    scope: str = "all",      # "all" | "code" | "interaction"
    limit: int = 8,
    expand_hops: int = 0,    # 0 | 1 | 2
    layer: str = None,        # API | Service | Data | UI | Utility
    project: str = None,
    detail: str = "auto",
)
```
Finds code entities by purpose using the code-embedding service. `scope="interaction"` filters to cross-service HTTP/gRPC calls; `layer` filters by architectural layer.

### query_code_structure
```python
query_code_structure(
    query_type: str,  # dependencies|imports|callers|methods|extends|interactions|path|composes|type_users
    target: str,
    project: str = None
)
```
Structural queries without reading source files. `path` type uses BFS (max depth 6) to find the shortest call path between two functions, format `"src.func->dst.func"`.

## ollama (infrastructure, not an MCP)

**Purpose**: Weaviate text embeddings (`qwen3-embedding:0.6b`) and the code-embedding service CPU fallback. KG-summary generation (`generate-kg-summary.py`) can also target Ollama directly when the Claude CLI is unavailable.

Ollama is not exposed as an MCP server. Claude's native reasoning, the `Read` tool, and built-in vision cover the chat / read-document / read-image use cases. Ollama runs at `http://localhost:11435` — its container is started by `ensure-containers.sh`.

## search

**Script**: `claude_mcp_servers/search_mcp/server.py`

**Purpose**: structured academic paper retrieval via OpenAlex and arXiv. Returns citation-rich, date-filtered metadata that ad-hoc web search cannot replicate.

**Environment**:
```
OPENALEX_EMAIL=<optional, polite-pool priority>
```

### search_papers
```python
search_papers(query, source="openalex", limit=10)
```
OpenAlex (CC0) or arXiv (CS/ML preprints, rate-limited). `OPENALEX_EMAIL` enables polite-pool priority. Calls structured APIs directly — no local search proxy needed. `search_papers` is the only tool this server exposes; general web access is covered by Claude's built-in WebFetch.

## code-embedding-service

**Script**: `claude_mcp_servers/code_embedding_service/server.py`

A small FastAPI server (not an MCP server — used internally by `weaviate-kg`) that wraps a code-embedding model. Exposes `/embed` and `/health`. Backend selected via `CODE_EMBED_BACKEND`:

- `gpu` (default if CUDA available): [[CodeSage-Large-v2]] — 2048-dim.
- `ollama`: `unclemusclez/jina-embeddings-v2-base-code` — 768-dim, CPU-friendly default. Override with `CODE_EMBED_MODEL` (e.g. `qwen3-embedding:0.6b`).

## Infrastructure

All servers register via the user's `~/.claude.json` (or per-project MCP config). Shared venv: `claude_mcp_servers/.venv`. Activate with `source claude_mcp_servers/.venv/bin/activate`.

**Multi-project routing**: per-project env vars in `.claude/settings.json` `env` (`KG_COLLECTION`, `SHARED_KG_COLLECTION`, `DEVELOPMENT_COLLECTION`, code-graph prefix) determine which collections are active — this is the canonical channel that propagates to MCP subprocesses across every Claude Code surface. When the launcher is running, its `vct-hub` resolves these per-project values and takes precedence. The active workspace determines the active KG, not which project's files are being discussed.

## Search Decision Tree

| Situation | Tool |
|---|---|
| Known exact term | `kg-search` CLI (~100ms, keyword) |
| Conceptual search | `hybrid_search` (default — most comprehensive) |
| Relationship exploration | `semantic_graph_search` |
| Code by purpose | `search_code_graph` |
| Architecture queries | `query_code_structure` |
| Quick analysis / rewrites | Claude's own reasoning |
| Large file extraction | `Read` tool with `offset`/`limit` |
| Web / current events | Claude's built-in WebFetch |
| Academic research | `search_papers` |
| Exact strings | Grep |
| Specific file content | Read |
