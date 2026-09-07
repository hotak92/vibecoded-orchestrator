# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5 — the ONE block-comment scrub, and the line desync it closes.

WHY THIS EXISTS
---------------
Nine extractors each carried their own
``re.sub(<open>.*?<close>, " ", text, flags=re.DOTALL)``. Substituting ONE
SPACE for a comment spanning N newlines removes N lines from the scrubbed
copy — and EIGHT of the nine then derive entity line numbers from that copy
(``content_clean[:m.start()].count('\\n') + 1``) while slicing bodies out of
``source_lines``, which is split from the ORIGINAL text. So every entity below
a multi-line block comment was stored with its line range and body shifted UP,
cumulatively, by the number of lines the comments above it occupied.

The golden corpus could not catch this: measured, NO fixture file contains a
``/* */`` or ``=begin`` block at all, and the one ``<# #>`` block lives in
``deploy.ps1``, whose producer re-anchors on the ORIGINAL content. That is
precisely why these tests exist as their own file — the invariant needs a
fixture that has the shape the corpus lacks.

WHAT IS PINNED
--------------
* the ACT: newline count in == newline count out, for all three delimiter
  families, and an entity below a multi-line comment lands on its own
  declaration in EVERY producer that maps scrubbed offsets to lines;
* the LEAVE-ALONE: a SINGLE-LINE block comment still becomes one space, so
  ``int/*x*/y`` does not become ``inty``; a file with no block comment is
  byte-identical through the helper; an UNTERMINATED comment is left verbatim
  rather than swallowing the rest of the file;
* that the one producer which does NOT map scrubbed offsets (powershell) is
  unchanged by the migration — it is migrated for the single-implementation
  rule, not to fix a defect there.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List

import pytest

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang import _shared
from vco_lib.codegraph_lang._shared import blank_block_comments_preserving_lines
from vco_lib.codegraph_lang.cpp import extract_cpp_file
from vco_lib.codegraph_lang.csharp import extract_csharp_file
from vco_lib.codegraph_lang.go import extract_go_file
from vco_lib.codegraph_lang.java import extract_java_file
from vco_lib.codegraph_lang.javascript import extract_js_file
from vco_lib.codegraph_lang.powershell import extract_powershell_file
from vco_lib.codegraph_lang.proto import extract_proto_file
from vco_lib.codegraph_lang.ruby import extract_ruby_file
from vco_lib.codegraph_lang.rust import extract_rust_file


class _Helpers:
    project_name = "ScrubProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# The helper's own invariant
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "text,open_tok,close_tok,anchored",
    [
        ("a\n/* one\ntwo\nthree */\nb\n", "/*", "*/", False),
        ("a\n=begin\ndoc\nmore\n=end\nb\n", "=begin", "=end", True),
        ("a\n<#\n.SYNOPSIS\nx\n#>\nb\n", "<#", "#>", False),
        # several comments in one file, and one on a shared line
        ("/*a*/x\n/* b\nc */y\n/* d\ne\nf */z\n", "/*", "*/", False),
        # no comment at all
        ("plain\nlines\nonly\n", "/*", "*/", False),
    ],
)
def test_newline_count_is_preserved(text, open_tok, close_tok, anchored) -> None:
    """THE invariant. Every consumer's line arithmetic rests on it."""
    out = blank_block_comments_preserving_lines(
        text, open_tok, close_tok, line_anchored=anchored
    )
    assert out.count("\n") == text.count("\n"), (
        f"line count changed: {text.count(chr(10))} -> {out.count(chr(10))}"
    )


def test_single_line_comment_still_becomes_one_space() -> None:
    """LEAVE-ALONE: byte-for-byte the pre-v0.2.92 behaviour for the N==0 case.

    A single-line comment carries no newline to separate the tokens around it,
    so collapsing it to "" would glue them: `int/*x*/y` -> `inty`.
    """
    assert blank_block_comments_preserving_lines("int/*x*/y") == "int y"


def test_multi_line_comment_becomes_only_newlines() -> None:
    out = blank_block_comments_preserving_lines("int/* a\nb */y")
    assert out == "int\ny"
    assert out.count("\n") == 1


def test_a_file_without_block_comments_is_returned_verbatim() -> None:
    src = "line one\nline two\n\nline four\n"
    assert blank_block_comments_preserving_lines(src) == src


def test_unterminated_comment_is_left_verbatim() -> None:
    """A scrub that ran away to EOF would delete the rest of the file from the
    parser's view. Non-match is the conservative outcome."""
    src = "class A {}\n/* never closed\nclass B {}\n"
    assert blank_block_comments_preserving_lines(src) == src


def test_two_comments_are_not_one_span() -> None:
    """Non-greedy: `/* a */ keep /* b */` must not swallow `keep`."""
    assert "keep" in blank_block_comments_preserving_lines("/* a */ keep /* b */")


def test_ruby_anchoring_is_required_not_incidental() -> None:
    """`=end` is only a terminator at the start of a line — an unanchored scan
    would end the comment inside an expression."""
    src = "=begin\nx = 1\nputs :not_end\n=end\nreal = 2\n"
    out = blank_block_comments_preserving_lines(src, "=begin", "=end", line_anchored=True)
    assert "real = 2" in out
    assert out.count("\n") == src.count("\n")


# ═══════════════════════════════════════════════════════════════════════════
# The straggler proof, asserted as code (§3.3)
# ═══════════════════════════════════════════════════════════════════════════
_LANG_DIR = Path(_shared.__file__).parent


def test_no_extractor_hand_rolls_a_block_comment_scrub() -> None:
    """One implementation, or this test names the file that grew a second."""
    offenders: List[str] = []
    for py in sorted(_LANG_DIR.glob("*.py")):
        if py.name == "_shared.py":
            continue
        text = py.read_text(encoding="utf-8")
        for m in re.finditer(r"re\.sub\((.{0,80})", text, re.DOTALL):
            frag = m.group(1)
            if "DOTALL" in frag and ("/\\*" in frag or "=begin" in frag or "<#" in frag):
                offenders.append(f"{py.name}: {frag.splitlines()[0]}")
    assert offenders == [], (
        "block-comment scrub duplicated outside _shared.py — route it through "
        "blank_block_comments_preserving_lines: " + "; ".join(offenders)
    )


# ═══════════════════════════════════════════════════════════════════════════
# The ACT, per producer: an entity below a multi-line comment lands on its own
# declaration line. Every source below is written so the entity's FIRST body
# line must equal its declaration.
# ═══════════════════════════════════════════════════════════════════════════
_BLOCK = "/* one\n   two\n   three\n   four */\n"

_CASES = [
    pytest.param(
        extract_csharp_file, "S.cs",
        "namespace N\n{\n" + _BLOCK + "    public class Widget\n    {\n"
        "        public int Size()\n        {\n            return 1;\n"
        "        }\n    }\n}\n",
        "Widget", "Size", id="csharp",
    ),
    pytest.param(
        extract_java_file, "S.java",
        "package p;\n" + _BLOCK + "public class Widget {\n"
        "    public int size() {\n        return 1;\n    }\n}\n",
        "Widget", "size", id="java",
    ),
    pytest.param(
        extract_cpp_file, "s.cpp",
        "#include <v>\n" + _BLOCK + "class Widget {\npublic:\n    int size;\n};\n",
        "Widget", None, id="cpp",
    ),
    pytest.param(
        extract_rust_file, "s.rs",
        "use std::fmt;\n" + _BLOCK + "pub struct Widget {\n    size: u64,\n}\n",
        "Widget", None, id="rust",
    ),
    pytest.param(
        extract_go_file, "s.go",
        "package main\n" + _BLOCK + "type Widget struct {\n    Size int\n}\n",
        "Widget", None, id="go",
    ),
    pytest.param(
        extract_js_file, "s.js",
        "import x from 'y';\n" + _BLOCK + "class Widget {\n"
        "  size() {\n    return 1;\n  }\n}\n",
        "Widget", None, id="javascript",
    ),
    pytest.param(
        extract_proto_file, "s.proto",
        'syntax = "proto3";\npackage p;\n' + _BLOCK +
        "message Widget {\n  int32 size = 1;\n}\n",
        "Widget", None, id="proto",
    ),
]


@pytest.mark.parametrize("extractor,fname,source,class_name,fn_name", _CASES)
def test_entity_below_a_multiline_block_comment_lands_on_its_declaration(
    tmp_path, extractor, fname, source, class_name, fn_name
) -> None:
    """The ACT. Pre-fix the comment's 4 lines vanished from the scrubbed copy,
    so each of these entities was stored 3 lines too high — inside the comment
    or on the line before its own declaration."""
    target = tmp_path / fname
    target.write_text(source, encoding="utf-8")
    fx = extractor(source, target, tmp_path, _Helpers())
    lines = source.split("\n")

    checked = 0
    for e in fx.entities:
        if e.kind == KIND_CLASS and e.name == class_name:
            want = class_name
        elif e.kind == KIND_FUNCTION and fn_name and e.name == fn_name:
            want = fn_name
        else:
            continue
        checked += 1
        assert e.start_line is not None and e.body is not None, (
            f"{fname} {e.kind} {e.name}: producer emitted a null line/body"
        )
        decl = lines[e.start_line - 1]
        assert want in decl, (
            f"{fname} {e.kind} {e.name}: start_line {e.start_line} is "
            f"{decl!r}, which is not its declaration"
        )
        assert (e.body or "").split("\n")[0] == decl, (
            f"{fname} {e.kind} {e.name}: body does not begin at start_line"
        )
    assert checked, f"{fname}: no entity matched — the fixture stopped exercising the path"


def test_ruby_entity_below_a_begin_end_block_lands_on_its_declaration(tmp_path) -> None:
    source = (
        "require 'set'\n"
        "=begin\none\ntwo\nthree\n=end\n"
        "class Widget\n"
        "  def size\n    1\n  end\n"
        "end\n"
    )
    target = tmp_path / "s.rb"
    target.write_text(source, encoding="utf-8")
    fx = extract_ruby_file(source, target, tmp_path, _Helpers())
    lines = source.split("\n")
    widget = next(e for e in fx.entities if e.kind == KIND_CLASS and e.name == "Widget")
    assert widget.start_line is not None
    assert lines[widget.start_line - 1] == "class Widget"


# ═══════════════════════════════════════════════════════════════════════════
# The LEAVE-ALONE for powershell: it re-anchors on the ORIGINAL content, so the
# migration must not move anything there.
# ═══════════════════════════════════════════════════════════════════════════
def test_powershell_is_unchanged_by_the_migration(tmp_path) -> None:
    source = (
        "<#\n.SYNOPSIS\nDeploy helpers.\nMore text.\n#>\n"
        "function Invoke-Thing {\n"
        "    param([string]$Target)\n"
        "    Write-Output $Target\n"
        "}\n"
    )
    target = tmp_path / "s.ps1"
    target.write_text(source, encoding="utf-8")
    fx = extract_powershell_file(source, target, tmp_path, _Helpers())
    lines = source.split("\n")
    fn = next(e for e in fx.entities if e.kind == KIND_FUNCTION)
    assert fn.name == "Invoke-Thing"
    assert fn.start_line is not None and fn.signature is not None
    assert lines[fn.start_line - 1] == "function Invoke-Thing {"
    # the param block is still parsed out of the scrubbed copy
    assert "$Target" in fn.signature


def test_powershell_block_comment_containing_a_decoy_is_still_stripped(tmp_path) -> None:
    """LEAVE-ALONE for the reason the scrub exists at all: a `function` word
    inside a doc block must not become an entity."""
    source = (
        "<#\nfunction Fake-Decoy {\n#>\n"
        "function Real-Thing {\n    Write-Output 1\n}\n"
    )
    target = tmp_path / "s.ps1"
    target.write_text(source, encoding="utf-8")
    fx = extract_powershell_file(source, target, tmp_path, _Helpers())
    names = {e.name for e in fx.entities if e.kind == KIND_FUNCTION}
    assert names == {"Real-Thing"}
