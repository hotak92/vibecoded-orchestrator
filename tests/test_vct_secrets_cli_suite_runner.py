# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""CI runner for the `vct` file-store CLI bash suite (v0.2.91).

``tools/vct-secrets/tests/test_vct.sh`` is a self-contained bash suite
(31 tests: store layout, scope precedence, exec injection, shape guard,
blob recovery, doctor, and the die_miss hub-probe classification). Like
``tests/test_vct_secrets_resolve.sh`` before v0.2.73 and
``tests/test_vct_project_config_extras.sh`` before v0.2.91, it had NO CI
runner — no workflow, pre-ship gate, or pytest wrapper invoked it — so
31 assertions over the secrets CLI only ran when someone remembered to
type `bash`. This shim closes that gap the same way its two siblings do.

HERMETICITY (why this runner passes an env, unlike its siblings): the
suite isolates ``VCT_SECRETS_DIR`` itself, but NOT ``VCT_STATE_DIR`` — so
its `vct get` miss-path tests would probe whatever hub happens to be
running on the developer's machine, with whatever token their shell
exported. We point ``VCT_STATE_DIR`` at an EMPTY directory and strip the
hub token/port from the child env, which makes the probe soft-fail
deterministically on every machine (with or without a live hub, with or
without a stale ``VCT_HUB_TOKEN``). The suite's own hub tests set
``VCT_STATE_DIR`` per invocation, so they are unaffected.

Skipped on Windows (no bash); on POSIX it shells out and asserts a clean
exit, surfacing the suite's own PASS/FAIL lines on failure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SUITE = REPO_ROOT / "tools" / "vct-secrets" / "tests" / "test_vct.sh"
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None,
    reason="no bash on PATH — POSIX vct CLI suite skipped (Windows host)",
)


def test_vct_cli_bash_suite_passes(tmp_path):
    assert _SUITE.is_file(), f"missing bash suite: {_SUITE}"
    env = dict(os.environ)
    empty_state = tmp_path / "empty-state"
    empty_state.mkdir()
    env["VCT_STATE_DIR"] = str(empty_state)
    env.pop("VCT_HUB_TOKEN", None)
    env.pop("VCT_HUB_PORT", None)
    env.pop("VCT_HUB_TOKEN_STRICT", None)
    proc = subprocess.run(
        [_BASH, str(_SUITE)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, (
        "vct CLI bash suite FAILED (tools/vct-secrets/tests/test_vct.sh)\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
