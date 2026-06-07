# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.49 Bug I + Bug J — install.py probe regression tests.

Bug I
-----
``install.py::_pull_ollama_models`` previously logged-then-continued on
EVERY model-pull failure. Embedding models are load-bearing — without
the active embedding model present in the local Ollama cache, the KG
silently cannot function (sync_knowledge_graph.py crashes downstream).
Pre-fix install would report "OK" while leaving the user with a broken
KG.

Post-fix the function classifies models as either "embedding"
(load-bearing) or "other" (best-effort). Embedding-model pull failures
raise :class:`EmbeddingModelPullError` AFTER attempting all remaining
pulls; non-embedding failures still emit a WARN log + manual-pull hint
and continue.

Bug J
-----
``install.py::_probe_dual_ollama_instances`` detects when both the
default-port Ollama daemon (:11434, the user's personal install) AND
the launcher-managed container (:11435, the VCO canonical) respond to
``/api/tags``. Users with both running hit "where did my model go?"
confusion because the two daemons have independent model caches. The
probe is paired with ``_emit_dual_ollama_deferral`` which writes an
``UPDATE_DEFERRED.md`` entry naming both ports + giving reconciliation
guidance.

All 7 tests are fully hermetic — no real Ollama needed; the relevant
subprocess + urllib calls are mocked.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch

import pytest


# Repo root is the parent of tests/. Inject so `import install` resolves
# to the install.py at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install  # noqa: E402 — late import, after sys.path mutation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeOllamaPullResponse:
    """Minimal urlopen-result substitute used for the success path."""

    def __init__(self) -> None:
        # Two reads: first returns a chunk, second returns empty (EOF) so
        # the while-loop in _pull_ollama_models terminates.
        self._chunks = [b'{"status":"success"}\n', b""]

    def read(self, _size: int = 4096) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):  # noqa: D401 — context-manager protocol
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeTagsResponse:
    """Minimal urlopen-result for /api/tags probe (Bug J)."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self._body = b'{"models":[]}'

    def read(self, _size: int = 64) -> bytes:
        # Single-shot read; subsequent calls return empty.
        chunk = self._body
        self._body = b""
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_pull_urlopen(failing_models: set[str]):
    """Build a urlopen replacement that fails for specific model names.

    The /api/pull request body is JSON `{"name": "<model>"}`. We decode
    the request's body to decide whether to raise URLError (simulating
    a pull failure) or return a success response.
    """

    def _urlopen(req, timeout: int = 600):  # noqa: ARG001
        # The pull-request always has a JSON body naming the model.
        try:
            body = req.data.decode("utf-8") if req.data else ""
        except Exception:
            body = ""
        for model in failing_models:
            # crude but sufficient: the model name appears in the JSON
            # body as `"name": "<model>"`. Match the suffix to avoid
            # collisions between e.g. "qwen3-embedding:0.6b" and other
            # qwen3-prefixed models.
            if f'"{model}"' in body:
                raise urllib.error.URLError(f"simulated failure for {model}")
        return _FakeOllamaPullResponse()

    return _urlopen


# ---------------------------------------------------------------------------
# Bug I tests
# ---------------------------------------------------------------------------


def test_bug_i_embedding_only_failure_raises(monkeypatch, capsys):
    """Single load-bearing embedding model fails → raises
    EmbeddingModelPullError.

    Pre-fix: function returned None silently (KG silently broken).
    Post-fix: raises so install aborts with a clear diagnostic.
    """
    failing = {"qwen3-embedding:0.6b"}
    monkeypatch.setattr(
        "urllib.request.urlopen", _make_pull_urlopen(failing)
    )
    with pytest.raises(install.EmbeddingModelPullError) as excinfo:
        install._pull_ollama_models(
            ["qwen3-embedding:0.6b"],
            embedding_models={"qwen3-embedding:0.6b"},
        )
    msg = str(excinfo.value)
    assert "qwen3-embedding:0.6b" in msg, msg
    assert "Knowledge Graph" in msg or "KG" in msg.upper(), msg


def test_bug_i_non_embedding_only_failure_continues(monkeypatch, capsys):
    """Single non-load-bearing model fails → logs + returns without raising.

    Both pre-fix and post-fix should NOT raise; post-fix additionally
    classifies this as a "non-load-bearing" warn.
    """
    failing = {"gemma4:e4b"}
    monkeypatch.setattr(
        "urllib.request.urlopen", _make_pull_urlopen(failing)
    )
    # Empty embedding_models set — gemma4 is explicitly non-load-bearing.
    install._pull_ollama_models(
        ["gemma4:e4b"],
        embedding_models=set(),
    )
    out = capsys.readouterr().out
    assert "WARN" in out, out


def test_bug_i_mixed_only_non_embedding_fails(monkeypatch):
    """Mixed list, only the non-embedding model fails → no raise.

    Confirms the classifier doesn't over-trigger (i.e. the function
    doesn't raise just because *something* failed; it only raises when a
    LOAD-BEARING model failed).
    """
    failing = {"gemma4:e4b"}
    monkeypatch.setattr(
        "urllib.request.urlopen", _make_pull_urlopen(failing)
    )
    # Must NOT raise.
    install._pull_ollama_models(
        ["qwen3-embedding:0.6b", "gemma4:e4b"],
        embedding_models={"qwen3-embedding:0.6b"},
    )


def test_bug_i_mixed_only_embedding_fails(monkeypatch):
    """Mixed list, only the embedding model fails → raises.

    Confirms the classifier correctly fires when the load-bearing
    member of the list fails (even though other models in the list
    succeeded).
    """
    failing = {"qwen3-embedding:0.6b"}
    monkeypatch.setattr(
        "urllib.request.urlopen", _make_pull_urlopen(failing)
    )
    with pytest.raises(install.EmbeddingModelPullError) as excinfo:
        install._pull_ollama_models(
            ["qwen3-embedding:0.6b", "gemma4:e4b"],
            embedding_models={"qwen3-embedding:0.6b"},
        )
    assert "qwen3-embedding:0.6b" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Bug J tests
# ---------------------------------------------------------------------------


def _make_tags_urlopen(
    responding_ports: Dict[int, int],
):
    """Build a urlopen replacement keyed by port.

    ``responding_ports`` maps `<port> -> <http_status>`. Ports not in
    the dict raise ConnectionRefusedError (the canonical "no daemon
    listening" signal).
    """

    def _urlopen(req, timeout: float = 1.0):  # noqa: ARG001
        full_url = req.full_url if hasattr(req, "full_url") else str(req)
        for port, status in responding_ports.items():
            if f":{port}/" in full_url:
                return _FakeTagsResponse(status=status)
        # Not in the dict → simulate "connection refused" via URLError
        # wrapping ConnectionRefusedError.
        raise urllib.error.URLError(
            ConnectionRefusedError(111, "Connection refused")
        )

    return _urlopen


def test_bug_j_neither_port_responds(monkeypatch):
    """Neither :11434 nor :11435 responds → returns None."""
    monkeypatch.setattr(
        "urllib.request.urlopen", _make_tags_urlopen({})
    )
    result = install._probe_dual_ollama_instances()
    assert result is None


def test_bug_j_only_canonical_responds(monkeypatch):
    """Only :11435 (canonical) responds → returns None.

    Single-Ollama setup (the normal case for VCO users). No deferral
    should be emitted.
    """
    monkeypatch.setattr(
        "urllib.request.urlopen", _make_tags_urlopen({11435: 200})
    )
    result = install._probe_dual_ollama_instances()
    assert result is None


def test_bug_j_both_ports_respond(monkeypatch):
    """Both :11434 AND :11435 respond → returns (11434, 11435)."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _make_tags_urlopen({11434: 200, 11435: 200}),
    )
    result = install._probe_dual_ollama_instances()
    assert result == (11434, 11435), (
        f"expected (11434, 11435) — order is (alternate, canonical) — "
        f"got {result!r}"
    )
