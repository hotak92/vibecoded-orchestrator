# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# diff-context-inject.ps1
# Diff-based context injection — only inject CHANGED sections of CONTEXT_STATE.md.
# First prompt: create baseline snapshot (full injection done by SessionStart hook).
# Subsequent prompts: output only changed sections (or nothing if unchanged).
# After /compact: reset baseline (detected via compact flag).
#
# Note: this PowerShell port uses a hash-set lookup (line content presence)
# rather than `diff` line-number extraction, so it is NOT affected by the
# bash-side --old-line-format leak that caused the 2026-05-07 GUI freeze
# in the .sh sibling. Parity-touch only — no behavioural change.

$ContextFile = ".claude/CONTEXT_STATE.md"
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$SnapshotDir = Join-Path $Tmp "claude_ctx_snapshots"
$SessionId = if ($env:CLAUDE_SESSION_ID) { $env:CLAUDE_SESSION_ID } else { "default" }
$SnapshotFile = Join-Path $SnapshotDir "snapshot_$SessionId"
$CompactFlag = Join-Path $SnapshotDir "compact_flag_$SessionId"

if (-not (Test-Path $SnapshotDir)) {
    New-Item -ItemType Directory -Path $SnapshotDir -Force | Out-Null
}

# If compact flag exists, reset baseline.
if (Test-Path $CompactFlag) {
    Remove-Item $CompactFlag -Force -ErrorAction SilentlyContinue
    Remove-Item $SnapshotFile -Force -ErrorAction SilentlyContinue
}

# If CONTEXT_STATE.md doesn't exist, nothing to do.
if (-not (Test-Path $ContextFile)) { exit 0 }

# If no snapshot exists, create baseline.
if (-not (Test-Path $SnapshotFile)) {
    Copy-Item -Path $ContextFile -Destination $SnapshotFile -Force
    exit 0
}

# Quick check: identical?
$currentBytes = [System.IO.File]::ReadAllBytes($ContextFile)
$snapshotBytes = [System.IO.File]::ReadAllBytes($SnapshotFile)
if ($currentBytes.Length -eq $snapshotBytes.Length) {
    $identical = $true
    for ($i = 0; $i -lt $currentBytes.Length; $i++) {
        if ($currentBytes[$i] -ne $snapshotBytes[$i]) { $identical = $false; break }
    }
    if ($identical) { exit 0 }
}

# Files differ — find changed sections.
$current = Get-Content $ContextFile
$snapshot = Get-Content $SnapshotFile

# Build a set of lines in the snapshot for quick "is this line new?" check.
# Note: matches the .sh's `diff --new-line-format='%dn'` behavior approximately:
# we treat a line as "changed" if its content doesn't appear in the snapshot.
$snapshotSet = @{}
foreach ($l in $snapshot) {
    if (-not $snapshotSet.ContainsKey($l)) { $snapshotSet[$l] = $true }
}

$changedSections = New-Object System.Collections.Specialized.OrderedDictionary
$currentSection = ""
$anyChanged = $false
foreach ($line in $current) {
    if ($line -match '^##\s') { $currentSection = $line }
    if ($currentSection -and -not $snapshotSet.ContainsKey($line)) {
        if (-not $changedSections.Contains($currentSection)) {
            $changedSections[$currentSection] = $true
            $anyChanged = $true
        }
    }
}

if (-not $anyChanged) {
    # Differ but no new content lines — file got shorter.
    Write-Output "[Context updated -- sections removed from CONTEXT_STATE.md]"
    Copy-Item -Path $ContextFile -Destination $SnapshotFile -Force
    exit 0
}

Write-Output "[Context update -- changed sections:]"
Write-Output ""

# Extract each changed section (header to next ## or EOF) from current file.
foreach ($header in $changedSections.Keys) {
    $emit = $false
    foreach ($line in $current) {
        if ($line -eq $header) { $emit = $true; Write-Output $line; continue }
        if ($emit -and $line -match '^##\s' -and $line -ne $header) { break }
        if ($emit) { Write-Output $line }
    }
    Write-Output ""
}

Copy-Item -Path $ContextFile -Destination $SnapshotFile -Force
exit 0
