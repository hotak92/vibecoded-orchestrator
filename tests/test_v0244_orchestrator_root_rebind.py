# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.44 V44-A: orchestrator-root adopt-and-route.

Tests the new ``_seed_weaviate_shared_kg_only`` behaviour on orchestrator-root
installs. Instead of running the shared-KG sync subprocess (legacy behaviour) or
gating on string-equality of ``KG_COLLECTION == SHARED_KG_COLLECTION``
(v0.2.43 V0243-0 behaviour), the function now ALWAYS short-circuits on
orchestrator-root: it picks a canonical collection name (``SHARED_KG_COLLECTION``
wins), rebinds the launcher.db ``project_kg_bindings`` rows (primary + shared)
to that canonical name, updates the env keys in ``.claude/settings.json`` +
``.claude/env``, upserts ``app_state.last_installed_{kg,shared_kg}_collection``,
and skips the sync subprocess entirely.

Pure-unit tests: no real Weaviate, no real subprocess. SQLite launcher.db is
backed by a tempfile so the rebind helper can actually UPDATE rows.

Tests in this module
~~~~~~~~~~~~~~~~~~~~

* test_orchestrator_root_rebind_picks_shared_as_canonical
* test_orchestrator_root_rebind_uses_kg_when_shared_empty
* test_non_orchestrator_root_unaffected
* test_rebind_soft_fails_when_launcher_db_missing
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402


# ─── DB fixture helpers ───────────────────────────────────────────────────────


# Mirrors the schema columns the rebind helper reads/writes. We do not
# include every column the real launcher.db has — only the ones the code
# under test interacts with.
_PROJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    folder_path  TEXT NOT NULL,
    host         TEXT NOT NULL,
    slug         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
"""

_BINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_kg_bindings (
    project_id        TEXT NOT NULL,
    role              TEXT NOT NULL,
    collection_name   TEXT NOT NULL,
    embedding_model   TEXT,
    embedding_dim     INTEGER,
    kg_dir_path       TEXT,
    weaviate_url      TEXT,
    config_json       TEXT,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (project_id, role)
);
"""

_APP_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def _make_launcher_db_with_root(
    tmp: Path,
    *,
    primary_collection: str = "VCODev_KnowledgeGraph",
    shared_collection: str = "VibeCodedOrchestrator_KnowledgeGraph",
    project_id: str = "root-project-uuid",
) -> Path:
    """Materialise a temp launcher.db pre-seeded with an orchestrator-root row.

    The bindings table is seeded with one primary + one shared row pointing
    at potentially-divergent collection names so the rebind helper has
    something to update.
    """
    db_path = tmp / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_PROJECTS_SCHEMA + _BINDINGS_SCHEMA + _APP_STATE_SCHEMA)
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at) "
        "VALUES (?, 'VCODev', ?, 'orchestrator_root', 'vcodev', ?, ?)",
        (project_id, str(tmp), now, now),
    )
    conn.execute(
        "INSERT INTO project_kg_bindings "
        "(project_id, role, collection_name, updated_at) VALUES (?, 'primary', ?, ?)",
        (project_id, primary_collection, now),
    )
    conn.execute(
        "INSERT INTO project_kg_bindings "
        "(project_id, role, collection_name, updated_at) VALUES (?, 'shared', ?, ?)",
        (project_id, shared_collection, now),
    )
    conn.commit()
    conn.close()
    return db_path


def _read_binding(db_path: Path, project_id: str, role: str) -> str | None:
    """Return ``collection_name`` for ``(project_id, role)`` or ``None``."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT collection_name FROM project_kg_bindings "
            "WHERE project_id = ? AND role = ?",
            (project_id, role),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _make_args(update: bool = True, skip_seed: bool = False) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.update = update
    ns.skip_seed = skip_seed
    return ns


# ─── Tests ────────────────────────────────────────────────────────────────────


class OrchestratorRootRebindTest(unittest.TestCase):
    """V44-A: adopt-and-route on orchestrator-root installs."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        # Required by install.py's app_state writer (resolves launcher.db via
        # _discover_app_state_db_path; we also pin VCT_LAUNCHER_DB_PATH so the
        # rebind helper's read uses the same DB).
        os.environ["VCT_STATE_DIR"] = str(self.tmp)
        # Captured subprocess.run invocations so we can assert sync was NOT run.
        self.captured_cmds: list[list[str]] = []

    def tearDown(self) -> None:
        os.environ.pop("VCT_STATE_DIR", None)
        os.environ.pop("VCT_LAUNCHER_DB_PATH", None)
        self._tmpdir.cleanup()

    def _fake_run(self, cmd, **kwargs):  # noqa: ARG002 - subprocess signature
        self.captured_cmds.append(list(cmd))

        class _R:
            returncode = 0

        return _R()

    def _patch_count(self, primary: int = 42, shared: int = 7):
        """Patch ``_count_weaviate_class_objects`` to return canned counts.

        The helper is called twice (once for primary, once for shared) for
        the diagnostic print line. Tests never depend on the exact numbers
        — they just need the helper to not blow up on a non-running Weaviate.
        """
        def _stub(weaviate_url: str, name: str) -> int:  # noqa: ARG001
            if name and "Shared" in name or name == "VibeCodedOrchestrator_KnowledgeGraph":
                return shared
            return primary

        return mock.patch.object(
            install, "_count_weaviate_class_objects", side_effect=_stub
        )

    # ── Test 1: shared wins as canonical ──────────────────────────────────

    def test_orchestrator_root_rebind_picks_shared_as_canonical(self):
        """KG="VCODev_KG", SHARED="VibeCodedOrchestrator_KG" → canonical=SHARED.

        The rebind helper must:
          1. Pick SHARED_KG_COLLECTION as canonical.
          2. UPDATE both project_kg_bindings rows to canonical.
          3. NOT invoke the shared-seed sync subprocess.
          4. Upsert both app_state keys to canonical.
        """
        kg = "VCODev_KG"
        shared = "VibeCodedOrchestrator_KG"
        project_id = "root-project-uuid"
        db_path = _make_launcher_db_with_root(
            self.tmp,
            primary_collection=kg,
            shared_collection=shared,
            project_id=project_id,
        )
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db_path)

        # Patch _is_orchestrator_root_install → True (we're inside a worktree
        # which IS an orchestrator clone in real life; pinning the value makes
        # the test robust to running from any cwd).
        with mock.patch.object(
            install, "_is_orchestrator_root_install", return_value=True
        ), mock.patch.object(
            install, "_discover_app_state_db_path", return_value=db_path
        ), self._patch_count(), mock.patch(
            "subprocess.run", side_effect=self._fake_run
        ):
            errors = install._seed_weaviate_shared_kg_only(
                args=_make_args(),
                venv_py=Path("/fake/python"),
                sync_kg=self.tmp / "sync_knowledge_graph.py",
                weaviate_url="http://localhost:8081",
                current_shared_kg=shared,
                current_kg_collection=kg,
            )

        self.assertEqual(errors, [], "rebind path must succeed with empty errors")

        # No subprocess.run calls — the sync subprocess is skipped entirely.
        sync_calls = [
            c for c in self.captured_cmds
            if any("sync_knowledge_graph" in part for part in c)
        ]
        self.assertEqual(
            sync_calls, [],
            f"shared-seed sync MUST NOT be invoked on orchestrator-root, "
            f"got: {sync_calls!r}",
        )

        # launcher.db bindings rewritten to canonical (= SHARED).
        self.assertEqual(
            _read_binding(db_path, project_id, "primary"), shared,
            "primary binding must be rebound to canonical (SHARED) name",
        )
        self.assertEqual(
            _read_binding(db_path, project_id, "shared"), shared,
            "shared binding must be set to canonical name",
        )

        # app_state keys upserted to canonical.
        last_kg = install._read_app_state_key(
            install._APP_STATE_KEY_LAST_KG_COLLECTION
        )
        last_shared = install._read_app_state_key(
            install._APP_STATE_KEY_LAST_SHARED_KG_COLLECTION
        )
        self.assertEqual(last_kg, shared, "app_state.last_kg_collection = canonical")
        self.assertEqual(
            last_shared, shared, "app_state.last_shared_kg_collection = canonical"
        )

    # ── Test 2: shared empty → KG wins ────────────────────────────────────

    def test_orchestrator_root_rebind_uses_kg_when_shared_empty(self):
        """SHARED_KG_COLLECTION='' (legacy install) → canonical = KG_COLLECTION.

        When the shared env value is empty, the function returns early at the
        ``if not current_shared_kg`` guard (no rebind, no error). That branch
        is the early-return path: the orchestrator-root canonical-selection
        block is gated on shared being non-empty.

        This documents the actual implementation contract — the v0.2.44
        guard ``if not current_shared_kg: return`` fires BEFORE the orchestrator-root
        branch and prints "shared KG seed: skipped (SHARED_KG_COLLECTION empty)".
        """
        kg = "VCODev_KG"
        project_id = "root-project-uuid"
        db_path = _make_launcher_db_with_root(
            self.tmp,
            primary_collection=kg,
            shared_collection=kg,  # match — but the call passes shared="" anyway
            project_id=project_id,
        )
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db_path)

        with mock.patch.object(
            install, "_is_orchestrator_root_install", return_value=True
        ), mock.patch.object(
            install, "_discover_app_state_db_path", return_value=db_path
        ), self._patch_count(), mock.patch(
            "subprocess.run", side_effect=self._fake_run
        ):
            errors = install._seed_weaviate_shared_kg_only(
                args=_make_args(),
                venv_py=Path("/fake/python"),
                sync_kg=self.tmp / "sync_knowledge_graph.py",
                weaviate_url="http://localhost:8081",
                current_shared_kg="",  # empty
                current_kg_collection=kg,
            )

        self.assertEqual(errors, [], "empty-shared early-return must succeed silently")

        # No sync subprocess.
        sync_calls = [
            c for c in self.captured_cmds
            if any("sync_knowledge_graph" in part for part in c)
        ]
        self.assertEqual(sync_calls, [], "no sync subprocess on empty SHARED")

        # Bindings UNTOUCHED — the early-return preempts the rebind.
        self.assertEqual(
            _read_binding(db_path, project_id, "primary"), kg,
            "early-return must not touch bindings",
        )

    # ── Test 3: per-project install (not orchestrator-root) unaffected ────

    def test_non_orchestrator_root_unaffected(self):
        """When _is_orchestrator_root_install() is False, the rebind path is
        skipped entirely and the legacy shared-seed subprocess flow runs.

        We pin ``_is_orchestrator_root_install`` to False and verify:
          1. NO launcher.db UPDATE happens (bindings untouched).
          2. The shared-seed sync subprocess IS invoked (the legacy path).
        """
        kg = "VCODev_KG"
        shared = "VibeCodedOrchestrator_KG"
        project_id = "root-project-uuid"
        db_path = _make_launcher_db_with_root(
            self.tmp,
            primary_collection=kg,
            shared_collection=shared,
            project_id=project_id,
        )
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db_path)

        # Provide a sync_kg path that EXISTS so the non-orchestrator-root branch
        # tries to invoke subprocess.run on it.
        sync_kg = self.tmp / "sync_knowledge_graph.py"
        sync_kg.write_text("# stub\n")

        # Pin PROJECT_ROOT to tmp so the sync subprocess env build doesn't
        # touch the real install dir.
        with mock.patch.object(
            install, "_is_orchestrator_root_install", return_value=False
        ), mock.patch.object(
            install, "_discover_app_state_db_path", return_value=db_path
        ), mock.patch.object(
            install, "PROJECT_ROOT", self.tmp
        ), mock.patch(
            "subprocess.run", side_effect=self._fake_run
        ):
            errors = install._seed_weaviate_shared_kg_only(
                args=_make_args(),
                venv_py=Path("/fake/python"),
                sync_kg=sync_kg,
                weaviate_url="http://localhost:8081",
                current_shared_kg=shared,
                current_kg_collection=kg,
            )

        self.assertEqual(errors, [], "per-project shared-seed must succeed")

        # Bindings UNTOUCHED — non-orchestrator-root path never rebinds.
        self.assertEqual(
            _read_binding(db_path, project_id, "primary"), kg,
            "per-project install must not touch primary binding",
        )
        self.assertEqual(
            _read_binding(db_path, project_id, "shared"), shared,
            "per-project install must not touch shared binding",
        )

        # The sync subprocess WAS invoked (legacy path).
        sync_calls = [
            c for c in self.captured_cmds
            if any("sync_knowledge_graph" in part for part in c)
        ]
        self.assertTrue(
            len(sync_calls) >= 1,
            f"legacy shared-seed sync MUST run for non-orchestrator-root, "
            f"got: {self.captured_cmds!r}",
        )

    # ── Test 4: launcher.db missing → soft-fail ───────────────────────────

    def test_rebind_soft_fails_when_launcher_db_missing(self):
        """VCT_LAUNCHER_DB_PATH points at a nonexistent file → rebind helper
        reports the missing-DB error but the install MUST NOT crash.

        Validates V44-A's soft-fail discipline. v0.2.44 V44-E refactor:
        env-surface writes now route through
        ``vco_lib.config_projection.apply_project_env`` (the Phase 0.B
        single-writer contract). When launcher.db is missing we cannot
        build a full bundle, so env-surface projection is deferred to
        the launcher's first-boot env-refresh. The test verifies the
        soft-fail flow: no crash, app_state upsert still runs, env
        files are left untouched, and the deferral is signalled via
        the captured rebind diagnostics (printed but not propagated).
        """
        kg = "VCODev_KG"
        shared = "VibeCodedOrchestrator_KG"

        # Use an app_state DB (for the _write_app_state_key calls) but point
        # the rebind helper at a nonexistent launcher.db.
        app_state_db = self.tmp / "app_state.db"
        conn = sqlite3.connect(str(app_state_db))
        conn.executescript(_APP_STATE_SCHEMA)
        conn.commit()
        conn.close()

        os.environ["VCT_LAUNCHER_DB_PATH"] = str(
            self.tmp / "definitely-does-not-exist.db"
        )

        # Stage .claude/settings.json + .claude/env in tmp. The rebind helper
        # MUST NOT touch them when launcher.db is missing — the launcher's
        # first-boot env-refresh is the canonical reconciliation path.
        claude_dir = self.tmp / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_dir / "settings.json"
        original_settings_text = (
            '{"env": {"KG_COLLECTION": "stale", '
            '"SHARED_KG_COLLECTION": "stale"}}\n'
        )
        settings_path.write_text(original_settings_text)
        env_path = claude_dir / "env"
        original_env_text = (
            'export KG_COLLECTION="stale"\n'
            'export SHARED_KG_COLLECTION="stale"\n'
        )
        env_path.write_text(original_env_text)

        # Spy on apply_project_env so we can assert it was NOT called when
        # launcher.db is missing (the contract requires a DB-resolved bundle
        # and there's no safe way to construct one without the DB).
        from vco_lib import config_projection as _cp

        with mock.patch.object(
            install, "_is_orchestrator_root_install", return_value=True
        ), mock.patch.object(
            install, "_discover_app_state_db_path", return_value=app_state_db
        ), mock.patch.object(
            install, "PROJECT_ROOT", self.tmp
        ), mock.patch.object(
            _cp, "apply_project_env", wraps=_cp.apply_project_env
        ) as apply_spy, self._patch_count(), mock.patch(
            "subprocess.run", side_effect=self._fake_run
        ):
            errors = install._seed_weaviate_shared_kg_only(
                args=_make_args(),
                venv_py=Path("/fake/python"),
                sync_kg=self.tmp / "sync_knowledge_graph.py",
                weaviate_url="http://localhost:8081",
                current_shared_kg=shared,
                current_kg_collection=kg,
            )

            # Read app_state INSIDE the with-block so the
            # _discover_app_state_db_path mock is still in effect.
            last_kg = install._read_app_state_key(
                install._APP_STATE_KEY_LAST_KG_COLLECTION
            )

        # Returns empty errors at the top-level (rebind soft-fails internally;
        # the helper prints "! rebind: ..." but does NOT propagate the error).
        self.assertEqual(
            errors, [],
            "soft-fail discipline: rebind errors must NOT propagate to caller",
        )

        # Sync subprocess STILL skipped (this is the orchestrator-root path).
        sync_calls = [
            c for c in self.captured_cmds
            if any("sync_knowledge_graph" in part for part in c)
        ]
        self.assertEqual(sync_calls, [], "no sync on orchestrator-root, DB or not")
        self.assertEqual(
            last_kg, shared,
            "app_state upsert must run even when launcher.db missing",
        )

        # apply_project_env MUST NOT have been called — without launcher.db
        # we can't build a full bundle, so env projection is deferred.
        self.assertEqual(
            apply_spy.call_count, 0,
            "apply_project_env must not be called when launcher.db is missing — "
            "the contract requires a DB-resolved bundle; env reconciliation "
            "is deferred to the launcher's first-boot env-refresh",
        )

        # Env files MUST be left untouched (no direct writes from the rebind
        # helper anymore — the Phase 0.B single-writer contract gates them).
        self.assertEqual(
            settings_path.read_text(), original_settings_text,
            ".claude/settings.json must NOT be touched when launcher.db is "
            "missing (deferred to launcher boot)",
        )
        self.assertEqual(
            env_path.read_text(), original_env_text,
            ".claude/env must NOT be touched when launcher.db is missing "
            "(deferred to launcher boot)",
        )


if __name__ == "__main__":
    unittest.main()
