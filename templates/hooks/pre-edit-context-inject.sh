#!/usr/bin/env bash
# Pre-edit context injection hook — THIN WRAPPER (v0.2.101 injection
# redesign, PLAN-V02101 §C3). Fires BEFORE the Edit tool executes.
#
#   stdin → hook_context_router.py edit → emit envelope
#
# The ROUTER owns the query and its discipline: the OLD semantic query
# `"<module-basename> <first 200 chars of new_string>"` is REPLACED by
# `edit_enclosing_symbols(file_path, old_string)` → an EXACT code-graph
# def+callers leg (structure, self-file callers excluded) + a KG leg keyed
# on module+symbol+path topic, with the §2.1 edit-profile floors
# (0.65 / titles-only below 0.85 / three_chunks above) applied inside
# rl_kg_search --injection-profile, plus seen-store dedupe, the per-turn
# budget and the RL retrieval event (task_type pre_edit_kg_search, passed
# by the router as --task-type; the wrapper's VCO_RL_TASK_TYPE export is
# retired with the direct producer call).
#
# What this wrapper still owns:
#   * the PER-FILE REPLAY CACHE (§C3 "keep its per-file replay cache, GC"):
#     the router's stdout is cached per edited file (TTL = the shared
#     VCO_QUERY_CACHE_TTL window, 900 s) and a fresh hit replays through the
#     CURRENT seen-state (vco_filter_seen_blocks) WITHOUT spawning the
#     router at all — the v0.2.77 warm-edit win, preserved. The cache stores
#     the router's RAW block output (pre-replay-dedup), so a /compact wipe
#     of the seen-store re-eligibilises the blocks exactly as before.
#     Soundness note: the router records the blocks it emitted in the
#     seen-store, so a replay of the SAME output filters to silence — the
#     same answer a fresh router run would give, at ~0 spawn cost.
#   * the VCO_HOOK_TRACE diagnostic gate, the state GC sweeps and the
#     emit_additional_context envelope.
#
# Constraints: never exit 2 (would block the edit); always exit 0; missing
# venv/router → silent no-op; kill switches VCT_DISABLE_HOOKS and
# VCO_INJECT_PROFILE=off checked BEFORE any spawn.
# MUST MATCH pre-edit-context-inject.ps1.

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: read-side delegator (PR #171 / 0.1.7). v0.2.101: the
#   delegate is claude_mcp_servers/scripts/hook_context_router.py, which loads
#   rl_kg_search.py / query_code_graph IN-PROCESS — both call the access-aware
#   helpers (_kg_collections_to_search / code_graph_collections_to_query) in
#   claude_mcp_servers/weaviate_mcp/server.py, which read VCT_KG_ACCESS_LIST +
#   VCT_CODE_GRAPH_ACCESS_LIST. This hook does NOT query Weaviate directly.
#   Env propagation is by subprocess inheritance (no `env -i`, no
#   `unset VCT_KG_ACCESS_LIST`). See tests/test_kg_access_list.py for the
#   consumer contract.

# v0.2.21 Step 25b (in-session dedup investigation): opt-in `set -x`
# trace mode. When VCO_HOOK_TRACE=1, write the full execution trace
# to a tmp log so the dedup codepath can be inspected post-mortem.
# Off by default. Enable per-shell via `export VCO_HOOK_TRACE=1`.
if [ "${VCO_HOOK_TRACE:-0}" = "1" ]; then
    _TRACE_FILE="${TMPDIR:-/tmp}/preedit-trace-$(date +%s%N)-$$.log"
    exec 2>>"$_TRACE_FILE"
    set -x
    echo "==== preedit hook trace start: $(date -u +%Y-%m-%dT%H:%M:%SZ) pwd=$(pwd) ====" >&2
    echo "[vct] preedit trace: $_TRACE_FILE" >&2
fi

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

# The replay path filters through the SHARED seen-store (one home); the
# router reads/writes the SAME files with the SAME key format (parity-pinned
# in tests/test_v02101_inject_gates.py).
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"
# shellcheck source=_lib/seen-store.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/seen-store.sh" ] && . "$SCRIPT_DIR/_lib/seen-store.sh"
# v0.2.101: the injection kill switch (VCO_INJECT_PROFILE=off).
# shellcheck source=_lib/inject-budget.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/inject-budget.sh" ] && . "$SCRIPT_DIR/_lib/inject-budget.sh"

if command -v vco_inject_profile_off >/dev/null 2>&1 && vco_inject_profile_off; then
    exit 0
fi

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec. The FULL
# payload is forwarded to the router untouched (it resolves session_id /
# prompt_id / transcript_path / cwd / tool_input itself — the Edit
# old_string/new_string never travel through argv, R31 privacy discipline).
# This parse extracts only the WRAPPER's fields: tool guard, session id and
# the edited file path (cache key + header).
HOOK_STDIN=$(cat 2>/dev/null || echo "")
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
    print(d.get('session_id', ''))
    print((d.get('tool_input') or {}).get('file_path', ''))
except Exception:
    print('')
    print('')
    print('')
" 2>/dev/null || printf '\n\n\n')
TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
FILE_PATH=$(printf '%s' "$_PARSED" | sed -n '3p')

# Only fire for Edit tool (the Write surface has its own wrapper:
# pre-write-context-inject.sh)
if [[ "$TOOL_NAME" != "Edit" ]]; then
    exit 0
fi
[[ -z "$FILE_PATH" ]] && exit 0

# v0.2.70 Stream E: canonical session id. SESSION_ID_RAW keeps the
# trustworthy-vs-untrustworthy distinction for the seen-store replay filter
# ("" / "default" → inject blind, no shared bucket).
if command -v vco_hook_session_id >/dev/null 2>&1; then
    SESSION_ID="$(vco_hook_session_id "$HOOK_STDIN")"
fi
SESSION_ID_RAW="$SESSION_ID"
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# V52-J Edit 4 (2026-06-09): export VCT_SESSION_ID so child processes
# inherit the session attribution (the router's KG leg reads it as layer-2
# of the telemetry 3-layer chain — the export discipline of WP-D 1).
# Skip the "default" sentinel — rather empty than a fake-key cohort.
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "default" ]; then
    export VCT_SESSION_ID="$SESSION_ID"
fi

# === Per-file replay cache (v0.2.101: stores the ROUTER's raw output) ======
CACHE_DIR="$PROJECT_ROOT/.claude/state/edit_cache_${SESSION_ID}"
mkdir -p "$CACHE_DIR" 2>/dev/null || true
# v0.2.29 GC: prune per-session edit_cache_* directories older than 14 days.
# HK-4 (v0.2.75) accepted-scatter: GC is intentionally per-hook (4 sites),
# not a shared sweeper — a shared sweeper would add a sourcing dependency
# and break the single-file-hook discipline. Each hook GCs its own state.
find "$PROJECT_ROOT/.claude/state" -maxdepth 1 -type d -name "edit_cache_*" -mtime +14 -exec rm -rf {} + 2>/dev/null || true
# P3 (v0.2.91): TTL aligned with the shared query cache (900 s default,
# VCO_QUERY_CACHE_TTL override) — the router's own kgi/cgi cache uses the
# same window, so the replay cache never outlives the semantics it replays.
# (v0.2.101) the retired _lib/query-cache.sh's _VCO_QUERY_CACHE_TTL_DEFAULT
# middle rung is gone with the lib: VCO_QUERY_CACHE_TTL is the ONE override,
# read by this replay cache and the router — no marker without a reader.
CACHE_TTL="${VCO_QUERY_CACHE_TTL:-900}"
case "$CACHE_TTL" in ''|*[!0-9]*) CACHE_TTL=900 ;; esac
[ "$CACHE_TTL" -gt 0 ] 2>/dev/null || CACHE_TTL=900

# Seen-store GC sweeps (14d) — the stores themselves are the router's and
# the shell libs' shared property; the sweeps historically live here.
SEEN_DIR="$PROJECT_ROOT/.claude/state"
mkdir -p "$SEEN_DIR" 2>/dev/null
find "$SEEN_DIR" -maxdepth 1 -type f -name "seen_inject_*.txt" -mtime +14 -delete 2>/dev/null || true
find "$SEEN_DIR" -maxdepth 1 -type f -name "seen_kg_titles_*.txt" -mtime +14 -delete 2>/dev/null || true

SEEN_INJECT_FILE=""
SEEN_READS_FILE=""
if command -v vco_seen_store_path >/dev/null 2>&1; then
    SEEN_INJECT_FILE="$(vco_seen_store_path inject "$SESSION_ID_RAW" "$PROJECT_ROOT")"
    SEEN_READS_FILE="$(vco_seen_store_path reads "$SESSION_ID_RAW" "$PROJECT_ROOT")"
fi

# === Cache key from the file path (md5; portable fallback) ===
if [ -n "${PY:-}" ]; then
    FILE_HASH=$(printf '%s' "$FILE_PATH" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" 2>/dev/null)
fi
if [ -z "${FILE_HASH:-}" ]; then
    FILE_HASH=$(printf '%s' "$FILE_PATH" | tr '/' '_' | tr ' ' '_' | head -c 100)
fi
CACHE_FILE="$CACHE_DIR/$FILE_HASH"
BASENAME=$(basename "$FILE_PATH")

# === Helper: emit context as PreToolUse JSON envelope ===
_emit_context_json() {
    if command -v emit_additional_context >/dev/null 2>&1; then
        emit_additional_context "$1" PreToolUse
    fi
}

# === Cache hit/miss observability (v0.2.77 Part 9 task 1) ===
_cache_log() {
    local _status="$1"
    local _log="$PROJECT_ROOT/.claude/state/preedit_cache_log.jsonl"
    local _ts
    _ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '')"
    if [ -f "$_log" ]; then
        local _sz
        _sz=$(wc -c < "$_log" 2>/dev/null || echo 0)
        if [ "${_sz:-0}" -gt 262144 ]; then
            mv -f "$_log" "${_log}.1" 2>/dev/null || true
        fi
    fi
    printf '{"ts":"%s","hook":"pre-edit","status":"%s","session":"%s"}\n' \
        "$_ts" "$_status" "$SESSION_ID" >> "$_log" 2>/dev/null || true
}

# === Cache replay (BEFORE any router spawn) ===
# Cross-OS mtime: GNU `stat -c %Y` first, BSD `stat -f %m`, Python fallback.
CACHE_HIT=0
CACHE_BLOB=""
if [[ -f "$CACHE_FILE" ]]; then
    CACHE_MTIME=$(stat -c '%Y' "$CACHE_FILE" 2>/dev/null \
        || stat -f '%m' "$CACHE_FILE" 2>/dev/null \
        || echo "")
    if [ -z "$CACHE_MTIME" ] && [ -n "${PY:-}" ]; then
        CACHE_MTIME=$("$PY" -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$CACHE_FILE" 2>/dev/null || echo 0)
    fi
    [ -z "$CACHE_MTIME" ] && CACHE_MTIME=0
    FILE_AGE=$(( $(date +%s) - CACHE_MTIME ))
    if [[ "$FILE_AGE" -lt "$CACHE_TTL" ]]; then
        CACHE_HIT=1
        CACHE_BLOB=$(cat "$CACHE_FILE" 2>/dev/null || true)
    fi
fi

if [[ "$CACHE_HIT" == "1" ]]; then
    _cache_log hit
    # Replay through the CURRENT seen-state. The router recorded the blocks
    # it emitted on the miss run, so a same-session replay filters to
    # silence — the same answer a fresh router run gives, with no spawn.
    # After a /compact seen-store wipe the blocks re-eligibilise (the cache
    # stores RAW pre-dedup output — the v0.2.77 invariant). A missing
    # seen-store helper (partial install) SKIPS the replay and falls
    # through to a live router run rather than replaying undeduped.
    if command -v vco_filter_seen_blocks >/dev/null 2>&1; then
        FILTERED_CACHE=$(vco_filter_seen_blocks "$CACHE_BLOB" "$SEEN_INJECT_FILE" "$SEEN_READS_FILE")
        case "$FILTERED_CACHE" in
            *[![:space:]]*)
                REPLAY_OUT="[Pre-edit context for ${BASENAME}]:"$'\n'$'\n'"${FILTERED_CACHE}"
                _emit_context_json "$REPLAY_OUT"
                exit 0
                ;;
            *)
                exit 0
                ;;
        esac
    fi
fi
_cache_log miss

# === Resolve venv + the router (orchestrator-root script, F3 discipline) ===
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
if [ -z "$VENV" ] || [ ! -f "$VENV" ]; then
    exit 0
fi
resolve_vco_orchestrator_script "$SCRIPT_DIR" "claude_mcp_servers/scripts/hook_context_router.py"
ROUTER="${VCO_ORCHESTRATOR_SCRIPT:-$PROJECT_ROOT/claude_mcp_servers/scripts/hook_context_router.py}"
if [ ! -f "$ROUTER" ]; then
    exit 0
fi
# Pin the CALLING project's identity for the router + producers.
export CLAUDE_PROJECT_DIR="$PROJECT_ROOT"

# === Run the router (single interpreter for both legs; inner budget
# VCO_INJECT_BUDGET_S). The router applies gates + dedupe + budget and
# emits the final blocks (or nothing). ===
INJECT=$(printf '%s' "$HOOK_STDIN" | "$VENV" "$ROUTER" edit 2>/dev/null || true)

# === Only output if we found something ===
case "$INJECT" in
    *[![:space:]]*)
        # Cache the RAW router output (pre-replay-dedup) so a later edit of
        # the same file within the TTL replays through CURRENT seen state.
        # §9 discipline: an EMPTY result is never cached.
        printf '%s\n' "$INJECT" > "$CACHE_FILE" 2>/dev/null || true
        OUTPUT="[Pre-edit context for ${BASENAME}]:"$'\n'$'\n'"${INJECT}"
        _emit_context_json "$OUTPUT"
        ;;
esac

exit 0
