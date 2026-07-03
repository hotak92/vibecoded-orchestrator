# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# Context Size Check Hook (PowerShell port)
# Monitor CONTEXT_STATE.md size and surface a warning when threshold exceeded.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# Shared session_id parse + path-safety sanitise (Get-VcoHookSessionId). One
# implementation for all four context hooks; see _lib/session-id.ps1.
. "$PSScriptRoot/_lib/session-id.ps1"

# MaxLines trigger = 500 (matches the documented CONTEXT_STATE.md "max 500";
# the 250-350 line working range is normal, so warning earlier just nags).
# MUST MATCH templates/hooks/context-size-check.sh (MAX_LINES).
$MaxLines = 500
$WarnLines = 300
$ContextFile = ".claude/CONTEXT_STATE.md"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

# Track C (v0.2.65): must match templates/hooks/context-size-check.sh. The
# shared Get-VcoHookSessionId parses session_id from the SessionStart stdin
# payload so we can also size-check this session's own CONTEXT_STATE file.
# Empty when payload absent/malformed → per-session block skipped.
# Defense-in-depth (review C-1): the helper sanitises the id to [A-Za-z0-9_-]
# before it reaches the file path below (hostile `/`/`..` → "default").
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$SessionId = Get-VcoHookSessionId -Stdin $HookStdin

# Test-ContextSizeThreshold: emit a size alert/notice for $File against the
# shared MaxLines/WarnLines thresholds. $Label is the display label. Reused for
# both the shared CONTEXT_STATE.md and the Track C per-session file — one
# threshold implementation, two callers (matches the .sh check_size_thresholds).
function Test-ContextSizeThreshold {
    param(
        [string]$File,
        [string]$Label
    )
    $lineCount = 0
    if (Test-Path $File) {
        $lineCount = (Get-Content $File -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    }

    if ($lineCount -ge $MaxLines) {
        Write-Output @"

$Label Size Alert (CRITICAL)
========================================================================
Current size: $lineCount lines (threshold: $MaxLines lines)

$Label has exceeded the recommended size. This can cause:
- Context bloat (losing track of current work)
- Catastrophic forgetting (old decisions not extracted)
- Reduced session efficiency

Recommended Action:
   Spawn doc-maintainer agent to refresh $Label:

   "Please spawn the doc-maintainer agent to refresh $Label"

   The agent will:
   1. Extract completed work to canonical docs (ARCHITECTURE.md, DECISIONS_LOG.md, etc.)
   2. Move historical context to knowledge graph nodes
   3. Keep only current work (the 250-350 line working range)
   4. Preserve all knowledge (no catastrophic forgetting)

========================================================================

"@
    } elseif ($lineCount -ge $WarnLines) {
        Write-Output @"

$Label Size Notice
========================================================================
Current size: $lineCount lines (warning threshold: $WarnLines lines)

$Label is approaching the recommended size limit of $MaxLines lines.

Consider refreshing soon with the doc-maintainer agent to:
- Extract completed work to canonical docs
- Keep $Label focused on current work
- Prevent context bloat

========================================================================

"@
    }
}

# 1. The shared CONTEXT_STATE.md rollup (original behaviour).
Test-ContextSizeThreshold -File $ContextFile -Label "CONTEXT_STATE.md"

# 2. Track C: this session's own CONTEXT_STATE file, IF it exists. Gated on a
# resolved session_id AND file existence — single-session projects pay nothing.
if ($SessionId) {
    $SessionCtxFile = Join-Path $ProjectDir ".claude/context/CONTEXT_STATE_$SessionId.md"
    if (Test-Path $SessionCtxFile) {
        Test-ContextSizeThreshold -File $SessionCtxFile -Label "CONTEXT_STATE_$SessionId.md"
    }
}

exit 0
