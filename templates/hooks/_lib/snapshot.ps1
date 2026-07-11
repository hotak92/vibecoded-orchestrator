# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/snapshot.ps1 — Windows sibling of _lib/snapshot.sh. See the .sh
# version for the full design rationale.
#
# Provides Take-Snapshot / Diff-Snapshot / Cleanup-Snapshot functions
# used by the V52-L.1 SubagentStart/SubagentStop reconciliation pair.
#
# Soft-fail contract: every function returns silently on error. The
# caller is expected to be a hook that MUST exit 0 regardless.

# Default extensions / directories (must match the .sh sibling).
$script:VCT_SnapshotCodeExtsDefault = @(
    'py','rs','ts','tsx','js','jsx','go','java','cs','c','cpp','h','hpp',
    'rb','php','swift','kt','scala','sh','ps1','sql'
)
$script:VCT_SnapshotDirsDefault = @(
    'knowledge','docs','src','lib','launcher','claude_mcp_servers',
    '.claude/scripts','vco_lib','templates','tests'
)
# v0.2.77 Part 9 task 9c: build-output / VCS / cache subtrees to PRUNE from the
# walk (MUST MATCH snapshot.sh _SNAPSHOT_PRUNE_DIRS_DEFAULT). These are matched
# by the code-ext filters but are never subagent-authored source, so the
# SubagentStop reconciler's consumers never want them. Excluding them shrinks
# the enumeration without changing any consumer's behaviour; applied identically
# at snapshot AND diff time (shared _Get-SnapshotFiles).
$script:VCT_SnapshotPruneDirsDefault = @(
    'target','node_modules','.git','.wt','__pycache__','.venv','dist','build',
    '.next','.svelte-kit','.pytest_cache','.mypy_cache','.ruff_cache'
)

# Internal: enumerate watched files. Returns absolute paths.
function _Get-SnapshotFiles {
    param(
        [string]$ProjectRoot
    )

    $dirs = if ($env:VCT_SNAPSHOT_DIRS) {
        $env:VCT_SNAPSHOT_DIRS -split '\s+'
    } else {
        $script:VCT_SnapshotDirsDefault
    }
    $exts = if ($env:VCT_SNAPSHOT_CODE_EXTS) {
        ($env:VCT_SNAPSHOT_CODE_EXTS -split '\|') | Where-Object { $_ }
    } else {
        $script:VCT_SnapshotCodeExtsDefault
    }
    # Always include .md as a watched extension (KG + docs).
    $allExts = @('md') + $exts | Sort-Object -Unique

    # task 9c: prune dirs. Get-ChildItem has no native -prune, so we filter each
    # candidate's path for a pruned segment. Build a set for O(1) lookup.
    $pruneDirs = if ($env:VCT_SNAPSHOT_PRUNE_DIRS) {
        $env:VCT_SNAPSHOT_PRUNE_DIRS -split '\s+' | Where-Object { $_ }
    } else {
        $script:VCT_SnapshotPruneDirsDefault
    }
    $pruneSet = @{}
    foreach ($p in $pruneDirs) { if ($p) { $pruneSet[$p] = $true } }

    foreach ($d in $dirs) {
        $full = Join-Path $ProjectRoot $d
        if (-not (Test-Path $full -PathType Container)) { continue }
        try {
            foreach ($ext in $allExts) {
                Get-ChildItem -Path $full -Recurse -File -Filter "*.$ext" `
                    -ErrorAction SilentlyContinue |
                    Where-Object {
                        # Skip if ANY path segment is a pruned dir (build/VCS/cache).
                        $segs = $_.FullName -split '[\\/]'
                        $skip = $false
                        foreach ($s in $segs) { if ($pruneSet.ContainsKey($s)) { $skip = $true; break } }
                        -not $skip
                    } |
                    ForEach-Object { $_.FullName }
            }
        } catch {
            # Soft-fail per dir
        }
    }
}

# Compute SHA-256 of a file via .NET. Returns hex string or $null on error.
function _Get-FileSha256 {
    param([string]$Path)
    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
        $info = Get-Item -LiteralPath $Path -ErrorAction Stop
        # Skip files >5 MB (match .sh sibling) — usually binaries we
        # don't want to credential-scan or KG-sync.
        if ($info.Length -gt 5MB) { return $null }
        $hash = Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop
        return $hash.Hash.ToLower()
    } catch {
        return $null
    }
}

# Sanitize agent_id for use as part of a file name.
function _Get-SafeAgentId {
    param([string]$AgentId)
    if (-not $AgentId) { return "" }
    $safe = [regex]::Replace($AgentId, '[^a-zA-Z0-9_-]', '_')
    if ($safe.Length -gt 64) { $safe = $safe.Substring(0, 64) }
    return $safe
}

# Take a snapshot of the current filesystem state. Returns $true on
# success, $false on failure. Writes the snapshot JSON to
# <SnapshotDir>/subagent-snapshot-<agent_id>.json.
function Take-Snapshot {
    param(
        [string]$AgentId,
        [string]$ProjectRoot,
        [string]$SnapshotDir = ""
    )

    if (-not $AgentId -or -not $ProjectRoot) { return $false }
    if (-not (Test-Path $ProjectRoot -PathType Container)) { return $false }

    if (-not $SnapshotDir) {
        $SnapshotDir = Join-Path $ProjectRoot ".claude/state"
    }
    try {
        if (-not (Test-Path $SnapshotDir)) {
            New-Item -ItemType Directory -Path $SnapshotDir -Force `
                -ErrorAction Stop | Out-Null
        }
    } catch {
        return $false
    }

    # v0.2.77 Part 9 task 9a: orphan GC (MUST MATCH snapshot.sh). Cleanup-Snapshot
    # only runs at SubagentStop; a killed agent's snapshot leaks forever. Sweep
    # snapshots older than VCT_SNAPSHOT_GC_DAYS (default 3) here — well past any
    # live subagent's lifetime, so only genuine orphans are reaped (a live
    # agent's fresh snapshot is mtime=now). Cleanup of dead state, not data loss.
    try {
        $gcDays = if ($env:VCT_SNAPSHOT_GC_DAYS) { [int]$env:VCT_SNAPSHOT_GC_DAYS } else { 3 }
        $cutoff = (Get-Date).AddDays(-$gcDays)
        Get-ChildItem -LiteralPath $SnapshotDir -File -Filter 'subagent-snapshot-*.json' `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }

    $safeId = _Get-SafeAgentId -AgentId $AgentId
    if (-not $safeId) { return $false }
    $snapFile = Join-Path $SnapshotDir "subagent-snapshot-$safeId.json"

    $resolvedRoot = (Resolve-Path $ProjectRoot -ErrorAction SilentlyContinue).Path
    if (-not $resolvedRoot) { $resolvedRoot = $ProjectRoot }

    $files = [ordered]@{}
    $processed = 0
    $maxFiles = 50000

    foreach ($abs in _Get-SnapshotFiles -ProjectRoot $ProjectRoot) {
        if ($processed -ge $maxFiles) { break }
        # task 9b: capture mtime + size alongside the hash so Diff-Snapshot can
        # skip re-hashing unchanged files (MUST MATCH snapshot.sh v2 format).
        try {
            $info = Get-Item -LiteralPath $abs -ErrorAction Stop
        } catch {
            continue
        }
        if ($info.Length -gt 5MB) { continue }
        $hash = _Get-FileSha256 -Path $abs
        if (-not $hash) { continue }
        try {
            # Path.GetRelativePath is .NET Core+; fall back to manual
            # prefix-strip for older PowerShell hosts.
            $rel = if ([System.IO.Path]::GetType().GetMethod('GetRelativePath',
                    [type[]]@([string],[string]))) {
                [System.IO.Path]::GetRelativePath($resolvedRoot, $abs)
            } else {
                $abs.Substring($resolvedRoot.Length).TrimStart('\','/')
            }
            # Skip files resolving outside project_root (rare; symlinks).
            if ($rel.StartsWith('..')) { continue }
            # Normalize to forward slashes for cross-OS comparability
            # with the .sh sibling's output.
            $rel = $rel -replace '\\', '/'
            # v2 entry: {h,m,s}. m is mtime in ticks (100ns units) — the value
            # only needs to be a stable integer that changes on write; the .ps1
            # snapshot is compared against a .ps1 snapshot (never cross-runtime),
            # so ticks-vs-ns divergence from the .sh side is fine (the schema-
            # parity test checks SHAPE, not the numeric mtime unit).
            $files[$rel] = [ordered]@{
                h = $hash
                m = $info.LastWriteTimeUtc.Ticks
                s = [int64]$info.Length
            }
            $processed++
        } catch {
            continue
        }
    }

    $doc = [ordered]@{
        version      = 2
        agent_id     = $AgentId
        project_root = $resolvedRoot
        created_at   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        files        = $files
    }

    $tmp = $snapFile + ".tmp"
    try {
        # Compact JSON to keep snapshots small.
        $doc | ConvertTo-Json -Compress -Depth 5 `
            | Out-File -FilePath $tmp -Encoding utf8 -ErrorAction Stop
        Move-Item -LiteralPath $tmp -Destination $snapFile -Force `
            -ErrorAction Stop
        return $true
    } catch {
        try { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue } catch {}
        return $false
    }
}

# Emit (on stdout) one relative path per line for every file that
# changed between the snapshot and the current filesystem. Includes
# added, modified, and deleted files. Soft-fail to silent no-op.
function Diff-Snapshot {
    param(
        [string]$AgentId,
        [string]$ProjectRoot,
        [string]$SnapshotDir = ""
    )

    if (-not $AgentId -or -not $ProjectRoot) { return }
    if (-not (Test-Path $ProjectRoot -PathType Container)) { return }

    if (-not $SnapshotDir) {
        $SnapshotDir = Join-Path $ProjectRoot ".claude/state"
    }

    $safeId = _Get-SafeAgentId -AgentId $AgentId
    if (-not $safeId) { return }
    $snapFile = Join-Path $SnapshotDir "subagent-snapshot-$safeId.json"
    if (-not (Test-Path $snapFile -PathType Leaf)) { return }

    # task 9b: parse BOTH v2 ({h,m,s} object) and legacy v1 (bare hash string)
    # entries. $beforeHash[rel] = hash; $beforeQuick[rel] = "<m>|<s>" (or $null
    # for v1). MUST MATCH snapshot.sh _before_parts.
    $beforeHash = @{}
    $beforeQuick = @{}
    try {
        $doc = Get-Content -Raw -LiteralPath $snapFile -ErrorAction Stop |
            ConvertFrom-Json -ErrorAction Stop
        if ($doc.files) {
            foreach ($prop in $doc.files.PSObject.Properties) {
                $val = $prop.Value
                if ($val -is [string]) {
                    $beforeHash[$prop.Name] = [string]$val
                    $beforeQuick[$prop.Name] = $null
                } else {
                    # v2 object with .h/.m/.s
                    $beforeHash[$prop.Name] = [string]$val.h
                    if ($null -ne $val.m -and $null -ne $val.s) {
                        $beforeQuick[$prop.Name] = "$($val.m)|$($val.s)"
                    } else {
                        $beforeQuick[$prop.Name] = $null
                    }
                }
            }
        }
    } catch {
        return
    }

    $resolvedRoot = (Resolve-Path $ProjectRoot -ErrorAction SilentlyContinue).Path
    if (-not $resolvedRoot) { $resolvedRoot = $ProjectRoot }

    $after = @{}
    $processed = 0
    $maxFiles = 50000
    foreach ($abs in _Get-SnapshotFiles -ProjectRoot $ProjectRoot) {
        if ($processed -ge $maxFiles) { break }
        try {
            $info = Get-Item -LiteralPath $abs -ErrorAction Stop
        } catch {
            continue
        }
        if ($info.Length -gt 5MB) { continue }
        try {
            $rel = if ([System.IO.Path]::GetType().GetMethod('GetRelativePath',
                    [type[]]@([string],[string]))) {
                [System.IO.Path]::GetRelativePath($resolvedRoot, $abs)
            } else {
                $abs.Substring($resolvedRoot.Length).TrimStart('\','/')
            }
            if ($rel.StartsWith('..')) { continue }
            $rel = $rel -replace '\\', '/'
        } catch {
            continue
        }
        # task 9b quick-check: reuse stored hash when (mtime,size) unchanged.
        $quick = "$($info.LastWriteTimeUtc.Ticks)|$([int64]$info.Length)"
        if ($beforeQuick.ContainsKey($rel) -and $null -ne $beforeQuick[$rel] `
                -and $beforeQuick[$rel] -eq $quick) {
            $after[$rel] = $beforeHash[$rel]
            $processed++
            continue
        }
        $hash = _Get-FileSha256 -Path $abs
        if (-not $hash) { continue }
        $after[$rel] = $hash
        $processed++
    }

    $changed = New-Object System.Collections.Generic.HashSet[string]
    foreach ($path in $after.Keys) {
        if (-not $beforeHash.ContainsKey($path) -or $beforeHash[$path] -ne $after[$path]) {
            [void]$changed.Add($path)
        }
    }
    foreach ($path in $beforeHash.Keys) {
        if (-not $after.ContainsKey($path)) {
            [void]$changed.Add($path)
        }
    }

    # Emit deterministically sorted.
    $changed | Sort-Object | ForEach-Object { Write-Output $_ }
}

# Delete the snapshot file. Soft-fail. No return value.
function Cleanup-Snapshot {
    param(
        [string]$AgentId,
        [string]$ProjectRoot,
        [string]$SnapshotDir = ""
    )

    if (-not $AgentId) { return }
    if (-not $SnapshotDir) {
        $SnapshotDir = Join-Path $ProjectRoot ".claude/state"
    }
    $safeId = _Get-SafeAgentId -AgentId $AgentId
    if (-not $safeId) { return }
    $snapFile = Join-Path $SnapshotDir "subagent-snapshot-$safeId.json"
    try {
        if (Test-Path -LiteralPath $snapFile) {
            Remove-Item -LiteralPath $snapFile -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}
