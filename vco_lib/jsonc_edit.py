# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""JSONC — read it, and edit it without losing a byte the edit did not ask for.

VS Code's ``settings.json`` (user and workspace) is JSONC: comments and
trailing commas are normal there, and VS Code writes both. :func:`json.loads`
rejects such a file, and re-serialising a parsed form silently deletes every
comment — so VCO's writers used either to refuse the file outright or, in the
env-projection writer, to treat it as ``{}`` and write that back, destroying
the user's settings. This module is the ONE home for doing it properly
(v0.2.97); every writer of a JSON(C) settings file routes through it:

* :func:`loads` parses JSONC — comments skipped outside strings, a trailing
  comma before ``}`` / ``]`` dropped — and then defers to the strict parser,
  so every value means exactly what :func:`json.loads` says it means;
* :func:`rewrite_preserving` turns the ORIGINAL text into one that parses to
  a caller-supplied new root, by editing only the members that changed — at
  the top level and one object level below it — and leaving every other byte
  (comments, blank lines, key order, indentation, CRLF) where it was;
* the result is VERIFIED by re-parsing before it is returned: anything but
  "exactly the requested settings" raises :class:`JsoncEditRefused`, and the
  caller writes nothing. An edit that would have to replace a value holding
  comments, or reach deeper than one level, is refused the same way rather
  than approximated.

Pure standard library, on purpose: ``vco_lib.config_projection`` runs where
the venv's packages may not import, and must still be able to use it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "JsoncEditRefused",
    "dumps_preserving",
    "is_strict_json",
    "load_object",
    "loads",
    "read_object",
    "rewrite_preserving",
    "sniff_indent",
    "sniff_newline",
]

_PUNCT = "{}[]:,"
_LITERAL_STOP = _PUNCT + ' \t\r\n"/'

#: One significant token: ``(kind, start, end)`` — kind is ``"str"``,
#: ``"punct"`` or ``"lit"``; offsets index the text.
Token = tuple[str, int, int]
#: A container address: ``()`` is the root object, ``(key,)`` the object
#: that is the root's ``key`` member. Nothing deeper is edited.
Container = tuple[str, ...]

_REMOVE = object()


class JsoncEditRefused(ValueError):
    """The edit could not be made without risking content. Nothing to write.

    ``reason`` is a stable machine code; ``message`` is for the user.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def tokens(text: str) -> list[Token]:
    """Every significant token of a JSONC text; whitespace, a BOM and
    comments (outside strings) skipped. ``ValueError`` on an unterminated
    string or comment, or a character JSONC does not allow."""
    found: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n﻿":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError(f"unterminated /* comment at offset {i}")
            i = end + 2
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise ValueError(f"unterminated string at offset {i}")
            found.append(("str", i, j + 1))
            i = j + 1
        elif ch in _PUNCT:
            found.append(("punct", i, i + 1))
            i += 1
        else:
            j = i
            while j < n and text[j] not in _LITERAL_STOP:
                j += 1
            if j == i:
                raise ValueError(f"unexpected {ch!r} at offset {i}")
            found.append(("lit", i, j))
            i = j
    return found


def _is(text: str, token: Optional[Token], chars: str) -> bool:
    return token is not None and token[0] == "punct" and text[token[1]] in chars


def _is_trailing_comma(text: str, toks: Sequence[Token], k: int) -> bool:
    """A comma right after a value and right before ``}`` / ``]`` — the one
    JSONC allowance. ``{,}`` is not one: nothing precedes it."""
    return (
        _is(text, toks[k], ",")
        and k > 0
        and not _is(text, toks[k - 1], "{[,:")
        and _is(text, toks[k + 1] if k + 1 < len(toks) else None, "}]")
    )


def loads(text: str) -> Any:
    """Parse JSONC; ``ValueError`` when it is not valid JSONC either."""
    toks = tokens(text)
    parts = [
        text[start:end]
        for k, (_kind, start, end) in enumerate(toks)
        if not _is_trailing_comma(text, toks, k)
    ]
    return json.loads(" ".join(parts))


def read_object(path: "str | os.PathLike[str]") -> tuple[dict, str]:
    """``(data, raw)`` of a JSON(C) file whose top level is an object.

    Raises ``OSError`` when it cannot be read and ``ValueError`` (a
    ``UnicodeDecodeError`` included) when it is not UTF-8 JSONC or its top
    level is not an object — each carrying the reason, so a caller that
    refuses to touch the file can say WHY (v0.2.97).
    """
    with open(path, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    data = loads(raw)
    if not isinstance(data, dict):
        kind = {list: "array", str: "string", bool: "boolean", type(None): "null"}.get(
            type(data), "number")
        raise ValueError(f"its top level is a JSON {kind}, not an object")
    return data, raw


def load_object(path: "str | os.PathLike[str]") -> Optional[tuple[dict, str]]:
    """:func:`read_object`, or ``None`` — the caller's "leave it alone" case.

    ``raw`` is what :func:`dumps_preserving` needs to edit it in place.
    """
    try:
        return read_object(path)
    except (OSError, ValueError):
        return None


def dumps_preserving(raw: str, data: Mapping[str, Any], *, indent: int) -> Optional[str]:
    """The bytes to write so the file ``raw`` came from holds ``data``.

    A strict-JSON original is re-serialised (``indent``, trailing newline) —
    it has no comments to lose. A JSONC one is EDITED member by member,
    comments kept (:func:`rewrite_preserving`); ``None`` when that edit cannot
    be verified, and the caller must then write nothing.
    """
    if is_strict_json(raw):
        return json.dumps(data, indent=indent) + "\n"
    try:
        return rewrite_preserving(raw, data)
    except JsoncEditRefused:
        return None


def is_strict_json(text: str) -> bool:
    """True when :func:`json.loads` accepts ``text`` as it stands."""
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def sniff_indent(text: str) -> str:
    """The file's own indentation unit, so an edit or rewrite does not
    reformat it: a tab or a run of spaces from the first indented line;
    four spaces, what VS Code writes, when there is none."""
    for line in text.splitlines():
        if not line.strip():
            continue
        stripped = line.lstrip(" \t")
        prefix = line[: len(line) - len(stripped)]
        if prefix:
            return "\t" if prefix.startswith("\t") else prefix
    return "    "


def sniff_newline(text: str) -> str:
    """``"\\r\\n"`` when the file already uses CRLF, else ``"\\n"``.

    Rewriting a Windows user's CRLF file as LF would show up as a whole-file
    diff in their VCS. ``json.dumps`` only ever emits ``\\n``, so the
    translation is done on the way out.
    """
    return "\r\n" if "\r\n" in text else "\n"


# ---------------------------------------------------------------------------
# Locating things in the token stream
# ---------------------------------------------------------------------------


def _value_end(text: str, toks: Sequence[Token], k: int) -> int:
    """Index of the LAST token of the value starting at token ``k``."""
    if not _is(text, toks[k], "{["):
        return k
    depth = 0
    for j in range(k, len(toks)):
        if _is(text, toks[j], "{["):
            depth += 1
        elif _is(text, toks[j], "}]"):
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced brackets")


def _members(text: str, toks: Sequence[Token], open_k: int) -> tuple[list[tuple[str, int, int, int]], int]:
    """Direct members of the object opening at ``open_k``:
    ``[(key, key_k, value_first_k, value_last_k)]`` and the closing index."""
    out: list[tuple[str, int, int, int]] = []
    k = open_k + 1
    while k < len(toks):
        if _is(text, toks[k], "}"):
            return out, k
        if _is(text, toks[k], ","):
            k += 1
            continue
        if toks[k][0] != "str" or not _is(text, toks[k + 1] if k + 1 < len(toks) else None, ":"):
            raise ValueError(f"expected a member at offset {toks[k][1]}")
        last = _value_end(text, toks, k + 2)
        out.append((json.loads(text[toks[k][1]:toks[k][2]]), k, k + 2, last))
        k = last + 1
    raise ValueError("unterminated object")


def _container(text: str, toks: Sequence[Token], where: Container) -> Optional[int]:
    """Token index of the ``{`` that opens ``where``; None when absent."""
    if not toks or not _is(text, toks[0], "{"):
        raise ValueError("the root is not an object")
    if not where:
        return 0
    members, _close = _members(text, toks, 0)
    hits = [m for m in members if m[0] == where[0]]
    if len(hits) != 1 or not _is(text, toks[hits[0][2]], "{"):
        return None
    return hits[0][2]


def _has_comment(text: str, toks: Sequence[Token], first: int, last: int) -> bool:
    """True when a comment sits between the tokens ``first..last``."""
    return any(
        "/" in text[toks[k][2]:toks[k + 1][1]] for k in range(first, last)
    )


def _line_indent(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    head = text[start:pos]
    return head if not head.strip() else head[: len(head) - len(head.lstrip(" \t"))]


# ---------------------------------------------------------------------------
# One edit at a time, each against a fresh token stream
# ---------------------------------------------------------------------------


def _render(value: Any, indent: str, unit: str, nl: str, *, inline: bool = False) -> str:
    """JSON for ``value`` as it will sit after ``"key": `` on a line indented
    ``indent`` — nested lines continue that indentation. ``inline`` (the
    container is written on one line) keeps the value on one line too."""
    if inline or not isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, indent=unit, ensure_ascii=False).replace("\n", nl + indent)


def _is_inline(text: str, toks: Sequence[Token], open_k: int, close_k: int) -> bool:
    """True when the object opening at ``open_k`` is written on one line."""
    return "\n" not in text[toks[open_k][1]:toks[close_k][1]]


def _collapse_if_empty(text: str, where: Container) -> str:
    """``{ <whitespace> }`` left by removing an object's last member becomes
    ``{}``. Anything else between the braces — a comment — stays."""
    toks = tokens(text)
    open_k = _container(text, toks, where)
    if open_k is None:
        return text
    members, close_k = _members(text, toks, open_k)
    start, end = toks[open_k][2], toks[close_k][1]
    if members or text[start:end].strip():
        return text
    return text[:start] + text[end:]


def _cut(text: str, cuts: list[tuple[int, int]], member: tuple[int, int]) -> str:
    """Remove ``cuts``; widen the member's cut to its whole line when that
    line would be left holding only whitespace (its line ending included)."""
    line_start = text.rfind("\n", 0, member[0]) + 1
    line_end = text.find("\n", member[1])
    line_end = len(text) if line_end < 0 else line_end + 1
    kept = "".join(
        ch for pos, ch in enumerate(text[line_start:line_end], start=line_start)
        if not any(s <= pos < e for s, e in cuts)
    )
    if not kept.strip():
        cuts = [(s, e) for s, e in cuts if not (line_start <= s and e <= line_end)]
        cuts.append((line_start, line_end))
    out = text
    for s, e in sorted(cuts, reverse=True):
        out = out[:s] + out[e:]
    return out


def _remove(text: str, where: Container, key: str) -> str:
    toks = tokens(text)
    open_k = _container(text, toks, where)
    if open_k is None:
        return text
    members, _close = _members(text, toks, open_k)
    hits = [m for m in members if m[0] == key]
    if not hits:
        return text
    if len(hits) > 1:
        raise JsoncEditRefused("jsonc_duplicate_key", f"`{key}` appears more than once")
    _key, key_k, _first, last = hits[0]
    start, end = toks[key_k][1], toks[last][2]
    cuts = [(start, end)]
    after = toks[last + 1] if last + 1 < len(toks) else None
    before = toks[key_k - 1]
    if _is(text, after, ","):
        cuts.append((after[1], after[2]))  # type: ignore[index]
    elif _is(text, before, ","):
        cuts.append((before[1], before[2]))
    return _collapse_if_empty(_cut(text, cuts, (start, end)), where)


def _set(text: str, where: Container, key: str, value: Any) -> str:
    toks = tokens(text)
    open_k = _container(text, toks, where)
    unit, nl = sniff_indent(text), sniff_newline(text)
    if open_k is None:
        # The container itself is missing: create it as a root member.
        return _set(text, (), where[0], {key: value})
    members, close_k = _members(text, toks, open_k)
    hits = [m for m in members if m[0] == key]
    if len(hits) > 1:
        raise JsoncEditRefused("jsonc_duplicate_key", f"`{key}` appears more than once")
    if hits:
        _key, key_k, first, last = hits[0]
        if _has_comment(text, toks, first, last):
            raise JsoncEditRefused(
                "jsonc_comment_in_replaced_value",
                f"the value of `{key}` holds comments that replacing it would drop",
            )
        indent = _line_indent(text, toks[key_k][1])
        start, end = toks[first][1], toks[last][2]
        inline = _is_inline(text, toks, open_k, close_k)
        return text[:start] + _render(value, indent, unit, nl, inline=inline) + text[end:]

    open_pos, close_pos = toks[open_k][1], toks[close_k][1]
    if members:
        indent = _line_indent(text, toks[members[0][1]][1])
    else:
        indent = _line_indent(text, open_pos) + unit
    rendered = _render(value, indent, unit, nl, inline=_is_inline(text, toks, open_k, close_k))
    member = f"{json.dumps(key, ensure_ascii=False)}: {rendered}"
    if not members:
        if "\n" not in text[open_pos:close_pos]:
            return text[: open_pos + 1] + member + text[open_pos + 1:]
        return text[: open_pos + 1] + nl + indent + member + text[open_pos + 1:]
    last = members[-1][3]
    value_end = toks[last][2]
    after = toks[last + 1]
    trailing = _is(text, after, ",")
    anchor = after[2] if trailing else value_end
    line_end = text.find("\n", anchor)
    if line_end < 0 or line_end > close_pos:
        # The container closes on this line: insert inline.
        if trailing:
            return text[:anchor] + " " + member + "," + text[anchor:]
        return text[:value_end] + ", " + member + text[value_end:]
    line_end = line_end - 1 if text[line_end - 1] == "\r" else line_end
    inserted = nl + indent + member + ("," if trailing else "")
    out = text[:line_end] + inserted + text[line_end:]
    if not trailing:
        out = out[:value_end] + "," + out[value_end:]
    return out


# ---------------------------------------------------------------------------
# The one entry point writers use
# ---------------------------------------------------------------------------


def _edits(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[tuple[Container, str, Any]]:
    """What turns ``old`` into ``new``: removals first, then sets; one level
    of nesting is diffed member by member, anything deeper is a whole-value
    set of the depth-1 member."""
    ops: list[tuple[Container, str, Any]] = []
    for key in old:
        if key not in new:
            ops.append(((), key, _REMOVE))
    for key, value in new.items():
        if key in old and old[key] == value:
            continue
        before = old.get(key)
        if key in old and isinstance(before, dict) and isinstance(value, dict):
            ops.extend(((key,), k, _REMOVE) for k in before if k not in value)
            ops.extend(
                ((key,), k, v) for k, v in value.items()
                if k not in before or before[k] != v
            )
        else:
            ops.append(((), key, value))
    ops.sort(key=lambda op: op[2] is not _REMOVE)
    return ops


def rewrite_preserving(text: str, new_root: Mapping[str, Any]) -> str:
    """``text`` edited so it parses to ``new_root``; every other byte kept.

    Raises :class:`JsoncEditRefused` when that cannot be done exactly — the
    caller must then write nothing and say why.
    """
    try:
        old = loads(text)
    except ValueError as exc:
        raise JsoncEditRefused("not_jsonc", f"not valid JSON or JSONC ({exc})") from exc
    if not isinstance(old, dict):
        raise JsoncEditRefused("not_an_object", "the top level is not a JSON object")
    out = text
    try:
        for where, key, value in _edits(old, new_root):
            out = _remove(out, where, key) if value is _REMOVE else _set(out, where, key, value)
        verified = loads(out) == dict(new_root)
    except JsoncEditRefused:
        raise
    except ValueError:
        verified = False
    if not verified:
        raise JsoncEditRefused(
            "jsonc_edit_unverified",
            "the edited text did not re-read as exactly the requested settings",
        )
    return out
