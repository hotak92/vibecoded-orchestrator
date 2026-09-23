# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review F1 — the venv rebuild after the relaunch started firing on POSIX.

Once install.py really relaunches into its venv, three things that used to
answer "this process" changed meaning:

* the drift check compared the venv's Python with ``sys.version_info`` — the
  venv's OWN, after the relaunch — so it could never fire, and its message
  labelled the venv's version ``launcher=``;
* ``--rebuild-venv`` only acted inside that drift arm, so it went inert;
* a recreate ran ``rmtree(.venv)`` from the interpreter living in it, then
  built the new venv with ``sys.executable`` — the path it had just deleted.

The relaunch now RECORDS the launching interpreter
(``install_companions.mark_relaunch``); the drift check and the venv builder
read that record; ``--rebuild-venv`` always recreates; and a rebuild never
happens from inside the venv — it hands off to the base interpreter first,
or refuses. Everything runs on a throwaway install root; ``os.execve`` and
the venv builder are recorded, never executed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

import install
from vco_lib import install_companions as ic
from vco_lib import manifest_paths

_POSIX = pytest.mark.skipif(os.name == "nt", reason="a shell script stands in for the venv's python")
HERE = f"{sys.version_info.major}.{sys.version_info.minor}"


class _Execed(Exception):
    """``os.execve`` never returns; the recorder raises this instead."""


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """An install root with a VCO-owned venv whose python reports ``HERE``."""
    root = tmp_path / "orchestrator"
    (root / ".venv" / "bin").mkdir(parents=True)
    fake = root / ".venv" / "bin" / "python"
    fake.write_text(f"#!/bin/sh\necho {HERE}\n", encoding="utf-8")
    fake.chmod(0o755)
    manifest = manifest_paths.manifest_path(root)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{}", encoding="utf-8")
    for key in (ic.ENV_RELAUNCHED, ic.ENV_BASE_PYTHON, ic.ENV_BASE_PYTHON_VERSION):
        monkeypatch.delenv(key, raising=False)
    return root


def _relaunched_from(monkeypatch, *, version: str, python: str | None = None) -> None:
    """The env a relaunched child sees: launched by ``python`` at ``version``."""
    monkeypatch.setenv(ic.ENV_RELAUNCHED, "1")
    monkeypatch.setenv(ic.ENV_BASE_PYTHON_VERSION, version)
    if python is not None:
        monkeypatch.setenv(ic.ENV_BASE_PYTHON, python)


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


def test_mark_relaunch_records_the_launcher_on_the_first_hop_only(monkeypatch):
    for key in (ic.ENV_RELAUNCHED, ic.ENV_BASE_PYTHON, ic.ENV_BASE_PYTHON_VERSION):
        monkeypatch.delenv(key, raising=False)
    first: dict = {}
    ic.mark_relaunch(first)
    assert first == {
        ic.ENV_RELAUNCHED: "1",
        ic.ENV_BASE_PYTHON: sys.executable,
        ic.ENV_BASE_PYTHON_VERSION: HERE,
    }
    # A second hop (the rebuild handoff) keeps the ORIGINAL launcher.
    _relaunched_from(monkeypatch, version="3.99", python="/usr/bin/python3.99")
    second = {ic.ENV_BASE_PYTHON: "/usr/bin/python3.99", ic.ENV_BASE_PYTHON_VERSION: "3.99"}
    ic.mark_relaunch(second)
    assert second[ic.ENV_BASE_PYTHON] == "/usr/bin/python3.99"
    assert second[ic.ENV_BASE_PYTHON_VERSION] == "3.99"


def test_the_relaunch_carries_the_record(monkeypatch, tmp_path: Path):
    """``_ensure_running_under_mcp_venv`` stamps the child env it execs with."""
    import importlib.util

    for key in (ic.ENV_RELAUNCHED, ic.ENV_BASE_PYTHON, ic.ENV_BASE_PYTHON_VERSION):
        monkeypatch.delenv(key, raising=False)
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, p=None: None if n == "weaviate" else real(n, p))
    target = tmp_path / "venv" / "bin" / "python"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(install, "_resolve_venv_python_for_install", lambda _r: target)
    calls: list = []
    monkeypatch.setattr(install.os, "execve", lambda p, a, e: calls.append(e))
    install._ensure_running_under_mcp_venv()
    (env,) = calls
    assert env[ic.ENV_BASE_PYTHON] == sys.executable
    assert env[ic.ENV_BASE_PYTHON_VERSION] == HERE


# ---------------------------------------------------------------------------
# triage
# ---------------------------------------------------------------------------


@_POSIX
def test_drift_is_detected_after_the_relaunch(root: Path, monkeypatch):
    """The venv reports THIS interpreter's version (it IS it, post-relaunch);
    the user launched with another. The drift must still be seen — and the
    message's ``launcher=`` must be the launcher."""
    _relaunched_from(monkeypatch, version="3.99")
    triage = install._venv_triage(root)
    assert triage["action"] == "recreate"
    assert f".venv='{HERE}'" in triage["reason"] and "launcher='3.99'" in triage["reason"]


@_POSIX
def test_no_drift_when_the_launcher_matches(root: Path, monkeypatch):
    _relaunched_from(monkeypatch, version=HERE)
    assert install._venv_triage(root)["action"] in ("upgrade", "skip")


@_POSIX
def test_rebuild_venv_forces_a_recreate(root: Path, monkeypatch):
    """The flag promises a rebuild; a healthy, matching venv is no exception."""
    _relaunched_from(monkeypatch, version=HERE)
    triage = install._venv_triage(root, force_rebuild=True)
    assert triage == {
        "action": "recreate",
        "reason": "--rebuild-venv: rebuild requested",
        "venv_python": None,
    }


# ---------------------------------------------------------------------------
# building a venv: the base interpreter, never a venv's own python
# ---------------------------------------------------------------------------


def _record_venv_builds(monkeypatch) -> list:
    builds: list = []

    class _Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(install.subprocess, "run", lambda argv, **_kw: builds.append(argv) or _Done())
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    return builds


def test_create_venv_uses_the_recorded_launcher(tmp_path: Path, monkeypatch):
    launcher = tmp_path / "python3.99"
    launcher.write_text("", encoding="utf-8")
    _relaunched_from(monkeypatch, version="3.99", python=str(launcher))
    builds = _record_venv_builds(monkeypatch)
    install._create_venv(tmp_path / "root")
    assert builds == [[str(launcher), "-m", "venv", str(tmp_path / "root" / ".venv")]]


def test_create_venv_never_uses_a_venvs_own_python(tmp_path: Path, monkeypatch):
    """Not relaunched, running from a venv (pytest here): the BASE interpreter."""
    for key in (ic.ENV_RELAUNCHED, ic.ENV_BASE_PYTHON, ic.ENV_BASE_PYTHON_VERSION):
        monkeypatch.delenv(key, raising=False)
    base = tmp_path / "base-python"
    base.write_text("", encoding="utf-8")
    monkeypatch.setattr(ic.sys, "_base_executable", str(base), raising=False)
    builds = _record_venv_builds(monkeypatch)
    install._create_venv(tmp_path / "root")
    assert builds[0][0] == str(base) != sys.executable


# ---------------------------------------------------------------------------
# a rebuild never deletes the tree it runs from
# ---------------------------------------------------------------------------


def _lightweight_args() -> argparse.Namespace:
    return argparse.Namespace(
        lightweight=True, lightweight_old_path=None, no_containers=True,
        dev=False, rebuild_venv=True,
    )


@pytest.fixture
def lightweight(root: Path, monkeypatch):
    """``_run_lightweight`` on ``root`` with the steps after triage stubbed."""
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_create_state_directory", lambda: None)
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    monkeypatch.setattr(install, "_install_requirements", lambda *a, **k: None)
    execs: list = []

    def _execve(path, argv, env):
        execs.append((path, argv, env))
        raise _Execed

    monkeypatch.setattr(ic.os, "execve", _execve)
    return execs


@_POSIX
def test_a_rebuild_from_inside_the_venv_hands_off_and_deletes_nothing(
    root: Path, lightweight: list, monkeypatch, capsys,
):
    base = root.parent / "system-python"
    base.write_text("", encoding="utf-8")
    _relaunched_from(monkeypatch, version=HERE, python=str(base))
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))  # THIS process is the venv
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--lightweight", "--rebuild-venv"])

    with pytest.raises(_Execed):
        install._run_lightweight(_lightweight_args())

    assert (root / ".venv" / "bin" / "python").is_file(), "nothing was deleted before the handoff"
    ((path, argv, env),) = lightweight
    assert path == str(base) and argv == [str(base), "install.py", "--lightweight", "--rebuild-venv"]
    assert env[ic.ENV_RELAUNCHED] == "1", "the child must not relaunch back into the venv"
    assert env[ic.ENV_BASE_PYTHON] == str(base)
    assert "outside the venv it was running from" in capsys.readouterr().out


@_POSIX
def test_a_rebuild_with_no_interpreter_outside_the_venv_is_refused(
    root: Path, lightweight: list, monkeypatch, capsys,
):
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(ic, "base_python_for_venv", lambda: str(root / ".venv" / "bin" / "python"))

    assert install._run_lightweight(_lightweight_args()) == 1

    assert lightweight == [], "no exec"
    assert (root / ".venv" / "bin" / "python").is_file(), "no deletion"
    assert "Venv rebuild REFUSED" in capsys.readouterr().out


@_POSIX
def test_a_rebuild_from_outside_deletes_and_rebuilds(root: Path, lightweight: list, monkeypatch, capsys):
    """The ordinary case once outside: the old venv goes, a new one is built,
    and the printed triage line says why."""
    created: list = []

    def _create(project_root: Path) -> Path:
        assert not (project_root / ".venv").exists(), "the old venv was removed first"
        created.append(project_root)
        return project_root / ".venv" / "bin" / "python"

    monkeypatch.setattr(install, "_create_venv", _create)
    monkeypatch.setattr(install, "_run_machine_migrations", lambda _r: None)

    install._run_lightweight(_lightweight_args())

    assert created == [root] and lightweight == []
    assert "[2/4] Venv triage: action=recreate (--rebuild-venv: rebuild requested)" in capsys.readouterr().out
