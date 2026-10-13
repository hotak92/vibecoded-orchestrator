# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# OS-EXEMPT-PARITY: this .ps1 carries a UTF-8 BOM its .sh sibling must NOT have (PS 5.1 encoding) — the two legitimately diverge on that byte, so a BOM-only edit need not touch the .sh. Keep the LOGIC in lockstep by hand.
# worktree-guard.ps1 — Windows sibling of worktree-guard.sh. WorktreeCreate
# hook (Layer 0, primary deterministic gate) for the worktree-isolation
# safeguard.
#
# ── Cross-OS parity (MUST match worktree-guard.sh) ────────────────────────
# Same decision matrix, same path convention
# (<toplevel>/.claude/worktrees/<sanitized-id>), same
# `git worktree add --detach <path> HEAD`, same idempotent-re-fire handling,
# same stdout/exit semantics. Any change to the decision logic in
# worktree-guard.sh MUST be mirrored here and vice versa — keep them in
# lockstep.
#
# ── What the WorktreeCreate contract ACTUALLY is (verified 2026-07-06) ────
# Per the official Claude Code Hooks Reference
# (https://code.claude.com/docs/en/hooks.md): the hook is RESPONSIBLE FOR
# CREATING the worktree ("Replaces default git behavior"), not merely
# validating a path. The stdin payload carries {session_id, transcript_path,
# cwd, hook_event_name} plus a worktree IDENTIFIER — the docs name it
# `worktree_name`, the live harness on the pinned build sends `name` (the
# agent id). THERE IS NO PROPOSED-PATH FIELD in the real payload. stdout =
# the absolute worktree path (plain line); exit 0 = success (worktree MUST
# exist); ANY non-zero exit ABORTS the create.
#
# ── The bug this version fixes ───────────────────────────────────────────
# The previous implementation was a VALIDATOR that no-op'd when no path was
# present (always, for the real payload) → the worktree was never created →
# the subagent silently fell back to the shared parent tree. This version
# CREATES the worktree.
#
# -- Lane reservations (v0.2.102 W6-AA) ---------------------------------------
# Before the repository checks, a pending lane reservation (vco_lib/lane_reservation.py)
# is consumed for an `agent-*` identifier: the worktree is created in the
# reserved repo/base/branch, or the reserved existing worktree is adopted; a
# failed reservation aborts the create loudly. Same as worktree-guard.sh.
#
# ── VCT_WORKTREE_GUARD_ENFORCE (staged-enable flag — now vestigial) ───────
# Creation is now the DEFAULT (ungated); a failed create always aborts
# loudly. The flag only affects the belt-and-suspenders explicit-path branch:
# when a path IS supplied AND equals the parent toplevel, ENFORCE hard-blocks
# (exit non-zero) while the default derives a safe separate path. For the real
# (no-path) payload the flag has no effect.
#
# ── Tunables (identical to .sh) ───────────────────────────────────────────
#   VCT_DISABLE_HOOKS              — global bypass.
#   VCT_WORKTREE_GUARD_ENFORCE=1   — hard-block the explicit-path==parent case
#                                    (belt-and-suspenders branch only; no
#                                    effect on the real no-path payload).

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# Project root: the ONE rule (_lib/project-root.ps1, must match
# vco_lib/project_root.py and _lib/project-root.sh). "" when no VCO project
# encloses the current location: the worktree decision still runs, but the
# log row, notices and launch records (state writes) are skipped.
. (Join-Path $ScriptDir "_lib/project-root.ps1")
$ProjectRoot = Get-VcoProjectRoot
$LogDir = ""
$LogFile = ""
if ($ProjectRoot) {
    $LogDir = Join-Path $ProjectRoot ".claude/logs"
    $LogFile = Join-Path $LogDir "worktree-guard.jsonl"
    try { New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction SilentlyContinue | Out-Null } catch { }
} else {
    [Console]::Error.WriteLine("worktree-guard: no VCO project encloses $((Get-Location).Path); log, notices and launch records skipped")
}

# ── Read stdin (the WorktreeCreate payload) ───────────────────────────────
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

# Emit-WtPath: satisfy the stdout contract. Empty arg → emit nothing.
function Emit-WtPath([string]$p) {
    if ($p) { Write-Output $p }
}

# Log-Event: append a JSONL row. ALWAYS capture the FULL raw payload so the
# integrator can verify the live schema field names + stdout semantics.
function Log-Event([string]$decision, [string]$reason, [string]$proposed, [string]$resolved) {
    try {
        $row = [ordered]@{
            timestamp     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            hook          = "worktree-guard"
            decision      = $decision
            reason        = $reason
            proposed_path = $proposed
            resolved_path = $resolved
            enforce       = [bool]$env:VCT_WORKTREE_GUARD_ENFORCE
            strict        = [bool]$env:VCT_WORKTREE_GUARD_STRICT
            raw_payload   = $HookStdin
        }
        $json = $row | ConvertTo-Json -Compress -Depth 6
        if (-not $LogFile) { return }
        Add-Content -Path $LogFile -Value $json -ErrorAction SilentlyContinue
    } catch { }
}

# ── Parse stdin defensively ───────────────────────────────────────────────
# Extract the worktree IDENTIFIER (worktree_name / name synonyms), the
# explicit proposed path (worktree_path / path / ... — absent in the real
# payload), and a repo hint (cwd / repo_root / ...).
$WtName = ""
$ProposedPath = ""
$RepoHint = ""
if ($HookStdin) {
    try {
        $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
        if ($payload) {
            # Identifier — docs say `worktree_name`; live harness sends `name`.
            foreach ($field in 'worktree_name','name','agent_id','agent_name','id') {
                if ($payload.PSObject.Properties[$field] -and $payload.$field) {
                    $WtName = [string]$payload.$field; break
                }
            }
            # Explicit path — same synonym set as the .sh (parity-pinned).
            foreach ($field in 'worktree_path','path','proposed_path','worktree','target_path','dir') {
                if ($payload.PSObject.Properties[$field] -and $payload.$field) {
                    $ProposedPath = [string]$payload.$field; break
                }
            }
            foreach ($field in 'repo_root','repo','project_root','cwd','toplevel') {
                if ($payload.PSObject.Properties[$field] -and $payload.$field) {
                    $RepoHint = [string]$payload.$field; break
                }
            }
        }
    } catch {
        # Malformed JSON → leave all empty.
    }
}

# Norm-Path: absolutise best-effort (the worktree dir does not exist yet at
# create time, so resolve textually, not via Resolve-Path which requires
# existence).
function Norm-Path([string]$p) {
    try { return [System.IO.Path]::GetFullPath($p) } catch { return $p }
}

# Sanitize-Id: reduce a worktree identifier to a filesystem-safe token. Keep
# [A-Za-z0-9._-]; collapse everything else to '-'; trim/collapse dashes.
# Empty → stable "agent" fallback so we never derive an empty path segment.
# v0.2.74 (M-3): first 8 hex chars of the SHA-256 of the RAW id — mirrors
# worktree-guard.sh's `sha256sum | cut -c1-8` so distinct raw ids that sanitize
# to the same token never share a worktree path (collision → silent tree reuse).
function Get-IdShortHash([string]$raw) {
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($raw)
        $hash = $sha.ComputeHash($bytes)
        $sha.Dispose()
        $hex = -join ($hash | ForEach-Object { $_.ToString('x2') })
        return $hex.Substring(0, 8)
    } catch {
        return '0'
    }
}

function Sanitize-Id([string]$raw) {
    $out = ($raw -replace '[^A-Za-z0-9._-]', '-')
    # Neutralise any surviving `..` run (already one path segment, so it can't
    # traverse — purely for a tidy token) and collapse dot runs.
    $out = ($out -replace '\.{2,}', '.')
    $out = ($out -replace '-+', '-') -replace '^[-.]+', '' -replace '[-.]+$', ''
    if (-not $out) { $out = "agent" }
    # Append the raw-id hash so distinct raw ids never share a token/path (M-3).
    return ('{0}-{1}' -f $out, (Get-IdShortHash $raw))
}

# ── Resolve the git toplevel (the repo this create is scoped to) ──────────
$GitScopeDir = if ($ProjectRoot) { $ProjectRoot } else { (Get-Location).Path }  # a read
if ($RepoHint -and (Test-Path -LiteralPath $RepoHint -PathType Container)) {
    $GitScopeDir = $RepoHint
}
$Toplevel = ""
if (Get-Command git -ErrorAction SilentlyContinue) {
    try {
        $Toplevel = (& git -C $GitScopeDir rev-parse --show-toplevel 2>$null | Select-Object -First 1)
    } catch { $Toplevel = "" }
}

# -- Isolation notices (v0.2.102 W6-Q) - must match worktree-guard.sh --------
# The harness discards a WorktreeCreate systemMessage, so a non-default
# create leaves a one-shot notice keyed by the identifier; the SubagentStart
# hook subagent-start-isolation-check.ps1 hands it to the agent.
$NoticeDir = ""
if ($ProjectRoot) { $NoticeDir = Join-Path (Join-Path (Join-Path $ProjectRoot ".claude") "state") "worktree-guard" }
# Per-launch records (v0.2.102 W6-Q follow-up): one home is
# _lib/worktree-records.ps1 (must match _lib/worktree-records.sh).
$WtrecHelper = Join-Path $PSScriptRoot "_lib/worktree-records.ps1"
if (Test-Path -LiteralPath $WtrecHelper) {
    . $WtrecHelper
    if ($ProjectRoot) { Remove-WtrecStale $ProjectRoot }
}
function Save-LaunchRecord([string]$mode, [string]$path, [string]$base, [string]$toplevel, [string]$extra = "") {
    if (-not $ProjectRoot) { return }
    if (Get-Command Write-WtrecRecord -ErrorAction SilentlyContinue) {
        Write-WtrecRecord $ProjectRoot $WtName $mode $path $base $toplevel $extra
    }
}
function Write-Notice([string]$text) {
    if (-not $NoticeDir) { return }
    if (-not $WtName) { return }
    if ($WtName -notmatch '^[A-Za-z0-9_-][A-Za-z0-9._-]*$') { return }
    try {
        New-Item -ItemType Directory -Force -Path $NoticeDir -ErrorAction Stop | Out-Null
        Set-Content -LiteralPath (Join-Path $NoticeDir "notice-$WtName.txt") -Value $text -Encoding UTF8 -ErrorAction Stop
    } catch { }
}

# -- Lane reservation (v0.2.102 W6-AA) - must match worktree-guard.sh -------
# A coordinator reserves this isolated agent's worktree in another repo first
# (python -m vco_lib.lane_reservation reserve ...). The logic lives ONCE in
# vco_lib/lane_reservation.py; this block only calls it. No pending file =>
# nothing here runs and the guard behaves exactly as before. Consume exit 3 =
# nothing to consume => fall through; any other failure is a LOUD abort, never
# a silent fallback to a worktree of the project. A consume that exits 0
# without its five non-empty lines (path, HEAD, repo, mode, extra JSON) is a
# malformed success: the reservation is already claimed, so it is the same
# loud abort. No VCO-side time bound: `git worktree add` on a very large repo
# can take minutes and must never be killed mid-run; the WorktreeCreate
# registration carries no timeout, so the harness default (600 s) is the
# outer bound.
$LanePending = Join-Path (Join-Path (Join-Path (Join-Path $ProjectRoot ".claude") "state") "lane-reservations") "pending"
if ($WtName -and (Test-Path -LiteralPath $LanePending -PathType Container) -and
    @(Get-ChildItem -LiteralPath $LanePending -File -Filter "res-*.json" -ErrorAction SilentlyContinue).Count -gt 0) {
    $VcoPy = $null
    $VenvHelper = Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1"
    if (Test-Path -LiteralPath $VenvHelper) {
        . $VenvHelper
        $VcoPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
    }
    if (-not $VcoPy) {
        $reason = "broken install: a lane reservation is pending in $LanePending but VCO's Python (vco_lib) cannot be resolved to consume it - repair the install (launcher: project Settings -> Update bundle), or cancel the reservation"
        Log-Event "create_failed" $reason $ProposedPath ""
        [Console]::Error.WriteLine("worktree-guard: ABORT - $reason")
        exit 1
    }
    $LaneOut = @(& $VcoPy -m vco_lib.lane_reservation consume --project $ProjectRoot --name $WtName)
    $LaneRc = $LASTEXITCODE
    if ($LaneRc -eq 0) {
        $LaneFields = @(0..4 | ForEach-Object { if ($_ -lt $LaneOut.Count) { [string]$LaneOut[$_] } else { "" } })
        if (@($LaneFields | Where-Object { -not $_ }).Count -gt 0) {
            $reason = "lane reservation consume reported success but its output was malformed (expected five non-empty lines: path, HEAD, repo, mode, extra JSON) - the reservation was claimed; no worktree of the project was created in its place"
            Log-Event "create_failed" $reason $ProposedPath ""
            [Console]::Error.WriteLine("worktree-guard: ABORT - $reason")
            exit 1
        }
        $LanePath = [string]$LaneOut[0]
        Log-Event "created" "lane_reservation" $ProposedPath $LanePath
        Save-LaunchRecord ([string]$LaneOut[3]) $LanePath ([string]$LaneOut[1]) ([string]$LaneOut[2]) ([string]$LaneOut[4])
        Emit-WtPath $LanePath
        exit 0
    } elseif ($LaneRc -ne 3) {
        $reason = "lane reservation could not be honoured (consume exit $LaneRc; reason above) - no worktree of the project was created in its place"
        Log-Event "create_failed" $reason $ProposedPath ""
        [Console]::Error.WriteLine("worktree-guard: ABORT - $reason")
        exit 1
    }
}

# -- Not a git repo => run in place, isolation OFF, said out loud ------------
# Echoing nothing fails the launch ("If the hook fails or produces no path,
# worktree creation fails with an error" - hooks reference, WorktreeCreate
# output). Return the working directory itself; the harness accepts a
# directory with no git metadata that no repository contains. No
# identifier => plain no-op. Same decision as worktree-guard.sh.
if (-not $Toplevel) {
    if (-not $WtName) {
        Log-Event "noop" "not_a_repo" $ProposedPath ""
        exit 0
    }
    $RunDir = $GitScopeDir
    try { $RunDir = (Resolve-Path -LiteralPath $GitScopeDir -ErrorAction Stop).ProviderPath } catch { }
    Write-Notice "VCO worktree-guard: worktree isolation is OFF for this subagent. $RunDir is not inside a git repository, so no isolated worktree can be created; you are working DIRECTLY in the project folder and every edit lands there immediately. Say this in your final report. Isolation turns on by itself once the folder is a git repository (git init)."
    Log-Event "in_place" "not_a_repo" $ProposedPath $RunDir
    Save-LaunchRecord "in_place" $RunDir "" ""
    [Console]::Error.WriteLine("worktree-guard: $RunDir is not a git repository - the subagent runs in place, without worktree isolation")
    Emit-WtPath $RunDir
    exit 0
}
$ToplevelAbs = Norm-Path $Toplevel

# -- No commits yet => base the worktree on a snapshot commit ----------------
# Unborn HEAD makes `git worktree add --detach <path> HEAD` fail and the
# harness refuses the main checkout itself, so build a commit of the working
# tree through a throwaway index (user index/refs/HEAD untouched) and detach
# the worktree there. Same decision as worktree-guard.sh.
# v0.2.102 W6-R - must match the .sh: credential FILES are excluded from the
# snapshot, by the ONE credential-file list (.claude/scripts/bash_security.py
# --credential-file-globs). When it cannot be read (python or the script
# missing - a broken install) the snapshot is REFUSED, never taken without
# the exclusion.
$BaseRef = "HEAD"
$BaseReason = "worktree_add_detached_head"
$SnapCredExcluded = ""
& git -C $ToplevelAbs rev-parse --verify -q 'HEAD^{commit}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $FindPy = Join-Path $ScriptDir "_lib/find-python.ps1"
    if ((-not $PY) -and (Test-Path -LiteralPath $FindPy)) { . $FindPy }
    $SnapSecurity = Join-Path (Join-Path (Split-Path -Parent $ScriptDir) "scripts") "bash_security.py"
    $SnapGlobs = @()
    if ($PY -and (Test-Path -LiteralPath $SnapSecurity)) {
        try { $SnapGlobs = @(& $PY $SnapSecurity --credential-file-globs 2>$null | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() }) } catch { $SnapGlobs = @() }
    }
    if ($SnapGlobs.Count -eq 0) {
        $pyShown = if ($PY) { $PY } else { "no python interpreter" }
        $reason = "broken install: repository at $ToplevelAbs has no commits, and the credential-file list its snapshot must exclude could not be read ($pyShown / $SnapSecurity) - refusing to snapshot a working tree that may hold credential files; repair it with the project's bundle update (launcher: project Settings -> Update bundle)"
        Log-Event "create_failed" $reason $ProposedPath ""
        [Console]::Error.WriteLine("worktree-guard: ABORT - $reason")
        exit 1
    }
    $SnapExcludes = @(':(exclude).claude/worktrees') + @($SnapGlobs | ForEach-Object { ":(exclude,glob)$_" })
    $SnapCredSpecs = @($SnapGlobs | ForEach-Object { ":(glob)$_" })
    try {
        $SnapCredExcluded = (@(& git -C $ToplevelAbs ls-files --cached --others --exclude-standard -- @SnapCredSpecs 2>$null) |
            Sort-Object -Unique | Select-Object -First 20) -join " "
    } catch { $SnapCredExcluded = "" }
    $SnapSha = ""
    $SnapErr = ""
    $SnapTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("vco-wtg-" + [guid]::NewGuid().ToString("N"))
    $PrevIndex = $env:GIT_INDEX_FILE
    $PrevNames = @($env:GIT_AUTHOR_NAME, $env:GIT_AUTHOR_EMAIL, $env:GIT_COMMITTER_NAME, $env:GIT_COMMITTER_EMAIL)
    try {
        New-Item -ItemType Directory -Force -Path $SnapTmp -ErrorAction Stop | Out-Null
        $env:GIT_INDEX_FILE = Join-Path $SnapTmp "index"
        $SnapErr = (& git -C $ToplevelAbs add -A -- . @SnapExcludes 2>&1 | Out-String)
        if ($LASTEXITCODE -eq 0) {
            $SnapTree = (& git -C $ToplevelAbs write-tree 2>$null | Select-Object -First 1)
            if ($LASTEXITCODE -eq 0 -and $SnapTree) {
                $env:GIT_AUTHOR_NAME = "VCO worktree-guard"
                $env:GIT_AUTHOR_EMAIL = "worktree-guard@vco.invalid"
                $env:GIT_COMMITTER_NAME = "VCO worktree-guard"
                $env:GIT_COMMITTER_EMAIL = "worktree-guard@vco.invalid"
                $out = (& git -C $ToplevelAbs -c commit.gpgsign=false commit-tree $SnapTree -m "VCO worktree-guard: snapshot of the working tree (repository has no commits yet)" 2>$null | Select-Object -First 1)
                if ($LASTEXITCODE -eq 0 -and $out) { $SnapSha = ([string]$out).Trim() }
            }
        }
    } catch {
        $SnapErr = $_.Exception.Message
    } finally {
        $env:GIT_INDEX_FILE = $PrevIndex
        $env:GIT_AUTHOR_NAME = $PrevNames[0]
        $env:GIT_AUTHOR_EMAIL = $PrevNames[1]
        $env:GIT_COMMITTER_NAME = $PrevNames[2]
        $env:GIT_COMMITTER_EMAIL = $PrevNames[3]
        try { Remove-Item -LiteralPath $SnapTmp -Recurse -Force -ErrorAction SilentlyContinue } catch { }
    }
    if (-not $SnapSha) {
        $SnapDetail = if ($SnapErr -and $SnapErr.Trim()) { $SnapErr.Trim() } else { "git write-tree/commit-tree failed" }
        $reason = "repository at $ToplevelAbs has no commits and a snapshot commit of its working tree could not be built: $SnapDetail"
        Log-Event "create_failed" $reason $ProposedPath ""
        [Console]::Error.WriteLine("worktree-guard: ABORT - $reason")
        exit 1
    }
    $BaseRef = $SnapSha
    $BaseReason = "worktree_add_snapshot_no_head"
}

# Create-Worktree: run `git worktree add --detach <target> HEAD`. Idempotent
# (already-registered path → success). On genuine failure, emit reason to
# stderr, log it, and abort NON-ZERO (a failed create must be LOUD, never a
# silent shared-tree fallback). Never returns normally on failure — it exits.
function Create-Worktree([string]$target) {
    # Already registered at this exact path? (re-fire / retry) → success.
    $existing = @()
    try {
        $existing = (& git -C $ToplevelAbs worktree list --porcelain 2>$null) |
            Where-Object { $_ -like 'worktree *' } |
            ForEach-Object { $_.Substring(9) }
    } catch { }
    $targetNorm = Norm-Path $target
    foreach ($e in $existing) {
        if ((Norm-Path $e) -eq $targetNorm) {
            Log-Event "created" "idempotent_existing_worktree" $ProposedPath $target
            Emit-WtPath $target
            exit 0
        }
    }
    # Ensure the parent directory exists.
    try { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) -ErrorAction SilentlyContinue | Out-Null } catch { }
    # Detached HEAD: safest base (no branch-name collisions across parallel
    # agents; clean separate checkout of HEAD).
    $addErr = ""
    try {
        $addErr = (& git -C $ToplevelAbs worktree add --detach $target $BaseRef 2>&1 | Out-String)
    } catch {
        $addErr = $_.Exception.Message
    }
    if ($LASTEXITCODE -eq 0) {
        if ($BaseRef -ne "HEAD") {
            Write-Notice "VCO worktree-guard: this repository has no commits yet, so your isolated worktree ($target) starts from a SNAPSHOT of the project's working tree (commit $BaseRef, on no branch) instead of HEAD. The main checkout is untouched by your edits. Credential files (VCO's credential-file list: *.env, *.env.*, *.credentials, *.netrc, *.pgpass and the like) are EXCLUDED from the snapshot and are not in your worktree$(if ($SnapCredExcluded) { " - left out here: $SnapCredExcluded" }); if the task needs one, ask the caller instead of recreating it. Say this in your final report and give the user the command that brings your changes back: git -C `"$target`" add -A && git -C `"$target`" diff --binary --cached $BaseRef | git -C `"$ToplevelAbs`" apply"
        }
        Log-Event "created" $BaseReason $ProposedPath $target
        $recMode = if ($BaseRef -eq "HEAD") { "head" } else { "snapshot" }
        $recBase = ""
        try { $recBase = ([string](& git -C $target rev-parse HEAD 2>$null | Select-Object -First 1)).Trim() } catch { }
        Save-LaunchRecord $recMode $target $recBase $ToplevelAbs
        Emit-WtPath $target
        exit 0
    }
    # Failure → LOUD abort.
    $reason = "git worktree add failed for ${target}: $($addErr.Trim())"
    Log-Event "create_failed" $reason $ProposedPath $target
    [Console]::Error.WriteLine("worktree-guard: ABORT — $reason")
    exit 1
}

# ── Belt-and-suspenders: an explicit path WAS supplied ────────────────────
# Absent in the real payload; only fires on a future build that sends one.
if ($ProposedPath) {
    $ProposedAbs = Norm-Path $ProposedPath
    if ($ProposedAbs -eq $ToplevelAbs) {
        # Proposed path == parent checkout: exactly the silent shared-tree
        # collapse we exist to prevent.
        if ($env:VCT_WORKTREE_GUARD_ENFORCE) {
            $reason = "explicit worktree path IS the parent checkout ($ToplevelAbs) — refusing (would collapse to the shared tree)"
            Log-Event "block" $reason $ProposedAbs $ToplevelAbs
            [Console]::Error.WriteLine("worktree-guard: BLOCK — $reason")
            exit 2
        }
        # Default: derive a safe separate path instead (fall through).
        Log-Event "redirect_parent_path" "explicit path equals parent toplevel; deriving a separate worktree path instead" $ProposedAbs $ToplevelAbs
    } else {
        # A genuinely separate proposed path → create it there.
        Create-Worktree $ProposedAbs
    }
}

# ── No usable identifier ⇒ graceful no-op ─────────────────────────────────
# Empty $WtName only on a degenerate/malformed invocation (empty stdin,
# non-JSON, or neither an identifier nor an explicit path). Do NOT fabricate
# a worktree from a fallback token — echo nothing + exit 0. The "agent"
# fallback in Sanitize-Id is reserved for a NON-empty identifier that
# sanitizes down to empty. Mirror of worktree-guard.sh.
if (-not $WtName) {
    Log-Event "noop" "no_worktree_identifier" $ProposedPath ""
    exit 0
}

# ── Primary path: derive + create under the VCO convention ────────────────
# <toplevel>/.claude/worktrees/<sanitized-id>.
$SafeId = Sanitize-Id $WtName
$DerivedPath = Join-Path (Join-Path (Join-Path $ToplevelAbs ".claude") "worktrees") $SafeId
$DerivedAbs = Norm-Path $DerivedPath
Create-Worktree $DerivedAbs
