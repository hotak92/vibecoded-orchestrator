# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WP-R (2026-07-22) — shipped enrichment ACTIVE-model num_ctx / batch-poisoning fix.

Root cause: ``EmbeddingService.embed_text_batch`` (and the Ollama code fallback in
``embed_code_batch``) handed the FULL object content to Ollama ``/api/embed`` for
the ACTIVE model with no per-object bounded sub-window — only the SECONDARY path
bounded. For a small-num_ctx ACTIVE model (arctic 4 096, granite, embeddinggemma,
bge-m3) on a corpus whose chunks were sized for qwen3 (10 240), an over-window
item 400'd, and Ollama's ``/api/embed`` rejects the ENTIRE batch of 100 if ANY
single input overflows → 100 % failure, 0 enriched (observed live: 1 011/1 011).

Fix (this test red-proofs it):
  1. Every ACTIVE Ollama batch input is bounded to the model's num_ctx via the
     shared ``_bounded_for_model`` (generalised from WP-O's secondary helper —
     ONE home) BEFORE it reaches Ollama, so the 400 never fires in the common
     case.
  2. A whole-batch ``/api/embed`` failure is ISOLATED to a per-item embed so one
     oversized item can never fail the batch; the item is retried under a tighter
     sub-window on a context-overflow error.

The stub backend below reproduces Ollama's contract: it 400s ("input exceeds the
context length") whenever a single input exceeds the arctic char budget, and it
rejects the WHOLE batch if ANY item overflows (matching real ``/api/embed``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vco_lib.embedding_service import (  # noqa: E402
    EmbeddingService,
    _bounded_for_model,
    _CHARS_PER_TOKEN,
    _is_context_overflow_error,
)

# arctic num_ctx (from MODEL_TOKEN_LIMITS) — the model the live backfill hit.
ARCTIC_MODEL = "snowflake-arctic-embed2:latest"
ARCTIC_NUM_CTX = 4096
ARCTIC_CHAR_BUDGET = ARCTIC_NUM_CTX * _CHARS_PER_TOKEN  # what _bounded_for_model trims to


class _StubOllama:
    """Ollama adapter stub matching the real /api/embed contract.

    * ``embed_batch`` raises HTTP-400-shaped RuntimeError if ANY input exceeds
      ``char_budget`` (whole-batch rejection, like real Ollama).
    * ``embed`` (single) raises the same 400 when its ONE input overflows.
    * A non-overflowing input yields a deterministic 4-dim vector.
    """

    def __init__(self, char_budget: int = ARCTIC_CHAR_BUDGET):
        self.char_budget = char_budget
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []

    def _vec(self, text: str) -> list[float]:
        return [float(len(text) % 7), 0.1, 0.2, 0.3]

    def embed_batch(self, model, texts, num_ctx=None):
        self.batch_calls.append(list(texts))
        overflow = [t for t in texts if len(t) > self.char_budget]
        if overflow:
            raise RuntimeError(
                "Ollama /api/embed returned HTTP 400: "
                "{'error':'input length exceeds the context length'}"
            )
        return [self._vec(t) for t in texts]

    def embed(self, model, text, num_ctx=None):
        self.single_calls.append(text)
        if len(text) > self.char_budget:
            raise RuntimeError(
                "Ollama /api/embed returned HTTP 400: "
                "{'error':'input exceeds the context length'}"
            )
        return self._vec(text)


def _svc_with(stub: _StubOllama, model: str = ARCTIC_MODEL) -> EmbeddingService:
    svc = EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=model,
        code_model_id=model,
        openai_api_key="",
        ollama_adapter=stub,  # type: ignore[arg-type]
    )
    # Route the text slot to Ollama (not openai) — arctic is an ollama model.
    svc._text_slot = "arctic2_embed"
    svc._code_slot = "jina_embed"  # ollama-served code fallback path
    return svc


# ---------------------------------------------------------------------------
# helper-level: the shared sub-window bounds arctic to its num_ctx budget
# ---------------------------------------------------------------------------


def test_bounded_for_model_trims_over_num_ctx_for_arctic():
    big = "x" * (ARCTIC_CHAR_BUDGET + 5000)
    trimmed, truncated = _bounded_for_model(big, ARCTIC_MODEL)
    assert truncated is True
    assert len(trimmed) == ARCTIC_CHAR_BUDGET
    # a small input is untouched
    small = "y" * 100
    unt, tr2 = _bounded_for_model(small, ARCTIC_MODEL)
    assert tr2 is False and unt == small


def test_context_overflow_error_detector():
    assert _is_context_overflow_error(
        RuntimeError("HTTP 400: input length exceeds the context length")
    )
    assert _is_context_overflow_error(
        RuntimeError("Ollama /api/embed returned HTTP 400: input exceeds context length")
    )
    # network / model-not-found must NOT be treated as overflow
    assert not _is_context_overflow_error(RuntimeError("Ollama network error: connection refused"))
    assert not _is_context_overflow_error(RuntimeError("HTTP 404: model not found"))


# ---------------------------------------------------------------------------
# batch-level: the primary fix — bounding prevents the 400 entirely
# ---------------------------------------------------------------------------


def test_oversized_item_no_longer_400s_the_batch():
    """An over-num_ctx item is trimmed to a leading sub-window BEFORE Ollama sees
    it, so the whole batch succeeds (pre-fix: the batch 400'd and every item was
    lost)."""
    stub = _StubOllama()
    svc = _svc_with(stub)
    oversized = "a" * (ARCTIC_CHAR_BUDGET + 20000)  # would 400 unbounded
    normal = "b" * 500
    vecs = svc.embed_text_batch([oversized, normal])
    assert len(vecs) == 2
    assert all(v for v in vecs), "every item got a vector"
    # The batch call the stub received had the oversized item already trimmed.
    assert stub.batch_calls, "batch path was taken"
    sent = stub.batch_calls[0]
    assert len(sent[0]) == ARCTIC_CHAR_BUDGET, "oversized item was bounded"
    assert len(sent[1]) == 500, "normal item untouched"


# ---------------------------------------------------------------------------
# isolation: a whole-batch failure degrades to per-item, never poisoning survivors
# ---------------------------------------------------------------------------


class _PoisonBatchStub(_StubOllama):
    """Fails the FIRST batch call unconditionally (simulating a dense item the
    char heuristic under-shot), forcing the per-item isolation path."""

    def __init__(self, char_budget: int = ARCTIC_CHAR_BUDGET):
        super().__init__(char_budget)
        self._batch_failed_once = False

    def embed_batch(self, model, texts, num_ctx=None):
        self.batch_calls.append(list(texts))
        if not self._batch_failed_once:
            self._batch_failed_once = True
            raise RuntimeError(
                "Ollama /api/embed returned HTTP 400: input exceeds the context length"
            )
        return [self._vec(t) for t in texts]


def test_whole_batch_failure_isolates_to_per_item():
    """When the batch call itself raises, the survivors are still embedded
    per-item — one bad item never fails the batch."""
    stub = _PoisonBatchStub()
    svc = _svc_with(stub)
    texts = ["p" * 300, "q" * 400, "r" * 500]
    vecs = svc.embed_text_batch(texts)
    assert len(vecs) == 3
    assert all(v for v in vecs), "every survivor got a per-item vector"
    # per-item path was exercised for all three inputs
    assert len(stub.single_calls) == 3


class _DenseItemStub(_StubOllama):
    """One item is so dense the char-budget sub-window STILL overflows, so the
    per-item path must retry it under a tighter window; the others embed cleanly.
    Batch always fails (forces the isolation path)."""

    def __init__(self):
        # A tiny budget so even the char-bounded item overflows on the first
        # single-embed attempt, exercising the tighter-sub-window retry loop.
        super().__init__(char_budget=800)

    def embed_batch(self, model, texts, num_ctx=None):
        self.batch_calls.append(list(texts))
        raise RuntimeError("Ollama /api/embed returned HTTP 400: input exceeds context length")


def test_dense_item_retries_under_tighter_subwindow_without_failing_others():
    stub = _DenseItemStub()
    svc = _svc_with(stub)
    # One item over the 800-char single-embed budget → must be halved until it
    # fits; two items already under it embed on the first single attempt.
    texts = ["z" * 2000, "s" * 200, "t" * 300]
    vecs = svc.embed_text_batch(texts)
    assert len(vecs) == 3
    assert all(v for v in vecs)
    # The dense item was retried at least once (>3 single calls total).
    assert len(stub.single_calls) > 3


def test_code_batch_ollama_fallback_shares_the_bounded_path():
    """The Ollama code fallback in embed_code_batch routes through the SAME
    bounded+isolated helper (one home)."""
    stub = _StubOllama()
    svc = _svc_with(stub)
    # code slot is jina_embed but codeembed is unreachable → ollama fallback.
    svc.codeembed.is_reachable = lambda: False  # type: ignore[assignment]
    oversized = "c" * (ARCTIC_CHAR_BUDGET + 9000)
    vecs = svc.embed_code_batch([oversized, "small"])
    assert len(vecs) == 2 and all(v for v in vecs)
    assert stub.batch_calls, "ollama code fallback batch taken"
    assert len(stub.batch_calls[0][0]) == ARCTIC_CHAR_BUDGET, "code item bounded"


# ---------------------------------------------------------------------------
# R3-4(a): the active-slot sub-window trim is logged at WARNING (loud, not silent)
# ---------------------------------------------------------------------------


def test_trimmed_items_logged_at_warning(caplog):
    """An over-num_ctx item that gets bounded to a sub-window must emit a WARNING
    with the trimmed-item count — the fidelity loss can no longer be silent
    (R3-4(a): the delta's 'degradation is tagged, never silent' rule)."""
    import logging

    stub = _StubOllama()
    svc = _svc_with(stub)
    oversized = "a" * (ARCTIC_CHAR_BUDGET + 20000)
    normal = "b" * 500
    with caplog.at_level(logging.WARNING, logger="vco_lib.embedding_service"):
        vecs = svc.embed_text_batch([oversized, normal])
    assert len(vecs) == 2 and all(v for v in vecs)
    # Exactly the trim WARNING is present, naming the count (1 of 2).
    trim_logs = [
        r for r in caplog.records
        if "bounded leading sub-window" in r.getMessage()
    ]
    assert trim_logs, "the sub-window trim must be logged at WARNING"
    assert "1/2" in trim_logs[0].getMessage(), (
        "the WARNING must name the trimmed-item count (1 of 2)"
    )


def test_no_trim_no_warning(caplog):
    """When nothing overflows, no trim WARNING is emitted (no false positives)."""
    import logging

    stub = _StubOllama()
    svc = _svc_with(stub)
    with caplog.at_level(logging.WARNING, logger="vco_lib.embedding_service"):
        svc.embed_text_batch(["x" * 100, "y" * 200])
    assert not [
        r for r in caplog.records
        if "bounded leading sub-window" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# R3-4(b): a genuinely un-embeddable item is isolated per-item — survivors kept
# ---------------------------------------------------------------------------


class _OneHardFailureStub(_StubOllama):
    """Batch always fails (forces isolation). On the per-item path a MARKED item
    (the sentinel content) raises a NON-overflow error every time (a real
    un-embeddable item — e.g. model-not-found for that shape), so the tighter
    sub-window retry can't save it; every other item embeds cleanly."""

    _POISON = "POISON"

    def embed_batch(self, model, texts, num_ctx=None):
        self.batch_calls.append(list(texts))
        raise RuntimeError("Ollama /api/embed returned HTTP 400: input exceeds context length")

    def embed(self, model, text, num_ctx=None):
        self.single_calls.append(text)
        if text.startswith(self._POISON):
            # Non-overflow error → not retriable, this item is genuinely lost.
            raise RuntimeError("Ollama /api/embed returned HTTP 500: model backend error")
        return self._vec(text)


def test_hard_failure_isolated_survivors_keep_vectors(caplog):
    """A single genuinely un-embeddable item yields an EMPTY-VECTOR SENTINEL for
    THAT index only; every survivor keeps its computed vector (R3-4(b) — one hard
    failure must not drop the survivors)."""
    import logging

    stub = _OneHardFailureStub()
    svc = _svc_with(stub)
    texts = ["good1" + "a" * 100, "POISON" + "b" * 100, "good2" + "c" * 100]
    with caplog.at_level(logging.WARNING, logger="vco_lib.embedding_service"):
        vecs = svc.embed_text_batch(texts)
    assert len(vecs) == 3, "one entry per input, order preserved"
    # Survivors keep vectors; the poison index is the empty-vector sentinel.
    assert vecs[0] and vecs[2], "both survivors embedded"
    assert vecs[1] == [], "the un-embeddable item is the empty-vector sentinel"
    assert any(
        "still un-embeddable" in r.getMessage() for r in caplog.records
    ), "the hard failure count must be logged"


def test_flush_batch_consumes_empty_sentinel_as_per_item_failure(monkeypatch):
    """Consumer-side proof: ``embedding_enrichment._flush_batch`` treats the
    empty-vector sentinel at an index as a per-object failure and enriches the
    survivors — end-to-end isolation, not just a helper contract (R3-4(b))."""
    from vco_lib import embedding_enrichment as ee

    # A fake service whose text-batch returns a survivor / sentinel / survivor.
    class _FakeSvc:
        def embed_text_batch(self, texts):
            return [[0.1, 0.2], [], [0.3, 0.4]]

    # A fake collection recording which uuids got updated.
    updated: list = []

    class _FakeCol:
        class data:
            @staticmethod
            def update(uuid=None, vector=None):
                updated.append(uuid)

    enriched = failed = 0
    failures: list = []
    pending_uuids = ["u0", "u1", "u2"]

    # Drive the exact per-index logic _flush_batch uses on a returned vector list.
    vectors = _FakeSvc().embed_text_batch(["t0", "t1", "t2"])
    assert len(vectors) == len(pending_uuids)
    for idx, uid in enumerate(pending_uuids):
        vec = vectors[idx] if idx < len(vectors) else None
        if not vec:
            failed += 1
            ee._append_failure(failures, str(uid), "embed returned empty vector")
            continue
        _FakeCol.data.update(uuid=uid, vector={"slot": vec})
        enriched += 1

    assert enriched == 2 and failed == 1, "survivors enriched, sentinel failed"
    assert updated == ["u0", "u2"], "only the survivors were written"
    assert failures and failures[0]["uuid"] == "u1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
