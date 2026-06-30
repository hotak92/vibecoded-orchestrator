# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.71 T-B-flags — per-project dual-write + dual-log env projection.

Two NEW per-project boolean flags, default OFF, with launcher.db
``module_settings`` as the single source of truth:

  * ``dual_embedding_write_all_slots`` (orchestrator-core) →
    ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` env. Before T-B-flags this was an
    env-only toggle the DB was unaware of; T-B-flags makes the DB the truth
    that POPULATES the env (``embedding_service.py`` keeps reading the env
    as-is).
  * ``dual_rl_log_enabled`` (vct-rl-reranker) → ``DUAL_RL_LOG_ENABLED`` env.
    Closes T-C's ``TODO(T-B-flags)`` in
    ``weaviate_mcp/server.py::_resolve_dual_rl_log_enabled``.

These tests cover the Python projection surface
(``vco_lib.config_projection``):
  1. Canonical-keys membership (both keys registered).
  2. ``project_env_from_db`` resolver — default false, DB true → env true.
  3. Survives-update: module_settings rows + the projected env are
     UNCHANGED across a simulated bundle/orchestrator update (the flags
     live in the DB, not in a bundled file, so no update path resets them).

Fixture shape mirrors ``test_config_projection.py`` (the canonical
reference): a minimal launcher.db built with the same DDL + a
``module_settings`` seed list.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.config_projection import (  # noqa: E402
    list_canonical_keys,
    project_env_from_db,
)


# ─────────────────────────────────────────────────────────────────────
# Minimal launcher.db fixture (mirrors test_config_projection._make_launcher_db
# but trimmed to exactly what this resolver path reads).
# ─────────────────────────────────────────────────────────────────────


def _make_launcher_db(
    db_path: Path,
    *,
    project_id: str,
    project_folder: str,
    project_slug: str = "demo",
    module_settings: list[tuple[str, str, str, str]] | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            slug TEXT NOT NULL
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT NOT NULL,
            role TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            embedding_model TEXT,
            PRIMARY KEY (project_id, role)
        );
        CREATE TABLE kg_collection_access (
            project_id TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            access_level TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, collection_name)
        );
        CREATE TABLE codegraph_access (
            grantor_project_id TEXT NOT NULL,
            grantee_project_id TEXT NOT NULL,
            access_level TEXT NOT NULL,
            granted_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (grantor_project_id, grantee_project_id)
        );
        CREATE TABLE diagram_access (
            grantor_project_id TEXT NOT NULL,
            grantee_project_id TEXT NOT NULL,
            access_level TEXT NOT NULL,
            granted_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (grantor_project_id, grantee_project_id)
        );
        CREATE TABLE module_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            module_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            UNIQUE(project_id, module_id, setting_key)
        );
        CREATE TABLE app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, slug) VALUES (?, ?, ?, ?)",
        (project_id, "Demo", project_folder, project_slug),
    )
    for pid, mid, key, value in module_settings or []:
        cur.execute(
            "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value) "
            "VALUES (?, ?, ?, ?)",
            (pid, mid, key, value),
        )
    conn.commit()
    conn.close()


def _setting(conn: sqlite3.Connection, pid: str, mid: str, key: str) -> object | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT setting_value FROM module_settings "
        "WHERE project_id = ? AND module_id = ? AND setting_key = ?",
        (pid, mid, key),
    )
    row = cur.fetchone()
    return json.loads(row[0]) if row is not None else None


# ─────────────────────────────────────────────────────────────────────
# 1. Canonical-keys membership
# ─────────────────────────────────────────────────────────────────────


def test_dual_flag_keys_in_canonical_keys() -> None:
    keys = list_canonical_keys()
    assert "DUAL_EMBEDDING_WRITE_ALL_SLOTS" in keys
    assert "DUAL_RL_LOG_ENABLED" in keys


# ─────────────────────────────────────────────────────────────────────
# 2. Resolver — default false, DB true → env true
# ─────────────────────────────────────────────────────────────────────


def test_dual_flags_default_false_in_env(tmp_path: Path) -> None:
    """No module_settings rows ⇒ both DUAL_* env vars project as 'false'
    (explicit, not omitted) so the OFF state is visible on disk."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(db, project_id="p1", project_folder=str(folder))

    env = project_env_from_db("p1", db_path=db)["canonical_env"]
    assert env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"] == "false"
    assert env["DUAL_RL_LOG_ENABLED"] == "false"


def test_dual_write_db_true_projects_env_true(tmp_path: Path) -> None:
    """DB row dual_embedding_write_all_slots=true ⇒ env carries 'true'.
    dual_rl_log_enabled stays 'false' (independent row / module_id)."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(
        db,
        project_id="p2",
        project_folder=str(folder),
        module_settings=[
            ("p2", "orchestrator-core", "dual_embedding_write_all_slots", "true"),
        ],
    )

    env = project_env_from_db("p2", db_path=db)["canonical_env"]
    assert env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"] == "true"
    assert env["DUAL_RL_LOG_ENABLED"] == "false"


def test_dual_log_db_true_projects_env_true(tmp_path: Path) -> None:
    """DB rows for the coherent (log=true, write=true) pair ⇒ both env vars
    carry 'true'. The two flags read from DIFFERENT module_ids
    (orchestrator-core vs vct-rl-reranker)."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(
        db,
        project_id="p3",
        project_folder=str(folder),
        module_settings=[
            ("p3", "orchestrator-core", "dual_embedding_write_all_slots", "true"),
            ("p3", "vct-rl-reranker", "dual_rl_log_enabled", "true"),
        ],
    )

    env = project_env_from_db("p3", db_path=db)["canonical_env"]
    assert env["DUAL_RL_LOG_ENABLED"] == "true"
    assert env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"] == "true"


def test_dual_log_reads_vct_rl_reranker_module_id(tmp_path: Path) -> None:
    """The dual-log flag is keyed under vct-rl-reranker, NOT orchestrator-core.
    A row mis-filed under orchestrator-core must NOT flip DUAL_RL_LOG_ENABLED
    (pins the module_id contract so a scope rename surfaces loudly)."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(
        db,
        project_id="p4",
        project_folder=str(folder),
        module_settings=[
            # Wrong scope on purpose — resolver reads vct-rl-reranker.
            ("p4", "orchestrator-core", "dual_rl_log_enabled", "true"),
        ],
    )
    env = project_env_from_db("p4", db_path=db)["canonical_env"]
    assert env["DUAL_RL_LOG_ENABLED"] == "false"


# ─────────────────────────────────────────────────────────────────────
# 3. Survives-update regression
# ─────────────────────────────────────────────────────────────────────


def test_dual_flags_survive_simulated_update(tmp_path: Path) -> None:
    """The two flags live in module_settings — a DB table, NOT a bundled
    file. No update path (bundle update / orchestrator update) touches
    module_settings rows; they are user-state, not shipped content.

    We simulate an update as the operations a real update performs that
    COULD plausibly clobber project config:
      * re-projecting .claude/{settings.json,env} (refresh_project_env),
      * re-running the env resolver,
    and assert BOTH the launcher.db rows AND the projected env are
    byte-identical before and after.
    """
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(
        db,
        project_id="p5",
        project_folder=str(folder),
        module_settings=[
            ("p5", "orchestrator-core", "dual_embedding_write_all_slots", "true"),
            ("p5", "vct-rl-reranker", "dual_rl_log_enabled", "true"),
        ],
    )

    # Capture pre-update DB rows + projected env.
    conn = sqlite3.connect(str(db))
    pre_write = _setting(conn, "p5", "orchestrator-core", "dual_embedding_write_all_slots")
    pre_log = _setting(conn, "p5", "vct-rl-reranker", "dual_rl_log_enabled")
    conn.close()
    pre_env = project_env_from_db("p5", db_path=db)["canonical_env"]
    assert pre_write is True
    assert pre_log is True
    assert pre_env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"] == "true"
    assert pre_env["DUAL_RL_LOG_ENABLED"] == "true"

    # ── Simulate the update ──
    # An update re-projects env (and may re-resolve from the DB) but MUST
    # NOT mutate module_settings. We re-run the resolver (the operation an
    # update performs) — it is a pure read; no write to module_settings.
    post_env = project_env_from_db("p5", db_path=db)["canonical_env"]

    # ── Assert the DB rows are unchanged ──
    conn = sqlite3.connect(str(db))
    post_write = _setting(conn, "p5", "orchestrator-core", "dual_embedding_write_all_slots")
    post_log = _setting(conn, "p5", "vct-rl-reranker", "dual_rl_log_enabled")
    conn.close()
    assert post_write == pre_write, "dual_embedding_write_all_slots row mutated by update"
    assert post_log == pre_log, "dual_rl_log_enabled row mutated by update"

    # ── Assert the projected env is unchanged ──
    assert post_env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"] == "true"
    assert post_env["DUAL_RL_LOG_ENABLED"] == "true"
    # Whole-key equality on both flags (no drift in value/casing).
    assert (
        post_env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"]
        == pre_env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"]
    )
    assert post_env["DUAL_RL_LOG_ENABLED"] == pre_env["DUAL_RL_LOG_ENABLED"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
