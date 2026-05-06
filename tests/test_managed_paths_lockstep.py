"""Lockstep guard for ORCHESTRATOR_MANAGED_PATHS.

PR-5 (`#144`) makes `orchestrator-managed-paths.txt` at the repo root the
single source of truth — both Rust (`installer.rs` via `include_str!`)
and Python (`install.py` at runtime) read it. This test pins the Python
side to the .txt file's parsed contents so divergence is structurally
detectable.

Cross-language consistency (Rust vs Python vs .txt) is covered by
`tests/test_managed_paths_consistency.py`; this file's job is to verify
the Python-side load and to assert the architectural-intent guard
(orchestrator-only entries must never appear).
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class ManagedPathsLockstepTests(unittest.TestCase):
    SOURCE_FILE = REPO_ROOT / "orchestrator-managed-paths.txt"

    def _python_set(self) -> set[str]:
        # Import directly — install.py is a module exposing the tuple
        # (loaded from orchestrator-managed-paths.txt at import time).
        import install  # noqa: F401
        return set(install.ORCHESTRATOR_MANAGED_PATHS)

    def _txt_set(self) -> set[str]:
        # Independent re-parse of the .txt source-of-truth.
        text = self.SOURCE_FILE.read_text(encoding="utf-8")
        out: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            out.add(stripped)
        return out

    def test_python_managed_paths_match_source_of_truth(self):
        """Pin the Python `install.ORCHESTRATOR_MANAGED_PATHS` to the
        contents of `orchestrator-managed-paths.txt`. Cross-language
        consistency (Rust vs Python vs .txt) is covered separately by
        `tests/test_managed_paths_consistency.py`.
        """
        py = self._python_set()
        txt = self._txt_set()
        self.assertEqual(
            py, txt,
            f"Python ORCHESTRATOR_MANAGED_PATHS drifted from "
            f"orchestrator-managed-paths.txt. "
            f"Python: {sorted(py)}; .txt: {sorted(txt)}",
        )

    def test_managed_paths_excludes_orchestrator_only_entries(self):
        """PR-1 / PR-2 architectural decision: these entries must NEVER
        appear in the managed-paths set, because they are orchestrator-
        only files that broke the VideoFrames install when copied into
        a user project. Pinning them here ensures a future revert
        cannot silently re-introduce the bug.
        """
        py = self._python_set()
        forbidden = {
            "install.py",
            "install.sh",
            "install.ps1",
            "state",
            "claude_mcp_servers",
            "templates",
            "requirements.txt",
            "requirements-dev.txt",
            "BOOTSTRAP.md",
            "config",
        }
        leaked = py & forbidden
        self.assertEqual(
            leaked, set(),
            f"orchestrator-only entries leaked back into Python "
            f"ORCHESTRATOR_MANAGED_PATHS: {sorted(leaked)}. See PR-1 / PR-2 "
            f"architectural intent in install.py docstring.",
        )

    def test_managed_paths_includes_required_entries(self):
        """The minimum viable set: project-meaningful configuration that
        legitimately lives alongside a user project. Removing any of
        these would break the per-project install path."""
        py = self._python_set()
        required = {
            ".claude",
            "CLAUDE.md",
            "knowledge",
            "docs",
            "tools",
            "infrastructure",
            "vct-module.json",
        }
        missing = required - py
        self.assertEqual(
            missing, set(),
            f"required entries missing from Python "
            f"ORCHESTRATOR_MANAGED_PATHS: {sorted(missing)}.",
        )


if __name__ == "__main__":
    unittest.main()
