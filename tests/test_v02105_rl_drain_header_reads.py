# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the citation drain reads only what it decides on, and a
``kg-search`` CLI retrieval is attributed to its chat.

1. The abandoned-file collector (``citation_pending.collect_abandoned``), the
   drain's pre-claim peeks and the brief adopter parsed EVERY pending file in
   full (``read_pending``) on every run — ~6 GB per Stop on one project, the
   same pattern that hung the context router. They now read only the routing
   fields (``read_pending_header``, the file's two ends); the full payload is
   read only for a file a pass has claimed to score. The decisions are the
   same: what was collected is still collected, what was kept is still kept.
2. A ``kg-search`` CLI run is staged without a session id (``nosession__
   kg_cli_*``), and the drain attributed such a file only through the chat's
   MCP ``hybrid_search`` calls — never through the Bash command that ran the
   CLI — so it could never be scored. The drain now also reads the query of
   the chat's own ``kg-search search <query>`` Bash / PowerShell calls.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from claude_mcp_servers.rl_client import answer_window as aw
from claude_mcp_servers.rl_client import citation_pending as cp
from claude_mcp_servers.scripts import rl_drain_citations as drain

DAY = 86400.0


def _big_ctx(n_nodes: int = 30) -> dict:
    # Well above the header reader's whole-file threshold (32 KiB), with
    # nested dicts and lists — the shape of a real staged ctx.
    return {
        "nodes": [{"title": f"node {i}", "chunk": "c" * 50,
                   "n_emb": [0.001 * j for j in range(256)],
                   "meta": {"k": i, "tags": ["a", "b"]}} for i in range(n_nodes)],
        "query_emb": [0.5] * 256,
        "task_type": "pre_edit_kg_search",
        "session_id": "s",
    }


def _stage(root: Path, sid: str, task: str, *, query: str = "q", ctx=None,
           source: str = "hook", agent_id: str = "") -> Path:
    path = cp.stage_pending(session_id=sid, task_id=task, seq=None, query=query,
                            ctx=_big_ctx() if ctx is None else ctx, source=source,
                            project_root=root, agent_id=agent_id)
    assert path is not None
    return path


def _rewrite(path: Path, **fields) -> None:
    """Change fields the way the stampers do: load, set, write back (a NEW
    key lands after ``ctx``, as an agent-transcript stamp does)."""
    payload = cp.read_pending(path)
    assert isinstance(payload, dict)
    payload.update(fields)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n")


def _read(path: Path) -> dict:
    payload = cp.read_pending(path)
    assert isinstance(payload, dict)
    return payload


def _head(path: Path) -> dict:
    head = cp.read_pending_header(path)
    assert isinstance(head, dict)
    return head


def _full_minus_ctx(path: Path):
    payload = cp.read_pending(path)
    return None if not isinstance(payload, dict) else {
        k: v for k, v in payload.items() if k != "ctx"}


# --------------------------------------------------------------------------
# read_pending_header: same fields as a full read, without reading the ctx
# --------------------------------------------------------------------------


class TestHeaderReader:
    def test_staged_file_header_equals_full_read_without_ctx(self, tmp_path: Path) -> None:
        p = _stage(tmp_path, "s1", "t1", query='quoted "q" — ünïcode, "x": {}')
        assert p.stat().st_size > 64 * 1024
        assert cp.read_pending_header(p) == _full_minus_ctx(p)

    def test_fields_appended_after_ctx_are_read_from_the_tail(self, tmp_path: Path) -> None:
        p = _stage(tmp_path, "s1", "t2", agent_id="a1")
        _rewrite(p, agent_transcript_path="/x/s1/subagents/agent-a1.jsonl",
                 **{cp.AGENT_WINDOW_FINAL_KEY: False}, brief_tool_use_id="toolu_1")
        head = _head(p)
        assert head == _full_minus_ctx(p)
        assert head["agent_transcript_path"] == "/x/s1/subagents/agent-a1.jsonl"
        assert head[cp.AGENT_WINDOW_FINAL_KEY] is False
        assert "ctx" not in head

    def test_header_does_not_parse_the_ctx(self, tmp_path: Path, monkeypatch) -> None:
        p = _stage(tmp_path, "s1", "t3")
        monkeypatch.setattr(cp, "read_pending",
                            lambda path: pytest.fail("the whole file was parsed"))
        assert _head(p)["task_id"] == "t3"

    def test_small_file_is_parsed_whole(self, tmp_path: Path) -> None:
        p = _stage(tmp_path, "s1", "t4", ctx={"nodes": []})
        assert cp.read_pending_header(p) == _full_minus_ctx(p)

    @pytest.mark.parametrize("extra", [[1, 2], {"a": 1}, [{"a": 1, "b": 2}]])
    def test_undecidable_tail_falls_back_to_the_full_read(self, tmp_path: Path, extra) -> None:
        # A non-scalar field after ctx (a pre-v0.2.105 writer's layout): the
        # tail cannot settle it alone, so the whole file is read.
        p = _stage(tmp_path, "s1", "t5")
        _rewrite(p, extra=extra)
        assert list(_read(p))[-1] == "extra"  # really after ctx
        assert cp.read_pending_header(p) == _full_minus_ctx(p)

    def test_the_pending_writer_puts_ctx_last(self, tmp_path: Path, monkeypatch) -> None:
        # Any field a writer adds — whatever its shape — lands before ctx,
        # where the head parse reads it exactly without the tail.
        p = _stage(tmp_path, "s1", "t7")
        payload = _read(p)
        payload["later"] = {"a": 1, "b": [1, {"c": 2}]}
        cp.write_pending(p, payload)
        assert list(_read(p))[-1] == "ctx"
        monkeypatch.setattr(cp, "read_pending",
                            lambda path: pytest.fail("the whole file was parsed"))
        head = _head(p)
        assert head["later"] == {"a": 1, "b": [1, {"c": 2}]}
        assert "ctx" not in head and head["task_id"] == "t7"

    def test_brief_id_and_stamps_are_written_before_ctx(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VCT_BRIEF_TOOL_USE_ID", "toolu_9")
        p = _stage(tmp_path, "s1", "t8")
        assert list(_read(p))[-1] == "ctx"
        drain._stamp_agent_transcript(p, _read(p), "/t/agent-x.jsonl")
        keys = list(_read(p))
        assert keys[-1] == "ctx" and "agent_transcript_path" in keys
        assert _head(p)["brief_tool_use_id"] == "toolu_9"

    def test_unreadable_file_is_none_like_the_full_read(self, tmp_path: Path) -> None:
        p = _stage(tmp_path, "s1", "t6")
        data = p.read_bytes()
        p.write_bytes(data[: len(data) // 2])  # truncated mid-ctx
        assert cp.read_pending(p) is None
        assert cp.read_pending_header(p) is None
        (tmp_path / "missing.json").touch()
        assert cp.read_pending_header(tmp_path / "missing.json") is None
        assert cp.read_pending_header(tmp_path / "nope.json") is None


# --------------------------------------------------------------------------
# The collector: same decisions, no full read of a file it keeps
# --------------------------------------------------------------------------


def _backlog(root: Path, tx_dir: Path) -> None:
    """A mixed backlog covering every branch of the abandonment rule."""
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - int(9 * DAY * 1000)
    tx_dir.mkdir(parents=True, exist_ok=True)
    old_tx = tx_dir / "old.jsonl"
    old_tx.write_text("{}\n")
    old_s = time.time() - 9 * DAY
    os.utime(old_tx, (old_s, old_s))
    fresh_tx = tx_dir / "fresh.jsonl"
    fresh_tx.write_text("{}\n")
    # 1. old stage time, no transcript at all -> collected (unscored)
    _rewrite(_stage(root, "gone", "a1"), ts_ms=old_ms)
    # 2. fresh stage time, no transcript -> kept
    _stage(root, "gone", "a2")
    # 3. fresh stage time, but its (stamped) transcript idle 9 days -> collected
    p = _stage(root, "s3", "a3", agent_id="ag3")
    _rewrite(p, agent_transcript_path=str(old_tx))
    # 4. old stage time, but its stamped transcript written now -> kept
    p = _stage(root, "s4", "a4", agent_id="ag4")
    _rewrite(p, ts_ms=old_ms, agent_transcript_path=str(fresh_tx))
    # 5. the running session's own old file -> kept (live owner)
    _rewrite(_stage(root, "live", "a5"), ts_ms=old_ms)
    # 6. an old file without a session id -> collected
    _rewrite(_stage(root, "", "a6", source="mcp", query="nobody"), ts_ms=old_ms)
    # 7. an old file under another process's claim -> kept
    p = _stage(root, "s7", "a7")
    _rewrite(p, ts_ms=old_ms)
    p.rename(p.with_name(p.name + cp.CLAIM_MARK + "999999"))


def _source_fn(payload: dict) -> str:
    return str(payload.get("agent_transcript_path") or "")


def _run_collector(root: Path) -> "tuple[list[str], list[tuple[str, str]], int]":
    removed = cp.collect_abandoned(root, live_session="live", source_fn=_source_fn,
                                   score_fn=None)
    left = sorted(p.name for p in (root / ".claude" / "state" / "rl_pending").iterdir())
    log = root / cp.UNSCORED_LOG_REL
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return left, sorted((r["file"], r["reason"]) for r in rows), removed


class TestCollectorDecisionUnchanged:
    def test_same_files_collected_and_kept_as_a_full_read_decides(
            self, tmp_path: Path, monkeypatch) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        _backlog(a, tmp_path / "tx")
        shutil.copytree(a, b)
        new = _run_collector(a)
        # The pre-v0.2.105 decision input: the whole payload.
        monkeypatch.setattr(cp, "read_pending_header", cp.read_pending)
        old = _run_collector(b)
        assert new == old
        left, rows, removed = new
        assert removed == 3
        assert [f for f, _ in rows] == sorted([
            "gone__a1.json", "s3__a3.json", "nosession__a6.json"])
        assert {r for _, r in rows} == {"no_scorer"}
        assert "gone__a2.json" in left and "s4__a4.json" in left
        assert "live__a5.json" in left
        assert any(n.startswith("s7__a7.json" + cp.CLAIM_MARK) for n in left)

    def test_kept_files_are_never_read_in_full(self, tmp_path: Path, monkeypatch) -> None:
        for i in range(5):
            _stage(tmp_path, f"s{i}", f"t{i}")
        reads: list = []
        real = cp.read_pending
        monkeypatch.setattr(cp, "read_pending", lambda p: reads.append(p) or real(p))
        assert cp.collect_abandoned(tmp_path, live_session="x", source_fn=_source_fn) == 0
        assert reads == []

    def test_a_collected_file_is_read_in_full_once_claimed(
            self, tmp_path: Path, monkeypatch) -> None:
        old_ms = int(time.time() * 1000) - int(9 * DAY * 1000)
        _rewrite(_stage(tmp_path, "gone", "t1"), ts_ms=old_ms)
        seen: list = []

        def _score(claimed, payload, transcript):
            seen.append(payload)
            return None

        reads: list = []
        real = cp.read_pending
        monkeypatch.setattr(cp, "read_pending", lambda p: reads.append(Path(p).name) or real(p))
        n = cp.collect_abandoned(tmp_path, live_session="x",
                                 source_fn=lambda _p: str(tmp_path / "none.jsonl"),
                                 score_fn=_score)
        assert n == 1
        # Only the claimed file was read in full, and the scorer got its ctx.
        assert reads and all(cp.CLAIM_MARK in name for name in reads)
        assert len(seen) == 1 and isinstance(seen[0].get("ctx"), dict)


class TestDrainPeeksReadHeadersOnly:
    def test_other_chats_files_are_never_read_in_full(
            self, tmp_path: Path, monkeypatch) -> None:
        for i in range(4):
            _stage(tmp_path, f"other{i}", f"t{i}")
            _stage(tmp_path, "", f"n{i}", source="mcp", query=f"elsewhere {i}")
        tp = tmp_path / "live.jsonl"
        tp.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"}]}}) + "\n")
        reads: list = []
        real = cp.read_pending
        monkeypatch.setattr(cp, "read_pending", lambda p: reads.append(p) or real(p))
        summary = drain.drain_session("live", str(tp), project_root=str(tmp_path),
                                      compute_fn=lambda *a, **k: pytest.fail("computed"))
        assert summary["computed"] == 0
        assert reads == []
        assert len(list((tmp_path / ".claude/state/rl_pending").glob("*.json"))) == 8


# --------------------------------------------------------------------------
# kg-search CLI retrievals are attributed to the chat that ran them
# --------------------------------------------------------------------------


def _ts_iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _bash(cmd: str, ts_ms: int, name: str = "Bash") -> dict:
    return {"type": "assistant", "timestamp": _ts_iso(ts_ms), "message": {"content": [
        {"type": "tool_use", "name": name, "input": {"command": cmd}}]}}


def _text(text: str, ts_ms: int) -> dict:
    return {"type": "assistant", "timestamp": _ts_iso(ts_ms),
            "message": {"content": [{"type": "text", "text": text}]}}


def _cli_case(tmp_path: Path, command: str, *, query: str = "callback thread physics worker"):
    now = int(time.time() * 1000)
    p = _stage(tmp_path, "", "kg_cli_0123abcd", query=query, source="hook",
               ctx={"nodes": [{"title": "A", "n_emb": [0.1]}]})
    _rewrite(p, ts_ms=now)
    tp = tmp_path / "chat.jsonl"
    tp.write_text("\n".join(json.dumps(m) for m in [
        _bash(command, now - 200),
        _text("the answer that used the CLI result " * 20, now + 500),
    ]) + "\n")
    calls: list = []

    def _compute(task_id, answer, ctx, write=True):
        calls.append(task_id)
        return {"cited": {"A": True}}

    return p, tp, calls, _compute


class TestCliRetrievalAttribution:
    def test_chat_that_ran_the_cli_scores_its_retrieval(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")
        p, tp, calls, compute = _cli_case(
            tmp_path, 'cd /x; .claude/scripts/kg-search search '
                      '"callback thread physics worker" 2>/dev/null | head -5')
        summary = drain.drain_session("chat", str(tp), project_root=str(tmp_path),
                                      compute_fn=compute)
        assert calls == ["kg_cli_0123abcd"]
        assert summary["computed"] == 1
        assert not p.exists()

    def test_a_chat_without_that_cli_call_leaves_it(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "10")
        # Same words, but not a kg-search query: an echo, and a longer query.
        p, tp, calls, compute = _cli_case(
            tmp_path, 'echo "callback thread physics worker"; '
                      '.claude/scripts/kg-search search "callback thread physics worker pool"')
        summary = drain.drain_session("chat", str(tp), project_root=str(tmp_path),
                                      compute_fn=compute)
        assert calls == []
        assert summary["computed"] == 0
        assert p.exists()


class TestCliQueryParser:
    @pytest.mark.parametrize("command,powershell,expected", [
        ('.claude/scripts/kg-search search "a b"', False, ["a b"]),
        ('kg-search search --limit 3 "a b" --type concept && kg-search list', False, ["a b"]),
        ('kg-search search --detail=full "x"; kg-search search y -p P', False, ["x", "y"]),
        ('python3 .claude/scripts/search_knowledge.py search "z"', False, ["z"]),
        (r'& C:\p\.claude\scripts\kg-search.ps1 search "q x" -c K', True, ["q x"]),
        ('echo kg-search search q', False, []),
        ('FOO=1 bash .claude/scripts/kg-search search q', False, ['q']),
        ('echo "kg-search search q"', False, []),
        ('kg-search list', False, []),
        ('kg-search recent "q"', False, []),
        ('echo "unterminated', False, []),
        ('my-kg-search search q', False, []),
    ])
    def test_parse(self, command: str, powershell: bool, expected: list) -> None:
        assert aw.cli_kg_search_queries(command, powershell=powershell) == expected

    def test_transcript_scan_takes_shell_tool_calls_only(self) -> None:
        msgs = [
            _bash('kg-search search "from bash"', 1),
            _bash('kg-search search "from pwsh"', 2, name="PowerShell"),
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"command": 'kg-search search "not a shell"'}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": 'kg-search search "user side"'}}]}},
        ]
        assert aw.find_cli_kg_queries(msgs) == frozenset({"from bash", "from pwsh"})


class TestBriefAdopterReadsHeadersOnly:
    def test_other_briefs_files_are_never_read_in_full(
            self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VCT_BRIEF_TOOL_USE_ID", "toolu_other")
        for i in range(3):
            _stage(tmp_path, "s1", f"t{i}")
        monkeypatch.setenv("VCT_BRIEF_TOOL_USE_ID", "toolu_mine")
        mine = _stage(tmp_path, "s1", "mine")
        reads: list = []
        real = cp.read_pending
        monkeypatch.setattr(cp, "read_pending", lambda p: reads.append(Path(p).name) or real(p))
        tp = tmp_path / "s1.jsonl"
        tp.write_text("{}\n")
        agent_tp = tmp_path / "s1" / "subagents" / "agent-ag1.jsonl"
        agent_tp.parent.mkdir(parents=True, exist_ok=True)
        agent_tp.write_text("{}\n")
        n = cp.adopt_brief_citations("s1", "toolu_mine", "ag1", agent_finished=True,
                                     parent_transcript=str(tp), project_root=tmp_path)
        assert n == 1
        assert all(cp.CLAIM_MARK in name for name in reads)  # only the adopted claim
        assert _read(mine)["agent_id"] == "ag1"
