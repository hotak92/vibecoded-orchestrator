# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Hub-backed event loader for the offline RL trainer (DB-as-source-of-truth).

Companion to ``offline_trainer.load_events`` (the JSONL reader). This module
fetches the SAME training events from ``launcher.db`` via the hub's
module-scoped route instead of a mounted JSONL file, and reshapes each hub
row into the exact event-dict shape ``load_events`` produces — so the rest of
the trainer (``pair_events`` → ``extract_samples`` → ``train``) is unchanged.

Why a hub read (not direct sqlite, not JSONL):
  - The WRITE side modernized to ``launcher.db.rl_events`` (V52, hub-written);
    the JSONL corpus is frozen legacy. The DB is the live source of truth.
  - The trainer runs INSIDE the RL container, which must NOT open launcher.db
    directly (single-writer principle) and does NOT hold the hub-wide
    ``hub.token``. So it reads its own events over HTTP via the module-scoped
    route ``GET /api/v1/modules/{module_id}/projects/{project_id}/rl/events``,
    authenticated with the module token it already possesses.

Shape contract (verified against ``telemetry_writer._wrap_for_hub``):
  The hub stores the full v3 event JSON verbatim in the row's ``payload_json``
  column. ``json.loads(row["payload_json"])`` therefore yields the identical
  dict the JSONL path appended — same keys (``event``, ``task_id``,
  ``embedding_source``, ``schema_version``, the v3 payload fields, …). We DO
  NOT re-derive any field from the denormalized columns; the payload is
  canonical, exactly as the JSONL line was.

Filtering parity with ``load_events``:
  - schema_version < 2 → refused (same as JSONL; v1 events predate per-source
    isolation and would silently blend embedding sources). DB-era events are
    schema_version 3, so this is a guard, not an expected path.
  - ``filter_embedding_source`` applies to RETRIEVAL events only; citation
    events carry no embedding metadata and join by task_id at pairing time.
  - Event type is restricted to {retrieval, citation}. Outcome events
    (bash_outcome / edit_outcome / pre_bash) are EXCLUDED BY DESIGN: RL trains
    only on what the LLM SENDS to a tool (the retrieval it used + the citation
    it produced), never on what the tool RETURNS (exit codes, diff sizes).
    This is a permanent boundary, not a v1 scoping shortcut.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Event types the trainer consumes. PERMANENT — see module docstring.
_TRAINABLE_EVENT_TYPES = ("retrieval", "citation")


def _reshape_hub_row(row: "dict[str, Any]") -> "dict[str, Any] | None":
    """Turn one hub row into a load_events()-shape event dict.

    The canonical event is ``json.loads(row["payload_json"])``. Returns None
    when the row has no parseable payload (skipped, like a malformed JSONL
    line). The denormalized columns (project_id, embedding_source, …) are
    used ONLY as a fallback for fields the older payloads might lack — the
    payload itself is authoritative when present.
    """
    raw = row.get("payload_json")
    if not raw:
        return None
    try:
        ev = json.loads(raw)
    except (TypeError, ValueError):
        logger.debug("hub_event_loader: skip row with unparseable payload_json (id=%r)", row.get("id"))
        return None
    if not isinstance(ev, dict):
        return None

    # Backfill fields the JSONL path always had but a sparse payload might
    # miss, from the denormalized columns. Never OVERRIDE a present payload
    # value — the payload is the canonical replayable event.
    if "embedding_source" not in ev and row.get("embedding_source") is not None:
        ev["embedding_source"] = row.get("embedding_source")
    if "project" not in ev and row.get("project_id") is not None:
        # JSONL events keyed cohort on a "project" field; the DB promoted it
        # to the project_id column. Mirror it back so any cohort logic that
        # still reads ev["project"] keeps working.
        ev["project"] = row.get("project_id")
    if "schema_version" not in ev and row.get("schema_version") is not None:
        ev["schema_version"] = row.get("schema_version")
    return ev


def load_events_from_hub(
    hub_client: Any,
    project_id: str,
    *,
    filter_embedding_source: str = "",
    since_ms: "int | None" = None,
    until_ms: "int | None" = None,
    max_events: "int | None" = None,
) -> "list[dict[str, Any]]":
    """Load trainable RL events for one project from the hub (sync wrapper).

    Mirrors ``offline_trainer.load_events`` semantics but sources rows from
    ``hub_client.list_rl_events`` instead of a JSONL file. Synchronous so it
    drops into the existing (sync, CPU-bound) trainer call sites; it drives
    the async hub client via ``asyncio.run`` when no loop is running, or via a
    fresh loop in a worker thread when called from within one.

    Args:
        hub_client: An object exposing the async
            ``list_rl_events(project_id, *, event_type, since_ms, until_ms,
            max_events)`` coroutine (the container's ``_HubClient``).
        project_id: Cohort key (the DB project_id column).
        filter_embedding_source: Keep only retrieval events whose
            embedding_source matches (citations pass through). Empty disables.
        since_ms / until_ms: Time-window bounds (unix epoch ms).
        max_events: Optional cap on total rows fetched.

    Returns:
        List of event dicts in the same shape ``load_events`` returns, ready
        for ``pair_events``. Raises ValueError on schema_version<2 events
        (parity with the JSONL reader's v1 refusal).
    """
    rows = _run_coro(
        hub_client.list_rl_events(
            project_id,
            event_type=None,  # fetch both retrieval + citation; filter below
            since_ms=since_ms,
            until_ms=until_ms,
            max_events=max_events,
        )
    )

    events: list[dict[str, Any]] = []
    legacy_count = 0
    for row in rows:
        ev = _reshape_hub_row(row)
        if ev is None:
            continue

        etype = ev.get("event") or row.get("event_type")
        if etype not in _TRAINABLE_EVENT_TYPES:
            # Outcome events (and anything else) are excluded by design.
            continue

        # v1 refusal — parity with load_events(): blending pre-isolation
        # events would silently mix embedding sources.
        if int(ev.get("schema_version", 0) or 0) < 2:
            legacy_count += 1
            continue

        # Per-source filter applies to retrieval events only.
        if filter_embedding_source and etype == "retrieval":
            if ev.get("embedding_source") != filter_embedding_source:
                continue

        events.append(ev)

    if legacy_count > 0:
        raise ValueError(
            f"hub_event_loader: {legacy_count} legacy (schema_version<2) events "
            f"returned for project_id={project_id!r}. DB-era events are v3; a v1 "
            f"event here means the corpus was not migrated. Refusing to train on "
            f"v1 events would silently merge data from different embedding sources."
        )

    return events


def _run_coro(coro: Any) -> Any:
    """Run an async coroutine from sync code, loop-safe.

    The offline trainer is synchronous. In production it is invoked either
    standalone (CLI: no event loop) or from the rl_server's ``/finetune``
    handler via ``asyncio.to_thread`` (worker thread → also no running loop).
    Both hit the ``asyncio.run`` fast path, where the client's aiohttp session
    is created, used, and closed on the SAME loop — the correct, simple case.

    Both production call paths hit the ``asyncio.run`` fast path, where the
    coroutine — and the aiohttp session it creates/uses/closes — live on one
    loop. That is the only supported invocation: the loader must be called from
    SYNC code (no running loop in the calling thread).

    We explicitly REJECT the nested case (a loop already running in this
    thread). An aiohttp session is bound to the loop it was created on, so a
    correct sync bridge would have to create + use + close the client on a
    single loop; driving a session that was opened on loop A from loop B
    silently fails. Rather than hide that with a fragile cross-loop shim, we
    fail loudly so a future async caller fixes the call site (open the client
    and read within one ``async def`` instead of going through this sync
    bridge). Tests must drive the loader from a worker thread (``asyncio.run``
    inside, no outer running loop), mirroring the ``/finetune`` worker-thread
    path.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    raise RuntimeError(
        "hub_event_loader must be called from synchronous code (no running "
        "event loop in this thread). The offline trainer is sync; the "
        "/finetune handler reaches it via asyncio.to_thread. If you need to "
        "load events from async code, call _HubClient.list_rl_events directly "
        "within your coroutine instead of going through load_events_from_hub."
    )


__all__ = ["load_events_from_hub"]
