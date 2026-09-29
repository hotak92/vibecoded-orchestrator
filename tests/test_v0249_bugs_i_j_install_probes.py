# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.49 Bug I + Bug J — install.py probe regression tests.

Bug I
-----
``install.py::_pull_ollama_models`` (since v0.2.100 a shim over
``vco_lib.ollama_pull.ensure``) previously logged-then-continued on
EVERY model-pull failure. Embedding models are load-bearing — without
the active embedding model present in the local Ollama cache, the KG
silently cannot function (sync_knowledge_graph.py crashes downstream).
Pre-fix install would report "OK" while leaving the user with a broken
KG.

Post-fix the function classifies models as either "embedding"
(load-bearing) or "other" (best-effort). Embedding-model pull failures
raise :class:`OllamaPullError` AFTER attempting all remaining
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
from pathlib import Path
from typing import Dict

import pytest


# Repo root is the parent of tests/. Inject so `import install` resolves
# to the install.py at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install  # noqa: E402 — late import, after sys.path mutation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


class _FakePullHttp:
    """v0.2.100: the pull moved to ``vco_lib.ollama_pull`` behind an injectable
    HTTP layer; this fake fails the pulls of ``failing_models``."""

    def __init__(self, failing_models: set[str]) -> None:
        self.failing = failing_models
        self.present: set[str] = set()

    def get_json(self, url: str, timeout: float):  # noqa: ARG002
        return {"models": [{"name": m} for m in self.present]}

    def post_lines(self, url: str, payload, timeout: float):  # noqa: ARG002
        if payload["name"] in self.failing:
            raise urllib.error.URLError(f"simulated failure for {payload['name']}")
        self.present.add(payload["name"])
        yield b'{"status":"success"}\n'


def _pull(models, embedding_models, failing):
    from vco_lib import ollama_pull

    return ollama_pull.ensure("http://127.0.0.1:1", models, load_bearing=embedding_models,
                              http=_FakePullHttp(failing))


# ---------------------------------------------------------------------------
# Bug I tests
# ---------------------------------------------------------------------------


def test_bug_i_embedding_only_failure_raises():
    """Single load-bearing embedding model fails → raises OllamaPullError
    (v0.2.100 name of EmbeddingModelPullError)."""
    from vco_lib.ollama_pull import OllamaPullError

    with pytest.raises(OllamaPullError) as excinfo:
        _pull(["qwen3-embedding:0.6b"], {"qwen3-embedding:0.6b"}, {"qwen3-embedding:0.6b"})
    msg = str(excinfo.value)
    assert "qwen3-embedding:0.6b" in msg, msg
    assert "Knowledge Graph" in msg, msg


def test_bug_i_non_embedding_only_failure_continues(capsys):
    """Single non-load-bearing model fails → reported, not raised."""
    res = _pull(["gemma4:e4b"], set(), {"gemma4:e4b"})
    assert set(res.failed) == {"gemma4:e4b"}
    assert "FAILED" in capsys.readouterr().out


def test_bug_i_mixed_only_non_embedding_fails():
    """Mixed list, only the non-embedding model fails → no raise."""
    res = _pull(["qwen3-embedding:0.6b", "gemma4:e4b"], {"qwen3-embedding:0.6b"}, {"gemma4:e4b"})
    assert res.pulled == ["qwen3-embedding:0.6b"]


def test_bug_i_mixed_only_embedding_fails():
    """Mixed list, only the embedding model fails → raises, after the rest."""
    from vco_lib.ollama_pull import OllamaPullError

    with pytest.raises(OllamaPullError) as excinfo:
        _pull(["qwen3-embedding:0.6b", "gemma4:e4b"], {"qwen3-embedding:0.6b"},
              {"qwen3-embedding:0.6b"})
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
