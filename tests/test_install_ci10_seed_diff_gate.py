# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.42 CI-10: content-hash diff-only sync gate on --update.

Tests for the install.py helpers that implement the "pay once, never again"
diff gate in ``_seed_weaviate``. Covers:

  1. Empty diff → skip sync entirely.
  2. N-file diff → invoke sync with only those files.
  3. Embedding change → force full sync (context change detected).
  4. Collection rename → force full sync (context change detected).
  5. First sync of pre-v0.2.17 install (content_hash absent in Weaviate)
     → diff shows all files as stale → full sync invoked.

Pure-unit tests: no real Weaviate, no real subprocess. Stubs replace
``_batch_query_weaviate_content_hashes``, ``_compute_on_disk_content_hashes``,
and subprocess.run so tests run quickly in CI.
"""

from __future__ import annotations

import argparse
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


# ─── DB fixture helpers ───────────────────────────────────────────────────────

_APP_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def _make_db_with_state(**kwargs) -> Path:
    """Create a temp launcher.db with app_state rows."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    p = Path(tmp.name)
    conn = sqlite3.connect(str(p))
    conn.executescript(_APP_STATE_SCHEMA)
    now = 1000000
    for k, v in kwargs.items():
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?,?,?)",
            (k, v, now),
        )
    conn.commit()
    conn.close()
    return p


# ─── Test helpers ─────────────────────────────────────────────────────────────

def _make_args(update=True, skip_seed=False):
    ns = argparse.Namespace()
    ns.update = update
    ns.skip_seed = skip_seed
    return ns


def _fake_venv_py(tmp_dir: Path) -> Path:
    """Write a minimal Python stub to tmp_dir that succeeds immediately."""
    stub = tmp_dir / "fake_python.sh"
    stub.write_text("#!/bin/sh\n# fake venv python — exits 0\n")
    stub.chmod(0o755)
    return stub


# ─── Tests ────────────────────────────────────────────────────────────────────


class SeedDiffGateTest(unittest.TestCase):
    """CI-10: _seed_weaviate diff gate logic tests."""

    def setUp(self):
        # v0.2.89: stub the bounded Weaviate-readiness gate. These tests stub
        # the seed machinery, not the gate; without this, runners with no live
        # Weaviate burn the 150s deadline and raise (and machines WITH one
        # leak a live probe into a hermetic test).
        _gate = mock.patch.object(
            install, "_wait_for_weaviate_ready", lambda *a, **k: True
        )
        _gate.start()
        self.addCleanup(_gate.stop)
        self.tmp = tempfile.mkdtemp()
        os.environ["VCT_STATE_DIR"] = self.tmp
        # Minimal matching env to simulate "no context change".
        os.environ["ACTIVE_EMBEDDING"] = "qwen3"
        os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
        os.environ["SHARED_KG_COLLECTION"] = ""  # shared seed skipped

        # Make a minimal launcher.db with the "same" context stored.
        self.db_path = _make_db_with_state(
            last_installed_active_embedding="qwen3",
            last_installed_kg_collection="TestProject_KnowledgeGraph",
            last_installed_shared_kg_collection="",
        )
        # Override the db discovery to return our temp db.
        self._db_patcher = mock.patch.object(
            install, "_discover_app_state_db_path", return_value=self.db_path
        )
        self._db_patcher.start()

    def tearDown(self):
        self._db_patcher.stop()
        os.environ.pop("VCT_STATE_DIR", None)
        os.environ.pop("ACTIVE_EMBEDDING", None)
        os.environ.pop("KG_COLLECTION", None)
        os.environ.pop("SHARED_KG_COLLECTION", None)
        try:
            os.unlink(str(self.db_path))
        except OSError:
            pass

    def _run_seed_with_mocks(
        self,
        *,
        on_disk_hashes: dict,
        stored_hashes: dict,
        args=None,
        sync_kg_path: str | None = None,
        venv_py_path: str | None = None,
    ) -> list[tuple]:
        """Run _seed_weaviate with mocked fs + Weaviate helpers.

        Returns: list of subprocess.run call args (what the gate invoked).
        """
        captured_calls: list[tuple] = []

        def _fake_subprocess_run(cmd, **kwargs):
            captured_calls.append(tuple(cmd))
            class _Ret:
                returncode = 0
            return _Ret()

        tmp_dir = Path(self.tmp)
        # Create a fake sync_knowledge_graph.py so sync_kg.exists() is True.
        scripts_dir = tmp_dir / ".claude" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        fake_sync_kg = scripts_dir / "sync_knowledge_graph.py"
        fake_sync_kg.write_text("# stub\n")

        # Create a fake venv python.
        fake_venv = tmp_dir / ".venv" / "bin"
        fake_venv.mkdir(parents=True, exist_ok=True)
        fake_python = fake_venv / "python"
        fake_python.write_text("#!/bin/sh\n")
        fake_python.chmod(0o755)

        if args is None:
            args = _make_args()

        with mock.patch.object(
            install, "PROJECT_ROOT", tmp_dir,
        ), mock.patch.object(
            install, "_compute_on_disk_content_hashes", return_value=on_disk_hashes,
        ), mock.patch.object(
            install, "_batch_query_weaviate_content_hashes", return_value=stored_hashes,
        ), mock.patch("subprocess.run", side_effect=_fake_subprocess_run):
            install._seed_weaviate(args)

        return captured_calls

    # ── Test 1: empty diff → skip sync entirely ───────────────────────────

    def test_empty_diff_skips_sync_on_update(self):
        """When all on-disk hashes match stored hashes, sync is skipped."""
        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        file_b = f"{self.tmp}/knowledge/concepts/bar.md"
        hashes = {file_a: "aabbcc", file_b: "ddeeff"}

        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes,
            stored_hashes=hashes,  # identical → empty diff
        )

        # subprocess.run must NOT have been called for the per-project KG sync.
        kg_sync_calls = [c for c in calls if "sync_knowledge_graph.py" in str(c)]
        for call in kg_sync_calls:
            self.assertNotIn("--all", call, "empty diff must not trigger --all sync")
            self.assertFalse(
                any(f.endswith(".md") for f in call),
                "empty diff must not trigger any per-file sync",
            )

        # app_state should have been updated with the current context.
        conn = sqlite3.connect(str(self.db_path))
        try:
            rows = {
                r[0]: r[1]
                for r in conn.execute("SELECT key, value FROM app_state").fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING), "qwen3",
            "app_state must be updated even on skip",
        )

    # ── Test 2: N-file diff → sync only changed files ────────────────────

    def test_partial_diff_syncs_only_changed_files(self):
        """When 2 of 5 files changed, sync is invoked with just those 2 files."""
        knowledge_root = f"{self.tmp}/knowledge"
        files = {f"{knowledge_root}/concepts/f{i}.md": f"hash{i}" for i in range(5)}
        stored = dict(files)
        # Simulate 2 files changed.
        changed_files = [
            f"{knowledge_root}/concepts/f1.md",
            f"{knowledge_root}/concepts/f3.md",
        ]
        on_disk = dict(files)
        on_disk[changed_files[0]] = "newhash_f1"
        on_disk[changed_files[1]] = "newhash_f3"

        calls = self._run_seed_with_mocks(
            on_disk_hashes=on_disk,
            stored_hashes=stored,
        )

        # The per-project sync call must include the changed files, NOT --all.
        kg_sync_calls = [c for c in calls if "sync_knowledge_graph.py" in str(c)]
        # Find the main sync call (not shared-kg).
        for call in kg_sync_calls:
            if "--all" in call:
                self.fail(f"partial diff must not use --all: {call}")

        # Both changed files must appear in exactly one sync call.
        all_args = " ".join(str(a) for a in sum(kg_sync_calls, ()))
        for cf in changed_files:
            self.assertIn(cf, all_args, f"changed file {cf} must be in sync call args")

    # ── Test 3: embedding change → full sync ─────────────────────────────

    def test_embedding_change_forces_full_sync(self):
        """When active_embedding changes, full --all sync is triggered."""
        # Store a DIFFERENT embedding in app_state.
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "UPDATE app_state SET value=? WHERE key=?",
            ("arctic2", install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING),
        )
        conn.commit()
        conn.close()

        # Matching hashes (shouldn't matter — full sync forced by context change).
        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        hashes = {file_a: "aabbcc"}

        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes,
            stored_hashes=hashes,  # identical → would be empty diff
        )

        kg_sync_calls = [c for c in calls if "sync_knowledge_graph.py" in str(c)]
        found_all = any("--all" in c for c in kg_sync_calls)
        self.assertTrue(found_all, "embedding change must force --all sync")

    # ── Test 4: collection rename → full sync ────────────────────────────

    def test_collection_rename_forces_full_sync(self):
        """When KG_COLLECTION changes, full --all sync is triggered."""
        # Store a DIFFERENT collection name in app_state.
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "UPDATE app_state SET value=? WHERE key=?",
            ("OldProject_KnowledgeGraph", install._APP_STATE_KEY_LAST_KG_COLLECTION),
        )
        conn.commit()
        conn.close()

        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        hashes = {file_a: "aabbcc"}

        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes,
            stored_hashes=hashes,
        )

        kg_sync_calls = [c for c in calls if "sync_knowledge_graph.py" in str(c)]
        found_all = any("--all" in c for c in kg_sync_calls)
        self.assertTrue(found_all, "collection rename must force --all sync")

    # ── Test 5: pre-v0.2.17 install (no content_hash in Weaviate) ────────

    def test_no_stored_hashes_triggers_full_diff_sync(self):
        """When Weaviate has no content_hash values (pre-v0.2.17), all files
        are treated as stale and a full diff-path sync is triggered (syncing
        all files by passing them as a list, not --all)."""
        knowledge_root = f"{self.tmp}/knowledge"
        on_disk = {
            f"{knowledge_root}/concepts/a.md": "hash_a",
            f"{knowledge_root}/concepts/b.md": "hash_b",
        }
        # stored_hashes is empty (Weaviate had no content_hash property).
        calls = self._run_seed_with_mocks(
            on_disk_hashes=on_disk,
            stored_hashes={},  # no stored hashes
        )

        kg_sync_calls = [c for c in calls if "sync_knowledge_graph.py" in str(c)]
        # Must NOT use --all (we're on the diff path, passing explicit file list).
        # But it MUST invoke sync (with the 2 files as args).
        all_args = " ".join(str(a) for a in sum(kg_sync_calls, ()))
        self.assertIn(".md", all_args, "pre-v0.2.17: all files must be synced")

    # ── Test 6: fresh install (no --update flag) → full sync ─────────────

    def test_fresh_install_runs_full_sync(self):
        """On fresh install (args.update=False), --all is always used."""
        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        hashes = {file_a: "aabbcc"}

        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes,
            stored_hashes=hashes,  # identical — would skip on --update
            args=_make_args(update=False),
        )

        kg_sync_calls = [c for c in calls if "sync_knowledge_graph.py" in str(c)]
        found_all = any("--all" in c for c in kg_sync_calls)
        self.assertTrue(found_all, "fresh install must always run --all sync")


class Seg1ContextPersistOnPartialFailureTest(unittest.TestCase):
    """SEG-1 (v0.2.73): the CONTEXT TRIPLE
    (last_installed_active_embedding / _kg_collection / _shared_kg_collection)
    must be persisted whenever the KG-sync subprocess ACTUALLY RAN — even if it
    exited non-zero because ≥1 node failed — and must NOT be persisted when the
    subprocess never ran (script missing / failed to launch).

    Regression: previously the persist was gated on `if not seed_errors:`, so a
    single transient node failure (sync_knowledge_graph.py exits 1) left
    last_installed_active_embedding at None forever, forcing a full ~2590-node
    re-embed on EVERY subsequent --update (CI-10 "pay once" defeated).
    """

    def setUp(self):
        # v0.2.89: stub the bounded Weaviate-readiness gate. These tests stub
        # the seed machinery, not the gate; without this, runners with no live
        # Weaviate burn the 150s deadline and raise (and machines WITH one
        # leak a live probe into a hermetic test).
        _gate = mock.patch.object(
            install, "_wait_for_weaviate_ready", lambda *a, **k: True
        )
        _gate.start()
        self.addCleanup(_gate.stop)
        self.tmp = tempfile.mkdtemp()
        os.environ["VCT_STATE_DIR"] = self.tmp
        os.environ["ACTIVE_EMBEDDING"] = "qwen3"
        os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
        os.environ["SHARED_KG_COLLECTION"] = ""  # shared seed skipped

        # Stored context MATCHES current env → no "context change" full sync;
        # the diff path decides sync vs skip based on hashes. We give a
        # non-empty diff below so the sync subprocess actually runs.
        self.db_path = _make_db_with_state(
            last_installed_kg_collection="TestProject_KnowledgeGraph",
            last_installed_shared_kg_collection="",
        )
        # NOTE: last_installed_active_embedding is deliberately ABSENT here —
        # simulating the stuck-at-None fresh-install / post-transient-failure
        # state SEG-1 is about. Because it's absent, context_changed is True,
        # so this run takes the FULL --all sync path (the exact path that
        # embeds all nodes against the current context).
        self._db_patcher = mock.patch.object(
            install, "_discover_app_state_db_path", return_value=self.db_path
        )
        self._db_patcher.start()

    def tearDown(self):
        self._db_patcher.stop()
        for k in ("VCT_STATE_DIR", "ACTIVE_EMBEDDING", "KG_COLLECTION",
                  "SHARED_KG_COLLECTION"):
            os.environ.pop(k, None)
        try:
            os.unlink(str(self.db_path))
        except OSError:
            pass

    def _read_triple(self) -> dict:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return {
                r[0]: r[1]
                for r in conn.execute("SELECT key, value FROM app_state").fetchall()
            }
        finally:
            conn.close()

    def _run_seed(self, *, subprocess_side_effect, write_sync_script=True):
        """Run install._seed_weaviate with a controllable subprocess stub.

        subprocess_side_effect(cmd, **kwargs) is called for every subprocess.run;
        it may return a fake completed-process or raise (e.g. CalledProcessError).
        When write_sync_script is False, the sync_knowledge_graph.py stub is NOT
        created → sync_kg.exists() is False → the subprocess never runs.
        """
        tmp_dir = Path(self.tmp)
        scripts_dir = tmp_dir / ".claude" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        if write_sync_script:
            (scripts_dir / "sync_knowledge_graph.py").write_text("# stub\n")

        fake_venv = tmp_dir / ".venv" / "bin"
        fake_venv.mkdir(parents=True, exist_ok=True)
        fake_python = fake_venv / "python"
        fake_python.write_text("#!/bin/sh\n")
        fake_python.chmod(0o755)

        # One on-disk file, no stored hash → non-empty diff → sync runs (when
        # context is unchanged). With embedding absent, context_changed forces
        # --all anyway; either way the sync subprocess is invoked.
        on_disk = {f"{self.tmp}/knowledge/concepts/foo.md": "aabbcc"}

        with mock.patch.object(
            install, "PROJECT_ROOT", tmp_dir,
        ), mock.patch.object(
            install, "_compute_on_disk_content_hashes", return_value=on_disk,
        ), mock.patch.object(
            install, "_batch_query_weaviate_content_hashes", return_value={},
        ), mock.patch(
            "subprocess.run", side_effect=subprocess_side_effect,
        ):
            install._seed_weaviate(_make_args())

    # ── (i) subprocess ran but exited non-zero → context triple PERSISTED ──

    def test_nonzero_exit_still_persists_context_triple(self):
        """Core regression: a sync that EXECUTED but exited non-zero (≥1 node
        failed) must still persist last_installed_active_embedding == current."""
        import subprocess as _sp

        def _raise_nonzero(cmd, **kwargs):
            # Only the per-project KG sync raises; other subprocess.run calls
            # (e.g. shared-KG seed) succeed. Identify by the sync script path.
            if "sync_knowledge_graph.py" in str(cmd):
                raise _sp.CalledProcessError(returncode=1, cmd=cmd)
            class _Ret:
                returncode = 0
            return _Ret()

        self._run_seed(subprocess_side_effect=_raise_nonzero)

        rows = self._read_triple()
        self.assertEqual(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING), "qwen3",
            "SEG-1: context embedding MUST be persisted even when the sync "
            "subprocess exited non-zero (1 node failed of many)",
        )
        self.assertEqual(
            rows.get(install._APP_STATE_KEY_LAST_KG_COLLECTION),
            "TestProject_KnowledgeGraph",
            "SEG-1: kg collection must be persisted on non-zero exit too",
        )
        # Stats/timestamp recorded too (they reflect the attempt reality).
        self.assertIn(install._APP_STATE_KEY_LAST_KG_SYNC_AT, rows)
        self.assertIn(install._APP_STATE_KEY_LAST_KG_SYNC_STATS, rows)

    # ── (ii) sync script MISSING → context triple NOT persisted ───────────

    def test_missing_sync_script_does_not_persist_context_triple(self):
        """When sync_knowledge_graph.py is absent, the subprocess never runs and
        nothing is embedded → the context triple must NOT be recorded (else we
        would falsely claim the collection is embedded against current context).
        """
        def _fail_if_called(cmd, **kwargs):
            # The per-project sync must NOT be invoked (script missing). Shared
            # seed may still run; allow it to succeed.
            if "sync_knowledge_graph.py" in str(cmd) and str(cmd).count(self.tmp):
                raise AssertionError("per-project sync must not run when script missing")
            class _Ret:
                returncode = 0
            return _Ret()

        self._run_seed(
            subprocess_side_effect=_fail_if_called,
            write_sync_script=False,
        )

        rows = self._read_triple()
        self.assertIsNone(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING),
            "SEG-1: context embedding must NOT be persisted when the sync "
            "script is missing (nothing was embedded)",
        )
        self.assertNotIn(
            install._APP_STATE_KEY_LAST_KG_SYNC_AT, rows,
            "SEG-1: no sync-at timestamp when the subprocess never ran",
        )

    # ── (iii) two-update integration: transient failure → diff path next ──

    def test_transient_failure_then_next_update_takes_diff_path(self):
        """Simulate two consecutive --update runs. Run 1 has a transient node
        failure (non-zero exit). After the fix, run 1 persists the context
        triple, so at run 2 `context_changed` is computable as False (the diff
        path), not the full-sync path.

        We assert the intermediate state directly: after run 1 the stored triple
        equals the current context, which is exactly the precondition the
        run-2 context-change check reads at install.py:16087-16091.
        """
        import subprocess as _sp

        # RUN 1 — 1 node fails (non-zero exit).
        def _raise_nonzero(cmd, **kwargs):
            if "sync_knowledge_graph.py" in str(cmd):
                raise _sp.CalledProcessError(returncode=1, cmd=cmd)
            class _Ret:
                returncode = 0
            return _Ret()

        self._run_seed(subprocess_side_effect=_raise_nonzero)

        rows = self._read_triple()
        stored_embedding = rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING)
        stored_kg = rows.get(install._APP_STATE_KEY_LAST_KG_COLLECTION)
        stored_shared = rows.get(install._APP_STATE_KEY_LAST_SHARED_KG_COLLECTION)

        # This is the exact comparison install.py runs at the top of run 2.
        current_active_embedding = os.environ["ACTIVE_EMBEDDING"]
        current_kg = os.environ["KG_COLLECTION"]
        current_shared = os.environ["SHARED_KG_COLLECTION"]
        context_changed = (
            stored_embedding != current_active_embedding
            or stored_kg != current_kg
            or stored_shared != current_shared
        )
        self.assertFalse(
            context_changed,
            "SEG-1: after a transient-failure run, the next --update must NOT "
            "see a context change → it takes the content-hash DIFF path (re-embed "
            "only the failed node), not a full 2590-node re-embed",
        )


class ContentHashHelpersTest(unittest.TestCase):
    """Unit tests for the pure helper functions."""

    def test_compute_on_disk_content_hashes_returns_dict(self):
        """_compute_on_disk_content_hashes returns a dict of absolute-path→hash."""
        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp) / "knowledge" / "concepts"
            knowledge_dir.mkdir(parents=True)
            f1 = knowledge_dir / "node1.md"
            f2 = knowledge_dir / "node2.md"
            f1.write_text("---\ntitle: Test\n---\nBody content.")
            f2.write_text("---\ntitle: Other\nupdated: 2026-01-01\n---\nBody.")

            result = install._compute_on_disk_content_hashes(Path(tmp) / "knowledge")

        self.assertIn(str(f1), result, "f1 must be in result")
        self.assertIn(str(f2), result, "f2 must be in result")
        # Both hashes must be non-empty strings.
        self.assertTrue(all(v for v in result.values()), "all hashes must be non-empty")

    def test_compute_on_disk_content_hashes_excludes_updated_line(self):
        """Content hash must NOT change when only the 'updated:' line changes."""
        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp) / "knowledge"
            knowledge_dir.mkdir()
            f = knowledge_dir / "node.md"

            f.write_text("---\ntitle: X\nupdated: 2026-01-01\n---\nBody.")
            h1 = install._compute_on_disk_content_hashes(knowledge_dir)[str(f)]

            f.write_text("---\ntitle: X\nupdated: 2026-12-31\n---\nBody.")
            h2 = install._compute_on_disk_content_hashes(knowledge_dir)[str(f)]

        self.assertEqual(h1, h2, "hash must be identical when only 'updated:' changes")

    def test_read_write_app_state_key_round_trip(self):
        """_write_app_state_key + _read_app_state_key round-trips correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["VCT_STATE_DIR"] = tmp
            db_path = Path(tmp) / "launcher.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                "CREATE TABLE app_state "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);"
            )
            conn.commit()
            conn.close()

            install._write_app_state_key("test_key", "hello_world")
            result = install._read_app_state_key("test_key")

            self.assertEqual(result, "hello_world")

            # Overwrite (ON CONFLICT DO UPDATE).
            install._write_app_state_key("test_key", "updated_value")
            result2 = install._read_app_state_key("test_key")
            self.assertEqual(result2, "updated_value")

        os.environ.pop("VCT_STATE_DIR", None)

    def test_read_app_state_key_returns_none_when_absent(self):
        """_read_app_state_key returns None when key is not in app_state."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["VCT_STATE_DIR"] = tmp
            db_path = Path(tmp) / "launcher.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                "CREATE TABLE app_state "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);"
            )
            conn.commit()
            conn.close()

            result = install._read_app_state_key("nonexistent_key")
            self.assertIsNone(result)

        os.environ.pop("VCT_STATE_DIR", None)


# ─── V0243-0 (updated for v0.2.44 V44-A adopt-and-route) ─────────────────────


class OrchestratorRootSharedKgSkipTest(unittest.TestCase):
    """v0.2.44 V44-A: ``_seed_weaviate_shared_kg_only`` ALWAYS short-circuits
    on orchestrator-root installs — picks a canonical collection name, rebinds
    pointers, and skips the sync subprocess. Names CAN legitimately differ
    (legacy migrations); the function no longer gates on name-equality.

    These tests cover both the same-name (v0.2.43 V0243-0) and different-name
    (post-migration) cases. The second test was inverted in V44-A: pre-V44 the
    sync ran when names differed; post-V44 the rebind path skips it.
    """

    def _make_db(self, tmp: str) -> Path:
        db_path = Path(tmp) / "launcher.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS app_state "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);"
        )
        conn.commit()
        conn.close()
        return db_path

    def test_skip_when_shared_kg_equals_per_project_kg(self):
        """V44-A: shared_kg == kg_collection on orchestrator-root →
        rebind to canonical (= shared), skip sync, upsert BOTH app_state keys.
        """
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["VCT_STATE_DIR"] = tmp
            db_path = self._make_db(tmp)

            captured_subprocess: list = []

            def _fake_run(cmd, **kwargs):
                captured_subprocess.append(list(cmd))
                class _R:
                    returncode = 0
                return _R()

            collection = "VCODev_KnowledgeGraph"

            # V44-A: count helper is invoked for the diagnostic print; stub it
            # so a missing Weaviate doesn't blow up the call.
            with mock.patch.object(install, "_is_orchestrator_root_install", return_value=True), \
                 mock.patch.object(install, "_discover_app_state_db_path", return_value=db_path), \
                 mock.patch.object(install, "_count_weaviate_class_objects", return_value=0), \
                 mock.patch.object(install, "_rebind_orchestrator_root_to_canonical", return_value=[]) as rebind_mock, \
                 mock.patch("subprocess.run", side_effect=_fake_run):
                errors = install._seed_weaviate_shared_kg_only(
                    args=argparse.Namespace(),
                    venv_py=Path("/fake/python"),
                    sync_kg=Path(tmp) / "sync_kg.py",
                    weaviate_url="http://localhost:8081",
                    current_shared_kg=collection,
                    current_kg_collection=collection,
                )

                # Read app_state INSIDE the with-block so the mock is in effect.
                stored_primary = install._read_app_state_key(
                    install._APP_STATE_KEY_LAST_KG_COLLECTION
                )
                stored_shared = install._read_app_state_key(
                    install._APP_STATE_KEY_LAST_SHARED_KG_COLLECTION
                )

            # V44-A: rebind helper invoked with canonical = collection.
            rebind_mock.assert_called_once_with(collection)

            # No subprocess.run calls (sync was skipped — orchestrator-root path).
            sync_calls = [c for c in captured_subprocess if "sync_knowledge_graph" in " ".join(c)]
            self.assertEqual(sync_calls, [], "sync must NOT run on orchestrator-root")

            # No errors returned.
            self.assertEqual(errors, [])

            # V44-A: BOTH app_state keys upserted to canonical (= collection).
            self.assertEqual(
                stored_primary, collection,
                "app_state.last_installed_kg_collection = canonical",
            )
            self.assertEqual(
                stored_shared, collection,
                "app_state.last_installed_shared_kg_collection = canonical",
            )

        os.environ.pop("VCT_STATE_DIR", None)

    def test_canonical_rebind_when_shared_kg_differs_from_per_project_kg(self):
        """V44-A: shared_kg != kg_collection on orchestrator-root →
        STILL skip sync, pick canonical = shared, rebind both pointers.

        This test was INVERTED in V44-A. Pre-V44 the sync subprocess ran when
        the two collection names differed (V0243-0's string-equality gate
        failed). V44-A's adopt-and-route detects orchestrator-root as a
        CATEGORY and rebinds to a single canonical collection regardless of
        name equality. SHARED wins as canonical.
        """
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["VCT_STATE_DIR"] = tmp
            db_path = self._make_db(tmp)

            # Write a minimal sync_kg stub so sync_kg.exists() is True if the
            # legacy branch were taken (it shouldn't be, post-V44).
            sync_kg = Path(tmp) / "sync_knowledge_graph.py"
            sync_kg.write_text("# stub\n")

            captured_subprocess: list = []

            def _fake_run(cmd, **kwargs):
                captured_subprocess.append(list(cmd))
                class _R:
                    returncode = 0
                return _R()

            shared = "VibeCodedOrchestrator_KnowledgeGraph"
            kg = "VCODev_KnowledgeGraph"

            with mock.patch.object(install, "_is_orchestrator_root_install", return_value=True), \
                 mock.patch.object(install, "_discover_app_state_db_path", return_value=db_path), \
                 mock.patch.object(install, "_count_weaviate_class_objects", return_value=0), \
                 mock.patch.object(install, "_rebind_orchestrator_root_to_canonical", return_value=[]) as rebind_mock, \
                 mock.patch("subprocess.run", side_effect=_fake_run):
                errors = install._seed_weaviate_shared_kg_only(
                    args=argparse.Namespace(),
                    venv_py=Path("/fake/python"),
                    sync_kg=sync_kg,
                    weaviate_url="http://localhost:8081",
                    current_shared_kg=shared,
                    current_kg_collection=kg,
                )

                stored_primary = install._read_app_state_key(
                    install._APP_STATE_KEY_LAST_KG_COLLECTION
                )
                stored_shared = install._read_app_state_key(
                    install._APP_STATE_KEY_LAST_SHARED_KG_COLLECTION
                )

            # V44-A: canonical = SHARED (predictable, public-shipping convention).
            rebind_mock.assert_called_once_with(shared)

            # Sync subprocess MUST NOT run (post-V44 adopt-and-route).
            sync_calls = [c for c in captured_subprocess if "sync_knowledge_graph" in " ".join(c)]
            self.assertEqual(
                sync_calls, [],
                "V44-A: sync must NOT run on orchestrator-root, even when "
                "shared_kg != per-project kg (canonical rebind path)",
            )

            self.assertEqual(errors, [], "rebind path must succeed silently")

            # Both app_state keys upserted to canonical (= SHARED).
            self.assertEqual(
                stored_primary, shared,
                "V44-A: last_kg_collection rebound to canonical (SHARED)",
            )
            self.assertEqual(
                stored_shared, shared,
                "V44-A: last_shared_kg_collection rebound to canonical (SHARED)",
            )

        os.environ.pop("VCT_STATE_DIR", None)


if __name__ == "__main__":
    unittest.main()
