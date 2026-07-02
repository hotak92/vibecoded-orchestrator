//! Tauri commands exposing per-project orchestrator state to the React UI.
//!
//! Backed by `crate::db::project_state`. Mutations call `db.audit(...)`
//! so the audit log records who changed what (without recording values).
//!
//! NEW-9 (v0.2.53) — `set_project_agent_enabled` and
//! `set_project_skill_enabled` now perform the **FS-disable contract**:
//! enabling/disabling an agent or skill also moves the corresponding
//! `.md` file (or skill directory) between `.claude/agents/` and
//! `.claude/agents.disabled/` (resp. skills). Without this move, a
//! subsequent `install-bundle --update` would re-overwrite the
//! enabled-side file from the orchestrator's template, silently
//! re-enabling the user-disabled agent/skill. See
//! `.claude/context/audits/project-bundle-install-audit-2026-06-10.md`
//! §6.5 / B2 for the audit + verdict.

use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::Value as JsonValue;
use tauri::{command, State};

use crate::db::project_mcp_servers::ProjectMcpServer;
use crate::db::project_state::{
    ProjectAgent, ProjectCodegraphBinding, ProjectHook, ProjectKgBinding, ProjectPermission,
    ProjectSecretRef, ProjectSkill, ProjectStateSnapshot,
};
use crate::db::Db;

// ─── FS-disable helpers (NEW-9, v0.2.53) ────────────────────────────────

/// Compute the enabled-side and disabled-side paths for an agent.
///
/// Agents are single `.md` files at `.claude/agents/<name>.md`. When
/// disabled, they live at `.claude/agents.disabled/<name>.md`.
fn agent_paths(folder: &Path, agent_name: &str) -> (PathBuf, PathBuf) {
    let enabled = folder
        .join(".claude")
        .join("agents")
        .join(format!("{}.md", agent_name));
    let disabled = folder
        .join(".claude")
        .join("agents.disabled")
        .join(format!("{}.md", agent_name));
    (enabled, disabled)
}

/// Compute the enabled-side and disabled-side paths for a skill.
///
/// Skills are whole directories at `.claude/skills/<name>/`. When
/// disabled, they live at `.claude/skills.disabled/<name>/`.
fn skill_paths(folder: &Path, skill_name: &str) -> (PathBuf, PathBuf) {
    let enabled = folder.join(".claude").join("skills").join(skill_name);
    let disabled = folder
        .join(".claude")
        .join("skills.disabled")
        .join(skill_name);
    (enabled, disabled)
}

/// Move `src` to `dst`, creating the parent directory of `dst` first.
/// Cross-OS: `fs::rename` is POSIX rename / Windows MoveFileExW with
/// REPLACE_EXISTING semantics; atomic per the OS's contract.
///
/// Returns `Ok(true)` when the move succeeded, `Ok(false)` when `src`
/// did not exist (caller decides if that's an error or a no-op), and
/// `Err(_)` on any other I/O error.
fn move_path_if_exists(src: &Path, dst: &Path) -> Result<bool, String> {
    if !src.exists() {
        return Ok(false);
    }
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create_dir_all({}): {}", parent.display(), e))?;
    }
    // If the destination ALREADY exists (corrupt state — file in both
    // locations), refuse to clobber. The populate-time both-locations
    // warning surfaces this state to the user, who can clean up
    // manually. Silently overwriting could destroy user edits made to
    // the destination side.
    if dst.exists() {
        return Err(format!(
            "FS-disable refusing to move {} → {}: destination already exists. \
             The agent/skill is registered in both enabled and disabled \
             locations; clean up the duplicate manually before toggling.",
            src.display(),
            dst.display(),
        ));
    }
    fs::rename(src, dst).map_err(|e| {
        format!(
            "FS-disable rename {} → {}: {}",
            src.display(),
            dst.display(),
            e
        )
    })?;
    Ok(true)
}

/// Apply the FS-disable contract for an agent: move the `.md` file
/// between `.claude/agents/` and `.claude/agents.disabled/` based on
/// `enabled`. Soft-fail when the source file doesn't exist (the user
/// may have removed it manually; the DB flag still gets flipped).
///
/// `folder` is the project's filesystem root. The function never
/// touches files outside `.claude/agents/`** / `.claude/agents.disabled/`.
fn apply_fs_disable_agent(folder: &Path, agent_name: &str, enabled: bool) -> Result<(), String> {
    let (enabled_path, disabled_path) = agent_paths(folder, agent_name);
    let (src, dst) = if enabled {
        // Enabling: disabled → enabled.
        (disabled_path, enabled_path)
    } else {
        // Disabling: enabled → disabled.
        (enabled_path, disabled_path)
    };
    let _moved = move_path_if_exists(&src, &dst)?;
    // Soft-fail on src-missing: it's possible the user manually moved
    // the file (or it never existed). DB-only state remains
    // authoritative when no FS effect is possible.
    Ok(())
}

/// Apply the FS-disable contract for a skill: same shape as
/// `apply_fs_disable_agent` but operating on whole directories.
///
/// `fs::rename` works for directories on POSIX (atomic same-FS) and
/// Windows (when both paths are on the same volume). Cross-volume
/// directory renames will fail with EXDEV / Win-equivalent; the
/// caller surfaces the error to the user.
fn apply_fs_disable_skill(folder: &Path, skill_name: &str, enabled: bool) -> Result<(), String> {
    let (enabled_path, disabled_path) = skill_paths(folder, skill_name);
    let (src, dst) = if enabled {
        (disabled_path, enabled_path)
    } else {
        (enabled_path, disabled_path)
    };
    let _moved = move_path_if_exists(&src, &dst)?;
    Ok(())
}

// ─── Read ────────────────────────────────────────────────────────────────

#[command]
pub async fn list_project_agents(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ProjectAgent>, String> {
    db.list_project_agents(&project_id)
}

#[command]
pub async fn list_project_skills(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ProjectSkill>, String> {
    db.list_project_skills(&project_id)
}

#[command]
pub async fn list_project_hooks(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ProjectHook>, String> {
    db.list_project_hooks(&project_id)
}

#[command]
pub async fn list_project_permissions(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ProjectPermission>, String> {
    db.list_project_permissions(&project_id)
}

#[command]
pub async fn list_project_secret_refs(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ProjectSecretRef>, String> {
    db.list_project_secret_refs(&project_id)
}

#[command]
pub async fn get_project_state_snapshot(
    project_id: String,
    db: State<'_, Db>,
) -> Result<ProjectStateSnapshot, String> {
    db.get_project_state_snapshot(&project_id)
}

// ─── v0.2.22 item #17: manual re-scan from disk ──────────────────────────
//
// The launcher GUI's Agents/Skills/Hooks tabs are backed by SQL queries
// against the per-project DB tables. If a project's tables get out of
// sync with the on-disk `.claude/` (orchestrator-root projects pre-v0.2.22
// never populated their tables; user-deleted DB rows; user-added files
// to `.claude/` between launcher boots), the tabs show "No agents
// registered." despite files being present on disk.
//
// The empty-state of each tab surfaces a "Re-scan from disk" button
// that invokes this command. It runs `populate_project_state_from_filesystem`
// (idempotent UPSERT — preserves user toggles), then returns the
// inserted-row counts so the GUI can show a toast like
// "Re-scanned: 65 agents, 52 skills, 28 hooks".

/// Summary of what `rescan_project_from_filesystem` did. The shapes
/// mirror `PopulateReport` (defined in commands/project_state_populate.rs)
/// but with a serializable representation suitable for IPC.
#[derive(Debug, Clone, serde::Serialize)]
pub struct RescanReport {
    pub agents_inserted: usize,
    pub skills_inserted: usize,
    pub hooks_inserted: usize,
    pub mcp_servers_inserted: usize,
    pub kg_access_rows_inserted: usize,
    pub warnings: Vec<String>,
}

#[command]
pub async fn rescan_project_from_filesystem(
    project_id: String,
    db: State<'_, Db>,
) -> Result<RescanReport, String> {
    // Resolve the project's folder_path from the DB. Caller must own a
    // valid project_id; if the row is gone, we surface a clear error
    // instead of silently no-oping.
    let project = db
        .get_project(&project_id)
        .map_err(|e| format!("get_project: {}", e))?
        .ok_or_else(|| format!("project '{}' not found", project_id))?;

    let folder = std::path::Path::new(&project.folder_path);
    if !folder.is_dir() {
        return Err(format!(
            "project folder no longer exists on disk: {} \
             (was the project moved/deleted? edit the path via Settings or remove the project)",
            project.folder_path
        ));
    }

    let report = crate::commands::project_state_populate::populate_project_state_from_filesystem(
        &project_id,
        &project.name,
        folder,
        db.inner(),
    );

    // Audit so the change-log shows the manual action.
    db.audit(
        "project_rescan_from_filesystem",
        Some(&project_id),
        None,
        &serde_json::json!({
            "agents_inserted": report.agents_inserted,
            "skills_inserted": report.skills_inserted,
            "hooks_inserted": report.hooks_inserted,
            "mcp_servers_inserted": report.mcp_servers_inserted,
            "warning_count": report.warnings.len(),
        }),
    )?;

    Ok(RescanReport {
        agents_inserted: report.agents_inserted,
        skills_inserted: report.skills_inserted,
        hooks_inserted: report.hooks_inserted,
        mcp_servers_inserted: report.mcp_servers_inserted,
        kg_access_rows_inserted: report.kg_access_rows_inserted,
        warnings: report.warnings,
    })
}

// ─── MCP servers (migration 010, 2026-05-10) ─────────────────────────────
//
// Resolves the KNOWN_ISSUES.md "Custom MCP tab is not populated by initial
// project registration" entry. The Custom MCP tab calls
// `list_user_added_project_mcp_servers` to surface only entries where
// `is_user_added=true` (anything beyond the bundled allowlist in
// `crate::db::project_mcp_servers::BUNDLED_MCP_NAMES`).
//
// The full unfiltered list is also exposed so a future "all MCPs" tab
// can render bundled + user-added together.

#[command]
pub async fn list_project_mcp_servers(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ProjectMcpServer>, String> {
    db.list_project_mcp_servers(&project_id)
}

#[command]
pub async fn list_user_added_project_mcp_servers(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ProjectMcpServer>, String> {
    db.list_user_added_mcp_servers(&project_id)
}

#[command]
pub async fn set_project_mcp_server_enabled(
    project_id: String,
    mcp_name: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_project_mcp_server_enabled(&project_id, &mcp_name, enabled)?;
    db.audit(
        "project_mcp_server_set_enabled",
        Some(&project_id),
        None,
        &serde_json::json!({ "mcp": mcp_name, "enabled": enabled }),
    )?;
    Ok(())
}

#[command]
pub async fn unregister_project_mcp_server(
    project_id: String,
    mcp_name: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.unregister_project_mcp_server(&project_id, &mcp_name)?;
    db.audit(
        "project_mcp_server_unregister",
        Some(&project_id),
        None,
        &serde_json::json!({ "mcp": mcp_name }),
    )?;
    Ok(())
}

// ─── Mutations ───────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct RegisterAgentReq {
    pub agent_name: String,
    pub source: String,
    pub source_module: Option<String>,
    pub model: Option<String>,
    pub file_path: Option<String>,
    #[serde(default)]
    pub config: JsonValue,
}

#[command]
pub async fn register_project_agent(
    project_id: String,
    req: RegisterAgentReq,
    db: State<'_, Db>,
) -> Result<ProjectAgent, String> {
    let row = db.register_project_agent(
        &project_id,
        &req.agent_name,
        &req.source,
        req.source_module.as_deref(),
        req.model.as_deref(),
        req.file_path.as_deref(),
        &req.config,
    )?;
    db.audit(
        "project_agent_register",
        Some(&project_id),
        req.source_module.as_deref(),
        &serde_json::json!({ "agent": req.agent_name, "source": req.source }),
    )?;
    Ok(row)
}

#[command]
pub async fn set_project_agent_enabled(
    project_id: String,
    agent_name: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // NEW-9 (v0.2.53) — apply the FS-disable contract BEFORE flipping the
    // DB flag. If the FS move fails (e.g. corrupt both-locations state),
    // refuse the toggle so the DB doesn't end up out-of-sync with disk.
    // If the source file is simply missing (user removed it manually),
    // the move is a no-op and we still flip the DB row — DB remains
    // authoritative for the soft-fail case.
    if let Some(project) = db.get_project(&project_id)? {
        let folder = Path::new(&project.folder_path);
        apply_fs_disable_agent(folder, &agent_name, enabled)?;
    }
    // If get_project returns None (project deleted between read+write —
    // unlikely but defensive), let the db.set_project_agent_enabled
    // call surface the foreign-key/row-missing error in its own
    // diagnostic shape.

    db.set_project_agent_enabled(&project_id, &agent_name, enabled)?;
    db.audit(
        "project_agent_set_enabled",
        Some(&project_id),
        None,
        &serde_json::json!({ "agent": agent_name, "enabled": enabled }),
    )?;
    Ok(())
}

#[command]
pub async fn unregister_project_agent(
    project_id: String,
    agent_name: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.unregister_project_agent(&project_id, &agent_name)?;
    db.audit(
        "project_agent_unregister",
        Some(&project_id),
        None,
        &serde_json::json!({ "agent": agent_name }),
    )?;
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct RegisterSkillReq {
    pub skill_name: String,
    pub source: String,
    pub source_module: Option<String>,
    pub model: Option<String>,
    pub file_path: Option<String>,
    #[serde(default)]
    pub config: JsonValue,
}

#[command]
pub async fn register_project_skill(
    project_id: String,
    req: RegisterSkillReq,
    db: State<'_, Db>,
) -> Result<ProjectSkill, String> {
    let row = db.register_project_skill(
        &project_id,
        &req.skill_name,
        &req.source,
        req.source_module.as_deref(),
        req.model.as_deref(),
        req.file_path.as_deref(),
        &req.config,
    )?;
    db.audit(
        "project_skill_register",
        Some(&project_id),
        req.source_module.as_deref(),
        &serde_json::json!({ "skill": req.skill_name, "source": req.source }),
    )?;
    Ok(row)
}

#[command]
pub async fn set_project_skill_enabled(
    project_id: String,
    skill_name: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // NEW-9 (v0.2.53) — see comment on set_project_agent_enabled for the
    // FS-disable contract rationale. Same pattern, operating on the
    // skill's whole directory rather than a single .md file.
    if let Some(project) = db.get_project(&project_id)? {
        let folder = Path::new(&project.folder_path);
        apply_fs_disable_skill(folder, &skill_name, enabled)?;
    }
    db.set_project_skill_enabled(&project_id, &skill_name, enabled)
}

#[command]
pub async fn unregister_project_skill(
    project_id: String,
    skill_name: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.unregister_project_skill(&project_id, &skill_name)
}

#[derive(Debug, Deserialize)]
pub struct RegisterHookReq {
    pub event: String,
    #[serde(default)]
    pub matcher: String,
    pub command: String,
    #[serde(default = "default_source")]
    pub source: String,
    pub source_module: Option<String>,
    pub timeout_ms: Option<i64>,
    #[serde(default)]
    pub config: JsonValue,
}
fn default_source() -> String {
    "project".to_string()
}

#[command]
pub async fn register_project_hook(
    project_id: String,
    req: RegisterHookReq,
    db: State<'_, Db>,
) -> Result<ProjectHook, String> {
    db.register_project_hook(
        &project_id,
        &req.event,
        &req.matcher,
        &req.command,
        &req.source,
        req.source_module.as_deref(),
        req.timeout_ms,
        &req.config,
    )
}

#[command]
pub async fn set_project_hook_enabled(
    hook_id: i64,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_project_hook_enabled(hook_id, enabled)
}

#[command]
pub async fn unregister_project_hook(hook_id: i64, db: State<'_, Db>) -> Result<(), String> {
    db.unregister_project_hook(hook_id)
}

#[derive(Debug, Deserialize)]
pub struct AddPermissionReq {
    pub subject: String,
    pub kind: String,
    pub value: String,
    #[serde(default)]
    pub config: JsonValue,
}

#[command]
pub async fn add_project_permission(
    project_id: String,
    req: AddPermissionReq,
    db: State<'_, Db>,
) -> Result<ProjectPermission, String> {
    let row = db.add_project_permission(
        &project_id,
        &req.subject,
        &req.kind,
        &req.value,
        &req.config,
    )?;
    db.audit(
        "project_permission_add",
        Some(&project_id),
        None,
        &serde_json::json!({
            "subject": req.subject, "kind": req.kind, "value": req.value
        }),
    )?;
    Ok(row)
}

#[command]
pub async fn delete_project_permission(perm_id: i64, db: State<'_, Db>) -> Result<(), String> {
    db.delete_project_permission(perm_id)
}

// ─── 0.2.x backlog #5: per-project MCP toggle ───────────────────────────
//
// MCP servers (weaviate-kg, ollama, search, custom user-added servers) are
// configured globally in `~/.claude.json mcpServers`. By default Claude
// Code spawns every enabled MCP server for every project the user opens.
// Power users want per-project enable/disable: a project that doesn't need
// browser automation shouldn't pay the startup cost of e.g. `playwright`.
//
// Storage: `project_permissions` rows with:
//   * `kind = 'mcp_server'`
//   * `value = <mcp server id>`  (e.g. "playwright", "weaviate-kg")
//   * `subject = '@project'`     (per-project gate, not per-subagent)
//   * `config = {"enabled": false}` when the user explicitly disabled.
//
// Semantics:
//   * NO ROW for (project_id, server_id) → DEFAULT-ENABLED. Backwards-
//     compatible: every project pre-0.2.x has zero rows and sees every
//     server, same as before.
//   * Row with `config.enabled = false` → disabled for this project. The
//     env-writer emits the server_id into `.claude/settings.json`'s
//     `disabledMcpjsonServers` array so Claude Code skips it.
//   * Row with `config.enabled = true`  → explicitly enabled (rare; the
//     enabled command path DELETES instead of writing this state, so the
//     row falls back to the default. Kept legible for future "explicit
//     enable wins over global disable" semantics if we ever add a
//     machine-wide kill switch.)

const MCP_PERMISSION_SUBJECT: &str = "@project";

/// One row in `list_project_mcp_permissions`'s response.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ProjectMcpPermission {
    pub server_id: String,
    pub enabled: bool,
    /// True iff the launcher has an explicit `project_permissions` row
    /// for this (project, server). When false, `enabled` is reporting
    /// the default state and the GUI should NOT render an active-toggle
    /// indicator beyond the regular on/off switch.
    pub explicit: bool,
}

/// List the per-project enable/disable state for every MCP server the
/// caller is interested in. Caller passes the canonical list of server
/// IDs (typically the keys from `get_mcp_servers()` + any custom servers
/// from `~/.claude.json`); the backend looks up each one and reports
/// the resolved state.
///
/// Rationale for caller-supplied IDs: the `mcp_servers` source-of-truth
/// is `OrchestratorConfig.mcp_servers` (in `~/.vct/orchestrator.json`)
/// + `~/.claude.json` for custom user-added servers — both live OUTSIDE
/// this DB. Asking the caller to pass IDs keeps this command pure-DB
/// (no JSON file probes) and lets the GUI render `<list of all servers>
/// + <toggle for each>` in one round trip without a second API call.
#[command]
pub async fn list_project_mcp_permissions(
    project_id: String,
    server_ids: Vec<String>,
    db: State<'_, Db>,
) -> Result<Vec<ProjectMcpPermission>, String> {
    // Single query for every (project_id, kind='mcp_server') row, then
    // join in-memory against `server_ids`. The mcp_server permission
    // count per project is bounded (one row per known MCP server) so
    // the table scan is cheap; we keep the SELECT WHERE kind='mcp_server'
    // narrow rather than joining each server_id with a separate query.
    let rows = db.list_project_permissions(&project_id)?;
    let mut out: Vec<ProjectMcpPermission> = Vec::with_capacity(server_ids.len());
    for server_id in &server_ids {
        let row = rows.iter().find(|r| {
            r.kind == "mcp_server"
                && r.subject == MCP_PERMISSION_SUBJECT
                && r.value == *server_id
        });
        let (enabled, explicit) = match row {
            Some(r) => {
                // Pull `config.enabled` if present; default to true when
                // the config object doesn't carry the field (forward-
                // compat with any future field additions).
                let enabled = r
                    .config
                    .get("enabled")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(true);
                (enabled, true)
            }
            None => (true, false),
        };
        out.push(ProjectMcpPermission {
            server_id: server_id.clone(),
            enabled,
            explicit,
        });
    }
    Ok(out)
}

/// Toggle a per-project MCP server's enabled state.
///
/// * `enabled = true`  → DELETE any explicit row so the (project, server)
///   pair falls back to the default-enabled state. No-op when there was
///   no row to begin with.
/// * `enabled = false` → UPSERT a row with `config.enabled = false`. The
///   env-writer reads this row's existence into the
///   `.claude/settings.json` `disabledMcpjsonServers` array.
///
/// The DELETE-on-enable shape (rather than UPSERT-with-enabled=true)
/// keeps the table small AND lets a future global-default flip from
/// enabled to disabled work without rewriting every existing row.
#[command]
pub async fn set_project_mcp_permission(
    project_id: String,
    server_id: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    if server_id.is_empty() {
        return Err("server_id must not be empty".to_string());
    }
    if enabled {
        db.delete_project_permission_by_key(
            &project_id,
            MCP_PERMISSION_SUBJECT,
            "mcp_server",
            &server_id,
        )?;
        db.audit(
            "project_mcp_permission_enable",
            Some(&project_id),
            None,
            &serde_json::json!({ "server_id": server_id }),
        )?;
    } else {
        // Upsert a disabled row. `add_project_permission` is upsert by
        // (project_id, subject, kind, value).
        db.add_project_permission(
            &project_id,
            MCP_PERMISSION_SUBJECT,
            "mcp_server",
            &server_id,
            &serde_json::json!({ "enabled": false }),
        )?;
        db.audit(
            "project_mcp_permission_disable",
            Some(&project_id),
            None,
            &serde_json::json!({ "server_id": server_id }),
        )?;
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct SetSecretRefReq {
    pub secret_key: String,
    pub resolution: String,
    pub file_path: Option<String>,
    pub env_name: Option<String>,
    pub source_module: Option<String>,
    #[serde(default)]
    pub required_for: Vec<String>,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub is_set: bool,
}

#[command]
pub async fn set_project_secret_ref(
    project_id: String,
    req: SetSecretRefReq,
    db: State<'_, Db>,
) -> Result<ProjectSecretRef, String> {
    let row = db.set_project_secret_ref(
        &project_id,
        &req.secret_key,
        &req.resolution,
        req.file_path.as_deref(),
        req.env_name.as_deref(),
        req.source_module.as_deref(),
        &req.required_for,
        &req.description,
        req.is_set,
    )?;
    // NB: audit logs the secret key only — never the value (no value here anyway).
    db.audit(
        "project_secret_ref_set",
        Some(&project_id),
        req.source_module.as_deref(),
        &serde_json::json!({ "key": req.secret_key, "resolution": req.resolution }),
    )?;
    Ok(row)
}

#[command]
pub async fn delete_project_secret_ref(
    project_id: String,
    secret_key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.delete_project_secret_ref(&project_id, &secret_key)?;
    db.audit(
        "project_secret_ref_delete",
        Some(&project_id),
        None,
        &serde_json::json!({ "key": secret_key }),
    )?;
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct SetKgBindingReq {
    #[serde(default = "default_kg_role")]
    pub role: String,
    pub collection_name: String,
    pub embedding_model: Option<String>,
    pub embedding_dim: Option<i64>,
    pub kg_dir_path: Option<String>,
    pub weaviate_url: Option<String>,
    #[serde(default)]
    pub config: JsonValue,
}
fn default_kg_role() -> String {
    "primary".to_string()
}

#[command]
pub async fn set_project_kg_binding(
    project_id: String,
    req: SetKgBindingReq,
    app: tauri::AppHandle,
    db: State<'_, Db>,
) -> Result<ProjectKgBinding, String> {
    // v0.2.18 Commit 8: when an embedding_model is supplied, verify it
    // against the live catalog so a typo doesn't silently break the
    // seed pipeline. Empty/None passes through unchanged (caller is
    // explicitly clearing or leaving the existing binding alone).
    if let Some(model_id) = req.embedding_model.as_deref().filter(|s| !s.trim().is_empty()) {
        let result = crate::commands::embedding_catalog::validate_model_against_catalog(
            model_id.trim().to_string(),
            crate::commands::embedding_catalog::ModelKind::Text,
            db.clone(),
        )
        .await?;
        if let crate::commands::embedding_catalog::ValidationResult::Invalid { reason } = result {
            return Err(format!(
                "embedding_model '{}' not valid for KG binding: {}",
                model_id, reason
            ));
        }
    }

    // F3 (v0.2.72): the row write's post-projection can now be
    // machine-global (F2: orchestrator-root rebind → refresh ALL projects,
    // N serial Python subprocesses) — run the sync core on the blocking
    // pool. The write's own Result rides through the join.
    let role_for_ensure = req.role.clone();
    let collection_for_ensure = req.collection_name.clone();
    let weaviate_url_for_ensure = req.weaviate_url.clone();
    let project_id_for_task = project_id.clone();
    let row = crate::commands::blocking::run_with_db_on_blocking_pool(
        app,
        "set_project_kg_binding",
        move |db| set_project_kg_binding_row_with_db(db, &project_id_for_task, &req),
    )
    .await??;

    // Bug 2 (2026-04-28): also ensure the Weaviate collection exists.
    // Previously the binding was registered in the DB but the
    // collection itself only got created lazily by sync_knowledge_graph.py
    // — meaning a fresh project's KG tab rendered 0 nodes (or errored)
    // until the user manually ran kg-sync. We now POST /v1/schema with
    // the canonical multi-named-vector schema (qwen3_embed primary +
    // legacy ollama_embed + openai_embed) at binding time. Idempotent:
    // if the collection already exists Weaviate returns 422 and we
    // treat that as success. Failure to reach Weaviate is non-fatal —
    // we log a warning and let the user retry from the GUI.
    if matches!(role_for_ensure.as_str(), "primary") {
        let weaviate_url = weaviate_url_for_ensure
            .as_deref()
            .unwrap_or("http://localhost:8081");
        if let Err(e) = ensure_kg_collection(weaviate_url, &collection_for_ensure).await {
            eprintln!(
                "[vct] warning: ensure_kg_collection({}) on {}: {}",
                collection_for_ensure, weaviate_url, e
            );
        }
    }

    Ok(row)
}

/// F2 (v0.2.72): which env re-projection ran after a KG-binding write.
/// The variant is the observable proof (for tests and callers) of which
/// scope the projection covered.
///
/// The report payloads are read only by the unit tests (production
/// callers log inside `reproject_after_kg_binding_change` and discard) —
/// hence the non-test dead_code allowance.
#[derive(Debug)]
#[cfg_attr(not(test), allow(dead_code))]
pub enum KgBindingReprojection {
    /// The orchestrator-root's primary/shared binding changed. EVERY
    /// project's projected SHARED_KG_COLLECTION derives from that binding
    /// (see `vco_lib/config_projection.py::
    /// _resolve_shared_kg_default_from_launcher_db`), so a single-project
    /// re-projection would leave every PEER project's settings.json stale
    /// — and the ensuing global SIGHUP would respawn their MCPs WITH the
    /// stale env. Machine-global refresh instead.
    AllProjects(crate::commands::projects_v2::RefreshAllProjectsEnvResult),
    /// Any other binding: only the target project's env derives from it.
    SingleProject(crate::commands::projects_v2::RefreshProjectEnvResult),
}

/// F2 (v0.2.72): shared post-write projection for KG-binding mutations
/// (set + delete). Root + primary/shared → refresh ALL projects; anything
/// else → single-project refresh. Soft-fail throughout: failures are
/// logged and folded into the returned reports; the binding write that
/// preceded this call is never rolled back.
///
/// `role` gates on both "primary" (the canonical shared-KG source) and
/// "shared" (the root's mirrored row — kept in lockstep by
/// `set_project_kg_binding_with_root_sync`, but a direct write/delete to
/// it still changes what peers may resolve, so treat it symmetrically).
pub fn reproject_after_kg_binding_change(
    db: &Db,
    project_id: &str,
    project_slug: &str,
    role: &str,
) -> KgBindingReprojection {
    let is_root =
        project_slug == crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG;
    if is_root && matches!(role, "primary" | "shared") {
        let report =
            crate::commands::projects_v2::refresh_all_projects_env_with_db(db);
        for w in &report.global_warnings {
            eprintln!(
                "[vct] warning: orchestrator-root KG rebind refresh-all: {}",
                w
            );
        }
        for (name, err) in &report.failed {
            eprintln!(
                "[vct] warning: orchestrator-root KG rebind env refresh \
                 failed for {}: {}",
                name, err
            );
        }
        KgBindingReprojection::AllProjects(report)
    } else {
        KgBindingReprojection::SingleProject(
            crate::commands::projects_v2::reproject_env_soft(db, project_id),
        )
    }
}

/// Free-function core of `set_project_kg_binding`'s DB write: the
/// orchestrator-root-aware binding upsert + audit + F5 env
/// re-projection. Testable without a Tauri runtime; the `#[command]`
/// wrapper handles the async catalog validation + Weaviate
/// collection-ensure around it.
///
/// F5 (v0.2.72): `config_projection.py` resolves KG_COLLECTION /
/// SHARED_KG_COLLECTION / DEVELOPMENT_COLLECTION (and the derived
/// DIAGRAMS_COLLECTION) from `project_kg_bindings` rows — all
/// `MCP_RELEVANT_ENV_KEYS` members. Re-projecting after the write is
/// what lets the settings watcher's diff-guard fire the guarded MCP
/// reload for a rebind. Soft-fail: projection warnings ride in the
/// returned result; the binding write is never rolled back.
///
/// F2 (v0.2.72): the projection scope is decided by
/// `reproject_after_kg_binding_change` — orchestrator-root primary/shared
/// rebinds refresh EVERY project (peers derive SHARED_KG_COLLECTION from
/// the root binding); everything else stays single-project.
pub fn set_project_kg_binding_row_with_db(
    db: &Db,
    project_id: &str,
    req: &SetKgBindingReq,
) -> Result<ProjectKgBinding, String> {
    // v0.2.46 Decision A — orchestrator-root primary/shared atomic auto-sync.
    // Resolve the project's slug so the DB layer can detect the
    // orchestrator-root case (slug == "orchestrator-root"). For peer
    // projects the slug isn't "orchestrator-root", so only the
    // requested row is written. Soft-fail to empty-string on a missing
    // project (the underlying write will fail with a clearer FK error).
    let project_slug = db
        .get_project(project_id)
        .ok()
        .flatten()
        .map(|p| p.slug)
        .unwrap_or_default();
    let row = db.set_project_kg_binding_with_root_sync(
        project_id,
        &project_slug,
        &req.role,
        &req.collection_name,
        req.embedding_model.as_deref(),
        req.embedding_dim,
        req.kg_dir_path.as_deref(),
        req.weaviate_url.as_deref(),
        &req.config,
    )?;
    db.audit(
        "project_kg_binding_set",
        Some(project_id),
        None,
        &serde_json::json!({ "role": req.role, "collection": req.collection_name }),
    )?;
    let _ = reproject_after_kg_binding_change(db, project_id, &project_slug, &req.role);
    Ok(row)
}

/// Free-function core of `delete_project_kg_binding` — DB delete + audit
/// + F5 env re-projection (an unbind changes the projected
/// KG_COLLECTION/SHARED_KG_COLLECTION resolution back to the derived
/// defaults, so the MCP must be reloaded the same way a rebind is).
///
/// F2 (v0.2.72): same scope rule as the set path — deleting the
/// orchestrator-root's primary/shared binding flips every peer project's
/// derived SHARED_KG_COLLECTION back to the last-resort default, so the
/// refresh covers ALL projects in that case.
pub fn delete_project_kg_binding_with_db(
    db: &Db,
    project_id: &str,
    role: &str,
) -> Result<KgBindingReprojection, String> {
    db.delete_project_kg_binding(project_id, role)?;
    db.audit(
        "project_kg_binding_delete",
        Some(project_id),
        None,
        &serde_json::json!({ "role": role }),
    )?;
    // Resolve the slug for root detection. Soft-fail to empty (→ treated
    // as non-root, single-project refresh) on a missing project row.
    let project_slug = db
        .get_project(project_id)
        .ok()
        .flatten()
        .map(|p| p.slug)
        .unwrap_or_default();
    Ok(reproject_after_kg_binding_change(db, project_id, &project_slug, role))
}

/// Remove a project's KG binding for a given role. Used by the launcher
/// GUI when the user wants to unbind a project from a KG collection
/// (e.g. revoke "shared" so the project no longer mounts the shared KG,
/// or fully unbind "primary" so the project temporarily has no KG of
/// its own). Does NOT delete the underlying Weaviate collection — that
/// stays around so other projects bound to the same collection still
/// see their data, and so the user can re-bind without losing nodes.
///
/// Idempotent: removing a non-existent (project_id, role) pair is a
/// no-op (DELETE … WHERE returns 0 affected rows, never errors).
#[command]
pub async fn delete_project_kg_binding(
    project_id: String,
    role: String,
    app: tauri::AppHandle,
) -> Result<(), String> {
    // F3 (v0.2.72): the unbind's post-projection can be machine-global
    // (F2: root primary/shared unbind → refresh ALL projects) — run the
    // sync core on the blocking pool.
    crate::commands::blocking::run_with_db_on_blocking_pool(
        app,
        "delete_project_kg_binding",
        move |db| delete_project_kg_binding_with_db(db, &project_id, &role).map(|_| ()),
    )
    .await?
}

/// POSTs a canonical KG-collection schema to Weaviate `/v1/schema` if
/// the collection does not yet exist. Schema mirrors the layout used
/// across multiple orchestrator instances:
///
///   - `vectorConfig` with named vectors `qwen3_embed` (primary,
///     1024d) + `ollama_embed` (legacy, 1024d) + `openai_embed`
///     (optional, 1536d). All vectorizer = "none" — embeddings are
///     pushed by the kg-sync pipeline, not generated by Weaviate.
///   - Properties: title, content, file_path, node_type, tags,
///     links, typed_links, created_at, updated_at, valid_from,
///     valid_until, status — same set sync_knowledge_graph.py uses
///     when it creates the class lazily.
///
/// Returns Ok(()) on creation OR if the class already exists. The
/// Weaviate REST contract for "already exists" is HTTP 422 with a
/// body mentioning `class already exists` — we match on that.
async fn ensure_kg_collection(weaviate_url: &str, collection_name: &str) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("http client: {}", e))?;

    // Probe first — if the schema endpoint already lists this class,
    // skip the POST. Cheaper than relying on the 422 path.
    let probe_url = format!("{}/v1/schema/{}", weaviate_url.trim_end_matches('/'), collection_name);
    if let Ok(resp) = client.get(&probe_url).send().await {
        if resp.status().is_success() {
            return Ok(()); // already exists
        }
    }

    // Schema body. The vectorConfig section follows Weaviate v1.30+
    // "named vectors with vectorizer none" syntax (per official docs at
    // https://docs.weaviate.io/weaviate/manage-collections/multi-vector).
    let schema = serde_json::json!({
        "class": collection_name,
        "description": format!("Knowledge graph for project (auto-created by VCT Launcher)"),
        "vectorConfig": {
            "qwen3_embed": {
                "vectorizer": { "none": {} },
                "vectorIndexType": "hnsw"
            },
            "ollama_embed": {
                "vectorizer": { "none": {} },
                "vectorIndexType": "hnsw"
            },
            "openai_embed": {
                "vectorizer": { "none": {} },
                "vectorIndexType": "hnsw"
            }
        },
        "properties": [
            { "name": "title",        "dataType": ["text"] },
            { "name": "content",      "dataType": ["text"] },
            { "name": "file_path",    "dataType": ["text"] },
            { "name": "node_type",    "dataType": ["text"] },
            { "name": "tags",         "dataType": ["text[]"] },
            { "name": "links",        "dataType": ["text[]"] },
            { "name": "typed_links",  "dataType": ["text[]"] },
            { "name": "created_at",   "dataType": ["date"] },
            { "name": "updated_at",   "dataType": ["date"] },
            { "name": "valid_from",   "dataType": ["date"] },
            { "name": "valid_until",  "dataType": ["date"] },
            { "name": "status",       "dataType": ["text"] }
        ]
    });

    let url = format!("{}/v1/schema", weaviate_url.trim_end_matches('/'));
    let resp = client
        .post(&url)
        .json(&schema)
        .send()
        .await
        .map_err(|e| format!("POST {}: {}", url, e))?;
    let status = resp.status();
    if status.is_success() {
        return Ok(());
    }
    let body = resp.text().await.unwrap_or_default();
    // Idempotence: 422 with "class already exists" → treat as success.
    if status.as_u16() == 422 && body.to_lowercase().contains("already exists") {
        return Ok(());
    }
    Err(format!("schema create failed: {} — {}", status, body))
}

#[derive(Debug, Deserialize)]
pub struct SetCodegraphBindingReq {
    pub collection_prefix: String,
    pub embedding_model: Option<String>,
    pub embedding_dim: Option<i64>,
    pub last_analyzed_commit: Option<String>,
    pub last_analyzed_at: Option<i64>,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub config: JsonValue,
}
fn default_true() -> bool {
    true
}

// v0.2.72 R2 (F5 residual) — set/delete_project_codegraph_binding now DO
// re-project env. The pre-R2 audit note here documented a deliberate
// no-op: `config_projection.py` derived CODE_GRAPH_PROJECT from the
// sanitized project NAME only, so a refresh after a prefix change would
// have rewritten `.claude/settings.json` byte-identically (inert —
// watcher hash-match, no reload). R2 changed the projection to derive
// CODE_GRAPH_PROJECT binding-prefix-first (hub-consistent; see
// `vco_lib/config_projection.py::_fetch_codegraph_binding_prefix`), so a
// prefix rebind NOW moves the projected value → the watcher's hashed
// CODE_GRAPH_PROJECT key changes (it is in
// `settings_json_watcher::MCP_RELEVANT_ENV_KEYS`) → the guarded MCP
// reload fires. Scope is single-project: unlike the shared-KG binding,
// no peer project derives anything from another project's codegraph
// binding row.

/// Free-function core of `set_project_codegraph_binding`'s DB write:
/// binding upsert + audit + R2 env re-projection. Testable without a
/// Tauri runtime; the `#[command]` wrapper handles the async catalog
/// validation around it. Soft-fail: projection warnings are logged
/// inside `reproject_env_soft`; the binding write is never rolled back.
pub fn set_project_codegraph_binding_row_with_db(
    db: &Db,
    project_id: &str,
    req: &SetCodegraphBindingReq,
) -> Result<ProjectCodegraphBinding, String> {
    let row = db.set_project_codegraph_binding(
        project_id,
        &req.collection_prefix,
        req.embedding_model.as_deref(),
        req.embedding_dim,
        req.last_analyzed_commit.as_deref(),
        req.last_analyzed_at,
        req.enabled,
        &req.config,
    )?;
    db.audit(
        "project_codegraph_binding_set",
        Some(project_id),
        None,
        &serde_json::json!({ "prefix": req.collection_prefix }),
    )?;
    let _ = crate::commands::projects_v2::reproject_env_soft(db, project_id);
    Ok(row)
}

#[command]
pub async fn set_project_codegraph_binding(
    project_id: String,
    req: SetCodegraphBindingReq,
    app: tauri::AppHandle,
    db: State<'_, Db>,
) -> Result<ProjectCodegraphBinding, String> {
    // v0.2.18 Commit 8: same catalog-validation pattern as
    // `set_project_kg_binding` above, but kind=Code. Rejects unknown
    // names so the GUI dropdown's "available" set is also the only set
    // that can be persisted.
    if let Some(model_id) = req.embedding_model.as_deref().filter(|s| !s.trim().is_empty()) {
        let result = crate::commands::embedding_catalog::validate_model_against_catalog(
            model_id.trim().to_string(),
            crate::commands::embedding_catalog::ModelKind::Code,
            db.clone(),
        )
        .await?;
        if let crate::commands::embedding_catalog::ValidationResult::Invalid { reason } = result {
            return Err(format!(
                "embedding_model '{}' not valid for codegraph binding: {}",
                model_id, reason
            ));
        }
    }

    // F3 (v0.2.72): the R2 post-projection shells out to Python — run
    // the sync core on the blocking pool. The write's own Result rides
    // through the join.
    let project_id_for_task = project_id.clone();
    crate::commands::blocking::run_with_db_on_blocking_pool(
        app,
        "set_project_codegraph_binding",
        move |db| set_project_codegraph_binding_row_with_db(db, &project_id_for_task, &req),
    )
    .await?
}

/// Free-function core of `delete_project_codegraph_binding` — DB delete
/// + audit + R2 env re-projection (an unbind flips the projected
/// CODE_GRAPH_PROJECT back to the name-derived prefix, so the MCP must
/// be reloaded the same way a rebind is). Returns the refresh result so
/// tests can observe that the projection ran.
pub fn delete_project_codegraph_binding_with_db(
    db: &Db,
    project_id: &str,
) -> Result<crate::commands::projects_v2::RefreshProjectEnvResult, String> {
    db.delete_project_codegraph_binding(project_id)?;
    db.audit(
        "project_codegraph_binding_delete",
        Some(project_id),
        None,
        &serde_json::json!({}),
    )?;
    Ok(crate::commands::projects_v2::reproject_env_soft(db, project_id))
}

/// Remove a project's codegraph binding. Used by the launcher GUI when
/// the user wants to unbind a project from its codegraph index (e.g.
/// stop maintaining a code graph for a finished sub-project, or rebind
/// to a different collection prefix). Does NOT delete the underlying
/// Weaviate collection — that stays around so the user can re-bind
/// without losing parsed entities.
///
/// Codegraph binding is single-keyed on project_id (no role concept,
/// unlike KG which has primary/shared/archive), so this command takes
/// only a project_id.
///
/// Idempotent: removing a non-existent project_id is a no-op.
///
/// F3 (v0.2.72): the R2 post-projection shells out to Python — run the
/// sync core on the blocking pool.
#[command]
pub async fn delete_project_codegraph_binding(
    project_id: String,
    app: tauri::AppHandle,
) -> Result<(), String> {
    crate::commands::blocking::run_with_db_on_blocking_pool(
        app,
        "delete_project_codegraph_binding",
        move |db| delete_project_codegraph_binding_with_db(db, &project_id).map(|_| ()),
    )
    .await?
}

// ─── Tests ──────────────────────────────────────────────────────────────
//
// These tests cover the 0.2.x backlog #5 per-project MCP toggle. They
// exercise the DB-only path so they run in any environment (no keychain,
// no Tauri runtime). The two commands are pure wrappers over the
// `project_permissions` table; tests target the resolution logic in
// `list_project_mcp_permissions` and the upsert/delete semantics in
// `set_project_mcp_permission`.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;
    use crate::db::Db;

    fn make_db() -> Db {
        Db::open_in_memory().unwrap()
    }

    fn seed_project(db: &Db, id: &str, name: &str) {
        // folder_path must be unique per project (UNIQUE constraint).
        db.insert_project(id, name, &format!("/tmp/mcp-perm-test/{}", id), ProjectHost::Base, id)
            .unwrap();
    }

    // ─── F5 (v0.2.72): KG-binding writes re-project env ────────────────

    /// `set_project_kg_binding_row_with_db` upserts the binding AND
    /// re-projects the project's env (KG_COLLECTION & friends derive from
    /// `project_kg_bindings` in the canonical projection). The binding row
    /// must land regardless of projection outcome (soft-fail contract).
    #[test]
    fn set_kg_binding_row_persists_and_reprojects() {
        let db = make_db();
        seed_project(&db, "p-f5-bind", "F5Bind");

        let req = SetKgBindingReq {
            role: "primary".to_string(),
            collection_name: "F5Bind_KnowledgeGraph".to_string(),
            embedding_model: None,
            embedding_dim: None,
            kg_dir_path: None,
            weaviate_url: None,
            config: serde_json::json!({}),
        };
        let row = set_project_kg_binding_row_with_db(&db, "p-f5-bind", &req)
            .expect("binding write must succeed");
        assert_eq!(row.collection_name, "F5Bind_KnowledgeGraph");

        // The authoritative row is in the DB (projection soft-fail can
        // never roll it back).
        let bindings = db.list_project_kg_bindings("p-f5-bind").unwrap();
        assert!(bindings
            .iter()
            .any(|b| b.role == "primary" && b.collection_name == "F5Bind_KnowledgeGraph"));
    }

    /// `delete_project_kg_binding_with_db` removes the row AND re-projects.
    /// Proof of the refresh: the returned result carries the access list
    /// only the refresh path (populate) computes — seeded here so the
    /// value is deterministic.
    #[test]
    fn delete_kg_binding_removes_row_and_reprojects() {
        let db = make_db();
        seed_project(&db, "p-f5-unbind", "F5Unbind");
        let req = SetKgBindingReq {
            role: "primary".to_string(),
            collection_name: "F5Unbind_KnowledgeGraph".to_string(),
            embedding_model: None,
            embedding_dim: None,
            kg_dir_path: None,
            weaviate_url: None,
            config: serde_json::json!({}),
        };
        set_project_kg_binding_row_with_db(&db, "p-f5-unbind", &req).unwrap();
        db.kg_set_access("p-f5-unbind", "PeerProj_KnowledgeGraph", "read")
            .unwrap();

        let result = delete_project_kg_binding_with_db(&db, "p-f5-unbind", "primary")
            .expect("unbind must succeed");

        assert!(
            db.list_project_kg_bindings("p-f5-unbind")
                .unwrap()
                .iter()
                .all(|b| b.role != "primary"),
            "primary binding row must be gone",
        );
        // F2: a NON-root project's unbind stays single-project scoped.
        let KgBindingReprojection::SingleProject(result) = result else {
            panic!("non-root unbind must take the single-project path");
        };
        assert_eq!(
            result.kg_access_list,
            vec!["PeerProj".to_string()],
            "the delete must re-project env after the write",
        );
    }

    // ─── R2 (v0.2.72 F5 residual): codegraph-binding writes re-project ──

    fn codegraph_req(prefix: &str) -> SetCodegraphBindingReq {
        SetCodegraphBindingReq {
            collection_prefix: prefix.to_string(),
            embedding_model: None,
            embedding_dim: None,
            last_analyzed_commit: None,
            last_analyzed_at: None,
            enabled: true,
            config: serde_json::json!({}),
        }
    }

    /// `set_project_codegraph_binding_row_with_db` upserts the binding
    /// AND re-projects the project's env (CODE_GRAPH_PROJECT derives
    /// binding-prefix-first in the canonical projection since R2). The
    /// binding row must land regardless of projection outcome (soft-fail
    /// contract — mirror of `set_kg_binding_row_persists_and_reprojects`).
    #[test]
    fn set_codegraph_binding_row_persists_and_reprojects() {
        let db = make_db();
        seed_project(&db, "p-r2-bind", "R2Bind");

        let row =
            set_project_codegraph_binding_row_with_db(&db, "p-r2-bind", &codegraph_req("Custom_Prefix"))
                .expect("binding write must succeed");
        assert_eq!(row.collection_prefix, "Custom_Prefix");

        // The authoritative row is in the DB (projection soft-fail can
        // never roll it back).
        let stored = db
            .get_project_codegraph_binding("p-r2-bind")
            .unwrap()
            .expect("binding row present");
        assert_eq!(stored.collection_prefix, "Custom_Prefix");
    }

    /// `delete_project_codegraph_binding_with_db` removes the row AND
    /// re-projects. Proof of the refresh: the returned result carries the
    /// access list only the refresh path (populate) computes — seeded
    /// here so the value is deterministic.
    #[test]
    fn delete_codegraph_binding_removes_row_and_reprojects() {
        let db = make_db();
        seed_project(&db, "p-r2-unbind", "R2Unbind");
        set_project_codegraph_binding_row_with_db(&db, "p-r2-unbind", &codegraph_req("R2Unbind"))
            .unwrap();
        db.kg_set_access("p-r2-unbind", "PeerProj_KnowledgeGraph", "read")
            .unwrap();

        let result = delete_project_codegraph_binding_with_db(&db, "p-r2-unbind")
            .expect("unbind must succeed");

        assert!(
            db.get_project_codegraph_binding("p-r2-unbind")
                .unwrap()
                .is_none(),
            "codegraph binding row must be gone",
        );
        assert_eq!(
            result.kg_access_list,
            vec!["PeerProj".to_string()],
            "the delete must re-project env after the write",
        );
    }

    // ─── F2 (v0.2.72): orchestrator-root rebind refreshes ALL projects ──

    /// Seed an orchestrator-root project (slug is the detection key the
    /// projection + root-sync layers use) plus a peer. Folders don't
    /// exist on disk, so a refresh-all lands every project in `skipped` —
    /// the observable proof that the machine-global path iterated them.
    fn seed_root_and_peer(db: &Db) {
        db.insert_project(
            "p-root",
            "RootProj",
            "/nonexistent/f2-root",
            ProjectHost::OrchestratorRoot,
            crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();
        db.insert_project(
            "p-peer",
            "PeerProj",
            "/nonexistent/f2-peer",
            ProjectHost::Base,
            "peerproj",
        )
        .unwrap();
    }

    /// Root + primary → the machine-global refresh runs and iterates
    /// EVERY registered project (root AND peer), because every project's
    /// SHARED_KG_COLLECTION derives from the root's primary binding.
    #[test]
    fn root_primary_rebind_refreshes_all_projects() {
        let db = make_db();
        seed_root_and_peer(&db);

        let result = reproject_after_kg_binding_change(
            &db,
            "p-root",
            crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG,
            "primary",
        );
        let KgBindingReprojection::AllProjects(report) = result else {
            panic!("root+primary rebind must take the all-projects path");
        };
        assert!(
            report.skipped.contains(&"RootProj".to_string())
                && report.skipped.contains(&"PeerProj".to_string()),
            "refresh-all must have iterated EVERY registered project \
             (missing-folder projects land in `skipped`); got {:?}",
            report,
        );
    }

    /// Root + shared behaves like primary (the mirrored row is part of the
    /// shared-KG derivation surface); root + any OTHER role stays
    /// single-project.
    #[test]
    fn root_shared_refreshes_all_but_other_roles_stay_single() {
        let db = make_db();
        seed_root_and_peer(&db);

        assert!(matches!(
            reproject_after_kg_binding_change(
                &db,
                "p-root",
                crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG,
                "shared",
            ),
            KgBindingReprojection::AllProjects(_)
        ));
        assert!(matches!(
            reproject_after_kg_binding_change(
                &db,
                "p-root",
                crate::commands::orchestrator_root::ORCHESTRATOR_ROOT_SLUG,
                "archive",
            ),
            KgBindingReprojection::SingleProject(_)
        ));
    }

    /// Non-root project + primary → single-project path (peers do NOT
    /// derive anything from a non-root binding).
    #[test]
    fn non_root_primary_rebind_stays_single_project() {
        let db = make_db();
        seed_root_and_peer(&db);

        assert!(matches!(
            reproject_after_kg_binding_change(&db, "p-peer", "peerproj", "primary"),
            KgBindingReprojection::SingleProject(_)
        ));
    }

    /// End-to-end through the delete helper: deleting the ROOT's primary
    /// binding takes the all-projects path (slug resolved from the DB row).
    #[test]
    fn delete_root_primary_binding_refreshes_all_projects() {
        let db = make_db();
        seed_root_and_peer(&db);
        let req = SetKgBindingReq {
            role: "primary".to_string(),
            collection_name: "Root_KnowledgeGraph".to_string(),
            embedding_model: None,
            embedding_dim: None,
            kg_dir_path: None,
            weaviate_url: None,
            config: serde_json::json!({}),
        };
        set_project_kg_binding_row_with_db(&db, "p-root", &req).unwrap();

        let result = delete_project_kg_binding_with_db(&db, "p-root", "primary")
            .expect("root unbind must succeed");
        let KgBindingReprojection::AllProjects(report) = result else {
            panic!("root primary unbind must take the all-projects path");
        };
        assert!(
            report.skipped.contains(&"PeerProj".to_string()),
            "peer projects must be covered by the root-unbind refresh; got {:?}",
            report,
        );
    }

    /// Replicate `list_project_mcp_permissions`'s DB-only logic (the
    /// #[command] requires Tauri State, but the inner work is just
    /// `list_project_permissions` + an in-memory join).
    fn list_mcp_perms(db: &Db, project_id: &str, server_ids: &[&str]) -> Vec<ProjectMcpPermission> {
        let rows = db.list_project_permissions(project_id).unwrap();
        server_ids
            .iter()
            .map(|sid| {
                let row = rows.iter().find(|r| {
                    r.kind == "mcp_server"
                        && r.subject == MCP_PERMISSION_SUBJECT
                        && r.value == *sid
                });
                let (enabled, explicit) = match row {
                    Some(r) => {
                        let enabled = r
                            .config
                            .get("enabled")
                            .and_then(|v| v.as_bool())
                            .unwrap_or(true);
                        (enabled, true)
                    }
                    None => (true, false),
                };
                ProjectMcpPermission {
                    server_id: sid.to_string(),
                    enabled,
                    explicit,
                }
            })
            .collect()
    }

    /// Default: no rows in `project_permissions` → every requested server
    /// reports `enabled: true, explicit: false`. Pre-0.2.x projects MUST
    /// pass through this branch (zero rows) and see every MCP server as
    /// enabled, identical to pre-fix behaviour.
    #[test]
    fn list_mcp_permissions_defaults_to_enabled_when_no_rows() {
        let db = make_db();
        seed_project(&db, "pdef", "Default");

        let perms = list_mcp_perms(&db, "pdef", &["weaviate-kg", "ollama", "playwright"]);
        assert_eq!(perms.len(), 3);
        for p in &perms {
            assert!(p.enabled, "{} must default-enabled", p.server_id);
            assert!(!p.explicit, "{} must be marked non-explicit", p.server_id);
        }
    }

    /// Disable one server → that row reports `enabled: false, explicit: true`;
    /// the other servers still default to enabled.
    #[test]
    fn disable_one_server_leaves_others_default_enabled() {
        let db = make_db();
        seed_project(&db, "ponly", "OnlyOne");

        // Disable playwright.
        db.add_project_permission(
            "ponly",
            MCP_PERMISSION_SUBJECT,
            "mcp_server",
            "playwright",
            &serde_json::json!({ "enabled": false }),
        )
        .unwrap();

        let perms = list_mcp_perms(&db, "ponly", &["weaviate-kg", "ollama", "playwright"]);
        let by_id: std::collections::HashMap<_, _> =
            perms.iter().map(|p| (p.server_id.as_str(), p)).collect();
        assert!(by_id["weaviate-kg"].enabled);
        assert!(!by_id["weaviate-kg"].explicit);
        assert!(by_id["ollama"].enabled);
        assert!(!by_id["ollama"].explicit);
        assert!(!by_id["playwright"].enabled);
        assert!(by_id["playwright"].explicit);
    }

    /// Re-enabling DELETES the row so the (project, server) pair falls
    /// back to the default-enabled state. Verified via direct
    /// `list_project_permissions` check — the row must be gone, not just
    /// flipped to `config.enabled=true`.
    #[test]
    fn enabling_back_deletes_the_row() {
        let db = make_db();
        seed_project(&db, "pflip", "Flip");

        // Disable, then re-enable.
        db.add_project_permission(
            "pflip",
            MCP_PERMISSION_SUBJECT,
            "mcp_server",
            "playwright",
            &serde_json::json!({ "enabled": false }),
        )
        .unwrap();
        // Row exists post-disable.
        let rows = db.list_project_permissions("pflip").unwrap();
        assert!(rows.iter().any(|r| r.kind == "mcp_server" && r.value == "playwright"));

        // Now re-enable using the same DELETE-by-key path the command takes.
        db.delete_project_permission_by_key(
            "pflip",
            MCP_PERMISSION_SUBJECT,
            "mcp_server",
            "playwright",
        )
        .unwrap();

        // Row gone — explicit=false again.
        let rows = db.list_project_permissions("pflip").unwrap();
        assert!(!rows.iter().any(|r| r.kind == "mcp_server" && r.value == "playwright"));
        let perms = list_mcp_perms(&db, "pflip", &["playwright"]);
        assert!(perms[0].enabled);
        assert!(!perms[0].explicit, "row must be gone, not just flipped");
    }

    /// Per-project isolation: disabling a server for project A does NOT
    /// affect project B's default state. Pin: the permission rows are
    /// scoped on `project_id`, not on the server_id alone.
    #[test]
    fn disabling_for_one_project_does_not_affect_another() {
        let db = make_db();
        seed_project(&db, "pA_iso", "A");
        seed_project(&db, "pB_iso", "B");

        db.add_project_permission(
            "pA_iso",
            MCP_PERMISSION_SUBJECT,
            "mcp_server",
            "playwright",
            &serde_json::json!({ "enabled": false }),
        )
        .unwrap();

        // pA sees disabled+explicit, pB still sees default-enabled.
        let a = list_mcp_perms(&db, "pA_iso", &["playwright"]);
        let b = list_mcp_perms(&db, "pB_iso", &["playwright"]);
        assert!(!a[0].enabled);
        assert!(a[0].explicit);
        assert!(b[0].enabled);
        assert!(!b[0].explicit);
    }

    /// Re-disabling an already-disabled row is idempotent — UPSERT
    /// semantic. Same row in `project_permissions`, no error, audit
    /// fires once per call.
    #[test]
    fn disable_is_idempotent_upsert() {
        let db = make_db();
        seed_project(&db, "pidem", "Idem");

        for _ in 0..3 {
            db.add_project_permission(
                "pidem",
                MCP_PERMISSION_SUBJECT,
                "mcp_server",
                "playwright",
                &serde_json::json!({ "enabled": false }),
            )
            .unwrap();
        }

        // Still one row.
        let rows = db.list_project_permissions("pidem").unwrap();
        let mcp_rows: Vec<_> = rows
            .iter()
            .filter(|r| r.kind == "mcp_server" && r.value == "playwright")
            .collect();
        assert_eq!(mcp_rows.len(), 1, "UPSERT must collapse to a single row");
    }

    // ─── v0.2.22 item #17: rescan_project_from_filesystem ──────────────

    /// Replicate the rescan command's DB-only logic (the #[command]
    /// requires Tauri State). Asserts the populate-from-disk produces
    /// the same result as if the command itself were invoked.
    ///
    /// Pattern mirrors `list_mcp_perms` above (same constraint: Tauri
    /// State is not constructible in unit tests; replicate the logic
    /// directly against the Db.)
    fn rescan_logic(db: &Db, project_id: &str) -> Result<RescanReport, String> {
        let project = db
            .get_project(project_id)
            .map_err(|e| format!("get_project: {}", e))?
            .ok_or_else(|| format!("project '{}' not found", project_id))?;
        let folder = std::path::Path::new(&project.folder_path);
        if !folder.is_dir() {
            return Err(format!(
                "project folder no longer exists on disk: {}",
                project.folder_path
            ));
        }
        let report = crate::commands::project_state_populate::
            populate_project_state_from_filesystem(project_id, &project.name, folder, db);
        Ok(RescanReport {
            agents_inserted: report.agents_inserted,
            skills_inserted: report.skills_inserted,
            hooks_inserted: report.hooks_inserted,
            mcp_servers_inserted: report.mcp_servers_inserted,
            kg_access_rows_inserted: report.kg_access_rows_inserted,
            warnings: report.warnings,
        })
    }

    /// Successful rescan against a real folder populates the per-project
    /// tables and returns the inserted-row counts in the report.
    #[test]
    fn rescan_populates_agents_and_skills_from_disk() {
        use std::fs;
        use uuid::Uuid;

        let db = make_db();

        // Stage a folder with .claude/agents + .claude/skills.
        let tmp = std::env::temp_dir().join(format!(
            "rescan-test-{}",
            Uuid::new_v4().simple()
        ));
        let agents_dir = tmp.join(".claude/agents");
        let skills_dir = tmp.join(".claude/skills");
        fs::create_dir_all(&agents_dir).unwrap();
        fs::create_dir_all(&skills_dir).unwrap();
        fs::write(
            agents_dir.join("planner.md"),
            "---\nname: planner\nmodel: sonnet\n---\n",
        )
        .unwrap();
        fs::write(
            agents_dir.join("README.md"),
            "# docs (must be skipped)",
        )
        .unwrap();
        // Skill: directory with SKILL.md.
        let sk = skills_dir.join("architect");
        fs::create_dir_all(&sk).unwrap();
        fs::write(
            sk.join("SKILL.md"),
            "---\nname: architect\nmodel: opus\n---\n",
        )
        .unwrap();

        // Seed a project pointing at our folder.
        let pid = Uuid::new_v4().to_string();
        db.insert_project(
            &pid,
            "Acme",
            tmp.to_string_lossy().as_ref(),
            ProjectHost::Base,
            &db.generate_unique_slug("Acme").unwrap(),
        )
        .unwrap();

        // Pre-condition: empty tables.
        assert_eq!(db.list_project_agents(&pid).unwrap().len(), 0);
        assert_eq!(db.list_project_skills(&pid).unwrap().len(), 0);

        // Rescan.
        let report = rescan_logic(&db, &pid).expect("rescan must succeed");

        assert_eq!(report.agents_inserted, 1, "planner counted, README skipped");
        assert_eq!(report.skills_inserted, 1);

        // Verify the rows are actually in the DB.
        let agents = db.list_project_agents(&pid).unwrap();
        assert_eq!(agents.len(), 1);
        assert_eq!(agents[0].agent_name, "planner");
        assert_eq!(agents[0].model.as_deref(), Some("sonnet"));

        let skills = db.list_project_skills(&pid).unwrap();
        assert_eq!(skills.len(), 1);
        assert_eq!(skills[0].skill_name, "architect");
        assert_eq!(skills[0].model.as_deref(), Some("opus"));

        fs::remove_dir_all(&tmp).ok();
    }

    /// Unknown project_id surfaces a clear error rather than panicking.
    #[test]
    fn rescan_returns_error_for_unknown_project_id() {
        let db = make_db();
        let err = rescan_logic(&db, "ghost-id").unwrap_err();
        assert!(
            err.contains("not found"),
            "expected 'not found' error, got: {}",
            err
        );
    }

    /// Project row exists but folder_path points at a missing directory
    /// (user moved/deleted the project on disk). The command surfaces
    /// a clear, actionable error.
    #[test]
    fn rescan_returns_error_when_folder_missing() {
        use uuid::Uuid;
        let db = make_db();
        let pid = Uuid::new_v4().to_string();
        let missing = std::env::temp_dir().join(format!(
            "rescan-MISSING-{}",
            Uuid::new_v4().simple()
        ));
        db.insert_project(
            &pid,
            "GhostProject",
            missing.to_string_lossy().as_ref(),
            ProjectHost::Base,
            &db.generate_unique_slug("GhostProject").unwrap(),
        )
        .unwrap();
        let err = rescan_logic(&db, &pid).unwrap_err();
        assert!(
            err.contains("no longer exists on disk"),
            "expected folder-missing error, got: {}",
            err
        );
    }

    // ──────────────────────────────────────────────────────────────────
    // NEW-9 (v0.2.53) — FS-disable contract unit tests.
    //
    // Reference:
    // `.claude/context/audits/project-bundle-install-audit-2026-06-10.md`
    // §6.5 / B2.
    // ──────────────────────────────────────────────────────────────────

    use uuid::Uuid;

    fn scratch_project_dir() -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("fs-disable-test-{}", Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Disabling an agent must move
    /// `.claude/agents/<name>.md` → `.claude/agents.disabled/<name>.md`.
    #[test]
    fn fs_disable_agent_moves_md_to_disabled_dir() {
        let folder = scratch_project_dir();
        let agents = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents).unwrap();
        let enabled_path = agents.join("coder.md");
        std::fs::write(&enabled_path, "name: coder\nmodel: sonnet\n").unwrap();

        apply_fs_disable_agent(&folder, "coder", false).unwrap();

        let disabled_path = folder.join(".claude/agents.disabled/coder.md");
        assert!(!enabled_path.exists(), "enabled-side file must be gone");
        assert!(
            disabled_path.exists(),
            "disabled-side file must exist after disable"
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Enabling an agent must move
    /// `.claude/agents.disabled/<name>.md` → `.claude/agents/<name>.md`.
    #[test]
    fn fs_disable_agent_enable_moves_md_back() {
        let folder = scratch_project_dir();
        let disabled_dir = folder.join(".claude/agents.disabled");
        std::fs::create_dir_all(&disabled_dir).unwrap();
        let disabled_path = disabled_dir.join("coder.md");
        std::fs::write(&disabled_path, "name: coder\nmodel: sonnet\n").unwrap();

        apply_fs_disable_agent(&folder, "coder", true).unwrap();

        let enabled_path = folder.join(".claude/agents/coder.md");
        assert!(enabled_path.exists());
        assert!(!disabled_path.exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    /// When the source file doesn't exist (user removed it manually),
    /// the helper must be a no-op — NEVER raise. The DB flag flip still
    /// proceeds and the absence is benign.
    #[test]
    fn fs_disable_agent_missing_source_is_noop() {
        let folder = scratch_project_dir();
        // Don't create any agent file.
        apply_fs_disable_agent(&folder, "ghost", false).unwrap();
        apply_fs_disable_agent(&folder, "ghost", true).unwrap();
        // No-op — neither side should have been created.
        assert!(!folder.join(".claude/agents/ghost.md").exists());
        assert!(!folder
            .join(".claude/agents.disabled/ghost.md")
            .exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    /// When the destination ALREADY exists (corrupt both-locations
    /// state), the helper must REFUSE the move — it would clobber the
    /// user's destination-side file silently otherwise.
    #[test]
    fn fs_disable_agent_refuses_to_clobber_existing_destination() {
        let folder = scratch_project_dir();
        let agents = folder.join(".claude/agents");
        let disabled = folder.join(".claude/agents.disabled");
        std::fs::create_dir_all(&agents).unwrap();
        std::fs::create_dir_all(&disabled).unwrap();
        std::fs::write(agents.join("coder.md"), "enabled\n").unwrap();
        std::fs::write(disabled.join("coder.md"), "disabled\n").unwrap();

        let err = apply_fs_disable_agent(&folder, "coder", false).unwrap_err();
        assert!(
            err.contains("destination already exists"),
            "expected clobber-refusal error, got: {}",
            err
        );
        // Both files survive the refusal.
        assert!(agents.join("coder.md").exists());
        assert!(disabled.join("coder.md").exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    /// Disabling a skill (whole directory) must move the entire
    /// `.claude/skills/<name>/` directory to `.claude/skills.disabled/<name>/`.
    #[test]
    fn fs_disable_skill_moves_directory() {
        let folder = scratch_project_dir();
        let skill_dir = folder.join(".claude/skills/tdd");
        std::fs::create_dir_all(&skill_dir).unwrap();
        std::fs::write(skill_dir.join("SKILL.md"), "name: tdd\n").unwrap();

        apply_fs_disable_skill(&folder, "tdd", false).unwrap();

        let disabled_dir = folder.join(".claude/skills.disabled/tdd");
        assert!(!skill_dir.exists());
        assert!(disabled_dir.exists());
        assert!(disabled_dir.join("SKILL.md").exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    // ──────────────────────────────────────────────────────────────────
    // v0.2.53 Phase 3 Audit B optional splice A3 — FS+DB chain integration
    //
    // Existing unit tests cover `apply_fs_disable_*` in isolation.
    // These tests exercise the composition: a "disable" call MUST move
    // the file on disk AND flip the DB row. The DB and FS signals MUST
    // stay in sync, because `install_project_bundle` consults BOTH
    // independently (FS for skip-rebundle, DB for permission gating).
    //
    // The #[command] async wrapper takes `State<'_, Db>` which is awkward
    // to construct in unit tests; we inline-replicate the wrapper's body
    // (FS move first, then DB flip) so the integration semantics — order
    // of operations + error propagation — are pinned.
    // ──────────────────────────────────────────────────────────────────

    fn seed_project_with_folder(db: &Db, id: &str, folder: &Path) {
        db.insert_project(
            id, id, folder.to_str().unwrap(), ProjectHost::Base, id,
        )
        .unwrap();
    }

    #[test]
    fn fs_disable_agent_and_db_flag_stay_in_sync_through_a_full_toggle_cycle() {
        let folder = scratch_project_dir();
        let agents = folder.join(".claude/agents");
        std::fs::create_dir_all(&agents).unwrap();
        std::fs::write(
            agents.join("coder.md"),
            "name: coder\nmodel: sonnet\n",
        )
        .unwrap();

        let db = make_db();
        let project_id = "proj-fs-db-sync-1";
        seed_project_with_folder(&db, project_id, &folder);
        db.register_project_agent(
            project_id, "coder", "bundled", None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();

        // ---- Disable: FS move first, then DB flip ----
        let project = db.get_project(project_id).unwrap().unwrap();
        apply_fs_disable_agent(Path::new(&project.folder_path), "coder", false)
            .unwrap();
        db.set_project_agent_enabled(project_id, "coder", false).unwrap();

        // Verify both signals report disabled.
        assert!(
            !folder.join(".claude/agents/coder.md").exists(),
            "enabled-side file must be gone after disable"
        );
        assert!(
            folder.join(".claude/agents.disabled/coder.md").exists(),
            "disabled-side file must exist after disable"
        );
        let rows = db.list_project_agents(project_id).unwrap();
        let coder_row = rows
            .iter()
            .find(|r| r.agent_name == "coder")
            .expect("agent row must exist");
        assert!(!coder_row.enabled, "DB flag must be false after disable");

        // ---- Re-enable: FS move back, then DB flip back ----
        apply_fs_disable_agent(Path::new(&project.folder_path), "coder", true)
            .unwrap();
        db.set_project_agent_enabled(project_id, "coder", true).unwrap();

        assert!(folder.join(".claude/agents/coder.md").exists());
        assert!(!folder.join(".claude/agents.disabled/coder.md").exists());
        let rows = db.list_project_agents(project_id).unwrap();
        let coder_row = rows
            .iter()
            .find(|r| r.agent_name == "coder")
            .expect("agent row must exist");
        assert!(coder_row.enabled, "DB flag must be true after re-enable");

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn fs_disable_skill_and_db_flag_stay_in_sync_through_a_full_toggle_cycle() {
        let folder = scratch_project_dir();
        let skill_dir = folder.join(".claude/skills/tdd");
        std::fs::create_dir_all(&skill_dir).unwrap();
        std::fs::write(skill_dir.join("SKILL.md"), "name: tdd\n").unwrap();

        let db = make_db();
        let project_id = "proj-fs-db-sync-2";
        seed_project_with_folder(&db, project_id, &folder);
        db.register_project_skill(
            project_id, "tdd", "bundled", None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();

        let project = db.get_project(project_id).unwrap().unwrap();
        apply_fs_disable_skill(Path::new(&project.folder_path), "tdd", false)
            .unwrap();
        db.set_project_skill_enabled(project_id, "tdd", false).unwrap();

        assert!(!skill_dir.exists());
        assert!(folder.join(".claude/skills.disabled/tdd").exists());
        let rows = db.list_project_skills(project_id).unwrap();
        let tdd_row = rows
            .iter()
            .find(|r| r.skill_name == "tdd")
            .expect("skill row must exist");
        assert!(!tdd_row.enabled);

        apply_fs_disable_skill(Path::new(&project.folder_path), "tdd", true)
            .unwrap();
        db.set_project_skill_enabled(project_id, "tdd", true).unwrap();

        assert!(skill_dir.exists());
        assert!(!folder.join(".claude/skills.disabled/tdd").exists());
        let rows = db.list_project_skills(project_id).unwrap();
        let tdd_row = rows
            .iter()
            .find(|r| r.skill_name == "tdd")
            .expect("skill row must exist");
        assert!(tdd_row.enabled);

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn fs_disable_agent_refuses_db_flip_when_fs_move_would_clobber() {
        // Corrupt both-locations state: file exists in BOTH enabled and
        // disabled dirs (created by a user manually copying). The
        // apply_fs_disable_*  helper refuses to clobber. The Tauri command
        // returns the error and SKIPS the DB flip, so the DB doesn't end
        // up out-of-sync with disk.
        let folder = scratch_project_dir();
        let agents = folder.join(".claude/agents");
        let disabled = folder.join(".claude/agents.disabled");
        std::fs::create_dir_all(&agents).unwrap();
        std::fs::create_dir_all(&disabled).unwrap();
        std::fs::write(agents.join("coder.md"), "enabled side\n").unwrap();
        std::fs::write(disabled.join("coder.md"), "disabled side stale\n")
            .unwrap();

        let db = make_db();
        let project_id = "proj-fs-corrupt";
        seed_project_with_folder(&db, project_id, &folder);
        db.register_project_agent(
            project_id, "coder", "bundled", None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();
        // Sanity: DB has the row as enabled by default.
        let rows = db.list_project_agents(project_id).unwrap();
        assert!(rows.iter().find(|r| r.agent_name == "coder").unwrap().enabled);

        // Try to disable: FS move should refuse → Tauri command would
        // bubble up the error and NOT flip the DB.
        let project = db.get_project(project_id).unwrap().unwrap();
        let fs_result = apply_fs_disable_agent(
            Path::new(&project.folder_path),
            "coder",
            false,
        );
        assert!(
            fs_result.is_err(),
            "apply_fs_disable_agent must refuse to clobber existing destination"
        );
        // The Tauri command body returns at the `?` on this line — never
        // reaches the DB flip. The DB row stays enabled.
        let rows = db.list_project_agents(project_id).unwrap();
        let coder_row = rows
            .iter()
            .find(|r| r.agent_name == "coder")
            .expect("agent row must exist");
        assert!(
            coder_row.enabled,
            "DB must remain enabled when FS move refused — out-of-sync state \
             is the bug we're guarding against"
        );

        // Both files still exist (FS state is unchanged by the refusal).
        assert!(agents.join("coder.md").exists());
        assert!(disabled.join("coder.md").exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    #[test]
    fn fs_disable_agent_succeeds_when_source_missing_db_still_flips() {
        // Soft-fail no-op shape: user manually removed the agent .md file
        // from the enabled dir. apply_fs_disable_agent is a no-op (doesn't
        // raise). The DB flip still proceeds — DB remains authoritative
        // for the "agent doesn't actually exist on disk" case.
        let folder = scratch_project_dir();
        // Create the dir but no agent file.
        std::fs::create_dir_all(folder.join(".claude/agents")).unwrap();

        let db = make_db();
        let project_id = "proj-fs-ghost";
        seed_project_with_folder(&db, project_id, &folder);
        db.register_project_agent(
            project_id, "ghost", "bundled", None, None, None,
            &serde_json::json!({}),
        )
        .unwrap();

        let project = db.get_project(project_id).unwrap().unwrap();
        // FS no-op:
        apply_fs_disable_agent(Path::new(&project.folder_path), "ghost", false)
            .unwrap();
        // DB still flips:
        db.set_project_agent_enabled(project_id, "ghost", false).unwrap();

        let rows = db.list_project_agents(project_id).unwrap();
        let ghost_row = rows
            .iter()
            .find(|r| r.agent_name == "ghost")
            .expect("ghost agent row must exist");
        assert!(
            !ghost_row.enabled,
            "DB flag must flip even when FS file is absent (soft-fail no-op)"
        );

        std::fs::remove_dir_all(&folder).ok();
    }
}
