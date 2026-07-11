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
