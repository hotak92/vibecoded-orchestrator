# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 C-9 rider — EmbeddingService retries ONCE (with delay) on HTTP 503.

The code-embed FastAPI service (and Ollama, briefly) return 503 while a model
is (re)loading; pre-C-9 the first embed of a cold session failed the whole
call chain (the C-1 trigger). One retry absorbs the transient; a second 503
re-raises (genuinely-down backend is not masked); non-503 errors re-raise
immediately (no behaviour change).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vco_lib.embedding_service import EmbeddingService


def _retry(fn, *args, monkeypatch=None):
    """Invoke the helper unbound (it does not use instance state)."""
    return EmbeddingService._retry_once_on_503(None, fn, *args)


def test_503_is_retried_once_and_succeeds(monkeypatch):
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CodeEmbed /embed returned HTTP 503: loading")
        return [0.1, 0.2]

    assert _retry(_fn) == [0.1, 0.2]
    assert calls["n"] == 2


def test_second_503_reraises(monkeypatch):
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise RuntimeError("Ollama /api/embed returned HTTP 503: busy")

    with pytest.raises(RuntimeError, match="503"):
        _retry(_fn)
    assert calls["n"] == 2  # exactly one retry, not a loop


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
