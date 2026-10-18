# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reading a request body's HEAD without parsing the whole body.

Moved out of :mod:`model_router.server` (v0.2.104, size budget) unchanged:
the over-buffer path needs the top-level ``model`` / ``stream`` fields of a
body too large to parse, and the exact byte span of the model literal to
splice the routed id in. Pure: bytes in, fields out. ``server`` imports
every name it used before.
"""

from __future__ import annotations

import json
from typing import NamedTuple, Optional

#: How much of an over-buffer body is walked for the routing fields. The
#: top-level ``model`` sits in the flat head of every request a client
#: actually sends, so this is generous; it is bounded at all because the walk
#: is a Python-level scan and the buffer it runs on can be tens of MiB.
HEAD_SCAN_BYTES = 64 * 1024


def _read_json_string(
    buf: bytes, start: int, end: int,
) -> "tuple[Optional[str], int]":
    """The JSON string literal starting at ``buf[start]``, and where it ends.

    ``start`` must be the opening quote. Escapes are honoured while looking
    for the closing one — an escaped quote inside a value must not end it —
    and the literal is decoded by :mod:`json` itself rather than by hand, so
    an escaped id is read exactly as the upstream will read it. Returns
    ``(None, …)`` when the literal is truncated or invalid, which is a normal
    outcome here: the buffer is a PREFIX of the body.
    """
    i = start + 1
    while i < end:
        c = buf[i]
        if c == 0x5C:  # backslash — the next byte is escaped, whatever it is
            i += 2
            continue
        if c == 0x22:  # the closing quote
            try:
                text = json.loads(buf[start:i + 1])
            except (ValueError, UnicodeDecodeError):
                return None, i + 1
            return (text if isinstance(text, str) else None), i + 1
        i += 1
    return None, end


class HeadField(NamedTuple):
    """A top-level field read out of a body the gateway did not parse.

    ``value`` is the decoded text. ``start`` and ``end`` bound the RAW literal
    inside the buffer — quotes included for a string — so a caller can splice
    a replacement into the bytes without re-encoding, or even parsing, the
    body around it. That is the whole reason the span is carried: on the
    over-buffer path there is no parsed body to edit, and the one edit the
    gateway still owes the request is a substitution of exactly this literal
    (see :func:`_splice_literal`).
    """

    value: str
    start: int
    end: int


def _splice_literal(buf: bytes, field: HeadField, replacement: str) -> bytes:
    """``buf`` with ``field``'s literal replaced by ``replacement``, encoded.

    The ONE edit made to a body that outgrew the rewrite buffer, and it is
    made because the thing being replaced is the GATEWAY's own invention: the
    ``claude-gw/`` namespace and Claude Code's ``[1m]`` suffix are client-side
    spellings that no upstream has ever heard of (see
    :mod:`model_router.routing`). Forwarding them verbatim would be forwarding
    a request only the gateway could have broken — the opposite of the reason
    this path exists. Everything else stays untouched: the tool-id repair is a
    correction of a VENDOR's output and skipping it costs a cosmetic id, not
    the request.

    ``json.dumps`` writes the replacement, so an id needing escapes is encoded
    the way the upstream's parser reads it rather than by string surgery.
    """
    return (
        buf[:field.start]
        + json.dumps(replacement).encode("utf-8")
        + buf[field.end:]
    )


def _shallow_top_level_fields(head: bytes) -> "dict[str, HeadField]":
    """The top-level scalar fields of a JSON object, read from its head.

    Used on ONE path: a request body too big to hold, where the gateway still
    has to know which upstream and which credential it is for. It reads only
    what it can prove is at the top level — a ``"model"`` nested inside a
    message, or one inside a string, is at a depth this walk tracks and is
    never mistaken for the request's own. That exactness is the point: a
    regex would send somebody's conversation to the wrong vendor, with the
    wrong key, on a body it never parsed.

    Duplicate top-level keys are read LAST-wins, which matches every JSON
    parser an upstream will use — but only within the scanned window: a
    second ``"model"`` sitting past :data:`HEAD_SCAN_BYTES` is invisible here
    while the upstream would honour it. A body with two top-level ``model``
    keys is malformed by convention rather than by grammar, and the outcome
    (the first one routes, the upstream sees the second) is documented rather
    than guarded, because guarding it would mean parsing the whole body —
    which is the thing this path exists to avoid.

    Values come back as :class:`HeadField` — the decoded text plus the span
    of the raw literal, which is what makes a splice possible (a string's
    contents, quotes excluded, but a span that INCLUDES them; a bare literal's
    spelling, e.g. ``"true"``). Containers are skipped, so nothing nested is
    returned. Missing is missing: a field sitting after a container longer
    than :data:`HEAD_SCAN_BYTES` is simply not found, and the caller decides
    what an absence means.
    """
    out: "dict[str, HeadField]" = {}
    end = min(len(head), HEAD_SCAN_BYTES)
    i = 0
    while i < end and head[i] in b" \t\r\n":
        i += 1
    if i >= end or head[i] != 0x7B:  # not a JSON object
        return out
    i += 1
    depth = 1
    expect_key = True
    key: Optional[str] = None
    while i < end:
        c = head[i]
        if c in b" \t\r\n":
            i += 1
            continue
        if c == 0x22:  # a string literal
            literal_start = i
            text, i = _read_json_string(head, i, end)
            if depth != 1:
                continue  # inside a container: read past it, keep nothing
            if text is None:
                return out  # truncated mid-literal; nothing after it is sound
            if expect_key:
                key = text
            else:
                if key is not None:
                    out[key] = HeadField(text, literal_start, i)
                key = None
            continue
        if depth == 1:
            if c == 0x3A:  # ':'
                expect_key = False
                i += 1
                continue
            if c == 0x2C:  # ','
                expect_key = True
                key = None
                i += 1
                continue
            if c == 0x7D:  # '}' — the whole object fit inside the head
                return out
            if c in b"{[":
                depth += 1
                i += 1
                continue
            start = i  # a bare literal: number, true, false, null
            while i < end and head[i] not in b",}] \t\r\n":
                i += 1
            if i == start:
                # NO PROGRESS. The byte here is one this scan has no branch
                # for and the literal scan stops on immediately — a ``]`` at
                # depth 1, i.e. a malformed body (``{"a":1]``, ``{"model":]``).
                # Continuing would re-examine the same byte forever, on the
                # EVENT LOOP: one such request froze the whole daemon, every
                # other session included. A body this broken has no readable
                # model, which is the caller's "cannot route" case, so
                # stopping here is also the honest answer.
                return out
            if key is not None and not expect_key:
                out[key] = HeadField(
                    head[start:i].decode("ascii", "replace"), start, i,
                )
            key = None
            continue
        if c in b"{[":
            depth += 1
        elif c in b"}]":
            depth -= 1
            if depth == 1:
                key = None  # the value that opened it is finished
        i += 1
    return out


__all__ = ["HEAD_SCAN_BYTES", "HeadField", "_shallow_top_level_fields", "_splice_literal"]
