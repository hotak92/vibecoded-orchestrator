// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.92 W12 — the `role='shared'` KG binding row: resolve it from the
//! launcher DB, never from a compile-time guess, and repair the rows a guess
//! already poisoned.
//!
//! ## The defect
//!
//! `populate_kg_bindings` used to seed the shared binding row directly from
//! `LAST_RESORT_SHARED_KG_COLLECTION`. A last resort is a READ-TIME fallback:
//! persisting it as a row promotes a guess to permanent authority, because
//! every downstream reader is binding-first —
//! `vco_lib/config_projection.py` resolves the shared name as
//! `kg_bindings.get("shared", resolved_default)`, so an existing row outranks
//! the (correct) resolver forever, and the hub reads the row directly. On an
//! install whose orchestrator KG was re-pointed away from the bundled default
//! — the dev/ship naming split makes that STRUCTURAL, not accidental — the
//! constant names a class that does not exist, so every project born after
//! the re-point inherits a shared-KG pointer into the void. Retrieval returns
//! nothing and every component reports success.
//!
//! ## Discipline (deliberately identical to `commands::binding_reconcile`)
//!
//! * **Resolve, don't guess.** [`resolve_shared_kg_collection`] answers from
//!   the DB or answers `None`. When it answers `None` the caller writes NO
//!   row: a missing row falls through to the read-time resolvers, which are
//!   correct; a wrong row is permanent.
//! * **Positive evidence only.** A repair requires the configured class to be
//!   POSITIVELY absent AND the replacement POSITIVELY present in ONE
//!   successful `/v1/schema` snapshot. Probe failure is not evidence of
//!   absence — an unreachable Weaviate must never cause a rewrite.
//! * **Hands off deliberate human corrections.** A row whose config JSON
//!   carries a truthy `manual_override` sentinel is never rewritten here.
//!   (The sentinel's own meaning is direction-of-authority — "this row is
//!   source of truth, propagate it outward" — not a write-lock; treating it
//!   as hands-off for AUTOMATED repair is a deliberate policy on top of that,
//!   because the sentinel is only ever set by a human or by an
//!   evidence-backed heal.)
//! * **Soft-fail.** Any per-project error logs and moves on; nothing here may
//!   block boot or project creation.
//!
//! ## Where the repair runs
//!
//! [`repair_shared_kg_bindings`] takes the class-name snapshot as an
//! argument precisely so it can share the ONE `/v1/schema` fetch that
//! `binding_reconcile::reconcile_half_renamed_bindings_at_boot` already does
//! at launcher boot, instead of adding a second probe. Wiring it there is one
//! call; see the crate-level W12 notes in the handoff.

use std::collections::HashSet;
use std::path::Path;

use serde_json::Value as JsonValue;

use crate::commands::project_env_settings::APP_STATE_KEY_SHARED_KG_NAME;
use crate::db::Db;

/// The orchestrator-root project's reserved slug. Mirrors
/// `commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG`, which is what this
/// module imports; kept as a doc anchor for readers arriving from the Python
/// side (`vco_lib/config_projection.py` hard-codes the same slug).
use crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG;

/// Resolve the shared-KG collection name from launcher.db, or `None`.
///
/// Priority order — MUST MATCH the other implementations of this chain, minus
/// their last-resort constant:
///   1. `app_state[shared_kg.collection_name]` — the GUI's explicit override
///      (SharedKgPicker).
///   2. The orchestrator-root project's PRIMARY KG binding. This is the
///      source of truth on every machine that has run the launcher once
///      (`ensure_orchestrator_root_kg_binding` seeds it at boot).
///
/// The peer implementations are
/// `commands::project_env_settings::populate` (Rust, for the `.env` surface),
/// `vco_lib/config_projection.py::project_env_from_db` (the canonical
/// `.claude/{settings.json,env}` writer, whose leg 2 is
/// `_resolve_shared_kg_default_from_launcher_db`), and the hub's
/// `config_api::project_config`. All four now agree on the DB-backed legs.
///
/// **Returns `None` rather than a constant on purpose.** A constant is a
/// read-time fallback owned by each READER; this function exists for a
/// WRITER, and a writer that materialises the fallback into a row is the
/// exact defect being fixed. `project_env_settings::populate` still appends
/// `LAST_RESORT_SHARED_KG_COLLECTION` for its read-time answer, and
/// `shared_resolution_matches_env_settings_populate` pins the two together so
/// they cannot drift apart silently.
pub fn resolve_shared_kg_collection(db: &Db) -> Option<String> {
    if let Some(explicit) = db
        .app_state_get(APP_STATE_KEY_SHARED_KG_NAME)
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
    {
        return Some(explicit);
    }
    let root = db.get_project_by_slug(ORCHESTRATOR_ROOT_SLUG).ok().flatten()?;
    db.list_project_kg_bindings(&root.id)
        .ok()?
        .into_iter()
        .find(|b| b.role == "primary")
        .map(|b| b.collection_name)
        .filter(|s| !s.is_empty())
}

// ═══════════════════════════════════════════════════════════════════════
// The REPAIR half — WIRED at launcher boot (v0.2.92 W12 Task 1)
// ═══════════════════════════════════════════════════════════════════════
//
// [`repair_shared_kg_bindings`] runs from
// `commands::binding_reconcile::reconcile_half_renamed_bindings_at_boot`,
// which `lib.rs::setup()` spawns once per launcher boot. It is called there —
// and takes its schema snapshot as an ARGUMENT — so it shares that sweep's
// ONE `/v1/schema` fetch instead of adding a second probe. The sweep returns
// early on probe failure, so this code is never reached without a successful
// snapshot; the `Option` parameter keeps the probe-failure contract
// enforceable in isolation anyway.
//
// Until the wiring landed every item below carried a dead-code allow. They
// are gone: with a caller they are all reachable, and a lingering allow would
// be a lie about reachability that hides the next genuinely dead item.

/// True when a binding row's config JSON carries a truthy `manual_override`
/// sentinel.
///
/// MUST MATCH `vco_lib/kg_binding_read.py::config_has_manual_override` — the
/// ONE Python home for this predicate since v0.2.92 (it was inline in the
/// binding reader before, which this comment used to name by its older
/// `project_init.py` location). That function is
/// `bool(cfg.get("manual_override")) if isinstance(cfg, dict) else False` —
/// so Python truthiness is the contract: a present-but-empty string, `false`,
/// `0`, `[]`, `{}` and `null` are all NOT an override. Anything else is.
pub fn has_manual_override(config: &JsonValue) -> bool {
    let Some(obj) = config.as_object() else {
        return false; // `null`, a scalar, an array: not a config object
    };
    match obj.get("manual_override") {
        None | Some(JsonValue::Null) => false,
        Some(JsonValue::Bool(b)) => *b,
        Some(JsonValue::String(s)) => !s.is_empty(),
        Some(JsonValue::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Some(JsonValue::Array(a)) => !a.is_empty(),
        Some(JsonValue::Object(o)) => !o.is_empty(),
    }
}

/// The repair decision for ONE project's shared binding row. A pure value so
/// every gate is unit-testable without HTTP or a DB.
#[derive(Debug, PartialEq, Eq, Clone)]
pub enum SharedBindingDecision {
    /// No `role='shared'` row — nothing to repair (and nothing to poison:
    /// the read-time resolvers own this project's shared name).
    NoRow,
    /// The row carries a `manual_override` sentinel: a deliberate human
    /// correction. Never rewritten by an automated sweep.
    ManualOverride,
    /// The configured class exists on Weaviate — leave it alone.
    Healthy,
    /// The configured class is absent, but the evidence chain for a
    /// replacement did not complete. Write nothing; the phantom warning on
    /// the hub's `/config` is the honest report.
    NoEvidence,
    /// Every gate passed: rewrite the row to `to`.
    Repair { to: String },
}

/// The `role='shared'` repair gates, as a pure function.
///
/// * `current` — `Some((collection_name, config_json))` of the existing
///   shared row, or `None` when the project has none.
/// * `replacement` — the DB-resolved shared name
///   ([`resolve_shared_kg_collection`]), or `None` when nothing resolved.
/// * `classes_lower` — lowercased class names from ONE **successful**
///   `/v1/schema` fetch. The caller must not call this at all when the probe
///   failed; [`repair_shared_kg_bindings`] enforces that with an `Option`.
pub fn decide_shared_binding_repair(
    current: Option<(&str, &JsonValue)>,
    replacement: Option<&str>,
    classes_lower: &HashSet<String>,
) -> SharedBindingDecision {
    // Gate 1: a row must exist. No row is the SAFE state, not a broken one.
    let Some((current_name, config)) = current else {
        return SharedBindingDecision::NoRow;
    };
    if current_name.is_empty() {
        return SharedBindingDecision::NoRow;
    }

    // Gate 2: never touch a deliberate human correction. Checked BEFORE any
    // evidence is weighed so the promise holds no matter what the probe says.
    if has_manual_override(config) {
        return SharedBindingDecision::ManualOverride;
    }

    // Gate 3: the configured class must be POSITIVELY absent. (Case-
    // insensitive: a case-different sibling resolves fine — the hub's casing
    // rebind adopts on-disk casing — so it is not a phantom.)
    if classes_lower.contains(&current_name.to_lowercase()) {
        return SharedBindingDecision::Healthy;
    }

    // Gate 4: a replacement must resolve, differ from the phantom, and be
    // POSITIVELY present. Any miss ⇒ no write.
    let Some(replacement) = replacement.filter(|r| !r.is_empty()) else {
        return SharedBindingDecision::NoEvidence;
    };
    if replacement.to_lowercase() == current_name.to_lowercase() {
        // Same class (possibly different casing) and it does not exist —
        // there is nothing to repair TO. A case-only rebind is a different
        // operation with its own owner (`db::access`'s case-rebind heal).
        return SharedBindingDecision::NoEvidence;
    }
    if !classes_lower.contains(&replacement.to_lowercase()) {
        return SharedBindingDecision::NoEvidence;
    }

    SharedBindingDecision::Repair {
        to: replacement.to_string(),
    }
}

/// Outcome summary for the caller's log line.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct SharedKgRepairReport {
    /// The schema probe did not produce a snapshot — nothing was inspected
    /// or written. Distinct from "inspected and found healthy".
    pub probe_failed: bool,
    /// Projects whose shared row was examined.
    pub inspected: usize,
    /// Rows rewritten from a phantom to the evidence-backed name.
    pub repaired: usize,
    /// Rows left alone because they carry a `manual_override` sentinel.
    pub skipped_manual_override: usize,
    /// Rows whose class is absent but for which no replacement could be
    /// evidenced — the honest no-repair outcome.
    pub phantom_without_evidence: usize,
    /// `shared_kg_binding_repaired` ledger entries actually WRITTEN.
    ///
    /// Counts EMISSIONS, not repairs — the same discipline as
    /// `binding_reconcile`'s `phantom_deferrals` (wave-2 F8): a repair on a
    /// project whose orchestrator clone cannot be located, or whose emit
    /// subprocess fails, is logged and NOT counted, so a boot line reporting
    /// "N ledger records" never overstates what a human will find on disk.
    pub deferrals_emitted: usize,
}

/// Sweep every project's shared binding row and repair the poisoned ones.
///
/// `classes_lower` is `Some(snapshot)` from ONE successful `/v1/schema`
/// fetch, or `None` when the probe failed. `None` writes NOTHING: an
/// unreachable Weaviate is not evidence that classes are missing.
///
/// `repo_root` is the orchestrator clone root, used ONLY as the `sys.path`
/// root for the `vco_lib.deferral_emit` bridge that writes the ledger record
/// (v0.2.92 W12 Task 4). `None` disables the ledger leg entirely and changes
/// nothing about the repair itself — which is why the parameter is passed in
/// rather than discovered here: the boot caller already resolves the root
/// once for its own emits, and the unit tests can exercise every repair gate
/// without spawning a Python subprocess.
///
/// Idempotent — after a successful repair the "class absent" gate fails on
/// the next pass. Every write routes through the single binding-writer home
/// (`db::bindings_writer`) and is audit-logged.
pub fn repair_shared_kg_bindings(
    db: &Db,
    classes_lower: Option<&HashSet<String>>,
    repo_root: Option<&Path>,
) -> SharedKgRepairReport {
    let mut report = SharedKgRepairReport::default();

    let Some(classes_lower) = classes_lower else {
        report.probe_failed = true;
        tracing::debug!(
            "[vct] shared-kg-repair: no schema snapshot; skipping — probe \
             failure is not evidence of absence"
        );
        return report;
    };

    let replacement = resolve_shared_kg_collection(db);

    let projects = match db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            tracing::warn!("[vct] shared-kg-repair: list_projects failed: {}", e);
            return report;
        }
    };

    for project in &projects {
        let bindings = match db.list_project_kg_bindings(&project.id) {
            Ok(b) => b,
            Err(e) => {
                tracing::warn!(
                    "[vct] shared-kg-repair: bindings for {} unreadable: {}",
                    project.id,
                    e
                );
                continue;
            }
        };
        let shared = bindings.iter().find(|b| b.role == "shared");
        if shared.is_some() {
            report.inspected += 1;
        }

        let decision = decide_shared_binding_repair(
            shared.map(|b| (b.collection_name.as_str(), &b.config)),
            replacement.as_deref(),
            classes_lower,
        );

        match decision {
            SharedBindingDecision::Repair { to } => {
                // `shared` is Some here: every other arm covers the None case.
                let Some(row) = shared else { continue };
                let from = row.collection_name.clone();
                match crate::db::bindings_writer::write_kg_binding(
                    db,
                    &project.id,
                    "shared",
                    &to,
                    row.embedding_model.as_deref(),
                    row.embedding_dim,
                    row.kg_dir_path.as_deref(),
                    row.weaviate_url.as_deref(),
                    // Config carried over verbatim: this sweep repairs a
                    // NAME, it does not author policy.
                    &row.config,
                ) {
                    Ok(_) => {
                        db.audit(
                            "shared_kg_binding_repaired",
                            Some(&project.id),
                            None,
                            &serde_json::json!({
                                "from": from,
                                "to": to,
                                "reason": "shared_kg_phantom_repair_v0292",
                                "evidence": "configured class absent on Weaviate; \
                                             replacement resolved from launcher.db \
                                             and present on Weaviate",
                            }),
                        )
                        .ok();
                        tracing::info!(
                            "[vct] shared-kg-repair: {:?} shared binding {:?} -> {:?} \
                             (configured class absent on Weaviate, replacement present)",
                            project.name,
                            from,
                            to
                        );
                        report.repaired += 1;
                        if emit_repair_record(
                            repo_root,
                            Path::new(&project.folder_path),
                            &from,
                            &to,
                        ) {
                            report.deferrals_emitted += 1;
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            "[vct] shared-kg-repair: rewrite {} -> {} for {} failed: {}",
                            from,
                            to,
                            project.id,
                            e
                        );
                    }
                }
            }
            SharedBindingDecision::ManualOverride => {
                report.skipped_manual_override += 1;
                // The gate fires for EVERY overridden row, healthy or not —
                // that ordering is deliberate (the promise must not depend on
                // any later gate). The LOG level splits on whether the skip
                // actually costs the user anything: a pinned row naming a
                // class that does not exist is the one case where "we
                // deliberately did not fix this" needs to be visible, because
                // the automated repair is exactly what will not save them.
                let dead = shared
                    .map(|b| !classes_lower.contains(&b.collection_name.to_lowercase()))
                    .unwrap_or(false);
                if dead {
                    tracing::warn!(
                        "[vct] shared-kg-repair: {:?} shared binding names a class \
                         that does not exist on Weaviate AND carries \
                         manual_override — left untouched by design; a human \
                         pinned this row, so a human must re-point it",
                        project.name
                    );
                } else {
                    tracing::debug!(
                        "[vct] shared-kg-repair: {:?} shared binding carries \
                         manual_override; left untouched by design",
                        project.name
                    );
                }
            }
            SharedBindingDecision::NoEvidence => {
                report.phantom_without_evidence += 1;
                tracing::warn!(
                    "[vct] shared-kg-repair: {:?} shared binding names a class \
                     that does not exist on Weaviate and no replacement could \
                     be evidenced; leaving it alone (the hub's /config warning \
                     is the report)",
                    project.name
                );
            }
            SharedBindingDecision::Healthy | SharedBindingDecision::NoRow => {}
        }
    }

    report
}

/// Write the `shared_kg_binding_repaired` ledger record for ONE repaired
/// project. Returns `true` only when an entry was actually written.
///
/// Why a ledger record at all (v0.2.92 W12 Task 4): the repair rewrites a row
/// the user never asked us to touch. The audit row and the boot log line are
/// both invisible to the person who will next wonder why their shared-KG name
/// changed — `UPDATE_DEFERRED.md` is the surface a human (and their Claude)
/// actually reads at session start. The precedent is
/// `kg_access_phantom_repaired`, emitted by `binding_reconcile` from this same
/// boot sweep for the same reason and with the same lifecycle.
///
/// Classified `informational_record` in `vco_lib/deferral_conditions.toml`:
/// the repair ALREADY HAPPENED, so nothing is pending for the user, and
/// `command_to_apply` must not ask for an action. `clear_probe =
/// "owned-drop-when-absent"` gives it one-shot auto-expiry on the next
/// install/update run — safe here for exactly the reason the registry
/// documents for its siblings: this emitter runs at launcher BOOT, never
/// inside an `install.py` run, so no run's own finalize can drop the entry it
/// just wrote.
///
/// Soft-fail in both directions: no resolvable clone root, or a failed emit,
/// logs and returns `false`. The repair is already committed and audited; a
/// ledger failure must never be mistaken for a repair failure.
fn emit_repair_record(
    repo_root: Option<&Path>,
    project_folder: &Path,
    from: &str,
    to: &str,
) -> bool {
    let Some(root) = repo_root else {
        tracing::debug!(
            "[vct] shared-kg-repair: no orchestrator clone root resolved; \
             skipping the ledger record for {:?} (the repair itself already \
             landed and is audit-logged)",
            project_folder
        );
        return false;
    };
    let detected = format!(
        "This project's shared-KG binding named {:?}, a class that does not \
         exist on Weaviate, so the cross-project shared-KG fan-out \
         (hybrid_search / semantic_graph_search) matched NOTHING while every \
         component reported success. The launcher repointed the binding to \
         {:?} at boot — the name resolved from launcher.db (the explicit \
         shared-KG override, or the orchestrator-root project's primary KG \
         binding) and was confirmed present on Weaviate in the same schema \
         snapshot that showed the old one absent.",
        from, to
    );
    let fields = crate::services::deferral::DeferralEntryFields {
        condition_id: "shared_kg_binding_repaired",
        title: "Shared-KG binding repointed off a class that does not exist",
        detected: &detected,
        why_deferred: "Informational record of an automatic, evidence-gated \
             repair (configured class positively absent, resolved replacement \
             positively present, no manual_override on the row). Nothing is \
             pending.",
        command_to_apply: "# No action needed — the binding was already \
             repaired. To choose a DIFFERENT shared collection, use the \
             launcher's Identity tab -> 'Manage shared KG collection'; a row \
             set there carries a manual_override sentinel and this sweep will \
             never touch it again.",
        severity: "info",
    };
    match crate::services::deferral::emit_deferral_entry(root, project_folder, &fields) {
        Ok(()) => true,
        Err(e) => {
            tracing::warn!(
                "[vct] shared-kg-repair: ledger record emit failed for {:?} \
                 (non-fatal, the repair itself already landed): {}",
                project_folder,
                e
            );
            false
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    /// These unit tests exercise the REPAIR gates, not the ledger bridge:
    /// passing `None` for `repo_root` keeps every case a pure DB assertion
    /// with no `python -c` subprocess. The ledger leg has its own coverage —
    /// `emit_repair_record_is_a_soft_no_op_without_a_clone_root` here, and the
    /// end-to-end boot test in `commands::binding_reconcile`, which runs the
    /// REAL emit against a temp project folder.
    const NO_LEDGER: Option<&std::path::Path> = None;

    fn classes(names: &[&str]) -> HashSet<String> {
        names.iter().map(|n| n.to_lowercase()).collect()
    }

    fn cfg(raw: &str) -> JsonValue {
        serde_json::from_str(raw).unwrap()
    }

    fn folder_for(tag: &str) -> String {
        if cfg!(windows) {
            format!(r"C:\tmp\vct-sharedkg-{}", tag)
        } else {
            format!("/tmp/vct-sharedkg-{}", tag)
        }
    }

    fn seed_project(db: &Db, id: &str, name: &str, slug: &str, host: ProjectHost) {
        db.insert_project(id, name, &folder_for(id), host, slug)
            .unwrap();
    }

    fn seed_binding(db: &Db, project_id: &str, role: &str, collection: &str, config: &JsonValue) {
        crate::db::bindings_writer::write_kg_binding(
            db,
            project_id,
            role,
            collection,
            Some("qwen3-embedding:0.6b"),
            Some(1024),
            None,
            Some("http://localhost:8081"),
            config,
        )
        .unwrap();
    }

    fn shared_of(db: &Db, project_id: &str) -> Option<String> {
        db.list_project_kg_bindings(project_id)
            .unwrap()
            .into_iter()
            .find(|b| b.role == "shared")
            .map(|b| b.collection_name)
    }

    /// A DB shaped like the machine that produced the outage: an
    /// orchestrator-root whose real KG is `VCODev_KnowledgeGraph`, and a
    /// project whose shared row still names the bundled constant (a class
    /// that does not exist).
    fn poisoned_db() -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        seed_project(
            &db,
            "root-id",
            "VibeCoded Orchestrator",
            "orchestrator-root",
            ProjectHost::OrchestratorRoot,
        );
        seed_binding(&db, "root-id", "primary", "VCODev_KnowledgeGraph", &JsonValue::Null);
        seed_project(&db, "p1", "Transcrypt", "transcrypt", ProjectHost::Base);
        seed_binding(&db, "p1", "primary", "Transcrypt_KnowledgeGraph", &JsonValue::Null);
        seed_binding(
            &db,
            "p1",
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            &JsonValue::Null,
        );
        db
    }

    /// The live schema on such a machine: the real shared KG exists, the
    /// bundled-constant class does not.
    fn live_classes() -> HashSet<String> {
        classes(&[
            "VCODev_KnowledgeGraph",
            "Transcrypt_KnowledgeGraph",
            "VibeCodedOrchestrator_CodeModule",
        ])
    }

    // ─── resolution ─────────────────────────────────────────────────

    #[test]
    fn resolution_prefers_app_state_override_then_orchestrator_root() {
        let db = poisoned_db();
        assert_eq!(
            resolve_shared_kg_collection(&db).as_deref(),
            Some("VCODev_KnowledgeGraph"),
            "leg 2: the orchestrator-root primary binding"
        );

        db.app_state_set(APP_STATE_KEY_SHARED_KG_NAME, "TeamWide_KnowledgeGraph")
            .unwrap();
        assert_eq!(
            resolve_shared_kg_collection(&db).as_deref(),
            Some("TeamWide_KnowledgeGraph"),
            "leg 1: an explicit GUI override wins"
        );

        // An explicitly-empty override is not an override.
        db.app_state_set(APP_STATE_KEY_SHARED_KG_NAME, "").unwrap();
        assert_eq!(
            resolve_shared_kg_collection(&db).as_deref(),
            Some("VCODev_KnowledgeGraph")
        );
    }

    #[test]
    fn resolution_answers_none_rather_than_a_constant() {
        let db = Db::open_in_memory().expect("in-memory db");
        seed_project(&db, "p1", "Solo", "solo", ProjectHost::Base);
        assert_eq!(
            resolve_shared_kg_collection(&db),
            None,
            "no override and no orchestrator-root ⇒ None; materialising the \
             last-resort constant here is the defect being fixed"
        );

        // A root row with no PRIMARY binding resolves to nothing either.
        seed_project(
            &db,
            "root-id",
            "VibeCoded Orchestrator",
            "orchestrator-root",
            ProjectHost::OrchestratorRoot,
        );
        assert_eq!(resolve_shared_kg_collection(&db), None);
    }

    /// Composition pin for the READER side. Since W12 Task 3 there is only
    /// ONE launcher-side implementation of legs 1+2 — `populate` calls this
    /// function — so this no longer pins two copies against each other. What
    /// it still pins is the SPLIT that made the unification safe: `populate`'s
    /// answer must equal this one whenever resolution succeeds, and must
    /// differ from it by EXACTLY the last-resort constant when it does not.
    ///
    /// That asymmetry is the whole design. A reader may end its chain at a
    /// constant; a WRITER may not, because a constant persisted as a binding
    /// row outranks the correct resolver forever. If someone ever
    /// "simplifies" the resolver to return the constant itself, this test is
    /// what catches it — the second half would start agreeing.
    #[test]
    fn shared_resolution_matches_env_settings_populate() {
        use crate::commands::project_env_settings::{
            populate, LAST_RESORT_SHARED_KG_COLLECTION,
        };

        let db = poisoned_db();
        let resolved = resolve_shared_kg_collection(&db);
        assert_eq!(
            resolved.as_deref(),
            Some(populate(&db, "Transcrypt", Some("p1")).shared_kg_collection.as_str()),
            "the two resolvers must agree whenever resolution succeeds"
        );

        let empty = Db::open_in_memory().expect("in-memory db");
        assert_eq!(resolve_shared_kg_collection(&empty), None);
        assert_eq!(
            populate(&empty, "Solo", None).shared_kg_collection,
            LAST_RESORT_SHARED_KG_COLLECTION,
            "…and differ only by the reader-side last-resort constant"
        );
    }

    /// v0.2.92 W12 Task 3 — CROSS-CRATE parity for the shared-KG resolver.
    ///
    /// The launcher's two copies of legs 1+2 were collapsed onto this
    /// function. A THIRD implementation survives in
    /// `vct-hub/src/config_api.rs::resolve_shared_kg_from_orchestrator_root`
    /// and cannot be collapsed: `vct-hub` depends on `vct-launcher-core` only,
    /// never on the launcher binary crate that owns this code. So it is pinned
    /// BEHAVIOURALLY instead — the truth table below is asserted verbatim on
    /// both sides against the same `Db` type. Its twin is
    /// `config_api::tests::shared_resolution_truth_table_matches_launcher`.
    /// Change one, the other goes red.
    ///
    /// Behavioural, not textual: it drives the real functions and compares
    /// their ANSWERS, so a refactor that keeps behaviour keeps the test green
    /// and a divergence in either direction turns it red.
    #[test]
    fn shared_resolution_truth_table() {
        // Row 1: nothing recorded → None (never a guessed constant).
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(resolve_shared_kg_collection(&db), None);

        // Row 2: an orchestrator-root row with NO primary binding → still None.
        seed_project(
            &db,
            "root-id",
            "VibeCoded Orchestrator",
            "orchestrator-root",
            ProjectHost::OrchestratorRoot,
        );
        assert_eq!(resolve_shared_kg_collection(&db), None);

        // Row 3: the orchestrator-root PRIMARY binding is the answer.
        seed_binding(&db, "root-id", "primary", "VCODev_KnowledgeGraph", &JsonValue::Null);
        assert_eq!(
            resolve_shared_kg_collection(&db).as_deref(),
            Some("VCODev_KnowledgeGraph"),
        );

        // Row 4: an EMPTY collection_name is not an answer. Raw SQL because
        // the row writer will not produce one — the filter guards a
        // hand-edited DB, and a guard nobody tests gets "simplified" away.
        {
            let guard = db.lock();
            guard
                .execute(
                    "UPDATE project_kg_bindings SET collection_name = '' \
                     WHERE project_id = 'root-id' AND role = 'primary'",
                    [],
                )
                .unwrap();
        }
        assert_eq!(resolve_shared_kg_collection(&db), None);
    }

    // ─── manual_override detection (Python-parity) ──────────────────

    #[test]
    fn manual_override_matches_python_truthiness() {
        assert!(has_manual_override(&cfg(r#"{"manual_override":"v0.2.40-prefix-adopt"}"#)));
        assert!(has_manual_override(&cfg(r#"{"manual_override":true}"#)));
        assert!(has_manual_override(&cfg(r#"{"manual_override":1}"#)));

        assert!(!has_manual_override(&cfg(r#"{"manual_override":""}"#)));
        assert!(!has_manual_override(&cfg(r#"{"manual_override":false}"#)));
        assert!(!has_manual_override(&cfg(r#"{"manual_override":0}"#)));
        assert!(!has_manual_override(&cfg(r#"{"manual_override":null}"#)));
        assert!(!has_manual_override(&cfg("{}")));
        assert!(!has_manual_override(&JsonValue::Null));
        assert!(!has_manual_override(&cfg(r#"{"other":"x"}"#)));
    }

    // ─── decision gates ─────────────────────────────────────────────

    #[test]
    fn decision_repairs_only_on_complete_evidence() {
        let live = live_classes();
        let phantom = "VibeCodedOrchestrator_KnowledgeGraph";
        let real = "VCODev_KnowledgeGraph";

        // ACT: absent configured class + present resolved replacement.
        assert_eq!(
            decide_shared_binding_repair(Some((phantom, &JsonValue::Null)), Some(real), &live),
            SharedBindingDecision::Repair {
                to: real.to_string()
            }
        );

        // LEAVE-ALONE: the configured class exists.
        assert_eq!(
            decide_shared_binding_repair(Some((real, &JsonValue::Null)), Some(real), &live),
            SharedBindingDecision::Healthy
        );

        // LEAVE-ALONE: replacement resolved but is ALSO absent — this is the
        // gate that stops one phantom being swapped for another.
        assert_eq!(
            decide_shared_binding_repair(
                Some((phantom, &JsonValue::Null)),
                Some("AlsoMissing_KnowledgeGraph"),
                &live
            ),
            SharedBindingDecision::NoEvidence
        );

        // LEAVE-ALONE: nothing resolved at all.
        assert_eq!(
            decide_shared_binding_repair(Some((phantom, &JsonValue::Null)), None, &live),
            SharedBindingDecision::NoEvidence
        );

        // LEAVE-ALONE: replacement is the same class (case-insensitively).
        assert_eq!(
            decide_shared_binding_repair(
                Some((phantom, &JsonValue::Null)),
                Some("vibecodedorchestrator_knowledgegraph"),
                &live
            ),
            SharedBindingDecision::NoEvidence
        );

        // LEAVE-ALONE: a case-different sibling of the configured class
        // exists, so reads resolve — not a phantom.
        let cased = classes(&["vibecodedorchestrator_knowledgegraph", real]);
        assert_eq!(
            decide_shared_binding_repair(Some((phantom, &JsonValue::Null)), Some(real), &cased),
            SharedBindingDecision::Healthy
        );

        // LEAVE-ALONE: no row.
        assert_eq!(
            decide_shared_binding_repair(None, Some(real), &live),
            SharedBindingDecision::NoRow
        );
    }

    /// The promise made in writing to the affected user: a row carrying a
    /// deliberate, evidence-backed human correction is never rewritten by an
    /// automated sweep — even when every other gate would pass.
    #[test]
    fn decision_never_touches_a_manual_override_row() {
        let live = live_classes();
        let sentinel = cfg(r#"{"manual_override":"transcrypt-2026-09-01-phantom-shared-kg"}"#);

        assert_eq!(
            decide_shared_binding_repair(
                Some(("VibeCodedOrchestrator_KnowledgeGraph", &sentinel)),
                Some("VCODev_KnowledgeGraph"),
                &live
            ),
            SharedBindingDecision::ManualOverride,
            "an overridden row must be skipped even with full repair evidence"
        );
    }

    // ─── sweep behaviour ────────────────────────────────────────────

    #[test]
    fn sweep_repairs_the_poisoned_row() {
        let db = poisoned_db();
        let report = repair_shared_kg_bindings(&db, Some(&live_classes()), NO_LEDGER);

        assert_eq!(report.repaired, 1, "report: {:?}", report);
        assert!(!report.probe_failed);
        assert_eq!(
            shared_of(&db, "p1").as_deref(),
            Some("VCODev_KnowledgeGraph")
        );

        // Idempotent: the second pass finds it healthy and writes nothing.
        let again = repair_shared_kg_bindings(&db, Some(&live_classes()), NO_LEDGER);
        assert_eq!(again.repaired, 0, "report: {:?}", again);
    }

    /// The ledger leg is OPTIONAL and its failure is not the repair's
    /// failure. With no clone root the record is skipped, the counter stays
    /// honest at 0, and the repair itself still lands — the ordering that
    /// matters, since a ledger problem must never be mistaken for (or block)
    /// a data fix.
    #[test]
    fn a_missing_clone_root_skips_the_record_without_touching_the_repair() {
        let db = poisoned_db();
        let report = repair_shared_kg_bindings(&db, Some(&live_classes()), None);

        assert_eq!(report.repaired, 1, "report: {:?}", report);
        assert_eq!(
            report.deferrals_emitted, 0,
            "no clone root ⇒ no record — and the counter must not claim one"
        );
        assert_eq!(
            shared_of(&db, "p1").as_deref(),
            Some("VCODev_KnowledgeGraph"),
            "the repair does not depend on the ledger leg"
        );
        assert!(!emit_repair_record(
            None,
            std::path::Path::new("/tmp/vct-sharedkg-nonexistent"),
            "A",
            "B"
        ));
    }

    #[test]
    fn sweep_preserves_the_row_metadata_it_is_not_repairing() {
        let db = poisoned_db();
        repair_shared_kg_bindings(&db, Some(&live_classes()), NO_LEDGER);

        let row = db
            .list_project_kg_bindings("p1")
            .unwrap()
            .into_iter()
            .find(|b| b.role == "shared")
            .expect("shared row");
        assert_eq!(row.embedding_model.as_deref(), Some("qwen3-embedding:0.6b"));
        assert_eq!(row.embedding_dim, Some(1024));
        assert_eq!(row.weaviate_url.as_deref(), Some("http://localhost:8081"));
    }

    #[test]
    fn sweep_writes_nothing_when_the_probe_failed() {
        let db = poisoned_db();
        let report = repair_shared_kg_bindings(&db, None, NO_LEDGER);

        assert!(report.probe_failed, "report: {:?}", report);
        assert_eq!(report.repaired, 0);
        assert_eq!(
            shared_of(&db, "p1").as_deref(),
            Some("VibeCodedOrchestrator_KnowledgeGraph"),
            "an unreachable Weaviate must never trigger a rewrite"
        );
    }

    #[test]
    fn sweep_writes_nothing_when_the_replacement_is_also_absent() {
        let db = poisoned_db();
        // The orchestrator-root's own class is missing too (e.g. a wiped
        // Weaviate): there is no evidenced target.
        let sparse = classes(&["Transcrypt_KnowledgeGraph"]);
        let report = repair_shared_kg_bindings(&db, Some(&sparse), NO_LEDGER);

        assert_eq!(report.repaired, 0, "report: {:?}", report);
        assert_eq!(report.phantom_without_evidence, 1);
        assert_eq!(
            shared_of(&db, "p1").as_deref(),
            Some("VibeCodedOrchestrator_KnowledgeGraph")
        );
    }

    #[test]
    fn sweep_skips_manual_override_rows() {
        let db = poisoned_db();
        // Re-stamp p1's shared row with the sentinel the companion used.
        seed_binding(
            &db,
            "p1",
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            &cfg(r#"{"manual_override":"transcrypt-2026-09-01-phantom-shared-kg"}"#),
        );

        let report = repair_shared_kg_bindings(&db, Some(&live_classes()), NO_LEDGER);

        assert_eq!(report.repaired, 0, "report: {:?}", report);
        assert_eq!(report.skipped_manual_override, 1);
        assert_eq!(
            shared_of(&db, "p1").as_deref(),
            Some("VibeCodedOrchestrator_KnowledgeGraph"),
            "a deliberate human correction is not ours to overwrite"
        );
    }

    #[test]
    fn sweep_leaves_healthy_and_rowless_projects_untouched() {
        let db = poisoned_db();
        // A healthy project…
        seed_project(&db, "p2", "Healthy", "healthy", ProjectHost::Base);
        seed_binding(&db, "p2", "shared", "VCODev_KnowledgeGraph", &JsonValue::Null);
        // …and one with no shared row at all.
        seed_project(&db, "p3", "Rowless", "rowless", ProjectHost::Base);
        seed_binding(&db, "p3", "primary", "Rowless_KnowledgeGraph", &JsonValue::Null);

        let report = repair_shared_kg_bindings(&db, Some(&live_classes()), NO_LEDGER);

        assert_eq!(report.repaired, 1, "only p1 is poisoned: {:?}", report);
        assert_eq!(
            shared_of(&db, "p2").as_deref(),
            Some("VCODev_KnowledgeGraph")
        );
        assert_eq!(
            shared_of(&db, "p3"),
            None,
            "a rowless project must NOT gain a row from the repair sweep"
        );
    }
}
