//! Row-level CRUD for `kg_summaries` table.
//!
//! One row per project tracks the lifecycle of the initial KG-summary
//! backfill run kicked off when the user creates a project (KG summary
//! auto-backfill, v0.2.3 — 2026-05-12). Higher-level orchestration
//! (per-file subprocess spawn, event emission, log capture, progress
//! parsing) lives in `crate::commands::kg_summary`.
//!
//! Mirrors `kg_syncs` (migration 011) in shape — same lifecycle states,
//! same FK cascade, same upsert-on-transition pattern. Kept as a sibling
//! table rather than a polymorphic "background_jobs" table for the same
//! reason `kg_syncs` is: the UI renders the three banners independently
//! (they update at different cadences and their failure modes don't
//! overlap).

use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

/// Lifecycle states for a project's initial KG-summary backfill.
///
/// `pending`  → row inserted, subprocess not yet started.
/// `running`  → walking knowledge/, invoking generate-kg-summary.py per file.
/// `success`  → every node processed (succeeded, unchanged, or skipped-with-reason).
/// `failed`   → fatal pre-flight error (script missing, venv missing) or
///              every node hit the same exception (sub-fatal per-node errors
///              are tolerated — see `kg_summary::run_summary_task` thresholds).
/// `skipped`  → no `.md` files under `knowledge/` to process.
pub mod status {
    pub const PENDING: &str = "pending";
    pub const RUNNING: &str = "running";
    pub const SUCCESS: &str = "success";
    pub const FAILED: &str = "failed";
    pub const SKIPPED: &str = "skipped";
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KgSummaryRow {
    pub project_id: String,
    pub status: String,
    pub started_at: Option<i64>,
    pub finished_at: Option<i64>,
    pub duration_ms: Option<i64>,
    /// Total `.md` files discovered under `knowledge/`.
    pub nodes_total: u32,
    /// Files where the summariser wrote a new entry to `.node_formats.json`.
    pub nodes_succeeded: u32,
    /// Files where the summariser detected an existing hash-match and exited 0.
    pub nodes_unchanged: u32,
    /// Files where the summariser raised an exception (sub-fatal — counted
    /// but the backfill continues with the next file).
    pub nodes_failed: u32,
    /// Files where the summariser exited 0 with "no backend available" or
    /// "no title" (the script's two skip paths). When nodes_skipped equals
    /// nodes_total the run lands in status='skipped' with an actionable
    /// error_message.
    pub nodes_skipped: u32,
    /// Backend the summariser used: "cli" | "ollama" | "api" | "skip" |
    /// "mixed". "mixed" is currently impossible (the backend is cached
    /// per-subprocess) but reserved if we ever invoke the summariser in a
    /// shared sub-shell. Empty = no successful run yet.
    pub backend: Option<String>,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
}

/// Cap stored log_tail at 4 KiB. Matches `kg_syncs::LOG_TAIL_MAX_BYTES`.
/// One node's progress is ~3-5 lines, so a 4 KiB tail covers the last
/// ~20-30 nodes for debugging without bloating SQLite.
pub const LOG_TAIL_MAX_BYTES: usize = 4096;

impl Db {
    /// UPSERT the summary row for a project. Used by every transition
    /// (pending → running → success/failed/skipped). Caller is responsible
    /// for picking sensible field values for each status.
    #[allow(clippy::too_many_arguments)]
    pub fn upsert_kg_summary(
        &self,
        project_id: &str,
        status: &str,
        started_at: Option<i64>,
        finished_at: Option<i64>,
        duration_ms: Option<i64>,
        nodes_total: u32,
        nodes_succeeded: u32,
        nodes_unchanged: u32,
        nodes_failed: u32,
        nodes_skipped: u32,
        backend: Option<&str>,
        error_message: Option<&str>,
        log_tail: Option<&str>,
    ) -> Result<(), String> {
        // Validate status against the CHECK constraint up-front so we
        // get a clear error rather than a SQLite "constraint failed".
        if !matches!(
            status,
            status::PENDING
                | status::RUNNING
                | status::SUCCESS
                | status::FAILED
                | status::SKIPPED
        ) {
            return Err(format!("invalid kg-summary status: {}", status));
        }

        // Defensive: cap log_tail so we never write a huge blob, even if
        // a buggy caller hands us megabytes. Same shape as
        // kg_syncs::upsert_kg_sync.
        let log_tail_capped: Option<String> = log_tail.map(|s| {
            if s.len() <= LOG_TAIL_MAX_BYTES {
                s.to_string()
            } else {
                let cut = floor_char_boundary(s, s.len() - LOG_TAIL_MAX_BYTES);
                format!("…\n{}", &s[cut..])
            }
        });

        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO kg_summaries
                    (project_id, status, started_at, finished_at, duration_ms,
                     nodes_total, nodes_succeeded, nodes_unchanged,
                     nodes_failed, nodes_skipped,
                     backend, error_message, log_tail)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
                 ON CONFLICT(project_id) DO UPDATE SET
                    status          = excluded.status,
                    started_at      = excluded.started_at,
                    finished_at     = excluded.finished_at,
                    duration_ms     = excluded.duration_ms,
                    nodes_total     = excluded.nodes_total,
                    nodes_succeeded = excluded.nodes_succeeded,
                    nodes_unchanged = excluded.nodes_unchanged,
                    nodes_failed    = excluded.nodes_failed,
                    nodes_skipped   = excluded.nodes_skipped,
                    backend         = excluded.backend,
                    error_message   = excluded.error_message,
                    log_tail        = excluded.log_tail",
                params![
                    project_id,
                    status,
                    started_at,
                    finished_at,
                    duration_ms,
                    nodes_total,
                    nodes_succeeded,
                    nodes_unchanged,
                    nodes_failed,
                    nodes_skipped,
                    backend,
                    error_message,
                    log_tail_capped,
                ],
            )
            .map_err(|e| format!("upsert kg_summaries: {}", e))?;
        Ok(())
    }

    pub fn get_kg_summary(&self, project_id: &str) -> Result<Option<KgSummaryRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, status, started_at, finished_at, duration_ms,
                        nodes_total, nodes_succeeded, nodes_unchanged,
                        nodes_failed, nodes_skipped,
                        backend, error_message, log_tail
                 FROM kg_summaries
                 WHERE project_id = ?1",
                params![project_id],
                row_to_summary,
            )
            .optional()
            .map_err(|e| format!("get kg_summary: {}", e))
    }

    /// Project IDs whose most recent recorded status is 'pending'. Used
    /// at launcher startup to recover from a crash that happened between
    /// the `create_project_v2` pending-row insert and the
    /// `spawn_initial_summary` task picking up — the row stays pending
    /// forever otherwise.
    ///
    /// 'running' rows are intentionally NOT included here (same contract
    /// as `list_pending_kg_syncs`): a 'running' row after a launcher
    /// crash is a stale ghost (the subprocess is dead). Use
    /// `list_orphaned_running_kg_summaries` +
    /// `mark_orphaned_running_kg_summaries_failed` for that recovery path
    /// — we surface the crash to the user as a failed state with a Retry
    /// button rather than silently re-spawning, so they see the lifecycle
    /// break.
    pub fn list_pending_kg_summaries(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM kg_summaries
                 WHERE status = 'pending' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list pending kg_summaries: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list pending kg_summaries: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list pending kg_summaries: {}", e))
    }

    /// Project IDs whose most recent recorded status is 'running'.
    /// Exposed for unit-test diagnostics — the production sweep path
    /// (`mark_orphaned_running_kg_summaries_failed`) is a single UPDATE
    /// statement that doesn't need a pre-list. Mirrors
    /// `list_orphaned_running_kg_syncs`.
    #[cfg(test)]
    pub fn list_orphaned_running_kg_summaries(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM kg_summaries
                 WHERE status = 'running' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list running kg_summaries: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list running kg_summaries: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list running kg_summaries: {}", e))
    }

    /// Single-statement update: flip every row currently in status='running'
    /// to status='failed' with a fixed error message + finished_at=now.
    /// Used by the launcher-startup sweep. Returns the number of rows
    /// affected so the caller can log / no-op cleanly.
    ///
    /// The error message is the contract the GUI banner reads in its
    /// "Show details" expansion — keep it terse and action-oriented.
    pub fn mark_orphaned_running_kg_summaries_failed(
        &self,
        error_message: &str,
    ) -> Result<usize, String> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        let affected = guard
            .execute(
                "UPDATE kg_summaries
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
            .map_err(|e| format!("mark orphaned running kg_summaries failed: {}", e))?;
        Ok(affected)
    }
}

fn row_to_summary(row: &rusqlite::Row<'_>) -> rusqlite::Result<KgSummaryRow> {
    Ok(KgSummaryRow {
        project_id: row.get(0)?,
        status: row.get(1)?,
        started_at: row.get(2)?,
        finished_at: row.get(3)?,
        duration_ms: row.get(4)?,
        nodes_total: row.get::<_, i64>(5)? as u32,
        nodes_succeeded: row.get::<_, i64>(6)? as u32,
        nodes_unchanged: row.get::<_, i64>(7)? as u32,
        nodes_failed: row.get::<_, i64>(8)? as u32,
        nodes_skipped: row.get::<_, i64>(9)? as u32,
        backend: row.get(10)?,
        error_message: row.get(11)?,
        log_tail: row.get(12)?,
    })
}

/// std::str::floor_char_boundary is unstable; tiny local replacement.
/// Returns the largest valid char-boundary index `<= idx`. Mirrors the
/// helper in `kg_syncs` / `code_graph_builds`.
fn floor_char_boundary(s: &str, idx: usize) -> usize {
    if idx >= s.len() {
        return s.len();
    }
    let mut i = idx;
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    /// Platform-aware placeholder folder path for fixtures (see
    /// `kg_syncs::tests::fixture_path` for rationale).
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
        let folder = fixture_path("kgsummary-whatever");
        db.insert_project(&id, "Test", &folder, ProjectHost::Base, &slug)
            .unwrap();
        (db, id)
    }

    #[test]
    fn upsert_then_get_round_trips_all_fields() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_summary(
            &pid,
            status::SUCCESS,
            Some(1000),
            Some(4500),
            Some(3500),
            50, 45, 3, 1, 1,
            Some("ollama"),
            None,
            Some("ok"),
        )
        .unwrap();

        let got = db.get_kg_summary(&pid).unwrap().expect("row exists");
        assert_eq!(got.status, "success");
        assert_eq!(got.started_at, Some(1000));
        assert_eq!(got.finished_at, Some(4500));
        assert_eq!(got.duration_ms, Some(3500));
        assert_eq!(got.nodes_total, 50);
        assert_eq!(got.nodes_succeeded, 45);
        assert_eq!(got.nodes_unchanged, 3);
        assert_eq!(got.nodes_failed, 1);
        assert_eq!(got.nodes_skipped, 1);
        assert_eq!(got.backend.as_deref(), Some("ollama"));
        assert_eq!(got.error_message, None);
        assert_eq!(got.log_tail.as_deref(), Some("ok"));
    }

    #[test]
    fn upsert_overwrites_on_state_transition() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_summary(
            &pid, status::PENDING, Some(1), None, None,
            0, 0, 0, 0, 0, None, None, None,
        ).unwrap();
        db.upsert_kg_summary(
            &pid, status::RUNNING, Some(1), None, None,
            50, 5, 0, 0, 0, Some("cli"), None, None,
        ).unwrap();
        db.upsert_kg_summary(
            &pid,
            status::SUCCESS,
            Some(1),
            Some(100),
            Some(99),
            50, 48, 1, 1, 0,
            Some("cli"),
            None,
            Some("done"),
        )
        .unwrap();

        let got = db.get_kg_summary(&pid).unwrap().unwrap();
        assert_eq!(got.status, "success");
        assert_eq!(got.nodes_succeeded, 48);
        assert_eq!(got.backend.as_deref(), Some("cli"));
    }

    #[test]
    fn invalid_status_rejected_with_clear_error() {
        let (db, pid) = fresh_db_with_project();
        let err = db
            .upsert_kg_summary(
                &pid, "borked", None, None, None,
                0, 0, 0, 0, 0, None, None, None,
            )
            .expect_err("must reject");
        assert!(err.contains("borked"));
    }

    #[test]
    fn log_tail_truncated_to_4kb() {
        let (db, pid) = fresh_db_with_project();
        let big = "x".repeat(10_000);
        db.upsert_kg_summary(
            &pid,
            status::SUCCESS,
            Some(1),
            Some(1),
            Some(0),
            0, 0, 0, 0, 0,
            None, None,
            Some(&big),
        )
        .unwrap();
        let got = db.get_kg_summary(&pid).unwrap().unwrap();
        let tail = got.log_tail.unwrap();
        assert!(
            tail.len() <= LOG_TAIL_MAX_BYTES + 8,
            "expected truncation, got {} bytes",
            tail.len()
        );
        assert!(tail.starts_with('…'), "expected leading ellipsis marker");
    }

    #[test]
    fn cascade_delete_removes_summary_row() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_summary(
            &pid, status::PENDING, None, None, None,
            0, 0, 0, 0, 0, None, None, None,
        ).unwrap();
        db.delete_project(&pid).unwrap();
        let got = db.get_kg_summary(&pid).unwrap();
        assert!(got.is_none(), "row should cascade-delete with project");
    }

    // ─── Resume-after-crash helpers (boot-time sweep) ────────────────────

    /// Build a fixture with multiple projects across different summary
    /// states so the list_* helpers can be exercised. Returns
    /// (db, project_ids keyed by status for assertions). Mirrors
    /// `kg_syncs::tests::fresh_db_with_mixed_states`.
    fn fresh_db_with_mixed_states() -> (Db, std::collections::HashMap<&'static str, String>) {
        let db = Db::open_in_memory().expect("in-memory db");
        let mut ids = std::collections::HashMap::new();
        for (idx, (label, st)) in [
            ("pending_a", status::PENDING),
            ("pending_b", status::PENDING),
            ("running_a", status::RUNNING),
            ("success_a", status::SUCCESS),
            ("failed_a", status::FAILED),
            ("skipped_a", status::SKIPPED),
        ]
        .iter()
        .enumerate()
        {
            let id = uuid::Uuid::new_v4().to_string();
            let slug = db.generate_unique_slug(label).unwrap();
            // Distinct folder paths — projects.folder_path is UNIQUE.
            let folder = fixture_path(&format!("summary-mixed-{}", idx));
            db.insert_project(&id, label, &folder, ProjectHost::Base, &slug)
                .unwrap();
            // started_at = idx so the ASC sort is deterministic.
            db.upsert_kg_summary(
                &id,
                st,
                Some(idx as i64),
                None,
                None,
                0, 0, 0, 0, 0,
                None, None, None,
            )
            .unwrap();
            ids.insert(*label, id);
        }
        (db, ids)
    }

    #[test]
    fn list_pending_returns_only_pending_rows_in_started_at_order() {
        let (db, ids) = fresh_db_with_mixed_states();
        let pending = db.list_pending_kg_summaries().unwrap();
        assert_eq!(pending.len(), 2, "exactly two pending rows expected");
        assert_eq!(&pending[0], ids.get("pending_a").unwrap());
        assert_eq!(&pending[1], ids.get("pending_b").unwrap());
        assert!(!pending.contains(ids.get("running_a").unwrap()));
        assert!(!pending.contains(ids.get("success_a").unwrap()));
        assert!(!pending.contains(ids.get("failed_a").unwrap()));
        assert!(!pending.contains(ids.get("skipped_a").unwrap()));
    }

    #[test]
    fn list_orphaned_running_returns_only_running_rows() {
        let (db, ids) = fresh_db_with_mixed_states();
        let running = db.list_orphaned_running_kg_summaries().unwrap();
        assert_eq!(running.len(), 1);
        assert_eq!(&running[0], ids.get("running_a").unwrap());
    }

    #[test]
    fn mark_orphaned_running_flips_status_to_failed_and_sets_error_message() {
        let (db, ids) = fresh_db_with_mixed_states();
        assert_eq!(db.list_orphaned_running_kg_summaries().unwrap().len(), 1);

        let n = db
            .mark_orphaned_running_kg_summaries_failed(
                "launcher crashed mid-run; click Retry to re-run",
            )
            .unwrap();
        assert_eq!(n, 1, "exactly one running row should have been swept");

        let row = db
            .get_kg_summary(ids.get("running_a").unwrap())
            .unwrap()
            .expect("row still exists");
        assert_eq!(row.status, "failed");
        assert!(
            row.error_message
                .as_deref()
                .unwrap_or("")
                .contains("launcher crashed")
        );
        assert!(row.finished_at.is_some(), "finished_at backfilled");

        assert!(db.list_orphaned_running_kg_summaries().unwrap().is_empty());

        // Other states untouched.
        let pending = db.get_kg_summary(ids.get("pending_a").unwrap()).unwrap().unwrap();
        assert_eq!(pending.status, "pending");
        let success = db.get_kg_summary(ids.get("success_a").unwrap()).unwrap().unwrap();
        assert_eq!(success.status, "success");
    }

    #[test]
    fn mark_orphaned_running_is_no_op_when_no_running_rows() {
        let (db, _) = fresh_db_with_project();
        let n = db.mark_orphaned_running_kg_summaries_failed("ignored").unwrap();
        assert_eq!(n, 0);
    }
}
