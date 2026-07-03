# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for v0.2.40 F3 — citation events carry the embedding triple.

Pre-fix: ``RLDataLogger.log_citations`` and
``RLTelemetryWriter._build_citation_payload`` wrote citation events
WITHOUT the ``(embedding_source, embedding_dim, embedding_model)``
triple that retrieval events have always carried. The offline RL
training reader (historically the JSONL ``training_loader``, retired
v0.2.73 RL-8; today the DB-only path — launcher.db ``rl_events`` rows
served by the hub and consumed by the container's ``offline_trainer``)
filters/partitions citation events by the same embedding-triple keys
as retrieval events — so citation events lacking the triple were
silently dropped on every load.

If a retrieval event was successfully written but its paired citation
event got dropped by the triple filter, the offline trainer couldn't
pair the cited-or-not signal with the retrieved candidates — the
citation orphaned silently.

Post-fix: both write paths now stamp the full triple on the citation
event. Mirror of the retrieval-event shape; field names match exactly
(``embedding_source`` / ``embedding_dim`` / ``embedding_model``).

Note: named ``test_telemetry_citation_embedding_triple.py`` (not
``test_rl_*``) to avoid the gitignore pattern ``tests/test_rl_*.py``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude_mcp_servers.rl_client.rl_logger import RLDataLogger  # noqa: E402
from claude_mcp_servers.rl_client.telemetry_writer import (  # noqa: E402
    RLTelemetryWriter,
)


class CitationEventEmbeddingTripleLocalJsonlTest(unittest.TestCase):
    """T1: citation events from RLDataLogger.log_citations include the triple."""

    def test_citation_event_carries_embedding_triple(self):
        """Citation event must serialize embedding_source +
        embedding_dim + embedding_model — same names as retrieval."""
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "rl_events.jsonl"
            logger = RLDataLogger(
                log_path=log_path,
                project="testproj",
                embedding_source="qwen3",
                embedding_dim=1024,
                embedding_model="qwen3-embedding:0.6b",
            )
            logger.log_citations(
                task_id="task-cite-1",
                task_type="mcp_interactive",
                citations={"NodeA": True, "NodeB": False, "NodeC": None},
            )

            self.assertTrue(log_path.exists())
            lines = log_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])

            # The fix: triple is present on the citation event.
            self.assertEqual(rec["embedding_source"], "qwen3")
            self.assertEqual(rec["embedding_dim"], 1024)
            self.assertEqual(rec["embedding_model"], "qwen3-embedding:0.6b")

            # Sanity: the rest of the citation event is unchanged.
            self.assertEqual(rec["event"], "citation")
            self.assertEqual(rec["task_id"], "task-cite-1")
            self.assertEqual(rec["task_type"], "mcp_interactive")
            self.assertEqual(rec["project"], "testproj")
            self.assertEqual(rec["schema_version"], RLDataLogger.SCHEMA_VERSION)
            self.assertEqual(
                rec["citations"], {"NodeA": True, "NodeB": False, "NodeC": None},
            )

    def test_citation_event_defaults_to_blank_triple_when_unset(self):
        """When the logger is constructed with no embedding metadata
        (test / standalone usage), the triple still serializes as the
        empty defaults — keys are PRESENT (so an offline reader's
        triple filter can identify the legacy / blank state explicitly)
        rather than missing entirely."""
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "rl_events.jsonl"
            # No embedding_* kwargs → defaults to "", 0, "".
            logger = RLDataLogger(log_path=log_path, project="testproj")
            logger.log_citations(
                task_id="task-cite-blank",
                task_type="mcp_interactive",
                citations={"X": True},
            )
            rec = json.loads(log_path.read_text().strip())

            # Keys are present (forward-compat: an offline reader's
            # triple filter sees the field exists but with the legacy /
            # empty value and drops the event explicitly, not silently).
            self.assertIn("embedding_source", rec)
            self.assertIn("embedding_dim", rec)
            self.assertIn("embedding_model", rec)
            self.assertEqual(rec["embedding_source"], "")
            self.assertEqual(rec["embedding_dim"], 0)
            self.assertEqual(rec["embedding_model"], "")


class CitationEventEmbeddingTripleTelemetryWriterTest(unittest.TestCase):
    """T2: citation events from RLTelemetryWriter._build_citation_payload
    include the triple."""

    def test_writer_local_jsonl_carries_full_triple(self):
        """v0.2.47 RL-6c: the local write target moved from JSONL to the
        hub. The full embedding triple still has to be present on the
        envelope's indexed columns AND embedded in the payload_json
        (same v3 event shape, different transport)."""
        captured: list[dict] = []
        writer = RLTelemetryWriter(
            project="testproj",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=lambda env, timeout=2.0: captured.append(env) or True,
        )
        writer.log_citations(
            task_id="task-cite-via-writer",
            task_type="mcp_interactive",
            citations={"NodeA": True},
            cosine_sims={"NodeA": 0.812},
        )
        self.assertEqual(len(captured), 1)
        envelope = captured[0]
        # Indexed columns:
        self.assertEqual(envelope["embedding_source"], "qwen3")
        self.assertEqual(envelope["embedding_dim"], 1024)
        self.assertEqual(envelope["embedding_model"], "qwen3-embedding:0.6b")
        # And mirrored inside the payload_json (the v3 event):
        rec = json.loads(envelope["payload_json"])
        self.assertEqual(rec["embedding_source"], "qwen3")
        self.assertEqual(rec["embedding_dim"], 1024)
        self.assertEqual(rec["embedding_model"], "qwen3-embedding:0.6b")

    def test_writer_upload_payload_carries_full_triple(self):
        """The queue-bound payload built by
        ``RLTelemetryWriter._build_citation_payload`` must also include
        the full triple. Pre-F3 it had only ``embedding_source`` —
        the hub-side training pipeline therefore couldn't filter by
        embedding_dim / embedding_model on uploaded citation events."""
        writer = RLTelemetryWriter(
            project="testproj",
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
        )
        payload = writer._build_citation_payload(
            task_id="task-cite-upload",
            task_type="mcp_interactive",
            citations={"NodeA": True, "NodeB": False},
            cosine_sims=None,
        )

        self.assertEqual(payload["embedding_source"], "qwen3")
        self.assertEqual(payload["embedding_dim"], 1024)
        self.assertEqual(payload["embedding_model"], "qwen3-embedding:0.6b")

        # Sanity: the rest of the upload payload is unchanged.
        self.assertEqual(payload["schema_version"], RLDataLogger.SCHEMA_VERSION)
        self.assertEqual(payload["project"], "testproj")
        self.assertEqual(payload["task_id"], "task-cite-upload")
        self.assertEqual(payload["task_type"], "mcp_interactive")
        self.assertEqual(
            payload["citations"], {"NodeA": True, "NodeB": False},
        )


class CitationEventRetrievalParityTest(unittest.TestCase):
    """T3 + T4: citation events use the SAME field names as retrieval
    events (regression guard) and the SAME default semantics."""

    def test_retrieval_and_citation_use_same_field_names(self):
        """Field-name regression guard: the offline trainer pairs
        retrieval ↔ citation by the triple, so the keys MUST be
        identical. If a refactor renames one side, this test catches
        it before training silently breaks."""
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
                task_id="task-parity-1",
                task_type="mcp_interactive",
                query="any query",
                nodes=[{"title": "N1", "score": 0.5, "tier": "top_k"}],
            )
            logger.log_citations(
                task_id="task-parity-1",
                task_type="mcp_interactive",
                citations={"N1": True},
            )

            lines = log_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            retrieval_rec = json.loads(lines[0])
            citation_rec = json.loads(lines[1])

            self.assertEqual(retrieval_rec["event"], "retrieval")
            self.assertEqual(citation_rec["event"], "citation")

            # The triple keys + values must match exactly so the offline
            # training pipeline can pair the two events by triple.
            for field in ("embedding_source", "embedding_dim", "embedding_model"):
                self.assertEqual(
                    retrieval_rec[field],
                    citation_rec[field],
                    f"retrieval/citation parity broken on field {field!r}: "
                    f"retrieval={retrieval_rec[field]!r} "
                    f"citation={citation_rec[field]!r}",
                )
            # And project + task_id + task_type — the other pairing keys.
            self.assertEqual(retrieval_rec["project"], citation_rec["project"])
            self.assertEqual(retrieval_rec["task_id"], citation_rec["task_id"])
            self.assertEqual(retrieval_rec["task_type"], citation_rec["task_type"])

    def test_default_embedding_matches_canonical_qwen3(self):
        """When the logger / writer is constructed via the canonical
        v0.2.40+ orchestrator-side path (``_get_rl_telemetry_writer``
        in weaviate_mcp.server), the embedding_source defaults to
        ``"qwen3"``. Verified by the sibling test in
        test_telemetry_orchestrator_v0231.py
        (RLTelemetryWriterEmbeddingFieldsTest); this test pins the
        value flows through the citation event as well.

        v0.2.47 RL-6c: capture via hub_post_fn stub (JSONL gone)."""
        captured: list[dict] = []
        writer = RLTelemetryWriter(
            project="testproj",
            # Canonical defaults the orchestrator picks for qwen3.
            embedding_source="qwen3",
            embedding_dim=1024,
            embedding_model="qwen3-embedding:0.6b",
            hub_post_fn=lambda env, timeout=2.0: captured.append(env) or True,
        )
        writer.log_citations(
            task_id="task-default",
            task_type="mcp_interactive",
            citations={"N1": True},
        )
        self.assertEqual(len(captured), 1)
        rec = json.loads(captured[0]["payload_json"])
        self.assertEqual(rec["embedding_source"], "qwen3")
        self.assertEqual(rec["embedding_dim"], 1024)
        self.assertEqual(rec["embedding_model"], "qwen3-embedding:0.6b")


if __name__ == "__main__":
    unittest.main()
