# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — ONE hardware-aware rule for the code-embedding model.

Field case (V02103-CODE-EMBED-MODEL-DIAG-2026-10-16): a fresh install on an
8 GB RTX 4070 Laptop picked qwen3-embedding:0.6b correctly, but every OTHER
consumer could disagree — the profile label still said "Jina V2 code", and
``code_embed_model.resolve_code_model_pick``'s hardware-blind tail answered
``codesage-large-v2`` (builtin) or, via the fixed ``ollama → qwen3`` mapping,
qwen3 even on a Jina-tier host. The fixes pinned here:

* F1 — the resolver's tail is the install RECORD, then the hardware ladder
  itself (``select_code_model_for_host``, the SAME selector install's profile
  choice uses, AMD→qwen3 included). The codesage builtin and the fixed
  ollama→qwen3 mapping are gone; CodeSage is never selected below the
  ladder's 12 GB threshold.
* F2 — the profile description is rebuilt from the FINAL models, so the
  step-[2b] label never says "Jina" when the code model is qwen3.
* F4 — the ACTIVE_EMBEDDING notices point at Preferences → "Embedding
  profile (global)" (global since the v0.2.102 W6-R change), not the stale
  "Identity tab".

Every test drives real code paths with fakes; nothing reaches a live
Weaviate or probes real hardware (the ladder legs stub
:mod:`vco_lib.host_probe`).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from vco_lib import code_embed_model as cem
from vco_lib import host_probe
from vco_lib import gpu_profile
from vco_lib.embedding_selection import select_code_model_for_host

REPO_ROOT = Path(__file__).resolve().parents[1]

QWEN3 = "qwen3-embedding:0.6b"
JINA = "unclemusclez/jina-embeddings-v2-base-code:latest"
CODESAGE = "codesage-large-v2"
ARCTIC = "snowflake-arctic-embed2:latest"

#: (vram_gb, ram_gb, cores, gpu_vendor) — one point per interesting rung,
#: including the field case (8.0 GB) and the AMD correction (16 GB AMD).
SWEEP = [
    (0.0, 8.0, 4, ""),          # weak CPU → jina / arctic
    (0.0, 32.0, 12, ""),        # capable CPU → qwen3 (strict RAM > 24)
    (0.0, 24.0, 8, ""),         # exactly 24 GB → the strict boundary: jina
    (2.0, 16.0, 4, "nvidia"),   # 2 GB card → CPU path → jina
    (4.0, 16.0, 8, "nvidia"),   # 2–6 GB → jina
    (6.0, 16.0, 8, "nvidia"),   # 6 GB (inclusive) → qwen3
    (8.0, 15.3, 8, "nvidia"),   # THE FIELD CASE → qwen3
    (11.9, 32.0, 8, "nvidia"),  # just below the codesage rung → qwen3
    (12.0, 32.0, 8, "nvidia"),  # 12 GB (inclusive) → codesage
    (16.0, 32.0, 8, "nvidia"),  # workstation → codesage
    (16.0, 32.0, 8, "amd"),     # AMD has no ROCm CodeSage → qwen3
    (12.0, 32.0, 8, "amd"),     # the correction at its boundary
]


def _stub_probes(monkeypatch, vram, ram, cores, vendor):
    monkeypatch.setattr(host_probe, "probe_gpu_vram_gb", lambda: vram)
    monkeypatch.setattr(host_probe, "probe_system_ram_gb", lambda: ram)
    monkeypatch.setattr(host_probe, "probe_cpu_cores", lambda: cores)
    monkeypatch.setattr(cem, "_gpu_vendor", lambda: vendor)


@pytest.fixture
def install():
    sys.path.insert(0, str(REPO_ROOT))
    import install  # noqa: PLC0415

    return install


def _choose(install, monkeypatch, vram, ram, cores, vendor):
    """install.py's step-[3] profile choice, hermetic (no replay, no writes)."""
    monkeypatch.setattr(install, "_record_install_choice", lambda *a, **k: None)
    monkeypatch.setattr(install, "_load_previous_choices", lambda: {})
    monkeypatch.setattr(install, "_probe_cpu_cores", lambda: cores)
    sysinfo = install.SystemInfo(
        os_name="Linux", has_gpu=vram > 0, has_metal=False, container_cmd="podman",
        gpu_name="GPU" if vram else "", vram_gb=vram, ram_gb=ram, gpu_vendor=vendor)
    args = argparse.Namespace(cpu_only=False, low_resource=False,
                              openai_key="", no_gpu_check=False)
    return install._choose_embedding_config(sysinfo, args)


# ═══════════════════════════ the ladder rungs ═══════════════════════════


@pytest.mark.parametrize("vram,ram,cores,vendor", SWEEP)
def test_codesage_never_below_the_ladders_threshold(vram, ram, cores, vendor):
    """The invariant behind F1: no consumer path can hand a sub-12 GB host
    (or any AMD host) the CodeSage model its hardware cannot serve."""
    pick = select_code_model_for_host(vram, ram, cores, vendor)
    if vram < 12.0 or vendor == "amd":
        assert pick != CODESAGE


@pytest.mark.parametrize("vram,ram,cores,vendor,want", [
    (8.0, 15.3, 8, "nvidia", QWEN3),   # the field case
    (4.0, 16.0, 8, "nvidia", JINA),
    (16.0, 32.0, 8, "nvidia", CODESAGE),
    (16.0, 32.0, 8, "amd", QWEN3),     # the AMD rule, through the ONE selector
    (0.0, 8.0, 4, "", JINA),
])
def test_resolver_tail_answers_with_the_ladder(monkeypatch, vram, ram, cores, vendor, want):
    """No env, no pin, no app_state default, no install record → the pick is
    the hardware ladder's answer (the same selector install uses), never a
    builtin codesage."""
    _stub_probes(monkeypatch, vram, ram, cores, vendor)
    assert cem._hardware_code_model() == want
    assert cem.resolve_code_model_pick(
        {}, app_state={}.get, install_record=lambda: None) == (want, "hardware-probe")


def test_the_recorded_install_profile_answers_before_the_ladder(monkeypatch, tmp_path):
    """3b: with a record on disk (step 7), the resolver answers with it — the
    same value the pull plan and compose derive from — in processes that carry
    none of the settings env (the 5b bundle child)."""
    from vco_lib import embedding_pull_plan as epp

    epp.record_profile(tmp_path, {"text_model": ARCTIC, "code_backend": "ollama",
                                  "code_model": JINA}, [])
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(tmp_path))
    assert cem.recorded_code_model() == JINA
    _stub_probes(monkeypatch, 16.0, 32.0, 8, "nvidia")  # ladder would say codesage
    assert cem.resolve_code_model_pick(
        {}, app_state={}.get) == (JINA, "install-record")


# ═════════════════ the sweep: every consumer, ONE rule ═══════════════════


@pytest.mark.parametrize("vram,ram,cores,vendor", SWEEP)
def test_every_consumer_agrees_with_the_one_rule(
        install, monkeypatch, tmp_path, vram, ram, cores, vendor):
    """install's profile choice, the resolver's no-record pick, compose's
    service model / in-stack marker and the pull-plan record all answer the
    SAME model for the same hardware."""
    from vco_lib import embedding_pull_plan as epp
    from vco_lib.code_embed_serving import CODE_EMBED_GPU_MODEL
    from vco_lib.compose_env import code_embed_container_in_stack, code_embed_service_model

    ladder = select_code_model_for_host(vram, ram, cores, vendor)

    # 1. install.py's profile choice (step [3])
    cfg = _choose(install, monkeypatch, vram, ram, cores, vendor)
    assert cfg["code_model"] == ladder

    # 2. the resolver's no-record pick (stubbed probes → the same selector)
    _stub_probes(monkeypatch, vram, ram, cores, vendor)
    assert cem.resolve_code_model_pick(
        {}, app_state={}.get, install_record=lambda: None) == (ladder, "hardware-probe")

    # 3. compose: the container loads the model the rule chose, and runs at
    #    all only when that model is CodeSage
    service_cfg = dict(cfg, gpu_vendor=vendor, has_gpu=vram > 0)
    if ladder == CODESAGE:
        assert code_embed_service_model(service_cfg) == CODE_EMBED_GPU_MODEL
        assert code_embed_container_in_stack(service_cfg) is True
    else:
        assert code_embed_service_model(service_cfg) == ladder
        assert code_embed_container_in_stack(service_cfg) is False

    # 4. the pull plan: the recorded profile IS the rule's answer, and the
    #    plan pulls it (Ollama-served models) or names the service (CodeSage)
    epp.record_profile(tmp_path, cfg, [])
    assert epp.read_record(tmp_path)["code_model"] == ladder
    pp = epp.plan_from_machine(tmp_path)
    if ladder == CODESAGE:
        assert pp.code_embedding == ()
        assert any("Code_embed service" in r or "code_embed service" in r
                   for r in pp.rationale)
    else:
        assert ladder in pp.embedding


# ═══════════════════════════ F2 — the label ═══════════════════════════


def test_the_field_cases_label_names_the_final_code_model(install, monkeypatch):
    """8 GB → low_resource profile with the code override to qwen3: the
    description must say qwen3 and must NOT promise Jina."""
    cfg = _choose(install, monkeypatch, 8.0, 15.3, 8, "nvidia")
    assert cfg["code_model"] == QWEN3
    assert "qwen3" in cfg["description"]
    assert "Jina" not in cfg["description"]


def test_the_label_rebuild_keeps_the_profile_prefix_and_serving():
    """The stock prefix (the profile key's own wording) survives; the body is
    regenerated from the final models and the code backend."""
    from vco_lib.embedding_selection import (
        _CODE_BACKEND_QWEN3, _KG_BACKEND_ARCTIC,
    )

    cfg = {"text_model": ARCTIC, "text_dims": 1024, "code_model": JINA,
           "code_dims": 768, "code_backend": "ollama", "active_embedding": "arctic",
           "description": "Low-resource (Arctic text + Jina V2 code, both via Ollama)"}
    gpu_profile.apply_tier_overrides(
        cfg, code_pick=_CODE_BACKEND_QWEN3, kg_pick=_KG_BACKEND_ARCTIC,
        model_dims={_CODE_BACKEND_QWEN3: 1024, _KG_BACKEND_ARCTIC: 1024},
        text_model_active_embedding={})
    assert cfg["description"] == "Low-resource (Arctic text + qwen3 code, both via Ollama)"


def test_an_unswapped_profile_keeps_its_stock_description():
    cfg = {"text_model": QWEN3, "code_model": CODESAGE, "code_backend": "gpu",
           "description": "GPU-accelerated (qwen3 text + CodeSage code, best quality)"}
    gpu_profile.apply_tier_overrides(
        cfg, code_pick=CODESAGE, kg_pick=QWEN3, model_dims={}, text_model_active_embedding={})
    assert cfg["description"] == "GPU-accelerated (qwen3 text + CodeSage code, best quality)"


# ═══════════════════════════ F4 — the notices ═══════════════════════════


def test_the_active_embedding_notices_point_at_preferences():
    """The global selector has lived in Preferences → "Embedding profile
    (global)" since v0.2.102 W6-R; the stale "Identity tab" hint is gone."""
    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    assert "Identity tab" not in src
    # Both notices (stale-reconcile + empty-env fill) carry the new pointer.
    assert src.count("Preferences → Embedding profile (global) to override") == 2
