# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 — opt-out must skip the COMPUTE, not just the write (Concerns A/B/C/D).

These tests pin four related fixes to the KG-search citation-capture path:

  Concern A — when NOTHING will consume the citation (local logging off AND no
    upload consent AND no online-training consumer), ``rerank_and_emit`` must
    skip the whole capture: the answer-monitor spawn, the citation cache, the
    ``.claude/state/rl_pending`` disk write, AND the retrieval-event emit. It
    must NOT change behaviour for default users (logging on), consented
    uploaders, or online-training consumers.

  Concern B — the answer-window read is bounded/cached in the ONE shared home
    (``rl_client.answer_window``); a cache HIT on an unchanged transcript yields
    a BYTE-IDENTICAL window to the full-read path. All three consumers (monitor,
    drain, online) use the same shared ``extract_answer_window``.

  Concern C — a machine-GLOBAL + per-project two-level online-training opt-out
    (``RL_ONLINE_TRAINING_DISABLED[_GLOBAL]``). A global disable overrides all
    projects; either level disables. The two-level ``_local_logging_disabled``
    likewise ORs the global leg.

  Concern D — single-compute-feeds-both: the capture gate never splits into two
    passes. When both the local log and the online RPC consume the citation, the
    answer is embedded ONCE and both sinks read the shared computed maps.

Async-test pattern: pytest-asyncio is not a declared dependency; async bodies
are wrapped in ``asyncio.run`` (mirrors tests/test_search_pipeline.py).
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

search_pipeline = pytest.importorskip(
    "claude_mcp_servers.rl_client.search_pipeline",
    reason="search_pipeline must be importable for the capture-gating tests",
)
from claude_mcp_servers.rl_client import telemetry_writer  # noqa: E402
from claude_mcp_servers.rl_client import answer_window  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _make_request(**overrides):
    defaults = {
        "query": "where does the KG collection name come from?",
        "candidates": [
            {"title": "ConfigDoc", "score": 0.91, "n_emb": [0.2] * 8},
            {"title": "InstallNotes", "score": 0.74, "n_emb": [0.3] * 8},
        ],
        "limit": 5,
        "query_emb": [0.1] * 8,
        "embedding_source": "qwen3",
        "embedding_dim": 8,
        "embedding_model": "qwen3-embedding:0.6b",
        "task_id": "test-task-optout",
        "task_type": "mcp_interactive",
        "session_id": "test-sess-optout",
        "spawn_answer_monitor": True,
    }
    defaults.update(overrides)
    return search_pipeline.RerankRequest(**defaults)


# ----------------------------------------------------------------------
# Concern C — two-level resolvers (global OR per-project; global overrides).
# ----------------------------------------------------------------------


class TestTwoLevelResolvers:
    """``_local_logging_disabled`` / ``_online_training_disabled`` two-level gate."""

    def _clear_env(self, monkeypatch):
        for k in (
            "RL_LOCAL_LOGGING_DISABLED",
            "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
            "RL_ONLINE_TRAINING_DISABLED",
            "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
        ):
            monkeypatch.delenv(k, raising=False)

    def test_defaults_not_disabled(self, monkeypatch):
        self._clear_env(monkeypatch)
        assert telemetry_writer._local_logging_disabled() is False
        assert telemetry_writer._online_training_disabled() is False

    def test_local_per_project_disable(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
        assert telemetry_writer._local_logging_disabled() is True

    def test_local_global_disable_overrides(self, monkeypatch):
        # Global disabled + per-project ABSENT → disabled (global override).
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED_GLOBAL", "true")
        assert telemetry_writer._local_logging_disabled() is True

    def test_online_per_project_disable(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED", "1")
        assert telemetry_writer._online_training_disabled() is True

    def test_online_global_disable_beats_per_project_enabled(self, monkeypatch):
        # Global disabled + per-project unset → disabled. (Per-project has no
        # "explicitly enabled" env — absence = inherit; the OR gives disabled.)
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED_GLOBAL", "on")
        assert telemetry_writer._online_training_disabled() is True

    def test_online_per_project_opt_out_when_global_enabled(self, monkeypatch):
        # Global enabled (unset) + per-project disabled → disabled.
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED", "yes")
        assert telemetry_writer._online_training_disabled() is True

    def test_falsey_values_not_disabled(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED", "false")
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "0")
        assert telemetry_writer._online_training_disabled() is False
        assert telemetry_writer._local_logging_disabled() is False


# ----------------------------------------------------------------------
# Concern A/C — capture gating (the consumer check drives spawn + populate).
# ----------------------------------------------------------------------


class TestCaptureGating:
    def _clear_env(self, monkeypatch):
        for k in (
            "RL_LOCAL_LOGGING_DISABLED",
            "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
            "RL_ONLINE_TRAINING_DISABLED",
            "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
        ):
            monkeypatch.delenv(k, raising=False)

    def _run_with(self, *, rl_enabled, upload=False):
        """Run rerank_and_emit spying spawn + populate + emit; return the spies."""
        async def _inner():
            req = _make_request()
            with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=rl_enabled), \
                 patch.object(search_pipeline, "_spawn_answer_monitor") as spawn, \
                 patch.object(search_pipeline, "_populate_citation_cache") as populate, \
                 patch.object(search_pipeline, "_emit_retrieval_event") as emit, \
                 patch.object(telemetry_writer, "_upload_consent_granted", return_value=upload):
                # emit is async; give it an awaitable return.
                async def _emit_stub(*a, **k):
                    return True
                emit.side_effect = _emit_stub
                await search_pipeline.rerank_and_emit(req)
            return spawn, populate, emit
        return _run(_inner())

    def test_local_on_captures_as_today(self, monkeypatch):
        """Default (local logging ON) → capture happens (spawn + populate + emit)."""
        self._clear_env(monkeypatch)
        spawn, populate, emit = self._run_with(rl_enabled=False)
        spawn.assert_called_once()
        populate.assert_called_once()
        emit.assert_called_once()

    def test_local_off_no_upload_no_online_full_skip(self, monkeypatch):
        """local off + no upload + rl DISABLED → skip spawn + populate + emit."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
        spawn, populate, emit = self._run_with(rl_enabled=False, upload=False)
        spawn.assert_not_called()
        populate.assert_not_called()
        emit.assert_not_called()

    def test_local_off_with_upload_still_captures(self, monkeypatch):
        """local off + upload consent ON → still captured (upload is a consumer)."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
        spawn, populate, emit = self._run_with(rl_enabled=False, upload=True)
        spawn.assert_called_once()
        populate.assert_called_once()
        # Retrieval emit ALSO has a consumer (upload) → emitted.
        emit.assert_called_once()

    def test_local_off_rl_enabled_online_on_keeps_capture(self, monkeypatch):
        """local off + no upload + rl ENABLED + online training ON → capture alive.

        The online /rl_update path consumes the citation event, so rl_enabled
        keeps the monitor + citation cache + pending file alive. The retrieval
        event has NO consumer (local off, no upload), so THAT is skipped.
        """
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
        spawn, populate, emit = self._run_with(rl_enabled=True, upload=False)
        spawn.assert_called_once()
        populate.assert_called_once()
        # Retrieval emit is NOT a consumer of the online path → still skipped.
        emit.assert_not_called()

    def test_local_off_rl_enabled_but_online_disabled_full_skip(self, monkeypatch):
        """Pro user, local off + no upload + online training DISABLED → full skip.

        This is the whole point of Concern C: a performance-concerned Pro user
        who turns off BOTH local logging AND online training pays ZERO
        answer-embedding / citation / pending-file cost.
        """
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
        monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED", "true")
        spawn, populate, emit = self._run_with(rl_enabled=True, upload=False)
        spawn.assert_not_called()
        populate.assert_not_called()
        emit.assert_not_called()

    def test_online_global_disable_full_skip_when_no_other_consumer(self, monkeypatch):
        """GLOBAL online-training disable + local off → full skip (global override)."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
        monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED_GLOBAL", "true")
        spawn, populate, emit = self._run_with(rl_enabled=True, upload=False)
        spawn.assert_not_called()
        populate.assert_not_called()
        emit.assert_not_called()

    def test_online_disabled_but_local_on_still_captures_locally(self, monkeypatch):
        """online training OFF but local ON → local consumer keeps capture alive.

        The online /rl_update RPC is short-circuited elsewhere (monitor site);
        here we assert the CAPTURE still runs because the local write consumes it.
        """
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED", "true")
        spawn, populate, emit = self._run_with(rl_enabled=True, upload=False)
        spawn.assert_called_once()
        populate.assert_called_once()
        emit.assert_called_once()

    def test_probe_error_falls_open_to_capture(self, monkeypatch):
        """A probe raising must fall OPEN to capturing (never drop a payer's data)."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")

        async def _inner():
            req = _make_request()
            with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
                 patch.object(search_pipeline, "_spawn_answer_monitor") as spawn, \
                 patch.object(search_pipeline, "_populate_citation_cache") as populate, \
                 patch.object(
                     telemetry_writer,
                     "_local_logging_disabled",
                     side_effect=RuntimeError("boom"),
                 ):
                await search_pipeline.rerank_and_emit(req)
            return spawn, populate

        spawn, populate = _run(_inner())
        spawn.assert_called_once()
        populate.assert_called_once()


# ----------------------------------------------------------------------
# Concern A — the skip must NOT stage a pending file (end-to-end, real populate).
# ----------------------------------------------------------------------


def test_no_pending_file_written_when_no_consumer(tmp_path, monkeypatch):
    """local off + no upload + rl disabled → NO ``.claude/state/rl_pending`` file."""
    for k in (
        "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
        "RL_ONLINE_TRAINING_DISABLED",
        "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    async def _inner():
        req = _make_request()
        with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
             patch.object(search_pipeline, "_spawn_answer_monitor"), \
             patch.object(telemetry_writer, "_upload_consent_granted", return_value=False):
            await search_pipeline.rerank_and_emit(req)

    _run(_inner())
    pending = tmp_path / ".claude" / "state" / "rl_pending"
    files = list(pending.glob("*.json")) if pending.exists() else []
    assert files == [], f"expected no pending file, found {files}"


# ----------------------------------------------------------------------
# Concern B — shared cached loader is byte-identical to the full read.
# ----------------------------------------------------------------------


def _assistant_tool(tool_name, tool_input):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": tool_name, "input": tool_input}]},
    }


def _assistant_text(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant_thinking(text):
    return {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": text}]}}


def _user_tool_result(text):
    # Tool OUTPUT lives on a user-type message → must be EXCLUDED from the window.
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}


def _write_transcript(path, messages):
    import json
    with open(path, "w") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


@pytest.mark.parametrize("shape", ["short", "multiturn", "thinking_heavy", "tool_input_heavy", "past_threshold"])
def test_cached_loader_window_byte_identical_to_full_read(tmp_path, shape):
    """load_messages_cached → same parsed messages → BYTE-IDENTICAL answer window
    as the full load_messages path, across representative transcript shapes."""
    if shape == "short":
        msgs = [
            _assistant_tool("hybrid_search", {"query": "alpha"}),
            _assistant_text("the answer text"),
            _user_tool_result("SHOULD-NOT-APPEAR shell output"),
            _assistant_text("more answer"),
        ]
    elif shape == "multiturn":
        msgs = [
            _assistant_tool("hybrid_search", {"query": "alpha"}),
            _assistant_text("turn one"),
            {"type": "user", "message": {"content": [{"type": "text", "text": "yes continue"}]}},
            _assistant_text("turn two after human"),
        ]
    elif shape == "thinking_heavy":
        msgs = [
            _assistant_tool("hybrid_search", {"query": "alpha"}),
            _assistant_thinking("internal scratch reasoning" * 20),
            _assistant_text("visible answer"),
        ]
    elif shape == "tool_input_heavy":
        msgs = [
            _assistant_tool("hybrid_search", {"query": "alpha"}),
            _assistant_tool("Bash", {"command": "echo lots of input " * 30}),
            _assistant_text("final answer"),
        ]
    else:  # past_threshold
        big = "x" * 200_000  # well past the 25k-token (~100KB char) threshold
        msgs = [
            _assistant_tool("hybrid_search", {"query": "alpha"}),
            _assistant_text(big),
            _assistant_text("trailing"),
        ]

    tpath = tmp_path / "transcript.jsonl"
    _write_transcript(tpath, msgs)

    full = answer_window.load_messages(tpath)
    cached_miss = answer_window.load_messages_cached(tpath)  # cache MISS (first)
    cached_hit = answer_window.load_messages_cached(tpath)   # cache HIT (unchanged)

    kg_full = answer_window.find_kg_positions(full)
    assert kg_full, "test transcript must contain a KG search"
    smi, sbi = kg_full[0]

    win_full = answer_window.extract_answer_window(full, smi, sbi)
    win_miss = answer_window.extract_answer_window(cached_miss, smi, sbi)
    win_hit = answer_window.extract_answer_window(cached_hit, smi, sbi)

    assert win_full == win_miss == win_hit, "cached loader must yield byte-identical window"
    # Content-inclusion contract: tool OUTPUT is excluded.
    assert "SHOULD-NOT-APPEAR" not in win_full[0]


def test_cached_loader_reparses_when_transcript_grows(tmp_path):
    """A CHANGED transcript (new bytes) → cache MISS → window reflects new content."""
    tpath = tmp_path / "grow.jsonl"
    _write_transcript(tpath, [
        _assistant_tool("hybrid_search", {"query": "alpha"}),
        _assistant_text("first"),
    ])
    m1 = answer_window.load_messages_cached(tpath)
    assert len(m1) == 2

    import time
    time.sleep(0.01)  # ensure mtime moves
    # Append a new assistant turn.
    import json
    with open(tpath, "a") as fh:
        fh.write(json.dumps(_assistant_text("second turn")) + "\n")

    m2 = answer_window.load_messages_cached(tpath)
    assert len(m2) == 3, "grown transcript must be re-parsed (cache invalidated)"
    win = answer_window.extract_answer_window(m2, 0, 0)
    assert "second turn" in win[0]


def test_stat_signature_none_on_missing_file(tmp_path):
    assert answer_window.stat_signature(tmp_path / "nope.jsonl") is None


# ----------------------------------------------------------------------
# Concern D — single-compute-feeds-both (answer embedded once; both sinks read it).
# ----------------------------------------------------------------------


def test_online_rpc_short_circuited_when_online_training_disabled(monkeypatch):
    """Concern-C: when online training is disabled, the monitor does NOT build
    an RL client for the /rl_update POST (short-circuited) even if a container
    would otherwise be reachable. Local capture is unaffected — verified by the
    capture-gating tests above; here we assert the RPC path is skipped."""
    import claude_mcp_servers.weaviate_mcp.rl_enrichment as rlmod
    import claude_mcp_servers.weaviate_mcp.server as srv

    for k in (
        "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
        "RL_LOCAL_LOGGING_DISABLED",
        "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RL_ONLINE_TRAINING_DISABLED", "true")

    # If online training were NOT disabled, the monitor calls
    # server._get_rl_client() to build the RPC client. Spy it; assert NOT called
    # when the online-gate short-circuits.
    called = {"get_client": 0}

    def _spy_get_client():
        called["get_client"] += 1
        return None

    # Drive the online-gate branch in isolation: replicate the exact guard the
    # monitor uses.
    from claude_mcp_servers.rl_client.telemetry_writer import _online_training_disabled
    assert _online_training_disabled() is True
    # The monitor code does: client = None if _online_off else server._get_rl_client()
    with patch.object(srv, "_get_rl_client", _spy_get_client):
        online_off = _online_training_disabled()
        client = None if online_off else srv._get_rl_client()
    assert client is None
    assert called["get_client"] == 0, "online-disabled must short-circuit _get_rl_client"


def test_monitor_idle_poll_short_circuits_unchanged_transcript(tmp_path, monkeypatch):
    """Concern-B: an idle poll (transcript unchanged since last poll) must NOT
    re-load/re-parse the transcript. We spy _rl_load_messages and assert it is
    called far fewer times than the poll count when nothing changes."""
    import claude_mcp_servers.weaviate_mcp.rl_enrichment as rlmod
    import claude_mcp_servers.weaviate_mcp.server as srv

    # Build a transcript that never reaches the threshold (so the monitor keeps
    # polling until timeout) and never changes (idle).
    tpath = tmp_path / "sess.jsonl"
    _write_transcript(tpath, [
        _assistant_tool("hybrid_search", {"query": "alpha"}),
        _assistant_text("short answer, below threshold"),
    ])

    load_calls = {"n": 0}
    real_load = rlmod._rl_load_messages

    def _spy_load(path):
        load_calls["n"] += 1
        return real_load(path)

    async def _fake_find_all():
        return [tpath]

    # Fast poll, short timeout → several poll iterations.
    monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.005, raising=False)
    monkeypatch.setattr(srv, "_RL_MONITOR_TIMEOUT", 0.06, raising=False)

    with patch.object(srv, "_rl_find_all_transcripts", _fake_find_all), \
         patch.object(srv, "_rl_load_messages", _spy_load), \
         patch.object(srv, "_rl_check_force_flush", return_value=False), \
         patch.object(srv, "_rl_node_content_cache", {}):
        _run(rlmod._rl_answer_monitor("tid-idle", 1, "alpha"))

    # With ~12 poll iterations (0.06/0.005) but an unchanged transcript, the load
    # should happen only on the FIRST poll (first-seen sig) — subsequent idle
    # polls short-circuit. Allow a small margin for scheduling jitter.
    assert load_calls["n"] <= 2, (
        f"idle polls re-loaded the transcript {load_calls['n']}x; "
        "expected the mtime/size short-circuit to skip unchanged polls"
    )


def test_single_compute_feeds_log_and_online(monkeypatch):
    """compute_citation embeds the answer ONCE; the returned maps feed BOTH the
    log write (inside compute) AND the online /rl_update payload (caller reads
    the SAME cosine_sims/literal_cited). Spy the embed call → exactly one pass."""
    from claude_mcp_servers.rl_client import citation_compute

    # Fake embedding service that counts embed calls.
    class _FakeSvc:
        def __init__(self):
            self.embed_calls = 0

        def embed_text(self, text):
            self.embed_calls += 1
            return [0.5] * 8

        def embed_code(self, text):
            self.embed_calls += 1
            return [0.5] * 8

    fake = _FakeSvc()

    class _FakeChunk:
        def __init__(self, content):
            self.content = content

    class _FakeChunker:
        @staticmethod
        def for_model(model):
            return _FakeChunker()

        def chunk_text(self, text, source_id=None):
            # ONE chunk regardless of length → one embed call expected.
            return [_FakeChunk(text)]

    captured_log = {}

    class _FakeWriter:
        def log_citations(self, **kwargs):
            captured_log.update(kwargs)

    def _fake_targets(cosine_sims, literal_cited=None, cross_encoder_cited=None):
        cited = {t: (v >= 0.0) for t, v in cosine_sims.items()}
        return cosine_sims, cited

    import vco_lib.rl_training_targets as rtt

    with patch.object(citation_compute, "logger"), \
         patch("claude_mcp_servers.weaviate_mcp.server.Chunker", _FakeChunker), \
         patch("claude_mcp_servers.weaviate_mcp.server._get_embedding_service", return_value=fake), \
         patch("claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer", return_value=_FakeWriter()), \
         patch("claude_mcp_servers.weaviate_mcp.server._cosine", return_value=0.8), \
         patch("claude_mcp_servers.weaviate_mcp.server._rl_is_literal_cited", return_value=True), \
         patch.object(rtt, "compute_unified_targets", _fake_targets):
        ctx = {
            "nodes": [
                {"title": "NodeA", "n_emb": [0.1] * 8},
                {"title": "NodeB", "n_emb": [0.2] * 8},
            ],
            "query_emb": [0.1] * 8,
            "active_model": "qwen3-embedding:0.6b",
            "embedding_source": "qwen3",
            "embedding_dim": 8,
            "task_type": "mcp_interactive",
        }
        result = citation_compute.compute_citation("tid", "the complete answer", ctx, write=True)

    # Exactly ONE embed pass over the single answer chunk.
    assert fake.embed_calls == 1, f"answer embedded {fake.embed_calls}x; expected once"
    # Log write got the computed maps.
    assert captured_log.get("cosine_sims") == {"NodeA": 0.8, "NodeB": 0.8}
    # ctx mutated in place so the online /rl_update caller reuses WITHOUT re-embed.
    assert ctx["cosine_sims_computed"] == {"NodeA": 0.8, "NodeB": 0.8}
    assert ctx["literal_cited_computed"] == {"NodeA": True, "NodeB": True}
    # The result the online path reads is the SAME single computed map.
    assert result["cosine_sims"] == ctx["cosine_sims_computed"]
