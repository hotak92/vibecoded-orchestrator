# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-F item 3 — user-prompt-submit-reminder's counter must COUNT.

Pre-fix defect (research report 2026-10-10, item 10 — verified):
``user-prompt-submit-reminder.sh`` set ``WORDS_THIS_TURN=0`` on every
invocation (its own comment: "today it always evaluates 0" — UserPromptSubmit
payloads carry no ``tool_name`` and the runtime does not populate
``CLAUDE_TOOL_NAME``), so the session counter never grew and the 200k-word
checkpoint / 120k-word CONTEXT_STATE-staleness nudge could never fire. The
.ps1 sibling parsed ``tool_name`` from a payload that does not carry one.

Post-fix contract pinned here: the counter is the session's cumulative WORK
UNITS from the live transcript, scanned INCREMENTALLY by the same
``claude_token_counter.py`` the KG-update nudge uses (one home), persisted
with a per-session byte offset beside the counter file. The checkpoint fires
at 300 000 work units (the documented "~300K tokens" scale of the retired
200k-word threshold) and the staleness nudge at a 180 000-unit delta (the
retired 120k-word window).

RED on the pre-fix tree: the counter stays 0 forever, so the grow / fire /
resume assertions all fail; the loud-fail and no-transcript cases pass both
before and after (they pin the failure discipline, not the new behaviour).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "user-prompt-submit-reminder.sh"
COUNTER_PY = REPO_ROOT / "templates" / "scripts" / "claude_token_counter.py"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook")


def _entry(req: str, out_tokens: int) -> str:
    return json.dumps({
        "requestId": req,
        "message": {
            "role": "assistant",
            "usage": {"output_tokens": out_tokens},
            "content": [{"type": "text", "text": "x"}],
        },
    }) + "\n"


def _rig(tmp_path: Path) -> dict:
    """Staged hook (real ``_lib`` beside it, so the hook-relative counter
    resolution is genuinely exercised) + fixture project + redirect TMPDIR."""
    proj = tmp_path / "proj"
    scripts = proj / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(COUNTER_PY, scripts / "claude_token_counter.py")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(HOOK_SH, hooks / HOOK_SH.name)
    shutil.copytree(HOOK_SH.parent / "_lib", hooks / "_lib")
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    env = {k: v for k, v in os.environ.items() if k != "VCT_DISABLE_HOOKS"}
    env.update({
        "CLAUDE_PROJECT_DIR": str(proj),
        "TMPDIR": str(tmpdir),
        "HOME": str(tmp_path / "home"),
    })
    return {"proj": proj, "hooks": hooks, "tmpdir": tmpdir,
            "transcript": transcript, "env": env}


def _run(rig: dict, session: str) -> subprocess.CompletedProcess:
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "transcript_path": str(rig["transcript"]),
        "prompt": "continue",
    }
    return subprocess.run(
        ["bash", str(rig["hooks"] / HOOK_SH.name)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60,
        cwd=str(rig["proj"]), env=rig["env"],
    )


def _counter(rig: dict, session: str) -> int:
    log = rig["tmpdir"] / f"claude-session-{rig['proj'].name}-{session}"
    assert log.is_file(), f"no session counter at {log}"
    return int(log.read_text(encoding="utf-8").strip())


@needs_bash
def test_counter_grows_with_real_work(tmp_path: Path) -> None:
    """RED pre-fix: WORDS_THIS_TURN is hardwired 0, the counter never moves."""
    rig = _rig(tmp_path)
    rig["transcript"].write_text(
        "".join(_entry(f"r{i}", 1000) for i in range(10)), encoding="utf-8")
    r = _run(rig, "s-grow")
    assert r.returncode == 0, r.stderr[-600:]
    assert _counter(rig, "s-grow") == 10_000


@needs_bash
def test_resume_counts_only_new_work(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig["transcript"].write_text(_entry("a", 5000), encoding="utf-8")
    assert _run(rig, "s-resume").returncode == 0
    with open(rig["transcript"], "a", encoding="utf-8") as fh:
        fh.write(_entry("b", 7000))
    assert _run(rig, "s-resume").returncode == 0
    assert _counter(rig, "s-resume") == 12_000


@needs_bash
def test_checkpoint_fires_once_at_threshold(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig["transcript"].write_text(_entry("big", 350_000), encoding="utf-8")
    r = _run(rig, "s-ckpt")
    assert r.returncode == 0, r.stderr[-600:]
    assert "Session checkpoint" in r.stdout, r.stdout
    # One-shot: the marker suppresses an immediate repeat.
    r2 = _run(rig, "s-ckpt")
    assert "Session checkpoint" not in r2.stdout


@needs_bash
def test_staleness_nudge_fires_on_delta(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig["transcript"].write_text(_entry("big", 200_000), encoding="utf-8")
    r = _run(rig, "s-stale")
    assert r.returncode == 0, r.stderr[-600:]
    assert "work units since last CONTEXT_STATE update" in r.stdout, r.stdout
    # Below the checkpoint, so only the staleness nudge fired; a second run
    # with no new work is silent.
    assert "Session checkpoint" not in r.stdout
    r2 = _run(rig, "s-stale")
    assert r2.stdout.strip() == ""


@needs_bash
def test_missing_counter_module_is_loud(tmp_path: Path) -> None:
    """A transcript to count but no counter module: broken install → loud
    stderr + non-zero exit (never a silent 0)."""
    rig = _rig(tmp_path)
    os.remove(rig["proj"] / ".claude" / "scripts" / "claude_token_counter.py")
    rig["transcript"].write_text(_entry("a", 100), encoding="utf-8")
    r = _run(rig, "s-broken")
    assert r.returncode != 0
    assert "claude_token_counter" in r.stderr


@needs_bash
def test_rotated_transcript_resumes_from_zero(tmp_path: Path) -> None:
    """Fix-round 2026-10-11 (review LOW): the old ``end = res.end_offset or
    prev_off`` treated 0 as "no offset" — after a rotate-to-empty scan
    (``end_offset == 0`` is falsy) it kept the STALE offset and skipped both
    writes, so a transcript that later regrew PAST the stale byte offset
    resumed mid-file and never counted the regrown head. The nudge's
    unconditional-persist shape (0 IS a valid resume offset) is mirrored
    here: the rotate run must persist offset 0 and a fresh-basis total 0,
    and the regrown content must be counted in full."""
    rig = _rig(tmp_path)
    first = _entry("a", 5000)
    rig["transcript"].write_text(first, encoding="utf-8")
    assert _run(rig, "s-rot").returncode == 0
    assert _counter(rig, "s-rot") == 5_000
    stale_off = len(first.encode("utf-8"))
    assert stale_off > 0
    # Rotate to empty: a fresh basis — total resets, offset must persist 0.
    rig["transcript"].write_text("", encoding="utf-8")
    r = _run(rig, "s-rot")
    assert r.returncode == 0, r.stderr[-600:]
    assert _counter(rig, "s-rot") == 0, (
        "rotate-to-empty is a fresh basis; the stale total must not survive"
    )
    # Regrow PAST the stale offset: the pre-fix code resumed at stale_off
    # mid-file and missed the regrown head; post-fix it counts everything.
    regrown = "".join(_entry(f"n{i}", 4000) for i in range(20))
    assert len(regrown.encode("utf-8")) > stale_off
    rig["transcript"].write_text(regrown, encoding="utf-8")
    r = _run(rig, "s-rot")
    assert r.returncode == 0, r.stderr[-600:]
    assert _counter(rig, "s-rot") == 80_000, (
        "content regrown past the pre-fix stale offset must be counted in full"
    )


@needs_bash
def test_no_transcript_is_silent(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s-none",
               "prompt": "hi"}
    r = subprocess.run(
        ["bash", str(rig["hooks"] / HOOK_SH.name)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60,
        cwd=str(rig["proj"]), env=rig["env"],
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""
