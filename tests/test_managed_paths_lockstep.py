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
        these would break the per-project install path.

        Note (PR-31, v0.2.12): ``CLAUDE.md`` is intentionally NOT in
        this set. The root CLAUDE.md is orchestrator-self development
        documentation; user projects render their CLAUDE.md from
        ``templates/CLAUDE.md.template`` via the project-bootstrapper,
        not via the install whitelist.

        Note (V52-C, v0.2.52): ``knowledge`` is intentionally NOT in
        this set anymore. KG nodes are USER-CURATED state. The
        orchestrator's curated KG set lives under
        ``templates/knowledge/`` and is bundle-materialized into
        ``<project>/knowledge/`` by ``_enumerate_bundle_files`` —
        manifest-tracked with the V47-A hash-compare pattern so user
        edits are preserved on update."""
        py = self._python_set()
        required = {
            ".claude",
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

    def test_managed_paths_excludes_root_claude_md(self):
        """PR-31 invariant (v0.2.12): the root CLAUDE.md must NOT be in
        the whitelist. It is orchestrator-self dev docs, not a user-
        project scaffold. A future revert that re-adds it to the .txt
        would silently drop 47 KB of orchestrator-internals
        documentation into every fresh user project — exactly the bug
        PR-31 was written to fix. User projects always render their
        CLAUDE.md from ``templates/CLAUDE.md.template`` instead."""
        py = self._python_set()
        self.assertNotIn(
            "CLAUDE.md",
            py,
            "Root CLAUDE.md must not appear in ORCHESTRATOR_MANAGED_PATHS. "
            "User projects get their CLAUDE.md from "
            "templates/CLAUDE.md.template, never from a whitelist copy of "
            "the orchestrator-self's own CLAUDE.md. See PR-31 / v0.2.12.",
        )

    def test_managed_paths_excludes_knowledge(self):
        """V52-C invariant (v0.2.52): ``knowledge`` must NOT be in the
        whitelist. KG nodes are USER-CURATED state — the directory
        mixes shipped + user-authored nodes, and copying through the
        whitelist caused modify-vs-delete merge conflicts when the
        user committed local changes and the orchestrator deleted the
        same upstream node (the v0.2.51 KG-conflict that triggered
        the V52-C architectural fix). User projects always
        bundle-materialize the orchestrator's curated KG set from
        ``templates/knowledge/`` via ``_enumerate_bundle_files``,
        never from a whitelist copy. A future revert that re-adds
        ``knowledge`` would re-introduce the conflict + risk
        wholesale overwrite of user-authored nodes."""
        py = self._python_set()
        self.assertNotIn(
            "knowledge",
            py,
            "`knowledge` must not appear in ORCHESTRATOR_MANAGED_PATHS. "
            "User projects receive shipped KG nodes via bundle "
            "materialization from `templates/knowledge/`, NOT a "
            "whitelist copy. See V52-C / v0.2.52.",
        )


if __name__ == "__main__":
    unittest.main()
