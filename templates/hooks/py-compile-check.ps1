# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# py-compile-check.ps1 — PostToolUse syntax check for Python writes (Windows).
#
# P2a (v0.2.75): replaces the INLINE powershell `python -m py_compile` entry
# at settings.json.windows.template:397, which (like its .sh twin) was
# unguarded, unscrubbed, not basename-supersedable, and printed a misleading
# fixed error string in its catch-all on ANY failure (including malformed
# stdin — which is not a syntax error). This named sibling self-guards +
# scrubs above and reports the TRUE compiler message below.
# MUST MATCH py-compile-check.sh.

# Resolve Python portably (py / python / python3).
$FindPy = Join-Path $PSScriptRoot "_lib/find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }  # No Python — nothing to check with.

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

# Parse file_path and distinguish malformed-stdin (NOT a syntax error →
# silent exit 0) from a real py_compile failure (surface the true message).
$fp = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input -and $payload.tool_input.file_path) {
        $fp = [string]$payload.tool_input.file_path
    }
} catch {
    # Malformed stdin — NOT a syntax error. Nothing to compile.
    exit 0
}
if (-not $fp) { exit 0 }

# Run py_compile via the resolved interpreter; capture stdout+stderr and the
# real exit code. A non-zero exit is a genuine compile error → surface the
# actual message (never a "Syntax error" lie for, e.g., a missing file).
$compileOut = & $PY -m py_compile $fp 2>&1
$compileExit = $LASTEXITCODE
if ($compileExit -ne 0) {
    $msg = ("$compileOut").Trim()
    if (-not $msg) { $msg = "py_compile failed (exit $compileExit)" }
    Write-Output "py_compile: $msg"
}
exit 0
