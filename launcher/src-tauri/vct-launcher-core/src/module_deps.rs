// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! `requirements.depends_on` — the reader (v0.2.97, review R6 F50 round 2).
//!
//! The manifest spec (`docs/VCT_MODULE_MANIFEST_SPEC.md` §4.1) declares
//! `depends_on` as "other module ids this one needs", and the bundled
//! manifests carry it, but nothing read it: a module could be installed or
//! enabled with its dependency absent. This is the ONE home for the rule,
//! used by:
//!
//! * the launcher's install, update and enable commands
//!   (`commands::modules::{install_module_for_project,
//!   update_module_for_project, set_module_enabled_v2}`) — they refuse, with a
//!   message naming every missing module, and never install one on the user's
//!   behalf;
//! * `validate-manifest` — every `depends_on` id must be a KNOWN module id
//!   ([`unknown_dependencies`]).
//!
//! A dependency is satisfied by a bundled core module (always installed —
//! [`crate::bundled_manifests::BUNDLED_MANIFESTS`]) or by an install row for
//! that module, per-project or global, whose status says the module is
//! actually there (`installed` / `running` / `stopped`). A row that is still
//! `installing`, failed (`error`) or `broken` does not satisfy it.

use std::collections::BTreeSet;

use crate::db::models::{ModuleInstallRow, ModuleStatus};
use crate::db::Db;
use crate::manifest::ModuleManifest;

/// Ids of the bundled core modules (each file's stem is its id — pinned by
/// `bundled_manifests::tests::every_referenced_module_id_is_an_embedded_manifest`).
pub fn bundled_module_ids() -> Vec<String> {
    crate::bundled_manifests::BUNDLED_MANIFESTS
        .iter()
        .map(|(file, _)| file.trim_end_matches(".json").to_string())
        .collect()
}

/// The manifest's `depends_on`, trimmed, de-duplicated, self-references and
/// empty entries dropped, in declaration order.
pub fn declared_dependencies(manifest: &ModuleManifest) -> Vec<String> {
    let mut seen = BTreeSet::new();
    manifest
        .requirements
        .depends_on
        .iter()
        .map(|d| d.trim().to_string())
        .filter(|d| !d.is_empty() && *d != manifest.id && seen.insert(d.clone()))
        .collect()
}

/// Whether an install row means the module is actually present.
pub fn row_satisfies(row: &ModuleInstallRow) -> bool {
    matches!(
        row.status,
        ModuleStatus::Installed | ModuleStatus::Running | ModuleStatus::Stopped
    )
}

/// The declared dependencies of `manifest` that are NOT satisfied for
/// `project_id`. `Err` when the launcher DB cannot be read — a dependency
/// that cannot be confirmed is not assumed present.
pub fn missing_dependencies(
    db: &Db,
    project_id: &str,
    manifest: &ModuleManifest,
) -> Result<Vec<String>, String> {
    let bundled = bundled_module_ids();
    let mut missing = Vec::new();
    for dep in declared_dependencies(manifest) {
        if bundled.contains(&dep) {
            continue;
        }
        let per_project = db.get_module_install(project_id, &dep)?;
        let global = db.get_global_module_install(&dep)?;
        let present = per_project.iter().chain(global.iter()).any(row_satisfies);
        if !present {
            missing.push(dep);
        }
    }
    Ok(missing)
}

/// The refusal text for `action` ("install" / "update" / "enable") of
/// `module_id` when `missing` is non-empty. Names every missing module and
/// says what to do; says explicitly that nothing was installed for the user.
pub fn refusal_message(action: &str, module_id: &str, missing: &[String]) -> String {
    let list = missing.join(", ");
    format!(
        "cannot {} {}: it depends on {} which {} not installed for this project. \
         Install {} first (Modules tab), then {} {} again — VCO does not install a \
         dependency on your behalf.",
        action,
        module_id,
        list,
        if missing.len() == 1 { "is" } else { "are" },
        if missing.len() == 1 { "it" } else { "them" },
        action,
        module_id,
    )
}

/// The ONE gate the install / update / enable commands call: `Ok(())` when
/// every dependency is satisfied, else `Err(refusal_message)`; a DB read
/// failure is also a refusal (the dependency could not be confirmed).
pub fn check_dependencies(
    db: &Db,
    project_id: &str,
    manifest: &ModuleManifest,
    action: &str,
) -> Result<(), String> {
    let missing = missing_dependencies(db, project_id, manifest).map_err(|e| {
        format!(
            "cannot {} {}: its dependencies ({}) could not be checked: {}",
            action,
            manifest.id,
            declared_dependencies(manifest).join(", "),
            e
        )
    })?;
    if missing.is_empty() {
        Ok(())
    } else {
        Err(refusal_message(action, &manifest.id, &missing))
    }
}

/// `validate-manifest`'s rule: the declared dependencies that are not a
/// known module id (`known` = the bundled ids plus whatever the caller knows
/// — the other manifests validated alongside, and `--known-module` ids for
/// modules published only in the catalog).
pub fn unknown_dependencies(manifest: &ModuleManifest, known: &BTreeSet<String>) -> Vec<String> {
    declared_dependencies(manifest)
        .into_iter()
        .filter(|d| !known.contains(d))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn manifest(id: &str, deps: &[&str]) -> ModuleManifest {
        let json = serde_json::json!({
            "manifest_version": 1,
            "id": id,
            "name": id,
            "version": "1.0.0",
            "description": "fixture",
            "category": "paid-independent",
            "license": {"required": false, "min_orchestrator_tier": "free"},
            "compatibility": {"hosts": ["base"]},
            "requirements": {"depends_on": deps},
            "install": {"method": "local"},
            "runtime": {"type": "cli"}
        });
        ModuleManifest::from_json(&json.to_string()).unwrap()
    }

    fn db_with_project() -> Db {
        let db = Db::open_in_memory().unwrap();
        db.insert_project("p1", "P1", "/tmp/p1", ProjectHost::Base, "p1").unwrap();
        db.insert_project("p2", "P2", "/tmp/p2", ProjectHost::Base, "p2").unwrap();
        db
    }

    #[test]
    fn no_dependencies_and_bundled_dependencies_pass() {
        let db = db_with_project();
        assert!(check_dependencies(&db, "p1", &manifest("m", &[]), "install").is_ok());
        assert!(check_dependencies(&db, "p1", &manifest("m", &["vct-kg", "vct-code-embedding"]), "install").is_ok());
    }

    #[test]
    fn a_missing_dependency_is_refused_by_name() {
        let db = db_with_project();
        let err = check_dependencies(&db, "p1", &manifest("vct-x", &["vct-kg", "vct-dep", "vct-other"]), "install")
            .unwrap_err();
        assert!(err.contains("cannot install vct-x"), "{}", err);
        assert!(err.contains("vct-dep, vct-other") && err.contains("are not installed"), "{}", err);
        assert!(!err.contains("vct-kg"), "a bundled dependency is not missing: {}", err);
        assert!(err.contains("does not install a dependency on your behalf"), "{}", err);
    }

    #[test]
    fn an_installed_dependency_satisfies_and_only_for_its_own_project() {
        let db = db_with_project();
        db.insert_module_install("i1", "p1", "vct-dep", "1.0.0", "/x").unwrap();
        // Still `installing` — not there yet.
        assert!(check_dependencies(&db, "p1", &manifest("m", &["vct-dep"]), "install").is_err());
        db.set_module_status("p1", "vct-dep", ModuleStatus::Installed, None).unwrap();
        assert!(check_dependencies(&db, "p1", &manifest("m", &["vct-dep"]), "install").is_ok());
        assert!(check_dependencies(&db, "p2", &manifest("m", &["vct-dep"]), "install").is_err());
        for bad in [ModuleStatus::Error, ModuleStatus::Broken] {
            db.set_module_status("p1", "vct-dep", bad, None).unwrap();
            assert!(check_dependencies(&db, "p1", &manifest("m", &["vct-dep"]), "enable").is_err());
        }
        for good in [ModuleStatus::Running, ModuleStatus::Stopped] {
            db.set_module_status("p1", "vct-dep", good, None).unwrap();
            assert!(check_dependencies(&db, "p1", &manifest("m", &["vct-dep"]), "enable").is_ok());
        }
    }

    #[test]
    fn a_global_install_satisfies_every_project() {
        let db = db_with_project();
        db.insert_global_module_install("g1", "vct-dep", "1.0.0", "/g").unwrap();
        db.set_global_module_status("vct-dep", ModuleStatus::Installed, None).unwrap();
        assert!(check_dependencies(&db, "p1", &manifest("m", &["vct-dep"]), "install").is_ok());
        assert!(check_dependencies(&db, "p2", &manifest("m", &["vct-dep"]), "install").is_ok());
    }

    #[test]
    fn self_duplicate_and_blank_entries_are_not_dependencies() {
        let m = manifest("vct-x", &["vct-x", " vct-dep ", "vct-dep", ""]);
        assert_eq!(declared_dependencies(&m), vec!["vct-dep".to_string()]);
    }

    #[test]
    fn unknown_dependencies_names_only_unknown_ids() {
        let known: BTreeSet<String> = bundled_module_ids().into_iter().chain(["vct-paid".to_string()]).collect();
        let m = manifest("vct-x", &["vct-kg", "vct-paid", "vct-ollama"]);
        assert_eq!(unknown_dependencies(&m, &known), vec!["vct-ollama".to_string()]);
    }
}
