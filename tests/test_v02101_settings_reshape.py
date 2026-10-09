# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 Opus branch review B2 — the UPGRADE merge delivers reshaped
registrations (``hook_retirements.PRIOR_SHIPPED_SHAPES``).

The defect: v0.2.100 shipped ONE if-less ``pre-bash-context-inject``
handler (matcher Bash, timeout 8); v0.2.101 ships the same command as an
``if``-filtered group. The supersede pass saw the same command string and
marked the old entry "already current", the append pass then skipped every
template handler of the group, and ``timeout``/``if`` are user-wins — so
every UPGRADED install kept the if-less handler (and the Edit entry its
timeout 8) while ``test_v02101_settings_if_filters`` stayed green on the
template. These tests pin the UPGRADE PRODUCT, not the template.

The "user" side is the verbatim v0.2.100 templates
(``tests/fixtures/settings_templates_v0.2.100/``, captured with
``git show 0e69744c:templates/settings.json.{linux,windows}.template``) —
exactly what a v0.2.100 install's settings.json holds.

Both install paths reach this merge through ONE function:
``project_init._merge_settings_template_for_bundle`` →
``bundle_settings_io.merge_settings_template`` →
``settings_merge.smart_merge_settings`` (per-project bundle update; the
root install runs the SAME ``install-bundle`` CLI against the root folder,
``vco_lib.self_install``). The I/O test below drives that shim.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from vco_lib import settings_merge
from vco_lib.hook_retirements import (
    PRIOR_SHIPPED_SHAPES,
    PriorShippedShape,
    hook_command_key,
    match_retired_registration,
    migrated_matcher,
    removal_envelope_rows,
    vco_hook_script_identity,
)
from vco_lib.hooks_settings import normalize_matcher

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "settings_templates_v0.2.100"
OS_FLAVOURS = {
    "linux": ("sh", REPO_ROOT / "templates" / "settings.json.linux.template"),
    "windows": ("ps1", REPO_ROOT / "templates" / "settings.json.windows.template"),
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _v02100(os_name: str) -> dict:
    return _load(FIXTURES / f"{os_name}.json")


def _current(os_name: str) -> dict:
    return _load(OS_FLAVOURS[os_name][1])


def _handlers(doc: dict, event: str, script_stem: str) -> list[tuple[str, dict]]:
    """(matcher, inner-hook) for every handler of ``script_stem`` in ``event``."""
    out = []
    for g in doc.get("hooks", {}).get(event, []):
        for h in g.get("hooks", []):
            ident = vco_hook_script_identity(h.get("command", "")) or ""
            if ident.rsplit(".", 1)[0] == script_stem:
                out.append((normalize_matcher(g), h))
    return out


@pytest.fixture(params=sorted(OS_FLAVOURS))
def os_name(request) -> str:
    return request.param


# ── the red-proof: v0.2.100 → v0.2.101 ──────────────────────────────────────


def test_v02100_install_receives_the_if_group(os_name: str) -> None:
    template = _current(os_name)
    merged = settings_merge.smart_merge_settings(_v02100(os_name), template)

    got = _handlers(merged, "PreToolUse", "pre-bash-context-inject")
    want = _handlers(template, "PreToolUse", "pre-bash-context-inject")
    assert len(want) >= 10, "the template's if-group shrank — re-check this test"
    assert got == want, (
        "an upgraded install must carry EXACTLY the template's if-filtered "
        "pre-bash group (same matcher, same handlers, same order)")
    assert all(h.get("if") for _m, h in got), (
        "an if-less pre-bash handler survived the upgrade — it spawns the "
        "hook on EVERY Bash command (B2)")


def test_v02100_install_receives_the_raised_edit_timeout(os_name: str) -> None:
    template = _current(os_name)
    merged = settings_merge.smart_merge_settings(_v02100(os_name), template)
    got = _handlers(merged, "PreToolUse", "pre-edit-context-inject")
    want = _handlers(template, "PreToolUse", "pre-edit-context-inject")
    assert got == want
    ((_m, h),) = got
    assert h.get("timeout") == 10 and h.get("if") == "Edit(*)"


def test_every_router_surface_of_the_upgrade_equals_a_fresh_install(os_name: str) -> None:
    """Fresh and upgraded installs must behave the same on every injection
    surface — the review's consequence 2."""
    template = _current(os_name)
    merged = settings_merge.smart_merge_settings(_v02100(os_name), template)
    for event, stem in (
        ("PreToolUse", "pre-bash-context-inject"),
        ("PreToolUse", "pre-edit-context-inject"),
        ("PreToolUse", "pre-write-context-inject"),
        ("PreToolUse", "grep-context-inject"),
        ("PreToolUse", "agent-brief-kg-inject"),
        ("PostToolUse", "read-context-inject"),
    ):
        assert _handlers(merged, event, stem) == _handlers(template, event, stem), (
            f"{event}/{stem}: upgraded install differs from a fresh one")


def test_reshape_is_reported_and_idempotent(os_name: str) -> None:
    template = _current(os_name)
    removed: list = []
    merged = settings_merge.smart_merge_settings(
        _v02100(os_name), template, retired_removed=removed)
    reshapes = [r for r in removed if isinstance(r["retirement"], PriorShippedShape)]
    assert sorted(vco_hook_script_identity(r["command"]) or "" for r in reshapes) == sorted(
        f"{s}.{OS_FLAVOURS[os_name][0]}"
        for s in ("pre-bash-context-inject", "pre-edit-context-inject"))
    # The bundle envelope and audit emitters read these records too.
    rows = removal_envelope_rows(reshapes)
    assert all(r["retired_in"] == "v0.2.101" and r["replacement"] for r in rows)

    again: list = []
    assert settings_merge.smart_merge_settings(
        copy.deepcopy(merged), template, retired_removed=again) == merged
    assert again == [], "a reshaped file is a fixed point of the merge"


# ── leave-alone: anything that is not byte-equal to a shipped shape ─────────


def _set_prebash(doc: dict, **changes) -> dict:
    doc = copy.deepcopy(doc)
    for g in doc["hooks"]["PreToolUse"]:
        for h in g["hooks"]:
            if "pre-bash-context-inject" in h.get("command", ""):
                h.update(changes)
    return doc


@pytest.mark.parametrize("changes", [
    {"timeout": 20},                      # user raised the timeout
    {"if": "Bash(npm *)"},                # user added their own filter
    {"async": False},                     # any extra key makes it theirs
], ids=["timeout", "own-if", "extra-key"])
def test_user_modified_prebash_entry_is_untouched(os_name: str, changes: dict) -> None:
    user = _set_prebash(_v02100(os_name), **changes)
    before = _handlers(user, "PreToolUse", "pre-bash-context-inject")
    removed: list = []
    merged = settings_merge.smart_merge_settings(
        user, _current(os_name), retired_removed=removed)
    # v0.2.102 W5-D: the handler is preserved byte-for-byte; only its GROUP
    # matcher widens Bash -> Bash|PowerShell (MATCHER_MIGRATIONS) — the
    # matcher names which shell tool the VCO script sees, not a knob the
    # user tuned (a user-CHANGED matcher is never migrated: see
    # test_prior_shape_under_a_user_matcher_is_untouched).
    want = [(migrated_matcher("PreToolUse", m, vco_hook_script_identity(h["command"])), h)
            for m, h in before]
    assert _handlers(merged, "PreToolUse", "pre-bash-context-inject") == want, (
        "a user-modified registration must be preserved exactly (user-wins)")
    assert not any(isinstance(r["retirement"], PriorShippedShape)
                   and "pre-bash" in r["command"] for r in removed)


def test_user_modified_edit_timeout_is_untouched(os_name: str) -> None:
    user = copy.deepcopy(_v02100(os_name))
    for g in user["hooks"]["PreToolUse"]:
        for h in g["hooks"]:
            if "pre-edit-context-inject" in h["command"]:
                h["timeout"] = 30
    merged = settings_merge.smart_merge_settings(user, _current(os_name))
    ((_m, h),) = _handlers(merged, "PreToolUse", "pre-edit-context-inject")
    assert h["timeout"] == 30


def test_prior_shape_under_a_user_matcher_is_untouched(os_name: str) -> None:
    user = copy.deepcopy(_v02100(os_name))
    for g in user["hooks"]["PreToolUse"]:
        if any("pre-bash-context-inject" in h["command"] for h in g["hooks"]):
            g["matcher"] = "Bash|Monitor"
    merged = settings_merge.smart_merge_settings(user, _current(os_name))
    hs = _handlers(merged, "PreToolUse", "pre-bash-context-inject")
    assert [(m, h.get("if")) for m, h in hs] == [("Bash|Monitor", None)]


# ── v0.2.102: the if-group GREW — an already-reshaped install gains the rules ─


#: The six write/chain-side rules v0.2.102 adds to the pre-bash group
#: (S16-8 production wiring; the v0.2.101 group was the 21
#: read/search rules).
V02102_NEW_RULES = [
    "Bash(perl *)", "Bash(ruby *)", "Bash(cd *)",
    "Bash(tee *)", "Bash(cp *)", "Bash(mv *)",
]


def _v02101_shaped(os_name: str) -> dict:
    """A settings.json exactly as the v0.2.101 reshape left it: the current
    template's pre-bash group MINUS the six v0.2.102 rules — i.e. the
    21-rule v0.2.101 shape, in template order. Building it from the live
    template (not a frozen fixture) keeps the 21 pinned to whatever the
    template ships; the caller asserts the six were actually removed."""
    doc = _current(os_name)
    for g in doc["hooks"]["PreToolUse"]:
        if g.get("matcher") == "Bash|PowerShell":
            # v0.2.101 shipped the group under `Bash`, with no PowerShell twins.
            g["matcher"] = "Bash"
            g["hooks"] = [h for h in g["hooks"]
                          if h.get("if") not in V02102_NEW_RULES
                          and h.get("if", "").startswith("Bash(")]
    return doc


def test_reshaped_install_gains_the_new_rules(os_name: str) -> None:
    """An install the v0.2.101 reshape already brought to the if-group must
    GAIN exactly the new rules on the next update — no duplication of the
    21 it already carries, no touch on anything else. The bare-command
    dedupe of the append pass used to mark the whole group "handled" (every
    rule shares ONE command string), so a grown group never delivered its
    new rules to an already-reshaped file."""
    user = _v02101_shaped(os_name)
    ifs = [h.get("if") for _m, h in
           _handlers(user, "PreToolUse", "pre-bash-context-inject")]
    assert len(ifs) == 21 and not set(ifs) & set(V02102_NEW_RULES), (
        "the v0.2.101 shape synthesis is off — re-check V02102_NEW_RULES")
    merged = settings_merge.smart_merge_settings(user, _current(os_name))
    assert _handlers(merged, "PreToolUse", "pre-bash-context-inject") == \
        _handlers(_current(os_name), "PreToolUse", "pre-bash-context-inject"), (
            "an already-reshaped install must end at EXACTLY the template's "
            "group (21 kept once each + the 6 new rules)")


def test_rule_group_growth_is_idempotent(os_name: str) -> None:
    """A second update over the grown file is a fixed point: nothing is
    appended twice, nothing is removed."""
    template = _current(os_name)
    merged = settings_merge.smart_merge_settings(_v02101_shaped(os_name), template)
    removed: list = []
    again = settings_merge.smart_merge_settings(
        copy.deepcopy(merged), template, retired_removed=removed)
    assert again == merged, "a fully-current file is a fixed point of the merge"
    assert removed == []


def test_user_tuned_rule_is_not_given_a_shipped_twin(os_name: str) -> None:
    """A rule the user TUNED (here: their own timeout on one of the new
    rules) is theirs — the update must not append the shipped variant next
    to it (a twin would double-spawn that one verb). Dedupe for if-rules is
    on (command, if), not the full fingerprint."""
    user = _v02101_shaped(os_name)
    for g in user["hooks"]["PreToolUse"]:
        if g.get("matcher") not in ("Bash", "Bash|PowerShell"):
            continue
        for h in g["hooks"]:
            if h.get("if") == "Bash(awk *)":
                h["timeout"] = 30
    merged = settings_merge.smart_merge_settings(user, _current(os_name))
    ifs = [h.get("if") for _m, h in
           _handlers(merged, "PreToolUse", "pre-bash-context-inject")]
    assert ifs.count("Bash(awk *)") == 1, ifs
    tuned = [h for _m, h in _handlers(merged, "PreToolUse",
                                      "pre-bash-context-inject")
             if h.get("if") == "Bash(awk *)"]
    assert tuned[0].get("timeout") == 30, "the user's tuning must survive"
    # and the new rules still arrive beside it
    assert all(r in ifs for r in V02102_NEW_RULES)


# ── other eras, duplicates, partial states ──────────────────────────────────


def test_pre_v0297_guard_prefixed_spelling_is_reshaped_too() -> None:
    """v0.2.52–v0.2.96 shipped the guard-prefixed relative command with the
    same keys; ``command_key`` covers every shipped spelling."""
    user = _set_prebash(
        _v02100("linux"),
        command='[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/pre-bash-context-inject.sh')
    merged = settings_merge.smart_merge_settings(user, _current("linux"))
    assert _handlers(merged, "PreToolUse", "pre-bash-context-inject") == \
        _handlers(_current("linux"), "PreToolUse", "pre-bash-context-inject")


def test_pre_v0269_backslash_ps1_spelling_is_reshaped_too() -> None:
    user = _set_prebash(
        _v02100("windows"),
        command="powershell -NoProfile -ExecutionPolicy Bypass -File "
                ".claude\\hooks\\pre-bash-context-inject.ps1")
    merged = settings_merge.smart_merge_settings(user, _current("windows"))
    assert _handlers(merged, "PreToolUse", "pre-bash-context-inject") == \
        _handlers(_current("windows"), "PreToolUse", "pre-bash-context-inject")


def test_custom_command_spelling_is_not_a_shipped_shape() -> None:
    """Same keys, but a command VCO never shipped (an added flag) is the
    user's: no reshape (the pre-existing supersede pass still owns the
    command string)."""
    user = _set_prebash(
        _v02100("linux"),
        command='bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/pre-bash-context-inject.sh" --debug')
    removed: list = []
    merged = settings_merge.smart_merge_settings(
        user, _current("linux"), retired_removed=removed)
    hs = _handlers(merged, "PreToolUse", "pre-bash-context-inject")
    assert [h.get("if") for _m, h in hs] == [None]
    assert not any(isinstance(r["retirement"], PriorShippedShape)
                   and "pre-bash" in r["command"] for r in removed)


def test_duplicate_prior_shape_collapses_to_one_group() -> None:
    user = copy.deepcopy(_v02100("linux"))
    for g in user["hooks"]["PreToolUse"]:
        if any("pre-bash-context-inject" in h["command"] for h in g["hooks"]):
            g["hooks"] = g["hooks"] * 2
    records: list = []
    merged = settings_merge.smart_merge_settings(
        user, _current("linux"), retired_removed=records)
    assert _handlers(merged, "PreToolUse", "pre-bash-context-inject") == \
        _handlers(_current("linux"), "PreToolUse", "pre-bash-context-inject")
    # Re-review N-a: ONE audit record for ONE actual change — the entry that
    # delivered the group. The duplicate (whose replacement is empty because
    # every template handler is already present) must not add a second row.
    bash_rows = [r for r in records if "pre-bash-context-inject" in r["command"]]
    assert len(bash_rows) == 1, records


def test_handler_already_present_is_not_duplicated() -> None:
    """A partially-hand-migrated file (old entry + one new handler) ends with
    each template handler exactly once."""
    template = _current("linux")
    user = copy.deepcopy(_v02100("linux"))
    first_new = _handlers(template, "PreToolUse", "pre-bash-context-inject")[0][1]
    for g in user["hooks"]["PreToolUse"]:
        if any("pre-bash-context-inject" in h["command"] for h in g["hooks"]):
            g["hooks"].append(dict(first_new))
    merged = settings_merge.smart_merge_settings(user, template)
    got = [str(h.get("if")) for _m, h in _handlers(merged, "PreToolUse", "pre-bash-context-inject")]
    assert sorted(got) == sorted(
        str(h.get("if")) for _m, h in _handlers(template, "PreToolUse", "pre-bash-context-inject"))


def test_reshape_rows_are_not_retirements() -> None:
    """The scripts are LIVE: the scrub, the parked re-enable refusal and the
    launcher's prune classifier (all via ``match_retired_registration``) must
    never treat a prior-shape registration as retired."""
    for os_name in OS_FLAVOURS:
        for _m, h in _handlers(_v02100(os_name), "PreToolUse", "pre-bash-context-inject"):
            assert match_retired_registration(
                "PreToolUse", h["command"],
                hook_identity=vco_hook_script_identity(h["command"])) is None


# ── table health: every row is live and still differs from the template ────


@pytest.mark.parametrize("row", PRIOR_SHIPPED_SHAPES,
                         ids=lambda r: f"{r.event}-{r.matcher}-{r.target}")
def test_prior_shape_row_is_live(row: PriorShippedShape) -> None:
    os_name = "linux" if row.target.endswith(".sh") else "windows"
    template = _current(os_name)
    # v0.2.102 W5-D: a row whose matcher was widened (MATCHER_MIGRATIONS)
    # reshapes into the template's group under the widened matcher.
    live_matcher = migrated_matcher(row.event, row.matcher, row.target)
    shipped = [h for m, h in _handlers(template, row.event, row.target.rsplit(".", 1)[0])
               if m == live_matcher]
    assert shipped, (
        f"{row.target}: the template no longer ships it under {row.matcher!r} "
        "— the row reshapes into nothing; retire the row (or declare a "
        "RETIRED_REGISTRATIONS entry)")
    assert all(hook_command_key(h["command"]) == row.command_key for h in shipped), (
        "the row's command_key must name the template's current command")
    as_shipped = [{k: v for k, v in h.items() if k != "command"} for h in shipped]
    assert as_shipped != [dict(row.fields)], (
        "the template ships this exact shape again — the row is a no-op")


@pytest.mark.parametrize("row", PRIOR_SHIPPED_SHAPES,
                         ids=lambda r: f"{r.event}-{r.matcher}-{r.target}")
def test_prior_shape_row_matches_the_v02100_template(row: PriorShippedShape) -> None:
    """Provenance: each row is a shape VCO actually shipped (v0.2.100)."""
    os_name = "linux" if row.target.endswith(".sh") else "windows"
    hits = [h for m, h in _handlers(_v02100(os_name), row.event,
                                    row.target.rsplit(".", 1)[0])
            if row.matches(row.event, m, h)]
    assert len(hits) == 1


# ── the I/O shim both install paths share ───────────────────────────────────


def test_bundle_settings_shim_reshapes_on_disk(tmp_path: Path) -> None:
    """``project_init._merge_settings_template_for_bundle`` is the call the
    per-project bundle update and the root install (via the install-bundle
    CLI) both make; drive it against a real file."""
    from vco_lib import project_init

    target = tmp_path / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        (FIXTURES / "linux.json").read_text(encoding="utf-8"), encoding="utf-8")
    removed: list = []
    status, _redirect = project_init._merge_settings_template_for_bundle(
        OS_FLAVOURS["linux"][1], target, dry_run=False,
        retired_removed=removed, project_root=tmp_path)
    assert status == "merged"
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert _handlers(on_disk, "PreToolUse", "pre-bash-context-inject") == \
        _handlers(_current("linux"), "PreToolUse", "pre-bash-context-inject")
    assert any(isinstance(r["retirement"], PriorShippedShape) for r in removed)
