# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Sibling-existence parity for the two populations the CI gate cannot see.

``.github/scripts/check_hook_parity.py`` proves a ``.ps1`` exists for every
``.sh`` — but it skips ``_lib/`` entirely (``is_excluded``) and only pairs
``.sh``/``.ps1`` suffixes, so an extension-less bash wrapper in
``templates/scripts/`` (``kg-sync``, ``kg-dedup``, ``code-graph-*``) with no
``.ps1`` is invisible to it. v0.2.92 added two ``_lib`` pairs and modified
five more, plus two extension-less wrapper pairs, none of which the gate
looked at (DELIVERY-PORTABILITY-AUDIT-v0292-2026-09-05, finding m2).

R42: Windows parity is achieved by WRITING the ``.ps1``, never by narrowing
the feature. This test is a ratchet: the ``KNOWN_MISSING`` set below is the
exact population of pre-existing gaps. Adding a new wrapper without a
sibling fails; writing the sibling for a known gap also fails until the
entry is removed here, so the register can never go stale in either
direction.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOKS_LIB = REPO / "templates" / "hooks" / "_lib"
SCRIPTS = REPO / "templates" / "scripts"

#: Extension-less bash wrappers shipped WITHOUT a PowerShell sibling.
#: v0.2.92 delivery audit m3 landed ``kg-duplicates.ps1`` (the audit's own
#: R42 fix), leaving ``cost-summary`` the only gap — Windows users reach the
#: same code via the documented portable entry point
#: ``python .claude/scripts/cost-summary.py``. Remove an entry when its
#: ``.ps1`` lands.
KNOWN_MISSING: frozenset[str] = frozenset({"cost-summary"})

#: ``.ps1``-only helpers with no POSIX counterpart by design (a Windows
#: mechanism with nothing to mirror).
PS1_ONLY_LIB: frozenset[str] = frozenset({
    "resolve-powershell.ps1",
    # v0.2.92 BLOCKER-1: splits a compose command string into head + args.
    # POSIX has nothing to mirror — `$COMPOSE_CMD up -d` word-splits correctly
    # for both `podman-compose` and `podman compose`, while PowerShell needs an
    # explicit splat whose naive form mis-handles the one-token shape.
    "compose-invocation.ps1",
})


def _is_bash_wrapper(path: Path) -> bool:
    if path.suffix or not path.is_file():
        return False
    try:
        first = path.open("rb").readline(256).decode("utf-8", "replace")
    except OSError:
        return False
    return first.startswith("#!") and "bash" in first


def test_every_lib_sh_has_a_ps1_sibling() -> None:
    missing = sorted(
        p.name for p in HOOKS_LIB.glob("*.sh") if not p.with_suffix(".ps1").is_file()
    )
    assert not missing, (
        f"templates/hooks/_lib/ .sh files with no .ps1 sibling: {missing} — "
        "the CI parity gate excludes _lib/, so this is the only check."
    )


def test_every_lib_ps1_has_an_sh_sibling_or_is_declared_windows_only() -> None:
    stray = sorted(
        p.name
        for p in HOOKS_LIB.glob("*.ps1")
        if not p.with_suffix(".sh").is_file() and p.name not in PS1_ONLY_LIB
    )
    assert not stray, (
        f"templates/hooks/_lib/ .ps1 files with no .sh sibling: {stray} — "
        "add the POSIX sibling, or declare it in PS1_ONLY_LIB with a reason."
    )


def test_extensionless_wrappers_without_ps1_are_exactly_the_known_set() -> None:
    wrappers = {p.name for p in SCRIPTS.iterdir() if _is_bash_wrapper(p)}
    assert wrappers, "found no extension-less bash wrappers — the scan is wrong"
    missing = {w for w in wrappers if not (SCRIPTS / f"{w}.ps1").is_file()}
    new_gaps = sorted(missing - KNOWN_MISSING)
    assert not new_gaps, (
        f"new extension-less wrapper(s) shipped without a .ps1 sibling: "
        f"{new_gaps} (R42: write the .ps1; the CI gate cannot see these)"
    )
    healed = sorted(KNOWN_MISSING - missing)
    assert not healed, (
        f"{healed} now have a .ps1 sibling — remove them from KNOWN_MISSING"
    )
