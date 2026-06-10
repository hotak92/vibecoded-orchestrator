# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 M-P1-3: pip subprocess robustness env + flags.

Verifies:
* _pip_subprocess_env() exports PIP_DISABLE_PIP_VERSION_CHECK + PIP_NO_INPUT
* _pip_install_flags() returns --timeout 60 --retries 5 --prefer-binary
* All pip-install callsites in _install_requirements thread the flags

Per docs/INSTALL_ARCHITECTURE_v2.md per-track table M-P1-3.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"


@pytest.fixture(scope="module")
def install_module():
    spec = importlib.util.spec_from_file_location(
        "install_under_test_mp13", INSTALL_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_under_test_mp13"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_pip_subprocess_env_scrubs_pythonpath(install_module):
    """PYTHONPATH is set to empty string (not deleted)."""
    env = install_module._pip_subprocess_env()
    assert "PYTHONPATH" in env
    assert env["PYTHONPATH"] == ""


def test_pip_subprocess_env_disables_pip_version_check(install_module):
    """PIP_DISABLE_PIP_VERSION_CHECK=1 set so banner is suppressed."""
    env = install_module._pip_subprocess_env()
    assert env.get("PIP_DISABLE_PIP_VERSION_CHECK") == "1"


def test_pip_subprocess_env_disables_input(install_module):
    """PIP_NO_INPUT=1 refuses pip prompts (defends against CI hangs)."""
    env = install_module._pip_subprocess_env()
    assert env.get("PIP_NO_INPUT") == "1"


def test_pip_install_flags_helper_exists(install_module):
    assert hasattr(install_module, "_pip_install_flags")


def test_pip_install_flags_has_timeout(install_module):
    flags = install_module._pip_install_flags()
    assert "--timeout" in flags
    idx = flags.index("--timeout")
    assert flags[idx + 1] == "60"


def test_pip_install_flags_has_retries(install_module):
    flags = install_module._pip_install_flags()
    assert "--retries" in flags
    idx = flags.index("--retries")
    assert flags[idx + 1] == "5"


def test_pip_install_flags_has_prefer_binary(install_module):
    flags = install_module._pip_install_flags()
    assert "--prefer-binary" in flags


def test_pip_install_flags_returns_a_new_list_each_call(install_module):
    """Calling twice doesn't share mutable state."""
    a = install_module._pip_install_flags()
    b = install_module._pip_install_flags()
    a.append("--extra")
    assert "--extra" not in b


def test_install_requirements_threads_pip_install_flags(install_module):
    """_install_requirements's pip-install argvs include _pip_install_flags()."""
    src = INSTALL_PY.read_text(encoding="utf-8")
    func_start = src.find("def _install_requirements(")
    assert func_start > 0
    func_end = src.find("\ndef ", func_start + 1)
    body = src[func_start:func_end]
    # At least 4 pip install sites should splice the flags.
    occurrences = body.count("_pip_install_flags()")
    assert occurrences >= 4, (
        f"_install_requirements should thread _pip_install_flags() into "
        f"≥4 pip-install argvs (M-P1-3); found {occurrences}."
    )


def test_pip_subprocess_env_preserves_os_environ_paths(install_module, monkeypatch):
    """PATH and HOME from os.environ pass through to pip subprocess env."""
    monkeypatch.setenv("SOME_USER_VAR", "kept_value")
    env = install_module._pip_subprocess_env()
    # PATH (which the test process inherited) should be preserved.
    assert "PATH" in env
    # The user-set var should also be preserved.
    assert env.get("SOME_USER_VAR") == "kept_value"
