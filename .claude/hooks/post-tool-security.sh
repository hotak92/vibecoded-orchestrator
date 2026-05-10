#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Post-tool credential scanning hook
# Fires after Write/Edit. Non-blocking (always exits 0).
# Scans for accidentally committed credentials and notifies.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Resolve a Python interpreter portably (python3 → python → py). Must run
# BEFORE the stdin-parsing step below because Windows ships python.exe / py
# but not python3 — bare `python3` would silently fail on Windows.
# See audit finding F6, 2026-04-30. _lib/find-python.sh sets $PY.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — silent no-op (security log skipped)

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1) are EMPTY because $CLAUDE_TOOL_ARG_FILE_PATH and
# similar env vars don't exist — settings.json substitutes to "". Verified
# 2026-05-08 via stdin-capture diagnostic.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
EDITED_FILE=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {})
    print(ti.get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

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
    # Build the JSONL line via Python so EDITED_FILE / ALERTS[] / patterns
    # are properly JSON-escaped. Audit fix 2026-05-07.
    JSONL=$(EDITED_FILE_FOR_PY="$EDITED_FILE" PATTERNS_FOR_PY="${ALERTS[*]}" "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "file": os.environ.get("EDITED_FILE_FOR_PY", ""),
    "patterns": os.environ.get("PATTERNS_FOR_PY", ""),
}))
' 2>/dev/null)
    if [ -n "$JSONL" ]; then
        printf '%s\n' "$JSONL" >> "$ALERT_LOG" 2>/dev/null || true
    fi
    # Cross-platform notification (Linux/macOS/Windows). See audit F2.
    if [ -n "${PY:-}" ] && [ -f "$PROJECT_ROOT/.claude/scripts/notify.py" ]; then
        "$PY" "$PROJECT_ROOT/.claude/scripts/notify.py" \
            "Claude Code Security Alert" "$MSG" \
            --urgency critical --icon dialog-warning 2>/dev/null || true
    fi
    echo "⚠️  $MSG"
    echo "   Review: $EDITED_FILE"
fi

exit 0
