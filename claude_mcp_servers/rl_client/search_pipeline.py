# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Canonical rerank-and-emit pipeline for KG retrieval — V52-J (v0.2.52).

Every KG-search entry point (MCP ``hybrid_search``, MCP
``semantic_graph_search``, the CLI scripts ``rl_kg_search.py`` and
``search_knowledge.py``, PreToolUse hooks via those CLIs) shares the
same rerank + cache + emit logic. Pre-v0.2.52 this logic lived inside
``weaviate_mcp.server._rl_cache_and_rerank`` and was reached by ad-hoc
imports from CLI scripts that reached INTO the MCP server module — an
awkward dependency direction that made it hard for new entry points to
participate (`templates/scripts/search_knowledge.py` historically did
not, producing zero telemetry on every kg-search CLI call).

This module exposes one public function, ``rerank_and_emit()``, plus a
small frozen-dataclass request shape ``RerankRequest`` and a response
shape ``RerankResult``. Callers supply already-fetched candidates (so
the pipeline stays orthogonal to whether the search was BM25-hybrid,
pure vector, with graph traversal, or a CLI near_vector); the pipeline
owns:

  1. License-tier gate (``feature_enabled("rl_retrieval")``) — free
     tier skips the rerank-RPC but still emits telemetry.
  2. Per-project enable toggle (hub-resolved
     ``rl_reranker_enabled_for_project``) — owners can opt one project
     out without revoking the license.
  3. RL rerank RPC via the cached ``RLClient`` (one client per
     (active_embedding, project_id) — keyed so the
     ``X-VCT-Project-ID`` header routes to the right per-project model
     head in vct-rl-reranker v0.2.10+).
  4. Citation-cache population (``_rl_node_content_cache`` in
     server.py) so the answer monitor can compute citations later
     without re-fetching from Weaviate.
  5. Answer-monitor task spawn (``_rl_answer_monitor``) — V52-N
     accumulates Claude's answer until token threshold OR compaction
     OR safety-valve timeout, then emits the citation event.
  6. Retrieval telemetry emit via the canonical ``emit_rl_event``
     (telemetry_emit.py) with the 3-layer session_id resolution + the
     fixed project_id propagation.

Lazy imports of ``weaviate_mcp.server`` symbols at call time keep the
direction defensible: this module is in ``rl_client`` (alongside the
RLClient + writer it orchestrates); server.py owns the MCP-tool-shape
concerns and the module-level globals
(``_rl_node_content_cache``, ``_rl_monitor_tasks``, ``_rl_call_seq``).
Reaching into server.py at call time mirrors the existing
``rl_kg_search.py`` pattern but routes through a single canonical
chokepoint instead of duplicating logic across entry points.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .telemetry_emit import (
    EmitValidationError,
    RetrievalEvent,
    emit_rl_event,
    new_task_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankRequest:
    """Inputs for one rerank-and-emit pass.

    ``candidates`` must be the already-fetched, already-merged list of
    node dicts (the caller did the Weaviate fan-out). Each dict needs at
    minimum ``title`` + ``score``; the pipeline preserves whatever
    enrichment fields the caller attached (``n_emb``, ``linked_embs``,
    ``cos_qn`` / ``cos_ql`` / ``cos_nl``, ``node_type``, ``links``) so
    the v3 retrieval-event payload carries them downstream.
    """

    query: str
    candidates: list[dict[str, Any]]
    limit: int
    # ---- embedding metadata (mandatory for v3 telemetry) ----
    query_emb: Optional[list[float]] = None
    embedding_source: str = ""
    embedding_dim: int = 0
    embedding_model: str = ""
    # ---- tracking ----
    task_id: Optional[str] = None
    task_type: str = "mcp_interactive"
    session_id: Optional[str] = None
    # ---- failure path ----
    failure_mode: Optional[str] = None
    failed_collections: list[str] = field(default_factory=list)
    # ---- behaviour overrides ----
    spawn_answer_monitor: bool = True


@dataclass(frozen=True)
class RerankResult:
    """Output of one rerank-and-emit pass."""

    ranked: list[dict[str, Any]]      # top-k after rerank (or Weaviate order if RL skipped)
    task_id: str                       # echo of req.task_id (or generated one)
    rl_used: bool                      # diagnostic — did the rerank RPC actually run?
    emit_success: bool                 # diagnostic — did emit_rl_event return True?


async def rerank_and_emit(req: RerankRequest) -> RerankResult:
    """Rerank candidates via RL + emit retrieval telemetry. Canonical.

    Soft-fail throughout — a missing RL container, broken telemetry hub,
    or invalid emit payload never breaks the user-facing KG search.
    The caller's ``candidates`` list is always returned (in some form)
    so the user gets results even when the side-channel observability
    fails.
    """
    # ---- ensure task_id ----
    task_id = req.task_id or new_task_id()

    # ---- license tier gate + per-project enable toggle ----
    rl_enabled = _resolve_rl_enabled()

    # ---- rerank or pass through ----
    ranked: list[dict[str, Any]]
    rl_used = False
    if rl_enabled and req.candidates:
        ranked = await _do_rerank(
            query=req.query,
            candidates=req.candidates,
            limit=req.limit,
            task_id=task_id,
            session_id=req.session_id,
        )
        rl_used = ranked is not None
        if not rl_used:
            ranked = list(req.candidates[: req.limit])
    else:
        # Free tier OR empty candidates → return Weaviate order. Free
        # tier still benefits from the telemetry emit below so the
        # historical corpus accumulates.
        ranked = list(req.candidates[: req.limit])

    # ---- spawn answer monitor (citation accumulator) ----
    # V52-N: monitor accumulates Claude prose + tool inputs until ≥25K
    # tokens OR compaction OR safety-valve timeout, then writes the
    # citation event. Free tier still spawns — failure-rate telemetry
    # for upgrade-path users.
    if req.spawn_answer_monitor and req.candidates:
        try:
            _spawn_answer_monitor(task_id, req.query)
        except Exception as exc:
            logger.debug("rerank_and_emit: answer monitor spawn failed (%s)", exc)

    # ---- populate citation cache ----
    # The monitor consumes this cache when it eventually fires; storing
    # candidates here saves a Weaviate re-fetch at citation-write time.
    try:
        _populate_citation_cache(
            task_id=task_id,
            candidates=req.candidates,
            limit=req.limit,
            query_emb=req.query_emb,
            embedding_source=req.embedding_source,
            embedding_dim=req.embedding_dim,
            embedding_model=req.embedding_model,
            task_type=req.task_type,
        )
    except Exception as exc:
        logger.debug("rerank_and_emit: citation cache populate failed (%s)", exc)

    # ---- emit retrieval telemetry ----
    emit_success = False
    log_nodes = _build_log_nodes(req.candidates, req.limit)
    try:
        ev = RetrievalEvent(
            query=req.query,
            query_emb=req.query_emb or [],
            embedding_source=req.embedding_source,
            embedding_dim=req.embedding_dim,
            embedding_model=req.embedding_model,
            nodes=log_nodes,
            task_id=task_id,
            task_type=req.task_type,
            session_id=req.session_id,
            failure_mode=req.failure_mode,
            failed_collections=list(req.failed_collections),
        )
        emit_success = emit_rl_event(ev)
    except EmitValidationError as exc:
        # Surface as DEBUG, not WARN — caller-side missing fields are
        # noisy in degraded-mode emit paths (failure_mode set, query_emb
        # absent). Production paths should never hit this; if they do
        # the validation message names the field for fast triage.
        logger.debug("rerank_and_emit: emit validation failed (%s)", exc)
    except Exception as exc:
        logger.debug("rerank_and_emit: emit raised (%s)", exc)

    return RerankResult(
        ranked=ranked,
        task_id=task_id,
        rl_used=rl_used,
        emit_success=emit_success,
    )


# ---- internal helpers ----------------------------------------------


def _resolve_rl_enabled() -> bool:
    """License-tier + per-project toggle gate. Returns True iff the
    caller should hit the RL container for rerank.

    Two independent gates, both falling open on resolver errors:

      1. License tier — ``feature_enabled("rl_retrieval", module_id=
         "vct-rl-reranker")``. Free → False. Pro / MAO → True. The
         per-module overlay in ``~/.vibecoded/license_cache.json``
         lets a user explicitly activated the paid module key reach
         True even when the orchestrator tier is free.
      2. Per-project enable toggle — ``ProjectConfig.
         rl_reranker_enabled_for_project``. Owners can opt one project
         out via the launcher GUI's per-project Modules panel without
         revoking the license. Hub-down branch falls open (True): never
         silently disable a paying user's reranker because the hub
         crashed mid-session.
    """
    try:
        from VCThelpers.license import feature_enabled
        if not feature_enabled("rl_retrieval", module_id="vct-rl-reranker"):
            return False
    except ImportError:
        # VCThelpers not available (pure-free install) → no RL.
        return False
    except Exception as exc:
        logger.debug("_resolve_rl_enabled: license probe raised (%s); free tier", exc)
        return False
    # Per-project toggle. Soft-fail to enabled.
    try:
        from claude_mcp_servers.weaviate_mcp.server import _try_resolve_project_config

        cfg = _try_resolve_project_config()
        if cfg is not None and not getattr(cfg, "rl_reranker_enabled_for_project", True):
            return False
    except Exception as exc:
        logger.debug("_resolve_rl_enabled: per-project toggle probe raised (%s)", exc)
    return True


async def _do_rerank(
    *,
    query: str,
    candidates: list[dict[str, Any]],
    limit: int,
    task_id: str,
    session_id: Optional[str],
) -> Optional[list[dict[str, Any]]]:
    """Call RLClient.cache_nodes. Returns None on any failure so the
    caller falls back to Weaviate order.

    Note: RLClient's own ``cache_nodes`` swallows network errors
    internally — it returns Weaviate's input order on connection-
    refused / 5xx / disabled-mode. So None from here means a structural
    error (client unavailable, import broken) rather than transport
    failure.
    """
    try:
        from claude_mcp_servers.weaviate_mcp.server import _get_rl_client
    except Exception as exc:
        logger.debug("_do_rerank: cannot import _get_rl_client (%s)", exc)
        return None
    client = _get_rl_client()
    if client is None:
        return None
    # Pre-resolve session_id with the same 3-layer rule we use for
    # telemetry — keeps the X-VCT-Session-ID header on cache_nodes
    # consistent with what the telemetry envelope eventually carries.
    from .telemetry_emit import resolve_session_id

    resolved_session = resolve_session_id(session_id)
    try:
        return await client.cache_nodes(
            query=query,
            nodes=candidates,
            top_k=limit,
            task_id=task_id,
            session_id=resolved_session,
        )
    except Exception as exc:
        logger.debug("_do_rerank: cache_nodes raised (%s)", exc)
        return None


def _spawn_answer_monitor(task_id: str, query: str) -> None:
    """Schedule the citation answer-monitor as a background task.

    Lazy-imports ``_rl_answer_monitor`` + the module-level
    ``_rl_call_seq`` + ``_rl_monitor_tasks`` set from server.py so this
    module has zero static dependency on the MCP server.

    The monitor accumulates Claude's response until token threshold +
    compaction-sentinel (V52-N) then writes the citation event. We keep
    a strong ref in the module-level set so the GC cannot drop the task
    mid-poll; the done-callback discards on completion.
    """
    from claude_mcp_servers.weaviate_mcp import server as srv

    srv._rl_call_seq += 1
    seq = srv._rl_call_seq
    monitor = asyncio.create_task(srv._rl_answer_monitor(task_id, seq, query))
    srv._rl_monitor_tasks.add(monitor)
    monitor.add_done_callback(srv._rl_monitor_tasks.discard)


def _populate_citation_cache(
    *,
    task_id: str,
    candidates: list[dict[str, Any]],
    limit: int,
    query_emb: Optional[list[float]],
    embedding_source: str,
    embedding_dim: int,
    embedding_model: str,
    task_type: str,
) -> None:
    """Write the per-task entry the answer monitor reads at citation time.

    Same entry shape as the pre-v0.2.52 inline write at
    ``server.py:4763``. Bounded by ``_RL_NODE_CACHE_MAX``; LRU-ish
    eviction via insertion-order pop.
    """
    from claude_mcp_servers.weaviate_mcp import server as srv

    # Build the cache-stored node list (same shape as what the monitor
    # later reads). This MAY differ from the rerank output if the RL
    # client returned a re-ordered subset — by storing the full
    # over-fetched list we let the citation computation see candidates
    # the user-facing response truncated.
    cache_nodes = _build_log_nodes(candidates, limit)
    try:
        project_id_for_cache: Optional[str] = None
        try:
            _cfg = srv._try_resolve_project_config()
            if _cfg is not None:
                project_id_for_cache = getattr(_cfg, "project_id", None)
        except Exception:
            pass
        srv._rl_node_content_cache[task_id] = {
            "nodes": cache_nodes,
            "query_emb": list(query_emb) if query_emb else None,
            "active_model": embedding_model,
            "embedding_source": embedding_source,
            "embedding_dim": embedding_dim,
            "project_id": project_id_for_cache,
            "project_name": getattr(srv, "PROJECT_NAME", "") or "",
            "task_type": task_type,
        }
        # LRU bound — pop oldest insertion-order entry until size <= max.
        max_size = getattr(srv, "_RL_NODE_CACHE_MAX", 256)
        while len(srv._rl_node_content_cache) > max_size:
            srv._rl_node_content_cache.pop(next(iter(srv._rl_node_content_cache)))
    except Exception as exc:
        logger.debug("_populate_citation_cache: write failed (%s)", exc)


def _build_log_nodes(
    candidates: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Reduce per-node fields to what telemetry actually needs.

    Mirrors the pre-v0.2.52 reducer at ``server.py:4719``. Drops any
    Weaviate-internal cruft; preserves enrichment fields that the v3
    retrieval-event payload uses (``n_emb`` for unified-target training,
    ``linked_embs`` / ``linked_type_names`` for graph context,
    ``cos_*`` for similarity diagnostics).
    """
    out: list[dict[str, Any]] = []
    for idx, n in enumerate(candidates):
        if not isinstance(n, dict):
            continue
        rec: dict[str, Any] = {
            "title": n.get("title", ""),
            "score": n.get("score", 0.0),
            "tier": "top_k" if idx < limit else "extra_reference",
        }
        if n.get("emb"):
            rec["emb"] = n["emb"]
        if n.get("n_emb"):
            rec["n_emb"] = n["n_emb"]
        if n.get("linked_embs"):
            rec["linked_embs"] = n["linked_embs"]
        if n.get("linked_type_names"):
            rec["linked_type_names"] = n["linked_type_names"]
        if n.get("node_type"):
            rec["node_type"] = n["node_type"]
        if n.get("links"):
            rec["links"] = n["links"]
        for cos_field in ("cos_qn", "cos_ql", "cos_nl"):
            val = n.get(cos_field)
            if val is not None:
                rec[cos_field] = val
        out.append(rec)
    return out
