# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# resolve-powershell.ps1 - pick the PowerShell executable for child spawns.
#
# v0.2.54 Track G (G-6): several hooks spawned helper scripts with a
# hardcoded `pwsh` (PowerShell 7+). The hooks THEMSELVES are launched via
# `powershell` (5.1, ships with every Windows 10+) per
# settings.json.windows.template - so on machines without PowerShell 7
# the hook body ran fine but every child spawn (`kg-sync.ps1`,
# `vct_access_check.ps1`, `code-graph-incremental.ps1`, ...) failed
# silently: KG sync, write-gate, dup-detection and code-graph updates
# were all lost with no error surfaced.
#
# Dot-source this file, then use $PsExe instead of a literal "pwsh":
#
#   . (Join-Path $ScriptDir "_lib/resolve-powershell.ps1")
#   & $PsExe -NoProfile -File $helper @args
#   Start-Process -FilePath $PsExe -ArgumentList @('-NoProfile','-File',$helper)
#
# Preference order: pwsh (7+, faster startup, the flavour the helpers are
# tested against) -> powershell (5.1 fallback - the helpers stick to
# 5.1-compatible syntax per the hook portability discipline).
$PsExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }

# ---------------------------------------------------------------------------
# Detached child spawn (v0.2.92 MAJOR-1, extended R2).
#
# ONE HOME for the three `Start-Process` quirks that silently destroy a spawn.
# The first two are cmdlet-level PARAMETER REJECTIONS: the process is never
# created at all, so the work the hook detached simply never happens and
# nothing is logged. Every VCO hook that detaches a child must go through the
# wrappers below rather than calling Start-Process itself.
#
# QUIRK 1: `-WindowStyle` is Windows-only. On PowerShell 7 for Linux/macOS
# the parameter is REJECTED ("The parameter '-WindowStyle' is not supported
# for the cmdlet 'Start-Process' on this edition of PowerShell"). Dropping it
# unconditionally is not an option either: on Windows it is what stops a
# console window flashing on every hook fire. Beyond the field impact, an
# unguarded site is UNTESTABLE on a Linux/macOS CI host -- the spawn dies
# before it can be observed -- which is why the guard lives here and not at
# five call sites.
#
# QUIRK 2: `-RedirectStandardOutput` and `-RedirectStandardError` may not
# name the SAME file, on any edition: "This command cannot be run because
# RedirectStandardOutput and RedirectStandardError are same." The POSIX
# siblings write `>> log 2>&1`, so transcribing that to PowerShell as two
# identical paths is the natural mistake -- and it kills the spawn outright
# (`kg-summary-generator.ps1` shipped exactly that). Callers may pass the same
# path to express "one log"; stderr is diverted to `<path>.err` so the output
# stays observable instead of the process never starting.
#
# QUIRK 3: elements containing spaces must be quoted by the caller --
# Start-Process joins an array with spaces and lets the child re-split it.
# The cmdlet does NO quoting of its own: it concatenates an ARRAY
# -ArgumentList with single spaces and hands the result to the child as ONE
# command line, which the child re-parses. An element containing whitespace
# loses its boundaries, an embedded double quote is consumed as a quoting
# character, and an EMPTY element vanishes entirely. This is not theoretical:
# `post-git-commit-kg-sync.ps1` passed its whole review prompt (a here-string
# carrying `git diff` output) on the command line, and the Claude CLI
# rejected the diff's `---` token as an option ("error: unknown option
# '---'", exit 1) on EVERY non-empty commit -- so the commit review never
# actually ran. The wrapper below encodes each element per the
# CommandLineToArgvW rule -- an element that is EMPTY or contains
# whitespace, a double quote or (for safety) a trailing backslash is wrapped
# in quotes; inside the wrapping every `"` becomes `\"` and every run of
# backslashes immediately before a quote being escaped or before the closing
# quote is DOUBLED -- and passes the JOINED STRING as -ArgumentList: both
# CreateProcess children and .NET's Unix-edition splitter honour that
# encoding, and both Windows PowerShell 5.1 and pwsh accept a single string.
#
# WHY Start-Process AND NOT Start-Job: a job's child is torn down when the
# host process exits. Hooks exit immediately; the work they detach (a KG sync,
# a whole-collection duplicate scan) takes
# seconds. Start-Process spawns an independent OS process that survives, which
# is what the POSIX siblings get from `( ... ) &` / setsid. Same reasoning as
# `_lib/kg-sync-debounce.ps1`'s flusher.
#
# QUIRK 4 (v0.2.103; redesigned same release after review R3 HIGH-1): on
# POSIX, a child spawned with NO stream redirects INHERITS the hook's own
# stdout/stderr pipes - the harness then waits for EOF while a detached
# 30-second job holds the write end open. But fixing that with
# `Start-Process -RedirectStandard*` is WORSE: on Unix pwsh implements
# those parameters by handing the child PIPES back to the parent and
# copying them into the files; when the hook exits nobody drains them, so
# the child's first write after hook-exit hits EPIPE and it dies having
# logged nothing (verified: `Start-Process python3 -RedirectStandardOutput
# /dev/null -RedirectStandardError <log>` -> BrokenPipeError(32) on both
# streams, log left empty). So on POSIX the wrapper never uses
# Start-Process at all: it launches `/bin/sh -c 'exec "$0" "$@"
# </dev/null >/dev/null 2>><log>'` with a ProcessStartInfo whose
# ArgumentList carries the script and argv as SEPARATE elements (no
# re-splitting), making the redirection a kernel-level fd duplication the
# child owns outright - exactly what the .sh siblings do. Caller-provided
# redirect paths are folded into the same exec line (which also fixes the
# pre-existing EPIPE on explicit-redirect callers such as
# stop-drain-citations.ps1). The default stderr log lives under
# $VCT_STATE_DIR/logs (default ~/.vct/logs) with a size bound, not /tmp,
# so it cannot accumulate unbounded. Windows behaviour is unchanged:
# hidden window, Start-Process, caller-provided redirects honoured
# exactly as before.

# True when this edition accepts `-WindowStyle`. $IsWindows is undefined on
# PowerShell 5.1 (which only exists on Windows), so "unset" means Windows.
function Test-VcoHiddenWindowSupported {
    return (($null -eq $IsWindows) -or $IsWindows)
}

# Bound for the shared detached-children stderr log (same diagnostic-tail
# bound the kg-sync-debounce log uses). Past it the log is restarted
# rather than grown without limit.
$script:VcoDetachedErrLogMaxBytes = 262144

# Default stderr target for POSIX detached children:
# <state>/logs/detached-children.log, where <state> is $VCT_STATE_DIR or
# ~/.vct (the same two-branch rule `_lib/metrics-dir.ps1` resolves; kept
# inline because this lib must stay loadable without metrics-dir.ps1).
# "" when the path is unusable - the caller then falls back to /dev/null.
function Get-VcoDetachedErrLog {
    try {
        $home_ = [System.Environment]::GetFolderPath('UserProfile')
        if (-not $home_) { $home_ = $env:USERPROFILE }
        if (-not $home_) { $home_ = $env:HOME }
        $stateRoot = if ($env:VCT_STATE_DIR) { $env:VCT_STATE_DIR }
                     elseif ($home_) { Join-Path $home_ ".vct" }
                     else { "" }
        if (-not $stateRoot) { return "" }
        $dir = Join-Path $stateRoot "logs"
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
            New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
        }
        $log = Join-Path $dir "detached-children.log"
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            if ((Get-Item -LiteralPath $log -ErrorAction Stop).Length -gt $script:VcoDetachedErrLogMaxBytes) {
                Set-Content -LiteralPath $log -Value "" -NoNewline -ErrorAction SilentlyContinue
            }
        }
        return $log
    } catch { return "" }
}

# Quote $Path for embedding in the POSIX `exec` line (single quotes, with
# embedded single quotes escaped per POSIX sh).
function ConvertTo-VcoShQuoted {
    param([string]$Path)
    return "'" + ($Path -replace "'", "'\''") + "'"
}

# Generic guarded Start-Process. Returns the process object when -PassThru is
# given (so a caller can WaitForExit), otherwise nothing.
function Start-VcoDetachedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$RedirectStandardOutput,
        [string]$RedirectStandardError,
        [string]$RedirectStandardInput,
        [switch]$PassThru
    )
    $spawn = @{
        FilePath    = $FilePath
        ErrorAction = 'SilentlyContinue'
    }
    # QUIRK 2 diversion first: identical redirect targets are rejected by
    # Start-Process on every edition (Windows branch below) and would leave
    # two fd offsets interleaving on one file (POSIX branch below) - divert
    # either way.
    $errPath = $RedirectStandardError
    if ($RedirectStandardOutput -and $errPath -and ($errPath -eq $RedirectStandardOutput)) {
        $errPath = "$RedirectStandardError.err"
    }
    # QUIRK 4 above: on POSIX NEVER hand the child pipes through
    # Start-Process -Redirect* (the hook exits, nobody drains the pipes,
    # the child's first write gets EPIPE). Launch /bin/sh with a
    # kernel-level fd redirection instead; caller-supplied redirect paths
    # are folded into the same exec line (which also fixes the pre-existing
    # EPIPE on explicit-redirect callers such as stop-drain-citations.ps1).
    if ($null -ne $IsWindows -and -not $IsWindows) {
        $proc = $null
        $failure = ''
        try {
            $inT = if ($RedirectStandardInput) { $RedirectStandardInput } else { '/dev/null' }
            $outT = if ($RedirectStandardOutput) { $RedirectStandardOutput } else { '/dev/null' }
            $errT = '/dev/null'
            if ($errPath) {
                $errT = $errPath
            } else {
                $errT = Get-VcoDetachedErrLog
                if (-not $errT) { $errT = '/dev/null' }
            }
            # Pre-validate in the PARENT what Start-Process used to validate
            # itself: a bad -WorkingDirectory or an unwritable redirect
            # target must soft-fail WITH a diagnostic, not die silently in
            # the child's sh (nothing would report it once the hook is gone).
            if ($WorkingDirectory -and -not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
                throw "WorkingDirectory '$WorkingDirectory' does not exist"
            }
            foreach ($t in @($inT, $outT, $errT)) {
                if ($t -and $t -ne '/dev/null') {
                    $tparent = Split-Path -Parent $t
                    if (-not (Test-Path -LiteralPath $tparent -PathType Container)) {
                        throw "cannot redirect to '$t' - directory '$tparent' does not exist"
                    }
                }
            }
            # Redirection semantics: caller-supplied targets TRUNCATE
            # (`>`), matching Start-Process -Redirect* on Windows; the
            # shared default stderr log APPENDS (`2>>`) - it is one bounded
            # diagnostic tail for every detached child, not a per-spawn
            # snapshot.
            $errOp = if ($errPath) { '2>' } else { '2>>' }
            $psi = [System.Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = '/bin/sh'
            $psi.UseShellExecute = $false
            if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
            # ONE -c script element + one element per argv: ArgumentList
            # passes each element as its own argv slot, so "$0"/"$@" keep
            # spaces, quotes, newlines and empty elements intact (no QUIRK-3
            # re-splitting on this path).
            [void]$psi.ArgumentList.Add('-c')
            [void]$psi.ArgumentList.Add(('exec "$0" "$@" <{0} >{1} {2}{3}' -f `
                (ConvertTo-VcoShQuoted $inT), (ConvertTo-VcoShQuoted $outT), `
                $errOp, (ConvertTo-VcoShQuoted $errT)))
            [void]$psi.ArgumentList.Add($FilePath)
            if ($ArgumentList) {
                foreach ($el in $ArgumentList) {
                    if ($null -ne $el) { [void]$psi.ArgumentList.Add([string]$el) }
                }
            }
            $proc = [System.Diagnostics.Process]::Start($psi)
        } catch {
            $failure = $_.Exception.Message
        }
        if ($failure) {
            try {
                [Console]::Error.WriteLine(
                    "[vco] detached spawn FAILED for '$FilePath': $failure")
            } catch { }
        }
        if ($PassThru) { return $proc }
        return
    }
    # PowerShell 5.1 rejects an EMPTY -ArgumentList (ValidateNotNullOrEmpty),
    # so omit the parameter entirely rather than passing @().
    if ($ArgumentList -and $ArgumentList.Count -gt 0) {
        # QUIRK 3 above: an ARRAY is space-joined and re-split by the child.
        # Encode each element per CommandLineToArgvW and pass ONE joined
        # string, so whitespace, embedded quotes, backslashes and empty
        # elements all survive the roundtrip.
        $parts = foreach ($el in $ArgumentList) {
            if ($el -eq '' -or $el -match '\s' -or $el.Contains('"') -or $el.EndsWith('\')) {
                '"' + ($el -replace '(\\*)"', '$1$1\"' -replace '(\\*)$', '$1$1') + '"'
            } else {
                $el
            }
        }
        $spawn['ArgumentList'] = $parts -join ' '
    }
    if ($WorkingDirectory) { $spawn['WorkingDirectory'] = $WorkingDirectory }
    if ($RedirectStandardInput) { $spawn['RedirectStandardInput'] = $RedirectStandardInput }
    if ($RedirectStandardOutput) { $spawn['RedirectStandardOutput'] = $RedirectStandardOutput }
    # QUIRK 2 diversion already applied to $errPath above (shared with the
    # POSIX branch).
    if ($errPath) { $spawn['RedirectStandardError'] = $errPath }
    if (Test-VcoHiddenWindowSupported) { $spawn['WindowStyle'] = 'Hidden' }
    # SOFT-FAIL, BUT NOT SILENT. A spawn that cannot start must never break
    # the hook (these are all best-effort background paths), but swallowing
    # the reason is how the two quirks above survived for releases: the work
    # stopped happening and NOTHING said so. So: never throw, and always emit
    # one line naming the executable and the cmdlet's own message.
    # [Console]::Error goes straight to stderr, so it cannot pollute the
    # -PassThru return value the way Write-Error/Write-Output would.
    # BOTH error shapes must be caught. `Start-Process` reports some failures
    # as NON-terminating errors (an invalid -WorkingDirectory) and others by
    # ThrowTerminatingError (an unwritable -RedirectStandardOutput path);
    # `-ErrorAction SilentlyContinue` only governs the first, so an
    # ErrorVariable alone still lets PowerShell print its whole error block
    # and, in a hook, that lands wherever the hook's stderr goes.
    $spawnErr = $null
    $proc = $null
    $failure = ''
    if ($PassThru) { $spawn['PassThru'] = $true }
    try {
        $proc = Start-Process @spawn -ErrorVariable spawnErr
        if ($spawnErr -and $spawnErr.Count -gt 0) { $failure = [string]$spawnErr[0] }
    } catch {
        $failure = $_.Exception.Message
    }
    if ($failure) {
        try {
            [Console]::Error.WriteLine(
                "[vco] detached spawn FAILED for '$FilePath': $failure")
        } catch { }
    }
    if ($PassThru) { return $proc }
}

# Detached PowerShell child. $Command is a PowerShell expression run in the
# child; it is passed as -EncodedCommand (UTF-16LE base64) so quoting and
# non-ASCII survive intact.
function Start-VcoDetachedPwsh {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string]$PowerShellExe,
        # R31 L-4: hand back the child process object (nothing on a failed
        # spawn - Start-VcoDetachedProcess never throws), so a caller can
        # tell "spawned" from "reported failed on stderr".
        [switch]$PassThru
    )
    $exe = if ($PowerShellExe) { $PowerShellExe } elseif ($script:PsExe) { $script:PsExe } else { 'pwsh' }
    $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($Command))
    if ($PassThru) {
        return Start-VcoDetachedProcess -FilePath $exe -ArgumentList @(
            '-NoProfile', '-NonInteractive', '-EncodedCommand', $enc
        ) -PassThru
    }
    Start-VcoDetachedProcess -FilePath $exe -ArgumentList @(
        '-NoProfile', '-NonInteractive', '-EncodedCommand', $enc
    )
}
