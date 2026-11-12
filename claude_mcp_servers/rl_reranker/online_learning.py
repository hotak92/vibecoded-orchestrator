# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-project online learning of the RL reranker, behind a per-project serving gate.

OWNER RULINGS (2026-11-12): the product is PER-PROJECT ONLINE adaptation; it
ships OPT-IN; no global weights ship; a project's model is SERVED only while,
on that project's own recent held-out queries, a PAIRED BOOTSTRAP says at 90%
confidence (one-sided) that its NDCG@10 is at least +2% above plain cosine
ordering AND that the MRR@10 of the literally cited node is not below
cosine's. Otherwise the project keeps its own search order, exactly as with
RL scoring off.

What one project's :class:`ProjectLearner` does with the citation events the
MCP posts to ``/rl_update`` (``RLServer._rl_update``):

1. **Ingest** (:meth:`ProjectLearner.ingest`): the event's targets are the
   unified target formula (``vco_lib.rl_training_targets``, the one copy) over
   its answer cosines + literal citations, its vectors the frozen
   retrieval-time ones. The query is appended to a time-ordered queue.
2. **Held-out window** (:class:`TimeQueue`): the shortest run of the newest
   queries holding :attr:`OnlineConfig.holdout_queries` queries and
   :attr:`OnlineConfig.min_cited_queries` citing ones (capped at
   :attr:`OnlineConfig.max_holdout_queries`) is the evaluation window. A query
   is NEVER trained on while it is in the window, nor before a purge gap has
   passed after it left (:attr:`OnlineConfig.purge_gap_ms`): consecutive
   queries of one task look alike, and without a gap the window would measure
   recall of what was trained just before it. On a busy project the queries
   waiting out the gap are bounded by :attr:`OnlineConfig.max_staged_queries`:
   past it the OLDEST waiting queries are dropped untrained — never released
   early.
3. **Training round** (every :attr:`OnlineConfig.round_every` new events —
   bounded compute, never per event): the released queries train the
   project's network with the offline objective, ``objective.batch_loss``
   (query-grouped mini-batches of :attr:`OnlineConfig.batch_queries` queries,
   pairs only within a query), at most :attr:`OnlineConfig.max_steps_per_round`
   AdamW steps per round. Inputs come from ``objective.build_inputs``, which
   applies the profile's linked-slot mask exactly as the serving path does.
4. **Gate** (:func:`gate_decision`, after every round): the model and plain
   cosine ordering are scored per query on the window (``ranking_metrics``),
   and ``ranking_metrics.paired_bootstrap_bounds`` turns the per-query values
   into one-sided lower bounds of the bar's two halves (NDCG@10 gain, cited
   MRR@10 difference); the model is served for the project only while BOTH
   bounds clear the owner's bar — the point estimates alone pass by noise on
   a window of 20-50 citing queries. Until
   then — and whenever it stops passing — the server answers with the
   client's own search order (``RLServer._cache_nodes``): a project starts
   at exactly the behaviour it has without the reranker.

A round runs in three steps so a slow round never blocks serving:
:meth:`ProjectLearner.prepare_round` (under the server's lock: copies the
network + optimizer state and the round's data), :func:`compute_round`
(off the event loop, touches no shared state) and
:meth:`ProjectLearner.apply_round` (under the lock: loads the trained state
into the live model, unless the model was replaced meanwhile, and applies the
gate). :meth:`ProjectLearner.run_round_now` chains the three — the replay
simulation (``training/transfer.py replay``) and the tests drive exactly this
path.

Persistence (per project, beside its checkpoint): ``online_<profile>.json``
(gate state, counters) + ``online_<profile>.npz`` (the queued queries and
their de-duplicated float16 vectors). Both are written atomically after every
applied round. A state written for a different checkpoint (rotated / reset
out of band) is discarded on load: its window may have been trained on.

Deterministic: rounds happen at fixed event counts, the batches are FIFO, every
random draw (linked-slot order, dropout, the gate's bootstrap resamples)
comes from a generator seeded by the round number.
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
    GATE_BOOTSTRAP_SAMPLES,
    GATE_CONFIDENCE,
    K,
    UNLOCK_MIN_REL_GAIN,
    nan_mean,
    paired_bootstrap_bounds,
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
STATE_FORMAT_VERSION = 1


@dataclass(frozen=True)
class OnlineConfig:
    """The knobs of per-project online learning. The defaults are what the
    server runs; the replay simulation passes the same object.

    The held-out window is the SHORTEST run of the newest labelled queries
    that holds at least ``holdout_queries`` queries AND at least
    ``min_cited_queries`` queries that literally cite a node (the MRR half of
    the bar needs them; on a busy project a fixed-size window can hold none),
    never longer than ``max_holdout_queries``. The gate judges nothing smaller
    than ``holdout_queries`` queries, and no MRR on fewer than
    ``min_cited_queries`` citing ones.
    ``purge_gap_ms``: a query becomes trainable only this long after it left
    the window (no exception). ``max_staged_queries``: queries waiting out the
    gap kept at most (bounds memory on a busy project): beyond it the oldest
    waiting queries are DROPPED untrained, never released before their gap.
    ``round_every``: new events between two rounds. ``batch_queries``: queries
    per AdamW step. ``max_steps_per_round``: compute bound of one round.
    ``max_pending_queries``: trainable queries kept waiting at most (the
    oldest are dropped beyond it).
    ``passes_to_serve``: consecutive passing checks before the model is
    served; ONE failing check stops serving at once. Never serves a model
    that fails the bar (it only waits longer to start). Kept beside the
    confidence bound (default 1 = the bound alone decides): the bound answers
    "is this window's gain real", the streak "has it held over successive,
    overlapping windows" — a hysteresis knob the bound does not replace.
    ``min_cited_queries`` likewise stays: a bootstrap over a handful of citing
    queries is itself unreliable, so the gate does not run below it.
    """

    holdout_queries: int = 300
    min_cited_queries: int = 20
    max_holdout_queries: int = 2000
    purge_gap_ms: int = 24 * 3_600_000
    max_staged_queries: int = 3000
    round_every: int = 32
    batch_queries: int = 8
    max_steps_per_round: int = 16
    max_pending_queries: int = 4096
    passes_to_serve: int = 1
    lr: float = 1e-4
    loss: LossConfig = field(default_factory=LossConfig)
    # The bar, judged on confidence bounds (owner ruling 2026-11-12): serve only
    # when a paired bootstrap of the window (``bootstrap_samples`` resamples)
    # puts the one-sided ``confidence`` lower bound of the relative NDCG@k gain
    # at >= ``min_ndcg_gain`` AND that of the cited-node MRR@k difference at >= 0.
    min_ndcg_gain: float = UNLOCK_MIN_REL_GAIN
    confidence: float = GATE_CONFIDENCE
    bootstrap_samples: int = GATE_BOOTSTRAP_SAMPLES
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

    @property
    def n(self) -> int:
        return int(self.node_rows.shape[0])

    @property
    def cites(self) -> bool:
        return bool(self.literal.any())


class TimeQueue:
    """A project's not-yet-trainable queries, oldest first: ``[staged | window]``.

    The window is the shortest newest run holding ``cfg.holdout_queries``
    queries and ``cfg.min_cited_queries`` citing ones, capped at
    ``cfg.max_holdout_queries`` (see :class:`OnlineConfig`); everything older
    is staged, waiting for the purge gap. Appending is amortised O(1).
    """

    def __init__(self, cfg: "OnlineConfig") -> None:
        self._cfg = cfg
        self._q: list[Optional[LabelledQuery]] = []
        self._head = 0      # first query not yet released
        self._ws = 0        # first query of the window
        self._cited = 0     # citing queries in the window

    def __len__(self) -> int:
        return len(self._q) - self._head

    def __iter__(self):
        return (q for q in self._q[self._head:] if q is not None)

    def last(self) -> LabelledQuery:
        q = self._q[-1]
        assert q is not None
        return q

    @property
    def window_size(self) -> int:
        return len(self._q) - self._ws

    @property
    def window_cited(self) -> int:
        return self._cited

    @property
    def staged(self) -> int:
        return self._ws - self._head

    def window(self) -> "list[LabelledQuery]":
        return [q for q in self._q[self._ws:] if q is not None]

    def append(self, q: LabelledQuery) -> None:
        self._q.append(q)
        self._cited += int(q.cites)
        cfg = self._cfg
        while self._ws < len(self._q):
            front = self._q[self._ws]
            assert front is not None
            w = len(self._q) - self._ws
            if w > cfg.max_holdout_queries or (
                w - 1 >= cfg.holdout_queries
                and self._cited - int(front.cites) >= cfg.min_cited_queries
            ):
                self._cited -= int(front.cites)
                self._ws += 1
            else:
                break

    def pop_released(self) -> "tuple[list[LabelledQuery], int]":
        """``(released, dropped)``: the staged queries whose purge gap has
        passed, oldest first, and how many of the oldest still-gapped ones
        were dropped because more than ``cfg.max_staged_queries`` were
        waiting. A query is never released before its gap has passed."""
        cfg = self._cfg
        out: list[LabelledQuery] = []
        dropped = 0
        while self._head < self._ws:
            front = self._q[self._head]
            win0 = self._q[self._ws] if self._ws < len(self._q) else None
            assert front is not None
            if win0 is not None and front.ts_ms > win0.ts_ms - cfg.purge_gap_ms:
                if self.staged <= cfg.max_staged_queries:
                    break
                dropped += 1          # over the staging cap: drop, never train early
            else:
                out.append(front)
            self._q[self._head] = None
            self._head += 1
        if self._head > 4096 and 2 * self._head > len(self._q):
            del self._q[: self._head]
            self._ws -= self._head
            self._head = 0
        return out, dropped

    def clear(self) -> None:
        self._q.clear()
        self._head = self._ws = self._cited = 0


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
# The gate
# ---------------------------------------------------------------------------

@dataclass
class Evaluation:
    """Model vs plain cosine on the held-out window: point estimates (means
    over its queries) and the paired-bootstrap lower bounds the gate uses.

    ``ndcg_gain_lb`` / ``mrr_diff_lb``: one-sided ``confidence`` lower bounds
    of the relative NDCG@k gain and of ``MRR@k(model) - MRR@k(cosine)`` on the
    citing queries. An undefined bound is ``-inf`` (failing), never NaN, so a
    persisted evaluation round-trips equal; ``-inf`` is also what a state
    persisted before the bound existed loads as, so it re-qualifies on new data.
    """

    at_ms: int
    n_queries: int
    n_cited: int
    ndcg: float
    cos_ndcg: float
    mrr: float
    cos_mrr: float
    search_ndcg: float
    search_mrr: float
    ndcg_gain_lb: float = float("-inf")
    mrr_diff_lb: float = float("-inf")
    confidence: float = GATE_CONFIDENCE
    n_boot: int = 0

    @property
    def ndcg_gain(self) -> float:
        return relative_gain(self.ndcg, self.cos_ndcg)

    @classmethod
    def from_dict(cls, d: dict) -> "Optional[Evaluation]":
        """The inverse of :meth:`as_dict` (``None`` metric = NaN); ``None``
        when the record is not a complete evaluation."""
        def num(k: str, missing: float = float("nan")) -> float:
            v = d.get(k)
            return missing if v is None else float(v)

        try:
            return cls(at_ms=int(d["at_ms"]), n_queries=int(d["n_queries"]),
                       n_cited=int(d["n_cited"]), ndcg=num("ndcg"), cos_ndcg=num("cos_ndcg"),
                       mrr=num("mrr"), cos_mrr=num("cos_mrr"), search_ndcg=num("search_ndcg"),
                       search_mrr=num("search_mrr"),
                       ndcg_gain_lb=num("ndcg_gain_lb", float("-inf")),
                       mrr_diff_lb=num("mrr_diff_lb", float("-inf")),
                       confidence=num("confidence", GATE_CONFIDENCE),
                       n_boot=int(d.get("n_boot") or 0))
        except (KeyError, TypeError, ValueError):
            return None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe: every non-finite number (NaN metric, ``-inf`` bound) is
        ``None`` (strict JSON for the state file and the hub row)."""
        d = asdict(self)
        d["ndcg_gain"] = self.ndcg_gain
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                for k, v in d.items()}


def _pct(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{100 * x:+.2f}%"


def _diff(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:+.3f}"


def gate_decision(ev: Optional[Evaluation], cfg: OnlineConfig) -> "tuple[bool, str]":
    """Serve the project's model? ``(serve, why)`` — the owner's bar, applied
    to the paired-bootstrap lower bounds on the held-out window (the point
    estimates are reported, never decisive). Pure."""
    if ev is None:
        return False, "no evaluation yet"
    if ev.n_queries < cfg.holdout_queries:
        return False, (f"collecting held-out queries ({ev.n_queries}/{cfg.holdout_queries}); "
                       "serving plain search order meanwhile")
    if ev.n_cited < cfg.min_cited_queries:
        return False, (f"only {ev.n_cited} held-out queries name a node in the answer "
                       f"(needs {cfg.min_cited_queries} to check the cited-node MRR)")
    gain, glb, dlb = ev.ndcg_gain, ev.ndcg_gain_lb, ev.mrr_diff_lb
    conf = f"{100 * ev.confidence:.0f}%"
    if not (glb >= cfg.min_ndcg_gain):  # NaN / -inf fail
        return False, (f"NDCG@{cfg.k} {_pct(gain)} vs plain cosine, {conf} lower bound "
                       f"{_pct(glb)}; the bound needs {100 * cfg.min_ndcg_gain:+.2f}%")
    if not (dlb >= 0.0):
        return False, (f"NDCG@{cfg.k} {_pct(gain)} (bound {_pct(glb)}) but the cited node's "
                       f"MRR@{cfg.k} {ev.mrr:.3f} vs plain cosine's {ev.cos_mrr:.3f}: the {conf} "
                       f"lower bound of the difference is {_diff(dlb)}, below 0")
    return True, (f"NDCG@{cfg.k} {_pct(gain)} over plain cosine ({conf} lower bound "
                  f"{_pct(glb)} >= {100 * cfg.min_ndcg_gain:+.2f}%), cited-node MRR@{cfg.k} "
                  f"{ev.mrr:.3f} vs {ev.cos_mrr:.3f} (lower bound of the difference "
                  f"{_diff(dlb)} >= 0)")


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
    holdout: Optional[SampleTable]
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
    evaluation: Optional[Evaluation]
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
    """Train the copied network on the round's batches, then evaluate it.

    Runs off the event loop; reads only ``job`` (its network is a private
    copy). The live model is updated by :meth:`ProjectLearner.apply_round`.
    Deterministic for a given job (``round_rng``, ``_ROUND_LOCK``).
    """
    with _ROUND_LOCK, round_rng(job.seed, job.device):
        return _compute_round(job)


def _compute_round(job: RoundJob) -> RoundResult:
    t0 = time.monotonic()
    net = job.net
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
    evaluation = (evaluate(net, job.holdout, job.cfg, job.now_ms, seed=job.seed)
                  if job.holdout is not None else None)
    return RoundResult(generation=job.generation, state_dict=state, opt_state=opt_state,
                       steps=len(job.steps) if state is not None else 0,
                       trained_queries=trained, evaluation=evaluation,
                       runtime_s=time.monotonic() - t0)


def evaluate(net: Any, tbl: SampleTable, cfg: OnlineConfig, now_ms: int, *,
             seed: int = 0) -> Evaluation:
    """Model vs cosine (and vs the client's search order) on ``tbl``'s
    queries, with the paired-bootstrap bounds of the bar (``seed``: the
    round's, so a check is reproducible)."""
    sidx = np.arange(len(tbl.np_target), dtype=np.int64)
    sc = score_samples(tbl, net, sidx)
    cs = cosine_scores(tbl, sidx)
    gains, rel = tbl.np_target, tbl.np_literal
    m = per_query_metrics(sc, gains, rel, tbl.first, tbl.count, cfg.k)
    c = per_query_metrics(cs, gains, rel, tbl.first, tbl.count, cfg.k)
    s = per_query_metrics(-tbl.np_search_pos.astype(np.float64), gains, rel,
                          tbl.first, tbl.count, cfg.k)
    b = paired_bootstrap_bounds(m, c, n_boot=cfg.bootstrap_samples,
                                confidence=cfg.confidence, seed=seed)
    return Evaluation(
        at_ms=int(now_ms), n_queries=int(len(tbl.first)),
        n_cited=int(np.sum(~np.isnan(m.mrr))),
        ndcg=nan_mean(m.ndcg), cos_ndcg=nan_mean(c.ndcg),
        mrr=nan_mean(m.mrr), cos_mrr=nan_mean(c.mrr),
        search_ndcg=nan_mean(s.ndcg), search_mrr=nan_mean(s.mrr),
        ndcg_gain_lb=b.ndcg_gain_lb, mrr_diff_lb=b.mrr_diff_lb,
        confidence=b.confidence, n_boot=b.n_boot,
    )


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
        self._queue = TimeQueue(cfg)                    # not yet trainable, oldest first
        self._pending: Deque[LabelledQuery] = deque()   # trainable, oldest first
        self._generation = 0          # bumped when the model is replaced
        self._round_in_flight = False
        self._events_since_round = 0
        self._passes = 0              # consecutive passing checks
        self.serving = False
        self.reason = "no evaluation yet"
        self.last_eval: Optional[Evaluation] = None
        self.counters: dict[str, int] = {
            "events_seen": 0, "events_skipped": 0, "queries_trained": 0,
            "train_steps": 0, "rounds": 0, "dropped_untrained": 0,
            "serving_turned_on": 0, "serving_turned_off": 0, "model_replaced": 0,
        }
        self.training_enabled = True
        self.last_skip_reason = ""
        self.model_fingerprint: dict[str, Any] = {}

    # -- events ---------------------------------------------------------

    @property
    def holdout_size(self) -> int:
        return self._queue.window_size

    def ingest(self, task: dict, *, ts_ms: int, train: bool = True) -> bool:
        """Add one citation event. ``train=False`` (online training switched
        off for the project): the event still enters the held-out window — the
        gate keeps judging the frozen model — but is never trained on.
        Returns whether the event was kept."""
        self.training_enabled = bool(train)
        q, why = labelled_query(task, self._store, profile=self.profile, ts_ms=ts_ms)
        if q is None:
            self.counters["events_skipped"] += 1
            self.last_skip_reason = why
            return False
        self.counters["events_seen"] += 1
        self._queue.append(q)
        self._events_since_round += 1
        self._release()
        return True

    def _release(self) -> None:
        """Move queries that left the window AND the purge gap to training."""
        cfg = self.cfg
        released, over_cap = self._queue.pop_released()
        self.counters["dropped_untrained"] += over_cap
        for front in released:
            if self.training_enabled:
                self._pending.append(front)
            else:
                self.counters["dropped_untrained"] += 1
        while len(self._pending) > cfg.max_pending_queries:
            self._pending.popleft()
            self.counters["dropped_untrained"] += 1

    def round_due(self) -> bool:
        return (not self._round_in_flight) and self._events_since_round >= self.cfg.round_every

    @property
    def round_in_flight(self) -> bool:
        return self._round_in_flight

    # -- rounds ---------------------------------------------------------

    def prepare_round(self, model: Any, *, now_ms: int) -> Optional[RoundJob]:
        """Snapshot what one round needs (call under the server's lock)."""
        if self._round_in_flight:
            return None
        cfg = self.cfg
        n_steps = min(len(self._pending) // cfg.batch_queries, cfg.max_steps_per_round)
        train_q = [self._pending.popleft() for _ in range(n_steps * cfg.batch_queries)]
        window = self._queue.window()
        if not train_q and not window:
            return None
        dev = torch.device(model.device)
        train = build_table(train_q, self._store, profile=self.profile, device=dev) if train_q else None
        steps = [np.arange(i * cfg.batch_queries, (i + 1) * cfg.batch_queries)
                 for i in range(n_steps)]
        holdout = build_table(window, self._store, profile=self.profile, device=dev) if window else None
        self._compact_store()
        self._round_in_flight = True
        self._events_since_round = 0
        return RoundJob(
            generation=self._generation, net=copy.deepcopy(model._net),
            opt_state=copy.deepcopy(model._opt.state_dict()), profile=self.profile,
            device=dev, train=train, steps=steps, holdout=holdout,
            seed=cfg.seed + self.counters["rounds"], cfg=cfg, now_ms=now_ms,
        )

    def apply_round(self, model: Any, result: RoundResult) -> bool:
        """Load the round into the live model and apply the gate (under the
        lock). Returns False when the model was replaced while the round ran
        (the round is discarded: it trained a network no longer served)."""
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
        if result.evaluation is not None:
            self.last_eval = result.evaluation
            passed, why = gate_decision(self.last_eval, self.cfg)
            self._passes = self._passes + 1 if passed else 0
        else:
            passed, why = gate_decision(self.last_eval, self.cfg)
        serve, why = self._serve_decision(passed, why)
        if serve and not self.serving:
            self.counters["serving_turned_on"] += 1
        if self.serving and not serve:
            self.counters["serving_turned_off"] += 1
        self.serving, self.reason = serve, why
        self.model_fingerprint = model_fingerprint(model)
        return True

    def _serve_decision(self, passed: bool, why: str) -> "tuple[bool, str]":
        """Serve only after ``passes_to_serve`` consecutive passing checks."""
        need = max(1, int(self.cfg.passes_to_serve))
        if passed and self._passes < need:
            return False, (f"{why} — passing check {self._passes} of {need} in a row "
                           "before the model is served")
        return passed, why

    def _compact_store(self) -> None:
        """Drop vectors no queued query uses any more (trained / dropped
        queries leave theirs behind). Bounded memory on a long-running server;
        runs when at least half the store is garbage."""
        live = list(self._queue) + list(self._pending)
        approx_live = sum(1 + 2 * q.n for q in live)  # cheap upper-ish bound
        if len(self._store) > max(4096, 2 * approx_live):
            self._store = self._store.compacted(live)

    def abandon_round(self) -> None:
        """The round failed: the trained queries are lost, the gate stays."""
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
        window may have been trained on by the new model, so it is cleared
        and the gate starts over; queued training data is dropped too."""
        self._generation += 1
        self._round_in_flight = False
        dropped = len(self._queue) + len(self._pending)
        self._queue.clear()
        self._pending.clear()
        self._store = VectorStore(self.emb_dim)
        self.counters["model_replaced"] += 1
        self.counters["dropped_untrained"] += dropped
        if self.serving:
            self.counters["serving_turned_off"] += 1
        self.serving = False
        self._passes = 0
        self.last_eval = None
        self.reason = f"model replaced ({why}); re-checking on new queries"
        self.model_fingerprint = model_fingerprint(model)

    # -- status / persistence -------------------------------------------

    def status(self) -> dict[str, Any]:
        cfg = self.cfg
        return {
            "profile": self.profile,
            "serving": self.serving,
            "reason": self.reason,
            "training": "on" if self.training_enabled else "off (online training disabled for this project)",
            "holdout_queries": self.holdout_size,
            "holdout_cited_queries": self._queue.window_cited,
            "holdout_target": cfg.holdout_queries,
            "waiting_queries": self._queue.staged,
            "pending_train_queries": len(self._pending),
            "last_eval": self.last_eval.as_dict() if self.last_eval else None,
            "counters": dict(self.counters),
            "last_skip_reason": self.last_skip_reason or None,
            "bar": {"min_ndcg_gain": cfg.min_ndcg_gain, "cited_mrr_not_below_cosine": True,
                    "judged_on": "paired-bootstrap lower bounds", "confidence": cfg.confidence,
                    "bootstrap_samples": cfg.bootstrap_samples, "k": cfg.k, "min_cited_queries": cfg.min_cited_queries,
                    "max_holdout_queries": cfg.max_holdout_queries,
                    "purge_gap_hours": cfg.purge_gap_ms / 3_600_000},
        }

    def save(self, directory: Path) -> None:
        """Write ``online_<profile>.json`` + ``.npz`` atomically."""
        directory.mkdir(parents=True, exist_ok=True)
        live = list(self._queue) + list(self._pending)
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
            q_pending=np.array([0] * len(self._queue) + [1] * len(self._pending), dtype=np.int8),
            node_row=remap[cat(lambda q: q.node_rows, np.int64)] if live else np.zeros(0, np.int64),
            linked_row=np.where(lr >= 0, remap[np.clip(lr, 0, None)], -1) if live
            else np.zeros((0, MAX_LINKED), np.int64),
            linked_type=cat(lambda q: q.linked_types, np.int64, MAX_LINKED),
            node_type=cat(lambda q: q.node_types, np.int64),
            target=cat(lambda q: q.target, np.float32),
            literal=cat(lambda q: q.literal, np.int8),
            search_pos=cat(lambda q: q.search_pos, np.int64),
        )
        atomic_write_bytes(directory / f"online_{self.profile}.npz", buf.getvalue())
        atomic_write_json(directory / f"online_{self.profile}.json", {
            "format_version": STATE_FORMAT_VERSION,
            "profile": self.profile,
            "emb_dim": self.emb_dim,
            "serving": self.serving,
            "reason": self.reason,
            "last_eval": self.last_eval.as_dict() if self.last_eval else None,
            "counters": self.counters,
            "events_since_round": self._events_since_round,
            "consecutive_passes": self._passes,
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
        window may have been trained on — and the gate starts over.
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
        lr_.reason = str(meta.get("reason") or "")
        lr_.training_enabled = bool(meta.get("training_enabled", True))
        lr_._events_since_round = int(meta.get("events_since_round") or 0)
        lr_._passes = int(meta.get("consecutive_passes") or 0)
        ev = meta.get("last_eval")
        if isinstance(ev, dict):
            lr_.last_eval = Evaluation.from_dict(ev)
        if arr_p.is_file():
            try:
                lr_._load_arrays(arr_p)
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("online learner data %s unreadable (%s); window starts over", arr_p, exc)
                lr_._queue.clear()
                lr_._pending.clear()
                lr_._store = VectorStore(emb_dim)
                lr_.serving = False
                lr_.reason = "held-out window lost (unreadable state); re-checking on new queries"
        # The serving decision is re-derived from the restored evaluation, so
        # a changed bar applies at once.
        if lr_.last_eval is not None:
            passed, why = gate_decision(lr_.last_eval, cfg)
            if not passed:
                lr_._passes = 0
            lr_.serving, lr_.reason = lr_._serve_decision(passed, why)
        return lr_

    def _load_arrays(self, path: Path) -> None:
        with np.load(path, allow_pickle=False) as z:
            d = {k: z[k] for k in z.files}
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
                weight=float(d["q_weight"][i]) if "q_weight" in d else 1.0,
            )
            off += n
            (self._pending if int(d["q_pending"][i]) else self._queue).append(q)


def model_fingerprint(model: Any) -> dict[str, Any]:
    """What identifies the checkpoint a learner state belongs to."""
    return {
        "file": Path(str(getattr(model, "_pt_path", ""))).name,
        "local_train_steps": int(getattr(model, "local_train_steps", 0) or 0),
        "base_weights_version": str(getattr(model, "base_weights_version", "") or ""),
    }
