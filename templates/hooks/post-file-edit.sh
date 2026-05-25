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

# 2b. Auto-index diagrams (Phase 1.5 — Mermaid + Excalidraw).
# Fires on any change under .claude/diagrams/. Throttled to 60s per
# file to avoid re-indexing during rapid in-editor save bursts.
# Sidecar `.meta.json` writes are NOT re-indexed (would infinite-loop).
# Notifies vct-hub for live UI refresh in DiagramsTab (best-effort —
# the /api/v1/notify/diagram-changed route is Phase 1.2's; 404s here
# are swallowed silently until that route lands).
DIAGRAMS_DIR="$PROJECT_ROOT/.claude/diagrams"
if [[ "$EDITED_FILE" == "$DIAGRAMS_DIR"/* ]] \
    && [[ "$EDITED_FILE" != *.meta.json ]] \
    && [[ "$EDITED_FILE" == *.mmd || "$EDITED_FILE" == *.excalidraw ]]; then

    # 60s per-file throttle. Mirrors the SEEN_NODES_FILE-style pattern
    # used by the KG dedup logic in pre-edit-context-inject.sh — bash 3.2
    # compatible (no associative arrays), file-as-set semantics.
    THROTTLE_DIR="$PROJECT_ROOT/.claude/state"
    mkdir -p "$THROTTLE_DIR" 2>/dev/null || true
    # Hash the path (md5 via Python — same portable pattern as
    # pre-edit-context-inject.sh). Avoids slashes in the throttle key.
    DIAGRAM_HASH=$(printf '%s' "$EDITED_FILE" \
        | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" \
        2>/dev/null || echo "_")
    THROTTLE_FILE="$THROTTLE_DIR/diagram_idx_${DIAGRAM_HASH}.ts"
    NOW_TS=$(date +%s)
    LAST_TS=0
    if [ -f "$THROTTLE_FILE" ]; then
        LAST_TS=$(cat "$THROTTLE_FILE" 2>/dev/null || echo 0)
        # Guard against non-numeric content
        case "$LAST_TS" in
            ''|*[!0-9]*) LAST_TS=0 ;;
        esac
    fi
    AGE=$(( NOW_TS - LAST_TS ))
    if [ "$AGE" -ge 60 ]; then
        echo "$NOW_TS" > "$THROTTLE_FILE" 2>/dev/null || true

        # Resolve venv-Python so `import vco_lib.diagram_indexer` works.
        # Same chain as pre-edit-context-inject.sh.
        _VENV_BASE="${VCT_INSTALL_ROOT:-$PROJECT_ROOT}"
        _DIAG_VENV=""
        if [ -n "${VCT_VENV:-}" ] && [ -x "$VCT_VENV/bin/python" ]; then
            _DIAG_VENV="$VCT_VENV/bin/python"
        fi
        if [ -z "$_DIAG_VENV" ]; then
            for _cand in \
                "$_VENV_BASE/.venv/bin/python" \
                "$_VENV_BASE/claude_mcp_servers/.venv/bin/python"; do
                if [ -x "$_cand" ]; then
                    _DIAG_VENV="$_cand"
                    break
                fi
            done
        fi
        [ -z "$_DIAG_VENV" ] && _DIAG_VENV="$PY"

        # Index in background — never block the hook on Weaviate slowness.
        ( "$_DIAG_VENV" -m vco_lib.diagram_indexer index "$EDITED_FILE" \
            >/dev/null 2>&1 || true ) &

        # A6 wire-up (Phase 1 item 9 + §1.5.6): auto-snapshot the file
        # before the next edit can land. Uses the same SQLite table
        # (`diagram_snapshots`) the launcher's Tauri command writes,
        # with trigger=`auto_pre_edit_save`. Throttle is shared with
        # the indexer above (one snapshot per file per 60s); content
        # dedup is enforced inside the CLI (UNIQUE constraint +
        # explicit hash check). Soft-fail: snapshot failure must
        # never block the user's edit, so we background + `|| true`.
        ( "$_DIAG_VENV" -m vco_lib.diagram_indexer snapshot create \
            "$EDITED_FILE" --quiet >/dev/null 2>&1 || true ) &

        # Live UI refresh in DiagramsTab is driven by the launcher's
        # frontend file-watcher (chokidar in launcher/src/lib/...) — NOT
        # a hub broadcast. The original Phase 1.5.A design called for a
        # vct-hub /api/v1/notify/diagram-changed route, but pub/sub from
        # hub → frontend would need an SSE/WebSocket plumbing layer the
        # launcher does not have today. The frontend-side watcher is
        # already reliable for the live-preview UX and avoids the
        # broker complexity. Re-evaluate if multi-machine notification
        # ever becomes a real requirement.
    fi
fi

# 3. Code file changes: incremental code graph update + LLM nudge.
# v0.2.21 Step 18 (caller migration): resolve the code-graph collection
# prefix via the launcher's vct-hub first (`vct_project_config.sh --field
# code_graph_collection_prefix`), fall back to the legacy env chain when
# the hub is unreachable (launcher not running, project not registered,
# stale token). The hub is the authoritative source post-v0.2.21.
#
# v0.2.23 field switch: previously this read `code_graph_project`, which
# the hub returns as a legacy alias for `project_slug` — NOT the canonical
# Weaviate prefix. The analyzer's own `_sanitize_collection_prefix` then
# re-canonicalised the slug, producing a prefix that diverged from the
# launcher's `project_codegraph_bindings.collection_prefix`. Symptom:
# every incremental write since the project rename landed in zombie
# collections (e.g. `Orchestrator_root_Code*`) while consumers queried
# the canonical prefix (e.g. `VibeCodedOrchestrator_Code*`) and saw 0
# results. `code_graph_collection_prefix` is the binding-row truth and
# the only correct source for the write target.
#
# Pre-v0.2.11 behaviour hardcoded "ClaudeOrchestrator" here, which
# polluted the legacy collection from every project install. Do NOT
# re-introduce a hardcoded literal in this position.
if [[ "$EDITED_FILE" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
    CODE_GRAPH_PREFIX_RESOLVED=""
    _RESOLVER="$PROJECT_ROOT/.claude/scripts/vct_project_config.sh"
    if [ -x "$_RESOLVER" ]; then
        CODE_GRAPH_PREFIX_RESOLVED=$(
            "$_RESOLVER" "$PROJECT_ROOT" --field code_graph_collection_prefix 2>/dev/null
        ) || CODE_GRAPH_PREFIX_RESOLVED=""
    fi
    if [ -z "$CODE_GRAPH_PREFIX_RESOLVED" ]; then
        CODE_GRAPH_PREFIX_RESOLVED="${CODE_GRAPH_PROJECT:-${PROJECT_NAME:-$(basename "$PROJECT_ROOT")}}"
    fi
    bash "$SCRIPT_DIR/code-graph-incremental.sh" \
        "$EDITED_FILE" \
        "$PROJECT_ROOT" \
        "$CODE_GRAPH_PREFIX_RESOLVED"

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
