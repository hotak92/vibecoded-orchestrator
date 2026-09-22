# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One home for version comparison — the divergence, and the migration.

Three implementations of one rule, two answers. The table below is the
reason this consolidation is a BUG FIX and not housekeeping: the
digits-filtered home read ``0.2.95rc1`` as patch **951** and therefore
ranked it above ``0.2.100``.
"""
from __future__ import annotations

import unittest

from vco_lib import deferral_probes, vscode_settings
from vco_lib.version_compare import version_ge, version_parts


class LeadingDigitSemanticsTests(unittest.TestCase):
    """What the one home actually does, stated as a table."""

    CASES = [
        ("0.2.95", [0, 2, 95]),
        ("0.2.100", [0, 2, 100]),
        # The case that was wrong: a suffix must not inflate the number it
        # trails. Filtering digits out of the whole chunk gave 951.
        ("0.2.95rc1", [0, 2, 95]),
        ("0.2.96.dev0", [0, 2, 96, 0]),
        ("0.2.96+local.1", [0, 2, 96, 1]),
        # Hostile-but-real inputs: these values come from a running process,
        # a package's metadata and a file on disk. None may raise.
        ("", [0]),
        ("not-a-version", [0]),
        ("0..2", [0, 0, 2]),
    ]

    def test_the_parse_table(self):
        for text, expected in self.CASES:
            with self.subTest(version=text):
                self.assertEqual(version_parts(text), expected)

    def test_a_release_candidate_no_longer_outranks_a_later_release(self):
        """The defect, pinned at the level a user would feel it."""
        self.assertFalse(
            version_ge("0.2.95rc1", "0.2.100"),
            "a release candidate must not rank above a later release",
        )
        self.assertTrue(version_ge("0.2.100", "0.2.95rc1"))

    def test_a_suffix_compares_equal_to_its_release(self):
        """The STATED limitation, pinned so it is a decision and not a drift.

        Proper prerelease ordering (rc BELOW its release) is a semantic
        change for every caller and was deliberately deferred. Ignoring the
        suffix is still strictly better than reading it as a larger patch.
        If someone implements real ordering later, this test is the one that
        should fail and be rewritten — that is its job.
        """
        self.assertTrue(version_ge("0.2.95rc1", "0.2.95"))
        self.assertTrue(version_ge("0.2.95", "0.2.95rc1"))

    def test_shorter_versions_are_zero_padded_not_treated_as_older(self):
        self.assertTrue(version_ge("0.2", "0.2.0"))
        self.assertTrue(version_ge("0.2.0", "0.2"))
        self.assertFalse(version_ge("0.2", "0.2.1"))


class EveryCallSiteUsesTheOneHomeTests(unittest.TestCase):
    """The migration, proven by BEHAVIOUR at each call-site.

    Asserting "they all import it" would be a source scan, which this repo
    has been bitten by: a name in a comment satisfies it. These drive each
    migrated entry point and assert the shared semantics come out.
    """

    def test_deferral_probes_delegates(self):
        self.assertEqual(deferral_probes._version_parts("0.2.95rc1"), [0, 2, 95])
        self.assertFalse(deferral_probes._version_ge("0.2.95rc1", "0.2.100"))

    def test_vscode_settings_delegates(self):
        # This is the one that was wrong. Before the migration it returned
        # (0, 2, 951) and the freshness proof compared backwards.
        self.assertEqual(vscode_settings._version_tuple("0.2.95rc1"), (0, 2, 95))
        self.assertLess(
            vscode_settings._version_tuple("0.2.95rc1"),
            vscode_settings._version_tuple("0.2.100"),
        )

    def test_install_py_holds_no_private_copy_any_more(self):
        """The third home was NESTED in a function body, so it was redefined
        per call and could be neither imported nor tested. Its removal is
        asserted structurally because there is no other way to reach it."""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "install.py").read_text(
            encoding="utf-8"
        )
        names = {
            node.name
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("_vparts", names, "the nested copy is back")


class NeighbouringParsersStayDistinctTests(unittest.TestCase):
    """Two similar-looking parsers were deliberately NOT folded in.

    Collapsing concerns because their code looks alike is the mistake this
    consolidation exists to undo, so the distinction gets a test rather than
    only a docstring.
    """

    def test_strict_semver_still_rejects_what_it_is_meant_to_reject(self):
        from vco_lib.codegraph_extractor_generation import parse_semver

        self.assertEqual(parse_semver("1.2.3"), (1, 2, 3))
        self.assertIsNone(parse_semver("1.2"), "callers depend on the rejection")
        self.assertIsNone(parse_semver("1.2.3rc1"))
        # The tolerant home answers the same inputs WITHOUT rejecting, which
        # is exactly why they are different functions.
        self.assertEqual(version_parts("1.2"), [1, 2])
        self.assertEqual(version_parts("1.2.3rc1"), [1, 2, 3])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
