"""Tests for `_is_archived_node` status-filter coverage (2026-05-22 fix).

Background: prior to 2026-05-22, nodes with frontmatter `status: superseded`
were NOT recognised by the sync hook's exclusion filter — they were treated
as regular nodes and silently kept appearing in Weaviate / MCP query results
despite the author's clear intent. A KG-cleanup audit on that date found
three real cases (`weaviate-usage-patterns`, `VLM_Prompt_Engineering_Best_Practices_2026`,
`WD14_Tag_Rotation_Strategy`) still being indexed. The fix added
`"superseded"` to the recognised set in
`templates/scripts/sync_knowledge_graph.py::_is_archived_node`.

These tests lock in the contract so the regression cannot reappear.

Pure unit tests; no Weaviate / Ollama / network dependencies.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"


def _load_sync_module():
    """Load the script as a module without running its CLI entrypoint."""
    spec = importlib.util.spec_from_file_location(
        "_sync_knowledge_graph_under_test", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    # The script imports MCP server libs at module import; guard with stub
    # modules so the unit test runs even when those deps aren't installed.
    sys.modules.setdefault(
        "_sync_knowledge_graph_under_test", mod
    )
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py has runtime deps not installed in this "
            f"test env ({exc}); skip the import-level test."
        )
    return mod


class TestIsArchivedNodeStatusValues(unittest.TestCase):
    """Lock in which `status:` values cause sync-time exclusion."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sync_module()

    def _check(self, status_value: str, expected: bool):
        # Use a path that does NOT contain "archive/" so the path-based
        # leg doesn't shadow the frontmatter-based leg under test.
        path = Path("knowledge/concepts/some-node.md")
        archived, reason = self.mod._is_archived_node(
            path, frontmatter={"status": status_value}
        )
        self.assertEqual(
            archived,
            expected,
            f"status={status_value!r}: expected archived={expected}, got {archived} (reason: {reason!r})",
        )

    def test_archived_status_excluded(self):
        self._check("archived", True)

    def test_deprecated_status_excluded(self):
        self._check("deprecated", True)

    def test_superseded_status_excluded(self):
        """2026-05-22 regression: `superseded` must be in the exclusion set."""
        self._check("superseded", True)

    def test_active_status_kept(self):
        self._check("active", False)

    def test_idea_status_kept(self):
        self._check("idea", False)

    def test_empty_status_kept(self):
        self._check("", False)

    def test_missing_status_kept(self):
        path = Path("knowledge/concepts/some-node.md")
        archived, _ = self.mod._is_archived_node(path, frontmatter={})
        self.assertFalse(archived)

    def test_no_frontmatter_keeps_node_path_aware(self):
        """When frontmatter is None and path lacks `archive/`, node is kept."""
        path = Path("knowledge/concepts/some-node.md")
        archived, _ = self.mod._is_archived_node(path, frontmatter=None)
        self.assertFalse(archived)

    def test_status_normalised_case_insensitively(self):
        """Whitespace + case shouldn't bypass the filter."""
        path = Path("knowledge/concepts/some-node.md")
        for variant in ("ARCHIVED", " archived ", "Superseded", "DEPRECATED "):
            archived, _ = self.mod._is_archived_node(
                path, frontmatter={"status": variant}
            )
            self.assertTrue(
                archived,
                f"status={variant!r} should be normalised + excluded",
            )

    def test_path_archive_segment_still_excluded(self):
        """The path-based leg keeps working regardless of frontmatter."""
        path = Path("knowledge/archive/concepts/some-node.md")
        archived, reason = self.mod._is_archived_node(path, frontmatter=None)
        self.assertTrue(archived)
        self.assertIn("archive", reason)


if __name__ == "__main__":
    unittest.main()
