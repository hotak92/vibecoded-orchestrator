#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: read-side delegator on the KG-suggestion path (PR #171 / 0.1.7).
#   The "KG search suggestion" branch (Edit/Write only, see section 5
#   below) calls .claude/scripts/kg-search; that wrapper invokes
#   search_knowledge.py which honors VCT_KG_ACCESS_LIST through the
#   shared helper. Other branches (SSRF guard, shell-injection scan,
#   Build Anchor, file backup, tool logging) do not touch KG/codegraph.
#   Env propagation: $(...) subshells inherit env. No centralization
#   needed in this hook itself.

# Pre-tool-use hook — Security enforcement + tool logging + KG suggestion
# Triggers: Before all tool uses
# Actions:
#   1. SSRF guard (WebFetch / fetch_page to private IPs)
#   2. Shell injection scan (network-fetch-to-shell patterns)
#   3. Tool call logging (TOUCAN dataset)
#   4. Build Anchor Protocol: track reads, block unread Write/Edit
#   5. File backup before Write/Edit on existing files
#   6. KG search suggestion before Edit/Write

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
. "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — silent no-op (logging+guards skipped)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1/$2/$3) are EMPTY because $CLAUDE_TOOL_NAME etc.
# don't exist as env vars. Without this, every toucan log entry is
# {"query":"","chosen_tool":"","tool_args":null} — silently broken.
# Verified empirically 2026-05-08 via stdin-capture diagnostic.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
TOOL_NAME=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
TOOL_ARGS=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(json.dumps(d.get('tool_input', {})))
except Exception:
    print('{}')
" 2>/dev/null || echo "{}")
USER_MESSAGE=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('user_message', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
SESSION_ID_FROM_STDIN=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

SESSION_ID="${SESSION_ID_FROM_STDIN:-${CLAUDE_SESSION_ID:-$(date +%Y%m%d_%H)}}"
SESSION_READS_FILE="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/.claude_reads_${SESSION_ID}"
BACKUP_DIR="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/.claude_backups"
SECURITY_LOG="$PROJECT_ROOT/.claude/logs/security_events.jsonl"

mkdir -p "$PROJECT_ROOT/.claude/logs"

# === HELPER: safe JSON field extraction ===
_get_field() {
    "$PY" -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('$1', ''))
except Exception:
    print('')
" <<< "$TOOL_ARGS" 2>/dev/null || echo ""
}

# === TOOL CALL LOGGING ===
# USER_MESSAGE may contain newlines, quotes, JSON metacharacters; TOOL_ARGS
# is JSON but isn't trustworthy when concatenated into another JSON string.
# Build the JSONL line through Python so every field is properly escaped.
# Audit fix 2026-05-07.
TOUCAN_LOG="$PROJECT_ROOT/.claude/logs/toucan_dataset.jsonl"
TOUCAN_JSONL=$(USER_MESSAGE_FOR_PY="$USER_MESSAGE" \
    TOOL_NAME_FOR_PY="$TOOL_NAME" \
    TOOL_ARGS_FOR_PY="$TOOL_ARGS" \
    "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
tool_args_raw = os.environ.get("TOOL_ARGS_FOR_PY", "")
try:
    tool_args_val = json.loads(tool_args_raw) if tool_args_raw else None
except (json.JSONDecodeError, TypeError):
    tool_args_val = tool_args_raw
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "query": os.environ.get("USER_MESSAGE_FOR_PY", ""),
    "chosen_tool": os.environ.get("TOOL_NAME_FOR_PY", ""),
    "tool_args": tool_args_val,
}))
' 2>/dev/null)
if [ -n "$TOUCAN_JSONL" ]; then
    printf '%s\n' "$TOUCAN_JSONL" >> "$TOUCAN_LOG" 2>/dev/null || true
fi

# === 1. SSRF GUARD ===
if [[ "$TOOL_NAME" == "WebFetch" ]] || [[ "$TOOL_NAME" == "mcp__search__fetch_page" ]]; then
    URL=$(_get_field "url")
    if [[ -n "$URL" ]]; then
        # Whitelisted local services (Weaviate, Ollama, SearXNG, Gradio)
        if echo "$URL" | grep -qE "(localhost:(8081|8082|11435|7860|8888)|127\.0\.0\.1:(8081|8082|11435|7860|8888))" 2>/dev/null; then
            : # whitelisted — fall through
        elif echo "$URL" | grep -qE "(localhost|127\.|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.|192\.168\.[0-9]+\.|169\.254\.[0-9]+\.|0\.0\.0\.0|::1)" 2>/dev/null; then
            echo "🔒 SSRF guard: '$URL' targets a private/internal network address."
            echo "   Whitelisted localhost services: Weaviate (:8081), Ollama (:11435), SearXNG (:8888), Gradio (:7860)"
            echo "   To allow additional services, add to whitelist in .claude/hooks/pre-tool-use.sh"
            echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"ssrf_blocked\",\"url\":\"$URL\"}" >> "$SECURITY_LOG" 2>/dev/null || true
            exit 2
        fi
    fi
fi

# === 2. SHELL INJECTION SCAN (Bash tool) ===
if [[ "$TOOL_NAME" == "Bash" ]]; then
    CMD=$(_get_field "command")
    INJECTION_FOUND=""

    # Pattern A: network fetch piped directly to a shell interpreter
    if echo "$CMD" | grep -qiE "(curl|wget)\s[^|]+\|\s*(ba)?sh\b" 2>/dev/null; then
        INJECTION_FOUND="network fetch piped to shell"
    fi

    # Pattern B: eval + network fetch (remote code execution)
    if [[ -z "$INJECTION_FOUND" ]] && echo "$CMD" | grep -qiE "eval\s+[\"\$\(]*(curl|wget)" 2>/dev/null; then
        INJECTION_FOUND="eval + network fetch"
    fi

    # Pattern C: base64-decoded pipe to shell
    if [[ -z "$INJECTION_FOUND" ]] && echo "$CMD" | grep -qiE "base64\s+-d.*\|\s*(ba)?sh\b" 2>/dev/null; then
        INJECTION_FOUND="base64-decoded pipe to shell"
    fi

    if [[ -n "$INJECTION_FOUND" ]]; then
        echo "🚨 Shell injection guard: detected '$INJECTION_FOUND' in Bash command."
        echo "   Blocked command preview: ${CMD:0:120}"
        echo "   If this is intentional, run the command manually in a terminal."
        echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"shell_injection_blocked\",\"pattern\":\"$INJECTION_FOUND\",\"cmd_preview\":\"${CMD:0:80}\"}" >> "$SECURITY_LOG" 2>/dev/null || true
        exit 2
    fi

    # Extended security scan via bash_security.py
    SECURITY_SCRIPT="$PROJECT_ROOT/.claude/scripts/bash_security.py"
    if [[ -f "$SECURITY_SCRIPT" ]]; then
        SECURITY_RESULT=$(echo "$CMD" | "$PY" "$SECURITY_SCRIPT" 2>&1)
        SECURITY_EXIT=$?
        if [[ "$SECURITY_EXIT" -eq 2 ]]; then
            echo "🚨 Bash security scanner blocked this command:"
            echo "   $SECURITY_RESULT"
            echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"bash_security_blocked\",\"detail\":\"${SECURITY_RESULT:0:200}\",\"cmd_preview\":\"${CMD:0:80}\"}" >> "$SECURITY_LOG" 2>/dev/null || true
            exit 2
        fi
    fi
fi

# === 3. BUILD ANCHOR PROTOCOL: Track reads ===
if [[ "$TOOL_NAME" == "Read" ]]; then
    FILE_PATH=$(_get_field "file_path")
    if [[ -n "$FILE_PATH" ]]; then
        echo "$FILE_PATH" >> "$SESSION_READS_FILE" 2>/dev/null || true
    fi
    exit 0
fi

# === 4. BUILD ANCHOR PROTOCOL + FILE BACKUP: Write/Edit checks ===
if [[ "$TOOL_NAME" == "Write" ]] || [[ "$TOOL_NAME" == "Edit" ]]; then
    FILE_PATH=$(_get_field "file_path")

    if [[ -n "$FILE_PATH" ]]; then
        if [[ -f "$FILE_PATH" ]]; then
            # Existing file: check Build Anchor
            ALREADY_READ=0
            if [[ -f "$SESSION_READS_FILE" ]]; then
                grep -qxF "$FILE_PATH" "$SESSION_READS_FILE" 2>/dev/null && ALREADY_READ=1 || true
            fi

            if [[ "$ALREADY_READ" -eq 0 ]]; then
                BASENAME=$(basename "$FILE_PATH")
                echo "⚠️  Build Anchor Protocol: '$BASENAME' has not been Read this session."
                echo "    Use the Read tool on this file before modifying it."
                echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"anchor_blocked\",\"file\":\"$FILE_PATH\"}" >> "$SECURITY_LOG" 2>/dev/null || true
                exit 2
            fi

            # Backup existing file before modification
            mkdir -p "$BACKUP_DIR"
            TIMESTAMP=$(date +%Y%m%d_%H%M%S)
            ENCODED=$(echo "$FILE_PATH" | tr '/' '__' | tr ' ' '_')
            cp "$FILE_PATH" "$BACKUP_DIR/${TIMESTAMP}__${ENCODED}" 2>/dev/null || true
            # Cleanup backups older than 24h
            find "$BACKUP_DIR" -maxdepth 1 -type f -mmin +1440 -delete 2>/dev/null || true
        fi

        # Track this file so subsequent writes don't need another Read
        echo "$FILE_PATH" >> "$SESSION_READS_FILE" 2>/dev/null || true
    fi
fi

# === 5. KG SEARCH SUGGESTION (Edit/Write only) ===
if [[ "$TOOL_NAME" != "Edit" ]] && [[ "$TOOL_NAME" != "Write" ]]; then
    exit 0
fi

CONCEPTS=$(echo "$USER_MESSAGE" | grep -oE "(caching|authentication|database|API|search|optimization|validation|testing|deployment|VRAM|quantization|inference|embedding|MCP|agent|workflow|pattern)" | head -3 | tr '\n' ' ')

if [ -n "$CONCEPTS" ]; then
    MATCHES=$("$PROJECT_ROOT/.claude/scripts/kg-search" search "$CONCEPTS" --limit 3 --files-only 2>/dev/null | grep "^knowledge/" || echo "")
    MATCH_COUNT=$(echo "$MATCHES" | grep -c "^knowledge/" 2>/dev/null || echo "0")

    if [ "$MATCH_COUNT" -ge 2 ]; then
        # PreToolUse hooks must wrap LLM-bound stdout in
        # `hookSpecificOutput.additionalContext` — plain stdout is silently
        # discarded by Claude Code's hook runner. Pre-fork-sweep this
        # branch printed plaintext that never reached the LLM. Same fix
        # class as pre-edit-context-inject (PR #168). The shared helper
        # in _lib/emit-context.sh handles the JSON envelope, the 10k char
        # cap, and (defense-in-depth) the whitespace-only-content guard.
        SUGGESTION_TEXT=$(printf '\n💡 Found %s related patterns for: %s\n%s\n\n   Search more: '\''Search knowledge graph for [concept]'\''\n' "$MATCH_COUNT" "$CONCEPTS" "$(echo "$MATCHES" | sed 's/^/   /')")
        emit_additional_context "$SUGGESTION_TEXT" PreToolUse
    fi
fi
