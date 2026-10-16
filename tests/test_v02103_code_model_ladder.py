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
    """Stub the resolver's OWN seams (R41 M3): the ONE selected-card helper
    (VRAM and vendor from the same card) plus the real cpu/ram probes."""
    monkeypatch.setattr(host_probe, "selected_gpu_specs", lambda: (vram, vendor))
    monkeypatch.setattr(host_probe, "probe_system_ram_gb", lambda: ram)
    monkeypatch.setattr(host_probe, "probe_cpu_cores", lambda: cores)


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
    none of the settings env (the 5b bundle child). Drives the REAL
    ``recorded_code_model`` (the conftest W-CODE-LADDER pin mutes it for the
    rest of the suite — R41 L1)."""
    from tests import conftest
    from vco_lib import embedding_pull_plan as epp

    epp.record_profile(tmp_path, {"text_model": ARCTIC, "code_backend": "ollama",
                                  "code_model": JINA}, [])
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setattr(cem, "recorded_code_model", conftest._REAL_RECORDED_CODE_MODEL)
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


def _reconcile_args(**over):
    base = dict(cpu_only=False, low_resource=False, openai_key="", no_gpu_check=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_the_stale_active_embedding_notice_points_at_preferences(
        install, monkeypatch, capsys):
    """F4, driven behaviourally (R41 L5 — not a source scan): the
    stale-reconcile branch PRINTS a notice naming Preferences → "Embedding
    profile (global)", never the stale "Identity tab"."""
    monkeypatch.setattr(install, "_read_active_embedding_from_app_state",
                        lambda: "")
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    embed_config = {"active_embedding": "arctic"}
    assert install._reconcile_install_active_embedding(
        embed_config, _reconcile_args()) is True
    out = capsys.readouterr().out
    assert "stale for this CPU hardware" in out
    assert "Preferences → Embedding profile (global)" in out
    assert "Identity tab" not in out


def test_the_empty_env_active_embedding_notice_points_at_preferences(
        install, monkeypatch, capsys):
    """F4's empty-env-fill branch (the GUI install path) PRINTS the same
    Preferences pointer."""
    monkeypatch.setattr(install, "_read_active_embedding_from_app_state",
                        lambda: "")
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
    embed_config = {"active_embedding": "arctic"}
    assert install._reconcile_install_active_embedding(
        embed_config, _reconcile_args()) is True
    out = capsys.readouterr().out
    assert "no ACTIVE_EMBEDDING set" in out
    assert "Preferences → Embedding profile (global)" in out
    assert "Identity tab" not in out


# ═══════════════ R41 — the fix round (review V02103-R41) ══════════════════


def test_install_binds_the_real_host_probe_functions(install):
    """R41 M3: the suite's W-CODE-LADDER pin must not stub
    ``vco_lib.host_probe``'s public probes — install.py binds
    ``probe_cpu_cores`` / ``probe_system_ram_gb`` BY NAME at import, so a
    conftest-time stub would silently fake every install-path test's host
    (an identity check alone cannot catch it: the stub would be bound on
    BOTH sides). Under pytest, ``install._probe_cpu_cores`` is the REAL
    probe — a conftest lambda would carry ``conftest`` as its module."""
    assert install._probe_cpu_cores is host_probe.probe_cpu_cores
    assert install._probe_system_ram_gb is host_probe.probe_system_ram_gb
    for bound in (install._probe_cpu_cores, install._probe_system_ram_gb):
        assert bound.__module__ == "vco_lib.host_probe", bound


def test_one_selected_card_feeds_both_install_and_the_resolver(
        install, monkeypatch):
    """R41 M2: VRAM and vendor must come from ONE selected card — the
    6 GB NVIDIA + 16 GB AMD host where the old resolver read VRAM from the
    AMD card and vendor from the first NVIDIA entry (→ CodeSage on a host
    whose install pick is qwen3). The helper, install's step-[3] choice and
    the resolver's hardware leg must all answer qwen3."""
    from tests import conftest
    from vco_lib import gpu_device
    from vco_lib.gpu_device import GpuCandidate

    candidates = [
        GpuCandidate(vendor="nvidia", name="RTX 6GB", vram_gb=6.0,
                     is_integrated=False, pci_bus="0000:01"),
        GpuCandidate(vendor="amd", name="Radeon 16GB", vram_gb=16.0,
                     is_integrated=False, pci_bus="0000:02"),
    ]
    monkeypatch.setattr(gpu_device, "enumerate_gpus", lambda: candidates)
    monkeypatch.delenv("VCT_GPU_VENDOR", raising=False)
    real_specs = conftest._REAL_SELECTED_GPU_SPECS
    real_specs.cache_clear()
    monkeypatch.setattr(host_probe, "selected_gpu_specs", real_specs)

    # The ONE helper selects the AMD card (most VRAM) and reports BOTH
    # numbers from it — a GPU vendor with no CodeSage build.
    assert host_probe.selected_gpu_specs() == (16.0, "amd")

    # install's step-[3] profile choice (which reads the helper through the
    # system-info detection's vendor/VRAM) and the resolver's hardware leg
    # agree on qwen3, not CodeSage.
    cfg = _choose(install, monkeypatch, 16.0, 32.0, 8, "amd")
    assert cfg["code_model"] == QWEN3
    assert cem._hardware_code_model() == QWEN3


def test_the_suite_pins_the_install_record_rung_to_none(monkeypatch, tmp_path):
    """R41 L1: rung 3b reads the MACHINE's ``ollama_pull_profile.json`` by
    default (``VCT_INSTALL_ROOT`` is pinned to the checkout, whose
    ``summary_model._orchestrator_roots`` ladder is consulted) — on an
    installed checkout that record would answer for every test instead of
    the pinned ladder. The conftest pin mutes it; only a test that restores
    the real function (or passes ``install_record``) reads a record."""
    from vco_lib import embedding_pull_plan as epp

    epp.record_profile(tmp_path, {"text_model": ARCTIC, "code_backend": "ollama",
                                  "code_model": JINA}, [])
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(tmp_path))
    assert cem.recorded_code_model() is None


def test_a_derived_default_never_retargets_a_recorded_graph():
    """R41 L2 (coordinator ruling, per the owner rule "never fall back to a
    different embedding model"): a DERIVED default (the install record here)
    must NOT re-target a graph that already has a recorded model — the
    record wins for both roles and no fill starts. Red before: the Jina
    record would have started a fill toward CodeSage, re-embedding the whole
    graph on the machine's weakest tier without the user selecting
    anything."""
    record = cem.CodeModelRecord(model=JINA, dim=768, slot="jina_embed")
    res = cem.resolve_code_model(
        record=record, env={}, app_state={}.get,
        install_record=lambda: CODESAGE, role="write")
    assert res.model == JINA
    assert res.target == JINA and res.target_source == "recorded"
    assert res.filling is False


def test_an_explicit_selection_still_retargets_a_recorded_graph():
    """R41 L2's other arm: an EXPLICIT selection (CODE_EMBED_MODEL env —
    likewise the Codegraph tab and the Preferences pin/default) still
    starts the additive fill toward the selected model."""
    record = cem.CodeModelRecord(model=JINA, dim=768, slot="jina_embed")
    res = cem.resolve_code_model(
        record=record, env={"CODE_EMBED_MODEL": QWEN3}, app_state={}.get,
        role="write")
    assert res.filling is True
    assert res.model == QWEN3 and res.target == QWEN3
    assert res.carry == JINA


def test_the_gpu_backend_ignore_line_prints_once_per_process(monkeypatch, capsys):
    """R41 L3: the "ignoring CODE_EMBED_BACKEND=gpu" stderr line is emitted
    ONCE per process, not on every resolution."""
    monkeypatch.setattr(cem, "_gpu_backend_ignore_notice_shown", False)
    _stub_probes(monkeypatch, 8.0, 16.0, 8, "nvidia")  # ladder says qwen3
    for _ in range(3):
        assert cem.resolve_code_model_pick(
            {"CODE_EMBED_BACKEND": "gpu"}, app_state={}.get,
            install_record=lambda: None) == (QWEN3, "hardware-probe")
    err = capsys.readouterr().err
    assert err.count("ignoring CODE_EMBED_BACKEND=gpu") == 1


def test_the_rebuilt_label_never_says_cpu_only_on_a_gpu_host(install, monkeypatch):
    """R41 L4: a 6-12 GB NVIDIA (or any >= 12 GB AMD) host lands on the cpu
    PROFILE, but its Ollama runs on the GPU — the rebuilt label's prefix
    follows the detected hardware, and the serving phrase follows the FINAL
    code model, not the profile's un-swapped code_backend."""
    for vram, vendor in ((10.0, "nvidia"), (16.0, "amd")):
        cfg = _choose(install, monkeypatch, vram, 32.0, 8, vendor)
        assert cfg["code_model"] == QWEN3
        assert cfg["description"].startswith("GPU-accelerated ("), cfg["description"]
        assert "qwen3 code, both via Ollama" in cfg["description"]
        assert "CPU-only" not in cfg["description"]


def test_a_cpu_only_host_keeps_the_cpu_only_prefix(install, monkeypatch):
    """R41 L4's leave-alone arm: no GPU → the cpu profile's "CPU-only"
    prefix stays (and the helper's has_gpu=False correction is not applied
    to a prefix that is already right)."""
    cfg = _choose(install, monkeypatch, 0.0, 32.0, 8, "")
    assert cfg["description"].startswith("CPU-only (")
    assert cfg["code_model"] == QWEN3
