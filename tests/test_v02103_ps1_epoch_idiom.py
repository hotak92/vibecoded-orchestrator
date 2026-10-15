# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""No shipped .ps1 computes unix seconds with ``Get-Date -UFormat %s``.

Measured on the v0.2.103 Windows notebook (it-IT, UTC+2):

    PS 7.6  : -UFormat %s = 1792071527   true UTC = 1792071526
    PS 5.1  : -UFormat %s = 1792078728,00394   true UTC = 1792071528

Windows PowerShell 5.1 — the only PowerShell on a default Windows install,
and the hooks' fallback when pwsh 7 is absent — returns LOCAL-time seconds
(here +7200 s) with a culture decimal comma. Four hook sites stamped files
with it that the .sh sibling (``date +%s``) and Python (``time.time()``)
read as true UTC: the code-graph drain's rate limit and extras-check
interval, the dropped-writes ledger and the RL force-flush sentinel.
The fixed idiom is ``[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()`` (see
tests/test_v0292_ps1_unix_epoch_parity.py for the stamps it already guards).

A banned-idiom check is the right tool here: on a Linux CI host pwsh 7
returns UTC for ``-UFormat %s``, so a behavioural run cannot see the bug.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_IDIOM = re.compile(r"Get-Date\s+-UFormat\s+['\"]?%s['\"]?\s*\)")


def test_no_shipped_ps1_uses_the_local_time_epoch_idiom():
    offenders = []
    for root in (REPO / "templates", REPO / "scripts"):
        for path in root.rglob("*.ps1"):
            for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                code = line.split("#", 1)[0]
                if _IDIOM.search(code):
                    offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, (
        "Get-Date -UFormat %s is local time on Windows PowerShell 5.1; use "
        "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds():\n" + "\n".join(offenders))
