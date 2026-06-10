# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 M-P1-1: Python wheel-coverage detection.

Replaces a hard MAX_PYTHON constant with a wheel-coverage probe.
When the host Python (3.14+) lacks wheels for VCO's binary deps,
install.py prints a clear refusal + workaround hint BEFORE the
pip-install step explodes with a confusing C-compiler error.

Per docs/INSTALL_ARCHITECTURE_v2.md §3.4 and per-track-table M-P1-1.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"


@pytest.fixture(scope="module")
def install_module():
    spec = importlib.util.spec_from_file_location(
        "install_under_test_mp11", INSTALL_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_under_test_mp11"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_check_wheel_support_helper_exists(install_module):
    assert hasattr(install_module, "_check_wheel_support_for_python")


def test_helper_returns_true_when_pip_dry_run_succeeds(install_module):
    """pip --dry-run --only-binary=:all: returns 0 → True."""
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        result = install_module._check_wheel_support_for_python("python3")
    assert result is True


def test_helper_returns_false_when_pip_dry_run_fails(install_module):
    """pip non-zero → False (would need source build)."""
    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
        result = install_module._check_wheel_support_for_python("python3")
    assert result is False


def test_helper_returns_none_on_subprocess_error(install_module):
    """OSError (no pip / no python) → None (probe failed)."""
    with patch("subprocess.run", side_effect=OSError("no such file")):
        result = install_module._check_wheel_support_for_python("python3")
    assert result is None


def test_helper_returns_none_on_timeout(install_module):
    """TimeoutExpired → None (probe inconclusive)."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["pip"], timeout=30),
    ):
        result = install_module._check_wheel_support_for_python("python3")
    assert result is None


def test_helper_uses_only_binary_flag(install_module):
    """pip is invoked with --only-binary=:all: to force wheel-only resolution."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        install_module._check_wheel_support_for_python("python3")

    assert "--only-binary=:all:" in captured["cmd"]
    assert "--dry-run" in captured["cmd"]
    assert "-m" in captured["cmd"]
    assert "pip" in captured["cmd"]


def test_helper_disables_pip_version_check(install_module):
    """PIP_DISABLE_PIP_VERSION_CHECK=1 is set so probe is quiet."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env", {})
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        install_module._check_wheel_support_for_python("python3")
    assert captured["env"].get("PIP_DISABLE_PIP_VERSION_CHECK") == "1"


def test_check_python_version_warns_only_for_3_14_plus(install_module):
    """The wheel check fires only when Python >= (3, 14)."""
    # On 3.12, the check should NOT fire (no subprocess call).
    fake_version = type("V", (), {
        "major": 3, "minor": 12, "micro": 5,
    })()
    with patch.object(install_module.sys, "version_info", fake_version), \
         patch("subprocess.run") as mock_run, \
         patch.object(install_module, "_log_install_event"):
        install_module._check_python_version()
    # Should not have invoked pip --dry-run.
    for call in mock_run.call_args_list:
        argv = call.args[0] if call.args else []
        if "--only-binary=:all:" in argv:
            pytest.fail(
                "wheel-check fired on Python 3.12; should only fire on 3.14+"
            )


def test_check_python_version_clear_refuse_for_3_14_no_wheels(install_module, capsys):
    """3.14+ + wheel_support_ok=False → clear refuse + exit 1."""
    fake_version = type("V", (), {
        "major": 3, "minor": 14, "micro": 0,
    })()
    with patch.object(install_module.sys, "version_info", fake_version), \
         patch.object(
             install_module, "_check_wheel_support_for_python",
             return_value=False,
         ), \
         patch.object(install_module, "_log_install_event"), \
         pytest.raises(SystemExit) as exc_info:
        install_module._check_python_version()
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "FAIL" in captured.out
    # Workaround hint must mention 3.12 or 3.13.
    assert "3.13" in captured.out or "3.12" in captured.out
    # The user should see a concrete command to run.
    assert "install.py" in captured.out
