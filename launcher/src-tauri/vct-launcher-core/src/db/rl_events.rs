// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! RL telemetry events — queryable replacement for the JSONL corpus.
//!
//! Migration 025 (v0.2.47) ships one table:
//!
//!   * `rl_events` — append-only `{retrieval, citation}` events written by
//!     the MCP-side telemetry writer (`claude_mcp_servers/rl_client/`)
//!     via the hub's `POST /api/v1/rl/events` route. The full event JSON
//!     lives in `payload_json TEXT`; the indexed columns (event_type,
//!     ts, project_id, task_id, embedding_source) are denormalized copies
//!     of fields inside the payload kept SQL-queryable.
//!
//! WRITER MODEL: this module exposes `insert_rl_event` only. The hub
//! handler (`vct-hub/src/rl_events_api.rs`) parses the incoming JSON,
//! pulls the indexed fields, and calls this helper with the raw payload.
//! Python clients NEVER open launcher.db directly — preserves the
//! single-writer architectural rule documented at
//! `vco_lib/config_projection.py:488-491`.
//!
//! READ MODEL: `list_rl_events` for dashboard widgets and `count_rl_events`
//! for the per-project counter the launcher Identity tab shows. The
//! offline trainer's read path uses the hub's GET route directly to
//! avoid in-process SQLite coupling.

use rusqlite::{params, OptionalExtension};

use super::Db;

/// One RL event row. Matches the migration-025 schema (+ the migration-039
/// quarantine columns, v0.2.75 RL-14).
///
/// `payload_json` carries the full v3 event JSON verbatim. The indexed
/// columns above are denormalized for SQL queries; callers that need
/// non-indexed fields (e.g. per-node `n_emb`, `linked_embs`, `cosine_sims`)
/// must parse `payload_json` themselves.
///
/// `quarantined_at` (unix-ms) + `quarantine_reason` mark POISONED rows —
/// e.g. the historical out-of-range-score class that pre-dates the v0.2.70
/// F-E writer clamp. Marked rows stay on disk (query-distribution signal)
/// but are excluded from training-data reads by default.
#[derive(Debug, Clone)]
pub struct RlEvent {
    pub id: i64,
    pub event_type: String,
    pub schema_version: i64,
    /// Unix epoch millis.
    pub ts_ms: i64,
    pub project_id: Option<String>,
    pub project_name: Option<String>,
    pub task_id: String,
    pub task_type: Option<String>,
    pub embedding_source: Option<String>,
    pub embedding_dim: Option<i64>,
    pub embedding_model: Option<String>,
    pub payload_json: String,
    /// RL-14: unix-ms when the row was quarantined; NULL = clean.
    pub quarantined_at: Option<i64>,
    /// RL-14: stable machine tag (e.g. `score_out_of_range`).
    pub quarantine_reason: Option<String>,
}

/// RL-14: the app_state key guarding the one-time historical marking pass.
/// Present (any value) ⇒ the backfill already ran on this launcher.db.
pub const QUARANTINE_BACKFILL_STATE_KEY: &str = "rl_events.quarantine_backfill_v1";

/// RL-14: stable reason tag for the historical out-of-range-score class.
pub const QUARANTINE_REASON_SCORE_OUT_OF_RANGE: &str = "score_out_of_range";

impl Db {
    /// Insert one RL event. Always appends; rl_events is never updated in
    /// place. Returns the new row's `id`.
    ///
    /// Soft-fail discipline: any DB error returns `Err(String)` to the
    /// caller. The hub handler logs + responds 5xx; the Python writer
    /// treats non-2xx as data loss (per the locked decision — no retry
    /// queue, no JSONL fallback).
    ///
    /// `ts_ms` is supplied by the caller (NOT server-side now()) because
    /// the writer captured the wall-clock at event-construction time and
    /// the hub may be reached after a buffering delay; the auth-time
    /// timestamp is what's training-relevant.
    #[allow(clippy::too_many_arguments)]
    pub fn insert_rl_event(
        &self,
        event_type: &str,
        schema_version: i64,
        ts_ms: i64,
        project_id: Option<&str>,
        project_name: Option<&str>,
        task_id: &str,
        task_type: Option<&str>,
        embedding_source: Option<&str>,
        embedding_dim: Option<i64>,
        embedding_model: Option<&str>,
        payload_json: &str,
    ) -> Result<i64, String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO rl_events
                    (event_type, schema_version, ts, project_id, project_name,
                     task_id, task_type, embedding_source, embedding_dim,
                     embedding_model, payload_json)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                params![
                    event_type,
                    schema_version,
                    ts_ms,
                    project_id,
                    project_name,
                    task_id,
                    task_type,
                    embedding_source,
                    embedding_dim,
                    embedding_model,
                    payload_json,
                ],
            )
            .map_err(|e| format!("insert rl_event: {}", e))?;
        Ok(guard.last_insert_rowid())
    }

    /// List rl_events for a project / event-type / time-range. Returns rows
    /// newest-first. Used by the launcher GUI's per-project event-rate
    /// dashboard and the offline trainer's resume-from-cursor read.
    ///
    /// All filters are optional; passing all `None` returns the most-recent
    /// `limit` rows across the whole table (use with care — the table grows
    /// linearly with retrieval traffic).
    ///
    /// RL-14 (v0.2.75): `include_quarantined = false` (the default every
    /// training-data read passes) excludes rows a marking pass flagged as
    /// poisoned (`quarantined_at IS NOT NULL`). Pass `true` only for
    /// inspection surfaces that deliberately want the full corpus.
    pub fn list_rl_events(
        &self,
        project_id: Option<&str>,
        event_type: Option<&str>,
        since_ms: Option<i64>,
        until_ms: Option<i64>,
        limit: u32,
        include_quarantined: bool,
    ) -> Result<Vec<RlEvent>, String> {
        // Build the WHERE clause + params iteratively to keep the prepared
        // statement cache-friendly across common filter combinations.
        let mut sql = String::from(
            "SELECT id, event_type, schema_version, ts, project_id, project_name,
                    task_id, task_type, embedding_source, embedding_dim,
                    embedding_model, payload_json, quarantined_at, quarantine_reason
               FROM rl_events
              WHERE 1=1",
        );
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
        if let Some(p) = project_id {
            sql.push_str(" AND project_id = ?");
            params_vec.push(Box::new(p.to_string()));
        }
        if let Some(et) = event_type {
            sql.push_str(" AND event_type = ?");
            params_vec.push(Box::new(et.to_string()));
        }
        if let Some(s) = since_ms {
            sql.push_str(" AND ts >= ?");
            params_vec.push(Box::new(s));
        }
        if let Some(u) = until_ms {
            sql.push_str(" AND ts <= ?");
            params_vec.push(Box::new(u));
        }
        if !include_quarantined {
            sql.push_str(" AND quarantined_at IS NULL");
        }
        sql.push_str(" ORDER BY ts DESC, id DESC LIMIT ?");
        params_vec.push(Box::new(limit as i64));

        let guard = self.lock();
        let mut stmt = guard
            .prepare(&sql)
            .map_err(|e| format!("prepare list_rl_events: {}", e))?;
        let param_refs: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();
        let rows = stmt
            .query_map(param_refs.as_slice(), |row| {
                Ok(RlEvent {
                    id: row.get(0)?,
                    event_type: row.get(1)?,
                    schema_version: row.get(2)?,
                    ts_ms: row.get(3)?,
                    project_id: row.get(4)?,
                    project_name: row.get(5)?,
                    task_id: row.get(6)?,
                    task_type: row.get(7)?,
                    embedding_source: row.get(8)?,
                    embedding_dim: row.get(9)?,
                    embedding_model: row.get(10)?,
                    payload_json: row.get(11)?,
                    quarantined_at: row.get(12)?,
                    quarantine_reason: row.get(13)?,
                })
            })
            .map_err(|e| format!("query list_rl_events: {}", e))?;

        let mut out = Vec::new();
        for r in rows {
            out.push(r.map_err(|e| format!("read rl_event row: {}", e))?);
        }
        Ok(out)
    }

    /// Count rl_events for a project / event-type / time-range.
    /// Used by the launcher Identity-tab event-rate badge.
    ///
    /// RL-14 (v0.2.75): `quarantined = None` counts ALL rows (the badge's
    /// pre-RL-14 semantics, unchanged); `Some(true)` counts only quarantined
    /// rows (rl-doctor's report); `Some(false)` only clean rows.
    pub fn count_rl_events(
        &self,
        project_id: Option<&str>,
        event_type: Option<&str>,
        since_ms: Option<i64>,
        quarantined: Option<bool>,
    ) -> Result<i64, String> {
        let mut sql = String::from("SELECT COUNT(*) FROM rl_events WHERE 1=1");
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
        if let Some(p) = project_id {
            sql.push_str(" AND project_id = ?");
            params_vec.push(Box::new(p.to_string()));
        }
        if let Some(et) = event_type {
            sql.push_str(" AND event_type = ?");
            params_vec.push(Box::new(et.to_string()));
        }
        if let Some(s) = since_ms {
            sql.push_str(" AND ts >= ?");
            params_vec.push(Box::new(s));
        }
        match quarantined {
            Some(true) => sql.push_str(" AND quarantined_at IS NOT NULL"),
            Some(false) => sql.push_str(" AND quarantined_at IS NULL"),
            None => {}
        }

        let guard = self.lock();
        let param_refs: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();
        let row: Option<i64> = guard
            .query_row(&sql, param_refs.as_slice(), |row| row.get(0))
            .optional()
            .map_err(|e| format!("count_rl_events: {}", e))?;
        Ok(row.unwrap_or(0))
    }

    /// Prune rl_events by age and/or row-cap (RL-5 retention, v0.2.73).
    ///
    /// Drives the hub's `POST /api/v1/rl/events/prune` route, which the
    /// Python retention driver (`rl_client/hub_writer.py::post_rl_prune`)
    /// calls to keep the corpus bounded. Two independent bounds, applied in
    /// a single logical pass; returns the TOTAL rows deleted across both.
    ///
    ///   * `cutoff_ms` (Some): delete rows with `ts < cutoff_ms` (age bound).
    ///   * `max_rows`  (Some): keep only the newest `max_rows` rows (by
    ///     `ts DESC, id DESC`), delete the rest (row-cap bound).
    ///   * `project_id` (Some): scope BOTH bounds to that project. A
    ///     `project_id = ?` predicate naturally excludes `project_id IS NULL`
    ///     (free-tier) rows — desired. `None` spans ALL projects (global),
    ///     which is the documented contract for an unscoped retention run.
    ///
    /// SAFETY (hard requirement): if BOTH bounds are `None` this is a no-op —
    /// it deletes nothing and returns `Ok(0)`. A prune with no age bound and
    /// no row cap must NEVER delete rows (that would wipe the corpus). The
    /// guard below returns early before any DELETE is prepared.
    ///
    /// The two DELETEs run under the SAME lock guard so a concurrent writer
    /// cannot interleave a row between the age-prune and the row-cap-prune.
    pub fn prune_rl_events(
        &self,
        cutoff_ms: Option<i64>,
        max_rows: Option<i64>,
        project_id: Option<&str>,
    ) -> Result<u64, String> {
        // Empty/degenerate-bounds no-op guard: never delete-all. A row-cap of
        // Some(0) (or negative) is NOT a valid keep-zero-delete-all request —
        // `LIMIT 0` yields an empty keep-set so `id NOT IN ()` would wipe the
        // ENTIRE corpus (the invariant the doc above forbids). `_DEFAULT_MAX_ROWS
        // = 0` on the Python driver means "row-cap disabled"; the driver coerces
        // 0 -> None, but this method is the deletion AUTHORITY and must not
        // depend on a caller's coercion (a manual curl / rl-doctor / future
        // driver edit could pass 0). Treat max_rows <= 0 as "no row-cap bound".
        let rowcap_active = matches!(max_rows, Some(n) if n > 0);
        if cutoff_ms.is_none() && !rowcap_active {
            return Ok(0);
        }

        let guard = self.lock();
        let mut deleted: u64 = 0;

        // Age bound: DELETE rows older than the cutoff, within project scope.
        if let Some(cutoff) = cutoff_ms {
            let n = if let Some(pid) = project_id {
                guard
                    .execute(
                        "DELETE FROM rl_events WHERE ts < ?1 AND project_id = ?2",
                        params![cutoff, pid],
                    )
                    .map_err(|e| format!("prune_rl_events (cutoff): {}", e))?
            } else {
                guard
                    .execute(
                        "DELETE FROM rl_events WHERE ts < ?1",
                        params![cutoff],
                    )
                    .map_err(|e| format!("prune_rl_events (cutoff): {}", e))?
            };
            deleted += n as u64;
        }

        // Row-cap bound: keep only the newest `max_rows` rows (by ts DESC,
        // id DESC as tiebreak), delete the rest — within project scope.
        // Defense-in-depth (matches the guard above): keep <= 0 is NOT a
        // keep-zero-delete-all — a LIMIT 0 keep-set would wipe the corpus. Only
        // a positive keep is a real row-cap; 0/negative = "row-cap disabled".
        if let Some(keep) = max_rows.filter(|&k| k > 0) {
            let n = if let Some(pid) = project_id {
                guard
                    .execute(
                        "DELETE FROM rl_events
                          WHERE project_id = ?1
                            AND id NOT IN (
                                SELECT id FROM rl_events
                                 WHERE project_id = ?1
                                 ORDER BY ts DESC, id DESC
                                 LIMIT ?2
                            )",
                        params![pid, keep],
                    )
                    .map_err(|e| format!("prune_rl_events (max_rows): {}", e))?
            } else {
                guard
                    .execute(
                        "DELETE FROM rl_events
                          WHERE id NOT IN (
                                SELECT id FROM rl_events
                                 ORDER BY ts DESC, id DESC
                                 LIMIT ?1
                            )",
                        params![keep],
                    )
                    .map_err(|e| format!("prune_rl_events (max_rows): {}", e))?
            };
            deleted += n as u64;
        }

        Ok(deleted)
    }

    /// RL-14 (v0.2.75): one-time marking pass for the HISTORICAL poisoned
    /// class — retrieval events whose payload carries any node `score > 1.0`
    /// (unbounded hybrid-fusion scores that pre-date the v0.2.70 F-E writer
    /// clamp; `compute_unified_targets` clamped them to 1.0, silently
    /// mis-marking those nodes as max-cited in every training pass).
    ///
    /// Marks rows (`quarantined_at = now_ms`, reason
    /// `score_out_of_range`) — never deletes. IDEMPOTENT by construction:
    /// only rows with `quarantined_at IS NULL` are examined, so a re-run
    /// touches nothing already marked (and never rewrites a timestamp).
    ///
    /// Runs in Rust, not migration SQL: `payload_json` is writer-supplied
    /// TEXT the hub never JSON-validates, so a SQL `json_each` pass would
    /// hard-error the whole migration on one malformed row. Here a row that
    /// fails to parse is SKIPPED (left clean — conservative leave-alone: we
    /// only quarantine rows we can positively convict).
    ///
    /// Returns the number of rows marked.
    pub fn backfill_quarantine_out_of_range(&self, now_ms: i64) -> Result<u64, String> {
        // Collect candidate ids under one lock, then mark under another —
        // the table is append-only + the NULL filter makes the two-step
        // safe against concurrent writers (new rows are clamped at the
        // writer boundary and can't join the historical class).
        let candidates: Vec<i64> = {
            let guard = self.lock();
            let mut stmt = guard
                .prepare(
                    "SELECT id, payload_json FROM rl_events
                      WHERE event_type = 'retrieval' AND quarantined_at IS NULL",
                )
                .map_err(|e| format!("prepare quarantine scan: {}", e))?;
            let rows = stmt
                .query_map([], |row| {
                    Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
                })
                .map_err(|e| format!("query quarantine scan: {}", e))?;

            let mut ids = Vec::new();
            for r in rows {
                let (id, payload) = r.map_err(|e| format!("read quarantine row: {}", e))?;
                if payload_has_out_of_range_score(&payload) {
                    ids.push(id);
                }
            }
            ids
        };

        let mut marked: u64 = 0;
        let guard = self.lock();
        for id in candidates {
            let n = guard
                .execute(
                    "UPDATE rl_events
                        SET quarantined_at = ?1, quarantine_reason = ?2
                      WHERE id = ?3 AND quarantined_at IS NULL",
                    params![now_ms, QUARANTINE_REASON_SCORE_OUT_OF_RANGE, id],
                )
                .map_err(|e| format!("mark quarantine row {}: {}", id, e))?;
            marked += n as u64;
        }
        Ok(marked)
    }

    /// RL-14: run [`Self::backfill_quarantine_out_of_range`] exactly once
    /// per launcher.db, guarded by the `rl_events.quarantine_backfill_v1`
    /// app_state key. Soft-fail: any error logs + leaves the guard UNSET so
    /// the next open retries (the pass is idempotent either way). Called
    /// from `Db::open` (both the launcher and the hub route through it).
    pub fn run_quarantine_backfill_once(&self) {
        match self.app_state_get(QUARANTINE_BACKFILL_STATE_KEY) {
            Ok(Some(_)) => return, // already ran on this DB
            Ok(None) => {}
            Err(e) => {
                eprintln!("[launcher-db] quarantine backfill guard read failed: {}", e);
                return; // no positive confirmation → do nothing (conservative)
            }
        }
        let now_ms = chrono::Utc::now().timestamp_millis();
        match self.backfill_quarantine_out_of_range(now_ms) {
            Ok(marked) => {
                if marked > 0 {
                    eprintln!(
                        "[launcher-db] RL-14 quarantine backfill: marked {} \
                         historical out-of-range rl_events row(s)",
                        marked
                    );
                }
                if let Err(e) = self.app_state_set(QUARANTINE_BACKFILL_STATE_KEY, "done") {
                    eprintln!("[launcher-db] quarantine backfill guard write failed: {}", e);
                }
            }
            Err(e) => {
                eprintln!("[launcher-db] RL-14 quarantine backfill failed (will retry next open): {}", e);
            }
        }
    }
}

/// RL-14: does this payload carry any node with `score > 1.0`?
///
/// Pure function over the raw payload text. Unparseable JSON, a missing /
/// non-array `nodes`, or non-numeric scores all return `false` — we only
/// convict on positive evidence. Scores exactly 1.0 are IN range (the F-E
/// clamp emits 1.0 legitimately).
fn payload_has_out_of_range_score(payload_json: &str) -> bool {
    let parsed: serde_json::Value = match serde_json::from_str(payload_json) {
        Ok(v) => v,
        Err(_) => return false,
    };
    let nodes = match parsed.get("nodes").and_then(|n| n.as_array()) {
        Some(a) => a,
        None => return false,
    };
    nodes.iter().any(|n| {
        n.get("score")
            .and_then(|s| s.as_f64())
            .map(|s| s > 1.0)
            .unwrap_or(false)
    })
}

#[cfg(test)]
mod tests {
    use super::super::Db;
    use super::{QUARANTINE_BACKFILL_STATE_KEY, QUARANTINE_REASON_SCORE_OUT_OF_RANGE};

    fn fresh_db() -> Db {
        // In-memory DB with all migrations applied (including migration 025
        // which creates `rl_events`).
        Db::open_in_memory().expect("in-memory db")
    }

    #[test]
    fn insert_returns_rowid() {
        let db = fresh_db();
        let id = db
            .insert_rl_event(
                "retrieval",
                3,
                1_700_000_000_000,
                None,
                Some("VCO_dev"),
                "task-abc",
                Some("mcp_interactive"),
                Some("qwen3"),
                Some(1024),
                Some("qwen3-embedding:0.6b"),
                r#"{"event":"retrieval","schema_version":3}"#,
            )
            .expect("insert");
        assert_eq!(id, 1);
    }

    #[test]
    fn list_returns_inserted_rows_newest_first() {
        let db = fresh_db();
        for i in 0..3 {
            db.insert_rl_event(
                "retrieval",
                3,
                1_700_000_000_000 + i,
                None,
                Some("VCO_dev"),
                &format!("task-{}", i),
                Some("mcp_interactive"),
                Some("qwen3"),
                Some(1024),
                Some("qwen3-embedding:0.6b"),
                r#"{"event":"retrieval"}"#,
            )
            .unwrap();
        }
        let rows = db.list_rl_events(None, None, None, None, 10, false).unwrap();
        assert_eq!(rows.len(), 3);
        // Newest-first ordering.
        assert_eq!(rows[0].task_id, "task-2");
        assert_eq!(rows[2].task_id, "task-0");
    }

    #[test]
    fn filter_by_event_type() {
        let db = fresh_db();
        db.insert_rl_event(
            "retrieval", 3, 1, None, None, "t1", None, None, None, None, "{}",
        )
        .unwrap();
        db.insert_rl_event(
            "citation", 3, 2, None, None, "t1", None, None, None, None, "{}",
        )
        .unwrap();
        let cit = db
            .list_rl_events(None, Some("citation"), None, None, 10, false)
            .unwrap();
        assert_eq!(cit.len(), 1);
        assert_eq!(cit[0].event_type, "citation");
    }

    #[test]
    fn count_matches_filter() {
        let db = fresh_db();
        for i in 0..5 {
            db.insert_rl_event(
                if i % 2 == 0 { "retrieval" } else { "citation" },
                3,
                1_000 + i,
                None,
                None,
                &format!("task-{}", i),
                None,
                None,
                None,
                None,
                "{}",
            )
            .unwrap();
        }
        assert_eq!(db.count_rl_events(None, None, None, None).unwrap(), 5);
        assert_eq!(
            db.count_rl_events(None, Some("retrieval"), None, None).unwrap(),
            3
        );
        assert_eq!(
            db.count_rl_events(None, Some("citation"), None, None).unwrap(),
            2
        );
    }

    #[test]
    fn since_ms_filter_excludes_older_rows() {
        let db = fresh_db();
        db.insert_rl_event(
            "retrieval", 3, 100, None, None, "t1", None, None, None, None, "{}",
        )
        .unwrap();
        db.insert_rl_event(
            "retrieval", 3, 200, None, None, "t2", None, None, None, None, "{}",
        )
        .unwrap();
        db.insert_rl_event(
            "retrieval", 3, 300, None, None, "t3", None, None, None, None, "{}",
        )
        .unwrap();
        let recent = db
            .list_rl_events(None, None, Some(150), None, 10, false)
            .unwrap();
        assert_eq!(recent.len(), 2);
        let count = db.count_rl_events(None, None, Some(150), None).unwrap();
        assert_eq!(count, 2);
    }

    /// Helper: insert one event with explicit ts + optional project_id.
    fn insert_at(db: &Db, ts: i64, project_id: Option<&str>, task: &str) -> i64 {
        db.insert_rl_event(
            "retrieval", 3, ts, project_id, None, task, None, None, None, None, "{}",
        )
        .unwrap()
    }

    /// Helper: seed a `projects` row so an rl_events insert with that
    /// `project_id` satisfies the FK constraint (project_id → projects.id).
    fn seed_project(db: &Db, id: &str) {
        use crate::db::models::ProjectHost;
        db.insert_project(
            id,
            &format!("Project {id}"),
            &format!("/tmp/project-{id}"),
            ProjectHost::Base,
            &format!("project-{id}"),
        )
        .expect("insert project");
    }

    #[test]
    fn prune_cutoff_deletes_older_keeps_newer() {
        let db = fresh_db();
        insert_at(&db, 100, None, "old-1");
        insert_at(&db, 150, None, "old-2");
        insert_at(&db, 200, None, "new-1");
        insert_at(&db, 300, None, "new-2");
        // Cutoff 200 → delete ts < 200 (the two ts=100,150 rows).
        let deleted = db.prune_rl_events(Some(200), None, None).unwrap();
        assert_eq!(deleted, 2);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 2);
        // Boundary row ts==200 is retained (strict <).
        assert!(rows.iter().all(|r| r.ts_ms >= 200));
    }

    #[test]
    fn prune_max_rows_keeps_newest_globally() {
        let db = fresh_db();
        for i in 0..5 {
            insert_at(&db, 1_000 + i, None, &format!("t-{}", i));
        }
        // Keep the newest 2 rows → delete the other 3.
        let deleted = db.prune_rl_events(None, Some(2), None).unwrap();
        assert_eq!(deleted, 3);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 2);
        // Newest kept: ts 1004 and 1003.
        assert_eq!(rows[0].task_id, "t-4");
        assert_eq!(rows[1].task_id, "t-3");
    }

    #[test]
    fn prune_project_scoping_leaves_other_projects_untouched() {
        let db = fresh_db();
        seed_project(&db, "proj-a");
        seed_project(&db, "proj-b");
        insert_at(&db, 100, Some("proj-a"), "a-old");
        insert_at(&db, 300, Some("proj-a"), "a-new");
        insert_at(&db, 100, Some("proj-b"), "b-old");
        insert_at(&db, 300, Some("proj-b"), "b-new");
        // Prune proj-a older-than-200 only.
        let deleted = db.prune_rl_events(Some(200), None, Some("proj-a")).unwrap();
        assert_eq!(deleted, 1);
        // proj-a lost its old row; proj-b fully intact.
        let a = db
            .list_rl_events(Some("proj-a"), None, None, None, 100, false)
            .unwrap();
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].task_id, "a-new");
        let b = db
            .list_rl_events(Some("proj-b"), None, None, None, 100, false)
            .unwrap();
        assert_eq!(b.len(), 2);
    }

    #[test]
    fn prune_project_scoping_excludes_null_project_rows() {
        let db = fresh_db();
        seed_project(&db, "proj-a");
        insert_at(&db, 100, Some("proj-a"), "a-old");
        insert_at(&db, 100, None, "null-old");
        // Scoped prune of proj-a must NOT touch the NULL-project row.
        let deleted = db.prune_rl_events(Some(200), None, Some("proj-a")).unwrap();
        assert_eq!(deleted, 1);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].task_id, "null-old");
        assert!(rows[0].project_id.is_none());
    }

    #[test]
    fn prune_both_bounds_together() {
        let db = fresh_db();
        // ts: 100,150 (old), 200..=204 (newer). Cutoff 200 removes 2 old.
        insert_at(&db, 100, None, "old-1");
        insert_at(&db, 150, None, "old-2");
        for i in 0..5 {
            insert_at(&db, 200 + i, None, &format!("keep-{}", i));
        }
        // Cutoff 200 deletes the 2 old rows; then max_rows=3 keeps newest 3
        // of the surviving 5 → deletes 2 more. Total 4.
        let deleted = db.prune_rl_events(Some(200), Some(3), None).unwrap();
        assert_eq!(deleted, 4);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].task_id, "keep-4");
        assert_eq!(rows[2].task_id, "keep-2");
    }

    #[test]
    fn prune_both_none_is_noop_returns_zero() {
        // The critical safety test: no cutoff + no max_rows must delete NOTHING.
        let db = fresh_db();
        for i in 0..4 {
            insert_at(&db, 1_000 + i, None, &format!("t-{}", i));
        }
        let deleted = db.prune_rl_events(None, None, None).unwrap();
        assert_eq!(deleted, 0);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 4, "no-op prune must leave all rows intact");
    }

    #[test]
    fn prune_max_rows_zero_is_noop_not_corpus_wipe() {
        // v0.2.73 Stage-1 correctness SEV-2 #1: max_rows=Some(0) must be a NO-OP,
        // NOT a "keep zero, delete all". LIMIT 0 -> empty keep-set -> id NOT IN ()
        // would wipe the ENTIRE corpus. _DEFAULT_MAX_ROWS=0 ("disabled") makes 0
        // a live expected value; the deletion authority must not delete-all on it.
        let db = fresh_db();
        for i in 0..5 {
            insert_at(&db, 2_000 + i, None, &format!("z-{}", i));
        }
        let deleted = db.prune_rl_events(None, Some(0), None).unwrap();
        assert_eq!(deleted, 0, "max_rows=0 must delete NOTHING (row-cap disabled)");
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 5, "max_rows=0 must leave the whole corpus intact");
    }

    #[test]
    fn prune_max_rows_negative_is_noop() {
        // Same guard, negative row-cap: never delete-all.
        let db = fresh_db();
        for i in 0..3 {
            insert_at(&db, 3_000 + i, None, &format!("n-{}", i));
        }
        let deleted = db.prune_rl_events(None, Some(-1), None).unwrap();
        assert_eq!(deleted, 0);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 3);
    }

    #[test]
    fn prune_max_rows_larger_than_count_deletes_nothing() {
        let db = fresh_db();
        for i in 0..3 {
            insert_at(&db, 1_000 + i, None, &format!("t-{}", i));
        }
        // Keep 100 but only 3 exist → nothing to delete.
        let deleted = db.prune_rl_events(None, Some(100), None).unwrap();
        assert_eq!(deleted, 0);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 3);
    }

    #[test]
    fn nullable_project_id_round_trips() {
        let db = fresh_db();
        let id = db
            .insert_rl_event(
                "retrieval",
                3,
                1,
                None, // free-tier: no project_id
                Some("workspace-slug"),
                "task-free",
                None,
                None,
                None,
                None,
                "{}",
            )
            .unwrap();
        let rows = db.list_rl_events(None, None, None, None, 10, false).unwrap();
        assert_eq!(rows[0].id, id);
        assert!(rows[0].project_id.is_none());
        assert_eq!(rows[0].project_name.as_deref(), Some("workspace-slug"));
    }

    // ─── RL-14 (v0.2.75): quarantine marker ─────────────────────────────

    /// Helper: insert one event with an explicit payload.
    fn insert_payload(db: &Db, event_type: &str, task: &str, payload: &str) -> i64 {
        db.insert_rl_event(
            event_type, 3, 1_000, None, None, task, None, None, None, None, payload,
        )
        .unwrap()
    }

    const POISONED: &str = r#"{"nodes":[{"title":"A","score":10.37},{"title":"B","score":0.4}]}"#;
    const CLEAN: &str = r#"{"nodes":[{"title":"A","score":0.91},{"title":"B","score":1.0}]}"#;

    #[test]
    fn backfill_marks_out_of_range_and_leaves_in_range_alone() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned", POISONED);
        insert_payload(&db, "retrieval", "clean", CLEAN);
        // score exactly 1.0 is IN range (the F-E clamp emits it legitimately).
        insert_payload(&db, "retrieval", "boundary", r#"{"nodes":[{"score":1.0}]}"#);
        // Malformed payload: never convicted (skip softly).
        insert_payload(&db, "retrieval", "malformed", "not json {");
        // Citation events carry no nodes[].score contract → never scanned.
        insert_payload(&db, "citation", "citation", POISONED);

        let marked = db.backfill_quarantine_out_of_range(9_999).unwrap();
        assert_eq!(marked, 1, "exactly the poisoned retrieval row is marked");

        let all = db.list_rl_events(None, None, None, None, 100, true).unwrap();
        let poisoned = all.iter().find(|r| r.task_id == "poisoned").unwrap();
        assert_eq!(poisoned.quarantined_at, Some(9_999));
        assert_eq!(
            poisoned.quarantine_reason.as_deref(),
            Some(QUARANTINE_REASON_SCORE_OUT_OF_RANGE)
        );
        for task in ["clean", "boundary", "malformed", "citation"] {
            let row = all.iter().find(|r| r.task_id == task).unwrap();
            assert!(
                row.quarantined_at.is_none(),
                "{} must be left alone",
                task
            );
        }
    }

    #[test]
    fn backfill_is_idempotent() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned", POISONED);
        assert_eq!(db.backfill_quarantine_out_of_range(1_111).unwrap(), 1);
        // Second pass: nothing new to mark, timestamp NOT rewritten.
        assert_eq!(db.backfill_quarantine_out_of_range(2_222).unwrap(), 0);
        let rows = db.list_rl_events(None, None, None, None, 10, true).unwrap();
        assert_eq!(rows[0].quarantined_at, Some(1_111), "first mark timestamp survives");
    }

    #[test]
    fn quarantined_rows_excluded_from_corpus_reads_by_default() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned", POISONED);
        insert_payload(&db, "retrieval", "clean", CLEAN);
        db.backfill_quarantine_out_of_range(5_000).unwrap();

        // Default (training) read: poisoned row invisible.
        let corpus = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(corpus.len(), 1);
        assert_eq!(corpus[0].task_id, "clean");

        // Inspection read: both visible.
        let full = db.list_rl_events(None, None, None, None, 100, true).unwrap();
        assert_eq!(full.len(), 2);

        // Count filters: None = all (badge unchanged), Some(true) = doctor's.
        assert_eq!(db.count_rl_events(None, None, None, None).unwrap(), 2);
        assert_eq!(db.count_rl_events(None, None, None, Some(true)).unwrap(), 1);
        assert_eq!(db.count_rl_events(None, None, None, Some(false)).unwrap(), 1);
    }

    #[test]
    fn run_once_guard_prevents_second_pass() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned-1", POISONED);
        db.run_quarantine_backfill_once();
        assert_eq!(
            db.count_rl_events(None, None, None, Some(true)).unwrap(),
            1,
            "first run marks the historical row"
        );
        // A NEW poisoned row after the one-time pass (can't happen in
        // production — the writer clamp blocks it — but pins the guard).
        insert_payload(&db, "retrieval", "poisoned-2", POISONED);
        db.run_quarantine_backfill_once();
        assert_eq!(
            db.count_rl_events(None, None, None, Some(true)).unwrap(),
            1,
            "guarded second run must not scan again"
        );
        assert!(db
            .app_state_get(QUARANTINE_BACKFILL_STATE_KEY)
            .unwrap()
            .is_some());
    }

    #[test]
    fn payload_scanner_only_convicts_on_positive_evidence() {
        assert!(super::payload_has_out_of_range_score(POISONED));
        assert!(!super::payload_has_out_of_range_score(CLEAN));
        assert!(!super::payload_has_out_of_range_score("not json {"));
        assert!(!super::payload_has_out_of_range_score("{}"));
        assert!(!super::payload_has_out_of_range_score(r#"{"nodes":"oops"}"#));
        assert!(!super::payload_has_out_of_range_score(r#"{"nodes":[{"score":"high"}]}"#));
    }
}
