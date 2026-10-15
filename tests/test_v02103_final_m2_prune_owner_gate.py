# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 final review M-2 — the REGISTERED-OWNER gate lives in the prune
home, so every caller of the deleting prune is covered.

Before the fix the gate (``_walked_root_not_collection_owner``) lived only in
``sync_knowledge_graph.py``: ``kg-sync --all`` from a lane worktree / second
clone was refused, but ``install-bundle --folder <worktree>`` and
``python -m vco_lib.collection_repair --project <worktree>`` still deleted the
rows whose file is absent under the worktree — rows the REAL checkout still
has. Now ``collection_repair.collection_owner_refusal`` runs inside
``prune_foreign_rows`` (both legs: KG and the derived Development
collection). Each caller path gets an act (owner prunes) + leave-alone
(worktree prunes nothing) pair. The kg-sync caller keeps its own tests in
``test_v02103_kg_sync_stored_prune.py::WorktreeRootGuardTests``.

Offline: Weaviate is faked at ``collection_repair._http_request``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from tests.test_v0289_collection_repair import _FakeWeaviateHTTP  # noqa: E402
from tests.test_v0289_knowledge_residue import (  # noqa: E402
    _make_fake_orchestrator)
from vco_lib import collection_repair as crep  # noqa: E402
from vco_lib import knowledge_residue as kres  # noqa: E402
from vco_lib import project_init  # noqa: E402

KG = "WireProj_KnowledgeGraph"
DEV = "WireProj_Development"
URL = "http://weaviate.test:8081"
ROWS = {
    KG: [("u-kg-here", "knowledge/here.md"), ("u-kg-gone", "knowledge/gone.md")],
    DEV: [("u-dev-here", "docs/here.md"), ("u-dev-gone", "docs/gone.md")],
}


class OwnerGateBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="vct-m2-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.owner = self.base / "main"
        self.worktree = self.base / "main.vco-lanes" / "lane-1"
        for tree in (self.owner, self.worktree):
            (tree / "knowledge").mkdir(parents=True)
            (tree / "docs").mkdir(parents=True)
            (tree / "knowledge" / "here.md").write_text("x\n", encoding="utf-8")
            (tree / "docs" / "here.md").write_text("x\n", encoding="utf-8")
            settings = tree / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"env": {"KG_COLLECTION": KG}}),
                                encoding="utf-8")
        self.db = self.base / "state" / "launcher.db"
        self.db.parent.mkdir()
        env = mock.patch.dict(os.environ, {"VCT_LAUNCHER_DB_PATH": str(self.db)})
        env.start()
        self.addCleanup(env.stop)

    def register_owner(self):
        make_launcher_db(self.db, projects=[dict(
            name="WireProj", slug="wireproj", folder_path=str(self.owner),
            kg_primary=KG)])

    def fake(self):
        return _FakeWeaviateHTTP(ROWS)


class PredicateTests(OwnerGateBase):
    def test_worktree_is_refused_for_the_kg_and_the_derived_dev_collection(self):
        self.register_owner()
        for coll in (KG, DEV, KG.lower()):
            reason = crep.collection_owner_refusal(self.worktree, coll)
            self.assertIsNotNone(reason, coll)
            assert reason is not None
            self.assertIn("registered to", reason)
            self.assertIn(str(self.owner), reason)

    def test_owner_and_unowned_collections_proceed(self):
        self.register_owner()
        self.assertIsNone(crep.collection_owner_refusal(self.owner, KG))
        self.assertIsNone(crep.collection_owner_refusal(self.owner, DEV))
        self.assertIsNone(crep.collection_owner_refusal(self.worktree, "Other_KG"))

    def test_no_registry_proceeds_unreadable_registry_refuses(self):
        self.assertIsNone(crep.collection_owner_refusal(self.worktree, KG))
        self.db.write_bytes(b"not a sqlite db")
        reason = crep.collection_owner_refusal(self.worktree, KG)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("could not be read", reason)


class PruneHomeTests(OwnerGateBase):
    """The home itself: every caller inherits this."""

    def _prune(self, tree):
        fake = self.fake()
        with mock.patch.object(crep, "_http_request", fake):
            result = crep.prune_foreign_rows(tree, KG, weaviate_url=URL)
        return result, fake

    def test_leave_alone_worktree_prunes_nothing_and_never_enumerates(self):
        self.register_owner()
        result, fake = self._prune(self.worktree)
        self.assertIn("registered to", result["skipped"])
        self.assertEqual(fake.deleted_uuids, [])
        self.assertEqual([c for c in fake.calls if c[0] == "POST"], [])

    def test_act_owner_prunes_its_gone_row_only(self):
        self.register_owner()
        result, fake = self._prune(self.owner)
        self.assertIsNone(result["skipped"])
        self.assertEqual(fake.deleted_uuids, ["u-kg-gone"])


class BundleCallerTests(OwnerGateBase):
    """``install-bundle --folder <X>`` (project_init's residue/foreign-row
    step → ``prune_foreign_rows_for_project``)."""

    def setUp(self):
        super().setUp()
        self.orch = self.base / "orch"
        self.orch.mkdir()
        _make_fake_orchestrator(self.orch)

    def _bundle(self, tree):
        fake = self.fake()
        cleanup = mock.MagicMock(return_value={"skipped": None, "removed": []})
        with mock.patch.object(crep, "_http_request", fake), \
                mock.patch.object(crep, "weaviate_reachable", lambda url: True), \
                mock.patch.object(kres, "cleanup_bundled_knowledge_residue",
                                  cleanup), \
                mock.patch.dict(os.environ, {"WEAVIATE_URL": URL}):
            result = project_init.install_project_bundle(tree, self.orch)
        return result["foreign_rows"], fake

    def test_leave_alone_worktree_bundle_prunes_nothing(self):
        """RED before the fix: the worktree bundle run deleted u-kg-gone and
        u-dev-gone — rows the registered main checkout may still have."""
        self.register_owner()
        fr, fake = self._bundle(self.worktree)
        self.assertEqual(fake.deleted_uuids, [])
        self.assertEqual(fr["total_deleted"], 0)
        legs = {leg["collection"]: leg for leg in fr["legs"]}
        self.assertIn("registered to", legs[KG]["skipped"])
        self.assertIn("registered to", legs[DEV]["skipped"])

    def test_act_registered_folder_bundle_still_prunes(self):
        self.register_owner()
        fr, fake = self._bundle(self.owner)
        self.assertEqual(sorted(fake.deleted_uuids), ["u-dev-gone", "u-kg-gone"])
        self.assertEqual(fr["total_deleted"], 2)


class CliCallerTests(OwnerGateBase):
    """``python -m vco_lib.collection_repair --project <X>``."""

    def _cli(self, tree):
        fake = self.fake()
        ctx = {"weaviate_url": URL, "kg_collection": KG,
               "development_collection": DEV, "shared_kg_collection": "",
               "source": "settings/binding-first", "fallback_reason": None}
        out = io.StringIO()
        with mock.patch.object(crep, "_http_request", fake), \
                mock.patch.object(crep, "weaviate_reachable", lambda url: True), \
                mock.patch.object(crep, "_resolve_project_context",
                                  lambda folder: dict(ctx)), \
                mock.patch.object(project_init, "_is_root_bundle_target",
                                  lambda root, folder: False), \
                contextlib.redirect_stdout(out):
            code = crep.main(["--project", str(tree), "--json"])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue()), fake

    def test_leave_alone_worktree_cli_prunes_nothing(self):
        self.register_owner()
        result, fake = self._cli(self.worktree)
        self.assertEqual(fake.deleted_uuids, [])
        self.assertEqual(result["total_deleted"], 0)
        self.assertIsNone(result["skipped"])  # reached the legs, not a probe skip
        self.assertEqual(len(result["legs"]), 2)
        for leg in result["legs"]:
            self.assertIn("registered to", leg["skipped"])

    def test_act_registered_folder_cli_still_prunes(self):
        self.register_owner()
        result, fake = self._cli(self.owner)
        self.assertEqual(sorted(fake.deleted_uuids), ["u-dev-gone", "u-kg-gone"])


if __name__ == "__main__":
    unittest.main()
