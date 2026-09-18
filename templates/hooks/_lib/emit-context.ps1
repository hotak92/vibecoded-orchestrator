# emit-context.ps1 — shared helper for hooks that inject LLM-visible context.
#
# Plain stdout from PreToolUse hooks is silently discarded by Claude Code's
# hook runner — only `hookSpecificOutput.additionalContext` reaches the LLM
# (system reminder wrapper). UserPromptSubmit and SessionStart accept plain
# stdout, but for those hooks this helper is still useful as a unified emit
# point with the same whitespace-only-content guard.
#
# Why the whitespace guard: the framework still surfaces a system-reminder
# block to the LLM when additionalContext is whitespace-only. Hooks that
# build context from optional sections can produce strings of just newlines
# or spaces when every section is suppressed. Without this guard, the LLM
# sees an empty `[Pre-edit context for ...]:` reminder with no body —
# user-visible noise plus prompt-cache misses.
#
# Usage:
#   . "$PSScriptRoot/_lib/emit-context.ps1"
#   Emit-AdditionalContext $ctx 'PreToolUse'
#
# OS support: pure PowerShell — no Python or external tools required.

function Emit-AdditionalContext {
    param(
        [string]$Ctx,
        [string]$EventName = 'PreToolUse'
    )

    if (-not $Ctx) { return }

    # Whitespace-only → treat as empty.
    if (-not ($Ctx -match '\S')) { return }

    $truncated = if ($Ctx.Length -gt 10000) { $Ctx.Substring(0, 10000) } else { $Ctx }

    $envelope = [ordered]@{
        hookSpecificOutput = [ordered]@{
            hookEventName      = $EventName
            permissionDecision = 'allow'
            additionalContext  = $truncated
        }
    }
    $json = $envelope | ConvertTo-Json -Compress -Depth 8
    Write-Output $json
}


# ---------------------------------------------------------------------------
# Emit-VcoMissingHookLibNotice -HooksDir <dir> -ProjectRoot <dir>
#                              -SessionId <id> -Lib <basename>
#
# A shipped hook library that is ABSENT is a BROKEN INSTALL, not a fallback
# case (CLAUDE.md: "loud-fail, never silent-fallback, when a shipped
# dependency is missing"). Hooks dot-source their `_lib/*` helpers
# conditionally so a half-applied bundle cannot make the user's Edit/Write/
# Bash ERROR -- but conditional-source plus an early `exit 0` had become a
# SILENT no-op mode: after v0.2.95 moved ALL routing into
# `_lib/route-touched-path.ps1`, a project missing that one file synced
# NOTHING on any write and said nothing about it (review MAJOR-1).
#
# This does not change the soft-fail contract -- the caller still exits 0 --
# it only makes the degradation VISIBLE, on two channels, once per session:
# stderr (for the human; PostToolUse stderr never reaches the model) and the
# RETURNED string, which the caller puts in its own additionalContext
# envelope so each hook still emits exactly one.
#
# Returns "" when the condition was already reported this session.
#
# MUST MATCH `_lib/emit-context.sh`'s vco_report_missing_hook_lib (same
# sentinel name, same two channels, same once-per-session rule).

# ---------------------------------------------------------------------------
# THE SET: which `_lib/` files the write pipeline cannot work without, and
# what each one's absence actually costs.
# ---------------------------------------------------------------------------
# v0.2.95 ship-gate review MAJOR-2. Rev 1's "shipped component with a SILENT
# no-op mode" was closed for `route-touched-path` alone, by hand, in the two
# places in front of the fix lane; two siblings of the same shape stayed
# silent and the SessionStart probe reported OK while one of them was gone.
# Enumerating paths by hand is what produced that, so the set has ONE home
# per flavour and three readers take it from here: the notice wording below,
# `session-start-retrieval-health.ps1`'s probe, and the missing-lib test
# (which refuses a row it has no driver for).
#
# Basenames, no extension — each reader appends its own.
# MUST MATCH `_lib/emit-context.sh`'s vco_required_hook_libs / vco_hook_lib_role.
function Get-VcoRequiredHookLibs {
    return @("route-touched-path", "bash-write-targets", "code-extensions")
}

# One clause naming what the file is the one home FOR and what stops without
# it. An unknown name still gets a true (if general) sentence, never silence.
function Get-VcoHookLibRole {
    param([string]$Lib = "")
    $base = Split-Path $Lib -Leaf
    $base = $base -replace '\.(sh|ps1)$', ''
    switch ($base) {
        "route-touched-path" {
            return "routing a touched path to kg-sync (knowledge/), the development collection (docs/), the diagram indexer and the code-graph queue, so every Edit, Write and CLI write is being parsed and then dropped"
        }
        "bash-write-targets" {
            return "recovering the paths a shell command wrote, so no CLI write is even looked at -- a redirect, a heredoc, sed -i or patch into knowledge/ or docs/ now reaches Weaviate never"
        }
        "code-extensions" {
            return "deciding which touched paths are code, so nothing is being queued for the end-of-turn code-graph drain and the code graph has stopped tracking this project"
        }
        default {
            return "part of this project's shipped hook library, and the hook that needs it has stopped doing its job"
        }
    }
}

function Emit-VcoMissingHookLibNotice {
    param(
        [string]$HooksDir = "",
        [string]$ProjectRoot = "",
        [string]$SessionId = "",
        [string]$Lib = ""
    )
    if (-not $Lib) { return "" }

    # Path-safety for the session id: it is interpolated into a FILE NAME.
    # Same charset rule as _lib/session-id.ps1 (sourced when present; its
    # absence costs the per-session KEY, not the notice).
    $key = "default"
    $sessionLib = Join-Path $HooksDir "_lib/session-id.ps1"
    if ($HooksDir -and (Test-Path $sessionLib)) { . $sessionLib }
    if ($SessionId -and (Get-Command Get-VcoSanitizedSessionId -ErrorAction SilentlyContinue)) {
        $key = Get-VcoSanitizedSessionId $SessionId
    } elseif ($SessionId -and ($SessionId -match '^[A-Za-z0-9_-]+$')) {
        $key = $SessionId
    }
    if (-not $key) { $key = "default" }

    $rootLabel = if ($ProjectRoot) { $ProjectRoot } else { "<project>" }
    $role = Get-VcoHookLibRole $Lib
    $notice = @"
[VCO broken install] .claude/hooks/_lib/$Lib is MISSING.
That file is the one home for $role.
Restore it by updating this project's bundle:
  python -m vco_lib.project_init install-bundle --folder $rootLabel --orchestrator-root <orchestrator-root> --update
(or the launcher's per-project Settings page -> "Update bundle").
"@

    $sentinel = ""
    if ($ProjectRoot) {
        $sentinelDir = Join-Path $ProjectRoot ".claude/state"
        try {
            if (-not (Test-Path $sentinelDir)) {
                New-Item -ItemType Directory -Force -Path $sentinelDir -ErrorAction Stop | Out-Null
            }
            $sentinel = Join-Path $sentinelDir ("route_lib_missing_{0}_{1}" -f $key, $Lib)
            if (Test-Path $sentinel) { return "" }   # already reported
        } catch {
            $sentinel = ""
        }
    }

    [Console]::Error.WriteLine($notice)
    if ($sentinel) {
        try { New-Item -ItemType File -Force -Path $sentinel -ErrorAction Stop | Out-Null } catch { }
    }
    return $notice
}
