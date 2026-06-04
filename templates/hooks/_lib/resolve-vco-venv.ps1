# _lib/resolve-vco-venv.ps1
# Windows PowerShell mirror of resolve-vco-venv.sh.
#
# v0.2.46 post-adversarial follow-up — eliminates venv-resolver drift
# across 9 hooks (5 .sh + 4 .ps1). See the .sh sibling for the full
# rationale; the precedence rules are identical.
#
# Usage from a .ps1 hook:
#
#     $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
#     . "$ScriptDir/_lib/resolve-vco-venv.ps1"
#     $VcoVenvPython = Resolve-VcoVenvPython -ScriptDir $ScriptDir
#     if (-not $VcoVenvPython) {
#         Write-Error "[hook-name] VCO venv not resolvable; skipping"
#         exit 0
#     }
#     & $VcoVenvPython my-script.py ...
#
# This file is dot-sourced, never executed standalone.

function Test-VcoOrchestratorClone {
    param([string]$Candidate)
    if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Candidate "install.py") -PathType Leaf)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Candidate "first-install.sh") -PathType Leaf)) {
        return $false
    }
    return $true
}

function Find-VenvPython {
    param([string]$VenvDir)
    if (-not (Test-Path -LiteralPath $VenvDir -PathType Container)) {
        return $null
    }
    $candidates = @(
        (Join-Path $VenvDir "Scripts/python.exe"),
        (Join-Path $VenvDir "bin/python"),
        (Join-Path $VenvDir "bin/python3")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c -PathType Leaf) {
            return $c
        }
    }
    return $null
}

function Resolve-VcoVenvPython {
    param([string]$ScriptDir)

    # Tier 1: $VCT_VENV explicit override.
    $vctVenv = $env:VCT_VENV
    if ($vctVenv) {
        $candidates = @(
            (Join-Path $vctVenv "Scripts/python.exe"),
            (Join-Path $vctVenv "bin/python"),
            (Join-Path $vctVenv "bin/python3"),
            $vctVenv  # in case user pointed at the interpreter directly
        )
        foreach ($c in $candidates) {
            if (Test-Path -LiteralPath $c -PathType Leaf) {
                return $c
            }
        }
    }

    # Tier 2 + 3: $VCT_INSTALL_ROOT (canonical, launcher-provided).
    $vctInstallRoot = $env:VCT_INSTALL_ROOT
    if ($vctInstallRoot) {
        $p = Find-VenvPython (Join-Path $vctInstallRoot ".venv")
        if ($p) { return $p }
        $p = Find-VenvPython (Join-Path (Join-Path $vctInstallRoot "claude_mcp_servers") ".venv")
        if ($p) { return $p }
    }

    # Tier 4 + 5: clone-relative, gated by VCO-clone discriminator.
    # Only try when ScriptDir was provided AND the 2-up path has
    # install.py + first-install.sh (= it's a real VCO clone, not the
    # user's project that just happens to have a .venv).
    if ($ScriptDir) {
        $cloneRoot = Resolve-Path -LiteralPath (Join-Path $ScriptDir "../..") -ErrorAction SilentlyContinue
        if ($cloneRoot -and (Test-VcoOrchestratorClone -Candidate $cloneRoot.Path)) {
            $p = Find-VenvPython (Join-Path $cloneRoot.Path ".venv")
            if ($p) { return $p }
            $p = Find-VenvPython (Join-Path (Join-Path $cloneRoot.Path "claude_mcp_servers") ".venv")
            if ($p) { return $p }
        }
    }

    # All tiers failed. Caller MUST check return value for null before
    # using. NEVER fall back to $env:PROJECT_ROOT/.venv (user's venv).
    return $null
}
