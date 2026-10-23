# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 review R29 — a hook registered under declared SIBLING matchers is
ONE hook for the user.

``session-start-ensure-hub`` ships under SessionStart ``startup`` AND
``resume|clear|compact`` (``hook_retirements.MATCHER_SIBLINGS``). Before this
fix the launcher Hooks tab listed two rows and a disable parked one
registration: the hook kept running on three of four session sources after the
user "disabled" it, and a re-enable of a row parked before the twin shipped
brought back only the ``startup`` half until the next update.

Contract pinned here (Python half; the launcher half is in
``launcher/src-tauri/src/commands/project_hooks_settings.rs`` tests):

* ``hooks_settings disable`` on either registration removes BOTH and parks
  them as ONE entry (``siblings``); ``enable`` restores both, byte-identically;
* ``enable`` of a row parked before the twin shipped re-creates the missing
  twin (verbatim) — the whole hook is back at once;
* ``list`` marks the added registration ``unit_of`` its sibling, so the tab
  renders one row; a hook the table does not name is untouched;
* the bundle merge keeps both registrations out for a unit parked under
  either matcher, and a user who REGROUPED the hook under a custom matcher and
  then disabled it still gets neither back (the R29 LOW on the single-matcher
  inference) — while a hook still running is not inferred parked.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import settings_merge
from vco_lib.hook_retirements import added_sibling_matchers, unit_matchers
from vco_lib.parked_hooks import ParkedHook, ParkedHooksState, find_parked_match

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "linux": REPO / "templates" / "settings.json.linux.template",
    "windows": REPO / "templates" / "settings.json.windows.template",
}
RESUME = "resume|clear|compact"
HUB = "session-start-ensure-hub"


def _template(flavour: str) -> dict:
    return json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))


def _hub_command(doc: dict) -> str:
    return next(
        h["command"] for g in doc["hooks"]["SessionStart"] for h in g["hooks"]
        if f"{HUB}." in h["command"])


def _matchers_for(doc: dict, stem: str = HUB) -> list:
    return [
        g.get("matcher", "") for g in (doc.get("hooks") or {}).get("SessionStart", [])
        for h in g.get("hooks", []) if f"{stem}." in h.get("command", "")]


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _cli(*argv: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.hooks_settings", *argv],
        capture_output=True, text=True, timeout=60, cwd=str(REPO), env=child_env())
    out = json.loads(proc.stdout)
    assert out.get("ok"), out
    return out


# ---------------------------------------------------------------------------
# the table helpers
# ---------------------------------------------------------------------------


def test_the_unit_is_declared_in_both_directions() -> None:
    target = f"{HUB}.sh"
    assert unit_matchers("SessionStart", "startup", target) == (RESUME,)
    assert unit_matchers("SessionStart", RESUME, target) == ("startup",)
    assert added_sibling_matchers("SessionStart", "startup", target) == (RESUME,)
    assert added_sibling_matchers("SessionStart", RESUME, target) == ()
    # Leave-alone: another hook under the same matchers is no unit.
    assert unit_matchers("SessionStart", "startup", "compact-context-reinject.sh") == ()


# ---------------------------------------------------------------------------
# disable / enable through the CLI every toggle uses (launcher + hub)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
@pytest.mark.parametrize("which", ["startup", RESUME])
def test_disabling_either_row_parks_the_whole_hook_and_enable_restores_it(
    flavour: str, which: str, tmp_path: Path,
) -> None:
    doc = _template(flavour)
    path = _write(tmp_path, doc)
    before = path.read_bytes()
    command = _hub_command(doc)

    out = _cli("disable", "--settings", str(path), "--event", "SessionStart",
               "--matcher", which, "--command", command)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert _matchers_for(after) == [], "the hook must not run on ANY source once disabled"
    parked = json.loads(out["parked_json"])
    assert parked["matcher"] == which
    assert [s["matcher"] for s in parked["siblings"]] == [
        "startup" if which == RESUME else RESUME]
    # Leave-alone: every other SessionStart hook is still there.
    assert _matchers_for(after, "compact-context-reinject") == ["startup", "compact"]

    _cli("enable", "--settings", str(path), "--entry-json", out["parked_json"])
    assert path.read_bytes() == before, "the round trip must be byte-identical"


def test_a_hook_with_no_declared_sibling_parks_exactly_as_before(tmp_path: Path) -> None:
    """Leave-alone: compact-context-reinject ships under two matchers too, but
    the table does not name it — each registration stays its own toggle."""
    doc = _template("linux")
    path = _write(tmp_path, doc)
    command = next(
        h["command"] for g in doc["hooks"]["SessionStart"] for h in g["hooks"]
        if "compact-context-reinject." in h["command"])
    out = _cli("disable", "--settings", str(path), "--event", "SessionStart",
               "--matcher", "startup", "--command", command)
    assert "siblings" not in json.loads(out["parked_json"])
    after = json.loads(path.read_text(encoding="utf-8"))
    assert _matchers_for(after, "compact-context-reinject") == ["compact"]
    assert sorted(_matchers_for(after)) == sorted(["startup", RESUME])


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_enabling_a_row_parked_before_the_twin_shipped_restores_the_whole_hook(
    flavour: str, tmp_path: Path,
) -> None:
    """R29 LOW: a v0.2.103 park (startup only, no ``siblings``) whose resume
    twin the update kept out — the enable re-creates the twin at once."""
    doc = _template(flavour)
    full = json.loads(json.dumps(doc))
    command = _hub_command(doc)
    doc["hooks"]["SessionStart"] = [
        g for g in doc["hooks"]["SessionStart"] if g.get("matcher") != RESUME]
    path = _write(tmp_path, doc)
    out = _cli("disable", "--settings", str(path), "--event", "SessionStart",
               "--matcher", "startup", "--command", command)
    assert "siblings" not in json.loads(out["parked_json"])
    assert _matchers_for(json.loads(path.read_text(encoding="utf-8"))) == []

    _cli("enable", "--settings", str(path), "--entry-json", out["parked_json"])
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert _matchers_for(restored) == ["startup", RESUME]
    # The twin is the shipped group, verbatim.
    twin = [g for g in restored["hooks"]["SessionStart"] if g.get("matcher") == RESUME]
    shipped = [g for g in full["hooks"]["SessionStart"] if g.get("matcher") == RESUME]
    assert twin == shipped

    # Idempotent: a second enable changes nothing.
    again = _cli("enable", "--settings", str(path), "--entry-json", out["parked_json"])
    assert again["changed"] is False


def test_a_nested_siblings_blob_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    doc = _template("linux")
    path = _write(tmp_path, doc)
    out = _cli("disable", "--settings", str(path), "--event", "SessionStart",
               "--matcher", "startup", "--command", _hub_command(doc))
    blob = json.loads(out["parked_json"])
    blob["siblings"][0]["siblings"] = []
    before = path.read_bytes()
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.hooks_settings", "enable", "--settings", str(path),
         "--entry-json", json.dumps(blob)],
        capture_output=True, text=True, timeout=60, cwd=str(REPO), env=child_env())
    assert json.loads(proc.stdout)["code"] == "parked_entry_invalid"
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# list: one row
# ---------------------------------------------------------------------------


def test_list_folds_the_added_registration_into_its_sibling_row(tmp_path: Path) -> None:
    doc = _template("linux")
    path = _write(tmp_path, doc)
    rows = _cli("list", "--settings", str(path))["hooks"]
    hub = {r["matcher"]: r["unit_of"] for r in rows if f"{HUB}." in r["command"]}
    assert hub == {"startup": None, RESUME: "startup"}
    # Leave-alone: nothing else folds.
    assert all(r["unit_of"] is None for r in rows if f"{HUB}." not in r["command"])


def test_an_added_registration_whose_sibling_is_gone_is_its_own_row(tmp_path: Path) -> None:
    doc = _template("linux")
    for g in doc["hooks"]["SessionStart"]:
        if g.get("matcher") == "startup":
            g["hooks"] = [h for h in g["hooks"] if f"{HUB}." not in h["command"]]
    path = _write(tmp_path, doc)
    rows = _cli("list", "--settings", str(path))["hooks"]
    assert [r["unit_of"] for r in rows if f"{HUB}." in r["command"]] == [None]


# ---------------------------------------------------------------------------
# the bundle merge
# ---------------------------------------------------------------------------


def _user_without_hub(flavour: str) -> tuple[dict, str]:
    doc = _template(flavour)
    command = _hub_command(doc)
    groups = []
    for g in doc["hooks"]["SessionStart"]:
        g["hooks"] = [h for h in g["hooks"] if f"{HUB}." not in h["command"]]
        if g["hooks"]:
            groups.append(g)
    doc["hooks"]["SessionStart"] = groups
    return doc, command


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
@pytest.mark.parametrize("parked_under", ["startup", RESUME])
def test_a_unit_parked_under_either_matcher_stays_out_on_update(
    flavour: str, parked_under: str,
) -> None:
    user, command = _user_without_hub(flavour)
    parked = ParkedHooksState(readable=True, hooks=(
        ParkedHook("SessionStart", parked_under, command, True),))
    merged = settings_merge.smart_merge_settings(
        user, _template(flavour), parked=parked, kept_out=[])
    assert _matchers_for(merged) == []


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_a_regrouped_then_disabled_hook_stays_off_on_update(flavour: str) -> None:
    """R29 LOW: the user moved the hook under a matcher VCO never shipped,
    then disabled it. Before the sibling table, the one-shipped-matcher
    inference kept it out; two shipped matchers must not undo that."""
    user, command = _user_without_hub(flavour)
    parked = ParkedHooksState(readable=True, hooks=(
        ParkedHook("SessionStart", "startup|resume", command, True),))
    merged = settings_merge.smart_merge_settings(
        user, _template(flavour), parked=parked, kept_out=[])
    assert _matchers_for(merged) == []


def test_the_regroup_inference_needs_proof_the_hook_runs_nowhere() -> None:
    """Leave-alone side: with the hook live (or liveness unknown), a row under
    a foreign matcher is a stale single-matcher disable, not the hook's."""
    template = _template("linux")
    command = _hub_command(template)
    groups = template["hooks"]["SessionStart"]
    parked = [ParkedHook("SessionStart", "startup|resume", command, True)]
    ident = f"{HUB}.sh"
    for matcher in ("startup", RESUME):
        assert find_parked_match(parked, "SessionStart", matcher, command, groups,
                                 live_identities=[]) is not None
        assert find_parked_match(parked, "SessionStart", matcher, command, groups,
                                 live_identities=[ident]) is None
        assert find_parked_match(parked, "SessionStart", matcher, command, groups) is None


# ─── FINAL4 NIT 3: a stale parked primary keeps no twin out ───────────────


def _user_with_startup_hub_only(flavour: str) -> tuple[dict, str]:
    """The template minus the `resume|clear|compact` hub registration: the
    user's file after they put the `startup` line back by hand while a
    v0.2.103 park of it is still recorded."""
    doc = _template(flavour)
    command = _hub_command(doc)
    groups = []
    for g in doc["hooks"]["SessionStart"]:
        if g.get("matcher") == RESUME:
            g["hooks"] = [h for h in g["hooks"] if f"{HUB}." not in h["command"]]
        if g["hooks"]:
            groups.append(g)
    doc["hooks"]["SessionStart"] = groups
    return doc, command


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_a_stale_parked_startup_row_does_not_keep_the_twin_out(flavour: str) -> None:
    """The startup registration runs again (re-added by hand), so its parked
    row is stale — the launcher view shows the line as running and covers
    nothing with it — and the update delivers the resume twin. RED before the
    fix: the sibling rule kept the twin out while the startup one ran."""
    user, command = _user_with_startup_hub_only(flavour)
    assert _matchers_for(user) == ["startup"]
    parked = ParkedHooksState(readable=True, hooks=(
        ParkedHook("SessionStart", "startup", command, True),))
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        user, _template(flavour), parked=parked, kept_out=kept)
    assert _matchers_for(merged) == ["startup", RESUME]
    assert not [k for k in kept if HUB in k.get("command", "")]


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_a_live_parked_startup_row_still_keeps_the_twin_out(flavour: str) -> None:
    """Leave-alone: the startup line is really gone (a real park), so the
    twin the update would add stays out."""
    user, command = _user_without_hub(flavour)
    parked = ParkedHooksState(readable=True, hooks=(
        ParkedHook("SessionStart", "startup", command, True),))
    merged = settings_merge.smart_merge_settings(
        user, _template(flavour), parked=parked, kept_out=[])
    assert _matchers_for(merged) == []


def test_the_sibling_rule_reads_what_is_present() -> None:
    template = _template("linux")
    command = _hub_command(template)
    groups = template["hooks"]["SessionStart"]
    parked = [ParkedHook("SessionStart", "startup", command, True)]
    ident = f"{HUB}.sh"
    present = {("SessionStart", "startup", ident)}
    assert find_parked_match(parked, "SessionStart", RESUME, command, groups,
                             live_registrations=present) is None
    assert find_parked_match(parked, "SessionStart", RESUME, command, groups,
                             live_registrations=set()) is parked[0]
    # Unknown presence keeps the sibling rule as it was.
    assert find_parked_match(parked, "SessionStart", RESUME, command, groups) is parked[0]

