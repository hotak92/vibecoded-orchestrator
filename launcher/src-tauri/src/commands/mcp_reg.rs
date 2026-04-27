//! Tauri commands for registering/deregistering module MCPs in the user's
//! Claude Code configuration files. Safe concurrent editor lives in
//! `crate::mcp_registration`.
//!
//! Removed from invoke_handler 2026-04-27 — the actual write path goes
//! through `crate::mcp_registration::register_mcp` / `deregister_mcp`
//! invoked server-side by `commands::dashboard` and `commands::installer`.
//! These Tauri command wrappers had zero FE/Hub consumers. Kept in source
//! under #[allow(dead_code)] in case a future direct-from-FE path is needed.
#![allow(dead_code)]

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::Db;
use crate::mcp_registration::{
    deregister_mcp, project_mcp_json, register_mcp, user_claude_json,
};

#[derive(Debug, Deserialize)]
pub struct RegisterMcpReq {
    pub project_id: String,
    pub module_id: String,
    /// "user" writes to ~/.claude.json; "project" writes to <folder>/.mcp.json
    pub scope: String,
    /// The `mcpServers.<name>` key. Comes from the manifest's mcp_registration.mcp_name.
    pub mcp_name: String,
    /// Full config block for the MCP — should already have command/args/env filled in.
    /// Shape: {"type":"stdio","command":"...","args":[...],"env":{...}}
    pub entry: serde_json::Value,
}

#[command]
pub async fn register_module_mcp(
    req: RegisterMcpReq,
    db: State<'_, Db>,
) -> Result<RegistrationTarget, String> {
    let target = resolve_target(&req.project_id, &req.scope, &db)?;
    register_mcp(&target.path, &req.mcp_name, &req.entry)?;
    db.audit(
        "mcp_register",
        Some(&req.project_id),
        Some(&req.module_id),
        &serde_json::json!({
            "scope": req.scope,
            "mcp_name": req.mcp_name,
            "target": target.path.display().to_string(),
        }),
    )?;
    Ok(target)
}

#[derive(Debug, Deserialize)]
pub struct DeregisterMcpReq {
    pub project_id: String,
    pub module_id: String,
    pub scope: String,
    pub mcp_name: String,
}

#[command]
pub async fn deregister_module_mcp(
    req: DeregisterMcpReq,
    db: State<'_, Db>,
) -> Result<RegistrationTarget, String> {
    let target = resolve_target(&req.project_id, &req.scope, &db)?;
    deregister_mcp(&target.path, &req.mcp_name)?;
    db.audit(
        "mcp_deregister",
        Some(&req.project_id),
        Some(&req.module_id),
        &serde_json::json!({
            "scope": req.scope,
            "mcp_name": req.mcp_name,
            "target": target.path.display().to_string(),
        }),
    )?;
    Ok(target)
}

#[derive(Debug, Serialize)]
pub struct RegistrationTarget {
    pub scope: String,
    pub path: PathBuf,
    pub exists: bool,
}

fn resolve_target(
    project_id: &str,
    scope: &str,
    db: &Db,
) -> Result<RegistrationTarget, String> {
    match scope {
        "user" => {
            let p = user_claude_json();
            let exists = p.exists();
            Ok(RegistrationTarget {
                scope: "user".to_string(),
                path: p,
                exists,
            })
        }
        "project" => {
            let project = db
                .get_project(project_id)?
                .ok_or_else(|| format!("project {} not found", project_id))?;
            let p = project_mcp_json(std::path::Path::new(&project.folder_path));
            let exists = p.exists();
            Ok(RegistrationTarget {
                scope: "project".to_string(),
                path: p,
                exists,
            })
        }
        other => Err(format!("scope must be 'user' or 'project', got '{}'", other)),
    }
}
