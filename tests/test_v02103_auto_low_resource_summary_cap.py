"""The low_resource profile's text-generation cap (``qwen3.5:0.8b``) applies
to the explicit ``--low-resource`` opt-in only (v0.2.103).

Field case (Windows, RTX 4070 Laptop 8 GB, 2026-10-16): the tier
auto-selector mapped the Arctic KG pick to the low_resource profile and the
profile's ``inference_models_override`` came along, so install pulled the
0.8b floor although the one free-VRAM floor rule admits ``qwen3.5:4b`` on
that card. An auto-selected tier now lets the hardware ladder pick.
"""
import argparse
from unittest.mock import patch

import install  # type: ignore
from vco_lib import embedding_pull_plan as epp
from vco_lib import embedding_selection as es


def _args(**kw) -> argparse.Namespace:
    base = dict(cpu_only=False, low_resource=False, openai_key="", no_gpu_check=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _laptop_8gb() -> "install.SystemInfo":
    return install.SystemInfo(
        os_name="Windows", has_gpu=True, has_metal=False, container_cmd="docker",
        gpu_name="NVIDIA GeForce RTX 4070 Laptop GPU", vram_gb=8.0, ram_gb=15.3,
        gpu_vendor="nvidia")


def _inference_pull(cfg: dict, vram_gb: float, ram_gb: float, cores: int) -> tuple:
    """What step 7 pulls for text generation: the profile cap when the config
    carries one, else the hardware tier."""
    plan = epp.plan(
        kg_active=cfg.get("active_embedding") or "qwen3",
        dual_write_all=False, dual_arctic_secondary=False,
        code_backend=cfg.get("code_backend"), code_model=cfg.get("code_model"),
        capability_tier=[es.summary_model_for_hardware(vram_gb, ram_gb, cores)],
        profile_override=cfg.get("inference_models_override"),
    )
    return plan.inference


def test_the_8gb_field_host_auto_tier_pulls_the_4b_summary_model():
    with patch.object(install, "_probe_cpu_cores", return_value=16):
        cfg = install._choose_embedding_config(_laptop_8gb(), _args())
    assert cfg.get("active_embedding") == "arctic", cfg  # the field tier
    assert "inference_models_override" not in cfg
    assert _inference_pull(cfg, 8.0, 15.3, 16) == ("qwen3.5:4b",)


def test_the_explicit_low_resource_opt_in_keeps_the_floor_cap():
    with patch.object(install, "_probe_cpu_cores", return_value=16):
        cfg = install._choose_embedding_config(_laptop_8gb(), _args(low_resource=True))
    assert cfg.get("inference_models_override") == ["qwen3.5:0.8b"]
    assert _inference_pull(cfg, 8.0, 15.3, 16) == ("qwen3.5:0.8b",)


def test_the_shared_profile_table_is_not_mutated():
    with patch.object(install, "_probe_cpu_cores", return_value=16):
        install._choose_embedding_config(_laptop_8gb(), _args())
    assert install.EMBEDDING_CONFIGS["low_resource"]["inference_models_override"] == ["qwen3.5:0.8b"]


def _replay(prior: dict) -> dict:
    """Drive the update replay of a recorded embedding_mode choice."""
    with patch.object(install, "_load_previous_choices", return_value={"embedding_mode": prior}), \
            patch.object(install, "_record_install_choice") as rec:
        cfg = install._choose_embedding_config(_laptop_8gb(), _args())
    return cfg, rec.call_args.args[2]


def test_an_auto_recorded_low_resource_tier_replays_without_the_cap():
    cfg, recorded = _replay({"value": "low_resource", "explicit": False,
                             "reason": "replayed from last install"})
    assert "inference_models_override" not in cfg
    assert recorded["explicit"] is False


def test_an_explicit_low_resource_choice_replays_with_the_cap():
    cfg, recorded = _replay({"value": "low_resource", "explicit": True,
                             "reason": "replayed from last install"})
    assert cfg.get("inference_models_override") == ["qwen3.5:0.8b"]
    assert recorded["explicit"] is True  # carried forward through every replay


def test_legacy_records_are_explicit_only_when_their_reason_says_so():
    cfg, _ = _replay({"value": "low_resource", "reason": "user passed --low-resource"})
    assert cfg.get("inference_models_override") == ["qwen3.5:0.8b"]
    cfg, _ = _replay({"value": "low_resource",
                      "reason": "auto-selected low_resource tier (VRAM=8.0 GB, RAM=15.3 GB, cores=16)"})
    assert "inference_models_override" not in cfg
