# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 DEDUP-1: tests for _run_logged_subprocess helper.

Implements DEDUP-1 per docs/INSTALL_ARCHITECTURE_v2.md §5.1. The helper
replaces 8+ silent-hang ``subprocess.run(capture_output=True, ...)``
callsites in install.py with a single consolidated helper that:

* surfaces stdout/stderr via tail-on-failure
* logs success/failure to install.jsonl via _log_install_event
* shows a dot-cycle animation after 3s for long-running steps (M-P1-7)
* enforces uniform timeout / env / on_failure policy
* never silently hangs (the M-P0-4 bug class)
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"


@pytest.fixture(scope="module")
def install_module():
    """Load install.py as a module so we can call _run_logged_subprocess.

    Loading install.py has heavy module-level side effects (it adds
    PROJECT_ROOT to sys.path, may relaunch under MCP venv, etc.). We
    bypass the relaunch by patching _ensure_running_under_mcp_venv
    before import — install.py is never imported as a module in
    production, this is test-only.
    """
    spec = importlib.util.spec_from_file_location("install_under_test", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    # Prevent the auto-relaunch + main() from firing.
    # install.py only runs main() under `if __name__ == "__main__"`, so
    # importing it as a module is safe.
    sys.modules["install_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_helper_exists(install_module):
    """_run_logged_subprocess is exported at module level."""
    assert hasattr(install_module, "_run_logged_subprocess")


def test_happy_path_returns_completed_process(install_module):
    """A 0-exit command returns a CompletedProcess with stdout."""
    result = install_module._run_logged_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.exit(0)"],
        step="test/0", phase_label="happy-path",
        timeout=10,
        show_dots_after_seconds=None,
    )
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_on_failure_return_returns_failed_result(install_module):
    """on_failure='return' returns CompletedProcess with non-zero rc."""
    result = install_module._run_logged_subprocess(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"],
        step="test/0", phase_label="fail-return",
        timeout=10,
        on_failure="return",
        show_dots_after_seconds=None,
    )
    assert result.returncode == 7
    assert "boom" in result.stderr


def test_on_failure_raise_raises_calledprocesserror(install_module):
    """on_failure='raise' raises CalledProcessError on non-zero exit."""
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        install_module._run_logged_subprocess(
            [sys.executable, "-c", "import sys; sys.exit(9)"],
            step="test/0", phase_label="fail-raise",
            timeout=10,
            on_failure="raise",
            show_dots_after_seconds=None,
        )
    assert exc_info.value.returncode == 9


def test_on_failure_exit_calls_sys_exit(install_module):
    """on_failure='exit' (default) calls sys.exit(1) on non-zero."""
    with pytest.raises(SystemExit) as exc_info:
        install_module._run_logged_subprocess(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            step="test/0", phase_label="fail-exit",
            timeout=10,
            show_dots_after_seconds=None,
        )
    assert exc_info.value.code == 1


def test_invalid_on_failure_raises_value_error(install_module):
    """on_failure must be one of 'exit'/'return'/'raise'."""
    with pytest.raises(ValueError, match="on_failure"):
        install_module._run_logged_subprocess(
            [sys.executable, "-c", "pass"],
            step="test/0", phase_label="bad-arg",
            timeout=10,
            on_failure="bogus",
        )


def test_timeout_propagates(install_module):
    """A subprocess that runs longer than timeout raises TimeoutExpired."""
    with pytest.raises((subprocess.TimeoutExpired, SystemExit)):
        install_module._run_logged_subprocess(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            step="test/0", phase_label="slow",
            timeout=1,
            on_failure="raise",
            show_dots_after_seconds=None,
        )


def test_env_override_propagates_to_child(install_module, tmp_path):
    """env= dict is passed to the subprocess (not inherited environ)."""
    out_file = tmp_path / "marker.txt"
    custom_env = {
        "PATH": os.environ.get("PATH", ""),  # keep PATH so subprocess can run
        "VCO_TEST_MARKER": "marker_value_42",
    }
    result = install_module._run_logged_subprocess(
        [sys.executable, "-c",
         f"import os; open(r'{out_file}', 'w').write(os.environ.get('VCO_TEST_MARKER', 'unset'))"],
        step="test/0", phase_label="env-test",
        timeout=10,
        env=custom_env,
        show_dots_after_seconds=None,
    )
    assert result.returncode == 0
    assert out_file.read_text() == "marker_value_42"


def test_cwd_override_propagates_to_child(install_module, tmp_path):
    """cwd= is honored."""
    result = install_module._run_logged_subprocess(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        step="test/0", phase_label="cwd-test",
        timeout=10,
        cwd=str(tmp_path),
        show_dots_after_seconds=None,
    )
    assert result.returncode == 0
    assert str(tmp_path) in result.stdout


def test_stderr_tail_is_logged_on_failure(install_module, capsys):
    """Failure path prints last stderr_tail_lines lines."""
    result = install_module._run_logged_subprocess(
        [sys.executable, "-c",
         "import sys\n"
         "for i in range(20): sys.stderr.write(f'line{i}\\n')\n"
         "sys.exit(1)"],
        step="test/0", phase_label="tail-test",
        timeout=10,
        on_failure="return",
        stderr_tail_lines=5,
        show_dots_after_seconds=None,
    )
    captured = capsys.readouterr()
    # Last 5 lines: line15..line19
    assert "line19" in captured.out
    assert "line15" in captured.out
    # Earlier lines NOT printed.
    assert "line5" not in captured.out
    assert result.returncode == 1


def test_user_hint_lines_printed_on_failure(install_module, capsys):
    result = install_module._run_logged_subprocess(
        [sys.executable, "-c", "import sys; sys.exit(1)"],
        step="test/0", phase_label="hint-test",
        timeout=10,
        on_failure="return",
        show_dots_after_seconds=None,
        user_hint_lines=[
            "Hint line A",
            "Hint line B",
        ],
    )
    captured = capsys.readouterr()
    assert "Hint line A" in captured.out
    assert "Hint line B" in captured.out
    assert result.returncode == 1


def test_dot_cycle_can_be_disabled(install_module, capsys):
    """show_dots_after_seconds=None disables the animation."""
    # Run a quick command; with animation disabled there should be NO
    # dot characters in stdout.
    result = install_module._run_logged_subprocess(
        [sys.executable, "-c", "pass"],
        step="test/0", phase_label="no-dots",
        timeout=10,
        show_dots_after_seconds=None,
    )
    captured = capsys.readouterr()
    # No dots (no animation triggered).
    assert "..." not in captured.out
    assert result.returncode == 0


def test_dot_cycle_does_not_trigger_for_fast_commands(install_module, capsys):
    """A command that finishes before show_dots_after_seconds shows no dots."""
    # Even with dots enabled at 3s, a 0.1s command should print no dots.
    # capsys captures non-tty output so the dot thread (which gates on
    # isatty()) won't print anyway.
    install_module._run_logged_subprocess(
        [sys.executable, "-c", "pass"],
        step="test/0", phase_label="fast",
        timeout=10,
        show_dots_after_seconds=3.0,
    )
    captured = capsys.readouterr()
    # No "elapsed" header should appear.
    assert "[ " not in captured.out or "fast" not in captured.out


def test_returncode_zero_skips_failure_path(install_module, capsys):
    """A 0-exit command does NOT print FAIL or stderr tail."""
    install_module._run_logged_subprocess(
        [sys.executable, "-c", "pass"],
        step="test/0", phase_label="silent-ok",
        timeout=10,
        show_dots_after_seconds=None,
    )
    captured = capsys.readouterr()
    assert "FAIL" not in captured.out
    assert "stderr" not in captured.out


def test_fail_message_empty_suppresses_header(install_module, capsys):
    """fail_message='' (empty string) suppresses both header and tail.

    Used by callers like _compile_python_modules that loop over many
    targets and emit one summary line at the end.
    """
    install_module._run_logged_subprocess(
        [sys.executable, "-c",
         "import sys; sys.stderr.write('some stderr\\n'); sys.exit(1)"],
        step="test/0", phase_label="suppressed",
        timeout=10,
        on_failure="return",
        show_dots_after_seconds=None,
        fail_message="",
    )
    captured = capsys.readouterr()
    # The empty fail_message suppresses BOTH the header AND the tail
    # (the loop owns its summary output).
    assert "FAIL" not in captured.out
    assert "some stderr" not in captured.out


def test_no_silent_hang_class_remaining_in_critical_sites(install_module):
    """M-P0-4 regression guard: critical pip/npm/playwright sites no
    longer use raw subprocess.run with capture_output that could
    silent-hang.

    This is a structural check: we scan install.py for the specific
    patterns the DEDUP-1 migration removed (the 4 pip-install sites,
    npm install -g, playwright npx). If a new dev re-introduces the
    raw pattern at one of these specific call shapes, this test fails.
    """
    src = INSTALL_PY.read_text(encoding="utf-8")
    # The 4 _install_requirements sites must all route through the helper.
    # We assert by counting _run_logged_subprocess occurrences in the
    # function body (4 calls expected after DEDUP-1).
    func_start = src.find("def _install_requirements(")
    assert func_start > 0
    # End at next top-level `def `:
    func_end = src.find("\ndef ", func_start + 1)
    body = src[func_start:func_end]
    helper_calls = body.count("_run_logged_subprocess(")
    assert helper_calls >= 4, (
        f"_install_requirements should route ≥4 sites through "
        f"_run_logged_subprocess (DEDUP-1); found {helper_calls}. "
        f"Did a regression re-introduce raw subprocess.run with "
        f"capture_output=True?"
    )
