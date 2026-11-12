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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

#: The bar: relative NDCG@10 improvement over cosine required.
UNLOCK_MIN_REL_GAIN = 0.02
#: Metric cut-off.
K = 10


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
