# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the env refresh removes pre-v0.2.73 in-tree user-secret VALUES.

``user_secret_values_retained_in_tree`` told the user "the next env refresh
scrubs the value automatically". It did not: the refresh
(``config_projection.apply_project_env``) removed only canonical keys, so the
value of a user secret a pre-v0.2.73 launcher wrote into
``.claude/settings.json`` ``env`` stayed forever. Now every apply strips the
launcher-KNOWN user-secret keys (the DB's per-project + shared + global
buckets) through the ONE JSON writer, trails each removal by key NAME, and
clears the deferral once nothing remains. Keys the launcher does not know are
the user's own and are never touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import insert_rows, make_launcher_db
from vco_lib import config_projection as cp
from vco_lib import jsonc_edit, project_init
from vco_lib.deferral_report import DeferralEntry, DeferralReport

CID = "user_secret_values_retained_in_tree"
KNOWN = "OPENAI_API_KEY"          # a launcher-known per-project user secret
SHARED = "SHARED_SERVICE_TOKEN"   # a launcher-known shared user secret
VALUE = "sk-legacy-in-tree-value-do-not-print"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    db = make_launcher_db(tmp_path / "launcher.db", projects=[{
        "project_id": "pid-scrub", "name": "Scrub", "folder_path": str(folder),
    }])
    insert_rows(db, "secret_active_state", [
        {"scope": "per_project", "project_id": "pid-scrub", "module_id": "user",
         "key": KNOWN, "requester_project_id": "pid-scrub", "active": 1, "updated_at": 0},
        {"scope": "shared", "project_id": "_user_shared_", "module_id": "user",
         "key": SHARED, "requester_project_id": "*", "active": 0, "updated_at": 0},
    ])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    return folder


def _settings(folder: Path, text: str, rel: str = ".claude/settings.json") -> Path:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _refresh() -> None:
    cp.apply_project_env(cp.project_env_from_db("pid-scrub"))


def _trail(folder: Path) -> str:
    path = folder / ".claude" / "logs" / "auto-resolutions.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _seed_deferral(folder: Path) -> None:
    report = DeferralReport.read(folder)
    report.add_entry(DeferralEntry(
        condition_id=CID, title="t", detected="d", why_deferred="w",
        command_to_apply="c", severity="warning",
    ))
    report.write(folder)


def test_the_bundle_carries_the_launcher_known_keys(project):
    assert cp.project_env_from_db("pid-scrub")["user_secret_known_keys"] == sorted([KNOWN, SHARED])


def test_a_legacy_value_is_removed_and_the_users_own_keys_survive(project):
    path = _settings(project, json.dumps({
        "hooks": {"Stop": []},
        "env": {KNOWN: VALUE, SHARED: VALUE, "MY_HAND_ADDED_TOKEN": "mine", "USER_KEY": "keep"},
    }))
    assert cp.retained_user_secret_values(project) == {".claude/settings.json": sorted([KNOWN, SHARED])}

    _refresh()

    env = json.loads(path.read_text())["env"]
    assert KNOWN not in env and SHARED not in env
    assert env["MY_HAND_ADDED_TOKEN"] == "mine", "a key VCO never wrote is never touched"
    assert env["USER_KEY"] == "keep"
    assert VALUE not in path.read_text()
    assert cp.retained_user_secret_values(project) == {}


def test_a_jsonc_file_keeps_its_comments(project):
    path = _settings(project, (
        "{\n  // the team's settings\n  \"hooks\": {},\n"
        f"  \"env\": {{\"{KNOWN}\": \"{VALUE}\", \"USER_KEY\": \"keep\",}}, /* keep */\n}}\n"
    ))
    _refresh()
    text = path.read_text(encoding="utf-8")
    assert "// the team's settings" in text and "/* keep */" in text
    assert VALUE not in text
    assert jsonc_edit.loads(text)["env"]["USER_KEY"] == "keep"


def test_a_vscode_block_a_pre_pr27_launcher_wrote_is_scrubbed_too(project):
    vscode = _settings(project, json.dumps({
        "editor.formatOnSave": True,
        "claude-code.env": {KNOWN: VALUE, "GITHUB_TOKEN": VALUE, "OTHER": "x"},
    }), ".vscode/settings.json")
    _refresh()  # default surfaces do NOT include .vscode — the scrub still runs
    data = json.loads(vscode.read_text())
    assert data["claude-code.env"] == {"OTHER": "x"}
    assert data["editor.formatOnSave"] is True


def test_an_unreadable_settings_file_is_refused_and_recorded_not_scrubbed(project):
    broken = f'{{"env": {{"{KNOWN}": "{VALUE}"}},, }}'
    path = _settings(project, broken)
    with pytest.raises(cp.SettingsWriteRefused):
        _refresh()
    assert path.read_text() == broken, "byte-identical"
    assert DeferralReport.read(project).has_condition("settings_write_refused_claude_settings_json")


def test_the_trail_names_the_key_and_never_the_value(project):
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    _refresh()
    trail = _trail(project)
    assert KNOWN in trail and ".claude/settings.json" in trail and CID in trail
    assert VALUE not in trail


def test_the_refresh_clears_the_deferral_once_nothing_remains(project):
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    _seed_deferral(project)
    assert project_init._scan_user_secret_values_retained(project) is True

    _refresh()

    assert project_init._scan_user_secret_values_retained(project) is False
    assert not DeferralReport.read(project).has_condition(CID), "paired clear"


def test_the_deferral_stays_while_a_refused_file_still_holds_a_value(project):
    """A JSONC edit the editor cannot verify (duplicate key) is refused; the
    value is still there, so the entry must not be cleared."""
    _settings(project, f'{{\n  // x\n  "env": {{"{KNOWN}": "a", "{KNOWN}": "b"}},\n}}\n')
    _seed_deferral(project)
    with pytest.raises(cp.SettingsWriteRefused):
        _refresh()
    assert DeferralReport.read(project).has_condition(CID)


def test_the_deferral_names_keys_and_is_emitted_only_for_what_the_refresh_would_strip(project):
    _settings(project, json.dumps({"env": {"MY_HAND_ADDED_TOKEN": VALUE}}))
    project_init._emit_user_secret_values_retained_deferral(project)
    assert not DeferralReport.read(project).has_condition(CID), (
        "a key the launcher does not know is the user's own: no promise to remove it"
    )
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    project_init._emit_user_secret_values_retained_deferral(project)
    entry = DeferralReport.read(project).entry_for(CID)
    assert entry is not None and KNOWN in entry.detected
    assert VALUE not in json.dumps(entry.__dict__, default=str)
