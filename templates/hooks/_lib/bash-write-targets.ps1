# _lib/bash-write-targets.ps1
# THE PowerShell-side home for "which files did / will this Bash command
# write?" -- sibling of _lib/bash-write-targets.sh (v0.2.95, lane F10).
#
# Two hooks need the same answer for different reasons:
#   * post-bash-file-sync.ps1 -- AFTER the command, to route what was
#     written through _lib/route-touched-path.ps1, so a CLI write syncs
#     like an Edit/Write;
#   * pre-bash-context-inject.ps1 -- BEFORE the command, to build the
#     retrieval query from the TARGET PATH the way
#     pre-edit-context-inject.ps1 does.
#
# The parse itself is vco_lib/bash_write_targets.py, shared with the .sh
# sibling: it is the same decision on both operating systems and a
# mirrored implementation would drift.
#
# MUST MATCH: templates/hooks/_lib/bash-write-targets.sh -- in particular
# Test-VcoWriteSuspicious and vco_bash_write_prefilter must accept the same
# commands, which tests/test_v0295_bash_write_sync.py pins.
#
# Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoBashWriteTargetsSourced) { return }
$script:VcoBashWriteTargetsSourced = $true

$script:VcoBwtPython = ""

# Initialize-VcoBashWriteTargets <HooksDir> [FallbackPython]
#
# The parser needs `import vco_lib`, so it runs on the VCO venv. When no
# VCO venv resolves we fall back to `python` on PATH with PYTHONPATH
# pointing at the project root -- that works inside the orchestrator clone
# and soft-fails (empty stdout) anywhere else. A broken install must never
# break the user's Bash.
function Initialize-VcoBashWriteTargets {
    param([string]$HooksDir, [string]$FallbackPython = "")

    $venvLib = Join-Path $HooksDir "_lib/resolve-vco-venv.ps1"
    if (Test-Path $venvLib) { . $venvLib }
    $resolved = ""
    if (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue) {
        $resolved = Resolve-VcoVenvPython -ScriptDir $HooksDir
    }
    if (-not $resolved) { $resolved = $FallbackPython }
    if (-not $resolved) {
        $resolved = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
    $script:VcoBwtPython = $resolved
}

# Test-VcoWriteSuspicious <Command>
#
# Runs on EVERY Bash call, so it must cost ~nothing and must not spawn.
# The `/dev/null` sinks and fd duplications are stripped FIRST because
# `2>&1` and `>/dev/null` appear in a large fraction of all commands.
#
# A BARE `knowledge/` / `docs/` MENTION IS NOT A WRITE (v0.2.95 review
# MAJOR-3). Until that review this function returned $true for ANY command
# containing either string -- before it even looked for a redirect -- so
# `cat docs/x.md`, `grep -rn foo knowledge/` and `ls docs/` each started a
# Python interpreter, ran the mtime scan and re-routed every knowledge/docs
# file touched in the last 300 s, including files the Edit tool had already
# synced seconds earlier. The directory trigger now requires a companion:
# one of the opaque writers below, whose target genuinely cannot be read out
# of the command text. Anything that shows its write in the text -- a
# redirect, a heredoc, a write verb -- is caught above and never needed the
# directory trigger at all.
#
# `install(1)` is deliberately absent: `npm install` / `pip install` are
# far more common than install(1) as a file copier.
#
# MUST MATCH _lib/bash-write-targets.sh's vco_bash_write_prefilter;
# tests/test_v0295_bash_write_sync.py runs both against one corpus.
function Test-VcoWriteSuspicious {
    param([string]$Command)
    if (-not $Command) { return $false }
    $s = $Command
    foreach ($noise in @('2>&1', '1>&2', '>&2', '&>/dev/null', '2>/dev/null',
                         '1>/dev/null', '>/dev/null', '> /dev/null')) {
        $s = $s.Replace($noise, '')
    }
    if ($s.Contains('>') -or $s.Contains('<<')) { return $true }
    # Shell separators become spaces so a verb that opens a pipeline stage or
    # follows a `;`/`&&` is still word-matched -- the same boundary the .sh
    # sibling gets from its `${s//[;&|()]/ }` substitution.
    $t = " " + ($s -replace '[;&|()]', ' ') + " "
    foreach ($verb in @(' tee ', ' cp ', ' mv ', ' touch ', ' dd ')) {
        if ($t.Contains($verb)) { return $true }
    }
    foreach ($frag in @('sed -i', 'sed --in-place', 'perl -i', 'ruby -i',
                        '--output', '--outfile', '--out-file', '--output-file',
                        'Set-Content', 'Add-Content', 'Out-File', 'Tee-Object')) {
        if ($t.Contains($frag)) { return $true }
    }
    # No write of its own: worth parsing only if it NAMES a scanned directory
    # AND could have written into it opaquely.
    if (-not ($Command.Contains("knowledge/") -or $Command.Contains("docs/"))) {
        return $false
    }
    foreach ($writer in @(' python ', ' python3 ', ' perl ', ' ruby ',
                          ' node ', ' php ', ' Rscript ',
                          ' patch ', ' rsync ',
                          ' git checkout ', ' git restore ', ' git apply ',
                          ' git stash ', ' git reset ')) {
        if ($t.Contains($writer)) { return $true }
    }
    return $false
}

# Invoke-VcoBashWriteParser <Command> <ProjectRoot> <ExtraArgs>
# Internal: run the shared parser with PYTHONPATH scoped to this call.
function Invoke-VcoBashWriteParser {
    param([string]$Command, [string]$ProjectRoot, [string[]]$ExtraArgs)
    if (-not $Command -or -not $ProjectRoot -or -not $script:VcoBwtPython) { return @() }
    $prev = $Env:PYTHONPATH
    if ($prev) {
        $Env:PYTHONPATH = "$ProjectRoot$([System.IO.Path]::PathSeparator)$prev"
    } else {
        $Env:PYTHONPATH = $ProjectRoot
    }
    try {
        $argv = @('-m', 'vco_lib.bash_write_targets', '--project-root', $ProjectRoot) + $ExtraArgs
        $raw = $Command | & $script:VcoBwtPython @argv 2>$null
        if ($null -eq $raw) { return @() }
        return @($raw)
    } catch {
        return @()
    } finally {
        $Env:PYTHONPATH = $prev
    }
}

# Get-VcoBashWriteTargets <Command> <ProjectRoot> [ScanState]
# Absolute paths that EXIST (the command has already run).
function Get-VcoBashWriteTargets {
    param([string]$Command, [string]$ProjectRoot, [string]$ScanState = "")
    $extra = @('--require-exists')
    if ($ScanState) { $extra += @('--scan-state', $ScanState) }
    return @(Invoke-VcoBashWriteParser -Command $Command -ProjectRoot $ProjectRoot -ExtraArgs $extra |
             Where-Object { $_ -and $_.ToString().Trim() })
}

# Get-VcoBashWritePreBash <Command> <ProjectRoot>
# Two entries: the write TARGET to build the query from (may be empty) and
# a content SNIPPET (may be empty). Existence is NOT required -- the file
# is about to be created.
function Get-VcoBashWritePreBash {
    param([string]$Command, [string]$ProjectRoot)
    $lines = @(Invoke-VcoBashWriteParser -Command $Command -ProjectRoot $ProjectRoot -ExtraArgs @('--format', 'prebash'))
    $target = if ($lines.Count -ge 1 -and $null -ne $lines[0]) { [string]$lines[0] } else { "" }
    $snippet = if ($lines.Count -ge 2 -and $null -ne $lines[1]) { [string]$lines[1] } else { "" }
    return @($target.Trim(), $snippet.Trim())
}
