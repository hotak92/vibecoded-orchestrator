# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One home for version comparison — the v0.2.96 migration, still pinned.

v0.2.96 collapsed three Python implementations (two answers: the
digits-filtered one read ``0.2.95rc1`` as patch **951** and ranked it above
``0.2.100``) into :mod:`vco_lib.version_compare`. v0.2.100 (owner ruling
Q7) then replaced that home's leading-digit semantics with a STRICT
``X.Y.Z`` parser: a suffixed string is no longer ranked at all — it raises
``VersionParseError`` and each caller reports "unknown". The semantics table
now lives in ``tests/test_v02100_version_order.py`` (driven by the shared
``tests/fixtures/version_order_cases.json``); this file keeps what is still
true of the v0.2.96 migration: every call-site reaches the one home.
"""
from __future__ import annotations

import unittest

from vco_lib import deferral_probes, vscode_settings
from vco_lib.version_compare import VersionParseError, version_ge


class TheSuffixedCandidateIsNotRankedTests(unittest.TestCase):
    """The v0.2.96 defect, restated under the v0.2.100 rule."""

    def test_a_release_candidate_is_refused_not_ranked(self):
        with self.assertRaises(VersionParseError):
            version_ge("0.2.95rc1", "0.2.100")
        with self.assertRaises(VersionParseError):
            version_ge("0.2.100", "0.2.95rc1")

    def test_a_later_release_still_outranks(self):
        self.assertTrue(version_ge("0.2.100", "0.2.95"))
        self.assertFalse(version_ge("0.2.95", "0.2.100"))


class EveryCallSiteUsesTheOneHomeTests(unittest.TestCase):
    """The migration, proven by BEHAVIOUR at each call-site.

    Asserting "they all import it" would be a source scan, which this repo
    has been bitten by: a name in a comment satisfies it. These drive each
    migrated entry point and assert the shared semantics come out.
    """

    def test_deferral_probes_delegates(self):
        self.assertFalse(deferral_probes._version_ge("0.2.99", "0.2.100"))
        self.assertTrue(deferral_probes._version_ge("0.2.100", "0.2.99"))

    def test_vscode_settings_delegates(self):
        # This is the one that was wrong in v0.2.95: it returned (0, 2, 951).
        self.assertIsNone(vscode_settings._version_tuple("0.2.95rc1"))
        older = vscode_settings._version_tuple("0.2.99")
        newer = vscode_settings._version_tuple("0.2.100")
        assert older is not None and newer is not None
        self.assertLess(older, newer)

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
    """``codegraph_extractor_generation.parse_semver`` keeps its ``None``
    contract (boundary checks: unparseable == no crossing proven); the SSOT
    raises. Same acceptance on these inputs, different failure shape."""

    def test_strict_semver_still_rejects_what_it_is_meant_to_reject(self):
        from vco_lib.codegraph_extractor_generation import parse_semver
        from vco_lib.version_compare import parse_version

        self.assertEqual(parse_semver("1.2.3"), (1, 2, 3))
        self.assertIsNone(parse_semver("1.2"), "callers depend on the rejection")
        self.assertIsNone(parse_semver("1.2.3rc1"))
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        for bad in ("1.2", "1.2.3rc1"):
            with self.subTest(bad=bad), self.assertRaises(VersionParseError):
                parse_version(bad)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
