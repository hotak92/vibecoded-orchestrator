# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# post-compact.ps1
# Fires on PostCompact event — after context compaction completes.
# Logs the event to ~/.claude/metrics/compactions.jsonl and notifies.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf

$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# Read stdin payload (may be empty).
$Payload = ""
try { $Payload = [Console]::In.ReadToEnd() } catch { }

$Trigger = "unknown"
$SessionId = ""
if ($Payload) {
    try {
        $payloadObj = $Payload | ConvertFrom-Json -ErrorAction Stop
        if ($payloadObj) {
            if ($payloadObj.trigger)    { $Trigger = [string]$payloadObj.trigger }
            if ($payloadObj.session_id) { $SessionId = [string]$payloadObj.session_id }
        }
    } catch { }
}
if (-not $Trigger) { $Trigger = "unknown" }

# Wipe the KG/codegraph injection dedup state for this session — the LLM
# just lost the context that included those previously-injected nodes, so
# re-injecting them on subsequent edits is now correct (and helpful).
# pre-edit-context-inject.ps1 writes to .claude/state/seen_kg_titles_<id>.txt.
if ($SessionId) {
    $SeenFile = Join-Path $ProjectDir ".claude/state/seen_kg_titles_$SessionId.txt"
    if (Test-Path $SeenFile) {
        Remove-Item $SeenFile -Force -ErrorAction SilentlyContinue
    }
}

# Same reasoning for pre-tool-use.ps1's per-session reads file (Build Anchor
# Protocol dedup). After compaction the LLM has lost its memory of which
# files it Read pre-compaction, so the reads list is no longer meaningful
# — Build Anchor should re-require a fresh Read before the next Write/Edit.
# Note: we do NOT wipe $ProjectDir/.claude/state/tool_backups/ — those
# have a different lifecycle (tool-call rollback, not dedup), and the
# pre-tool-use hook runs its own 24h GC there.
if ($SessionId) {
    $ReadsFile = Join-Path $ProjectDir ".claude/state/reads_$SessionId.txt"
    if (Test-Path $ReadsFile) {
        Remove-Item $ReadsFile -Force -ErrorAction SilentlyContinue
    }
}

# v0.2.29: same reset for the agent-skill-keyword-suggest hook's
# per-session dedup file. Without this, a "you might want to use skill X"
# suggestion that was emitted before compaction would NEVER fire again
# in the session — but post-compaction the user is plausibly starting a
# fresh logical task and the suggestion may once again be relevant.
# Path: $ProjectDir\.claude\state\keyword_suggest_<session_id>.txt.
# Matches what `agent-skill-keyword-match.py::_dedup_file` writes to
# (moved from $TMPDIR/claude_keyword_suggest/ to project-local state for
# the same resume-across-reboot reasoning as the ctx_snapshot block below).
if ($SessionId) {
    $KwSeen = Join-Path $ProjectDir ".claude/state/keyword_suggest_$SessionId.txt"
    if (Test-Path $KwSeen) {
        Remove-Item $KwSeen -Force -ErrorAction SilentlyContinue
    }
}

# Wipe diff-context-inject's per-session snapshot + compact flag. The
# CONTEXT_STATE.md diff baseline should reset whenever the LLM's context
# resets — PostCompact is the canonical reset point. Without this, the
# first prompt after a /compact would emit a "changed sections" diff
# anchored to the pre-compact baseline, which is incoherent (the LLM no
# longer has the pre-compact view of CONTEXT_STATE.md). pre-compact-save.ps1
# touches the compact flag as the cross-hook signal; this is the actual wipe.
if ($SessionId) {
    $CtxSnapshot = Join-Path $ProjectDir ".claude/state/ctx_snapshot_$SessionId"
    $CtxCompactFlag = Join-Path $ProjectDir ".claude/state/ctx_compact_flag_$SessionId"
    if (Test-Path $CtxSnapshot) {
        Remove-Item $CtxSnapshot -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $CtxCompactFlag) {
        Remove-Item $CtxCompactFlag -Force -ErrorAction SilentlyContinue
    }
    # Track C (v0.2.65): must match post-compact.sh. Reset the per-session
    # CONTEXT_STATE diff baseline (ctx_snapshot_session_<id>) too — after
    # compaction the LLM lost the pre-compact view. Reset ONLY the throwaway
    # snapshot baseline, never the per-session CONTEXT_STATE content file.
    $CtxSnapshotSession = Join-Path $ProjectDir ".claude/state/ctx_snapshot_session_$SessionId"
    if (Test-Path $CtxSnapshotSession) {
        Remove-Item $CtxSnapshotSession -Force -ErrorAction SilentlyContinue
    }
}

# Log compaction event under the user's home metrics dir.
$UserHome = [System.Environment]::GetFolderPath('UserProfile')
$LogDir = Join-Path $UserHome ".claude/metrics"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$line = "{""timestamp"":""$ts"",""project"":""$ProjectName"",""trigger"":""$Trigger""}"
try { Add-Content -Path (Join-Path $LogDir "compactions.jsonl") -Value $line } catch { }

# Cross-platform desktop notification.
$NotifyScript = Join-Path $ProjectDir ".claude/scripts/notify.py"
if ($PY -and (Test-Path $NotifyScript)) {
    try {
        & $PY $NotifyScript "Context compacted -- $ProjectName" "Trigger: $Trigger. Context re-injected." `
            --urgency low --icon dialog-information --expire-time 5000 2>$null | Out-Null
    } catch { }
}
exit 0
