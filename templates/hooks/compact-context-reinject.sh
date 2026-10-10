#!/usr/bin/env bash
# compact-context-reinject.sh
# Fires on SessionStart with matcher "compact"
# Re-injects critical session context that compaction may have dropped.
# Stdout is added to Claude's context automatically by the CLI.
#
# LITM ordering (Lost-In-The-Middle): Claude weights start and end of context
# most heavily. Place most critical info at start AND end.
#
# Total budget: ~250 lines max. CONTEXT_STATE gets full allocation, others capped.
# Track C (v0.2.65) line-budget split: the shared CONTEXT_STATE.md rollup stays
# uncapped at the START (most critical). The per-session file (if present) is
# injected right after it with a 120-line sub-cap, so the two together plus the
# downstream caps (plan 30 + commits 8 + snapshot 50 + pruned 30 = ~118) stay
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

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

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
RB_pruned=""; RB_overhead=""
_RB_FILE="$(dirname "${BASH_SOURCE[0]}")/_lib/reinject-budgets.txt"
if [ -f "$_RB_FILE" ]; then
    while read -r _rb_key _rb_val _; do
        [ -n "${_rb_val:-}" ] || continue
        case "$_rb_val" in *[!0-9]*) continue ;; esac
        case "$_rb_key" in
            session_state) RB_session_state="$_rb_val" ;;
            plan)          RB_plan="$_rb_val" ;;
            commits)       RB_commits="$_rb_val" ;;
            snapshot)      RB_snapshot="$_rb_val" ;;
            pruned)        RB_pruned="$_rb_val" ;;
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
    [ $(( $(date +%s) - mtime )) -gt "$max" ]
}
_vco_snapshot_saved_line() {
    head -5 "$1" 2>/dev/null | grep -m1 '^Saved: ' || printf '(unknown save time)'
}

# ─── build the sections ────────────────────────────────────────────────────
S_ctx=""; S_sess=""; S_plan=""; S_commits=""; S_snap=""; S_pruned=""

# 1. Current task state -- START position (most critical; the FLEX section)
if [ -f "$PROJECT_DIR/.claude/CONTEXT_STATE.md" ]; then
    S_ctx="## Current Task State (re-injected after compaction)
$(cat "$PROJECT_DIR/.claude/CONTEXT_STATE.md")"
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

# 3. Recent git activity -- MIDDLE position (cap: 8 commits)
S_commits="## Recent Commits
$(git -C "$PROJECT_DIR" log --oneline -8 2>/dev/null || true)"

# 4. Pre-compaction snapshot -- END position (line cap: 50)
#    Contains git status + recently changed files, saved by pre-compact-save.sh
#    BEFORE every compaction since v0.2.102 (manual /compact included). A
#    stale one is not replayed (see _vco_snapshot_is_stale).
SNAPSHOT="$PROJECT_DIR/.claude/context/pre-compact-snapshot.md"
if [ -f "$SNAPSHOT" ]; then
    if _vco_snapshot_is_stale "$SNAPSHOT"; then
        S_snap="## Pre-Compaction Snapshot (stale -- NOT replayed)
Last saved: $(_vco_snapshot_saved_line "$SNAPSHOT"). This snapshot is older than the current session's compaction, so pre-compact-save.sh did not run for it (it should, on every compact since v0.2.102). Read .claude/context/pre-compact-snapshot.md only if you need it."
    else
        S_snap="## Pre-Compaction Snapshot
$(head -50 "$SNAPSHOT")"
    fi
fi

# 5. Pruned activity summary -- END position (line cap: 30)
#    Frequently-edited paths + bash commands run repeatedly. Generated by
#    .claude/scripts/precompact_prune.py via pre-compact-save.sh.
#    Truncated AFTER re-injection so a non-compact SessionStart doesn't
#    re-show last compact's summary as stale state.
PRUNED="$PROJECT_DIR/.claude/context/pruned-context-summary.md"
if [ -s "$PRUNED" ]; then
    S_pruned="$(head -30 "$PRUNED")"
fi

# ─── budget the flex section, then emit ────────────────────────────────────
# RB_flex is empty (=> section 1 uncapped) when either the shared cap helper
# or the budget table is unavailable — the pre-v0.2.102 behaviour, with the
# whole-stream cap as the only bound.
RB_flex=""
if command -v vco_cap_context >/dev/null 2>&1 && [ -n "$RB_overhead" ]; then
    _fixed=0
    [ -n "$S_sess" ]    && _fixed=$(( _fixed + $(_rb_alloc "$S_sess"    "$RB_session_state") ))
    [ -n "$S_plan" ]    && _fixed=$(( _fixed + $(_rb_alloc "$S_plan"    "$RB_plan") ))
    [ -n "$S_commits" ] && _fixed=$(( _fixed + $(_rb_alloc "$S_commits" "$RB_commits") ))
    [ -n "$S_snap" ]    && _fixed=$(( _fixed + $(_rb_alloc "$S_snap"    "$RB_snapshot") ))
    [ -n "$S_pruned" ]  && _fixed=$(( _fixed + $(_rb_alloc "$S_pruned"  "$RB_pruned") ))
    RB_flex=$(( $(vco_hook_context_cap) - RB_overhead - _fixed ))
    # Never let the fixed sections crowd the flex section out entirely —
    # the whole-stream cap backstops the rare pathological case.
    [ "$RB_flex" -lt 200 ] && RB_flex=200
fi

{
[ -n "$S_ctx" ] && _rb_emit "$S_ctx" "$RB_flex" "rest is on disk: Read .claude/CONTEXT_STATE.md"
[ -n "$S_sess" ] && _rb_emit "$S_sess" "$RB_session_state" "rest is on disk: Read .claude/context/CONTEXT_STATE_${SESSION_ID}.md"
[ -n "$S_plan" ] && _rb_emit "$S_plan" "$RB_plan" "rest is on disk: Read .claude/context/plans/$(basename "$LATEST_PLAN")"
[ -n "$S_commits" ] && _rb_emit "$S_commits" "$RB_commits" "run 'git log --oneline -20' for more"
[ -n "$S_snap" ] && _rb_emit "$S_snap" "$RB_snapshot" "rest is on disk: Read .claude/context/pre-compact-snapshot.md"
[ -n "$S_pruned" ] && _rb_emit "$S_pruned" "$RB_pruned" "(pruned activity summary truncated)"
# Truncate the pruned summary AFTER emitting it (unchanged behaviour).
[ -n "$S_pruned" ] && : > "$PRUNED"
} | _reinject_cap
