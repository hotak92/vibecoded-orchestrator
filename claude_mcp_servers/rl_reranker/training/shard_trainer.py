# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Train the shipped RL reranker from export shards (multi-project, per profile).

Input: one or more directories written by ``rl_reranker.training.export`` (this
machine's, plus any a volunteer shared). Output: a checkpoint in the layout
``bundled_weights.py`` expects, an evaluation report (JSON + markdown), and —
through the ``ship`` sub-command — the bundled weights + manifest, written
only when BOTH profiles pass the unlock bar (owner rule: qwen3 and arctic ship
together, never one alone).

Pipeline for one profile (``train`` sub-command):

1. load every export's shard for the profile; vectors stay float16 and are
   moved to the GPU once (or kept on the CPU), samples are index arrays;
2. split by query, never inside a query: the PROJECT hold-out (one whole
   project, never trained on), then of the remaining queries the latest
   ``--time-holdout`` share by time is the TIME hold-out, the ``--val`` share
   before it is the validation set (LR choice + early stopping), and the rest
   trains;
3. cap any project's share of each training epoch (``--project-cap``) by
   subsampling its queries per epoch, so a shipped generic model is not one
   project's model;
4. LR sweep, one seeded run per LR: query-grouped mini-batches, loss =
   MSE + 0.5·BCE + λ·(pairwise target-difference loss over pairs of the SAME
   query), soft-label samples down-weighted exactly as ``offline_trainer``;
   patience on validation NDCG@10;
5. the best-validation model is scored on both hold-outs against plain cosine
   ordering (``eval_harness``) and the unlock bar is applied.

Model inputs are built exactly like ``RLModel._build_inputs`` builds them at
serving time: slot 0 the query, slot 1 the node, slots 2..6 the linked vectors
(present ones first, in random order, the rest NULL padding), per-slot cosines
``(cos(q, x), cos(n, x))``. The profile's linked-slot mask
(:func:`rl_model.linked_slots_masked`) is applied here and by the server's
scoring path alike. A node type outside the builtins gets the embedding a
serving model gives a type it registers on the fly: the mean of the builtin
rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import eval_harness
from .export import TYPE_CODE_OTHER, load_profile_shard
from ..rl_model import (
    LINKED_SLOTS_MASKED_PROFILES,
    N_ENTITY_TYPES,
    NODE_TYPES,
    NULL_TYPE_IDX,
    QUERY_TYPE_IDX,
    WEIGHT_DECAY,
    RLModel,
    linked_slots_masked,
)

# PyTorch is a hard requirement of training: a missing torch fails here, at
# import, with the ImportError that names it (the VCO venv carries torch).
import torch
import torch.nn.functional as F

#: The type_embs row a non-builtin type trains on (the first dynamic row; it
#: is truncated away on save, as every dynamic row is).
OTHER_TYPE_ROW = N_ENTITY_TYPES


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class TrainData:
    profile: str
    device: Any
    vectors: Any                 # torch (V, D) float16
    # per sample (torch, on device)
    q_vec: Any
    node_vec: Any
    linked_vec: Any              # (N, MAX_LINKED) long, -1 = none
    linked_type: Any             # (N, MAX_LINKED) long (export type codes)
    node_type: Any
    target: Any                  # float32
    weight: Any                  # float32 (soft-label weight per sample)
    sample_query: Any            # long, global query id per sample
    # per sample (numpy, for evaluation)
    np_target: np.ndarray
    np_literal: np.ndarray
    np_cos_qn: np.ndarray
    np_score: np.ndarray
    np_shown_rank: np.ndarray
    # per query (numpy)
    q_first: np.ndarray
    q_count: np.ndarray
    q_ts: np.ndarray
    q_project: np.ndarray
    q_task_type: np.ndarray
    project_names: list[str]
    task_type_names: list[str]


def _resolve_device(spec: str) -> Any:
    if spec == "auto":
        return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    return torch.device(spec)


def load_training_data(export_dirs: list[Path], profile: str, device: str = "auto") -> TrainData:
    """Concatenate the profile's shards of every export into one dataset."""
    dev = _resolve_device(device)
    vec_parts, per_sample, per_query = [], [], []
    projects: list[str] = []
    task_types: list[str] = []
    v_off = s_off = 0
    for ei, d in enumerate(export_dirs):
        sh = load_profile_shard(Path(d), profile)
        V = sh.vectors.shape[0]
        nq = len(sh.queries["vec"])
        if nq == 0:
            continue
        p_map = np.array([len(projects) + i for i in range(len(sh.projects))], dtype=np.int64)
        projects.extend(f"{h}" if len(export_dirs) == 1 else f"{ei}:{h}" for h in sh.projects)
        t_map = []
        for t in sh.task_types:
            if t not in task_types:
                task_types.append(t)
            t_map.append(task_types.index(t))
        t_map_arr = np.array(t_map or [0], dtype=np.int64)
        s = sh.samples
        q = sh.queries
        # query id per sample, from the query's [first, first+count) range
        sample_q = np.repeat(np.arange(nq, dtype=np.int64), q["n_samples"].astype(np.int64))
        q_global_off = sum(len(x["first"]) for x in per_query)
        per_sample.append({
            "q_vec": q["vec"].astype(np.int64)[sample_q] + v_off,
            "node_vec": s["node_vec"].astype(np.int64) + v_off,
            "linked_vec": np.where(s["linked_vec"] >= 0, s["linked_vec"].astype(np.int64) + v_off, -1),
            "linked_type": s["linked_type"].astype(np.int64),
            "node_type": s["node_type"].astype(np.int64),
            "target": s["target"].astype(np.float32),
            "weight": q["weight"].astype(np.float32)[sample_q],
            "sample_query": sample_q + q_global_off,
            "literal": s["literal_cited"].astype(np.int8),
            "cos_qn": s["cos_qn"].astype(np.float32),
            "score": s["score"].astype(np.float32),
            "shown_rank": s["shown_rank"].astype(np.int32),
        })
        per_query.append({
            "first": q["first_sample"].astype(np.int64) + s_off,
            "count": q["n_samples"].astype(np.int64),
            "ts": q["ts_hour_ms"].astype(np.int64),
            "project": p_map[q["project"].astype(np.int64)],
            "task_type": t_map_arr[q["task_type"].astype(np.int64)],
        })
        vec_parts.append(np.asarray(sh.vectors))
        v_off += V
        s_off += len(s["target"])
    if not per_query:
        raise ValueError(f"no {profile} queries in {export_dirs}")

    def cat(key: str, src: list[dict[str, np.ndarray]]) -> np.ndarray:
        return np.concatenate([x[key] for x in src])

    vectors = torch.from_numpy(np.concatenate(vec_parts)).to(dev)

    def to_dev(key: str, dtype: Any) -> Any:
        return torch.from_numpy(cat(key, per_sample)).to(dev, dtype=dtype)

    return TrainData(
        profile=profile,
        device=dev,
        vectors=vectors,
        q_vec=to_dev("q_vec", torch.long),
        node_vec=to_dev("node_vec", torch.long),
        linked_vec=to_dev("linked_vec", torch.long),
        linked_type=to_dev("linked_type", torch.long),
        node_type=to_dev("node_type", torch.long),
        target=to_dev("target", torch.float32),
        weight=to_dev("weight", torch.float32),
        sample_query=to_dev("sample_query", torch.long),
        np_target=cat("target", per_sample),
        np_literal=cat("literal", per_sample),
        np_cos_qn=cat("cos_qn", per_sample),
        np_score=cat("score", per_sample),
        np_shown_rank=cat("shown_rank", per_sample),
        q_first=cat("first", per_query),
        q_count=cat("count", per_query),
        q_ts=cat("ts", per_query),
        q_project=cat("project", per_query),
        q_task_type=cat("task_type", per_query),
        project_names=projects,
        task_type_names=task_types,
    )


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

def pick_holdout_project(
    data: TrainData, min_queries: int = 1000, max_share: float = 0.20,
) -> str:
    """The largest project with >= ``min_queries`` queries and <= ``max_share``.

    Large enough to measure, small enough that holding it out does not starve
    training of a big share of the data. Raises when no project qualifies.
    """
    counts = np.bincount(data.q_project, minlength=len(data.project_names))
    total = int(counts.sum())
    order = [int(i) for i in np.argsort(-counts, kind="stable")
             if counts[i] >= min_queries and counts[i] <= max_share * total]
    if not order:
        raise ValueError(
            f"no project has >= {min_queries} {data.profile} queries and <= "
            f"{int(100 * max_share)}% of the data; pass --holdout-project. Counts: "
            f"{[(data.project_names[i], int(c)) for i, c in enumerate(counts)]}"
        )
    return data.project_names[order[0]]


def make_splits(
    data: TrainData,
    holdout_project: str,
    *,
    time_holdout: float = 0.15,
    val: float = 0.10,
    dev_project: Optional[str] = None,
) -> dict[str, np.ndarray]:
    """Query-index splits: train / val / time_holdout / project_holdout (/ dev).

    ``dev_project`` (optional) is a second whole project kept out of training
    and reported per epoch — a cross-project check for choosing the training
    configuration WITHOUT looking at the project hold-out.
    """
    if holdout_project not in data.project_names:
        raise ValueError(f"hold-out project {holdout_project!r} is not in the data")
    hp = data.project_names.index(holdout_project)
    nq = len(data.q_first)
    allq = np.arange(nq)
    proj_hold = allq[data.q_project == hp]
    rest = allq[data.q_project != hp]
    out: dict[str, np.ndarray] = {}
    if dev_project is not None:
        if dev_project not in data.project_names or dev_project == holdout_project:
            raise ValueError(f"dev project {dev_project!r} must be a different project in the data")
        dp = data.project_names.index(dev_project)
        out["dev"] = rest[data.q_project[rest] == dp]
        rest = rest[data.q_project[rest] != dp]
    rest = rest[np.argsort(data.q_ts[rest], kind="stable")]
    n_time = int(round(len(rest) * time_holdout))
    n_val = int(round(len(rest) * val))
    time_hold = rest[len(rest) - n_time:] if n_time else rest[:0]
    val_q = rest[len(rest) - n_time - n_val: len(rest) - n_time]
    train_q = rest[: len(rest) - n_time - n_val]
    out.update({"train": train_q, "val": val_q, "time_holdout": time_hold,
                "project_holdout": proj_hold})
    return out


def epoch_queries(
    train_q: np.ndarray, q_project: np.ndarray, cap: float, rng: np.random.Generator,
) -> np.ndarray:
    """This epoch's training queries, shuffled, no project above ``cap`` share."""
    q = train_q
    if 0.0 < cap < 1.0 and len(q):
        projs, counts = np.unique(q_project[q], return_counts=True)
        keep_frac = {int(p): 1.0 for p in projs}
        # Iterate: shrinking one project changes the others' shares.
        for _ in range(len(projs)):
            eff = {int(p): keep_frac[int(p)] * c for p, c in zip(projs, counts)}
            tot = sum(eff.values())
            changed = False
            for p, c in zip(projs, counts):
                p = int(p)
                if eff[p] / tot > cap + 1e-9:
                    others = tot - eff[p]
                    keep_frac[p] = min(1.0, cap * others / ((1 - cap) * c))
                    changed = True
            if not changed:
                break
        if any(v < 1.0 for v in keep_frac.values()):
            u = rng.random(len(q))
            frac = np.array([keep_frac[int(p)] for p in q_project[q]])
            q = q[u < frac]
    return rng.permutation(q)


def query_sample_ranges(first: np.ndarray, count: np.ndarray) -> np.ndarray:
    """Concatenate ``[first_i, first_i + count_i)`` for every query, in order."""
    count = count.astype(np.int64)
    total = int(count.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64)
    starts = np.repeat(first.astype(np.int64) - np.concatenate(([0], np.cumsum(count)[:-1])), count)
    return starts + np.arange(total, dtype=np.int64)


# ---------------------------------------------------------------------------
# Model inputs
# ---------------------------------------------------------------------------

def _cos(a: Any, b: Any) -> Any:
    """Row-wise cosine; 0 when either vector is zero (math_utils semantics)."""
    na = a.norm(dim=-1)
    nb = b.norm(dim=-1)
    dot = (a * b).sum(dim=-1)
    denom = na * nb
    return torch.where(denom > 0, dot / denom.clamp_min(1e-30), torch.zeros_like(dot))


def _type_rows(codes: Any) -> Any:
    """Export type codes -> type_embs rows (OTHER -> the mean-of-builtins row)."""
    return torch.where(codes == TYPE_CODE_OTHER, torch.full_like(codes, OTHER_TYPE_ROW), codes)


def build_inputs(
    data: TrainData, idx: Any, *, generator: Any,
) -> tuple[Any, Any, Any]:
    """(entities, type_idxs, slot_scalars) for samples ``idx`` (a long tensor)."""
    q = data.vectors[data.q_vec[idx]].float()
    n = data.vectors[data.node_vec[idx]].float()
    B = q.shape[0]
    lv_idx = data.linked_vec[idx]
    present = lv_idx >= 0
    if linked_slots_masked(data.profile):
        present = torch.zeros_like(present)
    # Present slots first, in random order; the rest is padding (as
    # RLModel._build_inputs shuffles the linked list, then pads).
    keys = torch.rand(present.shape, device=present.device, generator=generator)
    keys = torch.where(present, keys, torch.full_like(keys, 2.0))
    perm = torch.argsort(keys, dim=1)
    lv_idx = torch.gather(lv_idx, 1, perm)
    present = torch.gather(present, 1, perm)
    l_types = torch.gather(data.linked_type[idx], 1, perm)
    lv = data.vectors[lv_idx.clamp_min(0)].float() * present.unsqueeze(-1)
    entities = torch.cat([q.unsqueeze(1), n.unsqueeze(1), lv], dim=1)
    q_t = torch.full((B, 1), QUERY_TYPE_IDX, dtype=torch.long, device=q.device)
    n_t = _type_rows(data.node_type[idx]).unsqueeze(1)
    l_t = torch.where(present, _type_rows(l_types), torch.full_like(l_types, NULL_TYPE_IDX))
    type_idxs = torch.cat([q_t, n_t, l_t], dim=1)
    cos_qn = _cos(q, n)
    ones = torch.ones_like(cos_qn)
    cql = _cos(q.unsqueeze(1).expand_as(lv), lv) * present
    cnl = _cos(n.unsqueeze(1).expand_as(lv), lv) * present
    slot_scalars = torch.cat([
        torch.stack([ones, ones], dim=-1).unsqueeze(1),
        torch.stack([cos_qn, ones], dim=-1).unsqueeze(1),
        torch.stack([cql, cnl], dim=-1),
    ], dim=1)
    return entities, type_idxs, slot_scalars


def random_rotation(dim: int, *, device: Any, generator: Any) -> Any:
    """A uniformly random ``dim x dim`` orthogonal matrix (QR of a Gaussian)."""
    a = torch.randn(dim, dim, device=device, generator=generator)
    q, r = torch.linalg.qr(a)
    return q * torch.sign(torch.diagonal(r)).unsqueeze(0)


def sync_other_type_row(net: Any) -> None:
    """Row OTHER_TYPE_ROW := mean of the builtin rows (a fresh registration's init)."""
    with torch.no_grad():
        w = net.type_embs.weight
        w[OTHER_TYPE_ROW] = w[: len(NODE_TYPES)].mean(0)


def within_query_pair_loss(pred: Any, target: Any, weight: Any, qid: Any) -> Any:
    """``offline_trainer.delta_mse_loss`` restricted to pairs of the same query."""
    p = pred.squeeze(-1)
    t = target.squeeze(-1)
    w = weight.squeeze(-1)
    gap = (t.unsqueeze(1) - t.unsqueeze(0)) - (p.unsqueeze(1) - p.unsqueeze(0))
    same = qid.unsqueeze(1) == qid.unsqueeze(0)
    mask = torch.triu(same, diagonal=1)
    wp = (w.unsqueeze(1) * w.unsqueeze(0))[mask]
    denom = wp.sum()
    if float(denom) <= 0.0:
        return p.sum() * 0.0
    return (wp * gap[mask] ** 2).sum() / denom


def _segment_log_softmax(x: Any, inv: Any, n: int) -> Any:
    mx = torch.full((n,), -math.inf, device=x.device, dtype=x.dtype)
    mx = mx.scatter_reduce(0, inv, x, reduce="amax", include_self=True)
    shifted = x - mx[inv]
    den = torch.zeros(n, device=x.device, dtype=x.dtype).index_add_(0, inv, shifted.exp())
    return shifted - den.log()[inv]


def within_query_listwise_loss(pred: Any, target: Any, weight: Any, qid: Any,
                               temp: float = 0.1) -> Any:
    """ListNet top-one loss per query: CE(softmax(target/T), softmax(pred/T)).

    Only the ORDER inside a query matters to it, so it cannot be lowered by
    learning a per-node prior (how "answer-like" a node is everywhere) — the
    failure mode of the pointwise terms on projects never seen in training.
    Queries are weighted by their sample weight; a one-node query contributes 0.
    """
    p = pred.squeeze(-1) / temp
    t = target.squeeze(-1) / temp
    w = weight.squeeze(-1)
    uq, inv = torch.unique(qid, return_inverse=True)
    n = int(uq.shape[0])
    lp = _segment_log_softmax(p, inv, n)
    pt = _segment_log_softmax(t, inv, n).exp()
    per_q = torch.zeros(n, device=p.device, dtype=p.dtype).index_add_(0, inv, -(pt * lp))
    cnt = torch.zeros(n, device=p.device, dtype=p.dtype).index_add_(0, inv, torch.ones_like(w))
    qw = torch.zeros(n, device=p.device, dtype=p.dtype).index_add_(0, inv, w) / cnt
    # subtract the target entropy so a perfect ranking scores 0 (KL divergence)
    ent = torch.zeros(n, device=p.device, dtype=p.dtype).index_add_(
        0, inv, -(pt * pt.clamp_min(1e-30).log()))
    return ((per_q - ent) * qw).sum() / qw.sum().clamp_min(1e-8)


# ---------------------------------------------------------------------------
# Train / score
# ---------------------------------------------------------------------------

def new_net(emb_dim: int, device: Any, seed: int) -> Any:
    """A fresh canonical v2 network, initialised exactly as RLModel does."""
    torch.manual_seed(seed)
    m = RLModel(emb_dim=emb_dim, state_path=Path("/nonexistent/rl_model_init.pt"),
                device="cpu")
    return m._net.to(device)


@torch.no_grad()
def score_samples(data: TrainData, net: Any, sample_idx: np.ndarray, *,
                  batch_size: int = 4096, seed: int = 0) -> np.ndarray:
    net.eval()
    sync_other_type_row(net)
    gen = torch.Generator(device=data.device)
    gen.manual_seed(seed)
    out = np.empty(len(sample_idx), dtype=np.float32)
    for i in range(0, len(sample_idx), batch_size):
        idx = torch.from_numpy(sample_idx[i:i + batch_size]).to(data.device)
        e, t, s = build_inputs(data, idx, generator=gen)
        out[i:i + batch_size] = net(e, t, s).squeeze(-1).float().cpu().numpy()
    return out


def cosine_scores(data: TrainData, sample_idx: np.ndarray, batch_size: int = 65536) -> np.ndarray:
    out = np.empty(len(sample_idx), dtype=np.float32)
    with torch.no_grad():
        for i in range(0, len(sample_idx), batch_size):
            idx = torch.from_numpy(sample_idx[i:i + batch_size]).to(data.device)
            q = data.vectors[data.q_vec[idx]].float()
            n = data.vectors[data.node_vec[idx]].float()
            out[i:i + batch_size] = _cos(q, n).cpu().numpy()
    return out


def _split_arrays(data: TrainData, qids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(sample_idx, first, count) with ``first`` re-based onto ``sample_idx``."""
    counts = data.q_count[qids]
    sample_idx = query_sample_ranges(data.q_first[qids], counts)
    first = np.concatenate(([0], np.cumsum(counts)[:-1])).astype(np.int64)
    return sample_idx, first, counts


def split_metrics(data: TrainData, net: Any, qids: np.ndarray,
                  k: int = eval_harness.K) -> dict[str, float]:
    """Mean NDCG@k and MRR@k of the model (and of cosine) over ``qids``."""
    sidx, first, count = _split_arrays(data, qids)
    sc = score_samples(data, net, sidx)
    cs = cosine_scores(data, sidx)
    gains, rel = data.np_target[sidx], data.np_literal[sidx] > 0
    m = eval_harness.per_query_metrics(sc, gains, rel, first, count, k)
    c = eval_harness.per_query_metrics(cs, gains, rel, first, count, k)

    def mean(x: np.ndarray) -> float:
        return float(np.nanmean(x)) if np.any(~np.isnan(x)) else float("nan")

    return {"ndcg": mean(m.ndcg), "mrr": mean(m.mrr),
            "cos_ndcg": mean(c.ndcg), "cos_mrr": mean(c.mrr)}


def val_ndcg(data: TrainData, net: Any, qids: np.ndarray, k: int = eval_harness.K) -> float:
    return split_metrics(data, net, qids, k)["ndcg"]


@dataclass(frozen=True)
class LossConfig:
    """Weights of the loss terms (all within the same mini-batch).

    ``pointwise``: MSE + 0.5·BCE to the target (offline_trainer's terms);
    ``pair``: the pairwise target-difference loss over pairs of one query;
    ``listwise``: ListNet KL over each query's nodes at ``list_temp``.
    """

    pointwise: float = 1.0
    pair: float = 0.5
    listwise: float = 0.0
    list_temp: float = 0.1

    def as_dict(self) -> dict[str, float]:
        return {"pointwise": self.pointwise, "pair": self.pair,
                "listwise": self.listwise, "list_temp": self.list_temp}


@dataclass
class RunResult:
    lr: float
    best_val_ndcg: float
    best_epoch: int
    epochs_run: int
    state_dict: dict[str, Any]
    history: list[dict[str, Any]]
    runtime_s: float


def train_one(
    data: TrainData,
    splits: dict[str, np.ndarray],
    *,
    lr: float,
    seed: int = 1234,
    epochs: int = 16,
    patience: int = 3,
    batch_size: int = 512,
    lambda_rank: Optional[float] = None,
    loss: LossConfig = LossConfig(),
    project_cap: float = 0.5,
    augment_rotation: bool = False,
    log: Any = print,
) -> RunResult:
    """One seeded training run; keeps the best-validation-NDCG state.

    ``augment_rotation``: multiply every training batch's vectors by a fresh
    random orthogonal matrix. Cosines (all the per-slot scalars) are
    unchanged, but no direction of the embedding space means the same thing
    twice, so the network cannot memorise WHICH nodes were useful in the
    training projects — a prior that does not transfer to a project it has
    never seen. Evaluation and serving use the vectors as they are.
    """
    if lambda_rank is not None:
        loss = LossConfig(pointwise=loss.pointwise, pair=lambda_rank,
                          listwise=loss.listwise, list_temp=loss.list_temp)
    random.seed(seed)
    np.random.seed(seed)
    rng = np.random.default_rng(seed)
    emb_dim = int(data.vectors.shape[1])
    net = new_net(emb_dim, data.device, seed)
    opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=WEIGHT_DECAY)
    gen = torch.Generator(device=data.device)
    gen.manual_seed(seed)
    best = (-math.inf, -1, None)
    history: list[dict[str, Any]] = []
    bad = 0
    started = time.time()
    epoch = 0
    for epoch in range(1, epochs + 1):
        net.train()
        q = epoch_queries(splits["train"], data.q_project, project_cap, rng)
        sidx = query_sample_ranges(data.q_first[q], data.q_count[q])
        tot = n_b = 0.0
        for i in range(0, len(sidx), batch_size):
            idx = torch.from_numpy(sidx[i:i + batch_size]).to(data.device)
            sync_other_type_row(net)
            e, t, s = build_inputs(data, idx, generator=gen)
            if augment_rotation:
                e = e @ random_rotation(e.shape[-1], device=e.device, generator=gen)
            tgt = data.target[idx].unsqueeze(-1)
            w = data.weight[idx].unsqueeze(-1)
            pred = net(e, t, s)
            qid = data.sample_query[idx]
            total = pred.sum() * 0.0
            if loss.pointwise:
                w_sum = w.sum().clamp_min(1e-8)
                mse = ((pred - tgt) ** 2 * w).sum() / w_sum
                bce = F.binary_cross_entropy(pred, tgt, weight=w, reduction="sum") / w_sum
                total = total + loss.pointwise * (mse + 0.5 * bce)
            if loss.pair:
                total = total + loss.pair * within_query_pair_loss(pred, tgt, w, qid)
            if loss.listwise:
                total = total + loss.listwise * within_query_listwise_loss(
                    pred, tgt, w, qid, loss.list_temp)
            opt.zero_grad(set_to_none=True)
            total.backward()
            opt.step()
            tot += float(total.detach())
            n_b += 1
        vm = split_metrics(data, net, splits["val"])
        v = vm["ndcg"]
        row = {"epoch": epoch, "train_loss": round(tot / max(n_b, 1), 6),
               "val_ndcg10": round(v, 6), "val_mrr10": round(vm["mrr"], 6),
               "train_queries": int(len(q))}
        msg = f"    lr={lr:g} epoch {epoch}: loss {tot / max(n_b, 1):.5f} val NDCG@10 {v:.5f} MRR {vm['mrr']:.4f}"
        if len(splits.get("dev", ())):
            dm = split_metrics(data, net, splits["dev"])
            row.update({"dev_ndcg10": round(dm["ndcg"], 6), "dev_mrr10": round(dm["mrr"], 6),
                        "dev_cos_ndcg10": round(dm["cos_ndcg"], 6),
                        "dev_cos_mrr10": round(dm["cos_mrr"], 6)})
            msg += (f" | dev NDCG {dm['ndcg']:.5f} (cos {dm['cos_ndcg']:.5f}) "
                    f"MRR {dm['mrr']:.4f} (cos {dm['cos_mrr']:.4f})")
        history.append(row)
        log(msg)
        if v > best[0] + 1e-6:
            best = (v, epoch, {k: x.detach().cpu().clone() for k, x in net.state_dict().items()})
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                break
    return RunResult(lr=lr, best_val_ndcg=best[0], best_epoch=best[1], epochs_run=epoch,
                     state_dict=best[2] or {}, history=history,
                     runtime_s=time.time() - started)


def evaluate_holdouts(data: TrainData, net: Any, splits: dict[str, np.ndarray],
                      holdout_project: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("time_holdout", "project_holdout"):
        qids = splits[key]
        sidx, first, count = _split_arrays(data, qids)
        model_sc = score_samples(data, net, sidx)
        cos_sc = cosine_scores(data, sidx)
        shown = data.np_shown_rank[sidx].astype(np.float64)
        extra = {
            "logged_cos_qn": np.nan_to_num(data.np_cos_qn[sidx], nan=-1.0),
            "weaviate_score": np.nan_to_num(data.np_score[sidx], nan=-1.0),
            "shown_rank_order": np.where(shown >= 0, -shown, -1e9),
        }
        out[key] = eval_harness.evaluate_split(
            model_scores=model_sc, cosine_scores=cos_sc,
            gains=data.np_target[sidx], relevant=data.np_literal[sidx] > 0,
            first=first, count=count,
            project=data.q_project[qids], task_type=data.q_task_type[qids],
            project_names=data.project_names, task_type_names=data.task_type_names,
            extra_baselines=extra,
        )
    return out


# ---------------------------------------------------------------------------
# Checkpoints + shipping
# ---------------------------------------------------------------------------

def save_checkpoint(state_dict: dict[str, Any], path: Path, *, profile: str,
                    version: str, emb_dim: int) -> str:
    """Write a serving checkpoint through RLModel.save (no optimizer moments)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    m = RLModel(emb_dim=emb_dim, state_path=path, device="cpu",
                weights_version=version, embedding_source=profile)
    m._net.load_state_dict(state_dict)
    m.mark_as_release_base()
    m.save()
    # RLModel.save writes a layout marker beside the file; a shipped weights
    # directory carries the checkpoint only.
    for extra in path.parent.iterdir():
        if extra != path:
            if extra.is_dir():
                shutil.rmtree(extra)
            else:
                extra.unlink()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ship(reports: list[Path], weights_root: Path) -> dict[str, Any]:
    """Install the checkpoints of passing reports into the bundled weights.

    Refuses unless a report is given for EVERY shipped profile and every one
    of them passes (owner rule: the profiles ship together, never one alone).
    """
    from ..bundled_weights import (
        MANIFEST_NAME,
        SHIPPED_PROFILES,
        load_manifest,
        sha256_file,
        weights_relpath,
    )

    docs = [json.loads(Path(r).read_text(encoding="utf-8")) for r in reports]
    by_profile = {d["profile"]: d for d in docs}
    missing = [p for p in SHIPPED_PROFILES if p not in by_profile]
    if missing:
        raise ValueError(f"no report for {missing}: the profiles ship together")
    failing = [p for p, d in by_profile.items() if not d["decision"]["passes"]]
    if failing:
        raise ValueError(f"not passing the unlock bar: {failing}; nothing ships")
    manifest = load_manifest(weights_root) or {"schema_version": 1, "profiles": {}}
    for profile in SHIPPED_PROFILES:
        d = by_profile[profile]
        src = Path(d["checkpoint"]["path"])
        version = d["checkpoint"]["version"]
        if sha256_file(src) != d["checkpoint"]["sha256"]:
            raise ValueError(f"{src}: sha256 differs from its report")
        dest = Path(weights_root) / Path(*weights_relpath(profile, version).parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        entry = manifest["profiles"].setdefault(profile, {"current": None, "versions": {}})
        entry.setdefault("versions", {})[version] = {"sha256": d["checkpoint"]["sha256"]}
        entry["current"] = version
    path = Path(weights_root) / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=4) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_train(args: argparse.Namespace) -> int:
    t0 = time.time()
    data = load_training_data([Path(d) for d in args.data], args.profile, args.device)
    holdout = args.holdout_project or pick_holdout_project(data)
    splits = make_splits(data, holdout, time_holdout=args.time_holdout, val=args.val,
                         dev_project=args.dev_project)
    loss = LossConfig(pointwise=args.pointwise_weight, pair=args.lambda_rank,
                      listwise=args.listwise_weight, list_temp=args.listwise_temp)
    print(f"{args.profile}: {len(data.q_first)} queries, {len(data.np_target)} samples, "
          f"{data.vectors.shape[0]} vectors on {data.device}; hold-out project {holdout}; "
          + ", ".join(f"{k}={len(v)}" for k, v in splits.items()), flush=True)
    runs = []
    for lr in args.lrs:
        r = train_one(data, splits, lr=lr, seed=args.seed, epochs=args.epochs,
                      patience=args.patience, batch_size=args.batch_size,
                      loss=loss, project_cap=args.project_cap,
                      augment_rotation=args.rotation_augment,
                      log=lambda m: print(m, flush=True))
        print(f"  lr={lr:g}: best val NDCG@10 {r.best_val_ndcg:.5f} at epoch {r.best_epoch} "
              f"({r.runtime_s:.0f} s)", flush=True)
        runs.append(r)
    best = max(runs, key=lambda r: r.best_val_ndcg)
    net = new_net(int(data.vectors.shape[1]), data.device, args.seed)
    net.load_state_dict(best.state_dict)
    holdouts = evaluate_holdouts(data, net, splits, holdout)
    decision = eval_harness.ship_decision(holdouts["time_holdout"], holdouts["project_holdout"])
    out = Path(args.out)
    ckpt = out / args.profile / args.version / f"rl_model_{args.profile}_{args.version}.pt"
    sha = save_checkpoint(best.state_dict, ckpt, profile=args.profile, version=args.version,
                          emb_dim=int(data.vectors.shape[1]))
    report = {
        "profile": args.profile,
        "holdout_project": holdout,
        "splits": {k: int(len(v)) for k, v in splits.items()},
        "linked_slots_masked": args.profile in LINKED_SLOTS_MASKED_PROFILES,
        "training": {
            "n_train_queries": int(len(splits["train"])),
            "lrs": list(args.lrs),
            "best_lr": best.lr,
            "best_epoch": best.best_epoch,
            "epochs_run": best.epochs_run,
            "best_val_ndcg10": round(best.best_val_ndcg, 6),
            "batch_size": args.batch_size,
            "loss": loss.as_dict(),
            "dev_project": args.dev_project,
            "rotation_augment": args.rotation_augment,
            "project_cap": args.project_cap,
            "patience": args.patience,
            "max_epochs": args.epochs,
            "seed": args.seed,
            "runtime_s": round(time.time() - t0, 1),
            "runs": [{"lr": r.lr, "best_val_ndcg10": round(r.best_val_ndcg, 6),
                      "best_epoch": r.best_epoch, "history": r.history,
                      "runtime_s": round(r.runtime_s, 1)} for r in runs],
        },
        "time_holdout": holdouts["time_holdout"],
        "project_holdout": holdouts["project_holdout"],
        "decision": decision,
        "checkpoint": {"path": str(ckpt), "version": args.version, "sha256": sha},
    }
    rpath = out / f"report_{args.profile}.json"
    rpath.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / f"report_{args.profile}.md").write_text(
        eval_harness.render_markdown([report]), encoding="utf-8")
    print(eval_harness.render_markdown([report]))
    return 0


def cmd_ship(args: argparse.Namespace) -> int:
    try:
        manifest = ship([Path(r) for r in args.report], Path(args.weights_root))
    except ValueError as exc:
        print(f"ship: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({p: e.get("current") for p, e in manifest["profiles"].items()}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m claude_mcp_servers.rl_reranker.training.shard_trainer")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train", help="train + evaluate one profile from export shards")
    t.add_argument("--data", action="append", required=True, help="export directory; repeatable")
    t.add_argument("--profile", required=True, choices=["qwen3", "arctic"])
    t.add_argument("--out", required=True, help="directory for the checkpoint + reports")
    t.add_argument("--version", required=True, help="weights version (semver) to stamp")
    t.add_argument("--holdout-project", default=None,
                   help="hashed project id held out of training (default: the largest "
                        "project with >= 1000 queries and <= 20%% of the data)")
    t.add_argument("--lrs", type=lambda s: [float(x) for x in s.split(",")],
                   default=[5e-6, 1e-5, 2e-5])
    t.add_argument("--epochs", type=int, default=16)
    t.add_argument("--patience", type=int, default=3)
    t.add_argument("--batch-size", type=int, default=512)
    t.add_argument("--lambda-rank", type=float, default=0.5,
                   help="weight of the within-query pairwise loss")
    t.add_argument("--pointwise-weight", type=float, default=1.0,
                   help="weight of the pointwise MSE + 0.5*BCE terms")
    t.add_argument("--listwise-weight", type=float, default=0.0,
                   help="weight of the within-query ListNet loss")
    t.add_argument("--listwise-temp", type=float, default=0.1)
    t.add_argument("--rotation-augment", action="store_true",
                   help="rotate every training batch's vectors by a random orthogonal "
                        "matrix (no memorising which nodes were useful; see train_one)")
    t.add_argument("--dev-project", default=None,
                   help="hashed id of a project kept out of training and reported per "
                        "epoch (choose the loss mix without looking at the hold-out)")
    t.add_argument("--project-cap", type=float, default=0.5)
    t.add_argument("--time-holdout", type=float, default=0.15)
    t.add_argument("--val", type=float, default=0.10)
    t.add_argument("--seed", type=int, default=1234)
    t.add_argument("--device", default="auto")
    t.set_defaults(func=cmd_train)
    s = sub.add_parser("ship", help="install passing checkpoints into the bundled weights")
    s.add_argument("--report", action="append", required=True)
    s.add_argument("--weights-root", required=True)
    s.set_defaults(func=cmd_ship)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
