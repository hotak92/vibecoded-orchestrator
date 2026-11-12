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

The serving gate applies the bar to a CONFIDENCE BOUND, not to the point
estimate (owner ruling 2026-11-12, after a replay showed the point check
passing by noise on 20-50 citing queries): :func:`paired_bootstrap_bounds`
resamples the held-out queries with replacement — one resample for both
rankers (paired) — and the bar must hold for the one-sided
:data:`GATE_CONFIDENCE` lower bound of each half.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

#: The bar: relative NDCG@10 improvement over cosine required.
UNLOCK_MIN_REL_GAIN = 0.02
#: Metric cut-off.
K = 10
#: The serving gate's one-sided confidence level and bootstrap resample count
#: (owner ruling 2026-11-12: serve only when a paired bootstrap says, at 90%
#: confidence, that the NDCG@10 gain is >= +2% AND the cited-node MRR@10
#: difference is >= 0). 1000 resamples bound the 10th percentile to about
#: +-1 percentile point at a few tens of ms per check on a 2,000-query window.
GATE_CONFIDENCE = 0.90
GATE_BOOTSTRAP_SAMPLES = 1000
#: Resamples drawn per vectorised block (bounds the check's memory to
#: ``block * n_queries`` indices).
_BOOT_BLOCK = 125


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


@dataclass(frozen=True)
class BootstrapBounds:
    """One-sided lower confidence bounds of the bar's two halves.

    ``ndcg_gain_lb``: of the relative NDCG@k gain of the model over the
    baseline (gain of the resampled MEANS, ``mean(model) / mean(base) - 1``).
    ``mrr_diff_lb``: of ``MRR@k(model) - MRR@k(base)`` on the citing queries
    of each resample. ``-inf`` when the bound is undefined (no query, or too
    many resamples without a defined value) — a failing bound.
    ``empty_cited_resamples``: resamples that drew no citing query; each
    counts as a FAILING resample of the MRR half (its difference is ``-inf``),
    so a window whose citing queries are rare cannot pass on the resamples
    that happen to miss them.
    """

    ndcg_gain_lb: float
    mrr_diff_lb: float
    n_boot: int
    confidence: float
    empty_cited_resamples: int


def _lower_bound(values: np.ndarray, confidence: float) -> float:
    """The one-sided ``confidence`` lower bound: the ``1 - confidence``
    order statistic (no interpolation, so ``-inf`` entries stay failing)."""
    if values.size == 0:
        return float("-inf")
    return float(np.quantile(values, 1.0 - confidence, method="lower"))


def paired_bootstrap_bounds(
    model: QueryMetrics,
    base: QueryMetrics,
    *,
    n_boot: int = GATE_BOOTSTRAP_SAMPLES,
    confidence: float = GATE_CONFIDENCE,
    seed: int = 0,
) -> BootstrapBounds:
    """Paired bootstrap of the bar over the per-query metrics of two rankers
    on the SAME queries (from :func:`per_query_metrics`).

    Each resample draws ``n`` query indices with replacement (``n`` = the
    window size) and uses them for both rankers. NDCG: queries whose NDCG is
    undefined (ideal DCG 0) are left out of both means; a resample with none
    defined, or a zero baseline mean, is failing (``-inf``). MRR: only the
    resample's citing queries count; a resample with none is failing.
    Deterministic for a given ``seed`` (``numpy.random.default_rng``).
    """
    n = int(len(model.ndcg))
    if n != len(base.ndcg) or n != len(model.mrr) or n != len(base.mrr):
        raise ValueError("paired bootstrap needs both rankers' metrics on the same queries")
    n_boot = max(1, int(n_boot))
    if n == 0:
        return BootstrapBounds(float("-inf"), float("-inf"), n_boot, confidence, n_boot)
    nd_ok = ~np.isnan(model.ndcg) & ~np.isnan(base.ndcg)
    nd_m = np.where(nd_ok, model.ndcg, 0.0)
    nd_b = np.where(nd_ok, base.ndcg, 0.0)
    mr_ok = ~np.isnan(model.mrr) & ~np.isnan(base.mrr)
    mr_d = np.where(mr_ok, model.mrr - base.mrr, 0.0)
    nd_okf = nd_ok.astype(np.float64)
    mr_okf = mr_ok.astype(np.float64)

    rng = np.random.default_rng(seed)
    gains = np.empty(n_boot, dtype=np.float64)
    diffs = np.empty(n_boot, dtype=np.float64)
    for lo in range(0, n_boot, _BOOT_BLOCK):
        b = min(_BOOT_BLOCK, n_boot - lo)
        idx = rng.integers(0, n, size=(b, n))
        s_m = nd_m[idx].sum(axis=1)
        s_b = nd_b[idx].sum(axis=1)
        c_nd = nd_okf[idx].sum(axis=1)
        ok = (c_nd > 0) & (s_b > 0)
        g = np.full(b, -np.inf)
        g[ok] = s_m[ok] / s_b[ok] - 1.0   # equal counts: ratio of sums = ratio of means
        s_d = mr_d[idx].sum(axis=1)
        c_mr = mr_okf[idx].sum(axis=1)
        d = np.full(b, -np.inf)
        has = c_mr > 0
        d[has] = s_d[has] / c_mr[has]
        gains[lo:lo + b] = g
        diffs[lo:lo + b] = d
    return BootstrapBounds(
        ndcg_gain_lb=_lower_bound(gains, confidence),
        mrr_diff_lb=_lower_bound(diffs, confidence),
        n_boot=n_boot, confidence=float(confidence),
        empty_cited_resamples=int(np.sum(np.isneginf(diffs))),  # -inf only when none cites
    )
