#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-stop-reconcile.sh — SubagentStop hook that logs subagent
# completion + transcript path to .claude/logs/subagent-reconciliation.jsonl
# for post-hoc forensics. The reconciliation log lets us answer "which
# subagents ran during session X, what did each one work on, where's
# the transcript?" — useful when investigating parallel-fanout failures
# or attributing telemetry rows back to specific subagent invocations.
#
# V52-L.2 Fix 5 (v0.2.52, optional reconciliation hook). Per A5 audit,
# SubagentStop fires after a subagent finishes; the payload includes
# agent_id + agent_type (same identity surface as PreToolUse) plus the
# canonical transcript path for that subagent.
#
# Side-effect only (writes a JSONL row). Does NOT emit additionalContext
# — there's no parent context to inject into at SubagentStop time
# (subagent has already terminated). Always exits 0.
#
# Schema of the JSONL row:
#   {
#     "timestamp": "<iso8601>",
#     "session_id": "<parent session uuid>",
#     "agent_id": "<subagent uuid>",
#     "agent_type": "<agent name from frontmatter>",
#     "transcript_path": "<path to subagent transcript file>",
#     "stop_reason": "<finish_reason from payload, optional>"
#   }

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
LOG_DIR="$PROJECT_ROOT/.claude/logs"
LOG_FILE="$LOG_DIR/subagent-reconciliation.jsonl"

mkdir -p "$LOG_DIR" 2>/dev/null || exit 0

HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

# Build the JSONL row in Python so every field is properly escaped.
# Field-synonym tolerance for transcript_path mirrors how the official
# docs describe it ("agent_transcript_path" in some payloads, plain
# "transcript_path" in others). We emit whichever is present; both
# missing → empty string.
JSONL=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
from datetime import datetime, timezone
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
sys.stdout.write(json.dumps({
    'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session_id': d.get('session_id', '') or '',
    'agent_id': d.get('agent_id', '') or '',
    'agent_type': d.get('agent_type', '') or '',
    'transcript_path': (d.get('agent_transcript_path') or d.get('transcript_path') or ''),
    'stop_reason': d.get('finish_reason', '') or d.get('stop_reason', '') or '',
}))
" 2>/dev/null)

if [ -n "$JSONL" ]; then
    printf '%s\n' "$JSONL" >> "$LOG_FILE" 2>/dev/null || true
fi

exit 0
