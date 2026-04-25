#!/usr/bin/env bash
# Static validation of the installer scripts.
# Runs in CI and locally — does NOT require docker / podman / network.
#
# Exits non-zero if any check fails.
#
# Usage:  scripts/check-install.sh
set -uo pipefail

cd "$(dirname "$0")/.."

fail=0
note() { printf '  %s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

section "install.sh — bash syntax"
if bash -n install.sh; then
    note "OK"
else
    note "FAIL"; fail=1
fi

section "install.sh — shellcheck (if installed)"
if command -v shellcheck >/dev/null 2>&1; then
    # SC1091 (sourced files), SC2086 (word splitting on intentional vars) are common false-positives here
    if shellcheck -e SC1091 install.sh; then
        note "OK"
    else
        note "FAIL"; fail=1
    fi
else
    note "shellcheck not installed — skipped (apt install shellcheck)"
fi

section "install.py — py_compile"
if python3 -m py_compile install.py; then
    note "OK"
else
    note "FAIL"; fail=1
fi

section "install.py --help"
if python3 install.py --help >/dev/null 2>&1; then
    note "OK"
else
    note "FAIL"; fail=1
fi

section "install.ps1 — PowerShell parse (if installed)"
if command -v pwsh >/dev/null 2>&1; then
    # Use pwsh to verify the script parses without executing it.
    parse_check='$ErrorActionPreference="Stop"; [void][System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw install.ps1), [ref]$null)'
    if pwsh -NoProfile -Command "$parse_check" >/dev/null 2>&1; then
        note "OK"
    else
        note "FAIL"; fail=1
    fi
else
    note "pwsh not installed — skipped (snap install powershell or apt)"
fi

section "requirements.txt — pip resolver dry-run"
if command -v python3 >/dev/null 2>&1; then
    if python3 -m pip install --dry-run -r requirements.txt >/dev/null 2>&1; then
        note "OK"
    else
        note "FAIL (resolver couldn't satisfy requirements.txt)"; fail=1
    fi
else
    note "python3 not installed — skipped"
fi

section "Hardcoded personal paths"
matches=$(grep -rn '/home/martino\|/Users/martino\|martino\.cesaratto' install.sh install.ps1 install.py BOOTSTRAP.md requirements.txt requirements-dev.txt infrastructure/ 2>/dev/null || true)
if [ -n "$matches" ]; then
    note "FAIL — personal paths found:"
    echo "$matches" | sed 's/^/    /'
    fail=1
else
    note "OK"
fi

section "Stale private-module references"
matches=$(grep -rn 'commercial_workflow\|commercial-MAO\|/home/martino' install.sh install.ps1 install.py BOOTSTRAP.md requirements.txt requirements-dev.txt 2>/dev/null || true)
if [ -n "$matches" ]; then
    note "FAIL — private-module references found:"
    echo "$matches" | sed 's/^/    /'
    fail=1
else
    note "OK"
fi

section "Compose files — image tags pinned"
unpinned=$(grep -nE '^\s*image:\s*\S+:latest' infrastructure/*.yml 2>/dev/null || true)
if [ -n "$unpinned" ]; then
    # ollama:latest is intentional (rolling) — flag but don't fail
    note "WARN — unpinned image tags (review):"
    echo "$unpinned" | sed 's/^/    /'
else
    note "OK"
fi

section "Summary"
if [ "$fail" -eq 0 ]; then
    note "all checks passed"
    exit 0
fi
note "one or more checks failed"
exit 1
