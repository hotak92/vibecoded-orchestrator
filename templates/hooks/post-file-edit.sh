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

# v0.2.49 Phase 8 (item #22): access-matrix gate for KG writes.
#
# Before kicking off any kg-sync subprocess, check if this project has
# write access to the target collection. The check is fail-open: if the
# hub is unreachable / the project isn't registered / the response is
# malformed, the gate returns "write" + emits a WARNING + logs a
# dropped-write-metric row, then the sync proceeds. This is DELIBERATE
# (closed-circuit would brick all KG writes during launcher restart).
#
# When the gate returns "read" or "none", we SKIP the sync silently +
# the user gets the WARNING from the resolver client about the deny.
#
# The resolver script lives at templates/scripts/vct_access_check.sh
# (orchestrator-root) and is byte-identical to .claude/scripts/
# vct_access_check.sh in user projects (template-drift gate enforces).
_kg_write_allowed() {
    local proj="${1:-}"
    local coll="${2:-}"
    [ -z "$proj" ] && return 0   # no project context → allow (legacy path)
    [ -z "$coll" ] && return 0   # no collection context → allow
    local checker=""
    if [ -x "$PROJECT_ROOT/templates/scripts/vct_access_check.sh" ]; then
        checker="$PROJECT_ROOT/templates/scripts/vct_access_check.sh"
    elif [ -x "$PROJECT_ROOT/.claude/scripts/vct_access_check.sh" ]; then
        checker="$PROJECT_ROOT/.claude/scripts/vct_access_check.sh"
    else
        # Resolver not on disk → allow (pre-v0.2.49 install, or
        # post-update where the script hasn't been bundled yet).
        return 0
    fi
    local level
    level=$("$checker" "$proj" "$coll" 2>/dev/null || echo "write")
    [ "$level" = "write" ]
}

# Resolve project_id once for the access checks below.
VCT_PROJECT_ID="${VCT_PROJECT_ID:-}"
if [ -z "$VCT_PROJECT_ID" ] && [ -f "$PROJECT_ROOT/.claude/env" ]; then
    # Best-effort grep for VCT_PROJECT_ID=… in .claude/env (sourced
    # form, not as a bash source — we don't want to inherit other env).
    VCT_PROJECT_ID=$(grep -E '^[[:space:]]*VCT_PROJECT_ID=' "$PROJECT_ROOT/.claude/env" 2>/dev/null \
        | head -1 | sed -E 's/^[[:space:]]*VCT_PROJECT_ID=//; s/^"//; s/"$//')
fi

# 1. Auto-sync knowledge graph files (background side-effect).
if [[ "$EDITED_FILE" == "$KNOWLEDGE_ROOT"* ]]; then
    REL_PATH="${EDITED_FILE#$PROJECT_ROOT/}"
    cd "$PROJECT_ROOT"
    # v0.2.49 Phase 8: gate the sync on access-matrix write permission.
    # KG_COLLECTION is the target Weaviate class for primary-KG writes.
    if _kg_write_allowed "$VCT_PROJECT_ID" "${KG_COLLECTION:-}"; then
        .claude/scripts/kg-sync "$REL_PATH" &
    fi

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
    # v0.2.49 Phase 8: gate docs sync on access-matrix write permission
    # against DEVELOPMENT_COLLECTION (the docs/ target).
    if _kg_write_allowed "$VCT_PROJECT_ID" "${DEVELOPMENT_COLLECTION:-}"; then
        .claude/scripts/kg-sync "$REL_PATH" &
    fi
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
        # v0.2.46 post-adversarial: source the shared resolver (POSIX hooks
        # all share resolve-vco-venv.sh — no more inline drift). The
        # helper NEVER falls back to $PROJECT_ROOT/.venv (the user's venv,
        # which won't have weaviate-client + vco_lib). When no VCO venv is
        # resolvable, we degrade to find-python's $PY which at least lets
        # the indexer's import-time error surface as a real ImportError
        # rather than running the wrong interpreter.
        # shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
        . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
        resolve_vco_venv_python "$SCRIPT_DIR"
        _DIAG_VENV="${VCO_VENV_PYTHON:-$PY}"

        # Build the indexer command. Pass --diagrams-collection when
        # DIAGRAMS_COLLECTION is set in the env (fix/a1-indexing-pipeline
        # 2026-05-25). Without this kwarg, the indexer's Weaviate upsert
        # silently skips even though SQLite + sidecar still happen —
        # Bug-1 of the wiring audit. Older projects without
        # DIAGRAMS_COLLECTION in env (pre-config_projection write of
        # this key) get the legacy sidecar-only behaviour automatically.
        _DIAG_ARGS=( -m vco_lib.diagram_indexer index "$EDITED_FILE" )
        if [ -n "${DIAGRAMS_COLLECTION:-}" ]; then
            _DIAG_ARGS+=( --diagrams-collection "$DIAGRAMS_COLLECTION" )
        fi

        # Index + snapshot in a SERIAL background chain.
        # R2 (code review 2026-05-25): previously these ran in PARALLEL
        # via two separate `( ... ) &` forks. The snapshot CLI queries
        # `project_diagrams WHERE project_id=? AND file_path=?` to find
        # the row to snapshot AGAINST. On the very first edit per file,
        # the indexer hasn't UPSERT'd that row yet → snapshot returns
        # "no row" → first-version content lost forever. Serializing the
        # two so the indexer always commits the row before the snapshot
        # query closes the race. Whole chain stays backgrounded so the
        # hook itself never blocks the user.
        (
            "$_DIAG_VENV" "${_DIAG_ARGS[@]}" >/dev/null 2>&1 || true
            # A6 wire-up (Phase 1 item 9 + §1.5.6): auto-snapshot the
            # file before the next edit can land. Throttle is shared
            # with the indexer above (one snapshot per file per 60s);
            # content dedup is enforced inside the CLI (UNIQUE constraint
            # + explicit hash check). Soft-fail: snapshot failure must
            # never block the user's edit.
            "$_DIAG_VENV" -m vco_lib.diagram_indexer snapshot create \
                "$EDITED_FILE" --quiet >/dev/null 2>&1 || true
        ) &

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
