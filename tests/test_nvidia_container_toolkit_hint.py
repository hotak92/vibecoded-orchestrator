# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Test: install.py's _nvidia_container_toolkit_install_hint() returns a
pkgmgr-aware install command for the detected package manager.

Background — v0.2.53 (Track G2 / L-P0-7)
========================================

When `_ensure_nvidia_cdi_spec_for_podman()` detects that
`nvidia-container-toolkit` is missing (no `nvidia-ctk` on PATH), it
previously pointed users at NVIDIA's 4-page install guide and left them
to figure out the right pkgmgr-specific keyring + repo + install
sequence. Common drop-off point on a Linux + NVIDIA + Podman
combination.

The fix adds `_nvidia_container_toolkit_install_hint()` which picks
the right install command per `shutil.which(<pkgmgr>)` result:

  - apt:  curl|gpg keyring + apt-sources file + apt-get install
  - dnf:  curl|tee .repo file + dnf install
  - pacman: AUR hint (package isn't in default Arch repos)
  - zypper: zypper ar + zypper install
  - apk:  Alpine wiki link + apk add (testing repo)

This test patches `shutil.which` to simulate each pkgmgr being on PATH
in isolation and asserts the returned hint matches the canonical
command shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def install_module():
    """Load install.py as a module without executing main().

    install.py is a top-level script (not in a package); we use
    importlib to load it by file path. The `if __name__ == "__main__":`
    guard ensures main() doesn't fire on import.
    """
    spec = importlib.util.spec_from_file_location(
        "install_module_under_test", ROOT / "install.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_module_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _which_factory(present: str | None):
    """Build a `shutil.which` replacement that reports `present` (one
    pkgmgr name) as on-PATH and everything else as missing.
    """

    def fake_which(cmd: str) -> str | None:
        if cmd == present:
            return f"/usr/bin/{cmd}"
        return None

    return fake_which


def test_hint_apt(install_module) -> None:
    """apt branch: include the libnvidia-container repo keyring step
    AND the final `apt-get install nvidia-container-toolkit` command."""
    with patch.object(install_module.shutil, "which", _which_factory("apt-get")):
        hint = install_module._nvidia_container_toolkit_install_hint()
    assert "apt-get install" in hint, hint
    assert "nvidia-container-toolkit" in hint
    # The libnvidia-container keyring URL must be present (otherwise
    # the install command will fail).
    assert "nvidia.github.io/libnvidia-container" in hint
    # Heads-up that this is the apt branch.
    assert "apt (Debian/Ubuntu)" in hint or "# apt" in hint


def test_hint_dnf(install_module) -> None:
    """dnf branch: must reference NVIDIA's .repo file + `dnf install`."""
    with patch.object(install_module.shutil, "which", _which_factory("dnf")):
        hint = install_module._nvidia_container_toolkit_install_hint()
    assert "dnf install" in hint, hint
    assert "nvidia-container-toolkit" in hint
    assert "nvidia.github.io/libnvidia-container" in hint
    assert "# dnf" in hint or "Fedora" in hint or "RHEL" in hint


def test_hint_pacman_aur(install_module) -> None:
    """pacman branch: must point at the AUR (package not in default repos)."""
    with patch.object(install_module.shutil, "which", _which_factory("pacman")):
        hint = install_module._nvidia_container_toolkit_install_hint()
    # Either an AUR helper command (yay/paru) or the AUR URL.
    aur_marker = ("yay" in hint or "paru" in hint or
                  "aur.archlinux.org" in hint or "AUR" in hint)
    assert aur_marker, f"pacman hint must reference AUR; got:\n{hint}"
    assert "nvidia-container-toolkit" in hint


def test_hint_zypper(install_module) -> None:
    """zypper branch: must use `zypper ar` (add repo) + `zypper install`."""
    with patch.object(install_module.shutil, "which", _which_factory("zypper")):
        hint = install_module._nvidia_container_toolkit_install_hint()
    assert "zypper" in hint
    # `zypper ar` adds the libnvidia-container repo; `zypper install`
    # finalises the install. Both should appear.
    assert "ar " in hint or "addrepo" in hint, (
        f"zypper hint must add NVIDIA's libnvidia-container repo; got:\n{hint}"
    )
    assert "zypper install" in hint
    assert "nvidia-container-toolkit" in hint


def test_hint_apk(install_module) -> None:
    """apk (Alpine) branch: must point at the Alpine wiki + `apk add`."""
    with patch.object(install_module.shutil, "which", _which_factory("apk")):
        hint = install_module._nvidia_container_toolkit_install_hint()
    assert "apk add" in hint
    assert "nvidia-container-toolkit" in hint
    # Alpine + NVIDIA is non-trivial (musl-compat); wiki link required
    # so users don't get stuck on the driver side.
    assert "alpinelinux.org" in hint or "Alpine" in hint


def test_hint_empty_on_unknown_pkgmgr(install_module) -> None:
    """No recognised pkgmgr on PATH → return empty string (caller falls
    back to the generic URL-only message). This is the deterministic
    failure mode for unsupported distros — we don't want to print
    misleading apt commands on a Void system that has no `apt-get`.
    """
    with patch.object(install_module.shutil, "which", _which_factory(None)):
        hint = install_module._nvidia_container_toolkit_install_hint()
    assert hint == "", (
        "When no supported pkgmgr is on PATH, the hint must be empty "
        "so the caller falls back to the URL-only message. Got:\n"
        f"{hint!r}"
    )


def test_hint_apt_prefers_apt_get_branch(install_module) -> None:
    """When only `apt-get` is on PATH (not `apt`), still produce the apt
    hint. install.py probes shutil.which("apt-get") in the existing
    ladder, not "apt"."""
    with patch.object(install_module.shutil, "which", _which_factory("apt-get")):
        hint = install_module._nvidia_container_toolkit_install_hint()
    assert "apt-get install" in hint


def test_pkgmgr_priority_apt_over_dnf(install_module) -> None:
    """If multiple pkgmgrs are present (rare but possible on Linuxbrew
    or chroot setups), the hint should pick a single deterministic
    pkgmgr — apt wins over dnf to mirror the rest of install.py's
    ladder ordering (apt → dnf → pacman → zypper → apk)."""
    def fake_which(cmd: str) -> str | None:
        # Both apt-get AND dnf "present" — should pick apt.
        if cmd in ("apt-get", "dnf"):
            return f"/usr/bin/{cmd}"
        return None

    with patch.object(install_module.shutil, "which", fake_which):
        hint = install_module._nvidia_container_toolkit_install_hint()
    # apt branch wins → contains `apt-get install`. dnf branch would
    # contain `dnf install` instead.
    assert "apt-get install" in hint
    assert "dnf install" not in hint, (
        "When apt and dnf are both present, apt must win (matches "
        "install.py's _prompt_install_container_runtime ladder order)."
    )
