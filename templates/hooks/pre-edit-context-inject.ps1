# Pre-edit context injection hook -- THIN WRAPPER (v0.2.101 injection
# redesign, PLAN-V02101 section C3). OS-PARITY: ports the .sh sibling.
# Fires BEFORE the Edit tool executes.
#
#   stdin -> hook_context_router.py edit -> emit envelope
#
# The ROUTER owns the query and its discipline: the OLD semantic query
# "<module-basename> <first 200 chars of new_string>" is REPLACED by
# edit_enclosing_symbols(file_path, old_string) -> an EXACT code-graph
# def+callers leg (structure, self-file callers excluded) + a KG leg keyed
# on module+symbol+path topic, with the section 2.1 edit-profile floors
# applied inside rl_kg_search --injection-profile, plus seen-store dedupe,
# the per-turn budget and the RL retrieval event (task_type
# pre_edit_kg_search via the router's --task-type; the wrapper's
# VCO_RL_TASK_TYPE export is retired with the direct producer call).
#
# What this wrapper still owns: the PER-FILE REPLAY CACHE (router stdout
# cached per edited file, TTL = VCO_QUERY_CACHE_TTL window, replayed through
# CURRENT seen-state with NO router spawn), the state GC sweeps and the
# Emit-AdditionalContext envelope.
#
# Never exit non-zero; missing venv/router -> silent no-op; kill switches
# VCT_DISABLE_HOOKS and VCO_INJECT_PROFILE=off checked BEFORE any spawn.
# MUST MATCH pre-edit-context-inject.sh.

# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: read-side delegator (PR #171 / 0.1.7). v0.2.101: the
#   delegate is claude_mcp_servers/scripts/hook_context_router.py, which loads
#   rl_kg_search.py / query_code_graph IN-PROCESS -- both access-aware via the
#   weaviate_mcp server helpers (VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST).
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
# The replay path filters through the SHARED seen-store (one home); the
# router reads/writes the SAME files with the SAME key format.
$SeenStoreLib = Join-Path $LibDir "seen-store.ps1"
if (Test-Path $SeenStoreLib) { . $SeenStoreLib }
# v0.2.101: the injection kill switch (VCO_INJECT_PROFILE=off).
$InjectBudgetLib = Join-Path $LibDir "inject-budget.ps1"
if (Test-Path $InjectBudgetLib) { . $InjectBudgetLib }
if ((Get-Command Test-VcoInjectProfileOff -ErrorAction SilentlyContinue) -and (Test-VcoInjectProfileOff)) {
    exit 0
}

# Hook input arrives as JSON on stdin. The FULL payload is forwarded to the
# router untouched; this parse extracts only the WRAPPER's fields (tool
# guard, session id, edited file path). MUST MATCH the .sh sibling's parse.
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

if ($ToolName -ne "Edit") { exit 0 }
if (-not $FilePath) { exit 0 }

if (Get-Command Get-VcoHookSessionId -ErrorAction SilentlyContinue) {
    $SessionId = Get-VcoHookSessionId -Stdin $HookStdin
}
$SessionIdRaw = $SessionId
if (-not $SessionId) { $SessionId = "default" }
if ($SessionId -and $SessionId -ne "default") {
    $env:VCT_SESSION_ID = $SessionId
}

# === Per-file replay cache (v0.2.101: stores the ROUTER's raw output) ===
$StateDir = Join-Path $ProjectRoot ".claude/state"
if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
}
$CacheBase = Join-Path $StateDir "edit_cache_$SessionId"
if (-not (Test-Path $CacheBase)) {
    New-Item -ItemType Directory -Path $CacheBase -Force -ErrorAction SilentlyContinue | Out-Null
}
# v0.2.29 GC: prune per-session edit_cache_* directories older than 14 days.
# HK-4 (v0.2.75) accepted-scatter: GC is intentionally per-hook, not a shared
# sweeper. MUST MATCH the .sh sibling's note.
Get-ChildItem -Directory $StateDir -Filter "edit_cache_*" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
# P3 (v0.2.91): TTL aligned with the shared query cache window.
$CacheTtl = 900
if ($env:VCO_QUERY_CACHE_TTL) {
    try { $parsedTtl = [int]$env:VCO_QUERY_CACHE_TTL; if ($parsedTtl -gt 0) { $CacheTtl = $parsedTtl } } catch { }
}

# Seen-store GC sweeps (14d) + replay-filter paths.
Get-ChildItem -File $StateDir -Filter "seen_inject_*.txt" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -File $StateDir -Filter "seen_kg_titles_*.txt" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
$SeenInjectFile = ""
$SeenReadsFile = ""
if (Get-Command Get-VcoSeenStorePath -ErrorAction SilentlyContinue) {
    $SeenInjectFile = Get-VcoSeenStorePath -Kind "inject" -SessionId $SessionIdRaw -ProjectRoot $ProjectRoot
    $SeenReadsFile = Get-VcoSeenStorePath -Kind "reads" -SessionId $SessionIdRaw -ProjectRoot $ProjectRoot
}

# === Cache key from the file path (.NET MD5; the .sh sibling uses hashlib) ===
$md5 = [System.Security.Cryptography.MD5]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($FilePath)
$FileHash = (($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
$CacheFile = Join-Path $CacheBase $FileHash
$Basename = Split-Path $FilePath -Leaf

function Emit-ContextJson([string]$ctx) {
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        Emit-AdditionalContext $ctx 'PreToolUse'
    }
}

# === Cache hit/miss observability (v0.2.77 Part 9 task 1) ===
function Write-CacheLog([string]$Status) {
    $log = Join-Path $StateDir "preedit_cache_log.jsonl"
    try {
        if (Test-Path -LiteralPath $log) {
            $sz = (Get-Item -LiteralPath $log).Length
            if ($sz -gt 262144) { Move-Item -LiteralPath $log "$log.1" -Force -ErrorAction SilentlyContinue }
        }
        $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Add-Content -LiteralPath $log -Value ('{"ts":"' + $ts + '","hook":"pre-edit","status":"' + $Status + '","session":"' + $SessionId + '"}') -ErrorAction SilentlyContinue
    } catch { }
}

# === Cache replay (BEFORE any router spawn) ===
$CacheHit = $false
$CacheBlob = ""
if (Test-Path -LiteralPath $CacheFile) {
    try {
        $age = ((Get-Date) - (Get-Item -LiteralPath $CacheFile).LastWriteTime).TotalSeconds
        if ($age -lt $CacheTtl) {
            $CacheHit = $true
            $CacheBlob = Get-Content -LiteralPath $CacheFile -Raw -ErrorAction SilentlyContinue
            if ($null -eq $CacheBlob) { $CacheBlob = "" }
        }
    } catch { }
}

if ($CacheHit) {
    Write-CacheLog "hit"
    # Replay through the CURRENT seen-state: the router recorded the blocks
    # it emitted on the miss run, so a same-session replay filters to
    # silence -- the same answer a fresh router run gives, with no spawn.
    # After a /compact seen-store wipe the blocks re-eligibilise (the cache
    # stores RAW pre-dedup output). A missing seen-store helper SKIPS the
    # replay and falls through to a live router run rather than replaying
    # undeduped. MUST MATCH the .sh sibling.
    if (Get-Command Invoke-VcoFilterSeenBlocks -ErrorAction SilentlyContinue) {
        $filtered = Invoke-VcoFilterSeenBlocks -InputText $CacheBlob -InjectFile $SeenInjectFile -ReadsFile $SeenReadsFile
        if (($filtered -replace '\s+', '')) {
            Emit-ContextJson "[Pre-edit context for ${Basename}]:`n`n$filtered"
        }
        exit 0
    }
}
Write-CacheLog "miss"

# === Resolve venv + the router (orchestrator-root script, F3 discipline) ===
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
if (-not $VenvPy -or -not (Test-Path $VenvPy)) { exit 0 }
$Router = Resolve-VcoOrchestratorScript -ScriptDir $ScriptDir -RelPath "claude_mcp_servers/scripts/hook_context_router.py"
if (-not $Router) { $Router = Join-Path $ProjectRoot "claude_mcp_servers/scripts/hook_context_router.py" }
if (-not (Test-Path $Router)) { exit 0 }
# Pin the CALLING project's identity for the router + producers.
$env:CLAUDE_PROJECT_DIR = $ProjectRoot

# === Run the router (single interpreter for both legs; inner budget
# VCO_INJECT_BUDGET_S). The router applies gates + dedupe + budget. ===
$Inject = ""
try {
    $Inject = ($HookStdin | & $VenvPy $Router "edit" 2>$null) -join "`n"
} catch { $Inject = "" }
if ($null -eq $Inject) { $Inject = "" }

# === Only output if we found something ===
if (($Inject -replace '\s+', '')) {
    # Cache the RAW router output (section-9 discipline: never cache empty).
    try { Set-Content -LiteralPath $CacheFile -Value $Inject -Encoding UTF8 } catch { }
    Emit-ContextJson "[Pre-edit context for ${Basename}]:`n`n$Inject"
}

exit 0
