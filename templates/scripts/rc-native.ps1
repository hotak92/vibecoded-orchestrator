# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# rc-native.ps1 — Claude Code Remote Control as a detached native-auth
# background server (PowerShell sibling of rc-native.sh; same commands,
# same state layout, same messages).
#
# Runs `claude remote-control` (server mode) with the gateway's routing
# env vars stripped from its environment, detached from this console, and
# prints the claude.ai/code join URL. Why this exists: a VS Code panel
# pointed at the VCO model gateway sets ANTHROPIC_BASE_URL, and Remote
# Control is ENDPOINT-gated (Claude Code v2.1.196+): it initializes ONLY
# in sessions talking directly to api.anthropic.com, even when signed in
# with claude.ai — API keys and setup/OAuth env tokens are refused by a
# second, independent full-scope-login gate. Do not downgrade Claude Code
# below v2.1.196 to get Remote Control through the gateway — auto-update
# reverts the downgrade and takes a month of fixes with it.
#
# Windows mechanisms chosen for the POSIX originals (setsid and the
# never-EOFing stdin pipe have no direct cmdlet counterpart):
#   * Detachment — a hidden PowerShell wrapper process started with
#     Start-Process: it is independent of this console (Windows child
#     processes are not tied to the parent's lifetime), so it survives
#     this window closing exactly like a setsid session.
#   * Never-EOFing stdin — the wrapper starts the server with a
#     redirected StandardInput pipe, writes the one-time consent answer
#     ("y"), and holds the write end open until the server exits (its
#     WaitForExit). This is the equivalent of bash's
#     `(printf "y\n"; exec sleep infinity) |` feeding the detached TUI.
#   * Process-group kill — `stop` runs `taskkill /PID <wrapper> /T /F`,
#     which terminates the whole tree (wrapper + cmd relay + claude),
#     the equivalent of `kill -- -PGID`.
#   * Output to the log — the server is launched through cmd.exe so the
#     `>> log 2>&1` redirection is done by cmd; no .NET stream pumping.
#
# Operational facts encoded here (verified against Claude Code 2.1.258):
#   1. Workspace trust is a CLI concept, separate from VS Code panel
#      usage: server mode from a directory used only in the panel fails
#      with "Workspace not trusted". Remedy: ONE interactive `claude` run
#      in that directory, accepting the trust dialog — not automatable
#      headlessly. The script detects the string and says exactly this.
#   2. The first server run asks "Enable Remote Control? (y/n)" once;
#      acceptance persists. The wrapper feeds the `y` on every start.
#   3. The server is session-detached, not a service: it does NOT survive
#      a reboot. `start` brings it back (idempotent), which is expected.
#   4. Trust is never saved for the home directory — the script refuses
#      to start there.
#
# Compatible with Windows PowerShell 5.1 and PowerShell 7+.
#
# Usage:
#   rc-native.ps1 start [name]    # detached, idempotent; prints the join URL
#   rc-native.ps1 status [name]   # pid + current join URL
#   rc-native.ps1 url [name]      # print just the URL
#   rc-native.ps1 stop [name]     # kill the whole process tree
#   rc-native.ps1 logs [name]     # tail the log (URL redacted)
#
# `name` defaults to the current directory's basename. State lives under
# ~\.cache\rc-native\ (pidfile + log).

[CmdletBinding()]
param(
    [Parameter(Position = 0)] [string]$Command = 'help',
    [Parameter(Position = 1)] [string]$Name = ''
)

$ErrorActionPreference = 'Stop'

# Gateway routing vars stripped from the server's environment (same list
# as rc-native.sh): with them gone the session reaches api.anthropic.com
# with stock auth.
$StripEnvKeys = @(
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL',
    'ANTHROPIC_DEFAULT_FABLE_MODEL',
    'ANTHROPIC_SMALL_FAST_MODEL',
    'CLAUDE_CODE_SUBAGENT_MODEL',
    'CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY'
)

$Dir = Join-Path $Home '.cache\rc-native'
if (-not $Name) { $Name = Split-Path -Leaf (Get-Location).Path }
$PidFile = Join-Path $Dir ('{0}.pid' -f $Name)
$LogFile = Join-Path $Dir ('{0}.log' -f $Name)

if (-not (Test-Path $Dir)) {
    New-Item -ItemType Directory -Path $Dir | Out-Null
    # umask 077 equivalent: the log holds the join URL (a capability), so
    # restrict the state dir to the current user. Best-effort — a locked-
    # down failure is reported by icacls itself and is not fatal; `logs`
    # redacts the URL regardless.
    try {
        & icacls $Dir /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F" | Out-Null
    } catch { }
}

function Write-ErrLine {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Get-PidFromFile {
    if (-not (Test-Path $PidFile)) { return 0 }
    $raw = Get-Content $PidFile -Raw -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($raw)) { return 0 }
    $parsed = 0
    if (-not [int]::TryParse($raw.Trim(), [ref]$parsed)) { return 0 }
    return $parsed
}

function Test-Alive {
    $p = Get-PidFromFile
    if ($p -le 0) { return $false }
    return [bool](Get-Process -Id $p -ErrorAction SilentlyContinue)
}

function Get-JoinUrl {
    if (-not (Test-Path $LogFile)) { return $null }
    $hits = Select-String -Path $LogFile -Pattern 'https://claude\.ai/code\?environment=[A-Za-z0-9_-]+' -AllMatches
    if (-not $hits) { return $null }
    $last = $hits[-1]
    return $last.Matches[$last.Matches.Count - 1].Value
}

function Test-LogHas {
    param([string]$Pattern)
    if (-not (Test-Path $LogFile)) { return $false }
    return [bool](Select-String -Path $LogFile -Pattern $Pattern -SimpleMatch -Quiet)
}

function Show-Usage {
    Write-Output @'
rc-native - Claude Code Remote Control as a detached background server.

  rc-native start [name]    detached, idempotent; prints the join URL
  rc-native status [name]   pid + current join URL
  rc-native url [name]      print just the URL
  rc-native stop [name]     kill the server (whole process tree)
  rc-native logs [name]     tail the log (URL redacted)

`name` defaults to the current directory's basename. State lives under
~\.cache\rc-native\. The server does not survive a reboot; start brings
it back. Requires a full-scope claude.ai login (API keys and setup tokens
are refused by Remote Control).
'@
}

switch ($Command) {
    'start' {
        if ((Get-Location).Path -eq $Home) {
            Write-ErrLine ("rc-native: refusing to start from `$HOME - the trust dialog never saves trust for the home directory. cd into a project first.")
            exit 1
        }
        if (Test-Alive) {
            Write-Output ("rc-native: '{0}' already running (pid {1})." -f $Name, (Get-PidFromFile))
            $u = Get-JoinUrl
            if ($u) { Write-Output ("  {0}" -f $u) }
            exit 0
        }
        $claude = Get-Command claude -ErrorAction SilentlyContinue
        if (-not $claude) {
            Write-ErrLine "rc-native: claude not found on PATH."
            exit 1
        }
        # Rotate the previous log (it holds the old join URL, now stale).
        if (Test-Path $LogFile) { Move-Item -Force $LogFile ($LogFile + '.old') }

        # The interpreter to relaunch the wrapper with (the same PowerShell
        # host that is running this script).
        $psExe = (Get-Process -Id $PID).Path
        if ([string]::IsNullOrEmpty($psExe)) {
            $psExe = Join-Path $PSHOME 'powershell.exe'
        }

        # Inner command line, run by cmd.exe: cmd performs the `>> log 2>&1`
        # redirection and relays the wrapper's stdin pipe to claude.
        $innerArgs = 'remote-control --no-create-session-in-dir --name "{0}"' -f $Name
        $target = $claude.Source
        if ($target -like '*.ps1') {
            # A .ps1 shim cannot be executed by cmd.exe; run it via the
            # running PowerShell host ($psExe — 5.1 or 7).
            $inner = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" {2}' -f $psExe, $target, $innerArgs
        } else {
            $inner = '"{0}" {1}' -f $target, $innerArgs
        }
        $cmdArgs = '/c "{0} >> "{1}" 2>&1"' -f $inner, $LogFile

        # The wrapper script, embedded base64-safe: it owns the server's
        # stdin pipe for the server's whole lifetime (see header).
        $wrapperTemplate = @'
$ErrorActionPreference = 'Stop'
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $env:ComSpec
$psi.Arguments = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('__CMD_ARGS__'))
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.CreateNoWindow = $true
$keys = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('__B64_KEYS__')).Split('|')
foreach ($k in $keys) { [void]$psi.EnvironmentVariables.Remove($k) }
$p = [System.Diagnostics.Process]::Start($psi)
# Answer the one-time "Enable Remote Control? (y/n)" consent prompt; the
# stream stays open for this wrapper's whole lifetime, which is what keeps
# the detached server's stdin from seeing EOF.
$p.StandardInput.WriteLine('y')
$p.StandardInput.Flush()
$p.WaitForExit()
'@
        $cmdArgsB64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmdArgs))
        $stripKeysB64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(($StripEnvKeys -join '|')))
        $wrapperScript = $wrapperTemplate.Replace('__CMD_ARGS__', $cmdArgsB64).Replace('__B64_KEYS__', $stripKeysB64)
        $b64Wrapper = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapperScript))

        # -WindowStyle exists only on Windows editions of PowerShell (5.1 and
        # Windows pwsh); on Unix editions the parameter is absent and would
        # abort the call. On Unix the .sh sibling is the intended path — here
        # we simply launch without it so the script still works under pwsh.
        $onWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or ($PSVersionTable.Platform -eq 'Win32NT')
        if ($onWindows) {
            $proc = Start-Process -FilePath $psExe `
                -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $b64Wrapper) `
                -WindowStyle Hidden -PassThru
        } else {
            $proc = Start-Process -FilePath $psExe `
                -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $b64Wrapper) `
                -PassThru
        }
        Set-Content -Path $PidFile -Value $proc.Id -NoNewLine -Encoding Ascii

        # Wait for readiness, surfacing the known failure mode by name.
        $ready = $false
        for ($i = 0; $i -lt 25; $i++) {
            Start-Sleep -Seconds 1
            if (Test-LogHas 'Workspace not trusted') {
                Write-ErrLine ("rc-native: '{0}' failed - workspace {1} is not trusted for the CLI." -f $Name, (Get-Location).Path)
                Write-ErrLine "  Fix: run \`claude\` in this directory once (interactive) and accept the trust dialog, then re-run start. VS Code panel usage does NOT record CLI trust."
                & taskkill /PID $proc.Id /T /F 2>$null | Out-Null
                Remove-Item $PidFile -ErrorAction SilentlyContinue
                exit 1
            }
            $u = Get-JoinUrl
            if ($u -and (Test-LogHas 'Ready')) {
                Write-Output ("rc-native: '{0}' running (pid {1})." -f $Name, $proc.Id)
                Write-Output '  Join from phone app (Code tab) or browser:'
                Write-Output ("  {0}" -f $u)
                $ready = $true
                break
            }
            if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) { break }
        }
        if (-not $ready) {
            Write-ErrLine ("rc-native: '{0}' did not report Ready within 25s - check the logs command." -f $Name)
            if (Test-Path $LogFile) {
                Get-Content $LogFile -Tail 5 | ForEach-Object { Write-ErrLine $_ }
            }
            exit 1
        }
    }
    'status' {
        if (Test-Alive) {
            Write-Output ("rc-native: '{0}' running (pid {1})." -f $Name, (Get-PidFromFile))
            $u = Get-JoinUrl
            if ($u) {
                Write-Output ("  {0}" -f $u)
            } else {
                Write-Output '  (no join URL in the log yet)'
            }
        } else {
            Write-Output ("rc-native: '{0}' not running." -f $Name)
            exit 1
        }
    }
    'url' {
        if (-not (Test-Alive)) {
            Write-ErrLine ("rc-native: '{0}' is not running (start it first)." -f $Name)
            exit 1
        }
        $u = Get-JoinUrl
        if (-not $u) {
            Write-ErrLine ("rc-native: no join URL in {0} yet." -f $LogFile)
            exit 1
        }
        Write-Output $u
    }
    'stop' {
        if (Test-Alive) {
            $p = Get-PidFromFile
            # taskkill /T terminates the whole process TREE (wrapper + cmd
            # relay + claude) - the Windows equivalent of `kill -- -PGID`.
            & taskkill /PID $p /T /F 2>$null | Out-Null
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            Write-Output ("rc-native: '{0}' stopped." -f $Name)
        } else {
            if (Test-Path $PidFile) { Remove-Item $PidFile -ErrorAction SilentlyContinue }
            Write-Output ("rc-native: '{0}' was not running." -f $Name)
        }
    }
    'logs' {
        if (-not (Test-Path $LogFile)) {
            Write-ErrLine ("rc-native: no log for '{0}'." -f $Name)
            exit 1
        }
        Get-Content $LogFile -Tail 40 | ForEach-Object {
            $_ -replace 'environment=[A-Za-z0-9_-]+', 'environment=<redacted>'
        }
    }
    'help' { Show-Usage }
    '-h' { Show-Usage }
    '--help' { Show-Usage }
    default {
        Write-ErrLine ("rc-native: unknown command '{0}' (start|status|url|stop|logs)." -f $Command)
        exit 1
    }
}
