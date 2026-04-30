"""Tests for install-time inference-model tier selection.

The install-time selector (`_inference_models_for_capability`) MUST mirror
the runtime selector ladder in
`claude_mcp_servers/ollama_mcp/server.py:TEXT_MODEL_TIERS`. If they drift,
"auto" model selection at runtime can pick a model that was never pulled
during install.

These tests are table-driven over the canonical tier ladder. Update them
in lockstep with both selectors when the ladder changes.
"""

from __future__ import annotations

import pytest

from install import (
    EMBEDDING_CONFIGS,
    SystemInfo,
    _build_ollama_pull_list,
    _inference_models_for_capability,
)


def _sysinfo(*, has_gpu: bool, vram_gb: float, ram_gb: float) -> SystemInfo:
    return SystemInfo(
        os_name="Linux",
        has_gpu=has_gpu,
        has_metal=False,
        container_cmd="docker",
        gpu_name="test",
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        gpu_vendor="nvidia" if has_gpu else "",
    )


# Each row: (label, has_gpu, vram_gb, ram_gb, expected_models)
INFERENCE_TIER_CASES = [
    # GPU paths
    ("16+ GB VRAM workstation", True, 24.0, 64.0,
     ["qwen3.5:9b", "gemma4:e4b", "qwen3:0.6b"]),
    ("8 GB VRAM mid-tier GPU", True, 8.0, 16.0,
     ["qwen3.5:9b", "gemma4:e4b", "qwen3:0.6b"]),  # 8 >= 7.5
    ("6 GB VRAM small GPU", True, 6.0, 16.0,
     ["gemma4:e4b", "qwen3:0.6b"]),
    ("4 GB VRAM laptop GPU", True, 4.0, 16.0,
     ["qwen3:0.6b"]),  # below 5 GB tier, has_gpu still set
    # CPU paths
    ("no GPU + 32 GB RAM", False, 0.0, 32.0,
     ["qwen3.5:9b", "gemma4:e4b", "qwen3:0.6b"]),
    ("no GPU + 16 GB RAM", False, 0.0, 16.0,
     ["gemma4:e4b", "qwen3:0.6b"]),
    ("no GPU + 12 GB RAM", False, 0.0, 12.0,
     ["gemma4:e4b", "qwen3:0.6b"]),
    ("no GPU + 8 GB RAM", False, 0.0, 8.0,
     ["qwen3:0.6b"]),
    # Edge cases
    ("probe failed (all zeros)", False, 0.0, 0.0,
     ["qwen3:0.6b"]),
    ("GPU detected but VRAM probe failed", True, 0.0, 16.0,
     # GPU presence with 0 VRAM falls through to RAM tiers — the
     # probe-failed safety net.
     ["gemma4:e4b", "qwen3:0.6b"]),
]


@pytest.mark.parametrize("label,has_gpu,vram,ram,expected", INFERENCE_TIER_CASES,
                         ids=[c[0] for c in INFERENCE_TIER_CASES])
def test_inference_models_for_capability(label, has_gpu, vram, ram, expected):
    sysinfo = _sysinfo(has_gpu=has_gpu, vram_gb=vram, ram_gb=ram)
    assert _inference_models_for_capability(sysinfo) == expected, (
        f"{label}: wrong model list for has_gpu={has_gpu} vram={vram} ram={ram}"
    )


def test_floor_always_includes_smallest_model():
    """qwen3:0.6b is the always-fits floor — must be in every output."""
    for case in INFERENCE_TIER_CASES:
        _, has_gpu, vram, ram, expected = case
        assert "qwen3:0.6b" in expected, (
            f"floor model missing from tier {case[0]}: {expected}"
        )


def test_low_resource_profile_pull_list_is_capped():
    """low_resource is opt-in — never layer larger inference tiers on it
    even on a workstation-class host."""
    workstation = _sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0)
    config = dict(EMBEDDING_CONFIGS["low_resource"])
    pull_list = _build_ollama_pull_list(config, workstation)
    # Must NOT include qwen3.5:9b even though host could run it.
    assert "qwen3.5:9b" not in pull_list, (
        "low_resource profile must be hard-capped, got " + str(pull_list)
    )
    assert "gemma4:e4b" in pull_list
    assert "qwen3:0.6b" in pull_list
    # Embedding side preserved.
    assert "snowflake-arctic-embed2:latest" in pull_list
    assert "unclemusclez/jina-embeddings-v2-base-code:latest" in pull_list


def test_gpu_profile_layers_inference_on_workstation():
    workstation = _sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0)
    config = dict(EMBEDDING_CONFIGS["gpu"])
    pull_list = _build_ollama_pull_list(config, workstation)
    # Embedding model from profile.
    assert "qwen3-embedding:0.6b" in pull_list
    # Top-tier inference layered on.
    assert "qwen3.5:9b" in pull_list
    assert "gemma4:e4b" in pull_list
    assert "qwen3:0.6b" in pull_list


def test_cpu_profile_low_ram_only_pulls_floor():
    tight = _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=8.0)
    config = dict(EMBEDDING_CONFIGS["cpu"])
    pull_list = _build_ollama_pull_list(config, tight)
    assert "qwen3.5:9b" not in pull_list
    assert "gemma4:e4b" not in pull_list
    assert "qwen3:0.6b" in pull_list
    # Embedding models still pulled.
    assert "qwen3-embedding:0.6b" in pull_list
    assert "unclemusclez/jina-embeddings-v2-base-code:latest" in pull_list


def test_pull_list_is_deduplicated():
    """If the inference tier and embedding list both contain qwen3:0.6b
    (it doesn't today, but defensively) the pull list must not repeat it."""
    tight = _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=8.0)
    config = dict(EMBEDDING_CONFIGS["cpu"])
    pull_list = _build_ollama_pull_list(config, tight)
    assert len(pull_list) == len(set(pull_list)), f"dups in pull list: {pull_list}"


def test_openai_profile_only_pulls_inference_models():
    """OpenAI handles embeddings — only inference models need pulling."""
    workstation = _sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0)
    config = dict(EMBEDDING_CONFIGS["openai"])
    pull_list = _build_ollama_pull_list(config, workstation)
    # No Ollama-served embedding models.
    assert "qwen3-embedding:0.6b" not in pull_list
    assert "snowflake-arctic-embed2:latest" not in pull_list
    # Inference models still pulled (for tools that need local LLM).
    assert "qwen3.5:9b" in pull_list
