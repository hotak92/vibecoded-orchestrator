# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tier-boundary tests for the v0.2.77 5c shared-pool concurrency budget.

`select_embedding_concurrency` is a PURE decision function (no probes) that
maps free device memory + the two chosen embedding backends onto a
concurrency budget (code-embed in-flight cap + update-all per-project cap),
implementing the USER DESIGN RULING:

    max_parallel = floor((system_memory / model_memory) * 0.8)

with ONE shared pool: reserve every concurrently-active model's BASE
footprint first, then allocate parallel slots from the remainder.

These tests sweep memory tiers + backend combinations and assert the exact
integer the formula produces (with the documented floor-1 / cap-8 clamps),
so a future footprint-table edit or an off-by-one in the pool math is caught.
"""

from __future__ import annotations

import math

import pytest

from vco_lib.embedding_selection import (
    EmbeddingConcurrencyBudget,
    select_embedding_concurrency,
    _CONCURRENCY_CEILING,
    _MEMORY_SAFETY_FACTOR,
    _MODEL_FOOTPRINT_GB,
    _footprint_for,
    _formula_slots,
    _clamp_slots,
    _CODE_BACKEND_CODESAGE,
    _CODE_BACKEND_QWEN3,
    _CODE_BACKEND_JINA,
    _CODE_BACKEND_OPENAI,
    _KG_BACKEND_QWEN3,
    _KG_BACKEND_ARCTIC,
    _KG_BACKEND_OPENAI,
)


def _expected_slots(pool: float, slot: float) -> int:
    """Re-derive the clamped formula result for cross-checking."""
    raw = int(math.floor((pool / slot) * _MEMORY_SAFETY_FACTOR))
    return max(1, min(_CONCURRENCY_CEILING, raw))


# ────────────────────────────────────────────────────────────────────
# The core formula + clamps
# ────────────────────────────────────────────────────────────────────

class TestFormulaAndClamps:
    def test_formula_is_floor_of_pool_over_slot_times_point_eight(self) -> None:
        # 10 GB pool, 1 GB slot → floor(10 * 0.8) = 8.
        assert _formula_slots(10.0, 1.0) == 8
        # 4 GB pool, 0.3 GB slot → floor((4/0.3)*0.8) = floor(10.67) = 10.
        assert _formula_slots(4.0, 0.3) == 10

    def test_formula_non_positive_slot_returns_ceiling(self) -> None:
        # Remote / zero-footprint model → no local constraint → ceiling
        # (caller's clamp caps it).
        assert _formula_slots(10.0, 0.0) == _CONCURRENCY_CEILING
        assert _formula_slots(10.0, -1.0) == _CONCURRENCY_CEILING
        assert _formula_slots(10.0, float("nan")) == _CONCURRENCY_CEILING

    def test_formula_non_positive_pool_returns_one(self) -> None:
        assert _formula_slots(0.0, 0.5) == 1
        assert _formula_slots(-3.0, 0.5) == 1

    def test_formula_non_finite_pool_returns_one(self) -> None:
        # A non-finite pool (should never happen; defensive) must not raise
        # (int(floor(inf)) would OverflowError) — the guard returns 1.
        assert _formula_slots(float("inf"), 0.5) == 1
        assert _formula_slots(float("nan"), 0.5) == 1

    def test_clamp_floor_is_one(self) -> None:
        assert _clamp_slots(0) == 1
        assert _clamp_slots(-5) == 1

    def test_clamp_cap_is_ceiling(self) -> None:
        assert _clamp_slots(_CONCURRENCY_CEILING + 1) == _CONCURRENCY_CEILING
        assert _clamp_slots(10_000) == _CONCURRENCY_CEILING

    def test_clamp_passthrough_in_range(self) -> None:
        assert _clamp_slots(3) == 3
        assert _clamp_slots(_CONCURRENCY_CEILING) == _CONCURRENCY_CEILING


# ────────────────────────────────────────────────────────────────────
# Shared-pool reservation semantics
# ────────────────────────────────────────────────────────────────────

class TestSharedPoolReservation:
    def test_both_bases_reserved_when_co_resident(self) -> None:
        # 16 GB free, CodeSage (base 2.6) + qwen3 KG (base 1.2) co-resident.
        b = select_embedding_concurrency(
            16.0, _CODE_BACKEND_CODESAGE, _KG_BACKEND_QWEN3,
            device="cuda", both_gpu_resident=True,
        )
        assert b.reserved_code_base_gb == pytest.approx(2.6)
        assert b.reserved_kg_base_gb == pytest.approx(1.2)
        # pool = 16 - 2.6 - 1.2 = 12.2
        assert b.pool_gb == pytest.approx(12.2)

    def test_single_resident_reserves_only_larger_base(self) -> None:
        b = select_embedding_concurrency(
            16.0, _CODE_BACKEND_CODESAGE, _KG_BACKEND_QWEN3,
            device="cuda", both_gpu_resident=False,
        )
        # CodeSage base (2.6) > qwen3 base (1.2) → only 2.6 reserved.
        assert b.reserved_code_base_gb == pytest.approx(2.6)
        assert b.reserved_kg_base_gb == pytest.approx(0.0)
        assert b.pool_gb == pytest.approx(13.4)
        assert any("single-resident" in n for n in b.notes)

    def test_incident_geometry_16gib_gpu_codesage_qwen3(self) -> None:
        """The exact incident host: 16 GiB GPU, CodeSage + qwen3, both resident.

        This must produce a SMALL, sane budget — never the fixed default of 4
        that shed 503s. We assert the derived numbers exactly.
        """
        b = select_embedding_concurrency(
            16.0, _CODE_BACKEND_CODESAGE, _KG_BACKEND_QWEN3,
            device="cuda", both_gpu_resident=True,
        )
        pool = 16.0 - 2.6 - 1.2  # 12.2
        code_slot = _MODEL_FOOTPRINT_GB[_CODE_BACKEND_CODESAGE][1]  # 0.6
        kg_slot = _MODEL_FOOTPRINT_GB[_KG_BACKEND_QWEN3][1]         # 0.3
        assert b.code_embed_max_concurrent == _expected_slots(pool, code_slot)
        assert b.update_all_max_parallel_projects == _expected_slots(
            pool, code_slot + kg_slot
        )
        # Sanity: within the documented bounds.
        assert 1 <= b.code_embed_max_concurrent <= _CONCURRENCY_CEILING
        assert 1 <= b.update_all_max_parallel_projects <= _CONCURRENCY_CEILING


# ────────────────────────────────────────────────────────────────────
# Memory-tier sweep
# ────────────────────────────────────────────────────────────────────

class TestMemoryTierSweep:
    @pytest.mark.parametrize("free_gb", [0.0, 0.5, 2.0])
    def test_tiny_or_underflowing_memory_floors_to_one(self, free_gb: float) -> None:
        # Free memory below the reserved bases → pool underflow → floor 1.
        b = select_embedding_concurrency(
            free_gb, _CODE_BACKEND_CODESAGE, _KG_BACKEND_QWEN3,
        )
        assert b.code_embed_max_concurrent == 1
        assert b.update_all_max_parallel_projects == 1
        if free_gb < 2.6 + 1.2:
            assert any("underflow" in n for n in b.notes)

    @pytest.mark.parametrize("free_gb", [8.0, 16.0, 24.0, 48.0, 80.0])
    def test_larger_memory_never_exceeds_ceiling(self, free_gb: float) -> None:
        b = select_embedding_concurrency(
            free_gb, _CODE_BACKEND_QWEN3, _KG_BACKEND_QWEN3,
        )
        assert b.code_embed_max_concurrent <= _CONCURRENCY_CEILING
        assert b.update_all_max_parallel_projects <= _CONCURRENCY_CEILING

    def test_budget_is_monotonic_nondecreasing_in_memory(self) -> None:
        prev_code = 0
        prev_proj = 0
        for free_gb in [4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 32.0, 64.0]:
            b = select_embedding_concurrency(
                free_gb, _CODE_BACKEND_CODESAGE, _KG_BACKEND_QWEN3,
            )
            assert b.code_embed_max_concurrent >= prev_code
            assert b.update_all_max_parallel_projects >= prev_proj
            prev_code = b.code_embed_max_concurrent
            prev_proj = b.update_all_max_parallel_projects


# ────────────────────────────────────────────────────────────────────
# Backend footprint table
# ────────────────────────────────────────────────────────────────────

class TestBackendFootprints:
    def test_all_selector_backend_ids_are_sized(self) -> None:
        # Every ID the two model selectors can return must have a footprint
        # entry (or resolve to the conservative unknown default without error).
        for backend in (
            _CODE_BACKEND_CODESAGE, _CODE_BACKEND_QWEN3, _CODE_BACKEND_JINA,
            _CODE_BACKEND_OPENAI, _KG_BACKEND_QWEN3, _KG_BACKEND_ARCTIC,
            _KG_BACKEND_OPENAI,
        ):
            base, slot = _footprint_for(backend)
            assert base >= 0.0 and slot >= 0.0

    def test_unknown_backend_uses_conservative_default(self) -> None:
        base, slot = _footprint_for("some-future-model:latest")
        assert base > 0.0 and slot > 0.0  # never zero (would mint ceiling)

    def test_openai_remote_has_zero_local_footprint(self) -> None:
        assert _footprint_for(_CODE_BACKEND_OPENAI) == (0.0, 0.0)
        assert _footprint_for(_KG_BACKEND_OPENAI) == (0.0, 0.0)

    def test_remote_code_model_pool_only_reserves_kg_base(self) -> None:
        # OpenAI code model → zero code base → only KG base reserved.
        b = select_embedding_concurrency(
            16.0, _CODE_BACKEND_OPENAI, _KG_BACKEND_QWEN3,
            both_gpu_resident=True,
        )
        assert b.reserved_code_base_gb == pytest.approx(0.0)
        assert b.reserved_kg_base_gb == pytest.approx(1.2)
        # Remote code model has zero slot footprint → code cap is the ceiling.
        assert b.code_embed_max_concurrent == _CONCURRENCY_CEILING


# ────────────────────────────────────────────────────────────────────
# Robustness / soft-fail
# ────────────────────────────────────────────────────────────────────

class TestRobustness:
    @pytest.mark.parametrize("bad", [None, float("nan"), float("-inf"), -5.0])
    def test_bad_memory_input_never_crashes(self, bad) -> None:
        b = select_embedding_concurrency(
            bad, _CODE_BACKEND_QWEN3, _KG_BACKEND_QWEN3,
        )
        assert isinstance(b, EmbeddingConcurrencyBudget)
        assert b.code_embed_max_concurrent >= 1
        assert b.update_all_max_parallel_projects >= 1

    def test_empty_backend_ids_use_unknown_default_not_crash(self) -> None:
        b = select_embedding_concurrency(16.0, "", "")
        assert b.code_embed_max_concurrent >= 1

    def test_dataclass_is_frozen(self) -> None:
        b = select_embedding_concurrency(16.0, _CODE_BACKEND_QWEN3, _KG_BACKEND_QWEN3)
        with pytest.raises(Exception):
            b.code_embed_max_concurrent = 99  # type: ignore[misc]
