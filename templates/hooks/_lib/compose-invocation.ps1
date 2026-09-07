# _lib/compose-invocation.ps1
# ONE home for splitting a compose command string into an invocable
# head + argument array on Windows.
#
# v0.2.92 BLOCKER-1. `vco_lib.containers resolve` hands back the compose
# invocation as a STRING that may be one token (`podman-compose`,
# `docker-compose`) or two (`podman compose`, `docker compose`). Four hook
# sites split it with:
#
#     $parts   = $ComposeCmd -split '\s+'
#     $cmdRest = $parts[1..($parts.Length - 1)]
#
# For a TWO-token command that is correct. For a ONE-token command
# `1..($parts.Length - 1)` is the range `1..0`, which PowerShell evaluates
# DESCENDING as @(1, 0) - so `$cmdRest` comes back as @($null, 'podman-compose')
# and the hook invokes `podman-compose podman-compose up -d ...`. Measured in
# pwsh 7: `count=1`, `[podman-compose]`. Standalone-compose Windows hosts
# therefore could not bring a service up from these hooks at all.
#
# Found while adding `--build` to the code_embed bring-up: a path that cannot
# start the service is a worse version of the defect being fixed, and adding a
# fifth copy of the broken split to carry the new flag was not an option
# (CLAUDE.md: extract before you duplicate).
#
# Usage:
#     . "$PSScriptRoot/_lib/compose-invocation.ps1"
#     $c = Split-VcoComposeCommand -ComposeCmd $ComposeCmd
#     & $c.Head @($c.Rest) up -d --build code_embed
#
# `Rest` is ALWAYS an array (possibly empty), so `@($c.Rest)` splats cleanly
# in both shapes. This file is dot-sourced, never executed standalone.

function Split-VcoComposeCommand {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$ComposeCmd)

    $parts = @($ComposeCmd -split '\s+' | Where-Object { $_ -ne '' })
    if ($parts.Count -eq 0) {
        return [pscustomobject]@{ Head = ''; Rest = @() }
    }
    $rest = @()
    if ($parts.Count -gt 1) {
        $rest = @($parts[1..($parts.Count - 1)])
    }
    return [pscustomobject]@{ Head = $parts[0]; Rest = $rest }
}
