---
title: Snowflake Arctic Embed 2.0
type: model
tags: [model, embedding, text-embedding, ollama, snowflake, multilingual, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

## Overview

Snowflake Arctic Embed 2.0 is a multilingual text-embedding model from Snowflake, the second iteration of the Arctic Embed family. The orchestrator uses it in two roles: as the **active** text embedder of the low-resource install profile (`ACTIVE_EMBEDDING=arctic`, the smallest-footprint 1024-dim option for low-RAM/low-VRAM hosts), and as a compatibility slot on collections embedded with it before a project switched to Qwen3 Embedding. The model is sub-1B parameters and was designed for enterprise retrieval at high throughput.

## Footprint

| Tag | Parameters | Embedding dim | File size | Context |
|---|---|---|---|---|
| `snowflake-arctic-embed2:latest` (alias `568m`) | 568M | 1024 | 1.2 GB | 8K |

Matryoshka Representation Learning (MRL) is supported by the model, but the orchestrator pins to the full 1024-dim output so vectors match the schema declared on existing Weaviate collections.

## Where the orchestrator uses it

- **`ollama_embed` named-vector slot**: `claude_mcp_servers/weaviate_mcp/server.py` reads `LEGACY_TEXT_EMBEDDING_MODEL` (default `snowflake-arctic-embed2:latest`) and registers the named vector `ollama_embed` (1024-dim) on KG and document collections, alongside the `qwen3_embed` slot. On the arctic profile this is the slot new content is written to; on a qwen3 project, entries embedded with Snowflake remain searchable through it.
- **Active embedder on the low-resource profile**: the `low_resource` install preset in `install.py` sets `text_model: snowflake-arctic-embed2:latest`, `text_dims: 1024`, and `active_embedding: arctic`. `_kg_backend_for_model` and `_normalise_embedding_alias` map the `arctic` alias to `snowflake-arctic-embed2:latest`. The hardware selector also picks arctic on hosts with ≤8 GB VRAM.
- **Chunk-size budget**: `claude_mcp_servers/weaviate_mcp/chunking.py` sets a 4096-token chunk budget for `snowflake-arctic-embed2` (the `:latest` and `:568m` tags share it). The model documents an 8K context window; the 4k budget keeps chunk granularity tuned for retrieval on low-VRAM hosts.
- **Install bootstrap**: `install.py` pulls `snowflake-arctic-embed2:latest` for the low-resource preset, paired with the Jina V2 code embedder.

## Role on a project

On the arctic profile it is the project's primary text embedder. On a qwen3 project, keeping it as a second named vector lets the orchestrator search arctic-embedded entries with their original embedding while indexing new content with Qwen3; `migrate_to_new_embeddings.py` consolidates a project onto one slot.

## License

Apache 2.0 (per the Hugging Face model card for `Snowflake/snowflake-arctic-embed-l-v2.0`).

## Sources

- [Ollama library — snowflake-arctic-embed2](https://ollama.com/library/snowflake-arctic-embed2)
- [Hugging Face — Snowflake/snowflake-arctic-embed-l-v2.0](https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0)
