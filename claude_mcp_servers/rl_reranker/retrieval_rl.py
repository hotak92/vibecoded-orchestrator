# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The RL reranker's live rerank of one search.

``RetrievalRL.rerank`` (called by ``/cache_nodes`` once the project's model
is served, see below) scores the candidates of one search with the loaded
RLModel from the vectors the client sends (``rl_client.rerank_wire``: query
vector, node vectors, linked vectors, node types) and returns the reranked
order + scores.

Training is NOT here any more (v0.2.106, owner ruling 2026-11-12: per-project
online adaptation, opt-in). The pre-v0.2.106 online path — ``schedule_update``
-> ``_update`` -> ``RLModel.update``, one BCE step per node at lr 1e-3, fired
as a task that ran after the routing lock was released and could therefore
train whichever project's model was swapped in by then — is replaced by
``online_learning.ProjectLearner``: per project, in rounds, with the offline
objective (``objective.batch_loss``), behind a serving gate judged on the
project's cumulative prequential record. The
unified target formula (``vco_lib.rl_training_targets``) is still the one
copy, applied by ``online_learning.labelled_query``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .rl_model import RLModel
    from rl_client.rl_logger import RLDataLogger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RetrievalRL
# ---------------------------------------------------------------------------

class RetrievalRL:
    """Holder of the model currently swapped in by the server's routing.

    :meth:`rerank` scores the candidates from the vectors the client sends
    (``rl_client.rerank_wire``); the container embeds nothing of its own.

    Args:
        project_root: Project root dir.
        rl_model:     The RLModel that scores (:meth:`rerank`).
    """

    def __init__(
        self,
        project_root: Path,
        rl_model: "RLModel | None" = None,
        data_logger: "RLDataLogger | None" = None,
    ) -> None:
        self._root = Path(project_root)
        self._rl_model = rl_model
        self._data_logger = data_logger

        if rl_model:
            logger.info("RetrievalRL: RLModel enabled for query-aware scoring")

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
