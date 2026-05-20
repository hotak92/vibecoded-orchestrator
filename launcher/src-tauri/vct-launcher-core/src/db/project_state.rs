//! Per-project orchestrator state: agents, skills, hooks, permissions,
//! secret references, and KG/codegraph bindings.
//!
//! Schema: `migrations/002_project_state.sql`. All tables CASCADE on
//! `projects.id` so deleting a project wipes its state.
//!
//! Secret VALUES never live here — only references. The actual values
//! are resolved at runtime from `~/.vct-secrets/shared/` (Phase 1 layout)
//! with fallback to legacy flat `~/.vct-secrets/` or the OS keychain
//! (see `crate::secrets`).

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

use super::Db;

// ═══════════════════════════════════════════════════════════════════════
// Row types
// ═══════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectAgent {
    pub project_id: String,
    pub agent_name: String,
    pub source: String,
    pub source_module: Option<String>,
    pub model: Option<String>,
    pub enabled: bool,
    pub file_path: Option<String>,
    pub config: JsonValue,
    pub installed_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectSkill {
    pub project_id: String,
    pub skill_name: String,
    pub source: String,
    pub source_module: Option<String>,
    pub model: Option<String>,
    pub enabled: bool,
    pub file_path: Option<String>,
    pub config: JsonValue,
    pub installed_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectHook {
    pub id: i64,
    pub project_id: String,
    pub event: String,
    pub matcher: String,
    pub command: String,
    pub source: String,
    pub source_module: Option<String>,
    pub enabled: bool,
    pub timeout_ms: Option<i64>,
    pub config: JsonValue,
    pub installed_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectPermission {
    pub id: i64,
    pub project_id: String,
    pub subject: String,
    pub kind: String,
    pub value: String,
    pub config: JsonValue,
    pub granted_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectSecretRef {
    pub project_id: String,
    pub secret_key: String,
    pub resolution: String,
    pub file_path: Option<String>,
    pub env_name: Option<String>,
    pub source_module: Option<String>,
    pub required_for: Vec<String>,
    pub description: String,
    pub is_set: bool,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectKgBinding {
    pub project_id: String,
    pub role: String,
    pub collection_name: String,
    pub embedding_model: Option<String>,
    pub embedding_dim: Option<i64>,
    pub kg_dir_path: Option<String>,
    pub weaviate_url: Option<String>,
    pub config: JsonValue,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectCodegraphBinding {
    pub project_id: String,
    pub collection_prefix: String,
    pub embedding_model: Option<String>,
    pub embedding_dim: Option<i64>,
    pub last_analyzed_commit: Option<String>,
    pub last_analyzed_at: Option<i64>,
    pub enabled: bool,
    pub config: JsonValue,
    pub updated_at: i64,
}

/// Aggregated per-project view used by the launcher GUI to render the
/// per-project tab in one round trip.
#[derive(Debug, Clone, Serialize)]
pub struct ProjectStateSnapshot {
    pub project_id: String,
    pub agents: Vec<ProjectAgent>,
    pub skills: Vec<ProjectSkill>,
    pub hooks: Vec<ProjectHook>,
    pub permissions: Vec<ProjectPermission>,
    pub secret_refs: Vec<ProjectSecretRef>,
    pub kg_bindings: Vec<ProjectKgBinding>,
    pub codegraph_binding: Option<ProjectCodegraphBinding>,
    /// Migration 010: per-project MCP server registry. Populated from
    /// `<folder>/.claude/settings.json::mcpServers` + `<folder>/.mcp.json`
    /// by `populate_project_state_from_filesystem`. The Custom MCP tab
    /// reads with `is_user_added=true` filtering applied client-side.
    pub mcp_servers: Vec<crate::db::project_mcp_servers::ProjectMcpServer>,
}

// ═══════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════

fn json_from_str(s: &str) -> JsonValue {
    serde_json::from_str(s).unwrap_or(JsonValue::Object(serde_json::Map::new()))
}

fn json_to_str(v: &JsonValue) -> String {
    serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string())
}

const VALID_SOURCE: &[&str] = &["bundled", "user", "paid-module", "project"];
const VALID_RESOLUTION: &[&str] = &[
    "keychain-per-project",
    "keychain-shared",
    "keychain-global",
    "file",
    "env",
];
const VALID_PERM_KIND: &[&str] = &[
    "write_scope",
    "allowed_tool",
    "denied_tool",
    "mcp_server",
    "permission_mode",
];
const VALID_KG_ROLE: &[&str] = &["primary", "shared", "archive"];

fn check_in(label: &str, value: &str, allowed: &[&str]) -> Result<(), String> {
    if allowed.iter().any(|a| *a == value) {
        Ok(())
    } else {
        Err(format!("invalid {}: '{}' (allowed: {:?})", label, value, allowed))
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Agents
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    pub fn register_project_agent(
        &self,
        project_id: &str,
        agent_name: &str,
        source: &str,
        source_module: Option<&str>,
        model: Option<&str>,
        file_path: Option<&str>,
        config: &JsonValue,
    ) -> Result<ProjectAgent, String> {
        check_in("agent.source", source, VALID_SOURCE)?;
        let now = Utc::now().timestamp_millis();
        let cfg = json_to_str(config);
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_agents
                 (project_id, agent_name, source, source_module, model, enabled,
                  file_path, config_json, installed_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, 1, ?6, ?7, ?8, ?8)
                 ON CONFLICT(project_id, agent_name) DO UPDATE SET
                    source = excluded.source,
                    source_module = excluded.source_module,
                    model = excluded.model,
                    file_path = excluded.file_path,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at",
                params![
                    project_id, agent_name, source, source_module, model, file_path, cfg, now,
                ],
            )
            .map_err(|e| format!("register_project_agent: {}", e))?;
        Ok(ProjectAgent {
            project_id: project_id.to_string(),
            agent_name: agent_name.to_string(),
            source: source.to_string(),
            source_module: source_module.map(str::to_string),
            model: model.map(str::to_string),
            enabled: true,
            file_path: file_path.map(str::to_string),
            config: config.clone(),
            installed_at: now,
            updated_at: now,
        })
    }

    pub fn list_project_agents(&self, project_id: &str) -> Result<Vec<ProjectAgent>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, agent_name, source, source_module, model, enabled,
                        file_path, config_json, installed_at, updated_at
                 FROM project_agents WHERE project_id = ?1
                 ORDER BY agent_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let cfg_s: String = r.get(7)?;
                let enabled_i: i32 = r.get(5)?;
                Ok(ProjectAgent {
                    project_id: r.get(0)?,
                    agent_name: r.get(1)?,
                    source: r.get(2)?,
                    source_module: r.get(3)?,
                    model: r.get(4)?,
                    enabled: enabled_i != 0,
                    file_path: r.get(6)?,
                    config: json_from_str(&cfg_s),
                    installed_at: r.get(8)?,
                    updated_at: r.get(9)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn set_project_agent_enabled(
        &self,
        project_id: &str,
        agent_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_agents SET enabled = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND agent_name = ?4",
                params![
                    enabled as i32,
                    Utc::now().timestamp_millis(),
                    project_id,
                    agent_name
                ],
            )
            .map_err(|e| format!("set_project_agent_enabled: {}", e))?;
        if n == 0 {
            return Err(format!("agent {} not registered for project {}", agent_name, project_id));
        }
        Ok(())
    }

    pub fn unregister_project_agent(
        &self,
        project_id: &str,
        agent_name: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_agents WHERE project_id = ?1 AND agent_name = ?2",
                params![project_id, agent_name],
            )
            .map_err(|e| format!("unregister_project_agent: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Skills
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    pub fn register_project_skill(
        &self,
        project_id: &str,
        skill_name: &str,
        source: &str,
        source_module: Option<&str>,
        model: Option<&str>,
        file_path: Option<&str>,
        config: &JsonValue,
    ) -> Result<ProjectSkill, String> {
        check_in("skill.source", source, VALID_SOURCE)?;
        let now = Utc::now().timestamp_millis();
        let cfg = json_to_str(config);
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_skills
                 (project_id, skill_name, source, source_module, model, enabled,
                  file_path, config_json, installed_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, 1, ?6, ?7, ?8, ?8)
                 ON CONFLICT(project_id, skill_name) DO UPDATE SET
                    source = excluded.source,
                    source_module = excluded.source_module,
                    model = excluded.model,
                    file_path = excluded.file_path,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at",
                params![
                    project_id, skill_name, source, source_module, model, file_path, cfg, now,
                ],
            )
            .map_err(|e| format!("register_project_skill: {}", e))?;
        Ok(ProjectSkill {
            project_id: project_id.to_string(),
            skill_name: skill_name.to_string(),
            source: source.to_string(),
            source_module: source_module.map(str::to_string),
            model: model.map(str::to_string),
            enabled: true,
            file_path: file_path.map(str::to_string),
            config: config.clone(),
            installed_at: now,
            updated_at: now,
        })
    }

    pub fn list_project_skills(&self, project_id: &str) -> Result<Vec<ProjectSkill>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, skill_name, source, source_module, model, enabled,
                        file_path, config_json, installed_at, updated_at
                 FROM project_skills WHERE project_id = ?1
                 ORDER BY skill_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let cfg_s: String = r.get(7)?;
                let enabled_i: i32 = r.get(5)?;
                Ok(ProjectSkill {
                    project_id: r.get(0)?,
                    skill_name: r.get(1)?,
                    source: r.get(2)?,
                    source_module: r.get(3)?,
                    model: r.get(4)?,
                    enabled: enabled_i != 0,
                    file_path: r.get(6)?,
                    config: json_from_str(&cfg_s),
                    installed_at: r.get(8)?,
                    updated_at: r.get(9)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn set_project_skill_enabled(
        &self,
        project_id: &str,
        skill_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_skills SET enabled = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND skill_name = ?4",
                params![
                    enabled as i32,
                    Utc::now().timestamp_millis(),
                    project_id,
                    skill_name
                ],
            )
            .map_err(|e| format!("set_project_skill_enabled: {}", e))?;
        if n == 0 {
            return Err(format!("skill {} not registered for project {}", skill_name, project_id));
        }
        Ok(())
    }

    pub fn unregister_project_skill(
        &self,
        project_id: &str,
        skill_name: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_skills WHERE project_id = ?1 AND skill_name = ?2",
                params![project_id, skill_name],
            )
            .map_err(|e| format!("unregister_project_skill: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Hooks
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    pub fn register_project_hook(
        &self,
        project_id: &str,
        event: &str,
        matcher: &str,
        command: &str,
        source: &str,
        source_module: Option<&str>,
        timeout_ms: Option<i64>,
        config: &JsonValue,
    ) -> Result<ProjectHook, String> {
        check_in("hook.source", source, VALID_SOURCE)?;
        let now = Utc::now().timestamp_millis();
        let cfg = json_to_str(config);
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_hooks
                 (project_id, event, matcher, command, source, source_module,
                  enabled, timeout_ms, config_json, installed_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, ?7, ?8, ?9, ?9)
                 ON CONFLICT(project_id, event, matcher, command) DO UPDATE SET
                    source = excluded.source,
                    source_module = excluded.source_module,
                    timeout_ms = excluded.timeout_ms,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at",
                params![
                    project_id, event, matcher, command, source, source_module,
                    timeout_ms, cfg, now,
                ],
            )
            .map_err(|e| format!("register_project_hook: {}", e))?;
        let id: i64 = guard
            .query_row(
                "SELECT id FROM project_hooks
                  WHERE project_id = ?1 AND event = ?2 AND matcher = ?3 AND command = ?4",
                params![project_id, event, matcher, command],
                |r| r.get(0),
            )
            .map_err(|e| format!("re-fetch hook id: {}", e))?;
        Ok(ProjectHook {
            id,
            project_id: project_id.to_string(),
            event: event.to_string(),
            matcher: matcher.to_string(),
            command: command.to_string(),
            source: source.to_string(),
            source_module: source_module.map(str::to_string),
            enabled: true,
            timeout_ms,
            config: config.clone(),
            installed_at: now,
            updated_at: now,
        })
    }

    pub fn list_project_hooks(&self, project_id: &str) -> Result<Vec<ProjectHook>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, event, matcher, command, source, source_module,
                        enabled, timeout_ms, config_json, installed_at, updated_at
                 FROM project_hooks WHERE project_id = ?1
                 ORDER BY event ASC, matcher ASC, id ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let cfg_s: String = r.get(9)?;
                let enabled_i: i32 = r.get(7)?;
                Ok(ProjectHook {
                    id: r.get(0)?,
                    project_id: r.get(1)?,
                    event: r.get(2)?,
                    matcher: r.get(3)?,
                    command: r.get(4)?,
                    source: r.get(5)?,
                    source_module: r.get(6)?,
                    enabled: enabled_i != 0,
                    timeout_ms: r.get(8)?,
                    config: json_from_str(&cfg_s),
                    installed_at: r.get(10)?,
                    updated_at: r.get(11)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn set_project_hook_enabled(&self, hook_id: i64, enabled: bool) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_hooks SET enabled = ?1, updated_at = ?2 WHERE id = ?3",
                params![enabled as i32, Utc::now().timestamp_millis(), hook_id],
            )
            .map_err(|e| format!("set_project_hook_enabled: {}", e))?;
        if n == 0 {
            return Err(format!("hook id {} not found", hook_id));
        }
        Ok(())
    }

    pub fn unregister_project_hook(&self, hook_id: i64) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute("DELETE FROM project_hooks WHERE id = ?1", params![hook_id])
            .map_err(|e| format!("unregister_project_hook: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Permissions
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    pub fn add_project_permission(
        &self,
        project_id: &str,
        subject: &str,
        kind: &str,
        value: &str,
        config: &JsonValue,
    ) -> Result<ProjectPermission, String> {
        check_in("permission.kind", kind, VALID_PERM_KIND)?;
        let now = Utc::now().timestamp_millis();
        let cfg = json_to_str(config);
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_permissions
                 (project_id, subject, kind, value, config_json, granted_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                 ON CONFLICT(project_id, subject, kind, value) DO UPDATE SET
                    config_json = excluded.config_json,
                    granted_at = excluded.granted_at",
                params![project_id, subject, kind, value, cfg, now],
            )
            .map_err(|e| format!("add_project_permission: {}", e))?;
        let id: i64 = guard
            .query_row(
                "SELECT id FROM project_permissions
                  WHERE project_id = ?1 AND subject = ?2 AND kind = ?3 AND value = ?4",
                params![project_id, subject, kind, value],
                |r| r.get(0),
            )
            .map_err(|e| format!("re-fetch perm id: {}", e))?;
        Ok(ProjectPermission {
            id,
            project_id: project_id.to_string(),
            subject: subject.to_string(),
            kind: kind.to_string(),
            value: value.to_string(),
            config: config.clone(),
            granted_at: now,
        })
    }

    pub fn list_project_permissions(
        &self,
        project_id: &str,
    ) -> Result<Vec<ProjectPermission>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT id, project_id, subject, kind, value, config_json, granted_at
                 FROM project_permissions WHERE project_id = ?1
                 ORDER BY subject ASC, kind ASC, value ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let cfg_s: String = r.get(5)?;
                Ok(ProjectPermission {
                    id: r.get(0)?,
                    project_id: r.get(1)?,
                    subject: r.get(2)?,
                    kind: r.get(3)?,
                    value: r.get(4)?,
                    config: json_from_str(&cfg_s),
                    granted_at: r.get(6)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn delete_project_permission(&self, perm_id: i64) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_permissions WHERE id = ?1",
                params![perm_id],
            )
            .map_err(|e| format!("delete_project_permission: {}", e))?;
        Ok(())
    }

    /// 0.2.x backlog #5 (2026-05-10): delete a permission row by its
    /// natural key `(project_id, subject, kind, value)`. Used by
    /// `set_project_mcp_permission(enabled=true)` to remove any explicit
    /// per-project row so the row falls back to the default-enabled
    /// state. Idempotent: a missing row is a no-op (0 rows affected).
    pub fn delete_project_permission_by_key(
        &self,
        project_id: &str,
        subject: &str,
        kind: &str,
        value: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_permissions
                  WHERE project_id = ?1 AND subject = ?2 AND kind = ?3 AND value = ?4",
                params![project_id, subject, kind, value],
            )
            .map_err(|e| format!("delete_project_permission_by_key: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Secret references
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    pub fn set_project_secret_ref(
        &self,
        project_id: &str,
        secret_key: &str,
        resolution: &str,
        file_path: Option<&str>,
        env_name: Option<&str>,
        source_module: Option<&str>,
        required_for: &[String],
        description: &str,
        is_set: bool,
    ) -> Result<ProjectSecretRef, String> {
        check_in("secret.resolution", resolution, VALID_RESOLUTION)?;
        let now = Utc::now().timestamp_millis();
        let req_json = serde_json::to_string(required_for).unwrap_or_else(|_| "[]".to_string());
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_secret_refs
                 (project_id, secret_key, resolution, file_path, env_name,
                  source_module, required_for, description, is_set, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
                 ON CONFLICT(project_id, secret_key) DO UPDATE SET
                    resolution = excluded.resolution,
                    file_path = excluded.file_path,
                    env_name = excluded.env_name,
                    source_module = excluded.source_module,
                    required_for = excluded.required_for,
                    description = excluded.description,
                    is_set = excluded.is_set,
                    updated_at = excluded.updated_at",
                params![
                    project_id, secret_key, resolution, file_path, env_name,
                    source_module, req_json, description, is_set as i32, now,
                ],
            )
            .map_err(|e| format!("set_project_secret_ref: {}", e))?;
        Ok(ProjectSecretRef {
            project_id: project_id.to_string(),
            secret_key: secret_key.to_string(),
            resolution: resolution.to_string(),
            file_path: file_path.map(str::to_string),
            env_name: env_name.map(str::to_string),
            source_module: source_module.map(str::to_string),
            required_for: required_for.to_vec(),
            description: description.to_string(),
            is_set,
            updated_at: now,
        })
    }

    pub fn list_project_secret_refs(
        &self,
        project_id: &str,
    ) -> Result<Vec<ProjectSecretRef>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, secret_key, resolution, file_path, env_name,
                        source_module, required_for, description, is_set, updated_at
                 FROM project_secret_refs WHERE project_id = ?1
                 ORDER BY secret_key ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let req_s: String = r.get(6)?;
                let is_set_i: i32 = r.get(8)?;
                Ok(ProjectSecretRef {
                    project_id: r.get(0)?,
                    secret_key: r.get(1)?,
                    resolution: r.get(2)?,
                    file_path: r.get(3)?,
                    env_name: r.get(4)?,
                    source_module: r.get(5)?,
                    required_for: serde_json::from_str(&req_s).unwrap_or_default(),
                    description: r.get(7)?,
                    is_set: is_set_i != 0,
                    updated_at: r.get(9)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn delete_project_secret_ref(
        &self,
        project_id: &str,
        secret_key: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_secret_refs WHERE project_id = ?1 AND secret_key = ?2",
                params![project_id, secret_key],
            )
            .map_err(|e| format!("delete_project_secret_ref: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// KG bindings
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    pub fn set_project_kg_binding(
        &self,
        project_id: &str,
        role: &str,
        collection_name: &str,
        embedding_model: Option<&str>,
        embedding_dim: Option<i64>,
        kg_dir_path: Option<&str>,
        weaviate_url: Option<&str>,
        config: &JsonValue,
    ) -> Result<ProjectKgBinding, String> {
        check_in("kg_binding.role", role, VALID_KG_ROLE)?;
        let now = Utc::now().timestamp_millis();
        let cfg = json_to_str(config);
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_kg_bindings
                 (project_id, role, collection_name, embedding_model, embedding_dim,
                  kg_dir_path, weaviate_url, config_json, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
                 ON CONFLICT(project_id, role) DO UPDATE SET
                    collection_name = excluded.collection_name,
                    embedding_model = excluded.embedding_model,
                    embedding_dim = excluded.embedding_dim,
                    kg_dir_path = excluded.kg_dir_path,
                    weaviate_url = excluded.weaviate_url,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at",
                params![
                    project_id, role, collection_name, embedding_model, embedding_dim,
                    kg_dir_path, weaviate_url, cfg, now,
                ],
            )
            .map_err(|e| format!("set_project_kg_binding: {}", e))?;
        Ok(ProjectKgBinding {
            project_id: project_id.to_string(),
            role: role.to_string(),
            collection_name: collection_name.to_string(),
            embedding_model: embedding_model.map(str::to_string),
            embedding_dim,
            kg_dir_path: kg_dir_path.map(str::to_string),
            weaviate_url: weaviate_url.map(str::to_string),
            config: config.clone(),
            updated_at: now,
        })
    }

    pub fn list_project_kg_bindings(
        &self,
        project_id: &str,
    ) -> Result<Vec<ProjectKgBinding>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, role, collection_name, embedding_model, embedding_dim,
                        kg_dir_path, weaviate_url, config_json, updated_at
                 FROM project_kg_bindings WHERE project_id = ?1
                 ORDER BY role ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let cfg_s: String = r.get(7)?;
                Ok(ProjectKgBinding {
                    project_id: r.get(0)?,
                    role: r.get(1)?,
                    collection_name: r.get(2)?,
                    embedding_model: r.get(3)?,
                    embedding_dim: r.get(4)?,
                    kg_dir_path: r.get(5)?,
                    weaviate_url: r.get(6)?,
                    config: json_from_str(&cfg_s),
                    updated_at: r.get(8)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn delete_project_kg_binding(
        &self,
        project_id: &str,
        role: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_kg_bindings WHERE project_id = ?1 AND role = ?2",
                params![project_id, role],
            )
            .map_err(|e| format!("delete_project_kg_binding: {}", e))?;
        Ok(())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Codegraph binding
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    pub fn set_project_codegraph_binding(
        &self,
        project_id: &str,
        collection_prefix: &str,
        embedding_model: Option<&str>,
        embedding_dim: Option<i64>,
        last_analyzed_commit: Option<&str>,
        last_analyzed_at: Option<i64>,
        enabled: bool,
        config: &JsonValue,
    ) -> Result<ProjectCodegraphBinding, String> {
        let now = Utc::now().timestamp_millis();
        let cfg = json_to_str(config);
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO project_codegraph_bindings
                 (project_id, collection_prefix, embedding_model, embedding_dim,
                  last_analyzed_commit, last_analyzed_at, enabled, config_json, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
                 ON CONFLICT(project_id) DO UPDATE SET
                    collection_prefix = excluded.collection_prefix,
                    embedding_model = excluded.embedding_model,
                    embedding_dim = excluded.embedding_dim,
                    last_analyzed_commit = excluded.last_analyzed_commit,
                    last_analyzed_at = excluded.last_analyzed_at,
                    enabled = excluded.enabled,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at",
                params![
                    project_id, collection_prefix, embedding_model, embedding_dim,
                    last_analyzed_commit, last_analyzed_at, enabled as i32, cfg, now,
                ],
            )
            .map_err(|e| format!("set_project_codegraph_binding: {}", e))?;
        Ok(ProjectCodegraphBinding {
            project_id: project_id.to_string(),
            collection_prefix: collection_prefix.to_string(),
            embedding_model: embedding_model.map(str::to_string),
            embedding_dim,
            last_analyzed_commit: last_analyzed_commit.map(str::to_string),
            last_analyzed_at,
            enabled,
            config: config.clone(),
            updated_at: now,
        })
    }

    pub fn get_project_codegraph_binding(
        &self,
        project_id: &str,
    ) -> Result<Option<ProjectCodegraphBinding>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, collection_prefix, embedding_model, embedding_dim,
                        last_analyzed_commit, last_analyzed_at, enabled, config_json, updated_at
                 FROM project_codegraph_bindings WHERE project_id = ?1",
                params![project_id],
                |r| {
                    let cfg_s: String = r.get(7)?;
                    let enabled_i: i32 = r.get(6)?;
                    Ok(ProjectCodegraphBinding {
                        project_id: r.get(0)?,
                        collection_prefix: r.get(1)?,
                        embedding_model: r.get(2)?,
                        embedding_dim: r.get(3)?,
                        last_analyzed_commit: r.get(4)?,
                        last_analyzed_at: r.get(5)?,
                        enabled: enabled_i != 0,
                        config: json_from_str(&cfg_s),
                        updated_at: r.get(8)?,
                    })
                },
            )
            .optional()
            .map_err(|e| format!("get_project_codegraph_binding: {}", e))
    }

    pub fn delete_project_codegraph_binding(&self, project_id: &str) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_codegraph_bindings WHERE project_id = ?1",
                params![project_id],
            )
            .map_err(|e| format!("delete_project_codegraph_binding: {}", e))?;
        Ok(())
    }

    /// Reverse-lookup: given a Weaviate collection prefix (e.g. "MyProject"),
    /// return the project_id that owns that prefix. Used by the codegraph
    /// dashboard to map Weaviate classes back to projects so it can render
    /// one card per project (not one per class). Returns None when the
    /// prefix isn't claimed by any project — the dashboard then renders
    /// the prefix as the project name and "none" access.
    pub fn find_project_by_codegraph_prefix(
        &self,
        prefix: &str,
    ) -> Result<Option<String>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id FROM project_codegraph_bindings WHERE collection_prefix = ?1",
                params![prefix],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .map_err(|e| format!("find_project_by_codegraph_prefix: {}", e))
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Snapshot
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// Aggregate every per-project state row into one struct. The launcher
    /// GUI calls this when rendering the per-project tab so it doesn't
    /// need 7 round-trips.
    pub fn get_project_state_snapshot(
        &self,
        project_id: &str,
    ) -> Result<ProjectStateSnapshot, String> {
        Ok(ProjectStateSnapshot {
            project_id: project_id.to_string(),
            agents: self.list_project_agents(project_id)?,
            skills: self.list_project_skills(project_id)?,
            hooks: self.list_project_hooks(project_id)?,
            permissions: self.list_project_permissions(project_id)?,
            secret_refs: self.list_project_secret_refs(project_id)?,
            kg_bindings: self.list_project_kg_bindings(project_id)?,
            codegraph_binding: self.get_project_codegraph_binding(project_id)?,
            mcp_servers: self.list_project_mcp_servers(project_id)?,
        })
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use std::sync::Mutex;

    fn make_db() -> Db {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        // Apply the same migrations the real launcher does.
        crate::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    fn seed_project(db: &Db, id: &str, name: &str) {
        // Placeholder folder_path — never resolved against disk by these
        // tests. Pick a platform-appropriate prefix so the value isn't
        // ambiguous on Windows.
        let folder = if cfg!(windows) {
            format!(r"C:\tmp\{}", id)
        } else {
            format!("/tmp/{}", id)
        };
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?4)",
                params![id, name, folder, 1_700_000_000_000_i64],
            )
            .unwrap();
    }

    /// Platform-aware placeholder path for fixture string fields (agent
    /// file paths, secret-ref file paths). Tests only round-trip these
    /// strings through SQLite; nothing on disk is opened.
    fn fixture_str_path(rel: &str) -> String {
        if cfg!(windows) {
            format!(r"C:\Users\u\{}", rel.replace('/', "\\"))
        } else {
            format!("/home/u/{}", rel)
        }
    }

    #[test]
    fn agents_insert_and_list() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        db.register_project_agent(
            "p1",
            "coder",
            "bundled",
            None,
            Some("sonnet"),
            Some(&fixture_str_path(".claude/agents/coder.md")),
            &serde_json::json!({"description": "general coder"}),
        )
        .unwrap();
        let agents = db.list_project_agents("p1").unwrap();
        assert_eq!(agents.len(), 1);
        assert_eq!(agents[0].agent_name, "coder");
        assert!(agents[0].enabled);
        assert_eq!(agents[0].model.as_deref(), Some("sonnet"));
    }

    #[test]
    fn agents_enable_disable_roundtrip() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        db.register_project_agent("p1", "tester", "user", None, None, None, &JsonValue::Null)
            .unwrap();
        db.set_project_agent_enabled("p1", "tester", false).unwrap();
        let agents = db.list_project_agents("p1").unwrap();
        assert!(!agents[0].enabled);
        db.set_project_agent_enabled("p1", "tester", true).unwrap();
        let agents = db.list_project_agents("p1").unwrap();
        assert!(agents[0].enabled);
    }

    #[test]
    fn agent_unknown_source_rejected() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        let err = db
            .register_project_agent("p1", "x", "garbage", None, None, None, &JsonValue::Null)
            .unwrap_err();
        assert!(err.contains("invalid agent.source"));
    }

    #[test]
    fn cascade_delete_wipes_state() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        db.register_project_agent("p1", "a", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        db.register_project_skill("p1", "s", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        db.register_project_hook(
            "p1",
            "PreToolUse",
            "Edit(*.py)",
            "ruff check",
            "project",
            None,
            Some(5_000),
            &JsonValue::Null,
        )
        .unwrap();
        db.add_project_permission("p1", "project", "write_scope", "src/**", &JsonValue::Null)
            .unwrap();
        db.set_project_secret_ref(
            "p1",
            "GITHUB_TOKEN",
            "file",
            Some(&fixture_str_path(".vct-secrets/github_pat")),
            None,
            None,
            &["coder".to_string()],
            "GH PAT",
            true,
        )
        .unwrap();
        db.set_project_kg_binding(
            "p1",
            "primary",
            "ProjectOneKG",
            Some("qwen3-embedding:0.6b"),
            Some(1024),
            None,
            None,
            &JsonValue::Null,
        )
        .unwrap();
        db.set_project_codegraph_binding(
            "p1",
            "ProjectOne",
            Some("CodeSage-Large-v2"),
            Some(2048),
            Some("abc123"),
            Some(1_700_000_000_000),
            true,
            &JsonValue::Null,
        )
        .unwrap();

        // Sanity: snapshot has everything.
        let snap = db.get_project_state_snapshot("p1").unwrap();
        assert_eq!(snap.agents.len(), 1);
        assert_eq!(snap.skills.len(), 1);
        assert_eq!(snap.hooks.len(), 1);
        assert_eq!(snap.permissions.len(), 1);
        assert_eq!(snap.secret_refs.len(), 1);
        assert_eq!(snap.kg_bindings.len(), 1);
        assert!(snap.codegraph_binding.is_some());

        // Now delete the project — every dependent row should vanish.
        db.delete_project("p1").unwrap();
        let snap = db.get_project_state_snapshot("p1").unwrap();
        assert!(snap.agents.is_empty());
        assert!(snap.skills.is_empty());
        assert!(snap.hooks.is_empty());
        assert!(snap.permissions.is_empty());
        assert!(snap.secret_refs.is_empty());
        assert!(snap.kg_bindings.is_empty());
        assert!(snap.codegraph_binding.is_none());
    }

    #[test]
    fn hooks_unique_constraint_and_upsert() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        let h1 = db
            .register_project_hook(
                "p1",
                "PostToolUse",
                "Edit(*.py)",
                "ruff check --fix",
                "project",
                None,
                None,
                &JsonValue::Null,
            )
            .unwrap();
        // Same key — should upsert, not duplicate.
        let h2 = db
            .register_project_hook(
                "p1",
                "PostToolUse",
                "Edit(*.py)",
                "ruff check --fix",
                "user",
                None,
                Some(10_000),
                &JsonValue::Null,
            )
            .unwrap();
        assert_eq!(h1.id, h2.id);
        let hooks = db.list_project_hooks("p1").unwrap();
        assert_eq!(hooks.len(), 1);
        assert_eq!(hooks[0].timeout_ms, Some(10_000));
        assert_eq!(hooks[0].source, "user");
    }

    #[test]
    fn secret_ref_never_stores_value() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        db.set_project_secret_ref(
            "p1",
            "OPENAI_API_KEY",
            "keychain-per-project",
            None,
            None,
            Some("openai-helper"),
            &["coder".to_string(), "tester".to_string()],
            "OpenAI",
            false,
        )
        .unwrap();
        let refs = db.list_project_secret_refs("p1").unwrap();
        assert_eq!(refs.len(), 1);
        assert_eq!(refs[0].secret_key, "OPENAI_API_KEY");
        assert_eq!(refs[0].required_for, vec!["coder".to_string(), "tester".to_string()]);
        assert!(!refs[0].is_set);
        // Schema check: the table has no `value` column.
        let guard = db.lock();
        let mut stmt = guard
            .prepare("SELECT name FROM pragma_table_info('project_secret_refs')")
            .unwrap();
        let cols: Vec<String> = stmt
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        assert!(!cols.iter().any(|c| c == "value" || c == "secret_value"));
    }

    /// PR-3 Commit 5 (2026-05-06): the SecretsPanel ↔ SecretsTab bridge.
    /// When the user sets a per-project secret value via the global
    /// SecretsPanel, the launcher also calls `set_project_secret_ref`
    /// so the per-project SecretsTab populates. This test pins the
    /// underlying DB contract: after a registration with the bridge's
    /// canonical args, `list_project_secret_refs` reflects the new ref
    /// AND the upsert is idempotent on a second registration with the
    /// same key.
    #[test]
    fn bridge_registration_appears_in_per_project_list_and_is_idempotent() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // First registration — mirrors what `secrets.setValue` calls
        // through `invoke('set_project_secret_ref', ...)` on a per-
        // project scope. resolution=keychain-per-project; is_set=true
        // (the keychain write that preceded it succeeded).
        db.set_project_secret_ref(
            "p1",
            "ANTHROPIC_API_KEY",
            "keychain-per-project",
            None,
            None,
            Some("user"),
            &[],
            "",
            true,
        )
        .unwrap();

        let refs = db.list_project_secret_refs("p1").unwrap();
        assert_eq!(refs.len(), 1, "first registration must create the ref row");
        assert_eq!(refs[0].secret_key, "ANTHROPIC_API_KEY");
        assert_eq!(refs[0].resolution, "keychain-per-project");
        assert_eq!(refs[0].source_module.as_deref(), Some("user"));
        assert!(refs[0].is_set);

        // Second registration of the SAME key (e.g. user updated the
        // value via the panel) — must upsert, not duplicate. The
        // `set_secret_v2` → `set_project_secret_ref` bridge fires on
        // every value-set, so re-runs MUST be idempotent.
        db.set_project_secret_ref(
            "p1",
            "ANTHROPIC_API_KEY",
            "keychain-per-project",
            None,
            None,
            Some("user"),
            &[],
            "",
            true,
        )
        .unwrap();
        let refs = db.list_project_secret_refs("p1").unwrap();
        assert_eq!(
            refs.len(),
            1,
            "second registration must upsert, not duplicate"
        );

        // Per-project tab shows zero refs to start with for project p2 —
        // proves the bridge correctly scopes by project_id.
        seed_project(&db, "p2", "Project Two");
        let p2_refs = db.list_project_secret_refs("p2").unwrap();
        assert!(
            p2_refs.is_empty(),
            "ref registered for p1 must NOT appear in p2's list"
        );
    }

    #[test]
    fn kg_binding_role_validation() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        let err = db
            .set_project_kg_binding("p1", "weird", "X", None, None, None, None, &JsonValue::Null)
            .unwrap_err();
        assert!(err.contains("invalid kg_binding.role"));
    }

    #[test]
    fn delete_kg_binding_removes_only_target_role() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        db.set_project_kg_binding(
            "p1", "primary", "P1Primary", None, None, None, None, &JsonValue::Null,
        )
        .unwrap();
        db.set_project_kg_binding(
            "p1", "shared", "SharedKG", None, None, None, None, &JsonValue::Null,
        )
        .unwrap();
        // Both bindings present.
        let bindings = db.list_project_kg_bindings("p1").unwrap();
        assert_eq!(bindings.len(), 2);

        // Remove only the shared role; primary stays.
        db.delete_project_kg_binding("p1", "shared").unwrap();
        let bindings = db.list_project_kg_bindings("p1").unwrap();
        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0].role, "primary");

        // Idempotent: deleting again is a no-op (0 affected rows, no error).
        db.delete_project_kg_binding("p1", "shared").unwrap();
        let bindings = db.list_project_kg_bindings("p1").unwrap();
        assert_eq!(bindings.len(), 1);
    }

    #[test]
    fn delete_codegraph_binding_unbinds_cleanly() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        db.set_project_codegraph_binding(
            "p1",
            "P1Code",
            Some("CodeSage-Large-v2"),
            Some(2048),
            None,
            None,
            true,
            &JsonValue::Null,
        )
        .unwrap();
        assert!(db.get_project_codegraph_binding("p1").unwrap().is_some());

        db.delete_project_codegraph_binding("p1").unwrap();
        assert!(db.get_project_codegraph_binding("p1").unwrap().is_none());

        // Idempotent.
        db.delete_project_codegraph_binding("p1").unwrap();
    }

    #[test]
    fn permissions_multiple_per_subject() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");
        db.add_project_permission("p1", "coder", "allowed_tool", "Read", &JsonValue::Null)
            .unwrap();
        db.add_project_permission("p1", "coder", "allowed_tool", "Write", &JsonValue::Null)
            .unwrap();
        db.add_project_permission("p1", "coder", "write_scope", "src/**", &JsonValue::Null)
            .unwrap();
        let perms = db.list_project_permissions("p1").unwrap();
        assert_eq!(perms.len(), 3);
    }
}
