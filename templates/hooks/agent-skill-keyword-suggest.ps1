# agent-skill-keyword-suggest.ps1 — UserPromptSubmit hook sibling of
# agent-skill-keyword-suggest.sh. Scans the user prompt for keywords
# declared in agents'/skills' `keywords:` frontmatter and emits a short
# additionalContext envelope when any keyword matches.
#
# Filesystem contract: globs `.claude/agents/*.md` and
# `.claude/skills/*/SKILL.md`. The launcher's disable mechanism moves
# files into sibling `.claude/agents.disabled/` / `.claude/skills.disabled/`
# directories, so disabled entries naturally fall outside these globs —
# no DB lookup needed.
#
# Always exits 0 (never blocks a prompt). Silent when no keyword matches.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# Resolve a Python interpreter portably.
$FindPy = Join-Path $ScriptDir "_lib/find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
# Hardening (Wave-1 integration review): if _lib/find-python.ps1 is missing
# (partial install, manual hook copy without the _lib/ siblings) $PY stays
# unset and the hook would silently no-op even when `python` is on PATH.
# Fall back to a direct Get-Command probe so we degrade gracefully.
if (-not $PY) {
    foreach ($candidate in @('python3', 'python', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { $PY = $cmd.Source; break }
    }
}
if (-not $PY) { exit 0 }  # No Python interpreter found anywhere → silent no-op.

# Emit-AdditionalContext helper (preferred). If missing we still emit a
# bare JSON envelope inline via ConvertTo-Json so the suggestion reaches
# the LLM.
$EmitHelper = Join-Path $ScriptDir "_lib/emit-context.ps1"
if (Test-Path $EmitHelper) { . $EmitHelper }

# Project root resolution: CLAUDE_PROJECT_DIR is the canonical signal at
# hook fire time; fall back to PWD for ad-hoc invocations.
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

# Locate the matcher. Installed layout: hooks at <project>/.claude/hooks/,
# scripts at <project>/.claude/scripts/. Templates layout (uninstalled,
# orchestrator clone for testing): templates/hooks/ + templates/scripts/.
$Matcher = Join-Path $ScriptDir "..\scripts\agent-skill-keyword-match.py"
if (-not (Test-Path $Matcher)) {
    $alt = Join-Path $ScriptDir "..\..\templates\scripts\agent-skill-keyword-match.py"
    if (Test-Path $alt) {
        $Matcher = $alt
    } else {
        exit 0
    }
}

# Hook input contract (v2.1.x): JSON payload on stdin. We need the `prompt`
# field. v0.2.29: also extract `session_id` so the matcher can dedup
# already-suggested items across prompts in the same session.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

$Prompt = ""
$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.prompt) { $Prompt = [string]$payload.prompt }
    if ($payload -and $payload.session_id) { $SessionId = [string]$payload.session_id }
} catch {
    # Malformed JSON → silent no-op.
}
if (-not $Prompt) { exit 0 }

# Run the matcher. Pipe the prompt to its stdin; capture stdout. The
# matcher always exits 0 and prints either an empty string or 1-2 short
# lines. v0.2.29: pass --session-id so the matcher can dedup. Empty
# session_id → matcher's dedup just no-ops (back-compat).
$prevProjectDir = $env:CLAUDE_PROJECT_DIR
$env:CLAUDE_PROJECT_DIR = $ProjectRoot
$Msg = ""
try {
    $Msg = ($Prompt | & $PY $Matcher --session-id $SessionId 2>$null) -join "`n"
} catch {
    $Msg = ""
} finally {
    if ($null -eq $prevProjectDir) {
        Remove-Item Env:CLAUDE_PROJECT_DIR -ErrorAction SilentlyContinue
    } else {
        $env:CLAUDE_PROJECT_DIR = $prevProjectDir
    }
}

if (-not $Msg) { exit 0 }
if (-not ($Msg -match '\S')) { exit 0 }

if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
    Emit-AdditionalContext $Msg 'UserPromptSubmit'
} else {
    # Inline fallback envelope.
    $envelope = [ordered]@{
        hookSpecificOutput = [ordered]@{
            hookEventName     = 'UserPromptSubmit'
            additionalContext = $Msg
        }
    }
    $json = $envelope | ConvertTo-Json -Compress -Depth 8
    Write-Output $json
}

exit 0
