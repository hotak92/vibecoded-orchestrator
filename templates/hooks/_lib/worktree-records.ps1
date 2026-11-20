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
# unchanged and the merged ones itself -- never at SubagentStop (v0.2.107
# REAP-INTERIM: only marked there; session-worktree-reap's SessionStart sweep
# removes them).

. (Join-Path $PSScriptRoot "utf8-nobom.ps1")  # BOM-free writes (review R5-8)
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
        Set-VcoUtf8Content -Path (Join-Path $dir "wt-$name.json") -Value ($rec | ConvertTo-Json -Compress)
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

# Set-WtrecStatus FILE STATUS REASON [SESSION TRANSCRIPT] -- record the reap
# outcome. With -WithSession (a SubagentStop decision) the session and
# transcript of that stop are stored too (reap_session, reap_transcript), so
# Invoke-WtrecSweep can leave the starting session's worktrees alone. Must
# match wtrec_set in the .sh sibling.
function Set-WtrecStatus([string]$file, [string]$status, [string]$reason, [string]$sessionId = "", [string]$transcript = "", [switch]$WithSession) {
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return }
    try {
        $h = [ordered]@{}
        foreach ($p in $rec.PSObject.Properties) { $h[$p.Name] = $p.Value }
        $h["status"] = $status
        $h["status_reason"] = $reason
        $h["status_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        if ($WithSession) {
            $h["reap_session"] = $sessionId
            $h["reap_transcript"] = $transcript
        }
        Set-VcoUtf8Content -Path $file -Value ($h | ConvertTo-Json -Compress)
    } catch { }
}

# Test-WtrecKeptAlertOnce FILE SESSION PATH -- $true when the PARENT should be
# told "worktree KEPT" now, $false when it was already told about THIS path in
# THIS session. Must match wtrec_kept_alert_once in the .sh sibling (see the
# WHY there, including which gate decides): SubagentStop carries no field that
# separates an interim stop (the subagent is waiting on its own background
# work) from a final one, so the notice is emitted once per worktree path per
# session, and the call site words it so the reader knows the agent may still
# be running. In the ordinary sequence Invoke-WtrecReap's PRE-EXISTING
# already_handled gate already suppresses the second stop of one launch whose
# worktree was kept (a reap candidate gets no notice at all); this
# marker covers the rewritten-record case and the detached-job race.
# The marker rides the agent's own record, so Remove-WtrecStale retires it
# with the record. FAIL-OPEN: an unreadable record, a valid-JSON non-object
# record or a failed write means "emit" (never rewrite a record we cannot
# read as an object).
function Test-WtrecKeptAlertOnce([string]$file, [string]$sessionId, [string]$path) {
    if (-not $file -or -not $path) { return $true }
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return $true }
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return $true }
    # A valid-JSON non-object record (a hand-corrupted file) is emitted for,
    # never rewritten: enumerating a string/array's members would mangle it.
    if ($rec -isnot [System.Management.Automation.PSCustomObject]) { return $true }
    try {
        # Case-SENSITIVE comparison, exactly as the .sh sibling's `==`
        # (PowerShell's default -eq is case-insensitive).
        if ([string]$rec.kept_alert_path -ceq $path -and [string]$rec.kept_alert_session -ceq $sessionId) { return $false }
        $h = [ordered]@{}
        foreach ($p in $rec.PSObject.Properties) { $h[$p.Name] = $p.Value }
        $h["kept_alert_path"] = $path
        $h["kept_alert_session"] = $sessionId
        # Atomic like the .sh's tmp + os.replace: a crash mid-write leaves the
        # previous record intact instead of truncating it. File.Replace needs
        # a real backup path (PowerShell turns a $null string argument into
        # "", which .NET rejects), and works on both .NET Framework (Windows
        # PowerShell 5.1) and .NET Core (pwsh 7). The backup is deleted at
        # once; a crash mid-call leaves wt-*.json.bak, which Remove-WtrecStale
        # ages out with wt-*.json.tmp.
        $tmp = "$file.tmp"
        $bak = "$file.bak"
        Set-VcoUtf8Content -Path $tmp -Value ($h | ConvertTo-Json -Compress)
        [System.IO.File]::Replace($tmp, (Resolve-VcoWritePath $file), $bak)
        [System.IO.File]::Delete($bak)
    } catch { return $true }
    return $true
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
# its record. A `wt-*.json.tmp` (or `.bak`) left by a crash between
# Test-WtrecKeptAlertOnce's tmp write and its rename ages out on that same
# window (nothing reads it - every lookup is by exact name). Must match
# wtrec_prune in the .sh sibling.
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
            Where-Object { ($_.Name -like 'wt-*.json' -or $_.Name -like 'wt-*.json.tmp' -or $_.Name -like 'wt-*.json.bak') -and -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -and $_.LastWriteTime -lt $recCut } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
}

# Get-WtrecChangedFiles ROOT ID -- v0.2.102 W6-C2 (census F7): the ABSOLUTE
# paths of the files the agent changed in ITS OWN worktree (uncommitted +
# committed since the recorded base). Exact attribution, unlike the shared-
# tree snapshot diff. Empty when there is no record or the worktree is gone.
# Read-only. MUST MATCH wtrec_changed_files in the .sh.
function Get-WtrecChangedFiles([string]$root, [string]$id) {
    $file = Find-WtrecRecord $root $id
    if (-not $file) { return @() }
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return @() }
    $path = [string]$rec.path
    $base = [string]$rec.base
    if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Container)) { return @() }
    $rels = New-Object System.Collections.Generic.List[string]
    # NUL-separated (-z): porcelain QUOTES a path with a space even under
    # core.quotepath=off (review R4c-7). Every -z entry is `XY <path>`; a
    # rename/copy (R/C) is followed by its ORIGIN path (skipped).
    try {
        $raw = (@(& git -C $path status --porcelain -z --untracked-files=all 2>$null) -join "`n")
        $skip = $false
        foreach ($e in ($raw -split "`0")) {
            if ($skip) { $skip = $false; continue }
            if (-not $e -or $e.Length -lt 4) { continue }
            if ($e[0] -eq 'R' -or $e[0] -eq 'C') { $skip = $true }
            $rels.Add($e.Substring(3))
        }
        if ($base) {
            $raw = (@(& git -C $path diff --name-only -z $base HEAD 2>$null) -join "`n")
            foreach ($e in ($raw -split "`0")) {
                if ($e) { $rels.Add([string]$e) }
            }
        }
    } catch { }
    $out = @($rels | Sort-Object -Unique -CaseSensitive | ForEach-Object { "$path/$_" })
    return $out
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

# Reaping (v0.2.107 REAP-INTERIM, owner rulings 2026-11-20) -- must match the
# "Reaping" block of the .sh sibling, which carries the full WHY.
# SubagentStop (Invoke-WtrecReap) never removes a worktree: SubagentStop also
# fires on an INTERIM stop and the payload cannot tell it from a final one,
# so a provably unchanged worktree is only marked `status: reap_candidate`
# (with reap_marked_epoch) and a changed one `kept`; both store the stop's
# reap_session and reap_transcript. Invoke-WtrecSweep, run at SessionStart by
# session-worktree-reap, removes (a) an unchanged candidate idle for
# WtrecAbandonDays and (b) a kept worktree whose HEAD is merged into the main
# checkout's current branch and whose status is clean (ignored files do not
# count), with the branch VCO created for it (`git branch -d`). Never a
# worktree of the starting session, a record without a session, in_place or
# reservation_existing; every condition is re-checked right before
# `git worktree remove` (never --force), and any doubt keeps the worktree.
$script:WtrecAbandonDays = 7  # MUST MATCH WTREC_ABANDON_DAYS in the .sh

# Invoke-WtrecReap: SubagentStop's step -- decide the agent's worktree WITHOUT
# removing it. Returns $null when the agent has no record, else an object
# with Decision (candidate|removed|kept|skipped; removed only as
# already_gone), Reason and Path. Same as wtrec_reap in the .sh sibling.
function Invoke-WtrecReap([string]$root, [string]$id, [string]$sessionId = "", [string]$transcript = "") {
    $file = Find-WtrecRecord $root $id
    if (-not $file) { return $null }
    return (Invoke-WtrecReapRecord $file "candidate" $sessionId $transcript)
}

# Set-WtrecCandidate FILE SESSION TRANSCRIPT -- status reap_candidate plus what
# Invoke-WtrecSweep needs. Must match _wtrec_mark_candidate in the .sh.
function Set-WtrecCandidate([string]$file, [string]$sessionId, [string]$transcript) {
    $rec = Read-WtrecRecord $file
    if (-not $rec -or $rec -isnot [System.Management.Automation.PSCustomObject]) { return }
    try {
        $h = [ordered]@{}
        foreach ($p in $rec.PSObject.Properties) { $h[$p.Name] = $p.Value }
        $h["status"] = "reap_candidate"
        $h["status_reason"] = "unchanged_reap_deferred"
        $h["status_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        $h["reap_session"] = $sessionId
        $h["reap_transcript"] = $transcript
        $h["reap_marked_epoch"] = [int64][Math]::Floor(([DateTimeOffset]::UtcNow).ToUnixTimeMilliseconds() / 1000)
        Set-VcoUtf8Content -Path $file -Value ($h | ConvertTo-Json -Compress)
    } catch { }
}

# Get-WtrecWorktreeBlock LISTED PATH -- the `git worktree list --porcelain`
# lines (LISTED, an array) of the worktree at PATH (compared as full,
# normalized paths: git prints forward slashes on Windows); $null when PATH
# is not listed. Must match _wtrec_wt_block in the .sh.
function Get-WtrecWorktreeBlock([string[]]$listed, [string]$path) {
    $norm = { param($p) try { [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/') } catch { $p } }
    $want = & $norm $path
    $block = $null
    foreach ($l in $listed) {
        if ($l -like 'worktree *') {
            if ($null -ne $block) { break }
            if ((& $norm $l.Substring(9)) -eq $want) { $block = New-Object System.Collections.Generic.List[string]; $block.Add($l) }
        } elseif ($null -ne $block) {
            if ($l -eq "") { break }
            $block.Add($l)
        }
    }
    if ($null -eq $block) { return $null }
    return ,($block.ToArray())
}

# Test-WtrecInUse PATH -- 'yes' when a process has its cwd at or under PATH,
# 'no' when none has, 'unknown' when that cannot be told: Linux reads
# /proc/<pid>/cwd, macOS asks `lsof -d cwd`, anything else (Windows) is
# unknown. A process whose cwd cannot be read is skipped. Must match
# _wtrec_in_use in the .sh.
function Test-WtrecInUse([string]$path) {
    try {
        $want = New-Object System.Collections.Generic.List[string]
        $want.Add([System.IO.Path]::GetFullPath($path).TrimEnd('/'))
        $cwds = New-Object System.Collections.Generic.List[string]
        if ($IsLinux -and (Test-Path -LiteralPath '/proc/self')) {
            try { $rp = @(& realpath -- $path 2>$null); if ($LASTEXITCODE -eq 0 -and $rp.Count -gt 0) { $want.Add(([string]$rp[0]).TrimEnd('/')) } } catch { }
            foreach ($d in [System.IO.Directory]::GetDirectories('/proc')) {
                if ([System.IO.Path]::GetFileName($d) -notmatch '^[0-9]+$') { continue }
                try { $c = [System.IO.DirectoryInfo]::new("$d/cwd").LinkTarget } catch { continue }
                if (-not $c) { continue }
                if ($c.EndsWith(" (deleted)")) { $c = $c.Substring(0, $c.Length - 10) }
                $cwds.Add($c)
            }
        } elseif ($IsMacOS) {
            try { $rp = @(& realpath -- $path 2>$null); if ($LASTEXITCODE -eq 0 -and $rp.Count -gt 0) { $want.Add(([string]$rp[0]).TrimEnd('/')) } } catch { }
            $lines = @(& lsof -a -d cwd -Fn 2>$null)
            if ($LASTEXITCODE -ne 0) { return "unknown" }
            foreach ($l in $lines) { if ($l.StartsWith("n")) { $cwds.Add($l.Substring(1)) } }
        } else {
            return "unknown"
        }
        foreach ($c in $cwds) {
            $cn = $c.TrimEnd('/')
            foreach ($w in $want) {
                if ($cn -ceq $w -or $cn.StartsWith($w + "/", [System.StringComparison]::Ordinal)) { return "yes" }
            }
        }
        return "no"
    } catch { return "unknown" }
}

# Invoke-WtrecSweep ROOT CURRENT_SESSION -- run the two sweep rules over every
# record whose stop session is known and is not the starting session. Returns
# one object per decision (Decision, Reason, Path, Name). Must match
# wtrec_sweep in the .sh.
function Invoke-WtrecSweep([string]$root, [string]$currentSession) {
    $dir = Join-Path $root $script:WtrecSubdir
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return @() }
    $out = New-Object System.Collections.Generic.List[object]
    $now = [DateTime]::UtcNow
    $window = [TimeSpan]::FromDays($script:WtrecAbandonDays)
    $files = @(Get-ChildItem -LiteralPath $dir -File -Filter "wt-*.json" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'wt-*.json' -and -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) } |
        Sort-Object Name -CaseSensitive)
    foreach ($f in $files) {
        try {
            $rec = Read-WtrecRecord $f.FullName
            if (-not $rec -or $rec -isnot [System.Management.Automation.PSCustomObject]) { continue }
            $sid = $rec.reap_session
            if ($sid -isnot [string] -or -not $sid -or $sid -ceq $currentSession) { continue }
            $status = [string]$rec.status
            $r = $null
            if ($status -ceq "kept") {
                $r = Invoke-WtrecReapMerged $f.FullName
            } elseif ($status -ceq "reap_candidate") {
                $last = $f.LastWriteTimeUtc
                $t = [string]$rec.reap_transcript
                if ($t -and (Test-Path -LiteralPath $t -PathType Leaf)) {
                    $tl = (Get-Item -LiteralPath $t).LastWriteTimeUtc
                    if ($tl -gt $last) { $last = $tl }
                }
                if (($now - $last) -lt $window) { continue }
                $r = Invoke-WtrecReapRecord $f.FullName "remove" "" ""
            }
            if ($r) {
                $name = $f.Name.Substring(3, $f.Name.Length - 8)
                $out.Add([pscustomobject]@{ Decision = $r.Decision; Reason = $r.Reason; Path = $r.Path; Name = $name })
            }
        } catch { continue }
    }
    return $out.ToArray()
}

# Write-WtrecReapLog LOG HOOK AGENT DECISION REASON PATH -- one
# worktree-guard.jsonl row per reap decision. Must match wtrec_log_reap.
function Write-WtrecReapLog([string]$log, [string]$hook, [string]$agent, [string]$decision, [string]$reason, [string]$path) {
    try {
        $d = Split-Path -Parent $log
        if ($d) { New-Item -ItemType Directory -Force -Path $d -ErrorAction SilentlyContinue | Out-Null }
        $row = [ordered]@{
            timestamp     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            hook          = $hook
            decision      = "reap_" + $decision
            reason        = $reason
            agent_id      = $agent
            resolved_path = $path
        }
        Add-Content -Path $log -Value ($row | ConvertTo-Json -Compress) -ErrorAction Stop
    } catch { }
}

# Invoke-WtrecReapRecord FILE ACTION SESSION TRANSCRIPT -- the one decision
# for an UNCHANGED worktree (ACTION candidate: mark it; remove: remove it,
# sweep rule a). Same conditions as _wtrec_reap_rec in the .sh sibling,
# including `deferred` (record left a candidate) for a locked worktree or one
# a process works in (or when that cannot be told).
function Invoke-WtrecReapRecord([string]$file, [string]$action, [string]$sessionId, [string]$transcript) {
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return $null }
    $mode = [string]$rec.mode; $path = [string]$rec.path
    $base = [string]$rec.base; $top = [string]$rec.toplevel
    $out = {
        param($d, $r)
        if ($d -ne "skipped" -and $d -ne "deferred") {
            if ($action -eq "candidate") { Set-WtrecStatus $file $d $r $sessionId $transcript -WithSession }
            else { Set-WtrecStatus $file $d $r }
        }
        return [pscustomobject]@{ Decision = $d; Reason = $r; Path = $path }
    }
    if ($mode -eq "in_place") { return (& $out "skipped" "in_place_never_removed") }
    if ($mode -eq "reservation_existing") { return (& $out "skipped" "reservation_existing_never_removed") }
    if (@("head", "snapshot", "reservation") -notcontains $mode) { return (& $out "skipped" "unknown_mode") }
    $status = [string]$rec.status
    $allowed = if ($action -eq "candidate") { @("created", "reap_candidate") } else { @("reap_candidate") }
    if ($allowed -cnotcontains $status) { return (& $out "skipped" "already_handled") }
    if (-not $path -or -not $base -or -not $top -or $path -eq $top) { return (& $out "kept" "incomplete_record") }
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        $r = (& $out "removed" "already_gone")
        if ($mode -eq "reservation") { Remove-WtrecLanesParent $top $path }
        return $r
    }
    $listed = @()
    try { $listed = @(& git -C $top worktree list --porcelain 2>$null) } catch { }
    $block = Get-WtrecWorktreeBlock $listed $path
    if ($null -eq $block) { return (& $out "kept" "not_a_registered_worktree") }
    $head = ""
    try { $head = [string](& git -C $path rev-parse HEAD 2>$null | Select-Object -First 1) } catch { }
    if ($head.Trim() -ne $base) { return (& $out "kept" "head_moved_or_commits") }
    $st = (& git -C $path status --porcelain --untracked-files=all --ignored=matching 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0) { return (& $out "kept" "status_unreadable") }
    if ($st.Trim()) { return (& $out "kept" "has_changes") }
    if ($action -eq "candidate") {
        Set-WtrecCandidate $file $sessionId $transcript
        return [pscustomobject]@{ Decision = "candidate"; Reason = "unchanged_reap_deferred"; Path = $path }
    }
    if (@($block | Where-Object { $_ -like 'locked*' }).Count -gt 0) { return (& $out "deferred" "locked") }
    $use = Test-WtrecInUse $path
    if ($use -eq "yes") { return (& $out "deferred" "in_use") }
    if ($use -ne "no") { return (& $out "deferred" "in_use_unknown") }
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

# Invoke-WtrecReapMerged FILE -- sweep rule (b): remove a KEPT worktree whose
# work is merged into the main checkout's current branch. Returns an object
# (Decision removed|deferred, Reason, Path) when it removed the worktree or a
# doubt held a merged one back, and $null when the worktree is simply not
# eligible (wrong mode or status, gone, unchanged, not merged yet). Writes
# the record only on removal. Must match _wtrec_reap_merged in the .sh.
function Invoke-WtrecReapMerged([string]$file) {
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return $null }
    $mode = [string]$rec.mode; $path = [string]$rec.path
    $base = [string]$rec.base; $top = [string]$rec.toplevel
    if (@("head", "snapshot", "reservation") -cnotcontains $mode) { return $null }
    if ([string]$rec.status -cne "kept") { return $null }
    if (-not $path -or -not $base -or -not $top -or $path -eq $top) { return $null }
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { return $null }
    $defer = { param($r) return [pscustomobject]@{ Decision = "deferred"; Reason = $r; Path = $path } }
    # (No `| Select-Object -First 1` on these: it stops the native command and
    # leaves $LASTEXITCODE meaningless.)
    $o = @(& git -C $path rev-parse --verify -q HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $o.Count -lt 1) { return $null }
    $head = ([string]$o[0]).Trim()
    if (-not $head -or $head -eq $base) { return $null }
    $listed = @(& git -C $top worktree list --porcelain 2>$null)
    if ($LASTEXITCODE -ne 0) { return (& $defer "worktree_list_failed") }
    # The MAIN checkout is the first entry git lists (the main worktree).
    $main = ""
    if ($listed.Count -gt 0 -and ([string]$listed[0]) -like 'worktree *') { $main = ([string]$listed[0]).Substring(9) }
    if (-not $main) { return (& $defer "main_checkout_unknown") }
    $norm = { param($p) try { [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/') } catch { $p } }
    if ((& $norm $main) -eq (& $norm $path)) { return $null }
    $o = @(& git -C $main symbolic-ref -q HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $o.Count -lt 1 -or -not ([string]$o[0]).Trim()) { return (& $defer "main_branch_unknown") }
    $mref = ([string]$o[0]).Trim()
    $o = @(& git -C $main rev-parse --verify -q "$mref^{commit}" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $o.Count -lt 1 -or -not ([string]$o[0]).Trim()) { return (& $defer "main_branch_unknown") }
    $mhead = ([string]$o[0]).Trim()
    & git -C $main merge-base --is-ancestor $head $mhead 2>$null | Out-Null
    $rc = $LASTEXITCODE
    if ($rc -eq 1) { return $null }
    if ($rc -ne 0) { return (& $defer "ancestry_unknown") }
    # -- merged: every doubt below keeps it --
    $block = Get-WtrecWorktreeBlock $listed $path
    if ($null -eq $block) { return (& $defer "not_a_registered_worktree") }
    $br = ""
    if ($mode -eq "reservation") {
        $br = [string]$rec.branch
        if (-not $br -or ($block -cnotcontains "branch refs/heads/$br")) { return (& $defer "record_mismatch") }
    } elseif ($block -cnotcontains "detached") { return (& $defer "record_mismatch") }
    if (@($block | Where-Object { $_ -like 'locked*' }).Count -gt 0) { return (& $defer "locked") }
    $st = (& git -C $path status --porcelain --untracked-files=all 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0) { return (& $defer "status_unreadable") }
    if ($st.Trim()) { return (& $defer "has_changes") }
    # Re-check right before the removal: HEAD unchanged since the ancestry test.
    $o = @(& git -C $path rev-parse --verify -q HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $o.Count -lt 1 -or ([string]$o[0]).Trim() -ne $head) { return (& $defer "head_moved") }
    $use = Test-WtrecInUse $path
    if ($use -eq "yes") { return (& $defer "in_use") }
    if ($use -ne "no") { return (& $defer "in_use_unknown") }
    $err = (& git -C $top worktree remove $path 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        $msg = ($err -replace '[\t\r\n]+', ' ').Trim()
        if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
        return (& $defer "git_refused: $msg")
    }
    $reason = "merged"
    if ($mode -eq "reservation") {
        # Only the branch VCO created for the lane, and only through
        # `git branch -d` in the main checkout (refuses an unmerged branch).
        $deleted = $false
        if ([string]$rec.branch_created -eq "yes" -and -not $br.StartsWith("-")) {
            & git -C $main branch -d $br 2>$null | Out-Null
            $deleted = ($LASTEXITCODE -eq 0)
        }
        if (-not $deleted) { $reason = "merged_branch_kept" }
        Remove-WtrecLanesParent $top $path
    }
    Set-WtrecStatus $file "removed" $reason
    return [pscustomobject]@{ Decision = "removed"; Reason = $reason; Path = $path }
}
