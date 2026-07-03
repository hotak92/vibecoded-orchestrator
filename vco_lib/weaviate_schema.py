# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Multi-slot named-vector schema helper for Weaviate KG + code collections.

v0.2.18 introduces a canonical named-vector slot catalog that's shared across
schema creation (`vco_lib/project_init.py::kg_class_definition` etc.) and
schema migration (`vco_lib/project_init.py::migrate_collections` + a new
`migrate_collections_to_v0218_schema` helper exposed via the
`migrate-collections` CLI subcommand).

The design constraint: **schema changes are additive**. Adding a new slot to
an existing collection must preserve all data already in the existing slots.

Background — why this module exists:
  * Weaviate ≤1.30 (we ship 1.28.4) marks `vectorConfig` immutable. PUT/PATCH
    to add a named-vector slot returns HTTP 422 "vector config is
    immutable". The only way to widen a collection's slot set is to recreate
    it with the new target schema and round-trip every object (UUID +
    vectors) through a staging class.
  * `project_init.migrate_collections` already implements that round-trip
    safely (`_copy_collection_with_vectors` + `__staging` double-copy +
    crash recovery). What's missing pre-v0.2.18 is: (a) a single catalog
    that names every slot the orchestrator wants to support, (b) a
    code-graph leg (the existing path only walks KG + Dev collections).
  * `claude_mcp_servers/scripts/migrate_to_new_embeddings.py` has been
    doing the manual "delete + recreate + reinsert" for ad-hoc additions;
    this module wraps the same pattern behind a clean API so the GUI
    "change embedding model" flow (Commit 9 of v0.2.18) can drive it.

Layering rule: this module declares slot **catalogs** and a thin
idempotent-add API. It does NOT compute embeddings or talk to Ollama /
OpenAI / CodeEmbed — that's `vco_lib/embedding_service.py` (Commit 2).
A consumer that wants to add a new slot AND backfill it computes vectors
itself (or calls into the EmbeddingService) and then does a one-shot
`coll.data.update(uuid=..., vector={new_slot: <list[float]>})` per object.
This separation keeps the schema layer pure and testable in isolation.

Slot catalog (the LOCKED v0.2.18 target):

  KG (KG_NAMED_VECTORS, applies to per-project KG + shared KG + Development
  collections — the three KG-shaped classes all use the same slot set):
    - qwen3_embed          (1024d, qwen3-embedding:0.6b)
    - ollama_embed         (1024d, snowflake-arctic-embed2 — LEGACY, kept)
    - openai_embed         (1536d, text-embedding-3-small — LEGACY, kept)
    - arctic2_embed        (1024d, snowflake-arctic-embed:l2 — NEW)
    - openai_text_embed    (1536d, text-embedding-3-small — NEW, replaces
                            the historical `openai_embed` slot on KG
                            collections; both kept for back-compat)

  Code (CODE_NAMED_VECTORS, applies to CodeModule / CodeClass /
  CodeFunction / CodeAPI / CodeInteraction):
    - codesage_embed       (2048d, CodeSage-Large-v2)
    - ollama_code_embed    (768d, llama-3.2-3b code embed — LEGACY, kept)
    - openai_embed         (1536d, text-embedding-3-small — LEGACY, kept)
    - qwen3_embed          (1024d, qwen3-embedding:0.6b — NEW, Ollama
                            CPU fallback path for code)
    - jina_embed           (768d, jina-embeddings-v2-base-code — NEW)
    - openai_code_embed    (1536d, text-embedding-3-small — NEW, separate
                            slot from openai_embed so a future code-tuned
                            OpenAI model can swap in without disturbing KG)

The legacy slots (`ollama_embed`, `openai_embed`, `ollama_code_embed`) are
deliberately retained in the v0.2.18 target. Rationale: v0.2.17 installs
have data in these slots and rebuilding a collection drops it. Carrying
them forward as part of the target makes `_schema_delta` see the existing
slots as "already present" (only the new ones become `missing_vec_slots`)
and lets the copy-with-vectors round-trip preserve every vector.

A future "purge legacy slots" command is explicitly OUT OF SCOPE for
v0.2.18.

All slots use `vectorizer="none"`: the MCP server / sync scripts compute
embeddings out-of-band (Ollama, CodeEmbed service, OpenAI) and POST
pre-computed vectors. Weaviate never invokes a vectorizer module.
"""

from __future__ import annotations

import enum
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

# Default Weaviate port (mirrors `vco_lib.project_init.DEFAULT_WEAVIATE_PORT`
# without importing it — this module stays import-cheap so the schema
# catalog can be picked up by static analysis / docs tooling without
# pulling in the rest of project_init).
DEFAULT_WEAVIATE_PORT = 8081


# ---------------------------------------------------------------------------
# Slot definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NamedVectorSlot:
    """A single named-vector slot in a Weaviate collection's vectorConfig.

    Attributes:
        name: The slot key (e.g. "qwen3_embed"). Must be unique within
            a collection. Naming convention: ``<provider>_<role>_embed``
            (we relax the `_role_` part for historical slots that ship
            without it — e.g. `qwen3_embed`, `codesage_embed`).
        dim: The vector dimensionality. Weaviate doesn't store this in the
            schema (it's inferred from the first vector posted), but the
            catalog records it so consumers can sanity-check before
            embedding + uploading. A `DimMismatchError` from the slot-add
            helper means the caller tried to register the same slot at a
            different dim than already-stored vectors — a degenerate state
            requiring manual intervention.
        vectorizer: Always `"none"` for the v0.2.18 catalog. Future slots
            that delegate to Weaviate's built-in vectorizer modules (e.g.
            text2vec-openai) would set this to the module name.
    """
    name: str
    dim: int
    vectorizer: str = "none"

    def to_weaviate_config(
        self,
        *,
        index_type: str = "hnsw",
        distance: str = "cosine",
        rescore_limit: int = -1,
    ) -> dict:
        """Serialize this slot into the JSON shape Weaviate's REST
        `POST /v1/schema` expects under the `vectorConfig` key.

        Returns the inner dict, NOT keyed by slot name. Callers compose
        ``{slot.name: slot.to_weaviate_config()}`` themselves so they can
        merge with other slots.

        Parameters
        ----------
        index_type:
            ``"hnsw"`` (default — the ONLY shipped default) or ``"hfresh"``.

            **v0.2.73 FIX-D4 — HFresh is PREVIEW + carries hard constraints
            (doc-verified against the 1.37 docs, 2026-07-03):**

            1. *HFresh MANDATES rotational quantization (RQ) — it cannot be
               turned off.* A collection born ``vectorIndexType:hfresh`` gets
               lossy RQ compression on its vectors. So the "vector-preserving
               copy" preserves the STORED client vectors but the INDEX
               quantizes them (a recall tradeoff). This is precisely WHY
               HFresh stays opt-in/flagged and is never a silent swap. When
               ``index_type='hfresh'`` an ``vectorIndexConfig`` with a
               ``rescoreLimit`` is emitted so callers can trade recall back
               for latency; the caller passes ``rescore_limit`` (>=0) to set
               it, or leaves ``-1`` to accept the engine default.
            2. *HFresh supports ONLY ``cosine`` + ``l2-squared`` distance
               (NOT ``dot``).* ``distance='dot'`` with ``index_type='hfresh'``
               raises ``ValueError`` up-front (a clear, actionable error) so
               the migrate path can route to a documented fallback rather than
               failing opaque at ``POST /v1/schema``.

            The default stays ``hnsw`` — do NOT flip it. The integrator runs a
            mandatory scratch-test on a real 1.37 engine (HFresh × client-
            supplied named vectors is DOCS-SILENT) before any default flip.

        distance:
            Distance metric for the vector index. Weaviate default is
            ``cosine`` (what VCO's KG + code collections use). Only relevant
            to validate against the HFresh cosine/l2-squared constraint above;
            when ``hnsw`` (the default) this parameter is accepted for API
            symmetry but the metric is left to Weaviate's own default unless
            explicitly non-cosine (kept minimal to avoid changing the shipped
            hnsw schema shape).

        rescore_limit:
            Only consulted for ``index_type='hfresh'``. ``>=0`` sets
            ``vectorIndexConfig.rescoreLimit``; ``-1`` (default) omits it and
            accepts the engine default.
        """
        it = (index_type or "hnsw").strip().lower()
        if it not in ("hnsw", "hfresh"):
            raise ValueError(
                f"unsupported vectorIndexType {index_type!r} "
                "(supported: 'hnsw', 'hfresh')"
            )

        if it == "hnsw":
            # UNCHANGED shipped shape — do not perturb the hnsw schema so
            # existing collections keep classifying as `noop` (the schema
            # delta must not see a spurious diff on a plain hnsw upgrade).
            cfg = {
                "vectorizer": {self.vectorizer: {}},
                "vectorIndexType": "hnsw",
            }
            return cfg

        # ── HFresh (preview, GATED) ─────────────────────────────────────
        # HFresh × distance guard (doc-verified): cosine + l2-squared only.
        dist = (distance or "cosine").strip().lower()
        _HFRESH_DISTANCES = {"cosine", "l2-squared"}
        if dist not in _HFRESH_DISTANCES:
            raise ValueError(
                f"HFresh vectorIndexType supports only {sorted(_HFRESH_DISTANCES)} "
                f"distance — got {distance!r}. A collection using {distance!r} "
                "cannot migrate to HFresh; keep it on hnsw (route to the "
                "documented fallback, do not force the swap)."
            )
        # HFresh forces RQ (mandatory — not disableable). We emit an explicit
        # vectorIndexConfig so the RQ tradeoff is visible in the schema and a
        # rescoreLimit is tunable. `distance` is set so the config is
        # self-describing (and so a non-cosine l2-squared choice round-trips).
        vic: dict = {"distance": dist}
        if isinstance(rescore_limit, int) and rescore_limit >= 0:
            vic["rescoreLimit"] = rescore_limit
        return {
            "vectorizer": {self.vectorizer: {}},
            "vectorIndexType": "hfresh",
            "vectorIndexConfig": vic,
        }


# ---------------------------------------------------------------------------
# Canonical slot catalogs (v0.2.18 target)
# ---------------------------------------------------------------------------


# KG-shaped collections (per-project KG, shared KG, Development collection).
# Order matters only for stable iteration / migration reports — Weaviate
# treats vectorConfig as an unordered map.
#
# Legacy slots (`ollama_embed`, `openai_embed`) are RETAINED so v0.2.17 ->
# v0.2.18 upgrades preserve data round-tripped through the migration's
# copy-with-vectors path.
KG_NAMED_VECTORS: list[NamedVectorSlot] = [
    # Active primary (v0.2.17 default; v0.2.18 keeps as one valid choice).
    NamedVectorSlot("qwen3_embed", 1024),
    # Legacy slots kept for back-compat data preservation. See module
    # docstring for the "no purge in v0.2.18" decision.
    NamedVectorSlot("ollama_embed", 1024),
    NamedVectorSlot("openai_embed", 1536),
    # NEW in v0.2.18 — added to existing collections via
    # `migrate_collection_to_target` (copy-with-vectors round-trip).
    NamedVectorSlot("arctic2_embed", 1024),
    NamedVectorSlot("openai_text_embed", 1536),
]


# Code-graph collections (`CodeModule`, `CodeClass`, `CodeFunction`,
# `CodeAPI`, `CodeInteraction`, including per-project prefix variants).
# Legacy slots (`ollama_code_embed`, `openai_embed`) retained for the
# same back-compat reason as the KG catalog.
CODE_NAMED_VECTORS: list[NamedVectorSlot] = [
    # Active primary (GPU CodeSage path).
    NamedVectorSlot("codesage_embed", 2048),
    # Legacy slots kept for back-compat data preservation.
    NamedVectorSlot("ollama_code_embed", 768),
    NamedVectorSlot("openai_embed", 1536),
    # NEW in v0.2.18.
    NamedVectorSlot("qwen3_embed", 1024),     # Ollama CPU fallback for code
    NamedVectorSlot("jina_embed", 768),       # jina-embeddings-v2-base-code
    NamedVectorSlot("openai_code_embed", 1536),
]


# Code-collection basename suffixes — used by callers that want to know if
# a class is code-shaped vs KG-shaped (e.g. `_resolve_target_slots`).
# Matches the constant in `claude_mcp_servers/scripts/migrate_to_new_embeddings.py`.
_CODE_COLLECTION_SUFFIXES: frozenset[str] = frozenset(
    {"CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"}
)


def embedding_schema_fingerprint(name: str) -> str:
    """v0.2.60: a stable fingerprint of the EMBED-RELEVANT schema for a
    collection, used to gate re-embedding on an ACTUAL embedding-schema
    change rather than on a version-number bump.

    The fingerprint hashes ONLY the things whose change actually invalidates
    stored vectors: the named-vector slot catalog as ``(slot_name, dim)``
    pairs (sorted for order-independence — Weaviate treats vectorConfig as an
    unordered map). It deliberately EXCLUDES property additions, descriptions,
    inverted-index flags, and the schema VERSION integer — none of those
    require re-embedding (a new property or an additive slot is a
    copy-with-vectors / patch_props operation, never a re-embed).

    Two collections with the same fingerprint hold vectors that are still
    valid under the current catalog → NO re-embed needed. A changed
    fingerprint (a slot removed, or a slot's dim changed) means the stored
    vectors no longer match the catalog → re-embed/rebuild required.

    Per-shape: code-shaped classes use ``CODE_NAMED_VECTORS``, everything
    else uses ``KG_NAMED_VECTORS`` (matches how the rest of the module
    resolves the target catalog).

    Returns a short hex digest. Stable across processes/machines (pure
    function of the in-code catalog), so it can be stored alongside the
    artifact_schema_versions row and compared on the next update.
    """
    from vco_lib.hashing import sha256_text

    catalog = CODE_NAMED_VECTORS if is_code_collection(name) else KG_NAMED_VECTORS
    # Sorted (name, dim) pairs — the ONLY embed-invalidating schema facts.
    payload = ";".join(
        f"{slot.name}:{slot.dim}" for slot in sorted(catalog, key=lambda s: s.name)
    )
    return sha256_text(payload)[:24]


def is_code_collection(name: str) -> bool:
    """Best-effort check: does `name` look like a code-graph class?

    Handles per-project prefixes ("MyProject_CodeFunction" -> True) and
    bare names ("CodeFunction" -> True). Names with no code suffix
    match-anywhere return False, even on partial substring overlap
    (e.g. "CodeBase" is not a code collection — it doesn't end in one of
    the canonical suffixes).
    """
    for suffix in _CODE_COLLECTION_SUFFIXES:
        if name.endswith(suffix) or name == suffix:
            return True
    return False


# ---------------------------------------------------------------------------
# Result types for the slot-add API
# ---------------------------------------------------------------------------


class AddSlotResult(enum.Enum):
    """Outcome enum for `add_named_vector_slot`.

    Created
        The slot was absent on the target collection and has been added
        (via collection recreate + data round-trip — Weaviate 1.28.4 does
        not support live PATCH-add of named vectors).
    Skipped
        The slot already exists on the target collection (and, if a dim
        probe was possible, the existing stored vector dim matches the
        target). This is the idempotent re-run case.
    DimMismatchError
        The slot exists but stored vectors have a different
        dimensionality than the target catalog claims. Caller must
        investigate (likely a user-managed collection that took a foreign
        slot name); we refuse to "fix" it automatically.
    """
    Created = "created"
    Skipped = "skipped"
    DimMismatchError = "dim_mismatch"


@dataclass
class MigrationReport:
    """Summary of a `migrate_collection_to_target` invocation.

    Attributes:
        collection: The collection name that was processed.
        added_slots: Slot names successfully added in this run (empty if
            the collection was already at-target).
        skipped_slots: Slot names that were already present (idempotent
            no-op).
        errors: List of error dicts:
            ``{"slot": str, "reason": str}``. Populated by DimMismatch
            and transport failures. Non-empty `errors` means the
            collection didn't reach the target schema.
        objects_copied: Total objects round-tripped during the migration.
            Zero if the collection was already at target (no recreate
            needed) OR if the collection had no data.
    """
    collection: str
    added_slots: list[str] = field(default_factory=list)
    skipped_slots: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    objects_copied: int = 0

    def ok(self) -> bool:
        """True iff every slot from the target list is now present and
        no errors were raised. A no-op run (already-at-target) returns
        True.
        """
        return not self.errors


# ---------------------------------------------------------------------------
# HTTP helpers (mirrors the urllib pattern in vco_lib.project_init for
# parity; we intentionally don't import from project_init to avoid a
# circular dependency at import time)
# ---------------------------------------------------------------------------


def _weaviate_url_default() -> str:
    """Default Weaviate URL. Late import of `os` so doc generators can
    introspect this module without an `os.environ` side-effect at import
    time."""
    import os
    return os.environ.get(
        "WEAVIATE_URL", f"http://localhost:{DEFAULT_WEAVIATE_PORT}"
    )


def _http_request(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    """Thin urllib wrapper. Returns (status, body_bytes). Never raises on
    non-2xx — caller decides what to do.
    """
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, resp.read())
    except urllib.error.HTTPError as e:
        try:
            return (e.code, e.read())
        except Exception:
            return (e.code, b"")


def _fetch_schema(
    collection: str, weaviate_url: Optional[str] = None
) -> Optional[dict]:
    """GET /v1/schema/<collection>. Returns dict on 200, None on 404.
    Raises on transport failure (network down, malformed URL).
    """
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    status, body = _http_request("GET", f"{base}/v1/schema/{collection}")
    if status == 200:
        return json.loads(body.decode("utf-8"))
    if status == 404:
        return None
    raise RuntimeError(
        f"GET /v1/schema/{collection} -> HTTP {status}: {body[:200]!r}"
    )


def _existing_slot_dim(
    collection: str,
    slot_name: str,
    *,
    weaviate_url: Optional[str] = None,
    sample_limit: int = 10,
) -> Optional[int]:
    """Probe the actual stored vector dim for `slot_name` on `collection`.

    Returns the dim of the first non-empty vector found in `sample_limit`
    objects, or None if no object has a populated vector for that slot
    (slot exists in schema but no data written yet, or collection is
    empty). Used by `add_named_vector_slot` to detect dim mismatch
    against the catalog.

    We use the v4 client because /v1/objects?include=vector returns a
    flat vector for legacy collections without named vectors and we
    don't want to misinterpret. The v4 iterator yields
    `obj.vector: dict[str, list[float]]` for multi-named-vector
    collections, which is unambiguous.

    Defensive: if the client can't connect or the collection has no
    objects, returns None (caller treats as "can't probe, accept the
    target"). DOES NOT raise on transport failure — schema mutations
    can't be safely gated behind transport reliability.
    """
    try:
        import weaviate  # type: ignore[import-untyped]
    except ImportError:
        return None

    url = weaviate_url or _weaviate_url_default()
    host = url.replace("http://", "").replace("https://", "").split(":")[0]
    try:
        port = int(url.rsplit(":", 1)[-1].split("/")[0])
    except ValueError:
        port = DEFAULT_WEAVIATE_PORT
    import os
    grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
    try:
        client = weaviate.connect_to_custom(
            http_host=host,
            http_port=port,
            http_secure=url.startswith("https://"),
            grpc_host=host,
            grpc_port=grpc_port,
            grpc_secure=False,
            skip_init_checks=True,
        )
    except Exception:
        return None
    try:
        col = client.collections.get(collection)
        seen = 0
        for obj in col.iterator(include_vector=True):
            seen += 1
            vec = obj.vector
            if isinstance(vec, dict):
                v = vec.get(slot_name)
                if v:
                    return len(v)
            # Legacy single-vector format won't have named slots; skip.
            if seen >= sample_limit:
                break
        return None
    except Exception:
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def diff_collection_vs_target(
    collection: str,
    target_slots: list[NamedVectorSlot],
    *,
    weaviate_url: Optional[str] = None,
    schema_fetcher: Any = None,
) -> list[NamedVectorSlot]:
    """Return the subset of `target_slots` that don't yet exist on the
    collection.

    Returns:
        A list (preserving input order) of slots from `target_slots`
        whose `.name` is NOT present in the collection's current
        `vectorConfig`. Slots that already exist — even with a different
        dim — are NOT returned by this function (the dim check happens
        in `add_named_vector_slot`).

    Args:
        collection: Weaviate class name.
        target_slots: Desired catalog (e.g. `KG_NAMED_VECTORS`).
        weaviate_url: Override default (`WEAVIATE_URL` env or
            `http://localhost:8081`).
        schema_fetcher: Test injection point — callable
            ``(collection_name) -> Optional[dict]`` returning the schema
            dict the live `_fetch_schema` would return. Lets unit tests
            avoid spinning up Weaviate.

    Raises:
        RuntimeError: if the collection doesn't exist (404). Caller
            decides whether to create it or skip — this function only
            diffs.
        RuntimeError: on transport failure to Weaviate.
    """
    fetch = schema_fetcher or (
        lambda c: _fetch_schema(c, weaviate_url=weaviate_url)
    )
    schema = fetch(collection)
    if schema is None:
        raise RuntimeError(
            f"collection {collection!r} not found — cannot diff slots"
        )
    actual_vc = schema.get("vectorConfig") or {}
    actual_names = set(actual_vc.keys())
    return [s for s in target_slots if s.name not in actual_names]


def add_named_vector_slot(
    collection: str,
    slot: NamedVectorSlot,
    *,
    weaviate_url: Optional[str] = None,
    schema_fetcher: Any = None,
    collection_rebuilder: Any = None,
) -> AddSlotResult:
    """Idempotently ensure `slot` exists on `collection`.

    Behaviour:
      * Slot absent on collection -> recreate collection with target
        schema (existing slots preserved + new slot added) via
        `collection_rebuilder`. Returns `AddSlotResult.Created`.
      * Slot present, stored vectors have matching dim -> no-op. Returns
        `AddSlotResult.Skipped`.
      * Slot present, stored vectors have DIFFERENT dim -> no
        modification. Returns `AddSlotResult.DimMismatchError`. Caller
        inspects the returned-but-not-acted-on state via a follow-up
        call to `diff_collection_vs_target` or by reading the existing
        slot's actual dim.

    Args:
        collection: Weaviate class name.
        slot: The catalog entry to ensure.
        weaviate_url: Override default URL.
        schema_fetcher: Test injection — callable returning schema dict
            for a class name.
        collection_rebuilder: Callable used to perform the recreate +
            data round-trip when a new slot must be added. Signature::

                collection_rebuilder(
                    collection: str,
                    target_slots: list[NamedVectorSlot],
                    weaviate_url: Optional[str],
                ) -> int  # objects copied

            Defaults to `_default_collection_rebuilder` (which delegates
            to `project_init._copy_collection_with_vectors` via a
            staging double-copy). Tests inject a fake for offline runs.

    Notes for callers:
      * "Idempotent" means: running this twice in a row produces
        Created+Skipped (or Skipped+Skipped). It does NOT mean
        "idempotent across concurrent runs" — the data round-trip is not
        crash-safe on its own. Use `migrate_collection_to_target` (or
        `project_init.migrate_collections`) which wraps this in the
        full staging-double-copy + crash-recovery harness.
      * This function does NOT compute embeddings for the new slot.
        Adding a slot makes Weaviate accept writes to it; backfilling
        existing objects with vectors for the new slot is the
        consumer's job (see `embedding_enrichment.py` in Commit 9).
    """
    if collection_rebuilder is None:
        collection_rebuilder = _default_collection_rebuilder

    fetch = schema_fetcher or (
        lambda c: _fetch_schema(c, weaviate_url=weaviate_url)
    )
    schema = fetch(collection)
    if schema is None:
        raise RuntimeError(
            f"collection {collection!r} not found — cannot add slot"
        )
    actual_vc = schema.get("vectorConfig") or {}

    if slot.name in actual_vc:
        # Slot exists — check dim if any data is stored.
        existing_dim = _existing_slot_dim(
            collection, slot.name, weaviate_url=weaviate_url,
        )
        if existing_dim is not None and existing_dim != slot.dim:
            return AddSlotResult.DimMismatchError
        return AddSlotResult.Skipped

    # Slot absent — compute union of existing slots + new slot and
    # recreate. The collection_rebuilder handles staging, copy, and
    # post-recreate property restore.
    existing_slots = _decode_existing_slots(actual_vc)
    new_target = list(existing_slots) + [slot]
    try:
        copied = collection_rebuilder(
            collection, new_target, weaviate_url=weaviate_url,
        )
    except Exception as e:
        raise RuntimeError(
            f"failed to rebuild {collection!r} for slot "
            f"{slot.name!r}: {type(e).__name__}: {e}"
        ) from e

    # Sanity: the rebuilder should have round-tripped objects without
    # data loss. Surface a soft warning if zero objects copied AND the
    # collection had data before; for now we trust the rebuilder
    # (the project_init version raises on mismatch already).
    _ = copied
    return AddSlotResult.Created


def migrate_collection_to_target(
    collection: str,
    target_slots: list[NamedVectorSlot],
    *,
    weaviate_url: Optional[str] = None,
    schema_fetcher: Any = None,
    collection_rebuilder: Any = None,
) -> MigrationReport:
    """Add all missing slots from `target_slots` to `collection`.

    Idempotent — running twice produces 0 new additions on the second
    pass (every slot already in actual schema -> all `Skipped`).

    Implementation: this is a thin wrapper around `add_named_vector_slot`
    that batches the schema rebuild. Each missing slot triggers a single
    recreate-with-target-schema cycle; subsequent slots in the same call
    re-read the fresh schema and skip (because the first rebuild already
    added them as part of the union).

    Returns:
        A `MigrationReport` listing added / skipped slots and per-slot
        errors. `report.ok()` is True iff every slot in `target_slots`
        is now present on the collection.

    Caller contract: this function does NOT compute embeddings. After
    `migrate_collection_to_target` succeeds, the new slots are empty
    on all objects. A backfill step (Commit 9's enrichment migration)
    runs separately to populate vectors for the new slots.
    """
    if collection_rebuilder is None:
        collection_rebuilder = _default_collection_rebuilder

    fetch = schema_fetcher or (
        lambda c: _fetch_schema(c, weaviate_url=weaviate_url)
    )

    report = MigrationReport(collection=collection)

    # Re-read the schema between slot adds — each successful add
    # triggers a collection rebuild that ALSO incorporates other
    # missing slots from the union (see `add_named_vector_slot` -> the
    # rebuild's `new_target` includes every existing slot, plus the
    # newly-required one). After the first add, the others are no-ops.
    for slot in target_slots:
        try:
            outcome = add_named_vector_slot(
                collection,
                slot,
                weaviate_url=weaviate_url,
                schema_fetcher=fetch,
                collection_rebuilder=collection_rebuilder,
            )
        except Exception as e:
            report.errors.append({
                "slot": slot.name,
                "reason": f"{type(e).__name__}: {e}",
            })
            continue

        if outcome == AddSlotResult.Created:
            report.added_slots.append(slot.name)
        elif outcome == AddSlotResult.Skipped:
            report.skipped_slots.append(slot.name)
        elif outcome == AddSlotResult.DimMismatchError:
            existing_dim = _existing_slot_dim(
                collection, slot.name, weaviate_url=weaviate_url,
            )
            report.errors.append({
                "slot": slot.name,
                "reason": (
                    f"dim mismatch: target={slot.dim} "
                    f"existing={existing_dim}; refusing to overwrite "
                    f"data with different-dim vectors"
                ),
            })

    return report


# ---------------------------------------------------------------------------
# Helpers (internal)
# ---------------------------------------------------------------------------


def _decode_existing_slots(vector_config: dict) -> list[NamedVectorSlot]:
    """Read a Weaviate `vectorConfig` dict and produce a slot list.

    Weaviate's schema doesn't carry dim, so the returned slots get
    `dim=0` (we don't probe stored vectors here — that's a heavier path
    used only by `add_named_vector_slot`'s dim-mismatch check). The
    `dim` is purely informational on these reconstructed slots; what
    matters for the rebuild is the slot NAMES and their vectorizer
    config, which we copy from the existing schema.
    """
    slots: list[NamedVectorSlot] = []
    for name, cfg in vector_config.items():
        vectorizer = "none"
        # vectorizer shape: {"none": {}} OR {"text2vec-openai": {...}} etc.
        if isinstance(cfg, dict) and "vectorizer" in cfg:
            vc = cfg["vectorizer"]
            if isinstance(vc, dict) and vc:
                vectorizer = next(iter(vc.keys()))
        slots.append(NamedVectorSlot(name=name, dim=0, vectorizer=vectorizer))
    return slots


def _default_collection_rebuilder(
    collection: str,
    target_slots: list[NamedVectorSlot],
    *,
    weaviate_url: Optional[str] = None,
) -> int:
    """Default implementation of the `collection_rebuilder` callable.

    Recreates `collection` with `target_slots` as the new `vectorConfig`,
    preserving all existing data (properties + named vectors + UUIDs)
    via a staging double-copy. Delegates to
    `vco_lib.project_init._copy_collection_with_vectors` for the data
    round-trip and `_create_class` / `_delete_class` for the schema
    surgery.

    Returns:
        Total objects copied (final staging -> name round-trip count).

    Implementation notes:
      * Uses the SAME staging pattern as `project_init.migrate_collections`
        (`<name>__staging` class with target schema, then 5-step
        double-copy). This means crash recovery via
        `_recover_or_drop_orphan_staging` works against rebuilds done
        through this helper too.
      * Properties are read from the existing schema and re-applied to
        both staging and the recreated `<name>`. Targets that need
        different properties (e.g. adding a new field) must use
        `project_init.migrate_collections` directly, which handles the
        property delta path.
      * Inverted-index config is preserved verbatim from the source
        (`indexNullState` etc.) so the KG MCP's stale-filter keeps
        working post-rebuild.
    """
    # Late import so this module can be imported without dragging in
    # urllib + weaviate-client. Also breaks the would-be import cycle
    # if project_init ever needs to import weaviate_schema.
    from vco_lib import project_init as _pi

    base_url = weaviate_url or _weaviate_url_default()

    # 0. Read the existing schema; we need its properties + inverted
    #    index config to recreate the collection faithfully.
    schema = _fetch_schema(collection, weaviate_url=base_url)
    if schema is None:
        # Collection doesn't exist — create it fresh with target slots
        # and minimal property surface.
        target_def = {
            "class": collection,
            "vectorConfig": {
                s.name: s.to_weaviate_config() for s in target_slots
            },
            "invertedIndexConfig": {"indexNullState": True},
            "properties": [],
        }
        _pi._create_class(target_def, weaviate_url=base_url)
        return 0

    # 1. Build the staging class def. Vector slots = union of target
    #    slots; properties + inverted index inherited from source.
    staging_name = f"{collection}{_pi._STAGING_SUFFIX}"
    target_vc = {s.name: s.to_weaviate_config() for s in target_slots}
    target_def = {
        "class": staging_name,
        "vectorConfig": target_vc,
        "invertedIndexConfig": schema.get("invertedIndexConfig") or {
            "indexNullState": True
        },
        "properties": schema.get("properties", []),
    }

    # Defensive cleanup: if a prior crashed run left a `__staging` from
    # this very name, drop it before we recreate (safe — if any data is
    # in it we'd lose it, but that scenario is already handled by
    # `project_init._recover_or_drop_orphan_staging` upstream; here we
    # assume the caller has flushed orphans).
    existing_staging = _fetch_schema(staging_name, weaviate_url=base_url)
    if existing_staging is not None:
        _pi._delete_class(staging_name, weaviate_url=base_url)

    # 2. Create staging with the target schema.
    _pi._create_class(target_def, weaviate_url=base_url)

    # 3. Copy source -> staging (preserves vectors that already exist;
    #    new slots arrive empty).
    copied_a = _pi._copy_collection_with_vectors(
        collection, staging_name, weaviate_url=base_url,
    )

    # 4. Drop source.
    _pi._delete_class(collection, weaviate_url=base_url)

    # 5. Recreate source with the target schema (same vc as staging,
    #    inherits the same properties).
    recreated_def = dict(target_def)
    recreated_def["class"] = collection
    _pi._create_class(recreated_def, weaviate_url=base_url)

    # 6. Copy staging -> source.
    copied_b = _pi._copy_collection_with_vectors(
        staging_name, collection, weaviate_url=base_url,
    )

    if copied_a != copied_b:
        # Don't auto-drop staging on mismatch — leave it for forensic
        # inspection. Same policy as project_init.migrate_collections.
        raise RuntimeError(
            f"copy round-trip mismatch on {collection}: "
            f"source->staging={copied_a}, staging->source={copied_b}; "
            f"staging {staging_name} RETAINED for manual review"
        )

    # 7. Drop staging.
    _pi._delete_class(staging_name, weaviate_url=base_url)
    return copied_b


# ---------------------------------------------------------------------------
# Collection enumeration (for the v0.2.18-schema migrate-collections CLI)
# ---------------------------------------------------------------------------


def enumerate_kg_collections(
    *,
    project_name: Optional[str] = None,
    weaviate_url: Optional[str] = None,
) -> list[str]:
    """Return KG-shaped collections to migrate.

    Strategy:
      * If `project_name` is provided, return the canonical KG + Dev +
        Shared KG triple for that project (from
        `vco_lib.project_init.derive_project_collection_names`). The
        shared KG name is the LOCKED constant
        `VibeCodedOrchestrator_KnowledgeGraph` (since v0.2.23 B1 / brand
        casing flip; was `VibecodedOrchestrator_KnowledgeGraph` v0.2.12–
        v0.2.22 — kept as a legacy alias below).
      * If `project_name` is None, list every class on the server and
        return those that end in `_KnowledgeGraph` or `_Development`
        (per-project) PLUS the shared KG class
        (`VibeCodedOrchestrator_KnowledgeGraph` if present, plus its
        v0.2.12–v0.2.22 lowercase-c variant and the pre-v0.2.12
        `VibeCodedTools_KnowledgeGraph`). Per-project prefix discovery is
        approximate — we err on the side of including matching classes
        so an "all projects" migrate covers the dev install.

    Filters out collections that don't actually exist on the server
    (the per-project triple includes Dev which is sometimes absent).
    """
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    listed = _list_all_classes(weaviate_url=base)

    if project_name is not None:
        # Late import to avoid a circular-import gotcha if project_init
        # ever imports this module.
        from vco_lib import project_init as _pi
        derived = _pi.derive_project_collection_names(project_name)
        candidates = [
            derived["kg_collection"],
            derived["development_collection"],
            derived["shared_kg_collection"],
        ]
        return [c for c in candidates if c in listed]

    # All projects path.
    kg_suffixes = ("_KnowledgeGraph", "_Development")
    shared_kg_default = "VibeCodedOrchestrator_KnowledgeGraph"
    # v0.2.23 B1: lowercase-c variant kept as a legacy alias (was the
    # default v0.2.12–v0.2.22 — see _LEGACY_SHARED_KG_NAME_LOWERCASE_C
    # in project_init.py for the rationale).
    legacy_shared_kg_lowercase_c = "VibecodedOrchestrator_KnowledgeGraph"
    legacy_shared_kg = "VibeCodedTools_KnowledgeGraph"  # pre-v0.2.12 alias

    found: list[str] = []
    for cls in listed:
        if any(cls.endswith(suffix) for suffix in kg_suffixes):
            found.append(cls)
        elif cls in (shared_kg_default,
                     legacy_shared_kg_lowercase_c,
                     legacy_shared_kg):
            found.append(cls)
    return sorted(set(found))


def enumerate_code_collections(
    *,
    project_name: Optional[str] = None,
    weaviate_url: Optional[str] = None,
) -> list[str]:
    """Return code-graph collections to migrate.

    Strategy:
      * If `project_name` is provided, return the project-prefixed
        variants of the 5 canonical code collections (e.g.
        `MyProject_CodeFunction`). Filtered to those that actually
        exist on the server.
      * If `project_name` is None, list every class and return any that
        ends in one of the 5 code-collection suffixes.

    The shared / unprefixed `CodeFunction` etc. (legacy global code
    collections from pre-multi-project orchestrator) are included when
    `project_name` is None — they may still hold data that the
    multi-slot migration should cover.
    """
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    listed = _list_all_classes(weaviate_url=base)

    if project_name is not None:
        # Per-project prefixing: most analyzers use `<sanitized>_Code*`.
        # We derive the sanitized basename via the same helper KG uses.
        from vco_lib import project_init as _pi
        basename = _pi.sanitize_for_weaviate_class(project_name)
        candidates = [
            f"{basename}_{suffix}" for suffix in _CODE_COLLECTION_SUFFIXES
        ]
        # Some installs use the bare names (no prefix) — include those
        # only if they exist on the server.
        candidates.extend(_CODE_COLLECTION_SUFFIXES)
        return sorted({c for c in candidates if c in listed})

    return sorted([c for c in listed if is_code_collection(c)])


def _list_all_classes(*, weaviate_url: Optional[str] = None) -> list[str]:
    """GET /v1/schema -> sorted list of class names. Returns [] on
    transport failure (Weaviate down).
    """
    base = (weaviate_url or _weaviate_url_default()).rstrip("/")
    try:
        status, body = _http_request("GET", f"{base}/v1/schema", timeout=10)
        if status != 200:
            return []
        payload = json.loads(body.decode("utf-8"))
        return sorted(
            c.get("class", "") for c in payload.get("classes", [])
            if c.get("class")
        )
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public top-level migration helper (CLI-shaped)
# ---------------------------------------------------------------------------


def migrate_collections_to_v0218_schema(
    *,
    project_name: Optional[str] = None,
    weaviate_url: Optional[str] = None,
    schema_fetcher: Any = None,
    collection_rebuilder: Any = None,
) -> list[MigrationReport]:
    """Walk every KG + Code collection and apply the v0.2.18 target slots.

    This is the entry point invoked by
    `python -m vco_lib.project_init migrate-collections` (v0.2.18 leg).

    Idempotent — re-running on already-migrated collections produces a
    list of reports where every slot is `Skipped`. Safe to retry after
    interruptions.

    Args:
        project_name: When set, narrows the migration to a single
            project's collections (per-project KG/Dev + shared KG +
            per-project code-graph classes). When None, walks every
            KG-shaped and Code-shaped class on the server.
        weaviate_url: Override default Weaviate URL.
        schema_fetcher: Test injection — same as `add_named_vector_slot`.
        collection_rebuilder: Test injection — same as
            `add_named_vector_slot`.

    Returns:
        One `MigrationReport` per collection visited. The caller
        prints a human-readable summary; the CLI wrapper exits
        non-zero if any report has errors.
    """
    reports: list[MigrationReport] = []

    for coll in enumerate_kg_collections(
        project_name=project_name, weaviate_url=weaviate_url,
    ):
        reports.append(migrate_collection_to_target(
            coll, KG_NAMED_VECTORS,
            weaviate_url=weaviate_url,
            schema_fetcher=schema_fetcher,
            collection_rebuilder=collection_rebuilder,
        ))

    for coll in enumerate_code_collections(
        project_name=project_name, weaviate_url=weaviate_url,
    ):
        reports.append(migrate_collection_to_target(
            coll, CODE_NAMED_VECTORS,
            weaviate_url=weaviate_url,
            schema_fetcher=schema_fetcher,
            collection_rebuilder=collection_rebuilder,
        ))

    return reports


def format_reports_table(reports: list[MigrationReport]) -> str:
    """Human-readable summary of `migrate_collections_to_v0218_schema`.

    Produces a fixed-width text table for stdout, NOT JSON. The CLI
    wrapper picks JSON via `--json`; this function is the default
    (printable) format.
    """
    if not reports:
        return "  (no collections matched)\n"

    rows = []
    name_w = max(len(r.collection) for r in reports)
    name_w = max(name_w, len("COLLECTION"))
    header = f"  {'COLLECTION':{name_w}s}  ADDED  SKIPPED  ERRORS  OBJECTS_COPIED"
    rows.append(header)
    rows.append("  " + "-" * (len(header) - 2))
    for r in reports:
        rows.append(
            f"  {r.collection:{name_w}s}  "
            f"{len(r.added_slots):5d}  "
            f"{len(r.skipped_slots):7d}  "
            f"{len(r.errors):6d}  "
            f"{r.objects_copied:14d}"
        )
        if r.added_slots:
            rows.append(f"      + added: {', '.join(r.added_slots)}")
        if r.errors:
            for err in r.errors:
                rows.append(f"      ! {err['slot']}: {err['reason']}")
    return "\n".join(rows) + "\n"


__all__ = [
    # slot defs
    "NamedVectorSlot",
    "KG_NAMED_VECTORS",
    "CODE_NAMED_VECTORS",
    # public API
    "AddSlotResult",
    "MigrationReport",
    "add_named_vector_slot",
    "diff_collection_vs_target",
    "migrate_collection_to_target",
    "migrate_collections_to_v0218_schema",
    # helpers
    "enumerate_kg_collections",
    "enumerate_code_collections",
    "format_reports_table",
    "is_code_collection",
]
