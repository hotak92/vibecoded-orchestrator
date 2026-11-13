# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
v0.2.8 — per-project lifecycle + global-slot tracking + R2 flag wiring.

Covers:
  - Migrations 0005 (rl_weights_state lifecycle columns) + 0006 (rl_global_weights_state table).
  - _build_weights_filename + _glob_weights_files helpers.
  - _fetch_project_flags caching + hub mocking.
  - _push_rl_weights_state writes is_using_global / online_training_disabled / last_finetuned_path.
  - _state_path set at construction; picks latest matching versioned file.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.common.launcher_db_fixture import make_launcher_db


from rl_reranker.rl_server import (
    _build_weights_filename,
    _glob_weights_files,
    _resolve_model_tag_and_dim,
    _LOCAL_FORK_VERSION_SENTINEL,
    RLServer,
)


def _run(coro):
    """Run a coroutine to completion. Mirrors test_endpoints.py's _run_async."""
    return asyncio.run(coro)


# ============================================================================
# _build_weights_filename + _glob_weights_files
# ============================================================================

class TestFilenameHelpers:
    def test_versioned_filename(self):
        assert _build_weights_filename("arctic", "0.2.7") == "rl_model_arctic_0.2.7.pt"
        assert _build_weights_filename("qwen3", "0.3.0") == "rl_model_qwen3_0.3.0.pt"

    def test_empty_version_uses_local_sentinel(self):
        assert _build_weights_filename("arctic") == f"rl_model_arctic_{_LOCAL_FORK_VERSION_SENTINEL}.pt"
        assert _build_weights_filename("arctic", "") == f"rl_model_arctic_{_LOCAL_FORK_VERSION_SENTINEL}.pt"
        assert _build_weights_filename("arctic", "   ") == f"rl_model_arctic_{_LOCAL_FORK_VERSION_SENTINEL}.pt"

    def test_sentinel_is_local(self):
        assert _LOCAL_FORK_VERSION_SENTINEL == "local"

    def test_glob_picks_matching_source(self, tmp_path):
        (tmp_path / "rl_model_arctic_0.2.6.pt").touch()
        (tmp_path / "rl_model_arctic_0.2.7.pt").touch()
        (tmp_path / "rl_model_qwen3_0.2.7.pt").touch()
        (tmp_path / "rl_model_arctic.txt").touch()
        (tmp_path / "rl_model.pt").touch()

        arctic_matches = _glob_weights_files(tmp_path, "arctic")
        assert len(arctic_matches) == 2
        assert all(p.name.startswith("rl_model_arctic_") for p in arctic_matches)
        assert all(p.suffix == ".pt" for p in arctic_matches)
        assert arctic_matches[-1].name == "rl_model_arctic_0.2.7.pt"

    def test_glob_empty_dir_returns_empty(self, tmp_path):
        assert _glob_weights_files(tmp_path, "arctic") == []

    def test_glob_handles_local_sentinel_sort(self, tmp_path):
        (tmp_path / "rl_model_arctic_0.2.5.pt").touch()
        (tmp_path / "rl_model_arctic_0.2.7.pt").touch()
        (tmp_path / "rl_model_arctic_local.pt").touch()
        matches = _glob_weights_files(tmp_path, "arctic")
        # 'local' sorts after '0.x.y' lexically → newest local fork wins.
        assert matches[-1].name == "rl_model_arctic_local.pt"


# ============================================================================
# _resolve_model_tag_and_dim (post-v0.2.8 refactor)
# ============================================================================

class TestModelTagPostRefactor:
    """v0.2.8: tag is the source string only. dim still derives from the table."""

    def test_known_sources_return_source_as_tag(self):
        for source in ("qwen3", "arctic", "openai", "legacy"):
            with patch.dict(os.environ, {"ACTIVE_EMBEDDING": source}, clear=False):
                os.environ.pop("RL_MODEL_TAG", None)
                os.environ.pop("RL_EMB_DIM", None)
                tag, dim = _resolve_model_tag_and_dim()
                assert tag == source
                assert dim > 0


# ============================================================================
# Migration application
# ============================================================================

def _apply_migrations(db_path: Path, migrations_dir: Path) -> None:
    """Apply the module's .sql migrations in order ON TOP OF the real
    launcher.db schema (the shared fixture, with project ``test-project``
    registered so the FK in 0002 resolves) — the database the launcher
    applies them to."""
    make_launcher_db(
        db_path,
        projects=[{"project_id": "test-project", "name": "T", "folder_path": "/tmp/t"}],
    )
    conn = sqlite3.connect(str(db_path))
    try:
        for sql_path in sorted(migrations_dir.glob("*.sql")):
            sql = sql_path.read_text()
            conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


class TestMigrations:
    def _db_path(self, tmp_path):
        return tmp_path / "test.db"

    def _migrations_dir(self):
        return (Path(__file__).resolve().parents[2]
                / "claude_mcp_servers" / "rl_reranker" / "db")

    def test_fresh_install_applies_all_migrations(self, tmp_path):
        """Regression for the v0.2.7 0003 bug: original 0003 ALTER TABLE
        ADD COLUMN embedding_source collided with 0002's primary-key
        column declaration → execute_batch failed → tx rollback → no
        further migrations applied. v0.2.8 hotfix removes the
        duplicate column add. This test verifies the full chain
        completes."""
        db = self._db_path(tmp_path)
        _apply_migrations(db, self._migrations_dir())
        # If we reach here without raising, all migrations applied.

    def test_0005_adds_lifecycle_columns(self, tmp_path):
        db = self._db_path(tmp_path)
        _apply_migrations(db, self._migrations_dir())
        conn = sqlite3.connect(str(db))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(rl_weights_state)")}
        finally:
            conn.close()
        assert "is_using_global" in cols
        assert "online_training_disabled" in cols
        assert "last_finetuned_path" in cols
        assert "project_id" in cols
        assert "embedding_source" in cols
        assert "local_version" in cols
        assert "weights_version" in cols

    def test_0005_columns_default_to_zero_or_empty(self, tmp_path):
        db = self._db_path(tmp_path)
        _apply_migrations(db, self._migrations_dir())
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO rl_weights_state (project_id, embedding_source) "
                "VALUES ('test-project', 'arctic')"
            )
            row = conn.execute(
                "SELECT is_using_global, online_training_disabled, last_finetuned_path "
                "FROM rl_weights_state WHERE project_id='test-project'"
            ).fetchone()
        finally:
            conn.close()
        assert row == (0, 0, "")

    def test_0006_creates_rl_global_weights_state_table(self, tmp_path):
        db = self._db_path(tmp_path)
        _apply_migrations(db, self._migrations_dir())
        conn = sqlite3.connect(str(db))
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(rl_global_weights_state)"
            )}
        finally:
            conn.close()
        assert "rl_global_weights_state" in tables
        expected = {
            "embedding_source", "local_version", "local_arch_version",
            "local_path", "last_loaded_at", "last_rotated_at",
        }
        assert expected <= cols

    def test_0006_embedding_source_is_primary_key(self, tmp_path):
        db = self._db_path(tmp_path)
        _apply_migrations(db, self._migrations_dir())
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO rl_global_weights_state (embedding_source, local_version) "
                "VALUES ('arctic', '0.2.7')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO rl_global_weights_state (embedding_source, local_version) "
                    "VALUES ('arctic', '0.2.8')"
                )
        finally:
            conn.close()


# ============================================================================
# RLServer construction + _state_path
# ============================================================================

def _make_basic_server(tmp_path, monkeypatch, *, project_id: str = "") -> RLServer:
    """Construct an RLServer pointed at tmp dirs. Mirrors test_endpoints._make_server
    but without the layout_paths fixture."""
    project_root = tmp_path / "project"
    (project_root / "state").mkdir(parents=True, exist_ok=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    global_dir = tmp_path / "global"
    global_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(global_dir))
    if project_id:
        monkeypatch.setenv("VCT_PROJECT_ID", project_id)
        monkeypatch.setenv("VCT_MODULE_TOKEN", "fake-token")
    else:
        monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
        monkeypatch.delenv("VCT_MODULE_TOKEN", raising=False)

    return RLServer(
        project_root=project_root,
        log_path=str(logs_dir / "rl_events.jsonl"),
        reranker_model=None,
        port=0,
        bind_host="127.0.0.1",
    )


class TestStatePath:
    def test_state_path_set_at_init(self, tmp_path, monkeypatch):
        srv = _make_basic_server(tmp_path, monkeypatch)
        assert srv._state_path is not None
        # No on-disk checkpoint → local sentinel filename.
        assert srv._state_path.name == "rl_model_arctic_local.pt"

    def test_state_path_picks_existing_versioned_file(self, tmp_path, monkeypatch):
        project_root = tmp_path / "project"
        state = project_root / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "rl_model_arctic_0.2.6.pt").write_bytes(b"\x00")
        (state / "rl_model_arctic_0.2.7.pt").write_bytes(b"\x00")

        monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "global").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(tmp_path / "global"))

        srv = RLServer(
            project_root=project_root,
            log_path=str(tmp_path / "logs" / "rl_events.jsonl"),
            reranker_model=None,
            port=0,
            bind_host="127.0.0.1",
        )
        assert srv._state_path.name == "rl_model_arctic_0.2.7.pt"


# ============================================================================
# _fetch_project_flags
# ============================================================================

def _patch_hub_get(srv, *, status=200, body=None):
    """Mock the hub client's HTTP layer to return a canned response.

    Returns the mock_session so callers can assert the URL that was GET'd
    (v0.2.11: _fetch_project_flags must hit the MODULE-SCOPED config route,
    not the hub.token-gated launcher-public /projects/{id}/config — the
    container holds only its module-identity token).
    """
    if body is None:
        body = {}
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=body)

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    srv._hub_client._ensure_session = AsyncMock(return_value=mock_session)
    srv._hub_client._maybe_refresh_token = AsyncMock()
    return mock_session


class TestFetchProjectFlags:
    def test_returns_flags_from_hub(self, tmp_path, monkeypatch):
        srv = _make_basic_server(tmp_path, monkeypatch, project_id="test-project")
        _patch_hub_get(srv, body={
            "rl_use_global": True,
            "rl_online_training_disabled": False,
            "rl_global_training_source_flag": True,
        })
        flags = _run(srv._fetch_project_flags())
        assert flags["rl_use_global"] is True
        assert flags["rl_online_training_disabled"] is False
        assert flags["rl_global_training_source_flag"] is True

    def test_reads_module_scoped_config_route_not_public(self, tmp_path, monkeypatch):
        """v0.2.11: the container holds only its module-identity token, so the
        flags MUST be read from the module-scoped config route
        `/api/v1/modules/{module_id}/projects/{pid}/config` — NOT the
        hub.token-gated launcher-public `/api/v1/projects/{pid}/config`
        (which would 401). Pins the URL so a regression to the public route
        is caught (it would silently break every per-project toggle)."""
        srv = _make_basic_server(tmp_path, monkeypatch, project_id="test-project")
        mock_session = _patch_hub_get(srv, body={"rl_use_global": True})
        _run(srv._fetch_project_flags())
        called_url = mock_session.get.call_args[0][0]
        assert called_url.endswith(
            "/api/v1/modules/vct-rl-reranker/projects/test-project/config"
        ), f"expected module-scoped config route, got: {called_url}"
        # Must NOT hit the hub.token-gated public route.
        assert "/api/v1/projects/test-project/config" not in called_url

    def test_flat_module_settings_absent_flags_default_false(self, tmp_path, monkeypatch):
        """v0.2.61 live contract (hub 65314d5e): the module-scoped config route
        returns ONLY this module's module_settings as a FLAT top-level
        {key: value} map (not the full ProjectConfig, not nested). A flag that
        is absent from the map means false (matches the launcher's
        get_bool_flag → unwrap_or(false)). Pin: present flag honored, absent
        flags default to false — never None/missing/KeyError."""
        srv = _make_basic_server(tmp_path, monkeypatch, project_id="test-project")
        # Only one flag present; the other two absent from the flat map.
        _patch_hub_get(srv, body={"rl_use_global": True})
        flags = _run(srv._fetch_project_flags())
        assert flags["rl_use_global"] is True
        assert flags["rl_online_training_disabled"] is False  # absent → false
        assert flags["rl_global_training_source_flag"] is False  # absent → false

    def test_accepts_nested_rl_settings_shape(self, tmp_path, monkeypatch):
        srv = _make_basic_server(tmp_path, monkeypatch, project_id="test-project")
        _patch_hub_get(srv, body={
            "rl_settings": {
                "rl_use_global": True,
                "rl_online_training_disabled": True,
                "rl_global_training_source_flag": False,
            },
        })
        flags = _run(srv._fetch_project_flags())
        assert flags["rl_use_global"] is True
        assert flags["rl_online_training_disabled"] is True

    def test_defaults_to_false_on_hub_error(self, tmp_path, monkeypatch):
        srv = _make_basic_server(tmp_path, monkeypatch, project_id="test-project")
        _patch_hub_get(srv, status=500)
        flags = _run(srv._fetch_project_flags())
        assert flags["rl_use_global"] is False
        assert flags["rl_online_training_disabled"] is False
        assert flags["rl_global_training_source_flag"] is False

    def test_cache_hits_within_ttl(self, tmp_path, monkeypatch):
        srv = _make_basic_server(tmp_path, monkeypatch, project_id="test-project")
        mock_session = _patch_hub_get(srv, body={"rl_use_global": True})
        _run(srv._fetch_project_flags())
        first_call_count = mock_session.get.call_count
        _run(srv._fetch_project_flags())
        # Second call should hit cache, not the network.
        assert mock_session.get.call_count == first_call_count

    def test_no_project_id_returns_all_false(self, tmp_path, monkeypatch):
        srv = _make_basic_server(tmp_path, monkeypatch)  # no project_id
        flags = _run(srv._fetch_project_flags())
        assert flags == {
            "rl_use_global": False,
            "rl_online_training_disabled": False,
            "rl_global_training_source_flag": False,
        }
