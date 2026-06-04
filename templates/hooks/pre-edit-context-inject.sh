#!/usr/bin/env bash
# Pre-edit context injection hook
# Fires BEFORE Edit tool executes — injects KG + code graph context for the file being edited
# Output goes to stdout → becomes additionalContext the LLM sees before executing the edit
#
# Constraints:
#   - Must complete in <3 seconds (timeout in settings.json)
#   - Only fires for Edit (not Write — new files have less context value)
#   - Never exit 2 (that would block the edit). Always exit 0.
#   - If searches fail or return empty → exit 0 silently

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# v0.2.21 Step 25b (in-session dedup investigation): opt-in `set -x`
# trace mode. When VCO_HOOK_TRACE=1, write the full execution trace
# to .claude/logs/preedit-trace-<session>-<ts>.log so the dedup
# codepath can be inspected post-mortem. Off by default (the trace
# is verbose and would clutter normal hook runs). Enable per-shell
# via `export VCO_HOOK_TRACE=1` in the Claude Code shell that's
# experiencing the dedup miss.
if [ "${VCO_HOOK_TRACE:-0}" = "1" ]; then
    _TRACE_FILE="${TMPDIR:-/tmp}/preedit-trace-$(date +%s%N)-$$.log"
    exec 2>>"$_TRACE_FILE"
    set -x
    echo "==== preedit hook trace start: $(date -u +%Y-%m-%dT%H:%M:%SZ) pwd=$(pwd) ====" >&2
    # Print the trace path on stdout so the operator can find it. Note:
    # PreToolUse hook stdout is discarded by the harness unless JSON-
    # shaped under `hookSpecificOutput.additionalContext`. The trace
    # path goes to stderr instead via the redirected fd 2.
    echo "[vct] preedit trace: $_TRACE_FILE" >&2
fi

# VCO-CENTRALIZED-KG: read-side delegator (PR #171 / 0.1.7).
#   Delegates KG search to claude_mcp_servers/scripts/rl_kg_search.py and
#   code-graph search to .claude/scripts/code-graph-query — both call the
#   access-aware helpers (_kg_collections_to_search /
#   code_graph_collections_to_query) in claude_mcp_servers/weaviate_mcp/
#   server.py, which read VCT_KG_ACCESS_LIST + VCT_CODE_GRAPH_ACCESS_LIST.
#   This hook does NOT query Weaviate directly. Env propagation is by
#   subprocess inheritance (no `env -i`, no `unset VCT_KG_ACCESS_LIST`).
#   See knowledge/concepts/multi-source-kg-runtime.md and
#   tests/test_kg_access_list.py for the consumer contract.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Source emit-context.sh ONLY if it exists. If the helper is missing
# (partial install, just-after-clone before _lib/ is fully populated),
# the conditional simply skips the source — the hook then runs without
# `emit_additional_context` defined; the wrapper `_emit_context_json`
# below tolerates this via `command -v`. We deliberately do NOT use
# `|| true` after the source: a syntax error inside an existing helper
# is a real bug we want surfaced, not silently swallowed.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# v0.2.29: prefer canonical $CLAUDE_PROJECT_DIR (the active workspace
# the launcher hands us — source of truth for per-project hooks). Fall
# back to SCRIPT_DIR/../.. for ad-hoc invocations (manual runs, tests)
# that don't set the env var.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Resolve a Python interpreter portably (python3 → python → py). Must run
# BEFORE the stdin-parsing step below — bare `python3` is missing on
# Windows (only python.exe / py exist) and would silently fail there.
# See audit F4 + F6.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — silent no-op (KG/codegraph injection skipped)

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1/$2) are EMPTY because $CLAUDE_TOOL_NAME etc. don't
# exist as env vars — settings.json substitutes them to "". Verified
# empirically 2026-05-08 via stdin-capture diagnostic.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
# Parse all fields we need in a single Python invocation (avoids re-parsing the
# JSON 3+ times). Outputs three lines: tool_name, session_id, tool_input as JSON.
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
    print(d.get('session_id', ''))
    print(json.dumps(d.get('tool_input', {})))
except Exception:
    print('')
    print('')
    print('{}')
" 2>/dev/null || printf '\n\n{}\n')
TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
TOOL_ARGS=$(printf '%s' "$_PARSED" | sed -n '3p')

# Only fire for Edit tool
if [[ "$TOOL_NAME" != "Edit" ]]; then
    exit 0
fi

# session_id from stdin JSON is the canonical per-conversation key. Falls
# back to "default" only if the payload is malformed (which would mean the
# hook contract itself is broken — see _PARSED above).
[ -z "$SESSION_ID" ] && SESSION_ID="default"
CACHE_BASE="$PROJECT_ROOT/.claude/state/edit_cache_${SESSION_ID}"
mkdir -p "$CACHE_BASE" 2>/dev/null || true
# v0.2.29 GC: prune per-session edit_cache_* directories older than 14 days.
# `find -mtime +14` is portable across GNU/BSD find. Best-effort — failure
# ignored. Keeps .claude/state/ bounded across heavy use.
find "$PROJECT_ROOT/.claude/state" -maxdepth 1 -type d -name "edit_cache_*" -mtime +14 -exec rm -rf {} + 2>/dev/null || true
CACHE_TTL=600  # 10 minutes in seconds

# === Dedup tracking: skip KG/codegraph nodes already injected this session ===
# State lives in the project directory (not /tmp/) so it survives reboots and
# is co-located with the session's other ephemeral state. The .claude/state/
# directory is gitignored (line 104 of the orchestrator's .gitignore), so no
# project-tree noise in git status / IDE file trees. Wiped by the PostCompact
# hook when the LLM's context is trimmed (so the dedup window matches the
# actual context window the LLM sees). v0.2.29 GC prunes ≥14d-old session
# files so the directory stays bounded across heavy use.
SEEN_DIR="$PROJECT_ROOT/.claude/state"
mkdir -p "$SEEN_DIR" 2>/dev/null
SEEN_NODES_FILE="$SEEN_DIR/seen_kg_titles_${SESSION_ID}.txt"
# v0.2.29 GC: prune session files older than 14 days. `find -mtime +14`
# is portable across GNU/BSD find. Best-effort — failure ignored.
find "$SEEN_DIR" -maxdepth 1 -type f -name "seen_kg_titles_*.txt" -mtime +14 -delete 2>/dev/null || true

# === Extract fields from TOOL_ARGS JSON ===
_extract_field() {
    local field="$1"
    [ -z "${PY:-}" ] && { echo ""; return; }
    "$PY" -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('$field', ''))
except Exception:
    print('')
" <<< "$TOOL_ARGS" 2>/dev/null || echo ""
}

FILE_PATH=$(_extract_field "file_path")
NEW_STRING=$(_extract_field "new_string")

# Need at least a file path to proceed
if [[ -z "$FILE_PATH" ]]; then
    exit 0
fi

# === Build cache key from file path ===
# Use Python's hashlib for portability — `md5sum` is GNU-only and absent
# on macOS (which has `md5 -q` instead) and minimal Linux installs.
# Without this, every Mac install would compute an empty FILE_HASH and
# every file would share the same cache key, corrupting the per-file
# cache. Falls back to a sanitized FILE_PATH if Python is unavailable
# (degrades to "no caching across paths" but stays correct).
if [ -n "${PY:-}" ]; then
    FILE_HASH=$(printf '%s' "$FILE_PATH" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" 2>/dev/null)
fi
if [ -z "${FILE_HASH:-}" ]; then
    # Sanitize path → safe filename (no slashes). Last-resort fallback.
    FILE_HASH=$(printf '%s' "$FILE_PATH" | tr '/' '_' | tr ' ' '_' | head -c 100)
fi
CACHE_DIR="$CACHE_BASE"
CACHE_FILE="$CACHE_DIR/$FILE_HASH"

mkdir -p "$CACHE_DIR" 2>/dev/null || true

# === Helper: emit context as PreToolUse JSON envelope ===
# Wraps emit_additional_context from _lib/emit-context.sh — the helper
# also gates on whitespace-only content so we don't surface empty
# system-reminder blocks when dedup suppresses every result. Pre-2026-05-08
# this hook printed plain stdout that never reached the LLM context, so
# all the KG/codegraph injection work was effectively dead. Confirmed by
# checking that no `[Pre-edit context for ...]` system-reminders ever
# appeared in real Edit-tool transcripts.
_emit_context_json() {
    # If the helper sourced (normal case), delegate. If it didn't (the
    # `_lib/emit-context.sh` file was missing at hook startup), fall
    # back to a silent no-op rather than crashing on an undefined
    # function under `set -e` / `set -u` discipline. The hook's other
    # work (dedup state, cache write) remains valid.
    if command -v emit_additional_context >/dev/null 2>&1; then
        emit_additional_context "$1" PreToolUse
    fi
}

# === Check cache (10 min TTL) ===
# Cross-OS mtime: `stat -c %Y` is GNU coreutils (Linux); `stat -f %m` is BSD
# (macOS). Try GNU first, fall back to BSD; final fallback is Python
# (find-python.sh has already resolved $PY). Without this, every macOS
# install treats the cache as expired. See audit finding F4.
#
# Cache stores RAW per-result blocks (with KG:/CODE: headers) so dedup can
# still apply on replay against the latest seen-list. The actual replay
# happens further down, after `_filter_seen` is defined — here we just
# capture the cached blob and a flag.
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

# === Auto-detect project for multi-codebase support ===
source "$SCRIPT_DIR/../scripts/detect-project.sh"
DETECTED_PROJECT=$(detect_project_for_file "$FILE_PATH" "$PROJECT_ROOT")
CODE_GRAPH_PROJECT_ARG=""
if [[ -n "$DETECTED_PROJECT" ]]; then
    CODE_GRAPH_PROJECT_ARG="--project $DETECTED_PROJECT"
fi

# === Build search query ===
BASENAME=$(basename "$FILE_PATH")
MODULE_NAME="${BASENAME%.*}"  # strip extension (e.g. retrieval_rl.py → retrieval_rl)

# First 200 chars of new_string as semantic signal
NEW_STRING_SNIPPET="${NEW_STRING:0:200}"

QUERY="$MODULE_NAME $NEW_STRING_SNIPPET"

# === Run searches in parallel to stay within 5s timeout ===
KG_TMP=$(mktemp)
CODE_TMP=$(mktemp)
# v0.2.46 post-adversarial: source shared resolver. The previous inline
# logic fell back to $PROJECT_ROOT/.venv when $VCT_INSTALL_ROOT was unset
# — that's the USER's project venv, which won't have weaviate-client +
# vco_lib, so the KG search subprocess would crash with ImportError. The
# shared helper enforces the canonical precedence and refuses to silently
# activate the user's venv. (PR-25 / v0.2.12 dual-layout history preserved
# in the helper's docstring.)
# Final fallback: if no venv resolved, the rl_kg_search subprocess below
# will short-circuit (writes empty KG_TMP) and the hook still exits 0
# without blocking the edit. Don't hard-fail.
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"

# KG search with RL reranking — same pipeline as weaviate MCP (Weaviate → RL server → top-k)
# Falls back to raw Weaviate order if RL server is unreachable.
# Returns score field; if score >= 0.6 we provide full node content below.
# v0.2.21 audit fix: pass --hook-format so each result is prefixed with
# "KG: <title>" and the dedup logic below (_filter_seen) recognises it.
# Pre-fix the hook captured untagged human output, so _filter_seen could
# never dedup KG injections across Edits within a session.
("$VENV" "$PROJECT_ROOT/claude_mcp_servers/scripts/rl_kg_search.py" "$QUERY" --limit 1 --hook-format 2>/dev/null \
    | head -40 > "$KG_TMP") &
KG_PID=$!

# Code graph search — only for code files (not markdown, yaml, etc.)
# Uses auto-detected project so edits in sibling repos query the right collections.
IS_CODE=0
if [[ "$FILE_PATH" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
    IS_CODE=1
    # v0.2.21 audit fix: pass --hook-format so results are prefixed with
    # "CODE: <full_name>" and _filter_seen below recognises them for
    # dedup. Empty result → emits one short "CODE: no-results | ..." line
    # so the model sees the hook fired AND knows the search scope.
    ("$PROJECT_ROOT/.claude/scripts/code-graph-query" search "$QUERY" $CODE_GRAPH_PROJECT_ARG --limit 2 --hook-format 2>/dev/null \
        | grep -v "$FILE_PATH" | head -20 > "$CODE_TMP") &
    CODE_PID=$!
fi

# Wait for searches (5s budget, leave 0.5s for formatting + output).
# NOTE (audit F7, P3): Git Bash on Windows occasionally hangs on this
# `wait <pid>` pattern due to signal-handling differences vs upstream bash.
# If you hit this, set VCT_DISABLE_HOOKS=1 in your shell to opt out — the
# only feature lost is the pre-edit context cache (a search-speed
# optimisation, not correctness).
wait "$KG_PID" 2>/dev/null || true
if [[ "$IS_CODE" == "1" ]]; then
    wait "$CODE_PID" 2>/dev/null || true
fi

KG_RESULT=$(cat "$KG_TMP" 2>/dev/null || true)
CODE_RESULT=""
if [[ "$IS_CODE" == "1" ]]; then
    CODE_RESULT=$(cat "$CODE_TMP" 2>/dev/null || true)
fi

rm -f "$KG_TMP" "$CODE_TMP"

# === Dedup: filter out nodes already injected this session ===
# The KG/codegraph result blocks emitted by rl_kg_search.py and
# query_code_graph have the shape:
#
#   KG: <title> | <type> | score=<n.nn> | <body...>
#   <body line 1>
#   <body line 2>
#   ...
#   (blank line separates blocks)
#
# We dedup by title (the part between "KG: " and the first " | "), and
# we suppress the ENTIRE block — title line plus its body — if the title
# has already been shown this session. The previous implementation
# extracted a "title" from every line including body content, which
# filled the seen-list with body fragments and let titles re-appear.
_filter_seen() {
    local input="$1"
    local filtered=""
    touch "$SEEN_NODES_FILE"

    # Bash-3.2 compatibility (Apple's shipped default on macOS Tier-2):
    # we deliberately avoid `declare -A` associative arrays — they
    # require bash 4+ and Apple's bash never gets updated past 3.2
    # (GPLv3 licensing). Instead the SEEN_NODES_FILE itself is the
    # source-of-truth set, queried via `grep -Fxq`. Per-emitted-block
    # we append the title to the file; that keeps in-batch dedup
    # correct (a duplicate title later in the SAME cache replay reads
    # back its earlier append). Slower per-lookup than an in-memory
    # hash but the typical batch is <20 blocks so the overhead is
    # sub-millisecond on every host.

    local current_title=""
    local current_block=""
    local current_skip=0

    _flush_block() {
        if [ -n "$current_title" ] && [ "$current_skip" = "0" ] \
            && ! grep -Fxq -- "$current_title" "$SEEN_NODES_FILE"; then
            filtered="${filtered}${current_block}"
            echo "$current_title" >> "$SEEN_NODES_FILE"
        fi
        current_title=""
        current_block=""
        current_skip=0
    }

    while IFS= read -r line; do
        # Header line starts a new block. Format: "KG: <title> | ..." or
        # "CODE: <full_name> | ..." — the first field after the prefix and
        # before the next " | " is the dedup key.
        if [[ "$line" =~ ^(KG|CODE):\ (.+)$ ]]; then
            _flush_block
            local rest="${BASH_REMATCH[2]}"
            # Defensive: strip any accidentally-doubled "KG: " / "CODE: " that
            # could slip in from a formatting transition (e.g. an old cache
            # written before the producers added their own prefix).
            rest="${rest#KG: }"
            rest="${rest#CODE: }"
            current_title="${rest%% | *}"
            # Cap to 200 chars defensively (some code-graph entity names can be long)
            current_title="${current_title:0:200}"
            current_block="${line}"$'\n'
            # If already seen, mark the block so we drop it AND its body lines.
            if grep -Fxq -- "$current_title" "$SEEN_NODES_FILE"; then
                current_skip=1
            fi
        elif [ -n "$current_title" ]; then
            # Body line for the current block — accumulate.
            current_block="${current_block}${line}"$'\n'
        else
            # Pre-amble or stray line not part of any block — pass through
            # only if it has non-whitespace content. Blank separators
            # between fully-deduped blocks would otherwise leak through
            # and make $filtered look "non-empty" to downstream HAS_KG
            # checks, producing an empty system-reminder block.
            if [[ "$line" =~ [^[:space:]] ]]; then
                filtered="${filtered}${line}"$'\n'
            fi
        fi
    done <<< "$input"
    _flush_block

    echo "$filtered"
}

# Cache replay: if we have a cache hit, dedup the cached blob against the
# current seen-list and emit. If everything in the cache is already seen,
# exit silently. The cache stores RAW per-result blocks (KG:/CODE: headers)
# so dedup state stays accurate across replays (a node seen since the
# cache was written gets filtered out here, rather than being baked into
# the cache and perma-suppressed after a /compact wipe).
#
# Ported from .ps1 sibling 2026-05-16 (PR-38, v0.2.12) — restores
# cross-OS parity with the cache layer that PR-35 confirmed had never
# existed on the .sh side. See knowledge/concepts/cross-os-hook-portability.md.
if [[ "$CACHE_HIT" == "1" ]]; then
    FILTERED_CACHE=$(_filter_seen "$CACHE_BLOB")
    # Whitespace-only filtered output → everything was already seen; silent exit.
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

# Capture raw producer output (pre-dedup) for the cache. Caching post-dedup
# would perma-suppress titles seen at write-time but eligible to re-appear
# after a /compact wipe.
KG_RAW="$KG_RESULT"
CODE_RAW="$CODE_RESULT"

if [[ -n "$KG_RESULT" ]]; then
    KG_RESULT=$(_filter_seen "$KG_RESULT")
fi
if [[ -n "$CODE_RESULT" ]]; then
    CODE_RESULT=$(_filter_seen "$CODE_RESULT")
fi

# === Only output if we found something after dedup ===
HAS_KG=$([[ -n "$KG_RESULT" ]] && echo "1" || echo "0")
HAS_CODE=$([[ -n "$CODE_RESULT" ]] && echo "1" || echo "0")

if [[ "$HAS_KG" == "0" ]] && [[ "$HAS_CODE" == "0" ]]; then
    # Still cache raw (pre-dedup) results so a later edit of the same file
    # within the TTL window can replay them through current dedup state.
    RAW_CACHE=""
    [[ -n "$KG_RAW" ]] && RAW_CACHE+="${KG_RAW}"$'\n'
    [[ -n "$CODE_RAW" ]] && RAW_CACHE+="${CODE_RAW}"$'\n'
    [[ -n "$RAW_CACHE" ]] && echo "$RAW_CACHE" > "$CACHE_FILE" 2>/dev/null || true
    exit 0
fi

# === Format output ===
# Per-result headers already carry "KG: " / "CODE: " prefixes from the
# producers (--hook-format). Don't add an extra block-level label.
OUTPUT="[Pre-edit context for ${BASENAME}]:"$'\n'$'\n'

if [[ "$HAS_KG" == "1" ]]; then
    OUTPUT+="${KG_RESULT}"$'\n'
fi

if [[ "$HAS_CODE" == "1" ]]; then
    OUTPUT+="${CODE_RESULT}"$'\n'
fi

# === Cache RAW per-result blocks (pre-dedup) so replays apply current
# dedup state. Caching post-dedup output would perma-suppress titles
# legitimately re-eligible after a /compact wipe.
RAW_CACHE=""
[[ -n "$KG_RAW" ]] && RAW_CACHE+="${KG_RAW}"$'\n'
[[ -n "$CODE_RAW" ]] && RAW_CACHE+="${CODE_RAW}"$'\n'
[[ -n "$RAW_CACHE" ]] && echo "$RAW_CACHE" > "$CACHE_FILE" 2>/dev/null || true

_emit_context_json "$OUTPUT"

exit 0
