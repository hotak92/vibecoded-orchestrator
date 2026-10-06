# Pre-bash context injection hook -- THIN WRAPPER (v0.2.101 injection
# redesign, PLAN-V02101 section C1). OS-PARITY: ports the .sh sibling.
# Fires BEFORE the Bash tool executes.
#
#   stdin -> hook_context_router.py bash --intent-out <state> -> emit envelope
#
# The ROUTER owns every retrieval decision: intent classification
# (READ/EDIT/SEARCH/MECHANICAL -- MECHANICAL spawns no producer and injects
# nothing), query building from target paths/symbols (NEVER command text),
# the section 2.1 noise gates, seen-store dedupe, per-turn budget, query
# cache and the RL retrieval events (rl_kg_search --injection-profile
# --task-type).
#
# What this wrapper still owns (WP-D 2): the bash_task_<session>_<cmdhash>.json
# state file + the pre_bash outcome event -- now for every
# READ/EDIT/SEARCH-classified command (UPSTREAM of the old 500-char gate),
# with intent/targets/symbols added to both payloads (additive keys;
# post-bash-context-record pairing UNCHANGED) -- and the Emit-AdditionalContext
# envelope around the router's text.
#
# RETIRED here (v0.2.101, owner-approved section C1): the 500-char threshold
# (VCT_BASH_KG_THRESHOLD_CHARS -- classification replaces it; the knob no
# longer exists), the noise-strip query build, the Test-VcoCodegraphBashGate
# branch (the whole _lib/codegraph-query lib was retired with this, its last
# caller), and the wrapper-side KG search/dedup (router-owned).
#
# Kill switches: VCT_DISABLE_HOOKS and VCO_INJECT_PROFILE=off (via
# _lib/inject-budget.ps1), both checked BEFORE any spawn. Missing venv /
# router -> silent no-op. Never exit non-zero.
# MUST MATCH pre-bash-context-inject.sh.

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
# v0.2.101: the injection kill switch (VCO_INJECT_PROFILE=off).
$InjectBudgetLib = Join-Path $LibDir "inject-budget.ps1"
if (Test-Path $InjectBudgetLib) { . $InjectBudgetLib }
if ((Get-Command Test-VcoInjectProfileOff -ErrorAction SilentlyContinue) -and (Test-VcoInjectProfileOff)) {
    exit 0
}

# Hook input arrives as JSON on stdin. The FULL payload is forwarded to the
# router untouched; this parse extracts only the wrapper's fields (tool
# guard, session id, command for the pairing hash). MUST MATCH the .sh parse.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
$SessionId = ""
$Command = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)  { $ToolName = [string]$payload.tool_name }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.tool_input -and $payload.tool_input.command) {
            $Command = [string]$payload.tool_input.command
        }
    }
} catch { }

if ($ToolName -ne "Bash") { exit 0 }
if (-not $Command) { exit 0 }

if (Get-Command Get-VcoHookSessionId -ErrorAction SilentlyContinue) {
    $SessionId = Get-VcoHookSessionId -Stdin $HookStdin
}
if (-not $SessionId) { $SessionId = "default" }
if ($SessionId -and $SessionId -ne "default") {
    $env:VCT_SESSION_ID = $SessionId
}

# === Deterministic cmd hash for state-file pairing (UNCHANGED contract) ===
# .NET MD5 -> first 16 hex chars -- same value the .sh sibling's hashlib.md5
# produces (test_v52_m_prepost_hooks pins both spellings).
$md5 = [System.Security.Cryptography.MD5]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($Command)
$CmdHash = (($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 16)
if (-not $CmdHash) {
    $CmdHash = (($Command -replace '[^A-Za-z0-9_]', '_'))
    if ($CmdHash.Length -gt 32) { $CmdHash = $CmdHash.Substring(0, 32) }
}
$CmdLen = $Command.Length

$StateDir = Join-Path $ProjectRoot ".claude/state"
if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
}
# The router's classification handoff (WP-D 2). Named like the state file so
# the 1d GC covers it. MUST MATCH the .sh sibling.
$IntentFile = Join-Path $StateDir "bash_intent_${SessionId}_${CmdHash}.json"

# === Resolve venv + the router (orchestrator-root script, F3 discipline) ===
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
if (-not $VenvPy -or -not (Test-Path $VenvPy)) { exit 0 }
$Router = Resolve-VcoOrchestratorScript -ScriptDir $ScriptDir -RelPath "claude_mcp_servers/scripts/hook_context_router.py"
if (-not $Router) { $Router = Join-Path $ProjectRoot "claude_mcp_servers/scripts/hook_context_router.py" }
if (-not (Test-Path $Router)) { exit 0 }
# Pin the CALLING project's identity for the router + producers.
$env:CLAUDE_PROJECT_DIR = $ProjectRoot

# === Run the router (single interpreter; inner budget VCO_INJECT_BUDGET_S) ===
$Inject = ""
try {
    $Inject = ($HookStdin | & $VenvPy $Router "bash" "--intent-out" $IntentFile 2>$null) -join "`n"
} catch { $Inject = "" }
if ($null -eq $Inject) { $Inject = "" }

# === Read the classification back (WP-D 2 gate for state + outcome) ===
# Manual JSON string-array building (not ConvertTo-Json -AsArray, which is
# PS7+ only -- the resolve-powershell ladder still supports 5.1 machines).
function ConvertTo-VcoJsonStringArray($items) {
    if (-not $items) { return "[]" }
    $parts = @()
    foreach ($it in @($items)) {
        $s = [string]$it
        $s = $s.Replace('\', '\\').Replace('"', '\"')
        $parts += ('"' + $s + '"')
    }
    return '[' + ($parts -join ',') + ']'
}
$Intent = ""
$TargetsJson = "[]"
$SymbolsJson = "[]"
if (Test-Path -LiteralPath $IntentFile) {
    try {
        $intentObj = Get-Content -LiteralPath $IntentFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($intentObj.intent)  { $Intent = [string]$intentObj.intent }
        if ($intentObj.targets) { $TargetsJson = ConvertTo-VcoJsonStringArray $intentObj.targets }
        if ($intentObj.symbols) { $SymbolsJson = ConvertTo-VcoJsonStringArray $intentObj.symbols }
    } catch { }
    Remove-Item -LiteralPath $IntentFile -Force -ErrorAction SilentlyContinue
}

# v0.2.29 GC (deliberate 1d threshold, unchanged): pairing state + intents.
Get-ChildItem -File $StateDir -Filter "bash_task_*.json" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -File $StateDir -Filter "bash_intent_*.json" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# === State file + pre_bash outcome event: READ/EDIT/SEARCH only ===
if ($Intent -eq "READ" -or $Intent -eq "EDIT" -or $Intent -eq "SEARCH") {
    $StateFile = Join-Path $StateDir "bash_task_${SessionId}_${CmdHash}.json"
    # Wave-2 review nit-5: the section-C1 if-group fires ONE handler per
    # matching rule, so a multi-match command spawns this hook TWICE for ONE
    # tool call. The injection side is idempotent (router seen-store + cache);
    # the pairing side must be too -- a FRESH (<60 s) unpaired state file
    # means a sibling spawn already paired this call: skip the rewrite and
    # the second pre_bash event (the old shape emitted an orphan with a
    # distinct task_id). MUST MATCH the .sh sibling's find -mmin -1 guard.
    $stateFresh = $false
    if (Test-Path -LiteralPath $StateFile) {
        try { $stateFresh = ((Get-Item -LiteralPath $StateFile).LastWriteTime -gt (Get-Date).AddSeconds(-60)) } catch { }
    }
    if (-not $stateFresh) {
    $TaskHex = ([guid]::NewGuid().ToString("N")).Substring(0, 8)
    $TaskId = "pre_bash_$TaskHex"
    $StartTsMs = [long][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

    $state = @{
        task_id = $TaskId
        start_ts_ms = $StartTsMs
        session_id = $SessionId
        cmd_hash = $CmdHash
        cmd_len = $CmdLen
        intent = $Intent
        targets = @()
        symbols = @()
    }
    try { $state.targets = @($TargetsJson | ConvertFrom-Json) } catch { }
    try { $state.symbols = @($SymbolsJson | ConvertFrom-Json) } catch { }
    try {
        $state | ConvertTo-Json -Compress | Set-Content -Path $StateFile -Encoding UTF8 -NoNewline
    } catch { }

    # === F-LOG (v0.2.70): the pre_bash pairing event (WP-D 2 payload) ===
    $QuerySnippet = if ($Command.Length -gt 120) { $Command.Substring(0, 120) } else { $Command }
    $env:VCT_PREBASH_QUERY = $QuerySnippet
    $env:VCT_PREBASH_TASK_ID = $TaskId
    $env:VCT_PREBASH_CMD_LEN = [string]$CmdLen
    $env:VCT_PREBASH_TS_MS = [string]$StartTsMs
    $env:VCT_PREBASH_SESSION = $SessionId
    $env:VCT_PREBASH_INTENT = $Intent
    $env:VCT_PREBASH_TARGETS = $TargetsJson
    $env:VCT_PREBASH_SYMBOLS = $SymbolsJson
    $env:VCT_PROJECT_ROOT = $ProjectRoot
    $preBashPy = @"
import os
_vco_project_root = r'''$ProjectRoot'''
if _vco_project_root:
    os.environ.setdefault('KG_BASE_DIR', _vco_project_root)
try:
    from vco_lib.project_config import resolve_for_project
    cfg = resolve_for_project(os.environ.get('CLAUDE_PROJECT_DIR', os.environ.get('VCT_PROJECT_ROOT', '')))
    project_id = cfg.get('project_id') if isinstance(cfg, dict) else None
except Exception:
    project_id = None
def _int(name):
    try:
        return int(os.environ.get(name, '0') or '0')
    except (TypeError, ValueError):
        return 0
def _list(name):
    import json as _json
    try:
        v = _json.loads(os.environ.get(name, '[]') or '[]')
        return v if isinstance(v, list) else []
    except Exception:
        return []
try:
    from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event
    emit_outcome_event(
        event_type='pre_bash',
        task_id=os.environ.get('VCT_PREBASH_TASK_ID', ''),
        task_type='pre_bash',
        payload={
            'cmd_len': _int('VCT_PREBASH_CMD_LEN'),
            'query': os.environ.get('VCT_PREBASH_QUERY', ''),
            'ts_ms': _int('VCT_PREBASH_TS_MS'),
            'intent': os.environ.get('VCT_PREBASH_INTENT', ''),
            'targets': _list('VCT_PREBASH_TARGETS'),
            'symbols': _list('VCT_PREBASH_SYMBOLS'),
        },
        session_id=os.environ.get('VCT_PREBASH_SESSION', ''),
        project_id=project_id,
    )
except Exception:
    pass
"@
    try { & $VenvPy -c $preBashPy *> $null } catch { }
    }
}

# === Emit the router's injection text (if any) ===
$trimmed = ($Inject -replace '\s+', '')
if ($trimmed) {
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        $firstLine = $Command -split "`n" | Select-Object -First 1
        if ($firstLine.Length -gt 80) { $firstLine = $firstLine.Substring(0, 80) }
        $out = [System.Text.StringBuilder]::new()
        [void]$out.AppendLine("[Pre-bash context for: ${firstLine}]:")
        [void]$out.AppendLine("")
        [void]$out.Append($Inject)
        [void]$out.AppendLine("")
        Emit-AdditionalContext $out.ToString() 'PreToolUse'
    }
}

exit 0
