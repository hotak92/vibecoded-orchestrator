#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Pre-tool-use hook — Security enforcement + tool logging + KG suggestion
# Triggers: Before all tool uses
# Actions:
#   1. SSRF guard (WebFetch / fetch_page to private IPs)
#   2. Shell injection scan (network-fetch-to-shell patterns)
#   3. Tool call logging (TOUCAN dataset)
#   4. Build Anchor Protocol: track reads, block unread Write/Edit
#   5. File backup before Write/Edit on existing files
#   6. KG search suggestion before Edit/Write

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TOOL_NAME="$1"
USER_MESSAGE="$2"
TOOL_ARGS="$3"

SESSION_ID="${CLAUDE_SESSION_ID:-$(date +%Y%m%d_%H)}"
SESSION_READS_FILE="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/.claude_reads_${SESSION_ID}"
BACKUP_DIR="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/.claude_backups"
SECURITY_LOG="$PROJECT_ROOT/.claude/logs/security_events.jsonl"

mkdir -p "$PROJECT_ROOT/.claude/logs"

# === HELPER: safe JSON field extraction ===
_get_field() {
    python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('$1', ''))
except Exception:
    print('')
" <<< "$TOOL_ARGS" 2>/dev/null || echo ""
}

# === TOOL CALL LOGGING ===
TOUCAN_LOG="$PROJECT_ROOT/.claude/logs/toucan_dataset.jsonl"
echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"query\":\"$USER_MESSAGE\",\"chosen_tool\":\"$TOOL_NAME\",\"tool_args\":$TOOL_ARGS}" >> "$TOUCAN_LOG" 2>/dev/null || true

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
        SECURITY_RESULT=$(echo "$CMD" | python3 "$SECURITY_SCRIPT" 2>&1)
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
        echo ""
        echo "💡 Found $MATCH_COUNT related patterns for: $CONCEPTS"
        echo "$MATCHES" | sed 's/^/   /'
        echo ""
        echo "   Search more: 'Search knowledge graph for [concept]'"
        echo ""
    fi
fi
