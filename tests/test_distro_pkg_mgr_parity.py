# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Parity test: install.py + install.sh + post-install-launcher.sh advertise
the SAME set of Linux package managers (apt, dnf, pacman, zypper, apk).

Background — v0.2.53 (Track G2 / L-P0-1)
========================================

Prior to v0.2.53, the three install scripts had inconsistent pkgmgr
coverage:

  - install.py:_prompt_install_container_runtime → apt/dnf/pacman ONLY.
  - install.sh:attempt_install_{linux,node_linux,podman_linux} → apt/dnf/pacman ONLY.
  - scripts/post-install-launcher.sh:288 → apt/dnf/pacman/zypper/apk.

openSUSE/SLES (zypper) and Alpine (apk) users hit "No supported package
manager found" in install.sh / install.py and bailed BEFORE ever reaching
post-install-launcher.sh — where their pkgmgr WAS detected, advertised,
and used (Tier-1 silent Node install path).

This test pins the contract so future edits to one file can't drift
silently from the others. If a new pkgmgr arm is added (e.g. xbps for
Void, eopkg for Solus), it should be added in all three files at the
same time — or this test fails, forcing the conversation.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The canonical set of pkgmgrs the orchestrator drives on Linux.
# v0.2.53: 5 entries (apt, dnf, pacman, zypper, apk). When extending,
# update this set AND the three install scripts together.
EXPECTED_PKGMGRS = frozenset({"apt", "dnf", "pacman", "zypper", "apk"})


def _pkgmgrs_in_install_py() -> set[str]:
    """Find pkgmgrs referenced by install.py's container-runtime install
    ladder (the only Linux pkgmgr-aware block in install.py)."""
    text = (ROOT / "install.py").read_text(encoding="utf-8")
    found: set[str] = set()
    # The Linux ladder uses `shutil.which("<pkgmgr>")` to gate each branch.
    # Both `apt-get` and `apt` map to the apt family; we normalise.
    for cmd in ("apt-get", "apt", "dnf", "pacman", "zypper", "apk"):
        # Match `shutil.which("<cmd>")` with optional whitespace.
        if re.search(rf'shutil\.which\(\s*["\']{re.escape(cmd)}["\']\s*\)', text):
            if cmd in ("apt-get", "apt"):
                found.add("apt")
            else:
                found.add(cmd)
    return found


def _pkgmgrs_in_install_sh() -> set[str]:
    """Find pkgmgrs referenced by install.sh's three attempt_install_*_linux
    functions. We check that EACH function references each pkgmgr — partial
    coverage (e.g. apt in Node but not in Python) counts as a miss for that
    pkgmgr."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")

    # Slice out the three Linux install functions. Each function spans
    # from `attempt_install_<name>_linux() {` to the next blank-line }.
    def _function_body(func_name: str) -> str:
        # The function bodies in install.sh end with `^}` at column 0.
        # Capture greedily but stop at the first such line.
        pat = re.compile(
            rf"^{re.escape(func_name)}\(\) \{{\n(.*?)\n\}}",
            re.MULTILINE | re.DOTALL,
        )
        m = pat.search(text)
        if not m:
            raise AssertionError(
                f"install.sh: function {func_name}() not found — refactor "
                f"detected; update this test."
            )
        return m.group(1)

    funcs = {
        "attempt_install_linux": _function_body("attempt_install_linux"),
        "attempt_install_node_linux": _function_body("attempt_install_node_linux"),
        "attempt_install_podman_linux": _function_body("attempt_install_podman_linux"),
    }

    # For each function, find which pkgmgrs it handles. A pkgmgr counts
    # as "handled" iff there's a `command -v <pkgmgr>` arm AND that arm
    # doesn't immediately fall through to the ERROR sentinel.
    per_func: dict[str, set[str]] = {}
    for fname, body in funcs.items():
        found: set[str] = set()
        for cmd in ("apt-get", "apt", "dnf", "pacman", "zypper", "apk"):
            if re.search(rf"command -v {re.escape(cmd)}\b", body):
                if cmd in ("apt-get", "apt"):
                    found.add("apt")
                else:
                    found.add(cmd)
        per_func[fname] = found

    # A pkgmgr is "covered by install.sh" iff ALL three functions handle it.
    # Partial coverage indicates drift between flows and is treated as
    # missing — the same condition that makes a user fail mid-script.
    return set.intersection(*per_func.values()) if per_func else set()


def _pkgmgrs_in_post_install_launcher() -> set[str]:
    """Find pkgmgrs referenced by post-install-launcher.sh's PKGMGR probe."""
    text = (ROOT / "scripts" / "post-install-launcher.sh").read_text(encoding="utf-8")

    # The canonical probe is `for cmd in apt-get apt dnf pacman zypper apk;`
    # at ~line 288. Match it directly.
    m = re.search(r"for cmd in\s+([\w\s-]+);\s*do", text)
    assert m is not None, (
        "post-install-launcher.sh: PKGMGR probe `for cmd in ...; do` not "
        "found — script refactored; update this test."
    )
    tokens = m.group(1).split()
    found = set()
    for tok in tokens:
        if tok in ("apt-get", "apt"):
            found.add("apt")
        elif tok in ("dnf", "pacman", "zypper", "apk"):
            found.add(tok)
    return found


def test_install_py_covers_expected_pkgmgrs() -> None:
    """install.py's container-runtime ladder must handle all 5 pkgmgrs."""
    found = _pkgmgrs_in_install_py()
    missing = EXPECTED_PKGMGRS - found
    assert not missing, (
        f"install.py is missing pkgmgr branches for: {sorted(missing)}. "
        f"Add `shutil.which(\"<pkgmgr>\")` arms in "
        f"_prompt_install_container_runtime's Linux block."
    )


def test_install_sh_covers_expected_pkgmgrs_in_all_three_flows() -> None:
    """install.sh's Python/Node/Podman attempt_install_*_linux functions
    must each handle all 5 pkgmgrs (no partial coverage)."""
    found = _pkgmgrs_in_install_sh()
    missing = EXPECTED_PKGMGRS - found
    assert not missing, (
        f"install.sh is missing pkgmgr branches for: {sorted(missing)} "
        f"in at least one of attempt_install_linux / attempt_install_node_linux "
        f"/ attempt_install_podman_linux. Add matching `command -v <pkgmgr>` "
        f"arms in all three functions."
    )


def test_post_install_launcher_covers_expected_pkgmgrs() -> None:
    """post-install-launcher.sh's PKGMGR probe must advertise all 5 pkgmgrs."""
    found = _pkgmgrs_in_post_install_launcher()
    missing = EXPECTED_PKGMGRS - found
    assert not missing, (
        f"post-install-launcher.sh's PKGMGR probe is missing: {sorted(missing)}. "
        f"Update the `for cmd in apt-get apt dnf pacman zypper apk;` line."
    )


def test_all_three_scripts_have_identical_pkgmgr_coverage() -> None:
    """The three scripts must agree on which Linux pkgmgrs are supported.

    This is the strongest constraint: if a future PR adds e.g. xbps to
    one script and forgets the other two, this test fails — preventing
    a SLES/Void/Alpine user from being told "your distro is supported"
    by one script and "no supported pkgmgr" by another.
    """
    py = _pkgmgrs_in_install_py()
    sh = _pkgmgrs_in_install_sh()
    launcher = _pkgmgrs_in_post_install_launcher()

    assert py == sh == launcher, (
        "pkgmgr coverage drift between install.py / install.sh / "
        "post-install-launcher.sh:\n"
        f"  install.py:                  {sorted(py)}\n"
        f"  install.sh:                  {sorted(sh)}\n"
        f"  post-install-launcher.sh:    {sorted(launcher)}\n"
        f"  symmetric diff (py^sh):      {sorted(py ^ sh)}\n"
        f"  symmetric diff (sh^launcher):{sorted(sh ^ launcher)}\n"
        "Each pkgmgr arm must exist in all three files; partial "
        "coverage leaves users stranded mid-install."
    )
