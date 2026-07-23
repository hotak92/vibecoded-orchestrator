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
    # v0.2.88 (DEFECT 5): the third dual-write flag joins the managed set so a
    # hand-set env value is reconciled to the DB truth on every update.
    assert "DUAL_EMBEDDING_ARCTIC_SECONDARY" in keys


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


# ─────────────────────────────────────────────────────────────────────
# 4. v0.2.88 (DEFECT 5): arctic-secondary flag on the same channel
# ─────────────────────────────────────────────────────────────────────


def test_arctic_secondary_default_false_in_env(tmp_path: Path) -> None:
    """No module_settings row ⇒ DUAL_EMBEDDING_ARCTIC_SECONDARY projects as
    'false' (explicit, not omitted) so the OFF state is visible on disk."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(db, project_id="a1", project_folder=str(folder))

    env = project_env_from_db("a1", db_path=db)["canonical_env"]
    assert env["DUAL_EMBEDDING_ARCTIC_SECONDARY"] == "false"


def test_arctic_secondary_db_true_projects_env_true(tmp_path: Path) -> None:
    """DB row dual_embedding_arctic_secondary=true ⇒ env carries 'true'.
    Independent — the other two flags stay 'false' (no cascade)."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(
        db,
        project_id="a2",
        project_folder=str(folder),
        module_settings=[
            ("a2", "orchestrator-core", "dual_embedding_arctic_secondary", "true"),
        ],
    )

    env = project_env_from_db("a2", db_path=db)["canonical_env"]
    assert env["DUAL_EMBEDDING_ARCTIC_SECONDARY"] == "true"
    # Independent of the other two.
    assert env["DUAL_EMBEDDING_WRITE_ALL_SLOTS"] == "false"
    assert env["DUAL_RL_LOG_ENABLED"] == "false"


def test_arctic_secondary_reads_orchestrator_core_module_id(tmp_path: Path) -> None:
    """The arctic-secondary flag is keyed under orchestrator-core. A row
    mis-filed under vct-rl-reranker must NOT flip the env (pins the module_id
    contract so a scope rename surfaces loudly)."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(
        db,
        project_id="a3",
        project_folder=str(folder),
        module_settings=[
            # Wrong scope on purpose — resolver reads orchestrator-core.
            ("a3", "vct-rl-reranker", "dual_embedding_arctic_secondary", "true"),
        ],
    )
    env = project_env_from_db("a3", db_path=db)["canonical_env"]
    assert env["DUAL_EMBEDDING_ARCTIC_SECONDARY"] == "false"


def test_arctic_secondary_db_wins_over_hand_set_env(tmp_path: Path) -> None:
    """RED-PROOF (DEFECT 5): a hand-set env value and the DB value DISAGREE —
    the projection re-derives DUAL_EMBEDDING_ARCTIC_SECONDARY from the DB, so
    the DB wins. Pre-fix the key was UNKNOWN to the projection: it wasn't in
    the canonical set, so a hand-set env survived (luck, not design). Now the
    projection emits the DB-truth value regardless of any prior on-disk value.

    We assert the projected canonical value matches the DB (not the phantom
    hand-set 'true'), for BOTH DB states:
      * DB says OFF (no row / false) → projection MUST emit 'false' even though
        a user might have hand-set 'true' in .claude/env.
      * DB says ON (row true)        → projection MUST emit 'true'.
    Because the projection is a pure DB→env derivation (it does not read the
    prior env), the canonical output is definitionally the DB truth — this
    test pins that contract so a future refactor can't reintroduce an
    env-passthrough that would let a hand-set value survive.
    """
    folder = tmp_path / "proj"
    folder.mkdir()

    # Case A: DB OFF — projection emits 'false' regardless of a phantom
    # hand-set 'true'.
    db_off = tmp_path / "launcher_off.db"
    _make_launcher_db(db_off, project_id="a4", project_folder=str(folder))
    env_off = project_env_from_db("a4", db_path=db_off)["canonical_env"]
    assert env_off["DUAL_EMBEDDING_ARCTIC_SECONDARY"] == "false", (
        "DB OFF must project 'false' — a hand-set env 'true' must NOT survive "
        "(the projection re-derives from the DB every update)"
    )

    # Case B: DB ON — projection emits 'true'.
    db_on = tmp_path / "launcher_on.db"
    _make_launcher_db(
        db_on,
        project_id="a5",
        project_folder=str(folder),
        module_settings=[
            ("a5", "orchestrator-core", "dual_embedding_arctic_secondary", "true"),
        ],
    )
    env_on = project_env_from_db("a5", db_path=db_on)["canonical_env"]
    assert env_on["DUAL_EMBEDDING_ARCTIC_SECONDARY"] == "true", (
        "DB ON must project 'true' — the DB is the source of truth"
    )


def test_arctic_secondary_hand_set_env_file_flips_to_db_off(tmp_path: Path) -> None:
    """v0.2.88 (NIT-13): END-TO-END red-proof — seed a REAL hand-set
    ``DUAL_EMBEDDING_ARCTIC_SECONDARY=true`` in ``.claude/env`` (the exact
    migration case named in MINOR-7: an env-only user updating to v0.2.88 with
    the DB default OFF) and assert the WRITTEN file flips to ``false`` after the
    projection runs. Unlike the pure DB→env-dict test above, this exercises
    ``apply_project_env`` against an on-disk env file so the "hand-set value does
    NOT survive an update" contract is pinned at the FILE level, not just the
    projection's return value.
    """
    from vco_lib.config_projection import apply_project_env  # noqa: PLC0415

    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    env_file = folder / ".claude" / "env"

    # A user hand-set the flag ON in .claude/env before v0.2.88 (the value the
    # projection now owns and re-derives from the DB).
    env_file.write_text(
        'export DUAL_EMBEDDING_ARCTIC_SECONDARY="true"\n', encoding="utf-8"
    )

    # DB has NO row → default OFF (the pre-0.2.88 env-only population is gone).
    db_off = tmp_path / "launcher.db"
    _make_launcher_db(db_off, project_id="n13", project_folder=str(folder))
    bundle = project_env_from_db("n13", db_path=db_off)
    apply_project_env(bundle, surfaces=("claude_env",))

    written = env_file.read_text(encoding="utf-8")

    # The projection writes its keys inside the `# vco-managed-begin/end` block
    # and preserves user lines OUTSIDE it. The MANAGED value is what shell-source
    # uses (top-to-bottom, the appended managed export is the LAST one to run and
    # therefore wins over the user's pre-block line). Assert the managed block
    # re-derived OFF.
    begin = written.index("# vco-managed-begin")
    end = written.index("# vco-managed-end")
    managed = written[begin:end]
    assert 'DUAL_EMBEDDING_ARCTIC_SECONDARY="false"' in managed, (
        "the projection's MANAGED block must carry the DB-off value 'false' — "
        "the hand-set env value does NOT survive the update (NIT-13); got "
        "managed block:\n" + managed
    )
    assert 'DUAL_EMBEDDING_ARCTIC_SECONDARY="true"' not in managed, (
        "the managed block must NOT carry the stale hand-set 'true'"
    )
    # The managed export is the effective value at shell-source time because it
    # is appended AFTER any pre-block user line (last export wins). Pin ordering.
    user_line_pos = written.index('export DUAL_EMBEDDING_ARCTIC_SECONDARY="true"')
    managed_false_pos = written.index(
        'export DUAL_EMBEDDING_ARCTIC_SECONDARY="false"'
    )
    assert managed_false_pos > user_line_pos, (
        "the managed 'false' export must come AFTER the user's pre-block 'true' "
        "so it wins at shell-source time"
    )


def test_arctic_secondary_survives_simulated_update(tmp_path: Path) -> None:
    """Like the two siblings: the arctic-secondary flag lives in
    module_settings (a DB table, not a bundled file), so re-running the
    resolver (what an update does) leaves BOTH the DB row and the projected
    env byte-identical."""
    db = tmp_path / "launcher.db"
    folder = tmp_path / "proj"
    folder.mkdir()
    _make_launcher_db(
        db,
        project_id="a6",
        project_folder=str(folder),
        module_settings=[
            ("a6", "orchestrator-core", "dual_embedding_arctic_secondary", "true"),
        ],
    )

    conn = sqlite3.connect(str(db))
    pre = _setting(conn, "a6", "orchestrator-core", "dual_embedding_arctic_secondary")
    conn.close()
    pre_env = project_env_from_db("a6", db_path=db)["canonical_env"]
    assert pre is True
    assert pre_env["DUAL_EMBEDDING_ARCTIC_SECONDARY"] == "true"

    post_env = project_env_from_db("a6", db_path=db)["canonical_env"]

    conn = sqlite3.connect(str(db))
    post = _setting(conn, "a6", "orchestrator-core", "dual_embedding_arctic_secondary")
    conn.close()
    assert post == pre, "arctic-secondary row mutated by update"
    assert (
        post_env["DUAL_EMBEDDING_ARCTIC_SECONDARY"]
        == pre_env["DUAL_EMBEDDING_ARCTIC_SECONDARY"]
        == "true"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
