# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Outcome-event emit path for V52-M RL training pairs (v0.2.52).

Companion to ``telemetry_emit.emit_rl_event`` for *outcome* events
(``bash_outcome``, ``edit_outcome``, ``pre_bash``). The retrieval/citation
path was already nailed down in V52-J's chokepoint; outcome events have a
genuinely different payload shape (no nodes list, no query embedding —
they carry exit codes / diff sizes / durations) so they get their own
thin emit helper rather than overloading ``RetrievalEvent``.

Why a separate module rather than extending the writer:

- The existing ``RLTelemetryWriter.log_retrieval`` / ``log_citations``
  signatures are load-bearing for ~6 call sites in server.py +
  search_pipeline. Adding a ``log_outcome`` method bloats the writer
  interface and risks silent shape drift between retrieval vs outcome.
- Outcome events have no embedding context (the writer's
  ``embedding_source`` / ``embedding_dim`` / ``embedding_model`` are
  still relevant for forensics, so we DO populate them from the
  cached writer when available).
- Keeping the emit-helper module-level + thin lets hooks call it
  directly without needing to know about writer caching.

Hub gate (Rust side: ``vct-hub/src/rl_events_api.rs``): the
``POST /api/v1/rl/events`` validator was extended in v0.2.52 to accept
``bash_outcome`` / ``edit_outcome`` / ``pre_bash`` alongside the existing
``retrieval`` / ``citation`` values. Older hubs (v0.2.51 and earlier)
will 400 on these — the writer soft-fails (returns False) and the
caller hook continues without breaking the user's flow.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Recognized outcome event_type values. The Rust hub validates against
# this same set; keep both lists in sync (cross-language parity test
# in tests/test_v52_m_prepost_hooks.py).
OUTCOME_EVENT_TYPES = ("bash_outcome", "edit_outcome", "pre_bash")


def emit_outcome_event(
    *,
    event_type: str,
    task_id: str,
    task_type: str,
    payload: Dict[str, Any],
    session_id: str = "",
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
) -> bool:
    """Emit an outcome event to launcher.db via the hub POST endpoint.

    Args:
        event_type: One of ``bash_outcome`` / ``edit_outcome`` / ``pre_bash``.
            Validates against ``OUTCOME_EVENT_TYPES``.
        task_id: Pairing key. For (pre_bash, bash_outcome) pairs the two
            events share the SAME task_id (written by pre-bash hook,
            read by post-bash hook from the .claude/state/ pairing file).
            For edit_outcome events without a paired pre_edit task_id,
            mint a fresh ``edit_outcome_<uuid>`` — the offline trainer
            JOINs by (session_id, file_path, ts_window) in that case.
        task_type: Free-form tag (typically same as event_type, kept
            as separate column for filter queries).
        payload: Event-specific dict. Serialized to ``payload_json`` in
            the v3 envelope. Schemas:
              * bash_outcome → exit_code (int), output_len (int),
                duration_ms (int), cmd_len (int).
              * edit_outcome → tool_name (str), file_path (str),
                diff_size (int), file_existed_before (bool), ts_ms (int),
                post_check (None | dict).
              * pre_bash → cmd_len (int), query (str truncated), ts_ms (int).
        session_id: Layer-1 input for session resolution (the 3-layer
            chain in telemetry_emit.resolve_session_id matches).
        project_id: Optional FK; soft-fails to NULL.
        project_name: Optional human-readable project tag.

    Returns:
        True on success, False on soft-fail (hub unreachable, writer
        unavailable, gate rejection on older hubs). Never raises —
        outcome telemetry must not break the user's tool flow.
    """
    if event_type not in OUTCOME_EVENT_TYPES:
        # Caller-correctable bug — surface as logger.debug rather than
        # raise (this is hook-invoked code; an exception would crash
        # the hook and break the user's editor flow).
        logger.debug(
            "emit_outcome_event: unknown event_type %r (allowed: %s); skipping",
            event_type, OUTCOME_EVENT_TYPES,
        )
        return False
    if not task_id:
        logger.debug("emit_outcome_event: empty task_id; skipping")
        return False

    # Resolve session_id via the same 3-layer chain retrieval events use.
    try:
        from claude_mcp_servers.rl_client.telemetry_emit import resolve_session_id
        session_id = resolve_session_id(session_id)
    except Exception:
        # Fallback: take session_id arg as-is; empty if absent.
        session_id = session_id or ""

    # Build the v3 envelope. Mirrors ``RLTelemetryWriter._wrap_for_hub``
    # so the launcher.db insert path is bytewise-identical to retrieval/
    # citation events. ``payload_json`` carries the full event JSON
    # verbatim — the offline trainer reads it back via the hub GET API.
    inner = {
        "event": event_type,
        "schema_version": _SCHEMA_VERSION,
        "ts": _now_iso(),
        "task_id": task_id,
        "task_type": task_type,
        "session_id": session_id,
        "payload": payload,
    }
    envelope = {
        "event_type": event_type,
        "schema_version": _SCHEMA_VERSION,
        "ts_ms": int(time.time() * 1000),
        "project_id": project_id,
        "project_name": project_name,
        "task_id": task_id,
        "task_type": task_type,
        # Outcome events have no embedding context per se, but we still
        # tag the writer's cached source/model so cross-collection joins
        # work in the offline trainer (e.g. "bash_outcomes for the
        # arctic2 cohort"). Resolved best-effort from the cached writer.
        "embedding_source": None,
        "embedding_dim": None,
        "embedding_model": None,
        "payload_json": json.dumps(inner),
    }
    _populate_embedding_tags_from_cached_writer(envelope)

    return _post_envelope(envelope)


# ---- helpers --------------------------------------------------------------

_SCHEMA_VERSION = 3  # v3 hub envelope — matches RLDataLogger.SCHEMA_VERSION


def _now_iso() -> str:
    """ISO 8601 local-time stamp matching ``telemetry_writer._now_iso``."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _populate_embedding_tags_from_cached_writer(envelope: Dict[str, Any]) -> None:
    """Best-effort: copy embedding tags from the cached writer.

    Outcome events don't *use* embeddings, but the indexed
    embedding_source/model columns are useful for filtering when joining
    against retrieval events. Soft-fails when the writer cache is
    unavailable (e.g. hook called from a fresh subprocess).
    """
    try:
        from claude_mcp_servers.weaviate_mcp.server import _get_rl_telemetry_writer
        w = _get_rl_telemetry_writer()
        if w is None:
            return
        envelope["embedding_source"] = getattr(w, "_embedding_source", None) or None
        envelope["embedding_dim"] = getattr(w, "_embedding_dim", None) or None
        envelope["embedding_model"] = getattr(w, "_embedding_model", None) or None
        # Also fill project_id/project_name if the caller didn't.
        if envelope.get("project_id") is None:
            envelope["project_id"] = getattr(w, "_project_id", None)
        if envelope.get("project_name") is None:
            envelope["project_name"] = getattr(w, "_project", None) or None
    except Exception as exc:
        logger.debug("populate_embedding_tags: writer-cache lookup failed (%s)", exc)


def _post_envelope(envelope: Dict[str, Any]) -> bool:
    """Submit the envelope to the hub. Soft-fails on every error path."""
    try:
        from claude_mcp_servers.rl_client.hub_writer import post_rl_event
    except Exception as exc:
        logger.debug("emit_outcome_event: hub_writer import failed (%s)", exc)
        return False
    try:
        return bool(post_rl_event(envelope))
    except Exception as exc:
        logger.debug("emit_outcome_event: post_rl_event raised (%s)", exc)
        return False
