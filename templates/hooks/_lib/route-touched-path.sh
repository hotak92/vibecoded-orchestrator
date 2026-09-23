# shellcheck shell=bash
# route-touched-path.sh — THE home for "a file was touched: where does it
# need to go?" (v0.2.95, lane F10).
#
# WHY THIS EXISTS
# ---------------
# Until v0.2.95 every line of this routing lived inside post-file-edit.sh,
# which is registered on `PostToolUse` matcher `Edit|Write` ONLY. A file
# written from the CLI — `cat > knowledge/foo.md <<EOF`, `sed -i` on a
# docs page, `cp` into a source tree — therefore reached Weaviate NEVER.
# Closing that gap needs a second hook (post-bash-file-sync.sh) that does
# the SAME routing, and the project's A>B>C rule forbids a second copy of
# it. So the routing moved here verbatim and BOTH hooks call it.
#
# WHAT "ROUTING" MEANS HERE, exactly:
#   knowledge/**             -> .claude/scripts/kg-sync           (KG_COLLECTION)
#   docs/**.md               -> .claude/scripts/kg-sync           (DEVELOPMENT_COLLECTION)
#   .claude/diagrams/*.mmd|.excalidraw -> vco_lib.diagram_indexer (throttled 60 s)
#   <code extension>         -> the per-turn code-graph drain queue
# ...each gated by the Phase-8 access matrix and coalesced by the
# per-file debounce, which is why "route", "gate" and "debounce" are one
# concern and live in one file.
#
# CONTRACT FOR CALLERS
#   . _lib/route-touched-path.sh
#   vco_route_init "<hooks_dir>" "<project_root>" "<python>"
#   vco_route_touched_path "<absolute_path>" "<session_id>"
#   # then: append "$VCO_ROUTE_NUDGE" to whatever the hook emits.
#
# Every function is exit-0 / soft-fail: a PostToolUse hook can never block
# and its plain stdout is discarded, so nothing here prints status.

# --- Idempotent double-source guard ---------------------------------------
if [ -n "${_VCO_ROUTE_TOUCHED_PATH_SOURCED:-}" ]; then
    return 0 2>/dev/null || true
fi
_VCO_ROUTE_TOUCHED_PATH_SOURCED=1

# Accumulates LLM-visible text the caller should emit (currently only the
# pending duplicate-scan report). Callers read it after the call.
VCO_ROUTE_NUDGE=""

_vco_route_add_nudge() {
    if [ -n "$VCO_ROUTE_NUDGE" ]; then
        VCO_ROUTE_NUDGE="${VCO_ROUTE_NUDGE}

$1"
    else
        VCO_ROUTE_NUDGE="$1"
    fi
}

# ===========================================================================
# Phase-8 access-matrix gate (moved verbatim from post-file-edit.sh)
# ===========================================================================
#
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
# are byte-identical the moment they are written. What holds afterwards is
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
    local deferred="$_VCO_ROUTE_PROJECT_ROOT/.claude/context/UPDATE_DEFERRED.md"
    local state_dir="$_VCO_ROUTE_PROJECT_ROOT/.claude/state"
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
        printf '**Detected**: A VCO write hook reached the Phase-8 WRITE gate with no VCT_PROJECT_ID. The gate cannot identify this project against the hub access matrix, so the write was permitted via the silent-allow path. Target collection: %s\n\n' "$coll"
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
# process that sources ONLY that lib, never the calling hook. Every command
# string built with `_kg_write_allowed <args> && ...` therefore failed with
# "_kg_write_allowed: command not found" (exit 127) at EVAL time inside the
# detached flusher — and because it's the LHS of `&&`, the sync on the RHS
# never ran. Use _kg_build_gated_sync_cmd instead. See knowledge/concepts/
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
    if [ -x "$_VCO_ROUTE_PROJECT_ROOT/templates/scripts/vct_access_check.sh" ]; then
        checker="$_VCO_ROUTE_PROJECT_ROOT/templates/scripts/vct_access_check.sh"
    elif [ -x "$_VCO_ROUTE_PROJECT_ROOT/.claude/scripts/vct_access_check.sh" ]; then
        checker="$_VCO_ROUTE_PROJECT_ROOT/.claude/scripts/vct_access_check.sh"
    else
        # Resolver not on disk → allow (pre-v0.2.49 install, or
        # post-update where the script hasn't been bundled yet).
        return 0
    fi
    local level
    level=$("$checker" "$proj" "$coll" 2>/dev/null || echo "write")
    [ "$level" = "write" ]
}

# v0.2.92 FIX: build a SELF-CONTAINED "gate THEN sync" command string —
# the actual replacement for the broken `_kg_write_allowed <args> && ...`
# pattern (see the extended comment above _kg_write_allowed). Mirrors
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

# ===========================================================================
# vco_route_init <hooks_dir> <project_root> <python>
# ===========================================================================
# Resolves everything the routing needs ONCE per hook fire: the debounce
# helper, the code-extension helper, VCT_PROJECT_ID and the access-matrix
# checker path. Safe to call more than once.
vco_route_init() {
    _VCO_ROUTE_HOOKS_DIR="${1:-}"
    _VCO_ROUTE_PROJECT_ROOT="${2:-}"
    _VCO_ROUTE_PY="${3:-}"
    KNOWLEDGE_ROOT="$_VCO_ROUTE_PROJECT_ROOT/knowledge"

    # Debounce helper (2026-06-18, write-amplification fix). Coalesces rapid
    # re-edits of the SAME file into one Weaviate write per quiet-window
    # (VCO_KG_SYNC_DEBOUNCE_SECONDS, default 5; 0 disables). The correctness
    # argument (final state always syncs) + crash-safety reasoning live in
    # the helper's header.
    #
    # Conditional source: a partial/old bundle install may lack the lib. If
    # it's absent we define a passthrough _kg_debounce_schedule that runs the
    # sync immediately in the background (the pre-2026-06-18 behaviour), so a
    # missing helper degrades to "no debounce" rather than breaking the hook.
    # shellcheck source=kg-sync-debounce.sh disable=SC1091
    if [ -f "$_VCO_ROUTE_HOOKS_DIR/_lib/kg-sync-debounce.sh" ]; then
        . "$_VCO_ROUTE_HOOKS_DIR/_lib/kg-sync-debounce.sh"
    else
        _kg_debounce_shquote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
        _kg_debounce_schedule() {
            # $1=PROJECT_ROOT $2=file $3=python $4=workdir $5=cmd $6=channel(unused)
            ( cd "$4" 2>/dev/null || true; eval "$5" ) &
        }
    fi

    # shellcheck source=code-extensions.sh disable=SC1091
    if [ -f "$_VCO_ROUTE_HOOKS_DIR/_lib/code-extensions.sh" ]; then
        . "$_VCO_ROUTE_HOOKS_DIR/_lib/code-extensions.sh"
    fi

    # Resolve project_id once for the access checks below.
    VCT_PROJECT_ID="${VCT_PROJECT_ID:-}"
    if [ -z "$VCT_PROJECT_ID" ] && [ -f "$_VCO_ROUTE_PROJECT_ROOT/.claude/env" ]; then
        # Best-effort read of VCT_PROJECT_ID from .claude/env (not a bash
        # `source` — we don't want to inherit other env). The line rule is
        # `vco_lib.envfile.parse_env_lines`: an optional `export ` prefix (the
        # form the projection's managed block WRITES — v0.2.97: the old
        # pattern required the bare `VCT_PROJECT_ID=` form and never matched
        # it), first match wins, one matching pair of quotes stripped.
        # MUST MATCH route-touched-path.ps1 (parity test:
        # tests/test_v0297_route_project_id_parity.py).
        VCT_PROJECT_ID=$(grep -E '^[[:space:]]*(export[[:space:]]+)?VCT_PROJECT_ID=' "$_VCO_ROUTE_PROJECT_ROOT/.claude/env" 2>/dev/null \
            | head -1 \
            | sed -E -e 's/^[[:space:]]*(export[[:space:]]+)?VCT_PROJECT_ID=[[:space:]]*//' -e 's/[[:space:]]+$//' \
                     -e 's/^"(.*)"$/\1/' -e "s/^'(.*)'\$/\\1/")
    fi

    # Resolve the access-matrix checker path ONCE, synchronously — mirrors
    # post-file-edit.ps1's $AccessCheckPs1 resolution. Safe to do at schedule
    # time (not eval time): the checker script is a static repo file that
    # cannot appear/disappear within a debounce window.
    _KG_ACCESS_CHECKER=""
    if [ -x "$_VCO_ROUTE_PROJECT_ROOT/templates/scripts/vct_access_check.sh" ]; then
        _KG_ACCESS_CHECKER="$_VCO_ROUTE_PROJECT_ROOT/templates/scripts/vct_access_check.sh"
    elif [ -x "$_VCO_ROUTE_PROJECT_ROOT/.claude/scripts/vct_access_check.sh" ]; then
        _KG_ACCESS_CHECKER="$_VCO_ROUTE_PROJECT_ROOT/.claude/scripts/vct_access_check.sh"
    fi
}

# ===========================================================================
# vco_route_touched_path <absolute_path> [session_id]
# ===========================================================================
# The whole routing decision for ONE file. Idempotent per file per debounce
# window; safe to call in a loop over several paths.
vco_route_touched_path() {
    local touched="${1:-}"
    local session_id="${2:-}"
    [ -n "$touched" ] || return 0
    [ -n "${_VCO_ROUTE_PROJECT_ROOT:-}" ] || return 0

    local PROJECT_ROOT="$_VCO_ROUTE_PROJECT_ROOT"
    local PY="$_VCO_ROUTE_PY"

    # 1. Auto-sync knowledge graph files (background side-effect).
    # D-9 (v0.2.73): match on "<root>/" not "<root>" so sibling directories
    # (knowledge_base/, knowledge-old/) don't sync into the KG collection.
    if [[ "$touched" == "$KNOWLEDGE_ROOT"/* ]]; then
        local REL_PATH="${touched#$PROJECT_ROOT/}"
        cd "$PROJECT_ROOT" || return 0
        # v0.2.49 Phase 8: gate the sync on access-matrix write permission.
        # KG_COLLECTION is the target Weaviate class for primary-KG writes.
        # 2026-06-18: debounced. The gate runs at SYNC time (inside the
        # quoted command the flusher eval's), not at schedule time, so a
        # coalesced burst still consults the access matrix exactly once when
        # the deferred sync fires. The sync re-reads the file from disk, so
        # the latest content lands.
        local _KG_SYNC_CMD
        _KG_SYNC_CMD="$(_kg_build_gated_sync_cmd "$VCT_PROJECT_ID" "${KG_COLLECTION:-}" ".claude/scripts/kg-sync $(_kg_debounce_shquote "$REL_PATH")")"
        _kg_debounce_schedule "$PROJECT_ROOT" "$touched" "$PY" "$PROJECT_ROOT" "$_KG_SYNC_CMD" "kg"

        _vco_route_knowledge_dup_scan
    fi

    # 2. Auto-sync development documentation files (background side-effect).
    # D-9 (v0.2.73): trailing slash — don't match docs-archive/, docsite/, etc.
    local DOCS_DIR="$PROJECT_ROOT/docs"
    if [[ "$touched" == "$DOCS_DIR"/* ]] && [[ "$touched" == *.md ]]; then
        local REL_PATH="${touched#$PROJECT_ROOT/}"
        cd "$PROJECT_ROOT" || return 0
        # v0.2.49 Phase 8: gate docs sync on access-matrix write permission
        # against DEVELOPMENT_COLLECTION (the docs/ target). Same
        # coalesce-rapid-repeats semantics as the knowledge/ branch above.
        local _DOCS_SYNC_CMD
        _DOCS_SYNC_CMD="$(_kg_build_gated_sync_cmd "$VCT_PROJECT_ID" "${DEVELOPMENT_COLLECTION:-}" ".claude/scripts/kg-sync $(_kg_debounce_shquote "$REL_PATH")")"
        _kg_debounce_schedule "$PROJECT_ROOT" "$touched" "$PY" "$PROJECT_ROOT" "$_DOCS_SYNC_CMD" "docs"
    fi

    # 2b. Auto-index diagrams (Phase 1.5 — Mermaid + Excalidraw).
    _vco_route_diagram "$touched"

    # 3. Code file changes: end-of-turn batched code graph drain + reminder.
    # v0.2.73 (FIX-B): the per-edit code-graph sync is REMOVED. The edited
    # path is APPENDED to a per-turn drain queue (+ the reminder accumulator),
    # and the Stop hook `stop-codegraph-drain.sh` runs ONE analyzer pass at
    # end-of-turn over ALL the turn's files, GROUPED BY CANONICAL ROOT,
    # rate-limited to once per 120 s per project. The per-edit sync hit the
    # big CodeFunction collection's insert-time HNSW churn on every keystroke
    # and drove a measured 857 GB/8 h of disk write-amplification.
    #
    # v0.2.95: the extension test comes from _lib/code-extensions.sh (ONE
    # home). A partial install without that helper routes NOTHING to the
    # drain rather than matching every path on an empty regex — and SAYS so
    # (ship-gate review MAJOR-2: routing no code file at all, silently and by
    # stated choice, is the same silent-no-op class as a missing routing lib;
    # the SessionStart probe covers the file too). The notice goes through
    # this lib's existing nudge channel, so the calling hook still emits
    # exactly ONE envelope.
    if ! command -v vco_is_code_file >/dev/null 2>&1; then
        if command -v vco_report_missing_hook_lib >/dev/null 2>&1; then
            vco_report_missing_hook_lib "$_VCO_ROUTE_HOOKS_DIR" "$PROJECT_ROOT" \
                "$session_id" "code-extensions.sh"
            [ -n "${VCO_MISSING_LIB_NOTICE:-}" ] \
                && _vco_route_add_nudge "$VCO_MISSING_LIB_NOTICE"
        fi
    elif vco_is_code_file "$touched"; then
        # NOTE: the accumulator append serves BOTH the end-of-turn reminder
        # (P6) AND the batched code-graph drain (FIX-B). Keep the FULL path
        # (the drain needs it to resolve the canonical root; the reminder
        # reduces to basenames itself).
        #
        # v0.2.72 P6: reminder AGGREGATION — appending here instead of
        # emitting means the Stop hook emits ONE reminder naming all edited
        # files rather than ~15 per busy turn. Soft-fail: an unkeyable
        # session (empty id) just skips accumulation.
        if [ -n "$session_id" ]; then
            mkdir -p "$PROJECT_ROOT/.claude/state" 2>/dev/null || true
            local _EDIT_ACCUM="$PROJECT_ROOT/.claude/state/edit_reminder_${session_id}.txt"
            printf '%s\n' "$touched" >> "$_EDIT_ACCUM" 2>/dev/null || true
            # v0.2.73 (FIX-B): SEPARATE code-graph drain queue. The reminder
            # accumulator above is drained + cleared EVERY turn by
            # stop-codegraph-reminder.sh; the drain queue below is drained by
            # stop-codegraph-drain.sh, which is RATE-LIMITED (once per 120 s)
            # and must PERSIST the union of edited paths across turns inside
            # the window (it clears the queue only when it actually runs the
            # analyzer). A distinct file avoids a two-consumer race.
            local _CG_DRAIN_QUEUE="$PROJECT_ROOT/.claude/state/codegraph_drain_${session_id}.txt"
            printf '%s\n' "$touched" >> "$_CG_DRAIN_QUEUE" 2>/dev/null || true
        fi
    fi
}

# --- knowledge/ side effects: every-10-writes duplicate scan --------------
_vco_route_knowledge_dup_scan() {
    local PROJECT_ROOT="$_VCO_ROUTE_PROJECT_ROOT"
    local EDIT_COUNT_FILE="$PROJECT_ROOT/.claude/logs/.kg_edit_count"
    mkdir -p "$PROJECT_ROOT/.claude/logs"
    local COUNT=0
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
    local _DUP_REPORT="$PROJECT_ROOT/.claude/state/kg_duplicates_report.txt"
    if [ $((COUNT % 10)) -eq 0 ]; then
        # D-8 (v0.2.73): the every-10-edits duplicate scan previously
        # greped its ✅/⚠️/📊 summary to PLAIN stdout, which PostToolUse
        # hooks silently drop — the advertised feature had no consumer.
        # Persist the summary to a report file instead; the NEXT
        # synchronous hook fire surfaces it. The scan itself still runs
        # backgrounded so the hook never blocks.
        mkdir -p "$PROJECT_ROOT/.claude/state" 2>/dev/null || true
        (
            # ❌ is kept too: the scan's failure line (`❌ Error during
            # duplicate detection: …`) is what the ⚠️ "See the error above."
            # verdict points at — filtering it out would surface a report
            # that names an error the reader cannot see.
            #
            # v0.2.94 (review item 3): `^<tool>: ERROR` joins the pattern. The
            # wrapper's REFUSAL — no qualifying interpreter — carried none of
            # the four markers, so `$_dup_out` came back empty and this scan
            # was a SILENT no-op on exactly the installs that had something to
            # report. The wrapper now also ends its refusal with a ⚠️ line
            # (`vct_venv_ladder_refusal_summary`), so either half alone would
            # surface it; both exist because a consumer that must REMEMBER a
            # convention is a consumer that will forget it.
            _dup_out=$(.claude/scripts/kg-duplicates --threshold 0.95 2>&1 \
                | head -c 204800 | head -200 \
                | grep -E "(✅|⚠️|📊|❌|^[A-Za-z0-9_-]+: ERROR)" || true)
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
    if [ -f "$_DUP_REPORT" ]; then
        local _dup_body
        _dup_body=$(cat "$_DUP_REPORT" 2>/dev/null || true)
        if [ -n "$_dup_body" ]; then
            _vco_route_add_nudge "[KG duplicate scan] The periodic duplicate check found candidates worth reviewing:
$_dup_body
Run \`.claude/scripts/kg-duplicates\` for detail, or ignore if these are intentional siblings."
        fi
        rm -f "$_DUP_REPORT" 2>/dev/null || true
    fi
}

# --- .claude/diagrams/ side effects ---------------------------------------
# Fires on any change under .claude/diagrams/. Throttled to 60 s per file to
# avoid re-indexing during rapid in-editor save bursts. Sidecar `.meta.json`
# writes are NOT re-indexed (would infinite-loop).
_vco_route_diagram() {
    local touched="$1"
    local PROJECT_ROOT="$_VCO_ROUTE_PROJECT_ROOT"
    local PY="$_VCO_ROUTE_PY"
    local SCRIPT_DIR="$_VCO_ROUTE_HOOKS_DIR"
    local DIAGRAMS_DIR="$PROJECT_ROOT/.claude/diagrams"

    [[ "$touched" == "$DIAGRAMS_DIR"/* ]] || return 0
    [[ "$touched" != *.meta.json ]] || return 0
    [[ "$touched" == *.mmd || "$touched" == *.excalidraw ]] || return 0

    # 60 s per-file throttle. bash 3.2 compatible (no associative arrays),
    # file-as-set semantics.
    local THROTTLE_DIR="$PROJECT_ROOT/.claude/state"
    mkdir -p "$THROTTLE_DIR" 2>/dev/null || true
    # Hash the path (md5 via Python — portable). Avoids slashes in the key.
    local DIAGRAM_HASH
    DIAGRAM_HASH=$(printf '%s' "$touched" \
        | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" \
        2>/dev/null || echo "_")
    local THROTTLE_FILE="$THROTTLE_DIR/diagram_idx_${DIAGRAM_HASH}.ts"
    local NOW_TS LAST_TS=0
    NOW_TS=$(date +%s)
    if [ -f "$THROTTLE_FILE" ]; then
        LAST_TS=$(cat "$THROTTLE_FILE" 2>/dev/null || echo 0)
        case "$LAST_TS" in
            ''|*[!0-9]*) LAST_TS=0 ;;
        esac
    fi
    local AGE=$(( NOW_TS - LAST_TS ))
    [ "$AGE" -ge 60 ] || return 0
    echo "$NOW_TS" > "$THROTTLE_FILE" 2>/dev/null || true

    # Resolve venv-Python so `import vco_lib.diagram_indexer` works. The
    # shared resolver NEVER falls back to $PROJECT_ROOT/.venv (the user's
    # venv, which won't have weaviate-client + vco_lib). When no VCO venv
    # is resolvable we degrade to $PY so the indexer's import-time error
    # surfaces as a real ImportError rather than running the wrong
    # interpreter.
    # shellcheck source=resolve-vco-venv.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
    resolve_vco_venv_python "$SCRIPT_DIR"
    local _DIAG_VENV="${VCO_VENV_PYTHON:-$PY}"

    # Pass --diagrams-collection when DIAGRAMS_COLLECTION is set in the env.
    # Without this kwarg the indexer's Weaviate upsert silently skips even
    # though SQLite + sidecar still happen. Older projects without the key
    # get the legacy sidecar-only behaviour automatically.
    local _DIAG_ARGS=( -m vco_lib.diagram_indexer index "$touched" )
    if [ -n "${DIAGRAMS_COLLECTION:-}" ]; then
        _DIAG_ARGS+=( --diagrams-collection "$DIAGRAMS_COLLECTION" )
    fi

    # Index + snapshot in a SERIAL background chain. R2 (code review
    # 2026-05-25): in PARALLEL the snapshot CLI's `project_diagrams WHERE
    # project_id=? AND file_path=?` lookup could run before the indexer
    # UPSERT'd the row → first-version content lost forever.
    (
        "$_DIAG_VENV" "${_DIAG_ARGS[@]}" >/dev/null 2>&1 || true
        "$_DIAG_VENV" -m vco_lib.diagram_indexer snapshot create \
            "$touched" --quiet >/dev/null 2>&1 || true
    ) &
}
