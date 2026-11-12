# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pin the pure-train contract of the container's online path.

v0.2.9 (C8) made the container pure-train: the MCP posts pre-packed citation
events (``RLClient.rl_update_v3``) and the container embeds nothing, writes no
data-logger event, and trains on the frozen retrieval-time vectors. v0.2.106
(owner 2026-11-12, per-project online adaptation) moved that path from
``RetrievalRL._update`` (one per-node BCE step per node) to
``online_learning.labelled_query`` + ``ProjectLearner`` (the offline
objective, in rounds). The contract is unchanged and pinned on the new home:

1. the pre-packed payload shape is accepted (no agent_output string);
2. no embedding call and no data-logger write;
3. ``n_emb`` / ``linked_embs`` / ``linked_type_names`` / ``node_type`` are
   taken from the payload as sent (vectors as float16, the store's precision);
4. one sample per node that has an answer cosine AND a vector; nodes without
   either are skipped; an event with none is skipped with a reason.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("torch")

from rl_reranker import retrieval_rl  # noqa: E402
from rl_reranker.online_learning import (  # noqa: E402
    OnlineConfig,
    ProjectLearner,
    VectorStore,
    labelled_query,
)
from rl_reranker.rl_model import (  # noqa: E402
    MAX_LINKED,
    NODE_TYPES,
    NULL_TYPE_IDX,
    TYPE_CODE_OTHER,
    UNKNOWN_TYPE_IDX,
)

D = 8


def _vec(seed: int) -> list[float]:
    return np.random.default_rng(seed).normal(size=D).astype(np.float32).tolist()


def _task(**over):
    task = {
        "nodes_packed": [
            {"title": "A", "node_type": "concept", "n_emb": _vec(1),
             "linked_embs": [_vec(2), [], _vec(3)], "linked_type_names": ["tool", "", "pattern"]},
            {"title": "B", "node_type": "pattern", "n_emb": _vec(4),
             "linked_embs": [], "linked_type_names": []},
            {"title": "C", "node_type": "tool", "n_emb": _vec(5)},
        ],
        "query_emb": _vec(0),
        "cosine_sims": {"A": 0.6, "B": 0.4, "C": 0.5},
        "literal_cited": {"A": True},
        "cross_encoder_cited": None,
    }
    task.update(over)
    return task


def test_the_container_online_path_embeds_nothing_and_logs_nothing():
    """The old per-node update path and its embedding hooks are gone."""
    rl = retrieval_rl.RetrievalRL
    for gone in ("schedule_update", "_update", "_delegate_update_to_server", "_embed"):
        assert not hasattr(rl, gone), gone
    names = labelled_query.__code__.co_names
    assert "log_citations" not in names and "log_retrieval" not in names


def test_vectors_and_types_are_taken_from_the_payload():
    store = VectorStore(D)
    q, why = labelled_query(_task(), store, profile="qwen3", ts_ms=5)
    assert q is not None and why == ""
    assert q.n == 3  # one sample per node with a target, in the citation's order
    np.testing.assert_allclose(store.take(q.node_rows[:1])[0].astype(np.float32),
                               np.float16(_vec(1)).astype(np.float32))
    # node types through the export's type codes
    assert list(q.node_types) == [0, TYPE_CODE_OTHER, NODE_TYPES.index("tool")]
    # linked: present vectors packed from slot 0 (the empty one skipped)
    row = q.linked_rows[0]
    assert (row[:2] >= 0).all() and (row[2:] == -1).all()
    assert list(q.linked_types[0][:2]) == [NODE_TYPES.index("tool"), TYPE_CODE_OTHER]
    assert (q.linked_types[0][2:] == NULL_TYPE_IDX).all()
    # nodes with no linked fields at all carry none
    assert (q.linked_rows[1] == -1).all() and (q.linked_rows[2] == -1).all()
    assert q.linked_rows.shape == (3, MAX_LINKED)
    # search position = position in nodes_packed
    assert list(q.search_pos) == [0, 1, 2]
    assert q.ts_ms == 5


def test_unknown_linked_type_name_maps_to_unknown():
    t = _task()
    t["nodes_packed"][0]["linked_type_names"] = ["", "", ""]
    q, _ = labelled_query(t, VectorStore(D), profile="qwen3", ts_ms=0)
    assert q is not None and q.linked_types[0][0] == UNKNOWN_TYPE_IDX


def test_arctic_events_carry_no_linked_slots():
    """The linked-slot mask applies to training data exactly as to serving."""
    q, _ = labelled_query(_task(), VectorStore(D), profile="arctic", ts_ms=0)
    assert q is not None and (q.linked_rows == -1).all()


def test_nodes_without_a_target_or_a_vector_are_skipped():
    t = _task(cosine_sims={"A": 0.6, "C": 0.5})  # B has no answer cosine
    t["nodes_packed"][2]["n_emb"] = [1.0] * (D + 1)  # C has a wrong-width vector
    q, _ = labelled_query(t, VectorStore(D), profile="qwen3", ts_ms=0)
    assert q is not None and q.n == 1


@pytest.mark.parametrize("over, why", [
    ({"nodes_packed": []}, "no node carries both a target and a vector"),
    ({"cosine_sims": {}}, "no answer cosines"),
    ({"query_emb": None}, "query vector missing or of the wrong width"),
    ({"query_emb": [0.1] * (D + 2)}, "query vector missing or of the wrong width"),
])
def test_unusable_events_are_skipped_with_the_reason(over, why):
    lr = ProjectLearner(profile="qwen3", emb_dim=D, cfg=OnlineConfig())
    assert lr.ingest(_task(**over), ts_ms=0) is False
    assert lr.counters["events_skipped"] == 1 and lr.counters["events_seen"] == 0
    assert lr.last_skip_reason == why
