# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# py-compile-check.ps1 -- PostToolUse syntax check for Python writes (Windows).
#
# P2a (v0.2.75): replaces the INLINE powershell `python -m py_compile` entry
# at settings.json.windows.template:397, which (like its .sh twin) was
# unguarded, unscrubbed, not basename-supersedable, and printed a misleading
# fixed error string in its catch-all on ANY failure (including malformed
# stdin -- which is not a syntax error). This named sibling self-guards +
# scrubs above and reports the TRUE compiler message below.
# MUST MATCH py-compile-check.sh (which carries the POSIX executable bit;
# this .ps1 needs none -- pwsh runs it via -File regardless of mode).

# Resolve Python portably (py / python / python3).
$FindPy = Join-Path $PSScriptRoot "_lib/find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }  # No Python -- nothing to check with.

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

# Parse file_path and distinguish malformed-stdin (NOT a syntax error ->
# silent exit 0) from a real py_compile failure (surface the true message).
$fp = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input -and $payload.tool_input.file_path) {
        $fp = [string]$payload.tool_input.file_path
    }
} catch {
    # Malformed stdin -- NOT a syntax error. Nothing to compile.
    exit 0
}
if (-not $fp) { exit 0 }
# v0.2.80: the settings matcher is bare `Write` (the old `if: "Write(*.py)"`
# never matched -- hook-if regression fix), so the .py gate lives HERE.
# Without it every non-Python Write produced a false "SyntaxError" and a
# Python-parsable non-.py file dropped __pycache__ next to user files
# (final-review B1). MUST MATCH py-compile-check.sh.
if (-not $fp.EndsWith('.py')) { exit 0 }

# v0.2.102 W6-PCHK: compile AND record the compile STATUS in ONE Python
# child, exactly like the .sh sibling. The status (file path, checker, ok,
# exit code, timestamp; never file contents) is what post-edit-outcome's
# detached poller turns into the edit_outcome row's post_check. The writer
# is vco_lib.edit_post_check.write_status (the ONE home: atomic write,
# path-hash + per-agent key), imported from the orchestrator root resolved
# by the SAME ladder every hook uses. Unresolved root or a failed write ->
# one stderr line; the check result is still reported. The code goes in on
# stdin and the values as argv (never interpolated into the source, never
# an empty argv item, which Windows PowerShell 5.1 drops).
# MUST MATCH py-compile-check.sh.
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$PccVcoRoot = ""
$ResolveLib = Join-Path $PSScriptRoot "_lib/resolve-vco-venv.ps1"
if (Test-Path $ResolveLib) {
    . $ResolveLib
    $pccScript = Resolve-VcoOrchestratorScript -ScriptDir $PSScriptRoot -RelPath "vco_lib/edit_post_check.py"
    if ($pccScript) { $PccVcoRoot = Split-Path (Split-Path $pccScript -Parent) -Parent }
}
$PccSession = if ($payload.session_id) { [string]$payload.session_id } else { "default" }
$PccAgent = if ($payload.agent_id) { [string]$payload.agent_id } else { "" }
$PccTool = if ($payload.tool_name) { [string]$payload.tool_name } else { "Write" }

$pyCode = @'
import sys, py_compile

def record_status(fp, exit_code):
    kv = dict(a.split('=', 1) for a in sys.argv[2:] if '=' in a)
    vco_root = kv.get('vco_root', '')
    if not vco_root:
        sys.stderr.write('py-compile-check: compile status not recorded: VCO root not resolved\n')
        return
    if vco_root not in sys.path:
        sys.path.insert(0, vco_root)
    try:
        from vco_lib.edit_post_check import write_status
        write_status(project_root=kv.get('project', ''), file_path=fp, checker='py_compile',
                     tool_name=kv.get('tool', '') or 'Write', exit_code=exit_code,
                     session_id=kv.get('session', '') or 'default',
                     agent_id=kv.get('agent', ''))
    except Exception as exc:
        sys.stderr.write('py-compile-check: compile status not recorded: %r\n' % (exc,))

fp = sys.argv[1]
try:
    py_compile.compile(fp, doraise=True)
    sys.stdout.write('OK')
    record_status(fp, 0)
except py_compile.PyCompileError as e:
    sys.stdout.write('ERR:' + str(e).strip().replace('\n', ' '))
    record_status(fp, 1)
except FileNotFoundError:
    sys.exit(0)
except Exception as e:
    sys.stdout.write('ERR:' + str(e).strip().replace('\n', ' '))
    record_status(fp, 1)
'@
$pyArgs = @(
    $fp, "vco_root=$PccVcoRoot", "project=$ProjectRoot", "tool=$PccTool",
    "session=$PccSession", "agent=$PccAgent"
)
$compileOut = ""
try { $compileOut = ($pyCode | & $PY - @pyArgs) -join " " } catch { }
if ($compileOut -like 'ERR:*') {
    $msg = $compileOut.Substring(4).Trim()
    if (-not $msg) { $msg = "py_compile failed" }
    Write-Output "py_compile: $msg"
}
exit 0
