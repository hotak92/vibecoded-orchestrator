# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
RL reranker — the server, model and trainer behind the ``vct-rl-reranker``
module.

Part of the orchestrator since v0.2.106 (it was a separately distributed,
licence-gated module before). It runs as a container built locally from this
directory (``Dockerfile``); the MCP side talks to it through
``claude_mcp_servers/rl_client``. The trained weights it serves are bundled in
``weights/`` (layout: ``bundled_weights.py``).

Public surface:
- RLServer       — aiohttp server exposing /cache_nodes + /rl_update and the
                   model-management endpoints (rl_server.py)
- RetrievalRL    — scores candidates from the vectors the client sends
                   (``rl_client.rerank_wire``) and trains online on citations
- RLDataLogger   — append-only JSONL logger for retrieval + citation events
                   (imported from ``rl_client.rl_logger`` — one copy)
- RLModel        — PyTorch model wrapper (per-embedding-source tag/dim
                   resolved at startup from ACTIVE_EMBEDDING)
- offline_trainer — batch trainer over launcher.db events read through the hub
- bundled_weights — the shipped-weights manifest reader
- training/      — offline export, shard trainer and evaluation harness for
                   the shipped weights (maintainer/volunteer side; not in the
                   image)

Per-embedding-source policy: each TEXT embedding source (qwen3, arctic,
openai, future text embedders) gets its own RLModel weights file to avoid
catastrophic interference. RLServer.__init__ derives the (tag, dim) pair from
ACTIVE_EMBEDDING; offline_trainer consumes only events matching the target
source's ``embedding_source`` field.

KG-only scope: this package reranks knowledge-graph results. Code-graph
reranking is not part of it.

Importing this package imports nothing: the image's build stage runs
``python -m rl_reranker.bundled_weights`` before torch is installed.
"""
