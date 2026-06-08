# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.49 access-matrix Phase 1 (item #2) — install.py persists the
orchestrator-root KG collection canonical name to launcher.db via the
`VCT_ORCHESTRATOR_ROOT_KG_COLLECTION` env-var override path.

These tests cover the Python-side helper. The Rust-side migration
contract (default value present after migration 028 runs) is tested
in `launcher/src-tauri/vct-launcher-core/src/db/app_state.rs::tests::
migration_creates_orchestrator_root_collection_setting`.

Test coverage:
  * No env override + no launcher.db (fresh first-install): skip
    cleanly, no deferral.
  * No env override + launcher.db present with default row: skip
    cleanly (migration's default is authoritative).
  * Env override + launcher.db present: row upserted to override value.
  * Env override + launcher.db missing app_state table (pre-mig-008
    schema): skip cleanly.
  * Env override + launcher.db has same value already: idempotent skip
    (no RW open).
  * Env override + launcher.db writer lock held: deferral entry emitted
    (mirrors Bug N + Bug O paired-fix discipline).
  * Whitespace trimming: env value is stripped before persist.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# ─── launcher.db helpers ──────────────────────────────────────────────────


_APP_STATE_DDL = """
CREATE TABLE app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
)
"""


def _build_launcher_db_with_app_state(
    db_path: Path,
    seed_root_collection: str | None,
) -> None:
    """Create launcher.db with the app_state table; optionally seed the
    orchestrator_root_kg_collection row with `seed_root_collection`.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_APP_STATE_DDL)
        if seed_root_collection is not None:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (
                    "orchestrator_root_kg_collection",
                    seed_root_collection,
                    int(time.time() * 1000),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _read_root_collection(db_path: Path) -> str | None:
    """Read the persisted orchestrator-root KG collection value."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT value FROM app_state "
            "WHERE key = 'orchestrator_root_kg_collection'"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ─── Tests ───────────────────────────────────────────────────────────────


class OrchestratorRootCollectionPersistTests(unittest.TestCase):
    """Pin the install.py-side persistence contract for
    `_persist_orchestrator_root_kg_collection`."""

    def setUp(self):
        self._tmp = (
            Path(__file__).resolve().parent
            / f"_tmp_orchroot_{os.getpid()}_{id(self)}"
        )
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._db_path = self._tmp / "launcher.db"
        # VCT_STATE_DIR points at the temp dir so
        # `_discover_app_state_db_path` resolves to our test DB.
        self._env_patch = mock.patch.dict(
            os.environ,
            {"VCT_STATE_DIR": str(self._tmp)},
            clear=False,
        )
        self._env_patch.start()
        # Wipe any leftover override env so test cases set it explicitly.
        for k in ("VCT_ORCHESTRATOR_ROOT_KG_COLLECTION",):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env_patch.stop()
        for k in ("VCT_ORCHESTRATOR_ROOT_KG_COLLECTION",):
            os.environ.pop(k, None)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── Skip-cleanly paths ──────────────────────────────────────────────

    def test_no_override_no_db_skips_cleanly(self):
        """Fresh first-install: no env override AND no launcher.db.
        Helper logs skip + returns without error."""
        self.assertFalse(self._db_path.exists())
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        self.assertEqual(
            [e.condition_id for e in report.entries],
            [],
            "no deferral should be emitted on the no-op skip path",
        )

    def test_no_override_with_db_does_not_touch_existing_row(self):
        """When the env override is absent, the migration's default is
        authoritative — helper does NOT write anything."""
        _build_launcher_db_with_app_state(
            self._db_path,
            seed_root_collection="VibeCodedOrchestrator_KnowledgeGraph",
        )
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        # Value unchanged.
        self.assertEqual(
            _read_root_collection(self._db_path),
            "VibeCodedOrchestrator_KnowledgeGraph",
        )
        self.assertEqual([e.condition_id for e in report.entries], [])

    def test_override_with_missing_db_skips_with_no_deferral(self):
        """Env override set, but launcher.db doesn't exist (fresh
        first-install). The migration handles the default on first
        boot — helper just logs skip."""
        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "AcmeCorp_KG"
        self.assertFalse(self._db_path.exists())
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        # No deferral — this is an expected timing case.
        self.assertEqual([e.condition_id for e in report.entries], [])

    def test_override_with_missing_app_state_table_skips_cleanly(self):
        """Pre-migration-008 launcher.db has no app_state table. Helper
        must NOT crash + leave a deferral entry."""
        # Build launcher.db with NO app_state table.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                "CREATE TABLE _schema_migrations (version INTEGER PRIMARY KEY)"
            )
            conn.commit()
        finally:
            conn.close()

        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "AcmeCorp_KG"
        report = DeferralReport()
        # Must not raise.
        install._persist_orchestrator_root_kg_collection(report)
        # No deferral — we just log skip and wait for migration to run.
        self.assertEqual([e.condition_id for e in report.entries], [])

    def test_override_value_already_matches_is_noop(self):
        """Idempotent: re-running with the same override value should
        not touch the row (no RW open at all)."""
        _build_launcher_db_with_app_state(
            self._db_path,
            seed_root_collection="AcmeCorp_KG",
        )
        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "AcmeCorp_KG"
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        # Value still AcmeCorp_KG; no deferrals.
        self.assertEqual(_read_root_collection(self._db_path), "AcmeCorp_KG")
        self.assertEqual([e.condition_id for e in report.entries], [])

    # ── Override-applied paths ──────────────────────────────────────────

    def test_override_upserts_when_row_present_with_default(self):
        """Common white-label install path: launcher.db has the
        migration's default row; env override flips it."""
        _build_launcher_db_with_app_state(
            self._db_path,
            seed_root_collection="VibeCodedOrchestrator_KnowledgeGraph",
        )
        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "AcmeCorp_KG"
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        self.assertEqual(
            _read_root_collection(self._db_path),
            "AcmeCorp_KG",
            "override must overwrite the default row",
        )
        self.assertEqual([e.condition_id for e in report.entries], [])

    def test_override_inserts_when_row_absent(self):
        """launcher.db exists + has app_state table but no row yet (the
        migration hasn't been applied for this column). Helper INSERTs
        the override value."""
        # Build app_state with NO orchestrator_root_kg_collection row.
        _build_launcher_db_with_app_state(
            self._db_path,
            seed_root_collection=None,
        )
        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "AcmeCorp_KG"
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        self.assertEqual(_read_root_collection(self._db_path), "AcmeCorp_KG")

    def test_override_value_is_trimmed(self):
        """Trailing whitespace / newline (e.g. piped via shell
        substitution) is stripped before persist."""
        _build_launcher_db_with_app_state(
            self._db_path,
            seed_root_collection="VibeCodedOrchestrator_KnowledgeGraph",
        )
        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "  AcmeCorp_KG  \n"
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        self.assertEqual(_read_root_collection(self._db_path), "AcmeCorp_KG")

    def test_empty_override_is_treated_as_no_override(self):
        """Empty / whitespace-only env value is equivalent to no
        override (migration default remains authoritative)."""
        _build_launcher_db_with_app_state(
            self._db_path,
            seed_root_collection="VibeCodedOrchestrator_KnowledgeGraph",
        )
        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "   "
        report = DeferralReport()
        install._persist_orchestrator_root_kg_collection(report)
        # Value unchanged.
        self.assertEqual(
            _read_root_collection(self._db_path),
            "VibeCodedOrchestrator_KnowledgeGraph",
        )

    # ── Locked-DB deferral path (Bug N + O paired-fix discipline) ──────

    def test_override_with_locked_db_emits_deferral(self):
        """vct-hub holds the writer lock (per Bug N's hub-daemon
        scenario). Helper soft-fails to a deferral entry rather than
        crashing the install."""
        _build_launcher_db_with_app_state(
            self._db_path,
            seed_root_collection="VibeCodedOrchestrator_KnowledgeGraph",
        )
        os.environ["VCT_ORCHESTRATOR_ROOT_KG_COLLECTION"] = "AcmeCorp_KG"

        # Acquire a writer lock via BEGIN IMMEDIATE on a separate
        # connection. The helper's RW open + INSERT path will hit a
        # "database is locked" OperationalError within its timeout.
        hub_conn = sqlite3.connect(str(self._db_path), timeout=0.1)
        try:
            hub_conn.execute("BEGIN IMMEDIATE")
            report = DeferralReport()
            install._persist_orchestrator_root_kg_collection(report)
            ids = [e.condition_id for e in report.entries]
            self.assertIn(
                "orchestrator_root_kg_collection_locked",
                ids,
                "expected a deferral entry; got: " + repr(ids),
            )
        finally:
            hub_conn.rollback()
            hub_conn.close()


if __name__ == "__main__":
    unittest.main()
