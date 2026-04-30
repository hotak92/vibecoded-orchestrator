---
title: Orchestrator MCP Servers
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, mcp, tools]
created: 2026-04-27T18:30:00Z
updated: 2026-04-27T18:30:00Z
status: active
---

# Orchestrator MCP Servers

The orchestrator ships several MCP (Model Context Protocol) servers that extend Claude Code with semantic search, local inference, web search, and team coordination. All servers run as native Python processes registered in the user's MCP config and share a virtual environment at `claude_mcp_servers/.venv`.

[[implements::Model Context Protocol]] [[uses::Weaviate]] [[uses::Ollama]] [[relatedTo::Orchestrator Knowledge Graph]]

## Shipped Servers

| Server | Purpose | Key Tools |
|---|---|---|
| weaviate-kg | Semantic search + KG/code-graph management | hybrid_search, search_code_graph, store_knowledge_node, query_code_structure |
| ollama | Local LLM inference (FREE) | chat, read_document, read_image |
| search | Web + academic + GitHub code search | web_search, search_papers, search_code, fetch_page |
| code-embedding-service | GPU/CPU code-embedding HTTP service (port 11438) | `/embed`, `/health` (REST, not MCP) |
| coordination | Team coordination notes (Pro tier) | post_coordination_note, read_coordination_notes |

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

## ollama

**Script**: `claude_mcp_servers/ollama_mcp/server.py`

**Purpose**: local LLM inference. All calls are FREE — runs on local GPU/CPU. Used instead of Claude API for analysis, rewriting, and summarization tasks.

**Environment**: `OLLAMA_URL=http://localhost:11435`.

### chat
```python
chat(
    prompt: str,
    model: str = "qwen3.5:0.8b",
    system_prompt: str = None,
    temperature: float = 0.7,
    max_tokens: int = 2048
)
```
Direct inference via Ollama API. Common models: `qwen3.5:0.8b` (fast), `qwen3.5:9b` (8B, better reasoning), [[Qwen3.5]] family for vision + text.

### read_document
```python
read_document(
    file_path: str,
    model: str = "qwen3.5:9b",
    task: str = "summarize",
    context_lines: int = 50
)
```
Loads a file and processes it locally. For files >100K chars, auto-switches to chunked-scanning mode (overlapping windows). Used for extracting specific info from large files without consuming the Claude API context budget.

### read_image
```python
read_image(file_path: str, prompt: str = None)
```
Returns the image as a base64 data URL Claude can see directly, plus an optional local description from a vision model. Memory-aware gating picks a vision model that fits available VRAM/RAM, falling back to a smaller model or skipping the description with a clear reason. See [[read_image — Memory-Aware Vision-Model Gating]].

## search

**Script**: `claude_mcp_servers/search_mcp/server.py`

**Purpose**: external information retrieval — web, academic papers, GitHub code.

**Environment**:
```
GITHUB_TOKEN=<optional>
OPENALEX_EMAIL=<optional, polite-pool priority>
SEARXNG_URL=http://localhost:8888
```

### web_search
```python
web_search(query: str, num_results: int = 10)
```
Routes through a local SearXNG instance. Rate-limited (1 req/s). No API key required.

### search_code
```python
search_code(query, language=None, repo=None)
```
GitHub code search API. Rate-limited (0.5 req/s). Authenticated via `GITHUB_TOKEN` for higher quotas.

### search_papers
```python
search_papers(query, source="openalex", limit=10)
```
OpenAlex (240M papers) or arXiv (CS/ML preprints, rate-limited 0.333 req/s). `OPENALEX_EMAIL` enables polite-pool priority.

### fetch_page
```python
fetch_page(url: str)
```
Fetches the full content of any URL; returns cleaned text (strips HTML, scripts, ads).

## code-embedding-service

**Script**: `claude_mcp_servers/code_embedding_service/server.py`

A small FastAPI server (not an MCP server — used internally by `weaviate-kg`) that wraps a code-embedding model. Exposes `/embed` and `/health`. Backend selected via `CODE_EMBED_BACKEND`:

- `gpu` (default if CUDA available): [[CodeSage-Large-v2]] (2048-dim).
- `ollama`: [[Jina Embeddings v2 Base Code]] (768-dim, CPU-friendly).

## coordination

**Script**: `claude_mcp_servers/coordination_mcp/server.py` (Pro tier — separate package).

Local KG-backed coordination notes for team decisions, task assignments, and cross-session agreements. Provides a persistent scratchpad that survives context compaction.

### post_coordination_note
```python
post_coordination_note(title, content, category="decision")
```

### read_coordination_notes
```python
read_coordination_notes(category=None, days=7)
```

## Infrastructure

All servers register via the user's `~/.claude.json` (or per-project MCP config). Shared venv: `claude_mcp_servers/.venv`. Activate with `source claude_mcp_servers/.venv/bin/activate`.

**Multi-project routing**: the `KG_COLLECTION` env var in VS Code workspace settings (`.vscode/settings.json::claude-code.env`) determines which collection is active. Opening a different workspace → that project's KG is active. The active VS Code workspace determines the KG target, not which project's files are being discussed.

## Search Decision Tree

| Situation | Tool |
|---|---|
| Known exact term | `kg-search` CLI (~100ms, keyword) |
| Conceptual search | `hybrid_search` (default — most comprehensive) |
| Relationship exploration | `semantic_graph_search` |
| Code by purpose | `search_code_graph` |
| Architecture queries | `query_code_structure` |
| Quick analysis / rewrites | `chat` (Ollama, FREE) |
| Large file extraction | `read_document` (Ollama, FREE) |
| Web / current events | `web_search` |
| Academic research | `search_papers` |
| Exact strings | Grep |
| Specific file content | Read |
