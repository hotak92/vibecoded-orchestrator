# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 M-P1-2: Podman machine auto-init on macOS + Windows.

Verifies install.py now runs `podman machine init` automatically when
no machine exists (was: ask the user to do it manually). Closes the
first-time-install friction reported as M-P1-2.

Per docs/INSTALL_ARCHITECTURE_v2.md per-track ownership table.
"""

from __future__ import annotations

import importlib.util
import json
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
        "install_under_test_mp12", INSTALL_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_under_test_mp12"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_helper_exists(install_module):
    assert hasattr(install_module, "_podman_machine_auto_init_and_start")


def test_skips_init_when_machine_already_exists(install_module):
    """If `podman machine list` shows a machine, init is skipped."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "list" in cmd:
            return MagicMock(returncode=0, stdout='[{"Name": "podman-machine-default"}]', stderr="")
        if "start" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, detail = install_module._podman_machine_auto_init_and_start()

    assert ok is True
    # Verify init was NOT called.
    assert not any("init" in c for c in calls), (
        f"init must be skipped when machine exists; calls={calls}"
    )


def test_runs_init_when_no_machine_exists(install_module):
    """Empty machine list → run init + start."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "list" in cmd:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if "init" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "start" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, detail = install_module._podman_machine_auto_init_and_start()

    assert ok is True
    assert any("init" in c for c in calls), (
        f"init must run when no machine exists; calls={calls}"
    )
    assert any("start" in c for c in calls), (
        f"start must run after init; calls={calls}"
    )


def test_init_failure_returns_false_with_detail(install_module):
    """Init exits non-zero → return False + detail."""
    def fake_run(cmd, **kw):
        if "list" in cmd:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if "init" in cmd:
            return MagicMock(returncode=125, stdout="", stderr="permission denied")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, detail = install_module._podman_machine_auto_init_and_start()
    assert ok is False
    assert "init" in detail.lower()
    assert "125" in detail or "permission denied" in detail


def test_init_timeout_returns_false_with_timeout_detail(install_module):
    """Init TimeoutExpired → False + clear timeout message."""
    def fake_run(cmd, **kw):
        if "list" in cmd:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if "init" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, detail = install_module._podman_machine_auto_init_and_start()
    assert ok is False
    assert "timed out" in detail.lower() or "timeout" in detail.lower()


def test_start_failure_after_init_returns_false(install_module):
    """If init succeeds but start fails, the start failure is surfaced."""
    def fake_run(cmd, **kw):
        if "list" in cmd:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if "init" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "start" in cmd:
            return MagicMock(returncode=125, stdout="", stderr="boot failed")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, detail = install_module._podman_machine_auto_init_and_start()
    assert ok is False
    assert "boot failed" in detail or "125" in detail


def test_already_running_is_success(install_module):
    """`podman machine start` returns non-zero with "already running" → success."""
    def fake_run(cmd, **kw):
        if "list" in cmd:
            return MagicMock(returncode=0, stdout='[{"Name": "x"}]', stderr="")
        if "start" in cmd:
            return MagicMock(returncode=125, stdout="", stderr="Error: VM already running")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, detail = install_module._podman_machine_auto_init_and_start()
    assert ok is True


def test_try_start_podman_daemon_routes_through_helper_on_darwin(install_module):
    """_try_start_podman_daemon on Darwin uses the new auto-init helper."""
    src = INSTALL_PY.read_text(encoding="utf-8")
    # The Darwin/Windows branch should call _podman_machine_auto_init_and_start.
    func_start = src.find("def _try_start_podman_daemon(")
    assert func_start > 0
    func_end = src.find("\ndef ", func_start + 1)
    body = src[func_start:func_end]
    assert "_podman_machine_auto_init_and_start" in body, (
        "_try_start_podman_daemon (Darwin/Windows branch) must route "
        "through the new auto-init helper."
    )


def test_machine_list_subprocess_failure_returns_clear_error(install_module):
    """OSError from machine list → False + clear error."""
    def fake_run(cmd, **kw):
        if "list" in cmd:
            raise OSError("podman gone")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, detail = install_module._podman_machine_auto_init_and_start()
    assert ok is False
    assert "list" in detail.lower() or "failed" in detail.lower()
