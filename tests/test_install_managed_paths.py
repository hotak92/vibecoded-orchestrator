# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for the source-of-truth allowlist loader in ``install.py``.

PR-5 (refactor: single source of truth for ORCHESTRATOR_MANAGED_PATHS)
replaced the hand-written tuple in ``install.py`` with a loader that
parses ``orchestrator-managed-paths.txt`` at the repo root. These
tests pin:

  * The parse rules (lines stripped, blank lines and ``#``-prefixed
    lines skipped, order preserved, no inline comments).
  * The exact post-trim entry set the file is expected to contain
    (catches accidental edits to the .txt that would change install
    behavior; intentional edits update the .txt AND this assertion
    in the same commit).
  * The self-reference invariant (``orchestrator-managed-paths.txt``
    lists itself so ``update_orchestrator_at`` propagates new
    editions of the list across existing installs).
  * Fatal-error behavior when the .txt file is missing — silently
    falling back to a default would re-introduce the drift bug
    PR-5 was written to fix.

The cross-language consistency check (Python ↔ Rust ↔ .txt) lives in
``tests/test_managed_paths_consistency.py``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


# Expected post-trim contents of orchestrator-managed-paths.txt as of
# PR-1 (which trimmed the Rust constant) plus PR-5's self-reference
# entry. If you change the file, change this list in the same commit.
EXPECTED_MANAGED_PATHS: tuple[str, ...] = (
    ".claude",
    "CLAUDE.md",
    "knowledge",
    "docs",
    "tools",
    "infrastructure",
    "vct-module.json",
    "orchestrator-managed-paths.txt",
)


class ParseManagedPathsTextTests(unittest.TestCase):
    """Parse rules for ``_parse_managed_paths_text``. The Rust side
    (``parse_managed_paths_text`` in ``installer.rs``) implements the
    same rules — see ``test_managed_paths_consistency.py`` for the
    cross-language pin."""

    def test_strips_whitespace_and_skips_blank_and_comment_lines(self) -> None:
        sample = (
            "# leading comment\n"
            "# another comment\n"
            ".claude\n"
            "\n"
            "CLAUDE.md\n"
            "   docs\n"
            "\t# indented comment\n"
            "infrastructure\n"
        )
        self.assertEqual(
            install._parse_managed_paths_text(sample),
            (".claude", "CLAUDE.md", "docs", "infrastructure"),
        )

    def test_preserves_order(self) -> None:
        sample = "z\na\nm\n"
        self.assertEqual(install._parse_managed_paths_text(sample), ("z", "a", "m"))

    def test_no_inline_comments(self) -> None:
        # `#` is only a line prefix — anything after a path on the same
        # line stays part of the path. We don't expect anyone to actually
        # do this; the test pins behavior so the rule can't drift across
        # languages.
        sample = ".claude  # not a comment\n"
        self.assertEqual(
            install._parse_managed_paths_text(sample),
            (".claude  # not a comment",),
        )

    def test_empty_input_yields_empty_tuple(self) -> None:
        self.assertEqual(install._parse_managed_paths_text(""), ())

    def test_only_comments_yields_empty_tuple(self) -> None:
        self.assertEqual(
            install._parse_managed_paths_text("# a\n# b\n#c\n   # d\n"), ()
        )

    def test_strips_utf8_bom_from_first_line(self) -> None:
        # Saved-from-Windows-Notepad files routinely carry a UTF-8 BOM
        # (\\ufeff) at the start. str.strip() does NOT remove it. Without
        # explicit handling, the first allowlist entry silently fails to
        # match — a real bug raised by the PR-5 reviewer 2026-05-06.
        sample = "﻿.claude\nCLAUDE.md\n"
        self.assertEqual(
            install._parse_managed_paths_text(sample),
            (".claude", "CLAUDE.md"),
        )

    def test_bom_only_stripped_from_start_not_inside_lines(self) -> None:
        # Defensive: a stray BOM mid-content (not at file start) is
        # still treated as part of the line. Real files won't have one;
        # the test pins the conservative behavior.
        sample = ".claude\n﻿CLAUDE.md\n"
        self.assertEqual(
            install._parse_managed_paths_text(sample),
            (".claude", "﻿CLAUDE.md"),
        )

    def test_returns_tuple_not_list(self) -> None:
        # Callers (e.g. iteration in copy_orchestrator_to_sync) expect
        # an immutable allowlist. A tuple makes accidental mutation a
        # type error.
        result = install._parse_managed_paths_text(".claude\n")
        self.assertIsInstance(result, tuple)


class OrchestratorManagedPathsContentsTests(unittest.TestCase):
    """The loaded constant must equal the expected post-trim set
    AND satisfy the invariants enforced by PR-1 (banned entries) and
    PR-5 (self-reference)."""

    def test_matches_expected_post_trim_set(self) -> None:
        self.assertEqual(install.ORCHESTRATOR_MANAGED_PATHS, EXPECTED_MANAGED_PATHS)

    def test_excludes_orchestrator_only_machinery(self) -> None:
        """PR-1 invariant: orchestrator-only entry points and
        per-install metadata must never reach a per-project folder via
        ``copy_orchestrator_to_sync``. The VideoFrames over-copy bug
        traced to ``install.py`` and ``state/`` being in this list."""
        banned = (
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
        )
        for entry in banned:
            self.assertNotIn(
                entry,
                install.ORCHESTRATOR_MANAGED_PATHS,
                f"ORCHESTRATOR_MANAGED_PATHS must not contain "
                f"orchestrator-only entry {entry!r}",
            )

    def test_self_reference_present(self) -> None:
        """PR-5 invariant: the .txt file lists itself so
        ``update_orchestrator_at`` syncs a freshly-edited version
        into every existing install. Dropping this entry would
        silently freeze the whitelist for any user who installed
        before the next launcher release."""
        self.assertIn(
            "orchestrator-managed-paths.txt",
            install.ORCHESTRATOR_MANAGED_PATHS,
        )

    def test_source_file_resolves_to_repo_root(self) -> None:
        """The loader resolves the .txt as a sibling of install.py.
        If install.py moves, the loader fails fast at import time
        (covered by the fatal-error test below) instead of silently
        loading the wrong list."""
        expected = (
            Path(install.__file__).resolve().parent
            / "orchestrator-managed-paths.txt"
        )
        self.assertEqual(install._MANAGED_PATHS_FILE, expected)
        self.assertTrue(install._MANAGED_PATHS_FILE.is_file())


class LoaderFatalErrorTests(unittest.TestCase):
    """If the .txt file is missing or unreadable, the loader must
    raise — not silently return a default. PR-5 was written to fix
    a drift bug; falling back to a hard-coded list would re-introduce
    the same class of bug."""

    def test_missing_file_raises_runtime_error(self) -> None:
        # Call the underlying loader directly with the file path
        # patched to a non-existent location. We don't reload the
        # module (that would lock in a different state for downstream
        # tests); we just call the parse helper after a manual read
        # that mirrors what _load_orchestrator_managed_paths does.
        bogus = Path("/nonexistent/orchestrator-managed-paths.txt")
        original = install._MANAGED_PATHS_FILE
        try:
            install._MANAGED_PATHS_FILE = bogus
            with self.assertRaises(RuntimeError) as ctx:
                install._load_orchestrator_managed_paths()
            # Error message must mention the missing file path so the
            # user can recover.
            self.assertIn(str(bogus), str(ctx.exception))
        finally:
            install._MANAGED_PATHS_FILE = original


if __name__ == "__main__":
    unittest.main()
