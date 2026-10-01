# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 - the "RL-Scored Retrieval" switch as the GLOBAL DEFAULT of the
per-project RL toggle.

The hub resolves ``rl_reranker_enabled_for_project`` (per-project row, else
the host-wide row the switch writes, else the system default, which is OFF
for the RL reranker) and serves it in ``ProjectConfig``. The MCP decides RL
scoring in ``search_pipeline._resolve_rl_enabled``. These tests pin, at the
Python end of that chain:

* the value the hub serves is what decides scoring (inherits / off);
* the licence gate still sits in front of it (a ``True`` toggle never turns
  scoring on for a free tier);
* event LOGGING is independent of it in every state (emit fires whether the
  switch resolves off or on).
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_mcp_servers.rl_client import search_pipeline


def _fake_license(feature_on: bool):
    """Install a stub ``VCThelpers.license`` whose gate answers ``feature_on``."""
    lic = types.ModuleType("VCThelpers.license")
    lic.feature_enabled = lambda *a, **k: feature_on  # type: ignore[attr-defined]
    pkg = types.ModuleType("VCThelpers")
    pkg.license = lic  # type: ignore[attr-defined]
    return {"VCThelpers": pkg, "VCThelpers.license": lic}


def _resolve(feature_on: bool, cfg) -> bool:
    import claude_mcp_servers.weaviate_mcp.server as srv

    with patch.dict(sys.modules, _fake_license(feature_on)), patch.object(
        srv, "_try_resolve_project_config", return_value=cfg
    ):
        return search_pipeline._resolve_rl_enabled()


class TestResolverFollowsTheServedToggle:
    def test_switch_off_no_row_resolves_scoring_off_even_with_a_licence(self):
        # No rows anywhere: the hub serves the RL system default, False.
        assert _resolve(True, SimpleNamespace(rl_reranker_enabled_for_project=False)) is False

    def test_global_default_on_is_inherited_when_licensed(self):
        assert _resolve(True, SimpleNamespace(rl_reranker_enabled_for_project=True)) is True

    def test_licence_gate_is_unchanged_a_true_toggle_never_beats_free_tier(self):
        assert _resolve(False, SimpleNamespace(rl_reranker_enabled_for_project=True)) is False

    def test_hub_unreachable_still_falls_open_to_the_licence_decision(self):
        # Unchanged contract: no config => the licence alone decides.
        assert _resolve(True, None) is True
        assert _resolve(False, None) is False


@pytest.mark.parametrize("scoring_resolves", [False, True])
def test_event_logging_is_independent_of_the_scoring_switch(scoring_resolves):
    """The retrieval event is emitted exactly once whether RL scoring
    resolves off or on: the switch gates reranking, never collection."""
    req = search_pipeline.RerankRequest(
        query="q",
        candidates=[{"title": "A", "score": 0.9, "n_emb": [0.2] * 8}],
        limit=5,
        query_emb=[0.1] * 8,
        embedding_source="ollama",
        embedding_dim=8,
        embedding_model="m",
        task_id="t-indep",
        task_type="mcp_interactive",
        session_id="s",
        spawn_answer_monitor=False,
    )

    async def _go():
        with patch.object(
            search_pipeline, "_resolve_rl_enabled", return_value=scoring_resolves
        ), patch.object(
            search_pipeline, "_do_rerank", return_value=None
        ), patch.object(
            search_pipeline, "_retrieval_emit_has_consumer", return_value=True
        ), patch.object(
            search_pipeline, "emit_rl_event", return_value=True
        ) as emit:
            res = await search_pipeline.rerank_and_emit(req)
        return emit, res

    emit, res = asyncio.run(_go())
    emit.assert_called_once()
    assert res.emit_success is True


def test_the_logging_gate_never_reads_the_scoring_switch():
    """Structural guard for the same property: the consumer probe that
    decides whether an event is written consults only the logging / upload
    flags, not the RL-reranker enable field."""
    import inspect

    src = inspect.getsource(search_pipeline._retrieval_emit_has_consumer)
    assert "rl_reranker_enabled_for_project" not in src
    assert "_resolve_rl_enabled" not in src
