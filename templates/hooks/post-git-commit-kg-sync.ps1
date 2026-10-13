# Parity: kept in step with post-git-commit-kg-sync.sh (this file's UTF-8 BOM serves
# Windows PowerShell 5.1 only and has no Bash-side counterpart; every
# behaviour must match the post-git-commit-kg-sync.sh sibling).
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

    # Root-commit safe (v0.2.102 W6-N F-3): a first commit has no HEAD~1, and
    # `git diff HEAD~1..HEAD` printed NOTHING there -- the fresh-repo first
    # build was reviewed with an empty diff. A root commit is shown against
    # the empty tree by `git show`; every other commit keeps the diff against
    # its FIRST parent (`git show` on a merge prints a combined diff instead).
    # No SIGPIPE hazard here (W6-N review MEDIUM-3, the .sh sibling's
    # pipefail defect): `Select-Object -First` stops the upstream native
    # command without raising, and this script sets no pipefail-like
    # preference. The >64 KiB commit test runs both siblings.
    & git rev-parse --verify --quiet 'HEAD~1' *> $null
    if ($LASTEXITCODE -eq 0) {
        $Diff       = (& git diff 'HEAD~1' HEAD --stat --no-color 2>$null | Select-Object -First 50) -join "`n"
        $DiffDetail = (& git diff 'HEAD~1' HEAD --no-color 2>$null | Select-Object -First 300) -join "`n"
    } else {
        $Diff       = (& git show --format= --stat --no-color HEAD 2>$null | Select-Object -First 50) -join "`n"
        $DiffDetail = (& git show --format= --no-color HEAD 2>$null | Select-Object -First 300) -join "`n"
    }

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
    # The prompt rides on STDIN / a prompt FILE, never on the command line
    # (Start-Process re-splits -ArgumentList; the prompt embeds `git diff`
    # output whose `---` the CLI parsed as an option; the Windows command
    # line is capped at 32 767 chars). [IO.File]::WriteAllText, not
    # Set-Content -Encoding utf8: the cmdlet form prepends a UTF-8 BOM on
    # Windows PowerShell 5.1.
    $PromptFile = Join-Path $LogDir "kg-commit-review.prompt"
    [System.IO.File]::WriteAllText($PromptFile, $prompt)
    # v0.2.102 W6-K (owner ruling 2026-10-11): spawn through the shared
    # backend chain -- Haiku by default; when Haiku is unavailable
    # (quota/429/auth, a REAL signal latched in the summary breaker) fall
    # back to the gateway flash models the local model gateway actually
    # serves (glm-5.3-flash -> deepseek-v4.1-flash -> qwen3.8-flash), then
    # to OpenAI when the user enabled OpenAI for summaries. One chain, one
    # home: .claude/scripts/kg_reviewer_backend.py. Broken/partial install
    # (python or the chain script missing): reported as a broken install
    # (below), then the pre-W6-K direct Haiku spawn.
    $ReviewerChain = Join-Path $ProjectRoot ".claude/scripts/kg_reviewer_backend.py"
    $LibDir = Join-Path $PSScriptRoot "_lib"
    $FindPy = Join-Path $LibDir "find-python.ps1"
    if (Test-Path $FindPy) { . $FindPy }
    # v0.2.102 W6-R (loud-fail rule) - must match the .sh sibling: a missing
    # python or a missing shipped chain script is a BROKEN install, named on
    # stderr and in this commit's kg-commit-reviews.jsonl row
    # (`broken_install`); the hook runs async, so the ledger row is the one a
    # user can find. (Unlike the .sh, which appends, the spawn below
    # TRUNCATES kg-commit-review.log as it redirects into it, so a line
    # written there would be lost: the ledger is this sibling's record.)
    $BrokenParts = @()
    if (-not $PY) {
        if (Test-Path $FindPy) { $BrokenParts += "no Python interpreter found (_lib/find-python.ps1 resolved none)" }
        else { $BrokenParts += "$FindPy is missing, so no Python interpreter was resolved" }
    }
    if (-not (Test-Path $ReviewerChain)) { $BrokenParts += "$ReviewerChain is missing" }
    $BrokenInstall = $BrokenParts -join "; "
    if ($BrokenInstall) {
        $brokenLine = "post-git-commit-kg-sync: broken install: $BrokenInstall - the KG reviewer runs the plain Haiku spawn without the backend fallback chain; repair it with the project's bundle update (launcher: project Settings -> Update bundle)"
        [Console]::Error.WriteLine($brokenLine)
    }
    # Through the ONE guarded spawn home (_lib/resolve-powershell.ps1). TWO
    # cmdlet-level parameter rejections were killing this spawn outright:
    #   * `-WindowStyle Hidden` is rejected by non-Windows PowerShell editions;
    #   * naming the SAME file for -RedirectStandardOutput and
    #     -RedirectStandardError is rejected on EVERY edition ("...are same").
    # stderr goes to `<log>.err` instead.
    # No --max-turns on the chain spawn: the turn budget is ONE knob owned
    # by the chain (DEFAULT_MAX_TURNS / VCO_KG_REVIEW_MAX_TURNS, v0.2.102
    # W6-N F-4).
    if (-not $BrokenInstall) {
        $proc = Start-VcoDetachedProcess -FilePath $PY `
            -ArgumentList @($ReviewerChain, 'run', '--prompt-file', $PromptFile, '--log', $logFile) `
            -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" `
            -WorkingDirectory $ProjectRoot -PassThru
    } else {
        # Broken install, named above: the direct Haiku spawn is KEPT so the
        # commit is still reviewed; it hides nothing (the breakage was just
        # reported in three places), and dropping it would lose the KG update.
        # With no python this branch cannot ask the chain for the turn
        # budget, so it mirrors it: the default 30 must match
        # DEFAULT_MAX_TURNS in .claude/scripts/kg_reviewer_backend.py, and
        # VCO_KG_REVIEW_MAX_TURNS must parse as resolve_max_turns() there
        # does: plain ASCII digits only (no sign, blanks or `_`, and NOT
        # trimmed), positive, within int64; anything else falls back to 30.
        # A parity test pins the three parsers to each other.
        $MaxTurns = 30
        $rawTurns = "$env:VCO_KG_REVIEW_MAX_TURNS"
        $parsedTurns = [int64]0
        if ($rawTurns -cmatch '\A[0-9]+\z' -and [int64]::TryParse($rawTurns, [ref]$parsedTurns) -and $parsedTurns -gt 0) { $MaxTurns = $parsedTurns }
        $allowedTools = "Read,Glob,Grep,mcp__weaviate-kg__hybrid_search,mcp__weaviate-kg__store_knowledge_node,Write,Edit"
        $proc = Start-VcoDetachedProcess -FilePath "claude" `
            -ArgumentList @('-p', '--model', 'haiku', '--max-turns', "$MaxTurns", '--no-session-persistence', '--allowedTools', $allowedTools) `
            -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" `
            -RedirectStandardInput $PromptFile `
            -WorkingDirectory $ProjectRoot -PassThru
    }

    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $msgEsc = $CommitMsg -replace '\\', '\\\\' -replace '"', '\"'
    # A failed spawn leaves $proc null; interpolating $proc.Id then emitted
    # `"pid":}` — a malformed line in a JSONL telemetry file that every later
    # reader has to survive. Record a null instead.
    $pidVal = if ($proc) { $proc.Id } else { "null" }
    $brokenField = ""
    if ($BrokenInstall) {
        $brokenEsc = $BrokenInstall -replace '\\', '\\\\' -replace '"', '\"'
        $brokenField = ",""broken_install"":""$brokenEsc"""
    }
    $line = "{""timestamp"":""$ts"",""commit"":""$CommitHash"",""message"":""$msgEsc"",""pid"":$pidVal$brokenField}"
    try { Add-Content -Path (Join-Path $LogDir "kg-commit-reviews.jsonl") -Value $line -ErrorAction Stop } catch { }

# No stdout: PostToolUse plain stdout is dropped per the v2.1.x
# contract, AND this hook runs `async: true` (fire-and-forget). Any
# emit here would be doubly discarded — leave it silent.
} finally { Pop-Location }
exit 0
