# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-project online learning of the RL reranker, behind a per-project serving gate.

OWNER RULINGS (2026-11-12): the product is PER-PROJECT ONLINE adaptation; it
ships OPT-IN; no global weights ship; a project's model is SERVED only while
its NDCG@10 is at least +2% above plain cosine ordering AND the MRR@10 of the
literally cited node is not below cosine's — judged on the project's
CUMULATIVE PREQUENTIAL record with an anytime-valid error budget (the rlgate2
study, 2026-11-12: a held-out window re-tested every round passed on lucky
windows whose gains the following queries did not show). Otherwise the
project keeps its own search order, exactly as with RL scoring off.

What one project's :class:`ProjectLearner` does with the citation events the
MCP posts to ``/rl_update`` (``RLServer._rl_update``):

1. **Ingest** (:meth:`ProjectLearner.ingest`): the event's targets are the
   unified target formula (``vco_lib.rl_training_targets``, the one copy) over
   its answer cosines + literal citations, its vectors the frozen
   retrieval-time ones. The query waits, unscored, for the next round; it
   remembers whether the project was being served the model when it arrived.
2. **Round** (every :attr:`OnlineConfig.round_every` new events — bounded
   compute, never per event): first the queries that arrived since the last
   round are SCORED by the model in place — model, plain cosine and the
   client's search order, per query (``ranking_metrics``) — for the project's
   :class:`PrequentialRecord`. Only then do they become trainable: the
   released queries train the project's network with the offline objective,
   ``objective.batch_loss`` (query-grouped mini-batches of
   :attr:`OnlineConfig.batch_queries` queries, pairs only within a query), at
   most :attr:`OnlineConfig.max_steps_per_round` AdamW steps per round. Inputs
   come from ``objective.build_inputs``, which applies the profile's
   linked-slot mask exactly as the serving path does. Every query in the
   record was therefore scored by a model that had not trained on it.
3. **Gate** (:func:`gate_decision`, after every round that added to the
   record): once the record holds :attr:`OnlineConfig.min_queries` queries
   and :attr:`OnlineConfig.min_cited_queries` citing ones, the k-th check
   computes ``ranking_metrics.paired_normal_bounds`` over the WHOLE record at
   confidence ``ranking_metrics.check_confidence(k)`` = ``1 - 0.10/(k(k+1))``
   (total false-pass probability <= :attr:`OnlineConfig.error_budget` over
   every check of the model). The model is served while BOTH lower bounds
   clear the bar and stops being served at the first check that fails. Until
   then — and whenever it stops passing — the server answers with the
   client's own search order (``RLServer._cache_nodes``): a project starts at
   exactly the behaviour it has without the reranker.

A round runs in three steps so a slow round never blocks serving:
:meth:`ProjectLearner.prepare_round` (under the server's lock: copies the
network + optimizer state and the round's data), :func:`compute_round`
(off the event loop, touches no shared state) and
:meth:`ProjectLearner.apply_round` (under the lock: loads the trained state
into the live model, unless the model was replaced meanwhile, appends the
round's scores to the record and applies the gate).
:meth:`ProjectLearner.run_round_now` chains the three — the replay simulation
(``training/transfer.py replay``) and the tests drive exactly this path. A
round that fails or is discarded loses its queries' scores as it loses their
training: the record skips them (it never holds a score a model computed on
a query it had trained on).

Persistence (per project, beside its checkpoint): ``online_<profile>.json``
(gate state, check count, counters) + ``online_<profile>.npz`` (the queued
queries with their de-duplicated float16 vectors, and the prequential record:
per query six float32 metrics and a served flag — no vectors, 25 bytes a
query). Both are written atomically after every applied round. A state written
for a different checkpoint (rotated / reset out of band) is discarded on load:
its record measured another model.

Deterministic: rounds happen at fixed event counts, the batches are FIFO, every
random draw (linked-slot order, dropout) comes from a generator seeded by the
round number, and the bounds are closed-form.
"""
from __future__ import annotations

import copy
import io
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

import numpy as np
import torch

from vco_lib.atomic import atomic_write_bytes, atomic_write_json

from .objective import (
    LossConfig,
    batch_loss,
    cosine_scores,
    score_samples,
    sync_dynamic_type_rows,
)
from .ranking_metrics import (
    GATE_ERROR_BUDGET,
    GATE_MIN_CITED_QUERIES,
    K,
    UNLOCK_MIN_REL_GAIN,
    QueryMetrics,
    check_confidence,
    nan_mean,
    paired_normal_bounds,
    per_query_metrics,
    relative_gain,
)
from .rl_model import (
    MAX_LINKED,
    N_ENTITY_TYPES,
    NULL_TYPE_IDX,
    UNKNOWN_TYPE_IDX,
    WEIGHT_DECAY,
    linked_slots_masked,
    type_code,
)

logger = logging.getLogger(__name__)

#: On-disk format of the learner state (``online_<profile>.json`` / ``.npz``).
#: 2 (2026-11-12): the prequential record replaced the held-out window; a
#: format-1 state is discarded on load (its window queries were never scored).
STATE_FORMAT_VERSION = 2


@dataclass(frozen=True)
class OnlineConfig:
    """The knobs of per-project online learning. The defaults are what the
    server runs; the replay simulation passes the same object.

    The gate: no verdict before the prequential record holds ``min_queries``
    queries AND ``min_cited_queries`` that literally cite a node (the MRR half
    of the bar needs them); from then on every round that adds to the record
    is a check, the k-th at confidence ``1 - error_budget / (k (k+1))``, and
    the model is served iff both lower bounds clear the bar
    (``min_ndcg_gain`` relative NDCG@``k`` gain, cited-node MRR@``k``
    difference >= 0).
    ``round_every``: new events between two rounds. ``batch_queries``: queries
    per AdamW step. ``max_steps_per_round``: compute bound of one round.
    ``max_pending_queries``: trainable queries kept waiting at most (the
    oldest are dropped untrained beyond it).
    """

    min_queries: int = 300
    min_cited_queries: int = GATE_MIN_CITED_QUERIES
    round_every: int = 32
    batch_queries: int = 8
    max_steps_per_round: int = 16
    max_pending_queries: int = 4096
    lr: float = 1e-4
    loss: LossConfig = field(default_factory=LossConfig)
    min_ndcg_gain: float = UNLOCK_MIN_REL_GAIN
    error_budget: float = GATE_ERROR_BUDGET
    k: int = K
    seed: int = 1234

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["loss"] = self.loss.as_dict()
        return d


DEFAULT_CONFIG = OnlineConfig()


# ---------------------------------------------------------------------------
# Data held per project
# ---------------------------------------------------------------------------

class VectorStore:
    """De-duplicated float16 vectors (the shared KG repeats nodes a lot)."""

    def __init__(self, dim: int) -> None:
        self.dim = int(dim)
        self._rows: list[np.ndarray] = []
        self._index: dict[bytes, int] = {}

    def __len__(self) -> int:
        return len(self._rows)

    def add(self, vec: Any) -> int:
        """Row of ``vec`` (added if new); ``-1`` for a wrong width / non-finite."""
        try:
            v = np.asarray(vec, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError):
            return -1
        if v.shape[0] != self.dim or not np.all(np.isfinite(v)):
            return -1
        h = v.astype(np.float16)
        key = h.tobytes()
        row = self._index.get(key)
        if row is None:
            row = len(self._rows)
            self._rows.append(h)
            self._index[key] = row
        return row

    def take(self, rows: np.ndarray) -> np.ndarray:
        """The vectors of ``rows`` (in that order), ``(len(rows), dim)`` float16."""
        if len(rows) == 0:
            return np.zeros((0, self.dim), dtype=np.float16)
        return np.stack([self._rows[int(i)] for i in rows])

    def compacted(self, queries: "list[LabelledQuery]") -> "VectorStore":
        """A store holding only the rows ``queries`` use; the queries' row
        indices are rewritten in place to point into it."""
        new = VectorStore(self.dim)
        remap: dict[int, int] = {}

        def m(r: int) -> int:
            got = remap.get(r)
            if got is None:
                got = len(new._rows)
                vec = self._rows[r]
                new._rows.append(vec)
                new._index[vec.tobytes()] = got
                remap[r] = got
            return got

        for q in queries:
            q.q_row = m(q.q_row)
            q.node_rows = np.array([m(int(r)) for r in q.node_rows], dtype=np.int64)
            q.linked_rows = np.array(
                [[m(int(r)) if r >= 0 else -1 for r in row] for row in q.linked_rows],
                dtype=np.int64).reshape(-1, MAX_LINKED)
        return new


@dataclass
class LabelledQuery:
    """One citation event as the trainer sees it (rows index a VectorStore)."""

    ts_ms: int
    q_row: int
    node_rows: np.ndarray      # (n,) int64
    linked_rows: np.ndarray    # (n, MAX_LINKED) int64, -1 = empty
    linked_types: np.ndarray   # (n, MAX_LINKED) int64 (type codes)
    node_types: np.ndarray     # (n,) int64 (type codes)
    target: np.ndarray         # (n,) float32
    literal: np.ndarray        # (n,) bool
    search_pos: np.ndarray     # (n,) int64 — position in the client's order
    weight: float = 1.0        # sample weight (offline soft-label policy; online 1.0)
    served: bool = False       # online: the project was served the model when it arrived
    trainable: bool = True     # online: False when training was off for the project

    @property
    def n(self) -> int:
        return int(self.node_rows.shape[0])

    @property
    def cites(self) -> bool:
        return bool(self.literal.any())

@dataclass
class SampleTable:
    """A compact sample table (``objective`` reads the torch fields)."""

    profile: str
    device: Any
    vectors: Any
    q_vec: Any
    node_vec: Any
    linked_vec: Any
    linked_type: Any
    node_type: Any
    target: Any
    weight: Any
    sample_query: Any
    np_target: np.ndarray
    np_literal: np.ndarray
    np_search_pos: np.ndarray
    first: np.ndarray
    count: np.ndarray


def build_table(queries: "list[LabelledQuery]", store: VectorStore, *,
                profile: str, device: Any) -> SampleTable:
    """Samples of ``queries`` with the vectors they use, remapped compactly."""
    count = np.array([q.n for q in queries], dtype=np.int64)
    first = np.concatenate(([0], np.cumsum(count)[:-1])).astype(np.int64)
    qv = np.repeat(np.array([q.q_row for q in queries], dtype=np.int64), count)
    nv = np.concatenate([q.node_rows for q in queries]).astype(np.int64)
    lv = np.concatenate([q.linked_rows for q in queries]).astype(np.int64)
    lt = np.concatenate([q.linked_types for q in queries]).astype(np.int64)
    nt = np.concatenate([q.node_types for q in queries]).astype(np.int64)
    tg = np.concatenate([q.target for q in queries]).astype(np.float32)
    lit = np.concatenate([q.literal for q in queries]).astype(bool)
    sp = np.concatenate([q.search_pos for q in queries]).astype(np.int64)
    used = np.unique(np.concatenate([qv, nv, lv[lv >= 0]]))
    remap = np.full(int(used.max()) + 1 if used.size else 1, -1, dtype=np.int64)
    remap[used] = np.arange(used.size, dtype=np.int64)
    vecs = torch.from_numpy(np.ascontiguousarray(store.take(used))).to(device)
    lv_m = np.where(lv >= 0, remap[np.clip(lv, 0, None)], -1)

    def t(a: np.ndarray, dtype: Any) -> Any:
        return torch.from_numpy(np.ascontiguousarray(a)).to(device, dtype=dtype)

    return SampleTable(
        profile=profile, device=device, vectors=vecs,
        q_vec=t(remap[qv], torch.long), node_vec=t(remap[nv], torch.long),
        linked_vec=t(lv_m, torch.long), linked_type=t(lt, torch.long),
        node_type=t(nt, torch.long), target=t(tg, torch.float32),
        weight=t(np.repeat(np.array([q.weight for q in queries], dtype=np.float32), count),
                 torch.float32),
        sample_query=t(np.repeat(np.arange(len(queries), dtype=np.int64), count), torch.long),
        np_target=tg, np_literal=lit, np_search_pos=sp, first=first, count=count,
    )


def query_samples(tbl: SampleTable, qs: Any) -> np.ndarray:
    """The sample indices of ``tbl``'s queries ``qs`` (a mini-batch)."""
    return np.concatenate([
        np.arange(int(tbl.first[int(i)]), int(tbl.first[int(i)]) + int(tbl.count[int(i)]))
        for i in qs
    ]).astype(np.int64)


def labelled_query(task: dict, store: VectorStore, *, profile: str, ts_ms: int,
                   targets: "dict[str, float] | None" = None,
                   weight: float = 1.0) -> "tuple[Optional[LabelledQuery], str]":
    """The trainer's view of one ``/rl_update`` task block (``rl_client``
    ``RLClient.rl_update_v3``): ``(query, "")`` or ``(None, why skipped)``.

    Samples follow the training export (``training/export.py``): the target
    formula over the event's answer cosines, one sample per node that has a
    target and a vector (last node per title wins), linked vectors packed
    from slot 0 (empty ones skipped), type codes through ``type_code``. Online
    events are full-window citations, so every sample weighs 1.0.

    ``targets`` / ``weight``: an offline caller that already applied the
    trainer's per-citation policy (``offline_trainer.citation_targets``: soft
    labels, the binary-citation path) passes its targets and weight; the
    ``/rl_update`` path never does — the server recomputes the targets from
    the raw inputs, whatever a client sent.
    """
    from vco_lib.rl_training_targets import compute_unified_targets

    literal_cited = task.get("literal_cited") or {}
    if targets is None:
        cosine_sims = task.get("cosine_sims") or {}
        if not isinstance(cosine_sims, dict) or not cosine_sims:
            return None, "no answer cosines"
        cross = task.get("cross_encoder_cited")
        targets, _cited = compute_unified_targets(
            {str(k): float(v) for k, v in cosine_sims.items() if isinstance(v, (int, float))},
            literal_cited=literal_cited if isinstance(literal_cited, dict) else {},
            cross_encoder_cited=cross if isinstance(cross, dict) else None,
        )
    q_emb = task.get("query_emb")
    q_row = store.add(q_emb if q_emb is not None else [])
    if q_row < 0:
        return None, "query vector missing or of the wrong width"
    by_title: dict[str, tuple[int, dict]] = {}
    for pos, node in enumerate(task.get("nodes_packed") or []):
        if isinstance(node, dict) and node.get("n_emb") is not None:
            by_title[str(node.get("title", ""))] = (pos, node)
    masked = linked_slots_masked(profile)
    rows: list[int] = []
    l_rows: list[list[int]] = []
    l_types: list[list[int]] = []
    n_types: list[int] = []
    tgt: list[float] = []
    lit: list[bool] = []
    pos_l: list[int] = []
    for title, target in targets.items():
        hit = by_title.get(title)
        if hit is None:
            continue
        pos, node = hit
        n_row = store.add(node.get("n_emb"))
        if n_row < 0:
            continue
        slots = [-1] * MAX_LINKED
        stypes = [NULL_TYPE_IDX] * MAX_LINKED
        if not masked:
            names = node.get("linked_type_names") or []
            slot = 0
            for li, lvec in enumerate((node.get("linked_embs") or [])[:MAX_LINKED]):
                if lvec is None or len(lvec) == 0:
                    continue
                v_row = store.add(lvec)
                if v_row < 0:
                    continue
                slots[slot] = v_row
                stypes[slot] = type_code(names[li] if li < len(names) else "",
                                         empty=UNKNOWN_TYPE_IDX)
                slot += 1
        rows.append(n_row)
        l_rows.append(slots)
        l_types.append(stypes)
        n_types.append(type_code(node.get("node_type") or "", empty=0))
        tgt.append(float(target))
        lit.append(bool((literal_cited or {}).get(title)))
        pos_l.append(int(pos))
    if not rows:
        return None, "no node carries both a target and a vector"
    return LabelledQuery(
        ts_ms=int(ts_ms), q_row=q_row,
        node_rows=np.array(rows, dtype=np.int64),
        linked_rows=np.array(l_rows, dtype=np.int64).reshape(-1, MAX_LINKED),
        linked_types=np.array(l_types, dtype=np.int64).reshape(-1, MAX_LINKED),
        node_types=np.array(n_types, dtype=np.int64),
        target=np.array(tgt, dtype=np.float32),
        literal=np.array(lit, dtype=bool),
        search_pos=np.array(pos_l, dtype=np.int64),
        weight=float(weight),
    ), ""


def fit(model: Any, queries: "list[LabelledQuery]", store: VectorStore, *, profile: str,
        epochs: int, batch_queries: int, lr: float, seed: int, shuffle: bool,
        loss: "LossConfig | None" = None) -> int:
    """Train ``model`` in place on ``queries`` with the ONE objective
    (``objective.batch_loss``, query-grouped mini-batches of ``batch_queries``
    queries) and the model's own AdamW at ``lr``. Returns the steps taken.

    The offline path of the server (``/global/retrain``) — the online rounds
    use :func:`compute_round` on a copy instead, so serving never waits.
    ``shuffle``: a seeded permutation per epoch; False keeps time order (an
    online-style replay). Deterministic for given inputs (``round_rng``).
    """
    if not queries or epochs <= 0:
        return 0
    loss = loss or LossConfig()
    dev = torch.device(model.device)
    tbl = build_table(queries, store, profile=profile, device=dev)
    opt = model._opt
    for g in opt.param_groups:
        g["lr"] = lr
        g["weight_decay"] = WEIGHT_DECAY
    rng = np.random.default_rng(seed)
    steps = 0
    with _ROUND_LOCK, round_rng(seed, dev):
        gen = torch.Generator(device=dev)
        gen.manual_seed(seed)
        for _ in range(epochs):
            order = rng.permutation(len(queries)) if shuffle else np.arange(len(queries))
            for i in range(0, len(order), max(1, batch_queries)):
                qs = order[i:i + batch_queries]
                sidx = query_samples(tbl, qs)
                idx = torch.from_numpy(sidx.astype(np.int64)).to(dev)
                model._net.train()
                total = batch_loss(model._net, tbl, idx, loss=loss, generator=gen)
                opt.zero_grad(set_to_none=True)
                total.backward()
                opt.step()
                steps += 1
    model.record_local_train_steps(steps)
    sync_dynamic_type_rows(model._net, [
        idx for idx in model._type_registry.values() if idx >= N_ENTITY_TYPES])
    return steps


# ---------------------------------------------------------------------------
# The prequential record and the gate
# ---------------------------------------------------------------------------

#: The record's per-query columns (float32; NaN = undefined for that query:
#: NDCG when the query's ideal DCG is 0, MRR when it cites no node).
RECORD_COLUMNS = ("model_ndcg", "cos_ndcg", "search_ndcg", "model_mrr", "cos_mrr", "search_mrr")


@dataclass
class QueryScores:
    """One round's arriving queries, scored BEFORE the round trains on them:
    the model in place, plain cosine and the client's search order, and
    whether the project was served the model when each query arrived."""

    model: QueryMetrics
    cosine: QueryMetrics
    search: QueryMetrics
    served: np.ndarray        # (n,) bool

    def __len__(self) -> int:
        return int(len(self.served))


class PrequentialRecord:
    """A project's cumulative prequential record: per query, the metrics of
    the model in place when it arrived (before it trained on it), of plain
    cosine and of the client's search order, and whether the model was being
    served. Column buffers that double when full (amortised O(1) append);
    25 bytes a query, no vectors."""

    def __init__(self) -> None:
        self._n = 0
        self._cols: dict[str, np.ndarray] = {c: np.zeros(0, np.float32) for c in RECORD_COLUMNS}
        self._served = np.zeros(0, dtype=bool)

    def __len__(self) -> int:
        return self._n

    def _reserve(self, extra: int) -> None:
        need = self._n + extra
        cap = len(self._served)
        if need <= cap:
            return
        new = max(need, 2 * cap, 256)
        for c in RECORD_COLUMNS:
            col = np.full(new, np.nan, dtype=np.float32)
            col[: self._n] = self._cols[c][: self._n]
            self._cols[c] = col
        served = np.zeros(new, dtype=bool)
        served[: self._n] = self._served[: self._n]
        self._served = served

    def _put(self, values: "dict[str, np.ndarray]", served: np.ndarray) -> None:
        m = int(len(served))
        if m == 0:
            return
        self._reserve(m)
        sl = slice(self._n, self._n + m)
        for c in RECORD_COLUMNS:
            self._cols[c][sl] = np.asarray(values[c], dtype=np.float32)
        self._served[sl] = np.asarray(served, dtype=bool)
        self._n += m

    def append(self, scores: QueryScores) -> None:
        self._put({"model_ndcg": scores.model.ndcg, "cos_ndcg": scores.cosine.ndcg,
                   "search_ndcg": scores.search.ndcg, "model_mrr": scores.model.mrr,
                   "cos_mrr": scores.cosine.mrr, "search_mrr": scores.search.mrr},
                  scores.served)

    def column(self, name: str) -> np.ndarray:
        return self._cols[name][: self._n]

    @property
    def served(self) -> np.ndarray:
        return self._served[: self._n]

    @property
    def n_cited(self) -> int:
        """Queries that literally cite a node (their MRR is defined)."""
        return int(np.sum(~np.isnan(self.column("model_mrr"))))

    def metrics(self) -> "tuple[QueryMetrics, QueryMetrics]":
        """(model, plain cosine) per-query metrics of the whole record."""
        def qm(prefix: str) -> QueryMetrics:
            mrr = self.column(f"{prefix}_mrr")
            return QueryMetrics(ndcg=self.column(f"{prefix}_ndcg"), mrr=mrr,
                                recall=np.full(mrr.shape, np.nan, dtype=np.float32))
        return qm("model"), qm("cos")

    def summary(self, rows: "np.ndarray | None" = None) -> dict[str, Any]:
        """Point estimates over ``rows`` (a bool mask; default every query):
        means of the defined values and the relative NDCG gain over cosine.
        JSON-safe (undefined = ``None``)."""
        def mean(name: str) -> float:
            col = self.column(name)
            return nan_mean(col if rows is None else col[rows])

        n = self._n if rows is None else int(np.sum(rows))
        mrr = self.column("model_mrr")
        cited = int(np.sum(~np.isnan(mrr if rows is None else mrr[rows])))
        out: dict[str, Any] = {
            "queries": int(n), "cited_queries": cited,
            "ndcg": mean("model_ndcg"), "cos_ndcg": mean("cos_ndcg"),
            "search_ndcg": mean("search_ndcg"), "mrr": mean("model_mrr"),
            "cos_mrr": mean("cos_mrr"), "search_mrr": mean("search_mrr")}
        out["ndcg_gain"] = relative_gain(out["ndcg"], out["cos_ndcg"])
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                for k, v in out.items()}

    def clear(self) -> None:
        self._n = 0
        self._cols = {c: np.zeros(0, np.float32) for c in RECORD_COLUMNS}
        self._served = np.zeros(0, dtype=bool)

    def arrays(self) -> "dict[str, np.ndarray]":
        """The record as ``.npz`` arrays (``rec_<column>``, ``rec_served``)."""
        d = {f"rec_{c}": self.column(c).copy() for c in RECORD_COLUMNS}
        d["rec_served"] = self.served.astype(np.int8)
        return d

    @classmethod
    def from_arrays(cls, d: "dict[str, np.ndarray]") -> "PrequentialRecord":
        rec = cls()
        served = np.asarray(d["rec_served"]).astype(bool)
        values = {c: np.asarray(d[f"rec_{c}"]) for c in RECORD_COLUMNS}
        if any(v.shape != served.shape for v in values.values()):
            raise ValueError("prequential record columns differ in length")
        rec._put(values, served)
        return rec


@dataclass(frozen=True)
class Check:
    """The k-th check of a project's model on its prequential record: the
    point estimates over the whole record, the lower bounds at the check's
    confidence, and the decision."""

    k: int
    at_ms: int
    confidence: float
    n_queries: int
    n_cited: int
    ndcg: float
    cos_ndcg: float
    mrr: float
    cos_mrr: float
    ndcg_gain_lb: float
    mrr_diff_lb: float
    serve: bool
    reason: str

    @property
    def ndcg_gain(self) -> float:
        return relative_gain(self.ndcg, self.cos_ndcg)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe: every non-finite number (NaN metric, ``-inf`` bound) is
        ``None`` (strict JSON for the status and the hub row)."""
        d = asdict(self)
        d["ndcg_gain"] = self.ndcg_gain
        d["error_rate"] = 1.0 - self.confidence
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                for k, v in d.items()}


def _pct(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{100 * x:+.2f}%"


def _diff(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:+.3f}"


def _conf(c: float) -> str:
    """A check's confidence for a reason line: ``95.0%`` while the error rate
    is >= 0.1%, else ``1-3.1e-08`` (late checks run at tiny error rates)."""
    alpha = 1.0 - c
    return f"{100 * c:.1f}%" if alpha >= 1e-3 else f"1-{alpha:.1e}"


def ready_for_check(record: PrequentialRecord, cfg: OnlineConfig) -> bool:
    """Whether the record is large enough for any verdict."""
    return len(record) >= cfg.min_queries and record.n_cited >= cfg.min_cited_queries


def collecting_reason(record: PrequentialRecord, cfg: OnlineConfig) -> str:
    return (f"collecting queries scored before training ({len(record)}/{cfg.min_queries} "
            f"queries, {record.n_cited}/{cfg.min_cited_queries} citing a node); serving "
            "plain search order meanwhile")


def gate_decision(record: PrequentialRecord, k: int, cfg: OnlineConfig, *,
                  at_ms: int) -> Check:
    """The ``k``-th check (``k >= 1``) of the model on ``record``: the owner's
    bar applied to the one-sided lower bounds at ``check_confidence(k)`` over
    the WHOLE record (the point estimates are reported, never decisive). Pure."""
    conf = check_confidence(k, cfg.error_budget)
    model, cos = record.metrics()
    b = paired_normal_bounds(model, cos, confidence=conf)
    s = record.summary()

    def f(key: str) -> float:
        v = s[key]
        return float("nan") if v is None else float(v)

    ndcg, cos_ndcg, mrr, cos_mrr = f("ndcg"), f("cos_ndcg"), f("mrr"), f("cos_mrr")
    gain = relative_gain(ndcg, cos_ndcg)
    glb, dlb = b.ndcg_gain_lb, b.mrr_diff_lb
    c, bar = _conf(conf), f"{100 * cfg.min_ndcg_gain:+.2f}%"
    n, nc = len(record), record.n_cited
    if not (glb >= cfg.min_ndcg_gain):  # NaN / -inf fail
        serve, why = False, (
            f"check {k}: NDCG@{cfg.k} {_pct(gain)} vs plain cosine on {n} queries scored "
            f"before training, lower bound {_pct(glb)} at confidence {c}; the bound needs {bar}")
    elif not (dlb >= 0.0):
        serve, why = False, (
            f"check {k}: NDCG@{cfg.k} {_pct(gain)} (bound {_pct(glb)}) but the cited node's "
            f"MRR@{cfg.k} {mrr:.3f} vs plain cosine's {cos_mrr:.3f} on {nc} citing queries: "
            f"the lower bound of the difference at confidence {c} is {_diff(dlb)}, below 0")
    else:
        serve, why = True, (
            f"check {k}: NDCG@{cfg.k} {_pct(gain)} over plain cosine on {n} queries scored "
            f"before training (lower bound {_pct(glb)} >= {bar} at confidence {c}), "
            f"cited-node MRR@{cfg.k} {mrr:.3f} vs {cos_mrr:.3f} on {nc} citing queries "
            f"(lower bound of the difference {_diff(dlb)} >= 0)")
    return Check(k=int(k), at_ms=int(at_ms), confidence=conf, n_queries=n, n_cited=nc,
                 ndcg=ndcg, cos_ndcg=cos_ndcg, mrr=mrr, cos_mrr=cos_mrr,
                 ndcg_gain_lb=glb, mrr_diff_lb=dlb, serve=serve, reason=why)


def score_queries(net: Any, tbl: SampleTable,
                  k: int = K) -> "tuple[QueryMetrics, QueryMetrics, QueryMetrics]":
    """Per-query metrics of ``tbl``'s queries: (``net``, plain cosine, the
    client's search order)."""
    sidx = np.arange(len(tbl.np_target), dtype=np.int64)
    sc = score_samples(tbl, net, sidx)
    cs = cosine_scores(tbl, sidx)
    gains, rel = tbl.np_target, tbl.np_literal
    m = per_query_metrics(sc, gains, rel, tbl.first, tbl.count, k)
    c = per_query_metrics(cs, gains, rel, tbl.first, tbl.count, k)
    s = per_query_metrics(-tbl.np_search_pos.astype(np.float64), gains, rel,
                          tbl.first, tbl.count, k)
    return m, c, s


# ---------------------------------------------------------------------------
# A round
# ---------------------------------------------------------------------------

@dataclass
class RoundJob:
    generation: int
    net: Any
    opt_state: dict
    profile: str
    device: Any
    train: Optional[SampleTable]
    steps: "list[np.ndarray]"     # per step: query indices into ``train``
    fresh: Optional[SampleTable]  # queries arrived since the last round: scored FIRST
    fresh_served: np.ndarray      # (n_fresh,) bool
    seed: int
    cfg: OnlineConfig
    now_ms: int


@dataclass
class RoundResult:
    generation: int
    state_dict: "dict[str, Any] | None"
    opt_state: "dict | None"
    steps: int
    trained_queries: int
    scores: Optional[QueryScores]
    now_ms: int
    runtime_s: float


#: Rounds run one at a time per process: they seed torch's global RNG (the
#: network's dropout draws from it, not from the round's generator), and two
#: projects' rounds interleaving would make both non-reproducible.
_ROUND_LOCK = threading.Lock()


def round_rng(seed: int, device: Any):
    """Context of a round's randomness: torch's global RNG (CPU, and the
    round's CUDA device) forked and seeded with ``seed``, restored afterwards.
    Dropout in training mode is then a function of the round."""
    devices = [device.index or 0] if getattr(device, "type", "cpu") == "cuda" else []
    fork = torch.random.fork_rng(devices=devices)

    class _Ctx:
        def __enter__(self) -> None:
            fork.__enter__()
            torch.manual_seed(int(seed))

        def __exit__(self, *exc: Any) -> None:
            fork.__exit__(*exc)

    return _Ctx()


def compute_round(job: RoundJob) -> RoundResult:
    """Score the round's arriving queries with the copied network, THEN train
    it on the round's batches.

    Runs off the event loop; reads only ``job`` (its network is a private
    copy). The live model is updated by :meth:`ProjectLearner.apply_round`.
    Deterministic for a given job (``round_rng``, ``_ROUND_LOCK``).
    """
    with _ROUND_LOCK, round_rng(job.seed, job.device):
        return _compute_round(job)


def _compute_round(job: RoundJob) -> RoundResult:
    t0 = time.monotonic()
    net = job.net
    scores = None
    if job.fresh is not None:
        # Prequential: the model in place, before it trains on these queries.
        m, c, s = score_queries(net, job.fresh, job.cfg.k)
        scores = QueryScores(model=m, cosine=c, search=s,
                             served=np.asarray(job.fresh_served, dtype=bool))
    trained = 0
    state = opt_state = None
    if job.train is not None and job.steps:
        opt = torch.optim.AdamW(net.parameters(), lr=job.cfg.lr, weight_decay=WEIGHT_DECAY)
        if job.opt_state:
            try:
                opt.load_state_dict(job.opt_state)
            except (ValueError, KeyError) as exc:  # a fresh optimizer is the safe fallback
                logger.warning("online round: optimizer state not reusable (%s); fresh AdamW", exc)
        for g in opt.param_groups:
            g["lr"] = job.cfg.lr
            g["weight_decay"] = WEIGHT_DECAY
        gen = torch.Generator(device=job.device)
        gen.manual_seed(job.seed)
        tbl = job.train
        for qs in job.steps:
            sidx = query_samples(tbl, qs)
            idx = torch.from_numpy(sidx.astype(np.int64)).to(job.device)
            net.train()
            loss = batch_loss(net, tbl, idx, loss=job.cfg.loss, generator=gen)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            trained += len(qs)
        state = {k: v.detach().clone() for k, v in net.state_dict().items()}
        opt_state = opt.state_dict()
    return RoundResult(generation=job.generation, state_dict=state, opt_state=opt_state,
                       steps=len(job.steps) if state is not None else 0,
                       trained_queries=trained, scores=scores, now_ms=job.now_ms,
                       runtime_s=time.monotonic() - t0)


# ---------------------------------------------------------------------------
# The learner
# ---------------------------------------------------------------------------

class ProjectLearner:
    """One project's online learning + serving gate (see the module docstring).

    Not thread-safe by itself: the server calls :meth:`ingest`,
    :meth:`prepare_round` and :meth:`apply_round` under its routing lock and
    only :func:`compute_round` outside it.
    """

    def __init__(self, *, profile: str, emb_dim: int,
                 cfg: OnlineConfig = DEFAULT_CONFIG) -> None:
        self.profile = profile
        self.emb_dim = int(emb_dim)
        self.cfg = cfg
        self._store = VectorStore(emb_dim)
        self._fresh: list[LabelledQuery] = []           # arrived, not yet scored
        self._pending: Deque[LabelledQuery] = deque()   # scored, trainable, oldest first
        self.record = PrequentialRecord()
        self.checks = 0               # k: checks made on the current model's record
        self._generation = 0          # bumped when the model is replaced
        self._round_in_flight = False
        self._events_since_round = 0
        self.serving = False
        self.reason = collecting_reason(self.record, cfg)
        self.last_check: Optional[Check] = None
        self.counters: dict[str, int] = {
            "events_seen": 0, "events_skipped": 0, "queries_trained": 0,
            "train_steps": 0, "rounds": 0, "dropped_untrained": 0,
            "serving_turned_on": 0, "serving_turned_off": 0, "model_replaced": 0,
        }
        self.training_enabled = True
        self.last_skip_reason = ""
        self.model_fingerprint: dict[str, Any] = {}

    # -- events ---------------------------------------------------------

    def ingest(self, task: dict, *, ts_ms: int, train: bool = True) -> bool:
        """Add one citation event. ``train=False`` (online training switched
        off for the project): the event is still scored into the record — the
        gate keeps judging the frozen model — but is never trained on.
        Returns whether the event was kept."""
        self.training_enabled = bool(train)
        q, why = labelled_query(task, self._store, profile=self.profile, ts_ms=ts_ms)
        if q is None:
            self.counters["events_skipped"] += 1
            self.last_skip_reason = why
            return False
        q.served = self.serving
        q.trainable = bool(train)
        self.counters["events_seen"] += 1
        self._fresh.append(q)
        self._events_since_round += 1
        return True

    @property
    def last_query(self) -> LabelledQuery:
        """The query :meth:`ingest` kept last (the replay compares its targets)."""
        return self._fresh[-1]

    def round_due(self) -> bool:
        return (not self._round_in_flight) and self._events_since_round >= self.cfg.round_every

    @property
    def round_in_flight(self) -> bool:
        return self._round_in_flight

    # -- rounds ---------------------------------------------------------

    def prepare_round(self, model: Any, *, now_ms: int) -> Optional[RoundJob]:
        """Snapshot what one round needs (call under the server's lock): the
        queries that arrived since the last round (scored first, by the copy
        of the model in place) and the trainable batches — the arrivals among
        them, since the copy scores them before any step."""
        if self._round_in_flight:
            return None
        cfg = self.cfg
        fresh, self._fresh = self._fresh, []
        for q in fresh:
            if q.trainable:
                self._pending.append(q)
            else:
                self.counters["dropped_untrained"] += 1
        while len(self._pending) > cfg.max_pending_queries:
            self._pending.popleft()
            self.counters["dropped_untrained"] += 1
        n_steps = min(len(self._pending) // cfg.batch_queries, cfg.max_steps_per_round)
        train_q = [self._pending.popleft() for _ in range(n_steps * cfg.batch_queries)]
        if not train_q and not fresh:
            return None
        dev = torch.device(model.device)
        train = build_table(train_q, self._store, profile=self.profile, device=dev) if train_q else None
        steps = [np.arange(i * cfg.batch_queries, (i + 1) * cfg.batch_queries)
                 for i in range(n_steps)]
        fresh_tbl = build_table(fresh, self._store, profile=self.profile, device=dev) if fresh else None
        self._compact_store()
        self._round_in_flight = True
        self._events_since_round = 0
        return RoundJob(
            generation=self._generation, net=copy.deepcopy(model._net),
            opt_state=copy.deepcopy(model._opt.state_dict()), profile=self.profile,
            device=dev, train=train, steps=steps, fresh=fresh_tbl,
            fresh_served=np.array([q.served for q in fresh], dtype=bool),
            seed=cfg.seed + self.counters["rounds"], cfg=cfg, now_ms=now_ms,
        )

    def apply_round(self, model: Any, result: RoundResult) -> bool:
        """Load the round into the live model, add its scores to the record
        and apply the gate (under the lock). Returns False when the model was
        replaced while the round ran (the round is discarded: it trained and
        scored a network no longer served)."""
        self._round_in_flight = False
        if result.generation != self._generation:
            return False
        self.counters["rounds"] += 1
        if result.state_dict is not None:
            model._net.load_state_dict(result.state_dict)
            if result.opt_state is not None:
                model._opt.load_state_dict(result.opt_state)
            model.record_local_train_steps(result.steps)
            sync_dynamic_type_rows(model._net, [
                idx for idx in model._type_registry.values() if idx >= N_ENTITY_TYPES])
            self.counters["train_steps"] += result.steps
            self.counters["queries_trained"] += result.trained_queries
        added = 0
        if result.scores is not None:
            self.record.append(result.scores)
            added = len(result.scores)
        if added:
            self._check(at_ms=result.now_ms)
        self.model_fingerprint = model_fingerprint(model)
        return True

    def _check(self, *, at_ms: int) -> None:
        """The record grew: the next check, once the record is large enough."""
        if not ready_for_check(self.record, self.cfg):
            self.last_check = None
            self._set_serving(False, collecting_reason(self.record, self.cfg))
            return
        self.checks += 1
        self.last_check = gate_decision(self.record, self.checks, self.cfg, at_ms=at_ms)
        self._set_serving(self.last_check.serve, self.last_check.reason)

    def _set_serving(self, serve: bool, why: str) -> None:
        if serve and not self.serving:
            self.counters["serving_turned_on"] += 1
        if self.serving and not serve:
            self.counters["serving_turned_off"] += 1
        self.serving, self.reason = serve, why

    def _compact_store(self) -> None:
        """Drop vectors no queued query uses any more (trained / dropped
        queries leave theirs behind). Bounded memory on a long-running server;
        runs when at least half the store is garbage."""
        live = list(self._fresh) + list(self._pending)
        approx_live = sum(1 + 2 * q.n for q in live)  # cheap upper-ish bound
        if len(self._store) > max(4096, 2 * approx_live):
            self._store = self._store.compacted(live)

    def abandon_round(self) -> None:
        """The round failed: its queries' training and scores are lost, the
        gate stays."""
        self._round_in_flight = False

    def run_round_now(self, model: Any, *, now_ms: int) -> Optional[RoundResult]:
        """prepare + compute + apply, synchronously (the replay and the tests)."""
        job = self.prepare_round(model, now_ms=now_ms)
        if job is None:
            return None
        result = compute_round(job)
        self.apply_round(model, result)
        return result

    def on_model_replaced(self, model: Any, why: str) -> None:
        """The project's model was swapped (rotate / reset / global): the
        record measured the old model, so it is cleared and the gate starts
        over (check count and error budget included); queued data is dropped
        too (the new model may have trained on it)."""
        self._generation += 1
        self._round_in_flight = False
        dropped = len(self._fresh) + len(self._pending)
        self._fresh.clear()
        self._pending.clear()
        self._store = VectorStore(self.emb_dim)
        self.record.clear()
        self.checks = 0
        self.counters["model_replaced"] += 1
        self.counters["dropped_untrained"] += dropped
        if self.serving:
            self.counters["serving_turned_off"] += 1
        self.serving = False
        self.last_check = None
        self.reason = f"model replaced ({why}); re-checking on new queries"
        self.model_fingerprint = model_fingerprint(model)

    # -- status / persistence -------------------------------------------

    def status(self) -> dict[str, Any]:
        cfg = self.cfg
        rec = self.record
        return {
            "profile": self.profile,
            "serving": self.serving,
            "reason": self.reason,
            "training": "on" if self.training_enabled else "off (online training disabled for this project)",
            "record": rec.summary(),
            "served_searches": rec.summary(rec.served),
            "checks": self.checks,
            "last_check": self.last_check.as_dict() if self.last_check else None,
            "waiting_to_score": len(self._fresh),
            "pending_train_queries": len(self._pending),
            "counters": dict(self.counters),
            "last_skip_reason": self.last_skip_reason or None,
            "bar": {"min_ndcg_gain": cfg.min_ndcg_gain, "cited_mrr_not_below_cosine": True,
                    "judged_on": "cumulative prequential record (each query scored by the "
                                 "model in place when it arrived, before training on it)",
                    "bounds": "one-sided paired normal approximation",
                    "confidence_schedule": "1 - error_budget / (k (k+1)) at the k-th check",
                    "error_budget": cfg.error_budget, "k": cfg.k,
                    "min_queries": cfg.min_queries, "min_cited_queries": cfg.min_cited_queries},
        }

    def save(self, directory: Path) -> None:
        """Write ``online_<profile>.json`` + ``.npz`` atomically."""
        directory.mkdir(parents=True, exist_ok=True)
        live = list(self._fresh) + list(self._pending)
        used = np.unique(np.concatenate(
            [np.array([q.q_row for q in live], dtype=np.int64)]
            + [q.node_rows for q in live] + [q.linked_rows[q.linked_rows >= 0] for q in live]
        )) if live else np.zeros(0, dtype=np.int64)
        remap = np.full(len(self._store) or 1, -1, dtype=np.int64)
        remap[used] = np.arange(used.size)

        def cat(get: Any, dtype: Any, width: int = 0) -> np.ndarray:
            if not live:
                return np.zeros((0, width) if width else 0, dtype=dtype)
            return np.concatenate([get(q) for q in live]).astype(dtype)

        lr = cat(lambda q: q.linked_rows, np.int64, MAX_LINKED)
        buf = io.BytesIO()
        np.savez(
            buf,
            vectors=self._store.take(used),
            q_ts=np.array([q.ts_ms for q in live], dtype=np.int64),
            q_row=remap[np.array([q.q_row for q in live], dtype=np.int64)] if live
            else np.zeros(0, np.int64),
            q_n=np.array([q.n for q in live], dtype=np.int64),
            q_weight=np.array([q.weight for q in live], dtype=np.float32),
            q_pending=np.array([0] * len(self._fresh) + [1] * len(self._pending), dtype=np.int8),
            q_served=np.array([q.served for q in live], dtype=np.int8),
            q_trainable=np.array([q.trainable for q in live], dtype=np.int8),
            node_row=remap[cat(lambda q: q.node_rows, np.int64)] if live else np.zeros(0, np.int64),
            linked_row=np.where(lr >= 0, remap[np.clip(lr, 0, None)], -1) if live
            else np.zeros((0, MAX_LINKED), np.int64),
            linked_type=cat(lambda q: q.linked_types, np.int64, MAX_LINKED),
            node_type=cat(lambda q: q.node_types, np.int64),
            target=cat(lambda q: q.target, np.float32),
            literal=cat(lambda q: q.literal, np.int8),
            search_pos=cat(lambda q: q.search_pos, np.int64),
            **self.record.arrays(),
        )
        atomic_write_bytes(directory / f"online_{self.profile}.npz", buf.getvalue())
        atomic_write_json(directory / f"online_{self.profile}.json", {
            "format_version": STATE_FORMAT_VERSION,
            "profile": self.profile,
            "emb_dim": self.emb_dim,
            "serving": self.serving,
            "reason": self.reason,
            "checks": self.checks,
            "last_check_at_ms": self.last_check.at_ms if self.last_check else None,
            "counters": self.counters,
            "events_since_round": self._events_since_round,
            "training_enabled": self.training_enabled,
            "model_fingerprint": self.model_fingerprint,
            "config": self.cfg.as_dict(),
        })

    @classmethod
    def load(cls, directory: Path, *, profile: str, emb_dim: int, model: Any,
             cfg: OnlineConfig = DEFAULT_CONFIG) -> "ProjectLearner":
        """The persisted learner for ``model``, or a fresh one.

        A state recorded for a different checkpoint (file name or local steps
        differ: rotated / reset outside this process) is discarded — its
        record measured another model — and the gate starts over. The serving
        decision is re-derived from the restored record at the restored check
        number (no new check, so no error budget is spent), so a changed bar
        applies at once.
        """
        lr_ = cls(profile=profile, emb_dim=emb_dim, cfg=cfg)
        lr_.model_fingerprint = model_fingerprint(model)
        meta_p = directory / f"online_{profile}.json"
        arr_p = directory / f"online_{profile}.npz"
        if not meta_p.is_file():
            return lr_
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("online learner state %s unreadable (%s); starting over", meta_p, exc)
            return lr_
        if (meta.get("format_version") != STATE_FORMAT_VERSION or meta.get("profile") != profile
                or int(meta.get("emb_dim") or 0) != int(emb_dim)):
            logger.warning("online learner state %s is for another format/profile; starting over", meta_p)
            return lr_
        counters = meta.get("counters") or {}
        for k in lr_.counters:
            if isinstance(counters.get(k), int):
                lr_.counters[k] = int(counters[k])
        if meta.get("model_fingerprint") != lr_.model_fingerprint:
            lr_.counters["model_replaced"] += 1
            lr_.reason = "model replaced since the last run; re-checking on new queries"
            return lr_
        lr_.serving = bool(meta.get("serving"))
        lr_.training_enabled = bool(meta.get("training_enabled", True))
        lr_._events_since_round = int(meta.get("events_since_round") or 0)
        lr_.checks = int(meta.get("checks") or 0)
        try:
            lr_._load_arrays(arr_p)
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("online learner data %s unreadable (%s); record starts over", arr_p, exc)
            lr_._fresh.clear()
            lr_._pending.clear()
            lr_._store = VectorStore(emb_dim)
            lr_.record.clear()
            lr_.checks = 0
            lr_.serving = False
            lr_.reason = "prequential record lost (unreadable state); re-checking on new queries"
            return lr_
        if lr_.checks > 0 and ready_for_check(lr_.record, cfg):
            lr_.last_check = gate_decision(lr_.record, lr_.checks, cfg,
                                           at_ms=int(meta.get("last_check_at_ms") or 0))
            lr_.serving, lr_.reason = lr_.last_check.serve, lr_.last_check.reason
        else:
            lr_.serving, lr_.reason = False, collecting_reason(lr_.record, cfg)
        return lr_

    def _load_arrays(self, path: Path) -> None:
        with np.load(path, allow_pickle=False) as z:
            d = {k: z[k] for k in z.files}
        self.record = PrequentialRecord.from_arrays(d)
        for row in d["vectors"]:
            self._store.add(row.astype(np.float32))
        # rows are unique, so store rows == file rows
        off = 0
        for i in range(len(d["q_ts"])):
            n = int(d["q_n"][i])
            sl = slice(off, off + n)
            q = LabelledQuery(
                ts_ms=int(d["q_ts"][i]), q_row=int(d["q_row"][i]),
                node_rows=d["node_row"][sl].astype(np.int64),
                linked_rows=d["linked_row"][sl].astype(np.int64).reshape(-1, MAX_LINKED),
                linked_types=d["linked_type"][sl].astype(np.int64).reshape(-1, MAX_LINKED),
                node_types=d["node_type"][sl].astype(np.int64),
                target=d["target"][sl].astype(np.float32),
                literal=d["literal"][sl].astype(bool),
                search_pos=d["search_pos"][sl].astype(np.int64),
                weight=float(d["q_weight"][i]),
                served=bool(d["q_served"][i]),
                trainable=bool(d["q_trainable"][i]),
            )
            off += n
            (self._pending.append if int(d["q_pending"][i]) else self._fresh.append)(q)


def model_fingerprint(model: Any) -> dict[str, Any]:
    """What identifies the checkpoint a learner state belongs to."""
    return {
        "file": Path(str(getattr(model, "_pt_path", ""))).name,
        "local_train_steps": int(getattr(model, "local_train_steps", 0) or 0),
        "base_weights_version": str(getattr(model, "base_weights_version", "") or ""),
    }


