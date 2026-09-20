# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WP-9 — one home for "may this schema migration be applied without asking?".

v0.2.95. The policy used to exist twice: `vco_lib.project_init` decided which
plan entries force a `schema_migration_required` deferral, and
`projects_v2.rs::should_auto_apply_additive` re-derived the whole rule in Rust
to decide whether to issue the wet apply. This module pins the collapsed
shape — `vco_lib.migration_plan_classify` classifies, the CLI publishes the
verdict, Rust reads it — at three levels:

* the classifier itself, both arms of every gate (the standing rule for a
  branch that gates a destructive action: test the ACT and the LEAVE-ALONE);
* the CLI, end to end, so the verdict on the wire is the one the classifier
  produced and the deferral is driven by the SAME reading of the plan;
* the cross-language key, read out of the Rust source, so renaming it on one
  side fails here instead of silently disabling auto-apply in the field.

Nothing here reaches Weaviate: `migrate_collections` is mocked at the
`project_init` boundary, exactly as `tests/test_vco_lib_migrate.py` does it,
and every plan is a literal.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from tests.common.rust_source import cfg_test_line_numbers, strip_rust_comments

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import migration_plan_classify as mpc  # noqa: E402
from vco_lib import project_init  # noqa: E402

PROJECTS_V2_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"
)


def _plan(*actions: str) -> dict:
    """A `migrate_collections` result with one entry per action."""
    return {
        "plan": [
            {
                "collection": f"Foo_Collection{i}",
                "action": action,
                "objects_copied": 0,
                "elapsed_ms": 0,
            }
            for i, action in enumerate(actions)
        ],
        "dry_run": True,
        "errors": [],
    }


class TheClassifierDecidesBothWays(unittest.TestCase):
    """Every gate, in both directions. An auto-apply that fires when it should
    not costs the user their vectors; one that refuses when it should fire
    costs one update cycle — so the arms are not symmetric in consequence, and
    both get an assertion."""

    def test_an_additive_only_plan_is_applied(self) -> None:
        for action in sorted(mpc.ADDITIVE_ACTIONS):
            with self.subTest(action=action):
                self.assertTrue(
                    mpc.classify_migration_plan(_plan(action))[
                        "auto_apply_additive"
                    ],
                    f"{action} is lossless — it must not need consent",
                )

    def test_a_lossy_entry_vetoes_the_whole_plan(self) -> None:
        # Mixed: the additive half is NOT split out and applied behind the
        # rebuild's back. It waits for the same consent.
        result = mpc.classify_migration_plan(_plan("copy", "rebuild"))
        self.assertFalse(result["auto_apply_additive"])
        self.assertTrue(result["has_additive"])
        self.assertTrue(result["has_lossy"])

    def test_a_rebuild_only_plan_is_never_applied(self) -> None:
        self.assertFalse(
            mpc.classify_migration_plan(_plan("rebuild"))["auto_apply_additive"]
        )

    def test_a_plan_with_nothing_additive_is_not_a_vacuous_yes(self) -> None:
        """`noop` / `create` are NEITHER additive nor lossy: there is nothing
        to apply, so the answer is False rather than "no objection"."""
        for action in ("noop", "create"):
            with self.subTest(action=action):
                verdict = mpc.classify_migration_plan(_plan(action))
                self.assertFalse(verdict["auto_apply_additive"])
                self.assertFalse(verdict["has_additive"])
                self.assertFalse(verdict["has_lossy"])

    def test_a_probe_that_reported_errors_is_not_trusted(self) -> None:
        dirty = _plan("copy")
        dirty["errors"] = [
            {"collection": "Foo_Collection0", "action": "copy", "error": "boom"}
        ]
        self.assertFalse(
            mpc.classify_migration_plan(dirty)["auto_apply_additive"],
            "a plan whose probe hit an error describes drift only partly seen",
        )

    def test_an_empty_or_malformed_envelope_classifies_as_do_nothing(self) -> None:
        for envelope in (
            {},
            {"plan": []},
            {"plan": None},
            {"plan": "copy"},
            {"plan": [None, 7, "copy"]},
        ):
            with self.subTest(envelope=envelope):
                verdict = mpc.classify_migration_plan(envelope)
                self.assertFalse(verdict["auto_apply_additive"])
                self.assertEqual(verdict["lossy_entries"], [])

    def test_the_lossy_entries_are_the_ones_the_deferral_names(self) -> None:
        entries = mpc.lossy_plan_entries(_plan("copy", "rebuild", "noop"))
        self.assertEqual([e["action"] for e in entries], ["rebuild"])
        # LEAVE-ALONE arm: an additive-only plan defers nothing.
        self.assertEqual(mpc.lossy_plan_entries(_plan("copy", "noop")), [])

    def test_the_additive_collection_list_names_only_additive_entries(self) -> None:
        result = _plan("copy", "rebuild", "noop", "patch_props")
        self.assertEqual(
            mpc.additive_collections(result),
            ["Foo_Collection0", "Foo_Collection3"],
        )
        self.assertEqual(mpc.additive_collections(_plan("rebuild")), [])


class TheActionVocabularyIsFullyClassified(unittest.TestCase):
    """Nothing the planner can emit may be un-triaged.

    `_classify_action` is a pure function of `SchemaDelta`'s fields, so the
    whole of its output space is reachable by enumerating truthy/falsy values
    for each field — no source scan, and no hand-maintained list of action
    names that could go stale. A NEW field with a type this test does not know
    fails it on purpose: a delta that can steer the planner must not be added
    without someone deciding what its action means for consent.
    """

    @staticmethod
    def _truthy(field: dataclasses.Field) -> object:
        annotation = str(field.type)
        if "bool" in annotation:
            return True
        if "list[str]" in annotation:
            return ["some_slot"]
        if "list[dict]" in annotation:
            return [{"name": "some_prop", "dataType": ["text"]}]
        if "str" in annotation:  # Optional[str]
            return "hfresh"
        raise AssertionError(
            f"SchemaDelta.{field.name}: {annotation} — this test enumerates "
            f"the planner's whole input space and does not know how to make "
            f"this field truthy. Teach it, and decide whether the action it "
            f"steers to is additive, lossy or neither."
        )

    def _every_action(self) -> set:
        fields = list(dataclasses.fields(project_init.SchemaDelta))
        seen = set()
        for combo in itertools.product([False, True], repeat=len(fields)):
            kwargs = {
                f.name: (self._truthy(f) if on else f.default_factory()
                         if f.default_factory is not dataclasses.MISSING
                         else f.default)
                for f, on in zip(fields, combo)
            }
            seen.add(project_init._classify_action(
                project_init.SchemaDelta(**kwargs)
            ))
        return seen

    def test_every_reachable_action_is_additive_lossy_or_deliberately_neither(
        self,
    ) -> None:
        deliberately_neither = {"noop", "create"}
        unclassified = (
            self._every_action()
            - mpc.ADDITIVE_ACTIONS
            - mpc.LOSSY_ACTIONS
            - deliberately_neither
        )
        self.assertEqual(
            unclassified,
            set(),
            "the planner can emit an action `migration_plan_classify` has "
            "never been told about; an unknown action currently classifies "
            "as do-nothing, which is safe but silent — decide it explicitly",
        )

    def test_the_sets_did_not_widen(self) -> None:
        """`rebuild` is the one thing consent exists for. A change here is a
        change to what gets applied to a user's data unattended."""
        self.assertEqual(mpc.ADDITIVE_ACTIONS, frozenset({"copy", "patch_props"}))
        self.assertEqual(mpc.LOSSY_ACTIONS, frozenset({"rebuild"}))
        self.assertEqual(mpc.ADDITIVE_ACTIONS & mpc.LOSSY_ACTIONS, frozenset())


class TheCliPublishesTheVerdict(unittest.TestCase):
    """End to end through `migrate-collections --json`: the launcher's only
    view of this decision is the envelope, so the envelope is what gets
    asserted."""

    def _run(self, fake_result: dict, folder: Path) -> dict:
        """Drive the real CLI handler with a canned plan.

        Two seams, both with precedent in
        `tests/test_vco_lib_migrate.py::CliMigrateCommandTests`:

        * `migrate_collections` is mocked — the plan is the INPUT to this
          decision, so it is supplied rather than discovered;
        * `weaviate_schema._list_all_classes` is pinned at `[]` because the
          handler ALWAYS runs the v0.2.18 additive helper afterwards, whose
          enumerators read `/v1/schema`. Since v0.2.92 W18 an unreachable
          server raises rather than returning `[]`, which lands in `errors[]`
          and makes `main()` exit 1 — i.e. without this pin the class would
          quietly require a live Weaviate. That seam is the one
          `weaviate_schema.py` documents for the purpose, so the real
          enumerator filtering stays in the path; only the socket goes.

        The three collection env keys the handler injects are reverted by its
        own `_scoped_environ` block (v0.2.84 F3), so nothing leaks into the
        shared test process from here.
        """
        from vco_lib import weaviate_schema as _ws

        with mock.patch.object(_ws, "_list_all_classes", return_value=[]), \
             mock.patch.object(
                 project_init, "migrate_collections", return_value=fake_result
             ):
            argv = [
                "migrate-collections", "--name", "Foo", "--dry-run",
                "--project-folder", str(folder), "--json",
            ]
            buf = StringIO()
            with mock.patch.object(sys, "stdout", buf):
                rc = project_init.main(argv)
            payload = json.loads(buf.getvalue().strip())
            self.assertEqual(rc, 0, f"CLI exited {rc}: {payload.get('errors')}")
            for key in (mpc.AUTO_APPLY_KEY, mpc.ADDITIVE_COLLECTIONS_KEY):
                self.assertIn(
                    key, payload,
                    f"the envelope must carry `{key}` — it is the launcher's "
                    f"only view of this decision, and a missing key silently "
                    f"leaves additive drift un-applied forever",
                )
            return payload

    def test_an_additive_plan_is_published_as_appliable_and_defers_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = self._run(_plan("copy"), Path(td))
        self.assertIs(payload[mpc.AUTO_APPLY_KEY], True)
        self.assertIs(
            payload["deferral_emitted"], False,
            "a lossless plan must not ask for consent",
        )
        self.assertEqual(payload["additive_collections"], ["Foo_Collection0"])

    def test_a_rebuild_plan_is_published_as_not_appliable_and_defers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            payload = self._run(_plan("rebuild"), folder)
            body = (
                folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            ).read_text(encoding="utf-8")
        self.assertIs(payload[mpc.AUTO_APPLY_KEY], False)
        self.assertIs(payload["deferral_emitted"], True)
        self.assertEqual(payload["additive_collections"], [])
        self.assertIn("schema_migration_required", body)

    def test_the_verdict_and_the_deferral_are_one_reading_of_the_plan(
        self,
    ) -> None:
        """A mixed plan is the case where two readings could disagree: the
        rebuild defers AND the copy is withheld. Both come off one call.

        `additive_collections` still NAMES the copy — it is a pure reading of
        the plan, matching the filter the Rust side used to run — and the
        launcher never renders it for a deferred plan, because it only reads
        that list from the WET apply it declines to make here.
        """
        with tempfile.TemporaryDirectory() as td:
            payload = self._run(_plan("copy", "rebuild"), Path(td))
        self.assertIs(payload[mpc.AUTO_APPLY_KEY], False)
        self.assertIs(payload["deferral_emitted"], True)
        self.assertEqual(payload["additive_collections"], ["Foo_Collection0"])


class TheKeysAreTheSameOnBothSides(unittest.TestCase):
    """The launcher reads these literals; Python writes them. A rename on one
    side is silent in the field (the reader just never finds the field and
    conservatively refuses), so it is caught here.

    Comments are stripped and `#[cfg(test)]` items removed before matching, so
    neither prose naming the key nor a test fixture can satisfy this — and the
    test-item spans come from `tests/common/rust_source.py`, the one home for
    that question."""

    def _rust_production_source(self) -> str:
        raw = PROJECTS_V2_RS.read_text(encoding="utf-8")
        gated = cfg_test_line_numbers(raw)
        return "\n".join(
            line
            for n, line in enumerate(strip_rust_comments(raw), start=1)
            if n not in gated
        )

    def test_the_launcher_reads_the_keys_python_publishes(self) -> None:
        # `assertTrue(x in src)` rather than `assertIn`: the haystack is a
        # 13 000-line file and unittest prints it whole on failure.
        src = self._rust_production_source()
        for const, value in (
            ("AUTO_APPLY_ADDITIVE_KEY", mpc.AUTO_APPLY_KEY),
            ("ADDITIVE_COLLECTIONS_KEY", mpc.ADDITIVE_COLLECTIONS_KEY),
        ):
            with self.subTest(const=const):
                self.assertTrue(
                    f'{const}: &str = "{value}"' in src,
                    f"{PROJECTS_V2_RS.name} must declare {const} = {value!r} — "
                    f"the key the launcher looks for is the key "
                    f"migration_plan_classify publishes, or the launcher "
                    f"silently never finds the verdict",
                )

    def test_the_rust_side_no_longer_re_derives_the_action_vocabulary(
        self,
    ) -> None:
        """WP-9's actual deliverable. `patch_props` appearing in production
        Rust again means a third copy of the policy has grown back."""
        src = self._rust_production_source()
        self.assertTrue(
            "patch_props" not in src,
            "the additive-action vocabulary reappeared in production Rust — "
            "it has one home (vco_lib/migration_plan_classify.py) and the "
            "launcher reads its verdict off the migrate-collections envelope",
        )
