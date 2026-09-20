# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 F3 — VCO never advises binding a class it refuses to write.

The field shape, on the maintainer's own install. Two D18 entries were live:

* ``kg_binding_evidence_mismatch`` — "this project's data lives in a class the
  binding does not name", whose remedy says *"PICK, as the primary KG
  collection … the evidence line's UNBOUND class"*.
* ``kg_binding_ambiguous_evidence`` — "more than one class holds this
  project's objects, pick one", which prints a copy-paste
  ``UPDATE project_kg_bindings SET collection_name = '<class>' …`` line per
  candidate.

The unbound class both were pointing at was ``Alpha_KnowledgeGraph``: 70
knowledge nodes whose ``file_path`` values all resolve under the project
folder, so it clears the ownership bar comfortably — beside the project's real
791-object collection. ``Alpha`` is not a project. It is one of VCO's own test
fixture names, and since v0.2.94 ``vco_lib/fixture_class_guard.py`` REFUSES
writes to a fixture-stemmed class — a guard built for this very incident.

So the product was advising the user toward a target the product itself
blocks: follow the remedy and the project's reads and writes move onto the
residue half, where every write is then refused. And because
``scan_kg_binding_evidence`` computed ``fixture_shaped`` only for classes NOT
already in ``owners``/``claimed``/``unclaimed``, an evidence-bearing fixture
class — the one case where the label decides something — was never labelled.

The rule under test: a fixture-stemmed class that NO binding row names is not
a binding candidate at all. It is not hidden either — with no verdict claiming
it, it lands in ``unclaimed``, where the reading already labels it
fixture-shaped and prints the LOOK-only instructions written for exactly that
case. One rule in one place, so the mismatch finding, the ambiguity ask and
the automated heal cannot disagree.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_v0292_unclaimed_kg_classes import FakeMachine  # noqa: E402

from vco_lib import doctor  # noqa: E402
from vco_lib import kg_binding_heal as heal  # noqa: E402
from vco_lib.fixture_class_guard import fixture_stem_of  # noqa: E402

#: The live case, verbatim. Guarded below against the table changing.
FIXTURE_CLASS = "Alpha_KnowledgeGraph"
REAL_CLASS = "RealName_KnowledgeGraph"


def _split_machine(tmp: Path, *, fixture_count=70, real_count=791):
    """A project whose files are anchored in BOTH its real collection and a
    fixture-named residue class — the maintainer's machine, in miniature."""
    m = FakeMachine(tmp)
    files = [f"knowledge/concepts/n{i}.md" for i in range(6)]
    m.add_project("p1", "RealProj", kg_primary=REAL_CLASS, files=files)
    m.add_class(REAL_CLASS, paths=files, count=real_count)
    m.add_class(FIXTURE_CLASS, paths=files, count=fixture_count)
    return m


class ThePremiseHolds(unittest.TestCase):
    def test_the_class_in_question_really_is_fixture_stemmed(self):
        """If the table ever drops ``Alpha``, these tests stop meaning what
        they say — fail loudly here rather than passing vacuously."""
        self.assertIsNotNone(fixture_stem_of(FIXTURE_CLASS))
        self.assertIsNone(fixture_stem_of(REAL_CLASS))


class FixtureShapedClassIsNeverABindingTarget(unittest.TestCase):
    def test_it_is_not_counted_as_the_projects_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            scan = _split_machine(Path(td)).scan()
            names = [e.name for v in scan.verdicts for e in v.evidence]
            self.assertEqual(names, [REAL_CLASS])

    def test_the_mismatch_entry_can_now_clear_itself(self):
        """With the residue no longer counted, the binding matches the only
        class holding the data — so ``kg_binding_evidence_mismatch`` has
        nothing to report and stops being a permanent row."""
        with tempfile.TemporaryDirectory() as td:
            scan = _split_machine(Path(td)).scan()
            self.assertEqual(scan.mismatches, ())

    def test_the_ambiguity_ask_can_now_clear_itself(self):
        """One candidate is not a split. RED before the fix: the 70-object
        residue was a rival to the 791-object real collection, neither led by
        the decisive margin, and the heal refused as AMBIGUOUS — forever."""
        with tempfile.TemporaryDirectory() as td:
            plan = heal.plan_evidence_repoints(_split_machine(Path(td)).scan())
            self.assertEqual(plan.refusals, ())
            self.assertEqual(plan.repoints, ())

    def test_no_surface_prints_it_as_something_to_bind(self):
        """The entries and the CLI line, end to end: the class must not appear
        anywhere a reader could take it for a target — and never with SQL."""
        with tempfile.TemporaryDirectory() as td:
            scan = _split_machine(Path(td)).scan()
            entries = []

            class _Report:
                def add_entry(self, entry):
                    entries.append(entry)

            from vco_lib.deferral_report import DeferralEntry

            heal.emit_ambiguous_evidence_entry(
                _Report(),
                plan=heal.plan_evidence_repoints(scan),
                deferral_entry_cls=DeferralEntry,
            )
            self.assertEqual(entries, [])
            findings = doctor.probe_kg_binding_evidence(
                REPO_ROOT, doctor.DoctorResolvers(kg_binding_evidence=lambda: scan),
                {},
            )
            for f in findings:
                blob = f"{f.summary}\n{f.command or ''}"
                if FIXTURE_CLASS in blob:
                    self.assertNotIn("UPDATE project_kg_bindings", blob)
                    self.assertIn("fixture", blob.lower())

    def test_it_is_still_REPORTED_as_residue_not_swallowed(self):
        """The data is 70 real nodes. Dropping it from the binding analysis
        must not drop it from the machine's report — it moves to the reading
        written for it, which names it, labels it, and proposes nothing
        destructive."""
        with tempfile.TemporaryDirectory() as td:
            scan = _split_machine(Path(td)).scan()
            self.assertEqual(
                [(u.name, u.count) for u in scan.unclaimed],
                [(FIXTURE_CLASS, 70)],
            )
            findings = doctor.probe_kg_binding_evidence(
                REPO_ROOT, doctor.DoctorResolvers(kg_binding_evidence=lambda: scan),
                {},
            )
            unclaimed = next(
                f for f in findings if f.condition_id == doctor.CID_KG_UNCLAIMED
            )
            self.assertIn(FIXTURE_CLASS, unclaimed.summary)
            self.assertIn("fixture", unclaimed.summary.lower())
            self.assertNotIn("UPDATE project_kg_bindings", unclaimed.command)


class TheRuleIsNarrow(unittest.TestCase):
    """An OWNED class is owned, whatever its stem looks like."""

    def test_a_bound_fixture_class_stays_its_projects_evidence(self):
        """Removing it would report that project as having its data nowhere —
        a worse lie than the one this fixes. (It cannot arise on a healthy
        install: the add-time guard refuses a fixture-named class. The doctor
        still reports what it SEES.)"""
        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/b{i}.md" for i in range(4)]
            m.add_project("p1", "Bound", kg_primary=FIXTURE_CLASS, files=files)
            m.add_class(FIXTURE_CLASS, paths=files, count=9)
            scan = m.scan()
            names = [e.name for v in scan.verdicts for e in v.evidence]
            self.assertEqual(names, [FIXTURE_CLASS])
            self.assertEqual(scan.unclaimed, ())

    def test_a_class_named_by_ANOTHER_projects_binding_still_counts(self):
        """``owners`` is role-unfiltered across every project by contract, so
        a fixture class some other row names is accounted for and the narrow
        exclusion must not touch it."""
        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/s{i}.md" for i in range(4)]
            m.add_project("p1", "Mine", kg_primary=REAL_CLASS, files=files)
            m.add_project("p2", "Theirs", kg_primary=FIXTURE_CLASS)
            m.add_class(REAL_CLASS, paths=files[:2], count=2)
            m.add_class(FIXTURE_CLASS, paths=files, count=4)
            scan = m.scan()
            p1 = next(v for v in scan.verdicts if v.project_name == "Mine")
            self.assertIn(FIXTURE_CLASS, [e.name for e in p1.evidence])

    def test_an_ordinary_unbound_ghost_is_untouched(self):
        """The D18 heal must keep firing for the case it exists for."""
        with tempfile.TemporaryDirectory() as td:
            m = FakeMachine(Path(td))
            files = [f"knowledge/concepts/g{i}.md" for i in range(4)]
            m.add_project("p1", "Acme", kg_primary="Old_KnowledgeGraph",
                          files=files)
            m.add_class("Old_KnowledgeGraph",
                        paths=["knowledge/other/z.md"], count=1)
            m.add_class("Stray_KnowledgeGraph", paths=files, count=4)
            plan = heal.plan_evidence_repoints(m.scan())
            self.assertEqual(
                [(r.old_name, r.new_name) for r in plan.repoints],
                [("Old_KnowledgeGraph", "Stray_KnowledgeGraph")],
            )


class NoShippedDeferralTextOffersAFixtureName(unittest.TestCase):
    """The second half of F3's ask: nothing else suggests one.

    The shipped remedy texts are rendered from real scans above; what remains
    is the STATIC vocabulary — no condition's prose may hardcode a
    fixture-stemmed class as something to bind or create.
    """

    def test_the_registry_notes_name_no_fixture_class(self):
        """Every condition's shipped prose, in one file. A future author who
        pastes a real machine's ``Alpha_KnowledgeGraph`` into a remedy note is
        writing advice VCO's own guard refuses — catch it here, where the
        rendered-scan tests above cannot (they only see what a scan produces).
        """
        blob = (
            REPO_ROOT / "vco_lib" / "deferral_conditions.toml"
        ).read_text(encoding="utf-8")
        offenders = sorted({
            token
            for token in _class_like_tokens(blob)
            if fixture_stem_of(token) is not None
        })
        self.assertEqual(offenders, [], f"registry prose names {offenders}")


def _class_like_tokens(blob: str) -> list:
    """Every ``<Stem>_<Family>`` token in ``blob`` that could name a class."""
    import re

    from vco_lib.fixture_class_guard import COLLECTION_FAMILY_SUFFIXES

    families = "|".join(re.escape(s) for s in sorted(COLLECTION_FAMILY_SUFFIXES))
    return re.findall(rf"\b([A-Za-z][A-Za-z0-9]*_(?:{families}))\b", blob)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
