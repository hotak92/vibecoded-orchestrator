# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reading a single integer out of a small state file — the ONE reader (v0.2.94).

A pid file, a port file, a lock file: VCO writes several one-number files and
had grown a private reader for each — seven by v0.2.94:
``model_router.config._read_port_file``, ``deferral_retry._read_pidfile``,
``hub_ensure.hub_pid``, the gateway's ``_recorded_pid``, and three separate
``hub.port`` parsers (``access_resolver._hub_port``,
``project_config.resolve``, ``codegraph_resync``). Every one answers
the same question ("what number is in this file, and what do I say when there
isn't one?") and every one had drifted a little in HOW it read: one parsed the
whole file, two took the first line, one stripped before splitting. Those
differences are invisible until a file has a trailing line, at which point one
reader says 11441 and another says "no evidence".

What is shared, and what deliberately is not:

* **shared** — open, decode, take the FIRST line, trim it, parse an int, and
  turn every failure into the caller's sentinel. Also the bounds check, which
  is the same idea one layer along: a number outside its legal range is not
  evidence either, and a caller that has to remember to re-check it is a
  caller that will forget.
* **not shared** — WHAT the sentinel is. ``None`` reads as "no evidence" for
  three callers; the gateway's pid claim needs to tell "the file says nothing
  usable" (``-1``) apart from "the file is mine now" (``None``), so its
  sentinel is a value, not absence.

**First line, trimmed** is the rule for all four now. It is the reading
``hub_status.rs::probe`` already used and the more useful of the two: a file
with an unexpected trailing line answers with the number somebody wrote rather
than falling through to a default that, on the machine this was written for,
is somebody else's service.

Stdlib only, and it must stay that way: ``hub_ensure`` imports it on the path
the launcher runs during an UPDATE, when the orchestrator venv is precisely
the thing in flux (``tests/test_v0292_hub_ensure.py::test_resolve_needs_only_the_stdlib``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, TypeVar, Union

__all__ = ["parse_int_line", "read_int_line"]

_T = TypeVar("_T")


def parse_int_line(
    text: str,
    *,
    sentinel: _T = None,  # type: ignore[assignment]
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Union[int, _T]:
    """The integer on the first line of ``text``, or ``sentinel``.

    Split from :func:`read_int_line` for the callers that must CLASSIFY the
    read failure themselves. ``vco_lib.project_config`` is the case: its
    ``hub.port`` discovery emits a different warning for "unreadable" than
    for "non-integer content", and that difference is a cross-language
    contract with the ``.sh`` and ``.ps1`` siblings. Collapsing both into one
    sentinel would have silently retired one of the two warnings — so such a
    caller keeps its own ``open``, and shares the PARSE, which is the part
    that was actually duplicated.
    """
    lines = text.splitlines()
    if not lines:
        return sentinel
    try:
        value = int(lines[0].strip())
    except ValueError:
        return sentinel
    if minimum is not None and value < minimum:
        return sentinel
    if maximum is not None and value > maximum:
        return sentinel
    return value


def read_int_line(
    path: Union[Path, str],
    *,
    sentinel: _T = None,  # type: ignore[assignment]
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Union[int, _T]:
    """The integer on the first line of ``path``, or ``sentinel``. Never raises.

    Args:
        path: the file to read. Missing, unreadable, empty, non-UTF-8,
            unparseable — every one of them is ``sentinel``, because these
            files are read on status polls and in start paths where an
            exception would blank an answer that has a perfectly good next
            source.
        sentinel: what "no usable number here" is called. ``None`` for a
            reader whose caller treats absence as "no evidence"; a value
            when the caller must distinguish it from something else.
        minimum / maximum: inclusive bounds. A number outside them is
            ``sentinel`` — out of range and unparseable are the same kind of
            "not evidence", and keeping the check here is what stops a caller
            from parsing correctly and then trusting a port of 0.

    Returns:
        The parsed integer, or ``sentinel``.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return sentinel
    return parse_int_line(
        raw, sentinel=sentinel, minimum=minimum, maximum=maximum,
    )
