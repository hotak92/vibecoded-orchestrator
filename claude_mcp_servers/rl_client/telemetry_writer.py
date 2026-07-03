# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RLTelemetryWriter — fan-out RL events to launcher.db (via hub) + upload queue.

v0.2.47 RL-6c (2026-06-04) — HARD CUTOVER from JSONL to launcher.db.
Pre-v0.2.47 design wrote a local ``~/.claude/retrieval_rl_data/rl_events.jsonl``
file via ``rl_logger.RLDataLogger``. v0.2.47 replaces that path with a
hub HTTP POST to ``http://127.0.0.1:<port>/api/v1/rl/events`` (added in
C4 + C5). Reasons:

  * Queryable + indexed (SQLite) rather than flat-file.
  * Cross-project dashboards: the launcher Identity tab can join against
    the ``projects`` table for per-project event-rate displays.
  * Single source of truth: offline_trainer reads via the hub's GET
    endpoint, NOT by re-opening the JSONL.
  * Preserves the launcher's single-writer architectural rule (Python
    never opens launcher.db directly — see
    ``vco_lib/config_projection.py:488-491`` + the KG node
    ``launcher-hub-single-writer-principle``).

Two-channel design (v0.2.47):

  1. **Hub write** (always-on by default; user-opt-out via the
     Preferences "Collect retrieval data locally" toggle, which writes
     ``RL_LOCAL_LOGGING_DISABLED=true`` to ``.claude/env``).
     Calls ``hub_writer.post_rl_event`` (commit C5) — soft-fails on
     hub unreachable, missing token, etc. Events lost in those cases
     are LOST (per the locked decision 2026-06-04: no retry queue,
     no JSONL fallback). The hub auto-starts on every Claude Code
     session via ``session-start-ensure-hub.sh``, bounding the
     down-window to "user explicitly stopped the hub".

  2. **Upload queue** (opt-in only; gated on ``consent.rl_data ==
     True`` from ``~/.vibecoded/config.json``). Publishes the same
     event payload to the existing ``VCThelpers.telemetry.queue``
     SQLite queue, which the uploader batch-sends to Supabase when
     consented. The Supabase-side schema for these payloads
     (``rl_retrieval`` / ``rl_citations`` event_types) has not been
     verified end-to-end as of v0.2.47 ship; the payload builders
     here already include the v3 fields so a future Supabase
     migration only needs to add columns, not reshape the writer.
     Track as v0.2.48 work — see SELF-HANDOFF v2 §"What's NOT in scope".

Migration of the historical 700 MB JSONL corpus at
``~/.claude/retrieval_rl_data/rl_events.jsonl`` happens via the C9
one-shot script, NOT through this writer.

The ``log_retrieval`` and ``log_citations`` signatures stay backwards-
compatible — callers pre-v0.2.47 still invoke
``writer.log_retrieval(task_id=..., task_type=..., ...)``; the
implementation switched out from under them.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .hub_writer import post_rl_event
from .rl_logger import RLDataLogger

logger = logging.getLogger(__name__)


# Env var: when set to a truthy value, even local JSONL writes are
# skipped (events are lost). Surfaced in Preferences UI → "Collect
# retrieval data locally" toggle (off → writes
# "RL_LOCAL_LOGGING_DISABLED=true" to the project's ``.claude/env``).
# Default empty/false = collect.
# Name preserved from the pre-v0.2.47 JSONL design for back-compat with
# users who already set it in `.claude/env`; the env reads "local" but
# now gates the launcher.db hub write — same opt-out semantics
# (user-controlled "don't record my retrievals").
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
    """Fan-out RL retrieval/citation events to launcher.db (via hub) + upload queue.

    v0.2.47 RL-6c (2026-06-04): the local write target is now the
    launcher's SQLite ``rl_events`` table reached via the hub's
    ``POST /api/v1/rl/events`` route, NOT the pre-v0.2.47 JSONL file.
    The ``log_retrieval`` / ``log_citations`` signatures are unchanged
    so callers in ``weaviate_mcp/server.py`` keep working without edits.

    Args:
        project: Project name tag written to every event. Maps to the
            v3 hub envelope's ``project_name`` column.
        project_id: Optional FK to ``projects.id``. NULL for free-tier
            installs that haven't registered the workspace with the
            launcher.
        embedding_source: Tag (qwen3 / arctic / openai / codesage / legacy).
        embedding_dim: Vector dim of the active embedding.
        embedding_model: Full model id (for log forensics).
        upload_event_type_retrieval / upload_event_type_citations:
            Override the queue event_type strings (tests).
        hub_post_fn: Override for the hub POST callable. Defaults to
            ``hub_writer.post_rl_event``. Tests inject a stub so they
            can assert what payload would have been written without
            standing up a real hub server.
    """

    def __init__(
        self,
        *,
        project: str = "",
        project_id: Optional[str] = None,
        embedding_source: str = "",
        embedding_dim: int = 0,
        embedding_model: str = "",
        upload_event_type_retrieval: str = _EVENT_TYPE_RETRIEVAL,
        upload_event_type_citations: str = _EVENT_TYPE_CITATIONS,
        hub_post_fn=None,
    ) -> None:
        self._project = project
        self._project_id = project_id
        self._embedding_source = embedding_source
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        self._etype_retrieval = upload_event_type_retrieval
        self._etype_citations = upload_event_type_citations
        # Hub POST callable. Lazily resolves to the default at construction
        # time so unit tests can swap it cleanly via the kwarg.
        self._hub_post = hub_post_fn if hub_post_fn is not None else post_rl_event
        # Capture the last envelope written for hub-post stub assertions
        # in tests. None until the first successful (or attempted) write.
        self._last_envelope: Optional[Dict[str, Any]] = None

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
        rl_used: Optional[bool] = None,
        extras: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a retrieval event to launcher.db (via hub) + (if consented) upload queue.

        v0.2.47 RL-6c: the local write target switched from JSONL to the
        launcher's SQLite ``rl_events`` table. The hub validates the
        envelope shape; payload_json carries the full v3 event JSON
        verbatim and is what the offline trainer reads back via the
        hub's GET endpoint.

        ``failure_mode`` and ``failed_collections`` are v0.2.24 additions
        (RL-defect-2026-05-22): when the fan-out hits a degraded mode
        (e.g. all collections schema-failed), callers pass nodes=[] +
        failure_mode so the offline trainer can filter the event out
        of training-pair construction while still using it as a
        query-distribution signal.
        """
        # Hub write (gated on user opt-out env). Soft-fail: a False return
        # from post_rl_event means hub unreachable / missing token / 5xx.
        # Per the locked decision 2026-06-04, lost events stay lost
        # (no retry queue, no JSONL fallback).
        if not _local_logging_disabled():
            try:
                event = self._build_v3_retrieval_event(
                    task_id=task_id,
                    task_type=task_type,
                    query=query,
                    nodes=nodes,
                    session_id=session_id,
                    query_emb=query_emb,
                    failure_mode=failure_mode,
                    failed_collections=failed_collections,
                    rl_used=rl_used,
                    extras=extras,
                )
                envelope = self._wrap_for_hub("retrieval", task_id, task_type, event)
                self._last_envelope = envelope
                self._hub_post(envelope)
            except Exception as exc:
                logger.debug("RLTelemetryWriter: hub log_retrieval failed (%s)", exc)

        # Upload publish (gated on consent.rl_data). v0.2.47 note:
        # the Supabase end-to-end wiring for these payloads has not been
        # verified — the payload builder includes the v3 fields so a
        # future Supabase migration just adds columns. Track as
        # v0.2.48 work.
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
                rl_used=rl_used,
                extras=extras,
            )
            _enqueue(self._etype_retrieval, payload)

    def log_citations(
        self,
        task_id: str,
        task_type: str,
        citations: Dict[str, Optional[bool]],
        cosine_sims: Optional[Dict[str, float]] = None,
        literal_cited: Optional[Dict[str, bool]] = None,
        cross_encoder_cited: Optional[Dict[str, bool]] = None,
        answer_text: Optional[str] = None,
        session_id: str = "",
        fire_reason: str = "",
        window_tokens: int = 0,
    ) -> None:
        """Log a citation event to launcher.db (via hub) + (if consented) upload queue.

        v3 fields ``literal_cited`` and ``cross_encoder_cited`` are optional
        per-node boost-flag dicts consumed by
        ``vco_lib.rl_training_targets.compute_unified_targets``. ``cosine_sims``
        stays RAW (no bonuses pre-applied) so the formula is replayable
        offline if coefficients are retuned.

        ``answer_text`` (v3+ optional) reserves a field for the agent's full
        answer so future versions can opt-in to logging answers for offline
        multi-model training without a schema bump. v0.2.9 leaves this None
        (privacy/size); None ⇒ the field is omitted from the event entirely.

        v0.2.73 riders (all optional, additive):
          * ``session_id`` (RL-9) — pre-RL-9 citation events carried NO
            session_id, breaking session-grouped analyses on the citation
            side. Empty ⇒ resolved via the 3-layer env chain at write time.
          * ``fire_reason`` + ``window_tokens`` (RL-6) — the monitor logged
            these only to logger.info; now they land in the stored event so
            the ≥25k-token gate distribution is auditable from data.
        """
        if not session_id:
            # RL-9: same 3-layer resolution the retrieval side uses. Local
            # import — telemetry_emit lazy-imports back toward server.py.
            try:
                from .telemetry_emit import resolve_session_id

                session_id = resolve_session_id("")
            except Exception:  # noqa: BLE001 — never break a citation write
                session_id = ""

        if not _local_logging_disabled():
            try:
                event = self._build_v3_citation_event(
                    task_id=task_id,
                    task_type=task_type,
                    citations=citations,
                    cosine_sims=cosine_sims,
                    literal_cited=literal_cited,
                    cross_encoder_cited=cross_encoder_cited,
                    answer_text=answer_text,
                    session_id=session_id,
                    fire_reason=fire_reason,
                    window_tokens=window_tokens,
                )
                envelope = self._wrap_for_hub("citation", task_id, task_type, event)
                self._last_envelope = envelope
                self._hub_post(envelope)
            except Exception as exc:
                logger.debug("RLTelemetryWriter: hub log_citations failed (%s)", exc)

        if _upload_consent_granted():
            payload = self._build_citation_payload(
                task_id=task_id,
                task_type=task_type,
                citations=citations,
                cosine_sims=cosine_sims,
                literal_cited=literal_cited,
                cross_encoder_cited=cross_encoder_cited,
                answer_text=answer_text,
                session_id=session_id,
                fire_reason=fire_reason,
                window_tokens=window_tokens,
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
        rl_used: Optional[bool] = None,
        extras: Optional[Dict[str, Any]] = None,
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
            # v3+: best-chunk vector (renamed from `emb` for v3 disambiguation)
            if n.get("n_emb"):
                rec["n_emb"] = list(n["n_emb"])
            # v3+: MAX_LINKED packed linked-slot embeddings
            if n.get("linked_embs"):
                rec["linked_embs"] = [list(e) for e in n["linked_embs"] if e]
            if n.get("linked_type_names"):
                rec["linked_type_names"] = [str(t) for t in n["linked_type_names"]]
            if n.get("node_type"):
                rec["node_type"] = str(n["node_type"])
            for field in ("cos_qn", "cos_ql", "cos_nl"):
                val = n.get(field)
                if val is not None:
                    rec[field] = float(val)
            # v0.2.73 RL-1/RL-7: additive, no PII (ranks + counts only).
            if n.get("shown_rank") is not None:
                rec["shown_rank"] = int(n["shown_rank"])
            if n.get("chunks_matched") is not None:
                rec["chunks_matched"] = int(n["chunks_matched"])
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
        if rl_used is not None:
            payload["rl_used"] = bool(rl_used)
        if extras:
            payload["extras"] = dict(extras)
        return payload

    def _build_citation_payload(
        self,
        *,
        task_id: str,
        task_type: str,
        citations: Dict[str, Optional[bool]],
        cosine_sims: Optional[Dict[str, float]],
        literal_cited: Optional[Dict[str, bool]] = None,
        cross_encoder_cited: Optional[Dict[str, bool]] = None,
        answer_text: Optional[str] = None,
        session_id: str = "",
        fire_reason: str = "",
        window_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Build the queue-bound payload for a citation event.

        v0.2.40 F3: includes the full embedding triple
        (embedding_source, embedding_dim, embedding_model) — mirrors
        the local JSONL citation shape and the retrieval-event shape.
        Lets the offline RL pipeline anchor the citation event by its
        own embedding triple if the paired retrieval event was dropped
        by the reader's embedding-triple filter (historically the JSONL
        training_loader, retired v0.2.73 RL-8; today the DB-only
        offline_trainer path).

        v3 (v0.2.47): adds ``literal_cited`` + ``cross_encoder_cited``
        per-node boost-flag dicts. Stored as separate fields (NOT baked
        into ``cosine_sims``) so historical events stay replayable when
        the bonus coefficients are retuned.
        """
        payload: Dict[str, Any] = {
            "schema_version": RLDataLogger.SCHEMA_VERSION,
            "project": self._project,
            "task_id": task_id,
            "task_type": task_type,
            "embedding_source": self._embedding_source,
            "embedding_dim": self._embedding_dim,
            "embedding_model": self._embedding_model,
            "citations": {
                title: (bool(cited) if cited is not None else None)
                for title, cited in citations.items()
            },
        }
        if cosine_sims:
            payload["cosine_sims"] = {t: float(v) for t, v in cosine_sims.items()}
        if literal_cited:
            payload["literal_cited"] = {t: bool(v) for t, v in literal_cited.items()}
        if cross_encoder_cited:
            payload["cross_encoder_cited"] = {
                t: bool(v) for t, v in cross_encoder_cited.items()
            }
        if answer_text is not None:
            payload["answer_text"] = str(answer_text)
        if session_id:
            payload["session_id"] = str(session_id)
        if fire_reason:
            payload["fire_reason"] = str(fire_reason)
        if window_tokens:
            payload["window_tokens"] = int(window_tokens)
        return payload

    # ---- v3 hub event builders (v0.2.47 RL-6c) -----------------------

    def _build_v3_retrieval_event(
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
        rl_used: Optional[bool] = None,
        extras: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the v3 retrieval event JSON stored in launcher.db's payload_json.

        Shape matches the SELF-HANDOFF v2 spec § "v3 retrieval event payload".
        Per-node records carry the same field set as ``_build_retrieval_payload``
        (the consented-upload queue payload) PLUS the raw query text — the
        hub keeps query strings because launcher.db is local-only, NOT
        uploaded. Supabase still strips them in the queue payload.

        v0.2.73 additions (all optional / additive): event-level ``rl_used``
        (RL-1 — did the rerank RPC run) + ``extras`` (RL-2 — code-path
        diagnostics); per-node ``shown_rank`` (RL-1 — post-rerank shown
        order), ``chunks_matched`` / ``best_chunk_number`` (RL-7) and the
        code-path fields ``collection`` / ``file_path`` / ``rerank_score`` /
        ``boost_delta`` / ``boost_signals`` (RL-2).
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
            if n.get("n_emb"):
                rec["n_emb"] = list(n["n_emb"])
            if n.get("linked_embs"):
                rec["linked_embs"] = [list(e) for e in n["linked_embs"] if e]
            if n.get("linked_type_names"):
                rec["linked_type_names"] = [str(t) for t in n["linked_type_names"]]
            if n.get("node_type"):
                rec["node_type"] = str(n["node_type"])
            if n.get("links"):
                rec["links"] = [str(lnk) for lnk in n["links"][:10]]
            for field in ("cos_qn", "cos_ql", "cos_nl"):
                val = n.get(field)
                if val is not None:
                    rec[field] = float(val)
            if n.get("shown_rank") is not None:
                rec["shown_rank"] = int(n["shown_rank"])
            if n.get("chunks_matched") is not None:
                rec["chunks_matched"] = int(n["chunks_matched"])
            if n.get("best_chunk_number") is not None:
                rec["best_chunk_number"] = int(n["best_chunk_number"])
            # RL-2 code-path node fields (present only on code retrievals).
            if n.get("collection"):
                rec["collection"] = str(n["collection"])
            if n.get("file_path"):
                rec["file_path"] = str(n["file_path"])
            if n.get("rerank_score") is not None:
                rec["rerank_score"] = float(n["rerank_score"])
            if n.get("boost_delta") is not None:
                rec["boost_delta"] = float(n["boost_delta"])
            if n.get("boost_signals"):
                # code_ranking stamps signals as a dict; keep the shape
                # verbatim (payload_json is stored as-is).
                _sig = n["boost_signals"]
                rec["boost_signals"] = (
                    dict(_sig) if isinstance(_sig, dict) else list(_sig)
                )
            node_records.append(rec)

        event: Dict[str, Any] = {
            "event": "retrieval",
            "schema_version": RLDataLogger.SCHEMA_VERSION,
            "ts": _now_iso(),
            "project": self._project,
            "task_id": task_id,
            "session_id": session_id,
            "task_type": task_type,
            "query": (query or "")[:2000],
            "embedding_source": self._embedding_source,
            "embedding_dim": self._embedding_dim,
            "embedding_model": self._embedding_model,
            "nodes": node_records,
        }
        if query_emb is not None:
            event["query_emb"] = list(query_emb)
        if failure_mode:
            event["failure_mode"] = str(failure_mode)
        if failed_collections:
            event["failed_collections"] = [
                str(c) for c in list(failed_collections)[:32]
            ]
        if rl_used is not None:
            event["rl_used"] = bool(rl_used)
        if extras:
            event["extras"] = dict(extras)
        return event

    def _build_v3_citation_event(
        self,
        *,
        task_id: str,
        task_type: str,
        citations: Dict[str, Optional[bool]],
        cosine_sims: Optional[Dict[str, float]],
        literal_cited: Optional[Dict[str, bool]] = None,
        cross_encoder_cited: Optional[Dict[str, bool]] = None,
        answer_text: Optional[str] = None,
        session_id: str = "",
        fire_reason: str = "",
        window_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Build the v3 citation event JSON stored in launcher.db's payload_json."""
        event: Dict[str, Any] = {
            "event": "citation",
            "schema_version": RLDataLogger.SCHEMA_VERSION,
            "ts": _now_iso(),
            "project": self._project,
            "task_id": task_id,
            "task_type": task_type,
            "embedding_source": self._embedding_source,
            "embedding_dim": self._embedding_dim,
            "embedding_model": self._embedding_model,
            "citations": {
                title: (bool(cited) if cited is not None else None)
                for title, cited in citations.items()
            },
        }
        if cosine_sims:
            event["cosine_sims"] = {
                t: round(float(v), 4) for t, v in cosine_sims.items()
            }
        if literal_cited:
            event["literal_cited"] = {t: bool(v) for t, v in literal_cited.items()}
        if cross_encoder_cited:
            event["cross_encoder_cited"] = {
                t: bool(v) for t, v in cross_encoder_cited.items()
            }
        if answer_text is not None:
            event["answer_text"] = str(answer_text)
        # v0.2.73 RL-9 / RL-6 riders (optional, additive).
        if session_id:
            event["session_id"] = str(session_id)
        if fire_reason:
            event["fire_reason"] = str(fire_reason)
        if window_tokens:
            event["window_tokens"] = int(window_tokens)
        return event

    def _wrap_for_hub(
        self,
        event_type: str,
        task_id: str,
        task_type: str,
        event_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Wrap a v3 event in the hub POST envelope shape.

        Matches ``vct-hub/src/rl_events_api::PostEventBody``. The hub
        denormalizes indexed columns out of the envelope; ``payload_json``
        carries the full v3 event JSON verbatim so downstream readers
        (offline_trainer via the hub's GET endpoint) get bytewise-identical
        replayable events.
        """
        return {
            "event_type": event_type,
            "schema_version": int(event_json.get("schema_version") or RLDataLogger.SCHEMA_VERSION),
            "ts_ms": int(time.time() * 1000),
            "project_id": self._project_id,
            "project_name": self._project or None,
            "task_id": task_id,
            "task_type": task_type,
            "embedding_source": self._embedding_source or None,
            "embedding_dim": self._embedding_dim or None,
            "embedding_model": self._embedding_model or None,
            "payload_json": json.dumps(event_json),
        }


def _now_iso() -> str:
    """Local-clock ISO 8601 timestamp matching the pre-v0.2.47 RLDataLogger format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
