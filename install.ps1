#Requires -Version 5.1
<#
.SYNOPSIS
    VibeCoded Tools - Orchestrator Installer for Windows
.DESCRIPTION
    Installs the orchestrator with all dependencies. Requires Python 3.11+.

    If Python 3.11+ is not on PATH, this wrapper offers to install it via
    winget (Python.Python.3.12). Auto-install is INTERACTIVE: pass
    -NonInteractive (or -Quiet) to disable and just fail with a hint.

    Why a shell wrapper instead of bootstrapping in Python: chicken-and-egg
    — install.py needs Python to run. A standalone bootstrap binary
    (Rust/Go) and an `uv` (Astral) bootstrap are tracked for v1.1. For v1.0
    the lightest touch is a wrapper that leans on winget.
.PARAMETER NoContainers
    Skip Docker/Podman service setup
.PARAMETER Gpu
    Enable GPU support for Ollama + code embeddings
.PARAMETER CpuOnly
    Force CPU-only mode
.PARAMETER OpenaiKey
    Use OpenAI embeddings (provide API key)
.PARAMETER Container
    Force container runtime: docker or podman
.PARAMETER Dev
    Install development dependencies
.PARAMETER Update
    Update mode: skip clone, re-install deps + restart services
.PARAMETER SkipModels
    Skip pulling Ollama models (manual later)
.PARAMETER Quiet
    Minimal output
.PARAMETER LowResource
    Lightest mode: Jina V2 (768d) via Ollama. For low-RAM/low-VRAM machines.
.PARAMETER WithJoern
    Force-enable Joern integration for richer code-graph metrics (CFG/PDG).
.PARAMETER NoJoern
    Skip Joern detection entirely.
.PARAMETER NoAgents
    Skip installing Claude agents.
.PARAMETER WithMaoAgents
    Install MAO-tier specialist agents.
.PARAMETER NoSkills
    Skip installing Claude skills.
.PARAMETER NoCompile
    Skip the bytecode-compile step (Step 11b). First import of orchestrator
    modules will be ~50-200ms slower; useful for dev/CI runs.
.PARAMETER NonInteractive
    Refuse to auto-install Python; fail with a hint instead.
#>
param(
    [switch]$NoContainers,
    [switch]$Gpu,
    [switch]$CpuOnly,
    [switch]$LowResource,
    [string]$OpenaiKey = "",
    [string]$Container = "",
    [switch]$Dev,
    [switch]$Update,
    [switch]$SkipModels,
    [switch]$Quiet,
    [switch]$WithJoern,
    [switch]$NoJoern,
    [switch]$NoAgents,
    [switch]$WithMaoAgents,
    [switch]$NoSkills,
    [switch]$NoCompile,
    [switch]$NonInteractive,
    [switch]$Yes,
    [switch]$NoAutoLaunch,
    [switch]$NoDesktopIcon
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# .bat-style args reconciliation: first-install.bat forwards `%*` (all user
# args) directly to this script. PowerShell's -File parameter binds the ones
# matching `param()` switches, but only in PS-style (`-Yes` not `--yes`).
# Double-dash args (`--yes`, `--no-auto-launch`) fall through to `$args`
# instead of binding to switches. Walk `$args` and lift them into the bound
# switch variables so the rest of the script sees a consistent state
# regardless of whether the user typed `-Yes` or `--yes`.
# ---------------------------------------------------------------------------
foreach ($a in $args) {
    switch -Regex ($a) {
        '^--yes$|^-y$'           { $Yes = $true }
        '^--non-interactive$'    { $NonInteractive = $true }
        '^--quiet$'              { $Quiet = $true }
        '^--no-auto-launch$'     { $NoAutoLaunch = $true }
        '^--no-desktop-icon$'    { $NoDesktopIcon = $true }
        '^--no-containers$'      { $NoContainers = $true }
        '^--gpu$'                { $Gpu = $true }
        '^--cpu-only$'           { $CpuOnly = $true }
        '^--low-resource$'       { $LowResource = $true }
        '^--dev$'                { $Dev = $true }
        '^--update$'             { $Update = $true }
        '^--skip-models$'        { $SkipModels = $true }
        '^--with-joern$'         { $WithJoern = $true }
        '^--no-joern$'           { $NoJoern = $true }
        '^--no-agents$'          { $NoAgents = $true }
        '^--with-mao-agents$'    { $WithMaoAgents = $true }
        '^--no-skills$'          { $NoSkills = $true }
        '^--no-compile$'         { $NoCompile = $true }
    }
}

Write-Host "=== VibeCoded Tools - Orchestrator Installer ===" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# WSL guard: PowerShell can technically run inside WSL via pwsh, but the
# orchestrator's container layer (compose against host Podman/Docker) is a
# different story across the WSL boundary. If we detect WSL, redirect the
# user to install.sh inside their WSL distro — that's the supported path.
# `$env:WSL_DISTRO_NAME` is set by Microsoft's WSL runtime; absence means
# native Windows or non-WSL environment.
# ---------------------------------------------------------------------------
if ($env:WSL_DISTRO_NAME) {
    Write-Host "Detected WSL ('$env:WSL_DISTRO_NAME')." -ForegroundColor Yellow
    Write-Host "Use the Linux installer instead — run inside your WSL distro:"
    Write-Host "  ./install.sh"
    Write-Host ""
    Write-Host "install.ps1 is for native Windows only."
    exit 1
}

# Treat -Quiet, $env:CI, and $env:VCT_NON_INTERACTIVE as non-interactive too.
$nonInteractiveMode = $NonInteractive -or $Quiet -or `
    ($env:CI -ne $null -and $env:CI -ne "") -or `
    ($env:VCT_NON_INTERACTIVE -ne $null -and $env:VCT_NON_INTERACTIVE -ne "")

# ---------------------------------------------------------------------------
# Python detection
# ---------------------------------------------------------------------------
function Find-Python {
    $candidates = @("python3.13", "python3.12", "python3.11", "python3", "python")
    foreach ($cmd in $candidates) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            $version = & $cmd -c "import sys; sys.stdout.write('%d.%d' % (sys.version_info[0], sys.version_info[1]))" 2>$null
            if ($version) {
                $parts = $version.Split(".")
                $major = [int]$parts[0]
                $minor = [int]$parts[1]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                    return @{ Cmd = $cmd; Args = @() }
                }
            }
        }
    }
    # Try Windows py launcher with explicit version.
    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($ver in @("3.13", "3.12", "3.11")) {
            $version = & py "-$ver" -c "import sys; sys.stdout.write('%d.%d' % (sys.version_info[0], sys.version_info[1]))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                return @{ Cmd = "py"; Args = @("-$ver") }
            }
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Manual install hint
# ---------------------------------------------------------------------------
function Print-ManualHint {
    Write-Host ""
    Write-Host "Install Python 3.11+ manually, then re-run install.ps1:" -ForegroundColor Yellow
    Write-Host "  winget:           winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements"
    Write-Host "  Microsoft Store:  https://apps.microsoft.com/detail/9ncvdn91xzqp        # Python 3.12 (Microsoft Store)"
    Write-Host "  python.org:       https://www.python.org/downloads/"
    Write-Host "  Docs:             https://github.com/hotak92/vibecoded-orchestrator#prerequisites"
}

# ---------------------------------------------------------------------------
# Auto-install Python (winget)
# ---------------------------------------------------------------------------
function Prompt-Yes {
    param([string]$Question)
    if ($nonInteractiveMode) { return $false }
    $reply = Read-Host "$Question [Y/n]"
    if ([string]::IsNullOrWhiteSpace($reply)) { return $true }
    return ($reply -match '^[Yy]')
}

function Attempt-Install-Python {
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) {
        # winget ships with Windows 10 1809+ / 11; older installs don't
        # have it, and we don't try to install winget itself (the
        # canonical install path is the Microsoft Store, which itself
        # requires a sign-in flow we won't shoulder).
        Write-Host "ERROR: winget not found." -ForegroundColor Red
        Write-Host "       winget ships with Windows 10 1809+ / 11. On older Windows, install Python manually:"
        Write-Host "         Microsoft Store: https://apps.microsoft.com/detail/9ncvdn91xzqp"
        Write-Host "         python.org:      https://www.python.org/downloads/"
        return $false
    }
    Write-Host "Detected winget. Will run:"
    Write-Host "  winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements"
    Write-Host "  (May trigger a UAC elevation prompt depending on your machine policy.)"
    if (-not (Prompt-Yes "Proceed?")) { return $false }

    & winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: winget install failed (exit $LASTEXITCODE)." -ForegroundColor Red
        Write-Host "       Try installing Python manually:"
        Write-Host "         Microsoft Store: https://apps.microsoft.com/detail/9ncvdn91xzqp"
        Write-Host "         python.org:      https://www.python.org/downloads/"
        return $false
    }
    return $true
}

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
$py = Find-Python

if (-not $py) {
    Write-Host "Python 3.11+ not found on PATH."

    if ($nonInteractiveMode) {
        Write-Host "ERROR: non-interactive mode - refusing to auto-install Python." -ForegroundColor Red
        Print-ManualHint
        exit 1
    }

    Write-Host ""
    Write-Host "vibecoded-orchestrator requires Python 3.11 or newer."
    Write-Host ""

    if (Attempt-Install-Python) {
        Write-Host ""
        Write-Host "Re-checking for Python..."
        # winget installs may not refresh PATH for the current shell. Reload it.
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $py = Find-Python
        if (-not $py) {
            Write-Host "ERROR: Python install appeared to succeed but no 3.11+ interpreter is on PATH." -ForegroundColor Red
            Write-Host "       Open a new PowerShell window and re-run install.ps1."
            Print-ManualHint
            exit 1
        }
    } else {
        Print-ManualHint
        exit 1
    }
}

$pythonCmd = $py.Cmd
$pythonArgs = $py.Args

$pyVersion = if ($pythonArgs.Count -gt 0) {
    & $pythonCmd @pythonArgs --version
} else {
    & $pythonCmd --version
}
Write-Host "Using Python: $pythonCmd $pyVersion"

# Change to script directory
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

# Build arguments for install.py
$installArgs = @()
if ($NoContainers)  { $installArgs += "--no-containers" }
if ($Gpu)           { $installArgs += "--gpu" }
if ($CpuOnly)       { $installArgs += "--cpu-only" }
if ($LowResource)   { $installArgs += "--low-resource" }
if ($OpenaiKey)     { $installArgs += "--openai-key"; $installArgs += $OpenaiKey }
if ($Container)     { $installArgs += "--container"; $installArgs += $Container }
if ($Dev)           { $installArgs += "--dev" }
if ($Update)        { $installArgs += "--update" }
if ($SkipModels)    { $installArgs += "--skip-models" }
if ($Quiet)         { $installArgs += "--quiet" }
if ($WithJoern)     { $installArgs += "--with-joern" }
if ($NoJoern)       { $installArgs += "--no-joern" }
if ($NoAgents)      { $installArgs += "--no-agents" }
if ($WithMaoAgents) { $installArgs += "--with-mao-agents" }
if ($NoSkills)      { $installArgs += "--no-skills" }
if ($NoCompile)     { $installArgs += "--no-compile" }
# --yes propagation: critical for first-install.bat --yes flow on Windows.
# Without this, install.py drops to interactive prompts that hang silently
# in the .bat-spawned PowerShell window. -NonInteractive implies -Yes too
# (matches the .ps1's own non-interactive semantics on line ~95).
if ($Yes -or $NonInteractive) { $installArgs += "--yes" }

if ($pythonArgs.Count -gt 0) {
    & $pythonCmd @pythonArgs install.py @installArgs
} else {
    & $pythonCmd install.py @installArgs
}

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Installation failed. See errors above." -ForegroundColor Red
    exit $LASTEXITCODE
}
