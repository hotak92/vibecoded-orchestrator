#!/usr/bin/env bash
# Parity note (v0.2.54 Track G G-6): the .ps1 sibling now resolves its
# child-spawn PowerShell binary via _lib/resolve-powershell.ps1 (pwsh ->
# powershell fallback for PS 5.1-only machines). No bash-side logic
# change is needed - bash hooks never spawn PowerShell.
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
#   centralization needed: writes always target the project's OWN
#   collections; the access lists gate reads fanning out to peers only.

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
# D-16 (v0.2.73): prefer CLAUDE_PROJECT_DIR so worktree-isolated /
# out-of-tree sessions resolve state, logs and accumulator paths against
# the SAME root as pre-tool-use.sh and post-tool-security.sh (whose
# comment already claims alignment with this hook). Falls back to the
# script-relative root when the env is absent.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
KNOWLEDGE_ROOT="$PROJECT_ROOT/knowledge"

# v0.2.95 (lane F10): the ROUTING — knowledge/ -> kg-sync, docs/ -> the
# development collection, .claude/diagrams/ -> the indexer, code files ->
# the end-of-turn code-graph drain queue — plus the Phase-8 access gate and
# the per-file debounce now live in _lib/route-touched-path.sh, because a
# SECOND hook needs exactly the same decision: post-bash-file-sync.sh, which
# gives a CLI write (`cat > knowledge/foo.md <<EOF`, `sed -i`, `cp`) the
# same treatment an Edit/Write gets. This hook keeps the stdin parse, the
# telemetry exports and the two LLM-visible nudges that are specific to the
# Edit/Write surface. Behaviour of the routing itself is UNCHANGED; the
# helpers moved verbatim.
#
# Conditional source, LOUDLY: a partial/old bundle install may lack the lib,
# and this hook must not ERROR on the user's Edit when it does — but it must
# not go quiet either. Before v0.2.95 the lib-absent case cost only the
# debounce; now it costs ALL routing, so an install missing this one file
# syncs NOTHING and (review MAJOR-1) used to say NOTHING. The report is one
# line per session on stderr plus a line in this hook's additionalContext
# envelope; see `vco_report_missing_hook_lib` in _lib/emit-context.sh for why
# those two channels and not stdout. Exit code is unchanged: always 0.
# shellcheck source=_lib/route-touched-path.sh disable=SC1091
if [ -f "$SCRIPT_DIR/_lib/route-touched-path.sh" ]; then
    . "$SCRIPT_DIR/_lib/route-touched-path.sh"
fi

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
#
# HK-1 (v0.2.73): parse stdin EXACTLY ONCE. Previously this block spawned
# four separate `$PY -c` interpreters (EDITED_FILE, AGENT_ID, AGENT_TYPE,
# SESSION_ID) each re-reading and re-decoding the same payload — a
# PostToolUse(Edit|Write) turn fires this hook plus ~6 siblings, so the
# per-edit interpreter-start count ran ~15. One decoder now emits all four
# fields as NUL-delimited records; bash reads them with a single `read`.
# NUL delimiting keeps a newline-bearing file_path intact. Malformed
# stdin → all-empty (each field defaults to ""), preserving the exit-0
# soft-fail contract.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
# The decoder emits the four fields NUL-delimited (a trailing NUL after
# each field, including the last), so `read -r -d ''` in a loop reads each
# field cleanly regardless of embedded newlines. Piping into the while
# loop (not a here-string) avoids the trailing-newline corruption a
# here-string would add.
EDITED_FILE=""
AGENT_ID=""
AGENT_TYPE=""
SESSION_ID_FROM_STDIN=""
_HK_IDX=0
while IFS= read -r -d '' _HK_VAL; do
    case "$_HK_IDX" in
        0) EDITED_FILE="$_HK_VAL" ;;
        1) AGENT_ID="$_HK_VAL" ;;
        2) AGENT_TYPE="$_HK_VAL" ;;
        3) SESSION_ID_FROM_STDIN="$_HK_VAL" ;;
    esac
    _HK_IDX=$((_HK_IDX + 1))
done < <(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {}) or {}
    fields = [
        ti.get('file_path', '') or '',
        d.get('agent_id', '') or '',
        d.get('agent_type', '') or '',
        d.get('session_id', '') or '',
    ]
except Exception:
    fields = ['', '', '', '']
# Trailing NUL after EACH field so the reader loop terminates cleanly.
sys.stdout.write(''.join(str(f) + '\0' for f in fields))
" 2>/dev/null)
# V52-L.2 Fix 2b context (preserved): AGENT_ID/AGENT_TYPE are exported
# below so the kg-sync / code-graph-incremental subprocesses we spawn can
# attribute their retrieval / sync telemetry to the originating subagent;
# SESSION_ID feeds VCT_SESSION_ID for the same 3-layer telemetry chain.
# Export for child processes (kg-sync, code-graph-incremental.sh, etc.)
# so their emit paths can attribute telemetry to the originating agent.
# Skip empty-string exports — downstream readers treat unset and empty
# identically, but unset keeps `env` listings clean for debugging.
[ -n "$AGENT_ID" ]   && export VCT_AGENT_ID="$AGENT_ID"
[ -n "$AGENT_TYPE" ] && export VCT_AGENT_TYPE="$AGENT_TYPE"
# session_id alignment with VCT_SESSION_ID (see V52-J Edit 4 in
# pre-edit-context-inject.sh): the canonical telemetry emit path reads
# VCT_SESSION_ID as layer-2 of its 3-layer chain. Without this export,
# every CLI-emitted event from a hook-triggered sync would have
# session_id="" — same v0.2.51 bug class as the pre-edit hook fixed.
[ -n "$SESSION_ID_FROM_STDIN" ] && export VCT_SESSION_ID="$SESSION_ID_FROM_STDIN"

[ -z "$EDITED_FILE" ] && exit 0

# === Routing: knowledge/ + docs/ + diagrams + the code-graph drain queue ===
# ONE home — _lib/route-touched-path.sh — shared with post-bash-file-sync.sh
# so a CLI write routes identically to an Edit/Write. vco_route_init resolves
# the debounce helper, the code-extension helper, VCT_PROJECT_ID and the
# access-matrix checker; vco_route_touched_path performs the routing and
# leaves any LLM-visible text in $VCO_ROUTE_NUDGE.
if command -v vco_route_touched_path >/dev/null 2>&1; then
    vco_route_init "$SCRIPT_DIR" "$PROJECT_ROOT" "$PY"
    vco_route_touched_path "$EDITED_FILE" "$SESSION_ID_FROM_STDIN"
    if [ -n "${VCO_ROUTE_NUDGE:-}" ]; then
        _add_nudge "$VCO_ROUTE_NUDGE"
    fi
elif command -v vco_report_missing_hook_lib >/dev/null 2>&1; then
    # The routing home is absent (or unreadable) — a broken install, not a
    # degraded mode. Say so once per session, then carry on with the nudges
    # this hook owns; exit stays 0.
    vco_report_missing_hook_lib "$SCRIPT_DIR" "$PROJECT_ROOT" \
        "$SESSION_ID_FROM_STDIN" "route-touched-path.sh"
    if [ -n "${VCO_MISSING_LIB_NOTICE:-}" ]; then
        _add_nudge "$VCO_MISSING_LIB_NOTICE"
    fi
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
