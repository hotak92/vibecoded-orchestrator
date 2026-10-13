# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-Y: the maintain_knowledge_graph docstring items 4-5, made true.

Item 4 (node timestamps): --check reports nodes whose stored timestamp
disagrees with the source file — the frontmatter ``updated:`` when the
file declares one (compared against the stored ``updated`` property),
else the file mtime (compared against the stored ``updated_at``
property); --fix re-syncs exactly those nodes through the kg-sync
writer (sync_node).

Item 5 (cross-references): --check reports nodes whose WikiLinks in the
file are missing from the stored ``links`` / ``typed_links``; --fix
re-syncs them the same way. Broken links whose target does not exist
stay item 3's report — no node is ever invented.

Safety pins: --check writes NOTHING (no row deletion, no sync call),
and --fix on the SHARED KG collection is still refused (exit 2).

All Weaviate interaction is faked (same pattern as
tests/test_maintain_kg_guards.py); WEAVIATE_URL is pinned to the
unroutable sentinel so no live Weaviate can be contacted.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "templates" / "scripts" / "maintain_knowledge_graph.py"

_TMP_PROJECT = tempfile.mkdtemp(prefix="maintain-kg-w6y-")


def _load_module():
    env = {
        "CLAUDE_PROJECT_ROOT": _TMP_PROJECT,
        "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
        "KG_COLLECTION": "W6YProj_KnowledgeGraph",
        "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
        "WEAVIATE_URL": "http://127.0.0.1:9",
    }
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location(
            "maintain_kg_w6y_under_test", SCRIPT,
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


@pytest.fixture()
def project(tmp_path):
    """A fake project root with a knowledge/ tree."""
    (tmp_path / "knowledge").mkdir()
    return tmp_path


def _write_node(project: Path, name: str, frontmatter: list[str], body: str) -> Path:
    """Write one KG node. `type:` is always declared so the sync parser
    never needs its KNOWLEDGE_ROOT-relative folder fallback (which does
    not cover this tmp project)."""
    fm = "---\n" + "\n".join(frontmatter) + "\n---\n"
    path = project / "knowledge" / f"{name}.md"
    path.write_text(fm + f"# {name}\n\n{body}\n", encoding="utf-8")
    return path


def _row(uuid: str, title: str, rel_path: str, **props):
    return (uuid, title, rel_path, props)


class _FakeCollection:
    def __init__(self):
        self.deleted: list[str] = []
        self.data = mock.MagicMock()
        self.data.delete_by_id.side_effect = self.deleted.append


class _FakeServer:
    def __init__(self, collection):
        self.client = mock.MagicMock()
        self.client.collections.get.return_value = collection


class _FakeSync:
    """Stands in for sync_knowledge_graph.sync_node (the one writer):
    records the files it was asked to sync, writes nothing."""

    def __init__(self):
        self.synced: list[str] = []

    def __call__(self, server, file_path):
        self.synced.append(Path(file_path).name)
        return True


def _sync_module():
    """The sync_knowledge_graph module object the script under test
    imports from (cached in sys.modules at first load)."""
    return sys.modules["sync_knowledge_graph"]


# ---------------------------------------------------------------------------
# Item 4 — stale timestamps
# ---------------------------------------------------------------------------

class TestStaleTimestamps:
    def test_stale_reported_and_matching_left_alone(self, mkg, project):
        _write_node(
            project, "Stale Node",
            ["title: Stale Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "body",
        )
        _write_node(
            project, "Fresh Node",
            ["title: Fresh Node", "type: concept",
             "updated: 2026-10-02T00:00:00Z"],
            "body",
        )
        rows = [
            _row("uuid-s", "Stale Node", "knowledge/Stale Node.md",
                 updated="2026-09-01T00:00:00Z"),
            _row("uuid-f", "Fresh Node", "knowledge/Fresh Node.md",
                 updated="2026-10-02T00:00:00Z"),
        ]
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            found = mkg.check_stale_timestamps(
                rows, mkg.get_all_knowledge_files()
            )
        assert set(found) == {"Stale Node"}
        stored, expected = found["Stale Node"]
        assert "2026-09-01" in stored
        assert "2026-10-01" in expected

    def test_mtime_fallback_when_no_updated_frontmatter(self, mkg, project):
        path = _write_node(
            project, "Mtime Node",
            ["title: Mtime Node", "type: concept"],
            "body",
        )
        stamp = 1760000000  # 2025-10-09T03:33:20Z
        os.utime(path, (stamp, stamp))
        expected_iso = datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            file_nodes = mkg.get_all_knowledge_files()
            stale_row = mkg.check_stale_timestamps(
                [_row("uuid-m", "Mtime Node", "knowledge/Mtime Node.md",
                      updated_at="2020-01-01T00:00:00Z")],
                file_nodes,
            )
            fresh_row = mkg.check_stale_timestamps(
                [_row("uuid-m", "Mtime Node", "knowledge/Mtime Node.md",
                      updated_at=expected_iso)],
                file_nodes,
            )
        assert set(stale_row) == {"Mtime Node"}
        assert fresh_row == {}, "a stored mtime equal to the file's is not stale"

    def test_undeclared_stored_property_is_not_a_finding(self, mkg, project):
        # A row that does not carry the compared property at all is
        # unjudgeable — conservative, never a finding.
        _write_node(
            project, "No Ts Node",
            ["title: No Ts Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "body",
        )
        rows = [_row("uuid-n", "No Ts Node", "knowledge/No Ts Node.md")]
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            assert mkg.check_stale_timestamps(
                rows, mkg.get_all_knowledge_files()
            ) == {}


# ---------------------------------------------------------------------------
# Item 5 — missing cross-references
# ---------------------------------------------------------------------------

_LINK_BODY = "See [[Plain Target]] and [[uses::Typed Target]]."


class TestMissingLinks:
    def test_missing_reported_and_complete_left_alone(self, mkg, project):
        _write_node(
            project, "Sparse Node",
            ["title: Sparse Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            _LINK_BODY,
        )
        _write_node(
            project, "Complete Node",
            ["title: Complete Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            _LINK_BODY,
        )
        rows = [
            _row("uuid-sp", "Sparse Node", "knowledge/Sparse Node.md",
                 links=[], typed_links=[]),
            _row("uuid-co", "Complete Node", "knowledge/Complete Node.md",
                 links=["Plain Target"],
                 typed_links=[{"relation_type": "uses",
                               "target_title": "Typed Target"}]),
        ]
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            found = mkg.check_missing_links(rows, mkg.get_all_knowledge_files())
        assert set(found) == {"Sparse Node"}
        assert "Plain Target" in found["Sparse Node"]
        assert "uses::Typed Target" in found["Sparse Node"]

    def test_unset_stored_links_count_as_missing(self, mkg, project):
        # links: null (a POSITIVE "unset" fact) is judgeable and empty —
        # every file link is missing.
        _write_node(
            project, "Null Links Node",
            ["title: Null Links Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "[[Plain Target]]",
        )
        rows = [_row("uuid-nl", "Null Links Node", "knowledge/Null Links Node.md",
                     links=None)]
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            found = mkg.check_missing_links(rows, mkg.get_all_knowledge_files())
        assert found == {"Null Links Node": ["Plain Target"]}

    def test_legacy_string_typed_links_are_read(self, mkg, project):
        _write_node(
            project, "Legacy Node",
            ["title: Legacy Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "[[uses::Typed Target]]",
        )
        rows = [_row("uuid-lg", "Legacy Node", "knowledge/Legacy Node.md",
                     links=[],
                     typed_links=["uses::Typed Target"])]
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            assert mkg.check_missing_links(
                rows, mkg.get_all_knowledge_files()
            ) == {}


# ---------------------------------------------------------------------------
# check_consistency integration — reporting, fixing, and write-free --check
# ---------------------------------------------------------------------------

def _mk_project_with_three_nodes(project: Path):
    """Stale Node (timestamp stale, links complete), Sparse Node
    (timestamp fresh, links missing), Fresh Node (all current)."""
    _write_node(
        project, "Stale Node",
        ["title: Stale Node", "type: concept",
         "updated: 2026-10-01T00:00:00Z"],
        "body",
    )
    _write_node(
        project, "Sparse Node",
        ["title: Sparse Node", "type: concept",
         "updated: 2026-10-02T00:00:00Z"],
        _LINK_BODY,
    )
    _write_node(
        project, "Fresh Node",
        ["title: Fresh Node", "type: concept",
         "updated: 2026-10-02T00:00:00Z"],
        _LINK_BODY,
    )
    rows = [
        _row("uuid-stale", "Stale Node", "knowledge/Stale Node.md",
             updated="2026-09-01T00:00:00Z", links=[],
             typed_links=[]),
        _row("uuid-sparse", "Sparse Node", "knowledge/Sparse Node.md",
             updated="2026-10-02T00:00:00Z", links=[],
             typed_links=[]),
        _row("uuid-fresh", "Fresh Node", "knowledge/Fresh Node.md",
             updated="2026-10-02T00:00:00Z", links=["Plain Target"],
             typed_links=[{"relation_type": "uses",
                           "target_title": "Typed Target"}]),
    ]
    triples = [(uuid, title, rel) for uuid, title, rel, _ in rows]
    return rows, triples


class TestCheckConsistency:
    def _patched(self, mkg, project, rows, triples):
        return [
            mock.patch.object(mkg, "PROJECT_ROOT", project),
            mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"),
            mock.patch.object(mkg, "get_all_weaviate_objects",
                              return_value=triples),
            mock.patch.object(mkg, "get_all_weaviate_rows",
                              return_value=rows),
        ]

    def test_check_reports_and_writes_nothing(self, mkg, project):
        rows, triples = _mk_project_with_three_nodes(project)
        col = _FakeCollection()
        server = _FakeServer(col)
        fake_sync = _FakeSync()
        for patch in self._patched(mkg, project, rows, triples):
            patch.start()
        try:
            with mock.patch.object(_sync_module(), "sync_node", fake_sync):
                stats = mkg.check_consistency(server, fix=False, assume_yes=False)
        finally:
            mock.patch.stopall()
        assert stats["stale_timestamps"] == 1
        assert stats["missing_links"] == 1
        assert stats["fixed"] == 0
        assert col.deleted == [], "--check must not delete anything"
        assert fake_sync.synced == [], "--check must not sync anything"

    def test_fix_resyncs_exactly_the_flagged_nodes(self, mkg, project):
        rows, triples = _mk_project_with_three_nodes(project)
        before = {
            p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (project / "knowledge").glob("*.md")
        }
        col = _FakeCollection()
        server = _FakeServer(col)
        fake_sync = _FakeSync()
        for patch in self._patched(mkg, project, rows, triples):
            patch.start()
        try:
            with mock.patch.object(_sync_module(), "sync_node", fake_sync):
                stats = mkg.check_consistency(server, fix=True, assume_yes=True)
        finally:
            mock.patch.stopall()
        assert stats["fixed"] == 2
        # Titles are re-synced in sorted order; Fresh Node is untouched.
        assert fake_sync.synced == ["Sparse Node.md", "Stale Node.md"]
        assert sorted(col.deleted) == ["uuid-sparse", "uuid-stale"]
        assert "uuid-fresh" not in col.deleted
        after = {
            p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (project / "knowledge").glob("*.md")
        }
        assert after == before, "--fix must never rewrite the .md files"


# ---------------------------------------------------------------------------
# Shared-collection refusal still applies
# ---------------------------------------------------------------------------

class TestSharedCollectionStillRefused:
    def test_fix_on_shared_collection_exits_2(self, mkg):
        sentinel = mock.MagicMock(
            side_effect=AssertionError("must not reach Weaviate"))
        os.environ.pop("VCO_MAINTAIN_SHARED_KG_CONSENT", None)
        with mock.patch.object(
                    mkg, "KNOWLEDGE_COLLECTION",
                    "VibeCodedOrchestrator_KnowledgeGraph"), \
             mock.patch.object(mkg, "WeaviateMCPServer", sentinel), \
             mock.patch.object(sys, "argv",
                               ["maintain_knowledge_graph.py", "--fix"]):
            with pytest.raises(SystemExit) as exc:
                mkg.main()
        assert exc.value.code == 2
        sentinel.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
