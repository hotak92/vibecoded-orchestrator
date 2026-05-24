// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.33 (Agent C, architecture review §10.b): startup reconciliation
//! between launcher.db `module_installs` rows and the on-disk extracted
//! manifest at `~/.vct/modules/<id>/vct-module.json`.
//!
//! ## Why
//!
//! After v0.2.33 the source-of-truth for "is module X installed" is
//! the `module_installs` row, and the source-of-truth for the FULL
//! manifest (gui.config_tab, db.migrations_dir, runtime block) is the
//! on-disk file at `~/.vct/modules/<id>/vct-module.json` extracted by
//! `module_manifest_extract::extract_manifest_from_image` during the
//! container_pull step.
//!
//! Those two sources can drift in three cases:
//!   1. user manually deletes `~/.vct/modules/<id>/` to "fix" a config
//!      issue, without uninstalling via the GUI;
//!   2. a crash interrupted `extract_manifest_from_image` AFTER the
//!      install row was inserted but BEFORE the atomic rename
//!      committed the manifest;
//!   3. the user is upgrading from a launcher version that wrote
//!      installs WITHOUT extracting (v0.2.32 and earlier — installs
//!      relied on the co-located `<install_path>/paid-modules/*/`
//!      affordance which isn't present in real-user installs).
//!
//! Without a reconciler, the catalog tile would render
//! kind=`installed` + button="Open dashboard", but the config tab
//! would fail because there's no manifest to read. The user sees a
//! broken state with no actionable fix.
//!
//! ## How
//!
//! Fires at launcher boot AFTER `vct-hub` is ensured running and
//! BEFORE the GUI's Modules tab is mounted (the call site is
//! `lib.rs::run` inside `setup()`). Walks every `module_installs` row
//! with `status='installed'`, checks if the on-disk manifest exists,
//! and flips missing rows to `status='broken'` (CHECK extended via
//! migration 021). The catalog refactor (Agent B) will read this
//! status and render kind=`broken` + button="Reinstall".
//!
//! ## Bounded soft-fail
//!
//! Reconciler runtime must stay <5 s (we want it to land before the
//! GUI mounts). Per-row errors log + skip, never abort the sweep.
//! DB errors at the list-step return early with an empty report so
//! the launcher boots even when the reconciler can't read its own DB.

use std::path::PathBuf;

use crate::db::Db;

/// Result of a reconciliation sweep.
#[derive(Debug, Clone, Default)]
pub struct ReconcileReport {
    /// Count of installed rows whose on-disk manifest is present.
    pub healthy: u32,
    /// `module_id`s of installed rows that were flipped to `broken`
    /// because their on-disk manifest was missing. Order matches the
    /// DB iteration order (most-recent-installed-first).
    pub broken: Vec<String>,
}

/// Walk every `module_installs` row with `status='installed'`. For
/// each, verify that `~/.vct/modules/<module_id>/vct-module.json`
/// exists. When it doesn't, flip the row's status to `'broken'`.
///
/// Soft-fail throughout: per-row errors log to stderr + skip, top-
/// level DB errors return an empty report so the launcher boots.
pub fn reconcile_installed_modules(db: &Db) -> ReconcileReport {
    let mut report = ReconcileReport::default();

    let vct_root = crate::paths::vct_root_dir();

    let installs = match db.list_module_installs_with_status("installed") {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("[reconciler] failed to list installed module rows: {}", e);
            return report;
        }
    };

    for row in installs {
        let manifest_path: PathBuf = vct_root
            .join("modules")
            .join(&row.module_id)
            .join("vct-module.json");

        if manifest_path.is_file() {
            report.healthy += 1;
            continue;
        }

        eprintln!(
            "[reconciler] {} ({}@{}): on-disk manifest missing at {}; marking status=broken",
            row.module_id,
            row.project_id,
            row.module_version,
            manifest_path.display(),
        );
        match db.set_module_install_status(&row.id, "broken") {
            Ok(()) => {
                report.broken.push(row.module_id);
            }
            Err(e) => {
                // Per-row soft-fail. The row stays at status='installed'
                // and will surface the same problem on next boot —
                // user sees a misleading-but-stable state instead of
                // a launcher that won't boot.
                eprintln!(
                    "[reconciler] failed to mark {} broken: {}",
                    row.module_id, e
                );
            }
        }
    }

    if !report.broken.is_empty() || report.healthy > 0 {
        eprintln!(
            "[reconciler] swept {} installed module row(s): {} healthy, {} broken",
            report.healthy as usize + report.broken.len(),
            report.healthy,
            report.broken.len(),
        );
    }

    report
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;
    use vct_launcher_core::db::models::ProjectHost;

    /// Set up an isolated VCT_STATE_DIR + in-memory Db pair with one
    /// project row that subsequent inserts can FK against.
    fn seed_env(tmp: &tempfile::TempDir) -> (Db, String) {
        std::env::set_var("VCT_STATE_DIR", tmp.path());
        let db = Db::open_in_memory().expect("open in-memory db");
        let project_id = Uuid::new_v4().to_string();
        db.insert_project(
            &project_id,
            "Test Project",
            tmp.path().to_str().unwrap(),
            ProjectHost::Base,
            "test-project",
        )
        .expect("insert project");
        (db, project_id)
    }

    /// Helper: insert an installed-state module_install row with a
    /// fresh UUID and the requested module_id.
    fn insert_installed(db: &Db, project_id: &str, module_id: &str) -> String {
        let install_id = Uuid::new_v4().to_string();
        db.insert_module_install(
            &install_id,
            project_id,
            module_id,
            "0.1.0",
            &format!("/tmp/fake-install/{}", module_id),
        )
        .expect("insert install");
        db.set_module_status(
            project_id,
            module_id,
            vct_launcher_core::db::models::ModuleStatus::Installed,
            None,
        )
        .expect("flip to installed");
        install_id
    }

    /// Helper: place a valid `vct-module.json` on disk at
    /// `<VCT_STATE_DIR>/modules/<id>/vct-module.json`.
    fn place_manifest_on_disk(vct_root: &std::path::Path, module_id: &str) {
        let dir = vct_root.join("modules").join(module_id);
        std::fs::create_dir_all(&dir).expect("mkdir");
        std::fs::write(dir.join("vct-module.json"), "{\"id\":\"x\"}").expect("write");
    }

    #[test]
    fn reconcile_marks_missing_manifest_as_broken() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        let install_id = insert_installed(&db, &project_id, "vct-missing-mod");
        // Intentionally do NOT place a manifest on disk.

        let report = reconcile_installed_modules(&db);

        assert_eq!(report.healthy, 0);
        assert_eq!(report.broken, vec!["vct-missing-mod".to_string()]);

        // Verify the DB row actually flipped status.
        let row = db
            .get_module_install(&project_id, "vct-missing-mod")
            .expect("get install")
            .expect("row present");
        assert_eq!(
            row.status,
            vct_launcher_core::db::models::ModuleStatus::Broken,
            "row {} must be marked broken",
            install_id
        );

        std::env::remove_var("VCT_STATE_DIR");
    }

    #[test]
    fn reconcile_preserves_healthy_installs() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        let _install_id = insert_installed(&db, &project_id, "vct-healthy-mod");
        place_manifest_on_disk(tmp.path(), "vct-healthy-mod");

        let report = reconcile_installed_modules(&db);

        assert_eq!(report.healthy, 1);
        assert!(report.broken.is_empty());

        // Row status untouched.
        let row = db
            .get_module_install(&project_id, "vct-healthy-mod")
            .expect("get install")
            .expect("row present");
        assert_eq!(
            row.status,
            vct_launcher_core::db::models::ModuleStatus::Installed,
            "healthy row must remain installed"
        );

        std::env::remove_var("VCT_STATE_DIR");
    }

    #[test]
    fn reconcile_mixed_population() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        let _h_id = insert_installed(&db, &project_id, "vct-healthy-mod");
        let _b_id = insert_installed(&db, &project_id, "vct-broken-mod");
        place_manifest_on_disk(tmp.path(), "vct-healthy-mod");

        let report = reconcile_installed_modules(&db);

        assert_eq!(report.healthy, 1);
        assert_eq!(report.broken, vec!["vct-broken-mod".to_string()]);

        std::env::remove_var("VCT_STATE_DIR");
    }

    #[test]
    fn reconcile_handles_empty_db_gracefully() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, _project_id) = seed_env(&tmp);

        // No module_installs rows seeded.
        let report = reconcile_installed_modules(&db);

        assert_eq!(report.healthy, 0);
        assert!(report.broken.is_empty());

        std::env::remove_var("VCT_STATE_DIR");
    }

    #[test]
    fn reconcile_ignores_non_installed_rows() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);

        // Insert one row but leave it in status='installing' — the
        // reconciler must NOT touch it (we only reconcile installed
        // rows; intermediate states are handled by the resume
        // machinery).
        let install_id = Uuid::new_v4().to_string();
        db.insert_module_install(
            &install_id,
            &project_id,
            "vct-installing-mod",
            "0.1.0",
            "/tmp/fake-install/vct-installing-mod",
        )
        .expect("insert");
        // Don't flip to installed; status stays 'installing'.

        let report = reconcile_installed_modules(&db);

        assert_eq!(report.healthy, 0);
        assert!(
            report.broken.is_empty(),
            "non-installed rows must be ignored, got broken={:?}",
            report.broken
        );

        // Row status untouched.
        let row = db
            .get_module_install(&project_id, "vct-installing-mod")
            .expect("get")
            .expect("row");
        assert_eq!(
            row.status,
            vct_launcher_core::db::models::ModuleStatus::Installing,
        );

        std::env::remove_var("VCT_STATE_DIR");
    }
}
