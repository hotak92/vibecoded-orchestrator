# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Tests for the 2026-05-23 cos3heads bake-off architectures.

Covers the four new arch_version strings introduced in rl_model.py:

  - "v2_no_tx_24ep"          (alias of canonical v2; differs only at train-time)
  - "v2_cos3heads_all_gelu"  (cos3heads + Track C, GELU everywhere)
  - "v2_cos3heads_all_silu"  (cos3heads + Track C, SiLU everywhere)
  - "v2_cos3heads_mixed"     (cos3heads + Track C, mixed activations)

Each variant must:
  1. Construct cleanly at emb_dim=64 (lightweight CPU test).
  2. Run a forward pass returning a finite scalar in [0, 1] (sigmoid head).
  3. Round-trip through save() + load_or_create(): identical scores after reload.
  4. Refuse to load a checkpoint saved under a different arch_version
     (the _try_load arch-mismatch guard still fires).
  5. Have the expected parameter-count footprint:
       - v2_no_tx_24ep            : identical to v2
       - v2_cos3heads_* variants  : v2 + 6×head MLPs + Track C + first-Linear
                                    growth on Tracks A/B + head_1 input growth.

All tests force device="cpu" so they run on machines without CUDA.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

# Repo-root-as-package import (matches conftest.py setup).

torch = pytest.importorskip("torch")

from rl_reranker.rl_model import (  # noqa: E402
    RLModel,
    _ARCH_CONFIG,
    _get_activation,
)

# Small emb_dim keeps the model lightweight (~250k params for baseline v2 at
# this width) so the full variant matrix runs in a few seconds on CPU.
_EMB_DIM = 64

_BAKE_OFF_ARCHES = (
    "v2_no_tx_24ep",
    "v2_cos3heads_all_gelu",
    "v2_cos3heads_all_silu",
    "v2_cos3heads_mixed",
)

_COS3_ARCHES = (
    "v2_cos3heads_all_gelu",
    "v2_cos3heads_all_silu",
    "v2_cos3heads_mixed",
)


def _make_model(state_path: Path, arch_version: str) -> RLModel:
    """Construct a CPU-only RLModel at the given state_path for a given arch."""
    return RLModel.load_or_create(
        state_path=state_path,
        emb_dim=_EMB_DIM,
        arch_version=arch_version,
        device="cpu",
    )


def _dummy_inputs(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Generate one (q_raw, n_raw) pair for inference tests."""
    q = rng.standard_normal(_EMB_DIM).astype(np.float32)
    n = rng.standard_normal(_EMB_DIM).astype(np.float32)
    return q, n


# ---------------------------------------------------------------------------
# Activation helper
# ---------------------------------------------------------------------------


def test_get_activation_known_names():
    """The activation registry resolves the three names used in the bake-off."""
    assert isinstance(_get_activation("silu"), torch.nn.SiLU)
    assert isinstance(_get_activation("gelu"), torch.nn.GELU)
    assert isinstance(_get_activation("relu"), torch.nn.ReLU)
    # Case-insensitive
    assert isinstance(_get_activation("SiLU"), torch.nn.SiLU)
    assert isinstance(_get_activation("GELU"), torch.nn.GELU)


def test_get_activation_unknown_name_raises():
    with pytest.raises(ValueError, match="Unsupported activation"):
        _get_activation("mish")


# ---------------------------------------------------------------------------
# Construction + forward
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arch", _BAKE_OFF_ARCHES)
def test_construction(tmp_path: Path, arch: str):
    """Each bake-off arch constructs cleanly and reports its config."""
    model = _make_model(tmp_path / "ckpt.pt", arch)
    assert model.arch_version == arch
    assert model.emb_dim == _EMB_DIM
    # Activation config reflects the registry entry.
    cfg = _ARCH_CONFIG[arch]
    assert model._net._act_spec["a"] == cfg["act_a"]
    assert model._net._act_spec["b"] == cfg["act_b"]
    # cos3heads gate reflects the registry entry.
    assert bool(model._net._use_cos3heads) == cfg["use_cos3heads"]


@pytest.mark.parametrize("arch", _BAKE_OFF_ARCHES)
def test_forward_in_unit_interval(tmp_path: Path, arch: str):
    """Forward pass returns a finite scalar in [0, 1] (sigmoid head)."""
    torch.manual_seed(42)
    rng = np.random.default_rng(42)
    model = _make_model(tmp_path / "ckpt.pt", arch)
    q, n = _dummy_inputs(rng)
    score = model.score(q, n, [], 0, None)
    assert isinstance(score, float)
    assert np.isfinite(score)
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Round-trip save / load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arch", _BAKE_OFF_ARCHES)
def test_roundtrip_score_identity(tmp_path: Path, arch: str):
    """save() + load_or_create() preserves the model: identical scores."""
    torch.manual_seed(42)
    rng = np.random.default_rng(7)

    ckpt = tmp_path / "rl_model.pt"
    model = _make_model(ckpt, arch)
    q, n = _dummy_inputs(rng)
    score_before = model.score(q, n, [], 0, None)

    model.save()
    assert ckpt.exists()

    model_reloaded = _make_model(ckpt, arch)
    score_after = model_reloaded.score(q, n, [], 0, None)

    assert score_before == pytest.approx(score_after, abs=1e-6), (
        f"{arch}: round-trip score drift {score_before} → {score_after} "
        f"(diff {abs(score_before - score_after):.2e})"
    )


@pytest.mark.parametrize("arch", _COS3_ARCHES)
def test_arch_mismatch_refused(tmp_path: Path, arch: str):
    """A checkpoint saved under one arch cannot be loaded under a different one.

    The cos3heads variants change the network shape (Tracks A/B + head_1 grow,
    new cos3 + Track C submodules exist) — loading across arches must fall back
    to a fresh init rather than silently mis-aligning state_dict entries.
    """
    ckpt = tmp_path / "rl_model.pt"
    # Save under cos3heads arch
    m1 = _make_model(ckpt, arch)
    m1.save()

    # Try to load the same checkpoint under baseline v2 — should
    # refuse the load (arch_version mismatch) and re-init weights instead.
    # _try_load returns False in this branch, and _init_weights runs.
    m2 = _make_model(ckpt, "v2")
    # The model is functional but its weights are NOT the saved ones.
    # We check this by comparing a forward pass on the same input.
    torch.manual_seed(0)
    rng = np.random.default_rng(0)
    q, n = _dummy_inputs(rng)
    s1 = m1.score(q, n, [], 0, None)
    s2 = m2.score(q, n, [], 0, None)
    # The two scores almost certainly differ — fresh-init m2 has nothing to do
    # with the saved cos3heads checkpoint. We assert a finite-difference rather
    # than approx-equality (the converse of test_roundtrip_score_identity).
    assert abs(s1 - s2) > 1e-5, (
        f"{arch} → v2 load should have re-initialised weights, "
        f"but scores match: s1={s1} s2={s2}"
    )


# ---------------------------------------------------------------------------
# Parameter count footprint
# ---------------------------------------------------------------------------


def _param_count(model: RLModel) -> int:
    return sum(p.numel() for p in model._net.parameters())


def test_param_count_v2_equals_v2_cos3heads_all_silu(tmp_path: Path):
    """v0.2.6 promoted v2_cos3heads_all_silu to be THE 'v2' arch. The alias
    is preserved for ablation-result traceability; they share parameter count
    + forward output."""
    canonical = _make_model(tmp_path / "v2.pt", "v2")
    alias = _make_model(tmp_path / "alias.pt", "v2_cos3heads_all_silu")
    assert _param_count(canonical) == _param_count(alias)


def test_param_count_v2_no_tx_24ep_smaller_than_v2(tmp_path: Path):
    """v2_no_tx_24ep is the pre-cos3heads ablation baseline — smaller than
    the cos3heads-equipped 'v2' canonical."""
    base = _make_model(tmp_path / "base.pt", "v2_no_tx_24ep")
    canonical = _make_model(tmp_path / "v2.pt", "v2")
    delta = _param_count(canonical) - _param_count(base)
    assert delta > 0, (
        f"v2 (cos3heads-equipped) should have MORE params than v2_no_tx_24ep "
        f"(pre-cos3heads ablation baseline); got Δ={delta}"
    )


@pytest.mark.parametrize("arch", _COS3_ARCHES)
def test_param_count_cos3heads_growth(tmp_path: Path, arch: str):
    """All cos3heads variants add ≈10k params over the v2_no_tx_24ep
    pre-cos3heads baseline at emb_dim=64.

    Breakdown at emb_dim=64:
      - 6 projection-head MLPs: 6 × (64×8+8 + 8×8+8 + 8×8+8) = 6 × 656 = 3,936
      - Track C: 3×16+16 + 16×8+8 = 64 + 136 = 200
      - Track A first Linear gains 3×256 = 768 weights (biases unchanged)
      - Track B first Linear gains 3×384 = 1,152 weights (biases unchanged)
      - Head first Linear gains 11×384 = 4,224 weights (biases unchanged)
      ≈ +10,280 params at emb_dim=64. At emb_dim=1024 the projection-head
        block dominates (6×(1024×8+8 + 8×8+8 + 8×8+8) = 50,064) and total
        growth is ~56,400 params.

    Note: post-v0.2.6, "v2" itself is a cos3heads arch (alias of
    v2_cos3heads_all_silu). The baseline for the delta test is now
    v2_no_tx_24ep (the pre-cos3heads ablation).
    """
    base = _make_model(tmp_path / "base.pt", "v2_no_tx_24ep")
    cos3 = _make_model(tmp_path / "cos3.pt", arch)
    delta = _param_count(cos3) - _param_count(base)
    assert delta > 0, f"{arch} should add params over pre-cos3 baseline, got Δ={delta}"
    # Sanity bounds — exact count depends on biases, but it should be in the
    # 5k–20k range at emb_dim=64.
    assert 5000 < delta < 20000, (
        f"{arch} unexpected param growth at emb_dim=64: {delta} "
        f"(expected ~10k for the projection-head + Track C + head1 growth)"
    )


# ---------------------------------------------------------------------------
# Activation-config differentiation (sanity check: SiLU vs GELU variants
# produce different forward outputs from the same init seed).
# ---------------------------------------------------------------------------


def test_silu_vs_gelu_diverge(tmp_path: Path):
    """all_silu and all_gelu produce different scores from the same input.

    Both variants are constructed under torch.manual_seed(42), so their
    Linear weights are identical. The only difference is the activation
    function — and SiLU vs GELU should not be numerically identical even
    on a single forward pass.
    """
    rng = np.random.default_rng(123)
    q, n = _dummy_inputs(rng)

    torch.manual_seed(42)
    m_silu = _make_model(tmp_path / "silu.pt", "v2_cos3heads_all_silu")
    s_silu = m_silu.score(q, n, [], 0, None)

    torch.manual_seed(42)
    m_gelu = _make_model(tmp_path / "gelu.pt", "v2_cos3heads_all_gelu")
    s_gelu = m_gelu.score(q, n, [], 0, None)

    assert abs(s_silu - s_gelu) > 1e-5, (
        f"SiLU and GELU variants should diverge from same init; "
        f"got s_silu={s_silu}, s_gelu={s_gelu}"
    )
