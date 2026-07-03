# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""CI runner for the bash resolver-chain suite (v0.2.73, pre-gate P5).

``tests/test_vct_secrets_resolve.sh`` is a self-contained bash test suite
(39 asserts across tiers 2 + 3 of the secrets resolver chain). Its Windows
sibling ``tests/test_vct_secrets_resolve_ps1.py`` is a pytest file and so
runs in CI (ubuntu-latest ships pwsh), but the POSIX ``.sh`` suite had NO
CI runner — no workflow, pre-ship-check, or pytest wrapper invoked it, so
its assertions only ran when someone remembered to run bash by hand. Wave 1
tripled that untested surface (the tier-2/tier-3 chain). This thin shim runs
the .sh suite under `pytest tests/` (the CI invocation) so a regression in
the bash resolver fails the gate like any other test.

Skipped on Windows (no bash); on POSIX it shells out to the suite and
asserts a clean exit. The suite prints its own per-assert PASS/FAIL lines;
on failure we surface its stdout/stderr in the assertion message.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SUITE = REPO_ROOT / "tests" / "test_vct_secrets_resolve.sh"
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None,
    reason="no bash on PATH — POSIX resolver-chain suite skipped (Windows host)",
)


def test_bash_resolver_chain_suite_passes():
    assert _SUITE.is_file(), f"missing bash suite: {_SUITE}"
    proc = subprocess.run(
        [_BASH, str(_SUITE)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "bash resolver-chain suite FAILED (tests/test_vct_secrets_resolve.sh)\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
