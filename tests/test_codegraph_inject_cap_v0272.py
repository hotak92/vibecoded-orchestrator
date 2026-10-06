# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 P6 — per-session codegraph inject VOLUME cap + end-of-turn reminder
aggregation.

Two volume fixes, tested here by driving the REAL bash helpers/hooks:

  1. Per-session inject cap (seen-store.sh counter): a marathon session that
     navigates many DISTINCT code entities injects a fresh block for each —
     unboundedly. The cap bounds the TOTAL EMITTED injections per session_id
     (VCO_CG_INJECT_CAP, default 40). v0.2.101 §C6: the Grep injection moved
     from pre-tool-use.sh's retired `_cg_inject` branch to the router-backed
     grep-context-inject.sh — the SAME counter file is enforced router-side
     (hook_context_router.py _cg_capped/_cg_record), with one BEHAVIOUR
     change: past the cap the router is SILENT (the old one-line cap note was
     a property of the retired shell branch and is retired with it). A
     different session_id gets a fresh count. Soft-fail OPEN: an unkeyable
     session runs uncapped.

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
# END-TO-END (v0.2.101 §C6 repoint): drive the REAL grep-context-inject.sh
# (router surface `grep`) N+1 times and assert the (N+1th) injection is
# suppressed + the counter records exactly N; a DIFFERENT session_id is not
# suppressed (fresh count). The CG producer is stubbed through the router's
# VCO_CG_SCRIPT seam (in-process, pinned argv) — the same enforcement point
# production uses.
# --------------------------------------------------------------------------
import sys  # noqa: E402 — sandbox venv symlink

_ROUTER_GREP_HOOK = HOOKS / "grep-context-inject.sh"

_CG_STUB = (
    "import argparse, os\n"
    "\n"
    "def main(argv=None):\n"
    "    ap = argparse.ArgumentParser()\n"
    "    ap.add_argument('subcommand')\n"
    "    ap.add_argument('kind')\n"
    "    ap.add_argument('target')\n"
    "    ap.add_argument('--hook-format', action='store_true')\n"
    "    ap.add_argument('--source-file')\n"
    "    ap.add_argument('--exclude-file')\n"
    "    ap.add_argument('--indexed-revision', action='store_true')\n"
    "    a = ap.parse_args(argv)\n"
    "    marker = os.environ.get('VCO_CAP_STUB_MARKER')\n"
    "    if marker:\n"
    "        with open(marker, 'a', encoding='utf-8') as fh:\n"
    "            fh.write('x')\n"
    "    sym = a.target\n"
    "    print(f'CODE: stub.{sym} | CodeFunction | def src/{sym}.py:1 | callers: [other]')\n"
)


def _router_grep_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """Project root with a fake VCO venv (VCT_INSTALL_ROOT) + the counting CG
    stub; the REAL router comes from VCT_ORCHESTRATOR_ROOT (this checkout)."""
    proot = tmp_path / "proj"
    (proot / ".claude" / "state").mkdir(parents=True)
    vb = proot / ".venv" / "bin"
    vb.mkdir(parents=True)
    os.symlink(sys.executable, vb / "python")
    stub = proot / "stub_cg.py"
    stub.write_text(_CG_STUB, encoding="utf-8")
    return proot, stub


def _drive_router_grep(proot: Path, stub: Path, sid: str, symbol: str,
                       cap: str = "40", marker: Path | None = None) -> str:
    payload = {"tool_name": "Grep", "session_id": sid,
               "tool_input": {"pattern": symbol}}
    env = {**os.environ,
           "CLAUDE_PROJECT_DIR": str(proot),
           "VCT_INSTALL_ROOT": str(proot),
           "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
           "VCO_CG_SCRIPT": str(stub),
           "VCO_CG_INJECT_CAP": cap}
    if marker is not None:
        env["VCO_CAP_STUB_MARKER"] = str(marker)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCO_INJECT_PROFILE", None)
    return subprocess.run(
        ["bash", str(_ROUTER_GREP_HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60,
        env=env, cwd=str(proot),
    ).stdout


def _count(marker: Path) -> int:
    return len(marker.read_text("utf-8")) if marker.exists() else 0


def test_grep_injection_capped_per_session(tmp_path: Path) -> None:
    proot, stub = _router_grep_sandbox(tmp_path)
    sid = "capsess"
    # cap=3: three distinct symbols each inject; the 4th is suppressed.
    outs = [_drive_router_grep(proot, stub, sid, f"widget_fn_{i}", cap="3")
            for i in range(4)]
    injected = [o for o in outs[:3] if "CODE: stub.widget_fn_" in o]
    assert len(injected) == 3, f"first 3 must inject; got {outs[:3]!r}"
    assert "CODE: stub." not in outs[3], (
        "the 4th injection (past cap=3) must be SUPPRESSED"
    )
    assert "additionalContext" not in outs[3], (
        "v0.2.101: past the cap the router is SILENT — the old one-line cap "
        "note was a property of pre-tool-use.sh's retired Grep branch"
    )
    # The counter file reflects exactly 3 recorded injections (SAME file the
    # retired shell branch used — the cap survived the surface move).
    cnt = proot / ".claude" / "state" / f"seen_cginject_count_{sid}.txt"
    assert cnt.read_text().strip() == "3"


def test_grep_cap_suppression_is_silent_every_time(tmp_path: Path) -> None:
    """The retired branch emitted a one-line cap note EXACTLY ONCE; the
    router's past-cap behaviour is silence on EVERY capped call. This row
    pins that new contract (and its difference from the old one)."""
    proot, stub = _router_grep_sandbox(tmp_path)
    sid = "onceSess"
    _ = [_drive_router_grep(proot, stub, sid, f"fn_{i}", cap="3") for i in range(3)]
    past1 = _drive_router_grep(proot, stub, sid, "fn_over_a", cap="3")
    past2 = _drive_router_grep(proot, stub, sid, "fn_over_b", cap="3")
    assert past1.strip() == "" and past2.strip() == "", (
        "past-cap calls must emit nothing at all (silence, not a note)"
    )


def test_grep_different_session_fresh_count(tmp_path: Path) -> None:
    proot, stub = _router_grep_sandbox(tmp_path)
    # Fill session A to the cap.
    _ = [_drive_router_grep(proot, stub, "sessA", f"a_fn_{i}", cap="3") for i in range(4)]
    # Session B starts fresh -> its FIRST injection is NOT suppressed.
    out_b = _drive_router_grep(proot, stub, "sessB", "b_fn_0", cap="3")
    assert "CODE: stub.b_fn_0" in out_b, (
        "a different session_id must have a FRESH count (not inherit A's cap)"
    )


# --------------------------------------------------------------------------
# REMINDER AGGREGATION: post-file-edit appends; stop hook aggregates + dedups.
# --------------------------------------------------------------------------
def test_post_file_edit_no_longer_emits_per_edit_reminder() -> None:
    """The per-Edit '[Code edit reminder] ... was just edited' _add_nudge must
    be GONE from post-file-edit.sh (replaced by accumulator append)."""
    body = POST_EDIT_SH.read_text(encoding="utf-8")
    route_lib = (HOOKS / "_lib" / "route-touched-path.sh").read_text(encoding="utf-8")
    for name, text in (("post-file-edit.sh", body), ("route-touched-path.sh", route_lib)):
        assert "_add_nudge \"[Code edit reminder]" not in text, (
            f"per-Edit reminder nudge must be removed from {name} "
            "(aggregation moved to Stop)"
        )
    # v0.2.95 (lane F10): the accumulator append moved into the ONE routing
    # home shared with post-bash-file-sync.sh, and the session id arrives as a
    # parameter rather than the hook-local SESSION_ID_FROM_STDIN.
    assert "edit_reminder_${session_id}.txt" in route_lib, (
        "the routing home must append touched paths to the per-turn accumulator"
    )
    assert "vco_route_touched_path" in body, (
        "post-file-edit.sh must delegate routing to _lib/route-touched-path.sh"
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
# v0.2.77 Part 9 task 4 (repointed §C6): query-cost accounting. A
# CAP-suppressed injection must NOT pay for a live CG query it then discards —
# the router short-circuits BEFORE the leg runs. The counting stub proves the
# capped call issues ZERO additional producer invocations.
# --------------------------------------------------------------------------
def test_capped_injection_issues_no_live_query(tmp_path: Path) -> None:
    """Cap=3: the first 3 DISTINCT symbols each run the producer (3 calls);
    the 4th (past-cap) call is suppressed BEFORE the query — the marker stays
    at 3, proving the capped path pays nothing for a discarded query."""
    marker = tmp_path / "cli_calls"
    proot, stub = _router_grep_sandbox(tmp_path)

    for i in range(3):
        _drive_router_grep(proot, stub, "cap-cost-sess", f"cost_fn_{i}",
                           cap="3", marker=marker)
    assert _count(marker) == 3, (
        f"first 3 distinct symbols should each query once; got {_count(marker)}")

    out4 = _drive_router_grep(proot, stub, "cap-cost-sess", "cost_fn_over",
                              cap="3", marker=marker)
    assert _count(marker) == 3, (
        "the capped (4th) injection must NOT issue a live query — the cap "
        f"short-circuits before the CG leg; producer ran {_count(marker)} times "
        "(expected 3, no extra call for the discarded injection)."
    )
    assert out4.strip() == "", "the capped call must emit nothing"


def test_repeat_symbol_served_from_cache_not_requeried(tmp_path: Path) -> None:
    """task 2 + task 4: re-Grepping the SAME symbol within TTL is served from
    the router's shared query cache — the producer is NOT re-invoked, so a
    dedup-suppressed repeat pays nothing for a live query."""
    marker = tmp_path / "cli_calls"
    proot, stub = _router_grep_sandbox(tmp_path)

    _drive_router_grep(proot, stub, "cache-repeat-sess", "repeat_sym",
                       marker=marker)
    assert _count(marker) == 1, (
        f"first query should run the producer once; got {_count(marker)}")
    # Same symbol again → served from the router's query cache, no re-query.
    _drive_router_grep(proot, stub, "cache-repeat-sess", "repeat_sym",
                       marker=marker)
    assert _count(marker) == 1, (
        f"repeat identical symbol must be served from cache (no re-query); "
        f"producer ran {_count(marker)} times (expected 1)."
    )
