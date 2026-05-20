//! Per-project MCP server registry.
//!
//! Mirrors `<folder>/.claude/settings.json::mcpServers` and
//! `<folder>/.mcp.json` (Anthropic project-scoped MCP config) into the
//! `project_mcp_servers` table so the launcher's "Custom MCP" tab can
//! render user-added entries without re-parsing JSON at every render.
//!
//! Schema: `migrations/010_project_mcp_servers.sql`. Cascade-delete on
//! `projects.id` mirrors the rest of the per-project tables.
//!
//! KNOWN_ISSUES.md (v0.2.x) entry resolved:
//!     "Custom MCP tab is not populated by initial project registration —
//!      `project_state_populate` mirrors `.claude/settings.json::mcpServers`
//!      into the launcher's per-project DB on `create_project_v2`, but
//!      doesn't flag user-added entries (anything beyond bundled
//!      `weaviate-kg` / `ollama` / `search` / `code-embedding` /
//!      `playwright`) as `is_user_added=true`. Tab reads with that
//!      filter so user-added servers show up blank."
//!
//! The bundled set is defined as the canonical source-of-truth in
//! `BUNDLED_MCP_NAMES` below. `is_user_added` is computed by the
//! populate step; this module only persists the flag.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

use super::Db;

/// MCP server names shipped by the orchestrator. Anything else gets
/// `is_user_added = true` and surfaces in the Custom MCP tab.
///
/// Source-of-truth: this list mirrors the orchestrator's MCP registration
/// code path. Adding a new bundled MCP requires updating BOTH this list
/// AND the install-side registration. Keep it sorted.
///
/// References:
///  - `install.py:6584` — uninstall scrubs entries with these names from
///    `~/.claude.json::mcpServers`. Same set, same semantics.
///  - `vct-coordination` is included even though install.py's uninstall
///    list also includes it; it's an orchestrator-shipped MCP.
///  - `playwright` is the default-enabled browser-automation MCP
///    (`KNOWN_ISSUES.md` "First-install grew by ~150 MB for Playwright
///    MCP" entry).
pub const BUNDLED_MCP_NAMES: &[&str] = &[
    "code-embedding",
    "ollama",
    "playwright",
    "search",
    "vct-coordination",
    "weaviate-kg",
];

/// True iff `name` is one of the bundled orchestrator MCPs.
pub fn is_bundled_mcp(name: &str) -> bool {
    BUNDLED_MCP_NAMES.iter().any(|b| *b == name)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectMcpServer {
    pub project_id: String,
    pub mcp_name: String,
    pub is_user_added: bool,
    pub source: String,
    pub source_module: Option<String>,
    pub source_file: Option<String>,
    pub enabled: bool,
    pub command: Option<String>,
    pub config: JsonValue,
    pub installed_at: i64,
    pub updated_at: i64,
}

const VALID_SOURCE: &[&str] = &["bundled", "user", "paid-module", "project"];

fn json_from_str(s: &str) -> JsonValue {
    serde_json::from_str(s).unwrap_or(JsonValue::Object(serde_json::Map::new()))
}

fn json_to_str(v: &JsonValue) -> String {
    serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string())
}

impl Db {
    /// Idempotent UPSERT of a single MCP server entry. Preserves the
    /// `enabled` column on conflict (mirrors register_project_agent /
    /// register_project_hook contract — user toggles survive re-populate).
    ///
    /// `is_user_added` is the discriminator the Custom MCP tab filters
    /// on. Caller computes it via `is_bundled_mcp(name)`.
    #[allow(clippy::too_many_arguments)]
    pub fn register_project_mcp_server(
        &self,
        project_id: &str,
        mcp_name: &str,
        is_user_added: bool,
        source: &str,
        source_module: Option<&str>,
        source_file: Option<&str>,
        command: Option<&str>,
        config: &JsonValue,
    ) -> Result<ProjectMcpServer, String> {
        if !VALID_SOURCE.iter().any(|s| *s == source) {
            return Err(format!(
                "invalid mcp.source: '{}' (allowed: {:?})",
                source, VALID_SOURCE
            ));
        }
        let now = Utc::now().timestamp_millis();
        let cfg = json_to_str(config);
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_mcp_servers
                 (project_id, mcp_name, is_user_added, source, source_module,
                  source_file, enabled, command, config_json,
                  installed_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, ?7, ?8, ?9, ?9)
                 ON CONFLICT(project_id, mcp_name) DO UPDATE SET
                    is_user_added = excluded.is_user_added,
                    source        = excluded.source,
                    source_module = excluded.source_module,
                    source_file   = excluded.source_file,
                    command       = excluded.command,
                    config_json   = excluded.config_json,
                    updated_at    = excluded.updated_at",
                params![
                    project_id,
                    mcp_name,
                    is_user_added as i32,
                    source,
                    source_module,
                    source_file,
                    command,
                    cfg,
                    now,
                ],
            )
            .map_err(|e| format!("register_project_mcp_server: {}", e))?;
        Ok(ProjectMcpServer {
            project_id: project_id.to_string(),
            mcp_name: mcp_name.to_string(),
            is_user_added,
            source: source.to_string(),
            source_module: source_module.map(str::to_string),
            source_file: source_file.map(str::to_string),
            enabled: true,
            command: command.map(str::to_string),
            config: config.clone(),
            installed_at: now,
            updated_at: now,
        })
    }

    /// All MCP servers for a project. Custom MCP tab can post-filter on
    /// `is_user_added` client-side (single round-trip is fine here:
    /// per-project MCP rows are O(10s), not O(thousands)).
    pub fn list_project_mcp_servers(
        &self,
        project_id: &str,
    ) -> Result<Vec<ProjectMcpServer>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, mcp_name, is_user_added, source, source_module,
                        source_file, enabled, command, config_json,
                        installed_at, updated_at
                 FROM project_mcp_servers WHERE project_id = ?1
                 ORDER BY mcp_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let cfg_s: String = r.get(8)?;
                let enabled_i: i32 = r.get(6)?;
                let user_i: i32 = r.get(2)?;
                Ok(ProjectMcpServer {
                    project_id: r.get(0)?,
                    mcp_name: r.get(1)?,
                    is_user_added: user_i != 0,
                    source: r.get(3)?,
                    source_module: r.get(4)?,
                    source_file: r.get(5)?,
                    enabled: enabled_i != 0,
                    command: r.get(7)?,
                    config: json_from_str(&cfg_s),
                    installed_at: r.get(9)?,
                    updated_at: r.get(10)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// Custom MCP tab feed: only entries flagged user-added.
    pub fn list_user_added_mcp_servers(
        &self,
        project_id: &str,
    ) -> Result<Vec<ProjectMcpServer>, String> {
        Ok(self
            .list_project_mcp_servers(project_id)?
            .into_iter()
            .filter(|m| m.is_user_added)
            .collect())
    }

    pub fn set_project_mcp_server_enabled(
        &self,
        project_id: &str,
        mcp_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_mcp_servers SET enabled = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND mcp_name = ?4",
                params![
                    enabled as i32,
                    Utc::now().timestamp_millis(),
                    project_id,
                    mcp_name
                ],
            )
            .map_err(|e| format!("set_project_mcp_server_enabled: {}", e))?;
        if n == 0 {
            return Err(format!(
                "mcp server '{}' not registered for project {}",
                mcp_name, project_id
            ));
        }
        Ok(())
    }

    pub fn unregister_project_mcp_server(
        &self,
        project_id: &str,
        mcp_name: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_mcp_servers
                 WHERE project_id = ?1 AND mcp_name = ?2",
                params![project_id, mcp_name],
            )
            .map_err(|e| format!("unregister_project_mcp_server: {}", e))?;
        Ok(())
    }

    /// Quick existence check used by the startup backfill: a project
    /// with zero rows is one that was registered before migration 010
    /// shipped and needs a populate-from-disk pass.
    pub fn count_project_mcp_servers(&self, project_id: &str) -> Result<i64, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT COUNT(*) FROM project_mcp_servers WHERE project_id = ?1",
                params![project_id],
                |r| r.get::<_, i64>(0),
            )
            .optional()
            .map_err(|e| format!("count_project_mcp_servers: {}", e))?
            .ok_or_else(|| "count_project_mcp_servers: query returned no row".to_string())
    }
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
        let folder = if cfg!(windows) { r"C:\tmp\x" } else { "/tmp/x" };
        db.insert_project(project_id, name, folder, ProjectHost::Base, &slug)
            .unwrap();
        db
    }

    #[test]
    fn bundled_set_is_sorted_and_unique() {
        // Catches a future PR that adds a duplicate or out-of-order entry.
        let mut sorted = BUNDLED_MCP_NAMES.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(
            sorted.as_slice(),
            BUNDLED_MCP_NAMES,
            "BUNDLED_MCP_NAMES must be sorted + unique"
        );
    }

    #[test]
    fn is_bundled_mcp_recognises_known_names() {
        for name in BUNDLED_MCP_NAMES {
            assert!(is_bundled_mcp(name), "{} should be bundled", name);
        }
        assert!(!is_bundled_mcp("my-custom-mcp"));
        assert!(!is_bundled_mcp("transcrypt-live"));
        // Empty string and case sensitivity sanity.
        assert!(!is_bundled_mcp(""));
        assert!(!is_bundled_mcp("Weaviate-KG")); // case-sensitive
    }

    #[test]
    fn register_and_list_round_trip() {
        let db = make_db_with_project("p1", "Acme");
        let cfg = serde_json::json!({
            "command": "/usr/bin/python3",
            "args": ["-m", "weaviate_mcp.server"],
            "env": {"OLLAMA_URL": "http://localhost:11435"},
        });
        let row = db
            .register_project_mcp_server(
                "p1",
                "weaviate-kg",
                false,
                "bundled",
                None,
                Some(".claude/settings.json"),
                Some("/usr/bin/python3"),
                &cfg,
            )
            .unwrap();
        assert_eq!(row.mcp_name, "weaviate-kg");
        assert!(!row.is_user_added);
        assert!(row.enabled);
        assert_eq!(row.source, "bundled");
        assert_eq!(row.command.as_deref(), Some("/usr/bin/python3"));

        let listed = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].mcp_name, "weaviate-kg");
        assert_eq!(listed[0].config, cfg);
    }

    #[test]
    fn list_user_added_filters_correctly() {
        let db = make_db_with_project("p1", "Acme");
        let cfg = serde_json::json!({"command": "x"});
        db.register_project_mcp_server(
            "p1",
            "weaviate-kg",
            false,
            "bundled",
            None,
            None,
            None,
            &cfg,
        )
        .unwrap();
        db.register_project_mcp_server(
            "p1",
            "my-custom",
            true,
            "user",
            None,
            None,
            None,
            &cfg,
        )
        .unwrap();
        db.register_project_mcp_server(
            "p1",
            "another-custom",
            true,
            "user",
            None,
            None,
            None,
            &cfg,
        )
        .unwrap();

        let user_only = db.list_user_added_mcp_servers("p1").unwrap();
        assert_eq!(user_only.len(), 2);
        let names: Vec<&str> = user_only.iter().map(|m| m.mcp_name.as_str()).collect();
        assert!(names.contains(&"my-custom"));
        assert!(names.contains(&"another-custom"));
        assert!(!names.contains(&"weaviate-kg"));
    }

    #[test]
    fn upsert_preserves_enabled_flag_on_re_register() {
        let db = make_db_with_project("p1", "Acme");
        let cfg = serde_json::json!({"command": "x"});
        db.register_project_mcp_server(
            "p1", "my-mcp", true, "user", None, None, None, &cfg,
        )
        .unwrap();
        // User disables it via the GUI.
        db.set_project_mcp_server_enabled("p1", "my-mcp", false)
            .unwrap();
        // Re-register (mimics re-populate).
        db.register_project_mcp_server(
            "p1", "my-mcp", true, "user", None, None, None, &cfg,
        )
        .unwrap();
        let rows = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert!(
            !rows[0].enabled,
            "user's disabled flag must survive re-register (mirrors agents/skills/hooks)"
        );
    }

    #[test]
    fn upsert_updates_config_and_user_flag_on_conflict() {
        let db = make_db_with_project("p1", "Acme");
        // First registration: bundled (is_user_added=false).
        db.register_project_mcp_server(
            "p1",
            "weaviate-kg",
            false,
            "bundled",
            None,
            Some(".claude/settings.json"),
            Some("/old/path"),
            &serde_json::json!({"command": "/old/path"}),
        )
        .unwrap();
        // Re-register with new command + same name (e.g. user moved venv).
        // is_user_added stays bundled.
        db.register_project_mcp_server(
            "p1",
            "weaviate-kg",
            false,
            "bundled",
            None,
            Some(".claude/settings.json"),
            Some("/new/path"),
            &serde_json::json!({"command": "/new/path"}),
        )
        .unwrap();
        let rows = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].command.as_deref(), Some("/new/path"));
        assert!(!rows[0].is_user_added);
    }

    #[test]
    fn unregister_removes_row() {
        let db = make_db_with_project("p1", "Acme");
        db.register_project_mcp_server(
            "p1",
            "x",
            true,
            "user",
            None,
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();
        db.unregister_project_mcp_server("p1", "x").unwrap();
        assert!(db.list_project_mcp_servers("p1").unwrap().is_empty());
    }

    #[test]
    fn count_returns_zero_for_fresh_project() {
        let db = make_db_with_project("p1", "Acme");
        assert_eq!(db.count_project_mcp_servers("p1").unwrap(), 0);
    }

    #[test]
    fn count_reflects_inserts() {
        let db = make_db_with_project("p1", "Acme");
        db.register_project_mcp_server(
            "p1", "a", false, "bundled", None, None, None, &serde_json::json!({}),
        )
        .unwrap();
        db.register_project_mcp_server(
            "p1", "b", true, "user", None, None, None, &serde_json::json!({}),
        )
        .unwrap();
        assert_eq!(db.count_project_mcp_servers("p1").unwrap(), 2);
    }

    #[test]
    fn cascade_delete_on_project_removal() {
        let db = make_db_with_project("p1", "Acme");
        db.register_project_mcp_server(
            "p1", "a", true, "user", None, None, None, &serde_json::json!({}),
        )
        .unwrap();
        // Drop project; FK CASCADE wipes the mcp_servers row.
        let guard = db.lock();
        guard
            .execute("DELETE FROM projects WHERE id = ?1", params!["p1"])
            .unwrap();
        drop(guard);
        assert_eq!(db.count_project_mcp_servers("p1").unwrap(), 0);
    }

    #[test]
    fn set_enabled_errors_on_unknown_row() {
        let db = make_db_with_project("p1", "Acme");
        let err = db
            .set_project_mcp_server_enabled("p1", "ghost", false)
            .unwrap_err();
        assert!(
            err.contains("not registered"),
            "expected 'not registered' in error, got: {}",
            err
        );
    }

    #[test]
    fn invalid_source_rejected() {
        let db = make_db_with_project("p1", "Acme");
        let err = db
            .register_project_mcp_server(
                "p1",
                "x",
                true,
                "garbage",
                None,
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap_err();
        assert!(err.contains("invalid mcp.source"), "got: {}", err);
    }
}
