# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity test for NEW-1 + DEDUP-15 (v0.2.53).

`scripts/lib/asset-ref-count.sh` and `scripts/lib/asset-ref-count.ps1`
implement the same count: occurrences of `_app/immutable/` (broad
SvelteKit marker) inside a binary. They MUST return the same count
for the same input, or the drift between runtime + CI checks will
re-emerge.

This test:
1. Confirms the lib files exist and use the BROAD marker
   `_app/immutable/` (NOT the narrow `_app/immutable/assets`).
2. Builds a synthetic binary with a known number of marker hits and
   confirms the bash helper returns the right count.
3. If `pwsh` is on PATH, confirms the PowerShell helper returns the
   SAME count. If not, skips that step (CI runners cover PowerShell).
4. Greps all known callsites to assert each calls the shared helper
   (or uses the broad substring directly, for sites still inlined).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_LIB = REPO_ROOT / "scripts" / "lib" / "asset-ref-count.sh"
PS_LIB = REPO_ROOT / "scripts" / "lib" / "asset-ref-count.ps1"


def test_lib_files_exist():
    assert SH_LIB.exists(), f"{SH_LIB} missing"
    assert PS_LIB.exists(), f"{PS_LIB} missing"


def test_lib_uses_broad_marker():
    """Both libs MUST use `_app/immutable/` (broad), not `_app/immutable/assets`."""
    sh = SH_LIB.read_text()
    ps = PS_LIB.read_text()
    # Broad marker must appear.
    assert 'ASSET_REF_MARKER="_app/immutable/"' in sh, (
        "bash lib does not declare the broad marker — drift risk"
    )
    assert 'AssetRefMarker = "_app/immutable/"' in ps, (
        "PowerShell lib does not declare the broad marker — drift risk"
    )
    # Narrow marker (the bug we're fixing) must NOT appear.
    assert '"_app/immutable/assets"' not in sh, (
        "bash lib still references narrow marker — NEW-1 regression"
    )
    assert '"_app/immutable/assets"' not in ps, (
        "PowerShell lib still references narrow marker — NEW-1 regression"
    )


def _make_synthetic_binary(tmp_path: Path, marker_hits: int) -> Path:
    """Write a synthetic ELF-ish blob with N copies of the marker.

    We don't need a real ELF — `strings` returns any sequence of
    printable chars terminated by a NUL or non-printable, so a
    binary file with N copies of the marker separated by NULs
    matches what `strings` would extract from a real binary.
    """
    binpath = tmp_path / "fake-launcher"
    payload = b"\x00".join([b"_app/immutable/chunk-fake.js"] * marker_hits)
    binpath.write_bytes(b"\x7fELF\x00" + payload + b"\x00trailer")
    return binpath


def test_bash_helper_counts_correctly(tmp_path: Path):
    """Smoke-test asset_ref_count + asset_ref_count_passes via bash."""
    binary = _make_synthetic_binary(tmp_path, marker_hits=7)
    out = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            f". {SH_LIB} && asset_ref_count {binary}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0, f"bash helper failed: {out.stderr}"
    count = int(out.stdout.strip())
    assert count == 7, f"bash helper returned {count}, expected 7"

    # Predicate: 7 >= 5 should pass.
    out_pass = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            f". {SH_LIB} && asset_ref_count_passes {binary}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out_pass.returncode == 0, "passes predicate failed for hits=7"

    # Predicate: with hits=3, must NOT pass.
    binary2 = _make_synthetic_binary(tmp_path, marker_hits=3)
    out_fail = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            f". {SH_LIB} && asset_ref_count_passes {binary2}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out_fail.returncode != 0, "passes predicate must fail for hits=3"


def test_powershell_helper_counts_correctly(tmp_path: Path):
    """Cross-shell parity: PowerShell must return the same count."""
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("pwsh/powershell not on PATH — covered by Windows CI")

    binary = _make_synthetic_binary(tmp_path, marker_hits=7)
    cmd = (
        f". '{PS_LIB}'; "
        f"Get-AssetRefCount -Path '{binary}'"
    )
    out = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert out.returncode == 0, (
        f"pwsh helper failed: rc={out.returncode}, stderr={out.stderr!r}"
    )
    # Parse last numeric line from stdout.
    numeric_lines = [l.strip() for l in out.stdout.splitlines() if l.strip().lstrip("-").isdigit()]
    assert numeric_lines, f"no numeric output from pwsh: {out.stdout!r}"
    count = int(numeric_lines[-1])
    assert count == 7, f"pwsh helper returned {count}, expected 7"


def test_callsites_use_helper_or_broad_substring():
    """All runtime callsites must source the helper OR use the broad substring."""
    callsites = [
        REPO_ROOT / "start-launcher.sh",
        REPO_ROOT / "start-launcher.command",
        REPO_ROOT / "scripts" / "post-install-launcher.sh",
    ]
    for path in callsites:
        text = path.read_text()
        sources_helper = "scripts/lib/asset-ref-count.sh" in text or \
                        "asset_ref_count_passes" in text or \
                        "asset_ref_count " in text
        # If the file still has an inline strings|grep with the
        # NARROW marker, that's a regression.
        narrow_inline = "'_app/immutable/assets'" in text
        assert not narrow_inline, (
            f"{path.relative_to(REPO_ROOT)} still uses narrow marker "
            "`_app/immutable/assets` inline — NEW-1 regression"
        )
        assert sources_helper, (
            f"{path.relative_to(REPO_ROOT)} does not source "
            "scripts/lib/asset-ref-count.sh (DEDUP-15)"
        )
