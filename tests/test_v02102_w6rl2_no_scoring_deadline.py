# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""v0.2.102 W6-RL2 — owner rulings on how RL citations are scored.

R7-3, same chat only: a citation is scored against the transcript of the
chat that produced it — the same-folder session sibling, never a file found
by searching other project folders (the cross-folder fallback is gone). A
citation whose chat cannot be found stays unscored and is logged. A file
staged without a session id (the MCP path) belongs to the chat whose
transcript holds its exact query.

R7-4, no scoring deadline: the 60-minute TTL that force-scored or deleted a
still-live chat's citation is gone, and so is the file-age trigger of the
terminal floors. A pending citation waits for its OWN chat's end: the main
chat's Stop drains plus a SessionEnd drain (which records the end first, so
the next drain finalizes what it could not), a subagent's SubagentStop
drain. The one time bound left is a garbage collector for PROVABLY
abandoned files (transcript idle >= ``RL_CITATION_ABANDONED_DAYS``, no live
owner), which scores a file before removing it or logs it as unscored. The
MCP answer monitor's own lifetime is not a deadline: when it gives up the
file stays for the drains.

R7-1: a re-stage of the same task while a scorer computes yields ONE
citation event — every scorer finishes through ``finish_scored``, and a
scorer that claims a file while another claim of the same name stands
steps down.

Every test drives the shipped code (``drain_session``, the collector, the
real MCP monitor, the shipped hooks) and counts compute calls.
"""

from __future__ import annotations

import asyncio
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
TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import citation_pending as cp  # noqa: E402

MAIN_ANSWER = "the main chat answer text " * 40
AGENT_ANSWER = "the subagent answer text " * 40
OTHER_ANSWER = "another session answer text " * 40
DAY = 86400.0
HOUR_MS = 3600 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


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


def _stage(project: Path, sid: str, task: str, *, agent: str = "", age_ms: int = 0,
           query: str = "q", source: str = "hook", extra: dict | None = None) -> Path:
    path = cp.stage_pending(session_id=sid, task_id=task, seq=None, query=query,
                            ctx={"nodes": []}, source=source, project_root=project,
                            agent_id=agent)
    assert path is not None
    d = json.loads(path.read_text())
    d["ts_ms"] = _now_ms() - age_ms
    d.update(extra or {})
    path.write_text(json.dumps(d))
    return path


class _Rig:
    """A project + a Claude-Code-shaped transcripts dir:
    ``<tdir>/<session>.jsonl`` and ``<tdir>/<session>/subagents/agent-<id>.jsonl``."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.project = tmp_path / "proj"
        (self.project / ".claude" / "state").mkdir(parents=True)
        self.tdir = tmp_path / "transcripts" / "-home-u-proj"
        self.main_tp = _write_transcript(self.tdir / "sessA.jsonl", MAIN_ANSWER)
        self.computed: list = []

    def agent_tp(self, agent: str, text: str = AGENT_ANSWER, sid: str = "sessA") -> str:
        return _write_transcript(self.tdir / sid / "subagents" / f"agent-{agent}.jsonl", text)

    def compute(self, task_id, answer, ctx, write=True):
        self.computed.append((task_id, answer, ctx.get("fire_reason")))
        return {"ok": True}

    def drain(self, sid: str = "sessA", tp: "str | None" = None, *, agent: str = "",
              session_end: bool = False, compute_fn=None, tokens: int = 5000) -> dict:
        import rl_drain_citations as drain

        # ``session_end`` only when set, so a red run against the pre-W6-RL2
        # drain fails on each test's own assertion, not on the keyword.
        extra = {"session_end": True} if session_end else {}
        return drain.drain_session(
            sid, self.main_tp if tp is None else tp, agent_id=agent,
            project_root=str(self.project), compute_fn=compute_fn or self.compute,
            token_count_fn=lambda t: tokens, **extra)

    @property
    def pdir(self) -> Path:
        return self.project / ".claude" / "state" / "rl_pending"

    def names(self) -> list:
        return sorted(p.name for p in self.pdir.iterdir()) if self.pdir.exists() else []

    def tasks(self) -> list:
        return [t for t, _, _ in self.computed]

    def unscored(self) -> list:
        log = self.project / cp.UNSCORED_LOG_REL
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


# ── R7-3: same chat only ──────────────────────────────────────────────────


class TestSameChatOnly:
    def test_no_cross_folder_search(self, tmp_path: Path) -> None:
        """RED at 61ba87d0: a unique match in a sibling project folder was
        accepted as the session's transcript."""
        rig = _Rig(tmp_path)
        elsewhere = tmp_path / "transcripts" / "-home-u-proj-sub"
        _write_transcript(elsewhere / "sessB.jsonl", OTHER_ANSWER)
        assert cp.session_transcript_for(rig.main_tp, "sessB") == ""
        _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER)
        assert cp.session_transcript_for(rig.main_tp, "sessB") == str(rig.tdir / "sessB.jsonl")

    def test_ended_session_found_only_in_another_folder_is_never_scored(
            self, tmp_path: Path) -> None:
        """An abandoned file whose chat's transcript exists only in another
        project folder is logged unscored — never scored against it. RED at
        61ba87d0: the TTL sweep found it there and scored it."""
        rig = _Rig(tmp_path)
        _write_transcript(tmp_path / "transcripts" / "-home-u-other" / "sessB.jsonl",
                          OTHER_ANSWER)
        _stage(rig.project, "sessB", "farB", age_ms=int(8 * DAY * 1000))
        summary = rig.drain()
        assert rig.computed == [], "never scored against another folder's file"
        assert [(r["task_id"], r["reason"]) for r in rig.unscored()] == [
            ("farB", "no_transcript")]
        assert summary["collected"] == 1 and summary["collected_unscored"] == 1

    def test_ended_session_is_scored_against_its_own_transcript(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER)
        _stage(rig.project, "sessB", "mainB")
        cp.mark_session_ended("sessB", rig.project)
        summary = rig.drain()
        assert rig.tasks() == ["mainB"]
        assert "another session" in rig.computed[0][1], "sessB's own answer"
        assert rig.computed[0][2] == "terminal_floor", "final floors: sessB ended"
        assert summary["finalized"] == 1
        assert rig.names() == [], "file consumed, end record dropped with it"

    def test_ended_session_without_its_transcript_stays_unscored(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessGone", "lostG")
        cp.mark_session_ended("sessGone", rig.project)
        rig.drain()
        assert rig.computed == []
        assert "sessGone__lostG.json" in rig.names(), "left for the collector, not scored"


class TestNoSessionAttribution:
    QUERY = "how does the deferred citation queue work"

    def test_file_without_session_scored_by_the_chat_holding_its_query(
            self, tmp_path: Path) -> None:
        """The MCP path stages without a session id. The chat whose transcript
        holds the exact query scores it at the full gate. RED at 61ba87d0: a
        session drain never listed it (only the 60-minute sweep did)."""
        rig = _Rig(tmp_path)
        rig.main_tp = _write_transcript(rig.tdir / "sessA.jsonl", MAIN_ANSWER, query=self.QUERY)
        _stage(rig.project, "", "mcpT", query=self.QUERY, source="mcp")
        summary = rig.drain(tokens=30_000)
        assert rig.tasks() == ["mcpT"] and summary["computed"] == 1
        assert rig.names() == []

    def test_file_without_session_not_held_by_this_chat_is_untouched(
            self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        rig.main_tp = _write_transcript(rig.tdir / "sessA.jsonl", MAIN_ANSWER,
                                        query="some other question entirely")
        _stage(rig.project, "", "mcpU", query=self.QUERY, source="mcp", age_ms=3 * HOUR_MS)
        rig.drain(tokens=30_000, session_end=True)
        assert rig.computed == [], "another chat's retrieval: not scored here"
        assert "nosession__mcpU.json" in rig.names()


    def test_week_old_file_without_session_of_a_live_chat_is_not_collected(
            self, tmp_path: Path) -> None:
        """Its chat (the one holding its query) is still writing: it has a
        live owner, so the collector leaves it however old it is."""
        rig = _Rig(tmp_path)
        rig.main_tp = _write_transcript(rig.tdir / "sessA.jsonl", MAIN_ANSWER, query=self.QUERY)
        _stage(rig.project, "", "mcpW", query=self.QUERY, source="mcp",
               age_ms=int(9 * DAY * 1000))
        summary = rig.drain(tokens=10)
        assert summary["collected"] == 0 and rig.computed == []
        assert rig.names() == ["nosession__mcpW.json"]


# ── R7-4: no scoring deadline ─────────────────────────────────────────────


class TestNoDeadline:
    def test_running_background_agents_citation_is_not_force_scored(
            self, tmp_path: Path) -> None:
        """A background agent running for three hours: its brief citation is
        tagged, its transcript is being written, its SubagentStop has not
        fired. The parent's Stop drains leave it alone. RED at 61ba87d0: the
        TTL sweep scored it with the final floors and deleted it."""
        rig = _Rig(tmp_path)
        rig.agent_tp("lane1")
        _stage(rig.project, "sessA", "bgbrief", agent="lane1", age_ms=3 * HOUR_MS)
        summary = rig.drain()
        assert rig.computed == [] and summary["collected"] == 0
        assert rig.names() == ["sessA__bgbrief.json"], "neither scored nor deleted"
        # The agent's own SubagentStop drain, however late, scores it final.
        rig.drain(tp=str(rig.tdir / "sessA" / "subagents" / "agent-lane1.jsonl"),
                  agent="lane1")
        assert rig.tasks() == ["bgbrief"] and rig.computed[0][2] == "terminal_floor"
        assert "subagent" in rig.computed[0][1]
        assert rig.names() == []

    def test_stamped_running_agent_brief_waits_for_the_gate(self, tmp_path: Path) -> None:
        """Stamped while its agent may still run (agent_window_final false):
        accumulates however old. RED at 61ba87d0 (swept at the TTL)."""
        rig = _Rig(tmp_path)
        tp = rig.agent_tp("lane2")
        _stage(rig.project, "sessA", "stamped", agent="lane2", age_ms=2 * HOUR_MS,
               extra={"agent_transcript_path": tp, cp.AGENT_WINDOW_FINAL_KEY: False})
        rig.drain()
        assert rig.computed == []
        assert rig.names() == ["sessA__stamped.json"]

    def test_live_main_chat_citation_older_than_an_hour_keeps_accumulating(
            self, tmp_path: Path) -> None:
        """RED at 61ba87d0: the age trigger (30 min) and the TTL (60 min)
        applied the terminal floor to a chat still running."""
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "oldmain", age_ms=2 * HOUR_MS)
        summary = rig.drain()
        assert rig.computed == [] and summary["left"] == 1
        assert rig.names() == ["sessA__oldmain.json"]
        # The full gate still fires at any age.
        rig.drain(tokens=30_000)
        assert rig.tasks() == ["oldmain"] and rig.computed[0][2] == "stop_drain"

    def test_session_end_scores_with_final_floors_and_records_the_end(
            self, tmp_path: Path) -> None:
        """RED at 61ba87d0: no SessionEnd drain (no ``session_end``)."""
        rig = _Rig(tmp_path)
        rig.agent_tp("lane3")
        _stage(rig.project, "sessA", "endmain")
        _stage(rig.project, "sessA", "endagent", agent="lane3")
        summary = rig.drain(session_end=True)
        assert sorted(rig.tasks()) == ["endagent", "endmain"]
        assert {r for _, _, r in rig.computed} == {"terminal_floor"}
        by_task = {t: a for t, a, _ in rig.computed}
        assert "subagent" in by_task["endagent"], "agent file: the agent's transcript"
        assert "main chat" in by_task["endmain"]
        assert summary["computed"] == 2
        assert cp.ended_sessions(rig.project) == ["sessA"]
        assert rig.names() == ["sessA" + cp.SESSION_ENDED_SUFFIX]

    def test_session_end_that_cannot_score_leaves_the_files(self, tmp_path: Path) -> None:
        """A SessionEnd drain deletes nothing it did not score; the NEXT drain
        (another session) finalizes: below every floor -> removed + logged."""
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "tinyA")

        def failing(task_id, answer, ctx, write=True):
            return None  # e.g. the embedding service went down with the session

        rig.drain(session_end=True, compute_fn=failing)
        assert "sessA__tinyA.json" in rig.names(), "left, never deleted"
        # Next session: the ended session's file is finalized with final floors.
        tpB = _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER)
        summary = rig.drain(sid="sessB", tp=tpB)
        assert rig.tasks() == ["tinyA"] and rig.computed[0][2] == "terminal_floor"
        assert "main chat" in rig.computed[0][1], "scored against sessA, not sessB"
        assert summary["finalized"] == 1
        assert rig.names() == [], "end record dropped once its files are gone"

    def test_finalize_removes_and_logs_what_can_never_score(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "tinyB")
        cp.mark_session_ended("sessA", rig.project)
        tpB = _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER)
        summary = rig.drain(sid="sessB", tp=tpB, tokens=10)
        assert rig.computed == [] and summary["finalized_unscored"] == 1
        rows = rig.unscored()
        assert [(r["task_id"], r["reason"], r["when"]) for r in rows] == [
            ("tinyB", "below_floor", "session_ended")]
        assert rig.names() == []

    def test_resumed_session_drops_an_old_end_record_but_not_a_newer_one(
            self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        assert cp.mark_session_ended("sessA", rig.project)
        marker = cp.session_ended_marker("sessA", rig.project)
        old = time.time() - 60
        os.utime(marker, (old, old))
        rig.drain()  # a Stop drain: the chat is live again
        assert not marker.exists()
        cp.mark_session_ended("sessA", rig.project)
        future = time.time() + 60  # written after this drain started
        os.utime(marker, (future, future))
        rig.drain()
        assert marker.exists(), "a SessionEnd that followed this turn stands"


class TestAbandonedCollector:
    def test_idle_transcript_file_is_scored_then_removed(self, tmp_path: Path) -> None:
        """RED at 61ba87d0: no collector (``collect_abandoned``)."""
        rig = _Rig(tmp_path)
        _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER, age_s=8 * DAY)
        _stage(rig.project, "sessB", "idleB", age_ms=int(8 * DAY * 1000))
        counts: dict = {}
        assert cp.collect_abandoned(
            rig.project, source_fn=lambda p: str(rig.tdir / "sessB.jsonl"),
            score_fn=lambda c, p, s: None, counts=counts) == 1
        assert counts == {"scored": 1, "unscored": 0}
        assert rig.names() == []

    def test_drain_collects_with_final_floors_and_logs_the_unscorable(
            self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER, age_s=8 * DAY)
        _write_transcript(rig.tdir / "sessC.jsonl", OTHER_ANSWER, age_s=8 * DAY)
        _stage(rig.project, "sessB", "goodB", age_ms=int(8 * DAY * 1000))
        summary = rig.drain()
        assert rig.tasks() == ["goodB"] and rig.computed[0][2] == "terminal_floor"
        assert summary["collected_scored"] == 1
        _stage(rig.project, "sessC", "tinyC", age_ms=int(8 * DAY * 1000))
        summary = rig.drain(tokens=10)
        assert summary["collected_unscored"] == 1
        assert [(r["task_id"], r["reason"], r["when"]) for r in rig.unscored()] == [
            ("tinyC", "below_floor", "abandoned")]
        assert rig.names() == []

    def test_old_file_of_a_chat_still_writing_is_not_collected(self, tmp_path: Path) -> None:
        """Eight days old, but its transcript was written a minute ago: the
        chat lives, so it is not abandoned."""
        rig = _Rig(tmp_path)
        _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER, age_s=60)
        _stage(rig.project, "sessB", "liveB", age_ms=int(8 * DAY * 1000))
        summary = rig.drain()
        assert rig.computed == [] and summary["collected"] == 0
        assert rig.names() == ["sessB__liveB.json"]

    def test_claimed_file_and_the_running_sessions_file_are_never_collected(
            self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        held = _stage(rig.project, "sessB", "heldB", age_ms=int(30 * DAY * 1000))
        claim = held.with_name(f"{held.name}{cp.CLAIM_MARK}{os.getpid()}")
        held.replace(claim)
        own = _stage(rig.project, "sessA", "ownA", age_ms=int(30 * DAY * 1000))
        assert cp.collect_abandoned(rig.project, idle_after_s=DAY, live_session="sessA",
                                    score_fn=lambda c, p, s: None) == 0
        assert claim.exists() and own.exists()

    def test_bound_is_configurable_through_the_env(self, tmp_path: Path, monkeypatch) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessB", "twoday", age_ms=int(2 * DAY * 1000))
        assert cp.collect_abandoned(rig.project) == 0, "default bound: 7 days"
        monkeypatch.setenv(cp.ABANDONED_AFTER_ENV, "1")
        assert cp.abandoned_after_seconds() == DAY
        assert cp.collect_abandoned(rig.project) == 1
        assert [r["reason"] for r in rig.unscored()] == ["no_scorer"]


# ── R7-1: a re-stage during a compute yields one event ────────────────────


class TestRestageDuringCompute:
    def _restage_compute(self, rig: _Rig, sid: str, task: str, also=None):
        def compute(task_id, answer, ctx, write=True):
            _stage(rig.project, sid, task)  # the same task re-staged mid-compute
            if also is not None:
                also()
            return rig.compute(task_id, answer, ctx, write)
        return compute

    def test_drain(self, tmp_path: Path) -> None:
        """RED at 61ba87d0: the drain deleted its claim and the re-staged
        file was scored again by the next drain (two events)."""
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "re1")
        rig.drain(tokens=30_000, compute_fn=self._restage_compute(rig, "sessA", "re1"))
        assert rig.names() == []
        rig.drain(tokens=30_000)
        assert rig.tasks() == ["re1"], "one event"

    def test_a_drain_running_meanwhile_stands_down(self, tmp_path: Path) -> None:
        """A second drain PROCESS that lists the re-staged file while the first
        still computes claims it, sees the first one's claim and stands down;
        the first then consumes the re-stage. RED at 61ba87d0: the second
        drain scored the re-stage (two events)."""
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "re2")
        inner_log = tmp_path / "inner.json"
        script = (
            "import json, sys\n"
            f"sys.path[:0] = [{str(REPO_ROOT)!r}, "
            f"{str(REPO_ROOT / 'claude_mcp_servers' / 'scripts')!r}]\n"
            "import rl_drain_citations as d\n"
            "done = []\n"
            "def c(t, a, x, write=True):\n"
            "    done.append(t)\n"
            "    return {'ok': True}\n"
            f"d.drain_session('sessA', {rig.main_tp!r}, project_root={str(rig.project)!r},\n"
            "                compute_fn=c, token_count_fn=lambda t: 30000)\n"
            f"open({str(inner_log)!r}, 'w').write(json.dumps(done))\n")

        def other_process_drain() -> None:
            subprocess.run([sys.executable, "-c", script], check=True, env=child_env(),
                           timeout=120)

        rig.drain(tokens=30_000, compute_fn=self._restage_compute(
            rig, "sessA", "re2", also=other_process_drain))
        assert json.loads(inner_log.read_text()) == [], (
            "the drain that met the re-stage stood down")
        assert rig.tasks() == ["re2"] and rig.names() == []

    def test_finalize_and_collector(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _write_transcript(rig.tdir / "sessB.jsonl", OTHER_ANSWER)
        _stage(rig.project, "sessB", "re3")
        cp.mark_session_ended("sessB", rig.project)
        rig.drain(compute_fn=self._restage_compute(rig, "sessB", "re3"))
        _write_transcript(rig.tdir / "sessC.jsonl", OTHER_ANSWER, age_s=8 * DAY)
        _stage(rig.project, "sessC", "re4", age_ms=int(8 * DAY * 1000))
        rig.drain(compute_fn=self._restage_compute(rig, "sessC", "re4"))
        rig.drain(tokens=30_000)
        assert rig.tasks() == ["re3", "re4"], "one event each"
        assert rig.names() == []

    def test_a_score_that_does_not_land_keeps_the_newer_stage(self, tmp_path: Path) -> None:
        rig = _Rig(tmp_path)
        _stage(rig.project, "sessA", "re5")

        def restage_then_fail(task_id, answer, ctx, write=True):
            _stage(rig.project, "sessA", "re5")
            return None

        rig.drain(tokens=30_000, compute_fn=restage_then_fail)
        assert rig.names() == ["sessA__re5.json"], "no event: the re-stage survives"
        rig.drain(tokens=30_000)
        assert rig.tasks() == ["re5"]

    def test_finish_scored_unit(self, tmp_path: Path) -> None:
        d = tmp_path / ".claude" / "state" / "rl_pending"
        d.mkdir(parents=True)
        p = d / "s__t.json"
        p.write_text("{}")
        claimed = cp.claim_pending(p)
        assert claimed is not None
        p.write_text('{"restaged": 1}')
        standing = d / f"s__t.json{cp.CLAIM_MARK}999999"
        standing.write_text("{}")
        assert cp.claim_pending(p) is None, "a second claim of one name stands down"
        assert p.exists(), "and hands the file back"
        assert cp.finish_scored(claimed, p) == 2
        assert list(d.iterdir()) == []


# ── the MCP answer monitor ────────────────────────────────────────────────


def _monitor_env(monkeypatch, tmp_path: Path, *, timeout: float, flush: bool, compute) -> None:
    import claude_mcp_servers.weaviate_mcp.server as srv

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(srv, "_RL_MONITOR_TIMEOUT", timeout)
    monkeypatch.setattr(srv, "_RL_MIN_ANSWER_TOKENS_FOR_CITATION", 1)
    monkeypatch.setattr(srv, "_rl_check_force_flush", lambda: flush)
    monkeypatch.setattr(srv, "_rl_clear_force_flush", lambda: None)

    async def _transcripts():
        return [Path("fake.jsonl")]

    monkeypatch.setattr(srv, "_rl_find_all_transcripts", _transcripts)
    monkeypatch.setattr(srv, "_rl_load_messages", lambda c: [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "hybrid_search", "input": {"query": "mq"}},
            {"type": "text", "text": MAIN_ANSWER}]}}])
    monkeypatch.setattr(srv, "_rl_compute_and_write_citations", compute)
    monkeypatch.setattr(srv, "_get_rl_client", lambda: None)


def _run_monitor(staged: Path) -> None:
    import claude_mcp_servers.weaviate_mcp.server as srv
    from claude_mcp_servers.weaviate_mcp import rl_enrichment

    srv._rl_node_content_cache["mtask"] = {
        "nodes": [{"title": "n"}], "query_emb": [0.1],
        cp.CTX_PENDING_PATH_KEY: str(staged)}
    try:
        asyncio.run(rl_enrichment._rl_answer_monitor("mtask", 1, "mq"))
    finally:
        srv._rl_node_content_cache.pop("mtask", None)


def test_monitor_that_times_out_leaves_the_file_for_the_drains(tmp_path, monkeypatch) -> None:
    """The monitor's own lifetime is not a scoring deadline: a monitor that
    gives up without firing leaves the staged file, unclaimed."""
    staged = _stage(tmp_path, "", "mtask", query="mq", source="mcp")
    computed: list = []

    async def compute(task_id, answer, ctx):
        computed.append(task_id)
        return {"ok": True}

    monkeypatch.setattr("claude_mcp_servers.weaviate_mcp.server._RL_MONITOR_ANSWER_THRESHOLD_TOKENS",
                        10**9, raising=False)
    _monitor_env(monkeypatch, tmp_path, timeout=0.2, flush=False, compute=compute)
    _run_monitor(staged)
    assert computed == []
    assert sorted(p.name for p in staged.parent.iterdir()) == [staged.name]


def test_monitor_restage_during_compute_is_one_event(tmp_path, monkeypatch) -> None:
    """RED at 61ba87d0: the monitor deleted its claim only, and the re-stage
    was scored again by the next drain."""
    staged = _stage(tmp_path, "", "mtask", query="mq", source="mcp")
    computed: list = []

    async def compute(task_id, answer, ctx):
        _stage(tmp_path, "", "mtask", query="mq", source="mcp")
        computed.append(task_id)
        return {"ok": True}

    _monitor_env(monkeypatch, tmp_path, timeout=30.0, flush=True, compute=compute)
    _run_monitor(staged)
    assert computed == ["mtask"]
    assert list(staged.parent.iterdir()) == [], "the re-stage was consumed"


# ── the SessionEnd hook ───────────────────────────────────────────────────


BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_session_end_registration(os_name: str) -> None:
    """RED at 61ba87d0: no SessionEnd registration. Synchronous (an async
    hook may never run once the session exits) with a short timeout — the
    hook only spawns the detached drain."""
    doc = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    hooks = [h for g in doc["hooks"].get("SessionEnd", []) for h in g["hooks"]
             if "stop-drain-citations" in h.get("command", "")]
    assert len(hooks) == 1, hooks
    assert not hooks[0].get("async")
    assert 0 < hooks[0]["timeout"] <= 10


def _wait_for(pred, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake venv")
@pytest.mark.parametrize("flavour", ["sh", "ps1"])
def test_session_end_hook_runs_the_drain_with_session_end(tmp_path: Path, flavour: str) -> None:
    """RED at 61ba87d0: the hook ignored the event (no ``--session-end``)."""
    if flavour == "sh" and not BASH:
        pytest.skip("bash required")
    if flavour == "ps1" and not PWSH:
        pytest.skip("pwsh required")
    project = tmp_path / "proj"
    (project / ".claude" / "logs").mkdir(parents=True)
    argv_log = tmp_path / "argv.json"
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(
        "#!/bin/sh\n"
        'case "$1" in *rl_drain_citations.py)\n'
        f'  exec "{sys.executable}" -c \'import json,sys; '
        f'open("{argv_log}","w").write(json.dumps(sys.argv[1:]))\' "$@" ;;\n'
        "esac\n"
        f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "VCT_DISABLE_HOOKS"}
    env.update(CLAUDE_PROJECT_DIR=str(project), VCT_VENV=str(venv))
    script = HOOKS / f"stop-drain-citations.{flavour}"
    argv = (["bash", str(script)] if flavour == "sh"
            else ["pwsh", *PWSH_BASE_FLAGS, "-File", str(script)])

    def run(payload: dict) -> None:
        r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                           env=env, cwd=str(project), timeout=120)
        assert r.returncode == 0, r.stderr

    run({"hook_event_name": "SessionEnd", "session_id": "sessE",
         "transcript_path": "/t/sessE.jsonl", "reason": "prompt_input_exit"})
    assert _wait_for(argv_log.exists), "the drain was never spawned"
    got = json.loads(argv_log.read_text())
    assert "--session-end" in got
    assert got[got.index("--session-id") + 1] == "sessE"
    assert got[got.index("--transcript-path") + 1] == "/t/sessE.jsonl"
    argv_log.unlink()
    run({"hook_event_name": "Stop", "session_id": "sessE", "transcript_path": "/t/sessE.jsonl"})
    assert _wait_for(argv_log.exists)
    assert "--session-end" not in json.loads(argv_log.read_text()), "a Stop is not an end"
