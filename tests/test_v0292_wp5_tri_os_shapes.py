# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5 — tri-OS (R12 / R14): the shape of every OS-dependent input.

WHY THIS EXISTS
---------------
WP-5's code contains NO OS-conditional branch — the extractors are pure text
processing and the three helpers it adds (``skip_leading_whitespace``,
``blank_block_comments_preserving_lines``, ``scan_to_declaration_terminator``)
take a string and return a string/int. There is therefore nothing to gate on
``sys.platform``, and adding such a gate would be the wrong fix.

What DOES vary by operating system is the INPUT: the line terminator a source
file carries. Windows checkouts are CRLF (git's ``core.autocrlf=true`` is the
default there), macOS and Linux are LF. And a ``.ps1`` authored by a Windows
tool routinely carries a UTF-8 BOM, which is not whitespace in Unicode and so
is not stepped over by anything.

Every WP-5 fix is arithmetic over newline COUNTS
(``content_clean[:pos].count('\\n') + 1``) cross-referenced against
``source_lines = content.split('\\n')``. If the scrub's replacement changed
that count for one terminator convention and not another, every stored line
number on that OS would be wrong — and CI would not see it, because CI checks
out LF. So the decision is unit-tested against all three shapes here, which is
R14's requirement where a real per-OS integration cannot run.

WHAT IS PINNED
--------------
* the ACT: newline-count preservation and correct entity line numbers under
  LF (macOS/Linux) and CRLF (Windows), for every migrated delimiter family;
* the whitespace skip steps over a CR as readily as a space or a tab, so a
  CRLF file's declaration is not left one line high on Windows only;
* the LEAVE-ALONE: a CR-only file (classic pre-OS-X Mac) behaves exactly as it
  did before — every producer already treated it as a single line, and this
  change does not alter that; a UTF-8 BOM is handled identically before and
  after, since neither ``\\s`` nor ``str.isspace()`` matches U+FEFF.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang._shared import (
    blank_block_comments_preserving_lines,
    scan_to_declaration_terminator,
    skip_leading_whitespace,
)
from vco_lib.codegraph_lang.csharp import extract_csharp_file
from vco_lib.codegraph_lang.java import extract_java_file
from vco_lib.codegraph_lang.powershell import extract_powershell_file


class _Helpers:
    project_name = "TriOsProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


#: The terminator each supported OS actually produces in a checkout.
_EOL = {
    "linux": "\n",
    "macos": "\n",
    "windows": "\r\n",
}


def _with_eol(text: str, eol: str) -> str:
    return text.replace("\n", eol)


# ═══════════════════════════════════════════════════════════════════════════
# The helpers, per OS line terminator
# ═══════════════════════════════════════════════════════════════════════════
_SCRUB_CASES = [
    ("/*", "*/", False, "a\n/* one\ntwo\nthree */\nclass X {\n"),
    ("=begin", "=end", True, "a\n=begin\none\ntwo\n=end\nclass X\n"),
    ("<#", "#>", False, "a\n<#\none\ntwo\n#>\nfunction X {\n"),
]


@pytest.mark.parametrize("os_name", sorted(_EOL))
@pytest.mark.parametrize("open_tok,close_tok,anchored,body", _SCRUB_CASES)
def test_newline_count_survives_every_os_line_terminator(
    os_name, open_tok, close_tok, anchored, body
) -> None:
    """THE invariant, per OS. Line NUMBERS are derived by counting ``\\n``, and
    ``source_lines`` is split on ``\\n`` — under CRLF both still see one ``\\n``
    per line, so the two stay in step."""
    text = _with_eol(body, _EOL[os_name])
    out = blank_block_comments_preserving_lines(
        text, open_tok, close_tok, line_anchored=anchored
    )
    assert out.count("\n") == text.count("\n")
    assert len(out.split("\n")) == len(text.split("\n"))


@pytest.mark.parametrize("os_name", sorted(_EOL))
def test_the_whitespace_skip_steps_over_this_os_line_terminator(os_name) -> None:
    """``str.isspace()`` is True for ``\\r``, so a CRLF file's declaration is
    reached exactly as an LF file's is. Without this, the Windows-only symptom
    would be a declaration left one line high."""
    text = _with_eol("\n\t  public class X {", _EOL[os_name])
    pos = skip_leading_whitespace(text, 0, len(text))
    assert text[pos:pos + 6] == "public", repr(text[pos:pos + 10])


@pytest.mark.parametrize("os_name", sorted(_EOL))
def test_the_terminator_scan_is_line_ending_agnostic(os_name) -> None:
    eol = _EOL[os_name]
    brace = _with_eol("() -> Result<(), E>\n{\n", eol)
    assert scan_to_declaration_terminator(brace, 0)[0] == "{"
    semi = _with_eol("(&mut self);\n", eol)
    assert scan_to_declaration_terminator(semi, 0)[0] == ";"


# ═══════════════════════════════════════════════════════════════════════════
# End to end, per OS: the stored line number must be identical on all three
# ═══════════════════════════════════════════════════════════════════════════
_CSHARP = """namespace N
{
    /* one
       two
       three */
    [Route("api/x")]
    public class Widget
    {
        public int Size()
        {
            return 1;
        }
    }
}
"""

_JAVA = """package p;
/* one
   two */
public class Widget {
    public int size() {
        return 1;
    }
}
"""

_PS1 = """<#
.SYNOPSIS
doc
#>

function Invoke-Thing {
    Write-Output 1
}
"""


@pytest.mark.parametrize("os_name", sorted(_EOL))
@pytest.mark.parametrize(
    "extractor,fname,source,expected",
    [
        (extract_csharp_file, "W.cs", _CSHARP, {"Widget": 7, "Size": 9}),
        (extract_java_file, "W.java", _JAVA, {"Widget": 4, "size": 5}),
        (extract_powershell_file, "w.ps1", _PS1, {"Invoke-Thing": 6}),
    ],
    ids=["csharp", "java", "powershell"],
)
def test_stored_line_numbers_are_identical_on_every_os(
    tmp_path, os_name, extractor, fname, source, expected
) -> None:
    """The same file, checked out with each OS's terminator, must yield the
    same stored line numbers — a graph built on Windows and one built on Linux
    describe the same source."""
    text = _with_eol(source, _EOL[os_name])
    target = tmp_path / f"{os_name}_{fname}"
    target.write_text(text, encoding="utf-8", newline="")
    fx = extractor(text, target, tmp_path, _Helpers())
    got = {
        e.name: e.start_line
        for e in fx.entities
        if e.kind in (KIND_CLASS, KIND_FUNCTION) and e.name in expected
    }
    assert got == expected, f"{os_name}: {got} != {expected}"


# ═══════════════════════════════════════════════════════════════════════════
# LEAVE-ALONE — the degenerate shapes behave exactly as before
# ═══════════════════════════════════════════════════════════════════════════
def test_a_cr_only_file_is_unchanged_in_kind() -> None:
    """Classic pre-OS-X Mac line endings. Every producer already treated such a
    file as ONE line (``split('\\n')`` finds no separator), and it still does:
    zero newlines in, zero out, so nothing regressed. Stated as a test rather
    than assumed, because "macOS" must not be read as "CR".
    """
    text = "a\r/* one\rtwo */\rclass X {\r"
    out = blank_block_comments_preserving_lines(text)
    assert text.count("\n") == 0 and out.count("\n") == 0
    assert len(out.split("\n")) == len(text.split("\n")) == 1


def test_a_utf8_bom_is_not_whitespace_before_or_after_the_fix() -> None:
    """A Windows-authored ``.ps1`` commonly starts with U+FEFF. It is a format
    character, not whitespace, in Unicode — so neither the old ``^\\s*`` anchor
    nor the new ``^[ \\t]*`` one steps over it, and the migration changed
    nothing here. Pinned so a future 'just strip whitespace' edit cannot
    quietly claim otherwise."""
    assert not "﻿".isspace()
    assert re.match(r"\s", "﻿") is None
    text = "﻿  public class X {"
    assert skip_leading_whitespace(text, 0, len(text)) == 0


@pytest.mark.parametrize("os_name", sorted(_EOL))
def test_a_bom_prefixed_powershell_file_still_yields_its_function(tmp_path, os_name) -> None:
    """The BOM sits on line 1; the declaration is below it, which is the real
    layout. Both anchors in this producer use ``^[ \\t]*`` now, so they agree."""
    text = "﻿" + _with_eol(_PS1, _EOL[os_name])
    target = tmp_path / f"{os_name}_bom.ps1"
    target.write_text(text, encoding="utf-8", newline="")
    fx = extract_powershell_file(text, target, tmp_path, _Helpers())
    names = {e.name for e in fx.entities if e.kind == KIND_FUNCTION}
    assert names == {"Invoke-Thing"}


def test_no_wp5_helper_branches_on_the_platform() -> None:
    """The claim this module opens with, asserted: a platform branch in a pure
    text helper would be a defect, not portability."""
    from vco_lib.codegraph_lang import _shared

    src = "".join(
        line
        for line in open(_shared.__file__, encoding="utf-8")
        if not line.lstrip().startswith("#")
    )
    for name in ("skip_leading_whitespace", "blank_block_comments_preserving_lines",
                 "scan_to_declaration_terminator"):
        assert f"def {name}(" in src
    assert "sys.platform" not in src
    assert "os.name" not in src
