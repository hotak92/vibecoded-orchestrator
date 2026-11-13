# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pin ``literal_cited`` as a TRAINING SIGNAL of the online path.

User directive 2026-06-04: "literal citation as a training signal for our
own reranking neural network is for this patch". The guard: a refactor must
never drop ``literal_cited`` from the path that reaches the gradient step.

v0.2.106: that path is ``online_learning.labelled_query`` (the trainer's
target for each node) feeding ``ProjectLearner`` rounds with the offline
objective. The target is ``compute_unified_targets`` — the one copy — so a
literal citation raises it by the unified bonus (×1.5 then +0.1, clamped).
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("torch")

from rl_reranker.online_learning import (  # noqa: E402
    OnlineConfig,
    ProjectLearner,
    VectorStore,
    labelled_query,
)
from vco_lib.rl_training_targets import compute_unified_targets  # noqa: E402

D = 8


def _task(cos, literal=None, cross=None):
    rng = np.random.default_rng(3)
    return {
        "nodes_packed": [{"title": t, "node_type": "concept",
                          "n_emb": rng.normal(size=D).tolist()} for t in cos],
        "query_emb": rng.normal(size=D).tolist(),
        "cosine_sims": cos,
        "literal_cited": literal or {},
        "cross_encoder_cited": cross,
    }


def _targets(task) -> dict[str, float]:
    q, _ = labelled_query(task, VectorStore(D), profile="qwen3", ts_ms=0)
    assert q is not None
    return {t: float(v) for t, v in zip(task["cosine_sims"], q.target)}


class TestLiteralCitedAsTrainingSignal:
    def test_literal_cited_true_yields_higher_target_than_false(self):
        cos = {"A": 0.40, "B": 0.55}
        off = _targets(_task(cos, {"A": False}))
        on = _targets(_task(cos, {"A": True}))
        assert on["A"] > off["A"]
        assert on["B"] == pytest.approx(off["B"])

    def test_target_matches_compute_unified_targets_exactly(self):
        cos = {"A": 0.40, "B": 0.55, "C": 0.2}
        lit = {"B": True}
        expected, _ = compute_unified_targets(cos, literal_cited=lit)
        got = _targets(_task(cos, lit))
        for t in cos:
            assert got[t] == pytest.approx(expected[t], abs=1e-6)

    def test_single_node_literal_bonus_value(self):
        # cosine 0.40 alone: step 1 -> 0.45; literal: *1.5 -> 0.675, +0.1 -> 0.775
        assert _targets(_task({"A": 0.40}, {"A": True}))["A"] == pytest.approx(0.775, abs=1e-6)

    def test_cross_encoder_cited_also_boosts(self):
        cos = {"A": 0.40, "B": 0.55}
        base = _targets(_task(cos))
        boosted = _targets(_task(cos, cross={"A": True}))
        assert boosted["A"] > base["A"]

    def test_the_literal_flag_travels_with_the_sample(self):
        q, _ = labelled_query(_task({"A": 0.4, "B": 0.5}, {"B": True}), VectorStore(D),
                              profile="qwen3", ts_ms=0)
        assert q is not None and list(q.literal) == [False, True] and q.cites


class TestLiteralCitedDefaultsToFalse:
    def test_node_missing_from_literal_cited_dict_gets_no_boost(self):
        cos = {"A": 0.4, "B": 0.5}
        assert _targets(_task(cos, {"B": False})) == pytest.approx(_targets(_task(cos)))

    def test_empty_literal_cited_equals_no_boost(self):
        cos = {"A": 0.4}
        expected, _ = compute_unified_targets(cos)
        assert _targets(_task(cos, {}))["A"] == pytest.approx(expected["A"], abs=1e-6)


class TestSkipTrainStillRespected:
    def test_training_off_events_are_scored_but_never_trained(self, tmp_path):
        """``rl_online_training_disabled``: the event is judged, not trained on."""
        from rl_reranker.rl_model import RLModel

        cfg = OnlineConfig(round_every=6, batch_queries=1, max_steps_per_round=4)
        lr = ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
        for i in range(6):
            assert lr.ingest(_task({"A": 0.4, "B": 0.6}, {"B": True}), ts_ms=i, train=False)
        assert lr.status()["waiting_to_score"] == 6
        m = RLModel(emb_dim=D, state_path=tmp_path / "rl.pt", device="cpu",
                    embedding_source="qwen3")
        lr.run_round_now(m, now_ms=6)
        assert len(lr.record) == 6                    # scored for the gate ...
        assert lr.status()["pending_train_queries"] == 0
        assert lr.counters["dropped_untrained"] == 6  # ... never trained
        assert lr.counters["queries_trained"] == 0 and m.local_train_steps == 0
        assert lr.status()["training"].startswith("off")
