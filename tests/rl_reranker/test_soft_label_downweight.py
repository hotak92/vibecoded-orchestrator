# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Soft-label down-weighting (2026-07-22) — the trainer must trust
``soft_label=true`` citation events LESS than hard terminal citations.

Contract (free-repo logger, v0.2.87 cycle): a citation fired via the
soft-terminal path (``fire_reason=soft_terminal``, ``soft_label=true``) comes
from a below-terminal-floor answer window — weaker evidence. The orchestrator
chat's guidance: "down-weight these in the loss, don't treat as hard
citations." This suite pins:

  * extract_samples threads ``sample_weight`` (SOFT_LABEL_WEIGHT for soft
    events, 1.0 for hard / legacy events lacking the field).
  * delta_mse_loss weighted path: all-ones == unweighted (bit-comparable);
    zero-weight removes a sample's pairs entirely.
  * _epoch: an all-hard batch takes the historical unweighted code path
    (weights=None), so pre-soft-label behaviour is bit-identical; a mixed
    batch trains with finite loss and a w=0 soft sample contributes nothing
    to the pointwise terms.
  * env tunable RL_SOFT_LABEL_WEIGHT: clamped, malformed → 0.5 default.
"""
from __future__ import annotations


import pytest

pytest.importorskip("numpy")
pytest.importorskip("torch")

import numpy as np  # noqa: E402
import torch  # noqa: E402


from rl_reranker.offline_trainer import (  # noqa: E402
    SOFT_LABEL_WEIGHT,
    SOFT_MIN_WINDOW_TOKENS,
    _resolve_soft_label_weight,
    _resolve_soft_min_window_tokens,
    delta_mse_loss,
    extract_samples,
)

EMB_DIM = 8


def _pair(soft_label: bool | None, window_tokens: int | None = 5000):
    """One (retrieval, citation) pair with a single scoreable node.

    ``window_tokens`` defaults comfortably ABOVE the soft floor so the
    weight-focused tests are not entangled with the floor gate; pass a small
    value (or None to omit the field) to exercise the floor."""
    ret = {
        "event": "retrieval",
        "schema_version": 3,
        "task_id": "t1",
        "query_emb": [0.1] * EMB_DIM,
        "nodes": [{"title": "A", "emb": [0.2] * EMB_DIM}],
    }
    cit = {
        "event": "citation",
        "schema_version": 3,
        "task_id": "t1",
        "cosine_sims": {"A": 0.9},
        "literal_cited": {"A": True},
    }
    if soft_label is not None:
        cit["soft_label"] = soft_label
    if window_tokens is not None:
        cit["window_tokens"] = window_tokens
    return (ret, cit)


class TestExtractSamplesWeight:
    def test_soft_citation_gets_downweight(self):
        samples = extract_samples([_pair(soft_label=True)], {})
        assert samples, "expected at least one sample"
        assert all(s["sample_weight"] == SOFT_LABEL_WEIGHT for s in samples)
        assert SOFT_LABEL_WEIGHT < 1.0

    def test_hard_citation_keeps_full_weight(self):
        samples = extract_samples([_pair(soft_label=False)], {})
        assert samples and all(s["sample_weight"] == 1.0 for s in samples)

    def test_legacy_event_without_field_is_hard(self):
        # Leave-alone case: 34k+ historical rows have no soft_label field —
        # they must keep weight 1.0 (no behaviour change on the old corpus).
        samples = extract_samples([_pair(soft_label=None)], {})
        assert samples and all(s["sample_weight"] == 1.0 for s in samples)


class TestDeltaMseWeighted:
    def test_all_ones_equals_unweighted(self):
        pred = torch.tensor([[0.2], [0.7], [0.5]])
        tgt = torch.tensor([[0.1], [0.9], [0.4]])
        w = torch.ones(3, 1)
        assert torch.isclose(
            delta_mse_loss(pred, tgt, weight=w), delta_mse_loss(pred, tgt)
        )

    def test_zero_weight_removes_samples_pairs(self):
        pred = torch.tensor([[0.2], [0.7], [0.5]])
        tgt = torch.tensor([[0.1], [0.9], [0.4]])
        # Zeroing sample 2's weight must reduce to the (0,1)-pair-only loss.
        w = torch.tensor([[1.0], [1.0], [0.0]])
        expected = delta_mse_loss(pred[:2], tgt[:2])
        assert torch.isclose(delta_mse_loss(pred, tgt, weight=w), expected)

    def test_all_zero_weights_returns_zero(self):
        pred = torch.tensor([[0.2], [0.7]])
        tgt = torch.tensor([[0.1], [0.9]])
        w = torch.zeros(2, 1)
        assert float(delta_mse_loss(pred, tgt, weight=w)) == 0.0


class TestEpochIntegration:
    def _run_epoch(self, samples):
        from rl_reranker.offline_trainer import _epoch
        from rl_reranker.rl_model import _RLNet

        net = _RLNet(emb_dim=EMB_DIM)
        opt = torch.optim.Adam(net.parameters(), lr=1e-3)
        return _epoch(
            net, samples, opt,
            batch_size=8, lambda_rank=0.5, training=True, emb_dim=EMB_DIM,
        )

    def test_mixed_batch_trains_finite(self):
        hard = extract_samples([_pair(soft_label=False)], {})
        soft = extract_samples([_pair(soft_label=True)], {})
        total, mse, delta, mae = self._run_epoch(hard + soft)
        assert np.isfinite(total) and np.isfinite(mse)

    def test_all_hard_batch_uses_unweighted_path(self):
        # Leave-alone case: with no soft samples the historical loss math is
        # untouched (weights=None branch) — same seed → same loss as a run
        # where sample_weight keys are absent entirely.
        torch.manual_seed(7)
        with_key = self._run_epoch(extract_samples([_pair(soft_label=False)], {}))
        torch.manual_seed(7)
        stripped = extract_samples([_pair(soft_label=False)], {})
        for s in stripped:
            s.pop("sample_weight", None)
        without_key = self._run_epoch(stripped)
        assert with_key[0] == pytest.approx(without_key[0], abs=1e-9)


class TestSoftWindowFloor:
    """Maintainer ruling 2026-07-22: soft labels from sub-floor answer windows
    are DISCARDED from training (a ~1-chunk window is a query-echo-dominated
    noise label; predict-the-mean risk on a small corpus). Writer-side events
    are untouched — this is reader-side training policy only."""

    def test_soft_below_floor_discarded(self):
        samples = extract_samples(
            [_pair(soft_label=True, window_tokens=SOFT_MIN_WINDOW_TOKENS - 1)], {}
        )
        assert samples == []

    def test_soft_at_floor_kept_downweighted(self):
        samples = extract_samples(
            [_pair(soft_label=True, window_tokens=SOFT_MIN_WINDOW_TOKENS)], {}
        )
        assert samples and all(s["sample_weight"] == SOFT_LABEL_WEIGHT for s in samples)

    def test_soft_missing_window_tokens_discarded(self):
        # Conservative default: cannot confirm the window was big enough.
        samples = extract_samples([_pair(soft_label=True, window_tokens=None)], {})
        assert samples == []

    def test_hard_ignores_floor(self):
        # Leave-alone case: the floor gates ONLY soft labels — a hard citation
        # with a small recorded window (defensive shape) still trains at 1.0.
        samples = extract_samples([_pair(soft_label=False, window_tokens=100)], {})
        assert samples and all(s["sample_weight"] == 1.0 for s in samples)

    def test_floor_env_tunable(self, monkeypatch):
        monkeypatch.setenv("RL_SOFT_MIN_WINDOW_TOKENS", "300")
        assert _resolve_soft_min_window_tokens() == 300
        monkeypatch.setenv("RL_SOFT_MIN_WINDOW_TOKENS", "not-a-number")
        assert _resolve_soft_min_window_tokens() == 1000
        monkeypatch.setenv("RL_SOFT_MIN_WINDOW_TOKENS", "-5")
        assert _resolve_soft_min_window_tokens() == 1000
        monkeypatch.delenv("RL_SOFT_MIN_WINDOW_TOKENS", raising=False)
        assert _resolve_soft_min_window_tokens() == 1000


class TestEnvTunable:
    def test_default_is_half(self, monkeypatch):
        monkeypatch.delenv("RL_SOFT_LABEL_WEIGHT", raising=False)
        assert _resolve_soft_label_weight() == 0.5

    def test_malformed_falls_back(self, monkeypatch):
        monkeypatch.setenv("RL_SOFT_LABEL_WEIGHT", "not-a-number")
        assert _resolve_soft_label_weight() == 0.5

    def test_out_of_range_falls_back(self, monkeypatch):
        monkeypatch.setenv("RL_SOFT_LABEL_WEIGHT", "1.5")
        assert _resolve_soft_label_weight() == 0.5
        monkeypatch.setenv("RL_SOFT_LABEL_WEIGHT", "-0.1")
        assert _resolve_soft_label_weight() == 0.5

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("RL_SOFT_LABEL_WEIGHT", "0.25")
        assert _resolve_soft_label_weight() == 0.25
