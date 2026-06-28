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


class RLClientRlUpdateActiveEmbeddingTest(unittest.TestCase):
    """v0.2.40 F1: RLClient.rl_update plumbs active_embedding into POST body.

    Mirrors the existing ``cache_nodes`` contract (which already carries
    both ``embedding_source`` and ``active_embedding``). Without this,
    training signals from a mismatched embedding source (e.g. arctic2
    events training a qwen3 NN) could silently corrupt the wrong neural
    network. Per multi-Opus pre-push review highest-risk silent-
    correctness gap #1.
    """

    def test_active_embedding_threaded_into_payload(self):
        from claude_mcp_servers.rl_client.client import RLClient

        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""
            def json(self):
                return {"ok": True, "scheduled": 1}

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
        result = _run(rl.rl_update(
            task_ids=["task-xyz"],
            agent_output="Claude's answer text",
        ))
        self.assertTrue(result.ok)
        # The /rl_update endpoint must be hit, not /cache_nodes.
        self.assertTrue(captured["url"].endswith("/rl_update"))
        # Both fields present — mirror of the cache_nodes payload shape.
        self.assertIn("active_embedding", captured["json"])
        self.assertEqual(captured["json"]["active_embedding"], "qwen3")
        self.assertIn("embedding_source", captured["json"])
        self.assertEqual(captured["json"]["embedding_source"], "qwen3")

    def test_active_embedding_override_honored(self):
        """Constructor-level override (e.g. arctic2) flows into rl_update."""
        from claude_mcp_servers.rl_client.client import RLClient

        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""
            def json(self):
                return {"ok": True}

        class _FakeClient:
            async def post(self, url, json=None, timeout=None):
                captured["json"] = json
                return _FakeResp()
            async def get(self, url, timeout=None):
                return _FakeResp()
            async def aclose(self):
                pass

        rl = RLClient(
            text_dim=1024,
            active_embedding="arctic2",
            base_url="http://127.0.0.1:0",
            client=_FakeClient(),
        )
        _run(rl.rl_update(
            task_ids=["task-arctic"],
            agent_output="answer",
        ))
        self.assertEqual(captured["json"]["active_embedding"], "arctic2")
        self.assertEqual(captured["json"]["embedding_source"], "arctic2")

    def test_active_embedding_defaults_to_qwen3_when_unset(self):
        """Backward-compat: constructor without explicit active_embedding
        falls back to the canonical 'qwen3' default (matches the existing
        ``RLClient.__init__`` contract — see ``self.active_embedding =
        str(active_embedding or 'qwen3')``)."""
        from claude_mcp_servers.rl_client.client import RLClient

        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""
            def json(self):
                return {"ok": True}

        class _FakeClient:
            async def post(self, url, json=None, timeout=None):
                captured["json"] = json
                return _FakeResp()
            async def get(self, url, timeout=None):
                return _FakeResp()
            async def aclose(self):
                pass

        # No active_embedding arg → defaults to "qwen3" per constructor.
        rl = RLClient(base_url="http://127.0.0.1:0", client=_FakeClient())
        _run(rl.rl_update(
            task_ids=["t"],
            agent_output="answer",
        ))
        # Default must still be present (NOT omitted) — the server uses
        # this to pin the training signal source; an absent field would
        # require the server to guess.
        self.assertIn("active_embedding", captured["json"])
        self.assertEqual(captured["json"]["active_embedding"], "qwen3")
        self.assertIn("embedding_source", captured["json"])
        self.assertEqual(captured["json"]["embedding_source"], "qwen3")

    def test_payload_preserves_existing_fields(self):
        """The new fields must coexist with task_ids/agent_output/task_type;
        adding active_embedding must not displace any prior contract."""
        from claude_mcp_servers.rl_client.client import RLClient

        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""
            def json(self):
                return {"ok": True}

        class _FakeClient:
            async def post(self, url, json=None, timeout=None):
                captured["json"] = json
                return _FakeResp()
            async def get(self, url, timeout=None):
                return _FakeResp()
            async def aclose(self):
                pass

        rl = RLClient(
            active_embedding="qwen3",
            base_url="http://127.0.0.1:0",
            client=_FakeClient(),
        )
        _run(rl.rl_update(
            task_ids=["t1", "t2"],
            agent_output="full answer",
            task_type="mcp_interactive",
        ))
        body = captured["json"]
        # Pre-existing contract fields untouched.
        self.assertEqual(body["task_ids"], ["t1", "t2"])
        self.assertEqual(body["agent_output"], "full answer")
        self.assertEqual(body["task_type"], "mcp_interactive")
        # New fields added.
        self.assertEqual(body["active_embedding"], "qwen3")
        self.assertEqual(body["embedding_source"], "qwen3")

    def test_get_rl_client_re_keys_on_mid_session_embedding_flip(self):
        """v0.2.42 RT-1: _get_rl_client() returns a new RLClient instance
        whose active_embedding matches the *current* ACTIVE_EMBEDDING env
        value, not the value read at import time.

        Pre-fix: _rl_client_instance was a bare None-or-RLClient singleton;
        the first call froze ACTIVE_EMBEDDING=qwen3 for the entire process
        lifetime even if the user later flipped to arctic2 via the launcher.

        v0.2.42 RT-1: the dict _rl_client_instances[<key>] is keyed on the
        current env value, so a flip yields a fresh client.

        v0.2.49 (commit 0ca7ee87): the cache key shape expanded from a
        bare `active_embedding` string to a `(active_embedding,
        project_id)` tuple so per-project routing via the
        ``X-VCT-Project-ID`` header gets a distinct client per project.
        The mid-session-flip semantics are unchanged for the
        active_embedding dimension; this test asserts membership via
        the resolved project_id so it survives both env shapes (hub
        unreachable → project_id=None → tuple key `(emb, None)`; hub
        reachable → tuple key `(emb, <uuid>)`).
        """
        import os
        import claude_mcp_servers.weaviate_mcp.server as srv

        # Clear the instance cache so this test starts clean.
        srv._rl_client_instances.clear()

        try:
            # Phase 1: first call with qwen3.
            os.environ["ACTIVE_EMBEDDING"] = "qwen3"
            client_qwen3 = srv._get_rl_client()
            if client_qwen3 is None:
                self.skipTest("RLClient import not available in this env")

            self.assertEqual(
                getattr(client_qwen3, "active_embedding", None),
                "qwen3",
                "First client must carry active_embedding=qwen3",
            )
            # v0.2.49: cache keys are tuples (embedding, project_id).
            # Assert membership via any-key-with-our-embedding match so
            # the test passes regardless of whether the hub resolver
            # returned a project_id (hub-reachable: (emb, uuid))
            # or fell back (hub-unreachable: (emb, None)).
            qwen3_keys = [k for k in srv._rl_client_instances if k[0] == "qwen3"]
            self.assertTrue(
                qwen3_keys,
                f"Expected at least one cache entry keyed on 'qwen3', "
                f"got keys: {list(srv._rl_client_instances.keys())}",
            )

            # Phase 2: mid-session flip to arctic2.
            os.environ["ACTIVE_EMBEDDING"] = "arctic2"
            client_arctic = srv._get_rl_client()
            self.assertIsNotNone(client_arctic)
            self.assertEqual(
                getattr(client_arctic, "active_embedding", None),
                "arctic2",
                "Second client must carry active_embedding=arctic2",
            )

            # The two clients must be distinct objects.
            self.assertIsNot(
                client_qwen3,
                client_arctic,
                "Mid-session flip must produce a distinct RLClient, not reuse the old one",
            )

            # Both entries survive in the dict (tombstone pattern).
            # v0.2.49: assert via any-key-with-the-embedding match
            # for both arms, same reason as Phase 1 above.
            qwen3_keys = [k for k in srv._rl_client_instances if k[0] == "qwen3"]
            arctic_keys = [k for k in srv._rl_client_instances if k[0] == "arctic2"]
            self.assertTrue(qwen3_keys, "qwen3 cache entry must survive flip")
            self.assertTrue(arctic_keys, "arctic2 cache entry must exist after flip")

        finally:
            # Restore original env state so we don't leak into adjacent tests.
            os.environ.pop("ACTIVE_EMBEDDING", None)
            srv._rl_client_instances.clear()


class RLTelemetryWriterEmbeddingFieldsTest(unittest.TestCase):
    """Fix 2: writer construction never ships blank embedding_source."""

    def setUp(self):
        # Force fresh writer cache each test.
        # v0.2.40 F2: writer is now keyed by (project, embedding_source);
        # call _reset_rl_telemetry_writers() to drop all cached writers.
        # (Legacy `_rl_telemetry_writer_instance = None` kept for back-compat
        # with any external test that still references the tombstone.)
        import claude_mcp_servers.weaviate_mcp.server as srv
        srv._rl_telemetry_writer_instance = None
        srv._reset_rl_telemetry_writers()
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

    def test_cosine_mismatched_length_refuses(self):
        # F-D (v0.2.70): mismatched lengths mean different embedding spaces
        # (e.g. 1024-dim qwen3 vs 2048-dim codesage). The pre-F-D body
        # truncated to min(len) and returned a plausible value (~0.75 for a
        # cross-model pair), silently passing the 0.6 citation gate with
        # garbage. It now REFUSES — returns 0.0 — making the docstring true.
        from claude_mcp_servers.weaviate_mcp.server import _cosine
        self.assertEqual(_cosine([1.0, 0.0, 0.0], [1.0, 0.0]), 0.0)
        self.assertEqual(_cosine([1.0] * 1024, [1.0] * 2048), 0.0)
        # Same-length comparison still works.
        self.assertAlmostEqual(_cosine([1.0, 0.0], [1.0, 0.0]), 1.0, places=6)


class ExtractObjVectorTest(unittest.TestCase):
    """Fix 3: _extract_obj_vector handles all Weaviate v4 shapes."""

    def test_dict_with_target(self):
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            vector = {"qwen3_embed": [0.1, 0.2], "codesage_embed": [0.3, 0.4]}

        result = _extract_obj_vector(_Obj(), "qwen3_embed")
        self.assertEqual(result, [0.1, 0.2])

    def test_dict_missing_target_refuses(self):
        # F-D (v0.2.70): asking for an absent active slot returns None — it
        # must NOT fall back to "first non-empty slot", which can pull a
        # FOREIGN embedding space (e.g. a legacy ollama/arctic slot when the
        # active model is qwen3). A missing active slot = "no comparable
        # vector for this model".
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            vector = {"ollama_embed": [0.1, 0.2]}  # foreign slot only

        result = _extract_obj_vector(_Obj(), "qwen3_embed")
        self.assertIsNone(result)

    def test_dict_no_target_picks_first_slot_agnostic(self):
        # Slot-agnostic caller (target_name="") — legacy single-vector mode,
        # no active-slot ambiguity, so the first non-empty slot is correct.
        from claude_mcp_servers.weaviate_mcp.server import _extract_obj_vector

        class _Obj:
            vector = {"default": [0.1, 0.2]}

        result = _extract_obj_vector(_Obj(), "")
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
