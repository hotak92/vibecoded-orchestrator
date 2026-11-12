# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""REINFORCE-lite KG retrieval reranker.

Learns which KG nodes are actually useful in agent answers and adjusts
retrieval order accordingly. Two concerns:

1. **Live rerank** (``RetrievalRL.rerank``, called by /cache_nodes): scores
   the candidates of one search with the loaded RLModel from the vectors the
   client sends (``rl_client.rerank_wire``: query vector, node vectors,
   linked vectors, node types) and returns the reranked order + scores.

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
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import numpy as np  # a hard dependency of the image (rl_model needs it too)

if TYPE_CHECKING:
    from .rl_model import RLModel
    from rl_client.rl_logger import RLDataLogger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RetrievalRL
# ---------------------------------------------------------------------------

class RetrievalRL:
    """Persisted REINFORCE-lite reranker for KG retrieval.

    Two concerns:

    * **live rerank** (:meth:`rerank`, called by ``/cache_nodes``): scores the
      candidates from the vectors the client sends (``rl_client.rerank_wire``).
      The container does no embedding of its own (v0.2.106: the pre-v0.2.106
      path embedded the query through an injected ``embedding_fn`` that no
      caller ever wired, so it always returned the search order);
    * **pure-train** (``schedule_update`` -> ``_update``, called by
      ``/rl_update``): MCP sends pre-packed payloads and the container applies
      the unified-target formula. MCP is the sole citation and retrieval-event
      writer.

    Args:
        project_root: Project root dir.
        ollama_url:   Accepted for the server's ``--ollama-url`` option; the
                      container no longer embeds anything (see above).
        rl_model:     The RLModel that scores (:meth:`rerank`) and trains
                      (``_update``).
    """

    def __init__(
        self,
        project_root: Path,
        ollama_url: str = "http://localhost:11435",
        rl_model: "RLModel | None" = None,
        data_logger: "RLDataLogger | None" = None,
        rl_server_url: str | None = None,
    ) -> None:
        self._root = Path(project_root)
        self._ollama_url = ollama_url.rstrip("/")
        # When rl_server_url is set, ``_delegate_update_to_server`` targets
        # it (a parent/child pattern kept for a one-release deprecation
        # window; no current caller sets it).
        self._rl_server_url = rl_server_url.rstrip("/") if rl_server_url else None
        self._rl_model = rl_model if not self._rl_server_url else None
        self._data_logger = data_logger

        if rl_model:
            logger.info("RetrievalRL: RLModel enabled for query-aware scoring")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
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
        encountered during :meth:`rerank`.  Callers (e.g. ContextBuilder)
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

    def rerank(self, features: Any, n_nodes: int, *, profile: str) -> "RerankScores":
        """Score the candidates of one search with the loaded model.

        ``features`` is the request's ``features`` field
        (``rl_client.rerank_wire``): the query vector plus, per candidate, the
        node vector, its linked vectors and the type names — the same vectors
        the client logs on the retrieval event and the trainer learns from.
        The inputs are assembled by ``RLModel._build_inputs`` (through
        ``score_batch``), the serving twin of the trainer's
        ``shard_trainer.build_inputs`` (parity test:
        tests/rl_reranker/test_serve_time_scoring.py). For a profile in
        ``LINKED_SLOTS_MASKED_PROFILES`` the linked slots are left empty,
        exactly as the trainer masks them.

        A candidate without a vector is not scored and keeps its position;
        the scored candidates are reordered among the positions they occupy
        (highest score first, ties in search order). Nothing is dropped: the
        model was evaluated on ordering (NDCG / MRR), not on a cut-off.

        Raises ``ValueError`` on malformed features (the server answers 422)
        and ``RuntimeError`` when no model is loaded.
        """
        from rl_client.rerank_wire import decode_rerank_features

        from .rl_model import linked_slots_masked

        model = self._rl_model
        if model is None:
            raise RuntimeError("no RL model loaded")
        decoded = decode_rerank_features(features)
        if len(decoded.nodes) != n_nodes:
            raise ValueError(
                f"features.nodes has {len(decoded.nodes)} entries for {n_nodes} nodes"
            )
        if decoded.dim != model.emb_dim:
            raise ValueError(
                f"feature dim {decoded.dim} != model emb_dim {model.emb_dim}"
            )
        masked = linked_slots_masked(profile)
        candidates, scored_idx = serving_candidates(model, decoded.nodes, masked=masked)
        scores = model.score_batch(decoded.query, candidates) if candidates else []
        per_node: "list[float | None]" = [None] * n_nodes
        for i, sc in zip(scored_idx, scores):
            per_node[i] = float(sc)
        return RerankScores(
            order=order_by_scores(n_nodes, scored_idx, scores),
            scores=per_node,
            linked_slots_masked=masked,
        )


def serving_candidates(
    model: "RLModel", nodes: "list[Any]", *, masked: bool,
) -> "tuple[list[tuple], list[int]]":
    """``RLModel.score_batch`` inputs for the decoded candidates that carry a
    vector, and their request indices.

    Types map like the training export: the node type through
    ``get_type_idx`` (empty -> ``concept``, the export's code 0), a linked
    type name through ``get_type_idx`` (empty -> ``UNKNOWN_TYPE_IDX``, the
    export's empty code); a name outside the builtins gets a fresh row
    initialised to the mean of the builtins, the row the trainer uses for
    every non-builtin type. ``masked`` leaves every linked slot empty.
    """
    from .rl_model import UNKNOWN_TYPE_IDX

    candidates: list[tuple] = []
    scored_idx: list[int] = []
    for i, nf in enumerate(nodes):
        if nf.vec is None:
            continue
        linked = [] if masked else list(nf.linked)
        linked_types = (
            [model.get_type_idx(t) if t else UNKNOWN_TYPE_IDX for t in nf.linked_types]
            if linked else None
        )
        candidates.append((
            nf.vec, linked, model.get_type_idx(nf.node_type or "concept"), linked_types,
        ))
        scored_idx.append(i)
    return candidates, scored_idx


def order_by_scores(n: int, scored_idx: "list[int]", scores: "list[float]") -> "list[int]":
    """Permutation of ``range(n)``: the scored positions refilled by score.

    Unscored positions keep their node; the positions the scored nodes
    occupy are refilled highest score first, ties broken by search order.
    """
    order = list(range(n))
    ranked = sorted(zip(scored_idx, scores), key=lambda p: (-p[1], p[0]))
    for pos, (idx, _sc) in zip(sorted(scored_idx), ranked):
        order[pos] = idx
    return order


class RerankScores:
    """Output of :meth:`RetrievalRL.rerank`.

    ``order``: request-node indices, best first (a permutation).
    ``scores``: model score per request node (``None`` = not scored).
    """

    __slots__ = ("order", "scores", "linked_slots_masked")

    def __init__(
        self,
        order: "list[int]",
        scores: "list[float | None]",
        linked_slots_masked: bool,
    ) -> None:
        self.order = order
        self.scores = scores
        self.linked_slots_masked = linked_slots_masked
