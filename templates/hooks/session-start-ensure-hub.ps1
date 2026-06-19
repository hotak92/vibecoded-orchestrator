# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# SessionStart hook: ensure vct-hub is running (Step 9, v0.2.21).
# PowerShell port of session-start-ensure-hub.sh — same semantics.
#
# Idempotent: invokes `vct-hub --start-if-not-running` (Step 5's CLI),
# which returns 0 whether the hub started fresh OR was already running.
# Soft-fail throughout — never blocks Claude Code startup. Worst case:
# a single stderr line + exit 0.
#
# Binary-discovery order (v0.2.63: install-folder copy preferred over PATH —
# must match the launcher's hub_launcher::find_hub_binary):
#   1. $env:VCT_HUB_BIN    — explicit override (dev builds, custom installs)
#   2. <repo_root>\launcher\dist\<arch>\vct-hub(.exe)  (INSTALL-FOLDER copy)
#      then <repo_root>\launcher\dist\vct-hub(.exe)    (arch-less fallback)
#   3. PATH                — first vct-hub.exe / vct-hub on PATH
#   4. $HOME\.vct\bin\vct-hub.exe (or .\vct-hub on non-Windows PowerShell)
# If none match: emit one stderr line, exit 0.
#
# Env overrides:
#   $env:VCT_HUB_BIN       — explicit binary path (highest precedence).
#   $env:VCT_DISABLE_HOOKS — set to non-empty to bypass entirely.
#   $env:VCO_HOOK_DEBUG=1  — verbose stderr (which path won, exit code).

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
# Staleness without a JSON parse: the launcher rewrites the lockfile on
# every phase advance and the expected update duration is 15 minutes, so
# "modified within the last 15 minutes" is a faithful proxy for the
# in-JSON `expected_completion_by` deadline.
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
# Get-ArchDirName :: name matching launcher\dist\<arch>\.
# Best-effort; empty string if not derivable.
# ---------------------------------------------------------------------------
function Get-ArchDirName {
    $arch = ""
    try {
        $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLower()
    } catch { }
    if ($IsWindows -or ($PSVersionTable.PSEdition -eq "Desktop")) {
        return "windows-$arch"
    } elseif ($IsMacOS) {
        return "macos-$arch"
    } elseif ($IsLinux) {
        return "linux-$arch"
    }
    return ""
}

# ---------------------------------------------------------------------------
# Get-HubExeNames :: list of candidate filenames to probe at each location.
# Windows tries .exe first; POSIX-style PowerShell tries the bare binary.
# ---------------------------------------------------------------------------
function Get-HubExeNames {
    if ($IsWindows -or ($PSVersionTable.PSEdition -eq "Desktop")) {
        return @("vct-hub.exe", "vct-hub")
    }
    return @("vct-hub", "vct-hub.exe")
}

# ---------------------------------------------------------------------------
# Find-HubBinary :: returns the first existing+executable candidate, or $null.
# ---------------------------------------------------------------------------
function Find-HubBinary {
    # 1. Explicit override.
    if ($env:VCT_HUB_BIN) {
        if (Test-Path -LiteralPath $env:VCT_HUB_BIN) {
            Write-Debug-Line "found via VCT_HUB_BIN: $($env:VCT_HUB_BIN)"
            return $env:VCT_HUB_BIN
        }
        Write-Debug-Line "VCT_HUB_BIN set but not found: $($env:VCT_HUB_BIN) -- falling through"
    }

    # 2. INSTALL-FOLDER copy (v0.2.63): the repo's own dist hub, arch-qualified
    #    subdir then arch-less fallback. PREFERRED over PATH/.vct\bin so a stale
    #    vct-hub on PATH never wins over the copy install.py deployed for THIS
    #    project. Must match the launcher's find_hub_binary order (hub_launcher.rs).
    $arch = Get-ArchDirName
    if ($arch) {
        foreach ($name in Get-HubExeNames) {
            $candidate = Join-Path $RepoRoot "launcher\dist\$arch\$name"
            if (Test-Path -LiteralPath $candidate) {
                Write-Debug-Line "found at in-tree arch dist: $candidate"
                return $candidate
            }
        }
    }
    foreach ($name in Get-HubExeNames) {
        $candidate = Join-Path $RepoRoot "launcher\dist\$name"
        if (Test-Path -LiteralPath $candidate) {
            Write-Debug-Line "found at in-tree flat dist: $candidate"
            return $candidate
        }
    }

    # 3. PATH.
    foreach ($name in Get-HubExeNames) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            Write-Debug-Line "found on PATH: $($cmd.Source)"
            return $cmd.Source
        }
    }

    # 4. Known user-install location.
    $userHome = [System.Environment]::GetFolderPath('UserProfile')
    if (-not $userHome -and $env:HOME) { $userHome = $env:HOME }
    if ($userHome) {
        foreach ($name in Get-HubExeNames) {
            $candidate = Join-Path $userHome ".vct\bin\$name"
            if (Test-Path -LiteralPath $candidate) {
                Write-Debug-Line "found at user install: $candidate"
                return $candidate
            }
        }
    }

    return $null
}

$HubBin = Find-HubBinary

if (-not $HubBin) {
    [Console]::Error.WriteLine("[vct] vct-hub not found on PATH; skipping auto-start (set VCT_HUB_BIN to override)")
    exit 0
}

# Idempotent invocation. `--start-if-not-running` exits 0 whether the hub
# started fresh, was already running, or could not start (in which case
# the hub writes its own diagnostic to stderr).
#
# We deliberately spawn detached so that a slow first-time start cannot
# block the SessionStart hook bus past its 10s budget. The hub itself
# short-circuits when already running, so the cost of the spawn is bounded.
try {
    if ($env:VCO_HOOK_DEBUG -eq "1") {
        & $HubBin --start-if-not-running
        Write-Debug-Line "vct-hub --start-if-not-running exit=$LASTEXITCODE"
    } else {
        # Spawn detached so a cold-start hub probe cannot block the hook bus.
        # WindowStyle Hidden + no -Wait: returns immediately, stdout/stderr
        # default to the parent stream which Claude Code drops past the
        # 10s hook timeout. The hub writes its own log file regardless.
        Start-Process -FilePath $HubBin -ArgumentList "--start-if-not-running" `
            -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
    }
} catch {
    Write-Debug-Line "spawn failed: $($_.Exception.Message)"
}

exit 0
