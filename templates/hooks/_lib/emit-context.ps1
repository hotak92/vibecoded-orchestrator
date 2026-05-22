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
