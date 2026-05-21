# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for RL-defect-2026-05-22.

Symptom: when ANY collection in the hybrid_search / semantic_graph_search
fan-out schema-failed (e.g. a hardcoded shared-KG default that doesn't
exist on the user's Weaviate), the WHOLE search bubbled a
WeaviateSchemaError and _rl_cache_and_rerank — the ONLY caller of
log_retrieval — was never reached. Every retrieval event was lost.

Fix (v0.2.24):
  1. Per-collection schema errors are now skipped (recorded but not
     bubbled). The fan-out continues with the remaining collections.
  2. When EVERY collection schema-fails (instance-level problem), a
     degraded-mode telemetry event is logged BEFORE the exception
     bubbles so offline training has visibility into the failure.
  3. _rl_cache_and_rerank now ALWAYS calls log_retrieval, even on the
     free-tier early-return path and even with empty all_nodes.

This file pins those contracts via direct unit-test fixtures (no live
Weaviate) so a future refactor that silently drops the log_retrieval
call fails CI immediately.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402
from claude_mcp_servers.rl_client.rl_logger import RLDataLogger  # noqa: E402
from claude_mcp_servers.rl_client.telemetry_writer import (  # noqa: E402
    RLTelemetryWriter,
)


def _run(coro):
    """Helper — most tests are sync but a few await async helpers."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TelemetryWriterFailureFieldsTest(unittest.TestCase):
    """RLTelemetryWriter + RLDataLogger must accept + persist
    failure_mode + failed_collections fields."""

    def test_logger_persists_failure_mode_in_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "rl_events.jsonl"
            logger = RLDataLogger(
                log_path=log_path,
                project="testproj",
                embedding_source="qwen3",
                embedding_dim=1024,
                embedding_model="qwen3-embedding:0.6b",
            )
            logger.log_retrieval(
                task_id="task-failure-1",
                task_type="mcp_interactive",
                query="any query",
                nodes=[],
                session_id="sess-1",
                failure_mode="all_collections_schema_missing",
                failed_collections=[
                    "VibeCodedOrchestrator_KnowledgeGraph",
                    "NonexistentPeer_KnowledgeGraph",
                ],
            )

            self.assertTrue(log_path.exists())
            lines = log_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["failure_mode"], "all_collections_schema_missing")
            self.assertEqual(
                rec["failed_collections"],
                [
                    "VibeCodedOrchestrator_KnowledgeGraph",
                    "NonexistentPeer_KnowledgeGraph",
                ],
            )
            self.assertEqual(rec["nodes"], [])
            self.assertEqual(rec["task_id"], "task-failure-1")

    def test_logger_omits_failure_fields_when_not_set(self):
        """Default invocations (None) must NOT add the new fields —
        existing offline-training pipelines that filter on field
        presence keep their semantics."""
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "rl_events.jsonl"
            logger = RLDataLogger(
                log_path=log_path,
                project="testproj",
                embedding_source="qwen3",
                embedding_dim=1024,
                embedding_model="qwen3-embedding:0.6b",
            )
            logger.log_retrieval(
                task_id="task-ok-1",
                task_type="mcp_interactive",
                query="any query",
                nodes=[{"title": "T", "score": 0.5, "tier": "top_k"}],
                session_id="sess-1",
            )
            rec = json.loads(log_path.read_text().strip())
            self.assertNotIn("failure_mode", rec)
            self.assertNotIn("failed_collections", rec)

    def test_writer_passes_failure_fields_through(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "rl_events.jsonl"
            writer = RLTelemetryWriter(
                log_path=log_path,
                project="testproj",
                embedding_source="qwen3",
                embedding_dim=1024,
                embedding_model="qwen3-embedding:0.6b",
            )
            writer.log_retrieval(
                task_id="task-failure-via-writer",
                task_type="mcp_interactive",
                query="any query",
                nodes=[],
                session_id="sess-2",
                failure_mode="partial_fan_out_schema_missing",
                failed_collections=["A_KG"],
            )
            rec = json.loads(log_path.read_text().strip())
            self.assertEqual(rec["failure_mode"], "partial_fan_out_schema_missing")
            self.assertEqual(rec["failed_collections"], ["A_KG"])


class RlCacheAndRerankAlwaysLogsTest(unittest.TestCase):
    """_rl_cache_and_rerank must ALWAYS call writer.log_retrieval, including
    on the free-tier early-return, with empty nodes, and with failure_mode."""

    def _make_capturing_writer(self):
        """Return (writer_stub, captured_calls list). The stub mimics
        RLTelemetryWriter.log_retrieval's kwargs surface but only
        records the call args for assertion."""
        captured: list[dict] = []

        class _Stub:
            def log_retrieval(self, **kwargs):
                captured.append(dict(kwargs))

        return _Stub(), captured

    def test_free_tier_empty_nodes_with_failure_mode_still_logs(self):
        """Free tier + empty all_nodes + failure_mode set → log lands."""
        writer_stub, captured = self._make_capturing_writer()
        with patch.object(srv, "_get_rl_telemetry_writer", return_value=writer_stub):
            with patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
                # ImportError path: VCThelpers unavailable → free tier
                result = _run(srv._rl_cache_and_rerank(
                    "task-fail-empty",
                    "any query",
                    [],
                    10,
                    failure_mode="all_collections_schema_missing",
                    failed_collections=["X_KG", "Y_KG"],
                ))
        self.assertEqual(result, [])
        self.assertEqual(len(captured), 1)
        call = captured[0]
        self.assertEqual(call["task_id"], "task-fail-empty")
        self.assertEqual(call["nodes"], [])
        self.assertEqual(call["failure_mode"], "all_collections_schema_missing")
        self.assertEqual(call["failed_collections"], ["X_KG", "Y_KG"])

    def test_free_tier_with_nodes_still_logs_without_failure_mode(self):
        """Free tier + some nodes + no failure_mode → standard log."""
        writer_stub, captured = self._make_capturing_writer()
        nodes = [
            {"title": "N1", "score": 0.7, "tier": "top_k"},
            {"title": "N2", "score": 0.5, "tier": "extra_reference"},
        ]
        with patch.object(srv, "_get_rl_telemetry_writer", return_value=writer_stub):
            with patch.dict("sys.modules", {"VCThelpers": None, "VCThelpers.license": None}):
                result = _run(srv._rl_cache_and_rerank(
                    "task-ok-nodes",
                    "any query",
                    nodes,
                    1,
                ))
        # Free tier returns Weaviate order, top-`limit`.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "N1")
        # Telemetry includes ALL candidates (top_k + extra_reference).
        self.assertEqual(len(captured), 1)
        call = captured[0]
        self.assertEqual(call["nodes"][0]["title"], "N1")
        self.assertEqual(call["nodes"][0]["tier"], "top_k")
        self.assertEqual(call["nodes"][1]["title"], "N2")
        self.assertEqual(call["nodes"][1]["tier"], "extra_reference")
        # No failure_mode passed → not in kwargs OR explicitly None.
        self.assertIsNone(call.get("failure_mode"))


if __name__ == "__main__":
    unittest.main()
