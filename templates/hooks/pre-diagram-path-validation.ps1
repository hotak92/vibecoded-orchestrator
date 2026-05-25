# Parity-confirmation: full body parity with pre-diagram-path-validation.sh.
#   - Sensitive env-var scrub (foreach below) mirrors .sh `unset ...`.
#   - VCT_DISABLE_HOOKS short-circuit mirrors .sh line 4.
#   - Stdin JSON parse + tool_input.file_path extraction mirrors .sh
#     Python one-liner.
#   - Path scoping (`.claude/diagrams/`) mirrors .sh case statement.
#   - Venv resolution chain mirrors .sh _VENV_BASE / VCT_VENV lookup.
#   - Validator invocation + exit code propagation mirrors .sh tail.
#
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# pre-diagram-path-validation.ps1
# PreToolUse hook (Phase 1.5 diagrams integration).
#
# Defense-in-depth on TWO entry points:
#   1. Native Write/Edit on .claude/diagrams/** paths.
#   2. MCP-routed saves via mcp__mermaid__* / mcp__excalidraw__* — the
#      wrapper MCP validates internally; this hook catches the call
#      BEFORE the wrapper subprocess spawns.
#
# Blocks calls whose path argument is under .claude/diagrams/ but
# violates the scoped-path rule (`<category>/<name>.{mmd,excalidraw}`
# with lowercase-kebab-case name).

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

$FilePath = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input) {
        # Probe order: native-tool conventional keys first, then MCP
        # wrapper save-tool conventional keys. First non-empty wins.
        foreach ($key in @('file_path', 'path', 'output', 'target', 'scene_path', 'name')) {
            $v = $payload.tool_input.$key
            if ($v -and ($v -is [string])) {
                $FilePath = [string]$v
                break
            }
        }
    }
} catch {
    # Empty / malformed stdin — keep $FilePath at default
}

if (-not $FilePath) { exit 0 }

# Belt-and-suspenders scope filter (the matcher in settings.json already
# limits us, but in case the matcher fires on a sibling glob).
if ($FilePath -notmatch '\.claude[/\\]diagrams[/\\]') { exit 0 }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

# Venv-Python resolution — match the chain used in pre-edit-context-inject.ps1.
$VenvBase = if ($env:VCT_INSTALL_ROOT) { $env:VCT_INSTALL_ROOT } else { $ProjectRoot }
$Venv = ""
if ($env:VCT_VENV) {
    foreach ($cand in @(
        (Join-Path $env:VCT_VENV "bin/python"),
        (Join-Path $env:VCT_VENV "Scripts/python.exe")
    )) {
        if (Test-Path $cand) { $Venv = $cand; break }
    }
}
if (-not $Venv) {
    foreach ($cand in @(
        (Join-Path $VenvBase ".venv/bin/python"),
        (Join-Path $VenvBase ".venv/Scripts/python.exe"),
        (Join-Path $VenvBase "claude_mcp_servers/.venv/bin/python"),
        (Join-Path $VenvBase "claude_mcp_servers/.venv/Scripts/python.exe")
    )) {
        if (Test-Path $cand) { $Venv = $cand; break }
    }
}
if (-not $Venv) {
    $Venv = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $Venv) {
    $Venv = (Get-Command python3 -ErrorAction SilentlyContinue).Source
}
if (-not $Venv) {
    # No Python — fail-open (silent allow). Mirrors .sh `[ -z "$PY" ] && exit 0`.
    exit 0
}

# Invoke the validator. stderr from the child process is passed through
# (Claude Code surfaces it as the block reason). Exit code becomes ours.
& $Venv -m vco_lib.diagram_paths validate --kind auto $FilePath
$rc = $LASTEXITCODE

if ($rc -eq 0) { exit 0 }
exit 2
