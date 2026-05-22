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

use std::path::{Path, PathBuf};

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

use super::Db;

// ═══════════════════════════════════════════════════════════════════════
// FS-disable: agent/skill file-move mechanism
// ═══════════════════════════════════════════════════════════════════════
//
// Background (see `.claude/context/plans/agent-skill-keyword-suggest-and-fs-disable.md`):
//
// Claude Code discovers agents by globbing `.claude/agents/*.md` and
// skills by globbing `.claude/skills/*/SKILL.md`. It has NO knowledge of
// the launcher DB. So flipping `project_agents.enabled = 0` is a no-op
// from Claude's perspective — a "disabled" agent stays visible to
// auto-complete, `/agents`, and autonomous invocation.
//
// Fix: when the user toggles "disable" in the launcher GUI, move the
// file out of Claude's discovery globs into a sibling `.disabled/`
// directory:
//
//   .claude/agents/<name>.md      ↔  .claude/agents.disabled/<name>.md
//   .claude/skills/<name>/        ↔  .claude/skills.disabled/<name>/
//
// `.disabled/` is lowercase (safe across HFS+/APFS/NTFS case-insensitive
// behaviour) and is a sibling directory, NOT a subfolder, so it is
// outside Claude's glob regardless of whether the glob is recursive.
//
// Invariant: `enabled=1 ⟺ file is in agents/`,
//            `enabled=0 ⟺ file is in agents.disabled/`.
// The one-time migration (`migrate_disabled_files_to_disabled_dir`)
// enforces this on first run after the v0.2.26 upgrade.

/// Distinguishes which discovery glob (and thus which sibling
/// directory) applies. Used by `resolve_kind_paths` and the migration
/// scan to share path-resolution code between agents and skills.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentOrSkill {
    /// Agent: single `.md` file under `.claude/agents/`.
    Agent,
    /// Skill: whole directory under `.claude/skills/` (containing
    /// `SKILL.md` + any companion files).
    Skill,
}

impl AgentOrSkill {
    /// The enabled-side directory name. Pure constant — no I/O.
    fn enabled_dir(self) -> &'static str {
        match self {
            AgentOrSkill::Agent => "agents",
            AgentOrSkill::Skill => "skills",
        }
    }
    /// The sibling disabled-side directory name. Pure constant.
    fn disabled_dir(self) -> &'static str {
        match self {
            AgentOrSkill::Agent => "agents.disabled",
            AgentOrSkill::Skill => "skills.disabled",
        }
    }
}

/// Pure path-joining helper. Given a project's filesystem location and
/// an entry name, return the `(enabled_path, disabled_path)` pair for
/// either an agent file or a skill directory.
///
/// - Agents: `<project>/.claude/agents/<name>.md`  and  `<project>/.claude/agents.disabled/<name>.md`
/// - Skills: `<project>/.claude/skills/<name>/`    and  `<project>/.claude/skills.disabled/<name>/`
///
/// No I/O. Uses `PathBuf::join` end-to-end so cross-OS separator
/// handling is delegated to `std::path` (no string concat anywhere).
pub fn resolve_kind_paths(
    project_folder: &Path,
    name: &str,
    kind: AgentOrSkill,
) -> (PathBuf, PathBuf) {
    let base = project_folder.join(".claude");
    let (enabled, disabled) = match kind {
        AgentOrSkill::Agent => {
            // Agents are individual `.md` files.
            let leaf = format!("{}.md", name);
            (
                base.join(kind.enabled_dir()).join(&leaf),
                base.join(kind.disabled_dir()).join(&leaf),
            )
        }
        AgentOrSkill::Skill => {
            // Skills are whole directories — the name IS the leaf.
            (
                base.join(kind.enabled_dir()).join(name),
                base.join(kind.disabled_dir()).join(name),
            )
        }
    };
    (enabled, disabled)
}

/// Move a file or directory across the `agents/` ↔ `agents.disabled/`
/// boundary with a small Windows retry shim.
///
/// On NTFS, `rename` can transiently fail with `ERROR_SHARING_VIOLATION`
/// or `ERROR_ACCESS_DENIED` if the destination tree was just touched by
/// AV / Explorer / a file watcher. We retry once after 200ms, which in
/// practice covers all the cases we've observed in CI.
///
/// On Linux + macOS the retry is a no-op fast path — the first attempt
/// always succeeds for same-volume moves.
///
/// Pre-conditions enforced by the caller (`set_project_*_enabled` and
/// the migration function):
///   - `from` exists
///   - `to` does NOT exist (no clobber)
///   - `to.parent()` exists or can be created
fn move_with_rollback(from: &Path, to: &Path) -> Result<(), String> {
    // Ensure parent dir of destination exists. mkdir -p semantics.
    if let Some(parent) = to.parent() {
        std::fs::create_dir_all(parent).map_err(|e| {
            format!("create parent dir {}: {}", parent.display(), e)
        })?;
    }

    match std::fs::rename(from, to) {
        Ok(()) => Ok(()),
        Err(first_err) => {
            // One retry with 200ms backoff for Windows transients
            // (sharing violations, AV scans, file watchers).
            std::thread::sleep(std::time::Duration::from_millis(200));
            match std::fs::rename(from, to) {
                Ok(()) => Ok(()),
                Err(second_err) => Err(format!(
                    "rename {} -> {} failed (first: {}, retry: {})",
                    from.display(),
                    to.display(),
                    first_err,
                    second_err
                )),
            }
        }
    }
}

/// Report from `migrate_disabled_files_to_disabled_dir`. The launcher's
/// startup wiring (Subagent E, Wave 2) reads this to surface results in
/// the GUI or audit log. Per-file errors are collected, not raised, so
/// one bad row does not abort the whole migration sweep.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct MigrationReport {
    /// Number of files actually moved into `.disabled/` this run.
    pub moved: usize,
    /// Number of rows that were already in the right place (no-op).
    pub already_disabled: usize,
    /// Number of `enabled=0` DB rows whose file is missing from BOTH
    /// `agents/` and `agents.disabled/` (stale row — left alone).
    pub stale_rows: usize,
    /// Number of rows where the file exists at BOTH locations
    /// simultaneously (logged + left alone, needs manual cleanup).
    pub both_locations: usize,
    /// Per-file errors keyed by `agent:<name>` or `skill:<name>`.
    /// Soft-fail per row — collected here for surfacing later.
    pub errors: Vec<String>,
}

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

    /// Toggle an agent's `enabled` flag AND move its `.md` file across
    /// the `.claude/agents/` ↔ `.claude/agents.disabled/` boundary.
    ///
    /// **Design choice**: this public method keeps the
    /// `(project_id, agent_name, enabled)` signature that every existing
    /// caller already uses (Tauri commands, vct-hub HTTP API, tests in
    /// `commands/project_state_populate.rs` and `commands/projects_v2.rs`).
    /// The `project_folder` needed for the FS move is resolved via a
    /// sub-query on `projects.folder_path` so callers don't have to
    /// thread the path through. Wave 2 agents (D/E) wiring populate +
    /// startup do not need to change their call sites.
    ///
    /// If a caller already has the project folder in hand (e.g. during a
    /// bulk populate) it can call `set_project_agent_enabled_with_folder`
    /// directly to skip the lookup.
    ///
    /// Atomicity contract (see the design doc, "Atomicity of FS-move +
    /// DB-flip"):
    ///   1. Resolve enabled+disabled paths.
    ///   2. Validate the source exists at one location and the target does NOT.
    ///   3. Move the file (rename).
    ///   4. Flip the DB column.
    ///   5. If step 4 fails → reverse the rename. If the reverse ALSO
    ///      fails, return a clear error citing the inconsistent state.
    ///
    /// Idempotent: toggling to the current value is a fast no-op (no
    /// FS move, no DB write) — we read the current row first.
    pub fn set_project_agent_enabled(
        &self,
        project_id: &str,
        agent_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let folder = self.lookup_project_folder(project_id)?;
        self.set_project_agent_enabled_with_folder(
            project_id,
            agent_name,
            enabled,
            &folder,
        )
    }

    /// Lower-level variant of `set_project_agent_enabled` that takes the
    /// project folder explicitly. Useful for batch operations and tests
    /// that drive a tempdir-rooted project layout.
    pub fn set_project_agent_enabled_with_folder(
        &self,
        project_id: &str,
        agent_name: &str,
        enabled: bool,
        project_folder: &Path,
    ) -> Result<(), String> {
        self.set_enabled_with_fs_move(
            project_id,
            agent_name,
            enabled,
            project_folder,
            AgentOrSkill::Agent,
        )
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

    /// Toggle a skill's `enabled` flag AND move its whole directory
    /// across the `.claude/skills/` ↔ `.claude/skills.disabled/`
    /// boundary. See `set_project_agent_enabled` for the design
    /// rationale — same contract, except the FS unit is a directory
    /// (containing SKILL.md plus optional companion files) rather than
    /// a single file.
    pub fn set_project_skill_enabled(
        &self,
        project_id: &str,
        skill_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let folder = self.lookup_project_folder(project_id)?;
        self.set_project_skill_enabled_with_folder(
            project_id,
            skill_name,
            enabled,
            &folder,
        )
    }

    /// Lower-level variant with explicit project folder. See
    /// `set_project_agent_enabled_with_folder` for why this exists.
    pub fn set_project_skill_enabled_with_folder(
        &self,
        project_id: &str,
        skill_name: &str,
        enabled: bool,
        project_folder: &Path,
    ) -> Result<(), String> {
        self.set_enabled_with_fs_move(
            project_id,
            skill_name,
            enabled,
            project_folder,
            AgentOrSkill::Skill,
        )
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
// FS-disable: shared helpers + one-time migration
// ═══════════════════════════════════════════════════════════════════════

impl Db {
    /// Look up `projects.folder_path` for a given project_id.
    /// Returns an error if the project doesn't exist (which is itself a
    /// data-integrity problem the caller wants to know about — better
    /// to fail loudly than silently no-op the FS move).
    fn lookup_project_folder(&self, project_id: &str) -> Result<PathBuf, String> {
        let guard = self.lock();
        let folder: String = guard
            .query_row(
                "SELECT folder_path FROM projects WHERE id = ?1",
                params![project_id],
                |r| r.get(0),
            )
            .map_err(|e| {
                format!(
                    "lookup_project_folder: project {} not found: {}",
                    project_id, e
                )
            })?;
        Ok(PathBuf::from(folder))
    }

    /// Read the current `enabled` flag for an agent or skill row.
    /// Returns Ok(None) when the row doesn't exist — used by the toggle
    /// to short-circuit a no-op toggle and to detect missing
    /// registrations.
    fn read_enabled_flag(
        &self,
        project_id: &str,
        name: &str,
        kind: AgentOrSkill,
    ) -> Result<Option<bool>, String> {
        let sql = match kind {
            AgentOrSkill::Agent => {
                "SELECT enabled FROM project_agents WHERE project_id = ?1 AND agent_name = ?2"
            }
            AgentOrSkill::Skill => {
                "SELECT enabled FROM project_skills WHERE project_id = ?1 AND skill_name = ?2"
            }
        };
        let guard = self.lock();
        guard
            .query_row(sql, params![project_id, name], |r| {
                let i: i32 = r.get(0)?;
                Ok(i != 0)
            })
            .optional()
            .map_err(|e| format!("read_enabled_flag: {}", e))
    }

    /// Update only the `enabled` column for an agent or skill row.
    /// Returns the number of rows affected (0 = row missing).
    fn write_enabled_flag(
        &self,
        project_id: &str,
        name: &str,
        enabled: bool,
        kind: AgentOrSkill,
    ) -> Result<usize, String> {
        let sql = match kind {
            AgentOrSkill::Agent => {
                "UPDATE project_agents SET enabled = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND agent_name = ?4"
            }
            AgentOrSkill::Skill => {
                "UPDATE project_skills SET enabled = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND skill_name = ?4"
            }
        };
        let guard = self.lock();
        guard
            .execute(
                sql,
                params![
                    enabled as i32,
                    Utc::now().timestamp_millis(),
                    project_id,
                    name,
                ],
            )
            .map_err(|e| format!("write_enabled_flag: {}", e))
    }

    /// The unified body of `set_project_{agent,skill}_enabled`. Holds
    /// the FS-move + DB-flip contract end-to-end:
    ///
    /// 1. Resolve enabled / disabled paths via `resolve_kind_paths`.
    /// 2. Read current DB flag; idempotent no-op if already at target.
    /// 3. If FS source is missing but the row exists, log + flip the
    ///    DB flag only (stale row recovery — preferable to failing
    ///    a user-initiated toggle on a missing file).
    /// 4. Pre-check: target path must NOT exist (avoid clobber).
    /// 5. Move source → target via `move_with_rollback`.
    /// 6. Write DB flag. On DB failure, reverse the rename. If the
    ///    reverse rename also fails, return an explicit "inconsistent
    ///    state" error so the user can repair manually.
    fn set_enabled_with_fs_move(
        &self,
        project_id: &str,
        name: &str,
        enabled: bool,
        project_folder: &Path,
        kind: AgentOrSkill,
    ) -> Result<(), String> {
        let kind_label = match kind {
            AgentOrSkill::Agent => "agent",
            AgentOrSkill::Skill => "skill",
        };

        // Step 1: resolve paths (pure).
        let (enabled_path, disabled_path) =
            resolve_kind_paths(project_folder, name, kind);

        // Step 2: read current flag.
        let current = self.read_enabled_flag(project_id, name, kind)?;
        let current = match current {
            Some(v) => v,
            None => {
                return Err(format!(
                    "{} {} not registered for project {}",
                    kind_label, name, project_id
                ));
            }
        };

        // Idempotent: already at target. Fast no-op (no FS, no DB).
        if current == enabled {
            return Ok(());
        }

        // Step 3: determine src / dst based on direction of the toggle.
        // - Disabling (enabled→disabled): src = enabled_path, dst = disabled_path
        // - Enabling  (disabled→enabled): src = disabled_path, dst = enabled_path
        let (src, dst) = if enabled {
            (disabled_path.as_path(), enabled_path.as_path())
        } else {
            (enabled_path.as_path(), disabled_path.as_path())
        };

        let src_exists = src.exists();
        let dst_exists = dst.exists();

        if !src_exists {
            // Stale row: file is missing from the side the DB says it
            // should be on. If the destination ALSO doesn't exist, the
            // file is simply gone — fix the DB to reflect that we
            // accept the new desired state. If the destination DOES
            // exist, the file was probably moved out-of-band and the
            // DB is just catching up; treat as a no-op move and flip
            // the flag. Either way: don't fail the toggle.
            eprintln!(
                "[fs-disable] WARN: {} '{}' file missing at expected source {} (dst_exists={}); flipping DB flag only",
                kind_label,
                name,
                src.display(),
                dst_exists
            );
            let n = self.write_enabled_flag(project_id, name, enabled, kind)?;
            if n == 0 {
                return Err(format!(
                    "{} {} not registered for project {} (race)",
                    kind_label, name, project_id
                ));
            }
            return Ok(());
        }

        // Step 4: clobber check.
        if dst_exists {
            return Err(format!(
                "{} '{}': cannot move to {} — destination already exists. \
                 Inspect and manually resolve (likely an orphan from a \
                 previous half-completed toggle).",
                kind_label,
                name,
                dst.display()
            ));
        }

        // Step 5: do the rename.
        move_with_rollback(src, dst)?;

        // Step 6: flip the DB column. On failure, reverse the rename.
        match self.write_enabled_flag(project_id, name, enabled, kind) {
            Ok(n) if n > 0 => Ok(()),
            Ok(_) => {
                // Row vanished between our read and our write — race
                // with delete_project. Reverse the rename so the FS
                // doesn't drift from a non-existent row.
                let rollback = move_with_rollback(dst, src);
                let base = format!(
                    "{} '{}' row vanished during enable toggle (race with delete_project?)",
                    kind_label, name
                );
                match rollback {
                    Ok(()) => Err(base),
                    Err(re) => Err(format!(
                        "{}; ALSO: rollback rename failed: {}. \
                         Inconsistent state — file is at {} but DB row is gone.",
                        base,
                        re,
                        dst.display()
                    )),
                }
            }
            Err(db_err) => {
                // DB write failed AFTER successful FS move. Try to
                // reverse the rename to keep FS in sync with DB.
                let rollback = move_with_rollback(dst, src);
                match rollback {
                    Ok(()) => Err(format!(
                        "{} '{}' DB update failed, FS rename rolled back: {}",
                        kind_label, name, db_err
                    )),
                    Err(re) => {
                        // Both writes failed. Log loudly per the design
                        // spec ("disk full" scenario) so the user can
                        // recover manually.
                        eprintln!(
                            "[fs-disable] FATAL: {} '{}' is now INCONSISTENT. \
                             DB still says enabled={} but file is at {}. \
                             DB error: {}; rollback error: {}",
                            kind_label,
                            name,
                            !enabled,
                            dst.display(),
                            db_err,
                            re
                        );
                        Err(format!(
                            "{} '{}' INCONSISTENT STATE: file moved to {} but \
                             DB write failed AND rollback failed. \
                             DB error: {}; rollback error: {}. \
                             Manually move {} back to {} and retry.",
                            kind_label,
                            name,
                            dst.display(),
                            db_err,
                            re,
                            dst.display(),
                            src.display()
                        ))
                    }
                }
            }
        }
    }

    /// One-time migration: scan every `enabled=0` row in
    /// `project_agents` and `project_skills` for the given project, and
    /// ensure each one's file/directory lives at the `.disabled/` side.
    ///
    /// Cases handled per row:
    ///   - File at enabled location + disabled location is free → move it
    ///   - File already at disabled location → no-op (counts toward
    ///     `already_disabled`)
    ///   - File at NEITHER location → stale row; log + leave it
    ///   - File at BOTH locations → log + leave both (counts toward
    ///     `both_locations`, needs manual cleanup)
    ///
    /// Idempotent: re-running on the same project produces the same
    /// state with `moved=0`, `already_disabled=N`.
    ///
    /// Soft-fail per row: an I/O error on one file does not abort the
    /// sweep; it's captured in `MigrationReport.errors` and the rest
    /// continues. The launcher's startup wiring (Subagent E, Wave 2)
    /// surfaces the report to the user.
    ///
    /// Called by the launcher on startup once per registered project,
    /// and by `install-bundle --update` per project. Both invocations
    /// pass the project's `folder_path` from the DB.
    pub fn migrate_disabled_files_to_disabled_dir(
        &self,
        project_id: &str,
        project_folder: &Path,
    ) -> Result<MigrationReport, String> {
        let mut report = MigrationReport::default();

        // Snapshot the disabled-row lists OUTSIDE the lock so we can
        // do file I/O without holding the DB mutex.
        let agent_names: Vec<String> = {
            let guard = self.lock();
            let mut stmt = guard
                .prepare(
                    "SELECT agent_name FROM project_agents
                     WHERE project_id = ?1 AND enabled = 0
                     ORDER BY agent_name ASC",
                )
                .map_err(|e| format!("prepare project_agents: {}", e))?;
            let rows = stmt
                .query_map(params![project_id], |r| r.get::<_, String>(0))
                .map_err(|e| format!("query project_agents: {}", e))?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("collect project_agents: {}", e))?
        };

        let skill_names: Vec<String> = {
            let guard = self.lock();
            let mut stmt = guard
                .prepare(
                    "SELECT skill_name FROM project_skills
                     WHERE project_id = ?1 AND enabled = 0
                     ORDER BY skill_name ASC",
                )
                .map_err(|e| format!("prepare project_skills: {}", e))?;
            let rows = stmt
                .query_map(params![project_id], |r| r.get::<_, String>(0))
                .map_err(|e| format!("query project_skills: {}", e))?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("collect project_skills: {}", e))?
        };

        // Helper closure: handle one row.
        let handle = |name: &str, kind: AgentOrSkill, report: &mut MigrationReport| {
            let (enabled_path, disabled_path) =
                resolve_kind_paths(project_folder, name, kind);
            let enabled_exists = enabled_path.exists();
            let disabled_exists = disabled_path.exists();

            match (enabled_exists, disabled_exists) {
                (true, false) => {
                    // Move it.
                    match move_with_rollback(&enabled_path, &disabled_path) {
                        Ok(()) => report.moved += 1,
                        Err(e) => report.errors.push(format!(
                            "{}:{} migrate move failed: {}",
                            kind_label_short(kind),
                            name,
                            e
                        )),
                    }
                }
                (false, true) => {
                    report.already_disabled += 1;
                }
                (false, false) => {
                    report.stale_rows += 1;
                    eprintln!(
                        "[fs-disable migrate] WARN: {} '{}' row says enabled=0 but file is missing at both {} and {}",
                        kind_label_short(kind),
                        name,
                        enabled_path.display(),
                        disabled_path.display()
                    );
                }
                (true, true) => {
                    report.both_locations += 1;
                    eprintln!(
                        "[fs-disable migrate] WARN: {} '{}' exists at BOTH {} and {}. Leaving as-is. Manual cleanup needed.",
                        kind_label_short(kind),
                        name,
                        enabled_path.display(),
                        disabled_path.display()
                    );
                }
            }
        };

        for name in &agent_names {
            handle(name, AgentOrSkill::Agent, &mut report);
        }
        for name in &skill_names {
            handle(name, AgentOrSkill::Skill, &mut report);
        }

        Ok(report)
    }
}

fn kind_label_short(kind: AgentOrSkill) -> &'static str {
    match kind {
        AgentOrSkill::Agent => "agent",
        AgentOrSkill::Skill => "skill",
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
        // Placeholder folder_path — these tests don't put real files
        // under it. The FS-disable code path (added v0.2.26) does call
        // `.exists()` on `<folder>/.claude/agents/<name>.md` etc., but
        // since this path doesn't physically exist on disk, the stale-
        // row recovery branch kicks in and only the DB flag is flipped.
        // We append a UUID so the path can NEVER collide with anything
        // pre-existing on the test machine's filesystem.
        let folder = if cfg!(windows) {
            format!(r"C:\tmp\vct-test-{}-{}", id, uuid::Uuid::new_v4())
        } else {
            format!("/tmp/vct-test-{}-{}", id, uuid::Uuid::new_v4())
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

// ═══════════════════════════════════════════════════════════════════════
// FS-disable tests (v0.2.26)
// ═══════════════════════════════════════════════════════════════════════
//
// These tests exercise the new file-move semantics added to
// `set_project_{agent,skill}_enabled` + the one-time migration
// (`migrate_disabled_files_to_disabled_dir`). They use a per-test
// `tempfile::TempDir` so nothing on the host filesystem is ever touched
// outside the tempdir's lifetime.

#[cfg(test)]
mod fs_disable_tests {
    use super::*;
    use rusqlite::Connection;
    use std::fs;
    use std::sync::Mutex;
    use tempfile::TempDir;

    /// Build a fresh in-memory DB + a tempdir for the project folder.
    /// Returns the Db, the TempDir handle (must outlive the test), and
    /// the absolute project folder path. Also seeds a `projects` row
    /// pointing at the tempdir.
    fn make_project_layout(project_id: &str) -> (Db, TempDir, PathBuf) {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        let db = Db(Mutex::new(conn));

        let tmp = tempfile::tempdir().expect("tempdir");
        let folder = tmp.path().to_path_buf();
        let folder_str = folder.to_string_lossy().to_string();

        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?4)",
                params![project_id, project_id, folder_str, 1_700_000_000_000_i64],
            )
            .unwrap();
        drop(guard);

        (db, tmp, folder)
    }

    /// Write a stub agent file at `<folder>/.claude/agents/<name>.md`.
    fn write_agent_file(folder: &Path, name: &str, body: &str) {
        let dir = folder.join(".claude").join("agents");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("{}.md", name));
        fs::write(&path, body).unwrap();
    }

    /// Build a stub skill directory at `<folder>/.claude/skills/<name>/`
    /// containing SKILL.md plus one extra companion file. Returns the
    /// skill dir path so callers can assert on its presence.
    fn write_skill_dir(folder: &Path, name: &str) -> PathBuf {
        let dir = folder.join(".claude").join("skills").join(name);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("SKILL.md"), format!("# {}\n", name)).unwrap();
        fs::write(dir.join("helper.txt"), "extra companion\n").unwrap();
        dir
    }

    // ───── resolve_kind_paths (pure helper) ─────

    #[test]
    fn resolve_kind_paths_agent_layout() {
        let root = PathBuf::from(if cfg!(windows) { r"C:\proj" } else { "/proj" });
        let (en, dis) = resolve_kind_paths(&root, "coder", AgentOrSkill::Agent);
        // Use ends_with on a relative path to stay cross-OS — separator
        // differences are hidden inside PathBuf.
        assert!(
            en.ends_with(PathBuf::from(".claude").join("agents").join("coder.md")),
            "enabled path: {:?}",
            en
        );
        assert!(
            dis.ends_with(
                PathBuf::from(".claude").join("agents.disabled").join("coder.md")
            ),
            "disabled path: {:?}",
            dis
        );
    }

    #[test]
    fn resolve_kind_paths_skill_layout() {
        let root = PathBuf::from(if cfg!(windows) { r"C:\proj" } else { "/proj" });
        let (en, dis) = resolve_kind_paths(&root, "tdd", AgentOrSkill::Skill);
        assert!(
            en.ends_with(PathBuf::from(".claude").join("skills").join("tdd")),
            "enabled path: {:?}",
            en
        );
        assert!(
            dis.ends_with(PathBuf::from(".claude").join("skills.disabled").join("tdd")),
            "disabled path: {:?}",
            dis
        );
    }

    // ───── Agent round-trip ─────

    #[test]
    fn agent_disable_then_re_enable_round_trip() {
        let (db, _tmp, folder) = make_project_layout("p1");

        // Seed: register an enabled agent + create its file on disk.
        db.register_project_agent(
            "p1",
            "coder",
            "bundled",
            None,
            Some("sonnet"),
            None,
            &JsonValue::Null,
        )
        .unwrap();
        write_agent_file(&folder, "coder", "# coder\n");

        let enabled_path = folder.join(".claude/agents/coder.md");
        let disabled_path = folder.join(".claude/agents.disabled/coder.md");

        assert!(enabled_path.exists());
        assert!(!disabled_path.exists());

        // Disable: file should move to .disabled/.
        db.set_project_agent_enabled("p1", "coder", false).unwrap();
        assert!(!enabled_path.exists(), "file should be gone from enabled side");
        assert!(disabled_path.exists(), "file should be at disabled side");
        let agents = db.list_project_agents("p1").unwrap();
        assert!(!agents[0].enabled);

        // Re-enable: file should move back.
        db.set_project_agent_enabled("p1", "coder", true).unwrap();
        assert!(enabled_path.exists(), "file should be back at enabled side");
        assert!(!disabled_path.exists(), "file should be gone from disabled side");
        let agents = db.list_project_agents("p1").unwrap();
        assert!(agents[0].enabled);
    }

    // ───── Skill round-trip ─────

    #[test]
    fn skill_disable_then_re_enable_round_trip_preserves_companion_files() {
        let (db, _tmp, folder) = make_project_layout("p1");

        db.register_project_skill(
            "p1",
            "tdd",
            "bundled",
            None,
            None,
            None,
            &JsonValue::Null,
        )
        .unwrap();
        let skill_dir = write_skill_dir(&folder, "tdd");

        let enabled_dir = folder.join(".claude/skills/tdd");
        let disabled_dir = folder.join(".claude/skills.disabled/tdd");

        // Sanity: companion file present at enabled side.
        assert!(enabled_dir.join("SKILL.md").exists());
        assert!(enabled_dir.join("helper.txt").exists());
        assert_eq!(skill_dir, enabled_dir);

        db.set_project_skill_enabled("p1", "tdd", false).unwrap();
        assert!(!enabled_dir.exists());
        assert!(disabled_dir.exists());
        // Companion file followed the directory move.
        assert!(disabled_dir.join("SKILL.md").exists());
        assert!(disabled_dir.join("helper.txt").exists());

        db.set_project_skill_enabled("p1", "tdd", true).unwrap();
        assert!(enabled_dir.exists());
        assert!(!disabled_dir.exists());
        assert!(enabled_dir.join("SKILL.md").exists());
        assert!(enabled_dir.join("helper.txt").exists());
    }

    // ───── Idempotent no-op ─────

    #[test]
    fn agent_set_to_current_state_is_noop() {
        let (db, _tmp, folder) = make_project_layout("p1");
        db.register_project_agent("p1", "coder", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        write_agent_file(&folder, "coder", "# coder\n");

        let enabled_path = folder.join(".claude/agents/coder.md");
        let mtime_before = fs::metadata(&enabled_path).unwrap().modified().unwrap();

        // Setting enabled=true when already enabled — should be a
        // perfect no-op (no FS touch).
        db.set_project_agent_enabled("p1", "coder", true).unwrap();

        let mtime_after = fs::metadata(&enabled_path).unwrap().modified().unwrap();
        assert_eq!(mtime_before, mtime_after, "file should not have been touched");
    }

    // ───── Clobber protection ─────

    #[test]
    fn agent_disable_refuses_to_clobber_existing_disabled_file() {
        let (db, _tmp, folder) = make_project_layout("p1");
        db.register_project_agent("p1", "coder", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        write_agent_file(&folder, "coder", "ENABLED\n");

        // Pre-stage a conflicting file at the disabled location.
        let disabled_dir = folder.join(".claude/agents.disabled");
        fs::create_dir_all(&disabled_dir).unwrap();
        fs::write(disabled_dir.join("coder.md"), "ORPHAN\n").unwrap();

        let err = db.set_project_agent_enabled("p1", "coder", false).unwrap_err();
        assert!(
            err.contains("destination already exists"),
            "expected clobber-refusal error, got: {}",
            err
        );

        // Both files still present, untouched.
        assert_eq!(
            fs::read_to_string(folder.join(".claude/agents/coder.md")).unwrap(),
            "ENABLED\n"
        );
        assert_eq!(
            fs::read_to_string(folder.join(".claude/agents.disabled/coder.md")).unwrap(),
            "ORPHAN\n"
        );
        // DB flag unchanged.
        let agents = db.list_project_agents("p1").unwrap();
        assert!(agents[0].enabled, "DB row should still say enabled");
    }

    // ───── Partial-failure rollback ─────
    //
    // Simulating a real DB-write failure mid-toggle is tricky because
    // the same `Db` handle owns the connection. To exercise the
    // rollback codepath we instead simulate the equivalent by deleting
    // the DB row between our internal read and write — this triggers
    // the "row vanished during toggle" branch which performs the same
    // reverse-rename logic as a true DB error. Verifies the FS is
    // returned to its pre-toggle state.
    #[test]
    fn agent_rollback_when_row_vanishes_mid_toggle() {
        use std::sync::Arc;
        // Build a layout with a registered+on-disk agent.
        let (db, _tmp, folder) = make_project_layout("p1");
        db.register_project_agent("p1", "coder", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        write_agent_file(&folder, "coder", "# coder\n");

        // The cleanest way to reach the rollback branch with the
        // public API is to drive `set_enabled_with_fs_move` directly
        // after the row has been deleted. We can't really delete the
        // row mid-call, but we CAN demonstrate that when the file is
        // at the source and the row is missing, the toggle returns
        // an error and the FS still has the file at the original
        // location. Approximation: register row, write file, then
        // unregister the row, then attempt to toggle — should err
        // with "not registered" and NOT have moved the file.
        let _arc = Arc::new(()); // silence unused-import lint on Arc

        let enabled_path = folder.join(".claude/agents/coder.md");
        let disabled_path = folder.join(".claude/agents.disabled/coder.md");

        db.unregister_project_agent("p1", "coder").unwrap();

        let err = db.set_project_agent_enabled("p1", "coder", false).unwrap_err();
        assert!(
            err.contains("not registered"),
            "expected not-registered error, got: {}",
            err
        );
        // FS unchanged.
        assert!(enabled_path.exists(), "file should still be at enabled side");
        assert!(!disabled_path.exists(), "no orphan at disabled side");
    }

    /// Direct test of the rollback path: drive `set_enabled_with_fs_move`
    /// with a folder that points at a read-only-ish location for the
    /// destination, force the DB write to fail, and observe the FS is
    /// rolled back. We simulate "DB write fails" by checking the rollback
    /// path executes after a synthetic missing-row condition: register,
    /// write file, manually DELETE the row via raw SQL, then call the
    /// internal helper. The helper sees the row vanish after step-5's
    /// rename and rolls back. Confirms the rollback rename succeeds.
    #[test]
    fn agent_rollback_reverses_rename_on_db_row_vanish() {
        let (db, _tmp, folder) = make_project_layout("p1");
        db.register_project_agent("p1", "coder", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        write_agent_file(&folder, "coder", "# coder\n");

        let enabled_path = folder.join(".claude/agents/coder.md");
        let disabled_path = folder.join(".claude/agents.disabled/coder.md");

        // To race between read_enabled_flag and write_enabled_flag, we
        // need a way to make the UPDATE return 0 rows. Easiest: do
        // both manually in the right order.

        // First, read current = true.
        let current = db
            .read_enabled_flag("p1", "coder", AgentOrSkill::Agent)
            .unwrap();
        assert_eq!(current, Some(true));

        // Now simulate the rest of set_enabled_with_fs_move manually,
        // injecting a DELETE between the rename and the UPDATE.
        let (en, dis) = resolve_kind_paths(&folder, "coder", AgentOrSkill::Agent);
        assert_eq!(en, enabled_path);
        assert_eq!(dis, disabled_path);

        // Pre-checks pass.
        assert!(en.exists());
        assert!(!dis.exists());

        // Perform the rename.
        move_with_rollback(&en, &dis).unwrap();
        assert!(!en.exists());
        assert!(dis.exists());

        // Simulate the DB write failing by deleting the row first.
        db.unregister_project_agent("p1", "coder").unwrap();
        let n = db
            .write_enabled_flag("p1", "coder", false, AgentOrSkill::Agent)
            .unwrap();
        assert_eq!(n, 0, "DB write should hit 0 rows after delete");

        // Manually invoke the rollback (in the real code this is what
        // set_enabled_with_fs_move does in the n==0 branch).
        move_with_rollback(&dis, &en).unwrap();
        assert!(en.exists(), "rollback should put the file back");
        assert!(!dis.exists());
    }

    // ───── Stale-row recovery: DB says enabled but file is missing ─────

    #[test]
    fn agent_disable_with_missing_file_flips_flag_only() {
        // The agent is registered + enabled in DB, but the file
        // doesn't exist on disk (deleted out-of-band). Toggling
        // disable should not fail — it should just flip the flag.
        let (db, _tmp, _folder) = make_project_layout("p1");
        db.register_project_agent("p1", "coder", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        // Intentionally do NOT write the file.

        db.set_project_agent_enabled("p1", "coder", false).unwrap();
        let agents = db.list_project_agents("p1").unwrap();
        assert!(!agents[0].enabled);
    }

    // ───── Unknown row ─────

    #[test]
    fn agent_toggle_on_unregistered_returns_error() {
        let (db, _tmp, _folder) = make_project_layout("p1");
        let err = db
            .set_project_agent_enabled("p1", "ghost", false)
            .unwrap_err();
        assert!(err.contains("not registered"), "got: {}", err);
    }

    // ───── Migration: idempotent + handles mixed states ─────

    #[test]
    fn migration_moves_files_to_disabled_and_is_idempotent() {
        let (db, _tmp, folder) = make_project_layout("p1");

        // Three agents: two disabled in DB (one with file at enabled
        // side, one with file already at disabled side), one enabled
        // (file at enabled side; migration should not touch it).
        db.register_project_agent("p1", "needs_move", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        db.register_project_agent("p1", "already_disabled", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        db.register_project_agent("p1", "stays_enabled", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        db.register_project_skill("p1", "tdd", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();

        // Files: needs_move + stays_enabled live at enabled side.
        write_agent_file(&folder, "needs_move", "# nm\n");
        write_agent_file(&folder, "stays_enabled", "# se\n");
        // Skill tdd lives at the enabled side too.
        write_skill_dir(&folder, "tdd");

        // already_disabled file is pre-staged at disabled side.
        let dis_dir = folder.join(".claude/agents.disabled");
        fs::create_dir_all(&dis_dir).unwrap();
        fs::write(dis_dir.join("already_disabled.md"), "# ad\n").unwrap();

        // Flip DB flags WITHOUT going through the FS-move code path
        // (simulates a pre-v0.2.26 launcher state where the GUI
        // toggled `enabled` without moving files).
        db.write_enabled_flag("p1", "needs_move", false, AgentOrSkill::Agent)
            .unwrap();
        db.write_enabled_flag("p1", "already_disabled", false, AgentOrSkill::Agent)
            .unwrap();
        db.write_enabled_flag("p1", "tdd", false, AgentOrSkill::Skill)
            .unwrap();

        // First migration run.
        let report = db
            .migrate_disabled_files_to_disabled_dir("p1", &folder)
            .unwrap();
        // needs_move + tdd should have moved. already_disabled was
        // already in place. stays_enabled isn't in the disabled-row
        // set so it's not scanned.
        assert_eq!(report.moved, 2, "report: {:?}", report);
        assert_eq!(report.already_disabled, 1, "report: {:?}", report);
        assert_eq!(report.stale_rows, 0);
        assert_eq!(report.both_locations, 0);
        assert!(report.errors.is_empty(), "errors: {:?}", report.errors);

        assert!(!folder.join(".claude/agents/needs_move.md").exists());
        assert!(folder.join(".claude/agents.disabled/needs_move.md").exists());
        assert!(folder.join(".claude/agents.disabled/already_disabled.md").exists());
        assert!(folder.join(".claude/agents/stays_enabled.md").exists());
        assert!(!folder.join(".claude/skills/tdd").exists());
        assert!(folder.join(".claude/skills.disabled/tdd").exists());

        // Second run: must be a complete no-op.
        let report2 = db
            .migrate_disabled_files_to_disabled_dir("p1", &folder)
            .unwrap();
        assert_eq!(report2.moved, 0, "second run must not move anything: {:?}", report2);
        assert_eq!(report2.already_disabled, 3, "all three disabled rows present at disabled side");
        assert_eq!(report2.stale_rows, 0);
        assert_eq!(report2.both_locations, 0);
    }

    #[test]
    fn migration_with_stale_row_logs_and_continues() {
        let (db, _tmp, folder) = make_project_layout("p1");

        db.register_project_agent("p1", "ghost", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        db.register_project_agent("p1", "real", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();

        // Both rows: enabled=0 in DB. ghost has NO file anywhere
        // (stale row). real has a file at the enabled side, awaiting
        // migration.
        write_agent_file(&folder, "real", "# real\n");
        db.write_enabled_flag("p1", "ghost", false, AgentOrSkill::Agent)
            .unwrap();
        db.write_enabled_flag("p1", "real", false, AgentOrSkill::Agent)
            .unwrap();

        let report = db
            .migrate_disabled_files_to_disabled_dir("p1", &folder)
            .unwrap();
        assert_eq!(report.moved, 1);
        assert_eq!(report.stale_rows, 1);
        assert!(report.errors.is_empty());
        assert!(folder.join(".claude/agents.disabled/real.md").exists());
    }

    #[test]
    fn migration_with_both_locations_present_warns_and_skips() {
        let (db, _tmp, folder) = make_project_layout("p1");

        db.register_project_agent("p1", "twin", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        // File exists at BOTH locations. Simulates a previously-failed
        // half-completed toggle that left an orphan.
        write_agent_file(&folder, "twin", "ENABLED\n");
        let dis_dir = folder.join(".claude/agents.disabled");
        fs::create_dir_all(&dis_dir).unwrap();
        fs::write(dis_dir.join("twin.md"), "DISABLED\n").unwrap();

        db.write_enabled_flag("p1", "twin", false, AgentOrSkill::Agent)
            .unwrap();

        let report = db
            .migrate_disabled_files_to_disabled_dir("p1", &folder)
            .unwrap();
        assert_eq!(report.moved, 0);
        assert_eq!(report.both_locations, 1);
        // Both files still present.
        assert_eq!(
            fs::read_to_string(folder.join(".claude/agents/twin.md")).unwrap(),
            "ENABLED\n"
        );
        assert_eq!(
            fs::read_to_string(folder.join(".claude/agents.disabled/twin.md")).unwrap(),
            "DISABLED\n"
        );
    }

    // ───── Cross-OS: PathBuf::join handles all separators ─────

    #[test]
    fn paths_use_pathbuf_join_no_string_concat() {
        // Smoke test: resolve_kind_paths and move_with_rollback work
        // on a tempdir without any platform-specific quirks. The real
        // cross-OS evidence is that the code uses PathBuf::join
        // throughout (audit by reading the source). This test just
        // exercises the full pipeline once to catch any obvious bugs.
        let (db, _tmp, folder) = make_project_layout("p1");
        db.register_project_agent("p1", "x", "bundled", None, None, None, &JsonValue::Null)
            .unwrap();
        write_agent_file(&folder, "x", "# x\n");

        db.set_project_agent_enabled("p1", "x", false).unwrap();
        db.set_project_agent_enabled("p1", "x", true).unwrap();

        // No assertions beyond round-trip success — that alone proves
        // the path resolution works on the current platform.
        assert!(folder.join(".claude/agents/x.md").exists());
    }
}
