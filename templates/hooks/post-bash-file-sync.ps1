# post-bash-file-sync.ps1 -- PostToolUse(Bash) hook (v0.2.95, lane F10)
# PowerShell sibling of post-bash-file-sync.sh. MUST MATCH that file's
# prefilter vocabulary, parser invocation and routing loop.
#
# A file written from the CLI must sync exactly like a file written with
# the Edit/Write tools. Before this hook it did not: post-file-edit.ps1 is
# registered on matcher `Edit|Write` ONLY, so
#
#     cat > knowledge/foo.md <<EOF ... EOF
#     sed -i 's/x/y/' docs/architecture.md
#     cp scratch/module.py vco_lib/module.py
#
# reached Weaviate NEVER.
#
# ONE HOME, NOT A SECOND COPY: the routing itself lives in
# _lib/route-touched-path.ps1 and is called by BOTH this hook and
# post-file-edit.ps1. What this hook adds is turning a command STRING into
# the list of paths it wrote -- vco_lib/bash_write_targets.py, the same
# parser the .sh sibling uses, so the two operating systems cannot disagree
# about what a command wrote.
#
# KNOWN MISSES (identical to the .sh sibling -- read its header for the
# reasoning): interpreter-internal writes (`python - <<EOF ... open(p,'w')`)
# are recovered for knowledge/ and docs/ by a bounded watermarked mtime
# scan and are a REAL MISS for code files; a relative path written after an
# unresolvable `cd` is dropped rather than guessed.
#
# That scan runs only when the parser found nothing AND the command text
# shows a write AND it could have landed in the two scanned directories --
# all three, per the .sh header's (a)/(b)/(c). A read-only command that
# merely NAMES knowledge/ or docs/ (`cat docs/x.md`, `grep -rn foo
# knowledge/`) reaches neither Python nor the scan (v0.2.95 review
# MAJOR-3); before that it reached both.
#
# Always exits 0 and never writes to plain stdout.

# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

. "$PSScriptRoot/_lib/stderr-cap.ps1"
. "$PSScriptRoot/_lib/resolve-powershell.ps1"
$EmitContextLib = Join-Path $PSScriptRoot "_lib/emit-context.ps1"
if (Test-Path $EmitContextLib) { . $EmitContextLib }
$RouteLib = Join-Path $PSScriptRoot "_lib/route-touched-path.ps1"
if (Test-Path $RouteLib) { . $RouteLib }

$ScriptDir = $PSScriptRoot
if ($Env:CLAUDE_PROJECT_DIR) {
    $ProjectRoot = $Env:CLAUDE_PROJECT_DIR
} else {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

# The prefilter + the parser invocation live in _lib/bash-write-targets.ps1,
# shared with pre-bash-context-inject.ps1 (which needs the SAME parse to
# build its retrieval query from the target path). Conditional dot-source, so
# a partial install cannot make the user's Bash call ERROR -- but its ABSENCE
# is a broken install and is reported below, after the payload is decoded,
# not skipped in silence (v0.2.95 ship-gate review MAJOR-2).
$WriteTargetsLib = Join-Path $PSScriptRoot "_lib/bash-write-targets.ps1"
if (Test-Path $WriteTargetsLib) { . $WriteTargetsLib }
# v0.2.102 W6-C1 (F16): the per-agent key helper for the once-per-session
# notices below. Conditional source, same discipline as the libs above.
# MUST MATCH post-bash-file-sync.sh.
$SessionIdLib = Join-Path $PSScriptRoot "_lib/session-id.ps1"
if (Test-Path $SessionIdLib) { . $SessionIdLib }

# --- Hook input -----------------------------------------------------------
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

$ToolName = ""
$Command = ""
$SessionId = ""
# v0.2.102 W6-C1 (F16): agent_id joined the parse so the once-per-session
# notices below key per agent and the telemetry export can attribute a
# lane's CLI writes to the lane (same shape as post-file-edit.ps1).
$AgentId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name) { $ToolName = [string]$payload.tool_name }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.agent_id) { $AgentId = [string]$payload.agent_id }
        if ($payload.tool_input -and $payload.tool_input.command) {
            $Command = [string]$payload.tool_input.command
        }
    }
} catch {
    # Empty/malformed stdin -- keep variables at defaults.
}

# v0.2.102 W6-C1 (F16): the notices reach the SUBAGENT only when this write
# is a lane's -- key their sentinels per agent (the ONE session-id helper)
# so the lane's notice never silences the main chat's. MUST MATCH the .sh
# sibling (both notice sites use the same key).
$NoticeKey = $SessionId
if ($AgentId -and (Get-Command Get-VcoAgentKeyFromIds -ErrorAction SilentlyContinue)) {
    $NoticeKey = Get-VcoAgentKeyFromIds -SessionId $SessionId -AgentId $AgentId
}

# Bash, or the PowerShell tool (v0.2.102 W5-D -- the only shell tool on
# Windows without Git Bash; the parser reads its syntax via -Tool).
if ($ToolName -ne "Bash" -and $ToolName -ne "PowerShell") { exit 0 }
if (-not $Command) { exit 0 }

# The parser lib is missing -> this hook can recover NO write target from any
# command, so every CLI write stops syncing while everything else looks
# healthy. Report once per session (stderr + one envelope) and exit 0. After
# the decode, not at the source: the notice is keyed on the session id, which
# only the payload carries.
if (-not (Get-Command Test-VcoWriteSuspicious -ErrorAction SilentlyContinue)) {
    if (Get-Command Emit-VcoMissingHookLibNotice -ErrorAction SilentlyContinue) {
        $missingNotice = Emit-VcoMissingHookLibNotice -HooksDir $ScriptDir `
            -ProjectRoot $ProjectRoot -SessionId $NoticeKey `
            -Lib "bash-write-targets.ps1"
        if ($missingNotice -and (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue)) {
            Emit-AdditionalContext $missingNotice PostToolUse
        }
    }
    exit 0
}
if (-not (Test-VcoWriteSuspicious -Command $Command -Tool $ToolName)) { exit 0 }

# Telemetry attribution for the children we spawn (same 3-layer chain as
# post-file-edit.ps1). v0.2.102 W6-C1 (F16): VCT_AGENT_ID joins the export
# so the debounced runner's kg-sync-failure sentinel keys per agent.
if ($SessionId) { $Env:VCT_SESSION_ID = $SessionId }
if ($AgentId) { $Env:VCT_AGENT_ID = $AgentId }

# --- Command -> written paths --------------------------------------------
Initialize-VcoBashWriteTargets -HooksDir $ScriptDir
$ScanState = Join-Path $ProjectRoot ".claude/state/bash_write_scan.ts"
$Paths = Get-VcoBashWriteTargets -Command $Command -ProjectRoot $ProjectRoot -ScanState $ScanState -Tool $ToolName
if (-not $Paths -or $Paths.Count -eq 0) { exit 0 }

# --- Route each path through the SHARED home ------------------------------
if (-not (Get-Command Invoke-VcoRouteTouchedPath -ErrorAction SilentlyContinue)) {
    # The routing home is absent -- a BROKEN INSTALL, not a fallback case
    # (review MAJOR-1). We have already parsed real write targets, so every
    # one of them is about to be dropped; report once per session on stderr
    # + one additionalContext envelope, then exit 0 as the contract requires.
    if (Get-Command Emit-VcoMissingHookLibNotice -ErrorAction SilentlyContinue) {
        $missingNotice = Emit-VcoMissingHookLibNotice -HooksDir $ScriptDir `
            -ProjectRoot $ProjectRoot -SessionId $NoticeKey `
            -Lib "route-touched-path.ps1"
        if ($missingNotice -and (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue)) {
            Emit-AdditionalContext $missingNotice PostToolUse
        }
    }
    exit 0
}
Initialize-VcoRoute -HooksDir $ScriptDir -ProjectRoot $ProjectRoot

$knowledgeSep = (Join-Path $ProjectRoot "knowledge").TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
$summaryHook = Join-Path $ScriptDir "kg-summary-generator.ps1"

foreach ($p in $Paths) {
    $path = $p.Trim()
    if (-not $path) { continue }
    Invoke-VcoRouteTouchedPath -Path $path -SessionId $SessionId

    # kg-summary parity. The shipped kg-summary-generator.ps1 is registered
    # on Edit|Write(knowledge/**/*.md) only, so a CLI-written node had no
    # LLM summary in knowledge/.node_formats.json and rendered empty at
    # hybrid_search's `summary` detail tier. Re-dispatch the EXISTING hook
    # with a synthesized payload rather than duplicating its logic; it
    # self-validates the path, self-debounces (60 s per file) and
    # backgrounds its own generator.
    if ($path.StartsWith($knowledgeSep, [StringComparison]::OrdinalIgnoreCase) `
        -and ($path -like "*.md") -and (Test-Path $summaryHook)) {
        $summaryPayload = @{
            tool_name  = "Write"
            tool_input = @{ file_path = $path }
        } | ConvertTo-Json -Compress
        try {
            $summaryPayload | & $script:PsExe -NoProfile -File $summaryHook *> $null
        } catch { }
    }
}

# Emit whatever the routing left for the model (currently the pending
# duplicate-scan report) as ONE PostToolUse envelope.
if ($script:VcoRouteNudge -and (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue)) {
    Emit-AdditionalContext $script:VcoRouteNudge PostToolUse
}
exit 0
