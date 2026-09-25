# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review R2 F17/F20 — install.py's relaunch record never outlives its hop.

When install.py relaunches itself it writes a record into the child's env: the
loop guard, the launching interpreter, the waiting parent's pid. Env is
inherited by every descendant. Before this fix a detached child carried the
record on — on Windows vct-updater -> the relaunched launcher -> its NEXT
``install.py --update``, which then (a) skipped the venv relaunch (stale loop
guard) and (b) watched a stale pid and could ``os._exit(22084)`` mid-update
when an unrelated process that reused it ended.

Three layers, one key list (``vco_lib/install_relaunch_env.toml``, embedded by
the launcher too):

* every long-lived child install.py starts gets a scrubbed env;
* the launcher removes the keys from every install.py it spawns (Rust test
  ``every_install_py_spawn_drops_the_relaunch_record``);
* a run acts on the record only when its argv carries the matching per-hop
  token — argv reaches the direct child only, so an inherited record is
  refused wherever it came from (``adopt_relaunch``).

F20: the rebuild handoff started from an activated venv records the base
interpreter it hands the run to, never the venv's own python.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

import install
from tests.common.child_env import child_env
from vco_lib import install_companions as ic
from vco_lib import manifest_paths

RECORD = {
    ic.ENV_RELAUNCHED: "1",
    ic.ENV_BASE_PYTHON: "/usr/bin/python3.99",
    ic.ENV_BASE_PYTHON_VERSION: "3.99",
    ic.ENV_PARENT_WAITS: "4242",
    ic.ENV_RELAUNCH_TOKEN: "feedfacecafebeef",
}


@pytest.fixture
def polluted(monkeypatch):
    """This process inherited a relaunch record from some older run."""
    for key, value in RECORD.items():
        monkeypatch.setenv(key, value)


def _no_record(env) -> None:
    leaked = sorted(set(RECORD) & set(env or {}))
    assert leaked == [], f"relaunch record leaked into a long-lived child: {leaked}"


# ---------------------------------------------------------------------------
# the one key list
# ---------------------------------------------------------------------------


def test_the_table_is_the_record():
    """The committed table (the launcher embeds it) names every key the
    Python side writes — adding one on either side alone fails here."""
    table = tomllib.loads(
        (Path(ic.__file__).with_name("install_relaunch_env.toml")).read_text(encoding="utf-8"))
    assert table["format_version"] == 1
    assert set(table["keys"]) == set(RECORD) == set(ic.RELAUNCH_ENV_KEYS)


def test_scrub_removes_the_record_and_nothing_else():
    env = {**RECORD, "PATH": "/bin", "VCT_STATE_DIR": "/s"}
    assert ic.scrub_install_relaunch_env(env) is env
    assert env == {"PATH": "/bin", "VCT_STATE_DIR": "/s"}


def test_detached_child_env_is_scrubbed(polluted):
    env = ic.detached_child_env()
    _no_record(env)
    assert env["PATH"] == os.environ["PATH"]
    assert os.environ[ic.ENV_RELAUNCHED] == "1", "this process's own env is untouched"


# ---------------------------------------------------------------------------
# every long-lived child install.py starts
# ---------------------------------------------------------------------------


class _Spawned(Exception):
    pass


def _recording_popen(seen: list, *, stop: bool = False):
    def _popen(argv, **kwargs):
        seen.append(kwargs.get("env"))
        if stop:
            raise _Spawned

        class _Proc:
            pid = 4321
            returncode = 0

        return _Proc()

    return _popen


def test_hub_ensure_spawn(polluted, monkeypatch, tmp_path):
    from vco_lib import hub_ensure

    seen: list = []
    monkeypatch.setattr(hub_ensure.subprocess, "Popen", _recording_popen(seen))
    monkeypatch.setattr(hub_ensure.subprocess, "run",
                        lambda argv, **kw: seen.append(kw.get("env")) or subprocess.CompletedProcess(argv, 0))
    hub_ensure._spawn(tmp_path / "vct-hub", wait=False)
    hub_ensure._spawn(tmp_path / "vct-hub", wait=True)
    assert len(seen) == 2 and all(env is not None for env in seen)
    for env in seen:
        _no_record(env)


def test_launcher_ensure_spawn(polluted, monkeypatch, tmp_path):
    from vco_lib import launcher_ensure

    seen: list = []
    monkeypatch.setattr(launcher_ensure.subprocess, "Popen", _recording_popen(seen))
    launcher_ensure._spawn([str(tmp_path / "vct-launcher")], tmp_path)
    (env,) = seen
    assert env is not None
    _no_record(env)


def test_deferral_retry_spawn_detached(polluted, monkeypatch, tmp_path):
    from vco_lib import deferral_retry

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    seen: list = []
    monkeypatch.setattr(deferral_retry.subprocess, "Popen", _recording_popen(seen))
    assert deferral_retry.spawn_detached(tmp_path, python=sys.executable) is True
    (env,) = seen
    assert env is not None
    _no_record(env)


def test_codegraph_resync_children(polluted, monkeypatch, tmp_path):
    from vco_lib import codegraph_resync as cr

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)  # Popen is faked below
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n", encoding="utf-8")
    seen: list = []
    monkeypatch.setattr(cr.subprocess, "Popen", _recording_popen(seen))
    assert cr.spawn_background_resync(tmp_path, "MyProj", python_exe=sys.executable).status == "launched"
    assert seen and all(env is not None for env in seen)
    for env in seen:
        _no_record(env)


def test_install_vct_updater_spawn(polluted, monkeypatch, tmp_path):
    """The chain F17 names: this child relaunches the launcher, whose next
    install.py --update must not inherit the record."""
    root = tmp_path / "install"
    dist = root / "launcher" / "dist" / "windows-x64"
    dist.mkdir(parents=True)
    for name in ("vct-launcher.exe", "vct-hub.exe", "vct-launcher.exe.new", "vct-updater.exe"):
        (dist / name).write_bytes(b"x")
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    seen: list = []
    monkeypatch.setattr(install.subprocess, "Popen", _recording_popen(seen))
    assert install._try_invoke_windows_stage1_updater(root, launcher_pid=42) is not None
    (env,) = seen
    assert env is not None
    _no_record(env)


def test_install_docker_desktop_start(polluted, monkeypatch, tmp_path):
    exe = tmp_path / "pf" / "Docker" / "Docker" / "Docker Desktop.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    seen: list = []
    monkeypatch.setattr(install.subprocess, "Popen", _recording_popen(seen, stop=True))
    with pytest.raises(_Spawned):
        install._try_start_docker_daemon()
    (env,) = seen
    assert env is not None
    _no_record(env)


def test_install_hub_start(polluted, monkeypatch, tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    binary = root / "vct-hub"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(install, "_ensure_vct_hub_binary", lambda *a, **k: binary)
    monkeypatch.setattr(install, "_write_vct_hub_cutover_sentinel", lambda *a, **k: None)
    monkeypatch.setattr(install, "_wait_for_vct_hub_health", lambda *a, **k: True)
    monkeypatch.setattr(install, "_delete_vct_hub_cutover_sentinel", lambda *a, **k: None)
    seen: list = []

    def _run(cmd, **kwargs):
        seen.append(kwargs.get("env"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(install.subprocess, "run", _run)
    install._deploy_and_start_vct_hub(root, stop_running_first=False)
    assert seen and seen[-1] is not None
    _no_record(seen[-1])
    assert seen[-1]["VCT_INSTALL_ROOT"] == str(root.resolve()), "the hub's own pins still set"


# ---------------------------------------------------------------------------
# adopt_relaunch: a record counts only for the run it was made for
# ---------------------------------------------------------------------------


def test_a_genuine_relaunch_is_adopted(polluted, monkeypatch):
    watched: list = []
    monkeypatch.setattr(ic, "start_parent_watch", lambda: watched.append(1) or True)
    argv = ["install.py", "--update", ic.RELAUNCH_TOKEN_ARG + RECORD[ic.ENV_RELAUNCH_TOKEN]]
    assert ic.adopt_relaunch(argv) is True
    assert argv == ["install.py", "--update"], "argparse never sees the token"
    assert watched == [1]
    assert all(os.environ[key] == value for key, value in RECORD.items())


@pytest.mark.parametrize("extra", [
    [],                                                    # inherited: no token on argv
    [ic.RELAUNCH_TOKEN_ARG + "0000000000000000"],          # a token, not this record's
    [ic.RELAUNCH_TOKEN_ARG + RECORD[ic.ENV_RELAUNCH_TOKEN]] * 2,  # ambiguous
])
def test_an_inherited_record_is_dropped(polluted, monkeypatch, capsys, extra):
    monkeypatch.setattr(ic, "start_parent_watch", lambda: pytest.fail("an inherited pid is never watched"))
    argv = ["install.py", "--update", *extra]
    assert ic.adopt_relaunch(argv) is False
    assert argv == ["install.py", "--update"]
    _no_record(os.environ)
    err = capsys.readouterr().err
    assert "ignoring" in err and ic.ENV_PARENT_WAITS in err


def test_a_fresh_run_is_left_alone(monkeypatch, capsys):
    for key in RECORD:
        monkeypatch.delenv(key, raising=False)
    argv = ["install.py", "--update"]
    assert ic.adopt_relaunch(argv) is False
    assert argv == ["install.py", "--update"] and capsys.readouterr().err == ""


def test_the_f17_scenario_cannot_skip_the_relaunch_or_watch_a_stale_pid(polluted, monkeypatch):
    """A launcher relaunched by vct-updater hands a later install.py the old
    record (an old launcher binary does; the new one removes it). That run
    must relaunch into the venv as usual and never open the stale pid."""
    import importlib.util

    monkeypatch.setattr(ic.sys, "platform", "win32")
    opened: list = []

    class _Winapi:
        SYNCHRONIZE = 0x00100000

        def OpenProcess(self, *a):  # noqa: N802
            opened.append(a)
            raise AssertionError("a stale pid was opened")

    monkeypatch.setitem(sys.modules, "_winapi", _Winapi())
    argv = ["install.py", "--update"]
    ic.adopt_relaunch(argv)
    assert opened == []

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, p=None: None if n == "weaviate" else real(n, p))
    target = Path(sys.prefix).parent / "elsewhere-venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(install, "_resolve_venv_python_for_install", lambda _r: target)
    monkeypatch.setattr(install.Path, "is_file", lambda self: True)
    monkeypatch.setattr(install.sys, "argv", argv)
    runs: list = []
    monkeypatch.setattr(ic, "run_child", lambda cmd, env: runs.append((cmd, env)) or 0)
    monkeypatch.setattr(ic.os, "_exit", lambda code: (_ for _ in ()).throw(_Spawned()))
    with pytest.raises(_Spawned):
        install._ensure_running_under_mcp_venv()
    ((cmd, env),) = runs
    assert env[ic.ENV_PARENT_WAITS] == str(os.getpid()), "a fresh record, naming THIS parent"
    assert env[ic.ENV_RELAUNCH_TOKEN] != RECORD[ic.ENV_RELAUNCH_TOKEN]
    assert cmd[-1] == ic.RELAUNCH_TOKEN_ARG + env[ic.ENV_RELAUNCH_TOKEN]


def test_across_real_processes(tmp_path):
    """Real children: the direct child adopts its record; a grandchild that
    merely inherits the env (no argv token) refuses it."""
    grandchild = (
        "import sys; from vco_lib import install_companions as ic; "
        "print('grandchild', ic.adopt_relaunch(sys.argv), ic.ENV_PARENT_WAITS in __import__('os').environ)"
    )
    child = textwrap.dedent(f"""
        import os, subprocess, sys
        from vco_lib import install_companions as ic
        ic.start_parent_watch = lambda: True
        print("child", ic.adopt_relaunch(sys.argv), sys.argv[1:], flush=True)
        subprocess.run([sys.executable, "-c", {grandchild!r}], check=True)
    """)
    parent = tmp_path / "parent.py"
    parent.write_text(textwrap.dedent(f"""
        import os, sys
        from vco_lib import install_companions as ic
        ic._exec_replaces_process = lambda: False
        env = dict(os.environ)
        ic.mark_relaunch(env)
        ic.hand_off(sys.executable, [sys.executable, "-c", {child!r}, "--update"], env)
    """), encoding="utf-8")
    done = subprocess.run([sys.executable, str(parent)], capture_output=True, text=True,
                          timeout=60, env=child_env())
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["child True ['--update']", "grandchild False False"]
    assert "ignoring" in done.stderr


# ---------------------------------------------------------------------------
# F20: the base interpreter, never the venv's own python
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_rebuild_from_an_activated_venv_records_the_base_interpreter(tmp_path, monkeypatch):
    """No relaunch happened (weaviate importable in the activated venv), so
    this is the FIRST hop: the record must name the base it hands off to."""
    for key in RECORD:
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "orchestrator"
    (root / ".venv" / "bin").mkdir(parents=True)
    fake = root / ".venv" / "bin" / "python"
    fake.write_text(f"#!/bin/sh\necho {sys.version_info.major}.{sys.version_info.minor}\n", encoding="utf-8")
    fake.chmod(0o755)
    manifest = manifest_paths.manifest_path(root)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{}", encoding="utf-8")
    base = tmp_path / "base-python"
    base.write_text("", encoding="utf-8")
    monkeypatch.setattr(ic.sys, "_base_executable", str(base), raising=False)
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(sys, "executable", str(fake))  # the activated venv's own python
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_create_state_directory", lambda: None)
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--lightweight", "--rebuild-venv"])
    execs: list = []

    def _execve(path, argv, env):
        execs.append((path, env))
        raise _Spawned

    monkeypatch.setattr(ic.os, "execve", _execve)
    with pytest.raises(_Spawned):
        install._run_lightweight(argparse.Namespace(
            lightweight=True, lightweight_old_path=None, no_containers=True, dev=False, rebuild_venv=True))
    ((path, env),) = execs
    assert path == str(base)
    assert env[ic.ENV_BASE_PYTHON] == str(base) != sys.executable
    assert env[ic.ENV_BASE_PYTHON_VERSION] == f"{sys.version_info.major}.{sys.version_info.minor}"
