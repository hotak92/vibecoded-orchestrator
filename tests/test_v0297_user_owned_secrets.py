# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — ``user_owned_secret_value_in_tree``: a secret the USER put in a
committable settings file is reported, never removed.

``user_secret_values_retained_in_tree`` was narrowed to the values VCO itself
wrote (every env refresh removes those). A hand-added secret-shaped key
(``STALE_SECRET`` below — the key the two older fixtures used) keeps its
security signal here instead: act (reported, names only), leave-alone (no VCO
writer touches it), clears (probe: key gone or value empty), dismiss (the
generic mechanism, keyed on the set of names).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import insert_rows, make_launcher_db
from vco_lib import config_projection as cp
from vco_lib import deferral_probes, deferral_registry, project_init, user_owned_secrets
from vco_lib.deferral_report import DeferralReport

CID = user_owned_secrets.CID
VALUE = "synthetic-secret-value-in-settings"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    db = make_launcher_db(tmp_path / "launcher.db", projects=[{
        "project_id": "pid-owned", "name": "Acme", "folder_path": str(folder),
    }])
    insert_rows(db, "secret_active_state", [
        {"scope": "per_project", "project_id": "pid-owned", "module_id": "user",
         "key": "LAUNCHER_KNOWN_TOKEN", "requester_project_id": "pid-owned",
         "active": 1, "updated_at": 0},
    ])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    return folder


def _write(folder: Path, env: dict, rel: str = ".claude/settings.json", key: str = "env") -> Path:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {}, key: env}, indent=2), encoding="utf-8")
    return path


def _probe(folder: Path):
    entry = DeferralReport.read(folder).entry_for(CID)
    ctx = deferral_probes.ProbeContext(folder=folder, entry=entry)
    return deferral_probes.PROBES["user_owned_secret_values_still_present"](ctx)


def _dismiss(folder: Path) -> None:
    rc = project_init._cmd_dismiss_deferral(
        argparse.Namespace(folder=str(folder), condition_id=CID, json=True))
    assert rc == 0


def test_the_condition_is_registered_as_action_required_with_a_real_probe():
    spec = deferral_registry.condition(CID)
    assert spec is not None and spec.condition_class == "action_required"
    assert spec.clear_probe == "probe:py:user_owned_secret_values_still_present"
    assert spec.dismiss_key == ("keys",)


def test_act_a_hand_added_secret_is_reported_by_name_only(project):
    path = _write(project, {"STALE_SECRET": VALUE, "KG_COLLECTION": "Acme_KnowledgeGraph"})
    before = path.read_bytes()

    user_owned_secrets.emit_deferral(project)

    entry = DeferralReport.read(project).entry_for(CID)
    assert entry is not None and entry.resolved_disposition == "action_required"
    assert "STALE_SECRET" in entry.detected and "will NOT remove" in entry.detected
    assert "KG_COLLECTION" not in entry.detected, "a routing key is not secret-shaped"
    assert "vct set" in entry.command_to_apply and "dismiss-deferral" in entry.command_to_apply
    assert VALUE not in json.dumps(entry.__dict__, default=str), "never a value"
    assert entry.dismiss_fields == {"keys": [".claude/settings.json:STALE_SECRET"]}
    assert path.read_bytes() == before, "reporting never touches the file"


def test_the_vscode_block_is_covered_too(project):
    _write(project, {"STALE_SECRET": VALUE}, ".vscode/settings.json", "claude-code.env")
    assert user_owned_secrets.found(project) == {".vscode/settings.json": ["STALE_SECRET"]}


def test_vco_written_keys_belong_to_the_sibling_condition_not_this_one(project):
    _write(project, {"GITHUB_TOKEN": VALUE, "LAUNCHER_KNOWN_TOKEN": VALUE})
    assert user_owned_secrets.found(project) == {}
    assert cp.retained_user_secret_values(project) == {
        ".claude/settings.json": ["GITHUB_TOKEN", "LAUNCHER_KNOWN_TOKEN"],
    }


def test_leave_alone_the_env_refresh_never_removes_a_user_owned_secret(project):
    path = _write(project, {"STALE_SECRET": VALUE})
    cp.apply_project_env(cp.project_env_from_db("pid-owned"))
    assert json.loads(path.read_text())["env"]["STALE_SECRET"] == VALUE
    assert user_owned_secrets.found(project) == {".claude/settings.json": ["STALE_SECRET"]}


def test_an_empty_value_is_not_reported(project):
    _write(project, {"STALE_SECRET": ""})
    user_owned_secrets.emit_deferral(project)
    assert not DeferralReport.read(project).has_condition(CID)


@pytest.mark.parametrize("fix", ["remove", "empty"])
def test_the_probe_clears_once_the_key_is_gone_or_empty(project, fix):
    _write(project, {"STALE_SECRET": VALUE, "EDITOR_THEME": "dark"})
    user_owned_secrets.emit_deferral(project)
    assert _probe(project) is True, "still applies"
    _write(project, {"EDITOR_THEME": "dark"} if fix == "remove" else {"STALE_SECRET": "", "EDITOR_THEME": "dark"})
    assert _probe(project) is False, "positive evidence the condition is over"


def test_an_unreadable_file_is_unknown_not_reported(project):
    (project / ".claude" / "settings.json").write_text('{"env": {"STALE_SECRET": "x"},, }')
    assert user_owned_secrets.found(project) == {}


def test_dismiss_holds_for_the_same_keys_and_refires_for_a_new_one(project):
    _write(project, {"STALE_SECRET": VALUE})
    user_owned_secrets.emit_deferral(project)
    _dismiss(project)
    assert not DeferralReport.read(project).has_condition(CID)

    user_owned_secrets.emit_deferral(project)
    assert not DeferralReport.read(project).has_condition(CID), "kept deliberately"

    _write(project, {"STALE_SECRET": VALUE, "ANOTHER_API_KEY": VALUE})
    user_owned_secrets.emit_deferral(project)
    entry = DeferralReport.read(project).entry_for(CID)
    assert entry is not None and "ANOTHER_API_KEY" in entry.detected, "a new key re-fires"


def test_a_real_bundle_update_emits_it(tmp_path, monkeypatch):
    """Wiring, end to end: an ``install-bundle --update`` of a project whose
    settings.json carries a hand-added secret records the condition (and does
    not remove the key)."""
    from tests.test_install_bundle import _make_fake_orchestrator

    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(tmp_path / "absent.db"))
    orch, proj = tmp_path / "orch", tmp_path / "proj"
    proj.mkdir()
    orch.mkdir()
    _make_fake_orchestrator(orch)
    project_init.install_project_bundle(proj, orchestrator_root=orch, update_mode=False)
    path = _write(proj, {"STALE_SECRET": VALUE})

    project_init.install_project_bundle(proj, orchestrator_root=orch, update_mode=True)

    entry = DeferralReport.read(proj).entry_for(CID)
    assert entry is not None and "STALE_SECRET" in entry.detected
    assert json.loads(path.read_text())["env"]["STALE_SECRET"] == VALUE
