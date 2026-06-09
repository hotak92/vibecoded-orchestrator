---
title: Model Context Protocol
type: concept
tags: [MCP, AI, protocol, tools, integration, Anthropic, agentic]
created: 2026-02-26T00:00:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

## Overview

The Model Context Protocol (MCP) is an open standard introduced by Anthropic in November 2024 to standardize how AI models connect to external data sources, tools, and services. It functions as a "USB-C for AI" — a universal connector that replaces bespoke integrations with a single, stable protocol.

MCP defines a client-server architecture where AI applications (hosts) connect to MCP servers that expose capabilities in three categories: **tools**, **resources**, and **prompts**.

## Architecture

```
Host (Claude, VS Code, etc.)
  └── MCP Client
        └── MCP Server 1 (e.g., filesystem)
        └── MCP Server 2 (e.g., database)
        └── MCP Server 3 (e.g., Weaviate KG)
```

### Core Primitives

**Tools** — executable functions the LLM can call:
- Have a name, description, and JSON Schema for parameters
- The LLM decides when to call them based on the description
- Results return to the LLM as tool outputs

**Resources** — data sources exposed to the client:
- URI-addressed content (files, database rows, API responses)
- Read by the host (not auto-invoked by the LLM)
- Can be static or dynamic (templates)

**Prompts** — pre-defined prompt templates:
- User-invokable slash commands or workflow templates
- Can include embedded resource references

**Sampling** — server can request LLM completions:
- Enables server-side agentic patterns
- Subject to host approval

### Transport Mechanisms

- **stdio** — local process communication (default for Claude Code)
- **HTTP + SSE** (Server-Sent Events) — remote servers, streaming
- **Streamable HTTP** — newer transport supporting bidirectional streams

## Protocol Flow

1. Client sends `initialize` with protocol version and capabilities
2. Server responds with supported capabilities
3. Client calls `tools/list`, `resources/list`, `prompts/list`
4. LLM decides to invoke tool → client sends `tools/call`
5. Server executes and returns result
6. LLM incorporates result into response

## Security Model

- Servers cannot access the full conversation history
- Tool calls require explicit LLM decision (not automatic)
- Hosts can restrict which servers/tools are exposed
- `mcp-request-context` pattern: inject per-request auth context
- Users must explicitly allow dangerous operations (file write, shell exec)

## Implementation (Python with FastMCP)

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-server")

@mcp.tool()
def search_knowledge(query: str, limit: int = 5) -> list[dict]:
    """Search the knowledge graph for relevant nodes."""
    return knowledge_graph.search(query, limit)

@mcp.resource("knowledge://{node_id}")
def get_node(node_id: str) -> str:
    """Get a specific knowledge node by ID."""
    return knowledge_graph.get(node_id)

if __name__ == "__main__":
    mcp.run()
```

## Configuration in Claude Code

```json
// ~/.claude.json
{
  "mcpServers": {
    "my-server": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/server.py"],
      "env": {
        "API_KEY": "..."
      }
    }
  }
}
```

## Ecosystem

- **claude-mcp**: Official Python SDK
- **@modelcontextprotocol/sdk**: Official TypeScript SDK
- **FastMCP**: Higher-level Python framework (decorator-based)
- Server registry: hundreds of community servers (GitHub, filesystem, databases, etc.)

## Key Design Decisions

1. **LLM-controlled tool invocation** — model decides when to call tools based on descriptions
2. **Capability negotiation** — server declares what it supports; client adapts
3. **Stateless by default** — each request is independent; sessions are optional
4. **JSON-RPC 2.0 underneath** — well-understood message format
5. **Separation of concerns** — tools, resources, and prompts are distinct primitives

## Related Links

[[relatedTo::MCP Server Architecture]]
[[relatedTo::Claude Code MCP Configuration Pattern]]
[[relatedTo::MCP Multi-Project Configuration for VS Code]]
[[relatedTo::MCP Request Context Security Pattern]]
[[relatedTo::FastMCP Server Pattern]]
[[relatedTo::Ollama Infrastructure for Embeddings]]
[[relatedTo::Weaviate MCP Server]]
