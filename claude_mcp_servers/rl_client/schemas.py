# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pydantic wire-contract schemas for the RL reranker HTTP API.

These models pin the request/response shape that
``paid-modules/vct-rl-reranker/rl_server.py`` (the paid container)
must accept. If a field name changes here, the same change MUST land
in the server's handlers — otherwise the launcher's free-tier
plumbing will silently fail to talk to the paid module.

The wire contract is derived directly from the server source as of
2026-05-19:

  * ``POST /cache_nodes`` — body: ``{task_id, query, nodes[], limit,
    embedding_source?}``. Response: ``{ok, task_id, top_k[]}``.
    ``nodes`` items have ``{title, content?, node_type?, emb?,
    score?, weaviate_id?, ...}`` — free-form dicts on the server
    side (it forwards them to ``RetrievalRL.rerank_query_aware``).
  * ``POST /rl_update`` — body: ``{task_ids[], agent_output}``.
    Response: ``{ok, scheduled}`` or ``{ok, skipped}``.
  * ``GET /health`` — response: ``{ok, model}``.

The shape is intentionally permissive (extra fields are tolerated)
so we can evolve the wire contract without breaking older clients
or servers in lockstep.
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class NodeInput(BaseModel):
    """One over-fetched node forwarded to ``/cache_nodes`` for reranking.

    Required fields:
        title: Node title (used for citation matching downstream).
        score: Base score from Weaviate (cosine distance / hybrid score).

    Optional fields:
        weaviate_id: Stable UUID for cross-call dedup.
        emb: Frozen embedding vector for offline-train consistency.
        content: Node body (server may use it for cross-encoder rerank).
        node_type: KG node type (concept / project / tool / ...).
        Other fields: tolerated and passed through (server uses extras).
    """

    model_config = ConfigDict(extra="allow")

    title: str
    score: float = 0.0
    weaviate_id: Optional[str] = None
    emb: Optional[List[float]] = None
    content: Optional[str] = None
    node_type: Optional[str] = None


class CacheNodesRequest(BaseModel):
    """Request body for ``POST /cache_nodes``.

    Mirrors the keys read by
    ``RLServer._cache_nodes`` (see ``rl_server.py``).
    """

    model_config = ConfigDict(extra="allow")

    task_id: str
    query: str
    nodes: List[NodeInput] = Field(default_factory=list)
    limit: int = 5
    # Optional metadata for cross-process drift detection. The server
    # logs these but does not switch networks per-request.
    embedding_source: Optional[str] = None
    query_emb: Optional[List[float]] = None
    active_embedding: Optional[str] = None


class RankedNode(BaseModel):
    """One node in the server's reranked ``top_k`` response.

    The server returns the same dict shape it received (with possibly
    updated scores + new ``rank``), so we keep this permissive.
    """

    model_config = ConfigDict(extra="allow")

    title: str = ""
    score: float = 0.0
    weaviate_id: Optional[str] = None
    rank: Optional[int] = None


class CacheNodesResponse(BaseModel):
    """Response body for ``POST /cache_nodes``."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    task_id: str = ""
    # Server returns plain dicts; we keep them as Any so callers can
    # forward them through the existing weaviate_mcp result pipeline
    # unchanged. Tests + the client adapter shape-check selectively.
    top_k: List[Any] = Field(default_factory=list)
    error: Optional[str] = None


class RLUpdateRequest(BaseModel):
    """Request body for ``POST /rl_update``.

    Note the plural ``task_ids`` — the server batches updates for
    multiple retrieval events that all reference the same agent
    output (e.g. one chat turn that emitted two KG searches).
    """

    model_config = ConfigDict(extra="allow")

    task_ids: List[str]
    agent_output: str
    task_type: Optional[str] = None


class RLUpdateResponse(BaseModel):
    """Response body for ``POST /rl_update``."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    scheduled: Optional[int] = None
    skipped: Optional[str] = None
    citations_detected: Optional[int] = None
    trained: Optional[bool] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    """Response body for ``GET /health``.

    When the client falls back to "disabled mode" (no container env
    configured) it synthesises ``HealthResponse(ok=False,
    model="disabled")`` rather than performing an HTTP call.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool = False
    model: str = "unknown"
