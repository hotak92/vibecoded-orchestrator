//! Append-only change log for cross-tab/cross-window invalidation.
//!
//! Every mutating command writes one row here via `Db::log_change`. The
//! frontend can poll `Db::changes_since(seq)` to discover what's stale
//! without comparing every row in every table.
//!
//! v1 design: poll-only. The frontend asks "what's new since seq N" every
//! few seconds and refetches the affected tables. A future v2 may upgrade
//! to a Tauri event push, but polling is simpler, correct under multiple
//! concurrent windows, and good enough at human-interaction frequencies.
//!
//! Storage: a single SQLite table with a monotonically increasing
//! `seq` (rowid alias). The launcher prunes rows older than 24h on
//! startup so the table stays small.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::Db;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ChangeRow {
    pub seq: i64,
    pub table_name: String,
    pub op: String, // 'insert' | 'update' | 'delete' | 'bulk'
    pub key: Option<String>,
    pub project_id: Option<String>,
    pub created_at: i64,
}

impl Db {
    /// Idempotent — creates the table on first call. Cheap; called from
    /// `Db::open` after migrations.
    pub fn ensure_change_log(&self) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute_batch(
                "CREATE TABLE IF NOT EXISTS change_log (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name   TEXT NOT NULL,
                    op           TEXT NOT NULL,
                    key          TEXT,
                    project_id   TEXT,
                    created_at   INTEGER NOT NULL
                 );
                 CREATE INDEX IF NOT EXISTS idx_change_log_created ON change_log(created_at);
                 CREATE INDEX IF NOT EXISTS idx_change_log_project ON change_log(project_id);",
            )
            .map_err(|e| format!("create change_log: {}", e))?;
        Ok(())
    }

    /// Append a change record. Best-effort: a logging failure must not
    /// fail the underlying mutation, so callers should `let _ = …;`.
    pub fn log_change(
        &self,
        table_name: &str,
        op: &str,
        key: Option<&str>,
        project_id: Option<&str>,
    ) -> Result<i64, String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO change_log (table_name, op, key, project_id, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![table_name, op, key, project_id, now],
            )
            .map_err(|e| format!("insert change_log: {}", e))?;
        Ok(guard.last_insert_rowid())
    }

    /// Latest seq the launcher has seen. Frontend uses this as the
    /// starting cursor on first call.
    pub fn current_change_seq(&self) -> Result<i64, String> {
        let guard = self.lock();
        let seq: Option<i64> = guard
            .query_row(
                "SELECT MAX(seq) FROM change_log",
                [],
                |row| row.get::<_, Option<i64>>(0),
            )
            .optional()
            .map_err(|e| format!("read current_change_seq: {}", e))?
            .flatten();
        Ok(seq.unwrap_or(0))
    }

    /// Returns all rows with seq > `since`, capped at 500. The frontend
    /// is expected to poll often enough that the cap is rarely hit; if it
    /// IS hit, the caller should treat the response as "everything is
    /// stale" and refetch wholesale.
    pub fn changes_since(&self, since: i64) -> Result<Vec<ChangeRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT seq, table_name, op, key, project_id, created_at
                 FROM change_log
                 WHERE seq > ?1
                 ORDER BY seq ASC
                 LIMIT 500",
            )
            .map_err(|e| format!("prepare changes_since: {}", e))?;
        let rows = stmt
            .query_map(params![since], |row| {
                Ok(ChangeRow {
                    seq: row.get(0)?,
                    table_name: row.get(1)?,
                    op: row.get(2)?,
                    key: row.get(3)?,
                    project_id: row.get(4)?,
                    created_at: row.get(5)?,
                })
            })
            .map_err(|e| format!("query changes_since: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect changes: {}", e))
    }

    /// Delete change_log rows older than `cutoff_ms`. Intended for
    /// startup cleanup so the table doesn't grow unbounded across
    /// long-lived installs.
    pub fn prune_change_log(&self, cutoff_ms: i64) -> Result<usize, String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM change_log WHERE created_at < ?1",
                params![cutoff_ms],
            )
            .map_err(|e| format!("prune change_log: {}", e))
    }
}
