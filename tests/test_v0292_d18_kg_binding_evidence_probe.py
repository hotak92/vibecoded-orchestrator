# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The D18 read-only KG-binding evidence probe — behaviour, not source text.

v0.2.92 (Fable round 6, MAJOR-R6-1). The D18 KNOWN_ISSUES entry had been
deleted on the strength of a probe that heals a binding whose class is
ABSENT from Weaviate; the recorded symptom is a ghost that EXISTS (it
received the project's writes), which the heal pass skips by construction.
The closure shipped here is a READ-ONLY doctor probe comparing, per
registered project: (1) the primary binding, (2) FILE-BACKED evidence of
where the data lives, (3) the name-derived expected class.

Every test drives the PRODUCTION entry point —
:func:`vco_lib.doctor.run_doctor` dispatching the registered probe, or the
scan the probe's default resolver runs — never a source-scan for a symbol
name. The guards are pinned by the mutations named in each test's docstring
(see the review standard: a guard that cannot go red is decoration).
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_project,
    make_launcher_db,
)

from vco_lib import doctor  # noqa: E402
from vco_lib import kg_binding_doctor as kbd  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: one fake machine — launcher.db + Weaviate seams + project folders
# ---------------------------------------------------------------------------


class FakeMachine:
    """A registered-project registry, a Weaviate class listing, and folders.

    ``projects`` rows land in a REAL shipped-schema launcher.db; the Weaviate
    seams are the scan's own injectable IO (listing / count / sample); the
    project folders and their knowledge files are real files under
    ``tmp_path`` so the file-backed evidence rule exercises the REAL
    filesystem path (``_file_exists_default``), not a stub.
    """

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
        """One Weaviate class: distinct sampled file_paths + object count."""
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


def _realname_machine(tmp: Path, *, bound):
    """The R6 probe-table shape: Ghost EXISTS, Real holds the project's data.

    ``bound`` picks which class the primary binding names — the R6 fixture
    put the binding ON the ghost; the recorded field symptom had it on the
    real class. Both must mismatch, naming all three values.
    """
    m = FakeMachine(tmp)
    real_files = [
        "knowledge/concepts/a.md",
        "knowledge/concepts/b.md",
        "knowledge/concepts/c.md",
    ]
    m.add_project("p1", "RealName", kg_primary=bound, files=real_files)
    # The ghost: 7 objects (R6 table), only 2 of its sampled paths exist on
    # disk — it received SOME of the project's writes.
    m.add_class(
        "GhostName_KnowledgeGraph",
        paths=[*real_files[:2],
               "knowledge/concepts/gone-1.md", "knowledge/concepts/gone-2.md",
               "knowledge/concepts/gone-3.md", "knowledge/concepts/gone-4.md",
               "knowledge/concepts/gone-5.md"],
        count=7,
    )
    # The real class: every sampled path exists under the project folder.
    m.add_class("RealName_KnowledgeGraph", paths=real_files, count=3)
    return m


# ---------------------------------------------------------------------------
# The scan's decision logic (the seams the doctor's resolver composes)
# ---------------------------------------------------------------------------


class ScanDecisionTests(unittest.TestCase):
    def test_agreement_is_silent(self):
        """Binding == where the data lives == expected: no mismatch.

        MUT-B target: break the predicate and this goes red.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/x{i}.md" for i in range(4)]
            m.add_project("p1", "Acme", kg_primary="Acme_KnowledgeGraph",
                          files=files)
            m.add_class("Acme_KnowledgeGraph", paths=files, count=9)
            scan = m.scan()
            self.assertIsNotNone(scan)
            self.assertEqual(scan.mismatches, ())

    def test_binding_on_ghost_mismatch_names_all_three_values(self):
        """R6 probe table: binding stays on the EXISTING ghost; Real holds
        the data. The verdict must name bound, expected and the evidence."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = _realname_machine(Path(td), bound="GhostName_KnowledgeGraph")
            scan = m.scan()
            (v,) = scan.verdicts
            self.assertTrue(v.mismatch, "the unbound Real class holds the data")
            self.assertEqual(v.bound, "GhostName_KnowledgeGraph")
            self.assertEqual(v.expected, "RealName_KnowledgeGraph")
            unbound = [e.name for e in v.unbound_evidence]
            self.assertEqual(unbound, ["RealName_KnowledgeGraph"])

    def test_binding_on_real_class_with_ghost_writes_mismatch(self):
        """Recorded symptom: Identity tab correct, ghost receiving writes."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            # Ghost holds ALL of the project's files (it received the writes).
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/n{i}.md" for i in range(5)]
            m.add_project("p1", "RealName", kg_primary="RealName_KnowledgeGraph",
                          files=files)
            m.add_class("RealName_KnowledgeGraph", paths=files, count=5)
            # v0.2.95 F3: the UNBOUND ghost is `Stray_`, not `GhostName_`.
            # `GhostName` is in `fixture_class_guard.FIXTURE_PROJECT_NAMES`, and
            # a fixture-stemmed class no binding row names is no longer a
            # binding CANDIDATE — VCO refuses writes to it, so reporting it as
            # "where your data should be bound" is advice the product blocks.
            # Every OTHER use of `GhostName_` in this file is the BOUND class,
            # which the rule deliberately leaves alone.
            m.add_class("Stray_KnowledgeGraph", paths=files, count=5)
            scan = m.scan()
            (v,) = scan.verdicts
            self.assertTrue(v.mismatch)
            self.assertEqual(v.bound, "RealName_KnowledgeGraph")
            self.assertEqual(
                [e.name for e in v.unbound_evidence],
                ["Stray_KnowledgeGraph"],
            )

    def test_custom_bound_name_with_data_in_it_is_not_a_mismatch(self):
        """A deliberately custom-named binding whose class holds the data is
        a supported state (the Identity-tab picker exists for it) — crying
        wolf there would bury the real signal."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/c{i}.md" for i in range(3)]
            m.add_project("p1", "Acme", kg_primary="MyCustom_KnowledgeGraph",
                          files=files)
            m.add_class("MyCustom_KnowledgeGraph", paths=files, count=3)
            scan = m.scan()
            (v,) = scan.verdicts
            self.assertEqual(v.expected, "Acme_KnowledgeGraph")
            self.assertFalse(v.mismatch)

    def test_weaviate_unreachable_returns_none(self):
        """Probe failure is not evidence: an unreachable backend must never
        read as 'the class is missing' NOR as agreement. The scan returns
        None; the doctor must emit NOTHING for it.

        MUT-C target: render unknown as an empty scan and this goes red.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            m.add_project("p1", "Acme", kg_primary="Acme_KnowledgeGraph",
                          files=["knowledge/concepts/a.md"])
            scan = kbd.scan_kg_binding_evidence(
                db_path=m.db, list_classes=lambda: None,
            )
            self.assertIsNone(scan)

    def test_unreadable_db_returns_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "not-a.db"
            bad.write_text("this is not a database", encoding="utf-8")
            self.assertIsNone(
                kbd.scan_kg_binding_evidence(db_path=bad,
                                             list_classes=lambda: [])
            )

    def test_project_without_primary_binding_row_is_skipped(self):
        """A name-DERIVED primary is a guess with nothing to compare; the
        heal pass owns bindingless rows. No verdict for such a project —
        even when a class holds its files.

        MUT-F target: treat derived primaries as bindings and this goes red.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/d{i}.md" for i in range(3)]
            m.add_project("p1", "Acme", kg_primary=None, files=files)
            m.add_class("Acme_KnowledgeGraph", paths=files, count=3)
            scan = m.scan()
            self.assertEqual(scan.verdicts, ())

    def test_unknown_count_is_not_zero(self):
        """A class whose Aggregate failed (None) is skipped — unknown is
        never treated as empty, and never as evidence."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/u{i}.md" for i in range(3)]
            m.add_project("p1", "Acme", kg_primary="Acme_KnowledgeGraph",
                          files=files)
            m.add_class("GhostName_KnowledgeGraph", paths=files, count=None)
            m._classes["GhostName_KnowledgeGraph"]["count"] = None
            scan = kbd.scan_kg_binding_evidence(
                db_path=m.db,
                list_classes=lambda: ["GhostName_KnowledgeGraph"],
                count_objects=lambda c: None,
                sample_paths=lambda c: tuple(files),
            )
            (v,) = scan.verdicts
            self.assertFalse(v.mismatch)

    def test_class_bound_by_another_project_is_not_my_ghost(self):
        """Two projects can share relative paths (template files, copied
        notes). A class ANOTHER project binds is that project's — even at
        100% path overlap — and must not appear as this project's unbound
        evidence.

        MUT-D target: drop the owners map and this goes red.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            shared = [f"knowledge/concepts/s{i}.md" for i in range(4)]
            m.add_project("pa", "Alpha", kg_primary="Alpha_KnowledgeGraph",
                          files=shared)
            m.add_project("pb", "Beta", kg_primary="Beta_KnowledgeGraph",
                          files=shared)
            # Beta's class: every path exists under BOTH folders, and Beta
            # binds it. Alpha's own class holds nothing.
            m.add_class("Beta_KnowledgeGraph", paths=shared, count=4)
            scan = m.scan()
            by_name = {v.project_name: v for v in scan.verdicts}
            self.assertFalse(by_name["Alpha"].mismatch,
                             "Beta's bound class is not Alpha's ghost")
            self.assertFalse(by_name["Beta"].mismatch)

    def test_single_coincidental_path_is_not_evidence(self):
        """One shared path between two projects is noise (measured 1-of-100
        cross-matches on a healthy machine): below MIN_MATCHED_PATHS."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/p{i}.md" for i in range(5)]
            m.add_project("p1", "Acme", kg_primary="Acme_KnowledgeGraph",
                          files=files)
            # Unbound class: only ONE of its five paths exists under Acme.
            m.add_class(
                "Leftover_KnowledgeGraph",
                paths=[files[0], "gone/1.md", "gone/2.md", "gone/3.md",
                       "gone/4.md"],
                count=5,
            )
            scan = m.scan()
            (v,) = scan.verdicts
            self.assertFalse(v.mismatch)

    def test_copied_notes_majority_is_not_ownership(self):
        """The live-calibration case: an unbound leftover class of ANOTHER
        project whose notes were copied into this folder matched at 30/58
        (52%). Below OWNERSHIP_MATCH_FRACTION it must not count — a true
        ghost is written ONLY by the project's own pipeline and matches at
        ~100%.

        MUT-E target: lower the bar to a plain majority and this goes red.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/k{i}.md" for i in range(3)]
            m.add_project("p1", "Acme", kg_primary="Acme_KnowledgeGraph",
                          files=files)
            # Unbound class: 3 of 5 sampled paths exist here (60% < 80%).
            m.add_class(
                "Leftover_KnowledgeGraph",
                paths=[*files, "gone/1.md", "gone/2.md"],
                count=5,
            )
            scan = m.scan()
            (v,) = scan.verdicts
            self.assertFalse(v.mismatch)

    def test_scan_never_writes_the_binding_table(self):
        """R38: a previous 'fix' here would have re-stamped the ghost. This
        probe must leave launcher.db BYTE-IDENTICAL — asserted directly, on
        the real fixture DB, across a mismatching scan."""
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = _realname_machine(Path(td), bound="GhostName_KnowledgeGraph")
            before = hashlib.sha256(m.db.read_bytes()).hexdigest()
            scan = m.scan()
            self.assertTrue(scan.mismatches, "fixture must mismatch for the "
                             "no-write check to mean anything")
            after = hashlib.sha256(m.db.read_bytes()).hexdigest()
            self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# The doctor wiring — every assertion through run_doctor
# ---------------------------------------------------------------------------


def _resolvers(scan):
    """DoctorResolvers with the KG scan injected and npm pinned hermetic.

    The other defaults match what the existing doctor test suite already
    runs against a fake folder (disk/git/which probes on /tmp paths).
    """
    return doctor.DoctorResolvers(
        kg_binding_evidence=lambda: scan,
        pin_rows=lambda: [],
    )


class DoctorWiringTests(unittest.TestCase):
    def test_probe_is_registered_and_emits_deferral_through_run_doctor(self):
        """The production path: run_doctor(full) dispatches the registered
        probe; the problem finding maps to the doctor-owned cid; the entry
        names ALL THREE VALUES and the Identity-tab remedy.

        MUT-A target: unregister the probe and this goes red.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = _realname_machine(Path(td), bound="GhostName_KnowledgeGraph")
            scan = m.scan()
            self.assertIn("kg_binding_evidence", doctor.PROBES)
            report = doctor.run_doctor(Path(td), scope=doctor.SCOPE_FULL,
                                       resolvers=_resolvers(scan))
            findings = [f for f in report.findings
                        if f.probe == "kg_binding_evidence"]
            self.assertEqual(len(findings), 1)
            f = findings[0]
            self.assertEqual(f.status, doctor.STATUS_PROBLEM)
            self.assertEqual(f.condition_id,
                             doctor.CID_KG_BINDING_EVIDENCE_MISMATCH)
            self.assertEqual(f.fix, doctor.FIX_DEFER)
            self.assertFalse(report.ok)
            # The deferral the doctor owes for this finding.
            entries = doctor.deferral_entries_for(report)
            self.assertEqual([e.condition_id for e in entries],
                             [doctor.CID_KG_BINDING_EVIDENCE_MISMATCH])
            e = entries[0]
            combined = f"{e.detected} {e.command_to_apply}"
            for value in ("GhostName_KnowledgeGraph",        # value 1: bound
                          "RealName_KnowledgeGraph",          # value 3: expected
                          "UNBOUND"):                         # value 2: evidence
                self.assertIn(value, combined, f"deferral must name {value}")
            self.assertIn("Identity tab", e.command_to_apply)
            self.assertEqual(e.dismiss_fields.get("projects"), ["RealName"])
            self.assertEqual(
                e.dismiss_fields.get("evidence_classes"),
                ["RealName_KnowledgeGraph"],
            )
            # The JSON report carries the three values machine-readably.
            (payload,) = f.detail["verdicts"]
            self.assertEqual(payload["bound"], "GhostName_KnowledgeGraph")
            self.assertEqual(payload["expected"],
                             "RealName_KnowledgeGraph")
            self.assertEqual(payload["unbound"], ["RealName_KnowledgeGraph"])

    def test_agreement_emits_ok_finding_that_self_resolves(self):
        """A clean reading clears the entry the same reading once emitted —
        the cid rides the OK finding (the disk_space contract)."""
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
            self.assertIn(doctor.CID_KG_BINDING_EVIDENCE_MISMATCH,
                          doctor.healthy_condition_ids(report))

    def test_end_to_end_ledger_write_carries_values_and_disposition(self):
        """emit_findings (the CLI path, no sink) writes UPDATE_DEFERRED.md
        with the cid, both class names, the Identity-tab remedy, and the
        REGISTRY-declared disposition — the lifecycle a reader is promised."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            m = _realname_machine(Path(td), bound="GhostName_KnowledgeGraph")
            scan = m.scan()
            folder = Path(td) / "proj"
            (folder / ".claude" / "context").mkdir(parents=True)
            report = doctor.run_doctor(
                folder, resolvers=_resolvers(scan))
            self.assertEqual(
                doctor.emit_findings(folder, report),
                [doctor.CID_KG_BINDING_EVIDENCE_MISMATCH],
            )
            md = (folder / ".claude" / "context"
                  / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")
            for needle in (
                "kg_binding_evidence_mismatch",
                "GhostName_KnowledgeGraph",
                "RealName_KnowledgeGraph",
                "Identity tab",
            ):
                self.assertIn(needle, md)
            self.assertIn("**Disposition**: action_required", md)

    def test_scan_none_emits_no_finding_at_all(self):
        """Unreachable Weaviate: NOTHING is emitted — not a problem, not an
        unknown, not an ok. A backend that cannot be looked at must not read
        as 'the class is missing' (nor as agreement, which would wrongly
        self-resolve a live entry)."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            report = doctor.run_doctor(
                Path(td), resolvers=_resolvers(None))
            self.assertEqual(
                [f for f in report.findings
                 if f.probe == "kg_binding_evidence"],
                [],
            )
            # Not even a self-resolve: an unlookable state must not clear a
            # live entry either. (Other probes' healthy ids are theirs.)
            self.assertNotIn(doctor.CID_KG_BINDING_EVIDENCE_MISMATCH,
                             doctor.healthy_condition_ids(report))

    def test_boot_scope_excludes_the_probe(self):
        """The scan is network I/O; boot stays the cheap subset (and the
        boot-ledger promise holds: no boot problem names a condition only
        the full scope can clear)."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            report = doctor.run_doctor(
                Path(td), scope=doctor.SCOPE_BOOT, resolvers=_resolvers(None))
            self.assertNotIn("kg_binding_evidence",
                             [f.probe for f in report.findings])


class DefaultResolverTests(unittest.TestCase):
    def test_default_resolver_chain(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            m = _realname_machine(Path(td), bound="GhostName_KnowledgeGraph")
            classes = dict(m._classes)
            with mock.patch.object(kbd, "_list_classes_default",
                                   lambda url: list(classes)), \
                 mock.patch.object(kbd, "_count_objects_default",
                                   lambda name, url: classes[name]["count"]), \
                 mock.patch.object(kbd, "_sample_file_paths_default",
                                   lambda name, url: classes[name]["paths"]), \
                 mock.patch.dict(os.environ,
                                 {"VCT_LAUNCHER_DB_PATH": str(m.db)}):
                report = doctor.run_doctor(
                    Path(td),
                    resolvers=doctor.DoctorResolvers(pin_rows=lambda: []),
                )
            findings = [f for f in report.findings
                        if f.probe == "kg_binding_evidence"]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].status, doctor.STATUS_PROBLEM)
            self.assertEqual(findings[0].condition_id,
                             doctor.CID_KG_BINDING_EVIDENCE_MISMATCH)


class ClearProbeTests(unittest.TestCase):
    def test_registry_probe_wraps_the_same_scan(self):
        """The registry's clear probe re-runs the IDENTICAL scan — one home
        for the rule, so it can never clear what the doctor re-emits."""
        import tempfile
        from unittest import mock

        from vco_lib import deferral_probes
        from vco_lib.kg_binding_doctor import BindingEvidenceScan

        with tempfile.TemporaryDirectory() as td:
            m = _realname_machine(Path(td), bound="GhostName_KnowledgeGraph")
            scan = m.scan()
            with mock.patch.object(kbd, "scan_kg_binding_evidence",
                                   return_value=scan):
                ctx = deferral_probes.ProbeContext(folder=Path(td))
                self.assertIs(
                    deferral_probes.run_probe(
                        "kg_binding_evidence_still_mismatched", ctx),
                    True,
                )
            with mock.patch.object(kbd, "scan_kg_binding_evidence",
                                   return_value=BindingEvidenceScan(())):
                self.assertIs(
                    deferral_probes.run_probe(
                        "kg_binding_evidence_still_mismatched", ctx),
                    False,
                )
            with mock.patch.object(kbd, "scan_kg_binding_evidence",
                                   return_value=None):
                self.assertIs(
                    deferral_probes.run_probe(
                        "kg_binding_evidence_still_mismatched", ctx),
                    None,
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# v0.2.102 W4-C L2 — the doctor's KG drift census (kg_drift_census probe)
# ---------------------------------------------------------------------------


class DriftCensusTests(unittest.TestCase):
    """The stored-vs-disk census: `scan_drift` finally has a doctor leg.

    Every test drives the PRODUCTION probe through the registered dispatch
    (``run_doctor``) or the probe function itself, with the REAL
    :func:`vco_lib.kg_sync_drift.scan_drift` under the scanner's own IO seams
    (``reachable_fn`` / ``query_hashes_fn``) — the census is wiring, the
    scanner is the one home for the comparison.
    """

    def _identity(self, tmp, *, name="Alpha", collection="Alpha_KnowledgeGraph"):
        from vco_lib.project_identity import (
            SOURCE_BINDING,
            IdentitySnapshot,
            ProjectIdentity,
        )

        folder = tmp / (name + "-folder")
        folder.mkdir(parents=True, exist_ok=True)
        ident = ProjectIdentity(
            name=name, folder_path=str(folder),
            kg_primary=collection, kg_primary_source=SOURCE_BINDING,
        )
        snapshot = IdentitySnapshot(resolvable=True, projects=(ident,))
        return ident, folder, snapshot

    def _knowledge_node(self, folder, rel="concepts/x.md", text="# x\nbody\n"):
        kdir = folder / "knowledge"
        (kdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (kdir / rel).write_text(text, encoding="utf-8")
        return "knowledge/" + rel

    def _census(self, tmp, *, stored_own=True, extra_hashes=None,
                reachable=True):
        """The probe with the REAL scanner over fake Weaviate seams.

        ``stored_own``: whether the on-disk node's own hash is in the stored
        set (False = the disk-only case). ``extra_hashes``: further stored
        rows (the stored-only case)."""
        from functools import partial

        from vco_lib import kg_sync_drift
        from vco_lib import doctor as _doctor

        _ident, folder, snapshot = self._identity(tmp)
        rel = self._knowledge_node(folder)
        signature = kg_sync_drift.content_signature_excluding_updated(
            (folder / rel).read_text(encoding="utf-8")
        )
        hashes = {} if not stored_own else {rel: signature}
        hashes.update(extra_hashes or {})
        with mock.patch.object(
            kg_binding_doctor_project_identity(), "resolve_snapshot",
            return_value=snapshot,
        ):
            with mock.patch.object(
                kg_sync_drift, "scan_drift",
                partial(
                    kg_sync_drift.scan_drift,
                    reachable_fn=lambda url: reachable,
                    query_hashes_fn=lambda url, coll, on_warn=None: dict(hashes),
                ),
            ):
                report = _doctor.run_doctor(
                    tmp, resolvers=_doctor.DoctorResolvers(),
                )
        return next(
            f for f in report.findings if f.probe == "kg_drift_census"
        ), report, rel, signature

    def test_disk_only_node_is_reported_by_the_doctor_row(self):
        """The L2 red-proof: one disk-only node → a problem row naming the
        count and the remedy command. Pre-fix there was NO kg_drift_census
        row at all."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            finding, report, _rel, _sig = self._census(
                Path(td), stored_own=False, reachable=True,
            )
            self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
            self.assertIn("1 disk-only", finding.summary)
            self.assertEqual(finding.detail["projects"][0]["disk_only"], 1)
            self.assertIn("kg-sync --all", finding.command)
            self.assertFalse(report.ok, "a drift census problem must fail the report")

    def test_clean_project_is_an_ok_row(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            finding, report, _rel, _sig = self._census(
                Path(td), stored_own=True, reachable=True,
            )
            self.assertEqual(finding.status, doctor.STATUS_OK)
            self.assertTrue(report.ok)

    def test_weaviate_down_is_unknown_never_a_crash(self):
        """Soft-fail per project: Weaviate down → one warning row, `ok`."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            finding, report, _rel, _sig = self._census(
                Path(td), stored_own=False, reachable=False,
            )
            self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)
            self.assertIn("Alpha", finding.summary)
            self.assertTrue(report.ok, "an unknown census never fails the report")

    def test_stored_only_row_is_census_drift_but_not_gate_drift(self):
        """A row whose file is gone: the scanner answers `ok` (no re-sync is
        owed — the bundle-update gate must not spawn one), and the census
        still reports it as drift with its own remedy."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            from vco_lib import kg_sync_drift

            _ident, folder, snapshot = self._identity(tmp=Path(td))
            rel = self._knowledge_node(folder)
            own_sig = kg_sync_drift.content_signature_excluding_updated(
                (folder / rel).read_text(encoding="utf-8")
            )
            report = kg_sync_drift.scan_drift(
                folder / "knowledge",
                weaviate_url="http://127.0.0.1:9",
                kg_collection="Alpha_KnowledgeGraph",
                reachable_fn=lambda url: True,
                query_hashes_fn=lambda url, coll, on_warn=None: {
                    rel: own_sig,
                    "knowledge/concepts/gone.md": "deadbeef",
                },
            )
            self.assertEqual(report.status, "ok",
                             "stored-only alone must NOT flip the gate's status")
            self.assertEqual(report.stored_only, ("knowledge/concepts/gone.md",))
            # And the census reports it as drift with its own remedy (the
            # same stored set, through the probe with patched scanner seams).
            from functools import partial

            from vco_lib import doctor as _doctor

            with mock.patch.object(
                kg_binding_doctor_project_identity(), "resolve_snapshot",
                return_value=snapshot,
            ):
                with mock.patch.object(
                    kg_sync_drift, "scan_drift",
                    partial(
                        kg_sync_drift.scan_drift,
                        reachable_fn=lambda url: True,
                        query_hashes_fn=lambda url, coll, on_warn=None: {
                            rel: own_sig,
                            "knowledge/concepts/gone.md": "deadbeef",
                        },
                    ),
                ):
                    findings = _doctor.probe_kg_drift_census(
                        Path(td), _doctor.DoctorResolvers(),
                        {"scope": _doctor.SCOPE_FULL},
                    )
            self.assertEqual(findings[0].status, doctor.STATUS_PROBLEM)
            self.assertEqual(findings[0].detail["projects"][0]["stored_only"], 1)
            self.assertIn("stored-only", findings[0].summary)

    def test_no_binding_sourced_projects_no_row(self):
        """Nothing registered → the census says nothing at all (the binding
        probe owns the unbound/mismatch questions)."""
        import tempfile

        from vco_lib import doctor as _doctor
        from vco_lib.project_identity import IdentitySnapshot

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                kg_binding_doctor_project_identity(), "resolve_snapshot",
                return_value=IdentitySnapshot(resolvable=True, projects=()),
            ):
                self.assertEqual(
                    _doctor.probe_kg_drift_census(
                        Path(td), _doctor.DoctorResolvers(), {},
                    ),
                    [],
                )

    def test_scan_exception_is_a_warning_not_a_crash(self):
        import tempfile

        from vco_lib import doctor as _doctor
        from vco_lib import kg_sync_drift

        with tempfile.TemporaryDirectory() as td:
            _ident, folder, snapshot = self._identity(tmp=Path(td))
            self._knowledge_node(folder)
            with mock.patch.object(
                kg_binding_doctor_project_identity(), "resolve_snapshot",
                return_value=snapshot,
            ):
                with mock.patch.object(
                    kg_sync_drift, "scan_drift",
                    side_effect=RuntimeError("boom"),
                ):
                    findings = _doctor.probe_kg_drift_census(
                        Path(td), _doctor.DoctorResolvers(), {},
                    )
            self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
            self.assertIn("boom", findings[0].summary)

    def test_full_only_not_a_boot_probe(self):
        """Network reads per project collection: full scope only, the same
        cost/promises reasoning as kg_binding_evidence."""
        self.assertIn("kg_drift_census", doctor.PROBES)
        _fn, scopes = doctor.PROBES["kg_drift_census"]
        self.assertEqual(scopes, (doctor.SCOPE_FULL,))


def kg_binding_doctor_project_identity():
    """The module object whose ``resolve_snapshot`` the census consults."""
    import vco_lib.project_identity as pi

    return pi
