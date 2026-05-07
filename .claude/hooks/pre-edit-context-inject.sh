#!/bin/bash
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

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

TOOL_NAME="$1"
TOOL_ARGS="$2"

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

SESSION_ID="${CLAUDE_SESSION_ID:-default}"
CACHE_BASE="${TMPDIR:-/tmp}/claude_edit_cache_${SESSION_ID}"
CACHE_TTL=600  # 10 minutes in seconds

# === Dedup tracking: skip KG/codegraph nodes already injected this session ===
SEEN_NODES_FILE="${TMPDIR:-/tmp}/claude_seen_nodes_${SESSION_ID}"
COMPACT_FLAG="${TMPDIR:-/tmp}/claude_ctx_snapshots/compact_flag_${SESSION_ID}"
# Reset seen nodes on compaction
if [ -f "$COMPACT_FLAG" ] && [ -f "$SEEN_NODES_FILE" ]; then
    rm -f "$SEEN_NODES_FILE"
fi

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
        cat "$CACHE_FILE"
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
# KG result lines start with "Title | type | score=..." — extract title (first field before |)
# Code graph lines vary but typically have the entity name as first field
#
# Performance: load SEEN_NODES_FILE once into an associative array rather
# than per-line grep. Output semantics identical. Audit fix 2026-05-07.
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

    while IFS= read -r line; do
        [ -z "$line" ] && continue
        # Extract title: first field before " | " (KG) or first meaningful token (code graph)
        local title
        title=$(echo "$line" | sed 's/ | .*//' | head -c 100)
        if [ -n "$title" ] && [ -z "${seen_titles[$title]:-}" ]; then
            filtered="${filtered}${line}"$'\n'
            seen_titles["$title"]=1
            echo "$title" >> "$SEEN_NODES_FILE"
        fi
    done <<< "$input"
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

# === Cache the result ===
echo "$OUTPUT" > "$CACHE_FILE" 2>/dev/null || true

# === Output to stdout (becomes additionalContext) ===
echo "$OUTPUT"

exit 0
