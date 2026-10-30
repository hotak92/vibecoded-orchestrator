# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105: duplicate hook GROUPS left behind by an update are folded.

The merge's older passes all work per COMMAND (supersede a stale spelling,
append a new one, scrub a retired one), so they never noticed that the same
shipped hooks sat in SEVERAL groups sharing one matcher. Measured after the
v0.2.104 update on a real install: FOUR `startup` SessionStart groups, so
`ensure-containers`, `verify-container-ports`, `ensure-code-embed-service`,
`session-start-kg-loader` and `context-size-check` each ran three extra times
per session start — and a second install carried a legacy `*`-shaped group
running kg-loader + context-size-check again beside the shipped `startup`
group.

What is pinned here:

  1. the reported shapes fold to ONE group, with no duplicate registration;
  2. a legacy all-source group that only repeats what a shipped group already
     runs is removed (and a group left with commands survives, trimmed);
  3. a group holding a command VCO does not ship is NEVER touched — it is
     reported instead, so the update summary can say why the duplicate stays;
  4. a matcher the template deliberately ships SEVERAL groups for is left
     alone (folding would diverge an upgrade from a fresh install);
  5. the shipped template is a fixed point, a fresh install is byte-equal to
     it, and a second merge changes nothing.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from vco_lib import settings_merge

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cmd(name: str) -> dict:
    return {"type": "command", "timeout": 10,
            "command": f'bash "${{CLAUDE_PROJECT_DIR:-.}}/.claude/hooks/{name}.sh"'}


SHIPPED = ("check-no-fork-bomb", "ensure-containers", "verify-container-ports",
           "ensure-code-embed-service", "session-start-ensure-hub",
           "session-start-kg-loader", "session-start-deferral-surface",
           "session-start-retrieval-health", "embedding-failures-surface",
           "context-size-check", "compact-context-reinject")

TEMPLATE = {"hooks": {
    "SessionStart": [
        {"matcher": "startup", "hooks": [_cmd(n) for n in SHIPPED]},
        {"matcher": "compact", "hooks": [_cmd("compact-context-reinject")]},
        {"matcher": "resume|clear|compact", "hooks": [_cmd("session-start-ensure-hub")]},
    ],
    # shipped on another event only — a legacy SessionStart group running it
    # is VCO's registration out of place, not a duplicate of one of these.
    "Stop": [{"hooks": [_cmd("notify-stop")]}],
}}


def _commands(group: dict) -> list[str]:
    return [h["command"] for h in group["hooks"]]


def _startup_groups(data: dict) -> list[dict]:
    return [g for g in data["hooks"]["SessionStart"] if g.get("matcher") == "startup"]


def _fold(user: dict, template: dict = TEMPLATE) -> tuple[dict, list]:
    records: list = []
    merged = settings_merge.smart_merge_settings(
        user, template, duplicate_groups=records)
    return merged, records


#: The measured shape: the pre-v0.2.97 five-hook group, the grown group, and
#: two groups the merge appended as new hooks shipped — all under `startup`.
SPLIT_STARTUP = {"hooks": {"SessionStart": [
    {"matcher": "startup", "hooks": [_cmd("ensure-containers"),
                                     _cmd("verify-container-ports"),
                                     _cmd("ensure-code-embed-service"),
                                     _cmd("session-start-kg-loader"),
                                     _cmd("context-size-check")]},
    {"matcher": "compact", "hooks": [_cmd("compact-context-reinject")]},
    {"matcher": "startup", "hooks": [_cmd(n) for n in SHIPPED[:9]]},
    {"matcher": "startup", "hooks": [_cmd("session-start-deferral-surface"),
                                     _cmd("session-start-retrieval-health")]},
    {"matcher": "startup", "hooks": [_cmd("compact-context-reinject")]},
    {"matcher": "resume|clear|compact", "hooks": [_cmd("session-start-ensure-hub")]},
]}}

#: The other measured shape: a legacy absent-matcher (every source) group
#: holding only shipped commands the shipped `startup` group already runs, in
#: the relative spelling VCO shipped before v0.2.97 and with the shipped keys.
def _legacy(name: str) -> dict:
    return {"type": "command", "timeout": 10,
            "command": f"bash .claude/hooks/{name}.sh"}


LEGACY_ALL_SOURCE = {"hooks": {"SessionStart": [
    {"hooks": [_legacy("session-start-kg-loader"), _legacy("context-size-check")]},
    {"matcher": "startup", "hooks": [_cmd(n) for n in SHIPPED]},
    {"matcher": "compact", "hooks": [_cmd("compact-context-reinject")]},
    {"matcher": "resume|clear|compact", "hooks": [_cmd("session-start-ensure-hub")]},
]}}


# ---------------------------------------------------------------------------
# 1. the reported shapes
# ---------------------------------------------------------------------------


def test_the_split_startup_groups_fold_to_one():
    merged, records = _fold(copy.deepcopy(SPLIT_STARTUP))
    groups = _startup_groups(merged)
    assert len(groups) == 1, "one group per matcher"
    assert len(_commands(groups[0])) == len(SHIPPED)
    assert sorted(_commands(groups[0])) == sorted(
        _cmd(n)["command"] for n in SHIPPED)
    folds = [r for r in records if r["action"] == "folded"]
    assert len(folds) == 1
    assert folds[0] == {
        "action": "folded", "event": "SessionStart", "matcher": "startup",
        "groups": 4,
        # every registration the fold dropped as a duplicate — four from the
        # old group, two from the group the merge had split them into
        "duplicate_commands": sorted({
            _cmd(n)["command"] for n in
            ("ensure-containers", "verify-container-ports",
             "ensure-code-embed-service", "session-start-kg-loader",
             "session-start-deferral-surface",
             "session-start-retrieval-health")}),
    }


def test_the_folded_group_is_in_template_order():
    """An upgraded file and a fresh install must end up the same shape."""
    merged, _ = _fold(copy.deepcopy(SPLIT_STARTUP))
    assert _commands(_startup_groups(merged)[0]) == [
        _cmd(n)["command"] for n in SHIPPED]


def test_the_legacy_all_source_group_is_gone():
    merged, records = _fold(copy.deepcopy(LEGACY_ALL_SOURCE))
    assert [g.get("matcher") for g in merged["hooks"]["SessionStart"]] == [
        "startup", "compact", "resume|clear|compact"]
    assert records[0]["action"] == "redundant_all_sources_removed"
    # the supersede pass has already anchored the relative spelling
    assert records[0]["commands"] == [
        _cmd("session-start-kg-loader")["command"],
        _cmd("context-size-check")["command"]]


def test_a_legacy_all_source_group_keeps_what_nothing_else_runs():
    """Trimmed, not dropped: only the commands another group already runs go.

    ``notify-stop.sh`` is a shipped hook, but no SessionStart group runs it —
    so the legacy group keeps it (a trim) instead of vanishing.
    """
    user = copy.deepcopy(LEGACY_ALL_SOURCE)
    user["hooks"]["SessionStart"][0]["hooks"].append(_legacy("notify-stop"))
    merged, records = _fold(user)
    legacy = [g for g in merged["hooks"]["SessionStart"] if not g.get("matcher")]
    assert len(legacy) == 1
    assert [h["command"].rsplit("/", 1)[-1] for h in legacy[0]["hooks"]] == [
        "notify-stop.sh"]
    assert records[0]["action"] == "redundant_all_sources_trimmed"
    # the supersede pass has already anchored the relative spelling
    assert records[0]["commands"] == [
        _cmd("session-start-kg-loader")["command"],
        _cmd("context-size-check")["command"]]


def test_a_legacy_group_in_a_spelling_vco_never_shipped_is_kept_and_reported():
    """``bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/x.sh`` runs a shipped script,
    but VCO never shipped that spelling: not provably VCO's, so not removed."""
    user = copy.deepcopy(LEGACY_ALL_SOURCE)
    split_var = ['bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/session-start-kg-loader.sh',
                 'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/context-size-check.sh']
    user["hooks"]["SessionStart"][0] = {"hooks": [
        {"type": "command", "timeout": 10, "command": c} for c in split_var]}
    snapshot = copy.deepcopy(user["hooks"]["SessionStart"][0])
    merged, records = _fold(user)
    assert merged["hooks"]["SessionStart"][0] == snapshot
    assert records == [{"action": "kept_user_command", "event": "SessionStart",
                        "matcher": "", "commands": split_var}]


def test_a_legacy_handler_with_the_users_own_timeout_is_kept():
    """A shipped spelling with keys VCO never shipped (timeout 5000) is the
    user's setting: that handler stays, the shipped-valued one goes."""
    user = copy.deepcopy(LEGACY_ALL_SOURCE)
    user["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 5000
    merged, records = _fold(user)
    legacy = [g for g in merged["hooks"]["SessionStart"] if not g.get("matcher")]
    loader = _cmd("session-start-kg-loader")["command"]
    assert legacy == [{"hooks": [{
        "type": "command", "timeout": 5000, "command": loader}]}]
    assert {"action": "kept_user_setting", "event": "SessionStart", "matcher": "",
            "commands": [loader]} in records
    assert {"action": "redundant_all_sources_trimmed", "event": "SessionStart",
            "matcher": "",
            "commands": [_cmd("context-size-check")["command"]]} in records


# ---------------------------------------------------------------------------
# 2. the user's own group: never deleted, reported
# ---------------------------------------------------------------------------


def test_a_duplicate_group_holding_the_users_own_command_is_kept_and_reported():
    user = copy.deepcopy(SPLIT_STARTUP)
    user["hooks"]["SessionStart"].append({
        "matcher": "startup",
        "hooks": [{"type": "command", "command": "bash .claude/hooks/my-own-hook.sh"}],
    })
    merged, records = _fold(user)
    kept = [g for g in merged["hooks"]["SessionStart"]
            if any(h["command"] == "bash .claude/hooks/my-own-hook.sh"
                   for h in g["hooks"])]
    assert len(kept) == 1, "the user's group survives byte for byte"
    assert len(_startup_groups(merged)) == 2, (
        "the VCO-shipped groups still fold, beside the user's group")
    assert records[-1] == {
        "action": "kept_user_command", "event": "SessionStart",
        "matcher": "startup", "commands": ["bash .claude/hooks/my-own-hook.sh"],
    }


def test_a_group_with_a_user_command_is_never_split_or_emptied():
    """The template's own hooks are added beside it (the merge is additive),
    but the two `startup` groups are never folded into one."""
    user = {"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [_cmd("ensure-containers")]},
        {"matcher": "startup", "hooks": [{"type": "command",
                                          "command": "bash ~/mine.sh"}]},
    ]}}
    merged, records = _fold(user)
    mine = [g for g in merged["hooks"]["SessionStart"]
            if any(h["command"] == "bash ~/mine.sh" for h in g["hooks"])]
    assert mine == [{"matcher": "startup",
                     "hooks": [{"type": "command", "command": "bash ~/mine.sh"}]}]
    assert len(_startup_groups(merged)) == 2
    assert [r["action"] for r in records] == ["folded", "kept_user_command"]


ENSURE = _cmd("ensure-containers")["command"]


@pytest.mark.parametrize("wrapped", [
    f"my-backup.sh && {ENSURE}",
    f"MY_FLAG=1 {ENSURE}",
    "notify-me; bash .claude/hooks/ensure-containers.sh",
    f"{ENSURE} > /tmp/ensure.log",
    f"{ENSURE} | tee -a ensure.log",
])
def test_a_command_that_wraps_a_shipped_hook_is_never_folded_or_rewritten(wrapped):
    """Review R1 H2: a command that only CONTAINS a shipped hook is the
    user's. Its group is never folded, its text is never replaced with the
    shipped spelling, and the update summary names it."""
    user = copy.deepcopy(TEMPLATE)
    user["hooks"]["SessionStart"].append({"matcher": "startup", "hooks": [
        {"type": "command", "command": wrapped, "timeout": 99}]})
    merged, records = _fold(user)
    assert {"matcher": "startup", "hooks": [
        {"type": "command", "command": wrapped, "timeout": 99}]} in \
        merged["hooks"]["SessionStart"]
    assert records == [{"action": "kept_user_command", "event": "SessionStart",
                        "matcher": "startup", "commands": [wrapped]}]


def test_a_wrapped_hook_in_the_only_group_is_kept_and_not_doubled():
    """The supersede pass used to replace ``my-backup.sh && bash …/x.sh``
    with the shipped spelling (the user's part gone). It is now the user's
    registration of that hook: kept verbatim, and no second copy appended."""
    wrapped = f"my-backup.sh && {ENSURE}"
    user = copy.deepcopy(TEMPLATE)
    startup = user["hooks"]["SessionStart"][0]["hooks"]
    startup[[h["command"] for h in startup].index(ENSURE)] = {
        "type": "command", "timeout": 10, "command": wrapped}
    merged, records = _fold(user)
    cmds = [h["command"] for g in merged["hooks"]["SessionStart"]
            if g.get("matcher") == "startup" for h in g["hooks"]]
    assert wrapped in cmds
    assert ENSURE not in cmds
    assert records == []


@pytest.mark.parametrize("edited", [
    f"{ENSURE} --strict",
    "bash .claude/hooks/ensure-containers.sh --strict",
    "bash .claude\\hooks\\ensure-containers.sh --strict --quiet",
])
def test_a_user_edited_argument_on_a_shipped_hook_is_kept_verbatim(edited):
    """Review R1 SB-1: an extra argument after the script is the user's edit.

    The supersede pass used to replace it with the shipped spelling, which
    has no argument — ``--strict`` disappeared with no record and no backup.
    Only the post-script tokens decide this, so the argument list is compared;
    a different path spelling or extra whitespace still heals below.
    """
    user = copy.deepcopy(TEMPLATE)
    startup = user["hooks"]["SessionStart"][0]["hooks"]
    startup[[h["command"] for h in startup].index(ENSURE)] = {
        "type": "command", "timeout": 10, "command": edited}
    merged, records = _fold(user)
    cmds = [h["command"] for g in merged["hooks"]["SessionStart"]
            if g.get("matcher") == "startup" for h in g["hooks"]]
    assert edited in cmds, "the user's argument survives byte for byte"
    assert ENSURE not in cmds, "and no second, argument-less copy is appended"
    assert records == []


def test_the_command_without_the_edited_argument_still_heals():
    """The leave-alone case: an argument difference is what keeps a command
    verbatim — the stale relative SPELLING of the same argument-less command
    is still rewritten to the shipped one (that is the upgrade path)."""
    stale = "bash .claude/hooks/ensure-containers.sh"
    user = copy.deepcopy(TEMPLATE)
    startup = user["hooks"]["SessionStart"][0]["hooks"]
    startup[[h["command"] for h in startup].index(ENSURE)] = {
        "type": "command", "timeout": 10, "command": stale}
    merged, records = _fold(user)
    cmds = [h["command"] for g in merged["hooks"]["SessionStart"]
            if g.get("matcher") == "startup" for h in g["hooks"]]
    assert ENSURE in cmds
    assert stale not in cmds
    assert records == []


def test_the_disable_guard_spelling_still_heals_after_the_argument_check():
    """The pre-v0.2.97 ``VCT_DISABLE_HOOKS`` prefix is a SHIPPED spelling:
    it carries no trailing argument, so it is still replaced by the current
    one (dropping only the retired guard is not a user edit)."""
    guarded = f'[ -n "$VCT_DISABLE_HOOKS" ] || {ENSURE}'
    user = copy.deepcopy(TEMPLATE)
    startup = user["hooks"]["SessionStart"][0]["hooks"]
    startup[[h["command"] for h in startup].index(ENSURE)] = {
        "type": "command", "timeout": 10, "command": guarded}
    merged, _ = _fold(user)
    cmds = [h["command"] for g in merged["hooks"]["SessionStart"]
            if g.get("matcher") == "startup" for h in g["hooks"]]
    assert ENSURE in cmds
    assert guarded not in cmds


#: A command that ships WITH an argument (wait-guard's mode token) — the
#: comparison must read the ARGUMENT LIST, not "any trailing token at all".
WAIT_PRE = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/wait-guard.sh" pretooluse'
TEMPLATE_WITH_ARGS = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "timeout": 10,
                                   "command": WAIT_PRE}]}]}}


def test_a_stale_spelling_of_a_command_that_ships_arguments_still_heals():
    """Leave-alone: the same argument list in an old spelling is the shipped
    command, so it is still rewritten (only the spelling changes)."""
    stale = "bash .claude/hooks/wait-guard.sh pretooluse"
    user = copy.deepcopy(TEMPLATE_WITH_ARGS)
    user["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = stale
    merged, records = _fold(user, TEMPLATE_WITH_ARGS)
    assert _commands(merged["hooks"]["PreToolUse"][0]) == [WAIT_PRE]
    assert records == []


def test_changing_a_shipped_argument_is_the_users_edit():
    """The act: a DIFFERENT argument is the user's command. The old supersede
    pass replaced the whole command with the template's, i.e. it silently
    changed the hook's mode — a deletion of the user's edit, and a behaviour
    change to boot."""
    edited = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/wait-guard.sh" posttooluse'
    user = copy.deepcopy(TEMPLATE_WITH_ARGS)
    user["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = edited
    merged, records = _fold(user, TEMPLATE_WITH_ARGS)
    cmds = _commands(merged["hooks"]["PreToolUse"][0])
    assert edited in cmds
    assert WAIT_PRE not in cmds
    assert records == []


def test_a_user_edited_timeout_survives_the_fold():
    """Review R1 H2: the duplicate that carries the user's own key wins over
    the shipped-valued copy, wherever it sits."""
    user = copy.deepcopy(SPLIT_STARTUP)
    third = user["hooks"]["SessionStart"][2]["hooks"]
    third[[h["command"] for h in third].index(ENSURE)]["timeout"] = 99
    merged, records = _fold(user)
    groups = _startup_groups(merged)
    assert len(groups) == 1
    ensure = [h for h in groups[0]["hooks"] if h["command"] == ENSURE]
    assert ensure == [{"type": "command", "timeout": 99, "command": ENSURE}]
    assert records[0]["action"] == "folded"


def test_two_different_user_timeouts_are_not_folded():
    """No single survivor keeps both settings, so nothing is folded."""
    user = copy.deepcopy(SPLIT_STARTUP)
    snapshot = copy.deepcopy(user)
    for i, timeout in ((0, 77), (2, 99)):
        group = user["hooks"]["SessionStart"][i]["hooks"]
        group[[h["command"] for h in group].index(ENSURE)]["timeout"] = timeout
    merged, records = _fold(user)
    assert len(_startup_groups(merged)) == len(_startup_groups(snapshot))
    assert records == [{"action": "kept_user_setting", "event": "SessionStart",
                        "matcher": "startup", "commands": [ENSURE]}]


# ---------------------------------------------------------------------------
# 3. leave-alone: a matcher the template deliberately splits
# ---------------------------------------------------------------------------

SPLIT_TEMPLATE = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [_cmd("wait-guard")]},
    {"matcher": "Bash", "hooks": [_cmd("pre-vercel-token-guard")]},
]}}


def test_a_matcher_the_template_ships_two_groups_for_is_left_alone():
    user = copy.deepcopy(SPLIT_TEMPLATE)
    merged, records = _fold(user, SPLIT_TEMPLATE)
    assert merged == user
    assert records == []


def test_no_duplicate_means_no_write_and_no_record():
    user = copy.deepcopy(TEMPLATE)
    merged, records = _fold(user)
    assert merged == user
    assert records == []


# ---------------------------------------------------------------------------
# 4. idempotence and the shipped template as a fixed point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [SPLIT_STARTUP, LEGACY_ALL_SOURCE])
def test_a_second_merge_changes_nothing(fixture):
    merged, _ = _fold(copy.deepcopy(fixture))
    again, records = _fold(copy.deepcopy(merged))
    assert again == merged
    assert records == []


@pytest.mark.parametrize("flavour", ["linux", "windows"])
def test_the_shipped_template_is_a_fixed_point(flavour):
    template = json.loads(
        (REPO_ROOT / "templates" / f"settings.json.{flavour}.template")
        .read_text(encoding="utf-8"))
    merged, records = _fold(copy.deepcopy(template), template)
    assert merged == template
    assert records == []


@pytest.mark.parametrize("flavour", ["linux", "windows"])
def test_a_fresh_install_matches_the_template_exactly(flavour):
    template = json.loads(
        (REPO_ROOT / "templates" / f"settings.json.{flavour}.template")
        .read_text(encoding="utf-8"))
    records: list = []
    fresh = settings_merge.smart_merge_settings(
        {}, template, duplicate_groups=records)
    assert fresh == template
    assert records == []


def test_the_inputs_are_not_mutated():
    user = copy.deepcopy(SPLIT_STARTUP)
    snapshot = copy.deepcopy(user)
    _fold(user)
    assert user == snapshot


# ---------------------------------------------------------------------------
# 5. through the bundle engine — the summary the user actually sees
# ---------------------------------------------------------------------------


def _bundle(tmp_path):
    from tests.test_install_bundle import _make_fake_orchestrator

    from vco_lib import project_init

    orch = tmp_path / "orchestrator"
    proj = tmp_path / "project"
    orch.mkdir()
    proj.mkdir()
    _make_fake_orchestrator(orch)
    # The fake tree's template has no SessionStart block; ship this file's
    # (three-group, group-per-matcher) shape so the fold has something to do.
    for flavour in ("linux", "windows"):
        (orch / "templates" / f"settings.json.{flavour}.template").write_text(
            json.dumps(TEMPLATE, indent=2), encoding="utf-8")
    project_init.install_project_bundle(proj, orchestrator_root=orch)
    return project_init, orch, proj


def _settings(proj: Path) -> dict:
    return json.loads(
        (proj / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _split_startup(proj: Path) -> None:
    """Damage an installed project the way an old update left it: the shipped
    `startup` group's hooks split across two groups."""
    settings = _settings(proj)
    groups = settings["hooks"]["SessionStart"]
    first = next(g for g in groups if g.get("matcher") == "startup"
                 and len(g["hooks"]) > 2)
    moved = {"matcher": "startup", "hooks": first["hooks"][3:]}
    first["hooks"] = first["hooks"][:3]
    groups.insert(groups.index(first) + 1, moved)
    (proj / ".claude" / "settings.json").write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def test_a_real_bundle_update_folds_the_duplicate_groups(tmp_path):
    project_init, orch, proj = _bundle(tmp_path)
    _split_startup(proj)
    result = project_init.install_project_bundle(
        proj, orchestrator_root=orch, update_mode=True)
    assert result["errors"] == []
    startup = [g for g in _settings(proj)["hooks"]["SessionStart"]
               if g.get("matcher") == "startup"]
    assert len(startup) == 1
    assert any(r.get("action") == "folded"
               for r in result["duplicate_hook_groups"])
    assert not any("duplicate hook group" in w for w in result.get("warnings", []))


def test_a_real_bundle_update_reports_the_group_it_had_to_leave_alone(tmp_path):
    project_init, orch, proj = _bundle(tmp_path)
    _split_startup(proj)
    settings = _settings(proj)
    settings["hooks"]["SessionStart"].append({
        "matcher": "startup",
        "hooks": [{"type": "command", "command": "bash .claude/hooks/my-own-hook.sh"}],
    })
    (proj / ".claude" / "settings.json").write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    result = project_init.install_project_bundle(
        proj, orchestrator_root=orch, update_mode=True)
    assert result["errors"] == []
    kept = [r for r in result["duplicate_hook_groups"]
            if r["action"] == "kept_user_command"]
    assert kept and kept[0]["commands"] == ["bash .claude/hooks/my-own-hook.sh"]
    warnings = " ".join(result.get("warnings", []))
    assert "left 1 duplicate hook group(s) exactly as they are" in warnings
    assert "my-own-hook.sh" in warnings
    assert any(h["command"] == "bash .claude/hooks/my-own-hook.sh"
               for g in _settings(proj)["hooks"]["SessionStart"]
               for h in g["hooks"])


def test_an_undamaged_project_reports_nothing(tmp_path):
    project_init, orch, proj = _bundle(tmp_path)
    result = project_init.install_project_bundle(
        proj, orchestrator_root=orch, update_mode=True)
    assert result["errors"] == []
    assert "duplicate_hook_groups" not in result
    assert not any("duplicate hook group" in w for w in result.get("warnings", []))

def test_a_real_bundle_update_reports_a_user_setting_it_could_not_fold(tmp_path):
    """Two copies of one shipped hook with two different timeouts of the
    user's: the update folds nothing and says so in its summary."""
    project_init, orch, proj = _bundle(tmp_path)
    _split_startup(proj)
    settings = _settings(proj)
    startup = [g for g in settings["hooks"]["SessionStart"]
               if g.get("matcher") == "startup"]
    for group, timeout in zip(startup, (77, 99)):
        group["hooks"].append({"type": "command", "timeout": timeout,
                               "command": ENSURE})
    (proj / ".claude" / "settings.json").write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    result = project_init.install_project_bundle(
        proj, orchestrator_root=orch, update_mode=True)
    assert result["errors"] == []
    assert [r["action"] for r in result["duplicate_hook_groups"]] == [
        "kept_user_setting"]
    warnings = " ".join(result.get("warnings", []))
    assert "a hook setting such as a timeout" in warnings
    assert len([g for g in _settings(proj)["hooks"]["SessionStart"]
                if g.get("matcher") == "startup"]) == 2
