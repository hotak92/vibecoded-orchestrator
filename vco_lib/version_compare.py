# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One home for comparing two orchestrator version STRINGS.

Before v0.2.96 this rule had three implementations with two different
answers, and the disagreement was live:

===========================  ==============  ===============
input                        leading-digit   digits-filtered
===========================  ==============  ===============
``0.2.95``                   ``[0, 2, 95]``  ``(0, 2, 95)``
``0.2.95rc1``                ``[0, 2, 95]``  ``(0, 2, 951)``
===========================  ==============  ===============

The filtered variant read ``95rc1`` as **951**, so it ranked a release
candidate ABOVE ``0.2.100`` — in the gateway freshness proof, which exists
to tell a user whether the running code is the installed code. It answered
the opposite of the truth for any version carrying a suffix, and an
editable install's ``importlib.metadata`` version carries one routinely
(``0.2.96.dev0`` and friends).

**The semantics kept here are the leading-digit ones**, because they were
already the behaviour of two of the three homes and are the ones every
existing caller was written against. Unifying is therefore a bug fix for
the third home and a no-op for the other two — which is the property worth
having when a comparison gates update and freshness decisions.

**Stated limitation, deliberately not fixed here**: a prerelease suffix is
IGNORED rather than ordered, so ``0.2.95rc1`` compares EQUAL to
``0.2.95``. Proper prerelease ordering (rc below its release) is a
semantic change affecting every caller and belongs in its own cycle, not
in a consolidation. Ignoring the suffix is still strictly better than
reading it as a larger patch number, which is what the third home did.

Two neighbouring parsers are deliberately NOT folded in here, because they
answer different questions:

* :func:`vco_lib.codegraph_extractor_generation.parse_semver` — STRICT:
  exactly three dotted integers or ``None``. Its callers depend on the
  rejection, which is a contract this module does not offer.
* ``install.py::_bootstrap_parse_version_tuple`` — extracts the first
  dotted-numeric run from ARBITRARY TEXT (``"Python 3.13.2"`` ->
  ``[3, 13, 2]``). That is version EXTRACTION from tool output, not
  version comparison.

Merging either into this module would be collapsing two concerns because
their code looks alike, which is the mistake this consolidation exists to
undo.
"""

from __future__ import annotations

__all__ = ["version_parts", "version_ge"]


def version_parts(version: str) -> list[int]:
    """Dotted parts as ints, taking each part's LEADING digit run.

    ``"0.2.95rc1"`` -> ``[0, 2, 95]``; a part with no leading digit
    contributes ``0`` rather than raising, because this runs on values read
    from a running process, a package's metadata and a file on disk — none
    of which this code controls, and any of which may be absent or odd.
    """
    out: list[int] = []
    for part in str(version).split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return out


def version_ge(a: str, b: str) -> bool:
    """``a >= b`` on the leading numeric components.

    The shorter version is zero-padded, so ``0.2`` and ``0.2.0`` compare
    equal — the release-tag convention here is three components and a
    two-component string is the same release written shorter, not an
    earlier one.
    """
    pa, pb = version_parts(a), version_parts(b)
    width = max(len(pa), len(pb))
    pa += [0] * (width - len(pa))
    pb += [0] * (width - len(pb))
    return pa >= pb
