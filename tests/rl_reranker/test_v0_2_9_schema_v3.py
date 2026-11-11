# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Schema v3 logger tests + unified-targets reproducibility.

v0.2.9 ships:
1. ``rl_logger.py`` SCHEMA_VERSION bumped to 3.
2. ``log_citations`` extended with ``literal_cited`` + ``cross_encoder_cited`` kwargs.
3. ``vco_lib.rl_training_targets.compute_unified_targets`` — the one copy the
   reranker imports (v0.2.106; a vendored duplicate before).

These tests pin those contracts. The container's online-training + offline
trainer behavioral changes ride in a separate commit on top of this one.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from rl_client.rl_logger import RLDataLogger
from vco_lib.rl_training_targets import compute_unified_targets


# ----------------------------------------------------------------------
# 1. Schema v3 — log_citations writes the new flag dicts.
# ----------------------------------------------------------------------


class TestSchemaV3CitationEvents:
    def _logger(self, td: Path) -> RLDataLogger:
        return RLDataLogger(
            log_path=td / "ev.jsonl",
            project="V3Test",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
        )

    def test_v3_minimal_citation_event(self) -> None:
        """Existing 4-arg form still works — flag dicts are optional."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t1",
                task_type="implementation",
                citations={"A": True, "B": False},
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["schema_version"] == RLDataLogger.SCHEMA_VERSION == 4
            assert event["event"] == "citation"
            assert event["citations"] == {"A": True, "B": False}
            # Optional fields absent when not supplied.
            assert "literal_cited" not in event
            assert "cross_encoder_cited" not in event

    def test_v3_with_literal_cited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t2",
                task_type="research",
                citations={"A": True, "B": False, "C": True},
                cosine_sims={"A": 0.85, "B": 0.30, "C": 0.55},
                literal_cited={"A": True, "B": False, "C": True},
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["literal_cited"] == {"A": True, "B": False, "C": True}
            # Cosine values are rounded to 4 decimal places.
            assert event["cosine_sims"] == {"A": 0.85, "B": 0.30, "C": 0.55}

    def test_v3_with_both_flag_dicts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t3",
                task_type="implementation",
                citations={"A": True, "B": False},
                cosine_sims={"A": 0.85, "B": 0.30},
                literal_cited={"A": True, "B": False},
                cross_encoder_cited={"A": True, "B": False},
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["literal_cited"] == {"A": True, "B": False}
            assert event["cross_encoder_cited"] == {"A": True, "B": False}

    def test_v3_empty_flag_dicts_are_omitted(self) -> None:
        """Empty dicts shouldn't pollute the event with empty objects."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t4",
                task_type="x",
                citations={"A": True},
                literal_cited={},  # empty dict
                cross_encoder_cited={},  # empty dict
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            # Empty dicts are falsy -> field omitted.
            assert "literal_cited" not in event
            assert "cross_encoder_cited" not in event


# ----------------------------------------------------------------------
# 2. The unified-targets contract as the reranker consumes it.
# ----------------------------------------------------------------------


class TestUnifiedTargetsContract:
    """The numerical contract of ``vco_lib/rl_training_targets.py`` as the
    reranker consumes it (the same contract test_unified_targets.py pins on
    the orchestrator side; since v0.2.106 there is one copy)."""

    def test_empty_input(self) -> None:
        targets, cited = compute_unified_targets({})
        assert targets == {}
        assert cited == {}

    def test_step1_canonical(self) -> None:
        # cos={A:0.3, B:0.7}: mean=0.5, std=0.2, denom=0.2.
        # A: z=-1, z_mapped=0, target=0.3.
        # B: z=+1, z_mapped=1, target=0.8.
        targets, cited = compute_unified_targets({"A": 0.3, "B": 0.7})
        assert targets["A"] == pytest.approx(0.30, abs=1e-9)
        assert targets["B"] == pytest.approx(0.80, abs=1e-9)
        assert cited == {"A": False, "B": True}

    def test_literal_cited_bonus(self) -> None:
        targets, cited = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            literal_cited={"A": True},
        )
        # A: 0.3 -> *1.5=0.45 -> +0.1=0.55 (still under threshold).
        assert targets["A"] == pytest.approx(0.55, abs=1e-9)
        assert cited["A"] is False
        assert cited["B"] is True

    def test_cross_encoder_bonus_composes(self) -> None:
        # Both bonuses applied: 0.3 -> 0.55 (literal) -> 0.925 (cross-enc).
        targets, cited = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            literal_cited={"A": True},
            cross_encoder_cited={"A": True},
        )
        assert targets["A"] == pytest.approx(0.925, abs=1e-9)
        assert cited["A"] is True

    def test_threshold_is_0_6(self) -> None:
        # target=0.6 exactly: cited=False (strict >).
        targets, cited = compute_unified_targets(
            {"A": 0.50}, binary_threshold=0.5
        )
        # cos=0.50, single-node target=0.50+0.05=0.55. > 0.5 True.
        assert cited["A"] is True

    def test_outputs_in_unit_interval(self) -> None:
        sims = {f"n{i}": v for i, v in enumerate([0.0, 0.2, 0.5, 0.8, 1.0])}
        targets, _ = compute_unified_targets(
            sims,
            literal_cited={k: True for k in sims},
            cross_encoder_cited={k: True for k in sims},
        )
        for t in targets.values():
            assert 0.0 <= t <= 1.0


# ----------------------------------------------------------------------
# 3. Back-compat: a v3 reader must handle v2-style events (missing flag
#    dicts default to all-False -> equivalent to pre-v3 cosine-only).
# ----------------------------------------------------------------------


class TestV2BackwardCompatibility:
    """Treat missing literal_cited / cross_encoder_cited as all-False so
    the 700 MB of v2 events in production keep training under the new
    formula without retroactive data loss."""

    def test_missing_flag_dicts_equivalent_to_all_false(self) -> None:
        cos = {"A": 0.3, "B": 0.7}
        # No flag dicts at all -> cosine-only path.
        t_none, _ = compute_unified_targets(cos)
        # Explicit all-False dicts -> identical.
        t_false, _ = compute_unified_targets(
            cos,
            literal_cited={"A": False, "B": False},
            cross_encoder_cited={"A": False, "B": False},
        )
        assert t_none == t_false
