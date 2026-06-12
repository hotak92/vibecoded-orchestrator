# Run launcher Rust tests with keyring-daemon-safe defaults (Windows).
#
# Parity with scripts/test-keychain-safe.sh. See that script for the
# threat-model rationale. Single-threaded test execution sidesteps the
# Windows Credential Manager's per-process throttling under burst load.
#
# Usage:
#   .\scripts\test-keychain-safe.ps1                # all workspace tests
#   .\scripts\test-keychain-safe.ps1 secrets_cmd::  # filter by name
#
# CI: invoked by .github/workflows/ci.yml on windows-latest runners.
#
# v0.2.54 Track E (P0-1): switched from `cargo test --lib` to
# `cargo test --workspace --tests` so all four workspace members are
# covered, not just `vct-launcher-temp`. See the .sh sibling for details.

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = Split-Path -Parent $ScriptDir
$Manifest = Join-Path $RepoRoot 'launcher/src-tauri/Cargo.toml'

if (-not (Test-Path $Manifest)) {
    Write-Error "[test-keychain-safe] FATAL: manifest not found at $Manifest"
    exit 2
}

# Forward args + pin --test-threads=1. The `--` separator routes the
# threads flag to the test binary, not to cargo.
Write-Host "[test-keychain-safe] cargo test --workspace --tests --manifest-path $Manifest -- --test-threads=1 $args"
& cargo test --workspace --tests --manifest-path $Manifest @args -- --test-threads=1
exit $LASTEXITCODE
