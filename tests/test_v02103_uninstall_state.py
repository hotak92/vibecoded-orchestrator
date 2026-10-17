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


def test_a_second_run_on_an_uninstalled_machine_removes_nothing_and_claims_nothing(home):
    """Idempotence (the Rust source audit `test_uninstall_is_idempotent_via_
    existence_guards` checked a per-file `launcher_db.exists()` guard; step
    [8] removes the whole state dir now, so the property is pinned here by
    behaviour): after a full run, a second plan finds nothing and executing
    it neither errors nor reports a removal."""
    root = _state(home / ".vct")
    us.execute(us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db"))
    assert not root.exists()
    again = us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db")
    assert not again.exists and not again.db_files
    audit = us.execute(again)
    assert not any("removed" in line.lower() for line in audit), audit
    assert not root.exists()


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
         yes=True, tty=None, answers=None, quiet=None, root_name=".vct"):
    root = _state(home / root_name)
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
    args = argparse.Namespace(dry_run=dry_run, yes=yes, quiet=yes if quiet is None else quiet, keep_data=True,
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
    assert capsys.readouterr().out.count("needs --yes when stdin is not a terminal") == 2


def test_tty_with_quiet_without_yes_says_quiet_is_the_reason(monkeypatch, tmp_path, home, capsys):
    """Review V02103-FINAL4 L2: on a terminal, --quiet without --yes skips
    [3]/[8] — and the line names --quiet, not a missing terminal."""
    events: list = []
    root = _run(monkeypatch, tmp_path, home, dry_run=False, events=events, yes=False, tty=True,
                quiet=True)
    assert events == [] and (root / "launcher.db").exists()
    out = capsys.readouterr().out
    assert out.count("needs --yes with --quiet") == 2 and "not a terminal" not in out


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
        # the pre-[8] check must work too: read-only process listing, no stops
        "import platform;"
        "exes = uninstall_artifacts.launcher_exe_paths(os_name=platform.system(), home=Path.home());"
        "left = uninstall_quiesce.still_running(exes, install_root=Path(sys.argv[1]));"
        "assert not [x for x in left if 'unreadable' in x or 'cannot check' in x], left;"
        "print('ok')"
    )
    # `-S` + a minimal env is the base-Python shape under test; the child puts
    # the checkout on sys.path itself (child_env only adds the test pins).
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "VCT_STATE_DIR": str(tmp_path / ".vct"), "PATH": os.environ.get("PATH", "")}
    from tests.common.child_env import child_env

    proc = subprocess.run([sys.executable, "-S", "-c", code, str(repo)],
                          capture_output=True, text=True, env=child_env(env), timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr[-2000:]


def test_still_running_treats_an_unreadable_hub_state_as_running(tmp_path):
    """R40 L1: the pre-[8] check fails closed for the hub too."""
    from vco_lib import uninstall_quiesce as uq

    def broken():
        raise OSError("pidfile unreadable")

    out = uq.still_running([], hub_running=broken, lister=lambda: [],
                           gateway_reader=lambda: None, gateway_candidates=[])
    assert any("vct-hub" in item for item in out)


# ── review V02103-FINAL4 H1: stop + gateway read without psutil ───────────

_SLEEPER = "import time\ntime.sleep(60)\n"


def _sleeper(tmp_path: Path, name: str) -> "subprocess.Popen":
    """A harmless child of THIS test process (never a real VCO process)."""
    from tests.common.child_env import child_env

    script = tmp_path / name
    script.write_text(_SLEEPER)
    return subprocess.Popen([sys.executable, "-S", str(script)], env=child_env())


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="drives real signals against throwaway children via /proc")
def test_stops_and_gateway_read_work_on_a_base_python_without_psutil(tmp_path):
    """The field shape: uninstall on the base Python (``-S``, psutil absent).
    The REAL launcher stopper and gateway stopper/reader run against
    throwaway sleepers this test spawned: a sleeper that does NOT run from
    the identified exe / gateway argv is left alive; one that does is
    stopped; a pid file naming a non-gateway reads "not ours", never
    "cannot check"."""
    from tests.common.child_env import child_env

    repo = Path(__file__).resolve().parents[1]
    state = tmp_path / ".vct"
    state.mkdir()
    keep_l, kill_l = _sleeper(tmp_path, "keep-launcher"), _sleeper(tmp_path, "kill-launcher")
    keep_g, kill_g = _sleeper(tmp_path, "keep-gw"), _sleeper(tmp_path, "vct-model-gateway")
    procs = [keep_l, kill_l, keep_g, kill_g]
    code = r"""
import importlib.util, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec("psutil") is None, "psutil must be absent (base-Python shape)"
from vco_lib import boot_service
from vco_lib import uninstall_quiesce as uq
keep_l, kill_l, keep_g, kill_g = (int(a) for a in sys.argv[2:6])
gw_script, state = sys.argv[6], Path(sys.argv[7])
py = Path(os.path.realpath(sys.executable))
ok = lambda m: True
# launcher, leave-alone arm: identified at a VCO path the process does not run from
fake = Path(sys.argv[7]) / "vct-launcher"
a = uq.stop_launchers([fake], interactive=False, confirm=ok, wait_for_user=ok,
                      lister=lambda: [uq.ProcInfo(keep_l, "vct-launcher", fake)])
assert any("could not stop" in x for x in a), a
# launcher, act arm: the stopper targets it via its real (injected) exe path
a = uq.stop_launchers([py], interactive=False, confirm=ok, wait_for_user=ok,
                      lister=lambda: [uq.ProcInfo(kill_l, "vct-launcher", py)])
assert any(x.startswith("stopped VCO launcher") for x in a), a
# gateway reader: the pid file names a live NON-gateway -> "not ours"
(state / boot_service.GATEWAY_PID_BASENAME).write_text(f"{keep_g}\n")
proc = uq._default_gateway_reader()
assert proc is not None and proc.cmdline, proc
cands = [[gw_script]]
assert uq.find_gateway(candidates=cands)[1].endswith("not VCO's gateway"), uq.find_gateway(candidates=cands)
a = uq.stop_gateway(candidates=cands, interactive=False, confirm=ok)
assert "not stopped" in a[0], a
left = uq.still_running([], install_root=Path(sys.argv[1]), hub_running=lambda: False,
                        gateway_candidates=cands)
assert not [x for x in left if "cannot check" in x or "unreadable" in x], left
# gateway, act arm: the pid file names the sleeper running the gateway script
(state / boot_service.GATEWAY_PID_BASENAME).write_text(f"{kill_g}\n")
a = uq.stop_gateway(candidates=cands, interactive=False, confirm=ok)
assert a == [f"stopped VCO model gateway (pid {kill_g})"], a
print("ok")
"""
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "VCT_STATE_DIR": str(state),
           "PATH": os.environ.get("PATH", "")}
    try:
        res = subprocess.run(
            [sys.executable, "-S", "-c", code, str(repo), *(str(p.pid) for p in procs),
             str(tmp_path / "vct-model-gateway"), str(state)],
            capture_output=True, text=True, env=child_env(env), timeout=120)
        assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr[-3000:]
        assert keep_l.poll() is None and keep_g.poll() is None, "a non-matching process was killed"
        assert kill_l.wait(timeout=10) == -15 and kill_g.wait(timeout=10) == -15
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)


class _Res:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self.stdout, self.returncode = stdout, returncode


def _fake_ps(payload: bytes, rc: int = 0):
    calls: list = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return _Res(payload, rc)

    return run, calls


def test_windows_listing_survives_oem_code_page_bytes():
    """Review V02103-FINAL4 M1: PowerShell 5.1 bytes in cp850 (``ü`` = 0x81,
    undefined in cp1252) never raise; the query forces UTF-8 output and the
    bytes are decoded as UTF-8 with replacement."""
    rows = ('[{"ProcessId":4242,"Name":"vct-launcher.exe","ExecutablePath":'
            '"C:\\\\Users\\\\J\u00fcrgen\\\\AppData\\\\Local\\\\vct-launcher\\\\vct-launcher.exe"},'
            '{"ProcessId":7,"Name":"Pi\u00f9.exe","ExecutablePath":null}]')
    run, calls = _fake_ps(rows.encode("cp850"))
    procs = uq._stdlib_lister(platform="win32", run=run)
    assert [p.pid for p in procs] == [4242, 7]
    assert "J\ufffdrgen" in str(procs[0].exe) and procs[1].exe is None
    cmd, kw = calls[0]
    assert cmd[-1].startswith("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;")
    assert "text" not in kw and "encoding" not in kw  # raw bytes, decoded by us
    run, _ = _fake_ps(rows.encode("utf-8"))
    assert "J\u00fcrgen" in str(uq._stdlib_lister(platform="win32", run=run)[0].exe)
    run, _ = _fake_ps(b"", rc=1)
    with pytest.raises(OSError):
        uq._stdlib_lister(platform="win32", run=run)


def test_windows_gateway_identity_reads_the_cim_command_line():
    line = '"C:\\VCO\\.venv\\Scripts\\vct-model-gateway.exe" serve --port 11460'
    payload = ('{"CommandLine":%s,"ExecutablePath":"C:\\\\VCO\\\\.venv\\\\Scripts\\\\vct-model-gateway.exe"}'
               % __import__("json").dumps(line)).encode("utf-8")
    run, calls = _fake_ps(payload)
    proc = uq._stdlib_identity(99, platform="win32", run=run)
    assert proc is not None and proc.cmdline == (
        "C:\\VCO\\.venv\\Scripts\\vct-model-gateway.exe", "serve", "--port", "11460")
    assert "ProcessId=99" in calls[0][0][-1]
    assert uq._stdlib_identity(99, platform="win32", run=_fake_ps(b"")[0]) is None  # gone
    no_line = b'{"CommandLine":null,"ExecutablePath":null}'
    blank = uq._stdlib_identity(99, platform="win32", run=_fake_ps(no_line)[0])
    assert blank is not None and blank.cmdline is None  # alive, will not say


@pytest.mark.parametrize("line,toks", [
    ('a b  c', ("a", "b", "c")),
    ('"C:\\Program Files\\x.exe" -m model_router', ("C:\\Program Files\\x.exe", "-m", "model_router")),
    ('"" x', ("", "x")),
])
def test_windows_command_line_split(line, toks):
    assert uq._split_windows_cmdline(line) == toks


@pytest.mark.parametrize("second_check,forced", [(True, True), (False, False), (None, False)])
def test_stdlib_stop_rechecks_identity_before_forcing(monkeypatch, second_check, forced):
    """The psutil-free stop asks gracefully, waits (bounded), and forces only
    if the pid is STILL the identified process (a pid reused during the
    grace wait is left alone; a pid gone by then needs nothing)."""
    signals: list = []
    checks = iter([True, second_check])
    monkeypatch.setattr(uq, "_gone", lambda pid: False)
    monkeypatch.setattr(uq, "_signal_stdlib", lambda pid, force: signals.append(force) or True)
    monkeypatch.setattr(uq, "_wait_gone", lambda pid, s, sleep=None: bool(signals and signals[-1]))
    out = uq._terminate_if_stdlib(5, lambda: next(checks), grace=1.0)
    assert signals == ([False, True] if forced else [False])
    assert out is (forced or second_check is None)


def test_stdlib_stop_never_signals_a_process_that_is_not_ours(monkeypatch):
    signals: list = []
    monkeypatch.setattr(uq, "_gone", lambda pid: False)
    monkeypatch.setattr(uq, "_signal_stdlib", lambda pid, force: signals.append(force) or True)
    assert uq._terminate_if_stdlib(5, lambda: False, grace=1.0) is False and signals == []


def test_without_psutil_the_default_stopper_takes_the_stdlib_path(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(uq, "_psutil", lambda: None)
    monkeypatch.setattr(uq, "_terminate_if_stdlib",
                        lambda pid, ours, grace: seen.append((pid, grace)) or True)
    assert uq._default_stopper(5, tmp_path / "vct-launcher") is True
    assert uq._default_gateway_stopper(6, CANDS) is True
    assert seen == [(5, 10.0), (6, 15.0)]


def test_a_stopper_that_raises_is_a_warn_and_the_rest_continue(tmp_path):
    ours = tmp_path / "vct-launcher"

    def boom(pid, exe):
        if pid == 1:
            raise ModuleNotFoundError("No module named 'psutil'")
        return True

    audit = uq.stop_launchers([ours], interactive=False, confirm=lambda m: True,
                              wait_for_user=lambda m: None,
                              lister=_procs((1, "vct-launcher", ours), (2, "vct-launcher", ours)),
                              stopper=boom)
    assert audit[0].startswith("WARN: could not stop VCO launcher") and "psutil" in audit[0]
    assert audit[1].startswith("stopped VCO launcher")
    g = uq.stop_gateway(candidates=CANDS, reader=lambda: uq.GatewayProc(9, (f"{VENV}/vct-model-gateway",)),
                        interactive=False, confirm=lambda m: True,
                        stopper=lambda pid, c: (_ for _ in ()).throw(OSError("denied")))
    assert g[0].startswith("WARN: could not stop the VCO model gateway") and "denied" in g[0]


def test_quiesce_runs_every_stop_even_when_one_raises(monkeypatch, tmp_path):
    """install.py step [3] goes through quiesce(): an unexpected failure in
    one stop is a WARN line and the others still run (step [8] re-checks)."""
    order: list = []

    def gw(**kw):
        order.append("gateway")
        raise ImportError("No module named 'psutil'")

    monkeypatch.setattr(uq, "stop_gateway", gw)
    monkeypatch.setattr(uq, "stop_hub", lambda bins: order.append("hub") or ["hub ok"])
    monkeypatch.setattr(uq, "stop_launchers", lambda *a, **k: order.append("launcher") or ["l ok"])
    audit = uq.quiesce([tmp_path / "x"], [], interactive=False, confirm=lambda m: True)
    assert order == ["gateway", "hub", "launcher"]
    assert audit[0].startswith("WARN: could not stop the model gateway") and "psutil" in audit[0]
    assert audit[1:] == ["hub ok", "l ok"]


class _FakePsutil:
    """Minimal psutil stand-in for the psutil leg of `_terminate_if`."""

    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    class TimeoutExpired(Error):
        pass

    def __init__(self, ours_after_grace: bool):
        self.ours_after_grace = ours_after_grace
        self.killed = False
        fake = self

        class Process:
            def __init__(self, pid):
                self.pid = pid

            def terminate(self):
                pass

            def wait(self, timeout=None):
                if not fake.killed:
                    raise fake.TimeoutExpired()

            def is_running(self):
                return not fake.killed

            def kill(self):
                fake.killed = True

        self.Process = Process


@pytest.mark.parametrize("ours_after_grace, killed", [(True, True), (False, False)])
def test_psutil_stop_reidentifies_before_forcing(monkeypatch, ours_after_grace, killed):
    """R47 / R39 L1: after the grace timeout the psutil leg re-checks the
    identity before `kill()` — a process that is no longer ours is left alone."""
    from vco_lib import uninstall_quiesce as uq

    fake = _FakePsutil(ours_after_grace)
    monkeypatch.setattr(uq, "_psutil", lambda: fake)
    calls = []

    def still_ours(_proc):
        calls.append(1)
        return True if len(calls) == 1 else fake.ours_after_grace

    uq._terminate_if(4242, still_ours, lambda: None, grace=0.01)
    assert fake.killed is killed
    assert len(calls) == 2


# ── v0.2.104: review R39 H1 / M1 / L2 / L3 (user-deferred from v0.2.103) ──


def test_a_linked_state_dir_root_is_refused_and_its_target_untouched(home, tmp_path):
    """R39 H1: ``~/.vct`` a symlink (a junction on Windows — ``_is_reparse``
    covers both) to a broader dir. Walking it would delete the TARGET's
    contents, which the plan never shows: the root is refused, the user's
    files survive, only VCO's exact names go."""
    target = tmp_path / "Documents"
    target.mkdir()
    (target / "thesis.docx").write_text("user")
    (target / "photos").mkdir()
    (target / "photos" / "x.jpg").write_text("user")
    (target / "launcher.db").write_text("x")  # one VCO marker
    root = home / ".vct"
    root.symlink_to(target, target_is_directory=True)
    plan = us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db")
    assert plan.refused and "link or junction" in plan.refused and str(target) in plan.refused
    assert "[refused]" in "\n".join(us.plan_lines(plan))
    assert "by name" in us.confirm_prompt(plan)
    us.execute(plan)
    assert (target / "thesis.docx").read_text() == "user"
    assert (target / "photos" / "x.jpg").read_text() == "user"
    assert not (target / "launcher.db").exists()  # exact VCO name only
    assert root.is_symlink()  # the link is the user's to remove


def test_a_root_that_becomes_a_link_after_planning_is_never_walked(home, tmp_path, monkeypatch):
    root = _state(home / ".vct")
    plan = us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db")
    assert plan.refused is None
    real = us._is_reparse
    monkeypatch.setattr(us, "_is_reparse", lambda p: p == root or real(p))
    audit = us.execute(plan)
    assert any("SKIPPED [8]" in a for a in audit), audit
    assert (root / "hub.db").exists() and (root / "retrieval_rl_data").is_dir()


def test_a_real_vct_dir_root_is_still_removed_whole(home):
    root = _state(home / ".vct")
    plan = us.build_plan(root=root, home=home, env={}, launcher_db=root / "launcher.db")
    assert plan.refused is None and not plan.selective
    assert us.confirm_prompt(plan).startswith("Remove the VCO state dir")
    us.execute(plan)
    assert not root.exists()


def test_an_explicit_state_dir_not_named_vct_loses_only_vcos_entries(home, tmp_path):
    """R39 M1: ``VCT_STATE_DIR=~/Documents`` holding VCO's files: VCO's own
    top-level entries go, the user's files, the generic-named dirs and the
    folder itself stay."""
    root = _state(tmp_path / "Documents")
    (root / "thesis.docx").write_text("user")
    (root / f"hub.token.{_PROJECT_ID}").write_text("x")
    (root / "launcher.db.bak-old").write_text("x")
    plan = us.build_plan(root=root, home=home, env={"VCT_STATE_DIR": str(root)},
                         launcher_db=root / "launcher.db")
    assert plan.refused is None and plan.selective
    text = "\n".join(us.plan_lines(plan))
    assert "not named .vct" in text and "LEFT IN PLACE" in text and "logs" in text
    assert "thesis.docx" not in text
    assert "folder stays" in us.confirm_prompt(plan)
    audit = us.execute(plan)
    assert (root / "thesis.docx").read_text() == "user"
    assert (root / "logs" / "model-gateway.log").exists() and (root / "state").is_dir()
    for gone in ("launcher.db", "launcher.db-wal", "launcher.db-shm", "hub.db", "hub.pid", "hub.token",
                 "model-gateway.token", "retrieval_rl_data", f"hub.token.{_PROJECT_ID}",
                 "launcher.db.bak-old"):
        assert not (root / gone).exists(), (gone, audit)
    assert root.is_dir()
    assert any("left in place" in a for a in audit)


_PROJECT_ID = "3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"

#: Derived names VCO really writes (each shape's writer is named at
#: ``uninstall_state.VCO_OWNED_DBS``) — the act arm of the exact-shape rule.
_VCO_DERIVED = ("hub.db-wal", "hub.db-shm", "launcher.db-journal", "launcher.db.bak-pre-x",
                "launcher.db.bak-pre-x-wal", f"hub.token.{_PROJECT_ID}",
                "rl_project_identity_cache.json.0bxawabr.tmp",
                "rl_project_identity_cache.json.lock", "services.toml.ab_12xyz.tmp",
                f"hub.token.{_PROJECT_ID}.k3j9_x0q.tmp", "services.toml.migrated-v0297")
#: Near-miss names a user may keep beside VCO's files — the leave-alone arm
#: (v0.2.104 R2 L4: the open prefixes deleted every one of these).
_USER_NEAR_MISS = ("hub.db-notes.txt", "model-gateway.notes", "model-gateway.howto.md",
                   "launcher.dbx", "launcher.db.bak-", "hub.token.mine", "hub.token.0123",
                   "services.toml.orig", "rl_project_identity_cache.json.old",
                   "thesis.docx.abcdefgh.tmp", "services.toml.ABCDEFGH.tmp", "notes.lock")


def test_a_selective_dir_removes_vcos_derived_names_and_keeps_near_miss_user_files(home, tmp_path):
    root = _state(tmp_path / "Documents")
    for name in (*_VCO_DERIVED, *_USER_NEAR_MISS):
        (root / name).write_text("x")
    us.execute(us.build_plan(root=root, home=home, env={"VCT_STATE_DIR": str(root)},
                             launcher_db=root / "launcher.db"))
    for name in _VCO_DERIVED:
        assert not (root / name).exists(), name
    for name in _USER_NEAR_MISS:
        assert (root / name).read_text() == "x", name
    assert root.is_dir()


def test_the_migrated_services_toml_name_matches_its_writer():
    from vco_lib import service_reconcile
    assert us.SERVICES_TOML_MIGRATED == "services.toml" + service_reconcile.MIGRATED_SUFFIX


def test_a_selective_dir_never_follows_a_link_named_like_vcos(home, tmp_path):
    root = _state(tmp_path / "custom-state")
    outside = tmp_path / "user-data"
    outside.mkdir()
    (outside / "precious.txt").write_text("user")
    (root / "rl_archive").symlink_to(outside, target_is_directory=True)
    us.execute(us.build_plan(root=root, home=home, env={"VCT_STATE_DIR": str(root)},
                             launcher_db=root / "launcher.db"))
    assert (outside / "precious.txt").read_text() == "user"
    assert not (root / "rl_archive").is_symlink()


VENV_WIN = "C:\\VCO\\.venv\\Scripts"
CANDS_WIN = [[f"{VENV_WIN}\\vct-model-gateway.exe"], [f"{VENV_WIN}\\python.exe", "-m", "model_router"]]


@pytest.mark.parametrize("cmdline,exe,cands,ours", [
    # R39 L2: the script later in argv is NOT the gateway
    (("cp", f"{VENV}/vct-model-gateway", "/tmp/"), "/usr/bin/cp", CANDS, False),
    (("vim", f"{VENV}/vct-model-gateway"), None, CANDS, False),
    (("/usr/bin/python3", "tool.py", f"{VENV}/vct-model-gateway"), None, CANDS, False),
    (("notepad.exe", f"{VENV_WIN}\\vct-model-gateway.exe"), None, CANDS_WIN, False),
    # the real shapes still match
    ((f"{VENV}/python3.12", f"{VENV}/vct-model-gateway", "serve"), None, CANDS, True),
    ((f"{VENV}/vct-model-gateway", "serve"), None, CANDS, True),
    ((f"{VENV}/python", "-m", "model_router", "serve"), None, CANDS, True),
    ((f"{VENV_WIN}\\vct-model-gateway.exe", "serve"), f"{VENV_WIN}\\vct-model-gateway.exe", CANDS_WIN, True),
    ((f"{VENV_WIN}\\python.exe", f"{VENV_WIN}\\vct-model-gateway.exe", "serve"), None, CANDS_WIN, True),
    ((f"{VENV_WIN}\\python.exe", "-m", "model_router", "serve"), None, CANDS_WIN, True),
])
def test_gateway_console_script_matches_only_at_argv0_or_after_python(cmdline, exe, cands, ours):
    proc = uq.GatewayProc(9, cmdline, Path(exe) if exe else None)
    assert uq.is_vco_gateway(proc, cands) is ours


def test_a_current_stack_container_with_a_blank_working_dir_label_is_step_1s(tmp_path):
    """R39 L3: some podman-compose versions leave ``working_dir`` blank; the
    current stack's canonical container is then matched by compose project
    and left to step [1], never listed as legacy. A blank label under another
    project is still legacy."""
    compose_dir = tmp_path / "vco" / "infrastructure"
    compose_dir.mkdir(parents=True)
    (compose_dir / "docker-compose.yml").write_text("services: {}\n")
    canon = containers.canonical_name("ollama")
    read, _ = _ident({canon: ("infrastructure", "", "ollama")})
    found, left = uc.find("docker", current_compose_dir=compose_dir, identity=read)
    assert canon not in [c.name for c in found] and not left
    read, _ = _ident({canon: ("someother", "", "ollama")})
    found, _ = uc.find("docker", current_compose_dir=compose_dir, identity=read)
    assert canon in [c.name for c in found]


@pytest.mark.parametrize("toks,script", [
    (("/v/bin/python", "-S", "/v/bin/gw", "serve"), "/v/bin/gw"),
    (("/v/bin/python3", "-X", "utf8", "-u", "/v/bin/gw"), "/v/bin/gw"),
    (("/v/bin/python", "-m", "model_router"), None),
    (("/v/bin/python", "-c", "/v/bin/gw"), None),
    (("/usr/bin/cp", "/v/bin/gw", "/tmp"), None),
    (("C:\\v\\Scripts\\pythonw.exe", "C:\\v\\Scripts\\gw.exe"), "C:\\v\\Scripts\\gw.exe"),
])
def test_python_script_arg(toks, script):
    assert uq._python_script_arg(toks) == script



def test_end_to_end_a_state_dir_not_named_vct_keeps_the_folder_and_the_users_files(
        monkeypatch, tmp_path, home, capsys):
    """R39 M1 + L3 through ``_run_uninstall``: the [8] question names what
    actually goes, the user's file and the folder survive, VCO's own entries
    go, and step [7] removes its exact-name state files inside the generic
    ``state/`` dir that [8] leaves."""
    prompts: list = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    (home / "Documents").mkdir()
    (home / "Documents" / "thesis.docx").write_text("user")
    root = _run(monkeypatch, tmp_path, home, dry_run=False, events=[], yes=False, tty=True,
                root_name="Documents")
    assert (root / "thesis.docx").read_text() == "user" and root.is_dir()
    assert not (root / "launcher.db").exists() and not (root / "retrieval_rl_data").exists()
    assert not (root / "state" / "codegraph-walk-claims" / "a.lock").exists()
    assert (root / "logs" / "model-gateway.log").exists()
    assert any("Remove VCO's own entries from" in p and "folder stays" in p for p in prompts), prompts
    assert not any("Remove the VCO state dir" in p for p in prompts), prompts
