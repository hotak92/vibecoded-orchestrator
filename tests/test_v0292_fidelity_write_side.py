# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W4 — the shrink/floor-refusal WRITE side of `vco_lib.embedding_fidelity`.

The reader half shipped first: the SessionStart hooks spawn
``python -m vco_lib.embedding_fidelity notice``, and kg-sync's outage writer
was live. But ``note_shrink`` / ``note_floor_refusal`` had **zero production
callers**, so the ``shrink_summary`` rows the notice reads were never produced
— the surfacing path was blind to precisely the failures v0.2.92 introduced.
(The module's own docstring said so, which is the honest way to ship a half:
name the gap where the next reader will look.)

Both are now called from ``_embed_shrinking_on_overflow``, the ONE loop all
four embed legs route through — primary text, secondary text, active code, and
the CodeEmbed service leg — so wiring it once covers every path.

Two decisions these tests pin, because both are easy to get subtly wrong:

1. **The OUTCOME is recorded, not each rung.** A three-step ladder is one
   input that lost text, not three shrinks. Per-iteration counting would
   inflate the summary by the ladder depth and make one pathological chunk
   look like a corpus-wide problem.
2. **A non-overflow error is NOT a fidelity loss.** Propagating an auth or
   network failure untouched is correct behaviour; recording it would put
   outages in a report about truncation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import embedding_fidelity as F  # noqa: E402
from vco_lib.embedding_service import (  # noqa: E402
    EmbeddingService,
    _embed_shrinking_on_overflow,
)

_MODEL = "snowflake-arctic-embed2"
_OVERFLOW = 'HTTP 400: {"error":"the input length exceeds the context length"}'


@pytest.fixture(autouse=True)
def _clean_state():
    """Per-test isolation: the module keeps process-wide counters, so rows
    from one test would otherwise leak into the next one's assertions."""
    F._STATE.shrinks.clear()
    F._STATE.floor_refusals.clear()
    yield
    F._STATE.shrinks.clear()
    F._STATE.floor_refusals.clear()


def _refuses_over(limit: int):
    calls: list[int] = []

    def fn(text: str):
        calls.append(len(text))
        if len(text) > limit:
            raise RuntimeError(_OVERFLOW)
        return [0.1]

    return fn, calls


def test_a_successful_ladder_records_exactly_one_row_with_the_final_length():
    fn, calls = _refuses_over(3_000)
    _embed_shrinking_on_overflow(fn, "z" * 40_000, model_id=_MODEL)

    assert len(calls) > 3, "fixture must actually exercise a multi-step ladder"
    assert len(F._STATE.shrinks) == 1
    row = F._STATE.shrinks[_MODEL]
    assert row["count"] == 1, (
        "one input that lost text is ONE shrink, however many rungs the "
        "ladder took to land"
    )
    assert row["orig_chars"] == 40_000
    assert row["sent_chars"] == calls[-1], "the accepted length, not an intermediate"


def test_a_full_fidelity_embed_records_nothing():
    """LEAVE-ALONE arm. If this ever records, every ordinary embed on a
    healthy corpus pollutes the notice and the signal is worthless."""
    fn, _ = _refuses_over(10**9)
    _embed_shrinking_on_overflow(fn, "z" * 40_000, model_id=_MODEL)
    assert not F._STATE.shrinks
    assert not F._STATE.floor_refusals


def test_a_refusal_at_the_floor_is_recorded_and_still_raises():
    fn, _ = _refuses_over(1)
    with pytest.raises(RuntimeError):
        _embed_shrinking_on_overflow(fn, "z" * 40_000, model_id=_MODEL)
    assert F._STATE.floor_refusals[_MODEL]["count"] == 1, (
        "a slot that got NO vector is the most actionable thing this surface "
        "can report — it must not be silent"
    )


def test_a_non_overflow_error_is_not_a_fidelity_loss():
    def auth_failure(_text: str):
        raise RuntimeError("HTTP 401: invalid api key")

    with pytest.raises(RuntimeError, match="401"):
        _embed_shrinking_on_overflow(auth_failure, "z" * 40_000, model_id=_MODEL)
    assert not F._STATE.floor_refusals, "an auth error is an outage, not truncation"
    assert not F._STATE.shrinks


def test_a_broken_recorder_never_breaks_the_embed(monkeypatch):
    """Telemetry is not allowed to fail an embed — the entire point of the
    ladder is that a dense chunk still produces a vector."""
    def boom(*_a, **_k):
        raise RuntimeError("recorder exploded")

    monkeypatch.setattr(F, "note_shrink", boom)
    fn, _ = _refuses_over(3_000)
    vec, sent = _embed_shrinking_on_overflow(fn, "z" * 40_000, model_id=_MODEL)
    assert vec == [0.1] and sent < 40_000


# ---------------------------------------------------------------------------
# The CODE legs own the same per-call record the text legs do — and must
# RESET it the same way. ``embed_text_all_configured`` clears
# ``EmbeddingService._last_truncated_slots`` up front, but
# ``embed_code_all_configured`` and ``embed_code`` wrote into a record they
# never cleared, and the secondary ``codesage_embed`` entry is only ever
# WRITTEN True (never assigned False), so one sub-windowed secondary latched
# into every later code embed's verdict: a full-fidelity embed still
# reported a truncation it did not have.
# ---------------------------------------------------------------------------


class _AcceptingOllama:
    """Injected OllamaAdapter: every input fits — full fidelity, no shrink."""

    def embed(self, model: str, text: str):
        return [0.4, 0.5, 0.6]


class _RefusingCodeEmbed:
    """Injected CodeEmbedAdapter: accepts only sub-``limit`` input.

    Over-window input raises the service's real refusal phrase so the ONE
    shared shrink loop — and its truncation verdict — runs unmodified.
    """

    def __init__(self, limit: int):
        self.limit = limit

    def is_reachable(self) -> bool:
        return True

    def embed(self, text: str):
        if len(text) > self.limit:
            raise RuntimeError(_OVERFLOW)
        return [0.1, 0.2, 0.3]


def _code_service() -> EmbeddingService:
    """CPU-tier shape: qwen3 serves BOTH slots, so the active code leg rides
    Ollama and the codesage service is the SECONDARY the fan-out adds."""
    return EmbeddingService(
        project_root=None,
        ollama_url="http://unused.invalid",
        code_embed_url="http://unused.invalid",
        text_model_id="qwen3-embedding:0.6b",
        code_model_id="qwen3-embedding:0.6b",
        openai_api_key="",
        ollama_adapter=_AcceptingOllama(),
        code_adapter=_RefusingCodeEmbed(3_000),
    )


def test_a_non_truncating_code_fan_out_clears_the_prior_call_s_verdict(monkeypatch):
    """Decision pin: the per-call truncation record describes THIS embed.

    Call 1 shrinks the codesage secondary (over the staged service's
    window) → ``codesage_embed`` is recorded truncated. Call 2 embeds a
    snippet nothing refuses — the record must not still carry call 1's
    verdict. LEAVE-ALONE arm included: the reset may not cripple the
    record, so this call's own (full-fidelity) verdict is still assigned.
    """
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "1")
    svc = _code_service()

    svc.embed_code_all_configured("z" * 40_000)
    assert svc._last_truncated_slots.get("codesage_embed") is True, (
        "fixture premise: the first call must produce a code-side "
        "truncation verdict"
    )

    svc.embed_code_all_configured("def f(): pass")
    assert "codesage_embed" not in svc._last_truncated_slots, (
        "a full-fidelity code embed still carries the previous call's "
        "truncated secondary — the verdict latched because the secondary "
        "entry is only ever written True, never cleared"
    )
    assert svc._last_truncated_slots.get(svc.code_vector_slot) is False, (
        "the reset must not disable the record: this call's own verdict "
        "is still assigned"
    )


def test_a_single_code_embed_clears_the_prior_call_s_verdict(monkeypatch):
    """Same pin through ``embed_code`` — the code-graph analyzer's per-entity
    path. It writes the ACTIVE slot's verdict into the shared record, so a
    verdict left by an earlier fan-out call leaks into what a later reader
    of the record consults."""
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "1")
    svc = _code_service()

    svc.embed_code_all_configured("z" * 40_000)
    assert svc._last_truncated_slots.get("codesage_embed") is True

    # Fresh input, so the per-instance memo cannot short-circuit the embed.
    svc.embed_code("def g(): pass")
    assert "codesage_embed" not in svc._last_truncated_slots, (
        "embed_code leaves a prior fan-out call's truncated secondary in "
        "the record a later reader consults"
    )
