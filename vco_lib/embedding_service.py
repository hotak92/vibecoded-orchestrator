# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Single source of truth for embedding dispatch across the orchestrator.

History (v0.2.18, 2026-05-18):

Before this module, every consumer read ``EMBEDDING_MODEL`` /
``ACTIVE_EMBEDDING`` env vars directly and hardcoded its own slot
name. ``templates/scripts/sync_knowledge_graph.py`` even had a
``RuntimeError`` for any ``ACTIVE_EMBEDDING != "qwen3"`` (KG-W1 audit
finding, 2026-04-30), which silently broke fresh installs that
install.py auto-selected for ``arctic`` or ``openai`` presets.

This module centralises everything:

  * **Catalogue discovery** — what models are reachable on the
    machine right now? Surfaces the answer to the GUI dropdown.
  * **Per-project slot resolution** — for a given project, which
    named-vector slot do we write to? (qwen3_embed / arctic2_embed /
    openai_text_embed / codesage_embed / openai_code_embed / ...)
  * **Single + batched embed calls** for both text (KG) and code
    (code graph), pooled through ONE ``requests.Session`` per
    instance so re-indexing loops don't re-establish TLS for every
    call.
  * **Multi-slot writes** — for enrichment migration, embed the same
    text into EVERY configured backend so the resulting object has
    every slot populated.
  * **Failure capture** — when zero backends are reachable, write a
    diagnostic to ``~/.claude/metrics/embedding_failures.jsonl`` AND
    a Claude-readable hint to ``.claude/context/EMBEDDING_FAILURES.md``
    so the user can ask Claude to investigate.

Design decisions (LOCKED, from v0.2.18 plan):

  1. **Per-project instance, NOT singleton** — concurrency + permissions
     isolation. Each project gets its own HTTP session, its own
     keyring resolution, its own validation cache.
  2. **Construction-time discovery** — backends are probed once when
     ``for_project()`` is called. Stale results are acceptable for
     short-lived processes (sync script ~minutes). Long-lived
     processes (MCP server) should re-construct periodically.
  3. **No silent fallback across embedding spaces** — if the user
     configured ``openai`` and OpenAI is down, we do NOT fall back
     to qwen3 (mixing 1536-dim and 1024-dim vectors in the same slot
     would corrupt search). Instead we raise.
  4. **Multi-slot writes are EXPLICIT** — ``embed_text`` produces
     ONE vector for the active slot; ``embed_text_all_configured``
     produces a dict of every-reachable-slot vectors. Callers pick.

API surface (locked):

    >>> from vco_lib.embedding_service import (
    ...     EmbeddingService, ModelChoice, NoEmbeddingBackendError
    ... )
    >>> svc = EmbeddingService.for_project()
    >>> svc.text_vector_slot
    'qwen3_embed'
    >>> svc.embed_text("hello")
    [0.012, -0.034, ...]
    >>> EmbeddingService.discover_text_models()
    [ModelChoice(id='qwen3-embedding:0.6b', label='qwen3-embedding (1024d)', ...)]

CLI entry point::

    python -m vco_lib.embedding_service discover

Prints a JSON catalogue suitable for the future Tauri
``get_embedding_catalog`` command (Commit 8).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Optional

import requests

from vco_lib.embedding_providers import (
    CodeEmbedAdapter,
    OllamaAdapter,
    OpenAIAdapter,
)
from vco_lib.embedding_providers.ollama import KNOWN_OLLAMA_DIMS
from vco_lib.embedding_providers.ollama_truncation import TruncationAwareOllamaAdapter
from vco_lib.embedding_providers.openai import (
    KNOWN_OPENAI_EMBEDDING_MODELS,
)
from vco_lib.paths import claude_metrics_dir

logger = logging.getLogger(__name__)

# ── v0.2.77 5c task 4: bounded 503 backoff-before-vectorless ──────────
#
# The code-embed FastAPI service sheds with HTTP 503 when its in-flight
# semaphore is saturated (a burst, e.g. an update-all fan-out) OR while a model
# (re)loads. Pre-v0.2.77 the retry was a SINGLE attempt (C-9); under the 5c
# update-all incident a saturating burst outlived one 2s retry and the embed
# call chain still failed → the analyzer degraded the object to VECTORLESS
# (embed_revision=0). A bounded exponential backoff rides out the burst window
# WITHOUT masking a genuinely-down backend (the schedule is finite; after the
# last delay a persistent 503 re-raises and the fail-safe vectorless-degrade
# path takes over exactly as before).
#
# Schedule: retries at 2s, 5s, 10s after the initial try (total ~17s of sleep
# < the 20s/object budget), each with a small +jitter to de-synchronise many
# concurrent objects hammering the service in lock-step. Module-level so the
# constants are visible + tunable in ONE place; the first delay is overridable
# via VCO_EMBED_503_RETRY_DELAY (back-compat with the C-9 knob — it SCALES the
# whole schedule proportionally so a "0" in tests makes every delay 0).
_EMBED_503_RETRY_BACKOFFS: "tuple[float, ...]" = (2.0, 5.0, 10.0)
# Jitter added to each delay: uniform [0, delay * _EMBED_503_JITTER_FRAC].
_EMBED_503_JITTER_FRAC = 0.25

__all__ = [
    "EmbeddingService",
    "ModelChoice",
    "NoEmbeddingBackendError",
    "TEXT_SLOT_MAP",
    "CODE_SLOT_MAP",
]


# ---------------------------------------------------------------------------
# Slot name maps. Single source of truth for "which model writes to which
# named-vector slot". Anything added here must also be added to the Weaviate
# schema in Commit 4 (vco_lib/weaviate_schema.py) — drift between this map
# and the schema means embeddings end up in the wrong slot, silently.
# ---------------------------------------------------------------------------

# Text models → KG named-vector slots. Keys are matched as substrings,
# case-insensitive, against the model id reported by the backend.
# Order matters: first match wins. Put more-specific names first.
TEXT_SLOT_MAP: tuple[tuple[str, str, int], ...] = (
    # (model_substring_lower, slot_name, dim)
    # OpenAI
    ("text-embedding-3-large", "openai_text_embed", 3072),
    ("text-embedding-3-small", "openai_text_embed", 1536),
    ("text-embedding-ada-002", "openai_text_embed", 1536),
    ("openai-", "openai_text_embed", 1536),
    # Arctic
    ("snowflake-arctic-embed2", "arctic2_embed", 1024),
    ("snowflake-arctic-embed-l-v2", "arctic2_embed", 1024),
    ("arctic-embed:l2", "arctic2_embed", 1024),
    ("arctic-embed2", "arctic2_embed", 1024),
    ("snowflake-arctic", "ollama_embed", 1024),  # legacy arctic
    ("arctic", "ollama_embed", 1024),            # legacy arctic
    # qwen3 (default)
    ("qwen3-embedding", "qwen3_embed", 1024),
    ("qwen3_embedding", "qwen3_embed", 1024),
    # Other Ollama models — fall into the legacy "ollama_embed" slot
    ("mxbai-embed", "ollama_embed", 1024),
    ("nomic-embed-text", "ollama_embed", 768),
)

# Code models → code-collection named-vector slots.
CODE_SLOT_MAP: tuple[tuple[str, str, int], ...] = (
    # OpenAI (forward-compat — OpenAI doesn't have a code-specific model
    # today, but the slot is reserved per the locked design decision)
    ("text-embedding-3-large", "openai_code_embed", 3072),
    ("text-embedding-3-small", "openai_code_embed", 1536),
    ("openai-", "openai_code_embed", 1536),
    # CodeSage (default GPU code embed)
    ("codesage-large-v2", "codesage_embed", 2048),
    ("codesage/codesage-large-v2", "codesage_embed", 2048),
    ("codesage-large", "codesage_embed", 2048),
    ("codesage", "codesage_embed", 2048),
    # Jina code (legacy)
    ("jina-embeddings-v2-base-code", "jina_embed", 768),
    ("jina-code", "jina_embed", 768),
    ("unclemusclez/jina-embeddings-v2-base-code", "jina_embed", 768),
    # Qwen3 fallback for code on CPU-only machines (no dedicated code
    # model — we reuse qwen3_embed slot for code too).
    ("qwen3-embedding", "qwen3_embed", 1024),
)


# Default fallback when a model name doesn't match any entry in the maps
# above. The unknown-text fallback IS the legacy ollama_embed slot
# because that's what pre-v0.2.18 collections wrote into; mapping there
# preserves searchability for un-recognised Ollama models the user has
# pulled themselves.
DEFAULT_TEXT_SLOT = ("ollama_embed", 1024)
DEFAULT_CODE_SLOT = ("ollama_code_embed", 768)


def _memo_key(text: str) -> str:
    """Cheap, collision-resistant fingerprint for the embed-memo cache.

    24 hex chars of sha256 = 96 bits of entropy → collision probability
    ~negligible up to ~10^14 distinct strings (well past the 512-entry
    LRU cap). Cheaper than a full sha256 hexdigest and dict-key-friendly.
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _resolve_text_slot(model_id: str) -> tuple[str, int]:
    """Map a model id to (slot_name, dim) using TEXT_SLOT_MAP."""
    lowered = model_id.lower()
    for substr, slot, dim in TEXT_SLOT_MAP:
        if substr in lowered:
            return slot, dim
    return DEFAULT_TEXT_SLOT


def _resolve_code_slot(model_id: str) -> tuple[str, int]:
    """Map a model id to (slot_name, dim) using CODE_SLOT_MAP."""
    lowered = model_id.lower()
    for substr, slot, dim in CODE_SLOT_MAP:
        if substr in lowered:
            return slot, dim
    return DEFAULT_CODE_SLOT


# ---------------------------------------------------------------------------
# OpenAI catalog-id ↔ API-model-name translation
# ---------------------------------------------------------------------------
#
# The GUI dropdown's source-of-truth identifier for an OpenAI model is the
# *prefixed* form (``"openai-text-embedding-3-small"``) — matches what
# `openai_cmd.rs::register_openai_api_key` writes to
# ``app_state.default_text_embedding`` and what install.py's
# ``_preset_to_default_models`` writes for the OpenAI preset. The pre-select
# logic in `KgCodegraphTab.svelte` compares ``app_state`` values to the
# catalog entry's ``id`` field by exact string equality; emitting the raw
# (un-prefixed) form here breaks that comparison silently.
#
# The OpenAI HTTP API, on the other hand, requires the RAW model name
# (``"text-embedding-3-small"``); passing the prefixed form to
# ``POST /v1/embeddings`` returns HTTP 400. So we translate at exactly two
# boundaries:
#
#   1. *Emission* — discover_text_models / discover_code_models pass the
#      raw model name from KNOWN_OPENAI_EMBEDDING_MODELS through
#      ``_to_openai_catalog_id`` before writing it to ``ModelChoice.id``.
#   2. *API call* — when something carrying a catalog id reaches the HTTP
#      layer (only happens in app_state-driven paths today, but the helper
#      is here for future call sites), ``_to_openai_api_model`` strips the
#      prefix back off.
#
# The raw form is also accepted as input to both helpers (idempotent) so
# legacy env-driven configs (``EMBEDDING_MODEL=text-embedding-3-small``)
# continue to round-trip cleanly without back-compat breaks.
OPENAI_MODEL_ID_PREFIX = "openai-"


def _to_openai_catalog_id(raw_model_name: str) -> str:
    """Convert an OpenAI raw model name to the GUI catalog id.

    Adds the ``openai-`` prefix unless one is already present. Idempotent.

    >>> _to_openai_catalog_id("text-embedding-3-small")
    'openai-text-embedding-3-small'
    >>> _to_openai_catalog_id("openai-text-embedding-3-small")
    'openai-text-embedding-3-small'
    """
    if raw_model_name.startswith(OPENAI_MODEL_ID_PREFIX):
        return raw_model_name
    return f"{OPENAI_MODEL_ID_PREFIX}{raw_model_name}"


def _to_openai_api_model(catalog_id: str) -> str:
    """Convert a GUI catalog id to the raw OpenAI API model name.

    Strips the ``openai-`` prefix if present. Idempotent for already-raw
    input — passes ``"text-embedding-3-small"`` through unchanged.

    Use this at any HTTP-call boundary where the input might be a
    catalog id (e.g. read from ``app_state.default_text_embedding``)
    rather than a raw env-driven model name.

    >>> _to_openai_api_model("openai-text-embedding-3-small")
    'text-embedding-3-small'
    >>> _to_openai_api_model("text-embedding-3-small")
    'text-embedding-3-small'
    """
    if catalog_id.startswith(OPENAI_MODEL_ID_PREFIX):
        return catalog_id[len(OPENAI_MODEL_ID_PREFIX):]
    return catalog_id


# ---------------------------------------------------------------------------
# Code-backend fallback chain (v0.2.18 correctness follow-up)
# ---------------------------------------------------------------------------
#
# Background: when ``for_project()`` resolves ``code_model_id`` to
# ``codesage-large-v2`` (the GPU-accelerated CodeEmbed default) but the
# FastAPI service is DOWN, every code-embed call subsequently routes to
# Ollama with model id ``codesage-large-v2`` — and Ollama doesn't have
# that model pulled. Net: every embed call raises RuntimeError.
#
# This fallback chain probes available backends at construction time
# and picks the FIRST reachable one. Order is locked by the v0.2.18
# plan (user direction 2026-05-19):
#
#   1. CodeEmbed FastAPI service (``/health`` → 200) — preferred,
#      GPU-accelerated, code-specific embeddings (codesage-large-v2,
#      2048-dim, slot ``codesage_embed``).
#   2. Ollama ``qwen3-embedding:0.6b`` — universal fallback. Every VCO
#      machine that has the KG also has qwen3 pulled, so reusing it
#      for code keeps code-graph working on every machine where the
#      KG works (1024-dim, slot ``qwen3_embed``).
#   3. Ollama ``unclemusclez/jina-embeddings-v2-base-code:latest`` —
#      code-specific Ollama fallback (auto-pulled by the
#      ``low_resource`` preset, but not by every preset, hence rank 3)
#      (768-dim, slot ``jina_embed``).
#
# OpenAI is handled separately by the caller — this function only
# probes local backends.

# Locked model id constants for the fallback chain. Kept here rather
# than buried in the function body so callers / tests can patch them
# in isolation.
_FALLBACK_QWEN3_MODEL = "qwen3-embedding:0.6b"
_FALLBACK_JINA_MODEL = "unclemusclez/jina-embeddings-v2-base-code:latest"


def _ollama_has_model(ollama: "OllamaAdapter", needle: str) -> bool:
    """Return True iff ``needle`` appears in Ollama's ``/api/tags`` list.

    Substring match, case-insensitive — handles tag variants like
    ``"qwen3-embedding:0.6b"`` vs ``"qwen3-embedding:latest"``. Soft-
    fail (returns False) on any HTTP error.
    """
    try:
        models = ollama.list_models()
    except Exception:  # pragma: no cover (defensive — adapter swallows)
        return False
    needle_lower = needle.lower()
    for m in models:
        name = str(m.get("name", "")).lower()
        if needle_lower in name or name.startswith(needle_lower.split(":")[0]):
            return True
    return False


def _resolve_code_model_with_fallback(
    *,
    requested_model_id: str,
    requested_slot: str,
    requested_dim: int,
    ollama: "OllamaAdapter",
    codeembed: "CodeEmbedAdapter",
) -> tuple[str, str, int, str]:
    """Resolve the code model + slot via the locked fallback chain.

    The chain only fires when the caller-resolved slot is
    ``codesage_embed`` — i.e. when the user intent is to use the
    GPU/CodeEmbed-service path. For any other slot (explicit
    ``jina_embed`` user override, CPU-fallback ``qwen3_embed``,
    ``openai_code_embed``), the requested triple is returned
    unchanged.

    Args:
        requested_model_id: Model id resolved by env-based logic in
            ``for_project()``.
        requested_slot: Named-vector slot the requested model maps to.
        requested_dim: Vector dim of the requested model.
        ollama: Adapter for probing Ollama ``/api/tags``.
        codeembed: Adapter for probing CodeEmbed ``/health``.

    Returns:
        ``(model_id, slot, dim, reason)`` — the first reachable backend
        in the locked chain, or the requested triple if nothing is
        reachable. ``reason`` is a human-readable string describing
        what was picked and why (empty when the requested codesage
        path is fully reachable, since that's the no-op case the user
        configured).
    """
    # Off-chain slots: don't second-guess the user's explicit choice.
    if requested_slot != "codesage_embed":
        return requested_model_id, requested_slot, requested_dim, ""

    # 1. CodeEmbed service: the preferred path. If it's up, we're done.
    if codeembed.is_reachable():
        # No fallback fired — caller will use the requested triple
        # and the existing routing logic will dispatch to CodeEmbed.
        return requested_model_id, requested_slot, requested_dim, ""

    # 2. Ollama qwen3-embedding:0.6b — universal fallback.
    if _ollama_has_model(ollama, _FALLBACK_QWEN3_MODEL):
        return (
            _FALLBACK_QWEN3_MODEL,
            "qwen3_embed",
            1024,
            (
                f"CodeEmbed service unreachable at {codeembed.base_url}; "
                f"using ollama:{_FALLBACK_QWEN3_MODEL} (slot=qwen3_embed)"
            ),
        )

    # 3. Ollama jina — code-specific Ollama fallback.
    if _ollama_has_model(ollama, _FALLBACK_JINA_MODEL):
        # Strip the ":latest" tag suffix for the resolver — the slot map
        # matches on the model family, not the tag.
        return (
            _FALLBACK_JINA_MODEL,
            "jina_embed",
            768,
            (
                "CodeEmbed + qwen3 both unavailable; "
                "using ollama:jina-embeddings-v2-base-code (slot=jina_embed)"
            ),
        )

    # 4. All down — return the requested triple. The caller's
    #    code_backend_ready() check will then correctly report False
    #    (CodeEmbed unreachable + Ollama lacks every fallback model),
    #    surfacing NoEmbeddingBackendError via the existing path.
    return (
        requested_model_id,
        requested_slot,
        requested_dim,
        "All code-embed backends unreachable; code embeddings will fail until one comes up",
    )


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelChoice:
    """One row in the catalogue dropdown.

    Attributes:
        id: Backend-specific model id (``"qwen3-embedding:0.6b"``,
            ``"text-embedding-3-small"``, ``"codesage-large-v2"``).
            This is the exact string the consumer sends to the backend.
        label: Human-readable label for the dropdown (includes dim hint
            and backend, e.g. ``"qwen3-embedding (1024d, Ollama)"``).
        dim: Vector dimension. 0 if unknown (model not in the static
            dim table and no cheap probe available).
        slot: Named-vector slot in Weaviate this model writes to
            (``"qwen3_embed"``, ``"openai_text_embed"``, ...).
        backend: ``"ollama"`` / ``"codeembed"`` / ``"openai"``.
        available_now: True if a probe at construction time found the
            backend reachable AND the model registered with it.
        reason_unavailable: Human-readable cause when ``available_now``
            is False (so the GUI dropdown can show a tooltip).
    """

    id: str
    label: str
    dim: int
    slot: str
    backend: str
    available_now: bool
    reason_unavailable: Optional[str] = None


class NoEmbeddingBackendError(RuntimeError):
    """Raised when no embedding backend is reachable.

    The construction of this exception triggers the failure-capture
    side-effects (writing the JSONL log + the EMBEDDING_FAILURES.md
    hint). Catching and re-raising does NOT re-trigger them — capture
    happens exactly once per exception instance.

    Attributes:
        attempted_backends: List of backend ids we tried
            (``["ollama", "codeembed", "openai"]``).
        error_per_backend: Map of backend id → human-readable cause.
        install_root: Project root used during the failed
            ``for_project()`` call (None for module-level discovery).
    """

    def __init__(
        self,
        message: str,
        *,
        attempted_backends: list[str] | None = None,
        error_per_backend: dict[str, str] | None = None,
        install_root: Path | None = None,
        env_snapshot: dict[str, str] | None = None,
        capture: bool = True,
    ) -> None:
        super().__init__(message)
        self.attempted_backends = list(attempted_backends or [])
        self.error_per_backend = dict(error_per_backend or {})
        self.install_root = install_root
        self.env_snapshot = dict(env_snapshot or {})
        if capture:
            _write_failure_jsonl(self)
            _write_failure_markdown(self)
            _write_failure_deferral(self)


# ---------------------------------------------------------------------------
# Failure capture
# ---------------------------------------------------------------------------

# Env vars whose values are safe to capture verbatim. Anything else
# (notably OPENAI_API_KEY) gets redacted before serialisation.
_SAFE_ENV_KEYS: tuple[str, ...] = (
    "OLLAMA_URL",
    "CODE_EMBED_SERVICE_URL",
    "CODE_EMBED_BACKEND",
    "CODE_EMBED_MODEL",
    "EMBEDDING_MODEL",
    "ACTIVE_EMBEDDING",
    "DUAL_EMBEDDING_ENABLED",
    "KG_COLLECTION",
    "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    "PROJECT_NAME",
    "WEAVIATE_URL",
)


def _redacted_env_snapshot() -> dict[str, str]:
    """Snapshot relevant env vars for the failure log, redacting secrets."""
    out: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        val = os.environ.get(key, "")
        if val:
            out[key] = val
    # Redact: present-but-truncated to avoid full-secret leakage.
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        # First 4 chars + length, so a maintainer can sanity-check the prefix
        # ("sk-p" vs "sk-") without exposing the secret in plain text.
        out["OPENAI_API_KEY"] = f"<redacted prefix={api_key[:4]!r} len={len(api_key)}>"
    else:
        out["OPENAI_API_KEY"] = "<unset>"
    return out


def _failure_jsonl_path() -> Path:
    """The embedding-failure jsonl, resolved — NOT a fixed path.

    It is NO LONGER under ``~/.claude``: v0.2.92 W7 moved the metrics home to
    ``<vct_root_dir()>/metrics`` and ``paths.claude_metrics_dir`` became a
    deprecated ALIAS that moved with it. This docstring used to name the old
    location, and four shipped scripts copied that string into messages they
    printed at users — pointing them at a file the rows are not in. Interpolate
    this function; never restate the path.

    Routed through :func:`vco_lib.paths.claude_metrics_dir` rather than
    reconstructing ``Path.home() / ".claude"`` inline, because inline was
    unsteerable: with no override anywhere in the chain, the test suite's
    fixture failures appended to the maintainer's REAL telemetry stream on
    every local ``pytest tests/`` (v0.2.92 W-CLAUDE; rows identifiable by
    ``"attempted_backends": []`` / ``"install_root": null``). Import is
    hard — a failing ``vco_lib`` import means a broken install, and this
    module is itself ``vco_lib``.
    """
    return claude_metrics_dir() / "embedding_failures.jsonl"


def _failure_markdown_path(install_root: Path | None) -> Path | None:
    """``<install_root>/.claude/context/EMBEDDING_FAILURES.md``.

    Returns None if ``install_root`` is None (module-level discovery
    failures have no project to write the hint into).
    """
    if install_root is None:
        return None
    return install_root / ".claude" / "context" / "EMBEDDING_FAILURES.md"


def _write_failure_jsonl(exc: NoEmbeddingBackendError) -> None:
    """Append one JSON line per failure to ``~/.claude/metrics/...``.

    Soft-fail: any IO error here is logged but does not propagate
    (we're already inside an error path; don't make it worse).
    """
    path = _failure_jsonl_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "install_root": str(exc.install_root) if exc.install_root else None,
            "attempted_backends": exc.attempted_backends,
            "error_per_backend": exc.error_per_backend,
            "env_snapshot": exc.env_snapshot or _redacted_env_snapshot(),
            "message": str(exc),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as e:
        logger.warning("Failed to write embedding failure JSONL: %s", e)


_MD_TEMPLATE = """# Embedding backend failure

**Timestamp**: {ts}
**Install root**: `{install_root}`
**Attempted backends**: {backends}

## Per-backend errors

{per_backend}

## What this means

`EmbeddingService.for_project()` tried every embedding backend
configured for this install and none of them were reachable. Until at
least one backend comes back online, KG syncs / code-graph indexing /
search calls that require fresh vectors will fail or be skipped.

## How Claude can help

Ask Claude to investigate the detailed failure log at:

  `{jsonl_path}`

The log lists every attempt with the redacted env snapshot. Claude can
read it, diagnose which service is down (Ollama not running, CodeEmbed
container OOM'd, OpenAI key revoked, etc.), and walk you through the
fix.

This file is auto-cleared the next time
`EmbeddingService.for_project()` succeeds.
"""


def _write_failure_markdown(exc: NoEmbeddingBackendError) -> None:
    """Write the Claude-readable hint file (soft-fail).

    Skips when ``install_root`` is None (no project context).
    """
    path = _failure_markdown_path(exc.install_root)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        per_backend_lines: list[str] = []
        if exc.error_per_backend:
            for backend, msg in sorted(exc.error_per_backend.items()):
                per_backend_lines.append(f"- **{backend}**: {msg}")
        else:
            per_backend_lines.append("- (no per-backend errors recorded)")
        content = _MD_TEMPLATE.format(
            ts=datetime.now(timezone.utc).isoformat(),
            install_root=str(exc.install_root) if exc.install_root else "(unknown)",
            backends=", ".join(exc.attempted_backends) or "(none)",
            per_backend="\n".join(per_backend_lines),
            jsonl_path=_failure_jsonl_path(),
        )
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write embedding failure markdown: %s", e)


def _clear_failure_markdown(install_root: Path | None) -> None:
    """Remove the EMBEDDING_FAILURES.md hint after a successful construction.

    No-op if the file or its directory doesn't exist. Soft-fail on IO.
    """
    path = _failure_markdown_path(install_root)
    if path is None or not path.exists():
        return
    try:
        path.unlink()
    except OSError as e:
        logger.debug("Failed to clear embedding failure markdown: %s", e)


# ---------------------------------------------------------------------------
# Deferral integration (v0.2.18 Commit 11 / observability)
#
# Reuses the existing DeferralReport / UPDATE_DEFERRED.md mechanism so the
# failure shows up on the launcher's GUI deferral banner alongside any
# other unresolved install actions. The launcher reads UPDATE_DEFERRED.md
# and surfaces the same condition_id; CLAUDE.md gets a wrapped reminder
# block injected automatically via DeferralReport.write().
#
# Soft-fail throughout — deferral_report is part of vco_lib but the import
# is local to the function so a circular-import or partial-install state
# can't break the embedding failure path (we're already in a failure
# branch; don't compound it).
# ---------------------------------------------------------------------------

_DEFERRAL_CONDITION_ID = "kg_summary_no_backend"


def _write_failure_deferral(exc: "NoEmbeddingBackendError") -> None:
    """Write/refresh a ``kg_summary_no_backend`` entry in UPDATE_DEFERRED.md.

    The entry points at the JSONL log + the .md hint and lists the exact
    backends probed. This is the third surface (alongside the JSONL log
    + the EMBEDDING_FAILURES.md hint) so the GUI deferral banner can pick
    up the failure without reading our private files.

    Skips when ``install_root`` is None (no project to write into).
    Soft-fail on any error — never propagates.
    """
    if exc.install_root is None:
        return
    try:
        # Local import: avoid circular-import risk if deferral_emit ever
        # imports from embedding_service (it doesn't today, but the import
        # is cheap and the safety margin is worth it on the error path).
        # v0.2.83 (WP-B1): routed through the ONE emitter home
        # (vco_lib.deferral_emit) — locked read-modify-write.
        from vco_lib.deferral_emit import DeferralEntry, emit

        backends = ", ".join(exc.attempted_backends) or "(none)"
        per_backend_lines: list[str] = []
        for backend, msg in sorted(exc.error_per_backend.items()):
            per_backend_lines.append(f"- {backend}: {msg}")
        per_backend_block = "\n".join(per_backend_lines) or "(no per-backend errors recorded)"

        detected = (
            f"EmbeddingService.for_project() found no reachable backend. "
            f"Backends probed: {backends}. "
            f"Per-backend errors: {per_backend_block}"
        )
        why_deferred = (
            "Cannot auto-fix: the user must bring up a local backend "
            "(Ollama / CodeEmbed) or configure OPENAI_API_KEY. KG syncs "
            "and code-graph indexing that require fresh vectors are "
            "blocked until at least one backend comes back online."
        )
        command_to_apply = (
            "bash claude_mcp_servers/start-all.sh   "
            "# OR: launch Ollama via podman/docker, OR: set OPENAI_API_KEY"
        )
        hint_md = _failure_markdown_path(exc.install_root)
        kg_refs: list[str] = []
        if hint_md is not None:
            try:
                kg_refs.append(str(hint_md.relative_to(exc.install_root)))
            except ValueError:
                kg_refs.append(str(hint_md))
        kg_refs.append(str(_failure_jsonl_path()))

        emit(
            exc.install_root,
            DeferralEntry(
                condition_id=_DEFERRAL_CONDITION_ID,
                title="Embedding backend unreachable; KG seed deferred",
                detected=detected,
                why_deferred=why_deferred,
                command_to_apply=command_to_apply,
                severity="warning",
                kg_node_refs=kg_refs,
            ),
            log=logger,
        )
    except Exception as e:  # noqa: BLE001 — soft-fail on the error path
        logger.warning("Failed to write embedding failure deferral entry: %s", e)


def _clear_failure_deferral(install_root: Path | None) -> None:
    """Mark the ``kg_summary_no_backend`` entry resolved (paired with success).

    No-op if there's no deferral file or no matching entry. Soft-fail.
    """
    if install_root is None:
        return
    try:
        # v0.2.83 (WP-B1): resolve through the ONE emitter home — the locked
        # read-modify-write reads the current report, tombstones + drops the
        # condition, and writes once (deleting the file when empty).
        # resolve_conditions is a safe no-op when the entry isn't present.
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(install_root, (_DEFERRAL_CONDITION_ID,), log=logger)
    except Exception as e:  # noqa: BLE001 — soft-fail; success path must not fail
        logger.debug("Failed to clear embedding failure deferral entry: %s", e)


# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------


def _detect_project_root(explicit: Path | None = None) -> Path | None:
    """Resolve the install_root used for failure-capture and config.

    Resolution order:

      1. Explicit ``project_root`` arg to ``for_project()``.
      2. ``KG_BASE_DIR`` env var (set by VS Code extension; equivalent
         to the workspace root).
      3. ``VCT_ORCHESTRATOR_ROOT`` env var (set by the launcher when
         spawning the bundled scripts).
      4. ``Path.cwd()`` if it contains a ``.claude/`` directory
         (heuristic — the current dir IS a VCO project).
      5. None — caller is responsible for handling "no project context"
         (currently means the failure-markdown hint isn't written).
    """
    if explicit is not None:
        return Path(explicit).resolve()

    for env_var in ("KG_BASE_DIR", "VCT_ORCHESTRATOR_ROOT"):
        v = os.environ.get(env_var, "").strip()
        if v:
            p = Path(v).resolve()
            if p.exists():
                return p

    cwd = Path.cwd().resolve()
    if (cwd / ".claude").is_dir():
        return cwd
    return None


# ---------------------------------------------------------------------------
# EmbeddingService
# ---------------------------------------------------------------------------


# Default backend-URL constants. These mirror the values in install.py
# and the MCP server — keeping them in one place avoids the historical
# fragmentation that motivated this whole refactor.
DEFAULT_OLLAMA_URL = "http://localhost:11435"
#: Kept for callers that import it; the RESOLUTION of the code-embed base URL
#: now lives in ``vco_lib.code_embed_image.service_base_url`` (see
#: ``_shared_service_base_url`` below), which also honours ``CODE_EMBED_PORT``.
DEFAULT_CODE_EMBED_URL = "http://localhost:11440"
DEFAULT_TEXT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_CODE_MODEL = "codesage-large-v2"


def _shared_service_base_url(explicit: "str | None" = None) -> str:
    """The code-embed base URL, from the ONE shared resolver.

    Imported lazily and wrapped in a thin function so the two call sites in
    this module name a single thing (per the one-concern-one-home rule) rather
    than repeating an import + call. ``code_embed_image`` imports only stdlib
    at module level, so there is no cycle back into this module.
    """
    from vco_lib.code_embed_image import service_base_url

    return service_base_url(explicit)


# v0.2.69 FIX 3: per-embed-REQUEST timeout (the correct granularity).
#
# Background: install.py used to wrap the WHOLE sync_knowledge_graph.py
# subprocess in a per-PROCESS timeout (600s / 900s). Those fired on
# legitimate slow re-embeds — a snowflake-arctic re-embed on a cold CPU
# can take far longer than any whole-process cap we'd pick, and killing
# it mid-seed strands the user. Per the maintainer ruling, there is NO
# per-process timeout on install/seed; the only guard is at CHUNK
# granularity — i.e. one HTTP embed request for one chunk.
#
# This timeout bounds a SINGLE embed request. A genuinely-wedged embedder
# (hung socket, dead container holding the connection) fails within the
# cap instead of hanging forever; a slow-but-progressing one — where each
# chunk completes under the cap — runs to completion no matter how many
# chunks there are. Default 180s is ~6x the observed ~30s/chunk boundary
# for arctic-on-CPU, so legitimate chunks never trip it. Override via
# ``VCT_EMBED_REQUEST_TIMEOUT_SECS`` when hardware is unusually slow (or
# to tighten it on fast machines). Applies to every embed backend
# (Ollama / CodeEmbed / OpenAI) — the value is threaded into each adapter
# at construction.
DEFAULT_EMBED_REQUEST_TIMEOUT_SECS = 180.0
EMBED_REQUEST_TIMEOUT_ENV = "VCT_EMBED_REQUEST_TIMEOUT_SECS"


def _resolve_embed_request_timeout() -> float:
    """Return the per-embed-request timeout in seconds.

    Reads ``VCT_EMBED_REQUEST_TIMEOUT_SECS`` (a positive number of
    seconds); falls back to :data:`DEFAULT_EMBED_REQUEST_TIMEOUT_SECS`
    when the var is unset, empty, non-numeric, or non-positive. A
    non-positive or garbage value is treated as "use the default"
    rather than disabling the guard, because an unbounded embed request
    is exactly the wedge this fix exists to prevent.

    Returns:
        A positive float — the ``timeout=`` value passed to every embed
        HTTP call.
    """
    raw = os.environ.get(EMBED_REQUEST_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_EMBED_REQUEST_TIMEOUT_SECS
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a number; using default %.0fs",
            EMBED_REQUEST_TIMEOUT_ENV,
            raw,
            DEFAULT_EMBED_REQUEST_TIMEOUT_SECS,
        )
        return DEFAULT_EMBED_REQUEST_TIMEOUT_SECS
    if val <= 0:
        logger.warning(
            "%s=%r is not positive; using default %.0fs (an unbounded "
            "embed request is the wedge this guard prevents)",
            EMBED_REQUEST_TIMEOUT_ENV,
            raw,
            DEFAULT_EMBED_REQUEST_TIMEOUT_SECS,
        )
        return DEFAULT_EMBED_REQUEST_TIMEOUT_SECS
    return val


# v0.2.71 Piece 5c — second-slot enrichment write toggle (default OFF).
#
# THE COST (audit `update-all-kg-reembed-serialization-2026-06-30.md` §5):
# ``embed_text_all_configured`` / ``embed_code_all_configured`` populate the
# ACTIVE named slot PLUS every other reachable backend's slot (qwen3, openai,
# codesage). On an arctic-active install with Ollama up that means TWO embed
# calls per write (arctic2_embed + qwen3_embed) — the "doubling" multiplier
# the tester saw, compounding the concurrency problem under contention.
#
# THE FEATURE IT SERVES (user, 2026-06-30): the second slot exists so a later
# model SWITCH (qwen3 → openai, arctic → qwen3) doesn't require a full
# re-embed — the destination slot is already populated. The user also wants
# the option to populate BOTH slots so BOTH the arctic AND qwen3 RL-module
# neural nets can have their embedding spaces filled. It is a REAL feature,
# not waste — but it doubles embed cost, so per user decision it is now
# **opt-in, DEFAULT OFF**.
#
# WHY A DEDICATED FLAG (not flipping ``DUAL_EMBEDDING_ENABLED``): in the MCP
# server + sync scripts, ``DUAL_EMBEDDING_ENABLED`` ALSO selects named-vector
# schema/read/write. Flipping ITS default to false would make searches stop
# passing ``target_vector`` (server.py:7108) and make writes emit a FLAT
# vector into a named-vector collection (sync_knowledge_graph.py
# ``_build_vector_arg`` legacy branch + collection.data.insert), BREAKING
# reads and writes on every existing install. This flag isolates ONLY the
# second-slot WRITE fan-out: the active named slot is ALWAYS written (so reads
# and existing dual data stay queryable), the SECONDARY slots are written only
# when this is explicitly enabled. Default OFF = the cost saving; set to true
# to keep the multi-slot model-switch-without-re-embed + dual-net enrichment.
#
# Opt-in: ``DUAL_EMBEDDING_WRITE_ALL_SLOTS=true``.
DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV = "DUAL_EMBEDDING_WRITE_ALL_SLOTS"


def _resolve_write_all_slots() -> bool:
    """Return whether to write the SECONDARY enrichment slots on each embed.

    Default FALSE (v0.2.71 Piece 5c — opt-in). The active slot is always
    written regardless; this only controls the qwen3/openai/codesage
    secondary fan-out. Any value other than a truthy string ("1"/"true"/
    "yes"/"on", case-insensitive) resolves to False.
    """
    raw = os.environ.get(DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── WP-O (2026-07-22): arctic as a locally-served SECONDARY text slot ─────────
#
# R2-5 gap: the secondary text fan-out was qwen3 + openai ONLY, so a
# qwen3-ACTIVE install could never populate the ``arctic2_embed`` named slot —
# the user's arctic RL corpus stayed empty on their own machine (arctic only
# ever appeared when it was the ACTIVE model). The user ruling: the dual-model
# logging component EXISTS; make arctic work as the configured secondary rather
# than forcing an ACTIVE switch.
#
# This adds a SECOND opt-in flag layered on top of ``DUAL_EMBEDDING_WRITE_ALL_SLOTS``:
# arctic is fanned out to ``arctic2_embed`` (via Ollama
# ``snowflake-arctic-embed2:latest``, 1024-dim) as a secondary ONLY WHEN:
#
#   1. ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` is on (the master secondary gate — no
#      secondary is ever written without it), AND
#   2. ``DUAL_EMBEDDING_ARCTIC_SECONDARY`` is on (this flag), AND
#   3. arctic is NOT already the ACTIVE slot (no self-duplication).
#
# Why a DEDICATED flag rather than "arctic is always a secondary under write-all":
# arctic is a locally-served Ollama model, so on a machine that has it pulled the
# fan-out would double every KG write's embed cost silently the moment write-all
# flips on. openai-secondary is self-gating (needs a key); qwen3-secondary is the
# default-always-present model. arctic is neither — a user who wants the qwen3
# corpus but not the arctic one must be able to keep it off. Default OFF; the
# arctic corpus is opt-in exactly like the master write-all fan-out.
#
# The model id is resolvable via ``LEGACY_TEXT_EMBEDDING_MODEL`` in the MCP layer
# for back-compat, but the canonical secondary-fan-out constant lives HERE (the
# SSOT embedding layer). It maps to the ``arctic2_embed`` slot via TEXT_SLOT_MAP
# (``snowflake-arctic-embed2`` → ``arctic2_embed``, 1024) — the SAME slot the
# arctic-ACTIVE path and the dual-log other-slot resolver already use, so a
# secondary-written arctic vector is byte-space-identical to an active one.
DUAL_EMBEDDING_ARCTIC_SECONDARY_ENV = "DUAL_EMBEDDING_ARCTIC_SECONDARY"

# Canonical Ollama model id for the arctic secondary slot. Matches
# ``_model_id_for_active("arctic")`` and the MCP layer's
# ``LEGACY_TEXT_EMBEDDING_MODEL`` default so every arctic embed — active,
# secondary, or dual-log re-embed — hits the identical backend + num_ctx.
ARCTIC_SECONDARY_MODEL = "snowflake-arctic-embed2:latest"


def _resolve_arctic_secondary() -> bool:
    """Return whether to fan out an arctic ``arctic2_embed`` SECONDARY slot.

    Default FALSE (opt-in). This is a SECOND-layer gate: it only has any effect
    when ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` is ALSO on (the master secondary gate)
    AND arctic is not already the active slot. Any value other than a truthy
    string ("1"/"true"/"yes"/"on", case-insensitive) resolves to False.

    See ``DUAL_EMBEDDING_ARCTIC_SECONDARY_ENV`` for the full rationale.
    """
    raw = os.environ.get(DUAL_EMBEDDING_ARCTIC_SECONDARY_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── WP-O rework (2026-07-22, no-functionality-loss rule) ──────────────────────
#
# STANDING RULE: the ACTIVE slot's chunk fidelity must NEVER drop below the
# single-write baseline. A dual-write install must produce active-slot data
# byte-≥-identical to a single-write install. So chunk boundaries follow the
# ACTIVE model's OWN preset (unclamped) — the min-across-slots clamp that the
# G5/fix-r2 pass introduced is REMOVED from active-slot sizing (it degraded qwen3
# to arctic's 4 096 tier). The SECONDARY slot is the degradable one.
#
# DEGRADATION MECHANISM (tagged, secondary-only): when a chunk exceeds a
# secondary Ollama model's num_ctx, we embed a BOUNDED LEADING SUB-WINDOW of the
# text (the model's own num_ctx worth) rather than handing the full chunk to
# Ollama and letting it SILENTLY truncate at num_ctx. The sub-window boundary is
# EXPLICIT and the fact is reported to the caller as ``truncated=True``. The KG
# write path (``store_knowledge_node``) reads the per-call truncated set via
# ``embed_text_all_configured_tagged`` and PERSISTS it on THREE surfaces:
#   1. the ``truncated_slots`` Weaviate chunk property (v0.2.92, m-R6-1) — the
#      COMPLETE per-call record: every configured slot, the ACTIVE one
#      included, whose vector was embedded from a bounded leading sub-window.
#      Its PRESENCE is the era marker a reader uses to tell "this row records
#      all slots" from "this row records only secondaries" — see
#      ``rl_enrichment.TRUNCATED_SLOTS_PROP``.
#   2. the ``secondary_truncated_slots`` Weaviate chunk property (R3-2) — the
#      SECONDARY-only view, derived from the SAME single capture by dropping
#      the active slot, byte-identical to what R3-2 wrote. A truncated arctic
#      vector is no longer indistinguishable from a full-fidelity one in the
#      vector DB; and
#   3. the per-node ``emb_truncated`` field on the v4 RL retrieval events
#      (rl_logger schema v4) — the surface the TRAINER reads (launcher.db
#      ``rl_events``) — on BOTH the dual-log (other-slot) event AND, since
#      v0.2.92, the MAIN (active-slot) event, so per-model dataset assembly
#      can partition truncated-vs-full vectors from the RL event ALONE, with
#      no Weaviate join. The field is tri-state on the wire: true / false
#      when known, ABSENT (= unknown) for pre-v4 events, on-the-fly
#      backfilled vectors, and rows/events that predate the state they are
#      asked for — see ``rl_logger.resolve_emb_truncation_state``.
#      Absence must never be read as "not truncated".
# This is strictly better than the silent-Ollama-truncation status quo (same
# coverage, now labelled AND persisted). The ACTIVE slot is no longer
# guaranteed full-fidelity: since round-5 its shrink-on-refusal is recorded in
# the SAME per-call record as the secondaries' bounding, so the active fact is
# persisted like the secondary fact instead of living in a WARNING log.
#
# We approximate the num_ctx-worth of text by a CHARACTER budget derived from
# num_ctx. v0.2.92 defect-1 fix (2026-09-04): the budget uses PER-MODEL
# MEASURED-MINIMUM chars/token ratios — NOT the chunker's model-agnostic
# ``CHARS_PER_TOKEN_TEXT`` (4), which is a chunking unit and over-counts the
# chars a dense tokenizer actually fits (arctic measured as low as 2.30
# chars/token, so a "4 096-token" 16 384-char budget was really 4 158-7 124
# true tokens against a 4 096 window). The MINIMUM is used, not the median,
# because the minimum is what overflows. A 25% safety margin (user-set
# 2026-09-04; do NOT widen it without a new measurement) is then taken OFF the
# top: char_budget = num_ctx × ratio_min × (1 − 0.25).
#
# Measured real-content ratios behind the table (defect-1 investigation,
# 2026-09-04): snowflake-arctic-embed2 min/median/max = 2.30 / 3.195 / 3.87;
# qwen3-embedding = 2.547 / 3.977 / 4.669.
#
# ONE HOME for these ratios: this table. A drifted duplicate token table
# already caused a live 4x over-budget bug this cycle — do not copy these
# numbers anywhere else. This is DELIBERATELY a different table from
# ``chunking.CHARS_PER_TOKEN_TEXT`` (frozen, the chunk-boundary unit): the
# chunker sizes chunk boundaries; this table bounds what is HANDED to a
# secondary embedder, and the two must not be coupled.
#
# Unmeasured-but-registered models (bge-m3, text-embedding-3-small, …) get the
# measured FLOOR (the smallest ratio measured across the shipped models): an
# unmeasured tokenizer is a genuine unknown, and under-filling a window is a
# fidelity cost while over-filling it is the silent truncation this bound
# exists to prevent.
# MEDIAN, not minimum — and the reason is measured, not preferential.
#
# Declaring the measured MINIMUM ratio makes the bound safe for the densest
# conceivable chunk, but it truncates everything else far too early: on a real
# project corpus the minimum-ratio bound truncated 312 of 859 chunks (36.3%),
# against 40 (4.7%) before — roughly 300 chunks losing text that would have
# fitted the window comfortably. For the SECONDARY slot, whose whole purpose is
# to be a faithful training corpus, that is a large self-inflicted loss.
#
# Declaring the MEDIAN is safe because a denser-than-median chunk is not lost
# silently: every embed sends `truncate: false`, so the runner REFUSES it, and
# the caller shrinks and retries until it fits
# (`_embed_secondary_with_refusal_retry`).
#
# CORRECTED (round-3/4). This block previously claimed truncation was "detected
# EXACTLY" from `prompt_eval_count` pinning at `num_ctx`, and concluded "so
# there is no retry". Both halves were wrong:
#   * a pinned count means the window was FILLED — an exact fit and a
#     truncation produce the identical number, and under `truncate: false` an
#     over-window input never returns 200 at all, so the pinned case is almost
#     always an exact FIT. Reporting it as truncation flagged the inputs that
#     fit best;
#   * there IS a retry, and there had to be: without one, `truncate: false`
#     turned an over-window chunk into a refusal that was logged and dropped —
#     no vector at all, strictly worse than the truncated-and-tagged vector it
#     replaced.
# Truncation is now known LOCALLY — we tagged it because we sent less than we
# were given — not inferred from the response.
#
# TIERS, because the runner REFUSES rather than truncates (measured
# 2026-09-04, this Ollama build):
#   /api/embed      over-window -> HTTP 400 {"error":"the input length exceeds
#                                            the context length"}
#   /api/embeddings over-window -> HTTP 500, SAME message (legacy endpoint)
# A refusal is worse than a truncation: the soft-fail logs a warning and
# persists NOTHING, so the secondary slot gets no vector at all for that chunk.
#
# Tier 1 (MEDIAN) is what we ATTEMPT: it lets typical content embed in full.
# (The cost of declaring the minimum instead is measured in the block above.)
# Tier 2 (MIN) is the FIRST shrink on a refusal. It is NOT a floor, and an
# earlier version of this comment wrongly claimed it was "provably <= num_ctx,
# so it cannot be refused again". That arithmetic used a minimum ratio measured
# on PROSE (2.30 chars/token). Real KG content is much denser — markdown tables
# 1.41, box-drawing 1.61, CJK 1.35 — and refuses at this tier too. With only
# two attempts the second refusal propagated and the slot got NO vector, which
# is the failure the retry exists to prevent, on the dominant non-prose
# content.
#
# So the retry does not STOP at tier 2; it continues by HALVING until the
# runner accepts or reaches `_EMBED_RETRY_FLOOR_CHARS`. That terminates for any
# density, because each step strictly shrinks and the floor cannot exceed any
# supported window. The guarantee comes from the loop, not from a constant.
#
# Tier 2 earns its place by being the LARGEST useful first shrink, not by being
# safe. Halving straight from the attempt over-shrinks exactly the content the
# MIN ratio was measured on: arctic refused at 9 815 would go to 4 907 chars
# (~2 133 tokens at 2.30) when 7 065 (~3 072 tokens) is accepted — a third of
# the window given up. `_shrink_step` therefore takes the larger of {MIN tier,
# half} that is strictly below the current attempt, and once the attempt is at
# or below the MIN tier the sequence is plain halving.
_EMBED_BOUND_CHARS_PER_TOKEN_ATTEMPT: dict[str, float] = {
    "snowflake-arctic-embed2": 3.195,  # measured MEDIAN, real content
    "qwen3-embedding": 3.977,          # measured MEDIAN, real content
    # W1 (2026-09-05): measured with the CodeSage tokenizer inside the
    # code-embed container on real repo Python — budget-window slices
    # aggregate 3.46-3.48 chars/token. Sizes the CODE legs' attempt budget
    # and the first-shrink tier now that the CodeEmbed service leg shrinks.
    "codesage": 3.46,
}
_EMBED_BOUND_MIN_CHARS_PER_TOKEN: dict[str, float] = {
    # Measured MINIMUM over the sampled corpus — the first shrink tier, NOT a
    # proof. Denser content exists (see the block above); the halving loop, not
    # this number, is what guarantees termination.
    "snowflake-arctic-embed2": 2.30,
    "qwen3-embedding": 2.547,
    # Densest budget-window slice observed on the same W1 measurement
    # (the head of a dense module: 3 584 chars -> 1 193 tokens).
    "codesage": 2.96,
}
# First-shrink ratio for models with no measurement: stay on the low side for
# the unknown. Like the table above, a starting point for the loop, not a bound.
_EMBED_BOUND_MIN_CHARS_PER_TOKEN_FLOOR: float = 2.30
# User-set safety margin (2026-09-04). Do NOT exceed 0.25.
_EMBED_BOUND_SAFETY_MARGIN: float = 0.25
#: Chars below which an overflow is not a window problem. Every shrink path
#: (primary single embed, secondary fan-out, batch single-item) reaches it
#: through the ONE helper `_embed_shrinking_on_overflow`, so they cannot drift.
_EMBED_RETRY_FLOOR_CHARS: int = 512


def _min_chars_per_token_for(model_id: str, conservative: bool = False) -> float:
    """Measured-minimum chars/token for ``model_id`` (defect-1 ratio home).

    Partial-match lookup mirrors the rule ``chunking._num_ctx_for_model``
    uses (``"qwen3-embedding"`` matches ``"qwen3-embedding:0.6b"`` and vice
    versa). Unmeasured models resolve to the measured FLOOR — the
    conservative side, see the table's comment block.
    """
    table = (_EMBED_BOUND_MIN_CHARS_PER_TOKEN if conservative
             else _EMBED_BOUND_CHARS_PER_TOKEN_ATTEMPT)
    val = table.get(model_id)
    if val is None:
        for key, registered in table.items():
            if key in model_id or model_id in key:
                val = registered
                break
    return float(val) if val is not None else _EMBED_BOUND_MIN_CHARS_PER_TOKEN_FLOOR


# NOTE (round-5): there was a second overflow detector here —
# ``is_length_refusal`` plus an ``_OLLAMA_LENGTH_REFUSAL`` phrase constant —
# added in round 4 without checking that ``_is_context_overflow_error`` (below,
# same file) already existed. It matched NARROWER, returning False for two
# documented older Ollama phrasings, so an older runner's refusal would have
# propagated instead of triggering a retry. Round 4 made it delegate; round 5
# deleted it, because a delegating alias with no production caller is a second
# name for one concern that the next editor has to keep in sync for nothing.
# ``_is_context_overflow_error`` is the one home. Its markers cover the two
# HTTP codes the same cause produces (400 on /api/embed, 500 on the legacy
# /api/embeddings), which is why they match on the MESSAGE, not the status.


def _char_budget_for_model(
    model_id: str,
    conservative: bool = False,
    *,
    full_coverage: bool = True,
) -> int:
    """Char budget for ``model_id``'s num_ctx (0 when the model is unregistered).

    ``num_ctx × chars/token × (1 − 0.25 margin)`` — the defect-1 bound, using
    the MEDIAN ratio by default and the measured-MINIMUM one under
    ``conservative=True``.

    This is an ESTIMATE, not a guarantee. At the measured minimum ratio it
    lands at 75% of the true token window *for content as dense as the sample*
    — and denser content exists (markdown tables 1.41 chars/token, box-drawing
    1.61, CJK 1.35), so a budgeted text CAN still overflow. Nothing may rely on
    this value being unrefusable; the caller shrinks on refusal
    (``_embed_shrinking_on_overflow``).
    """
    num_ctx = _num_ctx_for_secondary(model_id)
    if not num_ctx or num_ctx <= 0:
        return 0
    ratio = _min_chars_per_token_for(model_id, conservative=conservative)
    budget = int(num_ctx * ratio * (1.0 - _EMBED_BOUND_SAFETY_MARGIN))
    if conservative or not full_coverage:
        # The shrink tier is never widened by the primary's chunker-max floor.
        #
        # `full_coverage=False` is the SECONDARY role. The own-chunker-max
        # floor below is a PRIMARY guarantee — "never truncate chunks this
        # model sized itself". A secondary receives text chunked for the
        # ACTIVE model, so its own chunker maximum is not a meaningful
        # floor there, and applying it swallowed the user-set 25% margin
        # whole: arctic's attempt became 12 800 chars = 4 006 tokens
        # against a 4 096 window, an effective margin of 2.2%. The margin
        # exists to keep refusals rare; a floor that erases it turns every
        # dense secondary chunk into a refusal-plus-retry round trip.
        return budget
    # PRIMARY FULL COVERAGE (WP-O, restated 2026-09-04): a slot must never be
    # bounded below what its OWN chunker already produced for it. The chunker
    # sizes chunks to this model's preset; re-bounding them more tightly here
    # would truncate the ACTIVE slot's own correctly-sized chunks — a coverage
    # loss on the primary retrieval path to satisfy a margin meant for the
    # SECONDARY (whose window is smaller) and for legacy corpora chunked for a
    # different model.
    #
    # So the attempt budget is at least the chunker's own maximum for this
    # model. The bound remains a real safety net for the case WP-R was built
    # for — chunks sized for a LARGER model reaching a smaller one after a
    # model switch — while never firing on a chunk this model's chunker made.
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import (
            CHARS_PER_TOKEN_TEXT,
            _preset_for_limit,
        )
        _min_t, max_t, _target_t = _preset_for_limit(num_ctx)
        own_chunker_max_chars = int(max_t) * int(CHARS_PER_TOKEN_TEXT)
        budget = max(budget, own_chunker_max_chars)
    except Exception:
        # chunking unimportable (standalone script context) — keep the ratio
        # budget. Conservative, and never silently wrong: the worst case is a
        # tighter bound, which is tagged, not a silent overflow.
        pass
    return budget



def _chars_per_token_text() -> int:
    from claude_mcp_servers.weaviate_mcp.chunking import CHARS_PER_TOKEN_TEXT
    return CHARS_PER_TOKEN_TEXT


def __getattr__(name: str):
    # PEP 562: `_CHARS_PER_TOKEN` stays importable by name for the two tests
    # that pin the char-budget arithmetic, but it is the SAME object as
    # `chunking.CHARS_PER_TOKEN_TEXT`, not a second literal.
    if name == "_CHARS_PER_TOKEN":
        return _chars_per_token_text()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _num_ctx_for_secondary(model_id: str) -> "int | None":
    """Resolve a secondary model's num_ctx via the chunking SoT (soft-fail None).

    Reuses ``MODEL_TOKEN_LIMITS`` (the single source of truth for per-model
    num_ctx) with the same partial-match rule the Ollama adapter uses. Returns
    None when the model isn't registered (caller then does NOT bound — an
    unknown model gets the full text, same as before this rework).
    """
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import _num_ctx_for_model
        return _num_ctx_for_model(model_id)
    except Exception:  # noqa: BLE001 — best-effort; caller falls back to full text
        return None


def _bounded_for_model(
    text: str,
    model_id: str,
    conservative: bool = False,
    *,
    full_coverage: bool = True,
) -> "tuple[str, bool]":
    """Return ``(text_or_subwindow, truncated)`` bounded to ``model_id``'s num_ctx.

    ONE shared home for the "don't feed an over-num_ctx chunk to Ollama" rule
    (WP-R 2026-07-22 generalised WP-O's secondary-only ``_bounded_for_secondary``
    so the ACTIVE embed path can reuse it — no second copy).

    If ``text`` fits within ``model_id``'s num_ctx character budget (or the model
    is unregistered / measurement unavailable), returns ``(text, False)`` — a
    faithful full vector. If it exceeds, returns the LEADING sub-window
    (``_char_budget_for_model``: num_ctx × the model's MEASURED-MINIMUM
    chars/token × 0.75 safety margin — see the ratio table above for why the
    chunker's model-agnostic constant is not used here) and ``True`` so the
    caller can react (tag/log). Never raises.

    This char budget is the pre-embed ESTIMATE, and it is an estimate that can
    be WRONG in the unsafe direction: it is derived from a median chars/token
    ratio, and denser-than-median content (markdown tables 1.41, box-drawing
    1.61, CJK 1.35) overflows the window at a length this budget allows.
    Such a text is not silently truncated — every embed sends ``truncate:
    false``, so the runner REFUSES it and ``_embed_secondary_with_refusal_retry``
    halves until it is accepted, tagging the result truncated because WE sent
    less than we were given. Truncation is known locally, from our own
    bounding; it is not inferred from the response (see the corrected
    ``_EMBED_BOUND`` block above for why ``prompt_eval_count`` cannot say it).

    Two callers, same rule, different intent:
      * SECONDARY-slot embed (WP-O): records the truncated slot in the
        per-call truncation record (``_last_truncated_slots``), which
        ``store_knowledge_node`` persists as the ``truncated_slots`` /
        ``secondary_truncated_slots`` chunk properties (R3-2 + v0.2.92); the
        ACTIVE slot stays full-fidelity on this bound.
      * ACTIVE-slot batch embed (WP-R): prevents the Ollama ``/api/embed`` HTTP
        400 ("input exceeds context length") + whole-batch rejection that a
        small-num_ctx ACTIVE model (arctic 4 096, granite, embeddinggemma, …)
        hit on a corpus whose chunks were sized for a larger model. Without this
        bound, ONE oversized item 400'd the entire batch of 100 → 0 enriched.
    """
    num_ctx = _num_ctx_for_secondary(model_id)
    if not num_ctx or num_ctx <= 0:
        return text, False
    char_budget = _char_budget_for_model(
        model_id, conservative=conservative, full_coverage=full_coverage
    )
    if len(text) <= char_budget:
        return text, False
    return text[:char_budget], True


# Back-compat alias: the WP-O secondary path + its tests reference the original
# name. It is THE SAME rule (bound to the model's num_ctx) — a rename with an
# alias, not a fork. Prefer ``_bounded_for_model`` in new code.
_bounded_for_secondary = _bounded_for_model


def _shrink_step(current_len: int, floor: int, min_tier: int) -> int:
    """Next attempt length: the LARGEST candidate strictly below ``current_len``.

    Candidates are the measured-MIN tier (only while it is below the current
    length) and half. Preferring the MIN tier on the first shrink is what keeps
    code-dense content from over-shrinking — see the ``_EMBED_BOUND`` block.
    Once the attempt is at or below the MIN tier the candidate is half, so the
    sequence still strictly shrinks and therefore terminates at ``floor``.
    """
    candidate = current_len // 2
    if 0 < min_tier < current_len and min_tier > candidate:
        candidate = min_tier
    return max(floor, candidate)


def _note_fidelity(kind: str, model_id: str, *args: Any) -> None:
    """Record a fidelity event for the SessionStart notice. Never raises.

    Telemetry must never be able to fail an embed — the whole point of the
    shrink ladder is that a dense chunk still produces a vector. Import is
    lazy and the whole call is guarded: on any failure the embed proceeds and
    the only loss is one summary row.
    """
    try:
        from vco_lib import embedding_fidelity

        if kind == "shrink":
            embedding_fidelity.note_shrink(model_id, *args)
        else:
            embedding_fidelity.note_floor_refusal(model_id, *args)
    except Exception:  # noqa: BLE001 — telemetry never breaks the embed
        pass


def _embed_shrinking_on_overflow(
    embed_fn: Callable[[str], Any],
    text: str,
    *,
    model_id: str,
    floor: int = _EMBED_RETRY_FLOOR_CHARS,
    on_shrink: Optional[Callable[[str, int], None]] = None,
) -> "tuple[Any, int]":
    """Call ``embed_fn(attempt)``, shrinking on a context overflow until accepted.

    Returns ``(result, sent_len)`` — ``sent_len`` is how many characters were
    actually embedded, so the caller decides truncation LOCALLY (we know we
    sent less than we were given) rather than inferring it from the response.

    THE one home for this loop (v0.2.92 round-5). Three paths need it and it
    existed as two near-identical copies while the third — the PRIMARY single
    embed — had none. That gap was a P1 regression: R45 sends
    ``truncate: false`` on every Ollama embed, which turns a silent tail loss
    into a hard refusal, and R45's own text credited "the caller catches and
    retries". On the primary, no caller caught: a dense chunk (CJK, a
    box-drawing diagram, a symbolic table — all inside the normal preset range)
    lost its ACTIVE vector entirely, where v0.2.91 had stored a leading-window
    one. A third copy of the loop would have been the same defect waiting to
    happen on the fourth path.

    Termination: each iteration returns, raises, or strictly shrinks
    (``_shrink_step`` is < current whenever current > floor), and at or below
    ``floor`` an overflow is re-raised — an overflow that small is not a window
    problem. Non-overflow exceptions propagate untouched: shrinking is the
    remedy for "too long", and applying it to an auth error would turn a
    diagnosable failure into a silent half-answer.
    """
    min_tier = _char_budget_for_model(model_id, conservative=True)
    attempt = text
    while True:
        try:
            result = embed_fn(attempt), len(attempt)
        except Exception as exc:  # noqa: BLE001 — re-raised unless retriable
            overflowed = _is_context_overflow_error(exc)
            if not overflowed or len(attempt) <= floor:
                if overflowed:
                    # At/below the floor and STILL refused: this slot gets no
                    # vector. Rare by construction and actionable, so it is
                    # recorded for the SessionStart fidelity notice — the
                    # non-overflow arm is NOT recorded, because propagating an
                    # auth or network error untouched is correct behaviour,
                    # not a fidelity loss.
                    _note_fidelity(
                        "floor_refusal", model_id, len(attempt), str(exc)
                    )
                raise
            attempt = attempt[: _shrink_step(len(attempt), floor, min_tier)]
            if on_shrink is not None:
                try:
                    on_shrink(model_id, len(attempt))
                except Exception:  # noqa: BLE001 — logging must never break the embed
                    pass
        else:
            # Record the OUTCOME, not each rung: a three-step ladder is ONE
            # input that lost text, not three shrinks. Counting per iteration
            # would inflate the summary by the ladder depth and make a single
            # pathological chunk look like a corpus-wide problem.
            if len(attempt) < len(text):
                _note_fidelity("shrink", model_id, len(text), len(attempt))
            return result


def _embed_secondary_with_refusal_retry(
    ollama: Any,
    model_id: str,
    text: str,
) -> "tuple[list[float], bool]":
    """Embed a SECONDARY slot, shrinking on a context overflow until it fits.

    v0.2.92 round-4. The previous form had exactly TWO tiers — attempt at the
    secondary budget, then one fallback at the "measured minimum" ratio — and
    claimed to terminate by arithmetic. It did not. That floor was derived from
    PROSE (2.30 chars/token); real KG content is far denser: markdown tables
    measure 1.41, box-drawing 1.61, CJK 1.35. For those, the single fallback
    ALSO overflowed, the second refusal propagated, and the slot got no vector
    at all — the very outcome the retry existed to prevent, on the dominant
    non-prose content.

    So the tier count is not fixed: shrink until the runner accepts or the
    floor is reached. The first shrink drops to the measured-MIN tier and every
    shrink after that halves (``_shrink_step``) — going straight to half would
    give up about a third of the window on exactly the code-dense content the
    MIN ratio was measured on. It terminates for ANY density, because each step
    is strictly smaller and ``_EMBED_RETRY_FLOOR_CHARS`` cannot exceed any
    supported window.

    The shrink loop itself lives in ``_embed_shrinking_on_overflow`` — the one
    home shared with the batch path and the two ACTIVE-slot paths — and keys on
    ``_is_context_overflow_error`` rather than a second detector: the round-4
    review found the private one I had added matched NARROWER than the existing
    helper (False on two documented older Ollama phrasings), so an older
    runner's refusal would have propagated instead of retrying. One home, one
    behaviour.

    Non-overflow exceptions propagate untouched: shrinking is a remedy for
    "too long", and applying it to an auth error would turn a diagnosable
    failure into a silent half-answer.
    """
    sub, bounded = _bounded_for_model(text, model_id, full_coverage=False)

    def _attempt(candidate: str) -> "tuple[list[float], Optional[bool]]":
        return _embed_with_exact_truncation(
            ollama, model_id, candidate, bounded or len(candidate) < len(text)
        )

    def _log(model: str, new_len: int) -> None:
        logger.info(
            "%s secondary overflowed its window; retrying under a tighter "
            "%d-char sub-window", model, new_len,
        )

    (vec, _exact), sent = _embed_shrinking_on_overflow(
        _attempt, sub, model_id=model_id, on_shrink=_log,
    )
    # Truncated iff we sent less than the caller gave us — known locally and
    # exactly, not inferred from the response.
    return vec, sent < len(text)

def _embed_with_exact_truncation(
    ollama: Any,
    model_id: str,
    bounded_text: str,
    ratio_truncated: bool,
) -> "tuple[list[float], bool]":
    """Embed a secondary slot's text, consulting the response's
    ``prompt_eval_count`` where it is available (v0.2.92 defect-3, corrected
    round-3/4).

    Returns ``(vector, truncated)``. The count is a ONE-WAY signal, and only
    the negative direction is sound: ``count < window`` proves the runner saw
    the input WHOLE. A count pinned AT the window proves only that the window
    was filled — an exact fit and a truncation produce the identical number —
    so ``_prompt_eval_truncated`` returns ``None`` there rather than guessing,
    and this function keeps the ratio verdict. (Under ``truncate: false`` an
    over-window input never returns 200 at all, so a pinned count is in
    practice an exact FIT; the retired form tagged precisely the inputs that
    used the window best.) The ratio verdict is OR-ed in, never overridden: a
    text this bound already sub-windowed stays truncated even though the
    smaller input fit.

    When the adapter lacks the capability (injected test stubs, a custom
    adapter) the ratio estimate stands unchanged — the "response not
    available" fallback leg. The ACTIVE-slot WP-R batch path also stays on
    the estimate: ``/api/embed`` batches return one aggregate count for the
    whole batch, so a per-item verdict cannot be read from it.

    Raises exactly what ``ollama.embed`` raises — the caller's existing
    per-slot exception handling is unchanged.
    """
    embed_with_truncation = (
        ollama.embed_with_truncation
        if isinstance(ollama, TruncationAwareOllamaAdapter)
        else None
    )
    if embed_with_truncation is None:
        return ollama.embed(model_id, bounded_text), bool(ratio_truncated)
    vector, exact = embed_with_truncation(model_id, bounded_text)
    if exact is None:
        return vector, bool(ratio_truncated)
    return vector, bool(ratio_truncated or exact)


# WP-R: Ollama's ``/api/embed`` returns HTTP 400 with a body naming the context
# length when a single input overflows the model's num_ctx. The message text has
# been stable across Ollama versions ("input length exceeds the context length" /
# "input exceeds context length"). We match on the durable substrings so the
# per-item sub-window retry only fires for a genuine window overflow — a network
# error / model-not-found / other 4xx must still raise so the caller sees it.
_CONTEXT_OVERFLOW_MARKERS = (
    "exceeds the context length",
    "exceeds context length",
    "context length",
    "input length",
)


def _is_context_overflow_error(exc: BaseException) -> bool:
    """True iff ``exc`` looks like an Ollama num_ctx overflow (WP-R).

    Conservative substring match on the exception text. Non-overflow errors
    (network, 404 model-not-found, malformed response) return False so they
    propagate unchanged rather than triggering a futile sub-window retry.
    """
    msg = str(exc).lower()
    # Require an HTTP-400-ish or explicit context phrase so a bare "length"
    # mention elsewhere doesn't false-positive.
    if "400" in msg or "context" in msg or "input length" in msg:
        return any(m in msg for m in _CONTEXT_OVERFLOW_MARKERS)
    return False


def resolve_active_text_model_id() -> str:
    """Return the ACTIVE text-slot model id, honoring the env override.

    ONE resolution home for "which text model does the active slot use", shared
    by ``EmbeddingService.for_project`` (constructs the live service) and
    ``configured_text_models`` (env-only chunk-budget SSOT). Precedence mirrors
    ``for_project`` exactly so the two never diverge:

      1. ``EMBEDDING_MODEL`` env (non-empty) — the explicit per-project override
         (config_projection / install.py subprocess thread). A custom-model
         install (e.g. ``embeddinggemma:300m-bf16`` num_ctx 2 048) reaches here.
      2. ``OPENAI_EMBEDDING_MODEL`` when the active profile is ``openai``.
      3. ``_model_id_for_active(active)`` — derive from the resolved profile.

    Before this helper existed, ``configured_text_models`` took ONLY leg 3, so a
    custom ``EMBEDDING_MODEL`` install (profile qwen3) got xlarge chunks sized to
    qwen3's 10 240-ctx fed to e.g. a 2 048-ctx embedder → silent truncation of the
    ACTIVE slot (R2-3).
    """
    env_text_model = os.environ.get("EMBEDDING_MODEL", "").strip()
    if env_text_model:
        return env_text_model
    active = _resolve_active_embedding()
    if active == "openai":
        return (
            os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip()
            or "text-embedding-3-small"
        )
    return _model_id_for_active(active)


def configured_text_models() -> "list[str]":
    """Return the model ids of every TEXT slot a KG write would populate.

    Dual-write slot-fan-out SSOT (2026-07-22): mirrors the slot fan-out of
    ``EmbeddingService.embed_text_all_configured`` but env-only (no service
    instance). This returns the WRITE-SET (which slots get embedded), NOT the
    active-slot chunk budget. Under the WP-O rework (v0.2.88) the KG active-slot
    chunker is sized to the ACTIVE model ALONE (unclamped) — this fan-out set is
    NO LONGER wired into active-slot chunk sizing (that would clamp the active
    slot to the tightest secondary and drop its fidelity below single-write). Any
    min-across-slots collapse a caller runs over this list is a RETAINED UTILITY
    (tests / future consumers), not the write-path boundary. The set is:

      * the ACTIVE text model (always written) — resolved via
        ``resolve_active_text_model_id()`` (honors the ``EMBEDDING_MODEL`` /
        ``OPENAI_EMBEDDING_MODEL`` env override exactly like ``for_project``, so
        a custom-model install is sized to its real ctx, not qwen3's — R2-3);
      * ``DEFAULT_TEXT_MODEL`` (qwen3) as the secondary enrichment slot, IFF the
        active slot isn't already qwen3 AND ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` is
        on (this is exactly the ``embed_text_all_configured`` condition — the
        Ollama-reachability check there is a runtime soft-fail, not a config
        decision, so for chunk sizing we assume the configured slot is live);
      * the arctic secondary (``ARCTIC_SECONDARY_MODEL``, num_ctx 4 096) IFF
        ``DUAL_EMBEDDING_ARCTIC_SECONDARY`` is on AND the active slot isn't
        already arctic AND write-all-slots is on (WP-O — the locally-served
        secondary embedded alongside the active slot on a qwen3-active install).
        Its 4 096 num_ctx is TIGHTER than qwen3's 10 240, but under the WP-O
        rework the active chunk is NOT shrunk to fit it — instead this arctic
        slot is embedded from a bounded, tagged sub-window when a chunk exceeds
        4 096 (``embed_text_all_configured``), so the active slot keeps full
        fidelity;
      * the OpenAI text model IFF an OpenAI key is configured AND the active slot
        isn't already OpenAI AND write-all-slots is on.

    When write-all-slots is OFF this returns a single-element list (the active
    model) — so any min-across-slots collapse a caller runs is a no-op and the
    single-model preset is preserved byte-identically (no behaviour change for
    the default install). The active-slot chunker sizes to that single active
    model in every case (dual on or off) — see the WP-O rework note above.

    Order: active model first, then secondaries. De-duplicated, order-preserving.
    """
    active_model = resolve_active_text_model_id()
    models: list[str] = [active_model]

    if not _resolve_write_all_slots():
        return models

    # Secondary qwen3 enrichment slot (unless active is already qwen3).
    if active_model != DEFAULT_TEXT_MODEL:
        models.append(DEFAULT_TEXT_MODEL)

    # Secondary arctic slot (WP-O) — opt-in via DUAL_EMBEDDING_ARCTIC_SECONDARY,
    # unless arctic is already the active slot. Mirrors the write-side condition
    # in ``embed_text_all_configured`` exactly (the Ollama-reachability check
    # there is a runtime soft-fail; for the write-set we assume the configured
    # slot is live, same as the qwen3 secondary). Arctic's 4 096 num_ctx is
    # tighter than qwen3's 10 240, but the WP-O rework does NOT clamp the active
    # chunk to it — the arctic slot is embedded from a bounded, tagged sub-window
    # instead (see the module note above). This entry only adds arctic to the
    # WRITE fan-out; it does not size the active chunk boundary.
    if _resolve_arctic_secondary() and "arctic" not in active_model.lower():
        models.append(ARCTIC_SECONDARY_MODEL)

    # Secondary OpenAI slot (unless active is already OpenAI) when a key exists.
    # Presence of the key is the config signal; validity is a runtime concern.
    if "openai" not in active_model.lower():
        openai_key = (
            os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("OPENAI_EMBEDDING_API_KEY", "").strip()
        )
        if openai_key:
            openai_model = _to_openai_api_model(
                os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            )
            models.append(openai_model)

    # De-dup, order-preserving.
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# v0.2.52 V52-AJ: active-embedding resolution helpers.
#
# Single canonical resolution path for the ACTIVE_EMBEDDING value:
#   env (ACTIVE_EMBEDDING)  →  launcher.db (app_state[embedding.active_profile])
#                            →  "qwen3" default.
#
# Used by ``EmbeddingService.for_project()``. install.py uses the SAME
# launcher.db reader (``read_app_state_active_embedding``) to thread the
# resolved value into subprocess env BEFORE spawning sync_knowledge_graph.py
# — so both code paths arrive at the same answer.
#
# No back-compat fallback ladder (per the v0.2.52 "consistent" rule,
# user-locked 2026-06-09): exactly one resolution chain, env always wins.


def _resolve_active_embedding() -> str:
    """Return the active embedding profile (lowercased, stripped).

    Resolution chain (each step short-circuits if non-empty):

      1. ``os.environ["ACTIVE_EMBEDDING"]`` — explicit env / install.py
         subprocess thread. This env is the PROJECTION of the per-project
         cascade (config_projection.py writes it from the sticky user pick
         / global default), so a deliberate per-project choice already
         reaches here via the projected ``.claude/{settings.json,env}``.
      2. ``launcher.db app_state[embedding.active_profile]`` — the
         machine-global default the Identity tab + install.py's preset
         selection wrote.
      3. ``launcher.db app_state[default_text_embedding]`` mapped to its
         profile — the hardware-pick derive (v0.2.71 T-B-emb), mirroring
         the cascade's machine-global leg so an env-less fallback agrees
         with the launcher / projection resolvers.
      4. ``"qwen3"`` — final fallback (free-tier install without launcher,
         or the launcher never booted post-install).

    All inputs are normalised with ``.strip().lower()``. Empty strings
    are treated as "absent" and skipped in favour of the next step.

    Returns:
        A non-empty lowercase string identifying the active embedding
        profile (typically ``"qwen3"``, ``"arctic"``, ``"openai"``).
    """
    env_value = os.environ.get("ACTIVE_EMBEDDING", "").strip().lower()
    if env_value:
        return env_value
    try:
        # Imported lazily to avoid a hard dependency on launcher_db_reader
        # for callers that don't touch this resolution path.
        from vco_lib.launcher_db_reader import (
            profile_for_text_model,
            read_app_state_active_embedding,
            read_app_state_default_text_embedding,
        )

        db_value = read_app_state_active_embedding()
        if db_value:
            return db_value.strip().lower()
        # v0.2.71 T-B-emb: mirror the cascade's machine-global leg — when the
        # canonical `embedding.active_profile` key is unset, derive from the
        # hardware pick (`app_state[default_text_embedding]`) before the qwen3
        # floor. Keeps this env-less fallback consistent with
        # project_env_settings.rs::global_active_embedding +
        # config_projection.py::_global_active_embedding (the projected env is
        # still the primary surface; this only fires when no env was projected).
        derived = profile_for_text_model(read_app_state_default_text_embedding())
        if derived:
            return derived.strip().lower()
    except Exception:
        # Soft-fail: every read path in launcher_db_reader already
        # swallows exceptions, but defense-in-depth against an
        # ImportError on a partial install or sqlite-disabled build.
        pass
    return "qwen3"


def _model_id_for_active(active: str) -> str:
    """Map an active-embedding profile to its canonical Ollama / OpenAI model id.

    The mapping mirrors install.py's ``EMBEDDING_CONFIGS`` table — keeping
    one resolution rule per profile prevents drift between the install-time
    choice and the runtime ``EmbeddingService`` selection.

    Args:
        active: profile id (case-insensitive). One of ``"qwen3"``,
            ``"arctic"``, ``"openai"``, ``"codesage"``. Anything else
            falls back to qwen3 (the safe default — install.py's preset
            chooser refuses to write any other value, so this branch
            should never fire in production).

    Returns:
        The model id string the relevant adapter expects (e.g.
        ``"snowflake-arctic-embed2:latest"`` for Ollama,
        ``"text-embedding-3-small"`` for OpenAI).

    See also:
        install.py ``EMBEDDING_CONFIGS`` — install-time presets that
        produce these same active→model mappings.
    """
    normalised = (active or "").strip().lower()
    if normalised == "arctic":
        return "snowflake-arctic-embed2:latest"
    if normalised == "openai":
        return "text-embedding-3-small"
    # ``qwen3``, ``codesage`` (text-side rarely used), or anything else:
    # fall back to qwen3, which is the only-always-present-on-fresh-install
    # text embedder.
    return DEFAULT_TEXT_MODEL


class EmbeddingService:
    """Per-project embedding dispatcher.

    Construct via :classmethod:`for_project` (the canonical entry
    point). Direct ``__init__`` is allowed for tests / advanced
    callers that want to inject mock adapters.

    The instance owns one ``requests.Session`` shared across all
    adapters. Call :meth:`close` (or use as a context manager) to
    release HTTP connections.

    All ``embed_*`` methods are SYNCHRONOUS. The Weaviate MCP server's
    async embedding helpers (``get_ollama_embedding`` etc.) still
    exist for the MCP path; this class is the sync API for scripts +
    install.py + Tauri subprocess calls.
    """

    def __init__(
        self,
        *,
        project_root: Path | None,
        ollama_url: str,
        code_embed_url: str,
        text_model_id: str,
        code_model_id: str,
        openai_api_key: str,
        session: requests.Session | None = None,
        # v0.2.69 FIX 3: per-embed-request timeout (seconds). Defaults to
        # None, which resolves to VCT_EMBED_REQUEST_TIMEOUT_SECS (or the
        # 180s default). Threaded into every real adapter so a wedged
        # embedder fails at chunk granularity rather than hanging forever.
        # Tests can pass an explicit value to make the cap small + assertable.
        embed_request_timeout: float | None = None,
        # Adapter injection points for tests:
        ollama_adapter: OllamaAdapter | None = None,
        code_adapter: CodeEmbedAdapter | None = None,
        openai_adapter: OpenAIAdapter | None = None,
    ) -> None:
        self.project_root = project_root
        self.ollama_url = ollama_url
        self.code_embed_url = code_embed_url
        self.text_model_id = text_model_id
        self.code_model_id = code_model_id
        self.openai_api_key = openai_api_key

        # Resolve the per-embed-request timeout once and reuse for every
        # adapter so the whole instance shares one cap.
        self.embed_request_timeout = (
            embed_request_timeout
            if embed_request_timeout is not None
            else _resolve_embed_request_timeout()
        )

        self._owns_session = session is None
        self.session = session or requests.Session()

        # Build adapters. Tests can inject mocks; default is the real ones.
        # The per-embed-request timeout is passed to each real adapter so a
        # single hung embed call aborts at the configured cap. (Health /
        # discovery probes inside the adapters clamp to min(timeout, 5s),
        # so a large embed timeout never slows liveness checks.)
        #
        # The DEFAULT Ollama adapter is the truncation-aware subclass (v0.2.92
        # defect-3): identical behaviour to the base adapter, plus
        # ``embed_with_truncation`` so the secondary-slot fan-out can determine
        # truncation EXACTLY from the Ollama response's ``prompt_eval_count``
        # instead of the char-ratio estimate. The default is built through the
        # ``OllamaAdapter`` name first (the long-standing test/injection seam —
        # patching that name must keep yielding the patched object verbatim)
        # and upgraded only when it really is a plain base instance; anything
        # injected — mock, subclass, custom adapter — is kept EXACTLY as given,
        # and the exact path is feature-detected at the call site via
        # isinstance, so a plain adapter silently keeps the estimate-only
        # behaviour.
        if ollama_adapter is None:
            ollama_adapter = OllamaAdapter(
                base_url=ollama_url,
                session=self.session,
                timeout=self.embed_request_timeout,
            )
            if type(ollama_adapter) is OllamaAdapter:
                ollama_adapter = TruncationAwareOllamaAdapter(
                    base_url=ollama_url,
                    session=self.session,
                    timeout=self.embed_request_timeout,
                )
        self.ollama: OllamaAdapter = ollama_adapter
        self.codeembed: CodeEmbedAdapter = code_adapter or CodeEmbedAdapter(
            base_url=code_embed_url,
            session=self.session,
            timeout=self.embed_request_timeout,
        )
        self.openai: OpenAIAdapter = openai_adapter or OpenAIAdapter(
            api_key=openai_api_key,
            session=self.session,
            timeout=self.embed_request_timeout,
        )

        # Pre-compute slot assignments for the configured models.
        self._text_slot, self._text_dim = _resolve_text_slot(text_model_id)
        self._code_slot, self._code_dim = _resolve_code_slot(code_model_id)

        # Lazily computed health checks (probed on first access).
        self._text_ready: bool | None = None
        self._code_ready: bool | None = None

        # Per-instance embed-result memo cache (v0.2.47 RL-3).
        # MCP-side RL telemetry calls ``embed_text`` for both the query
        # (at retrieval time) and the answer chunks (at citation time);
        # users often re-query the same string within a session. Without
        # this memo, every call hits Ollama / OpenAI fresh — cold-path
        # tax that dominates the citation-detection latency.
        # Key = sha256(text)[:24] (cheap collision-resistant fingerprint).
        # Cap = 512 entries (~4 MB at 1024-dim float32); evict oldest on
        # overflow. Cache is per-EmbeddingService instance and intentionally
        # process-local (no cross-process sharing).
        self._embed_memo_text: dict[str, list[float]] = {}
        self._embed_memo_code: dict[str, list[float]] = {}
        self._embed_memo_cap: int = 512

        # v0.2.92: ONE per-call record of EVERY slot — the ACTIVE text/code
        # slots included — whose embed used a bounded leading sub-window
        # (chunk exceeded that model's num_ctx, or the runner REFUSED the full
        # text and the shrink loop fell back to a leading window). Reset at the
        # start of every ``embed_text_all_configured`` / ``embed_code_all_configured``
        # / ``embed_code`` call. TWO views are
        # DERIVED from it and must stay derived — a second parallel record here
        # is exactly how the active and secondary stories would drift apart:
        #   * ``last_secondary_truncated`` — every slot except the active
        #     text/code slots. This is the pre-v0.2.92
        #     ``secondary_truncated_slots`` property's meaning, FROZEN for
        #     back-compat: widening a stored property's meaning would silently
        #     reinterpret every row written before this release.
        #   * ``last_active_truncated`` — the ACTIVE text slot's own verdict.
        # ``embed_text_all_configured_tagged`` captures the COMPLETE record
        # atomically with the vectors; the KG write persists it as the
        # ``truncated_slots`` chunk property and derives the secondary-only
        # view from it (see the WP-O block above).
        self._last_truncated_slots: dict[str, bool] = {}

    # ---- construction --------------------------------------------------

    @classmethod
    def for_project(
        cls,
        project_root: Path | None = None,
    ) -> "EmbeddingService":
        """Construct an EmbeddingService from environment.

        Reads:

          * ``OLLAMA_URL`` → defaults to ``http://localhost:11435``
          * ``CODE_EMBED_SERVICE_URL`` → defaults to ``http://localhost:11440``
          * ``EMBEDDING_MODEL`` → if unset, falls back to launcher.db
            ``app_state[embedding.active_profile]`` mapping, then
            ``qwen3-embedding:0.6b`` (v0.2.52 V52-AJ).
          * ``CODE_EMBED_MODEL`` → defaults to ``codesage-large-v2``
          * ``ACTIVE_EMBEDDING`` → if unset, falls back to launcher.db
            ``app_state[embedding.active_profile]``, then ``"qwen3"``
            (v0.2.52 V52-AJ). Drives slot selection when value indicates
            a non-default provider (``"openai"`` selects the OpenAI
            text model, ``"arctic"`` selects snowflake-arctic-embed2).
          * ``OPENAI_API_KEY`` → empty string is "no key configured".
          * ``CODE_EMBED_BACKEND`` → ``"service"`` (default) /
            ``"ollama"``. Affects code-model defaults.

        v0.2.52 V52-AJ — launcher.db fallback:
            When ``ACTIVE_EMBEDDING`` / ``EMBEDDING_MODEL`` env vars are
            absent or empty, ``for_project()`` consults launcher.db's
            ``app_state[embedding.active_profile]`` (written by the
            Identity-tab embedding selector or install.py's preset
            seeding). This unblocks install.py's ``sync_knowledge_graph.py``
            subprocess on Windows + CPU-only machines where the launcher
            stored ``arctic`` but the install.py subprocess inherited an
            empty env. Env always wins; launcher.db is fallback; default
            ``qwen3`` is the final fallback when launcher.db is also
            unreachable (free-tier install without the launcher).

        Raises:
            NoEmbeddingBackendError: If neither a text backend NOR a
                code backend can be reached after construction. The
                exception itself writes the failure log + the
                Claude-readable hint.
        """
        resolved_root = _detect_project_root(project_root)
        ollama_url = os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).strip() or DEFAULT_OLLAMA_URL
        # ONE home for the three-step order (v0.2.92 R2): explicit →
        # CODE_EMBED_SERVICE_URL → http://localhost:<CODE_EMBED_PORT|11440>.
        # It was inlined here, at ``configured_code_models`` below, and in
        # ``vco_lib/codegraph_resync.py``. Both copies here also IGNORED
        # ``CODE_EMBED_PORT``, so moving the service off 11440 with that
        # variable (the knob compose and install.py both honour) left this
        # probing the old port and silently demoting to an Ollama code tier.
        code_embed_url = _shared_service_base_url()

        # v0.2.52 V52-AJ: the active-profile resolution (env → launcher.db
        # app_state → "qwen3" default; env always wins) is now encapsulated
        # inside ``resolve_active_text_model_id`` below (R2-3 unified the two
        # sites). No separate ``_resolve_active_embedding()`` call is needed
        # here — the previous local was dead after R2-3 routed resolution
        # through the shared resolver (R3-11).
        # Choose text model id with provider awareness. ONE resolution home
        # shared with ``configured_text_models`` (R2-3): EMBEDDING_MODEL env wins,
        # else OPENAI_EMBEDDING_MODEL when active=openai, else derive from the
        # resolved profile (the install.py-Windows-CPU arctic fix — an empty
        # EMBEDDING_MODEL must NOT collapse a seeded arctic profile to qwen3).
        text_model_id = resolve_active_text_model_id()

        # Code model id. CODE_EMBED_BACKEND="ollama" means CPU fallback
        # via the qwen3 model; "service" means the FastAPI service.
        code_backend = os.environ.get("CODE_EMBED_BACKEND", "service").strip().lower() or "service"
        env_code_model = os.environ.get("CODE_EMBED_MODEL", "").strip()
        if env_code_model:
            code_model_id = env_code_model
        elif code_backend == "ollama":
            code_model_id = DEFAULT_TEXT_MODEL  # qwen3 CPU fallback
        else:
            code_model_id = DEFAULT_CODE_MODEL

        openai_api_key = os.environ.get("OPENAI_API_KEY", "")

        svc = cls(
            project_root=resolved_root,
            ollama_url=ollama_url,
            code_embed_url=code_embed_url,
            text_model_id=text_model_id,
            code_model_id=code_model_id,
            openai_api_key=openai_api_key,
        )

        # ----- Code-backend fallback chain (v0.2.18 correctness fix) -----
        # If the env-based resolution above picked codesage-large-v2 (the
        # CodeEmbed-service default) but the FastAPI service is down,
        # falling back to qwen3 / jina via Ollama keeps code-graph working
        # on machines where the GPU service hasn't been started.
        #
        # The chain only fires for the codesage_embed slot — other slots
        # (qwen3_embed CPU fallback, jina_embed explicit, openai_code_embed)
        # reflect explicit user/preset intent and are left alone.
        new_model, new_slot, new_dim, reason = _resolve_code_model_with_fallback(
            requested_model_id=svc.code_model_id,
            requested_slot=svc.code_vector_slot,
            requested_dim=svc.code_dim,
            ollama=svc.ollama,
            codeembed=svc.codeembed,
        )
        if reason:
            # Fallback fired — surface the chosen backend in stderr so
            # operators and tests can see what was selected. We use a
            # plain print() (not logger.warning) because logging may not
            # be configured at construction time in install.py / Tauri
            # subprocess contexts, and the message MUST reach stderr.
            print(reason, file=sys.stderr)
        if (new_model, new_slot, new_dim) != (
            svc.code_model_id,
            svc.code_vector_slot,
            svc.code_dim,
        ):
            # Reassign the slot triple so search-by-active-slot stays
            # correct and the dispatcher routes to the resolved model.
            svc.code_model_id = new_model
            svc._code_slot = new_slot
            svc._code_dim = new_dim
            # Invalidate cached readiness — the new slot has different
            # backend semantics (qwen3_embed routes to Ollama, not the
            # CodeEmbed service).
            svc._code_ready = None

        # Probe both readiness flags so we can fail fast with a useful
        # error and write the diagnostic.
        text_ready = svc.text_backend_ready()
        code_ready = svc.code_backend_ready()

        if not text_ready and not code_ready:
            error_per_backend = svc._collect_backend_errors()
            attempted = sorted(error_per_backend.keys())
            raise NoEmbeddingBackendError(
                "No embedding backend is reachable. Tried: "
                + ", ".join(attempted)
                + f". See {_failure_jsonl_path()} for details.",
                attempted_backends=attempted,
                error_per_backend=error_per_backend,
                install_root=resolved_root,
                env_snapshot=_redacted_env_snapshot(),
            )

        # Success — clear any stale failure markdown + deferral entry.
        _clear_failure_markdown(resolved_root)
        _clear_failure_deferral(resolved_root)
        return svc

    def _collect_backend_errors(self) -> dict[str, str]:
        """Map of backend id → why it's not reachable. Used in error path."""
        errors: dict[str, str] = {}
        if not self.ollama.is_reachable():
            errors["ollama"] = (
                f"Ollama at {self.ollama_url} did not respond to GET /api/tags. "
                f"Is the container running? "
                f"`podman start vco_ollama` or `docker start vco_ollama`."
            )
        if not self.codeembed.is_reachable():
            errors["codeembed"] = (
                f"CodeEmbed service at {self.code_embed_url} did not respond "
                f"to GET /health. Is the container running? "
                f"`podman start vco_code_embed` or `docker start vco_code_embed`."
            )
        if self.openai_api_key:
            res = self.openai.validate()
            if not res.valid:
                errors["openai"] = f"OpenAI key validation failed: {res.reason}"
        # If we tried no backends, also note that:
        if not errors:
            errors["none"] = (
                "All backends responded as reachable — this code path "
                "should not have triggered NoEmbeddingBackendError. "
                "Possible bug in EmbeddingService."
            )
        return errors

    # ---- context manager ----------------------------------------------

    def __enter__(self) -> "EmbeddingService":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the HTTP session if this instance owns it.

        Idempotent: safe to call multiple times.
        """
        if self._owns_session and self.session is not None:
            try:
                self.session.close()
            except Exception:  # pragma: no cover (defensive)
                pass

    # ---- per-instance properties (locked API) -------------------------

    @property
    def text_vector_slot(self) -> str:
        """Named-vector slot the configured text model writes to."""
        return self._text_slot

    @property
    def last_secondary_truncated(self) -> "dict[str, bool]":
        """SECONDARY slots embedded from a bounded sub-window on the last call.

        A VIEW over the single per-call record ``_last_truncated_slots`` (every
        configured slot, the active ones included), excluding the ACTIVE text
        and code slots. Populated by ``embed_text_all_configured``: maps each
        secondary slot whose embed used a bounded leading sub-window (chunk
        exceeded that model's num_ctx) to ``True``. Empty when no secondary was
        truncated. The ACTIVE slot is never present. A COPY is returned so
        callers can't mutate instance state.

        NOTE: for PERSISTING the tag alongside the vectors (the KG write path),
        prefer ``embed_text_all_configured_tagged`` — it captures the COMPLETE
        record ATOMICALLY with the vectors so a concurrent call from another
        task can't reset the per-instance record between the embed and this read
        (R3-2). This property is the low-level in-process SECONDARY view;
        ``store_knowledge_node`` persists the complete record as the
        ``truncated_slots`` Weaviate chunk property and derives the
        secondary-only ``secondary_truncated_slots`` property from it, which
        lets stored arctic (and other secondary) vectors be partitioned
        truncated-vs-full from stored data alone.
        """
        return {
            name: flag
            for name, flag in self._last_truncated_slots.items()
            if name not in (self._text_slot, self._code_slot)
        }

    @property
    def last_active_truncated(self) -> bool:
        """Whether the ACTIVE TEXT slot's last embed was shrunk after a refusal.

        A VIEW over the single per-call record ``_last_truncated_slots``: the
        ACTIVE TEXT slot's own entry. Normally False: chunks are sized to the
        active model's own preset, so the primary sees the whole chunk. It goes
        True when the runner refused the full text and
        ``_embed_shrinking_on_overflow`` fell back to a leading sub-window — a
        real fidelity loss on the slot retrieval reads, already logged at
        WARNING.

        Scope, stated exactly because a looser sentence here would be wrong:
        the entry is ASSIGNED (not raised) by every text embed that actually
        reaches the active Ollama backend, so it describes THAT embed and
        cannot latch across later ones. Two consequences worth knowing —

        * ``embed_text`` is memoised, so a cache HIT does not re-embed and
          leaves the entry describing the last real embed;
        * the OpenAI active leg has no shrink path at all, so the entry stays
          at whatever the last Ollama embed set.

        ``embed_text_all_configured`` resets the whole record up front, which
        is why the tagged variant reads cleanly per call. The ACTIVE CODE leg
        records its verdict in the SAME record under ``_code_slot``; this
        property deliberately surfaces only the TEXT slot's entry (the KG
        write path it serves embeds text), and a code-active embed whose slot
        name differs from the text slot's is not visible through it — the
        tagged capture resets the record anyway, so a code verdict never
        reaches a persisted text tag.
        """
        return bool(self._last_truncated_slots.get(self._text_slot, False))

    @property
    def text_dim(self) -> int:
        """Vector dim of the configured text model."""
        return self._text_dim

    def text_model_short_id(self) -> str:
        """Return the short source tag for the configured text model.

        Maps the resolved ``text_model_id`` (which may be a full Ollama
        tag like ``qwen3-embedding:0.6b`` or an OpenAI catalog id like
        ``openai-text-embedding-3-small``) to the canonical short tag
        that ``rl_logger.RLDataLogger.embedding_source`` expects:

          ``qwen3`` / ``arctic`` / ``openai`` / ``codesage`` / ``legacy``

        The mapping is deliberately coarse — these tags partition the
        RL training corpus into mutually-incompatible embedding spaces
        (different dims, different model families). Cross-mapping
        would corrupt training data.

        Resolution order:
          1. Slot-name based (``qwen3_embed`` → ``qwen3``). This is
             the canonical mapping because the slot itself is what
             the KG named-vector lives in.
          2. Substring scan on the raw model id for edge cases the
             slot doesn't disambiguate (e.g. legacy arctic in
             ``ollama_embed``).
          3. Fallback: ``"legacy"`` (matches rl_server.py's default
             for un-tagged events).
        """
        # 1. Slot-driven dispatch (the common case).
        slot = self._text_slot
        if slot == "qwen3_embed":
            return "qwen3"
        if slot == "arctic2_embed":
            return "arctic"
        if slot == "openai_text_embed":
            return "openai"
        if slot == "codesage_embed":
            return "codesage"

        # 2. Model-id substring scan for slot-ambiguous cases.
        # ``ollama_embed`` is the legacy bucket and contains BOTH old
        # arctic and exotic Ollama models; disambiguate here.
        lowered = (self.text_model_id or "").lower()
        if "arctic" in lowered:
            return "arctic"
        if "qwen3" in lowered or "qwen3-embedding" in lowered:
            return "qwen3"
        if "text-embedding-3" in lowered or "openai" in lowered:
            return "openai"
        if "codesage" in lowered:
            return "codesage"

        # 3. Fallback — matches rl_server.py's "legacy_1024" partition.
        return "legacy"

    @property
    def code_vector_slot(self) -> str:
        """Named-vector slot the configured code model writes to."""
        return self._code_slot

    @property
    def code_dim(self) -> int:
        """Vector dim of the configured code model."""
        return self._code_dim

    # ---- readiness ----------------------------------------------------

    def text_backend_ready(self) -> bool:
        """Whether the configured text backend is currently reachable.

        Result is cached after first call. Re-construct the service to
        re-probe.
        """
        if self._text_ready is not None:
            return self._text_ready

        if "openai" in self._text_slot:
            self._text_ready = (
                bool(self.openai_api_key) and self.openai.validate().valid
            )
        else:
            self._text_ready = self.ollama.is_reachable()
        return self._text_ready

    def code_backend_ready(self) -> bool:
        """Whether the configured code backend is currently reachable.

        Result is cached after first call.
        """
        if self._code_ready is not None:
            return self._code_ready

        if "openai" in self._code_slot:
            self._code_ready = (
                bool(self.openai_api_key) and self.openai.validate().valid
            )
        elif self._code_slot in ("codesage_embed", "jina_embed"):
            # Either the FastAPI service or Ollama (jina via Ollama).
            # Prefer CodeEmbed service if reachable; fall back to Ollama.
            self._code_ready = (
                self.codeembed.is_reachable() or self.ollama.is_reachable()
            )
        else:
            # qwen3-embed CPU fallback or generic Ollama code model.
            self._code_ready = self.ollama.is_reachable()
        return self._code_ready

    # ---- single-item embed --------------------------------------------

    def _retry_once_on_503(self, fn, *args):
        """Retry a provider embed on HTTP 503 with a BOUNDED exponential
        backoff before letting the failure propagate (v0.2.77 5c task 4;
        supersedes the v0.2.73 C-9 single-retry).

        The code-embed FastAPI service sheds with 503 when its in-flight
        semaphore is saturated (an update-all burst) OR while a model
        (re)loads. A single retry (C-9) was not enough to ride out the 5c
        incident's saturation window, so the embed chain still failed and the
        analyzer degraded the object to VECTORLESS (embed_revision=0). This
        method now retries on the module-level ``_EMBED_503_RETRY_BACKOFFS``
        schedule (2s, 5s, 10s + jitter — total ~17s < the 20s/object budget)
        so a transient burst is absorbed. It does NOT mask a genuinely-down
        backend: the schedule is finite; after the last delay a persistent 503
        re-raises and the caller's fail-safe (vectorless degrade) takes over
        exactly as before. Non-503 errors re-raise IMMEDIATELY (no behaviour
        change — only 503 is transient-retryable).

        The name is kept (many call sites reference it) even though it now
        retries up to ``len(_EMBED_503_RETRY_BACKOFFS)`` times.

        ``VCO_EMBED_503_RETRY_DELAY`` (seconds, default from the schedule)
        SCALES the whole schedule proportionally for back-compat with the C-9
        knob: a value of ``0`` makes every delay 0 (used by tests). Malformed
        → schedule used unscaled (a knob must not be a kill switch).
        """
        # Resolve the optional scale override once. When set, it replaces the
        # FIRST backoff and scales the rest by the same ratio; "0" → all zero.
        scale: "float | None" = None
        raw = os.getenv("VCO_EMBED_503_RETRY_DELAY")
        if raw is not None:
            try:
                first = float(raw)
                if first < 0:
                    first = 0.0
                base = _EMBED_503_RETRY_BACKOFFS[0] or 1.0
                scale = first / base
            except (TypeError, ValueError):
                scale = None  # malformed → use the schedule unscaled

        last_exc: "Exception | None" = None
        # attempt 0 = initial try; attempts 1..N = one per scheduled backoff.
        for attempt in range(len(_EMBED_503_RETRY_BACKOFFS) + 1):
            try:
                return fn(*args)
            except Exception as exc:  # noqa: BLE001 — inspect, then re-raise
                if "503" not in str(exc):
                    raise
                last_exc = exc
                if attempt >= len(_EMBED_503_RETRY_BACKOFFS):
                    break  # schedule exhausted → re-raise below
                delay = _EMBED_503_RETRY_BACKOFFS[attempt]
                if scale is not None:
                    delay *= scale
                # Jitter de-syncs many concurrent objects retrying in lock-step
                # (each hammering the same saturated service on the same beat).
                if delay > 0:
                    delay += random.uniform(0.0, delay * _EMBED_503_JITTER_FRAC)
                logger.info(
                    "Embed backend returned 503 (saturated / model loading?): "
                    "%s — retry %d/%d after %.1fs",
                    exc, attempt + 1, len(_EMBED_503_RETRY_BACKOFFS), delay,
                )
                if delay > 0:
                    time.sleep(delay)
        # Every attempt raised 503 → propagate the last one so the caller's
        # fail-safe degrade runs (never a silent swallow).
        assert last_exc is not None
        raise last_exc

    def embed_text(self, text: str) -> list[float]:
        """Embed one text via the active text backend.

        v0.2.47 RL-3: memo'd per-instance via ``_embed_memo_text``
        (cap 512). Returns the cached vector when the same text has
        been embedded before in this process.

        Raises:
            RuntimeError: If the active backend is unreachable or
                returns an error.
        """
        key = _memo_key(text)
        cached = self._embed_memo_text.get(key)
        if cached is not None:
            return cached
        vec = self._retry_once_on_503(self._embed_text_via_active, text)
        self._memo_put(self._embed_memo_text, key, vec)
        return vec

    def embed_code(self, code: str) -> list[float]:
        """Embed one code snippet via the active code backend.

        v0.2.47 RL-3: memo'd per-instance via ``_embed_memo_code``.

        Raises:
            RuntimeError: If the active backend is unreachable or
                returns an error.
        """
        # Reset the per-call truncation record before anything else (even a
        # memo hit): this call's verdict must not inherit an entry a previous
        # fan-out call left — the record describes THIS embed, same contract
        # as ``embed_text_all_configured`` / ``embed_code_all_configured``.
        self._last_truncated_slots = {}
        key = _memo_key(code)
        cached = self._embed_memo_code.get(key)
        if cached is not None:
            return cached
        vec = self._retry_once_on_503(self._embed_code_via_active, code)
        self._memo_put(self._embed_memo_code, key, vec)
        return vec

    def _memo_put(
        self, memo: dict[str, list[float]], key: str, vec: list[float]
    ) -> None:
        """Insert into a memo dict, evicting the oldest entry when over cap."""
        if len(memo) >= self._embed_memo_cap:
            # dict iteration order = insertion order (PEP 468); pop oldest.
            memo.pop(next(iter(memo)))
        memo[key] = vec

    # ---- batched embed (preferred for re-indexing) --------------------

    def embed_text_batch(self, texts: list[str]) -> list[list[float]]:
        """Batched text embedding. Empty input → empty output, no HTTP call.

        Order is preserved. See provider docs for batch-size limits
        (CodeEmbed: 256, OpenAI: chunked at 100).

        WP-R (2026-07-22): for the ACTIVE Ollama text model, each input is first
        bounded to the model's own num_ctx via ``_bounded_for_model`` (the shared
        WP-O sub-window rule), and a whole-batch ``/api/embed`` failure is
        ISOLATED to a per-item retry so one oversized item can never fail the
        batch. Root cause this closes: a small-num_ctx ACTIVE model (arctic
        4 096, granite, embeddinggemma, bge-m3, …) on a corpus whose chunks were
        sized for a larger model (qwen3 10 240) 400'd on any over-window item,
        and Ollama's ``/api/embed`` rejects the ENTIRE batch of 100 if ANY single
        input overflows → 100 % failure, 0 enriched (observed: 1 011/1 011 failed
        on the KG+Development arctic enrich). OpenAI's adapter chunks + validates
        its own token windows, so it keeps its straight batch path.
        """
        if not texts:
            return []
        if "openai" in self._text_slot:
            return self._retry_once_on_503(
                self.openai.embed_batch, self.text_model_id, texts
            )
        return self._embed_ollama_batch_bounded(self.text_model_id, texts)

    def _embed_ollama_batch_bounded(
        self, model_id: str, texts: list[str]
    ) -> list[list[float]]:
        """Ollama batch embed with per-model sub-window bounding + failure isolation.

        WP-R shared path for both ``embed_text_batch`` and the Ollama code
        fallback in ``embed_code_batch`` (one home — search-before-add). Two
        layers of protection against the num_ctx/whole-batch-rejection hazard:

          1. PRE-BOUND every input to ``model_id``'s num_ctx (``_bounded_for_model``)
             so an over-window chunk is trimmed to a leading sub-window BEFORE it
             reaches Ollama. This is the primary fix — with it, the 400 never
             fires in the common case. The trim is a fidelity loss on the ACTIVE
             slot (the sub-window is embedded, not the full chunk), so a per-batch
             trimmed-item count is logged at WARNING (R3-4 — loud degradation, the
             delta's "never silent" rule); the alternative was Ollama's silent
             whole-batch 400, strictly worse.
          2. ISOLATE a whole-batch failure PER ITEM: if the single ``/api/embed``
             batch call still raises (a pathological item, or a model whose true
             tokenizer ratio is denser than the char heuristic), fall back to
             per-item embed and COLLECT results — each item is retried alone under
             a TIGHTER sub-window on a context-overflow error. A genuinely
             un-embeddable item (still overflowing at the 512-char floor, or a
             non-window error) yields an EMPTY-VECTOR SENTINEL (``[]``) for THAT
             index only; every survivor keeps its computed vector. The consumer
             (``embedding_enrichment._flush_batch``) already treats an empty
             vector at an index as a per-object failure ("embed returned empty
             vector") and enriches the rest — so one hard failure marks exactly
             one uuid failed, never discarding the survivors (R3-4).

        Order is preserved. Returns exactly ``len(texts)`` entries in both paths;
        on the per-item fallback a failed index is ``[]`` (the caller's per-object
        failure sentinel) rather than raising and dropping the survivors.
        """
        bounded_pairs = [_bounded_for_model(t, model_id) for t in texts]
        bounded = [text for text, _ in bounded_pairs]
        trimmed_count = sum(1 for _, truncated in bounded_pairs if truncated)
        if trimmed_count:
            # Loud-degradation: a bounded sub-window is a fidelity loss, not a
            # no-op — record how many of this batch were trimmed so an
            # arctic-active enrich over a qwen3-sized corpus is not silent.
            logger.warning(
                "Ollama batch embed: %d/%d input(s) exceeded model %r's num_ctx "
                "and were embedded from a bounded leading sub-window (fidelity "
                "loss on those items) to avoid a whole-batch context-overflow "
                "rejection.",
                trimmed_count, len(bounded), model_id,
            )
        try:
            return self._retry_once_on_503(
                self.ollama.embed_batch, model_id, bounded
            )
        except Exception as batch_exc:  # noqa: BLE001 — isolate to per-item
            logger.warning(
                "Ollama batch embed failed for %d input(s) with model %r (%s); "
                "isolating to per-item embed so one un-embeddable item can't fail "
                "the surviving items",
                len(bounded), model_id, batch_exc,
            )
            results: list[list[float]] = []
            hard_failures = 0
            for text in bounded:
                try:
                    results.append(self._embed_ollama_one_bounded(model_id, text))
                except Exception as item_exc:  # noqa: BLE001 — isolate per item
                    # Empty-vector sentinel for THIS index only: the consumer
                    # (_flush_batch) marks the matching uuid failed and continues.
                    hard_failures += 1
                    logger.warning(
                        "Ollama per-item embed failed for one input with model "
                        "%r (%s); marking that item failed (empty-vector "
                        "sentinel) and preserving the batch's survivors",
                        model_id, item_exc,
                    )
                    results.append([])
            if hard_failures:
                logger.warning(
                    "Ollama per-item fallback: %d/%d input(s) still un-embeddable "
                    "after the tighter sub-window retry (marked failed); %d "
                    "survivor(s) embedded",
                    hard_failures, len(bounded), len(bounded) - hard_failures,
                )
            return results

    def _embed_ollama_one_bounded(self, model_id: str, text: str) -> list[float]:
        """Embed ONE text via Ollama, retrying a context overflow under a tighter
        sub-window (WP-R).

        Already-bounded ``text`` is embedded once; on a context-overflow error
        (Ollama HTTP 400 "input exceeds context length" — the char heuristic can
        still under-shoot on very dense content, exactly what WP-P's live arctic
        backfill hit) it shrinks and retries down to ``_EMBED_RETRY_FLOOR_CHARS``.
        Raises the last error if even the floor sub-window overflows, so the
        caller's per-object soft-fail records THIS item and continues.

        The shrink itself lives in ``_embed_shrinking_on_overflow`` (round-5).
        This used to be its own copy of the loop with a literal 512 floor, one
        of the two copies that existed while the PRIMARY single-embed path had
        none — which is how a P1 regression hid in a file that already
        contained its fix twice.
        """
        def _log(model: str, new_len: int) -> None:
            logger.debug(
                "Ollama single embed context overflow with model %r; "
                "retrying under a tighter %d-char sub-window", model, new_len,
            )

        vec, _sent = _embed_shrinking_on_overflow(
            lambda candidate: self._retry_once_on_503(
                self.ollama.embed, model_id, candidate
            ),
            text,
            model_id=model_id,
            on_shrink=_log,
        )
        return vec

    def _embed_codeembed_one_bounded(self, code: str) -> "tuple[list[float], bool]":
        """Embed ONE code entity via the CodeEmbed service, shrinking on the
        service's over-window refusal. Returns ``(vector, truncated)``.

        W1 / MAJOR-W2 (wiring audit, 2026-09-05): the service's gpu backend
        refuses over-window input with HTTP 400 ("input length exceeds the
        context length"); its ``ollama`` backend 502s wrapping the IDENTICAL
        Ollama phrase. Both surface through the CodeEmbedAdapter as a
        RuntimeError whose text ``_is_context_overflow_error`` matches, so
        the ONE shared detector covers both backend modes and the shrink
        loop is the same one every other leg uses — no fourth copy.
        """
        def _log(model: str, new_len: int) -> None:
            logger.debug(
                "CodeEmbed service embed context overflow with model %r; "
                "retrying under a tighter %d-char sub-window", model, new_len,
            )

        vec, sent = _embed_shrinking_on_overflow(
            lambda candidate: self._retry_once_on_503(
                self.codeembed.embed, candidate
            ),
            code,
            model_id=self.code_model_id,
            on_shrink=_log,
        )
        return vec, sent < len(code)

    def _embed_codeembed_batch_bounded(
        self, codes: list[str]
    ) -> list[list[float]]:
        """CodeEmbed-service batch embed with sub-window bounding + failure
        isolation — the twin of ``_embed_ollama_batch_bounded`` (W1).

        The service's ``/embed`` rejects the WHOLE batch when any single
        text is over-window, exactly like Ollama's ``/api/embed``, so the
        same two layers apply:

          1. PRE-BOUND every input to the model's num_ctx budget
             (``_bounded_for_model``); a per-batch trimmed count is logged
             at WARNING (loud degradation — same contract as the Ollama
             twin).
          2. ISOLATE a whole-batch failure PER ITEM through the shared
             shrink loop (``_embed_codeembed_one_bounded``); a genuinely
             un-embeddable item yields the EMPTY-VECTOR sentinel (``[]``)
             for that index only, which the consumer
             (``embedding_enrichment._flush_batch``) already treats as a
             per-object failure.

        Before W1 the batch leg was a bare ``embed_batch`` call: one
        over-window entity either 400/502'd the entire batch, or — gpu
        backend, pre-refusal — was silently half-embedded at HTTP 200.
        """
        model_id = self.code_model_id
        bounded_pairs = [_bounded_for_model(t, model_id) for t in codes]
        bounded = [text for text, _ in bounded_pairs]
        trimmed_count = sum(1 for _, truncated in bounded_pairs if truncated)
        if trimmed_count:
            logger.warning(
                "CodeEmbed batch embed: %d/%d input(s) exceeded model %r's "
                "num_ctx and were embedded from a bounded leading sub-window "
                "(fidelity loss on those items) to avoid a whole-batch "
                "context-overflow rejection.",
                trimmed_count, len(bounded), model_id,
            )
        try:
            return self._retry_once_on_503(self.codeembed.embed_batch, bounded)
        except Exception as batch_exc:  # noqa: BLE001 — isolate to per-item
            logger.warning(
                "CodeEmbed batch embed failed for %d input(s) with model %r "
                "(%s); isolating to per-item embed so one un-embeddable item "
                "can't fail the survivors",
                len(bounded), model_id, batch_exc,
            )
            results: list[list[float]] = []
            hard_failures = 0
            for text in bounded:
                try:
                    vec, _trunc = self._embed_codeembed_one_bounded(text)
                    results.append(vec)
                except Exception as item_exc:  # noqa: BLE001 — isolate per item
                    hard_failures += 1
                    logger.warning(
                        "CodeEmbed per-item embed failed for one input with "
                        "model %r (%s); marking that item failed "
                        "(empty-vector sentinel) and preserving the batch's "
                        "survivors",
                        model_id, item_exc,
                    )
                    results.append([])
            if hard_failures:
                logger.warning(
                    "CodeEmbed per-item fallback: %d/%d input(s) still "
                    "un-embeddable after the tighter sub-window retry "
                    "(marked failed); %d survivor(s) embedded",
                    hard_failures, len(bounded), len(bounded) - hard_failures,
                )
            return results

    def embed_code_batch(self, codes: list[str]) -> list[list[float]]:
        """Batched code embedding. Empty input → empty output.

        Routes to CodeEmbed service when slot is ``codesage_embed`` or
        ``jina_embed`` AND the service is reachable; falls back to
        Ollama (which can serve jina or qwen3) when the service is down.
        OpenAI goes through ``openai`` adapter directly.

        W1 (2026-09-05): the CodeEmbed leg is bounded + failure-isolated
        like the Ollama twin — one over-window entity can neither silently
        truncate (gpu backend pre-refusal) nor 400/502 the whole batch.
        """
        if not codes:
            return []
        if "openai" in self._code_slot:
            return self._retry_once_on_503(
                self.openai.embed_batch, self.code_model_id, codes
            )
        if self._code_slot in ("codesage_embed", "jina_embed"):
            # Service when reachable (bounded + isolated, W1); otherwise
            # fall through to the Ollama leg using the configured code model
            # id — best-effort, and it shares the same bounded helper.
            if self.codeembed.is_reachable():
                return self._embed_codeembed_batch_bounded(codes)
        # WP-R: the Ollama code fallback (e.g. jina/qwen3 code embeds served via
        # Ollama on a CPU/low-VRAM floor) shares the same num_ctx/whole-batch
        # hazard as the text path, so route it through the SAME bounded + isolated
        # helper (one home) instead of a bare batch call.
        return self._embed_ollama_batch_bounded(self.code_model_id, codes)

    # ---- multi-slot writes --------------------------------------------

    def embed_text_all_configured(self, text: str) -> dict[str, list[float]]:
        """Embed ``text`` into the configured text slot(s).

        Returns ``{slot_name: vector}``. ALWAYS includes the active slot
        (``self._text_slot``). The SECONDARY enrichment slots (qwen3,
        openai, and — WP-O, opt-in via ``DUAL_EMBEDDING_ARCTIC_SECONDARY`` —
        arctic ``arctic2_embed``) are added only when
        ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` is enabled (v0.2.71 Piece 5c —
        default OFF). The arctic secondary is what lets a qwen3-active install
        collect the arctic RL corpus without switching the ACTIVE model (R2-5).

        The secondary slots exist for the enrichment-migration path: when a
        user switches from qwen3 to OpenAI, a pre-populated qwen3_embed slot
        means search-with-qwen3 keeps working after the switch (no full
        re-embed), and BOTH RL nets' embedding spaces can be filled. That is
        opt-in because it doubles embed cost (see
        ``DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV``). With the toggle OFF this
        returns a single-entry ``{active_slot: vector}`` dict — still a valid
        named-vector write (NOT a flat vector), so existing named-vector
        collections keep working and previously-written dual data stays
        queryable; we simply stop WRITING the second slot going forward.

        Soft-fail per backend: if one slot's embed call fails (e.g.
        rate limited), it's omitted from the returned dict and a log
        line is emitted. The caller can choose to retry just those.
        """
        result: dict[str, list[float]] = {}
        # ONE per-call record of every slot embedded from a bounded leading
        # sub-window — the SECONDARY legs (chunk exceeded that model's num_ctx)
        # and, since round-5, the ACTIVE leg (the runner REFUSED the full text,
        # which chunk sizing normally prevents) all write here under their slot
        # name. ``embed_text_all_configured_tagged`` captures the COMPLETE
        # record atomically with the vectors; the KG write persists it as the
        # ``truncated_slots`` chunk property and derives the secondary-only
        # legacy view from it. The ACTIVE slot's truncation is therefore
        # persisted like the secondaries' (v0.2.92 m-R6-1) — no longer a
        # WARNING-log-only fact.
        self._last_truncated_slots = {}
        # Active backend — ALWAYS written (this is the slot reads target), from the
        # FULL text. Active-slot fidelity is never reduced by the secondary fan-out
        # (no-functionality-loss rule): chunk boundaries already follow the
        # active model's own preset, and here the active embed sees the whole
        # chunk. The one exception is the runner REFUSING that whole chunk
        # (denser than the token counter assumed), in which case
        # ``_embed_text_via_active`` shrinks, warns, and sets
        # ``last_active_truncated`` — a leading-window vector, never no vector.
        try:
            result[self._text_slot] = self._embed_text_via_active(text)
        except Exception as exc:
            logger.warning("Active text backend failed: %s", exc)

        # v0.2.71 Piece 5c: secondary enrichment slots are opt-in (default
        # OFF). When disabled, return only the active slot above.
        if not _resolve_write_all_slots():
            return result

        # qwen3 fallback if not already the active slot. qwen3's num_ctx (10 240)
        # is the WIDEST text tier, so a chunk sized to a tighter active model never
        # exceeds it — but bound defensively anyway (a custom active model could be
        # wider, e.g. bge-m3-vs-nothing edge cases) so the same tagged-degradation
        # contract holds for every secondary.
        if self._text_slot != "qwen3_embed" and self.ollama.is_reachable():
            try:
                # Attempt at the secondary budget, then shrink on a LENGTH
                # refusal until the runner accepts (round-3 BLOCKER-B:
                # truncate=false makes an over-window chunk a hard 400, so an
                # unhandled refusal loses the vector entirely).
                result["qwen3_embed"], trunc = _embed_secondary_with_refusal_retry(
                    self.ollama, DEFAULT_TEXT_MODEL, text
                )
                if trunc:
                    self._last_truncated_slots["qwen3_embed"] = True
                    logger.info(
                        "qwen3 secondary embedded from a bounded sub-window "
                        "(chunk exceeded qwen3 num_ctx); slot tagged truncated"
                    )
            except Exception as exc:
                logger.warning("qwen3 fallback embedding failed: %s", exc)
        # Arctic SECONDARY slot (WP-O) — opt-in via DUAL_EMBEDDING_ARCTIC_SECONDARY,
        # only when arctic isn't already the active slot and Ollama is up. This is
        # what lets a qwen3-active install collect the arctic RL corpus without an
        # ACTIVE switch (R2-5). Writes the SAME ``arctic2_embed`` slot the active
        # path + the dual-log other-slot resolver use (TEXT_SLOT_MAP:
        # snowflake-arctic-embed2 → arctic2_embed, 1024).
        #
        # SECONDARY DEGRADATION (no-functionality-loss rule): arctic's num_ctx (4 096) is
        # NARROWER than qwen3's (10 240). Chunks are sized to the ACTIVE model, so
        # on a qwen3-active install an oversized chunk would overflow arctic. Rather
        # than hand the full chunk to Ollama and let it SILENTLY truncate at 4 096,
        # we embed an EXPLICIT bounded leading sub-window and tag the slot
        # ``arctic2_embed`` truncated — the active qwen3 slot keeps the full chunk,
        # and per-model dataset assembly can partition the tagged-truncated arctic
        # vectors cleanly.
        if (
            self._text_slot != "arctic2_embed"
            and _resolve_arctic_secondary()
            and self.ollama.is_reachable()
        ):
            try:
                # Attempt at the secondary budget, then shrink on a LENGTH
                # refusal until the runner accepts (round-3 BLOCKER-B).
                result["arctic2_embed"], trunc = _embed_secondary_with_refusal_retry(
                    self.ollama, ARCTIC_SECONDARY_MODEL, text
                )
                if trunc:
                    self._last_truncated_slots["arctic2_embed"] = True
                    logger.info(
                        "arctic secondary embedded from a bounded sub-window "
                        "(chunk %d chars exceeded arctic num_ctx); slot tagged "
                        "truncated (active slot unaffected)", len(text)
                    )
            except Exception as exc:
                logger.warning("arctic secondary embedding failed: %s", exc)
        # OpenAI if not already and key configured + valid
        if "openai" not in self._text_slot and self.openai_api_key:
            if self.openai.validate().valid:
                try:
                    # OPENAI_EMBEDDING_MODEL canonically holds the raw API
                    # name (back-compat with env-driven installs), but a
                    # user copy-pasting a catalog id from the GUI will land
                    # the prefixed form here — strip defensively so the
                    # HTTP call always sees the raw name OpenAI's API
                    # expects (passing "openai-text-embedding-3-small" to
                    # /v1/embeddings returns HTTP 400).
                    openai_model = _to_openai_api_model(
                        os.environ.get(
                            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
                        )
                    )
                    # Bound the OpenAI secondary the same way (its 8 191 num_ctx is
                    # tighter than qwen3's 10 240) — explicit sub-window + tag,
                    # never a silent OpenAI-side truncation. (This is also the R2-4
                    # boundary case; the active slot stays full-fidelity.)
                    #
                    # ``full_coverage=False`` because this is a SECONDARY role
                    # (round-5 MAJOR-R5-3). Reaching the shared bound through the
                    # ``_bounded_for_secondary`` alias hid that the DEFAULT is the
                    # primary tier, whose own-chunker-max floor (25 600 chars for
                    # this model) overrides the ratio bound entirely — so a
                    # 32 768-char qwen3 chunk was sent as 25 600 chars, ~10 240
                    # tokens on table-dense content against an 8 191 window.
                    # The secondary tier is 14 129.
                    #
                    # Estimate-only by necessity: the OpenAI adapter exposes no
                    # token count on its response, so no exact verdict exists here
                    # — and unlike Ollama there is no shrink-on-refusal either.
                    # OpenAI's over-length error text is its own; matching it with
                    # `_is_context_overflow_error` would be a guess, so an
                    # over-length OpenAI secondary is dropped with a warning rather
                    # than silently half-embedded. The bound is what keeps that
                    # rare; it is not a guarantee.
                    sub, trunc = _bounded_for_secondary(
                        text, openai_model, full_coverage=False
                    )
                    result["openai_text_embed"] = self.openai.embed(
                        openai_model, sub
                    )
                    if trunc:
                        self._last_truncated_slots["openai_text_embed"] = True
                        logger.info(
                            "OpenAI secondary embedded from a bounded sub-window "
                            "(chunk exceeded openai num_ctx); slot tagged truncated"
                        )
                except Exception as exc:
                    logger.warning("OpenAI fallback embedding failed: %s", exc)
        return result

    def embed_text_all_configured_tagged(
        self, text: str
    ) -> "tuple[dict[str, list[float]], list[str]]":
        """Like ``embed_text_all_configured`` but returns the truncation record
        ATOMICALLY with the vectors (R3-2; widened v0.2.92 to every slot).

        Returns ``(slots, truncated_slot_names)`` where ``truncated_slot_names``
        is the sorted list of EVERY configured slot — the ACTIVE slot included —
        whose vector was embedded from a bounded leading sub-window on THIS call
        (a subset of ``slots``' keys). This is the ONE per-call capture the KG
        write persists: ``store_knowledge_node`` stores it verbatim as the
        ``truncated_slots`` chunk property (the COMPLETE record; its presence
        marks a row that can answer for every slot) and derives the narrower
        ``secondary_truncated_slots`` property from it by dropping the active
        slot, whose pre-v0.2.92 meaning is frozen for back-compat (see
        ``rl_enrichment.TRUNCATED_SLOTS_PROP``).

        Reading ``last_secondary_truncated`` / ``last_active_truncated`` as
        separate property calls is race-prone under concurrency (a second
        ``embed_text_all_configured`` from another task resets the per-instance
        record between the embed and the read). Capturing here — same call,
        before returning — closes that window so the KG write can persist a
        truncated tag that FAITHFULLY matches the vectors it stores. Callers
        that persist the tag MUST use this, not the properties.
        """
        slots = self.embed_text_all_configured(text)
        truncated = sorted(
            name for name, flag in self._last_truncated_slots.items() if flag
        )
        return slots, truncated

    def embed_code_all_configured(self, code: str) -> dict[str, list[float]]:
        """Embed ``code`` into the configured code slot(s).

        Mirrors ``embed_text_all_configured`` (v0.2.71 Piece 5c): ALWAYS
        writes the active code slot; the secondary slots (codesage, openai)
        are added only when ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` is enabled
        (default OFF). With the toggle off, returns a single-entry
        ``{active_code_slot: vector}`` dict — a valid named-vector write.
        """
        result: dict[str, list[float]] = {}
        # Reset the per-call truncation record, mirroring the text side: the
        # secondary ``codesage_embed`` entry is only ever WRITTEN True (never
        # assigned False), so without this reset one sub-windowed secondary
        # latches into every later code fan-out's verdict.
        self._last_truncated_slots = {}
        # Active backend — ALWAYS written.
        try:
            result[self._code_slot] = self._embed_code_via_active(code)
        except Exception as exc:
            logger.warning("Active code backend failed: %s", exc)

        # v0.2.71 Piece 5c: secondary enrichment slots are opt-in (default OFF).
        if not _resolve_write_all_slots():
            return result

        # CodeEmbed service if not active and reachable
        if (
            self._code_slot not in ("codesage_embed", "jina_embed")
            and self.codeembed.is_reachable()
        ):
            try:
                # W1 (2026-09-05): shrink on the service's over-window
                # refusal and TAG it, like every secondary leg. Without this
                # the service's new 400 refusal would DROP the slot where
                # the gpu backend used to silently half-embed it — a
                # refusal with no catcher is strictly worse than the silent
                # truncation it replaces.
                vec, trunc = self._embed_codeembed_one_bounded(code)
                result["codesage_embed"] = vec
                if trunc:
                    self._last_truncated_slots["codesage_embed"] = True
                    logger.info(
                        "codesage secondary embedded from a bounded sub-window "
                        "(entity exceeded the served window); slot tagged "
                        "truncated"
                    )
            except Exception as exc:
                logger.warning("CodeEmbed fallback embedding failed: %s", exc)
        # OpenAI — same prefix-strip defense as embed_text_all_configured
        if "openai" not in self._code_slot and self.openai_api_key:
            if self.openai.validate().valid:
                try:
                    openai_model = _to_openai_api_model(
                        os.environ.get(
                            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
                        )
                    )
                    result["openai_code_embed"] = self.openai.embed(
                        openai_model, code
                    )
                except Exception as exc:
                    logger.warning("OpenAI code fallback embedding failed: %s", exc)
        return result

    # ---- internal dispatch -------------------------------------------

    def _embed_text_via_active(self, text: str) -> list[float]:
        """Route a single text embed to the configured backend.

        For the OpenAI backend, ``text_model_id`` may have been populated
        from a launcher-managed env file that carries the catalog id form
        (``"openai-text-embedding-3-small"``); strip the prefix at the
        HTTP-call boundary because OpenAI's API rejects the prefixed form
        with HTTP 400.

        The Ollama leg attempts the WHOLE text — the primary slot is never
        pre-bounded (R45 primary full coverage; chunks are already sized to
        this model's own preset) — and shrinks ONLY if the runner refuses it.

        Why the shrink is here at all (v0.2.92 round-5 BLOCKER): R45 sends
        ``truncate: false`` on every Ollama embed, so an over-window input is
        a hard refusal instead of a silent tail loss. That is the right trade
        ONLY where something catches it. The batch path caught it; this path,
        which is what kg-sync and the MCP ``store_knowledge_node`` write use,
        did not — so a chunk denser than the counter assumed (CJK, a
        box-drawing diagram, a symbolic table, all inside the normal preset
        range) lost its ACTIVE vector entirely and the node stored nothing,
        where v0.2.91 had stored a leading-window vector. Losing the P1
        retrieval vector is strictly worse than the tail loss it replaced.

        A shrink here is a real fidelity loss on the slot retrieval reads, so
        it logs at WARNING (not info) and sets ``last_active_truncated``.
        """
        if "openai" in self._text_slot:
            return self.openai.embed(
                _to_openai_api_model(self.text_model_id), text
            )

        def _warn(model: str, new_len: int) -> None:
            logger.warning(
                "ACTIVE text embed refused by %s for a %d-char input "
                "(num_ctx exceeded); retrying under a tighter %d-char "
                "sub-window — this chunk's primary vector covers only its "
                "leading window",
                model, len(text), new_len,
            )

        vec, sent = _embed_shrinking_on_overflow(
            lambda candidate: self.ollama.embed(self.text_model_id, candidate),
            text,
            model_id=self.text_model_id,
            on_shrink=_warn,
        )
        # Record under the ACTIVE TEXT slot, in the SAME per-call record the
        # secondary legs write (assign, never latch: a shrink on one chunk must
        # not make the next chunk's full-fidelity vector report as truncated).
        # The tagged fan-out captures this atomically, so the active slot's
        # truncation is PERSISTED like the secondaries' (v0.2.92 m-R6-1).
        self._last_truncated_slots[self._text_slot] = sent < len(text)
        return vec

    def _embed_code_via_active(self, code: str) -> list[float]:
        """Route a single code embed to the configured backend.

        For codesage_embed / jina_embed slots, prefer the FastAPI service
        when reachable; fall back to Ollama otherwise. OpenAI path applies
        the same catalog-id-prefix strip as ``_embed_text_via_active``.

        The Ollama leg shrinks on refusal for the same reason the text leg
        does (round-6 BLOCKER, the twin of round-5's): `truncate: false` makes
        an over-window input a hard 400, and this is the path the code-graph
        analyzer uses PER ENTITY — so without a catcher an entity the runner
        refuses gets no vector at all.

        W1 (wiring audit, 2026-09-05): the CodeEmbed SERVICE leg — the
        DEFAULT GPU tier's path — gets the same treatment. It could not even
        refuse before: sentence-transformers truncated silently at the
        SERVED window (sentence_bert_config.json max_seq_length = 1 024,
        not the 2 048 architectural cap every budget was sized for), so a
        maximal entity lost roughly half its text at HTTP 200 with no
        warning and no tag. The service now refuses over-window input with
        HTTP 400 carrying the phrase `_is_context_overflow_error` already
        matches, and this leg routes that refusal through the ONE shared
        shrink loop — a tagged leading-window vector, never a silent
        halving and never a dropped entity. The same wiring also covers the
        service's ``ollama`` backend mode (MAJOR-W2), whose 502 wraps the
        identical Ollama phrase.

        It is not a rare case here. Entities are pre-sized by
        ``code_truncation`` at ``num_ctx × CHARS_PER_TOKEN_CODE`` with
        CHARS_PER_TOKEN_CODE = 3.5, while real code measures ~2.8-2.95
        chars/token on the Ollama code tiers (measured 2026-09-04: jina
        1 719 tokens for 4 800 chars, qwen3 9 491 for 28 000) and
        2.96-3.47 on CodeSage budget-window slices (measured 2026-09-05,
        W1). A maximal entity therefore overflows its own budget on
        ORDINARY code. The shrink makes that correct rather than fatal;
        retuning the 3.5 constant would move code-graph boundaries and
        force a re-embed, so it is a separate, user-owned decision and is
        deliberately NOT taken here.
        """
        def _warn(model: str, new_len: int) -> None:
            logger.warning(
                "ACTIVE code embed refused by %s for a %d-char entity "
                "(num_ctx exceeded); retrying under a tighter %d-char "
                "sub-window — this entity's vector covers only its leading "
                "window",
                model, len(code), new_len,
            )

        if "openai" in self._code_slot:
            return self.openai.embed(
                _to_openai_api_model(self.code_model_id), code
            )
        if self._code_slot in ("codesage_embed", "jina_embed"):
            if self.codeembed.is_reachable():
                # W1: through the ONE shared shrink loop — see docstring.
                # Records under the ACTIVE CODE slot exactly like the
                # Ollama leg below (assign, never latch).
                vec, sent = _embed_shrinking_on_overflow(
                    lambda candidate: self.codeembed.embed(candidate),
                    code,
                    model_id=self.code_model_id,
                    on_shrink=_warn,
                )
                self._last_truncated_slots[self._code_slot] = sent < len(code)
                return vec

        vec, sent = _embed_shrinking_on_overflow(
            lambda candidate: self.ollama.embed(self.code_model_id, candidate),
            code,
            model_id=self.code_model_id,
            on_shrink=_warn,
        )
        # Record under the ACTIVE CODE slot, in the same per-call record the
        # text legs write (assign, never latch — same contract as the text
        # leg). The KG write path never captures this key (its record resets
        # on every ``embed_text_all_configured``), so a code verdict cannot
        # leak into a persisted text tag.
        self._last_truncated_slots[self._code_slot] = sent < len(code)
        return vec

    # ---- catalogue discovery (classmethods) ----------------------------

    @classmethod
    def discover_text_models(
        cls,
        *,
        ollama_url: str | None = None,
        openai_api_key: str | None = None,
        session: requests.Session | None = None,
    ) -> list[ModelChoice]:
        """Catalogue of text-embedding models reachable right now.

        Probes Ollama via ``/api/tags`` (filtered to embedding-capable
        models), CodeEmbed is skipped here (it's a code-only backend),
        OpenAI is probed via the free ``/v1/models/<model>`` endpoint
        if ``OPENAI_API_KEY`` is present.

        Each returned :class:`ModelChoice` carries ``available_now``
        based on whether the backend responded AND (for OpenAI) the
        key validates.

        For models not reachable, the entry is still included with
        ``available_now=False`` so the GUI dropdown can show greyed-out
        options that explain WHY (e.g. "OpenAI: no API key configured").
        """
        ollama_url = (
            ollama_url
            or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).strip()
            or DEFAULT_OLLAMA_URL
        )
        openai_api_key = (
            openai_api_key
            if openai_api_key is not None
            else os.environ.get("OPENAI_API_KEY", "")
        )

        owns_session = session is None
        sess = session or requests.Session()
        try:
            choices = cls._discover_text_choices(sess, ollama_url, openai_api_key)
        finally:
            if owns_session:
                sess.close()
        return choices

    @classmethod
    def discover_code_models(
        cls,
        *,
        ollama_url: str | None = None,
        code_embed_url: str | None = None,
        openai_api_key: str | None = None,
        session: requests.Session | None = None,
    ) -> list[ModelChoice]:
        """Catalogue of code-embedding models reachable right now.

        Probes the CodeEmbed FastAPI service (preferred for code),
        Ollama (jina / qwen3 fallback), and OpenAI (forward-compat
        slot — same model id used for text + code today).
        """
        ollama_url = (
            ollama_url
            or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).strip()
            or DEFAULT_OLLAMA_URL
        )
        # Same ONE home as ``for_project`` above (v0.2.92 R2).
        code_embed_url = _shared_service_base_url(code_embed_url)
        openai_api_key = (
            openai_api_key
            if openai_api_key is not None
            else os.environ.get("OPENAI_API_KEY", "")
        )

        owns_session = session is None
        sess = session or requests.Session()
        try:
            choices = cls._discover_code_choices(
                sess, ollama_url, code_embed_url, openai_api_key
            )
        finally:
            if owns_session:
                sess.close()
        return choices

    @classmethod
    def _discover_text_choices(
        cls,
        session: requests.Session,
        ollama_url: str,
        openai_api_key: str,
    ) -> list[ModelChoice]:
        choices: list[ModelChoice] = []

        # Ollama side.
        ollama = OllamaAdapter(ollama_url, session=session)
        ollama_reachable = ollama.is_reachable()
        if ollama_reachable:
            for m in ollama.list_embedding_models():
                name = str(m.get("name", ""))
                if not name:
                    continue
                slot, dim = _resolve_text_slot(name)
                # Try to upgrade dim from known table if resolver fell back
                if dim == DEFAULT_TEXT_SLOT[1] and name in KNOWN_OLLAMA_DIMS:
                    dim = KNOWN_OLLAMA_DIMS[name]
                choices.append(
                    ModelChoice(
                        id=name,
                        label=f"{name} ({dim}d, Ollama)",
                        dim=dim,
                        slot=slot,
                        backend="ollama",
                        available_now=True,
                    )
                )
        else:
            # Emit a placeholder so the GUI can show "Ollama not reachable"
            choices.append(
                ModelChoice(
                    id="ollama-unreachable",
                    label="Ollama (not reachable)",
                    dim=0,
                    slot="",
                    backend="ollama",
                    available_now=False,
                    reason_unavailable=(
                        f"Ollama at {ollama_url} did not respond to GET /api/tags."
                    ),
                )
            )

        # OpenAI side.
        #
        # Catalog id translation: the dict keys in KNOWN_OPENAI_EMBEDDING_MODELS
        # are the RAW OpenAI API model names (the form the HTTP API expects).
        # We probe the API with the raw form but emit the catalog id in the
        # PREFIXED form so it matches what `openai_cmd.rs`,
        # `install.py::_preset_to_default_models`, and the GUI dropdown all
        # write/expect for `app_state.default_text_embedding`. See
        # `_to_openai_catalog_id` for the boundary rationale.
        if openai_api_key:
            oa = OpenAIAdapter(openai_api_key, session=session)
            # v0.2.92: a `validate("text-embedding-3-small")` pre-probe sat
            # here with its result never read. Not a dropped branch — that id
            # is a KEY of KNOWN_OPENAI_EMBEDDING_MODELS, so the loop below
            # probes it anyway (and OpenAIAdapter caches per model). Don't
            # re-add it; the code sibling never had one.
            for raw_model_id, dim in KNOWN_OPENAI_EMBEDDING_MODELS.items():
                catalog_id = _to_openai_catalog_id(raw_model_id)
                slot, _ = _resolve_text_slot(raw_model_id)
                # Probe each known model individually so the user can
                # see which ones their key can access. Probe uses the RAW
                # name (the only form the HTTP API understands).
                v = oa.validate(raw_model_id)
                choices.append(
                    ModelChoice(
                        id=catalog_id,
                        label=f"{raw_model_id} ({dim}d, OpenAI)",
                        dim=dim,
                        slot=slot,
                        backend="openai",
                        available_now=v.valid,
                        reason_unavailable=None if v.valid else v.reason,
                    )
                )
        else:
            for raw_model_id, dim in KNOWN_OPENAI_EMBEDDING_MODELS.items():
                catalog_id = _to_openai_catalog_id(raw_model_id)
                slot, _ = _resolve_text_slot(raw_model_id)
                choices.append(
                    ModelChoice(
                        id=catalog_id,
                        label=f"{raw_model_id} ({dim}d, OpenAI)",
                        dim=dim,
                        slot=slot,
                        backend="openai",
                        available_now=False,
                        reason_unavailable="OPENAI_API_KEY not configured",
                    )
                )

        return choices

    @classmethod
    def _discover_code_choices(
        cls,
        session: requests.Session,
        ollama_url: str,
        code_embed_url: str,
        openai_api_key: str,
    ) -> list[ModelChoice]:
        choices: list[ModelChoice] = []

        # CodeEmbed service
        codeembed = CodeEmbedAdapter(code_embed_url, session=session)
        if codeembed.is_reachable():
            model_name = codeembed.model_name or "codesage-large-v2"
            dim = codeembed.model_dim or 2048
            slot, _ = _resolve_code_slot(model_name)
            backend = codeembed.backend or "codeembed"
            choices.append(
                ModelChoice(
                    id=model_name,
                    label=f"{model_name} ({dim}d, CodeEmbed/{backend})",
                    dim=dim,
                    slot=slot,
                    backend="codeembed",
                    available_now=True,
                )
            )
        else:
            choices.append(
                ModelChoice(
                    id="codesage-large-v2",
                    label="codesage-large-v2 (2048d, CodeEmbed service)",
                    dim=2048,
                    slot="codesage_embed",
                    backend="codeembed",
                    available_now=False,
                    reason_unavailable=(
                        f"CodeEmbed service at {code_embed_url} did not respond to /health."
                    ),
                )
            )

        # Ollama side — list whatever code-capable embedding models the
        # user has pulled (jina-v2, qwen3 fallback, etc.).
        ollama = OllamaAdapter(ollama_url, session=session)
        if ollama.is_reachable():
            for m in ollama.list_embedding_models():
                name = str(m.get("name", ""))
                if not name:
                    continue
                slot, dim = _resolve_code_slot(name)
                if dim == DEFAULT_CODE_SLOT[1] and name in KNOWN_OLLAMA_DIMS:
                    dim = KNOWN_OLLAMA_DIMS[name]
                # Skip pure-text models that don't have a code use
                # (we still emit them — better to show all options than
                # second-guess the user)
                choices.append(
                    ModelChoice(
                        id=name,
                        label=f"{name} ({dim}d, Ollama code/fallback)",
                        dim=dim,
                        slot=slot,
                        backend="ollama",
                        available_now=True,
                    )
                )

        # OpenAI (text-embedding-3-small / -large as code embed too).
        # Catalog id translation: see the equivalent block in
        # `_discover_text_choices` for the rationale — the dict keys are
        # raw API names, the catalog id emits the prefixed form so it
        # round-trips with `app_state.default_code_embedding`.
        if openai_api_key:
            oa = OpenAIAdapter(openai_api_key, session=session)
            for raw_model_id, dim in KNOWN_OPENAI_EMBEDDING_MODELS.items():
                catalog_id = _to_openai_catalog_id(raw_model_id)
                slot, _ = _resolve_code_slot(raw_model_id)
                v = oa.validate(raw_model_id)
                choices.append(
                    ModelChoice(
                        id=catalog_id,
                        label=f"{raw_model_id} ({dim}d, OpenAI as code)",
                        dim=dim,
                        slot=slot,
                        backend="openai",
                        available_now=v.valid,
                        reason_unavailable=None if v.valid else v.reason,
                    )
                )
        else:
            for raw_model_id, dim in KNOWN_OPENAI_EMBEDDING_MODELS.items():
                catalog_id = _to_openai_catalog_id(raw_model_id)
                slot, _ = _resolve_code_slot(raw_model_id)
                choices.append(
                    ModelChoice(
                        id=catalog_id,
                        label=f"{raw_model_id} ({dim}d, OpenAI as code)",
                        dim=dim,
                        slot=slot,
                        backend="openai",
                        available_now=False,
                        reason_unavailable="OPENAI_API_KEY not configured",
                    )
                )

        return choices


# ---------------------------------------------------------------------------
# CLI entry point — for Tauri sidecar invocation
# ---------------------------------------------------------------------------


def _cli_discover(project_root: Path | None = None) -> int:
    """Implement ``python -m vco_lib.embedding_service discover``.

    Prints a JSON document with shape::

        {
          "text_models": [<ModelChoice asdict>, ...],
          "code_models": [<ModelChoice asdict>, ...],
          "current_text_slot": "qwen3_embed",
          "current_code_slot": "codesage_embed",
          "errors": []
        }

    Stdout is JSON-only; logs go to stderr. This is the contract the
    Tauri ``get_embedding_catalog`` command (Commit 8) consumes.

    Args:
        project_root: optional project-root override for
            ``EmbeddingService.for_project()`` — forwards the GUI's
            "which project are we asking about" context. When ``None``,
            ``EmbeddingService.for_project()`` falls back to its
            normal env/cwd-based discovery.

    Returns exit code 0 on success, 1 if discovery itself raised
    (Ollama URL malformed, etc.).
    """
    errors: list[str] = []
    try:
        text_models = EmbeddingService.discover_text_models()
    except Exception as exc:
        text_models = []
        errors.append(f"discover_text_models failed: {exc}")

    try:
        code_models = EmbeddingService.discover_code_models()
    except Exception as exc:
        code_models = []
        errors.append(f"discover_code_models failed: {exc}")

    # Best-effort: also report the current project's active slots.
    current_text_slot: str | None = None
    current_code_slot: str | None = None
    try:
        svc = EmbeddingService.for_project(project_root=project_root)
        try:
            current_text_slot = svc.text_vector_slot
            current_code_slot = svc.code_vector_slot
        finally:
            svc.close()
    except NoEmbeddingBackendError as exc:
        errors.append(f"for_project() failed: {exc}")

    payload = {
        "text_models": [asdict(m) for m in text_models],
        "code_models": [asdict(m) for m in code_models],
        "current_text_slot": current_text_slot,
        "current_code_slot": current_code_slot,
        "errors": errors,
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.embedding_service",
        description=(
            "EmbeddingService — central dispatcher for VCO embeddings. "
            "Use 'discover' to print a JSON catalog of reachable models."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    discover = sub.add_parser(
        "discover",
        help="Print a JSON catalog of reachable embedding models",
    )
    # --project-root: forwarded into ``EmbeddingService.for_project()`` so
    # the GUI can ask "for this project, what slots are active?". When
    # the Tauri ``get_embedding_catalog`` is called with a project_id, the
    # Rust side resolves it to a folder path and passes it through here.
    discover.add_argument(
        "--project-root",
        type=str,
        default=None,
        help=(
            "Project root path used to resolve current_text_slot / "
            "current_code_slot. Defaults to env-based discovery."
        ),
    )
    # --json: accept-and-ignore for spec parity. Output is JSON-only
    # regardless. Kept as an explicit no-op so future callers that
    # forget the implicit-JSON contract don't trip the argparse error
    # path.
    discover.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="No-op: output is always JSON. Kept for caller-side clarity.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.cmd == "discover":
        project_root = Path(args.project_root) if args.project_root else None
        return _cli_discover(project_root=project_root)
    parser.error(f"Unknown command: {args.cmd}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
