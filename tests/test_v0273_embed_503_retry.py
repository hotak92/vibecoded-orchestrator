# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 C-9 / v0.2.77 5c task 4 — EmbeddingService BOUNDED 503 backoff.

The code-embed FastAPI service sheds with 503 when its in-flight semaphore is
saturated (an update-all burst) OR while a model (re)loads. v0.2.73 C-9 added
ONE retry; v0.2.77 5c generalised it to a BOUNDED exponential backoff
(_EMBED_503_RETRY_BACKOFFS, 3 retries by default) so a saturation window is
ridden out before the object degrades to vectorless — WITHOUT masking a
genuinely-down backend (the schedule is finite; a persistent 503 re-raises).
Non-503 errors re-raise immediately (no behaviour change).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vco_lib.embedding_service import EmbeddingService, _EMBED_503_RETRY_BACKOFFS


def _retry(fn, *args, monkeypatch=None):
    """Invoke the helper unbound (it does not use instance state)."""
    return EmbeddingService._retry_once_on_503(None, fn, *args)


def test_503_is_retried_and_succeeds(monkeypatch):
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CodeEmbed /embed returned HTTP 503: loading")
        return [0.1, 0.2]

    assert _retry(_fn) == [0.1, 0.2]
    assert calls["n"] == 2  # one 503, one retry that succeeded


def test_persistent_503_exhausts_schedule_then_reraises(monkeypatch):
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise RuntimeError("Ollama /api/embed returned HTTP 503: busy")

    with pytest.raises(RuntimeError, match="503"):
        _retry(_fn)
    # initial try + one retry per scheduled backoff, then re-raise.
    assert calls["n"] == len(_EMBED_503_RETRY_BACKOFFS) + 1


def test_recovers_on_a_later_retry_within_the_schedule(monkeypatch):
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        # Fail 503 twice, succeed on the third try (still within a 3-retry
        # schedule) — the burst was ridden out, no vectorless degrade.
        if calls["n"] <= 2:
            raise RuntimeError("HTTP 503 capacity")
        return [9.9]

    assert _retry(_fn) == [9.9]
    assert calls["n"] == 3


def test_scale_env_zero_makes_all_delays_zero(monkeypatch):
    # A "0" scale must zero EVERY backoff (not just the first) so the suite
    # never sleeps. Assert by spying on time.sleep.
    import vco_lib.embedding_service as es

    slept: list = []
    monkeypatch.setattr(es.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise RuntimeError("HTTP 503")

    with pytest.raises(RuntimeError, match="503"):
        _retry(_fn)
    # Every slept value is 0 (delay * scale=0, no jitter added when delay==0).
    assert all(s == 0 for s in slept)


def test_non_503_error_reraises_immediately(monkeypatch):
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise RuntimeError("Ollama /api/embed returned HTTP 500: boom")

    with pytest.raises(RuntimeError, match="500"):
        _retry(_fn)
    assert calls["n"] == 1


def test_malformed_delay_env_does_not_crash(monkeypatch):
    """A tunable knob must not be a kill switch (D-12 discipline)."""
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "not-a-number")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 503")
        return [1.0]

    # Patch sleep so the default 2 s doesn't slow the suite.
    import vco_lib.embedding_service as es

    monkeypatch.setattr(es.time, "sleep", lambda _s: None)
    assert _retry(_fn) == [1.0]


def test_success_path_untouched(monkeypatch):
    calls = {"n": 0}

    def _fn(x):
        calls["n"] += 1
        return [x]

    assert _retry(_fn, 0.5) == [0.5]
    assert calls["n"] == 1
