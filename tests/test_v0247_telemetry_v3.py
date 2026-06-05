# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.47 RL-3: MCP-side RL telemetry schema v3 tests.

Closes the split-brain caught by the C3 audit: pre-v0.2.47, the MCP-side
``claude_mcp_servers/rl_client/rl_logger.py::SCHEMA_VERSION`` was 2 while
the paid-module's was 3 (committed in v0.2.9 part 1). After this commit,
both sides write v3 and accept the new ``literal_cited`` /
``cross_encoder_cited`` flag dicts on citation events PLUS the
``n_emb`` / ``linked_embs`` / ``linked_type_names`` per-node fields on
retrieval events.

Also covers the new ``EmbeddingService`` per-instance embed-result memo
that the upcoming citation-detection monitor relies on for query/answer
chunk-embed dedup.

These tests pin contracts; the live-integration tests live elsewhere
(C6/C7 phase).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_mcp_servers.rl_client.rl_logger import RLDataLogger
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter


# ----------------------------------------------------------------------
# 1. Schema version is now 3 and split-brain is closed.
# ----------------------------------------------------------------------


class TestSchemaVersionAlignment:
    def test_mcp_logger_schema_version_is_3(self) -> None:
        assert RLDataLogger.SCHEMA_VERSION == 3

    def test_telemetry_writer_inherits_schema_version_from_logger(self) -> None:
        """RLTelemetryWriter references RLDataLogger.SCHEMA_VERSION directly
        — bumping the logger automatically bumps the writer's payloads."""
        writer = RLTelemetryWriter(
            project="X",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
        )
        payload = writer._build_retrieval_payload(
            task_id="t1",
            task_type="x",
            query="q",
            nodes=[],
            session_id="",
            query_emb=None,
        )
        assert payload["schema_version"] == 3


# ----------------------------------------------------------------------
# 2. log_citations accepts literal_cited + cross_encoder_cited flag dicts.
# ----------------------------------------------------------------------


class TestCitationV3Fields:
    def _logger(self, td: Path) -> RLDataLogger:
        return RLDataLogger(
            log_path=td / "ev.jsonl",
            project="V3Test",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
        )

    def test_minimal_call_omits_v3_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t1",
                task_type="implementation",
                citations={"A": True, "B": False},
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["schema_version"] == 3
            assert "literal_cited" not in event
            assert "cross_encoder_cited" not in event

    def test_with_literal_cited_writes_field(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t2",
                task_type="x",
                citations={"A": True, "B": False},
                cosine_sims={"A": 0.85, "B": 0.30},
                literal_cited={"A": True, "B": False},
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["literal_cited"] == {"A": True, "B": False}
            assert event["cosine_sims"] == {"A": 0.85, "B": 0.30}

    def test_with_both_flag_dicts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t3",
                task_type="x",
                citations={"A": True},
                cosine_sims={"A": 0.85},
                literal_cited={"A": True},
                cross_encoder_cited={"A": False},
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["literal_cited"] == {"A": True}
            assert event["cross_encoder_cited"] == {"A": False}

    def test_empty_flag_dicts_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_citations(
                task_id="t4",
                task_type="x",
                citations={"A": True},
                literal_cited={},
                cross_encoder_cited={},
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert "literal_cited" not in event
            assert "cross_encoder_cited" not in event


# ----------------------------------------------------------------------
# 3. log_retrieval accepts n_emb + linked_embs + linked_type_names.
# ----------------------------------------------------------------------


class TestRetrievalV3Fields:
    def _logger(self, td: Path) -> RLDataLogger:
        return RLDataLogger(
            log_path=td / "ev.jsonl",
            project="V3Test",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
        )

    def test_n_emb_field_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_retrieval(
                task_id="t1",
                task_type="x",
                query="q",
                nodes=[
                    {
                        "title": "A",
                        "score": 0.9,
                        "tier": "top_k",
                        "n_emb": [0.1, 0.2, 0.3],
                    }
                ],
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["nodes"][0]["n_emb"] == pytest.approx([0.1, 0.2, 0.3])

    def test_linked_embs_field_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_retrieval(
                task_id="t2",
                task_type="x",
                query="q",
                nodes=[
                    {
                        "title": "A",
                        "score": 0.9,
                        "tier": "top_k",
                        "linked_embs": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
                        "linked_type_names": ["concept", "concept", "tool"],
                    }
                ],
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            n = event["nodes"][0]
            assert len(n["linked_embs"]) == 3
            assert n["linked_type_names"] == ["concept", "concept", "tool"]

    def test_legacy_emb_field_still_persisted(self) -> None:
        """Back-compat: legacy callers (v2 shape) still work."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_retrieval(
                task_id="t3",
                task_type="x",
                query="q",
                nodes=[
                    {"title": "A", "score": 0.9, "tier": "top_k", "emb": [0.7, 0.8]}
                ],
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["nodes"][0]["emb"] == pytest.approx([0.7, 0.8])
            assert "n_emb" not in event["nodes"][0]

    def test_node_type_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log = self._logger(td_path)
            log.log_retrieval(
                task_id="t4",
                task_type="x",
                query="q",
                nodes=[
                    {
                        "title": "A",
                        "score": 0.9,
                        "tier": "top_k",
                        "node_type": "concept",
                    }
                ],
            )
            event = json.loads((td_path / "ev.jsonl").read_text().strip())
            assert event["nodes"][0]["node_type"] == "concept"


# ----------------------------------------------------------------------
# 4. RLTelemetryWriter pass-through of v3 fields (local + queue payload).
# ----------------------------------------------------------------------


class TestTelemetryWriterPassThrough:
    """v0.2.47 RL-6c: the writer no longer writes JSONL. It POSTs a v3
    envelope to the hub. Tests inject a stub `hub_post_fn` that captures
    what would have been posted, so they can assert on the envelope
    without standing up a real hub server.
    """

    def _writer_with_captured_posts(self):
        captured: list[dict] = []

        def stub_post(envelope: dict, timeout: float = 2.0) -> bool:
            captured.append(envelope)
            return True

        w = RLTelemetryWriter(
            project="X",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=stub_post,
        )
        return w, captured

    def test_log_citations_v3_signature_accepted(self) -> None:
        """Calling log_citations with the new kwargs MUST NOT raise + posts
        an envelope with the literal_cited flag dict inside payload_json."""
        w, captured = self._writer_with_captured_posts()
        w.log_citations(
            task_id="t1",
            task_type="x",
            citations={"A": True},
            cosine_sims={"A": 0.9},
            literal_cited={"A": True},
            cross_encoder_cited=None,
        )
        assert len(captured) == 1
        env = captured[0]
        assert env["event_type"] == "citation"
        event = json.loads(env["payload_json"])
        assert event["literal_cited"] == {"A": True}

    def test_build_citation_payload_includes_v3_fields(self) -> None:
        w, _ = self._writer_with_captured_posts()
        payload = w._build_citation_payload(
            task_id="t1",
            task_type="x",
            citations={"A": True},
            cosine_sims={"A": 0.9},
            literal_cited={"A": True},
            cross_encoder_cited={"A": False},
        )
        assert payload["literal_cited"] == {"A": True}
        assert payload["cross_encoder_cited"] == {"A": False}
        assert payload["schema_version"] == 3

    def test_build_retrieval_payload_includes_v3_node_fields(self) -> None:
        w, _ = self._writer_with_captured_posts()
        payload = w._build_retrieval_payload(
            task_id="t1",
            task_type="x",
            query="q",
            nodes=[
                {
                    "title": "A",
                    "score": 0.9,
                    "tier": "top_k",
                    "n_emb": [0.1, 0.2],
                    "linked_embs": [[0.3, 0.4]],
                    "linked_type_names": ["concept"],
                    "node_type": "concept",
                }
            ],
            session_id="",
            query_emb=None,
        )
        n = payload["nodes"][0]
        assert n["n_emb"] == [0.1, 0.2]
        assert n["linked_embs"] == [[0.3, 0.4]]
        assert n["linked_type_names"] == ["concept"]
        assert n["node_type"] == "concept"


# ----------------------------------------------------------------------
# 5. EmbeddingService embed-result memo cache.
# ----------------------------------------------------------------------


class TestEmbeddingServiceMemoCache:
    """v0.2.47 RL-3: per-instance memo for `embed_text` / `embed_code`.

    Behavior contract:
    - Second call with same text returns cached vector (no backend hit).
    - Different text triggers a real call.
    - LRU eviction when cap is exceeded.
    """

    def _make_service(self):
        """Build a service whose backend calls we can intercept.

        v0.2.48 CI fix: ``EmbeddingService.for_project`` probes the
        live text + code backends at construction time and raises
        ``NoEmbeddingBackendError`` when neither is reachable. The memo
        tests below mock ``_embed_text_via_active`` so a real backend
        is never needed at call time, but the probe fired earlier
        broke CI (no ollama / codeembed service in GitHub Actions).
        Patch both readiness probes to return True for the duration of
        the constructor, then return the constructed service with the
        patches expired — the memo paths under test never touch the
        probes again. This keeps the structural shape (an
        EmbeddingService built via the canonical factory) while making
        the test hermetic.
        """
        from vco_lib.embedding_service import EmbeddingService

        with patch.object(
            EmbeddingService, "text_backend_ready", return_value=True
        ), patch.object(
            EmbeddingService, "code_backend_ready", return_value=True
        ):
            svc = EmbeddingService.for_project()
        return svc

    def test_repeat_call_returns_cached(self) -> None:
        svc = self._make_service()
        call_count = {"n": 0}

        def fake_embed(text: str) -> list[float]:
            call_count["n"] += 1
            return [0.1, 0.2, 0.3]

        with patch.object(svc, "_embed_text_via_active", side_effect=fake_embed):
            v1 = svc.embed_text("hello world")
            v2 = svc.embed_text("hello world")
            assert v1 == v2
            assert call_count["n"] == 1  # second call hit the cache

    def test_different_texts_call_backend_each_time(self) -> None:
        svc = self._make_service()
        call_count = {"n": 0}

        def fake_embed(text: str) -> list[float]:
            call_count["n"] += 1
            return [float(call_count["n"])]

        with patch.object(svc, "_embed_text_via_active", side_effect=fake_embed):
            svc.embed_text("a")
            svc.embed_text("b")
            svc.embed_text("c")
            assert call_count["n"] == 3

    def test_eviction_when_cap_exceeded(self) -> None:
        svc = self._make_service()
        svc._embed_memo_cap = 3  # tight cap for the test

        def fake_embed(text: str) -> list[float]:
            return [hash(text) % 100 / 100.0]

        with patch.object(svc, "_embed_text_via_active", side_effect=fake_embed):
            svc.embed_text("a")
            svc.embed_text("b")
            svc.embed_text("c")
            assert len(svc._embed_memo_text) == 3
            svc.embed_text("d")  # should evict "a"
            assert len(svc._embed_memo_text) == 3

    def test_code_memo_is_separate_from_text_memo(self) -> None:
        svc = self._make_service()

        text_calls = {"n": 0}
        code_calls = {"n": 0}

        def fake_text(t: str) -> list[float]:
            text_calls["n"] += 1
            return [0.1]

        def fake_code(c: str) -> list[float]:
            code_calls["n"] += 1
            return [0.2]

        with patch.object(svc, "_embed_text_via_active", side_effect=fake_text), \
             patch.object(svc, "_embed_code_via_active", side_effect=fake_code):
            svc.embed_text("foo")
            svc.embed_code("foo")
            assert text_calls["n"] == 1
            assert code_calls["n"] == 1  # not deduplicated across kinds
            # Caching is per-kind:
            svc.embed_text("foo")
            svc.embed_code("foo")
            assert text_calls["n"] == 1
            assert code_calls["n"] == 1


# ----------------------------------------------------------------------
# 6. _memo_key helper sanity.
# ----------------------------------------------------------------------


class TestMemoKey:
    def test_stable_for_same_text(self) -> None:
        from vco_lib.embedding_service import _memo_key

        assert _memo_key("hello") == _memo_key("hello")

    def test_different_for_different_text(self) -> None:
        from vco_lib.embedding_service import _memo_key

        assert _memo_key("hello") != _memo_key("world")

    def test_returns_24_hex_chars(self) -> None:
        from vco_lib.embedding_service import _memo_key

        key = _memo_key("any text here")
        assert len(key) == 24
        int(key, 16)  # must be valid hex; raises otherwise


# ----------------------------------------------------------------------
# 7. v0.2.47 RL-6c: HARD CUTOVER from JSONL to hub POST.
# ----------------------------------------------------------------------


class TestHubCutoverEnvelopeShape:
    """The writer now POSTs an envelope matching the hub's
    `PostEventBody` schema; tests pin the shape so a future hub-side
    schema change can't silently drift the writer."""

    def _make(self, hub_post_fn):
        return RLTelemetryWriter(
            project="VCO_dev",
            project_id="uuid-fake-project",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=hub_post_fn,
        )

    def test_retrieval_envelope_carries_indexed_columns(self) -> None:
        captured: list[dict] = []
        w = self._make(lambda env, timeout=2.0: captured.append(env) or True)
        w.log_retrieval(
            task_id="t1",
            task_type="mcp_interactive",
            query="hello",
            nodes=[],
            session_id="sess-1",
        )
        assert len(captured) == 1
        env = captured[0]
        assert env["event_type"] == "retrieval"
        assert env["schema_version"] == 3
        assert env["project_id"] == "uuid-fake-project"
        assert env["project_name"] == "VCO_dev"
        assert env["task_id"] == "t1"
        assert env["task_type"] == "mcp_interactive"
        assert env["embedding_source"] == "qwen3"
        assert env["embedding_dim"] == 1024
        assert env["embedding_model"] == "qwen3-embedding:0.6b"
        # payload_json is the full v3 event JSON.
        event = json.loads(env["payload_json"])
        assert event["event"] == "retrieval"
        assert event["schema_version"] == 3
        assert event["task_id"] == "t1"
        assert event["session_id"] == "sess-1"

    def test_citation_envelope_carries_indexed_columns(self) -> None:
        captured: list[dict] = []
        w = self._make(lambda env, timeout=2.0: captured.append(env) or True)
        w.log_citations(
            task_id="t2",
            task_type="x",
            citations={"A": True},
        )
        assert len(captured) == 1
        env = captured[0]
        assert env["event_type"] == "citation"
        assert env["task_id"] == "t2"
        event = json.loads(env["payload_json"])
        assert event["event"] == "citation"
        assert event["citations"] == {"A": True}

    def test_project_id_none_round_trips_as_null(self) -> None:
        captured: list[dict] = []
        w = RLTelemetryWriter(
            project="VCO_dev",
            project_id=None,  # free-tier: no FK
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=lambda env, timeout=2.0: captured.append(env) or True,
        )
        w.log_retrieval(
            task_id="t1",
            task_type="x",
            query="q",
            nodes=[],
        )
        env = captured[0]
        assert env["project_id"] is None
        assert env["project_name"] == "VCO_dev"

    def test_failure_mode_propagates_into_payload(self) -> None:
        captured: list[dict] = []
        w = self._make(lambda env, timeout=2.0: captured.append(env) or True)
        w.log_retrieval(
            task_id="t1",
            task_type="x",
            query="q",
            nodes=[],
            failure_mode="all_collections_schema_missing",
            failed_collections=["VCODev_KG", "Shared_KG"],
        )
        event = json.loads(captured[0]["payload_json"])
        assert event["failure_mode"] == "all_collections_schema_missing"
        assert event["failed_collections"] == ["VCODev_KG", "Shared_KG"]


class TestHubPostSoftFail:
    """When the hub POST fails (returns False or raises), the writer
    MUST NOT propagate the error to the caller. Lost events stay lost."""

    def test_post_returns_false_does_not_raise(self) -> None:
        w = RLTelemetryWriter(
            project="X",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=lambda env, timeout=2.0: False,
        )
        # No exception even though hub returned False.
        w.log_retrieval(
            task_id="t1", task_type="x", query="q", nodes=[]
        )
        w.log_citations(task_id="t2", task_type="x", citations={})

    def test_post_raising_does_not_propagate(self) -> None:
        def boom(env, timeout=2.0):
            raise RuntimeError("simulated hub crash")

        w = RLTelemetryWriter(
            project="X",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=boom,
        )
        # No exception even though stub raises.
        w.log_retrieval(
            task_id="t1", task_type="x", query="q", nodes=[]
        )
        w.log_citations(task_id="t2", task_type="x", citations={})


class TestLocalLoggingDisabledEnv:
    """RL_LOCAL_LOGGING_DISABLED env var still gates the hub-write path
    (same opt-out semantics as the pre-v0.2.47 JSONL gate)."""

    def test_env_set_skips_hub_post(self) -> None:
        from unittest.mock import patch

        captured: list[dict] = []
        w = RLTelemetryWriter(
            project="X",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=lambda env, timeout=2.0: captured.append(env) or True,
        )
        with patch.dict("os.environ", {"RL_LOCAL_LOGGING_DISABLED": "true"}, clear=False):
            w.log_retrieval(
                task_id="t1", task_type="x", query="q", nodes=[]
            )
        # No hub post happened because the env opt-out was set.
        assert captured == []

    def test_env_unset_posts_normally(self) -> None:
        from unittest.mock import patch

        captured: list[dict] = []
        w = RLTelemetryWriter(
            project="X",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=lambda env, timeout=2.0: captured.append(env) or True,
        )
        env_without = dict(os.environ)
        env_without.pop("RL_LOCAL_LOGGING_DISABLED", None)
        with patch.dict("os.environ", env_without, clear=True):
            w.log_retrieval(
                task_id="t1", task_type="x", query="q", nodes=[]
            )
        assert len(captured) == 1
