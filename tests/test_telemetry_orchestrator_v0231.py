# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for v0.2.31 RL telemetry orchestrator-side fixes.

Pins three orchestrator-side fixes that complete the RL telemetry audit
(2026-05-23). See rl-chat-response-v0.2.31-2026-05-23.md "Item 2".

Fix 1: ``RLClient.cache_nodes`` forwards ``session_id`` into the POST
payload so the container (vct-rl-reranker v0.2.4+) can persist it.

Fix 2: ``_get_rl_telemetry_writer`` constructs the writer with non-empty
``embedding_source`` / ``embedding_model`` / ``embedding_dim`` even when
``EmbeddingService.for_project()`` fails (env-only fallback).

Fix 3: ``_rl_cache_and_rerank`` propagates ``emb`` + cosine fields
(``cos_qn`` / ``cos_ql`` / ``cos_nl``) from upstream candidate dicts into
``log_retrieval`` payloads. ``_extract_obj_vector`` + ``_cosine`` helpers
work without numpy (pure python).
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class RLClientCacheNodesSessionIdTest(unittest.TestCase):
    """Fix 1: RLClient.cache_nodes plumbs session_id through to POST body."""

    def test_session_id_threaded_into_payload(self):
        from claude_mcp_servers.rl_client.client import RLClient

        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""
            def json(self):
                return {"top_k": [{"title": "N1", "score": 0.9}]}

        class _FakeClient:
            async def post(self, url, json=None, timeout=None):
                captured["url"] = url
                captured["json"] = json
                return _FakeResp()
            async def get(self, url, timeout=None):
                return _FakeResp()
            async def aclose(self):
                pass

        rl = RLClient(
            text_dim=1024,
            active_embedding="qwen3",
            base_url="http://127.0.0.1:0",
            client=_FakeClient(),
        )
        result = _run(rl.cache_nodes(
            query="q",
            nodes=[{"title": "N1", "score": 0.5}],
            top_k=1,
            task_id="task-xyz",
            session_id="claude-session-abc",
        ))
        self.assertEqual(result, [{"title": "N1", "score": 0.9}])
        self.assertEqual(captured["json"]["session_id"], "claude-session-abc")

    def test_session_id_defaults_to_empty(self):
        """Backward-compat: omitted session_id must serialize as ''."""
        from claude_mcp_servers.rl_client.client import RLClient

        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""
            def json(self):
                return {"top_k": []}

        class _FakeClient:
            async def post(self, url, json=None, timeout=None):
                captured["json"] = json
                return _FakeResp()
            async def get(self, url, timeout=None):
                return _FakeResp()
            async def aclose(self):
                pass

        rl = RLClient(base_url="http://127.0.0.1:0", client=_FakeClient())
        _run(rl.cache_nodes(
            query="q", nodes=[], top_k=1, task_id="t",
        ))
        self.assertIn("session_id", captured["json"])
        self.assertEqual(captured["json"]["session_id"], "")


class RLTelemetryWriterEmbeddingFieldsTest(unittest.TestCase):
    """Fix 2: writer construction never ships blank embedding_source."""

    def setUp(self):
        # Force fresh singleton each test.
        import claude_mcp_servers.weaviate_mcp.server as srv
        srv._rl_telemetry_writer_instance = None
        self._srv = srv

    def test_writer_construction_falls_back_to_env_when_service_fails(self):
        """EmbeddingService.for_project raising → writer still gets
        non-empty embedding_source/model/dim from env defaults."""
        import claude_mcp_servers.weaviate_mcp.server as srv

        # Mock the EmbeddingService probe to always raise.
        with patch.object(srv, "ACTIVE_EMBEDDING", "qwen3"):
            with patch.object(srv, "EMBEDDING_MODEL", "qwen3-embedding:0.6b"):
                # Patch the import inside _get_rl_telemetry_writer to fail.
                # The except branch is the path that exercises env defaults.
                with patch.dict("sys.modules", {"vco_lib.embedding_service": None}):
                    writer = srv._get_rl_telemetry_writer()

        self.assertIsNotNone(writer)
        self.assertEqual(writer._embedding_source, "qwen3")
        self.assertEqual(writer._embedding_model, "qwen3-embedding:0.6b")
        self.assertEqual(writer._embedding_dim, 1024)

    def test_embedding_dim_for_known_models(self):
        from claude_mcp_servers.weaviate_mcp.server import _embedding_dim_for
        self.assertEqual(_embedding_dim_for("qwen3-embedding:0.6b"), 1024)
        self.assertEqual(_embedding_dim_for("snowflake-arctic-embed2:latest"), 1024)
        self.assertEqual(_embedding_dim_for("codesage-large-v2"), 2048)
        self.assertEqual(_embedding_dim_for("text-embedding-3-small"), 1536)
        self.assertEqual(_embedding_dim_for("openai-something"), 1536)
        # Unknown model → safe default 1024
        self.assertEqual(_embedding_dim_for("unknown-model"), 1024)
        # Empty string → safe default
        self.assertEqual(_embedding_dim_for(""), 1024)


class CosineHelperTest(unittest.TestCase):
    """Fix 3: _cosine pure-python helper soft-fails on bad inputs."""

    def test_cosine_basic(self):
        from claude_mcp_servers.weaviate_mcp.server import _cosine
        # Orthogonal
        self.assertAlmostEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)
        # Parallel
        self.assertAlmostEqual(_cosine([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]), 1.0, places=6)
        # Anti-parallel
        self.assertAlmostEqual(_cosine([1.0, 0.0], [-1.0, 0.0]), -1.0, places=6)

    def test_cosine_soft_fail(self):
        from claude_mcp_servers.weaviate_mcp.server import _cosine
        # Zero-norm
        self.assertEqual(_cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        # Empty
        self.assertEqual(_cosine([], [1.0]), 0.0)
        self.assertEqual(_cosine([1.0], []), 0.0)
        # None
        self.assertEqual(_cosine(None, [1.0]), 0.0)
        # Non-numeric → caught by try/except, returns 0.0
        self.assertEqual(_cosine(["bad"], [1.0]), 0.0)

    def test_cosine_mismatched_length_truncates(self):
        from claude_mcp_servers.weaviate_mcp.server import _cosine
        # min(len(a), len(b)) — truncates safely; doesn't raise.
        val = _cosine([1.0, 0.0, 0.0], [1.0, 0.0])
        self.assertAlmostEqual(val, 1.0, places=6)


class ExtractObjVectorTest(unittest.TestCase):
    """Fix 3: _extract_obj_vector handles all Weaviate v4 shapes."""

    def test_dict_with_target(self):
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            vector = {"qwen3_embed": [0.1, 0.2], "codesage_embed": [0.3, 0.4]}

        result = _extract_obj_vector(_Obj(), "qwen3_embed")
        self.assertEqual(result, [0.1, 0.2])

    def test_dict_missing_target_picks_first(self):
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            vector = {"qwen3_embed": [0.1, 0.2]}

        # Asking for non-existent target → first non-empty slot wins.
        result = _extract_obj_vector(_Obj(), "missing_slot")
        self.assertEqual(result, [0.1, 0.2])

    def test_unwrapped_list(self):
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            vector = [0.5, 0.6, 0.7]

        result = _extract_obj_vector(_Obj(), "any")
        self.assertEqual(result, [0.5, 0.6, 0.7])

    def test_no_vector_returns_none(self):
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            vector = None

        self.assertIsNone(_extract_obj_vector(_Obj(), "any"))

    def test_missing_attribute_returns_none(self):
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            pass

        self.assertIsNone(_extract_obj_vector(_Obj(), "any"))


class RlCacheAndRerankEmbCosPropagationTest(unittest.TestCase):
    """Fix 3: emb + cos_qn / cos_ql / cos_nl flow through log_retrieval payload."""

    def test_emb_and_cosines_propagate_to_log_nodes(self):
        import claude_mcp_servers.weaviate_mcp.server as srv

        captured = []

        class _Stub:
            def log_retrieval(self, **kwargs):
                captured.append(dict(kwargs))

        nodes = [
            {
                "title": "N1",
                "score": 0.9,
                "emb": [0.1, 0.2, 0.3],
                "cos_qn": 0.85,
            },
            {
                "title": "N2",
                "score": 0.7,
                "emb": [0.4, 0.5, 0.6],
                "cos_qn": 0.55,
                "cos_ql": 0.6,
                "cos_nl": 0.4,
            },
            # No emb / cos — must not crash, must be omitted.
            {"title": "N3", "score": 0.5},
        ]

        with patch.object(srv, "_get_rl_telemetry_writer", return_value=_Stub()):
            with patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
                _run(srv._rl_cache_and_rerank(
                    "task-cos-prop", "q", nodes, 2,
                ))

        self.assertEqual(len(captured), 1)
        log_nodes = captured[0]["nodes"]
        self.assertEqual(len(log_nodes), 3)

        # N1: emb + cos_qn, no cos_ql/nl
        self.assertEqual(log_nodes[0]["emb"], [0.1, 0.2, 0.3])
        self.assertEqual(log_nodes[0]["cos_qn"], 0.85)
        self.assertNotIn("cos_ql", log_nodes[0])
        self.assertNotIn("cos_nl", log_nodes[0])

        # N2: emb + all three cosines
        self.assertEqual(log_nodes[1]["emb"], [0.4, 0.5, 0.6])
        self.assertEqual(log_nodes[1]["cos_qn"], 0.55)
        self.assertEqual(log_nodes[1]["cos_ql"], 0.6)
        self.assertEqual(log_nodes[1]["cos_nl"], 0.4)

        # N3: bare — none of the optional fields
        self.assertNotIn("emb", log_nodes[2])
        self.assertNotIn("cos_qn", log_nodes[2])
        self.assertNotIn("cos_ql", log_nodes[2])
        self.assertNotIn("cos_nl", log_nodes[2])


if __name__ == "__main__":
    unittest.main()
