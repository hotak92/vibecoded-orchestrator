# Parity: kept in step with pre-compact-save.sh (this file's UTF-8 BOM serves
# Windows PowerShell 5.1 only and has no Bash-side counterpart; every
# behaviour must match the pre-compact-save.sh sibling).
# parity-confirmation 2026-05-10: .sh sibling now uses _lib/find-python.sh; this .ps1 already used _lib/find-python.ps1 — true parity, no asymmetric fix.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# pre-compact-save.ps1
# Fires BEFORE every context compaction (PreCompact event, matcher "*" --
# v0.2.102 W6-D widened it from "auto": the vendor hooks doc gives PreCompact
# exactly two matcher values, manual and auto, and a manual /compact under
# matcher "auto" saved NOTHING, so the reinject hook replayed a stale
# snapshot after every manual compaction).
# Saves a snapshot of current working state so compact-context-reinject.ps1
# can restore it after compaction.
#
# v0.2.102 W6-K (owner 2026-10-11): the "Recently Changed Files (last 5 min)"
# section (which actually listed files newer than CONTEXT_STATE.md -- a
# mislabelled heuristic) is replaced by "the last N modified files since the
# last compaction (or the chat's start when none)": the boundary comes from
# existing state (.claude/context/last-compact-marker, else the transcript's
# first-entry timestamp), files are listed newest-first, and N is the
# VCO_SNAPSHOT_FILELIST_N knob (default 30). The precompact_prune.py
# invocation was REMOVED the same round (owner-approved removal) -- the
# script no longer ships and the bundle update orphan-deletes the installed
# copy; compact-context-reinject no longer emits a pruned-summary section.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# Read stdin once and reuse. The hook contract delivers the full payload
# (including session_id and transcript_path) via stdin JSON; CLAUDE_*
# env vars are NOT populated by Claude Code (verified 2026-05-08).
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { $HookStdin = "{}" }

$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.session_id) { $SessionId = [string]$payload.session_id }
} catch {
    # Malformed/empty stdin — fall through to "default" below.
}
if (-not $SessionId) { $SessionId = "default" }

# v0.2.102 W6-C2: a SUBAGENT's compaction (UNVERIFIED whether PreCompact
# fires for one) must not touch the MAIN chat's snapshot or last-compact
# marker -- agent_id present -> nothing to save. Correct either way.
# MUST MATCH pre-compact-save.sh.
try {
    if ($payload -and $payload.agent_id) { exit 0 }
} catch { }

# Project root: the ONE rule (_lib/project-root.ps1, must match
# vco_lib/project_root.py): CLAUDE_PROJECT_DIR, else the nearest marked VCO
# project above the current location. None -> every write below (snapshot, marker, force-flush flag) is skipped.
. (Join-Path $PSScriptRoot "_lib/project-root.ps1")
$ProjectDir = Get-VcoProjectRoot
if (-not $ProjectDir) {
    [Console]::Error.WriteLine("pre-compact-save: no VCO project encloses $((Get-Location).Path); snapshot not saved")
    exit 0
}
$SnapshotFile = Join-Path $ProjectDir ".claude/context/pre-compact-snapshot.md"
$SnapshotDir = Split-Path $SnapshotFile -Parent

if (-not (Test-Path $SnapshotDir)) {
    New-Item -ItemType Directory -Path $SnapshotDir -Force | Out-Null
}

$lines = New-Object System.Collections.ArrayList
[void]$lines.Add("# Pre-Compaction Snapshot")
[void]$lines.Add("Saved: $((Get-Date).ToString('o'))")
[void]$lines.Add("")
[void]$lines.Add("## Modified Files (git status, top 30)")
try {
    $gitStatus = & git -C $ProjectDir status --short 2>$null
    if ($gitStatus) {
        $statusLines = @($gitStatus) | Select-Object -First 30
        foreach ($l in $statusLines) { [void]$lines.Add($l) }
    }
} catch { }
[void]$lines.Add("")
[void]$lines.Add("## Recently Modified Files (newest first, since the last compaction)")
# Parity-touch 2026-10-11 (W6-K review F3): the .sh sibling now records an
# explicit "(file list unavailable: no Python interpreter resolved)" line
# when nothing resolves -- its listing shells out to python. This .ps1
# listing is native PowerShell and needs no interpreter, so no equivalent
# arm exists here.
try {
    # The N knob (owner 2026-10-11: N=30 to start). One threshold, one home:
    # this listing is the only reader; the .sh sibling reads the same env var.
    # A non-positive/unparseable value falls back to 30.
    $ListN = 30
    $rawN = "$env:VCO_SNAPSHOT_FILELIST_N"
    if ($rawN) {
        $parsedN = 0
        if ([int]::TryParse($rawN, [System.Globalization.NumberStyles]::Integer,
                            [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsedN) -and $parsedN -gt 0) {
            $ListN = $parsedN
        }
    }
    # The boundary, from existing state (in this order): the last-compact
    # marker (epoch seconds; still the PREVIOUS compact's time here), else
    # the transcript's FIRST-ENTRY timestamp (the chat's start), else no
    # boundary (list the N most recent files outright).
    $boundary = $null
    $marker = Join-Path $ProjectDir ".claude/context/last-compact-marker"
    if (Test-Path -LiteralPath $marker) {
        $rawM = (Get-Content -LiteralPath $marker -TotalCount 1 -ErrorAction SilentlyContinue)
        $epochM = 0.0
        if ($rawM -and [double]::TryParse("$rawM".Trim(), [System.Globalization.NumberStyles]::Float,
                                          [System.Globalization.CultureInfo]::InvariantCulture, [ref]$epochM)) {
            $boundary = [DateTimeOffset]::FromUnixTimeMilliseconds([long]($epochM * 1000)).LocalDateTime
        }
    }
    if (-not $boundary -and $payload -and $payload.transcript_path -and (Test-Path -LiteralPath "$($payload.transcript_path)")) {
        try {
            $first = Get-Content -LiteralPath "$($payload.transcript_path)" -TotalCount 1 -ErrorAction Stop
            if ($first) {
                $entry = $first | ConvertFrom-Json -ErrorAction Stop
                if ($entry -and $entry.timestamp) {
                    $boundary = [DateTimeOffset]::Parse("$($entry.timestamp)",
                        [System.Globalization.CultureInfo]::InvariantCulture).LocalDateTime
                }
            }
        } catch { }
    }
    $exts = @(".py", ".md", ".json", ".yaml")
    $excludeNames = @(".git", "__pycache__", ".venv", "node_modules")
    $excludeRel = @(".claude/worktrees/", ".claude/logs/")
    $snapshotSelf = (Join-Path $ProjectDir ".claude/context/pre-compact-snapshot.md")
    $found = Get-ChildItem -Path $ProjectDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $fn = $_.FullName.Replace("\", "/")
            $exts -contains $_.Extension.ToLower() -and
            ($_.FullName -ne $snapshotSelf) -and
            (-not ($excludeNames -contains $_.Directory.Name)) -and
            (-not ($excludeRel | Where-Object { $_ -and $fn.Contains($_) })) -and
            ((-not $boundary) -or ($_.LastWriteTime -gt $boundary))
        } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First $ListN
    foreach ($f in $found) { [void]$lines.Add($f.FullName) }
} catch { }
[void]$lines.Add("")

# BOM-free (review R5-8): the snapshot is re-injected verbatim by
# compact-context-reinject; a BOM would ride into the model's context.
. (Join-Path $PSScriptRoot "_lib/utf8-nobom.ps1")
Set-VcoUtf8Content -Path $SnapshotFile -Value $lines

# Update the last-compact marker (epoch seconds) -- written BEFORE the next
# invocation's file list reads it, i.e. exactly after this compact.
# v0.2.102 W6-K: the precompact_prune.py invocation that used to sit here was
# removed with the script (owner-approved removal); the marker is what the
# file list above uses as its boundary on the NEXT compaction.
$Marker = Join-Path $ProjectDir ".claude/context/last-compact-marker"
try {
    # FLOOR, never round: `Get-Date -UFormat %s` rounds to the nearest
    # second on some builds, which can stamp the marker UP TO a second into
    # the future and make the next run's file list miss files written right
    # after the compaction. Same integer-second granularity as the .sh
    # sibling's `date +%s`.
    [long][Math]::Floor([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0) |
        Out-File -FilePath $Marker -Encoding ascii
} catch { }

# Set compact flag for diff-context-inject to reset its baseline.
# Project-local .claude/state/ — see diff-context-inject.ps1 for the
# rationale (resume-across-reboot, $TMPDIR wipe immunity). Filename is
# `ctx_compact_flag_*` to namespace within the shared dir and to stay
# in lockstep with diff-context-inject.ps1's reader.
$SnapshotDir2 = Join-Path $ProjectDir ".claude/state"
if (-not (Test-Path $SnapshotDir2)) {
    New-Item -ItemType Directory -Path $SnapshotDir2 -Force | Out-Null
}
$flag = Join-Path $SnapshotDir2 "ctx_compact_flag_$SessionId"
New-Item -ItemType File -Path $flag -Force | Out-Null

# V52-N (2026-06-09): signal running RL citation monitors to flush their
# accumulated answer window before the context goes away. The MCP's
# _rl_answer_monitor polls every 2 s for this sentinel and, when present,
# fires with whatever's accumulated regardless of the natural 25k-token
# threshold. Without this, every PreCompact event silently drops in-flight
# citation events (the transcript-window-based extractor can't recover
# from a compacted-away mid-answer state).
$Sentinel = Join-Path $ProjectDir ".claude/state/rl_monitors_force_flush.flag"
$SentinelDir = Split-Path $Sentinel -Parent
if (-not (Test-Path $SentinelDir)) {
    New-Item -ItemType Directory -Path $SentinelDir -Force | Out-Null
}
try {
    [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() | Set-Content -Path $Sentinel -Encoding ascii
} catch {
    # Fallback: at minimum ensure the file exists so the MCP picks it up.
    New-Item -ItemType File -Path $Sentinel -Force | Out-Null
}
exit 0
