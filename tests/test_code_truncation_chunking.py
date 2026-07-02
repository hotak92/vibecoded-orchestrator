# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 (P3): unit tests for model-aware chunking of over-budget code
entities in `weaviate_mcp.code_truncation`.

Covers:
  * in-budget function → 1 text carrying the FULL body, NO chunk header.
  * over-budget function → N (>=2) chunk texts, each with a correct
    `[chunk i/N]` header (1-indexed in the header text) that
    `server._parse_chunk_header` accepts.
  * the signature is always present in chunk 0.
  * class variant behaves the same.
  * back-compat: the existing `truncate_*_for_embedding` still work.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "claude_mcp_servers"))

from weaviate_mcp.code_truncation import (  # noqa: E402
    chunk_or_truncate_for_embedding,
    chunk_or_truncate_class_for_embedding,
    truncate_function_for_embedding,
    truncate_class_for_embedding,
    _max_chars_for_model,
)
from weaviate_mcp.server import _parse_chunk_header  # noqa: E402

_MODEL = "codesage/codesage-large-v2"  # ~8000-char budget
_HEADER_RE = re.compile(r"^\[chunk (\d+)/(\d+)\]\n\n")


def _big_body(n_lines: int) -> str:
    """A function body large enough to exceed the CodeSage budget."""
    lines = [f"    result_{i} = compute_value(input_{i}) + offset_{i} * scale_{i}" for i in range(n_lines)]
    return "def big():\n" + "\n".join(lines) + "\n    return result_0\n"


def test_in_budget_function_returns_single_full_body_no_header():
    sig = "def small(x)"
    body = "def small(x):\n    return x + 1\n"
    out = chunk_or_truncate_for_embedding(sig, body, language="python", model=_MODEL)
    assert isinstance(out, list)
    assert len(out) == 1, "in-budget entity → exactly one text"
    # No chunk header on the single case.
    assert _parse_chunk_header(out[0]) is None
    assert _HEADER_RE.match(out[0]) is None
    # Full body preserved (nothing dropped).
    assert "return x + 1" in out[0]


def test_over_budget_function_chunks_with_correct_headers():
    sig = "def big()"
    body = _big_body(600)  # well over ~8000 chars
    assert len(body) > _max_chars_for_model(_MODEL), "fixture must exceed budget"
    out = chunk_or_truncate_for_embedding(sig, body, language="python", model=_MODEL, full_name="mod.big")
    assert len(out) >= 2, "over-budget entity → multiple chunks"
    total = len(out)
    for i, text in enumerate(out):
        parsed = _parse_chunk_header(text)
        assert parsed is not None, f"chunk {i} missing a parseable header"
        one_indexed, parsed_total = parsed
        assert one_indexed == i + 1, "header number is 1-indexed"
        assert parsed_total == total, "header total matches chunk count"


def test_signature_present_in_chunk_zero_when_chunked():
    sig = "def big()"
    body = _big_body(600)
    out = chunk_or_truncate_for_embedding(sig, body, language="python", model=_MODEL, full_name="mod.big")
    assert len(out) >= 2
    # Strip the header, then the signature must lead chunk 0.
    chunk0 = _HEADER_RE.sub("", out[0], count=1)
    assert "def big" in chunk0, "signature must be present in chunk 0"


def test_class_in_budget_single_text():
    sig = "class Foo(Base)"
    body = "class Foo(Base):\n    def a(self):\n        return 1\n"
    out = chunk_or_truncate_class_for_embedding(sig, body, methods=["a"], language="python", model=_MODEL)
    assert len(out) == 1
    assert _parse_chunk_header(out[0]) is None


def test_class_over_budget_chunks():
    sig = "class Big(Base)"
    methods = [f"m{i}" for i in range(50)]
    body = "class Big(Base):\n" + "\n".join(
        f"    def m{i}(self):\n        return compute(self.state_{i}) + self.offset_{i} * {i}"
        for i in range(400)
    )
    assert len(body) > _max_chars_for_model(_MODEL)
    out = chunk_or_truncate_class_for_embedding(sig, body, methods=methods, language="python", model=_MODEL, full_name="mod.Big")
    assert len(out) >= 2
    total = len(out)
    for i, text in enumerate(out):
        parsed = _parse_chunk_header(text)
        assert parsed == (i + 1, total)
    chunk0 = _HEADER_RE.sub("", out[0], count=1)
    assert "class Big" in chunk0


def test_backcompat_truncate_helpers_still_work():
    sig = "def f()"
    body = "def f():\n    return 1\n"
    ft = truncate_function_for_embedding(sig, body, language="python", model=_MODEL)
    assert "def f" in ft
    ct = truncate_class_for_embedding("class C", "class C:\n    pass\n", methods=["x"], language="python", model=_MODEL)
    assert "class C" in ct


def test_empty_body_returns_signature_only():
    out = chunk_or_truncate_for_embedding("def f()", "", language="python", model=_MODEL)
    assert len(out) == 1
    assert "def f" in out[0]
