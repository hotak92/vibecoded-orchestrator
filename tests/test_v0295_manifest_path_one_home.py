# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 F9 — the bundle manifest's path has ONE spelling.

``<project>/.claude/.vco-manifest.json`` used to be written out twelve times:
three private constants that each carried a comment asking the reader to keep
them in step with the others, and nine inline ``folder / ".claude" /
".vco-manifest.json"`` constructions across ``vco_lib/`` and ``install.py``.
Nothing failed when they agreed, and nothing would have failed loudly if they
had not: a drifted spelling classifies a managed project as unmanaged, which
reads as "VCO was never installed here" rather than as a bug.

:mod:`vco_lib.manifest_paths` is now the home. This file is the ratchet that
keeps it the only one.

**A source-text gate is the right tool here, unusually.** The house rule is
that a scan over source is a weak test — a name in a comment satisfies it. That
objection does not apply when the thing under test IS the literal: there is no
runtime value a fresh hard-coded copy would differ from (it would agree,
today), so only the text can say whether a second spelling exists. The
positive-control test below is what keeps the scan honest: it proves the
pattern still MATCHES the literal in its home, so a regex that stopped matching
anything cannot pass as "no violations".

Prose is deliberately out of scope. A docstring or an error message naming
``.claude/.vco-manifest.json`` for a human is documentation; forbidding it
would push authors into unreadable f-strings for no safety gain. What is
forbidden is a string literal whose WHOLE value is that path — i.e. one being
used to build or compare a path.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from vco_lib import manifest_paths

REPO = Path(__file__).resolve().parents[1]

#: The one module allowed to spell it.
HOME = REPO / "vco_lib" / "manifest_paths.py"

#: Python trees where ``from vco_lib.manifest_paths import ...`` is reachable,
#: so a fresh literal is a choice rather than a necessity.
SCAN_ROOTS = (
    REPO / "vco_lib",
    REPO / "claude_mcp_servers",
    REPO / "templates" / "scripts",
    REPO / "install.py",
)

#: A string literal whose entire content is the manifest path, in any of the
#: forms a path construction would take: the bare basename, the
#: ``.claude/``-relative path, either separator, with or without a leading
#: anchor (``/`` for a git exclude pattern, ``./`` for a relative path).
#: Anchored to the quotes at both ends, which is what excludes prose.
LITERAL_RE = re.compile(
    r"""(['"])(?:\.?[/\\])?(?:\.claude[/\\])?\.vco-manifest\.json\1"""
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return [f for f in files if f.resolve() != HOME.resolve()]


class ManifestPathOneHomeTests(unittest.TestCase):
    def test_the_pattern_matches_the_home_itself(self) -> None:
        """Positive control: a scan that matches nothing is not a passing scan.

        If the literal is ever renamed, this fails FIRST and says so, instead
        of the ratchet below silently reporting zero violations forever.
        """
        home_src = HOME.read_text(encoding="utf-8")
        self.assertRegex(
            home_src,
            LITERAL_RE,
            "the ratchet's pattern no longer matches the literal in its own "
            "home — it would report zero violations regardless of the tree",
        )

    def test_no_second_spelling_anywhere_python_can_import_the_home(self) -> None:
        offenders: list[str] = []
        for path in _python_files():
            try:
                src = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(src.splitlines(), start=1):
                if LITERAL_RE.search(line):
                    rel = path.relative_to(REPO).as_posix()
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "a second spelling of the bundle manifest path was added. Import "
            "it instead:\n"
            "    from vco_lib.manifest_paths import MANIFEST_REL, manifest_path\n"
            "(`manifest_path(folder)` for `<folder>/.claude/.vco-manifest.json`, "
            "`MANIFEST_BASENAME` when only the file name is wanted).\n"
            + "\n".join(offenders),
        )

    def test_the_aliases_are_the_same_object_not_equal_copies(self) -> None:
        """The three former definitions are now views of one value.

        Equality would pass for two independent `Path(".claude") / ...`
        constructions; identity is what proves the second definition is gone.
        """
        from vco_lib import deferral_dismissal, project_init, project_move

        for name, value in (
            ("deferral_dismissal.MANIFEST_REL", deferral_dismissal.MANIFEST_REL),
            ("project_init._MANIFEST_REL", project_init._MANIFEST_REL),
            ("project_move._MANIFEST_REL", project_move._MANIFEST_REL),
        ):
            self.assertIs(
                value,
                manifest_paths.MANIFEST_REL,
                f"{name} is no longer an alias of the one home",
            )

    def test_the_composed_forms_agree_with_the_constant(self) -> None:
        """`manifest_path` and the POSIX string are derived, never re-typed."""
        folder = Path("/tmp/some-project")
        self.assertEqual(
            manifest_paths.manifest_path(folder),
            folder / manifest_paths.MANIFEST_REL,
        )
        self.assertEqual(
            manifest_paths.manifest_path(str(folder)),
            folder / manifest_paths.MANIFEST_REL,
            "a str folder must resolve identically to a Path one",
        )
        self.assertEqual(
            manifest_paths.MANIFEST_REL_POSIX,
            ".claude/.vco-manifest.json",
        )
        self.assertEqual(
            manifest_paths.MANIFEST_REL.name, manifest_paths.MANIFEST_BASENAME
        )

    def test_the_git_exclude_pattern_is_composed_from_the_basename(self) -> None:
        """The one caller that needs the ROOT-anchored form still gets it.

        `.git/info/exclude` patterns are root-anchored, so git_exclude reaches
        the same file by a different route — the leave-alone half of this
        migration, pinned so a future "tidy-up" cannot collapse the two.
        """
        from vco_lib import git_exclude

        self.assertEqual(
            git_exclude.VCO_EXCLUSIVE_TOPLEVEL[manifest_paths.MANIFEST_BASENAME],
            "/" + manifest_paths.MANIFEST_BASENAME,
        )
        entries = git_exclude.exclude_entries_for_created_paths(
            [], Path("/nonexistent-folder-for-this-test"), include_manifest=True
        )
        self.assertIn("/" + manifest_paths.MANIFEST_BASENAME, entries)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
