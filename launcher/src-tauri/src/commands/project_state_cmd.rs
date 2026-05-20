//! Tauri commands exposing per-project orchestrator state to the React UI.
//!
//! Backed by `crate::db::project_state`. Mutations call `db.audit(...)`
//! so the audit log records who changed what (without recording values).

use serde::Deserialize;
use serde_json::Value as JsonValue;
use tauri::{command, State};

use crate::db::project_mcp_servers::ProjectMcpServer;
use crate::db::project_state::{
    ProjectAgent, ProjectCodegraphBinding, ProjectHook, ProjectKgBinding, ProjectPermission,
    ProjectSecretRef, ProjectSkill, ProjectStateSnapshot,
};
use crate::db::Db;

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

    let row = db.set_project_kg_binding(
        &project_id,
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
        Some(&project_id),
        None,
        &serde_json::json!({ "role": req.role, "collection": req.collection_name }),
    )?;

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
    if matches!(req.role.as_str(), "primary") {
        let weaviate_url = req
            .weaviate_url
            .as_deref()
            .unwrap_or("http://localhost:8081");
        if let Err(e) = ensure_kg_collection(weaviate_url, &req.collection_name).await {
            eprintln!(
                "[vct] warning: ensure_kg_collection({}) on {}: {}",
                req.collection_name, weaviate_url, e
            );
        }
    }

    Ok(row)
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
    db: State<'_, Db>,
) -> Result<(), String> {
    db.delete_project_kg_binding(&project_id, &role)?;
    db.audit(
        "project_kg_binding_delete",
        Some(&project_id),
        None,
        &serde_json::json!({ "role": role }),
    )?;
    Ok(())
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

#[command]
pub async fn set_project_codegraph_binding(
    project_id: String,
    req: SetCodegraphBindingReq,
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

    let row = db.set_project_codegraph_binding(
        &project_id,
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
        Some(&project_id),
        None,
        &serde_json::json!({ "prefix": req.collection_prefix }),
    )?;
    Ok(row)
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
#[command]
pub async fn delete_project_codegraph_binding(
    project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.delete_project_codegraph_binding(&project_id)?;
    db.audit(
        "project_codegraph_binding_delete",
        Some(&project_id),
        None,
        &serde_json::json!({}),
    )?;
    Ok(())
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
}
