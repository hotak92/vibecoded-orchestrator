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

    foreach ($d in $dirs) {
        $full = Join-Path $ProjectRoot $d
        if (-not (Test-Path $full -PathType Container)) { continue }
        try {
            foreach ($ext in $allExts) {
                Get-ChildItem -Path $full -Recurse -File -Filter "*.$ext" `
                    -ErrorAction SilentlyContinue |
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
            $files[$rel] = $hash
            $processed++
        } catch {
            continue
        }
    }

    $doc = [ordered]@{
        version      = 1
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

    $before = @{}
    try {
        $doc = Get-Content -Raw -LiteralPath $snapFile -ErrorAction Stop |
            ConvertFrom-Json -ErrorAction Stop
        if ($doc.files) {
            foreach ($prop in $doc.files.PSObject.Properties) {
                $before[$prop.Name] = [string]$prop.Value
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
        $hash = _Get-FileSha256 -Path $abs
        if (-not $hash) { continue }
        try {
            $rel = if ([System.IO.Path]::GetType().GetMethod('GetRelativePath',
                    [type[]]@([string],[string]))) {
                [System.IO.Path]::GetRelativePath($resolvedRoot, $abs)
            } else {
                $abs.Substring($resolvedRoot.Length).TrimStart('\','/')
            }
            if ($rel.StartsWith('..')) { continue }
            $rel = $rel -replace '\\', '/'
            $after[$rel] = $hash
            $processed++
        } catch {
            continue
        }
    }

    $changed = New-Object System.Collections.Generic.HashSet[string]
    foreach ($path in $after.Keys) {
        if (-not $before.ContainsKey($path) -or $before[$path] -ne $after[$path]) {
            [void]$changed.Add($path)
        }
    }
    foreach ($path in $before.Keys) {
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
