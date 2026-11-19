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
# unchanged ones itself -- never at SubagentStop (v0.2.107 REAP-INTERIM: a reap
# candidate there, removed by session-worktree-reap at a later SessionStart).

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

function Set-WtrecStatus([string]$file, [string]$status, [string]$reason) {
    $rec = Read-WtrecRecord $file
    if (-not $rec) { return }
    try {
        $h = [ordered]@{}
        foreach ($p in $rec.PSObject.Properties) { $h[$p.Name] = $p.Value }
        $h["status"] = $status
        $h["status_reason"] = $reason
        $h["status_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
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
# Notices and session-ended markers go after 30 days; records of ANY status go only once IDLE
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
            Where-Object { ($_.Name -like 'notice-*.txt' -or $_.Name -like 'session-ended-*') -and $_.LastWriteTime -lt $cut } |
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

# Reaping (v0.2.107 REAP-INTERIM) -- must match the "Reaping" block of the .sh
# sibling, which carries the full WHY. SubagentStop (Invoke-WtrecReap) never
# removes a worktree: SubagentStop also fires on an INTERIM stop and the
# payload cannot tell it from a final one, so a provably unchanged worktree is
# only marked `status: reap_candidate` (with reap_session, reap_transcript,
# reap_marked_epoch). Invoke-WtrecSweep, run at SessionStart by
# session-worktree-reap, removes a candidate once its parent session ended (a
# `session-ended-<sid>` marker newer than the mark) or it is abandoned
# (record and transcript idle for WtrecAbandonDays); a candidate of the
# CURRENT session is never swept, and every unchanged-check runs again first.
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

# Set-WtrecSessionEnded / Clear-WtrecSessionEnded ROOT SESSION -- SessionEnd
# records the end; a resumed session's SessionStart withdraws it. Must match
# wtrec_session_ended / wtrec_session_resumed in the .sh.
function Set-WtrecSessionEnded([string]$root, [string]$sessionId) {
    if (-not (Test-WtrecName $sessionId)) { return }
    try {
        $dir = Join-Path $root $script:WtrecSubdir
        New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
        $m = Join-Path $dir "session-ended-$sessionId"
        [System.IO.File]::WriteAllText($m, "")
        [System.IO.File]::SetLastWriteTime($m, (Get-Date))
    } catch { }
}
function Clear-WtrecSessionEnded([string]$root, [string]$sessionId) {
    if (-not (Test-WtrecName $sessionId)) { return }
    try { Remove-Item -LiteralPath (Join-Path (Join-Path $root $script:WtrecSubdir) "session-ended-$sessionId") -Force -ErrorAction Stop } catch { }
}

# Invoke-WtrecSweep ROOT CURRENT_SESSION -- reap every candidate no session
# can resume any more. Returns one object per decided candidate (Decision,
# Reason, Path, Name). Must match wtrec_sweep in the .sh.
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
            if ([string]$rec.status -cne "reap_candidate") { continue }
            $sid = [string]$rec.reap_session
            if ($sid -and $sid -ceq $currentSession) { continue }
            $marked = $null
            $mv = $rec.reap_marked_epoch
            if ($null -ne $mv -and $mv -isnot [bool] -and $mv -isnot [string]) {
                try { $marked = [double]$mv } catch { $marked = $null }
            }
            $ok = $false
            if ($sid -and (Test-WtrecName $sid) -and $null -ne $marked) {
                $m = Join-Path $dir "session-ended-$sid"
                if (Test-Path -LiteralPath $m -PathType Leaf) {
                    $mEpoch = ([DateTimeOffset](Get-Item -LiteralPath $m).LastWriteTimeUtc).ToUnixTimeMilliseconds() / 1000.0
                    if ($mEpoch -ge $marked) { $ok = $true }
                }
            }
            if (-not $ok) {
                $last = $f.LastWriteTimeUtc
                $t = [string]$rec.reap_transcript
                if ($t -and (Test-Path -LiteralPath $t -PathType Leaf)) {
                    $tl = (Get-Item -LiteralPath $t).LastWriteTimeUtc
                    if ($tl -gt $last) { $last = $tl }
                }
                $ok = (($now - $last) -ge $window)
            }
            if (-not $ok) { continue }
            $r = Invoke-WtrecReapRecord $f.FullName "remove" "" ""
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
# (ACTION candidate: mark an unchanged worktree; remove: remove it). Same
# conditions as _wtrec_reap_rec in the .sh sibling.
function Invoke-WtrecReapRecord([string]$file, [string]$action, [string]$sessionId, [string]$transcript) {
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
    if ($action -eq "candidate") {
        Set-WtrecCandidate $file $sessionId $transcript
        return [pscustomobject]@{ Decision = "candidate"; Reason = "unchanged_reap_deferred"; Path = $path }
    }
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
