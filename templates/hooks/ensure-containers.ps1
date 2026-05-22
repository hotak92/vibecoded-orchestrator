# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Scrub sensitive env vars before any subprocess
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# ensure-containers.ps1
# Ensure all required containers are running (background, non-blocking).
# Mirror of ensure-containers.sh.
#
# Compose-dir resolution order (PR-2 portability fix 2026-05-06):
#   1. $env:VCT_COMPOSE_DIR              — explicit override
#   2. $env:VCT_INFRASTRUCTURE_DIR       — orchestrator clone's infrastructure/
#   3. $env:VCT_ORCHESTRATOR_ROOT\infrastructure   — env-resolved orch root
#   4. <project>\infrastructure          — bundled compose copy (per-project)
#   5. <project>\claude_mcp_servers      — orchestrator clone fallback (legacy)
# Container names come from the shared `_lib\container-names.ps1` registry
# so the hook and the bundled docker-compose.yml cannot disagree.
#
# Zombie-recovery (PR-13, v0.2.11, 2026-05-16):
#   After OOM events, podman containers may report State.Status=running
#   with State.Pid=<dead pid>. The conmon monitor was killed alongside the
#   container, so nobody triggered runc cleanup; the container exists in
#   podman's DB but its PID does not exist. `podman restart` then fails
#   with "container with given ID already exists: OCI runtime error".
#   We probe State.Pid via Get-Process; if dead, run `runc delete --force`
#   then `podman rm --force`, then re-bring-up via the GPU-safe wrapper or
#   compose. Each recovery attempt is appended to
#   $env:LOCALAPPDATA\vct\container-recovery.jsonl for audit.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ScriptDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

# Source canonical container-name registry. Supplies $VcoRequiredContainers.
$LibFile = Join-Path $ScriptDir "_lib\container-names.ps1"
if (Test-Path $LibFile) {
    . $LibFile
} else {
    # Fallback if _lib is missing (very old install pre-PR-2). Mirror the
    # current canonical defaults; users can still override via
    # VCT_REQUIRED_CONTAINERS.
    if ($env:VCT_REQUIRED_CONTAINERS) {
        $VcoRequiredContainers = $env:VCT_REQUIRED_CONTAINERS -split '\s+' | Where-Object { $_ }
    } else {
        $VcoRequiredContainers = @("vco_weaviate", "vco_ollama", "vco_code_embed")
    }
}

# Resolve compose dir.
$ComposeDir = $env:VCT_COMPOSE_DIR
if (-not $ComposeDir) {
    if ($env:VCT_INFRASTRUCTURE_DIR -and (Test-Path $env:VCT_INFRASTRUCTURE_DIR)) {
        $ComposeDir = $env:VCT_INFRASTRUCTURE_DIR
    } elseif ($env:VCT_ORCHESTRATOR_ROOT -and (Test-Path (Join-Path $env:VCT_ORCHESTRATOR_ROOT "infrastructure"))) {
        $ComposeDir = Join-Path $env:VCT_ORCHESTRATOR_ROOT "infrastructure"
    } elseif (Test-Path (Join-Path $RepoRoot "infrastructure")) {
        $ComposeDir = Join-Path $RepoRoot "infrastructure"
    } elseif (Test-Path (Join-Path $RepoRoot "claude_mcp_servers")) {
        # Legacy fallback — only the orchestrator clone has this layout.
        $ComposeDir = Join-Path $RepoRoot "claude_mcp_servers"
    } else {
        $ComposeDir = ""
    }
}

# Resolve orchestrator root (used to locate the GPU-safe wrapper script).
$OrchRoot = $env:VCT_ORCHESTRATOR_ROOT
if (-not $OrchRoot) {
    $candidate = Join-Path $RepoRoot "scripts\launch-claude-mcp-stack.sh"
    if (Test-Path $candidate) { $OrchRoot = $RepoRoot }
}
$WrapperScript = ""
if ($OrchRoot) {
    $candidate = Join-Path $OrchRoot "scripts\launch-claude-mcp-stack.sh"
    if (Test-Path $candidate) { $WrapperScript = $candidate }
}

# Container runtime: prefer podman, fallback docker.
$Runtime = $env:VCT_CONTAINER_RUNTIME
if (-not $Runtime) {
    if (Get-Command podman -ErrorAction SilentlyContinue) { $Runtime = "podman" }
    elseif (Get-Command docker -ErrorAction SilentlyContinue) { $Runtime = "docker" }
    else {
        [Console]::Error.WriteLine("ensure-containers: neither podman nor docker found, skipping")
        exit 0
    }
}

# Compose binary: detect both v2 plugin and v1 standalone.
$ComposeCmd = $env:VCT_COMPOSE_CMD
if (-not $ComposeCmd) {
    if ($Runtime -eq "podman") {
        try {
            & podman compose version 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ComposeCmd = "podman compose" }
        } catch { }
        if (-not $ComposeCmd -and (Get-Command podman-compose -ErrorAction SilentlyContinue)) {
            $ComposeCmd = "podman-compose"
        }
    } elseif ($Runtime -eq "docker") {
        try {
            & docker compose version 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ComposeCmd = "docker compose" }
        } catch { }
        if (-not $ComposeCmd -and (Get-Command docker-compose -ErrorAction SilentlyContinue)) {
            $ComposeCmd = "docker-compose"
        }
    }
}

# ---------------------------------------------------------------------------
# Test-PidAlive :: $true if PID is a live process, $false otherwise.
# Windows: Get-Process. Cross-platform PowerShell on Linux/macOS also
# supports Get-Process; that's the canonical liveness probe here.
# ---------------------------------------------------------------------------
function Test-PidAlive {
    param([Parameter(Mandatory=$true)] $TargetPid)
    if (-not $TargetPid) { return $false }
    if ($TargetPid -eq 0 -or $TargetPid -eq "0") { return $false }
    try {
        $p = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        return $null -ne $p
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# Get-RuncRoot :: probe likely runc-state directories. Windows podman runs
# under WSL2; the runc state lives inside the WSL filesystem (not directly
# accessible from Windows), so this is best-effort and may return $null.
# ---------------------------------------------------------------------------
function Get-RuncRoot {
    if ($env:VCT_RUNC_ROOT -and (Test-Path $env:VCT_RUNC_ROOT)) {
        return $env:VCT_RUNC_ROOT
    }
    # Windows podman desktop machines use a Hyper-V/WSL VM; runc state is
    # not addressable from Windows. Linux PowerShell hosts may reach the
    # rootless path though.
    $candidates = @(
        "/run/user/$(id -u 2>$null)/runc",
        "/run/runc",
        (Join-Path $HOME ".local/share/containers/storage/runc")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Write-RecoveryLog :: append a JSON line to the recovery audit log.
# Linux/macOS PowerShell: ~/.local/state/vct/container-recovery.jsonl
# Windows: $env:LOCALAPPDATA\vct\container-recovery.jsonl
# ---------------------------------------------------------------------------
function Write-RecoveryLog {
    param(
        [string]$Container,
        [string]$Action,
        [string]$Reason
    )
    $stateDir = $null
    if ($env:LOCALAPPDATA) {
        $stateDir = Join-Path $env:LOCALAPPDATA "vct"
    } elseif ($env:XDG_STATE_HOME) {
        $stateDir = Join-Path $env:XDG_STATE_HOME "vct"
    } else {
        $stateDir = Join-Path $HOME ".local/state/vct"
    }
    try {
        if (-not (Test-Path $stateDir)) {
            New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        $logFile = Join-Path $stateDir "container-recovery.jsonl"
        $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        $line = '{"timestamp":"' + $ts + '","container":"' + $Container + '","action":"' + $Action + '","reason":"' + $Reason + '"}'
        Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
    } catch { }
}

# ---------------------------------------------------------------------------
# Test-IsGpuContainer :: $true for ollama / code_embed (use GPU wrapper).
# ---------------------------------------------------------------------------
function Test-IsGpuContainer {
    param([string]$Name)
    return ($Name -match 'ollama' -or $Name -match 'code_embed')
}

# ---------------------------------------------------------------------------
# Invoke-WrapperOrCompose :: invoke the CDI-wait wrapper if available,
# else fall back to direct compose-up. Returns $true on success.
# ---------------------------------------------------------------------------
function Invoke-WrapperOrCompose {
    param([string]$Reason)
    if ($WrapperScript -and (Test-Path $WrapperScript)) {
        # The wrapper is bash; on Windows we need WSL/Git-Bash. Try `bash`.
        $bash = Get-Command bash -ErrorAction SilentlyContinue
        if ($bash) {
            if (-not $env:VCT_STACK_WORKING_DIR -and $ComposeDir) {
                $env:VCT_STACK_WORKING_DIR = $ComposeDir
            }
            & $bash.Source $WrapperScript
            Write-Output "Ran launch-claude-mcp-stack.sh wrapper ($Reason)"
            return $true
        }
        # No bash on Windows host → fall through to direct compose.
    }
    if ($ComposeCmd -and $ComposeDir -and (Test-Path $ComposeDir)) {
        Push-Location $ComposeDir
        try {
            $parts = $ComposeCmd -split '\s+'
            $cmdHead = $parts[0]
            $cmdRest = $parts[1..($parts.Length - 1)]
            & $cmdHead @cmdRest up -d
        } finally { Pop-Location }
        Write-Output "Ran '$ComposeCmd up -d' in $ComposeDir ($Reason)"
        return $true
    }
    return $false
}

# ---------------------------------------------------------------------------
# Invoke-ZombieRecovery :: tear down a zombie container's runc state and
# recreate it. Returns $true on best-effort recovery.
# ---------------------------------------------------------------------------
function Invoke-ZombieRecovery {
    param([string]$Name)
    $containerId = ""
    try {
        $containerId = (& $Runtime inspect $Name --format '{{.Id}}' 2>$null | Out-String).Trim()
    } catch { }

    # 1. Try runc delete --force.
    if (Get-Command runc -ErrorAction SilentlyContinue) {
        $runcRoot = Get-RuncRoot
        if ($runcRoot -and $containerId) {
            try {
                & runc --root $runcRoot delete --force $containerId 2>$null | Out-Null
            } catch { }
        }
    }

    # 2. podman rm --force (cleans state DB row even if OCI bundle is gone).
    try {
        & $Runtime rm --force $Name 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-RecoveryLog -Container $Name -Action "failed" -Reason "podman rm --force failed"
            [Console]::Error.WriteLine("ensure-containers: failed to remove zombie '$Name' -- manual cleanup required")
            return $false
        }
    } catch {
        Write-RecoveryLog -Container $Name -Action "failed" -Reason "podman rm --force threw"
        return $false
    }

    # 3. Recreate via wrapper or compose.
    $reason = if (Test-IsGpuContainer -Name $Name) {
        "recreating zombie GPU container $Name"
    } else {
        "recreating zombie container $Name"
    }
    if (-not (Invoke-WrapperOrCompose -Reason $reason)) {
        Write-RecoveryLog -Container $Name -Action "failed" -Reason "no wrapper or compose available for recreate"
        return $false
    }

    Write-RecoveryLog -Container $Name -Action "recovered" -Reason "zombie pid; runc+rm+recreate"
    Write-Output "ensure-containers: recovered zombie container '$Name'"
    return $true
}

$started = 0
$recovered = 0
$needsCompose = $false
$needsGpuWrapper = $false
foreach ($container in $VcoRequiredContainers) {
    $status = "missing"
    try {
        $status = (& $Runtime inspect $container --format '{{.State.Status}}' 2>$null | Out-String).Trim()
        if (-not $status) { $status = "missing" }
    } catch { $status = "missing" }

    if ($status -eq "running") {
        $containerPid = "0"
        try {
            $containerPid = (& $Runtime inspect $container --format '{{.State.Pid}}' 2>$null | Out-String).Trim()
        } catch { }
        if (Test-PidAlive -TargetPid $containerPid) { continue }
        Write-RecoveryLog -Container $container -Action "detected" -Reason "running status with dead pid=$containerPid"
        if (Invoke-ZombieRecovery -Name $container) { $recovered++ }
        continue
    } elseif ($status -eq "stopping") {
        $containerPid = "0"
        try {
            $containerPid = (& $Runtime inspect $container --format '{{.State.Pid}}' 2>$null | Out-String).Trim()
        } catch { }
        if (-not (Test-PidAlive -TargetPid $containerPid)) {
            Write-RecoveryLog -Container $container -Action "detected" -Reason "stopping status with dead pid=$containerPid"
            if (Invoke-ZombieRecovery -Name $container) { $recovered++ }
            continue
        }
        # Genuinely still stopping — let the runtime finish.
        continue
    } elseif ($status -eq "missing") {
        $needsCompose = $true
        if (Test-IsGpuContainer -Name $container) { $needsGpuWrapper = $true }
    } else {
        try {
            & $Runtime start $container 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $started++ }
        } catch { }
    }
}

if ($needsCompose) {
    if ($needsGpuWrapper -and $WrapperScript -and (Test-Path $WrapperScript)) {
        if (-not (Invoke-WrapperOrCompose -Reason "missing GPU container(s)")) {
            [Console]::Error.WriteLine("ensure-containers: wrapper invocation failed")
        }
    } elseif ($ComposeCmd -and $ComposeDir -and (Test-Path $ComposeDir)) {
        Push-Location $ComposeDir
        try {
            $parts = $ComposeCmd -split '\s+'
            $cmdHead = $parts[0]
            $cmdRest = $parts[1..($parts.Length - 1)]
            & $cmdHead @cmdRest up -d
        } finally { Pop-Location }
        Write-Output "Ran '$ComposeCmd up -d' in $ComposeDir (missing containers detected)"
    } elseif (-not $ComposeCmd) {
        [Console]::Error.WriteLine("ensure-containers: $Runtime has no compose available (tried '$Runtime compose' and standalone) -- install $Runtime-compose or the compose plugin")
    } elseif (-not $ComposeDir) {
        [Console]::Error.WriteLine("ensure-containers: no compose directory found (tried VCT_COMPOSE_DIR, VCT_INFRASTRUCTURE_DIR, VCT_ORCHESTRATOR_ROOT\infrastructure, $RepoRoot\infrastructure, $RepoRoot\claude_mcp_servers) -- set VCT_INFRASTRUCTURE_DIR or VCT_ORCHESTRATOR_ROOT in .claude\env")
    }
}

if ($started -gt 0) { Write-Output "Started $started container(s) via $Runtime" }
if ($recovered -gt 0) { Write-Output "Recovered $recovered zombie container(s) via $Runtime" }
exit 0
