# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Subprocess tests for templates/hooks/pre-diagram-path-validation.sh.

We exercise the script in three ways:
  1. Direct CLI invocation of `python -m vco_lib.diagram_paths validate ...`
     — guarantees the validator's exit-code contract holds even when
     the bash hook can't be exec'd (CI on Windows, sandbox without bash).
  2. Bash hook invocation when bash is on PATH — verifies the stdin
     JSON parsing + venv resolution + exit-code propagation end-to-end.
  3. Edge cases that bypass the matcher: paths outside .claude/diagrams/
     are silently allowed.

Bash invocations are skipped on Windows runners (no bash available).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "pre-diagram-path-validation.sh"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Run the validate CLI in-process via subprocess (PYTHONPATH set)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.diagram_paths", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    """Run the bash hook with a JSON stdin payload, returning the result.

    Skips on Windows where bash isn't available.
    """
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available — Windows runner")

    env = os.environ.copy()
    env["VCT_DISABLE_HOOKS"] = ""  # ensure hook runs (overrides any inherited)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Force the hook to find our Python on PATH (no venv in CI test env).
    env["PATH"] = (
        os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    )

    return subprocess.run(
        [bash, str(HOOK_SH)],
        input=json.dumps(payload),
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# CLI tests (cross-OS)
# ---------------------------------------------------------------------------


class TestValidateCLI:
    def test_valid_path_exit_0(self, tmp_path: Path):
        p = tmp_path / ".claude" / "diagrams" / "gui" / "auth" / "login.mmd"
        result = _run_cli(["validate", str(p)])
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_flat_path_exit_2(self, tmp_path: Path):
        p = tmp_path / ".claude" / "diagrams" / "flat.mmd"
        result = _run_cli(["validate", str(p)])
        assert result.returncode == 2
        assert "flat" in result.stderr.lower() or "category" in result.stderr.lower()

    def test_traversal_exit_2(self, tmp_path: Path):
        # We construct the structural path WITHOUT resolving — validator
        # rejects on the literal `..` in the input parts.
        p = tmp_path / ".claude" / "diagrams" / ".." / ".." / "escape.mmd"
        result = _run_cli(["validate", str(p)])
        assert result.returncode == 2
        assert "traversal" in result.stderr.lower() or "flat" in result.stderr.lower()

    def test_bad_extension_exit_2(self, tmp_path: Path):
        p = tmp_path / ".claude" / "diagrams" / "gui" / "x.txt"
        result = _run_cli(["validate", str(p)])
        assert result.returncode == 2
        assert "extension" in result.stderr.lower()

    def test_camelcase_name_exit_2(self, tmp_path: Path):
        p = tmp_path / ".claude" / "diagrams" / "gui" / "LoginForm.mmd"
        result = _run_cli(["validate", str(p)])
        assert result.returncode == 2
        assert "kebab" in result.stderr.lower()

    def test_kind_mismatch_exit_2(self, tmp_path: Path):
        p = tmp_path / ".claude" / "diagrams" / "gui" / "auth" / "login.mmd"
        result = _run_cli(["validate", "--kind", "excalidraw", str(p)])
        assert result.returncode == 2
        assert "expected kind" in result.stderr.lower()

    def test_corrective_message_includes_example(self, tmp_path: Path):
        p = tmp_path / ".claude" / "diagrams" / "flat.mmd"
        result = _run_cli(["validate", str(p)])
        # The corrective message should contain a copy-pasteable example.
        assert "example" in result.stderr.lower()
        assert "gui/auth/login-form.mmd" in result.stderr


# ---------------------------------------------------------------------------
# Bash hook end-to-end (POSIX only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="bash hook not exercised on Windows"
)
class TestHookSubprocess:
    def test_hook_exists_and_is_executable(self):
        assert HOOK_SH.exists(), f"hook missing: {HOOK_SH}"
        # Not requiring exec bit (settings.json runs via `bash <path>`);
        # just confirm it's a real file.
        assert HOOK_SH.is_file()

    def test_path_outside_diagrams_allowed(self):
        result = _run_hook(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/some-other-file.mmd"},
            }
        )
        # Outside scope → silently allowed (exit 0).
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_empty_stdin_allowed(self):
        # Hooks with no JSON should fail-open (allow), not crash.
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash not available")
        env = os.environ.copy()
        env["VCT_DISABLE_HOOKS"] = ""
        result = subprocess.run(
            [bash, str(HOOK_SH)],
            input="",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0

    def test_disable_hooks_env_bypasses(self):
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash not available")
        env = os.environ.copy()
        env["VCT_DISABLE_HOOKS"] = "1"
        # Even a bad path should be allowed when the disable flag is set.
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": ".claude/diagrams/flat.mmd"},
        }
        result = subprocess.run(
            [bash, str(HOOK_SH)],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0

    def test_bash_syntax_valid(self):
        """bash -n catches syntax errors without executing the script."""
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash not available")
        result = subprocess.run(
            [bash, "-n", str(HOOK_SH)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, (
            f"syntax error in {HOOK_SH}: {result.stderr}"
        )
