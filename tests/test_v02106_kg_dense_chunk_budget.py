# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — no KG / document / code chunk exceeds its model's window on
dense content (owner ruling 2026-11-11, "fix + re-embed").

The KG presets counted ``len // 4`` and their maxima measured 122-136% of the
model windows on dense text (qwen3 32 768 chars ≈ 12 864 tokens at the
measured 2.547 chars/token vs a 10 240 window; arctic 12 800 chars ≈ 5 565 vs
4 096). The runner refused those chunks and the shrink ladder embedded a
leading sub-window — the tail was lost from the KG. Since v0.2.106 every
model-aware preset is clamped to ``chunking.dense_char_budget`` (the ONE dense
budget — ``vco_lib.embedding_service._char_budget_for_model(conservative=
True)``), and the packer sizes a chunk on its joined text.

What is pinned here, per registered model, over a dense corpus (JSON,
minified code, base64-ish, CJK, a markdown table, prose, and a run of 3-char
sentences that the old per-sentence floor sum counted as ZERO tokens):

* every chunk the KG plan (``kg_chunk_plan.plan_node_chunks`` — the one plan
  kg-sync and the MCP store both call) produces, and every code-entity text
  (``code_truncation.chunk_or_truncate_for_embedding``) for the code models,
  fits ``num_ctx`` tokens at the model's MEASURED-MINIMUM chars/token ratio
  (the ratio lives in ``vco_lib.embedding_service``);
* no content is dropped: the chunks carry every non-whitespace character of
  the input, in order.

RED-PROOF: against the pre-v0.2.106 ``chunking.py`` every model fails the
window assertion (qwen3 126%, arctic / openai / jina 136%, CodeSage 122%),
and the short-sentence corpus produces a single unbounded chunk.
"""

from __future__ import annotations

import json
import random
import re

import pytest

from claude_mcp_servers.weaviate_mcp import chunking, code_truncation
from claude_mcp_servers.weaviate_mcp.kg_chunk_plan import plan_node_chunks
from vco_lib.embedding_service import _min_chars_per_token_for

TEXT_MODELS = [
    "qwen3-embedding:0.6b",
    "snowflake-arctic-embed2:latest",
    "text-embedding-3-small",
]
CODE_MODELS = [
    "codesage/codesage-large-v2",
    "unclemusclez/jina-embeddings-v2-base-code:latest",
]
ALL_MODELS = TEXT_MODELS + CODE_MODELS


def _rng() -> random.Random:
    return random.Random(20261111)


def _json_corpus() -> str:
    rng = _rng()
    rows = [
        {
            "tool": "Edit",
            "file_path": f"/repo/src/mod_{i}/file_{rng.randint(0, 999)}.py",
            "old_string": "".join(rng.choice("abcdef0123456789{}[]:,") for _ in range(60)),
            "new_string": [rng.randint(0, 10**9) for _ in range(6)],
        }
        for i in range(900)
    ]
    # One minified line (no newline, no sentence boundary) + a pretty dump.
    return json.dumps(rows, separators=(",", ":")) + "\n\n" + json.dumps(rows[:200], indent=2)


def _minified_code() -> str:
    rng = _rng()
    parts = []
    for i in range(2500):
        parts.append(
            f"function f{i}(a,b){{return a*{rng.randint(1, 99)}+b[{i % 7}]"
            f"||g{i}(a,'{rng.randint(0, 9999)}')}};"
        )
    return "".join(parts)


def _base64ish() -> str:
    rng = _rng()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return "\n".join(
        "".join(rng.choice(alphabet) for _ in range(76)) for _ in range(1200)
    )


def _cjk() -> str:
    rng = _rng()
    para = []
    for _ in range(60):
        sentence = "".join(chr(rng.randint(0x4E00, 0x9FA5)) for _ in range(400))
        para.append(sentence + "。")
    return "\n\n".join(para)


def _markdown_table() -> str:
    header = "| key | value | note |\n|---|---|---|\n"
    rows = "".join(f"| k{i} | {i * 7919 % 10007} | ✓ |\n" for i in range(3000))
    return header + rows


def _prose() -> str:
    sentence = (
        "The orchestrator chunks every knowledge node before it is embedded. "
        "Chunks must fit the window of the model that embeds them! "
        "Dense content tokenises far tighter than prose does. "
    )
    return "\n\n".join(f"## Part {i}\n\n" + sentence * 12 for i in range(120))


def _short_sentences() -> str:
    # 3-char sentences: under the pre-v0.2.106 per-sentence floor sum each one
    # counted 0 tokens, so the packer never closed a chunk.
    return " ".join("Ab." for _ in range(40_000))


CORPORA = {
    "json": _json_corpus,
    "minified_code": _minified_code,
    "base64ish": _base64ish,
    "cjk": _cjk,
    "markdown_table": _markdown_table,
    "prose": _prose,
    "short_sentences": _short_sentences,
}


def _window_tokens(text: str, model: str) -> float:
    """Tokens ``text`` occupies at the model's measured-minimum ratio."""
    return len(text) / _min_chars_per_token_for(model, conservative=True)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


@pytest.mark.parametrize("model", ALL_MODELS)
def test_dense_budget_is_the_one_shared_bound(model):
    # The presets, the code-entity budget and the answer-window mode all read
    # ONE number per model; it fits the window at the measured minimum.
    budget = chunking.dense_char_budget(model)
    num_ctx = chunking._num_ctx_for_model(model)
    assert num_ctx and budget > 0
    assert budget == chunking.answer_chunk_budget(model)
    assert chunking.chunking_preset_for_model(model)[1] <= budget // chunking.CHARS_PER_TOKEN_TEXT
    assert _window_tokens("x" * budget, model) <= num_ctx


def test_unregistered_model_has_no_dense_budget():
    assert chunking.dense_char_budget("not-a-registered-model") == 0
    # …and keeps the conservative unknown-model tier.
    assert chunking.chunking_preset_for_model("not-a-registered-model") == (
        chunking.CHUNKING_PRESETS["small_context"]
    )


@pytest.mark.parametrize("corpus", sorted(CORPORA))
@pytest.mark.parametrize("model", ALL_MODELS)
def test_kg_plan_chunks_fit_the_window_and_keep_every_character(model, corpus):
    text = CORPORA[corpus]()
    num_ctx = chunking._num_ctx_for_model(model)
    assert num_ctx is not None
    plan = plan_node_chunks(text, model, source_id="dense")
    pieces = [text] if plan.is_single else [c.content for c in plan.chunks]
    assert pieces, "the plan produced nothing"
    worst = max(_window_tokens(p, model) for p in pieces)
    assert worst <= num_ctx, (
        f"{model}/{corpus}: a chunk needs {worst:.0f} tokens at the measured "
        f"minimum ratio, window is {num_ctx}"
    )
    budget = chunking.dense_char_budget(model)
    # Counter units floor, so a chunk may sit up to CHARS_PER_TOKEN_TEXT - 1
    # chars past ``units * 4``; it never passes the budget by more than that.
    assert max(len(p) for p in pieces) < budget + chunking.CHARS_PER_TOKEN_TEXT
    assert _squash("".join(pieces)) == _squash(text), f"{model}/{corpus}: content dropped"


@pytest.mark.parametrize("corpus", ["json", "minified_code", "base64ish", "prose"])
@pytest.mark.parametrize("model", CODE_MODELS)
def test_code_entity_texts_fit_the_window(model, corpus):
    body = "def big_entity(x):\n" + CORPORA[corpus]()[:60_000]
    texts = code_truncation.chunk_or_truncate_for_embedding(
        "def big_entity(x)", body, language="python", model=model,
        full_name="m.big_entity",
    )
    num_ctx = chunking._num_ctx_for_model(model)
    assert num_ctx is not None
    for text in texts:
        # The ``[chunk i/N]`` header rides on top of the chunk; it is part of
        # what the embedder receives, so it is measured too.
        assert _window_tokens(text, model) <= num_ctx, (
            f"{model}/{corpus}: {len(text)} chars ≈ "
            f"{_window_tokens(text, model):.0f} tokens > {num_ctx}"
        )


@pytest.mark.parametrize("model", CODE_MODELS)
def test_single_entity_budget_is_the_dense_bound(model):
    assert code_truncation._max_chars_for_model(model) == chunking.dense_char_budget(model)


def test_short_sentences_are_bounded_by_the_joined_length():
    chunker = chunking.Chunker.for_model("qwen3-embedding:0.6b")
    chunks = chunker.chunk_text(_short_sentences(), source_id="s")
    assert len(chunks) > 1
    limit = chunker.max_tokens * chunking.CHARS_PER_TOKEN_TEXT + chunking.CHARS_PER_TOKEN_TEXT
    assert all(len(c.content) < limit for c in chunks)
