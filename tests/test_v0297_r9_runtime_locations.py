# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review R9 — H1, H2, H5, H6, H7: the runtime record is never switched
on the strength of "I could not find it", a confirmed choice is never switched at
all, data is looked for under the ACTUAL volume names, and the boot path never
hangs on the ledger lock.

* H1(a) — a READ-ONLY surface (the resolver, the boot wrapper, the Rust mirrors)
  substitutes the other runtime ONLY on positive evidence (it holds VCO's
  data). "No VCO data anywhere" stays install.py's decision.
* H1(b)/H5 — "installed" means on PATH OR in the usual install locations
  (``vco_lib/tool_search_dirs.toml``, one table for Python and Rust): a
  short-PATH process finds a rootless ``~/bin`` docker or a Homebrew podman and
  DRIVES it (the boot wrappers extend their PATH; the resolver's ``--shell``
  output exports it for the session hooks).
* H2 — ``state/install/runtime.confirmed`` (``install.py --container``) is never
  overridden; ``--container Y`` rewrites both files.
* H6 — the data probe uses the compose defaults PLUS the install's
  ``VCT_*_VOLUME_NAME`` overrides.
* H7 — the boot refusal's ledger write is bounded without coreutils' ``timeout``.

Every runtime is a FAKE (``which``/``run`` doubles or shell stubs on a private
PATH, ``VCT_TOOL_SEARCH_DIRS`` pinned); every install root is a tmp directory.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from tests.common.child_env import child_env
from typing import Optional

import pytest

from vco_lib import atomic
from vco_lib import containers
from vco_lib import runtime_reconcile as rr
from vco_lib import tool_search_dirs as tsd
from vco_lib.deferral_emit import LOCK_REL
from vco_lib.deferral_report import DeferralReport

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SH_WRAPPER = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.sh"
PS1_WRAPPER = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.ps1"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")
POSIX = os.name != "nt"

posix_only = pytest.mark.skipif(not POSIX or BASH is None, reason="POSIX shell runtime stubs")


# ---------------------------------------------------------------------------
# Shared fixtures (the Rust suite runs the same JSON)
# ---------------------------------------------------------------------------

_TSD_FIXTURE = json.loads((FIXTURES / "tool_search_dirs_cases.json").read_text(encoding="utf-8"))
_DIR_CASES = _TSD_FIXTURE["cases"]
_ORDER_CASES = _TSD_FIXTURE["order_cases"]
_VOL_CASES = json.loads((FIXTURES / "vco_volume_names_cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _DIR_CASES, ids=[c["name"] for c in _DIR_CASES])
def test_tool_search_dirs_match_the_shared_fixture(case: dict):
    got = tsd.search_entries(os_name=case["os"], home=case["home"], env=case["env"])
    assert [list(e) for e in got] == case["expect"]
    assert tsd.candidate_dirs(os_name=case["os"], home=case["home"], env=case["env"]) == [
        d for d, _p in case["expect"]]


@pytest.mark.parametrize("case", _ORDER_CASES, ids=[c["name"] for c in _ORDER_CASES])
def test_the_order_rule_matches_the_shared_fixture(case: dict):
    """R10 J3 split: graphical-launch dirs the PATH lacks go AHEAD of it, the
    runtime locations it lacks go AFTER it, a dir already on it never moves —
    and a name resolves along that order. The Rust suite runs the same cases
    (`runtime.rs::tool_search_order_matches_the_shared_fixture`)."""
    entries = tsd.search_entries(os_name=case["os"], home=case["home"], env=case["env"])
    order = tsd.lookup_entries(case["path"], entries)
    assert order == case["expect_path"]
    sep = "\\" if case["os"] == "windows" else "/"
    binaries = set(case["binaries"])
    hit = next((d + sep + case["resolve"] for d in order
                if d + sep + case["resolve"] in binaries), None)
    assert hit == case["expect"]


@pytest.mark.parametrize("case", _VOL_CASES, ids=[c["name"] for c in _VOL_CASES])
def test_volume_names_match_the_shared_fixture(case: dict):
    assert list(rr.volume_names_from(case["env_file"], case["env"])) == case["expect"]


def test_the_volume_name_keys_are_compose_envs_knobs():
    from vco_lib.compose_env import DATA_KNOBS

    assert rr.VOLUME_NAME_KEYS == tuple(pair[1] for pair in DATA_KNOBS.values())
    compose = (REPO_ROOT / "infrastructure" / "docker-compose.yml").read_text(encoding="utf-8")
    for key in rr.VOLUME_NAME_KEYS:
        assert f"name: ${{{key}:-" in compose, key


# ---------------------------------------------------------------------------
# A fake machine with per-runtime container and volume listings
# ---------------------------------------------------------------------------


class Machine:
    """``installed`` / ``up`` per runtime; ``containers`` / ``volumes`` are
    what ``ps -a`` / ``volume ls`` list under each. Records every argv."""

    def __init__(self, *, installed=(), up=(), containers_=None, volumes=None, start_ok=()):
        self.installed = set(installed)
        self.up = set(up)
        self.containers = {k: list(v) for k, v in (containers_ or {}).items()}
        self.volumes = {k: list(v) for k, v in (volumes or {}).items()}
        self.start_ok = set(start_ok)
        self.calls: list[list[str]] = []
        self.started: list[str] = []

    def which(self, name: str) -> Optional[str]:
        return f"/fake/{name}" if name in self.installed else None

    def run(self, argv, **_kw):
        self.calls.append(list(argv))
        rt, *rest = argv
        ok = rt in self.installed
        out = ""
        if rest[:1] == ["info"]:
            ok = ok and rt in self.up
        elif rest[:2] == ["ps", "-a"]:
            ok = ok and rt in self.up
            out = "".join(n + "\n" for n in self.containers.get(rt, ["someone_elses_ollama"]))
        elif rest[:2] == ["volume", "ls"]:
            ok = ok and rt in self.up
            out = "".join(n + "\n" for n in self.volumes.get(rt, ["someone_elses_data"]))
        return subprocess.CompletedProcess(argv, 0 if ok else 1, stdout=out if ok else "", stderr="")

    def start(self, runtime: str):
        self.started.append(runtime)
        if runtime in self.start_ok:
            self.up.add(runtime)
            return True, "started"
        return False, f"{runtime} would not start"


def _root(tmp_path: Path, recorded: Optional[str], confirmed: Optional[str] = None) -> Path:
    root = tmp_path / "clone"
    (root / "state" / "install").mkdir(parents=True)
    if recorded:
        containers.runtime_txt_path(root).write_text(recorded + "\n", encoding="utf-8")
    if confirmed:
        (root / rr.CONFIRMED_REL).write_text(confirmed + "\n", encoding="utf-8")
    return root


def _reconcile(root: Path, m: Machine, *, rewrite: bool = True, env=None):
    return rr.reconcile(root, env=env or {}, which=m.which, run=m.run,
                        start_daemon=m.start, rewrite=rewrite)


# ---------------------------------------------------------------------------
# H1(a) — read-only substitution needs POSITIVE evidence
# ---------------------------------------------------------------------------


def test_read_only_never_switches_when_no_vco_data_exists_anywhere(tmp_path):
    """The boot unit cannot find docker (its PATH), podman answers with nothing
    of VCO's under it: switching would start the stack on EMPTY volumes."""
    root = _root(tmp_path, "docker")
    m = Machine(installed={"podman"}, up={"podman"})
    res = _reconcile(root, m, rewrite=False)
    assert res.outcome is rr.Outcome.UNUSABLE and res.runtime is None
    assert "holds none of VCO's containers or volumes" in res.detail
    assert "python install.py --update" in res.detail
    assert not res.entries and containers.read_runtime_txt(root) == "docker"


def test_install_still_re_records_a_record_with_no_data_anywhere(tmp_path):
    """install.py (rewrite=True, the user's PATH, recorded in the ledger) is the
    one caller that may decide the no-data case — unchanged."""
    root = _root(tmp_path, "docker")
    m = Machine(installed={"podman"}, up={"podman"})
    res = _reconcile(root, m, rewrite=True)
    assert res.outcome is rr.Outcome.REWRITTEN and containers.read_runtime_txt(root) == "podman"
    assert [e.condition_id for e in res.entries] == [rr.CID_RECORD_RECONCILED]


def test_the_resolver_refuses_the_no_data_case_and_says_why(tmp_path):
    root = _root(tmp_path, "docker")
    m = Machine(installed={"podman"}, up={"podman"})
    warnings: list[str] = []
    res = containers.resolve(env={}, which=m.which, run=m.run, warn=warnings.append,
                             home=tmp_path, install_root=root)
    assert res.state is containers.RuntimeState.ABSENT and res.substituted is False
    assert "not switched:" in res.reason and "holds none of VCO's" in res.reason
    assert containers.read_runtime_txt(root) == "docker"


# ---------------------------------------------------------------------------
# H2 — a confirmed choice is never overridden
# ---------------------------------------------------------------------------


def test_a_confirmed_record_is_never_switched_at_install(tmp_path):
    """--container docker, then docker vanished from this PATH while the old
    podman volumes are still there: install records action_required and never
    switches (nor starts podman for a switch that will not happen)."""
    root = _root(tmp_path, "docker", confirmed="docker")
    m = Machine(installed={"podman"}, start_ok={"podman"},
                containers_={"podman": ["vco_weaviate"]}, volumes={"podman": ["vco_weaviate_data"]})
    res = _reconcile(root, m, rewrite=True)
    assert res.outcome is rr.Outcome.UNUSABLE and res.runtime is None
    assert "confirmed choice" in res.detail
    [entry] = res.entries
    assert entry.condition_id == rr.CID_UNUSABLE
    assert "--container podman" in entry.command_to_apply
    assert m.started == []
    assert containers.read_runtime_txt(root) == "docker" and rr.read_confirmed(root) == "docker"


def test_a_confirmed_record_is_never_switched_read_only(tmp_path):
    root = _root(tmp_path, "docker", confirmed="docker")
    m = Machine(installed={"podman"}, up={"podman"},
                containers_={"podman": ["vco_weaviate"]}, volumes={"podman": ["vco_weaviate_data"]})
    res = _reconcile(root, m, rewrite=False)
    assert res.outcome is rr.Outcome.UNUSABLE and not res.entries
    rez = containers.resolve(env={}, which=m.which, run=m.run, warn=lambda _m: None,
                             home=tmp_path, install_root=root)
    assert rez.state is containers.RuntimeState.ABSENT and rez.record_reconciled is False
    assert "confirmed choice" in rez.reason


def test_apply_at_install_defers_containers_for_an_unusable_confirmed_runtime(tmp_path):
    root = _root(tmp_path, "docker", confirmed="docker")
    m = Machine(installed={"podman"}, up={"podman"},
                containers_={"podman": ["vco_weaviate"]}, volumes={"podman": ["vco_weaviate_data"]})
    args = argparse.Namespace(no_containers=False, container=None)
    entries: list = []

    class _Report:
        def add_entry(self, e):
            entries.append(e)

    rr.apply_at_install(root, args, _Report(), start_daemon=m.start,
                        log_event=lambda *a, **k: None, out=lambda _s: None,
                        env={}, which=m.which, run=m.run)
    assert args.no_containers is True and "confirmed choice" in args.containers_deferred
    assert [e.condition_id for e in entries] == [rr.CID_UNUSABLE]
    assert containers.read_runtime_txt(root) == "docker"


def test_changing_your_mind_rewrites_both_files(tmp_path):
    """The one way out of a confirmed choice is another explicit one."""
    root = _root(tmp_path, "podman")
    for choice in ("docker", "podman"):
        args = argparse.Namespace(no_containers=False, container=choice)
        assert rr.apply_at_install(root, args, None, start_daemon=None,
                                   log_event=lambda *a, **k: None) is None
        assert containers.read_runtime_txt(root) == choice and rr.read_confirmed(root) == choice
    m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                volumes={"docker": ["vco_weaviate_data"], "podman": []})
    # podman confirmed, docker holds the data: (c) would re-record — a confirmed
    # choice is kept.
    assert _reconcile(root, m).outcome is rr.Outcome.KEPT


# ---------------------------------------------------------------------------
# H6 — the data probe uses the ACTUAL volume names
# ---------------------------------------------------------------------------


def _infra_env(root: Path, text: str) -> None:
    (root / "infrastructure").mkdir(parents=True, exist_ok=True)
    (root / "infrastructure" / ".env").write_text(text, encoding="utf-8")


def test_an_adopted_volume_under_the_recorded_runtime_is_vco_data(tmp_path):
    """podman recorded; its Weaviate data is the ADOPTED volume `my_weaviate`
    (containers removed); docker holds a stale default-named leftover. Before
    R9 the adopted volume read as "no data" and (c) re-recorded docker — away
    from the real data. Now: data under both → kept + asked."""
    root = _root(tmp_path, "podman")
    _infra_env(root, "VCT_WEAVIATE_VOLUME_NAME=my_weaviate\n")
    m = Machine(installed={"podman", "docker"}, up={"podman", "docker"},
                volumes={"podman": ["my_weaviate"], "docker": ["vco_weaviate_data"]})
    res = _reconcile(root, m)
    assert res.outcome is rr.Outcome.DATA_UNDER_BOTH
    assert containers.read_runtime_txt(root) == "podman"


def test_an_environment_volume_override_counts_too(tmp_path):
    root = _root(tmp_path, "podman")
    m = Machine(installed={"podman"}, up={"podman"}, volumes={"podman": ["shell_named"]})
    assert rr.vco_data_under("podman", run=m.run, install_root=root, env={}) is False
    assert rr.vco_data_under("podman", run=m.run, install_root=root,
                             env={"VCT_OLLAMA_VOLUME_NAME": "shell_named"}) is True


# ---------------------------------------------------------------------------
# H1(b) / H5 — "installed" is judged on PATH AND in the usual install locations
# ---------------------------------------------------------------------------


_OK_STUB = "#!/bin/sh\nexit 0\n"


def _stub(path: Path, body: str = _OK_STUB) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_which_prefers_path_and_falls_back_to_the_table(tmp_path, monkeypatch):
    on_path = tmp_path / "onpath"
    home = tmp_path / "home"
    _stub(home / "bin" / "vct-r9-tool")
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(tsd.ENV_OVERRIDE, "~/bin")
    assert tsd.which("vct-r9-tool") == str(home / "bin" / "vct-r9-tool")
    _stub(on_path / "vct-r9-tool")
    assert tsd.which("vct-r9-tool") == str(on_path / "vct-r9-tool"), "PATH keeps winning"


@posix_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="exercises the Linux table order")
def test_a_rootless_docker_in_home_bin_is_resolved_by_a_short_path_process(tmp_path, monkeypatch):
    """H1's own population: a process (boot unit, hub) whose PATH lacks
    ~/bin, on a machine whose install recorded docker there. Before R9 it read
    docker as NOT INSTALLED — the shape the stale-record reconcile switches
    away from. Now docker is found through the REAL table and driven."""
    home = tmp_path / "home"
    docker = _stub(home / "bin" / "docker")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(tsd.ENV_OVERRIDE, raising=False)
    root = _root(tmp_path, "docker")
    res = containers.resolve(env={}, warn=lambda _m: None, home=home, install_root=root)
    assert res.state is containers.RuntimeState.RESOLVED, res.reason
    assert res.runtime == "docker" and res.substituted is False
    assert res.compose == ["docker", "compose"]
    assert tsd.which("docker") == str(docker)


@posix_only
def test_the_resolver_cli_exports_the_path_that_drives_it(tmp_path, monkeypatch, capsys):
    """The session hooks `eval` `resolve --shell` and then run `$RUNTIME` by
    name: a runtime found only in the table comes with the PATH that reaches it."""
    home = tmp_path / "home"
    _stub(home / "bin" / "docker")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    root = _root(tmp_path, "docker")
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(tsd.ENV_OVERRIDE, "~/bin")
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(root))
    (root / "vco_lib").mkdir()
    (root / ".claude").mkdir()
    monkeypatch.delenv("VCT_CONTAINER_RUNTIME", raising=False)
    assert containers.main(["resolve", "--shell"]) == 0
    out = capsys.readouterr().out
    assert f"PATH={empty}:{home / 'bin'}; export PATH" in out
    assert "VCO_RUNTIME=docker" in out
    assert containers.main(["resolve", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["search_path"] == f"{empty}:{home / 'bin'}"


def test_run_spawns_a_tool_found_only_in_the_table(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _stub(home / "bin" / "vct-r9-echo", "#!/bin/sh\necho \"ran:$PATH\"\n")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(tsd.ENV_OVERRIDE, "~/bin")
    if not POSIX:
        pytest.skip("POSIX stub")
    res = tsd.run(["vct-r9-echo"], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0 and res.stdout.startswith("ran:")
    assert str(home / "bin") in res.stdout


def _real_table(monkeypatch, *, path: Path, home: Path) -> None:
    """This process sees the REAL table (not the suite's empty override),
    with a tmp HOME and a one-directory PATH."""
    monkeypatch.setenv("PATH", str(path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(tsd.ENV_OVERRIDE, raising=False)


@posix_only
def test_a_graphical_launch_prefers_the_login_shells_dir_on_every_python_surface(
        tmp_path, monkeypatch):
    """R10 J3 split, on a real filesystem: ``~/.local/bin`` is a v0.2.53
    graphical-launch dir (``prepend-when-missing`` on Linux and macOS). A
    short PATH that lacks it resolves the name THERE — ahead of the PATH's own
    same-named binary, as the user's login shell and the launcher's augment
    do — and ``which``, ``run`` and ``reachable_path`` agree on it."""
    home = tmp_path / "home"
    system = tmp_path / "system-bin"
    _stub(system / "vct-r10-tool", "#!/bin/sh\necho system\n")
    user = _stub(home / ".local" / "bin" / "vct-r10-tool", "#!/bin/sh\necho user:$PATH\n")
    _real_table(monkeypatch, path=system, home=home)
    assert tsd.which("vct-r10-tool") == str(user)
    res = tsd.run(["vct-r10-tool"], capture_output=True, text=True, timeout=10)
    assert res.stdout.startswith(f"user:{home / '.local' / 'bin'}:{system}"), res.stdout
    reach = tsd.reachable_path(("vct-r10-tool",))
    assert reach == f"{home / '.local' / 'bin'}:{system}"
    assert shutil.which("vct-r10-tool", path=reach) == str(user)


@posix_only
def test_an_appended_runtime_location_never_shadows_the_path(tmp_path, monkeypatch):
    """R10 J3 split: ``~/bin`` is a v0.2.97 runtime location (``append``). A
    same-named binary on the PATH keeps winning on every Python surface; a
    name only ``~/bin`` holds is still found, and the PATH that drives it
    APPENDS ``~/bin``."""
    home = tmp_path / "home"
    inherited = tmp_path / "inherited"
    mine = _stub(inherited / "vct-r10-docker", "#!/bin/sh\necho inherited\n")
    _stub(home / "bin" / "vct-r10-docker", "#!/bin/sh\necho table\n")
    only = _stub(home / "bin" / "vct-r10-only", "#!/bin/sh\necho only\n")
    _real_table(monkeypatch, path=inherited, home=home)
    assert tsd.which("vct-r10-docker") == str(mine)
    assert tsd.run(["vct-r10-docker"], capture_output=True, text=True,
                   timeout=10).stdout.strip() == "inherited"
    assert tsd.reachable_path(("vct-r10-docker",)) is None
    assert tsd.which("vct-r10-only") == str(only)
    assert tsd.reachable_path(("vct-r10-docker", "vct-r10-only")) == f"{inherited}:{home / 'bin'}"


@posix_only
def test_a_graphical_launch_dir_already_on_path_is_not_moved(tmp_path, monkeypatch):
    """``prepend-when-missing`` applies only to a MISSING dir: an inherited
    ``~/.local/bin`` late on the PATH stays late, so the PATH's earlier binary
    wins."""
    home = tmp_path / "home"
    first = tmp_path / "first"
    earlier = _stub(first / "vct-r10-tool")
    _stub(home / ".local" / "bin" / "vct-r10-tool")
    _real_table(monkeypatch, path=first, home=home)
    monkeypatch.setenv("PATH", f"{first}:{home / '.local' / 'bin'}")
    assert tsd.which("vct-r10-tool") == str(earlier)
    assert tsd.reachable_path(("vct-r10-tool",)) is None


# ---------------------------------------------------------------------------
# The boot wrappers (bash + pwsh) — main() end to end on stub runtimes
# ---------------------------------------------------------------------------

_COREUTILS = ("head", "tr", "grep", "sleep", "date", "cat", "uname", "dirname",
              "sed", "tail", "rm", "readlink", "bash", "env", "mkdir", "touch")


def _fake_bin(tmp_path: Path, *, with_timeout: bool) -> Path:
    fake = tmp_path / "bin"
    fake.mkdir(parents=True, exist_ok=True)
    names = _COREUTILS + (("timeout",) if with_timeout else ())
    for name in names:
        real = shutil.which(name)
        if real and not (fake / name).exists():
            (fake / name).symlink_to(real)
    return fake


def _clone(tmp_path: Path, script: Path) -> tuple[Path, Path]:
    root = tmp_path / "clone"
    (root / "scripts").mkdir(parents=True)
    copy = root / "scripts" / script.name
    shutil.copy2(script, copy)
    (root / "vco_lib").symlink_to(REPO_ROOT / "vco_lib", target_is_directory=True)
    (root / "infrastructure").mkdir()
    (root / "infrastructure" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "state" / "install").mkdir(parents=True)
    return root, copy


def _wrapper_env(tmp_path: Path, fake: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_CONTAINER_RUNTIME", "VCT_STACK_RUNTIME_FILE", "VCT_ORCHESTRATOR_ROOT",
                        "VCT_STACK_WORKING_DIR", "VCO_COMPOSE_SERVICES", "VCT_INSTALL_ROOT")}
    env.update({"PATH": str(fake), "HOME": str(tmp_path / "home"),
                "VCO_VENV_PYTHON": sys.executable, "PYTHONPATH": str(REPO_ROOT),
                "VCT_STATE_DIR": str(tmp_path / "vct-state"),
                "VCT_LAUNCHER_DB_PATH": str(tmp_path / "no-launcher.db"),
                "VCT_STACK_LOG_FILE": str(tmp_path / "stack.log"),
                tsd.ENV_OVERRIDE: ""})
    env.update(extra)
    return env


# podman up; logs every argv; `ps` / `volume ls` list nothing of VCO's.
_PODMAN_LOGGING = ('#!/usr/bin/env bash\necho "$*" >> "$HOME/podman.argv"\n'
                   'case "$1" in ps) echo someone_elses_ollama ;; volume) echo someone_elses_data ;; esac\n'
                   'exit 0\n')


def _run_wrapper(cmd: list, env: dict, cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(cwd))


def _sh(script: Path) -> list:
    return [BASH or "bash", str(script)]


def _ps1(script: Path) -> list:
    return [PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script)]


_SHELLS = [
    pytest.param(_sh, SH_WRAPPER, id="bash",
                 marks=pytest.mark.skipif(not POSIX or BASH is None, reason="bash wrapper")),
    pytest.param(_ps1, PS1_WRAPPER, id="pwsh",
                 marks=pytest.mark.skipif(not POSIX or BASH is None or PWSH is None,
                                          reason="pwsh + bash stubs")),
]


@pytest.mark.parametrize("launcher,wrapper", _SHELLS)
def test_the_boot_wrapper_never_starts_the_other_runtime_on_empty_volumes(tmp_path, launcher, wrapper):
    """H1: the record names docker, which this boot cannot find; podman answers
    with none of VCO's data. Before R9 the read-only reconcile answered podman
    and the wrapper brought the stack up on podman's EMPTY volumes. Now: exit 3,
    the refusal in the ledger, and podman never asked to compose anything."""
    root, script = _clone(tmp_path, wrapper)
    containers.runtime_txt_path(root).write_text("docker\n", encoding="utf-8")
    fake = _fake_bin(tmp_path, with_timeout=True)
    _stub(fake / "podman", _PODMAN_LOGGING)
    (tmp_path / "home").mkdir()
    proc = _run_wrapper(launcher(script), _wrapper_env(tmp_path, fake), tmp_path)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    argv_log = (tmp_path / "home" / "podman.argv")
    logged = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    assert "compose" not in logged and "start" not in logged.split(), logged
    [entry] = DeferralReport.read(root).entries
    assert entry.condition_id == rr.CID_UNUSABLE
    assert containers.read_runtime_txt(root) == "docker"


@pytest.mark.parametrize("launcher,wrapper", _SHELLS)
def test_the_boot_wrapper_drives_a_runtime_found_only_in_a_user_local_dir(tmp_path, launcher, wrapper):
    """H5: the boot service's PATH lacks the directory podman is installed in
    (~/bin here; /opt/homebrew/bin on Apple Silicon). Before R9 it exited 3 and
    wrote "not usable" at every boot while every session used podman fine. Now
    the wrapper extends its PATH from the shared table and composes with it —
    and on a PATH WITHOUT coreutils' `timeout`, the macOS default."""
    root, script = _clone(tmp_path, wrapper)
    containers.runtime_txt_path(root).write_text("podman\n", encoding="utf-8")
    fake = _fake_bin(tmp_path, with_timeout=False)
    home = tmp_path / "home"
    _stub(home / "bin" / "podman", _PODMAN_LOGGING)
    proc = _run_wrapper(launcher(script), _wrapper_env(tmp_path, fake, **{tsd.ENV_OVERRIDE: "~/bin"}),
                        tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    logged = (home / "podman.argv").read_text(encoding="utf-8")
    assert "compose" in logged and " up " in f" {logged} ", logged
    assert DeferralReport.read(root).entries == []


def _hold_ledger_lock(root: Path):
    import fcntl

    lock = root / LOCK_REL
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")  # noqa: SIM115 — released by the caller
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


@pytest.mark.parametrize("launcher,wrapper", _SHELLS)
def test_the_boot_refusal_never_hangs_behind_the_ledger_lock(tmp_path, launcher, wrapper):
    """H7: an update holds the deferral lock while the boot wrapper exits 3, on a
    host without coreutils' `timeout`. Before R9 the ledger write blocked on
    `flock(LOCK_EX)` for as long as the update held it (unbounded there). Now
    the emitter gives up after BOOT_LEDGER_LOCK_TIMEOUT_S and boot finishes."""
    root, script = _clone(tmp_path, wrapper)
    containers.runtime_txt_path(root).write_text("docker\n", encoding="utf-8")
    fake = _fake_bin(tmp_path, with_timeout=False)
    (tmp_path / "home").mkdir()
    fh = _hold_ledger_lock(root)
    try:
        t0 = time.monotonic()
        proc = _run_wrapper(launcher(script), _wrapper_env(tmp_path, fake), tmp_path, timeout=90)
        elapsed = time.monotonic() - t0
    finally:
        fh.close()
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert elapsed < 60, elapsed
    assert DeferralReport.read(root).entries == [], "skipped, never written unlocked"


@posix_only
def test_bounded_runs_without_coreutils_timeout(tmp_path):
    """The wrapper's own bound (and `_runtime_usable`'s 5 s probe) must work on
    macOS, which ships no `timeout`: before R9 `timeout 5 podman info` was
    "command not found" there, so every runtime read as unusable at boot."""
    fake = _fake_bin(tmp_path, with_timeout=False)
    _stub(fake / "podman", "#!/usr/bin/env bash\nexit 0\n")
    snippet = (f'set +e; source "{SH_WRAPPER}"; log() {{ :; }}; '
               '_bounded 1 sleep 20; echo "cut=$?"; '
               '_runtime_usable podman && echo usable || echo unusable')
    t0 = time.monotonic()
    proc = subprocess.run([BASH or "bash", "-c", snippet], capture_output=True, text=True, timeout=30,
                          env={"PATH": str(fake), "HOME": str(tmp_path)})
    assert time.monotonic() - t0 < 15
    lines = proc.stdout.split()
    assert lines[0] != "cut=0" and lines[0].startswith("cut="), proc.stdout + proc.stderr
    assert lines[1] == "usable", proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# H7 — the bounded lock itself
# ---------------------------------------------------------------------------


@posix_only
def test_a_bounded_lock_gives_up_instead_of_waiting(tmp_path):
    lock = tmp_path / "x.lock"
    import fcntl

    fh = open(lock, "a+")  # noqa: SIM115
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        with pytest.raises(atomic.LockTimeout):
            with atomic.exclusive_file_lock(lock, timeout_s=0.3):
                pytest.fail("the block must not run without the lock")
        assert time.monotonic() - t0 < 3
    finally:
        fh.close()
    with atomic.exclusive_file_lock(lock, timeout_s=0.3):
        pass  # free now: taken at once


@posix_only
def test_record_boot_refusal_skips_rather_than_waits(tmp_path, monkeypatch):
    root = _root(tmp_path, "docker")
    monkeypatch.setattr(rr, "BOOT_LEDGER_LOCK_TIMEOUT_S", 0.5)
    fh = _hold_ledger_lock(root)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("wrote", rr.record_boot_refusal(root, "x", env={})),
        daemon=True)
    try:
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "record_boot_refusal waited on the held ledger lock"
    finally:
        fh.close()
        worker.join(timeout=30)
    assert result["wrote"] is False and DeferralReport.read(root).entries == []
    assert rr.record_boot_refusal(root, "x", env={}) is True


# ---------------------------------------------------------------------------
# H7 — `record-boot-refusal` bounds ITSELF (every caller dropped `timeout`)
# ---------------------------------------------------------------------------


@posix_only
def test_the_record_refusal_cli_is_bounded_with_the_ledger_lock_held(tmp_path):
    """The CLI every caller runs (the boot wrappers, ensure-containers,
    verify-container-ports — none with a `timeout` of its own) returns while
    ANOTHER process holds the ledger lock, and writes nothing unlocked."""
    root = _root(tmp_path, "docker")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys, time\n"
         f"fh = open({str(root / LOCK_REL)!r}, 'a+')\n"
         "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(120)\n"],
        stdout=subprocess.PIPE, text=True, env=child_env())
    (root / LOCK_REL).parent.mkdir(parents=True, exist_ok=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "vco_lib.runtime_reconcile", "record-boot-refusal",
             "--root", str(root), "--source", "session", "--reason", "pinned to docker"],
            capture_output=True, text=True, timeout=90,
            env=child_env())
        elapsed = time.monotonic() - t0
    finally:
        holder.kill()
        holder.wait()
    assert proc.returncode == 0, proc.stderr
    assert elapsed < rr.RECORD_REFUSAL_DEADLINE_S + 5, elapsed
    assert DeferralReport.read(root).entries == []


def test_the_refusal_deadline_ends_a_stalled_write(tmp_path, monkeypatch):
    """Whatever stalls (not only the lock), the call ends at its deadline."""
    release = threading.Event()
    monkeypatch.setattr(rr, "record_boot_refusal",
                        lambda *_a, **_k: release.wait(30))
    t0 = time.monotonic()
    try:
        assert rr.record_refusal_bounded(tmp_path, "x", deadline_s=0.3) is False
        assert time.monotonic() - t0 < 5
    finally:
        release.set()
    assert rr.record_refusal_bounded(tmp_path, "x", deadline_s=5) is True


def test_a_session_refusal_says_a_session_hook_started_nothing(tmp_path):
    root = _root(tmp_path, "docker")
    assert rr.record_boot_refusal(root, "pinned to docker", env={}, source="session") is True
    [entry] = DeferralReport.read(root).entries
    # R10 J4: the condition's ONE title (it used to name the surface and flip
    # with whoever wrote last); the surface is in `detected`.
    assert entry.title == "Container runtime docker is not usable — containers were not started"
    assert entry.detected.startswith("A session-start hook started nothing")


def _installed_clone(tmp_path: Path) -> Path:
    """An install root `resolve_install_root` accepts (vco_lib/ + .claude/)
    with `state/install/` — so the ledger is written — and no record."""
    root = tmp_path / "installed"
    (root / "state" / "install").mkdir(parents=True)
    (root / "vco_lib").mkdir()
    (root / ".claude").mkdir()
    return root


@pytest.mark.parametrize("hook", ["ensure-containers", "verify-container-ports"])
@pytest.mark.parametrize("shell", [
    pytest.param("bash", marks=pytest.mark.skipif(not POSIX or BASH is None, reason="bash hook")),
    pytest.param("pwsh", marks=pytest.mark.skipif(not POSIX or BASH is None or PWSH is None,
                                                  reason="pwsh + bash stubs")),
])
def test_a_session_hook_records_a_refused_pin_in_the_installs_ledger(tmp_path, hook, shell):
    """R9 H4/H7 wiring, end to end: the pinned docker refuses (a fake that fails
    every call, first on PATH); the hook prints the refusal AND the entry lands
    in the ledger of the clone whose record the resolver read — through the
    self-bounded CLI, with no `timeout` binary anywhere on the path."""
    from tests.test_v0297_lifecycle_hooks import SENTINEL_MANAGED, _Machine

    m = _Machine(tmp_path, SENTINEL_MANAGED, {})
    _stub(m.bin / "docker", "#!/usr/bin/env bash\nexit 1\n")
    root = _installed_clone(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    script = REPO_ROOT / "templates" / "hooks" / f"{hook}.{'sh' if shell == 'bash' else 'ps1'}"
    argv = ([BASH or "bash", str(script)] if shell == "bash" else
            [PWSH or "pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    env = m.env(VCT_CONTAINER_RUNTIME="docker", VCT_INSTALL_ROOT=str(root),
                CLAUDE_PROJECT_DIR=str(project))
    env[tsd.ENV_OVERRIDE] = ""
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180,
                          cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VCT_CONTAINER_RUNTIME=docker" in proc.stdout, proc.stdout + proc.stderr
    deadline = time.monotonic() + 30  # the hook may relay a detached child
    entries: list = []
    while time.monotonic() < deadline:
        entries = DeferralReport.read(root).entries
        if entries:
            break
        time.sleep(0.2)
    assert [e.condition_id for e in entries] == [rr.CID_UNUSABLE], proc.stdout + proc.stderr
    assert entries[0].detected.startswith("A session-start hook started nothing")


SLOW_RECORD_S = 20.0  # a record slower than any foreground hook budget


@pytest.mark.parametrize("shell", [
    pytest.param("bash", marks=pytest.mark.skipif(not POSIX or BASH is None, reason="bash hook")),
    pytest.param("pwsh", marks=pytest.mark.skipif(not POSIX or BASH is None or PWSH is None,
                                                  reason="pwsh + bash stubs")),
])
def test_a_slow_refusal_record_never_blocks_the_watchdogs_foreground(tmp_path, shell):
    """R10 J5 — verify-container-ports FIRES the refused-pin record
    detached; its foreground returns (with the refusal line on stdout) even
    when the record is slower than the hook's whole 30 s budget. Before J5
    the record ran in the FOREGROUND after a resolve that can spend ~30 s
    probing: a slow ledger lock plus the record could push the hook past its
    timeout, and Claude Code discarded the output — the refusal line
    included.

    The slow record is injected through the VCT_VENV tier-1 interpreter
    seam (same shape as ``_plan_failing_python``): the wrapper stalls ONLY
    ``record-boot-refusal`` by 20 s, then delegates to the real CLI — so the
    ledger entry this test finally reads is written by the REAL emitter,
    proving the detached call completes after the hook has returned."""
    from tests.test_v0297_lifecycle_hooks import SENTINEL_MANAGED, _Machine

    m = _Machine(tmp_path, SENTINEL_MANAGED, {})
    _stub(m.bin / "docker", "#!/usr/bin/env bash\nexit 1\n")
    root = _installed_clone(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    wrapper = m.bin / "vco-slow-record-python"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-m" ] && [ "$2" = "vco_lib.runtime_reconcile" ] '
        '&& [ "$3" = "record-boot-refusal" ]; then\n'
        f"    sleep {int(SLOW_RECORD_S)}\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    script = REPO_ROOT / "templates" / "hooks" / f"verify-container-ports.{('sh' if shell == 'bash' else 'ps1')}"
    argv = ([BASH or "bash", str(script)] if shell == "bash" else
            [PWSH or "pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    env = m.env(VCT_CONTAINER_RUNTIME="docker", VCT_INSTALL_ROOT=str(root),
                CLAUDE_PROJECT_DIR=str(project), VCT_VENV=str(wrapper))
    env[tsd.ENV_OVERRIDE] = ""
    t0 = time.monotonic()
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180,
                          cwd=str(REPO_ROOT))
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VCT_CONTAINER_RUNTIME=docker" in proc.stdout, proc.stdout + proc.stderr
    assert elapsed < SLOW_RECORD_S, (
        f"the foreground waited {elapsed:.1f}s for a {SLOW_RECORD_S:.0f}s record — "
        "the record is not detached (R10 J5)")
    # The detached record still lands (it sleeps SLOW_RECORD_S first, then
    # the real CLI writes the installed clone's ledger).
    deadline = time.monotonic() + SLOW_RECORD_S + 30
    entries: list = []
    while time.monotonic() < deadline:
        entries = DeferralReport.read(root).entries
        if entries:
            break
        time.sleep(0.2)
    assert [e.condition_id for e in entries] == [rr.CID_UNUSABLE], proc.stdout + proc.stderr


# Tools a session hook may call by name. The hermetic PATH below holds ONLY
# these (symlinked) plus the fake machine's stubs — never /usr/bin, so the
# host's real podman/docker cannot be reached by name.
_HOOK_TOOLS = ("bash", "env", "head", "tail", "tr", "grep", "sed", "awk", "cut", "sort", "uniq",
               "wc", "cat", "date", "sleep", "mkdir", "rm", "mv", "cp", "touch", "mktemp",
               "dirname", "basename", "readlink", "realpath", "uname", "id", "ls", "tee", "stat",
               "printf", "flock", "timeout", "kill", "ps", "chmod", "ln", "find", "xargs")


@pytest.mark.parametrize("shell", [
    pytest.param("bash", marks=pytest.mark.skipif(not POSIX or BASH is None, reason="bash hook")),
    pytest.param("pwsh", marks=pytest.mark.skipif(not POSIX or BASH is None or PWSH is None,
                                                  reason="pwsh + bash stubs")),
])
def test_a_session_hook_drives_a_runtime_found_only_in_a_user_local_dir(tmp_path, shell):
    """H5 for the session hooks: podman lives in ~/bin, which the hook's PATH
    lacks. The resolver finds it through the table and hands back the PATH that
    reaches it (`resolve --shell` exports it; the .ps1 applies `search_path`),
    so the hook's own `podman rm -f` of a managed zombie runs — instead of the
    session calling an installed runtime absent."""
    from tests.test_v0297_lifecycle_hooks import SENTINEL_MANAGED, _Machine

    m = _Machine(tmp_path, SENTINEL_MANAGED, {"vco_weaviate": "zombie"})
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    shutil.move(str(m.bin / "podman"), str(home / "bin" / "podman"))
    for tool in _HOOK_TOOLS:
        real = shutil.which(tool)
        if real and not (m.bin / tool).exists():
            (m.bin / tool).symlink_to(real)
    for py in ("python3", "python"):  # the hooks' find-python ladder
        (m.bin / py).symlink_to(sys.executable)
    project = tmp_path / "project"
    project.mkdir()
    script = REPO_ROOT / "templates" / "hooks" / f"verify-container-ports.{'sh' if shell == 'bash' else 'ps1'}"
    argv = ([str(m.bin / "bash"), str(script)] if shell == "bash" else
            [PWSH or "pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    env = m.env(CLAUDE_PROJECT_DIR=str(project), HOME=str(home))
    env["PATH"] = str(m.bin)
    env[tsd.ENV_OVERRIDE] = "~/bin"
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180,
                          cwd=str(REPO_ROOT))
    assert ["rm", "-f", "vco_weaviate"] in m.runtime_calls(), proc.stdout + proc.stderr


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
