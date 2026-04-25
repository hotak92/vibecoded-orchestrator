#Requires -Version 5.1
<#
.SYNOPSIS
    VibeCoded Tools - Orchestrator Installer for Windows
.DESCRIPTION
    Installs the orchestrator with all dependencies.
    Requires Python 3.11+.
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
    [switch]$NoSkills
)

$ErrorActionPreference = "Stop"

Write-Host "=== VibeCoded Tools - Orchestrator Installer ===" -ForegroundColor Cyan
Write-Host ""

# Find Python 3.11+
$pythonCmd = $null
$pythonArgs = @()

foreach ($cmd in @("python3.12", "python3.11", "python3", "python", "py")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        $version = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($version) {
            $parts = $version.Split(".")
            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if ($major -ge 3 -and $minor -ge 11) {
                $pythonCmd = $cmd
                break
            }
        }
    }
}

# Try Windows py launcher with version flag
if (-not $pythonCmd) {
    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($ver in @("3.12", "3.11")) {
            $version = & py "-$ver" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                $pythonCmd = "py"
                $pythonArgs = @("-$ver")
                break
            }
        }
    }
}

if (-not $pythonCmd) {
    Write-Host "ERROR: Python 3.11+ required." -ForegroundColor Red
    Write-Host "Install: winget install Python.Python.3.12"
    Write-Host "Or download: https://python.org/downloads/"
    exit 1
}

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
