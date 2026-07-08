# OS-EXEMPT-PARITY: Windows-only fix — drop the dead `$env:CLAUDE_SESSION_ID` middle branch from the SessionId resolution cascade. The .sh sibling never had this branch (audit confirmed; sh went straight from stdin-parse-fail to "default"), so there's no symmetrical change to make on the .sh side.
# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
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

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# Shared session_id parse + path-safety sanitise (Get-VcoHookSessionId). One
# implementation for all four context hooks; see _lib/session-id.ps1.
. "$PSScriptRoot/_lib/session-id.ps1"

# Hook input contract (v2.1.x): session_id arrives as JSON on stdin, not as
# the $CLAUDE_SESSION_ID env var (which Claude Code does NOT populate —
# verified empirically 2026-05-08). Reading the env var meant every session
# in this project shared the same `default` snapshot file, so two concurrent
# sessions silently stomped on each other's diff baseline.
#
# Defense-in-depth (review C-1): session_id is interpolated into file paths
# below (ctx_snapshot_* and CONTEXT_STATE_*.md). Get-VcoHookSessionId parses
# AND sanitises it ([A-Za-z0-9_-] only; a hostile id with `/` or `..` becomes
# the safe sentinel "default"). Must match the .sh sibling's
# vco_hook_session_id. This hook always wants a key, so an empty parse
# collapses to "default" at the $SessionId assignment below.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$SessionIdFromStdin = Get-VcoHookSessionId -Stdin $HookStdin

$ContextFile = ".claude/CONTEXT_STATE.md"
# Snapshot state lives under the project (gitignored .claude/state/) so it
# survives reboots and launcher restarts — Claude Code's `resume` feature
# can reuse a session_id across these boundaries, and a $TMPDIR-based path
# would lose the diff baseline mid-session when /tmp is wiped on boot.
# The `ctx_` filename prefix namespaces these files within the shared
# .claude/state/ dir (which also holds seen_kg_titles_*, reads_*, etc.).
if (-not $env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR = (Get-Location).Path }
$SnapshotDir = Join-Path $env:CLAUDE_PROJECT_DIR ".claude/state"
$SessionId = if ($SessionIdFromStdin) { $SessionIdFromStdin } else { "default" }

# V52-J Edit 4 (2026-06-09): export VCT_SESSION_ID so child processes
# inherit the session_id. The canonical telemetry emit path
# (claude_mcp_servers/rl_client/telemetry_emit.py::resolve_session_id)
# reads VCT_SESSION_ID as layer-2 of its 3-layer chain. Skip the
# "default" sentinel — we'd rather have empty than fake-key. Sibling:
# see templates/hooks/diff-context-inject.sh.
if ($SessionId -and $SessionId -ne "default") {
    $env:VCT_SESSION_ID = $SessionId
}

$SnapshotFile = Join-Path $SnapshotDir "ctx_snapshot_$SessionId"
$CompactFlag = Join-Path $SnapshotDir "ctx_compact_flag_$SessionId"

# Track C (v0.2.65): per-session CONTEXT_STATE file. Must match
# templates/hooks/diff-context-inject.sh. Concurrent long-lived chats against
# the same project each keep their own session_id and would otherwise clobber
# one shared CONTEXT_STATE.md. If a session writes its own
# .claude/context/CONTEXT_STATE_<session_id>.md, we diff it independently
# using a SECOND baseline keyed on `ctx_snapshot_session_`. The shared
# CONTEXT_STATE.md rollup is untouched. Zero cost when the per-session file
# is absent.
$SessionContextFile = Join-Path $env:CLAUDE_PROJECT_DIR ".claude/context/CONTEXT_STATE_$SessionId.md"
$SessionSnapshotFile = Join-Path $SnapshotDir "ctx_snapshot_session_$SessionId"

if (-not (Test-Path $SnapshotDir)) {
    New-Item -ItemType Directory -Path $SnapshotDir -Force | Out-Null
}

# 14-day GC for stale ctx_snapshot_* files — sessions that haven't fired
# in two weeks are stale enough that their baseline is no longer useful.
# Best-effort. Doesn't touch the compact flags (short-lived sentinels,
# cleaned by post-compact.ps1). The `ctx_snapshot_*` filter also matches the
# Track C per-session baseline `ctx_snapshot_session_*` (both throwaway diff
# baselines); it NEVER touches the per-session CONTEXT_STATE content files
# under .claude/context/.
# HK-4 (v0.2.75) accepted-scatter: one of 4 per-hook GC sweeps (uniform 14d);
# a shared sweeper is optional and deliberately SKIPPED to keep hooks
# single-file. MUST MATCH the .sh sibling. See pre-edit-context-inject.ps1.
try {
    $GcCutoff = (Get-Date).AddDays(-14)
    Get-ChildItem -Path $SnapshotDir -Filter "ctx_snapshot_*" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $GcCutoff } |
        ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
} catch { }

# If compact flag exists, reset baseline. Also reset the Track C per-session
# baseline so the post-compact view recomputes from the current file.
if (Test-Path $CompactFlag) {
    Remove-Item $CompactFlag -Force -ErrorAction SilentlyContinue
    Remove-Item $SnapshotFile -Force -ErrorAction SilentlyContinue
    Remove-Item $SessionSnapshotFile -Force -ErrorAction SilentlyContinue
}

# Diff-Context-Section: emit the changed ## sections of $LiveFile relative to
# its snapshot baseline $SnapFile, then refresh the baseline. $Label is used
# only in the emitted header. Reused for both the shared CONTEXT_STATE.md and
# the Track C per-session file — one diff implementation, two calls (matches
# the .sh sibling's diff_context_section).
function Invoke-DiffContextSection {
    param(
        [string]$LiveFile,
        [string]$SnapFile,
        [string]$Label
    )

    # If the live file doesn't exist, nothing to do.
    if (-not (Test-Path $LiveFile)) { return }

    # If no snapshot exists, create baseline.
    if (-not (Test-Path $SnapFile)) {
        Copy-Item -Path $LiveFile -Destination $SnapFile -Force
        return
    }

    # Quick check: identical?
    $currentBytes = [System.IO.File]::ReadAllBytes($LiveFile)
    $snapshotBytes = [System.IO.File]::ReadAllBytes($SnapFile)
    if ($currentBytes.Length -eq $snapshotBytes.Length) {
        $identical = $true
        for ($i = 0; $i -lt $currentBytes.Length; $i++) {
            if ($currentBytes[$i] -ne $snapshotBytes[$i]) { $identical = $false; break }
        }
        if ($identical) { return }
    }

    # Files differ — find changed sections.
    $current = Get-Content $LiveFile
    $snapshot = Get-Content $SnapFile

    # Build a set of lines in the snapshot for quick "is this line new?" check.
    # Matches the .sh's `diff --new-line-format='%dn'` behaviour approximately:
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
        Write-Output "[$Label updated -- sections removed]"
        Copy-Item -Path $LiveFile -Destination $SnapFile -Force
        return
    }

    Write-Output "[$Label update -- changed sections:]"
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

    Copy-Item -Path $LiveFile -Destination $SnapFile -Force
}

# 1. The shared CONTEXT_STATE.md rollup (the original, unchanged behaviour).
Invoke-DiffContextSection -LiveFile $ContextFile -SnapFile $SnapshotFile -Label "Context"

# 2. Track C: the per-session CONTEXT_STATE file, IF it exists. A second call
# against the `_session_` baseline — gated entirely on file existence, so
# projects that never write a per-session file are unaffected.
if (Test-Path $SessionContextFile) {
    Invoke-DiffContextSection -LiveFile $SessionContextFile -SnapFile $SessionSnapshotFile -Label "Session context"
}

exit 0
