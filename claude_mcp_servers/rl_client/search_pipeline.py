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
    # ---- v0.2.71 Sweep-C: dual-RL-log fan-out (the OTHER embedding slot) ----
    # When ``dual_log`` is True the pipeline emits a SECOND retrieval event tagged
    # with the other slot's embedding triple, on a deterministic ``:slot``-suffixed
    # task_id (so the offline loader's per-source corpus picks it up cleanly and the
    # citation second-event pairs by the same suffixed id). The other-slot per-node
    # vectors are attached upstream as ``emb_other`` / ``cos_qn_other`` on each
    # candidate dict (mirrors the existing ``emb`` / ``cos_qn`` enrichment); the
    # active (bare-task_id) event is byte-unchanged.
    dual_log: bool = False
    other_query_emb: Optional[list[float]] = None
    other_embedding_source: str = ""
    other_embedding_dim: int = 0
    other_embedding_model: str = ""


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

    # ---- populate citation cache (+ stage deferred-queue pending file) ----
    # The monitor consumes this cache when it eventually fires; storing
    # candidates here saves a Weaviate re-fetch at citation-write time.
    # F-QUEUE (v0.2.70): the populate ALSO persists the staged ctx to a disk
    # pending file so the turn-end Stop-hook drain can recover the citation if
    # the in-process monitor never fires (the hook path has no monitor at all;
    # the MCP path's monitor may be evicted/timed-out). The MCP monitor deletes
    # its own pending file on fire so the drain only processes survivors.
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
            session_id=req.session_id,
            query=req.query,
            stage_pending_file=True,
            # v0.2.71 Sweep-C: carry the dual-log decision + other-slot triple
            # into the staged ctx so the citation second-event (suffixed task_id)
            # can be emitted at fire time alongside the active citation.
            dual_log=req.dual_log,
            other_embedding_source=req.other_embedding_source,
            other_embedding_dim=req.other_embedding_dim,
            other_embedding_model=req.other_embedding_model,
            other_query_emb=req.other_query_emb,
        )
    except Exception as exc:
        logger.debug("rerank_and_emit: citation cache populate failed (%s)", exc)

    # ---- emit retrieval telemetry ----
    emit_success = False
    log_nodes = _build_log_nodes(req.candidates, req.limit)
    try:
        ev = RetrievalEvent(
            query=req.query,
            # Pass None through verbatim (do NOT coerce to []) — the
            # writer + offline trainer distinguish "no embedding
            # available" (None) from "zero-length embedding" (bug).
            query_emb=req.query_emb,
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

    # ---- v0.2.71 Sweep-C: dual-RL-log fan-out (the OTHER embedding slot) ----
    # 1:1 reuse of the collapsed per-node candidate set (the collapse already ran
    # upstream of this pipeline), with the other slot's per-node vectors that the
    # enrichment site attached as ``emb_other`` / ``cos_qn_other``. No second
    # retrieval. Soft-fail throughout — a broken second emit never affects the
    # active event, the rerank, or the user-facing search.
    if req.dual_log:
        try:
            _emit_other_slot_event(task_id, req)
        except Exception as exc:
            logger.debug("rerank_and_emit: dual-log second emit raised (%s)", exc)

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
    session_id: Optional[str] = None,
    query: str = "",
    stage_pending_file: bool = False,
    dual_log: bool = False,
    other_embedding_source: str = "",
    other_embedding_dim: int = 0,
    other_embedding_model: str = "",
    other_query_emb: Optional[list[float]] = None,
) -> None:
    """Write the per-task entry the answer monitor reads at citation time.

    Same entry shape as the pre-v0.2.52 inline write at
    ``server.py:4763``. Bounded by ``_RL_NODE_CACHE_MAX``; LRU-ish
    eviction via insertion-order pop.

    F-QUEUE (v0.2.70): when ``stage_pending_file`` is True, ALSO persist the
    ctx to ``.claude/state/rl_pending/<session>__<task_id>.json`` so the
    turn-end Stop-hook drain can recover the citation when the in-process
    monitor never fires. Soft-fail — staging failure never breaks search.
    """
    from claude_mcp_servers.weaviate_mcp import server as srv

    # Build the cache-stored node list (same shape as what the monitor
    # later reads). This MAY differ from the rerank output if the RL
    # client returned a re-ordered subset — by storing the full
    # over-fetched list we let the citation computation see candidates
    # the user-facing response truncated.
    cache_nodes = _build_log_nodes(candidates, limit)
    ctx_dict: Optional[dict] = None
    try:
        project_id_for_cache: Optional[str] = None
        try:
            _cfg = srv._try_resolve_project_config()
            if _cfg is not None:
                project_id_for_cache = getattr(_cfg, "project_id", None)
        except Exception:
            pass
        ctx_dict = {
            "nodes": cache_nodes,
            "query_emb": list(query_emb) if query_emb else None,
            "active_model": embedding_model,
            "embedding_source": embedding_source,
            "embedding_dim": embedding_dim,
            "project_id": project_id_for_cache,
            "project_name": getattr(srv, "PROJECT_NAME", "") or "",
            "task_type": task_type,
        }
        # v0.2.71 Sweep-C: stage the dual-log decision + the OTHER slot's triple
        # so the citation second-event (slot-suffixed task_id) can fire at the
        # same time as the active citation. ``other_nodes`` carries each node's
        # other-slot vector under ``n_emb`` so ``compute_citation`` can compute
        # the other-model cosine without re-fetching; suppressed when no node has
        # the other slot (the second citation event is then skipped downstream).
        if dual_log:
            other_nodes = _build_other_slot_log_nodes(candidates, limit)
            if other_nodes:
                ctx_dict["dual_log"] = True
                ctx_dict["other_embedding_source"] = other_embedding_source
                ctx_dict["other_embedding_dim"] = other_embedding_dim
                ctx_dict["other_embedding_model"] = other_embedding_model
                ctx_dict["other_query_emb"] = (
                    list(other_query_emb) if other_query_emb else None
                )
                ctx_dict["other_nodes"] = other_nodes
        srv._rl_node_content_cache[task_id] = ctx_dict
        # LRU bound — pop oldest insertion-order entry until size <= max.
        max_size = getattr(srv, "_RL_NODE_CACHE_MAX", 256)
        while len(srv._rl_node_content_cache) > max_size:
            srv._rl_node_content_cache.pop(next(iter(srv._rl_node_content_cache)))
    except Exception as exc:
        logger.debug("_populate_citation_cache: write failed (%s)", exc)

    # F-QUEUE: durable pending file (backstop for the deferred-citation drain).
    # S1 (v0.2.70): this is the SINGLE stage point for BOTH paths — the hook
    # path threads its resolved session_id through RerankRequest and relies on
    # this stage exclusively (no separate hook-side re-stage). ``source`` is
    # derived from task_type: only the long-lived MCP path runs an in-process
    # monitor that deletes its own pending file on fire, so only it is tagged
    # "mcp"; hook-path tasks are "hook" (no monitor → drain always processes).
    if stage_pending_file and ctx_dict is not None:
        try:
            from claude_mcp_servers.rl_client.citation_pending import stage_pending
            from .telemetry_emit import resolve_session_id

            staged_seq = getattr(srv, "_rl_call_seq", None)
            source = "mcp" if task_type == "mcp_interactive" else "hook"
            stage_pending(
                session_id=resolve_session_id(session_id or ""),
                task_id=task_id,
                seq=staged_seq if source == "mcp" else None,
                query=query,
                ctx=ctx_dict,
                source=source,
            )
        except Exception as exc:
            logger.debug("_populate_citation_cache: stage_pending failed (%s)", exc)


def _clamp_unit_score(value: Any) -> float:
    """F-E (v0.2.70): clamp a telemetry score into the [0, 1] contract.

    Hybrid-fusion / BM25 paths can emit an UNBOUNDED ``score`` (observed max
    10.37 from the unnormalized ``mcp_interactive`` combiner). Such values
    leaked into ``rl_events`` and then ``compute_unified_targets`` clamped
    >1 → 1.0, silently mis-marking the node as max-cited. Normalizing/clamping
    at the WRITER boundary (here) stops NEW poison at the source rather than
    relying on a downstream clamp the offline trainer may not apply. A
    non-numeric score degrades to 0.0 (soft-fail — telemetry never raises).
    """
    try:
        s = float(value)
    except (TypeError, ValueError):
        return 0.0
    if s != s:  # NaN
        return 0.0
    if s < 0.0:
        return 0.0
    if s > 1.0:
        return 1.0
    return s


def _build_log_nodes(
    candidates: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Reduce per-node fields to what telemetry actually needs.

    Mirrors the pre-v0.2.52 reducer at ``server.py:4719``. Drops any
    Weaviate-internal cruft; preserves enrichment fields that the v3
    retrieval-event payload uses (``n_emb`` for unified-target training,
    ``linked_embs`` / ``linked_type_names`` for graph context,
    ``cos_*`` for similarity diagnostics).

    F-E (v0.2.70): the stored ``score`` is clamped to [0, 1] at this writer
    boundary (see ``_clamp_unit_score``) so unbounded hybrid-fusion values
    never reach ``rl_events`` / the training-target formula.
    """
    out: list[dict[str, Any]] = []
    for idx, n in enumerate(candidates):
        if not isinstance(n, dict):
            continue
        rec: dict[str, Any] = {
            "title": n.get("title", ""),
            "score": _clamp_unit_score(n.get("score", 0.0)),
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
        # V52-J Edit 3 / V52-Q (2026-06-09): raw cosine score from
        # the source Weaviate distance (= 1.0 - distance), distinct
        # from the fused ``score`` field (which the RL rerank or the
        # hybrid combiner may have rewritten). The offline trainer
        # consumes both: fused score for ranking supervision, raw
        # cosine for embedding-quality drift detection.
        if n.get("score_cosine") is not None:
            # score_cosine = 1 - distance, normally bounded [0,1]; clamp
            # defensively (a negative Weaviate distance would push it >1).
            rec["score_cosine"] = _clamp_unit_score(n["score_cosine"])
        out.append(rec)
    return out


# ---- v0.2.71 Sweep-C: dual-RL-log fan-out helpers ------------------


def slot_suffixed_task_id(task_id: str, embedding_source: str) -> str:
    """Derive the second-slot event's task_id (``<task_id>:<slot>``).

    The hub enforces NO task_id uniqueness and the offline loader pairs the
    retrieval↔citation events by the bare task_id WITHIN an ``embedding_source``
    partition (RL-chat contract, 2026-06-30). Applying a deterministic ``:slot``
    suffix to BOTH the second retrieval event AND the second citation event keeps
    them paired in the OTHER source's corpus while leaving the ACTIVE-slot pair on
    the bare ``task_id`` (so the existing single-log path is byte-unchanged). The
    loader then filters retrievals by ``embedding_source``, so ``<tid>:qwen3`` lands
    cleanly in the qwen3 corpus and never collides with the active arctic event.
    """
    return f"{task_id}:{embedding_source}"


def _build_other_slot_log_nodes(
    candidates: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Reduce candidates to telemetry log-nodes using the OTHER slot's vectors.

    Same shape + tiering as ``_build_log_nodes`` but reads ``emb_other`` /
    ``cos_qn_other`` (attached by the enrichment site for the non-active slot)
    in place of ``emb`` / ``cos_qn`` / ``n_emb``. Candidates lacking an
    ``emb_other`` are SKIPPED entirely (the second event only carries nodes for
    which the other slot's vector genuinely exists — never fabricates). All
    other fields (score, links, node_type) are reused verbatim from the
    identical per-node candidate set (case-(a) 1:1 fan-out).
    """
    out: list[dict[str, Any]] = []
    for idx, n in enumerate(candidates):
        if not isinstance(n, dict):
            continue
        emb_other = n.get("emb_other")
        if not emb_other:
            # No other-slot vector for this node — skip rather than fabricate.
            continue
        rec: dict[str, Any] = {
            "title": n.get("title", ""),
            "score": _clamp_unit_score(n.get("score", 0.0)),
            "tier": "top_k" if idx < limit else "extra_reference",
            # The other slot's per-node vector serves as BOTH ``emb`` and
            # ``n_emb`` (the citation cosine side reads ``n_emb`` first).
            "emb": emb_other,
            "n_emb": emb_other,
        }
        if n.get("node_type"):
            rec["node_type"] = n["node_type"]
        if n.get("links"):
            rec["links"] = n["links"]
        cos_other = n.get("cos_qn_other")
        if cos_other is not None:
            rec["cos_qn"] = cos_other
        if n.get("score_cosine") is not None:
            rec["score_cosine"] = _clamp_unit_score(n["score_cosine"])
        out.append(rec)
    return out


def _emit_other_slot_event(task_id: str, req: "RerankRequest") -> None:
    """Emit the second (other-slot) retrieval event. Soft-fail, no-op when empty.

    Builds the other-slot log-nodes (skipping candidates with no other-slot
    vector). When NO candidate has the other slot, the second event is SUPPRESSED
    (we do not write a node-less happy-path event — that would mis-signal). The
    writer is the OTHER-slot writer, resolved via ``_get_rl_telemetry_writer_for``
    keyed on ``other_embedding_source`` (the per-(project, emb_source) cache
    already isolates the two writers — no new caching code).
    """
    other_nodes = _build_other_slot_log_nodes(req.candidates, req.limit)
    if not other_nodes:
        logger.debug(
            "dual-log: no candidate carries the other slot; suppressing second event"
        )
        return

    other_src = req.other_embedding_source
    other_task_id = slot_suffixed_task_id(task_id, other_src or "other")

    def _other_writer_factory():
        from claude_mcp_servers.weaviate_mcp.server import _get_rl_telemetry_writer_for

        return _get_rl_telemetry_writer_for(
            other_src,
            embedding_dim=req.other_embedding_dim,
            embedding_model=req.other_embedding_model,
        )

    other_ev = RetrievalEvent(
        query=req.query,
        query_emb=req.other_query_emb,
        embedding_source=other_src,
        embedding_dim=req.other_embedding_dim,
        embedding_model=req.other_embedding_model,
        nodes=other_nodes,
        task_id=other_task_id,
        task_type=req.task_type,
        session_id=req.session_id,
        failure_mode=req.failure_mode,
        failed_collections=list(req.failed_collections),
    )
    try:
        emit_rl_event(other_ev, writer_factory=_other_writer_factory)
    except EmitValidationError as exc:
        logger.debug("dual-log: second emit validation failed (%s)", exc)
    except Exception as exc:
        logger.debug("dual-log: second emit raised (%s)", exc)
