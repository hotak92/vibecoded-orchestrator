# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-J — tests for the canonical rerank-and-emit pipeline.

Covers ``claude_mcp_servers/rl_client/search_pipeline.py`` (``rerank_and_emit``
+ supporting dataclasses ``RerankRequest`` / ``RerankResult``). Pre-V52-J
the rerank + telemetry-emit + answer-monitor spawn lived inline in
``weaviate_mcp.server._rl_cache_and_rerank`` and was reached by ad-hoc
imports from CLI scripts; this module centralises the logic so every entry
point routes through the same chokepoint.

Test surface:
1. Returns non-empty ``ranked`` when candidates non-empty.
2. Free-tier path (``feature_enabled("rl_retrieval") == False``) returns
   the Weaviate input order, doesn't hit the RL container.
3. Telemetry emit fires regardless of rerank path (free-tier still
   accumulates training corpus).
4. Answer-monitor spawn happens iff ``spawn_answer_monitor=True`` AND
   candidates list non-empty.
5. The frozen-dataclass request shape rejects mutation.

Sister A is concurrently refactoring ``_rl_cache_and_rerank`` to call
``rerank_and_emit``. To avoid coupling, we mock the deepest dependencies
(``_get_rl_client``, ``_rl_node_content_cache``, ``feature_enabled``)
rather than importing the in-flight refactor.

Async-test pattern: pytest-asyncio is not a declared dependency.  Each
test that needs an async body wraps it in ``asyncio.run`` -- mirrors the
pattern in ``tests/test_wrapper_mcp_base.py``.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

# Skip cleanly if sister B's branch hasn't merged yet.
search_pipeline = pytest.importorskip(
    "claude_mcp_servers.rl_client.search_pipeline",
    reason="V52-J search_pipeline lands in sister B's branch; skip if not yet merged",
)


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Fixtures + helpers
# ----------------------------------------------------------------------


def _make_request(**overrides):
    """RerankRequest with sensible defaults."""
    defaults = {
        "query": "where does the KG collection name come from?",
        "candidates": [
            {"title": "ConfigDoc", "score": 0.91, "n_emb": [0.2] * 1024},
            {"title": "InstallNotes", "score": 0.74, "n_emb": [0.3] * 1024},
            {"title": "TestFixture", "score": 0.55, "n_emb": [0.1] * 1024},
        ],
        "limit": 5,
        "query_emb": [0.1] * 1024,
        "embedding_source": "ollama",
        "embedding_dim": 1024,
        "embedding_model": "qwen3-embedding:0.6b",
        "task_id": "test-task-1",
        "task_type": "mcp_interactive",
        "session_id": "test-sess-1",
        "spawn_answer_monitor": False,  # default to False — most tests don't care
    }
    defaults.update(overrides)
    return search_pipeline.RerankRequest(**defaults)


# ----------------------------------------------------------------------
# 1. Basic shape
# ----------------------------------------------------------------------


def test_returns_ranked_non_empty_when_candidates_non_empty():
    """The pipeline must always return at least the Weaviate input order
    when given candidates, even if every dependency below soft-fails."""
    async def _inner():
        req = _make_request()
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False):
            result = await search_pipeline.rerank_and_emit(req)
        assert isinstance(result, search_pipeline.RerankResult)
        assert len(result.ranked) > 0
        assert len(result.ranked) <= req.limit
    _run(_inner())


def test_returns_empty_when_candidates_empty():
    """Empty candidates returns empty ranked; no rerank attempted."""
    async def _inner():
        req = _make_request(candidates=[])
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False):
            result = await search_pipeline.rerank_and_emit(req)
        assert result.ranked == []
        assert result.rl_used is False
    _run(_inner())


def test_task_id_echoed_or_generated():
    """The result echoes the request's task_id when supplied; otherwise
    generates a fresh uuid4-hex string."""
    async def _inner():
        req = _make_request(task_id="explicit-id-42")
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False):
            result = await search_pipeline.rerank_and_emit(req)
        assert result.task_id == "explicit-id-42"

        req_no_id = _make_request(task_id=None)
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False):
            result_no_id = await search_pipeline.rerank_and_emit(req_no_id)
        assert result_no_id.task_id
        assert len(result_no_id.task_id) == 32  # uuid4().hex
    _run(_inner())


# ----------------------------------------------------------------------
# 2. Free-tier path
# ----------------------------------------------------------------------


def test_free_tier_skips_rerank_returns_weaviate_order():
    """When feature_enabled('rl_retrieval') is False, the pipeline does
    NOT call _do_rerank; ranked is the input order truncated to limit."""
    async def _inner():
        req = _make_request(limit=2)
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_do_rerank") as mock_rerank:
            result = await search_pipeline.rerank_and_emit(req)
        mock_rerank.assert_not_called()
        assert result.rl_used is False
        # Weaviate order preserved.
        assert [n["title"] for n in result.ranked] == ["ConfigDoc", "InstallNotes"]
    _run(_inner())


def test_pro_tier_calls_rerank_when_candidates_present():
    """When RL is enabled, the pipeline routes through _do_rerank with
    the candidates list. Free-tier behaviour confirmed in prior test."""
    async def _inner():
        req = _make_request()

        async def fake_rerank(**kwargs):
            # Return candidates in reverse order so we can assert _do_rerank
            # was actually used vs the Weaviate-order fallback.
            return list(reversed(kwargs["candidates"]))[: kwargs["limit"]]

        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
             patch.object(search_pipeline, "_do_rerank", side_effect=fake_rerank) as mock_rerank:
            result = await search_pipeline.rerank_and_emit(req)
        mock_rerank.assert_awaited_once()
        assert result.rl_used is True
        # Reversed candidates means TestFixture comes first.
        assert result.ranked[0]["title"] == "TestFixture"
    _run(_inner())


def test_rerank_failure_falls_back_to_weaviate_order():
    """When _do_rerank returns None (structural error / client missing),
    the pipeline falls back to the Weaviate input order rather than
    raising or returning empty."""
    async def _inner():
        req = _make_request()
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
             patch.object(search_pipeline, "_do_rerank", return_value=None):
            result = await search_pipeline.rerank_and_emit(req)
        assert result.rl_used is False
        # First candidate by Weaviate score.
        assert result.ranked[0]["title"] == "ConfigDoc"
    _run(_inner())


# ----------------------------------------------------------------------
# 3. Telemetry emit fires even when rerank skipped
# ----------------------------------------------------------------------


def test_telemetry_emit_fires_on_free_tier():
    """Free-tier installs still emit retrieval events — the historical
    corpus accumulates regardless of license. This is the load-bearing
    "upgrade-path users get a head start" behaviour."""
    async def _inner():
        req = _make_request()
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "emit_rl_event", return_value=True) as mock_emit:
            result = await search_pipeline.rerank_and_emit(req)
        mock_emit.assert_called_once()
        assert result.emit_success is True
    _run(_inner())


def test_telemetry_emit_fires_on_pro_tier():
    """Pro-tier obviously needs the emit; assert it explicitly so a
    regression that gates emit on tier surfaces immediately."""
    async def _inner():
        req = _make_request()

        async def fake_rerank(**_):
            return req.candidates[: req.limit]

        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
             patch.object(search_pipeline, "_do_rerank", side_effect=fake_rerank), \
             patch.object(search_pipeline, "emit_rl_event", return_value=True) as mock_emit:
            result = await search_pipeline.rerank_and_emit(req)
        mock_emit.assert_called_once()
        assert result.emit_success is True
    _run(_inner())


def test_emit_validation_soft_path_does_not_raise():
    """Per ab706bd V52-J refactor's validation tiers: empty/None
    query_emb is SOFT (debug-log + still write), NOT STRICT (raise).

    The user-facing search must never break because telemetry had a
    soft-warn. emit_rl_event returns True in the SOFT path because the
    event WAS written — just with the warning logged. The hard-validation
    cases (empty query, empty task_id) remain STRICT and DO produce
    emit_success=False via the surrounding try/except in rerank_and_emit
    — tested separately.
    """
    async def _inner():
        req = _make_request(query_emb=None)  # SOFT path: warn but write
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False):
            # Must not raise.
            result = await search_pipeline.rerank_and_emit(req)
        # ranked still populated.
        assert len(result.ranked) > 0
        # Per SOFT-tier: emit succeeds + warning logged.
        assert result.emit_success is True
    _run(_inner())


# ----------------------------------------------------------------------
# 4. Answer-monitor spawn gating
# ----------------------------------------------------------------------


def test_answer_monitor_spawned_when_flag_true_and_candidates_present():
    """spawn_answer_monitor=True + non-empty candidates → spawn."""
    async def _inner():
        req = _make_request(spawn_answer_monitor=True)
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_spawn_answer_monitor") as mock_spawn:
            await search_pipeline.rerank_and_emit(req)
        mock_spawn.assert_called_once()
    _run(_inner())


def test_answer_monitor_not_spawned_when_flag_false():
    """spawn_answer_monitor=False → no spawn regardless of candidates."""
    async def _inner():
        req = _make_request(spawn_answer_monitor=False)
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_spawn_answer_monitor") as mock_spawn:
            await search_pipeline.rerank_and_emit(req)
        mock_spawn.assert_not_called()
    _run(_inner())


def test_answer_monitor_not_spawned_when_candidates_empty():
    """spawn_answer_monitor=True but empty candidates → no spawn.
    Without candidates the monitor would have nothing to compute
    citations against — spawning it would just leak a task."""
    async def _inner():
        req = _make_request(candidates=[], spawn_answer_monitor=True)
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_spawn_answer_monitor") as mock_spawn:
            await search_pipeline.rerank_and_emit(req)
        mock_spawn.assert_not_called()
    _run(_inner())


def test_answer_monitor_spawn_failure_does_not_break_search():
    """If the monitor spawn raises (asyncio task creation outside event
    loop, etc.), the pipeline still returns a valid result."""
    async def _inner():
        req = _make_request(spawn_answer_monitor=True)
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_spawn_answer_monitor",
                          side_effect=RuntimeError("no event loop")):
            # Must not raise.
            result = await search_pipeline.rerank_and_emit(req)
        assert len(result.ranked) > 0
    _run(_inner())


# ----------------------------------------------------------------------
# 5. Citation cache populated
# ----------------------------------------------------------------------


def test_citation_cache_populated_on_successful_emit():
    """The per-task cache must be populated so the answer monitor can
    consume nodes + query_emb when it eventually fires."""
    async def _inner():
        req = _make_request()
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_populate_citation_cache") as mock_cache:
            await search_pipeline.rerank_and_emit(req)
        mock_cache.assert_called_once()
        # Check the cache call carries the right shape.
        call_kwargs = mock_cache.call_args.kwargs
        assert call_kwargs["task_id"]
        assert call_kwargs["candidates"] == req.candidates
        assert call_kwargs["query_emb"] == req.query_emb
    _run(_inner())


# ----------------------------------------------------------------------
# 6. RerankRequest is frozen
# ----------------------------------------------------------------------


def test_rerank_request_is_frozen():
    """Frozen dataclass prevents accidental mutation after validation."""
    req = _make_request()
    with pytest.raises((AttributeError, Exception)):
        # FrozenInstanceError is a subclass of AttributeError on most
        # Python versions.
        req.query = "mutated"


def test_rerank_result_is_frozen():
    """Same contract on the output shape."""
    result = search_pipeline.RerankResult(
        ranked=[], task_id="x", rl_used=False, emit_success=False
    )
    with pytest.raises((AttributeError, Exception)):
        result.task_id = "mutated"


# ----------------------------------------------------------------------
# 7. F-E (v0.2.70) — score normalization at the writer boundary.
#    Unbounded hybrid-fusion scores (observed max 10.37) must be clamped to
#    [0, 1] before they reach rl_events, NOT relied on a downstream clamp.
# ----------------------------------------------------------------------


def test_clamp_unit_score_clamps_above_one():
    assert search_pipeline._clamp_unit_score(10.37) == 1.0
    assert search_pipeline._clamp_unit_score(1.0001) == 1.0


def test_clamp_unit_score_clamps_below_zero():
    assert search_pipeline._clamp_unit_score(-0.5) == 0.0


def test_clamp_unit_score_passes_through_unit_interval():
    assert search_pipeline._clamp_unit_score(0.0) == 0.0
    assert search_pipeline._clamp_unit_score(0.5) == 0.5
    assert search_pipeline._clamp_unit_score(1.0) == 1.0


def test_clamp_unit_score_soft_fails_non_numeric():
    assert search_pipeline._clamp_unit_score("bad") == 0.0
    assert search_pipeline._clamp_unit_score(None) == 0.0
    assert search_pipeline._clamp_unit_score(float("nan")) == 0.0


def test_build_log_nodes_clamps_over_one_score_at_writer_boundary():
    # A node arrives with an unbounded fusion score (the mcp_interactive bug).
    # The reducer that writes telemetry MUST clamp it to 1.0 before it lands in
    # rl_events — stopping NEW poison at the source.
    candidates = [
        {"title": "Hot", "score": 10.37, "score_cosine": 0.8},
        {"title": "Cold", "score": 0.3},
    ]
    out = search_pipeline._build_log_nodes(candidates, limit=5)
    by_title = {n["title"]: n for n in out}
    assert by_title["Hot"]["score"] == 1.0       # clamped from 10.37
    assert by_title["Cold"]["score"] == 0.3       # untouched in-range
    assert by_title["Hot"]["score_cosine"] == 0.8  # already bounded
