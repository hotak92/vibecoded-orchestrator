# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Wait-loop / polling guard -- THIN WRAPPER (v0.2.102 lane W4-D).
# OS-PARITY: ports wait-guard.sh. Registered FIVE times; every registration
# is this one script with a mode:
#
#   pretooluse       PreToolUse matchers Bash (if-filtered verb rules),
#                    PowerShell (NO if: a PowerShell loop keyword is not a
#                    command, so no if rule could match it -- the module
#                    judges loops itself; v0.2.102 review W5D-2),
#                    Read, and ScheduleWakeup|CronCreate|Monitor
#   record-expansion UserPromptExpansion, matcher loop|schedule (records a
#                    user-typed /loop or /schedule -- the documented direct
#                    path for a typed slash command)
#
#   stdin -> python -m vco_lib.wait_guard <mode> -> deny JSON on stdout
#
# The DECISION MODULE (vco_lib/wait_guard.py) owns every check. This
# wrapper owns nothing but interpreter resolution and stdin/stdout
# plumbing -- it forwards the module's stdout VERBATIM (the deny envelope)
# and lets the module's stderr lines through so the denial is visible in
# the transcript/logs.
#
# Kill switches, checked BEFORE any spawn: VCT_DISABLE_HOOKS (all hooks)
# and VCO_WAIT_GUARD=off/0/false (this guard only -- a USER decision). A
# missing venv/python is a silent no-op like every VCO hook. Never exit
# non-zero. MUST MATCH wait-guard.sh.

param([string]$Mode = "pretooluse")

$PSScriptRootLocal = $PSScriptRoot
$LibDir = Join-Path $PSScriptRootLocal "_lib"

# Scrub sensitive env before any subprocess.
$ScrubLib = Join-Path $LibDir "scrub-env.ps1"
if (Test-Path $ScrubLib) {
    . $ScrubLib
    Invoke-VctScrubSecretEnv
} else {
    foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
        if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    }
}

if ($env:VCT_DISABLE_HOOKS) { exit 0 }
if ($env:VCO_WAIT_GUARD) {
    $wg = $env:VCO_WAIT_GUARD.Trim().ToLowerInvariant()
    if ($wg -in @('off','0','false','no')) { exit 0 }
}

try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if ([string]::IsNullOrWhiteSpace($HookStdin)) { exit 0 }

$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }

# Prefer the VCO venv-Python so `import vco_lib.wait_guard` works; fall
# back to the portable interpreter.
$RunPy = $PY
$VenvLib = Join-Path $LibDir "resolve-vco-venv.ps1"
if (Test-Path $VenvLib) {
    . $VenvLib
    try {
        $VcoVenvPython = Resolve-VcoVenvPython -ScriptDir $PSScriptRootLocal
        if ($VcoVenvPython -and (Test-Path $VcoVenvPython)) { $RunPy = $VcoVenvPython }
    } catch { }
}

try {
    $HookStdin | & $RunPy -m vco_lib.wait_guard $Mode
} catch { }
exit 0
