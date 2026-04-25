#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# notify-stop.sh
# Fires on Stop event — Claude finished responding.
# Desktop notification via notify-send.
# NOTE: RL training is now handled inside the weaviate MCP server (_rl_answer_monitor),
#       not here — Stop hooks don't fire in the VS Code extension.

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")

notify-send \
    --icon=dialog-information \
    --expire-time=8000 \
    --urgency=low \
    "Claude finished — $PROJECT_NAME" \
    "Response ready" 2>/dev/null || true
