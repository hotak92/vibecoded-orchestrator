# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-Y: the maintain_knowledge_graph docstring items 4-6, made true.

Item 4 (node timestamps): --check reports rows whose stored ``updated``
property disagrees with the file's frontmatter ``updated:``. A file
without frontmatter ``updated:`` is NEVER a timestamp finding — the
mtime fallback is gone (mtime churns on every checkout; the comparison
vco_lib/kg_metadata_repair deliberately excludes). --fix repairs the
flagged rows IN PLACE (collection.data.update(uuid, properties=...)):
no deletion, no re-embed, never rewriting the .md.

Item 5 (cross-references): --check reports rows whose WikiLinks in the
file are missing from the stored ``links`` / ``typed_links``; --fix
repairs them the same in-place way with the file-derived lists.

Batch-review R3 Y-1/Y-2/Y-3: both checks key by the row's STORED
``file_path`` (each row is compared only with the file at that path —
duplicate H1s across files can never be cross-flagged or damaged);
maintain NEVER deletes rows for items 4-5 (a failed update leaves the
row as-is, so a down embedding backend cannot lose a node); failures
are isolated per row (one failing update never aborts the rest).

Follow-up 2, item 6 (required frontmatter fields): --check AND --fix
report FILES whose frontmatter lacks a required field (the set from
sync_knowledge_graph.REQUIRED_FRONTMATTER_FIELDS), naming the exact
fields, keyed by file path; report-only in both modes — --fix never
updates or rewrites for it (KG files are user data).

Safety pins: --check writes NOTHING (no row update, no deletion, no
sync call), and --fix on the SHARED KG collection is still refused
(exit 2).

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
        "KG_COLLECTION": "ProjA_KnowledgeGraph",
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


def _write_node(project: Path, name: str, frontmatter: list[str], body: str,
                filename: str | None = None) -> Path:
    """Write one KG node. `type:` is always declared so the sync parser
    never needs its KNOWLEDGE_ROOT-relative folder fallback (which does
    not cover this tmp project). `filename` defaults to `<name>.md` —
    pass it to build the duplicate-H1 case (two files, one H1)."""
    fm = "---\n" + "\n".join(frontmatter) + "\n---\n"
    path = project / "knowledge" / (filename or f"{name}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm + f"# {name}\n\n{body}\n", encoding="utf-8")
    return path


def _row(uuid: str, title: str, rel_path: str, **props):
    return (uuid, title, rel_path, props)


class _FakeCollection:
    def __init__(self):
        self.deleted: list[str] = []
        self.updates: list[tuple[str, dict]] = []
        self.data = mock.MagicMock()
        self.data.delete_by_id.side_effect = self.deleted.append
        self.data.update.side_effect = (
            lambda uuid, properties: self.updates.append((uuid, properties))
        )


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
# Item 4 — stale timestamps (frontmatter `updated:` only; mtime is gone)
# ---------------------------------------------------------------------------

class TestStaleTimestamps:
    def test_stale_reported_with_fix_properties_matching_left_alone(
            self, mkg, project):
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
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            found = mkg.check_stale_timestamps(rows)
        assert len(found) == 1
        assert found[0]["uuid"] == "uuid-s"
        assert "2026-09-01" in found[0]["detail"]
        assert "2026-10-01" in found[0]["detail"]
        assert found[0]["properties"] == {
            "updated": "2026-10-01T00:00:00+00:00"}, \
            "the in-place fix carries the file-derived updated value"

    def test_no_frontmatter_updated_is_never_a_finding(self, mkg, project):
        # Y-2: the mtime fallback is GONE. A node without frontmatter
        # `updated:` must not be flagged even when its stored
        # `updated_at` wildly disagrees with the file mtime (the churn
        # case: git checkout, branch switch, copy, touch).
        path = _write_node(
            project, "No Updated Node",
            ["title: No Updated Node", "type: concept"],
            "body",
        )
        stamp = 1760000000
        os.utime(path, (stamp, stamp))
        rows = [_row("uuid-nu", "No Updated Node",
                     "knowledge/No Updated Node.md",
                     updated_at="2020-01-01T00:00:00Z")]
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            assert mkg.check_stale_timestamps(rows) == []

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
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            assert mkg.check_stale_timestamps(rows) == []

    def test_row_with_missing_file_is_skipped(self, mkg, project):
        # A row whose stored file_path resolves to no file is the ORPHAN
        # check's finding, never a timestamp/link finding.
        _write_node(
            project, "Present Node",
            ["title: Present Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "body",
        )
        rows = [_row("uuid-x", "Ghost Node", "knowledge/Gone Node.md",
                     updated="2020-01-01T00:00:00Z")]
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            assert mkg.check_stale_timestamps(rows) == []


# ---------------------------------------------------------------------------
# Item 5 — missing cross-references
# ---------------------------------------------------------------------------

_LINK_BODY = "See [[Plain Target]] and [[uses::Typed Target]]."


class TestMissingLinks:
    def test_missing_reported_with_fix_properties_complete_left_alone(
            self, mkg, project):
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
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            found = mkg.check_missing_links(rows)
        assert len(found) == 1
        assert found[0]["uuid"] == "uuid-sp"
        assert "Plain Target" in found[0]["detail"]
        assert "uses::Typed Target" in found[0]["detail"]
        # The in-place fix writes the FILE-derived lists — exactly what
        # the sync writer would store.
        assert found[0]["properties"]["links"] == ["Plain Target"]
        assert found[0]["properties"]["typed_links"] == [
            {"relation_type": "uses", "target_title": "Typed Target"}]

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
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            found = mkg.check_missing_links(rows)
        assert len(found) == 1
        assert "Plain Target" in found[0]["detail"]

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
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            assert mkg.check_missing_links(rows) == []


# ---------------------------------------------------------------------------
# Y-1 — duplicate H1s: keyed by the row's stored file_path, never title
# ---------------------------------------------------------------------------

class TestDuplicateH1:
    """Two files share the H1 "Overview". Title-keying (the reworked-out
    design) compared BOTH rows against one file's parse and the fix step
    deleted both rows and re-synced one — the other node silently left
    Weaviate (the title-scoped delete sync_node's v0.2.70 FIX #1
    removed)."""

    def _setup(self, project):
        _write_node(
            project, "Overview", ["title: Overview", "type: concept",
                                  "updated: 2026-10-01T00:00:00Z"],
            "body", filename="alpha.md",
        )
        _write_node(
            project, "Overview", ["title: Overview", "type: concept",
                                  "updated: 2026-10-02T00:00:00Z"],
            "body", filename="beta.md",
        )
        # alpha's stored row is CURRENT for alpha but stale for beta;
        # beta's stored row is current for beta. Title-keying flagged
        # one of them wrongly no matter which file won the H1 map.
        return [
            _row("uuid-alpha", "Overview", "knowledge/alpha.md",
                 updated="2026-10-01T00:00:00Z", links=[], typed_links=[]),
            _row("uuid-beta", "Overview", "knowledge/beta.md",
                 updated="2026-10-02T00:00:00Z", links=[], typed_links=[]),
        ]

    def test_each_row_compared_with_the_file_at_its_own_path(
            self, mkg, project):
        rows = self._setup(project)
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            assert mkg.check_stale_timestamps(rows) == []
            assert mkg.check_missing_links(rows) == []

    def test_drift_on_one_file_never_touches_the_other_row(
            self, mkg, project):
        rows = self._setup(project)
        # alpha's file is edited after the sync; beta is untouched.
        alpha = project / "knowledge" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8").replace(
            "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z"),
            encoding="utf-8")
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            found = mkg.check_stale_timestamps(rows)
        assert [f["uuid"] for f in found] == ["uuid-alpha"]
        assert found[0]["properties"]["updated"].startswith("2026-10-03")

        col = _FakeCollection()
        server = _FakeServer(col)
        assert mkg.apply_property_updates(server, found) == 1
        assert col.deleted == [], "Y-1: maintain must never delete rows"
        assert col.updates == [("uuid-alpha",
                                {"updated": found[0]["properties"]["updated"]})]

    def test_fix_updates_both_duplicate_rows_in_place(self, mkg, project):
        rows = self._setup(project)
        for name, new_stamp in (("alpha", "2026-10-03T00:00:00Z"),
                                ("beta", "2026-10-04T00:00:00Z")):
            path = project / "knowledge" / f"{name}.md"
            path.write_text(path.read_text(encoding="utf-8").replace(
                "2026-10-0" + ("1" if name == "alpha" else "2")
                + "T00:00:00Z", new_stamp), encoding="utf-8")
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            found = mkg.check_stale_timestamps(rows)
        assert sorted(f["uuid"] for f in found) == ["uuid-alpha", "uuid-beta"]

        col = _FakeCollection()
        server = _FakeServer(col)
        assert mkg.apply_property_updates(server, found) == 2
        assert col.deleted == [], "Y-1: maintain must never delete rows"
        updated_by_uuid = dict(col.updates)
        assert set(updated_by_uuid) == {"uuid-alpha", "uuid-beta"}
        assert updated_by_uuid["uuid-alpha"]["updated"].startswith("2026-10-03")
        assert updated_by_uuid["uuid-beta"]["updated"].startswith("2026-10-04")


# ---------------------------------------------------------------------------
# Y-3 — per-row failure isolation (and no node can be lost)
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_one_failing_update_does_not_abort_the_rest(self, mkg, project):
        _write_node(
            project, "Stale Node",
            ["title: Stale Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "body",
        )
        _write_node(
            project, "Stale Node Two",
            ["title: Stale Node Two", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "body",
        )
        rows = [
            _row("uuid-a", "Stale Node", "knowledge/Stale Node.md",
                 updated="2026-09-01T00:00:00Z"),
            _row("uuid-b", "Stale Node Two", "knowledge/Stale Node Two.md",
                 updated="2026-09-01T00:00:00Z"),
        ]
        with mock.patch.object(mkg, "PROJECT_ROOT", project):
            found = mkg.check_stale_timestamps(rows)
        assert len(found) == 2

        col = _FakeCollection()
        real_update = col.data.update.side_effect

        def flaky_update(uuid, properties):
            if uuid == found[0]["uuid"]:
                raise RuntimeError("simulated backend hiccup")
            real_update(uuid=uuid, properties=properties)

        col.data.update.side_effect = flaky_update
        server = _FakeServer(col)
        assert mkg.apply_property_updates(server, found) == 1
        assert col.deleted == [], "no delete path exists at all"
        assert [uuid for uuid, _ in col.updates] == [found[1]["uuid"]]


# ---------------------------------------------------------------------------
# check_consistency integration — reporting, fixing, and write-free --check
# ---------------------------------------------------------------------------

def _mk_project_with_three_nodes(project: Path):
    """Stale Node (stored `updated` stale, links complete), Sparse Node
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
        assert col.updates == [], "--check must not update anything"
        assert fake_sync.synced == [], "--check must not sync anything"

    def test_fix_updates_in_place_exactly_the_flagged_rows(
            self, mkg, project):
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
        assert fake_sync.synced == [], \
            "items 4-5 must not go through the writer (no re-embed)"
        assert col.deleted == [], "Y-1: --fix must never delete rows"
        assert sorted(uuid for uuid, _ in col.updates) == [
            "uuid-sparse", "uuid-stale"]
        updated_by_uuid = dict(col.updates)
        assert updated_by_uuid["uuid-stale"] == {
            "updated": "2026-10-01T00:00:00+00:00"}
        assert updated_by_uuid["uuid-sparse"]["links"] == ["Plain Target"]
        assert updated_by_uuid["uuid-sparse"]["typed_links"] == [
            {"relation_type": "uses", "target_title": "Typed Target"}]
        after = {
            p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (project / "knowledge").glob("*.md")
        }
        assert after == before, "--fix must never rewrite the .md files"


# ---------------------------------------------------------------------------
# Item 6 (follow-up 2) — missing required frontmatter fields, report-only
# ---------------------------------------------------------------------------

_ALL_FIELDS = ["title: Complete Node", "type: concept", "tags: [a, b, c]",
               "created: 2026-10-01T00:00:00Z",
               "updated: 2026-10-01T00:00:00Z", "status: active"]


class TestMissingFrontmatterFields:
    def test_missing_field_reported_complete_node_not(self, mkg, project):
        _write_node(
            project, "Incomplete Node",
            ["title: Incomplete Node", "type: concept", "tags: [a, b, c]",
             "created: 2026-10-01T00:00:00Z",
             "updated: 2026-10-01T00:00:00Z"],
            "body",
        )
        _write_node(project, "Complete Node", _ALL_FIELDS, "body")
        with mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            found = mkg.check_missing_frontmatter_fields()
        assert set(found) == {"knowledge/Incomplete Node.md"}
        assert found["knowledge/Incomplete Node.md"] == ["status"], \
            "the exact missing field names must be reported"

    def test_no_frontmatter_block_reports_every_required_field(
            self, mkg, project):
        path = project / "knowledge" / "Bare Node.md"
        path.write_text("# Bare Node\n\nno frontmatter at all\n",
                        encoding="utf-8")
        with mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            found = mkg.check_missing_frontmatter_fields()
        assert set(found) == {"knowledge/Bare Node.md"}
        assert sorted(found["knowledge/Bare Node.md"]) == sorted(
            ("title", "type", "tags", "created", "updated", "status"))

    def test_nested_dialect_counts_as_declared(self, mkg, project):
        # The ONE parser promotes `metadata:` keys and `name:` → `title:`,
        # so a nested-dialect node is NOT missing fields.
        path = project / "knowledge" / "Nested Node.md"
        path.write_text(
            "---\n"
            "name: Nested Node\n"
            "metadata:\n"
            "  type: concept\n"
            "  tags: [a, b, c]\n"
            "  created: 2026-10-01T00:00:00Z\n"
            "  updated: 2026-10-01T00:00:00Z\n"
            "  status: active\n"
            "---\n\n# Nested Node\n",
            encoding="utf-8",
        )
        with mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            assert mkg.check_missing_frontmatter_fields() == {}

    def test_duplicate_h1_files_are_each_reported(self, mkg, project):
        # File-keyed, not title-keyed: two files sharing an H1 are both
        # judged, each on its own frontmatter.
        _write_node(
            project, "Overview",
            ["title: Overview", "type: concept", "tags: [a]"],
            "body", filename="alpha.md",
        )
        _write_node(
            project, "Overview",
            ["title: Overview", "type: concept", "tags: [a]",
             "created: 2026-10-01T00:00:00Z",
             "updated: 2026-10-01T00:00:00Z", "status: active"],
            "body", filename="beta.md",
        )
        with mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            found = mkg.check_missing_frontmatter_fields()
        assert set(found) == {"knowledge/alpha.md"}
        assert sorted(found["knowledge/alpha.md"]) == [
            "created", "status", "updated"]

    def test_ontology_meta_files_are_not_nodes(self, mkg, project):
        (project / "knowledge" / "VOCABULARY.md").write_text(
            "# Vocabulary\n\nnot a node\n", encoding="utf-8")
        (project / "knowledge" / "TAG_HIERARCHY.md").write_text(
            "# Tags\n\nnot a node\n", encoding="utf-8")
        with mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"):
            assert mkg.check_missing_frontmatter_fields() == {}

    def test_fix_reports_but_writes_nothing(self, mkg, project):
        _write_node(
            project, "Incomplete Node",
            ["title: Incomplete Node", "type: concept",
             "tags: [a, b, c]", "created: 2026-10-01T00:00:00Z",
             "updated: 2026-10-01T00:00:00Z"],
            "body",
        )
        before = {
            p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (project / "knowledge").glob("*.md")
        }
        rows = [_row("uuid-inc", "Incomplete Node",
                     "knowledge/Incomplete Node.md",
                     updated="2026-10-01T00:00:00Z", links=[], typed_links=[])]
        triples = [(u, t, r) for u, t, r, _ in rows]
        col = _FakeCollection()
        server = _FakeServer(col)
        fake_sync = _FakeSync()
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"), \
             mock.patch.object(mkg, "get_all_weaviate_objects",
                               return_value=triples), \
             mock.patch.object(mkg, "get_all_weaviate_rows",
                               return_value=rows), \
             mock.patch.object(_sync_module(), "sync_node", fake_sync):
            stats = mkg.check_consistency(server, fix=True, assume_yes=True)
        assert stats["missing_frontmatter_fields"] == 1
        assert stats["fixed"] == 0, "a frontmatter finding is never updated"
        assert col.deleted == [], "--fix must not delete anything for it"
        assert col.updates == [], "--fix must not update anything for it"
        assert fake_sync.synced == [], "--fix must not sync anything for it"
        after = {
            p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (project / "knowledge").glob("*.md")
        }
        assert after == before, "--fix must never rewrite the .md files"


# ---------------------------------------------------------------------------
# R4b follow-ups — Y4b-1 (a failed read is never a clean pass) and
# Y4b-3 (the "… and N more" count survives targets containing ", ")
# ---------------------------------------------------------------------------


class TestFailedFetchIsNotAPass:
    """Y4b-1: pre-fix both fetchers swallowed the fetch exception and
    returned [], so a dead Weaviate read printed "✓ No stale
    timestamps" / "✓ No missing cross-references" and — with no other
    findings — "✅ All checks passed" on a read that never happened."""

    @staticmethod
    def _run_check_main(mkg, project, collection):
        server = mock.MagicMock()
        server.client.collections.get.return_value = collection
        with mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"), \
             mock.patch.object(mkg, "EmbeddingService") as emb_cls, \
             mock.patch.object(mkg, "WeaviateMCPServer", return_value=server), \
             mock.patch.object(sys, "argv",
                               ["maintain_knowledge_graph.py", "--check"]):
            mkg.main()
            emb_cls.for_project.assert_called_once()

    @staticmethod
    def _empty_page_collection():
        collection = mock.MagicMock()
        empty_page = mock.MagicMock()
        empty_page.objects = []
        collection.query.fetch_objects.return_value = empty_page
        return collection

    def test_failed_fetch_exits_nonzero_and_never_says_passed(
            self, mkg, project, capsys):
        # THE ACT: a fetch failure must surface as a clear error + a
        # non-zero exit, and "All checks passed" must NOT be printed.
        collection = mock.MagicMock()
        collection.query.fetch_objects.side_effect = RuntimeError(
            "simulated dead backend")
        with pytest.raises(SystemExit) as exc:
            self._run_check_main(mkg, project, collection)
        assert exc.value.code not in (0, None), \
            "a failed Weaviate read must exit non-zero"
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "All checks passed" not in output
        assert "simulated dead backend" in output, \
            "the fetch failure must be reported as an error, not swallowed"

    def test_successful_empty_fetch_still_passes(self, mkg, project, capsys):
        # LEAVE-ALONE: an EMPTY collection is a legitimate clean pass —
        # the empty read must still print "All checks passed" and exit 0
        # (main() returns without SystemExit).
        self._run_check_main(mkg, project, self._empty_page_collection())
        captured = capsys.readouterr()
        assert "All checks passed" in captured.out


class TestMissingLinksDisplayCount:
    def test_comma_in_target_keeps_and_n_more_count_correct(
            self, mkg, project, capsys):
        # Y4b-3: pre-fix the report split finding["detail"] on ", " to
        # count the shown/hidden items, so a target like "Aaa, with comma"
        # was counted as TWO items and inflated the "… and N more" count.
        # 5 missing links (one containing ", ") → show 3 + "... and 2
        # more"; the pre-fix split counted 6 and printed "... and 3 more"
        # (and showed "Aaa" / "with comma" as two separate links).
        # Asserted ONLY on the cross-references section: the broken-links
        # section above prints its own "... and 2 more" (the targets do
        # not exist as files), which would mask the pre-fix behaviour.
        _write_node(
            project, "Comma Node",
            ["title: Comma Node", "type: concept",
             "updated: 2026-10-01T00:00:00Z"],
            "[[Alpha]] [[Beta]] [[Gamma]] [[Delta]] [[Aaa, with comma]]",
        )
        rows = [_row("uuid-c", "Comma Node", "knowledge/Comma Node.md",
                     links=[], typed_links=[])]
        triples = [(uuid, title, rel) for uuid, title, rel, _ in rows]
        col = _FakeCollection()
        server = _FakeServer(col)
        with mock.patch.object(mkg, "PROJECT_ROOT", project), \
             mock.patch.object(mkg, "KNOWLEDGE_ROOT", project / "knowledge"), \
             mock.patch.object(mkg, "get_all_weaviate_objects",
                               return_value=triples), \
             mock.patch.object(mkg, "get_all_weaviate_rows",
                               return_value=rows):
            stats = mkg.check_consistency(server, fix=False, assume_yes=False)
        assert stats["missing_links"] == 1
        section = capsys.readouterr().out.split(
            "Checking stored cross-references")[1]
        assert "[[Aaa, with comma]]" in section, \
            "the comma-bearing target is ONE link, shown whole"
        assert "... and 2 more" in section, \
            "5 missing links = 3 shown + 2 more, even when one target " \
            "contains ', ' (pre-fix split counted it as two and said " \
            "'... and 3 more')"
        assert "... and 3 more" not in section


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
