---
title: Snowflake Arctic Embed 2.0
type: model
tags: [model, embedding, text-embedding, ollama, snowflake, multilingual, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-04-27T18:30:00Z
status: active
---

## Overview

Snowflake Arctic Embed 2.0 is a multilingual text-embedding model from Snowflake, released as the second iteration of the Arctic Embed family. It is the orchestrator's **legacy** text embedder, kept for backward compatibility with collections embedded before the migration to Qwen3 Embedding. The model is sub-1B parameters and was designed for enterprise retrieval at high throughput.

## Footprint

| Tag | Parameters | Embedding dim | File size | Context |
|---|---|---|---|---|
| `snowflake-arctic-embed2:latest` (alias `568m`) | 568M | 1024 | 1.2 GB | 8K |

Matryoshka Representation Learning (MRL) is supported by the model, but the orchestrator pins to the full 1024-dim output so vectors match the schema declared on existing Weaviate collections.

## Where the orchestrator uses it

- **Legacy text embedder slot**: `claude_mcp_servers/weaviate_mcp/server.py` reads `LEGACY_TEXT_EMBEDDING_MODEL` (default `snowflake-arctic-embed2:latest`) and registers the named vector `ollama_embed` (1024-dim) on KG and document collections, alongside the active `qwen3_embed` slot. Existing entries embedded with Snowflake remain searchable through this named vector without re-embedding.
- **Chunk-size budget**: `claude_mcp_servers/weaviate_mcp/chunking.py` uses an explicit 2048-token chunk budget for `snowflake-arctic-embed2` (tested working) — this is the model's effective practical limit despite the documented 8K context window.
- **Install bootstrap**: `install.py` lists `snowflake-arctic-embed2:latest` only in the legacy ollama-only preset; new installs default to Qwen3 Embedding.

## Why kept around

Forcing a re-embed of every existing collection on upgrade is expensive and risky. Keeping Snowflake as a second named vector lets the orchestrator search legacy entries with their original embedding while indexing new content with Qwen3. The migration script (`migrate_to_new_embeddings.py`) is the explicit path forward when a project wants to consolidate.

## License

Apache 2.0 (per the Hugging Face model card for `Snowflake/snowflake-arctic-embed-l-v2.0`).

## Sources

- [Ollama library — snowflake-arctic-embed2](https://ollama.com/library/snowflake-arctic-embed2)
- [Hugging Face — Snowflake/snowflake-arctic-embed-l-v2.0](https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0)
