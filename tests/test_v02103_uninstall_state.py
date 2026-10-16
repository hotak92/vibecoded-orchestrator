# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Uninstall field run 2026-10-16 (Windows 11): hub/launcher stopped BEFORE
state removal (step [3]), the WHOLE VCO state dir removed (step [8]) and
legacy VCO containers ``compose down`` cannot reach (step [1b]).

Every test runs against a temp HOME / VCT state dir, a scripted container
runtime and a scripted process table: nothing on the real machine is read,
stopped or removed."""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from vco_lib import containers  # noqa: E402
from vco_lib import uninstall_artifacts as ua  # noqa: E402
from vco_lib import uninstall_containers as uc  # noqa: E402
from vco_lib import uninstall_quiesce as uq  # noqa: E402
from vco_lib import uninstall_state as us  # noqa: E402


def _state(root: Path) -> Path:
    """A realistic VCO state dir."""
    (root / "retrieval_rl_data" / "proj").mkdir(parents=True)
    (root / "retrieval_rl_data" / "proj" / "events.jsonl").write_text('{"q": 1}\n')
    for name in ("launcher.db", "launcher.db-wal", "launcher.db-shm", "hub.db", "hub.pid",
                 "hub.token", "model-gateway.token"):
        (root / name).write_text("x")
    (root / "logs").mkdir()
    (root / "logs" / "model-gateway.log").write_text("log")
    (root / "state" / "codegraph-walk-claims").mkdir(parents=True)
    (root / "state" / "codegraph-walk-claims" / "a.lock").write_text("")
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".vct-secrets" / "shared").mkdir(parents=True)
    (h / ".vct-secrets" / "shared" / "github_pat").write_text("secret")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("VCT_SECRETS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


# ── step [8]: the whole state dir ─────────────────────────────────────────


def test_the_whole_state_dir_goes_rl_data_and_db_siblings_included(home):
    root = _state(home / ".vct")
    plan = us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db")
    assert plan.refused is None and plan.rl_bytes and plan.file_count >= 10
    audit = us.execute(plan)
    assert not root.exists(), audit
    assert (home / ".vct-secrets" / "shared" / "github_pat").read_text() == "secret"


def test_dry_run_lines_name_the_dir_its_size_and_the_rl_data(home):
    root = _state(home / ".vct")
    (root / "license.key").write_text("k")
    plan = us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db")
    text = "\n".join(us.plan_lines(plan))
    assert str(root) in text and " MB" in text and "files)" in text
    assert "RL training data" in text and "retrieval_rl_data" in text
    assert "license.key" in text
    assert (root / "retrieval_rl_data" / "proj" / "events.jsonl").exists()


def test_a_launcher_db_outside_the_state_dir_goes_with_its_siblings(home, tmp_path):
    root = _state(home / ".vct")
    db = tmp_path / "elsewhere" / "launcher.db"
    db.parent.mkdir()
    for p in (db, db.with_name("launcher.db-wal"), db.with_name("launcher.db-shm")):
        p.write_text("x")
    (db.parent / "unrelated.txt").write_text("keep")
    plan = us.build_plan(root=root, home=home, env={}, launcher_db=db)
    us.execute(plan)
    assert not db.exists() and not db.with_name("launcher.db-wal").exists()
    assert not db.with_name("launcher.db-shm").exists()
    assert (db.parent / "unrelated.txt").exists()


@pytest.mark.parametrize("case", ["home", "fsroot", "ancestor_of_home", "is_secrets",
                                  "contains_secrets", "contains_clone", "foreign_name",
                                  "explicit_without_markers"])
def test_a_dir_that_is_not_plainly_vcos_is_refused(home, tmp_path, monkeypatch, case):
    clone = tmp_path / "work" / "vco"
    clone.mkdir(parents=True)
    root = {
        "home": home, "fsroot": Path(os.path.abspath(os.sep)), "ancestor_of_home": home.parent,
        "is_secrets": home / ".vct-secrets", "contains_secrets": tmp_path / "state-a",
        "contains_clone": tmp_path / "work", "foreign_name": tmp_path / "state",
        "explicit_without_markers": tmp_path / "Documents",
    }[case]
    root.mkdir(parents=True, exist_ok=True)
    if case in ("home", "ancestor_of_home", "contains_secrets", "contains_clone"):
        (root / "launcher.db").write_text("x")  # VCO markers do not override the rule
    if case == "contains_secrets":
        (root / "sec").mkdir()
        monkeypatch.setenv("VCT_SECRETS_DIR", str(root / "sec"))
    if case in ("foreign_name", "explicit_without_markers"):
        (root / "mine.txt").write_text("user")
    env = {} if case == "foreign_name" else {"VCT_STATE_DIR": str(root)}
    plan = us.build_plan(root=root, home=home, env=env, clone_root=clone,
                         launcher_db=tmp_path / "no-db" / "launcher.db")
    why = {"home": "home directory", "fsroot": "filesystem or drive root",
           "ancestor_of_home": "home directory", "is_secrets": "is or contains",
           "contains_secrets": "is or contains", "contains_clone": "is or contains",
           "foreign_name": "not named .vct", "explicit_without_markers": "none of VCO's"}[case]
    assert plan.refused and why in plan.refused, (case, plan.refused)
    assert "[refused]" in "\n".join(us.plan_lines(plan))
    us.execute(plan)
    assert (home / ".vct-secrets" / "shared" / "github_pat").exists()
    assert clone.is_dir()
    if case in ("foreign_name", "explicit_without_markers"):
        assert (root / "mine.txt").exists()
    if case in ("home", "ancestor_of_home", "contains_secrets", "contains_clone"):
        assert (root / "launcher.db").exists()  # launcher_db is elsewhere here


def test_an_explicit_state_dir_with_vco_files_is_accepted(home, tmp_path):
    root = _state(tmp_path / "custom-state")
    plan = us.build_plan(root=root, home=home, env={"VCT_STATE_DIR": str(root)},
                         launcher_db=root / "launcher.db")
    assert plan.refused is None


def test_a_refused_dir_still_loses_launcher_db_and_its_siblings_by_name(home, tmp_path):
    """Refusal falls back to the exact names the old step [3] removed —
    launcher.db now WITH its -wal/-shm — and nothing else in the dir."""
    root = home  # VCT_STATE_DIR=~ : refused
    db = root / "launcher.db"
    for p in (db, db.with_name("launcher.db-wal"), db.with_name("launcher.db-shm")):
        p.write_text("x")
    (root / "thesis.docx").write_text("user")
    plan = us.build_plan(root=root, home=home, env={"VCT_STATE_DIR": str(root)}, launcher_db=db)
    assert plan.refused and set(plan.db_files) == {db, db.with_name("launcher.db-wal"),
                                                   db.with_name("launcher.db-shm")}
    us.execute(plan)
    assert not any(p.exists() for p in plan.db_files)
    assert (root / "thesis.docx").exists() and (root / ".vct-secrets").is_dir()


def test_a_symlink_inside_is_removed_never_followed(home, tmp_path):
    root = _state(home / ".vct")
    outside = tmp_path / "user-data"
    outside.mkdir()
    (outside / "precious.txt").write_text("user")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    us.execute(us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db"))
    assert (outside / "precious.txt").read_text() == "user"
    assert not (root / "linked").is_symlink()


def test_a_junction_inside_is_never_walked(home, monkeypatch):
    """A junction is_symlink() misses: the walk consults _is_reparse and never
    descends into it."""
    root = _state(home / ".vct")
    junction = root / "junction"
    junction.mkdir()
    (junction / "target-file.txt").write_text("user")
    real = us._is_reparse
    monkeypatch.setattr(us, "_is_reparse", lambda p: p == junction or real(p))
    unlinked: list = []
    monkeypatch.setattr(us, "_unlink_link", lambda p: unlinked.append(p))
    us.execute(us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db"))
    assert unlinked == [junction]
    assert (junction / "target-file.txt").exists()


def test_an_in_use_file_is_reported_and_the_rest_still_goes(home, monkeypatch):
    root = _state(home / ".vct")
    real = Path.unlink

    def locked(self, *a, **k):
        if self.name == "hub.db":
            raise PermissionError("in use by another process")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", locked)
    audit = us.execute(us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db"))
    assert any("WARN" in a and "hub.db" in a and "in use" in a for a in audit)
    assert (root / "hub.db").exists()
    assert not (root / "launcher.db").exists() and not (root / "retrieval_rl_data").exists()


def test_kg_reviewer_backups_are_kept(home):
    root = _state(home / ".vct")
    kept = root / "backups" / "kg-reviewer-outside" / "20261016"
    kept.mkdir(parents=True)
    (kept / "copy.md").write_text("user bytes")
    audit = us.execute(us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db"))
    assert (kept / "copy.md").exists()
    assert not (root / "launcher.db").exists()
    assert any(a.startswith("left in place") for a in audit)


# ── step [3]: hub + launcher ──────────────────────────────────────────────


def test_stop_hub_waits_until_the_hub_is_gone(tmp_path):
    hub = tmp_path / "vct-hub"
    hub.write_text("")
    state = {"alive": True, "polls": 0}

    def running():
        if state["polls"] >= 3:
            state["alive"] = False
        state["polls"] += 1
        return state["alive"]

    calls: list = []
    audit = uq.stop_hub([None, hub], hub_running=running,
                        runner=lambda cmd, timeout=15: calls.append(cmd) or 0, sleep=lambda s: None)
    assert calls == [[str(hub), "--stop"]]
    assert audit == [f"stopped vct-hub via `{hub} --stop`"]


def test_stop_hub_without_a_binary_says_so(tmp_path):
    audit = uq.stop_hub([tmp_path / "missing"], hub_running=lambda: True, runner=lambda *a, **k: 0)
    assert audit and audit[0].startswith("WARN") and "no vct-hub binary" in audit[0]


def _procs(*rows):
    return lambda: [uq.ProcInfo(pid, name, Path(exe) if exe else None) for pid, name, exe in rows]


def test_the_launcher_is_stopped_only_when_its_exe_is_a_vco_path(tmp_path):
    ours = tmp_path / "LocalAppData" / "vct-launcher" / "vct-launcher.exe"
    elsewhere = tmp_path / "Other" / "vct-launcher.exe"
    stopped: list = []
    audit = uq.stop_launchers(
        [ours], interactive=False, confirm=lambda m: True, wait_for_user=lambda m: None,
        lister=_procs((101, "vct-launcher.exe", ours), (202, "vct-launcher.exe", elsewhere),
                      (303, "vct-launcher", None), (404, "notepad.exe", tmp_path / "notepad.exe")),
        stopper=lambda pid, exe: stopped.append(pid) or True,
    )
    assert stopped == [101]
    assert sum("not stopped" in a for a in audit) == 2
    assert any("stopped VCO launcher" in a and "101" in a for a in audit)


def test_interactive_the_user_is_asked_to_close_it_first(tmp_path):
    ours = tmp_path / "vct-launcher"
    seen = {"n": 0}

    def lister():
        seen["n"] += 1
        return [uq.ProcInfo(7, "vct-launcher", ours)] if seen["n"] == 1 else []

    asked: list = []
    stopped: list = []
    audit = uq.stop_launchers([ours], interactive=True, confirm=lambda m: True,
                              wait_for_user=asked.append, lister=lister,
                              stopper=lambda pid, exe: stopped.append(pid) or True)
    assert asked and "Close it" in asked[0]
    assert stopped == [] and audit[-1] == "VCO launcher closed by the user"


@pytest.mark.parametrize("answer", [True, False])
def test_interactive_a_launcher_left_open_is_stopped_only_on_yes(tmp_path, answer):
    ours = tmp_path / "vct-launcher"
    stopped: list = []
    audit = uq.stop_launchers([ours], interactive=True, confirm=lambda m: answer,
                              wait_for_user=lambda m: None,
                              lister=_procs((7, "vct-launcher", ours)),
                              stopper=lambda pid, exe: stopped.append(pid) or True)
    assert stopped == ([7] if answer else [])
    assert answer or "declined" in audit[-1]


def test_a_process_table_that_cannot_be_read_is_reported(tmp_path):
    def broken():
        raise RuntimeError("psutil is not importable")

    audit = uq.stop_launchers([tmp_path / "x"], interactive=False, confirm=lambda m: True,
                              wait_for_user=lambda m: None, lister=broken)
    assert audit[0].startswith("WARN") and "psutil" in audit[0]


def test_launcher_paths_cover_the_install_locations(tmp_path):
    home = tmp_path / "home"
    clone = tmp_path / "clone"
    (clone / "launcher" / "dist" / "windows-x64").mkdir(parents=True)
    win = ua.launcher_exe_paths(os_name="Windows", home=home, clone_root=clone,
                                env={"LOCALAPPDATA": str(tmp_path / "L")})
    assert tmp_path / "L" / "vct-launcher" / "vct-launcher.exe" in win
    assert clone / "launcher" / "dist" / "windows-x64" / "vct-launcher.exe" in win
    lin = ua.launcher_exe_paths(os_name="Linux", home=home, clone_root=clone, env={})
    assert home / ".local" / "share" / "vct-launcher" / "vct-launcher" in lin


# ── end to end through install._run_uninstall ─────────────────────────────


def _run(monkeypatch, tmp_path, home, *, dry_run, events, gateway_restarts=False,
         yes=True, tty=None, answers=None):
    root = _state(home / ".vct")
    monkeypatch.setenv("VCT_STATE_DIR", str(root))
    monkeypatch.delenv("VCT_LAUNCHER_DB_PATH", raising=False)
    install = importlib.import_module("install")
    monkeypatch.setattr(install._containers, "resolve", lambda *a, **k: argparse.Namespace(
        runtime=None, installed=None, compose=None, reason="test"))
    monkeypatch.setattr(install._boot_service, "unregister", lambda *a, **k: [])
    monkeypatch.setattr(install._boot_service, "unregister_hub_boot_service", lambda *a, **k: [])
    hub_bin = tmp_path / "bin" / "vct-hub"
    hub_bin.parent.mkdir()
    hub_bin.write_text("")
    monkeypatch.setattr(install, "_try_bundled_vct_hub_binary", lambda root_: hub_bin)
    hub = {"alive": True}
    monkeypatch.setattr(uq, "_default_hub_running", lambda: hub["alive"])
    monkeypatch.setattr(ua, "_default_hub_running", lambda: hub["alive"])

    def runner(cmd, timeout=15):
        events.append(("hub-stop", (root / "launcher.db").exists()))
        hub["alive"] = False
        if gateway_restarts:  # its boot unit brings it back before step [5] unregisters it
            gw["alive"] = True
        return 0

    monkeypatch.setattr(uq, "_default_runner", runner)
    ours = home / ".local" / "share" / "vct-launcher" / "vct-launcher"
    launcher = {"alive": True}
    monkeypatch.setattr(uq, "_default_lister", lambda: [uq.ProcInfo(55, "vct-launcher", ours)]
                        if launcher["alive"] else [])

    def launcher_stop(pid, exe):
        events.append(("launcher-stop", (root / "launcher.db").exists()))
        launcher["alive"] = False
        return True

    monkeypatch.setattr(uq, "_default_stopper", launcher_stop)
    venv_script = str(tmp_path / "clone" / ".venv" / "bin" / "vct-model-gateway")
    monkeypatch.setattr(uq, "_default_candidates", lambda install_root: [[venv_script]])
    gw = {"alive": True}
    monkeypatch.setattr(uq, "_default_gateway_reader", lambda: uq.GatewayProc(
        77, ("/venv/python", venv_script, "serve")) if gw["alive"] else None)

    def gw_stop(pid, cands):
        events.append(("gateway-stop", (root / "logs" / "model-gateway.log").exists()))
        gw["alive"] = False
        return True

    monkeypatch.setattr(uq, "_default_gateway_stopper", gw_stop)
    real_exec = us.execute
    monkeypatch.setattr(us, "execute", lambda plan: events.append(("state", None)) or real_exec(plan))
    if tty is not None:
        monkeypatch.setattr(install.sys.stdin, "isatty", lambda: tty)
    if answers is not None:  # prompt substring -> answer; anything else "n"
        monkeypatch.setattr("builtins.input", lambda prompt: next(
            (a for k, a in answers.items() if k in prompt), "n"))
    args = argparse.Namespace(dry_run=dry_run, yes=yes, quiet=yes, keep_data=True,
                              remove_projects=False)
    install._run_uninstall(args)
    return root


def test_hub_and_launcher_stop_before_any_state_is_removed(monkeypatch, tmp_path, home):
    events: list = []
    root = _run(monkeypatch, tmp_path, home, dry_run=False, events=events)
    names = [e[0] for e in events]
    assert names == ["gateway-stop", "hub-stop", "launcher-stop", "state"], events
    assert all(e[1] for e in events[:3]), "state was already gone when a process was stopped"
    assert not root.exists()
    assert (home / ".vct-secrets" / "shared" / "github_pat").read_text() == "secret"


def test_a_gateway_restarted_by_its_unit_is_stopped_again_before_step_8(monkeypatch, tmp_path, home):
    events: list = []
    _run(monkeypatch, tmp_path, home, dry_run=False, events=events, gateway_restarts=True)
    assert [e[0] for e in events] == ["gateway-stop", "hub-stop", "launcher-stop",
                                      "gateway-stop", "state"], events


def test_dry_run_lists_everything_and_touches_nothing(monkeypatch, tmp_path, home, capsys):
    events: list = []
    root = _run(monkeypatch, tmp_path, home, dry_run=True, events=events)
    out = capsys.readouterr().out
    assert events == []
    assert "[3]" in out and "pid 55" in out and "model gateway running: pid 77" in out
    assert "[8]" in out and str(root) in out and "RL training data" in out
    assert (root / "launcher.db-wal").exists() and (root / "retrieval_rl_data").is_dir()


# ── step [1b]: legacy containers ──────────────────────────────────────────


def _ident(rows):
    asked: list = []

    def read(name, runtime, run=None):
        asked.append(name)
        row = rows.get(name)
        return None if row is None else containers.ComposeIdentity(
            project=row[0], working_dir=row[1], service=row[2])

    return read, asked


def test_vco_prefixed_names_come_from_the_ssot_and_skip_generic_names():
    names = dict(containers.vco_prefixed_container_names())
    assert names["vct_code_embed"] == "code_embed"
    assert {"vco_weaviate", "vco_ollama", "vco_code_embed"} <= set(names)
    assert not {"weaviate", "ollama", "code_embed", "ollama_claude"} & set(names)


def test_a_legacy_container_is_removed_only_with_vco_labels(tmp_path):
    current = tmp_path / "new-install" / "infrastructure"
    current.mkdir(parents=True)
    read, asked = _ident({
        "vct_code_embed": ("infrastructure", str(tmp_path / "old" / "infrastructure"), "code_embed"),
        "vco_ollama": ("infrastructure", "", "postgres"),  # same name, not VCO's service
        "vco_weaviate": ("infrastructure", str(current), "weaviate"),  # current stack: step [1]
        "myapp": ("infrastructure", "", "myapp"),  # unrelated, same generic project
    })
    calls: list = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    audit = uc.remove("podman", current_compose_dir=current, identity=read, run=run)
    assert calls == [["podman", "rm", "--force", "vct_code_embed"]]
    assert "myapp" not in asked
    assert any("removed legacy container vct_code_embed" in a for a in audit)
    assert any("vco_ollama" in a and "postgres" in a for a in audit)


def test_legacy_plan_lines_list_and_skip():
    read, _ = _ident({"vct_code_embed": ("infrastructure", "/old", "code_embed")})
    found, left = uc.find("docker", identity=read)
    text = "\n".join(uc.plan_lines(found, left))
    assert "[1b]" in text and "vct_code_embed" in text and "volumes stay" in text
    assert "[skip]" in "\n".join(uc.plan_lines([], []))


def test_compose_identity_reads_the_service_label(monkeypatch):
    def run(argv, **_kw):
        assert any(containers.COMPOSE_SERVICE_LABEL in a for a in argv)
        return subprocess.CompletedProcess(argv, 0, "infrastructure\t/old\t\tinfrastructure\t\tcode_embed\n", "")

    monkeypatch.setattr(containers, "_resolve_runtime", lambda *a, **k: "podman")
    ident = containers.compose_identity_of("vct_code_embed", "podman", run=run)
    assert ident is not None and ident.service == "code_embed" and ident.provider == "podman"


# ── step [3]: the model gateway ───────────────────────────────────────────

VENV = "/opt/vco/.venv/bin"
CANDS = [[f"{VENV}/vct-model-gateway"], [f"{VENV}/python", "-m", "model_router"]]


@pytest.mark.parametrize("cmdline,exe,ours", [
    ((f"{VENV}/python", f"{VENV}/vct-model-gateway", "serve"), None, True),  # POSIX shebang
    (("vct-model-gateway", "serve"), f"{VENV}/vct-model-gateway", True),  # Windows launcher exe
    ((f"{VENV}/python", "-m", "model_router", "serve"), None, True),
    (("/usr/bin/python3", "-m", "model_router", "serve"), None, False),  # not the VCO venv
    (("/home/u/other/.venv/bin/python", "/home/u/other/.venv/bin/vct-model-gateway", "serve"),
     None, False),  # same script name, another venv
    (("/usr/bin/python3", "my_server.py"), None, False),
])
def test_gateway_identity_is_the_vco_venv_argv(cmdline, exe, ours):
    proc = uq.GatewayProc(9, cmdline, Path(exe) if exe else None)
    assert uq.is_vco_gateway(proc, CANDS) is ours


def _gw(proc, **kw):
    stopped: list = []
    audit = uq.stop_gateway(candidates=CANDS, reader=lambda: proc,
                            stopper=lambda pid, c: stopped.append(pid) or True, **kw)
    return audit, stopped


def test_the_vco_gateway_is_stopped_with_yes():
    audit, stopped = _gw(uq.GatewayProc(9, (f"{VENV}/python", "-m", "model_router", "serve")),
                         interactive=False, confirm=lambda m: False)
    assert stopped == [9] and audit == ["stopped VCO model gateway (pid 9)"]


@pytest.mark.parametrize("answer", [True, False])
def test_interactive_the_gateway_is_stopped_only_on_yes(answer):
    asked: list = []
    audit, stopped = _gw(uq.GatewayProc(9, (f"{VENV}/vct-model-gateway", "serve")),
                         interactive=True, confirm=lambda m: asked.append(m) or answer)
    assert asked and "model gateway" in asked[0]
    assert stopped == ([9] if answer else [])
    assert answer or "declined" in audit[0]


@pytest.mark.parametrize("proc", [
    uq.GatewayProc(9, ("/usr/bin/python3", "unrelated.py")),
    uq.GatewayProc(9, None),  # access denied: cannot be identified
])
def test_a_pidfile_naming_another_process_is_reported_never_stopped(proc):
    audit, stopped = _gw(proc, interactive=False, confirm=lambda m: True)
    assert stopped == []
    assert audit[0].startswith("WARN") and "not stopped" in audit[0]


def test_no_gateway_running():
    audit, stopped = _gw(None, interactive=False, confirm=lambda m: True)
    assert stopped == [] and audit == ["model gateway not running"]
    assert _gw(None, interactive=False, confirm=lambda m: True, report_idle=False)[0] == []


def test_the_real_reader_follows_the_pidfile_and_refuses_a_foreign_pid(tmp_path, monkeypatch):
    """The daemon's own pid file (fake, naming THIS test process) is read
    through gateway_ensure; the process it names is not a VCO gateway, so it
    is reported and nothing is stopped."""
    from vco_lib import boot_service

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    (tmp_path / boot_service.GATEWAY_PID_BASENAME).write_text(f"{os.getpid()}\n")
    stopped: list = []
    audit = uq.stop_gateway(candidates=CANDS, interactive=False, confirm=lambda m: True,
                            stopper=lambda pid, c: stopped.append(pid) or True)
    assert stopped == []
    assert f"pid {os.getpid()}" in audit[0] and "not stopped" in audit[0]


def test_a_gateway_check_that_cannot_run_is_reported():
    def broken():
        raise RuntimeError("psutil is not importable")

    audit = uq.stop_gateway(candidates=CANDS, reader=broken, interactive=False,
                            confirm=lambda m: True)
    assert audit[0].startswith("WARN") and "psutil" in audit[0]


# ── review R39: M2 (no [8] while VCO still runs), M3 (non-tty needs --yes) ──


def test_declining_step_3_then_accepting_step_8_removes_nothing(monkeypatch, tmp_path, home, capsys):
    """M2: hub + launcher + gateway still running -> [8] is skipped with a line."""
    events: list = []
    root = _run(monkeypatch, tmp_path, home, dry_run=False, events=events, yes=False, tty=True,
                answers={"Proceed": "y", "Remove the VCO state dir": "y"})
    assert events == []
    assert (root / "launcher.db").exists() and (root / "retrieval_rl_data").is_dir()
    out = capsys.readouterr().out
    assert "SKIPPED [8]: still running" in out and "vct-hub" in out
    assert "VCT Launcher" in out and "model gateway" in out


def test_non_tty_without_yes_never_stops_processes_or_removes_the_state_dir(
        monkeypatch, tmp_path, home, capsys):
    """M3: a piped stdin alone does not approve step [3] or [8]."""
    events: list = []
    root = _run(monkeypatch, tmp_path, home, dry_run=False, events=events, yes=False, tty=False)
    assert events == []
    assert root.is_dir() and (root / "launcher.db").exists()
    assert capsys.readouterr().out.count("needs --yes when not interactive") == 2


def test_still_running_uses_the_same_identification(tmp_path):
    ours = tmp_path / "vct-launcher"
    cands = [[str(tmp_path / "vct-model-gateway")]]
    busy = uq.still_running(
        [ours], hub_running=lambda: True,
        lister=lambda: [uq.ProcInfo(5, "vct-launcher", ours), uq.ProcInfo(6, "vct-launcher", tmp_path / "x")],
        gateway_reader=lambda: uq.GatewayProc(7, ("/usr/bin/python3", "other.py")), gateway_candidates=cands)
    assert busy[0] == "vct-hub" and len(busy) == 2 and "pid 5" in busy[1]  # foreign gateway/launcher: not ours
    assert uq.still_running([ours], hub_running=lambda: False, lister=lambda: [],
                            gateway_reader=lambda: None, gateway_candidates=cands) == []


def test_uninstall_plan_builders_need_only_the_standard_library(tmp_path):
    """Windows field run (v0.2.103): uninstall.bat runs on the base Python
    (no venv in the clone, or the venv already gone) and step [8]'s plan
    imported agent_secrets -> project_config -> requests and crashed. Every
    uninstall plan builder must import and run with site-packages disabled."""
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
        "from vco_lib import uninstall_state, uninstall_quiesce, uninstall_artifacts;"
        "import vco_lib.uninstall_containers;"
        "uninstall_state.build_plan(clone_root=Path(sys.argv[1]));"
        "print('ok')"
    )
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "VCT_STATE_DIR": str(tmp_path / ".vct"), "PATH": os.environ.get("PATH", "")}
    proc = subprocess.run([sys.executable, "-S", "-c", code, str(repo)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr[-2000:]


def test_still_running_treats_an_unreadable_hub_state_as_running(tmp_path):
    """R40 L1: the pre-[8] check fails closed for the hub too."""
    from vco_lib import uninstall_quiesce as uq

    def broken():
        raise OSError("pidfile unreadable")

    out = uq.still_running([], hub_running=broken, lister=lambda: [],
                           gateway_reader=lambda: None, gateway_candidates=[])
    assert any("vct-hub" in item for item in out)
