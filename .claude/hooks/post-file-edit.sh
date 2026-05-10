#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/kg-sync (writes to the project's own
#   KG_COLLECTION / DEVELOPMENT_COLLECTION) and code-graph-incremental.sh
#   (writes to the project's own code-graph collections via
#   analyze_code_graph.py). Writes do NOT consult VCT_KG_ACCESS_LIST or
#   VCT_CODE_GRAPH_ACCESS_LIST — those env vars are read-side only
#   (fan-out search across peer KGs). This hook is correct as-is; no
#   centralization needed. See knowledge/concepts/multi-source-kg-runtime.md.

# post-file-edit.sh — PostToolUse hook
#
# Side-effects (background):
#   1. Auto-sync knowledge/ files to Weaviate
#   2. Auto-sync docs/ files to Weaviate (development collection)
#   3. Queue code-graph incremental update for code files
#
# LLM-visible reminders (routed through additionalContext envelope):
#   4. CONTEXT_STATE.md significant-changes → expert-skill update prompt
#   5. .claude/skills or .claude/hooks edits → workflow-test prompt
#   6. Code-file edits → CONTEXT_STATE / KG capture reminder
#
# Plain stdout from PostToolUse hooks is silently dropped per the
# v2.1.x contract (see `.claude/context/hook-audit-2026-05-10.md`),
# so reminders intended for the model MUST go through
# `emit_additional_context` from `_lib/emit-context.sh`. Status
# banners ("syncing…", "done") are NOT emitted at all — they had
# no consumer.

set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
[ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python available — silent no-op

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KNOWLEDGE_ROOT="$PROJECT_ROOT/knowledge"

# Accumulate LLM-visible reminders here, emit one envelope at the end.
LLM_NUDGE=""
_add_nudge() {
    if [ -n "$LLM_NUDGE" ]; then
        LLM_NUDGE="${LLM_NUDGE}

$1"
    else
        LLM_NUDGE="$1"
    fi
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
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

[ -z "$EDITED_FILE" ] && exit 0

# 1. Auto-sync knowledge graph files (background side-effect).
if [[ "$EDITED_FILE" == "$KNOWLEDGE_ROOT"* ]]; then
    REL_PATH="${EDITED_FILE#$PROJECT_ROOT/}"
    cd "$PROJECT_ROOT"
    .claude/scripts/kg-sync "$REL_PATH" &

    EDIT_COUNT_FILE="$PROJECT_ROOT/.claude/logs/.kg_edit_count"
    mkdir -p "$PROJECT_ROOT/.claude/logs"
    if [ -f "$EDIT_COUNT_FILE" ]; then
        COUNT=$(cat "$EDIT_COUNT_FILE")
    else
        COUNT=0
    fi
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$EDIT_COUNT_FILE"
    if [ $((COUNT % 10)) -eq 0 ]; then
        (.claude/scripts/kg-duplicates --threshold 0.95 2>&1 \
            | head -c 204800 | head -200 \
            | grep -E "(✅|⚠️|📊)" || true) &
    fi
fi

# 2. Auto-sync development documentation files (background side-effect).
DOCS_DIR="$PROJECT_ROOT/docs"
if [[ "$EDITED_FILE" == "$DOCS_DIR"* ]] && [[ "$EDITED_FILE" == *.md ]]; then
    REL_PATH="${EDITED_FILE#$PROJECT_ROOT/}"
    cd "$PROJECT_ROOT"
    .claude/scripts/kg-sync "$REL_PATH" &
fi

# 3. Code file changes: incremental code graph update + LLM nudge.
if [[ "$EDITED_FILE" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
    bash "$SCRIPT_DIR/code-graph-incremental.sh" \
        "$EDITED_FILE" \
        "$PROJECT_ROOT" \
        "ClaudeOrchestrator"

    _add_nudge "[Code edit reminder] $(basename "$EDITED_FILE") was just edited.
When you're done with this work item:
- Update CONTEXT_STATE.md with what changed and what's next.
- Capture any non-obvious learnings as a KG node under knowledge/concepts/."
fi

# 4. CONTEXT_STATE.md significant-changes → expert-skill nudge.
if [[ "$EDITED_FILE" == *"CONTEXT_STATE.md" ]]; then
    EXPERT_SKILL="$PROJECT_ROOT/.claude/skills/project-experts/claude-orchestrator-expert.md"
    if [ -f "$EXPERT_SKILL" ]; then
        CHANGES=$(grep -E "(✅|##\s+(Status|Current Work|Next Steps|Knowledge Captured))" "$EDITED_FILE" | wc -l)
        if [ "$CHANGES" -gt 5 ]; then
            _add_nudge "[CONTEXT_STATE.md updated — expert-skill review] ${CHANGES} significant markers detected.
Consider updating .claude/skills/project-experts/claude-orchestrator-expert.md if any of:
  - Major milestone completed (Skills system, knowledge graph, etc.)
  - Architecture changed (MCP, agents, workflow)
  - New scripts/commands added (kg-*, wrappers)
  - Recent work section needs refresh"
        fi
    fi
fi

# 5. Workflow-system edits (Skills/Agents/hooks) → workflow-test nudge.
WORKFLOW_CHANGED=false
if [[ "$EDITED_FILE" == "$PROJECT_ROOT/.claude/skills"* ]] || \
   [[ "$EDITED_FILE" == "$PROJECT_ROOT/.claude/hooks"* ]]; then
    WORKFLOW_CHANGED=true
fi
if [ "$WORKFLOW_CHANGED" = true ]; then
    _add_nudge "[Workflow file edited] $(basename "$EDITED_FILE") was changed.
Consider:
  - Test the change in actual usage before assuming it works.
  - Update documentation if the structure changed.
  - Run /workflow-optimizer to check for optimizations.
  - Update skills-setup-guide.md if the setup process changed."
fi

# Emit accumulated nudges as a single PostToolUse envelope.
if [ -n "$LLM_NUDGE" ] && command -v emit_additional_context >/dev/null 2>&1; then
    emit_additional_context "$LLM_NUDGE" PostToolUse
fi
