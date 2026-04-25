//! Codegraph access matrix + query proxy.
//!
//! Each project's code graph is normally private to that project. The matrix
//! lets a user explicitly grant another project read access — useful when
//! an agent in project A needs to understand the structure of project B
//! (cross-project intelligence without sharing the whole KG).
//!
//! Grant semantics (must match the spec in LAUNCHER_BACKEND_API.md §4.6):
//!   - Grantor = project that OWNS the codegraph (the callee)
//!   - Grantee = project that WANTS to read it (the caller)
//!   - Self-access (grantor == grantee) is always "read"
//!   - No row OR access_level == "none" => deny
//!   - Only the user can grant; an agent calling the API cannot elevate
//!     itself because agents don't have a UI-level confirmation gesture.

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::Db;

#[derive(Debug, Serialize)]
pub struct ProjectRef {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Serialize)]
pub struct CodegraphAccessMatrix {
    pub project_id: String,
    pub can_read_from: Vec<ProjectRef>, // others whose codegraph I can read
    pub readable_by: Vec<ProjectRef>,   // others who can read MY codegraph
}

#[command]
pub async fn codegraph_list_access(
    project_id: String,
    db: State<'_, Db>,
) -> Result<CodegraphAccessMatrix, String> {
    let grants_to = db.codegraph_list_grants_to(&project_id)?; // rows where I am grantee
    let grants_from = db.codegraph_list_grants_from(&project_id)?; // rows where I am grantor

    let resolve = |ids: Vec<(String, String)>| -> Result<Vec<ProjectRef>, String> {
        let mut out = Vec::new();
        for (other_id, level) in ids {
            if level != "read" {
                continue;
            }
            let Some(p) = db.get_project(&other_id)? else {
                continue;
            };
            out.push(ProjectRef {
                id: p.id,
                name: p.name,
            });
        }
        Ok(out)
    };

    Ok(CodegraphAccessMatrix {
        project_id: project_id.clone(),
        can_read_from: resolve(grants_to)?,
        readable_by: resolve(grants_from)?,
    })
}

#[derive(Debug, Deserialize)]
pub struct CodegraphGrantReq {
    pub grantor_project_id: String,
    pub grantee_project_id: String,
    pub access_level: String, // "read" | "none"
}

#[command]
pub async fn codegraph_grant_access(
    req: CodegraphGrantReq,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Both projects must exist.
    db.get_project(&req.grantor_project_id)?
        .ok_or_else(|| format!("grantor project {} not found", req.grantor_project_id))?;
    db.get_project(&req.grantee_project_id)?
        .ok_or_else(|| format!("grantee project {} not found", req.grantee_project_id))?;

    if req.grantor_project_id == req.grantee_project_id {
        return Err("a project always has read access to its own codegraph — grant is a no-op".into());
    }

    db.codegraph_grant(
        &req.grantor_project_id,
        &req.grantee_project_id,
        &req.access_level,
    )?;
    db.audit(
        "codegraph_grant",
        Some(&req.grantor_project_id),
        None,
        &serde_json::json!({
            "grantee": req.grantee_project_id,
            "access_level": req.access_level,
        }),
    )?;
    Ok(())
}

// ─── Query proxy ─────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct CodegraphQueryReq {
    pub acting_project_id: String,
    pub target_project_id: String,
    pub query_type: String, // "search" | "dependencies" | "callers" | "methods" | "interactions"
    pub params: serde_json::Value,
}

/// Proxy a codegraph query through the launcher with access enforcement.
///
/// The heavy lifting (actual Weaviate GraphQL / SQLite queries) lives in the
/// vct-codegraph MCP that's running for the target project. This command
/// simply checks the access matrix and forwards. For V1 we accept that the
/// MCP is exposed on stdio and the launcher does NOT directly embed the
/// query logic — the caller is expected to use the codegraph MCP for the
/// actual queries. This command exists primarily for UI-side stats (node
/// counts, access badges) where a full MCP call is overkill.
///
/// Returns the acting project's access level so the UI can badge
/// "read" / "denied" on the result.
#[command]
pub async fn codegraph_check_access(
    acting_project_id: String,
    target_project_id: String,
    db: State<'_, Db>,
) -> Result<CodegraphCheckResult, String> {
    let level = db.codegraph_check(&target_project_id, &acting_project_id)?;
    let allowed = matches!(level.as_deref(), Some("read"));
    Ok(CodegraphCheckResult {
        allowed,
        access_level: level.unwrap_or_else(|| "none".to_string()),
    })
}

#[derive(Debug, Serialize)]
pub struct CodegraphCheckResult {
    pub allowed: bool,
    pub access_level: String,
}

/// Return a lightweight summary of a project's codegraph: counts per entity
/// type. Proxies to Weaviate. Enforces `codegraph_check` first.
#[command]
pub async fn codegraph_summary(
    acting_project_id: String,
    target_project_id: String,
    db: State<'_, Db>,
) -> Result<CodegraphSummary, String> {
    if acting_project_id != target_project_id {
        let level = db.codegraph_check(&target_project_id, &acting_project_id)?;
        if !matches!(level.as_deref(), Some("read")) {
            return Err(format!(
                "project {} has no read access to codegraph of project {}",
                acting_project_id, target_project_id
            ));
        }
    }

    let target = db
        .get_project(&target_project_id)?
        .ok_or_else(|| format!("target project {} not found", target_project_id))?;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let base = std::env::var("WEAVIATE_URL").unwrap_or_else(|_| "http://localhost:8081".to_string());

    let project_tag = &target.name; // codegraph entities are tagged by project name
    let mut counts = std::collections::HashMap::new();
    for class in ["CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"] {
        let q = format!(
            "{{ Aggregate {{ {class}(where: {{path:[\"project\"], operator:Equal, valueText:\"{project}\"}}) {{ meta {{ count }} }} }} }}",
            class = class,
            project = project_tag.replace('"', "\\\""),
        );
        let resp = client
            .post(format!("{}/v1/graphql", base))
            .json(&serde_json::json!({ "query": q }))
            .send()
            .await;
        let count = match resp {
            Ok(r) => {
                let body: serde_json::Value = r.json().await.unwrap_or(serde_json::json!({}));
                body.pointer(&format!("/data/Aggregate/{}/0/meta/count", class))
                    .and_then(|n| n.as_u64())
                    .unwrap_or(0)
            }
            Err(_) => 0,
        };
        counts.insert(class.to_string(), count as u32);
    }

    Ok(CodegraphSummary {
        project_id: target_project_id,
        project_name: target.name,
        module_count: *counts.get("CodeModule").unwrap_or(&0),
        class_count: *counts.get("CodeClass").unwrap_or(&0),
        function_count: *counts.get("CodeFunction").unwrap_or(&0),
        api_count: *counts.get("CodeAPI").unwrap_or(&0),
        interaction_count: *counts.get("CodeInteraction").unwrap_or(&0),
    })
}

#[derive(Debug, Serialize)]
pub struct CodegraphSummary {
    pub project_id: String,
    pub project_name: String,
    pub module_count: u32,
    pub class_count: u32,
    pub function_count: u32,
    pub api_count: u32,
    pub interaction_count: u32,
}
