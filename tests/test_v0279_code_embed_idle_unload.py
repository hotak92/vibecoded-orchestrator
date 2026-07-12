# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.79 §C — code-embed idle unload of the ~7.25 GB CodeSage weights.

The code-embed FastAPI service (``claude_mcp_servers/code_embedding_service/
server.py``) loads CodeSage-Large-v2 into the module-global ``_st_model`` and
held it for the whole process lifetime. §C adds an idle-unload timer: after
``CODE_EMBED_IDLE_UNLOAD_SECS`` of no ``/embed`` traffic the weights are freed
(VRAM returned to the driver) and the next request lazy-reloads them.

These tests exercise the LIFECYCLE LOGIC — lock acquisition, the double-checked
idle condition, ``_st_model`` being None-d, the reload trigger, the env knob,
and the cpu/mps (no-cuda) soft-fail — WITHOUT a GPU or the real 7 GB model.
``SentenceTransformer`` load + ``model.encode`` are mocked; ``torch.cuda`` is
absent in CI so the guarded ``empty_cache`` naturally exercises the no-op path.

Covers the review's required cases (v0279-plan-review §C):
  * unload → lazy-reload returns correct embeddings
  * idle-timer fires after the (tiny) idle window — deterministic, no sleep race
  * encode-in-flight → unload BLOCKS until idle (lock held; assert no unload)
  * knob = 0 → never unloads
  * cpu/mps backend (no torch.cuda) → guarded empty_cache is a safe no-op
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claude_mcp_servers.code_embedding_service.server as es  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_service_state():
    """Restore every module global we mutate so tests don't leak into each
    other (they share the imported module object)."""
    saved = {
        "BACKEND": es.BACKEND,
        "IDLE_UNLOAD_SECS": es.IDLE_UNLOAD_SECS,
        "_st_model": es._st_model,
        "_st_model_dim": es._st_model_dim,
        "_in_flight": es._in_flight,
        "_last_used": es._last_used,
        "_inference_lock": es._inference_lock,
    }
    # A clean lock per test — otherwise a lock acquired-and-abandoned by a
    # crashed test would poison the next one.
    es._inference_lock = None
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(es, k, v)


# ---------------------------------------------------------------------------
# _resolve_idle_unload_secs — env parsing
# ---------------------------------------------------------------------------


def test_idle_secs_default_300(monkeypatch):
    monkeypatch.delenv("CODE_EMBED_IDLE_UNLOAD_SECS", raising=False)
    assert es._resolve_idle_unload_secs() == 300.0


def test_idle_secs_zero_means_never(monkeypatch):
    monkeypatch.setenv("CODE_EMBED_IDLE_UNLOAD_SECS", "0")
    assert es._resolve_idle_unload_secs() == 0.0


def test_idle_secs_custom(monkeypatch):
    monkeypatch.setenv("CODE_EMBED_IDLE_UNLOAD_SECS", "42")
    assert es._resolve_idle_unload_secs() == 42.0


def test_idle_secs_bad_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CODE_EMBED_IDLE_UNLOAD_SECS", "notanumber")
    assert es._resolve_idle_unload_secs() == 300.0


def test_idle_secs_negative_falls_back_to_default(monkeypatch):
    # Negative would mean "always idle" — coerce to the safe default instead.
    monkeypatch.setenv("CODE_EMBED_IDLE_UNLOAD_SECS", "-5")
    assert es._resolve_idle_unload_secs() == 300.0


# ---------------------------------------------------------------------------
# _maybe_unload_idle_model — the core decision
# ---------------------------------------------------------------------------


def test_unload_fires_after_idle_window():
    """LOADED model + idle past the window + nothing in flight → unloaded."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    es._st_model = MagicMock(name="loaded_model")
    es._in_flight = 0
    # last_used well in the past relative to the injected clock.
    es._last_used = 1000.0
    now = 1000.0 + 11.0  # 11s idle > 10s window

    unloaded = asyncio.run(es._maybe_unload_idle_model(now=now))

    assert unloaded is True
    assert es._st_model is None


def test_no_unload_before_idle_window():
    """LEAVE-ALONE: not yet idle long enough → model stays loaded."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 0
    es._last_used = 1000.0
    now = 1000.0 + 5.0  # only 5s idle < 10s window

    unloaded = asyncio.run(es._maybe_unload_idle_model(now=now))

    assert unloaded is False
    assert es._st_model is model  # untouched


def test_no_unload_when_model_already_none():
    """LEAVE-ALONE: already unloaded → nothing to do, no crash."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    es._st_model = None
    es._in_flight = 0
    es._last_used = 0.0

    unloaded = asyncio.run(es._maybe_unload_idle_model(now=1_000_000.0))

    assert unloaded is False
    assert es._st_model is None


def test_knob_zero_never_unloads():
    """CODE_EMBED_IDLE_UNLOAD_SECS=0 → the model is NEVER unloaded, even when
    idle for an eternity."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 0.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 0
    es._last_used = 0.0

    unloaded = asyncio.run(es._maybe_unload_idle_model(now=1_000_000.0))

    assert unloaded is False
    assert es._st_model is model


def test_ollama_backend_never_unloads():
    """The ollama backend holds no in-process weights → unload is a no-op."""
    es.BACKEND = "ollama"
    es.IDLE_UNLOAD_SECS = 10.0
    model = MagicMock(name="not-really-a-model")
    es._st_model = model
    es._in_flight = 0
    es._last_used = 0.0

    unloaded = asyncio.run(es._maybe_unload_idle_model(now=1_000_000.0))

    assert unloaded is False
    assert es._st_model is model


def test_no_unload_while_request_in_flight():
    """CRITICAL (review C.3): even past the idle window, an in-flight request
    (``_in_flight > 0``) must BLOCK the unload — the pre-check catches this."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 1  # a request is running
    es._last_used = 1000.0
    now = 1000.0 + 1000.0  # very idle by the clock, but a request is live

    unloaded = asyncio.run(es._maybe_unload_idle_model(now=now))

    assert unloaded is False
    assert es._st_model is model


# ---------------------------------------------------------------------------
# CRITICAL (review C.3): the lock-guarded unload BLOCKS while an encode holds
# the inference lock, and re-checks the condition UNDER the lock.
# ---------------------------------------------------------------------------


def test_unload_blocks_until_inference_lock_released():
    """Drive a task that holds ``_inference_lock`` (standing in for an in-flight
    ``run_in_executor(encode)``) and prove the idle-unload cannot free the model
    until that lock is released. Without the lock, ``empty_cache`` could free
    buffers a live encode still uses → CUDA fault."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 0  # pre-check passes; the lock is the real gate here
    es._last_used = 1000.0
    now = 1000.0 + 100.0  # well past the window

    async def _run():
        lock = es._get_lock()
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def _hold_lock():
            async with lock:
                holder_entered.set()
                # Model is still loaded while we hold the lock.
                await release_holder.wait()

        holder = asyncio.create_task(_hold_lock())
        await holder_entered.wait()

        # Kick off the unload; it must not proceed while the holder has the lock.
        unload_task = asyncio.create_task(es._maybe_unload_idle_model(now=now))
        # Give the unloader ample opportunity to (wrongly) proceed.
        for _ in range(20):
            await asyncio.sleep(0)
        assert not unload_task.done(), "unload proceeded while lock was held!"
        assert es._st_model is model, "model was freed while a lock-holder was live!"

        # Release the holder → the unloader can now acquire the lock and unload.
        release_holder.set()
        await holder
        result = await unload_task
        return result

    unloaded = asyncio.run(_run())
    assert unloaded is True
    assert es._st_model is None


def test_double_checked_condition_bails_if_request_arrives_during_contention():
    """If a request arrives (``_in_flight`` bumps) while the unloader waits on
    the lock, the UNDER-LOCK re-check must abort the unload — double-checked
    locking. Simulate by holding the lock, letting the unloader queue, then
    setting ``_in_flight`` before releasing."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 0
    es._last_used = 1000.0
    now = 1000.0 + 100.0

    async def _run():
        lock = es._get_lock()
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def _hold_lock():
            async with lock:
                holder_entered.set()
                await release_holder.wait()

        holder = asyncio.create_task(_hold_lock())
        await holder_entered.wait()

        unload_task = asyncio.create_task(es._maybe_unload_idle_model(now=now))
        for _ in range(10):
            await asyncio.sleep(0)
        # A request lands while the unloader is blocked on the lock.
        es._in_flight = 1
        release_holder.set()
        await holder
        result = await unload_task
        return result

    unloaded = asyncio.run(_run())
    assert unloaded is False, "unload should abort — a request arrived under lock"
    assert es._st_model is model


# ---------------------------------------------------------------------------
# cpu/mps backend (no torch.cuda) — the guarded empty_cache is a safe no-op
# ---------------------------------------------------------------------------


def test_empty_cuda_cache_no_torch_is_noop():
    """A host without torch installed must not crash — the guard swallows the
    ImportError and the call is a no-op."""
    with patch.dict(sys.modules, {"torch": None}):  # import torch → ImportError
        es._empty_cuda_cache()  # must not raise
        assert es._cuda_memory_allocated() is None


def test_empty_cuda_cache_cpu_backend_is_noop():
    """torch present but CUDA unavailable (cpu/mps host) → empty_cache is
    skipped, no crash."""
    fake_torch = MagicMock(name="torch")
    fake_torch.cuda.is_available.return_value = False
    with patch.dict(sys.modules, {"torch": fake_torch}):
        es._empty_cuda_cache()  # must not raise
        fake_torch.cuda.empty_cache.assert_not_called()
        assert es._cuda_memory_allocated() is None


def test_unload_on_cpu_backend_does_not_crash():
    """Full unload path on a cpu-backed host (torch present, no CUDA): the model
    is freed and no CUDA op is attempted."""
    es.BACKEND = "gpu"  # 'gpu' backend can still resolve to a cpu device
    es.IDLE_UNLOAD_SECS = 10.0
    es._st_model = MagicMock(name="cpu_model")
    es._in_flight = 0
    es._last_used = 1000.0

    fake_torch = MagicMock(name="torch")
    fake_torch.cuda.is_available.return_value = False
    with patch.dict(sys.modules, {"torch": fake_torch}):
        unloaded = asyncio.run(es._maybe_unload_idle_model(now=1000.0 + 11.0))

    assert unloaded is True
    assert es._st_model is None
    fake_torch.cuda.empty_cache.assert_not_called()


# ---------------------------------------------------------------------------
# Lazy reload after unload — the whole point: retrieval still works.
# ---------------------------------------------------------------------------


def test_lazy_reload_after_unload_returns_correct_embeddings():
    """Unload the model, then hit the real ``_embed_gpu`` path (which calls the
    lazy ``_load_gpu_model``) and assert it re-materialises the model and
    returns the expected vectors. No feature removed — retrieval is transparent
    across the unload."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0

    # A fake SentenceTransformer whose .encode returns a known vector.
    import numpy as np

    class _FakeEncoded:
        def __init__(self, arr):
            self._arr = arr

        def tolist(self):
            return self._arr

    fake_model = MagicMock(name="reloaded_model")
    fake_model.encode.return_value = _FakeEncoded([[0.11, 0.22, 0.33]])
    fake_model.get_sentence_embedding_dimension.return_value = 3

    load_calls = {"n": 0}

    def _fake_load():
        # Mirror the real lazy guard: only "load" when unloaded.
        if es._st_model is not None:
            return es._st_model
        load_calls["n"] += 1
        es._st_model = fake_model
        es._st_model_dim = 3
        return es._st_model

    # Start loaded.
    es._st_model = fake_model
    es._st_model_dim = 3
    es._in_flight = 0
    es._last_used = 1000.0

    # 1) Unload it.
    assert asyncio.run(es._maybe_unload_idle_model(now=1000.0 + 11.0)) is True
    assert es._st_model is None

    # 2) Next embed call lazy-reloads and returns the right vector.
    with patch.object(es, "_load_gpu_model", side_effect=_fake_load):
        vecs = es._embed_gpu(["def foo(): pass"], is_query=False)

    assert vecs == [[0.11, 0.22, 0.33]]
    assert es._st_model is fake_model
    assert load_calls["n"] == 1  # exactly one reload happened

    _ = np  # keep the numpy import meaningful even if the fake bypasses it


# ---------------------------------------------------------------------------
# _stamp_used — activity resets the idle clock (integration with the endpoints)
# ---------------------------------------------------------------------------


def test_stamp_used_updates_last_used():
    es._last_used = 0.0
    es._stamp_used()
    assert es._last_used > 0.0  # monotonic clock is > 0 after process start


def test_embed_endpoint_stamps_activity_and_prevents_unload():
    """An /embed call stamps ``_last_used`` (both on entry and completion), so a
    unload check run immediately after sees a fresh timestamp and leaves the
    model loaded."""
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    model = MagicMock(name="loaded_model")
    es._st_model = model
    es._in_flight = 0
    es._last_used = 0.0  # ancient

    # Mock the actual inference so no model runs.
    with patch.object(es, "embed", return_value=[[1.0, 2.0]]):
        resp = asyncio.run(es.embed_endpoint(es.EmbedRequest(texts=["x"])))
    assert resp.embeddings == [[1.0, 2.0]]

    # last_used is now ~monotonic-now; an idle check just after must NOT unload.
    import time as _t
    unloaded = asyncio.run(es._maybe_unload_idle_model(now=_t.monotonic()))
    assert unloaded is False
    assert es._st_model is model


# ---------------------------------------------------------------------------
# /health must NOT reload an idle-unloaded model — otherwise every liveness
# probe would re-materialise the 7.25 GB weights and the model would never
# stay unloaded (defeats §C).
# ---------------------------------------------------------------------------


def test_health_does_not_reload_unloaded_model():
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    es._st_model = None            # unloaded
    es._st_model_dim = 2048        # dim cached from a prior load
    es._in_flight = 0

    # If /health called get_dim() it would invoke _load_gpu_model and reload.
    def _boom():
        raise AssertionError("/health must not reload the model via get_dim()")

    with patch.object(es, "get_dim", side_effect=_boom):
        payload = asyncio.run(es.health())

    assert payload["status"] == "ok"
    assert payload["dim"] == 2048          # served from the cache
    assert payload["model_loaded"] is False
    assert payload["idle_unload_secs"] == 10.0
    assert es._st_model is None            # still unloaded — probe left it alone


def test_health_reports_model_loaded_true_when_resident():
    es.BACKEND = "gpu"
    es.IDLE_UNLOAD_SECS = 10.0
    es._st_model = MagicMock(name="loaded")
    es._st_model_dim = 2048
    es._in_flight = 0

    payload = asyncio.run(es.health())
    assert payload["model_loaded"] is True
    assert payload["dim"] == 2048


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
