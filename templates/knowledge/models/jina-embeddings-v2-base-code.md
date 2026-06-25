---
title: Jina Embeddings v2 Base Code
type: model
tags: [model, embedding, code-embedding, ollama, jina, cpu, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

## Overview

`jina-embeddings-v2-base-code` is a code-and-English embedding model from Jina AI. It is the orchestrator's **CPU fallback** for code embeddings on hosts without a CUDA GPU. The model is a JinaBert variant (~161M parameters) supporting 8192-token sequences, trained on the github-code dataset plus Jina's collection of 150M+ code question/answer and docstring pairs across English and 30 widely-used programming languages.

## Footprint

| Tag | Parameters | Embedding dim | Context |
|---|---|---|---|
| `unclemusclez/jina-embeddings-v2-base-code:latest` (Ollama) | 161M | 768 | 8K |

At 161M params the model runs fast enough on CPU for incremental code-graph indexing, which is the use case the fallback path targets. The Ollama tag is a community re-pack of the official Jina release and is what `install.py` pulls.

## Where the orchestrator uses it

- **CPU fallback for code embeddings**: when the host has no CUDA GPU, `install.py` selects the ollama-text + ollama-code preset, which pulls `unclemusclez/jina-embeddings-v2-base-code:latest` and points the code-embedding service at it via `CODE_EMBED_BACKEND=ollama` + `CODE_EMBED_MODEL=unclemusclez/jina-embeddings-v2-base-code:latest`.
- **`ollama_code_embed` named-vector slot**: Weaviate code collections register the named vector `ollama_code_embed` (768-dim) for jina-embedded entries, alongside the `codesage_embed` (2048-dim) slot — same dual-vector pattern as the text side.
- **Compose**: `infrastructure/docker-compose.yml` defaults the `code_embed` container to `CODE_EMBED_BACKEND=gpu`; CPU-only machines set `CODE_EMBED_BACKEND=ollama` (with `CODE_EMBED_MODEL=unclemusclez/jina-embeddings-v2-base-code:latest`) in `.env` to route the container at the jina backend instead of building the CUDA image.

## Why this model for the fallback

It is one of the few open code embedders small enough to run usefully on CPU while still being trained on a code-aware objective (rather than reusing a generic text embedder). The 8192-token context is long enough for most function- and module-sized chunks. Apache 2.0 licensing is consistent with the rest of the bundle.

## License

Apache 2.0 (per the Hugging Face model card for `jinaai/jina-embeddings-v2-base-code`).

## Sources

- [Hugging Face — jinaai/jina-embeddings-v2-base-code](https://huggingface.co/jinaai/jina-embeddings-v2-base-code)
- [Ollama community tag — unclemusclez/jina-embeddings-v2-base-code](https://ollama.com/unclemusclez/jina-embeddings-v2-base-code)
