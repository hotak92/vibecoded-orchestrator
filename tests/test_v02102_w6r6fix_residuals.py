# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""v0.2.102 W6-R6FIX — the three residuals of batch review R6.

R6-2 (LOW): a pending citation that reached its TTL was DELETED by the
sweep without a score. That hit agent-tagged background brief citations
whose parent got no further Stop, and main-chat citations of a session
that ended. The sweep now CLAIMS each expiring file (the one claim the
drains and the MCP monitor take), scores it through the drain's one
scoring function when its transcript is readable, and only then removes
it; a file with no readable transcript is removed and RECORDED as an
unscored expiry. These tests drive the REAL ``drain_session`` (whose TTL
sweep is the only caller of ``sweep_expired`` in shipped code) and count
compute calls:

* act — an expired agent file with a stamped transcript is scored once,
  against the AGENT's answer, then removed;
* act — an expired main-chat file is scored once, against its own
  session's transcript (this drain's, or a sibling session's);
* leave-alone — an expired file with no transcript is removed, counted
  and logged, with no score; a too-short window likewise;
* race — a file a live drain holds is never touched by the sweep, and a
  drain / the MCP monitor running DURING the sweep's compute cannot score
  the same file again.

R6-1 (INFO): ``sweep_orphan_alerts`` also sweeps a ``.delivering.`` claim
older than ``ORPHAN_AFTER_SECONDS`` (act / leave-alone / log-unwritable).

R6-3 (INFO): ``pre-bash-context-inject.ps1`` says on stderr that
``_lib/utf8-nobom.ps1`` is missing (a broken install) instead of skipping
the pairing write in silence; the hook still exits 0.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import citation_pending as cp  # noqa: E402

PARENT_ANSWER = "the parent session answer text " * 40
AGENT_ANSWER = "the subagent final answer text " * 40
OTHER_ANSWER = "another session answer text " * 40


def _expired_ms() -> int:
    """Two hours old: past the 60-min TTL."""
    return int(time.time() * 1000) - 2 * 3600 * 1000


def _write_transcript(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _stage(project: Path, sid: str, task: str, *, agent: str = "",
           ts_ms: int | None = None, extra: dict | None = None) -> Path:
    path = cp.stage_pending(session_id=sid, task_id=task, seq=None, query="q",
                            ctx={"nodes": []}, source="hook", project_root=project,
                            agent_id=agent)
    assert path is not None
    d = json.loads(path.read_text())
    d["ts_ms"] = _expired_ms() if ts_ms is None else ts_ms
    d.update(extra or {})
    path.write_text(json.dumps(d))
    return path


class _Rig:
    """A project + a Claude-Code-shaped transcripts dir:
    ``<tdir>/<session>.jsonl`` and ``<tdir>/<session>/subagents/agent-<id>.jsonl``."""

    def __init__(self, tmp_path: Path) -> None:
        self.project = tmp_path / "proj"
        (self.project / ".claude" / "state").mkdir(parents=True)
        self.tdir = tmp_path / "transcripts"
        self.parent_tp = _write_transcript(self.tdir / "sessA.jsonl", PARENT_ANSWER)
        self.computed: list = []

    def agent_tp(self, agent: str, text: str = AGENT_ANSWER, sid: str = "sessA") -> str:
        return _write_transcript(self.tdir / sid / "subagents" / f"agent-{agent}.jsonl", text)

    def compute(self, task_id, answer, ctx, write=True):
        self.computed.append((task_id, answer, ctx.get("fire_reason")))
        return {"ok": True}

    def drain(self, sid: str = "sessA", tp: "str | None" = None, *, agent: str = "",
              compute_fn=None, tokens: int = 5000) -> dict:
        import rl_drain_citations as drain

        return drain.drain_session(
            sid, self.parent_tp if tp is None else tp, agent_id=agent,
            project_root=str(self.project), compute_fn=compute_fn or self.compute,
            token_count_fn=lambda t: tokens)

    def pending_names(self) -> list:
        return sorted(p.name for p in (self.project / ".claude" / "state" / "rl_pending").iterdir())

    def unscored_rows(self) -> list:
        log = self.project / cp.UNSCORED_LOG_REL
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


# ── R6-2 act: expired files are scored, once, against the right answer ─────


class TestTtlSweepScores:
    def test_expired_stamped_agent_file_scored_once_then_removed(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        agent_tp = rig.agent_tp("lane1")
        _stage(rig.project, "sessA", "agentbrief", agent="lane1",
               extra={"agent_transcript_path": agent_tp, cp.AGENT_WINDOW_FINAL_KEY: False})
        summary = rig.drain()
        assert [t for t, _, _ in rig.computed] == ["agentbrief"], rig.computed
        assert "subagent" in rig.computed[0][1] and "parent" not in rig.computed[0][1], (
            "scored against the AGENT's stamped transcript, never the parent's")
        assert rig.computed[0][2] == "terminal_floor"
        assert summary["swept"] == 1 and summary["swept_scored"] == 1
        assert summary["swept_unscored"] == 0
        assert rig.pending_names() == [], "removed after scoring, no claim left behind"
        assert rig.unscored_rows() == []
        rig.drain()
        assert len(rig.computed) == 1, "scored exactly once"

    def test_expired_unstamped_agent_file_uses_the_transcript_beside_its_session(
            self, tmp_path: Path) -> None:
        """The background-brief case: tagged at launch, its agent's drain and
        every later parent Stop missed — the agent's transcript sits beside
        the session's, where :func:`agent_transcript_for` finds it."""
        rig = _Rig(tmp_path)
        rig.agent_tp("lane2")
        _stage(rig.project, "sessA", "bgbrief", agent="lane2")
        summary = rig.drain()
        assert [t for t, _, _ in rig.computed] == ["bgbrief"]
        assert "subagent" in rig.computed[0][1]
        assert summary["swept_scored"] == 1 and rig.pending_names() == []

    def test_expired_main_chat_file_scored_once_against_its_session(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "mainA")
        summary = rig.drain()
        assert [t for t, _, _ in rig.computed] == ["mainA"]
        assert "parent session" in rig.computed[0][1]
        assert summary["swept_scored"] == 1 and rig.pending_names() == []
        rig.drain()
        assert len(rig.computed) == 1, "scored exactly once"

    def test_expired_file_of_another_session_uses_that_sessions_transcript(
            self, tmp_path: Path) -> None:
        """The ended-session case: sessB got no further Stop; sessA's drain
        sweeps it and scores it against sessB's OWN transcript."""
        rig = _Rig(tmp_path)
        _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER)
        _stage(rig.project, "sessB", "mainB")
        rig.drain()
        assert [t for t, _, _ in rig.computed] == ["mainB"]
        assert "another session" in rig.computed[0][1]
        assert rig.pending_names() == []

    def test_agent_drain_sweep_finds_the_parent_session_transcript(self, tmp_path: Path) -> None:
        """A SubagentStop drain knows only the agent transcript; the session's
        file is resolved from it (``<tdir>/<sess>/subagents/..`` → ``<tdir>/<sess>.jsonl``)."""
        rig = _Rig(tmp_path)
        agent_tp = rig.agent_tp("lane3")
        _stage(rig.project, "sessA", "mainA2")
        rig.drain(tp=agent_tp, agent="lane3")
        assert [t for t, _, _ in rig.computed] == ["mainA2"]
        assert "parent session" in rig.computed[0][1]


# ── R6-2 leave-alone: no transcript / below floor → removed, counted, unscored ──


class TestTtlSweepUnscored:
    def test_no_transcript_is_removed_counted_and_logged(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessGone", "lost1")  # no sessGone.jsonl anywhere
        summary = rig.drain()
        assert rig.computed == [], "no transcript → no score"
        assert summary["swept"] == 1 and summary["swept_unscored"] == 1
        assert summary["swept_scored"] == 0
        assert rig.pending_names() == [], "still removed (today's behaviour kept)"
        rows = rig.unscored_rows()
        assert [(r["task_id"], r["reason"], r["session_id"]) for r in rows] == [
            ("lost1", "no_transcript", "sessGone")]
        assert rows[0]["expired_at"]

    def test_agent_file_without_any_transcript_is_unscored(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "lostagent", agent="laneX")  # no agent transcript
        summary = rig.drain()
        assert rig.computed == []
        assert summary["swept_unscored"] == 1
        assert [r["reason"] for r in rig.unscored_rows()] == ["no_transcript"]

    def test_window_below_every_floor_is_unscored(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "tiny")
        summary = rig.drain(tokens=10)
        assert rig.computed == [] and summary["swept_unscored"] == 1
        assert [r["reason"] for r in rig.unscored_rows()] == ["below_floor"]

    def test_young_file_is_not_swept(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessOther", "young", ts_ms=int(time.time() * 1000) - 60_000)
        summary = rig.drain()
        assert summary["swept"] == 0 and rig.unscored_rows() == []
        assert rig.pending_names() == ["sessOther__young.json"]

    def test_sweep_without_a_scorer_records_every_expiry(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "plain")
        counts: dict = {}
        assert cp.sweep_expired(rig.project, counts=counts) == 1
        assert counts == {"scored": 0, "unscored": 1}
        assert [r["reason"] for r in rig.unscored_rows()] == ["no_scorer"]


# ── R6-2 race: the claim keeps the sweep, the drains and the monitor apart ──


def _holder() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            env=child_env())


class TestTtlSweepClaim:
    def test_file_held_by_a_live_drain_is_not_touched(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        path = _stage(rig.project, "sessA", "held")
        h = _holder()
        try:
            claim = path.with_name(f"{path.name}{cp.CLAIM_MARK}{h.pid}")
            path.replace(claim)
            summary = rig.drain()
            assert rig.computed == [], "the sweep never scores a file a live drain holds"
            assert claim.exists(), "nor removes or renames it"
            assert summary["swept"] == 0 and rig.unscored_rows() == []
        finally:
            h.kill()
            h.wait()

    def test_drain_and_monitor_during_the_sweeps_compute_score_nothing(
            self, tmp_path: Path) -> None:
        """The sweep holds the claim while it computes: a drain run then
        writes nothing, and the MCP monitor's claim reports ``taken``."""
        rig = _Rig(tmp_path)
        path = _stage(rig.project, "sessA", "racy")
        inner: list = []
        monitor_state: list = []

        def inner_compute(task_id, answer, ctx, write=True):
            inner.append(task_id)
            return {"ok": True}

        def outer_compute(task_id, answer, ctx, write=True):
            rig.drain(compute_fn=inner_compute)
            monitor_state.append(cp.claim_task_pending(task_id, staged_path=path)[0])
            return rig.compute(task_id, answer, ctx, write)

        rig.drain(compute_fn=outer_compute)
        assert [t for t, _, _ in rig.computed] == ["racy"], "the sweep scored it once"
        assert inner == [], "a drain during the sweep's compute scored nothing"
        assert monitor_state == ["taken"], "the monitor saw the file as owned"
        assert rig.pending_names() == []


def test_session_transcript_for_refuses_guesses(tmp_path: Path) -> None:
    tdir = tmp_path / "t"
    parent = _write_transcript(tdir / "s1.jsonl", "x")
    agent = _write_transcript(tdir / "s1" / "subagents" / "agent-a.jsonl", "y")
    assert cp.session_transcript_for(parent, "") == parent
    assert cp.session_transcript_for(agent, "") == parent
    assert cp.session_transcript_for(agent, "s1") == parent
    assert cp.session_transcript_for(parent, "s2") == "", "no such file"
    assert cp.session_transcript_for(parent, "../s1") == "", "not filesystem-safe"
    assert cp.session_transcript_for("", "s1") == ""
    assert cp.session_transcript_for(str(tdir / "x.txt"), "s1") == ""


# ── R6-1: an abandoned delivery claim is swept after 7 days ────────────────


def _backdate(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


class TestOrphanDeliveryClaim:
    def _claim(self, root: str, holder_pid: int, age_s: float) -> Path:
        from vco_lib import agent_alerts as aa

        p = aa.append_alert(root, "sOld", "lane1", "coder", "credential", "stuck in a claim")
        assert p
        claim = p.with_name(f"{p.name}{aa.DELIVERING_MARK}{holder_pid}")
        p.replace(claim)
        _backdate(claim, age_s)
        return claim

    def test_eight_day_old_live_claim_moves_to_the_undelivered_log(
            self, tmp_path: Path, monkeypatch) -> None:
        """The R6-1 case end-to-end: pid alive, start time unknown, so the
        re-adoption keeps it; the orphan sweep (run by every delivery) logs
        its rows and removes it."""
        from vco_lib import agent_alerts as aa
        from vco_lib import update_lock

        monkeypatch.setattr(update_lock, "process_start_time", lambda pid: None)
        root = str(tmp_path)
        h = _holder()
        try:
            claim = self._claim(root, h.pid, 8 * 86400)
            assert aa.readopt_stale_deliveries(root) == 0 and claim.exists(), (
                "precondition: the re-adoption cannot take it")
            assert aa.deliver_for_payload(root, {"hook_event_name": "PostToolUse",
                                                 "tool_name": "Bash", "session_id": "s1",
                                                 "tool_input": {"command": "ls"}}) == ""
            assert not claim.exists()
            log = tmp_path / aa.UNDELIVERED_LOG_REL
            rows = [json.loads(ln) for ln in log.read_text().splitlines()]
            assert [(r["message"], r["undelivered_reason"]) for r in rows] == [
                ("stuck in a claim", "orphaned_delivery_claim")]
            assert rows[0]["swept_at"]
            assert not list((tmp_path / ".claude" / "state").glob("agent_alerts_*"))
        finally:
            h.kill()
            h.wait()

    def test_six_day_old_claim_is_left(self, tmp_path: Path, monkeypatch) -> None:
        from vco_lib import agent_alerts as aa
        from vco_lib import update_lock

        monkeypatch.setattr(update_lock, "process_start_time", lambda pid: None)
        h = _holder()
        try:
            claim = self._claim(str(tmp_path), h.pid, 6 * 86400)
            assert aa.sweep_orphan_alerts(str(tmp_path)) == 0
            assert claim.exists()
            assert not (tmp_path / aa.UNDELIVERED_LOG_REL).exists()
        finally:
            h.kill()
            h.wait()

    def test_unwritable_log_keeps_the_claim(self, tmp_path: Path) -> None:
        from vco_lib import agent_alerts as aa

        h = _holder()
        try:
            claim = self._claim(str(tmp_path), h.pid, 8 * 86400)
            (tmp_path / aa.UNDELIVERED_LOG_REL).mkdir(parents=True)
            assert aa.sweep_orphan_alerts(str(tmp_path)) == 0
            assert [r["message"] for r in aa._read_rows(claim)] == ["stuck in a claim"]
        finally:
            h.kill()
            h.wait()


# ── R6-3: a missing _lib/utf8-nobom.ps1 is reported, not silent ────────────


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
class TestPreBashPs1MissingUtf8Helper:
    NOTICE = "[VCO broken install] .claude/hooks/_lib/utf8-nobom.ps1 is missing"

    def _run(self, tmp_path: Path, hooks_dir: Path):
        from tests.test_v02102_rl_w3r_pairing_and_suppression import PairingRig
        from tests.test_v02101_router_surfaces import _bash_payload

        rig = PairingRig(tmp_path)
        payload = _bash_payload("grep -rn vco_query_cache_put templates/",
                                session="sess-r63", cwd=str(rig.proj))
        payload["tool_use_id"] = "toolu_r63"
        r = subprocess.run(
            ["pwsh", *PWSH_BASE_FLAGS, "-File", str(hooks_dir / "pre-bash-context-inject.ps1")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=120,
            cwd=str(rig.proj), env=rig.env())
        return rig, r

    def test_missing_helper_is_one_loud_stderr_line_and_exit_0(self, tmp_path: Path) -> None:
        hooks = tmp_path / "hooks"
        shutil.copytree(HOOKS, hooks, ignore=shutil.ignore_patterns("utf8-nobom.ps1"))
        assert not (hooks / "_lib" / "utf8-nobom.ps1").exists()
        rig, r = self._run(tmp_path, hooks)
        assert r.returncode == 0, r.stderr
        assert r.stderr.count(self.NOTICE) == 1, r.stderr[-800:]
        assert rig.bash_task_files() == [], "no pairing state could be written"

    def test_healthy_install_says_nothing_and_writes_the_state(self, tmp_path: Path) -> None:
        rig, r = self._run(tmp_path, HOOKS)
        assert r.returncode == 0, r.stderr
        assert self.NOTICE not in r.stderr
        assert len(rig.bash_task_files()) == 1



def test_session_transcript_found_in_another_project_folder(tmp_path):
    """A session started from another working directory lives in a sibling
    project folder: found when exactly one file matches, never guessed."""
    from claude_mcp_servers.rl_client.citation_pending import session_transcript_for

    root = tmp_path / "projects"
    here = root / "-home-u-proj"
    there = root / "-home-u-proj-sub"
    here.mkdir(parents=True)
    there.mkdir(parents=True)
    known = here / "11111111-1111-1111-1111-111111111111.jsonl"
    known.write_text("{}\n", encoding="utf-8")
    other_sid = "22222222-2222-2222-2222-222222222222"
    (there / f"{other_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    assert session_transcript_for(str(known), other_sid) == str(there / f"{other_sid}.jsonl")
    # Leave-alone: two candidates (ambiguous) -> unknown, not a guess.
    third = root / "-home-u-other"
    third.mkdir()
    (third / f"{other_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    assert session_transcript_for(str(known), other_sid) == ""
    # Leave-alone: nothing anywhere -> unknown.
    assert session_transcript_for(str(known), "33333333-3333-3333-3333-333333333333") == ""
