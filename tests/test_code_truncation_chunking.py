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

_MODEL = "codesage/codesage-large-v2"  # 3 584-char budget (1 024 num_ctx × 3.5)
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


def test_v0292_jina_budget_matches_chunking_ssot_not_stale_8192():
    """WP-E (v0.2.92): CODE_MODEL_TOKEN_LIMITS used to hand-duplicate
    chunking.py's MODEL_TOKEN_LIMITS and had drifted — jina was pinned at
    the OLD 8192 tokens (28672 chars) here while chunking.py had already
    been corrected to 2048 tokens (jina-v2 is trained at 512; 2048 is the
    shipped conservative ceiling). CPU-tier installs (install.py's "cpu"
    EMBEDDING_CONFIGS profile) actively select jina as the code model, so
    the stale value silently over-budgeted truncation for real installs.

    This pins the CORRECT (SSOT-derived) value so the drift cannot silently
    reopen. Red-proofed against the pre-fix file
    (/tmp/wp-e-redproof/pre/code_truncation_pre.py, git HEAD copy): the old
    hardcoded dict resolves both jina keys to 8192 tokens / 28672 chars,
    which fails the assertions below.
    """
    from weaviate_mcp.chunking import _num_ctx_for_model

    jina_keys = (
        "unclemusclez/jina-embeddings-v2-base-code:latest",
        "jina-embeddings-v2-base-code",
    )
    ssot_tokens = _num_ctx_for_model(jina_keys[0])
    assert ssot_tokens == 2048, (
        "chunking.py's SSOT itself changed — update this test's expectation, "
        "not code_truncation.py's derivation"
    )
    for key in jina_keys:
        # 2048 tokens * 3.5 chars/token = 7168, NOT the stale 8192 * 3.5 = 28672.
        assert _max_chars_for_model(key) == 7168
        assert _max_chars_for_model(key) != 28672


def test_v0292_w1_codesage_budget_is_the_served_window_not_the_arch_cap():
    """W1 (wiring audit, 2026-09-05): the budget must derive from the SERVED
    1 024-token window (sentence_bert_config.json max_seq_length), not the
    2 048 architectural cap in config.json.

    The old 2 048 entry budgeted 7 168 chars ≈ 2 193 real CodeSage tokens
    (measured on real repo Python) — 2.1x the served window, silently
    truncated at HTTP 200. 1 024 × 3.5 = 3 584 chars. Red-proof: reverting
    the SSOT entry to 2 048 fails this (and the chunking tests) while the
    behaviour-level guard (the shrink tests in
    test_secondary_window_exact_bound.py) catches the runtime half.
    """
    from weaviate_mcp.chunking import _num_ctx_for_model

    cs_keys = ("codesage/codesage-large-v2", "codesage-large-v2")
    for key in cs_keys:
        assert _num_ctx_for_model(key) == 1024, (
            "chunking.py's SSOT itself changed — update this test's "
            "expectation, not code_truncation.py's derivation"
        )
        assert _max_chars_for_model(key) == 3584
        assert _max_chars_for_model(key) != 7168, (
            "the 2 048-derived budget fed CodeSage 2.1x its served window"
        )


def test_v0292_w1_codesage_single_entity_capped_at_the_new_budget():
    """A maximal entity's assembled priority text must come out AT the new
    3 584-char budget (not the old 7 168) — the truncation actually caps at
    the served-window-derived size.

    Density arithmetic (measured 2026-09-05, W1): real repo Python
    tokenises at 3.46-3.48 c/t on budget-window slices, so 3 584 chars is
    ~1 036 CodeSage tokens — at the served 1 024 window. Denser windows
    (2.96 c/t observed) can still overflow; that residual is the service's
    refusal + the caller's shrink (tagged), pinned in
    test_secondary_window_exact_bound.py.
    """
    sig = "def big(x):"
    lines = [
        f"    y_{i} = compute_value(input_{i}) + offset_{i} * scale_{i}"
        for i in range(200)
    ]
    body = sig + ":\n" + "\n".join(lines)
    assert len(body) > 7_168, "fixture must exceed even the OLD budget"

    text = truncate_function_for_embedding(
        sig, body, language="python", model=_MODEL
    )
    assert len(text) <= 3_584, (
        "the assembled text is capped at the served-window budget; the old "
        "cap let 7 168 chars through (≈ 2 193 real tokens vs a 1 024 window)"
    )
    # And the over-budget twin splits instead of truncating.
    parts = chunk_or_truncate_for_embedding(
        sig, body, language="python", model=_MODEL, full_name="mod.big"
    )
    assert len(parts) >= 2, "an old-budget-sized entity now chunks"
