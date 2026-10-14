# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 — code-embed idle unload / reload / concurrency ROOT-CAUSE fix.

Defect (live, 2026-10-07, 16 GB GPU): the idle-unload set ``_st_model = None``
then ``empty_cache()`` and logged ``VRAM allocated 7889 MB → 7889 MB (freed 0
MB)``. The weights stayed alive because the SentenceTransformer / nn.Module
object graph is CYCLIC and no ``gc.collect()`` ran between the drop and the
measure. The next request then loaded a SECOND copy on top of the un-freed
first one → ``torch.OutOfMemoryError`` → every request failed until a container
restart, and the log showed three "Loading model" attempts in 16 s (each failed
request retried a full load — no single-flight, no OOM budget).

These tests exercise the fix WITHOUT a GPU or the real 7 GB model: a fake torch
reports ``cuda.memory_allocated()`` from a registry the fake model increments on
construction and decrements in ``__del__``; a fake SentenceTransformer counts
constructs and can raise a torch-shaped ``OutOfMemoryError``.

RED-PROOF: every test marked ``# RED-PROOF`` fails on the pre-fix code (the
freed-0 unload, the discarded handle, the missing OOM retry budget, the missing
single-flight lock, the missing /health fields, the missing ``_is_cuda_oom``).
The leave-alone tests are green before and after — they pin the invariants the
fix must NOT break (in-flight blocks unload; knob=0 never unloads; the request
path queues and never drops).
"""
from __future__ import annotations

import asyncio
import gc
import logging
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claude_mcp_servers.code_embedding_service.server as es  # noqa: E402

_LOGGER_NAME = "code_embedding_service"
_MODEL_BYTES = 7_000_000_000  # ~7 GB, the CodeSage footprint


# ---------------------------------------------------------------------------
# Fakes — no GPU, no real torch tensors, no real SentenceTransformer
# ---------------------------------------------------------------------------
class OutOfMemoryError(RuntimeError):
    """Stands in for ``torch.OutOfMemoryError``. ``_is_cuda_oom`` matches it by
    class NAME (``outofmemoryerror`` contains ``outofmemory``) and by message."""


class _FakeCuda:
    def __init__(self, registry: dict):
        self._registry = registry
        self.empty_cache_calls = 0

    def is_available(self) -> bool:
        return True

    def memory_allocated(self) -> float:
        return float(self._registry.get("allocated", 0))

    def empty_cache(self) -> None:
        self.empty_cache_calls += 1


class _FakeTorch:
    """``torch`` stand-in whose ``cuda.memory_allocated()`` reads the registry the
    fake models write to — so "freed bytes" is measurable without a GPU."""

    def __init__(self, registry: dict):
        self.cuda = _FakeCuda(registry)
        self.backends = types.SimpleNamespace(mps=None)


class _FakeTokenizer:
    def __call__(self, text, add_special_tokens=True):
        return {"input_ids": list(range(min(len(text), 50)))}


class _FakeEncoded:
    def __init__(self, arr):
        self._arr = arr

    def tolist(self):
        return self._arr


class _FakeWeightsModel:
    """A model that 'holds' ``nbytes`` in the registry and releases them in
    ``__del__``. With ``cyclic=True`` it forms a self-reference cycle, so — like
    the real SentenceTransformer/nn.Module graph — it is reclaimed ONLY by the
    cyclic GC, never by refcounting alone. That is the exact shape the pre-fix
    unload missed (no ``gc.collect()`` → freed 0 MB)."""

    def __init__(self, registry: dict, nbytes: int = _MODEL_BYTES, cyclic: bool = True):
        self._registry = registry
        self._nbytes = nbytes
        registry["allocated"] = registry.get("allocated", 0) + nbytes
        if cyclic:
            self._self_ref = self  # reference cycle → needs gc.collect()
        self.max_seq_length = 1024
        self.tokenizer = _FakeTokenizer()

    def __del__(self):
        try:
            self._registry["allocated"] = self._registry.get("allocated", 0) - self._nbytes
        except Exception:  # pragma: no cover — interpreter shutdown
            pass

    def get_sentence_embedding_dimension(self) -> int:
        return 2048

    def encode(self, texts, **kwargs):
        return _FakeEncoded([[0.1, 0.2, 0.3] for _ in texts])


class _FakeST:
    """Fake ``SentenceTransformer``. Class-level knobs (reset per test):
    ``oom_first_n`` raises OOM for the first N constructs; ``always_oom`` raises
    on every construct; ``block_event`` makes the constructor block (so concurrent
    loaders overlap deterministically). ``calls_log`` records every construct."""

    calls_log: list = []
    oom_first_n: int = 0
    always_oom: bool = False
    block_event: "threading.Event | None" = None

    def __init__(self, name, **kwargs):
        _FakeST.calls_log.append(name)
        n = len(_FakeST.calls_log)
        if _FakeST.always_oom or n <= _FakeST.oom_first_n:
            raise OutOfMemoryError(
                f"CUDA out of memory. Tried to allocate 2.00 GiB (construct {n})"
            )
        if _FakeST.block_event is not None:
            _FakeST.block_event.wait(timeout=5)
        self.name = name
        self.max_seq_length = 1024
        self.tokenizer = _FakeTokenizer()

    def get_sentence_embedding_dimension(self) -> int:
        return 3

    def encode(self, texts, **kwargs):
        return _FakeEncoded([[0.1, 0.2, 0.3] for _ in texts])


def _fake_st_module() -> types.ModuleType:
    """Reset ``_FakeST`` state and return a ``sentence_transformers`` module whose
    ``SentenceTransformer`` is the fake."""
    _FakeST.calls_log = []
    _FakeST.oom_first_n = 0
    _FakeST.always_oom = False
    _FakeST.block_event = None
    mod: Any = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = _FakeST
    return mod


_RESET_KEYS = [
    "BACKEND", "DEVICE", "DTYPE", "IDLE_UNLOAD_SECS", "MAX_CONCURRENT",
    "MAX_SEQ_LEN", "INSTRUCTION", "_st_model", "_st_model_dim", "_in_flight",
    "_last_used", "_inference_lock", "_request_semaphore", "_ollama_dim_cache",
    "LOAD_OOM_RETRIES", "LOAD_OOM_BACKOFF_SECS", "UNLOAD_FREED_MIN_MB",
    "_last_unload_freed_mb", "_last_load_error",
    # Round 2 (v0.2.102 r2): the queue bound + the load fail-fast cooldown.
    "QUEUE_WAIT_SECS", "_load_unavailable_until",
]


@pytest.fixture(autouse=True)
def _reset_service_state():
    """Restore every module global we mutate; give each test a fresh lock,
    semaphore and load-lock so a crashed test cannot poison the next."""
    saved = {k: getattr(es, k, None) for k in _RESET_KEYS}
    saved_lock = es._load_lock
    es._inference_lock = None
    es._request_semaphore = None
    es._load_lock = threading.Lock()
    _fake_st_module()  # reset fake ST class state
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(es, k, v)
        es._load_lock = saved_lock
        _fake_st_module()


def _gpu_env():
    """Common gpu-backend env for the load/unload tests (cpu device so the fake
    torch is not probed for CUDA during device resolution)."""
    es.BACKEND = "gpu"
    es.DEVICE = "cpu"
    es._st_model = None
    es._st_model_dim = None
    es._in_flight = 0
    es.LOAD_OOM_RETRIES = 3
    es.LOAD_OOM_BACKOFF_SECS = 0.0
    es.UNLOAD_FREED_MIN_MB = 64.0


# ---------------------------------------------------------------------------
# 1. Unload with a reference cycle actually frees (gc.collect before measure)
# ---------------------------------------------------------------------------
def test_unload_with_reference_cycle_frees_memory():
    """RED-PROOF: the model is a self-cycle holding ~7 GB. The pre-fix unload did
    ``_st_model = None`` then ``empty_cache()`` with NO ``gc.collect()``, so the
    cycle kept the weights alive → freed 0 MB. The fix collects first → freed > 0.
    """
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    es.BACKEND = "gpu"
    es.DEVICE = "cpu"
    es.IDLE_UNLOAD_SECS = 10.0
    es._in_flight = 0
    es._last_used = 1000.0
    es._st_model = _FakeWeightsModel(registry, _MODEL_BYTES, cyclic=True)
    es._st_model_dim = 2048
    before = registry["allocated"]
    assert before == _MODEL_BYTES

    # Disable automatic GC so the ONLY collection is the explicit gc.collect() the
    # fix performs — this is what makes the red-proof deterministic (the pre-fix
    # code never collects, so the cycle survives and freed == 0).
    gc.disable()
    try:
        with patch.dict(sys.modules, {"torch": fake_torch}):
            unloaded = asyncio.run(es._maybe_unload_idle_model(now=1000.0 + 11.0))
    finally:
        gc.enable()

    assert unloaded is True
    assert es._st_model is None
    freed = before - registry["allocated"]
    assert freed > 0, (
        "gc.collect() must run between the drop and the measure so the cyclic "
        "weights are actually freed (pre-fix: freed 0 MB)"
    )
    assert registry["allocated"] == 0
    assert es._last_unload_freed_mb is not None and es._last_unload_freed_mb > 0


# ---------------------------------------------------------------------------
# 2. Unload that frees nothing KEEPS the handle (never a second copy)
# ---------------------------------------------------------------------------
def test_unload_freed_zero_keeps_handle_and_warns(caplog):
    """RED-PROOF: when a live reference still pins the weights (freed ≈ 0), the
    fix recovers the handle via weakref, marks the model loaded, logs a WARNING
    and returns False — so the next request REUSES this copy. The pre-fix code
    discarded the handle (``_st_model = None``) and returned True, which is what
    made the next request allocate a second copy and OOM."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    es.BACKEND = "gpu"
    es.DEVICE = "cpu"
    es.IDLE_UNLOAD_SECS = 10.0
    es._in_flight = 0
    es._last_used = 1000.0
    es.UNLOAD_FREED_MIN_MB = 64.0
    # cyclic=False + an external strong ref → gc.collect() cannot reclaim it, so
    # the free genuinely does not happen (freed == 0).
    model = _FakeWeightsModel(registry, _MODEL_BYTES, cyclic=False)
    external_holder = [model]
    es._st_model = model
    es._st_model_dim = 2048

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            unloaded = asyncio.run(es._maybe_unload_idle_model(now=1000.0 + 11.0))
            assert unloaded is False, "must report NOT unloaded when it could not free"
            assert es._st_model is model, "handle must be KEPT (marked loaded), not discarded"
            # No second load later: the lazy loader reuses the kept handle.
            again = es._load_gpu_model()
            assert again is model
            assert len(_FakeST.calls_log) == 0, "must NOT construct a second copy"

    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned, "a WARNING must be logged naming the un-freed weights"
    assert any("still hold" in r.getMessage().lower() for r in warned), (
        "the WARNING must say a live reference still holds the model"
    )
    external_holder.clear()


# ---------------------------------------------------------------------------
# 3. Reload OOM: bounded backoff retries, then a clean 503 + Retry-After
# ---------------------------------------------------------------------------
def test_reload_oom_once_then_succeeds():
    """RED-PROOF: the first construct OOMs, the retry succeeds. The pre-fix loader
    had no retry — the first OOM propagated and the request failed."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    _FakeST.oom_first_n = 1  # first construct OOMs, second succeeds
    _gpu_env()
    es.LOAD_OOM_RETRIES = 3
    es.LOAD_OOM_BACKOFF_SECS = 0.0

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        model = es._load_gpu_model()

    assert model is not None
    assert es._st_model is model
    assert es._st_model_dim == 3
    assert es._last_load_error is None
    assert len(_FakeST.calls_log) == 2, "one OOM then one success = two constructs"


def test_reload_oom_past_budget_raises_model_load_unavailable():
    """RED-PROOF: every construct OOMs; after the bounded budget the loader raises
    ``_ModelLoadUnavailable`` (which the endpoint maps to 503). The pre-fix loader
    raised the raw OOM on the first attempt (→ 500 traceback)."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    _FakeST.always_oom = True
    _gpu_env()
    es.LOAD_OOM_RETRIES = 2  # 3 attempts total
    es.LOAD_OOM_BACKOFF_SECS = 0.0

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with pytest.raises(es._ModelLoadUnavailable):
            es._load_gpu_model()

    assert len(_FakeST.calls_log) == 3, "must try exactly retries+1 times"
    assert es._st_model is None
    assert es._last_load_error is not None and "out of memory" in es._last_load_error.lower()


def test_load_oom_backoff_sleeps_between_retries():
    """RED-PROOF: the bounded budget waits (gc + empty_cache + backoff) between
    OOM attempts. The pre-fix loader never slept — it failed on attempt one."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    _FakeST.always_oom = True
    _gpu_env()
    es.LOAD_OOM_RETRIES = 2  # 3 attempts → 2 backoff sleeps
    es.LOAD_OOM_BACKOFF_SECS = 7.0

    sleeps: list = []
    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with patch.object(es.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            with pytest.raises(es._ModelLoadUnavailable):
                es._load_gpu_model()

    assert sleeps == [7.0, 7.0], "one backoff sleep between each pair of attempts"


def test_embed_endpoint_maps_load_oom_to_503_with_retry_after():
    """RED-PROOF: a load that exhausts its OOM budget surfaces to the client as a
    503 WITH a Retry-After header — never a 500 with a traceback. The pre-fix
    endpoint let the raw OOM escape (Starlette → 500)."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    _FakeST.always_oom = True
    _gpu_env()
    es.LOAD_OOM_RETRIES = 1  # 2 attempts, fast
    es.LOAD_OOM_BACKOFF_SECS = 3.0
    es.MAX_CONCURRENT = 4
    es._in_flight = 0

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(es.embed_endpoint(es.EmbedRequest(texts=["def f(): pass"])))

    assert exc_info.value.status_code == 503
    headers = exc_info.value.headers or {}
    assert "Retry-After" in headers, "a 503 for an OOM must carry Retry-After"
    assert int(headers["Retry-After"]) >= 1


def test_embed_endpoint_maps_inference_oom_to_503():
    """An OOM raised during ``encode`` (not load) is ALSO mapped to 503 — the
    endpoint never leaks a 500 traceback for a CUDA OOM."""
    _gpu_env()
    es.MAX_CONCURRENT = 4
    es._in_flight = 0

    def _oom_embed(*args, **kwargs):
        raise OutOfMemoryError("CUDA out of memory during encode")

    with patch.object(es, "embed", side_effect=_oom_embed):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(es.embed_endpoint(es.EmbedRequest(texts=["x"])))
    assert exc_info.value.status_code == 503
    assert "Retry-After" in (exc_info.value.headers or {})


# ---------------------------------------------------------------------------
# 4. Single-flight load — one loader, waiters reuse the result
# ---------------------------------------------------------------------------
def test_concurrent_loaders_are_single_flight():
    """RED-PROOF: N threads race into ``_load_gpu_model`` while the model is
    unloaded. The fix's ``_load_lock`` + double-check produce exactly ONE
    construct; the waiters reuse its result. The pre-fix loader had no load lock,
    so every racing thread constructed its own copy (the "three loads in 16 s")."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    block = threading.Event()
    _FakeST.block_event = block  # constructor blocks so all loaders overlap
    _gpu_env()
    es.LOAD_OOM_RETRIES = 0
    es.LOAD_OOM_BACKOFF_SECS = 0.0

    n = 6
    start = threading.Barrier(n)
    results: list = []
    errors: list = []

    def _worker():
        try:
            start.wait(timeout=5)
            results.append(es._load_gpu_model())
        except BaseException as exc:  # noqa: BLE001 — record, assert on the main thread
            errors.append(exc)

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        threads = [threading.Thread(target=_worker) for _ in range(n)]
        for t in threads:
            t.start()
        # Let every thread reach its blocking point (constructor, or the load lock).
        time.sleep(0.3)
        block.set()
        for t in threads:
            t.join(timeout=5)

    assert not errors, f"loaders raised: {errors!r}"
    assert len(_FakeST.calls_log) == 1, (
        f"exactly ONE construct expected (single-flight), got {len(_FakeST.calls_log)}"
    )
    assert len(results) == n
    assert all(r is results[0] for r in results), "all waiters reuse the one loaded model"


def test_concurrent_requests_during_load_all_succeed_one_load():
    """Request-path invariant (regression guard): N concurrent ``/embed`` requests
    arriving while the model is unloaded QUEUE on the inference lock, trigger
    exactly ONE load, and ALL succeed — none is dropped. Uses ``MAX_CONCURRENT``
    high enough to admit all N (the shed boundary is a separate, intended knob)."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    _gpu_env()
    es.LOAD_OOM_RETRIES = 0
    es.MAX_CONCURRENT = 8
    es._in_flight = 0
    es._request_semaphore = None
    es._inference_lock = None

    n = 5

    async def _run():
        return await asyncio.gather(
            *[es.embed_endpoint(es.EmbedRequest(texts=["def g(): pass"])) for _ in range(n)]
        )

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with patch.object(es, "_resolve_query_instruction", return_value=""):
            responses = asyncio.run(_run())

    assert len(responses) == n
    assert all(r.embeddings == [[0.1, 0.2, 0.3]] for r in responses)
    assert len(_FakeST.calls_log) == 1, "N concurrent requests must load the model once"
    assert es._in_flight == 0, "in-flight counter must return to 0 after all complete"


# ---------------------------------------------------------------------------
# 5. Leave-alone invariants the fix must NOT break
# ---------------------------------------------------------------------------
def test_in_flight_request_blocks_unload():
    """LEAVE-ALONE: an in-flight request (``_in_flight > 0``) blocks the unload
    even past the idle window — the C.3 locking invariant is preserved."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 1
    es._last_used = 1000.0
    unloaded = asyncio.run(es._maybe_unload_idle_model(now=1000.0 + 1000.0))
    assert unloaded is False
    assert es._st_model is model


def test_idle_unload_zero_never_unloads():
    """LEAVE-ALONE: ``CODE_EMBED_IDLE_UNLOAD_SECS=0`` → never unload, even after an
    eternity idle."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 0.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 0
    es._last_used = 0.0
    unloaded = asyncio.run(es._maybe_unload_idle_model(now=1_000_000.0))
    assert unloaded is False
    assert es._st_model is model


# ---------------------------------------------------------------------------
# /health truthfulness + the OOM classifier
# ---------------------------------------------------------------------------
def test_health_reports_last_unload_and_load_error():
    """RED-PROOF: /health carries the last unload's freed MB and the last load
    error (absent pre-fix → KeyError). It also must NOT force a load to report
    ``dim`` — the cached dim is served and the model stays unloaded."""
    es.BACKEND = "gpu"
    es._st_model = None
    es._st_model_dim = 2048
    es._in_flight = 0
    es._last_unload_freed_mb = 7123.0
    es._last_load_error = "CUDA out of memory persisted through 7 load attempt(s)"

    def _boom():
        raise AssertionError("/health must not force a model load via get_dim()")

    with patch.object(es, "get_dim", side_effect=_boom):
        payload = asyncio.run(es.health())

    assert payload["status"] == "ok"
    assert payload["model_loaded"] is False
    assert payload["dim"] == 2048
    assert payload["last_unload_freed_mb"] == 7123.0
    assert payload["last_load_error"] == es._last_load_error


def test_is_cuda_oom_classifier():
    """RED-PROOF: ``_is_cuda_oom`` did not exist pre-fix. It must match torch's
    ``OutOfMemoryError`` by name and the phrase by message, and reject the rest."""
    assert es._is_cuda_oom(OutOfMemoryError("x")) is True
    assert es._is_cuda_oom(RuntimeError("CUDA out of memory. Tried to allocate")) is True
    assert es._is_cuda_oom(RuntimeError("some other failure")) is False
    assert es._is_cuda_oom(ValueError("out of memory")) is True
    assert es._is_cuda_oom(None) is False


def test_retry_after_secs_is_positive():
    """The Retry-After hint is a positive integer tied to the OOM backoff."""
    es.LOAD_OOM_BACKOFF_SECS = 12.0
    assert es._retry_after_secs() == 12
    es.LOAD_OOM_BACKOFF_SECS = 0.0
    assert es._retry_after_secs() >= 1


# ---------------------------------------------------------------------------
# Env parsing for the new knobs
# ---------------------------------------------------------------------------
def test_load_oom_env_parsing(monkeypatch):
    monkeypatch.setenv("CODE_EMBED_LOAD_OOM_RETRIES", "9")
    assert es._resolve_load_oom_retries() == 9
    monkeypatch.setenv("CODE_EMBED_LOAD_OOM_RETRIES", "bad")
    assert es._resolve_load_oom_retries() == 12  # r2 default (was 6)
    monkeypatch.setenv("CODE_EMBED_LOAD_OOM_RETRIES", "-3")
    assert es._resolve_load_oom_retries() == 0

    monkeypatch.setenv("CODE_EMBED_LOAD_OOM_BACKOFF_SECS", "2.5")
    assert es._resolve_load_oom_backoff_secs() == 2.5
    monkeypatch.setenv("CODE_EMBED_LOAD_OOM_BACKOFF_SECS", "bad")
    assert es._resolve_load_oom_backoff_secs() == 30.0  # r2 default (was 10)

    monkeypatch.setenv("CODE_EMBED_UNLOAD_FREED_MIN_MB", "128")
    assert es._resolve_unload_freed_min_mb() == 128.0
    monkeypatch.setenv("CODE_EMBED_UNLOAD_FREED_MIN_MB", "bad")
    assert es._resolve_unload_freed_min_mb() == 64.0


# ---------------------------------------------------------------------------
# ROUND 2 (v0.2.102 r2) — B1: the bounded FIFO queue replaces the C-9 shed;
# SF: minutes-scale OOM budget + waiters share a failed load's outcome.
# ---------------------------------------------------------------------------
def test_budget_default_is_minutes_scale(monkeypatch):
    """RED-PROOF (r2/SF): the shipped OOM budget must cover a minutes-scale
    external VRAM holder — Ollama's keep-alive alone is 5 min, so retries ×
    backoff ≥ 360 s (the old default was 6 × 10 s = 60 s)."""
    monkeypatch.delenv("CODE_EMBED_LOAD_OOM_RETRIES", raising=False)
    monkeypatch.delenv("CODE_EMBED_LOAD_OOM_BACKOFF_SECS", raising=False)
    budget = es._resolve_load_oom_retries() * es._resolve_load_oom_backoff_secs()
    assert budget >= 360.0, f"OOM budget {budget}s is not minutes-scale"


def test_queue_wait_env_parsing(monkeypatch):
    """RED-PROOF (r2/B1): the queue-bound knob did not exist pre-fix."""
    monkeypatch.delenv("CODE_EMBED_QUEUE_WAIT_SECS", raising=False)
    assert es._resolve_queue_wait_secs() == 600.0  # generous default
    monkeypatch.setenv("CODE_EMBED_QUEUE_WAIT_SECS", "90")
    assert es._resolve_queue_wait_secs() == 90.0
    monkeypatch.setenv("CODE_EMBED_QUEUE_WAIT_SECS", "bad")
    assert es._resolve_queue_wait_secs() == 600.0
    monkeypatch.setenv("CODE_EMBED_QUEUE_WAIT_SECS", "-5")
    assert es._resolve_queue_wait_secs() == 600.0
    monkeypatch.setenv("CODE_EMBED_QUEUE_WAIT_SECS", "0")
    assert es._resolve_queue_wait_secs() == 0.0  # explicit no-queue is valid


def _queue_env():
    es.MAX_CONCURRENT = 1
    es.QUEUE_WAIT_SECS = 30.0
    es._request_semaphore = None  # rebuild at the new MAX_CONCURRENT
    es._inference_lock = None
    es._in_flight = 0


def test_request_beyond_capacity_queues_and_succeeds_when_slot_frees():
    """RED-PROOF (r2/B1 — the owner rule): with the single slot occupied, the
    next request WAITS (queues) and succeeds when the slot frees. The pre-fix
    C-9 code 503'd it on arrival (`_should_shed`) — a dropped request."""
    _queue_env()

    def _slow_embed(*_a, **_k):
        time.sleep(0.4)
        return [[1.0, 2.0]]

    async def _run():
        with patch.object(es, "embed", side_effect=_slow_embed):
            task_a = asyncio.create_task(
                es.embed_endpoint(es.EmbedRequest(texts=["a"]))
            )
            for _ in range(200):  # wait until A is inside the endpoint
                await asyncio.sleep(0.005)
                if es._in_flight >= 1:
                    break
            t0 = time.monotonic()
            resp_b = await es.embed_endpoint(es.EmbedRequest(texts=["b"]))
            waited = time.monotonic() - t0
            resp_a = await task_a
        return resp_a, resp_b, waited

    resp_a, resp_b, waited = asyncio.run(_run())
    assert resp_a.embeddings == [[1.0, 2.0]]
    assert resp_b.embeddings == [[1.0, 2.0]], (
        "the queued request must SUCCEED when the slot frees, never be dropped"
    )
    assert waited >= 0.2, "B must have WAITED for A's slot, not bypassed it"
    assert es._in_flight == 0


def test_queue_wait_bound_exceeded_returns_503_with_retry_after():
    """RED-PROOF (r2/B1): when no slot frees within ``QUEUE_WAIT_SECS``, the
    request is refused with a 503 that CARRIES Retry-After — and only after
    actually waiting the bound (the pre-fix capacity 503 was immediate and had
    no header)."""
    _queue_env()
    es.QUEUE_WAIT_SECS = 0.05

    async def _run():
        sem = es._get_semaphore()
        await sem.acquire()  # occupy the only slot indefinitely
        try:
            t0 = time.monotonic()
            with pytest.raises(HTTPException) as exc_info:
                await es.embed_endpoint(es.EmbedRequest(texts=["x"]))
            return exc_info.value, time.monotonic() - t0
        finally:
            sem.release()

    exc, waited = asyncio.run(_run())
    assert exc.status_code == 503
    assert "capacity" in exc.detail.lower()
    headers = exc.headers or {}
    assert "Retry-After" in headers, "a queue-bound 503 must carry Retry-After"
    assert int(headers["Retry-After"]) >= 1
    assert waited >= 0.04, "must queue for the bound before refusing"


def test_queue_wait_zero_refuses_when_saturated_but_serves_a_free_slot():
    """``CODE_EMBED_QUEUE_WAIT_SECS=0`` keeps the old shed semantics for a
    saturated service (immediate 503 — but now WITH Retry-After), while a
    request that finds a free slot is still served.

    v0.2.103 de-flake: the serve half used to rely on an UNCONTENDED
    ``sem.acquire()`` beating the shipped 1 ms zero-grace timer
    (``_QUEUE_ZERO_GRACE_SECS = 0.001``). That is a wall-clock margin
    against OS scheduling: under heavy machine load the test process can
    be descheduled between the acquire task's first scheduling and its
    completion, the monotonic grace timer matures meanwhile, and
    ``wait_for`` sheds the request with a 503 — observed once in a full
    run. The race is removed by widening ONLY the serve half's grace to
    5 s (the shed half keeps the shipped value, so "saturated + wait=0 →
    503 with Retry-After" still exercises the real 1 ms path); the grace
    magnitude is an internal tuning constant no test pins, and the
    property under test — a request that finds a free slot is SERVED, not
    shed — is unchanged."""
    _queue_env()
    es.QUEUE_WAIT_SECS = 0.0

    async def _run():
        sem = es._get_semaphore()
        await sem.acquire()
        try:
            with pytest.raises(HTTPException) as exc_info:
                await es.embed_endpoint(es.EmbedRequest(texts=["x"]))
            assert exc_info.value.status_code == 503
            assert "Retry-After" in (exc_info.value.headers or {})
        finally:
            sem.release()
        real_grace = es._QUEUE_ZERO_GRACE_SECS
        es._QUEUE_ZERO_GRACE_SECS = 5.0
        try:
            with patch.object(es, "embed", return_value=[[1.0]]):
                return await es.embed_endpoint(es.EmbedRequest(texts=["x"]))
        finally:
            es._QUEUE_ZERO_GRACE_SECS = real_grace

    resp = asyncio.run(_run())
    assert resp.embeddings == [[1.0]]


def test_queue_timeout_race_does_not_leak_a_permit(monkeypatch):
    """RED-PROOF (r3, re-review SHOULD-FIX): the Python 3.11 ``wait_for`` race —
    the semaphore acquire consumes a permit in the same event-loop step in
    which the timeout fires, and ``wait_for`` raises TimeoutError anyway. The
    pre-fix code (a bare ``sem.acquire()`` coroutine handed to ``wait_for``)
    leaked that permit: effective capacity decayed 4 → 3 → … under exactly the
    sustained saturation this queue exists for, silently, restart-only
    recovery. Simulated deterministically by a fake ``wait_for`` that lets the
    acquire COMPLETE and then raises TimeoutError regardless — then asserting
    the endpoint returns the queue-bound 503 AND the permit is back (capacity
    is exactly MAX_CONCURRENT: no leak, no over-release)."""
    _queue_env()
    es.QUEUE_WAIT_SECS = 5.0
    real_wait_for = asyncio.wait_for

    async def _racy_wait_for(awaitable, timeout, **kwargs):
        # The exact 3.11 race shape: the acquire completes (permit consumed)
        # AND the timeout fires anyway.
        task = asyncio.ensure_future(awaitable)
        await asyncio.shield(task)
        raise asyncio.TimeoutError()

    async def _run():
        sem = es._get_semaphore()
        with patch.object(es, "embed", return_value=[[1.0]]):
            with pytest.raises(HTTPException) as exc_info:
                await es.embed_endpoint(es.EmbedRequest(texts=["x"]))
        assert exc_info.value.status_code == 503
        # Capacity check with the REAL wait_for: exactly MAX_CONCURRENT (=1
        # here) permit must be available — the raced permit was released.
        await real_wait_for(sem.acquire(), 0.5)  # first acquire: must succeed
        with pytest.raises(asyncio.TimeoutError):
            # second acquire: must NOT succeed (capacity is 1, not 2 — no
            # over-release — and not 0 — no leak)
            await real_wait_for(sem.acquire(), 0.1)
        sem.release()

    monkeypatch.setattr(asyncio, "wait_for", _racy_wait_for)
    try:
        asyncio.run(_run())
    finally:
        assert es._in_flight == 0


def test_queue_retry_after_has_a_sane_floor():
    """RED-PROOF (r3, review nit): no-queue mode (``QUEUE_WAIT_SECS=0``) used to
    hint ``Retry-After: 1`` — an honouring client re-probed at ~1 Hz for up to
    its whole 503 tolerance. The floor is 5 s (one inference takes seconds)."""
    for wait in (0.0, 1.0, 4.0):
        es.QUEUE_WAIT_SECS = wait
        assert es._queue_retry_after_secs() >= 5, (
            f"QUEUE_WAIT_SECS={wait}: Retry-After must be ≥ 5 s, got "
            f"{es._queue_retry_after_secs()}"
        )
    es.QUEUE_WAIT_SECS = 120.0
    assert es._queue_retry_after_secs() == 30, "the 30 s cap still holds"


def test_failed_load_cooldown_is_shared_by_waiters():
    """RED-PROOF (r2/SF): after a load exhausts its OOM budget, the NEXT queued
    loader shares the failed outcome (fail-fast cooldown → the same
    ``_ModelLoadUnavailable``, zero extra constructs). Pre-fix, every waiter
    re-ran its own full budget serially (N × minutes)."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    _FakeST.always_oom = True
    _gpu_env()
    es.LOAD_OOM_RETRIES = 1  # 2 attempts
    es.LOAD_OOM_BACKOFF_SECS = 0.0  # retry_after → 1 s cooldown

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with pytest.raises(es._ModelLoadUnavailable):
            es._load_gpu_model()
        first_round_calls = len(_FakeST.calls_log)
        assert first_round_calls == 2

        t0 = time.monotonic()
        with pytest.raises(es._ModelLoadUnavailable) as exc_info:
            es._load_gpu_model()  # the queued waiter
        elapsed = time.monotonic() - t0

    assert len(_FakeST.calls_log) == first_round_calls, (
        "the waiter must share the failed outcome — no second full budget"
    )
    assert elapsed < 0.5, "the shared failure must be fast, not minutes"
    assert (exc_info.value.retry_after or 0) >= 1
    assert es._load_unavailable_until > 0.0


def test_cooldown_expiry_allows_a_fresh_attempt():
    """The cooldown is a fail-fast window, not a latch: once it expires (or is
    cleared), a load retries for real and a success resets the state."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)
    st_mod = _fake_st_module()
    _FakeST.always_oom = True
    _gpu_env()
    es.LOAD_OOM_RETRIES = 1
    es.LOAD_OOM_BACKOFF_SECS = 0.0

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with pytest.raises(es._ModelLoadUnavailable):
            es._load_gpu_model()
        # Simulate the cooldown having elapsed.
        es._load_unavailable_until = time.monotonic() - 1.0
        _FakeST.always_oom = False
        model = es._load_gpu_model()

    assert model is not None
    assert es._st_model is model
    assert es._last_load_error is None
    assert es._load_unavailable_until == 0.0, "a success clears the cooldown"


def test_health_reports_queue_wait():
    """RED-PROOF (r2/B1): /health exposes the queue bound so a client can
    compare its own request timeout against it (it MUST exceed it)."""
    es.BACKEND = "gpu"
    es._st_model = None
    es._st_model_dim = 2048
    es._in_flight = 0
    es.QUEUE_WAIT_SECS = 600.0

    payload = asyncio.run(es.health())
    assert payload["queue_wait_secs"] == 600.0


def test_compose_plumbs_the_new_knobs_with_server_defaults(monkeypatch):
    """RED-PROOF (r2/delivery): the container is the primary deployment, so the
    knobs must be reachable through ``infrastructure/docker-compose.yml`` as
    ``${VAR:-default}`` — and the compose defaults MUST MATCH the server's
    resolver defaults (drift would silently re-hardcode the container)."""
    import re

    import yaml

    for var in (
        "CODE_EMBED_IDLE_UNLOAD_SECS", "CODE_EMBED_LOAD_OOM_RETRIES",
        "CODE_EMBED_LOAD_OOM_BACKOFF_SECS", "CODE_EMBED_UNLOAD_FREED_MIN_MB",
        "CODE_EMBED_QUEUE_WAIT_SECS",
    ):
        monkeypatch.delenv(var, raising=False)

    compose_path = _REPO_ROOT / "infrastructure" / "docker-compose.yml"
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    env = doc["services"]["code_embed"]["environment"]

    server_defaults = {
        "CODE_EMBED_IDLE_UNLOAD_SECS": es._resolve_idle_unload_secs(),
        "CODE_EMBED_LOAD_OOM_RETRIES": float(es._resolve_load_oom_retries()),
        "CODE_EMBED_LOAD_OOM_BACKOFF_SECS": es._resolve_load_oom_backoff_secs(),
        "CODE_EMBED_UNLOAD_FREED_MIN_MB": es._resolve_unload_freed_min_mb(),
        "CODE_EMBED_QUEUE_WAIT_SECS": es._resolve_queue_wait_secs(),
    }
    for var, default in server_defaults.items():
        assert var in env, f"{var} must be plumbed into code_embed.environment"
        match = re.fullmatch(r"\$\{" + re.escape(var) + r":-([^}]*)\}", str(env[var]))
        assert match, f"{var} must use the ${{VAR:-default}} form, got {env[var]!r}"
        assert float(match.group(1)) == default, (
            f"compose default for {var} ({match.group(1)}) drifted from the "
            f"server resolver default ({default})"
        )


def test_non_oom_load_error_propagates_without_retry():
    """A NON-OOM construct fault (a config/import error) is not retried — it
    propagates immediately and is recorded on ``_last_load_error``."""
    registry = {"allocated": 0}
    fake_torch = _FakeTorch(registry)

    class _BoomST:
        calls = 0

        def __init__(self, name, **kwargs):
            _BoomST.calls += 1
            raise ValueError("trust_remote_code is required for this model")

    st_mod: Any = types.ModuleType("sentence_transformers")
    st_mod.SentenceTransformer = _BoomST
    _gpu_env()
    es.LOAD_OOM_RETRIES = 5

    with patch.dict(sys.modules, {"torch": fake_torch, "sentence_transformers": st_mod}):
        with pytest.raises(ValueError):
            es._load_gpu_model()

    assert _BoomST.calls == 1, "a non-OOM fault must NOT be retried"
    assert es._last_load_error is not None and "trust_remote_code" in es._last_load_error


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
