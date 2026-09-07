# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D18 HEAL — the evidence-backed primary-KG repoint (v0.2.92).

The gap this closes: ``kg_binding_heal._prefix_adopt_kg_bindings_pass``
rebinds a primary binding whose class is ABSENT from Weaviate, and skips at
its first guard every row whose class EXISTS. A ghost that already received
the project's writes EXISTS, so the recorded field shape was diagnosed
(``kg_binding_evidence_mismatch``) and left to a human.

Every test here drives a PRODUCTION entry point — either
``install._self_heal_kg_bindings_on_update`` (the ``--update`` step) or the
doctor probe — against a REAL shipped-schema ``launcher.db``, REAL project
folders holding REAL files, and a REAL HTTP server speaking Weaviate's
``/v1/schema`` and ``/v1/graphql``. Nothing about the decision is stubbed:
the ownership evidence is computed by ``kg_binding_doctor`` from files that
actually exist on disk.

THE SAFETY ARGUMENT, and how it is pinned
-----------------------------------------
R38 records that the previous attempt to "fix D18" rewrote state from the
NAME-DERIVED class, and that on the likeliest field machine — where the
binding itself is the stale thing — it would have re-stamped the ghost every
run while reporting success. The eligibility input here is file-backed
positive evidence instead, and two test classes pin that it is real, not a slogan:

* ``test_repoints_to_the_evidence_class_not_the_name_derived_one`` puts the
  name-derived class in Weaviate, populated, with files that are NOT the
  project's — the R38 trap — and asserts the repoint goes to the evidence
  class;
* ``EvidenceRuleIsNotForkedTests`` moves the DOCTOR's calibration constants
  and asserts the heal's decision moves with them, on one unchanged fixture.
  A forked copy of the bar in the heal module would not follow.

REFUSAL ARMS — each writes NOTHING and leaves the state diagnosed exactly as
it is today: ambiguous evidence (2+ classes clear the bar and none leads
decisively), below-bar evidence (too few matching paths / too low a fraction),
an unreachable Weaviate or unreadable launcher.db, a ``manual_override`` row,
and a target class named by another binding row (including an ORPHAN row whose
project was deleted — caught only by the write-time gate).

THE DECISIVE MARGIN (the v0.2.92 second wave)
---------------------------------------------
"Two or more classes cleared the bar" was originally a flat refusal, so the
commonest field shape — the dual-write divergence, where one class holds the
corpus and another holds a handful of nodes — could never be healed at all.
``DecisiveMarginTests`` pins the rule that replaced it: rank by the doctor's
own ``matched_paths`` and act only on a leader that beats the runner-up by
BOTH ``EVIDENCE_MARGIN_FACTOR`` and ``EVIDENCE_MARGIN_ABS`` (strict, so a
value exactly at either bar still refuses). Each boundary test is written so
that ONE leg is already satisfied and the refusal can therefore only come from
the other. ``AmbiguityIsAskedAboutTests`` pins the other half: what is still
refused is no longer SILENT — it raises ``kg_binding_ambiguous_evidence``,
from the read-only plan site, which is the only site an ambiguous machine
(which owes no write, and so never enters the RW pass) actually reaches.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_kg_binding,
    add_project,
    connect,
    create_empty_launcher_db,
    set_app_state,
)

import install  # noqa: E402
from vco_lib import kg_binding_doctor as kbd  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

REPOINT_CID = "kg_binding_evidence_repointed"
MISMATCH_CID = "kg_binding_evidence_mismatch"
AMBIGUOUS_CID = "kg_binding_ambiguous_evidence"


# ---------------------------------------------------------------------------
# A real HTTP Weaviate: /v1/schema + /v1/graphql (Aggregate count, Get sample)
# ---------------------------------------------------------------------------


class _StubWeaviate(http.server.BaseHTTPRequestHandler):
    #: ``{class_name: {"paths": [...], "count": int}}``
    classes: dict = {}
    #: When True every GraphQL POST answers 500 — "Weaviate is there but the
    #: probe could not look inside", the unreachable-arm shape.
    graphql_broken: bool = False

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path == "/v1/schema":
            self._send(200, {
                "classes": [{"class": n} for n in self.__class__.classes]
            })
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/graphql":
            self.send_response(404)
            self.end_headers()
            return
        if self.__class__.graphql_broken:
            self._send(500, {"error": "boom"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        query = json.loads(self.rfile.read(length) or b"{}").get("query", "")
        known = self.__class__.classes
        name = next((n for n in known if n in query), None)
        if name is None:
            self._send(200, {"data": {}})
            return
        if "Aggregate" in query:
            self._send(200, {"data": {"Aggregate": {
                name: [{"meta": {"count": known[name]["count"]}}]
            }}})
            return
        self._send(200, {"data": {"Get": {
            name: [{"file_path": p} for p in known[name]["paths"]]
        }}})

    def log_message(self, *args, **kwargs):
        pass


class Machine:
    """One fake machine: launcher.db + project folders + a live Weaviate."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db = tmp / "launcher.db"
        create_empty_launcher_db(self.db)
        conn = connect(self.db)
        try:
            # WAL mirrors the launcher's runtime pragma, so the RO detection
            # legs never block on the RW pass.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.commit()
        finally:
            conn.close()
        # A converged shared-KG pointer pair, so the pointer-drift leg is not
        # what makes the RW pass open — this suite must attribute every write
        # to the D18 pass alone.
        set_app_state(self.db, "orchestrator_root_kg_collection",
                      "VibeCodedOrchestrator_KnowledgeGraph")
        set_app_state(self.db, "last_installed_shared_kg_collection",
                      "VibeCodedOrchestrator_KnowledgeGraph")
        _StubWeaviate.classes = {}
        _StubWeaviate.graphql_broken = False
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _StubWeaviate)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    break
            except OSError:
                time.sleep(0.02)

    # -- seeding ----------------------------------------------------------
    def project(self, pid, name, *, primary, files=(), shared=None):
        folder = self.tmp / f"{pid}-folder"
        for rel in files:
            f = folder / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"# {rel}\n", encoding="utf-8")
        folder.mkdir(parents=True, exist_ok=True)
        add_project(self.db, project_id=pid, name=name, folder_path=folder,
                    kg_primary=primary, kg_shared=shared)
        return folder

    def klass(self, name, *, paths, count=None):
        _StubWeaviate.classes[name] = {
            "paths": list(paths),
            "count": len(paths) if count is None else count,
        }

    def set_config_json(self, pid, role, value: str):
        conn = connect(self.db)
        try:
            conn.execute(
                "UPDATE project_kg_bindings SET config_json = ? "
                "WHERE project_id = ? AND role = ?", (value, pid, role))
            conn.commit()
        finally:
            conn.close()

    # -- reading ----------------------------------------------------------
    def primary_of(self, pid) -> str:
        conn = connect(self.db)
        try:
            row = conn.execute(
                "SELECT collection_name FROM project_kg_bindings "
                "WHERE project_id = ? AND role = 'primary'", (pid,)).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()

    def config_of(self, pid, role="primary") -> dict:
        conn = connect(self.db)
        try:
            row = conn.execute(
                "SELECT config_json FROM project_kg_bindings "
                "WHERE project_id = ? AND role = ?", (pid, role)).fetchone()
        finally:
            conn.close()
        try:
            cfg = json.loads((row[0] if row else "") or "{}")
        except ValueError:
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def access_rows(self, pid) -> set:
        conn = connect(self.db)
        try:
            return {
                r[0] for r in conn.execute(
                    "SELECT collection_name FROM kg_collection_access "
                    "WHERE project_id = ?", (pid,)).fetchall()
            }
        finally:
            conn.close()

    def run_update(self) -> DeferralReport:
        """THE production entry point: the --update self-heal step."""
        report = DeferralReport()
        install._self_heal_kg_bindings_on_update(report)
        return report

    def doctor_findings(self):
        """THE other production surface: the registered doctor probe."""
        from vco_lib import doctor

        res = doctor.DoctorResolvers()
        probe = doctor.PROBES["kg_binding_evidence"][0]
        return probe(self.tmp, res, {})

    def close(self):
        self.server.shutdown()


class _Base(unittest.TestCase):
    def setUp(self):
        # System tmp, NOT the repo tree: tearDown rmtree's this, but an
        # INTERRUPTED run (killed lane, Ctrl-C'd suite) never reaches
        # tearDown — a repo-adjacent scratch dir then leaks into the
        # deliverable tree (v0.2.92 release day: four leaked _tmp_d18heal_*
        # dirs; agent `rm` was permission-denied). tempfile.mkdtemp is
        # unique per call, so no pid/id() bookkeeping either.
        self.tmp = Path(tempfile.mkdtemp(prefix="v0292_d18heal_"))
        self.m = Machine(self.tmp)
        self.env = mock.patch.dict(os.environ, {
            "VCT_STATE_DIR": str(self.tmp),
            "VCT_LAUNCHER_DB_PATH": str(self.m.db),
            "WEAVIATE_URL": f"http://127.0.0.1:{self.m.port}",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.m.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # A project whose data lives in an UNBOUND class while its binding names
    # a different, existing one. The canonical D18 shape.
    def seed_ghost_shape(self, *, bound="Old_KnowledgeGraph"):
        files = [f"knowledge/concepts/n{i}.md" for i in range(4)]
        self.m.project("p1", "Acme", primary=bound, files=files)
        # The bound class exists (that is what makes prefix-adopt skip it)
        # and holds somebody else's paths.
        self.m.klass(bound, paths=["knowledge/other/z.md"], count=1)
        # The ghost: every sampled path is a real file under p1's folder.
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=len(files))
        return files

    def cids(self, report):
        return [e.condition_id for e in report.entries]


# ---------------------------------------------------------------------------
# The heal fires
# ---------------------------------------------------------------------------


class HealFiresTests(_Base):
    def test_unambiguous_evidence_repoints_the_primary_binding(self):
        """MUTATION: neutralise `_evidence_repoint_pass` (return no applied
        rows) or drop the `evidence_plan=` argument from install.py's call —
        either way the binding stays `Old_KnowledgeGraph` and this goes red."""
        self.seed_ghost_shape()
        report = self.m.run_update()

        self.assertEqual(self.m.primary_of("p1"), "Ghost_KnowledgeGraph")
        self.assertIn(REPOINT_CID, self.cids(report))

    def test_the_repoint_is_visible_and_reversible(self):
        """A silent repoint would be worse than the defect: the ledger entry
        must name BOTH classes, and the row must record where it came from.

        MUTATION: drop `EVIDENCE_REPOINT_KEY` from the written config_json,
        or remove the old name from the entry text — red."""
        self.seed_ghost_shape()
        report = self.m.run_update()

        entry = next(e for e in report.entries
                     if e.condition_id == REPOINT_CID)
        blob = f"{entry.title}\n{entry.detected}\n{entry.why_deferred}\n" \
               f"{entry.command_to_apply}"
        self.assertIn("Old_KnowledgeGraph", blob)
        self.assertIn("Ghost_KnowledgeGraph", blob)
        self.assertIn("Identity", blob)  # the reversal route
        self.assertEqual(entry.severity, "info")

        audit = self.m.config_of("p1").get("evidence_repoint")
        assert isinstance(audit, dict), f"no repoint audit record: {audit!r}"
        self.assertEqual(audit["from"], "Old_KnowledgeGraph")
        self.assertEqual(audit["to"], "Ghost_KnowledgeGraph")
        # NOT laundered as a human pick — that sentinel is the thing every
        # automated pass must not forge.
        self.assertNotIn("manual_override", self.m.config_of("p1"))

    def test_access_row_follows_the_repoint(self):
        """The parity pass runs AFTER the repoint, so the newly-named class
        gets its write grant in the same transaction.

        MUTATION: move the D18 pass after pass 4 — red."""
        self.seed_ghost_shape()
        self.m.run_update()
        self.assertIn("Ghost_KnowledgeGraph", self.m.access_rows("p1"))

    def test_repoints_to_the_evidence_class_not_the_name_derived_one(self):
        """THE R38 PIN. `Acme_KnowledgeGraph` — the name-derived class — is
        present and populated, but with files that are not the project's.
        The evidence class is what gets bound.

        MUTATION: swap the plan's `new_name` for `verdict.expected` — red,
        which is exactly the fix R38 rejected."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(4)]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("Acme_KnowledgeGraph",
                     paths=["knowledge/nope/a.md", "knowledge/nope/b.md"],
                     count=9)
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=4)

        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Ghost_KnowledgeGraph")

    def test_the_previous_class_stays_in_the_drop_protection_keep_set(self):
        """A repoint must not turn live data into a drop candidate.

        After the binding moves off `Old_KnowledgeGraph`, no binding row names
        it — so without the repoint-audit contribution it would fall out of
        `IdentitySnapshot.kg_keep_tokens`, the PROTECTION list the legacy
        drop detector consults, and the automatic repair would have made a
        populated class droppable as a side effect.

        MUTATION: stop feeding `previously_bound_kg_collections` into
        `kg_keep_tokens` — red."""
        from vco_lib.project_identity import normalise_for_match, resolve_snapshot

        self.seed_ghost_shape()
        before = resolve_snapshot(db_path=self.m.db).kg_keep_tokens()
        self.assertIn(normalise_for_match("Old_KnowledgeGraph"), before)

        self.m.run_update()

        after = resolve_snapshot(db_path=self.m.db).kg_keep_tokens()
        self.assertIn(
            normalise_for_match("Old_KnowledgeGraph"), after,
            "the class the repoint moved OFF must stay drop-protected",
        )
        self.assertIn(normalise_for_match("Ghost_KnowledgeGraph"), after)

    def test_second_run_is_a_no_op(self):
        """Idempotent by construction: once re-pointed, the ONE class that
        clears the ownership bar IS the bound one, so the plan proposes
        nothing (the `found.name == verdict.bound` healthy short-circuit)
        and the gate cannot fire again.

        MUTATION: the eligibility input is `verdict.evidence` with bound
        classes COUNTED — that is the SHIPPED rule, not a mutation away
        from `unbound_evidence`. Switching it to `verdict.unbound_evidence`
        does NOT redden this test (a healed machine has zero unbound
        evidence, which is also a no-op); it reddens the ambiguity arm
        instead (`test_bound_class_also_holding_the_data_is_ambiguous`,
        H4), where a bound class that also holds the files would stop
        counting and an ambiguous shape would be healed. Dropping the
        healthy short-circuit alone also stays green here — the write-time
        target gate refuses the self-repoint — and is caught by the
        RW-lock test below."""
        self.seed_ghost_shape()
        self.m.run_update()
        first = self.m.config_of("p1")["evidence_repoint"]["at_ms"]

        report2 = self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Ghost_KnowledgeGraph")
        self.assertNotIn(REPOINT_CID, self.cids(report2))
        self.assertEqual(
            self.m.config_of("p1")["evidence_repoint"]["at_ms"], first,
            "a second run must not rewrite the row at all",
        )

    def test_a_healed_machine_never_reopens_launcher_db_read_write(self):
        """Bug-N discipline, extended. Once healed, the binding names the
        class holding the data, so the plan is EMPTY and install.py must not
        acquire the writer lock the hub holds in production.

        MUTATION: drop the `found.name == verdict.bound` short-circuit — the
        plan proposes a self-repoint, `has_work` turns true, an RW open
        happens on every update, red."""
        self.seed_ghost_shape()
        self.m.run_update()

        rw: list = []
        real = sqlite3.connect

        def tracking(*a, **kw):
            dsn = a[0] if a else kw.get("database", "")
            if not (kw.get("uri") and isinstance(dsn, str)
                    and "mode=ro" in dsn):
                rw.append(dsn)
            return real(*a, **kw)

        with mock.patch("sqlite3.connect", side_effect=tracking):
            self.m.run_update()
        self.assertEqual(
            [d for d in rw if str(self.m.db) in str(d)], [],
            "a healed machine must not open launcher.db read-write",
        )

    def test_doctor_stops_reporting_the_mismatch_after_the_heal(self):
        """The other production surface. Before: a PROBLEM finding carrying
        the D18 cid. After: the agreement reading, which is what clears the
        ledger entry."""
        self.seed_ghost_shape()
        before = self.m.doctor_findings()
        self.assertIn(
            MISMATCH_CID,
            [f.condition_id for f in before if f.status == "problem"],
        )

        self.m.run_update()

        after = self.m.doctor_findings()
        self.assertEqual(
            [], [f for f in after
                 if f.status == "problem" and f.condition_id == MISMATCH_CID],
            "the heal must clear the very diagnosis it answers",
        )
        self.assertTrue(any(f.status == "ok" and f.condition_id == MISMATCH_CID
                            for f in after))


# ---------------------------------------------------------------------------
# The heal refuses — one test per arm, each pinned by its own mutation
# ---------------------------------------------------------------------------


class HealRefusesTests(_Base):
    def assert_untouched(self, report, *, bound="Old_KnowledgeGraph"):
        self.assertEqual(self.m.primary_of("p1"), bound)
        self.assertNotIn(REPOINT_CID, self.cids(report))
        self.assertEqual(self.m.config_of("p1").get("evidence_repoint"), None)

    def test_two_evidence_classes_refuse(self):
        """Ambiguity is not a tie to break. Both candidates clear the bar;
        neither is chosen and the state stays diagnosed.

        MUTATION: pick `unbound[0]` when len > 1 — red."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(4)]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("GhostA_KnowledgeGraph", paths=files, count=4)
        self.m.klass("GhostB_KnowledgeGraph", paths=files, count=40)

        report = self.m.run_update()
        self.assert_untouched(report)
        # And the doctor still names it — "deferred exactly as today".
        self.assertIn(
            MISMATCH_CID,
            [f.condition_id for f in self.m.doctor_findings()
             if f.status == "problem"],
        )

    def test_one_matching_path_is_below_the_floor(self):
        """`MIN_MATCHED_PATHS = 2`: a ghost with a single write is below the
        evidence floor even at a 100% match.

        MUTATION: lower `MIN_MATCHED_PATHS` to 1 — red (see also
        EvidenceRuleIsNotForkedTests, which does exactly that on purpose)."""
        files = ["knowledge/concepts/only.md"]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=1)

        self.assert_untouched(self.m.run_update())

    def test_diluted_match_is_below_the_fraction_bar(self):
        """3 of 5 sampled paths exist (60%) — under the 80% ownership bar, the
        copied-notes shape the calibration excluded at 52%.

        MUTATION: lower `OWNERSHIP_MATCH_FRACTION` to 0.5 — red."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(3)]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("Ghost_KnowledgeGraph",
                     paths=[*files, "knowledge/gone/x.md",
                            "knowledge/gone/y.md"], count=5)

        self.assert_untouched(self.m.run_update())

    def test_bound_class_also_holding_the_data_is_ambiguous(self):
        """Both the BOUND class and an unbound ghost clear the ownership bar.
        The evidence cannot say which is the home, so nothing is written —
        the state is exactly the ambiguity the doctor keeps reporting.

        MUTATION: count only `verdict.unbound_evidence` when planning (i.e.
        ignore the bound class) — the ghost becomes the lone candidate, the
        binding moves off a class that holds its data, red."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(4)]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        # The bound class holds the project's files TOO — e.g. the project
        # wrote to the ghost for a while and was re-bound afterwards.
        self.m.klass("Old_KnowledgeGraph", paths=files, count=4)
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=9)

        self.assert_untouched(self.m.run_update())

    def test_manual_override_row_is_never_repointed(self):
        """A human's deliberate pick. Same promise the launcher's boot sweep
        makes, enforced at write time under the same cursor.

        MUTATION: drop the `config_has_manual_override` gate — red."""
        self.seed_ghost_shape()
        self.m.set_config_json(
            "p1", "primary", json.dumps({"manual_override": "user-2026-09"}))

        report = self.m.run_update()
        self.assert_untouched(report)
        self.assertEqual(
            self.m.config_of("p1")["manual_override"], "user-2026-09")

    def test_target_owned_by_another_registered_project_is_refused(self):
        """A class another registered project's binding names is never taken,
        however good the evidence looks — two projects can legitimately share
        relative paths, and one class with two readers is a worse state than
        the one being repaired.

        MUTATION: drop the `repoint.new_name in bound_anywhere` check — red
        (the same gate `..._orphan_binding_row_...` pins)."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(4)]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        self.m.project("p2", "Other", primary="Ghost_KnowledgeGraph")
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=4)

        report = self.m.run_update()
        self.assert_untouched(report)
        self.assertEqual(self.m.primary_of("p2"), "Ghost_KnowledgeGraph")

    def test_target_owned_by_an_orphan_binding_row_is_refused_at_write_time(
            self):
        """The gate the scan CANNOT provide. A binding row whose `projects`
        row is gone still OWNS its class, but the scan's owners map is built
        per registered project and cannot see it — so the plan proposes the
        repoint and the write-time re-check is the only thing that stops it.

        `Decoy_KnowledgeGraph` is present and genuinely unbound so the cheap
        precondition does NOT short-circuit — without it the scan never runs
        and this test would pass while the gate it names stayed inert (it
        did: the M6 mutation came back GREEN on the first draft).

        MUTATION: delete the `repoint.new_name in bound_anywhere` check in
        `_evidence_repoint_pass` — red."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(4)]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        add_kg_binding(self.m.db, "ghost-project-id", "primary",
                       "Ghost_KnowledgeGraph")
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=4)
        # Unbound, populated, and NOT this project's data — it exists only to
        # keep the precondition open so the scan actually runs.
        self.m.klass("Decoy_KnowledgeGraph",
                     paths=["knowledge/elsewhere/q.md"], count=1)

        self.assert_untouched(self.m.run_update())

    def test_prefix_adopt_wins_and_the_d18_pass_stands_down(self):
        """Pass composition. When the binding's class is ABSENT, the v0.2.40
        prefix-adopt pass owns the row — it runs first and adopts the single
        populated sibling. The D18 pass then re-reads the row under the same
        cursor, finds a name that is no longer the one its snapshot measured,
        and writes nothing: one repair, not two.

        MUTATION: remove BOTH write-time gates (`row_changed` and
        `target_bound`) from `_evidence_repoint_pass` — the D18 pass
        re-stamps the row and the `evidence_repoint` audit key appears, red."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(4)]
        self.m.project("p1", "Acme", primary="Missing_KnowledgeGraph",
                       files=files)
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=4)

        report = self.m.run_update()

        self.assertEqual(self.m.primary_of("p1"), "Ghost_KnowledgeGraph")
        cfg = self.m.config_of("p1")
        self.assertEqual(cfg.get("manual_override"), "v0.2.40-prefix-adopt")
        self.assertNotIn(
            "evidence_repoint", cfg,
            "the D18 pass must not re-write a row another pass just repaired",
        )
        self.assertNotIn(REPOINT_CID, self.cids(report))

    def test_row_changed_between_snapshot_and_write_is_refused(self):
        """The cross-PROCESS TOCTOU gate, exercised directly.

        The plan is computed read-only BEFORE the writer lock is taken; the
        launcher (a separate process) can rewrite a binding in that window.
        No single-process test can open that window through
        ``_self_heal_kg_bindings_on_update``, so this drives
        ``_evidence_repoint_pass`` with a plan whose ``old_name`` no longer
        matches the row — the state that window produces. The other two
        write-time gates cannot mask it here: no `manual_override`, and the
        target is named by no binding row.

        MUTATION: drop the `!= repoint.old_name` clause — red."""
        from vco_lib import kg_binding_heal as kbh

        self.m.project("p1", "Acme", primary="Actual_KnowledgeGraph")
        plan = kbh.EvidenceHealPlan(repoints=(kbh.EvidenceRepoint(
            project_id="p1", project_name="Acme", folder=str(self.tmp),
            old_name="Snapshotted_KnowledgeGraph",
            new_name="Ghost_KnowledgeGraph",
            object_count=4, matched_paths=4, sampled_paths=4,
        ),))
        conn = connect(self.m.db)
        try:
            applied, refusals = kbh._evidence_repoint_pass(
                conn.cursor(), plan=plan)
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(applied, [])
        self.assertEqual([r.reason for r in refusals],
                         [kbh.REFUSE_ROW_CHANGED])
        self.assertEqual(self.m.primary_of("p1"), "Actual_KnowledgeGraph")

    def test_unreachable_weaviate_changes_nothing(self):
        """Probe failure is not evidence. The class listing is served but
        every GraphQL read fails, so nothing can be counted or sampled.

        MUTATION: treat a `None` count/sample as zero, or a `None` scan as an
        empty scan — red (the row would move, or an entry would appear)."""
        self.seed_ghost_shape()
        _StubWeaviate.graphql_broken = True

        report = self.m.run_update()
        self.assert_untouched(report)
        self.assertNotIn(MISMATCH_CID, self.cids(report))

    def test_unreadable_launcher_db_changes_nothing(self):
        """The plan resolver returns None when launcher.db cannot be read; it
        must never fall through to a write or to an entry."""
        from vco_lib import kg_binding_heal as kbh

        plan = kbh.resolve_evidence_heal_plan(
            db_path=self.tmp / "does-not-exist.db",
            weaviate_url=f"http://127.0.0.1:{self.m.port}",
            existing_classes={"Ghost_KnowledgeGraph"},
        )
        self.assertIsNone(plan)

    def test_a_none_scan_yields_no_plan_at_all(self):
        """"Could not look" and "looked, nothing owed" are different answers.
        A scan that returns None (Weaviate unreachable / launcher.db
        unreadable) must produce NO plan — not an empty one.

        MUTATION: `return EvidenceHealPlan()` instead of `None` on a None
        scan — red."""
        from vco_lib import kg_binding_heal as kbh

        self.seed_ghost_shape()
        self.assertIsNone(
            kbh.resolve_evidence_heal_plan(
                db_path=self.m.db,
                weaviate_url=f"http://127.0.0.1:{self.m.port}",
                existing_classes={"Ghost_KnowledgeGraph"},
                scan_evidence=lambda **kw: None,
            )
        )

    def test_no_unbound_kg_class_short_circuits_before_any_weaviate_read(self):
        """The cheap precondition: with every KG class already bound, no
        unbound evidence can exist, so the scan is skipped entirely.

        MUTATION: drop the precondition — the scan runs and this goes red on
        the GraphQL call count."""
        from vco_lib import kg_binding_heal as kbh

        self.m.project("p1", "Acme", primary="Acme_KnowledgeGraph")
        calls: list = []
        plan = kbh.resolve_evidence_heal_plan(
            db_path=self.m.db,
            weaviate_url=f"http://127.0.0.1:{self.m.port}",
            existing_classes={"Acme_KnowledgeGraph"},
            scan_evidence=lambda **kw: calls.append(kw),
        )
        self.assertIsNone(plan)
        self.assertEqual(calls, [], "precondition must skip the scan")


# ---------------------------------------------------------------------------
# The decisive margin — the rule that heals the dual-write divergence
# ---------------------------------------------------------------------------


class _SplitBase(_Base):
    """Fixtures where the project's KG objects are SPLIT across two classes.

    Every path handed to a HOLDING class is one of the project's own files, so
    its ``matched_paths`` equals its path count exactly and its fraction is
    100% — both holders clear the doctor's ownership bar, which is what makes
    these the multi-candidate shape, and the path counts ARE the numbers the
    margin rule compares. (The non-holding class in ``seed_ghost_split`` gets
    somebody else's path, so it stays below the bar and out of the ranking.)
    Everything stays inside the doctor's ``SAMPLE_LIMIT`` (100), so each
    fixture is a state a real machine can actually produce.
    """

    BOUND = "Old_KnowledgeGraph"
    TOP = "GhostA_KnowledgeGraph"
    RUNNER = "GhostB_KnowledgeGraph"

    def seed_ghost_split(self, top_paths, runner_paths, *,
                         top_count=None, runner_count=None):
        """Bound class holds nobody's data; two unbound ghosts split it."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(top_paths)]
        self.m.project("p1", "Acme", primary=self.BOUND, files=files)
        self.m.klass(self.BOUND, paths=["knowledge/other/z.md"], count=1)
        self.m.klass(
            self.TOP, paths=files,
            count=len(files) if top_count is None else top_count,
        )
        self.m.klass(
            self.RUNNER, paths=files[:runner_paths],
            count=runner_paths if runner_count is None else runner_count,
        )
        return files

    def seed_bound_dominant(self, bound_paths, ghost_paths):
        """The BOUND class holds the corpus; an unbound ghost holds a few."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(bound_paths)]
        self.m.project("p1", "Acme", primary=self.BOUND, files=files)
        self.m.klass(self.BOUND, paths=files, count=len(files))
        self.m.klass(self.TOP, paths=files[:ghost_paths], count=ghost_paths)
        return files

    def assert_no_rw_open(self, run):
        """Run ``run()`` and assert launcher.db was never opened read-write."""
        rw: list = []
        real = sqlite3.connect

        def tracking(*a, **kw):
            dsn = a[0] if a else kw.get("database", "")
            if not (kw.get("uri") and isinstance(dsn, str)
                    and "mode=ro" in dsn):
                rw.append(dsn)
            return real(*a, **kw)

        with mock.patch("sqlite3.connect", side_effect=tracking):
            result = run()
        self.assertEqual(
            [d for d in rw if str(self.m.db) in str(d)], [],
            "nothing was owed, so the writer lock must never be taken",
        )
        return result


class DecisiveMarginTests(_SplitBase):
    def test_a_decisive_leader_is_healed_even_though_two_classes_clear(self):
        """The dual-write divergence, healed. 30 of the project's paths in one
        class against 3 in the other is not a tie — it is the measurement
        saying where the corpus lives (x10 the runner-up, +27 paths, so both
        margins are cleared with room).

        MUTATION: restore the old flat `len(cleared) > 1 → refuse` — red
        (the binding stays `Old_KnowledgeGraph`, which is the defect this
        wave exists to fix)."""
        self.seed_ghost_split(30, 3)

        report = self.m.run_update()

        self.assertEqual(self.m.primary_of("p1"), self.TOP)
        self.assertIn(REPOINT_CID, self.cids(report))
        self.assertNotIn(AMBIGUOUS_CID, self.cids(report))

    def test_the_leader_is_the_class_holding_the_data_not_the_biggest_one(self):
        """Ranking measure. The runner-up carries 900 objects — thirty times
        the leader's — but only 14 of THIS project's paths; the leader carries
        30 of them. `matched_paths` is the evidence measure and `count` is not:
        a class can be huge because it is somebody else's corpus.

        MUTATION: rank by `count` instead of `matched_paths` — the 900-object
        class becomes the leader, 14-vs-30 is not decisive, the heal refuses,
        red."""
        self.seed_ghost_split(30, 14, runner_count=900)

        self.m.run_update()

        self.assertEqual(self.m.primary_of("p1"), self.TOP)

    def test_exactly_at_the_factor_margin_refuses_one_more_path_heals(self):
        """STRICT `>`, on the factor leg. 30 vs 15 is a ratio of exactly 2.0,
        which the shipped `EVIDENCE_MARGIN_FACTOR` does NOT clear — a constant
        names the margin the evidence must EXCEED.

        The absolute leg is already satisfied here (+15 > +10), so the refusal
        can only be coming from the factor leg. Shrinking the runner-up by one
        path tips that leg and the same machine heals — which is what proves
        the boundary is where this test says it is, rather than the rule being
        broken-shut.

        MUTATION: relax either comparison to `>=` — the first leg goes red."""
        files = self.seed_ghost_split(30, 15)

        self.assertEqual(self.m.primary_of("p1"), self.BOUND)
        self.m.run_update()
        self.assertEqual(
            self.m.primary_of("p1"), self.BOUND,
            "a ratio of exactly x2.0 is not a decisive lead",
        )

        self.m.klass(self.RUNNER, paths=files[:14], count=14)
        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), self.TOP)

    def test_exactly_at_the_absolute_margin_refuses_one_more_path_heals(self):
        """STRICT `>`, on the absolute leg. 15 vs 5 is a difference of exactly
        10, which the shipped `EVIDENCE_MARGIN_ABS` does NOT clear.

        The factor leg is already satisfied here (x3.0 > x2.0), so the refusal
        can only be coming from the absolute leg — the mirror image of the
        test above, and together they pin both legs independently."""
        files = self.seed_ghost_split(15, 5)

        self.m.run_update()
        self.assertEqual(
            self.m.primary_of("p1"), self.BOUND,
            "a lead of exactly +10 matched paths is not a decisive lead",
        )

        self.m.klass(self.RUNNER, paths=files[:4], count=4)
        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), self.TOP)

    def test_a_dominant_bound_class_is_left_alone_and_not_asked_about(self):
        """BOUND-CLASS PROTECTION. The binding already names the class the
        evidence puts on top (30 paths against the ghost's 5). There is
        nothing to repair and nothing to ask: the row is right.

        The strongest available proof is the writer lock — a healthy machine
        must not take it. Dropping the `found.name == verdict.bound`
        short-circuit makes the plan propose a self-repoint, `has_work` turns
        true, install.py opens launcher.db read-write on every update, and
        this goes red (the row itself would still not move — the write-time
        target gate refuses it — so the RW open is what makes the regression
        visible at all).

        MUTATION: delete that short-circuit — red."""
        self.seed_bound_dominant(30, 5)

        report = self.assert_no_rw_open(self.m.run_update)

        self.assertEqual(self.m.primary_of("p1"), self.BOUND)
        self.assertNotIn(REPOINT_CID, self.cids(report))
        self.assertNotIn(AMBIGUOUS_CID, self.cids(report))
        self.assertEqual(self.m.config_of("p1").get("evidence_repoint"), None)

    def test_a_bound_class_that_only_leads_narrowly_is_still_ambiguous(self):
        """The other side of the protection. The bound class leads 30-to-28,
        which is a data SPLIT, not a verdict: the project reads only what its
        binding names, so more than a quarter of its corpus is unreachable and
        a human has to settle it. Nothing is written, and the ask is raised.

        MUTATION: treat "the leader is already bound" as healthy regardless of
        margin — red (no entry, and the split goes silent again)."""
        self.seed_bound_dominant(30, 28)

        report = self.m.run_update()

        self.assertEqual(self.m.primary_of("p1"), self.BOUND)
        self.assertNotIn(REPOINT_CID, self.cids(report))
        self.assertIn(AMBIGUOUS_CID, self.cids(report))

    def test_moving_the_margin_factor_moves_the_decision(self):
        """The constants are CONSUMED, not decorative — the same proof idiom
        `EvidenceRuleIsNotForkedTests` applies to the doctor's calibration.

        30 vs 15 refuses at the shipped x2.0. At x1.0 ("any lead at all counts,
        as long as the absolute margin holds") the very same fixture heals, so
        the shipped refusal is provably the constant's doing and not some
        unrelated gate quietly saying no."""
        from vco_lib import kg_binding_heal as kbh

        self.seed_ghost_split(30, 15)

        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), self.BOUND)

        with mock.patch.object(kbh, "EVIDENCE_MARGIN_FACTOR", 1.0):
            self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), self.TOP)

    def test_moving_the_absolute_margin_moves_the_decision(self):
        """Sibling of the test above, for the second constant. 15 vs 5 refuses
        at the shipped +10; at +9 the same machine heals."""
        from vco_lib import kg_binding_heal as kbh

        self.seed_ghost_split(15, 5)

        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), self.BOUND)

        with mock.patch.object(kbh, "EVIDENCE_MARGIN_ABS", 9):
            self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), self.TOP)


# ---------------------------------------------------------------------------
# What is still refused is no longer SILENT
# ---------------------------------------------------------------------------


class AmbiguityIsAskedAboutTests(_SplitBase):
    def entry(self, report, cid=AMBIGUOUS_CID):
        matches = [e for e in report.entries if e.condition_id == cid]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one {cid} entry, got {self.cids(report)}",
        )
        return matches[0]

    def blob(self, entry):
        return (f"{entry.title}\n{entry.detected}\n{entry.why_deferred}\n"
                f"{entry.command_to_apply}")

    def test_a_near_tie_refuses_and_names_every_candidate_with_its_numbers(
            self):
        """THE ASK. 30 vs 28 matched paths is a split, not a verdict — the
        heal writes nothing, and (v0.2.92) says so where the user reads.

        The entry has to carry enough for the reader to make the decision VCO
        declined to make: which project, which class the binding names, every
        class that holds the project's data, and HOW MUCH of it each holds —
        the numbers the ranking used, not just the class names.

        MUTATION: drop the `_emit_ambiguous_evidence_entry` call in
        install.py (or return early from the emitter) — red."""
        self.seed_ghost_split(30, 28)

        report = self.m.run_update()

        self.assertEqual(self.m.primary_of("p1"), self.BOUND)
        self.assertNotIn(REPOINT_CID, self.cids(report))
        entry = self.entry(report)
        blob = self.blob(entry)
        self.assertIn("Acme", blob)
        self.assertIn(self.BOUND, blob)
        for name, matched in ((self.TOP, 30), (self.RUNNER, 28)):
            self.assertIn(name, blob)
            self.assertIn(f"{matched}/{matched} sampled", blob)
        # The remedy is the channel that already exists, not a hand-edit.
        self.assertIn("Identity", blob)
        self.assertEqual(entry.severity, "warning")
        self.assertEqual(entry.resolved_disposition, "action_required")

    def test_the_ask_reaches_a_machine_that_owes_no_write(self):
        """PLACEMENT, pinned from both sides. An ambiguous project owes no
        write, so `has_work` is false and the RW heal pass is never entered —
        an emit sited beside `kg_binding_evidence_repointed` would be inert on
        exactly the machines this entry exists for.

        MUTATION A: move the emit into `self_heal_kg_bindings` — no entry, red.
        MUTATION B: 'fix' that by making a refusal set `needs_rebind = True` —
        the writer lock is taken with nothing to write, red on the RW leg."""
        self.seed_ghost_split(30, 28)

        report = self.assert_no_rw_open(self.m.run_update)

        self.assertIn(AMBIGUOUS_CID, self.cids(report))

    def test_the_ask_states_how_far_short_of_the_margin_the_leader_fell(self):
        """A refusal the reader cannot evaluate is just a shrug. The entry
        prints the leader's lead AND the two thresholds it had to beat, so
        "merge the leftovers and re-run" is a decision the user can reason
        about rather than a ritual.

        MUTATION: drop the margin sentence — red."""
        self.seed_ghost_split(30, 28)

        entry = self.entry(self.m.run_update())

        # runner-up 28 → needs > 56 (x2.0) and > 38 (+10).
        self.assertIn("30 vs 28", entry.detected)
        self.assertIn("56", entry.detected)
        self.assertIn("38", entry.detected)

    def test_a_class_another_project_owns_is_listed_but_never_offered(self):
        """The remedy must not propose a state the writer would refuse. One of
        the two classes holding p1's data is named by p2's binding: it belongs
        in the LISTING (the reader has to know where the data is), but not in
        the copy-paste SQL — pointing two projects at one class is exactly what
        the write-time gate exists to prevent, and an entry that suggests it
        would be teaching the user to create the worse state.

        MUTATION: drop the `c.bound and c.name != r.bound` skip when building
        the SQL — red."""
        files = [f"knowledge/concepts/n{i}.md" for i in range(30)]
        self.m.project("p1", "Acme", primary=self.BOUND, files=files)
        self.m.project("p2", "Other", primary=self.RUNNER)
        self.m.klass(self.BOUND, paths=["knowledge/other/z.md"], count=1)
        self.m.klass(self.TOP, paths=files, count=30)
        self.m.klass(self.RUNNER, paths=files[:28], count=28)

        entry = self.entry(self.m.run_update())

        self.assertIn(self.RUNNER, entry.detected)
        self.assertIn("another project's binding", entry.detected)
        self.assertNotIn(
            f"collection_name = '{self.RUNNER}'", entry.command_to_apply,
            "never offer SQL that would give one class two readers",
        )
        # The takeable one IS offered. (`Old_KnowledgeGraph` is not: it holds
        # none of this project's files, so it never cleared the bar and is not
        # a candidate — "keep reading an empty class" is not a remedy.)
        self.assertIn(
            f"collection_name = '{self.TOP}'", entry.command_to_apply)

    def test_keeping_the_currently_bound_class_is_an_offered_pick(self):
        """When the bound class is itself one of the holders — the commonest
        split — "I meant this one, stop asking" has to be expressible, or the
        entry's own clear route is unreachable without inventing SQL."""
        self.seed_bound_dominant(30, 28)

        entry = self.entry(self.m.run_update())

        self.assertIn(
            f"collection_name = '{self.BOUND}'", entry.command_to_apply)
        self.assertIn("manual_override", entry.command_to_apply)

    def test_a_manual_override_row_is_never_asked_about_again(self):
        """A DECLARED clear route, made real. The registry says this entry
        clears when the row records a deliberate human pick; the write-time
        gate already refuses to touch such a row, so continuing to ask about
        it would be VCO nagging for an action its own code would then refuse
        to act on.

        MUTATION: drop the `manual_override_projects` filter in
        `resolve_evidence_heal_plan` — red, and the entry becomes one the user
        can never clear by doing what it says."""
        self.seed_ghost_split(30, 28)
        self.assertIn(AMBIGUOUS_CID, self.cids(self.m.run_update()))

        self.m.set_config_json(
            "p1", "primary", json.dumps({"manual_override": "user-2026-09"}))

        report = self.m.run_update()
        self.assertNotIn(AMBIGUOUS_CID, self.cids(report))
        self.assertEqual(self.m.primary_of("p1"), self.BOUND)

    def test_a_healed_split_stops_asking(self):
        """The main clear route: the ask disappears the moment the evidence
        stops being ambiguous. Emptying the runner-up (what
        `migrate-collections` does when it merges the leftovers) leaves ONE
        class clearing the bar, and the same machine heals silently instead of
        asking."""
        self.seed_ghost_split(30, 28)
        self.assertIn(AMBIGUOUS_CID, self.cids(self.m.run_update()))

        # The leftover class is merged away: no objects, no sampled paths.
        self.m.klass(self.RUNNER, paths=[], count=0)

        report = self.m.run_update()
        self.assertNotIn(AMBIGUOUS_CID, self.cids(report))
        self.assertEqual(self.m.primary_of("p1"), self.TOP)
        self.assertIn(REPOINT_CID, self.cids(report))

    def test_the_registry_declares_the_condition(self):
        """The completeness gate source-scans emit sites; this pins the two
        properties the SCAN cannot check — the tier a reader is shown, and
        who owns the clear."""
        from vco_lib import deferral_registry as dr

        spec = dr.condition(AMBIGUOUS_CID)
        self.assertIsNotNone(spec, f"{AMBIGUOUS_CID} must be registered")
        self.assertEqual(spec.condition_class, "action_required")
        self.assertEqual(spec.owner, "vco_lib.kg_binding_heal")
        self.assertEqual(spec.clear_probe, "owned-drop-when-absent")


# ---------------------------------------------------------------------------
# The evidence rule is CONSUMED, not forked
# ---------------------------------------------------------------------------


class EvidenceRuleIsNotForkedTests(_Base):
    """Move the DOCTOR's calibration; the HEAL's decision must move with it.

    This is the behavioural proof that ``kg_binding_heal`` holds no second
    copy of the ownership bar. A forked threshold would keep its own answer
    while the doctor's changed — the exact drift that would let the report
    and the repair disagree about one machine.
    """

    def test_lowering_the_doctor_floor_makes_the_heal_fire(self):
        files = ["knowledge/concepts/only.md"]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("Ghost_KnowledgeGraph", paths=files, count=1)

        # At the shipped floor (2 distinct matching paths) this is below-bar.
        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Old_KnowledgeGraph")

        with mock.patch.object(kbd, "MIN_MATCHED_PATHS", 1):
            self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Ghost_KnowledgeGraph")

    def test_lowering_the_doctor_fraction_makes_the_heal_fire(self):
        files = [f"knowledge/concepts/n{i}.md" for i in range(3)]
        self.m.project("p1", "Acme", primary="Old_KnowledgeGraph", files=files)
        self.m.klass("Old_KnowledgeGraph", paths=["knowledge/other/z.md"],
                     count=1)
        self.m.klass("Ghost_KnowledgeGraph",
                     paths=[*files, "knowledge/gone/x.md",
                            "knowledge/gone/y.md"], count=5)

        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Old_KnowledgeGraph")

        with mock.patch.object(kbd, "OWNERSHIP_MATCH_FRACTION", 0.5):
            self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Ghost_KnowledgeGraph")

    def test_raising_the_doctor_fraction_stops_a_heal_that_otherwise_fires(
            self):
        self.seed_ghost_shape()
        with mock.patch.object(kbd, "OWNERSHIP_MATCH_FRACTION", 1.5):
            self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Old_KnowledgeGraph")

        self.m.run_update()
        self.assertEqual(self.m.primary_of("p1"), "Ghost_KnowledgeGraph")


if __name__ == "__main__":
    unittest.main()
