// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Per-project read-only filesystem paths contributing to a project's
//! codegraph collection (v0.2.47, migration 026).
//!
//! Use case: index a sibling clone (e.g. `vibecoded-orchestrator/`) into
//! the active project's codegraph without making it a launcher project.
//! Files under enabled extra paths flow into the SAME
//! `<prefix>_CodeFunction / _CodeClass / _CodeAPI / _CodeModule /
//! _CodeInteraction` collections that the project's own repo populates,
//! so `search_code_graph()` queries against the project return entries
//! from both sources interleaved.
//!
//! Schema: `migrations/026_project_codegraph_extra_paths.sql`.
//! Tauri command surface: `commands::project_codegraph_extras` in the
//! launcher crate (validation, audit, analyzer dispatch). The hub
//! resolver reads enabled rows via `list_codegraph_extras` to populate
//! the additive `code_graph_extra_paths` field on the
//! `/api/v1/projects/{id}/config` response so hooks + the bash/ps1/
//! python resolver clients don't talk to SQLite directly.
//!
//! This file is pure DB — canonicalisation, audit logging, and analyzer
//! invocation live one layer up. Path strings here are taken verbatim
//! (the Tauri command boundary is responsible for ensuring they're
//! already absolute + canonicalised + cross-platform-normalised).
//!
//! Design notes:
//!   * Rows are keyed by (project_id, path) — a path can be an extra
//!     for multiple projects, but each (project, path) pair appears at
//!     most once. The PRIMARY KEY enforces this.
//!   * `enabled = 0` keeps the row for history (preserves label +
//!     last_indexed_* timestamps) while telling every consumer to treat
//!     it as absent. Filtering happens at read time; mutators don't
//!     auto-prune disabled rows.
//!   * `last_indexed_at` / `last_indexed_commit` are updated by the
//!     Tauri analyzer-dispatch flow (single dedicated mutator
//!     `update_last_indexed`). Pure-DB readers (`list_extras`) return
//!     them verbatim; callers decide whether to display "never" for
//!     `None`.
//!   * No `last_indexed_at DESC` sort — the canonical list order is
//!     `added_at DESC` (newest extras first) so the UI's stable row
//!     ordering matches the user's add sequence.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

// ─── Row type ─────────────────────────────────────────────────────────────

/// One row in `project_codegraph_extra_paths`. Mirrors the migration-026
/// schema. Serialised over Tauri IPC as-is; the launcher GUI's Identity
/// tab "Extra codegraph paths" panel renders the fields.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CodegraphExtraPathRow {
    /// FK to projects(id). Owns the row via ON DELETE CASCADE.
    pub project_id: String,
    /// Absolute, canonicalised at add time. Cross-platform storage form:
    /// forward slashes throughout (Windows backslashes converted to `/`
    /// before INSERT). No trailing separator.
    pub path: String,
    /// Optional UI label. NULL → frontend falls back to file basename.
    pub label: Option<String>,
    /// Unix epoch millis when the row was first inserted.
    pub added_at: i64,
    /// Unix epoch millis at the most recent successful analyze pass.
    /// `None` until the first sync runs.
    pub last_indexed_at: Option<i64>,
    /// Git SHA at the most recent analyze pass. `None` when the extra
    /// path is not a git repo OR no analyze has run yet. Used by
    /// `--since-commit` for incremental analyze of this single path.
    pub last_indexed_commit: Option<String>,
    /// `true` = active (resolver includes it, hooks match against it).
    /// `false` = soft-disabled (kept for history; treated as absent
    /// by every consumer).
    pub enabled: bool,
}

// ═══════════════════════════════════════════════════════════════════════
// CRUD
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// List every extra-path row for a project, newest-first by `added_at`.
    /// Returns ALL rows (enabled + disabled) — filtering disabled is the
    /// caller's responsibility because (a) the GUI wants to show disabled
    /// rows so the user can re-enable them and (b) the hub resolver
    /// applies its own enabled-only filter before responding.
    pub fn list_codegraph_extras(
        &self,
        project_id: &str,
    ) -> Result<Vec<CodegraphExtraPathRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, path, label, added_at,
                        last_indexed_at, last_indexed_commit, enabled
                 FROM project_codegraph_extra_paths
                 WHERE project_id = ?1
                 ORDER BY added_at DESC, path ASC",
            )
            .map_err(|e| format!("prepare list_codegraph_extras: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], row_to_extra)
            .map_err(|e| format!("query list_codegraph_extras: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_codegraph_extras: {}", e))
    }

    /// List ONLY enabled extras for a project. Used by the hub resolver
    /// and the reindex-after-extras-change sweep. Same order as the
    /// general list (`added_at DESC`).
    pub fn list_enabled_codegraph_extras(
        &self,
        project_id: &str,
    ) -> Result<Vec<CodegraphExtraPathRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, path, label, added_at,
                        last_indexed_at, last_indexed_commit, enabled
                 FROM project_codegraph_extra_paths
                 WHERE project_id = ?1 AND enabled = 1
                 ORDER BY added_at DESC, path ASC",
            )
            .map_err(|e| format!("prepare list_enabled_codegraph_extras: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], row_to_extra)
            .map_err(|e| format!("query list_enabled_codegraph_extras: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_enabled_codegraph_extras: {}", e))
    }

    /// Get a single extra-path row by (project_id, path). Returns
    /// `None` if the row is absent. Used by the toggle/remove/sync
    /// commands to confirm the row exists before mutating.
    pub fn get_codegraph_extra(
        &self,
        project_id: &str,
        path: &str,
    ) -> Result<Option<CodegraphExtraPathRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, path, label, added_at,
                        last_indexed_at, last_indexed_commit, enabled
                 FROM project_codegraph_extra_paths
                 WHERE project_id = ?1 AND path = ?2",
                params![project_id, path],
                row_to_extra,
            )
            .optional()
            .map_err(|e| format!("get_codegraph_extra: {}", e))
    }

    /// Insert a new extra-path row. Returns the canonical row (so the
    /// caller doesn't need a follow-up SELECT to get `added_at`).
    ///
    /// Caller MUST canonicalise `path` BEFORE calling this — absolute,
    /// canonicalised via `Path::canonicalize`, forward-slash form,
    /// trailing separator stripped. This module performs NO path
    /// validation; SQL would silently store any string.
    ///
    /// Returns an error if a row with the same (project_id, path)
    /// already exists (PRIMARY KEY constraint). The Tauri command
    /// layer maps the "UNIQUE constraint failed" message to a
    /// friendly "duplicate path" error before surfacing.
    pub fn add_codegraph_extra(
        &self,
        project_id: &str,
        path: &str,
        label: Option<&str>,
    ) -> Result<CodegraphExtraPathRow, String> {
        let now = Utc::now().timestamp_millis();
        {
            let guard = self.lock();
            guard
                .execute(
                    "INSERT INTO project_codegraph_extra_paths
                     (project_id, path, label, added_at, enabled)
                     VALUES (?1, ?2, ?3, ?4, 1)",
                    params![project_id, path, label, now],
                )
                .map_err(|e| format!("add_codegraph_extra: {}", e))?;
        }
        Ok(CodegraphExtraPathRow {
            project_id: project_id.to_string(),
            path: path.to_string(),
            label: label.map(str::to_string),
            added_at: now,
            last_indexed_at: None,
            last_indexed_commit: None,
            enabled: true,
        })
    }

    /// Remove an extra-path row. Returns the number of rows deleted
    /// (0 = the row didn't exist; 1 = removed). The caller decides
    /// whether 0 is an error.
    pub fn remove_codegraph_extra(
        &self,
        project_id: &str,
        path: &str,
    ) -> Result<usize, String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_codegraph_extra_paths
                 WHERE project_id = ?1 AND path = ?2",
                params![project_id, path],
            )
            .map_err(|e| format!("remove_codegraph_extra: {}", e))
    }

    /// Toggle a row's `enabled` flag. Returns the number of rows
    /// updated (0 = row absent; 1 = flipped). The caller maps 0 to
    /// "row not found" at the command layer.
    ///
    /// `last_indexed_*` columns are preserved across the toggle so
    /// disabled rows retain their history (re-enabling a row leaves
    /// `--since-commit` workable from the pre-disable SHA).
    pub fn set_codegraph_extra_enabled(
        &self,
        project_id: &str,
        path: &str,
        enabled: bool,
    ) -> Result<usize, String> {
        let guard = self.lock();
        guard
            .execute(
                "UPDATE project_codegraph_extra_paths
                 SET enabled = ?1
                 WHERE project_id = ?2 AND path = ?3",
                params![enabled as i32, project_id, path],
            )
            .map_err(|e| format!("set_codegraph_extra_enabled: {}", e))
    }

    /// Record a successful analyze pass for a single extra path.
    /// Updates `last_indexed_at` (always) and `last_indexed_commit`
    /// (when the caller resolved a SHA for the path's git HEAD;
    /// non-git roots pass `None` and the column stays NULL or its
    /// prior value — we explicitly set NULL on a non-git pass so a
    /// repo that loses its `.git` between analyzes doesn't carry a
    /// stale SHA).
    ///
    /// Returns the number of rows updated. 0 means the (project,
    /// path) row vanished between the analyzer invocation and this
    /// post-write — the caller should treat that as the row having
    /// been removed mid-analyze and skip the timestamp write rather
    /// than re-inserting (the analyze was speculative; we don't want
    /// to resurrect a deleted row).
    pub fn update_codegraph_extra_last_indexed(
        &self,
        project_id: &str,
        path: &str,
        last_indexed_at: i64,
        last_indexed_commit: Option<&str>,
    ) -> Result<usize, String> {
        let guard = self.lock();
        guard
            .execute(
                "UPDATE project_codegraph_extra_paths
                 SET last_indexed_at = ?1, last_indexed_commit = ?2
                 WHERE project_id = ?3 AND path = ?4",
                params![
                    last_indexed_at,
                    last_indexed_commit,
                    project_id,
                    path,
                ],
            )
            .map_err(|e| format!("update_codegraph_extra_last_indexed: {}", e))
    }
}

// ─── Row mapping ──────────────────────────────────────────────────────────

fn row_to_extra(r: &rusqlite::Row) -> rusqlite::Result<CodegraphExtraPathRow> {
    let enabled_i: i32 = r.get(6)?;
    Ok(CodegraphExtraPathRow {
        project_id: r.get(0)?,
        path: r.get(1)?,
        label: r.get(2)?,
        added_at: r.get(3)?,
        last_indexed_at: r.get(4)?,
        last_indexed_commit: r.get(5)?,
        enabled: enabled_i != 0,
    })
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn make_db_with_project(project_id: &str, name: &str) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug(name).unwrap();
        let folder = if cfg!(windows) {
            format!(r"C:\tmp\{}", project_id)
        } else {
            format!("/tmp/{}", project_id)
        };
        db.insert_project(project_id, name, &folder, ProjectHost::Base, &slug)
            .unwrap();
        db
    }

    // ─── Add / list ─────────────────────────────────────────────────────

    #[test]
    fn add_and_list_round_trip() {
        let db = make_db_with_project("p1", "Acme");
        let row = db
            .add_codegraph_extra("p1", "/opt/sibling-repo", Some("sibling"))
            .expect("add");
        assert_eq!(row.project_id, "p1");
        assert_eq!(row.path, "/opt/sibling-repo");
        assert_eq!(row.label.as_deref(), Some("sibling"));
        assert!(row.enabled);
        assert!(row.last_indexed_at.is_none());
        assert!(row.last_indexed_commit.is_none());
        assert!(row.added_at > 0);

        let listed = db.list_codegraph_extras("p1").unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].path, "/opt/sibling-repo");
        assert_eq!(listed[0].label.as_deref(), Some("sibling"));
    }

    #[test]
    fn add_without_label_returns_none_label() {
        let db = make_db_with_project("p1", "Acme");
        let row = db
            .add_codegraph_extra("p1", "/opt/no-label", None)
            .unwrap();
        assert!(row.label.is_none());
        let listed = db.list_codegraph_extras("p1").unwrap();
        assert!(listed[0].label.is_none());
    }

    #[test]
    fn add_duplicate_returns_unique_error() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/dup", None).unwrap();
        let err = db
            .add_codegraph_extra("p1", "/opt/dup", Some("retry"))
            .unwrap_err();
        // SQLite's "UNIQUE constraint failed" — the Tauri command maps
        // this to a friendly message; the DB-level test just asserts
        // the surface is recognisable.
        assert!(
            err.to_lowercase().contains("unique") || err.to_lowercase().contains("constraint"),
            "expected UNIQUE constraint error, got: {}",
            err
        );
    }

    #[test]
    fn list_orders_newest_first_by_added_at() {
        let db = make_db_with_project("p1", "Acme");
        // Insert with monotonically increasing added_at by virtue of
        // wall-clock; cheat the test with explicit timestamps so the
        // ordering is deterministic regardless of clock granularity.
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO project_codegraph_extra_paths
                 (project_id, path, label, added_at, enabled)
                 VALUES ('p1', '/opt/oldest', NULL, 100, 1),
                        ('p1', '/opt/middle', NULL, 200, 1),
                        ('p1', '/opt/newest', NULL, 300, 1)",
                [],
            )
            .unwrap();
        drop(guard);

        let listed = db.list_codegraph_extras("p1").unwrap();
        assert_eq!(listed.len(), 3);
        assert_eq!(listed[0].path, "/opt/newest");
        assert_eq!(listed[1].path, "/opt/middle");
        assert_eq!(listed[2].path, "/opt/oldest");
    }

    #[test]
    fn list_empty_for_unknown_project() {
        let db = make_db_with_project("p1", "Acme");
        let listed = db.list_codegraph_extras("nonexistent").unwrap();
        assert!(listed.is_empty());
    }

    // ─── get_codegraph_extra ────────────────────────────────────────────

    #[test]
    fn get_returns_existing_row() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/x", Some("xname")).unwrap();
        let row = db
            .get_codegraph_extra("p1", "/opt/x")
            .unwrap()
            .expect("row exists");
        assert_eq!(row.path, "/opt/x");
        assert_eq!(row.label.as_deref(), Some("xname"));
    }

    #[test]
    fn get_returns_none_for_missing_row() {
        let db = make_db_with_project("p1", "Acme");
        let row = db.get_codegraph_extra("p1", "/opt/ghost").unwrap();
        assert!(row.is_none());
    }

    // ─── Enabled toggle ─────────────────────────────────────────────────

    #[test]
    fn set_enabled_flips_flag() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/x", None).unwrap();
        assert!(db.get_codegraph_extra("p1", "/opt/x").unwrap().unwrap().enabled);

        let n = db.set_codegraph_extra_enabled("p1", "/opt/x", false).unwrap();
        assert_eq!(n, 1);
        assert!(!db.get_codegraph_extra("p1", "/opt/x").unwrap().unwrap().enabled);

        let n = db.set_codegraph_extra_enabled("p1", "/opt/x", true).unwrap();
        assert_eq!(n, 1);
        assert!(db.get_codegraph_extra("p1", "/opt/x").unwrap().unwrap().enabled);
    }

    #[test]
    fn set_enabled_returns_zero_for_missing_row() {
        let db = make_db_with_project("p1", "Acme");
        let n = db
            .set_codegraph_extra_enabled("p1", "/opt/ghost", false)
            .unwrap();
        assert_eq!(n, 0);
    }

    #[test]
    fn list_enabled_filters_out_disabled() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/a", None).unwrap();
        db.add_codegraph_extra("p1", "/opt/b", None).unwrap();
        db.add_codegraph_extra("p1", "/opt/c", None).unwrap();
        db.set_codegraph_extra_enabled("p1", "/opt/b", false).unwrap();

        let all = db.list_codegraph_extras("p1").unwrap();
        assert_eq!(all.len(), 3);
        let enabled = db.list_enabled_codegraph_extras("p1").unwrap();
        assert_eq!(enabled.len(), 2);
        let paths: Vec<&str> = enabled.iter().map(|r| r.path.as_str()).collect();
        assert!(paths.contains(&"/opt/a"));
        assert!(paths.contains(&"/opt/c"));
        assert!(!paths.contains(&"/opt/b"));
    }

    #[test]
    fn set_enabled_preserves_last_indexed_columns() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/x", None).unwrap();
        db.update_codegraph_extra_last_indexed("p1", "/opt/x", 1_700_000_000_000, Some("abc123"))
            .unwrap();

        // Disable → last_indexed_* must survive.
        db.set_codegraph_extra_enabled("p1", "/opt/x", false).unwrap();
        let row = db.get_codegraph_extra("p1", "/opt/x").unwrap().unwrap();
        assert!(!row.enabled);
        assert_eq!(row.last_indexed_at, Some(1_700_000_000_000));
        assert_eq!(row.last_indexed_commit.as_deref(), Some("abc123"));

        // Re-enable → still survives.
        db.set_codegraph_extra_enabled("p1", "/opt/x", true).unwrap();
        let row = db.get_codegraph_extra("p1", "/opt/x").unwrap().unwrap();
        assert!(row.enabled);
        assert_eq!(row.last_indexed_at, Some(1_700_000_000_000));
        assert_eq!(row.last_indexed_commit.as_deref(), Some("abc123"));
    }

    // ─── Remove ─────────────────────────────────────────────────────────

    #[test]
    fn remove_deletes_row() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/x", None).unwrap();
        let n = db.remove_codegraph_extra("p1", "/opt/x").unwrap();
        assert_eq!(n, 1);
        assert!(db.list_codegraph_extras("p1").unwrap().is_empty());
    }

    #[test]
    fn remove_returns_zero_for_missing_row() {
        let db = make_db_with_project("p1", "Acme");
        let n = db.remove_codegraph_extra("p1", "/opt/ghost").unwrap();
        assert_eq!(n, 0);
    }

    // ─── last_indexed_at / _commit ──────────────────────────────────────

    #[test]
    fn update_last_indexed_records_both_columns() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/x", None).unwrap();
        let n = db
            .update_codegraph_extra_last_indexed(
                "p1",
                "/opt/x",
                1_700_000_000_000,
                Some("deadbeef"),
            )
            .unwrap();
        assert_eq!(n, 1);
        let row = db.get_codegraph_extra("p1", "/opt/x").unwrap().unwrap();
        assert_eq!(row.last_indexed_at, Some(1_700_000_000_000));
        assert_eq!(row.last_indexed_commit.as_deref(), Some("deadbeef"));
    }

    #[test]
    fn update_last_indexed_with_none_commit_clears_prior_sha() {
        let db = make_db_with_project("p1", "Acme");
        db.add_codegraph_extra("p1", "/opt/x", None).unwrap();
        db.update_codegraph_extra_last_indexed("p1", "/opt/x", 1, Some("old-sha"))
            .unwrap();
        // Subsequent analyze on a non-git path: commit becomes NULL.
        db.update_codegraph_extra_last_indexed("p1", "/opt/x", 2, None)
            .unwrap();
        let row = db.get_codegraph_extra("p1", "/opt/x").unwrap().unwrap();
        assert_eq!(row.last_indexed_at, Some(2));
        assert!(
            row.last_indexed_commit.is_none(),
            "non-git follow-up analyze must clear the SHA so we don't \
             carry a stale value"
        );
    }

    #[test]
    fn update_last_indexed_returns_zero_for_missing_row() {
        let db = make_db_with_project("p1", "Acme");
        let n = db
            .update_codegraph_extra_last_indexed("p1", "/opt/ghost", 1, None)
            .unwrap();
        assert_eq!(n, 0);
    }

    // ─── Cascade on project delete ──────────────────────────────────────

    #[test]
    fn cascade_deletes_extras_when_project_dropped() {
        let db = make_db_with_project("pA", "A");
        let slug = db.generate_unique_slug("B").unwrap();
        db.insert_project(
            "pB",
            "B",
            if cfg!(windows) {
                r"C:\tmp\pB-cas-ext"
            } else {
                "/tmp/pB-cas-ext"
            },
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        db.add_codegraph_extra("pA", "/opt/x", None).unwrap();
        db.add_codegraph_extra("pA", "/opt/y", Some("ylabel")).unwrap();
        db.add_codegraph_extra("pB", "/opt/z", None).unwrap();

        // Delete project A — its 2 extras die; B's row survives.
        {
            let guard = db.lock();
            guard
                .execute("DELETE FROM projects WHERE id = ?1", params!["pA"])
                .unwrap();
        }
        assert!(db.list_codegraph_extras("pA").unwrap().is_empty());
        assert_eq!(db.list_codegraph_extras("pB").unwrap().len(), 1);
    }

    #[test]
    fn cascade_leaves_no_dangling_fks() {
        let db = make_db_with_project("pA", "A");
        db.add_codegraph_extra("pA", "/opt/x", None).unwrap();
        db.add_codegraph_extra("pA", "/opt/y", None).unwrap();

        {
            let guard = db.lock();
            guard
                .execute("DELETE FROM projects WHERE id = ?1", params!["pA"])
                .unwrap();
        }

        let guard = db.lock();
        let mut stmt = guard.prepare("PRAGMA foreign_key_check").unwrap();
        let orphans: Vec<String> = stmt
            .query_map([], |r| {
                let t: String = r.get(0)?;
                Ok(t)
            })
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();
        assert!(
            orphans.is_empty(),
            "PRAGMA foreign_key_check found dangling refs after \
             project delete: {:?}",
            orphans
        );
    }

    // ─── Per-project isolation ──────────────────────────────────────────

    #[test]
    fn extras_are_isolated_per_project() {
        let db = make_db_with_project("pA", "A");
        let slug = db.generate_unique_slug("B").unwrap();
        db.insert_project(
            "pB",
            "B",
            if cfg!(windows) {
                r"C:\tmp\pB-iso-ext"
            } else {
                "/tmp/pB-iso-ext"
            },
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        // Same path can be an extra for BOTH projects (PK is composite).
        db.add_codegraph_extra("pA", "/opt/shared-sibling", None)
            .unwrap();
        db.add_codegraph_extra("pB", "/opt/shared-sibling", Some("from-B"))
            .unwrap();

        let a = db.list_codegraph_extras("pA").unwrap();
        let b = db.list_codegraph_extras("pB").unwrap();
        assert_eq!(a.len(), 1);
        assert_eq!(b.len(), 1);
        assert!(a[0].label.is_none());
        assert_eq!(b[0].label.as_deref(), Some("from-B"));
    }

    // ─── Migration accepts the rows ─────────────────────────────────────

    #[test]
    fn migration_026_creates_the_table() {
        let db = Db::open_in_memory().expect("in-memory db");
        let guard = db.lock();
        let exists: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master
                 WHERE type = 'table' AND name = 'project_codegraph_extra_paths'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(exists, 1, "migration 026 must create the table");

        let max_v: u32 = guard
            .query_row(
                "SELECT COALESCE(MAX(version), 0) FROM _schema_migrations",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(max_v >= 26, "expected at least version 26, got {}", max_v);
    }
}
