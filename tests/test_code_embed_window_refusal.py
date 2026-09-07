# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Wiring-audit W1 (2026-09-05): the CodeEmbed service must REFUSE
over-window input instead of letting sentence-transformers truncate it
silently.

Pre-fix, the gpu backend's ``model.encode`` accepted any length and dropped
everything past ``max_seq_length`` with an HTTP 200 — the DEFAULT code tier's
path. Every budget in the tree was sized for the 2 048 architectural cap
(config.json ``max_position_embeddings``) while the model snapshot serves
``sentence_bert_config.json``'s ``max_seq_length`` = 1 024 (verified live:
appending 500 tokens past position 1 024 leaves the vector IDENTICAL,
cos = 1.0000), so roughly half of every maximal entity never influenced its
vector, with no warning and no tag.

These tests pin the service-side half of the fix:
  * an over-window text is refused with HTTP 400 BEFORE encode runs;
  * the refusal phrase is the one ``_is_context_overflow_error`` (the ONE
    detector, in ``vco_lib.embedding_service``) already recognises — matched
    through the CodeEmbedAdapter's RuntimeError wrapper, and identically for
    the service's ollama-backend 502 (MAJOR-W2);
  * ``CODE_EMBED_MAX_SEQ_LEN`` actually sets the window (W11: it used to be
    plumbed into ``model_kwargs``, which sentence-transformers forwards to
    the TRANSFORMER, so the documented knob was inert).

The caller-side half (shrink-on-refusal through the production entry points)
lives in ``tests/test_secondary_window_exact_bound.py``.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import HTTPException  # noqa: E402

from vco_lib.embedding_service import _is_context_overflow_error  # noqa: E402

from claude_mcp_servers.code_embedding_service import server as srv  # noqa: E402


class _FakeTokenizer:
    """Deterministic tokenizer: 1 token per 4 characters."""

    def __call__(self, text, add_special_tokens=True):
        n = len(text) // 4 + (1 if add_special_tokens else 0)
        return {"input_ids": list(range(n))}


class _FakeModel:
    def __init__(self, max_seq_length: int = 1024):
        self.max_seq_length = max_seq_length
        self.tokenizer = _FakeTokenizer()
        self.encode_calls: list[list[str]] = []

    def encode(self, texts, **kwargs):
        self.encode_calls.append(list(texts))
        return _FakeNumpy([[0.1, 0.2, 0.3] for _ in texts])

    def get_sentence_embedding_dimension(self):
        return 3


class _FakeNumpy:
    """Stand-in for the ndarray ``encode`` returns (needs ``.tolist()``)."""

    def __init__(self, rows):
        self._rows = rows

    def tolist(self):
        return self._rows


# ---------------------------------------------------------------------------
# The refusal itself
# ---------------------------------------------------------------------------


def test_over_window_text_is_refused_before_encode():
    """4 200 chars = 1 051 fake tokens > 1 024 → HTTP 400, encode never runs."""
    model = _FakeModel(max_seq_length=1024)
    text = "x" * 4_200  # 1050 + 1 special token = 1051 tokens
    with pytest.raises(HTTPException) as exc_info:
        srv._refuse_over_window(model, [text], is_query=False)
    assert exc_info.value.status_code == 400
    assert "input length exceeds the context length" in exc_info.value.detail
    assert "1051" in exc_info.value.detail and "1024" in exc_info.value.detail


def test_at_window_text_is_accepted():
    """Exactly at the window (1024 tokens incl. special) → no refusal.

    The boundary is > window, not >=: an exact fit is a fit.
    """
    model = _FakeModel(max_seq_length=1024)
    text = "x" * 4_092  # 1023 + 1 special token = 1024 tokens
    srv._refuse_over_window(model, [text], is_query=False)  # must not raise


def test_query_instruction_counts_toward_the_window():
    """With an instruction prefix the QUERY path must count it too."""
    model = _FakeModel(max_seq_length=1024)
    text = "x" * 4_000  # 1001 tokens; + instruction (51) = over the window
    with monkeypatched_instruction("i" * 200):  # 50 + 1 tokens
        with pytest.raises(HTTPException) as exc_info:
            srv._refuse_over_window(model, [text], is_query=True)
    assert exc_info.value.status_code == 400


class monkeypatched_instruction:
    """Context manager pinning the service module's INSTRUCTION constant."""

    def __init__(self, value: str):
        self.value = value

    def __enter__(self):
        self._old = srv.INSTRUCTION
        srv.INSTRUCTION = self.value

    def __exit__(self, *exc):
        srv.INSTRUCTION = self._old
        return False


def test_embed_gpu_calls_the_guard_before_encode(monkeypatch):
    monkeypatch.setattr(srv, "_load_gpu_model", lambda: _FakeModel(1024))
    with pytest.raises(HTTPException) as exc_info:
        srv._embed_gpu(["y" * 8_000], is_query=False)
    assert exc_info.value.status_code == 400


def test_embed_gpu_under_window_still_encodes(monkeypatch):
    model = _FakeModel(1024)
    monkeypatch.setattr(srv, "_load_gpu_model", lambda: model)
    out = srv._embed_gpu(["y" * 400], is_query=False)
    assert out == [[0.1, 0.2, 0.3]]
    assert model.encode_calls == [["y" * 400]]


# ---------------------------------------------------------------------------
# The phrase contract — ONE detector, both backend modes
# ---------------------------------------------------------------------------


def _adapter_wrapped(status_code: int, detail: str) -> RuntimeError:
    """The exact RuntimeError shape CodeEmbedAdapter._embed_chunk raises."""
    body = json.dumps({"detail": detail})
    return RuntimeError(
        f"CodeEmbed /embed returned HTTP {status_code}: {body[:500]}"
    )


def test_gpu_refusal_phrase_matches_the_shared_overflow_detector():
    """The 400's message must match `_is_context_overflow_error` THROUGH the
    adapter wrapper — the detector is not widened for a new phrasing."""
    try:
        srv._refuse_over_window(_FakeModel(1024), ["z" * 4_200], False)
    except HTTPException as exc:
        wrapped = _adapter_wrapped(exc.status_code, exc.detail)
    else:  # pragma: no cover — the guard must have refused
        raise AssertionError("4 200 chars must be refused at a 1 024 window")
    assert _is_context_overflow_error(wrapped) is True, (
        "the service's refusal must be recognisable by the ONE detector the "
        "shrink loop keys on — else the retry never fires and the vector is "
        "dropped (the exact defect W1/W2 fix)"
    )


def test_ollama_backend_502_phrase_matches_the_shared_detector():
    """MAJOR-W2: the service's ollama backend 502 wraps the IDENTICAL Ollama
    phrase — same detector, same shrink."""
    ollama_text = '{"error":"the input length exceeds the context length"}'
    wrapped = _adapter_wrapped(502, f"Ollama error: {ollama_text}")
    assert _is_context_overflow_error(wrapped) is True


def test_non_window_service_error_does_not_match():
    """Leave-alone control: a 500/malformed error must NOT look like an
    overflow, or the shrink would mask diagnosable failures."""
    assert _is_context_overflow_error(
        _adapter_wrapped(500, "internal error")
    ) is False


# ---------------------------------------------------------------------------
# The CODE_EMBED_MAX_SEQ_LEN knob (W11 — it used to be plumbed into the
# transformer's kwargs and could not change the window at all)
# ---------------------------------------------------------------------------


class _FakeSentenceTransformer:
    """Records the max_seq_length assignment the loader must now perform."""

    instances: list["_FakeSentenceTransformer"] = []

    def __init__(self, model_name, **kwargs):
        assert "max_seq_length" not in kwargs, (
            "max_seq_length is NOT a SentenceTransformer init parameter "
            "(verified against 5.5.0) — plumbing it as a kwarg is the W11 "
            "defect this test pins closed"
        )
        assert "max_seq_length" not in kwargs.get("model_kwargs", {}), (
            "model_kwargs is forwarded to AutoModel.from_pretrained (the "
            "TRANSFORMER's kwargs) — it cannot set the truncation window"
        )
        self.model_name = model_name
        self.max_seq_length = 1024  # the snapshot's served default
        self.tokenizer = _FakeTokenizer()
        _FakeSentenceTransformer.instances.append(self)

    def get_sentence_embedding_dimension(self):
        return 3


@pytest.fixture()
def fake_st(monkeypatch):
    _FakeSentenceTransformer.instances = []
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    monkeypatch.setattr(srv, "_st_model", None)  # force a fresh load
    return _FakeSentenceTransformer


def test_max_seq_len_knob_sets_the_window(fake_st, monkeypatch):
    monkeypatch.setattr(srv, "MAX_SEQ_LEN", "2048")
    model = srv._load_gpu_model()
    assert model.max_seq_length == 2048, (
        "CODE_EMBED_MAX_SEQ_LEN must set the SentenceTransformer "
        "max_seq_length attribute — the window the refusal guards"
    )


def test_without_the_knob_the_served_default_stands(fake_st, monkeypatch):
    monkeypatch.setattr(srv, "MAX_SEQ_LEN", "")
    model = srv._load_gpu_model()
    assert model.max_seq_length == 1024, (
        "no knob → the snapshot's sentence_bert_config.json window (1 024 "
        "for codesage-large-v2), NOT the 2 048 architectural cap"
    )


# ---------------------------------------------------------------------------
# The SSOT entry the budgets derive from (behavioural coverage lives in the
# shrink tests + the budget tests; this pin guards the number itself)
# ---------------------------------------------------------------------------


def test_codesage_num_ctx_is_the_served_window():
    from claude_mcp_servers.weaviate_mcp.chunking import MODEL_TOKEN_LIMITS

    assert MODEL_TOKEN_LIMITS["codesage/codesage-large-v2"] == 1_024
    assert MODEL_TOKEN_LIMITS["codesage-large-v2"] == 1_024
    # The derived entity budget (see test_code_truncation_chunking.py for the
    # behavioural pins): 1 024 × 3.5 = 3 584 chars.
    from claude_mcp_servers.weaviate_mcp.code_truncation import (
        _max_chars_for_model,
    )

    assert _max_chars_for_model("codesage/codesage-large-v2") == 3_584
