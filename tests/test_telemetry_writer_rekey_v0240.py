# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.40 F2: telemetry writer re-keyed on (project, ACTIVE_EMBEDDING).

Pre-F2: ``_get_rl_telemetry_writer`` returned a single module-global
singleton constructed at first call. The (project, embedding_source)
tuple it tagged events with was frozen at that first-call moment, so
mid-session env changes (ACTIVE_EMBEDDING flip qwen3→arctic2,
PROJECT_NAME re-resolution from launcher.db adopt) silently shipped
stale tags into the offline training corpus for the lifetime of the
MCP subprocess.

Post-F2: writer is keyed by ``(project, embedding_source)`` in
``_rl_telemetry_writers: dict``. Each distinct env tuple gets its own
writer with current tags. ``_reset_rl_telemetry_writers()`` clears
the cache (used by tests; also a reusable shutdown hook).

Tests:
- T1: first call creates writer for (project=A, embedding=qwen3)
- T2: same env → returns the SAME writer instance (idempotent)
- T3: ACTIVE_EMBEDDING flip mid-session → NEW writer with new tag
       (the key correctness gap pre-F2)
- T4: multiple distinct tuples → multiple writers cached
- T5: _reset_rl_telemetry_writers() clears all cached writers

Per multi-Opus pre-push review highest-risk silent-correctness gap #2.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TelemetryWriterRekeyV0240Test(unittest.TestCase):
    """Writer caching is keyed on (project, embedding_source)."""

    def setUp(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        # Drop any state leaked from earlier tests in this file or in
        # the broader suite (pytest's import-once semantics means the
        # module-level dict survives test boundaries otherwise).
        srv._reset_rl_telemetry_writers()
        self._srv = srv

    def tearDown(self):
        # Leave the cache clean for the next test class.
        self._srv._reset_rl_telemetry_writers()

    # ---- T1 -----------------------------------------------------------

    def test_t1_first_call_creates_writer_for_tuple(self):
        """First call with (project=A, embedding=qwen3) constructs and
        caches a writer tagged accordingly."""
        srv = self._srv
        # Cache empty before first call.
        self.assertEqual(len(srv._rl_telemetry_writers), 0)
        # Patch env + skip the EmbeddingService probe so we exercise the
        # env-fallback path (deterministic across machines).
        with patch.object(srv, "ACTIVE_EMBEDDING", "qwen3"):
            with patch.object(srv, "EMBEDDING_MODEL", "qwen3-embedding:0.6b"):
                with patch.dict(os.environ, {
                    "ACTIVE_EMBEDDING": "qwen3",
                    "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
                    "PROJECT_NAME": "ProjectA",
                }, clear=False):
                    with patch.dict(
                        "sys.modules",
                        {"vco_lib.embedding_service": None},
                    ):
                        with patch.object(
                            srv, "_try_resolve_project_config",
                            return_value=None,
                        ):
                            writer = srv._get_rl_telemetry_writer()
        self.assertIsNotNone(writer)
        self.assertEqual(writer._embedding_source, "qwen3")
        # Exactly one cached writer.
        self.assertEqual(len(srv._rl_telemetry_writers), 1)
        # And the key is what we expect.
        keys = list(srv._rl_telemetry_writers.keys())
        self.assertEqual(len(keys), 1)
        project, emb = keys[0]
        self.assertEqual(emb, "qwen3")
        # Project went through sanitize_for_weaviate_class.
        self.assertTrue(project, "project tag must be non-empty")

    # ---- T2 -----------------------------------------------------------

    def test_t2_same_env_returns_same_instance(self):
        """Second call with identical env returns the SAME writer
        object (caching is by reference, not just by value)."""
        srv = self._srv
        with patch.object(srv, "ACTIVE_EMBEDDING", "qwen3"):
            with patch.object(srv, "EMBEDDING_MODEL", "qwen3-embedding:0.6b"):
                with patch.dict(os.environ, {
                    "ACTIVE_EMBEDDING": "qwen3",
                    "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
                    "PROJECT_NAME": "ProjectA",
                }, clear=False):
                    with patch.dict(
                        "sys.modules",
                        {"vco_lib.embedding_service": None},
                    ):
                        with patch.object(
                            srv, "_try_resolve_project_config",
                            return_value=None,
                        ):
                            w1 = srv._get_rl_telemetry_writer()
                            w2 = srv._get_rl_telemetry_writer()
        self.assertIs(w1, w2, "same env tuple must return same instance")
        self.assertEqual(
            len(srv._rl_telemetry_writers), 1,
            "no duplicate writers for the same key",
        )

    # ---- T3 -----------------------------------------------------------

    def test_t3_active_embedding_flip_yields_new_writer(self):
        """Mid-session ACTIVE_EMBEDDING flip → next factory call must
        return a NEW writer tagged with the new embedding source.

        This is the silent-correctness gap that v0.2.40 F2 closes:
        pre-F2 the singleton was frozen and would keep stamping
        ``embedding_source=qwen3`` even after the user flipped to
        arctic2 mid-session.
        """
        srv = self._srv
        # First call: ACTIVE_EMBEDDING=qwen3.
        with patch.object(srv, "ACTIVE_EMBEDDING", "qwen3"):
            with patch.object(srv, "EMBEDDING_MODEL", "qwen3-embedding:0.6b"):
                with patch.dict(os.environ, {
                    "ACTIVE_EMBEDDING": "qwen3",
                    "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
                    "PROJECT_NAME": "ProjectA",
                }, clear=False):
                    with patch.dict(
                        "sys.modules",
                        {"vco_lib.embedding_service": None},
                    ):
                        with patch.object(
                            srv, "_try_resolve_project_config",
                            return_value=None,
                        ):
                            w_old = srv._get_rl_telemetry_writer()
        self.assertEqual(w_old._embedding_source, "qwen3")

        # Second call after user flips ACTIVE_EMBEDDING → arctic2 at
        # runtime. The module constant ACTIVE_EMBEDDING is unchanged
        # (it's frozen at import); ONLY os.environ reflects the flip.
        # The factory must observe the env change and produce a NEW
        # writer with embedding_source="arctic2".
        with patch.object(srv, "ACTIVE_EMBEDDING", "qwen3"):  # still qwen3
            with patch.object(srv, "EMBEDDING_MODEL", "qwen3-embedding:0.6b"):
                with patch.dict(os.environ, {
                    "ACTIVE_EMBEDDING": "arctic2",  # flipped at runtime
                    "EMBEDDING_MODEL": "snowflake-arctic-embed2:latest",
                    "PROJECT_NAME": "ProjectA",
                }, clear=False):
                    with patch.dict(
                        "sys.modules",
                        {"vco_lib.embedding_service": None},
                    ):
                        with patch.object(
                            srv, "_try_resolve_project_config",
                            return_value=None,
                        ):
                            w_new = srv._get_rl_telemetry_writer()
        self.assertIsNot(w_old, w_new, "env flip must yield NEW writer")
        self.assertEqual(
            w_new._embedding_source, "arctic2",
            "new writer must reflect the CURRENT env, not the frozen one",
        )
        # Both writers coexist in the cache (one per tuple).
        self.assertEqual(len(srv._rl_telemetry_writers), 2)

    # ---- T4 -----------------------------------------------------------

    def test_t4_distinct_tuples_yield_distinct_writers(self):
        """Different (project, embedding) tuples each produce a
        distinct cached writer."""
        srv = self._srv

        def _build(project_name: str, emb: str, model: str):
            with patch.object(srv, "ACTIVE_EMBEDDING", emb):
                with patch.object(srv, "EMBEDDING_MODEL", model):
                    with patch.dict(os.environ, {
                        "ACTIVE_EMBEDDING": emb,
                        "EMBEDDING_MODEL": model,
                        "PROJECT_NAME": project_name,
                    }, clear=False):
                        with patch.dict(
                            "sys.modules",
                            {"vco_lib.embedding_service": None},
                        ):
                            with patch.object(
                                srv, "_try_resolve_project_config",
                                return_value=None,
                            ):
                                return srv._get_rl_telemetry_writer()

        w_a_qwen = _build("ProjectA", "qwen3", "qwen3-embedding:0.6b")
        w_a_arctic = _build("ProjectA", "arctic2", "snowflake-arctic-embed2:latest")
        w_b_qwen = _build("ProjectB", "qwen3", "qwen3-embedding:0.6b")
        w_b_qwen_again = _build("ProjectB", "qwen3", "qwen3-embedding:0.6b")

        # Three unique tuples cached.
        self.assertEqual(
            len(srv._rl_telemetry_writers), 3,
            "three unique (project, embedding) tuples → three writers",
        )
        # First three are pairwise distinct.
        self.assertIsNot(w_a_qwen, w_a_arctic)
        self.assertIsNot(w_a_qwen, w_b_qwen)
        self.assertIsNot(w_a_arctic, w_b_qwen)
        # Same tuple repeated → same instance.
        self.assertIs(w_b_qwen, w_b_qwen_again)
        # Tag correctness on each.
        self.assertEqual(w_a_qwen._embedding_source, "qwen3")
        self.assertEqual(w_a_arctic._embedding_source, "arctic2")
        self.assertEqual(w_b_qwen._embedding_source, "qwen3")

    # ---- T5 -----------------------------------------------------------

    def test_t5_reset_clears_all_cached_writers(self):
        """_reset_rl_telemetry_writers() drops all cached writers.

        RLTelemetryWriter has no persistent file handles or sockets
        (its underlying RLDataLogger opens+closes the JSONL via context
        manager on every write), so clearing the dict is sufficient
        teardown. The reset helper exists as a single canonical reset
        path for tests + future shutdown hooks.
        """
        srv = self._srv

        def _build(project_name: str, emb: str):
            with patch.object(srv, "ACTIVE_EMBEDDING", emb):
                with patch.object(srv, "EMBEDDING_MODEL", "qwen3-embedding:0.6b"):
                    with patch.dict(os.environ, {
                        "ACTIVE_EMBEDDING": emb,
                        "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
                        "PROJECT_NAME": project_name,
                    }, clear=False):
                        with patch.dict(
                            "sys.modules",
                            {"vco_lib.embedding_service": None},
                        ):
                            with patch.object(
                                srv, "_try_resolve_project_config",
                                return_value=None,
                            ):
                                return srv._get_rl_telemetry_writer()

        _build("ProjectA", "qwen3")
        _build("ProjectB", "qwen3")
        _build("ProjectA", "arctic2")
        self.assertEqual(len(srv._rl_telemetry_writers), 3)
        # Reset.
        srv._reset_rl_telemetry_writers()
        self.assertEqual(
            len(srv._rl_telemetry_writers), 0,
            "_reset_rl_telemetry_writers must drop all cached writers",
        )
        # And a fresh call after reset constructs a new writer (not a
        # stale reference to the old one).
        w_post = _build("ProjectA", "qwen3")
        self.assertEqual(len(srv._rl_telemetry_writers), 1)
        self.assertIsNotNone(w_post)


class TelemetryWriterForSlotV0271Test(unittest.TestCase):
    """v0.2.71 Sweep-C: ``_get_rl_telemetry_writer_for`` is the extracted
    construction body shared by the active path AND the dual-log other-slot
    writer. The active path is a thin wrapper that resolves the live triple
    then delegates; the other-slot writer is just a second cache entry."""

    def setUp(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        srv._reset_rl_telemetry_writers()
        self._srv = srv

    def tearDown(self):
        self._srv._reset_rl_telemetry_writers()

    def test_explicit_other_slot_writer_is_separate_cache_entry(self):
        """An explicit other-slot triple builds (and caches) a SECOND writer
        keyed on the other source — the dual-log fan-out's writer."""
        srv = self._srv
        with patch.dict(os.environ, {"PROJECT_NAME": "ProjectA"}, clear=False):
            with patch.object(srv, "_try_resolve_project_config", return_value=None):
                w_active = srv._get_rl_telemetry_writer_for(
                    "arctic", embedding_dim=1024,
                    embedding_model="snowflake-arctic-embed2",
                )
                w_other = srv._get_rl_telemetry_writer_for(
                    "qwen3", embedding_dim=1024,
                    embedding_model="qwen3-embedding:0.6b",
                )
        self.assertIsNot(w_active, w_other)
        self.assertEqual(w_active._embedding_source, "arctic")
        self.assertEqual(w_other._embedding_source, "qwen3")
        # Two distinct (project, source) cache entries.
        self.assertEqual(len(srv._rl_telemetry_writers), 2)

    def test_same_other_source_returns_same_instance(self):
        """Repeated lookups for the same source are idempotent (one writer)."""
        srv = self._srv
        with patch.dict(os.environ, {"PROJECT_NAME": "ProjectA"}, clear=False):
            with patch.object(srv, "_try_resolve_project_config", return_value=None):
                w1 = srv._get_rl_telemetry_writer_for(
                    "qwen3", embedding_dim=1024,
                    embedding_model="qwen3-embedding:0.6b",
                )
                w2 = srv._get_rl_telemetry_writer_for(
                    "qwen3", embedding_dim=1024,
                    embedding_model="qwen3-embedding:0.6b",
                )
        self.assertIs(w1, w2)
        self.assertEqual(len(srv._rl_telemetry_writers), 1)


class DualRLLogGateV0271Test(unittest.TestCase):
    """v0.2.71 Sweep-C: the dual-log gate is the AND of the dual-log env flag
    and the dual-WRITE gate (the hard precondition — the other slot's vectors
    only exist when dual-write populated them)."""

    def test_off_by_default(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(srv._resolve_dual_rl_log_enabled())

    def test_dual_log_on_but_write_off_is_forced_off(self):
        """The single most important guard: dual-log requested but dual-write
        OFF → forced off (no second-slot vectors to log)."""
        import claude_mcp_servers.weaviate_mcp.server as srv
        with patch.dict(os.environ, {
            "DUAL_RL_LOG_ENABLED": "true",
            # DUAL_EMBEDDING_WRITE_ALL_SLOTS absent → write gate off
        }, clear=True):
            self.assertFalse(srv._resolve_dual_rl_log_enabled())

    def test_both_on_enables(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        with patch.dict(os.environ, {
            "DUAL_RL_LOG_ENABLED": "1",
            "DUAL_EMBEDDING_WRITE_ALL_SLOTS": "1",
        }, clear=True):
            self.assertTrue(srv._resolve_dual_rl_log_enabled())

    def test_slot_short_source_mapping(self):
        import claude_mcp_servers.weaviate_mcp.server as srv
        self.assertEqual(srv._slot_short_source("qwen3_embed"), "qwen3")
        self.assertEqual(srv._slot_short_source("arctic2_embed"), "arctic")
        self.assertEqual(srv._slot_short_source("openai_text_embed"), "openai")
        self.assertEqual(srv._slot_short_source("codesage_embed"), "codesage")
        self.assertEqual(srv._slot_short_source("ollama_embed"), "legacy")


if __name__ == "__main__":
    unittest.main()
