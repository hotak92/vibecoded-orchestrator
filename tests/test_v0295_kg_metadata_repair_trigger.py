# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 WP-7 — something actually RUNS the KG metadata repair.

The repair itself (``vco_lib.kg_metadata_repair``, exercised by
``tests/test_v0295_kg_metadata_repair_on_skip.py``) was unreachable on every
upgrade path when it shipped. It fires per node on the sync's EMBED-SKIP
path, so only a run that VISITS a node can reach it — and for exactly the
nodes it repairs, nothing visits them:

  * ``install.py --update`` hands leg (c) the content-hash diff, and for
    these rows the hash MATCHES (the text never changed — that is the
    defect's premise), so the diff is empty and no sync is spawned at all;
  * ``--all`` fires only on a context change or a missing collection;
  * the bundle-update gate spawns ``--all`` only on drift, and drift is the
    same recomputed-signature-vs-stored-hash equality — equal here;
  * the edit hook visits only the edited file.

So a 0.2.94 user updating through the launcher kept prose-scraped ``tags``
and a folder-derived ``node_type`` forever, with tag filters silently
excluding those nodes.

What is under test here is the TRIGGER: leg (d) in
``install.py::_seed_weaviate_impl``, gated by
``vco_lib.install_weaviate.kg_metadata_repair_due_now`` and retired by
``stamp_kg_metadata_repair``. Five properties, each asserted on an
OBSERVABLE effect (the spawned argv, the stored ``app_state`` row, the
written stamp file) and never on a source scan — six source-scanning tests in
this release broke on legitimate changes:

  a. an install that has not paid the pass spawns ``--all``;
  b. an install that HAS paid it does not spawn it again;
  c. a run that exited non-zero does NOT record the pass, so the next update
     retries it;
  d. the pass costs ZERO embeds — asserted on the embed CALL COUNT, with one
     fetch per node and one patch per stale row, not on the absence of an
     error;
  e. the ``app_state`` row is a PROJECTION of the per-project stamp file the
     same run wrote, never a second opinion about it (round-6 ship-gate
     MAJOR): a repair that aborts part-way is counted rather than FAILED, so
     the run still exits 0, and only the withheld file stamp records that the
     work is still owed.

No live Weaviate anywhere: the install-level tests replace
``subprocess.run`` outright, and the cost test reuses the in-memory counting
fake from ``test_v0295_kg_metadata_repair_on_skip`` rather than forking a
second copy of it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import connect, make_launcher_db  # noqa: E402
from vco_lib import install_weaviate as iw  # noqa: E402
from vco_lib import deferral_retry as dr  # noqa: E402
from vco_lib import kg_metadata_repair_state as state  # noqa: E402
import install  # noqa: E402

# The fake-Weaviate family + the counting server live in the repair's own
# test module. Imported rather than copied: a second 200-line fake would be
# a second thing to keep in sync with the client's shape, and the two
# modules assert about the same subject from different ends.
from tests.test_v0295_kg_metadata_repair_on_skip import (  # noqa: E402
    ACTIVE_SLOT,
    NESTED_NODE,
    PROJECT_KG,
    STALE_STORED,
    _CountingServer,
    _FakeFilter,
    _FakeObj,
    _load_sync_module,
)

KG = "TestProject_KnowledgeGraph"

#: The frontmatter a v0.2.95 writer parses correctly, so a node using it has
#: nothing to repair. Used to prove the pass patches only the STALE rows.
CURRENT_NODE = """---
title: Already Current
type: concept
tags: [alpha, beta]
---

Body text with a #hash reference that must NOT become a tag.
"""

CURRENT_STORED = {
    "title": "Already Current",
    "node_type": "concept",
    "tags": ["alpha", "beta"],
    "external_links": "",
}


# ─────────────────────────────────────────────────────────────────────────
# PURE layer — the two decisions, every input class.
# ─────────────────────────────────────────────────────────────────────────


class RepairGateDecisionTests(unittest.TestCase):
    """``kg_metadata_repair_due`` / ``kg_metadata_repair_certified``."""

    def test_absent_stamp_is_owed(self):
        self.assertTrue(
            iw.kg_metadata_repair_due(None),
            "no stamp is the shape of every pre-0.2.95 install AND of every "
            "install with no launcher.db — unknown must be OWED",
        )

    def test_current_stamp_is_not_owed(self):
        self.assertFalse(iw.kg_metadata_repair_due(iw.KG_METADATA_REPAIR_STAMP))

    def test_later_stamp_is_not_owed(self):
        self.assertFalse(
            iw.kg_metadata_repair_due("0.3.1"),
            "a stamp PAST the newest bump still satisfies it",
        )

    def test_older_and_malformed_stamps_are_owed(self):
        for stamped in ("0.2.94", "0.1.0", "", "not-a-version", "0.2"):
            with self.subTest(stamped=stamped):
                self.assertTrue(
                    iw.kg_metadata_repair_due(stamped),
                    "a stamp we cannot read as at-or-past the bump is OWED, "
                    "never assumed satisfied",
                )

    def test_a_future_bump_reopens_a_satisfied_stamp(self):
        """Appending a bump is how a later dialect re-owes the pass."""
        self.assertFalse(iw.kg_metadata_repair_due("0.2.95", bumps=("0.2.95",)))
        self.assertTrue(
            iw.kg_metadata_repair_due("0.2.95", bumps=("0.2.95", "0.3.7")),
            "the ladder, not a boolean, is what a future dialect extends",
        )

    def test_only_a_clean_whole_tree_run_certifies(self):
        self.assertTrue(iw.kg_metadata_repair_certified(True, True))
        self.assertFalse(
            iw.kg_metadata_repair_certified(True, False),
            "a run that exited non-zero may have died before visiting the "
            "rest of the tree, and those nodes keep a MATCHING content_hash "
            "— no later diff can see that they are still owed",
        )
        self.assertFalse(
            iw.kg_metadata_repair_certified(False, True),
            "a run handed an explicit file list judged only those files",
        )
        self.assertFalse(iw.kg_metadata_repair_certified(False, False))

    def test_exit_zero_is_not_proof_the_repair_completed(self):
        """Round-6 MAJOR: the fourth precondition, on the pure seam.

        An INCOMPLETE repair is counted, not FAILED — the node's outcome is
        ``embed-skipped``, so the run exits 0 with that node still stale
        behind a matching content hash. The sync withholds its FILE stamp on
        that counter; this gate used to see only the exit code and certify
        anyway, leaving ``app_state`` claiming a pass the sync says is still
        owed. With the root in hand the two records cannot disagree.
        """
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)

        self.assertIs(state.repair_owed(root), True, "fixture sanity")
        self.assertFalse(
            iw.kg_metadata_repair_certified(True, True, project_root=root),
            "exit 0 with NO file stamp is the incomplete-repair shape — the "
            "one the whole mechanism exists to end",
        )

        state.write_stamp(root)
        self.assertTrue(
            iw.kg_metadata_repair_certified(True, True, project_root=root),
            "and the run that DID complete certifies — otherwise leg (d) "
            "would walk the whole tree on every update forever",
        )

    def test_an_unreadable_stamp_does_not_certify(self):
        """A stamp that cannot be READ is not a stamp that says done.

        It costs one more zero-embed pass, and that pass REWRITES the stamp
        (``write_stamp`` overwrites unconditionally), so it cannot loop.
        """
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = state.state_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        self.assertIsNone(state.repair_owed(root), "fixture sanity")
        self.assertFalse(
            iw.kg_metadata_repair_certified(True, True, project_root=root)
        )

    def test_stamp_seam_writes_only_when_certified(self):
        written: "list[tuple]" = []
        self.assertTrue(
            iw.stamp_kg_metadata_repair(True, True, lambda k, v: written.append((k, v)))
        )
        self.assertEqual(
            written,
            [(iw.KG_METADATA_REPAIR_STATE_KEY, iw.KG_METADATA_REPAIR_STAMP)],
        )
        written.clear()
        for sync_all, exit_zero in ((True, False), (False, True), (False, False)):
            with self.subTest(sync_all=sync_all, exit_zero=exit_zero):
                self.assertFalse(
                    iw.stamp_kg_metadata_repair(
                        sync_all, exit_zero, lambda k, v: written.append((k, v))
                    )
                )
        self.assertEqual(written, [], "an uncertified run writes nothing")

        # The third leg (round-6 MAJOR): a root in hand, and no file stamp.
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.assertFalse(
            iw.stamp_kg_metadata_repair(
                True, True, lambda k, v: written.append((k, v)),
                project_root=root,
            ),
            "exit 0 does not mean the repair completed — the file stamp the "
            "same run would have written is what says it did",
        )
        self.assertEqual(written, [], "and nothing reached app_state")

        state.write_stamp(root)
        self.assertTrue(
            iw.stamp_kg_metadata_repair(
                True, True, lambda k, v: written.append((k, v)),
                project_root=root,
            )
        )
        self.assertEqual(
            written,
            [(iw.KG_METADATA_REPAIR_STATE_KEY, iw.KG_METADATA_REPAIR_STAMP)],
        )

    def test_read_seam_asks_for_the_one_key(self):
        asked: "list[str]" = []

        def _read(key):
            asked.append(key)
            return None

        self.assertTrue(iw.kg_metadata_repair_due_now(_read))
        self.assertEqual(asked, [iw.KG_METADATA_REPAIR_STATE_KEY])


# ─────────────────────────────────────────────────────────────────────────
# INSTALL layer — what the seed step actually SPAWNS, and what it records.
# ─────────────────────────────────────────────────────────────────────────


class RepairTriggerTests(unittest.TestCase):
    """Leg (d) end to end, asserted on the spawned argv + stored app_state."""

    def setUp(self):
        gate = mock.patch.object(
            install, "_wait_for_weaviate_ready", lambda *a, **k: True
        )
        gate.start()
        self.addCleanup(gate.stop)

        self.tmp = Path(tempfile.mkdtemp())
        for key, value in (
            ("VCT_STATE_DIR", str(self.tmp)),
            ("ACTIVE_EMBEDDING", "qwen3"),
            ("KG_COLLECTION", KG),
            ("SHARED_KG_COLLECTION", ""),
        ):
            prior = os.environ.get(key)
            os.environ[key] = value
            self.addCleanup(
                lambda k=key, p=prior: (
                    os.environ.pop(k, None) if p is None else os.environ.__setitem__(k, p)
                )
            )

        scripts = self.tmp / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "sync_knowledge_graph.py").write_text("# stub\n")
        venv = self.tmp / ".venv" / "bin"
        venv.mkdir(parents=True, exist_ok=True)
        (venv / "python").write_text("#!/bin/sh\n")
        (venv / "python").chmod(0o755)

        self.db_path = self.tmp / "launcher.db"
        self._state = {
            "last_installed_active_embedding": "qwen3",
            "last_installed_kg_collection": KG,
            "last_installed_shared_kg_collection": "",
        }

    def _build_db(self, **extra):
        make_launcher_db(self.db_path, app_state={**self._state, **extra})

    def _app_state(self) -> dict:
        conn = connect(self.db_path)
        try:
            return {
                row[0]: row[1]
                for row in conn.execute("SELECT key, value FROM app_state").fetchall()
            }
        finally:
            conn.close()

    def _child(self, spawned, *, returncode=0, stamps=True, raise_on_nonzero=True):
        """The real sync child, faked: it writes the per-project stamp file at
        the end of a clean ``--all`` (the shape an exit code cannot show).

        ``raise_on_nonzero`` mirrors the two transports exactly: install.py's
        ``run_child_logged(check=True)`` RAISES on a non-zero exit, while the
        driver's runner returns the code to the handler instead.
        """
        def _fake_run(cmd, **kwargs):
            spawned.append(tuple(str(c) for c in cmd))

            class _Ret:
                returncode = 0
                stdout = ""
                stderr = ""

            if "vco_lib.embedding_enrichment" in str(cmd):
                _Ret.stdout = json.dumps(
                    {"total": 1, "enriched": 1, "skipped": 0, "failed": 0,
                     "failures": []}
                )
                return _Ret()
            if "sync_knowledge_graph.py" in str(cmd):
                if returncode and raise_on_nonzero:
                    raise subprocess.CalledProcessError(returncode, cmd)
                if returncode == 0 and stamps and "--all" in [str(a) for a in cmd]:
                    state.write_stamp(self.tmp)
            return _Ret()

        return _fake_run

    def _run_seed(self, *, hashes, update=True) -> "tuple[list, list]":
        """Drive install's seed step.

        v0.2.101 item 4: the whole-tree seed is ENQUEUED, so "the pass runs"
        now means "install hands the driver the work". Returns
        ``(enqueues, spawned)``: each detached-seed spawn install made (its
        ``extra_env`` is the context the driver inherits) and every child argv
        install itself ran.
        """
        enqueues: "list[dict]" = []
        spawned: "list[tuple]" = []

        def _fake_spawn(folder, *, python="", extra_env=None):
            enqueues.append({"folder": Path(folder), "env": dict(extra_env or {})})
            return True

        buf = io.StringIO()
        with mock.patch.object(
            install, "_discover_app_state_db_path", return_value=self.db_path
        ), mock.patch.object(
            install, "PROJECT_ROOT", self.tmp
        ), mock.patch.object(
            install, "_compute_on_disk_content_hashes", return_value=hashes
        ), mock.patch.object(
            install, "_batch_query_weaviate_content_hashes", return_value=hashes
        ), mock.patch.object(
            install, "_prune_stale_kg_rows", lambda *a, **k: None
        ), mock.patch(
            "subprocess.run", side_effect=self._child(spawned)
        ), mock.patch.object(
            install, "run_child_logged", side_effect=self._child(spawned)
        ), mock.patch(
            # v0.2.101 item 4: the whole-tree seed leaves via the detached
            # driver instead of run_child_logged.
            "vco_lib.deferral_retry.spawn_detached", side_effect=_fake_spawn
        ), contextlib.redirect_stdout(buf):
            ns = argparse.Namespace()
            ns.update = update
            ns.skip_seed = False
            install._seed_weaviate(ns)
        return enqueues, spawned

    def _run_driver(self, *, enqueue, returncode=0, stamps=True) -> "list[tuple]":
        """The detached driver's turn: run the ``--all`` child, then the REAL
        ``retry_kg_seed`` handler (which projects the context triple AND the
        one-time metadata-repair row).

        ``condition_cleared`` is patched to mirror the ledger: the child's own
        paired clear fires exactly when the ``--all`` finished with zero
        per-node failures. The enqueue's ``extra_env`` is applied, exactly as
        ``spawn_detached`` hands it to the real child.
        """
        spawned: "list[tuple]" = []

        def _runner(argv, cwd):
            # `_child` records the argv; do not record it twice. The driver's
            # runner RETURNS the code (no check=), unlike install.py's helper.
            self._child(spawned, returncode=returncode, stamps=stamps,
                        raise_on_nonzero=False)(tuple(str(a) for a in argv))
            return returncode

        ctx = dr.RetryContext(
            folder=self.tmp, condition_id=iw.SEED_OWED_WORK_CONDITION_ID,
            backend_probe=lambda *a, **k: True, runner=_runner, python="python",
        )
        with mock.patch.dict(os.environ, enqueue["env"], clear=False), \
                mock.patch.object(dr, "condition_cleared",
                                  return_value=(returncode == 0)):
            dr.retry_kg_seed(ctx)
        return spawned

    @staticmethod
    def _kg_sync_argvs(spawned) -> "list[tuple]":
        return [c for c in spawned if "sync_knowledge_graph.py" in str(c)]

    # ── (a) an unpaid install spawns the whole-tree pass ──────────────────

    def test_unstamped_install_spawns_the_all_pass(self):
        """RED-PROOF (a). The constructed failure, made to succeed.

        Every content hash MATCHES — the shape that made leg (c) return
        without spawning anything at all — and the install carries no repair
        stamp, which is what a 0.2.94 install looks like the moment it
        updates. The pass must be spawned anyway.
        """
        self._build_db()  # no repair stamp: the pre-0.2.95 shape
        node = str(self.tmp / "knowledge" / "concepts" / "stale.md")

        enqueues, _ = self._run_seed(hashes={node: "samehash"})
        self.assertEqual(
            len(enqueues), 1,
            "an install that has not paid the one-time metadata-repair pass "
            "must enqueue the whole-tree seed — with every hash equal, leg (c) "
            "spawns nothing and the repair is unreachable",
        )
        spawned = self._run_driver(enqueue=enqueues[0])
        self.assertEqual(
            [c[-1] for c in self._kg_sync_argvs(spawned)], ["--all"],
            "and the driver must run the WHOLE-TREE pass (only a run that "
            "visits every node can repair it)",
        )

    def test_a_clean_pass_records_itself(self):
        """The positive half of (e): exit 0 AND the file stamp present."""
        self._build_db()
        node = str(self.tmp / "knowledge" / "concepts" / "stale.md")

        enqueues, _ = self._run_seed(hashes={node: "samehash"})
        self._run_driver(enqueue=enqueues[0])

        self.assertTrue(
            state.state_path(self.tmp).exists(),
            "fixture sanity: a clean `--all` writes the per-project stamp, "
            "and app_state is derived from it",
        )
        self.assertEqual(
            self._app_state().get(iw.KG_METADATA_REPAIR_STATE_KEY),
            iw.KG_METADATA_REPAIR_STAMP,
            "a whole-tree run that exited 0 visited every node, so it must "
            "retire the pass — otherwise it repeats on every update",
        )

    # ── (b) the second update does NOT re-trigger ─────────────────────────

    def test_stamped_install_does_not_spawn_it_again(self):
        """RED-PROOF (b). Once, not on every subsequent update."""
        self._build_db(**{
            iw.KG_METADATA_REPAIR_STATE_KEY: iw.KG_METADATA_REPAIR_STAMP,
        })
        node = str(self.tmp / "knowledge" / "concepts" / "stale.md")

        enqueues, spawned = self._run_seed(hashes={node: "samehash"})

        self.assertEqual(
            enqueues, [],
            "the pass is ONE-TIME: with the stamp recorded and every hash "
            "matching, this update must enqueue no seed at all",
        )
        self.assertEqual(self._kg_sync_argvs(spawned), [])

    def test_the_two_runs_compose(self):
        """Run 1 repairs and stamps; run 2 — same tree — spawns nothing."""
        self._build_db()
        node = str(self.tmp / "knowledge" / "concepts" / "stale.md")

        first, _ = self._run_seed(hashes={node: "samehash"})
        self.assertEqual(len(first), 1, "run 1 enqueues the pass")
        first_spawned = self._run_driver(enqueue=first[0])
        self.assertEqual([c[-1] for c in self._kg_sync_argvs(first_spawned)], ["--all"])

        second, second_children = self._run_seed(hashes={node: "samehash"})
        self.assertEqual(
            second, [],
            "the stamp run 1's driver wrote is what run 2 reads; a gate that "
            "re-fires here would charge every future update a whole-tree walk",
        )
        self.assertEqual(self._kg_sync_argvs(second_children), [])

    # ── (c) a failed pass is NOT recorded, so it retries ──────────────────

    def test_a_failed_pass_is_not_recorded_and_retries(self):
        """RED-PROOF (c). The WP-4 rule, for WP-4's reason.

        A node the pass never reached keeps a content_hash that still
        MATCHES, so no later diff can see it is owed. Stamping a run that
        exited non-zero would therefore retire the repair permanently with
        the work undone.
        """
        self._build_db()
        node = str(self.tmp / "knowledge" / "concepts" / "stale.md")

        enqueues, _ = self._run_seed(hashes={node: "samehash"})
        self._run_driver(enqueue=enqueues[0], returncode=1)

        self.assertIsNone(
            self._app_state().get(iw.KG_METADATA_REPAIR_STATE_KEY),
            "a run that did not complete must NOT record the pass as done",
        )

        retry, _ = self._run_seed(hashes={node: "samehash"})
        self.assertEqual(
            len(retry), 1,
            "withholding the stamp IS the retry — the next update must "
            "enqueue the pass again",
        )
        retry_spawned = self._run_driver(enqueue=retry[0])
        self.assertEqual(
            [c[-1] for c in self._kg_sync_argvs(retry_spawned)], ["--all"],
        )
        self.assertEqual(
            self._app_state().get(iw.KG_METADATA_REPAIR_STATE_KEY),
            iw.KG_METADATA_REPAIR_STAMP,
            "and the clean retry finally retires it",
        )

    def test_a_clean_exit_without_the_file_stamp_records_nothing(self):
        """RED-PROOF, round-6 MAJOR. Exit 0 is not proof the repair completed.

        The constructed failure: a repair that fails after row 1 of 3 on a
        transient 5xx is COUNTED (`_METADATA_REPAIR_FAILED_COUNT`) and its
        node still returns ``embed-skipped`` — not a FAILED outcome — so
        ``total_fail`` is 0 and the run exits 0. The sync correctly withholds
        its file stamp. install.py sees only the exit code; before this fix
        it stamped ``app_state`` anyway, and from then on leg (d) answered
        not-due while leg (c)'s diff stayed empty for that node — its
        remaining rows keep prose-scraped tags permanently, with the run
        report's "the next sync retries the rest" naming a run that never
        comes.

        Asserted on the stored ``app_state`` row and on the argv of the NEXT
        update, never on a source scan.
        """
        self._build_db()
        node = str(self.tmp / "knowledge" / "concepts" / "stale.md")

        enqueues, _ = self._run_seed(hashes={node: "samehash"})
        self._run_driver(enqueue=enqueues[0], stamps=False)

        self.assertFalse(
            state.state_path(self.tmp).exists(),
            "fixture sanity: this is the shape where the sync withheld its "
            "own stamp",
        )
        self.assertIsNone(
            self._app_state().get(iw.KG_METADATA_REPAIR_STATE_KEY),
            "app_state must be a PROJECTION of the file stamp the same run "
            "wrote — two records of one fact that can disagree is the defect",
        )

        retry, _ = self._run_seed(hashes={node: "samehash"})
        self.assertEqual(
            len(retry), 1, "withholding the app_state row IS the retry",
        )
        retry_spawned = self._run_driver(enqueue=retry[0])
        self.assertEqual(
            [c[-1] for c in self._kg_sync_argvs(retry_spawned)], ["--all"],
        )
        self.assertEqual(
            self._app_state().get(iw.KG_METADATA_REPAIR_STATE_KEY),
            iw.KG_METADATA_REPAIR_STAMP,
            "and the run that DID complete — file stamp present — records it",
        )

    def test_a_fresh_install_records_the_pass_and_never_pays_it_twice(self):
        """A fresh install already walks the whole tree, so it IS the pass.

        Without this, a user who installed at 0.2.95 would pay a second
        whole-tree walk on their first `--update` for a repair that had
        nothing left to repair.
        """
        self._build_db()
        node = str(self.tmp / "knowledge" / "concepts" / "n.md")

        fresh, _ = self._run_seed(hashes={node: "samehash"}, update=False)
        self.assertEqual(len(fresh), 1, "a fresh install enqueues the pass")
        fresh_spawned = self._run_driver(enqueue=fresh[0])
        self.assertEqual([c[-1] for c in self._kg_sync_argvs(fresh_spawned)], ["--all"])
        self.assertEqual(
            self._app_state().get(iw.KG_METADATA_REPAIR_STATE_KEY),
            iw.KG_METADATA_REPAIR_STAMP,
        )

        later, later_children = self._run_seed(hashes={node: "samehash"})
        self.assertEqual(
            later, [],
            "the fresh install's own `--all` already visited every node — "
            "its first update must not walk the tree again",
        )
        self.assertEqual(self._kg_sync_argvs(later_children), [])

    def test_skip_seed_records_nothing(self):
        """Nothing ran, so nothing may be retired."""
        self._build_db()

        buf = io.StringIO()
        with mock.patch.object(
            install, "_discover_app_state_db_path", return_value=self.db_path
        ), mock.patch.object(
            install, "PROJECT_ROOT", self.tmp
        ), contextlib.redirect_stdout(buf):
            ns = argparse.Namespace()
            ns.update = True
            ns.skip_seed = True
            install._seed_weaviate(ns)

        self.assertIsNone(
            self._app_state().get(iw.KG_METADATA_REPAIR_STATE_KEY),
            "--skip-seed visited no node; stamping there would retire the "
            "pass without it ever having run",
        )

    def test_the_pass_does_not_disturb_the_context_triple(self):
        """Leg (d) is not leg (b): it records no embedding-context claim."""
        self._build_db()
        node = str(self.tmp / "knowledge" / "concepts" / "stale.md")

        self._run_seed(hashes={node: "samehash"})
        state = self._app_state()

        self.assertEqual(state.get("last_installed_active_embedding"), "qwen3")
        self.assertEqual(state.get("last_installed_kg_collection"), KG)


# ─────────────────────────────────────────────────────────────────────────
# COST layer — what the whole-tree pass actually spends.
# ─────────────────────────────────────────────────────────────────────────


class WholeTreePassCostTests(unittest.TestCase):
    """One fetch per node, one patch per STALE row, and zero embeds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        keys = (
            "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
            "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
            "VCT_DISABLE_HUB_RESOLVER", "VCT_STATE_DIR", "WEAVIATE_URL",
            "VCT_ORCHESTRATOR_ROOT", "KG_SYNC_PROJECT_ROOT", "VCT_PROJECT_ID",
            "SHARED_KG_WRITE_DISABLED", "SHARED_KG_OPT_OUT",
        )
        snapshot = {k: os.environ.get(k) for k in keys}

        def _restore():
            for k, v in snapshot.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore)

    def _write(self, name: str, text: str) -> Path:
        path = self.root / "knowledge" / "concepts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def _seed(mod, server, path: Path, stored: dict):
        text = path.read_text(encoding="utf-8")
        store = server.client.collections._store_for(PROJECT_KG)
        uid = f"row-{path.stem}"
        props = {
            "file_path": mod._canonical_file_path(path),
            "content_hash": mod._content_signature_excluding_updated(text),
            "chunk_num": 1,
            "total_chunks": 1,
            "content": text,
            "links": [],
        }
        props.update(stored)
        store[uid] = _FakeObj(uid, props, {ACTIVE_SLOT: [0.1, 0.2, 0.3]})
        return uid

    def test_whole_tree_pass_patches_only_stale_rows_and_embeds_nothing(self):
        """RED-PROOF (d) — the cost, asserted on COUNTS.

        Three nodes whose text is unchanged: one stored under the pre-0.2.95
        parse, two already correct. Walking all three is what leg (d) buys,
        and it must cost one fetch each, one patch for the stale one, and
        not a single embed.
        """
        mod = _load_sync_module(self.root)
        mod.Filter = _FakeFilter
        server = _CountingServer()

        stale = self._write("stale.md", NESTED_NODE)
        self._seed(mod, server, stale, dict(STALE_STORED))
        fresh = [
            self._write(f"fresh{i}.md", CURRENT_NODE) for i in range(2)
        ]
        for path in fresh:
            self._seed(mod, server, path, dict(CURRENT_STORED))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcomes = [mod.sync_node(server, p) for p in [stale, *fresh]]

        self.assertEqual(
            [o.status for o in outcomes],
            [mod.OUTCOME_EMBED_SKIPPED] * 3,
            "every node's text is unchanged — all three must take the "
            "embed-skip path",
        )
        self.assertEqual(
            server.embed_calls, 0,
            "THE cost assertion: the pass exists to patch properties on text "
            "it has proved unchanged; one embed here and the trigger is a "
            "whole-tree re-embed wearing a repair's name",
        )
        self.assertEqual(server.opts.inserts, 0)
        self.assertEqual(server.opts.deletes, 0)
        self.assertEqual(
            len(server.opts.updates), 1,
            "one PATCH, on the one stale row — the two already-correct nodes "
            "must not be rewritten, or every sync would rewrite the graph",
        )
        patched_uuid, payload = server.opts.updates[0]
        self.assertEqual(patched_uuid, "row-stale")
        self.assertEqual(
            sorted(payload), ["node_type", "tags", "title"],
            "only the properties that actually differ belong in the payload",
        )
        self.assertEqual(
            len(server.opts.fetch_props_seen), 3,
            "one fetch per node — the repair reads the SAME fetch the "
            "embed-skip gate already makes, so it adds no roundtrip",
        )

    def test_the_repaired_row_is_correct_afterwards(self):
        """A cost assertion alone would pass an implementation that no-ops."""
        mod = _load_sync_module(self.root)
        mod.Filter = _FakeFilter
        server = _CountingServer()
        stale = self._write("stale.md", NESTED_NODE)
        uid = self._seed(mod, server, stale, dict(STALE_STORED))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.sync_node(server, stale)

        stored = server.client.collections._store_for(PROJECT_KG)[uid].properties
        self.assertEqual(stored["node_type"], "concept")
        self.assertEqual(
            sorted(stored["tags"]), ["Acme", "Acme-PAY", "payments"],
            "the prose-scraped ['4','12','14'] is exactly what made tag "
            "filters exclude these nodes",
        )
        self.assertEqual(
            stored["content_hash"],
            mod._content_signature_excluding_updated(
                stale.read_text(encoding="utf-8")
            ),
            "the hash has readers that are not this gate — it must not move",
        )


if __name__ == "__main__":
    unittest.main()
