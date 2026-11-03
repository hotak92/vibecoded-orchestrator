# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the Claude account registry (``vco_lib.gateway_accounts``).

The truth is one JSON document in the launcher DB; the gateway reads an
export of it; a project's choice reaches Claude Code as the machine-local env
key ``ANTHROPIC_CUSTOM_HEADERS`` written by the ordinary env projection.
"""

from __future__ import annotations

import io
import json
import os
import stat
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import config_projection as cp
from vco_lib import gateway_accounts as ga

LOCAL = Path(".claude") / "settings.local.json"
SETTINGS = Path(".claude") / "settings.json"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    project = tmp_path / "proj"
    project.mkdir()
    db = tmp_path / "launcher.db"
    make_launcher_db(db, projects=[{"project_id": "p-1", "name": "Proj",
                                    "folder_path": str(project), "slug": "proj"}])
    return tmp_path, project, db


def run_cli(db: Path, *argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = ga.main(["--db", str(db), *argv])
    return code, json.loads(out.getvalue())


def local_env(project: Path) -> dict:
    path = project / LOCAL
    return json.loads(path.read_text()).get("env", {}) if path.exists() else {}


# ── pure rules ──────────────────────────────────────────────────────────


def test_add_validates_and_refuses_duplicates(tmp_path):
    reg = ga.add_account(ga.Registry(), account_id="work", label="Work",
                         config_dir=str(tmp_path / "w"))
    assert [a.id for a in reg.accounts] == ["work"]
    for bad in ("Work", "-x", "", "a" * 33, "primary", "work"):
        with pytest.raises(ga.AccountsError):
            ga.add_account(reg, account_id=bad, label="", config_dir=str(tmp_path / bad.strip("-") / "d"))
    with pytest.raises(ga.AccountsError, match="absolute"):
        ga.add_account(reg, account_id="rel", label="", config_dir="relative/dir")
    with pytest.raises(ga.AccountsError, match="already registered"):
        ga.add_account(reg, account_id="w2", label="", config_dir=str(tmp_path / "w"))
    with pytest.raises(ga.AccountsError, match="already registered"):
        ga.add_account(reg, account_id="w3", label="", config_dir=str(tmp_path / "p"),
                       primary_dir=tmp_path / "p")


def test_remove_is_refused_while_a_project_or_the_default_uses_it(tmp_path):
    reg = ga.add_account(ga.Registry(), account_id="work", label="", config_dir=str(tmp_path / "w"))
    bound = ga.set_project(reg, "p-1", "work")
    with pytest.raises(ga.AccountsError, match="p-1"):
        ga.remove_account(bound, "work")
    with pytest.raises(ga.AccountsError, match="default"):
        ga.remove_account(ga.set_default(reg, "work"), "work")
    with pytest.raises(ga.AccountsError):
        ga.remove_account(reg, "primary")
    # Leave-alone arm: an unbound, non-default account is removed.
    assert ga.remove_account(reg, "work").accounts == ()


def test_set_project_and_default(tmp_path):
    reg = ga.add_account(ga.Registry(), account_id="work", label="", config_dir=str(tmp_path / "w"))
    with pytest.raises(ga.AccountsError):
        ga.set_project(reg, "p-1", "nobody")
    reg = ga.set_project(reg, "p-1", "work")
    assert ga.project_header_value(reg, "p-1") == "x-vco-account: work"
    assert ga.project_header_value(ga.set_project(reg, "p-1", None), "p-1") is None
    assert ga.project_header_value(reg, "p-2") is None
    with pytest.raises(ga.AccountsError):
        ga.set_default(reg, "nobody")
    assert ga.set_default(reg, "work").default == "work"


def test_parse_skips_bad_entries_and_round_trips(tmp_path):
    doc = {"schema": 1, "default": "gone", "accounts": [
        {"id": "work", "label": "Work", "config_dir": str(tmp_path)},
        {"id": "BAD", "config_dir": str(tmp_path / "b")},
        {"id": "rel", "config_dir": "relative"},
        {"id": "primary", "config_dir": str(tmp_path / "p")},
        "not-an-object",
    ], "projects": {"p-1": "work", "p-2": "gone"}}
    reg = ga.parse_registry(doc)
    assert [a.id for a in reg.accounts] == ["work"]
    assert reg.default == "primary", "a default naming nothing falls back to primary"
    assert dict(reg.projects) == {"p-1": "work"}
    assert ga.parse_registry(ga.render_registry(reg, with_projects=True)) == reg
    assert "projects" not in ga.render_registry(reg, with_projects=False)
    with pytest.raises(ga.AccountsError):
        ga.parse_registry({"schema": 99})
    with pytest.raises(ga.AccountsError):
        ga.parse_registry([])


def test_read_selector():
    assert ga.read_selector("x-vco-account: work") == "work"
    assert ga.read_selector("X-Other: 1\nX-VCO-Account:  team ") == "team"
    assert ga.read_selector("X-Other: 1") is None
    assert ga.read_selector(None) is None


def test_login_commands_quote_the_path():
    cmds = ga.login_commands("/home/u/.claude team")
    assert cmds["posix"] == "CLAUDE_CONFIG_DIR='/home/u/.claude team' claude auth login"
    assert cmds["powershell"] == "$env:CLAUDE_CONFIG_DIR='/home/u/.claude team'; claude auth login"


# ── DB, export, projection ──────────────────────────────────────────────


def test_db_round_trip_and_owner_only_export(env):
    tmp, _project, db = env
    assert ga.load(db) == ga.Registry()
    reg = ga.add_account(ga.Registry(), account_id="work", label="Work",
                         config_dir=str(tmp / "cfg-work"))
    ga.save(reg, db)
    assert ga.load(db) == reg
    path = ga.export(reg)
    assert path == tmp / "vct" / "model-gateway" / "accounts.json"
    doc = json.loads(path.read_text())
    assert doc["accounts"] == [{"id": "work", "label": "Work", "config_dir": str(tmp / "cfg-work")}]
    assert "projects" not in doc
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_without_a_launcher_db_is_an_error(tmp_path):
    with pytest.raises(ga.AccountsError, match="does not exist"):
        ga.save(ga.Registry(), tmp_path / "missing.db")


def test_projection_writes_the_selector_machine_locally_and_removes_it(env):
    tmp, project, db = env
    reg = ga.set_project(
        ga.add_account(ga.Registry(), account_id="work", label="", config_dir=str(tmp / "w")),
        "p-1", "work",
    )
    ga.save(reg, db)
    cp.apply_project_env(cp.project_env_from_db("p-1", db_path=db))
    assert local_env(project)["ANTHROPIC_CUSTOM_HEADERS"] == "x-vco-account: work"
    settings = json.loads((project / SETTINGS).read_text())
    assert "ANTHROPIC_CUSTOM_HEADERS" not in settings.get("env", {}), "never in the committable file"

    ga.save(ga.set_project(reg, "p-1", None), db)
    cp.apply_project_env(cp.project_env_from_db("p-1", db_path=db))
    assert "ANTHROPIC_CUSTOM_HEADERS" not in local_env(project)


def test_a_users_own_custom_headers_are_kept(env):
    _tmp, project, db = env
    (project / ".claude").mkdir()
    (project / LOCAL).write_text(json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Mine: 1"}}))
    cp.apply_project_env(cp.project_env_from_db("p-1", db_path=db))
    assert local_env(project)["ANTHROPIC_CUSTOM_HEADERS"] == "X-Mine: 1"


# ── CLI (the launcher's entry point) ────────────────────────────────────


def test_cli_add_set_project_status_remove(env):
    tmp, project, db = env
    code, out = run_cli(db, "add", "--id", "work", "--label", "Work seat",
                        "--config-dir", str(tmp / "cfg-work"))
    assert code == 0 and out["ok"], out
    assert json.loads(Path(out["exported"]).read_text())["accounts"][0]["id"] == "work"

    code, out = run_cli(db, "set-project", "--project-id", "p-1", "--account", "work")
    assert code == 0, out
    assert local_env(project)["ANTHROPIC_CUSTOM_HEADERS"] == "x-vco-account: work"

    code, status = run_cli(db, "project-status", "--project-id", "p-1")
    assert code == 0
    assert status["assigned"] == "work" and status["file_says"] == "work"
    assert status["drift"] is False
    # work is not logged in: the project is served by primary until it is.
    assert status["falling_back"] is True and status["effective"] == "primary"

    code, out = run_cli(db, "remove", "--id", "work")
    assert code == 2 and "p-1" in out["error"]

    code, listing = run_cli(db, "list", "--no-gateway")
    rows = {r["id"]: r for r in listing["accounts"]}
    assert rows["work"]["projects"] == ["p-1"]
    assert rows["work"]["login"]["state"] == "absent"
    assert "claude auth login" in rows["work"]["login_commands"]["posix"]

    code, _ = run_cli(db, "set-project", "--project-id", "p-1", "--default")
    assert code == 0
    assert "ANTHROPIC_CUSTOM_HEADERS" not in local_env(project)
    code, out = run_cli(db, "remove", "--id", "work")
    assert code == 0, out


def test_status_reports_drift_when_the_file_disagrees(env):
    tmp, project, db = env
    reg = ga.set_project(
        ga.add_account(ga.Registry(), account_id="work", label="", config_dir=str(tmp / "w")),
        "p-1", "work",
    )
    status = ga.project_status(reg, "p-1", project, {"work": {"usable": True}})
    assert status["drift"] is True and status["file_says"] is None
    assert status["falling_back"] is False and status["effective"] == "work"


# ── review R4 A1/A2/A3/A10: the user's header, a tracked layer, the save ──


VSCODE = Path(".vscode") / "settings.json"


def _assign_work(tmp: Path, db: Path) -> "ga.Registry":
    reg = ga.set_project(
        ga.add_account(ga.Registry(), account_id="work", label="", config_dir=str(tmp / "w")),
        "p-1", "work",
    )
    ga.save(reg, db)
    return reg


@pytest.mark.parametrize("assigned", [False, True])
def test_a_users_own_header_in_the_committable_files_is_never_removed(env, assigned):
    """A1: with or without an account assigned, an apply leaves the user's
    own ANTHROPIC_CUSTOM_HEADERS in settings.json AND .vscode/settings.json
    byte-for-byte (the GITHUB_TOKEN precedent)."""
    tmp, project, db = env
    if assigned:
        _assign_work(tmp, db)
    (project / ".claude").mkdir()
    (project / ".vscode").mkdir()
    (project / SETTINGS).write_text(json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Mine: 1"}}))
    (project / VSCODE).write_text(json.dumps(
        {"claude-code.env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Mine: 2"}}))
    cp.apply_project_env(cp.project_env_from_db("p-1", db_path=db),
                         surfaces=[cp._SURFACE_CLAUDE_SETTINGS, cp._SURFACE_VSCODE_SETTINGS])
    assert json.loads((project / SETTINGS).read_text())["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-Mine: 1"
    assert json.loads((project / VSCODE).read_text())["claude-code.env"][
        "ANTHROPIC_CUSTOM_HEADERS"] == "X-Mine: 2"
    if assigned:  # ... and the selector still lands in the private file
        assert local_env(project)["ANTHROPIC_CUSTOM_HEADERS"] == "x-vco-account: work"


def test_the_drift_check_does_not_report_the_users_own_header(env):
    from vco_lib import env_projection_check as epc
    _tmp, project, db = env
    cp.apply_project_env(cp.project_env_from_db("p-1", db_path=db))
    data = json.loads((project / SETTINGS).read_text())
    data["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "X-Mine: 1"
    (project / SETTINGS).write_text(json.dumps(data))
    check = epc.check_env_surfaces(project, cp.project_env_from_db("p-1", db_path=db)["canonical_env"])
    assert not [d for d in check.drift if d["key"] == "ANTHROPIC_CUSTOM_HEADERS"], check.drift


def _git_repo(project: Path, monkeypatch) -> None:
    import subprocess
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".claude").mkdir(exist_ok=True)
    (project / LOCAL).write_text(json.dumps({"env": {}}))
    subprocess.run(["git", "add", "-f", str(LOCAL)], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "tracked local"], cwd=project, check=True)


def test_a_tracked_local_layer_never_puts_the_selector_in_settings_json(env, monkeypatch):
    """A2: the selector is a per-machine choice; with settings.local.json
    committed it is projected nowhere, and the status says why."""
    tmp, project, db = env
    _git_repo(project, monkeypatch)
    reg = _assign_work(tmp, db)
    cp.apply_project_env(cp.project_env_from_db("p-1", db_path=db))
    settings = json.loads((project / SETTINGS).read_text())
    assert "ANTHROPIC_CUSTOM_HEADERS" not in settings.get("env", {})
    assert "ANTHROPIC_CUSTOM_HEADERS" not in local_env(project)
    status = ga.project_status(reg, "p-1", project, {"work": {"usable": True}})
    assert status["drift"] is True and status["drift_cause"] == ga.DRIFT_LOCAL_TRACKED


def test_status_names_the_cause_of_drift(env):
    """A3: the user's own header (kept by design) is a different cause from
    a projection that has not run — only the latter is fixed by Re-apply."""
    tmp, project, db = env
    reg = _assign_work(tmp, db)
    status = ga.project_status(reg, "p-1", project, {"work": {"usable": True}})
    assert status["drift_cause"] == ga.DRIFT_NOT_APPLIED
    assert status["selector_line"] == "x-vco-account: work"
    (project / ".claude").mkdir()
    (project / LOCAL).write_text(json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Mine: 1"}}))
    status = ga.project_status(reg, "p-1", project, {"work": {"usable": True}})
    assert status["drift_cause"] == ga.DRIFT_USER_VALUE
    assert "X-Mine" not in json.dumps(status), "a header value is never read out"
    (project / LOCAL).write_text(json.dumps(
        {"env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Mine: 1\nx-vco-account: work"}}))
    status = ga.project_status(reg, "p-1", project, {"work": {"usable": True}})
    assert status["drift"] is False and status["drift_cause"] is None


def test_a_failed_projection_still_reports_the_save(env, monkeypatch):
    """A10: the choice is saved and exported before the projection runs; a
    projection that raises is reported beside ok:true, never as a crash."""
    tmp, _project, db = env
    run_cli(db, "add", "--id", "work", "--label", "W", "--config-dir", str(tmp / "cfg"))

    def refuse(_bundle, **_kw):
        raise cp.SettingsWriteRefused([])

    monkeypatch.setattr(cp, "apply_project_env", refuse)
    code, out = run_cli(db, "set-project", "--project-id", "p-1", "--account", "work")
    assert code == 0 and out["ok"] is True, out
    assert out["projection_error"].startswith("SettingsWriteRefused")
    assert ga.load(db).projects["p-1"] == "work"
    assert out["status"]["drift"] is True
