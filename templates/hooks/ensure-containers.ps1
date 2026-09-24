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
#
# WHICH containers, and what may be done to each (v0.2.97): ONE call,
# `python -m vco_lib.service_lifecycle plan --json`, reads the launcher.db
# `service_endpoints` rows and hands back the compose service list and a
# per-container policy. A VCO-managed service is started, created by compose
# when missing, and re-created when it is a zombie. An ADOPTED container is
# only ever started BY NAME: never `rm`, never composed (a compose re-create
# would bring it back on the installer's default, EMPTY volume). Every
# compose call names its services explicitly with `--no-deps` — there is no
# bare `up -d` (plan invariant I1). Mirror of ensure-containers.sh.
#
# Zombie-recovery (PR-13, v0.2.11, 2026-05-16):
#   After OOM events, podman containers may report State.Status=running
#   with State.Pid=<dead pid>. The conmon monitor was killed alongside the
#   container, so nobody triggered runc cleanup; the container exists in
#   podman's DB but its PID does not exist. `podman restart` then fails
#   with "container with given ID already exists: OCI runtime error".
#   We probe State.Pid via Get-Process; if dead, run `runc delete --force`;
#   a VCO-managed container is then `podman rm --force`d and re-created by
#   compose, an adopted one only gets `start` (v0.2.97). Each recovery
#   attempt is appended to
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
    # Fallback if _lib is missing (very old install pre-PR-2): the same
    # override-only rule (v0.2.97 — the plan supplies the default set).
    $VcoRequiredContainers = @()
    if ($env:VCT_REQUIRED_CONTAINERS) {
        $VcoRequiredContainers = @($env:VCT_REQUIRED_CONTAINERS -split '\s+' | Where-Object { $_ })
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

# Session reconcile FIRST (v0.2.97, parity with the .sh sibling — the
# ordering rationale is there): `python -m vco_lib.service_endpoints
# reconcile --phase session --json`, run by `service_lifecycle
# session-reconcile` as a time-bounded child (8 s of this hook's budget),
# soft-failing to one stdout line. The emit site of
# `service_endpoint_unreachable`; it corrects the rows the plan below reads.
try {
    & $RunPy -m vco_lib.service_lifecycle session-reconcile 2>$null | ForEach-Object { Write-Output $_ }
} catch { }

# The lifecycle plan: which containers, and what may be done to each
# (v0.2.97 — see the header). Loud-fail like the runtime resolver above: a
# plan that cannot be read means NOTHING is started, never a fallback to the
# old whole-stack `up -d` (which would compose-create adopted services).
$VcoLcErr = Join-Path ([System.IO.Path]::GetTempPath()) "vco-service-lifecycle.$PID.err"
$VcoLcArgs = @('-m', 'vco_lib.service_lifecycle', 'plan', '--json')
if (@($VcoRequiredContainers).Count -gt 0) { $VcoLcArgs += @('--required', (@($VcoRequiredContainers) -join ' ')) }
$VcoLc = $null
$VcoLcRc = $null
try {
    $VcoLcJson = & $RunPy @VcoLcArgs 2>$VcoLcErr
    $VcoLcRc = $LASTEXITCODE
    if ($VcoLcRc -eq 0) { $VcoLc = ($VcoLcJson | Out-String) | ConvertFrom-Json }
} catch { $VcoLc = $null }
if (-not $VcoLc) {
    $VcoLcWhy = ""
    if (Test-Path $VcoLcErr) { $VcoLcWhy = ((Get-Content $VcoLcErr -Tail 3) -join " ").Trim() }
    Remove-Item $VcoLcErr -ErrorAction SilentlyContinue
    Write-Output "ensure-containers: vco_lib.service_lifecycle plan failed (rc=$VcoLcRc): $VcoLcWhy; skipping"
    exit 0
}
Remove-Item $VcoLcErr -ErrorAction SilentlyContinue

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
# Test-IsGpuService :: $true for the compose services the GPU-safe wrapper
# should bring up (ollama, code_embed).
# ---------------------------------------------------------------------------
function Test-IsGpuService {
    param([string]$Service)
    return ($Service -eq 'ollama' -or $Service -eq 'code_embed')
}

# ---------------------------------------------------------------------------
# Invoke-ComposeUpServices :: bring up EXACTLY the named compose services
# (`--no-deps`, never a bare `up -d`). Mirrors compose_up_services in the
# .sh sibling: the CDI-wait wrapper when a GPU service is among them, else
# direct compose with the argv from `vco_lib.service_lifecycle
# compose-args`. Returns 0 on success, 1 on failure, 2 when no compose
# command / dir is available. Its messages (and compose's own output) go
# straight to stdout via [Console]::Out: a function's pipeline output would
# otherwise become part of its RETURN value.
# ---------------------------------------------------------------------------
function Invoke-ComposeUpServices {
    param([bool]$Build, [string[]]$Services)
    $Services = @($Services | Where-Object { $_ })
    if ($Services.Count -eq 0) { return 0 }
    $wantsGpu = @($Services | Where-Object { Test-IsGpuService -Service $_ }).Count -gt 0
    if ($wantsGpu -and $WrapperScript -and (Test-Path $WrapperScript)) {
        # The wrapper is bash; on Windows we need WSL/Git-Bash. Try `bash`.
        $bash = Get-Command bash -ErrorAction SilentlyContinue
        if ($bash) {
            if (-not $env:VCT_STACK_WORKING_DIR -and $ComposeDir) {
                $env:VCT_STACK_WORKING_DIR = $ComposeDir
            }
            $env:VCT_STACK_BUILD = if ($Build) { '1' } else { '' }
            & $bash.Source $WrapperScript up @Services | ForEach-Object { [Console]::Out.WriteLine($_) }
            $wrapperRc = $LASTEXITCODE
            Remove-Item Env:VCT_STACK_BUILD -ErrorAction SilentlyContinue
            if ($wrapperRc -eq 0) {
                [Console]::Out.WriteLine("Ran launch-claude-mcp-stack.sh wrapper for: $($Services -join ' ')")
                return 0
            }
            [Console]::Error.WriteLine("ensure-containers: wrapper invocation failed for: $($Services -join ' ')")
            return 1
        }
        # No bash on Windows host -> fall through to direct compose.
    }
    if (-not $ComposeCmd -or -not $ComposeDir -or -not (Test-Path $ComposeDir)) { return 2 }
    $argSets = @($true, $false)
    if (-not $Build) { $argSets = @($false) }
    foreach ($withBuild in $argSets) {
        $pyArgs = @('-m', 'vco_lib.service_lifecycle', 'compose-args', '--json', '--services', ($Services -join ' '))
        if ($withBuild) { $pyArgs += '--build' }
        $upArgs = $null
        try {
            $upJson = & $RunPy @pyArgs 2>$null
            if ($LASTEXITCODE -eq 0) { $upArgs = @((($upJson | Out-String) | ConvertFrom-Json).args) }
        } catch { $upArgs = $null }
        if ($null -eq $upArgs) {
            [Console]::Error.WriteLine("ensure-containers: vco_lib.service_lifecycle compose-args failed for: $($Services -join ' ')")
            return 1
        }
        if ($upArgs.Count -eq 0) { return 0 }
        $rc = 1
        Push-Location $ComposeDir
        try {
            $composeInvocation = Split-VcoComposeCommand -ComposeCmd $ComposeCmd
            $cmdHead = $composeInvocation.Head
            $cmdRest = @($composeInvocation.Rest)
            & $cmdHead @cmdRest @upArgs | ForEach-Object { [Console]::Out.WriteLine($_) }
            $rc = $LASTEXITCODE
        } finally { Pop-Location }
        if ($Build -and -not $withBuild) {
            # Report which invocation ACTUALLY ran (parity with the .sh
            # sibling): claiming "--build" after falling back would be a
            # promise not kept.
            [Console]::Out.WriteLine("Ran '$ComposeCmd $($upArgs -join ' ')' in $ComposeDir ('--build' was rejected, so the code-embedding image was NOT refreshed - run 'python install.py --update' from the orchestrator root)")
            return 0
        }
        if ($rc -eq 0) {
            [Console]::Out.WriteLine("Ran '$ComposeCmd $($upArgs -join ' ')' in $ComposeDir")
            return 0
        }
        if (-not $withBuild) { return 1 }
    }
    return 1
}

# ---------------------------------------------------------------------------
# Clear-ZombieState :: `runc delete --force` the container's orphan OCI
# state. Touches no container record and no data.
# ---------------------------------------------------------------------------
function Clear-ZombieState {
    param([string]$Name)
    $containerId = ""
    try {
        $containerId = (& $Runtime inspect $Name --format '{{.Id}}' 2>$null | Out-String).Trim()
    } catch { }
    if (Get-Command runc -ErrorAction SilentlyContinue) {
        $runcRoot = Get-RuncRoot
        if ($runcRoot -and $containerId) {
            try {
                & runc --root $runcRoot delete --force $containerId 2>$null | Out-Null
            } catch { }
        }
    }
}

# ---------------------------------------------------------------------------
# Invoke-ZombieAction :: act on a zombie container per its lifecycle policy
# (mirrors handle_zombie in the .sh sibling).
#   recreate (VCO-managed) -> runc cleanup + `rm --force`, and the service is
#     queued for the ONE compose call below.
#   start (adopted / unlisted) -> runc cleanup + `start` BY NAME. Never `rm`:
#     a compose re-create would bring it back on the installer's EMPTY volume.
# ---------------------------------------------------------------------------
function Invoke-ZombieAction {
    param([string]$Name, [string]$Service, [string]$Action)

    # v0.2.50 audit F6 (2026-06-08): the zombie state-DB-desync failure
    # mode is Podman-specific (rootless conmon vanishes without writing
    # the exit event). On Docker the centralised daemon manages State.*
    # honestly; the PID-alive cross-check at the caller can fire
    # spuriously (Docker PIDs live in a VM on macOS/Windows; even on
    # Linux Docker the host-side State.Pid is semantically different
    # from podman's). Running runc delete + `docker rm --force` on a
    # healthy Docker container produces noisy unnecessary recreate
    # cycles. Mirror verify-container-ports.ps1::Test-ContainerPidAlive.
    if ($Runtime -ne "podman") { return }
    switch ($Action) {
        'recreate' {
            Clear-ZombieState -Name $Name
            # podman rm --force cleans the state DB row even if the OCI
            # bundle is gone.
            $rmOk = $false
            try {
                & $Runtime rm --force $Name 2>$null | Out-Null
                $rmOk = ($LASTEXITCODE -eq 0)
            } catch { $rmOk = $false }
            if (-not $rmOk) {
                Write-RecoveryLog -Container $Name -Action "failed" -Reason "podman rm --force failed"
                [Console]::Error.WriteLine("ensure-containers: failed to remove zombie '$Name' -- manual cleanup required")
                return
            }
            $script:ComposeList += $Service
            $script:ZombieRecreated += $Name
        }
        'start' {
            Clear-ZombieState -Name $Name
            $startOk = $false
            try {
                & $Runtime start $Name 2>$null | Out-Null
                $startOk = ($LASTEXITCODE -eq 0)
            } catch { $startOk = $false }
            if ($startOk) {
                Write-RecoveryLog -Container $Name -Action "recovered" -Reason "zombie pid; runc cleanup+start (not VCO-managed: never removed)"
                Write-Output "ensure-containers: restarted zombie container '$Name' (not VCO-managed: cleaned its runtime state and started it, never removed it)"
                $script:recovered++
            } else {
                Write-RecoveryLog -Container $Name -Action "failed" -Reason "zombie pid; start after runc cleanup failed (not VCO-managed: never removed)"
                Write-Output "ensure-containers: '$Name' is in a zombie state and could not be started; VCO does not remove a container it does not manage - check it with its owner ($Runtime start $Name)"
            }
        }
        default {
            Write-RecoveryLog -Container $Name -Action "skipped" -Reason "zombie pid; lifecycle policy leaves it alone"
        }
    }
}

$started = 0
$script:recovered = 0
# The compose services to bring up, in ONE explicit-list call at the end
# (parity with compose_list in the .sh sibling). Adopted containers never
# land here.
$script:ComposeList = @()
$script:ZombieRecreated = @()
# v0.2.92 BLOCKER-1 (parity with needs_code_embed_build in the .sh sibling):
# code_embed is the ONE compose service BUILT from the checkout, so a stale
# image survives every update. Set only when THAT container is missing: an
# unconditional `--build` on a session-start hook would rebuild a 6 GB CUDA
# image every time any container went away.
$needsCodeEmbedBuild = $false
# v0.2.50 audit F6 (2026-06-08): zombie detection (running status with
# dead PID per Get-Process) is Podman-specific. On Docker the State.Pid
# value carries different host-side semantics (containerd PID, VM PID on
# macOS/Windows). Skip the PID-alive cross-check for non-podman runtimes
# and trust Docker's State.Status.
$ZombieDetectionEnabled = ($Runtime -eq "podman")
foreach ($policy in @($VcoLc.containers)) {
    $container = [string]$policy.container
    $service = [string]$policy.service
    if ($policy.on_missing -eq 'ignore' -and $policy.on_zombie -eq 'ignore' -and $policy.on_stopped -eq 'ignore') {
        continue  # disabled / not autostarted: VCO leaves it alone
    }
    $status = "missing"
    try {
        $status = (& $Runtime inspect $container --format '{{.State.Status}}' 2>$null | Out-String).Trim()
        if (-not $status) { $status = "missing" }
    } catch { $status = "missing" }

    if ($status -eq "running" -or $status -eq "stopping") {
        if (-not $ZombieDetectionEnabled) {
            # Docker / rootful runtime: trust State.Status.
            continue
        }
        $containerPid = "0"
        try {
            $containerPid = (& $Runtime inspect $container --format '{{.State.Pid}}' 2>$null | Out-String).Trim()
        } catch { }
        # A live `stopping` container is left to finish its teardown.
        if (Test-PidAlive -TargetPid $containerPid) { continue }
        Write-RecoveryLog -Container $container -Action "detected" -Reason "$status status with dead pid=$containerPid"
        Invoke-ZombieAction -Name $container -Service $service -Action ([string]$policy.on_zombie)
        continue
    } elseif ($status -eq "missing") {
        if ($policy.on_missing -eq 'compose') {
            $script:ComposeList += $service
            if ($service -eq 'code_embed') { $needsCodeEmbedBuild = $true }
        } elseif ($policy.on_missing -eq 'report') {
            Write-Output "ensure-containers: container '$container' does not exist; it is not VCO-managed, so VCO does not create it (see ``python -m vco_lib.service_endpoints show``)"
        }
    } elseif ($policy.on_stopped -eq 'start') {
        # Exists but stopped: start it BY NAME (managed or adopted).
        try {
            & $Runtime start $container 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $started++ }
        } catch { }
    }
}

if ($script:ComposeList.Count -gt 0) {
    # Warn BEFORE compose-up: a reserved-range conflict makes compose-up
    # "succeed" while the host port silently never binds. Surfacing it here
    # gives the user the fix instead of a cryptic bind error / mute KG.
    Test-VcoReservedPorts
    $upRc = Invoke-ComposeUpServices -Build $needsCodeEmbedBuild -Services $script:ComposeList
    if ($upRc -eq 2) {
        if (-not $ComposeCmd) {
            [Console]::Error.WriteLine("ensure-containers: $Runtime has no compose available (tried '$Runtime compose' and standalone) -- install $Runtime-compose or the compose plugin")
        } else {
            [Console]::Error.WriteLine("ensure-containers: no compose directory found (tried VCT_COMPOSE_DIR, VCT_INFRASTRUCTURE_DIR, VCT_ORCHESTRATOR_ROOT\infrastructure, $RepoRoot\infrastructure, $RepoRoot\claude_mcp_servers) -- set VCT_INFRASTRUCTURE_DIR or VCT_ORCHESTRATOR_ROOT in .claude\env")
        }
    }
    foreach ($name in $script:ZombieRecreated) {
        if ($upRc -eq 0) {
            Write-RecoveryLog -Container $name -Action "recovered" -Reason "zombie pid; runc+rm+recreate"
            Write-Output "ensure-containers: recovered zombie container '$name'"
            $script:recovered++
        } else {
            Write-RecoveryLog -Container $name -Action "failed" -Reason "no wrapper or compose available for recreate"
        }
    }
}
$recovered = $script:recovered

if ($started -gt 0) { Write-Output "Started $started container(s) via $Runtime" }
if ($recovered -gt 0) { Write-Output "Recovered $recovered zombie container(s) via $Runtime" }
exit 0
