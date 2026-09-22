# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 (ship-gate MINOR-2, register 14(b)): the doctor's Claude Code
trust-flag probe. The 2026-09-20 storm's engine was ~/.claude.json's
hasTrustDialogAccepted flipping False — every headless claude -p then
failed "not been trusted" and fired the StopFailure hook 304 times. The
probe names the state with its one-step recovery."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vco_lib import doctor  # noqa: E402

NO_RES = None  # the probe reads global state only; res is unused


def _run(folder: Path):
    return doctor.probe_claude_code_trust(folder, NO_RES, {})


class ClaudeCodeTrustProbeTests(unittest.TestCase):
    def _home_with(self, tmp: Path, payload) -> Path:
        fake = tmp / "home"
        fake.mkdir()
        (fake / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
        return fake

    def test_false_flag_is_a_problem_naming_the_recovery(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            folder = tmp / "proj"
            home = self._home_with(tmp, {"projects": {str(folder): {
                "hasTrustDialogAccepted": False}}})
            with mock.patch("vco_lib.paths.user_home", return_value=home):
                findings = _run(folder)
        self.assertEqual(findings[0].status, doctor.STATUS_PROBLEM)
        self.assertIn("trust flag is False", findings[0].summary)
        self.assertIn("accept", findings[0].detail["recovery"])

    def test_true_flag_is_ok(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            folder = tmp / "proj"
            home = self._home_with(tmp, {"projects": {str(folder): {
                "hasTrustDialogAccepted": True}}})
            with mock.patch("vco_lib.paths.user_home", return_value=home):
                findings = _run(folder)
        self.assertEqual(findings[0].status, doctor.STATUS_OK)

    def test_missing_entry_is_ok_not_a_verdict(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            home = self._home_with(tmp, {"projects": {}})
            with mock.patch("vco_lib.paths.user_home", return_value=home):
                findings = _run(tmp / "proj")
        self.assertEqual(findings[0].status, doctor.STATUS_OK)
        self.assertIn("no Claude Code trust entry", findings[0].summary)

    def test_corrupt_file_is_unknown_not_ok(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            fake = tmp / "home"
            fake.mkdir()
            (fake / ".claude.json").write_text("{not json", encoding="utf-8")
            with mock.patch("vco_lib.paths.user_home", return_value=fake):
                findings = _run(tmp / "proj")
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)

    def test_registered_full_scope(self):
        self.assertIn("claude_code_trust", doctor.PROBES)
        self.assertIn(doctor.SCOPE_FULL, doctor.PROBES["claude_code_trust"][1])


if __name__ == "__main__":
    unittest.main()
