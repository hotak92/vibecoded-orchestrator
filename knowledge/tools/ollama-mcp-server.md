---
title: Ollama MCP Server
type: tool
tags:
- tool
- MCP
- ollama
- local-llm
- document-processing
- AI
- python
created: 2026-01-28 19:00:00+00:00
updated: 2026-04-05T14:34:54Z
status: active
---

# Ollama MCP Server

FastMCP-based server providing local LLM inference through Ollama, with specialized tools for document processing without consuming Claude API tokens.

## Overview

Shared MCP server providing FREE local document processing and information extraction.

**Architecture**: Runs as stdio subprocess via Python
**Port**: Stdio (MCP protocol)
**Models**: qwen3.5:0.8b (fast, default for chat), qwen3.5:9b (8B, default for read_document)

## Tools

### chat
Local LLM inference — FREE, runs on-device.

**Use Cases**:
- Quick analysis, rewrites, reasoning tasks without consuming Claude API tokens
- Rewrite docstrings, summarize code, evaluate options

**Parameters**:
- `prompt` (required): The prompt to send
- `model` (optional): default `qwen3.5:0.8b` (fast); use `qwen3.5:9b` (8B) for complex reasoning
- `system_prompt` (optional): System context
- `temperature` (optional): Sampling temperature
- `max_tokens` (optional): Max output tokens

**Example**:
```python
chat("Rewrite this docstring to be clearer: [docstring]", model="qwen3.5:0.8b")
```

### read_document
Summarize or extract specific information from files using local LLM — FREE. Auto-switches to chunked scanning mode for large files (>100K chars).

**Use Cases**:
- Summarize large documents without Claude API tokens
- Extract specific info (use `task` param)
- Analyze document structure

**Parameters**:
- `file_path` (required): Absolute path to document
- `model` (optional): default `qwen3.5:9b` (8B); use `qwen3.5:0.8b` for speed
- `task` (optional): Processing instruction, e.g.:
  - `"summarize"` (default)
  - `"find the authentication logic"`
  - `"extract all API endpoints and their parameters"`
  - `"extract_key_points"`
- `context_lines` (optional): Lines per chunk for large-file scanning (default: 50)

**Returns**: JSON with `result` (processed output), `model_used`, `tokens_per_sec`, `duration_sec`

**Example**:
```python
read_document("/path/to/large_spec.md", task="find all error codes and their meanings")
read_document("/path/to/doc.md")  # default: summarize
```

## Models

### qwen3.5:9b (8.2B) - Default
**Size**: 8.2B parameters
**Use**: General purpose processing
**Speed**: ~50 tokens/sec
**Best For**: Summaries, extraction, general Q&A

### qwen3.5:0.8b - Fast inference
**Use for**: Simple tasks where speed matters (quick chat, short analysis).
**Avoid for**: Complex reasoning, long documents.

## Benefits

**Cost**: FREE (local inference, no API calls)
**Privacy**: Documents never leave local system
**Speed**: Fast enough for interactive use
**Context**: No context window limits (processes in chunks)

## Limitations

**File Size**: Max 100K characters per document (safety limit)
**Accuracy**: Local models less capable than Claude Sonnet
**Quality**: Best for extraction/summarization, not complex reasoning
**Hardware**: Requires sufficient VRAM (8GB+ recommended)

## Server Management

The MCP server runs as a stdio subprocess started by Claude Code — it is NOT a container.
Restart by reloading Claude Code MCP configuration.

## Implementation

**Framework**: FastMCP
**Language**: Python 3.12
**Dependencies**: requests, fastmcp

## Related

- [[Ollama]] (inference engine)
- [[FastMCP]] (MCP framework)
- [[Weaviate MCP Server]] (embeddings via Ollama)
- [[MCP Request Context Security]] (security patterns)

## History

- **2026-03-12**: Consolidated to 2 tools: `chat` + `read_document`. Auto-switches to chunked-scan mode for large files.
- **2026-01-19**: Enhanced with read_document tool
- **2026-01-19**: Updated to use correct models (qwen3.5:9b default)
- **2026-01-14**: Initial implementation
