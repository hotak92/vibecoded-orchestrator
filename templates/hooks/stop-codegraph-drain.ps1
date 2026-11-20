# Parity: kept in step with stop-codegraph-drain.sh (the v0.2.92 PowerShell-only
# detached-spawn hardening, Start-VcoDetachedPwsh, is Windows-specific; every
# other behaviour must match the .sh sibling).
# stop-codegraph-drain.ps1 -- Stop + SessionStart hook (v0.2.107 CG-3)
# OS-PARITY: ports templates/hooks/stop-codegraph-drain.sh. The code graph
# follows the COMMITTED state of the main checkout's current branch (and of
# each enabled extra code folder at its own HEAD), updated only when HEAD
# moves: an Edit/Write queues nothing, an edit in a linked git worktree (a
# lane) is never indexed and reaches the graph when merged into the main
# checkout's branch. This hook spawns, each DETACHED and at most once per its
# interval: the extras check (vco_lib.codegraph_extras_refresh) and the HEAD
# catch-up (vco_lib.codegraph_head_catchup, ONE analyzer run per HEAD move,
# plus the daily path-existence reconciliation). Registered on Stop AND
# SessionStart, so a pull made outside a session is picked up at the next
# session start. The name is historical: the per-edit drain queue was retired
# in v0.2.107. A project that is not a git repository is updated by a manual
# re-analysis only.
#
# Contract: always exit 0; soft-fail throughout. MUST MATCH the .sh sibling.
# Plain ASCII only (no em-dash, no BOM needed).

# Canonical list: _lib/scrub-env.ps1; the inline list is the fallback for a missing lib.
if ([System.IO.File]::Exists("$PSScriptRoot/_lib/scrub-env.ps1")) { . "$PSScriptRoot/_lib/scrub-env.ps1"; Invoke-VctScrubSecretEnv } else {
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
. "$PSScriptRoot/_lib/hook-stdin.ps1"  # UTF-8 payload read + UTF-8 output (before stderr-cap)

if (Test-Path "$PSScriptRoot/_lib/stderr-cap.ps1") { . "$PSScriptRoot/_lib/stderr-cap.ps1" }
if (Test-Path "$PSScriptRoot/_lib/resolve-powershell.ps1") { . "$PSScriptRoot/_lib/resolve-powershell.ps1" }
# Shared venv resolver -- resolves the analyzer's python interpreter from the
# VCO clone's `.venv`. MUST dot-source
# this helper for any `.venv` reference (venv-resolver-drift gate).
if (Test-Path "$PSScriptRoot/_lib/resolve-vco-venv.ps1") { . "$PSScriptRoot/_lib/resolve-vco-venv.ps1" }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

# The payload carries nothing this hook needs; read it so the harness's pipe
# is consumed.
try { $null = Read-VcoHookStdin } catch { }

$stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
# --- RUNTIME: analyzer script + interpreter (one home for this hook) ---
# Used by BOTH the extra-path refresh and the HEAD catch-up below. Sets
# $script:analyzer / $script:python and returns (cached, resolved at most
# once per run): 0 usable, 1 no analyzer script, 2 bare-PATH interpreter
# cannot import the analyzer's dependencies (or no interpreter at all).
# MUST MATCH stop-codegraph-drain.sh::_drain_resolve_runtime.
$script:runtimeRc = $null
$script:analyzer = $null
$script:python = $null
function Resolve-DrainRuntime {
    if ($null -ne $script:runtimeRc) { return $script:runtimeRc }
    $defaultRepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
    $script:analyzer = if ($env:VCT_ANALYZER_SCRIPT) { $env:VCT_ANALYZER_SCRIPT } else { Join-Path $defaultRepoRoot ".claude/scripts/analyze_code_graph.py" }
    if (-not (Test-Path -LiteralPath $script:analyzer)) { $script:runtimeRc = 1; return 1 }
    # Resolve python (venv preferred via the shared resolver, else system).
    $py = $env:VCT_PYTHON
    if (-not $py -and (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue)) {
        $py = Resolve-VcoVenvPython -ScriptDir $ScriptDir
    }
    $fellBack = $false
    if (-not $py) {
        foreach ($c in @('python','py','python3')) {
            $cmd = Get-Command $c -ErrorAction SilentlyContinue
            if ($cmd) { $py = $cmd.Source; $fellBack = $true; break }
        }
    }
    $script:python = $py
    if (-not $py) { $script:runtimeRc = 1; return 1 }
    # v0.2.96 (WP-5 S2) -- MUST MATCH the .sh probe. When the venv resolver
    # missed, the bare-PATH interpreter in a user project routinely has
    # neither `weaviate` nor `vco_lib`; the detached analyzer would die into a
    # redirected stream. Probed only on fallback.
    if ($fellBack) {
        & $py -c 'import weaviate, vco_lib' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $script:runtimeRc = 2; return 2 }
    }
    $script:runtimeRc = 0
    return 0
}

# --- EXTRA CODE-GRAPH PATHS: automatic re-index (v0.2.100 W5R-04) ---
# MUST MATCH the .sh block of the same name: at most once per
# VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS (default 600) per project, spawn
# `python -m vco_lib.codegraph_extras_refresh` DETACHED. It compares each
# enabled extra path's HEAD with its last indexed commit, runs the SAME
# single-path analyzer argv as the panel's Sync button (throttled per path,
# bounded), and records the new commit through the hub route -- never by
# opening launcher.db. The extra folder is re-indexed when ITS HEAD moves.
# Log: .claude/logs/codegraph_extras_refresh.log.
$extrasInterval = 600
if ($env:VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS -match '^\d+$') {
    $extrasInterval = [int]$env:VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS
}
if (Test-Path -LiteralPath $stateDir -PathType Container) {
    $extrasTsFile = Join-Path $stateDir "codegraph_extras_check.ts"
    $extrasNow = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $extrasLast = 0
    if (Test-Path -LiteralPath $extrasTsFile) {
        try {
            $rawTs = (Get-Content -LiteralPath $extrasTsFile -Raw -ErrorAction Stop).Trim()
            if ($rawTs -match '^\d+$') { $extrasLast = [long]$rawTs }
        } catch { }
    }
    # A stamp in the FUTURE is not a recent check: it is a pre-v0.2.103
    # local-time stamp (Windows PowerShell 5.1 wrote local time, up to +14 h
    # ahead of UTC) or a clock that moved back. Read as "no stamp" so the
    # check is not suppressed until the wall clock catches up (R20 L1).
    # MUST MATCH the .sh sibling's clamp.
    if ($extrasLast -gt $extrasNow) { $extrasLast = 0 }
    if ($extrasLast -eq 0 -or (($extrasNow - $extrasLast) -ge $extrasInterval)) {
        Set-Content -LiteralPath $extrasTsFile -Value "$extrasNow" -NoNewline -ErrorAction SilentlyContinue
        if ((Resolve-DrainRuntime) -eq 0) {
            $extrasLogDir = Join-Path (Join-Path $ProjectRoot ".claude") "logs"
            try { New-Item -ItemType Directory -Path $extrasLogDir -Force -ErrorAction Stop | Out-Null } catch { }
            $extrasLog = Join-Path $extrasLogDir "codegraph_extras_refresh.log"
            $q = { param($v) "'" + ($v -replace "'","''") + "'" }
            # The child's own output/errors land in the same log the module
            # writes, so a broken refresh is never silent. PowerShell single
            # quotes are LITERAL ('' escapes a quote), so paths with $ or
            # backticks cannot re-parse here -- the .sh sibling's NB-H5 class
            # does not exist on this surface.
            $extrasInner = "& " + (& $q $script:python) + " -m vco_lib.codegraph_extras_refresh --project-root " + (& $q $ProjectRoot) +
                " --analyzer " + (& $q $script:analyzer) + " --state-dir " + (& $q $stateDir) + " *>> " + (& $q $extrasLog)
            Start-VcoDetachedPwsh -Command $extrasInner -PowerShellExe $PsExe
        }
    }
}

# --- HEAD-DIFF CATCH-UP: the ONE writer of the primary root's code graph ---
# MUST MATCH the .sh block of the same name (v0.2.105 #512A; since v0.2.107
# CG-3 the only path by which the main checkout's code reaches the graph). At
# most once per VCO_CODEGRAPH_HEAD_CATCHUP_INTERVAL_SECONDS (default 600),
# spawn `python -m vco_lib.codegraph_head_catchup` DETACHED: it compares the
# binding's last_analyzed_commit (hub /config) with the main checkout's HEAD
# and, when that commit is an ANCESTOR, indexes exactly
# `git diff --name-only <recorded>..HEAD` (existing code files only); an
# enormous diff or a deleted code file goes to the analyzer's incremental walk
# instead, and a non-ancestor / missing baseline takes ONE full
# (non-incremental) walk, then records HEAD -- the converging fallback. A
# project with no ENABLED code-graph binding, and a session whose checkout is
# a linked worktree, are skipped. On success it records HEAD through the hub
# route /projects/{id}/codegraph/analyzed -- never by opening launcher.db.
# Installs NO git hook. Log: .claude/logs/codegraph_head_catchup.log.
$catchupInterval = 600
if ($env:VCO_CODEGRAPH_HEAD_CATCHUP_INTERVAL_SECONDS -match '^\d+$') {
    $catchupInterval = [int]$env:VCO_CODEGRAPH_HEAD_CATCHUP_INTERVAL_SECONDS
}
if (Test-Path -LiteralPath $stateDir -PathType Container) {
    $catchupTsFile = Join-Path $stateDir "codegraph_head_catchup.ts"
    $catchupNow = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $catchupLast = 0
    if (Test-Path -LiteralPath $catchupTsFile) {
        try {
            $rawTs = (Get-Content -LiteralPath $catchupTsFile -Raw -ErrorAction Stop).Trim()
            if ($rawTs -match '^\d+$') { $catchupLast = [long]$rawTs }
        } catch { }
    }
    # A stamp in the FUTURE is not a recent check (R20 L1; MUST MATCH the .sh
    # sibling's clamp).
    if ($catchupLast -gt $catchupNow) { $catchupLast = 0 }
    if ($catchupLast -eq 0 -or (($catchupNow - $catchupLast) -ge $catchupInterval)) {
        Set-Content -LiteralPath $catchupTsFile -Value "$catchupNow" -NoNewline -ErrorAction SilentlyContinue
        if ((Resolve-DrainRuntime) -eq 0) {
            $catchupLogDir = Join-Path (Join-Path $ProjectRoot ".claude") "logs"
            try { New-Item -ItemType Directory -Path $catchupLogDir -Force -ErrorAction Stop | Out-Null } catch { }
            $catchupLog = Join-Path $catchupLogDir "codegraph_head_catchup.log"
            $qc = { param($v) "'" + ($v -replace "'","''") + "'" }
            # PowerShell single quotes are LITERAL ('' escapes a quote), so
            # paths with $ or backticks cannot re-parse here -- the .sh
            # sibling's NB-H5 class does not exist on this surface.
            $catchupInner = "& " + (& $qc $script:python) + " -m vco_lib.codegraph_head_catchup --project-root " + (& $qc $ProjectRoot) +
                " --analyzer " + (& $qc $script:analyzer) + " --state-dir " + (& $qc $stateDir) + " *>> " + (& $qc $catchupLog)
            Start-VcoDetachedPwsh -Command $catchupInner -PowerShellExe $PsExe
        }
    }
}

exit 0
