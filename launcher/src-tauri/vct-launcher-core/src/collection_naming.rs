// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! One collection-naming rule — the single Rust home (v0.2.84 D1).
//!
//! Before v0.2.84 the DEVELOPMENT_COLLECTION (and, by extension, the KG /
//! diagrams) name was derived in THREE places with drifting rules:
//!
//!   * hub `config_api.rs` — CORRECT (v0.2.46 Decision C): suffix-swap
//!     from the RESOLVED primary KG binding, slug-sanitized fallback.
//!   * launcher `project_env_settings.rs::populate` — WRONG: name-derived
//!     from the display name, ignoring the binding.
//!   * python `config_projection.py::project_env_from_db` — WRONG (the
//!     live-reverter): keyed off a dead `role='archive'` binding row that
//!     no writer ever creates, so it ALWAYS name-derived and rewrote the
//!     other homes' correct value away on every install-bundle / update.
//!
//! v0.2.84 P2 unifies the rule ONCE PER LANGUAGE (tier B of A>B>C — a
//! python subprocess is rejected because the hub serves `/config` on
//! every hook call and the launcher must resolve with a broken venv).
//! This module is the Rust home; `vco_lib/config_projection.py::
//! project_env_from_db` is the python realization; both are pinned to the
//! same rule by parity tests (`tests/test_v0284_dev_collection_one_rule.py`
//! + the in-crate `#[cfg(test)]` cases below). Both the hub (config_api)
//! and the launcher (`populate`) DELEGATE here.
//!
//! ## The rule (LOCKED — == hub v0.2.46 Decision C)
//!
//!   * **KG (primary)** = the project's `project_kg_bindings(role='primary')
//!     .collection_name` when a row exists; else, as a LAST RESORT with no
//!     binding, `sanitize_kg_collection(name)_KnowledgeGraph` (the
//!     underscore-DROPPING, name-based sanitizer — matches `populate()`'s
//!     historical no-DB default and the python `_sanitize_kg_collection`
//!     fallback, so `populate_with_no_state_returns_canonical_defaults`
//!     stays green). The HUB never reaches this arm: it 503s
//!     (`service_misconfigured`) on a missing primary binding BEFORE
//!     calling here, and passes the binding it already resolved.
//!   * **dev / diagrams** = suffix-swap when the resolved KG ends
//!     `_KnowledgeGraph` (basename + `_Development` / `_Diagrams`); else —
//!     the custom-rename case where a primary binding exists but doesn't
//!     end `_KnowledgeGraph` — `sanitize_collection_prefix(slug or name)_
//!     <Suffix>` (the underscore-PRESERVING, slug-based sanitizer, byte-
//!     identical to the hub's pinned test
//!     `config_development_collection_falls_back_to_slug_for_non_canonical
//!     _primary`). This fallback fires ONLY for those custom-rename
//!     installs — a narrow blast radius.
//!
//! ## No Weaviate probing here
//!
//! The on-disk casing rebind (`resolve_existing_casing_for_class`) is a
//! surface concern and STAYS at the hub (config_api) and at install.py's
//! `_resolve_existing_casing`. This module produces the PRE-rebind
//! candidate names only; the hub layers its probe on top of the candidates
//! we return, byte-identically to its prior inline derivation.

use crate::db::access::sanitize_kg_collection_local;
use crate::db::Db;

/// The `_KnowledgeGraph` suffix that marks a canonical primary KG name.
pub const KG_SUFFIX: &str = "_KnowledgeGraph";
/// The `_Development` suffix for the development collection.
pub const DEV_SUFFIX: &str = "_Development";
/// The `_Diagrams` suffix for the diagrams collection.
pub const DIAGRAMS_SUFFIX: &str = "_Diagrams";

/// The three per-project collection names resolved by the one rule.
///
/// These are PRE-casing-rebind candidates. Callers that need on-disk
/// casing (the hub) layer their Weaviate probe on top of each field.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectCollections {
    /// `<primary-binding | sanitize_kg_collection(name)>` (+ `_KnowledgeGraph`
    /// when name-derived).
    pub kg: String,
    /// Development collection — suffix-swap from `kg`, slug-fallback for a
    /// non-`_KnowledgeGraph` primary.
    pub dev: String,
    /// Diagrams collection — same rule as `dev` with the `_Diagrams` suffix.
    pub diagrams: String,
}

/// Resolve the KG / dev / diagrams collection names for a project by the
/// ONE rule (v0.2.84 D1). See the module docstring for the full contract.
///
/// * `project_id` — `Some` to consult `project_kg_bindings`; `None`
///   (test / fresh-create contexts) skips the binding read and goes
///   straight to the name-derived last resort.
/// * `project_name` — the display name; drives the KG last-resort
///   basename via `sanitize_kg_collection`.
/// * `slug` — the URL-safe slug; drives the dev/diagrams NON-canonical
///   fallback via `sanitize_collection_prefix` (mirrors the hub, which
///   passes `project.slug`). `None` falls back to `project_name` for the
///   same fallback (the plan's "slug or name").
///
/// Soft-fail: any DB error resolving the binding is treated as "no
/// binding" (the name-derived last resort), never a panic — env
/// resolution is on a hot path and must not crash on a metadata hiccup.
pub fn resolve_project_collections(
    db: &Db,
    project_id: Option<&str>,
    project_name: &str,
    slug: Option<&str>,
) -> ProjectCollections {
    // 1. KG: binding-first, name-derived last resort.
    let kg = resolve_kg_collection(db, project_id, project_name);

    // 2. dev / diagrams: suffix-swap from the resolved KG; slug-sanitized
    //    fallback for a non-`_KnowledgeGraph` primary (custom-rename case).
    let fallback_seed = slug.filter(|s| !s.is_empty()).unwrap_or(project_name);
    let dev = derive_sibling_collection(&kg, DEV_SUFFIX, fallback_seed);
    let diagrams = derive_sibling_collection(&kg, DIAGRAMS_SUFFIX, fallback_seed);

    ProjectCollections { kg, dev, diagrams }
}

/// Resolve the primary KG collection name: `project_kg_bindings(role=
/// 'primary')` when present, else the name-derived last resort.
///
/// Split out so `populate()` and the hub can both reach the exact same
/// binding-first logic (and so the name-derived arm has ONE home).
pub fn resolve_kg_collection(
    db: &Db,
    project_id: Option<&str>,
    project_name: &str,
) -> String {
    if let Some(pid) = project_id {
        if let Ok(bindings) = db.list_project_kg_bindings(pid) {
            if let Some(primary) = bindings.iter().find(|b| b.role == "primary") {
                let name = primary.collection_name.trim();
                if !name.is_empty() {
                    return name.to_string();
                }
            }
        }
    }
    // Last resort, no binding: name-derived (underscore-DROPPING). Matches
    // populate()'s pre-D1 default + python `_sanitize_kg_collection`.
    format!("{}{}", sanitize_kg_collection_local(project_name), KG_SUFFIX)
}

/// Derive a `dev` / `diagrams` sibling name from a resolved KG name.
///
/// Suffix-swap when `kg` ends `_KnowledgeGraph` (basename + `suffix`);
/// else the slug-sanitized fallback (custom-rename primary). `suffix` is
/// [`DEV_SUFFIX`] or [`DIAGRAMS_SUFFIX`].
///
/// Public so the hub (`config_api`) can call it on its ALREADY
/// casing-rebound KG name — the hub layers its Weaviate casing probe on
/// the KG first, then derives siblings from the rebound value, so it must
/// pass its rebound KG here rather than let `resolve_project_collections`
/// derive from the pre-rebind binding. The output is byte-identical to the
/// hub's prior inline `if kg.ends_with(_KnowledgeGraph) { … } else { … }`
/// blocks (Decision C), only now sharing the ONE rule.
pub fn derive_sibling_collection(kg: &str, suffix: &str, fallback_seed: &str) -> String {
    if let Some(basename) = kg.strip_suffix(KG_SUFFIX) {
        format!("{}{}", basename, suffix)
    } else {
        format!("{}{}", sanitize_collection_prefix(fallback_seed), suffix)
    }
}

/// Inline ASCII-safe slug → class-prefix sanitiser (underscore-PRESERVING).
///
/// v0.2.84 D1: PROMOTED verbatim from `vct-hub/src/config_api.rs` (where it
/// was a file-local `fn`) into `vct-launcher-core` so the ONE rule +
/// its sanitizer share a home. The hub now re-exports it
/// (`use vct_launcher_core::collection_naming::sanitize_collection_prefix;`)
/// so its Decision C dev/diagrams fallback stays byte-identical.
///
/// Used ONLY for the dev/diagrams fallback when the primary KG binding's
/// name doesn't end with `_KnowledgeGraph` (custom-rename install). The
/// launcher's `project_naming::canonical_class_prefix` is the spec'd
/// codegraph-prefix version; this is the older, distinct algorithm the
/// hub's Decision C fallback used, kept byte-identical to avoid changing
/// the resolved name on any real install.
///
/// Algorithm (mirrors `_sanitize_collection_prefix` in the Python
/// analyzer + `config_projection._sanitize_collection_prefix`):
///   1. Replace non-alphanumeric ASCII chars with `_`.
///   2. Trim leading/trailing `_`, then capitalize the first character.
///   3. If empty after trimming, return `Project`.
///
/// Distinct from `sanitize_kg_collection` (underscore-DROPPING, `vct`
/// fallback): that one drives the KG basename; this one drives the
/// dev/diagrams non-canonical fallback. The two are deliberately
/// different rules with different fallbacks — keep them separate.
pub fn sanitize_collection_prefix(slug: &str) -> String {
    let mut out = String::with_capacity(slug.len());
    for ch in slug.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    let trimmed = out.trim_matches('_');
    if trimmed.is_empty() {
        return "Project".to_string();
    }
    let mut chars = trimmed.chars();
    let first = chars.next().unwrap().to_ascii_uppercase();
    let mut result = String::with_capacity(trimmed.len());
    result.push(first);
    result.extend(chars);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;
    use crate::db::Db;

    /// Seed a project row (required for the KG-binding FK) + a primary KG
    /// binding so `resolve_project_collections` reads the binding-first
    /// path. `resolve_project_collections` reads name/slug from its
    /// parameters (not the row), so the row only exists to satisfy the FK.
    fn seed_primary_binding(db: &Db, project_id: &str, name: &str, kg_collection: &str) {
        db.insert_project(
            project_id,
            name,
            &format!("/tmp/vct-test-{}", project_id),
            ProjectHost::Base,
            &format!("slug-{}", project_id),
        )
        .unwrap();
        db.set_project_kg_binding(
            project_id,
            "primary",
            kg_collection,
            None,
            None,
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();
    }

    // ── Parity for the promoted sanitizer (drift point per planner note #3) ──

    /// The rehomed `sanitize_collection_prefix` must byte-match the hub's
    /// prior inline `fn` (config_api.rs) AND the python
    /// `_sanitize_collection_prefix` on the exact cases the hub pinned +
    /// degenerate inputs (empty / all-symbol / leading-non-letter).
    #[test]
    fn sanitize_collection_prefix_parity() {
        assert_eq!(sanitize_collection_prefix("myproject"), "Myproject");
        assert_eq!(sanitize_collection_prefix("my-project"), "My_project");
        assert_eq!(sanitize_collection_prefix("my project"), "My_project");
        assert_eq!(sanitize_collection_prefix("MyProject"), "MyProject");
        assert_eq!(sanitize_collection_prefix("weirdproject"), "Weirdproject");
        // Degenerate: empty / all-symbol / leading-symbol all fall to
        // "Project" (underscore-PRESERVING sanitizer's fallback, NOT the
        // KG sanitizer's "vct").
        assert_eq!(sanitize_collection_prefix(""), "Project");
        assert_eq!(sanitize_collection_prefix("---"), "Project");
        assert_eq!(sanitize_collection_prefix("!!!"), "Project");
        // Leading digit is preserved as-is by this sanitizer (unlike the
        // KG one which rejects to "vct") — matches the hub's historical fn.
        assert_eq!(sanitize_collection_prefix("123"), "123");
    }

    // ── KG resolution: binding-first, name-derived last resort ──

    #[test]
    fn kg_binding_first_when_primary_present() {
        let db = Db::open_in_memory().unwrap();
        seed_primary_binding(&db, "p1", "VibeCoded Orchestrator", "VCODev_KnowledgeGraph");
        let c = resolve_project_collections(&db, Some("p1"), "VibeCoded Orchestrator", Some("vco"));
        assert_eq!(c.kg, "VCODev_KnowledgeGraph");
    }

    #[test]
    fn kg_name_derived_when_no_binding() {
        let db = Db::open_in_memory().unwrap();
        // No project_id → no binding read → name-derived last resort.
        let c = resolve_project_collections(&db, None, "Acme", Some("acme"));
        assert_eq!(c.kg, "Acme_KnowledgeGraph");
        assert_eq!(c.dev, "Acme_Development");
        assert_eq!(c.diagrams, "Acme_Diagrams");
    }

    #[test]
    fn kg_name_derived_underscore_dropping() {
        let db = Db::open_in_memory().unwrap();
        // KG basename uses the underscore-DROPPING rule (matches
        // sanitize_kg_collection): "snake_case_name" → "SnakeCaseName".
        let c = resolve_project_collections(&db, None, "snake_case_name", Some("snake-case-name"));
        assert_eq!(c.kg, "SnakeCaseName_KnowledgeGraph");
        // dev/diagrams suffix-swap off the resolved KG (ends _KnowledgeGraph).
        assert_eq!(c.dev, "SnakeCaseName_Development");
        assert_eq!(c.diagrams, "SnakeCaseName_Diagrams");
    }

    // ── dev/diagrams: suffix-swap vs slug-fallback ──

    /// FAIL-WITHOUT-FIX PIN (P2 no-name-derivation-when-binding-resolves):
    /// binding `VCODev_KnowledgeGraph` + display name "VibeCoded
    /// Orchestrator" (which name-derives to VibeCodedOrchestrator_*) ⇒ dev
    /// MUST be `VCODev_Development` (suffix-swap off the BINDING), never
    /// `VibeCodedOrchestrator_Development`.
    #[test]
    fn dev_suffix_swaps_from_binding_not_name() {
        let db = Db::open_in_memory().unwrap();
        seed_primary_binding(&db, "p2", "VibeCoded Orchestrator", "VCODev_KnowledgeGraph");
        let c = resolve_project_collections(&db, Some("p2"), "VibeCoded Orchestrator", Some("vco"));
        assert_eq!(c.kg, "VCODev_KnowledgeGraph");
        assert_eq!(
            c.dev, "VCODev_Development",
            "dev must suffix-swap off the resolved binding, NOT name-derive \
             from the display name (P2 regression: was \
             VibeCodedOrchestrator_Development)"
        );
        assert_eq!(c.diagrams, "VCODev_Diagrams");
    }

    /// Non-`_KnowledgeGraph` primary (custom-rename install) ⇒ dev/diagrams
    /// fall back to `sanitize_collection_prefix(slug)_<Suffix>`. Byte-
    /// matches the hub's pinned test
    /// (config_development_collection_falls_back_to_slug_for_non_canonical
    /// _primary): slug "weirdproject" → "Weirdproject_Development".
    #[test]
    fn dev_slug_fallback_for_non_canonical_primary() {
        let db = Db::open_in_memory().unwrap();
        seed_primary_binding(&db, "p3", "Weird Project", "WeirdName_Custom");
        let c = resolve_project_collections(&db, Some("p3"), "Weird Project", Some("weirdproject"));
        assert_eq!(c.kg, "WeirdName_Custom");
        assert_eq!(c.dev, "Weirdproject_Development");
        assert_eq!(c.diagrams, "Weirdproject_Diagrams");
    }

    /// Slug None ⇒ the non-canonical fallback seeds off the NAME instead
    /// (the plan's "slug or name").
    #[test]
    fn dev_slug_fallback_uses_name_when_slug_absent() {
        let db = Db::open_in_memory().unwrap();
        seed_primary_binding(&db, "p4", "Weird Project", "WeirdName_Custom");
        let c = resolve_project_collections(&db, Some("p4"), "Weird Project", None);
        // sanitize_collection_prefix("Weird Project") → "Weird_Project".
        assert_eq!(c.dev, "Weird_Project_Development");
    }

    /// Empty primary binding value is treated as "no binding" (name-
    /// derived last resort), never an empty collection name.
    #[test]
    fn empty_primary_binding_falls_to_name_derived() {
        let db = Db::open_in_memory().unwrap();
        seed_primary_binding(&db, "p5", "Acme", "   ");
        let c = resolve_project_collections(&db, Some("p5"), "Acme", Some("acme"));
        assert_eq!(c.kg, "Acme_KnowledgeGraph");
        assert_eq!(c.dev, "Acme_Development");
    }
}
