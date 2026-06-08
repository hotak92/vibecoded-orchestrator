# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Training corpus loader for offline qwen3 embedding model pretraining.

Streams training-eligible events from the on-disk JSONL corpus, applying a
10-step filter funnel (see V38-LOG-AUDIT 2026-05-28) and on-the-fly
query-embedding backfill via Ollama when needed.

Usage example::

    from pathlib import Path
    from claude_mcp_servers.rl_client.training_loader import load_qwen3_training_corpus

    for event in load_qwen3_training_corpus(include_synthetic=True):
        # event is a fully validated dict ready for the offline trainer
        print(event["project"], event["query"][:80])

The loader is a pure generator — it never holds the full corpus in memory.
Both source files are streamed line-by-line so 600+ MB files are handled
without OOM risk.

Schema contract: ``rl_logger.RLDataLogger.SCHEMA_VERSION == 3`` as of
v0.2.47 RL-3 (2026-06-04). All events written by v0.2.47+ pass. Pre-v0.2.28
legacy rows (649 ``Claude``-cohort rows without schema_version) AND v2
events (v0.2.28..v0.2.46) are dropped at step 3 of the funnel — the C9
JSONL->DB migration script ports v2 rows into the new
``launcher.db.rl_events`` table tagged ``schema_version=3`` first, so by
the time the offline trainer runs there are no v2 rows on disk.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

import httpx

from .rl_logger import RLDataLogger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical schema version we accept (v1 rows are in .v1.bak archives).
# ---------------------------------------------------------------------------
_REQUIRED_SCHEMA_VERSION = str(RLDataLogger.SCHEMA_VERSION)  # "3" as of v0.2.47
_REQUIRED_EMBEDDING_DIM = "1024"

# ---------------------------------------------------------------------------
# Default corpus paths (from rl-telemetry KG node 2026-05-28).
# ---------------------------------------------------------------------------
_DEFAULT_PRIMARY = Path.home() / ".claude" / "retrieval_rl_data" / "rl_events.jsonl"
_DEFAULT_QWEN3 = Path.home() / ".claude" / "retrieval_rl_data" / "rl_events_qwen3.jsonl"

# ---------------------------------------------------------------------------
# Default cohort alias map (NEW-7 from v0.2.38 backlog 2026-05-28).
# Keys are canonical slugs; values are the legacy aliases that should map to
# each canonical slug.  Applied to the ``project`` field at load time so the
# offline trainer sees a single cohort label regardless of which project
# alias the event was written with.
# ---------------------------------------------------------------------------
_DEFAULT_COHORT_ALIASES: dict[str, list[str]] = {
    "orchestrator-root": [
        "VCODev",
        "VibeCoded Orchestrator",
        "VibeCodedOrchestrator",
        "Claude",
    ],
}


def _build_alias_lookup(
    cohort_aliases: dict[str, list[str]] | None,
    apply_alias_map: bool,
) -> dict[str, str]:
    """Build a flat ``{alias: canonical}`` lookup from the nested alias map.

    Returns an empty dict when ``apply_alias_map`` is False, so downstream
    code can always do ``alias_lookup.get(project, project)`` safely.
    """
    if not apply_alias_map:
        return {}
    mapping: dict[str, str] = {}
    aliases = cohort_aliases if cohort_aliases is not None else _DEFAULT_COHORT_ALIASES
    for canonical, raw_aliases in aliases.items():
        for alias in raw_aliases:
            mapping[alias] = canonical
    return mapping


def load_qwen3_training_corpus(
    primary_path: Path = _DEFAULT_PRIMARY,
    qwen3_path: Path | None = _DEFAULT_QWEN3,
    *,
    apply_alias_map: bool = True,
    include_synthetic: bool = False,
    backfill_query_emb: bool = True,
    backfill_endpoint: str = "http://localhost:11435/api/embed",
    backfill_model: str = "qwen3-embedding:0.6b",
    cohort_aliases: dict[str, list[str]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream training-eligible events from on-disk JSONL corpus.

    Applies the 10-step filter funnel defined in V38-LOG-AUDIT (2026-05-28):

    1. Stream-read both files line-by-line.
    2. Drop JSON-parse failures (logged to stderr at DEBUG).
    3. Drop ``schema_version != "3"`` rows (post-v0.2.47).
    4. Drop missing / wrong-dim ``embedding_dim`` (must be ``"1024"``).
    5. Drop events with ``failure_mode`` set.
    6. Keep ``embedding_source == "qwen3"`` OR ``_reembedded_model == "qwen3-embedding:0.6b"``.
    7. Drop ``task_type == "synthetic"`` unless ``include_synthetic=True``.
    8. Apply ``cohort_aliases`` to canonicalise ``project`` field.
    9. Backfill missing ``query_emb`` via Ollama when ``backfill_query_emb=True``;
       drop events where backfill fails.
    10. Dedup across files by ``(task_id, event_type)``; prefer qwen3-native rows
        over re-embedded arctic rows (qwen3 file rows win over primary file rows).

    Args:
        primary_path: Path to ``rl_events.jsonl`` (primary corpus).
        qwen3_path: Path to ``rl_events_qwen3.jsonl`` (re-embedded subset).
            Pass ``None`` to skip the qwen3 file.
        apply_alias_map: When True, apply ``cohort_aliases`` to normalise the
            ``project`` field (e.g. "Claude" → "orchestrator-root").
        include_synthetic: When False (default), drop events where
            ``task_type == "synthetic"``.
        backfill_query_emb: When True, POST missing ``query_emb`` to the
            Ollama embed endpoint for retrieval events that lack it.
        backfill_endpoint: Ollama embed endpoint URL.
        backfill_model: Embedding model to use for backfill.
        cohort_aliases: Override the default alias map.  Dict maps canonical
            project slug to a list of legacy aliases.  ``None`` uses
            ``_DEFAULT_COHORT_ALIASES``.

    Yields:
        Event dicts that passed all 10 filter steps.  For retrieval events
        the ``project`` field reflects any alias-map canonicalization.

    Raises:
        Nothing — all I/O and HTTP errors are logged at DEBUG and cause the
        affected row to be dropped.
    """
    alias_lookup = _build_alias_lookup(cohort_aliases, apply_alias_map)

    # Step 9 cache: (query_text, model) → embedding vector.
    # Avoids redundant Ollama calls for the same query text within one load.
    _backfill_cache: dict[tuple[str, str], list[float]] = {}

    # Step 10 dedup: (task_id, event_type) → row dict.
    # We build the dedup table first (both files) then yield in order.
    dedup: dict[tuple[str, str], dict[str, Any]] = {}

    # -----------------------------------------------------------------------
    # Pass 1: ingest primary file, then overlay qwen3 file.
    # The qwen3 file rows win for the same (task_id, event) key because they
    # carry _reembedded_model and represent the qwen3-aligned version.
    # -----------------------------------------------------------------------
    files_to_load: list[tuple[Path, bool]] = [(primary_path, False)]
    if qwen3_path is not None:
        files_to_load.append((qwen3_path, True))  # True = prefer on collision

    for file_path, prefer_on_collision in files_to_load:
        if not file_path.exists():
            logger.debug(
                "training_loader: corpus file not found, skipping: %s", file_path
            )
            continue

        # Step 1: stream line-by-line (never readlines()).
        with file_path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue

                # Step 2: drop JSON parse failures.
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.debug(
                        "training_loader: JSON parse failure at %s:%d — %s",
                        file_path.name, lineno, exc,
                        stack_info=False,
                    )
                    continue

                # Step 3: drop schema_version != "3" (post-v0.2.47).
                raw_sv = event.get("schema_version")
                # Accept both int 3 and string "3" defensively.
                if str(raw_sv) != _REQUIRED_SCHEMA_VERSION:
                    logger.debug(
                        "training_loader: step-3 drop (schema_version=%r) at %s:%d",
                        raw_sv, file_path.name, lineno,
                    )
                    continue

                # Step 4: drop missing embedding_dim or != "1024".
                raw_dim = event.get("embedding_dim")
                if raw_dim is None or str(raw_dim) != _REQUIRED_EMBEDDING_DIM:
                    logger.debug(
                        "training_loader: step-4 drop (embedding_dim=%r) at %s:%d",
                        raw_dim, file_path.name, lineno,
                    )
                    continue

                # Step 5: drop failure_mode set.
                if event.get("failure_mode"):
                    logger.debug(
                        "training_loader: step-5 drop (failure_mode=%r) at %s:%d",
                        event["failure_mode"], file_path.name, lineno,
                    )
                    continue

                # Step 6: keep qwen3-native OR re-embedded to qwen3.
                emb_src = event.get("embedding_source", "")
                reembed_model = event.get("_reembedded_model", "")
                if emb_src != "qwen3" and reembed_model != backfill_model:
                    logger.debug(
                        "training_loader: step-6 drop (embedding_source=%r, "
                        "_reembedded_model=%r) at %s:%d",
                        emb_src, reembed_model, file_path.name, lineno,
                    )
                    continue

                # Step 7: drop synthetic unless include_synthetic.
                if not include_synthetic and event.get("task_type") == "synthetic":
                    logger.debug(
                        "training_loader: step-7 drop (task_type=synthetic) at %s:%d",
                        file_path.name, lineno,
                    )
                    continue

                # Step 8: apply cohort alias map to project field.
                original_project = event.get("project", "")
                canonical_project = alias_lookup.get(original_project, original_project)
                if canonical_project != original_project:
                    # Mutate a shallow copy so the caller sees the canonical label.
                    event = dict(event)
                    event["project"] = canonical_project

                # Step 10: dedup by (task_id, event_type).
                # For the qwen3 file (prefer_on_collision=True), always overwrite.
                # For the primary file, only insert if key is not yet seen.
                try:
                    task_id = str(event["task_id"])
                    event_type = str(event["event"])
                except KeyError:
                    logger.debug(
                        "training_loader: step-10 drop (missing task_id or event) "
                        "at %s:%d",
                        file_path.name, lineno,
                    )
                    continue

                key = (task_id, event_type)
                if prefer_on_collision or key not in dedup:
                    dedup[key] = event

    # -----------------------------------------------------------------------
    # Pass 2: apply step 9 (query_emb backfill) and yield.
    # Citation events do not carry query_emb — only retrieval events do.
    # -----------------------------------------------------------------------
    for event in dedup.values():
        event_type = event.get("event", "")

        # Step 9: only retrieval events need query_emb.
        if event_type == "retrieval":
            query_emb = event.get("query_emb")
            has_valid_emb = (
                isinstance(query_emb, list) and len(query_emb) > 0
            )
            if not has_valid_emb:
                if not backfill_query_emb:
                    logger.debug(
                        "training_loader: step-9 drop (no query_emb, backfill off) "
                        "task_id=%s",
                        event.get("task_id"),
                    )
                    continue

                query_text = event.get("query", "")
                backfilled = _backfill_embedding(
                    query_text,
                    backfill_model,
                    backfill_endpoint,
                    _backfill_cache,
                )
                if backfilled is None:
                    logger.debug(
                        "training_loader: step-9 drop (backfill failed) task_id=%s",
                        event.get("task_id"),
                    )
                    continue

                event = dict(event)
                event["query_emb"] = backfilled

        yield event


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _backfill_embedding(
    query: str,
    model: str,
    endpoint: str,
    cache: dict[tuple[str, str], list[float]],
) -> list[float] | None:
    """POST to Ollama embed endpoint; cache by (query, model).

    Returns the 1024-dim embedding vector, or None if the request fails
    (Ollama unreachable, unexpected response shape, etc.).

    The cache prevents duplicate calls within a single corpus load.
    """
    cache_key = (query, model)
    if cache_key in cache:
        return cache[cache_key]

    try:
        response = httpx.post(
            endpoint,
            json={"model": model, "input": query},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        # Ollama /api/embed returns {"embeddings": [[...]]} (list-of-lists).
        embeddings = payload.get("embeddings") or payload.get("embedding")
        if not embeddings:
            logger.debug(
                "training_loader: backfill: unexpected response shape from %s "
                "(keys=%r)", endpoint, list(payload.keys()),
            )
            return None

        # embeddings is either list-of-lists (new API) or flat list (old API).
        if isinstance(embeddings[0], list):
            vector: list[float] = embeddings[0]
        else:
            vector = list(embeddings)

        cache[cache_key] = vector
        return vector

    except httpx.TransportError as exc:
        logger.debug(
            "training_loader: backfill transport error for endpoint %s: %s",
            endpoint, exc,
        )
    except httpx.HTTPStatusError as exc:
        logger.debug(
            "training_loader: backfill HTTP error %d from %s",
            exc.response.status_code, endpoint,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "training_loader: backfill unexpected error: %s", exc,
        )

    return None
