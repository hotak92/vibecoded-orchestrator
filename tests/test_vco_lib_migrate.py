"""Tests for vco_lib.project_init.migrate_collections (PR 3).

Coverage:
  - _schema_delta + _classify_action against the 7 sampled cases from
    weaviate-schema-port-research-2026-05-01.md.
  - _drop_orphan_staging happy + no-op paths (mocked HTTP).
  - migrate_collections dispatch: dry-run, force-rebuild, copy/patch/rebuild
    selection (mock schema_fetcher, no live Weaviate).
  - --dry-run-migrate produces plan but no Weaviate writes.
  - Crash recovery: orphan __staging dropped before plan execution.
  - Live integration test (skipped if Weaviate unreachable): real
    create-populate-migrate-verify cycle on a throwaway VctMigrateTest
    collection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: synthetic schema dicts mirroring the 7 sampled real-world cases.
# ---------------------------------------------------------------------------

# v0.2.18 — 5-slot KG target catalog (was 3 in v0.2.17). The 3 legacy
# slots (qwen3_embed, ollama_embed, openai_embed) are RETAINED for
# back-compat data preservation; arctic2_embed + openai_text_embed are
# the new v0.2.18 additions. See vco_lib/weaviate_schema.py for the
# canonical slot catalog.
_TARGET_VEC_CONFIG = {
    "qwen3_embed":        {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
    "ollama_embed":       {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
    "openai_embed":       {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
    "arctic2_embed":      {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
    "openai_text_embed":  {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
}

_FULL_PROPS = [
    {"name": "title", "dataType": ["text"]},
    {"name": "content", "dataType": ["text"]},
    {"name": "file_path", "dataType": ["text"]},
    {"name": "node_type", "dataType": ["text"]},
    {"name": "tags", "dataType": ["text[]"]},
    {"name": "links", "dataType": ["text[]"]},
    {"name": "typed_links", "dataType": ["object[]"]},
    {"name": "status", "dataType": ["text"]},
    # v0.2.17 (Reviewer B nit, folded into v0.2.17 + this test
    # fixture mirrors `kg_class_definition`'s post-v0.2.17 shape):
    # content_hash holds the SHA-256 of the source markdown
    # (frontmatter-minus-updated + body) and powers the embed-skip
    # fast path in sync_node. Adding it to the fixture keeps the
    # schema-delta + patch-props tests aligned with the live shape.
    {"name": "content_hash", "dataType": ["text"]},
]


def _at_target() -> dict:
    return {
        "class": "ClaudeKnowledgeGraph",
        "vectorConfig": dict(_TARGET_VEC_CONFIG),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": list(_FULL_PROPS),
    }


def _at_target_dev() -> dict:
    """A Development collection schema at the post-v0.2.18 target shape.

    PR-24 (2026-05-16) added 4 canonical temporal properties to the
    Development class definition (created, updated, valid_from,
    valid_until — see vco_lib.project_init.development_class_definition).

    v0.2.18 (2026-05-19) added two more for KG parity: `status` (archived
    doc filter) and `content_hash` (embed-skip fast-path). Tests that
    supply a Dev fetcher result must use THIS shape, not `_at_target()`
    (which carries the KG-shape properties), or the migrate dispatch will
    see spurious missing properties.
    """
    return {
        "class": "ClaudeDevelopment",
        "vectorConfig": dict(_TARGET_VEC_CONFIG),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
            {"name": "created", "dataType": ["date"]},
            {"name": "updated", "dataType": ["date"]},
            {"name": "valid_from", "dataType": ["date"]},
            {"name": "valid_until", "dataType": ["date"]},
            # v0.2.18 (2026-05-19): KG parity — `status` + `content_hash`.
            # Must mirror `vco_lib.project_init.development_class_definition`
            # exactly so the schema-delta dispatcher returns noop on
            # at-target Dev collections.
            {"name": "status", "dataType": ["text"]},
            {"name": "content_hash", "dataType": ["text"]},
        ],
    }


def _missing_index_null_state_only() -> dict:
    sch = _at_target()
    sch["invertedIndexConfig"] = {"indexNullState": False}
    return sch


def _missing_openai_slot_and_null_state() -> dict:
    sch = _at_target()
    vec = dict(_TARGET_VEC_CONFIG)
    del vec["openai_embed"]
    sch["vectorConfig"] = vec
    sch["invertedIndexConfig"] = {"indexNullState": False}
    return sch


def _legacy_single_vector() -> dict:
    """ArcAgi-style: no vectorConfig at all."""
    return {
        "class": "ArcAgi_KnowledgeGraph",
        "invertedIndexConfig": {"indexNullState": False},
        "properties": list(_FULL_PROPS),
    }


def _missing_props_only() -> dict:
    sch = _at_target()
    # Drop the last 2 properties from the 'actual' schema to simulate
    # additive drift the user can patch without copy.
    sch["properties"] = list(_FULL_PROPS[:-2])
    return sch


# ---------------------------------------------------------------------------
# SchemaDelta + classify_action
# ---------------------------------------------------------------------------


class SchemaDeltaTests(unittest.TestCase):
    """Cover the 7 sampled cases from the research report."""

    def test_at_target_no_action(self):
        # ClaudeKnowledgeGraph: at-target → noop.
        actual = _at_target()
        target = project_init.kg_class_definition("ClaudeKnowledgeGraph")
        delta = project_init._schema_delta(actual, target)
        self.assertFalse(delta.any())
        self.assertEqual(project_init._classify_action(delta), "noop")

    def test_index_null_state_only(self):
        # TestInstall_KnowledgeGraph / BaliHospitality_KnowledgeGraph /
        # VibecodedOrchestrator_Development: needs indexNullState only.
        actual = _missing_index_null_state_only()
        target = project_init.kg_class_definition("TestInstall_KnowledgeGraph")
        delta = project_init._schema_delta(actual, target)
        self.assertFalse(delta.legacy_single_vector)
        self.assertEqual(delta.missing_vec_slots, [])
        self.assertTrue(delta.indexNullState_needed)
        self.assertEqual(project_init._classify_action(delta), "copy")

    def test_two_deltas_missing_slot_and_null_state(self):
        # SD15_KnowledgeGraph / ARTup_KnowledgeGraph: missing openai slot
        # + indexNullState.
        actual = _missing_openai_slot_and_null_state()
        target = project_init.kg_class_definition("SD15_KnowledgeGraph")
        delta = project_init._schema_delta(actual, target)
        self.assertFalse(delta.legacy_single_vector)
        self.assertIn("openai_embed", delta.missing_vec_slots)
        self.assertTrue(delta.indexNullState_needed)
        self.assertEqual(project_init._classify_action(delta), "copy")

    def test_legacy_single_vector_routes_to_rebuild(self):
        # ArcAgi_KnowledgeGraph: no vectorConfig at all → rebuild only.
        actual = _legacy_single_vector()
        target = project_init.kg_class_definition("ArcAgi_KnowledgeGraph")
        delta = project_init._schema_delta(actual, target)
        self.assertTrue(delta.legacy_single_vector)
        self.assertEqual(project_init._classify_action(delta), "rebuild")

    def test_not_yet_existing_routes_to_create(self):
        # Collection doesn't exist on server yet.
        delta = project_init.SchemaDelta(not_present=True)
        self.assertTrue(delta.any())
        self.assertEqual(project_init._classify_action(delta), "create")

    def test_missing_props_only_routes_to_patch(self):
        actual = _missing_props_only()
        target = project_init.kg_class_definition("Foo_KnowledgeGraph")
        delta = project_init._schema_delta(actual, target)
        self.assertFalse(delta.legacy_single_vector)
        self.assertEqual(delta.missing_vec_slots, [])
        self.assertFalse(delta.indexNullState_needed)
        self.assertTrue(delta.missing_props)
        self.assertEqual(project_init._classify_action(delta), "patch_props")

    def test_no_delta_classifies_noop(self):
        # Sanity: empty delta → noop (cover the order-matters branch in
        # _classify_action where not_present is falsy and any() is falsy).
        delta = project_init.SchemaDelta()
        self.assertEqual(project_init._classify_action(delta), "noop")


# ---------------------------------------------------------------------------
# Crash-recovery: _drop_orphan_staging
# ---------------------------------------------------------------------------


class DropOrphanStagingTests(unittest.TestCase):

    def test_no_orphan_returns_false(self):
        # Fetcher returns None for missing class → no drop, return False.
        with mock.patch.object(project_init, "_fetch_schema", return_value=None) as fmock, \
             mock.patch.object(project_init, "_delete_class") as dmock:
            self.assertFalse(project_init._drop_orphan_staging(
                "Foo_KnowledgeGraph", weaviate_url="http://x:1"))
            fmock.assert_called_once()
            dmock.assert_not_called()

    def test_orphan_present_drops_and_returns_true(self):
        # Post-BLOCKER-1: the back-compat shim now classifies before
        # dropping. With no target schema supplied and `<name>` reported
        # present (the mock returns the same dict for any name) but
        # `_count_objects` returning 0 (mocked URL → connection fails),
        # we hit the "both empty → safe drop" path inside the recover
        # branch. Result: shim returns True, _delete_class called.
        with mock.patch.object(project_init, "_fetch_schema",
                               return_value={"class": "Foo_KnowledgeGraph__staging"}), \
             mock.patch.object(project_init, "_count_objects", return_value=0), \
             mock.patch.object(project_init, "_delete_class") as dmock:
            self.assertTrue(project_init._drop_orphan_staging(
                "Foo_KnowledgeGraph", weaviate_url="http://x:1"))
            dmock.assert_called_once_with(
                "Foo_KnowledgeGraph__staging", weaviate_url="http://x:1")


# ---------------------------------------------------------------------------
# migrate_collections dispatch (mock-based, no live Weaviate)
# ---------------------------------------------------------------------------


class MigrateDispatchUnitTests(unittest.TestCase):
    """Feed fake schema_fetcher results, assert correct action chosen and
    correct downstream helper called."""

    def setUp(self):
        # Set env so migrate_collections sees both env keys.
        self._env_backup = {
            k: os.environ.get(k) for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION")
        }
        os.environ["KG_COLLECTION"] = "Foo_KnowledgeGraph"
        os.environ["DEVELOPMENT_COLLECTION"] = "Foo_Development"
        self.args = argparse.Namespace(force_rebuild=False)

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _fetcher_returning(self, mapping):
        return lambda name: mapping.get(name)

    def test_dry_run_emits_plan_no_writes(self):
        fetcher = self._fetcher_returning({
            "Foo_KnowledgeGraph":  _missing_index_null_state_only(),
            "Foo_Development":     _at_target_dev(),
        })
        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging", return_value="none"), \
             mock.patch.object(project_init, "_create_class") as cmock, \
             mock.patch.object(project_init, "_post_property") as pmock, \
             mock.patch.object(project_init, "_copy_collection_with_vectors") as copmock, \
             mock.patch.object(project_init, "_delete_class") as dmock:
            result = project_init.migrate_collections(
                self.args, dry_run=True, schema_fetcher=fetcher,
            )
        self.assertTrue(result["dry_run"])
        actions = {p["collection"]: p["action"] for p in result["plan"]}
        self.assertEqual(actions, {
            "Foo_KnowledgeGraph": "copy",
            "Foo_Development":    "noop",
        })
        # No Weaviate mutation calls.
        cmock.assert_not_called()
        pmock.assert_not_called()
        copmock.assert_not_called()
        dmock.assert_not_called()
        self.assertEqual(result["errors"], [])

    def test_copy_action_runs_double_copy_sequence(self):
        fetcher = self._fetcher_returning({
            "Foo_KnowledgeGraph":  _missing_index_null_state_only(),
            "Foo_Development":     _at_target_dev(),
        })
        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging", return_value="none"), \
             mock.patch.object(project_init, "_create_class") as cmock, \
             mock.patch.object(project_init, "_copy_collection_with_vectors", return_value=42) as copmock, \
             mock.patch.object(project_init, "_delete_class") as dmock:
            result = project_init.migrate_collections(
                self.args, dry_run=False, schema_fetcher=fetcher,
            )
        # Sequence per copy action: create staging, copy old→staging,
        # delete old, create new, copy staging→new, delete staging.
        # 2 _create_class calls + 2 _copy + 2 _delete for the "copy" action.
        self.assertGreaterEqual(cmock.call_count, 2)
        self.assertEqual(copmock.call_count, 2)
        self.assertEqual(dmock.call_count, 2)
        # Plan reports 42 objects copied (the second value, staging→new).
        kg_plan = next(p for p in result["plan"]
                       if p["collection"] == "Foo_KnowledgeGraph")
        self.assertEqual(kg_plan["action"], "copy")
        self.assertEqual(kg_plan["objects_copied"], 42)
        self.assertEqual(result["errors"], [])

    def test_patch_props_calls_post_property_per_missing_prop(self):
        fetcher = self._fetcher_returning({
            "Foo_KnowledgeGraph":  _missing_props_only(),
            "Foo_Development":     _at_target_dev(),
        })
        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging", return_value="none"), \
             mock.patch.object(project_init, "_post_property") as pmock, \
             mock.patch.object(project_init, "_copy_collection_with_vectors") as copmock:
            result = project_init.migrate_collections(
                self.args, dry_run=False, schema_fetcher=fetcher,
            )
        # _missing_props_only drops the last 2 from FULL_PROPS (currently 9
        # entries after v0.2.17's content_hash add), target has the full
        # set → 2 missing.
        self.assertEqual(pmock.call_count, 2)
        copmock.assert_not_called()
        kg_plan = next(p for p in result["plan"]
                       if p["collection"] == "Foo_KnowledgeGraph")
        self.assertEqual(kg_plan["action"], "patch_props")
        self.assertEqual(result["errors"], [])

    def test_create_called_for_not_present(self):
        fetcher = self._fetcher_returning({
            # KG missing entirely; Dev present at target.
            "Foo_Development":     _at_target_dev(),
        })
        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging", return_value="none"), \
             mock.patch.object(project_init, "_create_class") as cmock:
            result = project_init.migrate_collections(
                self.args, dry_run=False, schema_fetcher=fetcher,
            )
        # Exactly 1 _create_class for the missing KG.
        self.assertEqual(cmock.call_count, 1)
        kg_plan = next(p for p in result["plan"]
                       if p["collection"] == "Foo_KnowledgeGraph")
        self.assertEqual(kg_plan["action"], "create")
        self.assertEqual(result["errors"], [])

    def test_force_rebuild_flag_overrides_smart_path(self):
        fetcher = self._fetcher_returning({
            # Both at-target — would normally noop.
            "Foo_KnowledgeGraph": _at_target(),
            "Foo_Development":    _at_target_dev(),
        })
        args = argparse.Namespace(force_rebuild=True)
        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging", return_value="none"), \
             mock.patch.object(project_init, "_fetch_schema",
                               side_effect=lambda n, weaviate_url=None: fetcher(n)), \
             mock.patch.object(project_init, "_delete_class") as dmock:
            result = project_init.migrate_collections(
                args, dry_run=False, schema_fetcher=fetcher,
            )
        # Both should be classified rebuild → 2 deletes (skip recreate;
        # caller's _ensure_collections handles that).
        self.assertEqual(dmock.call_count, 2)
        actions = {p["action"] for p in result["plan"]}
        self.assertEqual(actions, {"rebuild"})

    def test_legacy_single_vector_routes_to_rebuild_path(self):
        fetcher = self._fetcher_returning({
            "Foo_KnowledgeGraph": _legacy_single_vector(),
            "Foo_Development":    _at_target_dev(),
        })
        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging", return_value="none"), \
             mock.patch.object(project_init, "_fetch_schema",
                               side_effect=lambda n, weaviate_url=None: fetcher(n)), \
             mock.patch.object(project_init, "_delete_class") as dmock, \
             mock.patch.object(project_init, "_copy_collection_with_vectors") as copmock:
            result = project_init.migrate_collections(
                self.args, dry_run=False, schema_fetcher=fetcher,
            )
        copmock.assert_not_called()  # legacy → no copy
        # 1 delete (KG); Dev was at-target → noop.
        self.assertEqual(dmock.call_count, 1)
        kg_plan = next(p for p in result["plan"]
                       if p["collection"] == "Foo_KnowledgeGraph")
        self.assertEqual(kg_plan["action"], "rebuild")

    def test_orphan_staging_dropped_before_planning(self):
        """Pre-existing <name>__staging from prior failed run gets dropped
        (or recovered) before plan execution."""
        fetcher = self._fetcher_returning({
            "Foo_KnowledgeGraph": _at_target(),
            "Foo_Development":    _at_target_dev(),
        })
        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging",
                               return_value="dropped") as drop_mock:
            project_init.migrate_collections(
                self.args, dry_run=True, schema_fetcher=fetcher,
            )
        # Called once per env-configured collection (2: KG + Dev).
        self.assertEqual(drop_mock.call_count, 2)
        # Each call passes the target schema definition for recovery use.
        for call in drop_mock.call_args_list:
            self.assertIn("target_def", call.kwargs)
            self.assertIsInstance(call.kwargs["target_def"], dict)


# ---------------------------------------------------------------------------
# CLI subcommand
# ---------------------------------------------------------------------------


class CliMigrateCommandTests(unittest.TestCase):
    """`python -m vco_lib.project_init migrate-collections` smoke test."""

    def test_dry_run_returns_zero_no_weaviate(self):
        # Mock the dispatcher so we don't hit the network. We're only
        # verifying argparse wiring + JSON shape.
        fake_result = {
            "plan": [
                {"collection": "Foo_KnowledgeGraph", "action": "copy",
                 "objects_copied": 0, "elapsed_ms": 0},
                {"collection": "Foo_Development",    "action": "noop",
                 "objects_copied": 0, "elapsed_ms": 0},
            ],
            "dry_run": True,
            "errors": [],
        }
        with mock.patch.object(project_init, "migrate_collections",
                               return_value=fake_result):
            argv = ["migrate-collections", "--name", "Foo",
                    "--dry-run", "--json"]
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(sys, "stdout", buf):
                rc = project_init.main(argv)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue().strip())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["errors"], [])

    def test_errors_surface_as_exit_1(self):
        fake_result = {
            "plan": [{"collection": "Foo_KnowledgeGraph", "action": "copy",
                      "objects_copied": 0, "elapsed_ms": 100}],
            "dry_run": False,
            "errors": [{"collection": "Foo_KnowledgeGraph", "action": "copy",
                        "error": "kaboom"}],
        }
        with mock.patch.object(project_init, "migrate_collections",
                               return_value=fake_result):
            argv = ["migrate-collections", "--name", "Foo", "--json"]
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(sys, "stdout", buf):
                rc = project_init.main(argv)
            self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Live integration test — real Weaviate, throwaway collection.
# Skipped automatically if Weaviate is not reachable on localhost:8081.
# ---------------------------------------------------------------------------


def _weaviate_reachable() -> bool:
    url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/v1/.well-known/ready")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


_LIVE_TEST_COLLECTION_NAME = "VctMigrateTest"


@unittest.skipUnless(_weaviate_reachable(),
                     "Weaviate not reachable on localhost:8081")
class LiveMigrateIntegrationTest(unittest.TestCase):
    """Real Weaviate round-trip:
      1. Create VctMigrateTest_KnowledgeGraph with OLD schema (missing
         indexNullState + missing openai slot).
      2. Populate 5 dummy objects with named-vector data.
      3. Call migrate_collections (action should be 'copy').
      4. Assert: collection now has target schema, all 5 UUIDs survived,
         vectors still present, staging dropped.
    """

    KG_NAME = f"{_LIVE_TEST_COLLECTION_NAME}_KnowledgeGraph"
    DEV_NAME = f"{_LIVE_TEST_COLLECTION_NAME}_Development"

    def setUp(self):
        self.url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
        # Pre-clean any leftover state from prior failed runs.
        for n in (self.KG_NAME, self.DEV_NAME,
                  f"{self.KG_NAME}__staging", f"{self.DEV_NAME}__staging"):
            try:
                project_init._delete_class(n, weaviate_url=self.url)
            except Exception:
                pass
        self._env_backup = {
            k: os.environ.get(k) for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION")
        }
        os.environ["KG_COLLECTION"] = self.KG_NAME
        os.environ["DEVELOPMENT_COLLECTION"] = self.DEV_NAME

    def tearDown(self):
        # Clean up after ourselves.
        for n in (self.KG_NAME, self.DEV_NAME,
                  f"{self.KG_NAME}__staging", f"{self.DEV_NAME}__staging"):
            try:
                project_init._delete_class(n, weaviate_url=self.url)
            except Exception:
                pass
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _create_old_schema(self, name: str) -> None:
        """Drift simulation: 2-slot vectorConfig + indexNullState=false."""
        old_def = {
            "class": name,
            "description": "test old-schema collection",
            "vectorConfig": {
                "qwen3_embed":  {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
                "ollama_embed": {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
            },
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [
                {"name": "title",    "dataType": ["text"]},
                {"name": "content",  "dataType": ["text"]},
            ],
        }
        project_init._create_class(old_def, weaviate_url=self.url)

    def _populate_objects(self, name: str, count: int = 5) -> list[str]:
        import uuid
        import weaviate
        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1].split("/")[0])
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host,
            grpc_port=int(os.environ.get("GRPC_PORT", "50052")),
            grpc_secure=False, skip_init_checks=True,
        )
        try:
            col = client.collections.get(name)
            uuids: list[str] = []
            with col.batch.dynamic() as bw:
                for i in range(count):
                    uid = str(uuid.uuid4())
                    uuids.append(uid)
                    bw.add_object(
                        properties={"title": f"node-{i}", "content": f"body-{i}"},
                        uuid=uid,
                        vector={
                            "qwen3_embed":  [float(i)] * 1024,
                            "ollama_embed": [float(i + 100)] * 1024,
                        },
                    )
            return uuids
        finally:
            client.close()

    def _count_objects(self, name: str) -> tuple[int, set[str]]:
        import weaviate
        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1].split("/")[0])
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host,
            grpc_port=int(os.environ.get("GRPC_PORT", "50052")),
            grpc_secure=False, skip_init_checks=True,
        )
        try:
            col = client.collections.get(name)
            uuids = set()
            for obj in col.iterator(include_vector=True):
                uuids.add(str(obj.uuid))
                # Verify named vectors round-tripped.
                self.assertIsInstance(obj.vector, dict)
                self.assertIn("qwen3_embed", obj.vector)
            return (len(uuids), uuids)
        finally:
            client.close()

    def test_round_trip_preserves_uuids_and_vectors(self):
        # 1. Create old-schema collection (drift simulation).
        self._create_old_schema(self.KG_NAME)
        # Dev: leave at target so we exercise the noop branch concurrently.
        project_init._create_class(
            project_init.development_class_definition(self.DEV_NAME),
            weaviate_url=self.url,
        )

        # 2. Populate 5 dummy objects.
        original_uuids = set(self._populate_objects(self.KG_NAME, count=5))
        self.assertEqual(len(original_uuids), 5)

        # 3. Run migrate.
        args = argparse.Namespace(force_rebuild=False)
        result = project_init.migrate_collections(
            args, dry_run=False, weaviate_url=self.url,
        )

        # 4. Assertions:
        # (a) no errors
        self.assertEqual(result["errors"], [], msg=f"plan: {result}")
        # (b) KG action = copy
        kg_plan = next(p for p in result["plan"] if p["collection"] == self.KG_NAME)
        self.assertEqual(kg_plan["action"], "copy")
        self.assertEqual(kg_plan["objects_copied"], 5)
        # (c) target schema present (v0.2.18: 5 slots + indexNullState).
        # Legacy v0.2.17 trio kept for data preservation + new v0.2.18
        # slots added by the additive migration.
        new_schema = project_init._fetch_schema(self.KG_NAME, weaviate_url=self.url)
        self.assertIsNotNone(new_schema)
        self.assertEqual(
            set(new_schema["vectorConfig"].keys()),
            {
                "qwen3_embed",
                "ollama_embed",
                "openai_embed",
                "arctic2_embed",
                "openai_text_embed",
            },
        )
        self.assertTrue(
            new_schema["invertedIndexConfig"]["indexNullState"],
        )
        # (d) all 5 UUIDs survived
        count, uuids_after = self._count_objects(self.KG_NAME)
        self.assertEqual(count, 5)
        self.assertEqual(uuids_after, original_uuids)
        # (e) staging cleaned up
        self.assertIsNone(
            project_init._fetch_schema(f"{self.KG_NAME}__staging",
                                       weaviate_url=self.url),
        )

    def test_crash_recovery_drops_orphan_staging(self):
        """Pre-create a `<name>__staging` orphan and verify migrate drops
        it before planning."""
        # 1. Create the real collection at target (noop normally).
        project_init._create_class(
            project_init.kg_class_definition(self.KG_NAME),
            weaviate_url=self.url,
        )
        project_init._create_class(
            project_init.development_class_definition(self.DEV_NAME),
            weaviate_url=self.url,
        )
        # 2. Pre-create an orphan __staging (simulating prior crash).
        orphan_name = f"{self.KG_NAME}__staging"
        project_init._create_class(
            {**project_init.kg_class_definition(orphan_name)},
            weaviate_url=self.url,
        )
        self.assertIsNotNone(
            project_init._fetch_schema(orphan_name, weaviate_url=self.url),
        )
        # 3. Run migrate.
        args = argparse.Namespace(force_rebuild=False)
        result = project_init.migrate_collections(
            args, dry_run=False, weaviate_url=self.url,
        )
        # 4. Orphan should be gone.
        self.assertIsNone(
            project_init._fetch_schema(orphan_name, weaviate_url=self.url),
        )
        self.assertEqual(result["errors"], [])

    def test_crash_recovery_recovers_data_when_name_deleted_mid_copy(self):
        """BLOCKER-1 repro: prior run died between step 3 (`_delete_class(name)`)
        and step 5 (staging→new copy). Staging holds the only surviving
        copy — migrate must RECOVER, not destroy it.

        Pre-condition setup:
          - `<name>` does NOT exist (simulating mid-copy crash after
            step 3 deleted it).
          - `<name>__staging` exists with 5 dummy objects + named vectors.

        Post-condition assertions:
          - `<name>` exists with target schema (3 named-vector slots +
            indexNullState).
          - All 5 original UUIDs are in `<name>` (data preserved).
          - `<name>__staging` is gone (cleaned up post-recovery).
        """
        # 1. DO NOT pre-create `<name>` — simulates mid-copy crash.
        # 2. Pre-create staging using a "staging-flavoured" KG schema
        #    (target schema would also be fine; we use a 2-slot schema
        #    here to verify the recovery path applies the FULL target
        #    schema afterward, not the staging's old shape).
        staging_name = f"{self.KG_NAME}__staging"
        old_staging_def = {
            "class": staging_name,
            "description": "test staging from prior crashed run",
            "vectorConfig": {
                "qwen3_embed":  {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
                "ollama_embed": {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
            },
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [
                {"name": "title",   "dataType": ["text"]},
                {"name": "content", "dataType": ["text"]},
            ],
        }
        project_init._create_class(old_staging_def, weaviate_url=self.url)

        # Populate staging with 5 dummy objects + named vectors.
        import uuid
        import weaviate
        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1].split("/")[0])
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host,
            grpc_port=int(os.environ.get("GRPC_PORT", "50052")),
            grpc_secure=False, skip_init_checks=True,
        )
        original_uuids: set[str] = set()
        try:
            col = client.collections.get(staging_name)
            with col.batch.dynamic() as bw:
                for i in range(5):
                    uid = str(uuid.uuid4())
                    original_uuids.add(uid)
                    bw.add_object(
                        properties={"title": f"recover-{i}",
                                    "content": f"orig-body-{i}"},
                        uuid=uid,
                        vector={
                            "qwen3_embed":  [float(i)] * 1024,
                            "ollama_embed": [float(i + 100)] * 1024,
                        },
                    )
        finally:
            client.close()
        self.assertEqual(len(original_uuids), 5)

        # Sanity: `<name>` doesn't exist; staging exists with 5 objects.
        self.assertIsNone(
            project_init._fetch_schema(self.KG_NAME, weaviate_url=self.url),
        )
        self.assertIsNotNone(
            project_init._fetch_schema(staging_name, weaviate_url=self.url),
        )

        # Dev: leave at target so migrate runs cleanly on it too.
        project_init._create_class(
            project_init.development_class_definition(self.DEV_NAME),
            weaviate_url=self.url,
        )

        # 3. Run migrate.
        args = argparse.Namespace(force_rebuild=False)
        result = project_init.migrate_collections(
            args, dry_run=False, weaviate_url=self.url,
        )

        # 4. Assertions.
        # (a) no errors — recovery succeeded.
        self.assertEqual(result["errors"], [], msg=f"plan: {result}")
        # (b) `<name>` now exists with the v0.2.18 target schema (5 named-
        #     vector slots: legacy v0.2.17 trio + arctic2_embed +
        #     openai_text_embed) + indexNullState=true.
        new_schema = project_init._fetch_schema(self.KG_NAME, weaviate_url=self.url)
        self.assertIsNotNone(new_schema, msg="recovery did not create <name>")
        self.assertEqual(
            set(new_schema["vectorConfig"].keys()),
            {
                "qwen3_embed",
                "ollama_embed",
                "openai_embed",
                "arctic2_embed",
                "openai_text_embed",
            },
        )
        self.assertTrue(new_schema["invertedIndexConfig"]["indexNullState"])
        # (c) all 5 original UUIDs are in `<name>` post-recovery.
        count, uuids_after = self._count_objects(self.KG_NAME)
        self.assertEqual(count, 5,
                         msg=f"expected 5 recovered objects, got {count}")
        self.assertEqual(uuids_after, original_uuids,
                         msg="UUIDs not preserved through recovery copy")
        # (d) staging cleaned up post-recovery.
        self.assertIsNone(
            project_init._fetch_schema(staging_name, weaviate_url=self.url),
        )

    def test_crash_recovery_drops_orphan_when_name_already_intact(self):
        """SAFE-DROP branch: `<name>` is fine, staging is a leftover from
        an earlier crash with fewer (or equal) objects. Drop staging,
        leave `<name>` untouched, no data loss.
        """
        # 1. Pre-create `<name>` with 5 objects (canonical).
        project_init._create_class(
            project_init.kg_class_definition(self.KG_NAME),
            weaviate_url=self.url,
        )
        project_init._create_class(
            project_init.development_class_definition(self.DEV_NAME),
            weaviate_url=self.url,
        )
        # Populate KG with 5 objects.
        import uuid
        import weaviate
        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1].split("/")[0])
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host,
            grpc_port=int(os.environ.get("GRPC_PORT", "50052")),
            grpc_secure=False, skip_init_checks=True,
        )
        kg_uuids: set[str] = set()
        try:
            col = client.collections.get(self.KG_NAME)
            with col.batch.dynamic() as bw:
                for i in range(5):
                    uid = str(uuid.uuid4())
                    kg_uuids.add(uid)
                    bw.add_object(
                        properties={"title": f"kg-{i}",
                                    "content": f"kg-body-{i}"},
                        uuid=uid,
                        vector={
                            "qwen3_embed":  [float(i)] * 1024,
                            "ollama_embed": [float(i + 100)] * 1024,
                            "openai_embed": [float(i + 200)] * 1024,
                        },
                    )
        finally:
            client.close()
        self.assertEqual(len(kg_uuids), 5)

        # 2. Pre-create staging with 0-2 dummy objects (orphan from
        #    earlier crash where copy was only partially complete).
        staging_name = f"{self.KG_NAME}__staging"
        project_init._create_class(
            {**project_init.kg_class_definition(staging_name)},
            weaviate_url=self.url,
        )
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host,
            grpc_port=int(os.environ.get("GRPC_PORT", "50052")),
            grpc_secure=False, skip_init_checks=True,
        )
        try:
            col = client.collections.get(staging_name)
            with col.batch.dynamic() as bw:
                for i in range(2):
                    bw.add_object(
                        properties={"title": f"orphan-{i}"},
                        uuid=str(uuid.uuid4()),
                        vector={
                            "qwen3_embed":  [float(i)] * 1024,
                            "ollama_embed": [float(i + 100)] * 1024,
                            "openai_embed": [float(i + 200)] * 1024,
                        },
                    )
        finally:
            client.close()

        # 3. Run migrate. Target schema is at `<name>` already → noop
        #    for the action plan, but the recovery sweep should drop
        #    the orphan.
        args = argparse.Namespace(force_rebuild=False)
        result = project_init.migrate_collections(
            args, dry_run=False, weaviate_url=self.url,
        )

        # 4. Assertions.
        # (a) no errors.
        self.assertEqual(result["errors"], [], msg=f"plan: {result}")
        # (b) staging dropped.
        self.assertIsNone(
            project_init._fetch_schema(staging_name, weaviate_url=self.url),
        )
        # (c) `<name>` untouched — all 5 original UUIDs survive.
        count, uuids_after = self._count_objects(self.KG_NAME)
        self.assertEqual(count, 5)
        self.assertEqual(uuids_after, kg_uuids,
                         msg="`<name>` was modified during safe-drop sweep")


class SnapshotForRebuildTests(unittest.TestCase):
    """HIGH-4 (2026-05-01): _snapshot_collection_for_rebuild captures count +
    sample UUIDs without raising on Weaviate-unreachable.
    """

    def test_snapshot_returns_count_and_uuids_on_success(self):
        with mock.patch.object(project_init, "_http_request") as hmock:
            # First call (REST objects) → 200 with two objects.
            # Second call (GraphQL aggregate) → 200 with count=99.
            hmock.side_effect = [
                (200, json.dumps({
                    "objects": [{"id": "uuid-1"}, {"id": "uuid-2"}],
                }).encode("utf-8")),
                (200, json.dumps({
                    "data": {"Aggregate": {"Foo": [{"meta": {"count": 99}}]}},
                }).encode("utf-8")),
            ]
            snap = project_init._snapshot_collection_for_rebuild(
                "Foo", weaviate_url="http://localhost:9999",
            )
        self.assertEqual(snap["object_count"], 99)
        self.assertEqual(snap["sample_uuids"], ["uuid-1", "uuid-2"])

    def test_snapshot_swallows_errors_returns_defaults(self):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=Exception("network down")):
            snap = project_init._snapshot_collection_for_rebuild(
                "Foo", weaviate_url="http://localhost:9999",
            )
        self.assertIsNone(snap["object_count"])
        self.assertEqual(snap["sample_uuids"], [])

    def test_rebuild_action_logs_snapshot_event(self):
        """End-to-end through migrate_collections: rebuild action emits a
        7b.rebuild snapshot log event with object_count + sample_uuids."""
        actual_legacy = {"class": "Foo_KnowledgeGraph", "properties": []}
        target_kg = project_init._kg_class_definition("Foo_KnowledgeGraph")
        fetcher = lambda n: actual_legacy if n == "Foo_KnowledgeGraph" else target_kg

        env_backup = {
            k: os.environ.get(k) for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION")
        }
        os.environ["KG_COLLECTION"] = "Foo_KnowledgeGraph"
        os.environ["DEVELOPMENT_COLLECTION"] = "Foo_Development"
        args = argparse.Namespace(force_rebuild=False)

        try:
            with mock.patch.object(project_init, "_drop_orphan_staging",
                                   return_value=False), \
                 mock.patch.object(project_init, "_fetch_schema",
                                   side_effect=lambda n, weaviate_url=None: fetcher(n)), \
                 mock.patch.object(project_init, "_snapshot_collection_for_rebuild",
                                   return_value={"object_count": 7,
                                                 "sample_uuids": ["x", "y"]}), \
                 mock.patch.object(project_init, "_delete_class") as dmock:
                events: list = []

                def _logger(step, phase, detail="", *, data=None):
                    events.append({"step": step, "phase": phase, "data": data})

                project_init.migrate_collections(
                    args, dry_run=False, schema_fetcher=fetcher,
                    log_event=_logger,
                )
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        snapshot_events = [
            e for e in events
            if e["step"] == "7b.rebuild" and e["phase"] == "snapshot"
        ]
        self.assertEqual(len(snapshot_events), 1)
        snap = snapshot_events[0]["data"]
        self.assertEqual(snap["collection"], "Foo_KnowledgeGraph")
        self.assertEqual(snap["object_count"], 7)
        self.assertEqual(snap["sample_uuids"], ["x", "y"])
        # And _delete_class was called for the rebuild after snapshot.
        dmock.assert_called_with(
            "Foo_KnowledgeGraph",
            weaviate_url=mock.ANY,
        )


if __name__ == "__main__":
    unittest.main()
