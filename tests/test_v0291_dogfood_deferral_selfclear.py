# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 LIVE DOGFOOD — the three defects that survived two `--update` runs.

The shipped release's deferral self-resolution was verified in the field and
failed. Two consecutive `install.py --update` runs on the maintainer's install
left a four-entry ledger untouched, and the trail said *why* in three different
ways at once:

1. ``orchestrator_user_modified_preserved`` — RESURRECTED. Its sidecar was long
   gone, the probe returned "provably over", and
   ``auto-resolutions.jsonl`` recorded ``reconciled_stale_bundle_deferral`` at
   00:14:20Z **and again** at 00:17:27Z. The bundle reconcile (step 5b) cleared
   it from the ON-DISK ledger; install.py's A-2 seed had already copied it into
   the run report at t0; ``finalize()`` then rebuilt the file from memory and
   wrote the stale copy straight back. Cleared twice, present twice.
2. ``kg_access_phantom_repaired`` — NEVER EXPIRED. It is install-OWNED
   (``clear_probe = "owned-drop-when-absent"``), so the seed deliberately skips
   it and the single end-of-run write is supposed to drop it. The re-probe
   pass's ``[unknown]`` fallback re-added it from disk on every run, which put
   it back in the run report and defeated the ownership contract entirely.
3. ``codegraph_embed_resync_pending`` — NEVER RETRIED. Classed
   ``auto_retryable`` with no ``retry_action``, so ``handler_name_for`` returned
   None, ``owed_condition_ids`` never listed it, and the WP-H dispatcher could
   not select it. Fourteen ``deferral-retry-*.log`` files, every one ZERO BYTES:
   the driver printed one line per RESULT and produced no results, so a pass
   that found nothing owed was indistinguishable from a driver that died at
   import or never started.

Fixture provenance
------------------
``tests/fixtures/live_ledger_v0291_dogfood.json`` is the maintainer's actual
four-entry ledger (JSON sidecar, ``generated_at 2026-08-28T00:18:27Z``), copied
verbatim and then scrubbed of machine/project identity with SHAPE-PRESERVING
replacements only (home path, collection prefixes). That matters: these entries
carry NONE of the fields a v0.2.91-authored entry would (no ``disposition``, no
``probe_status``, and for three of the four no ``dismiss_fields``), so the
legacy-compatibility axis is genuinely exercised. A fixture written in the new
shape would pass every assertion below vacuously.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_emit as de  # noqa: E402
from vco_lib import deferral_probes as dp  # noqa: E402
from vco_lib import deferral_retry as dr  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402
from vco_lib.install_deferral_flow import InstallDeferralFlow  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "live_ledger_v0291_dogfood.json"

_SIDECAR_CID = "orchestrator_user_modified_preserved"
_RECORD_CID = "kg_access_phantom_repaired"
_RESYNC_CID = "codegraph_embed_resync_pending"
_TEMPLATE_CID = "template_review_pending"


def _load_install():
    """Import install.py without running main() (it guards on __main__)."""
    spec = importlib.util.spec_from_file_location(
        "install_for_dogfood_tests", REPO_ROOT / "install.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


install = _load_install()


def live_entries() -> "list[DeferralEntry]":
    """The four live entries, through the production sidecar parser."""
    payload = FIXTURE.read_text(encoding="utf-8")
    from vco_lib.deferral_report import _parse_json_sidecar

    entries = _parse_json_sidecar(payload)
    assert entries is not None, "the live fixture must parse as a v1 sidecar"
    return entries


def seed_live_ledger(folder: Path) -> None:
    """Write the live four-entry ledger into ``folder`` (both views)."""
    report = DeferralReport()
    for entry in live_entries():
        report.add_entry(entry)
    (folder / ".claude" / "context").mkdir(parents=True, exist_ok=True)
    report.write(folder)


class LiveFixtureRealismTests(unittest.TestCase):
    """The fixture must be LEGACY-shaped, or every test below is vacuous."""

    def test_fixture_carries_the_four_live_conditions(self):
        cids = [e.condition_id for e in live_entries()]
        self.assertEqual(
            cids,
            [_SIDECAR_CID, _RECORD_CID, _RESYNC_CID, _TEMPLATE_CID],
        )

    def test_fixture_entries_predate_the_v0291_fields(self):
        """No ``disposition``, no ``probe_status`` — the shape whose absence is
        the whole legacy axis. If a future edit "modernises" the fixture, this
        fails loudly rather than letting the compatibility tests pass on a
        fixture that no longer resembles the field."""
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for item in raw["entries"]:
            self.assertNotIn("disposition", item, item["condition_id"])
            self.assertNotIn("probe_status", item, item["condition_id"])

    def test_fixture_carries_no_machine_identity(self):
        blob = FIXTURE.read_text(encoding="utf-8")
        for leaked in ("martino", "VCODev", "VibeCodedOrchestrator"):
            self.assertNotIn(leaked, blob)

    def test_reader_returns_every_entry(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            seed_live_ledger(folder)
            got = [e.condition_id for e in DeferralReport.read(folder).entries]
            self.assertEqual(len(got), 4, got)


class LegacySidecarProbeTests(unittest.TestCase):
    """Defect 1, part A — the LIVE entry must be probe-resolvable at all."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.entry = next(
            e for e in live_entries() if e.condition_id == _SIDECAR_CID
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_paths_come_out_of_the_live_prose(self):
        self.assertEqual(
            dp.upstream_sidecar_paths(self.entry),
            ("docs/VCT_MODULE_MANIFEST_SPEC.md.from-upstream-5a9ae53",),
        )

    def test_kept_while_the_sidecar_is_on_disk(self):
        target = self.folder / "docs" / "VCT_MODULE_MANIFEST_SPEC.md.from-upstream-5a9ae53"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("upstream", encoding="utf-8")
        self.assertIs(
            dp.evaluate(self.folder, self.entry), True,
        )

    def test_resolved_once_the_sidecar_is_gone(self):
        """The field state: the sidecar was deleted on 2026-07-23 and the entry
        still sat in the ledger 36 days later."""
        self.assertIs(dp.evaluate(self.folder, self.entry), False)


class ResurrectionTests(unittest.TestCase):
    """Defect 1, part B — a mid-run clear must not be written back.

    RED BEFORE THE FIX: ``finalize()`` rebuilt the file from the in-memory
    report, which still held the entry the A-2 seed imported at t0, so the
    on-disk clear performed by step 5b was undone by the run's own final write.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        seed_live_ledger(self.folder)
        self.flow = InstallDeferralFlow(
            self.folder,
            owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
            owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _cids_on_disk(self) -> set:
        return {e.condition_id for e in DeferralReport.read(self.folder).entries}

    def test_entry_cleared_on_disk_mid_run_is_not_resurrected(self):
        self.flow.seed()
        self.assertTrue(self.flow.report.has_condition(_SIDECAR_CID))

        # What step 5b's bundle reconcile does: clear it from the ON-DISK
        # ledger through the locked emitter, exactly as the field trail shows.
        de.resolve_conditions(self.folder, (_SIDECAR_CID,))
        self.assertNotIn(_SIDECAR_CID, self._cids_on_disk())

        result = self.flow.finalize()
        self.assertNotIn(_SIDECAR_CID, self._cids_on_disk())
        self.assertIn(_SIDECAR_CID, result.vanished)
        self.assertIsNone(result.vanish_error)
        # Every other FOREIGN live entry survives — the drop is targeted, not a
        # purge. (`kg_access_phantom_repaired` is install-OWNED: the seed never
        # imported it, so the flow's own drop-when-absent semantics expire it
        # here. That is the contract defect 2 shows the re-probe pass undoing.)
        self.assertEqual(
            self._cids_on_disk(), {_RESYNC_CID, _TEMPLATE_CID},
        )

    def test_a_re_emit_this_run_wins_over_the_disk_clear(self):
        """The other side: a condition RE-DETECTED during the run carries fresh
        evidence and must be written, even though the on-disk copy was cleared
        earlier in the same run. Re-detection is not resurrection."""
        self.flow.seed()
        de.resolve_conditions(self.folder, (_SIDECAR_CID,))
        fresh = DeferralEntry(
            condition_id=_SIDECAR_CID, title="re-detected this run",
            detected="a NEW conflict was parked as `docs/Z.md.from-upstream-cafed00`",
            why_deferred="w", command_to_apply="c", severity="info",
        )
        self.flow.report.add_entry(fresh)

        result = self.flow.finalize()
        self.assertEqual(result.vanished, ())
        self.assertIn(_SIDECAR_CID, self._cids_on_disk())
        written = DeferralReport.read(self.folder).entry_for(_SIDECAR_CID)
        self.assertEqual(written.title, "re-detected this run")

    def test_untouched_ledger_is_written_back_whole(self):
        """No mid-run clear ⇒ nothing vanishes. The reconcile must not become a
        general "drop anything I cannot re-confirm" pass — every FOREIGN entry
        the seed imported is still written."""
        self.flow.seed()
        result = self.flow.finalize()
        self.assertEqual(result.vanished, ())
        self.assertEqual(
            self._cids_on_disk(),
            {_SIDECAR_CID, _RESYNC_CID, _TEMPLATE_CID},
        )


class OwnedRecordExpiryTests(unittest.TestCase):
    """Defect 2 — install-owned records must expire on the promised run.

    RED BEFORE THE FIX: the re-probe pass's ``[unknown]`` branch re-added every
    entry it had no handler for, including the install-OWNED record classes, so
    the drop-when-absent contract could never fire.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        seed_live_ledger(self.folder)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_pass(self, run_report: DeferralReport) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            install._apply_deferred_entries(
                run_report, self.folder, args=None, side_effects=False,
            )
        return buf.getvalue()

    def test_record_is_registered_as_install_owned(self):
        """Pins the premise: if the registry stops owning it, this whole
        expiry path is silently inapplicable and the test would pass for the
        wrong reason."""
        self.assertIn(_RECORD_CID, install._INSTALL_OWNED_CONDITION_IDS)

    def test_owned_record_not_re_detected_is_dropped(self):
        run_report = DeferralReport()  # the A-2 seed excludes owned ids
        out = self._run_pass(run_report)
        self.assertFalse(run_report.has_condition(_RECORD_CID), out)
        self.assertIn("[expired]", out)

    def test_owned_record_re_emitted_this_run_is_kept(self):
        """Both sides. A record whose emitter fired during THIS run is live —
        expiring it would delete a fact detected seconds earlier."""
        run_report = DeferralReport()
        run_report.add_entry(
            DeferralEntry(
                condition_id=_RECORD_CID, title="re-emitted", detected="d",
                why_deferred="w", command_to_apply="c", severity="info",
            )
        )
        out = self._run_pass(run_report)
        self.assertTrue(run_report.has_condition(_RECORD_CID), out)

    def test_foreign_unhandled_entry_is_still_preserved(self):
        """The expiry arm is keyed on OWNERSHIP, not on "no handler". A foreign
        entry nobody re-detects must still be preserved verbatim."""
        run_report = DeferralReport()
        out = self._run_pass(run_report)
        self.assertTrue(run_report.has_condition(_TEMPLATE_CID), out)

    def test_pass_accounts_for_every_ledger_entry(self):
        """The summary line must reconcile against the ledger, so "the pass
        reported 3 things and the ledger holds 4" can never again be a silent
        discrepancy."""
        run_report = DeferralReport()
        out = self._run_pass(run_report)
        self.assertIn("[summary] 4 ledger entries probed", out)


class LedgerHonestyTests(unittest.TestCase):
    """Every kept entry must SAY how it can end — no silent immortality."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        seed_live_ledger(self.folder)

    def tearDown(self):
        self._tmp.cleanup()

    def test_probe_pass_annotates_every_entry(self):
        report = DeferralReport.read(self.folder)
        result = dp.probe_report(self.folder, report)
        self.assertEqual(result.total, 4)
        self.assertEqual(len(result.statuses), 4)
        for entry in report.entries:
            if entry.condition_id in result.resolvable:
                continue
            self.assertTrue(entry.probe_status, entry.condition_id)

    def test_manual_dismiss_says_it_will_not_clear_itself(self):
        sentence = dp.clear_mechanism_sentence("weaviate_unreachable_at_bootstrap")
        self.assertIn("no automatic clear", sentence)

    def test_owned_record_says_the_next_update_drops_it(self):
        self.assertIn("auto", dp.clear_mechanism_sentence(_RECORD_CID))

    def test_status_round_trips_through_both_views(self):
        report = DeferralReport.read(self.folder)
        dp.probe_report(self.folder, report)
        report.write(self.folder)

        md = (self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("**Probe status**:", md)
        reread = DeferralReport.read(self.folder)
        self.assertTrue(reread.entry_for(_TEMPLATE_CID).probe_status)

    def test_bundle_update_leg_persists_the_status(self):
        """The GUI "Update bundle" button never goes through install.py's pass,
        so the honest-status write has to land on that leg too — otherwise a
        project whose only update surface is the button never learns why its
        entries are immortal."""
        from vco_lib import project_init

        project_init._reconcile_bundle_deferrals(
            self.folder,
            still_user_modified=False,
            still_skipped_existing=False,
            still_template_review_pending=True,
        )
        reread = DeferralReport.read(self.folder)
        self.assertTrue(reread.entry_for(_TEMPLATE_CID).probe_status)

    def test_bundle_leg_annotates_even_with_nothing_to_resolve(self):
        """The annotation must not ride on a resolution happening. A ledger
        holding only entries the bundle leg neither owns nor can probe is
        EXACTLY the immortal case that needs the explanation most — and it is
        the one both of the leg's early returns used to skip."""
        from vco_lib import project_init

        report = DeferralReport()
        report.add_entry(
            next(e for e in live_entries() if e.condition_id == _RECORD_CID)
        )
        report.write(self.folder)

        project_init._reconcile_bundle_deferrals(
            self.folder,
            still_user_modified=False,
            still_skipped_existing=False,
        )
        reread = DeferralReport.read(self.folder)
        self.assertTrue(reread.has_condition(_RECORD_CID))
        self.assertTrue(reread.entry_for(_RECORD_CID).probe_status)

    def test_unannotated_entry_renders_exactly_as_before(self):
        """Additive: a ledger written by any writer that does not re-probe is
        byte-identical to the pre-fix render."""
        from vco_lib.deferral_report import _render_entry

        entry = DeferralEntry(
            condition_id="x", title="t", detected="d", why_deferred="w",
            command_to_apply="c", severity="info",
        )
        self.assertNotIn("Probe status", _render_entry(entry))


class RetryWiringTests(unittest.TestCase):
    """Defect 3 — the resync condition must be REACHABLE by the dispatcher.

    RED BEFORE THE FIX: no ``retry_action`` on the row, so
    ``handler_name_for`` was None and the cid never appeared in
    ``owed_condition_ids`` — the ledger advertised a retry that could not run.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        seed_live_ledger(self.folder)
        (self.folder / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
        (self.folder / ".claude" / "scripts" / "analyze_code_graph.py").write_text(
            "# analyzer", encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_condition_declares_a_handler(self):
        self.assertEqual(dr.handler_name_for(_RESYNC_CID), "codegraph_resync")

    def test_live_ledger_reports_it_as_owed(self):
        self.assertIn(_RESYNC_CID, dr.owed_condition_ids(self.folder))

    def _dispatch(self, *, clear_after: bool):
        calls: list = []

        def runner(argv, cwd):
            calls.append(list(argv))
            if clear_after:
                de.resolve_conditions(self.folder, (_RESYNC_CID,))
            return 0

        with mock.patch.object(dr, "_project_name", return_value="DemoProj"):
            results = dr.dispatch(
                self.folder,
                condition_ids=[_RESYNC_CID],
                backend_probe=lambda _folder, _kind: True,
                runner=runner,
                single_instance=False,
            )
        return calls, results

    def test_child_is_the_r7_driver_not_a_bare_analyzer_walk(self):
        """The distinction the fix turns on: only the DRIVER re-probes the
        stale count and performs this cid's paired clear. A bare analyzer walk
        clears nothing, so it would be INCONCLUSIVE forever and burn the cap."""
        calls, _ = self._dispatch(clear_after=False)
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertIn("-m", argv)
        self.assertIn("vco_lib.codegraph_resync", argv)
        self.assertIn("--run-resync", argv)
        self.assertIn("--repo-root", argv)
        self.assertIn("--analyzer", argv)
        # Never destructive: a retry has consent to re-embed, not to drop.
        self.assertNotIn("--prune-stale", argv)
        self.assertNotIn("--force-recreate", argv)

    def test_exit_zero_without_the_childs_clear_is_inconclusive(self):
        """Exit-0-is-not-success, held for the new tenant too."""
        _, results = self._dispatch(clear_after=False)
        self.assertEqual([r.status for r in results], [dr.INCONCLUSIVE])

    def test_retried_only_when_the_child_cleared_the_ledger(self):
        with mock.patch.object(dr, "_record_resolution"):
            _, results = self._dispatch(clear_after=True)
        self.assertEqual([r.status for r in results], [dr.RETRIED])

    def test_code_backend_down_skips_without_spawning(self):
        calls: list = []

        def runner(argv, cwd):  # pragma: no cover — must never run
            calls.append(list(argv))
            return 0

        results = dr.dispatch(
            self.folder,
            condition_ids=[_RESYNC_CID],
            backend_probe=lambda _folder, _kind: False,
            runner=runner,
            single_instance=False,
        )
        self.assertEqual(calls, [])
        self.assertEqual([r.status for r in results], [dr.SKIPPED])


class RetryTrailTests(unittest.TestCase):
    """Defect 3, part B — a driver that logs nothing cannot be diagnosed.

    RED BEFORE THE FIX: ``main()`` printed one line per RESULT, so a pass with
    zero results wrote a ZERO-BYTE log. Fourteen of them on the maintainer's
    machine, all equally uninformative.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, *args) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = dr.main(["--folder", str(self.folder), *args])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_empty_ledger_still_writes_a_trail(self):
        out = self._main()
        self.assertNotEqual(out.strip(), "")
        self.assertIn("driver start", out)
        self.assertIn("no retryable condition owed", out)
        self.assertIn("driver done: 0 result(s)", out)

    def test_trail_names_the_gate_verdicts_and_the_child(self):
        seed_live_ledger(self.folder)
        (self.folder / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
        (self.folder / ".claude" / "scripts" / "analyze_code_graph.py").write_text(
            "# analyzer", encoding="utf-8"
        )
        buf = io.StringIO()
        with redirect_stdout(buf), \
                mock.patch.object(dr, "_project_name", return_value="DemoProj"):
            dr.dispatch(
                self.folder,
                condition_ids=[_RESYNC_CID],
                backend_probe=lambda _folder, _kind: True,
                runner=lambda _argv, _cwd: 0,
                single_instance=False,
            )
        out = buf.getvalue()
        self.assertIn("ledger read at", out)
        self.assertIn("backend gate", out)
        self.assertIn("spawning child", out)
        self.assertIn("child exited 0", out)
        self.assertIn("ledger re-read", out)

    def test_json_mode_keeps_stdout_a_machine_contract(self):
        """The trail must never corrupt ``--json`` output."""
        out = self._main("--json")
        self.assertEqual(json.loads(out), [])

    def test_spawn_writes_the_header_before_the_child_exists(self):
        """A child that dies before its first print must still leave a file
        that says who spawned it, with what argv, when."""
        logs: list = []

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                logs.append((argv, kwargs))

        with mock.patch.object(dr.subprocess, "Popen", _FakePopen), \
                mock.patch.object(
                    dr, "_log_path_for_stamp",
                    return_value=self.folder / "deferral-retry-test.log",
                ):
            self.assertTrue(dr.spawn_detached(self.folder))
        written = (self.folder / "deferral-retry-test.log").read_text(encoding="utf-8")
        self.assertIn("deferral-retry driver spawned", written)
        self.assertIn(str(self.folder), written)
        self.assertIn("vco_lib.deferral_retry", written)


class ConvergenceTests(unittest.TestCase):
    """One ledger state, every entry-iterating path — SAME entries, SAME tiers.

    The three dogfood defects were one shape wearing three coats: the probe
    pass looked at a different entry set than the ledger held, the expiry
    decision existed in one place and was undone in another, and the retry
    dispatcher's view of "owed" excluded a condition the ledger advertised as
    retryable. Sibling implementations of the same lifecycle step produce
    exactly that, so the fix ends with a test that fails when a future path
    starts seeing a different world from its siblings.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        seed_live_ledger(self.folder)
        self.all_cids = {
            e.condition_id for e in DeferralReport.read(self.folder).entries
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_probe_pass_sees_exactly_the_ledger(self):
        result = dp.probe_report(self.folder, DeferralReport.read(self.folder))
        self.assertEqual(set(result.probed) | set(result.unprobed), self.all_cids)
        self.assertEqual(set(result.statuses), self.all_cids)

    def test_install_pass_accounts_for_exactly_the_ledger(self):
        run_report = DeferralReport()
        buf = io.StringIO()
        with redirect_stdout(buf):
            install._apply_deferred_entries(
                run_report, self.folder, args=None, side_effects=False,
            )
        kept = {c for c in self.all_cids if run_report.has_condition(c)}
        settled = self.all_cids - kept
        self.assertEqual(kept | settled, self.all_cids)
        self.assertIn(f"[summary] {len(self.all_cids)} ledger entries", buf.getvalue())

    def test_bundle_leg_sees_exactly_the_ledger(self):
        from vco_lib import project_init

        _resolvable, statuses, _changed = project_init._probe_deferrals(
            self.folder, DeferralReport.read(self.folder)
        )
        self.assertEqual(set(statuses), self.all_cids)

    def test_every_partitioning_surface_agrees(self):
        """The doctor, the CLAUDE.md reminder and the report partition are one
        partition. The doctor was the last caller of the registry-only copy —
        it could not see an explicit ``disposition`` and so reported a tier the
        ledger it had just read disagreed with."""
        from vco_lib import doctor as doctor_mod
        from vco_lib.deferral_report import (
            _disposition_split_line, partition_entries,
        )

        report = DeferralReport.read(self.folder)
        actionable, informational = partition_entries(report)

        finding = next(
            f for f in doctor_mod.run_doctor(self.folder).findings
            if f.probe == "deferral_ledger"
        )
        self.assertEqual(
            set(finding.detail["actionable"]),
            {e.condition_id for e in actionable},
        )
        self.assertEqual(
            set(finding.detail["informational"]),
            {e.condition_id for e in informational},
        )
        self.assertIn(
            f"**{len(actionable)} actionable**, {len(informational)} "
            "informational/record",
            _disposition_split_line(report.entries),
        )

    def test_an_explicit_disposition_moves_every_surface_together(self):
        """The escalation case that split the partitions apart. An entry whose
        emitter set an explicit tier must count that way EVERYWHERE — the
        registry-only partition is blind to it by construction."""
        from vco_lib import doctor as doctor_mod
        from vco_lib.deferral_report import partition_entries

        report = DeferralReport.read(self.folder)
        escalated = report.entry_for(_RECORD_CID)  # registry tier: record
        escalated.disposition = "action_required"
        report.write(self.folder)

        actionable, _ = partition_entries(DeferralReport.read(self.folder))
        self.assertIn(_RECORD_CID, {e.condition_id for e in actionable})
        finding = next(
            f for f in doctor_mod.run_doctor(self.folder).findings
            if f.probe == "deferral_ledger"
        )
        self.assertIn(_RECORD_CID, finding.detail["actionable"])

    def test_retry_view_is_the_registry_declared_subset(self):
        expected = {
            c for c in self.all_cids if dr.handler_name_for(c) is not None
        }
        self.assertEqual(set(dr.owed_condition_ids(self.folder)), expected)
        self.assertTrue(expected, "the fixture must carry a retryable cid")


#: Bash heredoc form used by every `templates/hooks/*.sh` Python payload:
#: `"$RUN_PY" - <<'PYEOF' [trailing redirection] \n <body> \n PYEOF`.
_HOOK_SH_PYEOF_RE = re.compile(r"<<'PYEOF'[^\n]*\n(.*?)\nPYEOF\b", re.DOTALL)
#: PowerShell single-quoted here-string form used by every
#: `templates/hooks/*.ps1` Python payload: `$var = @' \n <body> \n '@`.
_HOOK_PS1_HERESTRING_RE = re.compile(r"@'\r?\n(.*?)\r?\n'@", re.DOTALL)


def _hook_heredoc_python_blocks(hooks_dir: Path):
    """Yield ``(label, python_source)`` for every embedded Python block under
    ``hooks_dir`` — bash heredocs (``<<'PYEOF' ... PYEOF``) in ``.sh`` files
    and single-quoted PowerShell here-strings (``@' ... '@``) in ``.ps1``
    files.

    v0.2.91 dogfood NIT-D3, arm (b): the AST scanner's file walk covered
    ``.py`` sources only. A hook's shell/PowerShell body is not valid Python
    — ``ast.parse`` on the WHOLE file would always hit the loud SyntaxError
    branch — so the embedded block has to be extracted first, source text
    only (never executed), then handed to the same detector production
    ``.py`` sources go through.

    ``hooks_dir`` is a parameter (not hardcoded to the real
    ``templates/hooks``) so a test can point this at a synthetic directory
    for a planted-offender self-check without touching the real tree.
    """
    for sh_path in sorted(hooks_dir.glob("*.sh")):
        text = sh_path.read_text(encoding="utf-8", errors="replace")
        for i, block in enumerate(_HOOK_SH_PYEOF_RE.findall(text)):
            yield f"{sh_path.name}::heredoc#{i}", block
    for ps1_path in sorted(hooks_dir.glob("*.ps1")):
        text = ps1_path.read_text(encoding="utf-8", errors="replace")
        for i, block in enumerate(_HOOK_PS1_HERESTRING_RE.findall(text)):
            yield f"{ps1_path.name}::herestring#{i}", block


class NoSecondPartitionTests(unittest.TestCase):
    """Source guard: entries are never partitioned by the cid-only helper."""

    #: The registry module OWNS the cid-only helper; the parity tests use it
    #: for the registry-level question it actually answers.
    _ALLOWED = {Path("vco_lib/deferral_registry.py")}

    def _production_sources(self):
        for rel in ("vco_lib", "templates/scripts", "claude_mcp_servers"):
            for path in (REPO_ROOT / rel).rglob("*.py"):
                yield path.relative_to(REPO_ROOT), path
        yield Path("install.py"), REPO_ROOT / "install.py"

    @staticmethod
    def _calls_cid_only_partition(source: str) -> bool:
        """True when the source really CALLS ``<mod>.split_by_disposition(...)``
        — either the attribute form (``deferral_registry.split_by_disposition``,
        any name ending ``deferral_registry``) or the direct-name form bound by
        ``from ...deferral_registry import split_by_disposition [as x]``
        (v0.2.91 dogfood NIT-D3, arm (a): the attribute-only check missed this
        shape entirely — a direct-name import never appears as an
        ``ast.Attribute``).

        AST, not text: the module docstrings deliberately quote the helper's
        name while explaining why not to use it, and a text scan flags those —
        so a text scan gets muted, and a muted gate is the fail-toward-green
        hole. A parse failure is reported as a hit (loud), never as clean.
        """
        import ast

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return True

        # Local names bound to split_by_disposition via a direct
        # `from ...deferral_registry import split_by_disposition [as x]`.
        direct_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not (node.module or "").endswith("deferral_registry"):
                continue
            for alias in node.names:
                if alias.name == "split_by_disposition":
                    direct_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "split_by_disposition"
                and isinstance(func.value, ast.Name)
                and func.value.id.endswith("deferral_registry")
            ):
                return True
            if isinstance(func, ast.Name) and func.id in direct_names:
                return True
        return False

    def test_scanner_detects_a_planted_offender(self):
        """Self-check: prove the AST scanner FIRES, and that the docstring
        mentions it must tolerate do NOT fire it."""
        self.assertTrue(self._calls_cid_only_partition(
            "from vco_lib import deferral_registry\n"
            "a, b = deferral_registry.split_by_disposition(cids)\n"
        ))
        self.assertFalse(self._calls_cid_only_partition(
            'def f():\n'
            '    """Never use deferral_registry.split_by_disposition(cids)."""\n'
        ))
        self.assertFalse(self._calls_cid_only_partition(
            "# deferral_registry.split_by_disposition(cids)\n"
        ))

    def test_scanner_detects_a_planted_importfrom_offender(self):
        """Self-check, NIT-D3 arm (a): a direct-name call bound by
        `from ...deferral_registry import split_by_disposition` must be
        FOUND — the attribute-only form (above) cannot see this shape at
        all, since there is no `ast.Attribute` node for it."""
        self.assertTrue(self._calls_cid_only_partition(
            "from vco_lib.deferral_registry import split_by_disposition\n"
            "a, b = split_by_disposition(cids)\n"
        ))
        self.assertTrue(self._calls_cid_only_partition(
            "from vco_lib.deferral_registry import split_by_disposition as sbd\n"
            "a, b = sbd(cids)\n"
        ))
        # Leave-alone: imported directly but never called (referenced only).
        self.assertFalse(self._calls_cid_only_partition(
            "from vco_lib.deferral_registry import split_by_disposition\n"
            "callback = split_by_disposition\n"
        ))
        # Leave-alone: the import is present but a DIFFERENT name is called.
        self.assertFalse(self._calls_cid_only_partition(
            "from vco_lib.deferral_registry import split_by_disposition\n"
            "other_func(cids)\n"
        ))

    def test_no_production_caller_of_the_cid_only_partition(self):
        offenders = [
            str(rel) for rel, path in self._production_sources()
            if rel not in self._ALLOWED
            and self._calls_cid_only_partition(
                path.read_text(encoding="utf-8", errors="replace")
            )
        ]
        self.assertFalse(
            offenders,
            f"{offenders} partition ENTRIES through the registry-only helper, "
            "which cannot see an entry's explicit `disposition`. Use "
            "vco_lib.deferral_report.partition_entries — the ONE partition the "
            "ledger, the GUI badge and the CLAUDE.md reminder all read.",
        )

    def test_hook_heredoc_scanner_detects_a_planted_offender(self):
        """Self-check, NIT-D3 arm (b): a hook's embedded Python heredoc/
        here-string is scanned too. Uses a synthetic hooks dir so the REAL
        `templates/hooks/` tree is never touched by the plant."""
        with TemporaryDirectory() as td:
            hooks_dir = Path(td)
            (hooks_dir / "fake-hook.sh").write_text(
                "#!/usr/bin/env bash\n"
                '"$PY" - <<\'PYEOF\' 2>/dev/null || true\n'
                "from vco_lib import deferral_registry\n"
                "a, b = deferral_registry.split_by_disposition(cids)\n"
                "PYEOF\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (hooks_dir / "fake-hook.ps1").write_text(
                "$pyCode = @'\n"
                "from vco_lib.deferral_registry import split_by_disposition\n"
                "a, b = split_by_disposition(cids)\n"
                "'@\n"
                "exit 0\n",
                encoding="utf-8",
            )
            offenders = [
                label for label, block in _hook_heredoc_python_blocks(hooks_dir)
                if self._calls_cid_only_partition(block)
            ]
        self.assertEqual(
            sorted(offenders),
            ["fake-hook.ps1::herestring#0", "fake-hook.sh::heredoc#0"],
        )

    def test_no_hook_heredoc_caller_of_the_cid_only_partition(self):
        """The production gate: no shipped hook's embedded Python calls the
        registry-only helper either — same rule, same fix, different host
        language wrapper."""
        hooks_dir = REPO_ROOT / "templates" / "hooks"
        offenders = [
            label for label, block in _hook_heredoc_python_blocks(hooks_dir)
            if self._calls_cid_only_partition(block)
        ]
        self.assertFalse(
            offenders,
            f"{offenders} partition ENTRIES through the registry-only helper "
            "inside a hook's embedded Python — same rule as production "
            "vco_lib callers: use vco_lib.deferral_report.partition_entries.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
