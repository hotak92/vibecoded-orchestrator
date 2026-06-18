# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression: _gpu_tool_reports_live must never crash when a GPU CLI is
missing or hangs.

Background (v0.2.62): the compose-overlay ambiguity check in
``install._start_services`` probed both ``nvidia-smi`` and ``rocm-smi`` via a
bare ``subprocess.run([...])`` with no ``shutil.which`` guard. On a pure-NVIDIA
machine (no ROCm) the ``rocm-smi`` probe raised
``FileNotFoundError: [Errno 2] No such file or directory: 'rocm-smi'`` and
aborted the entire install/update. The guarded probe lives in the module-level
``_gpu_tool_reports_live`` helper now; these tests pin its contract:

  * tool absent from PATH        -> False, subprocess.run NEVER called
  * tool present, exit 0         -> True
  * tool present, exit non-zero  -> False
  * tool present, FileNotFoundError at run -> False (defense in depth)
  * tool present, TimeoutExpired -> False (a hanging tool is "not live")
"""
from __future__ import annotations

import subprocess

import pytest

import install


class _FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_missing_tool_returns_false_without_running(monkeypatch) -> None:
    monkeypatch.setattr(install.shutil, "which", lambda _tool: None)

    called = {"ran": False}

    def _boom(*_a, **_k):  # subprocess.run must NOT be reached
        called["ran"] = True
        raise AssertionError("subprocess.run should not run when tool is absent")

    monkeypatch.setattr(install.subprocess, "run", _boom)

    assert install._gpu_tool_reports_live("rocm-smi", ["--showid"]) is False
    assert called["ran"] is False


def test_present_tool_exit_zero_is_live(monkeypatch) -> None:
    monkeypatch.setattr(install.shutil, "which", lambda _tool: "/usr/bin/" + _tool)
    monkeypatch.setattr(install.subprocess, "run", lambda *_a, **_k: _FakeCompleted(0))
    assert install._gpu_tool_reports_live("nvidia-smi", ["-L"]) is True


def test_present_tool_nonzero_exit_is_not_live(monkeypatch) -> None:
    monkeypatch.setattr(install.shutil, "which", lambda _tool: "/usr/bin/" + _tool)
    monkeypatch.setattr(install.subprocess, "run", lambda *_a, **_k: _FakeCompleted(1))
    assert install._gpu_tool_reports_live("nvidia-smi", ["-L"]) is False


def test_filenotfound_at_run_is_swallowed(monkeypatch) -> None:
    # shutil.which says present (race / odd PATH) but exec still fails.
    monkeypatch.setattr(install.shutil, "which", lambda _tool: "/usr/bin/" + _tool)

    def _missing(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory", "rocm-smi")

    monkeypatch.setattr(install.subprocess, "run", _missing)
    assert install._gpu_tool_reports_live("rocm-smi", ["--showid"]) is False


def test_timeout_is_not_live(monkeypatch) -> None:
    monkeypatch.setattr(install.shutil, "which", lambda _tool: "/usr/bin/" + _tool)

    def _hang(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="rocm-smi", timeout=10)

    monkeypatch.setattr(install.subprocess, "run", _hang)
    assert install._gpu_tool_reports_live("rocm-smi", ["--showid"]) is False
