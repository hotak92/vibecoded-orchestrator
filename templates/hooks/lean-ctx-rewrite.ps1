# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Per-project lean-ctx PreToolUse hook for Bash tool calls (Windows).
# Windows sibling of templates/hooks/lean-ctx-rewrite.sh — see that file for
# the full rationale (fork-bomb avoidance, 0.2.11 redesign, three-tier
# bypass hierarchy).
#
# CONTRACT
# --------
# Claude Code pipes a JSON PreToolUse payload to stdin; `lean-ctx hook
# rewrite` reads it and emits a JSON `hookSpecificOutput.updatedInput`
# wrapping the command in `lean-ctx -c '<cmd>'`. Empty stdout = no rewrite
# = raw output.
#
# D-3 (v0.2.73): lean-ctx 3.x's response ALSO carries
# "permissionDecision":"allow", which on Claude Code >= 2.1.x AUTO-APPROVES
# the tool call -- every wrapped Bash command silently bypassed the user's
# permission settings. This hook strips that field (keeping updatedInput)
# before handing the response to Claude Code. MUST MATCH the .sh sibling's
# filter (verification record lives there).
#
# BYPASS HIERARCHY (matches .sh sibling)
# --------------------------------------
# 1. Per-call:    invoke as `lean-ctx bypass "<cmd>"` — auto-detected by
#                 `lean-ctx hook rewrite`, emits empty stdout → raw.
# 2. Per-project: add `VCO_LEAN_CTX_DEFAULT=off` to `.claude/env` — this
#                 script reads that file and exits early when the value
#                 is "off". Default "on" = compression active.
# 3. Global:      `$env:VCT_DISABLE_HOOKS = "1"` — disables ALL VCO hooks
#                 for this shell session.
#
# GUARD ORDER (intentional, mirrors .sh)
# --------------------------------------
# 1. VCT_DISABLE_HOOKS — global sledgehammer, first.
# 2. .claude/env  → VCO_LEAN_CTX_DEFAULT per-project gate.
# 3. lean-ctx availability — graceful no-op when missing.
# 4. & lean-ctx hook rewrite — per-call symmetric bypass inside.

# Scrub sensitive env vars before any subprocess spawning (defense-in-depth
# parity with every other VCO hook + .sh sibling). The hook itself doesn't
# read secrets, but `& lean-ctx hook rewrite` inherits our env; scrubbing
# before delegation means the lean-ctx subprocess can't accidentally leak a
# credential via its own logs / debug output.
foreach ($k in @('SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY')) {
    if (Test-Path "Env:$k") { Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue }
}

# 1. Global kill-switch (one switch for "turn off all VCT hook side-effects").
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# 2. Per-project default. Read .claude/env if it exists; look for the
#    VCO_LEAN_CTX_DEFAULT line (KEY=VALUE syntax). Anything else in the
#    file is ignored — this script doesn't `Invoke-Expression` user env
#    files (avoids arbitrary code-exec from a malformed .claude/env).
$envFile = Join-Path (Get-Location) ".claude/env"
$leanCtxDefault = "on"
if (Test-Path -LiteralPath $envFile) {
    try {
        foreach ($line in Get-Content -LiteralPath $envFile -ErrorAction Stop) {
            if ($line -match '^\s*VCO_LEAN_CTX_DEFAULT\s*=\s*(.+?)\s*$') {
                $leanCtxDefault = $Matches[1].Trim('"').Trim("'").ToLowerInvariant()
            }
        }
    } catch {
        # Read failure — fall through with the "on" default. Same shape
        # as the .sh sibling's `[ -f .claude/env ] && . .claude/env` no-op
        # when the file is unreadable.
    }
}
if ($leanCtxDefault -eq "off") { exit 0 }

# 3. lean-ctx availability — optional dep, never break Bash for users without it.
if (-not (Get-Command lean-ctx -ErrorAction SilentlyContinue)) { exit 0 }

# 4. Delegate to lean-ctx's rewrite handler, then strip permissionDecision
#    (D-3, v0.2.73). Stdin is connected by Claude Code (the PreToolUse JSON
#    payload); the FILTERED stdout flows back as the hook's rewrite response.
#    Conservative on every failure arm: unparseable output / nothing left to
#    emit -> print NOTHING (= no rewrite, raw command). Losing compression
#    for one call is strictly safer than emitting an auto-approval.
#    MUST MATCH templates/hooks/lean-ctx-rewrite.sh (python filter there).
$rewriteOut = & lean-ctx hook rewrite
# P3 (v0.2.73): a NON-ZERO lean-ctx exit suppresses output — MUST MATCH the
# .sh's `out="$(lean-ctx hook rewrite)" || exit 0`. Without this, lean-ctx
# exiting non-zero WITH parseable JSON on stdout would emit a rewrite on
# Windows while POSIX runs raw (cross-OS divergence).
#
# Guard against an UNSET $LASTEXITCODE: when `lean-ctx` resolves to a PS
# *script/function* (e.g. a `.ps1` shim) rather than a native executable, and
# that callee never `exit`s, PowerShell leaves $LASTEXITCODE $null. `$null -ne
# 0` is $true, so a bare `-ne 0` would wrongly treat success as failure and
# drop a valid rewrite. Only a REAL non-zero (native exe / explicit exit) may
# suppress — matching the .sh, where `||` fires only on an actual non-zero.
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit 0 }
if (-not $rewriteOut) { exit 0 }
$rewriteRaw = ($rewriteOut -join "`n").Trim()
if (-not $rewriteRaw) { exit 0 }
try {
    $data = $rewriteRaw | ConvertFrom-Json
    $hso = $data.hookSpecificOutput
    if ($null -eq $hso) { exit 0 }
    if ($hso.PSObject.Properties['permissionDecision']) {
        $hso.PSObject.Properties.Remove('permissionDecision')
    }
    # P3 (v0.2.73): suppress on a MISSING, null, OR EMPTY updatedInput — MUST
    # MATCH the .sh Python filter's truthiness check (`if not
    # hso.get("updatedInput"): sys.exit(0)`). An empty object {} is falsy
    # there, so it must be treated as "nothing to emit" here too (otherwise
    # Windows emits "updatedInput":{} where POSIX emits nothing).
    $ui = $hso.updatedInput
    if ($null -eq $hso.PSObject.Properties['updatedInput'] -or $null -eq $ui) { exit 0 }
    if (($ui.PSObject.Properties | Measure-Object).Count -eq 0) { exit 0 }
    Write-Output ($data | ConvertTo-Json -Depth 8 -Compress)
} catch {
    # Emit nothing: no rewrite, raw command runs under the normal
    # permission flow.
}
exit 0
