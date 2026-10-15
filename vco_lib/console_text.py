# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Console-safe stdout/stderr for VCO entry points on Windows (v0.2.103).

The ONE home for "what encoding does VCO print in". Field (Windows, cp1252
locale): em dashes and arrows in install output and in the code-graph
walk's "already running" refusal printed as garbage. Python only renders
Unicode correctly on Windows when it writes to a REAL console (it then uses
the console's wide-character API whatever the code page). Whenever its
output goes through a pipe or a file instead — a PowerShell wrapper
capturing ``2>&1``, a parent decoding with ``text=True``, ``| Tee-Object``
— the bytes are decoded by a reader whose code page Python cannot know:
PowerShell uses the OEM code page (cp437/cp850), a Python parent the ANSI
one (cp1252), the launcher UTF-8. Writing UTF-8 (what ``install.py`` used to
force) or cp1252 (Python's default) is garbage to at least one of them.

:func:`harden_stdio` therefore picks, per stream, on Windows only:

* a real console → leave the encoding alone (the console API renders
  Unicode), just never crash on a glyph (``errors="replace"``);
* a pipe/file whose reader DECLARED UTF-8 (``PYTHONUTF8=1`` / UTF-8 mode, or
  ``PYTHONIOENCODING=utf-8`` — the launcher sets both for every Python child)
  → UTF-8;
* any other pipe/file → ASCII, with typographic characters transliterated
  (``—`` → ``-``, ``→`` → ``->``, ``✓`` → ``[ok]``, unknown → ``?``). ASCII
  is the one encoding every Windows reader decodes identically.

POSIX: a no-op (terminals and pipes are UTF-8 there).
"""

from __future__ import annotations

import codecs
import os
import sys
from typing import Any, Iterable, Optional

#: The codec error handler that maps a non-ASCII character to its ASCII
#: stand-in. Registered on first use of :func:`harden_stdio`.
ASCII_ERROR_HANDLER = "vco_console_ascii"

#: Typographic characters VCO's user-facing strings use, with their ASCII
#: stand-ins. Anything else outside ASCII becomes ``?``.
ASCII_FALLBACKS: dict = {
    "—": "-",     # em dash
    "–": "-",     # en dash
    "‒": "-",     # figure dash
    "−": "-",     # minus sign
    "→": "->",    # rightwards arrow
    "←": "<-",    # leftwards arrow
    "↔": "<->",
    "⇒": "=>",
    "…": "...",   # ellipsis
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "«": '"', "»": '"',
    "•": "*", "·": "*",
    "≥": ">=", "≤": "<=", "≠": "!=", "≈": "~",
    "×": "x",
    " ": " ",
    "✓": "[ok]", "✔": "[ok]", "✅": "[ok]",
    "✗": "[x]", "✘": "[x]", "❌": "[x]",
    "⚠": "[!]", "️": "",  # warning sign; variation selector-16 drops
    "ℹ": "[i]",
    "─": "-", "━": "-", "═": "=", "│": "|",
}


def to_console_ascii(text: str) -> str:
    """*text* with every non-ASCII character replaced by its stand-in."""
    return "".join(ch if ord(ch) < 128 else ASCII_FALLBACKS.get(ch, "?") for ch in text)


def _ascii_error_handler(exc: UnicodeError) -> "tuple[str, int]":
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    return to_console_ascii(exc.object[exc.start:exc.end]), exc.end


def _register_handler() -> None:
    try:
        codecs.lookup_error(ASCII_ERROR_HANDLER)
    except LookupError:
        codecs.register_error(ASCII_ERROR_HANDLER, _ascii_error_handler)


def _is_real_console(stream: Any) -> bool:
    """True when *stream* writes through the Windows console API."""
    raw = getattr(getattr(stream, "buffer", None), "raw", None)
    return type(raw).__name__ == "_WindowsConsoleIO"


def _reader_declared_utf8(environ: "os._Environ[str] | dict[str, str]") -> bool:
    if getattr(sys.flags, "utf8_mode", 0):
        return True
    enc = (environ.get("PYTHONIOENCODING") or "").split(":", 1)[0].strip().lower()
    return enc.replace("-", "").replace("_", "") in ("utf8", "utf8sig")


def stream_plan(stream: Any, *, environ: Optional[dict] = None) -> "dict[str, str] | None":
    """The ``reconfigure`` keywords :func:`harden_stdio` applies to *stream*
    on Windows (``None`` → leave it alone). Pure decision, unit-testable."""
    if stream is None or not hasattr(stream, "reconfigure"):
        return None
    if _is_real_console(stream):
        return {"errors": "replace"}
    if _reader_declared_utf8(os.environ if environ is None else environ):
        return {"encoding": "utf-8", "errors": "replace"}
    return {"encoding": "ascii", "errors": ASCII_ERROR_HANDLER}


def harden_stdio(streams: Optional[Iterable[str]] = None, *, platform: Optional[str] = None,
                 environ: Optional[dict] = None) -> None:
    """Make ``sys.stdout`` / ``sys.stderr`` (or the named *streams*) print
    readable text on any Windows reader — see the module docstring. A no-op
    off Windows. Never raises: a stream that cannot be reconfigured
    (detached, replaced by a test harness) is left as it is."""
    if (platform or sys.platform) != "win32":
        return
    _register_handler()
    for name in (streams or ("stdout", "stderr")):
        stream = getattr(sys, name, None)
        plan = stream_plan(stream, environ=environ)
        if plan is None:
            continue
        try:
            stream.reconfigure(**plan)  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError, LookupError):
            continue


__all__ = ["ASCII_ERROR_HANDLER", "ASCII_FALLBACKS", "harden_stdio", "stream_plan",
           "to_console_ascii"]
