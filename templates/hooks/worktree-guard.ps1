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

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$LogDir = Join-Path $ProjectRoot ".claude/logs"
$LogFile = Join-Path $LogDir "worktree-guard.jsonl"
try { New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction SilentlyContinue | Out-Null } catch { }

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
$GitScopeDir = $ProjectRoot
if ($RepoHint -and (Test-Path -LiteralPath $RepoHint -PathType Container)) {
    $GitScopeDir = $RepoHint
}
$Toplevel = ""
if (Get-Command git -ErrorAction SilentlyContinue) {
    try {
        $Toplevel = (& git -C $GitScopeDir rev-parse --show-toplevel 2>$null | Select-Object -First 1)
    } catch { $Toplevel = "" }
}

# ── Not a git repo ⇒ graceful no-op ───────────────────────────────────────
# Echo nothing + exit 0 so the harness does its own default — do NOT abort a
# legitimate non-git spawn with a non-zero exit.
if (-not $Toplevel) {
    Log-Event "noop" "not_a_repo" $ProposedPath ""
    exit 0
}
$ToplevelAbs = Norm-Path $Toplevel

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
        $addErr = (& git -C $ToplevelAbs worktree add --detach $target HEAD 2>&1 | Out-String)
    } catch {
        $addErr = $_.Exception.Message
    }
    if ($LASTEXITCODE -eq 0) {
        Log-Event "created" "worktree_add_detached_head" $ProposedPath $target
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
