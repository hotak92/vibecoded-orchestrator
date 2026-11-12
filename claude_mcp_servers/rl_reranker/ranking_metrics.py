# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Ranking metrics of the RL reranker: the one home of NDCG@k / MRR@k and the bar.

Pure numpy. Two readers, one definition:

* ``training/eval_harness.py`` (offline reports: hold-outs, LOPO folds);
* ``online_learning.py`` (the per-project serving gate inside the server).

Definitions (per query = one retrieval with its citation):

* **NDCG@k**: gain = the training target (continuous, in [0, 1]); discount
  ``1 / log2(rank + 1)``; ideal = the same gains sorted descending. A query
  whose ideal DCG is 0 carries no ranking information and is skipped;
* **MRR@k / Recall@k**: relevant = the node's title appears literally in the
  answer (``literal_cited``). A query with no relevant node is skipped for
  these two;
* ties keep the logged order (stable sort), for every ranker alike.

The bar (owner rulings 2026-11-11 / 2026-11-12): a model is better than plain
cosine ordering when its NDCG@10 is at least :data:`UNLOCK_MIN_REL_GAIN`
(+2%, relative) above cosine's AND the MRR@10 of the literally cited node is
not below cosine's (the second half guards against the answer-cosine target's
centrality component: ranking generic nodes first raises NDCG and lowers the
named node).

The serving gate applies the bar to CONFIDENCE BOUNDS on the project's
CUMULATIVE PREQUENTIAL record (owner ruling 2026-11-12, after the rlgate2
study showed a held-out window re-tested every round passing on lucky
windows): every query is scored, at the first round after it arrived, by the
model in place at that round, before that model trained on it, and the k-th check of a project's model judges the
bar at confidence :func:`check_confidence` ``(k)`` = ``1 - 0.10 / (k (k+1))``
— an alpha-spending schedule whose error rates sum to at most
:data:`GATE_ERROR_BUDGET` over ALL checks, so re-checking after every round
cannot accumulate false passes. The bounds are :func:`paired_normal_bounds`
(a paired normal approximation: a bootstrap cannot resolve the tiny error
rates of late checks). No verdict before :data:`GATE_MIN_CITED_QUERIES`
queries in the record cite a node.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist
from typing import Optional

import numpy as np

#: The bar: relative NDCG@10 improvement over cosine required.
UNLOCK_MIN_REL_GAIN = 0.02
#: Metric cut-off.
K = 10
#: The serving gate's total false-pass probability over every check of one
#: model (owner ruling 2026-11-12: the k-th check runs at confidence
#: ``1 - GATE_ERROR_BUDGET / (k (k+1))``; the rates sum to the budget).
GATE_ERROR_BUDGET = 0.10
#: No verdict before the record holds this many queries that literally cite a
#: node (chosen a priori, 2026-11-12): the MRR half of the bar is a mean of
#: per-query differences in [-1, 1], and below ~50 values neither the normal
#: approximation of that mean nor a served-search MRR point estimate means
#: much (the rlgate2 study saw acceptance "verdicts" on 4-9 citing queries).
GATE_MIN_CITED_QUERIES = 50


def dcg_at_k(gains_in_rank_order: np.ndarray, k: int) -> float:
    g = np.asarray(gains_in_rank_order, dtype=np.float64)[:k]
    if g.size == 0:
        return 0.0
    discounts = 1.0 / np.log2(np.arange(2, g.size + 2, dtype=np.float64))
    return float(np.sum(g * discounts))


def _order(scores: np.ndarray) -> np.ndarray:
    """Descending order, stable (ties keep their input order)."""
    return np.argsort(-np.asarray(scores, dtype=np.float64), kind="stable")


def ndcg_at_k(scores: np.ndarray, gains: np.ndarray, k: int = K) -> Optional[float]:
    """NDCG@k of ``scores`` against ``gains``; ``None`` when ideal DCG is 0."""
    gains = np.asarray(gains, dtype=np.float64)
    ideal = dcg_at_k(np.sort(gains)[::-1], k)
    if ideal <= 0.0:
        return None
    return dcg_at_k(gains[_order(scores)], k) / ideal


def mrr_at_k(scores: np.ndarray, relevant: np.ndarray, k: int = K) -> Optional[float]:
    """Reciprocal rank of the first relevant item in the top k (``None`` if none relevant)."""
    rel = np.asarray(relevant, dtype=bool)
    if not rel.any():
        return None
    ranked = rel[_order(scores)][:k]
    hits = np.flatnonzero(ranked)
    return float(1.0 / (hits[0] + 1)) if hits.size else 0.0


def recall_at_k(scores: np.ndarray, relevant: np.ndarray, k: int = K) -> Optional[float]:
    """Share of the relevant items that make the top k (``None`` if none relevant)."""
    rel = np.asarray(relevant, dtype=bool)
    total = int(rel.sum())
    if total == 0:
        return None
    return float(rel[_order(scores)][:k].sum() / total)


@dataclass
class QueryMetrics:
    """Per-query metric values for one ranker (NaN = not defined for that query)."""

    ndcg: np.ndarray
    mrr: np.ndarray
    recall: np.ndarray


def per_query_metrics(
    scores: np.ndarray,
    gains: np.ndarray,
    relevant: np.ndarray,
    first: np.ndarray,
    count: np.ndarray,
    k: int = K,
) -> QueryMetrics:
    """Metrics for every query; samples of query ``i`` are ``[first[i], first[i]+count[i])``."""
    n = len(first)
    ndcg = np.full(n, np.nan)
    mrr = np.full(n, np.nan)
    rec = np.full(n, np.nan)
    for i in range(n):
        lo, hi = int(first[i]), int(first[i] + count[i])
        s, g, r = scores[lo:hi], gains[lo:hi], relevant[lo:hi]
        v = ndcg_at_k(s, g, k)
        if v is not None:
            ndcg[i] = v
        m = mrr_at_k(s, r, k)
        if m is not None:
            mrr[i] = m
            rec[i] = recall_at_k(s, r, k)  # defined exactly when mrr is
    return QueryMetrics(ndcg=ndcg, mrr=mrr, recall=rec)


def relative_gain(model: float, baseline: float) -> float:
    return (model - baseline) / baseline if baseline > 0 else float("nan")


def nan_mean(x: np.ndarray) -> float:
    """Mean of the defined values; NaN when none is defined."""
    x = np.asarray(x, dtype=np.float64)
    ok = ~np.isnan(x)
    return float(x[ok].mean()) if ok.any() else float("nan")


def check_confidence(k: int, error_budget: float = GATE_ERROR_BUDGET) -> float:
    """One-sided confidence of the ``k``-th check (``k >= 1``):
    ``1 - error_budget / (k (k+1))``. Summed over every ``k`` the error rates
    are ``error_budget`` (telescoping ``1/k - 1/(k+1)``), so the probability
    that ANY check passes a model that does not meet the bar is at most
    ``error_budget`` — an anytime-valid schedule."""
    k = int(k)
    if k < 1:
        raise ValueError(f"checks are numbered from 1, got {k}")
    return 1.0 - float(error_budget) / (k * (k + 1))


@dataclass(frozen=True)
class GateBounds:
    """One-sided lower confidence bounds of the bar's two halves.

    ``ndcg_gain_lb``: of the relative NDCG@k gain of the model over the
    baseline (``mean(model) / mean(base) - 1`` over the queries where both are
    defined). ``mrr_diff_lb``: of ``MRR@k(model) - MRR@k(base)`` over the
    citing queries. ``-inf`` when a bound is undefined (fewer than two
    defined queries, a zero baseline mean) — a failing bound. ``n_ndcg`` /
    ``n_cited``: the queries each bound used.
    """

    ndcg_gain_lb: float
    mrr_diff_lb: float
    confidence: float
    n_ndcg: int
    n_cited: int


def paired_normal_bounds(model: QueryMetrics, base: QueryMetrics, *,
                         confidence: float) -> GateBounds:
    """Paired normal-approximation lower bounds of the bar over the per-query
    metrics of two rankers on the SAME queries (from :func:`per_query_metrics`).

    NDCG: the ratio of means by the delta method — standard error of
    ``mean(model - r * base) / mean(base)`` with ``r`` the observed ratio;
    queries whose NDCG is undefined for either ranker are left out. MRR: the
    mean of the paired differences on the queries both define (the citing
    ones) and its standard error. Each bound is ``estimate - z * SE`` with
    ``z`` the standard normal ``confidence`` quantile. Deterministic.
    """
    n = int(len(model.ndcg))
    if n != len(base.ndcg) or n != len(model.mrr) or n != len(base.mrr):
        raise ValueError("paired bounds need both rankers' metrics on the same queries")
    if not 0.5 <= confidence < 1.0:
        raise ValueError(f"confidence must be in [0.5, 1), got {confidence}")
    z = NormalDist().inv_cdf(float(confidence))
    m_nd = np.asarray(model.ndcg, dtype=np.float64)
    b_nd = np.asarray(base.ndcg, dtype=np.float64)
    ok = ~np.isnan(m_nd) & ~np.isnan(b_nd)
    m_nd, b_nd = m_nd[ok], b_nd[ok]
    gain_lb = float("-inf")
    if m_nd.size >= 2 and b_nd.mean() > 0:
        r = m_nd.mean() / b_nd.mean()
        resid = m_nd - r * b_nd
        se = float(np.sqrt(resid.var(ddof=1) / m_nd.size)) / b_nd.mean()
        gain_lb = float(r - 1.0 - z * se)
    m_mr = np.asarray(model.mrr, dtype=np.float64)
    b_mr = np.asarray(base.mrr, dtype=np.float64)
    okm = ~np.isnan(m_mr) & ~np.isnan(b_mr)
    d = m_mr[okm] - b_mr[okm]
    diff_lb = float("-inf")
    if d.size >= 2:
        diff_lb = float(d.mean() - z * np.sqrt(d.var(ddof=1) / d.size))
    return GateBounds(ndcg_gain_lb=gain_lb, mrr_diff_lb=diff_lb, confidence=float(confidence),
                      n_ndcg=int(m_nd.size), n_cited=int(d.size))
