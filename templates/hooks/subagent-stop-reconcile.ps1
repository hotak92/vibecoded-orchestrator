# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-stop-reconcile.ps1 — Windows sibling of subagent-stop-reconcile.sh.
# SubagentStop hook that reconciles a subagent's filesystem changes back
# into the project's KG / code-graph / credential-scan / nudge-counter
# state, plus the V52-L.2 JSONL audit row.
#
# V52-L.1 (v0.2.52): five side effects, all soft-fail. See .sh sibling
# for the full design rationale.
#
# Performance budget: <2s no-modifications, <10s with modifications.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# V52-L.1: source the snapshot + credscan helpers. Optional — when
# missing, fall through to V52-L.2 logging-only behaviour.
$SnapshotHelper = Join-Path $ScriptDir "_lib/snapshot.ps1"
if (Test-Path $SnapshotHelper) { . $SnapshotHelper }
$CredScanHelper = Join-Path $ScriptDir "_lib/credscan.ps1"
if (Test-Path $CredScanHelper) { . $CredScanHelper }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LogDir = Join-Path $ProjectRoot ".claude/logs"
$StateDir = Join-Path $ProjectRoot ".claude/state"
$LogFile = Join-Path $LogDir "subagent-reconciliation.jsonl"
$AlertLog = Join-Path $LogDir "credential_alerts.jsonl"
$CodeQueue = Join-Path $StateDir "code-graph-queue.jsonl"

try {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction Stop | Out-Null
    }
} catch {
    exit 0
}
try {
    if (-not (Test-Path $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
    }
} catch {}

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

# Parse the SubagentStop payload. Field-synonym tolerance for
# transcript_path mirrors the .sh sibling.
$SessionId = ""
$AgentId = ""
$AgentType = ""
$TranscriptPath = ""
$StopReason = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.session_id)              { $SessionId      = [string]$payload.session_id }
        if ($payload.agent_id)                { $AgentId        = [string]$payload.agent_id }
        if ($payload.agent_type)              { $AgentType      = [string]$payload.agent_type }
        if ($payload.agent_transcript_path)   { $TranscriptPath = [string]$payload.agent_transcript_path }
        elseif ($payload.transcript_path)     { $TranscriptPath = [string]$payload.transcript_path }
        if ($payload.finish_reason)           { $StopReason     = [string]$payload.finish_reason }
        elseif ($payload.stop_reason)         { $StopReason     = [string]$payload.stop_reason }
    }
} catch {
    exit 0
}

# Step 1 (V52-L.2 preserved): emit the audit row.
$entry = [ordered]@{
    timestamp       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    session_id      = $SessionId
    agent_id        = $AgentId
    agent_type      = $AgentType
    transcript_path = $TranscriptPath
    stop_reason     = $StopReason
}
try {
    $line = $entry | ConvertTo-Json -Compress -Depth 5
    Add-Content -Path $LogFile -Value $line -ErrorAction Stop
} catch { }

# ----------------------------------------------------------------------
# Steps 2-5 (V52-L.1): only when we have a usable AgentId AND the
# snapshot helper sourced successfully. Otherwise short-circuit to
# logging-only mode.
# ----------------------------------------------------------------------
if (-not $AgentId) { exit 0 }
if (-not (Get-Command Diff-Snapshot -ErrorAction SilentlyContinue)) { exit 0 }

# Sanitize agent_id for snapshot filename (must match _Get-SafeAgentId
# in snapshot.ps1; we duplicate the sanitization here so the file
# existence check is bit-for-bit accurate).
$safeId = [regex]::Replace($AgentId, '[^a-zA-Z0-9_-]', '_')
if ($safeId.Length -gt 64) { $safeId = $safeId.Substring(0, 64) }
$snapFile = Join-Path $StateDir "subagent-snapshot-$safeId.json"
if (-not (Test-Path -LiteralPath $snapFile -PathType Leaf)) { exit 0 }

# Compute the diff. Cap to MAX_DIFF_FILES (default 500).
$maxDiff = if ($env:VCT_SUBAGENT_MAX_DIFF) {
    try { [int]$env:VCT_SUBAGENT_MAX_DIFF } catch { 500 }
} else { 500 }

$changedFiles = @()
try {
    $diffOut = Diff-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir
    if ($diffOut) {
        $changedFiles = @($diffOut | Select-Object -First $maxDiff)
    }
} catch { }

if (-not $changedFiles -or $changedFiles.Count -eq 0) {
    # Happy path: nothing changed. Clean up snapshot + exit.
    if (Get-Command Cleanup-Snapshot -ErrorAction SilentlyContinue) {
        try { Cleanup-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir } catch {}
    }
    exit 0
}

# Tally + classify. Mirrors the .sh sibling's bookkeeping.
$fileCount = 0
$totalBytes = [int64]0
$kgFiles = New-Object System.Collections.Generic.List[string]
$codeFiles = New-Object System.Collections.Generic.List[string]
$allFiles = New-Object System.Collections.Generic.List[string]

$codeExtRe = '\.(py|rs|ts|tsx|js|jsx|go|java|cs|c|cpp|h|hpp|rb|php|swift|kt|scala|sh|ps1|sql)$'

foreach ($rel in $changedFiles) {
    if (-not $rel) { continue }
    $fileCount++
    $abs = Join-Path $ProjectRoot $rel
    if (Test-Path -LiteralPath $abs -PathType Leaf) {
        try {
            $totalBytes += (Get-Item -LiteralPath $abs -ErrorAction Stop).Length
        } catch {}
    }
    $allFiles.Add($rel) | Out-Null
    if ($rel -match '^knowledge/.*\.md$') {
        $kgFiles.Add($rel) | Out-Null
    }
    if ($rel -match $codeExtRe) {
        $codeFiles.Add($rel) | Out-Null
    }
}

# -------------------- Step 2: KG sync --------------------
# Per-project kg-sync script. On Windows this is typically a .ps1, but
# we accept the unprefixed file too in case the project shipped a sh
# wrapper that's invoked via bash. Soft-fail on any error.
$kgSyncScript = ""
foreach ($candidate in @("kg-sync.ps1", "kg-sync.cmd", "kg-sync.bat", "kg-sync")) {
    $p = Join-Path $ProjectRoot ".claude/scripts/$candidate"
    if (Test-Path -LiteralPath $p -PathType Leaf) {
        $kgSyncScript = $p
        break
    }
}
if ($kgSyncScript -and $kgFiles.Count -gt 0) {
    foreach ($kf in $kgFiles) {
        $kfAbs = Join-Path $ProjectRoot $kf
        try {
            $proc = if ($kgSyncScript.EndsWith(".ps1")) {
                Start-Process -FilePath "pwsh" -ArgumentList @("-NoProfile","-File",$kgSyncScript,$kfAbs) -PassThru -WindowStyle Hidden -ErrorAction SilentlyContinue
            } else {
                Start-Process -FilePath $kgSyncScript -ArgumentList $kfAbs -PassThru -WindowStyle Hidden -ErrorAction SilentlyContinue
            }
            if ($proc) {
                # 30s timeout per kg-sync invocation.
                if (-not $proc.WaitForExit(30000)) {
                    try { $proc.Kill() } catch {}
                }
            }
        } catch {}
    }
}

# -------------------- Step 3: Code-graph queue --------------------
if ($codeFiles.Count -gt 0) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    try {
        $sb = New-Object System.Text.StringBuilder
        foreach ($f in $codeFiles) {
            $row = [ordered]@{
                timestamp  = $ts
                session_id = $SessionId
                agent_id   = $AgentId
                file_path  = $f
                source     = "subagent_stop"
            }
            $line = $row | ConvertTo-Json -Compress -Depth 5
            [void]$sb.AppendLine($line)
        }
        # Append-mode write; create the file if it doesn't exist.
        [System.IO.File]::AppendAllText($CodeQueue, $sb.ToString(), [System.Text.Encoding]::UTF8)
    } catch {}
}

# -------------------- Step 4: Credential scan --------------------
if ((Get-Command Scan-FileForCredentials -ErrorAction SilentlyContinue) -and $allFiles.Count -gt 0) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    foreach ($f in $allFiles) {
        $abs = Join-Path $ProjectRoot $f
        if (-not (Test-Path -LiteralPath $abs -PathType Leaf)) { continue }
        try {
            $hits = @(Scan-FileForCredentials -FilePath $abs)
            if ($hits.Count -gt 0) {
                $alert = [ordered]@{
                    timestamp  = $ts
                    file       = $abs
                    patterns   = ($hits -join " ")
                    session_id = $SessionId
                    agent_id   = $AgentId
                    agent_type = $AgentType
                    source     = "subagent_stop_reconciler"
                }
                $alertLine = $alert | ConvertTo-Json -Compress -Depth 5
                Add-Content -Path $AlertLog -Value $alertLine -ErrorAction SilentlyContinue
            }
        } catch {}
    }
}

# -------------------- Step 5: Nudge counter --------------------
if ($SessionId -and $fileCount -gt 0) {
    $workUnits = ($fileCount * 50) + [int]([math]::Floor($totalBytes / 4))
    $nudgeDir = if ($env:HOME) {
        Join-Path $env:HOME ".claude/metrics"
    } else {
        Join-Path $env:USERPROFILE ".claude\metrics"
    }
    $nudgeFile = Join-Path $nudgeDir "kg_update_tokens.jsonl"
    try {
        if (-not (Test-Path $nudgeDir)) {
            New-Item -ItemType Directory -Path $nudgeDir -Force -ErrorAction Stop | Out-Null
        }
    } catch {
        # Cannot create dir → skip step 5.
        if (Get-Command Cleanup-Snapshot -ErrorAction SilentlyContinue) {
            try { Cleanup-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir } catch {}
        }
        exit 0
    }

    # Read existing rows, find/upsert ours, atomic rewrite.
    $rows = @()
    if (Test-Path -LiteralPath $nudgeFile -PathType Leaf) {
        try {
            foreach ($line in (Get-Content -LiteralPath $nudgeFile -ErrorAction Stop)) {
                $line = $line.Trim()
                if (-not $line) { continue }
                try {
                    $entry = $line | ConvertFrom-Json -ErrorAction Stop
                    $rows += ,$entry
                } catch {}
            }
        } catch {}
    }

    $existingIdx = -1
    for ($i = 0; $i -lt $rows.Count; $i++) {
        if ($rows[$i].session_id -eq $SessionId) {
            $existingIdx = $i
            break
        }
    }

    if ($existingIdx -ge 0) {
        $r = $rows[$existingIdx]
        $existingWorkUnits = 0
        if ($r.PSObject.Properties["subagent_work_units"]) {
            try { $existingWorkUnits = [int64]$r.subagent_work_units } catch {}
        }
        $existingCount = 0
        if ($r.PSObject.Properties["subagent_count"]) {
            try { $existingCount = [int]$r.subagent_count } catch {}
        }
        $newRow = [ordered]@{}
        foreach ($p in $r.PSObject.Properties) { $newRow[$p.Name] = $p.Value }
        $newRow["subagent_work_units"] = $existingWorkUnits + $workUnits
        $newRow["subagent_last_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        $newRow["subagent_count"] = $existingCount + 1
        if ($newRow.Contains("subagent_ids") -and $newRow["subagent_ids"] -is [System.Collections.IList]) {
            $ids = [System.Collections.ArrayList]@($newRow["subagent_ids"])
            $ids.Add($AgentId) | Out-Null
            $newRow["subagent_ids"] = $ids
        } else {
            $newRow["subagent_ids"] = @($AgentId)
        }
        $rows[$existingIdx] = [PSCustomObject]$newRow
    } else {
        $newRow = [ordered]@{
            session_id          = $SessionId
            subagent_work_units = $workUnits
            subagent_last_at    = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            subagent_count      = 1
            subagent_ids        = @($AgentId)
        }
        $rows += ,[PSCustomObject]$newRow
    }

    # Deduplicate by session_id (last-wins matches the nudge hook).
    $seen = New-Object System.Collections.Generic.HashSet[string]
    $final = New-Object System.Collections.Generic.List[object]
    foreach ($r in $rows) {
        $sid = [string]$r.session_id
        if (-not $sid) { continue }
        if ($seen.Contains($sid)) { continue }
        [void]$seen.Add($sid)
        $final.Add($r)
    }

    # Atomic rewrite: temp file + rename.
    $tmp = $nudgeFile + ".tmp"
    try {
        $sb = New-Object System.Text.StringBuilder
        foreach ($r in $final) {
            $line = $r | ConvertTo-Json -Compress -Depth 5
            [void]$sb.AppendLine($line)
        }
        [System.IO.File]::WriteAllText($tmp, $sb.ToString(), [System.Text.Encoding]::UTF8)
        Move-Item -LiteralPath $tmp -Destination $nudgeFile -Force -ErrorAction Stop
    } catch {
        try { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue } catch {}
    }
}

# Cleanup: delete the snapshot file.
if (Get-Command Cleanup-Snapshot -ErrorAction SilentlyContinue) {
    try { Cleanup-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir } catch {}
}

exit 0
