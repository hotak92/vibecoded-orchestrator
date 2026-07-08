# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""F-9 (v0.2.75): vct_retrieval_tuning_set.ps1 emits the REAL exit-code matrix.

The ps1 header promised exit 4 (unknown field) / 64 (usage) "same exit
codes" as the bash sibling, but the body declared ``[ValidateSet]`` +
``Mandatory`` on -Field/-Value — so PowerShell's PARAMETER BINDER turned
both cases into a terminating error (exit 1), never the advertised 4 / 64.

F-9 replaces the binding with explicit checks so the ps1 emits the SAME
codes as ``vct_retrieval_tuning_set.sh`` (which really does: :304 exit 4,
six exit-64 sites). This pins the exit-code matrix for both siblings.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PS1 = REPO_ROOT / "templates" / "scripts" / "vct_retrieval_tuning_set.ps1"
SH = REPO_ROOT / "templates" / "scripts" / "vct_retrieval_tuning_set.sh"
_PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _run_sh(args: list[str], state_dir: str) -> int:
    env = dict(os.environ)
    env["VCT_STATE_DIR"] = state_dir
    return subprocess.run(
        ["bash", str(SH), *args], env=env,
        capture_output=True, text=True, timeout=15,
    ).returncode


@unittest.skipIf(_PWSH is None, "no PowerShell runtime on PATH")
class Ps1ExitCodeMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self._state = tempfile.mkdtemp(prefix="vct-f9-")

    def tearDown(self) -> None:
        shutil.rmtree(self._state, ignore_errors=True)

    def _run_ps(self, args: list[str]) -> int:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self._state,
            "VCT_STATE_DIR": self._state,
        }
        return subprocess.run(
            [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(PS1), *args],
            env=env, capture_output=True, text=True, timeout=30,
        ).returncode

    def test_unknown_field_exits_4(self):
        self.assertEqual(
            self._run_ps(["-Field", "not_a_real_field", "-Value", "0.5"]), 4,
            "unknown field must exit 4 (was binder exit 1 pre-F-9)",
        )

    def test_missing_value_exits_64(self):
        self.assertEqual(
            self._run_ps(["-Field", "kg_tier_min"]), 64,
            "missing -Value must exit 64 (usage), not binder exit 1",
        )

    def test_non_numeric_value_exits_64(self):
        self.assertEqual(
            self._run_ps(["-Field", "kg_tier_min", "-Value", "notanumber"]), 64,
            "non-numeric -Value must exit 64 (usage)",
        )

    def test_valid_field_value_exits_0(self):
        self.assertEqual(
            self._run_ps(["-Field", "kg_tier_min", "-Value", "0.40"]), 0,
        )

    def test_reset_exits_0(self):
        self.assertEqual(self._run_ps(["-Reset"]), 0)

    def test_out_of_range_value_exits_2(self):
        # 1.5 > 1.0 → validation failure (out of [0,1]).
        self.assertEqual(
            self._run_ps(["-Field", "kg_tier_min", "-Value", "1.5"]), 2,
        )

    def test_parity_with_bash_unknown_field(self):
        """The .sh sibling really emits 4 for an unknown field — parity."""
        self.assertEqual(
            _run_sh(["--field", "not_a_real_field", "--value", "0.5"], self._state),
            4,
        )
        self.assertEqual(self._run_ps(["-Field", "not_a_real_field", "-Value", "0.5"]), 4)


class HeaderContractHonestyTest(unittest.TestCase):
    """The F-9 decision is noted in BOTH siblings (no OS runtime needed)."""

    def test_both_siblings_note_f9_decision(self):
        ps1 = PS1.read_text(encoding="utf-8")
        sh = SH.read_text(encoding="utf-8")
        self.assertIn("F-9", ps1)
        self.assertIn("F-9", sh)
        # The ps1 must no longer use ValidateSet/Mandatory to gate -Field.
        self.assertNotIn("[ValidateSet(", ps1,
                         "F-9 must remove [ValidateSet] so exit 4/64 can fire")


if __name__ == "__main__":
    unittest.main()
