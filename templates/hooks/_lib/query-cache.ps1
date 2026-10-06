# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/query-cache.ps1
# Shared TTL result-cache for the code-graph / KG injection queries. The
# PowerShell sibling of _lib/query-cache.sh.
#
# Why this exists (v0.2.77 Part 9 task 2)
# ---------------------------------------
# Every injection surface re-issues the SAME expensive Weaviate+embed query
# many times per session. Each miss costs ~1.3 s. This shared cache serves
# repeat queries from disk (~ms) across ALL surfaces and files, keyed on the
# query itself.
#
# One home (CLAUDE.md "search before add"): the single SHELL cache
# implementation. Invoke-VcoKgSearchCached (pre-tool-use.ps1's Edit/Write KG
# suggestion path) calls Get-VcoQueryCache / Set-VcoQueryCache; the injection
# router keeps its OWN Python cache in the SAME directory with the SAME key
# algorithm under the disjoint "kgi"/"cgi" namespaces (hook_context_router.py).
# MUST MATCH templates/hooks/_lib/query-cache.sh.
#
# Semantics (identical to the .sh sibling):
#   - Stores the RAW producer block (pre-dedup); callers dedup per-session
#     after the cached value is returned.
#   - NEVER caches an EMPTY result (v0.2.101 poison fix, section 9): an empty
#     blob is indistinguishable from a leg killed by its inner timeout, and
#     caching it suppressed EVERY retry of that query for the whole TTL. A
#     pre-existing EMPTY entry (written by an older version) reads as a MISS
#     and is removed, so poisoned keys self-heal on first touch.
#   - TTL default 900 s; override with $env:VCO_QUERY_CACHE_TTL.
#   - Best-effort: any error falls back to running the query live.
#
# Plain ASCII only. Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoQueryCacheSourced) { return }
$script:VcoQueryCacheSourced = $true

$script:VcoQueryCacheTtlDefault = 900

# Get-VcoQueryCacheDir -- resolve (and create) the cache dir; "" on failure.
function Get-VcoQueryCacheDir {
    $root = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } elseif ($script:ProjectRoot) { $script:ProjectRoot } else { "" }
    if (-not $root) { return "" }
    $dir = Join-Path (Join-Path (Join-Path $root ".claude") "state") "query_cache"
    try {
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue | Out-Null
        }
    } catch { return "" }
    return $dir
}

# Get-VcoQueryCacheKey <parts...> -- deterministic sha1 hash of all parts,
# joined with a separator that cannot appear in the inputs (0x1f).
function Get-VcoQueryCacheKey {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Parts)
    $sep = [char]0x1f
    $joined = ($Parts -join $sep) + $sep
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
    return (($sha1.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
}

# Get-VcoQueryCache <Key> -- returns a hashtable @{Hit=$bool; Value=$string}.
# Hit is $true ONLY for a fresh (within-TTL) NON-EMPTY entry. An empty stored
# file is a miss (v0.2.101 poison fix, section 9) and is removed so a key
# poisoned by an older version self-heals. Hit is $false on
# miss/stale/empty/error -> caller runs live.
# MUST MATCH query-cache.sh vco_query_cache_get.
function Get-VcoQueryCache {
    param([string]$Key)
    $miss = @{ Hit = $false; Value = "" }
    if (-not $Key) { return $miss }
    $dir = Get-VcoQueryCacheDir
    if (-not $dir) { return $miss }
    $f = Join-Path $dir $Key
    if (-not (Test-Path -LiteralPath $f)) { return $miss }
    try {
        if ((Get-Item -LiteralPath $f).Length -eq 0) {
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
            return $miss
        }
    } catch { return $miss }
    $ttl = if ($env:VCO_QUERY_CACHE_TTL) { [int]$env:VCO_QUERY_CACHE_TTL } else { $script:VcoQueryCacheTtlDefault }
    try {
        $mtime = (Get-Item -LiteralPath $f).LastWriteTime
        $age = ((Get-Date) - $mtime).TotalSeconds
        if ($age -ge $ttl) { return $miss }
        $val = ""
        try { $val = (Get-Content -LiteralPath $f -Raw -ErrorAction Stop) } catch { $val = "" }
        if ($null -eq $val) { $val = "" }
        return @{ Hit = $true; Value = $val }
    } catch {
        return $miss
    }
}

# Set-VcoQueryCache <Key> <Blob> -- store a NON-EMPTY Blob for Key, then GC
# stale entries. An empty Blob is NEVER stored (v0.2.101 poison fix, section
# 9 -- this is the one chokepoint every cached surface puts through, so the
# guard lives HERE once). Soft-fail.
# MUST MATCH query-cache.sh vco_query_cache_put.
function Set-VcoQueryCache {
    param([string]$Key, [string]$Blob)
    if (-not $Key) { return }
    if ([string]::IsNullOrEmpty($Blob)) { return }
    $dir = Get-VcoQueryCacheDir
    if (-not $dir) { return }
    $f = Join-Path $dir $Key
    $tmp = "$f.$PID.tmp"
    try {
        Set-Content -LiteralPath $tmp -Value $Blob -NoNewline -Encoding UTF8 -ErrorAction Stop
        Move-Item -LiteralPath $tmp -Destination $f -Force -ErrorAction Stop
    } catch {
        try { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } catch { }
        return
    }
    $ttl = if ($env:VCO_QUERY_CACHE_TTL) { [int]$env:VCO_QUERY_CACHE_TTL } else { $script:VcoQueryCacheTtlDefault }
    $cutoff = (Get-Date).AddSeconds(-2 * $ttl)
    try {
        Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
}

# Invoke-VcoKgSearchCached <VenvPy> <RlScript> <Query> <Limit> [PromptId] [TranscriptPath]
# -- run the RL-aware KG search through the shared TTL cache; return the raw
# "KG:"-prefixed block(s), served from cache on a repeat query. MUST MATCH
# query-cache.sh vco_kg_search_cached (same "kg" surface + key order).
# Best-effort.
#
# WP-E (v0.2.92): PromptId/TranscriptPath are OPTIONAL, default "" (an
# omitting caller reproduces pre-WP-E behaviour byte-for-byte). PromptId
# joins the cache key ONLY -- see query-cache.sh's vco_kg_search_cached
# docstring for why (the query text on this boundary is always the raw,
# unenriched trigger; enrichment happens inside rl_kg_search.py from
# --transcript, so two turns issuing the same short trigger must not
# collide on one cache key). That separation holds only while a PromptId is
# actually delivered: with an EMPTY PromptId every turn contributes the same
# key component, all turns collapse onto ONE key, and a cross-turn re-ask of
# the identical trigger is a HIT replaying a different enrichment window's
# result for up to the TTL (900 s). See query-cache.sh's KNOWN DEGRADATION
# note -- same behaviour, same out-of-scope call, stated in both siblings so
# neither reads as an unconditional guarantee.
# TranscriptPath is a PATH, never text -- passed
# through as --transcript <path> so enrichment composition happens in the
# process that embeds it.
function Invoke-VcoKgSearchCached {
    param([string]$VenvPy, [string]$RlScript, [string]$Query, [int]$Limit = 1, [string]$PromptId = "", [string]$TranscriptPath = "")
    if (-not $Query) { return "" }
    if (-not $VenvPy -or -not (Test-Path -LiteralPath $VenvPy)) { return "" }
    if (-not (Test-Path -LiteralPath $RlScript)) { return "" }

    $key = ""
    if (Get-Command Get-VcoQueryCacheKey -ErrorAction SilentlyContinue) {
        $key = Get-VcoQueryCacheKey "kg" $Query "$Limit" $PromptId
    }
    if ($key -and (Get-Command Get-VcoQueryCache -ErrorAction SilentlyContinue)) {
        $qc = Get-VcoQueryCache $key
        if ($qc.Hit) { return $qc.Value }
    }

    $out = ""
    try {
        if ($TranscriptPath) {
            $out = (& $VenvPy $RlScript $Query --limit $Limit --hook-format --transcript $TranscriptPath 2>$null | Select-Object -First 40) -join "`n"
        } else {
            $out = (& $VenvPy $RlScript $Query --limit $Limit --hook-format 2>$null | Select-Object -First 40) -join "`n"
        }
    } catch { $out = "" }
    if ($null -eq $out) { $out = "" }
    if ($key -and (Get-Command Set-VcoQueryCache -ErrorAction SilentlyContinue)) {
        Set-VcoQueryCache $key $out
    }
    return $out
}


# v0.2.101 Wave-2 review SF-2: Invoke-VcoDualSearchCached was RETIRED here --
# its last caller (pre-edit-context-inject.ps1) became a thin router wrapper.
# The merged mechanism lives on in hook_dual_search.py (run_legs), imported
# in-process by hook_context_router.py. Remaining: the TTL primitives
# (section 9: empty is NEVER cached) and Invoke-VcoKgSearchCached (still
# called by pre-tool-use.ps1). MUST MATCH query-cache.sh.
