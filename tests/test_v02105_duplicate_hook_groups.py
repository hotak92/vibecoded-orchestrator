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

#: The other measured shape: a legacy `*`-shaped (here: absent-matcher) group
#: holding only shipped commands the shipped `startup` group already runs,
#: spelled the old brace-less way, with a timeout VCO never shipped.
LEGACY_ALL_SOURCE = {"hooks": {"SessionStart": [
    {"hooks": [{"type": "command", "timeout": 5000,
                "command": 'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/'
                           "session-start-kg-loader.sh"},
               {"type": "command", "timeout": 5000,
                "command": 'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/'
                           "context-size-check.sh"}]},
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
    assert records[0]["commands"] == [
        'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/session-start-kg-loader.sh',
        'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/context-size-check.sh']


def test_a_legacy_all_source_group_keeps_what_nothing_else_runs():
    """Trimmed, not dropped: only the commands another group already runs go.

    ``notify-stop.sh`` is a shipped hook, but no SessionStart group runs it —
    so the legacy group keeps it (a trim) instead of vanishing.
    """
    user = copy.deepcopy(LEGACY_ALL_SOURCE)
    user["hooks"]["SessionStart"][0]["hooks"].append(
        {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/'
                                       "notify-stop.sh"})
    merged, records = _fold(user)
    legacy = [g for g in merged["hooks"]["SessionStart"] if not g.get("matcher")]
    assert len(legacy) == 1
    assert [h["command"].rsplit("/", 1)[-1] for h in legacy[0]["hooks"]] == [
        "notify-stop.sh"]
    assert records[0]["action"] == "redundant_all_sources_trimmed"
    assert records[0]["commands"] == [
        'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/session-start-kg-loader.sh',
        'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/context-size-check.sh']


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