# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test: install.ps1 must include a WebView2 Runtime probe.

Background
----------
The Tauri launcher GUI links dynamically against Microsoft Edge WebView2
Runtime. On Windows 10 1903+ and Windows 11 it's pre-installed via the
Evergreen channel, but older SKUs (and images where Edge updates were
disabled by policy) ship without it. When absent, the launcher .exe
launches but opens a black/blank window with no error — a confusing
silent failure for new users.

This test guards against the WebView2 probe being removed or weakened
during future refactors of install.ps1.

Why static-scan and not runtime
-------------------------------
- install.ps1 only runs on native Windows; CI can't realistically exercise
  the Read-Host / winget path.
- Registry probing requires Windows; mocking PowerShell's `Test-Path`
  per-key behaviour is not worth the harness.
- The contract this test enforces is the SHAPE of install.ps1 — the
  function exists, is gated on Windows, surfaces the URL hint, and uses
  winget's --silent flag. That's enough to prevent regressions; the
  manual test plan (in the function block comment) covers behaviour.
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _install_ps1() -> str:
    return (REPO_ROOT / "install.ps1").read_text(encoding="utf-8-sig")


def test_install_ps1_defines_webview2_probe_function() -> None:
    """Test-WebView2Installed and Install-WebView2Runtime must be defined."""
    content = _install_ps1()
    assert "function Test-WebView2Installed" in content, (
        "install.ps1 is missing function Test-WebView2Installed — "
        "the registry-key probe that detects Edge WebView2 Runtime presence. "
        "See knowledge/concepts (cross-os-hook-portability) for context."
    )
    assert "function Install-WebView2Runtime" in content, (
        "install.ps1 is missing function Install-WebView2Runtime — "
        "the winget-or-URL-hint installer for the runtime."
    )


def test_install_ps1_probes_known_webview2_registry_keys() -> None:
    """The probe must check at least one of the three canonical registry keys.

    The product GUID {F3017226-FE2A-4295-8BDF-00C3A9A7E4C5} is the stable
    identifier Microsoft assigns to Edge WebView2 Runtime in the
    EdgeUpdate\\Clients\\ tree across per-machine and per-user installs.
    """
    content = _install_ps1()
    assert "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in content, (
        "install.ps1's WebView2 probe must check the EdgeUpdate Clients GUID "
        "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}. Without the right GUID the "
        "probe always reports 'not installed' and harasses every user."
    )
    assert "EdgeUpdate\\Clients\\" in content, (
        "install.ps1 must probe the HKLM/HKCU EdgeUpdate\\Clients\\ registry "
        "subtree where the WebView2 Runtime announces itself."
    )


def test_install_ps1_winget_invocation_is_silent() -> None:
    """`winget install` must use --silent and accept-agreements flags.

    Without --accept-source-agreements + --accept-package-agreements, winget
    interactively prompts for ToS acceptance which hangs forever in a
    .bat-spawned PowerShell window (same root cause as the silent-prompt bug
    Fabio fixed in fix/installer-windows-args-propagation).
    """
    content = _install_ps1()
    assert "winget install Microsoft.EdgeWebView2Runtime" in content, (
        "install.ps1 must invoke 'winget install "
        "Microsoft.EdgeWebView2Runtime' as the auto-install path."
    )
    assert "--silent" in content, "winget invocation must use --silent."
    assert "--accept-package-agreements" in content, (
        "winget invocation must use --accept-package-agreements; otherwise "
        "the install hangs on the EULA prompt in a non-interactive shell."
    )
    assert "--accept-source-agreements" in content, (
        "winget invocation must use --accept-source-agreements; otherwise "
        "the install hangs on the source-license prompt."
    )


def test_install_ps1_surfaces_webview2_url_fallback() -> None:
    """If winget is absent or install fails, install.ps1 must print the URL.

    The hint must be human-readable (no markdown) and contain the canonical
    Microsoft developer URL — that's the only no-account-required path for
    users on Windows 10 SKUs without winget.
    """
    content = _install_ps1()
    assert "WebView2 Runtime is REQUIRED" in content, (
        "install.ps1 must explicitly tell the user WebView2 is REQUIRED — "
        "an opaque 'recommended' phrasing produces broken installs that "
        "look like a launcher bug."
    )
    assert "https://developer.microsoft.com/microsoft-edge/webview2/" in content, (
        "install.ps1 must surface the Microsoft developer URL as the "
        "manual-install fallback."
    )


def test_install_ps1_webview2_probe_is_windows_gated() -> None:
    """The probe block must run only on Windows.

    install.ps1 is technically only invoked on native Windows (install.sh
    covers POSIX), but the gate makes intent explicit and protects against
    the file being sourced from a cross-platform context (e.g. pwsh on
    Linux for syntax linting).
    """
    content = _install_ps1()
    assert "$IsWindows" in content or "$env:OS -eq 'Windows_NT'" in content, (
        "install.ps1's WebView2 probe block must be Windows-gated via "
        "$IsWindows or $env:OS -eq 'Windows_NT'."
    )


def test_install_ps1_webview2_respects_noninteractive_mode() -> None:
    """The probe must respect $nonInteractiveMode (CI / -Quiet / -NonInteractive).

    In CI or with -NonInteractive, install.ps1 must NOT halt waiting for
    user input. It should either silently install via winget OR continue
    the install with a warning (the launcher GUI won't work until the user
    installs WebView2 manually, but the CLI/MCP layer still functions).
    """
    content = _install_ps1()
    # The non-interactive gate variable exists at the top of install.ps1
    # already; the WebView2 block must reference it to skip Read-Host
    # prompts in CI/Quiet mode.
    assert "$nonInteractiveMode" in content, (
        "install.ps1 must compute $nonInteractiveMode (already does at "
        "line ~95). This test will be a tautology under the current shape; "
        "kept here as a forward guard."
    )
    # Find the WebView2 block (between the function defs and the Python
    # detection section) and verify it references $nonInteractiveMode.
    webview_section_start = content.find("function Test-WebView2Installed")
    python_section_start = content.find("# Python detection")
    assert webview_section_start != -1
    assert python_section_start != -1
    webview_section = content[webview_section_start:python_section_start]
    assert "$nonInteractiveMode" in webview_section, (
        "install.ps1's WebView2 block must reference $nonInteractiveMode "
        "so CI / -Quiet / -NonInteractive runs don't hang on Read-Host."
    )
