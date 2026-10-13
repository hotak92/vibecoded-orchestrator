# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/kg-sync (writes to the project's own
#   KG_COLLECTION / DEVELOPMENT_COLLECTION) and code-graph-incremental.ps1
#   (writes to the project's own code-graph collections via
#   analyze_code_graph.py). Writes do NOT consult VCT_KG_ACCESS_LIST or
#   VCT_CODE_GRAPH_ACCESS_LIST — those env vars are read-side only
#   (fan-out search across peer KGs). This hook is correct as-is; no
#   centralization needed: writes always target the project's OWN
#   collections; the access lists gate reads fanning out to peers only.

# post-file-edit.ps1 — PostToolUse hook
#
# Side-effects (background): KG / docs sync, code-graph incremental.
# LLM-visible reminders (additionalContext envelope):
#   - the routing home's report (the pending KG duplicate-scan report)
#     and the once-per-session broken-install notice
#
# Plain stdout from PostToolUse hooks is silently dropped per the
# v2.1.x contract — reminders intended for the model MUST go through
# `Emit-AdditionalContext` from `_lib/emit-context.ps1`.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# v0.2.54 Track G (G-6): child spawns used a hardcoded `pwsh`, which does
# not exist on PowerShell 5.1-only machines - KG sync, write-gate,
# dup-detection and code-graph updates were all silently lost there.
# $PsExe resolves pwsh -> powershell fallback.
. "$PSScriptRoot/_lib/resolve-powershell.ps1"
$EmitContextLib = Join-Path $PSScriptRoot "_lib/emit-context.ps1"
if (Test-Path $EmitContextLib) { . $EmitContextLib }

# v0.2.95 (lane F10): the ROUTING - knowledge/ -> kg-sync, docs/ ->
# upload_docs, code files -> the end-of-turn code-graph drain queue -
# plus the Phase-8 access gate and
# the per-file debounce now live in _lib/route-touched-path.ps1, because a
# SECOND hook needs exactly the same decision: post-bash-file-sync.ps1,
# which gives a CLI write (`cat > knowledge/foo.md <<EOF`, `sed -i`, `cp`)
# the same treatment an Edit/Write gets. This hook keeps the stdin parse,
# the telemetry exports and the two LLM-visible nudges that are specific to
# the Edit/Write surface. MUST MATCH post-file-edit.sh's same split.
#
# Conditional source, LOUDLY: a partial/old bundle install may lack the lib,
# and this hook must not ERROR on the user's Edit when it does -- but it must
# not go quiet either. Before v0.2.95 the lib-absent case cost only the
# debounce; now it costs ALL routing, so an install missing this one file
# syncs NOTHING and (review MAJOR-1) used to say NOTHING. The report is one
# line per session on stderr plus a line in this hook's additionalContext
# envelope -- see Emit-VcoMissingHookLibNotice in _lib/emit-context.ps1.
# Exit code is unchanged: always 0. MUST MATCH post-file-edit.sh.
$RouteLib = Join-Path $PSScriptRoot "_lib/route-touched-path.ps1"
if (Test-Path $RouteLib) { . $RouteLib }
# v0.2.102 W6-C1 (F16): the per-agent key helper, for the once-per-session
# broken-install notice below (its sentinel must not let a subagent's notice
# silence the main chat's). Conditional source, same discipline.
# MUST MATCH post-file-edit.sh.
$SessionIdLib = Join-Path $PSScriptRoot "_lib/session-id.ps1"
if (Test-Path $SessionIdLib) { . $SessionIdLib }

# Accumulate LLM-visible reminders, emit one envelope at the end.
$LlmNudge = ""
function Add-Nudge([string]$msg) {
    if ($script:LlmNudge) {
        $script:LlmNudge = "$script:LlmNudge`n`n$msg"
    } else {
        $script:LlmNudge = $msg
    }
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$EditedFile = ""
# V52-L.2 Fix 2b: parse subagent identity + session_id. We don't write
# a JSONL log directly, but the kg-sync / code-graph-incremental child
# processes DO emit retrieval/sync telemetry — exporting these as env
# vars (VCT_AGENT_ID / VCT_AGENT_TYPE / VCT_SESSION_ID) lets the
# canonical emit path attribute those rows to the agent that triggered
# the write.
$AgentId = ""
$AgentType = ""
$SessionIdFromStdin = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input -and $payload.tool_input.file_path) {
        $EditedFile = [string]$payload.tool_input.file_path
    }
    if ($payload) {
        if ($payload.agent_id)   { $AgentId   = [string]$payload.agent_id }
        if ($payload.agent_type) { $AgentType = [string]$payload.agent_type }
        if ($payload.session_id) { $SessionIdFromStdin = [string]$payload.session_id }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}
# Export for child processes (kg-sync, code-graph-incremental.ps1, etc.)
# so their emit paths can attribute telemetry to the originating agent.
# Skip empty exports — downstream readers treat unset and empty
# identically, but unset keeps the env listing clean.
if ($AgentId)            { $Env:VCT_AGENT_ID   = $AgentId }
if ($AgentType)          { $Env:VCT_AGENT_TYPE = $AgentType }
if ($SessionIdFromStdin) { $Env:VCT_SESSION_ID = $SessionIdFromStdin }

$ScriptDir = $PSScriptRoot
# D-16 (v0.2.73): prefer CLAUDE_PROJECT_DIR (matches the .sh sibling) so
# worktree-isolated / out-of-tree sessions resolve state paths against the
# same root; fall back to the script-relative root otherwise.
if ($Env:CLAUDE_PROJECT_DIR) {
    $ProjectRoot = $Env:CLAUDE_PROJECT_DIR
} else {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}
$KnowledgeRoot = Join-Path $ProjectRoot "knowledge"
$DocsDir = Join-Path $ProjectRoot "docs"
# D-9 (v0.2.73): match on the directory WITH a trailing separator so
# sibling dirs (knowledge_base/, docs-archive/) don't sync into the KG /
# development collections. StartsWith on the bare root matched them.
$KnowledgeRootSep = $KnowledgeRoot.TrimEnd('\','/') + [System.IO.Path]::DirectorySeparatorChar
$DocsDirSep = $DocsDir.TrimEnd('\','/') + [System.IO.Path]::DirectorySeparatorChar

if (-not $EditedFile) { exit 0 }

# === Routing: knowledge/ + docs/ + the code-graph drain queue ===
# ONE home - _lib/route-touched-path.ps1 - shared with
# post-bash-file-sync.ps1 so a CLI write routes identically to an
# Edit/Write. Initialize-VcoRoute resolves the debounce helper, the
# code-extension helper, VCT_PROJECT_ID and the access-matrix checker;
# Invoke-VcoRouteTouchedPath performs the routing and leaves any
# LLM-visible text in $script:VcoRouteNudge.
if (Get-Command Invoke-VcoRouteTouchedPath -ErrorAction SilentlyContinue) {
    Initialize-VcoRoute -HooksDir $ScriptDir -ProjectRoot $ProjectRoot
    Invoke-VcoRouteTouchedPath -Path $EditedFile -SessionId $SessionIdFromStdin
    if ($script:VcoRouteNudge) { Add-Nudge $script:VcoRouteNudge }
} elseif (Get-Command Emit-VcoMissingHookLibNotice -ErrorAction SilentlyContinue) {
    # The routing home is absent (or unreadable) -- a broken install, not a
    # degraded mode. Say so once per session, then carry on with the nudges
    # this hook owns; exit stays 0. v0.2.102 W6-C1 (F16): the notice is
    # delivered as PostToolUse context, which reaches the SUBAGENT only
    # when this edit is a lane's -- key the sentinel per agent so the
    # lane's notice never silences the main chat's.
    # MUST MATCH post-file-edit.sh.
    $noticeKey = $SessionIdFromStdin
    if ($AgentId -and (Get-Command Get-VcoAgentKeyFromIds -ErrorAction SilentlyContinue)) {
        $noticeKey = Get-VcoAgentKeyFromIds -SessionId $SessionIdFromStdin -AgentId $AgentId
    }
    $missingNotice = Emit-VcoMissingHookLibNotice -HooksDir $ScriptDir `
        -ProjectRoot $ProjectRoot -SessionId $noticeKey `
        -Lib "route-touched-path.ps1"
    if ($missingNotice) { Add-Nudge $missingNotice }
}

# The after-edit workflow nudges are gone (owner-approved removal
# 2026-10-12, v0.2.102 W6-N review round): the CONTEXT_STATE.md
# "expert-skill review" pointer named a skill file VCO never ships, and the
# "[Workflow file edited]" reminder pointed at a skills-setup-guide.md that
# exists nowhere. Prompt-file advice comes BEFORE the edit, from the
# pre-edit / pre-write context router (vco_lib/prompt_edit_nudge.py).

# Emit accumulated nudges as a single PostToolUse envelope.
if ($LlmNudge -and (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue)) {
    Emit-AdditionalContext $LlmNudge PostToolUse
}
exit 0
