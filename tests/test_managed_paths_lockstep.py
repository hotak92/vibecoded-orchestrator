"""Lockstep guard for ORCHESTRATOR_MANAGED_PATHS.

The same hard-whitelist of paths is declared twice — once in Python
(`install.py`) and once in Rust (`launcher/src-tauri/src/commands/installer.rs`).
Both consume different copy-paths into a project root, so divergence
silently produces an asymmetric install: a user could end up with
different sets of files depending on whether `install.py` or
`copy_orchestrator_to_sync` (Rust) drove the copy.

PR-1 (`#142`) trimmed the Rust list. PR-2 trims the Python mirror.
This test pins both lists in lockstep going forward.

If you intentionally diverge them (very unusual — they're meant to be
the same architectural decision rendered twice), update both this test
AND the docstrings in the two source files.
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
    INSTALLER_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs"

    def _python_set(self) -> set[str]:
        # Import directly — install.py is a module exposing the tuple.
        import install  # noqa: F401
        return set(install.ORCHESTRATOR_MANAGED_PATHS)

    def _rust_set(self) -> set[str]:
        text = self.INSTALLER_RS.read_text(encoding="utf-8")
        # Find the constant definition. The pattern is permissive enough
        # to survive `pub const ORCHESTRATOR_MANAGED_PATHS: &[&str] = &[ ... ];`
        # with comments and multi-line layout.
        m = re.search(
            r"pub\s+const\s+ORCHESTRATOR_MANAGED_PATHS\s*:\s*&\[\s*&\s*str\s*\]\s*=\s*&\[(.*?)\];",
            text,
            re.DOTALL,
        )
        if not m:
            self.fail(
                "could not locate ORCHESTRATOR_MANAGED_PATHS in installer.rs",
            )
        body = m.group(1)
        return set(re.findall(r'"([^"]+)"', body))

    def test_python_managed_paths_match_canonical_trim(self):
        """Pin the Python list to the canonical trimmed set agreed on
        2026-05-06. Rust is trimmed separately by PR-1 (#142) — once that
        lands, `test_python_and_rust_agree_on_managed_paths` (below)
        will pass automatically.
        """
        py = self._python_set()
        canonical = {
            ".claude",
            "CLAUDE.md",
            "knowledge",
            "docs",
            "tools",
            "infrastructure",
            "vct-module.json",
        }
        self.assertEqual(
            py, canonical,
            f"Python ORCHESTRATOR_MANAGED_PATHS drifted from the "
            f"canonical 7-entry trimmed set. Got: {sorted(py)}",
        )

    def test_python_and_rust_agree_on_managed_paths(self):
        """Strict lockstep guard. Will start passing once PR-1 (#142)
        merges; until then this test self-skips with a clear pointer.
        Once PR-1 lands, REMOVE the skip — the assertion is the whole
        point of the test."""
        py = self._python_set()
        rs = self._rust_set()
        if py != rs:
            only_py = py - rs
            only_rs = rs - py
            # If the only divergence is the orchestrator-only entries
            # PR-1 is removing, this is the expected pre-PR-1 state.
            pr1_pending_excludes = {
                "install.py", "install.sh", "install.ps1",
                "state", "claude_mcp_servers", "templates",
                "requirements.txt", "requirements-dev.txt",
                "BOOTSTRAP.md", "config",
            }
            if only_py == set() and only_rs.issubset(pr1_pending_excludes):
                self.skipTest(
                    "PR-1 (#142) trims these from Rust; Python is already "
                    f"trimmed by PR-2. Pending Rust removals: "
                    f"{sorted(only_rs)}. Once PR-1 lands, this test starts "
                    f"passing automatically — remove the skip when it does."
                )
            self.fail(
                f"ORCHESTRATOR_MANAGED_PATHS unexpected divergence:\n"
                f"  only in Python: {sorted(only_py)}\n"
                f"  only in Rust:   {sorted(only_rs)}\n"
                f"  Python: {sorted(py)}\n"
                f"  Rust:   {sorted(rs)}",
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
