# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (owner 2026-10-15): the summary tier table and the runtime
free-VRAM floor are ONE rule.

Field case: an 8 GB RTX 4070 Laptop (~7.6 GB free at idle) was assigned
``gemma4:e4b`` (~9 GB on disk as pulled) by the hardware tier table, while
the runtime floor (size + max(1 GB, 40%)) made that model never loadable on
any 6-12 GB card — and install pulled 9 GB for nothing. The table now asks
the floor function itself whether a GPU rung fits.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import embedding_selection as es  # noqa: E402

FIELD_8GB_TOTAL_GB = 8188 / 1024.0       # RTX 4070 Laptop as nvidia-smi reports it
FIELD_8GB_FREE_MB = int(7.6 * 1024)      # free at idle, field reading


def _runtime_floor_mb(model: str) -> int:
    return es.summary_required_free_vram_mb(es.SUMMARY_MODEL_SIZE_MB[model])


def test_an_8gb_card_gets_a_model_whose_floor_fits_its_free_vram():
    model = es.summary_model_for_hardware(FIELD_8GB_TOTAL_GB, 16.0, 16)
    assert model == "qwen3.5:4b"
    assert _runtime_floor_mb(model) <= FIELD_8GB_FREE_MB
    # and the pick the field saw does NOT fit — the bug this pins
    assert _runtime_floor_mb("gemma4:e4b") > FIELD_8GB_FREE_MB


def test_a_16gb_card_keeps_qwen35_9b():
    assert es.summary_model_for_hardware(16.0, 32.0, 16) == "qwen3.5:9b"


@pytest.mark.parametrize("vram", [x / 4 for x in range(1, 4 * 48)])
def test_every_gpu_pick_fits_the_runtime_floor(vram: float):
    """Table/floor consistency over every card size up to 48 GB: whatever
    the table picks for a GPU machine, an idle card of that size satisfies
    the floor the runtime applies (the floor model excepted only when even
    it cannot fit — it is the always-pulled last resort)."""
    for ram, cores in ((8.0, 4), (64.0, 16)):
        model = es.summary_model_for_hardware(vram, ram, cores)
        free_idle_mb = vram * 1024 - es.SUMMARY_IDLE_VRAM_RESERVE_MB
        if model == es.SUMMARY_FLOOR_MODEL:
            for rung in es.SUMMARY_GPU_TIER_MODELS:
                assert _runtime_floor_mb(rung) > free_idle_mb, (vram, rung)
            continue
        assert _runtime_floor_mb(model) <= free_idle_mb, (vram, ram, cores, model)


def test_each_gpu_rung_is_chosen_exactly_from_its_floor():
    """Boundary per rung: just below the floor-derived size the rung is NOT
    picked, at it the rung IS picked."""
    for rung in es.SUMMARY_GPU_TIER_MODELS:
        edge = es.summary_min_vram_gb(rung)
        assert es.summary_model_fits_vram(rung, edge)
        assert not es.summary_model_fits_vram(rung, edge - 0.01)
    # the strongest rung wins as soon as it fits
    top = es.SUMMARY_GPU_TIER_MODELS[0]
    assert es.summary_model_for_hardware(es.summary_min_vram_gb(top), 8.0, 4) == top
    second = es.SUMMARY_GPU_TIER_MODELS[1]
    assert es.summary_model_for_hardware(es.summary_min_vram_gb(top) - 0.01, 8.0, 4) == second


def test_every_tier_model_has_a_recorded_size():
    tier_models = {t["model"] for t in es.summary_model_tiers()}
    assert tier_models <= set(es.SUMMARY_MODEL_SIZE_MB)


def test_cpu_tier_keeps_working_without_a_gpu():
    assert es.summary_model_for_hardware(0.0, 16.0, 8) == es.SUMMARY_CPU_TIER_MODEL
    assert es.summary_model_for_hardware(0.0, 8.0, 4) == es.SUMMARY_FLOOR_MODEL


def test_a_small_gpu_never_gets_the_cpu_rung_it_could_not_load():
    """A 4 GB card + capable CPU: the runtime measures the card's free VRAM,
    so gemma4:e4b (floor far above 4 GB) would be deferred forever."""
    assert es.summary_model_for_hardware(4.0, 64.0, 16) == es.SUMMARY_FLOOR_MODEL


def test_the_tier_text_admits_only_cards_that_fit():
    for tier in es.summary_model_tiers():
        if tier["model"] not in es.SUMMARY_GPU_TIER_MODELS:
            continue
        stated = float(tier["when"].split(" GB")[0])
        assert es.summary_model_fits_vram(tier["model"], stated)


def test_the_floor_tier_text_names_its_gpu_need_and_that_need_fits():
    """R20 L5: the floor model is not usable on EVERY machine — on a GPU the
    runtime free-VRAM floor still defers it below its need. The text says so,
    with a figure the floor rule itself admits."""
    floor = [t for t in es.summary_model_tiers() if t["model"] == es.SUMMARY_FLOOR_MODEL]
    assert len(floor) == 1
    when = floor[0]["when"]
    assert "on a GPU it needs" in when, when
    stated = float(when.split("needs ")[1].split(" GB")[0])
    assert es.summary_model_fits_vram(es.SUMMARY_FLOOR_MODEL, stated)
    assert not es.summary_model_fits_vram(es.SUMMARY_FLOOR_MODEL, stated - 0.1)


def _load_summary_backends():
    path = REPO_ROOT / "templates" / "scripts" / "summary_backends.py"
    spec = importlib.util.spec_from_file_location("_sb_floor_probe", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runtime_guard_uses_the_same_floor_function(monkeypatch):
    """The runtime floor IS the table's floor: patching the one home moves
    both (a second copy in summary_backends would not follow)."""
    sb = _load_summary_backends()
    monkeypatch.delenv("KG_SUMMARY_MIN_FREE_VRAM_MB", raising=False)
    for size in (None, 500, 3170, 6288, 9163):
        assert sb._required_free_vram_mb(size) == es.summary_required_free_vram_mb(size)
    monkeypatch.setattr(es, "summary_required_free_vram_mb", lambda _s: 4242)
    assert sb._min_free_vram_mb(3170) == 4242


def test_runtime_guard_defers_loudly_when_the_floor_cannot_be_sized(monkeypatch):
    sb = _load_summary_backends()
    monkeypatch.setattr(sb, "_codegraph_walk_active", lambda: False)
    monkeypatch.setattr(sb, "_ollama_model_resident", lambda _m: False)
    monkeypatch.setattr(sb, "OLLAMA_DEFAULT_MODEL", "qwen3.5:4b", raising=False)
    monkeypatch.setattr(sb, "_free_vram_mb", lambda: 7000)
    monkeypatch.delenv("KG_SUMMARY_MIN_FREE_VRAM_MB", raising=False)

    def _boom(_w):
        raise ImportError("no vco_lib")

    monkeypatch.setattr(sb, "_required_free_vram_mb", _boom)
    reason = sb._ollama_local_deferred(3170)
    assert reason is not None and "vco_lib is not importable" in reason
    # the env override still works without vco_lib
    monkeypatch.setenv("KG_SUMMARY_MIN_FREE_VRAM_MB", "100")
    assert sb._ollama_local_deferred(3170) is None
