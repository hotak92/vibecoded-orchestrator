# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R5FIX — the R5 review's alert-channel and citation-claim fixes.

* R5-1 — the deliver hooks' fast path counts an orphaned
  ``agent_alerts_*.jsonl.delivering.<pid>`` claim, so the re-adoption is
  reachable from the SHIPPED hook (sh + ps1), not only from Python.
* R5-3 — the parent Stop drain never CLAIMS an unstamped agent-tagged file
  (it would only leave it, and the claim hides it from the agent's one drain).
* R5-4 — a live holder's claim is taken over by age only when pid reuse is
  proven (process start time later than the claim's mtime).
* R5-5 — an alert file untouched for 7 days moves to
  ``.claude/logs/agent-alerts-undelivered.jsonl``; a fresh file is kept.
* R5-9 — a background brief whose agent transcript already exists is
  stamped (non-final), so a very fast agent's citation is not lost.

Every drop/move/take-over branch is tested for the act AND the leave-alone
case.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.test_v02102_w6c2_subagent_hook_census import (
    FLAVOURS,
    _dead_pid,
    _env,
    _need,
    _project,
    _run_hook,
    _stage,
    _transcript,
    _venv_env,
    _young,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))


def _aa():
    from vco_lib import agent_alerts

    return agent_alerts


def _holder() -> subprocess.Popen:
    """A live process whose pid can own a claim."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            env=child_env())


def _backdate(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


MAIN_CHAT_BASH = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s1",
                  "tool_input": {"command": "ls"}}


# ── R5-1: the hook re-adopts an orphaned delivery claim ────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_hook_readopts_dead_delivery_claim(tmp_path: Path, flavour: str) -> None:
    """Act: the ONLY file is a dead-pid `.delivering.` claim; a main-chat
    PostToolUse must start Python, re-adopt it and deliver its row."""
    _need(flavour)
    project = _project(tmp_path)
    path = _aa().append_alert(str(project), "s1", "lane1", "coder", "credential", "killed mid-way")
    assert path is not None
    claim = path.with_name(f"{path.name}.delivering.{_dead_pid()}")
    path.replace(claim)
    state = project / ".claude" / "state"
    assert [p.name for p in state.glob("agent_alerts_*")] == [claim.name]
    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT_BASH, project, env=_venv_env(project))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "the fast path hid the orphaned claim (R5-1)"
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "killed mid-way" in ctx
    assert not list(state.glob("agent_alerts_*")), "claim re-adopted and delivered"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_hook_leaves_young_live_delivery_claim(tmp_path: Path, flavour: str) -> None:
    """Leave-alone: a young claim held by a LIVE process is another delivery
    in flight — no envelope, the claim stays."""
    _need(flavour)
    project = _project(tmp_path)
    path = _aa().append_alert(str(project), "s1", "lane1", "coder", "credential", "in flight")
    assert path is not None
    holder = _holder()
    try:
        claim = path.with_name(f"{path.name}.delivering.{holder.pid}")
        path.replace(claim)
        r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT_BASH, project,
                      env=_venv_env(project))
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert claim.exists()
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_hook_claim_with_broken_venv_is_loud(tmp_path: Path, flavour: str) -> None:
    """A pending CLAIM with no resolvable venv is said out loud, like a
    pending file (the fast path no longer exits silently on it)."""
    _need(flavour)
    project = _project(tmp_path)
    path = _aa().append_alert(str(project), "s1", "lane1", "coder", "credential", "x")
    assert path is not None
    path.replace(path.with_name(f"{path.name}.delivering.{_dead_pid()}"))
    env = _env(project, VCT_VENV=str(tmp_path / "no-venv"),
               VCT_INSTALL_ROOT=str(tmp_path / "no-root"), VCO_FIND_PYTHON_PATH_ONLY="1")
    from tests.test_v02102_w6c2_subagent_hook_census import _hooks_copy_without

    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT_BASH, project, env=env,
                  hooks_dir=_hooks_copy_without(tmp_path, []))
    assert r.returncode == 0
    assert "cannot be read" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


# ── R5-4: a live holder keeps its claim unless pid reuse is proven ─────────


class TestClaimOwnerGone:
    def test_dead_pid_is_gone_at_any_age(self) -> None:
        from vco_lib.atomic import claim_owner_gone

        assert claim_owner_gone(_dead_pid(), 0.0, stale_after_s=3600) is True

    def test_live_young_claim_is_kept(self) -> None:
        from vco_lib.atomic import claim_owner_gone

        h = _holder()
        try:
            assert claim_owner_gone(h.pid, 5.0, stale_after_s=3600) is False
        finally:
            h.kill()
            h.wait()

    @pytest.mark.parametrize("started_ago,expected", [
        (3 * 3600, False),   # started BEFORE the claim: the real owner, merely slow
        (60, True),          # started AFTER the claim: the pid was reused
    ])
    def test_live_old_claim_needs_proven_reuse(self, monkeypatch, started_ago, expected) -> None:
        from vco_lib import update_lock
        from vco_lib.atomic import claim_owner_gone

        h = _holder()
        try:
            monkeypatch.setattr(update_lock, "process_start_time",
                                lambda pid: time.time() - started_ago)
            assert claim_owner_gone(h.pid, 2 * 3600, stale_after_s=3600) is expected
        finally:
            h.kill()
            h.wait()

    def test_live_old_claim_with_unknown_start_time_is_kept(self, monkeypatch) -> None:
        from vco_lib import update_lock
        from vco_lib.atomic import claim_owner_gone

        h = _holder()
        try:
            monkeypatch.setattr(update_lock, "process_start_time", lambda pid: None)
            assert claim_owner_gone(h.pid, 10 * 3600, stale_after_s=3600) is False
        finally:
            h.kill()
            h.wait()

    def test_real_start_time_proves_reuse(self) -> None:
        """No patching: a live process started seconds ago cannot own a claim
        made two hours ago -> reuse proven -> abandoned."""
        from vco_lib.atomic import claim_owner_gone

        h = _holder()
        try:
            assert claim_owner_gone(h.pid, 2 * 3600, stale_after_s=3600) is True
        finally:
            h.kill()
            h.wait()


def test_drain_keeps_a_slow_live_holders_back_dated_claim(tmp_path: Path, monkeypatch) -> None:
    """R5-4 through the drain: a live holder that has computed for over an
    hour keeps its claim (no second score); a dead-pid claim is re-adopted."""
    import rl_drain_citations as drain
    from vco_lib import update_lock

    live_file = _stage(tmp_path, "sessD", "t-slow", "lane4", ts_ms=_young())
    dead_file = _stage(tmp_path, "sessD", "t-dead", "lane4", ts_ms=_young())
    h = _holder()
    try:
        live = live_file.with_name(f"{live_file.name}.draining.{h.pid}")
        live_file.replace(live)
        _backdate(live, 2 * 3600)
        monkeypatch.setattr(update_lock, "process_start_time", lambda pid: time.time() - 3 * 3600)
        dead = dead_file.with_name(f"{dead_file.name}.draining.{_dead_pid()}")
        dead_file.replace(dead)
        agent_tp = _transcript(tmp_path / "agent.jsonl", "subagent answer " * 50)
        computed: list = []
        summary = drain.drain_session(
            "sessD", agent_tp, agent_id="lane4", project_root=str(tmp_path),
            compute_fn=lambda task_id, *a, **k: computed.append(task_id) or {"ok": True},
            token_count_fn=lambda t: 3000)
        assert summary["readopted"] == 1 and computed == ["t-dead"]
        assert live.exists(), "a slow live holder's claim is never taken over"
    finally:
        h.kill()
        h.wait()


def test_alert_delivery_keeps_a_live_back_dated_claim(tmp_path: Path, monkeypatch) -> None:
    """R5-4 through the alert channel (same rule, one home)."""
    from vco_lib import update_lock

    aa = _aa()
    root = str(tmp_path)
    p_live = aa.append_alert(root, "s1", "lane1", "coder", "credential", "live")
    p_dead = aa.append_alert(root, "s1", "lane2", "coder", "credential", "dead")
    assert p_live and p_dead
    h = _holder()
    try:
        live = p_live.with_name(f"{p_live.name}.delivering.{h.pid}")
        p_live.replace(live)
        _backdate(live, 600)
        monkeypatch.setattr(update_lock, "process_start_time", lambda pid: time.time() - 3600)
        p_dead.replace(p_dead.with_name(f"{p_dead.name}.delivering.{_dead_pid()}"))
        assert aa.readopt_stale_deliveries(root) == 1
        assert live.exists()
        assert [r["message"] for r in aa.collect(root, "s1", "lane2")] == ["dead"]
    finally:
        h.kill()
        h.wait()


# ── R5-3: the parent drain never claims an unstamped agent file ────────────


def test_parent_drain_never_claims_unstamped_agent_file(tmp_path: Path, monkeypatch) -> None:
    """Act: the unstamped agent file is left WITHOUT a claim (spy), its name
    never changes. Leave-alone side: the parent's own main-chat file and a
    STAMPED agent file are still claimed and scored."""
    import rl_drain_citations as drain
    from claude_mcp_servers.rl_client import citation_pending as cp

    unstamped = _stage(tmp_path, "sessP", "t-agent", "lane7", ts_ms=_young())
    agent_tp = _transcript(tmp_path / "agent7.jsonl", "subagent answer " * 50)
    stamped = _stage(tmp_path, "sessP", "t-stamped", "lane8", ts_ms=_young(),
                     extra={"agent_transcript_path": agent_tp})
    own = _stage(tmp_path, "sessP", "t-main", None, ts_ms=_young())
    parent_tp = _transcript(tmp_path / "parent.jsonl", "parent answer " * 50)

    real_claim = cp.claim_pending
    claimed_names: list = []
    names_during_claims: list = []

    def spy(path, *a, **k):
        claimed_names.append(Path(path).name)
        names_during_claims.append(unstamped.exists())
        return real_claim(path, *a, **k)

    monkeypatch.setattr(cp, "claim_pending", spy)
    computed: list = []
    summary = drain.drain_session(
        "sessP", parent_tp, project_root=str(tmp_path),
        compute_fn=lambda task_id, *a, **k: computed.append(task_id) or {"ok": True},
        token_count_fn=lambda t: 30000)
    assert unstamped.name not in claimed_names, "R5-3: never claim a file it will only leave"
    assert all(names_during_claims) and unstamped.exists()
    assert summary["left"] >= 1
    assert sorted(computed) == ["t-main", "t-stamped"]
    assert not own.exists() and not stamped.exists()


# ── R5-5: orphan alert files are swept after 7 days ────────────────────────


def test_orphan_alert_file_moves_to_the_undelivered_log(tmp_path: Path) -> None:
    aa = _aa()
    root = str(tmp_path)
    orphan = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "never delivered")
    nosess = aa.append_alert(root, "", "lane3", "coder", "credential", "no session row")
    fresh = aa.append_alert(root, "s2", "lane2", "coder", "credential", "other session, fresh")
    assert orphan and nosess and fresh
    _backdate(orphan, 8 * 86400)
    _backdate(nosess, 8 * 86400)
    out = aa.deliver_for_payload(root, dict(MAIN_CHAT_BASH))  # session s1: none of these
    assert out == ""
    assert not orphan.exists() and not nosess.exists(), "8-day-old orphans swept (act)"
    assert fresh.exists(), "a fresh file of another session is left (leave-alone)"
    log = tmp_path / ".claude" / "logs" / "agent-alerts-undelivered.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert sorted(r["message"] for r in rows) == ["never delivered", "no session row"]
    assert all(r["undelivered_reason"] == "orphaned_alert_file" and r["swept_at"] for r in rows)
    assert not list((tmp_path / ".claude" / "state").glob("*.delivering.*"))


def test_orphan_sweep_keeps_rows_when_the_log_cannot_be_written(tmp_path: Path) -> None:
    """Leave-alone on failure: an unwritable log never loses the rows."""
    aa = _aa()
    root = str(tmp_path)
    orphan = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "keep me")
    assert orphan
    _backdate(orphan, 8 * 86400)
    (tmp_path / ".claude" / "logs" / "agent-alerts-undelivered.jsonl").mkdir(parents=True)
    assert aa.sweep_orphan_alerts(root) == 0
    assert [r["message"] for r in aa._read_rows(orphan)] == ["keep me"]
    assert not list((tmp_path / ".claude" / "state").glob("*.delivering.*"))


def test_six_day_old_file_is_not_an_orphan(tmp_path: Path) -> None:
    aa = _aa()
    p = aa.append_alert(str(tmp_path), "sOld", "lane1", "coder", "credential", "x")
    assert p
    _backdate(p, 6 * 86400)
    assert aa.sweep_orphan_alerts(str(tmp_path)) == 0 and p.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_after_the_sweep_the_hook_fast_path_starts_no_interpreter(tmp_path: Path,
                                                                  flavour: str) -> None:
    """The point of R5-5: once the orphan is swept, the next tool call is
    back on the interpreter-free path (a bogus venv yields silence, not the
    broken-install notice)."""
    _need(flavour)
    project = _project(tmp_path)
    orphan = _aa().append_alert(str(project), "sOld", "lane1", "coder", "credential", "x")
    assert orphan
    _backdate(orphan, 8 * 86400)
    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT_BASH, project, env=_venv_env(project))
    assert r.returncode == 0 and r.stdout.strip() == "", r.stderr
    assert not orphan.exists()
    env = _env(project, VCT_VENV=str(tmp_path / "no-venv"),
               VCT_INSTALL_ROOT=str(tmp_path / "no-root"))
    t0 = time.monotonic()
    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT_BASH, project, env=env)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    print(f"agent-alerts-deliver after sweep {flavour}: {elapsed_ms:.0f} ms")


# ── R5-9: a fast background agent's brief citation is not lost ─────────────


class TestBackgroundBriefStamp:
    def _brief(self, tmp_path: Path, monkeypatch, tuid: str, task: str) -> Path:
        monkeypatch.setenv("VCT_BRIEF_TOOL_USE_ID", tuid)
        monkeypatch.delenv("VCT_AGENT_ID", raising=False)
        p = _stage(tmp_path, "sessB", task, None, ts_ms=_young())
        monkeypatch.delenv("VCT_BRIEF_TOOL_USE_ID")
        return p

    def _agent_tp(self, parent: Path, agent: str, text: str) -> Path:
        d = parent.with_suffix("") / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"agent-{agent}.jsonl"
        if text:
            _transcript(p, text)
        else:
            p.write_text("", encoding="utf-8")
        return p

    def test_existing_transcript_is_stamped_non_final(self, tmp_path: Path, monkeypatch) -> None:
        from claude_mcp_servers.rl_client.citation_pending import adopt_brief_citations
        import rl_drain_citations as drain

        brief = self._brief(tmp_path, monkeypatch, "toolu_fast", "t-fast")
        parent = tmp_path / "sess.jsonl"
        _transcript(parent, "parent answer " * 50)
        agent_tp = self._agent_tp(parent, "lane5", "fast agent answer " * 50)
        assert adopt_brief_citations("sessB", "toolu_fast", "lane5", agent_finished=False,
                                     parent_transcript=str(parent), project_root=tmp_path) == 1
        d = json.loads(brief.read_text())
        assert d["agent_id"] == "lane5" and d["agent_transcript_path"] == str(agent_tp)
        assert d["agent_window_final"] is False
        computed: list = []

        def run(tokens: int) -> dict:
            return drain.drain_session(
                "sessB", str(parent), project_root=str(tmp_path),
                compute_fn=lambda t, a, c, write=True: computed.append((t, a)) or {"ok": 1},
                token_count_fn=lambda t: tokens)
        # A short (maybe still growing) window accumulates like the main
        # chat's: NOT the final-window floors.
        run(3000)
        assert computed == [] and brief.exists()
        # Once the window clears the gate, the parent's drain scores it on
        # the SUBAGENT's answer — the citation is no longer lost to the TTL.
        run(30000)
        assert [c[0] for c in computed] == ["t-fast"] and "fast agent answer" in computed[0][1]

    def test_agent_own_drain_clears_the_flag(self, tmp_path: Path, monkeypatch) -> None:
        from claude_mcp_servers.rl_client.citation_pending import adopt_brief_citations
        import rl_drain_citations as drain

        brief = self._brief(tmp_path, monkeypatch, "toolu_bg2", "t-bg2")
        parent = tmp_path / "sess.jsonl"
        _transcript(parent, "parent answer " * 50)
        agent_tp = self._agent_tp(parent, "lane6", "partial answer " * 50)
        adopt_brief_citations("sessB", "toolu_bg2", "lane6", agent_finished=False,
                              parent_transcript=str(parent), project_root=tmp_path)
        # The agent's own drain finds a window it cannot score yet (no
        # answer after the anchor) and leaves the file — but its window is
        # final now, so the flag goes.
        drain.drain_session("sessB", str(agent_tp), agent_id="lane6", project_root=str(tmp_path),
                            compute_fn=lambda *a, **k: None, token_count_fn=lambda t: 0)
        d = json.loads(brief.read_text())
        assert "agent_window_final" not in d and d["agent_transcript_path"] == str(agent_tp)

    @pytest.mark.parametrize("make", ["missing", "empty"])
    def test_no_or_empty_transcript_tags_only(self, tmp_path: Path, monkeypatch, make) -> None:
        """Leave-alone: nothing to point at -> tag only (as before)."""
        from claude_mcp_servers.rl_client.citation_pending import adopt_brief_citations

        brief = self._brief(tmp_path, monkeypatch, "toolu_slow", "t-slow")
        parent = tmp_path / "sess.jsonl"
        _transcript(parent, "parent answer " * 50)
        if make == "empty":
            self._agent_tp(parent, "lane9", "")
        assert adopt_brief_citations("sessB", "toolu_slow", "lane9", agent_finished=False,
                                     parent_transcript=str(parent), project_root=tmp_path) == 1
        d = json.loads(brief.read_text())
        assert d["agent_id"] == "lane9"
        assert "agent_transcript_path" not in d and "agent_window_final" not in d
