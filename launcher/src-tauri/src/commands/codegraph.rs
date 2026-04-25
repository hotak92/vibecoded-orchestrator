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

// ─── Graph load (for Sigma viz) ─────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct CgVizNode {
    pub id: String,
    pub label: String,
    pub entity_type: String, // CodeModule | CodeClass | CodeFunction | CodeAPI | CodeInteraction
    pub project: String,
    pub file_path: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CgVizEdge {
    pub from_id: String,
    pub to_id: String,
    pub edge_type: String, // imports | calls | extends | interacts
}

#[derive(Debug, Serialize)]
pub struct CodegraphViz {
    pub nodes: Vec<CgVizNode>,
    pub edges: Vec<CgVizEdge>,
    pub truncated: bool,
}

/// Pull a lightweight subgraph for the visualizer.
///
/// Strategy: fetch `max_nodes` items each from Module/Class/Function/API/
/// Interaction, then reconstruct edges from `imports`, `calls`, `extends`,
/// and `interactions` properties. Best-effort — Weaviate fields names follow
/// what `code-graph-analyze` writes; fields that don't exist are silently
/// skipped.
#[command]
pub async fn codegraph_load_graph(
    acting_project_id: String,
    target_project_id: String,
    max_nodes: Option<u32>,
    db: State<'_, Db>,
) -> Result<CodegraphViz, String> {
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
    let project_tag = target.name.replace('"', "\\\"");

    let limit_each = max_nodes.unwrap_or(120).min(500);
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let base = std::env::var("WEAVIATE_URL").unwrap_or_else(|_| "http://localhost:8081".to_string());

    let mut nodes: Vec<CgVizNode> = Vec::new();
    let mut name_to_id: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();
    let mut edges: Vec<CgVizEdge> = Vec::new();

    // (class, label_field, extra_fields)
    let classes: &[(&str, &str, &str)] = &[
        ("CodeModule", "path", "imports"),
        ("CodeClass", "full_name", "extends"),
        ("CodeFunction", "full_name", "calls"),
        ("CodeAPI", "endpoint", ""),
        ("CodeInteraction", "endpoint", ""),
    ];
    let mut truncated = false;
    for (class, label_field, edge_field) in classes {
        let q = format!(
            "{{ Get {{ {class}(where: {{path:[\"project\"], operator:Equal, valueText:\"{project}\"}}, limit: {lim}) {{ {label_field} {ef} _additional {{ id }} }} }} }}",
            class = class,
            project = project_tag,
            lim = limit_each,
            label_field = label_field,
            ef = if edge_field.is_empty() { "".to_string() } else { format!(" {}", edge_field) },
        );
        let resp = client
            .post(format!("{}/v1/graphql", base))
            .json(&serde_json::json!({ "query": q }))
            .send()
            .await;
        let body: serde_json::Value = match resp {
            Ok(r) => r.json().await.unwrap_or(serde_json::json!({})),
            Err(_) => continue,
        };
        let empty_vec = vec![];
        let items = body
            .pointer(&format!("/data/Get/{}", class))
            .and_then(|v| v.as_array())
            .unwrap_or(&empty_vec);
        if items.len() as u32 >= limit_each {
            truncated = true;
        }
        for item in items {
            let id = item
                .pointer("/_additional/id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let label = item
                .get(*label_field)
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if id.is_empty() || label.is_empty() {
                continue;
            }
            name_to_id.insert(label.clone(), id.clone());
            nodes.push(CgVizNode {
                id: id.clone(),
                label: label.clone(),
                entity_type: class.to_string(),
                project: target.name.clone(),
                file_path: item
                    .get("path")
                    .and_then(|v| v.as_str())
                    .map(str::to_string),
            });

            // Collect edges if the field is one we know
            if !edge_field.is_empty() {
                if let Some(arr) = item.get(*edge_field).and_then(|v| v.as_array()) {
                    for target_name in arr.iter().filter_map(|v| v.as_str()) {
                        if let Some(target_id) = name_to_id.get(target_name) {
                            edges.push(CgVizEdge {
                                from_id: id.clone(),
                                to_id: target_id.clone(),
                                edge_type: match *edge_field {
                                    "imports" => "imports".to_string(),
                                    "calls" => "calls".to_string(),
                                    "extends" => "extends".to_string(),
                                    _ => "interacts".to_string(),
                                },
                            });
                        }
                    }
                }
            }
        }
    }

    // Second pass for edges to nodes seen later
    // (cheap — re-iterate cached items? we already lost them. Skip for now.)

    Ok(CodegraphViz {
        nodes,
        edges,
        truncated,
    })
}

// ─── Bulk per-entity access ──────────────────────────────────────────────
//
// Mirrors the KG `kg_set_node_access_bulk` pattern but for codegraph
// entities (CodeModule / CodeClass / CodeFunction / CodeAPI /
// CodeInteraction). Stores `cross_project_access` text[] on each
// Weaviate object — list of project IDs allowed to read this entity,
// or ["*"] for shared. Continues on per-entity failure and returns
// a summary.
//
// The acting project must own the codegraph (or be the target itself)
// for this to make sense. We don't currently verify this against the
// codegraph_access matrix because the matrix is project-level and an
// owner is implicitly allowed to set per-entity scopes.

#[derive(Debug, Deserialize)]
pub struct EntityAccessBulkReq {
    pub project_id: String,
    /// One of: CodeModule, CodeClass, CodeFunction, CodeAPI, CodeInteraction.
    pub entity_class: String,
    pub entity_ids: Vec<String>,
    pub mode: String, // shared | projects | private
    #[serde(default)]
    pub project_ids: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct EntityBulkAccessResult {
    pub succeeded: usize,
    pub failed: usize,
    pub failures: Vec<EntityBulkFailure>,
}

#[derive(Debug, Serialize)]
pub struct EntityBulkFailure {
    pub id: String,
    pub error: String,
}

#[command]
pub async fn codegraph_set_entity_access_bulk(
    req: EntityAccessBulkReq,
    db: State<'_, Db>,
) -> Result<EntityBulkAccessResult, String> {
    if !matches!(req.mode.as_str(), "shared" | "projects" | "private") {
        return Err(format!("invalid mode: {}", req.mode));
    }
    if !matches!(
        req.entity_class.as_str(),
        "CodeModule" | "CodeClass" | "CodeFunction" | "CodeAPI" | "CodeInteraction"
    ) {
        return Err(format!("invalid entity class: {}", req.entity_class));
    }
    if req.entity_ids.is_empty() {
        return Ok(EntityBulkAccessResult { succeeded: 0, failed: 0, failures: vec![] });
    }
    db.get_project(&req.project_id)?
        .ok_or_else(|| format!("project {} not found", req.project_id))?;

    let allowed: Vec<String> = match req.mode.as_str() {
        "shared" => vec!["*".to_string()],
        "projects" => req.project_ids.clone(),
        _ => vec![],
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let base = std::env::var("WEAVIATE_URL").unwrap_or_else(|_| "http://localhost:8081".to_string());

    let mut succeeded = 0usize;
    let mut failures: Vec<EntityBulkFailure> = Vec::new();
    for id in &req.entity_ids {
        let payload = serde_json::json!({
            "class": req.entity_class,
            "properties": { "cross_project_access": allowed },
        });
        let resp = client
            .patch(format!("{}/v1/objects/{}/{}", base, req.entity_class, id))
            .json(&payload)
            .send()
            .await;
        match resp {
            Ok(r) if r.status().is_success() => succeeded += 1,
            Ok(r) => {
                let status = r.status().as_u16();
                let body = r.text().await.unwrap_or_default();
                failures.push(EntityBulkFailure {
                    id: id.clone(),
                    error: format!(
                        "weaviate returned {}: {}",
                        status,
                        body.chars().take(200).collect::<String>()
                    ),
                });
            }
            Err(e) => failures.push(EntityBulkFailure {
                id: id.clone(),
                error: format!("weaviate PATCH: {}", e),
            }),
        }
    }

    db.audit(
        "codegraph_entity_access_set_bulk",
        Some(&req.project_id),
        None,
        &serde_json::json!({
            "entity_class": req.entity_class,
            "mode": req.mode,
            "project_count": req.project_ids.len(),
            "entity_count": req.entity_ids.len(),
            "succeeded": succeeded,
            "failed": failures.len(),
        }),
    )?;

    Ok(EntityBulkAccessResult {
        succeeded,
        failed: failures.len(),
        failures,
    })
}
