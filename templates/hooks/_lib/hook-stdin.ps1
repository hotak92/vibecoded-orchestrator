# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/hook-stdin.ps1 -- the ONE way a PowerShell hook reads its payload, and
# the UTF-8 setup for its output and its Python children (v0.2.107).
#
# Claude Code writes the hook payload as UTF-8 (JSON.stringify leaves
# non-ASCII characters unescaped). [Console]::In decodes it in the console's
# code page instead -- the OEM page (850, 437, ...) for a hook Claude Code
# starts on Windows, under Windows PowerShell 5.1 AND PowerShell 7 -- so a
# path such as `citta_<CJK>.py` arrived mangled: Build Anchor, the pre-edit
# backup and the credential scan looked at a file that does not exist and
# let the real one through, and a non-ASCII command was rewritten wrongly.
# Measured on Windows 11 (code page 850) with the installed hooks.
#
# Every hook dot-sources this file near its top -- BEFORE _lib/stderr-cap.ps1,
# so the capped stderr wraps the UTF-8 writer -- and reads the payload with
# Read-VcoHookStdin. tests/test_v02107_ps1_hook_stdin_utf8.py fails on any
# hook that still reads [Console]::In.
#
# * Read-VcoHookStdin: the raw stdin stream decoded as UTF-8, whatever the
#   console says. Raw bytes rather than `[Console]::InputEncoding = ...`: a
#   hook without a console cannot set a console code page.
# * $OutputEncoding (UTF-8, no BOM): what PowerShell encodes text piped into
#   a native command with (`$HookStdin | & $python ...`); 5.1's default is
#   ASCII, which turned every non-ASCII character into `?`.
# * [Console]::OutputEncoding (UTF-8, no BOM): what the hook's stdout and
#   stderr are written in AND what PowerShell decodes a native command's
#   output with. Best effort: a process without a console cannot set it.
# * PYTHONIOENCODING=utf-8: the Python children read the UTF-8 pipe and
#   write UTF-8 back (a child started with -I / -E ignores it).
#
# Owner ruling (v0.2.107): Windows hooks use UTF-8 unconditionally. No
# cmdlets below (plain .NET), so a hook's fast path pays no module autoload.
# ps1-only by design (PS1_ONLY_LIB in .github/scripts/check_hook_parity.py
# and tests/test_v0292_sibling_parity_blind_spots.py): a bash hook reads the
# payload's bytes as they are, so there is nothing to mirror.

$global:OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

function Read-VcoHookStdin {
    $reader = [System.IO.StreamReader]::new([Console]::OpenStandardInput(), [System.Text.UTF8Encoding]::new($false))
    try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
}
