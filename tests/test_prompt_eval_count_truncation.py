# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Defect 3 (v0.2.92) — what Ollama's ``prompt_eval_count`` can and cannot
say about truncation; the char-ratio bound is the estimate everywhere else.

CORRECTED round-3/4. The original premise was that Ollama pins the count at
``num_ctx`` when it truncates, making ``count >= window`` an exact detector.
Only half of that survives measurement: a pinned count means the window was
FILLED, and an exact fit produces the identical number, so the positive
direction is ambiguous. Worse, since every embed now sends ``truncate:
false``, an over-window input is REFUSED rather than returning 200 — the
pinned case is almost always an exact fit, and tagging it flagged the inputs
that used the window best. What remains sound is the NEGATIVE direction:
``count < window`` proves the runner saw the text whole. Truncation itself is
established locally, by the caller's own bounding and by the refusal-retry
path, never inferred from this number.

``OllamaAdapter.embed`` discards the payload, so the capability lives in the
``TruncationAwareOllamaAdapter`` subclass (NEW file — ``ollama.py`` is
outside this change's file set): it reuses the inherited ``embed`` verbatim
(no second copy of the HTTP ladder) and recovers the count through a
delegating session proxy that records counts KEYED BY (endpoint, input text).
Keyed, not a "last response" slot: ``bounded_post`` runs the actual POST on a
shared executor WORKER thread, so the record lands on a different thread than
the caller — and the service shares one session across its pool, so a single
last-response slot would let one thread's embed clobber another's between the
POST and the count read.

Service level: the secondary-slot fan-out ORs the SOUND half of the verdict
with the ratio verdict — a text the bound already sub-windowed STAYS truncated
even when the (smaller) input fit. It does NOT flag a text the ratio let
through just because the runner pinned the count: that number is ambiguous
(exact fit vs truncation), so the detector returns None there and the ratio
verdict stands. Adapters without the capability (injected stubs, plain adapter)
keep the estimate-only behaviour.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.embedding_service import (  # noqa: E402
    ARCTIC_SECONDARY_MODEL,
    DEFAULT_TEXT_MODEL,
    EmbeddingService,
    _char_budget_for_model,
)
from vco_lib.embedding_providers.ollama_truncation import (  # noqa: E402
    TruncationAwareOllamaAdapter,
)

ARCTIC_NUM_CTX = 4096
QWEN3_NUM_CTX = 10240
# PRIMARY-role budget (the default): num_ctx 4096 × median chars/token 4.166
# × 0.75 margin, floored at arctic's own chunker max = 12 800. The SECONDARY
# attempt tier is smaller (9 815) and its fallback smaller still (7 065) —
# see `_char_budget_for_model(..., full_coverage=False)` / `conservative=True`.
ARCTIC_BUDGET = _char_budget_for_model(ARCTIC_SECONDARY_MODEL)  # 12800


# ---------------------------------------------------------------------------
# Fake session satisfying BOTH the adapter ladder and bounded_post
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = dict(payload or {})
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeSession:
    """``requests.Session`` stand-in. ``post(url, timeout=..., json=...)`` is
    the exact shape ``bounded_post`` issues (on its executor worker); ``get``
    serves the /api/tags reachability probe."""

    def __init__(self, handler):
        self._handler = handler  # fn(url, body) -> _FakeResponse
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, timeout=None, json=None, **kw):
        self.posts.append((url, json))
        return self._handler(url, json)

    def get(self, url, timeout=None, **kw):
        return _FakeResponse(200, {"models": []})


def _counts_handler(counts: dict, *, include_count=True):
    """Modern /api/embed 200s; 404 elsewhere (so a legacy test drives the
    /api/embeddings fallback explicitly). ``counts`` is keyed by EITHER the
    model id or the input text (whichever the test drives); the response
    carries one embedding per input so the batch ladder accepts it."""

    def handler(url, body):
        if url.endswith("/api/embed"):
            inp = body.get("input")
            n = len(inp) if isinstance(inp, list) else 1
            payload = {"embeddings": [[0.1, 0.2, 0.3] for _ in range(n)]}
            if include_count:
                count = counts.get(body.get("model"))
                if count is None and isinstance(inp, str):
                    count = counts.get(inp)
                if count is not None:
                    payload["prompt_eval_count"] = count
            return _FakeResponse(200, payload)
        return _FakeResponse(404, {"error": "not found"})

    return handler


def _adapter(handler) -> tuple[TruncationAwareOllamaAdapter, _FakeSession]:
    session = _FakeSession(handler)
    return (
        TruncationAwareOllamaAdapter(
            base_url="http://localhost:11435", session=session, timeout=5.0
        ),
        session,
    )


# ---------------------------------------------------------------------------
# Adapter level: embed_with_truncation verdicts
# ---------------------------------------------------------------------------


def test_count_pinned_at_window_is_AMBIGUOUS_not_truncated():
    """count == num_ctx means the window was FILLED — not that it overflowed.

    Corrected v0.2.92 round-3. An exact fit and a truncation produce the
    identical number, so reporting True here was a false positive on exactly
    the inputs that fit best. And under the shipped configuration it is almost
    always an exact fit: every embed sends ``truncate: false``, so an
    over-window input is REFUSED with HTTP 400 rather than returning 200 with
    a pinned count — meaning a 200 that reaches this point describes an input
    the runner accepted whole.

    None hands the caller its char-ratio estimate: the honest "could not
    determine" rather than a confident wrong answer.
    """
    adapter, _ = _adapter(_counts_handler({ARCTIC_SECONDARY_MODEL: ARCTIC_NUM_CTX}))
    vector, truncated = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "some text")
    assert vector == [0.1, 0.2, 0.3]
    assert truncated is None


def test_exact_false_when_count_below_window():
    adapter, _ = _adapter(_counts_handler({ARCTIC_SECONDARY_MODEL: 3900}))
    _, truncated = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "some text")
    assert truncated is False


def test_unknown_when_count_absent():
    """No prompt_eval_count on the response → None: the caller must fall back
    to its ratio estimate (never a guessed True/False)."""
    adapter, _ = _adapter(_counts_handler({}, include_count=False))
    _, truncated = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "some text")
    assert truncated is None


def test_window_sent_matches_model_token_limits():
    """The num_ctx COMPARED against is the num_ctx actually sent — auto-
    resolved from the same table the chunker uses."""
    for model, window in (
        (ARCTIC_SECONDARY_MODEL, ARCTIC_NUM_CTX),
        (DEFAULT_TEXT_MODEL, QWEN3_NUM_CTX),
    ):
        adapter, session = _adapter(_counts_handler({model: window}))
        adapter.embed_with_truncation(model, "t")
        sent = [b for url, b in session.posts if url.endswith("/api/embed")]
        assert sent and sent[0]["options"]["num_ctx"] == window


def test_legacy_fallback_endpoint_count_is_read():
    """Old Ollama (404 on /api/embed) falls back to /api/embeddings whose
    body keys the text as ``prompt``; a count on THAT response is used."""

    def handler(url, body):
        if url.endswith("/api/embed"):
            return _FakeResponse(404, {"error": "not found"})
        assert "prompt" in body, "legacy body keys the text as 'prompt'"
        return _FakeResponse(
            200, {"embedding": [0.4, 0.5], "prompt_eval_count": ARCTIC_NUM_CTX - 10}
        )

    adapter, _ = _adapter(handler)
    vector, truncated = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "legacy text")
    assert vector == [0.4, 0.5]
    # Below the window => demonstrably whole. This test is about the legacy
    # endpoint's count being READ at all; it uses an unambiguous count so it
    # does not depend on the pinned-count semantics.
    assert truncated is False


def test_counts_are_keyed_by_input_text_not_last_response():
    """Regression for the capture design: ``bounded_post`` runs the POST on an
    executor worker thread and the session is shared, so the capture must be
    KEYED by input text. Embed A (fits), then B (pinned), then A again — A's
    verdict must still come from A's count, not be clobbered by B's (a
    last-response-slot design returns True for the third call)."""
    counts = {"A-text": 100, "B-text": ARCTIC_NUM_CTX}
    adapter, _ = _adapter(_counts_handler(counts))
    _, t1 = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "A-text")
    _, t2 = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "B-text")
    _, t3 = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "A-text")
    # A resolves from A's OWN count both times; B's differing verdict proves
    # the map is keyed rather than a single last-response slot. B's count is
    # pinned at the window, which is AMBIGUOUS (None) since v0.2.92 round-3 —
    # this test pins the KEYING, not the pinned-count semantics.
    assert (t1, t3) == (False, False)
    assert t2 is None, "a pinned count is ambiguous, not truncated"


def test_batch_bodies_are_not_recorded_as_single_items():
    """``embed_batch`` sends ``input`` as a LIST with one aggregate count —
    it must not pollute the per-text count map."""
    adapter, session = _adapter(
        _counts_handler({ARCTIC_SECONDARY_MODEL: ARCTIC_NUM_CTX - 10})
    )
    adapter.embed_batch(ARCTIC_SECONDARY_MODEL, ["one", "two"])
    assert all(isinstance(b.get("input"), list) for _, b in session.posts)
    # A later single embed still resolves ITS OWN count.
    _, truncated = adapter.embed_with_truncation(ARCTIC_SECONDARY_MODEL, "one")
    assert truncated is False


# ---------------------------------------------------------------------------
# Service level: the secondary fan-out reports the EXACT verdict
# ---------------------------------------------------------------------------


class _PlainStubOllama:
    """A NON-truncation-aware adapter (the injected-stub / custom-adapter
    case): the service must keep the estimate-only behaviour for it."""

    def __init__(self):
        self.embed_calls: list[tuple[str, int]] = []

    def is_reachable(self) -> bool:
        return True

    def embed(self, model, text, num_ctx=None):
        self.embed_calls.append((model, len(text)))
        return [0.1, 0.2, 0.3, 0.4]

    def embed_batch(self, model, texts, num_ctx=None):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _service(monkeypatch, ollama_adapter) -> EmbeddingService:
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", DEFAULT_TEXT_MODEL)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
    svc = EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=DEFAULT_TEXT_MODEL,
        code_model_id=DEFAULT_TEXT_MODEL,
        openai_api_key="",
        ollama_adapter=ollama_adapter,
    )
    svc._text_slot = "qwen3_embed"
    return svc


def test_service_does_not_tag_from_a_pinned_count_alone(monkeypatch):
    """Acceptance 3 (corrected round-3): a pinned count is NOT evidence of
    truncation. A 6 000-char text fits the ratio budget (estimate says
    full-fidelity) and the fake runner pins the count at the window — the
    ambiguous case. The slot must come back UNTAGGED, because an exact fit
    and a truncation are indistinguishable at that number."""
    handler = _counts_handler(
        {DEFAULT_TEXT_MODEL: 500, ARCTIC_SECONDARY_MODEL: ARCTIC_NUM_CTX}
    )
    adapter, _ = _adapter(handler)
    svc = _service(monkeypatch, adapter)
    text = "q" * 6_000
    assert len(text) <= ARCTIC_BUDGET, "ratio estimate must say NOT truncated"
    slots, truncated = svc.embed_text_all_configured_tagged(text)
    assert "qwen3_embed" in slots and "arctic2_embed" in slots
    # Corrected v0.2.92 round-3: a count pinned at the window no longer tags
    # the slot. It is ambiguous (exact fit vs truncation), and under
    # ``truncate: false`` an over-window input is REFUSED rather than
    # returning 200 — so this response describes text the runner took whole.
    # Truncation is now established by the caller's own bounding and by the
    # refusal-retry path, not inferred from this number.
    assert truncated == [], (
        "a pinned count is not evidence of truncation; tagging on it marks "
        "exactly the inputs that fit the window best"
    )


def test_service_keeps_ratio_verdict_when_count_absent(monkeypatch):
    """Fallback leg: no count on the response → the ratio estimate stands
    (an over-budget text is still tagged)."""
    handler = _counts_handler({}, include_count=False)
    adapter, _ = _adapter(handler)
    svc = _service(monkeypatch, adapter)
    text = "r" * (ARCTIC_BUDGET + 2_000)
    slots, truncated = svc.embed_text_all_configured_tagged(text)
    assert "arctic2_embed" in slots
    assert truncated == ["arctic2_embed"]


def test_service_subwindowed_text_stays_truncated_when_exact_fits(monkeypatch):
    """OR semantics: the bound already sub-windowed the text (ratio True);
    the smaller input then FITS the window (count below it) — the slot must
    STAY truncated. A sub-window vector is partial-text no matter what the
    runner said about the bounded input."""
    handler = _counts_handler(
        {DEFAULT_TEXT_MODEL: 500, ARCTIC_SECONDARY_MODEL: 3000}
    )
    adapter, _ = _adapter(handler)
    svc = _service(monkeypatch, adapter)
    text = "s" * (ARCTIC_BUDGET + 2_000)
    slots, truncated = svc.embed_text_all_configured_tagged(text)
    assert truncated == ["arctic2_embed"]


def test_service_with_plain_adapter_keeps_estimate_only(monkeypatch):
    """An adapter without the capability (injected stub) keeps the
    estimate-only behaviour — the isinstance gate, not a getattr probe (a
    MagicMock would auto-answer a getattr probe and break stubs)."""
    stub = _PlainStubOllama()
    svc = _service(monkeypatch, stub)
    # Fits the budget → NOT truncated (estimate; no exact path exists).
    _, truncated_fit = svc.embed_text_all_configured_tagged("t" * 6_000)
    assert truncated_fit == []
    # Over the budget → truncated by the estimate alone.
    _, truncated_over = svc.embed_text_all_configured_tagged(
        "u" * (ARCTIC_BUDGET + 2_000)
    )
    assert truncated_over == ["arctic2_embed"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
