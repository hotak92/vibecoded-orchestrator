// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! v0.2.49 access-matrix overhaul, Phase 6 S-4 (boot sanity check).
//!
//! ## What
//!
//! A non-blocking boot probe that walks every `projects` row at launcher
//! startup, fs::Path::is_dir-checks the recorded `folder_path`, and stamps
//! the `folder_missing_at_last_boot` column accordingly.
//!
//! The frontend (`ProjectCard.svelte`) reads the resulting flag via the
//! `read_project_folder_missing_flags` Tauri command and renders a non-
//! blocking warning banner on the affected project card ("Folder not
//! found at <path>. Did you move or delete it?"). The flag clears
//! automatically when the folder reappears on a subsequent boot — the
//! probe is idempotent and updates every row on every boot, so a once-
//! missing folder that comes back is detected and surfaced cleanly.
//!
//! ## Why a boot probe (vs lazy check on project open)
//!
//! Lazy "check on open" leaves the broken state invisible until the user
//! tries to do something with the project. By the time the user clicks
//! "Update bundle" or "Run hooks" the env writers / KG sync / codegraph
//! analysis are deep into an opaque failure path with confusing errors.
//! A single check at boot — front-loaded, with a clear "your folder is
//! gone" affordance — collapses N broken downstream paths into one
//! actionable banner.
//!
//! ## Soft-fail discipline
//!
//! This is a UX safety net, not a load-bearing gate:
//!   - DB read errors return Ok(()) with eprintln + skip (launcher boots
//!     even when the probe can't run).
//!   - Per-row IO errors (e.g. permission denied) treat the folder as
//!     "present" (false flag) — better to err on the side of not nagging
//!     than to flap on transient permission issues.
//!   - Per-row UPDATE failures eprintln + continue — the next boot
//!     retries.
//!
//! ## Coordination
//!
//! The lib.rs spawn site runs this probe INDEPENDENTLY of the Stream C
//! adopt/reconcile oneshot pair — it doesn't need ordering coordination
//! because it touches a column that lives on `projects` (orthogonal to
//! `project_kg_bindings` / `kg_collection_access`). Spawned as a separate
//! `tauri::async_runtime::spawn` task so it doesn't block window paint.

use crate::db::Db;
use serde::Serialize;
use std::path::Path;
use tauri::{command, State};

/// Per-project verdict returned by the probe + the read command.
#[derive(Debug, Clone, Serialize)]
pub struct ProjectFolderFlag {
    /// Project UUID (matches `projects.id`).
    pub id: String,
    /// On-disk path the project was registered against. Surface this in
    /// the banner copy so the user knows which folder needs attention.
    pub folder_path: String,
    /// True when the boot probe could not resolve `folder_path` to a
    /// directory on the last boot. The flag is read at boot time, not
    /// on demand — the frontend should not treat it as a live signal.
    pub folder_missing_at_last_boot: bool,
}

/// Tally of changes made by a single probe sweep. Used by callers (lib.rs
/// + tests) to emit useful eprintln + audit-trail entries.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProbeReport {
    /// Count of rows whose flag flipped from false → true (folder
    /// disappeared between boots).
    pub newly_missing: u32,
    /// Count of rows whose flag flipped from true → false (folder
    /// reappeared between boots).
    pub newly_returned: u32,
    /// Count of rows whose flag stayed false (folder healthy, no change).
    pub unchanged_healthy: u32,
    /// Count of rows whose flag stayed true (folder still missing,
    /// no change since last probe).
    pub unchanged_missing: u32,
    /// Project IDs whose UPDATE failed mid-sweep (eprintln'd; counted
    /// here so callers can surface in the audit trail).
    pub update_errors: Vec<String>,
}

/// Walk every `projects` row, probe the folder, write the verdict back.
///
/// `folder_existence_check`: injection seam for tests. Production callers
/// use `default_folder_check` (delegating to `Path::is_dir`). Tests pass
/// a closure that returns canned results without touching the filesystem.
///
/// Returns a [`ProbeReport`] summarising what changed; on a top-level DB
/// error returns the default (empty) report after logging.
pub fn run_folder_probe<F>(db: &Db, folder_existence_check: F) -> ProbeReport
where
    F: Fn(&str) -> bool,
{
    let mut report = ProbeReport::default();

    let rows = match db.list_project_folder_paths() {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!(
                "[folder-probe] failed to list project folder paths: {} \
                 (boot probe skipped this cycle)",
                e
            );
            return report;
        }
    };

    for (id, folder_path, previously_missing) in rows {
        // Treat empty folder_path as "missing" — a non-empty string is a
        // precondition of the create-project flow, but legacy rows or
        // direct SQL edits could leave it blank, and an empty string is
        // never a valid directory.
        let currently_missing = if folder_path.trim().is_empty() {
            true
        } else {
            !folder_existence_check(&folder_path)
        };

        match (previously_missing, currently_missing) {
            (false, false) => report.unchanged_healthy += 1,
            (true, true) => report.unchanged_missing += 1,
            (false, true) => {
                // Folder disappeared between boots. Flip the flag.
                if let Err(e) = db.set_project_folder_missing_flag(&id, true) {
                    eprintln!(
                        "[folder-probe] failed to mark project {} as folder-missing: {}",
                        id, e
                    );
                    report.update_errors.push(id.clone());
                    continue;
                }
                eprintln!(
                    "[folder-probe] project {}: folder is missing at {} (flagged)",
                    id, folder_path
                );
                report.newly_missing += 1;
            }
            (true, false) => {
                // Folder reappeared between boots — clear the flag.
                if let Err(e) = db.set_project_folder_missing_flag(&id, false) {
                    eprintln!(
                        "[folder-probe] failed to clear project {}'s folder-missing flag: {}",
                        id, e
                    );
                    report.update_errors.push(id.clone());
                    continue;
                }
                eprintln!(
                    "[folder-probe] project {}: folder restored at {} (cleared)",
                    id, folder_path
                );
                report.newly_returned += 1;
            }
        }
    }

    report
}

/// Production folder-existence check: delegates to `Path::is_dir`. Symlinks
/// that resolve to a directory count as present; symlinks that dangle count
/// as missing — that's the correct semantic for a "did the user move/
/// delete this folder?" probe.
pub fn default_folder_check(path: &str) -> bool {
    Path::new(path).is_dir()
}

/// Tauri command that returns the cached boot-probe verdict for every
/// project. The frontend wires this into the `ProjectCard` payload so
/// the warning banner can render without an extra round-trip.
///
/// Read-only: this does NOT re-run the probe. The probe runs once per
/// launcher boot (see lib.rs setup). Calling this in a loop is cheap
/// (single SQL SELECT, ordered by id).
#[command]
pub async fn read_project_folder_missing_flags(
    db: State<'_, Db>,
) -> Result<Vec<ProjectFolderFlag>, String> {
    let rows = db.list_project_folder_paths()?;
    Ok(rows
        .into_iter()
        .map(|(id, folder_path, folder_missing_at_last_boot)| ProjectFolderFlag {
            id,
            folder_path,
            folder_missing_at_last_boot,
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;
    use vct_launcher_core::db::models::ProjectHost;

    /// Open an in-memory Db with migrations applied.
    fn open_db() -> Db {
        Db::open_in_memory().expect("open in-memory db")
    }

    /// Seed one project row with the given id, name, and folder path.
    /// Slug is derived from the name to keep the UNIQUE index happy.
    fn seed_project(db: &Db, id: &str, name: &str, folder_path: &str) {
        db.insert_project(id, name, folder_path, ProjectHost::Base, name)
            .expect("seed project row");
    }

    /// Build a folder-existence check that returns true for paths in
    /// `present_paths` and false otherwise. Captures the set by value
    /// so callers can pass literal slices.
    fn check_with_present(present_paths: &[&str]) -> impl Fn(&str) -> bool {
        let present: HashSet<String> = present_paths.iter().map(|s| s.to_string()).collect();
        move |path: &str| present.contains(path)
    }

    /// A project whose folder doesn't exist on the filesystem gets
    /// flagged on the next probe sweep.
    #[test]
    fn boot_flags_project_with_missing_folder() {
        let db = open_db();
        seed_project(&db, "p1", "Alpha", "/tmp/does-not-exist-12345");

        // Sanity: flag starts at false (DEFAULT 0 from migration 030).
        assert_eq!(
            db.get_project_folder_missing_flag("p1").unwrap(),
            false,
            "fresh row must start with folder_missing flag = false"
        );

        let report = run_folder_probe(&db, check_with_present(&[]));

        assert_eq!(report.newly_missing, 1);
        assert_eq!(report.newly_returned, 0);
        assert_eq!(report.unchanged_healthy, 0);
        assert_eq!(report.unchanged_missing, 0);
        assert!(report.update_errors.is_empty());

        // Flag must now read true.
        assert_eq!(
            db.get_project_folder_missing_flag("p1").unwrap(),
            true,
            "after probe, the row must carry folder_missing = true"
        );
    }

    /// When a project's folder reappears between boots, the flag is
    /// cleared. This is the key idempotency property: a once-broken
    /// project that gets fixed gets surfaced as healthy again on the
    /// next launcher boot, without manual intervention.
    #[test]
    fn boot_clears_flag_when_folder_returns() {
        let db = open_db();
        seed_project(&db, "p1", "Alpha", "/tmp/folder-that-comes-back");

        // First probe: folder absent → flag set.
        let report1 = run_folder_probe(&db, check_with_present(&[]));
        assert_eq!(report1.newly_missing, 1);
        assert_eq!(
            db.get_project_folder_missing_flag("p1").unwrap(),
            true,
            "first probe must set the flag"
        );

        // Second probe: folder present → flag cleared.
        let report2 = run_folder_probe(
            &db,
            check_with_present(&["/tmp/folder-that-comes-back"]),
        );
        assert_eq!(report2.newly_returned, 1);
        assert_eq!(report2.newly_missing, 0);
        assert_eq!(report2.unchanged_healthy, 0);
        assert_eq!(report2.unchanged_missing, 0);
        assert_eq!(
            db.get_project_folder_missing_flag("p1").unwrap(),
            false,
            "second probe must clear the flag once the folder returns"
        );
    }

    /// Healthy projects whose folder is still present don't flip on
    /// re-probe — the report counts them under `unchanged_healthy`
    /// and the DB row is untouched.
    #[test]
    fn boot_leaves_healthy_projects_alone() {
        let db = open_db();
        seed_project(&db, "p1", "Alpha", "/tmp/alpha-folder");
        seed_project(&db, "p2", "Beta", "/tmp/beta-folder");

        let report = run_folder_probe(
            &db,
            check_with_present(&["/tmp/alpha-folder", "/tmp/beta-folder"]),
        );

        assert_eq!(report.newly_missing, 0);
        assert_eq!(report.newly_returned, 0);
        assert_eq!(report.unchanged_healthy, 2);
        assert_eq!(report.unchanged_missing, 0);
        assert_eq!(db.get_project_folder_missing_flag("p1").unwrap(), false);
        assert_eq!(db.get_project_folder_missing_flag("p2").unwrap(), false);
    }

    /// Mixed population: a healthy project + a missing project + a
    /// previously-missing project that's still missing all get
    /// counted correctly without cross-contamination.
    #[test]
    fn boot_mixed_population_categorises_correctly() {
        let db = open_db();
        seed_project(&db, "healthy", "Healthy", "/tmp/healthy-folder");
        seed_project(&db, "gone", "Gone", "/tmp/gone-folder");
        seed_project(&db, "still-gone", "StillGone", "/tmp/still-gone-folder");

        // Pre-set "still-gone" to flagged.
        db.set_project_folder_missing_flag("still-gone", true)
            .unwrap();

        let report = run_folder_probe(&db, check_with_present(&["/tmp/healthy-folder"]));

        assert_eq!(report.unchanged_healthy, 1);
        assert_eq!(report.newly_missing, 1);
        assert_eq!(report.unchanged_missing, 1);
        assert_eq!(report.newly_returned, 0);

        assert_eq!(db.get_project_folder_missing_flag("healthy").unwrap(), false);
        assert_eq!(db.get_project_folder_missing_flag("gone").unwrap(), true);
        assert_eq!(
            db.get_project_folder_missing_flag("still-gone").unwrap(),
            true
        );
    }

    /// An empty folder_path counts as missing (defensive — should not
    /// occur in a well-behaved DB but legacy rows or direct SQL edits
    /// could leave it blank).
    #[test]
    fn boot_treats_empty_folder_path_as_missing() {
        let db = open_db();
        seed_project(&db, "p1", "Alpha", "");

        let report = run_folder_probe(&db, check_with_present(&["/anything"]));

        assert_eq!(report.newly_missing, 1);
        assert_eq!(db.get_project_folder_missing_flag("p1").unwrap(), true);
    }

    /// Empty DB (no projects) probe is a no-op — report carries zero
    /// counts and no errors.
    #[test]
    fn boot_handles_empty_db_gracefully() {
        let db = open_db();
        let report = run_folder_probe(&db, check_with_present(&[]));
        assert_eq!(report, ProbeReport::default());
    }
}
