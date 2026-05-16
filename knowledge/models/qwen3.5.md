---
title: Qwen3.5
type: model
tags: [model, llm, vlm, multimodal, ollama, qwen, alibaba, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T03:53:39Z
status: active
---

## Overview

Qwen3.5 is a family of open-source multimodal foundation models released by Alibaba's Qwen team. It is a unified text + vision model — the same checkpoint handles both modalities through early-fusion training on multimodal tokens, replacing the separate Qwen3-VL line. The family ships in dense and sparse Mixture-of-Experts variants from 0.8B up to 122B parameters, all sharing a 256K context window.

## Variants and footprint

The orchestrator targets the dense `qwen3.5:9b` tag as its default text + vision model:

| Tag | File size | VRAM (q4_K_M, ctx 8K) | Context | Modalities |
|---|---|---|---|---|
| `qwen3.5:0.8b` | 1.0 GB | ~2 GB | 256K | Text, Image |
| `qwen3.5:2b` | 2.7 GB | ~4 GB | 256K | Text, Image |
| `qwen3.5:4b` | 3.4 GB | ~5 GB | 256K | Text, Image |
| `qwen3.5:9b` | 6.6 GB | ~9 GB | 256K | Text, Image |
| `qwen3.5:27b` | 17 GB | ~20 GB | 256K | Text, Image |
| `qwen3.5:35b` | 24 GB | ~26 GB | 256K | Text, Image |
| `qwen3.5:122b` | 81 GB | (workstation) | 256K | Text, Image |

VRAM figures are approximate practical floors at default quantization with a small KV cache. Context window is the model's documented maximum; effective context in Ollama depends on `OLLAMA_KV_CACHE_TYPE` and available VRAM.

## Where the orchestrator uses it

- **KG-summary generation**: `generate-kg-summary.py` targets `qwen3.5:9b` on capable hardware (≥24 GB RAM or ≥7.5 GB VRAM) as a fallback when the Claude CLI is unavailable.
- **Install bootstrap**: `install.py` pulls `qwen3.5:9b` for KG-summary generation on capable machines + `gemma4:e4b` as the low-power fallback.
- **Note (v0.2.11)**: the Ollama MCP (`read_image`, `chat`) was removed. `qwen3.5:9b`'s vision and inference capabilities are no longer exposed as MCP tools — Claude's native capabilities handle those use cases directly. Ollama continues running at `http://localhost:11435` as infrastructure for Weaviate embeddings.

## Why this model

Qwen3.5 replaces two separate models (Qwen3 text + Qwen3-VL) with a single unified checkpoint, cutting disk footprint roughly in half for projects that need both. The 256K context fits long documents and code files without RAG plumbing for many cases. Apache 2.0 licensing matches the rest of the orchestrator's local-model stack.

## License

Apache 2.0 (per the official model cards; verify the exact license tag on the Hugging Face model card before redistribution).

## Sources

- [Ollama library — qwen3.5](https://ollama.com/library/qwen3.5)
- Qwen team release notes via the Ollama library README
