# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-10 (v0.2.73): migrate-collections surfaces unmatched orphan `__staging`.

The env-configured recovery sweep only inspects the CURRENT
KG/DEV/DIAGRAMS collection names. A migration that crashed leaving
``OldName_KnowledgeGraph__staging`` becomes invisible once the project is
renamed before the next run. A-10 adds a second sweep that LISTS every
``*__staging`` class and SURFACES any not tied to the current env — as an
error (routed to the deferral integration), NEVER auto-dropped.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


class TestOrphanStagingSweep(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(force_rebuild=False)

    def _run(self, all_classes, env):
        """Drive migrate_collections with the orphan-staging sweep active but
        the rest of the migration stubbed out."""
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(project_init, "_list_classes", return_value=all_classes), \
             mock.patch.object(
                 project_init, "_recover_or_drop_orphan_staging",
                 return_value="none",
             ), \
             mock.patch.object(
                 project_init, "_build_plan", return_value=[],
             ):
            return project_init.migrate_collections(
                self.args, dry_run=True, weaviate_url="http://localhost:8081",
            )

    def test_unmatched_orphan_staging_surfaces_as_error(self):
        suffix = project_init._STAGING_SUFFIX
        env = {
            "KG_COLLECTION": "NewName_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "NewName_Development",
            "DIAGRAMS_COLLECTION": "NewName_Diagrams",
        }
        # A pre-rename crash left an OldName staging class + the current env's
        # own staging (which the env sweep already handled).
        orphan = "OldName_KnowledgeGraph" + suffix
        all_classes = [
            "NewName_KnowledgeGraph",
            "NewName_KnowledgeGraph" + suffix,  # handled by env sweep
            orphan,                             # unmatched → must surface
        ]
        result = self._run(all_classes, env)
        errs = result["errors"]
        surfaced = [e for e in errs if orphan in e.get("error", "")]
        self.assertTrue(
            surfaced,
            f"unmatched orphan {orphan} must be surfaced as an error; got {errs}",
        )
        # The current-env staging must NOT be double-surfaced by the A-10 sweep.
        env_staging = "NewName_KnowledgeGraph" + suffix
        self.assertFalse(
            any(env_staging in e.get("error", "")
                and "not tied to this" in e.get("error", "")
                for e in errs),
            "env-configured staging must not be surfaced by the A-10 sweep",
        )

    def test_missing_base_is_flagged_as_last_copy(self):
        """When the orphan's base collection is MISSING, the message must
        warn that staging may be the only surviving copy."""
        suffix = project_init._STAGING_SUFFIX
        env = {"KG_COLLECTION": "Cur_KnowledgeGraph"}
        orphan = "Gone_KnowledgeGraph" + suffix
        all_classes = ["Cur_KnowledgeGraph", orphan]  # base "Gone_*" absent
        result = self._run(all_classes, env)
        surfaced = [e for e in result["errors"] if orphan in e.get("error", "")]
        self.assertTrue(surfaced)
        self.assertIn("only surviving copy", surfaced[0]["error"])

    def test_no_orphans_no_extra_errors(self):
        suffix = project_init._STAGING_SUFFIX
        env = {"KG_COLLECTION": "Cur_KnowledgeGraph"}
        all_classes = ["Cur_KnowledgeGraph", "Cur_KnowledgeGraph" + suffix]
        result = self._run(all_classes, env)
        # Only the env-configured staging exists → handled by env sweep,
        # no A-10 unmatched-orphan errors.
        a10_errs = [e for e in result["errors"] if "not tied to this" in e.get("error", "")]
        self.assertEqual(a10_errs, [])

    def test_never_auto_drops(self):
        """The A-10 sweep must not call any drop/delete path on a KG-family
        orphan (KG staging may hold the only surviving copy)."""
        suffix = project_init._STAGING_SUFFIX
        env = {"KG_COLLECTION": "Cur_KnowledgeGraph"}
        orphan = "Old_KnowledgeGraph" + suffix
        all_classes = ["Cur_KnowledgeGraph", orphan]
        with mock.patch.object(project_init, "_delete_class") as del_mock:
            self._run(all_classes, env)
        del_mock.assert_not_called()


class TestCodegraphOrphanStagingSweep(unittest.TestCase):
    """Q3 (v0.2.73): code-graph ``*_Code*__staging`` orphans are reaped by the
    A-10 sweep with a COUNT-AWARE safe-drop (code graph is derived, so a
    provable orphan is self-heal — but only dropped when the base is intact and
    at least as complete)."""

    def setUp(self):
        self.args = argparse.Namespace(force_rebuild=False)

    def _run(self, all_classes, env, counts):
        """Drive migrate_collections with the code-graph orphan-staging arm
        active. ``counts`` maps class-name → object count for _count_objects.

        The current project's code-graph prefix defaults to ``Proj`` (via
        ``CODE_GRAPH_PROJECT``) so the cross-project SAFE-DROP gate treats
        ``Proj_Code*`` staging as OWNED. Pass ``CODE_GRAPH_PROJECT`` in ``env``
        to override (e.g. to exercise the foreign-project retain path)."""
        def _fake_count(name, weaviate_url=None):
            return counts.get(name, 0)

        _env = {"CODE_GRAPH_PROJECT": "Proj", **env}  # caller can override
        with mock.patch.dict("os.environ", _env, clear=False), \
             mock.patch.object(project_init, "_list_classes", return_value=all_classes), \
             mock.patch.object(
                 project_init, "_recover_or_drop_orphan_staging",
                 return_value="none",
             ), \
             mock.patch.object(project_init, "_count_objects", side_effect=_fake_count), \
             mock.patch.object(project_init, "_delete_class") as del_mock, \
             mock.patch.object(project_init, "_build_plan", return_value=[]):
            result = project_init.migrate_collections(
                self.args, dry_run=True, weaviate_url="http://localhost:8081",
            )
        return result, del_mock

    def test_safe_drops_when_base_intact_and_ge_count(self):
        """Base ``Proj_CodeFunction`` present with count >= staging → SAFE-DROP
        the staging; base is never touched."""
        suffix = project_init._STAGING_SUFFIX
        env = {"KG_COLLECTION": "Proj_KnowledgeGraph"}
        base = "Proj_CodeFunction"
        orphan = base + suffix
        all_classes = ["Proj_KnowledgeGraph", base, orphan]
        counts = {base: 23186, orphan: 15025}  # base >= staging
        result, del_mock = self._run(all_classes, env, counts)
        # The staging (and ONLY the staging) was dropped.
        dropped = [c.args[0] for c in del_mock.call_args_list]
        self.assertIn(orphan, dropped, f"staging must be safe-dropped; drops={dropped}")
        self.assertNotIn(base, dropped, "base collection must never be dropped")
        # A result entry describes the drop as resolved.
        drop_entries = [
            e for e in result["errors"]
            if orphan in e.get("error", "") and e.get("resolved") is True
        ]
        self.assertTrue(
            drop_entries,
            f"a resolved SAFE-DROP result entry must be recorded; got {result['errors']}",
        )

    def test_retains_when_base_missing(self):
        """Base MISSING → RETAIN + surface an error; nothing dropped."""
        suffix = project_init._STAGING_SUFFIX
        env = {"KG_COLLECTION": "Proj_KnowledgeGraph"}
        orphan = "Proj_CodeFunction" + suffix
        all_classes = ["Proj_KnowledgeGraph", orphan]  # base absent
        counts = {orphan: 15025}
        result, del_mock = self._run(all_classes, env, counts)
        del_mock.assert_not_called()
        surfaced = [e for e in result["errors"] if orphan in e.get("error", "")]
        self.assertTrue(surfaced, "missing-base orphan must surface as an error")
        self.assertIn("only surviving copy", surfaced[0]["error"])
        self.assertNotEqual(surfaced[0].get("resolved"), True)

    def test_retains_when_base_smaller(self):
        """Base present but with FEWER objects than staging → RETAIN + surface;
        nothing dropped (staging may be the fuller copy)."""
        suffix = project_init._STAGING_SUFFIX
        env = {"KG_COLLECTION": "Proj_KnowledgeGraph"}
        base = "Proj_CodeClass"
        orphan = base + suffix
        all_classes = ["Proj_KnowledgeGraph", base, orphan]
        counts = {base: 100, orphan: 500}  # base < staging
        result, del_mock = self._run(all_classes, env, counts)
        del_mock.assert_not_called()
        surfaced = [e for e in result["errors"] if orphan in e.get("error", "")]
        self.assertTrue(surfaced, "smaller-base orphan must surface as an error")
        self.assertIn("SMALLER", surfaced[0]["error"])
        self.assertNotEqual(surfaced[0].get("resolved"), True)

    def test_all_five_codegraph_suffixes_are_swept(self):
        """Every code-graph suffix (Module/Class/Function/API/Interaction)
        staging is recognised and safe-dropped when its base is intact."""
        suffix = project_init._STAGING_SUFFIX
        env = {"KG_COLLECTION": "Proj_KnowledgeGraph"}
        bases = [
            "Proj_CodeModule", "Proj_CodeClass", "Proj_CodeFunction",
            "Proj_CodeAPI", "Proj_CodeInteraction",
        ]
        orphans = [b + suffix for b in bases]
        all_classes = ["Proj_KnowledgeGraph", *bases, *orphans]
        counts = {b: 10 for b in bases}
        counts.update({o: 5 for o in orphans})  # base >= staging for all
        result, del_mock = self._run(all_classes, env, counts)
        dropped = {c.args[0] for c in del_mock.call_args_list}
        self.assertEqual(
            dropped, set(orphans),
            f"all five code-graph staging orphans must be safe-dropped; got {dropped}",
        )

    def test_never_drops_a_different_projects_staging(self):
        """CROSS-PROJECT SAFETY: a code-graph staging whose base belongs to a
        DIFFERENT project (not under THIS project's code-graph prefix) is NEVER
        auto-dropped, even when its base is intact and larger — that other
        project may have a rebuild in flight. It is only SURFACED for the other
        project's own migrate run to reconcile."""
        suffix = project_init._STAGING_SUFFIX
        # We are project 'Proj'; the orphan belongs to 'OtherProj'.
        env = {"KG_COLLECTION": "Proj_KnowledgeGraph", "CODE_GRAPH_PROJECT": "Proj"}
        foreign_base = "OtherProj_CodeFunction"
        foreign_orphan = foreign_base + suffix
        own_base = "Proj_CodeClass"
        own_orphan = own_base + suffix
        all_classes = [
            "Proj_KnowledgeGraph",
            foreign_base, foreign_orphan,
            own_base, own_orphan,
        ]
        # Both have base intact and >= staging (the safe-drop count condition).
        counts = {foreign_base: 100, foreign_orphan: 50,
                  own_base: 100, own_orphan: 50}
        result, del_mock = self._run(all_classes, env, counts)
        dropped = {c.args[0] for c in del_mock.call_args_list}
        # OUR staging is dropped; the FOREIGN one is NOT.
        self.assertIn(own_orphan, dropped, "own project's orphan should safe-drop")
        self.assertNotIn(
            foreign_orphan, dropped,
            "another project's code-graph staging must NEVER be auto-dropped",
        )
        # The foreign one is surfaced (retained) with the cross-project reason.
        foreign_surfaced = [
            e for e in result["errors"]
            if foreign_orphan in e.get("error", "")
        ]
        self.assertTrue(foreign_surfaced, "foreign orphan must be surfaced")
        self.assertNotEqual(foreign_surfaced[0].get("resolved"), True)
        self.assertIn("DIFFERENT project", foreign_surfaced[0]["error"])

    def test_unresolvable_prefix_never_drops(self):
        """When neither CODE_GRAPH_PROJECT nor PROJECT_NAME nor args.name
        resolves a prefix, ownership can't be proven → NEVER auto-drop, even a
        count-safe orphan (surface only). Guards the hermetic-unit-test path
        that historically reached a live DELETE."""
        suffix = project_init._STAGING_SUFFIX
        base = "Proj_CodeFunction"
        orphan = base + suffix
        all_classes = ["Proj_KnowledgeGraph", base, orphan]
        counts = {base: 100, orphan: 50}
        # Explicitly BLANK the prefix env (override the _run default) and use a
        # nameless args so nothing resolves a prefix.
        result, del_mock = self._run(
            all_classes,
            {"KG_COLLECTION": "Proj_KnowledgeGraph", "CODE_GRAPH_PROJECT": "",
             "PROJECT_NAME": ""},
            counts,
        )
        del_mock.assert_not_called()
        surfaced = [e for e in result["errors"] if orphan in e.get("error", "")]
        self.assertTrue(surfaced, "un-ownable orphan must surface, not drop")
        self.assertNotEqual(surfaced[0].get("resolved"), True)


if __name__ == "__main__":
    unittest.main()
