---
title: CodeSage-Large-v2
type: model
tags: [model, embedding, code-embedding, salesforce, gpu, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-04-27T18:30:00Z
status: active
---

## Overview

CodeSage-Large-v2 is a code-embedding encoder model from Salesforce, the v2 iteration of the CodeSage family introduced in *Code Representation Learning At Scale* (Zhang et al.). It produces 2048-dimensional embeddings tuned for source-code retrieval and similarity tasks across multiple programming languages. The orchestrator uses it as the **primary code embedder** when a CUDA-capable GPU is available.

## Footprint

| Variant | Parameters | Embedding dim |
|---|---|---|
| CodeSage-v2-Small | 130M | 1024 |
| CodeSage-v2-Base | 356M | 1024 |
| **CodeSage-Large-v2** | 1.3B | 2048 |

CodeSage-Large-v2 is GPU-only in practice — at 1.3B parameters with a transformer encoder it is too slow on CPU for interactive code-graph indexing. The orchestrator does not ship a pre-built quantization; the model loads via `sentence-transformers` in fp16/fp32 directly from Hugging Face.

## Where the orchestrator uses it

- **Code embedding service**: `claude_mcp_servers/code_embedding_service/server.py` is a small FastAPI server that wraps CodeSage-Large-v2 via `sentence-transformers`. It listens on port 11438 and exposes `/health` and `/embed` endpoints.
- **Backend selector**: the service reads `CODE_EMBED_BACKEND` (`gpu` for CodeSage, `ollama` for the jina fallback), `CODE_EMBED_MODEL` (default `codesage/codesage-large-v2`), and `CODE_EMBED_DEVICE` (default `cuda` if available).
- **Weaviate code-graph collections**: `CodeFunction`, `CodeClass`, `CodeModule`, `CodeAPI`, `CodeInteraction` register the named vector `codesage_embed` (2048-dim) sourced from this service.
- **Install bootstrap**: `install.py` defaults to the GPU CodeSage path on hosts that pass the GPU detection check; CPU-only hosts switch to the jina fallback automatically.
- **Compose**: `infrastructure/docker-compose.gpu.yml` ships an optional GPU compose service for the code-embedding container with `CODE_EMBED_MODEL: "codesage/codesage-large-v2"`.

## Why this model

CodeSage-v2 is an open-source code embedder that publishes competitive numbers against OpenAI's `text-embedding-3-large` on the standard code-retrieval benchmarks the authors evaluate (CoSQA, AdvTest, language-specific retrieval). Apache 2.0 licensing means it can be redistributed as part of the orchestrator's bundled defaults. The 2048-dim output gives more headroom than the 768-dim jina fallback for fine-grained code retrieval.

## License

Apache 2.0 (per the Hugging Face model card for `codesage/codesage-large-v2`).

## Sources

- [Hugging Face — codesage/codesage-large-v2](https://huggingface.co/codesage/codesage-large-v2)
- *Code Representation Learning At Scale*, Zhang et al. (referenced from the model card; arXiv:2402.01935)
