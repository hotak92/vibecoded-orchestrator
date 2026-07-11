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
# One home (CLAUDE.md "search before add"): the single cache implementation.
# Invoke-VcoCodegraphQueryBlock and the KG-search wrappers call
# Get-VcoQueryCache / Set-VcoQueryCache. MUST MATCH
# templates/hooks/_lib/query-cache.sh.
#
# Semantics (identical to the .sh sibling):
#   - Stores the RAW producer block (pre-dedup); callers dedup per-session
#     after the cached value is returned.
#   - Caches EMPTY results too (empty file) so a genuinely-empty symbol isn't
#     re-queried within the TTL. Miss vs cached-empty is signalled by the
#     return object, not by output emptiness.
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
# Hit is $true when a fresh (within-TTL) entry exists (Value may be "" for a
# cached empty result). Hit is $false on miss/stale/error -> caller runs live.
function Get-VcoQueryCache {
    param([string]$Key)
    $miss = @{ Hit = $false; Value = "" }
    if (-not $Key) { return $miss }
    $dir = Get-VcoQueryCacheDir
    if (-not $dir) { return $miss }
    $f = Join-Path $dir $Key
    if (-not (Test-Path -LiteralPath $f)) { return $miss }
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

# Set-VcoQueryCache <Key> <Blob> -- store Blob (may be "") for Key, then GC
# stale entries. Soft-fail.
function Set-VcoQueryCache {
    param([string]$Key, [string]$Blob)
    if (-not $Key) { return }
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

# Invoke-VcoKgSearchCached <VenvPy> <RlScript> <Query> <Limit> -- run the
# RL-aware KG search through the shared TTL cache; return the raw "KG:"-prefixed
# block(s), served from cache on a repeat query. MUST MATCH query-cache.sh
# vco_kg_search_cached (same "kg" surface + key order). Best-effort.
function Invoke-VcoKgSearchCached {
    param([string]$VenvPy, [string]$RlScript, [string]$Query, [int]$Limit = 1)
    if (-not $Query) { return "" }
    if (-not $VenvPy -or -not (Test-Path -LiteralPath $VenvPy)) { return "" }
    if (-not (Test-Path -LiteralPath $RlScript)) { return "" }

    $key = ""
    if (Get-Command Get-VcoQueryCacheKey -ErrorAction SilentlyContinue) {
        $key = Get-VcoQueryCacheKey "kg" $Query "$Limit"
    }
    if ($key -and (Get-Command Get-VcoQueryCache -ErrorAction SilentlyContinue)) {
        $qc = Get-VcoQueryCache $key
        if ($qc.Hit) { return $qc.Value }
    }

    $out = ""
    try {
        $out = (& $VenvPy $RlScript $Query --limit $Limit --hook-format 2>$null | Select-Object -First 40) -join "`n"
    } catch { $out = "" }
    if ($null -eq $out) { $out = "" }
    if ($key -and (Get-Command Set-VcoQueryCache -ErrorAction SilentlyContinue)) {
        Set-VcoQueryCache $key $out
    }
    return $out
}
