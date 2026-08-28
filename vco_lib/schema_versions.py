# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Canonical schema-version constants for every DERIVED artifact.

This module is the **single source of truth** for "what version of each
schema/content-shape the orchestrator currently knows how to produce". The
install + update flows compare against these constants and drop+recreate (for
derived state) or upgrade-in-place (for user-curated state) on mismatch.

See ``v0.2.52`` backlog ``§ V52-AG`` for full design rationale + migration
strategy. See ``launcher/src-tauri/vct-launcher-core/src/db/migrations/
033_artifact_schema_versions.sql`` for the registry-table shape.

Discipline rule (user-locked 2026-06-09): "from now on consistent".

- Every PR that changes the shape of a Weaviate class, a JSON column, a
  controlled-vocabulary column, or the bundle-materialization contract MUST
  bump the corresponding constant here AND make the install/update path
  trigger recreate/upgrade on mismatch.
- Future releases NEVER carry back-compat layers ("if v1 then patch, else
  fresh"). Bump the constant, recreate cleanly.
- For USER-CURATED state (KG node frontmatter, modified hooks/agents/skills,
  ``module_settings`` user toggles, secrets) — upgrade in place via a
  forward-only migration helper. Drop+recreate is NOT acceptable for these.

A parity test (``tests/test_schema_versions_parity.py``) asserts that the
Rust constants in ``schema_versions_rust.json`` (generated from this module)
match Python. CI fails if they drift.
"""

from __future__ import annotations

# ===========================================================================
# Layer 1 — Weaviate collection schemas (DERIVED — drop+recreate on bump)
# ===========================================================================

#: Per-project KG class (e.g. ``VCODev_KnowledgeGraph``).
#: v3: V52-I (2026-06-09) adds 4 temporal date props (``created_at``,
#: ``updated_at``, ``valid_from``, ``valid_until``) at create-time so the
#: MCP's universal stale filter doesn't emit ``partial_fan_out_schema_missing``
#: false-positives. Pre-V52-I shared KG + per-project Diagrams lacked these.
KG_COLLECTION_SCHEMA_VERSION = 3

#: Shared KG class (``VibeCodedOrchestrator_KnowledgeGraph``). Same v3 reason
#: as per-project — V52-I closes the schema gap.
SHARED_KG_COLLECTION_SCHEMA_VERSION = 3

#: Per-project Diagrams class (e.g. ``VCODev_Diagrams``).
#: v2: V52-I adds ``valid_from`` + ``valid_until`` (date-typed). The pre-V52-I
#: shape kept INT ``created_at``/``updated_at`` (preserved here, indexer writes
#: them as ``int(time.time())``).
DIAGRAMS_COLLECTION_SCHEMA_VERSION = 2

#: Per-project Development class (e.g. ``VCODev_Development``).
#: v2: V52-I parity bump — Development class already had the 4 temporal props
#: per the earlier ``migrate-development-temporal-props.sh`` work; v0.2.52
#: just makes the version explicit so the registry is consistent.
DEVELOPMENT_COLLECTION_SCHEMA_VERSION = 2

#: All 5 code-graph classes (CodeFunction / CodeClass / CodeModule / CodeAPI /
#: CodeInteraction).
#: v4: V52-O.3 (UUID5 derivation now mixes ``project_source`` so dual-walked
#: roots produce distinct UUIDs — eliminates the 49.2% cross-root collision
#: rate observed on 2026-06-09) + V52-O.4 (``file_path`` property added to
#: CodeFunction + CodeClass — pre-v0.2.52 only CodeModule had it).
#: v5: v0.2.72 P3/P7 — model-aware chunking of over-budget entities adds
#: ``chunk_num`` + ``total_chunks`` (INT) to CodeFunction/CodeClass, and a
#: ``embed_revision`` (INT) generation marker to all 5 classes. The shape
#: change is ADDITIVE + data-preserving: the migration
#: ``migrations/codegraph_collection/4_to_5.py`` edge ``add_property``s the new
#: props (NO drop, NO re-embed). The actual per-row re-embed for the ~7-9% of
#: functions/classes that now CHUNK is handled INCREMENTALLY + resumably by the
#: analyzer's per-object ``CODEGRAPH_EMBED_REVISION`` gate (see
#: ``analyze_code_graph.py`` + ``vco_lib/codegraph_resync.py``), NOT by a
#: framework drop+recreate — the framework has no selective-per-row re-embed
#: verb and a full CodeSage re-embed would be needlessly expensive for a 9%
#: tail. So: framework owns the SCHEMA shape (this bump + the preserving edge);
#: the analyzer's revision gate owns the DATA re-shape.
#: v6: v0.2.73 M1/M4 — ``is_test`` (BOOL) on CodeFunction/CodeClass/CodeModule
#: + ``n_callers`` (INT) on CodeFunction. ADDITIVE + data-preserving:
#: ``migrations/codegraph_collection/5_to_6.py`` add_property's them (NO drop,
#: NO re-embed — these are render/rank metadata, not embedding inputs).
#: Backfill of existing rows is data-side
#: (``codegraph_resync.backfill_codegraph_metadata`` for is_test/doc;
#: ``create_cross_references`` self-heals n_callers), NOT framework-side.
#: v7: v0.2.73 READ-amp origin cleanup — ONE-TIME targeted purge of
#: ``.claude/state/**`` transient-scratch rows (tool_backups snapshots) that the
#: pre-fix walk wrongly indexed (live-measured 43% of a real CodeFunction
#: collection → multi-TB mmap READ storm). ``migrations/codegraph_collection/
#: 6_to_7.py`` iterates + matches ``.claude/state/`` as an EXACT PYTHON
#: SUBSTRING and ``delete_by_id``s ONLY those rows (NEVER a Weaviate ``Like`` on
#: the word-tokenized ``file_path`` — that would delete real functions) — NO
#: drop, NO re-embed, NO re-summarize (survivors' vectors + summaries
#: untouched). The code fix
#: (``analyze_code_graph._path_is_excluded`` + per-root ``.claude`` gate) stops
#: NEW rows; this edge removes the already-indexed garbage on update,
#: algorithmically. Idempotent (re-run purges zero); disk reclaim is the
#: Weaviate-side compaction that follows the deletes (2 GiB segment cap).
CODEGRAPH_COLLECTION_SCHEMA_VERSION = 7

# ===========================================================================
# Layer 2 — KG node content schema (USER-CURATED — upgrade in place on bump)
# ===========================================================================

#: YAML frontmatter contract for ``knowledge/**/*.md`` nodes.
#: v1: V52-AG introduces the field. Pre-v0.2.52 nodes have no
#: ``schema_version`` line; the upgrade helper writes ``schema_version: 1``
#: idempotently on first pass.
KG_NODE_FRONTMATTER_SCHEMA_VERSION = 1

#: ``knowledge/.node_formats.json`` per-entry shape (the KG-summary cache
#: written by ``generate-kg-summary.py``). v1 entry shape:
#: ``{title, description, summary, generated_at, content_hash, backend}``.
#: DERIVED — fully regeneratable from the project's KG nodes, so a schema
#: bump triggers a clean regeneration (re-run the summary generator), not a
#: forward-migration. v0.2.57: registered so the bundle-update flow keeps
#: the per-project cache silently (action ``keep-regenerated``) and only
#: re-generates when THIS version bumps — handled via the
#: ``artifact_schema_versions`` DB registry, NOT a marker inside the JSON.
KG_NODE_FORMATS_SCHEMA_VERSION = 1

#: ``<project>/.claude/.code_formats.json`` per-entry shape (the code-summary
#: cache written by ``generate-code-summary.py``, M2 v0.2.73). v1 entry shape:
#: ``{full_name, file_path, collection, one_liner, summary, generated_at,
#: content_hash, backend}`` keyed by ``f"{file_path}::{full_name}"`` (+
#: ``total_chunks`` / ``chunk_summaries`` only when ``total_chunks > 1``).
#: DERIVED — fully regeneratable from the project's code graph, so a schema
#: bump triggers a clean regeneration (re-run the code-summary generator),
#: not a forward-migration (action ``keep-regenerated``, same discipline as
#: ``kg_node_formats``).
CODE_FORMATS_SCHEMA_VERSION = 1

# ===========================================================================
# Layer 3 — Bundle materialization (DERIVED — manifest-driven)
# ===========================================================================

#: Bundle ``<project>/.claude/.vco-manifest.json`` schema version. Already
#: tracked pre-V52-AG via ``vco_lib/project_init.py::_MANIFEST_SCHEMA_VERSION``;
#: this constant FORMALIZES it so the registry can include it. The two
#: constants must agree — a parity test enforces this.
BUNDLE_MATERIALIZATION_SCHEMA_VERSION = 2

# ===========================================================================
# Layer 4 — launcher.db row-content schemas (mixed: derived + user-curated)
# ===========================================================================

#: ``project_kg_bindings.config_json`` JSON shape.
PROJECT_KG_BINDINGS_SHAPE_VERSION = 1

#: ``project_codegraph_bindings.config_json`` JSON shape.
PROJECT_CODEGRAPH_BINDINGS_SHAPE_VERSION = 1

#: ``module_installs.kg_collections`` JSON shape (added by migration 032,
#: TEXT/JSON column).
MODULE_INSTALLS_KG_COLLECTIONS_SHAPE_VERSION = 1

#: ``module_settings.setting_value`` JSON-blob shape. User-curated — values
#: are user-set toggles + setting payloads. Upgrade in place; never drop.
MODULE_SETTINGS_VALUE_SHAPE_VERSION = 1

#: ``codegraph_access.access_level`` controlled vocabulary.
#: v1 set: ``'none'`` | ``'read'`` | ``'write'``. Adding a new tier (e.g.
#: ``'admin'``) requires a bump + a vocabulary-validation pass.
CODEGRAPH_ACCESS_VOCABULARY_VERSION = 1

#: ``kg_collection_access.access_level`` controlled vocabulary. Same v1 set
#: as codegraph_access.
KG_COLLECTION_ACCESS_VOCABULARY_VERSION = 1

#: ``code_graph_builds.status`` controlled vocabulary.
#: v1 set: ``'pending'`` | ``'running'`` | ``'success'`` | ``'failed'``.
CODE_GRAPH_BUILDS_STATUS_VOCABULARY_VERSION = 1

#: Project bootstrap version — represents "this project was bootstrapped
#: against orchestrator vX". When a release adds a new required sibling row
#: to an existing project (e.g. v0.2.49 adds a kg_collection_access row for
#: a newly-shipped global module), bump this + the install/update flow
#: checks the project has the v0.2.52-required sibling rows; if not, creates
#: them with sensible defaults. Closes V52-AF's per-project-update parity
#: gaps by construction.
PROJECT_BOOTSTRAP_VERSION = 1

# ===========================================================================
# Layer 5 — Orchestrator-wide artifacts (NULL project_id in registry)
# ===========================================================================

#: ``rl_events.payload_json`` v3 contract. Already at v3 pre-V52-AG (per the
#: existing ``"schema_version": 3`` field in retrieval-event payloads); this
#: constant formalizes it.
RL_EVENTS_PAYLOAD_SHAPE_VERSION = 3

#: Highest applied launcher.db schema migration. Matches the ``version`` field
#: of the last entry in ``launcher/src-tauri/vct-launcher-core/src/db/
#: migrations.rs::MIGRATIONS``. Used by the version-check helper to confirm
#: the DB schema is at the level this code expects (refuse to start if
#: launcher.db is somehow ahead — user downgraded orchestrator while running
#: on newer DB).
#: 42 = migration 042_project_hooks_disabled_entry.sql (v0.2.91, decision #27
#: — nullable project_hooks.disabled_entry_json). The Hooks tab was a full
#: placebo (its rows were read by nothing while Claude Code reads
#: .claude/settings.json directly); disable/enable now edit that file through
#: `python -m vco_lib.hooks_settings`, so a disabled hook's removed entry is
#: PARKED in this column to make re-enable exact. Bumped ATOMICALLY with mig
#: 042's Rust registration.
#: 41 = migration 041_job_heartbeats.sql (BUG 2, v0.2.89 — nullable
#: heartbeat_at liveness columns on kg_syncs + code_graph_builds so a task
#: death with the launcher still up is detected by the 5-min sweeper /
#: read-time guards instead of leaving RUNNING rows forever). Bumped
#: ATOMICALLY with mig 041's Rust registration.
#: 40 = migration 040_shared_kg_gate_module_id_canonicalize.sql (2026-07-14
#: split-brain fix — relocates the shared-KG gate flags
#: shared_kg_read_disabled / shared_kg_write_disabled / shared_kg_opt_out
#: from module_settings.module_id='__project__' (the launcher GUI writer) to
#: the canonical 'orchestrator-core' (what the hub /config resolver + this
#: Python env projection already read). Newer value wins on conflict; legacy
#: rows deleted. Bumped ATOMICALLY with mig 040's Rust registration.
#: 39 = migration 039_rl_events_quarantine.sql (RL-14, v0.2.75 — nullable
#: rl_events.quarantined_at/quarantine_reason marker columns + partial index;
#: poisoned rows are marked, not deleted, and excluded from training reads).
#: Bumped ATOMICALLY with mig 039's Rust registration (a Python-ahead bump
#: would stamp a phantom schema version).
#: 38 = migration 038_code_graph_build_partial_status.sql (extend
#: code_graph_builds.status CHECK with 'partial', C-11/RT-3 — a build whose
#: inserts succeeded but stale-row prune failed, PRUNE_FAILURES=N>0).
#: 37 = migration 037_code_graph_build_pid.sql (code_graph_builds.pid, R-4 —
#: registers the detached install-spawned resync walk so the GUI shows it and
#: the boot sweep can death-detect it).
LAUNCHER_DB_TABLE_SET_VERSION = 42


# ===========================================================================
# Aggregation — exported registry for the Rust parity check
# ===========================================================================

#: Canonical version constants by artifact_type → expected schema_version.
#: Used by ``tests/test_schema_versions_parity.py`` to assert Rust matches
#: Python, and by ``vco_lib/project_init.py`` install/update flows to drive
#: the recreate/upgrade decision.
CANONICAL_VERSIONS: dict[str, int] = {
    # Layer 1 — Weaviate collections (DERIVED)
    "kg_collection":              KG_COLLECTION_SCHEMA_VERSION,
    "shared_kg_collection":       SHARED_KG_COLLECTION_SCHEMA_VERSION,
    "diagrams_collection":        DIAGRAMS_COLLECTION_SCHEMA_VERSION,
    "development_collection":     DEVELOPMENT_COLLECTION_SCHEMA_VERSION,
    "codegraph_collection":       CODEGRAPH_COLLECTION_SCHEMA_VERSION,
    # Layer 2 — KG content
    "kg_node_frontmatter":        KG_NODE_FRONTMATTER_SCHEMA_VERSION,   # user-curated
    "kg_node_formats":            KG_NODE_FORMATS_SCHEMA_VERSION,        # derived (regen cache)
    "code_formats":               CODE_FORMATS_SCHEMA_VERSION,           # derived (regen cache)
    # Layer 3 — Bundle (DERIVED)
    "bundle_materialization":     BUNDLE_MATERIALIZATION_SCHEMA_VERSION,
    # Layer 4 — launcher.db row content
    "project_kg_bindings_shape":  PROJECT_KG_BINDINGS_SHAPE_VERSION,
    "project_codegraph_bindings_shape": PROJECT_CODEGRAPH_BINDINGS_SHAPE_VERSION,
    "module_installs_shape":      MODULE_INSTALLS_KG_COLLECTIONS_SHAPE_VERSION,
    "module_settings_shape":      MODULE_SETTINGS_VALUE_SHAPE_VERSION,
    "codegraph_access_vocabulary": CODEGRAPH_ACCESS_VOCABULARY_VERSION,
    "kg_collection_access_vocabulary": KG_COLLECTION_ACCESS_VOCABULARY_VERSION,
    "code_graph_builds_status_vocabulary": CODE_GRAPH_BUILDS_STATUS_VOCABULARY_VERSION,
    "project_bootstrap_version":  PROJECT_BOOTSTRAP_VERSION,
    # Layer 5 — Orchestrator-wide (NULL project_id)
    "rl_events_payload_shape":    RL_EVENTS_PAYLOAD_SHAPE_VERSION,
    "launcher_db_table_set":      LAUNCHER_DB_TABLE_SET_VERSION,
}

#: State classification per artifact_type. Drives the recreate vs upgrade-in-
#: place decision when a version mismatch is detected.
#:
#: - ``"derived"``: drop+recreate cleanly. Schema mismatch triggers a full
#:   regeneration from the canonical source-of-truth (filesystem walk for
#:   code-graph, module manifests for module-installs JSON, etc.).
#: - ``"user_curated"``: upgrade in place via a forward-only migration helper.
#:   Drop+recreate is NEVER acceptable — would lose user-state.
ARTIFACT_STATE_CLASSIFICATION: dict[str, str] = {
    # Layer 1 — Weaviate (derived from filesystem source + manifests)
    "kg_collection":              "derived",
    "shared_kg_collection":       "derived",
    "diagrams_collection":        "derived",
    "development_collection":     "derived",
    "codegraph_collection":       "derived",
    # Layer 2 — KG content
    "kg_node_frontmatter":        "user_curated",
    "kg_node_formats":            "derived",          # regen cache (generate-kg-summary)
    "code_formats":               "derived",          # regen cache (generate-code-summary)
    # Layer 3 — Bundle (derived from templates/)
    "bundle_materialization":     "derived",
    # Layer 4 — launcher.db rows
    "project_kg_bindings_shape":  "derived",          # manifest-derived
    "project_codegraph_bindings_shape": "derived",
    "module_installs_shape":      "derived",          # vct-module.json-derived
    "module_settings_shape":      "user_curated",     # user-set toggles
    "codegraph_access_vocabulary": "user_curated",    # user grants
    "kg_collection_access_vocabulary": "user_curated",
    "code_graph_builds_status_vocabulary": "derived",
    "project_bootstrap_version":  "derived",
    # Layer 5 — Orchestrator-wide
    "rl_events_payload_shape":    "user_curated",     # historical telemetry data
    "launcher_db_table_set":      "derived",          # schema_migrations-owned
}


def canonical_version(artifact_type: str) -> int:
    """Return the current canonical version for ``artifact_type``.

    Raises ``KeyError`` if the artifact_type is not registered — defensively
    surfacing the caller bug at the version-check call site rather than
    silently returning 0 or skipping.
    """
    return CANONICAL_VERSIONS[artifact_type]


def is_derived(artifact_type: str) -> bool:
    """Return True iff this artifact's mismatch handler is drop+recreate.

    Returns False for user_curated artifacts (which must upgrade in place).
    Raises ``KeyError`` for unknown artifact_type — see ``canonical_version``.
    """
    classification = ARTIFACT_STATE_CLASSIFICATION[artifact_type]
    return classification == "derived"


def all_artifact_types() -> tuple[str, ...]:
    """Return the sorted tuple of every registered artifact_type."""
    return tuple(sorted(CANONICAL_VERSIONS))
