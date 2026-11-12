# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The serving gate judges the CUMULATIVE PREQUENTIAL record (v0.2.106).

Owner ruling 2026-11-12 (after the rlgate2 study): a project's model is served
only while its record — every query scored by the model in place when it
arrived, before that model trained on it — shows, at an anytime-valid
confidence (the k-th check at ``1 - 0.10 / (k (k+1))``), that (a) the NDCG@10
gain over plain cosine is >= +2% AND (b) the cited node's MRR@10 minus
cosine's is >= 0. The held-out window it replaces, re-tested every round,
passed on lucky windows whose gain the following queries did not show.

The streams here run through the REAL learner (``ProjectLearner``: ingest,
rounds, the gate, persistence) with the per-query metrics fixed at the metric
layer: each event's query vector carries its stream index, the two scorers
return that index, and ``per_query_metrics`` returns the stream's chosen
values for it. That is the path both the windowed gate and this one take
(``score_samples`` / ``cosine_scores`` / ``per_query_metrics`` are module
globals of ``online_learning`` in both), so the lucky-window and the
consistent-model streams are a red-proof against the windowed gate as well
as a specification of this one.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from rl_reranker import online_learning as ol  # noqa: E402
from rl_reranker import ranking_metrics as rm  # noqa: E402
from rl_reranker.rl_model import RLModel  # noqa: E402
from rl_reranker.rl_server import _seeded_init  # noqa: E402

D = 8
N_NODES = 4


def _model(tmp_path: Path, name: str = "m") -> RLModel:
    with _seeded_init(7, enabled=True):
        return RLModel(emb_dim=D, state_path=tmp_path / name / "rl_model_qwen3_local.pt",
                       device="cpu", embedding_source="qwen3")


def _event(idx: int, cites: bool) -> dict:
    """Event ``idx``: its query vector carries ``idx`` exactly (two float16
    integers), its nodes are arbitrary; it cites node 0 when ``cites``."""
    rng = np.random.default_rng(idx)
    q = np.zeros(D, dtype=np.float32)
    q[0], q[1], q[2] = idx // 1024, idx % 1024, 1.0
    nodes = []
    for i in range(N_NODES):
        v = rng.normal(size=D).astype(np.float32)
        nodes.append({"title": f"n{i}", "node_type": "concept", "n_emb": v.tolist(),
                      "linked_embs": [], "linked_type_names": []})
    return {"nodes_packed": nodes, "query_emb": q.tolist(),
            "cosine_sims": {f"n{i}": 0.9 - 0.1 * i for i in range(N_NODES)},
            "literal_cited": {"n0": True} if cites else {},
            "cross_encoder_cited": None}


class Stream:
    """Per-query metrics chosen by the test: ``ndcg[i]`` / ``mrr[i]`` for the
    model and for plain cosine (NaN MRR = the query cites nothing)."""

    def __init__(self, m_ndcg, c_ndcg, m_mrr, c_mrr):
        self.m_ndcg, self.c_ndcg = np.asarray(m_ndcg), np.asarray(c_ndcg)
        self.m_mrr, self.c_mrr = np.asarray(m_mrr), np.asarray(c_mrr)

    def __len__(self) -> int:
        return len(self.m_ndcg)

    def cites(self, i: int) -> bool:
        return not np.isnan(self.m_mrr[i])

    def install(self, monkeypatch) -> None:
        """Model scores = the query's stream index + 0.5, cosine = -(1e6 +
        index), search = -position (<= 0): ``per_query_metrics`` reads which
        ranker and which query from the first score of each query."""
        def idx_of(tbl, sidx):
            v = tbl.vectors[tbl.q_vec[torch.from_numpy(np.asarray(sidx))]].float().cpu().numpy()
            return (v[:, 0] * 1024 + v[:, 1]).round().astype(np.int64)

        monkeypatch.setattr(ol, "score_samples",
                            lambda tbl, _net, sidx, **_kw: idx_of(tbl, sidx) + 0.5)
        monkeypatch.setattr(ol, "cosine_scores",
                            lambda tbl, sidx, **_kw: -(1e6 + idx_of(tbl, sidx)))

        def fake_pqm(scores, gains, relevant, first, count, k=10):
            n = len(first)
            out = rm.QueryMetrics(ndcg=np.full(n, np.nan), mrr=np.full(n, np.nan),
                                  recall=np.full(n, np.nan))
            for i in range(n):
                s = float(scores[int(first[i])])
                if s > 0:          # the model
                    j = int(s - 0.5)
                    out.ndcg[i], out.mrr[i] = self.m_ndcg[j], self.m_mrr[j]
                elif s <= -1e6:    # plain cosine
                    j = int(round(-s - 1e6))
                    out.ndcg[i], out.mrr[i] = self.c_ndcg[j], self.c_mrr[j]
                else:              # the search order: cosine's values
                    out.ndcg[i], out.mrr[i] = 0.5, (np.nan if not relevant[
                        int(first[i]):int(first[i] + count[i])].any() else 0.5)
            return out

        monkeypatch.setattr(ol, "per_query_metrics", fake_pqm)


def _stream(n: int, *, gain, diff, cite_every: int = 4, seed: int = 0,
            noise: float = 0.05) -> Stream:
    """``gain`` / ``diff``: per-query arrays (or scalars) of the model's
    relative NDCG gain and cited-MRR difference over cosine, plus noise."""
    rng = np.random.default_rng(seed)
    gain = np.broadcast_to(np.asarray(gain, dtype=np.float64), (n,))
    diff = np.broadcast_to(np.asarray(diff, dtype=np.float64), (n,))
    c_ndcg = np.full(n, 0.6)
    m_ndcg = c_ndcg * (1.0 + gain + noise * rng.normal(size=n))
    cited = (np.arange(n) % cite_every) == 0
    c_mrr = np.where(cited, 0.5, np.nan)
    m_mrr = np.where(cited, 0.5 + diff + 2 * noise * rng.normal(size=n), np.nan)
    return Stream(m_ndcg, c_ndcg, m_mrr, c_mrr)


def _drive(stream: Stream, tmp_path: Path, monkeypatch, cfg=None):
    """Feed ``stream`` through a learner with the shipped gate; returns the
    learner, the model and, per event, whether the model was served then."""
    stream.install(monkeypatch)
    cfg = cfg or ol.OnlineConfig()
    lr = ol.ProjectLearner(profile="qwen3", emb_dim=D, cfg=cfg)
    m = _model(tmp_path)
    served = []
    for i in range(len(stream)):
        served.append(lr.serving)
        lr.ingest(_event(i, stream.cites(i)), ts_ms=i)
        if lr.round_due():
            lr.run_round_now(m, now_ms=i)
    return lr, m, np.array(served)


def _lucky_window_stream() -> Stream:
    """2,400 queries, one in four cites a node. The model gains NDCG
    throughout (+3%) but LOSES the cited node on the first 2,000 queries
    (MRR -0.2); the last 400 are a lucky block (+0.3, +5%). A held-out window
    of the newest 300 queries that lies in the block passes its bounds; the
    whole record does not (cited-MRR difference about -0.12)."""
    n = 2400
    lucky = np.arange(n) >= 2000
    return _stream(n, gain=np.where(lucky, 0.05, 0.03), diff=np.where(lucky, 0.3, -0.2), seed=1)


# ─── red-proofs: the lucky window and the consistently better model ───


def test_a_lucky_window_is_never_served(tmp_path, monkeypatch):
    """The windowed gate (re-checked every round on the newest 300 queries)
    serves this stream once its window lies in the lucky block; the record
    says the model loses the cited node, so it is never served."""
    lr, _m, served = _drive(_lucky_window_stream(), tmp_path, monkeypatch)
    assert not served.any() and lr.serving is False, lr.reason
    assert lr.counters["serving_turned_on"] == 0


def test_a_consistently_better_model_is_served(tmp_path, monkeypatch):
    stream = _stream(1200, gain=0.04, diff=0.15, seed=2)
    lr, _m, served = _drive(stream, tmp_path, monkeypatch)
    assert lr.serving is True, lr.reason
    first = int(np.flatnonzero(served)[0])
    # it waited for a record of 300 queries / 50 citing ones, then was served
    # from the first check on, and stayed served
    assert 300 <= first <= 400 and served[first:].all()
    assert lr.counters["serving_turned_off"] == 0
    assert "over plain cosine" in lr.reason


def test_the_record_holds_every_query_scored_before_training(tmp_path, monkeypatch):
    """Every kept event lands in the record once, in arrival order, with the
    served flag it had when it arrived."""
    stream = _stream(700, gain=0.04, diff=0.15, seed=3)
    lr, _m, served = _drive(stream, tmp_path, monkeypatch)
    lr.run_round_now(_model(tmp_path, "tail"), now_ms=10**6)  # score the tail
    assert len(lr.record) == 700 == lr.counters["events_seen"]
    np.testing.assert_allclose(lr.record.column("model_ndcg"), stream.m_ndcg, rtol=1e-6)
    np.testing.assert_allclose(lr.record.column("cos_mrr"), stream.c_mrr, rtol=1e-6)
    np.testing.assert_array_equal(lr.record.served, served)


# ─── the confidence schedule ──────────────────────────────────────────


def test_the_kth_check_runs_at_one_minus_budget_over_k_k_plus_one():
    assert rm.check_confidence(1) == pytest.approx(0.95)
    assert rm.check_confidence(2) == pytest.approx(1 - 0.10 / 6)
    assert rm.check_confidence(10) == pytest.approx(1 - 0.10 / 110)
    # the error rates telescope to the budget
    alphas = [1 - rm.check_confidence(k) for k in range(1, 10_001)]
    assert sum(alphas) == pytest.approx(0.10 * (1 - 1 / 10_001))
    assert sum(alphas) < rm.GATE_ERROR_BUDGET == 0.10
    with pytest.raises(ValueError):
        rm.check_confidence(0)


def test_each_round_that_adds_to_a_ready_record_is_the_next_check(tmp_path, monkeypatch):
    stream = _stream(640, gain=0.04, diff=0.15, seed=4)
    lr, _m, _s = _drive(stream, tmp_path, monkeypatch)
    # rounds every 32 events; the first with >= 300 queries is after event 320
    assert lr.checks == (640 - 320) // 32 + 1
    assert lr.last_check is not None and lr.last_check.k == lr.checks
    assert lr.last_check.confidence == pytest.approx(rm.check_confidence(lr.checks))
    assert lr.last_check.n_queries == 640


def test_repeated_checks_keep_the_false_pass_rate_within_the_budget():
    """A model a hair WORSE than the bar on the cited node (-0.002), checked every
    32 queries over 3,000: with the schedule the share of streams that EVER
    pass stays within the 10% budget; at a constant 90% (what re-checking a
    fixed-confidence bound amounts to) far more do."""
    cfg = ol.OnlineConfig()
    passed_schedule = passed_constant = 0
    trials = 60
    for t in range(trials):
        s = _stream(3000, gain=0.06, diff=-0.002, seed=100 + t, noise=0.15)
        rec = ol.PrequentialRecord()
        k = 0
        ever_s = ever_c = False
        for lo in range(0, 3000, 32):
            hi = min(lo + 32, 3000)
            m = rm.QueryMetrics(ndcg=s.m_ndcg[lo:hi], mrr=s.m_mrr[lo:hi],
                                recall=np.full(hi - lo, np.nan))
            c = rm.QueryMetrics(ndcg=s.c_ndcg[lo:hi], mrr=s.c_mrr[lo:hi],
                                recall=np.full(hi - lo, np.nan))
            rec.append(ol.QueryScores(model=m, cosine=c, search=c,
                                      served=np.zeros(hi - lo, bool)))
            if not ol.ready_for_check(rec, cfg):
                continue
            k += 1
            ever_s |= ol.gate_decision(rec, k, cfg, at_ms=0).serve
            mm, cc = rec.metrics()
            b = rm.paired_normal_bounds(mm, cc, confidence=0.90)
            ever_c |= b.ndcg_gain_lb >= 0.02 and b.mrr_diff_lb >= 0
        passed_schedule += ever_s
        passed_constant += ever_c
    assert passed_schedule / trials <= 0.10, passed_schedule
    assert passed_constant / trials >= 0.25, passed_constant


# ─── no verdict before the minimums ───────────────────────────────────


def _record(stream: Stream) -> ol.PrequentialRecord:
    rec = ol.PrequentialRecord()
    n = len(stream)
    m = rm.QueryMetrics(ndcg=stream.m_ndcg, mrr=stream.m_mrr, recall=np.full(n, np.nan))
    c = rm.QueryMetrics(ndcg=stream.c_ndcg, mrr=stream.c_mrr, recall=np.full(n, np.nan))
    rec.append(ol.QueryScores(model=m, cosine=c, search=c, served=np.zeros(n, bool)))
    return rec


def test_no_verdict_before_fifty_citing_queries():
    """A clearly better model with 49 citing queries in a 2,000-query record
    gets no verdict at all (no check is spent); the 50th opens the checks."""
    assert ol.OnlineConfig().min_cited_queries == rm.GATE_MIN_CITED_QUERIES == 50
    cfg = ol.OnlineConfig()
    s = _stream(2000, gain=0.05, diff=0.3, cite_every=10**9, seed=5)
    s.m_mrr[:49], s.c_mrr[:49] = 0.9, 0.5
    rec = _record(s)
    assert rec.n_cited == 49 and not ol.ready_for_check(rec, cfg)
    assert "49/50 citing a node" in ol.collecting_reason(rec, cfg)
    s.m_mrr[49], s.c_mrr[49] = 0.9, 0.5
    assert ol.ready_for_check(_record(s), cfg)
    assert ol.gate_decision(_record(s), 1, cfg, at_ms=0).serve is True


def test_no_verdict_before_three_hundred_queries():
    cfg = ol.OnlineConfig()
    s = _stream(299, gain=0.05, diff=0.3, cite_every=1, seed=6)
    assert not ol.ready_for_check(_record(s), cfg)


def test_the_learner_spends_no_check_while_collecting(tmp_path, monkeypatch):
    stream = _stream(600, gain=0.05, diff=0.3, cite_every=20, seed=7)  # 30 citing
    lr, _m, served = _drive(stream, tmp_path, monkeypatch)
    assert lr.checks == 0 and not served.any() and lr.last_check is None
    assert lr.reason.startswith("collecting queries scored before training")


# ─── the bounds ───────────────────────────────────────────────────────


def test_the_bounds_are_paired_and_one_sided():
    n = 400
    rng = np.random.default_rng(8)
    base = rng.uniform(0.3, 0.9, n)
    cos = rm.QueryMetrics(ndcg=base, mrr=np.where(np.arange(n) % 2 == 0, base, np.nan),
                          recall=np.zeros(n))
    # model == cosine: both bounds exactly 0 (an unpaired bound would be < 0)
    b = rm.paired_normal_bounds(cos, cos, confidence=0.95)
    assert b.ndcg_gain_lb == 0.0 and b.mrr_diff_lb == 0.0
    assert (b.n_ndcg, b.n_cited) == (n, n // 2)
    # a constant +3% / +0.1: the bounds are the point values
    m = rm.QueryMetrics(ndcg=base * 1.03, mrr=cos.mrr + 0.1, recall=np.zeros(n))
    b = rm.paired_normal_bounds(m, cos, confidence=1 - 1e-9)
    assert b.ndcg_gain_lb == pytest.approx(0.03) and b.mrr_diff_lb == pytest.approx(0.1)
    # higher confidence, lower bound
    noisy = rm.QueryMetrics(ndcg=base * (1.03 + 0.1 * rng.normal(size=n)),
                            mrr=cos.mrr + 0.1 + 0.3 * rng.normal(size=n), recall=np.zeros(n))
    lo = rm.paired_normal_bounds(noisy, cos, confidence=0.90)
    hi = rm.paired_normal_bounds(noisy, cos, confidence=0.999)
    assert hi.ndcg_gain_lb < lo.ndcg_gain_lb and hi.mrr_diff_lb < lo.mrr_diff_lb


def test_undefined_bounds_fail():
    e = rm.QueryMetrics(ndcg=np.zeros(0), mrr=np.zeros(0), recall=np.zeros(0))
    b = rm.paired_normal_bounds(e, e, confidence=0.9)
    assert b.ndcg_gain_lb == b.mrr_diff_lb == float("-inf")
    one = rm.QueryMetrics(ndcg=np.array([0.5, 0.6]), mrr=np.array([1.0, np.nan]),
                          recall=np.zeros(2))
    b = rm.paired_normal_bounds(one, one, confidence=0.9)
    assert b.mrr_diff_lb == float("-inf") and b.ndcg_gain_lb == 0.0  # one citing query
    with pytest.raises(ValueError):
        rm.paired_normal_bounds(e, one, confidence=0.9)
    with pytest.raises(ValueError):
        rm.paired_normal_bounds(one, one, confidence=1.0)


@pytest.mark.parametrize("gain, diff, serve, needle", [
    (0.05, 0.10, True, "over plain cosine"),
    (0.005, 0.10, False, "the bound needs +2.00%"),
    (0.05, -0.10, False, "below 0"),
])
def test_gate_decision_is_the_owner_bar(gain, diff, serve, needle):
    rec = _record(_stream(2000, gain=gain, diff=diff, seed=9))
    chk = ol.gate_decision(rec, 3, ol.OnlineConfig(), at_ms=5)
    assert chk.serve is serve and needle in chk.reason, chk.reason
    assert chk.reason.startswith("check 3:") and chk.k == 3 and chk.at_ms == 5
    assert chk.n_queries == 2000 and chk.n_cited == 500


# ─── persistence ──────────────────────────────────────────────────────


def test_the_record_and_the_check_count_survive_a_restart(tmp_path, monkeypatch):
    stream = _stream(800, gain=0.04, diff=0.15, seed=10)
    lr, m, _s = _drive(stream, tmp_path, monkeypatch)
    m.save()
    lr.model_fingerprint = ol.model_fingerprint(m)
    d = tmp_path / "state"
    lr.save(d)
    back = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=lr.cfg)
    assert back.checks == lr.checks and back.serving is lr.serving is True
    assert back.last_check == lr.last_check   # re-derived at the same k: no check spent
    assert back.counters == lr.counters and back.reason == lr.reason
    for c in ol.RECORD_COLUMNS:
        np.testing.assert_array_equal(back.record.column(c), lr.record.column(c))
    np.testing.assert_array_equal(back.record.served, lr.record.served)
    assert [q.ts_ms for q in back._fresh] == [q.ts_ms for q in lr._fresh]
    assert [q.ts_ms for q in back._pending] == [q.ts_ms for q in lr._pending]
    # the record holds metrics, not vectors: 6 float32 + 1 flag per query
    with np.load(d / "online_qwen3.npz") as z:
        assert z["rec_model_ndcg"].dtype == np.float32 and len(z["rec_served"]) == 800
    # a stricter bar applies at once on load, at the same check number
    strict = dataclasses.replace(lr.cfg, min_ndcg_gain=0.5)
    s2 = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=strict)
    assert s2.serving is False and s2.checks == lr.checks
    # the checkpoint moved on without this learner: the record starts over
    m.record_local_train_steps(1)
    fresh = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=lr.cfg)
    assert fresh.serving is False and len(fresh.record) == 0 and fresh.checks == 0


def test_a_lost_record_starts_over(tmp_path, monkeypatch):
    stream = _stream(500, gain=0.04, diff=0.15, seed=11)
    lr, m, _s = _drive(stream, tmp_path, monkeypatch)
    lr.model_fingerprint = ol.model_fingerprint(m)
    d = tmp_path / "state"
    lr.save(d)
    (d / "online_qwen3.npz").write_bytes(b"not an npz")
    back = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m, cfg=lr.cfg)
    assert back.serving is False and back.checks == 0 and len(back.record) == 0
    assert "record lost" in back.reason


def test_a_format_1_state_is_discarded(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    (d / "online_qwen3.json").write_text('{"format_version": 1, "profile": "qwen3", '
                                         '"emb_dim": 8, "serving": true}')
    m = _model(tmp_path)
    back = ol.ProjectLearner.load(d, profile="qwen3", emb_dim=D, model=m)
    assert back.serving is False and back.checks == 0


# ─── the status surface ───────────────────────────────────────────────


def test_the_status_states_the_bar_and_the_served_searches(tmp_path, monkeypatch):
    stream = _stream(800, gain=0.04, diff=0.15, seed=12)
    lr, _m, served = _drive(stream, tmp_path, monkeypatch)
    st = lr.status()
    bar = st["bar"]
    assert bar["error_budget"] == 0.10 and bar["min_cited_queries"] == 50
    assert bar["min_queries"] == 300 and "prequential" in bar["judged_on"]
    assert st["checks"] == lr.checks and st["last_check"]["k"] == lr.checks
    assert st["record"]["queries"] == len(lr.record)
    sv = st["served_searches"]
    assert sv["queries"] == int(lr.record.served.sum()) > 0
    assert sv["ndcg_gain"] > 0.02 and sv["mrr"] > sv["cos_mrr"]
    assert st["last_check"]["error_rate"] == pytest.approx(1 - st["last_check"]["confidence"])
