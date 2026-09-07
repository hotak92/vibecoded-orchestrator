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
# D-16 (v0.2.73): prefer CLAUDE_PROJECT_DIR so worktree-isolated /
# out-of-tree sessions resolve state, logs and accumulator paths against
# the SAME root as pre-tool-use.sh and post-tool-security.sh (whose
# comment already claims alignment with this hook). Falls back to the
# script-relative root when the env is absent.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
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
# (orchestrator-root); the bundle install writes it to
# .claude/scripts/vct_access_check.sh in each user project, so the two
# are byte-identical the moment they are written. Prior text here said
# the "template-drift gate enforces" that identity. It does not: the
# gate `scripts/check_template_drift.py` was REMOVED in PR-39 / v0.2.12
# (see `.github/workflows/hook-parity.yml`) and this repo tracks no
# `.claude/scripts/` at all — so the citation was already false on the
# day this v0.2.49 block was written. What actually holds afterwards is
# the bundle-update contract: a copy the user has since edited is backed
# up to .claude/backups/bundle-adoptions/<ts>/ and replaced with the
# shipped one. Nothing gates the copy in between.
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

# v0.2.92 FIX (silent-KG-sync-drop): this synchronous function is RETAINED
# for the contract it documents (mirrors post-file-edit.ps1's
# Test-KgWriteAllowed, which carries the identical caveat), but it is NOT
# on the live sync path — do not assume calling it here has any effect on
# whether a sync actually runs. The v0.2.65 Track B "Item 1" hardening pass
# made the debounce flusher spawn via `setsid bash -c '...'` for
# crash-safety (see _lib/kg-sync-debounce.sh) — a genuinely separate
# process that sources ONLY that lib, never this script. Before that pass
# the flusher was an in-process `( ... ) &` subshell (shares function
# scope); after it, a bash function defined in THIS script is invisible
# there. Every command string built with `_kg_write_allowed <args> && ...`
# (the pattern this function was designed for) therefore failed with
# "_kg_write_allowed: command not found" (exit 127) at EVAL time inside the
# detached flusher — and because it's the LHS of `&&`, the sync on the RHS
# never ran. The failure was invisible: `_kg_debounce_run_claimed` redirects
# the eval to `>/dev/null 2>&1`, so nothing was printed, logged, or left as
# residue anywhere (the claimed work dir is `rm -rf`'d regardless of the
# eval's exit code). This silently broke the ENTIRE hook-triggered KG/docs
# auto-sync path (both the debounced flusher/reaper path AND the
# lock-ceiling/window=0 immediate-`sh -c` fallback, which doesn't even
# source kg-sync-debounce.sh) for every project on every install since the
# v0.2.65 debounce-hardening pass — verified via an isolated repro (a
# non-exported bash function called from a `setsid bash -c` child reports
# "command not found"). The existing debounce test suite
# (tests/test_kg_sync_debounce_cap_throttle.py) never caught it because it
# drives `_kg_debounce_schedule` with a trivial self-contained test command
# ("echo ... >> log"), never the real gated command built below — see
# tests/test_v0292_kg_sync_gate_selfcontained.py for the integration-level
# regression test this fix adds. See knowledge/concepts/
# debounced-hook-commands-must-be-self-contained-2026-09-01.md.
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

# Resolve the access-matrix checker path ONCE, synchronously, here — mirrors
# post-file-edit.ps1's $AccessCheckPs1 resolution. Safe to do at schedule
# time (not eval time): the checker script is a static repo file that
# cannot appear/disappear within a debounce window.
_KG_ACCESS_CHECKER=""
if [ -x "$PROJECT_ROOT/templates/scripts/vct_access_check.sh" ]; then
    _KG_ACCESS_CHECKER="$PROJECT_ROOT/templates/scripts/vct_access_check.sh"
elif [ -x "$PROJECT_ROOT/.claude/scripts/vct_access_check.sh" ]; then
    _KG_ACCESS_CHECKER="$PROJECT_ROOT/.claude/scripts/vct_access_check.sh"
fi

# v0.2.92 FIX: build a SELF-CONTAINED "gate THEN sync" command string —
# the actual replacement for the broken `_kg_write_allowed <args> && ...`
# pattern (see the extended comment above `_kg_write_allowed`). Mirrors
# post-file-edit.ps1's Build-GatedSyncCommand:
#   * The "no project_id" branch is decided SYNCHRONOUSLY, right here,
#     because VCT_PROJECT_ID cannot change within a debounce window — so
#     the metric + deferral emission (the real side effects that matter)
#     fire immediately instead of being embedded in a string that would
#     need its own file-I/O logic re-derived at eval time.
#   * The "no collection" / "no resolver on disk" branches fall open here
#     too, for the same reason (both are schedule-time-stable facts).
#   * ONLY the genuinely time-sensitive part — the access-matrix decision
#     itself — is embedded as a plain POSIX `[ ]`/`if` snippet that calls
#     the checker SCRIPT directly (not a shell function), so it is
#     evaluable by ANY shell that ends up running it: the bash flusher/
#     reaper (_lib/kg-sync-debounce.sh) OR the plain `sh -c` immediate-
#     detach fallback (window=0 / lock-ceiling overflow), which sources
#     nothing at all.
#   $1 = project id, $2 = target collection, $3 = sync command string
_kg_build_gated_sync_cmd() {
    local proj="$1" coll="$2" sync_expr="$3"
    if [ -z "$proj" ]; then
        if [ -n "$coll" ]; then
            _kg_emit_gate_skipped_metric "$coll"
            _kg_emit_gate_skipped_deferral "$coll"
        fi
        printf '%s' "$sync_expr"
        return 0
    fi
    if [ -z "$coll" ] || [ -z "$_KG_ACCESS_CHECKER" ]; then
        printf '%s' "$sync_expr"
        return 0
    fi
    printf '_p=%s; _c=%s; _lvl=$(%s "$_p" "$_c" 2>/dev/null || echo write); if [ "$_lvl" = write ]; then %s; fi' \
        "$(_kg_debounce_shquote "$proj")" \
        "$(_kg_debounce_shquote "$coll")" \
        "$(_kg_debounce_shquote "$_KG_ACCESS_CHECKER")" \
        "$sync_expr"
}

# 1. Auto-sync knowledge graph files (background side-effect).
# D-9 (v0.2.73): match on "<root>/" not "<root>" so sibling directories
# (knowledge_base/, knowledge-old/) don't sync into the KG collection.
# The diagrams branch below (:"$DIAGRAMS_DIR"/*) already uses this form.
if [[ "$EDITED_FILE" == "$KNOWLEDGE_ROOT"/* ]]; then
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
    # v0.2.92 FIX: build via _kg_build_gated_sync_cmd, NOT a
    # `_kg_write_allowed ... && ...` string — see the extended comment
    # above _kg_write_allowed for why the latter silently never ran.
    _KG_SYNC_CMD="$(_kg_build_gated_sync_cmd "$VCT_PROJECT_ID" "${KG_COLLECTION:-}" ".claude/scripts/kg-sync $(_kg_debounce_shquote "$REL_PATH")")"
    _kg_debounce_schedule "$PROJECT_ROOT" "$EDITED_FILE" "$PY" "$PROJECT_ROOT" "$_KG_SYNC_CMD" "kg"

    EDIT_COUNT_FILE="$PROJECT_ROOT/.claude/logs/.kg_edit_count"
    mkdir -p "$PROJECT_ROOT/.claude/logs"
    COUNT=0
    if [ -f "$EDIT_COUNT_FILE" ]; then
        COUNT=$(cat "$EDIT_COUNT_FILE" 2>/dev/null || echo 0)
    fi
    # D-8 (v0.2.73): guard against corrupted counter content. Under
    # `set -e`, `$((COUNT + 1))` with non-numeric COUNT aborts the rest of
    # the hook — coerce anything non-numeric back to 0.
    case "$COUNT" in
        ''|*[!0-9]*) COUNT=0 ;;
    esac
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$EDIT_COUNT_FILE"
    if [ $((COUNT % 10)) -eq 0 ]; then
        # D-8 (v0.2.73): the every-10-edits duplicate scan previously
        # greped its ✅/⚠️/📊 summary to PLAIN stdout, which PostToolUse
        # hooks silently drop (see the file header) — the advertised
        # feature had no consumer. Persist the summary to a report file
        # instead; the NEXT synchronous hook fire (a nudge-bearing edit,
        # or session-start-kg-loader) surfaces it. The scan itself still
        # runs backgrounded so the hook never blocks.
        _DUP_REPORT="$PROJECT_ROOT/.claude/state/kg_duplicates_report.txt"
        mkdir -p "$PROJECT_ROOT/.claude/state" 2>/dev/null || true
        (
            # ❌ is kept too: the scan's failure line (`❌ Error during
            # duplicate detection: …`) is what the ⚠️ "See the error above."
            # verdict points at — filtering it out would surface a report
            # that names an error the reader cannot see.
            _dup_out=$(.claude/scripts/kg-duplicates --threshold 0.95 2>&1 \
                | head -c 204800 | head -200 \
                | grep -E "(✅|⚠️|📊|❌)" || true)
            if [ -n "$_dup_out" ]; then
                {
                    printf '# KG duplicate scan (every-10-edits, %s)\n' \
                        "$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo unknown)"
                    printf '%s\n' "$_dup_out"
                } > "$_DUP_REPORT" 2>/dev/null || true
            fi
        ) &
    fi
    # D-8: surface a PENDING duplicate-scan report (from a prior fire)
    # through the LLM-visible envelope on THIS synchronous fire, then
    # consume it (rename) so it's shown once.
    _DUP_REPORT="$PROJECT_ROOT/.claude/state/kg_duplicates_report.txt"
    if [ -f "$_DUP_REPORT" ]; then
        _dup_body=$(cat "$_DUP_REPORT" 2>/dev/null || true)
        if [ -n "$_dup_body" ]; then
            _add_nudge "[KG duplicate scan] The periodic duplicate check found candidates worth reviewing:
$_dup_body
Run \`.claude/scripts/kg-duplicates\` for detail, or ignore if these are intentional siblings."
        fi
        rm -f "$_DUP_REPORT" 2>/dev/null || true
    fi
fi

# 2. Auto-sync development documentation files (background side-effect).
DOCS_DIR="$PROJECT_ROOT/docs"
# D-9 (v0.2.73): trailing slash — don't match docs-archive/, docsite/, etc.
if [[ "$EDITED_FILE" == "$DOCS_DIR"/* ]] && [[ "$EDITED_FILE" == *.md ]]; then
    REL_PATH="${EDITED_FILE#$PROJECT_ROOT/}"
    cd "$PROJECT_ROOT"
    # v0.2.49 Phase 8: gate docs sync on access-matrix write permission
    # against DEVELOPMENT_COLLECTION (the docs/ target).
    # 2026-06-18: debounced (same coalesce-rapid-repeats semantics as the
    # knowledge/ branch above; gate runs at sync time, latest content
    # lands).
    # v0.2.92 FIX: build via _kg_build_gated_sync_cmd — see the extended
    # comment above _kg_write_allowed / the knowledge/ branch above.
    _DOCS_SYNC_CMD="$(_kg_build_gated_sync_cmd "$VCT_PROJECT_ID" "${DEVELOPMENT_COLLECTION:-}" ".claude/scripts/kg-sync $(_kg_debounce_shquote "$REL_PATH")")"
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

# 3. Code file changes: end-of-turn batched code graph drain + LLM nudge.
# v0.2.73 (FIX-B): the per-edit code-graph sync is REMOVED. post-file-edit no
# longer resolves the code-graph collection prefix here NOR schedules a
# per-edit `code-graph-incremental.sh` run (which hit the big CodeFunction
# collection's insert-time HNSW churn on every keystroke and, multiplied by
# parallel worktrees, drove the measured disk write-amplification of 857 GB/8h
# vs a 5 GB dataset). Instead the edited path is APPENDED to a per-turn drain
# queue (+ the reminder accumulator), and the Stop hook `stop-codegraph-drain.sh`
# runs ONE analyzer pass at end-of-turn over ALL the turn's files, GROUPED BY
# CANONICAL ROOT (so the drain — not this hook — resolves the
# `code_graph_collection_prefix` per canonical root), rate-limited to once per
# 120s per project. Latency is acceptable (maintainer: "accept slightly
# outdated codegraph with less frequent syncs"). The KG-sync (knowledge/) and
# docs debounce paths above are UNCHANGED — this fix targets the CODE path only
# (the amplifier); those writes are small and already per-file coalesced.
if [[ "$EDITED_FILE" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
    # NOTE: the accumulator append below serves BOTH the end-of-turn reminder
    # (P6) AND the batched code-graph drain (FIX-B). Keep the FULL path (the
    # drain needs it to resolve the canonical root; the reminder reduces to
    # basenames itself).

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
        mkdir -p "$PROJECT_ROOT/.claude/state" 2>/dev/null || true
        _EDIT_ACCUM="$PROJECT_ROOT/.claude/state/edit_reminder_${SESSION_ID_FROM_STDIN}.txt"
        printf '%s\n' "$EDITED_FILE" >> "$_EDIT_ACCUM" 2>/dev/null || true
        # v0.2.73 (FIX-B): SEPARATE code-graph drain queue. The reminder
        # accumulator above is drained + cleared EVERY turn by
        # stop-codegraph-reminder.sh; the drain queue below is drained by
        # stop-codegraph-drain.sh, which is RATE-LIMITED (once per 120s) and
        # therefore must PERSIST the union of edited paths across turns that
        # fall inside the rate-limit window (it clears the queue only when it
        # actually runs the analyzer). A distinct file avoids a two-consumer
        # race on one accumulator (two Stop hooks reading + unlinking the same
        # path). Same full-path convention (the drain resolves canonical roots).
        _CG_DRAIN_QUEUE="$PROJECT_ROOT/.claude/state/codegraph_drain_${SESSION_ID_FROM_STDIN}.txt"
        printf '%s\n' "$EDITED_FILE" >> "$_CG_DRAIN_QUEUE" 2>/dev/null || true
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
