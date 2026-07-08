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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKEND = os.getenv("CODE_EMBED_BACKEND", "gpu").lower()
DEVICE = os.getenv("CODE_EMBED_DEVICE", "auto")
DTYPE = os.getenv("CODE_EMBED_DTYPE", "bfloat16")
PORT = int(os.getenv("CODE_EMBED_PORT", "11440"))
BATCH_SIZE = int(os.getenv("CODE_EMBED_BATCH_SIZE", "32"))
MAX_CONCURRENT = int(os.getenv("CODE_EMBED_MAX_CONCURRENT", "4"))  # max in-flight requests before shed
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
    try:
        async with _get_semaphore():
            async with _get_lock():
                loop = asyncio.get_event_loop()
                vecs = await loop.run_in_executor(None, embed, [req.prompt], False)
    finally:
        _in_flight -= 1
    return {"embedding": vecs[0]}


@app.get("/health")
async def health():
    try:
        dim = get_dim()
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
    logger.info("Starting code embedding service: backend=%s model=%s port=%d", BACKEND, MODEL_NAME, PORT)
    if BACKEND == "gpu":
        # Pre-load model on startup
        _load_gpu_model()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # V52-AI (v0.2.52): exit cleanly if an orchestrator update is in
    # progress. Same shape as the MCP server gates — prevents the
    # ensure-containers hook from spawning a GPU model load mid-update,
    # which would race the launcher's binary refresh.
    try:
        from _lib.update_gate import exit_if_update_in_progress  # type: ignore
    except ImportError:
        from pathlib import Path as _Path
        _parent_dir = str(_Path(__file__).resolve().parent.parent)
        if _parent_dir not in sys.path:
            sys.path.insert(0, _parent_dir)
        try:
            from _lib.update_gate import exit_if_update_in_progress  # type: ignore
        except ImportError:
            exit_if_update_in_progress = None  # type: ignore
    if exit_if_update_in_progress is not None:
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
