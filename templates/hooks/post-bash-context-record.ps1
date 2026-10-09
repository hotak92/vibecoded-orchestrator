# OS-EXEMPT-PARITY: v0.2.92 fixed a PowerShell-only epoch bug (mixed-DateTimeKind subtraction shifts by the UTC offset) — the .sh sibling's stamps were always correct; both siblings are pinned by tests/test_v0292_ps1_unix_epoch_parity.py.
# V52-M (v0.2.52) — post-bash outcome event recorder
# OS-PARITY: ports the .sh sibling. Fires AFTER Bash tool executes.
# Re-derives cmd_hash from stdin, resolves the state file written by
# pre-bash-context-inject.ps1 (per-CALL name via tool_use_id — v0.2.102
# W3R-3, legacy hash-only name as the fallback), claims it atomically
# (Move-Item to a per-PID claim name — the concurrent-spawn loser exits),
# emits a bash_outcome event with the same task_id, and deletes the claim
# ONLY after the emit succeeded OR the loss is ledgered (W3R-4:
# `outcome_unsent` via vco_lib.rl_telemetry_loss, the one loss-ledger home).

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

. "$PSScriptRoot/_lib/stderr-cap.ps1"

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

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

$ToolName = ""
$SessionId = ""
$Command = ""
$ToolResponse = ""
$ToolUseId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name) { $ToolName = [string]$payload.tool_name }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.tool_input -and $payload.tool_input.command) {
            $Command = [string]$payload.tool_input.command
        }
        if ($payload.tool_use_id) { $ToolUseId = [string]$payload.tool_use_id }
        if ($payload.tool_response) {
            if ($payload.tool_response -is [string]) {
                $ToolResponse = $payload.tool_response
            } else {
                $ToolResponse = ($payload.tool_response | ConvertTo-Json -Compress -Depth 8)
            }
        }
    }
} catch { }

# Bash, or the PowerShell tool (v0.2.102 W5-D) -- pairs with pre-bash.
if ($ToolName -ne "Bash" -and $ToolName -ne "PowerShell") { exit 0 }
if (-not $SessionId) { $SessionId = "default" }
if (-not $Command) { exit 0 }

# Re-derive cmd_hash from the command (must match pre-bash's hash exactly)
$md5 = [System.Security.Cryptography.MD5]::Create()
# [System.Text.Encoding] — NOT [System.Security.Encoding] (an invalid type
# name that throws at runtime, silently killing the hash -> null CmdHash -> no
# pairing on Windows; a v0.2.102 rewrite slip, caught in review and pinned by an
# end-to-end pwsh test).
$bytes = [System.Text.Encoding]::UTF8.GetBytes($Command)
$CmdHash = (($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 16)

# W3R-3: same sanitize ladder as pre-bash + the router's bash_claim_path.
$Tuid = ($ToolUseId -replace '[^A-Za-z0-9_-]', '')
if ($Tuid.Length -gt 64) { $Tuid = $Tuid.Substring(0, 64) }

$StateDir = Join-Path $ProjectRoot ".claude/state"
# Per-call name when the harness names the call; legacy name otherwise AND
# as the old-format fallback (a pre-W3R-3 state file still pairs).
$StateFile = Join-Path $StateDir "bash_task_${SessionId}_${CmdHash}.json"
if ($Tuid) {
    $PerCall = Join-Path $StateDir "bash_task_${SessionId}_${CmdHash}_${Tuid}.json"
    if (Test-Path -LiteralPath $PerCall) { $StateFile = $PerCall }
}

# If pre-bash didn't fire (MECHANICAL / crash) → no state file → skip silently.
if (-not (Test-Path -LiteralPath $StateFile)) { exit 0 }

# === W3R-3: atomic one-shot claim =====================================
# Move-Item (rename) is atomic: of two concurrent spawns only one can move
# the file; the loser exits (the winner records the one outcome this call
# gets). The claim name keeps the bash_task_*.json filter so the 1-day GC
# reclaims a crashed holder's claim.
$ClaimFile = "$($StateFile.Substring(0, $StateFile.Length - 5)).claim.$PID.json"
try {
    Move-Item -LiteralPath $StateFile -Destination $ClaimFile -Force -ErrorAction Stop
} catch {
    exit 0
}

# === Compute output length + exit-code heuristic =======================
$OutputLen = $ToolResponse.Length
$ExitCode = 0
if ($ToolResponse.StartsWith('<tool_use_error>') -or
    $ToolResponse.StartsWith('Error:') -or
    ($ToolResponse.Length -gt 0 -and $ToolResponse.Substring(0, [Math]::Min(200, $ToolResponse.Length)) -match 'Command exited with')) {
    $ExitCode = 1
}

$EndTsMs = [long][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

# === W3R-4: hook-side loss ledger (the child ledger cannot run) =======
# Records `outcome_unsent` through vco_lib.rl_telemetry_loss (the ONE
# loss-ledger home) using ANY python ($PY) with the orchestrator checkout
# on PYTHONPATH. Best-effort: returns $true only when a line was written.
function Record-OutcomeLoss {
    param([string]$Reason)
    foreach ($root in @($env:VCT_INSTALL_ROOT, $env:VCT_ORCHESTRATOR_ROOT)) {
        if (-not $root) { continue }
        if (-not (Test-Path (Join-Path $root "vco_lib"))) { continue }
        $prevPath = $env:PYTHONPATH
        $env:VCT_PB_CLAIM_FILE = $ClaimFile
        $env:VCT_PB_LOSS_REASON = $Reason
        $env:PYTHONPATH = if ($prevPath) { "$root$([IO.Path]::PathSeparator)$prevPath" } else { $root }
        try {
            & $PY -c @'
import json, os
task_id = ''
try:
    with open(os.environ.get('VCT_PB_CLAIM_FILE', ''), 'r') as fh:
        d = json.load(fh)
    if isinstance(d, dict):
        task_id = str(d.get('task_id', '') or '')
except Exception:
    pass
from vco_lib.rl_telemetry_loss import KIND_OUTCOME_UNSENT, record_loss
record_loss(KIND_OUTCOME_UNSENT, os.environ.get('VCT_PB_LOSS_REASON', ''),
            task_id=task_id, task_type='bash_outcome')
'@ 2>$null
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch { } finally {
            $env:PYTHONPATH = $prevPath
        }
    }
    return $false
}

# === Resolve venv + emit bash_outcome via Python helper ================
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir

$EmitRc = 127
if ($VenvPy -and (Test-Path $VenvPy)) {
    # Child contract (W3R-4): 0 = emit succeeded; 3 = the emit could not
    # run/complete pre-POST and the child already recorded an
    # `outcome_unsent` ledger line; anything else = the child itself
    # crashed — the hook ledgers below. State JSON is read inside Python.
    $env:VCT_PB_CLAIM_FILE = $ClaimFile
    $pyCode = @"
import json, os, sys
# v0.2.94: pin the project root the hook already resolved for THIS child.
# A rootless embedding_service._detect_project_root() falls through to
# Path.cwd() (rule 4) and reconciles THAT directory's deferral ledger,
# rewriting its CLAUDE.md -- in a detached child, cwd is whatever the
# harness gave us, not the project. KG_BASE_DIR is rule 2 of the same
# ladder and already means the project folder path; setdefault, so an
# explicit VS Code / launcher value still wins.
_vco_project_root = r'''$ProjectRoot'''
if _vco_project_root:
    os.environ.setdefault('KG_BASE_DIR', _vco_project_root)

def _loss(reason, task_id=''):
    # W3R-4: pre-POST emit failures are losses -- record them in the ONE
    # loss ledger. Never raises into the hook.
    try:
        from vco_lib.rl_telemetry_loss import KIND_OUTCOME_UNSENT, record_loss
        record_loss(KIND_OUTCOME_UNSENT, reason, task_id=task_id,
                    task_type='bash_outcome')
    except Exception:
        pass

try:
    with open(os.environ['VCT_PB_CLAIM_FILE'], 'r') as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError('pairing state is not a JSON object')
except Exception:
    _loss('corrupt_state')
    sys.exit(3)
task_id = state.get('task_id', '')
start_ts_ms = int(state.get('start_ts_ms', 0))
end_ts_ms = $EndTsMs
duration_ms = max(0, end_ts_ms - start_ts_ms)
if not task_id:
    _loss('missing_task_id')
    sys.exit(3)

project_id = None
try:
    from vco_lib.project_config import resolve_for_project
    cfg = resolve_for_project(os.environ.get('CLAUDE_PROJECT_DIR', r'''$ProjectRoot'''))
    project_id = cfg.get('project_id') if isinstance(cfg, dict) else None
except Exception:
    pass

try:
    from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event
except Exception:
    _loss('emit_import_failed', task_id)
    sys.exit(3)
try:
    ok = emit_outcome_event(
        event_type='bash_outcome',
        task_id=task_id,
        task_type='bash_outcome',
        payload={
            'exit_code': $ExitCode,
            'output_len': $OutputLen,
            'duration_ms': duration_ms,
            'cmd_len': int(state.get('cmd_len', 0)),
        },
        session_id=state.get('session_id', r'''$SessionId'''),
        project_id=project_id,
    )
except Exception:
    # Soft-fail -- telemetry must not break the user's bash flow -- but the
    # loss is now VISIBLE (W3R-4), never a silent orphan.
    _loss('emit_raised', task_id)
    sys.exit(3)
if not ok:
    # False usually means the POST was attempted and hub_writer already
    # recorded hub_post_failed; it can also be a pre-POST soft-fail with no
    # line of its own. Record the pairing-side line either way -- the kinds
    # count different facts (POST loss vs unsent outcome), and a possible
    # extra line is cheaper than an invisible loss.
    _loss('emit_returned_false', task_id)
    sys.exit(3)
sys.exit(0)
"@
    try {
        $pyCode | & $VenvPy - 2>$null
        $EmitRc = $LASTEXITCODE
        if ($null -eq $EmitRc) { $EmitRc = 0 }
    } catch { $EmitRc = 1 }
}

# === One-shot pairing release: only after emit-success or a ledger line =
if ($EmitRc -eq 0 -or $EmitRc -eq 3) {
    # 0 = emitted; 3 = the child ledgered the loss itself.
    Remove-Item -LiteralPath $ClaimFile -Force -ErrorAction SilentlyContinue
} else {
    # Missing venv (127 sentinel) / crashed child: ledger from HERE, then
    # release; if even the ledger fails, KEEP the claim file -- deleting
    # the pairing data with no emit and no evidence is the silent loss
    # W3R-4 exists to end (the 1-day GC reclaims it later).
    $reason = if ($EmitRc -eq 127) { "venv_unavailable" } else { "emit_child_failed" }
    if (Record-OutcomeLoss $reason) {
        Remove-Item -LiteralPath $ClaimFile -Force -ErrorAction SilentlyContinue
    }
}

exit 0
