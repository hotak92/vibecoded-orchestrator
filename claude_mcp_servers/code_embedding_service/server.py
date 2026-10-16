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
  CODE_EMBED_MAX_CONCURRENT  Concurrent execution slots (default: 4). Requests
                          beyond the slots QUEUE (FIFO-fair) instead of being
                          shed — owner rule v0.2.102: concurrent users wait,
                          none are dropped. The wait is bounded by
                          CODE_EMBED_QUEUE_WAIT_SECS; only past that bound is a
                          request refused (503 + Retry-After).
  CODE_EMBED_QUEUE_WAIT_SECS  Max seconds a request waits for a concurrency slot
                          before it is refused with 503 + Retry-After. Generous
                          by design (shipped timeouts are long; a queued request
                          is progress, not a wedge). 0 = do not queue (refuse
                          over-capacity requests immediately — the pre-v0.2.102
                          C-9 shed — still with Retry-After). Default: 600.
                          Clients MUST use a request timeout above this (the
                          bundled clients default to 660 s — see
                          vco_lib/retry_after.py).
  CODE_EMBED_IDLE_UNLOAD_SECS  Idle seconds before the GPU model (~7.25 GB CodeSage
                          weights) is unloaded to free VRAM; the next request
                          transparently lazy-reloads it. 0 = never unload
                          (keep resident for the process lifetime). Default: 300.
                          gpu backend only — the ollama backend holds no in-process weights.
                          The unload drops the module's strong reference, runs
                          gc.collect() to break the SentenceTransformer/nn.Module
                          reference cycles that otherwise pin the weights, then
                          empty_cache(), and MEASURES the freed bytes. If the free
                          does not actually happen (a live reference still holds the
                          weights) it KEEPS the model loaded and logs a WARNING
                          rather than discarding the handle — so the next request
                          reuses the resident copy instead of allocating a second one
                          that OOMs the GPU.
  CODE_EMBED_LOAD_OOM_RETRIES  Retry attempts after a first CUDA-OOM model load
                          (total attempts = retries + 1). Default: 12.
  CODE_EMBED_LOAD_OOM_BACKOFF_SECS  Seconds to wait between CUDA-OOM load retries
                          (after gc.collect() + empty_cache()). Generous by design —
                          VRAM held by another process can take MINUTES to release
                          (Ollama's keep-alive alone is 5 min), so the default
                          budget is 12 × 30 s ≈ 6 min. When the budget is exhausted
                          the service returns 503 + Retry-After (never a 500
                          traceback) and starts a short cooldown in which queued
                          loaders fail fast with the same 503 — waiters SHARE the
                          failed outcome instead of each re-running a full budget.
  CODE_EMBED_UNLOAD_FREED_MIN_MB  VRAM floor (MB) below which an idle-unload is
                          treated as successful without measuring freed bytes (a
                          cpu/mps model or a test mock holds no real VRAM, so there
                          is nothing to OOM on reload). Default: 64.
  OLLAMA_URL              Ollama API URL (default: http://localhost:11435)
  CODE_EMBED_INSTRUCTION  OVERRIDE for the query-side instruction prefix.
                          Default "" — when unset, the service resolves the
                          LOADED model's prefix from the shared table
                          (weaviate_mcp/chunking.py::MODEL_QUERY_PREFIXES),
                          applied only to requests that send is_query=true.
                          CodeSage/jina need none, so "" is the shipped
                          result either way.
  CODE_EMBED_MAX_SEQ_LEN  Sets the served max_seq_length window (default: the
                          model snapshot's sentence_bert_config.json — 1024
                          for codesage-large-v2, NOT the 2048 in config.json).
                          An input whose token count exceeds the window is
                          REFUSED with HTTP 400 ("input length exceeds the
                          context length"), never silently truncated (W1).
                          If you change it, change the SSOT too:
                          MODEL_TOKEN_LIMITS in weaviate_mcp/chunking.py is
                          where every chunk/entity budget is derived from —
                          this env only moves the SERVING window. Leaving the
                          two out of step is the exact drift W1 was (budgets
                          sized for one window, model serving another); it no
                          longer loses text silently — the refusal + the
                          caller's shrink make it loud — but a budget above
                          the served window costs an extra round trip per
                          over-window entity and a truncated leading window.
  CODE_EMBED_TRUST_REMOTE "true" | "false" (default: "true")

Usage:
  # GPU mode (default):
  python -m claude_mcp_servers.code_embedding_service.server

  # Ollama mode:
  CODE_EMBED_BACKEND=ollama CODE_EMBED_MODEL=qwen3-embedding:0.6b python -m ...

API:
  POST /embed  {"texts": [...], "is_query": false, "task": null}  → {"embeddings": [[...], ...], "dim": N}
  GET  /health  → {"status": "ok", "backend": "...", "model": "...", "dim": N,
                   "source_sha": "<sha256 of the source files in this image>"}
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import logging
import os
import sys
import threading
import time
import weakref
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("code_embedding_service")

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def _configure_logging_for_environment() -> bool:
    """Configure logging; return True when ``vco_lib`` was available.

    v0.2.91 (Decision #21) routed this through ``vco_lib.log_setup`` so the
    global ``VCO_LOG_LEVEL`` pref is honoured, as a BARE import under the
    "no silent-fallback on vco_lib imports" discipline.

    v0.2.92: that discipline's premise — *vco_lib is part of every healthy
    install* — is FALSE for this one file.  ``server.py`` runs in two
    environments, and only one of them is a VCO install:

    * the **host process** (``python -m claude_mcp_servers.code_embedding_service.server``)
      — a VCO install; ``vco_lib`` is there and a failure to import it IS a
      broken install;
    * the **container image**, whose Dockerfiles COPY exactly
      ``requirements.txt`` + ``image_source.py`` + ``server.py`` into a
      ``pytorch`` base and install nothing else.  ``vco_lib`` is absent BY
      DESIGN and cannot be added: the compose build context is this service
      directory, so a ``COPY`` cannot reach the repo root.

    A bare import therefore made the shipped image unstartable —
    ``ModuleNotFoundError: No module named 'vco_lib'`` at import time, before
    uvicorn binds, under ``restart: unless-stopped`` (i.e. a crash loop).
    Nobody saw it because nothing ever rebuilt the image; the two defects hid
    each other. Verified by running the then-current source inside the
    then-current image.

    So: fall back, but ANNOUNCE.  This is not the masking the rule forbids —
    the fallback names the environment on the container's own log, and it is
    scoped to ``ImportError`` of that one module.  Anything else propagates.
    """
    try:
        from vco_lib.log_setup import configure_logging
    except ImportError:
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
        logger.warning(
            "vco_lib is not importable — running as the minimal containerized "
            "service image, where it is absent by design. Logging falls back to "
            "INFO and the global VCO_LOG_LEVEL preference is NOT honoured. If "
            "you are seeing this from a host process rather than a container, "
            "your VCO install is broken: re-run `python install.py --update`."
        )
        return False
    configure_logging(format=_LOG_FORMAT)
    return True


#: True when ``vco_lib`` resolved (host process); False in the minimal image.
#: Reported on ``/health`` so the answer to "which environment is serving me?"
#: is readable rather than inferred — a HOST process with this False is a
#: broken install, while a container with it False is the normal shape.
VCO_LIB_AVAILABLE = _configure_logging_for_environment()


def _load_image_source_sha():
    """Digest of the source files THIS process is running, or ``None``.

    Loaded by path (not by import name) so it works identically as
    ``/app/server.py`` in the image — where ``claude_mcp_servers`` is not a
    package and the repo root is not on ``sys.path`` — and as a package
    module in the checkout.  The rule itself lives once, in
    ``image_source.py``, which is COPYed into the image alongside this file;
    see that module for why a mirror was not acceptable.

    ``None`` (rendered as JSON ``null`` on ``/health``) means "could not
    compute", which is NOT the same as the field being ABSENT: absent means a
    pre-v0.2.92 image that predates the field entirely.  Callers treat both
    as *not provably current*, and only the digest matching the checkout as
    *current*.
    """
    here = Path(__file__).resolve().parent
    module_path = here / "image_source.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "_vco_code_embed_image_source", module_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.source_sha(here)
    except Exception:  # noqa: BLE001 — a self-description must never crash the service
        logger.warning("could not compute the image source digest from %s", module_path)
        return None


#: The digest of the files this process is running (see ``/health``).
#: Computed ONCE at import: it describes the code that is loaded, so
#: re-reading the files later could only report something this process is
#: not running.
SOURCE_SHA = _load_image_source_sha()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKEND = os.getenv("CODE_EMBED_BACKEND", "gpu").lower()
DEVICE = os.getenv("CODE_EMBED_DEVICE", "auto")
DTYPE = os.getenv("CODE_EMBED_DTYPE", "bfloat16")
PORT = int(os.getenv("CODE_EMBED_PORT", "11440"))
BATCH_SIZE = int(os.getenv("CODE_EMBED_BATCH_SIZE", "32"))
MAX_CONCURRENT = int(os.getenv("CODE_EMBED_MAX_CONCURRENT", "4"))  # concurrent execution slots; over-capacity requests queue (QUEUE_WAIT_SECS)


def _env_text(name: str) -> "str | None":
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _env_float(name: str, default: float, *, minimum: "float | None" = None,
               clamp: bool = False) -> float:
    """DOCUMENTED COPY — must match ``vco_lib/env_values.py::env_float``
    (the ``minimum`` / ``clamp`` subset). This file also runs inside the
    container image, where ``vco_lib`` is absent BY DESIGN (see
    :func:`_configure_logging_for_environment`), so it cannot import the one
    home; ``tests/test_v02102_env_values.py`` pins the parity."""
    text = _env_text(name)
    if text is None:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return minimum if clamp else default
    return value


def _env_int(name: str, default: int, *, minimum: "int | None" = None,
             clamp: bool = False) -> int:
    """DOCUMENTED COPY — must match ``vco_lib/env_values.py::env_int`` (the
    ``minimum`` / ``clamp`` subset); same reason and parity test as
    :func:`_env_float`."""
    text = _env_text(name)
    if text is None:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return minimum if clamp else default
    return value


def _resolve_idle_unload_secs() -> float:
    """Idle seconds before the GPU model is unloaded. 0 = never; a bad/negative
    value coerces to the 300 s default (conservative: never a shorter-than-asked
    window that would thrash the reload). Read once at import — the timer honours
    it for the process lifetime."""
    return _env_float("CODE_EMBED_IDLE_UNLOAD_SECS", 300.0, minimum=0)


IDLE_UNLOAD_SECS = _resolve_idle_unload_secs()  # 0 = never unload


def _resolve_load_oom_retries() -> int:
    """Retry attempts after a FIRST CUDA-OOM model load (total attempts =
    retries + 1). A bad/negative value coerces to the default. Read once at
    import — the load path honours it for the process lifetime."""
    return _env_int("CODE_EMBED_LOAD_OOM_RETRIES", 12, minimum=0, clamp=True)


def _resolve_load_oom_backoff_secs() -> float:
    """Seconds to wait between CUDA-OOM load retries (after gc.collect() +
    empty_cache()). Generous by design: VRAM held by another process — e.g.
    Ollama before its keep-alive expires — can take minutes to be released, and
    a slow machine's load is not the ceiling. A bad/negative value coerces to
    the default. Read once at import."""
    # ``max`` (not a bound): it maps nan to 0.0, exactly as before.
    return max(0.0, _env_float("CODE_EMBED_LOAD_OOM_BACKOFF_SECS", 30.0))


def _resolve_unload_freed_min_mb() -> float:
    """VRAM floor (MB) below which an idle-unload is treated as successful
    WITHOUT measuring freed bytes. A real model holds GBs; below this floor
    nothing substantial was allocated (a cpu/mps model, or a test mock that
    holds no real VRAM), so there is nothing to OOM on reload and no reason to
    keep the handle. A bad/negative value coerces to the default. Read once at
    import."""
    return max(0.0, _env_float("CODE_EMBED_UNLOAD_FREED_MIN_MB", 64.0))


def _resolve_queue_wait_secs() -> float:
    """Max seconds a request waits for a concurrency slot before it is refused
    with 503 + Retry-After (v0.2.102 r2 — the owner rule is that concurrent
    requests QUEUE and none are dropped; this is the ONLY bound on the wait).
    Generous by design, per the long-timeouts doctrine. 0 is honoured and means
    "do not queue" (the pre-v0.2.102 C-9 immediate shed, still with
    Retry-After); a bad/negative value coerces to the 600 s default. Read once
    at import."""
    return _env_float("CODE_EMBED_QUEUE_WAIT_SECS", 600.0, minimum=0)


#: Bounded CUDA-OOM load-retry budget (v0.2.102). ``LOAD_OOM_RETRIES`` extra
#: attempts after the first, ``LOAD_OOM_BACKOFF_SECS`` apart — the default
#: total budget is 12 × 30 s ≈ 6 MINUTES, because the VRAM a retry waits on is
#: typically held by another process whose release cadence is minutes-scale
#: (Ollama's keep-alive alone is 5 min). Exhausting it answers 503 +
#: Retry-After and starts the ``_load_unavailable_until`` cooldown.
LOAD_OOM_RETRIES = _resolve_load_oom_retries()
LOAD_OOM_BACKOFF_SECS = _resolve_load_oom_backoff_secs()
UNLOAD_FREED_MIN_MB = _resolve_unload_freed_min_mb()
#: Queue bound for over-capacity requests (see ``_resolve_queue_wait_secs``).
QUEUE_WAIT_SECS = _resolve_queue_wait_secs()
#: A genuine unload of a multi-GB model frees at least HALF the VRAM it held;
#: less means a live reference still pins the weights — keep the handle rather
#: than discard it and allocate a second copy on the next request.
_UNLOAD_FREED_RATIO = 0.5

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


def _shared_query_prefix(model_name: str, task: "str | None" = None) -> "str | None":
    """Query prefix for ``model_name`` from the SHARED table, or None.

    v0.2.101: the per-model query-prefix table lives ONCE, beside
    ``MODEL_TOKEN_LIMITS`` in ``claude_mcp_servers/weaviate_mcp/chunking.py``
    (A>B>C rule A — one implementation, not a mirror). The service imports it
    when the orchestrator packages are on ``sys.path`` (the HOST process) and
    uses the table's OWN code-side default task (``QUERY_TASK_DEFAULT_CODE``)
    when the caller names none, so the task wording cannot drift.

    ``None`` means "table unavailable" — the minimal CONTAINER image, whose
    Dockerfiles COPY only ``requirements.txt`` + ``image_source.py`` +
    ``server.py`` and cannot reach ``chunking.py`` by design. That is NOT a
    silent fallback: the caller then uses ``CODE_EMBED_INSTRUCTION`` or none,
    and the shipped container models (codesage, jina) need no prefix at all.
    """
    for mod_name in (
        "weaviate_mcp.chunking",
        "claude_mcp_servers.weaviate_mcp.chunking",
    ):
        try:
            module = importlib.import_module(mod_name)
            task_key = task or getattr(module, "QUERY_TASK_DEFAULT_CODE", "code_nl")
            return module.query_prefix_for_model(model_name, task_key)
        except Exception:  # noqa: BLE001 — next candidate, else None
            continue
    return None


def _resolve_query_instruction(model_name: str, task: "str | None" = None) -> str:
    """The query instruction the service applies when a caller sends
    ``is_query=True``.

    Priority (ONE chain, no second table):
      1. ``CODE_EMBED_INSTRUCTION`` env — explicit operator override.
      2. The shared per-model table (``chunking.MODEL_QUERY_PREFIXES``) for the
         loaded model, worded for ``task`` (default: the code-side wording).
      3. ``""`` — table unavailable (container image) and no override set.
    """
    if INSTRUCTION:
        return INSTRUCTION
    prefix = _shared_query_prefix(model_name, task)
    return prefix or ""


# ---------------------------------------------------------------------------
# Backend: GPU (sentence-transformers)
# ---------------------------------------------------------------------------
_st_model = None  # lazy-loaded; the ONLY persistent strong reference to the
# SentenceTransformer. Every other use is transient and released when its call
# returns: the ``model`` local in ``_embed_gpu``/``_dim_gpu``, the ``model``
# argument threaded through ``_refuse_over_window``/``_gpu_token_count``, and the
# executor frame running ``encode``. So dropping this one global is what frees the
# weights — BUT only after ``gc.collect()``: the SentenceTransformer/nn.Module
# object graph is cyclic (module ↔ ``_modules``/``_parameters`` dicts, hooks), so
# ``_st_model = None`` alone leaves the refcount above zero and the ~7.25 GB of
# tensors still allocated when ``empty_cache()`` runs (the observed "freed 0 MB").
# v0.2.79 §C: cache the embedding dimension the first time the model loads. It
# is a fixed model property, so ``/health`` can report ``dim`` WITHOUT forcing a
# reload of an idle-unloaded model — otherwise every liveness probe would
# immediately re-materialise the 7.25 GB weights and the model would never stay
# unloaded. Survives unload (``_st_model = None`` does not clear this). It is an
# ``int`` — it does NOT reference the model object, so it never pins the weights.
_st_model_dim: int | None = None
# v0.2.102: single-flight guard for ``_load_gpu_model``. A plain ``threading``
# lock (NOT the asyncio ``_inference_lock``) because the load runs synchronously
# on an executor thread; it serialises the actual construction so concurrent
# callers produce ONE load and the waiters reuse its result.
_load_lock = threading.Lock()
# v0.2.102 r2: monotonic deadline set when a load EXHAUSTS its CUDA-OOM budget.
# While it is in the future, loaders fail fast with the same
# ``_ModelLoadUnavailable`` (→ 503 + Retry-After) — queued waiters SHARE the
# failed attempt's outcome instead of each re-running a full multi-minute budget
# serially. Cleared by the next successful load; its length is the Retry-After
# the 503 carries, so a client that obeys the header arrives exactly when a
# fresh attempt becomes possible.
_load_unavailable_until: float = 0.0
# v0.2.102 observability, reported on ``/health``:
_last_unload_freed_mb: float | None = None  # MB freed by the most recent idle-unload
_last_load_error: str | None = None         # last model-load failure (None when healthy)


class _ModelLoadUnavailable(RuntimeError):
    """Raised when the GPU model could not be loaded within the bounded
    CUDA-OOM retry budget. The endpoints translate it into a 503 + Retry-After
    rather than letting it surface as a 500 with a traceback."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _is_cuda_oom(exc: BaseException | None) -> bool:
    """True when ``exc`` is a CUDA out-of-memory error. Matched by class name
    (``torch.OutOfMemoryError``) and message substring so it works with the real
    torch AND with test fakes that carry no ``torch`` dependency. Never raises."""
    if exc is None:
        return False
    if "outofmemory" in type(exc).__name__.lower():
        return True
    return "out of memory" in str(exc).lower()


def _retry_after_secs() -> int:
    """Retry-After hint (seconds) for a 503 when the model could not load. Tied
    to the OOM backoff so a client retries on the same cadence the service uses
    internally."""
    return max(1, int(LOAD_OOM_BACKOFF_SECS))


def _resolve_device() -> str:
    """Resolve ``DEVICE`` (``auto`` probes CUDA → Apple Metal (MPS) → CPU)."""
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
    return device


def _resolve_dtype() -> str:
    """Resolve ``DTYPE`` to a sentence-transformers ``torch_dtype`` string."""
    dtype_map = {
        "float32": "float32",
        "bfloat16": "bfloat16",
        "float16": "float16",
    }
    return dtype_map.get(DTYPE, "bfloat16")


def _load_gpu_model():
    """Return the loaded SentenceTransformer, loading it on first use.

    SINGLE-FLIGHT (v0.2.102): guarded by a dedicated ``_load_lock`` with a
    double-check, so concurrent callers — the request path (serialised under
    ``_inference_lock``), plus ``startup`` and ``/health`` — produce exactly ONE
    load and the waiters reuse its result. This is the fix for the "three
    Loading-model attempts in 16 s" defect: previously each request whose load
    OOMed left ``_st_model`` None, so the next request retried a full load and
    allocated a SECOND copy on top of the un-freed first one. The construction
    and the bounded OOM retry budget live in ``_load_gpu_model_locked``.
    """
    global _st_model
    if _st_model is not None:
        return _st_model
    with _load_lock:
        # Double-check under the lock: a concurrent loader may have finished
        # while we waited, in which case we reuse its result (never a 2nd load).
        if _st_model is not None:
            return _st_model
        # v0.2.102 r2: share a RECENTLY-FAILED outcome too. Without this, every
        # waiter that acquires the lock after a failed budget re-runs its own
        # full ~6-minute budget serially (N waiters × budget) — the cooldown
        # makes them fail fast with the same 503 + Retry-After instead.
        cooldown = _load_unavailable_until - time.monotonic()
        if cooldown > 0:
            raise _ModelLoadUnavailable(
                "GPU model unavailable: the previous load exhausted its CUDA-OOM "
                f"budget and VRAM has not freed ({cooldown:.0f}s left in the "
                f"fail-fast cooldown). Last error: {_last_load_error}",
                retry_after=int(cooldown) + 1,
            )
        return _load_gpu_model_locked()


def _load_gpu_model_locked():
    """Construct the model. Runs UNDER ``_load_lock`` (single-flight).

    On CUDA OOM: ``gc.collect()`` + ``empty_cache()`` (reclaim anything this
    process can) then a bounded backoff retry — VRAM held by ANOTHER process
    (e.g. Ollama before its keep-alive expires) can be released during the wait,
    so a retry that seemed hopeless can succeed. Only after the budget
    (``LOAD_OOM_RETRIES`` × ``LOAD_OOM_BACKOFF_SECS``) is exhausted does it raise
    ``_ModelLoadUnavailable``, which the endpoints map to a 503 + Retry-After —
    never a 500 with a traceback. A NON-OOM error (a config/import fault) is not
    retried; it propagates immediately and is recorded on ``_last_load_error``.
    """
    global _st_model, _st_model_dim, _last_load_error, _load_unavailable_until

    from sentence_transformers import SentenceTransformer

    device = _resolve_device()
    torch_dtype = _resolve_dtype()
    kwargs = {
        "trust_remote_code": TRUST_REMOTE,
        "device": device,
        "model_kwargs": {"torch_dtype": torch_dtype},
    }
    attempts = LOAD_OOM_RETRIES + 1
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            logger.info(
                "Loading model '%s' on device='%s' dtype='%s' (attempt %d/%d)...",
                MODEL_NAME, device, torch_dtype, attempt + 1, attempts,
            )
            t0 = time.time()
            # W11 (2026-09-05, verified against sentence-transformers 5.5.0's
            # signature): CODE_EMBED_MAX_SEQ_LEN used to be plumbed into
            # ``model_kwargs``, which SentenceTransformer forwards to
            # ``AutoModel.from_pretrained`` — the TRANSFORMER's kwargs. The window
            # that governs truncation is the ``max_seq_length`` ATTRIBUTE (not an
            # init parameter at all), so the documented knob could not change it.
            # Set the attribute directly after load instead.
            model = SentenceTransformer(MODEL_NAME, **kwargs)
            if MAX_SEQ_LEN:
                model.max_seq_length = int(MAX_SEQ_LEN)
            dim = model.get_sentence_embedding_dimension()
            # Publish atomically: the handle last, so a concurrent double-check
            # under ``_load_lock`` never sees a half-initialised model.
            _st_model_dim = dim  # cache for /health so idle-unload survives probes
            _st_model = model
            _last_load_error = None
            _load_unavailable_until = 0.0  # a success ends any cooldown
            logger.info(
                "Model loaded in %.1fs — dim=%d, device=%s",
                time.time() - t0, dim, device,
            )
            return model
        except Exception as exc:  # noqa: BLE001 — classify, then retry or propagate
            if not _is_cuda_oom(exc):
                _last_load_error = f"{type(exc).__name__}: {exc}"
                raise
            last_exc = exc
            # Reclaim anything this process can before waiting on an external
            # holder (Ollama keep-alive) to release its VRAM.
            gc.collect()
            _empty_cuda_cache()
            if attempt + 1 < attempts:
                logger.warning(
                    "CUDA OOM loading model (attempt %d/%d) — freed local caches, "
                    "retrying in %.1fs: %s",
                    attempt + 1, attempts, LOAD_OOM_BACKOFF_SECS, exc,
                )
                if LOAD_OOM_BACKOFF_SECS > 0:
                    time.sleep(LOAD_OOM_BACKOFF_SECS)
    _last_load_error = (
        f"CUDA out of memory persisted through {attempts} load attempt(s) "
        f"(~{LOAD_OOM_RETRIES * LOAD_OOM_BACKOFF_SECS:.0f}s budget): {last_exc}"
    )
    # v0.2.102 r2: start the fail-fast cooldown — queued waiters share THIS
    # failed outcome (503 + Retry-After) instead of each re-running the budget.
    # The cooldown lasts exactly as long as the Retry-After we send, so an
    # obeying client arrives when a fresh attempt is possible.
    _load_unavailable_until = time.monotonic() + _retry_after_secs()
    logger.error(
        "Model load FAILED — CUDA OOM through %d attempt(s); the GPU is occupied "
        "by another process. Serving 503 + Retry-After (%ds fail-fast cooldown) "
        "until VRAM frees and a lazy reload succeeds: %s",
        attempts, _retry_after_secs(), last_exc,
    )
    raise _ModelLoadUnavailable(_last_load_error, retry_after=_retry_after_secs())


def _gpu_token_count(model, text: str) -> int:
    """Tokenise ``text`` with the model's own tokenizer, UNTRUNCATED.

    ``model.encode`` truncates silently at ``max_seq_length`` (HTTP 200, no
    marker), so the honest window check must count on the raw tokenizer.
    """
    ids = model.tokenizer(text, add_special_tokens=True)["input_ids"]
    return len(ids)


def _refuse_over_window(
    model, texts: list[str], is_query: bool, instruction: str | None = None
) -> None:
    """W1 (wiring audit, 2026-09-05): REFUSE over-window input instead of
    letting sentence-transformers truncate it silently.

    ``model.encode`` accepts any length and drops everything past
    ``max_seq_length`` with a 200 — the GPU code tier's default path fed it
    entities budgeted for the 2 048 architectural cap while the snapshot
    serves 1 024 (``sentence_bert_config.json``), so roughly half of every
    maximal entity never influenced its vector, untagged. This mirrors what
    ``truncate: false`` gives on the Ollama side: a hard refusal the caller
    can catch.

    ``instruction`` is the resolved query prefix; when ``None`` it is resolved
    for the LOADED model at the code default task (a direct caller that pins
    nothing). It must equal what ``_embed_gpu`` actually prepends, or the
    window check would count a different string than the one encoded.

    The message deliberately carries the phrase ``input length exceeds the
    context length`` — the SAME shape ``_is_context_overflow_error``
    (``vco_lib.embedding_service``) recognises on Ollama refusals, matched
    through the ``CodeEmbed /embed returned HTTP 400: ...`` wrapper the
    CodeEmbedAdapter raises. One home, one behaviour: do not invent a
    second phrasing, and do not widen the detector to match a new one.
    """
    if instruction is None:
        instruction = _resolve_query_instruction(MODEL_NAME)
    window = int(model.max_seq_length)
    for i, text in enumerate(texts):
        if is_query and instruction:
            text = instruction + text
        n = _gpu_token_count(model, text)
        if n > window:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"input length exceeds the context length: text at index "
                    f"{i} is {n} tokens, window is {window} "
                    f"(model {MODEL_NAME}, sentence_bert max_seq_length)"
                ),
            )


def _embed_gpu(
    texts: list[str], is_query: bool = False, task: str | None = None
) -> list[list[float]]:
    model = _load_gpu_model()
    instruction = _resolve_query_instruction(MODEL_NAME, task)
    _refuse_over_window(model, texts, is_query, instruction)
    prompt = instruction if is_query and instruction else None
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
    dim = model.get_sentence_embedding_dimension()
    # sentence-transformers types this Optional; a LOADED model always knows its
    # dimension, so None means a broken model — loud-fail, never guess.
    if dim is None:
        raise RuntimeError(
            f"loaded model {MODEL_NAME!r} reports no embedding dimension"
        )
    return int(dim)


# ---------------------------------------------------------------------------
# Backend: Ollama
# ---------------------------------------------------------------------------
#: The env key carrying the host-resolved Ollama embed body template. MUST
#: MATCH ``vco_lib/ollama_embed_body.py`` ``CONTAINER_BODY_ENV_KEY`` and the
#: ``code_embed`` environment in ``infrastructure/docker-compose.yml``
#: (pinned by tests/test_v02103_code_embed_ollama_body.py).
OLLAMA_BODY_ENV_KEY = "CODE_EMBED_OLLAMA_BODY"


def _ollama_embed_request_body(model: str, text: str) -> dict:
    """The Ollama ``/api/embeddings`` body for ``text`` — identical to every
    other VCO caller's (one runner per model: same num_ctx, keep_alive and
    ``truncate: false``).

    * Host process (``VCO_LIB_AVAILABLE``): the ONE builder,
      ``vco_lib.ollama_embed_body.embed_request_body`` (v0.2.102 W4-E).
    * Container (no vco_lib, BY DESIGN — v0.2.103 F1: the bare import here
      failed every embed and every /health with ``No module named
      'vco_lib'``): the template install resolved from that same builder and
      wrote to ``infrastructure/.env`` (A>B>C option B), plus the prompt.

    A missing, unparseable or other-model template is REFUSED with the remedy
    — never a body with a guessed ``num_ctx`` (Ollama's 2048 default would
    silently truncate every chunk tail with a 200)."""
    if VCO_LIB_AVAILABLE:
        from vco_lib.ollama_embed_body import embed_request_body

        return embed_request_body(model, prompt=text)
    import json

    raw = os.environ.get(OLLAMA_BODY_ENV_KEY, "").strip()
    remedy = (
        "re-run `python install.py --update` (or the launcher's Update) so "
        f"install writes {OLLAMA_BODY_ENV_KEY} for this model into "
        "infrastructure/.env, then recreate the code_embed container"
    )
    if not raw:
        raise RuntimeError(
            f"{OLLAMA_BODY_ENV_KEY} is not set — the ollama backend cannot "
            f"build its embed request without vco_lib; {remedy}"
        )
    try:
        template = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"{OLLAMA_BODY_ENV_KEY} is not valid JSON ({exc}); {remedy}") from exc
    if not isinstance(template, dict) or template.get("model") != model:
        got = template.get("model") if isinstance(template, dict) else None
        raise RuntimeError(
            f"{OLLAMA_BODY_ENV_KEY} was resolved for model {got!r}, but this "
            f"service serves {model!r}; {remedy}"
        )
    body = dict(template)
    body["prompt"] = text
    return body


def _embed_ollama(
    texts: list[str], is_query: bool = False, task: str | None = None
) -> list[list[float]]:
    import requests

    instruction = _resolve_query_instruction(MODEL_NAME, task)
    results = []
    for text in texts:
        if is_query and instruction:
            text = instruction + text
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json=_ollama_embed_request_body(MODEL_NAME, text),
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
def embed(
    texts: list[str], is_query: bool = False, task: str | None = None
) -> list[list[float]]:
    if BACKEND == "gpu":
        return _embed_gpu(texts, is_query, task)
    return _embed_ollama(texts, is_query, task)


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
# a try/finally around the whole endpoint body is honest, public, and identical
# across both endpoints. v0.2.102 r2 semantics: it counts requests INSIDE the
# endpoint — queued-waiting-for-a-slot as well as executing — so ``/health``
# reports the real number and the idle-unload timer never unloads while any
# request is pending. (Its former consumer, the C-9 ``_should_shed`` immediate
# 503, was replaced by the bounded FIFO queue — the owner rule is that
# concurrent requests QUEUE and none are dropped.)
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


def _queue_retry_after_secs() -> int:
    """Retry-After hint (seconds) for the queue-bound 503. Capped at 30 s: the
    queue drains at request cadence (unlike a GPU recovery), so clients should
    re-probe soon even when ``QUEUE_WAIT_SECS`` itself is huge. Floored at 5 s
    (v0.2.102 r3, review nit): a 1 s hint — which ``QUEUE_WAIT_SECS=0`` (the
    no-queue mode) used to yield — makes an honouring client re-probe at ~1 Hz
    for up to its whole 503 tolerance, tighter than the cap intends; one
    inference takes seconds, so re-probing faster than 5 s only burns slots."""
    return max(5, min(30, int(QUEUE_WAIT_SECS)))


# ---------------------------------------------------------------------------
# Idle model unload (v0.2.79 §C; v0.2.102 root-cause fix) — release the
# ~7.25 GB CodeSage VRAM when idle, and PROVE it was released
# ---------------------------------------------------------------------------
# ``_st_model`` holds the CodeSage-Large-v2 weights (~7.25 GB VRAM). On a
# workstation the user shares the GPU with other tools, so we unload the weights
# after ``IDLE_UNLOAD_SECS`` of no ``/embed`` traffic and lazy-reload them on the
# next request (``_load_gpu_model`` is lazy, single-flighted, and OOM-bounded).
# v0.2.102: the unload drops the reference, ``gc.collect()``s to break the
# model's reference cycles, ``empty_cache()``s, and MEASURES the freed bytes. If
# the free did not actually happen (a live reference still pins the weights) it
# KEEPS the model loaded and logs a WARNING — never discarding the handle into an
# un-freed copy that the next request would double-allocate and OOM on.
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

    Returns True iff the model was actually unloaded (its VRAM freed, or there
    was nothing substantial to free). Returns False — and KEEPS the model loaded
    — when a live reference still pins the weights after ``gc.collect()`` +
    ``empty_cache()``, so the next request reuses the resident copy instead of
    allocating a second one that OOMs the GPU (v0.2.102). Safe to call with any
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
    global _st_model, _last_unload_freed_mb
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

        # v0.2.102 root-cause fix. The old code did ``_st_model = None`` then
        # ``empty_cache()`` and logged "freed 0 MB": the SentenceTransformer /
        # nn.Module object graph is CYCLIC, so dropping the one global left the
        # refcount above zero and the ~7.25 GB of tensors still allocated when
        # empty_cache ran. The fix is the ``gc.collect()`` BETWEEN the drop and
        # the measure — plus a weakref so we can recover the handle if the free
        # STILL does not happen (a live reference we did not find pins it).
        before = _cuda_memory_allocated()
        model_ref = weakref.ref(_st_model)
        _st_model = None       # drop the ONLY persistent strong reference
        gc.collect()           # break the reference cycles pinning the weights
        _empty_cuda_cache()    # return the freed blocks to the driver
        after = _cuda_memory_allocated()

        # None-check FIRST so every later operand is a narrowed float (this
        # restructure is also the pyright reportOptionalOperand fix — the old
        # tuple-conditional left ``before``/``after`` Optional at every use).
        if before is None or after is None:
            # cpu/mps or no CUDA stats — no VRAM to OOM on reload; gc.collect()
            # released what it could. Treat as unloaded. (C.2 observability.)
            _warn_if_model_object_survived(model_ref)
            logger.info(
                "Idle-unloaded GPU model after %.0fs idle (torch.cuda memory "
                "stats unavailable — cpu/mps backend or no CUDA)",
                now - _last_used,
            )
            return True

        freed = before - after
        _last_unload_freed_mb = max(0.0, freed) / 1e6
        floor_bytes = UNLOAD_FREED_MIN_MB * 1e6

        if before < floor_bytes:
            # Nothing substantial was allocated (a cpu model, or a test mock that
            # holds no real VRAM). gc.collect() dropped the object and there is
            # no second-copy OOM risk, so treat as unloaded without the freed
            # ratio check.
            _warn_if_model_object_survived(model_ref)
            logger.info(
                "Idle-unloaded GPU model after %.0fs idle — no substantial VRAM "
                "was held (%.0f MB allocated)",
                now - _last_used, before / 1e6,
            )
            return True

        if freed < before * _UNLOAD_FREED_RATIO:
            # Substantial VRAM was held but NOT freed — a live reference still
            # pins the weights. Recover the handle and STAY LOADED so the next
            # request reuses this copy instead of allocating a second one that
            # OOMs the GPU (the exact "freed 0 MB → reload → OutOfMemoryError"
            # defect). Name the situation loudly so the leak can be found.
            recovered = model_ref()
            if recovered is not None:
                _st_model = recovered
                logger.warning(
                    "Idle-unload freed only %.0f MB of %.0f MB allocated after "
                    "gc.collect() + empty_cache() — a live reference still holds "
                    "the model weights. KEEPING the model loaded (no second copy "
                    "will be allocated). Investigate what retains the "
                    "SentenceTransformer object.",
                    max(0.0, freed) / 1e6, before / 1e6,
                )
                return False
            # weakref died yet nothing was freed — unexpected (the object is gone
            # but the allocator still counts the bytes, e.g. it lags). We cannot
            # recover a handle and the memory is on its way out; treat as
            # unloaded but leave a WARNING so it is visible.
            logger.warning(
                "Idle-unload freed only %.0f MB of %.0f MB and the model object "
                "is already gone — the CUDA allocator may lag; treating as "
                "unloaded.",
                max(0.0, freed) / 1e6, before / 1e6,
            )
            return True

        logger.info(
            "Idle-unloaded GPU model after %.0fs idle — VRAM allocated %.0f MB → "
            "%.0f MB (freed %.0f MB; CUDA context residual persists until process "
            "exit)",
            now - _last_used, before / 1e6, after / 1e6, max(0.0, freed) / 1e6,
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


def _warn_if_model_object_survived(model_ref) -> None:
    """Observability for the unload arms whose free is UNMEASURABLE (no CUDA
    stats) or below the ``UNLOAD_FREED_MIN_MB`` floor (v0.2.102 r2, review nit):
    if the model OBJECT survived ``gc.collect()`` there, a live reference pins
    it. Those arms still drop the handle — they carry no VRAM-OOM risk (cpu/mps:
    host RAM; below-floor: < the floor by definition) and the unload contract
    pinned by the lifecycle tests is to drop it — but the WARNING is the honest
    signal that a reload would construct a second copy while the first lingers.
    Never raises (observability must not crash the idle timer)."""
    try:
        if model_ref() is not None:
            logger.warning(
                "Idle-unload: the model object SURVIVED gc.collect() — a live "
                "reference still holds it. Memory on this path is unmeasurable "
                "or below the %.0f MB floor, so the handle was dropped anyway; "
                "a reload constructs a second copy while this one lingers.",
                UNLOAD_FREED_MIN_MB,
            )
    except Exception:  # noqa: BLE001 — observability must never crash the timer
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
    # v0.2.101: the retrieval task key (chunking.QUERY_TASKS) so the service can
    # pick the right query instruction wording for its loaded model. Only
    # meaningful with is_query=True; None → the code-side default wording.
    task: str | None = None


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dim: int
    count: int
    backend: str
    model: str


async def _run_embed_guarded(
    texts: list[str], is_query: bool, task: str | None
) -> list[list[float]]:
    """Shared concurrency + error-mapping wrapper for BOTH embed endpoints (one
    concern, one home — the two endpoints differ only in response shape).

    B1 (v0.2.102 r2 — owner rule: concurrent requests QUEUE, none dropped):
    a request beyond the ``MAX_CONCURRENT`` execution slots WAITS for a slot,
    FIFO-fair (``asyncio.Semaphore`` wakes waiters in arrival order). The
    pre-v0.2.102 C-9 immediate 503 shed dropped request N+1 on arrival; waiting
    replaced rejecting. The wait is bounded ONLY by ``QUEUE_WAIT_SECS``
    (generous default, env-tunable); past that bound — and only past it — the
    request gets a 503 + ``Retry-After``. The C-9 intent stands: concurrency
    stays capped at ``MAX_CONCURRENT`` (the GPU is protected from unbounded
    parallel encodes).

    Admitted requests then serialise under ``_inference_lock``: a request that
    arrives while another is loading the model WAITS and reuses that single
    load (``_load_gpu_model`` is single-flighted, with a fail-fast cooldown
    after a failed budget so waiters share the outcome instead of each
    re-running minutes of retries) — never dropped, never a second load.

    A model load that exhausts its bounded CUDA-OOM budget raises
    ``_ModelLoadUnavailable``; an inference that OOMs raises torch's
    ``OutOfMemoryError``. BOTH are mapped to a clean 503 + ``Retry-After`` here —
    never a 500 with a traceback (v0.2.102). An ``HTTPException`` (e.g. the
    over-window 400 from ``_refuse_over_window``) propagates untouched.
    """
    global _in_flight
    sem = _get_semaphore()
    _in_flight += 1  # counts queued AND executing; /health reports it
    _stamp_used()  # v0.2.79 §C: mark activity so the idle-unload timer resets
    try:
        acquired = False

        if QUEUE_WAIT_SECS <= 0:
            # v0.2.103 (review R3 MEDIUM-4): decide WITHOUT a timer. The
            # old ~1 ms ``wait_for`` grace was a wall-clock race against OS
            # scheduling — under load the timer could mature before an
            # UNCONTENDED acquire completed, shedding a request that had a
            # free slot available (in production, not just in tests).
            # ``Semaphore.locked()`` is exact in this single-threaded loop:
            # True means every permit is taken → shed NOW (with
            # Retry-After); False means a permit is free, and the plain
            # ``await acquire()`` below then cannot block.
            if sem.locked():
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Service at capacity ({MAX_CONCURRENT} concurrent "
                        "request(s)) and no queue wait is configured "
                        "(CODE_EMBED_QUEUE_WAIT_SECS=0). Retry later."
                    ),
                    headers={"Retry-After": str(_queue_retry_after_secs())},
                )
            await sem.acquire()
            acquired = True
        else:
            # Bounded FIFO queue wait for one of the MAX_CONCURRENT slots.
            #
            # The acquire runs in its own TASK, not as a bare coroutine handed
            # to wait_for (v0.2.102 r3, re-review SHOULD-FIX): on Python 3.11
            # (the container base image) wait_for can raise TimeoutError for
            # a future that ALREADY consumed a permit in the same event-loop
            # step — the permit would be permanently leaked and effective
            # capacity would decay (4 → 3 → …) under exactly the sustained
            # saturation this queue exists for, with recovery only via
            # restart. The ``acquired`` flag lets the timeout path give a
            # raced permit BACK; the same shape also covers
            # client-disconnect cancellation mid-acquire. (Python 3.12
            # reimplemented wait_for over asyncio.timeout and is race-free,
            # but ``requires-python = ">=3.11"`` means the guard must hold
            # on both.)

            async def _acquire_slot() -> None:
                nonlocal acquired
                await sem.acquire()
                acquired = True

            try:
                await asyncio.wait_for(
                    asyncio.ensure_future(_acquire_slot()),
                    timeout=QUEUE_WAIT_SECS,
                )
            except asyncio.CancelledError:
                if acquired:
                    # The race: a permit was consumed despite the
                    # cancellation.
                    sem.release()
                raise
            except asyncio.TimeoutError:
                if acquired:
                    # The race: a permit was consumed despite the timeout —
                    # give it back so capacity does not decay.
                    sem.release()
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Service at capacity ({MAX_CONCURRENT} concurrent "
                        f"request(s)) and the queue wait of "
                        f"{QUEUE_WAIT_SECS:.0f}s was exceeded. Retry later."
                    ),
                    headers={"Retry-After": str(_queue_retry_after_secs())},
                )
        try:
            async with _get_lock():
                # Run sync inference in a thread pool to avoid blocking the event
                # loop; the lock ensures only one inference (and one model load)
                # runs at a time.
                loop = asyncio.get_event_loop()
                try:
                    return await loop.run_in_executor(
                        None, embed, texts, is_query, task
                    )
                except HTTPException:
                    raise  # e.g. the over-window 400 — propagate as-is
                except _ModelLoadUnavailable as exc:
                    raise HTTPException(
                        status_code=503,
                        detail=str(exc),
                        headers={
                            "Retry-After": str(exc.retry_after or _retry_after_secs())
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — map OOM, re-raise the rest
                    if _is_cuda_oom(exc):
                        # Wording kept backend-honest: ``_is_cuda_oom`` also
                        # matches a host-RAM "out of memory", so the detail
                        # must not claim CUDA specifically (review nit).
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "Out of memory during embedding — GPU VRAM or "
                                "host memory is occupied (e.g. by another "
                                "process). Retry later."
                            ),
                            headers={"Retry-After": str(_retry_after_secs())},
                        )
                    raise
        finally:
            sem.release()
    finally:
        _in_flight -= 1
        _stamp_used()  # re-stamp on completion — idle measured from last activity


@app.post("/embed", response_model=EmbedResponse)
async def embed_endpoint(req: EmbedRequest):
    if not req.texts:
        raise HTTPException(status_code=400, detail="texts list is empty")
    if len(req.texts) > 256:
        raise HTTPException(status_code=400, detail="max 256 texts per request")

    t0 = time.time()
    vecs = await _run_embed_guarded(req.texts, req.is_query, req.task)
    elapsed = time.time() - t0

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
    raw fallback hit, so it MUST behave identically under burst to ``/embed`` —
    pre-fix it had no capacity guard at all, so one burst made the two
    endpoints diverge (``/embed`` 503'd while this one blocked). Shares
    ``_run_embed_guarded`` (and therefore the bounded FIFO queue + the OOM→503
    mapping + the ``_in_flight`` counter).
    """
    if not req.prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    vecs = await _run_embed_guarded([req.prompt], False, None)
    return {"embedding": vecs[0]}


@app.get("/health")
async def health():
    try:
        # v0.2.79 §C / v0.2.102: for the gpu backend, do NOT force a model load
        # just to report ``dim`` — a liveness probe must not undo an idle-unload
        # NOR block the event loop on the single-flighted (possibly OOM-retrying)
        # load lock. Report the dim cached at first load; it is None until the
        # model has loaded, which is the honest answer for a probe.
        if BACKEND == "gpu":
            dim = _st_model_dim
            model_loaded = _st_model is not None
        else:
            dim = get_dim()  # ollama: cheap probe, holds no in-process weights
            model_loaded = True
        return {
            "status": "ok",
            "backend": BACKEND,
            "model": MODEL_NAME,
            "dim": dim,
            "device": DEVICE,
            "max_concurrent": MAX_CONCURRENT,
            # C-9: report the REAL in-flight count from the explicit counter.
            # v0.2.102 r2 semantics: requests INSIDE the endpoint — queued
            # waiting for a slot as well as executing.
            "in_flight": _in_flight,
            "inference_busy": _get_lock().locked(),
            # v0.2.79 §C: idle-unload observability.
            "model_loaded": model_loaded,
            "idle_unload_secs": IDLE_UNLOAD_SECS,
            # v0.2.102 r2: the bounded-queue bound (B1) — a client can compare
            # its own request timeout against it (it MUST exceed this).
            "queue_wait_secs": QUEUE_WAIT_SECS,
            # v0.2.102: did the last unload ACTUALLY free VRAM, and did the last
            # load fail? Cheap truthfulness for the "freed 0 MB" + OOM defects —
            # ``last_unload_freed_mb`` near 0 with ``model_loaded`` False was the
            # old broken shape; a non-null ``last_load_error`` names a live OOM.
            "last_unload_freed_mb": _last_unload_freed_mb,
            "last_load_error": _last_load_error,
            # v0.2.92 BLOCKER-1: image-vs-source staleness, made CHECKABLE.
            # This service ships as an image BUILT from the checkout, and
            # `compose up` builds only when the image is MISSING — so a
            # source fix can be live in git and absent from every running
            # install. `vco_lib.code_embed_image` compares this digest with
            # the checkout's; `vco doctor` reports the mismatch; install.py
            # passes `--build` when it is not provably current. A field that
            # is ABSENT (not null) identifies a pre-v0.2.92 image.
            "source_sha": SOURCE_SHA,
            "vco_lib_available": VCO_LIB_AVAILABLE,
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
        # Pre-load model on startup. If the bounded CUDA-OOM budget is exhausted
        # (another process holds the GPU), stay up and serve 503 + Retry-After on
        # /embed until VRAM frees and a request's lazy reload succeeds — never
        # crash-loop under ``restart: unless-stopped`` (v0.2.102). A NON-OOM load
        # fault still propagates (loud-fail on a genuinely broken config).
        try:
            _load_gpu_model()
        except _ModelLoadUnavailable as exc:
            logger.warning(
                "GPU model not loaded at startup: %s — serving 503 + Retry-After "
                "until a lazy reload succeeds.", exc,
            )
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
    # `_lib.update_gate` is SHIPPED on the HOST; import_lib_member LOUD-FAILS
    # if it is present-but-broken. The pre-fix silent
    # `exit_if_update_in_progress = None` stub disabled the mid-update
    # GPU-load guard on the exact broken-install path most likely to be
    # mid-update. When this service runs as a bare script (host process /
    # ensure-containers hook) sys.path[0] is the server's own dir, so the
    # parent (claude_mcp_servers/) must be inserted for `_lib` to resolve.
    from pathlib import Path as _Path
    _mcp_root = str(_Path(__file__).resolve().parent.parent)
    if _mcp_root not in sys.path:
        sys.path.insert(0, _mcp_root)
    #
    # v0.2.92: `_lib` lives in `claude_mcp_servers/`, which the Dockerfiles do
    # NOT copy — so in the container this import raised ModuleNotFoundError
    # and the image could not start at all. The gate reads a HOST marker file
    # the container cannot see, so it could never have fired there; on the
    # host-process path `_lib` resolves and the gate runs unchanged. Announce
    # the skip rather than failing (same reasoning as
    # `_configure_logging_for_environment`).
    try:
        from _lib.bootstrap import import_lib_member
    except ImportError:
        logger.warning(
            "claude_mcp_servers/_lib is not importable — running as the "
            "minimal containerized service image. The mid-update start gate "
            "is skipped; it reads a host marker file this container cannot "
            "see in any case."
        )
    else:
        exit_if_update_in_progress = import_lib_member(
            "update_gate", "exit_if_update_in_progress"
        )
        exit_if_update_in_progress("code-embedding service")

    # Detect import path: standalone (container) vs package (python -m ...)
    try:
        import claude_mcp_servers.code_embedding_service.server  # noqa: F401  # pyright: ignore[reportUnusedImport] — the import IS the probe
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
