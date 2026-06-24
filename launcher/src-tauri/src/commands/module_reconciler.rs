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
    /// v0.2.66: `module_id`s of rows that were wedged at
    /// `status='installing'` at boot and auto-healed to `status='error'`.
    /// A row in this state can only be a leftover from an install process
    /// that died with the previous launcher run (no install survives a
    /// launcher restart), so transitioning it at boot is always safe.
    /// Healing it stops the forever-spinner in the GUI and makes the row
    /// eligible for the existing `retry_failed_module_installs` path.
    pub healed_installing: Vec<String>,
}

/// v0.2.43 V0243-12: return true when the on-disk manifest at `path`
/// declares `install.method == "container_pull"`. Reads and parses the
/// JSON file; any error (missing, malformed) returns false so the caller
/// falls through to the existing healthy/broken path without crashing.
fn manifest_indicates_container_pull(manifest_path: &std::path::Path) -> bool {
    let raw = match std::fs::read_to_string(manifest_path) {
        Ok(s) => s,
        Err(_) => return false,
    };
    // Avoid deserializing the full manifest — just check the install.method
    // field via a lightweight JSON value parse.
    let val: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return false,
    };
    val.pointer("/install/method")
        .and_then(|v| v.as_str())
        .map(|m| m == "container_pull")
        .unwrap_or(false)
}

/// v0.2.43 V0243-12: return true when `last_error` matches the failure
/// patterns for a pull that failed at the credential / registry layer.
/// These indicate a hard-broken state (registry auth failure, not a
/// transient network blip) that warrants a `broken` status flip so the
/// GUI can render a Reinstall CTA.
///
/// Patterns (case-insensitive substring):
///   - "pull fail"      — generic `podman pull` failure log
///   - "unauthorized"   — GHCR HTTP 401 (anonymous pull rejected)
///   - "retrieve auth"  — Docker credential helper error
fn last_error_indicates_pull_failure(last_error: &str) -> bool {
    let lower = last_error.to_lowercase();
    lower.contains("pull fail")
        || lower.contains("unauthorized")
        || lower.contains("retrieve auth")
}

/// Walk every `module_installs` row with `status='installed'`. For
/// each, verify that `<vct_root>/modules/<module_id>/vct-module.json`
/// exists. When it doesn't, flip the row's status to `'broken'`.
///
/// Soft-fail throughout: per-row errors log to stderr + skip, top-
/// level DB errors return an empty report so the launcher boots.
///
/// `vct_root_override` is only used by unit tests to avoid touching
/// `VCT_STATE_DIR` (which is process-wide and racy under `cargo test
/// --jobs N`). Production callers pass `None` and the function
/// resolves the root via `crate::paths::vct_root_dir()`.
pub fn reconcile_installed_modules(db: &Db) -> ReconcileReport {
    reconcile_installed_modules_inner(db, None)
}

#[doc(hidden)]
#[cfg(test)]
pub(crate) fn reconcile_installed_modules_for_test(
    db: &Db,
    vct_root: &std::path::Path,
) -> ReconcileReport {
    reconcile_installed_modules_inner(db, Some(vct_root))
}

fn reconcile_installed_modules_inner(
    db: &Db,
    vct_root_override: Option<&std::path::Path>,
) -> ReconcileReport {
    let mut report = ReconcileReport::default();

    let vct_root_buf;
    let vct_root = match vct_root_override {
        Some(r) => r,
        None => {
            vct_root_buf = crate::paths::vct_root_dir();
            &vct_root_buf
        }
    };

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
            // v0.2.43 V0243-12: even when the manifest is present, check
            // whether a container_pull module is stuck in a pull-failure
            // state (container_name IS NULL AND last_error indicates a
            // hard auth/registry failure). These rows have status='installed'
            // but will never auto-recover — surface them as broken so the
            // GUI can show a Reinstall CTA.
            let is_container_pull = manifest_indicates_container_pull(&manifest_path);
            if is_container_pull
                && row.container_name.is_none()
                && row.last_error
                    .as_deref()
                    .map(last_error_indicates_pull_failure)
                    .unwrap_or(false)
            {
                eprintln!(
                    "[reconciler] V0243-12: {} ({}): container_pull, \
                     container_name=NULL, last_error indicates pull failure \
                     ({:?}); marking status=broken",
                    row.module_id,
                    row.project_id.as_deref().unwrap_or("<global>"),
                    row.last_error.as_deref().unwrap_or(""),
                );
                match db.set_module_install_status(&row.id, "broken") {
                    Ok(()) => {
                        report.broken.push(row.module_id.clone());
                    }
                    Err(e) => {
                        eprintln!(
                            "[reconciler] failed to mark {} broken (pull-fail): {}",
                            row.module_id, e
                        );
                    }
                }
                continue;
            }

            report.healthy += 1;
            continue;
        }

        eprintln!(
            "[reconciler] {} ({}@{}): on-disk manifest missing at {}; marking status=broken",
            row.module_id,
            row.project_id.as_deref().unwrap_or("<global>"),
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

    // v0.2.66: auto-heal rows wedged at status='installing'. This is a
    // separate sweep (the 'installed' walk above queries status='installed'
    // and never sees 'installing' rows) over status='installing'.
    heal_wedged_installing_rows(db, &mut report);

    if !report.broken.is_empty()
        || report.healthy > 0
        || !report.healed_installing.is_empty()
    {
        eprintln!(
            "[reconciler] swept {} installed module row(s): {} healthy, {} broken; \
             {} wedged-installing row(s) auto-healed to error",
            report.healthy as usize + report.broken.len(),
            report.healthy,
            report.broken.len(),
            report.healed_installing.len(),
        );
    }

    report
}

/// Actionable `last_error` written when auto-healing a wedged
/// `installing` row. Public so the GUI / tests can match on it; phrased
/// as a user-facing instruction (the retry path turns it into a
/// Reinstall CTA).
pub const WEDGED_INSTALL_HEAL_MESSAGE: &str =
    "install was interrupted (launcher restarted mid-install) — click Retry to reinstall";

/// v0.2.66: auto-heal `module_installs` rows stuck at
/// `status='installing'`.
///
/// ## Why this is safe (conservative-default discipline)
///
/// `status='installing'` is written by `install_module_for_project`
/// at the start of an install and only ever transitioned to
/// `installed`/`error` by the SAME in-process async task when the pull
/// resolves. No install survives a launcher restart — the task that set
/// `installing` died with the previous process. Therefore ANY row still
/// at `installing` when this runs at BOOT is, by construction, a leftover
/// from an install that was interrupted (crash, force-quit, kill during
/// pull) and will never self-complete. Transitioning it to `error` at
/// boot reconcile is the positively-confirmed-not-live case the
/// conservative-default rule asks for.
///
/// This is BOOT-only via the existing reconcile call site
/// (`lib.rs::run` setup). It deliberately does NOT run mid-session,
/// where an `installing` row CAN be a genuine in-progress install — there
/// we cannot confirm non-liveness from the row alone, so we do nothing
/// (leave it for the next boot if it never completes).
///
/// ## Effect
///
/// Sets `status='error'` + an actionable `last_error` atomically (single
/// by-id UPDATE). The row stops rendering a forever-spinner and becomes
/// eligible for the existing `retry_failed_module_installs` predicate
/// (`status IN ('error','broken')`). Soft-fail per-row: a DB error logs +
/// skips, never aborts the sweep or blocks boot.
fn heal_wedged_installing_rows(db: &Db, report: &mut ReconcileReport) {
    let rows = match db.list_module_installs_with_status("installing") {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!(
                "[reconciler] failed to list wedged 'installing' rows: {}",
                e
            );
            return;
        }
    };

    for row in rows {
        eprintln!(
            "[reconciler] {} ({}@{}): wedged at status='installing' at boot \
             (no install in progress across a restart) — healing to status=error",
            row.module_id,
            row.project_id.as_deref().unwrap_or("<global>"),
            row.module_version,
        );
        match db.set_module_install_status_with_error(
            &row.id,
            "error",
            Some(WEDGED_INSTALL_HEAL_MESSAGE),
        ) {
            Ok(()) => report.healed_installing.push(row.module_id),
            Err(e) => {
                // Per-row soft-fail: the row stays 'installing' and is
                // re-attempted on the next boot. Never block startup.
                eprintln!(
                    "[reconciler] failed to heal wedged 'installing' row {}: {}",
                    row.module_id, e
                );
            }
        }
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;
    use vct_launcher_core::db::models::ProjectHost;

    /// Set up an in-memory Db pair with one project row that subsequent
    /// inserts can FK against. Does NOT touch VCT_STATE_DIR — tests use
    /// `reconcile_installed_modules_for_test(db, tmp.path())` to pass the
    /// vct root directly, avoiding process-wide env races.
    fn seed_env(tmp: &tempfile::TempDir) -> (Db, String) {
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

        let report = reconcile_installed_modules_for_test(&db, tmp.path());

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
    }

    #[test]
    fn reconcile_preserves_healthy_installs() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        let _install_id = insert_installed(&db, &project_id, "vct-healthy-mod");
        place_manifest_on_disk(tmp.path(), "vct-healthy-mod");

        let report = reconcile_installed_modules_for_test(&db, tmp.path());

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
    }

    #[test]
    fn reconcile_mixed_population() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        let _h_id = insert_installed(&db, &project_id, "vct-healthy-mod");
        let _b_id = insert_installed(&db, &project_id, "vct-broken-mod");
        place_manifest_on_disk(tmp.path(), "vct-healthy-mod");

        let report = reconcile_installed_modules_for_test(&db, tmp.path());

        assert_eq!(report.healthy, 1);
        assert_eq!(report.broken, vec!["vct-broken-mod".to_string()]);
    }

    #[test]
    fn reconcile_handles_empty_db_gracefully() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, _project_id) = seed_env(&tmp);

        // No module_installs rows seeded.
        let report = reconcile_installed_modules_for_test(&db, tmp.path());

        assert_eq!(report.healthy, 0);
        assert!(report.broken.is_empty());
    }

    // ----------------------------------------------------------------
    // v0.2.43 V0243-12: container_pull pull-failure broken detection.
    // ----------------------------------------------------------------

    /// Helper: place a `vct-module.json` on disk with `install.method=container_pull`.
    fn place_container_pull_manifest(vct_root: &std::path::Path, module_id: &str) {
        let dir = vct_root.join("modules").join(module_id);
        std::fs::create_dir_all(&dir).expect("mkdir");
        let manifest = r#"{"id":"x","install":{"method":"container_pull","install_dir":"{VCT_MODULES}"}}"#;
        std::fs::write(dir.join("vct-module.json"), manifest).expect("write manifest");
    }

    /// V0243-12 T1: container_pull row with container_name=NULL and a
    /// pull-failure error must be flipped to broken.
    #[test]
    fn reconcile_marks_container_pull_with_pull_error_as_broken() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        insert_installed(&db, &project_id, "vct-pull-fail-mod");
        // Place a container_pull manifest.
        place_container_pull_manifest(tmp.path(), "vct-pull-fail-mod");
        // Set last_error to a pull-failure string; container_name stays NULL.
        db.set_module_status(
            &project_id,
            "vct-pull-fail-mod",
            vct_launcher_core::db::models::ModuleStatus::Installed,
            Some("podman pull failed: unauthorized: authentication required".to_string()),
        ).expect("set last_error");

        let report = reconcile_installed_modules_for_test(&db, tmp.path());

        assert_eq!(report.healthy, 0);
        assert_eq!(report.broken, vec!["vct-pull-fail-mod".to_string()]);
    }

    /// V0243-12 T2: container_pull row with a non-failure last_error stays healthy.
    #[test]
    fn reconcile_keeps_container_pull_with_benign_error_healthy() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        insert_installed(&db, &project_id, "vct-benign-mod");
        place_container_pull_manifest(tmp.path(), "vct-benign-mod");
        // A benign / unrelated error string (e.g. a port bind warning).
        db.set_module_status(
            &project_id,
            "vct-benign-mod",
            vct_launcher_core::db::models::ModuleStatus::Installed,
            Some("port 11441 in use; retrying".to_string()),
        ).expect("set last_error");

        let report = reconcile_installed_modules_for_test(&db, tmp.path());

        assert_eq!(report.healthy, 1, "benign-error row must be healthy");
        assert!(report.broken.is_empty());
    }

    // ----------------------------------------------------------------
    // v0.2.66: auto-heal of wedged status='installing' rows at boot.
    // ----------------------------------------------------------------

    /// THE ACT (destructive-branch discipline): a row wedged at
    /// status='installing' at boot — left behind by an install process
    /// that died with the previous launcher run — is auto-healed to
    /// status='error' with a non-empty, actionable last_error. This is
    /// the row the GUI used to render as a forever-spinner; after the
    /// heal it stops spinning AND becomes eligible for retry.
    ///
    /// (Pre-v0.2.66 the reconciler left 'installing' rows untouched on
    /// the theory that "intermediate states are handled by resume
    /// machinery" — but no such machinery transitions 'installing', so
    /// the row lived forever. This test pins the corrected behaviour.)
    #[test]
    fn reconcile_heals_wedged_installing_row_to_error() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);

        // insert_module_install lands the row at status='installing'.
        // Do NOT flip it to installed — simulate the interrupted-install
        // leftover.
        let install_id = Uuid::new_v4().to_string();
        db.insert_module_install(
            &install_id,
            &project_id,
            "vct-installing-mod",
            "0.1.0",
            "/tmp/fake-install/vct-installing-mod",
        )
        .expect("insert");

        let report = reconcile_installed_modules_for_test(&db, tmp.path());

        // The 'installing' walk is separate from the 'installed' walk —
        // this row contributes to healed_installing, not healthy/broken.
        assert_eq!(report.healthy, 0);
        assert!(report.broken.is_empty());
        assert_eq!(
            report.healed_installing,
            vec!["vct-installing-mod".to_string()],
            "wedged 'installing' row must be auto-healed",
        );

        // The DB row actually flipped to error WITH a non-empty,
        // actionable last_error (the original wedge's silent failure was
        // status='installing' + last_error=NULL — both must change).
        let row = db
            .get_module_install(&project_id, "vct-installing-mod")
            .expect("get")
            .expect("row");
        assert_eq!(
            row.status,
            vct_launcher_core::db::models::ModuleStatus::Error,
            "wedged 'installing' must become 'error' so retry can pick it up",
        );
        let last_error = row
            .last_error
            .as_deref()
            .expect("healed row must carry a last_error, not NULL");
        assert!(
            !last_error.is_empty(),
            "last_error must be non-empty (actionable for the user)",
        );
        assert_eq!(
            last_error, WEDGED_INSTALL_HEAL_MESSAGE,
            "last_error must be the actionable heal message",
        );
    }

    /// THE LEAVE-ALONE (destructive-branch discipline): a healthy
    /// status='installed' row (manifest present on disk) is NOT touched
    /// by the wedged-installing heal. Guards against the heal sweep
    /// over-reaching into terminal states.
    #[test]
    fn reconcile_leaves_installed_row_untouched_by_installing_heal() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, project_id) = seed_env(&tmp);
        let _id = insert_installed(&db, &project_id, "vct-terminal-installed");
        place_manifest_on_disk(tmp.path(), "vct-terminal-installed");

        let report = reconcile_installed_modules_for_test(&db, tmp.path());

        assert_eq!(report.healthy, 1);
        assert!(
            report.healed_installing.is_empty(),
            "an installed row must NOT be swept by the installing-heal, \
             got healed_installing={:?}",
            report.healed_installing,
        );

        let row = db
            .get_module_install(&project_id, "vct-terminal-installed")
            .expect("get")
            .expect("row");
        assert_eq!(
            row.status,
            vct_launcher_core::db::models::ModuleStatus::Installed,
            "installed row must remain installed (heal must not over-reach)",
        );
        assert!(
            row.last_error.is_none(),
            "installed row's last_error must stay NULL",
        );
    }
}
