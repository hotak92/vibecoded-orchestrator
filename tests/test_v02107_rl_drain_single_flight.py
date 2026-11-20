# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 RL-DRAIN (A-024, A-032) — when the citation drain runs and how
much it reads. What it computes is unchanged.

1. The Stop hook spawns no drain while the session has no pending file (its
   own, or one staged without a session id); the rule is
   ``citation_pending.session_may_drain`` and both hook siblings match it.
2. A drain runs single-flight per transcript: a second request while one runs
   leaves a rerun marker and exits; the holder runs once more (N requests
   during one run coalesce into ONE rerun; an end request makes it a
   SessionEnd run).
3. A drain reads the transcript as its persisted SKELETON: only bytes
   appended since the last drain are parsed (a truncated / replaced / rewritten
   transcript is indexed again from byte 0); a live window below the gate is
   decided by subtraction, and the window text is read only for a file that
   fires. Positions, matches and extracted windows equal the slim parse.
4. State of a transcript that is gone is swept; an ended session's marker is
   cleared when its transcript is gone (A-032) and not kept by a session-end
   drain that left nothing behind.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_mcp_servers.rl_client import answer_window as aw
from claude_mcp_servers.rl_client import citation_pending as cp
from claude_mcp_servers.scripts import rl_drain_citations as drain
from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib.atomic import exclusive_file_lock

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")


# ── transcript fixtures ────────────────────────────────────────────────────

def _ts(i: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_790_000_000 + i * 7)) + ".000Z"


def _random_transcript(rnd: random.Random, n: int, start: int = 0) -> list:
    """Messages of every shape the consumers read (and some they must skip)."""
    out: list = []
    for i in range(start, start + n):
        r = rnd.random()
        if r < 0.05:
            out.append({"type": "system", "timestamp": _ts(i), "content": "x"})
            continue
        if r < 0.45:
            out.append({"type": "user", "timestamp": _ts(i), "message": {"content": [
                {"type": "tool_result", "content": "R" * rnd.randint(0, 3000)}]},
                "toolUseResult": {"stdout": "o" * rnd.randint(0, 500)}})
            continue
        blocks: list = []
        for _ in range(rnd.randint(0, 4)):
            k = rnd.random()
            if k < 0.25:
                blocks.append({"type": "text", "text": "w" * rnd.randint(0, 900)})
            elif k < 0.45:
                blocks.append({"type": "thinking", "thinking": "t" * rnd.randint(0, 900),
                               "signature": "s" * 50})
            elif k < 0.6:
                blocks.append({"type": "tool_use", "id": f"tu{i}",
                               "name": "mcp__weaviate-kg__hybrid_search",
                               "input": {"query": f"query {rnd.randint(0, 9)}"}})
            elif k < 0.7:
                blocks.append({"type": "tool_use", "name": "Bash", "input": {
                    "command": f'.claude/scripts/kg-search search "cli q {rnd.randint(0, 5)}"'}})
            elif k < 0.85:
                blocks.append({"type": "tool_use", "name": "Write",
                               "input": {"file_path": "a", "content": "c" * rnd.randint(0, 30000)}})
            elif k < 0.92:
                blocks.append("not a dict")
            else:
                blocks.append({"type": "image"})
        content = blocks if rnd.random() > 0.05 else "plain string content"
        msg = {"type": "assistant", "message": {"content": content}}
        if rnd.random() > 0.05:
            msg["timestamp"] = _ts(i)
        out.append(msg)
    return out


def _write(path: Path, messages: list, *, mode: str = "w", junk: bool = True) -> None:
    with open(path, mode, encoding="utf-8") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")
            if junk:
                fh.write("\n")  # blank lines are not messages


def _slim(path: Path) -> list:
    return [aw.slim_message(m) for m in aw.load_messages(path)]


def _assert_equivalent(full: list, skel: list) -> None:
    assert isinstance(skel, aw.TranscriptSkeleton)
    assert len(full) == len(skel)
    kg_full, kg_skel = aw.find_kg_positions(full), aw.find_kg_positions(skel)
    assert kg_full == kg_skel
    assert aw.find_cli_kg_queries(full) == aw.find_cli_kg_queries(skel)
    for q in [f"query {i}" for i in range(10)] + ["nothing", ""]:
        for pos in (None, 0, 3):
            assert (aw.match_position_for_query(full, kg_full, q, pos)
                    == aw.match_position_for_query(skel, kg_skel, q, pos))
    starts = list(kg_full) + [(i, -1) for i, m in enumerate(full)
                              if isinstance(m, dict) and m.get("type") == "assistant"]
    for i in range(0, len(full), 7):
        ts = aw._message_ts_ms(full[i]) if isinstance(full[i], dict) else None
        assert (aw.match_position_by_timestamp(full, ts)
                == aw.match_position_by_timestamp(skel, ts))
    for thr in (10, 2000, 25_000):
        for mi, bi in starts[::3]:
            want = aw.extract_answer_window(full, mi, bi, threshold_tokens=thr)
            assert aw.extract_answer_window(skel, mi, bi, threshold_tokens=thr) == want
            assert aw.answer_window_chars(skel, mi, bi, threshold_tokens=thr) == len(want[0])


# ── 3. the persisted skeleton ──────────────────────────────────────────────

class TestSkeletonEquivalence:
    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_skeleton_reads_like_the_slim_parse(self, tmp_path: Path, seed: int) -> None:
        rnd = random.Random(seed)
        tp = tmp_path / "s.jsonl"
        _write(tp, _random_transcript(rnd, 300))
        state = tmp_path / "state"
        skel = aw.load_transcript_skeleton(tp, state)
        assert isinstance(skel, aw.TranscriptSkeleton)
        _assert_equivalent(_slim(tp), skel)
        # Incremental: appended messages, then a partial final line.
        _write(tp, _random_transcript(rnd, 120, start=300), mode="a")
        with open(tp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": _ts(999),
                                 "message": {"content": [{"type": "text", "text": "tail"}]}}))
        skel2 = aw.load_transcript_skeleton(tp, state)
        full2 = aw.load_messages_cached(tp)  # its tentative-tail rule is the reference
        _assert_equivalent(full2, skel2)
        assert skel2[-1]["message"]["content"][0][aw._SKEL_LEN] == 4

    def test_a_second_load_parses_only_the_appended_bytes(self, tmp_path: Path,
                                                         monkeypatch) -> None:
        tp = tmp_path / "s.jsonl"
        _write(tp, _random_transcript(random.Random(5), 200), junk=False)
        state = tmp_path / "state"
        aw.load_transcript_skeleton(tp, state)
        parsed: list = []
        real = aw._parse_line
        monkeypatch.setattr(aw, "_parse_line", lambda raw: parsed.append(raw) or real(raw))
        new = _random_transcript(random.Random(6), 3, start=200)
        _write(tp, new, mode="a", junk=False)
        skel = aw.load_transcript_skeleton(tp, state)
        assert len(parsed) == 3, "only the three appended lines are parsed"
        assert len(skel) == 203
        parsed.clear()
        aw.load_transcript_skeleton(tp, state)
        assert parsed == [], "nothing appended: nothing parsed"

    @pytest.mark.parametrize("how", ["truncated", "replaced", "rewritten"])
    def test_truncated_replaced_or_rewritten_is_indexed_again(self, tmp_path: Path,
                                                              how: str) -> None:
        tp = tmp_path / "s.jsonl"
        _write(tp, _random_transcript(random.Random(8), 150))
        state = tmp_path / "state"
        aw.load_transcript_skeleton(tp, state)
        other = _random_transcript(random.Random(9), 40 if how == "truncated" else 220)
        if how == "replaced":
            tmp = tmp_path / "new.jsonl"
            _write(tmp, other)
            os.replace(tmp, tp)
        elif how == "rewritten":
            # Same inode, longer content whose byte before the old offset is
            # not a newline: the offset no longer ends a line (the last two
            # lines are now one unparseable line).
            raw = tp.read_bytes()
            assert raw.endswith(b"\n\n")
            with open(tp, "r+b") as fh:
                fh.seek(len(raw) - 1)
                fh.write(b"x")  # "x" + the next line: not JSON
            _write(tp, other, mode="a")
        else:
            _write(tp, other)
        skel = aw.load_transcript_skeleton(tp, state)
        _assert_equivalent(_slim(tp), skel)

    def test_a_replaced_transcript_under_a_read_skeleton_is_never_misread(
            self, tmp_path: Path) -> None:
        tp = tmp_path / "s.jsonl"
        _write(tp, _random_transcript(random.Random(10), 60))
        skel = aw.load_transcript_skeleton(tp, tmp_path / "state")
        tmp = tmp_path / "new.jsonl"
        _write(tmp, _random_transcript(random.Random(11), 60))
        os.replace(tmp, tp)
        mi = next(i for i, m in enumerate(skel) if m.get("type") == "assistant")
        with pytest.raises(aw.TranscriptChanged):
            aw.extract_answer_window(skel, mi, -1)


# ── drain level: what is read ──────────────────────────────────────────────

def _assistant_text(text: str, ts_ms: int) -> dict:
    return {"type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + ".000Z",
            "message": {"content": [{"type": "text", "text": text}]}}


def _stage(root: Path, sid: str, task: str, *, query: str = "q") -> Path:
    path = cp.stage_pending(session_id=sid, task_id=task, seq=None, query=query,
                            ctx={"nodes": [{"title": "A"}]}, source="hook",
                            project_root=root)
    assert path is not None
    return path


class TestDrainReadsOnlyWhatFires:
    def test_a_live_window_below_the_gate_is_left_without_reading_it(
            self, tmp_path: Path, monkeypatch) -> None:
        now = int(time.time() * 1000)
        f = _stage(tmp_path, "live", "t1")
        tdir = tmp_path / "t"
        tdir.mkdir()
        tp = tdir / "live.jsonl"
        _write(tp, [_assistant_text("words " * 400, now + 60_000)])
        reads: list = []
        monkeypatch.setattr(aw.TranscriptSkeleton, "_read_messages",
                            lambda self, off, want: reads.append(off) or pytest.fail("read"))
        summary = drain.drain_session("live", str(tp), project_root=str(tmp_path),
                                      compute_fn=lambda *a, **k: pytest.fail("computed"))
        assert summary["left"] == 1 and f.exists()
        assert reads == []

    def test_a_window_that_fires_is_read_and_computed_from_its_text(
            self, tmp_path: Path, monkeypatch) -> None:
        now = int(time.time() * 1000)
        _stage(tmp_path, "live", "t1")
        tdir = tmp_path / "t"
        tdir.mkdir()
        tp = tdir / "live.jsonl"
        _write(tp, [{"type": "user", "timestamp": _ts(1), "message": {"content": "hi"}},
                    _assistant_text("answer text " * 2000, now + 60_000)])
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "1000")
        seen: list = []
        summary = drain.drain_session(
            "live", str(tp), project_root=str(tmp_path),
            compute_fn=lambda task_id, answer, ctx, write=True: seen.append(answer) or {"ok": 1})
        assert summary["computed"] == 1
        full = _slim(tp)
        assert seen == [aw.extract_answer_window(full, 1, -1)[0]]

    def test_the_skeleton_is_persisted_in_the_drain_state_dir(self, tmp_path: Path) -> None:
        _stage(tmp_path, "live", "t1")
        tp = tmp_path / "live.jsonl"
        _write(tp, [_assistant_text("x", 1)])
        drain.drain_session("live", str(tp), project_root=str(tmp_path),
                            compute_fn=lambda *a, **k: None)
        state = cp.drain_state_dir(tmp_path)
        assert state is not None
        idx = state / f"{aw.transcript_key(tp)}{aw.SKELETON_INDEX_SUFFIX}"
        assert aw.skeleton_index_transcript(idx) == str(tp)


# ── 2. single flight ───────────────────────────────────────────────────────

class TestSingleFlight:
    def test_two_stops_in_a_row_are_one_drain_plus_one_rerun(self, tmp_path: Path) -> None:
        runs: list = []

        def run(end: bool) -> None:
            runs.append(end)
            if len(runs) == 1:
                # Three more requests arrive while the first drain runs.
                for _ in range(3):
                    assert drain.run_single_flight(tmp_path, "k", run) == 0

        assert drain.run_single_flight(tmp_path, "k", run) == 2
        assert runs == [False, False]
        assert not list(tmp_path.glob("k.rerun*")), "every request consumed"

    def test_an_end_request_during_a_stop_makes_the_rerun_a_session_end(
            self, tmp_path: Path) -> None:
        runs: list = []

        def run(end: bool) -> None:
            runs.append(end)
            if len(runs) == 1:
                drain.run_single_flight(tmp_path, "k", run, session_end=True)
                drain.run_single_flight(tmp_path, "k", run)

        drain.run_single_flight(tmp_path, "k", run)
        assert runs == [False, True]

    def test_a_held_lock_runs_nothing_here_and_keeps_the_request(self, tmp_path: Path) -> None:
        runs: list = []
        with exclusive_file_lock(tmp_path / f"k{drain.LOCK_SUFFIX}"):
            assert drain.run_single_flight(tmp_path, "k", runs.append) == 0
        assert runs == [] and (tmp_path / f"k{drain.RERUN_SUFFIX}").exists()
        # The next drain (lock free) runs the stranded request with its own.
        assert drain.run_single_flight(tmp_path, "k", runs.append) == 1

    def test_other_transcripts_do_not_wait(self, tmp_path: Path) -> None:
        runs: list = []
        with exclusive_file_lock(tmp_path / f"a{drain.LOCK_SUFFIX}"):
            assert drain.run_single_flight(tmp_path, "b", runs.append) == 1

    def test_a_failed_run_does_not_strand_the_queue(self, tmp_path: Path) -> None:
        runs: list = []

        def run(end: bool) -> None:
            runs.append(end)
            if len(runs) == 1:
                drain.run_single_flight(tmp_path, "k", run)
                raise RuntimeError("boom")

        assert drain.run_single_flight(tmp_path, "k", run) == 2


# ── 4. sweeps and end markers ──────────────────────────────────────────────

class TestSweep:
    def _key_files(self, d: Path, tp: Path) -> "tuple[str, Path]":
        aw.load_transcript_skeleton(tp, d)
        key = aw.transcript_key(tp)
        (d / f"{key}{drain.RERUN_SUFFIX}").touch()
        (d / f"{key}{drain.LOCK_SUFFIX}").touch()
        return key, d / f"{key}{aw.SKELETON_INDEX_SUFFIX}"

    def test_state_of_a_gone_transcript_is_removed(self, tmp_path: Path) -> None:
        d = tmp_path / "state"
        gone, live = tmp_path / "gone.jsonl", tmp_path / "live.jsonl"
        _write(gone, [_assistant_text("x", 1)])
        _write(live, [_assistant_text("x", 1)])
        gkey, gidx = self._key_files(d, gone)
        lkey, lidx = self._key_files(d, live)
        gone.unlink()
        assert drain.sweep_drain_state(d) == 3
        assert not list(d.glob(f"{gkey}*"))
        assert lidx.exists() and (d / f"{lkey}{drain.LOCK_SUFFIX}").exists()

    def test_a_held_lock_keeps_its_key(self, tmp_path: Path) -> None:
        d = tmp_path / "state"
        gone = tmp_path / "gone.jsonl"
        _write(gone, [_assistant_text("x", 1)])
        key, idx = self._key_files(d, gone)
        gone.unlink()
        with exclusive_file_lock(d / f"{key}{drain.LOCK_SUFFIX}"):
            assert drain.sweep_drain_state(d) == 0
        assert idx.exists()

    def test_an_idle_skeleton_is_removed_and_a_fresh_one_kept(self, tmp_path: Path) -> None:
        d = tmp_path / "state"
        tp = tmp_path / "s.jsonl"
        _write(tp, [_assistant_text("x", 1)])
        _key, idx = self._key_files(d, tp)
        assert drain.sweep_drain_state(d, idle_s=3600) == 0 and idx.exists()
        assert drain.sweep_drain_state(d, idle_s=3600, now=time.time() + 7200) == 3
        assert not idx.exists()


class TestEndMarkers:
    def test_an_ended_session_without_its_transcript_is_cleared(self, tmp_path: Path) -> None:
        """A-032 act: a nosession file exists, the ended session's transcript
        is not in the draining chat's folder → the marker goes."""
        _stage(tmp_path, "", "ns1", query="cli query")
        assert cp.mark_session_ended("gone", tmp_path)
        tdir = tmp_path / "t"
        tdir.mkdir()
        _write(tdir / "live.jsonl", [_assistant_text("x", 1)])
        drain.drain_session("live", str(tdir / "live.jsonl"), project_root=str(tmp_path),
                            compute_fn=lambda *a, **k: None)
        assert cp.ended_sessions(tmp_path) == []

    def test_an_ended_session_whose_transcript_holds_a_file_keeps_its_marker(
            self, tmp_path: Path, monkeypatch) -> None:
        """A-032 leave-alone: the nosession file's query is in the ended
        session's own transcript and the file is under another claim."""
        p = _stage(tmp_path, "", "ns1", query="held query")
        assert cp.mark_session_ended("ended", tmp_path)
        tdir = tmp_path / "t"
        tdir.mkdir()
        _write(tdir / "ended.jsonl", [{"type": "assistant", "timestamp": _ts(1), "message": {
            "content": [{"type": "tool_use", "name": "hybrid_search",
                         "input": {"query": "held query"}}]}}])
        _write(tdir / "live.jsonl", [_assistant_text("x", 1)])
        p.rename(p.with_name(p.name + cp.CLAIM_MARK + str(os.getpid())))  # the monitor holds it
        drain.drain_session("live", str(tdir / "live.jsonl"), project_root=str(tmp_path),
                            compute_fn=lambda *a, **k: None)
        assert cp.ended_sessions(tmp_path) == ["ended"]

    def test_a_session_end_that_leaves_nothing_keeps_no_marker(self, tmp_path: Path) -> None:
        tp = tmp_path / "s1.jsonl"
        _write(tp, [_assistant_text("x", 1)])
        drain.drain_session("s1", str(tp), project_root=str(tmp_path), session_end=True,
                            compute_fn=lambda *a, **k: None)
        assert cp.ended_sessions(tmp_path) == []

    def test_a_session_end_that_leaves_a_file_keeps_its_marker(self, tmp_path: Path) -> None:
        f = _stage(tmp_path, "s1", "t1")
        tp = tmp_path / "s1.jsonl"
        _write(tp, [_assistant_text("x" * 20000, int(time.time() * 1000) + 60_000)])
        # The compute fails: the file is left, so the end record must stay.
        drain.drain_session("s1", str(tp), project_root=str(tmp_path), session_end=True,
                            compute_fn=lambda *a, **k: None)
        assert f.exists() and cp.ended_sessions(tmp_path) == ["s1"]

    def test_a_session_end_whose_only_file_is_claimed_keeps_its_marker(
            self, tmp_path: Path) -> None:
        """Wave-2 check L-4: the hook now spawns this drain (the rule test
        below); the drain records the end and keeps it for the claimed file,
        so the file the monitor hands back is finalized by the next drain."""
        f = _stage(tmp_path, "s1", "t1")
        claim = f.with_name(f.name + cp.CLAIM_MARK + str(os.getpid()))  # the monitor holds it
        f.rename(claim)
        assert cp.session_may_drain("s1", tmp_path, session_end=True)
        tp = tmp_path / "s1.jsonl"
        _write(tp, [_assistant_text("x", 1)])
        drain.drain_session("s1", str(tp), project_root=str(tmp_path), session_end=True,
                            compute_fn=lambda *a, **k: None)
        assert claim.exists() and cp.ended_sessions(tmp_path) == ["s1"]


# ── 1. no spawn without a pending file (both hook siblings) ────────────────

_CASES = [
    ("empty", [], False),
    ("other_session", ["other__t.json"], False),
    ("claim_only", ["sessH__t.json.draining.123"], False),
    ("own", ["sessH__t.json"], True),
    ("nosession", ["nosession__kg_cli_1.json"], True),
    ("marker_only", ["sessH.session_ended"], False),
    ("other_claim", ["other__t.json.draining.9"], False),
]

#: At the main chat's SessionEnd a file of its own under a claim (the MCP
#: monitor's) also makes the drain run, so the session's end is recorded
#: (wave-2 check L-4); someone else's claim still does not.
_END_WANT = {"claim_only": True}


@pytest.mark.parametrize("name,files,want", _CASES, ids=[c[0] for c in _CASES])
def test_session_may_drain_rule(tmp_path: Path, name: str, files: list, want: bool) -> None:
    d = cp.pending_dir(tmp_path)
    for f in files:
        (d / f).write_text("{}")
    assert cp.session_may_drain("sessH", tmp_path) is want
    assert cp.session_may_drain("sessH", tmp_path, session_end=True) is _END_WANT.get(name, want)
    # An empty session id drains every file: any ``*.json`` counts — a claim
    # never does, SessionEnd or not.
    for end in (False, True):
        assert cp.session_may_drain("", tmp_path, session_end=end) is (
            name in ("other_session", "own", "nosession"))


_HOOK_CASES = [(sid, event, *c) for c in _CASES for sid in ("sessH", "")
               for event in ("Stop", "SessionEnd")
               if event == "Stop" or c[0] in ("claim_only", "other_claim", "own", "empty")]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake venv")
@pytest.mark.parametrize("flavour", ["sh", "ps1"])
@pytest.mark.parametrize("sid,event,name,files,want", _HOOK_CASES,
                         ids=[f"{c[2]}-{c[0] or 'nosid'}-{c[1]}" for c in _HOOK_CASES])
def test_hook_spawns_exactly_when_the_rule_says(tmp_path: Path, flavour: str, sid: str,
                                                event: str, name: str, files: list,
                                                want: bool) -> None:
    if flavour == "sh" and not BASH:
        pytest.skip("bash required")
    if flavour == "ps1" and not PWSH:
        pytest.skip("pwsh required")
    project = tmp_path / "proj"
    (project / ".claude" / "logs").mkdir(parents=True)
    d = cp.pending_dir(project)
    for f in files:
        (d / f).write_text("{}")
    want = cp.session_may_drain(sid, project, session_end=event == "SessionEnd")
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
    r = subprocess.run(argv, input=json.dumps({"hook_event_name": event, "session_id": sid,
                                               "transcript_path": "/t/sessH.jsonl"}),
                       capture_output=True, text=True, env=env, cwd=str(project), timeout=120)
    assert r.returncode == 0, r.stderr
    if want:
        deadline = time.monotonic() + 30
        while not argv_log.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert argv_log.exists(), "the drain was never spawned"
    else:
        time.sleep(1.5)
        assert not argv_log.exists(), "a drain was spawned without a pending file"
