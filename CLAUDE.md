# VibeCoded Tools — Orchestrator

## What This Is

An intelligent layer on top of Claude Code that adds:
- **Knowledge Graph** — persistent semantic memory across sessions (Weaviate + Ollama embeddings)
- **Code Graph** — AST-based code understanding with semantic search (5 entity types)
- **Automated Context Injection** — hooks that proactively feed relevant context to Claude
- **Security Scanning** — credential detection, shell injection prevention
- **Workflow Automation** — CONTEXT_STATE tracking, plans, memory management

## How It Works

The orchestrator runs transparently via Claude Code hooks. When you use Claude Code in this project:
1. **SessionStart**: Containers are checked, context is loaded
2. **UserPromptSubmit**: Relevant KG/code graph results are injected into context
3. **PreToolUse (Edit)**: Related code patterns are surfaced before edits
4. **PostToolUse (Write/Edit)**: Files are auto-synced to KG/code graph, security scanned
5. **PreCompact/PostCompact**: Context is preserved across compactions

You don't need to do anything special — just use Claude Code as normal.

## Quick Reference

### Search

```bash
# Knowledge graph (keyword)
.claude/scripts/kg-search search "query" [--type TYPE] [--tags TAGS]
.claude/scripts/kg-info info "Node Title"

# Code graph (semantic)
.claude/scripts/code-graph-query search "pattern name"
.claude/scripts/code-graph-query structure callers "module.function"
```

### MCP Tools (available in Claude Code sessions)

- `hybrid_search(query)` — semantic search across KG + project docs
- `semantic_graph_search(query)` — graph-aware search with WikiLink traversal
- `search_code_graph(query)` — find code by purpose/concept
- `query_code_structure(type, target)` — structural queries (callers, deps, methods)
- `chat(prompt)` — local LLM inference (FREE, via Ollama)

### Knowledge Graph

Nodes live in `knowledge/` as Markdown with YAML frontmatter:
```yaml
---
title: Node Title
type: concept  # project, concept, tool, research, model
tags: [tag1, tag2]
status: active
---
Content with [[relatedTo::Other Node]] typed WikiLinks.
```

### Maintenance

```bash
# Sync all KG nodes to Weaviate
.claude/scripts/kg-sync --all

# Analyze code graph
.claude/scripts/code-graph-analyze . --project "MyProject"

# Check for duplicate KG nodes
.claude/scripts/kg-duplicates
```

### Configuration

- `.env` — ports, embedding model, API keys
- `.claude/settings.json` — Claude Code permissions and MCP env vars
- `infrastructure/docker-compose.yml` — Weaviate + Ollama containers

### Directories

| Directory | Purpose |
|-----------|---------|
| `knowledge/` | Knowledge graph nodes (Markdown + YAML frontmatter) |
| `.claude/hooks/` | Automated workflow hooks |
| `.claude/scripts/` | CLI utilities for KG/code graph |
| `claude_mcp_servers/` | MCP servers (weaviate-kg, ollama, search, code embedding) |
| `infrastructure/` | Docker/Podman compose files |
| `config/` | Configuration templates |
| `docs/` | Documentation |
| `state/` | Runtime state (gitignored) |

## Embedding Models

The installer auto-configures based on your hardware:

| Hardware | Text Embeddings | Code Embeddings | Quality |
|----------|----------------|-----------------|---------|
| NVIDIA GPU | qwen3-embedding (Ollama) | CodeSage-Large-v2 (GPU) | Best |
| CPU only | qwen3-embedding (Ollama) | qwen3-embedding (fallback) | Good |
| OpenAI key | text-embedding-3-small | text-embedding-3-small | Good (fast) |

## License

AGPL-3.0 — free for individuals. See LICENSE file.
