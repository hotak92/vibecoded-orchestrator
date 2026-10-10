# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-E — ONE shared Ollama model copy across projects.

Owner ruling (2026-10-08, after the live measurement: one KG search pinned
qwen3-embedding 2.4 GB + arctic 1.15 GB in VRAM for 24 h each):

* the default ``keep_alive`` VCO sends on every Ollama request drops from
  ``24h`` to ``10m`` (``VCO_OLLAMA_KEEP_ALIVE`` keeps its override semantics);
* every VCO caller must request a model with IDENTICAL load options
  (``num_ctx`` from the ONE table ``chunking.MODEL_TOKEN_LIMITS``) so Ollama
  never loads a second runner of the same model;
* the shipped compose must raise ``OLLAMA_MAX_QUEUE`` to 1000 (never lower)
  so queued cross-project embeds are not 503-dropped;
* a 503 from Ollama (queue over the limit) and a connection-refused from the
  code-embed service (restarting mid model-load) are RETRYABLE, never a drop;
* the weaviate-client PyPI version lookup on connect is disabled (one network
  call per MCP start, to pypi.org, that VCO does not need).

RED-PROOF: the keep_alive default, the ``embed_request_body`` builder, the
connection-error retry, the compose knobs, the PyPI-check disable and the
resync/vector-copy routing all FAIL on the pre-W4-E tree; the Ollama-503
retry pin is deliberately GREEN before and after (verification, not a
change — ``_retry_once_on_503`` already matched the "503" substring).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.embedding_providers import ollama as ollama_mod  # noqa: E402

#: The embedding models every caller must agree on (one row per shipped
#: Ollama embedding tier: KG active, KG legacy/arctic, code jina fallback).
_MODELS = (
    "qwen3-embedding:0.6b",
    "snowflake-arctic-embed2:latest",
    "unclemusclez/jina-embeddings-v2-base-code:latest",
)


@pytest.fixture(autouse=True)
def _clean_keep_alive_env():
    saved = os.environ.get("VCO_OLLAMA_KEEP_ALIVE")
    os.environ.pop("VCO_OLLAMA_KEEP_ALIVE", None)
    saved503 = os.environ.get("VCO_EMBED_503_RETRY_DELAY")
    os.environ["VCO_EMBED_503_RETRY_DELAY"] = "0"  # zero-delay: fast, finite
    yield
    if saved is None:
        os.environ.pop("VCO_OLLAMA_KEEP_ALIVE", None)
    else:
        os.environ["VCO_OLLAMA_KEEP_ALIVE"] = saved
    if saved503 is None:
        os.environ.pop("VCO_EMBED_503_RETRY_DELAY", None)
    else:
        os.environ["VCO_EMBED_503_RETRY_DELAY"] = saved503


# ── Item 1: keep_alive default 10m ───────────────────────────────────────


def test_keep_alive_default_is_10m():
    assert ollama_mod._keep_alive() == "10m"


def test_compose_ollama_keep_alive_and_queue():
    infra = (
        _REPO_ROOT / "infrastructure" / "docker-compose.yml"
    ).read_text("utf-8")
    mcp_compose = (
        _REPO_ROOT / "claude_mcp_servers" / "compose.yaml"
    ).read_text("utf-8")
    for name, text in (("infrastructure", infra), ("compose.yaml", mcp_compose)):
        assert 'OLLAMA_KEEP_ALIVE: "24h"' not in text, (
            f"{name}: a stale 24h keep_alive default survived"
        )
        assert 'OLLAMA_KEEP_ALIVE: "10m"' in text, (
            f"{name}: OLLAMA_KEEP_ALIVE must be the 10m default"
        )
        assert 'OLLAMA_MAX_QUEUE: "1000"' in text, (
            f"{name}: OLLAMA_MAX_QUEUE must be >=1000 (owner: never lower)"
        )


def test_canonical_env_targets_not_24h():
    from vco_lib import service_adoption

    assert (
        service_adoption.CANONICAL_ENV_TARGETS.get("ollama", {}).get(
            "OLLAMA_KEEP_ALIVE"
        )
        == "10m"
    )


# ── Item 2: ONE options builder, identical per model across callers ──────


class _FakeResp:
    status_code = 200

    def json(self):
        return {"embeddings": [[0.1, 0.2]]}

    @property
    def text(self):
        return ""


def _capture_adapter_body(model: str, method: str) -> dict:
    captured: dict = {}

    def _fake_bounded_post(session, url, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        return _FakeResp()

    with mock.patch.object(ollama_mod, "bounded_post", _fake_bounded_post):
        adapter = ollama_mod.OllamaAdapter("http://x:11435", session=mock.MagicMock())
        if method == "embed":
            adapter.embed(model, "hello")
        else:
            adapter.embed_batch(model, ["hello"])
    return captured["json"]


def test_embed_request_builder_canonical_shape():
    builder = getattr(ollama_mod, "embed_request_body", None)
    assert builder is not None, "ONE shared embed body builder must exist"
    body = builder("qwen3-embedding:0.6b", prompt="x")
    assert body == {
        "model": "qwen3-embedding:0.6b",
        "prompt": "x",
        "options": {"num_ctx": 10_240},
        "truncate": False,
        "keep_alive": "10m",
    }
    batch = builder("qwen3-embedding:0.6b", input=["x", "y"])
    assert batch["input"] == ["x", "y"] and "prompt" not in batch
    assert batch["options"] == {"num_ctx": 10_240}


def test_options_identity_adapter_matches_builder():
    """For each embedding model, the canonical adapter's outgoing options
    dict is EXACTLY the shared builder's (one runner, one load shape)."""
    builder = ollama_mod.embed_request_body
    from claude_mcp_servers.weaviate_mcp.chunking import MODEL_TOKEN_LIMITS

    for model in _MODELS:
        expected = builder(model, prompt="x")
        for method in ("embed", "embed_batch"):
            got = _capture_adapter_body(model, method)
            assert got["options"] == expected["options"] == {
                "num_ctx": MODEL_TOKEN_LIMITS[model]
            }, (model, method, got)
            assert got["keep_alive"] == expected["keep_alive"] == "10m"
            assert got["truncate"] is False


# ── aiohttp fakes for the weaviate-MCP inline Ollama legs ────────────────


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeAioResp:
    status = 200

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return ""


class _CaptureAioSession:
    def __init__(self, capture, responder=None):
        self._capture = capture
        self._responder = responder

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self._capture.append({"url": url, "json": json})
        value = (
            _FakeAioResp({"embedding": [0.1, 0.2]})
            if self._responder is None
            else self._responder()
        )
        if isinstance(value, Exception):
            return _RaisingCtx(value)
        return _AsyncCtx(value)


class _RaisingCtx:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


def _fake_client_session(capture, responder=None):
    class _FakeClientSession:
        async def __aenter__(self):
            return _CaptureAioSession(capture, responder)

        async def __aexit__(self, *exc):
            return False

    return _FakeClientSession


def test_options_identity_weaviate_mcp_inline_legs(monkeypatch):
    """The three inline Ollama fallbacks in the MCP send the SAME options
    dict as the shared builder for their models."""
    import weaviate_mcp.embeddings as emb
    import weaviate_mcp.server as wserver

    monkeypatch.setattr(
        wserver, "_get_embedding_service", lambda: None, raising=False
    )
    captured: list = []
    monkeypatch.setattr(
        emb.aiohttp, "ClientSession", _fake_client_session(captured)
    )

    asyncio.run(emb.get_ollama_embedding("hello"))
    asyncio.run(emb.get_legacy_text_embedding("hello"))
    asyncio.run(emb.get_legacy_code_embedding("hello"))

    builder = ollama_mod.embed_request_body
    assert len(captured) == 3
    expected_models = (
        wserver.EMBEDDING_MODEL,
        wserver.LEGACY_TEXT_EMBEDDING_MODEL,
        "unclemusclez/jina-embeddings-v2-base-code:latest",
    )
    for call, model in zip(captured, expected_models):
        want = builder(model, prompt="hello")
        assert call["json"]["options"] == want["options"], (model, call)
        assert call["json"]["keep_alive"] == want["keep_alive"] == "10m", model
        assert call["json"]["truncate"] is False, model


def test_options_identity_code_embed_service_ollama_tier(monkeypatch):
    """The code-embed service's Ollama fallback tier sends the same options
    dict as the shared builder (and now carries keep_alive at all)."""
    ces = importlib.import_module(
        "claude_mcp_servers.code_embedding_service.server"
    )
    captured: list = []

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        captured.append({"url": url, "json": json})
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"embedding": [0.1, 0.2]}
        return resp

    # requests is imported function-local in _embed_ollama — patch the module.
    monkeypatch.setattr("requests.post", _fake_post)
    monkeypatch.setattr(ces, "MODEL_NAME", "unclemusclez/jina-embeddings-v2-base-code:latest")
    ces._embed_ollama(["hello"], is_query=False)

    builder = ollama_mod.embed_request_body
    want = builder("unclemusclez/jina-embeddings-v2-base-code:latest", prompt="hello")
    assert captured[0]["json"]["options"] == want["options"] == {"num_ctx": 2_048}
    assert captured[0]["json"]["keep_alive"] == "10m"


def test_options_identity_migrate_and_search_fallbacks(monkeypatch):
    """The migrate script's and the search CLI's legacy inline-Ollama legs
    route through the ONE builder (keep_alive present, num_ctx from the
    SSOT table)."""
    builder = ollama_mod.embed_request_body

    # migrate_to_new_embeddings.get_text_embedding fallback
    mig = importlib.import_module("claude_mcp_servers.scripts.migrate_to_new_embeddings")
    captured: list = []

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        captured.append({"url": url, "json": json})
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"embedding": [0.1]}
        return resp

    monkeypatch.setattr(mig, "HAS_EMBEDDING_SERVICE", False)
    monkeypatch.setattr(mig.requests, "post", _fake_post)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    mig.get_text_embedding("hello")
    want = builder("qwen3-embedding:0.6b", prompt="hello")
    assert captured[0]["json"]["options"] == want["options"], captured
    assert captured[0]["json"]["keep_alive"] == "10m", captured

    # (search_knowledge.get_embedding's direct-Ollama leg was removed in
    # v0.2.102 W6-B2 — owner ruling 2026-10-10, no other embedding model.)


def test_generate_calls_carry_keep_alive(monkeypatch):
    """Ollama GENERATE requests (KG summary, node formats) carry the same
    keep_alive policy — item 1 says every Ollama request VCO sends."""
    captured: list = []

    def _fake_post(url, json=None, timeout=None):  # noqa: A002
        captured.append({"url": url, "json": json})
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"response": "ok"}
        return resp

    gnf = importlib.import_module(
        "claude_mcp_servers.scripts.generate_node_formats"
    )
    monkeypatch.setattr(gnf.requests, "post", _fake_post)
    gnf.call_ollama("qwen3.5:9b", "prompt", 64)
    assert captured[0]["json"].get("keep_alive") == "10m", captured

    # summary_backends.call_ollama (stdlib urllib)
    sk_dir = str(_REPO_ROOT / "templates" / "scripts")
    if sk_dir not in sys.path:
        sys.path.insert(0, sk_dir)
    sb = importlib.import_module("summary_backends")
    urllib_capture: list = []

    class _FakeUrlopenResp:
        def read(self):
            return b'{"response": "ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        urllib_capture.append(json.loads(req.data.decode("utf-8")))
        return _FakeUrlopenResp()

    monkeypatch.setattr(sb.urllib.request, "urlopen", _fake_urlopen)
    sb.call_ollama("prompt", model="qwen3.5:9b")
    assert urllib_capture[0].get("keep_alive") == "10m", urllib_capture


# ── Item 3 / 6: 503 + connection-refused are retryable, never a drop ─────


def test_ollama_503_is_retryable():
    """Verification pin (green before AND after): an Ollama 'server busy'
    503 RuntimeError is retried by the shared retry loop, then succeeds."""
    from vco_lib.embedding_service import EmbeddingService

    calls: list = []

    def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(
                "Ollama /api/embed returned HTTP 503: server busy"
            )
        return [0.1, 0.2]

    out = EmbeddingService._retry_once_on_503(None, _flaky)
    assert out == [0.1, 0.2] and len(calls) == 3


def test_connection_refused_is_retryable():
    """W4-E item 6: connection-refused (code-embed restarting mid
    model-load) is transient-retryable at the raw-HTTP client layer —
    not an immediate drop. (The EmbeddingService layer deliberately keeps
    falling through to the inline HTTP client on a refusal instead —
    see tests/test_v02102_code_embed_client_retry.py's leave-alone pin.)"""
    from vco_lib.retry_after import retry_transient_embed

    calls: list = []

    def _flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError(
                "CodeEmbed /embed network error: HTTPConnectionPool("
                "host='localhost', port=11440): Max retries exceeded with "
                "url: /embed (Caused by NewConnectionError('<urllib3 "
                "connection failed>: [Errno 111] Connection refused'))"
            )
        return [0.3]

    out = retry_transient_embed(_flaky)
    assert out == [0.3] and len(calls) == 2


def test_connection_refused_gives_up_loudly():
    """A persistently refused connection re-raises after the bounded
    attempts — a loud failure, never a silent drop."""
    from vco_lib.retry_after import retry_transient_embed

    def _always_refused():
        raise RuntimeError("CodeEmbed /embed network error: [Errno 111] Connection refused")

    with pytest.raises(RuntimeError, match="Connection refused"):
        retry_transient_embed(_always_refused)


def test_retry_transient_embed_non_transient_immediate():
    """Back-compat shape: a non-503, non-connection error re-raises
    immediately (one call, no retry)."""
    from vco_lib.retry_after import retry_transient_embed

    calls: list = []

    def _bad_request():
        calls.append(1)
        raise RuntimeError("CodeEmbed /embed returned HTTP 400: too long")

    with pytest.raises(RuntimeError, match="400"):
        retry_transient_embed(_bad_request)
    assert len(calls) == 1


def test_inline_code_embed_http_retries_connection_error(monkeypatch):
    """The weaviate-MCP inline CodeEmbed HTTP fallback retries a connection
    error (service restarting during a model load) instead of returning
    None on the first shot."""
    import weaviate_mcp.embeddings as emb
    from vco_lib.retry_after import is_connection_error

    assert is_connection_error(RuntimeError("[Errno 111] Connection refused"))

    conn_exc = type("ClientConnectorError", (Exception,), {})
    calls: list = []

    def _responder():
        calls.append(1)
        if len(calls) < 2:
            return conn_exc("Connect call failed")
        return _FakeAioResp({"embedding": [0.5]})

    captured: list = []
    monkeypatch.setattr(
        emb.aiohttp, "ClientSession", _fake_client_session(captured, _responder)
    )
    out = asyncio.run(emb._inline_code_embed_http("hello"))
    assert out == [0.5], "connection refused must be retried, not dropped"
    assert len(calls) == 2


def test_inline_code_embed_http_non_retryable_still_fails_fast(monkeypatch):
    """A non-503 HTTP status (e.g. 400) still returns None immediately —
    the retry widening must not swallow genuine refusals."""
    import weaviate_mcp.embeddings as emb

    calls: list = []

    def _responder():
        calls.append(1)
        return _FakeAioResp400()

    captured: list = []
    monkeypatch.setattr(
        emb.aiohttp, "ClientSession", _fake_client_session(captured, _responder)
    )
    out = asyncio.run(emb._inline_code_embed_http("hello"))
    assert out is None and len(calls) == 1


class _FakeAioResp400(_FakeAioResp):
    status = 400


# ── Item 4: no PyPI lookup on connect ────────────────────────────────────


def test_connect_v4_disables_weaviate_client_pypi_check(monkeypatch):
    """connect_v4 no-ops weaviate-client's `_check_package_version` (the
    GET https://pypi.org/pypi/weaviate-client/json on every connect). The
    narrow patch keeps the server meta/health checks and the gRPC ping
    (skip_init_checks sites) intact.

    Order-independent: in a full-suite run an earlier test may already have
    applied the process-wide patch, so this asserts the STATE after a
    connect (patched flag set, method is a network-free no-op), not a
    before/after identity delta."""
    import weaviate
    import vco_lib.weaviate_helpers as wh
    from weaviate.connect import v4 as v4mod

    disable = getattr(wh, "disable_weaviate_client_pypi_check", None)
    assert disable is not None, "the shared PyPI-check disable must exist"
    monkeypatch.setattr(
        weaviate, "connect_to_custom", mock.MagicMock(return_value=object())
    )
    wh.connect_v4("http://localhost:8081")
    # The patch is in place and idempotent.
    assert wh.disable_weaviate_client_pypi_check() is True
    assert getattr(wh, "_PYPI_CHECK_PATCHED", False) is True
    # The installed method is a no-op: calling it touches no network —
    # the vendored original GETs pypi.org, which VCO forbids on connect.
    patched = v4mod._ConnectionBase._check_package_version

    class _FakeColour:
        pass

    assert patched(object(), _FakeColour()) is None


def test_resync_and_vector_copy_route_through_shared_connect(monkeypatch):
    """codegraph_resync / codegraph_vector_copy no longer hand-roll
    connect_to_custom (the bypass that still hit PyPI): the shared factory's
    answer is returned and the direct call is never made."""
    import vco_lib.weaviate_helpers as wh
    from vco_lib import codegraph_resync, codegraph_vector_copy

    sentinel = object()
    monkeypatch.setattr(wh, "connect_v4", mock.MagicMock(return_value=sentinel))
    # A direct connect_to_custom must never run: make it loud. Both
    # _build_client helpers swallow ordinary Exceptions into None, so the
    # assertion is on the RETURNED client: routed → sentinel; hand-rolled
    # (pre-fix) → the swallowed boom degrades to None.
    def _boom(*args, **kwargs):
        raise RuntimeError("hand-rolled connect_to_custom used")

    monkeypatch.setattr("weaviate.connect_to_custom", _boom)
    out_resync = codegraph_resync._build_client()
    out_copy = codegraph_vector_copy._build_client()
    assert out_resync is sentinel, "resync must connect via the shared factory"
    assert out_copy is sentinel, "vector_copy must connect via the shared factory"


# ── Review fixes (E1 / E2 / E3) ───────────────────────────────────────────


def _bare_connection(cls):
    """A ``ConnectionSync``/``ConnectionAsync`` built with ``object.__new__``
    — no init side effects. The ``connect()`` path under test only reads the
    attributes set here; every network-touching method is replaced by the
    per-test mock (the mocked server: meta REST fetch, REST session open,
    gRPC channel open, gRPC health ping). NO network is touched."""
    conn = object.__new__(cls)
    conn._connected = False
    conn._auth = None
    conn.embedded_db = None
    conn._skip_init_checks = False
    conn._grpc_max_msg_size = 100
    # __del__ touches these; set them so GC of the mocked object stays quiet
    conn._client = None
    conn._grpc_channel = None
    return conn


def test_pypi_check_noop_survives_sync_connect_path():
    """E1 (sync leg — non-regression pin, green before AND after): the
    patched ``_check_package_version`` keeps the ORIGINAL's sync result
    shape. ``ConnectionSync.connect`` runs it through ``executor.result``,
    which asserts the value is NOT an Awaitable — ``None`` passes."""
    import vco_lib.weaviate_helpers as wh
    from weaviate.connect import v4 as v4mod

    assert wh.disable_weaviate_client_pypi_check() is True

    conn = _bare_connection(v4mod.ConnectionSync)
    conn._open_connections_rest = lambda *a, **k: None
    conn.get_meta = lambda check_is_connected=True: {"version": "1.28.0"}
    conn.open_connection_grpc = lambda colour: None
    conn._ping_grpc = lambda colour: None
    conn.connect()
    assert conn._connected is True


def test_pypi_check_noop_survives_async_connect_path():
    """E1 (async leg — RED on the pre-fix tree): ``ConnectionAsync.connect``
    runs the patched method through ``executor.aresult``, which ASSERTS the
    return value IS an Awaitable. A no-op returning a bare ``None`` failed
    the assert, ``connect`` set ``_connected = False`` and re-raised — every
    async connect with ``skip_init_checks=False`` crashed after the patch."""
    import vco_lib.weaviate_helpers as wh
    from weaviate.connect import v4 as v4mod

    assert wh.disable_weaviate_client_pypi_check() is True

    async def _none() -> None:
        return None

    async def _meta():
        return {"version": "1.28.0"}

    conn = _bare_connection(v4mod.ConnectionAsync)
    conn._open_connections_rest = lambda *a, **k: _none()
    conn.get_meta = lambda check_is_connected=True: _meta()
    conn.open_connection_grpc = lambda colour: None
    conn._ping_grpc = lambda colour: _none()
    asyncio.run(conn.connect())
    assert conn._connected is True


def test_get_search_vector_without_a_service_is_loud_never_a_legacy_leg(
    monkeypatch,
):
    """v0.2.102 W6-B2 (owner ruling 2026-10-10) supersedes E2: the legacy
    code branch (an inline CodeEmbed call — CodeSage — whatever the graph is
    indexed in) is gone. svc unavailable → the loud error; nothing posted."""
    import pytest

    from weaviate_mcp import server
    from weaviate_mcp.embeddings import _get_search_vector
    from vco_lib.embed_outage import EmbeddingBackendUnavailable

    called: list = []

    async def _fake_inline(text, tolerance_secs=None):  # noqa: ARG001
        called.append(text)
        return [0.7]

    monkeypatch.setattr(server, "_get_embedding_service", lambda: None)
    monkeypatch.setattr(server, "_inline_code_embed_http", _fake_inline)
    monkeypatch.setattr(server, "ACTIVE_EMBEDDING", "codesage")
    for scheme in ("code", "kg"):
        with pytest.raises(EmbeddingBackendUnavailable) as ctx:
            asyncio.run(_get_search_vector("hello", scheme, None))
        assert ctx.value.scheme == scheme
        assert "never queried with a different embedding model" in str(ctx.value)
    assert called == []


def test_canonical_env_targets_carry_max_queue():
    """E3: the adoption/reconcile canonical table agrees with the compose
    surface (both compose files pin ``OLLAMA_MAX_QUEUE: "1000"``)."""
    from vco_lib import service_adoption

    targets = service_adoption.CANONICAL_ENV_TARGETS.get("ollama", {})
    assert targets.get("OLLAMA_KEEP_ALIVE") == "10m"
    assert targets.get("OLLAMA_MAX_QUEUE") == "1000"
    # the reconcile loop visits only BEHAVIOR_CRITICAL_ENV_KEYS — the queue
    # cap must be among them or the canonical value is never written
    assert "OLLAMA_MAX_QUEUE" in service_adoption.BEHAVIOR_CRITICAL_ENV_KEYS


def test_reconcile_writes_max_queue_fragment_when_installer_lacks_it():
    """E3: an ADOPTED (foreign) Ollama reconciled while the installer's
    effective config lacks MAX_QUEUE (e.g. an older infrastructure tree)
    gets the canonical 1000 as an override fragment — the reconcile surface
    and the compose surface agree."""
    from vco_lib.service_adoption import (
        LiveServiceState,
        MountSpec,
        reconcile_service,
    )

    live = LiveServiceState(
        mounts=(MountSpec("volume", "ollama_foreign", "/root/.ollama", ""),),
        env={"OLLAMA_KEEP_ALIVE": "30s"},  # no MAX_QUEUE → Ollama's default 512
    )
    cfg_service = {
        "environment": {"OLLAMA_KEEP_ALIVE": "10m"},  # no MAX_QUEUE here
        "volumes": ["ollama_data:/root/.ollama"],
    }
    fragments, refusals = reconcile_service("ollama", live, cfg_service, {})
    assert not refusals
    assert fragments["environment"].get("OLLAMA_MAX_QUEUE") == "1000"
    # keep-alive needs NO fragment: the installer's effective value already
    # equals the canonical target (fragment rule: only write on a difference)
    assert "OLLAMA_KEEP_ALIVE" not in fragments["environment"]


def test_config_drift_flags_adopted_ollama_on_default_queue():
    """E3: the update-path drift detector flags an adopted Ollama container
    still on Ollama's default queue cap (env key absent → 512)."""
    from vco_lib import service_reconcile
    from vco_lib.service_detection import ContainerInfo

    container = ContainerInfo(
        name="vco_ollama", image="ollama/ollama:0.5.7", state="running",
        env={"OLLAMA_KEEP_ALIVE": "10m"},  # drift-fixed on keep-alive only
    )
    drift = service_reconcile.config_drift(
        "ollama", container, _REPO_ROOT
    )
    assert "OLLAMA_MAX_QUEUE" in drift, (
        "an adopted container without the 1000 queue cap must be flagged"
    )
    assert drift["OLLAMA_MAX_QUEUE"] == (None, "1000")
    assert "OLLAMA_KEEP_ALIVE" not in drift  # already at the canonical value


if __name__ == "__main__":
    unittest.main()
