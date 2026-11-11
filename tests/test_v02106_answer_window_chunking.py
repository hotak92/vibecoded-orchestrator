# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the RL citation answer window is chunked to fit and to share.

Field report (a Windows user, CPU-only snowflake-arctic-embed2): an embedding
cycle about every 30 min while working, each leaving a ``shrink_summary`` row
of 90-190k characters in ``embedding_failures.jsonl``. Root cause: the
Stop-hook citation drain (``rl_drain_citations`` → ``citation_compute``)
embeds a ~100 000-char answer window per staged retrieval, chunked with the KG
DOCUMENT chunker:

* its chunks (up to 12 800 chars for arctic, 32 768 for qwen3) exceed the
  model window on dense transcript text, so each one was refused and shrunk;
* its boundaries depend on where the window starts, so the overlapping
  windows of one chat never produced the same chunk and the per-process embed
  memo never hit — the same text was embedded once per citation.

RED-PROOF: on the pre-fix ``citation_compute`` (``Chunker.for_model``)
``test_every_embedded_chunk_fits_the_dense_budget`` fails (chunks of ~12.8k
chars against a 7 065-char budget) and
``test_overlapping_windows_embed_mostly_shared_chunks`` fails (several times
the union of the windows is embedded).
"""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from claude_mcp_servers.rl_client import answer_chunking, citation_compute
from claude_mcp_servers.rl_client.answer_chunking import (
    answer_chunk_budget,
    chunk_answer_window,
)

ARCTIC = "snowflake-arctic-embed2"
QWEN = "qwen3-embedding:0.6b"


def _transcript_paragraphs(n: int = 400, seed: int = 7) -> list[str]:
    """Transcript-shaped text: prose, tool_use JSON, blank-line separated
    paragraphs and a few run-on blocks with no separator."""
    rnd = random.Random(seed)
    words = ("the a function returns value config path error test file hook "
             "embed model window chunk").split()
    paras = []
    for _ in range(n):
        text = " ".join(rnd.choice(words) for _ in range(rnd.choice([5, 20, 60, 150, 400])))
        if rnd.random() < 0.3:
            text = 'Edit: {"file_path": "/x/y.py", "old_string": "%s"}' % text
        paras.append(text + ("\n\n" if rnd.random() < 0.7 else ""))
    return paras


class _MemoSvc:
    """Fake EmbeddingService with the real service's per-process memo
    semantics: a repeated text is a hit, only new texts reach the backend."""

    def __init__(self):
        self.memo: dict[str, list[float]] = {}
        self.backend_texts: list[str] = []

    def _embed(self, text: str) -> list[float]:
        if text not in self.memo:
            self.backend_texts.append(text)
            self.memo[text] = [1.0, float(len(text) % 7)]
        return self.memo[text]

    embed_text = _embed
    embed_code = _embed


class _Writer:
    def __init__(self):
        self.calls: list[dict] = []

    def log_citations(self, **kwargs):
        self.calls.append(kwargs)


def _run(answer: str, svc, model: str = ARCTIC, ctx_extra: "dict | None" = None,
         task_id: str = "t"):
    ctx = {"active_model": model, "nodes": [{"title": "Node", "n_emb": [1.0, 0.0]}]}
    ctx.update(ctx_extra or {})
    with patch("claude_mcp_servers.weaviate_mcp.server._get_embedding_service",
               return_value=svc), \
         patch("claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer",
               return_value=_Writer()), \
         patch("claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer_for",
               return_value=_Writer()):
        return citation_compute.compute_citation(task_id, answer, ctx, write=True)


# ── the chunker itself ──────────────────────────────────────────────────


@pytest.mark.parametrize("model", [ARCTIC, QWEN, "codesage-large-v2"])
def test_chunks_fit_the_budget_and_reassemble(model):
    text = "".join(_transcript_paragraphs())
    chunks = chunk_answer_window(text, model)
    budget = answer_chunk_budget(model)
    assert chunks
    assert max(len(c) for c in chunks) <= budget
    # Nothing dropped: the chunks (only whitespace-only ones are omitted)
    # reassemble the window.
    assert "".join(chunks).replace(" ", "").replace("\n", "") == \
        text.replace(" ", "").replace("\n", "")


def test_budget_is_the_dense_bound_of_the_model_window():
    from vco_lib.embedding_service import _char_budget_for_model

    assert answer_chunk_budget(ARCTIC) == _char_budget_for_model(ARCTIC, conservative=True)
    # An unregistered model never gets a generous guess.
    assert answer_chunk_budget("no-such-model") == answer_chunking._FALLBACK_BUDGET_CHARS


def test_a_paragraph_longer_than_the_budget_is_split_from_its_own_start():
    budget = answer_chunk_budget(ARCTIC)
    para = "x" * (budget * 2 + 10)
    chunks = chunk_answer_window(para, ARCTIC)
    assert [len(c) for c in chunks] == [budget, budget, 10]


def test_boundaries_do_not_depend_on_where_the_window_starts():
    paras = _transcript_paragraphs()
    a = chunk_answer_window("".join(paras[0:]), ARCTIC)
    b = chunk_answer_window("".join(paras[13:]), ARCTIC)
    # After the first shared cut, every chunk of the later window is a chunk
    # of the earlier one.
    shared = [c for c in b if c in set(a)]
    assert len(shared) >= len(b) - 2


def test_empty_and_whitespace_windows_have_no_chunks():
    assert chunk_answer_window("", ARCTIC) == []
    assert chunk_answer_window("\n\n   \n\n", ARCTIC) == []


# ── wired through citation_compute (the behaviour that changed) ──────────


@pytest.mark.parametrize("model", [ARCTIC, QWEN])
def test_every_embedded_chunk_fits_the_dense_budget(model):
    svc = _MemoSvc()
    answer = "".join(_transcript_paragraphs())[:100_000]
    assert _run(answer, svc, model=model) is not None
    assert svc.backend_texts
    assert max(len(t) for t in svc.backend_texts) <= answer_chunk_budget(model)


def test_overlapping_windows_embed_mostly_shared_chunks():
    """Forty retrievals of one chat, windows starting a few paragraphs apart
    (the drain's real shape): the backend sees little more than the union of
    the windows, not forty copies of it."""
    paras = _transcript_paragraphs()
    svc = _MemoSvc()
    starts = list(range(0, 200, 5))
    windows = ["".join(paras[k:])[:100_000] for k in starts]
    for i, w in enumerate(windows):
        assert _run(w, svc, task_id=f"t{i}") is not None
    union_chars = len("".join(paras[starts[0]:])[: 100_000 + sum(
        len(p) for p in paras[: starts[-1]])])
    embedded = sum(len(t) for t in svc.backend_texts)
    # What is genuinely new per window: its leading chunk (up to the first
    # shared cut) and its trailing, cap-truncated one — at most two budgets.
    allowance = 2 * answer_chunk_budget(ARCTIC) * len(windows)
    assert embedded <= union_chars + allowance, (embedded, union_chars, allowance)


def test_other_slot_chunks_are_embedded_once_per_process():
    citation_compute._OTHER_SLOT_MEMO.clear()
    calls: list[str] = []

    def _other(svc, text, source, model=""):
        calls.append(text)
        return [0.5, 0.5]

    answer = "".join(_transcript_paragraphs())[:50_000]
    extra = {
        "dual_log": True,
        "other_nodes": [{"title": "Node", "n_emb": [0.5, 0.5]}],
        "other_embedding_source": "qwen3",
        "other_embedding_model": QWEN,
        "other_embedding_dim": 2,
    }
    with patch("claude_mcp_servers.weaviate_mcp.server._embed_text_in_other_model",
               side_effect=_other):
        _run(answer, _MemoSvc(), ctx_extra=extra, task_id="a")
        first = len(calls)
        _run(answer, _MemoSvc(), ctx_extra=extra, task_id="b")
    assert first > 0
    assert len(calls) == first  # the second citation hit the memo
    citation_compute._OTHER_SLOT_MEMO.clear()
