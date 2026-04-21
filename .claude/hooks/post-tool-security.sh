#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# Post-tool credential scanning hook
# Fires after Write/Edit. Non-blocking (always exits 0).
# Scans for accidentally committed credentials and notifies.

EDITED_FILE="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

ALERT_LOG="$PROJECT_ROOT/.claude/logs/credential_alerts.jsonl"
mkdir -p "$(dirname "$ALERT_LOG")"

[[ -z "$EDITED_FILE" ]] && exit 0
[[ ! -f "$EDITED_FILE" ]] && exit 0

# Collect any matching credential patterns
ALERTS=()

check_pattern() {
    local label="$1"; shift
    if grep -qE "$*" "$EDITED_FILE" 2>/dev/null; then
        ALERTS+=("$label")
    fi
}

check_pattern "Anthropic/OpenAI API key"  'sk-(ant-api03|[a-zA-Z0-9]{30,})-[a-zA-Z0-9]'
check_pattern "AWS access key"            'AKIA[A-Z0-9]{16}'
check_pattern "GitHub token"             'gh[pousr]_[a-zA-Z0-9]{36}'
check_pattern "PEM private key"          'BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY'
check_pattern "Generic secret"           '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*["'"'"'][a-zA-Z0-9+/=_\-]{32,}'

if [ ${#ALERTS[@]} -gt 0 ]; then
    MSG="Possible credential in $(basename "$EDITED_FILE"): ${ALERTS[*]}"
    echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"file\":\"$EDITED_FILE\",\"patterns\":\"${ALERTS[*]}\"}" >> "$ALERT_LOG" 2>/dev/null || true
    if command -v notify-send >/dev/null 2>&1; then
        notify-send "Claude Code Security Alert" "$MSG" 2>/dev/null || true
    elif command -v osascript >/dev/null 2>&1; then
        ESCAPED_MSG=$(printf '%s' "$MSG" | sed 's/"/\\"/g')
        osascript -e "display notification \"$ESCAPED_MSG\" with title \"Claude Code Security Alert\"" 2>/dev/null || true
    fi
    echo "⚠️  $MSG"
    echo "   Review: $EDITED_FILE"
fi

exit 0
