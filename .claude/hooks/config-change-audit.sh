#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# config-change-audit.sh
# Fires on ConfigChange event — logs all settings changes for audit trail.
# Background: does NOT write to stdout (not injected into context).

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
LOG_FILE="$PROJECT_DIR/.claude/logs/config_changes.jsonl"

mkdir -p "$(dirname "$LOG_FILE")"

echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"config_change\",\"tool\":\"${CLAUDE_TOOL_NAME:-unknown}\",\"args\":${CLAUDE_TOOL_ARGS:-{}}}" >> "$LOG_FILE" 2>/dev/null || true
