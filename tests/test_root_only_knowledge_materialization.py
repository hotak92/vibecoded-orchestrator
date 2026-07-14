# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.81 — root-only curated-knowledge materialization.

Focused unit tests for the gate + predicate + allowlist + Step 4d
(`materialize_root_knowledge`) that make the bundled curated KG set live ONCE
in the orchestrator root (== shared collection) instead of being copied into
every project. See PLAN-bundled-kg-root-only-2026-07-15.

T1 gate/non-root, T2 gate/root, T3 predicate canonicalization,
T4 allowlist lock, T8 Step 4d.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


def _make_fake_orch_knowledge(root: Path) -> None:
    """Minimal fake orchestrator tree with a curated + allowlisted
    knowledge subtree."""
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    kg = root / "templates" / "knowledge"
    (kg / "concepts").mkdir(parents=True)
    (kg / "tools").mkdir(parents=True)
    (kg / "concepts" / "foo.md").write_text("foo\n", encoding="utf-8")
    (kg / "concepts" / "bar.md").write_text("bar\n", encoding="utf-8")
    (kg / "tools" / "weaviate.md").write_text("wv\n", encoding="utf-8")
    # Allowlisted top-level files.
    (kg / "TAG_HIERARCHY.md").write_text("tags\n", encoding="utf-8")
    (kg / "VOCABULARY.md").write_text("vocab\n", encoding="utf-8")
    (kg / ".node_formats.json").write_text('{"v":1}\n', encoding="utf-8")
    (kg / ".node_embeddings.README.txt").write_text("readme\n", encoding="utf-8")


_CURATED = {
    "knowledge/concepts/foo.md",
    "knowledge/concepts/bar.md",
    "knowledge/tools/weaviate.md",
}
_ALLOWLISTED = {
    "knowledge/TAG_HIERARCHY.md",
    "knowledge/VOCABULARY.md",
    "knowledge/.node_formats.json",
    "knowledge/.node_embeddings.README.txt",
}


class GateNonRootTests(unittest.TestCase):
    """T1: a non-root target gets NO curated ops — only the allowlist."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-rootonly-t1-"))
        self.orch = self.tmp / "orch"
        self.orch.mkdir()
        _make_fake_orch_knowledge(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_root_excludes_curated_keeps_allowlist(self):
        other = self.tmp / "someproject"
        other.mkdir()
        ops = project_init._enumerate_bundle_files(self.orch, project_root=other)
        dests = {op.dest_rel for op in ops
                 if op.dest_rel.startswith("knowledge" + os.sep)
                 or op.dest_rel.startswith("knowledge/")}
        # No curated node ships.
        for rel in _CURATED:
            self.assertNotIn(str(Path(rel)), dests,
                             f"curated {rel} must NOT ship to a non-root target")
        # Exactly the allowlist ships.
        knowledge_dests = {d for d in dests}
        self.assertEqual(
            knowledge_dests,
            {str(Path(r)) for r in _ALLOWLISTED},
            "non-root knowledge ops must be exactly the allowlist",
        )


class GateRootTests(unittest.TestCase):
    """T2: a root target (project_root == orch AND project_root == None)
    gets the FULL curated set + allowlist."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-rootonly-t2-"))
        self.orch = self.tmp / "orch"
        self.orch.mkdir()
        _make_fake_orch_knowledge(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _knowledge_dests(self, project_root):
        ops = project_init._enumerate_bundle_files(self.orch, project_root=project_root)
        return {op.dest_rel for op in ops if op.dest_rel.startswith("knowledge")}

    def test_root_project_root_equals_orch(self):
        dests = self._knowledge_dests(self.orch)
        for rel in _CURATED | _ALLOWLISTED:
            self.assertIn(str(Path(rel)), dests, f"{rel} missing for root==orch")

    def test_root_project_root_none(self):
        dests = self._knowledge_dests(None)
        for rel in _CURATED | _ALLOWLISTED:
            self.assertIn(str(Path(rel)), dests, f"{rel} missing for project_root=None")


class PredicateCanonicalizationTests(unittest.TestCase):
    """T3: `_is_root_bundle_target` / `_canonical_path_eq` canonicalization."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-rootonly-t3-"))
        self.orch = (self.tmp / "orch").resolve()
        self.orch.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_none_is_root(self):
        self.assertTrue(project_init._is_root_bundle_target(self.orch, None))

    def test_exact_is_root(self):
        self.assertTrue(project_init._is_root_bundle_target(self.orch, self.orch))

    def test_subdirectory_is_not_root(self):
        sub = self.orch / "sub"
        sub.mkdir()
        self.assertFalse(project_init._is_root_bundle_target(self.orch, sub))

    def test_nonexistent_is_conservative_false(self):
        missing = self.orch / "does-not-exist"
        self.assertFalse(project_init._is_root_bundle_target(self.orch, missing))

    @unittest.skipUnless(os.name == "posix", "symlink test is POSIX-only")
    def test_symlink_to_root_is_root(self):
        link = self.tmp / "orch-link"
        os.symlink(self.orch, link)
        self.assertTrue(project_init._is_root_bundle_target(self.orch, link))

    def test_windows_casefold_via_injected_flag(self):
        # Same path, different case → equal on Windows, unequal on POSIX.
        a = str(self.orch).upper()
        b = str(self.orch)
        self.assertTrue(project_init._canonical_path_eq(a, b, is_windows=True))
        self.assertFalse(project_init._canonical_path_eq(a, b, is_windows=False))

    def test_resolve_error_is_false(self):
        # A NUL byte in a path raises inside resolve() → conservative False.
        bad = "\x00not-a-real-path"
        self.assertFalse(project_init._canonical_path_eq(bad, str(self.orch)))


class AllowlistLockTests(unittest.TestCase):
    """T4: `_PER_PROJECT_KNOWLEDGE_FILES` == exactly the 4 expected names.
    Guards against accidental growth (a curated node sneaking in)."""

    def test_allowlist_is_exactly_four_expected(self):
        self.assertEqual(
            set(project_init._PER_PROJECT_KNOWLEDGE_FILES),
            {
                "TAG_HIERARCHY.md",
                "VOCABULARY.md",
                ".node_formats.json",
                ".node_embeddings.README.txt",
            },
        )

    def test_top_level_only_matching(self):
        """A curated node accidentally placed at the TOP LEVEL with an
        allowlisted-looking name is still matched only by exact basename;
        a same-named file in a subdir is NOT allowlisted (depth-1 rule)."""
        tmp = Path(tempfile.mkdtemp(prefix="vct-rootonly-t4-"))
        try:
            orch = tmp / "orch"
            orch.mkdir()
            _make_fake_orch_knowledge(orch)
            # Place a decoy VOCABULARY.md inside concepts/ (subdir) — must NOT
            # be treated as allowlisted for a non-root target.
            (orch / "templates" / "knowledge" / "concepts" / "VOCABULARY.md") \
                .write_text("decoy\n", encoding="utf-8")
            ops = project_init._enumerate_knowledge_ops(orch, include_curated=False)
            dests = {op.dest_rel for op in ops}
            self.assertIn(str(Path("knowledge/VOCABULARY.md")), dests)
            self.assertNotIn(str(Path("knowledge/concepts/VOCABULARY.md")), dests)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class Step4cMaterializeTests(unittest.TestCase):
    """T8: `materialize_root_knowledge` — fresh, idempotent, preserve-edit,
    missing-templates."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-rootonly-t8-"))
        self.orch = self.tmp / "orch"
        self.orch.mkdir()
        _make_fake_orch_knowledge(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_installs_full_set(self):
        r = project_init.materialize_root_knowledge(self.orch)
        # 3 curated + 4 allowlisted = 7 files.
        self.assertEqual(r["installed"], 7, r)
        self.assertEqual(r["skipped"], 0)
        self.assertEqual(r["errors"], [])
        for rel in _CURATED | _ALLOWLISTED:
            self.assertTrue((self.orch / Path(rel)).exists(), f"{rel} not on disk")

    def test_rerun_is_idempotent_skip_existing(self):
        project_init.materialize_root_knowledge(self.orch)
        r2 = project_init.materialize_root_knowledge(self.orch)
        self.assertEqual(r2["installed"], 0)
        self.assertEqual(r2["skipped"], 7)
        self.assertEqual(r2["errors"], [])

    def test_user_edited_root_node_preserved(self):
        project_init.materialize_root_knowledge(self.orch)
        edited = self.orch / "knowledge" / "concepts" / "foo.md"
        edited.write_text("USER EDIT\n", encoding="utf-8")
        r = project_init.materialize_root_knowledge(self.orch)
        self.assertEqual(edited.read_text(encoding="utf-8"), "USER EDIT\n")
        self.assertGreaterEqual(r["skipped"], 1)

    def test_missing_templates_knowledge_returns_zeros(self):
        empty = self.tmp / "empty-root"
        empty.mkdir()
        (empty / "vct-module.json").write_text("{}\n", encoding="utf-8")
        r = project_init.materialize_root_knowledge(empty)
        self.assertEqual(
            r, {"installed": 0, "skipped": 0, "errors": [], "symlink_redirects": 0},
        )

    def test_log_event_callback_invoked(self):
        events = []

        def _log(step, phase, detail, data=None):
            events.append((step, phase))

        project_init.materialize_root_knowledge(self.orch, log_event=_log)
        # At least one "4d/10" event fired (the root knowledge-materialization
        # step; 4c is the pre-existing CLAUDE.md-from-template step).
        self.assertTrue(any(s == "4d/10" for s, _ in events), events)


class Step4dSymlinkRedirectTests(unittest.TestCase):
    """v0.2.81 write-consolidation: Step 4d routes through _write_file_atomic.
    Locks the redirect count conventions AND the intermediate-ancestor gap fix."""

    def setUp(self):
        # .resolve() so the ONLY symlinks in the chain are the ones each
        # test creates (a symlinked TMPDIR ancestor — e.g. macOS /var —
        # would otherwise add spurious per-file redirects and break the
        # exact-count assertions).
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-rootonly-4d-")).resolve()
        self.orch = self.tmp / "orch"
        self.orch.mkdir()
        _make_fake_orch_knowledge(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(os.name == "posix", "symlink test is POSIX-only")
    def test_intermediate_dir_symlink_redirected_not_written_through(self):
        """THE GAP: a symlinked intermediate dir (knowledge/concepts →
        elsewhere) must be redirected to a `.vco-new` sibling, never
        written THROUGH into the foreign target (V47-B). The old inline
        copyfile loop only guarded the per-file dest, not ancestors."""
        (self.orch / "knowledge").mkdir()
        external = self.tmp / "external-concepts"
        external.mkdir()
        os.symlink(external, self.orch / "knowledge" / "concepts")

        r = project_init.materialize_root_knowledge(self.orch)

        # Foreign target NOT written through.
        self.assertEqual(list(external.iterdir()), [],
                         "wrote THROUGH the symlinked intermediate dir")
        # Redirect landed at the `.vco-new` sibling of the symlinked dir.
        redirected = self.orch / "knowledge" / "concepts.vco-new"
        self.assertEqual((redirected / "foo.md").read_text(encoding="utf-8"),
                         "foo\n")
        self.assertEqual((redirected / "bar.md").read_text(encoding="utf-8"),
                         "bar\n")
        # The symlink itself survives untouched.
        self.assertTrue((self.orch / "knowledge" / "concepts").is_symlink())
        # Counts: 2 redirected + 5 normal; a redirect counts as installed.
        self.assertEqual(r["installed"], 7, r)
        self.assertEqual(r["skipped"], 0, r)
        self.assertEqual(r["symlink_redirects"], 2, r)
        self.assertEqual(r["errors"], [])
        # Atomicity-litter check: no mkstemp tempfiles left behind.
        self.assertEqual(list((self.orch / "knowledge").rglob(".*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "symlink test is POSIX-only")
    def test_per_file_symlink_dest_redirects_and_counts_installed(self):
        """Lock the pre-existing convention: a symlinked per-file dest is
        NOT written through; content lands at `<dest>.vco-new` and the
        redirect counts as BOTH symlink_redirects AND installed."""
        (self.orch / "knowledge" / "concepts").mkdir(parents=True)
        ext_file = self.tmp / "target.md"
        ext_file.write_text("ORIGINAL\n", encoding="utf-8")
        os.symlink(ext_file, self.orch / "knowledge" / "concepts" / "foo.md")

        r = project_init.materialize_root_knowledge(self.orch)

        # Symlink target never written through; symlink survives.
        self.assertEqual(ext_file.read_text(encoding="utf-8"), "ORIGINAL\n")
        self.assertTrue(
            (self.orch / "knowledge" / "concepts" / "foo.md").is_symlink())
        self.assertEqual(
            (self.orch / "knowledge" / "concepts" / "foo.md.vco-new")
            .read_text(encoding="utf-8"),
            "foo\n")
        # THE convention being locked: redirect counts as installed too.
        self.assertEqual(r["installed"], 7, r)
        self.assertEqual(r["skipped"], 0, r)
        self.assertEqual(r["symlink_redirects"], 1, r)
        self.assertEqual(r["errors"], [])

    @unittest.skipUnless(os.name == "posix", "symlink test is POSIX-only")
    def test_top_level_knowledge_symlink_rebases_subtree_once(self):
        """Lock the §3 keep decision: a symlinked top-level knowledge/ is
        rebased ONCE onto knowledge.vco-new/ (1 subtree redirect event,
        not 7 per-file events) and run 2 skips through the sibling."""
        external = self.tmp / "external-kg"
        external.mkdir()
        os.symlink(external, self.orch / "knowledge")

        r1 = project_init.materialize_root_knowledge(self.orch)
        self.assertEqual(r1["symlink_redirects"], 1, r1)  # ONE event, not 7
        self.assertEqual(r1["installed"], 7, r1)
        self.assertEqual(r1["skipped"], 0, r1)
        self.assertEqual(r1["errors"], [])
        sibling = self.orch / "knowledge.vco-new"
        for rel in _CURATED | _ALLOWLISTED:
            below = Path(rel).relative_to("knowledge")
            self.assertTrue((sibling / below).exists(),
                            f"{rel} not under knowledge.vco-new/")
        self.assertEqual(list(external.iterdir()), [],
                         "wrote THROUGH the symlinked knowledge/ dir")
        self.assertTrue((self.orch / "knowledge").is_symlink())

        # Run 2: idempotent THROUGH the sibling (lexists sees its files).
        r2 = project_init.materialize_root_knowledge(self.orch)
        self.assertEqual(r2["installed"], 0, r2)
        self.assertEqual(r2["skipped"], 7, r2)
        self.assertEqual(r2["symlink_redirects"], 1, r2)
        self.assertEqual(r2["errors"], [])


if __name__ == "__main__":
    unittest.main()
