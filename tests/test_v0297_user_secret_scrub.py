# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the env refresh removes the in-tree secret VALUES VCO can PROVE
it wrote, and nothing else.

``user_secret_values_retained_in_tree`` promised "the next env refresh removes
the value". The first fix made that true by NAME: every key the launcher knows
as a user secret was deleted from ``.claude/settings.json`` ``env`` — including
a key the user typed by hand that merely SHARES a name with a (paused) launcher
secret (review R2 F18, reproduced below). Owner rule: never destroy data
without positive evidence. Now a value is removed only when it EQUALS the value
the launcher stores for that key, read through the sanctioned resolver (hub,
active-gated) and compared in constant time; everything else is left
byte-for-byte and reported by ``user_owned_secret_value_in_tree``.

The resolver is replaced by an in-memory fake here (``stored``) — these tests
never reach a live hub, and never print, log or store a value.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import insert_rows, make_launcher_db
from vco_lib import config_projection as cp
from vco_lib import jsonc_edit, project_init, user_owned_secrets
from vco_lib.deferral_report import DeferralEntry, DeferralReport

CID = "user_secret_values_retained_in_tree"
KNOWN = "OPENAI_API_KEY"          # a launcher-known per-project user secret
SHARED = "SHARED_SERVICE_TOKEN"   # a launcher-known shared user secret (paused)
VALUE = "sk-legacy-in-tree-value-do-not-print"
TYPED = "sk-user-typed-this-by-hand"


@pytest.fixture()
def stored(monkeypatch) -> dict:
    """The launcher's stored values, by env key; a missing key answers
    ``absent`` (not active / not found), ``"<unknown>"`` answers ``unknown``."""
    values: dict = {}

    def fake(env_key: str, _root: Path):
        if values.get(env_key) == "<unknown>":
            return "unknown", None
        return ("ok", values[env_key]) if env_key in values else ("absent", None)

    monkeypatch.setattr(cp, "_stored_secret_value", fake)
    return values


@pytest.fixture()
def project(tmp_path: Path, monkeypatch, stored) -> Path:
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    db = make_launcher_db(tmp_path / "launcher.db", projects=[{
        "project_id": "pid-scrub", "name": "Acme", "folder_path": str(folder),
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


def _ledger(folder: Path) -> str:
    return "".join(
        p.read_text(encoding="utf-8")
        for p in (folder / ".claude" / "context").glob("UPDATE_DEFERRED.*")
    )


def _seed_deferral(folder: Path) -> None:
    report = DeferralReport.read(folder)
    report.add_entry(DeferralEntry(
        condition_id=CID, title="t", detected="d", why_deferred="w",
        command_to_apply="c", severity="warning",
    ))
    report.write(folder)


def test_the_bundle_carries_the_launcher_known_keys(project):
    assert cp.project_env_from_db("pid-scrub")["user_secret_known_keys"] == sorted([KNOWN, SHARED])


# ── ACT: the value equals the stored one ⇒ VCO wrote it ⇒ removed ──────────


def test_a_value_equal_to_the_stored_one_is_removed_and_the_users_keys_survive(project, stored):
    stored[KNOWN] = VALUE
    path = _settings(project, json.dumps({
        "hooks": {"Stop": []},
        "env": {KNOWN: VALUE, "MY_HAND_ADDED_TOKEN": "mine", "EDITOR_THEME": "dark"},
    }))
    assert cp.retained_user_secret_values(project) == {".claude/settings.json": [KNOWN]}

    _refresh()

    env = json.loads(path.read_text())["env"]
    assert KNOWN not in env
    assert env["MY_HAND_ADDED_TOKEN"] == "mine" and env["EDITOR_THEME"] == "dark"
    assert VALUE not in path.read_text()
    assert cp.retained_user_secret_values(project) == {}


def test_github_token_follows_the_same_rule_against_github_pat(project, stored):
    stored["GITHUB_TOKEN"] = VALUE
    path = _settings(project, json.dumps({"env": {"GITHUB_TOKEN": VALUE}}))
    _refresh()
    assert "GITHUB_TOKEN" not in json.loads(path.read_text())["env"]


def test_a_jsonc_file_keeps_its_comments(project, stored):
    stored[KNOWN] = VALUE
    path = _settings(project, (
        "{\n  // the team's settings\n  \"hooks\": {},\n"
        f"  \"env\": {{\"{KNOWN}\": \"{VALUE}\", \"EDITOR_THEME\": \"dark\",}}, /* keep */\n}}\n"
    ))
    _refresh()
    text = path.read_text(encoding="utf-8")
    assert "// the team's settings" in text and "/* keep */" in text
    assert VALUE not in text
    assert jsonc_edit.loads(text)["env"]["EDITOR_THEME"] == "dark"


def test_a_vscode_block_a_pre_pr27_launcher_wrote_is_scrubbed_too(project, stored):
    stored[KNOWN] = VALUE
    stored["GITHUB_TOKEN"] = VALUE
    vscode = _settings(project, json.dumps({
        "editor.formatOnSave": True,
        "claude-code.env": {KNOWN: VALUE, "GITHUB_TOKEN": VALUE, "OTHER": "x"},
    }), ".vscode/settings.json")
    _refresh()  # default surfaces do NOT include .vscode — the scrub still runs
    data = json.loads(vscode.read_text())
    assert data["claude-code.env"] == {"OTHER": "x"}
    assert data["editor.formatOnSave"] is True


def test_the_trail_names_the_key_and_never_the_value(project, stored, capsys):
    stored[KNOWN] = VALUE
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    _refresh()
    trail = _trail(project)
    assert KNOWN in trail and ".claude/settings.json" in trail and CID in trail
    out = capsys.readouterr()
    for surface in (trail, _ledger(project), out.out, out.err):
        assert VALUE not in surface


# ── LEAVE ALONE: a name match proves nothing ────────────────────────────────


def test_review_r2_f18_repro_a_hand_typed_key_sharing_a_paused_secrets_name_survives(project, stored):
    """The reviewer's exact scenario: shared ``OPENAI_API_KEY`` registered then
    PAUSED (the hub refuses it — ``absent``); the user hand-set the same name
    in settings.json. Before: removed, no ledger entry. Now: untouched, and
    reported as the user's."""
    db = Path(project.parent / "launcher.db")
    insert_rows(db, "secret_active_state", [
        {"scope": "shared", "project_id": "_user_shared_", "module_id": "user",
         "key": KNOWN, "requester_project_id": "*", "active": 0, "updated_at": 0},
    ])
    path = _settings(project, json.dumps({"env": {KNOWN: TYPED, "MY_OTHER": "keep"}}))

    _refresh()

    env = json.loads(path.read_text())["env"]
    assert env[KNOWN] == TYPED and env["MY_OTHER"] == "keep", "not VCO's to delete"
    assert user_owned_secrets.found(project) == {".claude/settings.json": [KNOWN]}
    assert "scrubbed_user_secret_value" not in _trail(project)


def test_same_name_different_value_is_left_and_reported(project, stored):
    stored[KNOWN] = VALUE
    path = _settings(project, json.dumps({"env": {KNOWN: TYPED}}))
    _refresh()
    assert json.loads(path.read_text())["env"][KNOWN] == TYPED
    assert user_owned_secrets.found(project) == {".claude/settings.json": [KNOWN]}


@pytest.mark.parametrize("answer", ["absent", "<unknown>"])
def test_a_paused_or_unresolvable_key_is_left_untouched(project, stored, answer):
    if answer == "<unknown>":
        stored[SHARED] = "<unknown>"
    path = _settings(project, json.dumps({"env": {SHARED: VALUE}}))
    _refresh()
    assert json.loads(path.read_text())["env"][SHARED] == VALUE, "no evidence ⇒ no removal"
    assert user_owned_secrets.found(project) == {".claude/settings.json": [SHARED]}


def test_the_compare_is_constant_time(project, stored, monkeypatch):
    import hmac

    calls: list[int] = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    stored[KNOWN] = VALUE
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    cp.classify_json_env_secrets(project)
    assert calls, "the value comparison goes through hmac.compare_digest"


def test_an_unreadable_settings_file_is_refused_and_recorded_not_scrubbed(project, stored):
    stored[KNOWN] = VALUE
    broken = f'{{"env": {{"{KNOWN}": "{VALUE}"}},, }}'
    path = _settings(project, broken)
    with pytest.raises(cp.SettingsWriteRefused):
        _refresh()
    assert path.read_text() == broken, "byte-identical"
    assert DeferralReport.read(project).has_condition("settings_write_refused_claude_settings_json")


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_the_refresh_clears_the_deferral_once_nothing_remains(project, stored):
    stored[KNOWN] = VALUE
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    _seed_deferral(project)
    assert project_init._scan_user_secret_values_retained(project) is True

    _refresh()

    assert project_init._scan_user_secret_values_retained(project) is False
    assert not DeferralReport.read(project).has_condition(CID), "paired clear"


def test_an_unanswerable_check_keeps_the_deferral(project, stored):
    stored[KNOWN] = "<unknown>"
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    assert cp.retained_user_secret_state(project) is None
    assert project_init._scan_user_secret_values_retained(project) is True, "no evidence it is over"


def test_the_deferral_stays_while_a_refused_file_still_holds_a_value(project, stored):
    """A JSONC edit the editor cannot verify (duplicate key) is refused; the
    proven value is still there, so the entry must not be cleared."""
    stored[KNOWN] = "b"
    _settings(project, f'{{\n  // x\n  "env": {{"{KNOWN}": "a", "{KNOWN}": "b"}},\n}}\n')
    _seed_deferral(project)
    with pytest.raises(cp.SettingsWriteRefused):
        _refresh()
    assert DeferralReport.read(project).has_condition(CID)


def test_the_deferral_is_emitted_only_for_proven_values(project, stored):
    _settings(project, json.dumps({"env": {"MY_HAND_ADDED_TOKEN": VALUE, KNOWN: TYPED}}))
    stored[KNOWN] = VALUE
    project_init._emit_user_secret_values_retained_deferral(project)
    assert not DeferralReport.read(project).has_condition(CID), (
        "neither key is provably VCO's: no promise to remove it"
    )
    _settings(project, json.dumps({"env": {KNOWN: VALUE}}))
    project_init._emit_user_secret_values_retained_deferral(project)
    entry = DeferralReport.read(project).entry_for(CID)
    assert entry is not None and KNOWN in entry.detected
    assert VALUE not in json.dumps(entry.__dict__, default=str)


# ── F21: the managed block ends at the first END after its BEGIN ────────────


def test_a_stray_end_marker_before_the_block_does_not_hide_a_managed_export(tmp_path):
    env = tmp_path / ".claude" / "env"
    env.parent.mkdir(parents=True)
    env.write_text(
        f"{cp.CLAUDE_ENV_MANAGED_END}\n# a stray END the user pasted earlier\n"
        f"{cp.CLAUDE_ENV_MANAGED_BEGIN}\nexport STALE_SECRET=\"x\"\n{cp.CLAUDE_ENV_MANAGED_END}\n",
        encoding="utf-8",
    )
    assert cp.retained_secret_keys_in(env) == ["STALE_SECRET"]
