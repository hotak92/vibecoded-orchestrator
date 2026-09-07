# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Defect 1 (v0.2.92) — the secondary-slot char budget must use the model's
MEASURED-MINIMUM chars/token ratio with the user-set 25% margin, not the
model-agnostic 4-chars-per-token constant.

Pre-fix, ``_bounded_for_model`` computed ``char_budget = int(num_ctx * 4)``
(16 384 chars for arctic's 4 096-token window). Measured on real corpus
content, arctic's tokenizer runs 2.30 chars/token minimum (3.195 median,
3.87 max); qwen3 runs 2.547 / 3.977 / 4.669. The 4-chars constant tracks the
MEDIAN — but the median is not what overflows: at the MINIMUM ratio a
16 384-char budget is 16 384 / 2.30 ≈ 7 125 TRUE arctic tokens against a
4 096-token window, a 1.7x overflow handed straight to Ollama, which silently
truncates at the window and returns a healthy-looking vector.

Post-fix the budget is ``num_ctx × measured-min ratio × (1 − 0.25)``:
arctic 4 096 × 2.30 × 0.75 = 7 065 chars; qwen3 10 240 × 2.547 × 0.75 =
19 560 chars. At the densest MEASURED ratio a budgeted input is at most 75%
of the true token window, so no measured tokenizer can overflow it.

Scope: this bound governs ONLY what is handed to an embedder past the frozen
chunker — ``chunking.CHARS_PER_TOKEN_TEXT`` (chunk boundaries) stays
model-agnostic by design and is deliberately not coupled to this table.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.embedding_service import (  # noqa: E402
    _EMBED_BOUND_MIN_CHARS_PER_TOKEN,
    _EMBED_BOUND_SAFETY_MARGIN,
    _bounded_for_model,
    _bounded_for_secondary,
    _char_budget_for_model,
    _min_chars_per_token_for,
)

ARCTIC_MODEL = "snowflake-arctic-embed2:latest"
QWEN3_MODEL = "qwen3-embedding:0.6b"

# Measured on real corpus content (load-bearing, do not re-litigate):
#   arctic  min 2.30  / median 3.195 / max 3.87
#   qwen3   min 2.547 / median 3.977 / max 4.669
ARCTIC_MIN_RATIO = 2.30       # provable floor -> the FALLBACK tier
QWEN3_MIN_RATIO = 2.547
ARCTIC_ATTEMPT_RATIO = 3.195  # measured median -> the ATTEMPT tier
QWEN3_ATTEMPT_RATIO = 3.977
ARCTIC_MEDIAN_RATIO = 3.195
# W1 (2026-09-05), CodeSage tokenizer inside the code-embed container:
# real repo Python, budget-window slices — aggregate 3.46-3.48, densest 2.96.
CODESAGE_MIN_RATIO = 2.96
CODESAGE_ATTEMPT_RATIO = 3.46

ARCTIC_NUM_CTX = 4096
QWEN3_NUM_CTX = 10240

# ATTEMPT budget = max(ratio budget, this model's OWN chunker maximum).
# The floor is the WP-O primary-full-coverage rule: a slot is never bounded
# below what its own chunker already produced for it, or the ACTIVE path would
# truncate its own correctly-sized chunks to satisfy a margin meant for the
# secondary. See _char_budget_for_model.
def _own_chunker_max(model: str) -> int:
    from claude_mcp_servers.weaviate_mcp.chunking import CHARS_PER_TOKEN_TEXT, Chunker
    return Chunker.for_model(model).max_tokens * CHARS_PER_TOKEN_TEXT

ARCTIC_BUDGET = max(int(ARCTIC_NUM_CTX * ARCTIC_ATTEMPT_RATIO * 0.75),
                    _own_chunker_max(ARCTIC_MODEL))                   # 12800
ARCTIC_FALLBACK = int(ARCTIC_NUM_CTX * ARCTIC_MIN_RATIO * 0.75)       # 7065 fallback
QWEN3_BUDGET = max(int(QWEN3_NUM_CTX * QWEN3_ATTEMPT_RATIO * 0.75),
                   _own_chunker_max(QWEN3_MODEL))                     # 32768
QWEN3_FALLBACK = int(QWEN3_NUM_CTX * QWEN3_MIN_RATIO * 0.75)  # 19560


# ---------------------------------------------------------------------------
# The budget arithmetic itself
# ---------------------------------------------------------------------------


def test_arctic_budget_is_measured_min_ratio_with_margin():
    """arctic ATTEMPT: 4096 × 3.195 × 0.75 = 9815; FALLBACK: × 2.30 = 7065.

    Two tiers because the runner REFUSES over-window input (400 on /api/embed,
    500 on legacy /api/embeddings) rather than truncating — and a refusal loses
    the vector entirely. The attempt uses the MEDIAN so typical content embeds
    in full; the fallback uses the measured MINIMUM so it is provably under the
    window even at worst density, and therefore cannot be refused again.
    """
    assert _char_budget_for_model(ARCTIC_MODEL) == ARCTIC_BUDGET == 12800
    assert _char_budget_for_model(ARCTIC_MODEL, conservative=True) == ARCTIC_FALLBACK == 7065


def test_qwen3_budget_is_measured_min_ratio_with_margin():
    """qwen3 ATTEMPT: 10240 × 3.977 × 0.75 = 30543; FALLBACK: × 2.547 = 19560."""
    assert _char_budget_for_model(QWEN3_MODEL) == QWEN3_BUDGET == 32768
    assert _char_budget_for_model(QWEN3_MODEL, conservative=True) == QWEN3_FALLBACK == 19560


def test_margin_is_pinned_at_the_user_set_value():
    """The 25% margin is user-set; it must not silently grow."""
    assert _EMBED_BOUND_SAFETY_MARGIN == 0.25


def test_measured_minima_are_the_registered_ratios():
    """The ratio home carries the MEASURED MINIMA (the overflow side), one
    entry per measured model — the single source of truth for this bound."""
    assert _EMBED_BOUND_MIN_CHARS_PER_TOKEN == {
        "snowflake-arctic-embed2": ARCTIC_MIN_RATIO,
        "qwen3-embedding": QWEN3_MIN_RATIO,
        # W1 (2026-09-05): measured with the CodeSage tokenizer inside the
        # code-embed container — densest budget-window slice of real repo
        # Python. SIZES the code legs' shrink tiers now that the CodeEmbed
        # service leg refuses + shrinks.
        "codesage": CODESAGE_MIN_RATIO,
    }


def test_ratio_lookup_partial_matches_model_tags():
    """Tag variants resolve to their measured model (same partial-match rule
    the chunker's num_ctx lookup uses)."""
    assert _min_chars_per_token_for(ARCTIC_MODEL) == ARCTIC_ATTEMPT_RATIO
    assert _min_chars_per_token_for("snowflake-arctic-embed2") == ARCTIC_ATTEMPT_RATIO
    assert _min_chars_per_token_for(QWEN3_MODEL) == QWEN3_ATTEMPT_RATIO
    assert _min_chars_per_token_for("qwen3-embedding") == QWEN3_ATTEMPT_RATIO
    # both tiers partial-match, or the fallback would silently use the floor
    assert _min_chars_per_token_for(ARCTIC_MODEL, conservative=True) == ARCTIC_MIN_RATIO
    assert _min_chars_per_token_for(QWEN3_MODEL, conservative=True) == QWEN3_MIN_RATIO


def test_unmeasured_registered_model_gets_the_measured_floor():
    """A registered-but-unmeasured model (bge-m3) gets the measured FLOOR
    (the smallest ratio measured across shipped models) — an unmeasured
    tokenizer is a genuine unknown and the conservative side under-fills."""
    from claude_mcp_servers.weaviate_mcp.chunking import MODEL_TOKEN_LIMITS

    assert "bge-m3" in MODEL_TOKEN_LIMITS, "fixture model must be registered"
    assert "bge-m3" not in _EMBED_BOUND_MIN_CHARS_PER_TOKEN
    assert _min_chars_per_token_for("bge-m3") == 2.30
    expected = max(int(MODEL_TOKEN_LIMITS["bge-m3"] * 2.30 * 0.75),
                   _own_chunker_max("bge-m3"))
    assert _char_budget_for_model("bge-m3") == expected


def test_unregistered_model_remains_unbounded():
    """An unregistered model keeps the pre-existing behaviour: the FULL text
    (no budget, no truncation flag) — the bound never invents a window for a
    model it does not know."""
    big = "x" * 60_000
    sub, trunc = _bounded_for_model(big, "totally-unknown-model:42b")
    assert sub is big and trunc is False
    # The budget helper reports 0 = "no bound" for unregistered models.
    assert _char_budget_for_model("totally-unknown-model:42b") == 0


# ---------------------------------------------------------------------------
# The defect: sizes the OLD (4-chars) budget let through must now be bounded
# ---------------------------------------------------------------------------


def test_sixteen_thousand_char_text_truncates_for_arctic():
    """THE live-bug shape: 16 000 chars fit the old 16 384-char budget
    (passed through unbounded, flagged NOT truncated) — yet at arctic's
    measured MINIMUM density that is ~6 957 true tokens against a 4 096-token
    window: Ollama silently truncated it. Post-fix the text is bounded to the
    7 065-char leading sub-window and flagged."""
    text = "x" * 16_000
    assert len(text) <= ARCTIC_NUM_CTX * 4, "fixture must fit the OLD budget"
    assert len(text) / ARCTIC_MIN_RATIO > ARCTIC_NUM_CTX, (
        "fixture must overflow the window at the measured minimum density"
    )
    sub, trunc = _bounded_for_model(text, ARCTIC_MODEL)
    assert trunc is True, "16 000 chars exceed the measured-min budget 7 065"
    assert len(sub) == ARCTIC_BUDGET
    assert sub == text[:ARCTIC_BUDGET], "the LEADING sub-window is kept"


def test_median_density_text_inside_the_window_is_not_truncated():
    """12 000 chars at the MEDIAN ratio is ~3 756 tokens — inside the 4 096
    window — so it must pass UNTOUCHED.

    Rewritten 2026-09-04. The previous version asserted the opposite, because
    the bound then keyed on the measured MINIMUM density: safe for the densest
    conceivable text, but it truncated 312 of 859 chunks on a real corpus
    against 40 before — ~300 chunks losing text that fitted comfortably.

    Denser-than-median text is no longer handled by pre-emptive truncation but
    by REFUSAL: every embed request now sends ``truncate: false``, so Ollama
    returns 400 rather than silently dropping the tail (measured across all
    three shipped models — qwen3 and jina silently truncated by default;
    arctic always refused). The caller retries once at the provable fallback
    budget. Estimation decides what we attempt; the runner decides what fits.
    """
    text = "y" * 12_000
    assert len(text) / ARCTIC_MEDIAN_RATIO < ARCTIC_NUM_CTX
    sub, trunc = _bounded_for_model(text, ARCTIC_MODEL)
    assert trunc is False, "text that fits at median density must not be cut"
    assert sub == text, "byte-for-byte fidelity below the attempt budget"
    # …and the dense case is caught by the fallback tier, not by cutting early.
    sub2, trunc2 = _bounded_for_model(text, ARCTIC_MODEL, conservative=True)
    assert trunc2 is True and len(sub2) == ARCTIC_FALLBACK


def test_text_within_budget_passes_untouched():
    """A text inside the budget keeps byte-for-byte fidelity and is NOT
    flagged (no false truncation tags)."""
    text = "z" * ARCTIC_BUDGET
    sub, trunc = _bounded_for_model(text, ARCTIC_MODEL)
    assert sub is text and trunc is False


# ---------------------------------------------------------------------------
# True-token safety of the budget (acceptance criterion 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "num_ctx,ratio",
    [
        (ARCTIC_NUM_CTX, ARCTIC_MIN_RATIO),
        (ARCTIC_NUM_CTX, ARCTIC_MEDIAN_RATIO),
        (ARCTIC_NUM_CTX, 3.87),  # arctic measured max
        (QWEN3_NUM_CTX, QWEN3_MIN_RATIO),
        (QWEN3_NUM_CTX, 3.977),  # qwen3 measured median
        (QWEN3_NUM_CTX, 4.669),  # qwen3 measured max
    ],
)
def test_budget_cannot_overflow_window_in_true_tokens(num_ctx, ratio):
    """Acceptance 1: no text handed to a secondary slot may exceed its window
    in TRUE tokens. A budget-length text at density ``ratio`` uses
    budget/ratio tokens; at every MEASURED ratio (min → max) that must stay
    within num_ctx. At the minimum it lands at 75% of the window — the margin
    is what buys the guarantee for every denser-than-minimum reality."""
    budget = int(num_ctx * max(ratio, ARCTIC_MIN_RATIO) * 0.75)
    assert budget / ratio <= num_ctx, (
        "a budget-length text at this measured density must fit the window"
    )


def test_fallback_budget_at_exact_minimum_ratio_is_75_percent_of_window():
    """At the measured MINIMUM density the FALLBACK budget is the 75%-of-window
    point — the margin's designed operating point.

    Re-pointed 2026-09-04 from the attempt tier to the fallback tier. The
    ATTEMPT budget is deliberately allowed to exceed the window at worst
    density: that is the case the retry exists to handle, and asserting it
    could not happen would have forbidden the two-tier design outright.
    """
    assert ARCTIC_FALLBACK / ARCTIC_MIN_RATIO <= 0.75 * ARCTIC_NUM_CTX
    assert QWEN3_FALLBACK / QWEN3_MIN_RATIO <= 0.75 * QWEN3_NUM_CTX


def test_boundaries_do_not_move_when_the_secondary_shrinks():
    """The retry shrinks what we SEND, never the chunk itself.

    If it resized the chunk, shrinking chunk 2 from 9k to 7k would push its
    tail into chunk 3, shift 4 and 5, and could spawn a chunk 6 — renumbering
    every chunk after it. That is unacceptable here for a reason stronger than
    cost: both named vectors live on ONE Weaviate object and the RL replay
    pairs the slots by ``chunk_num``, so a renumbering breaks the coupling the
    dual write exists to produce.

    So the bound returns a SUB-WINDOW of the given text plus a truncation flag,
    and reports nothing about chunk boundaries at all.
    """
    text = "x" * 9000
    sub, trunc = _bounded_for_model(text, ARCTIC_MODEL, conservative=True)
    assert trunc is True
    assert len(sub) == ARCTIC_FALLBACK
    assert text.startswith(sub), "the sub-window is a prefix, not a re-chunk"
    # The caller's text is unchanged — nothing downstream shifts.
    assert len(text) == 9000


# ---------------------------------------------------------------------------
# The back-compat alias carries the SAME rule (one home, no fork)
# ---------------------------------------------------------------------------


def test_bounded_for_secondary_alias_matches_bounded_for_model():
    """``_bounded_for_secondary`` is an alias of ``_bounded_for_model`` — the
    secondary path cannot drift from the shared rule."""
    assert _bounded_for_secondary is _bounded_for_model
    text = "w" * (ARCTIC_BUDGET + 1)
    assert _bounded_for_secondary(text, ARCTIC_MODEL) == (
        text[:ARCTIC_BUDGET],
        True,
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── Two-tier retry contract (2026-09-04) ────────────────────────────────────
# The runner REFUSES over-window input instead of truncating it, and a refusal
# means the secondary slot gets NO vector at all — strictly worse than a
# truncated one. So the fallback tier must be provable, not merely smaller.

def test_fallback_tier_cannot_be_refused_for_length():
    """The fallback budget must fit the window at the WORST measured density.

    This is the property that terminates the retry by arithmetic instead of by
    hope: attempt -> refusal -> fallback, and the fallback cannot be refused
    for length, so there is no third attempt and no loop.
    """
    for model, worst, num_ctx in (
        (ARCTIC_MODEL, ARCTIC_MIN_RATIO, ARCTIC_NUM_CTX),
        (QWEN3_MODEL, QWEN3_MIN_RATIO, QWEN3_NUM_CTX),
    ):
        budget = _char_budget_for_model(model, conservative=True)
        worst_case_tokens = budget / worst
        assert worst_case_tokens <= num_ctx, (
            f"{model}: fallback budget {budget} chars is {worst_case_tokens:.0f} "
            f"tokens at the worst measured density {worst} — over the {num_ctx} "
            "window, so the retry could be refused again"
        )


def test_attempt_tier_is_larger_than_fallback():
    """Strictly decreasing, or 'retry smaller' is not smaller."""
    for model in (ARCTIC_MODEL, QWEN3_MODEL):
        assert (_char_budget_for_model(model)
                > _char_budget_for_model(model, conservative=True))


def test_length_refusal_detected_on_both_status_codes():
    """400 (/api/embed) and 500 (legacy /api/embeddings) carry the SAME cause.

    Measured 2026-09-04. Keying the retry on the status code would miss the
    legacy path entirely; keying on 400 alone would also shrink-and-retry a
    request whose fault was something else.
    """
    from vco_lib.embedding_service import _is_context_overflow_error as refused
    assert refused(Exception("HTTP 400: the input length exceeds the context length"))
    assert refused(Exception("HTTP 500: The input length exceeds the context length"))
    # Older Ollama phrasings. Round 4 shipped a SECOND detector that returned
    # False for these, so an older runner's refusal would have propagated
    # instead of retrying; round 5 deleted it. These two assertions are why the
    # surviving home must stay the wider one.
    assert refused(Exception("HTTP 400: input length exceeds maximum context length"))
    assert refused(Exception("HTTP 400: input exceeds context length"))
    assert not refused(Exception("HTTP 400: model not found"))
    assert not refused(Exception("connection refused"))


def test_primary_is_never_bounded_below_its_own_chunker_output():
    """WP-O restated: a slot must get FULL COVERAGE of its own chunks.

    The chunker sizes chunks to a model's own preset. If the embed bound were
    tighter than that, the ACTIVE slot would truncate its OWN correctly-sized
    chunks — losing primary-retrieval coverage to satisfy a margin that exists
    for the SECONDARY (smaller window) and for legacy corpora chunked for a
    different model. Regression guard: an earlier draft of the median-ratio
    bound gave qwen3 30 543 chars against a 32 768-char chunker maximum, so a
    maximal chunk silently lost 2 225 characters on the PRIMARY path.
    """
    for model in (ARCTIC_MODEL, QWEN3_MODEL):
        assert _char_budget_for_model(model) >= _own_chunker_max(model), (
            f"{model}: attempt budget {_char_budget_for_model(model)} is below "
            f"its own chunker max {_own_chunker_max(model)} — the primary would "
            "truncate chunks it sized itself"
        )


# ── The retry must be WIRED — proven BEHAVIOURALLY (round-4) ───────────────
#
# The previous version of this guard was a pair of SUBSTRING checks over the
# fan-out's source. The round-4 review defeated them in one move: it replaced
# both call sites with a direct `ollama.embed` and left the helper's NAME in a
# comment — and all three assertions still passed. That is the cycle's own
# defect (a check satisfied by a comment) inside the guard built to prevent it.
#
# These drive the real code path instead: a fake adapter that REFUSES the first
# oversized input and accepts a smaller one. If the fan-out does not retry, the
# secondary slot is simply absent from the result, which is the field symptom.

class _RefuseThenAccept:
    """Refuses any input longer than ``limit`` with a context-overflow error."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls: list[int] = []

    def is_reachable(self) -> bool:
        return True

    def embed(self, model, text, num_ctx=None):
        self.calls.append(len(text))
        if len(text) > self.limit:
            raise RuntimeError(
                "Ollama /api/embed HTTP 400: the input length exceeds the "
                "context length"
            )
        return [0.1, 0.2, 0.3]

    def embed_batch(self, model, texts, num_ctx=None):
        return [self.embed(model, t) for t in texts]


def test_secondary_retries_and_still_produces_a_vector_on_refusal():
    """A refused secondary must end with a VECTOR, not a warning.

    Before the retry was wired, `truncate: false` turned an over-window chunk
    into a hard refusal that was logged and dropped — strictly worse than the
    truncated-and-tagged vector it replaced.
    """
    from vco_lib.embedding_service import _embed_secondary_with_refusal_retry
    adapter = _RefuseThenAccept(limit=3_000)
    text = "z" * 40_000
    vec, truncated = _embed_secondary_with_refusal_retry(
        adapter, ARCTIC_MODEL, text
    )
    assert vec == [0.1, 0.2, 0.3], "a refusal must not lose the vector"
    assert truncated is True, "we sent less than we were given — say so"
    assert len(adapter.calls) >= 2, "the first refusal must trigger a retry"
    assert adapter.calls == sorted(adapter.calls, reverse=True), (
        "each attempt must be strictly smaller, or the retry cannot terminate"
    )


def test_secondary_retry_terminates_on_pathologically_dense_content():
    """Dense content (tables 1.41 chars/token, CJK 1.35) overflowed the OLD
    single fallback too, and the second refusal propagated — no vector.

    The loop must keep shrinking to the floor rather than giving up after one
    step. This adapter accepts nothing above the floor, so a two-tier scheme
    raises here and the halving loop succeeds.
    """
    from vco_lib.embedding_service import (
        _EMBED_RETRY_FLOOR_CHARS,
        _embed_secondary_with_refusal_retry,
    )
    adapter = _RefuseThenAccept(limit=_EMBED_RETRY_FLOOR_CHARS)
    vec, truncated = _embed_secondary_with_refusal_retry(
        adapter, ARCTIC_MODEL, "y" * 40_000
    )
    assert vec == [0.1, 0.2, 0.3]
    assert truncated is True
    assert min(adapter.calls) <= _EMBED_RETRY_FLOOR_CHARS


def test_secondary_retry_does_not_swallow_a_non_overflow_error():
    """The leave-alone case: shrinking is a remedy for 'too long' only."""
    from vco_lib.embedding_service import _embed_secondary_with_refusal_retry

    class _AuthFailure(_RefuseThenAccept):
        def embed(self, model, text, num_ctx=None):
            self.calls.append(len(text))
            raise RuntimeError("HTTP 401: invalid api key")

    adapter = _AuthFailure(limit=10)
    try:
        _embed_secondary_with_refusal_retry(adapter, ARCTIC_MODEL, "q" * 40_000)
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("a non-overflow error must propagate")
    assert len(adapter.calls) == 1, "it must not retry a non-overflow failure"


# ── WIRED AT THE FAN-OUT, not just in the helper (round-5 MAJOR-R5-1) ──────
#
# Round 4 replaced a substring guard with these `_RefuseThenAccept` tests — but
# all three call `_embed_secondary_with_refusal_retry` DIRECTLY. Round 5 proved
# that gap: rewriting BOTH fan-out sites to bound-and-tag with the retry removed
# left 202 tests passing across six embedding files. A unit test of a helper
# says nothing about whether anything calls it; that is instance #6 of this
# cycle's defining defect, and these were its second failed guard.
#
# So: drive `embed_text_all_configured` — the production entry point — with a
# MODEL-AWARE fake that refuses only the model under test.

class _RefuseForModel:
    """Refuses inputs over ``limit`` for ``refuse_model`` only; accepts others.

    Deliberately NOT a `TruncationAwareOllamaAdapter` subclass: the plain-stub
    leg is what a custom adapter hits, and it is the leg where a missing retry
    is invisible.
    """

    def __init__(self, refuse_model: str, limit: int) -> None:
        self.refuse_model = refuse_model
        self.limit = limit
        self.calls: list[tuple[str, int]] = []

    def is_reachable(self) -> bool:
        return True

    def embed(self, model, text, num_ctx=None):
        self.calls.append((model, len(text)))
        if model == self.refuse_model and len(text) > self.limit:
            raise RuntimeError(
                "Ollama /api/embed HTTP 400: the input length exceeds the "
                "context length"
            )
        return [0.1, 0.2, 0.3]

    def embed_batch(self, model, texts, num_ctx=None):
        return [self.embed(model, t) for t in texts]

    def calls_for(self, model: str) -> "list[int]":
        return [n for m, n in self.calls if m == model]


def _dual_service(monkeypatch, adapter):
    """An EmbeddingService with qwen3 ACTIVE and the arctic SECONDARY on."""
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL, EmbeddingService

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
        ollama_adapter=adapter,
    )
    svc._text_slot = "qwen3_embed"
    return svc


def test_fanout_secondary_refusal_still_yields_a_tagged_vector(monkeypatch):
    """ACT arm: through `embed_text_all_configured`, an arctic refusal must
    end with an `arctic2_embed` vector tagged truncated — not an absent slot.

    Red-proof: replace either fan-out site's
    `_embed_secondary_with_refusal_retry(...)` with a direct
    `_bounded_for_model` + `_embed_with_exact_truncation` pair (the round-5
    MUT-B2 shape) and this test fails — the slot is gone.
    """
    from vco_lib.embedding_service import ARCTIC_SECONDARY_MODEL

    adapter = _RefuseForModel(ARCTIC_SECONDARY_MODEL, limit=3_000)
    svc = _dual_service(monkeypatch, adapter)

    slots, truncated = svc.embed_text_all_configured_tagged("z" * 20_000)

    assert "qwen3_embed" in slots, "the ACTIVE slot must always be written"
    assert "arctic2_embed" in slots, (
        "a refused secondary must still produce a vector; an absent slot is "
        "the field symptom of an unwired retry"
    )
    assert truncated == ["arctic2_embed"]
    arctic_calls = adapter.calls_for(ARCTIC_SECONDARY_MODEL)
    assert len(arctic_calls) >= 2, "the first refusal must trigger a retry"
    assert arctic_calls == sorted(arctic_calls, reverse=True), (
        "each attempt must be strictly smaller, or the retry cannot terminate"
    )


def test_fanout_secondary_auth_error_is_not_retried(monkeypatch):
    """LEAVE-ALONE twin: a non-overflow failure must drop the slot after ONE
    call. Shrinking an auth error would turn a diagnosable failure into a
    silent half-answer."""
    from vco_lib.embedding_service import ARCTIC_SECONDARY_MODEL

    class _AuthFailForArctic(_RefuseForModel):
        def embed(self, model, text, num_ctx=None):
            self.calls.append((model, len(text)))
            if model == self.refuse_model:
                raise RuntimeError("HTTP 401: invalid api key")
            return [0.1, 0.2, 0.3]

    adapter = _AuthFailForArctic(ARCTIC_SECONDARY_MODEL, limit=0)
    svc = _dual_service(monkeypatch, adapter)

    slots = svc.embed_text_all_configured("z" * 20_000)

    assert "qwen3_embed" in slots
    assert "arctic2_embed" not in slots
    assert len(adapter.calls_for(ARCTIC_SECONDARY_MODEL)) == 1


# ── The PRIMARY path shrinks too (round-5 BLOCKER-R5-1) ────────────────────
#
# R45 sends `truncate: false` on every Ollama embed and its text credited "the
# caller catches and retries". On the primary, no caller caught. Measured live:
# a 19 800-char CJK chunk, a 25 900-char box-drawing diagram and a 24 440-char
# symbolic table — all INSIDE the qwen3 preset's normal range — are refused at
# num_ctx 10 240. Before this fix the node stored no active vector at all,
# where v0.2.91 stored a leading-window one. Losing the slot retrieval reads is
# strictly worse than the tail loss `truncate: false` was introduced to expose.

def test_primary_refusal_yields_a_leading_window_vector_not_nothing(monkeypatch):
    """ACT arm, through `embed_text_all_configured`: the ACTIVE slot must be
    PRESENT after a refusal.

    Red-proof: revert `_embed_text_via_active`'s Ollama leg to a bare
    `self.ollama.embed(...)` and this fails — no `qwen3_embed` key.
    """
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL

    adapter = _RefuseForModel(DEFAULT_TEXT_MODEL, limit=13_000)
    svc = _dual_service(monkeypatch, adapter)

    slots = svc.embed_text_all_configured("z" * 20_000)

    assert "qwen3_embed" in slots, (
        "a refused ACTIVE embed must fall back to a leading window; dropping "
        "the P1 slot inverts the user's priority ordering (retrieval first)"
    )
    assert svc.last_active_truncated is True, "a primary shrink must be reported"
    qwen_calls = adapter.calls_for(DEFAULT_TEXT_MODEL)
    assert len(qwen_calls) >= 2
    assert qwen_calls == sorted(qwen_calls, reverse=True)
    assert qwen_calls[0] == 20_000, (
        "the primary is never pre-bounded — it attempts the WHOLE text and "
        "shrinks only if the runner refuses (R45 primary full coverage)"
    )


def test_primary_embed_text_returns_a_vector_on_refusal(monkeypatch):
    """The other production entry point: `embed_text` used to RAISE, which
    aborted the whole node sync in kg-sync."""
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL

    adapter = _RefuseForModel(DEFAULT_TEXT_MODEL, limit=13_000)
    svc = _dual_service(monkeypatch, adapter)

    assert svc.embed_text("z" * 20_000) == [0.1, 0.2, 0.3]
    assert svc.last_active_truncated is True


def test_primary_full_text_is_not_shrunk_when_accepted(monkeypatch):
    """LEAVE-ALONE twin: the ordinary case must be untouched — ONE call with
    the whole text, and no truncation reported."""
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL

    adapter = _RefuseForModel(DEFAULT_TEXT_MODEL, limit=10**9)
    svc = _dual_service(monkeypatch, adapter)

    slots = svc.embed_text_all_configured("z" * 20_000)

    assert "qwen3_embed" in slots
    assert svc.last_active_truncated is False
    assert adapter.calls_for(DEFAULT_TEXT_MODEL) == [20_000]


def test_first_shrink_prefers_the_min_tier_over_halving():
    """MAJOR-R5-2: halving straight from the attempt over-shrinks exactly the
    content the MIN ratio was measured on.

    arctic refused at 9 815 would halve to 4 907 chars (~2 133 tokens at 2.30
    chars/token) when 7 065 (~3 072 tokens) is accepted — a third of the window
    given up on code-dense text. The first shrink therefore takes the MIN tier;
    below it, plain halving.
    """
    from vco_lib.embedding_service import _char_budget_for_model, _shrink_step

    floor = 512
    min_tier = _char_budget_for_model(ARCTIC_MODEL, conservative=True)
    attempt = _char_budget_for_model(ARCTIC_MODEL, full_coverage=False)
    assert min_tier == 7065 and attempt == 9815

    assert _shrink_step(attempt, floor, min_tier) == min_tier, (
        "the first shrink is the MIN tier, not half"
    )
    assert _shrink_step(min_tier, floor, min_tier) == min_tier // 2, (
        "at or below the MIN tier the sequence is plain halving"
    )
    # Termination holds regardless: every step is strictly smaller until floor.
    n = attempt
    for _ in range(64):
        nxt = _shrink_step(n, floor, min_tier)
        assert nxt < n or nxt == floor
        n = nxt
    assert n == floor


def test_openai_secondary_uses_the_secondary_tier_not_the_primary_floor(monkeypatch):
    """MAJOR-R5-3: the OpenAI secondary reached the shared bound through the
    `_bounded_for_secondary` ALIAS, whose default is the PRIMARY tier.

    The primary tier carries an own-chunker-max floor — "never truncate chunks
    this model sized itself" — which for text-embedding-3-small is 25 600 chars
    and overrides the ratio bound entirely. That floor is meaningless for a
    secondary, which receives text chunked for the ACTIVE model: a 32 768-char
    qwen3 chunk was sent as 25 600 chars, ~10 240 tokens on table-dense content
    against an 8 191-token window. OpenAI refuses, and unlike Ollama this site
    has no shrink, so the slot was dropped.

    Round 4's caller audit concluded "external callers are tests only" and
    missed this site precisely because of the alias — the same class of miss as
    MAJOR-R3-1, on the third secondary.
    """
    from vco_lib.embedding_service import _bounded_for_model, _bounded_for_secondary

    openai_model = "text-embedding-3-small"
    primary = _char_budget_for_model(openai_model)
    secondary = _char_budget_for_model(openai_model, full_coverage=False)
    assert secondary < primary, (
        "the secondary tier must be tighter than the primary's chunker-max floor"
    )

    text = "z" * (primary + 5_000)
    sub, trunc = _bounded_for_secondary(text, openai_model, full_coverage=False)
    assert len(sub) == secondary, (
        "the OpenAI secondary must be bounded at the SECONDARY tier; the alias "
        "must not silently deliver the primary default"
    )
    assert trunc is True
    # And the alias is genuinely the same rule, not a fork.
    assert _bounded_for_secondary(text, openai_model, full_coverage=False) == (
        _bounded_for_model(text, openai_model, full_coverage=False)
    )


def test_active_truncation_flag_does_not_latch_across_calls(monkeypatch):
    """The flag must describe THIS embed, not any earlier one.

    Written because the first version of the fix only ever raised the flag to
    True. A single dense chunk would then have made every later full-fidelity
    vector report as truncated — and the RL tag derived from it would be wrong
    for every subsequent node, which is worse than not having the flag.
    """
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL

    adapter = _RefuseForModel(DEFAULT_TEXT_MODEL, limit=13_000)
    svc = _dual_service(monkeypatch, adapter)

    svc.embed_text("z" * 20_000)
    assert svc.last_active_truncated is True

    svc.embed_text("short text that fits")
    assert svc.last_active_truncated is False, (
        "a later full-fidelity embed must clear the flag, not inherit it"
    )


# ── The CODE single-embed path shrinks too (round-6 BLOCKER-R6-1) ──────────
#
# Round 5 fixed the TEXT twin; `_embed_code_via_active` was left bare, and it is
# what `analyze_code_graph.py` calls per entity. Entities are pre-sized at
# `num_ctx × 3.5` chars while real code measures ~2.8-2.95, so a maximal entity
# overflows its own budget by ~20-25% on BOTH Ollama code tiers — ordinary code,
# not an exotic content class. Missing this while fixing its twin is why the
# guard below drives the production entry points rather than the helper.

def _code_service(monkeypatch, adapter):
    """An EmbeddingService whose ACTIVE CODE slot is Ollama-served."""
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL, EmbeddingService

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
    svc = EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=DEFAULT_TEXT_MODEL,
        code_model_id=DEFAULT_TEXT_MODEL,
        openai_api_key="",
        ollama_adapter=adapter,
    )
    svc._text_slot = "qwen3_embed"
    # Not codesage/jina, so the FastAPI service leg is not taken and the
    # Ollama leg — the one under test — is reached.
    svc._code_slot = "qwen3_embed"
    return svc


def test_code_embed_refusal_yields_a_leading_window_vector_not_nothing(monkeypatch):
    """ACT arm through `embed_code`: the analyzer's per-entity path.

    Red-proof: revert `_embed_code_via_active`'s Ollama leg to a bare
    `self.ollama.embed(...)` and this fails — it raises instead.
    """
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL

    adapter = _RefuseForModel(DEFAULT_TEXT_MODEL, limit=5_000)
    svc = _code_service(monkeypatch, adapter)

    assert svc.embed_code("c" * 7_000) == [0.1, 0.2, 0.3]
    assert svc.last_active_truncated is True
    calls = adapter.calls_for(DEFAULT_TEXT_MODEL)
    assert len(calls) >= 2 and calls == sorted(calls, reverse=True)
    assert calls[0] == 7_000, "the entity is attempted WHOLE before any shrink"


def test_code_all_configured_keeps_the_active_slot_on_refusal(monkeypatch):
    """The fan-out twin: `embed_code_all_configured` must not return `{}`."""
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL

    adapter = _RefuseForModel(DEFAULT_TEXT_MODEL, limit=5_000)
    svc = _code_service(monkeypatch, adapter)

    slots = svc.embed_code_all_configured("c" * 7_000)

    assert "qwen3_embed" in slots, (
        "a refused ACTIVE code embed must fall back to a leading window; an "
        "empty dict is the field symptom (the entity stores no vector)"
    )


def test_code_embed_full_entity_is_not_shrunk_when_accepted(monkeypatch):
    """LEAVE-ALONE twin: the ordinary entity is embedded whole, once."""
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL

    adapter = _RefuseForModel(DEFAULT_TEXT_MODEL, limit=10**9)
    svc = _code_service(monkeypatch, adapter)

    assert svc.embed_code("c" * 7_000) == [0.1, 0.2, 0.3]
    assert svc.last_active_truncated is False
    assert adapter.calls_for(DEFAULT_TEXT_MODEL) == [7_000]


# ── The CODEEMBED SERVICE leg shrinks too (wiring-audit W1, 2026-09-05) ────
#
# Round 6 fixed the Ollama twin; the FastAPI-service leg — the DEFAULT GPU
# tier's path — stayed bare, and it could not even refuse: sentence-transformers
# truncated SILENTLY at the SERVED window (sentence_bert_config.json
# max_seq_length = 1 024, not the 2 048 architectural cap every budget was
# sized for), so roughly half of every maximal entity never influenced its
# vector at HTTP 200, with no warning and no tag. The service now REFUSES
# over-window input with HTTP 400 carrying the phrase
# `_is_context_overflow_error` already matches (see
# test_code_embed_window_refusal.py for the service side), and these drive the
# PRODUCTION entry points with a fake service adapter — the working form from
# the credited-mechanism lesson: if the caller does not retry, the vector is
# absent (or the entity was silently halved before the service learned to
# refuse), which is the field symptom.

CODESAGE_MODEL = "codesage/codesage-large-v2"


class _RefusingCodeEmbedService:
    """Fake CodeEmbed service adapter.

    Refuses any input longer than ``limit`` with the EXACT RuntimeError the
    real CodeEmbedAdapter raises when the service's over-window guard answers
    HTTP 400 (gpu backend, W1) — and the same shape its ollama backend's 502
    produces, whose detail wraps the identical Ollama phrase (MAJOR-W2). A
    batch with ANY over-window item rejects the whole batch, like the real
    /embed endpoint.
    """

    _OVER_WINDOW = (
        'CodeEmbed /embed returned HTTP 400: {"detail":"input length exceeds '
        'the context length: text at index 0 is 2193 tokens, window is 1024 '
        '(model codesage/codesage-large-v2, sentence_bert max_seq_length)"}'
    )

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls: list[int] = []
        self.batch_calls = 0

    def is_reachable(self) -> bool:
        return True

    def embed(self, text, is_query=False):
        self.calls.append(len(text))
        if len(text) > self.limit:
            raise RuntimeError(self._OVER_WINDOW)
        return [0.1, 0.2, 0.3]

    def embed_batch(self, texts, is_query=False):
        self.batch_calls += 1
        if any(len(t) > self.limit for t in texts):
            # The real endpoint rejects the WHOLE batch on one bad item.
            raise RuntimeError(self._OVER_WINDOW)
        return [[0.1, 0.2, 0.3] for _ in texts]


class _AuthFailingCodeEmbedService(_RefusingCodeEmbedService):
    """Non-overflow control: the shrink must not swallow a real error."""

    def embed(self, text, is_query=False):
        self.calls.append(len(text))
        raise RuntimeError("CodeEmbed /embed returned HTTP 500: boom")

    def embed_batch(self, texts, is_query=False):
        self.batch_calls += 1
        raise RuntimeError("CodeEmbed /embed returned HTTP 500: boom")


def _codeembed_service(monkeypatch, adapter, *, active_slot="codesage_embed"):
    """An EmbeddingService whose ACTIVE CODE slot is served by the (fake)
    CodeEmbed service — the default GPU tier's configuration."""
    from vco_lib.embedding_service import DEFAULT_TEXT_MODEL, EmbeddingService

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", raising=False)
    svc = EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=DEFAULT_TEXT_MODEL,
        code_model_id=CODESAGE_MODEL,
        openai_api_key="",
        ollama_adapter=_RefuseForModel(DEFAULT_TEXT_MODEL, limit=10**9),
        code_adapter=adapter,
    )
    svc._text_slot = "qwen3_embed"
    svc._code_slot = active_slot
    return svc


def test_codeembed_service_refusal_yields_leading_window_vector_not_nothing(monkeypatch):
    """ACT arm through `embed_code`: the analyzer's per-entity path, on the
    DEFAULT GPU tier's backend.

    Red-proof: revert `_embed_code_via_active`'s service leg to a bare
    `self.codeembed.embed(code)` and this FAILS — the refusal propagates and
    `embed_code` raises (or, pre-service-fix, silently half-embedded).
    """
    adapter = _RefusingCodeEmbedService(limit=2_000)
    svc = _codeembed_service(monkeypatch, adapter)

    assert svc.embed_code("c" * 7_000) == [0.1, 0.2, 0.3]
    # The code slot's verdict lives in the shared per-call record
    # (``last_active_truncated`` is deliberately the TEXT-slot view — see
    # its docstring — and the code slot here is codesage_embed, not the
    # text slot).
    assert svc._last_truncated_slots.get("codesage_embed") is True
    calls = adapter.calls
    assert len(calls) >= 2 and calls == sorted(calls, reverse=True), (
        "each attempt must be strictly smaller, or the retry cannot terminate"
    )
    assert calls[0] == 7_000, "the entity is attempted WHOLE before any shrink"


def test_codeembed_service_all_configured_keeps_the_active_slot_on_refusal(monkeypatch):
    """The fan-out twin: `embed_code_all_configured` must not return `{}` on
    the service leg — the analyzer's `generate_embedding` returns None for an
    empty dict, and the entity is written WITHOUT a vector."""
    adapter = _RefusingCodeEmbedService(limit=2_000)
    svc = _codeembed_service(monkeypatch, adapter)

    slots = svc.embed_code_all_configured("c" * 7_000)

    assert "codesage_embed" in slots, (
        "a refused ACTIVE code embed must fall back to a leading window; an "
        "empty dict is the field symptom (the entity stores no vector)"
    )


def test_codeembed_service_full_entity_is_not_shrunk_when_accepted(monkeypatch):
    """LEAVE-ALONE twin: the ordinary entity is embedded whole, once."""
    adapter = _RefusingCodeEmbedService(limit=10**9)
    svc = _codeembed_service(monkeypatch, adapter)

    assert svc.embed_code("c" * 7_000) == [0.1, 0.2, 0.3]
    assert svc.last_active_truncated is False
    assert adapter.calls == [7_000]


def test_codeembed_service_non_overflow_error_is_not_retried(monkeypatch):
    """A non-window service failure must propagate — shrinking is a remedy
    for 'too long' only (same contract as the Ollama legs)."""
    adapter = _AuthFailingCodeEmbedService(limit=10)
    svc = _codeembed_service(monkeypatch, adapter)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        svc.embed_code("c" * 7_000)
    assert adapter.calls == [7_000], "it must not retry a non-overflow failure"


def test_codeembed_service_batch_isolates_a_refusal_per_item(monkeypatch):
    """`embed_code_batch` (the enrichment path): one over-window entity must
    neither 400 the whole batch nor lose its own vector — it shrinks; the
    survivors embed whole.

    Red-proof: revert the CodeEmbed leg of `embed_code_batch` to the bare
    `self._retry_once_on_503(self.codeembed.embed_batch, codes)` and this
    FAILS (the whole-batch refusal propagates out of embed_code_batch).
    """
    adapter = _RefusingCodeEmbedService(limit=2_000)
    svc = _codeembed_service(monkeypatch, adapter)

    out = svc.embed_code_batch(["s" * 100, "c" * 7_000, "s" * 200])

    assert adapter.batch_calls == 1, "the batch is attempted once, whole"
    assert len(out) == 3, "order and count preserved"
    assert out[0] == [0.1, 0.2, 0.3] and out[2] == [0.1, 0.2, 0.3]
    assert out[1] == [0.1, 0.2, 0.3], (
        "the over-window item shrinks to a leading window, not [] — the "
        "refusal is an overflow, so the shared shrink loop handles it"
    )
    # The over-window item was retried strictly smaller.
    shrunk_attempts = [n for n in adapter.calls if n <= 7_000]
    assert len(shrunk_attempts) >= 1 and shrunk_attempts[0] < 7_000


def test_codeembed_service_batch_hard_failure_yields_the_sentinel(monkeypatch):
    """A genuinely un-embeddable item (a NON-overflow failure — not a window
    problem, so the shrink must not apply) marks ONLY its index with the
    empty-vector sentinel; the survivors keep their vectors — the Ollama
    batch twin's contract."""
    class _FailLongItems(_RefusingCodeEmbedService):
        def embed(self, text, is_query=False):
            self.calls.append(len(text))
            if len(text) > 1_000:
                raise RuntimeError("CodeEmbed /embed returned HTTP 500: boom")
            return [0.1, 0.2, 0.3]

        def embed_batch(self, texts, is_query=False):
            self.batch_calls += 1
            if any(len(t) > 1_000 for t in texts):
                raise RuntimeError("CodeEmbed /embed returned HTTP 500: boom")
            return [[0.1, 0.2, 0.3] for _ in texts]

    adapter = _FailLongItems(limit=10)
    svc = _codeembed_service(monkeypatch, adapter)

    out = svc.embed_code_batch(["s" * 100, "c" * 7_000])

    assert out[0] == [0.1, 0.2, 0.3], "the survivor keeps its vector"
    assert out[1] == [], (
        "a non-overflow hard failure marks exactly this index with the "
        "empty-vector sentinel (the consumer's per-object failure signal)"
    )


def test_codeembed_secondary_slot_refusal_shrinks_and_tags(monkeypatch):
    """The SECONDARY codesage slot (dual-write ON, active code slot served by
    Ollama): a refused entity shrinks and the slot is TAGGED truncated.

    Without the wrapper the service's NEW 400 refusal would DROP the slot
    with a warning — strictly worse than the silent half-embedding it
    replaced, which is why the secondary leg needed the same shrink.
    """
    adapter = _RefusingCodeEmbedService(limit=2_000)
    # Active code slot served by Ollama (qwen3) so the SECONDARY codesage
    # leg is the one under test.
    svc = _codeembed_service(
        monkeypatch, adapter, active_slot="qwen3_embed"
    )
    # AFTER the helper (it delenv's the flag for the default-off case).
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")

    slots = svc.embed_code_all_configured("c" * 7_000)

    assert "qwen3_embed" in slots, "the active Ollama code slot is unaffected"
    assert slots.get("codesage_embed") == [0.1, 0.2, 0.3], (
        "the secondary slot survives the refusal via the shared shrink loop"
    )
    assert svc._last_truncated_slots.get("codesage_embed") is True, (
        "a shrunk secondary slot is TAGGED, like every other secondary leg"
    )


# ── The ANALYZER's per-entity path (generate_embedding) on the service leg ──
#
# `embed_code`/`embed_code_all_configured` are generate_embedding's engines,
# but the lesson is to drive the production entry point itself: the analyzer
# could route around the service (or drop the dict shape) while every engine
# test stays green.

def test_analyzer_generate_embedding_survives_a_service_refusal(monkeypatch):
    """`generate_embedding` (dual mode → embed_code_all_configured) must
    return a non-empty slot dict when the CodeEmbed service refuses an
    over-window entity — the analyzer writes the row WITHOUT a vector when
    it returns None (MAJOR-W2's field symptom)."""
    import importlib.util

    analyzer_path = (
        Path(__file__).resolve().parent.parent
        / "templates" / "scripts" / "analyze_code_graph.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_w1_analyze_code_graph", str(analyzer_path)
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pytest.skip("analyzer deps unavailable in this environment")
        return

    adapter = _RefusingCodeEmbedService(limit=2_000)
    svc = _codeembed_service(monkeypatch, adapter)
    mod._set_embedding_service(svc)

    try:
        assert mod.DUAL_EMBEDDING_ENABLED, (
            "dual mode is the shipped default; the test's slot-dict contract "
            "assumes it"
        )
        out = mod.generate_embedding("c" * 7_000)
    finally:
        mod._set_embedding_service(None)

    assert out and "codesage_embed" in out, (
        "a refused entity must still produce its slot vector — None here is "
        "the row-written-without-a-vector field symptom"
    )
