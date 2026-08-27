# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/query-cache.sh — shared TTL result-cache for the code-graph / KG
# injection queries issued by the context hooks (pre-edit, pre-bash,
# pre-tool-use Read/Grep, subagent-start).
#
# Why this exists (v0.2.77 Part 9 task 2)
# ---------------------------------------
# Every injection surface re-issues the SAME expensive Weaviate+embed query
# many times per session (the audit observed 5x identical queries in one
# hour). Each miss costs ~1.3 s (interpreter+import+embed+query). The
# pre-edit hook already had a PER-FILE cache; this generalises it to a
# SHARED, cross-surface, cross-file cache keyed on the query itself so a
# symbol queried by a Grep is served from cache when a later Read of the
# same symbol's file re-queries it (and vice-versa).
#
# One home (CLAUDE.md "search before add"): this is the single cache
# implementation. codegraph_query_block (all four code-graph surfaces) and
# the KG-search wrappers call vco_query_cache_get / vco_query_cache_put.
# No inline copy anywhere. MUST MATCH templates/hooks/_lib/query-cache.ps1.
#
# Semantics
# ---------
#   - Stores the RAW producer block (pre-dedup). Per-session dedup is applied
#     by the CALLER after the cached value is returned, so caching raw keeps
#     dedup state accurate (a node seen since the cache was written is still
#     filtered out on replay). This mirrors the pre-edit hook's raw-cache
#     invariant.
#   - Caches EMPTY results too (via a one-byte sentinel file) so a symbol that
#     genuinely returns nothing isn't re-queried every call within the TTL.
#   - TTL default 900 s (15 min); override with VCO_QUERY_CACHE_TTL.
#   - Best-effort throughout: any cache error falls back to running the query
#     live. A broken cache must never break injection.
#
# This file is sourced, never executed — no shebang. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if [ -n "${_VCO_QUERY_CACHE_SOURCED:-}" ]; then
    return 0 2>/dev/null || true
fi
_VCO_QUERY_CACHE_SOURCED=1

# Default TTL (seconds). 15 min balances "same symbol re-queried within a
# task" against "the code graph changed mid-session". Tunable per-project.
_VCO_QUERY_CACHE_TTL_DEFAULT=900

# vco_query_cache_dir — resolve (and create) the cache directory. Echoes the
# path, or empty on failure. Lives under .claude/state/ (gitignored, GC'd).
vco_query_cache_dir() {
    local root="${PROJECT_ROOT:-${CLAUDE_PROJECT_DIR:-}}"
    [ -n "$root" ] || return 1
    local dir="$root/.claude/state/query_cache"
    mkdir -p "$dir" 2>/dev/null || return 1
    printf '%s' "$dir"
}

# vco_query_cache_key <surface> <query> [extra...] — deterministic key for the
# cache file. Hashes all args together (surface namespaces the key so a Grep
# and a Read of the same symbol can share OR separate as the caller chooses).
# Uses python hashlib for portability (md5sum is GNU-only, absent on macOS).
vco_query_cache_key() {
    local joined
    # Join args with a NUL-ish separator that can't appear in the inputs.
    joined="$(printf '%s\x1f' "$@")"
    if [ -n "${PY:-}" ]; then
        printf '%s' "$joined" | "$PY" -c "import hashlib,sys; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest())" 2>/dev/null && return 0
    fi
    # Fallback: sanitized concatenation (degrades to weaker keying, still
    # correct — just longer filenames / more collisions on exotic input).
    printf '%s' "$joined" | tr -c 'a-zA-Z0-9' '_' | head -c 96
}

# vco_query_cache_get <key> — if a fresh (within-TTL) cache entry exists for
# <key>, print its stored blob to stdout and return 0. An empty-result entry
# prints nothing and still returns 0 (the caller must distinguish "cached
# empty" from "miss" via the return code, NOT via output emptiness). Return
# non-zero on miss / stale / error so the caller runs the query live.
vco_query_cache_get() {
    local key="$1"
    [ -n "$key" ] || return 1
    local dir
    dir="$(vco_query_cache_dir)" || return 1
    local f="$dir/$key"
    [ -f "$f" ] || return 1
    local ttl="${VCO_QUERY_CACHE_TTL:-$_VCO_QUERY_CACHE_TTL_DEFAULT}"
    local mtime
    mtime=$(stat -c '%Y' "$f" 2>/dev/null || stat -f '%m' "$f" 2>/dev/null || echo "")
    if [ -z "$mtime" ] && [ -n "${PY:-}" ]; then
        mtime=$("$PY" -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$f" 2>/dev/null || echo 0)
    fi
    [ -z "$mtime" ] && mtime=0
    local age=$(( $(date +%s) - mtime ))
    if [ "$age" -ge "$ttl" ]; then
        return 1
    fi
    # Fresh hit. The stored file is the raw blob (may be empty for a cached
    # empty result). Emit it verbatim.
    cat "$f" 2>/dev/null || true
    return 0
}

# vco_query_cache_put <key> <blob> — store <blob> (may be empty) for <key>.
# Also opportunistically GCs stale entries so the dir stays bounded. Soft-fail.
vco_query_cache_put() {
    local key="$1"
    local blob="$2"
    [ -n "$key" ] || return 0
    local dir
    dir="$(vco_query_cache_dir)" || return 0
    # Atomic write: temp then rename, so a concurrent reader never sees a
    # partially-written blob.
    local f="$dir/$key"
    local tmp="$f.$$.tmp"
    printf '%s' "$blob" > "$tmp" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 0; }
    mv -f "$tmp" "$f" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 0; }
    # Opportunistic GC: prune entries older than 2x the TTL (well past useful
    # life). Cheap `find -mtime`; runs at most once per put. Bounded state.
    local ttl="${VCO_QUERY_CACHE_TTL:-$_VCO_QUERY_CACHE_TTL_DEFAULT}"
    # find -mmin takes minutes; 2x TTL in minutes, min 1.
    local gc_min=$(( (ttl * 2) / 60 ))
    [ "$gc_min" -lt 1 ] && gc_min=1
    find "$dir" -maxdepth 1 -type f -mmin "+$gc_min" -delete 2>/dev/null || true
    return 0
}

# vco_kg_search_cached <venv> <rl_script> <query> <limit> — run the RL-aware KG
# search (rl_kg_search.py --hook-format) through the shared TTL cache. Echoes
# the raw "KG:"-prefixed block(s), served from cache on a repeat query.
#
# Same one-home rationale as codegraph_query_block: pre-edit and pre-bash each
# invoked rl_kg_search.py inline with identical shape; this wraps that call so
# a symbol/query re-searched within the TTL is served from disk (~ms) instead
# of paying the ~1.3 s interpreter+import+embed+query round-trip again.
#
# NOTE: the KG cache is keyed on the "kg" surface + query + limit so it does
# NOT collide with the "cg" code-graph entries. The result is RAW (pre-dedup);
# the caller dedups per-session via the seen-store, so cached replays stay
# dedup-accurate. Empty results ARE cached (an empty symbol isn't re-queried).
# Best-effort: missing cache helper OR missing venv/script -> falls back to a
# direct call (pre-edit/pre-bash already guard the venv/script existence).
vco_kg_search_cached() {
    local venv="$1"
    local rl_script="$2"
    local query="$3"
    local limit="${4:-1}"
    [ -n "$query" ] || return 0
    [ -n "$venv" ] || return 0
    [ -f "$rl_script" ] || return 0

    local _key=""
    if command -v vco_query_cache_key >/dev/null 2>&1; then
        _key="$(vco_query_cache_key "kg" "$query" "$limit")"
    fi
    if [ -n "$_key" ] && command -v vco_query_cache_get >/dev/null 2>&1; then
        local _hit
        if _hit="$(vco_query_cache_get "$_key")"; then
            [ -n "$_hit" ] && printf '%s\n' "$_hit"
            return 0
        fi
    fi

    # Cache miss — run the live search. The producers cap themselves; the
    # caller historically `head -40`'d, so we do the same here for parity.
    local _out
    _out="$("$venv" "$rl_script" "$query" --limit "$limit" --hook-format 2>/dev/null | head -40 || true)"
    if [ -n "$_key" ] && command -v vco_query_cache_put >/dev/null 2>&1; then
        vco_query_cache_put "$_key" "$_out"
    fi
    [ -n "$_out" ] && printf '%s\n' "$_out"
    return 0
}

# --- P2 (v0.2.91): ONE interpreter for the KG + code-graph pair --------------
#
# The pre-edit miss path used to start TWO full CPython processes (rl_kg_search.py
# and code-graph-query) for the same query. The 2026-08-27 perf audit measured
# that miss at 1.50 s wall of which ~3 ms is the Weaviate query and ~58 ms the
# query embed — the rest is interpreter start + `import weaviate` (0.41 s alone)
# + client connect, paid twice for two processes that import the same modules.
# `hook_dual_search.py` runs both legs in one process and pays the import tax
# once (expected 1.5 s -> ~0.9-1.0 s).
#
# ZERO FUNCTIONALITY CHANGE is the contract, and it is preserved on all three
# axes that could have drifted:
#   * QUERIES — the driver calls the SAME entry points with the SAME argv, so
#     the two emitted blocks are byte-identical (golden-output diffed).
#   * OUTPUT CAPS — `head -40` (KG) / `head -20` (CG) still applied here, per
#     leg, exactly as vco_kg_search_cached / codegraph_query_block did.
#   * CACHE KEYS — each leg keeps its OWN pre-existing key ("kg"+query+limit /
#     "cg"+query+project_arg+limit+exclude+anchor), so a blob written here is
#     still a hit for pre-bash's KG query and pre-tool-use's code-graph query,
#     and vice-versa. Only the legs that MISS are sent to the driver.
#
# Args (bash-3.2 safe — positional, no arrays):
#   $1 kg_out_file   — file to receive the raw KG block ("" = KG leg disabled)
#   $2 cg_out_file   — file to receive the raw CODE block ("" = CG leg disabled)
#   $3 venv          — python interpreter (the resolved VCO venv)
#   $4 rl_script     — path to claude_mcp_servers/scripts/rl_kg_search.py
#   $5 query         — the shared query text
#   $6 kg_limit      — KG --limit (ignored when kg_out_file is "")
#   $7 cg_project_arg— "--project Foo" or "" (KEYING SHAPE — kept verbatim so the
#                      cache key matches codegraph_query_block's)
#   $8 cg_limit      — CG --limit (ignored when cg_out_file is "")
#   $9 cg_exclude    — CG --exclude-file
#   $10 cg_anchor    — CG --anchor
#
# Returns 0 always. Returns 1 ONLY as an internal signal that the caller should
# fall back to the legacy two-call path (driver missing / no venv / no markers in
# the output) — pre-edit checks that and degrades gracefully.
#
# ACCEPTED WORST CASE (v0.2.91 wave-4 NIT-5): the driver's inner `timeout 7`
# plus a subsequent LEGACY re-run can exceed the pre-edit hook's 8 s
# settings.json budget. Sequence: the driver hangs, is killed at 7 s, writes no
# marker for a leg we asked for, this function returns 1, and pre-edit then
# re-runs that leg on the two-process path — which starts after the 8 s budget
# has already been spent. The harness kills the hook mid-run. The consequence is
# FAIL-OPEN: no injection for that one Edit, nothing written, nothing corrupted;
# the next Edit is served from cache or retries cleanly. Deliberately not
# "fixed" by shrinking the inner timeout (7 s already sits inside the budget and
# only a genuinely hung backend reaches it) nor by suppressing the legacy
# fallback (that would silently drop a leg's injection on every driver hiccup —
# a worse, quieter failure than one missing injection).
vco_dual_search_cached() {
    local kg_out="$1" cg_out="$2" venv="$3" rl_script="$4" query="$5"
    local kg_limit="${6:-1}" cg_project_arg="$7" cg_limit="${8:-2}"
    local cg_exclude="$9" cg_anchor="${10:-}"

    [ -n "$query" ] || return 0
    local want_kg=0 want_cg=0
    [ -n "$kg_out" ] && want_kg=1
    [ -n "$cg_out" ] && want_cg=1
    [ "$want_kg" = "1" ] || [ "$want_cg" = "1" ] || return 0

    # --- 1. per-leg cache probe (same keys as the single-leg wrappers) ------
    local kg_key="" cg_key=""
    if command -v vco_query_cache_key >/dev/null 2>&1; then
        [ "$want_kg" = "1" ] && kg_key="$(vco_query_cache_key "kg" "$query" "$kg_limit")"
        [ "$want_cg" = "1" ] && cg_key="$(vco_query_cache_key "cg" "$query" "$cg_project_arg" "$cg_limit" "$cg_exclude" "$cg_anchor")"
    fi
    local need_kg="$want_kg" need_cg="$want_cg"
    local _hit
    if [ -n "$kg_key" ] && command -v vco_query_cache_get >/dev/null 2>&1; then
        if _hit="$(vco_query_cache_get "$kg_key")"; then
            printf '%s' "$_hit" > "$kg_out" 2>/dev/null || true
            need_kg=0
        fi
    fi
    if [ -n "$cg_key" ] && command -v vco_query_cache_get >/dev/null 2>&1; then
        if _hit="$(vco_query_cache_get "$cg_key")"; then
            printf '%s' "$_hit" > "$cg_out" 2>/dev/null || true
            need_cg=0
        fi
    fi
    # Everything served from disk — no interpreter at all.
    if [ "$need_kg" = "0" ] && [ "$need_cg" = "0" ]; then
        return 0
    fi

    # --- 2. resolve the driver; degrade to the legacy path when absent ------
    local driver="${rl_script%/*}/hook_dual_search.py"
    [ -n "$venv" ] || return 1
    [ -f "$driver" ] || return 1
    if [ "$need_kg" = "1" ] && [ ! -f "$rl_script" ]; then
        # KG producer genuinely absent (non-orchestrator project): that leg has
        # no result, which is the pre-P2 behaviour too. Disable it and continue.
        need_kg=0
        : > "$kg_out" 2>/dev/null || true
    fi
    local cg_script=""
    if [ "$need_cg" = "1" ]; then
        local cli=""
        if command -v vco_codegraph_cli >/dev/null 2>&1; then
            cli="$(vco_codegraph_cli || true)"
        fi
        if [ -n "$cli" ] && [ -f "${cli%/*}/query_code_graph.py" ]; then
            cg_script="${cli%/*}/query_code_graph.py"
        else
            need_cg=0
            : > "$cg_out" 2>/dev/null || true
        fi
    fi
    if [ "$need_kg" = "0" ] && [ "$need_cg" = "0" ]; then
        return 0
    fi

    # --- 3. ONE interpreter for whichever legs actually missed --------------
    # Positional argv build (bash-3.2 safe, space-safe for paths/queries).
    set -- --query "$query"
    if [ "$need_kg" = "1" ]; then
        set -- "$@" --kg-limit "$kg_limit"
    fi
    if [ "$need_cg" = "1" ]; then
        set -- "$@" --cg-limit "$cg_limit" --cg-script "$cg_script"
        # The project NAME for the CLI is derived from the keying shape
        # ("--project Foo"); the raw string stays the cache key.
        case "$cg_project_arg" in
            --project\ *) set -- "$@" --cg-project "${cg_project_arg#--project }" ;;
        esac
        [ -n "$cg_exclude" ] && set -- "$@" --cg-exclude-file "$cg_exclude"
        [ -n "$cg_anchor" ] && set -- "$@" --cg-anchor "$cg_anchor"
    fi

    local _tmp
    _tmp="$(mktemp 2>/dev/null || printf '%s' "/tmp/vco_dual_$$_$RANDOM")"
    # Inner hard bound. 7 s sits INSIDE the pre-edit hook's 8 s settings.json
    # budget (so the hook still reaches its cache-write + emit) and above the
    # 4 s the code-graph leg carried on the two-process path. When `timeout` is
    # absent (Git Bash), run unbounded — the harness timeout still applies, and
    # a bg-pid kill guard here would race the marker write.
    if command -v timeout >/dev/null 2>&1; then
        timeout 7 "$venv" "$driver" "$@" >"$_tmp" 2>/dev/null || true
    else
        "$venv" "$driver" "$@" >"$_tmp" 2>/dev/null || true
    fi

    # --- 4. split, cap, cache, emit ----------------------------------------
    # A missing marker for a leg we asked for means the driver did not run (or
    # died before writing) — signal the caller to use the legacy path rather
    # than silently dropping that leg's injection.
    if [ "$need_kg" = "1" ] && ! grep -Fxq -- "<<<VCO-DUAL:KG>>>" "$_tmp" 2>/dev/null; then
        rm -f "$_tmp" 2>/dev/null || true
        return 1
    fi
    if [ "$need_cg" = "1" ] && ! grep -Fxq -- "<<<VCO-DUAL:CG>>>" "$_tmp" 2>/dev/null; then
        rm -f "$_tmp" 2>/dev/null || true
        return 1
    fi

    local _blob
    if [ "$need_kg" = "1" ]; then
        _blob="$(awk '/^<<<VCO-DUAL:KG>>>$/{p=1;next} /^<<<VCO-DUAL:CG>>>$/{p=0;next} p' "$_tmp" 2>/dev/null | head -40)"
        printf '%s' "$_blob" > "$kg_out" 2>/dev/null || true
        [ -n "$kg_key" ] && command -v vco_query_cache_put >/dev/null 2>&1 \
            && vco_query_cache_put "$kg_key" "$_blob"
    fi
    if [ "$need_cg" = "1" ]; then
        _blob="$(awk '/^<<<VCO-DUAL:CG>>>$/{p=1;next} /^<<<VCO-DUAL:KG>>>$/{p=0;next} p' "$_tmp" 2>/dev/null | head -20)"
        printf '%s' "$_blob" > "$cg_out" 2>/dev/null || true
        [ -n "$cg_key" ] && command -v vco_query_cache_put >/dev/null 2>&1 \
            && vco_query_cache_put "$cg_key" "$_blob"
    fi
    rm -f "$_tmp" 2>/dev/null || true
    return 0
}
