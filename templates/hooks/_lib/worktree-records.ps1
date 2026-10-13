# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# worktree-records.ps1 -- the one home for worktree-guard's per-agent records
# (v0.2.102 W6-Q follow-up). MUST MATCH _lib/worktree-records.sh: same record
# file names (.claude/state/worktree-guard/wt-<name>.json), same fields
# (name, mode head|snapshot|in_place|reservation|reservation_existing, path,
# base, toplevel, status, created_at; plus a lane reservation's binding,
# v0.2.102 W6-AA), same reap conditions and decision/reason vocabulary. See the
# .sh sibling for the full WHY: the harness keeps a hook-created worktree and
# runs no WorktreeRemove when a subagent finishes, so VCO reaps the provably
# unchanged ones itself.

$script:WtrecSubdir = ".claude/state/worktree-guard"

function Test-WtrecName([string]$name) {
    if (-not $name) { return $false }
    return ($name -match '^[A-Za-z0-9_-][A-Za-z0-9._-]*$')
}

# $extra (v0.2.102 W6-AA): optional one-line JSON object of STRING fields
# merged into the record (a lane reservation's binding); the fixed fields
# always win. Must match wtrec_write in the .sh sibling.
function Write-WtrecRecord([string]$root, [string]$name, [string]$mode, [string]$path, [string]$base, [string]$toplevel, [string]$extra = "") {
    if (-not (Test-WtrecName $name)) { return }
    try {
        $dir = Join-Path $root $script:WtrecSubdir
        New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
        $rec = [ordered]@{}
        if ($extra) {
            $x = $null
            try { $x = $extra | ConvertFrom-Json -ErrorAction Stop } catch { $x = $null }
            if ($x -is [System.Management.Automation.PSCustomObject]) {
                foreach ($p in $x.PSObject.Properties) { if ($p.Value -is [string]) { $rec[$p.Name] = $p.Value } }
            }
        }
        $rec["name"] = $name
        $rec["mode"] = $mode
        $rec["path"] = $path
        $rec["base"] = $base
        $rec["toplevel"] = $toplevel
        $rec["status"] = "created"
        $rec["created_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Set-Content -LiteralPath (Join-Path $dir "wt-$name.json") -Value ($rec | ConvertTo-Json -Compress) -Encoding UTF8 -ErrorAction Stop
    } catch { }
}

# Find-WtrecRecord: the record file for an agent id (wt-agent-<id>.json, else
# wt-<id>.json), or "" when there is none. A found record's LastWriteTime is
# bumped, so it tracks the agent's LAST activity (a resume's SubagentStart,
# its SubagentStop) and Remove-WtrecStale's idle window counts from there.
# Must match wtrec_find in the .sh sibling.
function Find-WtrecRecord([string]$root, [string]$id) {
    if (-not (Test-WtrecName $id)) { return "" }
    $dir = Join-Path $root $script:WtrecSubdir
    foreach ($nm in @("wt-agent-$id.json", "wt-$id.json")) {
        $p = Join-Path $dir $nm
        if (Test-Path -LiteralPath $p -PathType Leaf) {
            try { [System.IO.File]::SetLastWriteTime($p, (Get-Date)) } catch { }
            return $p
        }
    }
    return ""
}

# Get-WtrecAgentDefIsolation: the `isolation:` frontmatter value of the agent
# definition whose `name` (else file stem) is $type, searched in
# <project>/.claude/agents then ~/.claude/agents; "" when unknown or absent.
# Must match wtrec_agent_def_isolation in the .sh sibling.
function Get-WtrecAgentDefIsolation([string]$root, [string]$type) {
    $roots = @((Join-Path (Join-Path $root ".claude") "agents"),
               (Join-Path (Join-Path $HOME ".claude") "agents"))
    foreach ($r in $roots) {
        if (-not (Test-Path -LiteralPath $r -PathType Container)) { continue }
        $files = Get-ChildItem -LiteralPath $r -Recurse -File -Filter "*.md" -ErrorAction SilentlyContinue | Sort-Object FullName
        foreach ($f in $files) {
            $text = ""
            try { $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop } catch { continue }
            $fm = @{}
            if ($text -and $text.StartsWith("---")) {
                $end = $text.IndexOf("`n---", 3)
                if ($end -ge 0) {
                    foreach ($line in ($text.Substring(3, $end - 3) -split "`r?`n")) {
                        if ($line -match '^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$') {
                            $fm[$Matches[1]] = $Matches[2].Trim('"', "'")
                        }
                    }
                }
            }
            $nm = if ($fm.ContainsKey("name")) { $fm["name"] } else { $f.BaseName }
            if ($nm -eq $type) {
                if ($fm.ContainsKey("isolation")) { return $fm["isolation"] }
                return ""
            }
        }
    }
    return ""
}

# Test-WtrecGuardRegistered: worktree-guard registered in the project's
# settings (when parked, the harness makes the worktree and no record exists).
function Test-WtrecGuardRegistered([string]$root) {
    foreach ($n in @("settings.json", "settings.local.json")) {
        $p = Join-Path (Join-Path $root ".claude") $n
        if ((Test-Path -LiteralPath $p -PathType Leaf) -and (Select-String -LiteralPath $p -Pattern 'worktree-guard\.' -Quiet -ErrorAction SilentlyContinue)) { return $true }
    }
    return $false
}

# Test-WtrecIsolationSkipped: $true only when it is DECIDABLE that this launch
# skipped its isolation (definition found, not plugin-scoped, declares
# `isolation: worktree`; guard registered; no record for this agent id).
# Shared by subagent-start-isolation-check and subagent-stop-reconcile.
# Must match wtrec_isolation_skipped in the .sh sibling.
function Test-WtrecIsolationSkipped([string]$root, [string]$id, [string]$type) {
    if (-not $type -or $type.Contains(":")) { return $false }
    if (-not (Test-WtrecName $id)) { return $false }
    if (Find-WtrecRecord $root $id) { return $false }
    if (-not (Test-WtrecGuardRegistered $root)) { return $false }
    return ((Get-WtrecAgentDefIsolation $root $type) -eq "worktree")
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

# Get-WtrecCleanupDays: the largest valid `cleanupPeriodDays` (Claude Code's
# transcript retention) from the user settings
# ($env:CLAUDE_CONFIG_DIR, else ~/.claude) and the project's settings.json +
# settings.local.json, floored at 30. Only that one key is read; a bool,
# negative or non-number value is ignored. Must match the python block in
# wtrec_prune (.sh sibling).
function Get-WtrecCleanupDays([string]$root) {
    $cfg = $env:CLAUDE_CONFIG_DIR
    if (-not $cfg) { $cfg = Join-Path $HOME ".claude" }
    $days = [double]30
    $files = @((Join-Path $cfg "settings.json"),
               (Join-Path (Join-Path $root ".claude") "settings.json"),
               (Join-Path (Join-Path $root ".claude") "settings.local.json"))
    foreach ($f in $files) {
        if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { continue }
        try {
            $doc = Get-Content -LiteralPath $f -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        } catch { continue }
        if ($null -eq $doc -or $doc -isnot [System.Management.Automation.PSCustomObject]) { continue }
        $prop = $doc.PSObject.Properties["cleanupPeriodDays"]
        if ($null -eq $prop) { continue }
        $v = $prop.Value
        if ($null -eq $v -or $v -is [bool]) { continue }
        # By type NAME: Windows PowerShell 5.1 may not have System.Numerics
        # loaded, and a missing type literal would throw.
        $numTypes = @("System.Byte", "System.Int16", "System.Int32", "System.Int64", "System.Single",
                      "System.Double", "System.Decimal", "System.Numerics.BigInteger")
        if ($numTypes -notcontains $v.GetType().FullName) { continue }
        $fv = [double]::NaN
        try { $fv = [double]$v } catch { }
        if ([double]::IsNaN($fv) -or [double]::IsInfinity($fv) -or $fv -lt 0) { continue }
        if ($fv -gt $days) { $days = $fv }
    }
    return $days
}

# Remove-WtrecStale: keep the directory bounded without breaking a resume.
# Notices go after 30 days; records of ANY status go only once IDLE
# (LastWriteTime - Find-WtrecRecord bumps it on every lookup) longer than
# max(30, cleanupPeriodDays) + 30 days, so a resumed subagent still finds
# its record. Must match wtrec_prune in the .sh sibling.
function Remove-WtrecStale([string]$root) {
    $dir = Join-Path $root $script:WtrecSubdir
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return }
    try {
        $cut = (Get-Date).AddDays(-30)
        Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'notice-*.txt' -and $_.LastWriteTime -lt $cut } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
    try {
        $window = (Get-WtrecCleanupDays $root) + 30
        # An unrepresentable window (infinite / beyond DateTime) prunes nothing.
        $recCut = (Get-Date).AddDays(-$window)
        Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'wt-*.json' -and -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -and $_.LastWriteTime -lt $recCut } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
}

# Remove-WtrecLanesParent: after the reap removed a lane reservation's
# worktree, remove its now-EMPTY parent `<R>.vco-lanes` (the path convention
# of vco_lib/lane_reservation.py: sibling of R's toplevel). Empty-only, never
# recursive: a directory still holding another lane's worktree or any stray
# file is left exactly as it is, and only that exact directory is touched.
# Must match _wtrec_rmdir_lanes_parent in the .sh sibling.
# Both paths are normalised lexically FIRST (GetFullPath: `.`, `..`, repeated
# separators; TrimEnd: trailing ones), then the parent is taken, so a trailing
# `/.` or `sub/..` in the recorded path counts the same as in the .sh
# (_wtrec_lexnorm; v0.2.102 W6-LOWS, R3 AA-5).
function Remove-WtrecLanesParent([string]$top, [string]$path) {
    try {
        $norm = { param($p) [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/') }
        $t = & $norm $top
        $parent = Split-Path -Parent (& $norm $path)
        $lanes = & $norm (Join-Path (Split-Path -Parent $t) ((Split-Path -Leaf $t) + ".vco-lanes"))
        if ((& $norm $parent) -ne $lanes) { return }
        $item = Get-Item -LiteralPath $lanes -Force -ErrorAction Stop
        if (-not $item.PSIsContainer) { return }
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return }
        if (@(Get-ChildItem -LiteralPath $lanes -Force -ErrorAction Stop).Count -gt 0) { return }
        # Directory.Delete without recursion refuses a non-empty directory on
        # its own, so a file that lands between the check and here survives.
        [System.IO.Directory]::Delete($lanes, $false)
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
    if ($mode -eq "reservation_existing") { return (& $out "skipped" "reservation_existing_never_removed") }
    if (@("head", "snapshot", "reservation") -notcontains $mode) { return (& $out "skipped" "unknown_mode") }
    if ([string]$rec.status -ne "created") { return (& $out "skipped" "already_handled") }
    if (-not $path -or -not $base -or -not $top -or $path -eq $top) { return (& $out "kept" "incomplete_record") }
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        $r = (& $out "removed" "already_gone")
        if ($mode -eq "reservation") { Remove-WtrecLanesParent $top $path }
        return $r
    }
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
    if ($LASTEXITCODE -eq 0) {
        # A reservation worktree's NEW branch goes too, but only while it still
        # points at the recorded base (update-ref compare-and-delete).
        if ($mode -eq "reservation" -and [string]$rec.branch_created -eq "yes") {
            $br = [string]$rec.branch
            $reason = "clean_branch_kept"
            if ($br) {
                & git -C $top update-ref -d "refs/heads/$br" $base 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { $reason = "clean" }
            }
            $r = (& $out "removed" $reason)
            Remove-WtrecLanesParent $top $path
            return $r
        }
        return (& $out "removed" "clean")
    }
    $msg = ($err -replace '[\t\r\n]+', ' ').Trim()
    if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
    return (& $out "kept" "git_refused: $msg")
}
