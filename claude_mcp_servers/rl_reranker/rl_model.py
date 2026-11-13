# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
V0.2.0 ARCHITECTURE — transformer-tail RL reranker for KG node relevance.

This is the active code path. The v0.1.x architecture is preserved verbatim
in rl_model_v1.py and instantiated only when RLModel is constructed with
arch_version="v1" (offline A/B comparison + emergency rollback).

Spec: .claude/context/plans/rl-module-arch-v0.2.0-spec-2026-05-22.md

ARCHITECTURE OVERVIEW

Forward pipeline (all dims shown for emb_dim=1024):

  INPUT
    entities      (B, 7, emb_dim)      # slot 0=query, 1=target, 2..6=linked
    type_idxs     (B, 7) int64
    slot_scalars  (B, 7, 2)             # (cos_to_query, cos_to_target) per slot

  FIRST SHARED BLOCK (3 layers, weights shared across all 7 slots)
    enc1: Linear(emb_dim → emb_dim//2) + SiLU    (B, 7, 512)
    enc2: Linear(512 → 448)             + SiLU   (B, 7, 448)
    enc3: Linear(448 → 128)             + SiLU   (B, 7, 128)

  REFINEMENT BLOCK (2 layers, also shared)
    ref_in = concat(enc3_out, type_emb, slot_scalars)  → (B, 7, 136)
    ref1: Linear(136 → 96)              + SiLU   (B, 7, 96)
    ref2: Linear(96 → 64)               + SiLU   (B, 7, 64)

  TRACK A (per-slot info + mid-stream cross-path mixing query↔target)
    flatten ref2_out                              (B, 7*64=448)
    trackA_1: Linear(448 → 256)         + SiLU   (B, 256)
    trackA_2: Linear(256 → 256)         + SiLU   (B, 256)
    # Cross-path: explicit query×target mixer between trackA_2 and trackA_3
    cross_in = concat(ref2_out[:,0,:], ref2_out[:,1,:])  → (B, 128)
    cross_1: Linear(128 → 96)           + SiLU   (B, 96)
    cross_2: Linear(96 → 64)            + SiLU   (B, 64)
    trackA_merged = concat(cross_out, trackA_2)   → (B, 320)
    trackA_3: Linear(320 → 192)         + SiLU   (B, 192)
    reshape                                       (B, 16, 12)

  TRACK B (query/target high-importance lane)
    trackB_in = concat(q_enc3, n_enc3, q_ref2, n_ref2, cos(q,n))  → (B, 385)
    trackB_1: Linear(385 → 384)         + SiLU   (B, 384)
    trackB_2: Linear(384 → 384)         + SiLU   (B, 384)
    reshape                                       (B, 16, 24)

  TOKEN CONCAT (feature axis)
    tokens = concat(trackA, trackB, dim=-1)       (B, 16, 36)

  TRANSFORMER TAIL (2 encoder layers, pre-norm, dropout 0.2)
    per layer:
      LayerNorm(36) → MHA(heads=4, head_dim=9) → Dropout → residual
      LayerNorm(36) → FFN(36→128→36, SiLU)     → Dropout → residual

  HEAD (concat-then-shrink, no pooling)
    flatten                                       (B, 576)
    head_1: Linear(576 → 384) + SiLU + Dropout(0.2)
    head_2: Linear(384 → 256) + SiLU + Dropout(0.2)
    head_3: Linear(256 → 128) + SiLU
    head_4: Linear(128 → 64)  + SiLU
    head_5: Linear(64  → 1)   + Sigmoid

Total: ~1.78M params at emb_dim=1024.

TYPE-EMBEDDING SLOTS

  0..5  builtin: concept, project, tool, model, hardware, research
  6     NULL_TYPE_IDX     padding slot (no linked node present)
  7     QUERY_TYPE_IDX    query entity marker
  8     UNKNOWN_TYPE_IDX  linked node present but node_type unknown
  9-255 dynamic types registered at runtime (mean-init from builtins)

PER-SLOT COSINE FEATURES (computed in _build_inputs)

  Query slot:       (1.0,         1.0)        identity anchor
  Target slot:      (cos(q,n),    1.0)
  Linked slot:      (cos(q,link), cos(n,link))
  NULL padding:     (0.0,         0.0)

TRAINING

  AdamW(lr=1e-3, weight_decay=1e-4). Single-sample online update() or batched
  offline via offline_trainer._epoch. BCE loss. Full autograd backprop.

CHECKPOINT FORMAT

  state/rl_model_<tag>.pt  — PyTorch checkpoint with arch_version="v2".

  Pre-v2 checkpoints (no arch_version field, or arch_version="v1") are
  refused by _load_pt — the new code He-initialises a fresh model instead.
  No in-place migration: v0.2.0 is too structurally different from v0.1.x
  to salvage learned weights. Users re-train via offline_trainer on logged
  events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
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

# ---------------------------------------------------------------------------
# Architecture constants (v0.2.0)
# ---------------------------------------------------------------------------

ARCH_VERSION = "v2"

# First shared block (enc1 → enc2 → enc3, applied per slot)
# enc1 output is emb_dim // 2 — computed at instantiation, NOT a fixed constant.
ENC2_DIM = 448
ENC3_DIM = 128

# Refinement block (ref1 → ref2, applied per slot)
REF1_DIM = 96
REF2_DIM = 64

# Type embeddings
TYPE_EMB_DIM = 6
_MAX_TYPES = 256

# Slot layout — UNCHANGED from v0.1.x to keep retrieval_rl.py + offline_trainer.py
# input-building code working without modification.
MAX_LINKED = 5
N_SLOTS = 2 + MAX_LINKED  # 7

NODE_TYPES: tuple[str, ...] = (
    "concept", "project", "tool", "model", "hardware", "research",
)
NULL_TYPE_IDX: int = len(NODE_TYPES)         # 6 -- padding slot
QUERY_TYPE_IDX: int = NULL_TYPE_IDX + 1      # 7 -- query entity marker
UNKNOWN_TYPE_IDX: int = QUERY_TYPE_IDX + 1   # 8 -- linked node, unknown type
N_ENTITY_TYPES: int = UNKNOWN_TYPE_IDX + 1   # 9 builtin slots

#: Type code for a node / linked node whose type is not one of the builtin
#: :data:`NODE_TYPES` (the training export's ``other`` code). The real name is
#: never exported (type names can be project-specific). Training
#: (``objective``) maps this code to the same embedding a serving model gives
#: a type it registers on the fly: the mean of the builtin rows.
TYPE_CODE_OTHER: int = 9


def type_code(name: "str | None", *, empty: int) -> int:
    """The training type code of a type name: its builtin index, ``empty``
    for none, else :data:`TYPE_CODE_OTHER`. One table for the export and the
    server's online learning."""
    if not name:
        return empty
    try:
        return NODE_TYPES.index(name)
    except ValueError:
        return TYPE_CODE_OTHER

#: Embedding profiles whose linked-entity slots (2..6) are MASKED: always NULL
#: padding (zero vector, NULL_TYPE_IDX, cosines (0, 0)), in training AND in
#: serving. Arctic training data comes from dual-log twins, which carry no
#: linked vectors by design (search_pipeline ``_ACTIVE_SPACE_ONLY_FIELDS``),
#: so a model trained on them never saw a filled linked slot. An
#: arctic-ACTIVE install's online path would fill those slots, and the model
#: would score inputs it was never trained on. The trainer
#: (``shard_trainer``) and the server's scoring path both read
#: :func:`linked_slots_masked` — one rule, both sides. Remove a profile from
#: this set only together with weights trained on its linked vectors.
LINKED_SLOTS_MASKED_PROFILES: frozenset[str] = frozenset({"arctic"})


def linked_slots_masked(profile: str) -> bool:
    """True when the linked-entity slots are masked for ``profile``."""
    return (profile or "").strip().lower() in LINKED_SLOTS_MASKED_PROFILES

# ---------------------------------------------------------------------------
# D1' — sidecar dynamic-type embeddings layout (v0.2.2+).
#
# Builtin slots 0..8 (N_ENTITY_TYPES rows of type_embs) ship inside the .pt
# alongside the network state. Dynamic types (rows 9..) are persisted as
# per-name sidecar files under <state_dir>/type_embs/ so they survive a
# /rotate_weights swap of the network .pt.
#
# Filename: "<sanitized_name>__<8-char-hash>.pt"
#   - sanitized_name: ASCII alphanumeric + "_" + "-" only (other chars → "_")
#   - hash: first 8 hex chars of blake2b(unsanitized_name) -- guarantees
#     filename uniqueness even when two distinct names sanitize identically
#     (e.g. "a/b" and "a-b" both become "a_b").
#
# Sidecar content (torch.save dict):
#   {
#     "name":    str   -- original (unsanitized) type name
#     "emb":     torch.Tensor(TYPE_EMB_DIM,) float32 on CPU
#     "version": int   -- format version (currently SIDECAR_FORMAT_VERSION)
#   }
#
# Marker file: <state_dir>/.types_layout_v2 is written after the first
# successful migration or split-save. Its presence signals "no further
# migration needed". Absent + .pt contains dynamic-type entries (idx >=
# N_ENTITY_TYPES) → trigger migration on next load.
# ---------------------------------------------------------------------------

SIDECAR_DIR_NAME = "type_embs"
SIDECAR_MARKER_NAME = ".types_layout_v2"
SIDECAR_FORMAT_VERSION = 1
_SIDECAR_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_\-]")

# Track A (per-slot info → flatten → FF → reshape into a flat output)
TRACKA_HIDDEN1 = 256
TRACKA_HIDDEN2 = 256
TRACKA_OUT_DIM = 192

# Cross-path (lives inside Track A, between trackA_2 and trackA_3)
# Mixes the refined query and target-node representations explicitly.
CROSS_HIDDEN = 96
CROSS_OUT = 64

# Track B (high-importance query/target lane)
TRACKB_HIDDEN = 384
TRACKB_OUT_DIM = 384

# Final head — concat trackA + trackB then shrink, no pooling.
HEAD_INPUT_DIM = TRACKA_OUT_DIM + TRACKB_OUT_DIM  # 576
HEAD_H1 = 384
HEAD_H2 = 256
HEAD_H3 = 128
HEAD_H4 = 64
HEAD_DROPOUT = 0.2

# Training
LR_MODEL = 1e-3
WEIGHT_DECAY = 1e-4

# ---------------------------------------------------------------------------
# Cos3heads block (2026-05-23 bake-off extension).
#
# Three independent (q_head_i, n_head_i) MLP pairs project the query and
# candidate-node raw embeddings (emb_dim) down to 8-d, with cosine similarity
# of each pair forming a 3-scalar feature vector (cos3). cos3 is then fed
# verbatim into Track A (concat with its existing input), Track B (concat),
# Track C (new, takes only cos3), and the final head (concat with the three
# track outputs).
#
# This is gated by ``use_cos3heads=True`` in ``_RLNet.__init__``. The
# baseline path (use_cos3heads=False) is the canonical v0.2.0 reranker.
# ---------------------------------------------------------------------------

COS3_HEAD_HIDDEN = 8       # all three Linear layers in each q/n head are 8-wide
COS3_NUM_PAIRS = 3         # cos3 vector is length-3
TRACKC_HIDDEN = 16         # Linear(3, 16)
TRACKC_OUT = 8             # Linear(16, 8) — feeds into final head

# ---------------------------------------------------------------------------
# Activation registry (used by per-track activation switching in cos3 variants).
# ---------------------------------------------------------------------------


def _get_activation(name: str) -> "nn.Module":
    """Return a fresh nn.Module activation for a given name string.

    Each variant's track uses a fresh instance (no shared module-state — these
    activations are stateless but conceptually owned by the track).
    """
    if not _HAS_TORCH:
        raise ImportError("PyTorch is required for activation modules.")
    key = (name or "").strip().lower()
    if key in ("silu", "swish"):
        return nn.SiLU()
    if key == "gelu":
        return nn.GELU()
    if key == "relu":
        return nn.ReLU()
    raise ValueError(f"Unsupported activation: {name!r}; expected silu|gelu|relu")


# ---------------------------------------------------------------------------
# Arch-variant configuration table.
#
# Each entry maps a variant name (the public ``arch_version`` string) to the
# constructor kwargs that ``_RLNet`` needs to materialise that variant.
#
#   - "v2"                 : canonical reranker (uniform SiLU, no cos3heads)
#   - "v2_no_tx_24ep"      : SAME architecture as "v2"; the suffix only
#                            affects training epoch budget (handled in
#                            train_v4_generic.py). Checkpoints saved under
#                            this arch_version load cleanly here.
#   - "v2_cos3heads_all_gelu" : cos3heads on; GELU everywhere.
#   - "v2_cos3heads_all_silu" : cos3heads on; SiLU everywhere.
#   - "v2_cos3heads_mixed"    : cos3heads on; Track A GELU, Track B SiLU,
#                               Track C SiLU, cos3 heads SiLU.
#
# Note on per-track activations for "v2" / "v2_no_tx_24ep":
#   The canonical v0.2.0 reranker uses ``F.silu`` UNIFORMLY across all
#   tracks (verified by grep against rl_model.py 2026-05-23). The bake-off
#   spec's mention of "Track A: GELU (current)" is an annotation error —
#   we preserve the literal current behaviour (SiLU everywhere) for the
#   baseline and the 24-epoch re-run so it stays an apples-to-apples test
#   against the prior 16-epoch v2_no_tx run.
# ---------------------------------------------------------------------------

_ARCH_CONFIG: dict[str, dict] = {
    # v0.2.6 default — promoted from the 2026-05-23 cos3heads bake-off winner.
    # Was "v2_cos3heads_all_silu" during the ablation; now THE canonical "v2".
    # No backward-compat with the pre-v0.2.6 "v2" (uniform SiLU, no cos3heads)
    # because zero production users existed on that arch — clean cutover.
    # See data/bake_off_cos3heads_2026-05-23.md + the RL arch-pitfalls KG node
    # for the bake-off methodology + decision rationale.
    "v2": {
        "use_cos3heads": True,
        "act_a": "silu",
        "act_b": "silu",
        "act_c": "silu",
        "act_cos3": "silu",
        "act_shared": "silu",
    },
    # Bake-off ablation variants — kept for forensic reproducibility, NOT
    # ship targets. Trained checkpoints under weights/<arch>/ for re-eval.
    "v2_no_tx_24ep": {
        "use_cos3heads": False,
        "act_a": "silu",
        "act_b": "silu",
        "act_c": "silu",
        "act_cos3": "silu",
        "act_shared": "silu",
    },
    "v2_cos3heads_all_gelu": {
        "use_cos3heads": True,
        "act_a": "gelu",
        "act_b": "gelu",
        "act_c": "gelu",
        "act_cos3": "gelu",
        "act_shared": "silu",
    },
    "v2_cos3heads_all_silu": {
        # Alias of "v2" for ablation-result traceability. Same config.
        "use_cos3heads": True,
        "act_a": "silu",
        "act_b": "silu",
        "act_c": "silu",
        "act_cos3": "silu",
        "act_shared": "silu",
    },
    "v2_cos3heads_mixed": {
        "use_cos3heads": True,
        "act_a": "gelu",
        "act_b": "silu",
        "act_c": "silu",
        "act_cos3": "silu",
        "act_shared": "silu",
    },
}

# ---------------------------------------------------------------------------
# Cosine helper (used for per-slot feature computation)
# ---------------------------------------------------------------------------

from .math_utils import cosine_similarity_np as _cosine_np  # noqa: E402
from vco_lib.atomic import atomic_write_bytes, atomic_write_text  # noqa: E402
from vco_lib.log_setup import log_safe  # noqa: E402


def load_checkpoint(path: Path) -> Any:
    """Read a checkpoint (``.pt``) onto the CPU with ``weights_only=True`` —
    the ONE place this module deserializes a file.

    ``weights_only=True`` makes ``torch.load`` use its restricted unpickler:
    tensors, primitive containers and numbers only, so a ``.pt`` carrying a
    pickled callable (``os.system``, any ``__reduce__`` payload) is REFUSED
    with an ``UnpicklingError`` instead of executing. Everything this module
    writes (network checkpoint, optimizer state, metadata strings and ints,
    sidecar ``{"name", "emb", "version"}``) is inside that set, so no
    allowlist (``torch.serialization.add_safe_globals``) is needed. Raises
    whatever ``torch.load`` raises; callers treat that as "not loadable".
    """
    return torch.load(str(path), map_location="cpu", weights_only=True)


def _atomic_torch_save(obj: object, path: Path) -> None:
    """``torch.save`` ``obj`` to ``path`` atomically.

    Serialises to memory, then hands the bytes to ``vco_lib.atomic`` — the
    ONE home of the tmp+fsync+rename write (the image COPIES it). A crash
    leaves either the previous checkpoint or the complete new one, never a
    truncated file. Checkpoints are a few MB, so holding one in memory is
    free.
    """
    import io

    buf = io.BytesIO()
    torch.save(obj, buf)
    atomic_write_bytes(Path(path), buf.getvalue())


# ---------------------------------------------------------------------------
# Network base — abstracts away the torch.nn.Module dependency when PyTorch
# is missing (lets the file import cleanly under typecheckers / linters that
# don't have torch installed).
# ---------------------------------------------------------------------------

_NetBase = nn.Module if _HAS_TORCH else object


# ---------------------------------------------------------------------------
# _RLNet — the v0.2.0 reranker.
#
# Picked as the winning architecture after a 10-way ablation sweep on the
# arctic 3375-pair corpus (see knowledge/concepts/rl-arch-comparison-pitfalls-
# 2026-05-22.md for the full bake-off). Beat every variant that added
# attention / LayerNorm / extra capacity / explicit pairwise interactions.
#
# Forward pipeline:
#   shared encoder (3 layers, per slot)  →  ENC3_DIM=128 per slot
#   refinement (2 layers, per slot)      →  REF2_DIM=64 per slot
#   Track A: flatten ref2 → FF×2 + cross-path mixing → 192-d
#   Track B: high-importance q/n lane    → 384-d
#   concat → head (576 → 384 → 256 → 128 → 64 → 1 + sigmoid)
#
# ~1.75M params at emb_dim=1024. No transformer, no normalization, no gates.
# Adding any of those (tested empirically) makes it worse on a dataset
# of this size.
# ---------------------------------------------------------------------------

class _RLNet(_NetBase):  # type: ignore[misc,valid-type]
    """The v0.2.0 reranker neural network.

    Slot layout (unchanged from v0.1.x):
        slot 0    = query
        slot 1    = target node
        slots 2-6 = linked-node context (NULL_TYPE_IDX padding when absent)

    Variant gating (2026-05-23):
        ``use_cos3heads=True`` extends the network with three independent
        (q_head_i, n_head_i) projection-pair MLPs producing a length-3
        cosine-similarity vector ``cos3``. cos3 is appended to Track A's
        input, Track B's input, fed solely into a new Track C, and
        concatenated with the three track outputs at the final head.

        Per-track activation choices are constructor kwargs (default SiLU
        everywhere — the canonical v0.2.0 reranker). The first linear of
        Tracks A/B grows by ``COS3_NUM_PAIRS=3`` input dims when cos3heads
        is on; the final head's first linear grows by ``TRACKC_OUT + 3 = 11``
        dims (Track C output + raw cos3).
    """

    def __init__(
        self,
        emb_dim: int,
        use_cos3heads: bool = False,
        act_a: str = "silu",
        act_b: str = "silu",
        act_c: str = "silu",
        act_cos3: str = "silu",
        act_shared: str = "silu",
    ) -> None:
        super().__init__()
        if emb_dim < 2:
            raise ValueError(f"emb_dim must be ≥ 2, got {emb_dim}")
        self.emb_dim = emb_dim
        self._enc1_dim = emb_dim // 2
        self._use_cos3heads = bool(use_cos3heads)

        # Activation modules — stored as submodules so save/load works and
        # per-track activation choices are introspectable from the state_dict.
        self.act_shared = _get_activation(act_shared)
        self.act_a = _get_activation(act_a)
        self.act_b = _get_activation(act_b)
        self.act_c = _get_activation(act_c)
        self.act_cos3 = _get_activation(act_cos3)
        # Forensic record of the activation choices (string form). Not
        # serialised in state_dict but useful for debugging via repr().
        self._act_spec = {
            "shared": act_shared, "a": act_a, "b": act_b,
            "c": act_c, "cos3": act_cos3,
        }

        # First shared block (3 layers)
        self.enc1 = nn.Linear(emb_dim, self._enc1_dim)
        self.enc2 = nn.Linear(self._enc1_dim, ENC2_DIM)
        self.enc3 = nn.Linear(ENC2_DIM, ENC3_DIM)

        # Refinement block (2 layers)
        self._ref_in = ENC3_DIM + TYPE_EMB_DIM + 2
        self.ref1 = nn.Linear(self._ref_in, REF1_DIM)
        self.ref2 = nn.Linear(REF1_DIM, REF2_DIM)
        self.type_embs = nn.Embedding(_MAX_TYPES, TYPE_EMB_DIM)

        # ----- Cos3heads projection pairs + Track C (gated on use_cos3heads) -----
        # Six small MLPs: 3 for the query (q_head_1..3), 3 for the candidate
        # node (n_head_1..3). Each is Linear(emb_dim, 8) → act → Linear(8, 8)
        # → act → Linear(8, 8). The final linear is NOT followed by an
        # activation (we feed the raw 8-d vector into cosine similarity).
        if self._use_cos3heads:
            self.q_head_1_l1 = nn.Linear(emb_dim, COS3_HEAD_HIDDEN)
            self.q_head_1_l2 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.q_head_1_l3 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.q_head_2_l1 = nn.Linear(emb_dim, COS3_HEAD_HIDDEN)
            self.q_head_2_l2 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.q_head_2_l3 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.q_head_3_l1 = nn.Linear(emb_dim, COS3_HEAD_HIDDEN)
            self.q_head_3_l2 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.q_head_3_l3 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.n_head_1_l1 = nn.Linear(emb_dim, COS3_HEAD_HIDDEN)
            self.n_head_1_l2 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.n_head_1_l3 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.n_head_2_l1 = nn.Linear(emb_dim, COS3_HEAD_HIDDEN)
            self.n_head_2_l2 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.n_head_2_l3 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.n_head_3_l1 = nn.Linear(emb_dim, COS3_HEAD_HIDDEN)
            self.n_head_3_l2 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)
            self.n_head_3_l3 = nn.Linear(COS3_HEAD_HIDDEN, COS3_HEAD_HIDDEN)

            # Track C — takes ONLY the 3-scalar cos3 vector. No embedding
            # feature mixing (per spec).
            self.trackC_1 = nn.Linear(COS3_NUM_PAIRS, TRACKC_HIDDEN)
            self.trackC_2 = nn.Linear(TRACKC_HIDDEN, TRACKC_OUT)

        # ----- Track A — flatten ref2 over slots + FF + cross-path -----
        # When cos3heads is on, the first linear grows by COS3_NUM_PAIRS (3)
        # input dims to accommodate the appended cos3 vector.
        self._tracka_in = N_SLOTS * REF2_DIM
        tracka_extra = COS3_NUM_PAIRS if self._use_cos3heads else 0
        self.trackA_1 = nn.Linear(self._tracka_in + tracka_extra, TRACKA_HIDDEN1)
        self.trackA_2 = nn.Linear(TRACKA_HIDDEN1, TRACKA_HIDDEN2)
        self.cross_1 = nn.Linear(2 * REF2_DIM, CROSS_HIDDEN)
        self.cross_2 = nn.Linear(CROSS_HIDDEN, CROSS_OUT)
        self.trackA_3 = nn.Linear(CROSS_OUT + TRACKA_HIDDEN2, TRACKA_OUT_DIM)

        # ----- Track B — high-importance q/n lane -----
        # Same input growth as Track A when cos3heads is on.
        self._trackb_in = 2 * ENC3_DIM + 2 * REF2_DIM + 1
        trackb_extra = COS3_NUM_PAIRS if self._use_cos3heads else 0
        self.trackB_1 = nn.Linear(self._trackb_in + trackb_extra, TRACKB_HIDDEN)
        self.trackB_2 = nn.Linear(TRACKB_HIDDEN, TRACKB_OUT_DIM)

        # NO transformer here — that's the ablation.

        # ----- Head — concat trackA + trackB (+ trackC + cos3 when enabled) → shrink to 1 -----
        # Dropout between head_1 and head_3 (not on the deeper bottleneck layers).
        assert TRACKA_OUT_DIM + TRACKB_OUT_DIM == HEAD_INPUT_DIM, (
            "Track A + Track B output dims must sum to HEAD_INPUT_DIM (576)."
        )
        head_extra = (TRACKC_OUT + COS3_NUM_PAIRS) if self._use_cos3heads else 0
        self.head_1 = nn.Linear(HEAD_INPUT_DIM + head_extra, HEAD_H1)
        self.head_dropout_1 = nn.Dropout(HEAD_DROPOUT)
        self.head_2 = nn.Linear(HEAD_H1, HEAD_H2)
        self.head_dropout_2 = nn.Dropout(HEAD_DROPOUT)
        self.head_3 = nn.Linear(HEAD_H2, HEAD_H3)
        self.head_4 = nn.Linear(HEAD_H3, HEAD_H4)
        self.head_5 = nn.Linear(HEAD_H4, 1)

    def _shared_encoder(self, raws: "torch.Tensor") -> "torch.Tensor":
        B, N, D = raws.shape
        x = raws.reshape(B * N, D)
        x = self.act_shared(self.enc1(x))
        x = self.act_shared(self.enc2(x))
        x = self.act_shared(self.enc3(x))
        return x.reshape(B, N, ENC3_DIM)

    def _refinement(
        self,
        enc3_out: "torch.Tensor",
        tidxs: "torch.Tensor",
        slot_scalars: "torch.Tensor",
    ) -> "torch.Tensor":
        B, N, _ = enc3_out.shape
        t = self.type_embs(tidxs)
        ref_in = torch.cat([enc3_out, t, slot_scalars], dim=-1)
        x = ref_in.reshape(B * N, self._ref_in)
        x = self.act_shared(self.ref1(x))
        x = self.act_shared(self.ref2(x))
        return x.reshape(B, N, REF2_DIM)

    def _compute_cos3(
        self,
        q_raw: "torch.Tensor",
        n_raw: "torch.Tensor",
    ) -> "torch.Tensor":
        """Compute the length-3 cosine-similarity vector from the 6 projection heads.

        Args:
            q_raw: (B, emb_dim) raw query embedding (entities[:, 0, :]).
            n_raw: (B, emb_dim) raw target-node embedding (entities[:, 1, :]).

        Returns:
            cos3: (B, 3) — three cosine similarities, one per (q_head_i, n_head_i)
                  pair. Each in [-1, 1].

        Numerical note: ``F.cosine_similarity`` adds a tiny eps internally (1e-8
        by default) so zero-vector projections don't divide by zero. Our heads
        have biases so they're effectively never identically zero in practice,
        but the safety net is there.
        """
        # q_head_i / n_head_i: emb_dim → 8 → 8 → 8 (no final activation)
        qh1 = self.q_head_1_l3(self.act_cos3(self.q_head_1_l2(self.act_cos3(self.q_head_1_l1(q_raw)))))
        qh2 = self.q_head_2_l3(self.act_cos3(self.q_head_2_l2(self.act_cos3(self.q_head_2_l1(q_raw)))))
        qh3 = self.q_head_3_l3(self.act_cos3(self.q_head_3_l2(self.act_cos3(self.q_head_3_l1(q_raw)))))
        nh1 = self.n_head_1_l3(self.act_cos3(self.n_head_1_l2(self.act_cos3(self.n_head_1_l1(n_raw)))))
        nh2 = self.n_head_2_l3(self.act_cos3(self.n_head_2_l2(self.act_cos3(self.n_head_2_l1(n_raw)))))
        nh3 = self.n_head_3_l3(self.act_cos3(self.n_head_3_l2(self.act_cos3(self.n_head_3_l1(n_raw)))))
        c1 = F.cosine_similarity(qh1, nh1, dim=-1)  # (B,)
        c2 = F.cosine_similarity(qh2, nh2, dim=-1)  # (B,)
        c3 = F.cosine_similarity(qh3, nh3, dim=-1)  # (B,)
        return torch.stack([c1, c2, c3], dim=-1)    # (B, 3)

    def forward(
        self,
        entities: "torch.Tensor",
        type_idxs: "torch.Tensor",
        slot_scalars: "torch.Tensor",
    ) -> "torch.Tensor":
        B = entities.shape[0]

        enc3_out = self._shared_encoder(entities)
        ref2_out = self._refinement(enc3_out, type_idxs, slot_scalars)

        # Compute cos3 from RAW query/target embeddings (slot 0 / slot 1).
        # Done once and reused across Tracks A, B, C, and the final head.
        if self._use_cos3heads:
            q_raw_emb = entities[:, 0, :]  # (B, emb_dim)
            n_raw_emb = entities[:, 1, :]  # (B, emb_dim)
            cos3 = self._compute_cos3(q_raw_emb, n_raw_emb)  # (B, 3)
        else:
            cos3 = None

        # ----- Track A — flatten ref2 over slots + FF + cross-path -----
        flat = ref2_out.reshape(B, -1)
        if cos3 is not None:
            flat = torch.cat([flat, cos3], dim=-1)
        a1 = self.act_a(self.trackA_1(flat))
        a2 = self.act_a(self.trackA_2(a1))
        q_ref2 = ref2_out[:, 0, :]
        n_ref2 = ref2_out[:, 1, :]
        cross_in = torch.cat([q_ref2, n_ref2], dim=-1)
        c1 = self.act_a(self.cross_1(cross_in))
        c2 = self.act_a(self.cross_2(c1))
        merged = torch.cat([c2, a2], dim=-1)
        trackA_out = self.act_a(self.trackA_3(merged))      # (B, 192)

        # ----- Track B — high-importance q/n lane -----
        q_enc3 = enc3_out[:, 0, :]
        n_enc3 = enc3_out[:, 1, :]
        cos_qn = slot_scalars[:, 1, 0:1]
        trackB_in = torch.cat([q_enc3, n_enc3, q_ref2, n_ref2, cos_qn], dim=-1)
        if cos3 is not None:
            trackB_in = torch.cat([trackB_in, cos3], dim=-1)
        b1 = self.act_b(self.trackB_1(trackB_in))
        trackB_out = self.act_b(self.trackB_2(b1))          # (B, 384)

        # ----- Track C — cos3-only lane (only when use_cos3heads is on) -----
        if cos3 is not None:
            tc = self.act_c(self.trackC_1(cos3))
            trackC_out = self.act_c(self.trackC_2(tc))      # (B, 8)
        else:
            trackC_out = None

        # ----- Concat into head -----
        if cos3 is not None:
            head_in = torch.cat([trackA_out, trackB_out, trackC_out, cos3], dim=-1)
        else:
            head_in = torch.cat([trackA_out, trackB_out], dim=-1)  # (B, 576)
        h = self.act_shared(self.head_1(head_in))
        h = self.head_dropout_1(h)
        h = self.act_shared(self.head_2(h))
        h = self.head_dropout_2(h)
        h = self.act_shared(self.head_3(h))
        h = self.act_shared(self.head_4(h))
        out = torch.sigmoid(self.head_5(h))
        return out


# ---------------------------------------------------------------------------
# RLModel — public API.
#
# v0.2.0 collapsed the multi-architecture dispatcher into a single canonical
# v2 reranker (_RLNet above). v1 (the v0.1.x snapshot in rl_model_v1.py) is
# still loadable for emergency rollback via arch_version="v1" but is never
# exercised in production paths. All other v2 variants (_RLNetV2NoTxLN,
# _RLNetV2WideNoTx, _RLNetV2DualResidual, _RLNetV2PairwiseNoTx,
# _RLNetV2GatedAttn, _RLNetV2NoTxLNHeadOnly) were deleted after the
# 2026-05-22 ablation sweep proved _RLNet won.
# ---------------------------------------------------------------------------

def read_checkpoint_meta(path: Path) -> "dict | None":
    """The metadata fields of a checkpoint, or ``None`` when it cannot be read.

    Returns ``{"weights_version", "embedding_source", "base_weights_version",
    "local_train_steps", "arch_version", "emb_dim"}`` (missing fields -> ""/0).
    Loads with ``weights_only=True`` on the CPU; never raises.
    """
    if not _HAS_TORCH:
        return None
    try:
        ckpt = load_checkpoint(path)
    except Exception as exc:  # noqa: BLE001 - unreadable = not a candidate
        logger.warning("RLModel: cannot read checkpoint metadata of %s: %s", path, exc)
        return None
    if not isinstance(ckpt, dict):
        return None
    try:
        steps = max(0, int(ckpt.get("local_train_steps", 0) or 0))
    except (TypeError, ValueError):
        steps = 0
    return {
        "weights_version": str(ckpt.get("weights_version", "") or ""),
        "embedding_source": str(ckpt.get("embedding_source", "") or ""),
        "base_weights_version": str(ckpt.get("base_weights_version", "") or ""),
        "local_train_steps": steps,
        "arch_version": str(ckpt.get("arch_version", "v1") or "v1"),
        "emb_dim": ckpt.get("emb_dim"),
    }


class RLModel:
    """v0.2.0 reranker with v1-compatible public API + arch_version selection.

    Default arch_version="v2" instantiates the new transformer-tail network.
    Explicit arch_version="v1" loads the snapshot in rl_model_v1.py for
    offline A/B comparison or emergency rollback.

    Args:
        emb_dim:      Input embedding dimension (e.g. 1024 for arctic/qwen3,
                      1536 for openai-3-small).
        state_path:   Where to load/save weights (.pt file).
        arch_version: "v2" (default) or "v1". Selects which inner network is
                      instantiated. Checkpoint files are arch-tagged; loading
                      a v1 checkpoint into a v2 RLModel is refused.

    Slot layout (unchanged from v0.1.x):
        [query, target_node, link0..link4]

    Per-slot cosine features are computed in _build_inputs from raw embeddings;
    callers pass linked_raws as a list of np.ndarray and the model derives
    cos_to_query / cos_to_target internally.
    """

    def __init__(
        self,
        emb_dim: int,
        state_path: Path,
        arch_version: str = "v2",
        device: str = "auto",
        weights_version: str = "",
        embedding_source: str = "",
    ) -> None:
        """Construct or load an RLModel.

        ``weights_version`` (v0.2.7+) is a SEMVER label for the trained
        checkpoint (e.g. "0.2.6" when seeded from the shipped reference).
        Distinct from ``arch_version`` which is a structural identifier
        ("v2"). The shipped checkpoint stamps it at training time; users'
        fine-tuned checkpoints inherit + may bump via release-tagging.
        Empty string = "unknown" (legacy / pre-v0.2.7 checkpoints).

        ``embedding_source`` (v0.2.7+) records which embedder produced the
        vectors this network was trained against ("arctic" / "qwen3" / etc.).
        Used as a defense-in-depth check at load time: a checkpoint trained
        on arctic-1024 cannot be loaded into a container pinned to qwen3,
        even though the dimensions match — they're vectors in different
        latent spaces and would silently produce garbage scores. Empty
        string = "unknown" (legacy / pre-v0.2.7 checkpoints); when empty,
        the cross-source check is permissive (warn-only).
        """
        if not _HAS_TORCH:
            raise ImportError(
                "PyTorch is required for RLModel. Install with: pip install torch"
            )
        # v1 = legacy snapshot in rl_model_v1.py.
        # v2 = canonical v0.2.0 reranker.
        # v2_no_tx_24ep = same architecture as v2, different epoch budget at
        #   train time (suffix is informational; the inner _RLNet is identical).
        # v2_cos3heads_* = v2 + 3 (q_head, n_head) projection-pair MLPs +
        #   Track C, with per-track activation choices encoded in the name.
        _VALID_ARCHS = (
            "v1",
            "v2",
            "v2_no_tx_24ep",
            "v2_cos3heads_all_gelu",
            "v2_cos3heads_all_silu",
            "v2_cos3heads_mixed",
        )
        if arch_version not in _VALID_ARCHS:
            raise ValueError(
                f"arch_version must be one of {_VALID_ARCHS!r}, got {arch_version!r}"
            )

        # Device resolution. "auto" picks CUDA if available else CPU. Explicit
        # "cuda" / "cpu" / "cuda:N" is honored. Invalid values fall back to
        # CPU with a warning rather than crashing — production users on
        # CPU-only machines should never crash because of a misset env var.
        self._device = self._resolve_device(device)

        self._emb_dim = emb_dim
        self._arch_version = arch_version
        self._weights_version = weights_version
        self._embedding_source = embedding_source
        # Training history on top of a released checkpoint (v0.2.106):
        # ``base_weights_version`` = the released version this network
        # started from ("" = a fresh initialisation, or a checkpoint older
        # than this field); ``local_train_steps`` = gradient steps applied
        # on THIS machine since then. The server's startup choice between a
        # local checkpoint and the bundled one reads both
        # (``rl_server.choose_local_checkpoint``).
        self._base_weights_version = ""
        self._local_train_steps = 0
        self._state_path = Path(state_path)
        self._pt_path = self._state_path.with_suffix(".pt")

        self._type_registry: dict[str, int] = {t: i for i, t in enumerate(NODE_TYPES)}
        self._new_type_messages: list[str] = []
        self._next_type_slot: int = N_ENTITY_TYPES

        if arch_version == "v1":
            from .rl_model_v1 import _RLNetV1
            self._net: nn.Module = _RLNetV1(emb_dim)
        else:
            # All v2-family variants share the _RLNet class; differ only in
            # the cos3heads gate + per-track activation choices.
            cfg = _ARCH_CONFIG[arch_version]
            self._net = _RLNet(
                emb_dim,
                use_cos3heads=cfg["use_cos3heads"],
                act_a=cfg["act_a"],
                act_b=cfg["act_b"],
                act_c=cfg["act_c"],
                act_cos3=cfg["act_cos3"],
                act_shared=cfg["act_shared"],
            )

        # Move net to chosen device BEFORE building the optimizer; AdamW
        # creates parameter-state tensors on the parameter's current device.
        self._net = self._net.to(self._device)
        self._opt = torch.optim.AdamW(
            self._net.parameters(), lr=LR_MODEL, weight_decay=WEIGHT_DECAY
        )

        if not self._try_load():
            self._init_weights()

    # ------------------------------------------------------------------
    # Properties (read-only)
    # ------------------------------------------------------------------

    @property
    def arch_version(self) -> str:
        return self._arch_version

    @property
    def weights_version(self) -> str:
        """v0.2.7+ — semver label of the trained checkpoint, empty if unknown."""
        return self._weights_version

    @property
    def embedding_source(self) -> str:
        """v0.2.7+ — embedder this network was trained against, empty if unknown."""
        return self._embedding_source

    @property
    def base_weights_version(self) -> str:
        """The released version this network started from ("" = none known)."""
        return self._base_weights_version

    @property
    def local_train_steps(self) -> int:
        """Gradient steps applied locally on top of :attr:`base_weights_version`."""
        return self._local_train_steps

    def mark_as_release_base(self) -> None:
        """Stamp this network as the release ``weights_version`` itself.

        Used when a release checkpoint is produced: its base is its own
        version and it carries no local training on top.
        """
        self._base_weights_version = self._weights_version
        self._local_train_steps = 0

    def record_local_train_steps(self, n: int) -> None:
        """Count ``n`` gradient steps applied outside :meth:`update`."""
        if n > 0:
            self._local_train_steps += int(n)

    @property
    def emb_dim(self) -> int:
        return self._emb_dim

    @property
    def device(self) -> str:
        """torch.device string the underlying _net is on (e.g. 'cuda:0' / 'cpu')."""
        return str(self._device)

    @staticmethod
    def _resolve_device(device: str) -> "torch.device":
        """Resolve a device spec to a concrete torch.device.

        Accepts:
            "auto"  → cuda:0 if torch.cuda.is_available() else cpu
            "cuda"  → cuda:0 (or first available cuda device)
            "cuda:N" → that specific GPU
            "cpu"    → cpu
            anything else → cpu (with a warning)

        Production-safe: never crashes. A user pointing at "cuda" on a
        CPU-only machine gets CPU with a log line rather than a hard error.
        """
        if not _HAS_TORCH:
            return None  # type: ignore[return-value]
        spec = (device or "auto").strip().lower()
        if spec == "auto":
            return torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
        if spec.startswith("cuda"):
            if not torch.cuda.is_available():
                logger.warning(
                    "RLModel: device=%r requested but CUDA not available; falling back to CPU",
                    device,
                )
                return torch.device("cpu")
            return torch.device(spec)
        if spec == "cpu":
            return torch.device("cpu")
        logger.warning(
            "RLModel: unrecognised device=%r; falling back to CPU", device,
        )
        return torch.device("cpu")

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def score(
        self,
        q_raw: "np.ndarray",
        n_raw: "np.ndarray",
        linked_raws: "list[np.ndarray]",
        n_type_idx: int,
        linked_type_idxs: "list[int] | None" = None,
    ) -> float:
        """Compute relevance score in [0, 1]. No gradient."""
        entities_t, type_idxs_t, slot_scalars_t = self._build_inputs(
            q_raw, n_raw, linked_raws, n_type_idx,
            linked_type_idxs=linked_type_idxs,
        )
        self._net.eval()
        with torch.no_grad():
            out = self._net(entities_t, type_idxs_t, slot_scalars_t)
        return float(out[0, 0])

    def score_batch(
        self,
        q_raw: "np.ndarray",
        candidates: "list[tuple[np.ndarray, list[np.ndarray], int, list[int] | None]]",
    ) -> "list[float]":
        """Score multiple candidates against the same query. No gradient.

        More efficient than calling score() in a loop because it batches all
        candidates into a single forward pass.
        """
        if not candidates:
            return []
        all_entities = []
        all_type_idxs = []
        all_slot_scalars = []
        for n_raw, linked_raws, n_type_idx, linked_type_idxs in candidates:
            e, t, s = self._build_inputs(
                q_raw, n_raw, linked_raws, n_type_idx,
                linked_type_idxs=linked_type_idxs,
            )
            all_entities.append(e)
            all_type_idxs.append(t)
            all_slot_scalars.append(s)

        entities_batch = torch.cat(all_entities, dim=0)
        type_idxs_batch = torch.cat(all_type_idxs, dim=0)
        scalars_batch = torch.cat(all_slot_scalars, dim=0)

        self._net.eval()
        with torch.no_grad():
            out = self._net(entities_batch, type_idxs_batch, scalars_batch)
        return [float(out[i, 0]) for i in range(out.shape[0])]

    def rerank_candidates(
        self,
        q_raw: "np.ndarray",
        candidates: "list[tuple[np.ndarray, list[np.ndarray], int, list[int] | None, dict]]",
        threshold: float = 0.0,
    ) -> "list[tuple[float, dict]]":
        """Score candidates and return sorted (score, metadata) pairs descending."""
        if not candidates:
            return []
        batch_input = [
            (n_raw, linked_raws, n_type_idx, linked_type_idxs)
            for n_raw, linked_raws, n_type_idx, linked_type_idxs, _ in candidates
        ]
        scores = self.score_batch(q_raw, batch_input)
        scored = [
            (sc, candidates[i][4])
            for i, sc in enumerate(scores)
            if sc >= threshold
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Training
    #
    # v0.2.106: the per-node online step (``update``: one BCE step per node,
    # lr 1e-3) is GONE. Online training is per project, in rounds, with the
    # offline objective (``objective.batch_loss``: query-grouped batches,
    # pairs within a query) — ``online_learning.ProjectLearner``, which steps
    # this model's ``_net`` / ``_opt`` and records the steps through
    # :meth:`record_local_train_steps`. One objective, offline and online.
    # ------------------------------------------------------------------

    def retarget(self, state_path: Path) -> None:
        """Save to ``state_path`` from now on (the network is unchanged).

        A project that starts from the base checkpoint loads it from the base
        path, then trains and saves under its own state dir — never over the
        base file other projects start from. Dynamic-type sidecars follow the
        new location on the next :meth:`save`.
        """
        self._state_path = Path(state_path)
        self._pt_path = self._state_path.with_suffix(".pt")

    # ------------------------------------------------------------------
    # Type registry
    # ------------------------------------------------------------------

    def get_type_idx(self, node_type: str) -> int:
        """Return type-embedding index for node_type; auto-register new types."""
        if not node_type:
            return UNKNOWN_TYPE_IDX
        idx = self._type_registry.get(node_type)
        if idx is None:
            idx = self._register_new_type(node_type)
        return idx

    def pop_new_type_messages(self) -> list[str]:
        msgs = list(self._new_type_messages)
        self._new_type_messages.clear()
        return msgs

    def _register_new_type(self, node_type: str) -> int:
        if self._next_type_slot >= _MAX_TYPES:
            logger.warning(
                "RLModel: type slot limit (%d) reached; mapping '%s' to UNKNOWN_TYPE_IDX",
                _MAX_TYPES, node_type,
            )
            return UNKNOWN_TYPE_IDX
        new_idx = self._next_type_slot
        self._next_type_slot += 1
        with torch.no_grad():
            builtin_mean = self._net.type_embs.weight[:len(NODE_TYPES)].mean(0)
            self._net.type_embs.weight[new_idx] = builtin_mean
        self._type_registry[node_type] = new_idx
        msg = (
            f"[RLModel] New node type auto-registered: '{node_type}' -> index {new_idx}. "
            f"Embedding initialized from mean of {len(NODE_TYPES)} builtin types. "
            f"Add '{node_type}' to NODE_TYPES in rl_model.py to make it permanent."
        )
        self._new_type_messages.append(msg)
        logger.info("RLModel: auto-registered type '%s' at slot %d", node_type, new_idx)
        # Persist immediately. Pre-D1' the registry only survived if the
        # caller eventually triggered a checkpoint elsewhere; if the
        # container crashed between register and next save(), the type
        # was lost. Under D1' we write just the one sidecar (cheap —
        # 6-float tensor + name) so the type is durable from creation.
        # Soft-fail: if the write errors (e.g. read-only state dir), the
        # type stays in-memory and the next full save() will retry.
        try:
            with torch.no_grad():
                row = self._net.type_embs.weight[new_idx].detach().cpu().clone()
            self._write_sidecar(node_type, row)
        except Exception as exc:
            logger.warning(
                "RLModel: save-on-register failed for '%s' (will retry on next save()): %s",
                node_type, exc,
            )
        return new_idx

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # D1' sidecar helpers (v0.2.2+).
    #
    # Dynamic-type rows of type_embs.weight (idx >= N_ENTITY_TYPES) live in
    # per-name sidecar files alongside the network .pt. Lets /rotate_weights
    # swap the network checkpoint without clobbering user-registered types,
    # and removes the row-index collision bug between independent users.
    # ------------------------------------------------------------------

    @property
    def _sidecar_dir(self) -> Path:
        return self._pt_path.parent / SIDECAR_DIR_NAME

    @property
    def _layout_marker_path(self) -> Path:
        return self._pt_path.parent / SIDECAR_MARKER_NAME

    @staticmethod
    def _sidecar_filename(name: str) -> str:
        """Compute the on-disk filename for a dynamic-type sidecar.

        Format: ``<sanitized>__<8-hex>.pt`` where sanitized strips chars
        outside ``[A-Za-z0-9_-]`` (replaced with ``_``) and the hex suffix
        is the first 8 hex chars of blake2b(name) (collision-resistant).

        The hash suffix is mandatory: two distinct names that sanitize
        identically (``"a/b"`` and ``"a-b"`` both → ``"a_b"``) must map
        to distinct filenames. Storing the unsanitized name *inside* the
        sidecar dict means the filename is just an addressing key — the
        canonical name comes from the file content at load time.
        """
        if not name:
            # Defensive — registry never holds empty strings, but if a
            # malformed entry slipped through we still produce a stable
            # filename rather than crash.
            name = "_empty"
        sanitized = _SIDECAR_SAFE_CHARS.sub("_", name)
        # Cap sanitized prefix length to keep filenames bounded on
        # filesystems with 255-char NAME_MAX (NTFS, ext4). 200 leaves room
        # for the __<hash>.pt suffix.
        if len(sanitized) > 200:
            sanitized = sanitized[:200]
        h = hashlib.blake2b(name.encode("utf-8"), digest_size=4).hexdigest()
        return f"{sanitized}__{h}.pt"

    def _write_sidecar(self, name: str, emb: "torch.Tensor") -> None:
        """Persist one dynamic-type embedding to a sidecar file.

        Tempfile + os.replace per file for crash-safety: if the process is
        killed mid-write, the destination either remains the prior valid
        sidecar or doesn't exist (never a partially-written .pt).
        """
        self._sidecar_dir.mkdir(parents=True, exist_ok=True)
        fname = self._sidecar_filename(name)
        final_path = self._sidecar_dir / fname
        payload = {
            "name": name,
            "emb": emb.detach().cpu().to(torch.float32),
            "version": SIDECAR_FORMAT_VERSION,
        }
        _atomic_torch_save(payload, final_path)

    def _write_layout_marker(self) -> None:
        """Drop the .types_layout_v2 marker so future loads skip migration.

        The marker carries a tiny JSON payload (format version + tag) for
        forensics. Its mere presence is what gates migration — content is
        informational only.
        """
        marker = self._layout_marker_path
        marker.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"layout": "v2", "version": SIDECAR_FORMAT_VERSION})
        atomic_write_text(marker, body)

    def _load_sidecars(self) -> int:
        """Merge dynamic-type sidecars into the in-memory model.

        Enumerates ``<state_dir>/type_embs/*.pt`` sorted by filename for
        deterministic row allocation, loads each as a (name, emb) pair,
        copies the embedding into ``type_embs.weight[row]`` and records
        the mapping in ``self._type_registry``.

        Returns the number of sidecars successfully merged.

        Robustness: malformed / unreadable sidecars are logged and
        skipped — they should never block a model load.
        """
        if not self._sidecar_dir.is_dir():
            # Path missing OR path-exists-but-is-not-a-directory (corrupted
            # state, user-misconfigured bind mount). Treat the same as
            # absent — user can rename/repair the path manually if they
            # care. Log when the path exists but is wrong-type so the
            # failure mode is visible in container logs.
            if self._sidecar_dir.exists():
                logger.warning(
                    "RLModel: %s exists but is not a directory — skipping "
                    "sidecar load; rename or remove it to use D1' dynamic types",
                    self._sidecar_dir,
                )
            return 0

        # Sorted by filename → deterministic ordering. Row indices are
        # process-local under D1', so two machines may assign different
        # rows to the same name; that's a deliberate design choice (it
        # decouples the on-disk layout from positional invariants).
        sidecars = sorted(p for p in self._sidecar_dir.iterdir()
                          if p.is_file() and p.suffix == ".pt")

        next_row = N_ENTITY_TYPES
        merged = 0
        for path in sidecars:
            if self._next_type_slot >= _MAX_TYPES:
                logger.warning(
                    "RLModel: sidecar load hit _MAX_TYPES cap at %d; "
                    "remaining sidecars ignored",
                    _MAX_TYPES,
                )
                break
            try:
                payload = load_checkpoint(path)
            except Exception as exc:
                logger.warning(
                    "RLModel: failed to read sidecar %s: %s — skipping",
                    log_safe(path.name), log_safe(exc),
                )
                continue
            name = payload.get("name") if isinstance(payload, dict) else None
            emb = payload.get("emb") if isinstance(payload, dict) else None
            if not isinstance(name, str) or not name:
                logger.warning(
                    "RLModel: sidecar %s missing 'name' — skipping", log_safe(path.name),
                )
                continue
            if emb is None or not hasattr(emb, "shape"):
                logger.warning(
                    "RLModel: sidecar %s missing 'emb' tensor — skipping",
                    log_safe(path.name),
                )
                continue
            if tuple(emb.shape) != (TYPE_EMB_DIM,):
                logger.warning(
                    "RLModel: sidecar %s has wrong emb shape %s (expected (%d,)) — skipping",
                    log_safe(path.name), tuple(emb.shape), TYPE_EMB_DIM,
                )
                continue
            # Sidecars are user-local state (bind-mounted from
            # {VCT_DATA}/vct-rl-reranker/{project_slug}/state/), inside the
            # container's trust boundary. We don't audit byte-level
            # provenance, but reject corrupted payloads that would poison
            # inference: NaN / Inf in the embedding would propagate through
            # type_embs.weight into every forward pass.
            if not bool(torch.isfinite(emb).all()):
                logger.warning(
                    "RLModel: sidecar %s contains non-finite values — skipping",
                    log_safe(path.name),
                )
                continue
            if name in self._type_registry and name not in NODE_TYPES:
                # Duplicate dynamic-type sidecar (shouldn't happen via
                # _write_sidecar's hash-suffix, but a manually-edited dir
                # might produce this). Skip — first one wins.
                logger.warning(
                    "RLModel: sidecar %s would duplicate '%s' (already at row %d) — skipping",
                    log_safe(path.name), log_safe(name), self._type_registry[name],
                )
                continue
            row = next_row
            with torch.no_grad():
                target = self._net.type_embs.weight[row]
                target.copy_(emb.to(target.device, dtype=target.dtype))
            self._type_registry[name] = row
            next_row += 1
            self._next_type_slot = next_row
            merged += 1

        return merged

    # ------------------------------------------------------------------
    # Save / load (D1' format).
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Atomically save weights + optimizer state + arch metadata.

        Under D1' (v0.2.2+), the on-disk layout splits into:

          <state_dir>/rl_model_<tag>.pt
              Network state_dict (with ``type_embs.weight`` truncated to
              rows 0..N_ENTITY_TYPES-1), optimizer state, emb_dim,
              arch_version, builtin-only type_registry, fixed
              next_type_slot=N_ENTITY_TYPES.

          <state_dir>/type_embs/<sanitized>__<hash>.pt   (one per dynamic type)
              ``{"name": <unsanitized>, "emb": Tensor(6,), "version": 1}``.

          <state_dir>/.types_layout_v2
              Marker file dropped on first successful split-save so
              subsequent loads skip the v0.2.1→v0.2.2 migration.

        State dicts are moved to CPU before pickling so checkpoints stay
        portable across machines. Sidecar writes use tempfile + os.replace
        per file for crash-safety.

        Actual save order is **network .pt → sidecars → marker**:
          1. Truncated network .pt is written first (atomic tmp + rename).
          2. Each dynamic-type sidecar is written (atomic tmp + rename).
          3. The marker file is dropped LAST so a process killed at any
             prior step lands in case (C) of _try_load on next start
             (marker absent → check for sidecars → drop marker once state
             is consistent). Idempotent under name keys; re-saves are safe.
        """
        try:
            self._pt_path.parent.mkdir(parents=True, exist_ok=True)

            # --- Build truncated state_dict (builtin rows only) ---
            #
            # We clone type_embs.weight, truncate to rows 0..N_ENTITY_TYPES-1,
            # then swap it back into a copy of state_dict so the original
            # in-memory model is unaffected. Truncation happens BEFORE the
            # detach().cpu() pass so the truncated tensor is what gets
            # pickled.
            full_state = self._net.state_dict()
            truncated_state = {}
            for k, v in full_state.items():
                if k == "type_embs.weight":
                    truncated_state[k] = v[:N_ENTITY_TYPES].detach().cpu().clone()
                else:
                    truncated_state[k] = v.detach().cpu()

            builtin_registry = {
                name: idx for name, idx in self._type_registry.items()
                if idx < N_ENTITY_TYPES
            }

            _atomic_torch_save(
                {
                    "state_dict": truncated_state,
                    "opt_state": self._opt.state_dict(),
                    "emb_dim": self._emb_dim,
                    "arch_version": self._arch_version,
                    # v0.2.7 (2026-05-24): weights_version + embedding_source
                    # additions. Empty string = "unknown" for legacy
                    # checkpoints (forward-compatible: _try_load below treats
                    # missing keys as empty).
                    "weights_version": self._weights_version,
                    "embedding_source": self._embedding_source,
                    # v0.2.106: training history on top of a release (see
                    # __init__). Absent in older checkpoints -> "" / 0.
                    "base_weights_version": self._base_weights_version,
                    "local_train_steps": int(self._local_train_steps),
                    "type_registry": builtin_registry,
                    "next_type_slot": N_ENTITY_TYPES,
                    "types_layout": "v2",  # forensic tag
                },
                self._pt_path,
            )

            # --- Write each dynamic-type row as a sidecar ---
            # Track the filenames we WRITE so the GC step below can prune
            # orphans (sidecars on disk whose name is no longer in the
            # registry — happens if a future "forget unused types" feature
            # ever lands, or if a stale sidecar was manually placed there).
            written_filenames: set[str] = set()
            for name, idx in self._type_registry.items():
                if idx < N_ENTITY_TYPES:
                    continue  # builtin → already inside the .pt
                with torch.no_grad():
                    row = self._net.type_embs.weight[idx].detach().cpu().clone()
                try:
                    self._write_sidecar(name, row)
                    written_filenames.add(self._sidecar_filename(name))
                except Exception as exc:
                    # A single sidecar failing is a soft failure; log and
                    # continue. The network .pt is already on disk so
                    # critical state is durable.
                    logger.warning(
                        "RLModel.save: sidecar write for '%s' failed: %s",
                        log_safe(name), log_safe(exc),
                    )

            # --- Orphan sidecar GC ---
            # Sidecar dir contents are authoritatively reconciled to the
            # in-memory registry on every save(). Any .pt file we didn't
            # just write (and isn't a .tmp staging file) is unreachable
            # and should be pruned. Logged at INFO so the deletion is
            # auditable from container logs.
            if self._sidecar_dir.is_dir():
                for path in self._sidecar_dir.iterdir():
                    if not (path.is_file() and path.suffix == ".pt"):
                        continue
                    if path.name in written_filenames:
                        continue
                    try:
                        path.unlink()
                        logger.info(
                            "RLModel.save: pruned orphan sidecar %s "
                            "(not in registry)", log_safe(path.name),
                        )
                    except OSError as exc:
                        logger.warning(
                            "RLModel.save: failed to prune orphan sidecar %s: %s",
                            log_safe(path.name), exc,
                        )

            # --- Drop the layout marker LAST ---
            #
            # Ordering matters: if we crashed before this point on a
            # legacy v0.2.1 checkpoint upgrade, the next load sees no
            # marker + a builtin-only .pt + sidecars in place → it just
            # re-writes the marker (idempotent, no embeddings lost).
            try:
                self._write_layout_marker()
            except Exception as exc:
                logger.warning("RLModel.save: marker write failed: %s", exc)

        except Exception as exc:
            logger.warning("RLModel.save failed: %s", exc)

    @classmethod
    def load_or_create(
        cls,
        state_path: Path,
        emb_dim: int = 1024,
        arch_version: str = "v2",
        device: str = "auto",
        weights_version: str = "",
        embedding_source: str = "",
    ) -> "RLModel":
        return cls(
            emb_dim=emb_dim,
            state_path=state_path,
            arch_version=arch_version,
            device=device,
            weights_version=weights_version,
            embedding_source=embedding_source,
        )

    def _try_load(self) -> bool:
        if not self._pt_path.exists():
            return False
        try:
            ckpt = load_checkpoint(self._pt_path)
        except Exception as exc:
            logger.warning("RLModel: failed to read .pt: %s", exc)
            return False

        ckpt_arch = ckpt.get("arch_version", "v1")  # checkpoints without the field are v1
        if ckpt_arch != self._arch_version:
            logger.warning(
                "RLModel: checkpoint arch %s does not match expected %s — "
                "ignoring checkpoint and re-initialising weights.",
                log_safe(ckpt_arch), self._arch_version,
            )
            return False

        if ckpt.get("emb_dim") != self._emb_dim:
            logger.warning(
                "RLModel: emb_dim mismatch (%s vs %d), re-initialising",
                log_safe(ckpt.get("emb_dim")), self._emb_dim,
            )
            return False

        # v0.2.7 — cross-source weight-sharing guard (R4).
        #
        # Reject a checkpoint trained on a DIFFERENT embedding source than
        # what the container is currently pinned to. Same dim but different
        # latent space → silently-garbage scores if we load it. The check
        # is strict when BOTH sides have an explicit embedding_source;
        # permissive (warn-only) when either side is empty string (= legacy
        # checkpoint OR test/standalone construction with no source set).
        ckpt_source = ckpt.get("embedding_source", "")
        expected_source = self._embedding_source
        if ckpt_source and expected_source and ckpt_source != expected_source:
            logger.warning(
                "RLModel: embedding_source mismatch — checkpoint trained on %r, "
                "container pinned to %r. Refusing load (would silently produce "
                "garbage scores on the wrong latent space). Re-initialising.",
                log_safe(ckpt_source), log_safe(expected_source),
            )
            return False
        elif (ckpt_source or expected_source) and not (ckpt_source and expected_source):
            # One side missing — log INFO so an operator can spot the
            # permissive case in audit trails, but proceed.
            logger.info(
                "RLModel: embedding_source partial (ckpt=%r, expected=%r); "
                "permissive load. Future v0.2.7+ checkpoints will populate "
                "both fields and enable strict checking.",
                log_safe(ckpt_source), log_safe(expected_source),
            )

        # v0.2.7 — capture weights_version from the checkpoint for downstream
        # reporting (dashboard widget reads via rl_weights_state.weights_version).
        # Empty string = unknown (legacy / pre-v0.2.7 checkpoint).
        self._weights_version = ckpt.get("weights_version", self._weights_version)
        self._base_weights_version = str(ckpt.get("base_weights_version", "") or "")
        try:
            self._local_train_steps = max(0, int(ckpt.get("local_train_steps", 0) or 0))
        except (TypeError, ValueError):
            self._local_train_steps = 0

        # D1' (v0.2.2+) truncates type_embs.weight to N_ENTITY_TYPES rows
        # before pickling, but the in-memory nn.Embedding stays sized
        # (_MAX_TYPES, TYPE_EMB_DIM). Detect that by inspecting the
        # incoming weight shape and route around load_state_dict's strict
        # shape check.
        ckpt_state = ckpt["state_dict"]
        ckpt_type_emb = ckpt_state.get("type_embs.weight")
        truncated_format = (
            ckpt_type_emb is not None
            and tuple(ckpt_type_emb.shape) == (N_ENTITY_TYPES, TYPE_EMB_DIM)
        )
        if truncated_format:
            # Load everything EXCEPT type_embs.weight via load_state_dict,
            # then merge the truncated rows manually into the existing
            # (_MAX_TYPES, TYPE_EMB_DIM) embedding tensor.
            partial_state = {k: v for k, v in ckpt_state.items() if k != "type_embs.weight"}
            try:
                missing, unexpected = self._net.load_state_dict(partial_state, strict=False)
            except Exception as exc:
                logger.warning("RLModel: state_dict load failed: %s", exc)
                return False
            # `missing` should contain exactly {"type_embs.weight"}.
            # `unexpected` should be empty. Anything else is a real
            # schema mismatch and worth surfacing.
            unexpected_real = list(unexpected or [])
            missing_real = [k for k in (missing or []) if k != "type_embs.weight"]
            if unexpected_real or missing_real:
                logger.warning(
                    "RLModel: partial load reports missing=%s unexpected=%s",
                    log_safe(missing_real), log_safe(unexpected_real),
                )
            with torch.no_grad():
                target = self._net.type_embs.weight[:N_ENTITY_TYPES]
                target.copy_(ckpt_type_emb.to(target.device, dtype=target.dtype))
        else:
            try:
                self._net.load_state_dict(ckpt_state)
            except Exception as exc:
                logger.warning("RLModel: state_dict load failed: %s", exc)
                return False
        try:
            self._opt.load_state_dict(ckpt["opt_state"])
        except Exception:
            pass  # optimizer-state shape changes are tolerable; fresh optimiser is fine.

        self._type_registry = dict(ckpt.get("type_registry", {}))
        if not self._type_registry:
            self._type_registry = {t: i for i, t in enumerate(NODE_TYPES)}
        self._next_type_slot = max(
            ckpt.get("next_type_slot", N_ENTITY_TYPES), N_ENTITY_TYPES
        )

        # ----------------------------------------------------------------
        # D1' layout resolution.
        #
        # Three possible states for an on-disk checkpoint at this point:
        #
        # (A) New-format (post-v0.2.2): marker exists, .pt registry
        #     contains only builtins (idx < N_ENTITY_TYPES). We just
        #     merge sidecars into rows 9+.
        #
        # (B) Legacy-format (v0.2.0 / v0.2.1): marker absent, .pt
        #     registry may contain dynamic-type entries (idx >=
        #     N_ENTITY_TYPES) baked into type_embs.weight[9..]. We need
        #     to MIGRATE: extract those rows → sidecars → rewrite the .pt
        #     with builtin-only registry + truncated type_embs → drop the
        #     marker. The migration must be IDEMPOTENT (crash partway →
        #     retry) and CRASH-SAFE (no data loss).
        #
        # (C) Mid-migration recovery: marker absent, .pt is ALREADY in
        #     post-migration form (builtin-only registry, type_embs[:9]),
        #     AND sidecars exist on disk. This happens when an earlier
        #     save() wrote sidecars + rewrote .pt but crashed before the
        #     marker. We just need to drop the marker (sidecars are
        #     already authoritative).
        # ----------------------------------------------------------------

        marker_present = self._layout_marker_path.exists()
        has_dynamic_in_pt = any(
            idx >= N_ENTITY_TYPES for idx in self._type_registry.values()
        )

        if marker_present and has_dynamic_in_pt:
            # Defense-in-depth: a rotation that drops a legacy-format .pt
            # over an existing v0.2.2 state dir (marker present, sidecars
            # populated) would otherwise collide — _load_sidecars allocates
            # starting at row N_ENTITY_TYPES, which load_state_dict above
            # already filled from the legacy .pt's dynamic entries.
            #
            # Sidecars are the authoritative source for user-local dynamic
            # types under D1'. Drop the legacy .pt's stale dynamic entries
            # from the registry — they will be discarded entirely when the
            # truncated rows above were copied (i.e. the rotated .pt was
            # truncated-format) OR re-saved as builtin-only on next save()
            # (if the rotated .pt was legacy-format with rows 9..255
            # populated in type_embs.weight — those rows are overwritten by
            # _load_sidecars below). Either way, the user's sidecars win.
            stale_names = [
                name for name, idx in self._type_registry.items()
                if idx >= N_ENTITY_TYPES
            ]
            logger.warning(
                "RLModel: rotation onto legacy-format .pt detected "
                "(marker present + .pt registry contains %d dynamic entry/ies: %s); "
                "discarding shipped registry entries and resyncing from sidecars. "
                "Build-pipeline note: shipped weights for v0.2.2+ MUST be produced "
                "via RLModel.save() so type_embs.weight ships truncated to %d rows.",
                len(stale_names), log_safe(stale_names), N_ENTITY_TYPES,
            )
            for name in stale_names:
                del self._type_registry[name]
            self._next_type_slot = N_ENTITY_TYPES
            merged = self._load_sidecars()
            if merged > 0:
                logger.debug(
                    "RLModel: merged %d dynamic-type sidecar(s) from %s",
                    merged, self._sidecar_dir,
                )
        elif not marker_present and has_dynamic_in_pt:
            # Case (B) — legacy upgrade required.
            try:
                self._migrate_legacy_to_sidecars(ckpt)
            except Exception as exc:
                logger.warning(
                    "RLModel: legacy → sidecar migration failed: %s — "
                    "leaving registry as-is; types live only in memory until next save",
                    exc,
                )
        else:
            # Cases (A) and (C) — merge any sidecars present on disk.
            merged = self._load_sidecars()
            if merged > 0:
                logger.debug(
                    "RLModel: merged %d dynamic-type sidecar(s) from %s",
                    merged, self._sidecar_dir,
                )
            if not marker_present and not has_dynamic_in_pt:
                # Case (C) — sidecars (if any) are in place; just drop
                # the marker so future loads stop checking.
                try:
                    self._write_layout_marker()
                except Exception as exc:
                    logger.warning(
                        "RLModel: marker post-load write failed: %s", exc,
                    )

        # _sanitize_registry guards against legacy registries that stored
        # a non-builtin name at a builtin row. Under D1' (marker present),
        # this case can't arise — sidecars allocate fresh rows ≥ N_ENTITY_TYPES.
        # Skip the scan on the happy path to keep load fast and avoid
        # touching a known-good registry. Always run when crossing the
        # migration boundary (marker absent at function entry; we may have
        # just written one in case-(C) repair, but _sanitize_registry is
        # cheap and the safeguard is intentional belt-and-suspenders during
        # the legacy → D1' window.)
        if not marker_present:
            self._sanitize_registry()
        logger.debug("RLModel: loaded %s checkpoint from %s", log_safe(ckpt_arch), log_safe(self._pt_path))
        return True

    def _migrate_legacy_to_sidecars(self, ckpt: dict) -> None:
        """Migrate a v0.2.0/v0.2.1 single-file checkpoint to D1' layout.

        Crash-safety contract: this function may be killed at any point
        between sidecar writes and the marker write; the next ``_try_load``
        must complete the migration cleanly without losing embeddings.
        That contract is enforced by:

          1. Sidecars are written FIRST (each via tempfile + os.replace).
          2. The network .pt is rewritten SECOND with builtin-only registry
             and ``type_embs.weight[:N_ENTITY_TYPES]``. After this step,
             the dynamic-type rows live ONLY in sidecars.
          3. The marker is written LAST. If we crash between #2 and #3,
             the next load sees case (C) (no marker + .pt has only
             builtins + sidecars present) and just writes the marker.

        Idempotency: running this twice on the same input is safe — the
        sidecar filenames are deterministic functions of the type names,
        so the second pass overwrites the same files with the same
        content, and the .pt rewrite has no dynamic types left to drop.
        """
        dynamic_entries = [
            (name, idx) for name, idx in self._type_registry.items()
            if idx >= N_ENTITY_TYPES
        ]
        if not dynamic_entries:
            return

        logger.info(
            "RLModel: migrating %d dynamic type(s) from legacy in-pt format "
            "to sidecar files under %s",
            len(dynamic_entries), self._sidecar_dir,
        )

        # Step 1 — write all sidecars BEFORE touching the .pt. Each row
        # is read from the live network (which has already loaded the
        # legacy state_dict, so type_embs.weight[idx] is authoritative).
        for name, idx in dynamic_entries:
            if idx >= _MAX_TYPES:
                logger.warning(
                    "RLModel: legacy entry '%s' has out-of-range idx %d; skipping",
                    log_safe(name), idx,
                )
                continue
            with torch.no_grad():
                row = self._net.type_embs.weight[idx].detach().cpu().clone()
            self._write_sidecar(name, row)

        # Step 2 — rewrite the .pt with builtin-only registry. We use
        # save() rather than open-coded logic so this stays in sync with
        # the canonical D1' save format. To avoid recursion (save() now
        # also writes sidecars + marker, which is exactly what we want
        # but it'd write the marker too early if dynamic_entries also
        # holds rows we haven't merged into the registry yet — they ARE
        # in the registry already, this just works).
        #
        # NB: save() will also re-write the same sidecars we just wrote.
        # That's the expected cost of consolidating the format
        # (deterministic + idempotent), and the alternative (open-coding
        # a truncated .pt write here) would duplicate logic.
        self.save()

    def _sanitize_registry(self) -> None:
        """Remap dynamic types whose stored index clashes with a builtin sentinel.

        Under D1' (v0.2.2+) this case shouldn't arise on fresh loads
        because sidecars are assigned fresh rows starting at N_ENTITY_TYPES.
        We keep the safeguard for the migration path, where a legacy
        registry might (in theory) have stored a non-builtin name at a
        builtin row.
        """
        builtin_names = set(NODE_TYPES)
        for name in list(self._type_registry):
            if name in builtin_names:
                continue
            idx = self._type_registry[name]
            if idx < N_ENTITY_TYPES:
                new_idx = self._next_type_slot
                self._next_type_slot += 1
                self._type_registry[name] = new_idx
                logger.info(
                    "RLModel: registry conflict -- remapped '%s' from slot %d -> %d",
                    log_safe(name), idx, new_idx,
                )

    # ------------------------------------------------------------------
    # Weight init
    # ------------------------------------------------------------------

    def _init_weights(self) -> None:
        for m in self._net.modules():
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, nonlinearity="relu")
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.LayerNorm):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)
        nn.init.normal_(self._net.type_embs.weight, 0.0, 0.1)
        n_params = sum(p.numel() for p in self._net.parameters())
        logger.debug(
            "RLModel: fresh %s weights (emb_dim=%d params=%dk)",
            self._arch_version, self._emb_dim, n_params // 1000,
        )

    # ------------------------------------------------------------------
    # Input preparation
    # ------------------------------------------------------------------

    def _build_inputs(
        self,
        q_raw: "np.ndarray",
        n_raw: "np.ndarray",
        linked_raws: "list[np.ndarray]",
        n_type_idx: int,
        linked_type_idxs: "list[int] | None" = None,
    ) -> "tuple[torch.Tensor, torch.Tensor, torch.Tensor]":
        """Build (entities, type_idxs, slot_scalars) tensors.

        Per-slot cosines:
          Query slot:       (1.0,        1.0)        identity anchor
          Target slot:      (cos(q,n),   1.0)
          Linked slot:      (cos(q,lk),  cos(n,lk))
          NULL padding:     (0.0,        0.0)
        """
        cos_qn = float(_cosine_np(q_raw, n_raw))

        # Co-shuffle linked embeddings + their types together so the model can't
        # learn slot-position artefacts.
        l_types = list(linked_type_idxs) if linked_type_idxs else [UNKNOWN_TYPE_IDX] * len(linked_raws)
        pairs = list(zip(linked_raws, l_types))
        random.shuffle(pairs)
        shuffled_embs = [p[0] for p in pairs]
        shuffled_types = [p[1] for p in pairs]

        entity_list: list["np.ndarray"] = [q_raw, n_raw]
        type_idx_list: list[int] = [QUERY_TYPE_IDX, n_type_idx]
        slot_cos_list: list[tuple[float, float]] = [
            (1.0, 1.0),
            (cos_qn, 1.0),
        ]

        zeros = np.zeros(self._emb_dim, dtype=np.float32)
        for i in range(MAX_LINKED):
            if i < len(shuffled_embs):
                slot_emb = shuffled_embs[i].astype(np.float32)
                entity_list.append(slot_emb)
                type_idx_list.append(shuffled_types[i])
                slot_cos_list.append((
                    float(_cosine_np(q_raw, slot_emb)),
                    float(_cosine_np(n_raw, slot_emb)),
                ))
            else:
                entity_list.append(zeros)
                type_idx_list.append(NULL_TYPE_IDX)
                slot_cos_list.append((0.0, 0.0))

        entities_np = np.stack(entity_list, axis=0).astype(np.float32)  # (7, emb_dim)
        entities_t = torch.from_numpy(entities_np).unsqueeze(0)         # (1, 7, emb_dim)
        type_idxs_t = torch.tensor([type_idx_list], dtype=torch.long)   # (1, 7)
        slot_scalars_t = torch.tensor([slot_cos_list], dtype=torch.float32)  # (1, 7, 2)
        # Move to the model's device so forward/backward don't trip on
        # cross-device tensors (CUDA model + CPU inputs would raise).
        if self._device is not None and self._device.type != "cpu":
            entities_t = entities_t.to(self._device, non_blocking=True)
            type_idxs_t = type_idxs_t.to(self._device, non_blocking=True)
            slot_scalars_t = slot_scalars_t.to(self._device, non_blocking=True)
        return entities_t, type_idxs_t, slot_scalars_t
