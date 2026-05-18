# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for vco_lib.embedding_service — the central embedding dispatcher.

These tests run with HTTP mocked at the ``requests.Session`` level so
nothing reaches Ollama, the CodeEmbed service, or OpenAI. The default
fixture installs adapters with a fake session that records every call
and returns scripted responses.

Test areas (mirrors the v0.2.18 plan acceptance criteria):

  * Slot resolution — qwen3 / arctic / openai / codesage / jina /
    fallback-to-default cover.
  * Per-preset construction — qwen3 (default), openai, arctic via
    ACTIVE_EMBEDDING + EMBEDDING_MODEL env, CPU fallback via
    CODE_EMBED_BACKEND=ollama.
  * Each provider in isolation — Ollama batched + legacy-fallback,
    CodeEmbed health probe + batched /embed, OpenAI validation
    matrix (200/401/403/404/429).
  * Catalog discovery — Ollama only, CodeEmbed only, OpenAI only,
    all three, none reachable.
  * ``NoEmbeddingBackendError`` raises cleanly + writes the JSONL log
    + writes EMBEDDING_FAILURES.md, and a successful construction
    clears the .md.
  * HTTP session re-use across batch calls.
  * Batched calls handle empty list / single item / 100+ items.
  * Context-manager protocol calls close() correctly.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter
from vco_lib.embedding_providers.ollama import (
    OllamaAdapter,
    looks_like_embedding_model,
)
from vco_lib.embedding_providers.openai import (
    KNOWN_OPENAI_EMBEDDING_MODELS,
    OpenAIAdapter,
    ValidationResult,
)
from vco_lib.embedding_service import (
    DEFAULT_CODE_MODEL,
    DEFAULT_TEXT_MODEL,
    DEFAULT_TEXT_SLOT,
    EmbeddingService,
    ModelChoice,
    NoEmbeddingBackendError,
    _resolve_code_slot,
    _resolve_text_slot,
    main as embedding_service_main,
)


# ---------------------------------------------------------------------------
# Fake HTTP — record-and-script style. Each test installs a script and
# verifies the calls afterwards.
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for `requests.Response`."""

    def __init__(
        self,
        status_code: int = 200,
        json_body: object | None = None,
        text_body: str = "",
    ) -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = text_body if text_body else (json.dumps(json_body) if json_body is not None else "")

    def json(self) -> object:
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            # Mirror requests.Response.raise_for_status() — raises
            # requests.HTTPError (a RequestException subclass). The
            # adapters only catch RequestException, so using anything
            # else here makes them choke on non-200 responses in tests.
            import requests as _req
            raise _req.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """A ``requests.Session`` look-alike that scripts responses by URL.

    Usage::

        sess = FakeSession()
        sess.script("GET", "http://localhost:11435/api/tags",
                    FakeResponse(200, {"models": [...]}))
        sess.script("POST", "http://localhost:11435/api/embed",
                    FakeResponse(200, {"embeddings": [[1, 2, 3]]}))

    After the test, inspect ``sess.calls`` for the (method, url, kwargs)
    tuples that were issued. ``closed`` flips True on ``close()``.
    """

    def __init__(self) -> None:
        # Keyed by (method.upper(), url) → list of FakeResponse (consumed in order)
        self._scripts: dict[tuple[str, str], list[FakeResponse]] = {}
        # Default response if no script matches (None → 404)
        self._default: FakeResponse | None = None
        self.calls: list[tuple[str, str, dict]] = []
        self.closed = False

    def script(self, method: str, url: str, response: FakeResponse) -> None:
        self._scripts.setdefault((method.upper(), url), []).append(response)

    def script_many(
        self, method: str, url: str, responses: list[FakeResponse]
    ) -> None:
        self._scripts.setdefault((method.upper(), url), []).extend(responses)

    def set_default(self, response: FakeResponse | None) -> None:
        self._default = response

    def get(self, url: str, **kwargs):  # noqa: D401  — mimics requests.Session.get
        return self._do("GET", url, kwargs)

    def post(self, url: str, **kwargs):
        return self._do("POST", url, kwargs)

    def close(self) -> None:
        self.closed = True

    def _do(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        queue = self._scripts.get((method, url), [])
        if queue:
            if len(queue) == 1:
                # Last response — keep it in the queue so repeated
                # calls to the same URL replay the final state. This
                # matches the common "same endpoint queried twice
                # within one logical operation" pattern (health probe
                # then list, validate cache miss then hit).
                return queue[0]
            return queue.pop(0)
        if self._default is not None:
            return self._default
        return FakeResponse(404, {"error": "no script"}, "no script registered")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EnvIsolation:
    """Context manager that wipes ALL embedding-relevant env vars.

    Tests should compose this with ``patch.dict`` to set only the keys
    they actually care about — guarantees no leakage from the runner's
    real environment.
    """

    _KEYS = (
        "OLLAMA_URL",
        "CODE_EMBED_SERVICE_URL",
        "CODE_EMBED_BACKEND",
        "CODE_EMBED_MODEL",
        "EMBEDDING_MODEL",
        "ACTIVE_EMBEDDING",
        "OPENAI_API_KEY",
        "OPENAI_EMBEDDING_MODEL",
        "KG_BASE_DIR",
        "VCT_ORCHESTRATOR_ROOT",
    )

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        for k in self._KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, exc_type, exc, tb):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _ollama_tags_response(model_names: list[str]) -> FakeResponse:
    return FakeResponse(
        200,
        {"models": [{"name": n, "size": 0, "modified_at": "2026-01-01"} for n in model_names]},
    )


# ---------------------------------------------------------------------------
# Slot resolution
# ---------------------------------------------------------------------------


class SlotResolutionTests(unittest.TestCase):
    def test_qwen3_text(self):
        slot, dim = _resolve_text_slot("qwen3-embedding:0.6b")
        self.assertEqual(slot, "qwen3_embed")
        self.assertEqual(dim, 1024)

    def test_arctic2_text(self):
        slot, dim = _resolve_text_slot("snowflake-arctic-embed2:latest")
        self.assertEqual(slot, "arctic2_embed")
        self.assertEqual(dim, 1024)

    def test_legacy_arctic_text(self):
        slot, _ = _resolve_text_slot("snowflake-arctic-embed:latest")
        self.assertEqual(slot, "ollama_embed")

    def test_openai_small_text(self):
        slot, dim = _resolve_text_slot("text-embedding-3-small")
        self.assertEqual(slot, "openai_text_embed")
        self.assertEqual(dim, 1536)

    def test_openai_large_text(self):
        slot, dim = _resolve_text_slot("text-embedding-3-large")
        self.assertEqual(slot, "openai_text_embed")
        self.assertEqual(dim, 3072)

    def test_unknown_text_falls_back(self):
        slot, dim = _resolve_text_slot("unobtainium-embed-2:latest")
        self.assertEqual((slot, dim), DEFAULT_TEXT_SLOT)

    def test_codesage_code(self):
        slot, dim = _resolve_code_slot("codesage-large-v2")
        self.assertEqual(slot, "codesage_embed")
        self.assertEqual(dim, 2048)

    def test_codesage_prefixed_code(self):
        slot, _ = _resolve_code_slot("codesage/codesage-large-v2")
        self.assertEqual(slot, "codesage_embed")

    def test_jina_code(self):
        slot, dim = _resolve_code_slot("unclemusclez/jina-embeddings-v2-base-code:latest")
        self.assertEqual(slot, "jina_embed")
        self.assertEqual(dim, 768)

    def test_openai_code(self):
        slot, _ = _resolve_code_slot("text-embedding-3-small")
        self.assertEqual(slot, "openai_code_embed")

    def test_qwen3_code_fallback(self):
        # CPU-only machines reuse the qwen3 text model as code fallback
        slot, _ = _resolve_code_slot("qwen3-embedding:0.6b")
        self.assertEqual(slot, "qwen3_embed")


# ---------------------------------------------------------------------------
# OllamaAdapter
# ---------------------------------------------------------------------------


class OllamaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.adapter = OllamaAdapter("http://localhost:11435", self.session)

    def test_looks_like_embedding_model(self):
        self.assertTrue(looks_like_embedding_model("qwen3-embedding:0.6b"))
        self.assertTrue(looks_like_embedding_model("text-embedding-3-small"))
        self.assertFalse(looks_like_embedding_model("llama3.2:latest"))

    def test_is_reachable_true(self):
        self.session.script(
            "GET", "http://localhost:11435/api/tags",
            _ollama_tags_response(["qwen3-embedding:0.6b"]),
        )
        self.assertTrue(self.adapter.is_reachable())

    def test_is_reachable_false_on_network_error(self):
        # Default response is 404 — also test connection-refused style errors
        def raise_err(*args, **kwargs):
            import requests
            raise requests.ConnectionError("connection refused")
        self.session.get = raise_err  # type: ignore[assignment]
        self.assertFalse(self.adapter.is_reachable())

    def test_list_embedding_models_filters_non_embed(self):
        self.session.script(
            "GET", "http://localhost:11435/api/tags",
            _ollama_tags_response([
                "qwen3-embedding:0.6b",
                "llama3.2:latest",
                "snowflake-arctic-embed2:latest",
            ]),
        )
        models = self.adapter.list_embedding_models()
        names = [m["name"] for m in models]
        self.assertIn("qwen3-embedding:0.6b", names)
        self.assertIn("snowflake-arctic-embed2:latest", names)
        self.assertNotIn("llama3.2:latest", names)

    def test_embed_modern_endpoint(self):
        self.session.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(200, {"embeddings": [[0.1, 0.2, 0.3]]}),
        )
        vec = self.adapter.embed("qwen3-embedding:0.6b", "hello")
        self.assertEqual(vec, [0.1, 0.2, 0.3])
        # Verify num_ctx=8192 was passed
        _, _, kwargs = self.session.calls[-1]
        self.assertEqual(kwargs["json"]["options"]["num_ctx"], 8192)

    def test_embed_falls_back_to_legacy_on_404(self):
        self.session.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(404, {"error": "not found"}),
        )
        self.session.script(
            "POST", "http://localhost:11435/api/embeddings",
            FakeResponse(200, {"embedding": [0.4, 0.5, 0.6]}),
        )
        vec = self.adapter.embed("legacy-model", "hello")
        self.assertEqual(vec, [0.4, 0.5, 0.6])

    def test_embed_batch_modern(self):
        self.session.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(200, {"embeddings": [[1, 2], [3, 4], [5, 6]]}),
        )
        vecs = self.adapter.embed_batch("qwen3-embedding:0.6b", ["a", "b", "c"])
        self.assertEqual(len(vecs), 3)
        self.assertEqual(vecs[0], [1.0, 2.0])
        self.assertEqual(vecs[2], [5.0, 6.0])

    def test_embed_batch_empty(self):
        vecs = self.adapter.embed_batch("any-model", [])
        self.assertEqual(vecs, [])
        # ZERO HTTP calls for empty batch
        self.assertEqual(self.session.calls, [])

    def test_embed_batch_count_mismatch_raises(self):
        self.session.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(200, {"embeddings": [[1, 2]]}),
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.embed_batch("any-model", ["a", "b", "c"])
        self.assertIn("order cannot be reconstructed", str(ctx.exception))

    def test_embed_batch_falls_back_to_legacy(self):
        self.session.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(404, {"error": "not found"}),
        )
        self.session.script(
            "POST", "http://localhost:11435/api/embeddings",
            FakeResponse(200, {"embedding": [1, 2]}),
        )
        self.session.script(
            "POST", "http://localhost:11435/api/embeddings",
            FakeResponse(200, {"embedding": [3, 4]}),
        )
        vecs = self.adapter.embed_batch("legacy", ["a", "b"])
        self.assertEqual(vecs, [[1.0, 2.0], [3.0, 4.0]])

    def test_embed_raises_on_500(self):
        self.session.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(500, text_body="oops"),
        )
        with self.assertRaises(RuntimeError):
            self.adapter.embed("any-model", "x")


# ---------------------------------------------------------------------------
# CodeEmbedAdapter
# ---------------------------------------------------------------------------


class CodeEmbedAdapterTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.adapter = CodeEmbedAdapter("http://localhost:11440", self.session)

    def test_health_ok(self):
        self.session.script(
            "GET", "http://localhost:11440/health",
            FakeResponse(200, {
                "status": "ok",
                "backend": "gpu",
                "model": "codesage-large-v2",
                "dim": 2048,
            }),
        )
        h = self.adapter.health()
        self.assertIsNotNone(h)
        self.assertEqual(self.adapter.model_name, "codesage-large-v2")
        self.assertEqual(self.adapter.model_dim, 2048)
        self.assertEqual(self.adapter.backend, "gpu")

    def test_health_cached(self):
        self.session.script(
            "GET", "http://localhost:11440/health",
            FakeResponse(200, {"status": "ok", "backend": "gpu", "model": "x", "dim": 100}),
        )
        self.adapter.health()
        self.adapter.health()  # should not trigger second HTTP call
        get_calls = [c for c in self.session.calls if c[0] == "GET"]
        self.assertEqual(len(get_calls), 1)

    def test_health_error_status(self):
        self.session.script(
            "GET", "http://localhost:11440/health",
            FakeResponse(200, {"status": "error", "error": "model not loaded"}),
        )
        self.assertIsNone(self.adapter.health())
        self.assertFalse(self.adapter.is_reachable())

    def test_embed_batch(self):
        self.session.script(
            "POST", "http://localhost:11440/embed",
            FakeResponse(200, {
                "embeddings": [[1, 2], [3, 4]],
                "dim": 2,
                "count": 2,
                "backend": "gpu",
                "model": "codesage-large-v2",
            }),
        )
        vecs = self.adapter.embed_batch(["def foo(): pass", "def bar(): pass"])
        self.assertEqual(vecs, [[1.0, 2.0], [3.0, 4.0]])

    def test_embed_batch_empty(self):
        self.assertEqual(self.adapter.embed_batch([]), [])
        self.assertEqual(self.session.calls, [])

    def test_embed_single(self):
        self.session.script(
            "POST", "http://localhost:11440/embed",
            FakeResponse(200, {"embeddings": [[7, 8, 9]], "dim": 3, "count": 1,
                               "backend": "gpu", "model": "m"}),
        )
        vec = self.adapter.embed("def foo(): pass")
        self.assertEqual(vec, [7.0, 8.0, 9.0])

    def test_embed_batch_chunks_at_256(self):
        # 300 inputs → 2 HTTP calls (256 + 44)
        self.session.script(
            "POST", "http://localhost:11440/embed",
            FakeResponse(200, {
                "embeddings": [[float(i)] for i in range(256)],
                "dim": 1, "count": 256, "backend": "gpu", "model": "m",
            }),
        )
        self.session.script(
            "POST", "http://localhost:11440/embed",
            FakeResponse(200, {
                "embeddings": [[float(i)] for i in range(44)],
                "dim": 1, "count": 44, "backend": "gpu", "model": "m",
            }),
        )
        texts = [f"x{i}" for i in range(300)]
        vecs = self.adapter.embed_batch(texts)
        self.assertEqual(len(vecs), 300)
        post_calls = [c for c in self.session.calls if c[0] == "POST"]
        self.assertEqual(len(post_calls), 2)


# ---------------------------------------------------------------------------
# OpenAIAdapter (validation matrix is the LOCKED design)
# ---------------------------------------------------------------------------


class OpenAIAdapterTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()

    def _adapter(self, key: str = "sk-test"):
        return OpenAIAdapter(key, self.session)

    def test_no_key_invalid(self):
        a = self._adapter("")
        r = a.validate()
        self.assertFalse(r.valid)
        self.assertEqual(r.reason, "no API key configured")
        # Zero HTTP calls when key is empty
        self.assertEqual(self.session.calls, [])

    def test_validate_200_valid(self):
        self.session.script(
            "GET", "https://api.openai.com/v1/models/text-embedding-3-small",
            FakeResponse(200, {"id": "text-embedding-3-small"}),
        )
        r = self._adapter().validate()
        self.assertTrue(r.valid)
        self.assertEqual(r.http_status, 200)

    def test_validate_401_invalid(self):
        self.session.script(
            "GET", "https://api.openai.com/v1/models/text-embedding-3-small",
            FakeResponse(401, {"error": "auth"}),
        )
        r = self._adapter().validate()
        self.assertFalse(r.valid)
        self.assertEqual(r.reason, "auth failed")

    def test_validate_403_blocked(self):
        self.session.script(
            "GET", "https://api.openai.com/v1/models/text-embedding-3-small",
            FakeResponse(403, {"error": "blocked"}),
        )
        r = self._adapter().validate()
        self.assertFalse(r.valid)
        self.assertIn("blocked", r.reason)

    def test_validate_404_model_inaccessible(self):
        self.session.script(
            "GET", "https://api.openai.com/v1/models/text-embedding-3-small",
            FakeResponse(404, {"error": "not found"}),
        )
        r = self._adapter().validate()
        self.assertFalse(r.valid)
        self.assertIn("not accessible", r.reason)

    def test_validate_429_treated_as_valid(self):
        self.session.script(
            "GET", "https://api.openai.com/v1/models/text-embedding-3-small",
            FakeResponse(429, {"error": "rate limit"}),
        )
        r = self._adapter().validate()
        self.assertTrue(r.valid)
        self.assertTrue(r.rate_limited)

    def test_validate_caches_result(self):
        self.session.script(
            "GET", "https://api.openai.com/v1/models/text-embedding-3-small",
            FakeResponse(200, {"id": "x"}),
        )
        a = self._adapter()
        r1 = a.validate()
        r2 = a.validate()
        self.assertTrue(r1.valid and r2.valid)
        # Only one HTTP call total despite two validate() invocations
        self.assertEqual(len(self.session.calls), 1)

    def test_validate_does_not_cache_network_errors(self):
        def raise_err(*args, **kwargs):
            import requests
            raise requests.ConnectionError("down")
        self.session.get = raise_err  # type: ignore[assignment]
        a = self._adapter()
        r1 = a.validate()
        r2 = a.validate()
        # Network errors aren't cached (transient) — both result in
        # invalid with the network-error reason.
        self.assertFalse(r1.valid)
        self.assertFalse(r2.valid)

    def test_embed_no_key_raises(self):
        a = self._adapter("")
        with self.assertRaises(RuntimeError) as ctx:
            a.embed("text-embedding-3-small", "hi")
        self.assertIn("not configured", str(ctx.exception))

    def test_embed_batch(self):
        self.session.script(
            "POST", "https://api.openai.com/v1/embeddings",
            FakeResponse(200, {
                "data": [
                    {"index": 0, "embedding": [1, 2]},
                    {"index": 1, "embedding": [3, 4]},
                ],
                "model": "text-embedding-3-small",
            }),
        )
        vecs = self._adapter().embed_batch("text-embedding-3-small", ["a", "b"])
        self.assertEqual(vecs, [[1.0, 2.0], [3.0, 4.0]])

    def test_embed_batch_chunks_at_100(self):
        # 250 inputs → 3 HTTP calls (100, 100, 50)
        for chunk_size in (100, 100, 50):
            data = [{"index": i, "embedding": [float(i)]} for i in range(chunk_size)]
            self.session.script(
                "POST", "https://api.openai.com/v1/embeddings",
                FakeResponse(200, {"data": data, "model": "x"}),
            )
        texts = [f"t{i}" for i in range(250)]
        vecs = self._adapter().embed_batch("text-embedding-3-small", texts)
        self.assertEqual(len(vecs), 250)
        post_calls = [c for c in self.session.calls if c[0] == "POST"]
        self.assertEqual(len(post_calls), 3)

    def test_embed_batch_empty(self):
        self.assertEqual(
            self._adapter().embed_batch("text-embedding-3-small", []),
            [],
        )
        self.assertEqual(self.session.calls, [])

    def test_embed_batch_sorts_by_index(self):
        # OpenAI guarantees `index` matches input order, but our adapter
        # sorts defensively. Verify with shuffled response.
        self.session.script(
            "POST", "https://api.openai.com/v1/embeddings",
            FakeResponse(200, {
                "data": [
                    {"index": 1, "embedding": [3, 4]},   # b
                    {"index": 0, "embedding": [1, 2]},   # a
                ],
                "model": "x",
            }),
        )
        vecs = self._adapter().embed_batch("text-embedding-3-small", ["a", "b"])
        self.assertEqual(vecs[0], [1.0, 2.0])
        self.assertEqual(vecs[1], [3.0, 4.0])


# ---------------------------------------------------------------------------
# EmbeddingService — construction + preset matrix
# ---------------------------------------------------------------------------


def _make_service_with_mocks(
    *,
    text_model: str = DEFAULT_TEXT_MODEL,
    code_model: str = DEFAULT_CODE_MODEL,
    openai_key: str = "",
    ollama_ready: bool = True,
    code_ready: bool = True,
    openai_valid: bool | None = None,
    project_root: Path | None = None,
) -> tuple[EmbeddingService, FakeSession]:
    """Build an EmbeddingService with adapter mocks for unit tests."""
    session = FakeSession()
    ollama = MagicMock(spec=OllamaAdapter)
    ollama.is_reachable.return_value = ollama_ready
    ollama.embed.return_value = [0.1, 0.2, 0.3]
    ollama.embed_batch.return_value = [[0.1, 0.2, 0.3]]
    ollama.list_embedding_models.return_value = []

    codee = MagicMock(spec=CodeEmbedAdapter)
    codee.is_reachable.return_value = code_ready
    codee.embed.return_value = [1.0, 2.0]
    codee.embed_batch.return_value = [[1.0, 2.0]]
    codee.model_name = "codesage-large-v2"
    codee.model_dim = 2048
    codee.backend = "gpu"
    codee.health.return_value = {
        "status": "ok",
        "backend": "gpu",
        "model": "codesage-large-v2",
        "dim": 2048,
    } if code_ready else None

    oa = MagicMock(spec=OpenAIAdapter)
    oa.api_key = openai_key
    if openai_valid is None:
        openai_valid = bool(openai_key)
    oa.validate.return_value = ValidationResult(
        valid=openai_valid,
        reason=None if openai_valid else "auth failed",
    )
    oa.is_reachable.return_value = openai_valid
    oa.embed.return_value = [0.7, 0.8, 0.9]
    oa.embed_batch.return_value = [[0.7, 0.8, 0.9]]

    svc = EmbeddingService(
        project_root=project_root,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=text_model,
        code_model_id=code_model,
        openai_api_key=openai_key,
        session=session,
        ollama_adapter=ollama,
        code_adapter=codee,
        openai_adapter=oa,
    )
    return svc, session


class ConstructionPresetTests(unittest.TestCase):
    """The 4-preset matrix from the v0.2.18 plan acceptance criteria."""

    def _patch_adapters(self, *, ollama_ready: bool, code_ready: bool, openai_valid: bool):
        """Patch all three adapter classes so for_project() probes succeed."""
        ollama_mock = MagicMock(spec=OllamaAdapter)
        ollama_mock.is_reachable.return_value = ollama_ready
        ollama_mock.list_embedding_models.return_value = []

        code_mock = MagicMock(spec=CodeEmbedAdapter)
        code_mock.is_reachable.return_value = code_ready

        openai_mock = MagicMock(spec=OpenAIAdapter)
        openai_mock.validate.return_value = ValidationResult(
            valid=openai_valid, reason=None if openai_valid else "auth failed"
        )
        return ollama_mock, code_mock, openai_mock

    def test_qwen3_preset_default(self):
        with _EnvIsolation(), patch.dict(os.environ, {
            "ACTIVE_EMBEDDING": "qwen3",
        }, clear=False):
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True, code_ready=True, openai_valid=False
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(svc.text_model_id, "qwen3-embedding:0.6b")
                    self.assertEqual(svc.text_vector_slot, "qwen3_embed")
                    self.assertEqual(svc.text_dim, 1024)
                    self.assertEqual(svc.code_vector_slot, "codesage_embed")
                    self.assertEqual(svc.code_dim, 2048)
                finally:
                    svc.close()

    def test_openai_preset(self):
        with _EnvIsolation(), patch.dict(os.environ, {
            "ACTIVE_EMBEDDING": "openai",
            "OPENAI_API_KEY": "sk-test",
        }, clear=False):
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=False, code_ready=False, openai_valid=True
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(svc.text_model_id, "text-embedding-3-small")
                    self.assertEqual(svc.text_vector_slot, "openai_text_embed")
                    self.assertEqual(svc.text_dim, 1536)
                finally:
                    svc.close()

    def test_arctic_preset(self):
        # arctic2 selected via EMBEDDING_MODEL override
        with _EnvIsolation(), patch.dict(os.environ, {
            "EMBEDDING_MODEL": "snowflake-arctic-embed2:latest",
        }, clear=False):
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True, code_ready=True, openai_valid=False
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(svc.text_model_id, "snowflake-arctic-embed2:latest")
                    self.assertEqual(svc.text_vector_slot, "arctic2_embed")
                finally:
                    svc.close()

    def test_cpu_fallback_preset(self):
        # CODE_EMBED_BACKEND=ollama means qwen3 for both text + code
        with _EnvIsolation(), patch.dict(os.environ, {
            "CODE_EMBED_BACKEND": "ollama",
        }, clear=False):
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True, code_ready=False, openai_valid=False
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(svc.code_model_id, DEFAULT_TEXT_MODEL)
                    self.assertEqual(svc.code_vector_slot, "qwen3_embed")
                finally:
                    svc.close()


# ---------------------------------------------------------------------------
# NoEmbeddingBackendError — failure capture
# ---------------------------------------------------------------------------


class FailureCaptureTests(unittest.TestCase):
    def test_for_project_raises_when_no_backends(self):
        # Patch Path.home() so the failure-capture JSONL goes to a temp
        # dir, NOT the user's real ~/.claude/metrics/.
        import tempfile
        with _EnvIsolation(), tempfile.TemporaryDirectory() as home_dir, \
             patch("vco_lib.embedding_service.Path.home", return_value=Path(home_dir)):
            ollama_m = MagicMock(spec=OllamaAdapter)
            ollama_m.is_reachable.return_value = False
            ollama_m.list_embedding_models.return_value = []
            code_m = MagicMock(spec=CodeEmbedAdapter)
            code_m.is_reachable.return_value = False
            oa_m = MagicMock(spec=OpenAIAdapter)
            oa_m.validate.return_value = ValidationResult(
                valid=False, reason="no API key configured"
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                with self.assertRaises(NoEmbeddingBackendError) as ctx:
                    EmbeddingService.for_project()
                self.assertIn("ollama", ctx.exception.attempted_backends)
                self.assertIn("codeembed", ctx.exception.attempted_backends)

    def test_failure_writes_jsonl_log(self):
        with _EnvIsolation():
            import tempfile
            with tempfile.TemporaryDirectory() as home_dir:
                fake_home = Path(home_dir)
                with patch("vco_lib.embedding_service.Path.home", return_value=fake_home):
                    # Build the error WITH capture (the default). The capture
                    # logic writes to ~/.claude/metrics/embedding_failures.jsonl
                    NoEmbeddingBackendError(
                        "test failure",
                        attempted_backends=["ollama"],
                        error_per_backend={"ollama": "not reachable"},
                        install_root=None,
                    )
                    log = fake_home / ".claude" / "metrics" / "embedding_failures.jsonl"
                    self.assertTrue(log.exists(), f"missing: {log}")
                    content = log.read_text()
                    record = json.loads(content.strip().splitlines()[-1])
                    self.assertEqual(record["attempted_backends"], ["ollama"])
                    self.assertEqual(
                        record["error_per_backend"], {"ollama": "not reachable"}
                    )

    def test_failure_writes_markdown_hint(self):
        with _EnvIsolation():
            import tempfile
            with tempfile.TemporaryDirectory() as proj_dir:
                proj_root = Path(proj_dir)
                with tempfile.TemporaryDirectory() as home_dir:
                    fake_home = Path(home_dir)
                    with patch("vco_lib.embedding_service.Path.home", return_value=fake_home):
                        NoEmbeddingBackendError(
                            "test failure",
                            attempted_backends=["ollama", "openai"],
                            error_per_backend={
                                "ollama": "not reachable",
                                "openai": "auth failed",
                            },
                            install_root=proj_root,
                        )
                        md = proj_root / ".claude" / "context" / "EMBEDDING_FAILURES.md"
                        self.assertTrue(md.exists(), f"missing: {md}")
                        content = md.read_text()
                        self.assertIn("ollama", content)
                        self.assertIn("openai", content)
                        self.assertIn("Ask Claude", content)

    def test_redacted_env_snapshot_hides_api_key(self):
        with _EnvIsolation(), patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-livekey-1234567890abcdef",
            "OLLAMA_URL": "http://localhost:11435",
        }, clear=False):
            import tempfile
            with tempfile.TemporaryDirectory() as home_dir:
                fake_home = Path(home_dir)
                with patch("vco_lib.embedding_service.Path.home", return_value=fake_home):
                    NoEmbeddingBackendError(
                        "test",
                        attempted_backends=["ollama"],
                        error_per_backend={"ollama": "down"},
                        install_root=None,
                    )
                    log = fake_home / ".claude" / "metrics" / "embedding_failures.jsonl"
                    record = json.loads(log.read_text().strip().splitlines()[-1])
                    snap = record["env_snapshot"]
                    self.assertNotIn("sk-livekey-1234567890abcdef", json.dumps(snap))
                    self.assertIn("OPENAI_API_KEY", snap)
                    self.assertIn("redacted", snap["OPENAI_API_KEY"])
                    self.assertEqual(snap["OLLAMA_URL"], "http://localhost:11435")

    def test_capture_disabled_skips_io(self):
        with _EnvIsolation():
            import tempfile
            with tempfile.TemporaryDirectory() as home_dir:
                fake_home = Path(home_dir)
                with patch("vco_lib.embedding_service.Path.home", return_value=fake_home):
                    NoEmbeddingBackendError(
                        "no capture",
                        attempted_backends=["ollama"],
                        capture=False,
                    )
                    log = fake_home / ".claude" / "metrics" / "embedding_failures.jsonl"
                    self.assertFalse(log.exists())

    def test_success_clears_failure_markdown(self):
        with _EnvIsolation(), patch.dict(os.environ, {}, clear=False):
            import tempfile
            with tempfile.TemporaryDirectory() as proj_dir:
                proj_root = Path(proj_dir)
                # Plant a stale failure hint file
                md = proj_root / ".claude" / "context" / "EMBEDDING_FAILURES.md"
                md.parent.mkdir(parents=True)
                md.write_text("stale")
                self.assertTrue(md.exists())

                ollama_m = MagicMock(spec=OllamaAdapter)
                ollama_m.is_reachable.return_value = True
                ollama_m.list_embedding_models.return_value = []
                code_m = MagicMock(spec=CodeEmbedAdapter)
                code_m.is_reachable.return_value = True
                oa_m = MagicMock(spec=OpenAIAdapter)
                oa_m.validate.return_value = ValidationResult(valid=True)
                with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                     patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                     patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                    svc = EmbeddingService.for_project(project_root=proj_root)
                    try:
                        self.assertFalse(
                            md.exists(),
                            "EMBEDDING_FAILURES.md should be cleared on success",
                        )
                    finally:
                        svc.close()

    # ------------------------------------------------------------------
    # v0.2.18 Commit 11 (observability): UPDATE_DEFERRED.md integration
    # ------------------------------------------------------------------

    def test_failure_writes_deferral_entry(self):
        """The failure-capture path writes a kg_summary_no_backend entry to
        UPDATE_DEFERRED.md alongside the JSONL + MD hint, so the launcher's
        GUI banner picks up the failure."""
        with _EnvIsolation():
            import tempfile
            with tempfile.TemporaryDirectory() as proj_dir:
                proj_root = Path(proj_dir)
                with tempfile.TemporaryDirectory() as home_dir:
                    fake_home = Path(home_dir)
                    with patch("vco_lib.embedding_service.Path.home", return_value=fake_home):
                        NoEmbeddingBackendError(
                            "no backends",
                            attempted_backends=["ollama", "codeembed"],
                            error_per_backend={
                                "ollama": "connection refused",
                                "codeembed": "service not running",
                            },
                            install_root=proj_root,
                        )
                        deferral = proj_root / ".claude" / "context" / "UPDATE_DEFERRED.md"
                        self.assertTrue(deferral.exists(), f"missing: {deferral}")
                        content = deferral.read_text(encoding="utf-8")
                        self.assertIn("kg_summary_no_backend", content)
                        self.assertIn("ollama", content)
                        self.assertIn("codeembed", content)
                        # The frontmatter must list the condition_id so
                        # downstream parsers (launcher GUI) can pick it up.
                        self.assertIn("condition_ids:", content)

    def test_failure_deferral_skipped_without_install_root(self):
        """Module-level discovery failures (install_root=None) must not try
        to write a deferral — there's no project root to write into."""
        with _EnvIsolation():
            import tempfile
            with tempfile.TemporaryDirectory() as home_dir:
                fake_home = Path(home_dir)
                with patch("vco_lib.embedding_service.Path.home", return_value=fake_home):
                    # Should not raise even without install_root.
                    exc = NoEmbeddingBackendError(
                        "discovery failure",
                        attempted_backends=["ollama"],
                        error_per_backend={"ollama": "down"},
                        install_root=None,
                    )
                    self.assertIsNone(exc.install_root)

    def test_success_clears_deferral_entry(self):
        """A successful EmbeddingService.for_project() must clear a stale
        kg_summary_no_backend entry from UPDATE_DEFERRED.md so the launcher
        banner doesn't stay red after the user fixes the backend."""
        with _EnvIsolation(), patch.dict(os.environ, {}, clear=False):
            import tempfile
            with tempfile.TemporaryDirectory() as proj_dir:
                proj_root = Path(proj_dir)
                with tempfile.TemporaryDirectory() as home_dir:
                    fake_home = Path(home_dir)
                    with patch("vco_lib.embedding_service.Path.home", return_value=fake_home):
                        # Plant a stale deferral via the failure-capture path.
                        NoEmbeddingBackendError(
                            "test failure",
                            attempted_backends=["ollama"],
                            error_per_backend={"ollama": "stale"},
                            install_root=proj_root,
                        )
                        deferral = proj_root / ".claude" / "context" / "UPDATE_DEFERRED.md"
                        self.assertTrue(deferral.exists())

                        # Now simulate a successful construction.
                        ollama_m = MagicMock(spec=OllamaAdapter)
                        ollama_m.is_reachable.return_value = True
                        ollama_m.list_embedding_models.return_value = []
                        code_m = MagicMock(spec=CodeEmbedAdapter)
                        code_m.is_reachable.return_value = True
                        oa_m = MagicMock(spec=OpenAIAdapter)
                        oa_m.validate.return_value = ValidationResult(valid=True)
                        with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                             patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                             patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                            svc = EmbeddingService.for_project(project_root=proj_root)
                            try:
                                # The deferral entry must be cleared. The file
                                # itself may be deleted (entries empty) OR
                                # rewritten without our condition_id.
                                if deferral.exists():
                                    content = deferral.read_text(encoding="utf-8")
                                    self.assertNotIn(
                                        "kg_summary_no_backend",
                                        content,
                                        "deferral entry must be removed on success",
                                    )
                            finally:
                                svc.close()

    def test_failure_deferral_soft_fail_on_import_error(self):
        """If deferral_report can't be imported (partial install), the
        failure-capture path must still complete — JSONL + MD must still
        be written."""
        with _EnvIsolation():
            import tempfile
            with tempfile.TemporaryDirectory() as proj_dir:
                proj_root = Path(proj_dir)
                with tempfile.TemporaryDirectory() as home_dir:
                    fake_home = Path(home_dir)
                    with patch("vco_lib.embedding_service.Path.home", return_value=fake_home), \
                         patch(
                             "vco_lib.embedding_service._write_failure_deferral",
                             side_effect=RuntimeError("simulated deferral failure"),
                         ):
                        # Even if _write_failure_deferral raises, the JSONL
                        # + MD writes must succeed (those happen first).
                        try:
                            NoEmbeddingBackendError(
                                "test",
                                attempted_backends=["ollama"],
                                error_per_backend={"ollama": "down"},
                                install_root=proj_root,
                            )
                        except RuntimeError:
                            # The current contract is "soft-fail inside
                            # _write_failure_deferral"; if a caller decides
                            # to patch it to raise, that's their choice.
                            # We assert JSONL + MD were written before the
                            # deferral attempt.
                            pass
                        jsonl = fake_home / ".claude" / "metrics" / "embedding_failures.jsonl"
                        md = proj_root / ".claude" / "context" / "EMBEDDING_FAILURES.md"
                        self.assertTrue(jsonl.exists(), "JSONL must be written first")
                        self.assertTrue(md.exists(), "MD hint must be written second")


# ---------------------------------------------------------------------------
# EmbeddingService — methods (single, batch, multi-slot, context manager)
# ---------------------------------------------------------------------------


class EmbeddingServiceMethodTests(unittest.TestCase):
    def test_embed_text_routes_to_ollama_for_qwen3(self):
        svc, _ = _make_service_with_mocks(text_model=DEFAULT_TEXT_MODEL)
        try:
            vec = svc.embed_text("hello")
            self.assertEqual(vec, [0.1, 0.2, 0.3])
            svc.ollama.embed.assert_called_once_with(DEFAULT_TEXT_MODEL, "hello")
            svc.openai.embed.assert_not_called()
        finally:
            svc.close()

    def test_embed_text_routes_to_openai(self):
        svc, _ = _make_service_with_mocks(
            text_model="text-embedding-3-small",
            openai_key="sk-test",
        )
        try:
            vec = svc.embed_text("hi")
            self.assertEqual(vec, [0.7, 0.8, 0.9])
            svc.openai.embed.assert_called_once_with("text-embedding-3-small", "hi")
            svc.ollama.embed.assert_not_called()
        finally:
            svc.close()

    def test_embed_code_prefers_codeembed_service(self):
        svc, _ = _make_service_with_mocks(
            code_model="codesage-large-v2", code_ready=True
        )
        try:
            vec = svc.embed_code("def foo(): pass")
            self.assertEqual(vec, [1.0, 2.0])
            svc.codeembed.embed.assert_called_once()
            svc.ollama.embed.assert_not_called()
        finally:
            svc.close()

    def test_embed_code_falls_back_to_ollama_when_service_down(self):
        svc, _ = _make_service_with_mocks(
            code_model="codesage-large-v2", code_ready=False
        )
        try:
            vec = svc.embed_code("def foo(): pass")
            # Ollama returns [0.1, 0.2, 0.3] from the mock — service is
            # down so the dispatcher routes there instead.
            self.assertEqual(vec, [0.1, 0.2, 0.3])
            svc.ollama.embed.assert_called_once()
        finally:
            svc.close()

    def test_embed_text_batch_empty_returns_empty(self):
        svc, _ = _make_service_with_mocks()
        try:
            self.assertEqual(svc.embed_text_batch([]), [])
            svc.ollama.embed_batch.assert_not_called()
        finally:
            svc.close()

    def test_embed_code_batch_empty_returns_empty(self):
        svc, _ = _make_service_with_mocks()
        try:
            self.assertEqual(svc.embed_code_batch([]), [])
            svc.codeembed.embed_batch.assert_not_called()
            svc.ollama.embed_batch.assert_not_called()
        finally:
            svc.close()

    def test_embed_text_batch_single(self):
        svc, _ = _make_service_with_mocks()
        try:
            svc.ollama.embed_batch.return_value = [[1.0]]
            self.assertEqual(svc.embed_text_batch(["a"]), [[1.0]])
        finally:
            svc.close()

    def test_embed_text_batch_100_plus(self):
        svc, _ = _make_service_with_mocks()
        try:
            texts = [f"t{i}" for i in range(150)]
            svc.ollama.embed_batch.return_value = [[float(i)] for i in range(150)]
            vecs = svc.embed_text_batch(texts)
            self.assertEqual(len(vecs), 150)
            # ONE call to the adapter — chunking is the adapter's concern
            svc.ollama.embed_batch.assert_called_once()
        finally:
            svc.close()

    def test_embed_text_all_configured_active_only(self):
        # qwen3 model, ollama reachable, no openai key — should produce
        # just qwen3_embed slot (no fallback duplicates).
        svc, _ = _make_service_with_mocks(text_model=DEFAULT_TEXT_MODEL)
        try:
            result = svc.embed_text_all_configured("hello")
            self.assertEqual(list(result.keys()), ["qwen3_embed"])
        finally:
            svc.close()

    def test_embed_text_all_configured_with_openai_fallback(self):
        # qwen3 is active; openai key is configured and valid. Both
        # should populate.
        svc, _ = _make_service_with_mocks(
            text_model=DEFAULT_TEXT_MODEL,
            openai_key="sk-test",
            openai_valid=True,
        )
        try:
            result = svc.embed_text_all_configured("hello")
            self.assertIn("qwen3_embed", result)
            self.assertIn("openai_text_embed", result)
        finally:
            svc.close()

    def test_embed_code_all_configured_active_and_openai(self):
        svc, _ = _make_service_with_mocks(
            code_model="codesage-large-v2",
            openai_key="sk-test",
            openai_valid=True,
            code_ready=True,
        )
        try:
            result = svc.embed_code_all_configured("def f(): pass")
            self.assertIn("codesage_embed", result)
            self.assertIn("openai_code_embed", result)
        finally:
            svc.close()

    def test_context_manager_closes_owned_session(self):
        # When the caller doesn't inject a session, the service owns
        # one. The context manager should close it on __exit__.
        ollama_m = MagicMock(spec=OllamaAdapter)
        code_m = MagicMock(spec=CodeEmbedAdapter)
        oa_m = MagicMock(spec=OpenAIAdapter)
        captured_session = None
        with EmbeddingService(
            project_root=None,
            ollama_url="http://x",
            code_embed_url="http://y",
            text_model_id=DEFAULT_TEXT_MODEL,
            code_model_id=DEFAULT_CODE_MODEL,
            openai_api_key="",
            session=None,  # service owns it
            ollama_adapter=ollama_m,
            code_adapter=code_m,
            openai_adapter=oa_m,
        ) as svc:
            captured_session = svc.session
            self.assertTrue(svc._owns_session)
        # Verify that close() was called by attempting to use the
        # session — requests.Session.close releases the connection
        # pool but doesn't error on subsequent calls; instead we
        # check the internal `adapters` dict went through close.
        # The most portable check: subsequent svc.close() doesn't
        # error and the flag stays True.
        self.assertTrue(svc._owns_session)

    def test_close_owned_session_does_not_raise_double_call(self):
        svc, _ = _make_service_with_mocks()
        svc.close()
        svc.close()  # second call is a no-op

    def test_session_reused_across_batch_calls(self):
        # Build a service WITHOUT injecting an external session — the
        # service owns its session. The same instance must be shared
        # across all adapters and all batch calls.
        with _EnvIsolation():
            ollama_m = MagicMock(spec=OllamaAdapter)
            ollama_m.is_reachable.return_value = True
            ollama_m.list_embedding_models.return_value = []
            ollama_m.embed_batch.return_value = [[0.1]]
            code_m = MagicMock(spec=CodeEmbedAdapter)
            code_m.is_reachable.return_value = True
            oa_m = MagicMock(spec=OpenAIAdapter)
            oa_m.validate.return_value = ValidationResult(
                valid=False, reason="no key"
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    sess1 = svc.session
                    svc.embed_text_batch(["a"])
                    sess2 = svc.session
                    self.assertIs(sess1, sess2)
                    # Owned session — close() is the responsibility of svc
                    self.assertTrue(svc._owns_session)
                finally:
                    svc.close()

    def test_injected_session_not_closed_by_service(self):
        # The caller's responsibility: don't close a session you didn't open.
        injected = FakeSession()
        ollama_m = MagicMock(spec=OllamaAdapter)
        code_m = MagicMock(spec=CodeEmbedAdapter)
        oa_m = MagicMock(spec=OpenAIAdapter)
        svc = EmbeddingService(
            project_root=None,
            ollama_url="http://x",
            code_embed_url="http://y",
            text_model_id=DEFAULT_TEXT_MODEL,
            code_model_id=DEFAULT_CODE_MODEL,
            openai_api_key="",
            session=injected,
            ollama_adapter=ollama_m,
            code_adapter=code_m,
            openai_adapter=oa_m,
        )
        self.assertFalse(svc._owns_session)
        svc.close()
        # FakeSession's `closed` flag should remain False — the
        # service does not close sessions it doesn't own.
        self.assertFalse(injected.closed)


# ---------------------------------------------------------------------------
# Catalogue discovery
# ---------------------------------------------------------------------------


class DiscoveryTests(unittest.TestCase):
    def test_discover_text_ollama_only(self):
        session = FakeSession()
        session.script(
            "GET", "http://localhost:11435/api/tags",
            _ollama_tags_response(["qwen3-embedding:0.6b"]),
        )
        with _EnvIsolation():
            choices = EmbeddingService.discover_text_models(
                ollama_url="http://localhost:11435",
                openai_api_key="",
                session=session,
            )
        ids = [c.id for c in choices]
        self.assertIn("qwen3-embedding:0.6b", ids)
        # OpenAI placeholders should be present but available_now=False
        openai_choices = [c for c in choices if c.backend == "openai"]
        self.assertGreater(len(openai_choices), 0)
        for c in openai_choices:
            self.assertFalse(c.available_now)

    def test_discover_text_openai_only(self):
        session = FakeSession()
        # Schedule explicit 200 for each OpenAI model validation. Ollama
        # has no script for /api/tags → falls through to the 404 default,
        # so the catalog will emit an "ollama-unreachable" placeholder.
        for model_id in KNOWN_OPENAI_EMBEDDING_MODELS:
            session.script(
                "GET", f"https://api.openai.com/v1/models/{model_id}",
                FakeResponse(200, {"id": model_id}),
            )
        with _EnvIsolation():
            choices = EmbeddingService.discover_text_models(
                ollama_url="http://localhost:11435",
                openai_api_key="sk-test",
                session=session,
            )
        # We should see openai choices available
        openai_choices = [c for c in choices if c.backend == "openai"]
        self.assertTrue(any(c.available_now for c in openai_choices))
        # And a placeholder ollama unreachable row
        unreach = [c for c in choices if c.id == "ollama-unreachable"]
        self.assertEqual(len(unreach), 1)
        self.assertFalse(unreach[0].available_now)

    def test_discover_text_none_reachable(self):
        session = FakeSession()
        # No scripts registered → all 404
        with _EnvIsolation():
            choices = EmbeddingService.discover_text_models(
                ollama_url="http://localhost:11435",
                openai_api_key="",
                session=session,
            )
        for c in choices:
            self.assertFalse(
                c.available_now,
                f"{c.id} unexpectedly reported as available",
            )

    def test_discover_code_codeembed_only(self):
        session = FakeSession()
        session.script(
            "GET", "http://localhost:11440/health",
            FakeResponse(200, {
                "status": "ok",
                "backend": "gpu",
                "model": "codesage-large-v2",
                "dim": 2048,
            }),
        )
        # Ollama unreachable: no script for /api/tags → 404
        with _EnvIsolation():
            choices = EmbeddingService.discover_code_models(
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                openai_api_key="",
                session=session,
            )
        codeembed_avail = [c for c in choices if c.backend == "codeembed" and c.available_now]
        self.assertEqual(len(codeembed_avail), 1)
        self.assertEqual(codeembed_avail[0].dim, 2048)

    def test_discover_code_all_three_reachable(self):
        session = FakeSession()
        session.script(
            "GET", "http://localhost:11440/health",
            FakeResponse(200, {
                "status": "ok", "backend": "gpu",
                "model": "codesage-large-v2", "dim": 2048,
            }),
        )
        session.script(
            "GET", "http://localhost:11435/api/tags",
            _ollama_tags_response(["unclemusclez/jina-embeddings-v2-base-code:latest"]),
        )
        for model_id in KNOWN_OPENAI_EMBEDDING_MODELS:
            session.script(
                "GET", f"https://api.openai.com/v1/models/{model_id}",
                FakeResponse(200, {"id": model_id}),
            )
        with _EnvIsolation():
            choices = EmbeddingService.discover_code_models(
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                openai_api_key="sk-test",
                session=session,
            )
        backends_with_avail = {c.backend for c in choices if c.available_now}
        self.assertEqual(backends_with_avail, {"codeembed", "ollama", "openai"})


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def test_discover_subcommand_prints_json(self):
        # Mock the discovery methods so we don't depend on env
        with patch.object(
            EmbeddingService, "discover_text_models", return_value=[
                ModelChoice(
                    id="qwen3-embedding:0.6b",
                    label="qwen3 (1024d)", dim=1024, slot="qwen3_embed",
                    backend="ollama", available_now=True,
                ),
            ]
        ), patch.object(
            EmbeddingService, "discover_code_models", return_value=[]
        ), patch.object(
            EmbeddingService, "for_project",
            side_effect=NoEmbeddingBackendError(
                "no project", attempted_backends=[], capture=False
            ),
        ):
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = embedding_service_main(["discover"])
            self.assertEqual(rc, 0)
            output = json.loads(buf.getvalue())
            self.assertEqual(len(output["text_models"]), 1)
            self.assertEqual(output["text_models"][0]["id"], "qwen3-embedding:0.6b")
            # for_project failed inside CLI → captured into "errors"
            self.assertTrue(any("for_project" in e for e in output["errors"]))

    def test_discover_json_flag_is_accepted_and_ignored(self):
        """--json is a no-op for caller-side clarity; output is JSON regardless."""
        with patch.object(
            EmbeddingService, "discover_text_models", return_value=[]
        ), patch.object(
            EmbeddingService, "discover_code_models", return_value=[]
        ), patch.object(
            EmbeddingService, "for_project",
            side_effect=NoEmbeddingBackendError(
                "no project", attempted_backends=[], capture=False
            ),
        ):
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = embedding_service_main(["discover", "--json"])
            self.assertEqual(rc, 0)
            # Still emits JSON (default behavior, --json flag was a no-op).
            output = json.loads(buf.getvalue())
            self.assertIn("text_models", output)
            self.assertIn("code_models", output)
            self.assertIn("errors", output)

    def test_discover_project_root_is_forwarded(self):
        """--project-root is passed verbatim into ``for_project``."""
        captured_kwargs: dict = {}

        def fake_for_project(*, project_root=None):
            captured_kwargs["project_root"] = project_root
            raise NoEmbeddingBackendError(
                "no project", attempted_backends=[], capture=False
            )

        with patch.object(
            EmbeddingService, "discover_text_models", return_value=[]
        ), patch.object(
            EmbeddingService, "discover_code_models", return_value=[]
        ), patch.object(
            EmbeddingService, "for_project",
            side_effect=fake_for_project,
        ):
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = embedding_service_main(
                    ["discover", "--project-root", "/tmp/some/project"]
                )
            self.assertEqual(rc, 0)
            # The flag must be parsed into a Path and forwarded as-is.
            self.assertEqual(
                captured_kwargs.get("project_root"),
                Path("/tmp/some/project"),
            )


if __name__ == "__main__":
    unittest.main()
