# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Chunking of an RL citation ANSWER WINDOW — the one home (v0.2.106).

Why the KG chunker is the wrong tool here
-----------------------------------------
``citation_compute`` embeds the answer window that follows every staged
retrieval: up to ``DEFAULT_ANSWER_THRESHOLD_TOKENS`` (25 000) token-equivalents
of the chat's text, thinking and ``tool_use`` inputs — roughly 100 000
characters. Until v0.2.105 it chunked that window with the KG document chunker
(``Chunker.for_model``), which has two properties that are right for KG nodes
and wrong for transcripts:

1. **Its budget assumes prose.** The KG chunker sizes chunks at 4 chars per
   token (``chunking.CHARS_PER_TOKEN_TEXT``) up to the preset maximum — 32 768
   chars for qwen3-embedding, 12 800 for snowflake-arctic-embed2. Transcript
   text is JSON tool inputs, code and paths, which tokenise far denser, so
   almost every maximal chunk overflowed the model window, was REFUSED
   (``truncate: false``) and went down the shrink ladder: a wasted embed
   round trip per chunk, the tail of every chunk never embedded, and one
   ``shrink_summary`` row per drain run in ``embedding_failures.jsonl``
   (field report: rows every ~30 min with 90-190k characters shrunk; this
   repo's own log: 47 shrinks of ~32 600-char answer chunks in one run).
2. **Its boundaries depend on where the window starts.** A chat stages a
   pending citation for EVERY retrieval (each hook injection is one), and
   their answer windows overlap almost entirely — the windows of retrievals
   five tool calls apart share ~95% of their text. A greedy packer started
   at a different offset never re-aligns, so every window produced chunks no
   other window had and the per-process embed memo
   (``EmbeddingService._embed_memo_text``) never hit: one drain run that
   computed 44 citations embedded ~44 x 100 000 characters of mostly the SAME
   text, on CPU-only machines minutes of saturated cores per run.

What this module does instead
-----------------------------
* **Budget = the model's dense-content bound**,
  :func:`vco_lib.embedding_service._char_budget_for_model` with
  ``conservative=True`` — the same measured-minimum chars/token figure the
  shrink ladder itself falls back to, so a chunk fits the window on the first
  attempt (the ladder stays as the net for denser-than-measured text).
* **Content-defined boundaries.** The window is split into paragraphs (text
  up to and including a blank line); a paragraph longer than the budget is
  cut into budget-sized pieces measured from ITS OWN start. Pieces are packed
  into a chunk and the chunk is closed after a piece whose content hash
  selects it as a cut point (or when the next piece would overflow the
  budget). A cut therefore depends only on the text around it, never on where
  the window began: two overlapping windows produce identical chunks from
  their first shared cut onwards, those chunks hit the embed memo, and only
  the window's leading and trailing chunks are new.

The hash is :func:`hashlib.blake2b` (stable across processes — Python's
``hash()`` is salted per process and would defeat a persistent comparison).
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "CUT_MODULUS",
    "MIN_CHUNK_FRACTION",
    "answer_chunk_budget",
    "chunk_answer_window",
]

#: A piece closes its chunk when ``blake2b(piece) % CUT_MODULUS == 0`` (and
#: the chunk has reached :data:`MIN_CHUNK_FRACTION` of the budget). 4 keeps
#: chunks a few paragraphs long on transcript text, so the max-over-chunks
#: cosine sees passages, not single lines.
CUT_MODULUS = 4

#: A content-defined cut is taken only once the open chunk holds at least this
#: fraction of the budget — without it a run of short paragraphs would yield a
#: stream of one-line chunks (more embed calls, noisier cosine maxima). The
#: floor is measured from the chunk's own start, so it does not re-introduce a
#: dependence on where the WINDOW started: once two windows share a cut, every
#: later decision is a function of the same text.
MIN_CHUNK_FRACTION = 0.25

# Paragraph = text up to and including a run of blank-line newlines. The
# lookbehind split keeps the separator on the left piece, so
# "".join(paragraphs) == the input byte for byte.
_PARAGRAPH_SPLIT = re.compile(r"(?<=\n\n)(?=[^\n])")

#: Fallback budget for a model with no registered window (chars). Matches the
#: smallest registered dense bound's order of magnitude rather than guessing
#: high: an over-estimate here is the defect this module exists to remove.
_FALLBACK_BUDGET_CHARS = 2000


def answer_chunk_budget(model_id: str) -> int:
    """Character budget for one answer chunk embedded by ``model_id``.

    The dense-content (measured-minimum chars/token) bound of the model's
    window, from the ONE table the shrink ladder uses. A model the table does
    not know gets :data:`_FALLBACK_BUDGET_CHARS`.
    """
    # vco_lib is part of every healthy install (citation_compute already
    # depends on the embedding service); a failed import is a broken install
    # and propagates — the caller's soft-fail turns it into "no citation".
    from vco_lib.embedding_service import _char_budget_for_model

    budget = _char_budget_for_model(model_id or "", conservative=True)
    return budget if budget > 0 else _FALLBACK_BUDGET_CHARS


def _is_cut(piece: str) -> bool:
    digest = hashlib.blake2b(piece.encode("utf-8", "surrogatepass"), digest_size=4)
    return int.from_bytes(digest.digest(), "big") % CUT_MODULUS == 0


def _pieces(text: str, budget: int) -> list[str]:
    """Paragraphs of ``text``, each at most ``budget`` chars (a longer one is
    cut at budget-sized offsets from its own start)."""
    out: list[str] = []
    for para in _PARAGRAPH_SPLIT.split(text):
        if not para:
            continue
        for start in range(0, len(para), budget):
            out.append(para[start:start + budget])
    return out


def chunk_answer_window(answer: str, model_id: str) -> list[str]:
    """Split ``answer`` into content-defined chunks that fit ``model_id``.

    Every returned chunk is at most :func:`answer_chunk_budget` characters and
    carries non-whitespace text; concatenating the chunks (whitespace-only
    ones included) reproduces ``answer``. Deterministic for a given input and
    budget, across processes.
    """
    if not answer:
        return []
    budget = answer_chunk_budget(model_id)
    min_len = max(1, int(budget * MIN_CHUNK_FRACTION))
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in _pieces(answer, budget):
        if size and size + len(piece) > budget:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
        if size >= min_len and _is_cut(piece):
            chunks.append("".join(current))
            current, size = [], 0
    if current:
        chunks.append("".join(current))
    return [c for c in chunks if c.strip()]
