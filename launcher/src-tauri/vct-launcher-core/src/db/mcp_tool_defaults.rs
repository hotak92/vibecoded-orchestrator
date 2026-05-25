// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! `module_mcp_tool_defaults` table accessors (v0.2.34 Agent E — Phase 4
//! generalisation of the per-tool MCP allowlist mechanism).
//!
//! Schema: migration 023. One row per `(mcp_name, tool_name)` tracking
//! the wrapper's default-enabled state + an optional description. The
//! row is written at module-install time from the
//! `manifest.mcp_registration.tool_allowlist` block (see
//! `manifest::ToolAllowlistEntry`) and cleared on uninstall.
//!
//! Why a SEPARATE module from `diagrams.rs`:
//!   * The diagrams.rs file already mixes five concerns (project
//!     diagrams, snapshots, access grants, per-project tool grants,
//!     per-project modules). Adding a sixth (per-module/global tool
//!     defaults) blurs the boundary further.
//!   * `project_mcp_tool_grants` is PER-PROJECT (Phase 1.1 sibling
//!     keyed on project_id). `module_mcp_tool_defaults` is per-MCP/per-
//!     module (Phase 4 generalisation keyed on mcp_name). Different
//!     life-cycles, different ownership; keep them apart.
//!
//! Read path: the hub's `/mcp-tool-grants/{mcp_name}` route composes
//! the response by merging these defaults with explicit per-project
//! rows from `project_mcp_tool_grants`. See `mcp_tool_grants_api.rs`
//! for the merge rules (per-project ALWAYS wins; absent project rows
//! fall through to `default_enabled`).

use rusqlite::params;
use serde::{Deserialize, Serialize};

use super::Db;

/// One row in `module_mcp_tool_defaults`. Mirrors
/// `manifest::ToolAllowlistEntry` on the wire shape but adds the
/// `mcp_name` + `module_id` columns the manifest doesn't carry
/// (they're derived from the enclosing `mcp_registration` block + the
/// module's id at install time).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct McpToolDefault {
    pub mcp_name: String,
    pub tool_name: String,
    pub default_enabled: bool,
    pub description: Option<String>,
    pub module_id: String,
    pub registered_at: i64,
}

impl Db {
    /// Replace every row for `(mcp_name)` belonging to `module_id` with
    /// the supplied list. Transactional — concurrent reads see either
    /// the OLD set or the NEW set, never a torn mid-write state.
    ///
    /// The "reconcile on module update" contract from the v0.2.34 plan:
    /// new tools added in v0.2.8 of a module land here as fresh rows
    /// with their declared `default_enabled`; tools removed in v0.2.8
    /// disappear (manifest is the source of truth for defaults).
    /// Per-project overrides in `project_mcp_tool_grants` are NOT
    /// touched — they survive a module update and re-apply if the
    /// matching tool is reinstated.
    ///
    /// Returns the count of rows that landed (i.e. `entries.len()` on
    /// success). Empty `entries` clears the (mcp_name, module_id) slice
    /// — useful when a manifest update removes the `tool_allowlist`
    /// block entirely.
    pub fn reconcile_mcp_tool_defaults(
        &self,
        mcp_name: &str,
        module_id: &str,
        entries: &[(String, bool, Option<String>)],
        now_ms: i64,
    ) -> Result<usize, String> {
        let mut guard = self.lock();
        let tx = guard
            .transaction()
            .map_err(|e| format!("begin txn: {}", e))?;
        // First drop every row for this (mcp_name, module_id) pair.
        // Scoping the delete by module_id matters: two modules could in
        // theory both ship an MCP with the same `mcp_name` (e.g. a
        // future "diagrams-pro" supplanting bundled diagrams). The
        // delete-then-insert pattern keeps each module's row set
        // independent.
        tx.execute(
            "DELETE FROM module_mcp_tool_defaults
             WHERE mcp_name = ?1 AND module_id = ?2",
            params![mcp_name, module_id],
        )
        .map_err(|e| format!("delete prior defaults: {}", e))?;

        let mut count = 0usize;
        for (tool_name, default_enabled, description) in entries {
            tx.execute(
                "INSERT INTO module_mcp_tool_defaults
                 (mcp_name, tool_name, default_enabled, description, module_id, registered_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                 ON CONFLICT(mcp_name, tool_name) DO UPDATE SET
                     default_enabled = excluded.default_enabled,
                     description     = excluded.description,
                     module_id       = excluded.module_id,
                     registered_at   = excluded.registered_at",
                params![
                    mcp_name,
                    tool_name,
                    *default_enabled as i32,
                    description.as_deref(),
                    module_id,
                    now_ms,
                ],
            )
            .map_err(|e| format!("insert default: {}", e))?;
            count += 1;
        }
        tx.commit().map_err(|e| format!("commit: {}", e))?;
        Ok(count)
    }

    /// List every row for a given `mcp_name`, sorted by `tool_name` for
    /// stable wire output. Used by the hub's `/mcp-tool-grants` route
    /// to assemble the default allowlist before merging with per-project
    /// overrides.
    pub fn list_mcp_tool_defaults(
        &self,
        mcp_name: &str,
    ) -> Result<Vec<McpToolDefault>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT mcp_name, tool_name, default_enabled, description, module_id, registered_at
                 FROM module_mcp_tool_defaults
                 WHERE mcp_name = ?1
                 ORDER BY tool_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![mcp_name], |r| {
                let enabled_i: i32 = r.get(2)?;
                Ok(McpToolDefault {
                    mcp_name: r.get(0)?,
                    tool_name: r.get(1)?,
                    default_enabled: enabled_i != 0,
                    description: r.get(3)?,
                    module_id: r.get(4)?,
                    registered_at: r.get(5)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// Drop every row owned by `module_id` (across all `mcp_name` values
    /// — a module that ships multiple wrappers has multiple rows per
    /// MCP). Used by the uninstall flow so a removed module's defaults
    /// don't continue to shape the hub's allowlist response.
    ///
    /// Returns the number of rows deleted. 0 is normal (module shipped
    /// no MCPs, or its defaults block was empty).
    pub fn clear_mcp_tool_defaults_for_module(
        &self,
        module_id: &str,
    ) -> Result<usize, String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "DELETE FROM module_mcp_tool_defaults WHERE module_id = ?1",
                params![module_id],
            )
            .map_err(|e| format!("clear_mcp_tool_defaults_for_module: {}", e))?;
        Ok(n)
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    fn empty_db() -> Db {
        Db::open_in_memory().expect("in-memory db")
    }

    #[test]
    fn reconcile_inserts_fresh_set() {
        let db = empty_db();
        let entries = vec![
            ("render".to_string(), true, Some("render mermaid".to_string())),
            ("export_png".to_string(), false, None),
        ];
        let n = db
            .reconcile_mcp_tool_defaults("mermaid", "diagrams", &entries, 100)
            .unwrap();
        assert_eq!(n, 2);
        let listed = db.list_mcp_tool_defaults("mermaid").unwrap();
        assert_eq!(listed.len(), 2);
        // Sorted alphabetically.
        assert_eq!(listed[0].tool_name, "export_png");
        assert!(!listed[0].default_enabled);
        assert!(listed[0].description.is_none());
        assert_eq!(listed[1].tool_name, "render");
        assert!(listed[1].default_enabled);
        assert_eq!(
            listed[1].description.as_deref(),
            Some("render mermaid")
        );
    }

    #[test]
    fn reconcile_replaces_prior_set_on_module_update() {
        // Module v0.2.7 ships [tool_a (on), tool_b (off)].
        // Module v0.2.8 removes tool_b, adds tool_c (on).
        // After the second reconcile, tool_b must be GONE; tool_a +
        // tool_c remain.
        let db = empty_db();
        db.reconcile_mcp_tool_defaults(
            "future-mcp",
            "fake-module",
            &[
                ("tool_a".to_string(), true, None),
                ("tool_b".to_string(), false, None),
            ],
            100,
        )
        .unwrap();
        db.reconcile_mcp_tool_defaults(
            "future-mcp",
            "fake-module",
            &[
                ("tool_a".to_string(), true, None),
                ("tool_c".to_string(), true, None),
            ],
            200,
        )
        .unwrap();
        let listed = db.list_mcp_tool_defaults("future-mcp").unwrap();
        assert_eq!(listed.len(), 2);
        let names: Vec<&str> = listed.iter().map(|r| r.tool_name.as_str()).collect();
        assert_eq!(names, vec!["tool_a", "tool_c"]);
    }

    #[test]
    fn reconcile_empty_clears_set() {
        let db = empty_db();
        db.reconcile_mcp_tool_defaults(
            "mermaid",
            "diagrams",
            &[("render".to_string(), true, None)],
            100,
        )
        .unwrap();
        assert_eq!(db.list_mcp_tool_defaults("mermaid").unwrap().len(), 1);
        // Manifest update drops the tool_allowlist block entirely → empty entries.
        db.reconcile_mcp_tool_defaults("mermaid", "diagrams", &[], 200)
            .unwrap();
        assert!(db.list_mcp_tool_defaults("mermaid").unwrap().is_empty());
    }

    #[test]
    fn list_returns_empty_for_unknown_mcp() {
        let db = empty_db();
        let listed = db.list_mcp_tool_defaults("never-shipped").unwrap();
        assert!(listed.is_empty());
    }

    #[test]
    fn clear_for_module_drops_all_its_rows() {
        // One module shipping TWO MCPs, plus a second module shipping a
        // third. Clearing module_a drops both of its rows; module_b's
        // rows survive.
        let db = empty_db();
        db.reconcile_mcp_tool_defaults(
            "mcp-a-1",
            "module_a",
            &[("t1".to_string(), true, None)],
            10,
        )
        .unwrap();
        db.reconcile_mcp_tool_defaults(
            "mcp-a-2",
            "module_a",
            &[("t2".to_string(), true, None)],
            10,
        )
        .unwrap();
        db.reconcile_mcp_tool_defaults(
            "mcp-b-1",
            "module_b",
            &[("t3".to_string(), true, None)],
            10,
        )
        .unwrap();

        let n = db.clear_mcp_tool_defaults_for_module("module_a").unwrap();
        assert_eq!(n, 2);
        assert!(db.list_mcp_tool_defaults("mcp-a-1").unwrap().is_empty());
        assert!(db.list_mcp_tool_defaults("mcp-a-2").unwrap().is_empty());
        // module_b's defaults untouched.
        assert_eq!(db.list_mcp_tool_defaults("mcp-b-1").unwrap().len(), 1);
    }

    #[test]
    fn two_modules_different_mcps_isolated() {
        // Belt-and-suspenders for the module_id scoping in reconcile —
        // if module_a calls reconcile for "mcp-a" it must not clobber
        // module_b's rows for a DIFFERENT mcp ("mcp-b").
        let db = empty_db();
        db.reconcile_mcp_tool_defaults(
            "mcp-a",
            "module_a",
            &[("t_a".to_string(), true, None)],
            10,
        )
        .unwrap();
        db.reconcile_mcp_tool_defaults(
            "mcp-b",
            "module_b",
            &[("t_b".to_string(), false, None)],
            10,
        )
        .unwrap();
        // module_a re-reconciles for its own mcp; module_b's must survive.
        db.reconcile_mcp_tool_defaults(
            "mcp-a",
            "module_a",
            &[("t_a2".to_string(), true, None)],
            20,
        )
        .unwrap();
        let a = db.list_mcp_tool_defaults("mcp-a").unwrap();
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].tool_name, "t_a2");
        let b = db.list_mcp_tool_defaults("mcp-b").unwrap();
        assert_eq!(b.len(), 1);
        assert_eq!(b[0].tool_name, "t_b");
    }
}
