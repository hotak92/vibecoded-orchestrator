# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# OS-EXEMPT-PARITY: this .ps1 carries a UTF-8 BOM its .sh sibling must NOT have (PS 5.1 encoding) — the two legitimately diverge on that byte, so a BOM-only edit need not touch the .sh. Keep the LOGIC in lockstep by hand.
# worktree-guard.ps1 — Windows sibling of worktree-guard.sh. WorktreeCreate
# hook (Layer 0, primary deterministic gate) for the worktree-isolation
# silent-fallback safeguard.
#
# ── Cross-OS parity (MUST match worktree-guard.sh) ────────────────────────
# The stdout-path contract is the one place .sh / .ps1 MUST behave
# identically: same decision matrix, same single BLOCK case (proposed path
# == parent toplevel), same echo-through-on-any-doubt discipline, same
# staged-enable gating behind $env:VCT_WORKTREE_GUARD_ENFORCE. Any change to
# the decision logic in worktree-guard.sh MUST be mirrored here and vice
# versa — keep them in lockstep.
#
# ── stdout contract ───────────────────────────────────────────────────────
# WorktreeCreate (can-block = Yes) requires the hook to print the absolute
# worktree path on stdout. We accept by echoing the proposed path back,
# block by emitting no path + non-zero exit (only in ENFORCE mode), and
# log-only otherwise.
#
# ── Staged enable ─────────────────────────────────────────────────────────
# Block branch gated behind VCT_WORKTREE_GUARD_ENFORCE (default off →
# log-only). VCO has never exercised WorktreeCreate; the integrator verifies
# the live stdin schema + stdout consumption from
# .claude/logs/worktree-guard.jsonl across real spawns THIS cycle, then flips
# the flag. Same-cycle staged enable, not a deferred TODO. FALLBACK: if the
# build does not consume stdout-as-path, this degrades to warn+log and the
# SubagentStart / SubagentStop backstops act as the block-equivalent.
#
# ── Tunables (identical to .sh) ───────────────────────────────────────────
#   VCT_DISABLE_HOOKS              — global bypass.
#   VCT_WORKTREE_GUARD_ENFORCE=1   — flip from log-only to block-on-violation.
#   VCT_WORKTREE_GUARD_STRICT=1    — upgrade dirty-parent WARN to BLOCK
#                                    (only when ENFORCE also set).

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$LogDir = Join-Path $ProjectRoot ".claude/logs"
$LogFile = Join-Path $LogDir "worktree-guard.jsonl"
try { New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction SilentlyContinue | Out-Null } catch { }

# ── Read stdin (the WorktreeCreate payload) ───────────────────────────────
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

# Emit-Path: satisfy the stdout contract. Empty arg → emit nothing.
function Emit-Path([string]$p) {
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
        Add-Content -Path $LogFile -Value $json -ErrorAction SilentlyContinue
    } catch { }
}

# ── Parse stdin defensively (synonym-tolerant) ────────────────────────────
$ProposedPath = ""
$RepoHint = ""
if ($HookStdin) {
    try {
        $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
        if ($payload) {
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
        # Malformed JSON → leave both empty (treated as "no path to validate").
    }
}

# ── Step 1: parse failure / no path ───────────────────────────────────────
if (-not $ProposedPath) {
    Log-Event "noop" "no_proposed_path_parsed" "" ""
    Emit-Path $ProposedPath
    exit 0
}

# Normalise the proposed path to absolute (best-effort; the worktree dir does
# not exist yet at create time, so we resolve textually, not via Resolve-Path
# which requires existence).
function Norm-Path([string]$p) {
    try {
        return [System.IO.Path]::GetFullPath($p)
    } catch {
        return $p
    }
}
$ProposedAbs = Norm-Path $ProposedPath

# ── Step 2: not-a-repo ⇒ graceful no-op ───────────────────────────────────
$Toplevel = ""
if (Get-Command git -ErrorAction SilentlyContinue) {
    try {
        $Toplevel = (& git -C $ProjectRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1)
    } catch { $Toplevel = "" }
}
if (-not $Toplevel) {
    Log-Event "noop" "not_a_repo" $ProposedAbs ""
    Emit-Path $ProposedAbs
    exit 0
}
$ToplevelAbs = Norm-Path $Toplevel

# ── Step 3+4: validate the proposed path is a SEPARATE checkout ────────────
# CLEAR VIOLATION (the one block case): proposed worktree path == parent
# checkout toplevel. "Inside the toplevel dir" is NOT by itself a violation
# (the harness legitimately nests worktrees under
# <toplevel>/.claude/worktrees/...); only equality to the primary checkout
# root collapses HEAD. Mirror of worktree-guard.sh Step 3+4.
$IsViolation = $false
if ($ProposedAbs -eq $ToplevelAbs) { $IsViolation = $true }

if ($IsViolation) {
    $reason = "isolation:worktree requested but the proposed worktree path IS the parent checkout ($ToplevelAbs) — refusing to avoid silent shared-tree fallback"
    if ($env:VCT_WORKTREE_GUARD_ENFORCE) {
        Log-Event "block" $reason $ProposedAbs $ToplevelAbs
        [Console]::Error.WriteLine("worktree-guard: BLOCK — $reason")
        exit 2
    } else {
        Log-Event "violation_logged_only" $reason $ProposedAbs $ToplevelAbs
        [Console]::Error.WriteLine("worktree-guard: WARNING (log-only) — $reason")
        Emit-Path $ProposedAbs
        exit 0
    }
}

# ── Step 5: dirty-parent handling ──────────────────────────────────────────
# WARN by default; escalate to BLOCK only under STRICT + ENFORCE together.
$ParentDirty = $false
if (Get-Command git -ErrorAction SilentlyContinue) {
    try {
        $porcelain = (& git -C $ToplevelAbs status --porcelain 2>$null)
        if ($porcelain) { $ParentDirty = $true }
    } catch { }
}

if ($ParentDirty) {
    $reason = "parent checkout ($ToplevelAbs) is dirty at worktree-create time — separate worktree still safe, but a fallback to the shared tree would not be"
    if ($env:VCT_WORKTREE_GUARD_STRICT -and $env:VCT_WORKTREE_GUARD_ENFORCE) {
        Log-Event "block" $reason $ProposedAbs $ToplevelAbs
        [Console]::Error.WriteLine("worktree-guard: BLOCK (strict) — $reason")
        exit 2
    }
    # WARN (not block): the proposed path is a SEPARATE checkout so the
    # worktree itself is safe; record the dirty-parent signal, echo through,
    # and exit here so we don't also emit the Step-6 "pass" row (the last log
    # row should read "warn_dirty_parent"). Mirror of worktree-guard.sh.
    Log-Event "warn_dirty_parent" $reason $ProposedAbs $ToplevelAbs
    [Console]::Error.WriteLine("worktree-guard: WARNING — $reason")
    Emit-Path $ProposedAbs
    exit 0
}

# ── Step 6: happy path ─────────────────────────────────────────────────────
Log-Event "pass" "validated_separate_checkout" $ProposedAbs $ToplevelAbs
Emit-Path $ProposedAbs
exit 0
