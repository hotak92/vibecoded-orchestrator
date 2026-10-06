# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# check_emit_context_ps1.ps1 -- RUNTIME leg for templates/hooks/_lib/emit-context.ps1.
#
# NB-16 (v0.2.101): the .ps1 sibling of emit-context.sh was covered only by a
# SOURCE-SCAN test (tests/test_hooks_pretooluse_json_envelope.py::
# test_context_envelopes_never_approve) -- the "never guard wiring with a
# source scan" gap. This script is the ONE home for the behavioural check:
# it dot-sources the real emitter, calls it, and asserts on the JSON it
# actually produces. Two readers, one definition:
#   1. the hook-parity workflow (.github/workflows/hook-parity.yml) runs it
#      in a `shell: pwsh` step on every PR / push to main -- guaranteed to
#      RUN in CI even where a pytest skipif would swallow the leg;
#   2. tests/test_hooks_pretooluse_json_envelope.py::
#      test_emit_context_ps1_envelope_has_no_decision invokes this SAME
#      script via `pwsh -NoProfile -File` wherever pwsh exists locally.
#
# Contract checked (must match emit-context.sh's runtime test leg):
#   - Emit-AdditionalContext emits a hookSpecificOutput.additionalContext
#     envelope carrying the payload;
#   - the envelope contains NO permissionDecision (on PreToolUse, "allow"
#     SKIPS the user's permission prompt -- a context-injecting hook must
#     never approve the tool call).
#
# Exit 0 with a PASS line on success; exit 1 with a clear stderr message on
# any failure (missing lib, no envelope, wrong payload, decision present).

[CmdletBinding()]
param(
    # Repo root; defaults to this script's grandparent (.github/scripts -> repo)
    # so the check runs from any cwd.
    [string]$RepoRoot = ''
)

if (-not $RepoRoot) {
    $RepoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
}

$ErrorActionPreference = 'Stop'

function Fail {
    param([string]$Message)
    [Console]::Error.WriteLine("check_emit_context_ps1: FAIL - $Message")
    exit 1
}

$libPath = Join-Path $RepoRoot 'templates/hooks/_lib/emit-context.ps1'
if (-not (Test-Path $libPath)) {
    Fail "emit-context.ps1 not found at '$libPath' (pass -RepoRoot <repo-root>)"
}

try {
    . $libPath
} catch {
    Fail "dot-sourcing emit-context.ps1 threw: $($_.Exception.Message)"
}

if (-not (Get-Command 'Emit-AdditionalContext' -ErrorAction SilentlyContinue)) {
    Fail 'Emit-AdditionalContext is not defined after dot-sourcing emit-context.ps1'
}

$payload = 'some kg context'
try {
    $out = @(Emit-AdditionalContext $payload 'PreToolUse')
} catch {
    Fail "Emit-AdditionalContext threw: $($_.Exception.Message)"
}

# A crash inside the emitter yields empty stdout -- that is a FAILURE, never a
# silent pass (the pytest leg relies on this to red-proof itself).
$lines = @($out | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') })
if ($lines.Count -eq 0) {
    Fail "no JSON envelope emitted (stdout was: '$($out -join ' | ')')"
}

try {
    $envelope = $lines[-1] | ConvertFrom-Json
} catch {
    Fail "emitted stdout is not valid JSON: $($_.Exception.Message); raw: '$($lines[-1])'"
}

$hookOut = $envelope.PSObject.Properties['hookSpecificOutput']
if ($null -eq $hookOut) {
    Fail "envelope has no hookSpecificOutput: '$($lines[-1])'"
}
$hookOut = $hookOut.Value

$ctxProp = $hookOut.PSObject.Properties['additionalContext']
if ($null -eq $ctxProp) {
    Fail "hookSpecificOutput has no additionalContext: '$($lines[-1])'"
}
$ctx = [string]$ctxProp.Value
if (-not $ctx.StartsWith($payload)) {
    Fail "additionalContext does not start with the payload ('$payload'): '$($ctx.Substring(0, [Math]::Min(120, $ctx.Length)))'"
}

$decisionProp = $hookOut.PSObject.Properties['permissionDecision']
if ($null -ne $decisionProp) {
    Fail "envelope carries permissionDecision='$($decisionProp.Value)' - a context-injecting hook must NEVER approve a tool call"
}

Write-Output 'check_emit_context_ps1: PASS - Emit-AdditionalContext envelope carries additionalContext and no permissionDecision'
exit 0
