# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.52 V52-AD — install.py seeds a host-wide
`vct-rl-reranker disabled` row in `module_settings` on fresh installs
when no training data has accumulated yet.

Covers:
  * `_seed_rl_reranker_default_disabled`:
      - skips silently when launcher.db missing
      - skips silently when rl_events table missing (pre-migration-025)
      - skips when module_settings probe fails (pre-migration-034)
      - skips when rl_events count >= threshold (auto-enable territory)
      - skips when global row already exists (preserves user choice)
      - writes NULL-project row when all conditions met
      - JSON encoding round-trips through `Db::module_global_enabled`
        semantics (Python writes 'false'; Rust decodes Value::Bool(false))

The tests use `sqlite3` directly + `tempfile.TemporaryDirectory` so the
path-discovery + on-disk-existence checks are exercised realistically.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# install.py lives at the repo root; tests/ is a sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — mirror the post-034 launcher.db schema for module_settings
# + the migration-025 schema for rl_events.
# ---------------------------------------------------------------------------

_MODULE_SETTINGS_POST_034 = """
CREATE TABLE module_settings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT,
    module_id       TEXT NOT NULL,
    setting_key     TEXT NOT NULL,
    setting_value   TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_ms_unique_global
    ON module_settings(module_id, setting_key)
    WHERE project_id IS NULL;
"""

_MODULE_SETTINGS_PRE_034 = """
CREATE TABLE module_settings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    module_id       TEXT NOT NULL,
    setting_key     TEXT NOT NULL,
    setting_value   TEXT NOT NULL,
    UNIQUE(project_id, module_id, setting_key)
);
"""

_RL_EVENTS_SCHEMA = """
CREATE TABLE rl_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    project_id      TEXT,
    task_id         TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);
"""


def _create_full_db(db_path: Path, num_rl_events: int = 0) -> None:
    """Create launcher.db with post-034 schema for both tables, plus
    `num_rl_events` filler rows in rl_events."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_MODULE_SETTINGS_POST_034)
        conn.executescript(_RL_EVENTS_SCHEMA)
        for i in range(num_rl_events):
            conn.execute(
                "INSERT INTO rl_events (event_type, ts, task_id, payload_json) "
                "VALUES (?, ?, ?, ?)",
                ("retrieval", 1700000000000 + i, f"t-{i}", "{}"),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSeedRlRerankerDefaultDisabled(unittest.TestCase):
    """Cover every soft-fail branch + the success path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._db_path = self._tmp_path / "launcher.db"
        # Patch the discoverer so install.py finds our temp DB.
        self._patcher = mock.patch.object(
            install,
            "_discover_app_state_db_path",
            return_value=self._db_path,
        )
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def _read_global_row(self) -> "str | None":
        """Return the setting_value of the global RL row, or None."""
        conn = sqlite3.connect(str(self._db_path))
        try:
            row = conn.execute(
                "SELECT setting_value FROM module_settings "
                " WHERE project_id IS NULL "
                "   AND module_id = ? "
                "   AND setting_key = ?",
                (install._RL_RERANKER_MODULE_ID,
                 install._MODULE_ENABLED_FOR_PROJECT_KEY),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_skip_when_db_missing(self) -> None:
        # No file at the resolved path — soft-fail, never raises.
        install._seed_rl_reranker_default_disabled()
        # Nothing to assert beyond "no exception raised".

    def test_skip_when_rl_events_table_missing(self) -> None:
        """Pre-migration-025 launcher.db: no rl_events table. Soft-fail."""
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.executescript(_MODULE_SETTINGS_POST_034)
            conn.commit()
        finally:
            conn.close()
        # Should not raise; should not write a row (because the probe
        # of rl_events fails before reaching the write).
        install._seed_rl_reranker_default_disabled()
        self.assertIsNone(self._read_global_row())

    def test_skip_when_module_settings_pre_034(self) -> None:
        """Pre-migration-034 module_settings (NOT NULL project_id):
        the probe for the global row uses `project_id IS NULL` which
        is valid SQL on either schema, but inserting NULL would fail.
        The helper detects the pre-034 schema by the failed insert path
        OR by an OperationalError on the probe — either way, soft-fail.
        """
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.executescript(_MODULE_SETTINGS_PRE_034)
            conn.executescript(_RL_EVENTS_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        # The probe SELECT WHERE project_id IS NULL works on either
        # schema (NULL semantics in SQL are universal). But the insert
        # of project_id=NULL fails on pre-034 with a NOT NULL
        # constraint violation. The helper's outer try/except catches
        # sqlite3.Error and logs `warn`. Verify nothing was written.
        install._seed_rl_reranker_default_disabled()
        # Either NULL-row insertion failed (good — preserves invariant)
        # or it succeeded somehow (would be a bug). The post-condition
        # we care about: no row with project_id IS NULL in the DB.
        self.assertIsNone(self._read_global_row())

    def test_skip_when_rl_events_above_threshold(self) -> None:
        """500+ events → auto-enable territory; don't write a default."""
        _create_full_db(self._db_path, num_rl_events=500)
        install._seed_rl_reranker_default_disabled()
        self.assertIsNone(self._read_global_row())

    def test_skip_when_global_row_already_exists(self) -> None:
        """Pre-existing global row → preserve user choice."""
        _create_full_db(self._db_path, num_rl_events=0)
        conn = sqlite3.connect(str(self._db_path))
        try:
            # Seed an existing global row set to TRUE (= user has
            # explicitly enabled). The seeder must not flip it to false.
            conn.execute(
                "INSERT INTO module_settings "
                "  (project_id, module_id, setting_key, setting_value) "
                "VALUES (NULL, ?, ?, ?)",
                (
                    install._RL_RERANKER_MODULE_ID,
                    install._MODULE_ENABLED_FOR_PROJECT_KEY,
                    "true",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        install._seed_rl_reranker_default_disabled()
        # User's "true" choice preserved.
        self.assertEqual(self._read_global_row(), "true")

    def test_writes_disable_row_on_fresh_install(self) -> None:
        """All conditions met: writes the JSON-encoded `false` row."""
        _create_full_db(self._db_path, num_rl_events=0)
        # Sanity: no global row pre-seed.
        self.assertIsNone(self._read_global_row())

        install._seed_rl_reranker_default_disabled()

        # Row written with JSON-encoded boolean false (matches the
        # Rust setter's Value::Bool(false) encoding).
        self.assertEqual(self._read_global_row(), "false")

    def test_writes_disable_row_with_partial_events(self) -> None:
        """Sub-threshold events (e.g. 100) → still seed disable."""
        _create_full_db(self._db_path, num_rl_events=100)
        install._seed_rl_reranker_default_disabled()
        self.assertEqual(self._read_global_row(), "false")

    def test_idempotent_second_run_preserves_first(self) -> None:
        """Second invocation must not raise, must preserve the seed."""
        _create_full_db(self._db_path, num_rl_events=0)
        install._seed_rl_reranker_default_disabled()
        first = self._read_global_row()
        self.assertEqual(first, "false")

        # Second run sees the existing row → skip path.
        install._seed_rl_reranker_default_disabled()
        second = self._read_global_row()
        self.assertEqual(second, "false")


if __name__ == "__main__":
    unittest.main()
