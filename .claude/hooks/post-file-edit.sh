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

# Claude Orchestrator post-file-edit hook
#
# Actions:
# 1. Auto-sync knowledge/ files to Weaviate (knowledge graph)
# 2. Auto-sync docs/ files to Weaviate (development collection)
# 3. Queue code graph updates for code files
# 4. Remind to update project expert when CONTEXT_STATE.md changes significantly
# 5. Suggest workflow optimization when Skills/Agents/hooks are edited

set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
KNOWLEDGE_ROOT="$PROJECT_ROOT/knowledge"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1) are EMPTY because $CLAUDE_TOOL_ARG_FILE_PATH and
# similar env vars don't exist — settings.json substitutes to "". Verified
# 2026-05-08 via stdin-capture diagnostic.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
EDITED_FILE=$(printf '%s' "$HOOK_STDIN" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {})
    print(ti.get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Nothing to do if extraction failed or file path is empty.
[ -z "$EDITED_FILE" ] && exit 0

# 1. Auto-sync knowledge graph files (with integrated inference)
if [[ "$EDITED_FILE" == "$KNOWLEDGE_ROOT"* ]]; then
    echo "🔄 Knowledge file edited: $EDITED_FILE"
    echo "   Syncing to Weaviate (inference happens during sync)..."

    # Get relative path from project root
    REL_PATH="${EDITED_FILE#$PROJECT_ROOT/}"

    # Run sync (inference now integrated - runs BEFORE storing to Weaviate)
    cd "$PROJECT_ROOT"
    .claude/scripts/kg-sync "$REL_PATH" &

    echo "✅ Background sync started for knowledge graph"

    # Run duplicate detection periodically (every 10th edit to avoid overhead)
    EDIT_COUNT_FILE="$PROJECT_ROOT/.claude/logs/.kg_edit_count"
    mkdir -p "$PROJECT_ROOT/.claude/logs"

    if [ -f "$EDIT_COUNT_FILE" ]; then
        COUNT=$(cat "$EDIT_COUNT_FILE")
    else
        COUNT=0
    fi

    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$EDIT_COUNT_FILE"

    # Check for duplicates every 10 edits.
    # Cap output upstream of the grep filter — kg-duplicates is O(n²) over KG
    # nodes so on large KGs the raw output can be thousands of lines. Audit
    # fix 2026-05-07.
    if [ $((COUNT % 10)) -eq 0 ]; then
        echo "🔍 Running duplicate detection (every 10 edits)..."
        (.claude/scripts/kg-duplicates --threshold 0.95 2>&1 \
            | head -c 204800 | head -200 \
            | grep -E "(✅|⚠️|📊)" || true) &
    fi
fi

# 2. Auto-sync development documentation files. Uses the same kg-sync
#    entry point as knowledge/ — it routes by path: docs/* → dev
#    collection (sync_doc), knowledge/* → KG (sync_node). Same chunker,
#    same named-vector slot logic, same archive-skip behaviour.
#    Audit-driven 2026-04-30; replaces the old upload_docs.py path.
DOCS_DIR="$PROJECT_ROOT/docs"
if [[ "$EDITED_FILE" == "$DOCS_DIR"* ]] && [[ "$EDITED_FILE" == *.md ]]; then
    echo "📚 Documentation edited: $EDITED_FILE"
    echo "   Syncing to Weaviate development collection..."

    REL_PATH="${EDITED_FILE#$PROJECT_ROOT/}"
    cd "$PROJECT_ROOT"
    .claude/scripts/kg-sync "$REL_PATH" &

    echo "✅ Background sync started for development docs"
fi

# 3. Code file changes: incremental code graph update (debounced, 120s)
if [[ "$EDITED_FILE" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
    echo "💻 Code file edited: $(basename "$EDITED_FILE")"

    bash "$SCRIPT_DIR/code-graph-incremental.sh" \
        "$EDITED_FILE" \
        "$PROJECT_ROOT" \
        "ClaudeOrchestrator"

    echo "🧠 [Reminder] When done coding: update CONTEXT_STATE.md + add KG node if new patterns emerged."
fi

# 4. Check for CONTEXT_STATE.md updates (project expert reminder)
if [[ "$EDITED_FILE" == *"CONTEXT_STATE.md" ]]; then
    EXPERT_SKILL="$PROJECT_ROOT/.claude/skills/project-experts/claude-orchestrator-expert.md"

    if [ -f "$EXPERT_SKILL" ]; then
        # Count significant changes
        CHANGES=$(grep -E "(✅|##\s+(Status|Current Work|Next Steps|Knowledge Captured))" "$EDITED_FILE" | wc -l)

        if [ "$CHANGES" -gt 5 ]; then
            echo ""
            echo "📝 Significant changes to CONTEXT_STATE.md detected ($CHANGES markers)"
            echo "   Consider updating: .claude/skills/project-experts/claude-orchestrator-expert.md"
            echo ""
            echo "   Update if:"
            echo "   - Major milestone completed (Skills system, knowledge graph, etc.)"
            echo "   - Architecture changed (MCP, agents, workflow)"
            echo "   - New scripts/commands added (kg-*, wrappers)"
            echo "   - Recent work section needs refresh"
            echo ""
        fi
    fi
fi

# 5. Check for workflow system changes (Skills/Agents/hooks)
WORKFLOW_CHANGED=false


# Check project-specific Skills/hooks
if [[ "$EDITED_FILE" == "$PROJECT_ROOT/.claude/skills"* ]] || \
   [[ "$EDITED_FILE" == "$PROJECT_ROOT/.claude/hooks"* ]]; then
    WORKFLOW_CHANGED=true
fi

if [ "$WORKFLOW_CHANGED" = true ]; then
    echo ""
    echo "⚙️  Workflow system file edited: $(basename "$EDITED_FILE")"
    echo "   Consider:"
    echo "   - Test the change in actual usage"
    echo "   - Update documentation if structure changed"
    echo "   - Run /workflow-optimizer to check for optimizations"
    echo "   - Update skills-setup-guide.md if setup process changed"
    echo ""
fi
