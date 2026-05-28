"""Regression test for NEW-8 (2026-05-28): _rl_cache_and_rerank must pass
query_emb to writer.log_retrieval.

Pre-fix: the writer call at server.py:3416 never included the query_emb
kwarg, so 100% of retrieval events written since v0.2.20 (2026-05-19)
lacked the query-side embedding — breaking offline training for any
qwen3-aligned pass that needs query vectors.

Post-fix: _rl_cache_and_rerank accepts query_emb and threads it through
to writer.log_retrieval.

Note: named test_telemetry_query_emb.py (not test_rl_*) to avoid the
gitignore pattern `tests/test_rl_*.py`.
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    # Use a fresh event loop per call so the test isn't sensitive to
    # asyncio policy state polluted by earlier tests in the suite (the
    # deprecated `get_event_loop()` raises when the default loop is
    # closed by an earlier test on Python 3.12+).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRlCacheAndRerankPassesQueryEmb:
    """NEW-8: query_emb must be forwarded to writer.log_retrieval."""

    def _call_rl_cache_and_rerank(self, mock_writer, query_emb_arg, nodes=None):
        """Invoke _rl_cache_and_rerank with a mocked writer and return the
        captured log_retrieval call kwargs."""
        from claude_mcp_servers.weaviate_mcp.server import _rl_cache_and_rerank

        if nodes is None:
            nodes = [{"title": "n1", "score": 0.8}]

        with patch(
            "claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer",
            return_value=mock_writer,
        ):
            # Patch feature gate so the telemetry block always runs
            # (free-tier still runs telemetry per v0.2.24 refactor).
            with patch(
                "claude_mcp_servers.weaviate_mcp.server._rl_enabled",
                False,
                create=True,
            ):
                _run(
                    _rl_cache_and_rerank(
                        task_id="test-task-id",
                        query="test query",
                        all_nodes=nodes,
                        limit=5,
                        query_emb=query_emb_arg,
                    )
                )

        return mock_writer.log_retrieval.call_args

    def test_query_emb_non_none_is_forwarded(self):
        """When query_emb is a 1024-dim vector, log_retrieval must receive it."""
        mock_writer = MagicMock()
        query_vector = [0.1] * 1024

        call_kwargs = self._call_rl_cache_and_rerank(mock_writer, query_vector)

        assert mock_writer.log_retrieval.called, (
            "writer.log_retrieval was never called"
        )
        # Accept both positional and keyword invocation styles.
        all_kwargs = call_kwargs.kwargs if call_kwargs is not None else {}
        if call_kwargs is not None and call_kwargs.args:
            # Some args may be positional; unpack into kwargs for uniform check.
            pass
        received_emb = all_kwargs.get("query_emb")
        assert received_emb is not None, (
            "Pre-fix regression: query_emb was not passed to writer.log_retrieval. "
            "Check that _rl_cache_and_rerank accepts the kwarg and forwards it."
        )
        assert received_emb == query_vector, (
            f"query_emb mismatch: expected {query_vector[:3]}... "
            f"got {received_emb[:3] if received_emb else None}..."
        )

    def test_query_emb_none_is_forwarded(self):
        """When query_emb is None, log_retrieval must be called with query_emb=None
        (not omitted entirely — the kwarg must still be present)."""
        mock_writer = MagicMock()

        call_kwargs = self._call_rl_cache_and_rerank(mock_writer, None)

        assert mock_writer.log_retrieval.called, (
            "writer.log_retrieval was never called"
        )
        all_kwargs = call_kwargs.kwargs if call_kwargs is not None else {}
        # None is a valid explicit value — the kwarg must appear.
        assert "query_emb" in all_kwargs, (
            "query_emb kwarg missing from writer.log_retrieval call when passed None"
        )
        assert all_kwargs["query_emb"] is None

    def test_query_emb_default_is_none(self):
        """Callers that omit query_emb entirely get query_emb=None at the writer."""
        from claude_mcp_servers.weaviate_mcp.server import _rl_cache_and_rerank

        mock_writer = MagicMock()
        nodes = [{"title": "n1", "score": 0.7}]

        with patch(
            "claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer",
            return_value=mock_writer,
        ):
            _run(
                _rl_cache_and_rerank(
                    task_id="test-task-id-default",
                    query="default test",
                    all_nodes=nodes,
                    limit=3,
                    # query_emb omitted intentionally
                )
            )

        assert mock_writer.log_retrieval.called
        call_kwargs = mock_writer.log_retrieval.call_args.kwargs
        assert call_kwargs.get("query_emb") is None
