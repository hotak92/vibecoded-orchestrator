# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The Weaviate class definitions VCO creates (KG, development, diagrams and
the five code-graph collections) — pure data builders, no I/O.

Extracted from :mod:`vco_lib.project_init` in v0.2.105 (that module is held
under a line ceiling, ``tests/test_v0292_wp4_delivery_and_promises.py``);
``project_init`` re-exports every name, so ``project_init.kg_class_definition``
and friends keep working for existing callers.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Schema definitions (relocated from install.py:4212-4270)
# ---------------------------------------------------------------------------


def named_vector_config(class_name: Optional[str] = None) -> dict:
    """KG-shaped multi-slot named-vector config (v0.2.18 catalog).

    Sources the slot catalog from `vco_lib.weaviate_schema.KG_NAMED_VECTORS`
    — see that module for the LOCKED slot list + rationale. v0.2.18 grew it
    from the hard-coded 3 slots (`qwen3_embed`, `ollama_embed`, `openai_embed`,
    RETAINED so upgrades keep their data) to 5 (+ `arctic2_embed`,
    `openai_text_embed`, extra embedding-provider targets).

    Each slot has `vectorizer: none` so we feed pre-computed embeddings
    from the MCP server. Index type stays HNSW (Weaviate default for ANN).
    v0.2.105: with ``class_name`` each slot also carries that collection's
    vector-index policy (the cache cap, :mod:`vco_lib.weaviate_vector_index`).

    The MCP server's `sync_knowledge_graph.py` writes objects with at
    least one named vector populated; the others are filled lazily as the
    user pulls more embedding backends. Without this multi-vector config
    seeding fails with HTTP 422 ("collection configured without multiple
    named vectors, but received named vectors").
    """
    # Late import to avoid a circular dep — weaviate_schema imports
    # project_init lazily inside `_default_collection_rebuilder`.
    from vco_lib.weaviate_schema import KG_NAMED_VECTORS
    from vco_lib.weaviate_vector_index import create_index_config
    index_config = create_index_config(class_name) if class_name else None
    return {s.name: s.to_weaviate_config(index_config=index_config) for s in KG_NAMED_VECTORS}


def code_class_definitions(
    project_prefix: str = "",
    *,
    index_type: str = "hnsw",
) -> dict[str, dict]:
    """Return Weaviate class definitions for the 5 code-graph collections.

    Args:
        project_prefix: Optional prefix added before each canonical name
            (e.g. `project_prefix="MyProj_"` -> `"MyProj_CodeFunction"`).
            Empty string returns bare-name definitions; callers using
            per-project prefixing prepend it.
        index_type: v0.2.73 FIX-D4 — ``"hnsw"`` (default; the ONLY shipped
            default) or ``"hfresh"``. When ``"hfresh"``, each slot's
            ``vectorConfig`` is emitted with ``vectorIndexType:hfresh`` (which
            forces mandatory RQ compression — see
            ``NamedVectorSlot.to_weaviate_config``). GATED: default stays hnsw
            until the integrator's 1.37 scratch-test passes (HFresh is preview
            + forces RQ). A ``hfresh`` collection is only ever born via the
            `copy` migrate action (create-fresh-with-target-schema), never a
            mutation of a live collection (vectorIndexType is immutable live).

    Used by analyzers + the v0.2.18 schema migration. The actual
    code-graph create site is `templates/scripts/analyze_code_graph.py`
    which has its own per-collection property list (richer than the KG
    schema). This function provides the MINIMAL VECTOR-CONFIG-ONLY shape
    that the v0.2.18 migrate-collections helper uses for new-class
    creation (when a code collection doesn't yet exist and the helper
    needs to bootstrap it with the multi-slot config). Property-rich
    creation still flows through analyze_code_graph.py's
    `create_collections` so the bare definitions returned here only
    carry the vector slots + the inverted-index invariant.

    v0.2.18 (Plan C): the minimal property surface now declares the
    `language` property (canonical lowercase ID per analyzed file). This
    enables the smart-dispatch's patch_props action to add the missing
    property on existing v0.2.17 code collections when the migrate path
    routes them through. analyze_code_graph.py's `_ensure_language_property`
    is the primary path; the patch_props branch is a defense-in-depth
    redundancy for callers that exercise migrate-collections directly
    without re-running analyze.

    The returned dict is keyed by basename (no prefix); composition with
    `project_prefix` is the caller's responsibility for canonical naming.
    """
    from vco_lib.weaviate_schema import CODE_NAMED_VECTORS, _CODE_COLLECTION_SUFFIXES

    # v0.2.73 FIX-D4: thread the target index type into every slot ("hfresh" =
    # preview, mandatory RQ; only the copy migrate action may change it). The
    # v0.2.105 cache cap is in vectorIndexConfig, which `_schema_delta` ignores.
    from vco_lib.weaviate_vector_index import create_index_config
    vc = {  # every basename is code-shaped, so one policy lookup serves all five
        slot.name: slot.to_weaviate_config(index_type=index_type,
                                           index_config=create_index_config("CodeFunction"))
        for slot in CODE_NAMED_VECTORS
    }

    # v0.2.18 (Plan C) — minimal property declaration. Same shape used by
    # analyze_code_graph.py's `create_collections` so the smart-dispatch
    # patch_props action recognises it as "already there" on freshly-
    # created collections.
    _language_prop = {
        "name": "language",
        "dataType": ["text"],
        "description": (
            "Canonical-lowercase language ID (python, javascript, ...) — "
            "Plan C scoped prune"
        ),
    }

    return {
        basename: {
            "class": f"{project_prefix}{basename}",
            "description": f"Code-graph {basename} collection (v0.2.18 multi-slot)",
            "vectorConfig": vc,
            "invertedIndexConfig": {"indexNullState": True},
            # Plan C: `language` is the only minimal property declared
            # here; analyze_code_graph.py owns the full property surface
            # for each collection. Both classes (Module/Class/Function/
            # API/Interaction) carry the same `language` prop — for
            # CodeInteraction it represents the SOURCE-SIDE language
            # (caller's), not the target's.
            "properties": [_language_prop],
        }
        for basename in sorted(_CODE_COLLECTION_SUFFIXES)
    }


def kg_class_definition(name: str) -> dict:
    """Weaviate class definition for a per-project KG collection.

    Sets `invertedIndexConfig.indexNullState=True` so the drift-detector
    in `detect_kg_schema_drift` sees a conformant schema on fresh
    installs. The drift detector requires it; previously the schema
    definition did not set it (silent drift on every fresh install).
    Adding it here closes that loop — see "Surprises" in the PR 2
    commit message.

    V52-I Fix B (2026-06-09): the 4 canonical temporal date props
    (`created_at`, `updated_at`, `valid_from`, `valid_until`) are now
    included at create-time so fresh KG collections — including the
    SHARED `VibeCodedOrchestrator_KnowledgeGraph` — pass the MCP's
    universal stale filter (``valid_until is_none(True) | valid_until > now``)
    without emitting `partial_fan_out_schema_missing` false-positives.

    Pre-V52-I gap matrix (audit 2026-06-09):
      - per-project KG: shipped without these props; sync_knowledge_graph
        additive-migrate added them lazily on first sync.
      - shared KG: shipped without these props AND nothing migrated it
        → universal stale filter emitted schema errors → 30 false-positive
        partial_fan_out events in our corpus.
    Including them here closes the gap on every fresh install; existing
    installs are reconciled by `migrate-development-temporal-props.sh`
    (regex extended to cover `_KnowledgeGraph` suffix — V52-I Fix B).
    """
    return {
        "class": name,
        "description": "VibeCoded Tools knowledge graph collection",
        "vectorConfig": named_vector_config(name),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
            {"name": "node_type", "dataType": ["text"]},
            {"name": "tags", "dataType": ["text[]"]},
            {"name": "links", "dataType": ["text[]"]},
            # WikiLink edges as nested objects: [[relationType::Target]]
            # → {relation_type: "uses", target_title: "Target"}.
            {
                "name": "typed_links",
                "dataType": ["object[]"],
                "nestedProperties": [
                    {"name": "relation_type", "dataType": ["text"]},
                    {"name": "target_title", "dataType": ["text"]},
                ],
            },
            {"name": "status", "dataType": ["text"]},
            # v0.2.17 (Reviewer B v0.2.18 nit, folded into v0.2.17):
            # SHA-256 over (frontmatter-minus-updated + body), used by
            # `templates/scripts/sync_knowledge_graph.py`'s embed-skip
            # fast path. Including it at create-time means fresh
            # per-project KGs ship with the property from day 0 — no
            # warm-up sync that re-embeds everything just to populate
            # this field. The orchestrator-self template also has it
            # via the `temporal_props` migration loop for upgrades; this
            # entry covers create-from-scratch on per-project init.
            {"name": "content_hash", "dataType": ["text"]},
            # V52-I Fix B (2026-06-09): the 4 canonical temporal date
            # props the MCP's universal `_stale_filter` and `days=`
            # recency filter expect on every KG-shaped collection.
            # `valid_until` is the load-bearing one (driver of the bug);
            # the other three are added for completeness so future code
            # can rely on the full temporal-metadata quartet.
            {"name": "created_at", "dataType": ["date"]},
            {"name": "updated_at", "dataType": ["date"]},
            {"name": "valid_from", "dataType": ["date"]},
            {"name": "valid_until", "dataType": ["date"]},
        ],
    }


def development_class_definition(name: str) -> dict:
    """Weaviate class definition for a per-project Development collection.

    Same `indexNullState=True` invariant as the KG schema (see
    `kg_class_definition`).

    Temporal properties (`created`, `updated`, `valid_from`, `valid_until`)
    mirror the KG schema. The MCP server's `hybrid_search` applies a
    stale-data filter (``valid_until is_none(True) | valid_until > now``)
    on every search target; without these properties Development queries
    fail at the GraphQL layer with "no such prop with name 'valid_until'
    found in class '<X>_Development'". Date dataType matches the KG
    definition (see ``kg_class_definition`` properties).

    v0.2.18 (2026-05-19): adds two properties for KG parity:

    * ``content_hash`` (text) — SHA-256 over the source file body, used by
      ``templates/scripts/sync_knowledge_graph.py::sync_doc``'s embed-skip
      fast-path. Mirrors the v0.2.17 KG addition (see
      `kg_class_definition`) so re-syncing an unchanged docs/ tree drops
      from "re-embed every file" to "compare hashes; skip everything in
      milliseconds." Existing v0.2.17 Dev collections gain the property
      via `migrate_collections`' additive patch_props action — additive,
      non-destructive. The first re-sync after the upgrade backfills
      values; subsequent re-syncs hit the fast path.

    * ``status`` (text) — lets docs that get superseded by newer versions
      be marked `archived` so `hybrid_search`'s stale-filter skips them.
      Same shape as the KG `status` field.

    EXPLICITLY NOT MIRRORED from the KG schema (user direction 2026-05-19):

    * ``tags`` / ``links`` / ``typed_links`` — KG-only graph metadata;
      Dev rows have no WikiLink resolution and no typed relationships.
      The corresponding fields in
      ``parse_doc_file`` synthesize empty values for symmetry, but they
      are never written into the Dev collection.
    * ``node_type`` — redundant: every row in a Development collection is
      unambiguously a "doc" by virtue of the collection name itself
      (``<Project>_Development``). No need for a per-row constant column.
    """
    return {
        "class": name,
        "description": "VibeCoded Tools project documentation collection",
        "vectorConfig": named_vector_config(name),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
            # Temporal metadata — mirrors the KG schema so MCP filters
            # (`valid_until is_none(True) | valid_until > now`) work
            # against Development collections too. Added 2026-05-16
            # (PR-24).
            {"name": "created", "dataType": ["date"]},
            {"name": "updated", "dataType": ["date"]},
            {"name": "valid_from", "dataType": ["date"]},
            {"name": "valid_until", "dataType": ["date"]},
            # v0.2.18 (2026-05-19): KG parity for archived-doc filtering
            # (`status`) and the embed-skip fast-path (`content_hash`).
            # Migration into existing v0.2.17 Dev collections is handled
            # by `migrate_collections`' additive patch_props action —
            # the diff between `_fetch_schema()` and this target picks up
            # any missing prop automatically (see `_schema_delta`).
            {"name": "status", "dataType": ["text"]},
            {"name": "content_hash", "dataType": ["text"]},
        ],
    }


def diagrams_class_definition(name: str) -> dict:
    """Weaviate class definition for a per-project Diagrams collection.

    Phase 1.5 of the Diagrams Integration (2026-05-24). Mirrors the
    `indexNullState=True` invariant of the KG / Dev schemas and uses
    the same multi-slot named-vector config so KG-style search ranking
    works without translation.

    Property surface (matches `vco_lib/diagram_indexer.py::_weaviate_upsert`):

    * ``title``           — `inferred_title` (mermaid frontmatter title,
                            excalidraw scene name, or humanised filename)
    * ``content``         — Mermaid source OR concatenated Excalidraw
                            text labels (the embedding target)
    * ``path_tags``       — split of `category_path` ("gui/auth" →
                            ["gui","auth"]) — primary tag axis
    * ``diagram_kind``    — "flowchart" / "classDiagram" / ... / "excalidraw"
    * ``chat_id``         — Claude Code session UUID when the wrapper-MCP
                            saved the diagram; nullable when user saved
                            outside a Claude session
    * ``linked_session_summary`` — first 200 chars of the chat's
                            summary file (best-effort)
    * ``file_path``       — absolute path on disk (used for dedup +
                            click-through from search results)
    * ``created_at`` / ``updated_at`` — unix epoch ints (NOT date strings
                            — the indexer writes Python ints from
                            `int(time.time())` to keep the SQLite +
                            Weaviate shapes identical)

    Explicitly NOT included (rationale documented inline):

    * ``status`` — diagrams have no archival workflow yet; if a Phase-3
      need emerges, add via the additive patch_props migration path.
    * ``content_hash`` — content_text already serves as the dedup key
      for now; adding hash later is an additive migration.
    * ``tags`` / ``links`` / ``typed_links`` — diagrams use `path_tags`
      as their sole tag axis (the path IS the tag). No WikiLink graph.

    V52-I Fix B (2026-06-09): adds `valid_from` and `valid_until` as
    date-typed props so the MCP's universal `_stale_filter` doesn't
    schema-error on diagram collections. The indexer doesn't yet write
    these fields (no per-diagram archival workflow); they stay None on
    new rows, and the stale filter's `is_none(True) | > now` matcher
    treats None as "active by default" — exactly what we want for
    diagrams. The pre-existing INT `created_at` / `updated_at` columns
    are NOT renamed or retyped — the indexer
    (`vco_lib/diagram_indexer.py::_weaviate_upsert`) still writes them
    as `int(time.time())` and downstream search code reads them as ints.
    `valid_from` / `valid_until` are additive only.
    """
    return {
        "class": name,
        "description": (
            "VibeCoded Tools per-project diagrams collection "
            "(Mermaid + Excalidraw)"
        ),
        "vectorConfig": named_vector_config(name),
        "invertedIndexConfig": {"indexNullState": True},
        "properties": [
            {"name": "title", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
            {"name": "path_tags", "dataType": ["text[]"]},
            {"name": "diagram_kind", "dataType": ["text"]},
            {"name": "chat_id", "dataType": ["text"]},
            {"name": "linked_session_summary", "dataType": ["text"]},
            {"name": "file_path", "dataType": ["text"]},
            {"name": "created_at", "dataType": ["int"]},
            {"name": "updated_at", "dataType": ["int"]},
            # V52-I Fix B (2026-06-09): date-typed validity window so the
            # MCP's universal stale filter
            # (`valid_until is_none(True) | valid_until > now`) doesn't
            # schema-error against diagram collections. Defaults to None
            # on every row the indexer writes — equivalent to "active by
            # default" under the stale filter's matcher.
            {"name": "valid_from", "dataType": ["date"]},
            {"name": "valid_until", "dataType": ["date"]},
        ],
    }


# Internal aliases preserving install.py's underscored names.
_named_vector_config = named_vector_config
_kg_class_definition = kg_class_definition
_development_class_definition = development_class_definition
_diagrams_class_definition = diagrams_class_definition
