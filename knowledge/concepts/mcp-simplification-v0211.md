---
title: MCP Simplification v0.2.11
type: concept
tags: [mcp, deprecation, architecture-decision, v0.2.11, vibecoded-orchestrator]
created: 2026-05-16T00:00:00Z
updated: 2026-05-16T03:54:53Z
status: active
---

# MCP Simplification v0.2.11

In v0.2.11 the orchestrator reduced its MCP surface area by removing the Ollama MCP entirely and narrowing the Search MCP to a single tool. This document records the rationale, what was removed, what was kept, and the migration path for existing installs.

[[relatedTo::Orchestrator MCP Servers]] [[relatedTo::Model Context Protocol]] [[relatedTo::Orchestrator Context Management]]

## Why We Simplified

Three forces converged:

1. **Capability parity**: Claude Code's native capabilities have matured. Claude's built-in reasoning replaces `chat`; the native `Read` tool with `offset`/`limit` replaces `read_document`; Claude's built-in vision (pass an image path to `Read`) replaces `read_image`. Adding an Ollama MCP hop introduced latency, complexity, and a new failure mode for no quality gain.

2. **Misleading cost framing**: the Ollama MCP was marketed as "FREE" to distinguish it from Claude API token cost. For users on a claude.ai OAuth subscription (the primary target audience), Claude tokens are already included in the subscription fee — so the "cost" comparison was inaccurate and steered agents toward an inferior tool.

3. **SearXNG operational burden**: running a self-hosted SearXNG instance added a container, configuration, and a maintenance surface. The only tool that used it (`web_search`) was superseded by Claude's built-in WebFetch. Removing SearXNG simplifies the default container stack from 4 services to 3 (Weaviate, Ollama, code-embed).

## What Was Removed

### Ollama MCP (`claude_mcp_servers/ollama_mcp/server.py`)

All three tools removed:

| Tool | Replacement |
|---|---|
| `chat(prompt, model, ...)` | Claude's own reasoning (no separate call needed) |
| `read_document(file_path, ...)` | Native `Read` tool with `offset`/`limit` for large files |
| `read_image(file_path, ...)` | Native `Read` tool on image path (Claude's built-in vision) |

### Search MCP — narrowed to `search_papers` only

| Tool | Status | Replacement |
|---|---|---|
| `web_search` | Removed | Claude's built-in WebFetch |
| `fetch_page` | Removed | Claude's built-in WebFetch |
| `search_code` | Removed | `search_code_graph` (semantic, in-project) |
| `search_papers` | **Kept** | No replacement needed — unique structured value |

`search_papers` was kept because OpenAlex (240M works, CC0) and arXiv return citation-rich, date-filtered, deduplicated academic metadata that ad-hoc web search cannot replicate. The academic research use case has genuine structured-API value.

### SearXNG container

Removed from `compose.yaml`. Was only used by `web_search`. No replacement needed — Claude's built-in WebFetch covers ad-hoc web retrieval.

## What Was Kept

- **Ollama infrastructure**: Ollama container still starts at `http://localhost:11435`. It serves `qwen3-embedding:0.6b` for Weaviate text embeddings and acts as a CPU fallback for the code-embedding service. KG-summary generation (`generate-kg-summary.py`) can still target Ollama when the Claude CLI is unavailable.
- **weaviate-kg MCP**: unchanged. All semantic search tools (`hybrid_search`, `semantic_graph_search`, `search_code_graph`, `query_code_structure`, `store_knowledge_node`) are unaffected.
- **coordination MCP**: unchanged.
- **Playwright MCP**: unchanged.

## Migration Path (Existing Installs)

The `update_project_v2` ("Update bundle") flow writes three deferral keys to `<project>/.claude/context/UPDATE_DEFERRED.md` for human review:

| Deferral key | Meaning |
|---|---|
| `ollama_mcp_deprecated` | The `ollama` entry in `~/.claude.json` `mcpServers` should be removed. The bundle updater cannot edit `~/.claude.json` (user-global file); the user must remove it manually or via `claude mcp remove ollama`. |
| `search_mcp_simplified` | The `search` MCP entry in `~/.claude.json` stays; only its exposed tools change. No manual action needed unless the user pinned `web_search`/`fetch_page`/`search_code` in agent frontmatter. |
| `searxng_removed_from_default_install` | The SearXNG container is no longer in the default `compose.yaml`. If the user added custom SearXNG config, their `docker-compose.override.yml` continues to work; the base file just no longer references it. |

## Date

Architecture decision recorded: 2026-05-16. Shipped in vibecoded-orchestrator v0.2.11.
