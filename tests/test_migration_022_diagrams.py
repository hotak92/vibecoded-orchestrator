# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Migration 022 (diagrams) schema-level tests.

The launcher's migrations live in Rust and are not callable directly
from Python — the `vct-launcher-core` crate owns `apply()`. The same
pattern as `test_install_self_heal_kg_bindings.py` is used here: hand-
roll the prerequisite tables in Python's `sqlite3`, exec the migration
SQL string, and assert the resulting schema matches the plan.

This protects the on-disk SQL file itself (a typo in the CHECK constraint
or a missing UNIQUE would surface here before the Rust test layer catches
it during `cargo test`). It also gives Python-only CI lanes coverage of
the diagrams schema without a Rust toolchain.

Covered:

* All 5 tables exist with the columns the plan demands.
* CHECK constraints fire on bad enum values.
* UNIQUE constraints fire on duplicates.
* Cascade delete works end-to-end across all FK-bearing rows.
* `idx_diagrams_chat` is a partial index (NULL `chat_id` rows excluded).
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "migrations"
    / "022_diagrams.sql"
)


# Minimal `projects` table — just enough to satisfy the FK references the
# migration creates. The launcher's real `projects` schema (migration 001)
# carries far more columns; we re-derive only what's needed.
_PROJECTS_PREREQ_SQL = """
CREATE TABLE projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    folder_path  TEXT NOT NULL UNIQUE,
    host         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    """Open an in-memory SQLite DB with FK enforcement enabled, then
    apply the projects prerequisite + migration 022.
    """
    if not MIGRATION_PATH.exists():
        raise FileNotFoundError(
            f"migration file not found at {MIGRATION_PATH} — did the path "
            "change? (test root: {REPO_ROOT})"
        )

    sql = MIGRATION_PATH.read_text(encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_PROJECTS_PREREQ_SQL)
    conn.executescript(sql)
    return conn


def _seed_project(conn: sqlite3.Connection, pid: str, name: str) -> None:
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, name, f"/tmp/{pid}", "base", pid, 1, 1),
    )


class TestMigration022Diagrams(unittest.TestCase):
    """Schema + constraint smoke tests for migration 022."""

    # ─── table existence ────────────────────────────────────────────────

    def test_all_five_tables_exist(self) -> None:
        conn = _conn()
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for expected in (
            "project_diagrams",
            "diagram_snapshots",
            "diagram_access",
            "project_mcp_tool_grants",
            "project_modules",
        ):
            self.assertIn(expected, names, f"table {expected} missing")

    def test_indexes_created(self) -> None:
        conn = _conn()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        for expected in (
            "idx_diagrams_category",
            "idx_diagrams_chat",
            "idx_diagrams_kind",
            "idx_snapshots_diagram",
            "idx_tool_grants_lookup",
        ):
            self.assertIn(expected, indexes, f"index {expected} missing")

    def test_project_diagrams_has_extended_metadata_columns(self) -> None:
        """Phase 1.5 §1.5.2 extended schema — supersedes the Phase 1
        sketch. All derived-metadata columns must be present.
        """
        conn = _conn()
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(project_diagrams)")
        }
        expected = {
            "id",
            "project_id",
            "diagram_name",
            "diagram_type",
            "file_path",
            "category_path",
            "enabled",
            "inferred_title",
            "diagram_kind",
            "content_text",
            "node_count",
            "edge_count",
            "chat_id",
            "linked_session_summary",
            "config_json",
            "created_at",
            "updated_at",
        }
        missing = expected - cols
        self.assertFalse(missing, f"missing project_diagrams columns: {missing}")

    # ─── CHECK constraints ──────────────────────────────────────────────

    def test_diagram_type_check_rejects_unknown_kind(self) -> None:
        conn = _conn()
        _seed_project(conn, "p1", "Acme")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            conn.execute(
                "INSERT INTO project_diagrams "
                "(project_id, diagram_name, diagram_type, file_path, "
                "category_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("p1", "x", "drawio", ".claude/d/g/x.drawio", "g", 1, 1),
            )
        self.assertIn("CHECK", str(ctx.exception).upper())

    def test_diagram_type_check_accepts_mermaid_and_excalidraw(self) -> None:
        conn = _conn()
        _seed_project(conn, "p1", "Acme")
        for t, name in (("mermaid", "m1"), ("excalidraw", "e1")):
            conn.execute(
                "INSERT INTO project_diagrams "
                "(project_id, diagram_name, diagram_type, file_path, "
                "category_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("p1", name, t, f".claude/d/g/{name}.x", "g", 1, 1),
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM project_diagrams"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_access_level_check_rejects_write(self) -> None:
        """diagram_access only allows `read`/`none` — diagrams have no
        cross-project write semantics by design."""
        conn = _conn()
        _seed_project(conn, "pA", "A")
        _seed_project(conn, "pB", "B")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            conn.execute(
                "INSERT INTO diagram_access "
                "(grantor_project_id, grantee_project_id, access_level, granted_at) "
                "VALUES (?, ?, ?, ?)",
                ("pA", "pB", "write", 1),
            )
        self.assertIn("CHECK", str(ctx.exception).upper())

    # ─── UNIQUE constraints ─────────────────────────────────────────────

    def test_project_diagrams_unique_on_project_and_name(self) -> None:
        conn = _conn()
        _seed_project(conn, "p1", "Acme")
        conn.execute(
            "INSERT INTO project_diagrams "
            "(project_id, diagram_name, diagram_type, file_path, "
            "category_path, created_at, updated_at) "
            "VALUES ('p1', 'x', 'mermaid', 'a.mmd', 'g', 1, 1)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO project_diagrams "
                "(project_id, diagram_name, diagram_type, file_path, "
                "category_path, created_at, updated_at) "
                "VALUES ('p1', 'x', 'mermaid', 'b.mmd', 'g', 2, 2)"
            )

    def test_diagram_snapshots_unique_on_diagram_and_hash(self) -> None:
        conn = _conn()
        _seed_project(conn, "p1", "Acme")
        cursor = conn.execute(
            "INSERT INTO project_diagrams "
            "(project_id, diagram_name, diagram_type, file_path, "
            "category_path, created_at, updated_at) "
            "VALUES ('p1', 'x', 'mermaid', 'a.mmd', 'g', 1, 1) "
            "RETURNING id"
        )
        did = cursor.fetchone()[0]
        conn.execute(
            "INSERT INTO diagram_snapshots "
            "(diagram_id, content_hash, content, created_at, trigger) "
            "VALUES (?, ?, ?, ?, ?)",
            (did, "abc", b"v1", 1, "manual"),
        )
        # Same (diagram_id, content_hash) — UNIQUE fires.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO diagram_snapshots "
                "(diagram_id, content_hash, content, created_at, trigger) "
                "VALUES (?, ?, ?, ?, ?)",
                (did, "abc", b"v1-other", 2, "manual"),
            )

    def test_project_mcp_tool_grants_pk_dedups_triplet(self) -> None:
        conn = _conn()
        _seed_project(conn, "p1", "Acme")
        conn.execute(
            "INSERT INTO project_mcp_tool_grants "
            "(project_id, mcp_name, tool_name, enabled) "
            "VALUES ('p1', 'mermaid', 'render', 1)"
        )
        # Same triplet — PK conflict.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO project_mcp_tool_grants "
                "(project_id, mcp_name, tool_name, enabled) "
                "VALUES ('p1', 'mermaid', 'render', 0)"
            )

    def test_project_modules_pk_dedups_pair(self) -> None:
        conn = _conn()
        _seed_project(conn, "p1", "Acme")
        conn.execute(
            "INSERT INTO project_modules "
            "(project_id, module_name, enabled, registered_at) "
            "VALUES ('p1', 'diagrams', 1, 1)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO project_modules "
                "(project_id, module_name, enabled, registered_at) "
                "VALUES ('p1', 'diagrams', 0, 2)"
            )

    # ─── Cascade behaviour ──────────────────────────────────────────────

    def test_dropping_project_cascades_to_every_diagrams_table(self) -> None:
        conn = _conn()
        _seed_project(conn, "pA", "A")
        _seed_project(conn, "pB", "B")

        # Seed every FK-bearing table.
        did = conn.execute(
            "INSERT INTO project_diagrams "
            "(project_id, diagram_name, diagram_type, file_path, "
            "category_path, created_at, updated_at) "
            "VALUES ('pA', 'x', 'mermaid', 'a.mmd', 'g', 1, 1) "
            "RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO diagram_snapshots "
            "(diagram_id, content_hash, content, created_at, trigger) "
            "VALUES (?, ?, ?, ?, ?)",
            (did, "h1", b"v1", 1, "manual"),
        )
        conn.execute(
            "INSERT INTO diagram_access "
            "(grantor_project_id, grantee_project_id, access_level, granted_at) "
            "VALUES ('pA', 'pB', 'read', 1)"
        )
        conn.execute(
            "INSERT INTO project_mcp_tool_grants "
            "(project_id, mcp_name, tool_name, enabled) "
            "VALUES ('pA', 'mermaid', 'render', 1)"
        )
        conn.execute(
            "INSERT INTO project_modules "
            "(project_id, module_name, enabled, registered_at) "
            "VALUES ('pA', 'diagrams', 1, 1)"
        )

        # Drop the parent project. Cascade should wipe everything keyed
        # on it (including the snapshot via the diagram's own CASCADE).
        conn.execute("DELETE FROM projects WHERE id = 'pA'")

        for table in (
            "project_diagrams",
            "project_mcp_tool_grants",
            "project_modules",
        ):
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = 'pA'"
            ).fetchone()[0]
            self.assertEqual(
                count,
                0,
                f"{table} should be empty after parent project cascade",
            )
        snap_count = conn.execute(
            "SELECT COUNT(*) FROM diagram_snapshots WHERE diagram_id = ?",
            (did,),
        ).fetchone()[0]
        self.assertEqual(snap_count, 0, "snapshots should cascade via parent diagram")
        # `diagram_access` cascades on grantor side too.
        acc_count = conn.execute(
            "SELECT COUNT(*) FROM diagram_access WHERE grantor_project_id = 'pA'"
        ).fetchone()[0]
        self.assertEqual(acc_count, 0, "diagram_access should cascade on grantor")

        # PRAGMA integrity check.
        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertFalse(orphans, f"dangling FKs after cascade: {orphans}")

    def test_dropping_diagram_cascades_to_snapshots(self) -> None:
        conn = _conn()
        _seed_project(conn, "p1", "Acme")
        did = conn.execute(
            "INSERT INTO project_diagrams "
            "(project_id, diagram_name, diagram_type, file_path, "
            "category_path, created_at, updated_at) "
            "VALUES ('p1', 'x', 'mermaid', 'a.mmd', 'g', 1, 1) "
            "RETURNING id"
        ).fetchone()[0]
        for h in ("h1", "h2", "h3"):
            conn.execute(
                "INSERT INTO diagram_snapshots "
                "(diagram_id, content_hash, content, created_at, trigger) "
                "VALUES (?, ?, ?, ?, ?)",
                (did, h, b"v", 1, "manual"),
            )
        # 3 snapshots
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM diagram_snapshots WHERE diagram_id = ?",
                (did,),
            ).fetchone()[0],
            3,
        )

        conn.execute("DELETE FROM project_diagrams WHERE id = ?", (did,))
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM diagram_snapshots WHERE diagram_id = ?",
                (did,),
            ).fetchone()[0],
            0,
        )

    # ─── Partial index sanity ───────────────────────────────────────────

    def test_idx_diagrams_chat_is_partial(self) -> None:
        """The chat_id index is partial (WHERE chat_id IS NOT NULL) so
        the bulk of NULL-chat rows don't bloat it.
        """
        conn = _conn()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_diagrams_chat'"
        ).fetchone()[0]
        self.assertIn("WHERE", sql.upper())
        self.assertIn("CHAT_ID", sql.upper())


if __name__ == "__main__":
    sys.exit(unittest.main())
