//! Row-level CRUD for `kg_syncs` table.
//!
//! One row per project tracks the lifecycle of the initial KG-sync run
//! kicked off when the user creates a project (KG auto-sync — 2026-05-12).
//! Higher-level orchestration (subprocess spawn, event emission, log
//! capture, progress parsing) lives in `crate::commands::kg_sync`.
//!
//! Mirrors `code_graph_builds` (migration 006) in shape — same lifecycle
//! states, same FK cascade, same upsert-on-transition pattern. Kept as a
//! sibling table rather than a polymorphic "background_jobs" table so the
//! UI can render the two pills independently (they update at different
//! cadences and their failure modes don't overlap).

use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

/// Lifecycle states for a project's initial KG / docs sync.
///
/// `pending`  → row inserted, subprocess not yet started.
/// `running`  → subprocess alive, files being embedded.
/// `success`  → subprocess exited 0, rows in Weaviate.
/// `failed`   → subprocess exited non-zero or panicked.
/// `skipped`  → no `.md` files in `knowledge/` or `docs/`, nothing to do.
pub mod status {
    pub const PENDING: &str = "pending";
    pub const RUNNING: &str = "running";
    pub const SUCCESS: &str = "success";
    pub const FAILED: &str = "failed";
    pub const SKIPPED: &str = "skipped";
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KgSyncRow {
    pub project_id: String,
    pub status: String,
    pub started_at: Option<i64>,
    pub finished_at: Option<i64>,
    pub duration_ms: Option<i64>,
    /// "📚 Found N markdown files in knowledge/" — set when scan completes.
    pub kg_total: u32,
    pub kg_succeeded: u32,
    pub kg_failed: u32,
    /// "📚 Found N markdown files in docs/" — set when scan completes.
    pub docs_total: u32,
    pub docs_succeeded: u32,
    pub docs_failed: u32,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
}

/// Cap stored log_tail at 4 KiB. Matches `code_graph_builds::LOG_TAIL_MAX_BYTES`.
/// The sync subprocess emits one progress line per node, so a 4 KiB tail
/// covers the last ~30-50 nodes for debugging without bloating SQLite.
pub const LOG_TAIL_MAX_BYTES: usize = 4096;

impl Db {
    /// UPSERT the sync row for a project. Used by every transition
    /// (pending → running → success/failed/skipped). Caller is
    /// responsible for picking sensible field values for each status.
    #[allow(clippy::too_many_arguments)]
    pub fn upsert_kg_sync(
        &self,
        project_id: &str,
        status: &str,
        started_at: Option<i64>,
        finished_at: Option<i64>,
        duration_ms: Option<i64>,
        kg_total: u32,
        kg_succeeded: u32,
        kg_failed: u32,
        docs_total: u32,
        docs_succeeded: u32,
        docs_failed: u32,
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
            return Err(format!("invalid kg-sync status: {}", status));
        }

        // Defensive: cap log_tail so we never write a huge blob, even if
        // a buggy caller hands us megabytes. Same shape as
        // code_graph_builds::upsert_code_graph_build.
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
                "INSERT INTO kg_syncs
                    (project_id, status, started_at, finished_at, duration_ms,
                     kg_total, kg_succeeded, kg_failed,
                     docs_total, docs_succeeded, docs_failed,
                     error_message, log_tail)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
                 ON CONFLICT(project_id) DO UPDATE SET
                    status         = excluded.status,
                    started_at     = excluded.started_at,
                    finished_at    = excluded.finished_at,
                    duration_ms    = excluded.duration_ms,
                    kg_total       = excluded.kg_total,
                    kg_succeeded   = excluded.kg_succeeded,
                    kg_failed      = excluded.kg_failed,
                    docs_total     = excluded.docs_total,
                    docs_succeeded = excluded.docs_succeeded,
                    docs_failed    = excluded.docs_failed,
                    error_message  = excluded.error_message,
                    log_tail       = excluded.log_tail",
                params![
                    project_id,
                    status,
                    started_at,
                    finished_at,
                    duration_ms,
                    kg_total,
                    kg_succeeded,
                    kg_failed,
                    docs_total,
                    docs_succeeded,
                    docs_failed,
                    error_message,
                    log_tail_capped,
                ],
            )
            .map_err(|e| format!("upsert kg_syncs: {}", e))?;
        Ok(())
    }

    pub fn get_kg_sync(&self, project_id: &str) -> Result<Option<KgSyncRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, status, started_at, finished_at, duration_ms,
                        kg_total, kg_succeeded, kg_failed,
                        docs_total, docs_succeeded, docs_failed,
                        error_message, log_tail
                 FROM kg_syncs
                 WHERE project_id = ?1",
                params![project_id],
                row_to_sync,
            )
            .optional()
            .map_err(|e| format!("get kg_sync: {}", e))
    }

    /// Project IDs whose most recent recorded status is 'pending'. Used
    /// at launcher startup to recover from a crash that happened between
    /// the `create_project_v2` pending-row insert and the
    /// `spawn_initial_sync` task picking up — the row stays pending
    /// forever otherwise.
    ///
    /// 'running' rows are intentionally NOT included here (same contract
    /// as `list_pending_code_graph_builds`): a 'running' row after a
    /// launcher crash is a stale ghost (the subprocess is dead). Use
    /// `list_orphaned_running_kg_syncs` + `mark_orphaned_running_kg_syncs_failed`
    /// for that recovery path — we surface the crash to the user as a
    /// failed state with a Retry button rather than silently re-spawning,
    /// so they see the lifecycle break.
    pub fn list_pending_kg_syncs(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM kg_syncs
                 WHERE status = 'pending' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list pending kg_syncs: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list pending kg_syncs: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list pending kg_syncs: {}", e))
    }

    /// Project IDs whose most recent recorded status is 'running'.
    /// Exposed for unit-test diagnostics — the production sweep path
    /// (`mark_orphaned_running_kg_syncs_failed`) is a single UPDATE
    /// statement that doesn't need a pre-list. Used by the resume-after-
    /// crash tests to assert that a fixture row in 'running' state is
    /// visible to the sweep before/after marking it failed.
    #[cfg(test)]
    pub fn list_orphaned_running_kg_syncs(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM kg_syncs
                 WHERE status = 'running' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list running kg_syncs: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list running kg_syncs: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list running kg_syncs: {}", e))
    }

    /// Single-statement update: flip every row currently in status='running'
    /// to status='failed' with a fixed error message + finished_at=now.
    /// Used by the launcher-startup sweep. Returns the number of rows
    /// affected so the caller can log / no-op cleanly.
    ///
    /// The error message is the contract the GUI banner reads in its
    /// "Show details" expansion — keep it terse and action-oriented.
    pub fn mark_orphaned_running_kg_syncs_failed(
        &self,
        error_message: &str,
    ) -> Result<usize, String> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        let affected = guard
            .execute(
                "UPDATE kg_syncs
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
            .map_err(|e| format!("mark orphaned running kg_syncs failed: {}", e))?;
        Ok(affected)
    }
}

fn row_to_sync(row: &rusqlite::Row<'_>) -> rusqlite::Result<KgSyncRow> {
    Ok(KgSyncRow {
        project_id: row.get(0)?,
        status: row.get(1)?,
        started_at: row.get(2)?,
        finished_at: row.get(3)?,
        duration_ms: row.get(4)?,
        kg_total: row.get::<_, i64>(5)? as u32,
        kg_succeeded: row.get::<_, i64>(6)? as u32,
        kg_failed: row.get::<_, i64>(7)? as u32,
        docs_total: row.get::<_, i64>(8)? as u32,
        docs_succeeded: row.get::<_, i64>(9)? as u32,
        docs_failed: row.get::<_, i64>(10)? as u32,
        error_message: row.get(11)?,
        log_tail: row.get(12)?,
    })
}

/// std::str::floor_char_boundary is unstable; tiny local replacement.
/// Returns the largest valid char-boundary index `<= idx`. Mirrors the
/// helper in `code_graph_builds` rather than factoring a shared util —
/// the two modules are independent and a shared util would have to live
/// in a top-level place neither currently imports from.
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
    /// `code_graph_builds::tests::fixture_path` for rationale).
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
        let folder = fixture_path("kgsync-whatever");
        db.insert_project(&id, "Test", &folder, ProjectHost::Base, &slug)
            .unwrap();
        (db, id)
    }

    #[test]
    fn upsert_then_get_round_trips_all_fields() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_sync(
            &pid,
            status::SUCCESS,
            Some(1000),
            Some(2500),
            Some(1500),
            50, 48, 2,   // kg
            12, 12, 0,   // docs
            None,
            Some("ok"),
        )
        .unwrap();

        let got = db.get_kg_sync(&pid).unwrap().expect("row exists");
        assert_eq!(got.status, "success");
        assert_eq!(got.started_at, Some(1000));
        assert_eq!(got.finished_at, Some(2500));
        assert_eq!(got.duration_ms, Some(1500));
        assert_eq!(got.kg_total, 50);
        assert_eq!(got.kg_succeeded, 48);
        assert_eq!(got.kg_failed, 2);
        assert_eq!(got.docs_total, 12);
        assert_eq!(got.docs_succeeded, 12);
        assert_eq!(got.docs_failed, 0);
        assert_eq!(got.error_message, None);
        assert_eq!(got.log_tail.as_deref(), Some("ok"));
    }

    #[test]
    fn upsert_overwrites_on_state_transition() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_sync(&pid, status::PENDING, Some(1), None, None, 0, 0, 0, 0, 0, 0, None, None)
            .unwrap();
        db.upsert_kg_sync(&pid, status::RUNNING, Some(1), None, None, 50, 5, 0, 0, 0, 0, None, None)
            .unwrap();
        db.upsert_kg_sync(
            &pid,
            status::SUCCESS,
            Some(1),
            Some(100),
            Some(99),
            50, 50, 0,
            10, 10, 0,
            None,
            Some("done"),
        )
        .unwrap();

        let got = db.get_kg_sync(&pid).unwrap().unwrap();
        assert_eq!(got.status, "success");
        assert_eq!(got.kg_succeeded, 50);
        assert_eq!(got.docs_succeeded, 10);
    }

    #[test]
    fn invalid_status_rejected_with_clear_error() {
        let (db, pid) = fresh_db_with_project();
        let err = db
            .upsert_kg_sync(&pid, "borked", None, None, None, 0, 0, 0, 0, 0, 0, None, None)
            .expect_err("must reject");
        assert!(err.contains("borked"));
    }

    #[test]
    fn log_tail_truncated_to_4kb() {
        let (db, pid) = fresh_db_with_project();
        let big = "x".repeat(10_000);
        db.upsert_kg_sync(
            &pid,
            status::SUCCESS,
            Some(1),
            Some(1),
            Some(0),
            0, 0, 0, 0, 0, 0,
            None,
            Some(&big),
        )
        .unwrap();
        let got = db.get_kg_sync(&pid).unwrap().unwrap();
        let tail = got.log_tail.unwrap();
        assert!(
            tail.len() <= LOG_TAIL_MAX_BYTES + 8,
            "expected truncation, got {} bytes",
            tail.len()
        );
        assert!(tail.starts_with('…'), "expected leading ellipsis marker");
    }

    #[test]
    fn cascade_delete_removes_sync_row() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_sync(&pid, status::PENDING, None, None, None, 0, 0, 0, 0, 0, 0, None, None)
            .unwrap();
        db.delete_project(&pid).unwrap();
        let got = db.get_kg_sync(&pid).unwrap();
        assert!(got.is_none(), "row should cascade-delete with project");
    }

    // ─── Resume-after-crash helpers (boot-time sweep) ────────────────────

    /// Build a fixture with multiple projects across different sync states
    /// so the list_* helpers can be exercised. Returns (db, project_ids
    /// keyed by status for assertions).
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
            let folder = fixture_path(&format!("mixed-{}", idx));
            db.insert_project(&id, label, &folder, ProjectHost::Base, &slug)
                .unwrap();
            // started_at = idx so the ASC sort is deterministic.
            db.upsert_kg_sync(
                &id,
                st,
                Some(idx as i64),
                None,
                None,
                0, 0, 0, 0, 0, 0,
                None,
                None,
            )
            .unwrap();
            ids.insert(*label, id);
        }
        (db, ids)
    }

    #[test]
    fn list_pending_returns_only_pending_rows_in_started_at_order() {
        let (db, ids) = fresh_db_with_mixed_states();
        let pending = db.list_pending_kg_syncs().unwrap();
        // pending_a inserted at idx=0, pending_b at idx=1 → ASC: [a, b].
        assert_eq!(pending.len(), 2, "exactly two pending rows expected");
        assert_eq!(&pending[0], ids.get("pending_a").unwrap());
        assert_eq!(&pending[1], ids.get("pending_b").unwrap());
        // None of the other states leaked in.
        assert!(!pending.contains(ids.get("running_a").unwrap()));
        assert!(!pending.contains(ids.get("success_a").unwrap()));
        assert!(!pending.contains(ids.get("failed_a").unwrap()));
        assert!(!pending.contains(ids.get("skipped_a").unwrap()));
    }

    #[test]
    fn list_orphaned_running_returns_only_running_rows() {
        let (db, ids) = fresh_db_with_mixed_states();
        let running = db.list_orphaned_running_kg_syncs().unwrap();
        assert_eq!(running.len(), 1);
        assert_eq!(&running[0], ids.get("running_a").unwrap());
    }

    #[test]
    fn mark_orphaned_running_flips_status_to_failed_and_sets_error_message() {
        let (db, ids) = fresh_db_with_mixed_states();
        // Pre-condition: one running row.
        assert_eq!(db.list_orphaned_running_kg_syncs().unwrap().len(), 1);

        let n = db
            .mark_orphaned_running_kg_syncs_failed("launcher crashed mid-run; click Retry to re-run")
            .unwrap();
        assert_eq!(n, 1, "exactly one running row should have been swept");

        // The previously-running row is now failed with the message.
        let row = db
            .get_kg_sync(ids.get("running_a").unwrap())
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

        // No more orphans.
        assert!(db.list_orphaned_running_kg_syncs().unwrap().is_empty());

        // Other states untouched.
        let pending = db.get_kg_sync(ids.get("pending_a").unwrap()).unwrap().unwrap();
        assert_eq!(pending.status, "pending");
        let success = db.get_kg_sync(ids.get("success_a").unwrap()).unwrap().unwrap();
        assert_eq!(success.status, "success");
    }

    #[test]
    fn mark_orphaned_running_is_no_op_when_no_running_rows() {
        let (db, _) = fresh_db_with_project();
        // No rows at all.
        let n = db.mark_orphaned_running_kg_syncs_failed("ignored").unwrap();
        assert_eq!(n, 0);
    }
}
