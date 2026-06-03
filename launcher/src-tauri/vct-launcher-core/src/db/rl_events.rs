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

/// One RL event row. Matches the migration-025 schema.
///
/// `payload_json` carries the full v3 event JSON verbatim. The indexed
/// columns above are denormalized for SQL queries; callers that need
/// non-indexed fields (e.g. per-node `n_emb`, `linked_embs`, `cosine_sims`)
/// must parse `payload_json` themselves.
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
}

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
    pub fn list_rl_events(
        &self,
        project_id: Option<&str>,
        event_type: Option<&str>,
        since_ms: Option<i64>,
        until_ms: Option<i64>,
        limit: u32,
    ) -> Result<Vec<RlEvent>, String> {
        // Build the WHERE clause + params iteratively to keep the prepared
        // statement cache-friendly across common filter combinations.
        let mut sql = String::from(
            "SELECT id, event_type, schema_version, ts, project_id, project_name,
                    task_id, task_type, embedding_source, embedding_dim,
                    embedding_model, payload_json
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
    pub fn count_rl_events(
        &self,
        project_id: Option<&str>,
        event_type: Option<&str>,
        since_ms: Option<i64>,
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

        let guard = self.lock();
        let param_refs: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();
        let row: Option<i64> = guard
            .query_row(&sql, param_refs.as_slice(), |row| row.get(0))
            .optional()
            .map_err(|e| format!("count_rl_events: {}", e))?;
        Ok(row.unwrap_or(0))
    }
}

#[cfg(test)]
mod tests {
    use super::super::Db;

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
        let rows = db.list_rl_events(None, None, None, None, 10).unwrap();
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
            .list_rl_events(None, Some("citation"), None, None, 10)
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
        assert_eq!(db.count_rl_events(None, None, None).unwrap(), 5);
        assert_eq!(
            db.count_rl_events(None, Some("retrieval"), None).unwrap(),
            3
        );
        assert_eq!(
            db.count_rl_events(None, Some("citation"), None).unwrap(),
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
            .list_rl_events(None, None, Some(150), None, 10)
            .unwrap();
        assert_eq!(recent.len(), 2);
        let count = db.count_rl_events(None, None, Some(150)).unwrap();
        assert_eq!(count, 2);
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
        let rows = db.list_rl_events(None, None, None, None, 10).unwrap();
        assert_eq!(rows[0].id, id);
        assert!(rows[0].project_id.is_none());
        assert_eq!(rows[0].project_name.as_deref(), Some("workspace-slug"));
    }
}
