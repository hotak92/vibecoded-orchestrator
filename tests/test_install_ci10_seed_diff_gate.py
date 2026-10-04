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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    connect,
    create_empty_launcher_db,
    make_launcher_db,
)
from vco_lib import install_weaviate as _install_weaviate  # noqa: E402
from vco_lib import deferral_retry  # noqa: E402
import install  # noqa: E402


# ─── DB fixture helpers ───────────────────────────────────────────────────────


def _make_db_with_state(**kwargs) -> Path:
    """Create a temp launcher.db (REAL schema) with app_state rows."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return make_launcher_db(Path(tmp.name), app_state=kwargs)


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


def _fail_if_the_seed_runs_inline(cmd, **kwargs):
    """v0.2.101 item 4: the whole-tree seed is ENQUEUED, never awaited here.

    Any other child (enrichment, the shared seed) still succeeds, so a test
    opting into this stub only forbids the inline KG seed.
    """
    if "sync_knowledge_graph.py" in str(cmd):
        raise AssertionError(f"the whole-tree seed must not run inline: {cmd}")

    class _Ret:
        returncode = 0
        stdout = ""
        stderr = ""

    return _Ret()


def _fail_if_the_full_seed_runs_inline(cmd, **kwargs):
    """Only the WHOLE-TREE seed is forbidden; the small per-file diff stays
    foreground (v0.2.101 item 4 — the driver can only run `--all`)."""
    if "sync_knowledge_graph.py" in str(cmd) and "--all" in list(cmd):
        raise AssertionError(f"the whole-tree seed must not run inline: {cmd}")

    class _Ret:
        returncode = 0
        stdout = ""
        stderr = ""

    return _Ret()


def _isolate_service_endpoints(case) -> None:
    """Drop the `ollama_owed` flag a sibling test's down-path leaves behind.

    ``install._SERVICE_ENDPOINTS`` is a module global; install's own code sets
    ``ollama_owed`` inside it, and no monkeypatch tracks that key, so a test
    file exercising the Ollama-down path leaks it into every later test in the
    same process — ``_seed_weaviate`` then short-circuits ("Skipping the KG
    seed: Ollama is down") before this file's seed step runs. Pre-existing and
    not this release's: made harmless here so THIS file is order-independent.
    """
    import install as _install

    _install._SERVICE_ENDPOINTS.pop("ollama_owed", None)
    case.addCleanup(_install._SERVICE_ENDPOINTS.pop, "ollama_owed", None)


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
        _isolate_service_endpoints(self)
        # Minimal matching env to simulate "no context change".
        os.environ["ACTIVE_EMBEDDING"] = "qwen3"
        os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
        os.environ["SHARED_KG_COLLECTION"] = ""  # shared seed skipped

        # Make a minimal launcher.db with the "same" context stored.
        #
        # v0.2.95 WP-7: the repair stamp belongs in this fixture because every
        # test below asserts what a STEADY-STATE install does — one that has
        # already paid its one-time metadata-repair pass. Without it, leg (d)
        # legitimately forces `--all` and these four assertions read that as a
        # re-embed. The not-yet-stamped shape is the subject of
        # tests/test_v0295_kg_metadata_repair_trigger.py, not of this file.
        self.db_path = _make_db_with_state(
            last_installed_active_embedding="qwen3",
            last_installed_kg_collection="TestProject_KnowledgeGraph",
            last_installed_shared_kg_collection="",
            **{
                _install_weaviate.KG_METADATA_REPAIR_STATE_KEY:
                    _install_weaviate.KG_METADATA_REPAIR_STAMP,
            },
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
        enrichment_report: dict | None = None,
    ) -> list[tuple]:
        """Run _seed_weaviate with mocked fs + Weaviate helpers.

        Returns: list of subprocess.run call args (what the gate invoked).
            A detached spawn is recorded as ``("__SPAWN_DETACHED__", folder)``
            and its ``extra_env`` on ``self.spawns``.
        """
        captured_calls: list[tuple] = []
        self.spawns: list[dict] = []

        def _fake_spawn(folder, *, python="", extra_env=None):
            # v0.2.101 item 4: the whole-tree seed is ENQUEUED to the detached
            # driver instead of run inline; record it like any other call.
            self.spawns.append({"folder": Path(folder), "env": dict(extra_env or {})})
            captured_calls.append(("__SPAWN_DETACHED__", str(folder)))
            return True

        def _fake_subprocess_run(cmd, **kwargs):
            captured_calls.append(tuple(cmd))
            class _Ret:
                returncode = 0
                stdout = ""
                stderr = ""
            if "vco_lib.embedding_enrichment" in str(cmd):
                # v0.2.95 WP-6: the enrichment CLI answers with a JSON report
                # on stdout. `enrichment_report` lets a test choose the shape;
                # the default is a clean pass so the SUCCESS path is what a
                # test has to opt out of, not opt into.
                _Ret.stdout = json.dumps(
                    enrichment_report
                    if enrichment_report is not None
                    else {"total": 3, "enriched": 3, "skipped": 0,
                          "failed": 0, "failures": []}
                )
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
        ), mock.patch("subprocess.run", side_effect=_fake_subprocess_run), \
           mock.patch.object(
               # v0.2.96 WP-1: the sync child now spawns via the log-routing
               # helper; route it through the same fake so no real child
               # runs and the captures keep recording the seed argvs.
               install, "run_child_logged",
               side_effect=_fake_subprocess_run,
           ), mock.patch(
               # v0.2.101 item 4: the whole-tree seed leaves via this spawn.
               "vco_lib.deferral_retry.spawn_detached", side_effect=_fake_spawn,
           ):
            install._seed_weaviate(args)

        return captured_calls

    def assert_enqueued_full_seed(self, calls) -> None:
        """v0.2.101 item 4: the whole-tree seed left via the detached driver.

        The intent each caller pins ("a fresh install / a profile change / a
        rename must still cause the FULL seed") is unchanged; what changed is
        the transport — install.py enqueues it and returns instead of waiting
        for `sync_knowledge_graph.py --all`.
        """
        self.assertEqual(len(self.spawns), 1,
                         f"expected exactly one detached seed spawn: {calls}")
        self.assertEqual(
            self.spawns[0]["env"].get(deferral_retry.SEED_CTX_ENV_ACTIVE_EMBEDDING),
            os.environ["ACTIVE_EMBEDDING"],
            "the driver must carry the context the seed embeds against",
        )
        self.assertEqual(
            [c for c in calls if "sync_knowledge_graph.py" in str(c)], [],
            "the whole-tree seed must not be awaited inline",
        )

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
        conn = connect(self.db_path)
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

    def test_pure_slot_change_enriches_instead_of_re_embedding(self):
        """v0.2.95 WP-6: an embedding-model change is NOT a collection rename.

        Every row is still correct except for one empty named-vector slot,
        which `vco_lib.embedding_enrichment` fills per object, UPDATE-only and
        idempotently. Re-embedding the tree for it was the defect that punished
        the user who did the recommended thing: fill the slot from the launcher
        and the next `--update` used to re-embed everything anyway, because
        enrichment never writes `last_installed_active_embedding` (install.py
        is its only writer) so the context still read as changed.
        """
        install._write_app_state_key(
            install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING, "arctic2",
        )

        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        hashes = {file_a: "aabbcc"}

        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes,
            stored_hashes=hashes,  # identical → nothing to re-embed
        )

        enrich_calls = [
            c for c in calls if "vco_lib.embedding_enrichment" in str(c)
        ]
        self.assertTrue(
            enrich_calls,
            "a pure embedding-profile change must reach enrichment",
        )
        self.assertTrue(
            any("--new-slot" in c for c in enrich_calls),
            "enrichment must be told which slot to fill",
        )
        kg_sync_calls = [c for c in calls if "sync_knowledge_graph.py" in str(c)]
        self.assertFalse(
            [c for c in kg_sync_calls if "--all" in c],
            "after a successful in-place enrichment the tree must NOT be "
            "re-embedded — the content is unchanged and the slot is filled",
        )

        rows = {
            r[0]: r[1] for r in
            connect(self.db_path)
            .execute("SELECT key, value FROM app_state").fetchall()
        }
        self.assertEqual(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING), "qwen3",
            "the new profile must be STAMPED after a complete enrichment — "
            "leaving it stale is what made the next update re-embed again",
        )

    def test_enrichment_failure_falls_back_to_full_sync(self):
        """Partial enrichment must never be recorded as a finished change.

        Same conservative rule WP-4 applies to an incomplete leg-(b) sync: if
        the slot was not provably filled, do the expensive thing.
        """
        install._write_app_state_key(
            install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING, "arctic2",
        )
        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        hashes = {file_a: "aabbcc"}

        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes,
            stored_hashes=hashes,
            enrichment_report={"total": 3, "enriched": 1, "skipped": 0,
                               "failed": 2, "failures": []},
        )

        self.assert_enqueued_full_seed(calls)

    def test_absent_previous_profile_still_forces_full_sync(self):
        """No RECORDED profile is not a model change — it is no record.

        It is also what a never-seeded install looks like, and only `--all`
        seeds `docs/` (the per-file diff path covers `knowledge/` only), so
        this case deliberately keeps its pre-v0.2.95 path.
        """
        conn = connect(self.db_path)
        conn.execute(
            "DELETE FROM app_state WHERE key=?",
            (install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING,),
        )
        conn.commit()
        conn.close()

        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        hashes = {file_a: "aabbcc"}
        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes, stored_hashes=hashes,
        )

        self.assertFalse(
            [c for c in calls if "vco_lib.embedding_enrichment" in str(c)],
            "an absent record must not be treated as a slot change",
        )
        self.assert_enqueued_full_seed(calls)

    # ── Test 4: collection rename → full sync ────────────────────────────

    def test_collection_rename_forces_full_sync(self):
        """When KG_COLLECTION changes, full --all sync is triggered."""
        # Store a DIFFERENT collection name in app_state.
        conn = connect(self.db_path)
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

        self.assert_enqueued_full_seed(calls)
        # v0.2.95 WP-6: the half of leg (b) that enrichment CANNOT answer.
        # A renamed class has no rows to enrich — filling a slot on nothing
        # would report a clean pass and stamp a collection that is empty.
        self.assertFalse(
            [c for c in calls if "vco_lib.embedding_enrichment" in str(c)],
            "a collection rename must not be routed to enrichment",
        )

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
        """On fresh install (args.update=False), the whole-tree seed is owed.

        v0.2.101 item 4: it is ENQUEUED to the detached driver — which runs
        `sync_knowledge_graph.py --all` — instead of being awaited inline.
        """
        file_a = f"{self.tmp}/knowledge/concepts/foo.md"
        hashes = {file_a: "aabbcc"}

        calls = self._run_seed_with_mocks(
            on_disk_hashes=hashes,
            stored_hashes=hashes,  # identical — would skip on --update
            args=_make_args(update=False),
        )

        self.assert_enqueued_full_seed(calls)


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
        _isolate_service_endpoints(self)
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
        conn = connect(self.db_path)
        try:
            return {
                r[0]: r[1]
                for r in conn.execute("SELECT key, value FROM app_state").fetchall()
            }
        finally:
            conn.close()

    def _run_seed(self, *, subprocess_side_effect, write_sync_script=True,
                  deferral_report=None):
        """Run install._seed_weaviate with a controllable subprocess stub.

        subprocess_side_effect(cmd, **kwargs) is called for every subprocess.run;
        it may return a fake completed-process or raise (e.g. CalledProcessError).
        When write_sync_script is False, the sync_knowledge_graph.py stub is NOT
        created → sync_kg.exists() is False → the subprocess never runs.

        v0.2.95 WP-4: `deferral_report` is the run report install.py threads in
        so an INCOMPLETE context change can record owed work. Passing one makes
        the ledger side observable; omitting it keeps the pre-v0.2.95 call
        shape, which must still work.
        """
        tmp_dir = Path(self.tmp)
        self.spawns: list[dict] = []

        def _fake_spawn(folder, *, python="", extra_env=None):
            # v0.2.101 item 4: the whole-tree seed is enqueued, not awaited.
            self.spawns.append({"folder": Path(folder), "env": dict(extra_env or {})})
            return True

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
        ), mock.patch.object(
            # v0.2.96 WP-1: see _run_seed_with_mocks — the sync child now
            # spawns via run_child_logged.
            install, "run_child_logged", side_effect=subprocess_side_effect,
        ), mock.patch(
            # v0.2.101 item 4: the whole-tree seed leaves via this spawn.
            "vco_lib.deferral_retry.spawn_detached", side_effect=_fake_spawn,
        ):
            install._seed_weaviate(
                _make_args(), deferral_report=deferral_report,
            )

    # ── (i) leg (b), non-zero exit → context triple NOT advanced ─────────
    #
    # This is the one decision v0.2.95 WP-4 REVERSES, and the reversal is
    # narrow: it applies to leg (b) — the branch taken when the embedding
    # model or a collection NAME changed — and to nothing else.
    #
    # SEG-1 (v0.2.73) chose the opposite, and was right to, on the evidence it
    # had: gating the persist on success meant one transient node failure left
    # `last_installed_active_embedding` at None forever, and every subsequent
    # `--update` paid a full ~2590-node re-embed. Stamping was the cheaper
    # error. Its worked example — "2589 of 2590 succeeded; the next update's
    # content-hash diff re-picks-up only the failed node" — is still true for a
    # node whose WRITE failed: that node has no stored hash, so the diff finds
    # it. `test_leg_c_nonzero_exit_still_persists_context_triple` below pins it.
    #
    # What the exit code cannot distinguish is the OTHER leg-(b) failure: a run
    # that died early with most of the tree never visited. Those nodes keep
    # rows whose content_hash matches — the hash is computed from file bytes
    # and carries no model identity — so the diff sees nothing, and a stamped
    # triple retires leg (b) too. The collection then keeps previous-model
    # vectors permanently, unrecorded.
    #
    # The cost side of SEG-1's trade-off was removed by v0.2.95 WP-5: the
    # embed-skip now also requires the ACTIVE vector slot to be populated, so a
    # repeated leg-(b) `--all` re-embeds only what never landed. Cheap enough
    # that honesty wins.

    def test_leg_b_is_enqueued_and_install_does_not_advance_the_context_triple(self):
        """The context change is OWED, not recorded — v0.2.101 item 4.

        Pre-v0.2.101 install.py observed the child's exit code and withheld the
        triple on a non-zero one. It observes nothing now: the seed is
        enqueued, install.py writes NO triple, and the detached handler stamps
        it only after the child's own paired clear proves the seed ran against
        that context (pinned in tests/test_v02101_seed_and_data_keys.py).
        """
        self._run_seed(subprocess_side_effect=_fail_if_the_seed_runs_inline)

        rows = self._read_triple()
        self.assertIsNone(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING),
            "an ENQUEUED leg-(b) re-embed must not advance "
            "last_installed_active_embedding — the marker must not claim work "
            "that has not finished",
        )
        # Nothing ran in this process, so the attempt record is not written
        # either; the retry driver's own log/trail is where the run is visible.
        self.assertNotIn(install._APP_STATE_KEY_LAST_KG_SYNC_AT, rows)
        self.assertEqual(len(self.spawns), 1, "leg (b) must enqueue its full seed")
        self.assertEqual(
            self.spawns[0]["env"][deferral_retry.SEED_CTX_ENV_KG_COLLECTION],
            os.environ["KG_COLLECTION"],
            "the driver must carry the collection the seed targets",
        )

    def test_leg_b_enqueue_records_owed_work_that_can_clear(self):
        """A withheld marker the user cannot see is not a report.

        v0.2.101 item 4: the owed work is recorded by the ENQUEUE (the driver
        does the work), so the entry must exist the moment the install returns
        — and it must be one that PROVABLY clears: this project treats a
        deferral with no way out as a defect, and the registry is where that
        is checked.
        """
        from vco_lib import deferral_registry as _dr
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport()
        self._run_seed(
            subprocess_side_effect=_fail_if_the_seed_runs_inline,
            deferral_report=report,
        )
        self.assertEqual(len(self.spawns), 1, "the seed must be enqueued")
        self.assertIn(
            "deferral-retry-*.log",
            "".join(e.command_to_apply for e in report.entries),
            "the user must be told where the background seed's log is",
        )

        cids = [e.condition_id for e in report.entries]
        self.assertIn(
            install._SEED_OWED_WORK_CONDITION_ID, cids,
            "an incomplete context change left no ledger entry — the owed "
            "re-embed would be invisible to the user and to the doctor",
        )
        self.assertTrue(
            _dr.matches_registered_pattern(
                install._SEED_OWED_WORK_CONDITION_ID
            ),
            "the emitted condition id is not in the registry",
        )
        self.assertTrue(
            _dr.retry_handler_for(install._SEED_OWED_WORK_CONDITION_ID),
            "the entry is emitted as owed work but resolves to no retry "
            "handler, so the dispatcher cannot select it — the exact "
            "classification-mistaken-for-implementation state the registry "
            "exists to end",
        )
        self.assertEqual(
            _dr.clear_probe_for(install._SEED_OWED_WORK_CONDITION_ID),
            "paired-resolution",
            "an install.py-OWNED id would be dropped by any later install.py "
            "run that does not re-detect it, retiring the record while the "
            "embedding work is still owed",
        )

    def test_leg_b_a_proven_seed_advances_the_triple_through_the_handler(self):
        """The positive case: the context change IS recorded once it finished.

        v0.2.101 item 4: install.py no longer writes the triple — the detached
        handler does, after the child's own paired clear. This pins the leg-(b)
        half of that contract next to the negative one above: `stamp_seed_context`
        (fed the context the spawn carried) is what makes the next --update take
        the cheap diff path.
        """
        report = self._new_report()
        self._run_seed(
            subprocess_side_effect=_fail_if_the_seed_runs_inline,
            deferral_report=report,
        )

        # install.py itself must NOT have stamped it (see the test above)...
        self.assertIsNone(
            self._read_triple().get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING))
        # ...but the context the handler will stamp travelled with the spawn.
        carried = self.spawns[0]["env"]
        deferral_retry.stamp_seed_context(
            {
                "active_embedding": carried[deferral_retry.SEED_CTX_ENV_ACTIVE_EMBEDDING],
                "kg_collection": carried[deferral_retry.SEED_CTX_ENV_KG_COLLECTION],
                "shared_kg_collection": carried[
                    deferral_retry.SEED_CTX_ENV_SHARED_KG_COLLECTION],
            },
            write_key=install._write_app_state_key,
        )

        rows = self._read_triple()
        self.assertEqual(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING), "qwen3",
        )
        self.assertEqual(
            rows.get(install._APP_STATE_KEY_LAST_KG_COLLECTION),
            os.environ["KG_COLLECTION"],
        )
        # The owed row exists from the ENQUEUE (install.py cannot know yet
        # whether the background seed will succeed); the seed's own paired
        # clear is what retires it — the registry half of that is pinned by
        # test_leg_b_enqueue_records_owed_work_that_can_clear.
        self.assertIn(
            install._SEED_OWED_WORK_CONDITION_ID,
            [e.condition_id for e in report.entries],
        )

    def _new_report(self):
        from vco_lib.deferral_report import DeferralReport
        return DeferralReport()

    def test_leg_c_nonzero_exit_still_persists_context_triple(self):
        """SEG-1's guarantee, unchanged, on the leg it was argued for.

        Context UNCHANGED (stored triple == current), so this run takes the
        per-file diff path. A node fails; the subprocess exits non-zero. The
        triple and the attempt record are still written — and no owed-work
        entry is raised, because on this leg the next run's content-hash diff
        genuinely does re-pick-up the failed node.
        """
        import subprocess as _sp

        # Complete the stored triple so context_changed is False. Written
        # through the production upsert (the db path is already patched at it)
        # rather than raw SQL, so the row carries every column the real schema
        # requires.
        install._write_app_state_key(
            install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING, "qwen3",
        )
        # v0.2.101 item 4: this class's fixture omits the metadata-repair stamp
        # on purpose (the absent embedding is what it is about), but leg (d)
        # would then force a FULL seed — which is now the enqueued path, not
        # the leg (c) diff this test pins. Complete the stamp so the run
        # actually takes leg (c).
        install._write_app_state_key(
            _install_weaviate.KG_METADATA_REPAIR_STATE_KEY,
            _install_weaviate.KG_METADATA_REPAIR_STAMP,
        )

        def _raise_nonzero(cmd, **kwargs):
            if "sync_knowledge_graph.py" in str(cmd):
                raise _sp.CalledProcessError(returncode=1, cmd=cmd)
            class _Ret:
                returncode = 0
            return _Ret()

        report = self._new_report()
        self._run_seed(
            subprocess_side_effect=_raise_nonzero, deferral_report=report,
        )

        rows = self._read_triple()
        self.assertEqual(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING), "qwen3",
            "SEG-1: on leg (c) a non-zero exit must still persist the context "
            "triple — the v0.2.95 carve-out is leg-(b)-only",
        )
        self.assertIn(install._APP_STATE_KEY_LAST_KG_SYNC_AT, rows)
        self.assertNotIn(
            install._SEED_OWED_WORK_CONDITION_ID,
            [e.condition_id for e in report.entries],
            "leg (c) has its own recovery (the per-file diff) and must not "
            "raise the leg-(b) owed-work entry",
        )

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

    # ── (iii) two-update integration: the retry TERMINATES ───────────────

    def test_incomplete_leg_b_retries_next_update_and_then_stops(self):
        """The property that makes the detach safe rather than a re-run treadmill.

        Run 1 is a leg-(b) context change: the whole-tree seed is ENQUEUED and
        the triple is NOT advanced, so run 2 re-enters the full-seed branch
        (the intended retry; since v0.2.95 WP-5 it re-embeds only what never
        landed). Once the detached handler has stamped the triple — its
        post-clear write, simulated here with the context the spawn carried —
        run 2 computes context_changed == False and takes the cheap diff path.
        The loop ends.
        """
        # RUN 1 — enqueued, not finished.
        self._run_seed(subprocess_side_effect=_fail_if_the_seed_runs_inline)
        rows = self._read_triple()
        self.assertIsNone(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING),
            "run 1 did not finish; the marker must still say so",
        )
        self.assertEqual(len(self.spawns), 1, "run 1 must enqueue the full seed")
        self.assertTrue(
            rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING)
            != os.environ["ACTIVE_EMBEDDING"],
            "run 2 must re-enter the full-seed branch — the retry",
        )

        # The detached handler finishes the seed and stamps the context. A
        # successful whole-tree run also records the one-time metadata-repair
        # pass (the sync script writes its own file stamp; install.py's
        # app_state record is a projection of it) — without that, run 2 would
        # legitimately re-enter the FULL branch via leg (d) instead of the
        # diff path this test is about.
        install._write_app_state_key(
            _install_weaviate.KG_METADATA_REPAIR_STATE_KEY,
            _install_weaviate.KG_METADATA_REPAIR_STAMP,
        )
        carried = self.spawns[0]["env"]
        deferral_retry.stamp_seed_context(
            {
                "active_embedding": carried[deferral_retry.SEED_CTX_ENV_ACTIVE_EMBEDDING],
                "kg_collection": carried[deferral_retry.SEED_CTX_ENV_KG_COLLECTION],
                "shared_kg_collection": carried[
                    deferral_retry.SEED_CTX_ENV_SHARED_KG_COLLECTION],
            },
            write_key=install._write_app_state_key,
        )

        # RUN 2 — context unchanged → the cheap per-file diff, no enqueue.
        self._run_seed(subprocess_side_effect=_fail_if_the_full_seed_runs_inline)
        self.assertEqual(
            self.spawns, [],
            "once the context is stamped, the next update must NOT enqueue "
            "another full seed — otherwise the retry became permanent",
        )
        rows = self._read_triple()
        stored_embedding = rows.get(install._APP_STATE_KEY_LAST_ACTIVE_EMBEDDING)
        stored_kg = rows.get(install._APP_STATE_KEY_LAST_KG_COLLECTION)
        stored_shared = rows.get(install._APP_STATE_KEY_LAST_SHARED_KG_COLLECTION)

        # This is the exact comparison install.py runs at the top of run 3.
        context_changed = (
            stored_embedding != os.environ["ACTIVE_EMBEDDING"]
            or stored_kg != os.environ["KG_COLLECTION"]
            or stored_shared != os.environ["SHARED_KG_COLLECTION"]
        )
        self.assertFalse(
            context_changed,
            "after a COMPLETED context change, the next --update must take "
            "the content-hash diff path — otherwise the detach would have "
            "turned a one-off retry into a permanent full re-embed",
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
            create_empty_launcher_db(Path(tmp) / "launcher.db")

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
            create_empty_launcher_db(Path(tmp) / "launcher.db")

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
        return create_empty_launcher_db(Path(tmp) / "launcher.db")

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
