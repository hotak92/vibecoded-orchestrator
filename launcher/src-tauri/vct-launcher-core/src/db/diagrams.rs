//! Per-project diagrams registry + snapshots + cross-project access +
//! per-tool MCP grants + per-project module-active flags.
//!
//! Schema: `migrations/021_diagrams.sql`. All FK-bearing tables CASCADE
//! on `projects.id` so deleting a project wipes its diagrams, snapshots,
//! grants, tool-grants, and module rows. Snapshot rows additionally
//! CASCADE on the parent `project_diagrams.id`.
//!
//! Phase 1.1 scope: this file is pure DB. The Tauri-command wrappers in
//! `commands/diagrams_cmd.rs` add audit-log + filesystem side effects.
//! The (out-of-scope-here) wrapper MCP and `diagram_indexer.py` are
//! consumers of this storage.
//!
//! Design notes:
//!   * Snapshot bytes are stored opaque — the writer chooses raw vs
//!     gzipped. `restore_diagram_snapshot` returns `(file_path, bytes)`
//!     to its caller; the filesystem write is the Tauri command's job
//!     (so it owns the atomic-rename + audit-log discipline).
//!   * `is_module_active` defaults to `false` for unknown modules. This
//!     is the conservative choice — a missing row should not surface a
//!     module-specific CLAUDE.md section, since the row will be present
//!     for any project that genuinely has the module installed.
//!   * Validations: `diagram_type` is checked in Rust before INSERT
//!     (defense-in-depth against the SQL CHECK constraint) so callers
//!     get a clear Rust-level error rather than a stringified SQLite
//!     "constraint failed" message. Same for `access_level`.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

// ─── Row types ───────────────────────────────────────────────────────────

/// One row in `project_diagrams`. Mirrors the extended Phase 1.5 schema.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiagramRow {
    pub id: i64,
    pub project_id: String,
    pub diagram_name: String,
    pub diagram_type: String,
    pub file_path: String,
    pub category_path: String,
    pub enabled: bool,
    pub inferred_title: Option<String>,
    pub diagram_kind: Option<String>,
    pub content_text: Option<String>,
    pub node_count: Option<i64>,
    pub edge_count: Option<i64>,
    pub chat_id: Option<String>,
    pub linked_session_summary: Option<String>,
    pub config_json: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
}

/// One row in `diagram_snapshots`. `content` is opaque bytes (the writer
/// chooses raw vs gzipped).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotRow {
    pub id: i64,
    pub diagram_id: i64,
    pub content_hash: String,
    /// Opaque payload. Serde-serialised as a byte array (the launcher
    /// frontend rarely reads this directly; snapshot UIs render the
    /// metadata + ask the backend to restore on click).
    pub content: Vec<u8>,
    pub created_at: i64,
    pub trigger: String,
    pub label: Option<String>,
}

/// One row in `diagram_access`. Mirrors the codegraph_access shape.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccessRow {
    pub grantor_project_id: String,
    pub grantee_project_id: String,
    pub access_level: String,
    pub granted_at: i64,
}

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

// ─── Validation constants ────────────────────────────────────────────────

const VALID_DIAGRAM_TYPES: &[&str] = &["mermaid", "excalidraw"];
const VALID_ACCESS_LEVELS: &[&str] = &["read", "none"];

fn check_in(label: &str, value: &str, allowed: &[&str]) -> Result<(), String> {
    if allowed.iter().any(|a| *a == value) {
        Ok(())
    } else {
        Err(format!(
            "invalid {}: '{}' (allowed: {:?})",
            label, value, allowed
        ))
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Diagrams
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// Register (or upsert) a diagram. Returns the canonical row.
    ///
    /// Upsert semantics mirror `register_project_agent`: on conflict
    /// (project_id, diagram_name), the file_path / category_path /
    /// type are refreshed; the `enabled` flag is preserved so user
    /// toggles survive re-registration from the populate path.
    pub fn register_diagram(
        &self,
        project_id: &str,
        diagram_name: &str,
        diagram_type: &str,
        file_path: &str,
        category_path: &str,
    ) -> Result<DiagramRow, String> {
        check_in("diagram_type", diagram_type, VALID_DIAGRAM_TYPES)?;
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_diagrams
                 (project_id, diagram_name, diagram_type, file_path,
                  category_path, enabled, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, 1, ?6, ?6)
                 ON CONFLICT(project_id, diagram_name) DO UPDATE SET
                    diagram_type   = excluded.diagram_type,
                    file_path      = excluded.file_path,
                    category_path  = excluded.category_path,
                    updated_at     = excluded.updated_at",
                params![
                    project_id,
                    diagram_name,
                    diagram_type,
                    file_path,
                    category_path,
                    now,
                ],
            )
            .map_err(|e| format!("register_diagram: {}", e))?;
        // Re-fetch so the returned row carries `id` + any prior derived
        // metadata that the upsert intentionally left alone.
        let id: i64 = guard
            .query_row(
                "SELECT id FROM project_diagrams
                 WHERE project_id = ?1 AND diagram_name = ?2",
                params![project_id, diagram_name],
                |r| r.get(0),
            )
            .map_err(|e| format!("register_diagram fetch id: {}", e))?;
        drop(guard);
        self.get_diagram_by_id(id)?.ok_or_else(|| {
            format!(
                "register_diagram: row vanished after insert (project={}, name={})",
                project_id, diagram_name
            )
        })
    }

    /// Fetch a single diagram by primary key.
    pub fn get_diagram_by_id(&self, id: i64) -> Result<Option<DiagramRow>, String> {
        let guard = self.lock();
        let row = guard
            .query_row(
                "SELECT id, project_id, diagram_name, diagram_type, file_path,
                        category_path, enabled, inferred_title, diagram_kind,
                        content_text, node_count, edge_count, chat_id,
                        linked_session_summary, config_json,
                        created_at, updated_at
                 FROM project_diagrams WHERE id = ?1",
                params![id],
                row_to_diagram,
            )
            .optional()
            .map_err(|e| format!("get_diagram_by_id: {}", e))?;
        Ok(row)
    }

    /// List all diagrams for a project, ordered by name.
    pub fn list_project_diagrams(&self, project_id: &str) -> Result<Vec<DiagramRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, diagram_name, diagram_type, file_path,
                        category_path, enabled, inferred_title, diagram_kind,
                        content_text, node_count, edge_count, chat_id,
                        linked_session_summary, config_json,
                        created_at, updated_at
                 FROM project_diagrams WHERE project_id = ?1
                 ORDER BY diagram_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], row_to_diagram)
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// Toggle a diagram's `enabled` flag. Pure DB op — the FS move (if any)
    /// is the caller's responsibility (Tauri command level). Mirrors the
    /// project_mcp_servers shape rather than project_agents because
    /// diagrams don't have a `.disabled/` sibling convention yet (the
    /// plan defers that to the Tauri command, which also runs the file
    /// move and audit).
    pub fn set_diagram_enabled(
        &self,
        project_id: &str,
        diagram_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_diagrams SET enabled = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND diagram_name = ?4",
                params![
                    enabled as i32,
                    Utc::now().timestamp_millis(),
                    project_id,
                    diagram_name,
                ],
            )
            .map_err(|e| format!("set_diagram_enabled: {}", e))?;
        if n == 0 {
            return Err(format!(
                "diagram '{}' not registered for project {}",
                diagram_name, project_id
            ));
        }
        Ok(())
    }

    /// Drop a diagram (cascades to its snapshots).
    pub fn unregister_diagram(
        &self,
        project_id: &str,
        diagram_name: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_diagrams
                 WHERE project_id = ?1 AND diagram_name = ?2",
                params![project_id, diagram_name],
            )
            .map_err(|e| format!("unregister_diagram: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Snapshots
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// Create a snapshot. The `(diagram_id, content_hash)` UNIQUE
    /// constraint means identical content fed twice will fail with
    /// a SQLite "UNIQUE constraint failed" error — callers that want
    /// dedup-then-no-op semantics should check for the hash first or
    /// match on the error. The Tauri command layer wraps this with
    /// idempotent UPSERT-or-fetch semantics.
    pub fn create_diagram_snapshot(
        &self,
        diagram_id: i64,
        content_hash: &str,
        content: &[u8],
        trigger: &str,
        label: Option<&str>,
    ) -> Result<SnapshotRow, String> {
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO diagram_snapshots
                 (diagram_id, content_hash, content, created_at, trigger, label)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![diagram_id, content_hash, content, now, trigger, label],
            )
            .map_err(|e| format!("create_diagram_snapshot: {}", e))?;
        let id = guard.last_insert_rowid();
        Ok(SnapshotRow {
            id,
            diagram_id,
            content_hash: content_hash.to_string(),
            content: content.to_vec(),
            created_at: now,
            trigger: trigger.to_string(),
            label: label.map(str::to_string),
        })
    }

    /// List snapshots for a diagram, newest first.
    pub fn list_diagram_snapshots(
        &self,
        diagram_id: i64,
    ) -> Result<Vec<SnapshotRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, diagram_id, content_hash, content, created_at, trigger, label
                 FROM diagram_snapshots WHERE diagram_id = ?1
                 ORDER BY created_at DESC, id DESC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![diagram_id], row_to_snapshot)
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// Look up a snapshot by id. Used by `restore_diagram_snapshot` to
    /// fetch the bytes without re-listing the whole timeline.
    pub fn get_diagram_snapshot(
        &self,
        snapshot_id: i64,
    ) -> Result<Option<SnapshotRow>, String> {
        let guard = self.lock();
        let row = guard
            .query_row(
                "SELECT id, diagram_id, content_hash, content, created_at, trigger, label
                 FROM diagram_snapshots WHERE id = ?1",
                params![snapshot_id],
                row_to_snapshot,
            )
            .optional()
            .map_err(|e| format!("get_diagram_snapshot: {}", e))?;
        Ok(row)
    }

    /// Resolve the (file_path, content) pair the caller needs to write
    /// back to disk in order to restore a snapshot.
    ///
    /// Pure DB query: no filesystem side effect. The caller (the Tauri
    /// command) is responsible for the atomic-rename write so the
    /// audit-log + restore-event ordering stays in one place.
    pub fn restore_diagram_snapshot(
        &self,
        snapshot_id: i64,
    ) -> Result<(String, Vec<u8>), String> {
        let snapshot = self
            .get_diagram_snapshot(snapshot_id)?
            .ok_or_else(|| format!("snapshot {} not found", snapshot_id))?;
        // Look up the parent diagram for its file_path.
        let diagram = self
            .get_diagram_by_id(snapshot.diagram_id)?
            .ok_or_else(|| {
                format!(
                    "parent diagram {} for snapshot {} not found",
                    snapshot.diagram_id, snapshot_id
                )
            })?;
        Ok((diagram.file_path, snapshot.content))
    }

    /// Drop a snapshot row by id.
    pub fn delete_diagram_snapshot(&self, snapshot_id: i64) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM diagram_snapshots WHERE id = ?1",
                params![snapshot_id],
            )
            .map_err(|e| format!("delete_diagram_snapshot: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Cross-project access (mirrors codegraph_access)
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// Grant or revoke `grantee`'s read access to `grantor`'s diagrams.
    /// Idempotent UPSERT on (grantor, grantee).
    pub fn set_diagram_access(
        &self,
        grantor_project_id: &str,
        grantee_project_id: &str,
        access_level: &str,
    ) -> Result<(), String> {
        check_in("access_level", access_level, VALID_ACCESS_LEVELS)?;
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO diagram_access
                 (grantor_project_id, grantee_project_id, access_level, granted_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(grantor_project_id, grantee_project_id)
                 DO UPDATE SET access_level = excluded.access_level,
                               granted_at   = excluded.granted_at",
                params![
                    grantor_project_id,
                    grantee_project_id,
                    access_level,
                    Utc::now().timestamp_millis(),
                ],
            )
            .map_err(|e| format!("set_diagram_access: {}", e))?;
        Ok(())
    }

    /// List every access row where the given project is the grantor
    /// (i.e. "who can read MY diagrams?"). Newest first.
    pub fn list_diagram_access(
        &self,
        grantor_project_id: &str,
    ) -> Result<Vec<AccessRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT grantor_project_id, grantee_project_id, access_level, granted_at
                 FROM diagram_access WHERE grantor_project_id = ?1
                 ORDER BY granted_at DESC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![grantor_project_id], |r| {
                Ok(AccessRow {
                    grantor_project_id: r.get(0)?,
                    grantee_project_id: r.get(1)?,
                    access_level: r.get(2)?,
                    granted_at: r.get(3)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Per-tool MCP grants
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// Enable or disable a specific tool of a specific MCP server for
    /// a project. UPSERT on (project_id, mcp_name, tool_name) — calling
    /// twice with the same args is idempotent and overwrites only the
    /// `enabled` column.
    ///
    /// An absent row means "fall through to the default-allowlist
    /// baked into `bundled_tool_defaults.toml`" — the wrapper MCP
    /// implements that fallback, not this layer.
    pub fn set_mcp_tool_enabled(
        &self,
        project_id: &str,
        mcp_name: &str,
        tool_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_mcp_tool_grants
                 (project_id, mcp_name, tool_name, enabled)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, mcp_name, tool_name)
                 DO UPDATE SET enabled = excluded.enabled",
                params![project_id, mcp_name, tool_name, enabled as i32],
            )
            .map_err(|e| format!("set_mcp_tool_enabled: {}", e))?;
        Ok(())
    }

    /// List every per-tool grant for a project's MCP server.
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

// ─── Row-mapping helpers ─────────────────────────────────────────────────

fn row_to_diagram(r: &rusqlite::Row) -> rusqlite::Result<DiagramRow> {
    let enabled_i: i32 = r.get(6)?;
    Ok(DiagramRow {
        id: r.get(0)?,
        project_id: r.get(1)?,
        diagram_name: r.get(2)?,
        diagram_type: r.get(3)?,
        file_path: r.get(4)?,
        category_path: r.get(5)?,
        enabled: enabled_i != 0,
        inferred_title: r.get(7)?,
        diagram_kind: r.get(8)?,
        content_text: r.get(9)?,
        node_count: r.get(10)?,
        edge_count: r.get(11)?,
        chat_id: r.get(12)?,
        linked_session_summary: r.get(13)?,
        config_json: r.get(14)?,
        created_at: r.get(15)?,
        updated_at: r.get(16)?,
    })
}

fn row_to_snapshot(r: &rusqlite::Row) -> rusqlite::Result<SnapshotRow> {
    Ok(SnapshotRow {
        id: r.get(0)?,
        diagram_id: r.get(1)?,
        content_hash: r.get(2)?,
        content: r.get(3)?,
        created_at: r.get(4)?,
        trigger: r.get(5)?,
        label: r.get(6)?,
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

    // ─── Diagrams CRUD ──────────────────────────────────────────────────

    #[test]
    fn register_and_list_diagram_round_trip() {
        let db = make_db_with_project("p1", "Acme");
        let row = db
            .register_diagram(
                "p1",
                "login-form",
                "mermaid",
                ".claude/diagrams/gui/auth/login-form.mmd",
                "gui/auth",
            )
            .unwrap();
        assert_eq!(row.diagram_name, "login-form");
        assert_eq!(row.diagram_type, "mermaid");
        assert_eq!(row.category_path, "gui/auth");
        assert!(row.enabled);
        assert!(row.id > 0);

        let listed = db.list_project_diagrams("p1").unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].diagram_name, "login-form");
    }

    #[test]
    fn register_rejects_invalid_diagram_type() {
        let db = make_db_with_project("p1", "Acme");
        let err = db
            .register_diagram("p1", "x", "drawio", ".claude/diagrams/g/x.drawio", "g")
            .unwrap_err();
        assert!(err.contains("invalid diagram_type"), "got: {}", err);
    }

    #[test]
    fn register_is_upsert_preserves_enabled() {
        let db = make_db_with_project("p1", "Acme");
        db.register_diagram("p1", "x", "mermaid", "a.mmd", "g").unwrap();
        db.set_diagram_enabled("p1", "x", false).unwrap();

        // Re-register; enabled must stay false.
        db.register_diagram("p1", "x", "mermaid", "a.mmd", "g").unwrap();
        let listed = db.list_project_diagrams("p1").unwrap();
        assert_eq!(listed.len(), 1);
        assert!(!listed[0].enabled, "user toggle must survive re-register");
    }

    #[test]
    fn set_diagram_enabled_errors_on_unknown_row() {
        let db = make_db_with_project("p1", "Acme");
        let err = db
            .set_diagram_enabled("p1", "ghost", false)
            .unwrap_err();
        assert!(err.contains("not registered"), "got: {}", err);
    }

    #[test]
    fn unregister_removes_diagram_and_cascades_snapshots() {
        let db = make_db_with_project("p1", "Acme");
        let row = db
            .register_diagram("p1", "x", "mermaid", "a.mmd", "g")
            .unwrap();
        db.create_diagram_snapshot(row.id, "hash1", b"flowchart TD; A-->B", "manual", None)
            .unwrap();
        assert_eq!(db.list_diagram_snapshots(row.id).unwrap().len(), 1);

        db.unregister_diagram("p1", "x").unwrap();
        assert!(db.list_project_diagrams("p1").unwrap().is_empty());
        // Snapshot rows cascade-removed.
        assert!(db.list_diagram_snapshots(row.id).unwrap().is_empty());
    }

    // ─── Snapshots ──────────────────────────────────────────────────────

    #[test]
    fn create_and_list_snapshot_round_trip() {
        let db = make_db_with_project("p1", "Acme");
        let d = db
            .register_diagram("p1", "x", "mermaid", "a.mmd", "g")
            .unwrap();
        let content = b"flowchart TD; A-->B".to_vec();
        let snap = db
            .create_diagram_snapshot(d.id, "abc123", &content, "manual", Some("first save"))
            .unwrap();
        assert_eq!(snap.content, content);
        assert_eq!(snap.trigger, "manual");
        assert_eq!(snap.label.as_deref(), Some("first save"));

        let listed = db.list_diagram_snapshots(d.id).unwrap();
        assert_eq!(listed.len(), 1);
        // Round-trip: bytes are byte-identical.
        assert_eq!(listed[0].content, content);
    }

    #[test]
    fn snapshot_dedup_rejects_identical_content_hash() {
        let db = make_db_with_project("p1", "Acme");
        let d = db
            .register_diagram("p1", "x", "mermaid", "a.mmd", "g")
            .unwrap();
        db.create_diagram_snapshot(d.id, "abc", b"data", "manual", None)
            .unwrap();
        // Same (diagram_id, content_hash) → UNIQUE constraint fires.
        let err = db
            .create_diagram_snapshot(d.id, "abc", b"other-data-but-same-hash", "manual", None)
            .unwrap_err();
        assert!(
            err.to_lowercase().contains("unique"),
            "expected UNIQUE constraint error, got: {}",
            err
        );
    }

    #[test]
    fn restore_returns_file_path_and_bytes() {
        let db = make_db_with_project("p1", "Acme");
        let d = db
            .register_diagram(
                "p1",
                "x",
                "mermaid",
                ".claude/diagrams/g/x.mmd",
                "g",
            )
            .unwrap();
        let content = b"opaque bytes (may be gzipped later)".to_vec();
        let snap = db
            .create_diagram_snapshot(d.id, "h1", &content, "manual", None)
            .unwrap();

        let (path, bytes) = db.restore_diagram_snapshot(snap.id).unwrap();
        assert_eq!(path, ".claude/diagrams/g/x.mmd");
        assert_eq!(bytes, content);
    }

    #[test]
    fn delete_snapshot_removes_only_target_row() {
        let db = make_db_with_project("p1", "Acme");
        let d = db
            .register_diagram("p1", "x", "mermaid", "a.mmd", "g")
            .unwrap();
        let s1 = db
            .create_diagram_snapshot(d.id, "h1", b"v1", "manual", None)
            .unwrap();
        let _s2 = db
            .create_diagram_snapshot(d.id, "h2", b"v2", "manual", None)
            .unwrap();
        db.delete_diagram_snapshot(s1.id).unwrap();
        let listed = db.list_diagram_snapshots(d.id).unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].content_hash, "h2");
    }

    // ─── Access grants ──────────────────────────────────────────────────

    #[test]
    fn diagram_access_grant_and_list() {
        let db = make_db_with_project("pA", "ProjA");
        // Seed a second project.
        let slug = db.generate_unique_slug("ProjB").unwrap();
        db.insert_project(
            "pB",
            "ProjB",
            if cfg!(windows) {
                r"C:\tmp\pB"
            } else {
                "/tmp/pB"
            },
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        db.set_diagram_access("pA", "pB", "read").unwrap();
        let grants = db.list_diagram_access("pA").unwrap();
        assert_eq!(grants.len(), 1);
        assert_eq!(grants[0].grantee_project_id, "pB");
        assert_eq!(grants[0].access_level, "read");

        // UPSERT: change level on the same edge.
        db.set_diagram_access("pA", "pB", "none").unwrap();
        let grants = db.list_diagram_access("pA").unwrap();
        assert_eq!(grants.len(), 1);
        assert_eq!(grants[0].access_level, "none");
    }

    #[test]
    fn diagram_access_rejects_invalid_level() {
        let db = make_db_with_project("pA", "ProjA");
        let slug = db.generate_unique_slug("ProjB").unwrap();
        db.insert_project(
            "pB",
            "ProjB",
            if cfg!(windows) {
                r"C:\tmp\pB-iv"
            } else {
                "/tmp/pB-iv"
            },
            ProjectHost::Base,
            &slug,
        )
        .unwrap();
        let err = db.set_diagram_access("pA", "pB", "write").unwrap_err();
        assert!(err.contains("invalid access_level"), "got: {}", err);
    }

    // ─── Per-tool MCP grants ────────────────────────────────────────────

    #[test]
    fn set_and_list_mcp_tool_grants() {
        let db = make_db_with_project("p1", "Acme");
        db.set_mcp_tool_enabled("p1", "mermaid", "render", true).unwrap();
        db.set_mcp_tool_enabled("p1", "mermaid", "export_png", false).unwrap();
        // Different MCP, same project.
        db.set_mcp_tool_enabled("p1", "weaviate-kg", "store_knowledge_node", false)
            .unwrap();

        let mermaid = db.list_project_mcp_tools("p1", "mermaid").unwrap();
        assert_eq!(mermaid.len(), 2);
        let by_name: std::collections::HashMap<_, _> = mermaid
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
        db.set_mcp_tool_enabled("p1", "mermaid", "render", false).unwrap();
        let mermaid = db.list_project_mcp_tools("p1", "mermaid").unwrap();
        assert_eq!(mermaid.len(), 2, "still two rows, just one flipped");
        let by_name: std::collections::HashMap<_, _> = mermaid
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
        assert!(!db.is_module_active("p1", "diagrams").unwrap());

        // Seed module as active.
        db.set_project_module_enabled("p1", "diagrams", true).unwrap();
        assert!(db.is_module_active("p1", "diagrams").unwrap());

        // Disable it.
        db.set_project_module_enabled("p1", "diagrams", false).unwrap();
        assert!(!db.is_module_active("p1", "diagrams").unwrap());

        // Re-enable it (UPSERT path).
        db.set_project_module_enabled("p1", "diagrams", true).unwrap();
        assert!(db.is_module_active("p1", "diagrams").unwrap());
    }

    #[test]
    fn list_project_modules_alphabetical() {
        let db = make_db_with_project("p1", "Acme");
        db.set_project_module_enabled("p1", "rl", true).unwrap();
        db.set_project_module_enabled("p1", "diagrams", true).unwrap();
        db.set_project_module_enabled("p1", "mao", false).unwrap();

        let rows = db.list_project_modules("p1").unwrap();
        let names: Vec<&str> = rows.iter().map(|m| m.module_name.as_str()).collect();
        assert_eq!(names, vec!["diagrams", "mao", "rl"]);
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

        db.set_project_module_enabled("pA", "diagrams", true).unwrap();
        // pB has no row → inactive.
        assert!(db.is_module_active("pA", "diagrams").unwrap());
        assert!(!db.is_module_active("pB", "diagrams").unwrap());
    }

    // ─── Cascades ───────────────────────────────────────────────────────

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

        let d = db
            .register_diagram("pA", "x", "mermaid", "a.mmd", "g")
            .unwrap();
        db.create_diagram_snapshot(d.id, "h1", b"v1", "manual", None)
            .unwrap();
        db.set_diagram_access("pA", "pB", "read").unwrap();
        db.set_mcp_tool_enabled("pA", "mermaid", "render", true).unwrap();
        db.set_project_module_enabled("pA", "diagrams", true).unwrap();

        // Drop the parent project.
        {
            let guard = db.lock();
            guard
                .execute("DELETE FROM projects WHERE id = ?1", params!["pA"])
                .unwrap();
        }

        assert!(db.list_project_diagrams("pA").unwrap().is_empty());
        assert!(db.list_diagram_snapshots(d.id).unwrap().is_empty());
        assert!(db.list_diagram_access("pA").unwrap().is_empty());
        assert!(db.list_project_mcp_tools("pA", "mermaid").unwrap().is_empty());
        assert!(db.list_project_modules("pA").unwrap().is_empty());
        // pB still exists; its inverse-grant lookup is empty (only pA was grantor).
        let inverse: i64 = {
            let guard = db.lock();
            guard
                .query_row(
                    "SELECT COUNT(*) FROM diagram_access WHERE grantee_project_id = ?1",
                    params!["pB"],
                    |r| r.get(0),
                )
                .unwrap()
        };
        assert_eq!(inverse, 0, "grantor-side cascade should have removed the row");
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

        let d = db
            .register_diagram("pA", "x", "mermaid", "a.mmd", "g")
            .unwrap();
        db.create_diagram_snapshot(d.id, "h1", b"v1", "manual", None)
            .unwrap();
        db.set_diagram_access("pA", "pB", "read").unwrap();
        db.set_mcp_tool_enabled("pA", "mermaid", "render", true).unwrap();
        db.set_project_module_enabled("pA", "diagrams", true).unwrap();

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
