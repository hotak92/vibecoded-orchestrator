# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.54 Track D (P0-2) regression tests for
templates/scripts/maintain_knowledge_graph.py data-safety guards:

  1. Shared-collection refusal: --fix / --rebuild against the shared KG
     collection (orchestrator-root rebind scenario) exit 2 unless
     VCO_MAINTAIN_SHARED_KG_CONSENT=1 — pre-fix the orphan prune would
     classify every cross-project shared node as orphaned and delete it.
  2. UUID-keyed deletion: orphan deletion targets the exact flagged
     object UUIDs — pre-fix it re-queried by TITLE equality (limit 10)
     and destroyed duplicate-titled LIVE nodes as collateral.
  3. Confirmation gate: destructive steps refuse on a non-interactive
     shell without --yes (exit 3 for --rebuild; skip for --fix).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "templates" / "scripts" / "maintain_knowledge_graph.py"

_TMP_PROJECT = tempfile.mkdtemp(prefix="maintain-kg-guards-")


def _load_module():
    env = {
        "CLAUDE_PROJECT_ROOT": _TMP_PROJECT,
        "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
        "KG_COLLECTION": "TestProj_KnowledgeGraph",
        "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
    }
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location(
            "maintain_kg_under_test", SCRIPT,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


@pytest.fixture(scope="module")
def mkg():
    try:
        return _load_module()
    except (ImportError, RuntimeError) as e:  # pragma: no cover
        pytest.skip(f"maintain_knowledge_graph deps unavailable: {e}")


# ---------------------------------------------------------------------------
# 1. Shared-collection detection + refusal
# ---------------------------------------------------------------------------

class TestSharedCollectionGuard:
    def test_detects_canonical_shared_name(self, mkg):
        assert mkg._is_shared_collection("VibeCodedOrchestrator_KnowledgeGraph")

    def test_detects_legacy_and_casing_variants(self, mkg):
        # Pre-v0.2.12 legacy name + lowercase-c v0.2.12-v0.2.22 casing.
        assert mkg._is_shared_collection("VibeCodedTools_KnowledgeGraph")
        assert mkg._is_shared_collection("VibecodedOrchestrator_KnowledgeGraph")
        assert mkg._is_shared_collection("VIBECODEDORCHESTRATOR_KNOWLEDGEGRAPH")

    def test_per_project_collection_not_shared(self, mkg):
        assert not mkg._is_shared_collection("TestProj_KnowledgeGraph")
        assert not mkg._is_shared_collection("")

    def _run_main(self, mkg, argv, env=None):
        with mock.patch.object(sys, "argv", ["maintain_knowledge_graph.py"] + argv), \
             mock.patch.dict(os.environ, env or {}, clear=False):
            mkg.main()

    def test_fix_on_shared_collection_refused(self, mkg):
        sentinel = mock.MagicMock(
            side_effect=AssertionError("must not reach Weaviate"))
        with mock.patch.object(mkg, "KNOWLEDGE_COLLECTION",
                               "VibeCodedOrchestrator_KnowledgeGraph"), \
             mock.patch.object(mkg, "WeaviateMCPServer", sentinel), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VCO_MAINTAIN_SHARED_KG_CONSENT", None)
            with pytest.raises(SystemExit) as exc:
                self._run_main(mkg, ["--fix"])
        assert exc.value.code == 2
        sentinel.assert_not_called()

    def test_rebuild_on_shared_collection_refused(self, mkg):
        sentinel = mock.MagicMock(
            side_effect=AssertionError("must not reach Weaviate"))
        with mock.patch.object(mkg, "KNOWLEDGE_COLLECTION",
                               "VibeCodedTools_KnowledgeGraph"), \
             mock.patch.object(mkg, "WeaviateMCPServer", sentinel):
            os.environ.pop("VCO_MAINTAIN_SHARED_KG_CONSENT", None)
            with pytest.raises(SystemExit) as exc:
                self._run_main(mkg, ["--rebuild"])
        assert exc.value.code == 2
        sentinel.assert_not_called()

    def test_check_on_shared_collection_allowed(self, mkg):
        # Read-only --check passes the guard (then fails later for
        # unrelated reasons — no embedding backend in the test env —
        # which exits 1, NOT the guard's 2).
        with mock.patch.object(mkg, "KNOWLEDGE_COLLECTION",
                               "VibeCodedOrchestrator_KnowledgeGraph"), \
             mock.patch.object(
                 mkg.EmbeddingService, "for_project",
                 side_effect=mkg.NoEmbeddingBackendError("none")):
            os.environ.pop("VCO_MAINTAIN_SHARED_KG_CONSENT", None)
            with pytest.raises(SystemExit) as exc:
                self._run_main(mkg, ["--check"])
        assert exc.value.code == 1, "guard must not fire for --check"

    def test_consent_env_bypasses_guard(self, mkg):
        # With consent, the guard is passed; execution proceeds to the
        # embedding-service step (which we fail deliberately → exit 1).
        with mock.patch.object(mkg, "KNOWLEDGE_COLLECTION",
                               "VibeCodedOrchestrator_KnowledgeGraph"), \
             mock.patch.object(
                 mkg.EmbeddingService, "for_project",
                 side_effect=mkg.NoEmbeddingBackendError("none")):
            with pytest.raises(SystemExit) as exc:
                self._run_main(mkg, ["--fix"],
                               env={"VCO_MAINTAIN_SHARED_KG_CONSENT": "1"})
        assert exc.value.code == 1, "consent must move past the guard"


# ---------------------------------------------------------------------------
# 2. UUID-keyed deletion
# ---------------------------------------------------------------------------

class _FakeCollection:
    def __init__(self):
        self.deleted: list[str] = []
        self.data = mock.MagicMock()
        self.data.delete_by_id.side_effect = self.deleted.append
        # Title-based re-query must NOT happen anymore.
        self.query = mock.MagicMock()
        self.query.fetch_objects.side_effect = AssertionError(
            "deletion must be UUID-keyed, not title-requeried")


class _FakeServer:
    def __init__(self, collection):
        self.client = mock.MagicMock()
        self.client.collections.get.return_value = collection


class TestUuidKeyedDeletion:
    def test_deletes_exactly_flagged_uuids(self, mkg):
        col = _FakeCollection()
        server = _FakeServer(col)
        orphans = [
            ("uuid-dead-1", "Duplicate Title", "knowledge/gone-a.md"),
            ("uuid-dead-2", "Duplicate Title", "knowledge/gone-b.md"),
        ]
        deleted = mkg.delete_orphaned_weaviate_entries(server, orphans)
        assert deleted == 2
        assert col.deleted == ["uuid-dead-1", "uuid-dead-2"]
        # The live node sharing the same title was never touched: no
        # title query was ever issued (fetch_objects would raise).

    def test_orphan_check_returns_triples(self, mkg, tmp_path):
        with mock.patch.object(mkg, "PROJECT_ROOT", tmp_path):
            (tmp_path / "knowledge").mkdir()
            (tmp_path / "knowledge" / "alive.md").write_text("# Alive\n")
            objects = [
                ("uuid-1", "Alive", "knowledge/alive.md"),
                ("uuid-2", "Gone", "knowledge/gone.md"),
            ]
            orphaned = mkg.check_orphaned_weaviate_entries(
                objects, {"Alive": tmp_path / "knowledge" / "alive.md"},
            )
        assert orphaned == [("uuid-2", "Gone", "knowledge/gone.md")]


# ---------------------------------------------------------------------------
# 3. Confirmation gate
# ---------------------------------------------------------------------------

class TestConfirmationGate:
    def test_assume_yes_short_circuits(self, mkg):
        assert mkg._confirm_destructive("Delete?", assume_yes=True) is True

    def test_non_interactive_without_yes_refuses(self, mkg):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            assert mkg._confirm_destructive("Delete?", assume_yes=False) is False

    def test_interactive_no_refuses(self, mkg):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"):
            assert mkg._confirm_destructive("Delete?", assume_yes=False) is False

    def test_interactive_yes_accepts(self, mkg):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="yes"):
            assert mkg._confirm_destructive("Delete?", assume_yes=False) is True

    def test_rebuild_without_confirmation_exits_3_and_deletes_nothing(self, mkg, tmp_path):
        col = _FakeCollection()
        server = _FakeServer(col)
        fake_obj = mock.MagicMock()
        fake_obj.uuid = "uuid-live"
        with mock.patch.object(mkg, "_fetch_all_objects_paginated",
                               return_value=[fake_obj]), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", tmp_path / "knowledge"), \
             mock.patch.object(sys.stdin, "isatty", return_value=False):
            with pytest.raises(SystemExit) as exc:
                mkg.rebuild_all(server, assume_yes=False)
        assert exc.value.code == 3
        assert col.deleted == [], "refused rebuild must delete nothing"

    def test_fix_skips_deletion_without_confirmation(self, mkg, tmp_path):
        # check_consistency with fix=True on a non-interactive shell and
        # no --yes: the orphan is REPORTED but not deleted.
        col = _FakeCollection()
        server = _FakeServer(col)
        with mock.patch.object(mkg, "PROJECT_ROOT", tmp_path), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", tmp_path / "knowledge"), \
             mock.patch.object(mkg, "get_all_knowledge_files", return_value={}), \
             mock.patch.object(
                 mkg, "get_all_weaviate_objects",
                 return_value=[("uuid-dead", "Gone", "knowledge/gone.md")]), \
             mock.patch.object(sys.stdin, "isatty", return_value=False):
            stats = mkg.check_consistency(server, fix=True, assume_yes=False)
        assert stats["orphaned_weaviate"] == 1
        assert stats["fixed"] == 0
        assert col.deleted == []

    def test_fix_with_yes_deletes(self, mkg, tmp_path):
        col = _FakeCollection()
        server = _FakeServer(col)
        with mock.patch.object(mkg, "PROJECT_ROOT", tmp_path), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", tmp_path / "knowledge"), \
             mock.patch.object(mkg, "get_all_knowledge_files", return_value={}), \
             mock.patch.object(
                 mkg, "get_all_weaviate_objects",
                 return_value=[("uuid-dead", "Gone", "knowledge/gone.md")]):
            stats = mkg.check_consistency(server, fix=True, assume_yes=True)
        assert stats["fixed"] == 1
        assert col.deleted == ["uuid-dead"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
