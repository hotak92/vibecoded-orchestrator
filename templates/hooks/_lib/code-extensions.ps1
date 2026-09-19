# _lib/code-extensions.ps1
# THE home for "is this path a code file?" -- PowerShell sibling of
# _lib/code-extensions.sh (v0.2.95, lane F10).
#
# Before this file the same extension alternation was written out in
# pre-edit-context-inject.ps1, post-file-edit.ps1, pre-tool-use.ps1,
# code-graph-incremental.ps1 and stop-codegraph-drain.ps1 -- a C-tier
# mirror per consumer. Lane F10 added a further consumer (the Bash-write
# routing), which is the point at which the project's A>B>C rule requires
# ONE home instead of another copy.
#
# MUST MATCH: templates/hooks/_lib/code-extensions.sh -- the alternation is
# the SAME decision on both operating systems, and
# tests/test_v0295_code_extension_one_home.py fails if the two literals
# diverge (or if any remaining mirror drifts from them).
#
# Plain ASCII only. Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoCodeExtensionsSourced) { return }
$script:VcoCodeExtensionsSourced = $true

# Extension alternation, as a .NET regex suitable for `-match`.
$script:VcoCodeExtRe = '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$'

# Test-VcoIsCodeFile <Path> -- $true when the path names a code file.
#
# Callers that cannot dot-source this helper (a partial bundle install)
# must treat "helper absent" as NOT-code: skipping a code-graph queue
# append costs one stale entry that the next edit re-queues, while
# defaulting to "code" on an empty regex would match EVERY path.
function Test-VcoIsCodeFile {
    param([string]$Path)
    if (-not $Path) { return $false }
    return ($Path -match $script:VcoCodeExtRe)
}
