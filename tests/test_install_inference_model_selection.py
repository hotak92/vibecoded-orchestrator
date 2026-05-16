# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for install-time embedding-model selection.

Originally this file also covered install-time inference-model tier
selection in lockstep with the Ollama MCP's `TEXT_MODEL_TIERS` runtime
ladder. In v0.2.11 the Ollama MCP server was removed (its tools were
redundant with Claude's native chat + Read + vision), so the
inference-tier table tests were dropped. What remains here only
exercises embedding-model selection — those models are still required
by Weaviate (text + code vectors) regardless of whether any LLM tools
ship in the MCP layer.

Future work: when the install pipeline is refactored to stop pulling
inference models entirely (PR-14b), the test names below may shift.
The assertions only touch embedding models, so they should survive any
PR-14b cleanup of inference-tier logic in `install.py`.
"""

from __future__ import annotations

from install import (
    EMBEDDING_CONFIGS,
    SystemInfo,
    _build_ollama_pull_list,
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


def test_gpu_profile_pulls_qwen3_text_embedding():
    """GPU profile uses Ollama for text embeddings (CodeSage on GPU for code)."""
    workstation = _sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0)
    config = dict(EMBEDDING_CONFIGS["gpu"])
    pull_list = _build_ollama_pull_list(config, workstation)
    assert "qwen3-embedding:0.6b" in pull_list, (
        f"qwen3-embedding:0.6b missing from gpu profile pull list: {pull_list}"
    )


def test_cpu_profile_pulls_both_text_and_code_embeddings():
    """CPU profile serves text + code embeddings via Ollama (no GPU service)."""
    cpu_host = _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=16.0)
    config = dict(EMBEDDING_CONFIGS["cpu"])
    pull_list = _build_ollama_pull_list(config, cpu_host)
    assert "qwen3-embedding:0.6b" in pull_list, (
        f"text embedding missing from cpu profile: {pull_list}"
    )
    assert "unclemusclez/jina-embeddings-v2-base-code:latest" in pull_list, (
        f"code embedding missing from cpu profile: {pull_list}"
    )


def test_openai_profile_pulls_no_embeddings():
    """OpenAI profile handles embeddings via API — no Ollama embedding pulls."""
    workstation = _sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0)
    config = dict(EMBEDDING_CONFIGS["openai"])
    pull_list = _build_ollama_pull_list(config, workstation)
    assert "qwen3-embedding:0.6b" not in pull_list, (
        f"openai profile pulled an embedding model it shouldn't: {pull_list}"
    )
    assert "snowflake-arctic-embed2:latest" not in pull_list, (
        f"openai profile pulled an embedding model it shouldn't: {pull_list}"
    )


def test_low_resource_profile_pulls_arctic_and_jina_code_embeddings():
    """low_resource profile uses Snowflake Arctic for text, Jina v2 for code."""
    tight = _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=8.0)
    config = dict(EMBEDDING_CONFIGS["low_resource"])
    pull_list = _build_ollama_pull_list(config, tight)
    assert "snowflake-arctic-embed2:latest" in pull_list, (
        f"arctic embedding missing from low_resource profile: {pull_list}"
    )
    assert "unclemusclez/jina-embeddings-v2-base-code:latest" in pull_list, (
        f"jina code embedding missing from low_resource profile: {pull_list}"
    )


def test_pull_list_is_deduplicated():
    """Pull list must not repeat the same model name, even if it appears in
    both the embedding list and any inference tier."""
    tight = _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=8.0)
    for profile_name in ("cpu", "gpu", "low_resource", "openai"):
        config = dict(EMBEDDING_CONFIGS[profile_name])
        pull_list = _build_ollama_pull_list(config, tight)
        assert len(pull_list) == len(set(pull_list)), (
            f"duplicates in {profile_name} pull list: {pull_list}"
        )
