# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R2-17 — dual-RL-log resolver short-circuits before the query embed when no
distinct OTHER text slot can exist.

``_resolve_dual_rl_log_inputs`` ran the full ``embed_text_all_configured`` fan-out
on EVERY search when dual-log was on, even when the configured text-slot set has
only one model (the common VCO_dev config: qwen3 active, no distinct secondary) —
one wasted query embed per search that always resolves to None. The fix probes the
env-only ``configured_text_models()`` SSOT first and returns None (no embed) when
it is length ≤ 1.

Red-proof: with dual-log ON and a single configured model, the embed fan-out must
NOT be called; with two configured models it IS called (the optimization must not
disable a legitimate dual-log).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytest.importorskip(
    "weaviate_mcp.rl_enrichment",
    reason="weaviate_mcp.rl_enrichment must be importable for the R2-17 test",
)


def _rle():
    import importlib
    return importlib.import_module("weaviate_mcp.rl_enrichment")


def _srv():
    import importlib
    return importlib.import_module("weaviate_mcp.server")


class _SpyService:
    """Records whether the embed fan-out was called."""

    def __init__(self, slots):
        self._slots = slots
        self.fanout_calls = 0

    def embed_text_all_configured(self, query):
        self.fanout_calls += 1
        return self._slots


def test_single_model_short_circuits_no_embed(monkeypatch):
    """dual-log ON + configured_text_models length-1 → None WITHOUT embedding."""
    rle = _rle()
    srv = _srv()
    spy = _SpyService({"qwen3_embed": [0.1] * 8})
    monkeypatch.setattr(srv, "_resolve_dual_rl_log_enabled", lambda: True)
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: spy)
    # Force the SSOT to report a single configured model.
    monkeypatch.setattr(
        "vco_lib.embedding_service.configured_text_models",
        lambda: ["qwen3-embedding:0.6b"],
    )

    result = asyncio.run(rle._resolve_dual_rl_log_inputs("some query", "qwen3_embed"))
    assert result is None, "single-model config must not dual-log"
    assert spy.fanout_calls == 0, (
        "R2-17: the embed fan-out must be short-circuited BEFORE embedding when "
        "only one text slot is configured (no distinct OTHER slot can exist)"
    )


def test_two_models_still_runs_fanout(monkeypatch):
    """dual-log ON + two configured models → the fan-out DOES run (the
    optimization must not disable a legitimate dual-log)."""
    rle = _rle()
    srv = _srv()
    # Two distinct slots present so a real OTHER slot resolves.
    spy = _SpyService({"qwen3_embed": [0.1] * 8, "arctic2_embed": [0.2] * 8})
    monkeypatch.setattr(srv, "_resolve_dual_rl_log_enabled", lambda: True)
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: spy)
    monkeypatch.setattr(
        "vco_lib.embedding_service.configured_text_models",
        lambda: ["snowflake-arctic-embed2:latest", "qwen3-embedding:0.6b"],
    )

    result = asyncio.run(rle._resolve_dual_rl_log_inputs("some query", "qwen3_embed"))
    assert spy.fanout_calls == 1, "two-model config must still run the fan-out"
    # A distinct other slot was resolved (arctic2_embed != active qwen3_embed).
    assert result is not None
    assert result["other_slot"] == "arctic2_embed"


def test_probe_failure_falls_through_to_fanout(monkeypatch):
    """A broken configured_text_models probe must FAIL-OPEN: fall through to the
    full fan-out rather than silently disabling dual-log."""
    rle = _rle()
    srv = _srv()
    spy = _SpyService({"qwen3_embed": [0.1] * 8, "arctic2_embed": [0.2] * 8})
    monkeypatch.setattr(srv, "_resolve_dual_rl_log_enabled", lambda: True)
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: spy)

    def _boom():
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(
        "vco_lib.embedding_service.configured_text_models", _boom
    )
    result = asyncio.run(rle._resolve_dual_rl_log_inputs("q", "qwen3_embed"))
    assert spy.fanout_calls == 1, "a broken probe must fall through to the fan-out"
    assert result is not None and result["other_slot"] == "arctic2_embed"


def test_wpo_arctic_secondary_dual_log_end_to_end(monkeypatch):
    """WP-O RED-PROOF (i, dual-log leg): with the REAL ``configured_text_models``
    (arctic flag ON, qwen3 active), the dual-RL-log resolver must yield an
    ``arctic2_embed`` OTHER slot tagged ``arctic`` — i.e. an arctic-tagged dual-log
    event is produced on a qwen3-active install. On pre-WP-O code
    ``configured_text_models`` reports a single model (no arctic secondary) so the
    R2-17 short-circuit returns None → NO arctic dual-log → this FAILS.
    """
    rle = _rle()
    srv = _srv()
    # A real fan-out that returns both slots (mirrors embed_text_all_configured
    # with the arctic secondary on). The KEY assertion is that the resolver even
    # GETS here — pre-WP-O the short-circuit returns None before any embed.
    spy = _SpyService({"qwen3_embed": [0.1] * 1024, "arctic2_embed": [0.2] * 1024})
    monkeypatch.setattr(srv, "_resolve_dual_rl_log_enabled", lambda: True)
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: spy)
    # REAL configured_text_models — driven by the actual WP-O env flags.
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")

    result = asyncio.run(rle._resolve_dual_rl_log_inputs("q", "qwen3_embed"))
    assert result is not None, (
        "WP-O: qwen3-active + arctic-secondary must NOT short-circuit — a distinct "
        "arctic OTHER slot exists, so dual-log must run"
    )
    assert spy.fanout_calls == 1, "the real fan-out must run (two configured models)"
    assert result["other_slot"] == "arctic2_embed"
    assert result["other_source"] == "arctic", "the dual-log event must carry the arctic tag"
    assert result["other_dim"] == 1024, "arctic is 1024-dim"
    assert "arctic" in result["other_model"].lower()
