# check-no-fork-bomb.ps1 — Windows sibling of check-no-fork-bomb.sh.
#
# See check-no-fork-bomb.sh for the full incident background and
# threshold rationale. Short version: defense-in-depth detector that
# kills runaway `lean-ctx` processes at SessionStart if the count
# exceeds a sane threshold. Root cause of the 2026-04-30 + 2026-05-15
# Linux fork-bombs (BASH_ENV shim recursion) does not exist on Windows
# in the same form — PowerShell hooks don't source BASH_ENV — but a
# similar recursive-spawning regression in any future Windows-side hook
# wrapper would manifest the same way (lean-ctx process count blowing
# past anything legitimate). This hook is the safety net for either OS.
#
# See knowledge/concepts/lean-ctx-shim-disabled.md for forensics.

if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# Cap any unbounded stderr from this hook (defense against the 2026-05-07
# GUI freeze; see _lib/stderr-cap.ps1).
. "$PSScriptRoot/_lib/stderr-cap.ps1"

# Threshold: > $threshold lean-ctx processes is treated as a fork-bomb
# in progress. Override via $env:LEAN_CTX_FORK_BOMB_THRESHOLD for testing.
$threshold = if ($env:LEAN_CTX_FORK_BOMB_THRESHOLD) {
    [int]$env:LEAN_CTX_FORK_BOMB_THRESHOLD
} else {
    100
}

# Get-Process is the PowerShell-native equivalent of `pgrep -x`. The
# -Name argument matches the executable base name exactly (no wildcards
# unless asked). -ErrorAction SilentlyContinue → empty array if no
# matches (vs. throwing a non-terminating error).
$leanCtxProcs = Get-Process -Name lean-ctx -ErrorAction SilentlyContinue
$count = ($leanCtxProcs | Measure-Object).Count

if ($count -le $threshold) {
    # Normal case: silent exit.
    exit 0
}

# --- Fork-bomb path ---
$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$hookTag = "[check-no-fork-bomb $ts]"

[Console]::Error.WriteLine("$hookTag Fork-bomb detected: $count lean-ctx processes (threshold $threshold).")
[Console]::Error.WriteLine("$hookTag Killing all lean-ctx processes for the current user...")
[Console]::Error.WriteLine("$hookTag See knowledge/concepts/lean-ctx-shim-disabled.md for context.")

# Stop-Process -Force = SIGKILL-equivalent. By default Stop-Process
# operates on processes the current session can signal — i.e. owned by
# the current user — so no extra filtering is needed.
try {
    Stop-Process -Name lean-ctx -Force -ErrorAction SilentlyContinue
} catch {
    # Already gone, race condition, or no permission — non-fatal.
}

# Give the OS a moment to reap the killed processes before recount.
Start-Sleep -Seconds 1

$remainingProcs = Get-Process -Name lean-ctx -ErrorAction SilentlyContinue
$remaining = ($remainingProcs | Measure-Object).Count

if ($remaining -eq 0) {
    [Console]::Error.WriteLine("$hookTag Kill complete. Remaining: 0.")
} else {
    [Console]::Error.WriteLine("$hookTag Kill done. Remaining: $remaining (may still be reaping).")
}

# Best-effort desktop notification via the cross-platform helper at
# .claude/scripts/notify.py (uses BurntToast / balloon on Windows).
# Non-fatal if Python or the helper is missing.
$projectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$notifyHelper = Join-Path $projectDir ".claude/scripts/notify.py"
$notifyTitle = "VCO fork-bomb killed"
$notifyBody = "Killed $count runaway lean-ctx processes (remaining: $remaining)"

if (Test-Path $notifyHelper) {
    # Resolve Python via the existing helper that sets $PY.
    $libDir = Join-Path $PSScriptRoot "_lib"
    $findPy = Join-Path $libDir "find-python.ps1"
    if (Test-Path $findPy) { . $findPy }
    if ($PY) {
        try {
            & $PY $notifyHelper $notifyTitle $notifyBody `
                --urgency critical --icon dialog-error 2>$null | Out-Null
        } catch { }
    }
}

# Exit 0 unconditionally — never block session start.
exit 0
