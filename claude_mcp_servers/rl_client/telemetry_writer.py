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
from .rl_logger import RLDataLogger, _round_emb, serialize_node_record

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

# v0.2.73 Concern-A/C — two-level RL telemetry gate. Each opt-out has a GLOBAL
# leg (machine-wide, from launcher.db ``app_state`` re-projected into every
# project's ``.claude/settings.json`` env by ``vco_lib/config_projection.py``)
# and a PER-PROJECT leg (the project's ``.claude/env``). The resolver is
# effective_disabled = global_disabled OR per_project_disabled, so a GLOBAL
# disable overrides ALL projects while a global-enabled state still lets one
# project opt out locally. Both env names MUST match the projected names in
# config_projection.py (``_ENV_RL_*_GLOBAL``).
_LOCAL_OPT_OUT_ENV_GLOBAL = "RL_LOCAL_LOGGING_DISABLED_GLOBAL"
# ONLINE per-answer training opt-out (Concern-C). Per-project ``.claude/env`` +
# machine-global app_state leg. Default ENABLED (both absent → not disabled).
_ONLINE_TRAINING_OPT_OUT_ENV = "RL_ONLINE_TRAINING_DISABLED"
_ONLINE_TRAINING_OPT_OUT_ENV_GLOBAL = "RL_ONLINE_TRAINING_DISABLED_GLOBAL"

# Telemetry event_type used for queue publishes. Hub-side schemas
# key on this string; do not rename without coordinating server-side.
_EVENT_TYPE_RETRIEVAL = "rl_retrieval"
_EVENT_TYPE_CITATIONS = "rl_citations"

# WP-Q item 2 (was R2-11): client-side payload size cap — a PATHOLOGICAL-CASE
# BACKSTOP, not the normal path. The hub's rl_events POST handler
# (vct-hub/src/rl_events_api.rs) now sets an EXPLICIT 16 MiB ``DefaultBodyLimit``
# on the ingest route (raised from axum's 2 MB default, per the user rule "move
# the limit, never the data": embedding-heavy events — a wide code retrieval
# with 2048-dim node vectors + near-chunk embeddings, or an answer-heavy
# dual-write citation — can legitimately exceed 2 MB, and their labels must not
# be lost). This client cap sits JUST UNDER that 16 MiB hub limit so a genuinely
# pathological event still degrades LOUDLY: if serialized bytes exceed the cap we
# DROP the OPTIONAL heavy embedding fields in a documented priority order so the
# CORE event (citations/cosine_sims/scalars — the trainable label itself) always
# survives, emitting a WARNING. Normal events never approach either cap — the
# trim never fires in practice; it exists only so a runaway event never silently
# 413s or produces a well-formed-but-untrainable row. Set below the hub's
# 16 MiB (16,777,216 B) with ~777 KB headroom for the JSON envelope + HTTP
# framing.
_HUB_PAYLOAD_MAX_BYTES_ENV = "RL_HUB_PAYLOAD_MAX_BYTES"
_HUB_PAYLOAD_MAX_BYTES_DEFAULT = 16_000_000  # ~15.26 MiB; hub cap is 16 MiB

# Trim priority (dropped in this ORDER until the payload fits — never the core
# event): NEAR-CHUNK/link embeddings first (per-node ``linked_embs`` — the
# bulkiest field, up to 5+ vectors per node, and the trainer zero-pads when the
# whole field is absent), THEN answer embeddings (``answer_chunk_embs`` —
# needed only for cross-slot label re-derivation). This ORDER was corrected in
# WP-M (linked_embs then answer_chunk_embs; core net inputs never trimmed) —
# do NOT reorder.
#
# NEVER-trim class: alongside the label fields (citations, cosine_sims,
# literal_cited, cross_encoder_cited, scalars), the CORE NET INPUTS are
# untrimmable — ``query_emb`` (slot 0) and each node's own ``emb`` (the
# matched-node vector, entity slot 1). An event without them is well-formed
# but UNTRAINABLE (the sample extractor skips embedless nodes), which is
# worse than a loud 413: the row looks healthy and poisons corpus stats.
# If the event still exceeds the cap after both optional trims, it is posted
# anyway and the hub's 413 surfaces at WARNING via hub_writer.
#
# ``linked_embs`` is only ever deleted WHOLE-FIELD (never element-wise), so
# its index alignment with ``linked_type_names`` can never be corrupted.
_TRIM_STEPS_NODE_EMB = ("linked_embs",)
_TRIM_STEPS_EVENT_EMB = ("answer_chunk_embs",)


def _resolve_hub_payload_max_bytes() -> int:
    """Resolve the payload byte cap from env, else the conservative default.
    A malformed/zero/negative override falls back to the default (a bad tunable
    must never disable the guard)."""
    try:
        val = int(os.environ.get(_HUB_PAYLOAD_MAX_BYTES_ENV, "") or _HUB_PAYLOAD_MAX_BYTES_DEFAULT)
    except (TypeError, ValueError):
        return _HUB_PAYLOAD_MAX_BYTES_DEFAULT
    return val if val > 0 else _HUB_PAYLOAD_MAX_BYTES_DEFAULT


def _serialized_len(event_json: Dict[str, Any]) -> int:
    """Byte length of the JSON serialization (UTF-8), the size the hub measures."""
    return len(json.dumps(event_json).encode("utf-8"))


def _trim_event_to_payload_cap(event_json: Dict[str, Any], max_bytes: int) -> "tuple[Dict[str, Any], List[str]]":
    """Drop OPTIONAL heavy embedding fields until the serialized event fits
    ``max_bytes`` (R2-11). Returns (possibly-mutated event, list of dropped
    field labels for logging). NEVER drops the core label fields NOR the core
    net inputs (``query_emb``, per-node ``emb``) — if even the stripped event
    is over-cap the caller still posts it (better a loud 413 on a genuinely
    pathological event than a well-formed-but-untrainable row).

    Priority order (documented in the module constants): per-node
    ``linked_embs`` (whole-field) first, then ``answer_chunk_embs``.
    """
    if _serialized_len(event_json) <= max_bytes:
        return event_json, []

    dropped: List[str] = []

    # 1) Near-chunk (per-node) embeddings — the bulk. Strip from every node.
    nodes = event_json.get("nodes")
    if isinstance(nodes, list) and nodes:
        for field in _TRIM_STEPS_NODE_EMB:
            removed_any = False
            for rec in nodes:
                if isinstance(rec, dict) and field in rec:
                    del rec[field]
                    removed_any = True
            if removed_any:
                dropped.append(f"nodes.{field}")
            if _serialized_len(event_json) <= max_bytes:
                return event_json, dropped

    # 2) Event-level embeddings — answer chunk embs. (query_emb is in the
    # never-trim class and is deliberately absent from _TRIM_STEPS_EVENT_EMB.)
    for field in _TRIM_STEPS_EVENT_EMB:
        if field in event_json:
            del event_json[field]
            dropped.append(field)
            if _serialized_len(event_json) <= max_bytes:
                return event_json, dropped

    return event_json, dropped


def _env_truthy(name: str) -> bool:
    """True iff env var ``name`` is set to a truthy value ({true,1,yes,on}).

    Shared by the two-level RL opt-out resolvers. OS-agnostic — reads a plain
    environment variable, so it behaves identically on Windows (the env is
    populated from ``.claude/settings.json`` / ``.claude/env`` on every surface).
    A read that raises (never expected for os.environ, but defensive) falls open
    to False = "not disabled".
    """
    try:
        return os.environ.get(name, "").strip().lower() in ("true", "1", "yes", "on")
    except Exception:  # noqa: BLE001 — env read must never break a gate
        return False


def _local_logging_disabled() -> bool:
    """True iff local RL logging is disabled at EITHER level (Concern-A/C).

    Two-level gate: the GLOBAL ``RL_LOCAL_LOGGING_DISABLED_GLOBAL`` (machine-wide
    app_state, projected into env) OR the per-project ``RL_LOCAL_LOGGING_DISABLED``
    (this project's ``.claude/env``). A truthy value at EITHER level disables the
    local hub write. The global leg is the hard override; the per-project leg is
    the individual opt-out when globally enabled.
    """
    return _env_truthy(_LOCAL_OPT_OUT_ENV_GLOBAL) or _env_truthy(_LOCAL_OPT_OUT_ENV)


def _online_training_disabled() -> bool:
    """True iff LIVE per-answer RL training is disabled at EITHER level (Concern-C).

    Gates the ONLINE ``/rl_update`` path — the live answer-embedding + citation
    RPC the paid ``vct-rl-reranker`` container consumes on every fired
    answer-monitor. Two-level, same shape as ``_local_logging_disabled``:
    GLOBAL ``RL_ONLINE_TRAINING_DISABLED_GLOBAL`` (machine-wide app_state) OR the
    per-project ``RL_ONLINE_TRAINING_DISABLED`` (``.claude/env``).

    DEFAULT ENABLED: both env vars absent → not disabled → returns False
    (unchanged Pro behaviour). FALL OPEN by construction — ``_env_truthy`` returns
    False on any read hiccup, so a transient failure never silently disables a
    paying user's training. An explicit truthy at either level is honoured.
    """
    return _env_truthy(_ONLINE_TRAINING_OPT_OUT_ENV_GLOBAL) or _env_truthy(
        _ONLINE_TRAINING_OPT_OUT_ENV
    )


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

        # RL-5 (v0.2.73) + NEW-2 (v0.2.75): opportunistically drive bounded
        # retention on the write cadence. Throttled to ≤1 pass/hour per process
        # (RL_EVENTS_RETENTION_MIN_INTERVAL_S) so this is nearly free; soft-fail
        # so a wedged/older hub never breaks the write. NEW-2 moved this OUT of
        # the logging-enabled branch: the prune is consumer-independent
        # HOUSEKEEPING, not telemetry — a user who opts out of collecting NEW
        # events still wants their pre-existing rl_events rows to age out (the
        # opt-out promise is "stop recording me", not "keep my old rows
        # forever"). The 6-h in-flight-citation floor inside the retention plan
        # applies identically in both branches.
        self._maybe_prune_rl_events()

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
        answer_chunk_embs: "Optional[List[List[float]]]" = None,
        answer_chunk_hashes: "Optional[List[str]]" = None,
        soft_label: bool = False,
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
                    answer_chunk_embs=answer_chunk_embs,
                    answer_chunk_hashes=answer_chunk_hashes,
                    soft_label=soft_label,
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
                answer_chunk_embs=answer_chunk_embs,
                answer_chunk_hashes=answer_chunk_hashes,
                soft_label=soft_label,
            )
            _enqueue(self._etype_citations, payload)

    # ---- RL-5 retention driver hook ----------------------------------

    def _maybe_prune_rl_events(self) -> None:
        """RL-5 (v0.2.73): opportunistically drive bounded ``rl_events`` retention.

        Called after a successful retrieval hub-write so the prune runs on the
        same connection cadence the writer already has. The heavy lifting
        (cadence throttle, cutoff computation, in-flight-citation floor, hub
        route call) lives in ``rl_retention.maybe_run_retention`` — this wrapper
        only forwards the project scope and swallows every error. Retention is
        best-effort: a wedged/older hub or a missing prune route must never
        break a telemetry write or the user-facing search.
        """
        try:
            from .rl_retention import maybe_run_retention

            maybe_run_retention(project_id=self._project_id)
        except Exception as exc:  # noqa: BLE001 — retention never breaks a write
            logger.debug("RLTelemetryWriter: retention driver raised (%s)", exc)

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
        # Per-node record shape is the single-home `serialize_node_record`
        # helper (rl_logger.py). The queue payload OMITS `links` and the RL-2
        # code-path fields (queue is the consented-upload surface, kept lean),
        # keeps full-precision scalars, and carries the RL-1/RL-7 rank+count
        # fields. Flags below reproduce this caller's exact historical field
        # set + insertion order byte-identically.
        node_records: List[Dict[str, Any]] = [
            serialize_node_record(
                n,
                include_links=False,
                include_shown_rank=True,
                include_chunks_matched=True,
                include_best_chunk_number=False,
                include_code_path_fields=False,
                round_scalars=False,
            )
            for n in nodes
        ]

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
            payload["query_emb"] = _round_emb(query_emb)
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
        answer_chunk_embs: "Optional[List[List[float]]]" = None,
        answer_chunk_hashes: "Optional[List[str]]" = None,
        soft_label: bool = False,
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
        # G4 (2026-07-22): mirror the answer-chunk embeddings + hashes into the
        # upload-queue payload so a consented cloud training corpus carries the
        # same replayable answer artifacts as the local launcher.db event.
        if answer_chunk_embs:
            payload["answer_chunk_embs"] = [
                _round_emb(e) for e in answer_chunk_embs if e
            ]
        if answer_chunk_hashes:
            payload["answer_chunk_hashes"] = [str(h) for h in answer_chunk_hashes]
        if soft_label:
            payload["soft_label"] = True
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
        Per-node records are built by the single-home ``serialize_node_record``
        helper (rl_logger.py), same as ``_build_retrieval_payload``. This event
        is the SUPERSET surface: per-node records carry a strict superset of the
        queue payload's fields — additionally ``links``, ``best_chunk_number``,
        and the RL-2 code-path fields (``collection`` / ``file_path`` /
        ``rerank_score`` / ``boost_delta`` / ``boost_signals``) — because
        launcher.db is local-only and stores payload_json verbatim. The
        event ALSO carries the raw query text (the queue payload strips query
        as PII). The two callers differ ONLY by explicit flags into the shared
        helper, so they can no longer silently drift.

        v0.2.73 additions (all optional / additive): event-level ``rl_used``
        (RL-1 — did the rerank RPC run) + ``extras`` (RL-2 — code-path
        diagnostics); per-node ``shown_rank`` (RL-1 — post-rerank shown
        order), ``chunks_matched`` / ``best_chunk_number`` (RL-7) and the
        code-path fields ``collection`` / ``file_path`` / ``rerank_score`` /
        ``boost_delta`` / ``boost_signals`` (RL-2).
        """
        # Per-node record shape is the single-home `serialize_node_record`
        # helper (rl_logger.py). The v3 launcher.db event is the SUPERSET
        # surface: it carries `links`, `best_chunk_number`, and the RL-2
        # code-path fields the leaner queue payload omits, at full scalar
        # precision. Flags below turn all of those ON and reproduce this
        # caller's exact historical field set + insertion order byte-identically.
        node_records: List[Dict[str, Any]] = [
            serialize_node_record(
                n,
                include_links=True,
                include_shown_rank=True,
                include_chunks_matched=True,
                include_best_chunk_number=True,
                include_code_path_fields=True,
                round_scalars=False,
            )
            for n in nodes
        ]

        # WP-R defect-2 (R3-7 step 2): mirror the ENVELOPE's measured-dim fix into
        # the payload-inner embedding_dim. When a query_emb is present its length
        # is ground truth — writing the construction-time config dim beside a
        # different-length vector is the exact historical escaper shape (e.g.
        # embedding_dim: 2048 beside a len-3 query_emb), now merely one level
        # deeper in payload_json. Measure from the vector we actually store so a
        # payload_json reader sees a self-consistent event; fall back to the config
        # dim only when no vector is carried.
        _rounded_query_emb = (
            _round_emb(query_emb) if query_emb is not None else None
        )
        _payload_dim = self._embedding_dim
        if isinstance(_rounded_query_emb, list) and _rounded_query_emb:
            _payload_dim = len(_rounded_query_emb)

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
            "embedding_dim": _payload_dim,
            "embedding_model": self._embedding_model,
            "nodes": node_records,
        }
        if _rounded_query_emb is not None:
            event["query_emb"] = _rounded_query_emb
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
        answer_chunk_embs: "Optional[List[List[float]]]" = None,
        answer_chunk_hashes: "Optional[List[str]]" = None,
        soft_label: bool = False,
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
            # R3-7 step 3: the config dim is DELIBERATE here — a citation event
            # carries no query/answer query-vector to measure against (the
            # answer_chunk_embs are per-chunk, not the event's embedding space), so
            # there is nothing to reconcile the dim to. Unlike the retrieval event
            # (which measures from its query_emb), this is the config dim by design.
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
        # G4 (2026-07-22): persist the ANSWER-CHUNK EMBEDDINGS (not the answer
        # text — privacy) that produced the cosine_sims labels, tagged implicitly
        # by this event's embedding_source/dim/model triple. These let the offline
        # trainer RE-DERIVE citation labels for a DIFFERENT embedding profile
        # (the second RL net's space) or a retuned target formula WITHOUT the
        # original answer — the cosine_sims scalars alone are frozen in the active
        # space and cannot be replayed against a node vector in another space.
        # ``answer_chunk_hashes`` (sha256 of each chunk's text) are stored in
        # parallel for cross-slot dedup: the same answer embedded into arctic +
        # qwen slots shares hashes, so a de-dup pass can pair the two slots'
        # answer artifacts. Embeddings are rounded like every other stored vector
        # (_round_emb, 4 dp). payload_json is stored verbatim by the hub → NO
        # schema/Rust change (the rl_events rows already carry query_emb + node
        # embeddings, proving the pipe accepts embedding payloads).
        if answer_chunk_embs:
            event["answer_chunk_embs"] = [
                _round_emb(e) for e in answer_chunk_embs if e
            ]
        if answer_chunk_hashes:
            event["answer_chunk_hashes"] = [str(h) for h in answer_chunk_hashes]
        # G5 (2026-07-22): mark a below-terminal-floor soft label so the trainer
        # can down-weight it (a shorter answer window is a weaker citation signal
        # than a full one). Absent on the normal path (default False → omitted).
        if soft_label:
            event["soft_label"] = True
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
        # WP-Q item 2 (was R2-11): PATHOLOGICAL-CASE BACKSTOP. Cap the payload
        # just below the hub's EXPLICIT 16 MiB axum body limit. Measure the
        # serialized event and, if over-cap, drop OPTIONAL heavy embedding fields
        # in a documented priority order (per-node linked_embs → answer embs),
        # NEVER the core label nor the core net inputs (query_emb, nodes[].emb).
        # Log at WARNING so a trimmed event is visible (a
        # dropped embedding is a real, if recoverable, loss of training signal).
        # Normal events never approach the cap — this branch only fires for a
        # runaway/pathological event, never in steady state.
        max_bytes = _resolve_hub_payload_max_bytes()
        trimmed_event, dropped = _trim_event_to_payload_cap(event_json, max_bytes)
        if dropped:
            logger.warning(
                "RL telemetry: %s event %s exceeded the %d-byte hub payload cap; "
                "dropped optional embedding field(s) %s to keep the core label "
                "(the hub's axum body limit is 16 MiB — see rl_events_api.rs)",
                event_type, task_id, max_bytes, ", ".join(dropped),
            )
        # The envelope's denormalized embedding_dim must never disagree with a
        # PRESENT payload vector: the payload's query_emb length is ground
        # truth; the construction-time config dim is only the fallback for
        # events that carry no vector (e.g. citations). Disagreement is logged
        # so config-vs-actual drift stays visible. (query_emb is in the
        # never-trim class, so measuring the trimmed event is safe.)
        measured_dim = None
        _qe = trimmed_event.get("query_emb")
        if isinstance(_qe, list) and _qe:
            measured_dim = len(_qe)
            if self._embedding_dim and measured_dim != self._embedding_dim:
                logger.warning(
                    "RL telemetry: envelope embedding_dim %d != measured "
                    "query_emb length %d for task %s — storing the measured "
                    "length (payload is ground truth)",
                    self._embedding_dim, measured_dim, task_id,
                )
        return {
            "event_type": event_type,
            "schema_version": int(trimmed_event.get("schema_version") or RLDataLogger.SCHEMA_VERSION),
            "ts_ms": int(time.time() * 1000),
            "project_id": self._project_id,
            "project_name": self._project or None,
            "task_id": task_id,
            "task_type": task_type,
            "embedding_source": self._embedding_source or None,
            "embedding_dim": measured_dim or self._embedding_dim or None,
            "embedding_model": self._embedding_model or None,
            "payload_json": json.dumps(trimmed_event),
        }


def _now_iso() -> str:
    """Local-clock ISO 8601 timestamp matching the pre-v0.2.47 RLDataLogger format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
