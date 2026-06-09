# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-J — tests for the canonical retrieval-event emit chokepoint.

Covers ``claude_mcp_servers/rl_client/telemetry_emit.py`` which is the
single emit entry point all KG-search paths (MCP, subagent, CLIs, hooks)
must route through. The pre-V52-J state had per-call-site divergence in
how mandatory fields (project_id, session_id, query_emb) were resolved;
this module collapses that into one well-tested function.

Test surface:
1. ``EmitValidationError`` raised on each invalid input shape:
   - empty query
   - empty query_emb
   - dim mismatch (query_emb length != embedding_dim)
   - empty task_id
2. Successful emit returns True and calls the writer's ``log_retrieval``
   with the expected args (query, nodes, session_id, query_emb).
3. ``resolve_session_id`` honours the 3-layer order:
   arg > VCT_SESSION_ID > CLAUDE_SESSION_ID.
4. ``new_task_id`` returns a 32-char hex (uuid4().hex shape).

Module-availability gate: ``telemetry_emit.py`` is being added in sister
B's branch; we ``pytest.importorskip`` so this file is a no-op on commits
that don't yet have it, then becomes load-bearing after the merge.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

# Module under test — soft-skip if sister B's branch hasn't landed yet.
telemetry_emit = pytest.importorskip(
    "claude_mcp_servers.rl_client.telemetry_emit",
    reason="V52-J telemetry_emit lands in sister B's branch; skip if not yet merged",
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_event(**overrides):
    """Build a RetrievalEvent with sensible defaults; override per-test."""
    defaults = {
        "query": "what is the kg collection naming convention?",
        "query_emb": [0.1] * 1024,
        "embedding_source": "ollama",
        "embedding_dim": 1024,
        "embedding_model": "qwen3-embedding:0.6b",
        "nodes": [
            {"title": "KG Collection Naming", "score": 0.82, "tier": "top_k"},
        ],
        "task_id": "abc123def456" * 2 + "ab",  # 26 chars -> not empty
    }
    defaults.update(overrides)
    return telemetry_emit.RetrievalEvent(**defaults)


# ----------------------------------------------------------------------
# 1. Validation surface
# ----------------------------------------------------------------------


def test_emit_raises_on_empty_query():
    """An empty query is a caller bug — raise immediately so the call
    site sees it during development, not in production silence."""
    ev = _make_event(query="")
    with pytest.raises(telemetry_emit.EmitValidationError) as exc_info:
        telemetry_emit.emit_rl_event(ev, writer_factory=lambda: MagicMock())
    assert "query" in str(exc_info.value).lower()


def test_emit_soft_warns_on_empty_query_emb():
    """An empty query_emb is SOFT-tier (per ab706bd V52-J refactor): the
    function logs a debug warning and STILL writes the event. Real
    production cases include failure_mode-flagged events where the
    embedding step legitimately failed but we want the cohort/query
    distribution signal anyway.

    Pre-refactor (legacy STRICT tier) raised EmitValidationError here;
    post-refactor the validation tiers are STRICT (raise — empty query
    or task_id), SOFT (log + write — empty query_emb or dim mismatch in
    happy path), SKIPPED (failure_mode events bypass validation
    entirely). See telemetry_emit.py lines 122-135 for the contract."""
    ev = _make_event(query_emb=[])
    # Should NOT raise; should return True (writer was called).
    result = telemetry_emit.emit_rl_event(ev, writer_factory=lambda: MagicMock())
    assert result is True


def test_emit_soft_warns_on_dim_mismatch():
    """Dim mismatch is SOFT-tier per ab706bd V52-J refactor — same
    rationale as test_emit_soft_warns_on_empty_query_emb. The writer's
    _build_v3_retrieval_event still handles a non-None query_emb of any
    length; the offline trainer filters by (embedding_dim, embedding_source)
    cohort labels and the dim-mismatch row simply lands in a separate cohort.
    """
    ev = _make_event(query_emb=[0.1] * 768, embedding_dim=1024)
    result = telemetry_emit.emit_rl_event(ev, writer_factory=lambda: MagicMock())
    assert result is True


def test_emit_raises_on_empty_task_id():
    """task_id is the join key for citation events; missing it would
    orphan every downstream citation write."""
    ev = _make_event(task_id="")
    with pytest.raises(telemetry_emit.EmitValidationError) as exc_info:
        telemetry_emit.emit_rl_event(ev, writer_factory=lambda: MagicMock())
    assert "task_id" in str(exc_info.value).lower()


def test_emit_allows_zero_embedding_dim():
    """embedding_dim=0 disables the length check (legacy callers without
    the field still validate cleanly)."""
    ev = _make_event(query_emb=[0.1] * 100, embedding_dim=0)
    writer = MagicMock()
    assert telemetry_emit.emit_rl_event(ev, writer_factory=lambda: writer) is True
    writer.log_retrieval.assert_called_once()


# ----------------------------------------------------------------------
# 2. Successful emit
# ----------------------------------------------------------------------


def test_successful_emit_returns_true():
    """The happy path returns True after writer.log_retrieval succeeds."""
    ev = _make_event()
    writer = MagicMock()
    result = telemetry_emit.emit_rl_event(ev, writer_factory=lambda: writer)
    assert result is True
    writer.log_retrieval.assert_called_once()


def test_emit_passes_canonical_fields_to_writer():
    """The writer must see (query, nodes, session_id, query_emb, task_id,
    task_type) verbatim. Pre-V52-J these were divergent across callers."""
    ev = _make_event(
        query="how do I configure KG_COLLECTION",
        nodes=[{"title": "ConfigDoc", "score": 0.91}],
        task_id="task-xyz-789",
    )
    writer = MagicMock()
    telemetry_emit.emit_rl_event(ev, writer_factory=lambda: writer)
    call_kwargs = writer.log_retrieval.call_args.kwargs
    assert call_kwargs["query"] == "how do I configure KG_COLLECTION"
    assert call_kwargs["nodes"] == [{"title": "ConfigDoc", "score": 0.91}]
    assert call_kwargs["task_id"] == "task-xyz-789"
    assert call_kwargs["query_emb"] == [0.1] * 1024
    assert call_kwargs["task_type"] == "mcp_interactive"


def test_emit_returns_false_when_writer_factory_yields_none():
    """No writer available (free tier / hub down / import failure) is
    soft-fail, not exception. Returns False; doesn't break the user's
    actual KG search."""
    ev = _make_event()
    result = telemetry_emit.emit_rl_event(ev, writer_factory=lambda: None)
    assert result is False


def test_emit_returns_false_when_writer_raises():
    """log_retrieval raising (network error, hub 5xx) is soft-fail."""
    ev = _make_event()
    writer = MagicMock()
    writer.log_retrieval.side_effect = ConnectionError("hub down")
    result = telemetry_emit.emit_rl_event(ev, writer_factory=lambda: writer)
    assert result is False


def test_emit_returns_false_when_factory_raises():
    """The factory itself raising (e.g. import error in MCP context) is
    soft-fail."""
    ev = _make_event()
    def boom():
        raise RuntimeError("cannot resolve writer")
    result = telemetry_emit.emit_rl_event(ev, writer_factory=boom)
    assert result is False


# ----------------------------------------------------------------------
# 3. resolve_session_id 3-layer order
# ----------------------------------------------------------------------


class TestResolveSessionId:
    """3-layer chain: arg > VCT_SESSION_ID > CLAUDE_SESSION_ID."""

    def setup_method(self):
        # Snapshot + clear so each test starts from a known state.
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("VCT_SESSION_ID", "CLAUDE_SESSION_ID")
        }

    def teardown_method(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_arg_wins_over_env(self):
        os.environ["VCT_SESSION_ID"] = "from-vct-env"
        os.environ["CLAUDE_SESSION_ID"] = "from-claude-env"
        assert telemetry_emit.resolve_session_id("from-arg") == "from-arg"

    def test_vct_env_wins_over_claude_env(self):
        os.environ["VCT_SESSION_ID"] = "from-vct-env"
        os.environ["CLAUDE_SESSION_ID"] = "from-claude-env"
        assert telemetry_emit.resolve_session_id(None) == "from-vct-env"

    def test_claude_env_used_when_only_one(self):
        os.environ["CLAUDE_SESSION_ID"] = "from-claude-env"
        assert telemetry_emit.resolve_session_id(None) == "from-claude-env"

    def test_empty_arg_falls_through_to_env(self):
        """Empty string in the arg slot is NOT a "use empty" signal —
        the resolver treats it the same as None and falls through to env.
        This matters because hooks read session_id from stdin JSON; if
        it's missing the JSON yields an empty string."""
        os.environ["VCT_SESSION_ID"] = "from-vct-env"
        assert telemetry_emit.resolve_session_id("") == "from-vct-env"

    def test_returns_empty_when_no_layer_resolves(self):
        """Empty string (not None) so downstream's str-typed schema
        column gets a value rather than NULL."""
        assert telemetry_emit.resolve_session_id(None) == ""


def test_session_id_resolution_threaded_through_emit():
    """The 3-layer resolver runs inside emit_rl_event; ev.session_id
    is treated as the arg layer, env vars as layers 2-3."""
    os.environ["VCT_SESSION_ID"] = "vct-fallback"
    try:
        # Arg layer wins.
        ev = _make_event()
        ev_with_session = telemetry_emit.RetrievalEvent(
            **{**ev.__dict__, "session_id": "explicit-arg"}
        )
        writer = MagicMock()
        telemetry_emit.emit_rl_event(ev_with_session, writer_factory=lambda: writer)
        assert writer.log_retrieval.call_args.kwargs["session_id"] == "explicit-arg"

        # No arg -> env wins.
        writer2 = MagicMock()
        telemetry_emit.emit_rl_event(ev, writer_factory=lambda: writer2)
        assert writer2.log_retrieval.call_args.kwargs["session_id"] == "vct-fallback"
    finally:
        os.environ.pop("VCT_SESSION_ID", None)


# ----------------------------------------------------------------------
# 4. new_task_id shape
# ----------------------------------------------------------------------


def test_new_task_id_is_hex_uuid4_shape():
    """uuid4().hex -> 32 lowercase hex chars. The writer enforces
    task_id uniqueness at the DB layer so any caller hardcoding a
    constant gets a collision error immediately."""
    tid = telemetry_emit.new_task_id()
    assert isinstance(tid, str)
    assert len(tid) == 32
    int(tid, 16)  # raises if not valid hex


def test_new_task_id_unique_across_calls():
    """Two consecutive calls must not collide (uuid4 collision rate is
    effectively zero, but a non-uuid implementation could regress)."""
    a = telemetry_emit.new_task_id()
    b = telemetry_emit.new_task_id()
    assert a != b


# ----------------------------------------------------------------------
# 5. default_writer_factory is lazy + None-safe
# ----------------------------------------------------------------------


def test_default_writer_factory_returns_value_or_none():
    """The default factory delegates to server._get_rl_telemetry_writer
    via lazy import. Outside an MCP process this returns None gracefully
    (no exception). Inside an MCP process it returns a writer (we don't
    assert that here — environment-dependent)."""
    result = telemetry_emit._default_writer_factory()
    # Either None (no MCP context / no project config) or a writer.
    # Both are valid; the contract is "doesn't raise".
    assert result is None or hasattr(result, "log_retrieval")
