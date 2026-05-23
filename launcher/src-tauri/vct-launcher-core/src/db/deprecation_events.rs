// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Module-deprecation surface — append-only audit + one-shot notification gate.
//!
//! Migration 018 (v0.2.31) ships two tables:
//!
//!   * `deprecation_events` — every transition observed by
//!     `apply_deprecation_state` (false → true OR true → false). Useful for
//!     "deprecated for N days" badge text and support-ticket triage.
//!     Append-only; rows are never UPDATEd in place.
//!   * `module_deprecation_seen` — single sentinel row per
//!     (project_id, module_id) pair. The launcher's desktop-notification
//!     code consults this to suppress re-fires across sessions.
//!
//! WRITER MODEL: this module exposes plain INSERT helpers and a single read
//! helper (`has_module_deprecation_been_seen`). Callers — the launcher's
//! `apply_deprecation_state` Tauri command — are responsible for sequencing:
//!
//!   1. Look up prior state (most-recent `deprecated` from `deprecation_events`).
//!   2. If the new state differs, append a row.
//!   3. On a false → true transition (and only then), insert a
//!      `module_deprecation_seen` row IF NOT EXISTS.
//!
//! Soft-fail discipline: every DB error is returned to the caller as a
//! `Result<_, String>`. The caller (`apply_deprecation_state`) logs +
//! continues so that a write hiccup in Layer 3 (audit) does NOT block
//! Layer 1 (GUI) or Layer 2 (env-var injection). See the spec at
//! `.claude/context/plans/rl-deprecation-warning-surface-spec-2026-05-23.md`
//! for the layering rationale.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};

use super::Db;

/// One observed deprecation transition. Rows are append-only.
#[derive(Debug, Clone)]
pub struct DeprecationEvent {
    pub id: i64,
    pub project_id: String,
    pub module_id: String,
    /// Unix epoch millis (matches the rest of the launcher DB schema).
    pub detected_at_ms: i64,
    pub deprecated: bool,
    pub message: Option<String>,
    pub eol_date: Option<String>,
    pub migration_url: Option<String>,
}

impl Db {
    /// Append a new transition row to `deprecation_events`. Always
    /// inserts — callers are expected to call this only when the
    /// `deprecated` state has actually flipped (use
    /// `get_last_deprecation_state` to decide).
    ///
    /// The `detected_at` column is set to `now()` server-side; callers
    /// cannot override it (audit-trail integrity).
    pub fn insert_deprecation_event(
        &self,
        project_id: &str,
        module_id: &str,
        deprecated: bool,
        message: Option<&str>,
        eol_date: Option<&str>,
        migration_url: Option<&str>,
    ) -> Result<i64, String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO deprecation_events
                    (project_id, module_id, detected_at, deprecated,
                     message, eol_date, migration_url)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![
                    project_id,
                    module_id,
                    now,
                    deprecated as i64,
                    message,
                    eol_date,
                    migration_url,
                ],
            )
            .map_err(|e| format!("insert deprecation_event: {}", e))?;
        Ok(guard.last_insert_rowid())
    }

    /// Read the most-recent transition row for a (project, module) pair.
    /// `Ok(None)` means "never observed" — the caller treats this as
    /// equivalent to `deprecated = false` for transition-detection.
    pub fn get_last_deprecation_state(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<Option<bool>, String> {
        let guard = self.lock();
        let row: Option<i64> = guard
            .query_row(
                "SELECT deprecated FROM deprecation_events
                  WHERE project_id = ?1 AND module_id = ?2
                  ORDER BY detected_at DESC, id DESC
                  LIMIT 1",
                params![project_id, module_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| format!("get_last_deprecation_state: {}", e))?;
        Ok(row.map(|i| i != 0))
    }

    /// List all transition rows for a (project, module) pair, oldest first.
    /// Used by the dashboard widget's "deprecated for N days" text.
    pub fn list_deprecation_events(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<Vec<DeprecationEvent>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, module_id, detected_at, deprecated,
                        message, eol_date, migration_url
                   FROM deprecation_events
                  WHERE project_id = ?1 AND module_id = ?2
                  ORDER BY detected_at ASC, id ASC",
            )
            .map_err(|e| format!("prepare list_deprecation_events: {}", e))?;
        let rows = stmt
            .query_map(params![project_id, module_id], |row| {
                Ok(DeprecationEvent {
                    id: row.get(0)?,
                    project_id: row.get(1)?,
                    module_id: row.get(2)?,
                    detected_at_ms: row.get(3)?,
                    deprecated: row.get::<_, i64>(4)? != 0,
                    message: row.get(5)?,
                    eol_date: row.get(6)?,
                    migration_url: row.get(7)?,
                })
            })
            .map_err(|e| format!("query list_deprecation_events: {}", e))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r.map_err(|e| format!("row list_deprecation_events: {}", e))?);
        }
        Ok(out)
    }

    /// Has the launcher already fired the one-shot desktop notification
    /// for this (project, module) pair? `true` means "do NOT re-fire".
    pub fn has_module_deprecation_been_seen(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM module_deprecation_seen
                  WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
                |row| row.get(0),
            )
            .map_err(|e| format!("has_module_deprecation_been_seen: {}", e))?;
        Ok(count > 0)
    }

    /// Mark a (project, module) pair as "notification fired". INSERT OR
    /// IGNORE so concurrent calls are safe — only the first wins.
    /// Returns `true` IFF this call actually inserted (= the caller should
    /// proceed with the desktop notification); `false` means an earlier
    /// call already marked it.
    pub fn mark_module_deprecation_seen(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<bool, String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        let changes = guard
            .execute(
                "INSERT OR IGNORE INTO module_deprecation_seen
                    (project_id, module_id, first_seen_at)
                 VALUES (?1, ?2, ?3)",
                params![project_id, module_id, now],
            )
            .map_err(|e| format!("mark_module_deprecation_seen: {}", e))?;
        Ok(changes > 0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn open_db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = "test-proj-dep".to_string();
        db.insert_project(
            &id,
            "Test Project",
            "/tmp/test-proj-dep",
            ProjectHost::Base,
            "test-project-dep",
        )
        .expect("insert project");
        (db, id)
    }

    #[test]
    fn empty_read_returns_none() {
        let (db, pid) = open_db_with_project();
        assert_eq!(
            db.get_last_deprecation_state(&pid, "vct-rl-reranker").unwrap(),
            None,
        );
    }

    #[test]
    fn insert_then_read_roundtrips() {
        let (db, pid) = open_db_with_project();
        db.insert_deprecation_event(
            &pid,
            "vct-rl-reranker",
            true,
            Some("End of life soon"),
            Some("2026-12-01"),
            Some("https://example.com/migrate"),
        )
        .unwrap();
        assert_eq!(
            db.get_last_deprecation_state(&pid, "vct-rl-reranker").unwrap(),
            Some(true),
        );
    }

    #[test]
    fn list_events_returns_ordered_history() {
        let (db, pid) = open_db_with_project();
        db.insert_deprecation_event(&pid, "vct-rl-reranker", true, None, None, None)
            .unwrap();
        db.insert_deprecation_event(&pid, "vct-rl-reranker", false, None, None, None)
            .unwrap();
        let events = db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap();
        assert_eq!(events.len(), 2);
        assert!(events[0].deprecated);
        assert!(!events[1].deprecated);
    }

    #[test]
    fn last_state_reflects_most_recent_transition() {
        let (db, pid) = open_db_with_project();
        db.insert_deprecation_event(&pid, "m1", true, None, None, None).unwrap();
        // Small spin to ensure timestamp ordering (id is the tie-breaker).
        db.insert_deprecation_event(&pid, "m1", false, None, None, None).unwrap();
        assert_eq!(db.get_last_deprecation_state(&pid, "m1").unwrap(), Some(false));
    }

    #[test]
    fn mark_seen_first_call_returns_true_second_returns_false() {
        let (db, pid) = open_db_with_project();
        assert!(!db.has_module_deprecation_been_seen(&pid, "m1").unwrap());
        assert!(db.mark_module_deprecation_seen(&pid, "m1").unwrap());
        assert!(db.has_module_deprecation_been_seen(&pid, "m1").unwrap());
        // Second mark must be a no-op AND report false.
        assert!(!db.mark_module_deprecation_seen(&pid, "m1").unwrap());
    }

    #[test]
    fn different_modules_keep_independent_seen_marks() {
        let (db, pid) = open_db_with_project();
        assert!(db.mark_module_deprecation_seen(&pid, "m1").unwrap());
        assert!(!db.has_module_deprecation_been_seen(&pid, "m2").unwrap());
        assert!(db.mark_module_deprecation_seen(&pid, "m2").unwrap());
    }

    #[test]
    fn cascade_delete_removes_dep_rows_with_project() {
        let (db, pid) = open_db_with_project();
        db.insert_deprecation_event(&pid, "m1", true, None, None, None).unwrap();
        db.mark_module_deprecation_seen(&pid, "m1").unwrap();
        // FK CASCADE: deleting the project drops dependent rows.
        {
            let g = db.lock();
            g.execute("DELETE FROM projects WHERE id = ?1", params![&pid]).unwrap();
        }
        assert_eq!(
            db.list_deprecation_events(&pid, "m1").unwrap().len(),
            0,
        );
        assert!(!db.has_module_deprecation_been_seen(&pid, "m1").unwrap());
    }
}
