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

# --- P2 (v0.2.91): ONE interpreter for the KG + code-graph pair -------------
#
# Invoke-VcoDualSearchCached -- run whichever of the two pre-edit searches MISSED
# their (unchanged, per-leg) cache in a SINGLE CPython process via
# claude_mcp_servers/scripts/hook_dual_search.py. Pre-P2 the pre-edit hook paid
# two full interpreter starts (~1.0 s each of import + client connect) for ~60 ms
# of real retrieval work. MUST MATCH query-cache.sh vco_dual_search_cached:
# the per-leg cache KEYS ("kg"+query+limit / "cg"+query+projectArg+limit+
# exclude+anchor), the per-leg output CAPS (40 / 20 lines) and the marker
# framing must agree cross-OS or the two OSes cache and split differently.
#
# Returns a hashtable @{ Ok=$bool; Kg=$string; Cg=$string }. Ok=$false means
# "fall back to the legacy two-call path" (driver absent, no venv, or no framing
# in the output) -- it is NOT an error signal.
#
# ACCEPTED WORST CASE (v0.2.91 wave-4 NIT-5, mirrors query-cache.sh): a hung
# driver plus the subsequent LEGACY re-run of the same leg can exceed the
# pre-edit hook's 8 s settings.json budget, and the harness kills the hook
# mid-run. The consequence is FAIL-OPEN -- no injection for that one Edit,
# nothing written, nothing corrupted; the next Edit is served from cache or
# retries cleanly. Suppressing the legacy fallback instead would silently drop a
# leg's injection on every driver hiccup: a worse, quieter failure.
function Invoke-VcoDualSearchCached {
    param(
        [string]$VenvPy,
        [string]$RlScript,
        [string]$Query,
        [int]$KgLimit = 1,
        [bool]$WantKg = $true,
        [bool]$WantCg = $false,
        [string]$CgProjectArg = "",
        [int]$CgLimit = 2,
        [string]$CgExcludeFile = "",
        [string]$CgAnchor = ""
    )
    $fallback = @{ Ok = $false; Kg = ""; Cg = "" }
    if (-not $Query) { return @{ Ok = $true; Kg = ""; Cg = "" } }
    if (-not $WantKg -and -not $WantCg) { return @{ Ok = $true; Kg = ""; Cg = "" } }
    if (-not $VenvPy -or -not (Test-Path -LiteralPath $VenvPy)) { return $fallback }

    # 1. Per-leg cache probe (identical keys to the single-leg wrappers).
    $kgKey = ""; $cgKey = ""
    if (Get-Command Get-VcoQueryCacheKey -ErrorAction SilentlyContinue) {
        if ($WantKg) { $kgKey = Get-VcoQueryCacheKey "kg" $Query "$KgLimit" }
        if ($WantCg) { $cgKey = Get-VcoQueryCacheKey "cg" $Query $CgProjectArg "$CgLimit" $CgExcludeFile $CgAnchor }
    }
    $kgOut = ""; $cgOut = ""
    $needKg = $WantKg; $needCg = $WantCg
    if ($kgKey -and (Get-Command Get-VcoQueryCache -ErrorAction SilentlyContinue)) {
        $qc = Get-VcoQueryCache $kgKey
        if ($qc.Hit) { $kgOut = $qc.Value; $needKg = $false }
    }
    if ($cgKey -and (Get-Command Get-VcoQueryCache -ErrorAction SilentlyContinue)) {
        $qc = Get-VcoQueryCache $cgKey
        if ($qc.Hit) { $cgOut = $qc.Value; $needCg = $false }
    }
    if (-not $needKg -and -not $needCg) {
        return @{ Ok = $true; Kg = $kgOut; Cg = $cgOut }
    }

    # 2. Resolve the driver; degrade to the legacy path when absent.
    $driver = Join-Path (Split-Path -Parent $RlScript) "hook_dual_search.py"
    if (-not (Test-Path -LiteralPath $driver)) { return $fallback }
    if ($needKg -and -not (Test-Path -LiteralPath $RlScript)) {
        # KG producer genuinely absent (non-orchestrator project) -- that leg has
        # no result, which is the pre-P2 behaviour too.
        $needKg = $false; $kgOut = ""
    }
    $cgScript = ""
    if ($needCg) {
        $cli = ""
        if (Get-Command Get-VcoCodegraphCli -ErrorAction SilentlyContinue) { $cli = Get-VcoCodegraphCli }
        if ($cli) {
            $cand = Join-Path (Split-Path -Parent $cli) "query_code_graph.py"
            if (Test-Path -LiteralPath $cand) { $cgScript = $cand }
        }
        if (-not $cgScript) { $needCg = $false; $cgOut = "" }
    }
    if (-not $needKg -and -not $needCg) {
        return @{ Ok = $true; Kg = $kgOut; Cg = $cgOut }
    }

    # 3. ONE interpreter for whichever legs actually missed.
    $cliArgs = @("--query", $Query)
    if ($needKg) { $cliArgs += @("--kg-limit", "$KgLimit") }
    if ($needCg) {
        $cliArgs += @("--cg-limit", "$CgLimit", "--cg-script", $cgScript)
        if ($CgProjectArg -like "--project *") {
            $cliArgs += @("--cg-project", $CgProjectArg.Substring("--project ".Length))
        }
        if ($CgExcludeFile) { $cliArgs += @("--cg-exclude-file", $CgExcludeFile) }
        if ($CgAnchor) { $cliArgs += @("--cg-anchor", $CgAnchor) }
    }

    $lines = @()
    try {
        $lines = @(& $VenvPy $driver @cliArgs 2>$null)
    } catch { return $fallback }

    # 4. Split on the markers, cap per leg, cache per leg.
    $kgMarker = "<<<VCO-DUAL:KG>>>"
    $cgMarker = "<<<VCO-DUAL:CG>>>"
    if ($needKg -and ($lines -notcontains $kgMarker)) { return $fallback }
    if ($needCg -and ($lines -notcontains $cgMarker)) { return $fallback }

    $kgLines = @(); $cgLines = @(); $cur = ""
    foreach ($line in $lines) {
        if ($line -eq $kgMarker) { $cur = "kg"; continue }
        if ($line -eq $cgMarker) { $cur = "cg"; continue }
        if ($cur -eq "kg") { $kgLines += $line }
        elseif ($cur -eq "cg") { $cgLines += $line }
    }
    if ($needKg) {
        $kgOut = (($kgLines | Select-Object -First 40) -join "`n")
        if ($null -eq $kgOut) { $kgOut = "" }
        if ($kgKey -and (Get-Command Set-VcoQueryCache -ErrorAction SilentlyContinue)) {
            Set-VcoQueryCache $kgKey $kgOut
        }
    }
    if ($needCg) {
        $cgOut = (($cgLines | Select-Object -First 20) -join "`n")
        if ($null -eq $cgOut) { $cgOut = "" }
        if ($cgKey -and (Get-Command Set-VcoQueryCache -ErrorAction SilentlyContinue)) {
            Set-VcoQueryCache $cgKey $cgOut
        }
    }
    return @{ Ok = $true; Kg = $kgOut; Cg = $cgOut }
}
