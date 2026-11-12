# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The RL reranker's training objective — ONE home for the offline trainer and the server.

Readers:

* ``training/shard_trainer.py`` (offline training from export shards) and
  ``training/transfer.py`` (LOPO / adaptation simulations);
* ``online_learning.py`` (the server's per-project online learning, v0.2.106:
  owner ruling 2026-11-12, "per-project online adaptation").

Both step on :func:`batch_loss` over a SAMPLE TABLE — any object with the
attributes ``profile`` / ``device`` / ``vectors`` (``(V, D)`` float16 tensor)
and, per sample, ``q_vec`` / ``node_vec`` / ``linked_vec`` (``(N, MAX_LINKED)``,
``-1`` = empty) / ``linked_type`` / ``node_type`` (export type codes) /
``target`` / ``weight`` / ``sample_query`` (long / float tensors on
``device``). ``shard_trainer.TrainData`` is one; ``online_learning`` builds a
small one per round from a project's own citation events.

Model inputs are built exactly like ``RLModel._build_inputs`` builds them at
serving time: slot 0 the query, slot 1 the node, slots 2..6 the linked vectors
(present ones first, in random order, the rest NULL padding), per-slot cosines
``(cos(q, x), cos(n, x))``. The profile's linked-slot mask
(:func:`rl_model.linked_slots_masked`) is applied here and by the server's
scoring path alike. A node type outside the builtins gets the embedding a
serving model gives a type it registers on the fly: the mean of the builtin
rows (:data:`OTHER_TYPE_ROW`, :func:`sync_other_type_row`,
:func:`sync_dynamic_type_rows`).

The loss (per mini-batch): MSE + 0.5·BCE to the target, plus λ· the pairwise
target-difference loss over pairs of the SAME query (never across queries: a
cross-query pair teaches a per-node prior, not a ranking), optionally a
listwise (ListNet) term.

PyTorch is a hard requirement (the serving image and the training venv both
carry it): a missing torch fails here, at import, with the ImportError that
names it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F

from .rl_model import (
    N_ENTITY_TYPES,
    NODE_TYPES,
    NULL_TYPE_IDX,
    QUERY_TYPE_IDX,
    TYPE_CODE_OTHER,
    linked_slots_masked,
)

#: The type_embs row a non-builtin type trains on (the first dynamic row; it
#: is truncated away on save, as every dynamic row is).
OTHER_TYPE_ROW = N_ENTITY_TYPES


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
    data: Any, idx: Any, *, generator: Any,
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


def drop_entities(entities: Any, p: float, *, generator: Any) -> Any:
    """Zero every raw vector of a random ``p`` share of the samples.

    The slot scalars (all the cosines) and the type rows stay, so a dropped
    sample must be scored from them alone: the network cannot lean on WHICH
    vectors it has seen for every sample, only for the rest (node-level
    dropout against memorising per-node priors). Serving never drops.
    """
    if p <= 0.0:
        return entities
    keep = torch.rand(entities.shape[0], device=entities.device, generator=generator) >= p
    return entities * keep.to(entities.dtype).view(-1, 1, 1)


def batch_loss(
    net: Any,
    data: Any,
    idx: Any,
    *,
    loss: LossConfig,
    generator: Any,
    augment_rotation: bool = False,
    entity_dropout: float = 0.0,
) -> Any:
    """The training loss of the samples ``idx`` (one mini-batch).

    The one home of the per-batch objective: ``shard_trainer.train_one``, the
    adaptation simulation (``transfer``) and the server's per-project online
    learning (``online_learning``) all step on it.
    """
    sync_other_type_row(net)
    e, t, s = build_inputs(data, idx, generator=generator)
    if augment_rotation:
        e = e @ random_rotation(e.shape[-1], device=e.device, generator=generator)
    e = drop_entities(e, entity_dropout, generator=generator)
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
    return total


@torch.no_grad()
def score_samples(data: Any, net: Any, sample_idx: np.ndarray, *,
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


def cosine_scores(data: Any, sample_idx: np.ndarray, batch_size: int = 65536) -> np.ndarray:
    out = np.empty(len(sample_idx), dtype=np.float32)
    with torch.no_grad():
        for i in range(0, len(sample_idx), batch_size):
            idx = torch.from_numpy(sample_idx[i:i + batch_size]).to(data.device)
            q = data.vectors[data.q_vec[idx]].float()
            n = data.vectors[data.node_vec[idx]].float()
            out[i:i + batch_size] = _cos(q, n).cpu().numpy()
    return out



def sync_dynamic_type_rows(net: Any, rows: "list[int] | tuple[int, ...]") -> None:
    """Every row in ``rows`` (dynamic types a serving model registered) := the
    mean of the builtin rows.

    Training scores every non-builtin type on :data:`OTHER_TYPE_ROW`, kept at
    the mean of the builtins (:func:`sync_other_type_row`). A serving model
    gives each registered type its own row, initialised to that mean when it
    registers. After the builtins have trained, call this so every registered
    row is again the row training used for it — train/serve parity for
    non-builtin types.
    """
    if not rows:
        return
    with torch.no_grad():
        w = net.type_embs.weight
        mean = w[: len(NODE_TYPES)].mean(0)
        for r in rows:
            if N_ENTITY_TYPES <= int(r) < w.shape[0]:
                w[int(r)] = mean
