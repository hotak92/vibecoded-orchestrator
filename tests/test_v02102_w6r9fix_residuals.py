# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R9FIX — the batched review R9 residuals.

F2: a file staged WITHOUT a session id (the MCP path) whose chat ended while
the MCP monitor held its claim. The SessionEnd drain could not see it (a
claim is not a ``*.json``), the finalize pass listed only ``<sid>__*`` files,
and every other chat's main loop drops it (its query is not in THEIR
transcript) — so it reached the 7-day collector as ``no_transcript`` although
its own chat's transcript was readable. The finalize pass now attributes such
files to the ended session whose own transcript (same folder) holds the exact
query, keeps that session's end record while one is still held by a claim,
and the collector reuses the same attribution.

F3: a hook the user disabled on one event (a parked launcher-DB row) must not
come back on an event an upgrade newly ships it on (``stop-drain-citations``
gaining ``SubagentStop`` and ``SessionEnd`` in v0.2.102) — while a hook still
running on some event keeps the event-scoped rule.

F7: an upgrade of a pre-0.2.102 ``settings.json`` gains the ``SessionEnd``
registration (the generic missing-event add, pinned for this hook).
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import citation_pending as cp  # noqa: E402
from tests.test_v02102_w6rl2_no_scoring_deadline import (  # noqa: E402
    DAY,
    MAIN_ANSWER,
    OTHER_ANSWER,
    _Rig,
    _stage,
    _write_transcript,
)
from vco_lib import parked_hooks, settings_merge  # noqa: E402
from vco_lib.parked_hooks import (  # noqa: E402
    ParkedHook,
    ParkedHooksState,
    find_parked_match,
)

QUERY = "how does the deferred citation queue work"
TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}
STEM = "stop-drain-citations"


# ── F2: a no-session file of an ENDED chat ────────────────────────────────


class _CtxRig(_Rig):
    """Records the ctx session each citation is written under, too."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.sessions: list = []

    def compute(self, task_id, answer, ctx, write=True):
        self.sessions.append(ctx.get("session_id"))
        return super().compute(task_id, answer, ctx, write)


def _ended_chat_with_query(rig: _Rig, *, age_s: float = 0.0) -> None:
    """sessA ended; its OWN transcript holds QUERY (the MCP hybrid_search)."""
    _write_transcript(rig.tdir / "sessA.jsonl", MAIN_ANSWER, query=QUERY, age_s=age_s)
    cp.mark_session_ended("sessA", rig.project)


def _other_chat(rig: _Rig, sid: str = "sessB", query: str = "") -> str:
    return _write_transcript(rig.tdir / f"{sid}.jsonl", OTHER_ANSWER, query=query)


def test_finalize_scores_an_ended_chats_nosession_file_against_its_transcript(
        tmp_path: Path) -> None:
    """ACT. RED at d25c2bf1: sessB's finalize pass listed only ``sessA__*``,
    dropped sessA's end record, and the file was left for the collector."""
    rig = _CtxRig(tmp_path)
    _ended_chat_with_query(rig)
    _stage(rig.project, "", "mcpEnd", query=QUERY, source="mcp")
    summary = rig.drain(sid="sessB", tp=_other_chat(rig), tokens=10_000)
    assert rig.tasks() == ["mcpEnd"]
    assert "main chat" in rig.computed[0][1], "scored against sessA's own answer"
    assert rig.computed[0][2] == "terminal_floor", "final floors: sessA ended"
    assert rig.sessions == ["sessA"], "written under the chat it was attributed to"
    assert summary["finalized"] == 1
    assert rig.names() == [], "file consumed, end record dropped with it"


def test_finalize_records_a_never_scorable_nosession_file_of_an_ended_chat(
        tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    _ended_chat_with_query(rig)
    _stage(rig.project, "", "mcpTiny", query=QUERY, source="mcp")
    summary = rig.drain(sid="sessB", tp=_other_chat(rig), tokens=10)
    assert rig.computed == [] and summary["finalized_unscored"] == 1
    assert [(r["task_id"], r["reason"], r["when"]) for r in rig.unscored()] == [
        ("mcpTiny", "below_floor", "session_ended")]
    assert rig.names() == []


def test_finalize_leaves_another_chats_nosession_file_alone(tmp_path: Path) -> None:
    """LEAVE-ALONE: sessA ended, but the file's query is in sessC's
    transcript (a live chat), not sessA's — sessA's finalize must not take it,
    and sessA's end record goes once it has nothing of its own."""
    rig = _Rig(tmp_path)
    _write_transcript(rig.tdir / "sessA.jsonl", MAIN_ANSWER, query="a different question")
    cp.mark_session_ended("sessA", rig.project)
    _other_chat(rig, "sessC", query=QUERY)
    _stage(rig.project, "", "mcpC", query=QUERY, source="mcp")
    summary = rig.drain(sid="sessB", tp=_other_chat(rig), tokens=10_000)
    assert rig.computed == [] and summary["finalized"] == 0
    assert summary["finalized_unscored"] == 0
    assert rig.names() == ["nosession__mcpC.json"], "untouched; sessA's record dropped"


def test_a_nosession_file_held_by_the_monitor_is_finalized_once_released(
        tmp_path: Path) -> None:
    """THE RACE. The monitor claims sessA's file; sessA's SessionEnd drain
    runs meanwhile (cannot see a claim); sessB's drain runs while it is still
    held; then the monitor gives up and hands it back. RED at d25c2bf1: the
    held file let sessB's finalize drop sessA's end record, so after the
    release no drain ever attributed the file again."""
    rig = _Rig(tmp_path)
    rig.main_tp = _write_transcript(rig.tdir / "sessA.jsonl", MAIN_ANSWER, query=QUERY)
    path = _stage(rig.project, "", "mcpHeld", query=QUERY, source="mcp")
    held = path.with_name(f"{path.name}{cp.CLAIM_MARK}{os.getpid()}")
    path.replace(held)  # the MCP monitor's claim (a live pid: never re-adopted)

    rig.drain(sid="sessA", session_end=True, tokens=10_000)  # its own SessionEnd
    assert rig.computed == [] and held.exists()
    tpB = _other_chat(rig)
    rig.drain(sid="sessB", tp=tpB, tokens=10_000)  # still held
    assert rig.computed == [] and held.exists(), "a live claim is never taken"
    assert cp.ended_sessions(rig.project) == ["sessA"], (
        "sessA's end record stays while a file of its own is held")

    cp.release_claim(held, path)  # the monitor's compute soft-failed
    summary = rig.drain(sid="sessB", tp=tpB, tokens=10_000)
    assert rig.tasks() == ["mcpHeld"], "scored ONCE, against sessA, after release"
    assert rig.computed[0][2] == "terminal_floor" and "main chat" in rig.computed[0][1]
    assert summary["finalized"] == 1
    assert rig.names() == []
    rig.drain(sid="sessB", tp=tpB, tokens=10_000)
    assert rig.tasks() == ["mcpHeld"], "never double-counted"


def test_a_nosession_file_the_monitor_scores_is_not_scored_again(tmp_path: Path) -> None:
    """The other end of the race: the monitor's write lands
    (``finish_scored``) — the drains then have nothing to score and the end
    record goes."""
    rig = _Rig(tmp_path)
    _ended_chat_with_query(rig)
    path = _stage(rig.project, "", "mcpLand", query=QUERY, source="mcp")
    claimed = cp.claim_pending(path)
    assert claimed is not None
    tpB = _other_chat(rig)
    rig.drain(sid="sessB", tp=tpB, tokens=10_000)
    assert cp.ended_sessions(rig.project) == ["sessA"]
    cp.finish_scored(claimed, path)  # the monitor's landed write
    rig.drain(sid="sessB", tp=tpB, tokens=10_000)
    assert rig.computed == [] and rig.names() == []


def test_collector_attributes_an_abandoned_nosession_file_to_its_ended_chat(
        tmp_path: Path) -> None:
    """The collector reuses the ended-session attribution before falling back
    to ``no_transcript``. A compute that keeps failing makes the finalize pass
    leave the file, so the collector reaches it in the same drain. RED at
    d25c2bf1: recorded ``no_transcript`` (the attribution was blind)."""
    rig = _Rig(tmp_path)
    _ended_chat_with_query(rig, age_s=8 * DAY)
    _stage(rig.project, "", "mcpOld", query=QUERY, source="mcp", age_ms=int(8 * DAY * 1000))
    tries: list = []

    def failing(task_id, answer, ctx, write=True):
        tries.append(answer)
        return None

    summary = rig.drain(sid="sessB", tp=_other_chat(rig), compute_fn=failing, tokens=10_000)
    assert tries and all("main chat" in a for a in tries), "only sessA's own answer"
    assert summary["collected"] == 1
    assert [(r["task_id"], r["reason"]) for r in rig.unscored()] == [
        ("mcpOld", "compute_failed")]


# ── F3: a disable carries to the events a hook newly ships on ─────────────


def _template(os_name: str) -> dict:
    return json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))


def _drain_cmd(template: dict) -> str:
    for g in template["hooks"]["Stop"]:
        for h in g["hooks"]:
            if STEM in h["command"]:
                return h["command"]
    raise AssertionError("the template ships stop-drain-citations on Stop")


def _pre_0_2_102(template: dict) -> dict:
    """A pre-0.2.102 settings.json: no SessionEnd event, and no
    stop-drain-citations under SubagentStop (both arrived in v0.2.102)."""
    user = copy.deepcopy(template)
    user["hooks"].pop("SessionEnd")
    groups = []
    for g in user["hooks"].get("SubagentStop", []):
        inner = [h for h in g["hooks"] if STEM not in h["command"]]
        if inner:
            groups.append({**g, "hooks": inner})
    if groups:
        user["hooks"]["SubagentStop"] = groups
    else:
        user["hooks"].pop("SubagentStop", None)
    return user


def _disable_on_stop(user: dict) -> dict:
    """What the launcher's disable does: the Stop entry leaves settings.json."""
    groups = []
    for g in user["hooks"]["Stop"]:
        inner = [h for h in g["hooks"] if STEM not in h["command"]]
        if inner:
            groups.append({**g, "hooks": inner})
    user["hooks"]["Stop"] = groups
    return user


def _events_running(settings: dict) -> list:
    return sorted(ev for ev, groups in settings["hooks"].items()
                  for g in groups for h in g.get("hooks", []) if STEM in h["command"])


def _readable(*rows: ParkedHook) -> ParkedHooksState:
    return ParkedHooksState(readable=True, hooks=tuple(rows), source="launcher_db")


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_upgrade_of_a_pre_0_2_102_settings_gains_the_session_end_registration(
        os_name: str) -> None:
    """F7 + the F3 leave-alone: nothing parked -> SessionEnd (and the
    SubagentStop registration) are added, exactly as the template ships them."""
    template = _template(os_name)
    user = _pre_0_2_102(template)
    assert _events_running(user) == ["Stop"]
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        user, template, parked=_readable(), kept_out=kept)
    assert _events_running(merged) == ["SessionEnd", "Stop", "SubagentStop"]
    assert merged["hooks"]["SessionEnd"] == template["hooks"]["SessionEnd"]
    assert kept == []


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_a_hook_parked_on_stop_stays_off_on_its_new_events(os_name: str) -> None:
    """F3 ACT. RED at d25c2bf1: the parked Stop row (event-scoped match) did
    not cover SessionEnd / SubagentStop, so the upgrade switched the hook the
    user turned off partly back on."""
    template = _template(os_name)
    cmd = _drain_cmd(template)
    user = _disable_on_stop(_pre_0_2_102(template))
    kept: list = []
    merged = settings_merge.smart_merge_settings(
        user, template, parked=_readable(ParkedHook("Stop", "", cmd)), kept_out=kept)
    assert _events_running(merged) == [], "off everywhere, as the user left it"
    assert "SessionEnd" not in merged["hooks"], "an event emptied by the filter is not added"
    carried = sorted((r["event"], r.get("carried_from_event")) for r in kept)
    assert carried == [("SessionEnd", "Stop"), ("Stop", None), ("SubagentStop", "Stop")]
    assert {r["reason"] for r in kept} == {"parked"}


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_a_hook_still_running_on_another_event_keeps_the_event_scoped_rule(
        os_name: str) -> None:
    """F3 LEAVE-ALONE: disabled on Stop only, still running on SubagentStop —
    the user turned off ONE event, so the new SessionEnd is added."""
    template = _template(os_name)
    cmd = _drain_cmd(template)
    user = _disable_on_stop(copy.deepcopy(template))
    user["hooks"].pop("SessionEnd")
    merged = settings_merge.smart_merge_settings(
        user, template, parked=_readable(ParkedHook("Stop", "", cmd)))
    assert _events_running(merged) == ["SessionEnd", "SubagentStop"]


def test_carry_is_script_identity_only_and_off_without_live_identities() -> None:
    """An inline command never carries; ``live_identities=None`` is the
    pre-R9 event-scoped rule; a live identity blocks the carry. (Attribute
    access, so a red run fails here and not at collection.)"""
    live_hook_identities = parked_hooks.live_hook_identities
    drain = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/stop-drain-citations.sh"'
    parked = [ParkedHook("Stop", "", drain), ParkedHook("Stop", "", "echo done")]
    assert find_parked_match(parked, "SessionEnd", "", drain, []) is None
    hit = find_parked_match(parked, "SessionEnd", "", drain, [], live_identities=set())
    assert hit is not None and hit.event == "Stop"
    assert find_parked_match(parked, "SessionEnd", "", "echo done", [],
                             live_identities=set()) is None
    live = live_hook_identities({"SubagentStop": [{"hooks": [{"command": drain}]}]})
    assert find_parked_match(parked, "SessionEnd", "", drain, [],
                             live_identities=live) is None
    assert live_hook_identities({"Stop": "malformed", "X": [None, {"hooks": 3}]}) == set()
