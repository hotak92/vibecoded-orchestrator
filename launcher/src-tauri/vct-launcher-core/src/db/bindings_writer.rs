// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Single-writer home for the launcher.db binding tables (X-1 / v0.2.76).
//!
//! Single-writer contract
//! ----------------------
//! The Rust launcher is the authoritative CREATOR of `project_kg_bindings` /
//! `project_codegraph_bindings` rows (the Python side only ever HEALS them,
//! via `vco_lib.kg_binding_heal` — see that module's matching contract
//! header). On the Rust side the base upsert SQL lives in
//! [`crate::db::project_state`]'s `Db::set_project_kg_binding` /
//! `Db::set_project_codegraph_binding` methods, and the drift-repair SQL lives
//! in [`crate::db::access`]. This module is the ONE place that owns the
//! **derive-a-name-then-write** orchestration: callers that need to seed a
//! project's default bindings hand a project NAME here, and the derivation
//! (via [`crate::db::access::sanitize_kg_collection_local`], the core-crate
//! copy of the shared Python-parity sanitizer) happens in one spot rather than
//! being open-coded at each call site.
//!
//! Enforcement: `tests/test_kg_binding_single_writer_rust.py` scans the Rust
//! sources and fails if any file OUTSIDE the allowlist
//! (`bindings_writer.rs`, `project_state.rs`, `access.rs`, `migrations.rs`)
//! issues a direct `INSERT`/`UPDATE` against the two binding tables. So a new
//! caller cannot quietly open-code a write — it must route through the
//! canonical `Db` methods (ideally via this module's helpers).
//!
//! Why not physically move every write here? The `project_state` upserts and
//! the `access` heal carry intricate lock/transaction context; hoisting the
//! raw SQL would be a large, risky change for no behaviour gain. The contract
//! is enforced by the allowlist lint + this documented entry point, matching
//! the S-M sizing in the X-1 design (`DESIGN-part9-gated-themes` §2).

use serde_json::Value as JsonValue;

use crate::db::project_state::{ProjectCodegraphBinding, ProjectKgBinding};
use crate::db::Db;

/// The canonical suffix appended to the sanitized project name to form the
/// per-project primary KG collection name.
pub const KG_PRIMARY_SUFFIX: &str = "_KnowledgeGraph";

/// Seed (or upsert) a project's PRIMARY KG binding, deriving the collection
/// name from `project_name` via the shared sanitizer. This is the ONE place
/// the KG-name derivation and the KG-binding write are wired together.
///
/// The KG-name sanitizer is [`crate::db::access::sanitize_kg_collection_local`]
/// — byte-equivalent to the launcher-crate `sanitize_kg_collection` and pinned
/// against the Python SSOT (`vco_lib.codegraph_naming.sanitize_for_weaviate_class`)
/// by `tests/fixtures/kg_sanitizer_parity.json`. Callers pass the raw name; the
/// derived basename + `_KnowledgeGraph` suffix is written.
#[allow(clippy::too_many_arguments)]
pub fn write_kg_binding_primary_from_name(
    db: &Db,
    project_id: &str,
    project_name: &str,
    embedding_model: Option<&str>,
    embedding_dim: Option<i64>,
    weaviate_url: Option<&str>,
    config: &JsonValue,
) -> Result<ProjectKgBinding, String> {
    let basename = crate::db::access::sanitize_kg_collection_local(project_name);
    let collection = format!("{basename}{KG_PRIMARY_SUFFIX}");
    db.set_project_kg_binding(
        project_id,
        "primary",
        &collection,
        embedding_model,
        embedding_dim,
        None,
        weaviate_url,
        config,
    )
}

/// Write a KG binding with an explicit collection name (no derivation) — the
/// thin routing seam for callers that already hold the resolved collection
/// name (e.g. the "shared" role pointing at the fixed shared collection). Kept
/// here so ALL binding-creation call sites can name a single writer module.
#[allow(clippy::too_many_arguments)]
pub fn write_kg_binding(
    db: &Db,
    project_id: &str,
    role: &str,
    collection_name: &str,
    embedding_model: Option<&str>,
    embedding_dim: Option<i64>,
    kg_dir_path: Option<&str>,
    weaviate_url: Option<&str>,
    config: &JsonValue,
) -> Result<ProjectKgBinding, String> {
    db.set_project_kg_binding(
        project_id,
        role,
        collection_name,
        embedding_model,
        embedding_dim,
        kg_dir_path,
        weaviate_url,
        config,
    )
}

// v0.2.76 (R2): `write_codegraph_binding_from_name` was DELETED. It derived the
// code-graph collection prefix via the KG-name sanitizer
// (`sanitize_kg_collection_local`, underscore-DROPPING), which is the WRONG rule
// for code-graph collections — the analyzer stamps them with the
// underscore-PRESERVING `canonical_class_prefix`. Its only caller
// (`populate_codegraph_binding`) now derives via `canonical_class_prefix` and
// routes through `write_codegraph_binding` (explicit prefix) below. Do NOT
// reintroduce a from_name helper for the codegraph table: the KG sanitizer must
// never derive a codegraph prefix (that is the R2 bug).

/// Write a code-graph binding with an explicit, already-resolved prefix (no
/// derivation) — the routing seam for callers that carry the prefix (e.g. the
/// rename-propagation path, which derives the prefix with `canonical_class_prefix`).
#[allow(clippy::too_many_arguments)]
pub fn write_codegraph_binding(
    db: &Db,
    project_id: &str,
    collection_prefix: &str,
    embedding_model: Option<&str>,
    embedding_dim: Option<i64>,
    last_analyzed_commit: Option<&str>,
    last_analyzed_at: Option<i64>,
    enabled: bool,
    config: &JsonValue,
) -> Result<ProjectCodegraphBinding, String> {
    db.set_project_codegraph_binding(
        project_id,
        collection_prefix,
        embedding_model,
        embedding_dim,
        last_analyzed_commit,
        last_analyzed_at,
        enabled,
        config,
    )
}
