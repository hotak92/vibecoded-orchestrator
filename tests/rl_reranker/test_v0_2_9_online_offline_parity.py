# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.9 C8 — pin online == offline target parity on the same JSONL event.

The load-bearing property: the container's online ``_update`` and the
offline trainer must compute the SAME training target from the SAME
inputs. Before C8 they diverged (online had the query-name boost +
missed-citation penalty; offline had neither — same JSONL, different
training distributions). v0.2.9 routes BOTH through
``_training_targets.compute_unified_targets``.

This test pins reproducibility: a fixture citation event yields identical
target values whether resolved via the offline trainer's
``compute_targets_from_cosine_sims`` (which forwards to
``compute_unified_targets``) or via direct call to the unified helper
(which is what the container does in ``_update``).

If the offline trainer ever diverges (e.g. someone re-introduces a
custom z-score formula), this test fails.

Async convention: pure-sync (no async needed for the pure-function path).
"""

from __future__ import annotations

import pytest

from vco_lib.rl_training_targets import compute_unified_targets
from rl_reranker.offline_trainer import compute_targets_from_cosine_sims


class TestOnlineOfflineTargetParity:
    def test_v3_event_with_literal_cited_yields_same_target_both_paths(self) -> None:
        """A v3 citation event with literal_cited+cosine_sims must produce
        the same target via offline_trainer as via direct unified helper."""
        cit_ev = {
            "event": "citation",
            "schema_version": 3,
            "task_id": "t-parity",
            "cosine_sims": {"NodeA": 0.30, "NodeB": 0.70},
            "literal_cited": {"NodeA": True, "NodeB": False},
        }

        cosine_sims = cit_ev["cosine_sims"]
        literal_cited = cit_ev["literal_cited"]

        # Path 1: through offline_trainer (mirrors extract_samples).
        offline_targets = compute_targets_from_cosine_sims(
            cosine_sims,
            literal_cited=literal_cited,
            cross_encoder_cited=None,
        )

        # Path 2: direct unified helper (mirrors container _update).
        direct_targets, _ = compute_unified_targets(
            cosine_sims,
            literal_cited=literal_cited,
            cross_encoder_cited=None,
        )

        assert offline_targets == direct_targets

    def test_v3_event_with_cross_encoder_cited_propagates(self) -> None:
        """cross_encoder_cited (None in v0.2.9 production but reserved in
        the schema) must propagate from event to target identically."""
        cosine_sims = {"X": 0.30}
        literal_cited = {"X": True}
        cross_encoder_cited = {"X": True}

        offline = compute_targets_from_cosine_sims(
            cosine_sims,
            literal_cited=literal_cited,
            cross_encoder_cited=cross_encoder_cited,
        )
        direct, _ = compute_unified_targets(
            cosine_sims,
            literal_cited=literal_cited,
            cross_encoder_cited=cross_encoder_cited,
        )
        assert offline == direct
        # Quantitative anchor: 0.3 -> 0.35 -> 0.625 (literal) -> 1.0 (cross-enc clamped).
        assert offline["X"] == pytest.approx(1.0, abs=1e-6)

    def test_v2_event_no_flag_dicts_degenerates_to_cosine_only(self) -> None:
        """Pre-v3 events lack literal_cited / cross_encoder_cited. The
        offline trainer must treat them as all-False (= no bonus), making
        the formula degenerate to the legacy cosine-only path. Back-compat
        for the ~700 MB of historical v2 JSONL on the dogfood box."""
        cosine_sims = {"NodeA": 0.30, "NodeB": 0.70}

        v2_offline = compute_targets_from_cosine_sims(cosine_sims)
        v3_all_false_offline = compute_targets_from_cosine_sims(
            cosine_sims,
            literal_cited={"NodeA": False, "NodeB": False},
            cross_encoder_cited={"NodeA": False, "NodeB": False},
        )

        assert v2_offline == v3_all_false_offline

    def test_back_compat_legacy_default_beta_preserved(self) -> None:
        """The beta=0.1 default (legacy compute_targets_from_cosine_sims
        signature) must produce the same result as the unified helper's
        beta_z=0.1 default."""
        cosine_sims = {"A": 0.4, "B": 0.6, "C": 0.8}

        offline = compute_targets_from_cosine_sims(cosine_sims)
        direct, _ = compute_unified_targets(cosine_sims)

        assert offline == direct
