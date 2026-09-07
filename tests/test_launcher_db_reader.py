# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.launcher_db_reader (v0.2.44 V44-B).

Validates the read-only launcher.db helper used by install.py to resolve
canonical KG collection names from project_kg_bindings (the SoT) instead
of from env vars (a projection).

Coverage:
  * Path discovery (env override, default, nonexistent).
  * Read-only DB open (missing file → None, corrupt file → None).
  * Orchestrator-root project_id lookup (happy path, missing row, no DB).
  * KG binding lookup (primary, shared, invalid role).
  * Convenience helper get_orchestrator_root_bindings (both Nones, both
    resolved, partial).
  * Soft-fail on corrupt DB (write garbage bytes).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_project,
    create_corrupt_launcher_db,
    create_empty_launcher_db,
    create_foreign_schema_launcher_db,
)
from vco_lib import launcher_db_reader  # noqa: E402
from vco_lib.launcher_db_reader import (  # noqa: E402
    _discover_db_path,
    _open_db_readonly,
    get_kg_binding,
    get_orchestrator_root_bindings,
    get_orchestrator_root_project_id,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


def _seed_launcher_db(
    db_path: Path,
    *,
    with_orchestrator_root: bool = True,
    primary_collection: str | None = "OrchestratorRoot_KG",
    shared_collection: str | None = "VibeCodedOrchestrator_KnowledgeGraph",
    extra_projects: list[tuple[str, str, str]] | None = None,
) -> None:
    """Create a REAL-schema launcher.db seeded for the reader's queries.

    The schema is the shipped migration set (see
    ``tests/common/launcher_db_fixture``), not a hand-picked column subset:
    the old inline DDL declared only ``(id, host, name)`` on ``projects`` and
    so silently dropped the NOT NULL ``folder_path`` / ``created_at`` /
    ``updated_at`` columns and the ``host`` CHECK the real table enforces.

    ``extra_projects`` rows are ``(project_id, host, name)``. ``host`` must be
    one of the real CHECK values — ``base`` | ``mao`` | ``orchestrator_root``.
    Folder paths and slugs are derived per project because the real schema
    makes both UNIQUE.
    """
    create_empty_launcher_db(db_path)
    if with_orchestrator_root:
        add_project(
            db_path,
            project_id="root-pid-001",
            name="orchestrator",
            folder_path=db_path.parent / "orchestrator-root",
            slug="orchestrator-root",
            host="orchestrator_root",
            kg_primary=primary_collection,
            kg_shared=shared_collection,
        )
    for pid, host, name in extra_projects or []:
        add_project(
            db_path,
            project_id=pid,
            name=name,
            folder_path=db_path.parent / f"project-{pid}",
            slug=pid,
            host=host,
        )


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Clear VCT_LAUNCHER_DB_PATH and redirect $HOME to tmp_path.

    Without this fixture, tests would accidentally see the real
    ``~/.vct/launcher.db`` on the developer machine and pass/fail
    based on its contents.
    """
    monkeypatch.delenv("VCT_LAUNCHER_DB_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # On Windows, Path.home() consults USERPROFILE first.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


# ────────────────────────────────────────────────────────────────────
# _discover_db_path
# ────────────────────────────────────────────────────────────────────


class TestDiscoverDbPath:
    def test_returns_none_when_no_override_and_no_default_file(self, isolated_env):
        assert _discover_db_path() is None

    def test_returns_default_when_file_exists(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        result = _discover_db_path()
        assert result == default_db

    def test_env_override_takes_precedence(self, isolated_env, tmp_path, monkeypatch):
        custom = tmp_path / "custom-location" / "lncr.db"
        _seed_launcher_db(custom)
        monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(custom))
        assert _discover_db_path() == custom

    def test_env_override_pointing_at_missing_file_returns_none(
        self, isolated_env, tmp_path, monkeypatch
    ):
        nonexistent = tmp_path / "does-not-exist.db"
        monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(nonexistent))
        assert _discover_db_path() is None

    def test_blank_env_override_falls_back_to_default(
        self, isolated_env, tmp_path, monkeypatch
    ):
        # Empty/whitespace override should NOT prevent default fallback.
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", "   ")
        assert _discover_db_path() == default_db


# ────────────────────────────────────────────────────────────────────
# _open_db_readonly
# ────────────────────────────────────────────────────────────────────


class TestOpenDbReadonly:
    def test_returns_none_when_no_db_discoverable(self, isolated_env):
        assert _open_db_readonly() is None

    def test_returns_connection_when_file_exists(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        conn = _open_db_readonly()
        assert conn is not None
        try:
            # row_factory must be sqlite3.Row for the readers to use ["col"] access
            assert conn.row_factory is sqlite3.Row
        finally:
            conn.close()

    def test_returns_none_on_corrupt_db(self, isolated_env, tmp_path, monkeypatch):
        # SQLite will open a garbage-bytes file lazily — connect() succeeds
        # but any query raises DatabaseError. The reader functions catch
        # this; _open_db_readonly itself may or may not raise depending on
        # the platform. Either way the helpers above must return None.
        bad = tmp_path / ".vct" / "launcher.db"
        bad.parent.mkdir(parents=True, exist_ok=True)
        create_corrupt_launcher_db(bad)
        # The reader-level functions must soft-fail; verify via them.
        assert get_orchestrator_root_project_id() is None
        assert get_orchestrator_root_bindings() == (None, None)


# ────────────────────────────────────────────────────────────────────
# get_orchestrator_root_project_id
# ────────────────────────────────────────────────────────────────────


class TestGetOrchestratorRootProjectId:
    def test_returns_id_when_row_present(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        assert get_orchestrator_root_project_id() == "root-pid-001"

    def test_returns_none_when_no_db(self, isolated_env):
        assert get_orchestrator_root_project_id() is None

    def test_returns_none_when_no_orchestrator_root_row(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        # host="base" is the real schema's value for an ordinary user project
        # (CHECK: base|mao|orchestrator_root). The pre-migration fixture wrote
        # "user_project", a host string production cannot write; the property
        # under test — a non-orchestrator_root project is never returned — is
        # unchanged.
        _seed_launcher_db(
            default_db,
            with_orchestrator_root=False,
            extra_projects=[("other-pid", "base", "Some Project")],
        )
        assert get_orchestrator_root_project_id() is None

    def test_returns_first_match_when_multiple(self, isolated_env, tmp_path):
        # Defensive: schema doesn't enforce one-orchestrator-root, but the
        # reader uses LIMIT 1, so this must not raise.
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        # A SECOND orchestrator_root row (the fixture seeds only one). Distinct
        # folder_path/slug because the real schema makes both UNIQUE.
        add_project(
            default_db,
            project_id="root-pid-dupe",
            name="dupe",
            folder_path=tmp_path / "dupe-root",
            slug="dupe-root",
            host="orchestrator_root",
        )
        result = get_orchestrator_root_project_id()
        assert result in {"root-pid-001", "root-pid-dupe"}


# ────────────────────────────────────────────────────────────────────
# get_kg_binding
# ────────────────────────────────────────────────────────────────────


class TestGetKgBinding:
    def test_primary_role_returns_collection(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        assert get_kg_binding("root-pid-001", "primary") == "OrchestratorRoot_KG"

    def test_shared_role_returns_collection(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        assert (
            get_kg_binding("root-pid-001", "shared")
            == "VibeCodedOrchestrator_KnowledgeGraph"
        )

    def test_invalid_role_returns_none_without_db_access(
        self, isolated_env, tmp_path, monkeypatch
    ):
        # Even if a valid DB exists, an invalid role must short-circuit
        # to None BEFORE touching the connection (defensive).
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        # Sentinel: spy on _open_db_readonly — it must NOT be called.
        calls: list[int] = []
        original = launcher_db_reader._open_db_readonly

        def spy():
            calls.append(1)
            return original()

        monkeypatch.setattr(launcher_db_reader, "_open_db_readonly", spy)
        assert get_kg_binding("root-pid-001", "auxiliary") is None
        assert get_kg_binding("root-pid-001", "") is None
        assert get_kg_binding("root-pid-001", "PRIMARY") is None  # case-sensitive
        assert calls == []  # never opened the DB

    def test_missing_binding_row_returns_none(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db, shared_collection=None)
        assert get_kg_binding("root-pid-001", "shared") is None
        # primary still present
        assert get_kg_binding("root-pid-001", "primary") == "OrchestratorRoot_KG"

    def test_unknown_project_id_returns_none(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        assert get_kg_binding("no-such-pid", "primary") is None

    def test_no_db_returns_none(self, isolated_env):
        assert get_kg_binding("any-pid", "primary") is None


# ────────────────────────────────────────────────────────────────────
# get_orchestrator_root_bindings
# ────────────────────────────────────────────────────────────────────


class TestGetOrchestratorRootBindings:
    def test_returns_none_none_when_no_db(self, isolated_env):
        assert get_orchestrator_root_bindings() == (None, None)

    def test_returns_both_when_present(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db)
        assert get_orchestrator_root_bindings() == (
            "OrchestratorRoot_KG",
            "VibeCodedOrchestrator_KnowledgeGraph",
        )

    def test_returns_partial_when_one_role_missing(self, isolated_env, tmp_path):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db, primary_collection=None)
        assert get_orchestrator_root_bindings() == (
            None,
            "VibeCodedOrchestrator_KnowledgeGraph",
        )

    def test_returns_none_none_when_orchestrator_root_missing(
        self, isolated_env, tmp_path
    ):
        default_db = tmp_path / ".vct" / "launcher.db"
        _seed_launcher_db(default_db, with_orchestrator_root=False)
        assert get_orchestrator_root_bindings() == (None, None)


# ────────────────────────────────────────────────────────────────────
# Soft-fail: corrupt DB must not raise
# ────────────────────────────────────────────────────────────────────


def test_corrupt_db_soft_fails_everywhere(isolated_env, tmp_path):
    """All public functions must return None / (None, None) on a corrupt DB.

    This is the load-bearing invariant: install.py wraps the import in
    its own try/except already, but the helpers must not bubble exceptions
    on a malformed launcher.db (e.g. partial write, version mismatch).
    """
    bad = tmp_path / ".vct" / "launcher.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    # DELIBERATELY a different corruption shape from
    # create_corrupt_launcher_db()'s byte blob: an all-zero, page-sized file
    # (the partial-write / sparse-restore case), which SQLite rejects at a
    # different point than a non-zero non-header blob.
    bad.write_bytes(b"\x00" * 4096)

    # None of these may raise
    assert get_orchestrator_root_project_id() is None
    assert get_kg_binding("any-pid", "primary") is None
    assert get_kg_binding("any-pid", "shared") is None
    assert get_orchestrator_root_bindings() == (None, None)


def test_table_missing_soft_fails(isolated_env, tmp_path):
    """When the DB exists but the expected tables are absent, return None."""
    db = tmp_path / ".vct" / "launcher.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    # DELIBERATELY not a launcher.db: a valid SQLite file carrying one
    # unrelated table and NONE of the launcher tables, so every reader query
    # hits "no such table". Built by the shared helper rather than by local
    # DDL — it is the same degraded shape, named once.
    create_foreign_schema_launcher_db(db)

    assert get_orchestrator_root_project_id() is None
    assert get_kg_binding("pid", "primary") is None
    assert get_orchestrator_root_bindings() == (None, None)
