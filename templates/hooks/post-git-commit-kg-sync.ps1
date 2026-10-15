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
#   The reviewer invokes the weaviate-kg MCP tools (hybrid_search,
#   store_knowledge_node) - those go through claude_mcp_servers/weaviate_mcp/
#   server.py which is access-aware via VCT_KG_ACCESS_LIST. Hybrid_search
#   reads from self+shared+peers; store_knowledge_node writes to the
#   project's own collection (writes are not multi-source). This hook
#   itself does NOT query Weaviate. Env propagation: Start-Process
#   inherits the current process env, so VCT_KG_ACCESS_LIST flows into
#   the spawned `claude` process and from there into the MCP server.

# post-git-commit-kg-sync.ps1
# Spawn the background KG reviewer for the commit. Mirror of
# post-git-commit-kg-sync.sh.
#
# v0.2.103 (owner rulings 2026-10-15): this is a THIN wrapper. Everything
# else lives in ONE home, .claude/scripts/kg_reviewer_backend.py `run`
# (with vco_lib.kg_reviewer_prompt / _model / _guard): the commit capture
# and research-first prompt, the model choice (one machine-wide setting,
# default Sonnet; non-flash gateway fallback or skip), the knowledge/-only
# tool scope, the post-run validator that reverts forbidden edits, the
# isolated `kg-sync: <hash> <subject>` commit of the reviewer's own files,
# and the recursion guard (a `kg-sync:` commit is never reviewed).

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# $PsExe + the ONE guarded detached-spawn home (Start-VcoDetachedProcess).
. "$PSScriptRoot/_lib/resolve-powershell.ps1"

# No `claude`-on-PATH check here (v0.2.103 R7 F6) - must match the .sh
# sibling: whether a reviewer can run is the backend's decision, in ONE
# home. Without the CLI its chain logs every claude rung as unavailable and
# can still reach the consent-gated OpenAI rung; an early silent exit here
# made that rung dead and left no log line or ledger row.

$ProjectRoot = (& git rev-parse --show-toplevel 2>$null | Out-String).Trim()
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$LogDir = Join-Path $ProjectRoot ".claude/logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$logFile = Join-Path $LogDir "kg-commit-review.log"

$Input = ""
try { $Input = [Console]::In.ReadToEnd() } catch { }

# Check if the commit actually succeeded - look for failure markers in the
# tool_response embedded in the JSON payload. We use a substring scan rather
# than depending on jq.
$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
$ToolResponseStr = ""
if ($Input -and $PY) {
    try {
        $ToolResponseStr = ($Input | & $PY -c "import sys,json; d=json.load(sys.stdin); v=d.get('tool_response',''); print(v if isinstance(v,str) else json.dumps(v))" 2>$null | Out-String).Trim()
    } catch { }
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

    $ReviewerChain = Join-Path $ProjectRoot ".claude/scripts/kg_reviewer_backend.py"
    # Loud-fail rule - must match the .sh sibling: a missing python or a
    # missing shipped reviewer script is a BROKEN install, named on stderr, in
    # kg-commit-review.log and in this commit's kg-commit-reviews.jsonl row
    # (`broken_install`). v0.2.103: the commit is then NOT reviewed - the
    # guard that keeps the reviewer inside knowledge/ and reverts forbidden
    # edits is Python, so an unguarded spawn would bypass the boundaries the
    # owner requires; the pre-v0.2.103 direct `claude` spawn (and its
    # turn-budget mirror) is retired, superseded by the guarded run.
    $BrokenParts = @()
    if (-not $PY) {
        if (Test-Path $FindPy) { $BrokenParts += "no Python interpreter found (_lib/find-python.ps1 resolved none)" }
        else { $BrokenParts += "$FindPy is missing, so no Python interpreter was resolved" }
    }
    if (-not (Test-Path $ReviewerChain)) { $BrokenParts += "$ReviewerChain is missing" }
    $BrokenInstall = $BrokenParts -join "; "

    $proc = $null
    if ($BrokenInstall) {
        $brokenLine = "post-git-commit-kg-sync: broken install: $BrokenInstall - the KG reviewer did NOT review this commit (its guarded run needs both); repair it with the project's bundle update (launcher: project Settings -> Update bundle)"
        [Console]::Error.WriteLine($brokenLine)
        $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        try { Add-Content -Path $logFile -Value "[$stamp] $brokenLine" -ErrorAction Stop } catch { }
    } else {
        # Through the ONE guarded spawn home (_lib/resolve-powershell.ps1).
        # The reviewer appends its own lines to kg-commit-review.log, so the
        # process's stdout/stderr go to SIDE files: a redirect onto the log
        # itself would truncate it on every spawn and hold it open while the
        # reviewer appends (and naming one file for both streams is rejected
        # on every edition). No --max-turns / --model here: each is ONE knob
        # owned by the chain (VCO_KG_REVIEW_MAX_TURNS; VCO_KG_REVIEW_MODEL /
        # launcher Preferences).
        $proc = Start-VcoDetachedProcess -FilePath $PY `
            -ArgumentList @($ReviewerChain, 'run', '--project-root', $ProjectRoot, '--commit', $CommitHash, '--log', $logFile) `
            -RedirectStandardOutput "$logFile.out" -RedirectStandardError "$logFile.err" `
            -WorkingDirectory $ProjectRoot -PassThru
    }

    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $msgEsc = $CommitMsg -replace '\\', '\\\\' -replace '"', '\"'
    # A failed (or skipped) spawn leaves $proc null: record a JSON null
    # rather than the malformed `"pid":}`.
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
# emit here would be doubly discarded - leave it silent.
} finally { Pop-Location }
exit 0
