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
#   <code extension>         -> the per-turn code-graph drain queue, OR the
#                               per-EXTRA queue when the path sits under an
#                               enabled extra code-graph path (v0.2.102 S16-7)
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

# v0.2.49 SB1: record an UPDATE_DEFERRED.md entry directing the user to
# resolve the empty-VCT_PROJECT_ID condition (re-run install.py --update OR
# re-register via Launcher GUI). Silent-allow remains the default at the gate;
# this entry is the user-facing surface.
#
# v0.2.100 WP-17: the entry is written by the ONE locked writer,
# `python -m vco_lib.gate_skipped_deferral` (shared with the weaviate-kg MCP
# server), never by an append from here. The old bare `>>` took no lock: it
# raced install.py's finalize (which reads, merges and rewrites the ledger
# under `.claude/context/.update-deferred.lock`) and could lose the entry or
# another writer's. Writers append/merge, never clobber.
#
# Idempotency: deduped per (session, project) via a sentinel file in
# .claude/state/ so a kg-sync burst spawns the writer once. The sentinel is
# removed again when the write fails, so the next write retries. No VCO venv
# (a broken install) is said on stderr — never an unlocked fallback write.
# MUST MATCH route-touched-path.ps1's Emit-KgGateSkippedDeferral.
_kg_emit_gate_skipped_deferral() {
    local coll="${1:-}"
    local state_dir="$_VCO_ROUTE_PROJECT_ROOT/.claude/state"
    local session_id="${VCT_SESSION_ID:-${CLAUDE_SESSION_ID:-$$}}"
    local sentinel="$state_dir/gate_skipped_deferral_${session_id}"

    [ -n "$_VCO_ROUTE_PROJECT_ROOT" ] || return 0
    # Per-session dedup. The first call writes; subsequent calls within
    # the same session are no-ops.
    [ -f "$sentinel" ] && return 0
    mkdir -p "$state_dir" 2>/dev/null || return 0
    : > "$sentinel" 2>/dev/null || true

    VCO_VENV_PYTHON=""
    if [ -f "$_VCO_ROUTE_HOOKS_DIR/_lib/resolve-vco-venv.sh" ]; then
        # shellcheck source=resolve-vco-venv.sh disable=SC1091
        . "$_VCO_ROUTE_HOOKS_DIR/_lib/resolve-vco-venv.sh"
        resolve_vco_venv_python "$_VCO_ROUTE_HOOKS_DIR" 2>/dev/null || true
    fi
    if [ -z "${VCO_VENV_PYTHON:-}" ]; then
        echo "[VCO] gate_skipped_no_project_id: the deferral entry was NOT recorded -- the VCO venv is not resolvable (broken install; re-run the orchestrator's install / update)." >&2
        rm -f "$sentinel" 2>/dev/null || true
        return 0
    fi
    if ! "$VCO_VENV_PYTHON" -m vco_lib.gate_skipped_deferral \
            --folder "$_VCO_ROUTE_PROJECT_ROOT" --collection "$coll" --surface hook \
            </dev/null >/dev/null; then
        rm -f "$sentinel" 2>/dev/null || true
    fi
    return 0
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
# v0.2.102 S16-7: enabled-extra-path membership (cached list, option B)
# ===========================================================================
# A code file under an ENABLED extra code-graph path (launcher → project →
# Codegraph → "Extra codegraph paths") must be queued for re-index as
# promptly as a primary-root file — but into a PER-EXTRA queue
# (codegraph_drain_extra_<md5(extra)>_<session>.txt), never the primary
# queue: a path outside the primary root must never be analyzed under the
# primary's collection. The Stop hook (stop-codegraph-drain.sh) drains those
# queues through `python -m vco_lib.codegraph_extras_refresh --queued`.
#
# The membership DECISION is computed by Python (the hub /config resolver is
# the one reader of the extras rows) and cached as a plain table both shells
# read — option B of the A>B>C rule, chosen because this runs PER EDIT: a
# hub resolve (or even a Python spawn) per keystroke is too costly, and the
# list changes only when the user toggles extras in the launcher GUI.
#   cache : .claude/state/codegraph_extra_paths_cache.txt (one path per line)
#   stale : missing, older than VCO_CODEGRAPH_EXTRAS_LIST_TTL_SECONDS
#           (default 300), or older than launcher.db (an extras add / remove /
#           toggle takes effect within seconds, not at TTL expiry)
#   refresh: "$venv_py" -m vco_lib.codegraph_extras_refresh \
#              --list-enabled-extra-paths --project-root <root>
# A failed refresh (no venv, hub down — the CLI exits non-zero) keeps the
# stale cache; with no cache at all nothing routes to the extra queues (the
# commit-stamp refresh at Stop still re-indexes stale extras — soft-fail).
# MUST MATCH route-touched-path.ps1.
_VCO_ROUTE_EXTRAS_CACHE_NAME="codegraph_extra_paths_cache.txt"

_vco_route_extras_db_path() {
    if [ -n "${VCT_LAUNCHER_DB_PATH:-}" ]; then
        printf '%s' "$VCT_LAUNCHER_DB_PATH"
    else
        printf '%s' "${VCT_STATE_DIR:-$HOME/.vct}/launcher.db"
    fi
}

# Returns 0 when the cache is STALE (missing / TTL-expired / older than
# launcher.db), 1 when fresh. Soft-fail biases to "stale": a spurious refresh
# costs one Python spawn; a never-refreshing cache silently misroutes.
_vco_route_extras_cache_stale() {
    local cache="$1"
    [ -f "$cache" ] || return 0
    local ttl="${VCO_CODEGRAPH_EXTRAS_LIST_TTL_SECONDS:-300}"
    case "$ttl" in ''|*[!0-9]*) ttl=300 ;; esac
    local now cache_mtime db_mtime
    now=$(date +%s 2>/dev/null || echo 0)
    cache_mtime=$(stat -c %Y "$cache" 2>/dev/null || stat -f %m "$cache" 2>/dev/null || echo 0)
    case "$cache_mtime" in ''|*[!0-9]*) return 0 ;; esac
    if [ $(( now - cache_mtime )) -ge "$ttl" ]; then
        return 0
    fi
    local db
    db="$(_vco_route_extras_db_path)"
    if [ -f "$db" ]; then
        db_mtime=$(stat -c %Y "$db" 2>/dev/null || stat -f %m "$db" 2>/dev/null || echo 0)
        case "$db_mtime" in ''|*[!0-9]*) db_mtime=0 ;; esac
        if [ "$db_mtime" -gt "$cache_mtime" ]; then
            return 0
        fi
    fi
    return 1
}

# Best-effort cache rewrite through the ONE Python lister. Never fails the
# caller: a non-zero CLI exit (hub down / project unregistered) removes the
# temp file and leaves any previous cache in place.
_vco_route_refresh_extras_cache() {
    local cache="$1"
    VCO_VENV_PYTHON=""
    if [ -f "$_VCO_ROUTE_HOOKS_DIR/_lib/resolve-vco-venv.sh" ]; then
        # shellcheck source=resolve-vco-venv.sh disable=SC1091
        . "$_VCO_ROUTE_HOOKS_DIR/_lib/resolve-vco-venv.sh"
        resolve_vco_venv_python "$_VCO_ROUTE_HOOKS_DIR" 2>/dev/null || true
    fi
    [ -n "${VCO_VENV_PYTHON:-}" ] || return 0
    local tmp="$cache.tmp.$$"
    if "$VCO_VENV_PYTHON" -m vco_lib.codegraph_extras_refresh \
            --list-enabled-extra-paths --project-root "$_VCO_ROUTE_PROJECT_ROOT" \
            </dev/null >"$tmp" 2>/dev/null; then
        mv "$tmp" "$cache" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
    else
        rm -f "$tmp" 2>/dev/null || true
    fi
    return 0
}

# Resolve symlinks in $1's DIRECTORY portion with the `cd -P` builtin — no
# subprocess, so this costs nothing per edit (v0.2.102 L-A2: registration
# canonicalises extras via dunce::canonicalize, but the touched path arrives
# as the editor wrote it; an edit through a symlinked directory must still
# prefix-match). On any failure (deleted dir, no slash) echoes the input.
_vco_route_resolve_physical() {
    local p="$1" d b rd
    case "$p" in
        */*) ;;
        *) printf '%s' "$p"; return 0 ;;
    esac
    d="${p%/*}"
    b="${p##*/}"
    rd=$(cd "$d" 2>/dev/null && pwd -P) || rd=""
    if [ -z "$rd" ]; then
        printf '%s' "$p"
    elif [ "$rd" = "/" ]; then
        printf '/%s' "$b"
    else
        printf '%s/%s' "$rd" "$b"
    fi
    return 0
}

# Route a code edit under an ENABLED extra path to its per-extra drain queue:
# appends the PHYSICAL path to codegraph_drain_extra_<md5(extra)>_<session>.txt
# and echoes that queue path; echoes nothing (caller then uses the primary
# queue) when the edit is not under any enabled extra. $1 = absolute touched
# path, $2 = session id.
#
# v0.2.102 fix-round (M-A1) precedence, MUST MATCH route-touched-path.ps1:
#   1. PRIMARY-ROOT CONTAINMENT WINS — an edit inside the project root always
#      goes to the primary queue, even when a registered extra is an ANCESTOR
#      of the root (registration rejects ancestor extras too, but a pre-fix
#      row can still be on disk).
#   2. Among extras the LONGEST matching prefix wins — nested extras route to
#      the inner one regardless of cache order.
#   3. Matching is on the SYMLINK-RESOLVED path; trailing separators on the
#      cache line are ignored for the match but the RAW line is hashed (the
#      queue name must equal vco_lib.path_key(row.path), trailing slash and
#      all, or the Python drain's glob never finds it).
_vco_route_extra_enqueue() {
    local touched="$1" session_id="$2"
    local state_dir="$_VCO_ROUTE_PROJECT_ROOT/.claude/state"
    local cache="$state_dir/$_VCO_ROUTE_EXTRAS_CACHE_NAME"

    # 1. Primary-root containment wins — checked on the resolved path so a
    # symlinked route INTO the root also lands in the primary queue.
    local resolved
    resolved="$(_vco_route_resolve_physical "$touched")"
    case "$resolved" in
        "$_VCO_ROUTE_PROJECT_ROOT"/*) return 0 ;;
    esac

    if _vco_route_extras_cache_stale "$cache"; then
        mkdir -p "$state_dir" 2>/dev/null || true
        _vco_route_refresh_extras_cache "$cache"
    fi
    [ -f "$cache" ] || return 0
    # 2. Longest matching prefix wins.
    local _ep _epm _best="" _best_raw=""
    while IFS= read -r _ep; do
        # A CRLF cache (a refresh written by a Windows-side Python, read back
        # under WSL/Git-bash) must not poison the prefix match or the hash.
        _ep="${_ep%$'\r'}"
        [ -n "$_ep" ] || continue
        # 3. Trailing separators are ignored for the MATCH (the .ps1 sibling's
        # TrimEnd); the raw line is kept for the hash.
        _epm="$_ep"
        while [ "${_epm%/}" != "$_epm" ]; do _epm="${_epm%/}"; done
        [ -n "$_epm" ] || continue
        case "$resolved" in
            "$_epm"/*)
                if [ ${#_epm} -gt ${#_best} ]; then
                    _best="$_epm"
                    _best_raw="$_ep"
                fi
                ;;
        esac
    done < "$cache"
    [ -n "$_best" ] || return 0
    # md5 of the RAW extra path — the SAME key vco_lib's path_key() computes,
    # so the queue name matches what the Python drain globs for. Unkeyable
    # (no python) → no extra routing.
    local _hash
    _hash=$(printf '%s' "$_best_raw" | "$_VCO_ROUTE_PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" 2>/dev/null || echo "")
    [ -n "$_hash" ] || return 0
    printf '%s\n' "$resolved" >> "$state_dir/codegraph_drain_extra_${_hash}_${session_id}.txt" 2>/dev/null || return 0
    printf '%s\n' "$state_dir/codegraph_drain_extra_${_hash}_${session_id}.txt"
    return 0
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
        # it), LAST match wins (what `source` gives — a user's override after
        # the managed block counts; v0.2.102, this took the first before), one
        # matching pair of quotes stripped.
        # MUST MATCH route-touched-path.ps1 and
        # `vco_lib.envfile.env_value(..., last_wins=True)` (parity test:
        # tests/test_v0297_route_project_id_parity.py).
        VCT_PROJECT_ID=$(grep -E '^[[:space:]]*(export[[:space:]]+)?VCT_PROJECT_ID=' "$_VCO_ROUTE_PROJECT_ROOT/.claude/env" 2>/dev/null \
            | tail -n 1 \
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
            #
            # v0.2.102 S16-7: a code file under an ENABLED extra code-graph
            # path goes to the PER-EXTRA queue instead — a path outside the
            # primary root must never be analyzed under the primary's
            # collection. The extra queue is drained by the same Stop hook
            # through `vco_lib.codegraph_extras_refresh --queued` (scoped
            # --only-files-from, the extra's own --project prefix).
            # _vco_route_extra_enqueue appends the RESOLVED path itself and
            # echoes the queue it used; empty → primary queue. Precedence
            # (M-A1): primary-root containment wins, then longest extra prefix.
            local _EXTRA_QUEUE
            _EXTRA_QUEUE="$(_vco_route_extra_enqueue "$touched" "$session_id")"
            if [ -z "$_EXTRA_QUEUE" ]; then
                local _CG_DRAIN_QUEUE="$PROJECT_ROOT/.claude/state/codegraph_drain_${session_id}.txt"
                printf '%s\n' "$touched" >> "$_CG_DRAIN_QUEUE" 2>/dev/null || true
            fi
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

# --- .claude/diagrams/ side effects: REMOVED v0.2.102 ----------------------
# The diagram auto-index leg (Phase 1.5, vco_lib.diagram_indexer) was
# removed with the diagrams integrations (owner ruling 2026-10-07).
