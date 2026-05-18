#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# v0.2.18 (Commit 11): surface embedding-backend failure hints to Claude.
#
# Purpose
#   When EmbeddingService.for_project() fails because no backend is reachable,
#   vco_lib/embedding_service.py writes a Claude-readable hint to
#   .claude/context/EMBEDDING_FAILURES.md (and a JSONL diagnostic to
#   ~/.claude/metrics/embedding_failures.jsonl). The MD file is auto-cleared
#   the next time construction succeeds. This SessionStart hook surfaces the
#   hint (when it exists) into the current Claude Code session so the LLM
#   has immediate context about the broken state.
#
# Idempotent — safe to run on every SessionStart even when there's nothing
# to surface. Soft-fails throughout; never blocks SessionStart.
#
# The user MAY ask Claude to investigate the detailed JSONL log; the hook
# points at the absolute path so it's a one-tool-call away.

set -u

# Discover install root — same anchor convention as ensure-containers.sh.
# $CLAUDE_PROJECT_DIR is set by Claude Code; git toplevel is the fallback.
INSTALL_ROOT="${CLAUDE_PROJECT_DIR:-}"
if [ -z "$INSTALL_ROOT" ]; then
    INSTALL_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
fi
if [ -z "$INSTALL_ROOT" ]; then
    # No project context — nothing to do (running outside any VCO project).
    exit 0
fi

HINT_FILE="$INSTALL_ROOT/.claude/context/EMBEDDING_FAILURES.md"
if [ ! -f "$HINT_FILE" ]; then
    # No failure recorded — silent no-op (idempotent zero-output path).
    exit 0
fi

# Resolve $HOME via Python if the env var is unset (cmd.exe on Windows
# routing through Git Bash doesn't always expose $HOME). The JSONL path
# is informational so Claude can read it; existence is not required here.
USER_HOME="${HOME:-}"
if [ -z "$USER_HOME" ]; then
    for PY_CAND in python3 python py; do
        if command -v "$PY_CAND" >/dev/null 2>&1; then
            USER_HOME=$("$PY_CAND" -c "from pathlib import Path; print(Path.home())" 2>/dev/null || echo "")
            [ -n "$USER_HOME" ] && break
        fi
    done
fi
JSONL_PATH="${USER_HOME:-~}/.claude/metrics/embedding_failures.jsonl"

# Surface to Claude via stdout — SessionStart hook stdout is injected as a
# system-reminder. Print path + full hint body so the LLM sees both the
# pointer and the diagnostic in-context.
echo ""
echo "==================================================================="
echo "Embedding-backend failure recorded since last successful run."
echo "Claude: read this hint and (if asked) investigate the JSONL log."
echo ""
echo "Hint file:    $HINT_FILE"
echo "Detail log:   $JSONL_PATH"
echo "==================================================================="
echo ""
cat "$HINT_FILE" 2>/dev/null || echo "(hint file became unreadable between check and cat)"
echo ""
echo "==================================================================="
echo ""

exit 0
