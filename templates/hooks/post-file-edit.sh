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

# Debounce helper (2026-06-18, write-amplification fix). Coalesces rapid
# re-edits of the SAME file into one Weaviate write per quiet-window
# (VCO_KG_SYNC_DEBOUNCE_SECONDS, default 5; 0 disables). The correctness
# argument (final state always syncs) + crash-safety reasoning live in
# the helper's header. Sourced (not exec'd) so the access-matrix gate
# functions defined below stay in scope for the deferred sync command —
# the gate runs at SYNC time inside the debounced flusher, never bypassed.
#
# Conditional source: a partial/old bundle install may lack the lib. If
# it's absent we define a passthrough _kg_debounce_schedule that runs the
# sync immediately in the background (the pre-2026-06-18 behaviour), so a
# missing helper degrades to "no debounce" rather than breaking the hook.
# shellcheck source=_lib/kg-sync-debounce.sh disable=SC1091
if [ -f "$SCRIPT_DIR/_lib/kg-sync-debounce.sh" ]; then
    . "$SCRIPT_DIR/_lib/kg-sync-debounce.sh"
else
    _kg_debounce_shquote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
    _kg_debounce_schedule() {
        # $1=PROJECT_ROOT $2=file $3=python $4=workdir $5=cmd $6=channel(unused)
        ( cd "$4" 2>/dev/null || true; eval "$5" ) &
    }
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
# V52-L.2 Fix 2b: parse subagent identity from the stdin payload. We
# don't write a JSONL log directly from this hook, but the kg-sync /
# code-graph-incremental subprocesses we spawn DO emit retrieval / sync
# telemetry — exporting these as env vars (VCT_AGENT_ID / VCT_AGENT_TYPE)
# lets the canonical emit path (rl_client/telemetry_emit.py) attribute
# those rows to the agent that triggered the write. Pre-V52-L.2 every
# subprocess saw an empty agent context regardless of which subagent ran
# the edit. Empty string when absent (parent context).
AGENT_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('agent_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
AGENT_TYPE=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('agent_type', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
SESSION_ID_FROM_STDIN=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
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
# v0.2.49 SB1: emit a `dropped_writes.jsonl` row when the gate falls
# back to silent-allow because VCT_PROJECT_ID is empty. Mirrors the
# Python-side `_emit_gate_skipped_metric` shape and the existing
# `emit_metric` helper in vct_access_check.sh. Never fails the caller.
_kg_emit_gate_skipped_metric() {
    local coll="${1:-}"
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    local cache_dir="$state_dir/cache"
    local jsonl="$cache_dir/dropped_writes.jsonl"
    mkdir -p "$cache_dir" 2>/dev/null || return 0
    local ts
    ts=$(date +%s 2>/dev/null) || ts=0
    printf '{"ts":%d,"project_id":"","collection":"%s","reason":"gate_skipped_no_project_id","fail_open":true}\n' \
        "$ts" "$coll" \
        >> "$jsonl" 2>/dev/null || true
}

# v0.2.49 SB1: write an UPDATE_DEFERRED.md entry directing the user to
# resolve the empty-VCT_PROJECT_ID condition (re-run install.py
# --update OR re-register via Launcher GUI). Per the user's 2026-06-08
# Q1 directive, this is the user-facing surface — silent-allow remains
# the default at the gate, no stderr WARNING is emitted.
#
# Idempotency: deduped per (session, project) via a sentinel file in
# .claude/state/ so a kg-sync burst doesn't accumulate duplicate
# blocks. The condition_id token matches the Python sibling so even
# cross-process duplicates upsert under the
# vco_lib.deferral_report contract when --apply-deferred eventually
# runs.
_kg_emit_gate_skipped_deferral() {
    local coll="${1:-}"
    local deferred="$PROJECT_ROOT/.claude/context/UPDATE_DEFERRED.md"
    local state_dir="$PROJECT_ROOT/.claude/state"
    local session_id="${VCT_SESSION_ID:-${CLAUDE_SESSION_ID:-$$}}"
    local sentinel="$state_dir/gate_skipped_deferral_${session_id}"

    # Per-session dedup. The first call writes; subsequent calls within
    # the same session are no-ops.
    [ -f "$sentinel" ] && return 0
    mkdir -p "$state_dir" 2>/dev/null || return 0
    : > "$sentinel" 2>/dev/null || true

    mkdir -p "$(dirname "$deferred")" 2>/dev/null || return 0

    # Idempotent body marker — if a prior session already wrote a row
    # for this condition_id, leave it in place rather than duplicating
    # the section header.
    local marker="## gate_skipped_no_project_id"
    if [ -f "$deferred" ] && grep -q "^$marker" "$deferred" 2>/dev/null; then
        return 0
    fi

    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null) || ts="unknown"

    # Append-mode write. If the file doesn't exist, this creates a
    # naked entry with no frontmatter — vco_lib.deferral_report.read()
    # treats absent frontmatter as an empty header (the section parser
    # still picks up the entry via `^## <cid> (sev)`). The next
    # install.py --update pass calls DeferralReport.read() then write()
    # which canonicalises the file with frontmatter.
    {
        printf '\n%s (warning)\n\n' "$marker"
        printf '**Title**: Phase-8 access-matrix gate skipped (VCT_PROJECT_ID missing from hook env)\n\n'
        printf '**Detected**: The post-file-edit.sh hook reached _kg_write_allowed with no VCT_PROJECT_ID. The Phase-8 WRITE gate cannot identify this project against the hub access matrix, so the write was permitted via the silent-allow path. Target collection: %s\n\n' "$coll"
        printf '**Why deferred**: Seeding VCT_PROJECT_ID requires an orchestrator install pass (queries launcher.db for the project UUID) or a Launcher GUI re-registration. The hook cannot self-heal.\n\n'
        printf '**To apply**:\n'
        printf '```bash\n'
        printf '# Option A — orchestrator-root install / update:\n'
        printf 'python install.py --update\n\n'
        printf '# Option B — per-project (pre-v0.2.49 install): re-register the\n'
        printf '# project via Launcher GUI -> Projects -> Identity tab. The\n'
        printf "# launcher's apply_project_env pass seeds VCT_PROJECT_ID into\n"
        printf '# the project-local .claude/env from launcher.db.\n'
        printf '```\n\n'
        printf '**Detected at**: %s\n\n' "$ts"
        printf -- '---\n'
    } >> "$deferred" 2>/dev/null || true
}

_kg_write_allowed() {
    local proj="${1:-}"
    local coll="${2:-}"
    if [ -z "$proj" ]; then
        # v0.2.49 SB1: empty VCT_PROJECT_ID was previously a silent
        # bypass — gate effectively disabled. Per user Q1 (2026-06-08),
        # silent-allow stays the default; the metric (audit trail) +
        # the deferral (user-facing remediation) are the two
        # visibility surfaces. Order is metric-first so the JSONL row
        # lands even if the deferral write hits a permission error.
        _kg_emit_gate_skipped_metric "$coll"
        _kg_emit_gate_skipped_deferral "$coll"
        return 0
    fi
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
    # 2026-06-18: debounced. The gate runs at SYNC time (inside the
    # quoted command the flusher eval's), not at schedule time, so a
    # coalesced burst still consults the access matrix exactly once when
    # the deferred sync fires. The sync re-reads the file from disk, so
    # the latest content lands. All interpolated values are shquote'd so
    # a space- or quote-bearing path survives the eval (and the reaper's
    # re-eval from the recorded cmd file).
    _KG_SYNC_CMD="_kg_write_allowed $(_kg_debounce_shquote "$VCT_PROJECT_ID") $(_kg_debounce_shquote "${KG_COLLECTION:-}") && .claude/scripts/kg-sync $(_kg_debounce_shquote "$REL_PATH")"
    _kg_debounce_schedule "$PROJECT_ROOT" "$EDITED_FILE" "$PY" "$PROJECT_ROOT" "$_KG_SYNC_CMD" "kg"

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
    # 2026-06-18: debounced (same coalesce-rapid-repeats semantics as the
    # knowledge/ branch above; gate runs at sync time, latest content
    # lands).
    _DOCS_SYNC_CMD="_kg_write_allowed $(_kg_debounce_shquote "$VCT_PROJECT_ID") $(_kg_debounce_shquote "${DEVELOPMENT_COLLECTION:-}") && .claude/scripts/kg-sync $(_kg_debounce_shquote "$REL_PATH")"
    _kg_debounce_schedule "$PROJECT_ROOT" "$EDITED_FILE" "$PY" "$PROJECT_ROOT" "$_DOCS_SYNC_CMD" "docs"
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
    # 2026-06-18: debounced. code-graph-incremental.sh runs the analyzer
    # per-edit with NO internal debounce (its own comment: "no debounce —
    # keep code graph fresh"), so each edit was a separate Weaviate
    # upsert. Coalescing rapid re-edits of the SAME file is safe: the
    # analyzer re-reads the file from disk (diffs the working tree) at run
    # time, so the deferred run indexes the latest content. The
    # incremental script has no access-matrix gate of its own (code-graph
    # writes are not gated), so the debounced command is the bare invoke.
    _CG_SYNC_CMD="bash $(_kg_debounce_shquote "$SCRIPT_DIR/code-graph-incremental.sh") $(_kg_debounce_shquote "$EDITED_FILE") $(_kg_debounce_shquote "$PROJECT_ROOT") $(_kg_debounce_shquote "$CODE_GRAPH_PREFIX_RESOLVED")"
    _kg_debounce_schedule "$PROJECT_ROOT" "$EDITED_FILE" "$PY" "$PROJECT_ROOT" "$_CG_SYNC_CMD" "code"

    # v0.2.72 P6: reminder AGGREGATION. This "was just edited → update
    # CONTEXT_STATE / capture KG" nudge previously fired on EVERY code-file
    # Edit (~15x/turn on a busy turn — pure repetition). Instead of emitting
    # here, APPEND the edited path to a per-turn accumulator file; the Stop
    # hook (stop-codegraph-reminder.sh) drains it at end-of-turn and emits ONE
    # aggregated reminder naming all edited files. Soft-fail: an unkeyable
    # session (empty id) or a write error just skips accumulation (no reminder
    # that turn) rather than reverting to per-edit spam. The Stop hook dedups
    # paths, so re-editing the same file across the turn lists it once.
    if [ -n "$SESSION_ID_FROM_STDIN" ]; then
        _EDIT_ACCUM="$PROJECT_ROOT/.claude/state/edit_reminder_${SESSION_ID_FROM_STDIN}.txt"
        mkdir -p "$PROJECT_ROOT/.claude/state" 2>/dev/null || true
        printf '%s\n' "$EDITED_FILE" >> "$_EDIT_ACCUM" 2>/dev/null || true
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
