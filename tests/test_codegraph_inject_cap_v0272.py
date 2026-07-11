# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 P6 — per-session codegraph inject VOLUME cap + end-of-turn reminder
aggregation.

Two volume fixes, tested here by driving the REAL bash helpers/hooks:

  1. Per-session inject cap (seen-store.sh): a marathon session that navigates
     many DISTINCT code entities injects a fresh block for each — unboundedly.
     The cap bounds the TOTAL EMITTED injections per session_id
     (VCO_CG_INJECT_CAP, default 40): once hit, `_cg_inject` stops and emits a
     one-line cap note ONCE. A different session_id gets a fresh count.
     Soft-fail OPEN: an unkeyable session runs uncapped.

  2. Reminder aggregation: the "code file was just edited -> update
     CONTEXT_STATE / capture KG" nudge fired on EVERY Edit (~15x/turn).
     post-file-edit.sh now only APPENDS the edited path to a per-turn
     accumulator; stop-codegraph-reminder.sh drains it at end-of-turn and emits
     ONE aggregated reminder (deduped basenames).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "templates" / "hooks" / "_lib"
SEEN_SH = LIB_DIR / "seen-store.sh"
SEEN_PS1 = LIB_DIR / "seen-store.ps1"
HOOKS = REPO_ROOT / "templates" / "hooks"
STOP_SH = HOOKS / "stop-codegraph-reminder.sh"
STOP_PS1 = HOOKS / "stop-codegraph-reminder.ps1"
POST_EDIT_SH = HOOKS / "post-file-edit.sh"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(not _has_bash(), reason="bash required")


def _run_seen(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run a bash snippet with seen-store.sh sourced and $PY set."""
    py = shutil.which("python3") or "python3"
    script = f'export PY="{py}"\n. "{SEEN_SH}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, cwd=str(tmp_path)
    )


# --------------------------------------------------------------------------
# .ps1 sibling parity (parity gate EXCLUDES _lib/ + the new Stop hook is a
# top-level hook, but assert both siblings + MUST-MATCH markers explicitly)
# --------------------------------------------------------------------------
def test_stop_hook_siblings_exist() -> None:
    assert STOP_SH.exists(), "stop-codegraph-reminder.sh missing"
    assert STOP_PS1.exists(), "stop-codegraph-reminder.ps1 sibling missing"


def test_cap_helpers_have_must_match_comments() -> None:
    seen = SEEN_SH.read_text(encoding="utf-8")
    assert "MUST MATCH" in seen and "seen-store.ps1" in seen
    ps1 = SEEN_PS1.read_text(encoding="utf-8")
    assert "MUST MATCH" in ps1 and "seen-store.sh" in ps1


def test_new_state_files_wiped_on_compact() -> None:
    """post-compact must reset the P6 state (count/capnote/reminder) on BOTH OSes
    so a straddling session gets a fresh bounded budget + no stale reminder."""
    sh = (HOOKS / "post-compact.sh").read_text(encoding="utf-8")
    for tok in (
        "seen_cginject_count_${SESSION_ID}.txt",
        "seen_cginject_capnote_${SESSION_ID}.txt",
        "edit_reminder_${SESSION_ID}.txt",
    ):
        assert tok in sh, f"post-compact.sh must wipe {tok}"
    ps1 = (HOOKS / "post-compact.ps1").read_text(encoding="utf-8")
    for tok in (
        "seen_cginject_count_$SessionId.txt",
        "seen_cginject_capnote_$SessionId.txt",
        "edit_reminder_$SessionId.txt",
    ):
        assert tok in ps1, f"post-compact.ps1 must wipe {tok}"


# --------------------------------------------------------------------------
# count-path resolution (mirrors the seen-store untrustworthy-session policy)
# --------------------------------------------------------------------------
def test_count_path_empty_for_untrustworthy_session(tmp_path: Path) -> None:
    r = _run_seen(
        'echo "[$(vco_cg_inject_count_path "" /proj)]"\n'
        'echo "[$(vco_cg_inject_count_path default /proj)]"\n'
        'echo "[$(vco_cg_inject_count_path abc123 /proj)]"\n',
        tmp_path,
    )
    lines = r.stdout.splitlines()
    assert lines[0] == "[]", "empty session id -> empty count path (uncapped)"
    assert lines[1] == "[]", '"default" session id -> empty count path (uncapped)'
    assert "/proj/.claude/state/seen_cginject_count_abc123.txt" in lines[2]


# --------------------------------------------------------------------------
# cap default + env override + malformed-override fallback
# --------------------------------------------------------------------------
def test_cap_default_is_40(tmp_path: Path) -> None:
    r = _run_seen('vco_cg_inject_cap', tmp_path)
    assert r.stdout.strip() == "40"


def test_cap_env_override(tmp_path: Path) -> None:
    r = _run_seen('VCO_CG_INJECT_CAP=3 vco_cg_inject_cap', tmp_path)
    assert r.stdout.strip() == "3"


def test_cap_malformed_override_falls_back_to_default(tmp_path: Path) -> None:
    for bad in ("abc", "-5", "0", ""):
        r = _run_seen(f'VCO_CG_INJECT_CAP={bad!r} vco_cg_inject_cap', tmp_path)
        assert r.stdout.strip() == "40", f"malformed cap {bad!r} must fall back to 40"


# --------------------------------------------------------------------------
# capped predicate + record: N records reach the cap, N+1th is capped
# --------------------------------------------------------------------------
def test_capped_after_n_records(tmp_path: Path) -> None:
    """With cap=3: 3 records fill the budget; the 4th check reports capped.
    The predicate is READ-ONLY (never mutates), record() is the mutator."""
    cnt = tmp_path / "cnt.txt"
    snippet = (
        f'CNT="{cnt}"\n'
        'export VCO_CG_INJECT_CAP=3\n'
        # Initially not capped.
        'vco_cg_inject_capped "$CNT" && echo "capped0" || echo "room0"\n'
        # Record 3 real injections (each under the cap at record time).
        'vco_cg_inject_record "$CNT"\n'
        'vco_cg_inject_capped "$CNT" && echo "capped1" || echo "room1"\n'
        'vco_cg_inject_record "$CNT"\n'
        'vco_cg_inject_capped "$CNT" && echo "capped2" || echo "room2"\n'
        'vco_cg_inject_record "$CNT"\n'
        # Now count==3==cap -> capped.
        'vco_cg_inject_capped "$CNT" && echo "capped3" || echo "room3"\n'
    )
    r = _run_seen(snippet, tmp_path)
    out = r.stdout
    assert "room0" in out
    assert "room1" in out
    assert "room2" in out
    assert "capped3" in out, "after cap records the predicate must report capped"
    assert cnt.read_text().strip() == "3"


def test_capped_predicate_is_readonly(tmp_path: Path) -> None:
    """vco_cg_inject_capped must NOT mutate the counter (only record does)."""
    cnt = tmp_path / "cnt.txt"
    cnt.write_text("1\n")
    _run_seen(f'export VCO_CG_INJECT_CAP=5; vco_cg_inject_capped "{cnt}"', tmp_path)
    assert cnt.read_text().strip() == "1", "capped predicate must be read-only"


def test_empty_count_file_never_capped(tmp_path: Path) -> None:
    """Untrustworthy session -> empty count path -> capped predicate returns
    'not capped' (uncapped) and record is a no-op (soft-fail OPEN)."""
    r = _run_seen(
        'vco_cg_inject_capped "" && echo "capped" || echo "uncapped"\n'
        'vco_cg_inject_record ""\n'   # must not crash
        'echo done\n',
        tmp_path,
    )
    assert "uncapped" in r.stdout
    assert "done" in r.stdout


# --------------------------------------------------------------------------
# note-once: the cap note is emitted EXACTLY ONCE per session
# --------------------------------------------------------------------------
def test_note_once_fires_exactly_once(tmp_path: Path) -> None:
    proot = tmp_path / "proj"
    (proot / ".claude" / "state").mkdir(parents=True)
    snippet = (
        f'PR="{proot}"\n'
        'vco_cg_inject_note_once s1 "$PR" && echo "first-yes" || echo "first-no"\n'
        'vco_cg_inject_note_once s1 "$PR" && echo "second-yes" || echo "second-no"\n'
        # A different session still fires once.
        'vco_cg_inject_note_once s2 "$PR" && echo "s2-yes" || echo "s2-no"\n'
    )
    r = _run_seen(snippet, tmp_path)
    assert "first-yes" in r.stdout, "note must emit the first time"
    assert "second-no" in r.stdout, "note must NOT emit the second time (same session)"
    assert "s2-yes" in r.stdout, "a different session gets its own one-shot note"


# --------------------------------------------------------------------------
# END-TO-END: drive the REAL pre-tool-use.sh Grep branch N+1 times and assert
# the (N+1)th injection is suppressed + the cap note appears; a DIFFERENT
# session_id is not suppressed (fresh count).
# --------------------------------------------------------------------------
def _sandbox_pretooluse(tmp_path: Path) -> Path:
    """Build a project root with a stub code-graph-query CLI + the _lib helpers
    the hook sources, returning the project root."""
    proot = tmp_path / "proj"
    (proot / "templates" / "hooks" / "_lib").mkdir(parents=True)
    (proot / ".claude" / "state").mkdir(parents=True)
    (proot / ".claude" / "scripts").mkdir(parents=True)
    for lib in ("session-id.sh", "seen-store.sh", "codegraph-query.sh"):
        (proot / "templates" / "hooks" / "_lib" / lib).write_bytes(
            (LIB_DIR / lib).read_bytes()
        )
    (proot / "templates" / "hooks" / "_lib" / "stderr-cap.sh").write_text("# noop\n", encoding="utf-8")
    (proot / "templates" / "hooks" / "_lib" / "find-python.sh").write_text(
        'PY="$(command -v python3)"\n', encoding="utf-8"
    )
    # emit_additional_context prints a stable marker + the payload so the test
    # can count emissions and detect the cap note.
    (proot / "templates" / "hooks" / "_lib" / "emit-context.sh").write_text(
        'emit_additional_context() { printf "EMIT<<%s>>\\n" "$1"; }\n',
        encoding="utf-8",
    )
    hook = proot / "templates" / "hooks" / "pre-tool-use.sh"
    hook.write_bytes((HOOKS / "pre-tool-use.sh").read_bytes())

    # Stub CLI: always returns a UNIQUE CODE block per query so identity-dedup
    # never suppresses (isolating the VOLUME cap as the only limiter).
    cli = proot / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        "# args: search <query> --limit N --hook-format\n"
        'q="$2"\n'
        'printf "CODE: stub.%s | CodeFunction | distance=0.10 | src=src/%s.py\\n  body for %s\\n\\n" "$q" "$q" "$q"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return proot


def _drive_grep(proot: Path, sid: str, symbol: str) -> str:
    """Run pre-tool-use.sh with a Grep payload; return combined stdout."""
    payload = {"tool_name": "Grep", "session_id": sid, "tool_input": {"pattern": symbol}}
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proot), "VCO_CG_INJECT_CAP": "3"}
    r = subprocess.run(
        ["bash", str(proot / "templates" / "hooks" / "pre-tool-use.sh")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
        env=env, cwd=str(proot),
    )
    return r.stdout


def test_pretooluse_grep_injection_capped_per_session(tmp_path: Path) -> None:
    proot = _sandbox_pretooluse(tmp_path)
    sid = "capsess"
    # cap=3: three distinct symbols each inject; the 4th is suppressed + gets
    # the cap note. Use snake_case symbols so codegraph_pattern_gate fires.
    outs = [_drive_grep(proot, sid, f"widget_fn_{i}") for i in range(4)]
    injected = [o for o in outs[:3] if "EMIT<<[Code-graph context for symbol:" in o]
    assert len(injected) == 3, f"first 3 must inject; got {outs[:3]!r}"
    assert "EMIT<<[Code-graph context for symbol:" not in outs[3], (
        "the 4th injection (past cap=3) must be SUPPRESSED"
    )
    assert "codegraph injection cap reached for this session" in outs[3], (
        "the cap note must be emitted on the first suppressed call"
    )
    # The counter file reflects exactly 3 recorded injections.
    cnt = proot / ".claude" / "state" / f"seen_cginject_count_{sid}.txt"
    assert cnt.read_text().strip() == "3"


def test_pretooluse_cap_note_emitted_once(tmp_path: Path) -> None:
    proot = _sandbox_pretooluse(tmp_path)
    sid = "onceSess"
    # 3 fills + 2 past-cap calls: the cap note appears on exactly ONE of them.
    _ = [_drive_grep(proot, sid, f"fn_{i}") for i in range(3)]
    past1 = _drive_grep(proot, sid, "fn_over_a")
    past2 = _drive_grep(proot, sid, "fn_over_b")
    note = "codegraph injection cap reached for this session"
    assert (note in past1) != (note in past2), (
        "the cap note must be emitted EXACTLY ONCE across repeated capped calls"
    )


def test_pretooluse_different_session_fresh_count(tmp_path: Path) -> None:
    proot = _sandbox_pretooluse(tmp_path)
    # Fill session A to the cap.
    _ = [_drive_grep(proot, "sessA", f"a_fn_{i}") for i in range(4)]
    # Session B starts fresh -> its FIRST injection is NOT suppressed.
    out_b = _drive_grep(proot, "sessB", "b_fn_0")
    assert "EMIT<<[Code-graph context for symbol:" in out_b, (
        "a different session_id must have a FRESH count (not inherit A's cap)"
    )


# --------------------------------------------------------------------------
# REMINDER AGGREGATION: post-file-edit appends; stop hook aggregates + dedups.
# --------------------------------------------------------------------------
def test_post_file_edit_no_longer_emits_per_edit_reminder() -> None:
    """The per-Edit '[Code edit reminder] ... was just edited' _add_nudge must
    be GONE from post-file-edit.sh (replaced by accumulator append)."""
    body = POST_EDIT_SH.read_text(encoding="utf-8")
    assert "_add_nudge \"[Code edit reminder]" not in body, (
        "per-Edit reminder nudge must be removed (aggregation moved to Stop)"
    )
    assert "edit_reminder_${SESSION_ID_FROM_STDIN}.txt" in body, (
        "post-file-edit.sh must append edited paths to the per-turn accumulator"
    )


def test_stop_hook_aggregates_and_dedups(tmp_path: Path) -> None:
    """Drive stop-codegraph-reminder.sh against a hand-primed accumulator with a
    DUPLICATE path -> exactly ONE reminder listing each basename once; the
    accumulator is drained (removed) afterward."""
    proot = tmp_path / "proj"
    (proot / ".claude" / "state").mkdir(parents=True)
    # Copy the hook + the _lib helpers it sources into a runnable layout.
    hookdir = proot / "hooks"
    (hookdir / "_lib").mkdir(parents=True)
    (hookdir / "stop-codegraph-reminder.sh").write_bytes(STOP_SH.read_bytes())
    (hookdir / "_lib" / "stderr-cap.sh").write_text("# noop\n", encoding="utf-8")
    (hookdir / "_lib" / "find-python.sh").write_text(
        'PY="$(command -v python3)"\n', encoding="utf-8"
    )
    (hookdir / "_lib" / "emit-context.sh").write_text(
        'emit_additional_context() { printf "EMIT<<%s>>\\n" "$1"; }\n',
        encoding="utf-8",
    )
    sid = "turnSess"
    accum = proot / ".claude" / "state" / f"edit_reminder_{sid}.txt"
    accum.write_text(
        f"{proot}/src/alpha.py\n{proot}/src/beta.py\n{proot}/src/alpha.py\n",
        encoding="utf-8",
    )
    payload = {"session_id": sid}
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proot)}
    r = subprocess.run(
        ["bash", str(hookdir / "stop-codegraph-reminder.sh")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
        env=env, cwd=str(proot),
    )
    out = r.stdout
    assert out.count("EMIT<<") == 1, f"exactly ONE aggregated reminder expected; got {out!r}"
    assert "alpha.py" in out and "beta.py" in out, "both edited files must be listed"
    assert out.count("alpha.py") == 1, "a re-edited file must be listed ONCE (deduped)"
    assert "2 code file(s) edited this turn" in out, "count must reflect deduped set"
    assert not accum.exists(), "the accumulator must be drained (removed) after emit"


def test_stop_hook_noop_without_accumulator(tmp_path: Path) -> None:
    """No accumulator for the session -> the Stop hook emits nothing + exits 0."""
    proot = tmp_path / "proj"
    (proot / ".claude" / "state").mkdir(parents=True)
    hookdir = proot / "hooks"
    (hookdir / "_lib").mkdir(parents=True)
    (hookdir / "stop-codegraph-reminder.sh").write_bytes(STOP_SH.read_bytes())
    (hookdir / "_lib" / "stderr-cap.sh").write_text("# noop\n", encoding="utf-8")
    (hookdir / "_lib" / "find-python.sh").write_text(
        'PY="$(command -v python3)"\n', encoding="utf-8"
    )
    (hookdir / "_lib" / "emit-context.sh").write_text(
        'emit_additional_context() { printf "EMIT<<%s>>\\n" "$1"; }\n', encoding="utf-8"
    )
    payload = {"session_id": "nofile"}
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proot)}
    r = subprocess.run(
        ["bash", str(hookdir / "stop-codegraph-reminder.sh")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
        env=env, cwd=str(proot),
    )
    assert r.returncode == 0
    assert "EMIT<<" not in r.stdout


# --------------------------------------------------------------------------
# v0.2.77 Part 9 task 4: query-cost accounting. A CAP-suppressed injection must
# NOT pay for a live code-graph query it then discards — the cap short-circuits
# BEFORE codegraph_query_block is invoked. This test uses a COUNTING CLI to
# prove the capped call issues ZERO additional CLI invocations.
# --------------------------------------------------------------------------
def _sandbox_counting_cli(tmp_path: Path, marker: Path) -> Path:
    """Like _sandbox_pretooluse but the stub CLI appends to `marker` on every
    invocation and the query-cache lib is present (production shape)."""
    proot = tmp_path / "proj"
    (proot / "templates" / "hooks" / "_lib").mkdir(parents=True)
    (proot / ".claude" / "state").mkdir(parents=True)
    (proot / ".claude" / "scripts").mkdir(parents=True)
    for lib in ("session-id.sh", "seen-store.sh", "codegraph-query.sh", "query-cache.sh"):
        (proot / "templates" / "hooks" / "_lib" / lib).write_bytes(
            (LIB_DIR / lib).read_bytes()
        )
    (proot / "templates" / "hooks" / "_lib" / "stderr-cap.sh").write_text("# noop\n", encoding="utf-8")
    (proot / "templates" / "hooks" / "_lib" / "find-python.sh").write_text(
        'PY="$(command -v python3)"\n', encoding="utf-8"
    )
    (proot / "templates" / "hooks" / "_lib" / "emit-context.sh").write_text(
        'emit_additional_context() { printf "EMIT<<%s>>\\n" "$1"; }\n', encoding="utf-8"
    )
    (proot / "templates" / "hooks" / "pre-tool-use.sh").write_bytes(
        (HOOKS / "pre-tool-use.sh").read_bytes()
    )
    cli = proot / ".claude" / "scripts" / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f'printf x >> "{marker}"\n'
        'q="$2"\n'
        'printf "CODE: stub.%s | CodeFunction | distance=0.10 | src=src/%s.py\\n  body for %s\\n\\n" "$q" "$q" "$q"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return proot


def test_capped_injection_issues_no_live_query(tmp_path: Path) -> None:
    """Cap=3: the first 3 DISTINCT symbols each run the CLI (3 calls); the 4th
    (past-cap) call is suppressed BEFORE the query — so the CLI count stays at
    3, proving the capped path pays nothing for a discarded query."""
    marker = tmp_path / "cli_calls"
    proot = _sandbox_counting_cli(tmp_path, marker)
    sid = "cap-cost-sess"

    def _grep(symbol: str) -> str:
        payload = {"tool_name": "Grep", "session_id": sid, "tool_input": {"pattern": symbol}}
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proot), "VCO_CG_INJECT_CAP": "3"}
        return subprocess.run(
            ["bash", str(proot / "templates" / "hooks" / "pre-tool-use.sh")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
            env=env, cwd=str(proot),
        ).stdout

    # 3 distinct symbols fill the cap; each runs the CLI once.
    for i in range(3):
        _grep(f"cost_fn_{i}")
    calls_at_cap = len(marker.read_text("utf-8")) if marker.exists() else 0
    assert calls_at_cap == 3, f"first 3 distinct symbols should each query once; got {calls_at_cap}"

    # 4th DISTINCT symbol is past the cap → suppressed BEFORE the query.
    out4 = _grep("cost_fn_over")
    calls_after = len(marker.read_text("utf-8")) if marker.exists() else 0
    assert calls_after == 3, (
        "the capped (4th) injection must NOT issue a live query — the cap "
        f"short-circuits before codegraph_query_block; CLI ran {calls_after} times "
        "(expected 3, no extra call for the discarded injection)."
    )
    assert "codegraph injection cap reached" in out4, (
        "the capped call should still emit the one-shot cap note"
    )


def test_repeat_symbol_served_from_cache_not_requeried(tmp_path: Path) -> None:
    """task 2 + task 4: re-Grepping the SAME symbol within TTL is served from
    the shared cache — the CLI is NOT re-invoked, so a dedup-suppressed repeat
    pays nothing for a live query."""
    marker = tmp_path / "cli_calls"
    proot = _sandbox_counting_cli(tmp_path, marker)
    sid = "cache-repeat-sess"

    def _grep(symbol: str) -> str:
        payload = {"tool_name": "Grep", "session_id": sid, "tool_input": {"pattern": symbol}}
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proot), "VCO_CG_INJECT_CAP": "40"}
        return subprocess.run(
            ["bash", str(proot / "templates" / "hooks" / "pre-tool-use.sh")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
            env=env, cwd=str(proot),
        ).stdout

    _grep("repeat_sym")
    first = len(marker.read_text("utf-8")) if marker.exists() else 0
    assert first == 1, f"first query should run the CLI once; got {first}"
    # Same symbol again → served from the shared query-cache, no new CLI call.
    _grep("repeat_sym")
    second = len(marker.read_text("utf-8")) if marker.exists() else 0
    assert second == 1, (
        f"repeat identical symbol must be served from cache (no re-query); "
        f"CLI ran {second} times (expected 1)."
    )
