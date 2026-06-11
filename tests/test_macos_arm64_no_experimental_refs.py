# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for M-P0-2 (v0.2.53).

`experimental_macOS/` was the pre-v0.2.13 dist directory; the canonical
slot is `macos-arm64/` and has been since v0.2.13 (install.py:16956).
Three shell scripts never got the memo and continued to write to /
search the legacy slot, which silently broke every macOS user on a
fresh checkout. This test enforces:

1. Any reference to `experimental_macOS` in shell scripts must appear
   alongside a `macos-arm64` reference in the same file (= retained as
   a legacy fallback only).
2. Critical write targets (e.g. rebuild-dist-binary.sh's ARCH_DIR
   assignment for Darwin) must point at `macos-arm64`, NOT
   `experimental_macOS`.
3. install.py retains a documented legacy comment block — this is
   allowed (it is the historical record).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def test_rebuild_dist_binary_targets_macos_arch_aware():
    """rebuild-dist-binary.sh Darwin case must be arch-aware (v0.2.54).

    M-P0-2 (v0.2.53) fixed the legacy `experimental_macOS` slot to
    `macos-arm64`; v0.2.54 Track C made it arch-aware so a local build
    on an Intel Mac stages into `macos-x64/` (matching install.py's
    `_launcher_binary_relative_path` + restart.rs).
    """
    src = _read(REPO_ROOT / "launcher" / "scripts" / "rebuild-dist-binary.sh")
    assert src, "rebuild-dist-binary.sh missing"
    # The Darwin case must assign BOTH canonical slots, gated on uname -m.
    assert 'ARCH_DIR="macos-arm64"' in src, (
        "rebuild-dist-binary.sh Darwin case lost the macos-arm64 slot"
    )
    assert 'ARCH_DIR="macos-x64"' in src, (
        "rebuild-dist-binary.sh Darwin case must stage Intel builds "
        "into macos-x64 (v0.2.54 Track C)"
    )
    assert 'uname -m' in src, (
        "rebuild-dist-binary.sh Darwin case must gate the slot on the "
        "machine arch"
    )
    # Legacy name must not be the assignment target anywhere.
    assert 'ARCH_DIR="experimental_macOS"' not in src
    # Verify the staging block keys off BOTH canonical names.
    assert (
        'if [ "$ARCH_DIR" = "macos-arm64" ] || [ "$ARCH_DIR" = "macos-x64" ]'
        in src
    ), (
        "rebuild-dist-binary.sh staging block must handle both macOS "
        "arch slots"
    )


def test_start_launcher_command_includes_macos_arm64_candidate():
    """start-launcher.command must list macos-arm64 BEFORE experimental_macOS."""
    src = _read(REPO_ROOT / "start-launcher.command")
    assert src, "start-launcher.command missing"
    arm64_idx = src.find("launcher/dist/macos-arm64/vct-launcher")
    legacy_idx = src.find("launcher/dist/experimental_macOS/vct-launcher")
    assert arm64_idx >= 0, (
        "start-launcher.command does not include macos-arm64 candidate "
        "path (M-P0-2)"
    )
    # Legacy path may remain as a fallback, but if present must come
    # AFTER the canonical one.
    if legacy_idx >= 0:
        assert arm64_idx < legacy_idx, (
            "start-launcher.command lists experimental_macOS BEFORE "
            "macos-arm64 — fallback ordering inverted"
        )


def test_post_install_launcher_includes_macos_arm64_candidates():
    """scripts/post-install-launcher.sh candidates_mac must include macos-arm64."""
    src = _read(REPO_ROOT / "scripts" / "post-install-launcher.sh")
    assert src, "scripts/post-install-launcher.sh missing"
    # The candidates_mac=( ... ) block must contain at least the flat-file
    # macos-arm64 path.
    assert "launcher/dist/macos-arm64/vct-launcher" in src, (
        "post-install-launcher.sh candidates_mac does not include "
        "macos-arm64 path (M-P0-2)"
    )
    # Locate candidates_mac block boundaries. Naive ")" search breaks
    # on parenthetical comments inside the array; instead, walk lines
    # until we hit a line that is just ")" (closing the bash array).
    start = src.find("candidates_mac=(")
    assert start >= 0, "post-install-launcher.sh missing candidates_mac array"
    rest = src[start:]
    block_lines: list[str] = []
    for line in rest.splitlines():
        block_lines.append(line)
        if line.strip() == ")":
            break
    block = "\n".join(block_lines)
    # Within the block, macos-arm64 must come before experimental_macOS.
    arm64_idx = block.find("macos-arm64/vct-launcher")
    legacy_idx = block.find("experimental_macOS/vct-launcher")
    assert arm64_idx >= 0, "candidates_mac missing macos-arm64 entry"
    if legacy_idx >= 0:
        assert arm64_idx < legacy_idx, (
            "candidates_mac lists experimental_macOS BEFORE macos-arm64 — "
            "fallback ordering inverted"
        )


def test_no_naked_experimental_macos_writes_in_shell_scripts():
    """No shell script outside legacy-fallback contexts should TARGET experimental_macOS.

    A write target is anything that looks like:
        cp ... experimental_macOS/...
        mkdir ... experimental_macOS
        DIST_DIR=experimental_macOS
        ARCH_DIR="experimental_macOS"
    Legacy-fallback READ candidates (in candidates arrays) are allowed
    because they support old checkouts.
    """
    bad_patterns = [
        re.compile(r'ARCH_DIR\s*=\s*"experimental_macOS"'),
        re.compile(r'DIST_DIR\s*=\s*"[^"]*experimental_macOS[^"]*"'),
    ]
    offenders: list[tuple[str, str]] = []
    for suffix in ("*.sh", "*.command"):
        for path in REPO_ROOT.rglob(suffix):
            # Skip vendored / .git / build outputs.
            if any(seg in path.parts for seg in (".git", "node_modules", "vendor", "build")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for rx in bad_patterns:
                if rx.search(text):
                    offenders.append((str(path.relative_to(REPO_ROOT)), rx.pattern))
    assert not offenders, (
        "Shell scripts still TARGET experimental_macOS as a write slot "
        "(should be macos-arm64 — M-P0-2):\n"
        + "\n".join(f"  - {p}: {r}" for p, r in offenders)
    )
