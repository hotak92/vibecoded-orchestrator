# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# SessionStart hook: ensure vct-hub is running (Step 9, v0.2.21).
# PowerShell port of session-start-ensure-hub.sh — same semantics.
#
# Idempotent: `python -m vco_lib.hub_ensure ensure` leaves a live hub alone
# and otherwise invokes `vct-hub --start-if-not-running` (Step 5's CLI).
# Soft-fail throughout — never blocks Claude Code startup. Worst case:
# a single stderr line + exit 0.
#
# v0.2.92 (ruling R20): binary discovery and the spawn are NO LONGER mirrored
# here. They live in `vco_lib/hub_ensure.py`, the ONE home shared with the
# .sh sibling and the Rust launcher (`hub_launcher.rs`). The discovery chain
# it implements is unchanged:
#   1. $env:VCT_HUB_BIN    — explicit override (dev builds, custom installs)
#   2. <repo_root>\launcher\dist\<arch>\vct-hub(.exe)  (INSTALL-FOLDER copy)
#      then <repo_root>\launcher\dist\vct-hub(.exe)    (arch-less fallback)
#   3. PATH                — first vct-hub.exe / vct-hub on PATH
#   4. $HOME\.vct\bin\vct-hub.exe (or .\vct-hub on non-Windows PowerShell)
# If none match the module exits 3 with a named reason; this hook turns that
# into one stderr line + exit 0.
#
# Env overrides:
#   $env:VCT_HUB_BIN       — explicit binary path (highest precedence).
#   $env:VCT_DISABLE_HOOKS — set to non-empty to bypass entirely.
#   $env:VCO_HOOK_DEBUG=1  — verbose stderr, and run the spawn in the
#                            foreground (`--wait`) so its exit code is known.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ScriptDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

function Write-Debug-Line {
    param([string]$Message)
    if ($env:VCO_HOOK_DEBUG -eq "1") {
        [Console]::Error.WriteLine("[vct] $Message")
    }
}

# ---------------------------------------------------------------------------
# v0.2.54 Track C (C-7): respect the orchestrator update gate (parity with
# the .sh sibling). During `update_orchestrator` the launcher writes
# `<vct_root>\.update-in-progress.json` and explicitly STOPS vct-hub so the
# binary can be swapped (Windows mandatory locks). Respawning the hub here
# mid-update would re-lock vct-hub.exe between the stop and the swap. MCP
# servers already honour this gate (exit 75); the hook does too.
#
# The gate stays HERE, not in `vco_lib.hub_ensure`: the two callers answer it
# differently on purpose. The launcher parses the in-JSON deadline
# (`commands::update_gate::is_update_in_progress`); this hook has no JSON
# deadline parse, so it uses the mtime proxy below. The launcher rewrites the
# lockfile on every phase advance and the expected update duration is 15
# minutes, so "modified within the last 15 minutes" is a faithful proxy for
# the in-JSON `expected_completion_by` deadline.
# ---------------------------------------------------------------------------
$vctRoot = if ($env:VCT_STATE_DIR) { $env:VCT_STATE_DIR } else {
    $p = [System.Environment]::GetFolderPath('UserProfile')
    if (-not $p -and $env:HOME) { $p = $env:HOME }
    Join-Path $p ".vct"
}
$updateGateFile = Join-Path $vctRoot ".update-in-progress.json"
if (Test-Path -LiteralPath $updateGateFile) {
    try {
        $gateAge = (Get-Date) - (Get-Item -LiteralPath $updateGateFile).LastWriteTime
        if ($gateAge.TotalMinutes -lt 15) {
            [Console]::Error.WriteLine("[vct] orchestrator update in progress ($updateGateFile) -- skipping vct-hub auto-start")
            exit 0
        }
        Write-Debug-Line "stale update gate file present (>15 min old) -- ignoring"
    } catch {
        Write-Debug-Line "update gate probe failed: $($_.Exception.Message) -- continuing"
    }
}

# ---------------------------------------------------------------------------
# Hub discovery + "start if not running": ONE home -- `python -m vco_lib.hub_ensure`
# (v0.2.92, ruling R20). This hook, its .sh sibling and `hub_launcher.rs` each
# used to carry their own copy of the four-step chain; the copies had already
# drifted on arch-slot naming. Class A of the A>B>C rule: one Python
# implementation, called via a ~50 ms subprocess on this session-start path.
#
# Loud-fail: if the resolver cannot run at all (no interpreter, broken
# install), say so on stderr and skip -- never fall back to an inline copy.
# ---------------------------------------------------------------------------
$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
$RunPy = $PY
$VenvLib = Join-Path $LibDir "resolve-vco-venv.ps1"
if (Test-Path $VenvLib) {
    . $VenvLib
    try {
        $VcoVenvPython = Resolve-VcoVenvPython -ScriptDir $PSScriptRoot
        if ($VcoVenvPython -and (Test-Path $VcoVenvPython)) { $RunPy = $VcoVenvPython }
    } catch { }
}
if (-not $RunPy) {
    # v0.2.92 MAJOR-6: stdout, not stderr (see the .sh sibling).
    Write-Output "session-start-ensure-hub: no Python interpreter for vco_lib.hub_ensure (broken VCO install?); skipping"
    exit 0
}

# The .ps1 side uses --json + ConvertFrom-Json (the .sh side evals --shell),
# matching the established ensure-containers pair.
$HubArgs = @("-m", "vco_lib.hub_ensure", "ensure", "--json", "--repo-root", $RepoRoot)
if ($env:VCO_HOOK_DEBUG -eq "1") {
    # Foreground so the hub's own exit code is observable while debugging.
    $HubArgs += "--wait"
}
$HubRes = $null
$HubRc = $null
try {
    # v0.2.92 MAJOR-6: `2>$null` threw the reason away, so a crash on Windows
    # printed only "rc=N" with no cause. Capture it and report the tail.
    $HubErr = [System.IO.Path]::GetTempFileName()
    $HubJson = & $RunPy @HubArgs 2>$HubErr
    $HubRc = $LASTEXITCODE
    if ($HubRc -in 0, 3, 4) { $HubRes = ($HubJson | Out-String) | ConvertFrom-Json }
} catch { $HubRes = $null }
if (-not $HubRes) {
    $HubWhy = ""
    if (Test-Path $HubErr) {
        $HubWhy = ((Get-Content $HubErr -Tail 3 -ErrorAction SilentlyContinue) -join " ").Trim()
        Remove-Item $HubErr -Force -ErrorAction SilentlyContinue
    }
    Write-Output "session-start-ensure-hub: vco_lib.hub_ensure failed (rc=$HubRc): $HubWhy; skipping"
    exit 0
}

# Soft-fail contract: the module exits 3 (no binary) / 4 (spawn failed) LOUDLY
# with a named reason; the hook reports it once and still exits 0, because a
# SessionStart hook must never block Claude Code from starting.
if ($HubRc -ne 0) {
    Write-Output "[vct] $($HubRes.reason)"
    exit 0
}

$HubWhat = if ($HubRes.binary) { $HubRes.binary } else { "pid $($HubRes.pid)" }
Write-Debug-Line "vct-hub $($HubRes.state): $HubWhat"
exit 0
