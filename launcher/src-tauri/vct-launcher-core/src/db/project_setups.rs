//! Row-level CRUD for `project_setups` table.
//!
//! One row per project tracks the lifecycle of the async setup task that
//! `create_project_v2` detaches (Defect B, v0.2.68) — bootstrap-collections
//! + install-bundle + the post-bundle phase. The modal returns FAST once
//! the synchronous phase (DB row + `.claude/env`) is committed; this row +
//! the `project://setup-progress` event drive a global top banner while the
//! heavy phase runs in the background.
//!
//! Higher-level orchestration (the actual `tokio::spawn`, event emission,
//! per-mode phase closures) lives in `crate::commands::project_setup`.
//!
//! Schema mirrors `code_graph_builds` (migration 006) + `kg_syncs`
//! (migration 011) on purpose — same {started,finished}_at, same FK cascade.
//! The two extra terminal states are deliberate:
//!   - `deferred` — a phase deferred cleanly (e.g. Weaviate bootstrap on a
//!     cold backend self-bounds then defers). Informational amber in the
//!     banner; NOT a failure, NO Retry button.
//!   - `failed`   — a genuine subprocess failure. Red banner + Retry.
//!
//! A `pending`/`running` row is ALSO the re-entrancy lock (the row IS the
//! lock — see `setup_in_flight_should_refuse`) and gates the boot-resume
//! sweeps for code-graph / kg-sync / kg-summary so a crash mid-setup can't
//! resurrect the 2026-05-06 spawn-before-bundle race.

use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

/// Lifecycle states for a project's async setup task.
///
/// `pending`  → row inserted, background task not yet picked up.
/// `running`  → background task alive (bootstrap → bundle → post-bundle).
/// `done`     → all phases completed cleanly.
/// `deferred` → a phase deferred cleanly (e.g. cold-Weaviate bootstrap).
///              Terminal + informational; the project still works, the
///              deferred work (collections, indexing) catches up later.
/// `failed`   → a genuine subprocess failure; banner shows Retry.
pub mod status {
    pub const PENDING: &str = "pending";
    pub const RUNNING: &str = "running";
    pub const DONE: &str = "done";
    pub const DEFERRED: &str = "deferred";
    pub const FAILED: &str = "failed";
}

/// Coarse phase labels recorded on the row so the boot-resume sweep + GUI
/// can show where an interrupted setup got to.
pub mod phase {
    pub const BOOTSTRAP: &str = "bootstrap";
    pub const BUNDLE: &str = "bundle";
    pub const POST_BUNDLE: &str = "post_bundle";
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectSetupRow {
    pub project_id: String,
    pub status: String,
    /// Last-known coarse phase ('bootstrap' | 'bundle' | 'post_bundle').
    pub phase: Option<String>,
    pub started_at: Option<i64>,
    pub finished_at: Option<i64>,
    pub duration_ms: Option<i64>,
    /// Warnings collected across all phases (deferral pointers,
    /// preserved-files notices, soft-fail env messages). None when no
    /// warnings were recorded.
    pub warnings: Option<Vec<String>>,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
}

pub use super::log_tail::LOG_TAIL_MAX_BYTES;
use super::log_tail::cap_log_tail;

impl Db {
    /// UPSERT the setup row for a project. Used by every transition
    /// (pending → running → done/deferred/failed). Caller is responsible
    /// for picking sensible field values for each status.
    ///
    /// `warnings` is serialized to a JSON array in the `warnings` column.
    /// None stays SQL NULL. Same for `phase`, `error_message`, `log_tail`.
    #[allow(clippy::too_many_arguments)]
    pub fn upsert_project_setup(
        &self,
        project_id: &str,
        status: &str,
        phase: Option<&str>,
        started_at: Option<i64>,
        finished_at: Option<i64>,
        duration_ms: Option<i64>,
        warnings: Option<&[String]>,
        error_message: Option<&str>,
        log_tail: Option<&str>,
    ) -> Result<(), String> {
        // Validate status against the CHECK constraint up-front so we get a
        // clear error rather than a SQLite "constraint failed".
        if !matches!(
            status,
            status::PENDING
                | status::RUNNING
                | status::DONE
                | status::DEFERRED
                | status::FAILED
        ) {
            return Err(format!("invalid project-setup status: {}", status));
        }

        let warnings_json: Option<String> = warnings.map(|w| {
            serde_json::to_string(w).unwrap_or_else(|_| "[]".to_string())
        });
        let log_tail_capped: Option<String> = log_tail.map(cap_log_tail);

        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_setups
                    (project_id, status, phase, started_at, finished_at,
                     duration_ms, warnings, error_message, log_tail)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
                 ON CONFLICT(project_id) DO UPDATE SET
                    status        = excluded.status,
                    phase         = excluded.phase,
                    started_at    = excluded.started_at,
                    finished_at   = excluded.finished_at,
                    duration_ms   = excluded.duration_ms,
                    warnings      = excluded.warnings,
                    error_message = excluded.error_message,
                    log_tail      = excluded.log_tail",
                params![
                    project_id,
                    status,
                    phase,
                    started_at,
                    finished_at,
                    duration_ms,
                    warnings_json,
                    error_message,
                    log_tail_capped,
                ],
            )
            .map_err(|e| format!("upsert project_setups: {}", e))?;
        Ok(())
    }

    pub fn get_project_setup(
        &self,
        project_id: &str,
    ) -> Result<Option<ProjectSetupRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, status, phase, started_at, finished_at,
                        duration_ms, warnings, error_message, log_tail
                 FROM project_setups
                 WHERE project_id = ?1",
                params![project_id],
                row_to_setup,
            )
            .optional()
            .map_err(|e| format!("get project_setup: {}", e))
    }

    /// Project IDs whose most recent recorded status is 'pending'. Used at
    /// startup to recover from a launcher crash that happened after the
    /// row was inserted but before the background task picked it up.
    ///
    /// Note: 'running' is intentionally NOT included here — a 'running' row
    /// after a launcher crash is a stale ghost (the subprocess died with
    /// the launcher). The boot sweep flips such rows to 'failed' via
    /// `mark_orphaned_running_project_setups_failed` so the GUI shows the
    /// broken lifecycle with a Retry button instead of a silent re-spawn.
    pub fn list_pending_project_setups(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM project_setups
                 WHERE status = 'pending' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list pending project_setups: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list pending project_setups: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list pending project_setups: {}", e))
    }

    /// Project IDs whose setup row is NOT in a terminal state
    /// ('done' | 'deferred' | 'failed'). Used by the boot-resume sweep to
    /// GATE the code-graph / kg-sync / kg-summary resume sweeps: a project
    /// whose setup never finished must NOT have those sweeps re-spawn
    /// against it (the bundle may not be on disk → the 2026-05-06
    /// spawn-before-bundle race). 'pending' + 'running' both count as
    /// incomplete here.
    pub fn list_incomplete_project_setups(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM project_setups
                 WHERE status NOT IN ('done','deferred','failed')",
            )
            .map_err(|e| format!("prepare list incomplete project_setups: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list incomplete project_setups: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list incomplete project_setups: {}", e))
    }

    /// Project IDs whose status is 'running'. Test-only diagnostic, mirrors
    /// `list_orphaned_running_code_graph_builds`.
    #[cfg(test)]
    pub fn list_orphaned_running_project_setups(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM project_setups
                 WHERE status = 'running' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list running project_setups: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list running project_setups: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list running project_setups: {}", e))
    }

    /// Single-statement update: flip every row currently in status='running'
    /// to status='failed' with a fixed error message + finished_at=now.
    /// Used by the launcher-startup sweep. Returns rows-affected.
    ///
    /// Mirrors `Db::mark_orphaned_running_code_graph_builds_failed`.
    pub fn mark_orphaned_running_project_setups_failed(
        &self,
        error_message: &str,
    ) -> Result<usize, String> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        let affected = guard
            .execute(
                "UPDATE project_setups
                    SET status = 'failed',
                        finished_at = ?1,
                        duration_ms = CASE
                            WHEN started_at IS NOT NULL THEN ?1 - started_at
                            ELSE NULL
                        END,
                        error_message = ?2
                  WHERE status = 'running'",
                params![now_ms, error_message],
            )
            .map_err(|e| format!("mark orphaned running project_setups failed: {}", e))?;
        Ok(affected)
    }
}

fn row_to_setup(row: &rusqlite::Row<'_>) -> rusqlite::Result<ProjectSetupRow> {
    let warnings_json: Option<String> = row.get(6)?;
    let warnings: Option<Vec<String>> = warnings_json
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok());
    Ok(ProjectSetupRow {
        project_id: row.get(0)?,
        status: row.get(1)?,
        phase: row.get(2)?,
        started_at: row.get(3)?,
        finished_at: row.get(4)?,
        duration_ms: row.get(5)?,
        warnings,
        error_message: row.get(7)?,
        log_tail: row.get(8)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn fixture_path(suffix: &str) -> String {
        if cfg!(windows) {
            format!(r"C:\tmp\{}", suffix)
        } else {
            format!("/tmp/{}", suffix)
        }
    }

    fn fresh_db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = uuid::Uuid::new_v4().to_string();
        let slug = db.generate_unique_slug("Test").unwrap();
        let folder = fixture_path("setup-whatever");
        db.insert_project(&id, "Test", &folder, ProjectHost::Base, &slug)
            .unwrap();
        (db, id)
    }

    #[test]
    fn upsert_then_get_round_trips_all_fields() {
        let (db, pid) = fresh_db_with_project();
        let warns = vec!["bootstrap deferred".to_string(), "preserved 2".to_string()];
        db.upsert_project_setup(
            &pid,
            status::DEFERRED,
            Some(phase::BOOTSTRAP),
            Some(1000),
            Some(2500),
            Some(1500),
            Some(&warns),
            None,
            None,
        )
        .unwrap();

        let got = db.get_project_setup(&pid).unwrap().expect("row exists");
        assert_eq!(got.status, "deferred");
        assert_eq!(got.phase.as_deref(), Some("bootstrap"));
        assert_eq!(got.started_at, Some(1000));
        assert_eq!(got.finished_at, Some(2500));
        assert_eq!(got.duration_ms, Some(1500));
        assert_eq!(
            got.warnings.as_deref().unwrap(),
            ["bootstrap deferred", "preserved 2"]
        );
        assert_eq!(got.error_message, None);
    }

    #[test]
    fn upsert_overwrites_on_state_transition() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_project_setup(&pid, status::PENDING, None, Some(1), None, None, None, None, None)
            .unwrap();
        db.upsert_project_setup(
            &pid,
            status::RUNNING,
            Some(phase::BUNDLE),
            Some(1),
            None,
            None,
            None,
            None,
            None,
        )
        .unwrap();
        db.upsert_project_setup(
            &pid,
            status::DONE,
            Some(phase::POST_BUNDLE),
            Some(1),
            Some(100),
            Some(99),
            None,
            None,
            None,
        )
        .unwrap();

        let got = db.get_project_setup(&pid).unwrap().unwrap();
        assert_eq!(got.status, "done");
        assert_eq!(got.phase.as_deref(), Some("post_bundle"));
    }

    #[test]
    fn invalid_status_rejected_with_clear_error() {
        let (db, pid) = fresh_db_with_project();
        let err = db
            .upsert_project_setup(&pid, "borked", None, None, None, None, None, None, None)
            .expect_err("must reject");
        assert!(err.contains("borked"));
    }

    fn fresh_db_with_mixed_setup_states() -> (Db, std::collections::HashMap<&'static str, String>) {
        let db = Db::open_in_memory().expect("in-memory db");
        let mut ids = std::collections::HashMap::new();
        for (idx, (label, st)) in [
            ("pending_a", status::PENDING),
            ("pending_b", status::PENDING),
            ("running_a", status::RUNNING),
            ("done_a", status::DONE),
            ("deferred_a", status::DEFERRED),
            ("failed_a", status::FAILED),
        ]
        .iter()
        .enumerate()
        {
            let id = uuid::Uuid::new_v4().to_string();
            let slug = db.generate_unique_slug(label).unwrap();
            let folder = fixture_path(&format!("setup-mixed-{}", idx));
            db.insert_project(&id, label, &folder, ProjectHost::Base, &slug)
                .unwrap();
            db.upsert_project_setup(&id, st, None, Some(idx as i64), None, None, None, None, None)
                .unwrap();
            ids.insert(*label, id);
        }
        (db, ids)
    }

    #[test]
    fn list_pending_returns_only_pending_rows() {
        let (db, _ids) = fresh_db_with_mixed_setup_states();
        let pending = db.list_pending_project_setups().unwrap();
        assert_eq!(pending.len(), 2);
    }

    #[test]
    fn a2_2_early_claimed_setup_is_resumable_after_crash_in_sync_phase() {
        // A2.2 (v0.2.75): `create_project_v2` now claims a PENDING
        // `project_setups` row IMMEDIATELY after `insert_project`, BEFORE the
        // synchronous env/populate/B12 phase. This models a launcher crash in
        // that phase: the project row + a PENDING setup row exist, but the
        // heavy phase (bootstrap/bundle/post-bundle) never ran. The boot-resume
        // sweep (`resume_pending_setups`) iterates `list_pending_project_setups`
        // and re-spawns each, so the setup MUST be discoverable as pending.
        //
        // ACT: the setup row exists immediately after insert (before any phase).
        let (db, pid) = fresh_db_with_project();
        db.upsert_project_setup(
            &pid,
            status::PENDING,
            None,          // phase: none — the heavy phase never started
            Some(1000),    // started_at
            None,
            None,
            None,
            None,
            None,
        )
        .unwrap();

        let row = db.get_project_setup(&pid).unwrap().expect("early claim row");
        assert_eq!(row.status, "pending");
        assert_eq!(row.phase, None, "no phase should have run before the crash");
        assert_eq!(row.finished_at, None);

        // SWEEP: the crash-window row is picked up by the boot-resume list.
        let pending = db.list_pending_project_setups().unwrap();
        assert!(
            pending.contains(&pid),
            "an early-claimed setup that crashed before any phase must be \
             resumable via list_pending_project_setups; got {:?}",
            pending
        );

        // Idempotent re-affirm (the pre-spawn UPSERT) keeps it pending — it must
        // NOT flip out of the resumable state.
        db.upsert_project_setup(
            &pid,
            status::PENDING,
            None,
            Some(2000),
            None,
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert!(
            db.list_pending_project_setups().unwrap().contains(&pid),
            "re-affirming PENDING must keep the row resumable"
        );
    }

    #[test]
    fn list_incomplete_returns_pending_and_running_only() {
        let (db, ids) = fresh_db_with_mixed_setup_states();
        let mut incomplete = db.list_incomplete_project_setups().unwrap();
        incomplete.sort();
        let mut expected = vec![
            ids.get("pending_a").unwrap().clone(),
            ids.get("pending_b").unwrap().clone(),
            ids.get("running_a").unwrap().clone(),
        ];
        expected.sort();
        assert_eq!(incomplete, expected);
        // done / deferred / failed are terminal → NOT incomplete.
        assert!(!incomplete.contains(ids.get("done_a").unwrap()));
        assert!(!incomplete.contains(ids.get("deferred_a").unwrap()));
        assert!(!incomplete.contains(ids.get("failed_a").unwrap()));
    }

    #[test]
    fn mark_orphaned_running_flips_to_failed() {
        let (db, ids) = fresh_db_with_mixed_setup_states();
        assert_eq!(db.list_orphaned_running_project_setups().unwrap().len(), 1);

        let n = db
            .mark_orphaned_running_project_setups_failed(
                "launcher crashed mid-setup; click Retry to re-run",
            )
            .unwrap();
        assert_eq!(n, 1);

        let row = db
            .get_project_setup(ids.get("running_a").unwrap())
            .unwrap()
            .expect("row still exists");
        assert_eq!(row.status, "failed");
        assert!(row
            .error_message
            .as_deref()
            .unwrap_or("")
            .contains("launcher crashed"));
        assert!(row.finished_at.is_some());

        // Terminal + pending states left untouched.
        let pending = db.get_project_setup(ids.get("pending_a").unwrap()).unwrap().unwrap();
        assert_eq!(pending.status, "pending");
        let done = db.get_project_setup(ids.get("done_a").unwrap()).unwrap().unwrap();
        assert_eq!(done.status, "done");
        let deferred = db.get_project_setup(ids.get("deferred_a").unwrap()).unwrap().unwrap();
        assert_eq!(deferred.status, "deferred");
        assert!(db.list_orphaned_running_project_setups().unwrap().is_empty());
    }

    #[test]
    fn mark_orphaned_running_no_op_when_empty() {
        let (db, _) = fresh_db_with_project();
        let n = db
            .mark_orphaned_running_project_setups_failed("ignored")
            .unwrap();
        assert_eq!(n, 0);
    }

    #[test]
    fn cascade_delete_removes_setup_row() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_project_setup(&pid, status::PENDING, None, None, None, None, None, None, None)
            .unwrap();
        db.delete_project(&pid).unwrap();
        let got = db.get_project_setup(&pid).unwrap();
        assert!(got.is_none(), "row should cascade-delete with project");
    }

    #[test]
    fn warnings_round_trip_empty_vec() {
        let (db, pid) = fresh_db_with_project();
        let empty: Vec<String> = vec![];
        db.upsert_project_setup(
            &pid,
            status::DONE,
            None,
            Some(1),
            Some(2),
            Some(1),
            Some(&empty),
            None,
            None,
        )
        .unwrap();
        let got = db.get_project_setup(&pid).unwrap().unwrap();
        assert_eq!(got.warnings.as_deref().unwrap().len(), 0);
    }
}
