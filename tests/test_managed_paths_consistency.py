# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language consistency check for ``ORCHESTRATOR_MANAGED_PATHS``.

PR-5 (refactor: single source of truth for ORCHESTRATOR_MANAGED_PATHS)
moved the allowlist into ``orchestrator-managed-paths.txt`` at the
repo root. Both languages read the same file with the same parse
rules:

  * Rust (``launcher/src-tauri/src/commands/installer.rs``) embeds
    the file with ``include_str!`` and parses at LazyLock-init time.
  * Python (``install.py``) reads the file at module-import time and
    parses with ``_parse_managed_paths_text``.

This test forms the "consistency triangle":

      orchestrator-managed-paths.txt
              /            \
   Rust unit test    Python unit test
   (installer.rs::    (test_install_managed_paths.py::
    test_managed_     OrchestratorManagedPathsContentsTests
    paths_matches_      ::test_matches_expected_post_trim_set)
    source_of_truth)

  -- both pin against EXPECTED_MANAGED_PATHS, so if the .txt drifts
     from either language one of these tests fails first.

This file pins the third edge: the .txt file's contents (parsed by an
independent Python re-implementation of the parse rules) match the
loaded Python constant. If a future refactor changes the Python
parser without changing the .txt, this test catches it.

We do NOT spawn ``cargo`` here (slow, fragile in pytest). The Rust
side's parser is exercised by ``cargo test --lib`` against the same
EXPECTED_MANAGED_PATHS constant (see installer.rs unit tests).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install  # type: ignore  # noqa: E402

# Import the SAME expected-set the Python unit test uses, so the two
# tests stay in lockstep. The Rust test file (``installer.rs``)
# duplicates this list in its own array literal; if you change one
# you must change all three (the .txt, this Python constant, and the
# Rust array). The duplication is deliberate: each language owns its
# own assertion target so a typo in one doesn't quietly break the
# other.
from tests.test_install_managed_paths import (  # type: ignore  # noqa: E402
    EXPECTED_MANAGED_PATHS,
)


def _independent_parse(text: str) -> tuple[str, ...]:
    """Re-implementation of the parse rules, intentionally written
    NOT to call install._parse_managed_paths_text. If the production
    parser drifts from these rules, this test fails — which is the
    point. The rules are simple by design (line-trim, skip blank,
    skip ``#`` prefix, no inline comments) so the duplication is
    cheap and self-documenting."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s[0] == "#":
            continue
        out.append(s)
    return tuple(out)


class ManagedPathsConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.txt_path = REPO_ROOT / "orchestrator-managed-paths.txt"
        self.assertTrue(
            self.txt_path.is_file(),
            f"Source-of-truth file missing at {self.txt_path}",
        )
        self.text = self.txt_path.read_text(encoding="utf-8")

    def test_python_loaded_constant_matches_expected(self) -> None:
        """Python ``ORCHESTRATOR_MANAGED_PATHS`` (loaded at import
        time) equals the expected post-trim set."""
        self.assertEqual(install.ORCHESTRATOR_MANAGED_PATHS, EXPECTED_MANAGED_PATHS)

    def test_independent_parse_of_file_matches_expected(self) -> None:
        """Parsing the .txt with an independent re-implementation of
        the rules also yields the expected set. If the production
        Python parser drifts, this catches it."""
        self.assertEqual(_independent_parse(self.text), EXPECTED_MANAGED_PATHS)

    def test_python_parser_matches_independent_parse(self) -> None:
        """The production Python parser and the independent
        re-implementation agree on the same input — pinning the
        parse-rule contract."""
        self.assertEqual(
            install._parse_managed_paths_text(self.text),
            _independent_parse(self.text),
        )

    def test_rust_source_includes_repo_root_txt(self) -> None:
        """Rust must `include_str!` the same .txt file at the repo
        root. We grep the source file rather than spawning cargo —
        the Rust unit test in installer.rs is responsible for the
        actual parse correctness; here we just guard against the
        Rust side accidentally pointing at a different path or
        falling back to an inline literal."""
        installer_rs = (
            REPO_ROOT
            / "launcher"
            / "src-tauri"
            / "src"
            / "commands"
            / "installer.rs"
        )
        self.assertTrue(installer_rs.is_file(), f"Missing {installer_rs}")
        src = installer_rs.read_text(encoding="utf-8")

        # The include_str! must reference the repo-root .txt. Path is
        # 4 levels up from installer.rs (commands → src → src-tauri →
        # launcher → repo root). Be explicit so a future refactor
        # that moves installer.rs has to update the path here too.
        expected_include = (
            'include_str!("../../../../orchestrator-managed-paths.txt")'
        )
        self.assertIn(
            expected_include,
            src,
            "Rust installer.rs must embed orchestrator-managed-paths.txt "
            "via include_str! with a 4-level relative path. If installer.rs "
            "moved, update both the include_str! call and this assertion.",
        )

        # The hand-written `pub const ORCHESTRATOR_MANAGED_PATHS: &[&str] = &[`
        # array literal MUST be gone. If it comes back, we're back to two
        # copies and PR-5 was undone.
        self.assertNotIn(
            "pub const ORCHESTRATOR_MANAGED_PATHS: &[&str]",
            src,
            "Rust installer.rs has resurrected the inline array constant. "
            "ORCHESTRATOR_MANAGED_PATHS must be a LazyLock derived from "
            "the .txt — see PR-5 for rationale.",
        )

    def test_python_source_includes_loader(self) -> None:
        """Python install.py must use the file-derived loader. The
        old hand-written ``ORCHESTRATOR_MANAGED_PATHS: tuple[str, ...] = (``
        literal MUST be gone — same reason as the Rust check above."""
        install_py = REPO_ROOT / "install.py"
        src = install_py.read_text(encoding="utf-8")

        # The literal we replaced was a multi-line tuple literal. The
        # loader-based assignment is a single-line call. We check both
        # the loader is present AND the old literal pattern is gone.
        self.assertIn(
            "ORCHESTRATOR_MANAGED_PATHS: tuple[str, ...] = "
            "_load_orchestrator_managed_paths()",
            src,
            "Python install.py must define ORCHESTRATOR_MANAGED_PATHS via "
            "_load_orchestrator_managed_paths() — see PR-5.",
        )
        self.assertNotIn(
            'ORCHESTRATOR_MANAGED_PATHS: tuple[str, ...] = (\n    ".claude",',
            src,
            "Python install.py has resurrected the hand-written tuple. "
            "ORCHESTRATOR_MANAGED_PATHS must be loaded from the .txt — "
            "see PR-5 for rationale.",
        )

    def test_self_reference_present_in_file(self) -> None:
        """The .txt lists itself. Without this entry,
        update_orchestrator_at would not propagate edits to the
        list across existing installs."""
        parsed = _independent_parse(self.text)
        self.assertIn("orchestrator-managed-paths.txt", parsed)


if __name__ == "__main__":
    unittest.main()
