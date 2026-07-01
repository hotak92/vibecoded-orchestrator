# stop-codegraph-reminder.ps1 -- Stop hook (v0.2.72 P6)
# OS-PARITY: ports the .sh sibling. End-of-turn AGGREGATION of the "code file
# was just edited -> update CONTEXT_STATE / capture KG" reminder.
#
# post-file-edit.ps1 now APPENDS each edited path to a per-turn accumulator
# (.claude/state/edit_reminder_<sid>.txt) instead of emitting the nudge on every
# Edit (~15x/turn). This hook drains that file at turn-end and emits ONE
# aggregated reminder naming all edited files, then clears the accumulator.
#
# Contract: Stop hooks' plain stdout is discarded, so the reminder goes through
# Emit-AdditionalContext (JSON envelope; surfaces into the NEXT turn). Always
# exit 0; soft-fail throughout. The accumulator path convention MUST MATCH
# stop-codegraph-reminder.sh + post-file-edit.{sh,ps1}.
#
# Plain ASCII only (no em-dash, no BOM needed).

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

if (Test-Path "$PSScriptRoot/_lib/stderr-cap.ps1") { . "$PSScriptRoot/_lib/stderr-cap.ps1" }
if (Test-Path "$PSScriptRoot/_lib/emit-context.ps1") { . "$PSScriptRoot/_lib/emit-context.ps1" }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.session_id) { $SessionId = [string]$payload.session_id }
} catch { }

# Untrustworthy / path-hostile session id -> nothing to drain.
if ([string]::IsNullOrEmpty($SessionId)) { exit 0 }
if ($SessionId -match '[^A-Za-z0-9_-]') { exit 0 }

$stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
$accum = Join-Path $stateDir ("edit_reminder_{0}.txt" -f $SessionId)
if (-not (Test-Path -LiteralPath $accum)) { exit 0 }

# Drain: read accumulated paths, then remove the file so the next turn starts
# fresh (unlink even if the emit below no-ops).
$filesRaw = @()
try { $filesRaw = @(Get-Content -LiteralPath $accum -ErrorAction Stop) } catch { }
try { Remove-Item -LiteralPath $accum -ErrorAction SilentlyContinue } catch { }
if ($filesRaw.Count -eq 0) { exit 0 }

# Unique basenames, first-seen order, capped at 40.
$seen = @{}
$names = New-Object System.Collections.Generic.List[string]
foreach ($p in $filesRaw) {
    if ([string]::IsNullOrWhiteSpace($p)) { continue }
    $bn = Split-Path -Leaf $p
    if (-not $seen.ContainsKey($bn)) {
        $seen[$bn] = $true
        [void]$names.Add($bn)
        if ($names.Count -ge 40) { break }
    }
}
if ($names.Count -eq 0) { exit 0 }

$fileList = ($names | ForEach-Object { "  - $_" }) -join "`n"
$reminder = "[Code edit reminder] $($names.Count) code file(s) edited this turn:`n$fileList`nWhen you're done with this work item:`n- Update CONTEXT_STATE.md with what changed and what's next.`n- Capture any non-obvious learnings as a KG node under knowledge/concepts/."

if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
    Emit-AdditionalContext $reminder 'Stop'
}

exit 0
