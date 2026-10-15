# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for M-P0-3 (v0.2.53).

`scripts/post-install-launcher.sh:583-589` filtered prebuilt assets by
extension `.dmg` (macOS) / `.appimage` (Linux). `release.yml` only
publishes `.zip` for both, so the filter always missed the actual
asset and the script fell through to the build path. This is the third
silent-failure layer on top of M-P0-1 + M-P0-2.

This test drives the picker through the SAME call the shell script makes
(R20 L2: the picker moved from an inline here-doc fed by an env var — which
an over-128 KiB release document turns into E2BIG — into
``python -m vco_lib.launcher_acquire pick-asset --release-json <file>``):

1. The release JSON is written to a file and the CLI is run as a
   subprocess, exactly as post-install-launcher.sh runs it.
2. Asserts the picker returns the right asset for macOS-arm64 and
   Linux-x64, plus the URL of its ``.sha256`` sidecar (R20 M1).
3. Also exercises a legacy-DMG/AppImage release payload to confirm
   the fallback still works for legacy assets.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "post-install-launcher.sh"


def _run_pick(os_name: str, arch: str, assets: list[dict], tmp_path: Path) -> dict:
    """Run the shared picker CLI on a release document held in a FILE."""
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "vco_lib.launcher_acquire", "pick-asset",
         "--os", os_name, "--arch", arch, "--release-json", str(release)],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT),
    )
    if out.returncode == 2:
        return {"url": "", "name": "", "sha256_url": ""}
    assert out.returncode == 0, (
        f"pick-asset exit {out.returncode}; stderr={out.stderr!r}"
    )
    lines = out.stdout.splitlines() + ["", "", ""]
    return {"url": lines[0], "name": lines[1], "sha256_url": lines[2]}


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


def test_script_calls_the_shared_picker_through_a_file():
    """The shell no longer embeds a picker or passes the JSON in an env var."""
    src = SCRIPT.read_text()
    assert "vco_lib.launcher_acquire pick-asset" in src
    assert '--release-json "$release_file"' in src
    assert "VCT_RELEASE_JSON" not in src
    assert "python3 - \"$OS\" <<'PY'" not in src


def test_zip_only_macos_arm64(tmp_path):
    r = _run_pick("macos", "arm64", _zip_only_release(), tmp_path)
    assert r["name"] == "vibecoded-orchestrator-v0.2.53-macos-arm64.zip", (
        f"macOS arm64 should pick the macos-arm64.zip asset, got: {r}"
    )
    assert r["url"] == "https://example.com/macos-arm64.zip"
    assert r["sha256_url"] == "https://example.com/macos-arm64.zip.sha256"


def test_zip_only_linux_x86_64(tmp_path):
    r = _run_pick("linux", "x86_64", _zip_only_release(), tmp_path)
    assert r["name"] == "vibecoded-orchestrator-v0.2.53-linux-x64.zip", (
        f"Linux x86_64 should pick the linux-x64.zip asset, got: {r}"
    )
    assert r["url"] == "https://example.com/linux-x64.zip"
    assert r["sha256_url"] == "https://example.com/linux-x64.zip.sha256"


def test_legacy_dmg_macos_arm64_fallback(tmp_path):
    """Legacy .dmg path still picked when no .zip available."""
    r = _run_pick("macos", "arm64", _legacy_release(), tmp_path)
    assert r["name"] == "vct-launcher-0.2.0-macos-arm64.dmg", (
        f"macOS arm64 fallback should pick legacy .dmg, got: {r}"
    )
    assert r["sha256_url"] == ""  # no sidecar in that release


def test_legacy_appimage_linux_fallback(tmp_path):
    """Legacy .appimage path still picked when no .zip available."""
    r = _run_pick("linux", "x86_64", _legacy_release(), tmp_path)
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
