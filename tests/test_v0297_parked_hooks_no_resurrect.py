# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A bundle update must not switch back on a hook the user disabled from the
launcher (v0.2.97, `vco_lib.parked_hooks`).

Disabling a hook REMOVES its entry from `.claude/settings.json` and parks the
removed bytes in `launcher.db` (`project_hooks.disabled_entry_json`). Before
this fix the bundle merge saw the shipped registration missing and appended
it again on every update, while the Hooks tab still said Disabled.

Each behaviour is pinned both ways — the act and the leave-alone — and the
parked state is driven through the REAL launcher schema
(`tests/common/launcher_db_fixture.py` applies the migration SQL), never a
hand-written table. Every DB lives under `tmp_path`; production code is
pointed at it with `VCT_LAUNCHER_DB_PATH`.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.launcher_db_fixture import (
    create_corrupt_launcher_db,
    create_empty_launcher_db,
    insert_rows,
    make_launcher_db,
    now_ms,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import hooks_settings, parked_hooks, project_init, settings_merge  # noqa: E402
from vco_lib.parked_hooks import (  # noqa: E402
    ParkedHook,
    ParkedHooksState,
    find_parked_match,
    read_parked_hooks,
    same_hook_command,
)

GUARD = '[ -n "$VCT_DISABLE_HOOKS" ] || '
CONTAINERS = "bash .claude/hooks/ensure-containers.sh"
LOADER = "bash .claude/hooks/session-start-kg-loader.sh"
NOTIFY = "bash .claude/hooks/notify-stop.sh"
SUMMARY = "bash .claude/hooks/kg-summary-generator.sh"
USER_OWN = "python3 /home/me/my-own-hook.py --loud"


def _template() -> dict:
    """A slice of the shipped template's shape: an event with two hooks in
    one group, a single-hook event, and one script under two matchers."""
    return {
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": CONTAINERS, "timeout": 15, "async": True},
                {"type": "command", "command": LOADER, "timeout": 5},
            ]}],
            "Stop": [{"hooks": [{"type": "command", "command": NOTIFY}]}],
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": SUMMARY}]},
                {"matcher": "Write", "hooks": [{"type": "command", "command": SUMMARY}]},
            ],
        }
    }


def _commands(settings: dict, event: str) -> list[str]:
    return [
        h["command"]
        for group in (settings.get("hooks") or {}).get(event, [])
        for h in group.get("hooks", [])
    ]


def _known(*hooks: tuple[str, str, str]) -> ParkedHooksState:
    return ParkedHooksState(
        readable=True, hooks=tuple(ParkedHook(*h) for h in hooks), source="launcher_db",
    )


UNREADABLE = ParkedHooksState(readable=False, source="unreadable", detail="corrupt")


# ---------------------------------------------------------------------------
# 1. identity — the same hook across eras, and nothing else
# ---------------------------------------------------------------------------


def test_a_legacy_guarded_command_is_the_same_hook_as_the_unprefixed_one():
    assert same_hook_command(GUARD + CONTAINERS, CONTAINERS)
    assert same_hook_command("bash .claude\\hooks\\ensure-containers.sh", CONTAINERS)


def test_different_scripts_and_user_commands_are_not_the_same_hook():
    assert not same_hook_command(CONTAINERS, LOADER)
    assert not same_hook_command(USER_OWN, CONTAINERS)
    # A VCO path at an ARGUMENT position is the user's own hook, not VCO's.
    assert not same_hook_command("bash wrap.sh --target .claude/hooks/notify-stop.sh", NOTIFY)


def test_an_inline_command_matches_across_the_guard_prefix_by_normalisation():
    assert same_hook_command(GUARD + "echo  done", "echo done")
    assert not same_hook_command("echo done", "echo other")


# ---------------------------------------------------------------------------
# 2. the pure merge
# ---------------------------------------------------------------------------


def _user_missing_containers() -> dict:
    """What the file looks like after the user disabled ensure-containers."""
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": LOADER, "timeout": 5}]}],
        "Stop": [{"hooks": [{"type": "command", "command": NOTIFY}]}],
        "PostToolUse": _template()["hooks"]["PostToolUse"],
    }}


def test_a_parked_hook_is_not_re_added_to_an_event_the_user_has():
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        _user_missing_containers(), _template(),
        parked=_known(("SessionStart", "", CONTAINERS)), kept_out=kept,
    )
    assert _commands(merged, "SessionStart") == [LOADER]
    assert [(r["command"], r["reason"]) for r in kept] == [(CONTAINERS, "parked")]


def test_a_missing_hook_that_is_not_parked_is_still_re_added():
    """Leave-alone: the fix must not stop the merge healing a lost line."""
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        _user_missing_containers(), _template(),
        parked=_known(("Stop", "", NOTIFY)), kept_out=kept,
    )
    assert CONTAINERS in _commands(merged, "SessionStart")
    assert kept == []


def test_a_parked_row_with_the_legacy_guard_still_keeps_the_new_command_out():
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        _user_missing_containers(), _template(),
        parked=_known(("SessionStart", "", GUARD + CONTAINERS)), kept_out=kept,
    )
    assert _commands(merged, "SessionStart") == [LOADER]
    assert kept[0]["parked_command"] == GUARD + CONTAINERS


def test_the_users_own_hooks_are_never_touched_by_parked_state():
    user = _user_missing_containers()
    user["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": USER_OWN})
    merged = settings_merge.smart_merge_settings(
        user, _template(),
        # A parked row naming the user's own command is irrelevant to the
        # template, and one naming a present VCO hook must not remove it.
        parked=_known(("Stop", "", USER_OWN), ("Stop", "", NOTIFY)),
    )
    assert _commands(merged, "Stop") == [NOTIFY, USER_OWN]


def test_a_whole_event_the_user_lacks_comes_back_minus_the_parked_hook():
    user = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": NOTIFY}]}]}}
    merged = settings_merge.smart_merge_settings(
        user, _template(), parked=_known(("SessionStart", "", CONTAINERS)),
    )
    assert _commands(merged, "SessionStart") == [LOADER]
    assert merged["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 5


def test_an_event_whose_only_hook_is_parked_is_not_recreated():
    user = {"hooks": {"SessionStart": _template()["hooks"]["SessionStart"]}}
    merged = settings_merge.smart_merge_settings(
        user, _template(), parked=_known(("Stop", "", NOTIFY)),
    )
    assert "Stop" not in merged["hooks"]
    # Leave-alone: without the parked row the event IS recreated.
    healed = settings_merge.smart_merge_settings(user, _template(), parked=_known())
    assert _commands(healed, "Stop") == [NOTIFY]


def test_a_hooks_key_deleted_by_disabling_everything_is_not_copied_back():
    """The launcher deletes the `hooks` key when the last hook goes."""
    every = [
        ("SessionStart", "", CONTAINERS), ("SessionStart", "", LOADER),
        ("Stop", "", NOTIFY), ("PostToolUse", "Edit", SUMMARY),
        ("PostToolUse", "Write", SUMMARY),
    ]
    merged = settings_merge.smart_merge_settings(
        {"permissions": {}}, _template(), parked=_known(*every),
    )
    assert "hooks" not in merged
    merged_one_left = settings_merge.smart_merge_settings(
        {"permissions": {}}, _template(), parked=_known(*every[1:]),
    )
    assert _commands(merged_one_left, "SessionStart") == [CONTAINERS]


def test_disabling_one_matcher_of_a_multi_matcher_script_keeps_the_other():
    user = {"hooks": {"Stop": _template()["hooks"]["Stop"]}}
    merged = settings_merge.smart_merge_settings(
        user, _template(), parked=_known(("PostToolUse", "Edit", SUMMARY)),
    )
    assert [g["matcher"] for g in merged["hooks"]["PostToolUse"]] == ["Write"]


def test_a_drifted_matcher_matches_only_when_the_template_is_unambiguous():
    groups = _template()["hooks"]["PostToolUse"]
    stop_groups = _template()["hooks"]["Stop"]
    # Stop ships notify-stop under ONE matcher: an old matcher still matches.
    assert find_parked_match(
        [ParkedHook("Stop", "*", NOTIFY)], "Stop", "", NOTIFY, stop_groups) is not None
    # PostToolUse ships the summary script under TWO: an unknown old matcher
    # cannot say which one the user meant.
    assert find_parked_match(
        [ParkedHook("PostToolUse", "Edit(*)", SUMMARY)],
        "PostToolUse", "Edit", SUMMARY, groups) is None


def test_a_hook_retired_while_parked_matches_nothing_and_breaks_nothing():
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        _user_missing_containers(), _template(),
        parked=_known(("Stop", "", "bash .claude/hooks/cost-tracker.sh")), kept_out=kept,
    )
    assert CONTAINERS in _commands(merged, "SessionStart")
    assert kept == []


def test_unreadable_state_withholds_every_missing_registration_but_still_supersedes():
    user = _user_missing_containers()
    user["hooks"]["Stop"][0]["hooks"][0]["command"] = GUARD + NOTIFY
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        user, _template(), parked=UNREADABLE, kept_out=kept,
    )
    assert _commands(merged, "SessionStart") == [LOADER]
    assert _commands(merged, "Stop") == [NOTIFY], "a PRESENT stale form is still healed"
    assert [(r["command"], r["reason"]) for r in kept] == [
        (CONTAINERS, "parked_state_unreadable")]


def test_parked_none_is_byte_for_byte_the_old_behaviour():
    user = _user_missing_containers()
    assert settings_merge.smart_merge_settings(user, _template(), parked=None) == \
        settings_merge.smart_merge_settings(user, _template())
    assert settings_merge.smart_merge_settings(user, _template(), parked=_known()) == \
        settings_merge.smart_merge_settings(user, _template())


# ---------------------------------------------------------------------------
# 3. reading the parked state
# ---------------------------------------------------------------------------


def _park_row(db: Path, project_id: str, event: str, matcher: str, command: str,
              blob: str | None = '{"schema": 1}') -> None:
    insert_rows(db, "project_hooks", [{
        "project_id": project_id, "event": event, "matcher": matcher,
        "command": command, "enabled": 0 if blob else 1, "installed_at": now_ms(),
        "updated_at": now_ms(), "disabled_entry_json": blob,
    }])


def test_the_reader_returns_this_projects_parked_rows_only(tmp_path):
    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "Mine", "folder_path": mine},
        {"project_id": "p2", "name": "Other", "folder_path": other},
    ])
    _park_row(db, "p1", "SessionStart", "", CONTAINERS)
    _park_row(db, "p1", "Stop", "", NOTIFY, blob=None)  # mirrored, NOT parked
    _park_row(db, "p2", "Stop", "", NOTIFY)
    state = read_parked_hooks(mine, db_path=db)
    assert state.readable and state.source == "launcher_db"
    assert state.hooks == (ParkedHook("SessionStart", "", CONTAINERS),)


def test_no_launcher_db_is_a_readable_empty_answer(tmp_path):
    state = read_parked_hooks(tmp_path, db_path=tmp_path / "absent.db")
    assert state.readable and state.hooks == () and state.source == "no_launcher_db"


def test_an_unregistered_folder_and_a_pre_parking_schema_have_nothing_parked(tmp_path):
    db = make_launcher_db(tmp_path / "state")
    assert read_parked_hooks(tmp_path, db_path=db).source == "not_registered"
    old = create_empty_launcher_db(tmp_path / "old" / "launcher.db", up_to=41)
    state = read_parked_hooks(tmp_path, db_path=old)
    assert state.readable and state.source == "no_parking_schema"


def test_a_corrupt_db_is_unreadable_not_empty(tmp_path):
    db = create_corrupt_launcher_db(tmp_path / "launcher.db")
    state = read_parked_hooks(tmp_path, db_path=db)
    assert not state.readable and state.detail


# ---------------------------------------------------------------------------
# 4. through the bundle engine — every entry path lands here
# ---------------------------------------------------------------------------


def _fake_orchestrator(root: Path) -> Path:
    (root / "templates").mkdir(parents=True)
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    for os_name in ("linux", "windows"):
        (root / "templates" / f"settings.json.{os_name}.template").write_text(
            json.dumps(_template(), indent=2), encoding="utf-8")
    return root


def _legacy_settings() -> dict:
    """A pre-v0.2.97 install: every VCO command carries the guard."""
    tpl = _template()
    for groups in tpl["hooks"].values():
        for group in groups:
            for h in group["hooks"]:
                h["command"] = GUARD + h["command"]
    return tpl


@pytest.fixture
def world(tmp_path, monkeypatch):
    orch = _fake_orchestrator(tmp_path / "orch")
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.json"
    settings.write_text(json.dumps(_legacy_settings(), indent=2) + "\n", encoding="utf-8")
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "Proj", "folder_path": project}])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    return orch, project, settings, db


def _disable_from_launcher(settings: Path, db: Path, event: str, matcher: str, command: str) -> str:
    """What the launcher does: the ONE writer removes the entry, then the
    returned bytes are parked on the row."""
    doc = hooks_settings.load_settings(settings)
    parked = hooks_settings.remove_hook(doc, event, matcher, command)
    hooks_settings.write_settings(doc)
    blob = json.dumps(parked, ensure_ascii=False)
    _park_row(db, "p1", event, matcher, command, blob=blob)
    return blob


def _update(project: Path, orch: Path, **kw) -> dict:
    return project_init.install_project_bundle(
        project, orchestrator_root=orch, update_mode=True, **kw)


def test_update_keeps_a_parked_legacy_hook_out_and_heals_the_rest(world):
    orch, project, settings, db = world
    _disable_from_launcher(settings, db, "SessionStart", "", GUARD + CONTAINERS)
    result = _update(project, orch)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert _commands(data, "SessionStart") == [LOADER], "parked hook came back"
    assert _commands(data, "Stop") == [NOTIFY], "unparked legacy form not superseded"
    assert [r["command"] for r in result["parked_hooks_kept_out"]] == [CONTAINERS]
    assert not any("could not be read" in w for w in result["warnings"])


def test_update_re_adds_a_missing_hook_nobody_parked(world):
    orch, project, settings, db = world
    doc = hooks_settings.load_settings(settings)
    hooks_settings.remove_hook(doc, "SessionStart", "", GUARD + CONTAINERS)
    hooks_settings.write_settings(doc)  # removed by hand: no parked row
    result = _update(project, orch)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert CONTAINERS in _commands(data, "SessionStart")
    assert "parked_hooks_kept_out" not in result


def test_re_enabling_after_the_update_restores_a_working_entry(world):
    orch, project, settings, db = world
    blob = _disable_from_launcher(settings, db, "Stop", "", GUARD + NOTIFY)
    _update(project, orch)
    assert _commands(json.loads(settings.read_text()), "Stop") == []
    # Enable: the writer restores the parked bytes, then the launcher unparks.
    doc = hooks_settings.load_settings(settings)
    assert hooks_settings.insert_hook(doc, json.loads(blob)) is True
    hooks_settings.write_settings(doc)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE project_hooks SET disabled_entry_json = NULL, enabled = 1")
    conn.commit()
    conn.close()
    restored = _commands(json.loads(settings.read_text()), "Stop")
    assert restored == [GUARD + NOTIFY]
    # The next update normalises it to the current form — once, not stacked.
    _update(project, orch)
    assert _commands(json.loads(settings.read_text()), "Stop") == [NOTIFY]


def test_an_unreadable_db_withholds_missing_hooks_and_says_so(world, monkeypatch):
    orch, project, settings, _db = world
    doc = hooks_settings.load_settings(settings)
    hooks_settings.remove_hook(doc, "SessionStart", "", GUARD + CONTAINERS)
    hooks_settings.write_settings(doc)
    corrupt = create_corrupt_launcher_db(project.parent / "corrupt.db")
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(corrupt))
    result = _update(project, orch)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert _commands(data, "SessionStart") == [LOADER]
    warning = [w for w in result["warnings"] if "could not be read" in w]
    assert warning and CONTAINERS in warning[0]
    assert any(line.startswith("  WARNING settings.json: the hooks you disabled")
               for line in project_init.format_bundle_result_lines(result))


def test_a_readable_db_with_nothing_missing_prints_no_warning(world, monkeypatch):
    """Leave-alone for the notice: an unreadable DB with no absent
    registration decided nothing, so nothing is reported."""
    orch, project, _settings, _db = world
    corrupt = create_corrupt_launcher_db(project.parent / "corrupt.db")
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(corrupt))
    result = _update(project, orch)
    assert not [w for w in result["warnings"] if "launcher" in w and "hook" in w]


def test_create_withholds_known_parked_hooks(world):
    orch, project, settings, db = world
    _park_row(db, "p1", "Stop", "", NOTIFY)
    settings.unlink()
    result = project_init.install_project_bundle(project, orchestrator_root=orch)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert result["settings_action"] == "created"
    assert "Stop" not in data["hooks"]
    assert CONTAINERS in _commands(data, "SessionStart")


def test_create_with_an_unreadable_db_writes_every_hook_and_warns(world, monkeypatch):
    orch, project, settings, _db = world
    settings.unlink()
    monkeypatch.setenv(
        "VCT_LAUNCHER_DB_PATH", str(create_corrupt_launcher_db(project.parent / "c.db")))
    result = project_init.install_project_bundle(project, orchestrator_root=orch)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == _template()
    assert any("created with every shipped hook" in w for w in result["warnings"])


def test_dry_run_reports_the_kept_out_hook_and_writes_nothing(world):
    orch, project, settings, db = world
    _disable_from_launcher(settings, db, "SessionStart", "", GUARD + CONTAINERS)
    before = settings.read_bytes()
    result = _update(project, orch, dry_run=True)
    assert settings.read_bytes() == before
    assert [r["command"] for r in result["parked_hooks_kept_out"]] == [CONTAINERS]


def test_the_cli_path_keeps_a_parked_hook_out_with_no_launcher_running(world):
    """`python -m vco_lib.project_init install-bundle --update`: no hub, no
    launcher process — only launcher.db on disk."""
    orch, project, settings, db = world
    _disable_from_launcher(settings, db, "SessionStart", "", GUARD + CONTAINERS)
    env = child_env(VCT_LAUNCHER_DB_PATH=str(db), VCT_HUB_PORT="9")
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.project_init", "install-bundle",
         "--folder", str(project), "--orchestrator-root", str(orch), "--update", "--json"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    envelope = json.loads(proc.stdout)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert _commands(data, "SessionStart") == [LOADER]
    assert [r["command"] for r in envelope["parked_hooks_kept_out"]] == [CONTAINERS]


def test_the_reader_module_does_not_import_project_init():
    """`settings_merge` imports `parked_hooks`; neither may pull in
    `project_init` (the direction `test_v0295_settings_merge` pins)."""
    code = (
        "import sys\nimport vco_lib.parked_hooks\n"
        "print('vco_lib.project_init' in sys.modules)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                          env=child_env(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"
    assert parked_hooks.read_parked_hooks is project_init.read_parked_hooks


# ---------------------------------------------------------------------------
# 5. multi-matcher scripts: identity is (script, matcher)
# ---------------------------------------------------------------------------


def _summary_groups(settings: dict) -> list[tuple[str, list[str]]]:
    return [
        (g.get("matcher", ""), [h["command"] for h in g["hooks"]])
        for g in settings["hooks"].get("PostToolUse", [])
    ]


def test_a_lost_matcher_of_a_multi_matcher_script_is_re_added():
    user = {"hooks": {"PostToolUse": [
        {"matcher": "Write", "hooks": [{"type": "command", "command": SUMMARY}]}]}}
    merged = settings_merge.smart_merge_settings(user, _template())
    assert sorted(_summary_groups(merged)) == [("Edit", [SUMMARY]), ("Write", [SUMMARY])]


def test_a_single_matcher_hook_the_user_regrouped_is_not_duplicated():
    """Leave-alone: notify-stop ships under ONE matcher; the user moved it
    into their own group, and the event-wide rule still recognises it."""
    user = {"hooks": {"Stop": [
        {"matcher": "mine", "hooks": [{"type": "command", "command": NOTIFY}]}]}}
    merged = settings_merge.smart_merge_settings(user, _template())
    assert _commands(merged, "Stop") == [NOTIFY]


def test_legacy_multi_matcher_entries_are_superseded_not_stacked():
    user = {"hooks": {"PostToolUse": [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": GUARD + SUMMARY}]},
        {"matcher": "Write", "hooks": [{"type": "command", "command": GUARD + SUMMARY}]},
    ]}}
    merged = settings_merge.smart_merge_settings(user, _template())
    assert _summary_groups(merged) == [("Edit", [SUMMARY]), ("Write", [SUMMARY])]


def test_a_legacy_entry_under_one_matcher_plus_a_lost_one_heals_to_exactly_two():
    user = {"hooks": {"PostToolUse": [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": GUARD + SUMMARY}]}]}}
    once = settings_merge.smart_merge_settings(user, _template())
    assert sorted(_summary_groups(once)) == [("Edit", [SUMMARY]), ("Write", [SUMMARY])]
    assert settings_merge.smart_merge_settings(once, _template()) == once


@pytest.mark.parametrize("os_name", ["linux", "windows"])
def test_the_shipped_template_merges_into_itself_and_its_legacy_form_unchanged(os_name):
    """The real template: no duplicate anywhere, from either era."""
    tpl = json.loads((REPO_ROOT / "templates" / f"settings.json.{os_name}.template")
                     .read_text(encoding="utf-8"))
    assert settings_merge.smart_merge_settings(tpl, tpl) == tpl
    legacy = json.loads(json.dumps(tpl))
    for groups in legacy["hooks"].values():
        for group in groups:
            for h in group["hooks"]:
                if os_name == "linux":
                    h["command"] = GUARD + h["command"]
    assert settings_merge.smart_merge_settings(legacy, tpl)["hooks"] == tpl["hooks"]


# ---------------------------------------------------------------------------
# 6. parked AND running — reported, never repaired by removal
# ---------------------------------------------------------------------------

from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.parked_hooks import (  # noqa: E402
    CONFLICT_CID,
    conflict_still_present,
    find_live_conflicts,
)


def test_a_parked_legacy_row_with_the_live_current_form_is_a_conflict():
    live = {"Stop": [{"hooks": [{"type": "command", "command": NOTIFY}]}]}
    conflicts = find_live_conflicts(live, [ParkedHook("Stop", "", GUARD + NOTIFY)])
    assert conflicts == [{"event": "Stop", "matcher": "", "parked_command": GUARD + NOTIFY,
                          "live_command": NOTIFY}]


def test_a_live_entry_under_another_matcher_is_not_a_conflict():
    live = {"PostToolUse": [{"matcher": "Write", "hooks": [
        {"type": "command", "command": SUMMARY}]}]}
    assert find_live_conflicts(live, [ParkedHook("PostToolUse", "Edit", SUMMARY)]) == []
    assert find_live_conflicts(live, []) == []


def _resurrected(world) -> tuple:
    """The field state: parked in the DB, re-added in the current form."""
    orch, project, settings, db = world
    blob = _disable_from_launcher(settings, db, "Stop", "", GUARD + NOTIFY)
    doc = hooks_settings.load_settings(settings)
    hooks_settings.register_hook(doc, "Stop", "", NOTIFY)
    hooks_settings.write_settings(doc)
    return orch, project, settings, db, blob


def test_an_update_records_a_parked_and_running_hook_and_leaves_it_running(world):
    orch, project, settings, _db, _blob = _resurrected(world)
    _update(project, orch)
    assert _commands(json.loads(settings.read_text()), "Stop") == [NOTIFY], \
        "a running entry must never be removed for the user"
    report = DeferralReport.read(project)
    assert report.has_condition(CONFLICT_CID)
    text = (project / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
    assert NOTIFY in text and "Enable on its Disabled row" in text
    assert "turn off the row that shows it running" in text
    assert conflict_still_present(project) is True


def test_no_conflict_means_no_entry(world):
    orch, project, settings, db = world
    _disable_from_launcher(settings, db, "Stop", "", GUARD + NOTIFY)
    _update(project, orch)
    assert not DeferralReport.read(project).has_condition(CONFLICT_CID)
    assert conflict_still_present(project) is False


def test_a_dry_run_records_nothing(world):
    orch, project, _settings, _db, _blob = _resurrected(world)
    _update(project, orch, dry_run=True)
    assert not DeferralReport.read(project).has_condition(CONFLICT_CID)


def test_re_disabling_clears_the_entry_on_the_next_update(world):
    orch, project, settings, db, _blob = _resurrected(world)
    _update(project, orch)
    assert DeferralReport.read(project).has_condition(CONFLICT_CID)
    _disable_from_launcher(settings, db, "Stop", "", NOTIFY)  # remedy 1
    _update(project, orch)
    assert not DeferralReport.read(project).has_condition(CONFLICT_CID)
    assert _commands(json.loads(settings.read_text()), "Stop") == []


def test_enable_on_the_disabled_row_adds_nothing_and_clears_the_entry(world):
    orch, project, settings, db, blob = _resurrected(world)
    _update(project, orch)
    before = settings.read_bytes()
    doc = hooks_settings.load_settings(settings)
    assert hooks_settings.insert_hook(doc, json.loads(blob)) is False  # remedy 2
    assert settings.read_bytes() == before, "the legacy form must not be stacked"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE project_hooks SET disabled_entry_json = NULL, enabled = 1")
    conn.commit()
    conn.close()
    _update(project, orch)
    assert not DeferralReport.read(project).has_condition(CONFLICT_CID)
    assert _commands(json.loads(settings.read_text()), "Stop") == [NOTIFY]


def test_the_probe_keeps_the_entry_when_the_db_cannot_be_read(world, monkeypatch):
    _orch, project, _settings, _db, _blob = _resurrected(world)
    monkeypatch.setenv(
        "VCT_LAUNCHER_DB_PATH", str(create_corrupt_launcher_db(project.parent / "c.db")))
    assert conflict_still_present(project) is None


def test_the_condition_is_registered_with_its_probe():
    from vco_lib import deferral_probes
    from vco_lib.deferral_registry import clear_probe_for, disposition_for

    assert disposition_for(CONFLICT_CID) == "action_required"
    assert clear_probe_for(CONFLICT_CID) == "probe:py:parked_hook_conflict_still_present"
    assert deferral_probes.registry_probe_name(CONFLICT_CID) == \
        "parked_hook_conflict_still_present"
    assert "parked_hook_conflict_still_present" in deferral_probes.PROBES


# ---------------------------------------------------------------------------
# 7. restore idempotency by identity
# ---------------------------------------------------------------------------


def test_restoring_a_legacy_entry_under_another_matcher_still_inserts(tmp_path):
    """Leave-alone for the identity idempotency: another matcher is another
    registration."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "Write", "hooks": [{"type": "command", "command": SUMMARY}]},
        {"matcher": "Edit", "hooks": [{"type": "command", "command": GUARD + SUMMARY}]},
    ]}}, indent=2) + "\n", encoding="utf-8")
    doc = hooks_settings.load_settings(settings)
    parked = hooks_settings.remove_hook(doc, "PostToolUse", "Edit", GUARD + SUMMARY)
    assert hooks_settings.insert_hook(doc, parked) is True
