# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for M-P0-3 (v0.2.53).

`scripts/post-install-launcher.sh:583-589` filtered prebuilt assets by
extension `.dmg` (macOS) / `.appimage` (Linux). `release.yml` only
publishes `.zip` for both, so the filter always missed the actual
asset and the script fell through to the build path. This is the third
silent-failure layer on top of M-P0-1 + M-P0-2.

This test:
1. Extracts the embedded Python `pick()` block from
   `scripts/post-install-launcher.sh`.
2. Runs it standalone against a synthetic GitHub-Releases JSON that
   ships only `.zip` assets (matching the real release.yml output).
3. Asserts the picker returns the right asset for macOS-arm64 and
   Linux-x64.
4. Also exercises a legacy-DMG/AppImage release payload to confirm
   the fallback still works for legacy assets.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "post-install-launcher.sh"


def _extract_pick_block(src: str) -> str:
    """Pull out the inline `python3 - "$OS" <<'PY' ... PY` block."""
    m = re.search(
        r"python3 - \"\$OS\" <<'PY'\n(.*?)\nPY\n",
        src,
        re.DOTALL,
    )
    assert m, "Could not locate embedded Python pick block"
    return m.group(1)


def _run_pick(pick_src: str, os_name: str, arch: str, assets: list[dict]) -> dict:
    """Run the embedded picker with os_name + arch + synthetic release JSON."""
    # We need to inject `arch` overriding the platform.machine() call.
    # Wrap the original code so platform.machine() returns our arch.
    wrapper = textwrap.dedent(f"""
        import sys, json, platform
        _ORIG_MACHINE = platform.machine
        platform.machine = lambda: {arch!r}
        sys.argv = [sys.argv[0], {os_name!r}]
        _release = {{"assets": {json.dumps(assets)}}}
        # Replace stdin with the release JSON.
        import io
        sys.stdin = io.StringIO(json.dumps(_release))
        # Now run the original block.
    """)
    full = wrapper + "\n" + pick_src
    out = subprocess.run(
        [sys.executable, "-c", full],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0, (
        f"pick block exit {out.returncode}; stderr={out.stderr!r}"
    )
    lines = out.stdout.splitlines()
    if not lines:
        return {"url": "", "name": ""}
    url = lines[0] if len(lines) >= 1 else ""
    name = lines[1] if len(lines) >= 2 else ""
    return {"url": url, "name": name}


def _zip_only_release() -> list[dict]:
    """Mimic the exact release.yml output as of v0.2.53."""
    return [
        {
            "name": "vibecoded-orchestrator-v0.2.53-linux-x64.zip",
            "browser_download_url": "https://example.com/linux-x64.zip",
        },
        {
            "name": "vibecoded-orchestrator-v0.2.53-linux-x64.zip.sha256",
            "browser_download_url": "https://example.com/linux-x64.zip.sha256",
        },
        {
            "name": "vibecoded-orchestrator-v0.2.53-macos-arm64.zip",
            "browser_download_url": "https://example.com/macos-arm64.zip",
        },
        {
            "name": "vibecoded-orchestrator-v0.2.53-macos-arm64.zip.sha256",
            "browser_download_url": "https://example.com/macos-arm64.zip.sha256",
        },
        {
            "name": "vibecoded-orchestrator-v0.2.53-windows-x64.zip",
            "browser_download_url": "https://example.com/windows-x64.zip",
        },
    ]


def _legacy_release() -> list[dict]:
    """Hypothetical legacy payload (CI before the .zip migration)."""
    return [
        {
            "name": "vct-launcher-0.2.0-linux-x86_64.AppImage",
            "browser_download_url": "https://example.com/launcher.appimage",
        },
        {
            "name": "vct-launcher-0.2.0-macos-arm64.dmg",
            "browser_download_url": "https://example.com/launcher-arm64.dmg",
        },
        {
            "name": "vct-launcher-0.2.0-macos-x64.dmg",
            "browser_download_url": "https://example.com/launcher-x64.dmg",
        },
    ]


def test_pick_block_extractable():
    pick_src = _extract_pick_block(SCRIPT.read_text())
    assert "def pick(" in pick_src
    assert "endswith(\".zip\")" in pick_src, (
        "M-P0-3: pick block must accept .zip assets (release.yml output)"
    )


def test_zip_only_macos_arm64():
    pick_src = _extract_pick_block(SCRIPT.read_text())
    r = _run_pick(pick_src, "macos", "arm64", _zip_only_release())
    assert r["name"] == "vibecoded-orchestrator-v0.2.53-macos-arm64.zip", (
        f"macOS arm64 should pick the macos-arm64.zip asset, got: {r}"
    )
    assert r["url"] == "https://example.com/macos-arm64.zip"


def test_zip_only_linux_x86_64():
    pick_src = _extract_pick_block(SCRIPT.read_text())
    r = _run_pick(pick_src, "linux", "x86_64", _zip_only_release())
    assert r["name"] == "vibecoded-orchestrator-v0.2.53-linux-x64.zip", (
        f"Linux x86_64 should pick the linux-x64.zip asset, got: {r}"
    )
    assert r["url"] == "https://example.com/linux-x64.zip"


def test_legacy_dmg_macos_arm64_fallback():
    """Legacy .dmg path still picked when no .zip available."""
    pick_src = _extract_pick_block(SCRIPT.read_text())
    r = _run_pick(pick_src, "macos", "arm64", _legacy_release())
    assert r["name"] == "vct-launcher-0.2.0-macos-arm64.dmg", (
        f"macOS arm64 fallback should pick legacy .dmg, got: {r}"
    )


def test_legacy_appimage_linux_fallback():
    """Legacy .appimage path still picked when no .zip available."""
    pick_src = _extract_pick_block(SCRIPT.read_text())
    r = _run_pick(pick_src, "linux", "x86_64", _legacy_release())
    assert r["name"].lower().endswith(".appimage"), (
        f"Linux fallback should pick .appimage, got: {r}"
    )


def test_extraction_block_handles_zip():
    """Verify the bash extraction logic dispatches .zip (M-P0-3 follow-on)."""
    src = SCRIPT.read_text()
    assert 'asset_kind="zip"' in src, (
        "post-install-launcher.sh extraction block must classify .zip "
        "assets (M-P0-3)"
    )
    assert "unzip -q" in src, (
        "post-install-launcher.sh must call `unzip` to extract .zip assets"
    )
    # Legacy paths must remain for backward-compat.
    assert 'asset_kind="appimage"' in src, "Legacy .appimage path removed"
    assert 'asset_kind="dmg"' in src, "Legacy .dmg path removed"
