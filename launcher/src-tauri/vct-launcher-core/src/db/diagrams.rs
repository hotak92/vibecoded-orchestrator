//! Per-tool MCP grant reader + per-project module-active flags.
//!
//! v0.2.102 (owner ruling 2026-10-07 — diagrams dropped): this file used
//! to also hold the diagrams registry / snapshots / cross-project access
//! DB functions. Those died with the Diagrams tab and the diagram Tauri
//! commands (their only consumers). What stays:
//!
//!   * `list_project_mcp_tools` — the `project_mcp_tool_grants` READER the
//!     hub's `mcp_tool_grants_api` route serves.
//!   * the `project_modules` functions — the per-project module-active
//!     flag behind `{{#if_module_active <name>}}` rendering and
//!     module-gated bundle delivery (consumed via
//!     `commands/module_enabled.rs`, moved out of `diagrams_cmd.rs` in
//!     the same change).
//!
//! Schema: `migrations/022_diagrams.sql`. The migration and ALL its
//! tables stay (user data is never dropped); the file name keeps the
//! migration's name for archaeological continuity even though the
//! diagram query functions are gone. All FK-bearing tables CASCADE on
//! `projects.id`, so deleting a project wipes its diagrams, snapshots,
//! grants, tool-grants, and module rows — pinned by the cascade tests
//! below, which seed the diagram tables with raw SQL now that the
//! typed writers are deleted.
//!
//! Design notes:
//!   * `is_module_active` defaults to `false` for unknown modules. This
//!     is the conservative choice — a missing row should not surface a
//!     module-specific CLAUDE.md section, since the row will be present
//!     for any project that genuinely has the module installed.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

// ─── Row types ───────────────────────────────────────────────────────────

/// One row in `project_mcp_tool_grants` — per-tool allowlist override.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolGrant {
    pub project_id: String,
    pub mcp_name: String,
    pub tool_name: String,
    pub enabled: bool,
}

/// One row in `project_modules` — per-project module-active flag.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModuleRow {
    pub project_id: String,
    pub module_name: String,
    pub enabled: bool,
    pub registered_at: i64,
}

// ═══════════════════════════════════════════════════════════════════════
// Per-tool MCP grants (reader)
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// List a project's per-tool grant rows for one MCP, alphabetical by
    /// tool name. The corresponding WRITER (`set_mcp_tool_enabled`) was
    /// RETIRED (v0.2.101). Its only production callers were the
    /// per-tool-grant Tauri commands the wrapper-MCP permissions UI used,
    /// and that UI is gone with the wrapper MCPs' default registration;
    /// this READER stays because the hub's `mcp_tool_grants_api` route
    /// serves it. (v0.2.102: the diagram wrappers that route was built
    /// for are deleted and their leftover entries auto-scrubbed; the
    /// route + reader remain as the generic per-tool surface for any
    /// module-shipped MCP with a `tool_allowlist`.) Rows reach the table
    /// through history (written before the retirement), by a module
    /// install, or directly by the user.
    pub fn list_project_mcp_tools(
        &self,
        project_id: &str,
        mcp_name: &str,
    ) -> Result<Vec<ToolGrant>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, mcp_name, tool_name, enabled
                 FROM project_mcp_tool_grants
                 WHERE project_id = ?1 AND mcp_name = ?2
                 ORDER BY tool_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id, mcp_name], |r| {
                let enabled_i: i32 = r.get(3)?;
                Ok(ToolGrant {
                    project_id: r.get(0)?,
                    mcp_name: r.get(1)?,
                    tool_name: r.get(2)?,
                    enabled: enabled_i != 0,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Per-project modules
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// Flip a project's module-active flag. UPSERT on
    /// (project_id, module_name). `registered_at` is preserved on
    /// conflict (only set on first insertion).
    pub fn set_project_module_enabled(
        &self,
        project_id: &str,
        module_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_modules
                 (project_id, module_name, enabled, registered_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, module_name)
                 DO UPDATE SET enabled = excluded.enabled",
                params![
                    project_id,
                    module_name,
                    enabled as i32,
                    Utc::now().timestamp_millis(),
                ],
            )
            .map_err(|e| format!("set_project_module_enabled: {}", e))?;
        Ok(())
    }

    /// Delete a project's EXPLICIT module-flag row (v0.2.101, G1 follow-up).
    ///
    /// The inverse of [`set_project_module_enabled`]: with no row, the
    /// module falls back to its default resolution — for `model_gateway`
    /// the machine gateway signal
    /// (`vco_lib.module_gated_delivery.gateway_agents_gate`), the same
    /// "follows the machine" state the CLAUDE.md render already gives a
    /// row-less project. Until this existed, a project the user had ever
    /// toggled could never return to that state.
    ///
    /// `Ok(false)` when no row existed — an absent row is not an error
    /// (same rule as `delete_chat_model_context`), and a double-click on
    /// "Follow this machine" must not look like a failure.
    pub fn clear_project_module(
        &self,
        project_id: &str,
        module_name: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let affected = guard
            .execute(
                "DELETE FROM project_modules
                 WHERE project_id = ?1 AND module_name = ?2",
                params![project_id, module_name],
            )
            .map_err(|e| format!("clear_project_module: {}", e))?;
        Ok(affected > 0)
    }

    /// List every module-flag row for a project. Used by the launcher's
    /// per-project "Modules" tab and by the CLAUDE.md conditional-block
    /// renderer when it needs all flags in one round trip.
    pub fn list_project_modules(
        &self,
        project_id: &str,
    ) -> Result<Vec<ModuleRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, module_name, enabled, registered_at
                 FROM project_modules WHERE project_id = ?1
                 ORDER BY module_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let enabled_i: i32 = r.get(2)?;
                Ok(ModuleRow {
                    project_id: r.get(0)?,
                    module_name: r.get(1)?,
                    enabled: enabled_i != 0,
                    registered_at: r.get(3)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// Quick boolean lookup for `{{#if_module_active <name>}}` template
    /// expansion. Returns `false` when the (project_id, module_name)
    /// row is absent or its `enabled` flag is `0`. Returns `true` only
    /// when the row exists AND `enabled = 1`.
    pub fn is_module_active(
        &self,
        project_id: &str,
        module_name: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let enabled: Option<i32> = guard
            .query_row(
                "SELECT enabled FROM project_modules
                 WHERE project_id = ?1 AND module_name = ?2",
                params![project_id, module_name],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("is_module_active: {}", e))?;
        Ok(enabled.map(|e| e != 0).unwrap_or(false))
    }
}

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

    // ─── Per-tool MCP grants (reader) ──────────────────────────────────
    // The v0.2.101 retirement removed the WRITE side that used to ship
    // with the per-tool-grant GUI (owner ruling). Tests seed
    // `project_mcp_tool_grants` rows with the table's own UPSERT SQL —
    // the same statement the retired writer issued — and pin the READER
    // the hub route still serves.

    fn seed_tool_grant(db: &Db, project: &str, mcp: &str, tool: &str, enabled: bool) {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO project_mcp_tool_grants
                 (project_id, mcp_name, tool_name, enabled)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, mcp_name, tool_name)
                 DO UPDATE SET enabled = excluded.enabled",
                params![project, mcp, tool, enabled as i32],
            )
            .unwrap();
    }

    #[test]
    fn list_project_mcp_tools_reads_seeded_grant_rows() {
        let db = make_db_with_project("p1", "Acme");
        seed_tool_grant(&db, "p1", "some-mcp", "render", true);
        seed_tool_grant(&db, "p1", "some-mcp", "export_png", false);
        // Different MCP, same project.
        seed_tool_grant(&db, "p1", "weaviate-kg", "store_knowledge_node", false);

        let some = db.list_project_mcp_tools("p1", "some-mcp").unwrap();
        assert_eq!(some.len(), 2);
        let by_name: std::collections::HashMap<_, _> = some
            .iter()
            .map(|t| (t.tool_name.as_str(), t.enabled))
            .collect();
        assert!(by_name["render"]);
        assert!(!by_name["export_png"]);

        let wv = db.list_project_mcp_tools("p1", "weaviate-kg").unwrap();
        assert_eq!(wv.len(), 1);
        assert_eq!(wv[0].tool_name, "store_knowledge_node");
        assert!(!wv[0].enabled);

        // UPSERT: flip render off.
        seed_tool_grant(&db, "p1", "some-mcp", "render", false);
        let some = db.list_project_mcp_tools("p1", "some-mcp").unwrap();
        assert_eq!(some.len(), 2, "still two rows, just one flipped");
        let by_name: std::collections::HashMap<_, _> = some
            .iter()
            .map(|t| (t.tool_name.as_str(), t.enabled))
            .collect();
        assert!(!by_name["render"]);
    }

    // ─── Per-project modules ────────────────────────────────────────────

    #[test]
    fn module_active_default_off_then_toggled_on() {
        let db = make_db_with_project("p1", "Acme");
        // Pre-seed: no row → inactive.
        assert!(!db.is_module_active("p1", "model_gateway").unwrap());

        // Seed module as active.
        db.set_project_module_enabled("p1", "model_gateway", true).unwrap();
        assert!(db.is_module_active("p1", "model_gateway").unwrap());

        // Disable it.
        db.set_project_module_enabled("p1", "model_gateway", false).unwrap();
        assert!(!db.is_module_active("p1", "model_gateway").unwrap());

        // Re-enable it (UPSERT path).
        db.set_project_module_enabled("p1", "model_gateway", true).unwrap();
        assert!(db.is_module_active("p1", "model_gateway").unwrap());
    }

    // ─── clear_project_module (v0.2.101, G1 follow-up) ──────────────────

    #[test]
    fn clear_project_module_deletes_only_the_named_row_and_reports_absence() {
        let db = make_db_with_project("p1", "Acme");
        db.set_project_module_enabled("p1", "model_gateway", false).unwrap();
        db.set_project_module_enabled("p1", "rl", true).unwrap();

        // ACT: the explicit choice goes away — the module's default
        // resolution takes over again ("follows the machine").
        assert!(db.clear_project_module("p1", "model_gateway").unwrap());
        let modules = db.list_project_modules("p1").unwrap();
        assert_eq!(
            modules.iter().map(|m| m.module_name.as_str()).collect::<Vec<_>>(),
            vec!["rl"],
            "only the named (project, module) row is deleted"
        );

        // LEAVE-ALONE: clearing a row that is not there is Ok(false), not
        // an error — and it still touches nothing else.
        assert!(!db.clear_project_module("p1", "model_gateway").unwrap());
        assert!(!db.clear_project_module("p-other", "rl").unwrap());
        assert_eq!(db.list_project_modules("p1").unwrap().len(), 1);
    }

    #[test]
    fn list_project_modules_alphabetical() {
        let db = make_db_with_project("p1", "Acme");
        db.set_project_module_enabled("p1", "rl", true).unwrap();
        db.set_project_module_enabled("p1", "mao", true).unwrap();
        db.set_project_module_enabled("p1", "model_gateway", false).unwrap();

        let rows = db.list_project_modules("p1").unwrap();
        let names: Vec<&str> = rows.iter().map(|m| m.module_name.as_str()).collect();
        assert_eq!(names, vec!["mao", "model_gateway", "rl"]);
    }

    #[test]
    fn module_active_isolated_per_project() {
        let db = make_db_with_project("pA", "A");
        let slug = db.generate_unique_slug("B").unwrap();
        db.insert_project(
            "pB",
            "B",
            if cfg!(windows) {
                r"C:\tmp\pB-iso"
            } else {
                "/tmp/pB-iso"
            },
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        db.set_project_module_enabled("pA", "model_gateway", true).unwrap();
        // pB has no row → inactive.
        assert!(db.is_module_active("pA", "model_gateway").unwrap());
        assert!(!db.is_module_active("pB", "model_gateway").unwrap());
    }

    // ─── Cascades (migration 022 schema — the tables stay, so their
    // ON DELETE CASCADE behaviour stays pinned. v0.2.102: seeds use raw
    // SQL now that the typed diagram writers are deleted.) ──────────────

    /// Seed one row into each migration-022 diagram table with raw SQL.
    fn seed_diagram_tables(db: &Db, project_id: &str, grantee: &str) {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO project_diagrams
                 (project_id, diagram_name, diagram_type, file_path,
                  category_path, enabled, created_at, updated_at)
                 VALUES (?1, 'x', 'mermaid', 'a.mmd', 'g', 1, 0, 0)",
                params![project_id],
            )
            .unwrap();
        let diagram_id: i64 = guard
            .query_row(
                "SELECT id FROM project_diagrams
                 WHERE project_id = ?1 AND diagram_name = 'x'",
                params![project_id],
                |r| r.get(0),
            )
            .unwrap();
        guard
            .execute(
                "INSERT INTO diagram_snapshots
                 (diagram_id, content_hash, content, created_at, trigger, label)
                 VALUES (?1, 'h1', X'7631', 0, 'manual', NULL)",
                params![diagram_id],
            )
            .unwrap();
        guard
            .execute(
                "INSERT INTO diagram_access
                 (grantor_project_id, grantee_project_id, access_level, granted_at)
                 VALUES (?1, ?2, 'read', 0)",
                params![project_id, grantee],
            )
            .unwrap();
    }

    #[test]
    fn cascade_deletes_diagrams_snapshots_grants_modules_when_project_dropped() {
        let db = make_db_with_project("pA", "A");
        let slug = db.generate_unique_slug("B").unwrap();
        db.insert_project(
            "pB",
            "B",
            if cfg!(windows) {
                r"C:\tmp\pB-cas"
            } else {
                "/tmp/pB-cas"
            },
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        seed_diagram_tables(&db, "pA", "pB");
        seed_tool_grant(&db, "pA", "some-mcp", "render", true);
        db.set_project_module_enabled("pA", "model_gateway", true).unwrap();

        // Drop the parent project.
        {
            let guard = db.lock();
            guard
                .execute("DELETE FROM projects WHERE id = ?1", params!["pA"])
                .unwrap();
        }

        {
            let guard = db.lock();
            for table in [
                "project_diagrams",
                "diagram_snapshots",
                "diagram_access",
                "project_mcp_tool_grants",
                "project_modules",
            ] {
                let n: i64 = guard
                    .query_row(
                        &format!("SELECT COUNT(*) FROM {}", table),
                        [],
                        |r| r.get(0),
                    )
                    .unwrap();
                assert_eq!(n, 0, "{} rows must cascade away with the project", table);
            }
            // pB still exists; its inverse-grant lookup is empty (only pA
            // was grantor).
            let inverse: i64 = guard
                .query_row(
                    "SELECT COUNT(*) FROM diagram_access WHERE grantee_project_id = ?1",
                    params!["pB"],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(inverse, 0, "grantor-side cascade should have removed the row");
        }
    }

    /// PRAGMA foreign_key_check verifies no dangling refs after the cascade.
    #[test]
    fn cascade_leaves_no_dangling_fks() {
        let db = make_db_with_project("pA", "A");
        let slug = db.generate_unique_slug("B").unwrap();
        db.insert_project(
            "pB",
            "B",
            if cfg!(windows) {
                r"C:\tmp\pB-fkc"
            } else {
                "/tmp/pB-fkc"
            },
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        seed_diagram_tables(&db, "pA", "pB");
        seed_tool_grant(&db, "pA", "some-mcp", "render", true);
        db.set_project_module_enabled("pA", "model_gateway", true).unwrap();

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
            "PRAGMA foreign_key_check found dangling refs: {:?}",
            orphans
        );
    }
}
