# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RLTelemetryWriter — fan-out RL events to local JSONL + upload queue.

Two-channel design:

  1. **Local JSONL** (always-on by default; user-opt-out via the
     Preferences "Collect retrieval data locally" toggle, which writes
     ``RL_LOCAL_LOGGING_DISABLED=true`` to ``.claude/env``).
     Wraps ``rl_logger.RLDataLogger`` — keeps the v0.2.x free-tier
     behavior unchanged: every retrieval + citation event is appended
     to ``~/.claude/retrieval_rl_data/rl_events.jsonl`` so when the
     user upgrades to Pro the historical training data is already
     there.

  2. **Upload queue** (opt-in only; gated on ``consent.rl_data ==
     True`` from ``~/.vibecoded/config.json``). Publishes the same
     event payload to the existing ``VCThelpers.telemetry.queue``
     SQLite queue, which the uploader batch-sends to the central
     hub when consented.

The writer is **graceful under failure**:

  * Local opt-out via env → skip JSONL writes (no-op return).
  * VCThelpers.telemetry not importable (lean install) → upload-queue
    side becomes a no-op; local writes still happen.
  * Consent denied → no enqueue, but local writes still happen.
  * Either side raises → log at debug, never propagate.

The ``log_retrieval`` and ``log_citations`` signatures match
``RLDataLogger`` exactly so callers can swap one for the other
without touching arguments.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .rl_logger import RLDataLogger

logger = logging.getLogger(__name__)


# Env var: when set to a truthy value, even local JSONL writes are
# skipped. Surfaced in Preferences UI → "Collect retrieval data
# locally" toggle (off → writes "RL_LOCAL_LOGGING_DISABLED=true" to
# the project's ``.claude/env``). Default empty/false = collect.
_LOCAL_OPT_OUT_ENV = "RL_LOCAL_LOGGING_DISABLED"

# Telemetry event_type used for queue publishes. Hub-side schemas
# key on this string; do not rename without coordinating server-side.
_EVENT_TYPE_RETRIEVAL = "rl_retrieval"
_EVENT_TYPE_CITATIONS = "rl_citations"


def _local_logging_disabled() -> bool:
    """True iff ``RL_LOCAL_LOGGING_DISABLED`` env is set to a truthy value."""
    v = os.environ.get(_LOCAL_OPT_OUT_ENV, "").strip().lower()
    return v in ("true", "1", "yes", "on")


def _upload_consent_granted() -> bool:
    """Whether the user has consented to uploading RL training data.

    Reads ``~/.vibecoded/config.json`` via
    ``VCThelpers.telemetry.consent.load_consent`` and returns the
    ``rl_data`` flag. Soft-fails to False when the module isn't
    importable (lean install / unit-test isolation).
    """
    try:
        from VCThelpers.telemetry.consent import load_consent
    except Exception:
        return False
    try:
        consent = load_consent()
    except Exception as exc:
        logger.debug("RLTelemetryWriter: load_consent failed (%s)", exc)
        return False
    return bool(consent.get("rl_data", False))


def _enqueue(event_type: str, payload: Dict[str, Any]) -> bool:
    """Publish to the VCThelpers telemetry queue. Soft-fail on import error."""
    try:
        from VCThelpers.telemetry.queue import get_queue
    except Exception:
        return False
    try:
        return bool(get_queue().enqueue(event_type, payload))
    except Exception as exc:
        logger.debug("RLTelemetryWriter: enqueue failed (%s)", exc)
        return False


class RLTelemetryWriter:
    """Fan-out RL retrieval/citation events to local JSONL + upload queue.

    Args:
        log_path: Override for the local JSONL path. Defaults to the
            ``RLDataLogger.DEFAULT_PATH`` (~/.claude/retrieval_rl_data/
            rl_events.jsonl).
        project: Project name tag written to every event.
        embedding_source: Tag (qwen3 / arctic / openai / codesage / legacy).
        embedding_dim: Vector dim of the active embedding.
        embedding_model: Full model id (for log forensics).
        upload_event_type_retrieval / upload_event_type_citations:
            Override the queue event_type strings (tests).
    """

    def __init__(
        self,
        *,
        log_path: Optional[Path] = None,
        project: str = "",
        embedding_source: str = "",
        embedding_dim: int = 0,
        embedding_model: str = "",
        upload_event_type_retrieval: str = _EVENT_TYPE_RETRIEVAL,
        upload_event_type_citations: str = _EVENT_TYPE_CITATIONS,
    ) -> None:
        self._local = RLDataLogger(
            log_path=log_path,
            project=project,
            embedding_source=embedding_source,
            embedding_dim=embedding_dim,
            embedding_model=embedding_model,
        )
        self._project = project
        self._embedding_source = embedding_source
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        self._etype_retrieval = upload_event_type_retrieval
        self._etype_citations = upload_event_type_citations

    @property
    def log_path(self) -> Path:
        """Resolved path of the local JSONL log."""
        return self._local.log_path

    # ---- public API ---------------------------------------------------

    def log_retrieval(
        self,
        task_id: str,
        task_type: str,
        query: str,
        nodes: List[Dict[str, Any]],
        session_id: str = "",
        query_emb: Optional[List[float]] = None,
        failure_mode: Optional[str] = None,
        failed_collections: Optional[List[str]] = None,
    ) -> None:
        """Log a retrieval event to local JSONL + (if consented) upload queue.

        Signature matches ``RLDataLogger.log_retrieval`` exactly so
        callers can swap the two transparently.

        ``failure_mode`` and ``failed_collections`` are v0.2.24 additions
        (RL-defect-2026-05-22): when the fan-out hits a degraded mode
        (e.g. all collections schema-failed), callers pass nodes=[] +
        failure_mode so the offline trainer can filter the event out
        of training-pair construction while still using it as a
        query-distribution signal.
        """
        # Local write (gated on user opt-out env)
        if not _local_logging_disabled():
            try:
                self._local.log_retrieval(
                    task_id=task_id,
                    task_type=task_type,
                    query=query,
                    nodes=nodes,
                    session_id=session_id,
                    query_emb=query_emb,
                    failure_mode=failure_mode,
                    failed_collections=failed_collections,
                )
            except Exception as exc:
                logger.debug("RLTelemetryWriter: local log_retrieval failed (%s)", exc)

        # Upload publish (gated on consent.rl_data)
        if _upload_consent_granted():
            payload = self._build_retrieval_payload(
                task_id=task_id,
                task_type=task_type,
                query=query,
                nodes=nodes,
                session_id=session_id,
                query_emb=query_emb,
                failure_mode=failure_mode,
                failed_collections=failed_collections,
            )
            _enqueue(self._etype_retrieval, payload)

    def log_citations(
        self,
        task_id: str,
        task_type: str,
        citations: Dict[str, Optional[bool]],
        cosine_sims: Optional[Dict[str, float]] = None,
    ) -> None:
        """Log a citation event to local JSONL + (if consented) upload queue."""
        if not _local_logging_disabled():
            try:
                self._local.log_citations(
                    task_id=task_id,
                    task_type=task_type,
                    citations=citations,
                    cosine_sims=cosine_sims,
                )
            except Exception as exc:
                logger.debug("RLTelemetryWriter: local log_citations failed (%s)", exc)

        if _upload_consent_granted():
            payload = self._build_citation_payload(
                task_id=task_id,
                task_type=task_type,
                citations=citations,
                cosine_sims=cosine_sims,
            )
            _enqueue(self._etype_citations, payload)

    # ---- payload builders --------------------------------------------

    def _build_retrieval_payload(
        self,
        *,
        task_id: str,
        task_type: str,
        query: str,
        nodes: List[Dict[str, Any]],
        session_id: str,
        query_emb: Optional[List[float]],
        failure_mode: Optional[str] = None,
        failed_collections: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build the queue-bound payload for a retrieval event.

        Mirrors the local JSONL shape but **omits the raw query text**
        — the hub-side schema treats query as PII. Embeddings + scores
        are kept; titles are kept (KG titles are non-secret).

        ``failure_mode`` and ``failed_collections`` are v0.2.24 additions
        (RL-defect-2026-05-22): failure tags surface in hub-side
        telemetry so we can monitor retrieval-defect rates across
        installs. The fields are emitted in both the local JSONL and
        the consented queue payload (no PII in either — failure_mode
        is a fixed tag string and failed_collections are class names).
        """
        node_records: List[Dict[str, Any]] = []
        for n in nodes:
            rec: Dict[str, Any] = {
                "title": str(n.get("title", "")),
                "score": float(n.get("score", 0.0)),
                "tier": str(n.get("tier", "top_k")),
            }
            if n.get("emb"):
                rec["emb"] = list(n["emb"])
            for field in ("cos_qn", "cos_ql", "cos_nl"):
                val = n.get(field)
                if val is not None:
                    rec[field] = float(val)
            node_records.append(rec)

        payload: Dict[str, Any] = {
            "schema_version": RLDataLogger.SCHEMA_VERSION,
            "project": self._project,
            "task_id": task_id,
            "session_id": session_id,
            "task_type": task_type,
            "embedding_source": self._embedding_source,
            "embedding_dim": self._embedding_dim,
            "embedding_model": self._embedding_model,
            "nodes": node_records,
            # query intentionally omitted from queue payload (PII).
            "query_length": len(query or ""),
        }
        if query_emb is not None:
            payload["query_emb"] = list(query_emb)
        if failure_mode:
            payload["failure_mode"] = str(failure_mode)
        if failed_collections:
            payload["failed_collections"] = [
                str(c) for c in list(failed_collections)[:32]
            ]
        return payload

    def _build_citation_payload(
        self,
        *,
        task_id: str,
        task_type: str,
        citations: Dict[str, Optional[bool]],
        cosine_sims: Optional[Dict[str, float]],
    ) -> Dict[str, Any]:
        """Build the queue-bound payload for a citation event."""
        payload: Dict[str, Any] = {
            "schema_version": RLDataLogger.SCHEMA_VERSION,
            "project": self._project,
            "task_id": task_id,
            "task_type": task_type,
            "embedding_source": self._embedding_source,
            "citations": {
                title: (bool(cited) if cited is not None else None)
                for title, cited in citations.items()
            },
        }
        if cosine_sims:
            payload["cosine_sims"] = {t: float(v) for t, v in cosine_sims.items()}
        return payload
