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


def _run(monkeypatch, tmp_path, home, *, dry_run, events):
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
        return 0

    monkeypatch.setattr(uq, "_default_runner", runner)
    ours = home / ".local" / "share" / "vct-launcher" / "vct-launcher"
    monkeypatch.setattr(uq, "_default_lister", lambda: [uq.ProcInfo(55, "vct-launcher", ours)])
    monkeypatch.setattr(uq, "_default_stopper", lambda pid, exe: events.append(
        ("launcher-stop", (root / "launcher.db").exists())) or True)
    real_exec = us.execute
    monkeypatch.setattr(us, "execute", lambda plan: events.append(("state", None)) or real_exec(plan))
    args = argparse.Namespace(dry_run=dry_run, yes=True, quiet=True, keep_data=True,
                              remove_projects=False)
    assert install._run_uninstall(args) == 0
    return root


def test_hub_and_launcher_stop_before_any_state_is_removed(monkeypatch, tmp_path, home):
    events: list = []
    root = _run(monkeypatch, tmp_path, home, dry_run=False, events=events)
    names = [e[0] for e in events]
    assert names == ["hub-stop", "launcher-stop", "state"], events
    assert events[0][1] and events[1][1], "launcher.db was already gone when the hub stopped"
    assert not root.exists()
    assert (home / ".vct-secrets" / "shared" / "github_pat").read_text() == "secret"


def test_dry_run_lists_everything_and_touches_nothing(monkeypatch, tmp_path, home, capsys):
    events: list = []
    root = _run(monkeypatch, tmp_path, home, dry_run=True, events=events)
    out = capsys.readouterr().out
    assert events == []
    assert "[3]" in out and "pid 55" in out
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
