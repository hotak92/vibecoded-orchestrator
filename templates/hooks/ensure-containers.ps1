# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Scrub sensitive env vars before any subprocess
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# V52-AI (v0.2.52): MCP fork-bomb mitigation. If an orchestrator update
# is in progress, skip container startup entirely. See the .sh sibling
# for the full rationale. Treats missing-file / parse-error / stale-
# deadline as "no active update" (proceed normally — same as today's
# pre-fix behaviour).
$VctRootDir = if ($env:VCT_STATE_DIR) { $env:VCT_STATE_DIR } else { Join-Path $env:USERPROFILE ".vct" }
$VctUpdateLockfile = Join-Path $VctRootDir ".update-in-progress.json"
if (Test-Path $VctUpdateLockfile) {
    try {
        $LockData = Get-Content $VctUpdateLockfile -Raw | ConvertFrom-Json
        $DeadlineStr = $LockData.expected_completion_by
        if ($DeadlineStr) {
            # PowerShell parses ISO-8601 with `Z` suffix natively.
            $Deadline = [datetime]::Parse($DeadlineStr).ToUniversalTime()
            $NowUtc = [datetime]::UtcNow
            if ($NowUtc -lt $Deadline) {
                [Console]::Error.WriteLine("[ensure-containers] orchestrator update in progress; skipping container startup until update completes")
                exit 0
            }
        }
    } catch {
        # Soft-fail: corrupt/unreadable lockfile means no active update,
        # proceed with container startup.
    }
}

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
. "$PSScriptRoot/_lib/compose-invocation.ps1"

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

# Container runtime + compose: ONE home — `python -m vco_lib.containers resolve`
# (v0.2.92 PLAN-EXTENSION §3.5 / R13). This hook used to mirror the
# podman/docker + compose-form detection inline (as did two sibling hooks,
# install.py and the launcher), and the four copies had drifted in their
# compose preference order. Class A of the A>B>C rule: one Python
# implementation, called via a ~50 ms subprocess on this session-start path.
# Loud-fail: if the resolver cannot run at all (no interpreter, broken
# install), say so on stderr and skip — never fall back to an inline copy.
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
    Write-Output "ensure-containers: no Python interpreter for vco_lib.containers (broken VCO install?); skipping"
    exit 0
}
$VcoRt = $null
$VcoRtRc = $null
# Capture the resolver's stderr instead of discarding it (v0.2.92 MAJOR-6):
# `2>$null` used to throw the reason away, so a crash on Windows printed
# "resolve failed (rc=N)" with nothing to act on while the .sh sibling
# printed the stderr tail.
$VcoRtErr = Join-Path ([System.IO.Path]::GetTempPath()) "vco-containers-resolve.$PID.err"
try {
    $VcoRtJson = & $RunPy -m vco_lib.containers resolve --json 2>$VcoRtErr
    $VcoRtRc = $LASTEXITCODE
    if ($VcoRtRc -in 0, 3, 4) { $VcoRt = ($VcoRtJson | Out-String) | ConvertFrom-Json }
} catch { $VcoRt = $null }
if (-not $VcoRt) {
    $VcoRtWhy = ""
    if (Test-Path $VcoRtErr) { $VcoRtWhy = ((Get-Content $VcoRtErr -Tail 3) -join " ").Trim() }
    Remove-Item $VcoRtErr -ErrorAction SilentlyContinue
    Write-Output "ensure-containers: vco_lib.containers resolve failed (rc=$VcoRtRc): $VcoRtWhy; skipping"
    exit 0
}
Remove-Item $VcoRtErr -ErrorAction SilentlyContinue
# v0.2.92 BLOCKER-4 + MAJOR-6: the resolver REFUSES a pinned-but-unusable
# runtime (podman and docker have per-runtime named volumes, so driving the
# one the user did not pin brings the stack up EMPTY) and hands back the
# refusal as its reason. Report it on STDOUT, not stderr: a SessionStart
# hook's stderr is not surfaced to the user when the hook exits 0 - only
# stdout is injected as session context, and an unread report is not a report.
if ($VcoRt.state -ne "resolved") {
    # `absent` is a true fact (nothing installed / daemon down / a refused
    # pin); `unknown` means a probe could not run. Both are skips, both said.
    Write-Output "ensure-containers: $($VcoRt.reason); skipping"
    exit 0
}
$Runtime = $VcoRt.runtime
# User can override the compose invocation via VCT_COMPOSE_CMD.
$ComposeCmd = if ($env:VCT_COMPOSE_CMD) { $env:VCT_COMPOSE_CMD } elseif ($VcoRt.compose) { ($VcoRt.compose -join " ") } else { "" }

# ---------------------------------------------------------------------------
# Windows reserved-port-range warning (v0.2.64).
#
# WinNAT / Hyper-V auto-allocate a dynamic TCP range (e.g. 11410-11509) and
# mark it EXCLUDED; any port inside can no longer be bound. VCO's default
# ollama (11435) / code_embed (11440) ports sometimes land inside it. The
# container then shows "Up" but the host port silently fails to bind and the
# KG goes mute. The range MOVES across Windows updates/reboots, so a returning
# user can be bitten even if install worked once.
#
# This MUST stay logically identical to vco_lib/windows_reserved_ports.py
# (the install-time path) — parse `netsh int ipv4 show excludedportrange tcp`,
# test each port against the inclusive ranges, warn (or note the elevated fix).
# Warn-only here: a session-start hook must never mutate system state.
#
# Linux/macOS no-op: $IsWindows is $false on PowerShell Core off Windows
# (Windows PowerShell 5.1 has no $IsWindows automatic var → treat $null as
# "assume Windows", which is correct because 5.1 only runs on Windows).
# ---------------------------------------------------------------------------
function Test-VcoReservedPorts {
    $onWindows = (-not (Test-Path Variable:\IsWindows)) -or $IsWindows
    if (-not $onWindows) { return }

    $ollamaPort = if ($env:OLLAMA_PORT) { [int]$env:OLLAMA_PORT } else { 11435 }
    $codeEmbedPort = if ($env:CODE_EMBED_PORT) { [int]$env:CODE_EMBED_PORT } else { 11440 }
    $weaviatePort = if ($env:WEAVIATE_PORT) { [int]$env:WEAVIATE_PORT } else { 8081 }
    $targets = @(
        @{ Label = "ollama"; Port = $ollamaPort },
        @{ Label = "code_embed"; Port = $codeEmbedPort },
        @{ Label = "weaviate"; Port = $weaviatePort }
    )

    $raw = ""
    try {
        $raw = (& netsh int ipv4 show excludedportrange tcp 2>$null | Out-String)
    } catch {
        # Soft-fail: netsh missing / errored → can't confirm → do nothing.
        return
    }
    if (-not $raw) { return }

    # Parse data rows: exactly two integers per line. Header / dashes / the
    # asterisk footnote carry no bare integer pairs, so they never match.
    # Mirrors parse_excluded_ranges(): second >= first ⇒ inclusive end,
    # otherwise ⇒ a count from first (the "Number of Ports" layout).
    $ranges = @()
    foreach ($line in ($raw -split "`r?`n")) {
        if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
            $first = [int]$Matches[1]
            $second = [int]$Matches[2]
            if ($first -le 0 -or $first -gt 65535) { continue }
            if ($second -ge $first -and $second -le 65535) {
                $start = $first; $end = $second
            } else {
                $start = $first; $end = $first + [Math]::Max($second, 1) - 1
            }
            if ($end -gt 65535) { $end = 65535 }
            $ranges += ,@($start, $end)
        }
    }
    if ($ranges.Count -eq 0) { return }

    foreach ($t in $targets) {
        foreach ($r in $ranges) {
            if ($t.Port -ge $r[0] -and $t.Port -le $r[1]) {
                [Console]::Error.WriteLine("[ensure-containers] WARNING: $($t.Label) port $($t.Port) is inside a Windows reserved TCP range ($($r[0])-$($r[1])). The container will start but the host port WILL NOT bind, and the knowledge graph will go silently mute.")
                [Console]::Error.WriteLine("[ensure-containers] To fix, run this in an ELEVATED (Administrator) terminal:")
                [Console]::Error.WriteLine("    netsh int ipv4 add excludedportrange protocol=tcp startport=$($t.Port) numberofports=1 store=persistent")
                # R42 sweep: two lines, never `net stop winnat && net start
                # winnat`. This advice is introduced as "run this in an
                # ELEVATED terminal", and the elevated terminal Windows 10/11
                # opens by default is PowerShell 5.1 — which rejects `&&` as a
                # syntax error. Mirrors vco_lib/windows_reserved_ports.py.
                [Console]::Error.WriteLine("    net stop winnat")
                [Console]::Error.WriteLine("    net start winnat")
                break
            }
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
            $composeInvocation = Split-VcoComposeCommand -ComposeCmd $ComposeCmd
            $cmdHead = $composeInvocation.Head
            $cmdRest = @($composeInvocation.Rest)
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

    # v0.2.50 audit F6 (2026-06-08): the zombie state-DB-desync failure
    # mode is Podman-specific (rootless conmon vanishes without writing
    # the exit event). On Docker the centralised daemon manages State.*
    # honestly; the PID-alive cross-check at the caller can fire
    # spuriously (Docker PIDs live in a VM on macOS/Windows; even on
    # Linux Docker the host-side State.Pid is semantically different
    # from podman's). Running runc delete + `docker rm --force` on a
    # healthy Docker container produces noisy unnecessary recreate
    # cycles. Mirror verify-container-ports.ps1::Test-ContainerPidAlive.
    if ($Runtime -ne "podman") {
        return $true
    }

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
# v0.2.92 BLOCKER-1 (parity with needs_code_embed_build in the .sh sibling):
# code_embed is the ONE compose service BUILT from the checkout, so a stale
# image survives every update. Set only when THAT container is missing.
$needsCodeEmbedBuild = $false
# v0.2.50 audit F6 (2026-06-08): zombie detection (running status with
# dead PID per Get-Process) is Podman-specific. On Docker the State.Pid
# value carries different host-side semantics (containerd PID, VM PID on
# macOS/Windows). Skip the PID-alive cross-check for non-podman runtimes
# and trust Docker's State.Status.
$ZombieDetectionEnabled = ($Runtime -eq "podman")
foreach ($container in $VcoRequiredContainers) {
    $status = "missing"
    try {
        $status = (& $Runtime inspect $container --format '{{.State.Status}}' 2>$null | Out-String).Trim()
        if (-not $status) { $status = "missing" }
    } catch { $status = "missing" }

    if ($status -eq "running") {
        if (-not $ZombieDetectionEnabled) {
            # Docker / rootful runtime: trust State.Status=running.
            continue
        }
        $containerPid = "0"
        try {
            $containerPid = (& $Runtime inspect $container --format '{{.State.Pid}}' 2>$null | Out-String).Trim()
        } catch { }
        if (Test-PidAlive -TargetPid $containerPid) { continue }
        Write-RecoveryLog -Container $container -Action "detected" -Reason "running status with dead pid=$containerPid"
        if (Invoke-ZombieRecovery -Name $container) { $recovered++ }
        continue
    } elseif ($status -eq "stopping") {
        if (-not $ZombieDetectionEnabled) {
            # Docker / rootful runtime: trust State.Status=stopping.
            continue
        }
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
        if ($container -like "*code_embed*") { $needsCodeEmbedBuild = $true }
    } else {
        try {
            & $Runtime start $container 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $started++ }
        } catch { }
    }
}

if ($needsCompose) {
    # Warn BEFORE compose-up: a reserved-range conflict makes compose-up
    # "succeed" while the host port silently never binds. Surfacing it here
    # gives the user the fix instead of a cryptic bind error / mute KG.
    Test-VcoReservedPorts
    if ($needsGpuWrapper -and $WrapperScript -and (Test-Path $WrapperScript)) {
        if (-not (Invoke-WrapperOrCompose -Reason "missing GPU container(s)")) {
            [Console]::Error.WriteLine("ensure-containers: wrapper invocation failed")
        }
    } elseif ($ComposeCmd -and $ComposeDir -and (Test-Path $ComposeDir)) {
        Push-Location $ComposeDir
        try {
            $composeInvocation = Split-VcoComposeCommand -ComposeCmd $ComposeCmd
            $cmdHead = $composeInvocation.Head
            $cmdRest = @($composeInvocation.Rest)
            # v0.2.92 BLOCKER-1 (parity with ensure-containers.sh): `--build`
            # only when the code_embed container is among the missing ones -
            # it is the one service whose image is BUILT from the checkout, and
            # we are creating it anyway. An unconditional `--build` on a
            # session-start hook would rebuild a 6 GB CUDA image every time any
            # container went away.
            $buildRan = $false
            if ($needsCodeEmbedBuild) {
                & $cmdHead @cmdRest up -d --build
                if ($LASTEXITCODE -eq 0) {
                    $buildRan = $true
                } else {
                    & $cmdHead @cmdRest up -d
                }
            } else {
                & $cmdHead @cmdRest up -d
            }
        } finally { Pop-Location }
        # Report which invocation ACTUALLY ran (parity with the .sh sibling):
        # claiming "--build" after falling back would be a promise not kept.
        if ($buildRan) {
            Write-Output "Ran '$ComposeCmd up -d --build' in $ComposeDir (missing containers incl. the code-embedding service, whose image is built from source)"
        } elseif ($needsCodeEmbedBuild) {
            Write-Output "Ran '$ComposeCmd up -d' in $ComposeDir ('--build' was rejected, so the code-embedding image was NOT refreshed - run 'python install.py --update' from the orchestrator root)"
        } else {
            Write-Output "Ran '$ComposeCmd up -d' in $ComposeDir (missing containers detected)"
        }
    } elseif (-not $ComposeCmd) {
        [Console]::Error.WriteLine("ensure-containers: $Runtime has no compose available (tried '$Runtime compose' and standalone) -- install $Runtime-compose or the compose plugin")
    } elseif (-not $ComposeDir) {
        [Console]::Error.WriteLine("ensure-containers: no compose directory found (tried VCT_COMPOSE_DIR, VCT_INFRASTRUCTURE_DIR, VCT_ORCHESTRATOR_ROOT\infrastructure, $RepoRoot\infrastructure, $RepoRoot\claude_mcp_servers) -- set VCT_INFRASTRUCTURE_DIR or VCT_ORCHESTRATOR_ROOT in .claude\env")
    }
}

if ($started -gt 0) { Write-Output "Started $started container(s) via $Runtime" }
if ($recovered -gt 0) { Write-Output "Recovered $recovered zombie container(s) via $Runtime" }
exit 0
