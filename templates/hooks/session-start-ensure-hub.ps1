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
# Binary-discovery order:
#   1. $env:VCT_HUB_BIN    — explicit override (dev builds, custom installs)
#   2. PATH                — first vct-hub.exe / vct-hub on PATH
#   3. $HOME\.vct\bin\vct-hub.exe (or .\vct-hub on non-Windows PowerShell)
#   4. <orchestrator_root>\launcher\dist\<arch>\vct-hub(.exe)
#   5. <orchestrator_root>\launcher\dist\vct-hub(.exe)
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

    # 2. PATH.
    foreach ($name in Get-HubExeNames) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            Write-Debug-Line "found on PATH: $($cmd.Source)"
            return $cmd.Source
        }
    }

    # 3. Known user-install location.
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

    # 4. In-tree dev build (arch-qualified subdir).
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

    # 5. In-tree dev build (arch-less fallback).
    foreach ($name in Get-HubExeNames) {
        $candidate = Join-Path $RepoRoot "launcher\dist\$name"
        if (Test-Path -LiteralPath $candidate) {
            Write-Debug-Line "found at in-tree flat dist: $candidate"
            return $candidate
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
