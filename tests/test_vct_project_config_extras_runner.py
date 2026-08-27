# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""CI runner for the bash project-config suite (v0.2.91).

``tests/test_vct_project_config_extras.sh`` is a self-contained bash
suite driving ``templates/scripts/vct_project_config.sh`` against a
python ``http.server`` fake hub. Like its secrets-resolver sibling
before v0.2.73, it had NO CI runner — no workflow, pre-ship gate, or
pytest wrapper invoked it — so its assertions only ran when someone
remembered to run bash by hand. v0.2.91 added the stale-env-token
fallback cases to it, which made the gap load-bearing.

This shim is a byte-for-byte sibling of
``tests/test_vct_secrets_resolve_sh_runner.py``: it runs the suite under
``pytest tests/`` (the CI invocation) so a regression in the bash
resolver fails the gate like any other test.

Skipped on Windows (no bash); on POSIX it shells out to the suite and
asserts a clean exit. The suite prints its own per-assert PASS/FAIL
lines; on failure we surface its stdout/stderr in the assertion message.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SUITE = REPO_ROOT / "tests" / "test_vct_project_config_extras.sh"
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None,
    reason="no bash on PATH — POSIX project-config suite skipped (Windows host)",
)


def test_bash_project_config_suite_passes():
    assert _SUITE.is_file(), f"missing bash suite: {_SUITE}"
    proc = subprocess.run(
        [_BASH, str(_SUITE)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        "bash project-config suite FAILED "
        "(tests/test_vct_project_config_extras.sh)\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
