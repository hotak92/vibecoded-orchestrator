# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test: every .ps1 with non-ASCII bytes MUST start with a UTF-8 BOM.

Background
----------
Windows PowerShell 5.1 (bundled with Windows 10/11) defaults to reading
.ps1 files as Windows-1252 when no BOM is present. UTF-8 multi-byte
sequences (em-dash 0xE2 0x80 0x94, smart quotes, etc.) get mis-decoded
mid-file, corrupting the parser state and producing cryptic errors like

    Argomento mancante nell'elenco di parametri.

at a line FAR from the actual non-ASCII content. This blocks
first-install.bat for every Windows user who does not have pwsh 7+
installed (which is most of them).

PowerShell 7+ defaults to UTF-8 without needing a BOM, so the BOM is a
no-op there — but it's the single change that fixes PS 5.1 without
breaking anything else.

This test is the gate that prevents the bug from sneaking back in.
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UTF8_BOM = b"\xef\xbb\xbf"


def _ps1_files() -> list[pathlib.Path]:
    return sorted(REPO_ROOT.rglob("*.ps1"))


def test_ps1_files_with_non_ascii_have_utf8_bom() -> None:
    """Every .ps1 containing non-ASCII bytes must start with a UTF-8 BOM.

    Files that are pure ASCII are exempt — PS 5.1 parses them identically
    in either codepage, so the BOM is optional.
    """
    offenders: list[str] = []
    for path in _ps1_files():
        data = path.read_bytes()
        has_bom = data.startswith(UTF8_BOM)
        has_non_ascii = any(b > 127 for b in data)
        if has_non_ascii and not has_bom:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, (
        "The following .ps1 files contain non-ASCII bytes but lack a UTF-8 BOM. "
        "On Windows PowerShell 5.1 (the default on Win10/11) they will be mis-decoded "
        "as Windows-1252 and produce parser errors at runtime. Fix by re-saving each "
        "file as 'UTF-8 with BOM' (or run scripts/fix-ps1-bom.ps1):\n  - "
        + "\n  - ".join(offenders)
    )
