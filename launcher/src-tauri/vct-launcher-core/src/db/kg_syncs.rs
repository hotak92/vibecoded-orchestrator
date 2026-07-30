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
    /// BUG 2 (v0.2.89, migration 041): ms-since-epoch stamp the RUNNING
    /// task's 60 s ticker writes (`touch_kg_sync_heartbeat`). Liveness of
    /// the launcher TASK, not subprocess progress — the stall watchdog
    /// owns that. NULL = no tick yet (fresh RUNNING row before the first
    /// tick, terminal rows, or pre-migration legacy rows); the staleness
    /// predicate falls back to `started_at` for those.
    pub heartbeat_at: Option<i64>,
}

/// BUG 2 (v0.2.89): pure staleness predicate shared by the read-time
/// guards in `commands::{kg_sync,codegraph}` — MUST match the SQL
/// predicate in `mark_stale_running_kg_syncs_failed` /
/// `mark_stale_running_code_graph_builds_failed`
/// (`COALESCE(heartbeat_at, started_at, 0) < now - stale_secs*1000`).
/// Callers additionally gate on status=='running' (and pid IS NULL for
/// the code-graph twin); this function only answers "is the liveness
/// stamp older than the window?".
pub fn heartbeat_is_stale(
    heartbeat_at: Option<i64>,
    started_at: Option<i64>,
    now_ms: i64,
    stale_secs: u64,
) -> bool {
    let last_alive = heartbeat_at.or(started_at).unwrap_or(0);
    let cutoff = now_ms.saturating_sub((stale_secs as i64).saturating_mul(1000));
    last_alive < cutoff
}

/// Cap stored log_tail at 4 KiB. (v0.2.54 Track J: re-exported from
/// the shared `db::log_tail` module — was a per-file const
/// triplicated across the three log-writing db modules.)
pub use super::log_tail::LOG_TAIL_MAX_BYTES;
use super::log_tail::cap_log_tail;

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
        let log_tail_capped: Option<String> = log_tail.map(cap_log_tail);

        let guard = self.lock();
        // BUG 2 (v0.2.89): every lifecycle transition CLEARS heartbeat_at
        // (INSERT NULL / SET NULL). A retry re-queues the row as 'pending'
        // with a FRESH started_at; carrying the previous run's stale
        // heartbeat across that transition would make the new RUNNING row
        // look stale to `COALESCE(heartbeat_at, started_at, 0)` before its
        // first tick lands. Mirrors code_graph_builds' pid-clear rationale.
        guard
            .execute(
                "INSERT INTO kg_syncs
                    (project_id, status, started_at, finished_at, duration_ms,
                     kg_total, kg_succeeded, kg_failed,
                     docs_total, docs_succeeded, docs_failed,
                     error_message, log_tail, heartbeat_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, NULL)
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
                    log_tail       = excluded.log_tail,
                    heartbeat_at   = NULL",
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
                        error_message, log_tail, heartbeat_at
                 FROM kg_syncs
                 WHERE project_id = ?1",
                params![project_id],
                row_to_sync,
            )
            .optional()
            .map_err(|e| format!("get kg_sync: {}", e))
    }

    /// BUG 2 (v0.2.89): stamp `heartbeat_at = now` on this project's row
    /// — status-guarded so a row that already reached a terminal state is
    /// NEVER touched (the ticker races the terminal upsert by design; the
    /// guard makes the post-terminal tick a no-op). Returns rows affected
    /// (0 = row absent or no longer RUNNING).
    pub fn touch_kg_sync_heartbeat(&self, project_id: &str) -> Result<usize, String> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "UPDATE kg_syncs SET heartbeat_at = ?1
                  WHERE project_id = ?2 AND status = 'running'",
                params![now_ms, project_id],
            )
            .map_err(|e| format!("touch kg_sync heartbeat: {}", e))
    }

    /// BUG 2 (v0.2.89): flip RUNNING rows whose liveness stamp is older
    /// than `stale_secs` to 'failed'. The SQL predicate MUST match the
    /// pure `heartbeat_is_stale` helper above (read-time guards use that
    /// to decide whether to invoke this). `only_project = Some(id)` scopes
    /// the flip to one row (the read-time guard path); `None` sweeps every
    /// stale row (the 5-min sweeper).
    ///
    /// This is a LIVENESS reconcile, not a duration cap: a live task
    /// stamps `heartbeat_at` every 60 s regardless of how slow the
    /// subprocess is, so only a dead task (or a launcher that never came
    /// back) can age past the window. Fresh RUNNING rows, PENDING rows,
    /// and every terminal state are untouched by construction of the
    /// WHERE clause.
    pub fn mark_stale_running_kg_syncs_failed(
        &self,
        stale_secs: u64,
        error_message: &str,
        only_project: Option<&str>,
    ) -> Result<usize, String> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let cutoff_ms = now_ms.saturating_sub((stale_secs as i64).saturating_mul(1000));
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
                  WHERE status = 'running'
                    AND COALESCE(heartbeat_at, started_at, 0) < ?3
                    AND (?4 IS NULL OR project_id = ?4)",
                params![now_ms, error_message, cutoff_ms, only_project],
            )
            .map_err(|e| format!("mark stale running kg_syncs failed: {}", e))?;
        Ok(affected)
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
        heartbeat_at: row.get(13)?,
    })
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

    // ─── BUG 2 (v0.2.89): heartbeat liveness ─────────────────────────────

    const STALE_SECS: u64 = 1800;
    const STALE_MSG: &str = "sync task died without reporting (heartbeat stale)";

    /// Backdate liveness stamps directly (the public writers only stamp
    /// wall-clock now, so tests need raw SQL to construct stale rows).
    fn backdate(db: &Db, project_id: &str, heartbeat_at: Option<i64>, started_at: Option<i64>) {
        db.lock()
            .execute(
                "UPDATE kg_syncs SET heartbeat_at = ?1, started_at = ?2 WHERE project_id = ?3",
                params![heartbeat_at, started_at, project_id],
            )
            .unwrap();
    }

    fn insert_sync_project(db: &Db, label: &str, status: &str) -> String {
        let id = uuid::Uuid::new_v4().to_string();
        let slug = db.generate_unique_slug(label).unwrap();
        db.insert_project(
            &id,
            label,
            &fixture_path(&format!("hb-{}", label)),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();
        db.upsert_kg_sync(&id, status, Some(0), None, None, 0, 0, 0, 0, 0, 0, None, None)
            .unwrap();
        id
    }

    #[test]
    fn heartbeat_is_stale_predicate_matrix() {
        let now = 10_000_000i64;
        let window = STALE_SECS; // 1800 s → 1_800_000 ms
        // Fresh heartbeat (60 s ago) → alive.
        assert!(!heartbeat_is_stale(Some(now - 60_000), Some(0), now, window));
        // Stale heartbeat (2× window ago) → dead, even with a fresh started_at:
        // COALESCE picks heartbeat_at first — this is WHY the legacy upsert
        // clears heartbeat_at on every transition.
        assert!(heartbeat_is_stale(Some(now - 3_600_000), Some(now - 1), now, window));
        // NULL heartbeat (legacy / pre-first-tick) falls back to started_at.
        assert!(!heartbeat_is_stale(None, Some(now - 60_000), now, window));
        assert!(heartbeat_is_stale(None, Some(now - 3_600_000), now, window));
        // Nothing at all → treated as epoch-0, stale.
        assert!(heartbeat_is_stale(None, None, now, window));
    }

    /// Ticker contract: the status-guarded UPDATE stamps ONLY a RUNNING
    /// row, and a tick that lands after the row reached a terminal state
    /// is a no-op (the ticker races the terminal upsert by design).
    #[test]
    fn touch_heartbeat_stamps_running_and_noops_post_terminal() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_sync(&pid, status::RUNNING, Some(1), None, None, 0, 0, 0, 0, 0, 0, None, None)
            .unwrap();

        let n = db.touch_kg_sync_heartbeat(&pid).unwrap();
        assert_eq!(n, 1, "RUNNING row must accept the tick");
        let row = db.get_kg_sync(&pid).unwrap().unwrap();
        assert!(row.heartbeat_at.is_some(), "heartbeat stamped");

        // Terminal transition (legacy upsert clears heartbeat)…
        db.upsert_kg_sync(
            &pid,
            status::SUCCESS,
            Some(1),
            Some(2),
            Some(1),
            1, 1, 0, 0, 0, 0,
            None,
            None,
        )
        .unwrap();
        let row = db.get_kg_sync(&pid).unwrap().unwrap();
        assert_eq!(row.heartbeat_at, None, "transition must clear heartbeat");

        // …and a late tick touches NOTHING (leave-alone).
        let n = db.touch_kg_sync_heartbeat(&pid).unwrap();
        assert_eq!(n, 0, "post-terminal tick must be a no-op");
        let row = db.get_kg_sync(&pid).unwrap().unwrap();
        assert_eq!(row.status, "success");
        assert_eq!(row.heartbeat_at, None);
    }

    /// The retry path re-queues via the legacy upsert; a stale heartbeat
    /// carried across that transition would make the fresh run look dead
    /// before its first tick. Pin the clear.
    #[test]
    fn upsert_clears_stale_heartbeat_on_retry_transition() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_kg_sync(&pid, status::RUNNING, Some(1), None, None, 0, 0, 0, 0, 0, 0, None, None)
            .unwrap();
        db.touch_kg_sync_heartbeat(&pid).unwrap();
        assert!(db.get_kg_sync(&pid).unwrap().unwrap().heartbeat_at.is_some());

        // Retry re-queues as PENDING.
        db.upsert_kg_sync(&pid, status::PENDING, Some(2), None, None, 0, 0, 0, 0, 0, 0, None, None)
            .unwrap();
        assert_eq!(
            db.get_kg_sync(&pid).unwrap().unwrap().heartbeat_at,
            None,
            "pending re-queue must not carry the previous run's heartbeat"
        );
    }

    /// Act + leave-alone: the stale sweep flips ONLY running-with-stale-
    /// liveness rows. Fresh-heartbeat RUNNING, legacy-fresh RUNNING,
    /// PENDING, and every terminal state are untouched.
    #[test]
    fn mark_stale_flips_only_stale_running_rows() {
        let db = Db::open_in_memory().expect("in-memory db");
        let now = chrono::Utc::now().timestamp_millis();
        let window_ms = (STALE_SECS as i64) * 1000;

        let running_stale_hb = insert_sync_project(&db, "running-stale-hb", status::RUNNING);
        backdate(&db, &running_stale_hb, Some(now - 2 * window_ms), Some(now - 3 * window_ms));

        let running_fresh_hb = insert_sync_project(&db, "running-fresh-hb", status::RUNNING);
        backdate(&db, &running_fresh_hb, Some(now - 60_000), Some(now - 3 * window_ms));

        // Legacy rows: NULL heartbeat, judged by started_at.
        let legacy_stale = insert_sync_project(&db, "legacy-stale", status::RUNNING);
        backdate(&db, &legacy_stale, None, Some(now - 2 * window_ms));

        let legacy_fresh = insert_sync_project(&db, "legacy-fresh", status::RUNNING);
        backdate(&db, &legacy_fresh, None, Some(now - 60_000));

        // Non-running rows with ancient stamps must never flip.
        let pending = insert_sync_project(&db, "hb-pending", status::PENDING);
        backdate(&db, &pending, None, Some(0));
        let success = insert_sync_project(&db, "hb-success", status::SUCCESS);
        backdate(&db, &success, Some(0), Some(0));
        let failed = insert_sync_project(&db, "hb-failed", status::FAILED);
        backdate(&db, &failed, None, Some(0));
        let skipped = insert_sync_project(&db, "hb-skipped", status::SKIPPED);
        backdate(&db, &skipped, None, Some(0));

        let n = db
            .mark_stale_running_kg_syncs_failed(STALE_SECS, STALE_MSG, None)
            .unwrap();
        assert_eq!(n, 2, "exactly the two stale RUNNING rows flip");

        let flipped = db.get_kg_sync(&running_stale_hb).unwrap().unwrap();
        assert_eq!(flipped.status, "failed");
        assert!(flipped.error_message.as_deref().unwrap_or("").contains("heartbeat stale"));
        assert!(flipped.finished_at.is_some(), "finished_at backfilled");
        assert_eq!(db.get_kg_sync(&legacy_stale).unwrap().unwrap().status, "failed");

        // Leave-alone legs.
        assert_eq!(db.get_kg_sync(&running_fresh_hb).unwrap().unwrap().status, "running");
        assert_eq!(db.get_kg_sync(&legacy_fresh).unwrap().unwrap().status, "running");
        assert_eq!(db.get_kg_sync(&pending).unwrap().unwrap().status, "pending");
        assert_eq!(db.get_kg_sync(&success).unwrap().unwrap().status, "success");
        assert_eq!(db.get_kg_sync(&failed).unwrap().unwrap().status, "failed");
        assert_eq!(db.get_kg_sync(&skipped).unwrap().unwrap().status, "skipped");
    }

    /// The read-time guard scopes the flip to ONE project; a sibling
    /// stale row is left for the sweeper.
    #[test]
    fn mark_stale_scopes_to_project_when_filter_given() {
        let db = Db::open_in_memory().expect("in-memory db");
        let now = chrono::Utc::now().timestamp_millis();
        let window_ms = (STALE_SECS as i64) * 1000;

        let a = insert_sync_project(&db, "scoped-a", status::RUNNING);
        backdate(&db, &a, Some(now - 2 * window_ms), None);
        let b = insert_sync_project(&db, "scoped-b", status::RUNNING);
        backdate(&db, &b, Some(now - 2 * window_ms), None);

        let n = db
            .mark_stale_running_kg_syncs_failed(STALE_SECS, STALE_MSG, Some(&a))
            .unwrap();
        assert_eq!(n, 1, "only the filtered project flips");
        assert_eq!(db.get_kg_sync(&a).unwrap().unwrap().status, "failed");
        assert_eq!(db.get_kg_sync(&b).unwrap().unwrap().status, "running");
    }
}
