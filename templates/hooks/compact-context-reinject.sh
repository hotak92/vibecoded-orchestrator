#!/usr/bin/env bash
# compact-context-reinject.sh
# Fires on SessionStart with matchers "compact" and "startup"
# (v0.2.102 W6-F: the ORCHESTRATOR template documented "SessionStart
# (compact/resume)" while only compact was registered, and
# diff-context-inject's "full injection handled by session-start hooks"
# comment had no backing hook at all at startup).
# Re-injects critical session context that compaction may have dropped —
# and at startup injects CONTEXT_STATE.md (in full, inside the
# per-section budgets and the shared cap below). The COMPACTION ARTEFACT
# (the pre-compact snapshot) only means anything right after a compaction:
# it is emitted only for source=compact (or an absent/unknown source — the
# pre-W6-F shape). v0.2.102 W6-K: the "resume" matcher was withdrawn (owner
# 2026-10-11 — a resumed chat already has CONTEXT_STATE in context) and the
# pruned-activity summary section was removed with precompact_prune.py.
# Stdout is added to Claude's context automatically by the CLI.
#
# LITM ordering (Lost-In-The-Middle): Claude weights start and end of context
# most heavily. Place most critical info at start AND end.
#
# Total budget: ~250 lines max. CONTEXT_STATE gets full allocation, others capped.
# Track C (v0.2.65) line-budget split: the shared CONTEXT_STATE.md rollup stays
# uncapped at the START (most critical). The per-session file (if present) is
# injected right after it with a 120-line sub-cap, so the two together plus the
# downstream caps (plan 30 + commits 8 + snapshot 50 = ~88) stay
# within the ~250-line budget.
#
# v0.2.100 WP-17: the line budget never bounded CHARACTERS, and Claude Code
# shows the model only a file path + a 2 000-character preview of hook output
# past 10 000 characters — measured: 223 of 235 runs of this hook were over
# (up to 144 891 characters), so after a compaction the model saw a preview
# of the state this hook exists to restore. The whole output now goes through
# the shared cap (_lib/emit-context.sh vco_cap_context_stream), which keeps
# the START (CONTEXT_STATE — the most critical section) and ends with a marker
# naming the files to Read for the rest.
#
# v0.2.102 W6-D: the whole-stream cap alone cut the stream INSIDE section 1
# whenever CONTEXT_STATE.md was oversized (measured: 20.7 KB), so the plan,
# commits and snapshot sections never reached the model at all. Each section
# now gets a CHARACTER budget inside the cap — the committed table
# _lib/reinject-budgets.txt (A>B>C tier B: one table both OS siblings parse)
# — and is truncated WITHIN itself with a pointer naming its file.
# CONTEXT_STATE is the flex section: it gets the cap minus the overhead minus
# what the present fixed sections actually take, so every section is always
# represented. The whole-stream cap stays as the backstop.
#
# v0.2.102 W6-D (item 1, defence in depth): a snapshot older than this
# session's compaction is NOT replayed (one line saying so instead) — see
# _vco_snapshot_is_stale below.

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably for the session_id stdin parse below (python3 →
# python → py). Same fallback chain as diff-context-inject.sh.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
# Shared session_id parse + path-safety sanitise (vco_hook_session_id). One
# implementation for all four context hooks; see _lib/session-id.sh.
# shellcheck source=_lib/session-id.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/session-id.sh"

# Project root: the ONE shell rule (_lib/project-root.sh, must match
# vco_lib/project_root.py): CLAUDE_PROJECT_DIR, else the nearest marked VCO
# project above the cwd. None -> nothing to do (no state is written).
# shellcheck source=_lib/project-root.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/project-root.sh"
PROJECT_DIR="$(vco_project_root)" || PROJECT_DIR=""
[ -n "$PROJECT_DIR" ] || exit 0

# Track C (v0.2.65): SessionStart payload carries session_id on stdin. The
# shared vco_hook_session_id parses it (json.loads → 'session_id') so we can
# locate this session's own CONTEXT_STATE file. Empty when the payload is
# absent/malformed → the per-session block below is skipped (gated on
# `[ -n "$SESSION_ID" ]`). Defense-in-depth (review C-1): the helper also
# sanitises the id to [A-Za-z0-9_-] before it reaches the file path below — a
# hostile `/` or `..` collapses to the safe sentinel "default". Must match the
# .ps1 sibling's Get-VcoHookSessionId.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
SESSION_ID=$(vco_hook_session_id "$HOOK_STDIN")

# v0.2.102 W6-F (items 5+6): which SessionStart source fired us. The
# compaction artefact section (snapshot) is emitted only
# for compactions; an absent/unknown source keeps the pre-W6-F shape
# (artefacts included) so older harness payloads behave exactly as before.
HOOK_SOURCE=""
if [ -n "${PY:-}" ] && [ -n "$HOOK_STDIN" ]; then
    HOOK_SOURCE=$(printf '%s' "$HOOK_STDIN" | "$PY" -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get("source") or "" if isinstance(d, dict) else "")
except Exception:
    print("")
' 2>/dev/null || true)
fi
_IS_COMPACT_SOURCE=0
if [ -z "$HOOK_SOURCE" ] || [ "$HOOK_SOURCE" = "compact" ]; then
    _IS_COMPACT_SOURCE=1
fi

# v0.2.104: after a compaction Claude Code itself re-injects a fresh git
# status (which carries the recent commits) — vendor page
# https://code.claude.com/docs/en/context-window, "What survives compaction".
# Repeating it here only spends the 10 000-char hook budget twice, so when
# the payload POSITIVELY says source=compact (an absent/unknown source keeps
# the full pre-v0.2.104 shape — repeating is the safe direction) the Recent
# Commits section and the snapshot's git-status block are dropped. Every
# other section (CONTEXT_STATE, the session state, the plan) is re-injected
# in full: whether the harness also re-attached one of those files is only
# flushed to the transcript around this hook's own start, so it is not
# knowable reliably here. The snapshot trimming is
# `python -m vco_lib.compact_reattach snapshot-body`; when that is
# unavailable the plain first 50 lines are replayed.
# MUST MATCH compact-context-reinject.ps1.
_IS_HARNESS_COMPACT=0
[ "$HOOK_SOURCE" = "compact" ] && _IS_HARNESS_COMPACT=1
_RC_PY=""
if [ "$_IS_HARNESS_COMPACT" = "1" ] && [ -n "${PY:-}" ]; then
    # shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
    [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/resolve-vco-venv.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/_lib/resolve-vco-venv.sh"
    VCO_VENV_PYTHON=""
    if command -v resolve_vco_venv_python >/dev/null 2>&1; then
        resolve_vco_venv_python "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" 2>/dev/null || true
    fi
    _RC_PY="${VCO_VENV_PYTHON:-$PY}"
    [ -x "$_RC_PY" ] || _RC_PY="$PY"
fi

# The shared injection cap. A missing helper is a broken install; the hook
# still re-injects (uncapped) rather than dropping the user's state.
# shellcheck source=_lib/emit-context.sh disable=SC1091
[ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
_reinject_cap() {
    if command -v vco_cap_context_stream >/dev/null 2>&1; then
        vco_cap_context_stream "The rest is on disk: Read .claude/CONTEXT_STATE.md (and .claude/context/CONTEXT_STATE_<session>.md, the active plan under .claude/context/plans/, .claude/context/pre-compact-snapshot.md)."
    else
        cat
    fi
}

# ─── v0.2.102 W6-D item 2: the per-section budget table ────────────────────
# One committed table both OS siblings parse (A>B>C tier B). A missing table
# degrades to the pre-v0.2.102 whole-stream-cap-only behaviour, with one
# stderr line (PostToolUse-style stderr never reaches the model; here it is
# SessionStart, where stderr goes to the terminal only).
RB_session_state=""; RB_plan=""; RB_commits=""; RB_snapshot=""
RB_overhead=""
_RB_FILE="$(dirname "${BASH_SOURCE[0]}")/_lib/reinject-budgets.txt"
if [ -f "$_RB_FILE" ]; then
    while read -r _rb_key _rb_val _; do
        [ -n "${_rb_val:-}" ] || continue
        case "$_rb_val" in *[!0-9]*) continue ;; esac
        # v0.2.102 W6-D review F3: rows must be POSITIVE integers. A 0 row is
        # rejected with one stderr line and its section runs uncapped — the
        # same semantics the .ps1 sibling enforces.
        if [ "$_rb_val" -eq 0 ]; then
            echo "[VCO] reinject-budgets.txt: ignoring non-positive budget row '$_rb_key $_rb_val' (rows must be positive integers; the section runs uncapped)" >&2
            continue
        fi
        case "$_rb_key" in
            session_state) RB_session_state="$_rb_val" ;;
            plan)          RB_plan="$_rb_val" ;;
            commits)       RB_commits="$_rb_val" ;;
            snapshot)      RB_snapshot="$_rb_val" ;;
            overhead)      RB_overhead="$_rb_val" ;;
        esac
    done < "$_RB_FILE"
else
    echo "[VCO broken install] .claude/hooks/_lib/reinject-budgets.txt is missing; the compact re-inject falls back to the whole-stream cap (a large CONTEXT_STATE.md can again crowd out the later sections)." >&2
fi

# _rb_alloc <text> <budget-or-empty> — chars this section takes: its length
# when under budget, the budget when over. An empty budget means "uncapped".
_rb_alloc() {
    local len=${#1} budget="${2:-}"
    [ -n "$budget" ] || budget=1000000000
    if [ "$len" -lt "$budget" ]; then printf '%s' "$len"; else printf '%s' "$budget"; fi
}

# _rb_emit <text> <budget-or-empty> <pointer> — one section under its char
# budget; the shared vco_cap_context does the cut and appends the same
# "[VCO: output cut at N of M characters ... <pointer>]" marker it uses for
# the whole stream. Sections are separated by one blank line, as before.
_rb_emit() {
    if [ -n "${2:-}" ] && command -v vco_cap_context >/dev/null 2>&1; then
        vco_cap_context "$1" "$3" "$2"
    else
        printf '%s' "$1"
    fi
    printf '\n\n'
}

# ─── v0.2.102 W6-D item 1: staleness guard for the pre-compact snapshot ───
# pre-compact-save.sh now fires on manual compacts too (PreCompact matcher
# "*"), so the snapshot should be as fresh as the compaction that triggered
# this re-inject. When it is NOT (hook failed, or a pre-migration install
# still matches "auto" only), replaying it would show days-old git status as
# if current — say so in one line instead. Fresh window: PreCompact fires,
# the compaction summary is generated (one model request), then
# SessionStart(compact) fires — minutes at most, so the default tolerance
# (30 min, VCO_SNAPSHOT_MAX_AGE seconds) is deliberately generous. Unknown
# mtime (no stat, no python) => replay, never a false "stale".
_vco_snapshot_mtime() {
    stat -c %Y "$1" 2>/dev/null && return 0
    stat -f %m "$1" 2>/dev/null && return 0
    if [ -n "${PY:-}" ]; then
        "$PY" -c 'import os,sys; print(int(os.path.getmtime(sys.argv[1])))' "$1" 2>/dev/null && return 0
    fi
    return 1
}
_vco_snapshot_is_stale() {
    local max="${VCO_SNAPSHOT_MAX_AGE:-1800}" mtime
    case "$max" in ''|*[!0-9]*) max=1800 ;; esac
    mtime=$(_vco_snapshot_mtime "$1") || return 1
    [ -n "$mtime" ] || return 1
    # Stale iff the snapshot predates (now - max). Comparing mtimes DIRECTLY
    # (rather than an integer-second age) keeps VCO_SNAPSHOT_MAX_AGE=0 meaning
    # "any existing snapshot is stale" — an age computed as $(date +%s) minus
    # an integer mtime is 0 for a just-written file and would never trip
    # `-gt 0` here (the .ps1 sibling compares fractional seconds and would,
    # so the two would diverge exactly at the documented 0 edge).
    [ "$mtime" -lt $(( $(date +%s) - max )) ]
}
_vco_snapshot_saved_line() {
    head -5 "$1" 2>/dev/null | grep -m1 '^Saved: ' || printf '(unknown save time)'
}

# ─── build the sections ────────────────────────────────────────────────────
S_ctx=""; S_sess=""; S_plan=""; S_commits=""; S_snap=""

# 1. Current task state -- START position (most critical; the FLEX section).
#    The header names WHY we fired (compact vs startup/resume) so the model
#    does not read a fresh session's injection as post-compaction recovery.
if [ -f "$PROJECT_DIR/.claude/CONTEXT_STATE.md" ]; then
    if [ "$_IS_COMPACT_SOURCE" = "1" ]; then
        S_ctx="## Current Task State (re-injected after compaction)
$(cat "$PROJECT_DIR/.claude/CONTEXT_STATE.md")"
    else
        S_ctx="## Current Task State (re-injected at session start: ${HOOK_SOURCE})
$(cat "$PROJECT_DIR/.claude/CONTEXT_STATE.md")"
    fi
fi

# 1b. Per-session task state -- START position (Track C). IF this session has
# written its own .claude/context/CONTEXT_STATE_<session_id>.md, reinject it
# right after the shared rollup — it's the most task-relevant state for THIS
# chat. Line-capped at 120 (the ~250-line total budget), then char-capped at
# its table budget. Gated on both a resolved session_id AND file existence,
# so single-session projects (no per-session file) pay nothing.
if [ -n "$SESSION_ID" ]; then
    SESSION_CONTEXT_FILE="$PROJECT_DIR/.claude/context/CONTEXT_STATE_${SESSION_ID}.md"
    if [ -f "$SESSION_CONTEXT_FILE" ]; then
        S_sess="## This Session's Task State (re-injected after compaction)
$(head -120 "$SESSION_CONTEXT_FILE")"
    fi
fi

# 2. Active plan summary -- MIDDLE position (line cap: 30)
LATEST_PLAN=$(ls -t "$PROJECT_DIR/.claude/context/plans/"*.md 2>/dev/null | grep -v archive | head -1)
if [ -n "$LATEST_PLAN" ]; then
    S_plan="## Active Plan: $(basename "$LATEST_PLAN")
$(head -30 "$LATEST_PLAN")"
fi

# 3. Recent git activity -- MIDDLE position (cap: 8 commits). Not after a
#    compaction (v0.2.104): the harness re-injects a fresh git status with
#    the recent commits itself.
if [ "$_IS_HARNESS_COMPACT" != "1" ]; then
    S_commits="## Recent Commits
$(git -C "$PROJECT_DIR" log --oneline -8 2>/dev/null || true)"
fi

# 4. Pre-compaction snapshot -- END position (line cap: 50)
#    Contains git status + recently changed files, saved by pre-compact-save.sh
#    BEFORE every compaction since v0.2.102 (manual /compact included). A
#    stale one is not replayed (see _vco_snapshot_is_stale). A compaction
#    artefact: only emitted for source=compact / unknown (W6-F).
SNAPSHOT="$PROJECT_DIR/.claude/context/pre-compact-snapshot.md"
if [ "$_IS_COMPACT_SOURCE" = "1" ] && [ -f "$SNAPSHOT" ]; then
    if _vco_snapshot_is_stale "$SNAPSHOT"; then
        S_snap="## Pre-Compaction Snapshot (stale -- NOT replayed)
Last saved: $(_vco_snapshot_saved_line "$SNAPSHOT"). This snapshot is older than the current session's compaction, so pre-compact-save.sh did not run for it (it should, on every compact since v0.2.102). Read .claude/context/pre-compact-snapshot.md only if you need it."
    else
        # v0.2.104: minus its git-status block (the harness re-injects a
        # fresh one); the plain first 50 lines when the module is unavailable.
        _SNAP_BODY=""
        if [ -n "$_RC_PY" ]; then
            _SNAP_BODY=$("$_RC_PY" -m vco_lib.compact_reattach snapshot-body --path "$SNAPSHOT" 2>/dev/null) || _SNAP_BODY=""
        fi
        [ -n "$_SNAP_BODY" ] || _SNAP_BODY=$(head -50 "$SNAPSHOT")
        S_snap="## Pre-Compaction Snapshot
$_SNAP_BODY"
    fi
fi

# ─── budget the flex section, then emit ────────────────────────────────────
# RB_flex is empty (=> section 1 uncapped) when either the shared cap helper
# or the budget table is unavailable — the pre-v0.2.102 behaviour, with the
# whole-stream cap as the only bound.
RB_MIN_SECTION=300   # a section's floor: its header plus a cut marker
RB_FLEX_FLOOR=200
_rb_floor() {
    local v="$1"
    [ "$v" -lt "$RB_MIN_SECTION" ] && v=$RB_MIN_SECTION
    printf '%s' "$v"
}
RB_flex=""
if command -v vco_cap_context >/dev/null 2>&1 && [ -n "$RB_overhead" ]; then
    _cap=$(vco_hook_context_cap)
    # v0.2.102 W6-D review F2: a user-lowered VCO_HOOK_CONTEXT_CAP below what
    # the table asks for (overhead + sum of fixed budgets + the flex floor)
    # scales the FIXED budgets down proportionally, each floored at
    # RB_MIN_SECTION chars, so every section stays represented at any cap
    # down to the sane floor 1650 (4*300 + 200 flex floor + 250 headroom;
    # v0.2.102 W6-K removed the fifth fixed section with precompact_prune.py).
    # Below that no budgeting can keep five headers, and the whole-stream
    # backstop cuts inside the later sections — documented in the table.
    _fixed_total=$(( RB_session_state + RB_plan + RB_commits + RB_snapshot ))
    if [ $(( RB_overhead + _fixed_total + RB_FLEX_FLOOR )) -gt "$_cap" ]; then
        _avail=$(( _cap - RB_overhead - RB_FLEX_FLOOR ))
        if [ "$_avail" -lt $(( RB_MIN_SECTION * 4 )) ]; then
            _avail=$(( _cap - 250 - RB_FLEX_FLOOR ))   # bare-minimum headroom
        fi
        if [ "$_avail" -gt 0 ] && [ "$_fixed_total" -gt 0 ]; then
            [ -n "$RB_session_state" ] && RB_session_state=$(_rb_floor "$(( RB_session_state * _avail / _fixed_total ))")
            [ -n "$RB_plan" ]          && RB_plan=$(_rb_floor "$(( RB_plan * _avail / _fixed_total ))")
            [ -n "$RB_commits" ]       && RB_commits=$(_rb_floor "$(( RB_commits * _avail / _fixed_total ))")
            [ -n "$RB_snapshot" ]      && RB_snapshot=$(_rb_floor "$(( RB_snapshot * _avail / _fixed_total ))")
        fi
    fi
    _fixed=0
    [ -n "$S_sess" ]    && _fixed=$(( _fixed + $(_rb_alloc "$S_sess"    "$RB_session_state") ))
    [ -n "$S_plan" ]    && _fixed=$(( _fixed + $(_rb_alloc "$S_plan"    "$RB_plan") ))
    [ -n "$S_commits" ] && _fixed=$(( _fixed + $(_rb_alloc "$S_commits" "$RB_commits") ))
    [ -n "$S_snap" ]    && _fixed=$(( _fixed + $(_rb_alloc "$S_snap"    "$RB_snapshot") ))
    RB_flex=$(( _cap - RB_overhead - _fixed ))
    # Never let the fixed sections crowd the flex section out entirely —
    # the whole-stream cap backstops the rare pathological case.
    [ "$RB_flex" -lt "$RB_FLEX_FLOOR" ] && RB_flex=$RB_FLEX_FLOOR
fi

{
[ -n "$S_ctx" ] && _rb_emit "$S_ctx" "$RB_flex" "rest is on disk: Read .claude/CONTEXT_STATE.md"
[ -n "$S_sess" ] && _rb_emit "$S_sess" "$RB_session_state" "rest is on disk: Read .claude/context/CONTEXT_STATE_${SESSION_ID}.md"
[ -n "$S_plan" ] && _rb_emit "$S_plan" "$RB_plan" "rest is on disk: Read .claude/context/plans/$(basename "$LATEST_PLAN")"
[ -n "$S_commits" ] && _rb_emit "$S_commits" "$RB_commits" "run 'git log --oneline -20' for more"
[ -n "$S_snap" ] && _rb_emit "$S_snap" "$RB_snapshot" "rest is on disk: Read .claude/context/pre-compact-snapshot.md"
} | _reinject_cap
