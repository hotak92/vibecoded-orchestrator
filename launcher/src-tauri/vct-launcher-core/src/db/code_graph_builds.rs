//! Row-level CRUD for `code_graph_builds` table.
//!
//! One row per project tracks the lifecycle of the initial code-graph
//! analyzer run kicked off when the user creates a project (Gap 2 — OSS
//! launch 2026-05-12). Higher-level orchestration (the actual subprocess
//! spawn, event emission, log capture) lives in
//! `crate::commands::codegraph::build`.

use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

/// Lifecycle states for a project's initial code-graph build.
///
/// `pending`  → row inserted, subprocess not yet started.
/// `running`  → subprocess alive, files being analyzed.
/// `success`  → subprocess exited 0, rows in Weaviate.
/// `partial`  → inserts succeeded but stale-row DELETES failed
///              (`PRUNE_FAILURES=N`, N>0). Terminal, NOT a hard failure —
///              the file count survives (v0.2.73 C-11 / RT-3).
/// `failed`   → subprocess exited non-zero or panicked.
/// `skipped`  → no supported source files in the folder, nothing to do.
pub mod status {
    pub const PENDING: &str = "pending";
    pub const RUNNING: &str = "running";
    pub const SUCCESS: &str = "success";
    pub const PARTIAL: &str = "partial";
    pub const FAILED: &str = "failed";
    pub const SKIPPED: &str = "skipped";
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodeGraphBuildRow {
    pub project_id: String,
    pub status: String,
    pub started_at: Option<i64>,
    pub finished_at: Option<i64>,
    pub duration_ms: Option<i64>,
    pub files_analyzed: u32,
    /// JSON array, e.g. `["py","ts"]`. None when no build ran or no
    /// languages were detected.
    pub languages: Option<Vec<String>>,
    /// DEPRECATED (v0.2.73 CG-3): Joern CFG/PDG extraction was removed (zero
    /// readers); every write passes `false`. The `joern_used` DB column is
    /// RETAINED — dropping it needs a schema migration (a later cycle removes
    /// the column + this field together). Do NOT wire new logic to it.
    pub joern_used: bool,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
    /// R-4 (v0.2.73, migration 037): OS pid of a DETACHED analyzer
    /// process registered via the hub's codegraph-build endpoint
    /// (install.py's post-update resync). `None` = launcher-spawned
    /// build whose lifecycle is tied to the launcher process. The
    /// boot-time sweep treats the two differently — see
    /// `sweep_dead_detached_code_graph_builds`.
    pub pid: Option<i64>,
}

/// Cap stored log_tail at 4 KiB. The analyzer's stdout/stderr is mostly
/// human-readable progress lines; we keep just the tail for debugging
/// without bloating the SQLite row. Source code is never logged by the
/// analyzer, but if a future change ever leaks lines we still have a
/// hard size cap. (v0.2.54 Track J: re-exported from the shared
/// `db::log_tail` module — was a per-file const triplicated across the
/// three log-writing db modules.)
pub use super::log_tail::LOG_TAIL_MAX_BYTES;
use super::log_tail::cap_log_tail;

impl Db {
    /// UPSERT the build row for a project. Used by every transition
    /// (pending → running → success/failed/skipped). Caller is
    /// responsible for picking sensible field values for each status.
    ///
    /// Languages are serialized to JSON in the `languages` column. None
    /// stays SQL NULL. Same for `error_message` and `log_tail`.
    ///
    /// R-4 (v0.2.73): this legacy writer explicitly CLEARS `pid` on
    /// every call. Every caller of this signature is a LAUNCHER-spawned
    /// lifecycle transition (tokio child, no meaningful OS pid to
    /// record); a launcher-driven write superseding a detached-walk row
    /// must not leave the detached walk's stale pid attached to a row it
    /// no longer describes (the boot sweep would then aliveness-check
    /// the wrong process). Detached walks register through
    /// `register_running_code_graph_build` instead.
    #[allow(clippy::too_many_arguments)]
    pub fn upsert_code_graph_build(
        &self,
        project_id: &str,
        status: &str,
        started_at: Option<i64>,
        finished_at: Option<i64>,
        duration_ms: Option<i64>,
        files_analyzed: u32,
        languages: Option<&[String]>,
        joern_used: bool,
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
            return Err(format!("invalid code-graph build status: {}", status));
        }

        let langs_json: Option<String> = languages.map(|l| {
            serde_json::to_string(l)
                .unwrap_or_else(|_| "[]".to_string())
        });
        // Defensive: cap log_tail so we never write a huge blob, even if
        // a buggy caller hands us megabytes.
        let log_tail_capped: Option<String> = log_tail.map(cap_log_tail);

        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO code_graph_builds
                    (project_id, status, started_at, finished_at, duration_ms,
                     files_analyzed, languages, joern_used, error_message, log_tail, pid)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, NULL)
                 ON CONFLICT(project_id) DO UPDATE SET
                    status         = excluded.status,
                    started_at     = excluded.started_at,
                    finished_at    = excluded.finished_at,
                    duration_ms    = excluded.duration_ms,
                    files_analyzed = excluded.files_analyzed,
                    languages      = excluded.languages,
                    joern_used     = excluded.joern_used,
                    error_message  = excluded.error_message,
                    log_tail       = excluded.log_tail,
                    pid            = NULL",
                params![
                    project_id,
                    status,
                    started_at,
                    finished_at,
                    duration_ms,
                    files_analyzed,
                    langs_json,
                    if joern_used { 1 } else { 0 },
                    error_message,
                    log_tail_capped,
                ],
            )
            .map_err(|e| format!("upsert code_graph_builds: {}", e))?;
        Ok(())
    }

    /// Register a DETACHED analyzer walk as this project's build row
    /// (R-4, v0.2.73). Called by the hub's codegraph-build endpoint on
    /// behalf of install.py's `spawn_background_resync` — the row is
    /// written BEFORE the spawn is considered registered, giving the GUI
    /// its progress pill and the boot sweep a pid to aliveness-check.
    ///
    /// Writes: status='running', started_at=now, pid=`pid`; clears the
    /// terminal fields from any prior row (a fresh walk supersedes the
    /// previous build's outcome, same as the legacy upsert's semantics).
    pub fn register_running_code_graph_build(
        &self,
        project_id: &str,
        pid: u32,
    ) -> Result<(), String> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO code_graph_builds
                    (project_id, status, started_at, finished_at, duration_ms,
                     files_analyzed, languages, joern_used, error_message, log_tail, pid)
                 VALUES (?1, 'running', ?2, NULL, NULL, 0, NULL, 0, NULL, NULL, ?3)
                 ON CONFLICT(project_id) DO UPDATE SET
                    status         = 'running',
                    started_at     = excluded.started_at,
                    finished_at    = NULL,
                    duration_ms    = NULL,
                    files_analyzed = 0,
                    languages      = NULL,
                    joern_used     = 0,
                    error_message  = NULL,
                    log_tail       = NULL,
                    pid            = excluded.pid",
                params![project_id, now_ms, pid as i64],
            )
            .map_err(|e| format!("register running code_graph_build: {}", e))?;
        Ok(())
    }

    pub fn get_code_graph_build(
        &self,
        project_id: &str,
    ) -> Result<Option<CodeGraphBuildRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, status, started_at, finished_at, duration_ms,
                        files_analyzed, languages, joern_used, error_message, log_tail, pid
                 FROM code_graph_builds
                 WHERE project_id = ?1",
                params![project_id],
                row_to_build,
            )
            .optional()
            .map_err(|e| format!("get code_graph_build: {}", e))
    }

    /// Project IDs whose most recent recorded status is 'pending'. Used
    /// at startup to recover from a launcher crash mid-build (we requeue
    /// the build) and by anyone wanting to know what's queued.
    ///
    /// Note: 'running' is intentionally NOT included here. A 'running'
    /// row after a launcher crash is a stale ghost (the subprocess is
    /// dead). Rebuild-on-startup logic should treat such rows as failed
    /// via `mark_orphaned_running_code_graph_builds_failed` so the GUI
    /// banner shows the broken lifecycle with a Retry button — silent
    /// re-spawn would mask the underlying crash.
    ///
    /// Wired at launcher boot (2026-05-12): see `lib.rs setup()` →
    /// `codegraph::resume_pending_builds`. Pre-2026-05-12 this function
    /// had a `#[allow(dead_code)]` and a "TODO: wire" docstring; both
    /// are gone now that the boot-time resume pass is live.
    pub fn list_pending_code_graph_builds(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM code_graph_builds
                 WHERE status = 'pending' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list pending: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list pending: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list pending: {}", e))
    }

    /// Project IDs whose most recent recorded status is 'running'.
    /// Exposed for unit-test diagnostics — the production sweep path
    /// (`mark_orphaned_running_code_graph_builds_failed`) is a single
    /// UPDATE that doesn't need a pre-list. Used by the resume-after-
    /// crash tests to assert the fixture row selection.
    ///
    /// Mirrors the test-only `Db::list_orphaned_running_kg_syncs`.
    #[cfg(test)]
    pub fn list_orphaned_running_code_graph_builds(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id FROM code_graph_builds
                 WHERE status = 'running' ORDER BY started_at ASC",
            )
            .map_err(|e| format!("prepare list running code_graph_builds: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list running code_graph_builds: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list running code_graph_builds: {}", e))
    }

    /// Single-statement update: flip every LAUNCHER-SPAWNED row currently
    /// in status='running' (pid IS NULL) to status='failed' with a fixed
    /// error message + finished_at=now. Used by the launcher-startup
    /// sweep. Returns rows-affected.
    ///
    /// R-4 (v0.2.73): restricted to `pid IS NULL`. A 'running' row that
    /// CARRIES a pid belongs to a detached analyzer (install.py resync)
    /// that legitimately survives launcher restarts — those rows are
    /// reconciled by `sweep_dead_detached_code_graph_builds` (pid
    /// aliveness-aware) instead of being blanket-failed at boot.
    ///
    /// Mirrors `Db::mark_orphaned_running_kg_syncs_failed`.
    pub fn mark_orphaned_running_code_graph_builds_failed(
        &self,
        error_message: &str,
    ) -> Result<usize, String> {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        let affected = guard
            .execute(
                "UPDATE code_graph_builds
                    SET status = 'failed',
                        finished_at = ?1,
                        duration_ms = CASE
                            WHEN started_at IS NOT NULL THEN ?1 - started_at
                            ELSE NULL
                        END,
                        error_message = ?2
                  WHERE status = 'running' AND pid IS NULL",
                params![now_ms, error_message],
            )
            .map_err(|e| format!("mark orphaned running code_graph_builds failed: {}", e))?;
        Ok(affected)
    }

    /// R-4 (v0.2.73): reconcile DETACHED 'running' rows (pid IS NOT NULL)
    /// against actual process liveness. For each such row, `is_pid_alive`
    /// decides:
    ///   * alive → leave alone (the detached walk is still working; it
    ///     survives launcher restarts by design).
    ///   * positively dead → flip to 'failed' with a message naming the
    ///     pid, so the GUI shows the broken walk with a Retry button and
    ///     the silent-death case (RT-5: a P7 resync died mid-walk leaving
    ///     ~6.2k stale rows with zero signal) becomes visible.
    ///   * pid out of u32 range (corrupt row) → CONSERVATIVE leave-alone;
    ///     we cannot positively confirm deadness, and a false 'failed' on
    ///     a live walk is worse than a stale pill.
    ///
    /// `is_pid_alive` is injected so unit tests can simulate dead/alive
    /// pids without real processes; production passes
    /// `vct_launcher_core::process::pid_is_alive`.
    ///
    /// Returns the project_ids whose rows were flipped to 'failed'.
    pub fn sweep_dead_detached_code_graph_builds(
        &self,
        is_pid_alive: impl Fn(u32) -> bool,
    ) -> Result<Vec<String>, String> {
        // Snapshot (project_id, pid) pairs first; decide + update per row.
        let rows: Vec<(String, i64)> = {
            let guard = self.lock();
            let mut stmt = guard
                .prepare(
                    "SELECT project_id, pid FROM code_graph_builds
                     WHERE status = 'running' AND pid IS NOT NULL",
                )
                .map_err(|e| format!("prepare list detached running builds: {}", e))?;
            let mapped = stmt
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))
                .map_err(|e| format!("query detached running builds: {}", e))?;
            mapped
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("collect detached running builds: {}", e))?
        };

        let mut failed = Vec::new();
        for (project_id, pid) in rows {
            let pid_u32 = match u32::try_from(pid) {
                Ok(p) => p,
                // Corrupt/out-of-range pid: cannot positively confirm
                // deadness → leave the row alone (conservative default).
                Err(_) => continue,
            };
            if is_pid_alive(pid_u32) {
                continue;
            }
            let now_ms = chrono::Utc::now().timestamp_millis();
            let msg = format!(
                "detached analyzer process (pid {}) is no longer running; \
                 the walk died before completing — click Re-analyze (or re-run \
                 the update resync) to finish it",
                pid_u32
            );
            let guard = self.lock();
            guard
                .execute(
                    "UPDATE code_graph_builds
                        SET status = 'failed',
                            finished_at = ?1,
                            duration_ms = CASE
                                WHEN started_at IS NOT NULL THEN ?1 - started_at
                                ELSE NULL
                            END,
                            error_message = ?2
                      WHERE project_id = ?3 AND status = 'running'",
                    params![now_ms, msg, project_id],
                )
                .map_err(|e| format!("fail dead detached build {}: {}", project_id, e))?;
            failed.push(project_id);
        }
        Ok(failed)
    }
}

fn row_to_build(row: &rusqlite::Row<'_>) -> rusqlite::Result<CodeGraphBuildRow> {
    let langs_json: Option<String> = row.get(6)?;
    let languages: Option<Vec<String>> = langs_json
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok());
    let joern_int: i64 = row.get(7)?;
    Ok(CodeGraphBuildRow {
        project_id: row.get(0)?,
        status: row.get(1)?,
        started_at: row.get(2)?,
        finished_at: row.get(3)?,
        duration_ms: row.get(4)?,
        files_analyzed: row.get::<_, i64>(5)? as u32,
        languages,
        joern_used: joern_int != 0,
        error_message: row.get(8)?,
        log_tail: row.get(9)?,
        pid: row.get(10)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    /// Platform-aware placeholder folder path for fixtures. Tests only
    /// store this in the `folder_path` SQL column for uniqueness / round-
    /// trip checks — they never touch disk — but a string like `/tmp/x`
    /// looks ambiguous on Windows where `Path::new("/tmp/x")` is parsed
    /// as relative-ish. Pick a host-appropriate fake.
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
        let folder = fixture_path("whatever");
        db.insert_project(&id, "Test", &folder, ProjectHost::Base, &slug)
            .unwrap();
        (db, id)
    }

    #[test]
    fn upsert_then_get_round_trips_all_fields() {
        let (db, pid) = fresh_db_with_project();
        let langs = vec!["py".to_string(), "ts".to_string()];
        db.upsert_code_graph_build(
            &pid,
            status::SUCCESS,
            Some(1000),
            Some(2500),
            Some(1500),
            42,
            Some(&langs),
            true,
            None,
            Some("ok"),
        )
        .unwrap();

        let got = db.get_code_graph_build(&pid).unwrap().expect("row exists");
        assert_eq!(got.status, "success");
        assert_eq!(got.started_at, Some(1000));
        assert_eq!(got.finished_at, Some(2500));
        assert_eq!(got.duration_ms, Some(1500));
        assert_eq!(got.files_analyzed, 42);
        assert_eq!(got.languages.as_deref().unwrap(), ["py", "ts"]);
        assert!(got.joern_used);
        assert_eq!(got.error_message, None);
        assert_eq!(got.log_tail.as_deref(), Some("ok"));
    }

    #[test]
    fn upsert_overwrites_on_state_transition() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_code_graph_build(&pid, status::PENDING, Some(1), None, None, 0, None, false, None, None).unwrap();
        db.upsert_code_graph_build(&pid, status::RUNNING, Some(1), None, None, 5, None, false, None, None).unwrap();
        db.upsert_code_graph_build(
            &pid,
            status::SUCCESS,
            Some(1),
            Some(100),
            Some(99),
            10,
            Some(&["py".to_string()]),
            false,
            None,
            Some("done"),
        )
        .unwrap();

        let got = db.get_code_graph_build(&pid).unwrap().unwrap();
        assert_eq!(got.status, "success");
        assert_eq!(got.files_analyzed, 10);
    }

    #[test]
    fn invalid_status_rejected_with_clear_error() {
        let (db, pid) = fresh_db_with_project();
        let err = db
            .upsert_code_graph_build(&pid, "borked", None, None, None, 0, None, false, None, None)
            .expect_err("must reject");
        assert!(err.contains("borked"));
    }

    #[test]
    fn log_tail_truncated_to_4kb() {
        let (db, pid) = fresh_db_with_project();
        let big = "x".repeat(10_000);
        db.upsert_code_graph_build(
            &pid,
            status::SUCCESS,
            Some(1),
            Some(1),
            Some(0),
            0,
            None,
            false,
            None,
            Some(&big),
        )
        .unwrap();
        let got = db.get_code_graph_build(&pid).unwrap().unwrap();
        let tail = got.log_tail.unwrap();
        // Truncated tail should be ≤ 4KB + a couple bytes for the leading marker.
        assert!(
            tail.len() <= LOG_TAIL_MAX_BYTES + 8,
            "expected truncation, got {} bytes",
            tail.len()
        );
        assert!(tail.starts_with('…'), "expected leading ellipsis marker");
    }

    #[test]
    fn list_pending_returns_only_pending_rows() {
        let db = Db::open_in_memory().unwrap();
        for (i, (name, st)) in [
            ("A", status::PENDING),
            ("B", status::RUNNING),
            ("C", status::SUCCESS),
            ("D", status::PENDING),
        ]
        .iter()
        .enumerate()
        {
            let id = uuid::Uuid::new_v4().to_string();
            let slug = db.generate_unique_slug(name).unwrap();
            // folder_path has a UNIQUE index in the projects schema —
            // give each row a distinct path.
            let folder = fixture_path(&format!("cgbuild-{}", i));
            db.insert_project(&id, name, &folder, ProjectHost::Base, &slug)
                .unwrap();
            db.upsert_code_graph_build(&id, st, Some(0), None, None, 0, None, false, None, None)
                .unwrap();
        }
        let pending = db.list_pending_code_graph_builds().unwrap();
        assert_eq!(pending.len(), 2);
    }

    // ─── Resume-after-crash helpers (boot-time sweep, 2026-05-12) ────────

    /// Build a fixture with multiple projects across different build states
    /// so the list_* + sweep helpers can be exercised. Mirrors the
    /// `fresh_db_with_mixed_states` fixture in `kg_syncs.rs`.
    fn fresh_db_with_mixed_build_states() -> (Db, std::collections::HashMap<&'static str, String>) {
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
            let folder = fixture_path(&format!("cgbuild-mixed-{}", idx));
            db.insert_project(&id, label, &folder, ProjectHost::Base, &slug)
                .unwrap();
            // started_at = idx for deterministic ASC sort.
            db.upsert_code_graph_build(
                &id,
                st,
                Some(idx as i64),
                None,
                None,
                0,
                None,
                false,
                None,
                None,
            )
            .unwrap();
            ids.insert(*label, id);
        }
        (db, ids)
    }

    #[test]
    fn list_orphaned_running_code_graph_builds_returns_only_running() {
        let (db, ids) = fresh_db_with_mixed_build_states();
        let running = db.list_orphaned_running_code_graph_builds().unwrap();
        assert_eq!(running.len(), 1);
        assert_eq!(&running[0], ids.get("running_a").unwrap());
    }

    #[test]
    fn mark_orphaned_running_code_graph_builds_flips_to_failed() {
        let (db, ids) = fresh_db_with_mixed_build_states();
        assert_eq!(db.list_orphaned_running_code_graph_builds().unwrap().len(), 1);

        let n = db
            .mark_orphaned_running_code_graph_builds_failed(
                "launcher crashed mid-run; click Retry to re-run",
            )
            .unwrap();
        assert_eq!(n, 1);

        let row = db
            .get_code_graph_build(ids.get("running_a").unwrap())
            .unwrap()
            .expect("row still exists");
        assert_eq!(row.status, "failed");
        assert!(row
            .error_message
            .as_deref()
            .unwrap_or("")
            .contains("launcher crashed"));
        assert!(row.finished_at.is_some());

        // Other states left untouched.
        let pending = db.get_code_graph_build(ids.get("pending_a").unwrap()).unwrap().unwrap();
        assert_eq!(pending.status, "pending");
        let success = db.get_code_graph_build(ids.get("success_a").unwrap()).unwrap().unwrap();
        assert_eq!(success.status, "success");
        // No more orphans.
        assert!(db.list_orphaned_running_code_graph_builds().unwrap().is_empty());
    }

    #[test]
    fn mark_orphaned_running_code_graph_builds_no_op_when_empty() {
        let (db, _) = fresh_db_with_project();
        let n = db
            .mark_orphaned_running_code_graph_builds_failed("ignored")
            .unwrap();
        assert_eq!(n, 0);
    }

    // ─── R-4 (v0.2.73): detached-walk registration + pid-aware sweep ────

    /// The hub endpoint's writer: a fresh 'running' row carrying the
    /// detached analyzer's pid, terminal fields cleared.
    #[test]
    fn register_running_writes_running_row_with_pid() {
        let (db, pid_str) = fresh_db_with_project();
        // Seed a prior terminal row so we prove the re-registration
        // clears it (fresh walk supersedes the previous outcome).
        db.upsert_code_graph_build(
            &pid_str,
            status::SUCCESS,
            Some(1),
            Some(2),
            Some(1),
            9,
            None,
            false,
            None,
            Some("old"),
        )
        .unwrap();

        db.register_running_code_graph_build(&pid_str, 424242).unwrap();

        let row = db.get_code_graph_build(&pid_str).unwrap().unwrap();
        assert_eq!(row.status, "running");
        assert_eq!(row.pid, Some(424242));
        assert!(row.started_at.is_some(), "started_at stamped");
        assert_eq!(row.finished_at, None, "terminal fields cleared");
        assert_eq!(row.error_message, None);
        assert_eq!(row.log_tail, None);
        assert_eq!(row.files_analyzed, 0);
    }

    /// The legacy (launcher-spawned) upsert CLEARS pid: a launcher-driven
    /// transition superseding a detached-walk row must not leave the
    /// stale pid attached (the sweep would aliveness-check the wrong
    /// process).
    #[test]
    fn legacy_upsert_clears_pid() {
        let (db, pid_str) = fresh_db_with_project();
        db.register_running_code_graph_build(&pid_str, 555).unwrap();
        assert_eq!(
            db.get_code_graph_build(&pid_str).unwrap().unwrap().pid,
            Some(555)
        );

        db.upsert_code_graph_build(
            &pid_str,
            status::RUNNING,
            Some(10),
            None,
            None,
            0,
            None,
            false,
            None,
            None,
        )
        .unwrap();

        let row = db.get_code_graph_build(&pid_str).unwrap().unwrap();
        assert_eq!(row.pid, None, "legacy upsert must clear pid");
    }

    /// Boot ghost-sweep must SKIP detached rows: a 'running' row with a
    /// pid belongs to a process that survives launcher restarts.
    #[test]
    fn orphan_sweep_skips_detached_pid_rows() {
        let (db, ids) = fresh_db_with_mixed_build_states();
        // Add a detached running row alongside the launcher-spawned one.
        let detached = uuid::Uuid::new_v4().to_string();
        let slug = db.generate_unique_slug("detached").unwrap();
        db.insert_project(
            &detached,
            "detached",
            &fixture_path("cgbuild-detached"),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();
        db.register_running_code_graph_build(&detached, std::process::id())
            .unwrap();

        let n = db
            .mark_orphaned_running_code_graph_builds_failed("launcher crashed")
            .unwrap();
        assert_eq!(n, 1, "only the pid-NULL launcher ghost is swept");

        let ghost = db
            .get_code_graph_build(ids.get("running_a").unwrap())
            .unwrap()
            .unwrap();
        assert_eq!(ghost.status, "failed");
        let kept = db.get_code_graph_build(&detached).unwrap().unwrap();
        assert_eq!(
            kept.status, "running",
            "detached (pid-bearing) row must survive the ghost sweep"
        );
    }

    /// Dead-detached sweep: flips only rows whose pid is POSITIVELY dead;
    /// alive pids and launcher (pid-NULL) rows are untouched.
    #[test]
    fn sweep_dead_detached_flips_only_dead_pids() {
        let (db, ids) = fresh_db_with_mixed_build_states();
        let mk = |db: &Db, label: &str, pid: u32| -> String {
            let id = uuid::Uuid::new_v4().to_string();
            let slug = db.generate_unique_slug(label).unwrap();
            db.insert_project(
                &id,
                label,
                &fixture_path(&format!("cgbuild-{}", label)),
                ProjectHost::Base,
                &slug,
            )
            .unwrap();
            db.register_running_code_graph_build(&id, pid).unwrap();
            id
        };
        let alive_id = mk(&db, "detached-alive", 1111);
        let dead_id = mk(&db, "detached-dead", 2222);

        // Injected aliveness: 1111 alive, everything else dead.
        let failed = db
            .sweep_dead_detached_code_graph_builds(|pid| pid == 1111)
            .unwrap();
        assert_eq!(failed, vec![dead_id.clone()], "exactly the dead pid's row flips");

        let dead_row = db.get_code_graph_build(&dead_id).unwrap().unwrap();
        assert_eq!(dead_row.status, "failed");
        assert!(
            dead_row.error_message.as_deref().unwrap_or("").contains("2222"),
            "failure message names the dead pid: {:?}",
            dead_row.error_message
        );
        assert!(dead_row.finished_at.is_some());

        let alive_row = db.get_code_graph_build(&alive_id).unwrap().unwrap();
        assert_eq!(alive_row.status, "running", "alive walk left alone");
        // Launcher-spawned running row (pid NULL) untouched by THIS sweep.
        let ghost = db
            .get_code_graph_build(ids.get("running_a").unwrap())
            .unwrap()
            .unwrap();
        assert_eq!(
            ghost.status, "running",
            "pid-NULL rows are the ghost sweep's job, not the detached sweep's"
        );
    }

    /// pid survives the row round-trip and defaults to None on the
    /// legacy writer.
    #[test]
    fn pid_round_trips_and_defaults_none() {
        let (db, pid_str) = fresh_db_with_project();
        db.upsert_code_graph_build(
            &pid_str,
            status::PENDING,
            None,
            None,
            None,
            0,
            None,
            false,
            None,
            None,
        )
        .unwrap();
        assert_eq!(db.get_code_graph_build(&pid_str).unwrap().unwrap().pid, None);
        db.register_running_code_graph_build(&pid_str, 7).unwrap();
        assert_eq!(
            db.get_code_graph_build(&pid_str).unwrap().unwrap().pid,
            Some(7)
        );
    }

    #[test]
    fn cascade_delete_removes_build_row() {
        let (db, pid) = fresh_db_with_project();
        db.upsert_code_graph_build(&pid, status::PENDING, None, None, None, 0, None, false, None, None)
            .unwrap();
        db.delete_project(&pid).unwrap();
        let got = db.get_code_graph_build(&pid).unwrap();
        assert!(got.is_none(), "row should cascade-delete with project");
    }
}
