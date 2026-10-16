# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-F item 2 — the work-unit counter must COUNT, never answer
"0 because too big".

Pre-fix defects (research report 2026-10-10, item 9 — verified against live
state: 635 of 639 recorded sessions show ``last_seen_total == 0``):

* ``templates/scripts/claude_token_counter.py`` returned an EMPTY
  ``ScanResult`` for any transcript of 256 MB or more
  (``MAX_TRANSCRIPT_BYTES`` give-up). This machine's main-session transcript
  is 1.55 GB, so the KG-update nudge's counter never moved on exactly the
  long sessions the nudge exists for.
* ``kg-update-nudge.{sh,ps1}`` resolved the counter module from
  ``os.getcwd()/.claude/scripts`` — a session whose cwd is not the project
  root (e.g. working in another checkout) silently got ``session_total = 0``.

Post-fix contract pinned here:

* ``TranscriptScanner.scan(path, start_offset=N)`` resumes from a byte
  offset and reports ``end_offset`` — an incremental scan never re-reads
  (and never double-counts) already-consumed bytes.
* A transcript at/over the full-scan limit is answered by a BOUNDED TAIL
  scan (the counter keeps working; the number is "work units in the tail
  window", documented) — never an empty result on size alone.
* A final line without a trailing newline is LEFT UNCONSUMED (a live
  transcript is being appended to; the partial line completes later).
* A stored offset past end-of-file (rotated/truncated transcript) restarts
  the scan instead of silently counting nothing forever.
* The nudge hook resolves the counter from the hook's own location /
  ``CLAUDE_PROJECT_DIR`` (never cwd), persists ``scan_offset`` +
  ``cumulative_total`` per session, and fails LOUDLY (stderr + non-zero
  exit) when the counter module is missing but a transcript is there to
  count.

RED on the pre-fix tree: the 256 MB give-up returns 0 (tests 1-2), there is
no ``start_offset``/``end_offset`` (tests 3-5), and the hook resolves the
counter from cwd only (test 6's fixture has no ``.claude/scripts`` under
cwd, so the pre-fix hook writes ``last_seen_total: 0``).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COUNTER_PY = REPO_ROOT / "templates" / "scripts" / "claude_token_counter.py"
NUDGE_SH = REPO_ROOT / "templates" / "hooks" / "kg-update-nudge.sh"


def _load_counter():
    spec = importlib.util.spec_from_file_location("claude_token_counter", COUNTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_token_counter"] = mod  # dataclasses resolve the module here
    spec.loader.exec_module(mod)
    return mod


def _entry(req: str, out_tokens: int, text: str = "x") -> str:
    return json.dumps({
        "requestId": req,
        "message": {
            "role": "assistant",
            "usage": {"output_tokens": out_tokens},
            "content": [{"type": "text", "text": text}],
        },
    }) + "\n"


def _write_transcript(path: Path, entries: list) -> None:
    path.write_text("".join(entries), encoding="utf-8")


# --- 1. oversized transcript (injected tiny limits) ---------------------------


def test_oversized_transcript_gets_a_bounded_tail_scan(tmp_path: Path) -> None:
    """A transcript over the full-scan limit must NOT come back empty — the
    tail window still counts. Constructor-injected tiny limits exercise the
    same branch the shipped 256 MB / 64 MB constants take."""
    ctc = _load_counter()
    t = tmp_path / "big.jsonl"
    entries = [_entry(f"r{i}", 100) for i in range(400)]  # ~45 KB of JSONL
    _write_transcript(t, entries)
    assert t.stat().st_size > 4096
    scanner = ctc.TranscriptScanner(max_full_scan_bytes=4096, tail_scan_bytes=2048)
    result = scanner.scan(t)
    assert result.work_units_total > 0, (
        "an oversized transcript must be tail-scanned, never answered with 0"
    )
    assert result.start_offset_used > 0, "the tail scan must seek, not read from 0"
    # The tail window bounded the scan: fewer units than the full file's.
    full = ctc.TranscriptScanner(max_full_scan_bytes=10**12).scan(t)
    assert result.work_units_total < full.work_units_total
    assert result.end_offset == t.stat().st_size


def test_real_256mb_sparse_transcript_scans_its_tail(tmp_path: Path) -> None:
    """The shipped constants themselves: a >256 MB transcript (sparse hole +
    real JSONL appended at the end) yields non-zero work units. Pre-fix the
    256 MB give-up returned an empty ScanResult for exactly this file."""
    ctc = _load_counter()
    t = tmp_path / "huge.jsonl"
    # Leading "\n": the tail-window seek lands inside the sparse hole, and the
    # hole's NUL run then terminates as its OWN unparseable line (the exact
    # boundary behaviour a mid-line seek has on a real file: the partial line
    # is skipped by the JSON parse, the next complete line counts).
    entries = "\n" + "".join(_entry(f"tail-{i}", 250) for i in range(50))
    with open(t, "wb") as fh:
        fh.seek(257 * 1024 * 1024)  # sparse hole — instant, no real blocks
        fh.write(entries.encode("utf-8"))
    assert t.stat().st_size >= ctc.TranscriptScanner.MAX_TRANSCRIPT_BYTES
    result = ctc.TranscriptScanner().scan(t)
    assert result.work_units_total == 50 * 250
    assert result.end_offset == t.stat().st_size


# --- 2. incremental resume -----------------------------------------------------


def test_offset_resume_never_double_counts(tmp_path: Path) -> None:
    ctc = _load_counter()
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [_entry("a", 100), _entry("b", 200)])
    sc = ctc.TranscriptScanner()
    first = sc.scan(t)
    assert first.work_units_total == 300
    assert first.end_offset == t.stat().st_size > 0

    with open(t, "a", encoding="utf-8") as fh:
        fh.write(_entry("c", 50))
    second = sc.scan(t, start_offset=first.end_offset)
    assert second.work_units_total == 50, (
        f"resume must count ONLY the appended entries, got {second.work_units_total}"
    )
    assert second.start_offset_used == first.end_offset
    assert second.end_offset == t.stat().st_size
    assert first.work_units_total + second.work_units_total == sc.scan(t).work_units_total


def test_offset_resume_dedups_a_straddled_request(tmp_path: Path) -> None:
    """A requestId whose streaming entries straddle the resume boundary must
    not be counted twice (the backscan seeds the dedup set)."""
    ctc = _load_counter()
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [_entry("a", 100)])
    sc = ctc.TranscriptScanner()
    first = sc.scan(t)
    # A second streaming entry for the SAME requestId arrives after the
    # first scan, then a fresh request.
    with open(t, "a", encoding="utf-8") as fh:
        fh.write(_entry("a", 100))  # duplicate streaming row of request a
        fh.write(_entry("b", 40))
    second = sc.scan(t, start_offset=first.end_offset)
    assert second.work_units_total == 40, (
        "the straddled requestId's repeat row must be deduped, got "
        f"{second.work_units_total}"
    )


def test_offset_past_eof_restarts(tmp_path: Path) -> None:
    """A stored offset beyond the file size means the transcript was rotated
    or truncated: restart from the beginning instead of counting nothing."""
    ctc = _load_counter()
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [_entry("a", 70)])
    result = ctc.TranscriptScanner().scan(t, start_offset=10**9)
    assert result.start_offset_used == 0
    assert result.work_units_total == 70


def test_partial_final_line_is_left_unconsumed(tmp_path: Path) -> None:
    """A live transcript's last line may be mid-write: without a trailing
    newline it is NOT counted and NOT consumed — the next resume gets it."""
    ctc = _load_counter()
    t = tmp_path / "s.jsonl"
    t.write_text(_entry("a", 100) + json.dumps({
        "requestId": "b",
        "message": {"role": "assistant", "usage": {"output_tokens": 900},
                    "content": []},
    }), encoding="utf-8")  # no trailing newline on the second entry
    sc = ctc.TranscriptScanner()
    first = sc.scan(t)
    assert first.work_units_total == 100
    assert first.end_offset < t.stat().st_size
    with open(t, "a", encoding="utf-8") as fh:
        fh.write("\n")
    second = sc.scan(t, start_offset=first.end_offset)
    assert second.work_units_total == 900
    assert second.end_offset == t.stat().st_size


def test_scan_at_eof_resumes_from_its_start_not_zero(tmp_path: Path) -> None:
    """A scan that starts at EOF consumes nothing: its resume point is its own
    start. Returning 0 here made the nudge re-count the whole transcript."""
    ctc = _load_counter()
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [_entry("a", 100), _entry("b", 200)])
    size = t.stat().st_size
    r = ctc.TranscriptScanner().scan(t, start_offset=size)
    assert r.work_units_total == 0
    assert r.start_offset_used == size
    assert r.end_offset == size


def test_only_a_partial_line_after_the_offset_keeps_the_offset(tmp_path: Path) -> None:
    ctc = _load_counter()
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [_entry("a", 100)])
    done = t.stat().st_size
    with open(t, "a", encoding="utf-8") as fh:
        fh.write('{"requestId": "b", "message": {"role": "assist')  # mid-write
    r = ctc.TranscriptScanner().scan(t, start_offset=done)
    assert r.work_units_total == 0
    assert r.end_offset == done


# --- 3. the hook: resolution + persistence + loud-fail --------------------------

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook")


def _nudge_rig(tmp_path: Path) -> dict:
    """A STAGED hook (copied beside its real ``_lib`` into a tmp hooks dir,
    like the dispatcher tests rig) plus a fixture project holding the REAL
    counter module the way a bundle install places it, plus a separate cwd
    that has NO .claude/scripts — the pre-fix cwd-relative resolution then
    finds nothing. Staging the hook matters: run from the checkout, the
    hook-relative leg would always find the checkout's own
    ``templates/scripts`` copy and the "module missing" case could never
    exist."""
    proj = tmp_path / "proj"
    scripts = proj / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(COUNTER_PY, scripts / "claude_token_counter.py")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(NUDGE_SH, hooks / NUDGE_SH.name)
    shutil.copytree(NUDGE_SH.parent / "_lib", hooks / "_lib")
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    state = tmp_path / "vctstate"
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "KG_NUDGE_OFF")}
    env.update({
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCT_STATE_DIR": str(state),
        "HOME": str(tmp_path / "home"),
    })
    return {"proj": proj, "hooks": hooks, "cwd": other_cwd,
            "transcript": transcript, "env": env,
            "metrics": state / "metrics" / "kg_update_tokens.jsonl"}


def _run_nudge(rig: dict, session: str, payload: dict | None = None) -> subprocess.CompletedProcess:
    payload = payload or {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "transcript_path": str(rig["transcript"]),
        "prompt": "keep working on the scanner",
    }
    return subprocess.run(
        ["bash", str(rig["hooks"] / NUDGE_SH.name)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60,
        cwd=str(rig["cwd"]), env=rig["env"],
    )


def _state_row(rig: dict, session: str) -> dict:
    assert rig["metrics"].is_file(), "the nudge wrote no metrics row"
    for line in rig["metrics"].read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("session_id") == session:
            return row
    raise AssertionError(f"no state row for {session}")


@needs_bash
def test_nudge_counts_from_hook_relative_counter_not_cwd(tmp_path: Path) -> None:
    """RED pre-fix: the hook resolves the counter from os.getcwd(), which
    here holds no .claude/scripts — the row records last_seen_total 0."""
    rig = _nudge_rig(tmp_path)
    _write_transcript(rig["transcript"], [_entry(f"r{i}", 1000) for i in range(10)])
    r = _run_nudge(rig, "s-count")
    assert r.returncode == 0, r.stderr[-800:]
    row = _state_row(rig, "s-count")
    assert row["last_seen_total"] == 10_000, row
    assert row.get("scan_offset", 0) == rig["transcript"].stat().st_size, (
        "the resume offset must be persisted per session"
    )


@needs_bash
def test_nudge_resume_does_not_double_count(tmp_path: Path) -> None:
    rig = _nudge_rig(tmp_path)
    _write_transcript(rig["transcript"], [_entry("a", 5000)])
    assert _run_nudge(rig, "s-resume").returncode == 0
    with open(rig["transcript"], "a", encoding="utf-8") as fh:
        fh.write(_entry("b", 7000))
    assert _run_nudge(rig, "s-resume").returncode == 0
    row = _state_row(rig, "s-resume")
    assert row["last_seen_total"] == 12_000, row


@needs_bash
def test_kg_write_with_no_new_bytes_keeps_offset_and_total(tmp_path: Path) -> None:
    """2026-10-14 field report: prompt → KG-write PostToolUse (no new
    transcript bytes) → prompt. The KG write's scan starts at EOF; it must
    not persist scan_offset=0, or the next prompt re-adds the whole
    transcript and the nudge fires right after a KG write."""
    rig = _nudge_rig(tmp_path)
    _write_transcript(rig["transcript"], [_entry("a", 5000), _entry("b", 7000)])
    size = rig["transcript"].stat().st_size
    assert _run_nudge(rig, "s-kgw").returncode == 0
    kg_write = {
        "hook_event_name": "PostToolUse",
        "session_id": "s-kgw",
        "transcript_path": str(rig["transcript"]),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(rig["proj"] / "knowledge" / "concepts" / "x.md")},
    }
    assert _run_nudge(rig, "s-kgw", kg_write).returncode == 0
    after_write = _state_row(rig, "s-kgw")
    assert after_write["scan_offset"] == size, after_write
    assert _run_nudge(rig, "s-kgw").returncode == 0
    row = _state_row(rig, "s-kgw")
    assert row["last_seen_total"] == 12_000, row
    assert row["scan_offset"] == size, row


@needs_bash
def test_nudge_fires_when_threshold_crossed(tmp_path: Path) -> None:
    """End-to-end: a session past KG_NUDGE_FIRST gets the nudge on stdout
    (UserPromptSubmit stdout becomes a system-reminder)."""
    rig = _nudge_rig(tmp_path)
    _write_transcript(rig["transcript"], [_entry("big", 200_000)])
    r = _run_nudge(rig, "s-fire")
    assert r.returncode == 0, r.stderr[-800:]
    assert "KG-update nudge" in r.stdout, r.stdout[-400:]
    # Second prompt, no new work: fired_once suppresses a repeat.
    r2 = _run_nudge(rig, "s-fire")
    assert "KG-update nudge" not in r2.stdout


@needs_bash
def test_nudge_survives_transcript_rotation(tmp_path: Path) -> None:
    """The transcript is replaced by a fresh (smaller) file mid-session: the
    persisted offset must not skip regrown content. Rotation to EMPTY is the
    edge: the scan consumes nothing and must persist offset 0, not keep the
    stale pre-rotation offset."""
    rig = _nudge_rig(tmp_path)
    _write_transcript(rig["transcript"], [_entry("old", 5000)])
    assert _run_nudge(rig, "s-rot").returncode == 0
    assert _state_row(rig, "s-rot")["scan_offset"] > 0
    # rotation: a brand-new, EMPTY transcript at the same path
    rig["transcript"].write_text("", encoding="utf-8")
    assert _run_nudge(rig, "s-rot").returncode == 0
    assert _state_row(rig, "s-rot")["scan_offset"] == 0
    # the new transcript regrows BELOW the old offset: nothing may be skipped
    _write_transcript(rig["transcript"], [_entry("new", 3000)])
    assert _run_nudge(rig, "s-rot").returncode == 0
    row = _state_row(rig, "s-rot")
    assert row["last_seen_total"] == 3000, row


@needs_bash
def test_nudge_loud_when_counter_module_missing(tmp_path: Path) -> None:
    """A transcript to count but no importable counter module is a BROKEN
    INSTALL — loud stderr + non-zero exit, never a silent 0."""
    rig = _nudge_rig(tmp_path)
    os.remove(rig["proj"] / ".claude" / "scripts" / "claude_token_counter.py")
    _write_transcript(rig["transcript"], [_entry("a", 100)])
    r = _run_nudge(rig, "s-broken")
    assert r.returncode != 0, "a missing counter module must fail loudly"
    assert "claude_token_counter" in r.stderr


@needs_bash
def test_nudge_silent_without_transcript(tmp_path: Path) -> None:
    """No transcript in the payload = nothing to count: not an error, exit 0
    and no loud line even if the counter module is missing."""
    rig = _nudge_rig(tmp_path)
    os.remove(rig["proj"] / ".claude" / "scripts" / "claude_token_counter.py")
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s-none",
               "prompt": "hi"}
    r = subprocess.run(
        ["bash", str(rig["hooks"] / NUDGE_SH.name)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60,
        cwd=str(rig["cwd"]), env=rig["env"],
    )
    assert r.returncode == 0, r.stderr
