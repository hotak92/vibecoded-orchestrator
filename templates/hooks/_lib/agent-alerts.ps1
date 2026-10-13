# _lib/agent-alerts.ps1 - v0.2.102 W6-C2 fix round (review R4c-2/R4c-3).
#
# File one alert a SUBAGENT hit, for delivery to its PARENT
# (agent-alerts-deliver). The path, the row format and the append/claim
# locking live in ONE place, vco_lib/agent_alerts.py; this helper only hands
# it the row's inputs as JSON on stdin (`python -m vco_lib.agent_alerts
# append`). Hooks used to hand-build the row and Add-Content it, racing a
# delivery's claim of the same file.
#
# Add-VcoAgentAlert -ScriptDir -ProjectRoot -SessionId -AgentId -AgentType
#                   -Kind -Message [-File]
#   Returns $true when filed (or nothing to file: no agent id), $false when
#   the alert could NOT be filed. Never silent on failure: an unresolvable
#   VCO venv is a BROKEN install and is said so on stderr; callers add their
#   own louder channel.
#
# Dot-sourced, never executed. MUST MATCH _lib/agent-alerts.sh.

function Add-VcoAgentAlert {
    param(
        [string]$ScriptDir,
        [string]$ProjectRoot,
        [string]$SessionId,
        [string]$AgentId,
        [string]$AgentType,
        [string]$Kind,
        [string]$Message,
        [string]$File = ""
    )
    if (-not $AgentId) { return $true }
    if (-not (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue)) {
        $venvLib = Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1"
        if (Test-Path -LiteralPath $venvLib) { . $venvLib }
    }
    $venvPy = $null
    if (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue) {
        $venvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
    }
    if (-not $venvPy -or -not (Test-Path -LiteralPath $venvPy)) {
        [Console]::Error.WriteLine("agent-alerts: VCO venv not resolvable - this subagent alert was NOT filed for the parent (broken VCO install; repair it with the orchestrator update): $Message")
        return $false
    }
    $row = [ordered]@{
        session_id = $SessionId
        agent_id   = $AgentId
        agent_type = $AgentType
        kind       = $Kind
        message    = $Message
    }
    if ($File) { $row["file"] = $File }
    $json = $row | ConvertTo-Json -Compress -Depth 4
    # ASCII-only on the pipe: Windows PowerShell 5.1 encodes a native pipe
    # with $OutputEncoding (ASCII), which would mangle a non-ASCII path.
    $json = [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
    try {
        $json | & $venvPy -m vco_lib.agent_alerts append --project-root $ProjectRoot
        if ($LASTEXITCODE -ne 0) { throw "exit $LASTEXITCODE" }
    } catch {
        [Console]::Error.WriteLine("agent-alerts: filing this subagent alert for the parent FAILED: $Message")
        return $false
    }
    return $true
}
