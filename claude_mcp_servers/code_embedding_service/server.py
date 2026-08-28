# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Code Embedding Service — FastAPI server for code embeddings.

Supports two backends:
  1. "gpu" (default): sentence-transformers with CodeSage-Large-v2 on GPU/CPU
  2. "ollama": delegate to Ollama API (any model, e.g. jina, nomic, qwen3-embedding)

Environment variables:
  CODE_EMBED_BACKEND      "gpu" | "ollama"  (default: "gpu")
  CODE_EMBED_MODEL        HuggingFace model ID or Ollama model name
                          gpu default: "codesage/codesage-large-v2"
                          ollama default: "unclemusclez/jina-embeddings-v2-base-code:latest"
  CODE_EMBED_DEVICE       "cuda" | "mps" | "cpu" | "auto"  (default: "auto", gpu backend only)
                          "auto" probes CUDA, then Apple Metal (MPS), then CPU.
  CODE_EMBED_DTYPE        "float32" | "bfloat16" | "float16"  (default: "bfloat16", gpu backend only)
  CODE_EMBED_PORT         Server port (default: 11440)
  CODE_EMBED_BATCH_SIZE   Max batch size (default: 32)
  CODE_EMBED_MAX_CONCURRENT  Max in-flight requests before shedding with 503 (default: 4)
  CODE_EMBED_IDLE_UNLOAD_SECS  Idle seconds before the GPU model (~7.25 GB CodeSage
                          weights) is unloaded to free VRAM; the next request
                          transparently lazy-reloads it. 0 = never unload
                          (keep resident for the process lifetime). Default: 300.
                          gpu backend only — the ollama backend holds no in-process weights.
  OLLAMA_URL              Ollama API URL (default: http://localhost:11435)
  CODE_EMBED_INSTRUCTION  Query instruction prefix (default: "" — CodeSage needs none)
  CODE_EMBED_MAX_SEQ_LEN  Max sequence length (default: model default)
  CODE_EMBED_TRUST_REMOTE "true" | "false" (default: "true")

Usage:
  # GPU mode (default):
  python -m claude_mcp_servers.code_embedding_service.server

  # Ollama mode:
  CODE_EMBED_BACKEND=ollama CODE_EMBED_MODEL=qwen3-embedding:0.6b python -m ...

API:
  POST /embed  {"texts": [...], "is_query": false}  → {"embeddings": [[...], ...], "dim": N}
  GET  /health  → {"status": "ok", "backend": "...", "model": "...", "dim": N}
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("code_embedding_service")
# v0.2.91 (Decision #21): honors the global VCO_LOG_LEVEL pref via the
# shared vco_lib helper instead of a hardcoded INFO level. Bare import —
# vco_lib is a SHIPPED, editable-installed part of every healthy install,
# so a failed import here already fails loudly (ImportError), matching the
# "no silent-fallback on vco_lib imports" discipline used elsewhere.
from vco_lib.log_setup import configure_logging
configure_logging(format="%(asctime)s %(name)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKEND = os.getenv("CODE_EMBED_BACKEND", "gpu").lower()
DEVICE = os.getenv("CODE_EMBED_DEVICE", "auto")
DTYPE = os.getenv("CODE_EMBED_DTYPE", "bfloat16")
PORT = int(os.getenv("CODE_EMBED_PORT", "11440"))
BATCH_SIZE = int(os.getenv("CODE_EMBED_BATCH_SIZE", "32"))
MAX_CONCURRENT = int(os.getenv("CODE_EMBED_MAX_CONCURRENT", "4"))  # max in-flight requests before shed


def _resolve_idle_unload_secs() -> float:
    """Idle seconds before the GPU model is unloaded. 0 = never; a bad/negative
    value coerces to the 300 s default (conservative: never a shorter-than-asked
    window that would thrash the reload). Read once at import — the timer honours
    it for the process lifetime."""
    raw = os.getenv("CODE_EMBED_IDLE_UNLOAD_SECS", "300")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 300.0
    if val < 0:
        return 300.0
    return val


IDLE_UNLOAD_SECS = _resolve_idle_unload_secs()  # 0 = never unload
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
INSTRUCTION = os.getenv("CODE_EMBED_INSTRUCTION", "")
MAX_SEQ_LEN = os.getenv("CODE_EMBED_MAX_SEQ_LEN", "")
TRUST_REMOTE = os.getenv("CODE_EMBED_TRUST_REMOTE", "true").lower() == "true"

GPU_DEFAULT_MODEL = "codesage/codesage-large-v2"
OLLAMA_DEFAULT_MODEL = "unclemusclez/jina-embeddings-v2-base-code:latest"

if BACKEND == "gpu":
    MODEL_NAME = os.getenv("CODE_EMBED_MODEL", GPU_DEFAULT_MODEL)
elif BACKEND == "ollama":
    MODEL_NAME = os.getenv("CODE_EMBED_MODEL", OLLAMA_DEFAULT_MODEL)
else:
    logger.error("CODE_EMBED_BACKEND must be 'gpu' or 'ollama', got '%s'", BACKEND)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Backend: GPU (sentence-transformers)
# ---------------------------------------------------------------------------
_st_model = None  # lazy-loaded
# v0.2.79 §C: cache the embedding dimension the first time the model loads. It
# is a fixed model property, so ``/health`` can report ``dim`` WITHOUT forcing a
# reload of an idle-unloaded model — otherwise every liveness probe would
# immediately re-materialise the 7.25 GB weights and the model would never stay
# unloaded. Survives unload (``_st_model = None`` does not clear this).
_st_model_dim: int | None = None


def _load_gpu_model():
    global _st_model
    if _st_model is not None:
        return _st_model

    from sentence_transformers import SentenceTransformer

    device = DEVICE
    if device == "auto":
        import torch
        if torch.cuda.is_available():
            device = "cuda"
        elif (
            getattr(torch.backends, "mps", None) is not None
            and torch.backends.mps.is_available()
        ):
            # v0.2.54 (gpu-audit C-7): Apple Metal. Stock torch wheels
            # have shipped MPS for years and sentence-transformers
            # accepts device="mps" — the previous auto-probe only
            # checked CUDA, so Apple Silicon hosts running this service
            # natively (outside a container) silently landed on CPU.
            device = "mps"
        else:
            device = "cpu"

    dtype_map = {
        "float32": "float32",
        "bfloat16": "bfloat16",
        "float16": "float16",
    }
    torch_dtype = dtype_map.get(DTYPE, "bfloat16")

    logger.info("Loading model '%s' on device='%s' dtype='%s'...", MODEL_NAME, device, torch_dtype)
    t0 = time.time()

    kwargs = {
        "trust_remote_code": TRUST_REMOTE,
        "device": device,
        "model_kwargs": {"torch_dtype": torch_dtype},
    }
    if MAX_SEQ_LEN:
        kwargs["model_kwargs"]["max_seq_length"] = int(MAX_SEQ_LEN)

    _st_model = SentenceTransformer(MODEL_NAME, **kwargs)
    dim = _st_model.get_sentence_embedding_dimension()
    global _st_model_dim
    _st_model_dim = dim  # cache for /health so idle-unload survives liveness probes
    logger.info("Model loaded in %.1fs — dim=%d, device=%s", time.time() - t0, dim, device)
    return _st_model


def _embed_gpu(texts: list[str], is_query: bool = False) -> list[list[float]]:
    model = _load_gpu_model()
    prompt = INSTRUCTION if is_query and INSTRUCTION else None
    embeddings = model.encode(
        texts,
        prompt=prompt,
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
        normalize_embeddings=True,
    )
    return embeddings.tolist()


def _dim_gpu() -> int:
    model = _load_gpu_model()
    return model.get_sentence_embedding_dimension()


# ---------------------------------------------------------------------------
# Backend: Ollama
# ---------------------------------------------------------------------------
def _embed_ollama(texts: list[str], is_query: bool = False) -> list[list[float]]:
    import requests

    results = []
    for text in texts:
        if is_query and INSTRUCTION:
            text = INSTRUCTION + text
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": MODEL_NAME, "prompt": text},
            timeout=30,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Ollama error: {resp.text}")
        results.append(resp.json()["embedding"])
    return results


_ollama_dim_cache: int | None = None


def _dim_ollama() -> int:
    global _ollama_dim_cache
    if _ollama_dim_cache is not None:
        return _ollama_dim_cache
    # Probe with a short text
    vecs = _embed_ollama(["dim probe"], is_query=False)
    _ollama_dim_cache = len(vecs[0])
    return _ollama_dim_cache


# ---------------------------------------------------------------------------
# Unified interface
# ---------------------------------------------------------------------------
def embed(texts: list[str], is_query: bool = False) -> list[list[float]]:
    if BACKEND == "gpu":
        return _embed_gpu(texts, is_query)
    return _embed_ollama(texts, is_query)


def get_dim() -> int:
    if BACKEND == "gpu":
        return _dim_gpu()
    return _dim_ollama()


# ---------------------------------------------------------------------------
# Concurrency control
# ---------------------------------------------------------------------------
# Lock serializes GPU inference (model.encode is not thread-safe on CUDA).
# Semaphore limits concurrent in-flight requests to prevent OOM under burst.
_inference_lock: asyncio.Lock | None = None
_request_semaphore: asyncio.Semaphore | None = None
# C-9 (v0.2.75 P3b): explicit in-flight counter. Previously the code read the
# semaphore's PRIVATE ``._value`` (CPython-internal, no stability guarantee) to
# derive "how many are running". An explicit counter incremented/decremented in
# a try/finally around the semaphore hold is honest, public, and identical
# across both endpoints — so ``/embed`` and ``/api/embeddings`` shed on the SAME
# condition (pre-fix ``/api/embeddings`` never shed, so one burst diverged the
# two endpoints' behaviour) and ``/health`` reports the real number.
_in_flight: int = 0


def _get_lock() -> asyncio.Lock:
    global _inference_lock
    if _inference_lock is None:
        _inference_lock = asyncio.Lock()
    return _inference_lock


def _get_semaphore() -> asyncio.Semaphore:
    global _request_semaphore
    if _request_semaphore is None:
        _request_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _request_semaphore


def _should_shed() -> bool:
    """True when the service is already at capacity (``MAX_CONCURRENT`` requests
    in flight) — a new request would only queue, so shed it with a 503. ONE home
    for the load-shed decision, shared by BOTH embed endpoints so they behave
    identically under burst."""
    return _in_flight >= MAX_CONCURRENT


# ---------------------------------------------------------------------------
# Idle model unload (v0.2.79 §C) — release the ~7.25 GB CodeSage VRAM when idle
# ---------------------------------------------------------------------------
# ``_st_model`` holds the CodeSage-Large-v2 weights (~7.25 GB VRAM) for the
# process lifetime. On a workstation the user shares the GPU with other tools,
# so we unload the weights after ``IDLE_UNLOAD_SECS`` of no ``/embed`` traffic
# and lazy-reload them on the next request (``_load_gpu_model`` is already lazy).
#
# ``_last_used`` is a MONOTONIC timestamp (``time.monotonic``, not wall clock —
# immune to NTP steps) stamped by both endpoints on every request. It seeds to
# "now" at import so a service that never receives a request still ages toward
# the idle deadline from startup rather than from epoch 0.
_last_used: float = time.monotonic()
_idle_task: "asyncio.Task | None" = None


def _stamp_used() -> None:
    """Record request activity for the idle-unload timer. Called by both
    endpoints. Monotonic so an NTP/clock adjustment never makes the model look
    idle (or busy) spuriously."""
    global _last_used
    _last_used = time.monotonic()


async def _maybe_unload_idle_model(now: float | None = None) -> bool:
    """Unload the GPU model if it has been idle past ``IDLE_UNLOAD_SECS``.

    Returns True iff a model was actually unloaded. Safe to call with any
    backend and whether or not a model is loaded — a no-op in every case where
    the condition isn't met.

    CRITICAL (v0.2.79 §C, review C.3): the unload MUST acquire the SAME
    ``_inference_lock`` the endpoints hold across ``run_in_executor(encode)``,
    and re-check the idle condition UNDER the lock (double-checked locking).
    Both ``/embed`` endpoints hold that lock for the whole executor await, and
    the reload path (``embed`` → ``_embed_gpu`` → ``_load_gpu_model``) also runs
    inside it. Without the lock, ``torch.cuda.empty_cache()`` could free buffers
    a concurrent executor-thread ``encode`` still references → CUDA fault /
    corrupt embeddings.
    """
    global _st_model
    if BACKEND != "gpu" or IDLE_UNLOAD_SECS <= 0:
        return False
    if _st_model is None:
        return False
    # Whether the caller pinned the clock. When `now` is injected (tests, or a
    # caller that already sampled), we must NOT re-sample under the lock — the
    # idle math has to stay on the SAME clock the caller reasoned about.
    # Re-sampling would (a) make the function untestable via an injected `now`
    # and (b) on a host whose `time.monotonic()` is smaller than `_last_used`
    # (a fresh runner started after `_last_used` was stamped in a DIFFERENT
    # epoch — e.g. a test that sets `_last_used=1000.0`), spuriously read the
    # window as "not idle". The production poll loop calls with `now=None`, so
    # it still gets a fresh sample for the contention-refresh case.
    now_injected = now is not None
    if now is None:
        now = time.monotonic()
    # Cheap pre-check OUTSIDE the lock to avoid taking it every poll. The
    # authoritative re-check happens UNDER the lock below.
    if _in_flight != 0 or (now - _last_used) <= IDLE_UNLOAD_SECS:
        return False

    async with _get_lock():
        # Double-checked: re-evaluate under the lock. ``_in_flight`` and
        # ``_last_used`` can change between the pre-check and acquiring the
        # lock (a request may have arrived and incremented ``_in_flight``
        # before taking the lock itself). Recompute ``now`` ONLY when the
        # caller did not pin it, so a request that landed during lock
        # contention refreshes the idle window on the production path, while an
        # injected `now` stays authoritative.
        if not now_injected:
            now = time.monotonic()
        if _st_model is None or _in_flight != 0 or (now - _last_used) <= IDLE_UNLOAD_SECS:
            return False

        # C.2: log before/after allocated VRAM for observability. This frees the
        # WEIGHTS (~7.25 GB), not the CUDA context (a few hundred MB persists
        # per-process until exit) — an expected, documented residual.
        before = _cuda_memory_allocated()
        _st_model = None
        _empty_cuda_cache()
        after = _cuda_memory_allocated()
        if before is not None and after is not None:
            logger.info(
                "Idle-unloaded GPU model after %.0fs idle — VRAM allocated "
                "%.0f MB → %.0f MB (freed %.0f MB; CUDA context residual "
                "persists until process exit)",
                now - _last_used, before / 1e6, after / 1e6,
                max(0.0, (before - after)) / 1e6,
            )
        else:
            logger.info(
                "Idle-unloaded GPU model after %.0fs idle (torch.cuda "
                "memory stats unavailable — cpu/mps backend or no CUDA)",
                now - _last_used,
            )
        return True


def _cuda_memory_allocated() -> float | None:
    """Return the currently-allocated CUDA bytes, or None when torch/CUDA is
    unavailable (cpu/mps backend, no GPU). Best-effort observability only —
    never raises."""
    try:
        import torch
        if torch.cuda.is_available():
            return float(torch.cuda.memory_allocated())
    except Exception:  # noqa: BLE001 — observability must never crash the timer
        pass
    return None


def _empty_cuda_cache() -> None:
    """Return freed CUDA blocks to the driver. Guarded: ``empty_cache`` is
    CUDA-only, so a cpu/mps host (or a box without torch) must never crash the
    idle timer — soft-fail to a no-op (review C.2 requirement)."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — cuda-only op; cpu/mps → harmless no-op
        pass


async def _idle_unload_loop(poll_secs: float) -> None:
    """Background task: periodically check whether the GPU model has gone idle
    and unload it if so. Runs for the process lifetime; cancellation (shutdown)
    is a clean exit."""
    try:
        while True:
            await asyncio.sleep(poll_secs)
            try:
                await _maybe_unload_idle_model()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad poll must not kill the loop
                logger.warning("Idle-unload check failed (continuing): %s", e)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Code Embedding Service", version="1.0.0")


class EmbedRequest(BaseModel):
    texts: list[str]
    is_query: bool = False


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dim: int
    count: int
    backend: str
    model: str


@app.post("/embed", response_model=EmbedResponse)
async def embed_endpoint(req: EmbedRequest):
    if not req.texts:
        raise HTTPException(status_code=400, detail="texts list is empty")
    if len(req.texts) > 256:
        raise HTTPException(status_code=400, detail="max 256 texts per request")

    global _in_flight
    # C-9: shed at capacity (in-flight == MAX) with an HONEST message — the
    # service runs inference under a single lock, so an over-cap request would
    # BLOCK, not queue. Same condition on both endpoints (_should_shed).
    if _should_shed():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Service at capacity ({MAX_CONCURRENT} request(s) in flight). "
                "Retry later."
            ),
        )

    sem = _get_semaphore()
    _in_flight += 1
    _stamp_used()  # v0.2.79 §C: mark activity so the idle-unload timer resets
    try:
        async with sem:
            t0 = time.time()
            lock = _get_lock()
            async with lock:
                # Run sync inference in thread pool to avoid blocking the event
                # loop, but the lock ensures only one inference runs at a time.
                loop = asyncio.get_event_loop()
                vecs = await loop.run_in_executor(None, embed, req.texts, req.is_query)
            elapsed = time.time() - t0
    finally:
        _in_flight -= 1
        _stamp_used()  # re-stamp on completion — idle measured from last activity

    dim = len(vecs[0]) if vecs else 0
    logger.info(
        "Embedded %d texts in %.2fs (%.0f texts/s) backend=%s in_flight=%d",
        len(req.texts), elapsed, len(req.texts) / elapsed if elapsed > 0 else 0,
        BACKEND, _in_flight,
    )
    return EmbedResponse(
        embeddings=vecs,
        dim=dim,
        count=len(vecs),
        backend=BACKEND,
        model=MODEL_NAME,
    )


# Ollama-compatible endpoint for drop-in replacement
class OllamaEmbedRequest(BaseModel):
    model: str = ""
    prompt: str = ""


@app.post("/api/embeddings")
async def ollama_compat_endpoint(req: OllamaEmbedRequest):
    """Ollama-compatible /api/embeddings endpoint for drop-in replacement.

    C-9 (v0.2.75 P3b): this is the endpoint the MCP inline fallback + the CLI
    raw fallback hit, so it MUST shed under burst identically to ``/embed`` —
    pre-fix it had no shed at all, so one burst made the two endpoints diverge
    (``/embed`` 503'd while this one blocked). Shares ``_should_shed`` +
    ``_in_flight``.
    """
    if not req.prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    global _in_flight
    if _should_shed():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Service at capacity ({MAX_CONCURRENT} request(s) in flight). "
                "Retry later."
            ),
        )

    _in_flight += 1
    _stamp_used()  # v0.2.79 §C: mark activity so the idle-unload timer resets
    try:
        async with _get_semaphore():
            async with _get_lock():
                loop = asyncio.get_event_loop()
                vecs = await loop.run_in_executor(None, embed, [req.prompt], False)
    finally:
        _in_flight -= 1
        _stamp_used()  # re-stamp on completion — idle measured from last activity
    return {"embedding": vecs[0]}


@app.get("/health")
async def health():
    try:
        # v0.2.79 §C: for the gpu backend, do NOT force a model reload just to
        # report ``dim`` — a liveness probe hitting /health must not undo an
        # idle-unload. Use the dim cached at first load; only fall through to
        # ``get_dim()`` (which may load) when the model has never loaded yet.
        if BACKEND == "gpu":
            dim = _st_model_dim if _st_model_dim is not None else get_dim()
            model_loaded = _st_model is not None
        else:
            dim = get_dim()
            model_loaded = True  # ollama backend holds no in-process weights
        return {
            "status": "ok",
            "backend": BACKEND,
            "model": MODEL_NAME,
            "dim": dim,
            "device": DEVICE,
            "max_concurrent": MAX_CONCURRENT,
            # C-9: report the REAL in-flight count from the explicit counter
            # (was ``MAX_CONCURRENT - sem._value`` — a private-API read that
            # actually meant "in flight", not "queued"; the key was mis-named).
            "in_flight": _in_flight,
            "inference_busy": _get_lock().locked(),
            # v0.2.79 §C: idle-unload observability.
            "model_loaded": model_loaded,
            "idle_unload_secs": IDLE_UNLOAD_SECS,
        }
    except Exception as e:
        # Log the full error internally; return only a generic message in the
        # response body to avoid leaking internal details (stack traces, model
        # paths, etc.) through the health endpoint. This service is
        # localhost-only, but defence-in-depth still applies.
        logger.error("Health check failed: %s", e)
        return {"status": "error", "error": "health check failed — see server logs"}


@app.on_event("startup")
async def startup():
    global _idle_task
    logger.info("Starting code embedding service: backend=%s model=%s port=%d", BACKEND, MODEL_NAME, PORT)
    if BACKEND == "gpu":
        # Pre-load model on startup
        _load_gpu_model()
        # Fresh activity stamp so the idle window starts from a loaded model,
        # not from import time (which may predate the pre-load by seconds).
        _stamp_used()
        # v0.2.79 §C: launch the idle-unload timer (gpu backend only; the
        # ollama backend holds no in-process weights). Poll cadence is a
        # fraction of the idle window (min 5 s, max 60 s) so an idle model is
        # freed within roughly one poll of crossing the deadline.
        if IDLE_UNLOAD_SECS > 0:
            poll = max(5.0, min(60.0, IDLE_UNLOAD_SECS / 10.0))
            _idle_task = asyncio.create_task(_idle_unload_loop(poll))
            logger.info(
                "Idle-unload timer active: model unloads after %.0fs idle "
                "(poll every %.0fs); set CODE_EMBED_IDLE_UNLOAD_SECS=0 to disable",
                IDLE_UNLOAD_SECS, poll,
            )
        else:
            logger.info(
                "Idle-unload disabled (CODE_EMBED_IDLE_UNLOAD_SECS=0) — GPU model "
                "stays resident for the process lifetime"
            )


@app.on_event("shutdown")
async def shutdown():
    global _idle_task
    if _idle_task is not None:
        _idle_task.cancel()
        try:
            await _idle_task
        except asyncio.CancelledError:
            pass
        _idle_task = None


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # V52-AI (v0.2.52): exit cleanly if an orchestrator update is in
    # progress. Same shape as the MCP server gates — prevents the
    # ensure-containers hook from spawning a GPU model load mid-update,
    # which would race the launcher's binary refresh.
    #
    # `_lib.update_gate` is SHIPPED; import_lib_member LOUD-FAILS if it's
    # missing. The pre-fix silent `exit_if_update_in_progress = None` stub
    # disabled the mid-update GPU-load guard on the exact broken-install
    # path most likely to be mid-update. When this service runs as a bare
    # script (container / ensure-containers hook) sys.path[0] is the
    # server's own dir, so the parent (claude_mcp_servers/) must be
    # inserted for `_lib` to resolve.
    from pathlib import Path as _Path
    _mcp_root = str(_Path(__file__).resolve().parent.parent)
    if _mcp_root not in sys.path:
        sys.path.insert(0, _mcp_root)
    from _lib.bootstrap import import_lib_member
    exit_if_update_in_progress = import_lib_member(
        "update_gate", "exit_if_update_in_progress"
    )
    exit_if_update_in_progress("code-embedding service")

    # Detect import path: standalone (container) vs package (python -m ...)
    try:
        import claude_mcp_servers.code_embedding_service.server  # noqa: F401
        app_path = "claude_mcp_servers.code_embedding_service.server:app"
    except ImportError:
        app_path = "server:app"

    uvicorn.run(
        app_path,
        host="0.0.0.0",
        port=PORT,
        workers=1,  # MUST be 1 — GPU model lives in-process, multiple workers = multiple VRAM copies
        log_level="info",
    )
