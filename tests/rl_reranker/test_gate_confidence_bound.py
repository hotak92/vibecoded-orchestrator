# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The serving gate judges paired-bootstrap confidence bounds (v0.2.106).

Owner ruling 2026-11-12: a project's model is served only when a PAIRED
BOOTSTRAP over its held-out window says, at 90% confidence (one-sided), that
(a) the NDCG@10 gain over plain cosine is >= +2% AND (b) the cited node's
MRR@10 minus cosine's is >= 0. A replay had shown the point check passing by
noise on windows of 20-50 citing queries.

The windows here are built at the metric level: ``online_learning.evaluate``
reads the table's gains / literal flags / query layout, and the two scorers
are replaced by fixed per-node scores, so each test states exactly which
queries the model ranks better or worse. ``evaluate`` + ``gate_decision`` is
the path ``compute_round`` / ``apply_round`` take after every round.
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

pytest.importorskip("torch")

from rl_reranker import online_learning as ol  # noqa: E402
from rl_reranker import ranking_metrics as rm  # noqa: E402

N_NODES = 6
#: Gains in cosine order: cosine ranks the most useful node (index 1) second.
GAINS = np.array([0.3, 1.0, 0.5, 0.4, 0.2, 0.1])
COS = np.arange(N_NODES, 0, -1, dtype=np.float64)          # cosine order = index order
MODEL_BETTER = np.array([5, 6, 4, 3, 2, 1], dtype=np.float64)   # node 1 first
MODEL_CITED_LOW = np.array([4, 6, 5, 3, 2, 1], dtype=np.float64)  # node 0 third


def _window(queries: "list[tuple[np.ndarray, int | None]]", monkeypatch):
    """A held-out table of ``queries`` = (model scores, cited node or None);
    cosine scores are the index order for every query."""
    n = len(queries)
    count = np.full(n, N_NODES, dtype=np.int64)
    first = np.arange(n, dtype=np.int64) * N_NODES
    lit = np.zeros(n * N_NODES, dtype=bool)
    for i, (_m, cited) in enumerate(queries):
        if cited is not None:
            lit[i * N_NODES + cited] = True
    tbl = cast(Any, SimpleNamespace(
        np_target=np.tile(GAINS, n).astype(np.float32), np_literal=lit,
        np_search_pos=np.tile(np.arange(N_NODES), n), first=first, count=count))
    model = np.concatenate([m for m, _c in queries])
    monkeypatch.setattr(ol, "score_samples", lambda _t, _net, sidx: model[sidx])
    monkeypatch.setattr(ol, "cosine_scores", lambda _t, sidx: np.tile(COS, n)[sidx])
    return tbl


def _noisy_mrr_window() -> "list[tuple[np.ndarray, int | None]]":
    """300 queries; the model clearly wins NDCG on the 275 non-citing ones.
    Of the 25 citing queries (cited = node 1 or node 0), the model lifts the
    cited node from rank 2 to 1 on 15 (+0.5) and drops it from rank 1 to 3
    on 10 (-0.667): mean difference +0.033 — the point check passes — but a
    10-query swing in 25 is noise, and the 90% lower bound is below 0."""
    qs: list = [(MODEL_BETTER, None)] * 275
    qs += [(MODEL_BETTER, 1)] * 15          # cosine rank 2 -> model rank 1
    qs += [(MODEL_CITED_LOW, 0)] * 10       # cited node 0: cosine rank 1 -> model rank 3
    return qs


def _clearly_better_window() -> "list[tuple[np.ndarray, int | None]]":
    qs: list = [(MODEL_BETTER, None)] * 275
    qs += [(MODEL_BETTER, 1)] * 25
    return qs


CFG = ol.OnlineConfig()  # shipped defaults: 300 / 20 cited / +2% / 90% / 1000 resamples


def _gate(qs, monkeypatch, *, seed: int = 3, cfg: ol.OnlineConfig = CFG):
    tbl = _window(qs, monkeypatch)
    ev = ol.evaluate(None, tbl, cfg, 0, seed=seed)
    return ev, ol.gate_decision(ev, cfg)


# ─── the red-proof: noise passes the point check, not the bound ───────


def _noisy_ndcg_window() -> "list[tuple[np.ndarray, int | None]]":
    """NDCG +2.5% on the point estimate, carried by 62 of 300 queries
    (+17.6% each) against 30 that lose (-11.6% each: the most useful node to
    the bottom) — the 90% lower bound is under +2%. The cited-node half
    passes (cited node lifted on all 20 citing queries)."""
    worse = np.array([6, 1, 5, 4, 3, 2], dtype=np.float64)
    qs: list = [(MODEL_BETTER, None)] * 42 + [(worse, None)] * 30 + [(COS, None)] * 208
    qs += [(MODEL_BETTER, 1)] * 20
    return qs


@pytest.mark.parametrize("window, serve", [
    (_noisy_mrr_window, False),
    (_noisy_ndcg_window, False),
    (_clearly_better_window, True),
])
def test_the_gate_serves_on_the_bound_not_the_point_estimate(window, serve, monkeypatch):
    """Decision only (the attribute-free view the pre-bound gate also has):
    both noisy windows pass the point check, so the pre-bound gate served
    them; the clearly better window is served by both."""
    _ev, (got, why) = _gate(window(), monkeypatch)
    assert got is serve, why


def test_a_noisy_window_passes_the_point_check_but_not_the_bound(monkeypatch):
    ev, (serve, why) = _gate(_noisy_mrr_window(), monkeypatch)
    # the point estimates clear the bar (what the pre-bound gate served on) ...
    assert ev.n_queries == 300 and ev.n_cited == 25
    assert ev.ndcg_gain >= CFG.min_ndcg_gain
    assert ev.mrr >= ev.cos_mrr
    # ... the bound does not: the project keeps its own search order
    assert ev.ndcg_gain_lb >= CFG.min_ndcg_gain
    assert ev.mrr_diff_lb < 0.0
    assert serve is False, why
    assert "lower bound of the difference" in why and "below 0" in why


def test_a_clearly_better_window_is_served(monkeypatch):
    ev, (serve, why) = _gate(_clearly_better_window(), monkeypatch)
    assert ev.mrr > ev.cos_mrr and ev.ndcg_gain > CFG.min_ndcg_gain
    assert ev.ndcg_gain_lb >= CFG.min_ndcg_gain and ev.mrr_diff_lb >= 0.0
    assert serve is True, why
    assert "over plain cosine" in why and "90% lower bound" in why


def test_a_noisy_ndcg_gain_is_refused_on_its_bound(monkeypatch):
    ev, (serve, why) = _gate(_noisy_ndcg_window(), monkeypatch)
    assert ev.mrr_diff_lb >= 0.0, ev
    assert ev.ndcg_gain >= CFG.min_ndcg_gain, ev
    assert ev.ndcg_gain_lb < CFG.min_ndcg_gain, ev
    assert serve is False and "the bound needs +2.00%" in why, why


# ─── determinism, pairing ─────────────────────────────────────────────


def test_the_check_is_deterministic_per_seed(monkeypatch):
    qs = _noisy_mrr_window()
    a, _ = _gate(qs, monkeypatch, seed=11)
    b, _ = _gate(qs, monkeypatch, seed=11)
    assert a == b
    m = rm.QueryMetrics(ndcg=np.linspace(0.5, 1, 40), mrr=np.r_[np.ones(10), np.full(30, np.nan)],
                        recall=np.zeros(40))
    c = rm.QueryMetrics(ndcg=np.linspace(0.6, 0.9, 40)[::-1],
                        mrr=np.r_[np.full(10, 0.5), np.full(30, np.nan)], recall=np.zeros(40))
    x = rm.paired_bootstrap_bounds(m, c, n_boot=500, seed=1)
    assert x == rm.paired_bootstrap_bounds(m, c, n_boot=500, seed=1)
    assert x != rm.paired_bootstrap_bounds(m, c, n_boot=500, seed=2)


def test_the_resample_is_paired(monkeypatch):
    """Model == cosine on every query: each resample's gain and difference
    are exactly 0 — an unpaired bootstrap would spread them both ways."""
    qs: list = [(COS, None)] * 280 + [(COS, 1)] * 20
    ev, (serve, why) = _gate(qs, monkeypatch)
    assert ev.ndcg_gain_lb == 0.0 and ev.mrr_diff_lb == 0.0
    assert serve is False and "the bound needs +2.00%" in why


def test_the_round_seed_reaches_the_bootstrap(tmp_path, monkeypatch):
    """compute_round evaluates with the round's seed (cfg.seed + rounds)."""
    seen = []
    real = rm.paired_bootstrap_bounds

    def spy(m, c, **kw):
        seen.append(kw["seed"])
        return real(m, c, **kw)

    monkeypatch.setattr(ol, "paired_bootstrap_bounds", spy)
    tbl = _window(_clearly_better_window(), monkeypatch)
    job = ol.RoundJob(generation=0, net=None, opt_state={}, profile="qwen3", device=None,
                      train=None, steps=[], holdout=tbl, seed=CFG.seed + 7, cfg=CFG, now_ms=1)
    res = ol.compute_round(job)
    assert seen == [CFG.seed + 7] and res.evaluation is not None


# ─── the citing subset ────────────────────────────────────────────────


def test_no_citing_query_in_a_resample_is_a_failing_resample():
    """One citing query in 300: ~37% of the resamples miss it, and each of
    those counts as failing — the bound is -inf even though that one query's
    difference is +1."""
    n = 300
    m = rm.QueryMetrics(ndcg=np.full(n, 0.9), mrr=np.r_[1.0, np.full(n - 1, np.nan)],
                        recall=np.zeros(n))
    c = rm.QueryMetrics(ndcg=np.full(n, 0.8), mrr=np.r_[0.0, np.full(n - 1, np.nan)],
                        recall=np.zeros(n))
    b = rm.paired_bootstrap_bounds(m, c, n_boot=1000, seed=5)
    assert 250 <= b.empty_cited_resamples <= 480
    assert b.mrr_diff_lb == float("-inf")
    assert b.ndcg_gain_lb == pytest.approx(0.125)


def test_a_window_with_no_citing_query_never_passes(monkeypatch):
    cfg = ol.OnlineConfig(min_cited_queries=0)
    ev, (serve, why) = _gate([(MODEL_BETTER, None)] * 300, monkeypatch, cfg=cfg)
    assert ev.n_cited == 0 and ev.mrr_diff_lb == float("-inf")
    assert serve is False and "n/a" in why


def test_an_empty_window_has_failing_bounds():
    e = rm.QueryMetrics(ndcg=np.zeros(0), mrr=np.zeros(0), recall=np.zeros(0))
    b = rm.paired_bootstrap_bounds(e, e)
    assert b.ndcg_gain_lb == b.mrr_diff_lb == float("-inf")
    with pytest.raises(ValueError):
        rm.paired_bootstrap_bounds(e, rm.QueryMetrics(ndcg=np.zeros(1), mrr=np.zeros(1),
                                                      recall=np.zeros(1)))


# ─── persistence + the status surfaces ────────────────────────────────


def test_bounds_round_trip_and_an_old_state_loads_as_failing(monkeypatch):
    ev, _ = _gate(_noisy_mrr_window(), monkeypatch)
    d = ev.as_dict()
    assert d["mrr_diff_lb"] == pytest.approx(ev.mrr_diff_lb) and d["confidence"] == 0.9
    assert ol.Evaluation.from_dict(d) == ev
    # -inf (undefined) is strict-JSON None and comes back as -inf (still failing)
    ev2, _ = _gate([(MODEL_BETTER, None)] * 300, monkeypatch,
                   cfg=ol.OnlineConfig(min_cited_queries=0))
    d2 = ev2.as_dict()
    assert d2["mrr_diff_lb"] is None
    back2 = ol.Evaluation.from_dict(d2)
    assert back2 is not None and back2.mrr_diff_lb == float("-inf")
    assert back2.ndcg_gain_lb == ev2.ndcg_gain_lb
    # a state written before the bound existed: point metrics only -> not served
    old = {k: v for k, v in _clearly_better_eval(monkeypatch).as_dict().items()
           if k not in ("ndcg_gain_lb", "mrr_diff_lb", "confidence", "n_boot")}
    back = ol.Evaluation.from_dict(old)
    assert back is not None and back.ndcg_gain_lb == float("-inf")
    assert ol.gate_decision(back, CFG)[0] is False


def _clearly_better_eval(monkeypatch) -> ol.Evaluation:
    ev, _ = _gate(_clearly_better_window(), monkeypatch)
    return ev


def test_the_status_names_the_bound(monkeypatch):
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=4)
    st = lr.status()
    assert st["bar"]["judged_on"] == "paired-bootstrap lower bounds"
    assert st["bar"]["confidence"] == 0.9 and st["bar"]["bootstrap_samples"] == 1000
    lr.last_eval = _clearly_better_eval(monkeypatch)
    ev = lr.status()["last_eval"]
    assert ev["ndcg_gain_lb"] > 0.02 and ev["mrr_diff_lb"] >= 0 and ev["n_cited"] == 25


def test_shipped_defaults_cite_the_ruling():
    cfg = ol.OnlineConfig()
    assert (cfg.confidence, cfg.bootstrap_samples, cfg.min_ndcg_gain) == (0.90, 1000, 0.02)
    assert rm.GATE_CONFIDENCE == 0.90 and rm.GATE_BOOTSTRAP_SAMPLES == 1000


# ─── bounded cost ─────────────────────────────────────────────────────


def test_the_check_is_cheap_on_a_full_window():
    """2,000-query window (the cap), 1,000 resamples: the check runs in the
    background round, but stays far below a round's training time."""
    rng = np.random.default_rng(0)
    n = 2000
    m = rm.QueryMetrics(ndcg=rng.random(n), mrr=np.where(rng.random(n) < 0.1, rng.random(n), np.nan),
                        recall=np.zeros(n))
    c = rm.QueryMetrics(ndcg=rng.random(n), mrr=np.where(np.isnan(m.mrr), np.nan, rng.random(n)),
                        recall=np.zeros(n))
    t0 = time.perf_counter()
    b = rm.paired_bootstrap_bounds(m, c, n_boot=1000, seed=1)
    took = time.perf_counter() - t0
    assert math.isfinite(b.ndcg_gain_lb) and math.isfinite(b.mrr_diff_lb)
    assert took < 5.0, took
