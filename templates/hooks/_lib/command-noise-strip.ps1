# _lib/command-noise-strip.ps1
# The ONE PowerShell-side home for the D-3 "command-noise strip" (the .ps1
# sibling of _lib/command-noise-strip.sh). Turns a raw bash command into a clean
# KG query by dropping noise tokens (flags, paths, shell operators, bare cwd
# dots) so a bare cd/ls yields little query signal instead of injecting
# directory-keyword KG.
#
# Why a shared helper (CLAUDE.md "one concern, one home" + coordinator SF-1/N-3):
# the strip was inlined in pre-bash-context-inject.sh, its .ps1, AND the test.
# This file is the single PowerShell home; pre-bash-context-inject.ps1 sources it.
# MUST MATCH command-noise-strip.sh (same token rules + code-file extension list;
# the Python token logic is identical in both helpers' embedded snippet).
#
# Plain ASCII only. Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoCommandNoiseStripSourced) { return }
$script:VcoCommandNoiseStripSourced = $true

# Get-VcoCommandNoiseStripped <Command> [-Py <pythonExe>]
# Return the noise-stripped query. Requires a Python interpreter ($Py, normally
# the $PY resolved by find-python.ps1). With no interpreter, returns the input
# unchanged (soft-fail). The Python token rules live HERE, once.
function Get-VcoCommandNoiseStripped {
    param([string]$Command, [string]$Py = "")
    if (-not $Py) { $Py = $script:PY }
    if (-not $Py) { return $Command }
    $stripCode = @'
import re, sys
cmd = sys.stdin.read()
toks = []
for t in cmd.split():
    if t.startswith('-'):
        continue
    if t in ('|', '||', '&&', ';', '>', '>>', '<', '2>', '2>&1', '&', '.', '..', '*'):
        continue
    if '/' in t:
        base = t.rstrip('/').split('/')[-1]
        if re.search(r'\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$', base):
            toks.append(base)
        continue
    toks.append(t)
print(' '.join(toks).strip())
'@
    try {
        $stripped = ($Command | & $Py -c $stripCode 2>$null)
        if ($stripped) { return $stripped.Trim() }
    } catch { }
    return $Command
}
