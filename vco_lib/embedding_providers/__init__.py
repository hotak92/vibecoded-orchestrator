# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Backend adapters for vco_lib.embedding_service.

Each adapter knows how to talk to ONE embedding backend over HTTP:

- :mod:`vco_lib.embedding_providers.ollama` — Ollama local LLM
  (`http://localhost:11435/api/embed` with `/api/embeddings` fallback for
  pre-v0.4 Ollama). Handles both text models (qwen3-embedding,
  snowflake-arctic-embed2, mxbai-embed-large) and the CPU fallback for
  code (qwen3-embedding when GPU CodeEmbed isn't available).
- :mod:`vco_lib.embedding_providers.codeembed` — CodeEmbed FastAPI
  service (`http://localhost:11440/embed`). GPU-accelerated CodeSage-
  Large-v2 or any sentence-transformers code-embedding model. Probed
  via `/health` for the loaded model + dim.
- :mod:`vco_lib.embedding_providers.openai` — OpenAI hosted API
  (`https://api.openai.com/v1/embeddings`). Validation probe is
  `GET /v1/models/<model>` (free, per OpenAI docs).

All adapters return ``list[float]`` for single-item calls and
``list[list[float]]`` for batched calls, in the same order as the input.
None of them retry — retry/circuit-breaker logic lives in
:class:`vco_lib.embedding_service.EmbeddingService`. Adapters raise
``RuntimeError`` (or a subclass) with a human-readable message on
unrecoverable HTTP errors.

All adapters take an injected ``requests.Session`` so callers can pool
connections across many calls (e.g. a re-indexing loop). They do NOT
own the session — the caller is responsible for ``close()``.
"""

from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter
from vco_lib.embedding_providers.ollama import OllamaAdapter
from vco_lib.embedding_providers.openai import OpenAIAdapter

__all__ = ["CodeEmbedAdapter", "OllamaAdapter", "OpenAIAdapter"]
