# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Audit A-022 (owner ruling): unregistering a project KEEPS its RL training
events, with the project name in the rows.

Migration 053 removes ``rl_events``' ``ON DELETE CASCADE`` foreign key to
``projects`` by editing the stored CREATE TABLE text (no table rebuild: the
table is tens of GB on a real install) and back-fills ``project_name``. These
tests run the shipped ``.sql`` files through the Python fixture runner — the
same files the Rust runner ``include_str!``s — on a POPULATED pre-053 DB, so
they see exactly what an upgrading launcher.db goes through. The Rust tests
(``migrations.rs`` ``migration_053_*``, ``projects.rs``
``rl_events_on_delete_tests``) cover the writer and the unregister path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.common.launcher_db_fixture import apply_migrations


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _fk_to_projects(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM pragma_foreign_key_list('rl_events') WHERE \"table\" = 'projects'"
    ).fetchone()[0]


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at) "
        "VALUES ('p1', 'Alpha', '/w/p1', 'base', 'alpha', 0, 0)"
    )
    rows = [
        ("p1", None, "t-unnamed"),
        ("p1", "writer-name", "t-named"),
        (None, "free-slug", "t-free"),
    ]
    for pid, name, task in rows:
        conn.execute(
            "INSERT INTO rl_events (event_type, ts, project_id, project_name, task_id, payload_json) "
            "VALUES ('retrieval', 1, ?, ?, ?, '{}')",
            (pid, name, task),
        )


def _row(conn: sqlite3.Connection, task: str) -> tuple:
    return conn.execute(
        "SELECT project_id, project_name FROM rl_events WHERE task_id = ?", (task,)
    ).fetchone()


def test_before_053_a_project_delete_cascades_into_rl_events(tmp_path: Path) -> None:
    """The defect, pinned: on a pre-053 schema the delete takes the events."""
    db = tmp_path / "launcher.db"
    apply_migrations(db, up_to=52)
    conn = _connect(db)
    _seed(conn)
    assert _fk_to_projects(conn) == 1
    conn.execute("DELETE FROM projects WHERE id = 'p1'")
    assert conn.execute("SELECT COUNT(*) FROM rl_events WHERE project_id = 'p1'").fetchone()[0] == 0


def test_053_keeps_the_events_named_after_a_project_delete(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    apply_migrations(db, up_to=52)
    seed_conn = _connect(db)
    _seed(seed_conn)
    seed_conn.close()

    assert apply_migrations(db) >= 1
    assert apply_migrations(db) == 0, "re-applying is a no-op"

    conn = _connect(db)
    assert _fk_to_projects(conn) == 0
    # Back-fill: the unnamed row gets the project's name; the writer's own
    # name and the free-tier row are left alone.
    assert _row(conn, "t-unnamed") == ("p1", "Alpha")
    assert _row(conn, "t-named") == ("p1", "writer-name")
    assert _row(conn, "t-free") == (None, "free-slug")

    conn.execute("DELETE FROM projects WHERE id = 'p1'")
    assert conn.execute("SELECT COUNT(*) FROM rl_events WHERE project_id = 'p1'").fetchone()[0] == 2
    assert _row(conn, "t-unnamed") == ("p1", "Alpha")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    indexes = sorted(
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'index' AND tbl_name = 'rl_events' "
            "AND name LIKE 'idx_%'"
        )
    )
    assert indexes == [
        "idx_rl_events_embedding_source_ts",
        "idx_rl_events_event_type_ts",
        "idx_rl_events_project_ts",
        "idx_rl_events_quarantined",
        "idx_rl_events_task_id",
    ]


def test_053_leaves_the_other_cascades_in_place(tmp_path: Path) -> None:
    db = tmp_path / "launcher.db"
    apply_migrations(db)
    conn = _connect(db)
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at) "
        "VALUES ('p1', 'Alpha', '/w/p1', 'base', 'alpha', 0, 0)"
    )
    conn.execute(
        "INSERT INTO code_graph_builds (project_id, status, started_at) VALUES ('p1', 'pending', 1)"
    )
    conn.execute("DELETE FROM projects WHERE id = 'p1'")
    assert conn.execute("SELECT COUNT(*) FROM code_graph_builds").fetchone()[0] == 0
