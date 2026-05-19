# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.weaviate_schema (v0.2.18 Commit 4).

Coverage:
  * Slot catalog invariants (KG_NAMED_VECTORS, CODE_NAMED_VECTORS).
  * `diff_collection_vs_target` — missing-slot detection via fake schema fetcher.
  * `add_named_vector_slot` — Created / Skipped / DimMismatch branches.
  * `migrate_collection_to_target` — idempotency, multi-slot batch add,
    error propagation.
  * `is_code_collection` — naming heuristic for code-graph classes.
  * `enumerate_kg_collections` / `enumerate_code_collections` — both
    per-project and server-wide discovery paths via fake list_all.
  * Live integration (auto-skipped when Weaviate is unreachable):
    creates a throwaway collection, adds slots additively, verifies
    data preservation across the schema rebuild + idempotent re-run.
  * v0.2.17 backward-compat: a 3-slot collection populated with data
    survives a v0.2.18 migration to 5 slots — populated slots round-
    trip, new slots arrive empty.

The mocked-client tests don't need Weaviate to be running. The
`LiveSchemaMigrationTest` block self-skips if Weaviate isn't reachable
at the URL pointed to by WEAVIATE_URL (or http://localhost:8081 by
default).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import weaviate_schema as ws  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_schema(slot_names: list[str], class_name: str = "TestKG") -> dict:
    """Build a fake `GET /v1/schema/<class>` response dict carrying just
    `vectorConfig` (sufficient for diff + slot-add tests)."""
    return {
        "class": class_name,
        "vectorConfig": {
            n: {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"}
            for n in slot_names
        },
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
        ],
    }


class _FakeFetcher:
    """Callable that returns a fixed schema dict by collection name.

    Set `responses[name] = dict` for known classes, None for absent.
    """
    def __init__(self, responses: Optional[dict] = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def __call__(self, name: str) -> Optional[dict]:
        self.calls.append(name)
        return self.responses.get(name)


class _FakeRebuilder:
    """Callable matching the `collection_rebuilder` signature.

    Records every invocation so tests can assert the new target slot
    list passed in. Updates the linked `_FakeFetcher`'s response so a
    subsequent `add_named_vector_slot` call sees the new schema (the
    actual rebuilder mutates server state; the fake mutates the fetcher
    cache).
    """
    def __init__(self, fetcher: _FakeFetcher, objects_copied: int = 0):
        self.fetcher = fetcher
        self.objects_copied = objects_copied
        self.calls: list[tuple] = []

    def __call__(
        self,
        collection: str,
        target_slots: list[ws.NamedVectorSlot],
        *,
        weaviate_url: Optional[str] = None,
    ) -> int:
        self.calls.append((collection, [s.name for s in target_slots]))
        # Mutate the fetcher's view so subsequent diff sees the new slot set.
        new_schema = _fake_schema(
            [s.name for s in target_slots], class_name=collection,
        )
        self.fetcher.responses[collection] = new_schema
        return self.objects_copied


# ---------------------------------------------------------------------------
# Slot catalog invariants
# ---------------------------------------------------------------------------


class SlotCatalogTests(unittest.TestCase):
    """The catalog has a precise shape — these tests freeze it."""

    def test_kg_named_vectors_includes_v0218_additions(self):
        names = {s.name for s in ws.KG_NAMED_VECTORS}
        # Legacy v0.2.17 trio kept for data preservation.
        self.assertIn("qwen3_embed", names)
        self.assertIn("ollama_embed", names)
        self.assertIn("openai_embed", names)
        # v0.2.18 additions.
        self.assertIn("arctic2_embed", names)
        self.assertIn("openai_text_embed", names)

    def test_code_named_vectors_includes_v0218_additions(self):
        names = {s.name for s in ws.CODE_NAMED_VECTORS}
        # Legacy slots kept.
        self.assertIn("codesage_embed", names)
        self.assertIn("ollama_code_embed", names)
        self.assertIn("openai_embed", names)
        # v0.2.18 additions.
        self.assertIn("qwen3_embed", names)
        self.assertIn("jina_embed", names)
        self.assertIn("openai_code_embed", names)

    def test_all_slots_use_vectorizer_none(self):
        for s in (*ws.KG_NAMED_VECTORS, *ws.CODE_NAMED_VECTORS):
            self.assertEqual(
                s.vectorizer, "none",
                msg=f"{s.name}: v0.2.18 expects vectorizer='none' (pre-"
                    f"computed embeddings from MCP / scripts)",
            )

    def test_slot_dims_match_documented_model_sizes(self):
        dims = {s.name: s.dim for s in (*ws.KG_NAMED_VECTORS, *ws.CODE_NAMED_VECTORS)}
        self.assertEqual(dims["qwen3_embed"], 1024)
        self.assertEqual(dims["arctic2_embed"], 1024)
        self.assertEqual(dims["openai_text_embed"], 1536)
        self.assertEqual(dims["codesage_embed"], 2048)
        self.assertEqual(dims["jina_embed"], 768)
        self.assertEqual(dims["openai_code_embed"], 1536)

    def test_named_vector_slot_serializes_to_weaviate_config(self):
        slot = ws.NamedVectorSlot("test_embed", 512)
        out = slot.to_weaviate_config()
        self.assertEqual(out["vectorizer"], {"none": {}})
        self.assertEqual(out["vectorIndexType"], "hnsw")
        # Caller is expected to compose {slot.name: out} themselves.
        self.assertNotIn("name", out)


class IsCodeCollectionTests(unittest.TestCase):
    def test_canonical_bare_names_match(self):
        for n in ("CodeModule", "CodeClass", "CodeFunction", "CodeAPI",
                  "CodeInteraction"):
            self.assertTrue(ws.is_code_collection(n))

    def test_per_project_prefixes_match(self):
        for n in ("MyProject_CodeFunction", "SD15_CodeAPI",
                  "Vibecoded_orchestrator_CodeInteraction"):
            self.assertTrue(ws.is_code_collection(n))

    def test_non_code_class_names_do_not_match(self):
        for n in ("MyProject_KnowledgeGraph", "MyProject_Development",
                  "CodeBase",  # substring but doesn't match suffix
                  "VibecodedOrchestrator_KnowledgeGraph"):
            self.assertFalse(ws.is_code_collection(n))


# ---------------------------------------------------------------------------
# diff_collection_vs_target
# ---------------------------------------------------------------------------


class DiffTests(unittest.TestCase):
    def test_returns_missing_slots_only(self):
        # Collection has only qwen3_embed; target is the 5-slot KG catalog.
        fetcher = _FakeFetcher({
            "TestKG": _fake_schema(["qwen3_embed"]),
        })
        missing = ws.diff_collection_vs_target(
            "TestKG", ws.KG_NAMED_VECTORS, schema_fetcher=fetcher,
        )
        missing_names = {s.name for s in missing}
        self.assertNotIn("qwen3_embed", missing_names)
        # All non-present slots are returned.
        self.assertEqual(
            missing_names,
            {"ollama_embed", "openai_embed", "arctic2_embed", "openai_text_embed"},
        )

    def test_empty_diff_when_at_target(self):
        # Collection has every catalog slot.
        all_names = [s.name for s in ws.KG_NAMED_VECTORS]
        fetcher = _FakeFetcher({"TestKG": _fake_schema(all_names)})
        missing = ws.diff_collection_vs_target(
            "TestKG", ws.KG_NAMED_VECTORS, schema_fetcher=fetcher,
        )
        self.assertEqual(missing, [])

    def test_extra_slots_in_actual_are_not_flagged(self):
        # Collection has more slots than target — diff should still be empty.
        names = [s.name for s in ws.KG_NAMED_VECTORS] + ["custom_user_slot"]
        fetcher = _FakeFetcher({"TestKG": _fake_schema(names)})
        missing = ws.diff_collection_vs_target(
            "TestKG", ws.KG_NAMED_VECTORS, schema_fetcher=fetcher,
        )
        self.assertEqual(missing, [])

    def test_missing_collection_raises(self):
        fetcher = _FakeFetcher({})  # collection absent
        with self.assertRaises(RuntimeError) as ctx:
            ws.diff_collection_vs_target(
                "TestKG", ws.KG_NAMED_VECTORS, schema_fetcher=fetcher,
            )
        self.assertIn("not found", str(ctx.exception))

    def test_preserves_input_slot_order(self):
        # diff returns missing slots in the order they appear in target_slots.
        # Verify by ordering a synthetic target with reversed names.
        target = [
            ws.NamedVectorSlot("c_slot", 1),
            ws.NamedVectorSlot("a_slot", 1),
            ws.NamedVectorSlot("b_slot", 1),
        ]
        fetcher = _FakeFetcher({"X": _fake_schema([])})  # collection has no slots
        missing = ws.diff_collection_vs_target(
            "X", target, schema_fetcher=fetcher,
        )
        self.assertEqual([s.name for s in missing], ["c_slot", "a_slot", "b_slot"])


# ---------------------------------------------------------------------------
# add_named_vector_slot
# ---------------------------------------------------------------------------


class AddNamedVectorSlotTests(unittest.TestCase):
    def test_created_when_slot_absent(self):
        fetcher = _FakeFetcher({"X": _fake_schema(["qwen3_embed"])})
        rebuilder = _FakeRebuilder(fetcher)
        slot = ws.NamedVectorSlot("new_slot", 1024)
        result = ws.add_named_vector_slot(
            "X", slot,
            schema_fetcher=fetcher,
            collection_rebuilder=rebuilder,
        )
        self.assertEqual(result, ws.AddSlotResult.Created)
        self.assertEqual(len(rebuilder.calls), 1)
        # The rebuilder was called with union(existing, new_slot).
        _, target_names = rebuilder.calls[0]
        self.assertIn("qwen3_embed", target_names)
        self.assertIn("new_slot", target_names)

    def test_skipped_when_slot_present_with_matching_dim(self):
        # Slot present and probing dim returns matching value (the
        # `_existing_slot_dim` probe is patched to return the catalog dim).
        slot = ws.NamedVectorSlot("qwen3_embed", 1024)
        fetcher = _FakeFetcher({"X": _fake_schema(["qwen3_embed"])})
        rebuilder = _FakeRebuilder(fetcher)
        with mock.patch.object(ws, "_existing_slot_dim", return_value=1024):
            result = ws.add_named_vector_slot(
                "X", slot,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        self.assertEqual(result, ws.AddSlotResult.Skipped)
        # No rebuild should fire when slot already exists.
        self.assertEqual(rebuilder.calls, [])

    def test_skipped_when_slot_present_dim_unknown(self):
        # No data stored yet → can't probe → treat as Skipped, not error.
        slot = ws.NamedVectorSlot("qwen3_embed", 1024)
        fetcher = _FakeFetcher({"X": _fake_schema(["qwen3_embed"])})
        rebuilder = _FakeRebuilder(fetcher)
        with mock.patch.object(ws, "_existing_slot_dim", return_value=None):
            result = ws.add_named_vector_slot(
                "X", slot,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        self.assertEqual(result, ws.AddSlotResult.Skipped)

    def test_dim_mismatch_returns_error(self):
        # Slot present with 768d stored vectors; target says 1024d.
        slot = ws.NamedVectorSlot("qwen3_embed", 1024)
        fetcher = _FakeFetcher({"X": _fake_schema(["qwen3_embed"])})
        rebuilder = _FakeRebuilder(fetcher)
        with mock.patch.object(ws, "_existing_slot_dim", return_value=768):
            result = ws.add_named_vector_slot(
                "X", slot,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        self.assertEqual(result, ws.AddSlotResult.DimMismatchError)
        # No rebuild attempted on dim-mismatch.
        self.assertEqual(rebuilder.calls, [])

    def test_missing_collection_raises(self):
        fetcher = _FakeFetcher({})
        rebuilder = _FakeRebuilder(fetcher)
        slot = ws.NamedVectorSlot("new_slot", 1024)
        with self.assertRaises(RuntimeError) as ctx:
            ws.add_named_vector_slot(
                "X", slot,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        self.assertIn("not found", str(ctx.exception))

    def test_rebuilder_failure_wraps_in_runtime_error(self):
        fetcher = _FakeFetcher({"X": _fake_schema(["qwen3_embed"])})

        def failing_rebuilder(*a, **kw):
            raise ValueError("simulated copy failure")

        slot = ws.NamedVectorSlot("new_slot", 1024)
        with self.assertRaises(RuntimeError) as ctx:
            ws.add_named_vector_slot(
                "X", slot,
                schema_fetcher=fetcher,
                collection_rebuilder=failing_rebuilder,
            )
        self.assertIn("failed to rebuild", str(ctx.exception))
        self.assertIn("new_slot", str(ctx.exception))


# ---------------------------------------------------------------------------
# migrate_collection_to_target
# ---------------------------------------------------------------------------


class MigrateCollectionToTargetTests(unittest.TestCase):
    def test_idempotent_when_at_target(self):
        all_names = [s.name for s in ws.KG_NAMED_VECTORS]
        fetcher = _FakeFetcher({"X": _fake_schema(all_names)})
        rebuilder = _FakeRebuilder(fetcher)
        with mock.patch.object(ws, "_existing_slot_dim", return_value=None):
            report = ws.migrate_collection_to_target(
                "X", ws.KG_NAMED_VECTORS,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        self.assertEqual(report.added_slots, [])
        self.assertEqual(set(report.skipped_slots), set(all_names))
        self.assertEqual(report.errors, [])
        self.assertTrue(report.ok())
        # No rebuilds — everything was already there.
        self.assertEqual(rebuilder.calls, [])

    def test_adds_missing_slots_and_skips_existing(self):
        # Start with v0.2.17 trio; target is full v0.2.18 catalog.
        legacy = ["qwen3_embed", "ollama_embed", "openai_embed"]
        fetcher = _FakeFetcher({"X": _fake_schema(legacy)})
        rebuilder = _FakeRebuilder(fetcher)
        with mock.patch.object(ws, "_existing_slot_dim", return_value=None):
            report = ws.migrate_collection_to_target(
                "X", ws.KG_NAMED_VECTORS,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        # Legacy 3 should be Skipped, new 2 should be Added.
        self.assertEqual(set(report.skipped_slots), set(legacy))
        self.assertEqual(set(report.added_slots),
                         {"arctic2_embed", "openai_text_embed"})
        self.assertEqual(report.errors, [])
        self.assertTrue(report.ok())

    def test_second_run_is_noop_after_first(self):
        legacy = ["qwen3_embed"]
        fetcher = _FakeFetcher({"X": _fake_schema(legacy)})
        rebuilder = _FakeRebuilder(fetcher)
        with mock.patch.object(ws, "_existing_slot_dim", return_value=None):
            ws.migrate_collection_to_target(
                "X", ws.KG_NAMED_VECTORS,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
            # Reset call log; second invocation must produce 0 additions.
            rebuilder.calls.clear()
            report = ws.migrate_collection_to_target(
                "X", ws.KG_NAMED_VECTORS,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        self.assertEqual(report.added_slots, [])
        self.assertEqual(rebuilder.calls, [])

    def test_dim_mismatch_recorded_in_errors(self):
        # qwen3_embed exists but stored vectors are 768d (catalog says 1024d).
        fetcher = _FakeFetcher({"X": _fake_schema(["qwen3_embed"])})
        rebuilder = _FakeRebuilder(fetcher)
        with mock.patch.object(ws, "_existing_slot_dim", return_value=768):
            report = ws.migrate_collection_to_target(
                "X", ws.KG_NAMED_VECTORS,
                schema_fetcher=fetcher,
                collection_rebuilder=rebuilder,
            )
        # qwen3_embed surfaces in errors[]; ok() is False.
        err_slots = {e["slot"] for e in report.errors}
        self.assertIn("qwen3_embed", err_slots)
        self.assertFalse(report.ok())

    def test_missing_collection_records_error_per_slot(self):
        fetcher = _FakeFetcher({})  # absent
        rebuilder = _FakeRebuilder(fetcher)
        # Pass a single slot for compactness; expect a "not found" error.
        report = ws.migrate_collection_to_target(
            "X", [ws.NamedVectorSlot("any", 1024)],
            schema_fetcher=fetcher,
            collection_rebuilder=rebuilder,
        )
        self.assertEqual(len(report.errors), 1)
        self.assertIn("not found", report.errors[0]["reason"].lower())


# ---------------------------------------------------------------------------
# enumerate_kg_collections / enumerate_code_collections
# ---------------------------------------------------------------------------


class EnumerateTests(unittest.TestCase):
    def setUp(self):
        # Patch the internal `_list_all_classes` to return a fixed list.
        self._all_classes = [
            "ClaudeOrchestrator_KnowledgeGraph",
            "ClaudeOrchestrator_Development",
            "SD15_KnowledgeGraph",
            "SD15_CodeFunction",
            "SD15_CodeClass",
            "CodeFunction",  # bare name (legacy)
            "Vibecoded_orchestrator_CodeInteraction",
            "VibecodedOrchestrator_KnowledgeGraph",  # shared KG (canonical)
            "Random_OtherCollection",  # unrelated
        ]
        self._patch = mock.patch.object(
            ws, "_list_all_classes", return_value=self._all_classes,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_enumerate_kg_all_projects(self):
        found = ws.enumerate_kg_collections(project_name=None)
        # Must include both per-project KG/Dev classes and the shared KG.
        self.assertIn("ClaudeOrchestrator_KnowledgeGraph", found)
        self.assertIn("ClaudeOrchestrator_Development", found)
        self.assertIn("SD15_KnowledgeGraph", found)
        self.assertIn("VibecodedOrchestrator_KnowledgeGraph", found)
        # Must NOT include code collections or unrelated names.
        self.assertNotIn("SD15_CodeFunction", found)
        self.assertNotIn("Random_OtherCollection", found)

    def test_enumerate_kg_per_project(self):
        found = ws.enumerate_kg_collections(project_name="SD15")
        # SD15_KnowledgeGraph exists in the listed; Development doesn't.
        self.assertIn("SD15_KnowledgeGraph", found)
        # Dev is absent in our fixture, so it must NOT appear (filtered out).
        self.assertNotIn("SD15_Development", found)
        # Shared KG always part of per-project triple (if listed).
        self.assertIn("VibecodedOrchestrator_KnowledgeGraph", found)

    def test_enumerate_code_all_projects(self):
        found = ws.enumerate_code_collections(project_name=None)
        # Per-project code classes + bare-name legacy class included.
        self.assertIn("SD15_CodeFunction", found)
        self.assertIn("SD15_CodeClass", found)
        self.assertIn("Vibecoded_orchestrator_CodeInteraction", found)
        self.assertIn("CodeFunction", found)
        # KG and unrelated NOT included.
        self.assertNotIn("SD15_KnowledgeGraph", found)
        self.assertNotIn("Random_OtherCollection", found)

    def test_enumerate_code_per_project(self):
        found = ws.enumerate_code_collections(project_name="SD15")
        # Project-prefixed code classes that actually exist.
        self.assertIn("SD15_CodeFunction", found)
        self.assertIn("SD15_CodeClass", found)
        # Bare-name code classes that exist also included.
        self.assertIn("CodeFunction", found)
        # Project-prefixed code classes NOT in the listed set are excluded.
        self.assertNotIn("SD15_CodeAPI", found)
        # Non-SD15 code classes NOT included.
        self.assertNotIn("Vibecoded_orchestrator_CodeInteraction", found)


# ---------------------------------------------------------------------------
# format_reports_table
# ---------------------------------------------------------------------------


class FormatReportsTableTests(unittest.TestCase):
    def test_empty_reports_handled(self):
        out = ws.format_reports_table([])
        self.assertIn("no collections matched", out)

    def test_rows_include_added_and_errors(self):
        reports = [
            ws.MigrationReport(
                collection="Test_KnowledgeGraph",
                added_slots=["arctic2_embed", "openai_text_embed"],
                skipped_slots=["qwen3_embed", "ollama_embed", "openai_embed"],
                errors=[],
                objects_copied=5,
            ),
            ws.MigrationReport(
                collection="Test_CodeFunction",
                added_slots=[],
                skipped_slots=["codesage_embed"],
                errors=[{"slot": "jina_embed", "reason": "fetch failed"}],
                objects_copied=0,
            ),
        ]
        out = ws.format_reports_table(reports)
        self.assertIn("Test_KnowledgeGraph", out)
        self.assertIn("arctic2_embed, openai_text_embed", out)
        self.assertIn("Test_CodeFunction", out)
        self.assertIn("jina_embed", out)
        self.assertIn("fetch failed", out)


# ---------------------------------------------------------------------------
# Live integration test (auto-skipped when Weaviate unreachable)
# ---------------------------------------------------------------------------


def _weaviate_reachable(url: str) -> bool:
    """Quick HEAD-style probe — Weaviate's readyz endpoint."""
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/v1/.well-known/ready",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ConnectionError):
        return False


class LiveSchemaMigrationTest(unittest.TestCase):
    """Live tests against a running Weaviate.

    These exercise the full code path:
      - Create a v0.2.17-shaped collection (3 KG slots, populated with vectors)
      - Run `migrate_collection_to_target` with v0.2.18 KG catalog
      - Verify the new 2 slots are added + all 3 original slots' data survives
      - Re-run migration — verify second-pass is fully Skipped (idempotency)

    Self-skips when Weaviate is unreachable so CI runs that don't ship
    a Weaviate container don't choke.
    """

    TEST_COLL = "VCO218SchemaTest"

    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
        if not _weaviate_reachable(cls.url):
            raise unittest.SkipTest(
                f"Weaviate not reachable at {cls.url} — live test skipped"
            )

    def setUp(self):
        # Ensure a clean slate.
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        for name in (self.TEST_COLL, f"{self.TEST_COLL}__staging"):
            try:
                req = urllib.request.Request(
                    f"{self.url}/v1/schema/{name}",
                    method="DELETE",
                )
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                # 404 = already gone, fine.
                if e.code != 404:
                    pass
            except (urllib.error.URLError, OSError):
                pass

    def _create_v0217_shaped_collection(self):
        """Create a 3-slot KG collection mimicking a v0.2.17 install."""
        body = {
            "class": self.TEST_COLL,
            "vectorConfig": {
                "qwen3_embed":  {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
                "ollama_embed": {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
                "openai_embed": {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
            },
            "invertedIndexConfig": {"indexNullState": True},
            "properties": [
                {"name": "title", "dataType": ["text"]},
                {"name": "content", "dataType": ["text"]},
            ],
        }
        req = urllib.request.Request(
            f"{self.url}/v1/schema",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn(r.status, (200, 201))

    def _populate_with_vectors(self, count: int = 3) -> list[str]:
        """Insert `count` objects with multi-named-vector payloads.

        Each object has qwen3_embed populated (1024d float). Returns
        UUIDs for later round-trip verification.
        """
        import uuid as _uuid

        import weaviate  # type: ignore[import-untyped]

        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1])
        grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host, grpc_port=grpc_port, grpc_secure=False,
            skip_init_checks=True,
        )
        uuids: list[str] = []
        try:
            col = client.collections.get(self.TEST_COLL)
            with col.batch.dynamic() as bw:
                for i in range(count):
                    uid = str(_uuid.uuid4())
                    uuids.append(uid)
                    bw.add_object(
                        properties={"title": f"node-{i}", "content": f"body-{i}"},
                        uuid=uid,
                        vector={"qwen3_embed": [float(i)] * 1024},
                    )
        finally:
            client.close()
        return uuids

    def _count_objects_and_verify_qwen3(self) -> tuple[int, set[str]]:
        import weaviate  # type: ignore[import-untyped]

        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1])
        grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host, grpc_port=grpc_port, grpc_secure=False,
            skip_init_checks=True,
        )
        uuids = set()
        try:
            col = client.collections.get(self.TEST_COLL)
            for obj in col.iterator(include_vector=True):
                uuids.add(str(obj.uuid))
                # qwen3_embed data must survive the rebuild.
                self.assertIsInstance(obj.vector, dict)
                self.assertIn("qwen3_embed", obj.vector)
                self.assertEqual(len(obj.vector["qwen3_embed"]), 1024)
            return (len(uuids), uuids)
        finally:
            client.close()

    def test_v0217_collection_gains_v0218_slots_with_data_preserved(self):
        """Critical backward-compat test:
          1. Create a v0.2.17 3-slot KG with 3 objects (qwen3 vectors populated).
          2. Run v0.2.18 migration to 5-slot KG.
          3. Verify all 3 original objects still exist with intact qwen3 vectors.
          4. Verify the 2 new slots (arctic2_embed, openai_text_embed) are
             present in the schema but empty (not yet backfilled — that's
             Commit 9's job).
          5. Re-run migration → all 5 slots reported as Skipped (idempotency).
        """
        self._create_v0217_shaped_collection()
        original_uuids = set(self._populate_with_vectors(count=3))
        self.assertEqual(len(original_uuids), 3)

        # First migration pass.
        report = ws.migrate_collection_to_target(
            self.TEST_COLL,
            ws.KG_NAMED_VECTORS,
            weaviate_url=self.url,
        )
        self.assertTrue(report.ok(), msg=f"errors: {report.errors}")
        self.assertEqual(
            set(report.added_slots),
            {"arctic2_embed", "openai_text_embed"},
            msg=f"added: {report.added_slots}",
        )
        self.assertEqual(
            set(report.skipped_slots),
            {"qwen3_embed", "ollama_embed", "openai_embed"},
        )

        # Verify post-migration schema has all 5 slots.
        post_schema = ws._fetch_schema(self.TEST_COLL, weaviate_url=self.url)
        self.assertIsNotNone(post_schema)
        post_slots = set(post_schema["vectorConfig"].keys())
        self.assertEqual(
            post_slots,
            {
                "qwen3_embed", "ollama_embed", "openai_embed",
                "arctic2_embed", "openai_text_embed",
            },
        )

        # Verify data preserved: 3 UUIDs survived, qwen3_embed vectors
        # round-tripped through the schema rebuild.
        count, post_uuids = self._count_objects_and_verify_qwen3()
        self.assertEqual(count, 3)
        self.assertEqual(post_uuids, original_uuids,
                         msg="UUIDs not preserved through schema migration")

        # Staging must be cleaned up.
        staging_schema = ws._fetch_schema(
            f"{self.TEST_COLL}__staging", weaviate_url=self.url,
        )
        self.assertIsNone(staging_schema,
                         msg="staging not cleaned up after successful migrate")

        # Second pass is fully idempotent.
        report2 = ws.migrate_collection_to_target(
            self.TEST_COLL,
            ws.KG_NAMED_VECTORS,
            weaviate_url=self.url,
        )
        self.assertEqual(report2.added_slots, [],
                         msg="second migration must add zero slots")
        self.assertEqual(len(report2.skipped_slots), 5)
        self.assertTrue(report2.ok())

    def test_multi_slot_writes_survive_subsequent_migration(self):
        """A vector posted to one slot doesn't affect other slots, and
        subsequent migrations that add OTHER slots preserve it.

        Setup: create a fresh 5-slot collection (post-v0.2.18 target),
        write a vector ONLY to qwen3_embed, run migrate_collection_to_target
        with an EXTRA hypothetical slot, verify qwen3 data is preserved.
        """
        # Create the post-v0.2.18 5-slot collection from scratch.
        body = {
            "class": self.TEST_COLL,
            "vectorConfig": {
                s.name: s.to_weaviate_config() for s in ws.KG_NAMED_VECTORS
            },
            "invertedIndexConfig": {"indexNullState": True},
            "properties": [
                {"name": "title", "dataType": ["text"]},
            ],
        }
        req = urllib.request.Request(
            f"{self.url}/v1/schema",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn(r.status, (200, 201))

        # Write 2 objects with vectors only in qwen3_embed.
        uuids = self._populate_with_vectors(count=2)
        self.assertEqual(len(uuids), 2)

        # Run migration with an EXTRA slot beyond the standard catalog.
        extended_catalog = list(ws.KG_NAMED_VECTORS) + [
            ws.NamedVectorSlot("hypothetical_future_slot", 1024),
        ]
        report = ws.migrate_collection_to_target(
            self.TEST_COLL,
            extended_catalog,
            weaviate_url=self.url,
        )
        self.assertTrue(report.ok(), msg=f"errors: {report.errors}")
        self.assertIn("hypothetical_future_slot", report.added_slots)

        # Verify post-migration: qwen3_embed data still present.
        count, post_uuids = self._count_objects_and_verify_qwen3()
        self.assertEqual(count, 2)
        self.assertEqual(set(post_uuids), set(uuids))

        # Verify the new slot is in the schema.
        post_schema = ws._fetch_schema(self.TEST_COLL, weaviate_url=self.url)
        self.assertIn(
            "hypothetical_future_slot",
            post_schema["vectorConfig"],
        )


if __name__ == "__main__":
    unittest.main()
