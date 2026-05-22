# parity-confirmation 2026-05-16 (PR-32, Group K Phase B): full body parity
# audit confirmed — every .sh-side guard check is present in this .ps1 sibling:
#   - Sensitive env-var scrub (foreach loop below, lines 3-5) mirrors .sh
#     `unset SUPABASE_KEY ...` (line 4 of sibling).
#   - VCT_DISABLE_HOOKS short-circuit (line 6) mirrors `.sh` line 5.
#   - Tool-name == "Bash" early-out (line 33) mirrors `.sh` line 55.
#   - Vercel + --token regex match (lines 36-37) mirrors `.sh` lines 69-70.
#     .sh uses `LEAN_CTX_OFF=1 command grep -qE` to bypass any lean-ctx
#     wrapping of the grep binary; PowerShell uses the -match operator
#     (native regex engine, no external binary, no lean-ctx interposition
#     possible) — same matching semantics, no equivalent guard needed.
#   - Block + exit 2 with multi-line stderr (lines 39-55) mirrors `.sh`
#     lines 71-85 (cat heredoc + exit 2).
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
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

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($args) and $env:CLAUDE_TOOL_NAME etc. are EMPTY —
# verified empirically 2026-05-08 via stdin-capture diagnostic.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
$cmd = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name) { $ToolName = [string]$payload.tool_name }
        if ($payload.tool_input -and $payload.tool_input.command) {
            $cmd = [string]$payload.tool_input.command
        }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}

if ($ToolName -ne "Bash") { exit 0 }

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
