# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unclaimed populated KG classes — the inverse half of the D18 evidence scan.

v0.2.92 (reported-not-fixed item 3). The field find: a live machine carried
``AgapeTest_KnowledgeGraph`` with 84 objects, NO binding row anywhere naming it,
and no source files in any registered project's folder — hours of embedded
data with no reader, invisible to every surface: the D18 verdicts are keyed
by REGISTERED project, the legacy-name detectors are hardcoded to specific
names, the Dev-collection orphan detector is hardcoded + 0-row-gated, and the
code-graph orphan detector covers Code* collections only.

The closure extends the SAME scan (no new IO, no new probe id): a populated
``*_KnowledgeGraph`` class that no binding row names and no registered
folder anchors is UNCLAIMED, and the doctor reports it — diagnosis only, no
drop command ever, probe failure never reads as orphaned.

Every test drives the PRODUCTION entry point — the scan itself, or
``vco_lib.doctor.run_doctor`` dispatching the registered probe — and each
guard names the mutation that must turn it red (see
knowledge/concepts/credited-mechanisms-that-never-fire-2026-09-04.md: a
guard that cannot go red is decoration).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_project,
    make_launcher_db,
    set_app_state,
)

from vco_lib import doctor  # noqa: E402
from vco_lib import deferral_probes  # noqa: E402
from vco_lib import kg_binding_doctor as kbd  # noqa: E402


class FakeMachine:
    """Same shape as the D18 suite's fixture: real shipped-schema launcher.db,
    injectable Weaviate seams, real files under tmp for path anchoring."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db = make_launcher_db(tmp)
        self._classes: dict[str, dict] = {}

    def add_project(self, pid, name, *, folder=None, kg_primary=None,
                    files=(), kg_shared=None):
        folder = Path(folder) if folder else self.tmp / (pid + "-folder")
        for rel in files:
            f = folder / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"# {rel}\n", encoding="utf-8")
        add_project(
            self.db,
            project_id=pid,
            name=name,
            folder_path=folder,
            kg_primary=kg_primary,
            kg_shared=kg_shared,
        )
        return folder

    def add_class(self, name, *, paths, count=None):
        self._classes[name] = {
            "paths": tuple(paths),
            "count": len(paths) if count is None else count,
        }

    def scan(self):
        return kbd.scan_kg_binding_evidence(
            db_path=self.db,
            list_classes=lambda: list(self._classes),
            count_objects=lambda c: self._classes[c]["count"],
            sample_paths=lambda c: self._classes[c]["paths"],
        )


def _agape_test_machine(tmp: Path):
    """The field find, reproduced: one registered healthy project + one
    populated class from a project that is no longer registered."""
    m = FakeMachine(tmp)
    files = [f"knowledge/concepts/l{i}.md" for i in range(3)]
    m.add_project("p1", "LiveName", kg_primary="LiveName_KnowledgeGraph",
                  files=files)
    m.add_class("LiveName_KnowledgeGraph", paths=files, count=3)
    # The removed project's class: populated, its paths resolve under NO
    # registered folder (the AgapeTest folder is gone with the project row).
    m.add_class(
        "AgapeTest_KnowledgeGraph",
        paths=[f"knowledge/concepts/g{i}.md" for i in range(4)],
        count=84,
    )
    return m


# ---------------------------------------------------------------------------
# The scan's unclaimed dimension
# ---------------------------------------------------------------------------


class ScanUnclaimedTests(unittest.TestCase):
    def test_removed_project_leftover_is_unclaimed(self):
        """MUT target: drop the unclaimed computation from the scan (return
        BindingEvidenceScan(verdicts=...) without the field) and this goes
        red — the AgapeTest class becomes invisible again."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            scan = _agape_test_machine(Path(td)).scan()
            self.assertEqual(scan.mismatches, ())  # the live project is fine
            self.assertEqual(
                [(u.name, u.count) for u in scan.unclaimed],
                [("AgapeTest_KnowledgeGraph", 84)],
            )

    def test_bound_class_is_not_unclaimed(self):
        """A class a binding row names is accounted for, whatever it holds."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            # No registered project anchors these paths, but a binding names
            # the class — someone owns it.
            m.add_project("p1", "Owner", kg_primary="Kept_KnowledgeGraph")
            m.add_class("Kept_KnowledgeGraph",
                        paths=["knowledge/concepts/x1.md",
                               "knowledge/concepts/x2.md"], count=2)
            scan = m.scan()
            self.assertEqual(scan.unclaimed, ())

    def test_class_claimed_by_a_project_is_not_unclaimed(self):
        """A class anchored to a registered folder is that project's
        evidence (the D18 dimension) even when unbound — claimed, not
        unclaimed. One home per signal, no double-reporting."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/c{i}.md" for i in range(3)]
            m.add_project("p1", "RealName", kg_primary="Ghost_KnowledgeGraph",
                          files=files)
            m.add_class("RealName_KnowledgeGraph", paths=files, count=3)
            scan = m.scan()
            self.assertTrue(scan.mismatches)  # the D18 ghost case
            self.assertEqual(scan.unclaimed, ())

    def test_shared_pointer_class_is_exempt(self):
        """The fresh-install shape: shared corpus seeded, root project not
        yet registered, NO binding rows at all. The canonical shared class
        (app_state pointer, default when unset) must not read as unclaimed —
        its lifecycle belongs to the shared-KG surfaces. MUT target: delete
        the `cls != exempt` exclusion and this goes red."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            set_app_state(m.db, "orchestrator_root_kg_collection",
                          "CanonicalShared_KnowledgeGraph")
            m.add_class(
                "CanonicalShared_KnowledgeGraph",
                paths=["knowledge/concepts/s1.md",
                       "knowledge/concepts/s2.md"], count=117,
            )
            scan = m.scan()
            self.assertEqual(scan.unclaimed, ())

    def test_shared_default_is_the_fallback_exemption(self):
        """Absent pointer key → the compiled-in default is exempt (mirrors
        the Rust getter + migration 028), so a pre-boot machine with the
        default-named corpus is not nagged either."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            m.add_class(
                kbd.DEFAULT_SHARED_KG_COLLECTION,
                paths=["knowledge/concepts/s1.md",
                       "knowledge/concepts/s2.md"], count=5,
            )
            scan = m.scan()
            self.assertEqual(scan.unclaimed, ())

    def test_unreachable_weaviate_is_not_orphaned(self):
        """Probe failure is not evidence: a scan that could not look emits
        nothing (None), so no class can ever read as orphaned through an
        outage."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            m.add_class("AgapeTest_KnowledgeGraph",
                        paths=["knowledge/concepts/g1.md"], count=84)
            scan = kbd.scan_kg_binding_evidence(
                db_path=m.db,
                list_classes=lambda: None,  # unreachable
            )
            self.assertIsNone(scan)

    def test_uncountable_class_is_not_unclaimed(self):
        """A class whose count could not be read is unknown, not unclaimed."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            m.add_class("AgapeTest_KnowledgeGraph",
                        paths=["knowledge/concepts/g1.md",
                               "knowledge/concepts/g2.md"], count=84)
            scan = kbd.scan_kg_binding_evidence(
                db_path=m.db,
                list_classes=lambda: list(m._classes),
                count_objects=lambda c: None,  # Aggregate failed
                sample_paths=lambda c: m._classes[c]["paths"],
            )
            self.assertEqual(scan.unclaimed, ())

    def test_unusable_sample_is_not_unclaimed(self):
        """A populated class whose objects carry no usable relative paths
        cannot be anchor-checked — unknown is not unclaimed."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            m.add_class("LegacyShape_KnowledgeGraph", paths=[], count=9)
            scan = m.scan()
            self.assertEqual(scan.unclaimed, ())


# ---------------------------------------------------------------------------
# The doctor probe + ledger entry, through run_doctor
# ---------------------------------------------------------------------------


def _resolvers(scan):
    return doctor.DoctorResolvers(
        kg_binding_evidence=lambda: scan,
        pin_rows=lambda: [],
    )


class DoctorUnclaimedTests(unittest.TestCase):
    def test_unclaimed_machine_gets_problem_finding_and_entry(self):
        """The production path end to end: run_doctor(full) emits the
        doctor-owned cid; the entry names the class and carries ONLY
        non-destructive remedies — no drop command anywhere in it.

        MUT target: make the probe skip the unclaimed branch (return the OK
        finding unconditionally) and this goes red."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            scan = _agape_test_machine(Path(td)).scan()
            report = doctor.run_doctor(
                Path(td), scope=doctor.SCOPE_FULL, resolvers=_resolvers(scan))
            findings = [f for f in report.findings
                        if f.probe == "kg_binding_evidence"]
            # TWO findings: the unclaimed PROBLEM plus the per-project
            # AGREEMENT (the D18 self-resolve finding must fire on this
            # branch too — its promise may not become conditional on the
            # unclaimed set being clean).
            self.assertEqual(len(findings), 2)
            f = next(x for x in findings
                     if x.condition_id == doctor.CID_KG_UNCLAIMED)
            ok = next(x for x in findings
                      if x.condition_id ==
                      doctor.CID_KG_BINDING_EVIDENCE_MISMATCH)
            self.assertEqual(f.status, doctor.STATUS_PROBLEM)
            self.assertEqual(ok.status, doctor.STATUS_OK)
            self.assertIn(doctor.CID_KG_BINDING_EVIDENCE_MISMATCH,
                          doctor.healthy_condition_ids(report))
            self.assertEqual(f.fix, doctor.FIX_DEFER)
            self.assertFalse(report.ok)
            entries = doctor.deferral_entries_for(report)
            self.assertEqual([e.condition_id for e in entries],
                             [doctor.CID_KG_UNCLAIMED])
            e = entries[0]
            combined = f"{e.detected} {e.command_to_apply} {e.why_deferred}"
            self.assertIn("AgapeTest_KnowledgeGraph", combined)
            self.assertIn("84", combined)
            self.assertEqual(e.severity, "info")
            self.assertEqual(e.dismiss_fields.get("classes"),
                             ["AgapeTest_KnowledgeGraph"])
            # The absolute constraint: no deletion path, printed or implied.
            for forbidden in ("DELETE /v1/schema", "drop the orphan",
                              "curl -X DELETE"):
                self.assertNotIn(forbidden, combined)
            self.assertIn("Identity tab", e.command_to_apply)
            self.assertIn("migrate-collections", e.command_to_apply)

    def test_ledger_write_end_to_end(self):
        """emit_findings (CLI path) writes UPDATE_DEFERRED.md carrying the
        cid, the class name, and the registry disposition."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            scan = _agape_test_machine(Path(td)).scan()
            folder = Path(td) / "proj"
            (folder / ".claude" / "context").mkdir(parents=True)
            report = doctor.run_doctor(folder, resolvers=_resolvers(scan))
            self.assertEqual(
                doctor.emit_findings(folder, report),
                [doctor.CID_KG_UNCLAIMED],
            )
            md = (folder / ".claude" / "context"
                  / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")
            for needle in (
                "kg_unclaimed_populated_classes",
                "AgapeTest_KnowledgeGraph",
                "Identity tab",
            ):
                self.assertIn(needle, md)
            self.assertIn("**Disposition**: action_required", md)
            self.assertNotIn("DELETE /v1/schema", md)

    def test_clean_machine_ok_finding_names_zero_unclaimed(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/w{i}.md" for i in range(3)]
            m.add_project("p1", "Acme", kg_primary="Acme_KnowledgeGraph",
                          files=files)
            m.add_class("Acme_KnowledgeGraph", paths=files, count=3)
            scan = m.scan()
            report = doctor.run_doctor(Path(td), resolvers=_resolvers(scan))
            self.assertTrue(report.ok)
            self.assertEqual(doctor.deferral_entries_for(report), [])
            # The D18 self-resolve contract is untouched by this change.
            self.assertIn(doctor.CID_KG_BINDING_EVIDENCE_MISMATCH,
                          doctor.healthy_condition_ids(report))
            payload = json.loads(json.dumps(report.to_dict()))
            (finding,) = [f for f in payload["findings"]
                          if f["probe"] == "kg_binding_evidence"
                          and f["status"] == "ok"]
            # The clean reading's "no unclaimed" signal is the EMPTY list in
            # the detail (the summary speaks only to the per-project
            # comparison) plus the absence of any problem finding.
            self.assertEqual(finding["detail"].get("unclaimed"), [])
            self.assertEqual(
                [f for f in payload["findings"]
                 if f["condition_id"] == doctor.CID_KG_UNCLAIMED],
                [],
            )

    def test_scan_none_emits_no_unclaimed_finding(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            report = doctor.run_doctor(
                Path(td), resolvers=_resolvers(None))
            self.assertEqual(
                [f for f in report.findings
                 if f.condition_id == doctor.CID_KG_UNCLAIMED],
                [],
            )

    def test_mismatch_pass_carries_unclaimed_in_detail_only(self):
        """While a D18 mismatch is live, the unclaimed classes ride the
        mismatch finding's detail (its summary and dismiss-key are the D18
        contract); the dedicated finding comes on the first clean pass."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/c{i}.md" for i in range(3)]
            m.add_project("p1", "RealName", kg_primary="Ghost_KnowledgeGraph",
                          files=files)
            m.add_class("RealName_KnowledgeGraph", paths=files, count=3)
            m.add_class("AgapeTest_KnowledgeGraph",
                        paths=[f"knowledge/concepts/g{i}.md" for i in range(4)],
                        count=84)
            scan = m.scan()
            report = doctor.run_doctor(Path(td), resolvers=_resolvers(scan))
            (finding,) = [f for f in report.findings
                          if f.probe == "kg_binding_evidence"]
            self.assertEqual(finding.condition_id,
                             doctor.CID_KG_BINDING_EVIDENCE_MISMATCH)
            self.assertEqual(finding.detail.get("unclaimed"),
                             ["AgapeTest_KnowledgeGraph"])
            # The mismatch entry's dismissal identity is NOT keyed on the
            # unclaimed set (that pass owes the D18 entry only).
            entries = doctor.deferral_entries_for(report)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].condition_id,
                             doctor.CID_KG_BINDING_EVIDENCE_MISMATCH)
            self.assertNotIn("classes", entries[0].dismiss_fields)


# ---------------------------------------------------------------------------
# The registry clear probe — the entry's documented lifecycle
# ---------------------------------------------------------------------------


class ClearProbeTests(unittest.TestCase):
    def test_registry_probe_registered_and_tri_state(self):
        """The toml's clear_probe must resolve (completeness gate) AND mean
        the right thing: True while unclaimed data exists, False once every
        populated class is accounted for, None when the scan could not look.

        MUT target: make the probe return bool(scan.mismatches) (copy-paste
        of the sibling) and the True leg goes red."""
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            scan = _agape_test_machine(Path(td)).scan()
            ctx = deferral_probes.ProbeContext(folder=Path(td))
            with mock.patch.object(kbd, "scan_kg_binding_evidence",
                                   return_value=scan):
                self.assertIs(
                    deferral_probes.run_probe(
                        "kg_unclaimed_classes_still_present", ctx),
                    True,
                )
            # Resolved: the class gained a binding (re-add / re-bind).
            resolved = kbd.BindingEvidenceScan(
                verdicts=scan.verdicts, unclaimed=(),
            )
            with mock.patch.object(kbd, "scan_kg_binding_evidence",
                                   return_value=resolved):
                self.assertIs(
                    deferral_probes.run_probe(
                        "kg_unclaimed_classes_still_present", ctx),
                    False,
                )
            # Unlookable: never reads as resolved.
            with mock.patch.object(kbd, "scan_kg_binding_evidence",
                                   return_value=None):
                self.assertIs(
                    deferral_probes.run_probe(
                        "kg_unclaimed_classes_still_present", ctx),
                    None,
                )

    def test_probe_defect_is_not_a_verdict(self):
        """An exception inside the scan reads None, never False."""
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            ctx = deferral_probes.ProbeContext(folder=Path(td))
            with mock.patch.object(
                kbd, "scan_kg_binding_evidence", side_effect=RuntimeError,
            ):
                self.assertIs(
                    deferral_probes.run_probe(
                        "kg_unclaimed_classes_still_present", ctx),
                    None,
                )


if __name__ == "__main__":
    unittest.main()
