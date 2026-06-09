# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Canonical emit path for RL retrieval telemetry — V52-J (v0.2.52).

Every KG-search entry point in the orchestrator (direct MCP, MCP-from-subagent,
PreToolUse hook, CLI script, Python helper) MUST route its retrieval-event
emission through this module. The motivation is to eliminate per-call-site
divergence in how mandatory fields are resolved + validated.

Pre-v0.2.52 history this fixes:

- ``server.py:_get_rl_telemetry_writer`` constructed ``RLTelemetryWriter``
  without ``project_id`` even though ``_try_resolve_project_config()`` already
  returned it → 100% ``project_id=NULL`` in launcher.db rl_events.
- ``server.py:_rl_cache_and_rerank`` resolved ``session_id`` via
  ``os.getenv("CLAUDE_SESSION_ID", "")`` which is empty in MCP subprocesses
  (Claude Code deliberately does not propagate that env to children) → 99.6%
  ``session_id=""``.
- ``templates/scripts/search_knowledge.py`` had no telemetry write at all →
  Path D-1 silent hole (hooks invoking it produced zero events).

The contract here is INTENDED to be load-bearing: caller passes raw retrieval
inputs, this module owns mandatory-field validation + the 3-layer session_id
resolution + the writer-cache lookup. Adding a new entry point means adding a
call to ``emit_rl_event(...)``, nothing else.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class EmitValidationError(ValueError):
    """Raised when a RetrievalEvent fails canonical-field validation.

    Surfaced as an exception (not silent-fail) so callers see the bug
    immediately during development. Production call sites should wrap in
    ``try / except EmitValidationError`` and log.debug — never let a
    telemetry write break the user's actual KG search.
    """


@dataclass(frozen=True)
class RetrievalEvent:
    """Inputs for a single retrieval emit.

    Frozen + dataclass so callers can't accidentally mutate after
    validation. Validation happens in ``emit_rl_event``; constructing the
    dataclass itself never raises.
    """

    # ---- mandatory fields ----
    query: str
    # query_emb is the vector that drove the search. None is allowed
    # explicitly (rather than []) so the writer can pass it through to
    # the v3 envelope as null — the offline trainer distinguishes
    # "embedding genuinely unavailable" (None) from "zero-length
    # embedding" (which would be a bug). Pre-V52-J server.py passed
    # None on the failure-mode / per-project-disabled paths; preserve
    # that contract.
    query_emb: Optional[list[float]]
    embedding_source: str
    embedding_dim: int
    embedding_model: str
    nodes: list[dict[str, Any]]
    task_id: str
    task_type: str = "mcp_interactive"
    # ---- optional fields ----
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    project: Optional[str] = None
    failure_mode: Optional[str] = None
    failed_collections: list[str] = field(default_factory=list)


def resolve_session_id(arg_session_id: Optional[str] = None) -> str:
    """Resolve session_id via the 3-layer chain.

    Layer 1: caller-supplied argument (highest priority — used by hooks
    that read ``session_id`` from their stdin JSON payload).
    Layer 2: ``VCT_SESSION_ID`` env (orchestrator-owned, hook-exported
    from the stdin payload — see ``pre-edit-context-inject.{sh,ps1}``).
    Layer 3: ``CLAUDE_SESSION_ID`` env (Claude Code's own variable —
    usually empty in MCP/hook subprocesses, kept as a last-resort
    fallback in case future Claude Code releases start populating it).

    Returns empty string if no layer yields a value. Callers downstream
    treat empty-string as "unknown session" and skip session-grouped
    analyses — better than NULL because the schema requires the field.
    """
    if arg_session_id:
        return arg_session_id
    env_vct = os.environ.get("VCT_SESSION_ID", "")
    if env_vct:
        return env_vct
    return os.environ.get("CLAUDE_SESSION_ID", "")


def emit_rl_event(
    ev: RetrievalEvent,
    *,
    writer_factory: Optional[Callable[[], Any]] = None,
) -> bool:
    """Canonical emit. Validates mandatory fields then writes via the
    project's cached ``RLTelemetryWriter``.

    Returns True on success, False on soft-fail (writer unavailable, hub
    unreachable, etc). Raises ``EmitValidationError`` on
    caller-correctable bugs (missing query, dim mismatch).

    ``writer_factory`` is for test injection. Production calls leave it
    None and the module-level resolver lazy-imports
    ``server._get_rl_telemetry_writer`` (the only existing per-(project,
    embedding) writer cache; no point duplicating).
    """
    # ---- validation ----
    # Validation tiers (V52-J Edit 2 refinement 2026-06-09):
    #   STRICT (raise EmitValidationError) — empty query (= no telemetry
    #     value at all) or empty task_id (= writer can't dedupe). These
    #     are genuinely caller-correctable bugs.
    #   SOFT (logger.debug + still write) — empty query_emb in
    #     happy-path mode, or query_emb length != embedding_dim. The
    #     writer's _build_v3_retrieval_event handles None query_emb
    #     gracefully (just omits the field from the v3 envelope), so
    #     the historical "log_retrieval ALWAYS fires" guarantee from
    #     server.py:_rl_cache_and_rerank's pre-V52-J body holds.
    #   SKIPPED — failure_mode set OR nodes empty. Degraded-mode events
    #     legitimately lack a useful query_emb (the offline trainer
    #     filters non-None failure_mode out of training-pair
    #     construction but still uses them as query-distribution +
    #     failure-rate signals).
    if not ev.query:
        raise EmitValidationError("RetrievalEvent.query is empty")
    if not ev.task_id:
        raise EmitValidationError("RetrievalEvent.task_id is empty")
    _is_degraded = bool(ev.failure_mode) or not ev.nodes
    if not _is_degraded:
        # Soft-warn on missing / mismatched query_emb. Do NOT raise —
        # the writer is robust enough to handle it and dropping the
        # write would silently lose telemetry that production paths
        # depend on. Surface as DEBUG so noisy free-tier installs
        # don't flood logs. ``ev.query_emb is None`` is meaningfully
        # different from ``len(ev.query_emb) == 0`` (former = "no
        # embedding available", latter = "zero-length embedding" =
        # caller bug).
        if ev.query_emb is None or len(ev.query_emb) == 0:
            logger.debug(
                "emit_rl_event: happy-path event has empty query_emb "
                "(task_id=%s); writing anyway", ev.task_id,
            )
        elif ev.embedding_dim and len(ev.query_emb) != ev.embedding_dim:
            logger.debug(
                "emit_rl_event: query_emb length %d != embedding_dim %d "
                "(task_id=%s); writing anyway",
                len(ev.query_emb), ev.embedding_dim, ev.task_id,
            )

    # ---- writer resolution ----
    if writer_factory is None:
        writer_factory = _default_writer_factory
    try:
        writer = writer_factory()
    except Exception as exc:
        logger.debug("emit_rl_event: writer factory raised (%s); skipping", exc)
        return False
    if writer is None:
        logger.debug("emit_rl_event: no writer available; skipping")
        return False

    # ---- session_id resolution ----
    session_id = resolve_session_id(ev.session_id)

    # ---- write ----
    # Preserve None vs [] distinction on query_emb (see RetrievalEvent
    # docstring) — list(None) would TypeError, so guard explicitly.
    try:
        writer.log_retrieval(
            task_id=ev.task_id,
            task_type=ev.task_type,
            query=ev.query,
            nodes=ev.nodes,
            session_id=session_id,
            query_emb=(list(ev.query_emb) if ev.query_emb is not None else None),
            failure_mode=ev.failure_mode,
            failed_collections=list(ev.failed_collections),
        )
    except Exception as exc:
        logger.debug("emit_rl_event: log_retrieval raised (%s); soft-fail", exc)
        return False
    return True


def _default_writer_factory():
    """Resolve the project's cached writer via server's existing cache.

    Lazy import to avoid a circular dep at module load time.
    Returns None when the import fails (e.g. running outside the MCP
    process) — callers degrade gracefully.
    """
    try:
        from claude_mcp_servers.weaviate_mcp.server import _get_rl_telemetry_writer

        return _get_rl_telemetry_writer()
    except Exception as exc:
        logger.debug("_default_writer_factory: cannot resolve writer (%s)", exc)
        return None


def new_task_id() -> str:
    """Convenience for callers that don't have an upstream task_id.

    UUID4 chosen for uniqueness without coordination; the writer
    enforces ``task_id`` uniqueness at the DB layer so collisions surface
    immediately if any caller hardcodes a value.
    """
    return uuid.uuid4().hex
