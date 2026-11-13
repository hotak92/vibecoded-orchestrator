# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""v0.2.106 — a citation is scored against the EXACT transcript recorded when
its retrieval was staged (owner ruling R7-3, 2026-10-13: "evaluations must use
the transcript of the SAME chat that produced the retrieval -> store the
transcript path at staging; score only against it").

Before: the drain looked a file's transcript up in ITS OWN transcripts folder
(``session_transcript_for`` / ``agent_transcript_for`` from the draining
chat's path). A retrieval whose chat's transcript lives elsewhere — a worktree
lane, a workflow-launched agent (``subagents/workflows/<wf>/agent-<id>.jsonl``),
a chat whose transcripts folder differs from the draining chat's — was removed
unscored by the finalize pass or the collector. And a lane's ``kg-search``
run in a worktree staged its file under the WORKTREE (or nowhere), where no
drain ever runs.

Every test drives the shipped code: ``stage_pending``, the router's env step
(and the router itself as a subprocess), ``drain_session``, the collector,
``adopt_brief_citations``. Compute calls are counted, so "exactly once" and
"never against another chat's transcript" are asserted, not assumed.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTER = REPO_ROOT / "claude_mcp_servers" / "scripts" / "hook_context_router.py"
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import citation_pending as cp  # noqa: E402

LANE_ANSWER = "the worktree lane answer text " * 40
MAIN_ANSWER = "the main chat answer text " * 40
OTHER_ANSWER = "another chat answer text " * 40
DAY = 86400.0


def _write_transcript(path: Path, text: str, *, query: str = "", age_s: float = 0.0) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if query:
        rows.append({"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z",
                     "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": "tu1",
                          "name": "mcp__weaviate-kg__hybrid_search",
                          "input": {"query": query}}]}})
    rows.append({"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))
    return str(path)


class _Machine:
    """A main checkout (the project whose drains run), a worktree lane folder
    OUTSIDE it, and a Claude-Code-shaped ``~/.claude/projects``:

    * ``folder_main`` = the main checkout's transcripts folder — the parent
      session ``sessP`` (its lanes' transcripts beside it, flat or under
      ``workflows/``);
    * ``folder_wt`` = a DIFFERENT transcripts folder (a chat whose cwd was a
      worktree) holding session ``sessW``;
    * ``folder_other`` = an unrelated chat ``sessO``.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.project = tmp_path / "repo"
        (self.project / ".claude").mkdir(parents=True)
        (self.project / ".claude" / "settings.json").write_text("{}")
        self.worktree = tmp_path / "repo.vco-lanes" / "feature-lane1"
        self.worktree.mkdir(parents=True)
        projects = tmp_path / "home" / ".claude" / "projects"
        self.folder_main = projects / "-tmp-repo"
        self.folder_wt = projects / "-tmp-repo-vco-lanes-feature-lane1"
        self.folder_other = projects / "-tmp-elsewhere"
        self.parent_tp = _write_transcript(self.folder_main / "sessP.jsonl", MAIN_ANSWER)
        self.computed: list = []

    def lane_tp(self, agent: str, *, workflow: str = "", text: str = LANE_ANSWER) -> str:
        base = self.folder_main / "sessP" / "subagents"
        if workflow:
            base = base / "workflows" / workflow
        return _write_transcript(base / f"agent-{agent}.jsonl", text)

    def compute(self, task_id, answer, ctx, write=True):
        self.computed.append((task_id, answer, ctx.get("fire_reason")))
        return {"ok": True}

    def drain(self, sid: str, tp: str, *, agent: str = "", session_end: bool = False,
              tokens: int = 5000) -> dict:
        from claude_mcp_servers.scripts import rl_drain_citations as drain

        return drain.drain_session(
            sid, tp, agent_id=agent, session_end=session_end,
            project_root=str(self.project), compute_fn=self.compute,
            token_count_fn=lambda t: tokens)

    @property
    def pdir(self) -> Path:
        return self.project / ".claude" / "state" / "rl_pending"

    def names(self) -> list:
        return sorted(p.name for p in self.pdir.iterdir()) if self.pdir.exists() else []

    def unscored(self) -> list:
        log = self.project / cp.UNSCORED_LOG_REL
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def _hook_stage(monkeypatch, m: _Machine, *, sid: str, task: str, payload_tp: str,
                agent: str = "", query: str = "q", source: str = "hook") -> Path:
    """Stage exactly as a hook run does: the router's env step from its hook
    payload, then ``stage_pending`` with no explicit transcript / root (the KG
    leg's call), from the lane's worktree cwd."""
    from claude_mcp_servers.scripts import hook_context_router as router

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(m.project))
    monkeypatch.chdir(m.worktree)
    if agent:
        monkeypatch.setenv("VCT_AGENT_ID", agent)
    else:
        monkeypatch.delenv("VCT_AGENT_ID", raising=False)
    # Registered with monkeypatch first, so the value the router step writes
    # straight into os.environ is undone after the test (no leak into the
    # tests that follow).
    monkeypatch.delenv(cp.TRANSCRIPT_ENV, raising=False)
    router._set_staging_transcript_env(payload_tp, agent)
    path = cp.stage_pending(session_id=sid, task_id=task, seq=None, query=query,
                            ctx={"nodes": []}, source=source)
    assert path is not None
    return path


# --- staging: what is recorded ------------------------------------------------


class TestStagingRecordsTheExactTranscript:
    def test_main_chat_records_its_own_transcript(self, tmp_path, monkeypatch) -> None:
        m = _Machine(tmp_path)
        path = _hook_stage(monkeypatch, m, sid="sessP", task="mainT", payload_tp=m.parent_tp)
        assert json.loads(path.read_text())[cp.TRANSCRIPT_PATH_KEY] == m.parent_tp

    def test_lane_records_its_own_transcript_not_the_parents(self, tmp_path, monkeypatch) -> None:
        """A subagent's hook payload carries the PARENT's transcript_path; the
        file records the lane's own ``agent-<id>.jsonl``."""
        m = _Machine(tmp_path)
        lane = m.lane_tp("a1b2")
        path = _hook_stage(monkeypatch, m, sid="sessP", task="laneT",
                           payload_tp=m.parent_tp, agent="a1b2")
        rec = json.loads(path.read_text())
        assert rec[cp.TRANSCRIPT_PATH_KEY] == lane
        assert rec["agent_id"] == "a1b2"
        assert path.parent == m.pdir, "staged under the main checkout, not the worktree"

    def test_workflow_lane_transcript_is_resolved(self, tmp_path) -> None:
        m = _Machine(tmp_path)
        lane = m.lane_tp("c3d4", workflow="wf_0001")
        assert cp.staging_transcript_for(m.parent_tp, "c3d4") == lane
        assert cp.agent_transcript_for(m.parent_tp, "c3d4") == lane

    def test_unknown_transcript_records_nothing(self, tmp_path, monkeypatch) -> None:
        """No agent transcript yet / a payload path that does not exist: the
        field is empty (pre-v0.2.106 routing), never the parent's file."""
        m = _Machine(tmp_path)
        path = _hook_stage(monkeypatch, m, sid="sessP", task="t1",
                           payload_tp=m.parent_tp, agent="nofile9")
        assert json.loads(path.read_text())[cp.TRANSCRIPT_PATH_KEY] == ""
        path = _hook_stage(monkeypatch, m, sid="sessP", task="t2",
                           payload_tp=str(m.folder_main / "missing.jsonl"))
        assert json.loads(path.read_text())[cp.TRANSCRIPT_PATH_KEY] == ""
        assert cp.staged_transcript(json.loads(path.read_text())) is None

    def test_router_env_step_pops_a_stale_value(self, tmp_path, monkeypatch) -> None:
        from claude_mcp_servers.scripts import hook_context_router as router

        m = _Machine(tmp_path)
        monkeypatch.setenv(cp.TRANSCRIPT_ENV, m.parent_tp)
        router._set_staging_transcript_env("", "")
        assert cp.TRANSCRIPT_ENV not in os.environ

    def test_router_subprocess_stages_the_lanes_transcript(self, tmp_path) -> None:
        """Wiring, end to end: the shipped router, fed a SUBAGENT's hook
        payload (parent's transcript_path + agent_id, cwd = the worktree),
        runs a KG leg that stages through the real ``stage_pending`` — the
        file records the lane's own transcript."""
        m = _Machine(tmp_path)
        lane = m.lane_tp("e5f6")
        stub = tmp_path / "kg_stub.py"
        stub.write_text(
            "import os, sys\n"
            "def main():\n"
            "    if '--warm-legs' in sys.argv:\n"
            "        return\n"
            "    sys.path.insert(0, os.environ['VCO_TEST_REPO'])\n"
            "    from claude_mcp_servers.rl_client.citation_pending import stage_pending\n"
            "    stage_pending(session_id=os.environ.get('VCT_SESSION_ID', ''),\n"
            "                  task_id='prompt_lane', seq=None, query='q',\n"
            "                  ctx={'nodes': [{'title': 'Stub', 'n_emb': [0.1]}]},\n"
            "                  source='hook')\n"
            "    print('KG: Stub | concept | score=0.90 | TITLES | src=knowledge/s.md')\n")
        env = child_env(
            CLAUDE_PROJECT_DIR=str(m.project), VCT_STATE_DIR=str(tmp_path / "vct"),
            WEAVIATE_URL="http://127.0.0.1:9", PYTHONPATH=str(REPO_ROOT),
            VCO_ROUTER_KG_SCRIPT=str(stub), VCO_CG_SCRIPT=str(stub),
            VCO_TEST_REPO=str(REPO_ROOT), RL_HUB_POST_DISABLED="1",
            VCO_INJECT_HARD_DEADLINE_S="60")
        for k in ("VCO_INJECT_PROFILE", "VCO_HOOK_STARTED_AT", "VCO_PROMPT_INJECT",
                  "VCT_DISABLE_HOOKS", "VCT_AGENT_ID", cp.TRANSCRIPT_ENV, "VCT_SESSION_ID"):
            env.pop(k, None)
        payload = json.dumps({
            "session_id": "sessP", "prompt_id": "p1", "transcript_path": m.parent_tp,
            "agent_id": "e5f6", "cwd": str(m.worktree),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "how does the telemetry ingestion pipeline segment laps and "
                      "smooth tyre temperatures during a live session"})
        r = subprocess.run([sys.executable, str(ROUTER), "prompt"], input=payload,
                           capture_output=True, text=True, timeout=60, env=env,
                           cwd=str(m.worktree))
        assert r.returncode == 0, r.stderr
        staged = list(m.pdir.glob("sessP__*.json"))
        assert len(staged) == 1, (m.names(), r.stdout, r.stderr)
        rec = json.loads(staged[0].read_text())
        assert rec["agent_id"] == "e5f6"
        assert rec[cp.TRANSCRIPT_PATH_KEY] == lane


# --- where a lane's CLI retrieval lands --------------------------------------


class TestLaneCliStagesUnderTheChatsProject:
    def _stage_cli(self, monkeypatch, cwd: Path) -> "Path | None":
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv(cp.TRANSCRIPT_ENV, raising=False)
        monkeypatch.chdir(cwd)
        return cp.stage_pending(session_id="", task_id="kg_search_cli_x", seq=None,
                                query="q", ctx={"nodes": []}, source="hook")

    def test_worktree_outside_the_project(self, tmp_path, monkeypatch) -> None:
        """RED before: the cwd walk found no project, nothing was staged."""
        m = _Machine(tmp_path)
        monkeypatch.setenv("KG_BASE_DIR", str(m.project))
        path = self._stage_cli(monkeypatch, m.worktree)
        assert path is not None and path.parent == m.pdir.resolve()

    def test_harness_worktree_inside_the_project(self, tmp_path, monkeypatch) -> None:
        """RED before: the worktree carries the tracked .claude/settings.json,
        so the walk staged under the WORKTREE, where no drain runs."""
        m = _Machine(tmp_path)
        wt = m.project / ".claude" / "worktrees" / "agent-x"
        (wt / ".claude").mkdir(parents=True)
        (wt / ".claude" / "settings.json").write_text("{}")
        monkeypatch.setenv("KG_BASE_DIR", str(m.project))
        path = self._stage_cli(monkeypatch, wt)
        assert path is not None and path.parent == m.pdir.resolve()
        assert not (wt / ".claude" / "state").exists()

    def test_a_home_or_non_project_kg_base_dir_is_ignored(self, tmp_path, monkeypatch) -> None:
        m = _Machine(tmp_path)
        wt = m.project / "sub"
        wt.mkdir()
        not_a_project = tmp_path / "plain"
        not_a_project.mkdir()
        monkeypatch.setenv("KG_BASE_DIR", str(not_a_project))
        path = self._stage_cli(monkeypatch, wt)
        assert path is not None and path.parent.resolve() == m.pdir.resolve(), "cwd walk"

    def test_the_chat_project_rung_never_reaches_a_collector(
            self, tmp_path, monkeypatch) -> None:
        """Staging only: a reader/collector run without CLAUDE_PROJECT_DIR
        must never resolve the KG_BASE_DIR project's queue (an ambient value
        once let a test's ``collect_abandoned(idle_after_s=0)`` remove a real
        project's pending files, 2026-11-11)."""
        m = _Machine(tmp_path)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(m.project))
        live = cp.stage_pending(session_id="s", task_id="live", seq=None, query="q",
                                ctx={"nodes": []})
        assert live is not None
        elsewhere = tmp_path / "plain"
        elsewhere.mkdir()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        monkeypatch.setenv("KG_BASE_DIR", str(m.project))
        monkeypatch.chdir(elsewhere)
        assert cp.pending_dir() is None
        assert cp.collect_abandoned(idle_after_s=0, now=10**12) == 0
        assert cp.list_pending_for_session("") == []
        assert live.exists()

    def test_claude_project_dir_still_wins(self, tmp_path, monkeypatch) -> None:
        m = _Machine(tmp_path)
        other = tmp_path / "other"
        (other / ".claude").mkdir(parents=True)
        (other / ".claude" / "settings.json").write_text("{}")
        monkeypatch.setenv("KG_BASE_DIR", str(other))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(m.project))
        path = cp.stage_pending(session_id="s", task_id="t", seq=None, query="q",
                                ctx={"nodes": []})
        assert path is not None and path.parent == m.pdir


# --- the drain: exactly that file, exactly once --------------------------------


class TestRealOrderLane:
    def test_lane_scored_once_against_its_own_transcript(self, tmp_path, monkeypatch) -> None:
        """REAL ORDER for a workflow lane in a worktree whose SubagentStop drain
        never ran: staged (parent's payload, worktree cwd) -> the parent's live
        Stop drain leaves it -> an unrelated chat's drain never touches it ->
        the parent's SessionEnd drain scores it ONCE against the lane's
        transcript -> nothing is left, nothing scores again. RED before: at
        SessionEnd the lane's transcript was looked up only at the flat
        ``subagents/agent-<id>.jsonl``, so the file stayed unscored."""
        m = _Machine(tmp_path)
        lane = m.lane_tp("f7a8", workflow="wf_lane")
        other_tp = _write_transcript(m.folder_other / "sessO.jsonl", OTHER_ANSWER)
        _hook_stage(monkeypatch, m, sid="sessP", task="laneTask",
                    payload_tp=m.parent_tp, agent="f7a8")

        m.drain("sessP", m.parent_tp)  # the parent's live Stop drain
        assert m.computed == [] and m.names() == ["sessP__laneTask.json"]
        m.drain("sessO", other_tp, session_end=True)  # another chat ends
        assert m.computed == [], "never scored against another chat's transcript"

        m.drain("sessP", m.parent_tp, session_end=True)
        assert [t for t, _, _ in m.computed] == ["laneTask"]
        assert LANE_ANSWER.strip() in m.computed[0][1], f"the answer is {lane}'s"
        assert "main chat answer" not in m.computed[0][1]
        assert "laneTask" not in " ".join(m.names())

        m.drain("sessP", m.parent_tp, session_end=True)
        m.drain("sessO", other_tp)
        assert len(m.computed) == 1, "exactly once"

    def test_subagent_stop_drain_scores_it_and_nothing_else_does(
            self, tmp_path, monkeypatch) -> None:
        m = _Machine(tmp_path)
        lane = m.lane_tp("b9c0")
        _hook_stage(monkeypatch, m, sid="sessP", task="laneT2",
                    payload_tp=m.parent_tp, agent="b9c0")
        m.drain("sessP", lane, agent="b9c0")  # SubagentStop
        assert [t for t, _, _ in m.computed] == ["laneT2"]
        assert "worktree lane answer" in m.computed[0][1]
        m.drain("sessP", m.parent_tp)
        m.drain("sessP", m.parent_tp, session_end=True)
        assert len(m.computed) == 1


class TestRecordedTranscriptInAnotherFolder:
    def test_ended_chat_in_another_folder_is_scored_by_the_finalize_pass(
            self, tmp_path, monkeypatch) -> None:
        """Session sessW's transcript lives in ANOTHER transcripts folder than
        the draining chat's. Its SessionEnd drain did not finish; the next
        drain of sessP finalizes it — against sessW's recorded transcript,
        once. RED before: the same-folder lookup found nothing and the
        finalize pass removed it as ``no_transcript``."""
        m = _Machine(tmp_path)
        tp_w = _write_transcript(m.folder_wt / "sessW.jsonl", OTHER_ANSWER)
        _hook_stage(monkeypatch, m, sid="sessW", task="wtT", payload_tp=tp_w)
        cp.mark_session_ended("sessW", m.project)
        summary = m.drain("sessP", m.parent_tp)
        assert [t for t, _, _ in m.computed] == ["wtT"]
        assert "another chat answer" in m.computed[0][1]
        assert summary["finalized"] == 1 and m.unscored() == []
        m.drain("sessP", m.parent_tp)
        assert len(m.computed) == 1

    def test_abandoned_file_is_collected_with_a_score(self, tmp_path, monkeypatch) -> None:
        m = _Machine(tmp_path)
        tp_w = _write_transcript(m.folder_wt / "sessW.jsonl", OTHER_ANSWER, age_s=8 * DAY)
        path = _hook_stage(monkeypatch, m, sid="sessW", task="oldT", payload_tp=tp_w)
        rec = json.loads(path.read_text())
        rec["ts_ms"] = int((time.time() - 8 * DAY) * 1000)
        cp.write_pending(path, rec)
        summary = m.drain("sessP", m.parent_tp)
        assert [t for t, _, _ in m.computed] == ["oldT"]
        assert summary["collected_scored"] == 1

    def test_a_gone_recorded_transcript_is_never_replaced(self, tmp_path, monkeypatch) -> None:
        """The recorded file is authoritative: when it is gone the citation is
        unscored (and the row names the path), even though a same-named
        sibling sits in the draining chat's folder."""
        m = _Machine(tmp_path)
        tp_w = _write_transcript(m.folder_wt / "sessW.jsonl", OTHER_ANSWER)
        _hook_stage(monkeypatch, m, sid="sessW", task="goneT", payload_tp=tp_w)
        os.unlink(tp_w)
        _write_transcript(m.folder_main / "sessW.jsonl", MAIN_ANSWER)  # a stand-in
        cp.mark_session_ended("sessW", m.project)
        m.drain("sessP", m.parent_tp)
        assert m.computed == []
        rows = m.unscored()
        assert [(r["task_id"], r["reason"], r["transcript_path"]) for r in rows] == [
            ("goneT", "no_transcript", tp_w)]

    def test_no_session_file_with_a_recorded_chat_is_not_claimed_by_query(
            self, tmp_path, monkeypatch) -> None:
        """A file whose chat is recorded belongs to that chat only, even when
        another chat's transcript holds the same query."""
        m = _Machine(tmp_path)
        query = "how is the deferred citation queue drained"
        tp_w = _write_transcript(m.folder_wt / "sessW.jsonl", OTHER_ANSWER, query=query)
        mine = _write_transcript(m.folder_main / "sessA.jsonl", MAIN_ANSWER, query=query)
        _hook_stage(monkeypatch, m, sid="", task="nsT", payload_tp=tp_w, query=query)
        m.drain("sessA", mine, tokens=30_000, session_end=True)
        assert m.computed == [], "not sessA's retrieval"
        m.drain("sessW", tp_w, tokens=30_000)
        assert [t for t, _, _ in m.computed] == ["nsT"]
        assert "another chat answer" in m.computed[0][1]


class TestBriefAdoption:
    def test_adopted_brief_records_the_agents_transcript(self, tmp_path, monkeypatch) -> None:
        """The brief is staged by the CALLER (its transcript recorded);
        adoption hands it to the agent, so the record must become the
        agent's transcript — or go, while that is unknown."""
        m = _Machine(tmp_path)
        monkeypatch.setenv("VCT_BRIEF_TOOL_USE_ID", "toolu_1")
        p1 = _hook_stage(monkeypatch, m, sid="sessP", task="brief1", payload_tp=m.parent_tp)
        monkeypatch.setenv("VCT_BRIEF_TOOL_USE_ID", "toolu_2")
        p2 = _hook_stage(monkeypatch, m, sid="sessP", task="brief2", payload_tp=m.parent_tp)
        monkeypatch.delenv("VCT_BRIEF_TOOL_USE_ID")
        assert json.loads(p1.read_text())[cp.TRANSCRIPT_PATH_KEY] == m.parent_tp

        lane = m.lane_tp("d1e2")
        assert cp.adopt_brief_citations("sessP", "toolu_1", "d1e2", agent_finished=True,
                                        parent_transcript=m.parent_tp,
                                        project_root=m.project) == 1
        assert json.loads(p1.read_text())[cp.TRANSCRIPT_PATH_KEY] == lane
        # A background launch whose transcript does not exist yet.
        assert cp.adopt_brief_citations("sessP", "toolu_2", "f3a4", agent_finished=False,
                                        parent_transcript=m.parent_tp,
                                        project_root=m.project) == 1
        assert json.loads(p2.read_text())[cp.TRANSCRIPT_PATH_KEY] == ""


def test_pre_v02106_files_keep_the_same_folder_rule(tmp_path) -> None:
    """Backfill: a file staged before v0.2.106 (no recorded path) resolves
    exactly as before — its session's sibling in the drain's folder, no
    other folder searched."""
    m = _Machine(tmp_path)
    m.pdir.mkdir(parents=True)
    tp_b = _write_transcript(m.folder_main / "sessB.jsonl", OTHER_ANSWER)
    _write_transcript(m.folder_wt / "sessC.jsonl", OTHER_ANSWER)
    for sid in ("sessB", "sessC"):
        cp.write_pending(m.pdir / f"{sid}__old{sid}.json",
                         {"session_id": sid, "task_id": f"old{sid}", "seq": None,
                          "query": "q", "ts_ms": int(time.time() * 1000),
                          "source": "hook", "agent_id": "", "ctx": {"nodes": []}})
        cp.mark_session_ended(sid, m.project)
    m.drain("sessP", m.parent_tp)
    assert [t for t, _, _ in m.computed] == ["oldsessB"]
    assert tp_b
    assert [(r["task_id"], r["reason"]) for r in m.unscored()] == [("oldsessC", "no_transcript")]


@pytest.mark.parametrize("bad", ["", "relative/x.jsonl", "/nope/x.jsonl", "/tmp/x.txt"])
def test_only_an_existing_absolute_jsonl_is_recorded(bad: str) -> None:
    assert cp.staging_transcript_for(bad, "") == ""
