# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-HTAB — a disable that CARRIES to a newly shipped event gets its
own parked row, so the launcher's Hooks tab lists each (hook, event) as its
own toggle and re-enabling one restores exactly that registration at once.

R9 F3 taught the bundle merge that a hook parked on one event stays off on
the events an upgrade newly ships it on (``stop-drain-citations`` gaining
``SubagentStop`` and ``SessionEnd``). Python never writes launcher.db, so those
kept-out registrations had no row: the tab showed only the parked Stop row and
the new events came back only at the next bundle update after a re-enable.

The SSOT for "which registrations are newly blocked" is the merge itself —
:func:`vco_lib.parked_hooks.carried_registrations` runs it and reads its
kept-out records — and ``python -m vco_lib.parked_hooks carried`` hands the
answer (plus the parked entry to store) to the launcher.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import hooks_settings, parked_hooks, settings_merge  # noqa: E402
from vco_lib.parked_hooks import ParkedHook, ParkedHooksState  # noqa: E402

TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}
STEM = "stop-drain-citations"


def _template(os_name: str) -> dict:
    return json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))


def _drain_cmd(template: dict, event: str = "Stop") -> str:
    for g in template["hooks"][event]:
        for h in g["hooks"]:
            if STEM in h["command"]:
                return h["command"]
    raise AssertionError(f"the template ships {STEM} on {event}")


def _without_stem(user: dict, event: str) -> dict:
    groups = []
    for g in user["hooks"].get(event, []):
        inner = [h for h in g["hooks"] if STEM not in h["command"]]
        if inner:
            groups.append({**g, "hooks": inner})
    if groups:
        user["hooks"][event] = groups
    else:
        user["hooks"].pop(event, None)
    return user


def _parked_on_stop_everywhere_off(template: dict) -> dict:
    """A pre-0.2.102 file whose user disabled the drain on Stop: the hook
    runs on no event (no SessionEnd, no SubagentStop drain, no Stop drain)."""
    user = copy.deepcopy(template)
    for event in ("SessionEnd", "SubagentStop", "Stop"):
        _without_stem(user, event)
    return user


def _doc(data: dict) -> hooks_settings.SettingsDoc:
    return hooks_settings.SettingsDoc(Path("settings.json"), data, 2, True)


def _events_running(hooks: dict) -> list:
    return sorted(ev for ev, groups in hooks.items()
                  for g in groups for h in g.get("hooks", []) if STEM in h["command"])


# ── The decision (one home: the merge's own kept-out records) ─────────────


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_a_stop_disable_yields_one_parked_entry_per_new_event(os_name: str) -> None:
    """ACT. Parked on Stop, running nowhere -> SubagentStop and SessionEnd
    each get a record; Stop itself is event-scoped (it has its own row)."""
    template = _template(os_name)
    user = _parked_on_stop_everywhere_off(template)
    rows = parked_hooks.carried_registrations(
        user["hooks"], template["hooks"], [ParkedHook("Stop", "", _drain_cmd(template))])
    assert sorted(r["event"] for r in rows) == ["SessionEnd", "SubagentStop"]
    for r in rows:
        assert r["carried_from_event"] == "Stop"
        assert r["command"] == _drain_cmd(template, r["event"])
        entry = json.loads(r["parked_json"])
        assert entry["schema"] == hooks_settings.PARKED_SCHEMA_VERSION
        assert entry["event"] == r["event"] and entry["group_removed"] is True
        shipped = next(h for g in template["hooks"][r["event"]] for h in g["hooks"]
                       if STEM in h["command"])
        assert entry["item"] == shipped, "the shipped registration, verbatim"
        assert r["timeout_seconds"] == shipped.get("timeout")


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_restoring_one_carried_entry_adds_exactly_that_registration(os_name: str) -> None:
    """ACT (re-enable). The SessionEnd entry restores SessionEnd as the
    template ships it — nothing else — and the merge then adds nothing for
    it (no duplicate, the other carried event still kept out by its row)."""
    template = _template(os_name)
    user = _parked_on_stop_everywhere_off(template)
    stop_row = ParkedHook("Stop", "", _drain_cmd(template))
    rows = {r["event"]: r for r in parked_hooks.carried_registrations(
        user["hooks"], template["hooks"], [stop_row])}

    doc = _doc(copy.deepcopy(user))
    assert hooks_settings.insert_hook(doc, json.loads(rows["SessionEnd"]["parked_json"]))
    assert _events_running(doc.data["hooks"]) == ["SessionEnd"]
    assert doc.data["hooks"]["SessionEnd"] == template["hooks"]["SessionEnd"]

    # The launcher now holds three event-scoped rows (Stop, SubagentStop;
    # SessionEnd was unparked by the enable). The next bundle merge keeps the
    # two parked ones out and leaves the restored one exactly as it is.
    still_parked = [stop_row, ParkedHook("SubagentStop", "", rows["SubagentStop"]["command"])]
    kept: list = []
    merged = settings_merge.merge_hooks_block(
        doc.data["hooks"], template["hooks"],
        parked=ParkedHooksState(readable=True, hooks=tuple(still_parked)), kept_out=kept)
    assert _events_running(merged) == ["SessionEnd"]
    assert sorted(r["event"] for r in kept) == ["Stop", "SubagentStop"]
    assert not any(r.get("carried_from_event") for r in kept), "now event-scoped"
    assert parked_hooks.carried_registrations(
        doc.data["hooks"], template["hooks"], still_parked) == []


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_rows_written_once_are_not_carried_again(os_name: str) -> None:
    """Idempotent: once each new event has its own parked row, nothing is
    carried any more."""
    template = _template(os_name)
    user = _parked_on_stop_everywhere_off(template)
    parked = [ParkedHook("Stop", "", _drain_cmd(template)),
              ParkedHook("SubagentStop", "", _drain_cmd(template, "SubagentStop")),
              ParkedHook("SessionEnd", "", _drain_cmd(template, "SessionEnd"))]
    assert parked_hooks.carried_registrations(user["hooks"], template["hooks"], parked) == []


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_a_hook_still_running_somewhere_carries_nothing(os_name: str) -> None:
    """LEAVE-ALONE. Disabled on Stop but running on SubagentStop: the user
    turned off one event, so SessionEnd is a normal add — no row."""
    template = _template(os_name)
    user = _without_stem(copy.deepcopy(template), "Stop")
    user["hooks"].pop("SessionEnd")
    assert parked_hooks.carried_registrations(
        user["hooks"], template["hooks"], [ParkedHook("Stop", "", _drain_cmd(template))]) == []


def test_nothing_parked_or_no_template_carries_nothing() -> None:
    """LEAVE-ALONE: no parked rows, or an unusable template, is no answer."""
    template = _template("linux")
    user = _parked_on_stop_everywhere_off(template)
    assert parked_hooks.carried_registrations(user["hooks"], template["hooks"], []) == []
    assert parked_hooks.carried_registrations(
        user["hooks"], None, [ParkedHook("Stop", "", _drain_cmd(template))]) == []


def test_an_if_group_carries_as_one_items_entry() -> None:
    """A carried registration that is an ``if`` group (several rules, one
    command) becomes ONE parked entry in the group-park ``items`` form, the
    shape insert_hook restores as a unit."""
    cmd = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/fake-guard.sh"'
    rules = [{"type": "command", "command": cmd, "if": "Bash(git *)"},
             {"type": "command", "command": cmd, "if": "Bash(rm *)"}]
    template_hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": rules}],
                      "Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}
    rows = parked_hooks.carried_registrations({}, template_hooks, [ParkedHook("Stop", "", cmd)])
    assert [r["event"] for r in rows] == ["PreToolUse"]
    entry = json.loads(rows[0]["parked_json"])
    assert "item" not in entry and [rec["item"] for rec in entry["items"]] == rules
    doc = _doc({"hooks": {}})
    assert hooks_settings.insert_hook(doc, entry)
    assert doc.data["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": rules}]


def test_the_template_path_has_one_home() -> None:
    """project_init's private name is the bundle_settings_io function, not a
    second copy of the OS pick."""
    from vco_lib import bundle_settings_io, project_init

    assert project_init._settings_template_path is bundle_settings_io.settings_template_path


# ── The machine CLI the launcher calls ─────────────────────────────────────


def _run_cli(request: dict, *, root: Path = REPO_ROOT) -> tuple[int, dict]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "WEAVIATE_URL": "http://127.0.0.1:9"}
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.parked_hooks", "carried",
         "--orchestrator-root", str(root)],
        input=json.dumps(request), capture_output=True, text=True, cwd=REPO_ROOT,
        env=env, timeout=120, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"one JSON object on stdout: {proc.stdout!r} {proc.stderr!r}"
    return proc.returncode, json.loads(lines[0])


def _project(tmp_path: Path, hooks: dict) -> Path:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
    return tmp_path


def test_cli_answers_from_the_callers_rows_and_the_projects_file(tmp_path: Path) -> None:
    from vco_lib.bundle_settings_io import settings_template_path

    template = json.loads(settings_template_path(REPO_ROOT).read_text(encoding="utf-8"))
    folder = _project(tmp_path, _parked_on_stop_everywhere_off(template)["hooks"])
    rc, out = _run_cli({"project_folder": str(folder), "parked": [
        {"event": "Stop", "matcher": "", "command": "bash .claude/hooks/stop-drain-citations.sh"}]})
    assert rc == 0 and out["ok"] is True
    assert sorted(r["event"] for r in out["carried"]) == ["SessionEnd", "SubagentStop"]
    assert {r["parked_command"] for r in out["carried"]} == {
        "bash .claude/hooks/stop-drain-citations.sh"}


def test_cli_with_no_parked_rows_answers_empty_without_reading_anything(tmp_path: Path) -> None:
    rc, out = _run_cli({"project_folder": str(tmp_path / "absent"), "parked": []})
    assert (rc, out) == (0, {"ok": True, "carried": []})


@pytest.mark.parametrize("body", ["{not json", "{\"oops\": [}"])
def test_cli_unreadable_settings_is_a_refusal_never_an_answer(tmp_path: Path, body: str) -> None:
    """An unreadable settings.json could register the hook, so nothing is
    declared carried: refusal with a code, exit 1 (the launcher writes nothing)."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(body, encoding="utf-8")
    rc, out = _run_cli({"project_folder": str(tmp_path), "parked": [
        {"event": "Stop", "matcher": "", "command": "bash .claude/hooks/stop-drain-citations.sh"}]})
    assert rc == 1 and out == {"ok": False, "code": "settings_unreadable",
                               "error": out["error"]}


def test_cli_bad_request_is_a_refusal(tmp_path: Path) -> None:
    rc, out = _run_cli({"parked": "nope"})
    assert rc == 1 and out["code"] == "bad_request"


def test_cli_missing_template_is_a_refusal(tmp_path: Path) -> None:
    """The template is read from the clone the caller names — the launcher's
    install root, never an ambient env var — and a clone without it refuses."""
    fake_root = tmp_path / "clone"
    (fake_root / "templates").mkdir(parents=True)
    (tmp_path / "proj").mkdir()
    folder = _project(tmp_path / "proj", {})
    rc, out = _run_cli({"project_folder": str(folder), "parked": [
        {"event": "Stop", "matcher": "", "command": "bash .claude/hooks/x.sh"}]}, root=fake_root)
    assert rc == 1 and out["code"] == "template_unreadable"
