# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W18 — the code-graph prefix GENERATION RECORD and its evidence gate.

MEASURED, not hypothesised: three of five live
``.claude/state/codegraph-prefix-generation.json`` files named a prefix matching
ZERO live Weaviate classes — two written by the pre-v0.2.92 folder-basename
derivation, one by a KG-rule/code-rule divergence in the writer. The drift guard
compared those records as though they described a real collection generation,
and the deferral it emitted told the user their old classes "are now ORPHANED"
and printed a reclaim command aimed at a collection set that does not exist.

The fix has two halves and BOTH are tested on BOTH branches:

1. ``vco_lib.codegraph_prefix_record`` — the record's own module (extracted from
   the 16k-line ``project_init``), whose ``classify_generation_evidence`` is a
   PURE function, so every branch is testable without a server.
2. the wiring in ``project_init.detect_codegraph_prefix_drift`` — the live-class
   probe that supplies the evidence, and the three-way outcome:
   heal / report-with-a-command / report-without-one.

THREE-WAY BINDING BRANCH (the brief's PRESENT / ABSENT / UNKNOWN). The binding
probe is ``config_projection.probe_codegraph_binding_prefix``, consumed upstream
by ``project_identity``: PRESENT ⇒ an authoritative prefix reaches the guard;
ABSENT ⇒ a derived one does and may not overwrite the record; UNKNOWN ⇒ the
identity snapshot is unresolvable and the guard is not run at all. All three are
asserted here end-to-end.
"""

from __future__ import annotations

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

from tests.common.launcher_db_fixture import connect, seed_launcher_db  # noqa: E402
from vco_lib import codegraph_prefix_record as cpr  # noqa: E402
from vco_lib import project_identity, project_init  # noqa: E402

URL = "http://localhost:8081"
REG_NAME = "ACME_widget"
FOLDER_BASENAME = "widget"
KG_PRIMARY = "ACMEWidget_KnowledgeGraph"
CODE_PREFIX = "ACME_widget"


def _probe_for(*live_classes):
    """A ``probe_classes_exist``-shaped stub over a fixed live class set."""
    live = {c.lower() for c in live_classes}

    def _probe(names, weaviate_url=None):
        return {n: (n.lower() in live) for n in names}
    return _probe


def _probe_unreachable(names, weaviate_url=None):
    return {n: None for n in names}


# ═══════════════════════════════════════════════════════════════════════════
# The extracted module: format, reads, write
# ═══════════════════════════════════════════════════════════════════════════

class RecordModuleTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        self.assertTrue(cpr.write(self.folder, "Foo", source=cpr.SOURCE_BINDING))
        self.assertEqual(cpr.read_prefix(self.folder), "Foo")
        self.assertEqual(cpr.read_source(self.folder), cpr.SOURCE_BINDING)

    def test_missing_file_reads_as_none_not_empty_string(self):
        self.assertIsNone(cpr.read_prefix(self.folder))
        self.assertIsNone(cpr.read_source(self.folder))

    def test_pre_v0292_record_has_unknown_provenance(self):
        p = cpr.record_path(self.folder)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"schema": cpr.RECORD_SCHEMA,
                                 "collection_prefix": "Old"}), encoding="utf-8")
        self.assertEqual(cpr.read_prefix(self.folder), "Old")
        self.assertIsNone(cpr.read_source(self.folder),
                          "a record with no `source` is UNKNOWN, never 'derived'")

    def test_malformed_file_soft_fails(self):
        p = cpr.record_path(self.folder)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[]not json", encoding="utf-8")
        self.assertIsNone(cpr.read_prefix(self.folder))
        self.assertIsNone(cpr.read_source(self.folder))

    def test_project_init_aliases_point_at_this_module(self):
        """The extraction left thin aliases; four test modules and the bundle
        flow reach for the historical names."""
        self.assertIs(project_init._read_codegraph_prefix_generation,
                      cpr.read_prefix)
        self.assertIs(project_init._write_codegraph_prefix_generation, cpr.write)
        self.assertIs(project_init._read_codegraph_prefix_generation_source,
                      cpr.read_source)
        self.assertIs(project_init._codegraph_prefix_gen_path, cpr.record_path)
        self.assertIs(project_init._recorded_prefix_is_basename_poison,
                      cpr.recorded_is_basename_derivation)
        self.assertEqual(project_init._CODEGRAPH_PREFIX_GEN_FILENAME,
                         cpr.RECORD_FILENAME)

    def test_provenance_vocabulary_matches_project_identity(self):
        """Two modules, one vocabulary — pinned so they cannot drift."""
        self.assertEqual(cpr.SOURCE_BINDING, project_identity.SOURCE_BINDING)
        self.assertEqual(cpr.SOURCE_DERIVED, project_identity.SOURCE_DERIVED)


# ═══════════════════════════════════════════════════════════════════════════
# The PURE decision — every branch, no server
# ═══════════════════════════════════════════════════════════════════════════

class EvidenceClassificationTests(unittest.TestCase):

    def _c(self, recorded_live, current_live, authoritative=True):
        return cpr.classify_generation_evidence(
            recorded_live=recorded_live, current_live=current_live,
            current_is_authoritative=authoritative)

    def test_recorded_prefix_has_live_classes_is_a_real_drift(self):
        self.assertEqual(self._c(True, True), cpr.EVIDENCE_REPORT_OLD_CLASSES_LIVE)
        self.assertEqual(self._c(True, False), cpr.EVIDENCE_REPORT_OLD_CLASSES_LIVE)
        self.assertEqual(self._c(True, None), cpr.EVIDENCE_REPORT_OLD_CLASSES_LIVE)

    def test_dead_record_plus_authoritative_live_current_heals(self):
        self.assertEqual(self._c(False, True), cpr.EVIDENCE_HEAL_TO_CURRENT)

    def test_dead_record_without_authority_is_never_healed(self):
        """Re-deriving the record to a NAME SANITIZATION is exactly the
        poisoning v0.2.92 F-3 removed."""
        self.assertEqual(self._c(False, True, authoritative=False),
                         cpr.EVIDENCE_REPORT_OLD_CLASSES_ABSENT)

    def test_neither_prefix_live_reports_without_claiming_orphans(self):
        self.assertEqual(self._c(False, False),
                         cpr.EVIDENCE_REPORT_OLD_CLASSES_ABSENT)
        self.assertEqual(self._c(False, None),
                         cpr.EVIDENCE_REPORT_OLD_CLASSES_ABSENT)

    def test_unknown_recorded_evidence_falls_back_to_the_historic_path(self):
        for cur in (True, False, None):
            for auth in (True, False):
                with self.subTest(current_live=cur, authoritative=auth):
                    self.assertEqual(self._c(None, cur, auth),
                                     cpr.EVIDENCE_REPORT_UNVERIFIED)

    def test_could_not_check_is_never_read_as_false(self):
        """The single property the whole tri-state exists for."""
        self.assertNotEqual(self._c(None, True), self._c(False, True))


# ═══════════════════════════════════════════════════════════════════════════
# The prefix-level live-class probe
# ═══════════════════════════════════════════════════════════════════════════

class PrefixLiveProbeTests(unittest.TestCase):

    def test_present(self):
        self.assertIs(
            project_init.probe_prefix_has_live_code_classes(
                "Foo", URL, probe=_probe_for("Foo_CodeModule")),
            True)

    def test_absent(self):
        self.assertIs(
            project_init.probe_prefix_has_live_code_classes(
                "Foo", URL, probe=_probe_for("Bar_CodeModule")),
            False)

    def test_unreachable_is_none_not_false(self):
        self.assertIsNone(
            project_init.probe_prefix_has_live_code_classes(
                "Foo", URL, probe=_probe_unreachable))

    def test_empty_prefix_is_none(self):
        self.assertIsNone(
            project_init.probe_prefix_has_live_code_classes("", URL))
        self.assertIsNone(
            project_init.probe_prefix_has_live_code_classes("   ", URL))

    def test_a_raising_probe_is_none(self):
        def boom(names, weaviate_url=None):
            raise RuntimeError("injected")
        self.assertIsNone(
            project_init.probe_prefix_has_live_code_classes("Foo", URL, probe=boom))

    def test_partial_answers_are_unknown(self):
        """Some names answered, others not: not enough to say 'absent'."""
        def _partial(names, weaviate_url=None):
            return {n: (None if n.endswith("_CodeModule") else False)
                    for n in names}
        self.assertIsNone(
            project_init.probe_prefix_has_live_code_classes(
                "Foo", URL, probe=_partial))


# ═══════════════════════════════════════════════════════════════════════════
# The wiring — heal vs report, both branches
# ═══════════════════════════════════════════════════════════════════════════

class DriftEvidenceGateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _deferral_text(self) -> str:
        md = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return md.read_text(encoding="utf-8") if md.exists() else ""

    def _drift(self, recorded, current, probe, *, weaviate_url=URL,
               authoritative=True):
        cpr.write(self.folder, recorded, source=cpr.SOURCE_BINDING)
        return project_init.detect_codegraph_prefix_drift(
            self.folder, REG_NAME, emit_deferral=True,
            weaviate_url=weaviate_url,
            code_prefix=current if authoritative else None,
            live_class_probe=probe,
        )

    # ── HEAL: the record named nothing; the binding names live classes ────

    def test_heal_a_record_that_names_no_live_class(self):
        drift = self._drift("Dead_prefix", CODE_PREFIX,
                            _probe_for(f"{CODE_PREFIX}_CodeFunction"))
        self.assertIsNone(drift, "a record naming nothing is not a generation")
        self.assertEqual(cpr.read_prefix(self.folder), CODE_PREFIX)
        self.assertEqual(cpr.read_source(self.folder), cpr.SOURCE_BINDING)
        self.assertNotIn("codegraph_prefix_drift_detected", self._deferral_text())

    def test_heal_leaves_an_audit_row_rather_than_being_silent(self):
        self._drift("Dead_prefix", CODE_PREFIX,
                    _probe_for(f"{CODE_PREFIX}_CodeFunction"))
        jsonl = self.folder / ".claude" / "logs" / "auto-resolutions.jsonl"
        self.assertTrue(jsonl.is_file())
        self.assertIn("corrected_prefix_record_naming_no_live_class",
                      jsonl.read_text(encoding="utf-8"))

    # ── ACT: a genuine generation change is STILL reported, with the
    #        reclaim command, because the old classes provably exist ───────

    def test_a_real_orphaned_generation_still_reports_with_a_command(self):
        drift = self._drift("Legacy_prefix", CODE_PREFIX,
                            _probe_for("Legacy_prefix_CodeFunction",
                                       f"{CODE_PREFIX}_CodeFunction"))
        self.assertEqual(drift, {"old_prefix": "Legacy_prefix",
                                 "new_prefix": CODE_PREFIX})
        md = self._deferral_text()
        self.assertIn("codegraph_prefix_drift_detected", md)
        self.assertIn("ORPHANED", md)
        self.assertIn("detect-orphan-code-collections", md)

    # ── LEAVE ALONE: neither prefix names anything — report, but the text
    #    must not claim orphans and must not print a reclaim command ───────

    def test_neither_prefix_live_reports_without_a_reclaim_command(self):
        drift = self._drift("Legacy_prefix", CODE_PREFIX,
                            _probe_for("Unrelated_CodeFunction"))
        self.assertEqual(drift, {"old_prefix": "Legacy_prefix",
                                 "new_prefix": CODE_PREFIX})
        md = self._deferral_text()
        self.assertIn("codegraph_prefix_drift_detected", md)
        self.assertIn("NOTHING is orphaned", md)
        self.assertNotIn("detect-orphan-code-collections", md,
                         "no destructive follow-up may be printed for a "
                         "collection set proven not to exist")

    # ── LEAVE ALONE: could not check — historic behaviour, honest text ────

    def test_unreachable_weaviate_keeps_the_historic_report_and_says_so(self):
        drift = self._drift("Legacy_prefix", CODE_PREFIX, _probe_unreachable)
        self.assertEqual(drift["old_prefix"], "Legacy_prefix")
        md = self._deferral_text()
        self.assertIn("UNKNOWN", md)
        self.assertIn("READ-ONLY", md)
        self.assertNotIn("are now ORPHANED", md)

    def test_no_weaviate_url_does_not_probe_at_all(self):
        """Hermeticity AND honesty: evidence requires a NAMED server. Without
        one there is no probe, no network call, and no new branch."""
        calls = []

        def _spy(names, weaviate_url=None):
            calls.append(names)
            return {n: False for n in names}

        cpr.write(self.folder, "Legacy_prefix", source=cpr.SOURCE_BINDING)
        drift = project_init.detect_codegraph_prefix_drift(
            self.folder, REG_NAME, emit_deferral=True,
            code_prefix=CODE_PREFIX, live_class_probe=_spy,
        )
        self.assertEqual(calls, [], "no URL ⇒ no probe")
        self.assertEqual(drift["old_prefix"], "Legacy_prefix")

    def test_a_raising_probe_falls_back_to_the_historic_path(self):
        def boom(names, weaviate_url=None):
            raise RuntimeError("injected")
        drift = self._drift("Legacy_prefix", CODE_PREFIX, boom)
        self.assertEqual(drift["old_prefix"], "Legacy_prefix")
        self.assertIn("codegraph_prefix_drift_detected", self._deferral_text())

    # ── the pre-existing guards are not weakened by the new branch ────────

    def test_same_generation_is_still_not_drift(self):
        drift = self._drift(CODE_PREFIX, CODE_PREFIX,
                            _probe_for(f"{CODE_PREFIX}_CodeFunction"))
        self.assertIsNone(drift)

    def test_a_derived_run_still_cannot_overwrite_a_binding_record(self):
        """F-3 stays in force: the evidence gate sits AFTER it, so a derived
        run never reaches the heal even with a live current prefix."""
        cpr.write(self.folder, "Good_record", source=cpr.SOURCE_BINDING)
        drift = project_init.detect_codegraph_prefix_drift(
            self.folder, FOLDER_BASENAME, emit_deferral=True,
            weaviate_url=URL, code_prefix=None,
            live_class_probe=_probe_for("Widget_CodeFunction"),
        )
        self.assertIsNone(drift)
        self.assertEqual(cpr.read_prefix(self.folder), "Good_record")
        self.assertNotIn("codegraph_prefix_drift_detected", self._deferral_text())


# ═══════════════════════════════════════════════════════════════════════════
# The THREE-WAY BINDING BRANCH, end to end through the identity resolver
# ═══════════════════════════════════════════════════════════════════════════

class BindingProbeThreeWayTests(unittest.TestCase):
    """PRESENT / ABSENT / UNKNOWN — from `probe_codegraph_binding_prefix` all
    the way to what the generation record does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-wp4-"))
        self.proj = self.tmp / FOLDER_BASENAME
        self.proj.mkdir()
        self.db = self.tmp / "launcher.db"

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _pin(self):
        return mock.patch.dict(os.environ,
                               {"VCT_LAUNCHER_DB_PATH": str(self.db)})

    # ── DELIBERATELY degraded schemas (each derived from the REAL one) ───

    def _rename_codegraph_prefix_column(self):
        """Half-migrated / foreign shape: ``project_codegraph_bindings``
        EXISTS but its ``collection_prefix`` column does not, so production's
        SELECT raises "no such column" — a READ FAILURE, structurally
        different from the "no such table" ABSENCE modelled below.

        Produced by one ALTER against the real migrated table rather than by
        re-declaring the table by hand, so every other column keeps its true
        shape and only the one axis under test is degraded.
        """
        conn = connect(self.db)
        try:
            conn.execute(
                "ALTER TABLE project_codegraph_bindings "
                "RENAME COLUMN collection_prefix TO prefix_but_wrong_name"
            )
            conn.commit()
        finally:
            conn.close()

    def _drop_codegraph_bindings_table(self):
        """Pre-migration shape: no ``project_codegraph_bindings`` table at
        all, so the SELECT raises "no such table" — structural ABSENCE, which
        the code must treat as leave-alone rather than as unknown. Dropped
        from the real migrated DB, so every other table is intact.
        """
        conn = connect(self.db)
        try:
            conn.execute("DROP TABLE project_codegraph_bindings")
            conn.commit()
        finally:
            conn.close()

    def test_present_yields_an_authoritative_prefix(self):
        seed_launcher_db(self.db, [{
            "name": REG_NAME, "folder_path": str(self.proj),
            "kg_primary": KG_PRIMARY, "codegraph_prefix": CODE_PREFIX,
        }])
        with self._pin():
            identity, snap = project_identity.resolve_identity(self.proj)
        self.assertTrue(snap.resolvable)
        assert identity is not None
        self.assertEqual(identity.authoritative_codegraph_prefix(), CODE_PREFIX)

    def test_absent_binding_yields_a_derived_prefix_and_no_authority(self):
        """No `project_codegraph_bindings` row: a SUCCESSFUL read that found
        nothing. The project is still resolvable; the prefix is a marked guess.
        """
        seed_launcher_db(self.db, [{
            "name": REG_NAME, "folder_path": str(self.proj),
            "kg_primary": KG_PRIMARY, "codegraph_prefix": None,
        }])
        with self._pin():
            identity, snap = project_identity.resolve_identity(self.proj)
        self.assertTrue(snap.resolvable)
        assert identity is not None
        self.assertIsNone(identity.authoritative_codegraph_prefix())
        self.assertEqual(identity.codegraph_prefix_source,
                         project_identity.SOURCE_DERIVED)

    def test_unknown_binding_makes_the_whole_snapshot_unresolvable(self):
        """The half that used to be swallowed: a DB we could not READ is not an
        absent binding, and must not produce a populated identity.

        Driven through the REAL sqlite error rather than a mock: the
        ``project_codegraph_bindings`` table exists with the wrong columns (a
        half-migrated / foreign schema), so the SELECT raises "no such column",
        which is NOT the "no such table" structural-absence shape.
        """
        seed_launcher_db(self.db, [{
            "project_id": "p1", "name": REG_NAME,
            "folder_path": str(self.proj), "slug": "acme-widget",
            "kg_primary": KG_PRIMARY,
        }])
        self._rename_codegraph_prefix_column()
        with self._pin():
            identity, snap = project_identity.resolve_identity(self.proj)
        self.assertFalse(snap.resolvable,
                         "could-not-read must not report a resolvable identity")
        self.assertIsNone(identity)

    def test_the_raise_reaches_the_bundle_wiring_as_a_skip(self):
        """Consequence of the fix, stated end-to-end: with the binding
        unreadable, `authoritative_identity` is None, so the drift guard is not
        run and NO baseline is stamped. That is the UNKNOWN arm of the
        three-way branch — 'touch nothing' — realised by composition rather
        than by a third code path inside the guard."""
        # No KG binding row here — the projects row alone.
        seed_launcher_db(self.db, [{
            "project_id": "p1", "name": REG_NAME,
            "folder_path": str(self.proj), "slug": "acme-widget",
        }])
        self._rename_codegraph_prefix_column()
        with self._pin():
            snap = project_identity.resolve_snapshot()
        self.assertFalse(snap.resolvable)
        self.assertEqual(snap.projects, ())
        # Nothing was stamped into the project's state dir.
        self.assertIsNone(cpr.read_prefix(self.proj))

    def test_a_missing_codegraph_table_is_absence_not_unknown(self):
        """Leave-alone side of the same guard: a pre-migration DB is READABLE,
        so the snapshot stays resolvable and the derived placeholder applies."""
        seed_launcher_db(self.db, [{
            "project_id": "p1", "name": REG_NAME,
            "folder_path": str(self.proj), "slug": "acme-widget",
            "kg_primary": KG_PRIMARY,
        }])
        self._drop_codegraph_bindings_table()
        with self._pin():
            identity, snap = project_identity.resolve_identity(self.proj)
        self.assertTrue(snap.resolvable)
        assert identity is not None
        self.assertIsNone(identity.authoritative_codegraph_prefix())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
