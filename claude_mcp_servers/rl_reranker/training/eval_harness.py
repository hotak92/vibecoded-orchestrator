# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Ranking evaluation for the RL reranker: model vs plain cosine ordering.

Pure numpy; no torch, no I/O. ``shard_trainer`` produces per-sample scores and
calls :func:`evaluate_split`; :func:`ship_decision` applies the owner's unlock
bar; :func:`render_markdown` turns a report into the summary that may be
committed (it carries hashed project ids only).

Definitions (per query = one retrieval with its citation):

* the candidate set is every node that has both a vector and a training
  target (the trainer's sample set for that query);
* **NDCG@k**: gain = the training target (continuous, in [0, 1]); discount
  ``1 / log2(rank + 1)``; ideal = the same gains sorted descending. Queries
  whose ideal DCG is 0 carry no ranking information and are skipped;
* **MRR@k / Recall@k**: relevant = the node's title appears literally in the
  answer (``literal_cited``) — sharper than the binary ``cited`` flag, which
  is positive for ~69% of judgments. Queries with no relevant node are
  skipped for these two;
* **cosine baseline**: nodes ordered by the cosine between the query vector
  and the node vector (computed from the exported vectors, so it is the same
  cosine the model sees as an input);
* ties keep the logged order (stable sort), for every ranker alike.

Unlock bar (owner ruling 2026-11-11): relative NDCG@10 gain over cosine of at
least +2% on BOTH hold-outs (a later time window and a project not seen in
training), and no project worse than cosine. A project with fewer than
``min_project_queries`` evaluated queries is reported but not judged (too few
queries to tell a regression from noise).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

import numpy as np

#: The bar: relative NDCG@10 improvement over cosine required on each hold-out.
UNLOCK_MIN_REL_GAIN = 0.02
#: Projects with fewer evaluated queries than this are reported, not judged.
MIN_PROJECT_QUERIES = 100
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


def bootstrap_rel_gain_ci(
    model_vals: np.ndarray,
    base_vals: np.ndarray,
    *,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile CI of the relative mean gain, resampling queries."""
    mask = ~(np.isnan(model_vals) | np.isnan(base_vals))
    m, b = model_vals[mask], base_vals[mask]
    if m.size == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, m.size, size=(n_boot, m.size))
    mm = m[idx].mean(axis=1)
    bm = b[idx].mean(axis=1)
    rel = (mm - bm) / np.where(bm > 0, bm, np.nan)
    lo, hi = np.nanpercentile(rel, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def _summ(model: QueryMetrics, base: QueryMetrics, sel: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("ndcg", "mrr", "recall"):
        mv = getattr(model, name)[sel]
        bv = getattr(base, name)[sel]
        ok = ~(np.isnan(mv) | np.isnan(bv))
        n = int(ok.sum())
        mmean = float(mv[ok].mean()) if n else float("nan")
        bmean = float(bv[ok].mean()) if n else float("nan")
        out[name] = {
            "n_queries": n,
            "cosine": round(bmean, 5),
            "model": round(mmean, 5),
            "rel_gain": round(relative_gain(mmean, bmean), 5) if n else None,
        }
    return out


def evaluate_split(
    *,
    model_scores: np.ndarray,
    cosine_scores: np.ndarray,
    gains: np.ndarray,
    relevant: np.ndarray,
    first: np.ndarray,
    count: np.ndarray,
    project: np.ndarray,
    task_type: np.ndarray,
    project_names: list[str],
    task_type_names: list[str],
    extra_baselines: Optional[dict[str, np.ndarray]] = None,
    min_project_queries: int = MIN_PROJECT_QUERIES,
    k: int = K,
    n_boot: int = 1000,
) -> dict[str, Any]:
    """Model vs cosine on one evaluation split (arrays cover only its queries)."""
    m = per_query_metrics(model_scores, gains, relevant, first, count, k)
    b = per_query_metrics(cosine_scores, gains, relevant, first, count, k)
    allq = np.ones(len(first), dtype=bool)
    report: dict[str, Any] = {
        "k": k,
        "n_queries": int(len(first)),
        "n_samples": int(count.sum()),
        "overall": _summ(m, b, allq),
    }
    lo, hi = bootstrap_rel_gain_ci(m.ndcg, b.ndcg, n_boot=n_boot)
    report["overall"]["ndcg"]["rel_gain_ci95"] = [round(lo, 5), round(hi, 5)]
    if extra_baselines:
        report["other_baselines_ndcg"] = {}
        for name, sc in extra_baselines.items():
            e = per_query_metrics(sc, gains, relevant, first, count, k)
            ok = ~np.isnan(e.ndcg)
            report["other_baselines_ndcg"][name] = round(float(e.ndcg[ok].mean()), 5) if ok.any() else None
    per_project = {}
    for p in np.unique(project):
        sel = project == p
        row = _summ(m, b, sel)["ndcg"]
        row["judged"] = row["n_queries"] >= min_project_queries
        row["worse_than_cosine"] = bool(row["judged"] and row["model"] < row["cosine"])
        per_project[project_names[int(p)]] = row
    report["per_project_ndcg"] = per_project
    per_task = {}
    for t in np.unique(task_type):
        sel = task_type == t
        per_task[task_type_names[int(t)]] = _summ(m, b, sel)["ndcg"]
    report["per_task_type_ndcg"] = per_task
    return report


def ship_decision(
    time_report: dict[str, Any],
    project_report: dict[str, Any],
    *,
    min_rel_gain: float = UNLOCK_MIN_REL_GAIN,
) -> dict[str, Any]:
    """Apply the unlock bar to one profile's two hold-out reports."""
    reasons: list[str] = []
    for name, rep in (("time hold-out", time_report), ("project hold-out", project_report)):
        g = rep["overall"]["ndcg"]["rel_gain"]
        if g is None or not np.isfinite(g) or g < min_rel_gain:
            reasons.append(
                f"{name}: NDCG@{rep['k']} relative gain {g if g is None else round(100 * g, 2)}% "
                f"< required {round(100 * min_rel_gain, 2)}%"
            )
    for name, rep in (("time hold-out", time_report), ("project hold-out", project_report)):
        for proj, row in rep["per_project_ndcg"].items():
            if row["worse_than_cosine"]:
                reasons.append(
                    f"{name}: project {proj} worse than cosine "
                    f"({row['model']:.4f} < {row['cosine']:.4f}, {row['n_queries']} queries)"
                )
    return {"passes": not reasons, "reasons": reasons, "min_rel_gain": min_rel_gain}


def _fmt_pct(x: Optional[float]) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{100 * x:+.2f}%"


def render_markdown(reports: Iterable[dict[str, Any]]) -> str:
    """Short summary of one or more profile reports (hashed ids only)."""
    lines = ["# RL reranker evaluation", ""]
    for rep in reports:
        prof = rep["profile"]
        dec = rep["decision"]
        lines += [
            f"## {prof}: {'PASSES' if dec['passes'] else 'does not pass'} the unlock bar",
            "",
            f"Training: {rep['training']['n_train_queries']} queries, best LR "
            f"{rep['training']['best_lr']}, {rep['training']['epochs_run']} epochs, "
            f"{rep['training']['runtime_s']:.0f} s.",
            "",
            "| hold-out | queries | NDCG@10 cosine | NDCG@10 model | gain | 95% CI | "
            "MRR@10 cos/model | Recall@10 cos/model |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for key, label in (("time_holdout", "time (latest 15%)"),
                           ("project_holdout", "project " + rep["holdout_project"])):
            r = rep[key]["overall"]
            ci = r["ndcg"].get("rel_gain_ci95", [None, None])
            lines.append(
                f"| {label} | {r['ndcg']['n_queries']} | {r['ndcg']['cosine']:.4f} | "
                f"{r['ndcg']['model']:.4f} | {_fmt_pct(r['ndcg']['rel_gain'])} | "
                f"{_fmt_pct(ci[0])} .. {_fmt_pct(ci[1])} | "
                f"{r['mrr']['cosine']:.3f} / {r['mrr']['model']:.3f} | "
                f"{r['recall']['cosine']:.3f} / {r['recall']['model']:.3f} |"
            )
        lines += ["", "Per project (time hold-out, NDCG@10):", ""]
        for proj, row in rep["time_holdout"]["per_project_ndcg"].items():
            flag = "" if row["judged"] else " (too few queries to judge)"
            lines.append(
                f"- {proj}: {row['n_queries']} queries, cosine {row['cosine']:.4f}, "
                f"model {row['model']:.4f} ({_fmt_pct(row['rel_gain'])}){flag}"
            )
        if dec["reasons"]:
            lines += ["", "Why not:", ""] + [f"- {r}" for r in dec["reasons"]]
        lines.append("")
    return "\n".join(lines)
