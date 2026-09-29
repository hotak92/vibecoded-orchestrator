# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-01 — strict X.Y.Z version ordering, Python home.

Driven by the ONE case table ``tests/fixtures/version_order_cases.json``,
which the Rust (``vct-launcher-core/src/version.rs``) and TypeScript
(``launcher/src/lib/version-compare.ts``) homes read too. Plus the
caller-side tri-state rule for the Python caller this WP owns: a version
that does not parse is reported as UNKNOWN, never ranked as fresh or stale.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from vco_lib import vscode_settings
from vco_lib.codegraph_extractor_generation import parse_semver
from vco_lib.version_compare import (
    VersionParseError,
    parse_version,
    version_cmp,
    version_ge,
    version_lt,
)

_CASES_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "version_order_cases.json"
)
CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))


class SharedTableTests(unittest.TestCase):
    def test_the_table_carries_the_owner_pairs(self):
        """The literal pairs the plan names must be IN the table, so a later
        edit cannot quietly drop the case that motivated it."""
        pairs = {(r["a"], r["b"]) for r in CASES["order"]}
        for pair in [
            ("0.2.99", "0.2.100"), ("0.2.9", "0.2.10"), ("0.2.100", "0.3.0"),
            ("0.2.100", "0.3.11"), ("0.3.11", "0.10.0"), ("1.0.0", "0.99.99"),
        ]:
            self.assertIn(pair, pairs)
        for bad in ["0.2.100-rc1", "0.2.100.dev0", "0.2.100.1", "0.2",
                    "0.2.100 ", "abc"]:
            self.assertIn(bad, CASES["reject"])

    def test_order_rows(self):
        for row in CASES["order"]:
            a, b, want = row["a"], row["b"], row["cmp"]
            with self.subTest(a=a, b=b):
                self.assertEqual(version_cmp(a, b), want)
                self.assertEqual(version_cmp(b, a), -want)
                self.assertEqual(version_lt(a, b), want < 0)
                self.assertEqual(version_ge(a, b), want >= 0)

    def test_reject_rows_raise_the_typed_error_with_the_offending_string(self):
        for bad in CASES["reject"]:
            with self.subTest(bad=bad):
                with self.assertRaises(VersionParseError) as ctx:
                    parse_version(bad)
                self.assertEqual(ctx.exception.text, bad)
                # Either side unparseable -> the comparison raises; it is
                # never answered as older, newer or equal.
                for call in (version_cmp, version_lt, version_ge):
                    with self.assertRaises(VersionParseError):
                        call(bad, "0.2.100")
                    with self.assertRaises(VersionParseError):
                        call("0.2.100", bad)

    def test_non_strings_are_unknown_not_zero(self):
        for bad in (None, 2, ["0", "2", "100"]):
            with self.subTest(bad=bad), self.assertRaises(VersionParseError):
                parse_version(bad)

    def test_the_parse_error_is_a_value_error(self):
        """Callers that already catch ``ValueError`` keep working."""
        self.assertTrue(issubclass(VersionParseError, ValueError))

    def test_neighbouring_boundary_parser_agrees_on_order(self):
        """``codegraph_extractor_generation.parse_semver`` keeps its ``None``
        contract, but where it parses it must ORDER exactly like the SSOT."""
        for row in CASES["order"]:
            a, b, want = row["a"], row["b"], row["cmp"]
            if a.startswith("v") or b.startswith("v"):
                continue  # parse_semver does not take the prefix
            pa, pb = parse_semver(a), parse_semver(b)
            assert pa is not None and pb is not None
            with self.subTest(a=a, b=b):
                self.assertEqual((pa > pb) - (pa < pb), want)
        for bad in ["0.2.100-rc1", "0.2.100.dev0", "0.2.100.1", "0.2", "abc"]:
            with self.subTest(bad=bad):
                self.assertIsNone(parse_semver(bad))


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeConn:
    """Stands in for ``http.client.HTTPConnection`` — no socket is opened."""

    body = b"{}"

    def __init__(self, *_a, **_k) -> None:
        pass

    def request(self, *_a, **_k) -> None:
        pass

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.body)

    def close(self) -> None:
        pass


def _dogfood_version_with(running: str, mine: str) -> "tuple[bool, str]":
    conn = type("Conn", (_FakeConn,), {"body": json.dumps({"version": running}).encode()})
    with mock.patch("http.client.HTTPConnection", conn), mock.patch.object(
        vscode_settings, "_this_package_version", return_value=mine
    ):
        return vscode_settings._dogfood_version(1, 0.1)


class GatewayFreshnessMappingTests(unittest.TestCase):
    """``vscode_settings._dogfood_version`` — the gateway freshness proof."""

    def test_unparseable_running_version_is_unknown_not_stale_or_fresh(self):
        ok, detail = _dogfood_version_with("0.2.100-rc1", "0.2.100")
        # The function's existing "unknown" shape (same as an absent version):
        # it does not refuse, and it says the comparison is unavailable,
        # naming the offending string. It never claims "vX" (fresh).
        self.assertTrue(ok)
        self.assertIn("unavailable", detail)
        self.assertIn("'0.2.100-rc1'", detail)
        self.assertIn("running daemon", detail)

    def test_unparseable_installed_version_is_unknown(self):
        ok, detail = _dogfood_version_with("0.2.100", "0.2.100.dev0")
        self.assertTrue(ok)
        self.assertIn("unavailable", detail)
        self.assertIn("'0.2.100.dev0'", detail)
        self.assertIn("this install", detail)

    def test_leave_alone_an_older_daemon_is_still_refused(self):
        """0.2.99 answering for a 0.2.100 install — the ordering the old
        comparator and the new one must both get right, now via the SSOT."""
        ok, detail = _dogfood_version_with("0.2.99", "0.2.100")
        self.assertFalse(ok)
        self.assertIn("older than this install", detail)

    def test_leave_alone_a_current_daemon_passes(self):
        ok, detail = _dogfood_version_with("0.2.100", "0.2.99")
        self.assertTrue(ok)
        self.assertEqual(detail, "v0.2.100")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
