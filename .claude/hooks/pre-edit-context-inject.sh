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

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1/$2) are EMPTY because $CLAUDE_TOOL_NAME etc. don't
# exist as env vars — settings.json substitutes them to "". Verified
# empirically 2026-05-08 via stdin-capture diagnostic.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
# Parse all fields we need in a single Python invocation (avoids re-parsing the
# JSON 3+ times). Outputs three lines: tool_name, session_id, tool_input as JSON.
_PARSED=$(printf '%s' "$HOOK_STDIN" | python3 -c "
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Resolve a Python interpreter portably (python3 → python → py).
# Used for JSON arg parsing and cross-platform mtime; see audit F4 + F6.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"

# session_id from stdin JSON is the canonical per-conversation key. Falls
# back to "default" only if the payload is malformed (which would mean the
# hook contract itself is broken — see _PARSED above).
[ -z "$SESSION_ID" ] && SESSION_ID="default"
CACHE_BASE="${TMPDIR:-/tmp}/claude_edit_cache_${SESSION_ID}"
CACHE_TTL=600  # 10 minutes in seconds

# === Dedup tracking: skip KG/codegraph nodes already injected this session ===
# State lives in the project directory (not /tmp/) so it survives reboots and
# is co-located with the session's other ephemeral state. Wiped by the
# PostCompact hook when the LLM's context is trimmed (so the dedup window
# matches the actual context window the LLM sees).
SEEN_DIR="$PROJECT_ROOT/.claude/state"
mkdir -p "$SEEN_DIR" 2>/dev/null
SEEN_NODES_FILE="$SEEN_DIR/seen_kg_titles_${SESSION_ID}.txt"

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
FILE_HASH=$(echo "$FILE_PATH" | md5sum | cut -d' ' -f1)
CACHE_DIR="$CACHE_BASE"
CACHE_FILE="$CACHE_DIR/$FILE_HASH"

mkdir -p "$CACHE_DIR" 2>/dev/null || true

# === Helper: emit context as PreToolUse JSON envelope ===
# Plain stdout is silently discarded by Claude Code's hook runner — only
# `hookSpecificOutput.additionalContext` reaches the LLM (system reminder
# wrapper). 10k char cap per the documented contract. Pre-2026-05-08 this
# hook printed plain stdout that never reached the LLM context, so all
# the KG/codegraph injection work was effectively dead. Confirmed by
# checking that no `[Pre-edit context for ...]` system-reminders ever
# appeared in real Edit-tool transcripts.
_emit_context_json() {
    local ctx="$1"
    [ -z "$ctx" ] && return 0
    [ -z "${PY:-}" ] && return 0
    local truncated
    truncated=$(printf '%s' "$ctx" | head -c 10000)
    "$PY" -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow',
        'additionalContext': sys.stdin.read(),
    }
}))
" <<< "$truncated" 2>/dev/null || true
}

# === Check cache (10 min TTL) ===
# Use Python for mtime — `stat -c %Y` is GNU-only; macOS BSD stat needs
# `-f %m`. Without this, every macOS install treats the cache as expired.
# See audit finding F4. Falls back to 0 if Python is missing.
if [[ -f "$CACHE_FILE" ]]; then
    if [ -n "${PY:-}" ]; then
        CACHE_MTIME=$("$PY" -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$CACHE_FILE" 2>/dev/null || echo 0)
    else
        CACHE_MTIME=0
    fi
    FILE_AGE=$(( $(date +%s) - CACHE_MTIME ))
    if [[ "$FILE_AGE" -lt "$CACHE_TTL" ]]; then
        _emit_context_json "$(cat "$CACHE_FILE")"
        exit 0
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
VENV="${VCT_INSTALL_ROOT:-$PROJECT_ROOT}/claude_mcp_servers/.venv/bin/python"

# KG search with RL reranking — same pipeline as weaviate MCP (Weaviate → RL server → top-k)
# Falls back to raw Weaviate order if RL server is unreachable.
# Returns score field; if score >= 0.6 we provide full node content below.
("$VENV" "$PROJECT_ROOT/claude_mcp_servers/scripts/rl_kg_search.py" "$QUERY" --limit 1 2>/dev/null \
    | head -40 > "$KG_TMP") &
KG_PID=$!

# Code graph search — only for code files (not markdown, yaml, etc.)
# Uses auto-detected project so edits in sibling repos query the right collections.
IS_CODE=0
if [[ "$FILE_PATH" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
    IS_CODE=1
    ("$PROJECT_ROOT/.claude/scripts/code-graph-query" search "$QUERY" $CODE_GRAPH_PROJECT_ARG --limit 2 2>/dev/null \
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

    # Load existing seen titles into a hash table (one file scan, O(n)).
    declare -A seen_titles
    while IFS= read -r seen_line; do
        [ -z "$seen_line" ] && continue
        seen_titles["$seen_line"]=1
    done < "$SEEN_NODES_FILE"

    local current_title=""
    local current_block=""
    local current_skip=0

    _flush_block() {
        if [ -n "$current_title" ] && [ "$current_skip" = "0" ] && [ -z "${seen_titles[$current_title]:-}" ]; then
            filtered="${filtered}${current_block}"
            seen_titles["$current_title"]=1
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
            current_title="${rest%% | *}"
            # Cap to 200 chars defensively (some code-graph entity names can be long)
            current_title="${current_title:0:200}"
            current_block="${line}"$'\n'
            # If already seen, mark the block so we drop it AND its body lines.
            if [ -n "${seen_titles[$current_title]:-}" ]; then
                current_skip=1
            fi
        elif [ -n "$current_title" ]; then
            # Body line for the current block — accumulate.
            current_block="${current_block}${line}"$'\n'
        else
            # Pre-amble or stray line not part of any block — pass through verbatim.
            filtered="${filtered}${line}"$'\n'
        fi
    done <<< "$input"
    _flush_block

    echo "$filtered"
}

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
    exit 0
fi

# === Format output ===
OUTPUT="[Pre-edit context for ${BASENAME}]:"$'\n'$'\n'

if [[ "$HAS_KG" == "1" ]]; then
    OUTPUT+="KG: ${KG_RESULT}"$'\n'
fi

if [[ "$HAS_CODE" == "1" ]]; then
    OUTPUT+="Related code: ${CODE_RESULT}"$'\n'
fi

# === Cache the plain-text form + emit JSON envelope ===
# Cache stores the human-readable form so re-emission produces identical
# content. _emit_context_json wraps it for Claude Code's PreToolUse contract.
echo "$OUTPUT" > "$CACHE_FILE" 2>/dev/null || true

_emit_context_json "$OUTPUT"

exit 0
