# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — install.py relaunches into its venv by VENV identity, not binary.

The field defect: ``_ensure_running_under_mcp_venv`` skipped the relaunch when
``Path(target).resolve() == Path(sys.executable).resolve()``. A POSIX venv's
``bin/python`` is a SYMLINK to the interpreter it was made from, so on the
standard Linux case — the launcher starts ``/usr/bin/python3.12 install.py``
and the venv's python links to ``/usr/bin/python3.12`` — both sides resolved to
the same file and the relaunch never happened. Every launcher-driven update
then ran on the system interpreter, where ``model_router`` does not import;
the one-time panel Default-pin migration died on exactly that import on every
update since it shipped (``state/logs/install.jsonl``: ``No module named
'model_router'``).

These tests drive the real helper with ``os.execve`` recorded, never executed.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

import install

_posix_only = pytest.mark.skipif(
    os.name == "nt", reason="the symlinked-interpreter layout is the POSIX venv shape",
)


@pytest.fixture
def execve_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def _fake_execve(path, argv, env):
        calls.append({"path": path, "argv": list(argv), "env": dict(env)})

    monkeypatch.setattr(install.os, "execve", _fake_execve)
    return calls


@pytest.fixture
def weaviate_missing(monkeypatch):
    """The launcher's interpreter: ``import weaviate`` does not resolve."""
    real = importlib.util.find_spec

    def _fake(name, package=None):
        if name == "weaviate":
            return None
        return real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _fake)
    monkeypatch.delenv("VCT_INSTALL_RELAUNCHED", raising=False)


def _venv_with_symlinked_python(root: Path) -> Path:
    """``<root>/bin/python`` -> the RUNNING interpreter, as ``python -m venv`` makes it."""
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    link = root / "bin" / "python"
    try:
        link.symlink_to(Path(sys.executable).resolve())
    except OSError as exc:  # pragma: no cover — filesystem without symlinks
        pytest.skip(f"cannot create a symlink here: {exc}")
    return link


@_posix_only
def test_symlinked_venv_interpreter_relaunches(
    tmp_path, monkeypatch, execve_calls, weaviate_missing,
):
    """THE field case: the venv's python resolves to the running binary, yet
    this process is NOT the venv — it must relaunch into it."""
    venv_python = _venv_with_symlinked_python(tmp_path / "venv")
    assert venv_python.resolve() == Path(sys.executable).resolve()
    monkeypatch.setattr(install, "_resolve_venv_python_for_install", lambda _root: venv_python)
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--update"])

    install._ensure_running_under_mcp_venv()

    assert len(execve_calls) == 1, "a symlinked venv interpreter must still be relaunched into"
    call = execve_calls[0]
    assert call["path"] == str(venv_python)
    token = call["env"]["VCT_INSTALL_RELAUNCH_TOKEN"]
    assert call["argv"] == [str(venv_python), "install.py", "--update", f"--vct-relaunch-token={token}"]
    assert call["env"]["VCT_INSTALL_RELAUNCHED"] == "1"


@_posix_only
def test_process_already_inside_the_venv_is_not_relaunched(
    tmp_path, monkeypatch, execve_calls, weaviate_missing,
):
    """sys.prefix IS the venv root: nothing to do, however the binary resolves."""
    root = tmp_path / "venv"
    venv_python = _venv_with_symlinked_python(root)
    monkeypatch.setattr(install, "_resolve_venv_python_for_install", lambda _root: venv_python)
    monkeypatch.setattr(install.sys, "prefix", str(root))

    install._ensure_running_under_mcp_venv()

    assert execve_calls == []


def test_inside_a_venv_whose_python_is_a_real_file_is_not_relaunched(
    tmp_path, monkeypatch, execve_calls, weaviate_missing,
):
    """A copied (non-symlink) interpreter — Windows, ``venv --copies`` — whose
    venv this process already is: identity by prefix, not by binary."""
    root = tmp_path / "venv"
    (root / "bin").mkdir(parents=True)
    venv_python = root / "bin" / "python"
    venv_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(install, "_resolve_venv_python_for_install", lambda _root: venv_python)
    monkeypatch.setattr(install.sys, "prefix", str(root))

    install._ensure_running_under_mcp_venv()

    assert execve_calls == []


@_posix_only
def test_relaunched_flag_stops_a_loop(
    tmp_path, monkeypatch, execve_calls, weaviate_missing,
):
    """The relaunched child still cannot import weaviate (a broken venv): the
    guard, not the identity check, is what stops a second exec."""
    venv_python = _venv_with_symlinked_python(tmp_path / "venv")
    monkeypatch.setattr(install, "_resolve_venv_python_for_install", lambda _root: venv_python)
    monkeypatch.setenv("VCT_INSTALL_RELAUNCHED", "1")

    install._ensure_running_under_mcp_venv()

    assert execve_calls == []


# ---------------------------------------------------------------------------
# The identity rule itself, across layouts
# ---------------------------------------------------------------------------


def test_identity_windows_layout(tmp_path):
    """``<root>\\Scripts\\python.exe`` is two levels below the root, like ``bin/python``."""
    root = tmp_path / "venv"
    (root / "Scripts").mkdir(parents=True)
    exe = root / "Scripts" / "python.exe"
    exe.write_bytes(b"")
    assert install._is_running_inside_venv(exe, prefix=str(root)) is True
    assert install._is_running_inside_venv(exe, prefix=str(tmp_path / "base")) is False


@_posix_only
def test_identity_through_an_aliased_directory(tmp_path):
    """macOS reports ``/private/var/...`` for a ``/var/...`` path: the same venv
    reached through a symlinked parent is still the same venv."""
    real = tmp_path / "real"
    (real / "venv" / "bin").mkdir(parents=True)
    (real / "venv" / "bin" / "python").write_bytes(b"")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    venv_python = alias / "venv" / "bin" / "python"
    assert install._is_running_inside_venv(venv_python, prefix=str(real / "venv")) is True


def test_identity_base_interpreter_is_not_the_venv(tmp_path):
    """The base prefix (``/usr`` for ``/usr/bin/python3.12``) is never a venv root."""
    venv_python = tmp_path / "venv" / "bin" / "python"
    assert install._is_running_inside_venv(venv_python, prefix=sys.base_prefix) is False
