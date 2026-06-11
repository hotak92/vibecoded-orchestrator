# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.54 Track D (P0-2) regression tests: `migrate-collections` rebuild
must RECREATE + RE-INGEST, not just drop.

Pre-fix behaviour (audit P0-2, Phase C migration-data-recovery scout):

  * `migrate_collections` action == "rebuild" only `_delete_class`-ed; the
    comment claimed the caller's `_ensure_collections` + `_seed_weaviate`
    recreate + re-ingest — true ONLY for the install.py call path.
  * `_cmd_migrate_collections` (the CLI the `schema_migration_required`
    deferral's own `command_to_apply` points users at) never recreated and
    never re-ingested → following the documented recovery command left the
    user's KG collection GONE until the next full install.py run.

Post-fix contract (tested here):

  1. The rebuild action recreates the collection with the target schema
     immediately after the drop (`_create_class(target)`).
  2. The CLI handler re-ingests via the project's bundled
     `.claude/scripts/sync_knowledge_graph.py --all` when --project-folder
     is given.
  3. When the sync script is missing or --project-folder is absent, the
     CLI surfaces `reingest_required: true` (and an errors[] entry for the
     missing-script case) instead of silently reporting success.
  4. The `schema_migration_required` deferral's command_to_apply includes
     `--project-folder` so the documented recovery path re-ingests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


def _legacy_single_vector(name: str = "Foo_KnowledgeGraph") -> dict:
    """A pre-named-vectors class definition that classifies as rebuild."""
    return {
        "class": name,
        "properties": [],
        "vectorizer": "none",
    }


class TestRebuildRecreatesCollection(unittest.TestCase):
    """Fix 1: the rebuild action calls _create_class(target) after the drop."""

    def setUp(self):
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                      "DIAGRAMS_COLLECTION")
        }
        os.environ["KG_COLLECTION"] = "Foo_KnowledgeGraph"
        os.environ.pop("DEVELOPMENT_COLLECTION", None)
        os.environ.pop("DIAGRAMS_COLLECTION", None)
        self.args = argparse.Namespace(force_rebuild=False)

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_rebuild_calls_create_class_after_delete(self):
        fetcher = lambda n: _legacy_single_vector(n)  # noqa: E731
        call_order: list[str] = []

        with mock.patch.object(project_init, "_recover_or_drop_orphan_staging",
                               return_value="none"), \
             mock.patch.object(project_init, "_fetch_schema",
                               side_effect=lambda n, weaviate_url=None: fetcher(n)), \
             mock.patch.object(project_init, "_snapshot_collection_for_rebuild",
                               return_value={"object_count": 0,
                                             "sample_uuids": []}), \
             mock.patch.object(
                 project_init, "_delete_class",
                 side_effect=lambda *a, **k: call_order.append("delete")), \
             mock.patch.object(
                 project_init, "_create_class",
                 side_effect=lambda *a, **k: call_order.append("create")) as cmock:
            result = project_init.migrate_collections(
                self.args, dry_run=False, schema_fetcher=fetcher,
            )

        self.assertEqual(call_order, ["delete", "create"],
                         "rebuild must recreate immediately after the drop")
        # The recreate must use the TARGET schema (current spec), not the
        # legacy definition that triggered the rebuild.
        (recreate_payload,), recreate_kwargs = cmock.call_args
        self.assertEqual(recreate_payload["class"], "Foo_KnowledgeGraph")
        self.assertIn("vectorConfig", recreate_payload,
                      "recreate must POST the current named-vector schema")
        kg_plan = next(p for p in result["plan"]
                       if p["collection"] == "Foo_KnowledgeGraph")
        self.assertEqual(kg_plan["action"], "rebuild")
        self.assertEqual(result["errors"], [])


def _stub_args(**overrides) -> argparse.Namespace:
    base = dict(
        name="Foo",
        all_projects=False,
        force_rebuild=False,
        dry_run=False,
        weaviate_url="http://localhost:9",
        include_code=False,
        project_folder=None,
        json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _canned_result(actions: list[tuple[str, str]]) -> dict:
    return {
        "plan": [
            {"collection": c, "action": a, "objects_copied": 0,
             "elapsed_ms": 1}
            for c, a in actions
        ],
        "dry_run": False,
        "errors": [],
    }


class TestCliReingestAfterRebuild(unittest.TestCase):
    """Fixes 2+3: the CLI handler re-ingests (or loudly refuses to claim
    success) whenever the plan contained a rebuild."""

    def _run_cmd(self, args, canned):
        with mock.patch.object(project_init, "migrate_collections",
                               return_value=canned), \
             mock.patch.object(sys, "stdout"):
            return project_init._cmd_migrate_collections(args)

    def test_reingest_invoked_with_project_folder(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            scripts = folder / ".claude" / "scripts"
            scripts.mkdir(parents=True)
            marker = folder / "reingest-ran.txt"
            (scripts / "sync_knowledge_graph.py").write_text(
                "import sys, pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            args = _stub_args(project_folder=str(folder))
            canned = _canned_result([("Foo_KnowledgeGraph", "rebuild")])

            with mock.patch.object(project_init, "migrate_collections",
                                   return_value=canned):
                rc = project_init._cmd_migrate_collections(args)

            self.assertEqual(rc, 0)
            self.assertTrue(marker.is_file(),
                            "sync_knowledge_graph.py must run after rebuild")
            self.assertEqual(marker.read_text(), "--all")
            self.assertFalse(canned["reingest_required"])
            self.assertEqual(canned["reingest"]["returncode"], 0)

    def test_missing_sync_script_surfaces_error(self):
        with TemporaryDirectory() as td:
            args = _stub_args(project_folder=td)
            canned = _canned_result([("Foo_KnowledgeGraph", "rebuild")])

            with mock.patch.object(project_init, "migrate_collections",
                                   return_value=canned):
                rc = project_init._cmd_migrate_collections(args)

            self.assertEqual(rc, 1, "missing re-ingest path must exit non-zero")
            self.assertTrue(canned["reingest_required"])
            self.assertTrue(
                any(e.get("action") == "reingest" for e in canned["errors"]),
                f"expected a reingest error entry, got: {canned['errors']}",
            )

    def test_no_project_folder_flags_reingest_required(self):
        args = _stub_args(project_folder=None)
        canned = _canned_result([("Foo_KnowledgeGraph", "rebuild")])

        with mock.patch.object(project_init, "migrate_collections",
                               return_value=canned):
            rc = project_init._cmd_migrate_collections(args)

        self.assertEqual(rc, 0)
        self.assertTrue(canned["reingest_required"])

    def test_failing_sync_script_exits_nonzero(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            scripts = folder / ".claude" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "sync_knowledge_graph.py").write_text(
                "import sys; sys.exit(3)\n", encoding="utf-8",
            )
            args = _stub_args(project_folder=str(folder))
            canned = _canned_result([("Foo_KnowledgeGraph", "rebuild")])

            with mock.patch.object(project_init, "migrate_collections",
                                   return_value=canned):
                rc = project_init._cmd_migrate_collections(args)

            self.assertEqual(rc, 1)
            self.assertTrue(canned["reingest_required"])
            self.assertEqual(canned["reingest"]["returncode"], 3)

    def test_no_rebuild_means_no_reingest(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            scripts = folder / ".claude" / "scripts"
            scripts.mkdir(parents=True)
            marker = folder / "reingest-ran.txt"
            (scripts / "sync_knowledge_graph.py").write_text(
                "import pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            args = _stub_args(project_folder=str(folder))
            canned = _canned_result([("Foo_KnowledgeGraph", "copy"),
                                     ("Foo_Development", "noop")])

            with mock.patch.object(project_init, "migrate_collections",
                                   return_value=canned):
                rc = project_init._cmd_migrate_collections(args)

            self.assertEqual(rc, 0)
            self.assertFalse(marker.exists(),
                             "copy/noop plans must not trigger re-ingest")
            self.assertFalse(canned["reingest_required"])


class TestDeferralCommandIncludesProjectFolder(unittest.TestCase):
    """Fix 4: the documented recovery command must carry --project-folder
    so the re-ingest step can find the .md sources."""

    def test_command_to_apply_has_project_folder(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            project_init._emit_migrate_required_deferral(
                folder,
                project_name="Foo",
                weaviate_url="http://localhost:9",
                plan_entries=[
                    {"collection": "Foo_KnowledgeGraph", "action": "rebuild"},
                ],
            )
            deferred = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            self.assertTrue(deferred.is_file())
            content = deferred.read_text(encoding="utf-8")
            self.assertIn("--project-folder", content)
            self.assertIn(str(folder), content)
            self.assertNotIn(
                "falls back to drop+re-embed", content,
                "stale promise removed: the CLI now actually re-ingests",
            )


class TestNameScopingCoversDiagrams(unittest.TestCase):
    """v0.2.54 Track D live-test finding: an AMBIENT DIAGRAMS_COLLECTION
    (exported into the shell by the INVOKING project's settings.json env)
    must not leak into a `--name <other-project>`-scoped run — pre-fix,
    `migrate-collections --name TrackDLive --force-rebuild` executed from
    a shell with the invoker's ambient env-vars dropped + rebuilt the
    invoker's diagrams collection."""

    def test_ambient_diagrams_collection_is_overridden(self):
        captured = {}

        def _capture(ns, **kwargs):
            captured["kg"] = os.environ.get("KG_COLLECTION")
            captured["dev"] = os.environ.get("DEVELOPMENT_COLLECTION")
            captured["diagrams"] = os.environ.get("DIAGRAMS_COLLECTION")
            return {"plan": [], "dry_run": True, "errors": []}

        env_backup = {
            k: os.environ.get(k)
            for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                      "DIAGRAMS_COLLECTION")
        }
        os.environ["DIAGRAMS_COLLECTION"] = "AmbientProject_Diagrams"
        try:
            args = _stub_args(dry_run=True)
            with mock.patch.object(project_init, "migrate_collections",
                                   side_effect=_capture):
                project_init._cmd_migrate_collections(args)
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.assertEqual(captured["kg"], "Foo_KnowledgeGraph")
        self.assertEqual(captured["dev"], "Foo_Development")
        self.assertEqual(
            captured["diagrams"], "Foo_Diagrams",
            "ambient DIAGRAMS_COLLECTION must be re-scoped to --name "
            "(pre-fix it leaked and the invoking project's live Diagrams "
            "collection got rebuilt)",
        )


if __name__ == "__main__":
    unittest.main()
