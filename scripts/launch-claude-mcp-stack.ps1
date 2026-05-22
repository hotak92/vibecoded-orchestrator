#!/usr/bin/env pwsh
# launch-claude-mcp-stack.ps1 — boot-safe compose-up for the Claude MCP stack.
#
# Windows sibling of scripts/launch-claude-mcp-stack.sh (v0.2.9 Bug J +
# PR-12 Bug A/B + PR-22 PR-32 v0.2.10/.11/.12 fixes).
#
# v0.2.14 Bug #2 (2026-05-17): cross-OS wrapper materialization. Before
# this file shipped, Windows installs hit two failure modes:
#
#   1. launcher/src-tauri/src/commands/lifecycle.rs::find_stack_wrapper
#      probes for launch-claude-mcp-stack.ps1 on Windows. The file did
#      not exist, so the launcher silently fell back to direct compose
#      calls — losing the CDI-wait, runtime resolution, daemon-access
#      checks, and override-file logic that the .sh wrapper provides.
#
#   2. templates/windows/claude-mcp-containers.task.xml.template invoked
#      `bash scripts/launch-claude-mcp-stack.sh` via cmd.exe. This
#      required Git Bash or WSL on PATH; install-time logged a soft
#      warning when neither was present, but the Scheduled Task itself
#      then failed silently on every logon.
#
# This file ports the bash wrapper's logic to PowerShell 5.1+ (cross-
# compatible with PowerShell Core 7+). Functional parity matrix:
#
#   bash function              | this file
#   ---------------------------|------------------------------------------
#   log()                      | Write-StackLog
#   resolve_runtime_file()     | Resolve-RuntimeFile
#   _runtime_usable()          | Test-RuntimeUsable
#   detect_runtime()           | Find-Runtime
#   has_nvidia()               | Test-HasNvidia
#   wait_for_cdi()             | Wait-ForCdi (Windows: no-op; see note)
#   overlay_exists()           | Test-OverlayExists
#   pick_compose_invocation()  | Get-ComposeInvocation
#   main()                     | Invoke-Main
#
# GPU on Windows: Docker Desktop and Podman both run via WSL2; GPU
# passthrough requires the host-side NVIDIA-WSL2 driver and Docker
# Desktop >= 4.16. The bash wrapper's nvidia-smi + /var/run/cdi/nvidia.yaml
# probes are Linux-host-only. This file's Test-HasNvidia returns $false
# on Windows by default (the WSL distro is where compose actually runs,
# not the Windows host) — the user falls through to CPU-only compose,
# which is the safe default. A future revision could shell into WSL2
# to probe; out of scope for v0.2.14.
#
# Exit codes (mirror the bash wrapper):
#   0   success (or compose returned 125 → some containers failed,
#       restart policy will recover)
#   2   FATAL: working directory does not exist / cd failed
#   3   FATAL: no container runtime found
#   4   FATAL: pick_compose_invocation rejected runtime/gpu combo
#   *   compose's own non-zero exit code is propagated as-is
#
# Soft-fail discipline: every non-fatal failure path logs + falls
# through to a graceful default (mirrors `set +e` semantics in bash).
#
# Compat: targets Windows PowerShell 5.1 (shipped in every Win10+) AND
# PowerShell Core 7+. No PS Core-only cmdlets, no PS 6+ syntax. Uses
# full cmdlet names (no aliases) for readability.

# Set strict mode for safety. v3 (not Latest) so PS 5.1 doesn't reject
# uninitialized-variable references inside string expansion — there are
# legitimate cases (e.g. `$env:VCT_FOO` reads) where the variable may
# not exist. We catch those via Test-Path / null comparisons.
Set-StrictMode -Version 3.0

# Soft-fail by default (mirrors `set +u` in bash sense and `set +e`).
# Individual probe calls override this locally with try/catch.
$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------------------
# Configuration. Override via env if you need to:
#   - VCT_STACK_WORKING_DIR   — directory containing compose.yaml
#   - VCT_STACK_LOG_FILE      — log path (default: $env:TEMP\claude-mcp-containers.log)
#   - VCT_STACK_CDI_TIMEOUT   — seconds to wait for CDI yaml (default: 30,
#                                 unused on Windows where CDI is WSL-internal)
#   - VCT_STACK_RUNTIME_FILE  — explicit runtime.txt path. When set, wins
#                                 over candidate-path search.
#   - VCT_ORCHESTRATOR_ROOT   — orchestrator install root (used as one of
#                                 the runtime.txt candidate-path roots).
#   - VCT_STACK_GPU_OVERLAY   — overlay filename for podman path
#                                 (default: infrastructure/podman-compose.gpu.yml)
#   - VCT_STACK_GPU_OVERLAY_DOCKER — overlay for docker path
#                                 (default: infrastructure/docker-compose.gpu.yml)
#   - VCT_STACK_COMPOSE_OVERRIDE  — user-machine compose override
#                                 (default: compose.override.yaml). Resolved
#                                 relative to VCT_STACK_WORKING_DIR; auto-
#                                 applied iff the file exists and is non-empty.
#   - VCT_CONTAINER_RUNTIME   — explicit runtime preference (docker|podman),
#                                 highest-priority override above runtime.txt.
# ---------------------------------------------------------------------------

function Get-EnvOrDefault {
    param(
        [Parameter(Mandatory=$true)][string] $Name,
        [Parameter(Mandatory=$true)][string] $Default
    )
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { return $Default }
    return $value
}

# Default working dir: matches the bash default's semantic on Windows.
# Bash uses ${HOME}/Desktop/PROGETTI/Claude/claude_mcp_servers; on Windows
# $env:USERPROFILE is the rough HOME equivalent. The systemd unit / Task
# XML always sets VCT_STACK_WORKING_DIR explicitly so this default rarely
# fires.
$script:DefaultWorkingDir = Join-Path $env:USERPROFILE "Desktop\PROGETTI\Claude\claude_mcp_servers"
$script:VctStackWorkingDir       = Get-EnvOrDefault 'VCT_STACK_WORKING_DIR' $script:DefaultWorkingDir
$script:VctStackLogFile          = Get-EnvOrDefault 'VCT_STACK_LOG_FILE' (Join-Path $env:TEMP 'claude-mcp-containers.log')
$script:VctStackCdiTimeout       = [int](Get-EnvOrDefault 'VCT_STACK_CDI_TIMEOUT' '30')
$script:VctStackGpuOverlay       = Get-EnvOrDefault 'VCT_STACK_GPU_OVERLAY' 'infrastructure/podman-compose.gpu.yml'
$script:VctStackGpuOverlayDocker = Get-EnvOrDefault 'VCT_STACK_GPU_OVERLAY_DOCKER' 'infrastructure/docker-compose.gpu.yml'
$script:VctStackComposeFile      = Get-EnvOrDefault 'VCT_STACK_COMPOSE_FILE' 'compose.yaml'
$script:VctStackComposeOverride  = Get-EnvOrDefault 'VCT_STACK_COMPOSE_OVERRIDE' 'compose.override.yaml'

# Resolve the directory that contains THIS script — used as one fallback
# root for runtime.txt resolution. $PSScriptRoot is empty when the script
# is dot-sourced from an interactive shell; fall back to $MyInvocation.
$script:VctScriptDir = $PSScriptRoot
if ([string]::IsNullOrEmpty($script:VctScriptDir)) {
    if ($MyInvocation.MyCommand.Path) {
        $script:VctScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    } else {
        $script:VctScriptDir = ''
    }
}

# Test flag — when set, suppress Invoke-Main when the script is dot-sourced
# AND not invoked directly. Mirrors `if [ "${BASH_SOURCE[0]}" = "${0}" ]`.
$script:VctScriptInvokedDirectly = $false

# ---------------------------------------------------------------------------
# Write-StackLog :: append a timestamped line to the log file. Best-effort;
# never errors. Mirrors bash log().
#
# Output also goes to stderr (Write-Host on the Error stream) so the
# Scheduled Task captures it in the Last Run details and Get-WinEvent
# Microsoft-Windows-TaskScheduler/Operational picks it up.
# ---------------------------------------------------------------------------
function Write-StackLog {
    param([Parameter(Mandatory=$true, ValueFromRemainingArguments=$true)][string[]] $Message)
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = "$ts [launch-claude-mcp-stack] $($Message -join ' ')"
    # Best-effort file write; suppress all errors (matches bash `|| true`).
    try {
        $logDir = Split-Path -Parent $script:VctStackLogFile
        if ($logDir -and -not (Test-Path -LiteralPath $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        Add-Content -LiteralPath $script:VctStackLogFile -Value $line -ErrorAction SilentlyContinue
    } catch {
        # Swallow — log is best-effort.
    }
    # Always emit to host stream so callers see it even when the log file
    # is unwritable. Use Write-Information for cleanliness on PS Core, but
    # fall back to Write-Output for PS 5.1.
    [Console]::Out.WriteLine($line)
}

# ---------------------------------------------------------------------------
# Test-CommandExists :: PowerShell equivalent of `command -v <cmd>`.
# Returns $true iff the named command resolves to an executable on PATH.
# ---------------------------------------------------------------------------
function Test-CommandExists {
    param([Parameter(Mandatory=$true)][string] $Name)
    $cmd = Get-Command -Name $Name -ErrorAction SilentlyContinue
    return $null -ne $cmd
}

# ---------------------------------------------------------------------------
# Invoke-WithTimeout :: run a script block and kill it after $TimeoutSec.
# Returns the script block's stdout (collected as string) and the exit
# code via the $ExitCode parameter (by reference). Mirrors `timeout 5 cmd`.
#
# We use Start-Process + WaitForExit instead of jobs because jobs add
# ~500ms per-invocation overhead in PS 5.1 (significant when probing
# multiple runtimes back-to-back).
# ---------------------------------------------------------------------------
function Invoke-WithTimeout {
    param(
        [Parameter(Mandatory=$true)][string] $Executable,
        [Parameter(Mandatory=$true)][string[]] $ArgumentList,
        [Parameter(Mandatory=$false)][int] $TimeoutSec = 5
    )
    # Returns hashtable @{ ExitCode = N; StdOut = "..."; StdErr = "..."; TimedOut = $bool }
    $result = @{ ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $false }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Executable
        # PS 5.1's ProcessStartInfo does not expose ArgumentList (added
        # in .NET Core 2.1+). We always build the Arguments string by
        # quoting each arg that contains whitespace. This is the lowest
        # common denominator that works on both PS 5.1 and PS Core 7+.
        $quoted = $ArgumentList | ForEach-Object {
            if ($_ -match '\s') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }
        $psi.Arguments = ($quoted -join ' ')
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
            try { $proc.Kill() } catch { }
            $result.TimedOut = $true
            return $result
        }
        $result.ExitCode = $proc.ExitCode
        $result.StdOut = $proc.StandardOutput.ReadToEnd()
        $result.StdErr = $proc.StandardError.ReadToEnd()
        return $result
    } catch {
        return $result
    }
}

# ---------------------------------------------------------------------------
# Test-RuntimeUsable :: given a runtime token (lowercase: "docker" or
# "podman"), return $true iff the runtime is actually usable on this host
# (binary exists AND its daemon / WSL backend is reachable).
#
# Mirrors bash _runtime_usable. Detection rules:
#   docker → `docker info` exit 0 AND output contains a "Server:" or
#            "Server Version:" line. The Client section appears even
#            without daemon access; the Server section requires reach.
#   podman → `podman info` exit 0 (rootless / podman-machine includes
#            its own probes; podman info exits non-zero if the storage
#            backend isn't initialized).
#   anything else → not usable.
#
# Both probes carry a 5s timeout — a hung daemon socket must NOT block
# Task Scheduler indefinitely.
# ---------------------------------------------------------------------------
function Test-RuntimeUsable {
    param([Parameter(Mandatory=$true)][string] $Token)
    switch ($Token.ToLowerInvariant()) {
        'docker' {
            if (-not (Test-CommandExists 'docker')) { return $false }
            $result = Invoke-WithTimeout -Executable 'docker' -ArgumentList @('info') -TimeoutSec 5
            if ($result.TimedOut -or $result.ExitCode -ne 0) { return $false }
            # Server: section presence is the daemon-access proxy.
            if ($result.StdOut -match '(?m)^(Server:|Server Version:)') {
                return $true
            }
            return $false
        }
        'podman' {
            if (-not (Test-CommandExists 'podman')) { return $false }
            $result = Invoke-WithTimeout -Executable 'podman' -ArgumentList @('info') -TimeoutSec 5
            if ($result.TimedOut -or $result.ExitCode -ne 0) { return $false }
            return $true
        }
        default { return $false }
    }
}

# ---------------------------------------------------------------------------
# Resolve-RuntimeFile :: returns the path to the FIRST runtime.txt
# candidate that exists on disk and contains a usable runtime token.
# Empty string if none usable. Mirrors bash resolve_runtime_file.
#
# Probe order (first hit wins):
#   1. $env:VCT_STACK_RUNTIME_FILE if explicitly set (caller override)
#   2. $VctStackWorkingDir\state\install\runtime.txt
#   3. $env:VCT_ORCHESTRATOR_ROOT\state\install\runtime.txt
#   4. <script_dir>\..\state\install\runtime.txt   (script lives in
#      <orchestrator>\scripts\, so .. is the orchestrator root)
# ---------------------------------------------------------------------------
function Resolve-RuntimeFile {
    $candidates = New-Object System.Collections.Generic.List[string]
    $explicit = [Environment]::GetEnvironmentVariable('VCT_STACK_RUNTIME_FILE')
    if (-not [string]::IsNullOrEmpty($explicit)) {
        [void] $candidates.Add($explicit)
    }
    if (-not [string]::IsNullOrEmpty($script:VctStackWorkingDir)) {
        [void] $candidates.Add((Join-Path $script:VctStackWorkingDir 'state\install\runtime.txt'))
    }
    $orchRoot = [Environment]::GetEnvironmentVariable('VCT_ORCHESTRATOR_ROOT')
    if (-not [string]::IsNullOrEmpty($orchRoot)) {
        [void] $candidates.Add((Join-Path $orchRoot 'state\install\runtime.txt'))
    }
    if (-not [string]::IsNullOrEmpty($script:VctScriptDir)) {
        $rel = Join-Path $script:VctScriptDir '..\state\install\runtime.txt'
        # Normalise the .. so dedup works correctly.
        try {
            $rel = [System.IO.Path]::GetFullPath($rel)
        } catch {
            # Path normalisation can fail on non-existent intermediate
            # dirs in PS 5.1; tolerate by keeping the raw path.
        }
        [void] $candidates.Add($rel)
    }

    $seen = ''
    foreach ($cand in $candidates) {
        # De-dup adjacent identical candidates (common when env vars
        # collapse to the same path on default installs).
        if ($cand -eq $seen) { continue }
        $seen = $cand
        if (-not (Test-Path -LiteralPath $cand -PathType Leaf)) { continue }
        $token = ''
        try {
            $line = Get-Content -LiteralPath $cand -TotalCount 1 -ErrorAction SilentlyContinue
            if ($null -ne $line) {
                $token = ($line.Trim()).ToLowerInvariant()
            }
        } catch {
            continue
        }
        if ([string]::IsNullOrEmpty($token)) { continue }
        if (Test-RuntimeUsable -Token $token) {
            return $cand
        } else {
            Write-StackLog "runtime.txt at $cand names '$token' but its daemon is not reachable — falling through to live probe"
        }
    }
    return ''
}

# ---------------------------------------------------------------------------
# Find-Runtime :: returns one of "docker", "podman-compose", "podman compose"
# or "". Mirrors bash detect_runtime.
#
# Order:
#   1. VCT_CONTAINER_RUNTIME env preference (v0.2.14 Bug #3 pattern) —
#      explicit user override, only honored if the named runtime is
#      actually usable.
#   2. resolve_runtime_file → token from runtime.txt → expand to
#      compose invocation IFF the runtime is usable.
#   3. Probe podman first (preferred default — no group-perm gotcha
#      on Windows since podman-machine runs in WSL2 with the user's
#      own subordinate uids).
#   4. Probe docker.
#   5. Empty (no usable runtime).
# ---------------------------------------------------------------------------
function Find-Runtime {
    # 1. Explicit env preference. VCT_CONTAINER_RUNTIME is the highest-
    # priority signal — v0.2.14 Bug #3 added this check ahead of the
    # runtime.txt probe to give users a way to force a specific runtime
    # at boot without editing on-disk state.
    $envPref = [Environment]::GetEnvironmentVariable('VCT_CONTAINER_RUNTIME')
    if (-not [string]::IsNullOrEmpty($envPref)) {
        $envPref = $envPref.Trim().ToLowerInvariant()
        if ($envPref -eq 'docker' -and (Test-RuntimeUsable -Token 'docker')) {
            return 'docker'
        }
        if ($envPref -eq 'podman' -and (Test-RuntimeUsable -Token 'podman')) {
            if (Test-CommandExists 'podman-compose') { return 'podman-compose' }
            $help = Invoke-WithTimeout -Executable 'podman' -ArgumentList @('compose','--help') -TimeoutSec 3
            if (-not $help.TimedOut -and $help.ExitCode -eq 0) { return 'podman compose' }
        }
        if ($envPref -ne '' -and -not @('docker','podman').Contains($envPref)) {
            Write-StackLog "VCT_CONTAINER_RUNTIME='$envPref' is not a recognized runtime token; ignoring"
        } else {
            Write-StackLog "VCT_CONTAINER_RUNTIME='$envPref' but that runtime is not usable; falling through"
        }
    }

    # 2. runtime.txt — only honored if its named runtime is actually usable.
    $runtimeFile = Resolve-RuntimeFile
    if (-not [string]::IsNullOrEmpty($runtimeFile)) {
        $persisted = ''
        try {
            $line = Get-Content -LiteralPath $runtimeFile -TotalCount 1 -ErrorAction SilentlyContinue
            if ($null -ne $line) {
                $persisted = ($line.Trim()).ToLowerInvariant()
            }
        } catch { }
        switch ($persisted) {
            'docker' {
                # Test-RuntimeUsable already validated docker daemon access
                # in Resolve-RuntimeFile; we trust that result here.
                return 'docker'
            }
            'podman' {
                if (Test-CommandExists 'podman-compose') { return 'podman-compose' }
                $help = Invoke-WithTimeout -Executable 'podman' -ArgumentList @('compose','--help') -TimeoutSec 3
                if (-not $help.TimedOut -and $help.ExitCode -eq 0) { return 'podman compose' }
                # podman is usable but no compose front-end — fall through.
            }
        }
    }

    # 3. Probe podman first — preferred default.
    if (Test-RuntimeUsable -Token 'podman') {
        if (Test-CommandExists 'podman-compose') { return 'podman-compose' }
        $help = Invoke-WithTimeout -Executable 'podman' -ArgumentList @('compose','--help') -TimeoutSec 3
        if (-not $help.TimedOut -and $help.ExitCode -eq 0) { return 'podman compose' }
        Write-StackLog "podman daemon is reachable but neither 'podman-compose' nor 'podman compose' is available — falling through to docker"
    }

    # 4. Probe docker.
    if (Test-RuntimeUsable -Token 'docker') {
        return 'docker'
    }

    # 5. No usable runtime.
    return ''
}

# ---------------------------------------------------------------------------
# Test-HasNvidia :: return $true iff nvidia-smi reports at least one GPU
# on the Windows HOST. Note: this is the host-side probe — the WSL2 distro
# where containers actually run has its own nvidia-smi exposed via the
# NVIDIA-WSL2 driver, but we don't probe inside WSL from here (that would
# require knowing the distro name + invoking `wsl.exe -d <distro> ...`,
# both of which can vary per-machine).
#
# Pragmatic Windows policy: default to $false (CPU-only compose) unless
# nvidia-smi is on the Windows PATH AND reports a GPU. This matches what
# the bash wrapper would do on a non-Linux host (its uname check falls
# through to CPU-mode).
# ---------------------------------------------------------------------------
function Test-HasNvidia {
    if (-not (Test-CommandExists 'nvidia-smi')) { return $false }
    $result = Invoke-WithTimeout -Executable 'nvidia-smi' -ArgumentList @('-L') -TimeoutSec 2
    if ($result.TimedOut -or $result.ExitCode -ne 0) { return $false }
    if ($result.StdOut -match '(?m)^GPU \d+:') { return $true }
    return $false
}

# ---------------------------------------------------------------------------
# Wait-ForCdi :: on Linux, polls /var/run/cdi/nvidia.yaml. On Windows,
# CDI is internal to the WSL2 distro — there is no Windows-host CDI
# file to poll. We treat this as immediately-ready (return $true) when
# Test-HasNvidia was true; the actual CDI handoff happens inside WSL
# before the container starts, and Docker Desktop's GPU runtime hook
# handles its own readiness check. Mirrors the bash wrapper's
# "docker runtime: skipping CDI wait" branch.
# ---------------------------------------------------------------------------
function Wait-ForCdi {
    # Windows-host CDI does not exist. The bash wrapper's loop polls
    # /var/run/cdi/nvidia.yaml; the analogous file would live inside the
    # WSL2 distro at the same path. We don't reach into WSL here — the
    # container runtime (Docker Desktop / podman-machine) handles its
    # own CDI readiness when it boots its WSL distro at host login.
    return $true
}

# ---------------------------------------------------------------------------
# Test-OverlayExists :: pure helper, returns $true iff the argument is a
# non-empty existing regular file. Mirrors bash overlay_exists.
# ---------------------------------------------------------------------------
function Test-OverlayExists {
    param([Parameter(Mandatory=$false)][string] $Path)
    if ([string]::IsNullOrEmpty($Path)) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        return $item.Length -gt 0
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# Get-ComposeInvocation :: given (runtime, gpu_mode, [working_dir]),
# return a hashtable with the argv that should be invoked.
#
# Mirrors bash pick_compose_invocation. Output format:
#   @{
#       Command = 'docker' | 'podman-compose' | 'podman' ;
#       Args = @('compose', '-f', 'compose.yaml', ...) ;
#       OverlayMissingWarned = $true|$false ;
#       Ok = $true ;
#       ErrorCode = 0
#   }
# Or on failure:
#   @{ Ok = $false ; ErrorCode = 1|2 }
#
# ErrorCode 1: empty runtime
# ErrorCode 2: unknown runtime token
#
# Side-channel `OverlayMissingWarned` is set when gpu_mode=gpu but the
# overlay file doesn't exist on disk (matches bash's OVERLAY_MISSING_WARNED
# global flag — surfaced as a structured field instead of a script-scoped
# variable so tests can assert on it cleanly).
# ---------------------------------------------------------------------------
function Get-ComposeInvocation {
    param(
        # AllowEmptyString lets the soft-fail branch (`'' -> ErrorCode=1`)
        # below execute when callers pass "" explicitly. Without this
        # attribute, PowerShell's mandatory-param validation rejects the
        # empty string BEFORE the function body runs — non-interactive
        # invocations then receive $null back and `exit $null` collapses
        # to exit 0, masking the soft-fail. The bash sibling has no such
        # quirk because bash treats "" as a present-but-empty positional
        # parameter naturally. v0.2.15 fix.
        [Parameter(Mandatory=$true)][AllowEmptyString()][string] $Runtime,
        [Parameter(Mandatory=$true)][AllowEmptyString()][string] $GpuMode,
        [Parameter(Mandatory=$false)][string] $WorkingDir = ''
    )
    if ([string]::IsNullOrEmpty($WorkingDir)) { $WorkingDir = (Get-Location).Path }

    # Pick the right overlay filename per runtime.
    $overlay = ''
    switch ($Runtime) {
        'docker'           { $overlay = $script:VctStackGpuOverlayDocker }
        'podman-compose'   { $overlay = $script:VctStackGpuOverlay }
        'podman compose'   { $overlay = $script:VctStackGpuOverlay }
        ''                 { return @{ Ok = $false; ErrorCode = 1 } }
        default            { return @{ Ok = $false; ErrorCode = 2 } }
    }

    # Resolve overlay path against working_dir for existence-check, but
    # emit the path EXACTLY as configured (so log output and the on-disk
    # check share semantics with the bash version).
    $resolvedOverlay = ''
    if (-not [string]::IsNullOrEmpty($overlay)) {
        if ([System.IO.Path]::IsPathRooted($overlay)) {
            $resolvedOverlay = $overlay
        } else {
            $resolvedOverlay = Join-Path $WorkingDir $overlay
        }
    }

    # The "use the overlay flag" decision is the AND of:
    #   - gpu_mode is "gpu"
    #   - an overlay path was configured for this runtime
    #   - that overlay actually exists on disk
    $useOverlay = $false
    $overlayMissingWarned = $false
    if ($GpuMode -eq 'gpu' -and -not [string]::IsNullOrEmpty($overlay)) {
        if (Test-OverlayExists -Path $resolvedOverlay) {
            $useOverlay = $true
        } else {
            $overlayMissingWarned = $true
        }
    }

    # PR-22 (2026-05-16): user-machine compose override. Resolved relative
    # to working_dir when not absolute. Auto-applied iff the file exists
    # and is non-empty.
    $useOverride = $false
    if (-not [string]::IsNullOrEmpty($script:VctStackComposeOverride)) {
        if ([System.IO.Path]::IsPathRooted($script:VctStackComposeOverride)) {
            $resolvedOverride = $script:VctStackComposeOverride
        } else {
            $resolvedOverride = Join-Path $WorkingDir $script:VctStackComposeOverride
        }
        if (Test-Path -LiteralPath $resolvedOverride -PathType Leaf) {
            try {
                $item = Get-Item -LiteralPath $resolvedOverride -ErrorAction Stop
                if ($item.Length -gt 0) { $useOverride = $true }
            } catch { }
        }
    }

    # Build the argv. Order matters:
    #   1. -f compose.yaml          (base)
    #   2. -f <gpu-overlay>         (GPU additions, when applicable)
    #   3. -f <user-override>       (LAST so it wins on conflicts)
    $args = New-Object System.Collections.Generic.List[string]
    $command = ''
    $extraPrefix = @()
    switch ($Runtime) {
        'docker' {
            $command = 'docker'
            $extraPrefix = @('compose')
        }
        'podman-compose' {
            $command = 'podman-compose'
            $extraPrefix = @()
        }
        'podman compose' {
            $command = 'podman'
            $extraPrefix = @('compose')
        }
    }
    foreach ($p in $extraPrefix) { [void] $args.Add($p) }
    [void] $args.Add('-f')
    [void] $args.Add($script:VctStackComposeFile)
    if ($useOverlay) {
        [void] $args.Add('-f')
        [void] $args.Add($overlay)
    }
    if ($useOverride) {
        [void] $args.Add('-f')
        [void] $args.Add($script:VctStackComposeOverride)
    }

    return @{
        Ok                   = $true
        ErrorCode            = 0
        Command              = $command
        Args                 = $args.ToArray()
        OverlayMissingWarned = $overlayMissingWarned
    }
}

# ---------------------------------------------------------------------------
# Format-Invocation :: render the Get-ComposeInvocation hashtable back to
# a space-separated string for log output (mirrors the bash wrapper's
# `log "exec: $argv up -d"` line and the test assertions in
# tests/test_launch_claude_mcp_stack_pick.py which compare against the
# space-joined string form).
# ---------------------------------------------------------------------------
function Format-Invocation {
    param([Parameter(Mandatory=$true)][hashtable] $Invocation)
    if (-not $Invocation.Ok) { return '' }
    # Note: do NOT shell-quote here. Bash's pick_compose_invocation also
    # emits a raw space-joined string; the tests compare against that
    # literal. Quoting would be added by the actual launch (Start-Process
    # / .NET ProcessStartInfo).
    $parts = @($Invocation.Command) + $Invocation.Args
    return ($parts -join ' ')
}

# ---------------------------------------------------------------------------
# Invoke-Main :: orchestrate the boot-safe compose-up. Mirrors bash main().
#
# Returns the exit code the script should propagate.
# ---------------------------------------------------------------------------
function Invoke-Main {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]] $RemainingArgs)
    # The bash main() ignores its arguments and always does `up -d`.
    # The launcher (lifecycle.rs::run_stack_wrapper) passes `start`/`stop`/
    # `restart` subcommands; we accept them silently for forward-compat
    # but always perform compose `up -d`. Log the received subcommand so
    # operators can see what the launcher requested.
    $subcmd = if ($RemainingArgs -and $RemainingArgs.Count -gt 0) { $RemainingArgs[0] } else { '' }

    Write-StackLog "starting (working_dir=$($script:VctStackWorkingDir) cdi_timeout=$($script:VctStackCdiTimeout) subcommand='$subcmd')"

    if (-not (Test-Path -LiteralPath $script:VctStackWorkingDir -PathType Container)) {
        Write-StackLog "FATAL: working directory does not exist: $($script:VctStackWorkingDir)"
        return 2
    }
    try {
        Push-Location -LiteralPath $script:VctStackWorkingDir -ErrorAction Stop
    } catch {
        Write-StackLog "FATAL: cd $($script:VctStackWorkingDir) failed: $_"
        return 2
    }
    try {
        $runtime = Find-Runtime
        if ([string]::IsNullOrEmpty($runtime)) {
            Write-StackLog "FATAL: no container runtime found (tried VCT_CONTAINER_RUNTIME, runtime.txt, podman, docker)"
            return 3
        }
        Write-StackLog "runtime=$runtime"

        $gpuMode = 'cpu'
        # Mirrors bash `case "$(uname -s)" in Linux) ... esac`. PowerShell
        # is primarily a Windows-host story here; on Windows we delegate
        # GPU readiness to the container runtime (Docker Desktop /
        # podman-machine WSL2 distro). Non-Linux hosts default to CPU.
        $isWindows = [System.Environment]::OSVersion.Platform.ToString().StartsWith('Win')
        if ($isWindows) {
            if (Test-HasNvidia) {
                Write-StackLog "nvidia detected on Windows host; relying on $runtime's WSL2/GPU runtime hook (no Windows-host CDI poll)"
                $gpuMode = 'gpu'
            } else {
                Write-StackLog "no NVIDIA GPU detected (nvidia-smi absent on Windows host) — CPU-only compose"
                $gpuMode = 'cpu'
            }
        } else {
            # Cross-OS pwsh on Linux/macOS. Mirror the bash wrapper's
            # behavior as closely as we can without re-implementing
            # /var/run/cdi/nvidia.yaml parsing in PowerShell.
            if (Test-HasNvidia) {
                if ($runtime -eq 'docker') {
                    Write-StackLog "docker runtime: skipping CDI wait (docker uses runtime hook)"
                    $gpuMode = 'gpu'
                } else {
                    # podman on Linux: would normally Wait-ForCdi, but the
                    # PS port doesn't have a fully-faithful poller. Default
                    # conservative: log + CPU-only.
                    Write-StackLog "WARNING: NVIDIA detected on non-Windows host but PowerShell wrapper does not poll /var/run/cdi/nvidia.yaml — degrading to CPU-only. Use the .sh wrapper on Linux for full CDI handling."
                    $gpuMode = 'cpu'
                }
            } else {
                Write-StackLog "no NVIDIA GPU detected — CPU-only compose"
                $gpuMode = 'cpu'
            }
        }

        $invocation = Get-ComposeInvocation -Runtime $runtime -GpuMode $gpuMode -WorkingDir $script:VctStackWorkingDir
        if (-not $invocation.Ok) {
            Write-StackLog "FATAL: Get-ComposeInvocation rejected runtime=$runtime gpu_mode=$gpuMode (error_code=$($invocation.ErrorCode))"
            return 4
        }
        if ($invocation.OverlayMissingWarned) {
            $missing = if ($runtime -eq 'docker') { $script:VctStackGpuOverlayDocker } else { $script:VctStackGpuOverlay }
            Write-StackLog "WARNING: inline-GPU compose assumed — overlay file '$(Join-Path $script:VctStackWorkingDir $missing)' not found, proceeding without overlay"
        }
        $argvString = Format-Invocation -Invocation $invocation
        Write-StackLog "exec: $argvString up -d"

        # Actually invoke compose. We append `up -d` to the args array.
        $finalArgs = @($invocation.Args) + @('up', '-d')
        # Use Start-Process / .NET ProcessStartInfo so we can capture the
        # real exit code (`&` in PS does not always preserve it cleanly
        # for native-cmd outputs containing newlines).
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $invocation.Command
        # See Invoke-WithTimeout: PS 5.1 needs the legacy Arguments
        # string; we quote whitespace-bearing tokens by hand.
        $quoted = $finalArgs | ForEach-Object {
            if ($_ -match '\s') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }
        $psi.Arguments = ($quoted -join ' ')
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WorkingDirectory = $script:VctStackWorkingDir
        $rc = 0
        try {
            $proc = [System.Diagnostics.Process]::Start($psi)
            # Stream stdout/stderr to our log + console live-ish. We
            # ReadToEnd at the end to avoid pipe-fill deadlocks on
            # chatty compose output.
            $proc.WaitForExit()
            $stdout = $proc.StandardOutput.ReadToEnd()
            $stderr = $proc.StandardError.ReadToEnd()
            $rc = $proc.ExitCode
            if (-not [string]::IsNullOrEmpty($stdout)) {
                foreach ($line in $stdout -split "`n") {
                    if ($line -ne '') { Write-StackLog "compose-stdout: $($line.TrimEnd())" }
                }
            }
            if (-not [string]::IsNullOrEmpty($stderr)) {
                foreach ($line in $stderr -split "`n") {
                    if ($line -ne '') { Write-StackLog "compose-stderr: $($line.TrimEnd())" }
                }
            }
        } catch {
            Write-StackLog "FATAL: failed to spawn $($invocation.Command): $_"
            return 5
        }

        Write-StackLog "compose exited rc=$rc"
        # Exit 125 from podman-compose means "one or more containers
        # failed to start" — tolerated at the unit level.
        if ($rc -eq 0 -or $rc -eq 125) { return 0 }
        return $rc
    } finally {
        Pop-Location -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Entrypoint guard. Mirrors bash `if [ "${BASH_SOURCE[0]}" = "${0}" ]`.
#
# In PowerShell, the analog is comparing $MyInvocation.InvocationName
# / $MyInvocation.MyCommand.Path to the entry script. When the file is
# dot-sourced (`. .\launch-claude-mcp-stack.ps1`) the InvocationName is
# `.` and we skip Invoke-Main. When run directly (`powershell -File
# .\launch-claude-mcp-stack.ps1` or `pwsh -File ...`), InvocationName
# is the file path and we call Invoke-Main with the user's args.
#
# $MyInvocation.Line on direct invocation contains the powershell.exe
# command line; on dot-source it contains the dot-source command.
# ---------------------------------------------------------------------------
if ($MyInvocation.InvocationName -ne '.') {
    # Direct invocation. Forward $args to Invoke-Main; capture its
    # return value as the script's exit code.
    $script:VctScriptInvokedDirectly = $true
    $exitCode = Invoke-Main @args
    if ($null -eq $exitCode) { $exitCode = 0 }
    exit $exitCode
}
