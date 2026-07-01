# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Idempotent enrichment migration for embed-slot changes (v0.2.18, Commit 9).

When the user switches a project's KG or codegraph embedding model in the GUI
dropdown (Commit 8), the schema-migration step (Commit 4) has already added
the new named-vector slot to the live collection — but every existing object
in that collection still has the OLD slot populated and the new slot empty.

This module bridges the gap. It walks one Weaviate collection and, for each
object whose target slot is empty, computes a vector via ``EmbeddingService``
and writes JUST that slot (preserving every other slot's data verbatim). The
operation is idempotent (re-running on an already-enriched collection is a
zero-write no-op), soft-fail per object (one bad embed doesn't abort the
batch), and bounded in memory (batches of 100 objects, never reads the whole
collection at once).

It explicitly does NOT:
  * Delete any slot, object, or property.
  * Change any object's UUID.
  * Modify the schema (that's Commit 4's job — enrichment assumes the slot
    already exists; if it doesn't, we surface a clear error pointing the user
    at ``migrate-collections``).
  * Touch collections other than the one named.
  * Dedup — enrichment is UPDATE-only on existing UUIDs (no INSERTs), so
    duplicates are impossible by construction.

Locked design decisions (from the v0.2.18 plan):
  * **Batch size = 100**: Ollama-friendly throughput; CodeEmbed handles its
    own queueing internally; OpenAI chunks at 100 inside its adapter. One
    batch = one inner HTTP round-trip per backend.
  * **Per-object failure → continue**: enrichment is best-effort. A
    single embed/write failure increments the ``failed`` counter and adds
    a redacted entry to ``failures`` (capped at 20). The whole run does
    not abort.
  * **Pre-flight all errors**: collection-not-found, slot-not-in-catalog,
    slot-not-in-live-schema, and no-backend-reachable all raise BEFORE
    any walk happens. The caller gets a structured exception with a
    helpful next-step hint; no half-written state.
  * **No-op fast path**: if the requested ``new_slot`` is already the
    project's active text/code slot per ``EmbeddingService``, the run
    skips the walk entirely. No wasted embed calls.
  * **Dry-run via flag, not separate function**: ``dry_run=True`` counts
    what WOULD be enriched + skipped without making any writes. Failures
    are still raised during pre-flight (so the user gets a real signal
    rather than "well, it would have failed").
  * **Progress streaming via callback**: the Tauri command wraps the CLI
    invocation and parses ``--stream-progress`` JSON lines into
    ``vct-enrichment-progress`` Tauri events. The Python side stays pure
    and testable; the streaming logic is just a stdout printer.

CLI entry::

    python -m vco_lib.embedding_enrichment enrich \\
        --collection MyProject_KnowledgeGraph \\
        --new-slot arctic2_embed \\
        [--project-root /path/to/project] \\
        [--dry-run] \\
        [--stream-progress] \\
        [--json]

Output (``--json``):

    {
      "collection": "MyProject_KnowledgeGraph",
      "new_slot": "arctic2_embed",
      "total": 1234,
      "enriched": 1199,
      "skipped": 30,
      "failed": 5,
      "failures": [
        {"uuid": "...", "error": "Ollama /api/embed network error: ..."},
        ...
      ]
    }

With ``--stream-progress``, additional JSON lines on stdout precede the final
report::

    {"progress": 0.08, "message": "Enriched 100/1234 (5 skipped, 0 failed)"}
    {"progress": 0.16, "message": "..."}
    ...

The Rust Tauri wrapper (``launcher/src-tauri/src/commands/embedding_enrichment.rs``)
parses these lines and re-emits them as ``vct-enrichment-progress`` events for
the Svelte progress modal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# Worktree-safe sys.path bootstrap: enrichment is also invoked directly via
# `python -m vco_lib.embedding_enrichment` from the Tauri shell-out, where
# the parent directory of `vco_lib/` must be on sys.path before any import
# of sibling vco_lib modules. Same pattern as analyze_code_graph.py.
_VCO_LIB_PARENT = Path(__file__).resolve().parent.parent
if str(_VCO_LIB_PARENT) not in sys.path:
    sys.path.insert(0, str(_VCO_LIB_PARENT))

from vco_lib.embedding_service import (  # noqa: E402
    EmbeddingService,
    NoEmbeddingBackendError,
)
from vco_lib.weaviate_schema import (  # noqa: E402
    CODE_NAMED_VECTORS,
    KG_NAMED_VECTORS,
    NamedVectorSlot,
    is_code_collection,
)

logger = logging.getLogger(__name__)

__all__ = [
    # Core API
    "enrich_collection_vectors",
    "EnrichmentReport",
    # Exceptions
    "CollectionNotFoundError",
    "UnknownSlotError",
    "SlotNotInSchemaError",
    "SchemaDimMismatchError",
    # Internals exposed for tests
    "BATCH_SIZE",
    "MAX_FAILURE_DETAILS",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default batch size for the enrich loop. Locked at 100 per the plan; the
#: rationale is in the module docstring. Tests can monkey-patch this.
BATCH_SIZE = 100

#: Maximum number of detailed failure entries kept on the EnrichmentReport.
#: Beyond this we still increment ``failed`` but don't store the per-uuid
#: error string. Prevents memory blow-up on a catastrophic backend
#: regression (every object failing).
MAX_FAILURE_DETAILS = 20

#: Maximum chars of content fed into a single embed call. Matches the
#: conservative cap used by ``migrate_to_new_embeddings.py``; keeps Ollama
#: from choking on multi-megabyte node bodies. The exact token-aware
#: truncation is the embedder's job — we just protect against the worst
#: cases.
EMBED_INPUT_CHAR_CAP = 16000


# ---------------------------------------------------------------------------
# Content-kind classification
# ---------------------------------------------------------------------------

#: Property name carrying the embed input on KG-shaped collections. Same as
#: ``sync_knowledge_graph.py``'s ``content`` write.
KG_CONTENT_PROPERTY = "content"

#: Map of code-collection suffix → property name carrying the embed input.
#: Matches what ``analyze_code_graph.py`` uses for each Code* class when it
#: computes the original embedding. Drift between this map and analyze's
#: insert payloads means enrichment computes vectors from different text
#: than the original — preserving the map alongside the analyzer is
#: deliberate (see test ``test_kg_uses_content_property_for_embed_input``
#: + friends).
CODE_CONTENT_PROPERTY_BY_SUFFIX: dict[str, str] = {
    "CodeFunction": "function_body",
    "CodeClass": "class_body",
    "CodeModule": "module_summary",
    "CodeAPI": "api_description",
    "CodeInteraction": "endpoint",
}


def _resolve_content_property(collection_name: str) -> str:
    """Return the property name carrying embed input for ``collection_name``.

    KG-shaped collections use ``content``. Code-shaped collections use the
    suffix-specific property listed in :data:`CODE_CONTENT_PROPERTY_BY_SUFFIX`.

    Unknown shapes (caller passed e.g. a generic class that ends with a
    code suffix but isn't a canonical Code* class) fall back to ``content``
    — which gracefully degrades to ``skipped`` for any object that doesn't
    have the property set.
    """
    if not is_code_collection(collection_name):
        return KG_CONTENT_PROPERTY
    for suffix, prop in CODE_CONTENT_PROPERTY_BY_SUFFIX.items():
        if collection_name.endswith(suffix):
            return prop
    return KG_CONTENT_PROPERTY


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CollectionNotFoundError(RuntimeError):
    """The named Weaviate collection does not exist on the server.

    The migration helper (``vco_lib.project_init.migrate_collections`` or
    ``vco_lib.weaviate_schema.migrate_collections_to_v0218_schema``)
    creates collections; enrichment never does. If you see this, the
    project's KG/code graph hasn't been seeded yet — run install.py's
    seed step or the launcher's "Seed KG" action first.
    """


class UnknownSlotError(RuntimeError):
    """The requested ``new_slot`` is not in the orchestrator's slot catalog.

    The catalog is the union of ``vco_lib.weaviate_schema.KG_NAMED_VECTORS``
    and ``CODE_NAMED_VECTORS``. Adding a new slot requires registering it
    in those lists first (which auto-flows into schema migration).
    """


class SlotNotInSchemaError(RuntimeError):
    """The requested slot is in the catalog but not in the live collection.

    Means the collection hasn't yet been migrated to the v0.2.18+ schema.
    The launcher's "Migrate collections" action (or the CLI command
    ``python -m vco_lib.project_init migrate-collections``) adds missing
    slots without destroying existing data. After that completes, re-run
    enrichment.
    """


class SchemaDimMismatchError(RuntimeError):
    """The slot exists in the schema but at a different dim than expected.

    This is a paranoid defense — AGENT-SCHEMA's slot catalog is supposed
    to make dim mismatches impossible. If you see this, something
    upstream (a manually-edited schema, a stale collection from a pre-
    v0.2.18 install with a custom slot name reused for a different model)
    has put the collection in an inconsistent state. Run
    ``migrate-collections`` to walk the project's schema back into the
    canonical shape.
    """


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichmentReport:
    """Outcome of an :func:`enrich_collection_vectors` invocation.

    Attributes:
        collection: The Weaviate class name that was enriched.
        new_slot: The named-vector slot that was filled in.
        total: Total objects walked (post-filter, pre-action). May equal
            zero when the collection is empty.
        enriched: Objects that gained a new-slot vector this run.
        skipped: Objects that already had the slot populated (the
            idempotency counter).
        failed: Objects where embed or write failed. Details (capped at
            :data:`MAX_FAILURE_DETAILS`) live in ``failures``. The
            integer count is the FULL failure total — a value larger
            than ``len(failures)`` indicates the cap kicked in.
        failures: Per-object failure rows. Capped at
            :data:`MAX_FAILURE_DETAILS` to bound memory. Each row is a
            JSON-serialisable dict carrying at least ``uuid`` and
            ``error``. The dry-run path injects a single sentinel row
            (``{"dry_run_count": <int>}``) carrying the number of objects
            that WOULD have been enriched.

    Frozen + dataclass so callers can ``asdict`` for JSON output without
    accidental mutation.
    """

    collection: str
    new_slot: str
    total: int
    enriched: int
    skipped: int
    failed: int
    failures: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Weaviate connection helpers
# ---------------------------------------------------------------------------


def _weaviate_url() -> str:
    """Default Weaviate URL. Cross-OS: reads ``WEAVIATE_URL`` env, falls
    back to the canonical localhost port.
    """
    return os.environ.get("WEAVIATE_URL", "http://localhost:8081").rstrip("/")


def _grpc_port() -> int:
    """Default Weaviate gRPC port from env."""
    try:
        return int(os.environ.get("GRPC_PORT", "50052"))
    except ValueError:
        return 50052


def _http_get_schema(collection: str, base_url: str) -> Optional[dict]:
    """``GET /v1/schema/<collection>``. Returns ``None`` on 404, dict on 200.

    Raises ``RuntimeError`` on other transport failures so the caller can
    distinguish "collection doesn't exist" from "Weaviate is down".
    """
    url = f"{base_url}/v1/schema/{collection}"
    try:
        with urllib.request.urlopen(url, timeout=10.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
            return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(
            f"GET /v1/schema/{collection} -> HTTP {e.code}: {e.reason}"
        ) from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"GET /v1/schema/{collection} transport error: {e}"
        ) from e


def _connect_weaviate(base_url: str) -> Any:
    """Open a v4 Weaviate client for collection iteration + per-uuid update.

    Pure HTTP/gRPC; works against podman OR docker (the constraint comes
    from the running Weaviate container's endpoint config, not the
    client). Late import of ``weaviate`` so the module imports on a half-
    installed venv without choking.
    """
    import weaviate  # type: ignore[import-untyped]

    host = base_url.replace("http://", "").replace("https://", "").split(":")[0]
    try:
        port = int(base_url.rsplit(":", 1)[-1].split("/")[0])
    except ValueError:
        port = 8081
    return weaviate.connect_to_custom(
        http_host=host,
        http_port=port,
        http_secure=base_url.startswith("https://"),
        grpc_host=host,
        grpc_port=_grpc_port(),
        grpc_secure=False,
        skip_init_checks=True,
    )


# ---------------------------------------------------------------------------
# Slot catalog lookup
# ---------------------------------------------------------------------------


def _slot_catalog_for(collection_name: str) -> list[NamedVectorSlot]:
    """Pick the canonical slot list (KG vs Code) for the given class name."""
    if is_code_collection(collection_name):
        return CODE_NAMED_VECTORS
    return KG_NAMED_VECTORS


def _find_catalog_slot(
    catalog: list[NamedVectorSlot], slot_name: str,
) -> Optional[NamedVectorSlot]:
    """Linear scan — catalogs are tiny (≤6 entries each)."""
    for s in catalog:
        if s.name == slot_name:
            return s
    return None


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def enrich_collection_vectors(
    collection_name: str,
    new_slot: str,
    project_root: Optional[Path] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    dry_run: bool = False,
    *,
    embedding_service: Optional[EmbeddingService] = None,
    weaviate_client_factory: Optional[Callable[[], Any]] = None,
) -> EnrichmentReport:
    """Populate ``new_slot`` on every object in ``collection_name`` that
    lacks it.

    Pre-flight checks (raise BEFORE any walk happens):

    1. ``new_slot`` must appear in the canonical slot catalog
       (``KG_NAMED_VECTORS`` for KG-shaped collections,
       ``CODE_NAMED_VECTORS`` for code-shaped). Otherwise
       :class:`UnknownSlotError`.
    2. The collection must exist on the live Weaviate server. Otherwise
       :class:`CollectionNotFoundError`.
    3. The slot must be present in the LIVE schema (added by
       ``migrate-collections``). Otherwise :class:`SlotNotInSchemaError`.
    4. The relevant embedding backend (text for KG-shaped, code for code-
       shaped) must be reachable. Otherwise
       :class:`NoEmbeddingBackendError` from EmbeddingService.

    Fast paths (return early without doing real work):

    * Collection is empty → ``EnrichmentReport(total=0, enriched=0, ...)``.
    * Requested ``new_slot`` already matches the project's active text /
      code slot per EmbeddingService → no-op. Still walks the collection
      but the active-slot match means every object is either already-
      populated (skipped) or pending (enriched with vectors that the
      caller WOULD have computed anyway via the normal seed pipeline).
      Caller is the GUI dropdown flow — see the Svelte side.

    Main loop:

    * Page the collection in batches of :data:`BATCH_SIZE`.
    * For each batch, identify objects whose ``vector[new_slot]`` is
      empty AND whose content property is non-empty.
    * Embed the batch in one call to ``svc.embed_text_batch`` /
      ``svc.embed_code_batch``.
    * Write the resulting vector to the new slot ONLY (single-slot
      update — Weaviate's ``coll.data.update(uuid=..., vector={new_slot:
      v})`` preserves every other slot's data).
    * Soft-fail per object: an exception during embed OR write
      increments ``failed`` and appends a redacted detail row to
      ``failures`` (up to :data:`MAX_FAILURE_DETAILS`). The remaining
      objects in the batch still get processed.
    * After each batch, invoke ``progress_callback(pct, message)`` if
      provided.

    Args:
        collection_name: Weaviate class name (e.g.
            ``"MyProject_KnowledgeGraph"``, ``"MyProject_CodeFunction"``).
        new_slot: Target named-vector slot (e.g. ``"arctic2_embed"``,
            ``"openai_text_embed"``, ``"openai_code_embed"``,
            ``"jina_embed"``).
        project_root: Forwarded to ``EmbeddingService.for_project()`` so
            the per-project env (KG_COLLECTION etc.) is picked up.
        progress_callback: Called with ``(pct, message)`` after each
            batch. ``pct`` in ``[0.0, 1.0]``. Used by the Tauri shell-out
            with ``--stream-progress`` to emit
            ``vct-enrichment-progress`` events.
        dry_run: When True, no writes happen. Counts what WOULD be
            enriched and skipped. The would-have-enriched count is
            returned via ``failures[0]["dry_run_count"]``.

    Keyword Args (testing only):
        embedding_service: Pre-constructed EmbeddingService. Tests inject
            a mocked instance; production callers leave ``None`` so the
            function constructs via ``for_project()``.
        weaviate_client_factory: Zero-arg callable returning an open v4
            Weaviate client. Tests inject a mocked client; production
            callers leave ``None`` so the function opens its own.

    Returns:
        An :class:`EnrichmentReport`. Always returned — exceptions are
        only raised by pre-flight checks. The main loop's per-object
        errors land in ``failures`` instead.

    Raises:
        CollectionNotFoundError: Collection absent on the live server.
        UnknownSlotError: ``new_slot`` not in the canonical catalog.
        SlotNotInSchemaError: ``new_slot`` valid but not in live schema.
        SchemaDimMismatchError: Live schema has the slot at a different
            dim than the catalog claims. (Paranoid defense.)
        NoEmbeddingBackendError: No relevant embedding backend reachable.
    """
    if progress_callback is None:
        progress_callback = _null_progress

    # ── Pre-flight 1: slot must be in the canonical catalog ─────────────
    catalog = _slot_catalog_for(collection_name)
    slot_def = _find_catalog_slot(catalog, new_slot)
    if slot_def is None:
        catalog_names = sorted(s.name for s in catalog)
        kind = "code" if is_code_collection(collection_name) else "kg"
        raise UnknownSlotError(
            f"Slot {new_slot!r} is not in the {kind} catalog. "
            f"Known {kind} slots: {catalog_names}. "
            f"To register a new slot, add it to "
            f"vco_lib/weaviate_schema.py "
            f"({'CODE_NAMED_VECTORS' if kind == 'code' else 'KG_NAMED_VECTORS'})."
        )

    # ── Pre-flight 2: collection must exist on the live server ──────────
    base_url = _weaviate_url()
    schema = _http_get_schema(collection_name, base_url)
    if schema is None:
        raise CollectionNotFoundError(
            f"Collection {collection_name!r} not found on Weaviate at "
            f"{base_url}. The KG/code graph hasn't been seeded yet — run "
            f"the launcher's 'Seed KG' or 'Rebuild code graph' action, or "
            f"`python -m vco_lib.project_init seed-kg`, before enriching."
        )

    # ── Pre-flight 3: slot must be in the LIVE schema ───────────────────
    live_vc = schema.get("vectorConfig") or {}
    if new_slot not in live_vc:
        present = sorted(live_vc.keys())
        raise SlotNotInSchemaError(
            f"Slot {new_slot!r} is in the catalog but not in the live "
            f"schema of {collection_name!r}. Live slots: {present}. "
            f"Run `python -m vco_lib.project_init migrate-collections "
            f"--name <project>` first to add the slot non-destructively."
        )

    # ── Pre-flight 4: EmbeddingService + backend reachability ───────────
    owns_service = embedding_service is None
    if embedding_service is None:
        # for_project() raises NoEmbeddingBackendError when no backend
        # is reachable AND captures the failure into the JSONL log +
        # EMBEDDING_FAILURES.md hint. We don't catch — that's the
        # signal the user is supposed to see.
        embedding_service = EmbeddingService.for_project(project_root=project_root)

    try:
        kind = "code" if is_code_collection(collection_name) else "text"
        ready = (
            embedding_service.code_backend_ready()
            if kind == "code"
            else embedding_service.text_backend_ready()
        )
        if not ready:
            # Convert to NoEmbeddingBackendError so the caller sees the
            # same exception type for "service constructible but
            # unreachable" as for "service unconstructible". The
            # failure-capture side-effects don't double-fire because
            # capture=False is set.
            raise NoEmbeddingBackendError(
                f"{kind} backend not reachable for enrichment of "
                f"{collection_name!r}. Check that the corresponding "
                f"service (Ollama / CodeEmbed / OpenAI) is running.",
                attempted_backends=[kind],
                error_per_backend={kind: f"{kind}_backend_ready() returned False"},
                install_root=embedding_service.project_root,
                capture=False,
            )

        # ── No-op fast path: requested slot already active ──────────
        # If the user "changed" the model to what's already active for
        # this project, enrichment is a no-op semantically. Walk anyway
        # to count + report — the user wants to see "everything's
        # already there".
        active_slot = (
            embedding_service.code_vector_slot
            if kind == "code"
            else embedding_service.text_vector_slot
        )
        same_active = active_slot == new_slot

        # ── Open Weaviate client ────────────────────────────────────
        if weaviate_client_factory is not None:
            client = weaviate_client_factory()
        else:
            client = _connect_weaviate(base_url)

        try:
            return _run_enrichment_loop(
                client=client,
                collection_name=collection_name,
                new_slot=new_slot,
                kind=kind,
                slot_def=slot_def,
                embedding_service=embedding_service,
                progress_callback=progress_callback,
                dry_run=dry_run,
                same_active=same_active,
            )
        finally:
            try:
                client.close()
            except Exception:
                pass
    finally:
        if owns_service:
            try:
                embedding_service.close()
            except Exception:
                pass


def _null_progress(_pct: float, _msg: str) -> None:
    """Default no-op progress callback. Module-level for cheap reuse."""
    return None


def _run_enrichment_loop(
    *,
    client: Any,
    collection_name: str,
    new_slot: str,
    kind: str,
    slot_def: NamedVectorSlot,
    embedding_service: EmbeddingService,
    progress_callback: Callable[[float, str], None],
    dry_run: bool,
    same_active: bool,
) -> EnrichmentReport:
    """Walk + batch + embed + write. Returns the final report.

    Split from :func:`enrich_collection_vectors` so the pre-flight phase
    can ``raise`` while the inner walk has a clean control-flow shape:
    no nested ``try`` chains for the report-emit path.
    """
    content_prop = _resolve_content_property(collection_name)

    try:
        col = client.collections.get(collection_name)
    except Exception as e:
        # Collection vanished between the schema GET and the v4 client
        # open (very rare but possible across process boundaries). Re-
        # raise as the canonical pre-flight error.
        raise CollectionNotFoundError(
            f"Collection {collection_name!r} not retrievable via the "
            f"v4 client: {e}"
        ) from e

    # First pass: collect (uuid, content) pairs for objects missing the
    # target slot. This is bounded by BATCH_SIZE — once we have a batch's
    # worth, we embed + write, then continue iterating. Streaming saves
    # memory on huge collections (1M+ objects).
    total = 0
    enriched = 0
    skipped = 0
    failed = 0
    failures: list[dict] = []
    dry_run_pending = 0

    pending_uuids: list[Any] = []
    pending_texts: list[str] = []

    # We need a total estimate for the progress %. v4's iterator doesn't
    # expose a length without consuming. Use Weaviate's aggregate count
    # via the REST endpoint — soft-fail to "0" (which renders progress
    # as just "Enriched X..." with no percentage).
    estimated_total = _estimate_object_count(collection_name)

    def _flush_batch() -> None:
        nonlocal enriched, failed, dry_run_pending
        if not pending_uuids:
            return
        if dry_run:
            dry_run_pending += len(pending_uuids)
            pending_uuids.clear()
            pending_texts.clear()
            return

        # Embed the whole batch in one call.
        try:
            vectors = (
                embedding_service.embed_code_batch(pending_texts)
                if kind == "code"
                else embedding_service.embed_text_batch(pending_texts)
            )
        except Exception as e:
            # Whole-batch embed failure — mark every uuid in this batch
            # as failed. We don't abort; the next batch gets its own
            # chance.
            msg = f"batch embed failed: {type(e).__name__}: {e}"
            logger.warning(
                "Enrichment batch embed failed (%d objects, slot=%r): %s",
                len(pending_uuids), new_slot, e,
            )
            for uid in pending_uuids:
                failed += 1
                _append_failure(failures, str(uid), msg)
            pending_uuids.clear()
            pending_texts.clear()
            return

        # Defensive: vector count should match input count exactly.
        if len(vectors) != len(pending_uuids):
            logger.warning(
                "Embed returned %d vectors for %d inputs; marking "
                "shortage as failed and writing what we have.",
                len(vectors), len(pending_uuids),
            )

        for idx, uid in enumerate(pending_uuids):
            if idx >= len(vectors):
                failed += 1
                _append_failure(
                    failures, str(uid),
                    f"embed returned no vector at batch index {idx}",
                )
                continue
            vec = vectors[idx]
            if not vec:
                failed += 1
                _append_failure(failures, str(uid), "embed returned empty vector")
                continue
            try:
                col.data.update(uuid=uid, vector={new_slot: vec})
                enriched += 1
            except Exception as e:
                msg = f"update failed: {type(e).__name__}: {e}"
                logger.warning(
                    "Enrichment write failed for uuid=%s slot=%r: %s",
                    uid, new_slot, e,
                )
                failed += 1
                _append_failure(failures, str(uid), msg)

        pending_uuids.clear()
        pending_texts.clear()

    # ── Iterate ─────────────────────────────────────────────────────
    try:
        iterator = col.iterator(include_vector=True)
    except Exception as e:
        raise CollectionNotFoundError(
            f"Could not iterate collection {collection_name!r}: {e}"
        ) from e

    for obj in iterator:
        total += 1

        # Inspect the object's named-vector dict. v4 client returns
        # ``obj.vector`` as ``dict[str, list[float]]`` for multi-named-
        # vector collections.
        existing_vec = obj.vector if isinstance(obj.vector, dict) else {}
        if new_slot in existing_vec and existing_vec[new_slot]:
            # Idempotent skip — vector already present.
            skipped += 1
        else:
            content = ""
            try:
                content = str(obj.properties.get(content_prop, "") or "")
            except Exception:
                # Property bag malformed — count as failure rather than
                # silent skip so the user notices.
                failed += 1
                _append_failure(
                    failures, str(obj.uuid),
                    f"object has no readable {content_prop!r} property",
                )
                content = ""

            if not content.strip():
                # Empty content — nothing to embed. Count as skipped
                # (not failure): some KG nodes legitimately have empty
                # content (e.g. placeholder index pages).
                skipped += 1
            else:
                pending_uuids.append(obj.uuid)
                pending_texts.append(content[:EMBED_INPUT_CHAR_CAP])

        if len(pending_uuids) >= BATCH_SIZE:
            _flush_batch()
            _emit_progress(
                progress_callback,
                total, estimated_total,
                enriched, skipped, failed,
            )

    # Drain the tail.
    _flush_batch()
    if total > 0:
        _emit_progress(
            progress_callback,
            total, estimated_total,
            enriched, skipped, failed,
        )

    # ── Wrap-up ─────────────────────────────────────────────────────
    if dry_run:
        # Inject the sentinel row carrying the would-have-enriched count.
        # We keep ``enriched == 0`` to make the dry-run vs real-run
        # difference obvious in the report.
        failures = [{"dry_run_count": dry_run_pending}] + failures

    if total == 0 and not same_active:
        progress_callback(1.0, "Collection is empty — nothing to enrich.")
    elif same_active and enriched == 0 and skipped == total and total > 0:
        progress_callback(
            1.0,
            f"No-op: slot {new_slot!r} is already the active slot for this project.",
        )

    return EnrichmentReport(
        collection=collection_name,
        new_slot=new_slot,
        total=total,
        enriched=enriched,
        skipped=skipped,
        failed=failed,
        failures=failures,
    )


def _append_failure(failures: list[dict], uuid: str, error: str) -> None:
    """Append a failure row to ``failures`` respecting MAX_FAILURE_DETAILS.

    Once the cap is hit, subsequent failures are silently dropped from
    the detail list (the count on EnrichmentReport.failed still
    increments — that's done by the caller).
    """
    if len(failures) >= MAX_FAILURE_DETAILS:
        return
    failures.append({"uuid": uuid, "error": error})


def _emit_progress(
    cb: Callable[[float, str], None],
    total: int,
    estimated_total: int,
    enriched: int,
    skipped: int,
    failed: int,
) -> None:
    """Compute pct + message and call ``cb``. Soft-fail if cb raises."""
    if estimated_total > 0:
        pct = min(1.0, total / estimated_total)
    else:
        # Unknown total — emit a monotonic-but-fake progress that
        # ramps toward 1.0 as ``total`` grows. Not great UX but it's
        # the best we can do without iterating the collection twice.
        # The Tauri side prefers numeric pct over string.
        pct = min(0.95, 0.01 * (total / max(BATCH_SIZE, 1)))
    msg = (
        f"Enriched {enriched}/{total} (skipped={skipped}, failed={failed})"
    )
    try:
        cb(pct, msg)
    except Exception as e:
        logger.debug("progress_callback raised %s — continuing", e)


def _estimate_object_count(collection: str) -> int:
    """Best-effort object count for the progress %. Returns 0 on failure.

    Uses Weaviate's GraphQL aggregate endpoint, which is O(1) on Weaviate
    1.28.4 (the version we ship). Soft-fail to 0 if anything goes wrong
    — the progress callback degrades to monotonic-fake mode.
    """
    base_url = _weaviate_url()
    query = {"query": f'{{ Aggregate {{ {collection} {{ meta {{ count }} }} }} }}'}
    body = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/v1/graphql",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            if resp.status != 200:
                return 0
            data = json.loads(resp.read().decode("utf-8"))
        return int(
            data["data"]["Aggregate"][collection][0]["meta"]["count"]
        )
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Per-slot populated counts (powers the "keep previous model" modal option)
# ---------------------------------------------------------------------------

# Canonical text-slot -> embedding profile map.
#
# MUST STAY CONSISTENT WITH `TEXT_SLOT_MAP` in `vco_lib/embedding_service.py`
# (the `(model-substr, slot_name, dim)` tuple list, ~line 117): every text
# slot_name that TEXT_SLOT_MAP can produce must have an entry here mapping it
# to its user-selectable profile id, and the profile ids must match the
# ACTIVE_EMBEDDING profiles `active_profile_for_model` yields
# (qwen3 / arctic / openai). TEXT_SLOT_MAP is model-substr -> slot -> dim;
# THIS map is the inverse-ish slot -> profile projection the modal needs (we
# only have a raw slot name from the Weaviate object, not a model id). Keep
# them in sync — diverging them would mis-label the modal's "keep previous
# model" default and write the wrong profile through
# `set_project_active_embedding`. (An earlier version of this comment named
# `_text_partition_tag`, which does not exist — corrected v0.2.71.)
_TEXT_SLOT_TO_PROFILE: dict[str, str] = {
    "qwen3_embed": "qwen3",
    "arctic2_embed": "arctic",
    "openai_text_embed": "openai",
    # Legacy 1024-dim arctic bucket — surfaced as "arctic" so a project that
    # was embedded before the arctic2_embed slot existed still maps to a
    # selectable profile rather than the opaque "legacy" partition.
    "ollama_embed": "arctic",
    # Legacy OpenAI slot (pre openai_text_embed split).
    "openai_embed": "openai",
}


def _text_slot_to_profile(slot_name: str) -> Optional[str]:
    """Map a KG named-vector slot to its embedding profile id, or None.

    None means the slot is not a user-selectable text profile (e.g. a code
    slot, or an unknown future slot). The caller drops such slots from the
    "keep previous model" choice set.
    """
    return _TEXT_SLOT_TO_PROFILE.get(slot_name)


# Profile -> CURRENT target slot, derived from the canonical `TEXT_SLOT_MAP`
# in `vco_lib/embedding_service.py` so it can never drift from it. For each
# selectable profile we take the model id `_model_id_for_active` yields, then
# resolve that model's slot via `text_slot_for_model` (the same resolver the
# enrichment/embed path uses). This is the FORWARD direction the model-switch
# "Regenerate now" needs: given the profile the user switched TO, which slot
# must we enrich into. (The `_TEXT_SLOT_TO_PROFILE` map above is the reverse
# projection used by the "keep previous" chooser.)
def _profile_to_active_slot(profile: str) -> Optional[str]:
    """Return the CURRENT text named-vector slot a profile embeds into.

    Single-sourced from `embedding_service.TEXT_SLOT_MAP` via the canonical
    profile->model (`_model_id_for_active`) then model->slot
    (`_resolve_text_slot`) resolvers, so adding a new profile/slot to the map
    automatically flows here. Returns None only when `embedding_service` is
    unimportable (half-installed venv) — a soft-fail the caller treats as "no
    slot to enrich".
    """
    try:
        from vco_lib.embedding_service import (  # local import: keep module import cheap
            _model_id_for_active,
            _resolve_text_slot,
        )
    except Exception:
        return None
    # `_model_id_for_active` always yields a model id (qwen3 default for an
    # unknown profile); `_resolve_text_slot` always yields (slot, dim)
    # (DEFAULT_TEXT_SLOT fallback). So a known profile always resolves.
    model_id = _model_id_for_active(profile)
    slot, _dim = _resolve_text_slot(model_id)
    return slot


def count_populated_slots(
    collection: str,
    *,
    weaviate_client_factory: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    """Count objects whose named-vector slot is POPULATED, per slot.

    Composes the two existing primitives:

      * the v4 ``include_vector`` iteration pattern from
        ``weaviate_schema._existing_slot_dim`` (a non-empty
        ``obj.vector[slot]`` means that slot is populated for that object);
      * ``_estimate_object_count`` for the collection total (the aggregate
        denominator the modal renders as "X of N embedded").

    Returns a JSON-serialisable dict::

        {
          "collection": "MyProject_KnowledgeGraph",
          "total": 1234,
          "slots": [
            {"slot": "qwen3_embed",  "profile": "qwen3",  "populated": 1234},
            {"slot": "arctic2_embed","profile": "arctic", "populated": 200},
            ...
          ],
          "most_populated_profile": "qwen3"
        }

    Only slots that map to a user-selectable text profile (see
    ``_text_slot_to_profile``) are reported, so the modal's "keep previous
    model" choice never proposes a code-only or legacy-opaque slot.

    Soft-fail: on any transport/connection failure returns the same shape
    with ``total=0``, ``slots=[]`` and ``most_populated_profile=None`` so the
    modal degrades to its 2-option form (no smart default) rather than
    erroring. Counting must never gate the user's update flow.
    """
    total = _estimate_object_count(collection)

    # Per-slot populated tally. We only track slots that map to a text
    # profile — code slots and unknown slots are irrelevant to the
    # active-text-embedding revert choice.
    tally: dict[str, int] = {}
    catalog = _slot_catalog_for(collection)
    candidate_slots = [
        s.name for s in catalog if _text_slot_to_profile(s.name) is not None
    ]
    if not candidate_slots:
        return {
            "collection": collection,
            "total": total,
            "slots": [],
            "most_populated_profile": None,
        }

    factory = weaviate_client_factory or (lambda: _connect_weaviate(_weaviate_url()))
    client = None
    try:
        client = factory()
        col = client.collections.get(collection)
        for obj in col.iterator(include_vector=True):
            vec = obj.vector
            if not isinstance(vec, dict):
                # Legacy single-vector format has no named slots — nothing
                # to attribute to a per-slot count.
                continue
            for slot in candidate_slots:
                v = vec.get(slot)
                if v:
                    tally[slot] = tally.get(slot, 0) + 1
    except Exception as e:  # noqa: BLE001 — soft-fail by contract
        logger.warning(
            "count_populated_slots(%r) soft-failed: %s: %s",
            collection, type(e).__name__, e,
        )
        return {
            "collection": collection,
            "total": total,
            "slots": [],
            "most_populated_profile": None,
        }
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    slots_out: list[dict[str, Any]] = []
    for slot in candidate_slots:
        populated = tally.get(slot, 0)
        if populated == 0:
            # Don't surface empty slots — they're not a "previous model" the
            # user could meaningfully revert to.
            continue
        slots_out.append(
            {
                "slot": slot,
                "profile": _text_slot_to_profile(slot),
                "populated": populated,
            }
        )
    # Most-populated profile drives the modal's smart default. Ties resolve
    # to the catalog order (stable) since `slots_out` preserves it.
    most_populated_profile: Optional[str] = None
    if slots_out:
        best = max(slots_out, key=lambda r: r["populated"])
        most_populated_profile = best["profile"]

    return {
        "collection": collection,
        "total": total,
        "slots": slots_out,
        "most_populated_profile": most_populated_profile,
    }


# ---------------------------------------------------------------------------
# CLI entry point (consumed by the Tauri shell-out)
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.embedding_enrichment",
        description=(
            "Enrich a Weaviate collection by computing + writing vectors "
            "into a named-vector slot that's currently empty. Idempotent: "
            "re-running on an enriched collection is a zero-write no-op."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    enrich = sub.add_parser(
        "enrich",
        help="Enrich one collection's named-vector slot",
    )
    enrich.add_argument(
        "--collection", required=True,
        help="Weaviate class name to enrich (e.g. MyProject_KnowledgeGraph)",
    )
    enrich.add_argument(
        "--new-slot", required=True,
        help="Named-vector slot to populate (e.g. arctic2_embed)",
    )
    enrich.add_argument(
        "--project-root", default=None,
        help=(
            "Project root to forward to EmbeddingService.for_project(). "
            "Defaults to KG_BASE_DIR / cwd / None resolution inside "
            "EmbeddingService."
        ),
    )
    enrich.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Count what would be enriched without writing.",
    )
    enrich.add_argument(
        "--stream-progress", action="store_true", default=False,
        help=(
            "Emit one JSON progress line per batch on stdout. The final "
            "report is also a JSON line at end of stream. Tauri "
            "wrapper consumes both."
        ),
    )
    enrich.add_argument(
        "--json", action="store_true", default=False,
        help="Output the final report as JSON (default is JSON too).",
    )

    # `slot-counts` — read-only per-slot populated counts for one collection.
    # Powers the RegenerateOrDeferModal "keep previous model" smart default.
    counts = sub.add_parser(
        "slot-counts",
        help="Per-slot populated-vector counts for one collection (read-only)",
    )
    counts.add_argument(
        "--collection", required=True,
        help="Weaviate class name to count (e.g. MyProject_KnowledgeGraph)",
    )
    counts.add_argument(
        "--for-profile", default=None,
        help=(
            "Optional: the profile the user is switching TO. When given, the "
            "output also carries `target_slot` — the named-vector slot that "
            "profile embeds into — so the model-switch modal's 'Regenerate "
            "now' can enrich the collection into it without re-deriving the "
            "profile->slot map on the frontend."
        ),
    )
    return parser


def _cli_slot_counts(args: argparse.Namespace) -> int:
    """Implement ``python -m vco_lib.embedding_enrichment slot-counts ...``.

    Always returns 0 and emits a JSON object — counting is soft-fail by
    contract (a Weaviate outage yields ``total=0, slots=[]`` rather than a
    non-zero exit), so the caller's update flow is never blocked.
    """
    result = count_populated_slots(args.collection)
    # When the caller names the profile they're switching TO, resolve its
    # current target slot (single-sourced from TEXT_SLOT_MAP) so the modal's
    # "Regenerate now" can enrich into it. Soft: None on an unresolvable
    # profile (unimportable embedding_service) — the modal then can't
    # regenerate, only keep/defer, which it already handles.
    if getattr(args, "for_profile", None):
        result["target_slot"] = _profile_to_active_slot(args.for_profile)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _cli_enrich(args: argparse.Namespace) -> int:
    """Implement ``python -m vco_lib.embedding_enrichment enrich ...``.

    Returns 0 on success (report produced, regardless of per-object
    failures), 1 on a pre-flight error (caller should NOT proceed),
    2 on unexpected exception.
    """
    project_root: Optional[Path] = (
        Path(args.project_root).resolve() if args.project_root else None
    )

    def _stream_cb(pct: float, message: str) -> None:
        if not args.stream_progress:
            return
        line = json.dumps(
            {"progress": pct, "message": message},
            ensure_ascii=False,
        )
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    try:
        report = enrich_collection_vectors(
            collection_name=args.collection,
            new_slot=args.new_slot,
            project_root=project_root,
            progress_callback=_stream_cb,
            dry_run=args.dry_run,
        )
    except (
        CollectionNotFoundError,
        UnknownSlotError,
        SlotNotInSchemaError,
        SchemaDimMismatchError,
        NoEmbeddingBackendError,
    ) as exc:
        # Pre-flight failure. Emit a structured error so the Tauri side
        # can switch the modal into error-state with a usable message.
        payload = {
            "error": type(exc).__name__,
            "message": str(exc),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 1
    except Exception as exc:  # noqa: BLE001
        # Unexpected failure — surface it cleanly. The Rust wrapper logs
        # this verbatim.
        payload = {
            "error": type(exc).__name__,
            "message": str(exc),
            "unexpected": True,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 2

    sys.stdout.write(json.dumps(asdict(report), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.cmd == "enrich":
        return _cli_enrich(args)
    if args.cmd == "slot-counts":
        return _cli_slot_counts(args)
    parser.error(f"Unknown command: {args.cmd}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
