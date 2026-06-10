# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language Python interpreter candidate-list parity test (v0.2.53 NEW-3).

Three places have a hard-coded list of Python interpreters to try when
detecting the user's Python installation:

  - ``install.sh:48`` (bash, posix install path).
  - ``install.ps1:233`` (PowerShell, native-Windows install path).
  - ``launcher/src-tauri/src/commands/installer.rs:9596`` (Rust,
    launcher-driven re-install / ``detect_system()``).

Before v0.2.53, ``installer.rs`` was missing ``python3.13``. The drift
caused this user-visible failure: on a Linux box where the user had
ONLY ``python3.13`` installed (no ``python3`` alias — see Fedora's
default and several Arch derivatives' default-stripped installs), the
bash installer succeeded but a launcher-driven ``detect_system()``
returned ``has_python=false``. The launcher then fired the "Python not
found" modal even though ``install.py`` was running fine.

This test extracts the literal candidate lists from all three sites and
asserts identical ordering + content for the POSIX/Unix branch. Windows
intentionally diverges (``py`` first, no version-suffix variants — see
the installer.rs comment for Microsoft Store stub rationale).

Per audit ``install-family-crossfile-dedup-2026-06-10.md`` §Pattern #2.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Canonical POSIX candidate list. Order matters — the installer tries
# them in order, and the first that responds to `-c 'import sys'` wins.
EXPECTED_POSIX = ["python3.13", "python3.12", "python3.11", "python3", "python"]


def _extract_install_sh() -> list[str]:
    """Find the ``for cmd in <candidates>; do`` loop in install.sh."""
    src = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    m = re.search(r"for\s+cmd\s+in\s+([^\n;]+?);\s*do", src)
    assert m is not None, "could not find Python candidate loop in install.sh"
    return m.group(1).split()


def _extract_install_ps1() -> list[str]:
    """Find ``$candidates = @(...)`` in install.ps1's Find-Python."""
    src = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    m = re.search(
        r"\$candidates\s*=\s*@\(([^)]+)\)",
        src,
    )
    assert m is not None, "could not find $candidates assignment in install.ps1"
    inner = m.group(1)
    # Tokens are quoted strings: "python3.13", "python3.12", ...
    return re.findall(r'"([^"]+)"', inner)


def _extract_installer_rs() -> list[str]:
    """Find the POSIX-branch ``vec![...]`` in installer.rs's
    ``detect_system`` Python probe."""
    src = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs").read_text(
        encoding="utf-8"
    )
    # The Windows branch comes first (`if cfg!(windows)`); the POSIX
    # branch is the `else` arm. Anchor on the `else { vec![...] };`
    # form to avoid matching the Windows list.
    m = re.search(
        r"if\s+cfg!\(windows\)\s*\{[^}]*?\}\s*else\s*\{\s*vec!\[([^\]]+)\]",
        src,
    )
    assert m is not None, "could not find POSIX `else { vec![...] }` Python list in installer.rs"
    inner = m.group(1)
    return re.findall(r'"([^"]+)"', inner)


def test_install_sh_python_candidates_match_canonical():
    assert _extract_install_sh() == EXPECTED_POSIX


def test_install_ps1_python_candidates_match_canonical():
    assert _extract_install_ps1() == EXPECTED_POSIX


def test_installer_rs_python_candidates_match_canonical():
    actual = _extract_installer_rs()
    assert actual == EXPECTED_POSIX, (
        f"installer.rs POSIX Python candidate list drifted from canonical.\n"
        f"  expected: {EXPECTED_POSIX!r}\n"
        f"  actual:   {actual!r}\n"
        f"Update launcher/src-tauri/src/commands/installer.rs around the "
        f"`if cfg!(windows)` block so the POSIX branch lists "
        f"['python3.13', ...] in lock-step with install.sh and install.ps1."
    )


def test_all_three_sources_agree():
    """End-to-end parity assertion (caught the NEW-3 drift)."""
    sh = _extract_install_sh()
    ps1 = _extract_install_ps1()
    rs = _extract_installer_rs()
    assert sh == ps1 == rs, (
        f"Python candidate list drift detected:\n"
        f"  install.sh:  {sh!r}\n"
        f"  install.ps1: {ps1!r}\n"
        f"  installer.rs (POSIX branch): {rs!r}\n"
        f"All three sources must list the same Python interpreters in the same order."
    )
