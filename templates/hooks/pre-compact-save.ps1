# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# parity-confirmation 2026-05-10: .sh sibling now uses _lib/find-python.sh; this .ps1 already used _lib/find-python.ps1 — true parity, no asymmetric fix.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# pre-compact-save.ps1
# Fires BEFORE auto context compaction (PreCompact event).
# Saves a snapshot of current working state so compact-context-reinject.ps1
# can restore it after compaction.

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

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
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
[void]$lines.Add("## Recently Changed Files (last 5 min)")
try {
    $anchor = Join-Path $ProjectDir ".claude/CONTEXT_STATE.md"
    if (Test-Path $anchor) {
        $anchorTime = (Get-Item $anchor).LastWriteTime
        $exts = @(".py", ".md", ".json", ".yaml")
        $excludePatterns = @("\.git\\", "__pycache__\\", "\.venv\\", "node_modules\\", "\.claude\\worktrees\\", "\.claude\\logs\\",
                             "/.git/", "/__pycache__/", "/.venv/", "/node_modules/", "/.claude/worktrees/", "/.claude/logs/")
        $found = Get-ChildItem -Path $ProjectDir -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object {
                $_.LastWriteTime -gt $anchorTime -and
                $exts -contains $_.Extension.ToLower() -and
                -not ($excludePatterns | Where-Object { $_ -and $_.FullName -match [regex]::Escape($_) })
            } |
            Select-Object -First 15 |
            ForEach-Object { $_.FullName }
        foreach ($f in $found) { [void]$lines.Add($f) }
    }
} catch { }
[void]$lines.Add("")

Set-Content -Path $SnapshotFile -Value $lines -Encoding UTF8

# Generate pruned activity summary scoped to "since last compact" via the
# precompact_prune.py script (cross-OS, always Python).
$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$PruneScript = Join-Path $ProjectDir ".claude/scripts/precompact_prune.py"
if ($PY -and (Test-Path $PruneScript)) {
    try {
        $HookStdin | & $PY $PruneScript 2>$null | Out-Null
    } catch { }
}

# Update the last-compact marker AFTER the prune script ran.
$Marker = Join-Path $ProjectDir ".claude/context/last-compact-marker"
try {
    [int][double]::Parse((Get-Date -UFormat %s)) | Out-File -FilePath $Marker -Encoding ascii
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
exit 0
