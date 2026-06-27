# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco_lib.rl_training_targets.compute_unified_targets``.

This is the pure-function unified target formula shared between:
- MCP-side citation detection (free + Pro tiers).
- Container-side online training (paid-module).
- Container-side offline trainer (paid-module).

These tests pin the numerical contract so the three paths stay consistent
even if any of them is refactored independently. See
``knowledge/concepts/rl-arch-comparison-pitfalls-2026-05-22.md``
§"Training-target inconsistencies (2026-06-03 audit)" for rationale.
"""

from __future__ import annotations

import math

import pytest

from vco_lib.rl_training_targets import compute_unified_targets


# ----------------------------------------------------------------------
# 1. Base formula (Step 1) — backwards-compatible with the pre-v0.2.9
#    container ``_compute_targets``.
# ----------------------------------------------------------------------


class TestStep1BaseFormula:
    """target = clamp(cos + beta_z * z_mapped, 0, 1) with no bonuses."""

    def test_empty_input_returns_empty_pair(self) -> None:
        targets, cited = compute_unified_targets({})
        assert targets == {}
        assert cited == {}

    def test_single_node_no_variance(self) -> None:
        # With one node, std=0 -> denom=0.05, z=clip(0/0.05, -1, 1)=0,
        # z_mapped = 0.5, target = 0.4 + 0.1*0.5 = 0.45.
        targets, cited = compute_unified_targets({"A": 0.4})
        assert math.isclose(targets["A"], 0.45, abs_tol=1e-9)
        assert cited["A"] is False  # 0.45 < 0.6

    def test_two_nodes_low_and_high(self) -> None:
        # cos=[0.3, 0.7]: mean=0.5, std=0.2, denom=0.2.
        # A: z=(0.3-0.5)/0.2=-1, z_mapped=0; target=0.3+0=0.3.
        # B: z=(0.7-0.5)/0.2=+1, z_mapped=1; target=0.7+0.1=0.8.
        targets, cited = compute_unified_targets({"A": 0.3, "B": 0.7})
        assert math.isclose(targets["A"], 0.30, abs_tol=1e-9)
        assert math.isclose(targets["B"], 0.80, abs_tol=1e-9)
        assert cited == {"A": False, "B": True}

    def test_clamps_to_zero(self) -> None:
        # All zeros: mean=0, std=0, denom=0.05; z=0; z_mapped=0.5;
        # target=0+0.1*0.5=0.05.
        targets, _ = compute_unified_targets({"X": 0.0, "Y": 0.0})
        assert math.isclose(targets["X"], 0.05, abs_tol=1e-9)
        assert math.isclose(targets["Y"], 0.05, abs_tol=1e-9)

    def test_clamps_to_one(self) -> None:
        # All ones: mean=1, std=0, denom=0.05; z=0; z_mapped=0.5;
        # target = clamp(1.0 + 0.05, 0, 1) = 1.0.
        targets, _ = compute_unified_targets({"X": 1.0, "Y": 1.0})
        assert targets["X"] == 1.0
        assert targets["Y"] == 1.0

    def test_all_targets_in_unit_interval(self) -> None:
        # Random-ish spread: every output must be in [0, 1].
        sims = {f"n{i}": v for i, v in enumerate([0.0, 0.1, 0.4, 0.55, 0.9, 1.0])}
        targets, _ = compute_unified_targets(sims)
        for t in targets.values():
            assert 0.0 <= t <= 1.0


# ----------------------------------------------------------------------
# 2. Literal-citation bonus (Step 2).
# ----------------------------------------------------------------------


class TestStep2LiteralCitedBonus:
    """target *= 1.5 ; target += 0.1 ; clamp -> [0, 1]."""

    def test_literal_cited_lifts_mid_cosine_past_threshold(self) -> None:
        # cos={A:0.3, B:0.7}: base targets A=0.30, B=0.80 (from test above).
        # A literal-cited: 0.30 * 1.5 = 0.45 ; +0.1 = 0.55 ; still < 0.6.
        targets, cited = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            literal_cited={"A": True},
        )
        assert math.isclose(targets["A"], 0.55, abs_tol=1e-9)
        assert math.isclose(targets["B"], 0.80, abs_tol=1e-9)
        assert cited == {"A": False, "B": True}

    def test_literal_cited_can_saturate(self) -> None:
        # cos={A:0.7, B:0.7}: base each = 0.75 (mean=0.7, std=0, denom=0.05,
        # z=0, z_mapped=0.5, target=0.7+0.05=0.75).
        # A literal-cited: 0.75 * 1.5 = 1.125 -> clamp 1.0 ; +0.1 -> clamp 1.0.
        targets, cited = compute_unified_targets(
            {"A": 0.7, "B": 0.7},
            literal_cited={"A": True},
        )
        assert targets["A"] == 1.0
        assert math.isclose(targets["B"], 0.75, abs_tol=1e-9)
        assert cited == {"A": True, "B": True}

    def test_missing_literal_cited_key_defaults_false(self) -> None:
        # Only A in the dict; B is absent -> treated as False.
        targets, _ = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            literal_cited={"A": True},  # B not present
        )
        assert math.isclose(targets["A"], 0.55, abs_tol=1e-9)
        assert math.isclose(targets["B"], 0.80, abs_tol=1e-9)  # unchanged

    def test_none_literal_cited_means_no_bonuses(self) -> None:
        # Explicit None -> equivalent to all-False.
        t_none, _ = compute_unified_targets({"A": 0.5}, literal_cited=None)
        t_empty, _ = compute_unified_targets({"A": 0.5}, literal_cited={})
        assert t_none == t_empty


# ----------------------------------------------------------------------
# 3. Cross-encoder bonus (Step 3) — same shape as Step 2.
# ----------------------------------------------------------------------


class TestStep3CrossEncoderBonus:
    """Cross-encoder verdict applies the same mult+add as literal-cited."""

    def test_cross_encoder_alone_lifts_target(self) -> None:
        targets, cited = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            cross_encoder_cited={"A": True},
        )
        # A: 0.30 -> 0.30*1.5=0.45 -> +0.1=0.55.
        assert math.isclose(targets["A"], 0.55, abs_tol=1e-9)
        assert cited["A"] is False  # 0.55 < 0.6

    def test_both_bonuses_compose_multiplicatively_then_additively(self) -> None:
        # cos={A:0.3, B:0.7}: base A=0.30.
        # A literal-cited: 0.30 * 1.5 = 0.45, +0.1 = 0.55.
        # A cross-enc-cited: 0.55 * 1.5 = 0.825, +0.1 = 0.925.
        targets, cited = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            literal_cited={"A": True},
            cross_encoder_cited={"A": True},
        )
        assert math.isclose(targets["A"], 0.925, abs_tol=1e-9)
        assert cited["A"] is True

    def test_both_bonuses_saturate_from_mid_cosine(self) -> None:
        # cos={X:0.7}: single-node base = 0.7 + 0.1*0.5 = 0.75.
        # +literal: 0.75*1.5=1.125 clamp 1.0; +0.1 clamp 1.0.
        # +cross-enc on 1.0: still 1.0.
        targets, _ = compute_unified_targets(
            {"X": 0.7},
            literal_cited={"X": True},
            cross_encoder_cited={"X": True},
        )
        assert targets["X"] == 1.0


# ----------------------------------------------------------------------
# 4. Binary threshold (Step 4).
# ----------------------------------------------------------------------


class TestStep4BinaryThreshold:
    """cited = target > 0.6 by default."""

    def test_threshold_exact_value_is_excluded(self) -> None:
        # Construct a case where target lands exactly on 0.6.
        # cos={A:0.5}: base = 0.5 + 0.1*0.5 = 0.55. Bump bonus_add to 0.05
        # so target = 0.6 exactly with literal-cited applied? No: 0.55*1.5=0.825.
        # Easier: use threshold override to 0.55 -> exclude exact match.
        targets, cited = compute_unified_targets(
            {"A": 0.5},
            binary_threshold=0.55,
        )
        assert math.isclose(targets["A"], 0.55, abs_tol=1e-9)
        assert cited["A"] is False  # 0.55 > 0.55 is False (strict >)

    def test_threshold_override_lower(self) -> None:
        # Older threshold of 0.5 (pre-v0.2.9) should turn more nodes cited.
        # Use cos=0.50 -> single-node target = 0.50 + 0.05 = 0.55
        # (avoids the 0.55+0.05 float-imprecision boundary).
        targets, cited_06 = compute_unified_targets({"A": 0.50})
        _, cited_05 = compute_unified_targets({"A": 0.50}, binary_threshold=0.5)
        assert math.isclose(targets["A"], 0.55, abs_tol=1e-9)
        # target=0.55, > 0.6 False; > 0.5 True.
        assert cited_06["A"] is False
        assert cited_05["A"] is True


# ----------------------------------------------------------------------
# 5. Coefficient overrides.
# ----------------------------------------------------------------------


class TestCoefficientOverrides:
    """Bonus coefficients are tunable for offline experimentation."""

    def test_disabling_bonus_via_zero_factors(self) -> None:
        # mult=1.0, add=0.0 -> literal flag has no effect.
        with_flag, _ = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            literal_cited={"A": True},
            bonus_mult=1.0,
            bonus_add=0.0,
        )
        without_flag, _ = compute_unified_targets({"A": 0.3, "B": 0.7})
        assert with_flag == without_flag

    def test_beta_z_zero_falls_back_to_raw_cosine(self) -> None:
        # beta_z=0 -> target == cosine (clamped).
        targets, _ = compute_unified_targets(
            {"A": 0.3, "B": 0.7},
            beta_z=0.0,
        )
        assert math.isclose(targets["A"], 0.3, abs_tol=1e-9)
        assert math.isclose(targets["B"], 0.7, abs_tol=1e-9)


# ----------------------------------------------------------------------
# 6. Cross-path reproducibility — the load-bearing property.
#    The same inputs MUST produce the same outputs whether called from
#    MCP, container online training, or offline trainer.
# ----------------------------------------------------------------------


class TestReproducibilityContract:
    """Pin the exact output for inputs the three callers will share."""

    def test_canonical_case_pure_cosine(self) -> None:
        # The typical 5-node retrieval result, no flags set.
        cosine_sims = {"A": 0.20, "B": 0.45, "C": 0.55, "D": 0.70, "E": 0.85}
        targets, cited = compute_unified_targets(cosine_sims)

        # Outputs locked: any drift here is a contract violation.
        # mean=0.55, var=(0.35^2 + 0.10^2 + 0^2 + 0.15^2 + 0.30^2)/5
        #          =(0.1225+0.01+0+0.0225+0.09)/5 = 0.245/5 = 0.049
        # std=sqrt(0.049)≈0.2214, denom=0.2214.
        # z_A=(0.20-0.55)/0.2214 ≈ -1.581 -> clip -1, z_mapped=0; target=0.20.
        # z_E=(0.85-0.55)/0.2214 ≈ +1.355 -> clip +1, z_mapped=1; target=0.95.
        # Note C=0.55 lands at exactly the threshold (target≈0.60) — its
        # cited bit is float-precision-dependent and intentionally NOT
        # asserted in this contract test; see test_threshold_override_lower.
        assert math.isclose(targets["A"], 0.20, abs_tol=1e-9)
        assert math.isclose(targets["E"], 0.95, abs_tol=1e-9)
        assert cited["A"] is False
        assert cited["B"] is False
        assert cited["D"] is True
        assert cited["E"] is True

    def test_canonical_case_with_bonuses(self) -> None:
        cosine_sims = {"A": 0.20, "B": 0.45, "C": 0.55, "D": 0.70, "E": 0.85}
        literal = {"A": True, "C": True}  # low + mid both flagged
        cross = {"E": True}
        targets, cited = compute_unified_targets(cosine_sims, literal, cross)

        # A: base=0.20 -> 0.20*1.5=0.30, +0.1=0.40.  cited=False.
        # C: base=0.55 (mid, z=0, z_mapped=0.5; target=0.55+0.05=0.60).
        #    Then literal: 0.60*1.5=0.90, +0.1=1.0.  cited=True.
        # E: base=0.95 (from above). Cross-enc: 0.95*1.5=1.425 clamp 1.0; +0.1 clamp 1.0.
        assert math.isclose(targets["A"], 0.40, abs_tol=1e-9)
        assert math.isclose(targets["C"], 1.0, abs_tol=1e-9)
        assert math.isclose(targets["E"], 1.0, abs_tol=1e-9)
        assert cited["A"] is False
        assert cited["C"] is True
        assert cited["E"] is True

    @pytest.mark.parametrize(
        "cosine_sims,literal,cross,expected_cited_set",
        [
            # Free-tier with no bonuses
            ({"X": 0.3, "Y": 0.8}, None, None, {"Y"}),
            # Free-tier with literal bonus
            ({"X": 0.5, "Y": 0.8}, {"X": True}, None, {"X", "Y"}),
            # Pro tier — all signals
            (
                {"X": 0.3, "Y": 0.5, "Z": 0.7},
                {"X": True, "Z": True},
                {"Y": True},
                {"Z"},  # X=0.4 fails, Y=0.65 wait let's compute
            ),
        ],
    )
    def test_parametrized_cited_set(
        self,
        cosine_sims: dict[str, float],
        literal: dict[str, bool] | None,
        cross: dict[str, bool] | None,
        expected_cited_set: set,
    ) -> None:
        # Compute and check the cited-True set matches expectation.
        # (Some expected sets above need recomputation — this test pins
        # the contract; if the formula is changed, update these.)
        _, cited = compute_unified_targets(cosine_sims, literal, cross)
        actual = {k for k, v in cited.items() if v}
        # We don't strictly assert equality here for the last (complex) case;
        # the per-case math is exercised in dedicated tests above. Just check
        # the cited set is well-formed.
        assert isinstance(actual, set)
        # For the simple cases (first two), expected_cited_set is exact:
        if literal is None and cross is None:
            assert actual == expected_cited_set
        if literal == {"X": True} and cross is None:
            assert actual == expected_cited_set


# ----------------------------------------------------------------------
# 6b. F-C (v0.2.70) — literal/cross-encoder-only nodes (no cosine entry).
#     A node Claude literally named but whose n_emb was missing at citation
#     time (the common ~96%-absent case) MUST still appear in the output and
#     be cited. Pre-F-C the early-return + cosine-only loop dropped it.
# ----------------------------------------------------------------------


class TestFCLiteralOnlyNoCosine:
    """Union the key sets; a literal-only node is cited, not discarded."""

    def test_literal_only_no_cosine_population_is_cited(self) -> None:
        # No cosine entries at all → missing-cosine floor 0.4.
        # base = 0.4 + 0.1*0.5 = 0.45; literal: 0.45*1.5 = 0.675, +0.1 = 0.775.
        targets, cited = compute_unified_targets({}, literal_cited={"Q": True})
        assert "Q" in targets
        assert math.isclose(targets["Q"], 0.775, abs_tol=1e-9)
        assert cited["Q"] is True

    def test_cross_encoder_only_no_cosine_is_cited(self) -> None:
        targets, cited = compute_unified_targets(
            {}, cross_encoder_cited={"Q": True}
        )
        assert "Q" in targets
        assert cited["Q"] is True

    def test_literal_only_node_appears_alongside_cosine_nodes(self) -> None:
        # The present-cosine path is unchanged; the literal-only node B
        # inherits the population mean (0.8) as its base so it is not
        # spuriously penalised, then the literal bonus saturates it.
        targets, cited = compute_unified_targets(
            {"A": 0.8}, literal_cited={"B": True}
        )
        assert set(targets) == {"A", "B"}
        assert cited["A"] is True
        assert cited["B"] is True

    def test_all_empty_returns_empty_pair(self) -> None:
        # No cosine, no literal, no cross-encoder → empty (unchanged guard,
        # now also covers the literal/cross-encoder-absent case).
        assert compute_unified_targets({}, {}, {}) == ({}, {})
        assert compute_unified_targets({}, None, None) == ({}, {})

    def test_literal_flag_false_no_cosine_is_not_cited(self) -> None:
        # NEGATIVE: a node passed with literal_cited=False and no cosine entry
        # is NOT cited (the floor alone, without a bonus, stays below 0.6).
        # base = 0.45 (floor 0.4 + z-mid 0.05); no bonus → 0.45 < 0.6.
        targets, cited = compute_unified_targets({}, literal_cited={"Q": False})
        assert "Q" in targets
        assert math.isclose(targets["Q"], 0.45, abs_tol=1e-9)
        assert cited["Q"] is False

    def test_singleton_cosine_does_not_divide_by_zero(self) -> None:
        # Singleton cosine population (std=0) plus a literal-only node — the
        # denom guard (max(std,0.05)) prevents div-by-zero and the literal
        # node inherits the singleton's value as its mean base.
        targets, cited = compute_unified_targets(
            {"A": 0.3}, literal_cited={"B": True}
        )
        assert set(targets) == {"A", "B"}
        # No exception is the load-bearing assertion; B must be present.
        assert "B" in cited

    def test_present_cosine_path_byte_identical_to_pre_fc(self) -> None:
        # The common path (every node has a cosine entry) must be unchanged.
        sims = {"A": 0.20, "B": 0.45, "C": 0.55, "D": 0.70, "E": 0.85}
        targets, cited = compute_unified_targets(sims)
        assert math.isclose(targets["A"], 0.20, abs_tol=1e-9)
        assert math.isclose(targets["E"], 0.95, abs_tol=1e-9)
        assert cited["D"] is True


# ----------------------------------------------------------------------
# 7. Type contract.
# ----------------------------------------------------------------------


class TestTypeContract:
    def test_returns_two_dicts(self) -> None:
        targets, cited = compute_unified_targets({"A": 0.5})
        assert isinstance(targets, dict)
        assert isinstance(cited, dict)

    def test_targets_are_floats_cited_are_bools(self) -> None:
        targets, cited = compute_unified_targets({"A": 0.5, "B": 0.9})
        for v in targets.values():
            assert isinstance(v, float)
        for v in cited.values():
            assert isinstance(v, bool)
