# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-18 (W14) — the collection-rename engine.

BOTH SIDES OF EVERY DESTRUCTIVE-CAPABLE STEP. The leave-alone side is the one
that matters here: this operation copies populated Weaviate collections, and a
suite that only proves the intended classes moved cannot tell a correct rename
from one that also wrote into a neighbour's data or quietly dropped something.

Every test drives a FAKE Weaviate through :class:`WeaviateOps` and a synthetic
sqlite fixture. Nothing here touches a real server or the real ``launcher.db``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_codegraph_binding,
    add_kg_binding,
    add_project,
    create_empty_launcher_db,
    insert_rows,
)
from vco_lib import collection_rename as cr  # noqa: E402


# ───────────────────────────────────────────────────────────────────────────
# Fakes
# ───────────────────────────────────────────────────────────────────────────


class FakeWeaviate:
    """A minimal in-memory Weaviate: class name -> {uuid: named-vector dict}.

    ``reachable=False`` makes every probe return ``None`` — the tri-state
    "could not check" that the engine must refuse on rather than read as
    "absent". ``fail_copy_on`` injects the mid-copy interruption.
    """

    def __init__(self, classes=None, *, reachable=True, fail_copy_on=None,
                 unreadable_counts=()):
        self.classes: dict[str, dict] = {k: dict(v) for k, v in
                                         (classes or {}).items()}
        self.reachable = reachable
        self.fail_copy_on = fail_copy_on
        self.unreadable_counts = set(unreadable_counts)
        self.created: list[str] = []
        self.copied: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.reminted: list[tuple] = []
        self.remint_result = {"moved": 0, "deduped": 0, "left": 0,
                              "failures": 0, "line": "IDENTITY_MIGRATION "
                                                     "moved=0 deduped=0 "
                                                     "left=0 failures=0"}

    # -- ops -------------------------------------------------------------

    def probe(self, names):
        if not self.reachable:
            return {n: None for n in names}
        return {n: (n in self.classes) for n in names}

    def count(self, name):
        if not self.reachable or name in self.unreadable_counts:
            return None
        return len(self.classes.get(name, {}))

    def create_class(self, payload):
        name = payload["class"]
        self.created.append(name)
        self.classes.setdefault(name, {})

    def copy_class(self, src, dst):
        if self.fail_copy_on == dst:
            raise RuntimeError(f"injected copy failure at {dst}")
        # UUID-preserving: re-copying the same source converges rather than
        # duplicating. Modelled faithfully because the resume guarantee rests
        # on exactly this property.
        self.classes.setdefault(dst, {}).update(self.classes.get(src, {}))
        self.copied.append((src, dst))
        return len(self.classes.get(src, {}))

    def sample_vectors(self, name, limit):
        return dict(list(self.classes.get(name, {}).items())[:limit])

    def fetch_vectors(self, name, uuids):
        holder = self.classes.get(name, {})
        return {u: holder.get(u) for u in uuids}

    def delete_class(self, name):
        self.deleted.append(name)
        self.classes.pop(name, None)

    def remint(self, prefix, old_identity, new_identity, *, dry_run=False):
        self.reminted.append((prefix, old_identity, new_identity, dry_run))
        return dict(self.remint_result)

    def ops(self) -> cr.WeaviateOps:
        return cr.WeaviateOps(
            probe=self.probe, count=self.count, create_class=self.create_class,
            copy_class=self.copy_class, sample_vectors=self.sample_vectors,
            fetch_vectors=self.fetch_vectors, delete_class=self.delete_class,
            remint_identity=self.remint,
        )

    def snapshot(self) -> str:
        """A hash-able fingerprint of every class and every object in it.

        The leave-alone assertions compare THIS, not a human reading of the
        report: a report can say "untouched" while the data moved.
        """
        return json.dumps({k: sorted(v.items()) for k, v in
                           sorted(self.classes.items())}, sort_keys=True)


def make_db(tmp: Path, projects, kg_bindings=(), codegraph=(), moves=(),
            builds=()) -> Path:
    """A launcher.db carrying the REAL schema, seeded for the rename engine.

    ``projects`` rows are ``(id, name, slug, folder_path, host)`` tuples;
    ``kg_bindings`` ``(project_id, role, collection_name, kg_dir_path)``;
    ``codegraph`` ``(project_id, collection_prefix)``. ``moves`` and
    ``builds`` take MAPPINGS for ``project_moves`` / ``code_graph_builds``
    (both real tables since migration 044 / 016) — the NOT NULL columns the
    pre-merge three-column guess omitted (``src``, ``dst``, ``started_at``)
    are why they are not positional.
    """
    path = tmp / "launcher.db"
    create_empty_launcher_db(path)
    for pid, name, slug, folder_path, host in projects:
        add_project(path, project_id=pid, name=name, slug=slug,
                    folder_path=folder_path, host=host)
    for pid, role, collection_name, kg_dir_path in kg_bindings:
        add_kg_binding(path, pid, role, collection_name,
                       kg_dir_path=kg_dir_path)
    for pid, prefix in codegraph:
        add_codegraph_binding(path, pid, prefix)
    if moves:
        insert_rows(path, "project_moves", moves)
    if builds:
        insert_rows(path, "code_graph_builds", builds)
    return path


class RenameFixture:
    """One registered project ``Old_Name`` with a populated family.

    The name carries an UNDERSCORE on purpose: that is the only shape where
    the two sanitizers diverge (KG drops it -> ``OldName_KnowledgeGraph``,
    code preserves it -> ``Old_Name_CodeFunction``), and a fixture without one
    would let a single-rule derivation pass every test. It is also the real
    shape — this machine's own orchestrator project is ``VCO_dev``."""

    def __init__(self, stack, *, extra_projects=(), extra_classes=None,
                 codegraph=True, **fake_kw):
        self.tmp = Path(stack.enter_context(TemporaryDirectory()))
        self.folder = self.tmp / "proj"
        (self.folder / ".claude" / "context").mkdir(parents=True)
        (self.folder / ".claude" / "state").mkdir(parents=True)
        projects = [("p1", "Old_Name", "old-name", str(self.folder), "base")]
        projects.extend(extra_projects)
        self.db_path = make_db(
            self.tmp, projects,
            kg_bindings=[("p1", "primary", "OldName_KnowledgeGraph", None)],
            codegraph=[("p1", "Old_Name")] if codegraph else [],
        )
        classes = {
            "OldName_KnowledgeGraph": {"u1": {"qwen3_embed": [0.1, 0.2]},
                                       "u2": {"qwen3_embed": [0.3, 0.4]}},
            "OldName_Development": {"d1": {"qwen3_embed": [1.0]}},
            "OldName_Diagrams": {},
            "Old_Name_CodeFunction": {"f1": {"codesage_embed": [9.0]}},
            "Old_Name_CodeModule": {},
            "Old_Name_CodeClass": {},
            "Old_Name_CodeAPI": {},
            "Old_Name_CodeInteraction": {},
        }
        classes.update(extra_classes or {})
        self.fake = FakeWeaviate(classes, **fake_kw)

    def plan(self, new_name="New_Name"):
        return cr.plan_rename("p1", new_name, ops=self.fake.ops(),
                              db_path=self.db_path)


# ───────────────────────────────────────────────────────────────────────────
# Planning + refusals
# ───────────────────────────────────────────────────────────────────────────


class TestPlanning(unittest.TestCase):
    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def test_plan_pairs_both_families_through_their_own_sanitizers(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        pairs = {(m.src, m.dst) for m in plan.moves}
        # KG family DROPS underscores; the code family PRESERVES them. Deriving
        # both with one rule is the R2 bug.
        self.assertIn(("OldName_KnowledgeGraph", "NewName_KnowledgeGraph"), pairs)
        self.assertIn(("Old_Name_CodeFunction", "New_Name_CodeFunction"), pairs)
        self.assertEqual(plan.new_code_prefix, "New_Name")

    def test_plan_is_read_only(self):
        fx = RenameFixture(self.stack)
        before = fx.fake.snapshot()
        fx.plan()
        self.assertEqual(fx.fake.snapshot(), before)
        self.assertEqual(fx.fake.created, [])
        self.assertEqual(fx.fake.deleted, [])

    def test_current_family_is_read_from_the_binding_not_the_name(self):
        """The binding is the identity. A name-derived family would rename a
        collection the project does not actually use."""
        fx = RenameFixture(self.stack)
        conn = sqlite3.connect(fx.db_path)
        conn.execute("UPDATE project_kg_bindings SET collection_name = "
                     "'Legacy_KnowledgeGraph'")
        conn.commit()
        conn.close()
        fx.fake.classes["Legacy_KnowledgeGraph"] = {"x": {"qwen3_embed": [1.0]}}
        plan = fx.plan()
        kg = [m for m in plan.moves if m.role == cr.ROLE_KG_PRIMARY][0]
        self.assertEqual(kg.src, "Legacy_KnowledgeGraph")

    def test_unreachable_weaviate_refuses_and_never_reads_as_absent(self):
        fx = RenameFixture(self.stack, reachable=False)
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "weaviate_unreachable")

    def test_populated_destination_refuses(self):
        fx = RenameFixture(
            self.stack,
            extra_classes={"NewName_KnowledgeGraph":
                           {"other": {"qwen3_embed": [7.0]}}})
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "destination_exists")

    def test_a_destination_bound_by_a_peer_refuses(self):
        fx = RenameFixture(
            self.stack,
            extra_projects=[("p2", "New Name", "new-name", "/tmp/p2", "base")])
        add_kg_binding(fx.db_path, "p2", "primary",
                       "NewName_KnowledgeGraph")
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "prefix_collision")

    def test_a_peer_code_prefix_collision_refuses_case_insensitively(self):
        fx = RenameFixture(
            self.stack,
            extra_projects=[("p2", "Peer", "peer", "/tmp/p2", "base")])
        add_codegraph_binding(fx.db_path, "p2", "new_name")
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "prefix_collision")

    def test_orchestrator_root_refuses(self):
        fx = RenameFixture(self.stack)
        conn = sqlite3.connect(fx.db_path)
        conn.execute("UPDATE projects SET host='orchestrator_root' "
                     "WHERE id='p1'")
        conn.commit()
        conn.close()
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "orchestrator_root")

    def test_shared_kg_designation_refuses(self):
        fx = RenameFixture(self.stack)
        add_kg_binding(fx.db_path, "p1", "shared",
                       "OldName_KnowledgeGraph")
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "shared_kg_designation")

    def test_a_live_move_refuses(self):
        fx = RenameFixture(self.stack)
        insert_rows(fx.db_path, "project_moves", [{
            "id": "mv1", "project_id": "p1", "status": "running",
            "src": str(fx.folder), "dst": str(fx.tmp / "moved"),
        }])
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "move_in_flight")

    def test_a_pending_codegraph_build_refuses(self):
        fx = RenameFixture(self.stack)
        insert_rows(fx.db_path, "code_graph_builds", [{
            "project_id": "p1", "status": "pending",
        }])
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "build_in_flight")

    def test_same_family_refuses_rather_than_no_opping(self):
        fx = RenameFixture(self.stack)
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan("Old_Name")
        self.assertEqual(ctx.exception.reason, "no_change")

    def test_degenerate_name_refuses(self):
        fx = RenameFixture(self.stack)
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan("   ")
        self.assertIn(ctx.exception.reason,
                      ("degenerate_name", "no_change"))

    def test_unknown_project_refuses(self):
        fx = RenameFixture(self.stack)
        with self.assertRaises(cr.RenameRefused) as ctx:
            cr.plan_rename("nope", "New Name", ops=fx.fake.ops(),
                           db_path=fx.db_path)
        self.assertEqual(ctx.exception.reason, "project_not_found")

    def test_an_unreadable_count_refuses(self):
        fx = RenameFixture(
            self.stack, unreadable_counts=["OldName_KnowledgeGraph"])
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "count_unreadable")


# ───────────────────────────────────────────────────────────────────────────
# The ALREADY-DAMAGED axis — a binding naming a class that does not exist
# ───────────────────────────────────────────────────────────────────────────


class TestAlreadyDamaged(unittest.TestCase):
    """The measured population: 3 of 5 prefix records on this machine name a
    prefix matching ZERO live classes. The command must give those users a
    plan, not a refusal, and must not pretend it carried data it did not."""

    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def _damaged(self):
        fx = RenameFixture(self.stack)
        # The bound code prefix addresses nothing live.
        conn = sqlite3.connect(fx.db_path)
        conn.execute("UPDATE project_codegraph_bindings SET "
                     "collection_prefix='Ghost_Prefix'")
        conn.commit()
        conn.close()
        return fx

    def test_dry_run_on_a_damaged_project_plans_and_refuses_nothing(self):
        fx = self._damaged()
        plan = fx.plan()  # must NOT raise
        code = [m for m in plan.moves if m.family == cr.FAMILY_CODE]
        self.assertTrue(code)
        self.assertTrue(all(m.action == cr.ACTION_SOURCE_ABSENT for m in code))

    def test_the_plan_says_no_data_is_carried_for_the_missing_classes(self):
        fx = self._damaged()
        plan = fx.plan()
        self.assertTrue(any("do not exist in Weaviate and carry no data" in w
                            for w in plan.warnings))
        self.assertNotIn("Ghost_Prefix_CodeFunction", plan.retired_classes)

    def test_a_missing_source_still_gets_a_real_destination_class(self):
        """The point of the whole feature: never leave a binding naming a
        class that does not exist."""
        fx = self._damaged()
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        self.assertIn("New_Name_CodeFunction", fx.fake.classes)
        self.assertEqual(fx.fake.copied, [
            ("OldName_KnowledgeGraph", "NewName_KnowledgeGraph"),
            ("OldName_Development", "NewName_Development"),
        ])


# ───────────────────────────────────────────────────────────────────────────
# Copy + verify
# ───────────────────────────────────────────────────────────────────────────


class TestCopyAndVerify(unittest.TestCase):
    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def test_copy_then_verify_passes_with_equal_counts_and_vectors(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        report = cr.verify_copy(plan, ops=fx.fake.ops())
        self.assertTrue(report["ok"])
        self.assertEqual(report["classes"]["NewName_KnowledgeGraph"],
                         {"src": 2, "dst": 2})
        self.assertGreater(report["vectors_sampled"], 0)

    def test_the_copy_never_touches_the_source_objects(self):
        fx = RenameFixture(self.stack)
        before = {k: dict(v) for k, v in fx.fake.classes.items()}
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        for name, objs in before.items():
            self.assertEqual(fx.fake.classes[name], objs,
                             f"{name} must be byte-identical after a copy")

    def test_the_copy_drops_nothing(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        cr.verify_copy(plan, ops=fx.fake.ops())
        self.assertEqual(fx.fake.deleted, [])

    def test_verify_aborts_on_a_count_mismatch_and_deletes_nothing(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        # An EXTRA object at the destination, not a missing one. Removing an
        # object would also trip the sampled-vector read-back, so the test
        # would pass with the count guard disabled — the exact "passed for the
        # wrong reason" shape W3 hit. Red-proofed: neutralising the count
        # comparison makes this test fail.
        fx.fake.classes["NewName_KnowledgeGraph"]["stowaway"] = {
            "qwen3_embed": [9.9]}
        before = fx.fake.snapshot()
        with self.assertRaises(cr.RenameRefused) as ctx:
            cr.verify_copy(plan, ops=fx.fake.ops())
        self.assertEqual(ctx.exception.reason, "count_unreadable")
        self.assertIn("Nothing was flipped", str(ctx.exception))
        self.assertEqual(fx.fake.snapshot(), before, "abort deletes nothing")
        self.assertEqual(fx.fake.deleted, [])

    def test_verify_aborts_when_a_copied_vector_differs(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        fx.fake.classes["NewName_KnowledgeGraph"]["u1"] = {
            "qwen3_embed": [0.1, 0.9999]}
        with self.assertRaises(cr.RenameRefused) as ctx:
            cr.verify_copy(plan, ops=fx.fake.ops())
        self.assertIn("vector", str(ctx.exception))
        self.assertEqual(fx.fake.deleted, [])

    def test_verify_aborts_when_weaviate_goes_away_mid_operation(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        fx.fake.reachable = False
        with self.assertRaises(cr.RenameRefused) as ctx:
            cr.verify_copy(plan, ops=fx.fake.ops())
        self.assertEqual(ctx.exception.reason, "weaviate_unreachable")

    def test_vectors_equal_rejects_a_missing_slot(self):
        self.assertFalse(cr._vectors_equal({"a": [1.0], "b": [2.0]},
                                           {"a": [1.0]}))
        self.assertTrue(cr._vectors_equal({"a": [1.0]}, {"a": [1.0]}))
        self.assertFalse(cr._vectors_equal({"a": [1.0]}, {"a": [1.0, 0.0]}))


# ───────────────────────────────────────────────────────────────────────────
# THE INTERRUPTION — the failure must land AFTER real work has happened
# ───────────────────────────────────────────────────────────────────────────


class TestInterruption(unittest.TestCase):
    """W3's first atomicity test passed for the wrong reason: its failure hit
    statement 1, so nothing partial existed to roll back. These tests force
    the interruption to land LATE — after classes have been created and at
    least one full class has been copied — and then assert what the user is
    left with."""

    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def _interrupt_late(self):
        # Fail on the FOURTH class the copy touches, so KG + Development are
        # already fully copied and their destinations exist.
        fx = RenameFixture(self.stack, fail_copy_on="New_Name_CodeFunction")
        plan = fx.plan()
        with self.assertRaises(RuntimeError):
            cr.execute_copy(plan, ops=fx.fake.ops())
        return fx, plan

    def test_the_interruption_lands_after_real_work(self):
        fx, _ = self._interrupt_late()
        self.assertIn("NewName_KnowledgeGraph", fx.fake.classes)
        self.assertEqual(len(fx.fake.classes["NewName_KnowledgeGraph"]), 2,
                         "a full class was copied before the failure")
        self.assertGreaterEqual(len(fx.fake.copied), 2)

    def test_an_interrupted_copy_leaves_the_source_intact(self):
        fx, _ = self._interrupt_late()
        self.assertEqual(len(fx.fake.classes["OldName_KnowledgeGraph"]), 2)
        self.assertEqual(len(fx.fake.classes["Old_Name_CodeFunction"]), 1)
        self.assertEqual(fx.fake.deleted, [])

    def test_the_interrupted_state_is_visible_not_silent(self):
        fx, _ = self._interrupt_late()
        status = cr.rename_status(fx.folder)
        self.assertTrue(status["in_flight"])
        self.assertEqual(status["phase"], cr.PHASE_COPYING)
        self.assertIn("NOTHING was committed", status["summary"])
        self.assertIn("NewName_KnowledgeGraph", status["created_classes"])

    def test_the_sentinel_records_every_class_it_created_before_copying(self):
        """The fact that makes a resume safe rather than a collision."""
        fx, _ = self._interrupt_late()
        created = cr.read_sentinel(fx.folder)["created_classes"]
        self.assertIn("New_Name_CodeFunction", created,
                      "the class was created before its copy failed, so the "
                      "sentinel must own it")

    def test_a_resume_is_planned_not_refused(self):
        fx, _ = self._interrupt_late()
        fx.fake.fail_copy_on = None
        plan2 = fx.plan()  # must NOT raise destination_exists
        self.assertIsNotNone(plan2.resuming_from)
        resumed = [m for m in plan2.moves if m.action == cr.ACTION_RESUME]
        self.assertTrue(resumed)

    def test_a_resume_converges_instead_of_duplicating(self):
        fx, _ = self._interrupt_late()
        fx.fake.fail_copy_on = None
        plan2 = fx.plan()
        cr.execute_copy(plan2, ops=fx.fake.ops())
        report = cr.verify_copy(plan2, ops=fx.fake.ops())
        self.assertTrue(report["ok"])
        self.assertEqual(len(fx.fake.classes["NewName_KnowledgeGraph"]), 2,
                         "UUID-preserving copy re-writes, never appends")

    def test_a_destination_this_rename_did_not_create_still_refuses(self):
        """The resume allowance must not become a blanket write permission."""
        fx, _ = self._interrupt_late()
        fx.fake.fail_copy_on = None
        # Someone else's class appears at a name we have NOT claimed.
        fx.fake.classes["NewName_Diagrams"] = {"foreign": {"qwen3_embed": [5.0]}}
        s = cr.read_sentinel(fx.folder)
        s["created_classes"] = [c for c in s["created_classes"]
                                if c != "NewName_Diagrams"]
        cr.write_sentinel(fx.folder, s)
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "destination_exists")

    def test_a_flipped_sentinel_refuses_a_second_rename(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.claim_sentinel(fx.folder, {"project_id": "p1",
                                      "new_name": plan.new_name,
                                      "phase": cr.PHASE_FLIPPED,
                                      "started_at": 0})
        with self.assertRaises(cr.RenameRefused) as ctx:
            fx.plan()
        self.assertEqual(ctx.exception.reason, "rename_in_flight")

    def test_a_flipped_status_reads_as_committed_not_failed(self):
        fx = RenameFixture(self.stack)
        cr.claim_sentinel(fx.folder, {"project_id": "p1",
                                      "new_name": "New Name",
                                      "phase": cr.PHASE_FLIPPED,
                                      "started_at": 0})
        status = cr.rename_status(fx.folder)
        self.assertIn("COMMITTED", status["summary"])
        self.assertIn("Nothing is lost", status["summary"])

    def test_the_claim_is_atomic(self):
        fx = RenameFixture(self.stack)
        cr.claim_sentinel(fx.folder, {"phase": cr.PHASE_COPYING})
        with self.assertRaises(cr.RenameRefused) as ctx:
            cr.claim_sentinel(fx.folder, {"phase": cr.PHASE_COPYING})
        self.assertEqual(ctx.exception.reason, "rename_in_flight")


# ───────────────────────────────────────────────────────────────────────────
# The guarded drop — every refusal, and the act
# ───────────────────────────────────────────────────────────────────────────


class TestGuardedDrop(unittest.TestCase):
    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.fx = RenameFixture(self.stack)
        self.plan = self.fx.plan()
        cr.execute_copy(self.plan, ops=self.fx.fake.ops())
        cr.write_completed_record(self.plan)
        # The flip has happened: the bindings now name the NEW family.
        conn = sqlite3.connect(self.fx.db_path)
        conn.execute("UPDATE project_kg_bindings SET "
                     "collection_name='NewName_KnowledgeGraph'")
        conn.execute("UPDATE project_codegraph_bindings SET "
                     "collection_prefix='New_Name'")
        conn.commit()
        conn.close()

    def _drop(self, **kw):
        kw.setdefault("confirm", True)
        return cr.drop_retired(self.fx.folder, ops=self.fx.fake.ops(),
                               db_path=self.fx.db_path, **kw)

    def test_without_confirm_nothing_is_deleted(self):
        before = self.fx.fake.snapshot()
        out = self._drop(confirm=False)
        self.assertEqual(out["dropped"], [])
        self.assertEqual(self.fx.fake.snapshot(), before)
        self.assertTrue(any("--confirm" in r for r in out["refused"]))

    def test_with_no_record_nothing_is_deleted(self):
        (self.fx.folder / cr.COMPLETED_REL).unlink()
        before = self.fx.fake.snapshot()
        out = self._drop()
        self.assertEqual(out["dropped"], [])
        self.assertEqual(self.fx.fake.snapshot(), before)

    def test_unreachable_weaviate_refuses_rather_than_dropping_blind(self):
        self.fx.fake.reachable = False
        before = self.fx.fake.snapshot()
        out = self._drop()
        self.assertEqual(out["dropped"], [])
        self.assertEqual(self.fx.fake.snapshot(), before)

    def test_a_still_bound_class_is_refused(self):
        """The single most important refusal: a reverted or half-finished
        rename leaves the OLD class bound, and dropping it then is the data
        loss this whole package exists to prevent."""
        conn = sqlite3.connect(self.fx.db_path)
        conn.execute("UPDATE project_kg_bindings SET "
                     "collection_name='OldName_KnowledgeGraph'")
        conn.commit()
        conn.close()
        out = self._drop()
        self.assertNotIn("OldName_KnowledgeGraph", out["dropped"])
        self.assertIn("OldName_KnowledgeGraph", self.fx.fake.classes)
        self.assertTrue(any("STILL BOUND" in r for r in out["refused"]))

    def test_a_missing_replacement_is_refused(self):
        del self.fx.fake.classes["NewName_KnowledgeGraph"]
        out = self._drop()
        self.assertNotIn("OldName_KnowledgeGraph", out["dropped"])
        self.assertIn("OldName_KnowledgeGraph", self.fx.fake.classes)

    def test_a_replacement_that_lost_objects_is_refused(self):
        self.fx.fake.classes["NewName_KnowledgeGraph"].pop("u2")
        out = self._drop()
        self.assertNotIn("OldName_KnowledgeGraph", out["dropped"])
        self.assertIn("OldName_KnowledgeGraph", self.fx.fake.classes)
        self.assertTrue(any("holds only" in r for r in out["refused"]))

    def test_an_unreadable_count_is_refused(self):
        self.fx.fake.unreadable_counts.add("NewName_KnowledgeGraph")
        out = self._drop()
        self.assertNotIn("OldName_KnowledgeGraph", out["dropped"])
        self.assertIn("OldName_KnowledgeGraph", self.fx.fake.classes)

    def test_the_act_side_drops_exactly_the_retired_set(self):
        out = self._drop()
        self.assertIn("OldName_KnowledgeGraph", out["dropped"])
        self.assertNotIn("OldName_KnowledgeGraph", self.fx.fake.classes)
        # The NEW family and every unrelated class survive untouched.
        self.assertIn("NewName_KnowledgeGraph", self.fx.fake.classes)
        self.assertEqual(len(self.fx.fake.classes["NewName_KnowledgeGraph"]), 2)
        for dropped in out["dropped"]:
            self.assertTrue(dropped.startswith(("OldName_", "Old_Name_")))

    def test_a_peers_collection_is_never_dropped(self):
        self.fx.fake.classes["Peer_KnowledgeGraph"] = {"p": {"qwen3_embed": [1.0]}}
        out = self._drop()
        self.assertIn("Peer_KnowledgeGraph", self.fx.fake.classes)
        self.assertNotIn("Peer_KnowledgeGraph", out["dropped"])

    def test_an_unreadable_launcher_db_refuses(self):
        out = cr.drop_retired(self.fx.folder, confirm=True,
                              ops=self.fx.fake.ops(),
                              db_path=self.fx.tmp / "nonexistent.db")
        self.assertEqual(out["dropped"], [])


# ───────────────────────────────────────────────────────────────────────────
# Reconciliation
# ───────────────────────────────────────────────────────────────────────────


class TestReconcile(unittest.TestCase):
    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def _run(self, fx, plan, **kw):
        class _Ok:
            returncode = 0
            stderr = ""

        kw.setdefault("env_runner", lambda argv, env: _Ok())
        return cr.execute_reconcile(plan, ops=fx.fake.ops(), **kw)

    def test_the_identity_remint_runs_on_the_new_prefix_and_never_re_embeds(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        self._run(fx, plan)
        self.assertEqual(fx.fake.reminted,
                         [("New_Name", "Old_Name", "New_Name", False)])

    def test_the_prefix_generation_record_is_written_with_binding_provenance(self):
        from vco_lib import codegraph_prefix_record as cpr

        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        self._run(fx, plan)
        self.assertEqual(cpr.read_prefix(fx.folder), "New_Name")
        self.assertEqual(cpr.read_source(fx.folder), cpr.SOURCE_BINDING)

    def test_a_failed_env_projection_is_reported_not_swallowed(self):
        class _Fail:
            returncode = 1
            stderr = "boom"

        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        out = self._run(fx, plan, env_runner=lambda argv, env: _Fail())
        self.assertTrue(any("could not be re-projected" in w
                            for w in out["warnings"]))
        self.assertIn(cr.CID_RECONCILE_PENDING, out["deferrals"])

    def test_a_clean_reconcile_emits_only_the_retained_record(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        out = self._run(fx, plan)
        self.assertEqual(out["deferrals"], [cr.CID_OLD_RETAINED])

    def test_the_sentinel_is_cleared_only_after_reconciliation(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        self.assertIsNotNone(cr.read_sentinel(fx.folder))
        self._run(fx, plan)
        self.assertIsNone(cr.read_sentinel(fx.folder))

    def test_left_rows_from_the_remint_raise_a_named_entry(self):
        fx = RenameFixture(self.stack)
        fx.fake.remint_result = {"moved": 3, "deduped": 0, "left": 2,
                                 "failures": 0, "line": "IDENTITY_MIGRATION "
                                                        "moved=3 deduped=0 "
                                                        "left=2 failures=0"}
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        out = self._run(fx, plan)
        self.assertIn(cr.CID_IDENTITY_REMINT_INCOMPLETE, out["deferrals"])

    def test_reconcile_drops_nothing(self):
        fx = RenameFixture(self.stack)
        plan = fx.plan()
        cr.execute_copy(plan, ops=fx.fake.ops())
        before = fx.fake.snapshot()
        self._run(fx, plan)
        self.assertEqual(fx.fake.snapshot(), before)
        self.assertEqual(fx.fake.deleted, [])


# ───────────────────────────────────────────────────────────────────────────
# Tri-OS shapes (R12 / R14) — unit-tested, not "only verifiable on Linux"
# ───────────────────────────────────────────────────────────────────────────


class TestTriOsShapes(unittest.TestCase):
    def test_posix_quoting_uses_single_quotes(self):
        self.assertEqual(cr.quote_for_shell("/a b/c", platform="posix"),
                         "'/a b/c'")

    def test_windows_quoting_uses_double_quotes_cmd_understands(self):
        # cmd.exe does not understand single quotes at all, so a POSIX-quoted
        # path is a command a Windows user cannot run.
        self.assertEqual(cr.quote_for_shell(r"C:\Program Files\p",
                                            platform="nt"),
                         '"C:\\Program Files\\p"')

    def test_windows_quoting_escapes_an_embedded_quote(self):
        self.assertEqual(cr.quote_for_shell('a"b', platform="nt"), '"a""b"')

    def test_the_drop_command_is_runnable_on_every_os(self):
        for plat, opener in (("posix", "'"), ("nt", '"')):
            cmd = cr.drop_retired_command("/p ath", platform=plat)
            self.assertIn("vco project rename-collections --drop-retired "
                          "--confirm --folder", cmd)
            self.assertIn(opener, cmd)

    def test_the_analyzer_remediation_names_the_ps1_sibling_on_windows(self):
        posix = cr._analyze_wrapper(Path("/p"), platform="posix")
        win = cr._analyze_wrapper(Path("/p"), platform="nt")
        self.assertTrue(posix.endswith("code-graph-analyze"))
        self.assertTrue(win.endswith("code-graph-analyze.ps1"))

    def test_the_sentinel_path_is_separator_agnostic(self):
        # Built from Path parts, never a hardcoded "/" join, so it is correct
        # on Windows without a second code path.
        p = cr.sentinel_path("/x")
        self.assertEqual(p.parts[-3:],
                         (".claude", "context", ".vco-collection-rename.json"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
