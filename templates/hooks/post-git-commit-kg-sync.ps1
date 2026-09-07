# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
if ($env:CLAUDE_CODE_DISABLE_AUTO_MEMORY) { exit 0 }

# VCO-CENTRALIZED-KG: spawns a background `claude` CLI subprocess (PR #171 / 0.1.7).
#   The Haiku agent invokes the weaviate-kg MCP tools (hybrid_search,
#   store_knowledge_node) — those go through claude_mcp_servers/weaviate_mcp/
#   server.py which is access-aware via VCT_KG_ACCESS_LIST. Hybrid_search
#   reads from self+shared+peers; store_knowledge_node writes to the
#   project's own collection (writes are not multi-source). This hook
#   itself does NOT query Weaviate. Env propagation: Start-Process
#   inherits the current process env, so VCT_KG_ACCESS_LIST flows into
#   the spawned `claude` process and from there into the MCP server.

# post-git-commit-kg-sync.ps1
# Spawn a background Haiku agent to review the commit diff and update relevant
# KG nodes / docs to keep everything in sync. Mirror of post-git-commit-kg-sync.sh.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# $PsExe + the ONE guarded detached-spawn home (Start-VcoDetachedProcess).
. "$PSScriptRoot/_lib/resolve-powershell.ps1"

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { exit 0 }

$ProjectRoot = (& git rev-parse --show-toplevel 2>$null | Out-String).Trim()
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$LogDir = Join-Path $ProjectRoot ".claude/logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$Input = ""
try { $Input = [Console]::In.ReadToEnd() } catch { }

# Check if the commit actually succeeded — look for failure markers in the
# tool_response embedded in the JSON payload. We use a substring scan rather
# than depending on jq.
$ToolResponseStr = ""
if ($Input) {
    $LibDir = Join-Path $PSScriptRoot "_lib"
    $FindPy = Join-Path $LibDir "find-python.ps1"
    if (Test-Path $FindPy) { . $FindPy }
    if ($PY) {
        try {
            $ToolResponseStr = ($Input | & $PY -c "import sys,json; d=json.load(sys.stdin); v=d.get('tool_response',''); print(v if isinstance(v,str) else json.dumps(v))" 2>$null | Out-String).Trim()
        } catch { }
    }
}
if ($ToolResponseStr -match '(nothing to commit|no changes added|error:|fatal:)') { exit 0 }

Push-Location $ProjectRoot
try {
    $CommitHash = (& git rev-parse --short HEAD 2>$null | Out-String).Trim()
    if (-not $CommitHash) { $CommitHash = "unknown" }
    $CommitMsg = (& git log -1 --format=%s 2>$null | Out-String).Trim()
    if (-not $CommitMsg) { $CommitMsg = "unknown" }

    $LastReviewedFile = Join-Path $LogDir ".last_reviewed_commit"
    $LastReviewed = ""
    if (Test-Path $LastReviewedFile) {
        try { $LastReviewed = (Get-Content $LastReviewedFile -Raw -ErrorAction Stop).Trim() } catch { }
    }
    if ($CommitHash -eq $LastReviewed) { exit 0 }
    Set-Content -Path $LastReviewedFile -Value $CommitHash -Encoding ascii

    $Diff       = (& git diff HEAD~1..HEAD --stat --no-color 2>$null | Select-Object -First 50) -join "`n"
    $DiffDetail = (& git diff HEAD~1..HEAD --no-color 2>$null | Select-Object -First 300) -join "`n"

    $prompt = @"
Review the following git commit and update relevant knowledge graph nodes and documentation.

## Commit: $CommitHash -- $CommitMsg

### Files changed:
$Diff

### Diff (first 300 lines):
$DiffDetail

## Instructions:
1. Read the diff carefully and identify which KG nodes (in $ProjectRoot/knowledge/) or docs (in $ProjectRoot/docs/) need updating
2. For each affected area:
   - If an existing KG node covers this topic: update it with the new information
   - If this introduces a NEW concept/pattern not yet documented: create a new KG node
   - If docs reference behavior that changed: update them
3. Use hybrid_search to find existing relevant nodes before creating new ones
4. Keep updates minimal and factual -- just reflect what changed, don't add speculation
5. Skip trivial changes (typo fixes, formatting, test-only changes)
6. Use store_knowledge_node MCP tool to write KG nodes (they auto-sync to Weaviate)

Focus on architectural changes, new features, API changes, and pattern changes.
Skip if the commit is purely cosmetic or test-only.
"@

    $logFile = Join-Path $LogDir "kg-commit-review.log"
    $allowedTools = "Read,Glob,Grep,mcp__weaviate-kg__hybrid_search,mcp__weaviate-kg__store_knowledge_node,Write,Edit"
    # The prompt rides on STDIN, never on the command line, for two reasons:
    #   * Start-Process joins an ARRAY -ArgumentList with spaces and the child
    #     re-splits it (QUIRK 3 in _lib/resolve-powershell.ps1) — this prompt
    #     embeds `git diff` output, and the CLI parsed the diff's `---` token
    #     as an option ("error: unknown option '---'", exit 1) on every
    #     non-empty commit, so the review never actually ran;
    #   * the Windows command line is capped at 32 767 chars, and this prompt
    #     carries the first 300 lines of the unified diff.
    # `claude -p` with no prompt argument reads the prompt from stdin — the
    # same stdin shape summary_backends.call_cli adopted.
    # [IO.File]::WriteAllText, not Set-Content -Encoding utf8: on Windows
    # PowerShell 5.1 the cmdlet form prepends a UTF-8 BOM, which would then
    # be the first bytes of the model's prompt.
    $PromptFile = Join-Path $LogDir "kg-commit-review.prompt"
    [System.IO.File]::WriteAllText($PromptFile, $prompt)
    # Through the ONE guarded spawn home (_lib/resolve-powershell.ps1). TWO
    # cmdlet-level parameter rejections were killing this spawn outright:
    #   * `-WindowStyle Hidden` is rejected by non-Windows PowerShell editions;
    #   * naming the SAME file for -RedirectStandardOutput and
    #     -RedirectStandardError is rejected on EVERY edition ("...are same"),
    #     which is what this call did — so the commit review has never started
    #     on Windows either. stderr goes to `<log>.err` instead.
    $proc = Start-VcoDetachedProcess -FilePath "claude" `
        -ArgumentList @('-p', '--model', 'haiku', '--max-turns', '10', '--no-session-persistence', '--allowedTools', $allowedTools) `
        -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" `
        -RedirectStandardInput $PromptFile `
        -WorkingDirectory $ProjectRoot -PassThru

    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $msgEsc = $CommitMsg -replace '\\', '\\\\' -replace '"', '\"'
    # A failed spawn leaves $proc null; interpolating $proc.Id then emitted
    # `"pid":}` — a malformed line in a JSONL telemetry file that every later
    # reader has to survive. Record a null instead.
    $pidVal = if ($proc) { $proc.Id } else { "null" }
    $line = "{""timestamp"":""$ts"",""commit"":""$CommitHash"",""message"":""$msgEsc"",""pid"":$pidVal}"
    try { Add-Content -Path (Join-Path $LogDir "kg-commit-reviews.jsonl") -Value $line -ErrorAction Stop } catch { }

# No stdout: PostToolUse plain stdout is dropped per the v2.1.x
# contract, AND this hook runs `async: true` (fire-and-forget). Any
# emit here would be doubly discarded — leave it silent.
} finally { Pop-Location }
exit 0
