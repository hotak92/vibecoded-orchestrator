# _lib/route-touched-path.ps1
# THE home for "a file was touched: where does it need to go?" -- the
# PowerShell sibling of _lib/route-touched-path.sh (v0.2.95, lane F10).
#
# WHY THIS EXISTS
# ---------------
# Until v0.2.95 every line of this routing lived inside post-file-edit.ps1,
# which is registered on PostToolUse matcher `Edit|Write` ONLY. A file
# written from the CLI -- `cat > knowledge/foo.md <<EOF`, `sed -i` on a
# docs page, `cp` into a source tree -- therefore reached Weaviate NEVER.
# Closing that gap needs a second hook (post-bash-file-sync.ps1) that does
# the SAME routing, and the project's A>B>C rule forbids a second copy.
#
# MUST MATCH: templates/hooks/_lib/route-touched-path.sh -- same routing
# table, same gate semantics, same debounce channels.
#
# CONTRACT FOR CALLERS
#   . $PSScriptRoot/_lib/route-touched-path.ps1
#   Initialize-VcoRoute -HooksDir <dir> -ProjectRoot <dir>
#   Invoke-VcoRouteTouchedPath -Path <abs> -SessionId <id> [-AgentId <id>]
#   # then: emit $script:VcoRouteNudge through Emit-AdditionalContext.
#
# Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoRouteTouchedPathSourced) { return }
$script:VcoRouteTouchedPathSourced = $true
. (Join-Path $PSScriptRoot "utf8-nobom.ps1")  # BOM-free writes (review R5-8)
# The detached dup-scan child dot-sources the same helper by absolute path.
$script:VcoUtf8NoBomLib = Join-Path $PSScriptRoot "utf8-nobom.ps1"
# v0.2.104 E-1: path containment (Test-VcoPathUnderRoot) for the reminder gate.
$script:VcoRouteProjectRootLib = Join-Path $PSScriptRoot "project-root.ps1"
if (Test-Path -LiteralPath $script:VcoRouteProjectRootLib) { . $script:VcoRouteProjectRootLib }

# Accumulates LLM-visible text the caller should emit (currently only the
# pending duplicate-scan report).
$script:VcoRouteNudge = ""

function Add-VcoRouteNudge([string]$msg) {
    if ($script:VcoRouteNudge) {
        $script:VcoRouteNudge = "$script:VcoRouteNudge`n`n$msg"
    } else {
        $script:VcoRouteNudge = $msg
    }
}

# --- Sibling helpers ------------------------------------------------------
# Dot-sourced at TOP LEVEL, never from inside a function: a `.` source
# executed inside a function puts the definitions in that function's LOCAL
# scope, which evaporates when it returns -- the functions would then be
# missing at the moment the routing needs them.
#
# v0.2.54 Track G (G-6): child spawns used a hardcoded `pwsh`, which does
# not exist on PowerShell 5.1-only machines. $PsExe resolves the fallback
# and Start-VcoDetachedPwsh is the ONE guarded spawn home.
$_VcoRouteLibDir = $PSScriptRoot
foreach ($_vcoLib in @("resolve-powershell.ps1", "resolve-vco-venv.ps1", "code-extensions.ps1")) {
    $_vcoLibPath = Join-Path $_VcoRouteLibDir $_vcoLib
    if (Test-Path $_vcoLibPath) { . $_vcoLibPath }
}
$script:VcoRoutePsExe = if ($script:PsExe) { $script:PsExe } else { "pwsh" }

    # Debounce helper (2026-06-18, write-amplification fix). Coalesces rapid
    # re-edits of the SAME file into one Weaviate write per quiet-window
    # (VCO_KG_SYNC_DEBOUNCE_SECONDS, default 5; 0 disables). Correctness
    # argument (final state always syncs) + crash-safety reasoning live in
    # the helper header. Identical semantics to _lib/kg-sync-debounce.sh.
    #
    # Conditional source: a partial/old bundle install may lack the lib. If
    # absent we define a passthrough Invoke-KgDebounceSchedule that runs the
    # sync immediately in a detached process (pre-2026-06-18 behaviour), so a
    # missing helper degrades to "no debounce" rather than breaking the hook.
    $DebounceLib = Join-Path $PSScriptRoot "_lib/kg-sync-debounce.ps1"
    if (Test-Path $DebounceLib) {
        . $DebounceLib
    } else {
        function Invoke-KgDebounceSchedule {
            param([string]$ProjectRoot, [string]$FilePath, [string]$WorkingDir,
                  [string]$Command, [string]$Channel = "kg")
            $psExe = if ($script:VcoRoutePsExe) { $script:VcoRoutePsExe } else { "pwsh" }
            $wdEsc = ($WorkingDir -replace "'", "''")
            $child = "if ('$wdEsc') { Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue }; try { $Command } catch { }"
            # Through the ONE guarded spawn home (_lib/resolve-powershell.ps1,
            # dot-sourced above): an unguarded `-WindowStyle Hidden` is REJECTED
            # on non-Windows PowerShell and takes the whole spawn with it.
            Start-VcoDetachedPwsh -Command $child -PowerShellExe $psExe
        }
    }

# Initialize-VcoRoute: resolve the per-project facts the routing needs ONCE
# per hook fire -- the project root, the two directory prefixes,
# VCT_PROJECT_ID and the access-matrix checker path. Safe to call more than
# once.
function Initialize-VcoRoute {
    param([string]$HooksDir, [string]$ProjectRoot)

    $script:VcoRouteHooksDir = $HooksDir
    $script:VcoRouteProjectRoot = $ProjectRoot
    $knowledgeRoot = Join-Path $ProjectRoot "knowledge"
    $docsDir = Join-Path $ProjectRoot "docs"
    # D-9 (v0.2.73): match on the directory WITH a trailing separator so
    # sibling dirs (knowledge_base/, docs-archive/) don't sync into the KG /
    # development collections. StartsWith on the bare root matched them.
    $script:VcoRouteKnowledgeSep = $knowledgeRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $script:VcoRouteDocsSep = $docsDir.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar

    # Resolve project_id once for the access checks below. Same env-then-
    # .claude/env fallback and the SAME line rule as the bash sibling
    # (`vco_lib.envfile.parse_env_lines`): optional `export ` prefix -- the
    # form the projection's managed block writes -- LAST match wins (what
    # `source` gives: a user's override after the managed block counts;
    # v0.2.102, this took the first before), one matching pair of quotes
    # stripped. MUST MATCH route-touched-path.sh and
    # `vco_lib.envfile.env_value(..., last_wins=True)` (parity test:
    # tests/test_v0297_route_project_id_parity.py).
    $script:VcoRouteProjectId = $Env:VCT_PROJECT_ID
    if (-not $script:VcoRouteProjectId) {
        $envFile = Join-Path $ProjectRoot ".claude/env"
        if (Test-Path $envFile) {
            try {
                $envLines = Get-Content -LiteralPath $envFile -ErrorAction Stop
                foreach ($line in $envLines) {
                    if ($line -match '^\s*(?:export\s+)?VCT_PROJECT_ID=(.*)$') {
                        $v = $Matches[1].Trim()
                        if ($v.Length -ge 2 -and $v[0] -eq $v[$v.Length - 1] -and ($v[0] -eq '"' -or $v[0] -eq "'")) {
                            $v = $v.Substring(1, $v.Length - 2)
                        }
                        # No `break`: a later assignment overrides (last wins).
                        $script:VcoRouteProjectId = $v
                    }
                }
            } catch { }
        }
    }

    # Resolve the access-matrix checker path once for the debounced (gate
    # runs at SYNC time) command strings. The detached child that eventually
    # evals these commands is a SEPARATE process that does NOT inherit
    # functions defined here, so the deferred command must re-run the gate
    # via the EXTERNAL resolver script rather than calling a function by name.
    $script:VcoRouteAccessCheckPs1 = $null
    foreach ($c in @(
        (Join-Path $ProjectRoot "templates/scripts/vct_access_check.ps1"),
        (Join-Path $ProjectRoot ".claude/scripts/vct_access_check.ps1")
    )) { if (Test-Path $c) { $script:VcoRouteAccessCheckPs1 = $c; break } }
}

# v0.2.49 Phase 8 (item #22): access-matrix gate for KG writes.
#
# Before kicking off any kg-sync subprocess (or upload_docs.py), check
# if this project has write access to the target Weaviate collection.
# The check is fail-open: if the hub is unreachable / the project
# isn't registered / the response is malformed, the resolver returns
# "write" + emits a WARNING + logs a dropped-write-metric row, then
# the sync proceeds. This is DELIBERATE (closed-circuit would brick
# all KG writes during launcher restart).
#
# When the gate returns "read" or "none", we SKIP the sync silently +
# the user gets the WARNING from the resolver client about the deny.
#
# Mirrors templates/hooks/post-file-edit.sh's _kg_write_allowed shell
# function. Resolver discovery: templates/scripts/vct_access_check.ps1
# (orchestrator-root) → .claude/scripts/vct_access_check.ps1
# (user-project install) — the bundle install writes that copy, so the
# two are byte-identical the moment they are written; nothing gates the
# copy afterwards (a copy the user has since edited is backed up to
# .claude/backups/bundle-adoptions/<ts>/ and replaced on the next
# bundle update). Prior text here also called this resolver
# "byte-equivalent to the bash sibling". It is not and never was: the
# .sh and .ps1 resolvers are two separate languages. What IS pinned is
# that BOTH flavours exist (a missing .ps1 sibling is a Windows
# outage) and that they stay logically equivalent — the
# `.github/workflows/hook-parity.yml` gate, which is what survived the
# PR-39 / v0.2.12 removal of `scripts/check_template_drift.py`.
# v0.2.49 SB1: emit a dropped_writes.jsonl row when the gate falls
# back to silent-allow because VCT_PROJECT_ID is missing. Mirrors
# templates/hooks/post-file-edit.sh::_kg_emit_gate_skipped_metric and
# the existing emit_metric helper in vct_access_check.ps1. Never
# throws (silent-allow contract must hold).
function Emit-KgGateSkippedMetric {
    param([string]$Collection)
    try {
        $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else {
            Join-Path $Env:USERPROFILE ".vct"
        }
        $cacheDir = Join-Path $stateDir "cache"
        if (-not (Test-Path $cacheDir)) {
            New-Item -ItemType Directory -Path $cacheDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        $jsonl = Join-Path $cacheDir "dropped_writes.jsonl"
        $ts = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $row = @{
            ts          = $ts
            project_id  = ""
            collection  = $Collection
            reason      = "gate_skipped_no_project_id"
            fail_open   = $true
        } | ConvertTo-Json -Compress
        Add-VcoUtf8Content -Path $jsonl -Value $row
    } catch {
        # Metric-emit failure must not break the silent-allow contract.
    }
}

# v0.2.49 SB1: record an UPDATE_DEFERRED.md entry directing the user to
# resolve the empty-VCT_PROJECT_ID condition (run install.py --update OR
# re-register via Launcher GUI). Silent-allow remains the default at the gate;
# this entry is the user-facing surface.
#
# v0.2.100 WP-17: the entry is written by the ONE locked writer,
# `python -m vco_lib.gate_skipped_deferral` (shared with the weaviate-kg MCP
# server), never by an Add-Content from here. The old append took no lock: it
# raced install.py's finalize (which reads, merges and rewrites the ledger
# under `.claude/context/.update-deferred.lock`) and could lose the entry or
# another writer's. Writers append/merge, never clobber.
#
# Idempotent per (session, project) via a sentinel file in .claude/state/,
# removed again when the write fails so the next write retries. No VCO venv
# (a broken install) is said on stderr — never an unlocked fallback write.
# MUST MATCH route-touched-path.sh's _kg_emit_gate_skipped_deferral.
function Emit-KgGateSkippedDeferral {
    param([string]$Collection)
    if (-not $script:VcoRouteProjectRoot) { return }
    $stateDir = Join-Path $script:VcoRouteProjectRoot ".claude/state"
    $sessionId = if ($Env:VCT_SESSION_ID) { $Env:VCT_SESSION_ID } `
                 elseif ($Env:CLAUDE_SESSION_ID) { $Env:CLAUDE_SESSION_ID } `
                 else { [string]$PID }
    $sentinel = Join-Path $stateDir "gate_skipped_deferral_$sessionId"

    # Per-session dedup. First call writes; subsequent calls in the same
    # session are silent no-ops.
    if (Test-Path -LiteralPath $sentinel) { return }

    try {
        if (-not (Test-Path $stateDir)) {
            New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        Set-Content -LiteralPath $sentinel -Value "" -ErrorAction SilentlyContinue
    } catch { return }

    $venvPy = $null
    $venvLib = Join-Path $script:VcoRouteHooksDir "_lib/resolve-vco-venv.ps1"
    if (Test-Path -LiteralPath $venvLib) {
        . $venvLib
        $venvPy = Resolve-VcoVenvPython -ScriptDir $script:VcoRouteHooksDir
    }
    if (-not $venvPy) {
        [Console]::Error.WriteLine("[VCO] gate_skipped_no_project_id: the deferral entry was NOT recorded -- the VCO venv is not resolvable (broken install; re-run the orchestrator's install / update).")
        Remove-Item -LiteralPath $sentinel -Force -ErrorAction SilentlyContinue
        return
    }
    try {
        & $venvPy -m vco_lib.gate_skipped_deferral --folder $script:VcoRouteProjectRoot `
            "--collection=$Collection" --surface hook | Out-Null
        $ok = ($LASTEXITCODE -eq 0)
    } catch { $ok = $false }
    if (-not $ok) {
        Remove-Item -LiteralPath $sentinel -Force -ErrorAction SilentlyContinue
    }
}

# NOTE (2026-06-18 debounce): this synchronous gate is RETAINED for
# reference + sibling parity with the bash _kg_write_allowed, but the
# ACTIVE gate now runs at SYNC time inside the debounced flusher. Because
# a detached Start-Process child cannot call PowerShell functions defined
# here, the gate logic is re-expressed inline in Build-GatedSyncCommand
# (it invokes the external vct_access_check.ps1 resolver at flush time).
# The empty-project_id metric+deferral surfaces are emitted by
# Build-GatedSyncCommand directly. Keep this function for the contract it
# documents; do not assume it is on the live sync path.
#
# v0.2.92: the .sh sibling now matches this design exactly. Pre-v0.2.92,
# the .sh sibling embedded a call to its `_kg_write_allowed` bash function
# by NAME inside the eval'd command string, on the (incorrect) assumption
# that the detached flusher shares function scope with post-file-edit.sh —
# it does not (the v0.2.65 "Item 1" hardening pass made the flusher spawn
# via `setsid bash -c '...'`, a genuinely separate process that sources
# only _lib/kg-sync-debounce.sh). That silently broke the ENTIRE
# hook-triggered KG/docs auto-sync path on the bash side for ~2.5 months
# (verified via isolated repro). The fix, `_kg_build_gated_sync_cmd` in
# post-file-edit.sh, now mirrors this file's Build-GatedSyncCommand: the
# empty-project_id branch is decided synchronously at schedule time, and
# only the access-matrix decision itself is embedded as a self-contained
# POSIX snippet that calls vct_access_check.sh directly (no function-name
# dependency).
function Test-KgWriteAllowed {
    param(
        [string]$Project,
        [string]$Collection
    )
    if (-not $Project) {
        # v0.2.49 SB1: empty VCT_PROJECT_ID was a silent bypass. Per
        # user Q1 (2026-06-08), silent-allow stays the default; metric +
        # deferral are the visibility surfaces. Metric-first so the
        # JSONL row lands even if the deferral write hits a permission
        # error.
        Emit-KgGateSkippedMetric -Collection $Collection
        Emit-KgGateSkippedDeferral -Collection $Collection
        return $true   # no project context → allow (legacy path)
    }
    if (-not $Collection) { return $true } # no collection context → allow

    $resolver = $null
    $candidates = @(
        (Join-Path $script:VcoRouteProjectRoot "templates/scripts/vct_access_check.ps1"),
        (Join-Path $script:VcoRouteProjectRoot ".claude/scripts/vct_access_check.ps1")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $resolver = $c; break }
    }
    if (-not $resolver) {
        # Resolver not on disk → allow (pre-v0.2.49 install, or post-
        # update where the script hasn't been bundled yet). Matches the
        # bash sibling's same fallthrough.
        return $true
    }

    try {
        $level = & $script:VcoRoutePsExe -NoProfile -File $resolver $Project $Collection 2>$null
        if ($null -eq $level) { return $true }  # fail-open on null
        $level = ([string]$level).Trim()
    } catch {
        return $true  # fail-open on any invocation error
    }
    return ($level -eq 'write')
}

# Build a self-contained "gate THEN sync" command string for Start-Job.
# Runs the resolver at flush time; only proceeds to the sync command
# when the gate returns "write" (or when the gate cannot apply, matching
# fall-open). $SyncExpr is the PowerShell expression that performs the
# actual sync.
function Build-GatedSyncCommand {
    param([string]$Project, [string]$Collection, [string]$SyncExpr)
    # Empty VCT_PROJECT_ID: preserve the v0.2.49 SB1 user-facing
    # surfaces. Test-KgWriteAllowed emitted the dropped-write metric +
    # the UPDATE_DEFERRED.md remediation entry on this path; both are
    # idempotent (sentinel-guarded per session), so emitting them once
    # synchronously here at schedule time is equivalent — they are a
    # "your project_id is missing" notification, not a per-sync gate
    # decision. The actual write/deny gate still runs at sync time via
    # the resolver for the project-present case below.
    if (-not $Project) {
        if ($Collection) {
            Emit-KgGateSkippedMetric -Collection $Collection
            Emit-KgGateSkippedDeferral -Collection $Collection
        }
        return $SyncExpr   # fall open (legacy silent-allow)
    }
    # No collection context, or no resolver on disk → fall open (allow).
    # Identical to Test-KgWriteAllowed's early returns.
    if ((-not $Collection) -or (-not $script:VcoRouteAccessCheckPs1)) {
        return $SyncExpr
    }
    $pEsc = $Project    -replace "'", "''"
    $cEsc = $Collection -replace "'", "''"
    $rEsc = $script:VcoRouteAccessCheckPs1 -replace "'", "''"
    $psEsc = $script:VcoRoutePsExe -replace "'", "''"
    # Inline gate: invoke resolver; allow on null/error (fail-open) OR
    # exact "write"; deny otherwise.
    return @"
`$lvl = `$null
try { `$lvl = & '$psEsc' -NoProfile -File '$rEsc' '$pEsc' '$cEsc' 2>`$null } catch { `$lvl = `$null }
if ((`$null -eq `$lvl) -or ((([string]`$lvl).Trim()) -eq 'write')) { $SyncExpr }
"@
}


# --- v0.2.102 S16-7: enabled-extra-path membership (cached list, option B) ---
# MUST MATCH _lib/route-touched-path.sh's block of the same name. A code file
# under an ENABLED extra code-graph path goes to a PER-EXTRA queue
# (codegraph_drain_extra_<md5(extra)>_<session>.txt), never the primary
# queue; the Stop hook drains it via `vco_lib.codegraph_extras_refresh
# --queued`. The membership decision is computed by Python (the hub /config
# resolver) and cached as a plain table both shells read -- option B of the
# A>B>C rule, because this runs PER EDIT and the list changes only on a
# launcher-GUI extras toggle.
#   cache : .claude/state/codegraph_extra_paths_cache.txt (one path per line)
#   stale : missing, older than VCO_CODEGRAPH_EXTRAS_LIST_TTL_SECONDS
#           (default 300), or older than launcher.db
# A failed refresh keeps the stale cache; no cache at all -> nothing routes
# to the extra queues (the commit-stamp refresh at Stop still re-indexes).
$script:VcoRouteExtrasCacheName = "codegraph_extra_paths_cache.txt"

function Get-VcoRouteExtrasDbPath {
    if ($Env:VCT_LAUNCHER_DB_PATH) { return $Env:VCT_LAUNCHER_DB_PATH }
    $stateRoot = $null
    if ($Env:VCT_STATE_DIR) { $stateRoot = $Env:VCT_STATE_DIR }
    elseif ($Env:USERPROFILE) { $stateRoot = Join-Path $Env:USERPROFILE ".vct" }
    elseif ($Env:HOME) { $stateRoot = Join-Path $Env:HOME ".vct" }
    if (-not $stateRoot) { return "" }
    return (Join-Path $stateRoot "launcher.db")
}

# $true when the cache is STALE (missing / TTL-expired / older than
# launcher.db), $false when fresh. Soft-fail biases to "stale".
function Test-VcoRouteExtrasCacheStale {
    param([string]$Cache)
    if (-not (Test-Path -LiteralPath $Cache)) { return $true }
    $ttl = 300
    if ($Env:VCO_CODEGRAPH_EXTRAS_LIST_TTL_SECONDS -match '^\d+$') {
        $ttl = [int]$Env:VCO_CODEGRAPH_EXTRAS_LIST_TTL_SECONDS
    }
    try { $cacheItem = Get-Item -LiteralPath $Cache -ErrorAction Stop } catch { return $true }
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [DateTimeOffset]::new($cacheItem.LastWriteTimeUtc).ToUnixTimeSeconds()
    if ($age -ge $ttl) { return $true }
    $db = Get-VcoRouteExtrasDbPath
    if (Test-Path -LiteralPath $db) {
        try {
            $dbItem = Get-Item -LiteralPath $db -ErrorAction Stop
            if ($dbItem.LastWriteTimeUtc -gt $cacheItem.LastWriteTimeUtc) { return $true }
        } catch { }
    }
    return $false
}

# Best-effort cache rewrite through the ONE Python lister. Never throws: a
# non-zero CLI exit (hub down / unregistered) leaves any previous cache.
function Update-VcoRouteExtrasCache {
    param([string]$Cache)
    $venvPy = Resolve-VcoVenvPython -ScriptDir $script:VcoRouteHooksDir
    if (-not $venvPy) { return }
    $tmp = "$Cache.tmp.$PID"
    try {
        $out = & $venvPy -m vco_lib.codegraph_extras_refresh `
            --list-enabled-extra-paths --project-root $script:VcoRouteProjectRoot 2>$null
        if ($LASTEXITCODE -eq 0) {
            Set-VcoUtf8Content -Path $tmp -Value ($out -join "`n")
            Move-Item -LiteralPath $tmp -Destination $Cache -Force -ErrorAction Stop
        } else {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

# Resolve symlinks component-by-component (the .sh sibling's `cd -P`
# equivalent, v0.2.102 L-A2): registration canonicalises extras via
# dunce::canonicalize, but the touched path arrives as the editor wrote it;
# an edit through a symlinked directory must still prefix-match. Pure .NET,
# no subprocess; on any failure returns the input unchanged.
function Resolve-VcoRoutePhysicalPath {
    param([string]$Path)
    try {
        $dir = [System.IO.Path]::GetDirectoryName($Path)
        if (-not $dir) { return $Path }
        $leaf = [System.IO.Path]::GetFileName($Path)
        $root = [System.IO.Path]::GetPathRoot($dir)
        if (-not $root) { return $Path }
        $cur = $root
        $rel = $dir.Substring($root.Length)
        foreach ($seg in $rel.Split([System.IO.Path]::DirectorySeparatorChar, [StringSplitOptions]::RemoveEmptyEntries)) {
            $cur = Join-Path $cur $seg
            $di = [System.IO.DirectoryInfo]::new($cur)
            if ($di.LinkTarget) {
                $t = $di.ResolveLinkTarget($true)
                if ($t) { $cur = $t.FullName }
            }
        }
        return (Join-Path $cur $leaf)
    } catch { return $Path }
}

# Route a code edit under an ENABLED extra path to its per-extra drain queue:
# appends the PHYSICAL path to codegraph_drain_extra_<md5(extra)>_<session>.txt
# and returns that queue path; returns $null (caller then uses the primary
# queue) when the edit is not under any enabled extra.
#
# v0.2.102 fix-round (M-A1) precedence, MUST MATCH route-touched-path.sh:
#   1. PRIMARY-ROOT CONTAINMENT WINS -- an edit inside the project root always
#      goes to the primary queue, even when a registered extra is an ANCESTOR
#      of the root (registration rejects ancestor extras too, but a pre-fix
#      row can still be on disk).
#   2. Among extras the LONGEST matching prefix wins -- nested extras route to
#      the inner one regardless of cache order.
#   3. Matching is on the SYMLINK-RESOLVED path; trailing separators on the
#      cache line are ignored for the match but the RAW line is hashed (the
#      queue name must equal vco_lib.path_key(row.path), trailing slash and
#      all, or the Python drain's glob never finds it).
function Add-VcoRouteExtraQueue {
    param([string]$Path, [string]$SessionId)
    $stateDir = Join-Path (Join-Path $script:VcoRouteProjectRoot ".claude") "state"

    # 1. Primary-root containment wins -- checked on the resolved path so a
    # symlinked route INTO the root also lands in the primary queue.
    $resolved = Resolve-VcoRoutePhysicalPath -Path $Path
    $projSep = $script:VcoRouteProjectRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($resolved.StartsWith($projSep, [StringComparison]::OrdinalIgnoreCase)) { return $null }

    $cache = Join-Path $stateDir $script:VcoRouteExtrasCacheName
    if (Test-VcoRouteExtrasCacheStale -Cache $cache) {
        if (-not (Test-Path -LiteralPath $stateDir)) {
            New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        Update-VcoRouteExtrasCache -Cache $cache
    }
    if (-not (Test-Path -LiteralPath $cache)) { return $null }
    $lines = @()
    try { $lines = @(Get-Content -LiteralPath $cache -ErrorAction Stop) } catch { return $null }
    # 2. Longest matching prefix wins.
    $bestRaw = $null
    $bestLen = 0
    foreach ($ep in $lines) {
        $ep = ([string]$ep).TrimEnd("`r")
        if (-not $ep) { continue }
        # 3. Trailing separators ignored for the MATCH; the raw line is hashed.
        $epSep = $ep.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if ($epSep.Length -eq 1) { continue }  # the line was all separators
        if ($resolved.StartsWith($epSep, [StringComparison]::OrdinalIgnoreCase)) {
            if ($epSep.Length -gt $bestLen) { $bestLen = $epSep.Length; $bestRaw = $ep }
        }
    }
    if (-not $bestRaw) { return $null }
    # MD5 of the RAW extra path -- the SAME key vco_lib.path_key() computes,
    # so the queue name matches the Python drain's glob.
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $digest = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($bestRaw))
        $hash = -join ($digest | ForEach-Object { $_.ToString('x2') })
    } finally {
        $md5.Dispose()
    }
    $queue = Join-Path $stateDir ("codegraph_drain_extra_{0}_{1}.txt" -f $hash, $SessionId)
    try { Add-Content -LiteralPath $queue -Value $resolved -ErrorAction SilentlyContinue } catch { }
    return $queue
}


# Should this code edit be listed in the end-of-turn "[Code edit reminder]"?
# v0.2.104 E-1. The reminder is drained by stop-codegraph-reminder.ps1, a
# STOP hook -- it fires for the MAIN agent only (a subagent ends on
# SubagentStop, which drains nothing). A subagent's PostToolUse payload
# carries the PARENT's session_id plus a non-empty agent_id, so before this
# fix every lane edit (in its own worktree, in a temp dir) landed in the
# parent's accumulator.
#   - AgentId non-empty -> NOT listed (skip, rather than key per agent: a
#     per-agent accumulator would have no drainer and grow forever).
#   - path outside the project root (raw OR symlink-resolved) and not under
#     an enabled extra code folder -> NOT listed.
# The code-graph drain queues are NOT gated by this. MUST MATCH
# _vco_route_reminder_wanted in route-touched-path.sh.
function Test-VcoRouteReminderWanted {
    param([string]$Path, [string]$AgentId = "", [string]$ExtraQueue = "")
    if ($AgentId) { return $false }
    if ($ExtraQueue) { return $true }
    # Containment: Test-VcoPathUnderRoot (_lib/project-root.ps1, the one
    # home); absent helper (partial install) -> not listed.
    if (-not (Get-Command Test-VcoPathUnderRoot -ErrorAction SilentlyContinue)) { return $false }
    return (Test-VcoPathUnderRoot -Root $script:VcoRouteProjectRoot -Path $Path)
}

# Invoke-VcoRouteTouchedPath: the whole routing decision for ONE file.
# Idempotent per file per debounce window; safe to call in a loop.
function Invoke-VcoRouteTouchedPath {
    param([string]$Path, [string]$SessionId = "", [string]$AgentId = "")

    if (-not $Path) { return }
    if (-not $script:VcoRouteProjectRoot) { return }
    $ProjectRoot = $script:VcoRouteProjectRoot
    $ScriptDir = $script:VcoRouteHooksDir
    $PsExe = $script:VcoRoutePsExe
    $VctProjectId = $script:VcoRouteProjectId
    $KnowledgeRootSep = $script:VcoRouteKnowledgeSep
    $DocsDirSep = $script:VcoRouteDocsSep

    # 1. Knowledge graph auto-sync (background side-effect).
    # D-9: require the trailing separator so knowledge_base/ etc. don't match.
    if ($Path.StartsWith($KnowledgeRootSep, [StringComparison]::OrdinalIgnoreCase)) {
        $relPath = $Path
        if ($Path.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
            $relPath = $Path.Substring($ProjectRoot.Length).TrimStart('\','/')
        }

        # v0.2.49 Phase 8: gate the sync on access-matrix write permission.
        # KG_COLLECTION is the target Weaviate class for primary-KG writes.
        # 2026-06-18: debounced. The gate runs at SYNC time (inside the job's
        # command), not at schedule time, so a coalesced burst consults the
        # access matrix exactly once when the deferred sync fires. The sync
        # re-reads the file from disk → latest content lands.
        $kgSyncPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-sync.ps1"
        $kgSyncSh = Join-Path $ProjectRoot ".claude/scripts/kg-sync"
        $relEsc = $relPath -replace "'", "''"
        $syncExpr = $null
        if (Test-Path $kgSyncPs1) {
            $ps1Esc = $kgSyncPs1 -replace "'", "''"
            $psEscape = $PsExe -replace "'", "''"
            $syncExpr = "& '$psEscape' -NoProfile -File '$ps1Esc' '$relEsc' *> `$null"
        } elseif ((Test-Path $kgSyncSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            $shEsc = $kgSyncSh -replace "'", "''"
            $syncExpr = "& bash '$shEsc' '$relEsc' *> `$null"
        }
        if ($syncExpr) {
            $kgCmd = Build-GatedSyncCommand -Project $VctProjectId -Collection $Env:KG_COLLECTION -SyncExpr $syncExpr
            Invoke-KgDebounceSchedule -ProjectRoot $ProjectRoot -FilePath $Path -WorkingDir $ProjectRoot -Command $kgCmd -Channel "kg"
        }

        # Duplicate detection every 10 edits.
        $editCountFile = Join-Path $ProjectRoot ".claude/logs/.kg_edit_count"
        $logsDir = Split-Path $editCountFile -Parent
        if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
        $count = 0
        if (Test-Path $editCountFile) {
            try { $count = [int](Get-Content $editCountFile -Raw -ErrorAction Stop).Trim() } catch { $count = 0 }
        }
        $count++
        Set-Content -Path $editCountFile -Value $count -Encoding ascii

        if (($count % 10) -eq 0) {
            # D-8 (v0.2.73): capture the summary into a report file (previously
            # the scan ran hidden and its output was discarded — inert feature).
            # The next KG-file edit surfaces + consumes the report.
            #
            # v0.2.92 (R42 / MAJOR-1): the PowerShell wrapper SHIPS now
            # (templates/scripts/kg-duplicates.ps1 -> .claude/scripts/), so it is
            # the FIRST branch — same shape the kg-sync spawn above uses. The
            # comment this replaced denied the sibling's existence and gated
            # the whole feature on `Get-Command bash`, which left every
            # native-Windows-without-bash machine with no duplicate detection at
            # all while a written, tested .ps1 sat beside it. Parity is achieved by
            # CALLING the .ps1, not by narrowing the feature; the bash wrapper
            # stays as the fallback for a project whose bundle predates the
            # sibling.
            #
            # DETACHED via Start-Process, NOT Start-Job: a job's child is torn
            # down when this hook's PowerShell host exits (measured on Linux pwsh
            # 7 — a 300 ms job never ran), and the scan is a whole-collection
            # Weaviate query that always outlives the hook, so the report was
            # never written even on machines that DID have bash. This is the same
            # reasoning _lib/kg-sync-debounce.ps1 records for its flusher; the
            # bash sibling's `( ... ) &` subshell already survives, so this also
            # restores .sh/.ps1 parity.
            $dupPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates.ps1"
            $dupSh = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates"
            $dupReport = Join-Path $ProjectRoot ".claude/state/kg_duplicates_report.txt"
            $dupExpr = $null
            if (Test-Path $dupPs1) {
                $psEscape = $PsExe -replace "'", "''"
                $dupEsc = $dupPs1 -replace "'", "''"
                $dupExpr = "& '$psEscape' -NoProfile -File '$dupEsc' '--threshold' '0.95' 2>&1"
            } elseif ((Test-Path $dupSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
                $shEsc = $dupSh -replace "'", "''"
                $dupExpr = "& bash '$shEsc' '--threshold' '0.95' 2>&1"
            }
            if ($dupExpr) {
                $stateDir = Split-Path $dupReport -Parent
                if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
                $reportEsc = $dupReport -replace "'", "''"
                $rootEsc = $ProjectRoot -replace "'", "''"
                $nobomEsc = $script:VcoUtf8NoBomLib -replace "'", "''"
                $dupChild = @"
Set-Location -LiteralPath '$rootEsc' -ErrorAction SilentlyContinue
. '$nobomEsc'
try {
    # ❌ kept too — the scan's failure line is what the ⚠️ "See the error
    # above." verdict points at (parity with the bash sibling's filter).
    #
    # v0.2.94 (review item 3): `^<tool>: ERROR` joins the pattern. A wrapper
    # REFUSAL carried none of the four markers, so this scan was a silent
    # no-op on exactly the installs that had something to report.
    `$out = $dupExpr | Select-String -Pattern '✅|⚠️|📊|❌|^[A-Za-z0-9_-]+: ERROR' | ForEach-Object { `$_.ToString() }
    if (`$out) {
        `$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Set-VcoUtf8Content -Path '$reportEsc' -Value (@("# KG duplicate scan (every-10-edits, `$ts)") + `$out)
    }
} catch { }
"@
                Start-VcoDetachedPwsh -Command $dupChild -PowerShellExe $PsExe
            }
        }

        # D-8: surface a PENDING duplicate-scan report (from a prior fire)
        # through the additionalContext envelope, then consume it.
        $dupReport = Join-Path $ProjectRoot ".claude/state/kg_duplicates_report.txt"
        if (Test-Path $dupReport) {
            $dupBody = (Get-Content $dupReport -Raw -ErrorAction SilentlyContinue)
            if ($dupBody) {
                Add-VcoRouteNudge "[KG duplicate scan] The periodic duplicate check found candidates worth reviewing:`n$dupBody`nRun .claude/scripts/kg-duplicates for detail, or ignore if these are intentional siblings."
            }
            Remove-Item $dupReport -Force -ErrorAction SilentlyContinue
        }
    }

    # 2. Docs auto-sync (background side-effect).
    # v0.2.46 post-adversarial: the shared resolver (dot-sourced once by
    # Initialize-VcoRoute) instead of the inline VCT_INSTALL_ROOT-or-ProjectRoot
    # fallback, which pointed at the USER's venv (no vco_lib + weaviate-client).
    if ($Path.StartsWith($DocsDirSep, [StringComparison]::OrdinalIgnoreCase) -and ($Path -like "*.md")) {
        # v0.2.49 Phase 8: gate docs sync on access-matrix write permission
        # against DEVELOPMENT_COLLECTION (the docs/ target).
        # 2026-06-18: debounced (same coalesce semantics + sync-time gate as
        # the knowledge/ branch above).
        # v0.2.97 (promise sweep B1): this branch used to invoke the
        # retired upload_docs.py — a script nothing ships, so Windows docs
        # auto-sync silently never ran. Mirror
        # the .sh sibling exactly: route docs/*.md through kg-sync
        # (kg-sync.ps1 preferred, bash kg-sync fallback), which targets the
        # development collection the same way the bash branch does.
        $relPath = $Path
        if ($Path.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
            $relPath = $Path.Substring($ProjectRoot.Length).TrimStart('\','/')
        }
        $kgSyncPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-sync.ps1"
        $kgSyncSh = Join-Path $ProjectRoot ".claude/scripts/kg-sync"
        $relEsc = $relPath -replace "'", "''"
        $syncExpr = $null
        if (Test-Path $kgSyncPs1) {
            $ps1Esc = $kgSyncPs1 -replace "'", "''"
            $psEscape = $PsExe -replace "'", "''"
            $syncExpr = "& '$psEscape' -NoProfile -File '$ps1Esc' '$relEsc' *> `$null"
        } elseif ((Test-Path $kgSyncSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            $shEsc = $kgSyncSh -replace "'", "''"
            $syncExpr = "& bash '$shEsc' '$relEsc' *> `$null"
        }
        if ($syncExpr) {
            $docsCmd = Build-GatedSyncCommand -Project $VctProjectId -Collection $Env:DEVELOPMENT_COLLECTION -SyncExpr $syncExpr
            Invoke-KgDebounceSchedule -ProjectRoot $ProjectRoot -FilePath $Path -WorkingDir $ProjectRoot -Command $docsCmd -Channel "docs"
        }
    }

    # 2b. Diagram auto-index: REMOVED in v0.2.102 with the diagrams
    # integrations (owner ruling 2026-10-07) — mirror of the .sh sibling.

    # 3. Code file changes: code graph incremental update + LLM nudge.
    # v0.2.21 Step 18 (caller migration): resolve the code-graph collection
    # prefix via the launcher's vct-hub first (`vct_project_config.ps1 -Field
    # code_graph_collection_prefix`); fall back to the legacy env chain when
    # the hub is unreachable. Mirrors the .sh sibling at the same line.
    #
    # v0.2.23 field switch: previously this read `code_graph_project`, which
    # the hub returns as a legacy alias for `project_slug` — NOT the canonical
    # Weaviate prefix. The analyzer's `_sanitize_collection_prefix` then
    # re-canonicalised the slug, producing a prefix that diverged from the
    # launcher's `project_codegraph_bindings.collection_prefix`. Incremental
    # writes landed in zombie collections (e.g. `Orchestrator_root_Code*`)
    # while consumers queried the canonical prefix and saw 0 results.
    # `code_graph_collection_prefix` is the binding-row truth and the only
    # correct source for the write target.
    #
    # Pre-v0.2.11 behaviour hardcoded "ClaudeOrchestrator" here, which
    # polluted the legacy collection from every project install. Do NOT
    # re-introduce a hardcoded literal in this position.
    # v0.2.95: the extension test comes from _lib/code-extensions.ps1 (ONE
    # home). A partial install without that helper routes NOTHING to the drain
    # rather than matching every path on an empty regex -- and SAYS so
    # (ship-gate review MAJOR-2: routing no code file at all, silently and by
    # stated choice, is the same silent-no-op class as a missing routing lib).
    # The notice goes through this lib's existing nudge channel, so the
    # calling hook still emits exactly ONE envelope.
    if (-not (Get-Command Test-VcoIsCodeFile -ErrorAction SilentlyContinue)) {
        if (Get-Command Emit-VcoMissingHookLibNotice -ErrorAction SilentlyContinue) {
            $missingCodeExt = Emit-VcoMissingHookLibNotice -HooksDir $ScriptDir `
                -ProjectRoot $ProjectRoot -SessionId $SessionId -Lib "code-extensions.ps1"
            if ($missingCodeExt) { Add-VcoRouteNudge $missingCodeExt }
        }
    }
    elseif (Test-VcoIsCodeFile $Path) {
        $bn = Split-Path $Path -Leaf
        # v0.2.73 (FIX-B, MUST MATCH post-file-edit.sh): the per-EDIT code-graph
        # sync is REMOVED. It used to Invoke-KgDebounceSchedule a
        # code-graph-incremental.ps1 run on every edit; each run hit the big
        # CodeFunction collection's insert-time churn, and parallel worktrees
        # multiplied it into the measured Weaviate disk write-amplification.
        # Instead, the edited path is appended to a per-turn drain queue below and
        # drained ONCE at end-of-turn (stop-codegraph-drain.ps1) over ALL the
        # turn's files in one analyzer pass, rate-limited to once per 120s per
        # project. KG/docs debounce paths are UNCHANGED (this fix targets the CODE
        # path only, the amplifier).
        # v0.2.72 P6 + v0.2.73 FIX-B: append the edited path to BOTH the reminder
        # accumulator (drained + cleared EVERY turn by stop-codegraph-reminder.ps1)
        # AND the code-graph drain queue (drained by the rate-limited
        # stop-codegraph-drain.ps1, which persists the union across rate-limited
        # turns). Distinct files avoid a two-consumer race. Soft-fail: unkeyable
        # session / write error just skips.
        if ($SessionId) {
            $stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
            try {
                if (-not (Test-Path -LiteralPath $stateDir)) {
                    New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction Stop | Out-Null
                }
                # v0.2.102 S16-7: a code file under an ENABLED extra code-graph
                # path goes to the PER-EXTRA queue instead -- a path outside
                # the primary root must never be analyzed under the primary's
                # collection. Drained by stop-codegraph-drain.ps1 through
                # `vco_lib.codegraph_extras_refresh --queued`. MUST MATCH the
                # .sh sibling. Add-VcoRouteExtraQueue appends the RESOLVED path
                # itself and returns the queue it used; $null -> primary queue.
                # Precedence (M-A1): primary-root containment wins, then
                # longest extra prefix.
                $extraQueue = Add-VcoRouteExtraQueue -Path $Path -SessionId $SessionId
                if (-not $extraQueue) {
                    $cgQueue = Join-Path $stateDir ("codegraph_drain_{0}.txt" -f $SessionId)
                    Add-Content -LiteralPath $cgQueue -Value $Path -ErrorAction SilentlyContinue
                }
                # v0.2.104 E-1: only the MAIN agent's edits inside the project
                # (its root, or an enabled extra code folder) feed the
                # reminder. The drain queues above are unaffected.
                if (Test-VcoRouteReminderWanted -Path $Path -AgentId $AgentId -ExtraQueue $extraQueue) {
                    $accum = Join-Path $stateDir ("edit_reminder_{0}.txt" -f $SessionId)
                    Add-Content -LiteralPath $accum -Value $Path -ErrorAction Stop
                }
            } catch { }
        }
    }

}
