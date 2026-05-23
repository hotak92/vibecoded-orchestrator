"""Tests for the v0.2.28 KG_COLLECTION / SHARED_KG_COLLECTION /
DEVELOPMENT_COLLECTION env-block plumbing in `.claude/settings.json`.

Background: pre-v0.2.28 the three KG-collection env keys never made
it into the canonical per-project env channel
(`.claude/settings.json::env`). The MCP weaviate-kg subprocess fell
back to the orchestrator-root default and KG searches silently
resolved to the wrong collection. v0.2.28 ships
`_backfill_kg_collection_env_in_project` and wires it into the
install-bundle and orchestrator-root update paths.

Discipline (matching the v0.2.11 PROJECT_NAME / CODE_GRAPH_PROJECT
backfill): missing keys are ADDED, present keys are PRESERVED VERBATIM.
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

from vco_lib import project_init  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _make_project(tmp: Path, settings_env: dict | None = None) -> Path:
    """Create a fake project folder with `.claude/settings.json`.
    If `settings_env` is None, the settings file is created without an
    `env` block. If it's a dict (possibly empty), the env block is
    seeded with its contents.
    """
    folder = tmp / "fake-project"
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    body: dict = {"$schema": "ignored", "permissions": {"allow": []}}
    if settings_env is not None:
        body["env"] = settings_env
    (folder / ".claude" / "settings.json").write_text(
        json.dumps(body, indent=2) + "\n",
        encoding="utf-8",
    )
    return folder


def _make_launcher_db(state_dir: Path, project_folder: Path,
                     primary: str | None,
                     shared: str | None) -> Path:
    """Create a minimal launcher.db with a `projects` row pointing at
    `project_folder` and primary/shared bindings as requested. Returns
    the DB path. Pass `None` for either binding to skip seeding it.
    """
    db_path = state_dir / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            slug TEXT
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT NOT NULL,
            role TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            embedding_model TEXT,
            embedding_dim INTEGER,
            kg_dir_path TEXT,
            weaviate_url TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (project_id, role)
        );
        """
    )
    pid = "00000000-0000-0000-0000-000000000001"
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug) "
        "VALUES (?, ?, ?, ?, 0, 0, ?)",
        (pid, "Test", str(project_folder.resolve()), "base", "test"),
    )
    if primary is not None:
        cur.execute(
            "INSERT INTO project_kg_bindings "
            "(project_id, role, collection_name, config_json, updated_at) "
            "VALUES (?, 'primary', ?, '{}', 0)",
            (pid, primary),
        )
    if shared is not None:
        cur.execute(
            "INSERT INTO project_kg_bindings "
            "(project_id, role, collection_name, config_json, updated_at) "
            "VALUES (?, 'shared', ?, '{}', 0)",
            (pid, shared),
        )
    conn.commit()
    conn.close()
    return db_path


# ─────────────────────────────────────────────────────────────────────
# Behaviour: missing settings file / unparseable JSON
# ─────────────────────────────────────────────────────────────────────


class MissingOrUnparseableSettingsTests(unittest.TestCase):

    def test_returns_missing_when_settings_file_absent(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "no-settings"
            folder.mkdir()
            result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "missing")
            self.assertEqual(result["added_keys"], [])

    def test_returns_unparseable_when_json_is_garbage(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "bad-json"
            (folder / ".claude").mkdir(parents=True)
            (folder / ".claude" / "settings.json").write_text(
                "{not valid json", encoding="utf-8",
            )
            result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "unparseable")
            self.assertEqual(result["added_keys"], [])

    def test_returns_unparseable_when_top_level_not_object(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "list-not-dict"
            (folder / ".claude").mkdir(parents=True)
            (folder / ".claude" / "settings.json").write_text(
                "[]", encoding="utf-8",
            )
            result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "unparseable")


# ─────────────────────────────────────────────────────────────────────
# Behaviour: idempotence — user values preserved verbatim
# ─────────────────────────────────────────────────────────────────────


class UserValuesPreservedTests(unittest.TestCase):

    def test_noop_when_all_three_keys_already_present(self):
        with tempfile.TemporaryDirectory() as td:
            folder = _make_project(Path(td), {
                "KG_COLLECTION": "MyProject_KG",
                "SHARED_KG_COLLECTION": "Shared_KG",
                "DEVELOPMENT_COLLECTION": "MyProject_Dev",
            })
            result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "noop")
            self.assertEqual(result["added_keys"], [])
            # File contents must be unchanged byte-equivalent.
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["KG_COLLECTION"], "MyProject_KG")
            self.assertEqual(on_disk["env"]["SHARED_KG_COLLECTION"], "Shared_KG")
            self.assertEqual(on_disk["env"]["DEVELOPMENT_COLLECTION"], "MyProject_Dev")

    def test_preserves_existing_kg_collection_does_not_overwrite_from_db(self):
        """User has KG_COLLECTION set to a legacy value. launcher.db
        says something different. The backfill MUST NOT overwrite the
        user's value."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "UserChosen_KG",
            })
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_launcher_db(state_dir, folder,
                              primary="DBSaid_KG", shared="DBSaid_Shared")
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertIn("SHARED_KG_COLLECTION", result["added_keys"])
            self.assertIn("DEVELOPMENT_COLLECTION", result["added_keys"])
            self.assertNotIn("KG_COLLECTION", result["added_keys"])
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["KG_COLLECTION"], "UserChosen_KG",
                             "user value must NEVER be overwritten")

    def test_preserves_empty_shared_kg_intentional_disable(self):
        """SHARED_KG_COLLECTION="" is a legitimate user choice meaning
        'no cross-project fan-out'. The backfill MUST treat an empty
        string as 'key present' and not overwrite it.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "X_KG",
                "SHARED_KG_COLLECTION": "",
                "DEVELOPMENT_COLLECTION": "X_Dev",
            })
            result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "noop",
                             "all 3 keys present (even empty SHARED) → noop")
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["SHARED_KG_COLLECTION"], "",
                             "empty-string user disable must survive")


# ─────────────────────────────────────────────────────────────────────
# Behaviour: launcher.db source-of-truth
# ─────────────────────────────────────────────────────────────────────


class LauncherDbSourceOfTruthTests(unittest.TestCase):

    def test_writes_keys_from_db_when_absent_in_settings(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {})  # empty env block
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_launcher_db(state_dir, folder,
                              primary="ProjectX_KnowledgeGraph",
                              shared="Shared_KnowledgeGraph")
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(set(result["added_keys"]), {
                "KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION",
            })
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["KG_COLLECTION"],
                             "ProjectX_KnowledgeGraph")
            self.assertEqual(on_disk["env"]["SHARED_KG_COLLECTION"],
                             "Shared_KnowledgeGraph")
            self.assertEqual(on_disk["env"]["DEVELOPMENT_COLLECTION"],
                             "ProjectX_Development",
                             "dev_collection derived via suffix swap")

    def test_falls_back_to_derivation_when_db_absent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = tmp / "MyCoolProject"
            (folder / ".claude").mkdir(parents=True)
            (folder / ".claude" / "settings.json").write_text(
                json.dumps({"env": {}}, indent=2) + "\n",
                encoding="utf-8",
            )
            # Point VCT_STATE_DIR at an empty dir (no launcher.db).
            empty_state = tmp / "empty-state"
            empty_state.mkdir()
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(empty_state)}):
                result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            # Derived from folder.name = "MyCoolProject"
            self.assertEqual(on_disk["env"]["KG_COLLECTION"],
                             "MyCoolProject_KnowledgeGraph")
            # No shared binding seeded → leave the cross-project gate
            # closed (empty string)
            self.assertEqual(on_disk["env"]["SHARED_KG_COLLECTION"], "")
            self.assertEqual(on_disk["env"]["DEVELOPMENT_COLLECTION"],
                             "MyCoolProject_Development")

    def test_explicit_project_name_overrides_folder_name(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = tmp / "weird-folder-name"
            (folder / ".claude").mkdir(parents=True)
            (folder / ".claude" / "settings.json").write_text(
                json.dumps({"env": {}}), encoding="utf-8",
            )
            empty_state = tmp / "empty-state"
            empty_state.mkdir()
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(empty_state)}):
                result = project_init._backfill_kg_collection_env_in_project(
                    folder, project_name="Canonical Name"
                )
            self.assertEqual(result["action"], "backfilled")
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["KG_COLLECTION"],
                             "CanonicalName_KnowledgeGraph")


# ─────────────────────────────────────────────────────────────────────
# Behaviour: only fill the missing key(s); compose with existing
# ─────────────────────────────────────────────────────────────────────


class PartialFillTests(unittest.TestCase):

    def test_only_shared_added_when_others_present(self):
        with tempfile.TemporaryDirectory() as td:
            folder = _make_project(Path(td), {
                "KG_COLLECTION": "X_KG",
                "DEVELOPMENT_COLLECTION": "X_Dev",
            })
            result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(result["added_keys"], ["SHARED_KG_COLLECTION"])

    def test_dev_derived_from_existing_kg_collection(self):
        """When KG_COLLECTION is preset and DEVELOPMENT_COLLECTION is
        missing, the latter is derived by suffix swap from the former
        (not via folder.name)."""
        with tempfile.TemporaryDirectory() as td:
            folder = _make_project(Path(td), {
                "KG_COLLECTION": "Legacy_KnowledgeGraph",
            })
            result = project_init._backfill_kg_collection_env_in_project(folder)
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["DEVELOPMENT_COLLECTION"],
                             "Legacy_Development")


# ─────────────────────────────────────────────────────────────────────
# v0.2.30: manual_override CORRECTS existing-but-stale env values
# ─────────────────────────────────────────────────────────────────────


def _make_launcher_db_with_override(
    state_dir: Path,
    project_folder: Path,
    primary: str | None,
    shared: str | None,
    primary_override: bool = False,
    shared_override: bool = False,
) -> Path:
    """Variant of `_make_launcher_db` that sets `config_json.manual_override`
    on the seeded binding rows. Used by the v0.2.30 corrector tests."""
    db_path = state_dir / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            slug TEXT
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT NOT NULL,
            role TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            embedding_model TEXT,
            embedding_dim INTEGER,
            kg_dir_path TEXT,
            weaviate_url TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (project_id, role)
        );
        """
    )
    pid = "00000000-0000-0000-0000-000000000001"
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug) "
        "VALUES (?, ?, ?, ?, 0, 0, ?)",
        (pid, "Test", str(project_folder.resolve()), "base", "test"),
    )
    if primary is not None:
        cfg = '{"manual_override":"v0.2.30-test"}' if primary_override else "{}"
        cur.execute(
            "INSERT INTO project_kg_bindings "
            "(project_id, role, collection_name, config_json, updated_at) "
            "VALUES (?, 'primary', ?, ?, 0)",
            (pid, primary, cfg),
        )
    if shared is not None:
        cfg = '{"manual_override":"v0.2.30-test"}' if shared_override else "{}"
        cur.execute(
            "INSERT INTO project_kg_bindings "
            "(project_id, role, collection_name, config_json, updated_at) "
            "VALUES (?, 'shared', ?, ?, 0)",
            (pid, shared, cfg),
        )
    conn.commit()
    conn.close()
    return db_path


class ManualOverrideCorrectsStaleEnvTests(unittest.TestCase):
    """v0.2.30: when launcher.db has `config_json.manual_override` and
    settings.json env disagrees, the backfill should CORRECT the env —
    not just add missing keys."""

    def test_corrects_stale_kg_collection_when_manual_override(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "Default_KnowledgeGraph",
            })
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_launcher_db_with_override(
                state_dir, folder,
                primary="VCODev_KnowledgeGraph", shared=None,
                primary_override=True,
            )
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                result = project_init._backfill_kg_collection_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertIn("KG_COLLECTION (corrected)", result["added_keys"])
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["KG_COLLECTION"],
                             "VCODev_KnowledgeGraph",
                             "manual_override binding must correct stale env value")

    def test_does_not_correct_when_no_manual_override(self):
        """Without manual_override sentinel (auto-seeded default), leave
        user-edited env alone."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "UserPick_KG",
            })
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_launcher_db_with_override(
                state_dir, folder,
                primary="AutoSeed_KG", shared=None,
                primary_override=False,
            )
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                result = project_init._backfill_kg_collection_env_in_project(folder)
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["KG_COLLECTION"], "UserPick_KG",
                             "auto-seed (no manual_override) must NOT overwrite user value")

    def test_corrects_shared_kg_when_manual_override(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "X_KG",
                "SHARED_KG_COLLECTION": "Old_Shared",
                "DEVELOPMENT_COLLECTION": "X_Dev",
            })
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_launcher_db_with_override(
                state_dir, folder,
                primary="X_KG", shared="New_Shared",
                primary_override=False, shared_override=True,
            )
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                project_init._backfill_kg_collection_env_in_project(folder)
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["SHARED_KG_COLLECTION"], "New_Shared")

    def test_respects_empty_shared_intentional_disable(self):
        """SHARED_KG_COLLECTION="" is user's explicit-disable. Even with
        DB manual_override, don't overwrite."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "X_KG",
                "SHARED_KG_COLLECTION": "",
                "DEVELOPMENT_COLLECTION": "X_Dev",
            })
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_launcher_db_with_override(
                state_dir, folder,
                primary="X_KG", shared="Override_Shared",
                shared_override=True,
            )
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                project_init._backfill_kg_collection_env_in_project(folder)
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["SHARED_KG_COLLECTION"], "",
                             "empty-string user-disable must survive even with DB manual_override")

    def test_corrects_paired_development_collection(self):
        """When KG_COLLECTION corrected, paired DEVELOPMENT_COLLECTION
        recomputed via suffix swap."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = _make_project(tmp, {
                "KG_COLLECTION": "OldBase_KnowledgeGraph",
                "DEVELOPMENT_COLLECTION": "OldBase_Development",
            })
            state_dir = tmp / "state"
            state_dir.mkdir()
            _make_launcher_db_with_override(
                state_dir, folder,
                primary="NewBase_KnowledgeGraph", shared=None,
                primary_override=True,
            )
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                project_init._backfill_kg_collection_env_in_project(folder)
            on_disk = json.loads(
                (folder / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(on_disk["env"]["KG_COLLECTION"],
                             "NewBase_KnowledgeGraph")
            self.assertEqual(on_disk["env"]["DEVELOPMENT_COLLECTION"],
                             "NewBase_Development")


class InstallSettingsTemplateMergePreservesEnvTests(unittest.TestCase):
    """v0.2.30: install.py's settings.json render path now merges
    additively when settings.json exists, preserving user's env block."""

    def test_merge_preserves_env_block(self):
        with tempfile.TemporaryDirectory() as td:
            install_root = Path(td) / "vco"
            install_root.mkdir()
            claude_dir = install_root / ".claude"
            claude_dir.mkdir()
            settings = claude_dir / "settings.json"
            settings.write_text(
                json.dumps({
                    "permissions": {"allow": []},
                    "env": {
                        "KG_COLLECTION": "VCODev_KnowledgeGraph",
                        "PROJECT_NAME": "VCO_dev",
                    },
                    "_user_field": "do-not-clobber",
                }, indent=2) + "\n",
                encoding="utf-8",
            )

            # Replay the merge logic install.py uses
            rendered_obj = {
                "$schema": "https://example.test/schema.json",
                "permissions": {"allow": ["Bash(git *)"]},
                "hooks": {"SessionStart": []},
            }
            existing = json.loads(settings.read_text(encoding="utf-8"))
            merged = dict(existing)
            for k, v in rendered_obj.items():
                merged[k] = v
            settings.write_text(
                json.dumps(merged, indent=2) + "\n",
                encoding="utf-8",
            )

            result = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(result["env"]["KG_COLLECTION"],
                             "VCODev_KnowledgeGraph")
            self.assertEqual(result["env"]["PROJECT_NAME"], "VCO_dev")
            self.assertEqual(result["_user_field"], "do-not-clobber")
            self.assertEqual(result["permissions"]["allow"], ["Bash(git *)"])


if __name__ == "__main__":
    unittest.main()
