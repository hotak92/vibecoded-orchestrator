# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.7 checkpoint metadata fields: weights_version + embedding_source.

Covers:
- weights_version round-trips through save + load (RLModel.weights_version property).
- embedding_source round-trips through save + load.
- Strict cross-source guard refuses a checkpoint trained on a DIFFERENT embedding
  source than the container is pinned to (R4 correctness bug fix).
- Permissive case: when EITHER side has an empty embedding_source (legacy checkpoint
  or test/standalone construction with no source set), load proceeds with a warning.
- Legacy checkpoint compat: pre-v0.2.7 checkpoints without weights_version /
  embedding_source still load cleanly (the new fields default to empty string).
"""
from __future__ import annotations

from pathlib import Path

import pytest


torch = pytest.importorskip("torch")

from rl_reranker.rl_model import RLModel  # noqa: E402

_EMB_DIM = 32


def _make_model(state_path: Path, *, weights_version: str = "", embedding_source: str = "") -> RLModel:
    return RLModel.load_or_create(
        state_path=state_path,
        emb_dim=_EMB_DIM,
        device="cpu",
        weights_version=weights_version,
        embedding_source=embedding_source,
    )


# ---------------------------------------------------------------------------
# R5 — weights_version round-trip
# ---------------------------------------------------------------------------


class TestWeightsVersionRoundtrip:
    def test_weights_version_persists_across_save_load(self, tmp_path):
        path = tmp_path / "rl_model.pt"
        m1 = _make_model(path, weights_version="0.2.6")
        assert m1.weights_version == "0.2.6"
        m1.save()

        m2 = _make_model(path, weights_version="")  # caller doesn't have to pass it; load reads from ckpt
        assert m2.weights_version == "0.2.6", (
            f"weights_version should be hydrated from checkpoint; got {m2.weights_version!r}"
        )

    def test_caller_passed_weights_version_overridden_by_checkpoint(self, tmp_path):
        """If the caller passes a value AND the checkpoint has one, the checkpoint wins.
        Rationale: the on-disk metadata is the source of truth for what was trained;
        the constructor's kwarg is only meaningful for fresh-init or save-time stamping."""
        path = tmp_path / "rl_model.pt"
        m1 = _make_model(path, weights_version="0.2.6")
        m1.save()

        m2 = _make_model(path, weights_version="0.99.99-caller-passed")
        assert m2.weights_version == "0.2.6"

    def test_empty_weights_version_when_constructed_without_one(self, tmp_path):
        """Fresh-init or standalone use without a weights_version → empty string,
        not None. Downstream callers can match against "" as the "unknown" sentinel."""
        path = tmp_path / "rl_model.pt"
        m = _make_model(path)
        assert m.weights_version == ""


# ---------------------------------------------------------------------------
# R4 — cross-source weight-sharing guard
# ---------------------------------------------------------------------------


class TestCrossSourceGuard:
    def test_strict_refuse_when_both_sources_explicit_and_different(self, tmp_path, caplog):
        """Checkpoint trained on arctic, container pinned to qwen3 → REFUSED.
        This is the correctness-bug fix: same dim, different latent spaces."""
        import logging
        path = tmp_path / "rl_model.pt"

        m1 = _make_model(path, embedding_source="arctic")
        # Pin a known param so we can detect if the load actually succeeded.
        with torch.no_grad():
            m1._net.enc1.weight.fill_(0.42)
        m1.save()

        with caplog.at_level(logging.WARNING, logger="rl_reranker.rl_model"):
            m2 = _make_model(path, embedding_source="qwen3")

        # The load should have been refused → m2's enc1 should be FRESH-INIT,
        # not 0.42 from the arctic checkpoint.
        enc1_mean = float(m2._net.enc1.weight.mean())
        assert abs(enc1_mean - 0.42) > 0.01, (
            f"cross-source load should have been refused (fresh init expected); "
            f"got enc1 mean {enc1_mean:.4f} suspiciously close to the saved 0.42"
        )
        # Warning should have been logged.
        assert any(
            "embedding_source mismatch" in rec.message
            for rec in caplog.records
        ), "expected a WARNING about embedding_source mismatch"

    def test_strict_accept_when_both_sources_match(self, tmp_path):
        path = tmp_path / "rl_model.pt"
        m1 = _make_model(path, embedding_source="arctic")
        with torch.no_grad():
            m1._net.enc1.weight.fill_(0.42)
        m1.save()

        m2 = _make_model(path, embedding_source="arctic")
        enc1_mean = float(m2._net.enc1.weight.mean())
        assert abs(enc1_mean - 0.42) < 0.01, (
            f"matching-source load should have populated the network; "
            f"got enc1 mean {enc1_mean:.4f}, expected ~0.42"
        )

    def test_permissive_when_checkpoint_source_empty(self, tmp_path, caplog):
        """Legacy checkpoint (no embedding_source field) → load proceeds with INFO log.

        Pre-v0.2.7 checkpoints don't have the field; we can't refuse them
        without making the upgrade path impossible.
        """
        import logging
        path = tmp_path / "rl_model.pt"

        m1 = _make_model(path)  # no embedding_source → checkpoint stores ""
        with torch.no_grad():
            m1._net.enc1.weight.fill_(0.42)
        m1.save()

        with caplog.at_level(logging.INFO, logger="rl_reranker.rl_model"):
            m2 = _make_model(path, embedding_source="arctic")

        # Permissive: load proceeds.
        enc1_mean = float(m2._net.enc1.weight.mean())
        assert abs(enc1_mean - 0.42) < 0.01, (
            "permissive-case load should populate the network; "
            f"got enc1 mean {enc1_mean:.4f}"
        )
        # INFO log noting the partial case.
        assert any(
            "embedding_source partial" in rec.message
            for rec in caplog.records
        ), "expected an INFO about partial embedding_source"

    def test_permissive_when_expected_source_empty(self, tmp_path, caplog):
        """Container constructed without ACTIVE_EMBEDDING set (test / standalone use)
        → load proceeds even if checkpoint has a source. Symmetric with the above."""
        import logging
        path = tmp_path / "rl_model.pt"

        m1 = _make_model(path, embedding_source="arctic")
        m1.save()

        with caplog.at_level(logging.INFO, logger="rl_reranker.rl_model"):
            _make_model(path)  # no embedding_source

        assert any(
            "embedding_source partial" in rec.message
            for rec in caplog.records
        ), "expected an INFO about partial embedding_source"


# ---------------------------------------------------------------------------
# Legacy compat — pre-v0.2.7 checkpoints have no weights_version or
# embedding_source. _try_load must tolerate them.
# ---------------------------------------------------------------------------


class TestLegacyCheckpointCompat:
    def test_load_checkpoint_missing_both_new_fields(self, tmp_path):
        """Synthesize a pre-v0.2.7 checkpoint missing both fields; load must
        succeed + the new fields default to empty string."""
        path = tmp_path / "rl_model.pt"

        # Build a fresh model + save normally to get a valid checkpoint.
        m1 = _make_model(path)
        m1.save()

        # Surgically strip the new fields to simulate a legacy save.
        ckpt = torch.load(str(path), map_location="cpu", weights_only=True)
        ckpt.pop("weights_version", None)
        ckpt.pop("embedding_source", None)
        import os
        tmp = str(path) + ".tmp"
        torch.save(ckpt, tmp)
        os.replace(tmp, str(path))

        # Load — should succeed cleanly.
        m2 = _make_model(path)
        assert m2.weights_version == ""
        assert m2.embedding_source == ""
