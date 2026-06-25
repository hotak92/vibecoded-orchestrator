---
title: FastMCP
type: tool
tags: [MCP, python, framework, LLM, tool-calling, server, API]
created: 2026-02-26T00:00:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# FastMCP

## Overview

FastMCP is the standard Python framework for building Model Context Protocol (MCP) servers and clients. Version 1.0 was incorporated into the official MCP Python SDK in 2024. The actively maintained standalone project (now at 2.x/3.x) is downloaded approximately 1 million times per day and powers approximately 70% of MCP servers across all languages.

FastMCP is maintained by Prefect and documented at `gofastmcp.com`.

## Core Concepts

### Minimal Server

```python
from fastmcp import FastMCP

mcp = FastMCP("My Server")

@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b

if __name__ == "__main__":
    mcp.run()
```

That's it — FastMCP auto-generates the JSON schema, validation, and MCP protocol boilerplate from the Python type annotations and docstring.

### Three Pillars

1. **Servers** — Expose tools, resources, and prompts to LLMs
2. **Apps** — Give tools interactive UIs rendered in conversation (FastMCP 3.x)
3. **Clients** — Connect to any MCP server (local or remote, programmatic or CLI)

## Key Features

### Tool Declaration
- Decorator-based: `@mcp.tool`
- Schema auto-generated from Python type hints
- Docstrings become tool descriptions
- Validation via Pydantic

### Resources
- Expose data as readable resources: `@mcp.resource("file://...")`
- Dynamic resources with URI templates

### Prompts
- Reusable prompt templates: `@mcp.prompt`

### Transport Support
- stdio (default; used by Claude Code extension)
- HTTP with SSE (Server-Sent Events)
- WebSocket

### Authentication (FastMCP 2.x+)
- OAuth 2.0 support
- API key middleware
- Per-request authorization

### Response Caching (FastMCP 3.x)
- Middleware for expensive operations
- Configurable TTL

### Server Lifespans
- Proper initialization and cleanup hooks
- Database connection management patterns

## FastMCP vs Raw MCP SDK

| Feature | FastMCP | Raw Python MCP SDK |
|---|---|---|
| Schema generation | Automatic from type hints | Manual JSON schema |
| Validation | Built-in (Pydantic) | Manual |
| Boilerplate | Minimal | Significant |
| Transport abstraction | Yes | Partial |
| Client support | Yes | Partial |
| Production features | Auth, caching, lifespan | Basic |

## Async Pattern

```python
from fastmcp import FastMCP
import aiohttp

mcp = FastMCP("Async Server")

@mcp.tool
async def fetch_data(url: str) -> str:
    """Fetch data from URL"""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()
```

## Installation

```bash
pip install fastmcp
# or
uv add fastmcp
```

## Links

[[implements::MCP Server Architecture]]
[[implements::MCP Protocol]]
[[relatedTo::Claude Code MCP Configuration Pattern]]
[[relatedTo::FastAPI]]
[[relatedTo::Weaviate]]
