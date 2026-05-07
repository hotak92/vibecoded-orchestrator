# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# pre-vercel-token-guard.ps1
# PreToolUse hook: Block `vercel --token=...` invocations that would leak the
# token back into stdout (Claude Code shows tool output to the user/agent).
# Mirror of pre-vercel-token-guard.sh.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

param(
    [Parameter(Position=0)] [string]$ToolName = "",
    [Parameter(Position=1)] [string]$UserMessage = "",
    [Parameter(Position=2)] [string]$ToolArgs = ""
)

if ($ToolName -ne "Bash") { exit 0 }
$cmd = $ToolArgs

# Match `vercel` (or path/local-bin variants) followed somewhere by --token=... or --token <value>.
$hasVercel = $cmd -match '(^|[\s/])vercel(\s|$)'
$hasToken  = $cmd -match '--token[= ]'

if ($hasVercel -and $hasToken) {
    $stderrText = @"
BLOCKED: ``vercel --token=...`` echoes the token back in stdout (in the
``next:`` block on success), which leaks ``vcp_*`` strings into the tool-result
view. Use the env var instead, which the CLI reads transparently and never
prints:

    `$env:VERCEL_TOKEN = (Get-Content ~/.vct-secrets/shared/vercel_token); ``
      .\node_modules\.bin\vercel deploy --prod --yes

If you really need --token (e.g. machine that has no env access), pipe the
output through ``2>&1 | Select-Object -Last 3`` or redirect stderr and only
keep stdout (vercel writes the deployment URL on stdout, errors on stderr).
"@
    [Console]::Error.WriteLine($stderrText)
    exit 2
}
exit 0
