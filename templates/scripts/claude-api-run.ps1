# PowerShell wrapper: run one bounded headless Claude Code task billed to the
# Claude Console API key, and report what it cost (Windows equivalent of
# claude-api-run.sh). v0.2.104. Typical use: a final code review with Fable
# on API credits instead of the Claude subscription.
#
# Usage:
#   .\claude-api-run.ps1 --prompt-file review-prompt.md --report review.md `
#       [--model claude-fable-5-1] [--effort high] [--max-budget-usd 25] `
#       [--max-turns N] [--policy review|edit] [--cwd DIR] [--key-name NAME] `
#       [--json]
#
# The key is VCO's Shared secret ANTHROPIC_API_KEY_GATEWAY (Secrets panel,
# Shared scope), passed only in the child's environment. Each run appends one
# line (no prompt, no key) to <VCO metrics dir>\claude_api_runs.jsonl.
#
# Exit codes: whatever `python -m vco_lib.claude_api_run` exits (0 ok, 1 the
# run failed, 2 usage, 3 refused before spawning); 3 also = REFUSAL - no
# Python environment with vco_lib (wrapper-only).
# Every rule lives in ONE home (`vco_lib/claude_api_run.py`); this wrapper is
# a dumb forwarder that names the project it is shipped in as the requester
# of the key (`--project`; a later `--project` on the command line wins).
#
# PARITY: this file and the bash sibling `claude-api-run.sh` must stay in
# lockstep (same tiers, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `vco_lib.claude_api_run` needs only the standard library plus vco_lib.
$LadderImport = "import vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("claude-api-run: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("claude-api-run: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  claude-api-run did NOT run (exit 3): its shared venv ladder is missing. See the lines above.")
    exit 3
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

# NO BARE `python` FALLBACK - a shipped component does not get a silent
# fallback (standing rule). PARITY: the bash sibling refuses identically.
if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "claude-api-run" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("claude-api-run: Refusing to run with an unqualified interpreter -")
    [Console]::Error.WriteLine("claude-api-run: nothing was spawned and nothing was billed.")
    Write-VctLadderRefusalSummary -Tool "claude-api-run" -ExitCode 3
    exit 3
}

# The project this wrapper ships in (.claude\scripts\ -> project root).
& $VenvPython -m vco_lib.claude_api_run --project "$ProjectRoot" @args
exit $LASTEXITCODE
