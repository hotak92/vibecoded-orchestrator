# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""REINFORCE-lite KG retrieval reranker.

Learns which KG nodes are actually useful in agent answers and adjusts
retrieval order accordingly. v0.2.9 C8 split the container into two
concerns:

1. **Live rerank** (``rerank_query_aware``, called inside /cache_nodes):
   over-fetches nodes from Weaviate, scores each by
   ``QUERY_WEIGHT * cosine(query, node) + POSITION_WEIGHT * pos_score``
   (or the RLModel when active), filters by ``RELEVANCE_THRESHOLD``, and
   returns ``RankedResult(top_k, extra_reference)``.

2. **Pure-train** (``_update``, called from /rl_update via
   ``schedule_update``): consumes a pre-packed payload from MCP
   (``cosine_sims`` + ``literal_cited`` + ``cross_encoder_cited`` +
   ``nodes_packed`` + ``query_emb``), applies the unified target formula
   in ``vco_lib.rl_training_targets.compute_unified_targets`` (the one
   copy; the image COPIES it), and steps the gradient
   via ``rl_model.update(...)``. The container does NO embedding,
   chunking, or data-logger writes — MCP is the sole citation writer
   per the v0.2.47 C6c HARD CUTOVER.

The unified target formula (applied identically online and offline):

  target_i = clamp(cos_i + 0.1 * z_mapped_i, 0, 1)
  if literal_cited[title]:
      target_i = min(1.0, target_i * 1.5)
      target_i = min(1.0, target_i + 0.1)
  if cross_encoder_cited[title]:
      target_i = min(1.0, target_i * 1.5)
      target_i = min(1.0, target_i + 0.1)
  cited_i = target_i > 0.6

``literal_cited`` (whether the node's title/slug/wikilink/file_path
appears in the agent's answer text via word-boundary regex) is a
**training signal** — not a logged-only label. The boost reaches the
cos3heads NN by construction because the formula is applied here in
``_update``, not in MCP.

``cross_encoder_cited`` is reserved in the schema but None in v0.2.9.
Pro-tier cross-encoder wiring (Qwen3-Reranker) is deferred to v0.2.10+
and will live MCP-side (the container no longer talks to Ollama for
citation detection).

State file: ``state/link_similarity.json``
  Precomputed cosine similarity between a node and its linked neighbours.
  Used as a feature by the RLModel during ``rerank_query_aware`` scoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

# `_central_embed` was previously imported from MAO's
# orchestrator.context.embedding. It is now injected at construction time
# via RetrievalRL(embedding_fn=...) — the launch script wires it to the
# active embedding pipeline (qwen3, arctic, openai, codesage) so the RL
# server uses the same vectoriser as the MCP server that calls it.
# A no-op fallback is used if no embedding_fn is provided; in that case
# any code path that needs cosine pre-filtering must be skipped at the
# call site (see _embed_query / _embed_node usage below).
async def _central_embed_unset(text: str, *args, **kwargs):  # pragma: no cover
    return None

try:
    import numpy as np  # optional — only needed when RLModel is active
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

if TYPE_CHECKING:
    from .rl_model import RLModel
    from rl_client.rl_logger import RLDataLogger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hyper-parameters
# ---------------------------------------------------------------------------

# Embedding constants — were imported from MAO's
# orchestrator.context.embedding module. Defaults below match the MAO
# values; override via env vars when packaging the RL server alongside a
# different KG vectoriser. Still used by ``rerank_query_aware`` (live rerank
# path) which embeds candidate chunks locally for scoring.
CHUNK_SIZE = int(os.environ.get("RL_EMBED_CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.environ.get("RL_EMBED_CHUNK_OVERLAP", "50"))
EMBED_MODEL = os.environ.get("RL_EMBED_MODEL", "qwen3-embedding:0.6b")
EMBED_TIMEOUT = float(os.environ.get("RL_EMBED_TIMEOUT", "30.0"))

# v0.2.9 C8: cross-encoder path (_detect_citations_reranker, _cite_with_reranker)
# was DELETED. The COSINE_SKIP_RERANKER_*, _RERANKER_*, RERANKER_TIMEOUT
# constants that fed it were removed in the same commit. Pro-tier cross-encoder
# wiring is deferred to v0.2.10+ on the MCP side (see SELF-HANDOFF-rl-citation-
# unified-v3-2026-06-04.md). The current container is pure-train: MCP supplies
# pre-packed payloads via /rl_update; the container applies the unified target
# formula and steps the gradient.

_MAX_EMBED_CACHE = 512      # max cached embeddings (LRU-style: drop oldest)
_MAX_CACHE_TASKS = 64       # max task_id entries in node content cache

_LINK_SIM_FILE = "state/link_similarity.json"

# Query-aware reranking (rerank_query_aware)
OVERFETCH_FACTOR = 3         # fetch this many × target_k nodes from Weaviate
RELEVANCE_THRESHOLD = 0.3    # minimum score to include a node (neural-net or cosine)


# ---------------------------------------------------------------------------
# RankedResult — output of rerank_query_aware
# ---------------------------------------------------------------------------

class RankedResult:
    """
    Output of ``rerank_query_aware()``.

    Attributes:
        top_k:           Top-K nodes above threshold — formatted at full/tiered fidelity.
        extra_reference: Nodes above threshold but beyond top-K — reference tier only.
    """

    __slots__ = ("top_k", "extra_reference")

    def __init__(
        self,
        top_k: "list[dict[str, Any]]",
        extra_reference: "list[dict[str, Any]]",
    ) -> None:
        self.top_k = top_k
        self.extra_reference = extra_reference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from .math_utils import cosine_similarity as _cosine_sim  # noqa: E402


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks of CHUNK_SIZE chars."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# v0.2.9 C8: _text_hash, _output_sample, _truncate_agent_output were DELETED.
# They served the cross-encoder + missed-citation-penalty paths that are now
# gone — the container no longer sees agent_output strings.


# ---------------------------------------------------------------------------
# RetrievalRL
# ---------------------------------------------------------------------------

class RetrievalRL:
    """Persisted REINFORCE-lite reranker for KG retrieval.

    v0.2.9 C8 reshape: this class is now **pure-train + live-rerank**. The
    citation-detection path (``_detect_citations_reranker``, the cross-
    encoder + cosine pre-filter pipeline) was deleted. MCP is the sole
    citation writer; it sends pre-packed payloads to /rl_update and the
    container applies the unified-target formula in ``_update``. The
    ``rerank_query_aware`` path (live in-container scoring during
    /cache_nodes calls) is unchanged.

    Args:
        project_root: Project root dir (state/ lives here).
        ollama_url:   Ollama API base URL for embeddings used by
                      ``rerank_query_aware``. Not used by ``_update``.
        embed_model:  Ollama embedding model
                      (``qwen3-embedding:0.6b`` default).
        rl_model:     Optional RLModel for query-aware neural relevance
                      scoring. When set, ``rerank_query_aware`` uses the
                      neural network instead of the cosine+weight fallback;
                      ``_update`` calls ``rl_model.update`` per training event.
    """

    def __init__(
        self,
        project_root: Path,
        ollama_url: str = "http://localhost:11435",
        embed_model: str = EMBED_MODEL,
        rl_model: "RLModel | None" = None,
        data_logger: "RLDataLogger | None" = None,
        rl_server_url: str | None = None,
        state_dir: Path | None = None,
        embedding_fn = None,  # async callable (text: str) -> list[float] | None
        # v0.2.9 C8: ``reranker_model`` was REMOVED. Cross-encoder citation
        # detection has been retired from the container; the wire contract
        # carries pre-packed payloads from MCP. Pro-tier cross-encoder
        # wiring lives on the MCP side and is deferred to v0.2.10+ (see
        # SELF-HANDOFF-rl-citation-unified-v3-2026-06-04.md). Pass nothing
        # for the old keyword — it's no longer accepted.
    ) -> None:
        self._root = Path(project_root)
        if state_dir:
            self._link_sim_path = Path(state_dir) / "link_similarity.json"
        else:
            self._link_sim_path = self._root / _LINK_SIM_FILE
        self._ollama_url = ollama_url.rstrip("/")
        self._embed_model = embed_model
        # Injected embedding function — see _central_embed_unset note above.
        # Used by ``rerank_query_aware`` (live rerank path); ``_update``
        # never calls it because MCP supplies all vectors pre-frozen.
        self._embedding_fn = embedding_fn
        # When rl_server_url is set, delegate scoring/training to the
        # centralized RL server. v0.2.9 C8: ``schedule_update`` no longer
        # goes through this path (the OLD agent_output → child-server POST
        # is gone), but ``rerank_query_aware`` still uses it for the live
        # /cache_nodes scoring path. Kept for a one-release deprecation
        # window; removal targeted for v0.2.10+.
        self._rl_server_url = rl_server_url.rstrip("/") if rl_server_url else None
        self._rl_model = rl_model if not self._rl_server_url else None
        self._data_logger = data_logger
        # {task_id: [{title, content, links, node_type}, ...]}  — populated
        # by ``cache_nodes`` for the live ``rerank_query_aware`` path.
        # v0.2.9 C8: NOT consumed by ``_update`` anymore — MCP supplies
        # everything via the wire payload.
        self._node_cache: dict[str, list[dict[str, str]]] = {}
        # {task_id: str}  — query text per in-flight task. Same scope as
        # _node_cache (live rerank only).
        self._task_queries: dict[str, str] = {}
        # v0.2.9 C8: ``_candidate_cache`` was REMOVED with the missed-
        # citation penalty (which was the only consumer). The penalty was
        # an online-only signal that couldn't be replicated offline, so
        # online vs offline training diverged on the same JSONL.
        # {node_title: float}  — precomputed cos(node, linked_nodes), offline
        self._link_sim: dict[str, float] = self._load_link_sim()
        self._link_sim_dirty: bool = False

        if rl_model:
            logger.info("RetrievalRL: RLModel enabled for query-aware scoring")

    # ------------------------------------------------------------------
    # Public — called from ContextBuilder
    # ------------------------------------------------------------------

    def cache_nodes(
        self,
        task_id: str,
        nodes: list[dict[str, Any]],
        query: str = "",
    ) -> None:
        """Cache retrieved nodes + query text for the live rerank path.

        Populates ``self._node_cache`` and ``self._task_queries`` for
        ``rerank_query_aware`` (the in-container live-rerank scoring path).
        Evicts oldest task entries when over ``_MAX_CACHE_TASKS``.

        v0.2.9 C8: the ``candidate_pool`` param was REMOVED together with
        the missed-citation penalty (its only consumer). The training path
        (``schedule_update`` -> ``_update``) no longer reads from these
        caches — MCP supplies pre-packed payloads via /rl_update.
        """
        if len(self._node_cache) >= _MAX_CACHE_TASKS:
            oldest = next(iter(self._node_cache))
            del self._node_cache[oldest]

        def _node_entry(node: dict) -> dict:
            return {
                "title": node.get("title", ""),
                "content": (node.get("content") or "")[:1000],
                "links": (node.get("links") or [])[:10],
                "node_type": node.get("node_type") or "concept",
            }

        self._node_cache[task_id] = [
            _node_entry(node) for node in nodes if node.get("title")
        ]
        if query:
            self._task_queries[task_id] = query

    def schedule_update(
        self,
        task_id: str,
        *,
        nodes_packed: "list[dict[str, Any]] | None" = None,
        query_emb: "list[float] | None" = None,
        cosine_sims: "dict[str, float] | None" = None,
        literal_cited: "dict[str, bool] | None" = None,
        cross_encoder_cited: "dict[str, bool] | None" = None,
        targets: "dict[str, float] | None" = None,
        task_type: str = "mcp_interactive",
        skip_train: bool = False,
    ) -> None:
        """Non-blocking: schedule weight update from MCP-supplied payload.

        v0.2.9 (C8): the wire contract is now a pre-packed payload from MCP.
        ``nodes_packed`` carries frozen ``n_emb`` / ``linked_embs`` /
        ``linked_type_names`` / ``node_type`` per node; ``query_emb`` is the
        frozen query vector; ``cosine_sims`` + ``literal_cited`` +
        ``cross_encoder_cited`` are the inputs the unified-target formula
        consumes inside ``_update``. The container does NO embedding work
        of its own — see ``_update`` docstring for the rationale.

        Pre-C8 contract (``agent_output``-based) is gone; callers that pass
        the old shape will get a no-op + a warning log. The
        ``_delegate_update_to_server`` parent-child pattern (when
        ``rl_server_url`` is set) is preserved but now unreachable from this
        method — it can only fire from a now-deleted legacy code path; it
        stays in the file for a one-release deprecation window before
        removal.

        Safe to call from synchronous context (fires async task on the
        current event loop). No-op if no running loop (e.g. during tests
        that don't spin one up).

        Args:
            task_id: Identifier matching the retrieval event.
            nodes_packed: Per-node pre-packed dicts from MCP. Required;
                ``schedule_update`` is a no-op when this is None/empty.
            query_emb: Frozen query vector from the retrieval event.
            cosine_sims: ``{title: raw_cosine}`` from MCP's citation path.
            literal_cited: ``{title: bool}`` flag dict (training signal).
            cross_encoder_cited: ``{title: bool}`` or None (None in v0.2.9
                production).
            targets: Ignored — the container recomputes targets from raw
                inputs to apply ``literal_cited`` as a training signal.
                Accepted for symmetry with the wire contract.
            task_type: Telemetry label (e.g. ``"mcp_interactive"``).
            skip_train: When True (project flag ``rl_online_training_disabled``
                is on), accept the payload but skip the gradient step.
        """
        if not nodes_packed:
            logger.debug(
                "RetrievalRL.schedule_update: no nodes_packed for task=%s; "
                "skipping (legacy agent_output shape is no longer supported)",
                task_id[:8],
            )
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "RetrievalRL.schedule_update: no running loop, skipping update for %s",
                task_id[:8],
            )
            return

        loop.create_task(
            self._update(
                task_id=task_id,
                targets=targets,
                nodes_packed=nodes_packed,
                query_emb=query_emb or [],
                cosine_sims=cosine_sims or {},
                literal_cited=literal_cited or {},
                cross_encoder_cited=cross_encoder_cited,
                skip_train=skip_train,
            )
        )

    async def _delegate_update_to_server(self, task_id: str, agent_output: str) -> None:
        """Fire-and-forget training delegation to centralized RL server."""
        try:
            payload = {"task_ids": [task_id], "agent_output": agent_output[:8000]}
            timeout = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(
                    f"{self._rl_server_url}/rl_update", json=payload,
                ) as resp:
                    if resp.status == 200:
                        logger.debug("RL training delegated to server for task %s", task_id[:8])
        except Exception as exc:
            logger.debug("RL server training delegation failed: %s", exc)

    def pop_new_type_messages(self) -> "list[str]":
        """
        Return and clear any pending new-node-type notification messages.

        These are generated when the RLModel auto-registers an unknown node type
        encountered during ``rerank_query_aware()``.  Callers (e.g. ContextBuilder)
        should inject these messages into the agent context so the agent is aware
        a new type was created and can confirm or correct it.
        """
        if self._rl_model is not None:
            return self._rl_model.pop_new_type_messages()
        return []

    # ------------------------------------------------------------------
    # Private — async update pipeline
    # ------------------------------------------------------------------

    async def _update(
        self,
        task_id: str,
        targets: "dict[str, float] | None",  # accepted for symmetry; recomputed below
        nodes_packed: "list[dict[str, Any]]",
        query_emb: "list[float]",
        cosine_sims: "dict[str, float]",
        literal_cited: "dict[str, bool]",
        cross_encoder_cited: "dict[str, bool] | None",
        *,
        skip_train: bool = False,
    ) -> None:
        """v0.2.9 pure-train: train ``_rl_model`` on a pre-packed payload.

        ALL retrieval-time embedding work is done by MCP and shipped in
        ``nodes_packed`` (each entry has ``n_emb``, ``linked_embs``,
        ``linked_type_names``, ``node_type`` already frozen at scoring time).
        The container does NO chunking, NO embedding, NO data-logger writes
        (MCP is sole citation writer per the C6c HARD CUTOVER + C7 citation
        path) — it just applies the unified-target formula and steps the
        gradient.

        The user-locked invariant 2026-06-04: ``literal_cited`` is a TRAINING
        SIGNAL for the cos3heads NN, not a logged-only label. We re-derive
        targets via ``compute_unified_targets(cosine_sims, literal_cited,
        cross_encoder_cited)`` so the literal-citation bonus actually reaches
        ``rl_model.update(target=...)``. The same formula runs at offline-
        training time (offline_trainer.py reads the citation event's
        ``cosine_sims`` + ``literal_cited`` + ``cross_encoder_cited`` fields),
        making online == offline byte-identical — the load-bearing property
        that prevents replay drift.

        ``targets`` is accepted for symmetry with the wire shape but is
        deliberately RECOMPUTED here from raw inputs. Trusting an MCP-supplied
        ``targets`` would skip the literal_cited bonus when MCP didn't apply
        it (e.g. mid-rollout version skew) and would split the formula across
        two surfaces. Single source of truth: ``vco_lib/rl_training_targets.py``.

        Args:
            task_id: Identifier matching the retrieval event written by MCP.
            targets: Ignored (see above). Kept in the signature for the wire
                contract; future revisions may revive it if the formula
                relocates to MCP authoritatively.
            nodes_packed: Per-node dicts from MCP's
                ``_rl_enrich_nodes_with_linked_embs``. Each entry:
                ``{title, node_type, n_emb, linked_embs, linked_type_names}``.
                Vectors are frozen at retrieval time — never re-embedded.
            query_emb: Frozen query embedding from the retrieval event.
            cosine_sims: ``{title: max-cos(answer_chunk, n_emb)}`` from MCP's
                citation-write path. RAW (no bonuses pre-applied).
            literal_cited: ``{title: bool}`` per-node literal-citation flag.
                Drives the boost in ``compute_unified_targets``.
            cross_encoder_cited: ``{title: bool}`` or ``None``. None in v0.2.9
                production (Pro-tier cross-encoder is deferred to v0.2.10+).
            skip_train: When True, accept the payload but do NOT call
                ``_rl_model.update``. Honors the
                ``rl_online_training_disabled`` project flag.
        """
        if self._rl_model is None or skip_train or not nodes_packed:
            return

        from vco_lib.rl_training_targets import compute_unified_targets
        targets_with_bonus, _cited_binary = compute_unified_targets(
            cosine_sims,
            literal_cited=literal_cited,
            cross_encoder_cited=cross_encoder_cited,
        )

        q_raw = np.array(query_emb, dtype=np.float32)
        total_loss = 0.0
        n_trained = 0
        n_literal = sum(1 for v in (literal_cited or {}).values() if v)

        for n in nodes_packed:
            title = n.get("title", "")
            if not title or title not in targets_with_bonus:
                continue
            n_emb = n.get("n_emb")
            if not n_emb:
                continue
            n_raw = np.array(n_emb, dtype=np.float32)
            linked_raws: "list[np.ndarray]" = [
                np.array(e, dtype=np.float32) for e in (n.get("linked_embs") or [])
            ]
            linked_type_names = n.get("linked_type_names") or []
            linked_type_idxs = (
                [self._rl_model.get_type_idx(t) for t in linked_type_names]
                or None
            )
            n_type_idx = self._rl_model.get_type_idx(
                n.get("node_type") or "concept"
            )

            loss = self._rl_model.update(
                q_raw, n_raw, linked_raws, n_type_idx,
                target=targets_with_bonus[title],
                linked_type_idxs=linked_type_idxs,
            )
            total_loss += loss
            n_trained += 1

        if n_trained:
            self._rl_model.save()
            logger.debug(
                "RetrievalRL: trained %d nodes task=%s avg_loss=%.4f literal_cited=%d",
                n_trained, task_id[:8], total_loss / n_trained, n_literal,
            )

    # v0.2.9 C8: the cross-encoder + cosine-only citation-detection
    # methods (_detect_citations_reranker, _compute_cosine_similarities,
    # _compute_advantage_rewards, _compute_targets, _cite_with_reranker)
    # were DELETED. MCP is sole citation writer; container receives
    # pre-packed payloads via /rl_update and applies the unified target
    # formula in _update. See SELF-HANDOFF-rl-citation-unified-v3-
    # 2026-06-04.md for the architectural rationale.

    async def _embed(self, text: str) -> list[float] | None:
        """
        Get embedding via the injected embedding function.

        If no embedding_fn was passed at construction time we return None,
        which causes call sites to skip the cosine pre-filter path and
        fall back to the cross-encoder (slower but correct). This keeps
        the RL server functional in test/standalone deployments without
        a live KG embedding pipeline.
        """
        if self._embedding_fn is None:
            return None
        try:
            return await self._embedding_fn(text)
        except Exception as exc:  # pragma: no cover
            logger.debug("RetrievalRL._embed: injected embedding_fn failed (%s)", exc)
            return None

    # ------------------------------------------------------------------
    # Neural scoring helpers
    # ------------------------------------------------------------------

    # v0.2.9 C8: _train_rl_model was DELETED. The new _update IS the
    # training step — it accepts pre-packed payload (cosine_sims +
    # literal_cited + cross_encoder_cited + nodes_packed + query_emb),
    # applies compute_unified_targets to derive bonus-boosted training
    # targets, and calls rl_model.update directly. The query-name-citation
    # boost AND the missed-citation penalty (both online-only signals
    # that the offline trainer couldn't replicate) were retired along
    # with this method.

    async def rerank_query_aware(
        self,
        nodes: list[dict[str, Any]],
        task_type: str,
        query: str,
        target_k: int,
        threshold: float = RELEVANCE_THRESHOLD,
        task_id: str = "",
        session_id: str = "",
        explore_rate_below: float = 0.0,
        explore_rate_swap: float = 0.0,
    ) -> "RankedResult":
        """
        Neural reranker: embed query (multi-chunk), score all nodes, threshold-filter.

        Nodes above threshold are sorted by score descending:
          - First ``target_k`` → ``RankedResult.top_k``   (full/tiered fidelity)
          - Remaining above threshold → ``RankedResult.extra_reference``  (reference tier)
          - Below threshold → dropped (unless exploration policy fires)

        Query multi-chunk:
          Long queries are split into CHUNK_SIZE chunks.  Node cosine is the *max*
          over all query chunks (captures relevance to any part of the query).
          The first chunk's embedding is used as the RLModel query input (q_raw).

        Scoring:
          - When ``_rl_model`` is active: ``RLModel.score()`` (neural network).
          - Otherwise: pure cosine similarity ``cos(query, node)`` (max over query chunks).
          - Fallback (no embedding): preserve Weaviate order, slice to target_k.

        Args:
            nodes:      Candidate pool from Weaviate (typically target_k × OVERFETCH_FACTOR).
            task_type:  Task type label (used for data logger, not for scoring).
            query:      Task description / search query text.
            target_k:   Max nodes for the ``top_k`` list.
            threshold:  Minimum score to keep a node (default: RELEVANCE_THRESHOLD).
            task_id:    Optional task identifier for data logger linkage.
            explore_rate_below: Probability (0–1) to add one below-threshold node
                                as an extra probe in the result, gaining citation
                                signal for the sub-threshold region.  Use 0.1 in
                                eval, 0.0 in production.
            explore_rate_swap:  Probability (0–1) to replace the lowest-scored
                                selected node with a below-threshold probe.
                                Use 0.2 in eval, 0.0 in production.

        Returns:
            RankedResult(top_k, extra_reference).
        """
        from .rl_model import MAX_LINKED, UNKNOWN_TYPE_IDX

        if not nodes:
            return RankedResult([], [])

        # --- Delegate to centralized RL server when configured ---
        if self._rl_server_url:
            try:
                payload = {
                    "task_id": task_id or "",
                    "query": query,
                    "nodes": nodes,
                    "limit": target_k,
                }
                timeout = aiohttp.ClientTimeout(total=5.0)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.post(
                        f"{self._rl_server_url}/cache_nodes", json=payload,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            top_k = data.get("top_k", [])
                            if top_k:
                                extra = [n for n in nodes if n not in top_k][:target_k]
                                return RankedResult(top_k[:target_k], extra)
            except Exception as exc:
                logger.debug("RL server unreachable (%s) — falling back to local scoring", exc)
            # Fall through to local scoring if server fails

        # Embed query — multi-chunk support
        query_chunks = _chunk_text(query) if len(query) > CHUNK_SIZE else [query]
        q_chunk_embs: list[list[float]] = []
        for chunk in query_chunks:
            emb = await self._embed(chunk)
            if emb:
                q_chunk_embs.append(emb)

        if not q_chunk_embs:
            # No embedding available — preserve Weaviate order as fallback
            logger.debug("RetrievalRL: no query embedding for task %s, using Weaviate order", task_id[:8])
            return RankedResult(nodes[:target_k], [])

        # First chunk embedding used as RLModel query input
        q_emb = q_chunk_embs[0]
        q_raw = np.array(q_emb, dtype=np.float32) if _HAS_NUMPY else None

        # Build title→type_idx map from the candidate pool for linked-node type resolution.
        # Linked nodes that appear in this pool get their correct type; others → NULL_TYPE_IDX.
        _title_to_type: dict[str, str] = {}
        if self._rl_model is not None:
            for _n in nodes:
                _t = _n.get("title", "")
                if _t:
                    _title_to_type[_t] = _n.get("node_type") or "concept"

        scored: list[tuple[float, dict[str, Any]]] = []
        below_threshold: list[tuple[float, dict[str, Any]]] = []
        # node_embs[title] = embedding vector (for logging, frozen at scoring time)
        node_embs: dict[str, list[float]] = {}
        # node_features[title] = cos scalars + link titles for log_retrieval
        node_features: dict[str, dict[str, Any]] = {}
        # Batched scoring: collect candidates for single forward pass
        _batch_candidates: list[tuple] = []
        _batch_nodes: list[dict] = []
        _batch_cos_qn: list[float] = []

        for node in nodes:
            title = node.get("title", "")
            content = node.get("content") or title
            links = node.get("links") or []
            node_type = node.get("node_type") or "concept"

            # Multi-chunk node embedding.  Each content chunk is embedded separately;
            # the chunk with the highest cosine to the primary query embedding is the
            # "retrieved" chunk → n_raw (target node input).  Extra chunks get placed
            # first in the linked-node slots, using the target node's type index, so
            # the model sees the full node context.  _build_inputs shuffles all slots.
            node_content_chunks = (
                _chunk_text(content) if len(content) > CHUNK_SIZE else [content]
            )
            node_chunk_embs: list[list[float]] = []
            for _chunk in node_content_chunks:
                _cemb = await self._embed(_chunk)
                if _cemb:
                    node_chunk_embs.append(_cemb)

            n_emb: "list[float] | None" = None
            extra_chunk_raws: "list[np.ndarray]" = []
            cos_qn = 0.0
            if node_chunk_embs:
                best_idx = max(
                    range(len(node_chunk_embs)),
                    key=lambda i: _cosine_sim(node_chunk_embs[i], q_emb),
                )
                n_emb = node_chunk_embs[best_idx]
                node_embs[title] = n_emb
                cos_qn = max(
                    _cosine_sim(nc, qe)
                    for nc in node_chunk_embs for qe in q_chunk_embs
                )
                extra_chunk_raws = [
                    np.array(node_chunk_embs[i], dtype=np.float32)
                    for i in range(len(node_chunk_embs)) if i != best_idx
                ]

            # Features logged for every node regardless of scoring path
            cos_nl = self.get_link_similarity(title)
            link_titles = [lnk if isinstance(lnk, str) else str(lnk) for lnk in links[:10]]
            node_features[title] = {"cos_qn": cos_qn, "cos_nl": cos_nl, "links": link_titles}

            if self._rl_model is not None and n_emb and q_raw is not None:
                n_type_idx = self._rl_model.get_type_idx(node_type)
                n_raw = np.array(n_emb, dtype=np.float32)

                # Linked slot filling:
                # 1. Extra node chunks first (target node's type)
                # 2. Actual linked nodes fill remaining slots
                # _build_inputs shuffles all + pads remainder with NULL + zero
                actual_link_embs: list[list[float]] = []
                actual_link_raws: "list[np.ndarray]" = []
                actual_link_type_idxs: list[int] = []
                link_type_names: list[str] = []
                remaining_slots = max(0, MAX_LINKED - len(extra_chunk_raws))
                for raw_link in links[:remaining_slots]:
                    link_str = raw_link if isinstance(raw_link, str) else str(raw_link)
                    lemb = await self._embed(link_str)
                    if lemb:
                        actual_link_embs.append(lemb)
                        actual_link_raws.append(np.array(lemb, dtype=np.float32))
                        ltype = _title_to_type.get(link_str, "")
                        link_type_names.append(ltype)
                        actual_link_type_idxs.append(
                            self._rl_model.get_type_idx(ltype) if ltype else UNKNOWN_TYPE_IDX
                        )

                # Combine: extra chunks (node type) prepended to actual linked nodes
                linked_raws = extra_chunk_raws + actual_link_raws
                linked_type_idxs = (
                    [n_type_idx] * len(extra_chunk_raws) + actual_link_type_idxs
                )

                # cos_ql = cosine(query, all linked slots: extra chunks + actual links).
                # Extra chunks are already treated as linked nodes in every other respect;
                # the cosine feature should follow the same rule.
                all_linked_embs: list = list(extra_chunk_raws) + actual_link_embs
                cos_ql = (
                    sum(_cosine_sim(q_emb, le) for le in all_linked_embs)
                    / len(all_linked_embs)
                    if all_linked_embs else 0.0
                )
                node_features[title]["cos_ql"] = cos_ql
                # Log type names for ALL linked slots (extra chunks first, then actual links)
                # so offline trainer populates link_type_idxs for every slot correctly.
                all_link_type_names = [node_type] * len(extra_chunk_raws) + link_type_names
                if all_link_type_names:
                    node_features[title]["link_types"] = all_link_type_names
                # Compute and cache cos_nl from actual linked node embeddings
                if title not in self._link_sim and actual_link_raws:
                    computed_cos_nl = self._compute_cos_nl(n_raw, actual_link_raws)
                    self._link_sim[title] = computed_cos_nl
                    self._link_sim_dirty = True
                    node_features[title]["cos_nl"] = computed_cos_nl

                # Collect for batched scoring (below)
                _batch_candidates.append((n_raw, linked_raws, n_type_idx, linked_type_idxs or None))
                _batch_nodes.append(node)
                _batch_cos_qn.append(cos_qn)
            else:
                # Cosine-only fallback (no RLModel or no node embedding)
                if cos_qn >= threshold:
                    scored.append((cos_qn, node))
                else:
                    below_threshold.append((cos_qn, node))

        # Batched neural scoring — single forward pass for all candidates
        if _batch_candidates and self._rl_model is not None and q_raw is not None:
            batch_scores = self._rl_model.score_batch(q_raw, _batch_candidates)
            for sc, node, cos_qn in zip(batch_scores, _batch_nodes, _batch_cos_qn):
                if sc >= threshold:
                    scored.append((sc, node))
                else:
                    below_threshold.append((sc, node))
        elif _batch_candidates:
            # Model disappeared between check and here — fall back to cosine
            for node, cos_qn in zip(_batch_nodes, _batch_cos_qn):
                if cos_qn >= threshold:
                    scored.append((cos_qn, node))
                else:
                    below_threshold.append((cos_qn, node))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Exploration policy — occasionally surface sub-threshold nodes so the
        # model gets citation signal for the low-score region.
        # probe_titles tracks injected probes (used to set tier="probe" in log).
        probe_titles: set[str] = set()
        if below_threshold:
            if explore_rate_swap > 0.0 and scored and random.random() < explore_rate_swap:
                probe = random.choice(below_threshold)
                below_threshold = [x for x in below_threshold if x is not probe]
                scored[-1] = probe  # replace lowest selected (list is desc-sorted)
                probe_titles.add(probe[1].get("title", ""))
                scored.sort(key=lambda x: x[0], reverse=True)
            if below_threshold and explore_rate_below > 0.0 and random.random() < explore_rate_below:
                probe_extra = random.choice(below_threshold)
                probe_titles.add(probe_extra[1].get("title", ""))
                scored.append(probe_extra)  # appended last; lands in extra_reference tier

        above_scores = scored                                 # [(score, node), ...]
        # Inject score + tier into each node dict so callers (incl. HTTP /cache_nodes) can use them.
        for i, (sc, node) in enumerate(above_scores):
            node["score"] = round(sc, 4)
            node["tier"] = "top_k" if i < target_k else "extra_reference"
        above = [node for _, node in above_scores]
        result = RankedResult(top_k=above[:target_k], extra_reference=above[target_k:])

        # Log retrieval event for offline RL training.
        # Embed vectors are frozen at scoring time: Weaviate node content may
        # change later, so we record the exact vectors used during this session.
        # cos_qn/cos_nl/links stored for every node; cos_ql stored on RLModel
        # path only (absent → offline trainer uses cos_qn as proxy, never 0.5).
        if self._data_logger is not None:
            log_nodes = []
            for sc, node in above_scores[:target_k]:
                title = node.get("title", "")
                tier = "probe" if title in probe_titles else "top_k"
                entry: dict[str, Any] = {"title": title, "score": sc, "tier": tier}
                if title in node_embs:
                    entry["emb"] = node_embs[title]
                entry.update(node_features.get(title, {}))
                log_nodes.append(entry)
            for sc, node in above_scores[target_k:]:
                title = node.get("title", "")
                tier = "probe" if title in probe_titles else "extra_reference"
                entry = {"title": title, "score": sc, "tier": tier}
                if title in node_embs:
                    entry["emb"] = node_embs[title]
                entry.update(node_features.get(title, {}))
                log_nodes.append(entry)
            self._data_logger.log_retrieval(
                task_id=task_id,
                task_type=task_type,
                query=query,
                nodes=log_nodes,
                session_id=session_id,
                query_emb=q_emb,
            )

        if self._link_sim_dirty:
            self._save_link_sim()

        return result

    # ------------------------------------------------------------------
    # Link-similarity helpers
    # ------------------------------------------------------------------

    def get_link_similarity(self, title: str) -> float:
        """Return precomputed cos(node, linked_nodes) for ``title``, or 0.5 if unknown."""
        return self._link_sim.get(title, 0.5)

    def _compute_cos_nl(
        self,
        n_raw: "np.ndarray",
        linked_raws: "list[np.ndarray]",
    ) -> float:
        """Return cosine(node, mean(linked_nodes)) — real computed value, no defaults."""
        mean_link = np.mean(linked_raws, axis=0).astype(np.float32)
        na = float(np.linalg.norm(n_raw))
        nb = float(np.linalg.norm(mean_link))
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(n_raw, mean_link) / (na * nb))

    def _save_link_sim(self) -> None:
        """Persist _link_sim to disk and clear the dirty flag."""
        try:
            self._link_sim_path.parent.mkdir(parents=True, exist_ok=True)
            self._link_sim_path.write_text(
                json.dumps(self._link_sim, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._link_sim_dirty = False
            logger.debug(
                "RetrievalRL: saved %d link_sim entries to %s",
                len(self._link_sim), self._link_sim_path,
            )
        except Exception as exc:
            logger.warning("RetrievalRL: failed to save link_similarity: %s", exc)

    def _load_link_sim(self) -> "dict[str, float]":
        """Load state/link_similarity.json — mapping node_title → cos(node, linked) float."""
        try:
            if self._link_sim_path.exists():
                raw = json.loads(self._link_sim_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}
        except Exception as exc:
            logger.warning("RetrievalRL: failed to load link_similarity: %s", exc)
        return {}
