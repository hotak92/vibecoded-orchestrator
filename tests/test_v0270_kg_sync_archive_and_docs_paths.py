# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 FIX #5 + #6: archive-segment variants and dev-docs path handling.

FIX #5 — ``_is_archived_node`` matched only the exact path segment
``"archive"``. The dot/underscore archive conventions ``.archive/`` and
``_archive/`` (Obsidian hidden-folder layout; common ``_archive/`` trees)
slipped through and got indexed. The fix matches those three exact segment
forms WITHOUT over-matching legitimate dirs like ``architecture/`` or
``archived-notes/``.

FIX #6 — explicit single-file sync ran ``Path(p).resolve()`` (which rewrites
a symlink to its target) BEFORE the ``relative_to(DOCS_ROOT)`` membership
check, so a symlink physically located under ``docs/`` but pointing at content
elsewhere was rejected ("not in knowledge/ or docs/ — skipping"). The fix
classifies by the path's LOCATION (``os.path.abspath`` — no symlink
resolution of the final component) and only falls back to the resolved path.
A ``DEV_DOCS_ROOT`` env override lets projects keep docs under a non-default
folder (e.g. ``documentation/``).

Pure unit tests; the #5 leg has no external deps. The #6 leg loads the sync
module (skips cleanly if runtime deps are absent).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"


def _load_sync_module(project_root: Path, dev_docs_root: str | None = None):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
    os.environ["DEVELOPMENT_COLLECTION"] = "TestProject_Development"
    os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    if dev_docs_root is not None:
        os.environ["DEV_DOCS_ROOT"] = dev_docs_root
    else:
        os.environ.pop("DEV_DOCS_ROOT", None)

    mod_name = f"_sync_kg_paths_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py runtime deps missing ({exc}); skipping."
        )
    return mod


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
    "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER", "DEV_DOCS_ROOT",
)


class ArchiveSegmentVariantsTest(unittest.TestCase):
    """FIX #5: archive / .archive / _archive segments are excluded; lookalikes
    are not."""

    @classmethod
    def setUpClass(cls):
        os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
        cls._tmp = tempfile.TemporaryDirectory()
        cls.mod = _load_sync_module(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        cls._tmp.cleanup()

    def _archived(self, rel: str) -> bool:
        return self.mod._is_archived_node(Path(rel))[0]

    def test_plain_archive_excluded(self):
        self.assertTrue(self._archived("knowledge/archive/a.md"))

    def test_dot_archive_excluded(self):
        self.assertTrue(self._archived("knowledge/.archive/a.md"))

    def test_underscore_archive_excluded(self):
        self.assertTrue(self._archived("knowledge/_archive/a.md"))

    def test_dot_archive_in_docs_excluded(self):
        self.assertTrue(self._archived("docs/.archive/x.md"))

    def test_architecture_dir_not_excluded(self):
        """`architecture/` must NOT be treated as archived (no substring match)."""
        self.assertFalse(self._archived("knowledge/architecture/a.md"))

    def test_archived_notes_dir_not_excluded(self):
        self.assertFalse(self._archived("knowledge/archived-notes/a.md"))

    def test_normal_concept_not_excluded(self):
        self.assertFalse(self._archived("knowledge/concepts/a.md"))

    def test_reason_mentions_matched_segment(self):
        _, reason = self.mod._is_archived_node(Path("knowledge/_archive/a.md"))
        self.assertIn("_archive", reason)


class DevDocsPathClassificationTest(unittest.TestCase):
    """FIX #6: symlink-under-docs is recognised; out-of-tree is rejected;
    DEV_DOCS_ROOT overrides the docs root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "knowledge").mkdir(parents=True)

    def tearDown(self):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_symlink_under_docs_classified_as_docs(self):
        (self.root / "docs").mkdir()
        external = self.root.parent / (self.root.name + "_ext")
        external.mkdir(exist_ok=True)
        try:
            (external / "guide.md").write_text("# Guide\n")
            link = self.root / "docs" / "guide.md"
            try:
                link.symlink_to(external / "guide.md")
            except (OSError, NotImplementedError):
                self.skipTest("filesystem does not support symlinks")

            mod = _load_sync_module(self.root)
            file_path, in_knowledge, in_docs = mod._classify_sync_target(str(link))

            self.assertTrue(in_docs, "symlink under docs/ must classify as docs")
            self.assertFalse(in_knowledge)
            # Stored file_path must reflect the docs/ LOCATION, not the target.
            self.assertEqual(
                str(file_path.relative_to(mod.PROJECT_ROOT)),
                "docs/guide.md",
            )
        finally:
            import shutil
            shutil.rmtree(external, ignore_errors=True)

    def test_normal_docs_file_classified_as_docs(self):
        (self.root / "docs").mkdir()
        f = self.root / "docs" / "real.md"
        f.write_text("# Real\n")
        mod = _load_sync_module(self.root)
        file_path, in_knowledge, in_docs = mod._classify_sync_target(str(f))
        self.assertTrue(in_docs)
        self.assertFalse(in_knowledge)
        self.assertEqual(
            str(file_path.relative_to(mod.PROJECT_ROOT)), "docs/real.md"
        )

    def test_out_of_tree_file_rejected(self):
        (self.root / "docs").mkdir()
        external = self.root.parent / (self.root.name + "_outside")
        external.mkdir(exist_ok=True)
        try:
            f = external / "stray.md"
            f.write_text("# Stray\n")
            mod = _load_sync_module(self.root)
            _, in_knowledge, in_docs = mod._classify_sync_target(str(f))
            self.assertFalse(in_knowledge)
            self.assertFalse(in_docs)
        finally:
            import shutil
            shutil.rmtree(external, ignore_errors=True)

    def test_knowledge_file_classified_as_knowledge(self):
        f = self.root / "knowledge" / "concepts" / "n.md"
        f.parent.mkdir(parents=True)
        f.write_text("---\ntitle: N\n---\nBody.\n")
        mod = _load_sync_module(self.root)
        _, in_knowledge, in_docs = mod._classify_sync_target(str(f))
        self.assertTrue(in_knowledge)
        self.assertFalse(in_docs)

    def test_dev_docs_root_override_accepts_documentation_dir(self):
        (self.root / "documentation").mkdir()
        f = self.root / "documentation" / "x.md"
        f.write_text("# X\n")
        mod = _load_sync_module(self.root, dev_docs_root="documentation")
        self.assertEqual(mod.DOCS_ROOT, self.root / "documentation")
        file_path, _, in_docs = mod._classify_sync_target(str(f))
        self.assertTrue(in_docs)
        self.assertEqual(
            str(file_path.relative_to(mod.PROJECT_ROOT)), "documentation/x.md"
        )

    def test_default_docs_root_unchanged_when_env_unset(self):
        mod = _load_sync_module(self.root)
        self.assertEqual(mod.DOCS_ROOT, self.root / "docs")


if __name__ == "__main__":
    unittest.main()
