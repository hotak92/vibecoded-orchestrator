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
#   Invoke-VcoRouteTouchedPath -Path <abs> -SessionId <id>
#   # then: emit $script:VcoRouteNudge through Emit-AdditionalContext.
#
# Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoRouteTouchedPathSourced) { return }
$script:VcoRouteTouchedPathSourced = $true

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
    # form the projection's managed block writes -- first match wins, one
    # matching pair of quotes stripped. MUST MATCH route-touched-path.sh
    # (parity test: tests/test_v0297_route_project_id_parity.py).
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
                        $script:VcoRouteProjectId = $v
                        break
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
        $ts = [int][double]::Parse((Get-Date -UFormat %s))
        $row = @{
            ts          = $ts
            project_id  = ""
            collection  = $Collection
            reason      = "gate_skipped_no_project_id"
            fail_open   = $true
        } | ConvertTo-Json -Compress
        Add-Content -Path $jsonl -Value $row -Encoding utf8 -ErrorAction SilentlyContinue
    } catch {
        # Metric-emit failure must not break the silent-allow contract.
    }
}

# v0.2.49 SB1: write an UPDATE_DEFERRED.md entry directing the user to
# resolve the empty-VCT_PROJECT_ID condition (run install.py --update
# OR re-register via Launcher GUI). Per user Q1 (2026-06-08), this is
# the user-facing surface — no stderr WARNING by default.
#
# Idempotent per (session, project) via a sentinel file in
# .claude/state/. Mirrors the bash sibling's
# _kg_emit_gate_skipped_deferral exactly.
function Emit-KgGateSkippedDeferral {
    param([string]$Collection)
    $deferred = Join-Path $script:VcoRouteProjectRoot ".claude/context/UPDATE_DEFERRED.md"
    $stateDir = Join-Path $script:VcoRouteProjectRoot ".claude/state"
    $sessionId = if ($Env:VCT_SESSION_ID) { $Env:VCT_SESSION_ID } `
                 elseif ($Env:CLAUDE_SESSION_ID) { $Env:CLAUDE_SESSION_ID } `
                 else { [string]$PID }
    $sentinel = Join-Path $stateDir "gate_skipped_deferral_$sessionId"

    # Per-session dedup. First call writes; subsequent calls in the same
    # session are silent no-ops.
    if (Test-Path $sentinel) { return }

    try {
        if (-not (Test-Path $stateDir)) {
            New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        Set-Content -Path $sentinel -Value "" -ErrorAction SilentlyContinue
    } catch { return }

    try {
        $deferredDir = Split-Path $deferred -Parent
        if (-not (Test-Path $deferredDir)) {
            New-Item -ItemType Directory -Path $deferredDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
    } catch { return }

    # Idempotent body marker — if a prior session wrote a row for this
    # condition_id, leave it in place.
    $marker = "## gate_skipped_no_project_id"
    if ((Test-Path $deferred) -and (Select-String -Path $deferred -SimpleMatch -Pattern $marker -Quiet -ErrorAction SilentlyContinue)) {
        return
    }

    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

    # Append-mode write. Without frontmatter the deferral parser still
    # finds the entry via "^## <cid> (sev)"; the next install.py
    # --update pass canonicalises the file with a header.
    $body = @"

$marker (warning)

**Title**: Phase-8 access-matrix gate skipped (VCT_PROJECT_ID missing from hook env)

**Detected**: A VCO write hook reached the Phase-8 WRITE gate with no VCT_PROJECT_ID. The Phase-8 WRITE gate cannot identify this project against the hub access matrix, so the write was permitted via the silent-allow path. Target collection: $Collection

**Why deferred**: Seeding VCT_PROJECT_ID requires an orchestrator install pass (queries launcher.db for the project UUID) or a Launcher GUI re-registration. The hook cannot self-heal.

**To apply**:
``````bash
# Option A — orchestrator-root install / update:
python install.py --update

# Option B — per-project (pre-v0.2.49 install): re-register the
# project via Launcher GUI -> Projects -> Identity tab. The
# launcher's apply_project_env pass seeds VCT_PROJECT_ID into
# the project-local .claude/env from launcher.db.
``````

**Detected at**: $ts

---
"@
    try {
        Add-Content -Path $deferred -Value $body -Encoding utf8 -ErrorAction SilentlyContinue
    } catch {
        # Silent failure: the silent-allow contract is the priority.
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


# Invoke-VcoRouteTouchedPath: the whole routing decision for ONE file.
# Idempotent per file per debounce window; safe to call in a loop.
function Invoke-VcoRouteTouchedPath {
    param([string]$Path, [string]$SessionId = "")

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
                $dupChild = @"
Set-Location -LiteralPath '$rootEsc' -ErrorAction SilentlyContinue
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
        Set-Content -Path '$reportEsc' -Value (@("# KG duplicate scan (every-10-edits, `$ts)") + `$out) -Encoding utf8
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

    # 2b. Auto-index diagrams (Phase 1.5 — Mermaid + Excalidraw). Mirror of
    # the .sh sibling's same-numbered branch. 60s per-file throttle, skips
    # sidecar .meta.json writes (would infinite-loop), notifies vct-hub for
    # UI refresh (404 silently swallowed until Phase 1.2 broadcast route).
    $DiagramsDir = Join-Path $ProjectRoot ".claude/diagrams"
    if ($Path.StartsWith($DiagramsDir, [StringComparison]::OrdinalIgnoreCase) `
        -and ($Path -notlike "*.meta.json") `
        -and (($Path -like "*.mmd") -or ($Path -like "*.excalidraw"))) {

        $throttleDir = Join-Path $ProjectRoot ".claude/state"
        if (-not (Test-Path $throttleDir)) {
            New-Item -ItemType Directory -Path $throttleDir -Force | Out-Null
        }

        # MD5 hash of file path → throttle key (no slashes).
        $md5 = [System.Security.Cryptography.MD5]::Create()
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($Path)
            $digest = $md5.ComputeHash($bytes)
            $diagramHash = -join ($digest | ForEach-Object { $_.ToString('x2') })
        } finally {
            $md5.Dispose()
        }
        $throttleFile = Join-Path $throttleDir "diagram_idx_${diagramHash}.ts"

        # TRUE Unix seconds. MUST MATCH the .sh sibling's `date +%s` — both write
        # and read the SAME `.claude/state/diagram_idx_<hash>.ts` file, so a
        # timezone-shifted value here breaks the throttle across siblings (on a
        # WSL/mixed install the .ps1 read a .sh stamp as `now - last == +offset`
        # and re-indexed on EVERY edit). `(Get-Date) - (Get-Date "1970-01-01Z")`
        # subtracts a UTC instant from a LOCAL one and is off by the UTC offset;
        # .NET subtracts raw ticks and ignores DateTimeKind, so mixing kinds never
        # errors, it just silently returns the wrong number.
        $nowTs = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $lastTs = 0
        if (Test-Path $throttleFile) {
            try {
                $raw = (Get-Content $throttleFile -Raw -ErrorAction Stop).Trim()
                if ($raw -match '^[0-9]+$') { $lastTs = [int]$raw }
            } catch { $lastTs = 0 }
        }

        if (($nowTs - $lastTs) -ge 60) {
            Set-Content -Path $throttleFile -Value $nowTs -Encoding ascii -ErrorAction SilentlyContinue

            # v0.2.46 post-adversarial: shared resolver (already dot-sourced
            # above at the docs branch). Falls back to system python ONLY when
            # no VCO venv is resolvable — never to the USER's project venv.
            $diagVenv = Resolve-VcoVenvPython -ScriptDir $ScriptDir
            if (-not $diagVenv) {
                $diagVenv = (Get-Command python -ErrorAction SilentlyContinue).Source
            }

            if ($diagVenv) {
                # Build the indexer arguments. Pass --diagrams-collection
                # when DIAGRAMS_COLLECTION is set in the env (fix/a1-indexing-
                # pipeline 2026-05-25). Without this kwarg the indexer's
                # Weaviate upsert silently skips (Bug-1 of the wiring audit).
                # Older projects without DIAGRAMS_COLLECTION in env keep the
                # legacy sidecar-only behaviour automatically.
                $diagArgs = @('-m', 'vco_lib.diagram_indexer', 'index', $Path)
                if ($env:DIAGRAMS_COLLECTION) {
                    $diagArgs += @('--diagrams-collection', $env:DIAGRAMS_COLLECTION)
                }

                # Serialized index + snapshot in ONE detached child.
                #
                # R2 (code review 2026-05-25): previously two separate
                # Start-Process calls ran in parallel; the snapshot CLI's
                # `project_diagrams WHERE file_path=?` query returned no
                # row on first-edit-per-file because the indexer UPSERT
                # hadn't committed yet → first-version snapshot lost
                # forever. SEQUENCING IS LOAD-BEARING: index must complete
                # before snapshot runs. The two CLIs are therefore emitted
                # as consecutive statements in a single child process.
                #
                # v0.2.92 R2: that fix used Start-Job, which reintroduced the
                # loss by a different route — a job's child is torn down when
                # the host process exits, and this hook exits within
                # milliseconds while the indexer's Weaviate upsert takes
                # seconds. So on Windows NEITHER CLI completed. Identical
                # defect to the duplicate-scan branch above; same remedy, and
                # the same one home for the spawn (`Start-VcoDetachedPwsh`).
                # The .sh sibling's `( ... ) &` subshell already survives, so
                # this also restores .sh/.ps1 parity.
                $snapArgs = @(
                    '-m', 'vco_lib.diagram_indexer',
                    'snapshot', 'create', $Path, '--quiet'
                )
                $diagPyEsc = $diagVenv -replace "'", "''"
                $diagRootEsc = $ProjectRoot -replace "'", "''"
                $idxLit = ($diagArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
                $snapLit = ($snapArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
                $diagChild = @"
Set-Location -LiteralPath '$diagRootEsc' -ErrorAction SilentlyContinue
`$vp = '$diagPyEsc'
`$ia = @($idxLit)
`$sa = @($snapLit)
try {
    & `$vp @ia *> `$null
    & `$vp @sa *> `$null
} catch { }
"@
                Start-VcoDetachedPwsh -Command $diagChild -PowerShellExe $PsExe
            }

            # Live UI refresh in DiagramsTab is driven by the launcher's
            # frontend file-watcher (chokidar) — NOT a hub broadcast. The
            # original Phase 1.5.A design called for /api/v1/notify/diagram-changed
            # but pub/sub from hub → frontend would need SSE/WebSocket
            # plumbing the launcher does not have today. Re-evaluate if
            # multi-machine notification ever becomes a real requirement.
        }
    }

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
                $accum = Join-Path $stateDir ("edit_reminder_{0}.txt" -f $SessionId)
                Add-Content -LiteralPath $accum -Value $Path -ErrorAction Stop
                $cgQueue = Join-Path $stateDir ("codegraph_drain_{0}.txt" -f $SessionId)
                Add-Content -LiteralPath $cgQueue -Value $Path -ErrorAction SilentlyContinue
            } catch { }
        }
    }

}
