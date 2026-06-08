# tools/install-vco-dev-pre-push-guard.ps1
#
# Installs a pre-push hook that BLOCKS pushes to the public-repo remote
# from a PRIVATE OPERATIONAL VCO checkout (e.g. VCO_dev clones).
#
# Run this once after cloning into a private operational tree.
# It is a no-op if you are in the public-repo clone (the hook will
# never fire because there is no `vco_upstream` / `public` remote
# pointing at the public repo).
#
# Usage:
#   cd <your-private-vco-checkout>
#   pwsh -File tools/install-vco-dev-pre-push-guard.ps1
#
# To bypass (rare — coordinated incident recovery only):
#   git push --no-verify <remote> <ref>

$ErrorActionPreference = "Stop"

# Locate the actual .git directory (handles worktrees)
$gitDir = (& git rev-parse --git-common-dir 2>$null)
if (-not $gitDir) {
    $gitDir = (& git rev-parse --git-dir 2>$null)
}

if (-not $gitDir -or -not (Test-Path $gitDir)) {
    Write-Error "Error: not inside a git repository, or .git directory not found"
    exit 1
}

$hookPath = Join-Path $gitDir "hooks/pre-push"

# Backup any existing pre-push hook
if ((Test-Path $hookPath) -and -not ((Get-Item $hookPath).LinkType)) {
    $timestamp = (Get-Date -AsUTC).ToString("yyyyMMddTHHmmssZ")
    $backup = "$hookPath.bak-$timestamp"
    Copy-Item $hookPath $backup
    Write-Host "Existing pre-push hook backed up to $backup"
}

$hookBody = @'
#!/usr/bin/env bash
# VCO pre-push guard — refuses pushes to public-repo remote.
#
# This hook is layer 2 of 3 defenses:
#   1. Disable the push URL on any public-repo remote (.git/config)
#   2. THIS hook (pattern-blocks vibecoded-orchestrator destinations)
#   3. This file is installed by tools/install-vco-dev-pre-push-guard.ps1
#      (re-installable on fresh clones).
#
# Override (USE WITH CARE): git push --no-verify <remote> <ref>

remote_name="${1:-}"
remote_url="${2:-}"

# Block by name
case "$remote_name" in
  vco_upstream|public|upstream)
    echo "[BLOCK] pre-push: refusing to push to remote '$remote_name' from a VCO operational checkout."
    echo "   Public-repo work goes in a SEPARATE clone of the public repo."
    echo "   This checkout's role is private operational state, not public-source work."
    echo ""
    echo "   If you ABSOLUTELY need to bypass: git push --no-verify $remote_name <ref>"
    exit 1
    ;;
esac

# Block by URL pattern (catches re-added or renamed remotes)
case "$remote_url" in
  *vibecoded-orchestrator*|*vibecoded-tools*)
    echo "[BLOCK] pre-push: refusing to push to '$remote_url' from this VCO operational checkout."
    echo "   URL pattern matches public-repo target."
    exit 1
    ;;
esac

# Default: allow other remotes (origin = private fork, etc.)
exit 0
'@

Set-Content -Path $hookPath -Value $hookBody -NoNewline -Encoding ascii

# Make executable on POSIX (no-op on Windows)
if ($IsLinux -or $IsMacOS) {
    & chmod +x $hookPath
}

Write-Host "OK pre-push hook installed at $hookPath"
Write-Host "OK Verify with: git push --dry-run vco_upstream HEAD (should be blocked if vco_upstream exists)"
