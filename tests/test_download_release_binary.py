# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 DEDUP-2: tests for _download_release_binary helper.

Verifies the consolidated GitHub-release binary downloader replaces
the 2 near-twin (_try_download_launcher_binary +
_try_download_vct_hub_binary) callsites, and that both wrappers route
through the same code path.

Per audit install-py-dedup-2026-06-10.md #7 — the two helpers were 95%
identical pre-v0.2.53; consolidation closes the drift risk before
v0.2.55 needs a third binary (vct-updater?).
"""

from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"


@pytest.fixture(scope="module")
def install_module():
    spec = importlib.util.spec_from_file_location("install_under_test_d2", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_under_test_d2"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_helper_exists(install_module):
    assert hasattr(install_module, "_download_release_binary")


def test_launcher_wrapper_delegates_to_helper(install_module):
    """_try_download_launcher_binary is a thin wrapper now."""
    src = INSTALL_PY.read_text(encoding="utf-8")
    # Find the function block.
    start = src.find("def _try_download_launcher_binary(")
    assert start > 0
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    # Wrapper should contain a single call to _download_release_binary.
    assert body.count("_download_release_binary(") == 1, (
        f"_try_download_launcher_binary should be a thin wrapper; "
        f"got body of {len(body.splitlines())} lines, "
        f"{body.count('_download_release_binary(')} helper calls."
    )
    # Wrapper should NOT have its own raw subprocess.run / zipfile logic.
    assert "subprocess.run(" not in body, (
        "Wrapper should not contain raw subprocess.run; route via helper."
    )
    assert "zipfile.ZipFile" not in body, (
        "Wrapper should not contain zipfile logic; route via helper."
    )


def test_hub_wrapper_delegates_to_helper(install_module):
    """_try_download_vct_hub_binary is a thin wrapper now."""
    src = INSTALL_PY.read_text(encoding="utf-8")
    start = src.find("def _try_download_vct_hub_binary(")
    assert start > 0
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    assert body.count("_download_release_binary(") == 1
    assert "subprocess.run(" not in body
    assert "zipfile.ZipFile" not in body


def test_helper_returns_none_when_version_unresolvable(install_module, tmp_path):
    """If _read_launcher_version returns None, helper returns None."""
    with patch.object(install_module, "_read_launcher_version", return_value=None):
        result = install_module._download_release_binary(
            install_root=tmp_path,
            binary_basename="vct-launcher",
            bin_subdir_fname=("linux-x64", "vct-launcher"),
            tmpdir_prefix="t-",
        )
    assert result is None


def test_helper_returns_none_when_neither_gh_nor_curl_available(
    install_module, tmp_path
):
    """No gh + no curl → None (caller falls to Tier 3 cargo)."""
    with patch.object(install_module, "_read_launcher_version", return_value="0.2.53"), \
         patch("shutil.which", return_value=None):
        result = install_module._download_release_binary(
            install_root=tmp_path,
            binary_basename="vct-launcher",
            bin_subdir_fname=("linux-x64", "vct-launcher"),
            tmpdir_prefix="t-",
        )
    assert result is None


def test_helper_extracts_binary_from_synthetic_zip(install_module, tmp_path):
    """Happy path: gh succeeds, ZIP contains the binary, extraction lands it."""
    # We build a synthetic ZIP at the location curl would land it,
    # patch gh to "succeed", and verify the helper extracts the binary.
    version = "0.2.53"
    subdir = "linux-x64"
    fname = "vct-launcher"
    artifact = f"vibecoded-orchestrator-{version}-linux-x64.zip"
    inner = f"vibecoded-orchestrator-{version}-linux-x64/vct-launcher"

    install_root = tmp_path

    def fake_run(cmd, **kw):
        # gh release download writes the zip to the --dir.
        # Find --dir in argv:
        if "--dir" in cmd:
            dest = Path(cmd[cmd.index("--dir") + 1])
            zip_path = dest / artifact
            with zipfile.ZipFile(zip_path, "w") as z:
                z.writestr(inner, b"\x7fELF...fake binary...")
        # curl path: -o <path> <url>
        elif "-o" in cmd:
            dest = Path(cmd[cmd.index("-o") + 1])
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr(inner, b"\x7fELF...fake binary...")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(install_module, "_read_launcher_version", return_value=version), \
         patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in ("gh", "curl") else None), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("platform.system", return_value="Linux"):
        result = install_module._download_release_binary(
            install_root=install_root,
            binary_basename="vct-launcher",
            bin_subdir_fname=(subdir, fname),
            tmpdir_prefix="vct-launcher-dl-",
        )

    assert result is not None
    assert result.exists()
    assert result.read_bytes().startswith(b"\x7fELF")
    # Posix permissions: 0o755 set.
    mode = result.stat().st_mode & 0o777
    assert mode == 0o755


def test_helper_returns_none_when_zip_lacks_binary(install_module, tmp_path):
    """If the ZIP exists but contains no `vct-launcher` member, returns None."""
    version = "0.2.53"
    artifact = f"vibecoded-orchestrator-{version}-linux-x64.zip"

    def fake_run(cmd, **kw):
        if "--dir" in cmd:
            dest = Path(cmd[cmd.index("--dir") + 1])
            zip_path = dest / artifact
            with zipfile.ZipFile(zip_path, "w") as z:
                # Put SOMETHING in it but not vct-launcher.
                z.writestr("README.md", b"hello")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(install_module, "_read_launcher_version", return_value=version), \
         patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name == "gh" else None), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("platform.system", return_value="Linux"):
        result = install_module._download_release_binary(
            install_root=tmp_path,
            binary_basename="vct-launcher",
            bin_subdir_fname=("linux-x64", "vct-launcher"),
            tmpdir_prefix="vct-launcher-dl-",
        )
    assert result is None


def test_helper_handles_gh_subprocess_failure(install_module, tmp_path):
    """gh returns non-zero → helper returns None."""
    with patch.object(install_module, "_read_launcher_version", return_value="0.2.53"), \
         patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name == "gh" else None), \
         patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="auth refused")):
        result = install_module._download_release_binary(
            install_root=tmp_path,
            binary_basename="vct-launcher",
            bin_subdir_fname=("linux-x64", "vct-launcher"),
            tmpdir_prefix="t-",
        )
    assert result is None


def test_helper_falls_back_from_gh_to_curl(install_module, tmp_path):
    """When gh is absent but curl is, fallback path is taken."""
    version = "0.2.53"
    artifact = f"vibecoded-orchestrator-{version}-linux-x64.zip"
    inner = f"vibecoded-orchestrator-{version}-linux-x64/vct-launcher"

    def fake_run(cmd, **kw):
        # curl path: -o <path> <url>
        if cmd[0].endswith("curl"):
            dest = Path(cmd[cmd.index("-o") + 1])
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr(inner, b"fake")
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def which(name):
        return f"/usr/bin/{name}" if name == "curl" else None

    with patch.object(install_module, "_read_launcher_version", return_value=version), \
         patch("shutil.which", side_effect=which), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("platform.system", return_value="Linux"):
        result = install_module._download_release_binary(
            install_root=tmp_path,
            binary_basename="vct-launcher",
            bin_subdir_fname=("linux-x64", "vct-launcher"),
            tmpdir_prefix="t-",
        )
    assert result is not None
    assert result.exists()
