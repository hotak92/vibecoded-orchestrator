# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/run-python-program.ps1 - v0.2.106: the ONE home for running an
# embedded Python program body from a .ps1 hook or script.
#
# WHY THIS EXISTS (Windows field report, v0.2.105): Claude Code runs .ps1
# hooks with Windows PowerShell 5.1 (`powershell -NoProfile -File`). 5.1's
# native-command argument passing does NOT escape embedded double quotes,
# so `& $py -c $body` truncates the program at the first `"` it contains —
# every multi-line body with a docstring or double-quoted string reached
# Python as `File "<string>", line 1 ... SyntaxError: unterminated string
# literal` (observed on EVERY tool call in
# `<metrics>/post-tool-use-async.log`). pwsh 7.3+ passes arguments
# correctly by default, which is why the Windows CI hook-parity legs stayed
# green. Running the BODY as a temp FILE removes the command line from the
# program-delivery path entirely: no quoting, no length ceiling, identical
# behaviour on 5.1 and pwsh 7.
#
# Semantics (kept exactly what the migrated `& $py -c $body` call sites had):
#   * -Body       the Python program text (multi-line here-string, usually).
#   * -Arguments  extra argv AFTER the program file (what used to follow -c).
#   * -Stdin      optional text piped to the child's stdin. Bind it (even as
#                 "") exactly when the old call site piped a payload; an
#                 empty string pipes one newline, like piping "" always did.
#   * stdout/stderr flow to the CALLER's streams: capture the call's output
#     to read the child's stdout, `2>$null` it to discard stderr, or leave
#     it bare to pass both through to the hook's own stdout/stderr.
#   * $LASTEXITCODE after the call is the child's exit code (the cleanup
#     Remove-Item is a cmdlet and never touches it).
#   * the temp file is private (GetRandomFileName) and deleted afterwards.
#
# A body that reads sys.stdin itself (the hook JSON payload) is SAFE here:
# the program arrives as a file, so stdin stays free for the payload.
#
# ps1-only by design (PS1_ONLY_LIB in .github/scripts/check_hook_parity.py
# and tests/test_v0292_sibling_parity_blind_spots.py): bash hands heredoc
# bodies to `python -` / `python - <<'PYEOF'` with no command-line quoting
# involved, so there is nothing to mirror on the POSIX side.

. (Join-Path $PSScriptRoot "utf8-nobom.ps1")  # the one BOM-free writer

function Invoke-VcoPythonProgram {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Body,
        [string[]]$Arguments,
        [string]$Stdin
    )
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) (
        "vco-py-" + [System.IO.Path]::GetRandomFileName() + ".py")
    try {
        # BOM-free UTF-8: 5.1's Set-Content would write a BOM and Python
        # would then see U+FEFF ahead of the first statement.
        Set-VcoUtf8Content -Path $tmp -Value $Body -NoNewline
        $argv = @($tmp)
        if ($Arguments) { $argv += @($Arguments) }
        if ($PSBoundParameters.ContainsKey('Stdin')) {
            $Stdin | & $Python @argv
        } else {
            & $Python @argv
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}
