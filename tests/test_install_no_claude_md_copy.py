# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""PR-31 / v0.2.12 — root CLAUDE.md must NOT reach user projects.

Background
----------
Before PR-31, ``orchestrator-managed-paths.txt`` listed ``CLAUDE.md``
in the install whitelist. The launcher's ``copy_orchestrator_to_sync``
and ``install.py``'s ``apply_conflict_strategy`` both iterate that
whitelist, so a fresh install would copy the orchestrator-self's root
CLAUDE.md (47 KB of orchestrator-internals documentation: where MCP
servers live, dev-time agent conventions, KG layout, etc.) into every
downstream user project. That document is not a project scaffold —
user projects should render their CLAUDE.md from
``templates/CLAUDE.md.template`` via ``vco_lib/project_init.py`` instead.

PR-31 (v0.2.12) removed ``CLAUDE.md`` from the whitelist. These tests
pin the new behavior so a future revert can't silently re-introduce
the 47 KB drop.

What this file does NOT cover
-----------------------------
* The conflict-resolution paths (overwrite_all / overwrite_preserve /
  delete_claude_and_reinstall) are exercised by
  ``test_install_conflict_resolution.py``; they were already updated
  in PR-31 to assert CLAUDE.md stays put.
* The whitelist contents themselves are pinned by
  ``test_install_managed_paths.py`` and ``test_managed_paths_lockstep.py``.
* The project-bootstrapper's CLAUDE.md.template rendering path
  (``vco_lib/project_init.py::_PROJECT_LEVEL_TEMPLATES``) is exercised
  by ``test_install_bundle.py``.

This file's job is the negative assertion at the integration boundary:
given a source tree that has a root CLAUDE.md, the whitelist-driven
copy machinery (the SHARED code path between fresh-install and
update-install via ``apply_conflict_strategy``) MUST NOT produce a
CLAUDE.md at the target via that path.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install  # type: ignore  # noqa: E402


def _make_fake_source(root: Path) -> Path:
    """Build a fake orchestrator-self source tree containing a root
    CLAUDE.md PLUS the bits the whitelist legitimately copies.

    We use deliberately fake project names (Foo / FooBar) in the
    test fixture content so this file never references a real user
    project on disk.
    """
    src = root / "Foo-orchestrator-src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "vct-module.json").write_text("{}", encoding="utf-8")
    # The orchestrator-self's own root CLAUDE.md — the file PR-31 is
    # explicitly preventing from leaking into user projects.
    (src / "CLAUDE.md").write_text(
        "# Orchestrator-self CLAUDE.md\n\n"
        "Internal dev docs for the FooBar orchestrator clone.\n"
        "DO NOT propagate into user projects.\n",
        encoding="utf-8",
    )
    (src / ".claude").mkdir()
    (src / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (src / "knowledge").mkdir()
    (src / "knowledge" / "n.md").write_text("k", encoding="utf-8")
    return src


def _make_empty_target(root: Path) -> Path:
    tgt = root / "Acme-user-project"
    tgt.mkdir(parents=True, exist_ok=True)
    return tgt


class WhitelistDoesNotIncludeRootClaudeMd(unittest.TestCase):
    """The whitelist itself excludes CLAUDE.md. Defends the
    architectural change at the constant-definition layer."""

    def test_orchestrator_managed_paths_excludes_claude_md(self) -> None:
        self.assertNotIn(
            "CLAUDE.md",
            install.ORCHESTRATOR_MANAGED_PATHS,
            "Root CLAUDE.md must not appear in the install whitelist. "
            "It is orchestrator-self dev docs, not a per-project scaffold. "
            "User projects render CLAUDE.md from "
            "templates/CLAUDE.md.template via the project-bootstrapper.",
        )

    def test_default_preserve_list_still_includes_claude_md(self) -> None:
        """The preserve list is a SEPARATE concern from the whitelist.
        DEFAULT_PRESERVE_LIST governs which existing user files survive
        an update; it must still mention CLAUDE.md so a user-edited
        CLAUDE.md (placed there by the project-bootstrapper) survives
        a re-run of the install. The whitelist governs which files
        the orchestrator's own root tree gets to copy in the first
        place — those are independent."""
        self.assertIn(
            "CLAUDE.md",
            install.DEFAULT_PRESERVE_LIST,
            "DEFAULT_PRESERVE_LIST must still preserve a user-edited "
            "CLAUDE.md on update — that's separate from the whitelist "
            "removal PR-31 enacted.",
        )


class ApplyConflictStrategyDoesNotCopyRootClaudeMd(unittest.TestCase):
    """The shared whitelist-iterating code path
    (``apply_conflict_strategy``) is what backs both fresh-install
    and update-install copy semantics. Verify each strategy refuses
    to copy CLAUDE.md from source → target."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pr31-no-claude-md-"))
        self.source = _make_fake_source(self.tmp)
        self.target = _make_empty_target(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_overwrite_all_does_not_drop_root_claude_md(self) -> None:
        """overwrite_all is the most aggressive copy strategy — if it
        doesn't drop CLAUDE.md, neither does any other strategy."""
        report = install.apply_conflict_strategy(
            self.source, self.target, "overwrite_all", []
        )
        self.assertGreater(report["copied_count"], 0, "expected some copies")
        self.assertFalse(
            (self.target / "CLAUDE.md").exists(),
            "Root CLAUDE.md was copied from the orchestrator-self source "
            "into a fresh user project — the exact regression PR-31 was "
            "written to prevent. Check orchestrator-managed-paths.txt did "
            "not accidentally re-add 'CLAUDE.md'.",
        )

    def test_overwrite_preserve_does_not_drop_root_claude_md(self) -> None:
        """overwrite_preserve also iterates the whitelist — same
        invariant applies."""
        preserve = list(install.DEFAULT_PRESERVE_LIST)
        install.apply_conflict_strategy(
            self.source, self.target, "overwrite_preserve", preserve
        )
        self.assertFalse(
            (self.target / "CLAUDE.md").exists(),
            "overwrite_preserve dropped a CLAUDE.md into the empty user "
            "project. Check the whitelist.",
        )
        self.assertFalse(
            (self.target / "CLAUDE.new.md").exists(),
            "overwrite_preserve wrote a CLAUDE.new.md sibling. Since "
            "CLAUDE.md is no longer in the whitelist, the preserve "
            "machinery should never have visited it.",
        )

    def test_delete_claude_and_reinstall_does_not_drop_root_claude_md(self) -> None:
        """delete_claude_and_reinstall wipes .claude/ then copies the
        whitelist fresh. CLAUDE.md must still not land at the target."""
        install.apply_conflict_strategy(
            self.source, self.target, "delete_claude_and_reinstall", []
        )
        self.assertFalse(
            (self.target / "CLAUDE.md").exists(),
            "delete_claude_and_reinstall dropped a CLAUDE.md into the "
            "user project. Check the whitelist.",
        )

    def test_adopt_as_is_does_not_drop_root_claude_md(self) -> None:
        """adopt_as_is is a no-op on disk. Trivial defense — included
        for symmetry so the test class covers all 4 strategies."""
        install.apply_conflict_strategy(
            self.source, self.target, "adopt_as_is", []
        )
        self.assertFalse((self.target / "CLAUDE.md").exists())


if __name__ == "__main__":
    unittest.main()
