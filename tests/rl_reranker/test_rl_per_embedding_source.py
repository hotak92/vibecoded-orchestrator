# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Tests for the per-embedding-source RL pipeline (added 2026-05-05):

  - rl_server._resolve_model_tag_and_dim  (ACTIVE_EMBEDDING → tag, dim)
  - rl_logger.RLDataLogger v2 schema      (embedding_source on every event)
  - migrate_rl_log_v1_to_v2                (legacy JSONL → v2, for porting to DB)

NOTE (v0.2.11): the per-source-filter / v1-refusal tests that used
``offline_trainer.load_events`` were removed when training went DB-only — that
filtering logic now lives in ``hub_event_loader.load_events_from_hub`` and is
covered by ``tests/test_db_hub_event_loader_parity.py``. The migration-script
tests stay: that script is how archived JSONL gets PORTED into launcher.db.

Why this lives in one file: all five components reference the same
embedding_source vocabulary (qwen3, arctic, openai, codesage, jina). A
single test module makes contract drift between them obvious.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


from rl_reranker.rl_server import _resolve_model_tag_and_dim, _EMBEDDING_SOURCE_TO_DIM
from rl_client.rl_logger import RLDataLogger

# Import the migration script as a module
from rl_reranker import migrate_rl_log_v1_to_v2 as migrator


# ============================================================================
# Phase E — RLServer tag/dim resolution
# ============================================================================

class TestResolveModelTagAndDim:
    """v0.2.8 refactor: tag is the SOURCE STRING only (dim + arch live
    inside the .pt's pickled dict per the v0.2.7 R4 cross-source guard).
    The filename pattern is rl_model_<source>_<version>.pt — the version
    segment is read off the loaded checkpoint, not embedded in the tag."""

    def test_qwen3_resolves_to_qwen3(self):
        with patch.dict(os.environ, {"ACTIVE_EMBEDDING": "qwen3"}, clear=False):
            os.environ.pop("RL_MODEL_TAG", None)
            os.environ.pop("RL_EMB_DIM", None)
            tag, dim = _resolve_model_tag_and_dim()
            assert tag == "qwen3"
            assert dim == 1024

    def test_arctic_resolves_to_arctic(self):
        with patch.dict(os.environ, {"ACTIVE_EMBEDDING": "arctic"}, clear=False):
            os.environ.pop("RL_MODEL_TAG", None)
            os.environ.pop("RL_EMB_DIM", None)
            tag, dim = _resolve_model_tag_and_dim()
            assert tag == "arctic"
            assert dim == 1024

    def test_openai_resolves_to_openai_1536(self):
        with patch.dict(os.environ, {"ACTIVE_EMBEDDING": "openai"}, clear=False):
            os.environ.pop("RL_MODEL_TAG", None)
            os.environ.pop("RL_EMB_DIM", None)
            tag, dim = _resolve_model_tag_and_dim()
            assert tag == "openai"
            assert dim == 1536

    def test_codesage_is_rejected_at_runtime(self):
        """v0.1.2: codesage REMOVED from the active text-embedding table.
        The KG RL reranker is KG-only (text embeddings). Code-graph
        reranking is not part of this package.
        Legacy log entries with embedding_source=codesage are still
        parseable by the migration script (test_2048_dim_inferred_as_codesage
        below) — only the RUNTIME resolution rejects it."""
        with patch.dict(os.environ, {"ACTIVE_EMBEDDING": "codesage"}, clear=False):
            os.environ.pop("RL_MODEL_TAG", None)
            os.environ.pop("RL_EMB_DIM", None)
            try:
                _resolve_model_tag_and_dim()
                assert False, "expected ValueError for codesage"
            except ValueError as exc:
                assert "Unknown ACTIVE_EMBEDDING" in str(exc)
                assert "codesage" in str(exc)

    def test_unset_active_embedding_falls_back_to_legacy(self):
        env = {k: v for k, v in os.environ.items() if k not in (
            "ACTIVE_EMBEDDING", "RL_MODEL_TAG", "RL_EMB_DIM"
        )}
        with patch.dict(os.environ, env, clear=True):
            tag, dim = _resolve_model_tag_and_dim()
            assert tag == "legacy"
            assert dim == 1024

    def test_explicit_env_overrides_active_embedding(self):
        """When both RL_MODEL_TAG and RL_EMB_DIM are set, ACTIVE_EMBEDDING is
        ignored. This supports retraining flows on re-embedded data where the
        active vector and the target network differ."""
        with patch.dict(os.environ, {
            "ACTIVE_EMBEDDING": "qwen3",
            "RL_MODEL_TAG": "experimental_v3",
            "RL_EMB_DIM": "768",
        }, clear=False):
            tag, dim = _resolve_model_tag_and_dim()
            assert tag == "experimental_v3"
            assert dim == 768

    def test_explicit_partial_env_does_NOT_override(self):
        """If only ONE of RL_MODEL_TAG/RL_EMB_DIM is set, do NOT use them —
        the override path requires both. This avoids silent dim mismatches."""
        env = {k: v for k, v in os.environ.items() if k not in (
            "ACTIVE_EMBEDDING", "RL_MODEL_TAG", "RL_EMB_DIM"
        )}
        env["ACTIVE_EMBEDDING"] = "qwen3"
        env["RL_MODEL_TAG"] = "experimental_v3"  # missing RL_EMB_DIM
        with patch.dict(os.environ, env, clear=True):
            tag, dim = _resolve_model_tag_and_dim()
            assert tag == "qwen3"  # falls back to ACTIVE_EMBEDDING path

    def test_unknown_active_embedding_raises(self):
        with patch.dict(os.environ, {"ACTIVE_EMBEDDING": "bogus"}, clear=False):
            os.environ.pop("RL_MODEL_TAG", None)
            os.environ.pop("RL_EMB_DIM", None)
            try:
                _resolve_model_tag_and_dim()
                assert False, "expected ValueError"
            except ValueError as exc:
                assert "Unknown ACTIVE_EMBEDDING" in str(exc)
                assert "bogus" in str(exc)

    def test_known_sources_table_has_expected_entries(self):
        """Contract test — the source vocabulary must include the three
        active KG text-embedding models (qwen3 / arctic / openai).
        codesage REMOVED 2026-05-19 (code-graph rerank is a future
        separate module). Open to future text additions (e.g. jina)."""
        for required in ("qwen3", "arctic", "openai"):
            assert required in _EMBEDDING_SOURCE_TO_DIM
        # Negative assertion: codesage must NOT be a runtime-resolvable source.
        assert "codesage" not in _EMBEDDING_SOURCE_TO_DIM

    def test_each_source_has_a_dim(self):
        """Every source in the table must map to a positive integer dim.
        The tag (source) IS the key; uniqueness is guaranteed by dict
        semantics. Filename collisions are handled by the v0.2.8
        rl_model_<source>_<version>.pt convention (version disambiguates
        for the same source)."""
        for source, dim in _EMBEDDING_SOURCE_TO_DIM.items():
            assert isinstance(dim, int) and dim > 0, f"bad dim for {source}: {dim}"


# ============================================================================
# Phase D — RLDataLogger v2 schema
# ============================================================================

class TestRLDataLoggerV2Schema:
    def test_log_retrieval_includes_v2_fields(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "ev.jsonl"
            logger = RLDataLogger(
                log_path=log,
                project="TestProject",
                embedding_source="qwen3",
                embedding_dim=1024,
                embedding_model="qwen3-embedding:0.6b",
            )
            logger.log_retrieval(
                task_id="t1",
                task_type="implementation",
                query="hello",
                nodes=[{"title": "A", "score": 0.5, "tier": "top_k"}],
            )
            line = log.read_text().strip()
            event = json.loads(line)
            assert event["schema_version"] == RLDataLogger.SCHEMA_VERSION == 4
            assert event["embedding_source"] == "qwen3"
            assert event["embedding_dim"] == 1024
            assert event["embedding_model"] == "qwen3-embedding:0.6b"

    def test_log_citations_includes_schema_version(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "ev.jsonl"
            logger = RLDataLogger(
                log_path=log,
                project="TestProject",
                embedding_source="qwen3",
                embedding_dim=1024,
                embedding_model="qwen3-embedding:0.6b",
            )
            logger.log_citations(
                task_id="t1",
                task_type="implementation",
                citations={"A": True, "B": False},
            )
            line = log.read_text().strip()
            event = json.loads(line)
            assert event["schema_version"] == RLDataLogger.SCHEMA_VERSION == 4
            assert event["citations"] == {"A": True, "B": False}

    def test_logger_constructed_without_embedding_metadata_writes_empty_strings(self):
        """Standalone tests / scripts may construct a logger without
        passing embedding metadata. The fields are still written (empty)
        so the schema is consistent."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "ev.jsonl"
            logger = RLDataLogger(log_path=log, project="X")
            logger.log_retrieval(
                task_id="t1",
                task_type="x",
                query="q",
                nodes=[],
            )
            event = json.loads(log.read_text().strip())
            assert event["schema_version"] == RLDataLogger.SCHEMA_VERSION == 4
            assert event["embedding_source"] == ""
            assert event["embedding_dim"] == 0


# ============================================================================
# Phase C — migration script (legacy JSONL → v2, for porting archived corpora
# into launcher.db; NOT part of the training read path)
# ============================================================================

class TestMigrationScript:
    @staticmethod
    def _write_v1_jsonl(path: Path, events: list[dict]) -> None:
        with path.open("w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_1024_dim_post_cutover_inferred_as_qwen3(self):
        ev = {"event": "retrieval", "ts": "2026-04-15T00:00:00+00:00",
              "query_emb": [0.0] * 1024, "task_id": "t1"}
        source, dim, model = migrator._infer_source_for_event(ev)
        assert source == "qwen3"
        assert dim == 1024
        assert "qwen3" in model

    def test_1024_dim_pre_cutover_inferred_as_arctic(self):
        ev = {"event": "retrieval", "ts": "2026-03-15T00:00:00+00:00",
              "query_emb": [0.0] * 1024, "task_id": "t1"}
        source, dim, model = migrator._infer_source_for_event(ev)
        assert source == "arctic"
        assert dim == 1024
        assert "arctic" in model

    def test_768_dim_inferred_as_jina(self):
        ev = {"event": "retrieval", "ts": "2026-03-15T00:00:00+00:00",
              "query_emb": [0.0] * 768, "task_id": "t1"}
        source, dim, _ = migrator._infer_source_for_event(ev)
        assert source == "jina"
        assert dim == 768

    def test_1536_dim_inferred_as_openai(self):
        ev = {"event": "retrieval", "ts": "2026-04-20T00:00:00+00:00",
              "query_emb": [0.0] * 1536, "task_id": "t1"}
        source, dim, _ = migrator._infer_source_for_event(ev)
        assert source == "openai"
        assert dim == 1536

    def test_2048_dim_inferred_as_codesage(self):
        ev = {"event": "retrieval", "ts": "2026-04-20T00:00:00+00:00",
              "query_emb": [0.0] * 2048, "task_id": "t1"}
        source, dim, _ = migrator._infer_source_for_event(ev)
        assert source == "codesage"
        assert dim == 2048

    def test_no_query_emb_inferred_as_unknown(self):
        ev = {"event": "retrieval", "ts": "2026-04-20T00:00:00+00:00",
              "task_id": "t1"}
        source, dim, _ = migrator._infer_source_for_event(ev)
        assert source == "unknown"
        assert dim == 0

    def test_full_migration_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            in_path = Path(td) / "v1.jsonl"
            out_path = Path(td) / "v2.jsonl"
            events = [
                {"event": "retrieval", "ts": "2026-04-15T00:00:00+00:00",
                 "task_id": "qwen", "query": "q",
                 "query_emb": [0.0] * 1024, "nodes": []},
                {"event": "retrieval", "ts": "2026-03-15T00:00:00+00:00",
                 "task_id": "arctic", "query": "q",
                 "query_emb": [0.0] * 1024, "nodes": []},
                {"event": "citation", "ts": "2026-04-15T00:01:00+00:00",
                 "task_id": "qwen", "task_type": "x",
                 "citations": {"A": True}},
            ]
            self._write_v1_jsonl(in_path, events)

            stats = migrator.migrate_file(in_path, out_path, dry_run=False)
            assert stats["retrieval_qwen3"] == 1
            assert stats["retrieval_arctic"] == 1
            assert stats["citation_matched"] == 1
            assert stats["already_v2"] == 0

            # Read output and verify v2 fields
            lines = out_path.read_text().strip().split("\n")
            out_events = [json.loads(line) for line in lines]
            qwen_ev = [e for e in out_events if e.get("task_id") == "qwen" and e["event"] == "retrieval"][0]
            assert qwen_ev["schema_version"] == 2
            assert qwen_ev["embedding_source"] == "qwen3"
            assert qwen_ev["embedding_dim"] == 1024
            arctic_ev = [e for e in out_events if e.get("task_id") == "arctic"][0]
            assert arctic_ev["embedding_source"] == "arctic"
            citation_ev = [e for e in out_events if e["event"] == "citation"][0]
            assert citation_ev["schema_version"] == 2
            assert citation_ev["embedding_source"] == "qwen3"  # joined by task_id

    def test_migration_is_idempotent(self):
        """Running migration twice must not corrupt v2 events."""
        with tempfile.TemporaryDirectory() as td:
            in_path = Path(td) / "ev.jsonl"
            self._write_v1_jsonl(in_path, [
                {"event": "retrieval", "ts": "2026-04-15T00:00:00+00:00",
                 "task_id": "t1", "query": "q",
                 "query_emb": [0.0] * 1024, "nodes": []},
            ])

            # First migration v1 → v2
            out1 = Path(td) / "out1.jsonl"
            stats1 = migrator.migrate_file(in_path, out1, dry_run=False)
            assert stats1["retrieval_qwen3"] == 1

            # Second migration v2 → v2 (should classify as already_v2)
            out2 = Path(td) / "out2.jsonl"
            stats2 = migrator.migrate_file(out1, out2, dry_run=False)
            assert stats2["already_v2"] == 1
            assert stats2["retrieval_qwen3"] == 0  # NOT re-classified

            # Output of second migration should equal output of first
            assert out1.read_text() == out2.read_text()

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            in_path = Path(td) / "v1.jsonl"
            self._write_v1_jsonl(in_path, [
                {"event": "retrieval", "ts": "2026-04-15T00:00:00+00:00",
                 "task_id": "t1", "query": "q",
                 "query_emb": [0.0] * 1024, "nodes": []},
            ])
            out_path = Path(td) / "should_not_exist.jsonl"
            stats = migrator.migrate_file(in_path, out_path, dry_run=True)
            assert stats["retrieval_qwen3"] == 1
            assert not out_path.exists()
