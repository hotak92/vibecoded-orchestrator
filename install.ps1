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
.PARAMETER Help
    Print usage and exit 0 with no side effects. Also accepted in
    .bat-forwarded form: --help / -h / /help / /? (see the $args walk).
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
    [switch]$NoAgents,
    [switch]$WithMaoAgents,
    [switch]$NoSkills,
    [switch]$NoCompile,
    [switch]$NonInteractive,
    [switch]$Yes,
    [switch]$NoAutoLaunch,
    [switch]$NoDesktopIcon,
    [switch]$Help
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
        '^--no-agents$'          { $NoAgents = $true }
        '^--with-mao-agents$'    { $WithMaoAgents = $true }
        '^--no-skills$'          { $NoSkills = $true }
        '^--no-compile$'         { $NoCompile = $true }
        '^--help$|^-h$|^/help$|^/h$|^/\?$|^-\?$' { $Help = $true }
    }
}

# ---------------------------------------------------------------------------
# Help / usage (v0.2.54 G-1 — W-P1-3 regression fix). Must run BEFORE any
# side effect (WSL guard prints, WebView2 probe, Python detect, winget).
# Previously --help fell through the $args walk unmatched and a full
# install ran instead — that is how CI's `first-install.bat /help` smoke
# spent ~3 minutes side-effecting the runner before exiting non-zero.
# ---------------------------------------------------------------------------
if ($Help) {
    Write-Host "Usage: .\install.ps1 [options]"
    Write-Host ""
    Write-Host "VibeCoded Tools - Orchestrator installer for Windows."
    Write-Host "Wraps install.py: detects/installs Python 3.11+, then runs the"
    Write-Host "canonical 10-step install."
    Write-Host ""
    Write-Host "Options (PS-style switch / .bat-forwarded form):"
    Write-Host "  -Help            --help            Show this help and exit."
    Write-Host "  -Yes             --yes             Non-interactive, accept defaults."
    Write-Host "  -NonInteractive  --non-interactive Refuse auto-installs; fail with hints."
    Write-Host "  -NoContainers    --no-containers   Skip Docker/Podman service setup."
    Write-Host "  -SkipModels      --skip-models     Skip Ollama model pulls."
    Write-Host "  -NoAutoLaunch    --no-auto-launch  Skip post-install launcher spawn."
    Write-Host "  -NoDesktopIcon   --no-desktop-icon Skip desktop shortcut creation."
    Write-Host "  -Gpu / -CpuOnly / -LowResource     Hardware mode selection."
    Write-Host "  -Dev -Update -NoAgents -WithMaoAgents"
    Write-Host "  -NoSkills -NoCompile -Quiet        See .PARAMETER docs in this file."
    Write-Host "  -OpenaiKey <key>  -Container <docker|podman>"
    Write-Host ""
    Write-Host "Docs: docs/GETTING_STARTED.md  |  Recovery: docs/INSTALL_RECOVERY.md"
    exit 0
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
# WebView2 Runtime check (Windows-only)
#
# The Tauri launcher GUI links dynamically against Microsoft Edge WebView2
# Runtime. On Windows 10 1903+ and Windows 11 it's pre-installed via the
# Evergreen channel, but older Windows 10 SKUs (or images where Edge updates
# were disabled by group policy) ship without it. When absent, the launcher
# .exe LAUNCHES but opens a black/blank window with no error — confusing
# silent failure for new users.
#
# Probe the well-known registry keys first; if absent, try `winget install`
# silently, falling back to a human-readable URL hint. Runs only on Windows
# (no-op on Linux/macOS — install.sh handles those platforms and they don't
# use WebView2).
#
# How to test:
#   1. Uninstall the WebView2 Runtime: Settings > Apps > Microsoft Edge
#      WebView2 Runtime > Uninstall. (Or: winget uninstall
#      Microsoft.EdgeWebView2Runtime.)
#   2. Run install.ps1; verify it offers the winget install path (or URL
#      hint if winget is absent).
#   3. Re-run install.ps1; verify it skips the check silently (Test- returns
#      $true on the first registry key).
# ---------------------------------------------------------------------------
function Test-WebView2Installed {
    # GUID is the stable product ID for Microsoft Edge WebView2 Runtime.
    # Sources: per-machine x64 install (WOW6432Node), per-machine native,
    # and per-user install. Any one of the three counts as "installed".
    $guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    $keys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$guid",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid"
    )
    foreach ($k in $keys) {
        if (Test-Path $k) { return $true }
    }
    return $false
}

function Install-WebView2Runtime {
    Write-Host "Microsoft Edge WebView2 Runtime not detected." -ForegroundColor Yellow
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Detected winget. Will run:"
        Write-Host "  winget install Microsoft.EdgeWebView2Runtime --silent --accept-package-agreements --accept-source-agreements"
        # Skip the confirm prompt in non-interactive mode (CI / -Quiet /
        # VCT_NON_INTERACTIVE / -NonInteractive). Prompt-Yes is defined
        # later in the file so we inline a Read-Host here instead.
        $proceed = $nonInteractiveMode
        if (-not $proceed) {
            $reply = Read-Host "Install WebView2 Runtime now? [Y/n]"
            $proceed = [string]::IsNullOrWhiteSpace($reply) -or ($reply -match '^[Yy]')
        }
        if ($proceed) {
            & winget install Microsoft.EdgeWebView2Runtime --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                Write-Host "WebView2 Runtime installed successfully."
                return $true
            }
            Write-Host "winget install failed (exit $LASTEXITCODE). Falling back to URL hint." -ForegroundColor Yellow
        }
    } else {
        Write-Host "winget not found." -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "  WebView2 Runtime is REQUIRED by the launcher GUI on Windows." -ForegroundColor Yellow
    Write-Host "  Without it, the launcher opens a black/blank window."
    Write-Host "  Install manually from:"
    Write-Host "    https://developer.microsoft.com/microsoft-edge/webview2/"
    Write-Host ""
    Write-Host "  Re-run install.ps1 (or first-install.bat) after installation."
    return $false
}

if ($IsWindows -or $env:OS -eq 'Windows_NT') {
    if (-not (Test-WebView2Installed)) {
        $installed = Install-WebView2Runtime
        if (-not $installed) {
            if ($nonInteractiveMode) {
                # Non-interactive: don't block the install — WebView2 is
                # only needed for the GUI launcher, not the CLI/MCP stack.
                # The user can install it manually before first GUI launch.
                Write-Host "Continuing install (WebView2 needed only for launcher GUI; install before first GUI launch)." -ForegroundColor Yellow
            } else {
                $resp = Read-Host "Continue install without WebView2 Runtime? Launcher GUI will not work until installed. [y/N]"
                if ($resp -notmatch '^[Yy]') {
                    Write-Host "Aborting. Install WebView2 Runtime and re-run." -ForegroundColor Red
                    exit 1
                }
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Python detection
# ---------------------------------------------------------------------------
function Find-Python {
    # CROSS-LANGUAGE PARITY (v0.2.53 NEW-3): this candidate list is a MIRROR —
    # it must stay identical, in order, to the other two bootstrap Python
    # probes:
    #   * install.sh   -> find_python `for cmd in ...`
    #   * launcher/src-tauri/src/commands/installer.rs -> detect_python POSIX
    #     `else { vec![...] }` branch
    # The mirror is deliberate (C-tier, justified): these run at bootstrap on a
    # fresh machine with NO jq / interpreter / launcher available, so a shared
    # data file cannot be safely parsed here. The drift is locked by
    # tests/test_python_candidate_parity.py (extracts all three literal lists,
    # asserts sh == ps1 == rs for the POSIX list). Edit all three + keep that
    # test green when this list changes.
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
# v0.2.51 Bug G: Node.js detection + auto-install (Windows)
#
# Node 18+ is needed by Playwright MCP and the Tauri launcher build path.
# winget package ID: OpenJS.NodeJS.LTS (currently 20.x; tracks the LTS).
# ---------------------------------------------------------------------------
function Find-Node {
    # Returns the major version (int) if Node >= 18 is on PATH, $null otherwise.
    $nodeCmd = Get-Command "node" -ErrorAction SilentlyContinue
    if (-not $nodeCmd) { return $null }
    $rawVersion = & node --version 2>$null
    if (-not $rawVersion) { return $null }
    # "v20.11.1" → 20
    $trimmed = $rawVersion.TrimStart('v')
    $parts = $trimmed.Split('.')
    if ($parts.Length -lt 1) { return $null }
    try {
        $major = [int]$parts[0]
    } catch {
        return $null
    }
    if ($major -ge 18) { return @{ Major = $major; Version = $trimmed } }
    return $null
}

function Print-Node-Manual-Hint {
    Write-Host ""
    Write-Host "Install Node.js 18+ manually, then re-run install.ps1:" -ForegroundColor Yellow
    Write-Host "  winget:           winget install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements"
    Write-Host "  nodejs.org:       https://nodejs.org/"
}

function Attempt-Install-Node {
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "ERROR: winget not found." -ForegroundColor Red
        Print-Node-Manual-Hint
        return $false
    }
    Write-Host "Detected winget. Will run:"
    Write-Host "  winget install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements"
    if (-not (Prompt-Yes "Proceed?")) { return $false }

    & winget install OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: winget install failed (exit $LASTEXITCODE)." -ForegroundColor Red
        Print-Node-Manual-Hint
        return $false
    }
    return $true
}

# ---------------------------------------------------------------------------
# v0.2.51 Bug G: Podman detection + auto-install (Windows)
#
# Note: Podman on Windows requires WSL2. winget install RedHat.Podman
# DOES install the podman client but DOES NOT install/enable WSL2.
# install.py's _prompt_install_container_runtime emits a clear hint for
# the WSL2 prerequisite when only the binary is present; we just get
# the binary on PATH here.
# ---------------------------------------------------------------------------
function Find-Podman {
    return ([bool](Get-Command "podman" -ErrorAction SilentlyContinue))
}

function Find-Container-Runtime {
    if (Find-Podman) { return "podman" }
    if (Get-Command "docker" -ErrorAction SilentlyContinue) { return "docker" }
    return $null
}

function Print-Podman-Manual-Hint {
    Write-Host ""
    Write-Host "Install Podman manually, then re-run install.ps1:" -ForegroundColor Yellow
    Write-Host "  winget:           winget install RedHat.Podman --silent --accept-package-agreements --accept-source-agreements"
    Write-Host "  podman.io:        https://podman.io/getting-started/installation"
    Write-Host "  Note: Podman on Windows requires WSL2: https://learn.microsoft.com/windows/wsl/install"
}

function Attempt-Install-Podman {
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "ERROR: winget not found." -ForegroundColor Red
        Print-Podman-Manual-Hint
        return $false
    }
    Write-Host "Detected winget. Will run:"
    Write-Host "  winget install RedHat.Podman --silent --accept-package-agreements --accept-source-agreements"
    Write-Host "  Note: Podman on Windows requires WSL2. Install WSL2 first if you haven't:"
    Write-Host "        https://learn.microsoft.com/windows/wsl/install"
    if (-not (Prompt-Yes "Proceed?")) { return $false }

    & winget install RedHat.Podman --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: winget install failed (exit $LASTEXITCODE)." -ForegroundColor Red
        Print-Podman-Manual-Hint
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

# ---------------------------------------------------------------------------
# v0.2.51 Bug G: Node.js + Podman pre-flight (best-effort).
#
# install.py soft-fails when they're missing, but offering to install in
# the same session is a strict UX win. Non-interactive mode skips silently.
# ---------------------------------------------------------------------------
$nodeInfo = Find-Node
if ($nodeInfo) {
    Write-Host "Found Node.js: v$($nodeInfo.Version)"
} else {
    if ($nonInteractiveMode) {
        Write-Host "Node.js 18+ not detected (non-interactive - skipping auto-install)."
        Write-Host "  Playwright MCP + Tauri launcher build will be limited until installed."
    } else {
        Write-Host "Node.js 18+ not detected."
        if (Prompt-Yes "Install Node.js now? (Playwright MCP + launcher build need it)") {
            if (Attempt-Install-Node) {
                Write-Host "Re-checking for Node.js..."
                # winget installs may not refresh PATH for the current shell.
                $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
                            [System.Environment]::GetEnvironmentVariable("Path", "User")
                $nodeInfo = Find-Node
                if ($nodeInfo) {
                    Write-Host "Found Node.js: v$($nodeInfo.Version)"
                } else {
                    Write-Host "WARN: Node.js install appeared to succeed but `node --version` still reports < 18 or not found." -ForegroundColor Yellow
                    Write-Host "      Open a new PowerShell window; install.py will surface a deferral if needed."
                    Print-Node-Manual-Hint
                }
            } else {
                Print-Node-Manual-Hint
                Write-Host "Continuing - Node.js is non-blocking."
            }
        } else {
            Write-Host "Skipped - install.py will note missing Node.js in its summary."
        }
    }
}

$rt = Find-Container-Runtime
if ($rt) {
    Write-Host "Found container runtime: $rt"
} else {
    if ($nonInteractiveMode) {
        Write-Host "No container runtime detected (non-interactive - skipping auto-install)."
        Write-Host "  install.py will surface a prompt later in this run."
    } else {
        Write-Host "No container runtime (podman or docker) detected."
        if (Prompt-Yes "Install Podman now? (recommended over Docker - no license, native)") {
            if (Attempt-Install-Podman) {
                Write-Host "Re-checking for Podman..."
                $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
                            [System.Environment]::GetEnvironmentVariable("Path", "User")
                if (Find-Podman) {
                    Write-Host "Found podman."
                } else {
                    Write-Host "WARN: Podman install appeared to succeed but podman not on PATH." -ForegroundColor Yellow
                    Write-Host "      Open a new PowerShell window; install.py will re-probe + surface a deferral if needed."
                    Print-Podman-Manual-Hint
                }
            } else {
                Print-Podman-Manual-Hint
                Write-Host "Continuing - install.py will prompt again if no runtime is present."
            }
        } else {
            Write-Host "Skipped - install.py will prompt again later."
        }
    }
}

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
if ($NoAgents)      { $installArgs += "--no-agents" }
if ($WithMaoAgents) { $installArgs += "--with-mao-agents" }
if ($NoSkills)      { $installArgs += "--no-skills" }
if ($NoCompile)     { $installArgs += "--no-compile" }
# --yes propagation: critical for first-install.bat --yes flow on Windows.
# Without this, install.py drops to interactive prompts that hang silently
# in the .bat-spawned PowerShell window. -NonInteractive implies -Yes too
# (matches the .ps1's own non-interactive semantics on line ~95).
if ($Yes -or $NonInteractive) { $installArgs += "--yes" }

# ---- Step 1: bootstrap prepass (read-only) ----
# Probes Python/Node/Podman/Docker/GPU/RAM/OS into a JSON envelope at
# state/logs/bootstrap-prepass.json. Read-only; never blocks the install.
# Invoked alone in a separate process because --bootstrap is exclusive
# with install flags like --update.
$prepassDir = Join-Path (Get-Location) "state\logs"
if (-not (Test-Path $prepassDir)) {
    New-Item -ItemType Directory -Path $prepassDir -Force | Out-Null
}
$prepassPath = Join-Path $prepassDir "bootstrap-prepass.json"
try {
    if ($pythonArgs.Count -gt 0) {
        & $pythonCmd @pythonArgs install.py --bootstrap --json 2>$null `
            | Out-File -Encoding utf8 $prepassPath
    } else {
        & $pythonCmd install.py --bootstrap --json 2>$null `
            | Out-File -Encoding utf8 $prepassPath
    }
} catch {
    # Soft-fail: any prepass error is non-blocking.
}

# ---- Step 2: full install ----
if ($pythonArgs.Count -gt 0) {
    & $pythonCmd @pythonArgs install.py @installArgs
} else {
    & $pythonCmd install.py @installArgs
}
$installExitCode = $LASTEXITCODE

if ($installExitCode -ne 0) {
    Write-Host ""
    Write-Host "Installation failed. See errors above." -ForegroundColor Red
    exit $installExitCode
}

# ---- Step 3: launcher post-install (auto-spawn) ----
# Parity with Linux/macOS first-install.{sh,command}. Honors the
# -NoAutoLaunch param (declared in the script's param block).
# Soft-fail: a broken launcher spawn must NOT mask a successful install.
if (-not $NoAutoLaunch) {
    $postInstallScript = Join-Path (Get-Location) "scripts\post-install-launcher.ps1"
    if (Test-Path $postInstallScript) {
        try {
            & $postInstallScript -RepoRoot (Get-Location).Path
        } catch {
            Write-Host "post-install-launcher.ps1 failed (non-fatal): $_" `
                -ForegroundColor Yellow
        }
    }
}

exit 0
