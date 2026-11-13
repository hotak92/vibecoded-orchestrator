# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Offline batch trainer for the RetrievalRL RLModel.

Reads retrieval+citation event pairs from launcher.db (via the module-scoped
hub route — DB-only since v0.2.11; never a JSONL file) and trains the neural
relevance model using the stored embedding vectors (frozen at scoring time) and
cosine similarity signals (stored in citation events).

Why standalone?
---------------
Embedding vectors are stored in the log at retrieval time, so:
  - No Weaviate or Ollama required during training (fully offline).
  - Reproducible: uses the exact vectors that were seen during the live session,
    even if node content has since changed in Weaviate.

Target formula
--------------
Cosine-sims path: clamp(cos + 0.1 * z_mapped, 0, 1)
  where z_mapped = 0.5 + z_score / 2  (same as RetrievalRL._compute_targets)

Binary citation path (no cosine_sims): 1.0 if cited, 0.0 if not cited.

Feature resolution (no arbitrary defaults)
-------------------------------------------
cos_qn: stored in log if present; otherwise recomputed from query_emb × node.emb.
cos_ql: stored in log if present (0.0 for no-links — no link context available);
        absent = use cos_qn as proxy (better than a generic 0.5).
cos_nl: stored in log if present; 0.5 (neutral) when the log has none.
        (v0.2.106: the ``link_similarity.json`` fallback is gone — nothing ever
        wrote that file, and the v2 network does not read these three sample
        scalars anyway: ``_epoch`` computes the per-slot cosines from the raw
        query / node / linked vectors, as serving does, since G2b 2026-07-22.
        The scalars stay in the sample for the loader-parity checks.)

Loss: MSE(pred, target) + 0.5 * BCE(pred, target) + λ * DeltaMSE(pred, target)
  MSE: main pointwise regression toward cosine-derived target.
  BCE (×0.5): probability-calibration regularizer (stronger than before).
  DeltaMSE: pairwise loss — MSE( (target_i − target_j) − (pred_i − pred_j) )
            over all upper-triangle pairs in the batch.
  λ=0.5 — MSE dominates; DeltaMSE aligns relative score differences with
           cosine differences (replaces binary RankNet).

Training log (accumulated across runs):
  {run_start, run_end, epochs, train_loss, val_loss, early_stopped, n_train, ...}

Usage (v0.2.11+, DB-only — events come from launcher.db via the hub)::

    python -m rl_reranker.offline_trainer \\
        --project-id <uuid> \\
        --model-state state/rl_model_1024.pt \\
        --epochs 3

    # Per-embedding-source head (DB cohorting is by project_id column)
    python -m rl_reranker.offline_trainer \\
        --project-id <uuid> \\
        --filter-embedding-source qwen3 \\
        --model-state state/rl_model_qwen3_1024.pt \\
        --epochs 5
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    _HAS_TORCH = True
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    nn = None     # type: ignore[assignment]
    _HAS_TORCH = False

try:
    from .rl_model import RLModel
    from .rl_model import (
        NODE_TYPES as _NODE_TYPES,
        NULL_TYPE_IDX as _NULL_TYPE_IDX,
        UNKNOWN_TYPE_IDX as _UNKNOWN_TYPE_IDX,
        MAX_LINKED,
    )
    # Fast name→index map for builtins (no RLModel instance needed)
    _BUILTIN_TYPE_MAP: "dict[str, int]" = {t: i for i, t in enumerate(_NODE_TYPES)}
except Exception:
    RLModel = None  # type: ignore[assignment,misc]
    _BUILTIN_TYPE_MAP = {}
    _NULL_TYPE_IDX = 6
    _UNKNOWN_TYPE_IDX = 8
    MAX_LINKED = 5


def _resolve_soft_label_weight() -> float:
    """Loss weight for soft-label citation events (v0.2.87 logger:
    ``fire_reason=soft_terminal`` / ``soft_label=true`` — a below-terminal-floor
    answer window). These are weaker evidence than a hard terminal citation, so
    their samples are DOWN-WEIGHTED in the loss rather than treated as hard
    labels (per the free-repo logging contract). Env-tunable via
    ``RL_SOFT_LABEL_WEIGHT`` (default 0.5); clamped to [0, 1]; a malformed
    override falls back to the default — a bad tunable must never flip a
    soft label into a hard one (1.0) or silently discard it (<0)."""
    try:
        w = float(os.environ.get("RL_SOFT_LABEL_WEIGHT", "0.5"))
    except (TypeError, ValueError):
        return 0.5
    if not (0.0 <= w <= 1.0) or w != w:  # NaN-guard: NaN != NaN
        return 0.5
    return w


SOFT_LABEL_WEIGHT = _resolve_soft_label_weight()


def _resolve_soft_min_window_tokens() -> int:
    """Minimum answer-window size (tokens) for a soft-label citation to be
    TRAINABLE. Below this the pair is DISCARDED from training entirely
    (maintainer ruling 2026-07-22): at ~200 tokens the answer is a single
    chunk, so "max over answer-chunks" degenerates to one comparison and
    query-echo dominates — the label is noise, and training on noise is worse
    than skipping the pair. The writer keeps EMITTING sub-floor events
    (flagged, with answer_chunk_embs), so nothing is lost — they stay
    re-labelable offline; this floor is reader-side training policy only.
    Default 1000 (>= ~2 full chunker chunks). Env ``RL_SOFT_MIN_WINDOW_TOKENS``;
    malformed/negative falls back to the default. Applies ONLY to soft_label
    events — hard citations fired at the terminal floor (2000) or the 25k gate
    and are always multi-chunk."""
    try:
        v = int(os.environ.get("RL_SOFT_MIN_WINDOW_TOKENS", "1000"))
    except (TypeError, ValueError):
        return 1000
    return v if v >= 0 else 1000


SOFT_MIN_WINDOW_TOKENS = _resolve_soft_min_window_tokens()


# ---------------------------------------------------------------------------
# Event loading — DB-only (launcher.db via the module-scoped hub route)
# ---------------------------------------------------------------------------
#
# v0.2.11 cutover: JSONL is GONE from the training path. launcher.db (written
# by the hub on every retrieval/citation, V52) is the sole source of truth.
# The trainer reads its own events over HTTP via the module-scoped route
# GET /api/v1/modules/{module_id}/projects/{project_id}/rl/events — scoped to
# granted projects by the module token; it never opens launcher.db directly
# (single-writer principle) and never reads a JSONL file. Old JSONL corpora
# stay on disk for archival; if ever needed they are PORTED to the DB
# (scripts/migrate_rl_jsonl_to_db.py), not read by training code.

def load_training_events(
    hub_client: "Any",
    project_id: str,
    *,
    filter_embedding_source: str = "",
    since_ms: "int | None" = None,
    until_ms: "int | None" = None,
) -> list[dict[str, Any]]:
    """Load trainable RL events for one project from launcher.db via the hub.

    Thin wrapper over :func:`hub_event_loader.load_events_from_hub` — the
    trainer's single loading entrypoint, so callers don't import the hub module
    directly. Returns event dicts in the shape ``pair_events`` /
    ``extract_samples`` consume (the hub's ``payload_json`` is the verbatim v3
    event). Per-source filter applies to retrieval events only; citations pass
    through to join by task_id. schema_version<2 events are refused (v3 is the
    DB-era norm; a v1 event means an unmigrated corpus leaked in).

    Raises ValueError if ``hub_client`` is None or ``project_id`` is empty —
    both mandatory (the route is per-project and module-token-scoped).
    """
    if hub_client is None:
        raise ValueError(
            "load_training_events: hub_client is required (the container's "
            "_HubClient instance). JSONL training was retired in v0.2.11."
        )
    if not project_id:
        raise ValueError(
            "load_training_events: project_id is required (the hub events "
            "route is per-project and module-token-scoped). A '_base'/no-header "
            "request has no project to train; reject upstream."
        )
    from .hub_event_loader import load_events_from_hub
    return load_events_from_hub(
        hub_client,
        project_id,
        filter_embedding_source=filter_embedding_source,
        since_ms=since_ms,
        until_ms=until_ms,
    )


def pair_events(
    events: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    Match retrieval events with their citation events by task_id.

    Returns list of ``(retrieval_event, citation_event)`` tuples.
    """
    retrievals: dict[str, dict[str, Any]] = {}
    citations: dict[str, dict[str, Any]] = {}

    for ev in events:
        tid = ev.get("task_id", "")
        if not tid:
            continue
        if ev.get("event") == "retrieval":
            retrievals[tid] = ev
        elif ev.get("event") == "citation":
            citations[tid] = ev

    return [
        (ret_ev, citations[tid])
        for tid, ret_ev in retrievals.items()
        if tid in citations
    ]


# ---------------------------------------------------------------------------
# Feature resolution and target computation
# ---------------------------------------------------------------------------

from .math_utils import cosine_similarity_np as _cosine_np  # noqa: E402


def compute_targets_from_cosine_sims(
    cosine_sims: "dict[str, float]",
    literal_cited: "dict[str, bool] | None" = None,
    cross_encoder_cited: "dict[str, bool] | None" = None,
    beta: float = 0.1,
) -> "dict[str, float]":
    """Compute BCE training targets via the unified-target formula.

    v0.2.9 C8: this function is now a thin wrapper around
    ``vco_lib.rl_training_targets.compute_unified_targets`` (the one copy;
    the image COPIES it). The wrapper exists for
    backward-compat with existing callers that don't yet pass the v3
    flag dicts — missing dicts default to all-False, which makes the
    formula degenerate to the legacy cosine-only path:

        target_i = clamp(cos_i + 0.1 * z_mapped_i, 0, 1)

    When v3 events supply ``literal_cited`` and/or
    ``cross_encoder_cited``, the unified helper applies the same
    multiplicative+additive boost that the online container's
    ``_update`` applies — making online and offline training targets
    byte-identical on the same event by construction.

    The ``beta`` kwarg is kept for back-compat with any caller that
    overrode it; the unified helper takes the same value under the
    ``beta_z`` kwarg.
    """
    from vco_lib.rl_training_targets import compute_unified_targets
    targets, _cited = compute_unified_targets(
        cosine_sims,
        literal_cited=literal_cited,
        cross_encoder_cited=cross_encoder_cited,
        beta_z=beta,
    )
    return targets


def resolve_features(
    node: "dict[str, Any]",
    q_emb: "np.ndarray | None",
) -> "tuple[float, float, float]":
    """
    Resolve (cos_qn, cos_ql, cos_nl) without arbitrary 0.5 defaults.

    cos_qn: stored → use it; else recompute from q_emb × node.emb.
    cos_ql: stored → use it (0.0 for no-links is correct);
            absent → use cos_qn as proxy (NOT 0.5 as a generic stand-in).
    cos_nl: stored → use it; 0.5 (neutral) when the log has none. The
            ``link_similarity.json`` lookup that sat between the two was
            removed in v0.2.106 as superseded: no writer of that file ever
            existed, and the network's linked-slot cosines are computed from
            the stored linked vectors (``_epoch``), not from this scalar.
    """
    n_emb: "np.ndarray | None" = node.get("_emb_np")  # pre-converted numpy array

    # cos_qn
    if "cos_qn" in node:
        cos_qn = float(node["cos_qn"])
    elif q_emb is not None and n_emb is not None:
        cos_qn = _cosine_np(q_emb, n_emb)
    else:
        cos_qn = 0.0

    # cos_ql: stored value (even 0.5 for no-links is real); absent → cos_qn proxy
    if "cos_ql" in node:
        cos_ql = float(node["cos_ql"])
    else:
        cos_ql = cos_qn

    # cos_nl: stored → use; 0.5 (neutral) when the log has none
    if "cos_nl" in node:
        cos_nl = float(node["cos_nl"])
    else:
        cos_nl = 0.5

    return cos_qn, cos_ql, cos_nl


# ---------------------------------------------------------------------------
# Task-level sample extraction
# ---------------------------------------------------------------------------

def citation_targets(
    cit_ev: "dict[str, Any]",
) -> "tuple[dict[str, float], float] | None":
    """The training targets and loss weight one citation event yields.

    The ONE home of the per-citation training policy; :func:`extract_samples`
    and the offline export (``rl_reranker.export``) both call it, so the
    shards a volunteer shares carry exactly the labels this trainer would use.

    Returns ``({title: target}, sample_weight)``, or ``None`` when the pair
    must be DISCARDED from training.

    Targets: the unified-target formula over ``cosine_sims`` (+ the v3
    ``literal_cited`` / ``cross_encoder_cited`` flag dicts; v2 events lack them
    and the formula degenerates to cosine-only); events without
    ``cosine_sims`` fall back to binary ``citations`` labels.

    Soft-label policy (2026-07-22): a citation fired via the soft-terminal
    path (below-terminal-floor answer window) is weaker evidence than a hard
    terminal citation.

    * window < SOFT_MIN_WINDOW_TOKENS (default 1000 ≈ <2 chunker chunks):
      DISCARD (maintainer ruling — a ~1-chunk window degenerates "max over
      answer-chunks" to one query-echo-dominated comparison; the label is
      noise). A soft event MISSING ``window_tokens`` is discarded too
      (conservative default: the precondition cannot be confirmed). The stored
      event is untouched — it stays re-labelable later.
    * window ≥ floor: keep, weighted by SOFT_LABEL_WEIGHT.

    Hard citations always weigh 1.0 (they fired at the 2000-token terminal
    floor or the 25k gate — always multi-chunk).
    """
    if bool(cit_ev.get("soft_label")):
        window_tokens = int(cit_ev.get("window_tokens") or 0)
        if window_tokens < SOFT_MIN_WINDOW_TOKENS:
            return None
        sample_weight = SOFT_LABEL_WEIGHT
    else:
        sample_weight = 1.0

    cosine_sims: dict[str, float] = {
        k: float(v) for k, v in (cit_ev.get("cosine_sims") or {}).items()
    }
    if cosine_sims:
        literal_cited = {
            k: bool(v) for k, v in (cit_ev.get("literal_cited") or {}).items()
        }
        cross_encoder_cited_raw = cit_ev.get("cross_encoder_cited") or {}
        cross_encoder_cited = {
            k: bool(v) for k, v in cross_encoder_cited_raw.items()
        } if cross_encoder_cited_raw else None
        targets = compute_targets_from_cosine_sims(
            cosine_sims,
            literal_cited=literal_cited or None,
            cross_encoder_cited=cross_encoder_cited,
        )
    else:
        # Fallback: binary labels (events that predate the schema_version=2
        # cosine_sims field, or events where citation detection failed to
        # compute cosines).
        targets = {
            t: (1.0 if cited else 0.0)
            for t, cited in (cit_ev.get("citations") or {}).items()
            if cited is not None
        }
    return targets, sample_weight


def extract_samples(
    pairs: "list[tuple[dict, dict]]",
) -> "list[dict[str, Any]]":
    """
    Convert paired events into flat training samples.

    Each sample: {q_emb_np, n_emb_np, cos_qn, cos_ql, cos_nl, target, task_id}.
    Samples with no stored node embedding are skipped (can't build features).
    """
    out: list[dict[str, Any]] = []

    for ret_ev, cit_ev in pairs:
        q_emb_raw = ret_ev.get("query_emb")
        if not q_emb_raw:
            continue
        q_emb = np.array(q_emb_raw, dtype=np.float32)

        task_id = ret_ev.get("task_id", "")
        decided = citation_targets(cit_ev)
        if decided is None:
            logger.debug(
                "extract_samples: discarding soft-label pair task_id=%s "
                "(window_tokens below floor %d)",
                task_id[:8], SOFT_MIN_WINDOW_TOKENS,
            )
            continue
        targets, sample_weight = decided

        # Pre-convert node embeddings to numpy once
        node_map: dict[str, dict] = {}
        for n in ret_ev.get("nodes", []):
            title = n.get("title", "")
            emb_raw = n.get("emb")
            if not emb_raw:
                continue
            n2 = dict(n)
            n2["_emb_np"] = np.array(emb_raw, dtype=np.float32)
            node_map[title] = n2

        for title, target in targets.items():
            node = node_map.get(title)
            if node is None:
                continue
            cos_qn, cos_ql, cos_nl = resolve_features(node, q_emb)
            # G2 (2026-07-22): resolve per-link type indices from the field the
            # LOGGER actually writes — ``linked_type_names`` (serialize_node_record
            # in claude_mcp_servers/rl_client/rl_logger.py). The prior code read
            # ``link_types``, a name the writer NEVER emits, so offline link-type
            # features were silently always empty (every stored row carries
            # ``linked_type_names`` and ZERO carry ``link_types`` — verified against
            # the live launcher.db: 357/357 sampled linked nodes). ``link_types`` is
            # kept ONLY as a forward-compat fallback; it must never be the primary
            # key because renaming the LOG field would orphan the 34k+ row history
            # (the constraint: fix on the reader side, never the writer).
            # Falls back to UNKNOWN_TYPE_IDX for any link whose type isn't a builtin.
            raw_link_types: list[str] = (
                node.get("linked_type_names") or node.get("link_types") or []
            )
            link_type_idxs: list[int] = [
                _BUILTIN_TYPE_MAP.get(t, _UNKNOWN_TYPE_IDX) for t in raw_link_types
            ]
            # G2b (2026-07-22): stack the STORED linked-slot embeddings so the v2
            # model's 5 linked entity tokens train on the real graph context the
            # ONLINE path already uses — NOT zeros. v3 events carry ``linked_embs``
            # (packed by _rl_pack_linked_embs_for_node: extra chunks of THIS node
            # followed by actual linked nodes, index-aligned with
            # ``linked_type_names``). The prior ``_epoch`` zero-padded every linked
            # slot with a stale "not in log" comment, amputating 5 of the model's 7
            # entity tokens offline. Cap at MAX_LINKED, coerce float32; a node with
            # no links yields [].
            raw_linked_embs = node.get("linked_embs") or []
            linked_embs: list["np.ndarray"] = []
            for e in raw_linked_embs[:MAX_LINKED]:
                if e:
                    linked_embs.append(np.array(e, dtype=np.float32))
            out.append({
                "task_id": task_id,
                "q_emb": q_emb,
                "n_emb": node["_emb_np"],
                "cos_qn": cos_qn,
                "cos_ql": cos_ql,
                "cos_nl": cos_nl,
                "target": float(target),
                "link_type_idxs": link_type_idxs,  # [] when not in log (old events)
                "linked_embs": linked_embs,  # G2b: real linked-slot vectors ([] = none)
                "sample_weight": sample_weight,  # soft_label → SOFT_LABEL_WEIGHT, else 1.0
            })

    return out


# ---------------------------------------------------------------------------
# RankNet pairwise loss (within task groups)
# ---------------------------------------------------------------------------

def delta_mse_loss(
    pred: "torch.Tensor",
    target: "torch.Tensor",
    weight: "torch.Tensor | None" = None,
) -> "torch.Tensor":
    """
    Continuous pairwise loss over all upper-triangle pairs in a mini-batch.

    For every pair (i, j):
        xdiff = target_i − target_j   (cosine-derived target difference)
        ydiff = pred_i   − pred_j     (predicted score difference)
        loss  = (xdiff − ydiff)²

    This pushes the model's score differences to match the cosine target
    differences exactly, replacing binary RankNet with a fully continuous,
    symmetric signal.  Uses upper triangle only to avoid double-counting
    (the squared loss is symmetric in sign).

    ``weight`` (optional, shape (B,)): per-SAMPLE confidence weights
    (soft-label down-weighting). A pair's weight is ``w_i * w_j`` and the
    result is the weighted mean ``Σ w_pair·gap² / Σ w_pair`` — so a pair of
    two hard samples (1·1) counts fully, hard×soft counts at w, soft×soft at
    w². ``weight=None`` (or all-ones) is EXACTLY the unweighted mean.

    Returns 0 when the batch has fewer than 2 items (or all pair weights are 0).
    """
    p = pred.squeeze(-1)    # (B,)
    t = target.squeeze(-1)  # (B,)
    if p.shape[0] < 2:
        return p.sum() * 0.0

    x_diff = t.unsqueeze(1) - t.unsqueeze(0)   # (B, B) target diffs
    y_diff = p.unsqueeze(1) - p.unsqueeze(0)   # (B, B) pred diffs
    gap = x_diff - y_diff                       # (B, B)
    mask = torch.triu(torch.ones_like(gap, dtype=torch.bool), diagonal=1)
    if weight is None:
        return (gap[mask] ** 2).mean()

    w = weight.squeeze(-1).to(gap.dtype)                      # (B,)
    w_pair = (w.unsqueeze(1) * w.unsqueeze(0))[mask]          # (n_pairs,)
    denom = w_pair.sum()
    if float(denom) <= 0.0:
        return p.sum() * 0.0
    return (w_pair * gap[mask] ** 2).sum() / denom


# ---------------------------------------------------------------------------
# Train / eval epoch using _RLNet directly
# ---------------------------------------------------------------------------

def _epoch(
    net: "Any",    # _RLNet
    samples: "list[dict]",
    opt: "Any | None",  # optimizer
    batch_size: int,
    lambda_rank: float,
    training: bool,
    emb_dim: int,
    device: "torch.device | str | None" = None,
) -> "tuple[float, float, float, float]":
    """
    One training/validation epoch.

    If ``device`` is None, the net's current device is inferred from its
    first parameter. Explicit device override is supported for the (rare)
    case where the caller wants to colocate tensors with a different model.

    Returns (total_loss, mse_loss, delta_loss, mae) averaged over batches.
    mae = mean |pred - target|, interpretable as average error in [0, 1] utility estimate.
    """
    from .rl_model import QUERY_TYPE_IDX, NULL_TYPE_IDX, MAX_LINKED

    # Resolve target device. If caller didn't specify, infer from the net's
    # first parameter. This keeps the existing call sites working (they
    # don't pass device) while letting CUDA-resident nets get tensors on
    # the same device.
    if device is None:
        try:
            device = next(net.parameters()).device
        except StopIteration:
            device = torch.device("cpu")
    elif isinstance(device, str):
        device = torch.device(device)
    _use_gpu = device.type != "cpu"

    net.train(training)
    if training:
        random.shuffle(samples)

    total_mse = 0.0
    total_bce = 0.0
    total_delta = 0.0
    total_mae = 0.0
    n_batches = 0

    for i in range(0, len(samples), batch_size):
        batch = samples[i : i + batch_size]
        if not batch:
            continue
        B = len(batch)

        # G2b (2026-07-22): build the linked entity slots from the STORED
        # ``linked_embs`` (packed by the logger's _rl_pack_linked_embs_for_node),
        # NOT zeros. This restores the 5 linked entity tokens the v2 net consumes
        # (slots 2..6) so offline training sees the same graph context the online
        # /rl_update path does. Slots beyond a sample's stored link count stay zero
        # (genuine "no linked node here" padding). ``actual_emb_dim`` is taken from
        # the query vector so a batch recorded with a different model still builds.
        q_stack = np.stack([s["q_emb"] for s in batch])    # (B, emb_dim)
        n_stack = np.stack([s["n_emb"] for s in batch])    # (B, emb_dim)
        q_t = torch.from_numpy(q_stack)
        n_t = torch.from_numpy(n_stack)

        # entities: (B, 2+MAX_LINKED, actual_emb_dim)
        actual_emb_dim = q_t.shape[-1]
        # Per-sample linked embeddings, right-padded with zeros to MAX_LINKED.
        # A stored linked vector whose dim mismatches the batch (mixed-model
        # corruption) is skipped to zero rather than crashing the stack.
        linked_stack = np.zeros((B, MAX_LINKED, actual_emb_dim), dtype=np.float32)
        for bi, s in enumerate(batch):
            for li, lv in enumerate((s.get("linked_embs") or [])[:MAX_LINKED]):
                if lv is not None and getattr(lv, "shape", (0,))[-1] == actual_emb_dim:
                    linked_stack[bi, li, :] = lv
        linked_t = torch.from_numpy(linked_stack)          # (B, MAX_LINKED, emb_dim)
        entities = torch.cat([q_t.unsqueeze(1), n_t.unsqueeze(1), linked_t], dim=1)

        # type indices: (B, 2+MAX_LINKED)
        q_tidx = torch.full((B, 1), QUERY_TYPE_IDX, dtype=torch.long)
        n_tidx = torch.zeros(B, 1, dtype=torch.long)  # concept=0 default
        # Per-link type indices when stored in log; NULL for padding slots beyond
        # the stored list (no linked node at that position). Aligned index-for-index
        # with the linked embeddings stacked above (same packing order).
        l_tidx_rows = []
        for s in batch:
            link_types = s.get("link_type_idxs") or []
            row = [link_types[i] if i < len(link_types) else NULL_TYPE_IDX
                   for i in range(MAX_LINKED)]
            l_tidx_rows.append(row)
        l_tidx = torch.tensor(l_tidx_rows, dtype=torch.long)
        type_idxs = torch.cat([q_tidx, n_tidx, l_tidx], dim=1)

        # Per-slot cosine features: computed from the entity embeddings above.
        # Slot layout: [query, target_node, link0..link{MAX_LINKED-1}]
        #   Query slot:       (1.0, 1.0)              identity anchor
        #   Target node slot: (cos(q,n), 1.0)
        #   Linked slot:      (cos(q,link), cos(n,link))   G2b: from stored linked_embs
        #   NULL padding:     (0.0, 0.0)              (zero linked vector present)
        # This matches RLModel._build_inputs' per-slot cosine contract exactly, so
        # the offline features are byte-consistent with the online scoring path.
        slot_scalars_list = []
        for bi in range(B):
            q_np = q_stack[bi]
            n_np = n_stack[bi]
            cos_qn = float(_cosine_np(q_np, n_np))
            slots = [(1.0, 1.0), (cos_qn, 1.0)]  # query (identity anchor), target_node
            stored_links = (batch[bi].get("linked_embs") or [])
            for li in range(MAX_LINKED):
                if li < len(stored_links) and stored_links[li] is not None \
                        and getattr(stored_links[li], "shape", (0,))[-1] == actual_emb_dim:
                    lv = stored_links[li]
                    slots.append((
                        float(_cosine_np(q_np, lv)),
                        float(_cosine_np(n_np, lv)),
                    ))
                else:
                    # No linked node at this slot (or dim mismatch) → zero padding.
                    slots.append((0.0, 0.0))
            slot_scalars_list.append(slots)
        slot_scalars = torch.tensor(slot_scalars_list, dtype=torch.float32)  # (B, N_SLOTS, 2)

        targets = torch.tensor([[s["target"]] for s in batch], dtype=torch.float32)
        # Soft-label down-weighting: per-sample confidence, 1.0 for hard
        # citations, SOFT_LABEL_WEIGHT for soft_label events (below-floor
        # answer windows — weaker evidence, per the v0.2.87 logger contract).
        # ``weights`` is None on an all-hard batch so that path stays
        # bit-identical to the historical unweighted loss.
        w_list = [float(s.get("sample_weight", 1.0)) for s in batch]
        weights = (
            torch.tensor([[w] for w in w_list], dtype=torch.float32)
            if any(w != 1.0 for w in w_list) else None
        )

        # Move the batch to the net's device when on GPU. No-op on CPU.
        if _use_gpu:
            entities = entities.to(device, non_blocking=True)
            type_idxs = type_idxs.to(device, non_blocking=True)
            slot_scalars = slot_scalars.to(device, non_blocking=True)
            targets = targets.to(device, non_blocking=True)
            if weights is not None:
                weights = weights.to(device, non_blocking=True)

        if training:
            opt.zero_grad()

        with torch.set_grad_enabled(training):
            pred = net(entities, type_idxs, slot_scalars)
            if weights is None:
                mse = F.mse_loss(pred, targets)
                bce = F.binary_cross_entropy(pred, targets)
                delta = delta_mse_loss(pred, targets)
            else:
                # Weighted means normalized by Σw (not B) so the loss scale is
                # comparable across batches with different soft/hard mixes.
                w_sum = weights.sum().clamp_min(1e-8)
                mse = ((pred - targets) ** 2 * weights).sum() / w_sum
                bce = (
                    F.binary_cross_entropy(pred, targets, weight=weights,
                                           reduction="sum") / w_sum
                )
                delta = delta_mse_loss(pred, targets, weight=weights)
            loss = mse + 0.5 * bce + lambda_rank * delta
            mae = torch.mean(torch.abs(pred.detach() - targets))

        if training:
            loss.backward()
            opt.step()

        total_mse += float(mse.detach())
        total_bce += float(bce.detach())
        total_delta += float(delta.detach()) if hasattr(delta, "detach") else float(delta)
        total_mae += float(mae)
        n_batches += 1

    if n_batches == 0:
        return 0.0, 0.0, 0.0, 0.0
    avg_mse = total_mse / n_batches
    avg_bce = total_bce / n_batches
    avg_delta = total_delta / n_batches
    avg_mae = total_mae / n_batches
    return avg_mse + 0.5 * avg_bce + lambda_rank * avg_delta, avg_mse, avg_delta, avg_mae


# ---------------------------------------------------------------------------
# Statistics helpers (preserved from original)
# ---------------------------------------------------------------------------

def log_stats(pairs: "list[tuple[dict, dict]]") -> None:
    """Print dataset statistics before training."""
    n_pairs = len(pairs)
    cited_counts: list[int] = []
    not_cited_counts: list[int] = []
    has_emb_pairs = 0
    has_cosine_sims = 0
    has_stored_cos_features = 0

    for ret_ev, cit_ev in pairs:
        if ret_ev.get("query_emb"):
            has_emb_pairs += 1
        if cit_ev.get("cosine_sims"):
            has_cosine_sims += 1
        if any(
            "cos_qn" in n
            for n in ret_ev.get("nodes", [])
        ):
            has_stored_cos_features += 1
        cits = cit_ev.get("citations", {})
        cited_counts.append(sum(1 for v in cits.values() if v is True))
        not_cited_counts.append(sum(1 for v in cits.values() if v is False))

    total_cited = sum(cited_counts)
    total_not_cited = sum(not_cited_counts)
    total = total_cited + total_not_cited

    print(f"Dataset: {n_pairs} matched pairs")
    print(f"  Pairs with query_emb:        {has_emb_pairs} / {n_pairs}")
    print(f"  Pairs with cosine_sims:      {has_cosine_sims} / {n_pairs}")
    print(f"  Pairs with stored cos feats: {has_stored_cos_features} / {n_pairs}")
    if total:
        print(
            f"  Citations: {total_cited} cited, {total_not_cited} not-cited "
            f"({100*total_cited/total:.1f}% positive)"
        )
    else:
        print("  Citations: none")

    by_type: dict[str, int] = defaultdict(int)
    for ret_ev, _ in pairs:
        by_type[ret_ev.get("task_type", "unknown")] += 1
    if len(by_type) > 1:
        print("  By task_type:", ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def train(
    model_state_path: Path,
    *,
    hub_client: "Any",
    project_id: str,
    epochs: int = 5,
    batch_queries: "int | None" = None,
    patience: int = 2,
    filter_embedding_source: str = "",
    profile: str = "",
    emb_dim: int = 1024,
    shuffle: bool = True,
    lr: "float | None" = None,
    arch_version: str = "v2",
    device: str = "auto",
    since_ms: "int | None" = None,
    until_ms: "int | None" = None,
    seed: "int | None" = None,
    val_share: float = 0.1,
) -> "dict[str, Any]":
    """Train one project's RLModel from its launcher.db events (DB-only),
    with the ONE training objective.

    v0.2.106: the samples are per-query (:func:`queries_from_pairs`, the
    per-citation policy of :func:`citation_targets`), and every step is
    ``objective.batch_loss`` over query-grouped mini-batches
    (``online_learning.fit`` — pairs only WITHIN a query), the loss the
    server's online rounds and the offline shard trainer use. The pre-v0.2.106
    path (``_epoch``: pointwise + a pairwise term over every pair of a
    sample-shuffled batch, i.e. across different queries) no longer trains
    any served model.

    Time-ordered split: the newest ``val_share`` of the queries is held out;
    after each epoch the model's NDCG@10 there is measured
    (``online_learning.evaluate``) and the best epoch's weights are kept
    (early stop after ``patience`` epochs without improvement). Knobs default
    to the online learner's (``online_learning.DEFAULT_CONFIG``: batch of
    queries, learning rate, seed, loss).

    Returns ``{trained: bool, reason, n_events, n_pairs, n_samples, ...}`` —
    ``trained=False`` for no data, so the job never reports a false success.
    """
    if not _HAS_TORCH:
        raise ImportError("PyTorch required: pip install torch")
    if not _HAS_NUMPY:
        raise ImportError("NumPy required: pip install numpy")

    import copy

    from .online_learning import (
        DEFAULT_CONFIG,
        VectorStore,
        build_table,
        fit,
        score_queries,
    )
    from .ranking_metrics import nan_mean

    cfg = DEFAULT_CONFIG
    batch_queries = int(batch_queries or cfg.batch_queries)
    lr = float(lr if lr is not None else cfg.lr)
    seed = int(seed if seed is not None else cfg.seed)
    prof = profile or filter_embedding_source or "qwen3"

    logger.info(
        "offline_trainer: loading events from hub (project_id=%s, emb_source=%s)",
        project_id, filter_embedding_source or "<all>",
    )
    events = load_training_events(
        hub_client,
        project_id,
        filter_embedding_source=filter_embedding_source,
        since_ms=since_ms,
        until_ms=until_ms,
    )
    pairs = pair_events(events)
    logger.info("offline_trainer: %d events → %d matched pairs", len(events), len(pairs))
    if not pairs:
        logger.warning("offline_trainer: no paired samples, nothing to train")
        return {"trained": False, "reason": "no_pairs",
                "n_events": len(events), "n_pairs": 0, "n_samples": 0}

    store = VectorStore(emb_dim)
    queries = queries_from_pairs(pairs, store, profile=prof)
    queries.sort(key=lambda q: q.ts_ms)  # stable: equal stamps keep pair order
    if not queries:
        logger.warning("offline_trainer: no pairs with a usable query + node vectors")
        return {"trained": False, "reason": "no_samples",
                "n_events": len(events), "n_pairs": len(pairs), "n_samples": 0}

    n_val = int(len(queries) * val_share) if len(queries) >= 10 else 0
    train_q = queries[: len(queries) - n_val]
    val_q = queries[len(queries) - n_val:]
    logger.info("offline_trainer: %d queries → train=%d val=%d",
                len(queries), len(train_q), len(val_q))

    rl_model = RLModel.load_or_create(
        model_state_path, emb_dim=emb_dim, arch_version=arch_version, device=device,
    )
    logger.info("offline_trainer: training on device=%s", rl_model.device)
    val_tbl = (build_table(val_q, store, profile=prof, device=torch.device(rl_model.device))
               if val_q else None)

    def val_ndcg() -> float:
        if val_tbl is None:
            return float("nan")
        return nan_mean(score_queries(rl_model._net, val_tbl, cfg.k)[0].ndcg)

    pt_path = model_state_path.with_suffix(".pt")
    run_log_path = pt_path.with_name(pt_path.stem + "_train_log.json")
    training_history: list[dict] = []
    if run_log_path.exists():
        try:
            training_history = json.loads(run_log_path.read_text())
        except Exception:
            pass

    run_start = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    start_ndcg = val_ndcg()
    best_ndcg = start_ndcg
    best_state = None
    since_best = 0
    epochs_done = 0
    steps = 0
    early_stopped = False
    for epoch in range(max(0, int(epochs))):
        steps += fit(rl_model, train_q, store, profile=prof, epochs=1,
                     batch_queries=batch_queries, lr=lr, seed=seed + epoch,
                     shuffle=shuffle, loss=cfg.loss)
        epochs_done += 1
        cur = val_ndcg()
        logger.info("offline_trainer: epoch %d/%d  val NDCG@10=%.4f (start %.4f)",
                    epoch + 1, epochs, cur, start_ndcg)
        if val_tbl is None:
            continue
        if math.isnan(best_ndcg) or cur > best_ndcg + 1e-6:
            best_ndcg = cur
            best_state = copy.deepcopy(rl_model._net.state_dict())
            since_best = 0
        else:
            since_best += 1
            if since_best >= patience:
                early_stopped = True
                logger.info("offline_trainer: early stop after %d epochs (patience=%d)",
                            epochs_done, patience)
                break
    if best_state is not None:
        rl_model._net.load_state_dict(best_state)
    rl_model.save()

    training_history.append({
        "run_start": run_start,
        "run_end": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "objective": "objective.batch_loss (query-grouped)",
        "epochs": epochs_done,
        "steps": steps,
        "val_ndcg_start": None if math.isnan(start_ndcg) else round(start_ndcg, 5),
        "val_ndcg_best": None if math.isnan(best_ndcg) else round(best_ndcg, 5),
        "early_stopped": early_stopped,
        "n_queries": len(queries),
        "n_train": len(train_q),
        "n_val": len(val_q),
    })
    try:
        run_log_path.write_text(json.dumps(training_history, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("offline_trainer: training log write failed: %s", exc)

    return {"trained": steps > 0, "reason": "ok" if steps > 0 else "no_steps",
            "n_events": len(events), "n_pairs": len(pairs), "n_samples": len(queries),
            "n_queries": len(queries), "epochs": epochs_done, "steps": steps,
            "val_ndcg": None if math.isnan(best_ndcg) else best_ndcg}


# ---------------------------------------------------------------------------
# Backward-compatible helpers (used by existing tests and external callers)
# ---------------------------------------------------------------------------



def _advantage_rewards(values: "dict[str, float]") -> "dict[str, float]":
    """Z-score normalise values into advantage rewards in [-1, +1]."""
    if not values:
        return {}
    vals = list(values.values())
    mean_val = sum(vals) / len(vals)
    variance = sum((v - mean_val) ** 2 for v in vals) / len(vals)
    std_val = variance ** 0.5
    denom = max(std_val, 0.05)
    return {
        title: max(-1.0, min(1.0, (v - mean_val) / denom))
        for title, v in values.items()
    }


def compute_rewards(
    retrieval_ev: "dict[str, Any]",
    citation_ev: "dict[str, Any]",
) -> "dict[str, float]":
    """
    Reconstruct analog advantage rewards for a matched event pair.

    If citation_ev has ``cosine_sims``, uses the signed-cosine approach;
    otherwise falls back to binary ±1 with Z-score normalisation.
    Returns ``{title: advantage_reward}`` — empty dict if no usable signal.
    """
    citations: dict[str, Any] = citation_ev.get("citations", {})
    cosine_sims: dict[str, float] = citation_ev.get("cosine_sims", {})

    if cosine_sims:
        signed: dict[str, float] = {}
        for title, cited in citations.items():
            if cited is None:
                continue
            sim = cosine_sims.get(title, 0.5)
            signed[title] = sim if cited else -sim
        return _advantage_rewards(signed)
    else:
        binary: dict[str, float] = {
            title: (1.0 if cited else -1.0)
            for title, cited in citations.items()
            if cited is not None
        }
        return _advantage_rewards(binary)


# v0.2.106: ``train_epoch`` (one ``RLModel.update`` per node — a per-node BCE
# step at lr 1e-3, the pre-v0.2.106 ONLINE objective replayed offline; its one
# caller was the server's ``/global/retrain`` job) is gone together with
# ``RLModel.update``. Online training is per project, in rounds, with the
# trainer's objective (``objective.batch_loss``) —
# ``online_learning.ProjectLearner``; ``/global/retrain`` now fits the same
# objective over :func:`queries_from_pairs` (``online_learning.fit``).


def queries_from_pairs(
    pairs: "list[tuple[dict, dict]]",
    store: Any,
    *,
    profile: str,
) -> "list[Any]":
    """Paired launcher.db events -> the trainer's per-query samples
    (``online_learning.LabelledQuery``, vectors in ``store``).

    The per-citation policy is :func:`citation_targets` (targets, soft-label
    discard / weight, the binary-citation fallback); the sample layout is the
    online path's own (``online_learning.labelled_query``: last node per
    title, linked slots packed, type codes, the profile's linked-slot mask).
    Pairs without a usable query or node are skipped.
    """
    from .online_learning import labelled_query

    out: list[Any] = []
    for ret_ev, cit_ev in pairs:
        decided = citation_targets(cit_ev)
        if decided is None:
            continue
        targets, weight = decided
        task = {
            "nodes_packed": [
                {"title": n.get("title", ""), "node_type": n.get("node_type"),
                 "n_emb": n.get("emb"), "linked_embs": n.get("linked_embs") or [],
                 "linked_type_names": n.get("linked_type_names") or n.get("link_types") or []}
                for n in (ret_ev.get("nodes") or []) if isinstance(n, dict) and n.get("emb")
            ],
            "query_emb": ret_ev.get("query_emb"),
            "literal_cited": cit_ev.get("literal_cited") or {},
        }
        q, _why = labelled_query(task, store, profile=profile,
                                 ts_ms=int(ret_ev.get("ts_ms") or 0),
                                 targets=targets, weight=weight)
        if q is not None:
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m orchestrator.context.offline_trainer",
        description="Offline batch trainer for the RetrievalRL RLModel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # v0.2.11: DB-only. Events come from launcher.db via the module-scoped hub
    # route; --project-id is mandatory and there is no JSONL --source/--log-path.
    p.add_argument("--project-id", type=str, default="", required=False,
                   help="Project to train for (REQUIRED). The hub events route "
                        "is per-project + module-token-scoped. Cohort key = the "
                        "launcher.db project_id column.")
    p.add_argument("--since-ms", type=int, default=None,
                   help="Only events at/after this unix-epoch-ms.")
    p.add_argument("--until-ms", type=int, default=None,
                   help="Only events at/before this unix-epoch-ms.")
    p.add_argument("--hub-base-url", type=str, default=None,
                   help="Override hub base URL (default: $VCT_HUB_BASE_URL or "
                        "http://127.0.0.1:7700).")
    p.add_argument("--model-state", type=Path, required=True,
                   help="Path to model state .pt (created/updated by training)")
    p.add_argument("--epochs", type=int, default=5)
    # v0.2.106: --batch-size / --lambda-rank / --lr-min configured the retired
    # per-sample loss (`_epoch`); superseded by the one objective
    # (objective.batch_loss, its weights in objective.LossConfig) — the batch
    # is now counted in queries.
    p.add_argument("--batch-queries", type=int, default=None,
                   help="Queries per step (default: the online learner's)")
    p.add_argument("--patience", type=int, default=2,
                   help="Early stopping patience (epochs without val NDCG@10 improvement)")
    p.add_argument("--filter-embedding-source", type=str, default="",
                   # codesage REMOVED 2026-05-19 — KG RL is text-only. Code-graph
                   # reranking will be a separate future module. `jina` retained
                   # as a possible future text embedder (Apache-2.0, 768-dim).
                   choices=["", "qwen3", "arctic", "openai", "jina", "legacy"],
                   help="Train only on events with this embedding source. "
                        "Required when training a per-source RLModel "
                        "(e.g. rl_model_qwen3_1024.pt should only see "
                        "embedding_source=qwen3 events). Empty = legacy "
                        "single-network mode.")
    p.add_argument("--min-pairs", type=int, default=1,
                   help="Abort if fewer than N matched pairs found")
    p.add_argument("--emb-dim", type=int, default=1024)
    p.add_argument("--arch-version", type=str, default="v2", choices=["v1", "v2"],
                   help="RLModel architecture version. v2 (default) = transformer "
                        "tail; v1 = legacy MLP snapshot in rl_model_v1.py")
    p.add_argument("--device", type=str, default="auto",
                   help="Device for training: 'auto' (CUDA if available, else CPU), "
                        "'cuda', 'cuda:N', 'cpu'. Invalid values fall back to CPU.")
    p.add_argument("--lr", type=float, default=None,
                   help="AdamW learning rate (default: the online learner's)")
    p.add_argument("--no-shuffle", dest="shuffle", action="store_false",
                   help="Keep time order within each epoch (no seeded permutation)")
    p.set_defaults(shuffle=True)
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv: "list[str] | None" = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    from vco_lib.log_setup import configure_logging

    configure_logging(
        logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not _HAS_TORCH:
        print("ERROR: PyTorch not available. Install: pip install torch")
        return 1

    # DB-only: events come from the hub's module-scoped route, which is
    # per-project + module-token-scoped → --project-id is mandatory.
    if not args.project_id:
        print("ERROR: --project-id is required (the hub events route is "
              "per-project + module-token-scoped). JSONL training was retired "
              "in v0.2.11.")
        return 1
    try:
        from .rl_server import _HubClient
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not import _HubClient: {exc}")
        return 1
    hub_kwargs: dict[str, Any] = {}
    if args.hub_base_url:
        hub_kwargs["base_url"] = args.hub_base_url
    hub_client = _HubClient(**hub_kwargs)
    print(f"Loading events from hub (project_id={args.project_id})...")
    if args.filter_embedding_source:
        print(f"  filter: embedding_source={args.filter_embedding_source}")

    try:
        # Quick stats before training (same source as the train() call below).
        events = load_training_events(
            hub_client,
            args.project_id,
            filter_embedding_source=args.filter_embedding_source,
            since_ms=args.since_ms,
            until_ms=args.until_ms,
        )
        pairs = pair_events(events)
        print(f"Found {len(events)} events → {len(pairs)} matched pairs.")

        if len(pairs) < args.min_pairs:
            print(f"Too few pairs: {len(pairs)} < --min-pairs {args.min_pairs}. Aborting.")
            return 1

        log_stats(pairs)

        print(f"\nTraining for up to {args.epochs} epoch(s) "
              f"(patience={args.patience}, objective.batch_loss)...")
        train(
            model_state_path=args.model_state,
            hub_client=hub_client,
            project_id=args.project_id,
            epochs=args.epochs,
            batch_queries=args.batch_queries,
            patience=args.patience,
            filter_embedding_source=args.filter_embedding_source,
            emb_dim=args.emb_dim,
            shuffle=args.shuffle,
            lr=args.lr,
            arch_version=args.arch_version,
            device=args.device,
            since_ms=args.since_ms,
            until_ms=args.until_ms,
        )
    finally:
        # Close the hub client's aiohttp session.
        try:
            from .hub_event_loader import _run_coro
            _run_coro(hub_client.close())
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    print(f"\nDone. Model saved to {args.model_state.with_suffix('.pt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
