---
title: Qwen3 Embedding
type: model
tags: [model, embedding, text-embedding, ollama, qwen, alibaba, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

## Overview

Qwen3 Embedding is Alibaba's text-embedding family built on the dense Qwen3 foundation models, released in the Qwen3 series and shipped through Ollama and Hugging Face. The Qwen team reports that the 8B variant ranked #1 on the MTEB multilingual leaderboard at release (June 5, 2025, score 70.58). The orchestrator uses the smallest variant, `qwen3-embedding:0.6b`, as its primary text embedder.

## Variants and footprint

| Tag | Parameters | Embedding dim | File size | Context |
|---|---|---|---|---|
| `qwen3-embedding:0.6b` | 0.6B | up to 1024 | 639 MB | 32K |
| `qwen3-embedding:4b` | 4B | up to 2560 | 2.5 GB | 40K |
| `qwen3-embedding:8b` (latest alias) | 8B | up to 4096 | 4.7 GB | 40K |

Qwen3 Embedding supports user-defined output dimensions per request (Matryoshka-style truncation) — the orchestrator pins to 1024 dims to match the legacy Snowflake schema. The 0.6b variant requires `num_ctx=8192` to surface its full embedding quality; the orchestrator's wrapper sets this explicitly because Ollama's default is too small.

## Where the orchestrator uses it

- **Active text embedder**: `claude_mcp_servers/weaviate_mcp/server.py` reads `EMBEDDING_MODEL` (default `qwen3-embedding:0.6b`) and registers the named vector `qwen3_embed` (1024-dim) on each KG/document collection. All knowledge-graph and document indexing flows through this embedder by default.
- **Install bootstrap**: `install.py` lists `qwen3-embedding:0.6b` in every embedding-backend preset (gpu, ollama-text+gpu-code, ollama-only) and pulls it via `ollama pull` during the embedding-bootstrap phase.
- **Re-embed migrations**: `claude_mcp_servers/scripts/migrate_to_new_embeddings.py` re-vectorizes existing collections into the `qwen3_embed` named-vector slot from the legacy `ollama_embed` slot.

## Why this model

Higher MTEB score in the Qwen team's reported benchmarks and a roadmap aligned with the Qwen family the orchestrator already targets for inference. It produces 1024-dim vectors, schema-compatible with the `snowflake-arctic-embed2` slot, so a project can carry both named vectors on the same collection and search either. `snowflake-arctic-embed2` occupies the `ollama_embed` named-vector slot; qwen3-embedding occupies `qwen3_embed` and is the default active embedder.

## License

Apache 2.0 (per the Hugging Face model card for Qwen3-Embedding-0.6B and the rest of the family).

## Sources

- [Ollama library — qwen3-embedding](https://ollama.com/library/qwen3-embedding)
- [Hugging Face — Qwen/Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- MTEB multilingual leaderboard reference from the official Qwen3 Embedding model card
