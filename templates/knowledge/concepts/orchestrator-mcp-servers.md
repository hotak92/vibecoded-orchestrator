---
title: Orchestrator MCP Servers
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, mcp, tools]
created: 2026-04-27T18:30:00Z
updated: 2026-06-12T00:00:00Z
status: active
---

# Orchestrator MCP Servers

The orchestrator ships MCP (Model Context Protocol) servers that extend Claude Code with semantic search and academic paper search. All servers run as native Python processes registered in the user's MCP config and share a virtual environment at `claude_mcp_servers/.venv`.

[[implements::Model Context Protocol]] [[uses::Weaviate]] [[uses::Ollama]] [[relatedTo::Orchestrator Knowledge Graph]] [[relatedTo::MCP Simplification v0.2.11]]

## Shipped Servers (v0.2.11+)

| Server | Purpose | Key Tools |
|---|---|---|
| weaviate-kg | Semantic search + KG/code-graph management | hybrid_search, search_code_graph, store_knowledge_node, query_code_structure |
| search | Academic paper search | search_papers |
| code-embedding-service | GPU/CPU code-embedding HTTP service (port 11440) | `/embed`, `/health` (REST, not MCP) |

## Removed in v0.2.11

| Server | Rationale |
|---|---|
| ollama (chat, read_document, read_image) | Redundant with Claude's native reasoning, Read tool, and built-in vision. For OAuth-subscription users the "FREE" framing was misleading. Ollama continues running as infrastructure for Weaviate embeddings. |
| search (web_search, fetch_page, search_code) | Redundant with Claude's built-in WebFetch; SearXNG removed from default container stack. `search_papers` kept for structured academic retrieval. |

See `knowledge/concepts/mcp-simplification-v0211.md` for the full architecture decision record.

## weaviate-kg

**Script**: `claude_mcp_servers/weaviate_mcp/server.py`

**Purpose**: manages Knowledge Graph, Code Graph, and development-documentation collections in Weaviate. All semantic search routes through this server.

**Environment**:
```
WEAVIATE_URL=http://localhost:8081
OLLAMA_URL=http://localhost:11435
EMBEDDING_MODEL=qwen3-embedding:0.6b
LEGACY_TEXT_EMBEDDING_MODEL=snowflake-arctic-embed2:latest
KG_COLLECTION=<ProjectBasename>_KnowledgeGraph
DEVELOPMENT_COLLECTION=<ProjectBasename>_development
GRPC_PORT=50052
```

### hybrid_search
```python
hybrid_search(
    query: str,
    limit: int = 10,
    node_type: str = None,
    tags: list[str] = None,
    days: int = None,
    detail: str = "descriptions"  # "titles" | "descriptions" | "full"
)
```
Combines BM25 keyword + vector similarity (hybrid fusion). Searches KG collection and development docs simultaneously, scoped to the active project. Results pass through an optional RL reranker — see [[Orchestrator RL Retrieval]].

### semantic_graph_search
```python
semantic_graph_search(query: str, depth: int = 2)
```
GraphRAG: finds seed nodes via embedding similarity, then traverses WikiLinks. Returns a connected subgraph rather than a flat ranked list.

### store_knowledge_node
```python
store_knowledge_node(
    title, content, node_type, tags,
    file_path, scope="project"  # "project" | "shared"
)
```
Writes the `.md` file and syncs to Weaviate. Upsert: skips if content identical. Preferred path is to write the `.md` file directly and let the PostToolUse hook sync; this tool exists for agents that cannot write files directly.

### search_code_graph
```python
search_code_graph(
    query: str,
    scope: str = "all",  # "all" | "code" | "interaction"
    limit: int = 10,
    expand_hops: int = 0
)
```
Finds code entities by purpose using the code-embedding service. `scope="interaction"` filters to cross-service HTTP/gRPC calls.

### query_code_structure
```python
query_code_structure(
    query_type: str,  # dependencies|imports|callers|methods|extends|interactions|path|composes|type_users
    target: str,
    project: str = None
)
```
Structural queries without reading source files. `path` type uses BFS (max depth 6) to find the shortest call path between two functions, format `"src.func->dst.func"`.

## ollama (infrastructure only as of v0.2.11)

**Purpose**: Weaviate text embeddings (`qwen3-embedding:0.6b`) and code-embedding service CPU fallback. KG-summary generation (`generate-kg-summary.py`) can also target Ollama directly when the Claude CLI is unavailable.

The Ollama MCP server (`chat`, `read_document`, `read_image`) was removed in v0.2.11 as redundant. Claude's native reasoning replaces `chat`, the `Read` tool replaces `read_document`, and Claude's built-in vision replaces `read_image`. Ollama continues running at `http://localhost:11435` — the container is still started by `ensure-containers.sh`. See [[MCP Simplification v0.2.11]].

## search (search_papers only as of v0.2.11)

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
OpenAlex (240M papers, CC0) or arXiv (CS/ML preprints, rate-limited 0.333 req/s). `OPENALEX_EMAIL` enables polite-pool priority. Calls structured APIs directly — no local search proxy needed.

**Removed in v0.2.11** (v0.2.10 and earlier only):
- `web_search` — routed through SearXNG; superseded by Claude's built-in WebFetch.
- `fetch_page` — fetched arbitrary URLs; superseded by Claude's built-in WebFetch.
- `search_code` — GitHub code search; `search_code_graph` covers in-project code search semantically.
- **SearXNG container** (`SEARXNG_URL=http://localhost:8888`) — removed from `compose.yaml`.

## code-embedding-service

**Script**: `claude_mcp_servers/code_embedding_service/server.py`

A small FastAPI server (not an MCP server — used internally by `weaviate-kg`) that wraps a code-embedding model. Exposes `/embed` and `/health`. Backend selected via `CODE_EMBED_BACKEND`:

- `gpu` (default if CUDA available): [[CodeSage-Large-v2]] (2048-dim).
- `ollama`: [[Jina Embeddings v2 Base Code]] (768-dim, CPU-friendly).

## Infrastructure

All servers register via the user's `~/.claude.json` (or per-project MCP config). Shared venv: `claude_mcp_servers/.venv`. Activate with `source claude_mcp_servers/.venv/bin/activate`.

**Multi-project routing**: the `KG_COLLECTION` env var in VS Code workspace settings (`.vscode/settings.json::claude-code.env`) determines which collection is active. Opening a different workspace → that project's KG is active. The active VS Code workspace determines the KG target, not which project's files are being discussed.

## Search Decision Tree (v0.2.11+)

| Situation | Tool |
|---|---|
| Known exact term | `kg-search` CLI (~100ms, keyword) |
| Conceptual search | `hybrid_search` (default — most comprehensive) |
| Relationship exploration | `semantic_graph_search` |
| Code by purpose | `search_code_graph` |
| Architecture queries | `query_code_structure` |
| Quick analysis / rewrites | Claude's own reasoning (Ollama MCP removed v0.2.11) |
| Large file extraction | `Read` tool with `offset`/`limit` |
| Web / current events | Claude's built-in WebFetch (web_search removed v0.2.11) |
| Academic research | `search_papers` |
| Exact strings | Grep |
| Specific file content | Read |
