# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 round 2 (B2) — the CLIENT side of the code-embed 503 contract.

The service now QUEUES requests (owner rule: none dropped) and every refusal it
can still emit is a 503 + ``Retry-After`` (queue-wait bound exceeded, CUDA-OOM
load budget exhausted / fail-fast cooldown, inference OOM). A Retry-After that
no client reads is dead weight — pre-fix:

* ``CodeEmbedAdapter._embed_chunk`` raised a bare ``RuntimeError`` and DISCARDED
  the response headers, so the server's hint was unreachable;
* ``EmbeddingService._retry_once_on_503`` retried on a fixed 2/5/10 s schedule
  (~17 s total) — SHORTER than the server's ~6 min load-OOM budget — and never
  read Retry-After;
* the weaviate-MCP ``_inline_code_embed_http`` fallback posted ONCE and returned
  ``None`` on any non-200 — no retry at all ("Failed to generate query
  embedding" surfaced on the first 503).

The fix routes both retry layers through ONE shared helper
(:mod:`vco_lib.retry_after` — one concern, one home; no third copy) and raises
the client request timeout above the server's queue wait.

RED-PROOF: the module-level ``import vco_lib.retry_after`` and every test
naming the new attributes fail on the round-1 tree (the helper module does not
exist there, the adapter raises a plain RuntimeError, the retry loop is
schedule-only, the inline fallback does not retry). Back-compat pins (the
``VCO_EMBED_503_RETRY_DELAY=0`` legacy mode, the non-503 immediate re-raise,
the overflow-phrase contract) are GREEN before and after — they prove the fix
did not break the v0.2.73/v0.2.77 contract.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import types
from email.utils import formatdate
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vco_lib.retry_after as ra  # noqa: E402  (RED-PROOF: absent pre-fix)
from vco_lib.embed_outage import EmbeddingBackendUnavailable  # noqa: E402
from vco_lib.embedding_providers import codeembed as ce  # noqa: E402
from vco_lib.embedding_service import (  # noqa: E402
    _EMBED_503_RETRY_BACKOFFS,
    DEFAULT_EMBED_REQUEST_TIMEOUT_SECS,
    EmbeddingService,
    _is_context_overflow_error,
)

import weaviate_mcp.embeddings as emb  # noqa: E402


def _retry(fn, *args):
    """Invoke the helper unbound (it uses no instance state) — same harness as
    tests/test_v0273_embed_503_retry.py."""
    return EmbeddingService._retry_once_on_503(None, fn, *args)  # pyright: ignore[reportArgumentType] — deliberate unbound call; the method touches no instance state


def _oom503(retry_after=None, status_code=503):
    return ce.CodeEmbedHTTPError(
        f"CodeEmbed /embed returned HTTP {status_code}: busy",
        status_code=status_code,
        retry_after=retry_after,
    )


# ---------------------------------------------------------------------------
# vco_lib.retry_after — the ONE shared helper
# ---------------------------------------------------------------------------
def test_parse_retry_after_delta_seconds():
    assert ra.parse_retry_after("30") == 30.0
    assert ra.parse_retry_after("2.5") == 2.5
    assert ra.parse_retry_after(45) == 45.0
    assert ra.parse_retry_after("-3") == 0.0  # clamped, never negative
    assert ra.parse_retry_after(" 12 ") == 12.0


def test_parse_retry_after_http_date():
    future = formatdate(timeval=time.time() + 60, usegmt=True)
    val = ra.parse_retry_after(future)
    assert val is not None and 55.0 <= val <= 61.0
    past = formatdate(timeval=time.time() - 60, usegmt=True)
    assert ra.parse_retry_after(past) == 0.0  # already elapsed → retry now


def test_parse_retry_after_unusable_is_none():
    assert ra.parse_retry_after(None) is None
    assert ra.parse_retry_after("") is None
    assert ra.parse_retry_after("garbage") is None
    assert ra.parse_retry_after(True) is None  # bool is not a number here


def test_error_retry_after_reads_attribute_then_headers():
    exc = _oom503(retry_after=12.0)
    assert ra.error_retry_after(exc) == 12.0

    class _Resp:
        headers = {"Retry-After": "7"}

    exc2 = RuntimeError("HTTP 503 boom")
    exc2.response = _Resp()  # type: ignore[attr-defined]
    assert ra.error_retry_after(exc2) == 7.0

    assert ra.error_retry_after(RuntimeError("HTTP 503: x")) is None


def test_is_503_error_keeps_the_substring_contract():
    assert ra.is_503_error(_oom503()) is True  # structured status_code
    assert ra.is_503_error(RuntimeError("CodeEmbed /embed returned HTTP 503: busy")) is True
    assert ra.is_503_error(RuntimeError("CodeEmbed /embed returned HTTP 500: x")) is False


def test_resolve_503_tolerance(monkeypatch):
    monkeypatch.delenv(ra.EMBED_503_TOLERANCE_ENV, raising=False)
    assert ra.resolve_503_tolerance() == ra.DEFAULT_503_TOLERANCE_SECS == 420.0
    assert ra.DEFAULT_503_TOLERANCE_SECS >= 360.0, (
        "tolerance must be ≥ the server's default load-OOM budget (12 × 30 s)"
    )
    monkeypatch.setenv(ra.EMBED_503_TOLERANCE_ENV, "60")
    assert ra.resolve_503_tolerance() == 60.0
    monkeypatch.setenv(ra.EMBED_503_TOLERANCE_ENV, "bad")
    assert ra.resolve_503_tolerance() == 420.0
    monkeypatch.setenv(ra.EMBED_503_TOLERANCE_ENV, "-1")
    assert ra.resolve_503_tolerance() == 420.0
    monkeypatch.setenv(ra.EMBED_503_TOLERANCE_ENV, "0")
    assert ra.resolve_503_tolerance() == 0.0  # explicit fail-fast is valid


def test_resolve_503_delay_scale(monkeypatch):
    monkeypatch.delenv(ra.EMBED_503_RETRY_DELAY_ENV, raising=False)
    assert ra.resolve_503_delay_scale() is None
    monkeypatch.setenv(ra.EMBED_503_RETRY_DELAY_ENV, "0")
    assert ra.resolve_503_delay_scale() == 0.0
    monkeypatch.setenv(ra.EMBED_503_RETRY_DELAY_ENV, "4")
    assert ra.resolve_503_delay_scale(base=2.0) == 2.0
    monkeypatch.setenv(ra.EMBED_503_RETRY_DELAY_ENV, "not-a-number")
    assert ra.resolve_503_delay_scale() is None


def test_compute_503_delay_policy():
    sched = ra.DEFAULT_503_FALLBACK_BACKOFFS
    # Retry-After honoured VERBATIM (no jitter added):
    assert ra.compute_503_delay(1, 30.0, sched) == 30.0
    assert ra.compute_503_delay(3, 30.0, sched, scale=0.5) == 15.0
    # No Retry-After → schedule, last entry repeats past the end:
    assert ra.compute_503_delay(1, None, sched) == 2.0
    assert ra.compute_503_delay(2, None, sched) == 5.0
    assert ra.compute_503_delay(3, None, sched) == 10.0
    assert ra.compute_503_delay(9, None, sched) == 10.0
    assert ra.compute_503_delay(1, None, sched, scale=2.0) == 4.0
    assert ra.compute_503_delay(1, None, ()) == 0.0


# ---------------------------------------------------------------------------
# CodeEmbedAdapter — surfaces status + Retry-After, keeps the phrase contract
# ---------------------------------------------------------------------------
def test_adapter_surfaces_status_and_retry_after(monkeypatch):
    """RED-PROOF: pre-fix the adapter raised a bare RuntimeError and DISCARDED
    the response headers — the server's Retry-After was unreachable."""

    class _Resp:
        status_code = 503
        text = '{"detail":"Service at capacity ..."}'
        headers = {"Retry-After": "30"}

    monkeypatch.setattr(ce, "bounded_post", lambda *a, **k: _Resp())
    adapter = ce.CodeEmbedAdapter("http://fake:11440", session=None)  # type: ignore[arg-type]
    with pytest.raises(ce.CodeEmbedHTTPError) as exc_info:
        adapter.embed_batch(["x"])
    err = exc_info.value
    assert err.status_code == 503
    assert err.retry_after == 30.0
    # The message keeps the long-standing phrase contract (substring detectors
    # like _retry_once_on_503's and the overflow matcher key on it):
    assert str(err).startswith("CodeEmbed /embed returned HTTP 503:")
    assert isinstance(err, RuntimeError)  # existing except-RuntimeError callers


def test_adapter_error_still_matches_the_overflow_detector():
    """BACK-COMPAT pin (green before+after): the 400 over-window refusal stays
    recognisable to ``_is_context_overflow_error`` through the new error type."""
    err = ce.CodeEmbedHTTPError(
        'CodeEmbed /embed returned HTTP 400: {"detail":"input length exceeds '
        'the context length: text at index 0 is 2000 tokens, window is 1024"}',
        status_code=400,
    )
    assert _is_context_overflow_error(err) is True


def test_client_timeout_floor_exceeds_server_queue_wait():
    """RED-PROOF: the client deadline must exceed the server's queue wait
    (CODE_EMBED_QUEUE_WAIT_SECS, default 600 s) — pre-fix the adapter defaulted
    to 120 s and the service-wide default was 180 s, both BELOW the bound, so a
    queued request was aborted client-side (a drop by another name)."""
    assert ra.CODE_EMBED_CLIENT_TIMEOUT_SECS > 600.0
    assert DEFAULT_EMBED_REQUEST_TIMEOUT_SECS >= ra.CODE_EMBED_CLIENT_TIMEOUT_SECS
    adapter = ce.CodeEmbedAdapter("http://x", session=None)  # type: ignore[arg-type]
    assert adapter.timeout == ra.CODE_EMBED_CLIENT_TIMEOUT_SECS


# ---------------------------------------------------------------------------
# EmbeddingService._retry_once_on_503 — honours Retry-After within a tolerance
# ---------------------------------------------------------------------------
def test_retry_once_on_503_honours_retry_after_verbatim(monkeypatch):
    """RED-PROOF: the server's Retry-After (30 s) drives the wait — pre-fix the
    loop ignored it and slept the fixed 2 s / 5 s schedule."""
    monkeypatch.delenv("VCO_EMBED_503_RETRY_DELAY", raising=False)
    monkeypatch.delenv("VCO_EMBED_503_TOLERANCE_SECS", raising=False)
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _oom503(retry_after=30.0)
        return [0.5]

    assert _retry(_fn) == [0.5]
    assert calls["n"] == 3
    assert sleeps == [30.0, 30.0], (
        "Retry-After must be honoured VERBATIM — no schedule, no jitter"
    )


def test_retry_once_on_503_tolerance_outlasts_the_legacy_schedule(monkeypatch):
    """RED-PROOF: retries continue until the tolerance elapses — pre-fix the
    loop stopped after len(backoffs)+1 = 4 attempts (~17 s), SHORTER than the
    server's ~6 min load-OOM budget, so the client gave up mid-recovery."""
    monkeypatch.delenv("VCO_EMBED_503_RETRY_DELAY", raising=False)
    monkeypatch.setenv("VCO_EMBED_503_TOLERANCE_SECS", "0.25")
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise _oom503(retry_after=0.01)

    t0 = time.monotonic()
    with pytest.raises(ce.CodeEmbedHTTPError):
        _retry(_fn)
    elapsed = time.monotonic() - t0
    assert calls["n"] > len(_EMBED_503_RETRY_BACKOFFS) + 1, (
        "tolerance (not the fixed schedule) must bound the retries"
    )
    assert elapsed < 5.0, "the tolerance must also STOP the loop (finite)"


def test_retry_once_on_503_zero_delay_keeps_legacy_bounded_mode(monkeypatch):
    """BACK-COMPAT pin (green before+after): VCO_EMBED_503_RETRY_DELAY=0 keeps
    the legacy finite schedule — 4 attempts, zero sleeps — so existing test
    harnesses stay fast and finite."""
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    monkeypatch.delenv("VCO_EMBED_503_TOLERANCE_SECS", raising=False)
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise RuntimeError("CodeEmbed /embed returned HTTP 503: busy")

    with pytest.raises(RuntimeError):
        _retry(_fn)
    assert calls["n"] == len(_EMBED_503_RETRY_BACKOFFS) + 1
    assert sleeps == []


def test_retry_once_on_503_non_503_still_immediate(monkeypatch):
    """BACK-COMPAT pin (green before+after): only 503 is retryable."""
    monkeypatch.delenv("VCO_EMBED_503_RETRY_DELAY", raising=False)
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise RuntimeError("CodeEmbed /embed returned HTTP 500: boom")

    with pytest.raises(RuntimeError):
        _retry(_fn)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# weaviate-MCP inline fallback — the SAME shared policy, async
# ---------------------------------------------------------------------------
class _FakeAioResponse:
    def __init__(self, status, headers=None, payload=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeAioSession:
    script: "list[_FakeAioResponse]" = []
    posts: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, timeout=None):
        _FakeAioSession.posts.append({"url": url, "json": json, "timeout": timeout})
        idx = min(len(_FakeAioSession.posts) - 1, len(_FakeAioSession.script) - 1)
        return _FakeAioSession.script[idx]


@pytest.fixture()
def fake_mcp_server(monkeypatch):
    """Inject a fake ``weaviate_mcp.server`` (``_inline_code_embed_http`` does
    ``from . import server`` at call time)."""
    import weaviate_mcp as pkg

    fake: Any = types.ModuleType("weaviate_mcp.server")
    fake.CODE_EMBED_SERVICE_URL = "http://fake-code-embed:11440"
    fake.logger = logging.getLogger("test_inline_code_embed_fallback")
    monkeypatch.setattr(pkg, "server", fake, raising=False)
    monkeypatch.setitem(sys.modules, "weaviate_mcp.server", fake)
    _FakeAioSession.script = []
    _FakeAioSession.posts = []
    monkeypatch.setattr(emb.aiohttp, "ClientSession", _FakeAioSession)
    return fake


def test_inline_fallback_retries_503_then_succeeds(monkeypatch, fake_mcp_server):
    """RED-PROOF (the coordinator's case): pre-fix the fallback posted ONCE and
    returned None on the first 503 — "Failed to generate query embedding"
    surfaced immediately. Now it honours Retry-After via the shared policy and
    the second attempt's 200 is returned."""
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")  # zero-delay mode
    monkeypatch.delenv("VCO_EMBED_503_TOLERANCE_SECS", raising=False)
    _FakeAioSession.script = [
        _FakeAioResponse(503, headers={"Retry-After": "30"}),
        _FakeAioResponse(200, payload={"embedding": [0.1, 0.2]}),
    ]

    out = asyncio.run(emb._inline_code_embed_http("def f(): pass"))

    assert out == [0.1, 0.2]
    assert len(_FakeAioSession.posts) == 2
    # The per-request timeout must exceed the server's queue wait (600 s):
    assert _FakeAioSession.posts[0]["timeout"].total == ra.CODE_EMBED_CLIENT_TIMEOUT_SECS


def test_inline_fallback_persistent_503_gives_up_bounded(monkeypatch, fake_mcp_server):
    """RED-PROOF: a persistent 503 retries up to the shared bound (legacy
    zero-delay mode: len(schedule)+1 attempts) and THEN returns None — the
    caller's degrade path still runs; pre-fix it stopped after attempt 1."""
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    _FakeAioSession.script = [_FakeAioResponse(503, headers={"Retry-After": "0"})]

    out = asyncio.run(emb._inline_code_embed_http("def f(): pass"))

    assert out is None
    assert len(_FakeAioSession.posts) == len(ra.DEFAULT_503_FALLBACK_BACKOFFS) + 1


def test_inline_fallback_non_503_fails_immediately(monkeypatch, fake_mcp_server):
    """LEAVE-ALONE (green before+after): a non-503 status is not transient —
    one post, None, no retry."""
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    _FakeAioSession.script = [_FakeAioResponse(500)]

    out = asyncio.run(emb._inline_code_embed_http("def f(): pass"))

    assert out is None
    assert len(_FakeAioSession.posts) == 1


def test_inline_fallback_honours_retry_after_delay(monkeypatch, fake_mcp_server):
    """The async leg waits the server's Retry-After (scaled by the knob), not a
    private schedule — proven by recording asyncio.sleep."""
    monkeypatch.delenv("VCO_EMBED_503_RETRY_DELAY", raising=False)
    monkeypatch.setenv("VCO_EMBED_503_TOLERANCE_SECS", "30")
    slept: list = []

    async def _fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(emb.asyncio, "sleep", _fake_sleep)
    _FakeAioSession.script = [
        _FakeAioResponse(503, headers={"Retry-After": "12"}),
        _FakeAioResponse(200, payload={"embedding": [1.0]}),
    ]

    out = asyncio.run(emb._inline_code_embed_http("x"))

    assert out == [1.0]
    assert slept == [12.0], "Retry-After honoured verbatim on the async leg too"


# ---------------------------------------------------------------------------
# v0.2.102 r3 — the INTERACTIVE tolerance split (re-review FIX FIRST)
# ---------------------------------------------------------------------------
def test_resolve_503_interactive_tolerance(monkeypatch):
    """RED-PROOF (r3): the interactive knob + resolver did not exist pre-fix,
    and the interactive bound may never EXCEED the batch tolerance."""
    monkeypatch.delenv(ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV, raising=False)
    monkeypatch.delenv(ra.EMBED_503_TOLERANCE_ENV, raising=False)
    assert (
        ra.resolve_503_interactive_tolerance()
        == ra.DEFAULT_503_INTERACTIVE_TOLERANCE_SECS
        == 90.0
    )
    # clamped to the batch tolerance (interactive ≤ batch, always):
    monkeypatch.setenv(ra.EMBED_503_TOLERANCE_ENV, "30")
    assert ra.resolve_503_interactive_tolerance() == 30.0
    monkeypatch.delenv(ra.EMBED_503_TOLERANCE_ENV, raising=False)
    monkeypatch.setenv(ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV, "10")
    assert ra.resolve_503_interactive_tolerance() == 10.0
    monkeypatch.setenv(ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV, "bad")
    assert ra.resolve_503_interactive_tolerance() == 90.0
    monkeypatch.setenv(ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV, "-1")
    assert ra.resolve_503_interactive_tolerance() == 90.0
    monkeypatch.setenv(ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV, "0")
    assert ra.resolve_503_interactive_tolerance() == 0.0  # explicit fail-fast


def test_tolerance_override_binds_the_shared_resolver(monkeypatch):
    """RED-PROOF (r3): the override is what lets the interactive path bound the
    PRODUCTION retry loop (``_retry_once_on_503`` resolves its tolerance per
    call through ``resolve_503_tolerance``) without threading a parameter
    through every batch call site — and it travels with ``asyncio.to_thread``
    (exactly how the MCP's svc leg invokes it)."""
    monkeypatch.delenv(ra.EMBED_503_TOLERANCE_ENV, raising=False)
    assert ra.resolve_503_tolerance() == ra.DEFAULT_503_TOLERANCE_SECS == 420.0

    async def _resolve_in_thread() -> float:
        return await asyncio.to_thread(ra.resolve_503_tolerance)

    token = ra.set_503_tolerance_override(5.0)
    try:
        assert ra.resolve_503_tolerance() == 5.0
        assert asyncio.run(_resolve_in_thread()) == 5.0, (
            "asyncio.to_thread copies the context — the override must hold "
            "inside the worker thread"
        )
    finally:
        ra.reset_503_tolerance_override(token)
    assert ra.resolve_503_tolerance() == 420.0, "reset restores the batch value"


def _run_query(text):
    """Run the MCP query path; return the vector or the raised loud error."""
    try:
        return asyncio.run(emb.get_code_query_embedding(text))
    except EmbeddingBackendUnavailable as exc:
        return exc


@pytest.fixture()
def fake_query_env(monkeypatch):
    """Fake ``weaviate_mcp.server`` carrying BOTH a fake EmbeddingService (svc)
    and a RECORDING inline fallback, plus the fake aiohttp session — the
    harness for the interactive query path."""
    import weaviate_mcp as pkg

    fake: Any = types.ModuleType("weaviate_mcp.server")
    fake.CODE_EMBED_SERVICE_URL = "http://fake-code-embed:11440"
    fake.logger = logging.getLogger("test_interactive_query")
    state = {"inline_calls": 0}

    async def _recording_inline(text, **kwargs):
        state["inline_calls"] += 1
        return await emb._inline_code_embed_http(text, **kwargs)

    fake._inline_code_embed_http = _recording_inline
    svc: Any = types.SimpleNamespace()
    fake._get_embedding_service = lambda: svc
    monkeypatch.setattr(pkg, "server", fake, raising=False)
    monkeypatch.setitem(sys.modules, "weaviate_mcp.server", fake)
    _FakeAioSession.script = []
    _FakeAioSession.posts = []
    monkeypatch.setattr(emb.aiohttp, "ClientSession", _FakeAioSession)
    return fake, svc, state


def test_interactive_query_persistent_503_gives_up_within_the_interactive_bound(
    monkeypatch, fake_query_env
):
    """RED-PROOF (r3, the coordinator's case): a persistently-503 service must
    fail the INTERACTIVE query within the interactive bound and must NOT run
    the inline fallback as a second full retry. The fake svc runs the REAL
    production retry loop (``_retry_once_on_503``), so the test also proves the
    context override reaches it through ``asyncio.to_thread``. Pre-fix, the svc
    leg spent the BATCH tolerance (2.0 s here, 420 s in production) and THEN
    the inline fallback spent another one."""
    fake, svc, state = fake_query_env
    monkeypatch.delenv("VCO_EMBED_503_RETRY_DELAY", raising=False)
    monkeypatch.setenv("VCO_EMBED_503_TOLERANCE_SECS", "2.0")  # batch (red: pre-fix uses this)
    monkeypatch.setenv("VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS", "0.3")
    calls = {"n": 0}

    def _persistently_503():
        calls["n"] += 1
        raise _oom503(retry_after=0.02)

    def _embed_code_query(text, task=None):
        return EmbeddingService._retry_once_on_503(None, _persistently_503)  # pyright: ignore[reportArgumentType] — deliberate unbound call (no instance state)

    svc.embed_code_query = _embed_code_query

    t0 = time.monotonic()
    out = _run_query("def f(): pass")
    elapsed = time.monotonic() - t0

    assert isinstance(out, EmbeddingBackendUnavailable), (
        "a spent interactive bound is the LOUD busy error (W6-B2), not None")
    assert "busy" in str(out) and "503" in str(out)
    assert state["inline_calls"] == 0, (
        "NO second tolerance via the inline fallback after 503 exhaustion"
    )
    assert elapsed < 1.5, "the interactive bound (0.3 s here) must bound the svc leg"
    assert calls["n"] > 1, "the svc leg genuinely retried before giving up"


def test_interactive_query_connection_refused_never_uses_another_endpoint(
    monkeypatch, fake_query_env
):
    """Owner ruling 2026-10-10 (W6-B2): NON-503 unavailability is a LOUD
    error — the raw CodeEmbed endpoint (it serves CodeSage, whatever the graph
    is indexed in) is never asked instead. Pre-W6-B2 this fell through to the
    inline fallback and returned its vector."""
    fake, svc, state = fake_query_env
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")

    def _refused(text, task=None):
        raise ConnectionError("[Errno 111] Connection refused")

    svc.embed_code_query = _refused
    _FakeAioSession.script = [
        _FakeAioResponse(200, payload={"embedding": [0.3, 0.4]})
    ]

    out = _run_query("x")

    assert isinstance(out, EmbeddingBackendUnavailable)
    assert "never queried with a different embedding model" in str(out)
    assert state["inline_calls"] == 0
    assert _FakeAioSession.posts == []


def test_query_path_without_a_service_is_loud(monkeypatch, fake_query_env):
    """W6-B2: an unconfigured service is the loud error naming the
    construction failure — no inline CodeEmbed post at any tolerance."""
    fake, svc, state = fake_query_env
    fake._get_embedding_service = lambda: None
    fake._embed_service_construction_error = "Ollama did not answer"
    _FakeAioSession.script = [
        _FakeAioResponse(200, payload={"embedding": [0.3, 0.4]})
    ]

    out = _run_query("x")

    assert isinstance(out, EmbeddingBackendUnavailable)
    assert "Ollama did not answer" in str(out)
    assert state["inline_calls"] == 0
    assert _FakeAioSession.posts == []


def test_inline_fallback_default_tolerance_is_the_batch_one(
    monkeypatch, fake_mcp_server
):
    """LEAVE-ALONE pin (green before+after): with NO tolerance argument the
    inline fallback keeps the BATCH tolerance (``VCO_EMBED_503_TOLERANCE_SECS``
    — analyzer / sync / backfill callers rely on it riding out a full
    server-side recovery)."""
    monkeypatch.delenv("VCO_EMBED_503_RETRY_DELAY", raising=False)
    monkeypatch.setenv("VCO_EMBED_503_TOLERANCE_SECS", "0.25")
    monkeypatch.delenv("VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS", raising=False)
    _FakeAioSession.script = [
        _FakeAioResponse(503, headers={"Retry-After": "0.02"})
    ]

    t0 = time.monotonic()
    out = asyncio.run(emb._inline_code_embed_http("x"))  # no tolerance arg
    elapsed = time.monotonic() - t0

    assert out is None
    assert len(_FakeAioSession.posts) > 1
    assert elapsed >= 0.25, "it used the BATCH env tolerance, not a shorter default"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
