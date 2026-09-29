# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One home for parsing and ordering orchestrator/module version STRINGS.

**The rule (v0.2.100, owner ruling Q7): a version is exactly three numeric
parts** — ``^v?(\\d+)\\.(\\d+)\\.(\\d+)$`` over ASCII digits, nothing before
the optional ``v``, nothing after the patch number. No pre-release suffix,
no fourth number, no surrounding whitespace. Anything else raises
:class:`VersionParseError`; it is never silently ranked.

Sibling homes, one per language, answering from the SAME case table
(``tests/fixtures/version_order_cases.json``):

* Rust — ``launcher/src-tauri/vct-launcher-core/src/version.rs``
* TypeScript — ``launcher/src/lib/version-compare.ts``

**Superseded (recorded, not forgotten).** Until v0.2.99 this module offered
``version_parts``: each dotted part's LEADING digit run, so ``0.2.95rc1``
compared EQUAL to ``0.2.95`` and a part with no digit read as ``0``. Its
docstring called the ignored suffix a "stated limitation, deliberately not
fixed here". The owner's rule replaces that promise: no producer emits a
suffix (``scripts/bump-version.sh`` refuses anything but ``X.Y.Z``,
``vco_lib.vco_version`` rejects suffixes at read time), so a suffixed or
otherwise odd string reaching a comparison is a DEFECT to report, not a
value to approximate. ``version_parts`` is deleted; every caller maps
:class:`VersionParseError` to its own "unknown" / refusal, carrying the
offending string. Ranking an unreadable version — as newer, older or equal —
is how a comparator answers "up to date" about something it never read.

Two neighbouring parsers are deliberately NOT folded in here, because they
answer different questions:

* :func:`vco_lib.codegraph_extractor_generation.parse_semver` — the
  ``None``-returning form used by boundary checks whose contract is "a
  version we cannot parse is never proof of a crossing". Its tuple ordering
  agrees with this module on every ``order`` row of the case table (pinned
  by ``tests/test_v02100_version_order.py``).
* ``install.py::_bootstrap_parse_version_tuple`` — extracts the first
  dotted-numeric run from ARBITRARY TEXT (``"Python 3.13.2"`` ->
  ``[3, 13, 2]``). That is version EXTRACTION from tool output, not version
  comparison.
"""

from __future__ import annotations

import re

__all__ = [
    "VersionParseError",
    "parse_version",
    "version_cmp",
    "version_lt",
    "version_ge",
]

# ``re.ASCII`` matters: without it ``\d`` also matches e.g. Arabic-Indic
# digits, which ``int()`` then happily converts — a parse the Rust and TS
# homes would reject. ``fullmatch`` matters too: ``$`` alone accepts a
# trailing newline.
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)", re.ASCII)


class VersionParseError(ValueError):
    """``text`` is not a strict ``X.Y.Z`` (optionally ``v``-prefixed) version.

    ``text`` is the offending value exactly as received, so a caller can put
    it in the "unknown" / refusal message it shows the user.
    """

    def __init__(self, text: object) -> None:
        self.text = text
        super().__init__(
            f"version {text!r} is not X.Y.Z (three numeric parts, no suffix)"
        )


def parse_version(text: object) -> tuple[int, int, int]:
    """``"0.2.100"`` / ``"v0.2.100"`` -> ``(0, 2, 100)``; anything else raises.

    Non-``str`` input raises too — a ``None`` read from a missing JSON field
    is an unknown version, not ``0.0.0``.
    """
    if not isinstance(text, str):
        raise VersionParseError(text)
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        raise VersionParseError(text)
    major, minor, patch = (int(g) for g in match.groups())
    return (major, minor, patch)


def version_cmp(a: object, b: object) -> int:
    """``-1`` / ``0`` / ``1`` for ``a < b`` / ``a == b`` / ``a > b``.

    Raises :class:`VersionParseError` when EITHER side is unparseable.
    """
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def version_lt(a: object, b: object) -> bool:
    """``a < b``; raises :class:`VersionParseError` on an unparseable side."""
    return version_cmp(a, b) < 0


def version_ge(a: object, b: object) -> bool:
    """``a >= b``; raises :class:`VersionParseError` on an unparseable side."""
    return version_cmp(a, b) >= 0
