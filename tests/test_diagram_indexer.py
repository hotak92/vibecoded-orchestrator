# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for vco_lib.diagram_indexer.

Covers:
  - parse_mermaid: title-frontmatter parsing, kind detection, edge/node
    counts, malformed input fallback.
  - parse_excalidraw: scene name, text labels, element counts, malformed
    JSON tolerance.
  - humanize_filename: kebab/snake/camel, empties.
  - index_diagram: round-trip (sidecar == DB content_text), idempotency
    (same input → same row, updated_at advanced but no spurious change),
    missing-frontmatter title fallback, malformed-content tolerance,
    all Mermaid kinds detected.
  - retry-table fallback: Weaviate failure enqueues a retry row even
    when the table didn't exist beforehand.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from vco_lib.diagram_indexer import (
    DiagramRow,
    ExcalidrawMetadata,
    MermaidMetadata,
    _MERMAID_KINDS,
    _validate_scoped_path,
    _upsert_row,
    humanize_filename,
    index_diagram,
    parse_excalidraw,
    parse_mermaid,
)


# ---------------------------------------------------------------------------
# humanize_filename
# ---------------------------------------------------------------------------


class TestHumanizeFilename:
    def test_kebab_case(self):
        assert humanize_filename("auth-flow-v2") == "Auth Flow V2"

    def test_snake_case(self):
        assert humanize_filename("auth_flow_v2") == "Auth Flow V2"

    def test_camel_case(self):
        assert humanize_filename("AuthFlowV2") == "Auth Flow V2"

    def test_single_word(self):
        assert humanize_filename("login") == "Login"

    def test_empty_string(self):
        assert humanize_filename("") == ""

    def test_leading_separator_collapsed(self):
        # Leading separator produces an empty token; should be dropped.
        assert humanize_filename("-foo-bar") == "Foo Bar"

    def test_consecutive_separators(self):
        assert humanize_filename("foo--bar__baz") == "Foo Bar Baz"

    def test_numbers_preserved(self):
        assert humanize_filename("v2-flow") == "V2 Flow"


# ---------------------------------------------------------------------------
# parse_mermaid
# ---------------------------------------------------------------------------


class TestParseMermaid:
    def test_frontmatter_title(self):
        src = "---\ntitle: My Auth Flow\n---\nflowchart TD\n  A --> B"
        md = parse_mermaid(src)
        assert md.title == "My Auth Flow"
        assert md.diagram_kind == "flowchart"
        # 2 nodes (A, B), 1 edge (-->)
        assert md.node_count == 0  # A/B have no shapes — heuristic returns 0
        assert md.edge_count == 1
        assert md.content_text == src

    def test_frontmatter_title_with_extra_yaml(self):
        src = (
            "---\n"
            "title: GUI Flow\n"
            "config:\n"
            "  theme: dark\n"
            "---\n"
            "flowchart LR\n"
            "  Start[Start] --> Done[Done]"
        )
        md = parse_mermaid(src)
        assert md.title == "GUI Flow"
        assert md.diagram_kind == "flowchart"
        assert md.node_count == 2  # Start, Done both have [shape]
        assert md.edge_count == 1

    def test_no_frontmatter_no_title(self):
        src = "flowchart TD\n  A[Login] --> B[Dashboard]"
        md = parse_mermaid(src)
        assert md.title is None
        assert md.diagram_kind == "flowchart"
        assert md.node_count == 2
        assert md.edge_count == 1

    def test_comments_skipped_for_kind_detection(self):
        src = "%% this is a comment\n\nclassDiagram\n  class User"
        md = parse_mermaid(src)
        assert md.diagram_kind == "classDiagram"

    @pytest.mark.parametrize(
        "kind",
        [
            "flowchart",
            "classDiagram",
            "sequenceDiagram",
            "stateDiagram",
            "erDiagram",
            "gantt",
            "pie",
            "journey",
            "mindmap",
            "timeline",
            "gitGraph",
        ],
    )
    def test_all_kinds_detected(self, kind):
        # Append a minimal trailing token so the kind isn't bare.
        src = f"{kind} TD\n  X --> Y"
        md = parse_mermaid(src)
        assert md.diagram_kind == kind, (
            f"Failed to detect kind '{kind}'; got {md.diagram_kind!r}"
        )

    def test_edge_variants_counted(self):
        # All edge variants in one source.
        src = (
            "flowchart TD\n"
            "  A --> B\n"
            "  B === C\n"
            "  C --- D\n"
            "  D ==> E\n"
            "  E -.-> F\n"
        )
        md = parse_mermaid(src)
        assert md.edge_count == 5

    def test_unique_node_counting(self):
        # A appears twice; should count as 1 unique node.
        src = "flowchart TD\n  A[a] --> B(b)\n  A[a] --> C{c}"
        md = parse_mermaid(src)
        # A, B, C → 3 unique node IDs with shapes.
        assert md.node_count == 3

    def test_malformed_body_safe(self):
        # Garbage input — parser returns metadata, doesn't raise.
        src = "not a real mermaid file at all"
        md = parse_mermaid(src)
        assert md.title is None
        assert md.diagram_kind is None
        assert md.node_count == 0
        assert md.edge_count == 0
        assert md.content_text == src

    def test_kinds_list_has_no_dups(self):
        assert len(_MERMAID_KINDS) == len(set(_MERMAID_KINDS))


# ---------------------------------------------------------------------------
# parse_excalidraw
# ---------------------------------------------------------------------------


class TestParseExcalidraw:
    def test_basic_scene(self):
        scene = {
            "type": "excalidraw",
            "version": 2,
            "appState": {"name": "Auth Sketch"},
            "elements": [
                {"type": "rectangle"},
                {"type": "text", "text": "Login"},
                {"type": "text", "text": "Submit"},
                {"type": "arrow"},
                {"type": "arrow"},
            ],
        }
        ed = parse_excalidraw(scene)
        assert ed.scene_name == "Auth Sketch"
        assert ed.text_labels == ["Login", "Submit"]
        assert ed.element_counts == {
            "rectangle": 1,
            "text": 2,
            "arrow": 2,
        }
        assert ed.content_text == "Login\nSubmit"

    def test_missing_appstate(self):
        scene = {"elements": [{"type": "text", "text": "Hello"}]}
        ed = parse_excalidraw(scene)
        assert ed.scene_name is None
        assert ed.text_labels == ["Hello"]

    def test_missing_elements(self):
        scene = {"appState": {"name": "Empty"}}
        ed = parse_excalidraw(scene)
        assert ed.scene_name == "Empty"
        assert ed.text_labels == []
        assert ed.element_counts == {}

    def test_non_dict_input(self):
        ed = parse_excalidraw([])  # type: ignore[arg-type]
        assert ed.scene_name is None
        assert ed.text_labels == []

    def test_text_originaltext_fallback(self):
        scene = {
            "elements": [
                {"type": "text", "originalText": "Original"},
            ]
        }
        ed = parse_excalidraw(scene)
        assert ed.text_labels == ["Original"]

    def test_empty_text_skipped(self):
        scene = {
            "elements": [
                {"type": "text", "text": ""},
                {"type": "text", "text": "   "},  # whitespace-only
                {"type": "text", "text": "Real"},
            ]
        }
        ed = parse_excalidraw(scene)
        assert ed.text_labels == ["Real"]

    def test_non_dict_element_skipped(self):
        scene = {
            "elements": [
                "not a dict",
                {"type": "rectangle"},
                None,
            ]
        }
        ed = parse_excalidraw(scene)
        assert ed.element_counts == {"rectangle": 1}


# ---------------------------------------------------------------------------
# _validate_scoped_path — delegates to vco_lib.diagram_paths (1.2 canonical).
# Error-message regexes match 1.2's wording (see vco_lib/diagram_paths.py).
# ---------------------------------------------------------------------------


class TestScopedPathValidator:
    def test_valid_mermaid(self):
        p = Path(".claude/diagrams/gui/auth/login-form.mmd")
        dtype, cat, name = _validate_scoped_path(p)
        assert dtype == "mermaid"
        assert cat == "gui/auth"
        assert name == "login-form"

    def test_valid_excalidraw(self):
        p = Path(".claude/diagrams/architecture/data-flow.excalidraw")
        dtype, cat, name = _validate_scoped_path(p)
        assert dtype == "excalidraw"
        assert cat == "architecture"
        assert name == "data-flow"

    def test_flat_rejected(self):
        p = Path(".claude/diagrams/flat.mmd")
        with pytest.raises(ValueError, match="flat"):
            _validate_scoped_path(p)

    def test_traversal_rejected(self):
        p = Path(".claude/diagrams/../../secrets.mmd")
        with pytest.raises(ValueError):
            _validate_scoped_path(p)

    def test_no_anchor_rejected(self):
        p = Path("/tmp/diagrams/flat.mmd")
        with pytest.raises(ValueError, match="\\.claude/diagrams"):
            _validate_scoped_path(p)

    def test_bad_extension_rejected(self):
        p = Path(".claude/diagrams/gui/auth/login.txt")
        with pytest.raises(ValueError, match="extension|\\.mmd|\\.excalidraw"):
            _validate_scoped_path(p)

    def test_camelcase_name_rejected(self):
        p = Path(".claude/diagrams/gui/LoginForm.mmd")
        with pytest.raises(ValueError, match="kebab|lowercase"):
            _validate_scoped_path(p)

    def test_underscore_name_rejected(self):
        p = Path(".claude/diagrams/gui/login_form.mmd")
        with pytest.raises(ValueError, match="kebab|lowercase"):
            _validate_scoped_path(p)


# ---------------------------------------------------------------------------
# DB schema fixture (mirrors Phase 1.1's project_diagrams table)
# ---------------------------------------------------------------------------


_PROJECT_DIAGRAMS_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE project_diagrams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    diagram_name TEXT NOT NULL,
    diagram_type TEXT NOT NULL CHECK(diagram_type IN ('mermaid','excalidraw')),
    file_path TEXT NOT NULL,
    category_path TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    inferred_title TEXT,
    diagram_kind TEXT,
    content_text TEXT,
    node_count INTEGER,
    edge_count INTEGER,
    chat_id TEXT,
    linked_session_summary TEXT,
    config_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(project_id, diagram_name)
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a fresh SQLite DB with the project_diagrams schema."""
    p = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(p))
    try:
        conn.executescript(_PROJECT_DIAGRAMS_SCHEMA)
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-test-uuid", "TestProject"),
        )
        conn.commit()
    finally:
        conn.close()
    return p


@pytest.fixture
def diagrams_root(tmp_path: Path) -> Path:
    """Create a `.claude/diagrams/` root inside tmp_path."""
    root = tmp_path / ".claude" / "diagrams"
    root.mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# index_diagram — end-to-end
# ---------------------------------------------------------------------------


class TestIndexDiagram:
    def test_mermaid_with_frontmatter(self, db_path: Path, diagrams_root: Path):
        cat = diagrams_root / "gui" / "auth"
        cat.mkdir(parents=True)
        f = cat / "login-form.mmd"
        f.write_text(
            "---\n"
            "title: Login Form\n"
            "---\n"
            "flowchart TD\n"
            "  Start[Start] --> Submit[Submit]\n"
        )

        row = index_diagram(
            f,
            project_id="proj-test-uuid",
            chat_id="chat-123",
            db_path=db_path,
            diagrams_collection=None,  # skip Weaviate
        )

        assert row.id is not None
        assert row.diagram_type == "mermaid"
        assert row.diagram_name == "login-form"
        assert row.category_path == "gui/auth"
        assert row.inferred_title == "Login Form"
        assert row.diagram_kind == "flowchart"
        assert row.node_count == 2
        assert row.edge_count == 1
        assert row.chat_id == "chat-123"
        assert row.content_text and "flowchart TD" in row.content_text

        # Sidecar exists and matches DB content_text round-trip.
        sidecar = f.with_suffix(".mmd.meta.json")
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["content_text"] == row.content_text
        assert data["inferred_title"] == row.inferred_title
        assert data["category_path"] == "gui/auth"
        # Sidecar excludes the SQLite rowid (travels across machines).
        assert "id" not in data
        assert data["_sidecar_schema_version"] == 1

    def test_mermaid_no_frontmatter_falls_back_to_filename(
        self, db_path: Path, diagrams_root: Path
    ):
        cat = diagrams_root / "architecture"
        cat.mkdir(parents=True)
        f = cat / "auth-flow-v2.mmd"
        f.write_text("classDiagram\n  class User\n  class Token")

        row = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )

        # Title fell back to humanise(stem).
        assert row.inferred_title == "Auth Flow V2"
        assert row.diagram_kind == "classDiagram"
        assert row.chat_id is None

    def test_excalidraw_indexed(self, db_path: Path, diagrams_root: Path):
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "wireframe.excalidraw"
        scene = {
            "type": "excalidraw",
            "version": 2,
            "appState": {"name": "Dashboard Wireframe"},
            "elements": [
                {"type": "rectangle"},
                {"type": "text", "text": "Header"},
                {"type": "arrow"},
            ],
        }
        f.write_text(json.dumps(scene))

        row = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )

        assert row.diagram_type == "excalidraw"
        assert row.inferred_title == "Dashboard Wireframe"
        assert row.diagram_kind == "excalidraw"
        # node_count = total element count, edge_count = arrow + line
        assert row.node_count == 3
        assert row.edge_count == 1
        assert row.content_text == "Header"

    def test_malformed_excalidraw_still_indexes(
        self, db_path: Path, diagrams_root: Path
    ):
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "broken.excalidraw"
        f.write_text("not valid JSON at all")

        row = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )

        # Indexer doesn't crash on broken content; defaults to filename
        # for title and empty metadata otherwise.
        assert row.diagram_type == "excalidraw"
        assert row.inferred_title == "Broken"
        assert row.diagram_kind == "excalidraw"
        assert row.node_count == 0

    def test_idempotency_same_input_no_spurious_change(
        self, db_path: Path, diagrams_root: Path
    ):
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "stable.mmd"
        f.write_text("flowchart TD\n  A[A] --> B[B]")

        row1 = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )
        original_created = row1.created_at

        # Sleep enough for the updated_at int to advance.
        time.sleep(1.1)

        row2 = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )

        # Same rowid (UPSERT updated in place).
        assert row1.id == row2.id
        # created_at preserved across upsert.
        assert row2.created_at == original_created
        # updated_at advanced.
        assert row2.updated_at >= row1.updated_at
        # All derived metadata identical.
        assert row1.inferred_title == row2.inferred_title
        assert row1.diagram_kind == row2.diagram_kind
        assert row1.node_count == row2.node_count

    def test_round_trip_mutate_reindex(
        self, db_path: Path, diagrams_root: Path
    ):
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "mut.mmd"
        f.write_text("flowchart TD\n  A --> B")

        row1 = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )
        assert row1.edge_count == 1

        # Mutate file: add another edge.
        f.write_text("flowchart TD\n  A --> B\n  B --> C")
        time.sleep(1.1)
        row2 = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )

        assert row2.edge_count == 2
        # Restoration (snapshot revert): write original back.
        f.write_text("flowchart TD\n  A --> B")
        time.sleep(1.1)
        row3 = index_diagram(
            f,
            project_id="proj-test-uuid",
            db_path=db_path,
            diagrams_collection=None,
        )
        assert row3.edge_count == 1
        # Sidecar tracks the latest content_text.
        sidecar = f.with_suffix(".mmd.meta.json")
        data = json.loads(sidecar.read_text())
        assert data["edge_count"] == 1

    def test_chat_id_null_preserved_on_update(
        self, db_path: Path, diagrams_root: Path
    ):
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "chatful.mmd"
        f.write_text("flowchart TD\n  A --> B")

        # First save WITH chat_id.
        row1 = index_diagram(
            f, project_id="proj-test-uuid",
            chat_id="chat-original", db_path=db_path,
            diagrams_collection=None,
        )
        assert row1.chat_id == "chat-original"

        # Subsequent save WITHOUT chat_id (e.g. manual user edit) —
        # the original chat_id should be preserved (COALESCE in UPSERT).
        row2 = index_diagram(
            f, project_id="proj-test-uuid",
            chat_id=None, db_path=db_path,
            diagrams_collection=None,
        )
        assert row2.chat_id == "chat-original"

    def test_missing_file_raises(self, db_path: Path, diagrams_root: Path):
        f = diagrams_root / "gui" / "nonexistent.mmd"
        with pytest.raises(FileNotFoundError):
            index_diagram(
                f,
                project_id="proj-test-uuid",
                db_path=db_path,
                diagrams_collection=None,
            )

    def test_invalid_path_raises(self, db_path: Path, tmp_path: Path):
        # File OUTSIDE .claude/diagrams/ — validator rejects.
        f = tmp_path / "outside.mmd"
        f.write_text("flowchart TD\n  A --> B")
        with pytest.raises(ValueError, match="\\.claude/diagrams"):
            index_diagram(
                f,
                project_id="proj-test-uuid",
                db_path=db_path,
                diagrams_collection=None,
            )

    def test_atomic_sidecar_write(self, db_path: Path, diagrams_root: Path):
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "atomic.mmd"
        f.write_text("flowchart TD\n  A --> B")

        index_diagram(
            f, project_id="proj-test-uuid",
            db_path=db_path, diagrams_collection=None,
        )

        sidecar = f.with_suffix(".mmd.meta.json")
        assert sidecar.exists()
        # No leftover tempfiles in the directory.
        siblings = list(cat.iterdir())
        tmp_leftovers = [p for p in siblings if p.name.startswith(".meta.")]
        assert tmp_leftovers == [], f"Leftover tempfiles: {tmp_leftovers}"


# ---------------------------------------------------------------------------
# Retry table fallback
# ---------------------------------------------------------------------------


class TestRetryTableEnqueue:
    def test_weaviate_failure_enqueues_retry(
        self, db_path: Path, diagrams_root: Path
    ):
        """When Weaviate upsert fails, the retry table is created on
        demand (if missing) and a row is enqueued — but the index call
        still succeeds and returns the row."""
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "fail.mmd"
        f.write_text("flowchart TD\n  A --> B")

        # Confirm retry table doesn't exist yet.
        conn = sqlite3.connect(str(db_path))
        existing = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='diagram_index_retry'"
        ).fetchone()
        conn.close()
        assert existing is None

        # Patch _weaviate_upsert to raise.
        with patch(
            "vco_lib.diagram_indexer._weaviate_upsert",
            side_effect=RuntimeError("simulated Weaviate down"),
        ):
            row = index_diagram(
                f,
                project_id="proj-test-uuid",
                db_path=db_path,
                diagrams_collection="MyProj_Diagrams",
            )

        # Indexer returned successfully.
        assert row.id is not None
        assert row.diagram_name == "fail"

        # Retry table now exists with one row.
        conn = sqlite3.connect(str(db_path))
        retries = conn.execute(
            "SELECT project_id, file_path, error FROM diagram_index_retry"
        ).fetchall()
        conn.close()
        assert len(retries) == 1
        assert retries[0][0] == "proj-test-uuid"
        assert "simulated" in retries[0][2]


# ---------------------------------------------------------------------------
# DiagramRow → sidecar_dict
# ---------------------------------------------------------------------------


class TestSidecarDict:
    def test_excludes_id_includes_schema_version(self):
        row = DiagramRow(
            id=42,
            project_id="proj",
            diagram_name="name",
            diagram_type="mermaid",
            file_path="/abs/path.mmd",
            category_path="gui",
            enabled=1,
            inferred_title="Title",
            diagram_kind="flowchart",
            content_text="src",
            node_count=2,
            edge_count=1,
            chat_id=None,
            linked_session_summary=None,
            config_json=None,
            created_at=1000,
            updated_at=2000,
        )
        d = row.to_sidecar_dict()
        assert "id" not in d
        assert d["_sidecar_schema_version"] == 1
        assert d["diagram_name"] == "name"
        assert d["created_at"] == 1000


# ---------------------------------------------------------------------------
# snapshot_diagram_file + `snapshot create` CLI subcommand (A6 wire-up)
# ---------------------------------------------------------------------------
#
# The DB schema fixture above (`_PROJECT_DIAGRAMS_SCHEMA`) doesn't include
# diagram_snapshots — we extend it locally here so the snapshot tests
# don't depend on migration 022 being applied. Mirrors the
# launcher-core SQL in `launcher/src-tauri/vct-launcher-core/src/db/
# migrations/022_diagrams.sql` byte-for-byte.

_SNAPSHOTS_SCHEMA = """
CREATE TABLE diagram_snapshots (
    id              INTEGER PRIMARY KEY,
    diagram_id      INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    content         BLOB NOT NULL,
    created_at      INTEGER NOT NULL,
    trigger         TEXT NOT NULL,
    label           TEXT,
    UNIQUE(diagram_id, content_hash)
);
"""


def _add_snapshots_table(db_path: Path) -> None:
    """Apply the diagram_snapshots schema fragment to an existing DB."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SNAPSHOTS_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _insert_diagram_row(
    db_path: Path,
    project_id: str,
    diagram_name: str,
    file_path: Path,
    *,
    category_path: str = "gui",
    diagram_type: str = "mermaid",
) -> int:
    """Insert a minimal project_diagrams row (bypassing the indexer)
    and return its rowid. Used by snapshot tests that need a
    pre-existing diagram row to point at."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO project_diagrams "
            "(project_id, diagram_name, diagram_type, file_path, "
            " category_path, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 1000, 1000)",
            (
                project_id,
                diagram_name,
                diagram_type,
                str(file_path.resolve()),
                category_path,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _count_snapshots(db_path: Path, diagram_id: int) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM diagram_snapshots WHERE diagram_id = ?",
            (diagram_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _read_snapshot(db_path: Path, snapshot_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT diagram_id, content_hash, content, trigger, label "
            "FROM diagram_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    return {
        "diagram_id": row[0],
        "content_hash": row[1],
        "content": row[2],
        "trigger": row[3],
        "label": row[4],
    }


class TestSnapshotDiagramFile:
    def test_creates_snapshot_for_known_diagram(
        self, db_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import (
            snapshot_diagram_file,
            _sha256_bytes,
        )

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "snap-test.mmd"
        f.write_text("flowchart TD\n  A --> B")

        diagram_id = _insert_diagram_row(
            db_path, "proj-test-uuid", "snap-test", f,
        )

        snapshot_id = snapshot_diagram_file(
            f, "proj-test-uuid", db_path=db_path,
        )
        assert snapshot_id is not None
        assert _count_snapshots(db_path, diagram_id) == 1

        snap = _read_snapshot(db_path, snapshot_id)
        assert snap["diagram_id"] == diagram_id
        assert snap["trigger"] == "auto_pre_edit_save"
        assert snap["label"] is None
        # content stored as raw bytes; hash matches.
        expected_hash = _sha256_bytes(f.read_bytes())
        assert snap["content_hash"] == expected_hash
        assert bytes(snap["content"]) == f.read_bytes()

    def test_dedup_skips_when_latest_hash_matches(
        self, db_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import snapshot_diagram_file

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "dedup.mmd"
        f.write_text("flowchart TD\n  A --> B")

        diagram_id = _insert_diagram_row(
            db_path, "proj-test-uuid", "dedup", f,
        )

        snapshot_id_1 = snapshot_diagram_file(
            f, "proj-test-uuid", db_path=db_path,
        )
        assert snapshot_id_1 is not None
        # Second call with unchanged content → no-op (returns None).
        snapshot_id_2 = snapshot_diagram_file(
            f, "proj-test-uuid", db_path=db_path,
        )
        assert snapshot_id_2 is None
        assert _count_snapshots(db_path, diagram_id) == 1

    def test_dedup_does_not_block_changed_content(
        self, db_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import snapshot_diagram_file

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "changing.mmd"
        f.write_text("flowchart TD\n  A --> B")

        diagram_id = _insert_diagram_row(
            db_path, "proj-test-uuid", "changing", f,
        )

        s1 = snapshot_diagram_file(f, "proj-test-uuid", db_path=db_path)
        assert s1 is not None

        # Change the file → second snapshot must land (different hash).
        f.write_text("flowchart TD\n  A --> C")
        s2 = snapshot_diagram_file(f, "proj-test-uuid", db_path=db_path)
        assert s2 is not None
        assert s2 != s1
        assert _count_snapshots(db_path, diagram_id) == 2

    def test_no_diagram_row_returns_none(
        self, db_path: Path, diagrams_root: Path
    ):
        """If the indexer hasn't UPSERTed a project_diagrams row yet
        (e.g. snapshot fires before indexer in a hook race), we soft-
        skip rather than create a dangling snapshot."""
        from vco_lib.diagram_indexer import snapshot_diagram_file

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "orphan.mmd"
        f.write_text("flowchart TD\n  A --> B")

        # No INSERT into project_diagrams.
        result = snapshot_diagram_file(
            f, "proj-test-uuid", db_path=db_path,
        )
        assert result is None

    def test_missing_db_returns_none(
        self, tmp_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import snapshot_diagram_file

        f = diagrams_root / "gui" / "nodbskip.mmd"
        f.parent.mkdir(parents=True)
        f.write_text("flowchart TD\n  A --> B")

        nonexistent = tmp_path / "definitely-not-here.db"
        result = snapshot_diagram_file(
            f, "proj-test-uuid", db_path=nonexistent,
        )
        assert result is None

    def test_missing_file_returns_none(
        self, db_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import snapshot_diagram_file

        _add_snapshots_table(db_path)
        ghost = diagrams_root / "gui" / "ghost.mmd"
        # Don't write the file.
        result = snapshot_diagram_file(
            ghost, "proj-test-uuid", db_path=db_path,
        )
        assert result is None

    def test_custom_trigger_and_label_recorded(
        self, db_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import snapshot_diagram_file

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "labelled.mmd"
        f.write_text("flowchart TD\n  A --> B")
        diagram_id = _insert_diagram_row(
            db_path, "proj-test-uuid", "labelled", f,
        )
        snapshot_id = snapshot_diagram_file(
            f, "proj-test-uuid", db_path=db_path,
            trigger="manual", label="pre-refactor",
        )
        assert snapshot_id is not None
        snap = _read_snapshot(db_path, snapshot_id)
        assert snap["trigger"] == "manual"
        assert snap["label"] == "pre-refactor"
        # Sanity: the row points at the right diagram.
        assert snap["diagram_id"] == diagram_id


class TestSnapshotCli:
    """Exercise the `snapshot create` CLI subcommand against an
    in-process call (not subprocess) so we keep the test cheap and
    deterministic. End-to-end subprocess coverage is provided by the
    post-file-edit hook test (test_post_file_edit_diagrams_branch.py)."""

    def test_cli_creates_snapshot(self, db_path: Path, diagrams_root: Path):
        from vco_lib.diagram_indexer import _cli

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "cli-snap.mmd"
        f.write_text("flowchart TD\n  A --> B")
        diagram_id = _insert_diagram_row(
            db_path, "proj-test-uuid", "cli-snap", f,
        )

        rc = _cli([
            "snapshot", "create", str(f),
            "--project-id", "proj-test-uuid",
            "--db-path", str(db_path),
            "--quiet",
        ])
        assert rc == 0
        assert _count_snapshots(db_path, diagram_id) == 1

    def test_cli_dedup_returns_zero_and_no_extra_row(
        self, db_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import _cli

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "cli-dedup.mmd"
        f.write_text("flowchart TD\n  A --> B")
        diagram_id = _insert_diagram_row(
            db_path, "proj-test-uuid", "cli-dedup", f,
        )

        argv = [
            "snapshot", "create", str(f),
            "--project-id", "proj-test-uuid",
            "--db-path", str(db_path),
            "--quiet",
        ]
        # First call: insert. Second call: dedup no-op.
        assert _cli(argv) == 0
        assert _cli(argv) == 0
        assert _count_snapshots(db_path, diagram_id) == 1

    def test_cli_missing_project_id_returns_2(
        self, db_path: Path, diagrams_root: Path, monkeypatch
    ):
        """When neither --project-id nor CWD-resolved project_id is
        available, the CLI returns 2 (the unrecoverable code reserved
        for project-id resolution failure)."""
        from vco_lib.diagram_indexer import _cli

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "noproj.mmd"
        f.write_text("flowchart TD\n  A --> B")

        # Force the CWD resolver to return None.
        monkeypatch.setattr(
            "vco_lib.diagram_indexer._resolve_project_id_from_cwd",
            lambda: None,
        )
        rc = _cli([
            "snapshot", "create", str(f),
            "--db-path", str(db_path),
            "--quiet",
        ])
        assert rc == 2

    def test_cli_respects_custom_trigger(
        self, db_path: Path, diagrams_root: Path
    ):
        from vco_lib.diagram_indexer import _cli

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "trig.mmd"
        f.write_text("flowchart TD\n  A --> B")
        _ = _insert_diagram_row(
            db_path, "proj-test-uuid", "trig", f,
        )
        rc = _cli([
            "snapshot", "create", str(f),
            "--project-id", "proj-test-uuid",
            "--db-path", str(db_path),
            "--trigger", "auto_interval",
            "--label", "interval-keepalive",
            "--quiet",
        ])
        assert rc == 0

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT trigger, label FROM diagram_snapshots "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("auto_interval", "interval-keepalive")

    def test_cli_rejects_invalid_trigger(
        self, db_path: Path, diagrams_root: Path
    ):
        """argparse `choices=` should refuse an unknown trigger so we
        never accidentally write garbage that the launcher's snapshot
        UI filter dropdown won't recognise."""
        from vco_lib.diagram_indexer import _cli

        _add_snapshots_table(db_path)
        cat = diagrams_root / "gui"
        cat.mkdir(parents=True)
        f = cat / "bad-trig.mmd"
        f.write_text("flowchart TD\n  A --> B")
        _insert_diagram_row(
            db_path, "proj-test-uuid", "bad-trig", f,
        )
        with pytest.raises(SystemExit):
            _cli([
                "snapshot", "create", str(f),
                "--project-id", "proj-test-uuid",
                "--db-path", str(db_path),
                "--trigger", "definitely-not-allowed",
                "--quiet",
            ])
