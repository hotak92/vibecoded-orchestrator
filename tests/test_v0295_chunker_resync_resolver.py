# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 F1 — ``chunker_preset_overhaul_pending`` finally has a clear.

It was the last IMMORTAL condition id: the registry declared
``clear_probe = "paired-resolution"`` and named "the Rust chunker-revision
flow" as the site that settles it, and no such site exists in either language
(``launcher/src-tauri/src/commands/chunker_revision_deferral.rs`` emits only;
no ``resolve_conditions`` anywhere names the cid). Meanwhile
``vco_lib/chunker_revision.py::gate`` re-stamps its own sentinel at EMIT time,
so every later run reads stored == live and the one comparison that could have
re-derived anything is permanently satisfied.

Two shipped consequences, and the second is the expensive one:

1. The row was permanent for every user who crossed a chunker revision — while
   BOTH of its emitters promise, in the entry the user reads: *"Once you run
   the re-sync commands below, this deferral self-resolves on the next bundle
   update."* That is the INTENT this lane recovered, and the standing rule is
   that a false promise is made TRUE, not deleted.
2. ``templates/scripts/sync_knowledge_graph.py`` arms a per-node chunk-plan
   comparison *while the ledger carries the cid*, commented "it clears through
   its existing lifecycle … at which point the comparison stops being paid".
   A cid that never cleared made that cost permanent.

The mechanism under test: each half of the printed remedy stamps
``.claude/state/chunker-resync.json`` at the one point that proves it ran, and
``probe:py:chunker_resync_still_owed`` retires the row when BOTH name the
CURRENT revision. Every probe arm is tested in both directions — each one
gates the deletion of a user-visible record of owed work.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import chunker_revision as cr  # noqa: E402
from vco_lib import codegraph_deferrals as cd  # noqa: E402
from vco_lib import deferral_probes as dp  # noqa: E402
from vco_lib import doctor  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402

CID = "chunker_preset_overhaul_pending"
REV = "v9.9.9-test"
OLD_REV = "v0.0.1-test"


def _entry() -> DeferralEntry:
    """A row shaped like the one the revision gate emits."""
    return DeferralEntry(
        condition_id=CID,
        title="KG + codegraph re-sync recommended (chunker revision changed)",
        detected=f"The KG chunker revision changed to `{REV}`.",
        why_deferred=(
            "Once you run the re-sync commands below, this deferral "
            "self-resolves on the next bundle update."
        ),
        command_to_apply="kg-sync --all",
        severity="info",
    )


def _seed(folder: Path) -> None:
    from vco_lib.deferral_emit import emit

    emit(folder, _entry())


def _cids(folder: Path) -> list:
    return [e.condition_id for e in DeferralReport.read(folder).entries]


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _pin(self, revision=REV):
        """Pin the live chunker revision so the test does not depend on the
        checkout's current value (which moves every time boundaries change)."""
        return mock.patch.object(cr, "current_revision", return_value=revision)


class TheRegistryDeclaresARealClear(unittest.TestCase):
    """The defect in one assertion: the row must name a probe that EXISTS."""

    def test_the_condition_declares_a_python_probe(self) -> None:
        from vco_lib.deferral_registry import condition

        spec = condition(CID)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.clear_probe, "probe:py:chunker_resync_still_owed")

    def test_the_probe_name_resolves_to_a_callable(self) -> None:
        name = dp.registry_probe_name(CID)
        self.assertEqual(name, "chunker_resync_still_owed")
        self.assertIn(name, dp.PROBES)

    def test_the_ledger_no_longer_calls_it_a_paired_resolution(self) -> None:
        """The sentence every surface shows. Before the fix it said "auto —
        the component that emitted this entry clears it", naming a component
        that did not clear it."""
        sentence = dp.clear_mechanism_sentence(CID)
        self.assertNotIn("the component that emitted this entry", sentence)


class TheOwedProbe(_Tmp):
    """``resync_still_owed`` — every arm, both directions."""

    def test_no_stamp_file_is_still_owed(self) -> None:
        with self._pin():
            self.assertIs(cr.resync_still_owed(self.folder), True)

    def test_one_half_is_still_owed(self) -> None:
        with self._pin():
            cr.record_resync_half(self.folder, "kg")
            self.assertIs(cr.resync_still_owed(self.folder), True)

    def test_both_halves_at_the_current_revision_is_provably_over(self) -> None:
        with self._pin():
            cr.record_resync_half(self.folder, "kg")
            cr.record_resync_half(self.folder, "codegraph")
            self.assertIs(cr.resync_still_owed(self.folder), False)

    def test_the_second_half_does_not_erase_the_first(self) -> None:
        """The halves are run independently, often days apart. A write that
        replaced the file would make each half retire the other."""
        with self._pin():
            cr.record_resync_half(self.folder, "codegraph")
            cr.record_resync_half(self.folder, "kg")
        self.assertEqual(
            cr.read_resync_stamps(self.folder), {"kg": REV, "codegraph": REV},
        )

    def test_stamps_from_an_older_revision_are_owed_again(self) -> None:
        """A LATER crossing re-arms for free: the gate re-emits and the old
        stamps stop matching — nobody has to clear them."""
        with self._pin(OLD_REV):
            cr.record_resync_half(self.folder, "kg")
            cr.record_resync_half(self.folder, "codegraph")
        with self._pin(REV):
            self.assertIs(cr.resync_still_owed(self.folder), True)

    def test_an_unreadable_revision_is_unknown_not_over(self) -> None:
        with self._pin(None):
            self.assertIsNone(cr.resync_still_owed(self.folder))

    def test_a_corrupt_stamp_file_is_unknown_not_over(self) -> None:
        path = cr.resync_state_path(self.folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self._pin():
            self.assertIsNone(cr.resync_still_owed(self.folder))

    def test_an_unknown_revision_records_nothing_rather_than_guessing(self) -> None:
        with self._pin(None):
            self.assertFalse(cr.record_resync_half(self.folder, "kg"))
        self.assertFalse(cr.resync_state_path(self.folder).exists())

    def test_an_unknown_half_name_is_refused(self) -> None:
        with self._pin():
            self.assertFalse(cr.record_resync_half(self.folder, "typo"))
        self.assertFalse(cr.resync_state_path(self.folder).exists())

    def test_the_gate_sentinel_is_a_different_file(self) -> None:
        """The two answer different questions; merging them would put the
        emit-time re-stamp back on top of the clear evidence."""
        self.assertNotEqual(cr.RESYNC_STATE_REL, cr.STATE_REL)


class TheReconcilerClearsIt(_Tmp):
    """`doctor.reconcile_probe_cleared` — act / leave-alone.

    The pass F1's sibling lane built. Before this change the cid had no
    Python probe, so `probe_report` bucketed it `unprobed` and the reconciler
    could never touch it, whatever the user had done.
    """

    def test_a_half_done_remedy_keeps_the_entry(self) -> None:
        _seed(self.folder)
        with self._pin():
            cr.record_resync_half(self.folder, "kg")
            cleared = doctor.reconcile_probe_cleared(
                self.folder, log=lambda _l: None,
            )
        self.assertEqual(cleared, [])
        self.assertIn(CID, _cids(self.folder))

    def test_both_halves_done_clears_the_entry(self) -> None:
        _seed(self.folder)
        with self._pin():
            cr.record_resync_half(self.folder, "kg")
            cr.record_resync_half(self.folder, "codegraph")
            cleared = doctor.reconcile_probe_cleared(
                self.folder, log=lambda _l: None,
            )
        self.assertEqual(cleared, [CID])
        self.assertNotIn(CID, _cids(self.folder))

    def test_an_unknown_verdict_keeps_the_entry(self) -> None:
        """Positive evidence only: a corrupt stamp never deletes the record."""
        _seed(self.folder)
        path = cr.resync_state_path(self.folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        with self._pin():
            cleared = doctor.reconcile_probe_cleared(
                self.folder, log=lambda _l: None,
            )
        self.assertEqual(cleared, [])
        self.assertIn(CID, _cids(self.folder))

    def test_other_entries_are_left_alone(self) -> None:
        from vco_lib.deferral_emit import emit

        _seed(self.folder)
        emit(self.folder, DeferralEntry(
            condition_id="mcp_registration_failed", title="t", detected="d",
            why_deferred="w", command_to_apply="c", severity="warning",
        ))
        with self._pin():
            cr.record_resync_half(self.folder, "kg")
            cr.record_resync_half(self.folder, "codegraph")
            doctor.reconcile_probe_cleared(self.folder, log=lambda _l: None)
        self.assertEqual(_cids(self.folder), ["mcp_registration_failed"])


class TheCodeGraphHalf(_Tmp):
    """The analyzer's success point stamps — and ONLY for a forced recreate."""

    def test_a_force_recreate_walk_records_the_half(self) -> None:
        with self._pin():
            cd.record_successful_walk(self.folder, force_recreate=True)
        self.assertEqual(
            cr.read_resync_stamps(self.folder), {"codegraph": REV},
        )

    def test_an_incremental_walk_records_nothing(self) -> None:
        """The distinction the whole stamp turns on: an incremental walk
        hash-SKIPS unchanged entities, so it re-chunks nothing. Stamping it
        would retire the entry for work nobody did."""
        with self._pin():
            cd.record_successful_walk(self.folder, force_recreate=False)
        self.assertFalse(cr.resync_state_path(self.folder).exists())

    def test_the_paired_backend_clear_still_happens_either_way(self) -> None:
        """The pre-existing MAJOR-1 behaviour must survive the composition."""
        for force in (False, True):
            with self.subTest(force_recreate=force):
                with TemporaryDirectory() as td:
                    folder = Path(td)
                    cd.emit_no_backend(folder, RuntimeError("x"))
                    with self._pin():
                        removed = cd.record_successful_walk(
                            folder, force_recreate=force,
                        )
                    self.assertEqual(removed, 1)
                    self.assertEqual(_cids(folder), [])

    def test_the_clear_lands_even_if_the_stamp_cannot(self) -> None:
        """Order is the safety property: the pre-existing paired clear runs
        FIRST, so a stamp that cannot be written (read-only ``.claude/state``)
        never costs the analyzer the ledger resolve it has always done. The
        analyzer's own ``_deferral_op`` guard is what absorbs the raise."""
        cd.emit_no_backend(self.folder, RuntimeError("x"))
        with mock.patch.object(
            cr, "record_resync_half", side_effect=RuntimeError("read-only fs")
        ):
            with self.assertRaises(RuntimeError):
                cd.record_successful_walk(self.folder, force_recreate=True)
        self.assertEqual(_cids(self.folder), [])


class TheKgHalf(unittest.TestCase):
    """The tree sync's success point stamps — driven through real ``main()``.

    Not a source scan: ``main()`` is executed with ``--all`` against described
    tallies, so the assertion is that the CALL SITE fires under exactly the
    conditions the three sibling ledger clears beside it use.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_sync_module()

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        (self.folder / "knowledge").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_all(
        self, *, kg_considered: int, failures: int, rechunk: bool = False,
    ) -> None:
        mod = self.mod
        kg, docs = _tally(mod, kg_considered, failures), _tally(mod, 0, 0)
        with mock.patch.multiple(
            mod,
            PROJECT_ROOT=self.folder,
            KNOWLEDGE_ROOT=self.folder / "knowledge",
            # Per-process cache of the ledger probe; the module is loaded once
            # for the class, so each run must re-ask.
            _resync_pending_cache=None,
            _RECHUNK_FORCED=rechunk,
            EmbeddingService=mock.MagicMock(),
            WeaviateMCPServer=mock.MagicMock(),
            ensure_collection_exists=mock.Mock(return_value=True),
            ensure_dev_collection_exists=mock.Mock(),
            sync_all_nodes=mock.Mock(return_value=kg),
            sync_all_docs=mock.Mock(return_value=docs),
            _regen_node_formats_after_full_sync=mock.Mock(),
            _print_run_details=mock.Mock(),
            _validate_argv_flags=mock.Mock(),
            _refuse_fixture_shaped_targets=mock.Mock(),
        ):
            with mock.patch.object(
                sys, "argv", ["sync_knowledge_graph.py", "--all"]
            ):
                with self.assertRaises(SystemExit):
                    mod.main()

    def test_a_clean_full_sync_records_the_kg_half(self) -> None:
        """The entry IS in the ledger, so the plan comparison was armed for
        the whole walk — this run really did re-chunk."""
        _seed(self.folder)
        with mock.patch.object(cr, "current_revision", return_value=REV):
            self._run_all(kg_considered=3, failures=0)
        self.assertEqual(cr.read_resync_stamps(self.folder), {"kg": REV})

    def test_an_UNARMED_full_sync_records_nothing(self) -> None:
        """Review MAJOR-2, the false-clear direction. Without the cid in the
        ledger, `_chunker_resync_pending()` is false, the plan comparison never
        runs, and every unchanged node is hash-SKIPPED — the run re-chunks
        nothing. The window is reachable by hand: an orchestrator that has
        already updated while a project's bundle still lags, plus the bare
        `kg-sync --all` another entry's remedy prints. Stamping there would let
        a later `--force-recreate` alone retire the deferral with stale KG
        boundaries still in place."""
        with mock.patch.object(cr, "current_revision", return_value=REV):
            self._run_all(kg_considered=3, failures=0)
        self.assertFalse(cr.resync_state_path(self.folder).exists())

    def test_rechunk_arms_it_without_a_ledger_entry(self) -> None:
        """`--rechunk` is the other honest arming (MAJOR-R5-4): it forces the
        comparison for one run, so the half really is done."""
        with mock.patch.object(cr, "current_revision", return_value=REV):
            self._run_all(kg_considered=3, failures=0, rechunk=True)
        self.assertEqual(cr.read_resync_stamps(self.folder), {"kg": REV})

    def test_a_sync_with_failures_records_nothing(self) -> None:
        """Narrow clear: only a run that finished with ZERO failures proves
        every entry whose stored plan differed was re-chunked."""
        _seed(self.folder)
        with mock.patch.object(cr, "current_revision", return_value=REV):
            self._run_all(kg_considered=3, failures=1)
        self.assertFalse(cr.resync_state_path(self.folder).exists())

    def test_a_run_that_walked_no_knowledge_node_records_nothing(self) -> None:
        """A tree that exists and yielded zero outcomes re-chunked nothing."""
        _seed(self.folder)
        with mock.patch.object(cr, "current_revision", return_value=REV):
            self._run_all(kg_considered=0, failures=0)
        self.assertFalse(cr.resync_state_path(self.folder).exists())


def _tally(mod, considered: int, failures: int):
    """A real ``SyncTally`` with ``considered`` outcomes, ``failures`` failed."""
    tally = mod.SyncTally()
    for i in range(considered):
        status = mod.OUTCOME_FAILED if i < failures else mod.OUTCOME_SYNCED
        tally.add(mod.SyncOutcome(path=f"knowledge/n{i}.md", status=status))
    return tally


def _load_sync_module():
    """Load ``sync_knowledge_graph.py`` as a module (the loader shape
    ``test_v0292_kg_chunk_plan_transition`` established — the orchestrator root
    is PINNED to this repo so the script does not bind `weaviate_mcp.*` to
    another checkout's presets for the rest of the process)."""
    import importlib.util
    import os
    import uuid

    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ["WEAVIATE_URL"] = "http://127.0.0.1:9"
    os.environ["KG_COLLECTION"] = "TestProj_KnowledgeGraph"
    os.environ.pop("DEVELOPMENT_COLLECTION", None)

    path = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
    name = f"_sync_kg_resolver_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        with mock.patch.object(sys, "argv", ["sync_knowledge_graph.py"]):
            spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # pragma: no cover — env-dependent
        raise unittest.SkipTest(f"sync deps not installed ({exc})")
    return mod


class TheTransitionCostIsNoLongerPermanent(unittest.TestCase):
    """The second consequence: the plan comparison is armed by the cid, and
    the cid can now end. Pinned as a BEHAVIOUR of the arming predicate, not as
    a comment."""

    def test_the_comparison_is_armed_only_while_the_entry_lives(self) -> None:
        mod = TheKgHalf.mod if hasattr(TheKgHalf, "mod") else _load_sync_module()
        with TemporaryDirectory() as td:
            folder = Path(td)
            with mock.patch.multiple(mod, PROJECT_ROOT=folder,
                                     _resync_pending_cache=None,
                                     _RECHUNK_FORCED=False):
                self.assertFalse(mod._chunker_resync_pending())
            _seed(folder)
            with mock.patch.multiple(mod, PROJECT_ROOT=folder,
                                     _resync_pending_cache=None,
                                     _RECHUNK_FORCED=False):
                self.assertTrue(mod._chunker_resync_pending())
            from vco_lib.deferral_emit import resolve_conditions

            resolve_conditions(folder, (CID,))
            with mock.patch.multiple(mod, PROJECT_ROOT=folder,
                                     _resync_pending_cache=None,
                                     _RECHUNK_FORCED=False):
                self.assertFalse(mod._chunker_resync_pending())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
