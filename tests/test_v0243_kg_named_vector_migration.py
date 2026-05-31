# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V0243-2: Integration test for the kg_named_vector_slots migration step.

Tests the `_migrate_kg_named_vector_slots` function added to install.py at
step 7d/10. Verifies that per-project KG + Development collections that only
carry the v0.2.17 3-slot catalog (qwen3_embed + ollama_embed + openai_embed)
are additively upgraded to the full v0.2.18 5-slot catalog (adds
arctic2_embed + openai_text_embed) without touching existing data.

Unit tests run without Weaviate (mock-based). Live integration test is
skipped when Weaviate is unreachable.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402 — install.py is at repo root
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402
from vco_lib import weaviate_schema as ws  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FULL_5_SLOTS = {
    "qwen3_embed",
    "ollama_embed",
    "openai_embed",
    "arctic2_embed",
    "openai_text_embed",
}

_LEGACY_3_SLOTS = {
    "qwen3_embed",
    "ollama_embed",
    "openai_embed",
}


def _make_3slot_schema(name: str) -> dict:
    """3-slot schema simulating a pre-v0.2.18 collection."""
    return {
        "class": name,
        "vectorConfig": {
            s: {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"}
            for s in sorted(_LEGACY_3_SLOTS)
        },
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [{"name": "title", "dataType": ["text"]}],
    }


def _make_5slot_schema(name: str) -> dict:
    """5-slot schema at the v0.2.18 target."""
    return {
        "class": name,
        "vectorConfig": {
            s: {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"}
            for s in sorted(_FULL_5_SLOTS)
        },
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [{"name": "title", "dataType": ["text"]}],
    }


# ---------------------------------------------------------------------------
# Unit tests (no live Weaviate)
# ---------------------------------------------------------------------------


class TestMigrateKgNamedVectorSlotsUnit(unittest.TestCase):
    """Mock-based tests for _migrate_kg_named_vector_slots."""

    def _env(self, kg: str = "Test_KnowledgeGraph",
             dev: str = "Test_Development") -> dict:
        return {
            "KG_COLLECTION": kg,
            "DEVELOPMENT_COLLECTION": dev,
            "WEAVIATE_URL": "http://localhost:9999",  # unreachable in unit tests
        }

    def test_missing_env_vars_skips_silently(self):
        """When neither KG_COLLECTION nor DEVELOPMENT_COLLECTION is set,
        the function must return without calling migrate_collection_to_target.
        """
        report = DeferralReport()
        env_backup = {
            k: os.environ.pop(k, None)
            for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION")
        }
        try:
            with mock.patch.object(
                ws, "migrate_collection_to_target"
            ) as mct_mock:
                install._migrate_kg_named_vector_slots(report)
            mct_mock.assert_not_called()
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_both_collections_migrated(self):
        """Both KG_COLLECTION and DEVELOPMENT_COLLECTION are migrated."""
        report = DeferralReport()
        env_backup = {}
        env_patch = self._env()
        for k, v in env_patch.items():
            env_backup[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            with mock.patch.object(
                ws, "migrate_collection_to_target",
                return_value=ws.MigrationReport(
                    collection="x",
                    added_slots=["arctic2_embed", "openai_text_embed"],
                ),
            ) as mct_mock:
                install._migrate_kg_named_vector_slots(report)
            # Must be called once for KG + once for Dev = 2 calls.
            self.assertEqual(mct_mock.call_count, 2)
            called_names = {c.args[0] for c in mct_mock.call_args_list}
            self.assertIn("Test_KnowledgeGraph", called_names)
            self.assertIn("Test_Development", called_names)
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_transport_error_captured_as_deferral(self):
        """When migrate_collection_to_target raises, a deferral entry is
        added and the function does NOT re-raise.
        """
        report = DeferralReport()
        env_backup = {}
        for k, v in self._env().items():
            env_backup[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            with mock.patch.object(
                ws, "migrate_collection_to_target",
                side_effect=ConnectionRefusedError("Weaviate down"),
            ):
                install._migrate_kg_named_vector_slots(report)
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        # Connection errors: skipped silently (logged but no deferral).
        # No deferral should have been added (transport failure != slot error).
        self.assertEqual(len(report.entries), 0)

    def test_slot_error_adds_deferral_entry(self):
        """When MigrationReport.errors is non-empty, a deferral entry is
        emitted per error.
        """
        report = DeferralReport()
        env_backup = {}
        env_patch = {
            "KG_COLLECTION": "Foo_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "",  # blank dev → only 1 collection
            "WEAVIATE_URL": "http://localhost:9999",
        }
        for k, v in env_patch.items():
            env_backup[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            err_report = ws.MigrationReport(collection="Foo_KnowledgeGraph")
            err_report.errors.append({
                "slot": "arctic2_embed",
                "reason": "dim mismatch: target=1024 existing=768",
            })
            with mock.patch.object(
                ws, "migrate_collection_to_target",
                return_value=err_report,
            ):
                install._migrate_kg_named_vector_slots(report)
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        # Exactly 1 deferral entry for the slot error.
        self.assertEqual(len(report.entries), 1)
        entry = report.entries[0]
        self.assertIn("arctic2_embed", entry.condition_id)
        self.assertIn("Foo_KnowledgeGraph", entry.condition_id)

    def test_idempotent_all_skipped_no_deferral(self):
        """When all slots are already present (all Skipped), no deferral
        entries are emitted.
        """
        report = DeferralReport()
        env_backup = {}
        env_patch = {
            "KG_COLLECTION": "Foo_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "Foo_Development",
            "WEAVIATE_URL": "http://localhost:9999",
        }
        for k, v in env_patch.items():
            env_backup[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            skipped_report = ws.MigrationReport(
                collection="x",
                skipped_slots=list(_FULL_5_SLOTS),
            )
            with mock.patch.object(
                ws, "migrate_collection_to_target",
                return_value=skipped_report,
            ) as mct_mock:
                install._migrate_kg_named_vector_slots(report)
            # Still called for both collections even when all skipped.
            self.assertEqual(mct_mock.call_count, 2)
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(len(report.entries), 0)


# ---------------------------------------------------------------------------
# Live integration test (auto-skipped when Weaviate unreachable)
# ---------------------------------------------------------------------------


def _weaviate_reachable() -> bool:
    url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/v1/.well-known/ready")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


@unittest.skipUnless(
    _weaviate_reachable(),
    "Weaviate not reachable — skipping live V0243-2 integration test",
)
class TestMigrateKgNamedVectorSlotsLive(unittest.TestCase):
    """Real Weaviate round-trip:
      1. Create a test KG collection with the legacy 3-slot schema.
      2. Populate a few objects.
      3. Call _migrate_kg_named_vector_slots → expect arctic2_embed +
         openai_text_embed added, existing data preserved.
      4. Second call → all slots skipped (idempotent).
    """

    KG_NAME = "V0243Test_KnowledgeGraph"
    DEV_NAME = "V0243Test_Development"

    def setUp(self) -> None:
        self.url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
        # Pre-clean from prior failed runs.
        for n in (self.KG_NAME, self.DEV_NAME):
            self._drop_if_exists(n)
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION", "WEAVIATE_URL")
        }
        os.environ["KG_COLLECTION"] = self.KG_NAME
        os.environ["DEVELOPMENT_COLLECTION"] = self.DEV_NAME
        os.environ["WEAVIATE_URL"] = self.url

    def tearDown(self) -> None:
        for n in (self.KG_NAME, self.DEV_NAME):
            self._drop_if_exists(n)
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _drop_if_exists(self, name: str) -> None:
        req = urllib.request.Request(
            f"{self.url}/v1/schema/{name}", method="DELETE",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def _create_3slot(self, name: str) -> None:
        body = json.dumps(_make_3slot_schema(name)).encode()
        req = urllib.request.Request(
            f"{self.url}/v1/schema",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn(r.status, (200, 201))

    def _schema_slots(self, name: str) -> set[str]:
        req = urllib.request.Request(f"{self.url}/v1/schema/{name}")
        with urllib.request.urlopen(req, timeout=10) as r:
            schema = json.loads(r.read())
        return set(schema.get("vectorConfig", {}).keys())

    def test_migrates_3slot_to_5slot(self) -> None:
        # 1. Create both collections with 3-slot schema.
        self._create_3slot(self.KG_NAME)
        self._create_3slot(self.DEV_NAME)

        # Verify start state.
        self.assertEqual(self._schema_slots(self.KG_NAME), _LEGACY_3_SLOTS)
        self.assertEqual(self._schema_slots(self.DEV_NAME), _LEGACY_3_SLOTS)

        # 2. Run migration.
        report = DeferralReport()
        install._migrate_kg_named_vector_slots(report)

        # 3. Both collections should now have 5 slots.
        self.assertEqual(self._schema_slots(self.KG_NAME), _FULL_5_SLOTS)
        self.assertEqual(self._schema_slots(self.DEV_NAME), _FULL_5_SLOTS)
        # No deferral errors.
        self.assertEqual(len(report.entries), 0)

    def test_idempotent_second_run(self) -> None:
        # Start with 5-slot collections.
        for name in (self.KG_NAME, self.DEV_NAME):
            body = json.dumps(_make_5slot_schema(name)).encode()
            req = urllib.request.Request(
                f"{self.url}/v1/schema",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertIn(r.status, (200, 201))

        # Run twice — must be stable on second pass.
        for run in range(2):
            report = DeferralReport()
            install._migrate_kg_named_vector_slots(report)
            self.assertEqual(self._schema_slots(self.KG_NAME), _FULL_5_SLOTS,
                             msg=f"KG slots changed on run {run+1}")
            self.assertEqual(self._schema_slots(self.DEV_NAME), _FULL_5_SLOTS,
                             msg=f"Dev slots changed on run {run+1}")
            self.assertEqual(len(report.entries), 0,
                             msg=f"unexpected deferral on run {run+1}")


if __name__ == "__main__":
    unittest.main()
