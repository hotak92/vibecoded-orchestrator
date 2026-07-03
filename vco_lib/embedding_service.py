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
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Optional

import requests

from vco_lib.embedding_providers import (
    CodeEmbedAdapter,
    OllamaAdapter,
    OpenAIAdapter,
)
from vco_lib.embedding_providers.ollama import KNOWN_OLLAMA_DIMS
from vco_lib.embedding_providers.openai import (
    KNOWN_OPENAI_EMBEDDING_MODELS,
)

logger = logging.getLogger(__name__)

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
                f"CodeEmbed + qwen3 both unavailable; "
                f"using ollama:jina-embeddings-v2-base-code (slot=jina_embed)"
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
    """``~/.claude/metrics/embedding_failures.jsonl``. Cross-OS via Path.home()."""
    return Path.home() / ".claude" / "metrics" / "embedding_failures.jsonl"


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
        # Local import: avoid circular-import risk if deferral_report ever
        # imports from embedding_service (it doesn't today, but the import
        # is cheap and the safety margin is worth it on the error path).
        from vco_lib.deferral_report import DeferralEntry, DeferralReport

        report = DeferralReport.read(exc.install_root)
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

        report.add_entry(
            DeferralEntry(
                condition_id=_DEFERRAL_CONDITION_ID,
                title="Embedding backend unreachable; KG seed deferred",
                detected=detected,
                why_deferred=why_deferred,
                command_to_apply=command_to_apply,
                severity="warning",
                kg_node_refs=kg_refs,
            )
        )
        report.write(exc.install_root)
    except Exception as e:  # noqa: BLE001 — soft-fail on the error path
        logger.warning("Failed to write embedding failure deferral entry: %s", e)


def _clear_failure_deferral(install_root: Path | None) -> None:
    """Mark the ``kg_summary_no_backend`` entry resolved (paired with success).

    No-op if there's no deferral file or no matching entry. Soft-fail.
    """
    if install_root is None:
        return
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(install_root)
        # mark_resolved is a no-op when the entry isn't present, so this
        # is safe to call unconditionally.
        existing_ids = {e.condition_id for e in report.entries}
        if _DEFERRAL_CONDITION_ID not in existing_ids:
            return
        report.mark_resolved(_DEFERRAL_CONDITION_ID)
        report.write(install_root)
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
DEFAULT_CODE_EMBED_URL = "http://localhost:11440"
DEFAULT_TEXT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_CODE_MODEL = "codesage-large-v2"


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
        self.ollama: OllamaAdapter = ollama_adapter or OllamaAdapter(
            base_url=ollama_url,
            session=self.session,
            timeout=self.embed_request_timeout,
        )
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
        code_embed_url = (
            os.environ.get("CODE_EMBED_SERVICE_URL", DEFAULT_CODE_EMBED_URL).strip()
            or DEFAULT_CODE_EMBED_URL
        )

        # v0.2.52 V52-AJ: env → launcher.db app_state → "qwen3" default.
        # Env always wins (explicit user / install.py thread); launcher.db
        # is the fallback for subprocesses that inherit an empty env
        # (notably install.py's sync_knowledge_graph.py spawn on fresh
        # CPU-only installs where the embedding choice lives only in
        # the launcher's app_state). No back-compat fallback ladder —
        # one canonical resolution path per the v0.2.52 "consistent" rule.
        active = _resolve_active_embedding()
        # Choose text model id with provider awareness.
        env_text_model = os.environ.get("EMBEDDING_MODEL", "").strip()
        if env_text_model:
            text_model_id = env_text_model
        elif active == "openai":
            text_model_id = os.environ.get(
                "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
            ).strip() or "text-embedding-3-small"
        else:
            # No EMBEDDING_MODEL env override → derive from the resolved
            # ``active`` value. This is the install.py-Windows-CPU fix:
            # when the launcher seeded ``active=arctic`` but install.py's
            # subprocess inherited an empty EMBEDDING_MODEL, the previous
            # code unconditionally picked DEFAULT_TEXT_MODEL (qwen3) and
            # spent hours embedding on the wrong backend.
            text_model_id = _model_id_for_active(active)

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
                + ". See ~/.claude/metrics/embedding_failures.jsonl for details.",
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
        """v0.2.73 C-9: retry a provider embed ONCE after a short delay when
        the backend answered HTTP 503.

        The code-embed FastAPI service (and Ollama, briefly) return 503 while
        a model is (re)loading — a transient that previously failed the whole
        embed call chain on the first request of a cold session (the C-1
        trigger). ONE retry with a small delay absorbs the common case
        without masking a genuinely-down backend (a second 503 re-raises).
        Non-503 errors re-raise immediately — no behaviour change.

        Delay tunable via ``VCO_EMBED_503_RETRY_DELAY`` (seconds, default 2;
        malformed → default — a knob must not be a kill switch).
        """
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 — inspect, then re-raise
            if "503" not in str(exc):
                raise
            try:
                delay = float(os.getenv("VCO_EMBED_503_RETRY_DELAY", "2"))
            except (TypeError, ValueError):
                delay = 2.0
            if delay < 0:
                delay = 0.0
            logger.info(
                "Embed backend returned 503 (model loading?): %s — "
                "retrying once after %.1fs", exc, delay,
            )
            time.sleep(delay)
            return fn(*args)

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
        """
        if not texts:
            return []
        if "openai" in self._text_slot:
            return self._retry_once_on_503(
                self.openai.embed_batch, self.text_model_id, texts
            )
        return self._retry_once_on_503(
            self.ollama.embed_batch, self.text_model_id, texts
        )

    def embed_code_batch(self, codes: list[str]) -> list[list[float]]:
        """Batched code embedding. Empty input → empty output.

        Routes to CodeEmbed service when slot is ``codesage_embed`` or
        ``jina_embed`` AND the service is reachable; falls back to
        Ollama (which can serve jina or qwen3) when the service is down.
        OpenAI goes through ``openai`` adapter directly.
        """
        if not codes:
            return []
        if "openai" in self._code_slot:
            return self._retry_once_on_503(
                self.openai.embed_batch, self.code_model_id, codes
            )
        if self._code_slot in ("codesage_embed", "jina_embed"):
            # Try service first; on failure fall back to Ollama using the
            # configured code model id. The fallback is best-effort —
            # if Ollama doesn't have a matching model the call will fail
            # cleanly and the caller's exception handler kicks in.
            if self.codeembed.is_reachable():
                return self._retry_once_on_503(self.codeembed.embed_batch, codes)
        return self._retry_once_on_503(
            self.ollama.embed_batch, self.code_model_id, codes
        )

    # ---- multi-slot writes --------------------------------------------

    def embed_text_all_configured(self, text: str) -> dict[str, list[float]]:
        """Embed ``text`` into the configured text slot(s).

        Returns ``{slot_name: vector}``. ALWAYS includes the active slot
        (``self._text_slot``). The SECONDARY enrichment slots (qwen3,
        openai) are added only when ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` is
        enabled (v0.2.71 Piece 5c — default OFF).

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
        # Active backend — ALWAYS written (this is the slot reads target).
        try:
            result[self._text_slot] = self._embed_text_via_active(text)
        except Exception as exc:
            logger.warning("Active text backend failed: %s", exc)

        # v0.2.71 Piece 5c: secondary enrichment slots are opt-in (default
        # OFF). When disabled, return only the active slot above.
        if not _resolve_write_all_slots():
            return result

        # qwen3 fallback if not already the active slot
        if self._text_slot != "qwen3_embed" and self.ollama.is_reachable():
            try:
                result["qwen3_embed"] = self.ollama.embed(
                    DEFAULT_TEXT_MODEL, text
                )
            except Exception as exc:
                logger.warning("qwen3 fallback embedding failed: %s", exc)
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
                    result["openai_text_embed"] = self.openai.embed(
                        openai_model, text
                    )
                except Exception as exc:
                    logger.warning("OpenAI fallback embedding failed: %s", exc)
        return result

    def embed_code_all_configured(self, code: str) -> dict[str, list[float]]:
        """Embed ``code`` into the configured code slot(s).

        Mirrors ``embed_text_all_configured`` (v0.2.71 Piece 5c): ALWAYS
        writes the active code slot; the secondary slots (codesage, openai)
        are added only when ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` is enabled
        (default OFF). With the toggle off, returns a single-entry
        ``{active_code_slot: vector}`` dict — a valid named-vector write.
        """
        result: dict[str, list[float]] = {}
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
                result["codesage_embed"] = self.codeembed.embed(code)
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
        """
        if "openai" in self._text_slot:
            return self.openai.embed(
                _to_openai_api_model(self.text_model_id), text
            )
        return self.ollama.embed(self.text_model_id, text)

    def _embed_code_via_active(self, code: str) -> list[float]:
        """Route a single code embed to the configured backend.

        For codesage_embed / jina_embed slots, prefer the FastAPI service
        when reachable; fall back to Ollama otherwise. OpenAI path applies
        the same catalog-id-prefix strip as ``_embed_text_via_active``.
        """
        if "openai" in self._code_slot:
            return self.openai.embed(
                _to_openai_api_model(self.code_model_id), code
            )
        if self._code_slot in ("codesage_embed", "jina_embed"):
            if self.codeembed.is_reachable():
                return self.codeembed.embed(code)
        return self.ollama.embed(self.code_model_id, code)

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
        code_embed_url = (
            code_embed_url
            or os.environ.get("CODE_EMBED_SERVICE_URL", DEFAULT_CODE_EMBED_URL).strip()
            or DEFAULT_CODE_EMBED_URL
        )
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
            valid_for_small = oa.validate("text-embedding-3-small")
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
