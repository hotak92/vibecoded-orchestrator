# Pre-write context injection hook (v0.2.101 injection redesign,
# PLAN-V02101 section C3) -- THIN WRAPPER. OS-PARITY: ports the .sh sibling.
# Fires BEFORE the Write tool executes.
#
#   stdin -> hook_context_router.py write -> emit envelope
#
# The ROUTER owns everything retrieval-side: KG titles keyed on module name
# + sibling dir (the path topic -- never the file content), the write-profile
# section 2.1 floor (0.65, titles-only below 0.85), an exact code-graph
# def+callers leg ONLY when the path is a REWRITE of an existing file,
# seen-store dedupe, the per-turn budget, the query cache and the RL
# retrieval event (pre_write_kg_search).
#
# No per-file replay cache here (unlike pre-edit): a Write is a whole-file
# event and the router's own query cache already serves repeats at ~ms.
#
# Never exit non-zero; missing venv/router -> silent no-op; kill switches
# VCT_DISABLE_HOOKS and VCO_INJECT_PROFILE=off checked BEFORE any spawn.
# MUST MATCH pre-write-context-inject.sh.

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: read-side delegator (PR #171 / 0.1.7). v0.2.101: the
#   delegate is claude_mcp_servers/scripts/hook_context_router.py (KG +
#   code-graph legs loaded in-process, both access-aware via the weaviate_mcp
#   server helpers reading VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST).
#   This hook does NOT query Weaviate directly; env propagates by subprocess
#   inheritance. See tests/test_kg_access_list.py for the consumer contract.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
if (Test-Path "$PSScriptRoot/_lib/emit-context.ps1") {
    . "$PSScriptRoot/_lib/emit-context.ps1"
}

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }

$SessionIdLib = Join-Path $LibDir "session-id.ps1"
if (Test-Path $SessionIdLib) { . $SessionIdLib }
$InjectBudgetLib = Join-Path $LibDir "inject-budget.ps1"
if (Test-Path $InjectBudgetLib) { . $InjectBudgetLib }
if ((Get-Command Test-VcoInjectProfileOff -ErrorAction SilentlyContinue) -and (Test-VcoInjectProfileOff)) {
    exit 0
}

# Hook input arrives as JSON on stdin. The FULL payload is forwarded to the
# router untouched (the Write content never travels through argv -- R31).
# MUST MATCH the .sh sibling's parse.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
$SessionId = ""
$FilePath = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)  { $ToolName = [string]$payload.tool_name }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.tool_input -and $payload.tool_input.file_path) {
            $FilePath = [string]$payload.tool_input.file_path
        }
    }
} catch { }

if ($ToolName -ne "Write") { exit 0 }
if (-not $FilePath) { exit 0 }

if (Get-Command Get-VcoHookSessionId -ErrorAction SilentlyContinue) {
    $SessionId = Get-VcoHookSessionId -Stdin $HookStdin
}
if (-not $SessionId) { $SessionId = "default" }
if ($SessionId -and $SessionId -ne "default") {
    $env:VCT_SESSION_ID = $SessionId
}

# === Resolve venv + the router (orchestrator-root script, F3 discipline) ===
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
if (-not $VenvPy -or -not (Test-Path $VenvPy)) { exit 0 }
$Router = Resolve-VcoOrchestratorScript -ScriptDir $ScriptDir -RelPath "claude_mcp_servers/scripts/hook_context_router.py"
if (-not $Router) { $Router = Join-Path $ProjectRoot "claude_mcp_servers/scripts/hook_context_router.py" }
if (-not (Test-Path $Router)) { exit 0 }
$env:CLAUDE_PROJECT_DIR = $ProjectRoot

# === Run the router ===
$Inject = ""
try {
    $Inject = ($HookStdin | & $VenvPy $Router "write" 2>$null) -join "`n"
} catch { $Inject = "" }
if ($null -eq $Inject) { $Inject = "" }

# === Only output if we found something ===
if (($Inject -replace '\s+', '')) {
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        $Basename = Split-Path $FilePath -Leaf
        Emit-AdditionalContext "[Pre-write context for ${Basename}]:`n`n$Inject" 'PreToolUse'
    }
}

exit 0
