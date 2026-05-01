# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# Context Size Check Hook (PowerShell port)
# Monitor CONTEXT_STATE.md size and surface a warning when threshold exceeded.

$MaxLines = 400
$WarnLines = 300
$ContextFile = ".claude/CONTEXT_STATE.md"

$LineCount = 0
if (Test-Path $ContextFile) {
    $LineCount = (Get-Content $ContextFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
}

if ($LineCount -ge $MaxLines) {
    Write-Output @"

CONTEXT_STATE.md Size Alert (CRITICAL)
========================================================================
Current size: $LineCount lines (threshold: $MaxLines lines)

CONTEXT_STATE.md has exceeded the recommended size. This can cause:
- Context bloat (losing track of current work)
- Catastrophic forgetting (old decisions not extracted)
- Reduced session efficiency

Recommended Action:
   Spawn doc-maintainer agent to refresh CONTEXT_STATE.md:

   "Please spawn the doc-maintainer agent to refresh CONTEXT_STATE.md"

   The agent will:
   1. Extract completed work to canonical docs (ARCHITECTURE.md, DECISIONS_LOG.md, etc.)
   2. Move historical context to knowledge graph nodes
   3. Keep only current work (<200 lines)
   4. Preserve all knowledge (no catastrophic forgetting)

========================================================================

"@
} elseif ($LineCount -ge $WarnLines) {
    Write-Output @"

CONTEXT_STATE.md Size Notice
========================================================================
Current size: $LineCount lines (warning threshold: $WarnLines lines)

CONTEXT_STATE.md is approaching the recommended size limit of $MaxLines lines.

Consider refreshing soon with the doc-maintainer agent to:
- Extract completed work to canonical docs
- Keep CONTEXT_STATE.md focused on current work
- Prevent context bloat

========================================================================

"@
}

exit 0
