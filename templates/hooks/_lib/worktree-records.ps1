# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# worktree-records.ps1 -- the one home for worktree-guard's per-agent records
# (v0.2.102 W6-Q follow-up). MUST MATCH _lib/worktree-records.sh: same record
# file names (.claude/state/worktree-guard/wt-<name>.json), same fields
# (name, mode head|snapshot|in_place, path, base, toplevel, status,
# created_at), same reap conditions and decision/reason vocabulary. See the
# .sh sibling for the full WHY: the harness keeps a hook-created worktree and
# runs no WorktreeRemove when a subagent finishes, so VCO reaps the provably
# unchanged ones itself.

$script:WtrecSubdir = ".claude/state/worktree-guard"

function Test-WtrecName([string]$name) {
    if (-not $name) { return $false }
    return ($name -match '^[A-Za-z0-9_-][A-Za-z0-9._-]*$')
}

function Write-WtrecRecord([string]$root, [string]$name, [string]$mode, [string]$path, [string]$base, [string]$toplevel) {
    if (-not (Test-WtrecName $name)) { return }
    try {
        $dir = Join-Path $root $script:WtrecSubdir
        New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
        $rec = [ordered]@{
            name       = $name
            mode       = $mode
            path       = $path
            base       = $base
            toplevel   = $toplevel
            status     = "created"
            created_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        }
        Set-Content -LiteralPath (Join-Path $dir "wt-$name.json") -Value ($rec | ConvertTo-Json -Compress) -Encoding UTF8 -ErrorAction Stop
    } catch { }
}

# Find-WtrecRecord: the record file for an agent id (wt-agent-<id>.json, else
# wt-<id>.json), or "" when there is none.
function Find-WtrecRecord([string]$root, [string]$id) {
    if (-not (Test-WtrecName $id)) { return "" }
    $dir = Join-Path $root $script:WtrecSubdir
    foreach ($nm in @("wt-agent-$id.json", "wt-$id.json")) {
        $p = Join-Path $dir $nm
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
    return ""
}

function Read-WtrecRecord([string]$file) {
    try { return (Get-Content -LiteralPath $file -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop) } catch { return $null }
}

function Set-WtrecStatus([string]$file, [string]$status, [string]$reason) {
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return }
    try {
        $h = [ordered]@{}
        foreach ($p in $rec.PSObject.Properties) { $h[$p.Name] = $p.Value }
        $h["status"] = $status
        $h["status_reason"] = $reason
        $h["status_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Set-Content -LiteralPath $file -Value ($h | ConvertTo-Json -Compress) -Encoding UTF8 -ErrorAction Stop
    } catch { }
}

# Remove-WtrecStale: drop records and notices older than 30 days.
function Remove-WtrecStale([string]$root) {
    $dir = Join-Path $root $script:WtrecSubdir
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return }
    try {
        $cut = (Get-Date).AddDays(-30)
        Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
            Where-Object { ($_.Name -like 'wt-*.json' -or $_.Name -like 'notice-*.txt') -and $_.LastWriteTime -lt $cut } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
}

# Invoke-WtrecReap: remove the agent's worktree when provably unchanged.
# Returns $null when the agent has no record, else an object with
# Decision (removed|kept|skipped), Reason and Path. Same conditions as
# wtrec_reap in the .sh sibling.
function Invoke-WtrecReap([string]$root, [string]$id) {
    $file = Find-WtrecRecord $root $id
    if (-not $file) { return $null }
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return $null }
    $mode = [string]$rec.mode; $path = [string]$rec.path
    $base = [string]$rec.base; $top = [string]$rec.toplevel
    $out = {
        param($d, $r)
        if ($d -ne "skipped") { Set-WtrecStatus $file $d $r }
        return [pscustomobject]@{ Decision = $d; Reason = $r; Path = $path }
    }
    if ($mode -eq "in_place") { return (& $out "skipped" "in_place_never_removed") }
    if ($mode -ne "head" -and $mode -ne "snapshot") { return (& $out "skipped" "unknown_mode") }
    if ([string]$rec.status -ne "created") { return (& $out "skipped" "already_handled") }
    if (-not $path -or -not $base -or -not $top -or $path -eq $top) { return (& $out "kept" "incomplete_record") }
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { return (& $out "removed" "already_gone") }
    $listed = @()
    try {
        $listed = @((& git -C $top worktree list --porcelain 2>$null) |
            Where-Object { $_ -like 'worktree *' } | ForEach-Object { $_.Substring(9) })
    } catch { }
    # git prints forward slashes on Windows; compare full, normalized paths.
    $norm = { param($p) try { [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/') } catch { $p } }
    $want = & $norm $path
    $hit = $false
    foreach ($l in $listed) { if ((& $norm $l) -eq $want) { $hit = $true; break } }
    if (-not $hit) { return (& $out "kept" "not_a_registered_worktree") }
    $head = ""
    try { $head = [string](& git -C $path rev-parse HEAD 2>$null | Select-Object -First 1) } catch { }
    if ($head.Trim() -ne $base) { return (& $out "kept" "head_moved_or_commits") }
    $st = (& git -C $path status --porcelain --untracked-files=all --ignored=matching 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0) { return (& $out "kept" "status_unreadable") }
    if ($st.Trim()) { return (& $out "kept" "has_changes") }
    $err = (& git -C $top worktree remove $path 2>&1 | Out-String)
    if ($LASTEXITCODE -eq 0) { return (& $out "removed" "clean") }
    $msg = ($err -replace '[\t\r\n]+', ' ').Trim()
    if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
    return (& $out "kept" "git_refused: $msg")
}
