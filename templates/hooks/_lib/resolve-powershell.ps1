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
#   Start-VcoDetachedProcess -FilePath $PsExe -ArgumentList @('-NoProfile','-File',$helper)
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
# five call sites. (Since v0.2.105 neither branch below calls Start-Process,
# so the quirk can no longer bite here; QUIRK 5 says why the Windows branch
# dropped it. The rule stands for any Start-Process a hook might add: don't -
# call these wrappers.)
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
# so it cannot accumulate unbounded.
#
# QUIRK 5 (v0.2.105): on Windows a Start-Process child got a NEW console
# that -WindowStyle Hidden only asked to hide - a console window flashed on
# every detached spawn, several per minute in normal use. The Windows branch
# no longer calls Start-Process at all: it hands the spawn to the ONE Python
# home, vco_lib/windowless_spawn.py, which starts the child with
# CREATE_NO_WINDOW (a console that has no window). See the branch itself.

# This lib's own folder, captured while it is being dot-sourced (inside a
# function $PSScriptRoot is not guaranteed to name it on every edition): the
# Windows branch finds resolve-vco-venv.ps1 and the hooks folder from it.
$script:VcoResolvePowershellLibDir = $PSScriptRoot

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
    # $script:VcoSpawnViaWindowlessHelper (set only by tests, after
    # dot-sourcing) routes a POSIX host through the Windows branch below, so
    # its wiring - spec, helper resolution, pid handshake, -PassThru exit
    # code - runs end to end on a Linux/macOS CI host too.
    if ($null -ne $IsWindows -and -not $IsWindows -and -not $script:VcoSpawnViaWindowlessHelper) {
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
    # Windows (v0.2.105): NEVER Start-Process. Start-Process gives every
    # console child a NEW console (CREATE_NEW_CONSOLE / ShellExecute) and only
    # ASKS for it to be hidden (-WindowStyle Hidden is a SW_HIDE show-window
    # hint on that new console - which the console host may show anyway, e.g.
    # when Windows 11 hands new consoles to its default terminal). And .NET's
    # own Process.Start - the only CreateNoWindow spawn PowerShell has - makes
    # the child inherit EVERY inheritable handle of this hook, the harness's
    # stdout pipe included, so a child that outlives the hook would hold that
    # pipe open after the hook exits. So the spawn is handed to the ONE Python
    # home, vco_lib/windowless_spawn.py (stdlib-only, run with -I): it starts
    # the child with CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP (its own
    # console with NO window, which everything it starts shares) and
    # close_fds (no inherited hook pipes), prints the child's pid, and exits.
    # With -PassThru it instead waits for the child and exits with ITS exit
    # code, and the helper's process object is what is returned: WaitForExit /
    # ExitCode then speak for the child. (The helper itself, started by .NET,
    # does inherit the hook's handles - for milliseconds without -PassThru;
    # the -PassThru callers either wait for it or run in an async hook.)
    $tramp = Get-VcoWindowlessTrampoline
    if (-not $tramp.Python) {
        try {
            [Console]::Error.WriteLine(
                "[vco] detached spawn FAILED for '$FilePath': $($tramp.Problem)")
        } catch { }
        return
    }
    $cwd = $WorkingDirectory
    if (-not $cwd) {
        # Start-Process's default: PowerShell's current FileSystem location
        # (not the process cwd, which Set-Location does not move).
        try { $cwd = (Get-Location -PSProvider FileSystem).ProviderPath } catch { $cwd = '' }
    }
    $stdErrTarget = $errPath
    $stdErrAppend = $false
    if (-not $stdErrTarget) {
        # The same shared, bounded diagnostic tail the POSIX branch appends to.
        $stdErrTarget = Get-VcoDetachedErrLog
        $stdErrAppend = [bool]$stdErrTarget
    }
    $specB64 = New-VcoWindowlessSpawnSpec -FilePath $FilePath -ArgumentList $ArgumentList `
        -WorkingDirectory $cwd -StdIn $RedirectStandardInput -StdOut $RedirectStandardOutput `
        -StdErr $stdErrTarget -StdErrAppend $stdErrAppend -Wait ([bool]$PassThru)
    # SOFT-FAIL, BUT NOT SILENT: a spawn that cannot start never breaks the
    # hook (these are best-effort background paths), and always leaves one
    # stderr line naming the executable and the reason. [Console]::Error goes
    # straight to stderr, so it cannot pollute the -PassThru return value.
    $trampProc = $null
    $failure = ''
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $tramp.Python
        $psi.Arguments = Get-VcoWindowlessTrampolineArguments -Script $tramp.Script -SpecB64 $specB64
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $trampProc = [System.Diagnostics.Process]::Start($psi)
        # The pid line arrives once the child has started; EOF without one
        # means the helper reported a failed start on stderr and exited.
        $pidLine = $trampProc.StandardOutput.ReadLine()
        if (-not $pidLine) {
            $trampProc.WaitForExit()
            $failure = ($trampProc.StandardError.ReadToEnd()).Trim()
            if (-not $failure) {
                $failure = "[vco] detached spawn FAILED for '$FilePath': the spawn helper exited $($trampProc.ExitCode) without starting it"
            }
            $trampProc = $null
        }
    } catch {
        $failure = "[vco] detached spawn FAILED for '$FilePath': $($_.Exception.Message)"
        $trampProc = $null
    }
    if ($failure) {
        try { [Console]::Error.WriteLine($failure) } catch { }
    }
    if ($PassThru) { return $trampProc }
}

# The interpreter + script of the windowless spawn helper, resolved once per
# hook process: @{ Python; Script; Problem }. The helper is stdlib-only and
# ships with the orchestrator, so the VCO venv's Python runs it; failing to
# resolve either is a BROKEN install, which the caller reports (never works
# around).
function Get-VcoWindowlessTrampoline {
    if ($script:VcoWindowlessTrampoline) { return $script:VcoWindowlessTrampoline }
    $result = @{ Python = $null; Script = $null; Problem = '' }
    try {
        $libDir = $script:VcoResolvePowershellLibDir
        if (-not $libDir) { $libDir = $PSScriptRoot }
        if (-not (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue)) {
            $venvLib = Join-Path $libDir 'resolve-vco-venv.ps1'
            if (Test-Path -LiteralPath $venvLib -PathType Leaf) { . $venvLib }
        }
        $hooksDir = Split-Path -Parent $libDir
        $py = $null
        $helper = $null
        if (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue) {
            $py = Resolve-VcoVenvPython -ScriptDir $hooksDir
            $helper = Resolve-VcoOrchestratorScript -ScriptDir $hooksDir -RelPath 'vco_lib/windowless_spawn.py'
        }
        if (-not $py) {
            $result.Problem = 'the VCO venv Python could not be resolved (VCT_VENV / VCT_INSTALL_ROOT unset or stale) - a broken install; re-run the orchestrator update'
        } elseif (-not $helper) {
            $result.Problem = 'vco_lib/windowless_spawn.py was not found under the orchestrator root (VCT_INSTALL_ROOT / VCT_ORCHESTRATOR_ROOT) - a broken install; re-run the orchestrator update'
        } else {
            $result.Python = $py
            $result.Script = $helper
        }
    } catch {
        $result.Problem = "the windowless spawn helper could not be resolved: $($_.Exception.Message)"
    }
    if ($result.Python) { $script:VcoWindowlessTrampoline = $result }
    return $result
}

# The spawn spec the helper reads (vco_lib/windowless_spawn.py, `main`):
# UTF-8 JSON, base64-encoded so the command line carries only
# [A-Za-z0-9+/=] - no element can lose its boundaries, quotes or emptiness
# on the way (QUIRK 3 cannot arise). Every argv element is its own JSON
# string.
function New-VcoWindowlessSpawnSpec {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$StdIn,
        [string]$StdOut,
        [string]$StdErr,
        [bool]$StdErrAppend,
        [bool]$Wait
    )
    $argv = New-Object System.Collections.Generic.List[string]
    $argv.Add($FilePath)
    if ($ArgumentList) {
        foreach ($el in $ArgumentList) {
            if ($null -ne $el) { $argv.Add([string]$el) }
        }
    }
    $spec = [ordered]@{
        argv          = $argv.ToArray()
        cwd           = $WorkingDirectory
        stdin         = $StdIn
        stdout        = $StdOut
        stderr        = $StdErr
        stderr_append = $StdErrAppend
        wait          = $Wait
    }
    $json = ConvertTo-Json -InputObject $spec -Compress -Depth 4
    return [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))
}

# The helper's command line: isolated mode (-I: no user site, no PYTHON*
# variables, no script directory on sys.path - nothing in the project folder
# or the caller's env can shadow a stdlib module), the quoted script path,
# then `detach --spec-b64 <b64>`.
function Get-VcoWindowlessTrampolineArguments {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$SpecB64
    )
    return ('-I "{0}" detach --spec-b64 {1}' -f $Script, $SpecB64)
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
