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
#                    Read, and ScheduleWakeup|CronCreate
#                    (the Monitor tool is deliberately NOT guarded -- it is the
#                    sanctioned streaming tool, v0.2.104)
#   record-expansion UserPromptExpansion, matcher loop|schedule (records a
#                    user-typed /loop or /schedule -- the documented direct
#                    path for a typed slash command)
#
#   stdin -> python -m vco_lib.wait_guard <mode> -> deny JSON on stdout
#
# The DECISION MODULE (vco_lib/wait_guard.py) owns every check: foreground
# sleep waits, watch/tail -f/timeout+bash -c polling shapes, loops that
# wait, self-wake tools in subagents and unlocked-main sessions, and the
# Nth-identical status-check repeat detector. Since v0.2.103 a LITERAL
# fixed sleep BETWEEN other segments of one command (`start-x && sleep 3
# && curl ...`; Start-Sleep -Seconds 2 on PowerShell) is allowed -- a sleep
# inside a loop (body or condition), a non-literal duration, a
# sole/first/last sleep, or a wait hidden in a subshell, command
# substitution, xargs per-item command or function-body loop is still
# denied. `cmd & wait` is allowed (not a poll). This
# wrapper owns nothing but interpreter resolution and stdin/stdout
# plumbing -- it forwards the module's stdout VERBATIM (the deny envelope)
# and lets the module's stderr lines through so the denial is visible in
# the transcript/logs.
#
# Since v0.2.105 the module also: reads the script file a command runs
# (bash/sh <file>, ./file, source/. <file>, cat <file> | bash; .ps1 on
# PowerShell; at most 256 KiB, text only) and judges it in its script
# reading (loops that wait, watch, tail -f -- not a single pause); judges
# code fed to a shell on stdin / a here-string / a heredoc, case arms,
# coproc and code held in a variable; refuses the 3rd identical no-op call
# (true, :, exit 0, echo/printf ..., sleep 0) within 120 s; and allows a
# one-shot CronCreate (recurring: false) in the main chat.
# run_in_background is exempt from every FOREGROUND check (fix round 2):
# its own rule is that a background WAIT LOOP must be bounded -- a literal
# `timeout <N>` around the whole command or a literal iteration cap -- and
# a loop whose condition throws stderr away (`2>/dev/null`) is refused.
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
