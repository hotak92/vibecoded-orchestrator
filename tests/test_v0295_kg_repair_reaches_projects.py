# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 ship-gate MAJOR — the metadata repair reaches registered PROJECTS.

The defect
----------
``install.py::_seed_weaviate_impl`` leg (d) runs the one-time KG
metadata-repair pass when ``app_state`` carries no
``last_kg_metadata_repair_version``. That is the ONE place seeding the install
ROOT's own ``knowledge/`` — and ``app_state`` is a GLOBAL table, so it cannot
speak for anything else.

A REGISTERED PROJECT's tree is synced by the launcher's bundle update, whose
gate runs ``kg-sync --check-drift`` and spawns ``--all`` only on drift. Drift
is *recomputed signature ≠ stored hash* (``vco_lib.kg_sync_drift.scan_drift``)
— and that is EQUAL for exactly the rows this release repairs: their text
never changed, which is the defect's whole premise. So the constructed
failure the ship-gate review gave: a project where an agent wrote a ``name:``
plus nested ``metadata:`` node under 0.2.94 — the origin the CHANGELOG names —
gets "Update bundle" at 0.2.95, reports no drift, runs no sync, and keeps its
prose-scraped ``tags``, so tag filters still exclude it. Forever.

The fix under test
------------------
``sync_knowledge_graph.py`` writes a per-project stamp
(``.claude/state/kg-metadata-repair.json``,
:mod:`vco_lib.kg_metadata_repair_state`) at the END of a clean ``--all``,
beside the three deferral clears that already live there. ``--check-drift``
reads it and reports ``repair_owed`` on its machine-readable verdict; the
EXISTING drift machinery spawns the ordinary zero-embed ``--all``; that run's
clean completion writes the stamp and the signal retires itself. One
mechanism covering the root, every project, and the launcher's Sync-KG button.

What is asserted, and on what
-----------------------------
Every check below lands on an OBSERVABLE — a written stamp file, a parsed
sentinel payload, an embed CALL COUNT, a decision value — never on a source
scan, and never on the mere absence of an error:

  (a) a project with no stamp reports ``repair_owed: true`` on the wire;
  (b) a project stamped at the current generation reports ``false``;
  (c) an ``--all`` that exits non-zero leaves NO stamp, so the next update
      retries — and the clean run that follows writes it;
  (d) the stamp costs ZERO embeds.

The Rust half of the chain (sentinel → verdict → spawn decision) is pinned in
``kg_sync.rs`` / ``change_detect.rs`` unit tests.

No live Weaviate anywhere: the in-memory fake and the module loader are
REUSED from ``tests/test_v0292_kg_sync_flags_tally_stage.py`` rather than
forked (a third copy of that harness is exactly the duplication the project's
modularity rule forbids), and ``conftest.py`` pins ``WEAVIATE_URL`` at the
unroutable sentinel. Every path written is under ``tmp``.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Reuse, do not fork: the fake-Weaviate harness + module loader.
# `_SyncTestBase` carries no test methods, so importing it adds no test.
from tests.test_v0292_kg_sync_flags_tally_stage import (  # noqa: E402
    PROJECT_KG,
    _SyncTestBase,
    _write_node,
)
from vco_lib import kg_metadata_repair_state as state  # noqa: E402
from vco_lib.install_weaviate import (  # noqa: E402
    KG_METADATA_REPAIR_BUMPS,
    KG_METADATA_REPAIR_STAMP,
)
from vco_lib.kg_sync_drift import BindingCheck, DriftReport  # noqa: E402

DRIFT_SENTINEL_PREFIX = "KG_DRIFT_JSON "


def _sentinel_payload(stdout: str) -> dict:
    """The parsed machine-readable verdict — the launcher's ONLY input."""
    lines = [
        ln.strip() for ln in stdout.splitlines()
        if ln.strip().startswith(DRIFT_SENTINEL_PREFIX)
    ]
    assert lines, f"--check-drift emitted no sentinel line:\n{stdout}"
    return json.loads(lines[-1][len(DRIFT_SENTINEL_PREFIX):])


# ═════════════════════════════════════════════════════════════════════════
# 1. The stamp itself — tri-state, and the ladder it shares with the root
# ═════════════════════════════════════════════════════════════════════════


class TheStampIsTriState(unittest.TestCase):
    """Absent / recorded / UNREADABLE are three answers, not two."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_project_with_no_stamp_owes_the_pass(self):
        """RED-PROOF (a), decision half: the state EVERY 0.2.94 project is in."""
        self.assertEqual(state.read_stamp(self.root), "",
                         "no file is a KNOWN state, not an unreadable one")
        self.assertIs(state.repair_owed(self.root), True)

    def test_a_current_stamp_does_not_owe_it(self):
        """RED-PROOF (b), decision half: once, then silence."""
        self.assertTrue(state.write_stamp(self.root))
        self.assertEqual(state.read_stamp(self.root), KG_METADATA_REPAIR_STAMP)
        self.assertIs(state.repair_owed(self.root), False)

    def test_an_unreadable_stamp_is_unknown_and_never_owed(self):
        """A corrupt stamp must degrade to PRIOR behaviour.

        `None` (not `True`): "could not look" spawning a whole-tree pass on
        every update forever is the failure mode the cost rule forbids, and
        it is the one shape an absent file must NOT be conflated with.
        """
        path = state.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(state.read_stamp(self.root))
        self.assertIsNone(state.repair_owed(self.root))

    def test_a_stamp_below_the_newest_bump_re_owes_the_pass(self):
        """Appending a bump is how a later dialect re-owns the work."""
        state.write_stamp(self.root, "0.2.94")
        self.assertIs(state.repair_owed(self.root), True)

    def test_the_ladder_is_the_install_roots_ladder_not_a_second_one(self):
        """One home for "at least this generation" — root and project alike."""
        state.write_stamp(self.root, KG_METADATA_REPAIR_BUMPS[-1])
        self.assertIs(state.repair_owed(self.root), False)
        self.assertEqual(KG_METADATA_REPAIR_STAMP, KG_METADATA_REPAIR_BUMPS[-1])

    def test_an_empty_generation_is_never_recorded(self):
        """Stamping a guess retires the pass with the work unproven."""
        self.assertFalse(state.write_stamp(self.root, "   "))
        self.assertFalse(state.state_path(self.root).exists())


# ═════════════════════════════════════════════════════════════════════════
# 2. `--check-drift` puts it on the wire — the launcher's only input
# ═════════════════════════════════════════════════════════════════════════


class CheckDriftReportsRepairOwed(_SyncTestBase):
    """The verdict carries the SECOND question, without lying about the first."""

    def _run_probe(self, *, drift: bool = False):
        """Drive `--check-drift` with the store's answer injected.

        Backends are replaced by SENTINELS that raise: `--check-drift` is
        dispatched before any backend construction, and this proves the
        repair-owed leg did not change that (it must stay runnable with the
        embedding backend down).
        """
        mod = self.load()

        class _SentinelEmbeddingService:
            @staticmethod
            def for_project(root):  # noqa: ARG003
                raise RuntimeError("reached-backend")

        class _SentinelServer:
            def __init__(self, **kwargs):  # noqa: ARG004
                raise RuntimeError("reached-weaviate-constructor")

        mod.EmbeddingService = _SentinelEmbeddingService
        mod.WeaviateMCPServer = _SentinelServer

        report = (
            DriftReport(status="drift", scanned=1, missing=("knowledge/a.md",),
                        detail="1 missing, 0 stale out of 1 checked")
            if drift else
            DriftReport(status="ok", scanned=1, detail="1 node(s) verified in sync")
        )
        binding = BindingCheck(status="bound", kg_collection=PROJECT_KG, detail="")
        with mock.patch("vco_lib.kg_sync_drift.check_kg_binding",
                        return_value=binding), \
                mock.patch("vco_lib.kg_sync_drift.scan_drift",
                           return_value=report):
            code, out, err = self.run_main(mod, ["kg-sync", "--check-drift"])
        self.assertEqual(code, 0, f"the probe always exits 0. stderr:\n{err}")
        self.assertNotIn("reached-backend", out + err)
        self.assertNotIn("reached-weaviate-constructor", out + err)
        return _sentinel_payload(out), out

    def test_an_unstamped_project_reports_repair_owed(self):
        """RED-PROOF (a). The constructed failure, on the wire.

        A clean store (`status: ok`) and a project that has never run the
        pass. Pre-fix the verdict said `ok` and the gate skipped the project
        forever; the node's `tags` stayed prose-scraped.
        """
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        payload, out = self._run_probe()

        self.assertIs(payload["repair_owed"], True)
        # The FIRST question must stay honestly answered: these rows are
        # missing from nothing and stale in nothing.
        self.assertEqual(payload["status"], "ok")
        self.assertEqual((payload["missing"], payload["stale"]), (0, 0))
        self.assertIn("KG metadata repair: owed", out,
                      "the human output must say it too — a machine-only "
                      "signal is undiagnosable in a log_tail")

    def test_a_stamped_project_reports_nothing_owed(self):
        """RED-PROOF (b). It runs once per project, then converges silently."""
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        self.assertTrue(state.write_stamp(self.root))

        payload, out = self._run_probe()

        self.assertIs(payload["repair_owed"], False)
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("KG metadata repair: owed", out)

    def test_an_unreadable_stamp_reports_nothing_owed(self):
        """Prior behaviour, never a pass on every update."""
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        path = state.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{{", encoding="utf-8")

        payload, _out = self._run_probe()
        self.assertIs(payload["repair_owed"], False)

    def test_real_drift_still_reports_drift_with_its_counts(self):
        """The new field must not disturb the verdict that already worked."""
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        payload, _out = self._run_probe(drift=True)

        self.assertEqual(payload["status"], "drift")
        self.assertEqual(payload["missing"], 1)
        self.assertIs(payload["repair_owed"], True)


# ═════════════════════════════════════════════════════════════════════════
# 3. A clean `--all` stamps the project — and a failed one does not
# ═════════════════════════════════════════════════════════════════════════


class ACleanWholeTreeRunStampsTheProject(_SyncTestBase):
    def _stamp(self) -> Path:
        return state.state_path(self.root)

    def test_a_clean_all_writes_the_stamp(self):
        """RED-PROOF (a), completion half: the spawned pass retires itself."""
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        mod = self.load()
        self.install_working_backends(mod)

        self.assertFalse(self._stamp().exists(), "fixture sanity")
        code, _out, err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertTrue(self._stamp().exists(),
                        "a clean whole-tree run IS the pass; nothing else "
                        "will ever revisit these rows")
        self.assertEqual(
            json.loads(self._stamp().read_text(encoding="utf-8"))["generation"],
            KG_METADATA_REPAIR_STAMP,
        )
        self.assertIs(state.repair_owed(self.root), False)

    def test_a_second_run_costs_zero_embeds_and_keeps_the_stamp(self):
        """RED-PROOF (d): the cost rule, asserted on the embed COUNT.

        The pass is one fetch per node and zero embeds on an already-synced
        tree — never re-chunk, never a content_hash change.
        """
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        mod = self.load()
        harness = self.install_working_backends(mod)
        self.run_main(mod, ["kg-sync", "--all"])
        first_embeds = harness.last.embed_calls
        self.assertGreater(first_embeds, 0, "fixture sanity: the seed embedded")

        mod2 = self.load()
        harness2 = self.install_working_backends(mod2)
        # Carry the first run's store over, so run 2 sees rows already current.
        mod2.WeaviateMCPServer = lambda **kw: harness.last  # noqa: ARG005
        code, out, err = self.run_main(mod2, ["kg-sync", "--all"])

        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertEqual(
            harness.last.embed_calls, first_embeds,
            "the converged pass must embed NOTHING — an embed here means the "
            "repair is re-writing rows it should only be reading",
        )
        self.assertEqual(len(harness2.instances), 0, "fixture sanity")
        self.assertIn("📊 KG:", out)
        self.assertTrue(self._stamp().exists())

    def test_a_failed_run_leaves_no_stamp_so_it_retries(self):
        """RED-PROOF (c). Withholding the stamp IS the retry.

        A node the pass never reached keeps a content_hash that still
        MATCHES — it is computed from file bytes alone — so no later diff,
        drift scan or edit can see it is owed. Stamping a partial run would
        retire the repair permanently with the work undone.
        """
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        mod = self.load()
        self.install_working_backends(mod)
        mod.sync_node = lambda server, path: mod.SyncOutcome(  # noqa: ARG005
            mod.OUTCOME_FAILED, str(path), "injected failure",
        )

        code, _out, _err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 1, "per-node failures exit 1")
        self.assertFalse(self._stamp().exists(),
                         "a run that did not finish must leave the pass owed")
        self.assertIs(state.repair_owed(self.root), True)

    def test_an_incomplete_metadata_repair_blocks_the_stamp(self):
        """Zero sync failures is NOT the same as "every row was repaired".

        A repair that aborted part-way leaves the node stale behind a
        matching hash. The run exits 0 — nothing failed to SYNC — so only
        this counter can withhold the stamp.
        """
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        mod = self.load()
        self.install_working_backends(mod)
        real_sync_node = mod.sync_node

        def _sync_and_fail_a_repair(server, path):
            mod._METADATA_REPAIR_FAILED_COUNT += 1
            return real_sync_node(server, path)

        mod.sync_node = _sync_and_fail_a_repair

        code, out, err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 0, f"nothing failed to sync. stderr:\n{err}")
        self.assertIn("Metadata repair could not complete", out,
                      "the owed work must be named in the run report")
        self.assertFalse(self._stamp().exists())
        self.assertIs(state.repair_owed(self.root), True)

    def test_a_run_that_walked_an_empty_tree_stamps_it(self):
        """RETARGETED at round 6 (MINOR-6). An empty walk IS a complete walk.

        It used to assert the opposite — the stamp sat inside the
        ``kg_tally.total > 0 or not KNOWLEDGE_ROOT.exists()`` guard its three
        neighbours share. That guard is right for THEM: a drift entry names
        nodes and a re-chunk names entries, so both can still be true about a
        tree the run never looked at, and a clear must not fire on one that
        did not look. The repair stamp names neither — it records WHICH
        GENERATION this project's tree has been walked under, and a tree with
        nothing in it has no node left holding a stale property.

        Worse, the shared guard split the two EMPTY shapes: ``knowledge/``
        absent stamped (its ``or not KNOWLEDGE_ROOT.exists()`` exception)
        while ``knowledge/`` present-but-empty did not. Harmless while
        ``app_state`` was an independent record; once it became a projection
        of this stamp (round-6 MAJOR), the second shape re-fired an empty
        ``--all`` on every single update, forever.

        Both shapes are asserted here, in one place, so they cannot drift
        apart again.
        """
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Guide\n\nbody\n", encoding="utf-8")
        mod = self.load(dev="TestDev")
        self.install_working_backends(mod)

        code, out, err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertIn("📊 KG:   0 succeeded, 0 failed, 0 skipped", out)
        self.assertTrue(
            (self.root / "knowledge").is_dir(),
            "fixture sanity: this is the PRESENT-but-empty shape",
        )
        self.assertTrue(self._stamp().exists(),
                        "an empty walk under this generation is a complete "
                        "walk under it — withholding the stamp charges every "
                        "future update a pass with nothing to repair")
        self.assertIs(state.repair_owed(self.root), False)

        # The other empty shape: no `knowledge/` at all. Same answer.
        self._stamp().unlink()
        (self.root / "knowledge").rmdir()
        mod2 = self.load(dev="TestDev")
        self.install_working_backends(mod2)

        code2, _out2, err2 = self.run_main(mod2, ["kg-sync", "--all"])

        self.assertEqual(code2, 0, f"stderr:\n{err2}")
        self.assertTrue(self._stamp().exists(),
                        "the two empty-tree shapes must agree")


# ═════════════════════════════════════════════════════════════════════════
# 4. A pass over a collection the user does not read is not a pass
# ═════════════════════════════════════════════════════════════════════════


class OnlyAConfiguredRunStamps(_SyncTestBase):
    """Round-6 ship-gate MINOR-1 — the project half of the root's own rule.

    ``_resolve_collections`` falls back to the literal ``"KnowledgeGraph"``
    when the hub is unreachable AND ``KG_COLLECTION`` is unset. A by-hand
    ``--all`` in that state walks every node into a collection nobody reads;
    stamping it retires the repair for the collection they DO read, and the
    self-heal that covers the ordinary case does not reach here — a later
    configured ``--check-drift`` finds the real rows present at a matching
    hash, answers ``ok``, and looks no further.

    install.py closes the same hole at the root by passing
    ``bool(current_kg_collection)``; this is that rule where the sync script
    is the one that knows.
    """

    def test_the_resolver_reports_whether_the_kg_name_was_resolved(self):
        """The verdict itself — the one thing only the resolver can tell."""
        mod = self.load()

        # The harness's own fixture collection, not a fresh literal: a new
        # `<Stem>_KnowledgeGraph` name in tests/ is a classification the
        # fixture-class guard requires someone to make (v0.2.94), and this
        # assertion needs no name of its own to be true.
        os.environ["KG_COLLECTION"] = PROJECT_KG
        name, _dev, _shared, resolved = mod._resolve_collections()
        self.assertEqual(name, PROJECT_KG)
        self.assertTrue(resolved)

        os.environ.pop("KG_COLLECTION", None)
        name, _dev, _shared, resolved = mod._resolve_collections()
        self.assertEqual(name, "KnowledgeGraph", "the literal fallback")
        self.assertFalse(
            resolved,
            "downstream every caller sees one string, and a REAL collection "
            "can genuinely be called KnowledgeGraph — only this function can "
            "tell a configured name from the default it invented",
        )

    def test_a_run_against_the_fallback_collection_does_not_stamp(self):
        """RED-PROOF: the whole run, loaded with nothing configured."""
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")
        mod = self.load()
        fake_filter = mod.Filter  # the loader's; a re-exec drops it
        # Re-enter the module the way an unconfigured shell would: the
        # collection is resolved ONCE at import, so the env must be gone
        # BEFORE the module body runs again. The module's OWN loader re-runs
        # it — a second copy of `_load_sync_module` is the duplication the
        # file's header refuses, and `importlib.reload` cannot find a spec
        # for a synthetic module name.
        os.environ.pop("KG_COLLECTION", None)
        mod.__spec__.loader.exec_module(mod)  # same module, body re-run
        mod.Filter = fake_filter
        self.install_working_backends(mod)

        self.assertEqual(mod.COLLECTION_NAME, "KnowledgeGraph", "fixture sanity")

        code, out, err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 0, f"the run itself succeeds. stderr:\n{err}")
        self.assertFalse(
            state.state_path(self.root).exists(),
            "a whole-tree pass over the fallback collection proves nothing "
            "about the collection the user reads",
        )
        self.assertIs(state.repair_owed(self.root), True)
        self.assertIn("metadata-repair stamp withheld", out,
                      "and it says so — a silent withhold is unexplainable "
                      "when the pass runs again next update")


# ═════════════════════════════════════════════════════════════════════════
# 5. The signal is only ever raised where it can also be RETIRED
# ═════════════════════════════════════════════════════════════════════════


class TheSignalConverges(_SyncTestBase):
    """`repair_owed: true` is a remedy, so it must be able to stop being one.

    These run the REAL `check_kg_binding` (section 2 injects a bound one), to
    pin the state the launcher and a by-hand `--check-drift` actually reach.
    """

    def _probe(self):
        mod = self.load(dev="TestDev")
        code, out, err = self.run_main(mod, ["kg-sync", "--check-drift"])
        self.assertEqual(code, 0, f"the probe always exits 0. stderr:\n{err}")
        return _sentinel_payload(out), out

    def test_a_tree_with_no_bindable_content_is_never_reported_owed(self):
        """A remedy that cannot retire itself must not be printed.

        `check_kg_binding` answers `ok` — NOT `bound` — for a tree with no
        non-archived content, and there is nothing there for the pass to
        patch. Worse, nothing AUTOMATIC could act on it if there were: the
        launcher returns `skipped` for a project with no markdown WITHOUT
        running the script (`kg_sync.rs::run_sync_task`), so the spawn this
        signal asks for never happens and `owed` becomes an instruction that
        repeats forever. (Since round-6 MINOR-6 a by-hand `--all` over an
        empty tree DOES stamp, so the gate rests on the launcher's
        short-circuit, not on the stamp rule.) Asserted on the WIRE field and
        on the human line.
        """
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Guide\n\nbody\n", encoding="utf-8")

        payload, out = self._probe()

        self.assertNotEqual(payload["binding"], "bound", "fixture sanity")
        self.assertIs(payload["repair_owed"], False)
        self.assertNotIn("KG metadata repair: owed", out)

    def test_a_real_node_owes_the_pass_until_a_clean_all_retires_it(self):
        """The whole loop, end to end, on observables only.

        owed → the launcher's `--all` → stamp → not owed. Nothing else in
        the project changes between the two probes.
        """
        _write_node(self.root / "knowledge" / "concepts" / "n.md", "N")

        before, out_before = self._probe()
        self.assertEqual(before["binding"], "bound", "fixture sanity")
        self.assertIs(before["repair_owed"], True)
        self.assertIn("KG metadata repair: owed", out_before)

        mod = self.load(dev="TestDev")
        self.install_working_backends(mod)
        code, _out, err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertTrue(state.state_path(self.root).exists())

        after, out_after = self._probe()
        self.assertIs(after["repair_owed"], False,
                      "one pass per project, then silence — a second spawn "
                      "would re-walk the tree on every bundle update")
        self.assertNotIn("KG metadata repair: owed", out_after)


if __name__ == "__main__":
    unittest.main()
