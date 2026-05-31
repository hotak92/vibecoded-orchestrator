# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.42 RT-13: W40-adoption smart-path uplift tests.

The helper under test is ``install.py::_w40_run_adoption_uplifts``.
It is called from ``_self_heal_kg_bindings_on_update`` after every
``_prefix_adopt_kg_bindings_pass`` adoption to run ``migrate_collections``
against the newly-adopted collection and handle schema drift.

Test coverage:
  * noop action → no deferral entry emitted, audit row written, applied=True.
  * patch_props action → applied silently, audit row written, applied=True.
  * rebuild action → deferral entry emitted, audit row written, applied=False.
  * migrate error → soft-fail, audit row written, applied=False.
  * known-suffix detection: _KnowledgeGraph → KG_COLLECTION, _Development →
    DEVELOPMENT_COLLECTION; unknown suffix → skip (no migrate call, no audit).
  * audit_log rows use operation='kg_collection_adopt_uplift' with correct detail.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# ─── Minimal launcher.db schema ──────────────────────────────────────────────

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    operation  TEXT NOT NULL,
    project_id TEXT,
    module_id  TEXT,
    detail     TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def _open_temp_db():
    """Create a temporary launcher.db with just the audit_log table."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(_AUDIT_SCHEMA)
    conn.commit()
    conn.close()
    return Path(tmp.name)


def _read_audit_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT operation, detail FROM audit_log ORDER BY id"
        ).fetchall()
        return [{"operation": r[0], "detail": json.loads(r[1])} for r in rows]
    finally:
        conn.close()


# ─── Helper: build a prefix_adopts list ──────────────────────────────────────

def _adopts(collection_name: str, old_name: str | None = None) -> list:
    return [
        ("proj-1", "kg", old_name or f"Legacy_{collection_name}", collection_name, 42)
    ]


# ─── Tests ────────────────────────────────────────────────────────────────────


class AdoptionUpliftActionTest(unittest.TestCase):
    """Tests for _w40_run_adoption_uplifts across different migrate_collections actions."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        os.environ["VCT_STATE_DIR"] = self.tmp_dir
        self.db_path = _open_temp_db()

    def tearDown(self):
        os.environ.pop("VCT_STATE_DIR", None)
        try:
            os.unlink(str(self.db_path))
        except OSError:
            pass

    def _run_uplift(self, collection_name, mock_result, old_name=None):
        """Helper: run _w40_run_adoption_uplifts with mocked migrate_collections."""
        deferral = DeferralReport.read(Path(self.tmp_dir))

        with mock.patch.object(
            install._project_init,
            "migrate_collections",
            return_value=mock_result,
        ):
            install._w40_run_adoption_uplifts(
                prefix_adopts=_adopts(collection_name, old_name),
                weaviate_url="http://localhost:8081",
                deferral_report=deferral,
                db_path=self.db_path,
            )

        return deferral

    def test_noop_action_no_deferral_audit_applied(self):
        """noop action → no deferral entry, audit row with applied=True."""
        collection = "AcmeCorp_KnowledgeGraph"
        mock_result = {
            "plan": [{"collection": collection, "action": "noop"}],
            "dry_run": False,
            "errors": [],
        }
        deferral = self._run_uplift(collection, mock_result)

        # No deferral entries.
        self.assertEqual(
            len(deferral.entries), 0,
            "noop must not produce a deferral entry",
        )

        # Audit row written with applied=True.
        rows = _read_audit_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["operation"], "kg_collection_adopt_uplift")
        self.assertEqual(rows[0]["detail"]["collection_name"], collection)
        self.assertEqual(rows[0]["detail"]["smart_path_action"], "noop")
        self.assertTrue(rows[0]["detail"]["applied"])

    def test_patch_props_action_applied_silently(self):
        """patch_props action → applied silently, audit applied=True."""
        collection = "AcmeCorp_KnowledgeGraph"
        mock_result = {
            "plan": [{"collection": collection, "action": "patch_props"}],
            "dry_run": False,
            "errors": [],
        }
        deferral = self._run_uplift(collection, mock_result)

        self.assertEqual(len(deferral.entries), 0)

        rows = _read_audit_rows(self.db_path)
        self.assertEqual(rows[0]["detail"]["smart_path_action"], "patch_props")
        self.assertTrue(rows[0]["detail"]["applied"])

    def test_rebuild_action_deferred_not_applied(self):
        """rebuild action → deferral entry emitted, audit applied=False."""
        collection = "AcmeCorp_KnowledgeGraph"
        mock_result = {
            "plan": [{"collection": collection, "action": "rebuild"}],
            "dry_run": False,
            "errors": [],
        }
        deferral = self._run_uplift(collection, mock_result)

        # Must have a deferral entry.
        self.assertGreater(
            len(deferral.entries), 0,
            "rebuild must produce a deferral entry",
        )
        entry = deferral.entries[0]
        self.assertIn("schema_migration_required", entry.condition_id)
        self.assertEqual(entry.severity, "warning")
        # The command must mention the collection name.
        self.assertIn(collection, entry.command_to_apply)

        # Audit row with applied=False.
        rows = _read_audit_rows(self.db_path)
        self.assertEqual(rows[0]["detail"]["smart_path_action"], "rebuild")
        self.assertFalse(rows[0]["detail"]["applied"])

    def test_migrate_error_soft_fails_audit_applied_false(self):
        """If migrate_collections raises, it soft-fails and writes audit applied=False."""
        collection = "AcmeCorp_KnowledgeGraph"
        deferral = DeferralReport.read(Path(self.tmp_dir))

        with mock.patch.object(
            install._project_init,
            "migrate_collections",
            side_effect=RuntimeError("Weaviate connection refused"),
        ):
            install._w40_run_adoption_uplifts(
                prefix_adopts=_adopts(collection),
                weaviate_url="http://localhost:8081",
                deferral_report=deferral,
                db_path=self.db_path,
            )

        rows = _read_audit_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["detail"]["applied"])
        self.assertEqual(rows[0]["detail"]["smart_path_action"], "error")

    def test_unknown_suffix_skips_migrate_no_audit(self):
        """Collections without _KnowledgeGraph / _Development suffix are skipped."""
        collection = "AcmeCorp_CustomSuffix"
        deferral = DeferralReport.read(Path(self.tmp_dir))

        migrate_called = []
        with mock.patch.object(
            install._project_init,
            "migrate_collections",
            side_effect=lambda *a, **kw: migrate_called.append(True) or {},
        ):
            install._w40_run_adoption_uplifts(
                prefix_adopts=_adopts(collection),
                weaviate_url="http://localhost:8081",
                deferral_report=deferral,
                db_path=self.db_path,
            )

        self.assertEqual(migrate_called, [], "migrate must not be called for unknown suffix")
        rows = _read_audit_rows(self.db_path)
        self.assertEqual(len(rows), 0, "no audit row for skipped collection")

    def test_development_suffix_uses_dev_env_key(self):
        """_Development suffix sets DEVELOPMENT_COLLECTION in env during migrate call."""
        collection = "AcmeCorp_Development"
        seen_env = {}
        mock_result = {
            "plan": [{"collection": collection, "action": "noop"}],
            "dry_run": False,
            "errors": [],
        }

        def _capture_env(*a, **kw):
            seen_env["DEVELOPMENT_COLLECTION"] = os.environ.get("DEVELOPMENT_COLLECTION")
            seen_env["KG_COLLECTION"] = os.environ.get("KG_COLLECTION")
            return mock_result

        deferral = DeferralReport.read(Path(self.tmp_dir))
        with mock.patch.object(
            install._project_init,
            "migrate_collections",
            side_effect=_capture_env,
        ):
            install._w40_run_adoption_uplifts(
                prefix_adopts=_adopts(collection),
                weaviate_url="http://localhost:8081",
                deferral_report=deferral,
                db_path=self.db_path,
            )

        self.assertEqual(
            seen_env["DEVELOPMENT_COLLECTION"], collection,
            "DEVELOPMENT_COLLECTION must point at the adopted collection during migrate",
        )
        # KG_COLLECTION must NOT be polluted.
        original_kg = os.environ.get("KG_COLLECTION")
        self.assertEqual(
            seen_env.get("KG_COLLECTION"), original_kg,
            "KG_COLLECTION must be unchanged during _Development adoption",
        )

    def test_env_restored_after_migrate_call(self):
        """The env override (KG_COLLECTION) is restored after migrate_collections returns."""
        collection = "AcmeCorp_KnowledgeGraph"
        original_kg = os.environ.get("KG_COLLECTION", "ORIGINAL_VALUE")
        os.environ["KG_COLLECTION"] = "ORIGINAL_VALUE"
        mock_result = {
            "plan": [{"collection": collection, "action": "noop"}],
            "dry_run": False,
            "errors": [],
        }
        deferral = DeferralReport.read(Path(self.tmp_dir))

        with mock.patch.object(
            install._project_init,
            "migrate_collections",
            return_value=mock_result,
        ):
            install._w40_run_adoption_uplifts(
                prefix_adopts=_adopts(collection),
                weaviate_url="http://localhost:8081",
                deferral_report=deferral,
                db_path=self.db_path,
            )

        self.assertEqual(
            os.environ.get("KG_COLLECTION"), "ORIGINAL_VALUE",
            "KG_COLLECTION must be restored after migrate call",
        )
        # Cleanup.
        if original_kg == "ORIGINAL_VALUE":
            pass  # restore is already done
        elif original_kg:
            os.environ["KG_COLLECTION"] = original_kg
        else:
            os.environ.pop("KG_COLLECTION", None)


if __name__ == "__main__":
    unittest.main()
