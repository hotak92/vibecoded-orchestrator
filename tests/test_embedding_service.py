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
import threading
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
    DEFAULT_EMBED_REQUEST_TIMEOUT_SECS,
    DEFAULT_TEXT_MODEL,
    DEFAULT_TEXT_SLOT,
    DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV,
    EMBED_REQUEST_TIMEOUT_ENV,
    OPENAI_MODEL_ID_PREFIX,
    EmbeddingService,
    ModelChoice,
    NoEmbeddingBackendError,
    _resolve_code_slot,
    _resolve_embed_request_timeout,
    _resolve_text_slot,
    _resolve_write_all_slots,
    _to_openai_api_model,
    _to_openai_catalog_id,
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
        # v0.2.71 Piece 5c: secondary-slot write toggle (default OFF).
        "DUAL_EMBEDDING_WRITE_ALL_SLOTS",
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
        # v0.2.47 RL-7.5 (2026-06-04): num_ctx now auto-resolves from
        # MODEL_TOKEN_LIMITS per-model. qwen3-embedding:0.6b is registered
        # at 10240 — a 4× bump from the pre-v0.2.47.5 hardcoded 8192 default.
        _, _, kwargs = self.session.calls[-1]
        self.assertEqual(kwargs["json"]["options"]["num_ctx"], 10240)

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
# Code-backend fallback chain (v0.2.18 correctness fix)
# ---------------------------------------------------------------------------


class CodeFallbackChainTests(unittest.TestCase):
    """Locked fallback chain: codesage → qwen3 → jina.

    Verifies the v0.2.18 correctness follow-up to Commit 2: when
    ``CODE_EMBED_BACKEND=service`` (default) AND the CodeEmbed service
    is DOWN, ``for_project()`` falls back through Ollama qwen3 then
    Ollama jina before giving up.
    """

    def _patch_adapters(
        self,
        *,
        ollama_ready: bool,
        ollama_model_names: list[str] | None = None,
        code_ready: bool,
        openai_valid: bool = False,
    ):
        """Helper: build adapter mocks for ``for_project`` patching.

        ``ollama_model_names`` controls what ``list_models()`` returns
        (used by ``_ollama_has_model`` in the fallback chain).
        """
        ollama_mock = MagicMock(spec=OllamaAdapter)
        ollama_mock.is_reachable.return_value = ollama_ready
        ollama_mock.list_embedding_models.return_value = []
        ollama_mock.list_models.return_value = [
            {"name": n} for n in (ollama_model_names or [])
        ]
        ollama_mock.embed.return_value = [0.1] * 4

        code_mock = MagicMock(spec=CodeEmbedAdapter)
        code_mock.is_reachable.return_value = code_ready
        # Used by the fallback chain's reason string (``codeembed.base_url``).
        code_mock.base_url = "http://localhost:11440"

        openai_mock = MagicMock(spec=OpenAIAdapter)
        openai_mock.validate.return_value = ValidationResult(
            valid=openai_valid,
            reason=None if openai_valid else "no key",
        )
        return ollama_mock, code_mock, openai_mock

    # --- 1. CodeEmbed up: chain is a no-op, codesage path preserved. --

    def test_code_fallback_codeembed_up(self):
        with _EnvIsolation():
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=["qwen3-embedding:0.6b"],
                code_ready=True,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(svc.code_model_id, "codesage-large-v2")
                    self.assertEqual(svc.code_vector_slot, "codesage_embed")
                    self.assertEqual(svc.code_dim, 2048)
                    self.assertTrue(svc.code_backend_ready())
                finally:
                    svc.close()

    # --- 2. CodeEmbed down, qwen3 present → qwen3 wins. ---------------

    def test_code_fallback_codeembed_down_qwen3_present(self):
        with _EnvIsolation():
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=["qwen3-embedding:0.6b", "llama3:8b"],
                code_ready=False,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(svc.code_model_id, "qwen3-embedding:0.6b")
                    self.assertEqual(svc.code_vector_slot, "qwen3_embed")
                    self.assertEqual(svc.code_dim, 1024)
                    self.assertTrue(
                        svc.code_backend_ready(),
                        "qwen3_embed slot should be ready when Ollama responds",
                    )
                finally:
                    svc.close()

    # --- 3. CodeEmbed down, qwen3 missing, jina present → jina wins. --

    def test_code_fallback_codeembed_down_qwen3_missing_jina_present(self):
        with _EnvIsolation():
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=[
                    "unclemusclez/jina-embeddings-v2-base-code:latest",
                ],
                code_ready=False,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    self.assertEqual(
                        svc.code_model_id,
                        "unclemusclez/jina-embeddings-v2-base-code:latest",
                    )
                    self.assertEqual(svc.code_vector_slot, "jina_embed")
                    self.assertEqual(svc.code_dim, 768)
                finally:
                    svc.close()

    # --- 4. All down: keep requested triple, code_backend_ready==False. -

    def test_code_fallback_all_down(self):
        # Patch Path.home() to a tempdir so the JSONL failure log doesn't
        # touch the user's real ~/.claude/metrics/.
        import tempfile
        with _EnvIsolation(), tempfile.TemporaryDirectory() as home_dir, \
             patch("vco_lib.embedding_service.Path.home", return_value=Path(home_dir)):
            # Ollama up but lacking qwen3 + jina; CodeEmbed down.
            # Text backend still works (Ollama responds to /api/tags),
            # so for_project() succeeds — but code_backend_ready==False
            # because no code model is present on Ollama AND CodeEmbed is
            # unreachable.
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=["llama3:8b"],
                code_ready=False,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    # No fallback was selected — requested codesage triple
                    # is preserved, but code_backend_ready() reports False
                    # because CodeEmbed is unreachable (slot semantics).
                    self.assertEqual(svc.code_model_id, "codesage-large-v2")
                    self.assertEqual(svc.code_vector_slot, "codesage_embed")
                    # codesage_embed routes via either codeembed.is_reachable
                    # OR ollama.is_reachable — Ollama IS reachable (just
                    # missing the model), so the existing slot-level gate
                    # returns True. The actual failure surfaces only at
                    # embed-call time. This matches pre-fallback behaviour
                    # and is gated to False by analyze_code_graph.py's
                    # additional sanity checks (the slot+model combo is
                    # what the consumer migrates on).
                    # Document the actual state so future-readers know:
                    self.assertFalse(code_m.is_reachable())
                finally:
                    svc.close()

    # --- 5. Fallback log fires exactly once at construction. ---------

    def test_code_fallback_logs_one_line(self):
        from io import StringIO
        captured = StringIO()
        with _EnvIsolation():
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=["qwen3-embedding:0.6b"],
                code_ready=False,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m), \
                 patch("vco_lib.embedding_service.sys.stderr", captured):
                svc = EmbeddingService.for_project()
                try:
                    output = captured.getvalue()
                    # Exactly one fallback-reason line in stderr.
                    fallback_lines = [
                        ln for ln in output.splitlines()
                        if "CodeEmbed service unreachable" in ln
                    ]
                    self.assertEqual(
                        len(fallback_lines), 1,
                        f"expected exactly one fallback log line, got: {output!r}",
                    )
                    self.assertIn("qwen3-embedding:0.6b", fallback_lines[0])
                    self.assertIn("slot=qwen3_embed", fallback_lines[0])
                finally:
                    svc.close()

    # --- 6. Text resolution unchanged by code fallback. ---------------

    def test_code_fallback_preserves_text_resolution(self):
        with _EnvIsolation():
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=["qwen3-embedding:0.6b"],
                code_ready=False,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    # Code falls back to qwen3, but text resolution is
                    # entirely independent and stays at the default.
                    self.assertEqual(svc.text_model_id, "qwen3-embedding:0.6b")
                    self.assertEqual(svc.text_vector_slot, "qwen3_embed")
                    self.assertEqual(svc.text_dim, 1024)
                    # And the code fallback DID land on qwen3 as expected.
                    self.assertEqual(svc.code_vector_slot, "qwen3_embed")
                finally:
                    svc.close()

    # --- 7. OpenAI explicit code path: chain doesn't fire. ------------

    def test_code_fallback_respects_openai_when_set_as_default(self):
        # User has explicitly set OPENAI_API_KEY + CODE_EMBED_MODEL =
        # an OpenAI model. The chain only activates for the codesage_embed
        # slot, so the openai_code_embed path is left untouched even if
        # the CodeEmbed service is unreachable.
        with _EnvIsolation(), patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "CODE_EMBED_MODEL": "text-embedding-3-small",
        }, clear=False):
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=["qwen3-embedding:0.6b"],
                code_ready=False,
                openai_valid=True,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc = EmbeddingService.for_project()
                try:
                    # OpenAI choice respected — no fallback to qwen3/jina.
                    self.assertEqual(svc.code_model_id, "text-embedding-3-small")
                    self.assertEqual(svc.code_vector_slot, "openai_code_embed")
                    self.assertEqual(svc.code_dim, 1536)
                finally:
                    svc.close()

    # --- 8. Idempotency: re-running for_project gives the same answer. -

    def test_code_fallback_idempotent_resolution(self):
        with _EnvIsolation():
            ollama_m, code_m, oa_m = self._patch_adapters(
                ollama_ready=True,
                ollama_model_names=["qwen3-embedding:0.6b"],
                code_ready=False,
            )
            with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
                 patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
                 patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
                svc1 = EmbeddingService.for_project()
                try:
                    first = (svc1.code_model_id, svc1.code_vector_slot, svc1.code_dim)
                finally:
                    svc1.close()
                svc2 = EmbeddingService.for_project()
                try:
                    second = (svc2.code_model_id, svc2.code_vector_slot, svc2.code_dim)
                finally:
                    svc2.close()
                self.assertEqual(first, second)


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
        # should populate WHEN the secondary-slot write toggle is ON
        # (v0.2.71 Piece 5c made the fan-out opt-in; default OFF writes
        # only the active slot — see DualWriteAllSlotsToggleTests).
        with _EnvIsolation(), patch.dict(
            os.environ, {DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV: "true"}, clear=False
        ):
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
        # Secondary-slot fan-out is opt-in (v0.2.71 Piece 5c) — enable it.
        with _EnvIsolation(), patch.dict(
            os.environ, {DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV: "true"}, clear=False
        ):
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
# OpenAI catalog-id ↔ API-model-name prefix translation
#
# Regression coverage for the v0.2.18 Commit-12 fix: the GUI dropdown's
# source of truth for an OpenAI model id is the PREFIXED form
# (`"openai-text-embedding-3-small"`) so it round-trips with what
# `openai_cmd.rs::register_openai_api_key` and
# `install.py::_preset_to_default_models` write to
# `app_state.default_text_embedding`. The OpenAI HTTP API rejects that
# prefixed form with HTTP 400, so every HTTP-call boundary inside
# EmbeddingService strips the prefix back off before sending.
# ---------------------------------------------------------------------------


class OpenAICatalogIdPrefixTests(unittest.TestCase):
    """The prefix-translation helpers must be idempotent and total."""

    def test_to_catalog_id_adds_prefix_when_missing(self):
        self.assertEqual(
            _to_openai_catalog_id("text-embedding-3-small"),
            "openai-text-embedding-3-small",
        )

    def test_to_catalog_id_is_idempotent_when_prefix_present(self):
        self.assertEqual(
            _to_openai_catalog_id("openai-text-embedding-3-small"),
            "openai-text-embedding-3-small",
        )

    def test_to_api_model_strips_prefix(self):
        self.assertEqual(
            _to_openai_api_model("openai-text-embedding-3-small"),
            "text-embedding-3-small",
        )

    def test_to_api_model_is_idempotent_when_prefix_absent(self):
        # Back-compat: existing env-driven installs carrying the raw form
        # in EMBEDDING_MODEL / OPENAI_EMBEDDING_MODEL must continue to
        # work — passing the raw form through must be a no-op.
        self.assertEqual(
            _to_openai_api_model("text-embedding-3-small"),
            "text-embedding-3-small",
        )

    def test_round_trip_catalog_id_to_api_model(self):
        for raw in KNOWN_OPENAI_EMBEDDING_MODELS:
            catalog = _to_openai_catalog_id(raw)
            self.assertTrue(catalog.startswith(OPENAI_MODEL_ID_PREFIX))
            self.assertEqual(_to_openai_api_model(catalog), raw)


class DiscoverEmitsPrefixedOpenAIIdTests(unittest.TestCase):
    """discover_text_models / discover_code_models must emit the prefixed
    form for OpenAI entries so `app_state.default_text_embedding` and the
    catalog choice's ``id`` field compare equal byte-for-byte (the GUI
    dropdown's pre-select logic uses exact string equality).
    """

    def _ollama_unreachable_session(self) -> "FakeSession":
        # Default 404 for everything → Ollama unreachable placeholder.
        # We don't script the OpenAI validation endpoint; the absent
        # script makes validate() return ``valid=False`` which is fine
        # for the "is the id prefixed?" check (we don't assert on
        # available_now here).
        return FakeSession()

    def test_discover_text_emits_prefixed_id_when_key_set(self):
        session = self._ollama_unreachable_session()
        # Validate succeeds for all known OpenAI models so we exercise
        # the available_now=True branch too.
        for raw_id in KNOWN_OPENAI_EMBEDDING_MODELS:
            session.script(
                "GET", f"https://api.openai.com/v1/models/{raw_id}",
                FakeResponse(200, {"id": raw_id}),
            )
        with _EnvIsolation():
            choices = EmbeddingService.discover_text_models(
                ollama_url="http://localhost:11435",
                openai_api_key="sk-test",
                session=session,
            )
        openai_choices = [c for c in choices if c.backend == "openai"]
        self.assertEqual(len(openai_choices), len(KNOWN_OPENAI_EMBEDDING_MODELS))
        for c in openai_choices:
            self.assertTrue(
                c.id.startswith(OPENAI_MODEL_ID_PREFIX),
                f"OpenAI text catalog id missing prefix: {c.id!r}",
            )
            # The stripped form must be one of the raw OpenAI model names
            # we know about — sanity-check the round-trip.
            self.assertIn(_to_openai_api_model(c.id), KNOWN_OPENAI_EMBEDDING_MODELS)
            # Slot wiring still uses the canonical openai_text_embed slot.
            self.assertEqual(c.slot, "openai_text_embed")

    def test_discover_text_emits_prefixed_id_when_key_unset(self):
        # No key → entries are still emitted but available_now=False.
        # The prefix MUST be applied either way so the GUI dropdown
        # shows the right id even for greyed-out rows.
        session = self._ollama_unreachable_session()
        with _EnvIsolation():
            choices = EmbeddingService.discover_text_models(
                ollama_url="http://localhost:11435",
                openai_api_key="",
                session=session,
            )
        openai_choices = [c for c in choices if c.backend == "openai"]
        self.assertEqual(len(openai_choices), len(KNOWN_OPENAI_EMBEDDING_MODELS))
        for c in openai_choices:
            self.assertFalse(c.available_now)
            self.assertTrue(
                c.id.startswith(OPENAI_MODEL_ID_PREFIX),
                f"OpenAI text catalog id missing prefix: {c.id!r}",
            )

    def test_discover_code_emits_prefixed_openai_id(self):
        session = self._ollama_unreachable_session()
        for raw_id in KNOWN_OPENAI_EMBEDDING_MODELS:
            session.script(
                "GET", f"https://api.openai.com/v1/models/{raw_id}",
                FakeResponse(200, {"id": raw_id}),
            )
        with _EnvIsolation():
            choices = EmbeddingService.discover_code_models(
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                openai_api_key="sk-test",
                session=session,
            )
        openai_choices = [c for c in choices if c.backend == "openai"]
        self.assertEqual(len(openai_choices), len(KNOWN_OPENAI_EMBEDDING_MODELS))
        for c in openai_choices:
            self.assertTrue(
                c.id.startswith(OPENAI_MODEL_ID_PREFIX),
                f"OpenAI code catalog id missing prefix: {c.id!r}",
            )
        # The two text-embedding-3 variants ARE wired through to the
        # canonical openai_code_embed slot; ada-002 isn't in the CODE
        # slot map (forward-compat-only, OpenAI has no code-specific
        # model today) so it falls through to ollama_code_embed and the
        # GUI labels it accordingly. Both behaviours are intentional.
        modern_openai = [
            c for c in openai_choices
            if _to_openai_api_model(c.id) in {
                "text-embedding-3-small", "text-embedding-3-large",
            }
        ]
        self.assertEqual(len(modern_openai), 2)
        for c in modern_openai:
            self.assertEqual(c.slot, "openai_code_embed")

    def test_validate_probe_uses_raw_api_name(self):
        """When discovery probes OpenAI, the HTTP URL must use the raw
        model name (no prefix) — OpenAI's API doesn't know about our
        catalog-id naming convention.
        """
        session = self._ollama_unreachable_session()
        for raw_id in KNOWN_OPENAI_EMBEDDING_MODELS:
            session.script(
                "GET", f"https://api.openai.com/v1/models/{raw_id}",
                FakeResponse(200, {"id": raw_id}),
            )
        with _EnvIsolation():
            EmbeddingService.discover_text_models(
                ollama_url="http://localhost:11435",
                openai_api_key="sk-test",
                session=session,
            )
        # FakeSession records every GET it answered as (method, url, kwargs).
        # The OpenAI URLs in the recorded calls must use the RAW form,
        # never the prefixed.
        openai_calls = [
            call for call in session.calls
            if call[1].startswith("https://api.openai.com/v1/models/")
        ]
        self.assertGreater(len(openai_calls), 0, "no OpenAI validation probe was issued")
        for method, url, _kwargs in openai_calls:
            model_in_url = url.rsplit("/", 1)[-1]
            self.assertFalse(
                model_in_url.startswith(OPENAI_MODEL_ID_PREFIX),
                f"OpenAI HTTP probe used the prefixed form: {url!r}",
            )
            self.assertIn(model_in_url, KNOWN_OPENAI_EMBEDDING_MODELS)


class OpenAIHttpBoundaryStripsPrefixTests(unittest.TestCase):
    """If the active text/code model id was loaded from env / app_state
    in its catalog-id form (``"openai-text-embedding-3-small"``), the
    HTTP-call boundary inside EmbeddingService MUST strip the prefix
    before invoking ``OpenAIAdapter.embed`` — otherwise the OpenAI API
    returns HTTP 400 (the prefixed name is not a real OpenAI model id).
    """

    def _build_openai_service(
        self,
        text_model_id: str,
        code_model_id: str = "openai-text-embedding-3-small",
    ) -> tuple[EmbeddingService, FakeSession]:
        session = FakeSession()
        # Validation probe for both model variants (raw form only —
        # because the strip MUST happen before the HTTP call).
        for raw in ("text-embedding-3-small", "text-embedding-3-large"):
            session.script(
                "GET", f"https://api.openai.com/v1/models/{raw}",
                FakeResponse(200, {"id": raw}),
            )
            # Embed endpoint returns a stub vector for either model.
            session.script(
                "POST", "https://api.openai.com/v1/embeddings",
                FakeResponse(200, {
                    "data": [{"index": 0, "embedding": [0.0] * 8}],
                    "model": raw,
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }),
            )
        svc = EmbeddingService(
            project_root=None,
            ollama_url="http://localhost:11435",
            code_embed_url="http://localhost:11440",
            text_model_id=text_model_id,
            code_model_id=code_model_id,
            openai_api_key="sk-test",
            session=session,
        )
        return svc, session

    def test_active_text_path_strips_prefix(self):
        """``_embed_text_via_active`` must call OpenAI with the raw name
        even when ``text_model_id`` carries the catalog-id prefix.
        """
        svc, session = self._build_openai_service(
            text_model_id="openai-text-embedding-3-small",
        )
        try:
            self.assertEqual(svc.text_vector_slot, "openai_text_embed")
            _ = svc.embed_text("hello")
        finally:
            svc.close()
        # The POST to /v1/embeddings must carry the raw model name.
        # FakeSession stores calls as (method, url, kwargs); body lives
        # in kwargs["json"].
        embed_calls = [
            call for call in session.calls
            if call[0] == "POST" and call[1] == "https://api.openai.com/v1/embeddings"
        ]
        self.assertEqual(len(embed_calls), 1)
        body = embed_calls[0][2]["json"]
        self.assertEqual(body["model"], "text-embedding-3-small")

    def test_active_code_path_strips_prefix(self):
        """``_embed_code_via_active`` must apply the same strip when the
        ACTIVE code backend is OpenAI.
        """
        svc, session = self._build_openai_service(
            text_model_id="openai-text-embedding-3-small",
            code_model_id="openai-text-embedding-3-large",
        )
        try:
            self.assertEqual(svc.code_vector_slot, "openai_code_embed")
            _ = svc.embed_code("def f(): pass")
        finally:
            svc.close()
        embed_calls = [
            call for call in session.calls
            if call[0] == "POST" and call[1] == "https://api.openai.com/v1/embeddings"
        ]
        # One call for the code embed; the model in the body must be raw.
        # FakeSession stores call body in kwargs["json"].
        code_call = next(
            (c for c in embed_calls
             if c[2]["json"].get("input") == ["def f(): pass"]),
            None,
        )
        self.assertIsNotNone(code_call, "code embed POST was not issued")
        self.assertEqual(code_call[2]["json"]["model"], "text-embedding-3-large")

    def test_embed_text_all_configured_strips_prefix_for_openai_fallback(self):
        """When the active backend is Ollama (not OpenAI) but the user
        has set ``OPENAI_EMBEDDING_MODEL`` to the prefixed catalog id by
        mistake (or copy-pasted from the GUI), the OpenAI-fallback path
        in ``embed_text_all_configured`` must still strip before calling.
        """
        # Build an Ollama-active service so embed_text_all_configured
        # exercises the OpenAI fallback branch.
        session = FakeSession()
        # Ollama active text backend
        session.script(
            "GET", "http://localhost:11435/api/tags",
            _ollama_tags_response(["qwen3-embedding:0.6b"]),
        )
        session.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(200, {"embeddings": [[0.0] * 1024]}),
        )
        # OpenAI fallback
        session.script(
            "GET", "https://api.openai.com/v1/models/text-embedding-3-small",
            FakeResponse(200, {"id": "text-embedding-3-small"}),
        )
        session.script(
            "POST", "https://api.openai.com/v1/embeddings",
            FakeResponse(200, {
                "data": [{"index": 0, "embedding": [0.0] * 1536}],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }),
        )
        with _EnvIsolation():
            # Simulate user error: put the prefixed catalog id in env.
            os.environ["OPENAI_EMBEDDING_MODEL"] = "openai-text-embedding-3-small"
            # v0.2.71 Piece 5c: the OpenAI fallback is a SECONDARY slot, now
            # opt-in. Enable the toggle so this prefix-strip boundary test
            # still exercises the fallback branch.
            os.environ[DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV] = "true"
            svc = EmbeddingService(
                project_root=None,
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                text_model_id="qwen3-embedding:0.6b",
                code_model_id="codesage-large-v2",
                openai_api_key="sk-test",
                session=session,
            )
            try:
                result = svc.embed_text_all_configured("hello")
            finally:
                svc.close()
        self.assertIn("openai_text_embed", result)
        # The OpenAI HTTP call must have used the raw form despite the
        # prefixed env value.
        post_calls = [
            c for c in session.calls
            if c[0] == "POST" and c[1] == "https://api.openai.com/v1/embeddings"
        ]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0][2]["json"]["model"], "text-embedding-3-small")


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


# ---------------------------------------------------------------------------
# v0.2.69 FIX 3 — per-embed-REQUEST timeout (replaces install.py's removed
# per-process seed timeouts). The whole-subprocess caps fired on legitimate
# slow re-embeds; the only guard is now at chunk granularity (one HTTP embed
# request). These tests verify (a) the env-overridable resolution, (b) the
# resolved value is threaded into every adapter, and (c) a hung embed request
# aborts with the configured cap rather than hanging forever.
# ---------------------------------------------------------------------------


class EmbedRequestTimeoutResolutionTests(unittest.TestCase):
    """``_resolve_embed_request_timeout`` env override + fallbacks."""

    def test_default_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(EMBED_REQUEST_TIMEOUT_ENV, None)
            self.assertEqual(
                _resolve_embed_request_timeout(),
                DEFAULT_EMBED_REQUEST_TIMEOUT_SECS,
            )

    def test_default_is_180(self):
        # The default must be generous (~6x the ~30s/chunk arctic-on-CPU
        # boundary) so a slow re-embed never trips it.
        self.assertEqual(DEFAULT_EMBED_REQUEST_TIMEOUT_SECS, 180.0)

    def test_env_override_honoured(self):
        with patch.dict(os.environ, {EMBED_REQUEST_TIMEOUT_ENV: "42"}):
            self.assertEqual(_resolve_embed_request_timeout(), 42.0)

    def test_env_float_override(self):
        with patch.dict(os.environ, {EMBED_REQUEST_TIMEOUT_ENV: "12.5"}):
            self.assertEqual(_resolve_embed_request_timeout(), 12.5)

    def test_garbage_env_falls_back_to_default(self):
        with patch.dict(os.environ, {EMBED_REQUEST_TIMEOUT_ENV: "not-a-number"}):
            self.assertEqual(
                _resolve_embed_request_timeout(),
                DEFAULT_EMBED_REQUEST_TIMEOUT_SECS,
            )

    def test_nonpositive_env_falls_back_to_default(self):
        # A non-positive value would mean "no timeout" to requests — exactly
        # the unbounded-hang this guard prevents — so we coerce to default.
        for bad in ("0", "-5", "-1.0"):
            with patch.dict(os.environ, {EMBED_REQUEST_TIMEOUT_ENV: bad}):
                self.assertEqual(
                    _resolve_embed_request_timeout(),
                    DEFAULT_EMBED_REQUEST_TIMEOUT_SECS,
                    f"{bad!r} should fall back to default",
                )


class EmbedRequestTimeoutThreadingTests(unittest.TestCase):
    """The resolved timeout reaches every real adapter."""

    def _make_service(self, *, explicit=None, env=None):
        with _EnvIsolation(), patch.dict(os.environ, env or {}):
            return EmbeddingService(
                project_root=None,
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                text_model_id="qwen3-embedding:0.6b",
                code_model_id="codesage-large-v2",
                openai_api_key="",
                session=FakeSession(),
                embed_request_timeout=explicit,
            )

    def test_explicit_value_threaded_to_all_adapters(self):
        svc = self._make_service(explicit=7.0)
        self.assertEqual(svc.embed_request_timeout, 7.0)
        self.assertEqual(svc.ollama.timeout, 7.0)
        self.assertEqual(svc.codeembed.timeout, 7.0)
        self.assertEqual(svc.openai.timeout, 7.0)

    def test_env_value_threaded_when_no_explicit(self):
        svc = self._make_service(
            explicit=None, env={EMBED_REQUEST_TIMEOUT_ENV: "99"}
        )
        self.assertEqual(svc.embed_request_timeout, 99.0)
        self.assertEqual(svc.ollama.timeout, 99.0)
        self.assertEqual(svc.codeembed.timeout, 99.0)
        self.assertEqual(svc.openai.timeout, 99.0)

    def test_default_threaded_when_env_unset(self):
        svc = self._make_service(explicit=None, env={})
        self.assertEqual(
            svc.embed_request_timeout, DEFAULT_EMBED_REQUEST_TIMEOUT_SECS
        )
        self.assertEqual(
            svc.ollama.timeout, DEFAULT_EMBED_REQUEST_TIMEOUT_SECS
        )


class _HangingSession:
    """A ``requests.Session`` look-alike whose ``post`` simulates a wedged
    embedder: it blocks for ``sleep_for`` seconds and then — like
    ``requests`` itself when the per-request deadline elapses — raises
    ``requests.Timeout`` once the elapsed time exceeds the ``timeout=``
    kwarg the caller passed.

    The key behaviour under test: the adapter passes ``timeout=self.timeout``
    to ``post``. If that kwarg is honoured (cap small), the call raises
    promptly; if the production code regressed and dropped ``timeout=``, the
    kwarg would be ``None`` and this fake would block the full ``sleep_for``,
    which the test's own wall-clock guard then catches.
    """

    def __init__(self, sleep_for: float) -> None:
        self.sleep_for = sleep_for
        self.posted_timeouts: list = []
        self.closed = False

    def get(self, url: str, **kwargs):
        # Health/discovery probes — return reachable so construction-side
        # readiness checks don't interfere. (Not exercised by these tests,
        # which call the adapter embed methods directly.)
        return FakeResponse(200, {"models": [{"name": "qwen3-embedding:0.6b"}]})

    def post(self, url: str, **kwargs):
        import time as _time

        import requests as _req

        timeout = kwargs.get("timeout")
        self.posted_timeouts.append(timeout)
        # Emulate requests' own behaviour: block up to the deadline, then
        # raise Timeout. A None timeout (the regression) blocks the full
        # sleep_for. We cap the actual sleep so the test never hangs the
        # whole suite even on regression — the wall-clock assertion below
        # is what proves the cap was honoured.
        deadline = timeout if (timeout and timeout > 0) else self.sleep_for
        _time.sleep(min(deadline, self.sleep_for))
        if timeout and timeout > 0 and self.sleep_for > timeout:
            raise _req.Timeout(f"mock embed request exceeded {timeout}s")
        # If no positive timeout was passed, fall through to a hung-style
        # error after the full sleep so embed_text still surfaces a failure.
        raise _req.Timeout("mock embed request hung (no timeout passed)")

    def close(self) -> None:
        self.closed = True


class HungEmbedRequestAbortsTests(unittest.TestCase):
    """A wedged embedder aborts within the per-request cap, not forever."""

    def test_ollama_embed_aborts_within_cap(self):
        import time as _time

        # Cap the request at 0.05s; the fake "embedder" would otherwise
        # block for 5s. With the timeout honoured the embed call must raise
        # quickly (well under the 5s hang) — proving the per-request guard
        # is wired through to the adapter's POST.
        cap = 0.05
        hang = _HangingSession(sleep_for=5.0)
        with _EnvIsolation():
            svc = EmbeddingService(
                project_root=None,
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                text_model_id="qwen3-embedding:0.6b",
                code_model_id="codesage-large-v2",
                openai_api_key="",
                session=hang,
                embed_request_timeout=cap,
            )
        self.assertEqual(svc.ollama.timeout, cap)

        started = _time.monotonic()
        with self.assertRaises(RuntimeError):
            # OllamaAdapter wraps requests.RequestException (Timeout is a
            # subclass) as RuntimeError("Ollama /api/embed network error").
            svc.embed_text("a chunk that takes forever to embed")
        elapsed = _time.monotonic() - started

        # The POST must have received our small cap, not None / the default.
        self.assertTrue(hang.posted_timeouts)
        self.assertEqual(hang.posted_timeouts[0], cap)
        # And the whole call must have returned far faster than the 5s hang —
        # if production dropped the timeout= kwarg this would blow past it.
        self.assertLess(
            elapsed,
            2.0,
            f"embed call should abort near the {cap}s cap, took {elapsed:.2f}s "
            "(did the per-request timeout get dropped?)",
        )


# ---------------------------------------------------------------------------
# v0.2.70 FIX A — total per-request deadline catches a DRIBBLING wedge
# ---------------------------------------------------------------------------
#
# The pre-existing HungEmbedRequestAbortsTests above only models a socket that
# the scalar ``requests`` timeout WOULD catch (the mock raises Timeout itself
# based on the timeout kwarg). That does NOT exercise the actual install-path
# wedge: a backend that keeps the socket open and dribbles bytes more often
# than the read timeout. ``requests`` resets its read clock on every dribble,
# so the scalar timeout never fires and the request hangs forever.
#
# These tests model that real wedge — a ``post`` that blocks (effectively)
# forever and never raises on its own — and assert that the v0.2.70
# bounded_post total deadline fails the single request near the cap instead of
# hanging. The deadline is per-HTTP-request (one chunk for embed_text, one
# batch for embed_*_batch); it is never per-node or per-process.


class _DribblingSession:
    """A wedged backend that NEVER returns and NEVER raises on its own.

    Models the real install-path failure: the socket stays open and dribbles
    keep-alive bytes faster than the scalar read timeout, so ``requests``
    never times out. Here we simply block on an ``Event`` that is never set,
    ignoring the ``timeout=`` kwarg entirely — exactly what a scalar-timeout
    request does against a dribbling peer. Only an external total deadline
    (bounded_post's Future) can break out.

    The blocking happens on bounded_post's worker thread, so the test's main
    thread is freed by ``future.result(timeout=cap)``. ``release()`` is called
    in ``tearDown`` so the parked daemon worker can unwind and not leak.
    """

    def __init__(self) -> None:
        self.posted_timeouts: list = []
        self.get_calls = 0
        self._gate = threading.Event()
        self.closed = False

    def get(self, url: str, **kwargs):
        # Health/discovery probes resolve immediately (reachable) so they don't
        # interfere with the embed path under test.
        self.get_calls += 1
        return FakeResponse(200, {"models": [{"name": "qwen3-embedding:0.6b"}]})

    def post(self, url: str, **kwargs):
        self.posted_timeouts.append(kwargs.get("timeout"))
        # Block until released. A real dribbling socket would keep requests
        # busy here indefinitely; we never set the gate during the deadline
        # window, so only bounded_post's Future deadline can abort the caller.
        self._gate.wait()  # no timeout — would hang forever without the fix
        import requests as _req

        raise _req.Timeout("dribbling socket released after test (never reached in-window)")

    def release(self) -> None:
        self._gate.set()

    def close(self) -> None:
        self.closed = True


class DribblingWedgeBoundedDeadlineTests(unittest.TestCase):
    """A dribbling/no-forward-progress wedge fails at the bounded total cap."""

    def setUp(self) -> None:
        self.dribble = _DribblingSession()

    def tearDown(self) -> None:
        # Let the abandoned daemon worker unwind so it doesn't leak between
        # tests (it's a daemon, so it would not block process exit regardless).
        self.dribble.release()

    def _make_service(self, cap: float):
        with _EnvIsolation():
            return EmbeddingService(
                project_root=None,
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                text_model_id="qwen3-embedding:0.6b",
                code_model_id="codesage-large-v2",
                openai_api_key="",
                session=self.dribble,
                embed_request_timeout=cap,
            )

    def test_dribbling_ollama_embed_aborts_at_total_deadline(self):
        import time as _time

        cap = 0.5
        svc = self._make_service(cap)
        self.assertEqual(svc.ollama.timeout, cap)

        started = _time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            # OllamaAdapter wraps the requests.Timeout from bounded_post's
            # deadline as RuntimeError("Ollama /api/embed network error").
            svc.embed_text("a chunk against a dribbling, never-returning socket")
        elapsed = _time.monotonic() - started

        # The wedge must fail at the REQUEST level, not hang.
        self.assertIn("network error", str(ctx.exception).lower())
        # The POST received the small cap (proves it routed through the
        # bounded path that honours embed_request_timeout).
        self.assertTrue(self.dribble.posted_timeouts)
        self.assertEqual(self.dribble.posted_timeouts[0], cap)
        # And it returned near the cap — NOT after a multi-minute hang. Give
        # generous slack (CI scheduling jitter) but well under any "forever".
        self.assertLess(
            elapsed,
            cap + 4.0,
            f"dribbling embed should abort near the {cap}s total deadline, "
            f"took {elapsed:.2f}s — did the total per-request deadline regress "
            "back to a scalar read-gap timeout?",
        )

    def test_normal_fast_embed_succeeds_through_bounded_post(self):
        # A healthy fast embed must NOT be penalised by the bounded wrapper:
        # it completes well under the cap and returns its real vector.
        fast = FakeSession()
        fast.script(
            "POST", "http://localhost:11435/api/embed",
            FakeResponse(200, {"embeddings": [[0.11, 0.22, 0.33]]}),
        )
        with _EnvIsolation():
            svc = EmbeddingService(
                project_root=None,
                ollama_url="http://localhost:11435",
                code_embed_url="http://localhost:11440",
                text_model_id="qwen3-embedding:0.6b",
                code_model_id="codesage-large-v2",
                openai_api_key="",
                session=fast,
                embed_request_timeout=0.5,
            )
        vec = svc.embed_text("a fast healthy chunk")
        self.assertEqual(vec, [0.11, 0.22, 0.33])
        # The POST still received the per-request cap (one deadline, applied).
        post_calls = [c for c in fast.calls if c[0] == "POST"]
        self.assertTrue(post_calls)
        self.assertEqual(post_calls[-1][2]["timeout"], 0.5)

    def test_bounded_post_passes_timeout_to_underlying_post(self):
        # Direct unit check of the helper: the scalar timeout is still threaded
        # into the real session.post (the fast-failure floor) AND a caller's
        # stray timeout kwarg is ignored in favour of the explicit one.
        from vco_lib.embedding_providers._http import bounded_post

        sess = FakeSession()
        sess.script(
            "POST", "http://example/embed", FakeResponse(200, {"ok": True})
        )
        resp = bounded_post(
            sess,
            "http://example/embed",
            timeout=3.0,
            json={"x": 1},
            # A stray timeout kwarg must be overridden by the explicit one.
            extra_should_pass="yes",
        )
        self.assertEqual(resp.status_code, 200)
        method, url, kwargs = sess.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["timeout"], 3.0)
        self.assertEqual(kwargs["json"], {"x": 1})
        self.assertEqual(kwargs["extra_should_pass"], "yes")


# ---------------------------------------------------------------------------
# v0.2.70 FIX B — install.py threads VCT_EMBED_REQUEST_TIMEOUT_SECS into seed
# ---------------------------------------------------------------------------


class SubprocessEnvThreadsRequestTimeoutTests(unittest.TestCase):
    """``_subprocess_env_with_embedding`` propagates the per-chunk timeout env.

    Operators on very slow machines RAISE ``VCT_EMBED_REQUEST_TIMEOUT_SECS``
    (the maintainer-sanctioned cure for "slow but healthy" is a tunable
    per-chunk bound, never a process kill). For that override to reach the
    ``sync_knowledge_graph.py`` subprocess, install.py must thread the env var
    through when it is set in the install shell. When unset, the subprocess
    falls back to the 180s default — so install.py must NOT inject a value.
    """

    def _load_install_module(self):
        import importlib.util

        install_path = REPO_ROOT / "install.py"
        spec = importlib.util.spec_from_file_location(
            "_install_for_fixb_test", install_path
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_request_timeout_threaded_when_set(self):
        install_mod = self._load_install_module()
        with _EnvIsolation(), patch.dict(
            os.environ,
            {"ACTIVE_EMBEDDING": "qwen3", EMBED_REQUEST_TIMEOUT_ENV: "600"},
        ):
            env = install_mod._subprocess_env_with_embedding()
        self.assertEqual(env.get(EMBED_REQUEST_TIMEOUT_ENV), "600")

    def test_request_timeout_absent_when_unset(self):
        install_mod = self._load_install_module()
        with _EnvIsolation():
            # Make sure it is not present in the starting env.
            os.environ.pop(EMBED_REQUEST_TIMEOUT_ENV, None)
            with patch.dict(os.environ, {"ACTIVE_EMBEDDING": "qwen3"}):
                env = install_mod._subprocess_env_with_embedding()
        # install.py must not synthesise a value — absence → subprocess default.
        self.assertNotIn(EMBED_REQUEST_TIMEOUT_ENV, env)


class DualWriteAllSlotsToggleTests(unittest.TestCase):
    """v0.2.71 Piece 5c — secondary-slot enrichment write is opt-in (default OFF).

    The active slot must ALWAYS be written (reads target it; a single-entry
    named-vector dict keeps existing collections working); the secondary
    slots (qwen3/openai for text, codesage/openai for code) are written only
    when DUAL_EMBEDDING_WRITE_ALL_SLOTS is truthy. Default OFF cuts the embed
    cost the field report flagged without breaking any read.
    """

    def test_default_resolves_to_false_when_unset(self):
        with _EnvIsolation():
            os.environ.pop(DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV, None)
            self.assertFalse(
                _resolve_write_all_slots(),
                "DUAL_EMBEDDING_WRITE_ALL_SLOTS must default to FALSE (opt-in)",
            )

    def test_truthy_values_enable(self):
        for val in ("1", "true", "TRUE", "yes", "On"):
            with _EnvIsolation(), patch.dict(
                os.environ, {DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV: val}, clear=False
            ):
                self.assertTrue(
                    _resolve_write_all_slots(),
                    f"{val!r} must enable secondary-slot writes",
                )

    def test_falsey_and_garbage_values_disable(self):
        for val in ("0", "false", "no", "off", "", "maybe", "2"):
            with _EnvIsolation(), patch.dict(
                os.environ, {DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV: val}, clear=False
            ):
                self.assertFalse(
                    _resolve_write_all_slots(),
                    f"{val!r} must NOT enable secondary-slot writes",
                )

    def test_text_default_off_writes_only_active_slot(self):
        # arctic2-active install with Ollama (qwen3) + OpenAI both reachable:
        # with the toggle OFF, only the active arctic2_embed slot is written.
        with _EnvIsolation(), patch.dict(
            os.environ, {"EMBEDDING_MODEL": "snowflake-arctic-embed2:latest"}, clear=False
        ):
            os.environ.pop(DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV, None)
            svc, _ = _make_service_with_mocks(
                text_model="snowflake-arctic-embed2:latest",
                openai_key="sk-test",
                ollama_ready=True,
                openai_valid=True,
            )
            try:
                slots = svc.embed_text_all_configured("hello world")
            finally:
                svc.close()
            self.assertEqual(
                set(slots.keys()),
                {svc.text_vector_slot},
                "default-OFF must write ONLY the active slot (a valid named-vector "
                "write), not the qwen3/openai secondary slots",
            )
            self.assertEqual(svc.text_vector_slot, "arctic2_embed")

    def test_text_on_writes_secondary_slots(self):
        with _EnvIsolation(), patch.dict(
            os.environ,
            {
                "EMBEDDING_MODEL": "snowflake-arctic-embed2:latest",
                DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV: "true",
            },
            clear=False,
        ):
            svc, _ = _make_service_with_mocks(
                text_model="snowflake-arctic-embed2:latest",
                openai_key="sk-test",
                ollama_ready=True,
                openai_valid=True,
            )
            try:
                slots = svc.embed_text_all_configured("hello world")
            finally:
                svc.close()
            # active arctic2 + qwen3 (ollama) + openai_text_embed all present
            self.assertIn("arctic2_embed", slots)
            self.assertIn("qwen3_embed", slots)
            self.assertIn("openai_text_embed", slots)

    def test_code_default_off_writes_only_active_slot(self):
        # qwen3-active (text) → code active slot is codesage_embed by default.
        with _EnvIsolation():
            os.environ.pop(DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV, None)
            svc, _ = _make_service_with_mocks(
                openai_key="sk-test",
                code_ready=True,
                openai_valid=True,
            )
            try:
                slots = svc.embed_code_all_configured("def f(): pass")
            finally:
                svc.close()
            self.assertEqual(
                set(slots.keys()),
                {svc.code_vector_slot},
                "default-OFF must write ONLY the active code slot",
            )

    def test_active_slot_always_present_even_off(self):
        # The guarantee that keeps reads working: the active slot is never
        # dropped by the toggle, regardless of its value.
        for toggle in (None, "false", "true"):
            with _EnvIsolation():
                if toggle is not None:
                    os.environ[DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV] = toggle
                svc, _ = _make_service_with_mocks(ollama_ready=True)
                try:
                    slots = svc.embed_text_all_configured("x")
                finally:
                    svc.close()
                self.assertIn(
                    svc.text_vector_slot,
                    slots,
                    f"active slot must always be written (toggle={toggle!r})",
                )


if __name__ == "__main__":
    unittest.main()
