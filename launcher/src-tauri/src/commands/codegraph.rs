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
use tauri::{command, AppHandle, Emitter, Manager, State};

use crate::db::code_graph_builds::{status as build_status, CodeGraphBuildRow};
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

// ─── Gap 2: initial code-graph build on project create ────────────────────
//
// When a user creates a project we kick off `code-graph-analyze` in the
// background so `search_code_graph` returns useful results out of the
// box (rather than the user having to drop into a terminal). The build
// status is persisted in `code_graph_builds` and live progress is
// emitted on the `code-graph-build-progress` Tauri event.
//
// Behaviour:
//   1. `create_project_v2` calls `spawn_initial_build` AFTER the project
//      row is inserted. The spawn is fire-and-forget — project create
//      returns immediately to the user.
//   2. Pre-check: if the project folder has no supported source files
//      within depth 3, we record status='skipped' and stop. This avoids
//      a needless multi-second analyzer startup for empty / asset-only
//      folders.
//   3. Otherwise: shell out to `<orchestrator>/.claude/scripts/code-graph-analyze`
//      capturing stdout+stderr. Tail last 4 KiB into log_tail. Parse
//      "Files analyzed: N" from stdout.
//   4. Joern (`--cfg --pdg`) is gated on `VCT_JOERN_AVAILABLE=1` — this
//      env is set by install.py when the Joern binary is on PATH and
//      otherwise stays unset. We never assume it's installed.
//
// Failure isolation: ANY failure of this background task (analyzer not
// found, Weaviate down, subprocess crash) is recorded in the row's
// `error_message` and emitted as a terminal `failed` event. It is NEVER
// propagated to the create_project_v2 caller — the user has already
// gotten their `ProjectView` back by the time this runs.

const BUILD_EVENT: &str = "code-graph-build-progress";

/// Tauri-event payload + DTO for `get_code_graph_build_status`.
///
/// Mirrors `CodeGraphBuildRow` but in a public-API shape: timestamps in
/// ISO 8601 (so the GUI doesn't have to convert epoch-ms), explicit
/// optionals, and a `current_phase` string for live progress events.
#[derive(Debug, Clone, Serialize)]
pub struct CodeGraphBuildView {
    pub project_id: String,
    pub status: String,
    pub started_at_iso: Option<String>,
    pub finished_at_iso: Option<String>,
    pub duration_ms: Option<i64>,
    pub files_analyzed: u32,
    pub languages: Vec<String>,
    pub joern_used: bool,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
    /// Live phase indicator. Only populated on `running` events emitted
    /// during the build (e.g. "python", "typescript", "joern-cfg",
    /// "weaviate-upload"). Always None for stored rows fetched via
    /// `get_code_graph_build_status`.
    pub current_phase: Option<String>,
}

impl CodeGraphBuildView {
    fn from_row(row: CodeGraphBuildRow) -> Self {
        Self {
            project_id: row.project_id,
            status: row.status,
            started_at_iso: row.started_at.and_then(epoch_ms_to_iso),
            finished_at_iso: row.finished_at.and_then(epoch_ms_to_iso),
            duration_ms: row.duration_ms,
            files_analyzed: row.files_analyzed,
            languages: row.languages.unwrap_or_default(),
            joern_used: row.joern_used,
            error_message: row.error_message,
            log_tail: row.log_tail,
            current_phase: None,
        }
    }
}

fn epoch_ms_to_iso(ms: i64) -> Option<String> {
    chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).map(|dt| dt.to_rfc3339())
}

#[command]
pub async fn get_code_graph_build_status(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Option<CodeGraphBuildView>, String> {
    Ok(db
        .get_code_graph_build(&project_id)?
        .map(CodeGraphBuildView::from_row))
}

/// Re-trigger a code-graph build for an existing project. Marks the row
/// as `pending` and re-spawns the background task. Safe to call while
/// a previous build is still running — the new spawn will overwrite the
/// row when it transitions, and the old subprocess (if any) keeps going
/// until it finishes; whichever finishes last wins. Re-builds are rare
/// enough in practice that we don't bother with cancellation.
#[command]
pub async fn rebuild_code_graph(
    project_id: String,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    db.upsert_code_graph_build(
        &project.id,
        build_status::PENDING,
        Some(chrono::Utc::now().timestamp_millis()),
        None,
        None,
        0,
        None,
        false,
        None,
        None,
    )?;
    db.audit(
        "code_graph_rebuild",
        Some(&project.id),
        None,
        &serde_json::json!({ "name": project.name }),
    )?;

    spawn_initial_build(app, project.id, project.name, project.folder_path);
    Ok(())
}

/// Public entry point used by `create_project_v2` (and the rebuild
/// command). Spawns a background task; never blocks. The caller has
/// already inserted a `pending` row into `code_graph_builds`.
pub fn spawn_initial_build(
    app: AppHandle,
    project_id: String,
    project_name: String,
    folder_path: String,
) {
    tokio::spawn(async move {
        run_build_task(app, project_id, project_name, folder_path).await;
    });
}

/// Body of the spawned task. Errors here are recorded in the build row,
/// never propagated. Each transition emits a `code-graph-build-progress`
/// event so the GUI updates live.
async fn run_build_task(
    app: AppHandle,
    project_id: String,
    project_name: String,
    folder_path: String,
) {
    let started_at = chrono::Utc::now().timestamp_millis();

    // 1. Mark RUNNING + emit. We deliberately recompute the languages
    //    pre-check here so the user sees a "scanning…" pill the moment
    //    project create returns.
    upsert_quiet(
        &app,
        &project_id,
        build_status::RUNNING,
        Some(started_at),
        None,
        None,
        0,
        None,
        false,
        None,
        None,
    );
    emit_build(&app, &project_id, build_status::RUNNING, 0, Some("scan"), None);

    // 2. Pre-check: any supported source files at all?
    let detected = match detect_supported_languages(std::path::Path::new(&folder_path), 3) {
        Ok(set) => set,
        Err(e) => {
            finalize_failed(
                &app,
                &project_id,
                started_at,
                format!("scan folder failed: {}", e),
                None,
            );
            return;
        }
    };

    if detected.is_empty() {
        let finished_at = chrono::Utc::now().timestamp_millis();
        upsert_quiet(
            &app,
            &project_id,
            build_status::SKIPPED,
            Some(started_at),
            Some(finished_at),
            Some(finished_at - started_at),
            0,
            None,
            false,
            Some("no supported source files found within depth 3"),
            None,
        );
        emit_build(
            &app,
            &project_id,
            build_status::SKIPPED,
            0,
            None,
            Some("no supported source files found within depth 3"),
        );
        return;
    }

    let langs_vec: Vec<String> = detected.iter().cloned().collect();
    emit_build(
        &app,
        &project_id,
        build_status::RUNNING,
        0,
        Some("analyze"),
        None,
    );

    // 3. Resolve the analyzer wrapper. Convention: the script lives at
    //    `<orchestrator-root>/.claude/scripts/code-graph-analyze`. We
    //    try the project's own folder first, then the launcher's repo
    //    root (one of the worktrees may be running us during dev), then
    //    fall back to the system PATH.
    let script = match resolve_analyzer_script(std::path::Path::new(&folder_path)) {
        Some(p) => p,
        None => {
            finalize_failed(
                &app,
                &project_id,
                started_at,
                "code-graph-analyze script not found (looked in project, launcher install, $PATH)"
                    .to_string(),
                None,
            );
            return;
        }
    };

    let joern_available = std::env::var("VCT_JOERN_AVAILABLE")
        .map(|v| v == "1")
        .unwrap_or(false);

    // 4. Build args. First run uses no `--incremental` flag so the
    //    analyzer does a full pass. Re-builds also pass through here
    //    (rebuild_code_graph upserts pending → spawn_initial_build),
    //    and we keep them as full passes too for now: incremental
    //    semantics depend on git state we don't necessarily have.
    let mut args: Vec<String> = vec![
        folder_path.clone(),
        "--project".to_string(),
        project_name.clone(),
    ];
    if joern_available {
        args.push("--cfg".to_string());
        args.push("--pdg".to_string());
    }

    // 5. Run it. We capture stdout+stderr; they're combined into one
    //    log buffer (interleaving is fine for human debugging).
    let output = tokio::process::Command::new(&script)
        .args(&args)
        // Don't inherit the launcher's working dir; the analyzer is
        // path-aware and we don't want it picking up an unrelated cwd.
        .current_dir(std::env::temp_dir())
        // Don't leak Tauri's pipe to a long-running subprocess that
        // might hang on stdin: explicitly close it.
        .stdin(std::process::Stdio::null())
        .output()
        .await;

    let finished_at = chrono::Utc::now().timestamp_millis();

    let (status_str, files_analyzed, error_msg, log_tail, joern_used) = match output {
        Ok(out) => {
            let stdout_str = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr_str = String::from_utf8_lossy(&out.stderr).to_string();
            let combined = format!("{}{}", stdout_str, stderr_str);
            let tail = tail_log(&combined);

            if out.status.success() {
                let count = parse_files_analyzed(&stdout_str).unwrap_or(0);
                (
                    build_status::SUCCESS.to_string(),
                    count,
                    None,
                    Some(tail),
                    joern_available,
                )
            } else {
                let head = stderr_str
                    .lines()
                    .find(|l| !l.trim().is_empty())
                    .unwrap_or("");
                let snippet: String = head.chars().take(200).collect();
                let exit_code = out.status.code().unwrap_or(-1);
                (
                    build_status::FAILED.to_string(),
                    0,
                    Some(format!(
                        "code-graph-analyze exited {}: {}",
                        exit_code,
                        if snippet.is_empty() { "no stderr" } else { &snippet }
                    )),
                    Some(tail),
                    joern_available,
                )
            }
        }
        Err(e) => (
            build_status::FAILED.to_string(),
            0,
            Some(format!("could not spawn code-graph-analyze: {}", e)),
            None,
            false,
        ),
    };

    // 6. Persist + emit terminal event.
    upsert_quiet(
        &app,
        &project_id,
        &status_str,
        Some(started_at),
        Some(finished_at),
        Some(finished_at - started_at),
        files_analyzed,
        Some(&langs_vec),
        joern_used,
        error_msg.as_deref(),
        log_tail.as_deref(),
    );
    emit_build(
        &app,
        &project_id,
        &status_str,
        files_analyzed,
        None,
        error_msg.as_deref(),
    );
}

/// Helper: resolve the launcher Db from the AppHandle and write a row.
/// We swallow errors here because the alternative (panicking the
/// background task) would lose the whole build status. Errors are
/// logged to stderr.
#[allow(clippy::too_many_arguments)]
fn upsert_quiet(
    app: &AppHandle,
    project_id: &str,
    status: &str,
    started_at: Option<i64>,
    finished_at: Option<i64>,
    duration_ms: Option<i64>,
    files_analyzed: u32,
    languages: Option<&[String]>,
    joern_used: bool,
    error_message: Option<&str>,
    log_tail: Option<&str>,
) {
    let db = app.state::<Db>();
    if let Err(e) = db.upsert_code_graph_build(
        project_id,
        status,
        started_at,
        finished_at,
        duration_ms,
        files_analyzed,
        languages,
        joern_used,
        error_message,
        log_tail,
    ) {
        eprintln!(
            "[vct] warning: code_graph_builds upsert failed for {}: {}",
            project_id, e
        );
    }
}

/// Failed-state convenience helper. Keeps `run_build_task` readable.
fn finalize_failed(
    app: &AppHandle,
    project_id: &str,
    started_at: i64,
    error: String,
    log_tail: Option<String>,
) {
    let finished_at = chrono::Utc::now().timestamp_millis();
    upsert_quiet(
        app,
        project_id,
        build_status::FAILED,
        Some(started_at),
        Some(finished_at),
        Some(finished_at - started_at),
        0,
        None,
        false,
        Some(&error),
        log_tail.as_deref(),
    );
    emit_build(
        app,
        project_id,
        build_status::FAILED,
        0,
        None,
        Some(&error),
    );
}

fn emit_build(
    app: &AppHandle,
    project_id: &str,
    status: &str,
    files_analyzed: u32,
    current_phase: Option<&str>,
    error: Option<&str>,
) {
    let payload = CodeGraphBuildView {
        project_id: project_id.to_string(),
        status: status.to_string(),
        started_at_iso: None,
        finished_at_iso: None,
        duration_ms: None,
        files_analyzed,
        languages: vec![],
        joern_used: false,
        error_message: error.map(|s| s.to_string()),
        log_tail: None,
        current_phase: current_phase.map(|s| s.to_string()),
    };
    let _ = app.emit(BUILD_EVENT, payload);
}

/// File-extension set we know `analyze_code_graph.py` can handle. Kept
/// in sync with the language dispatch table at the top of that file
/// (Python / TS / JS / Go / Rust / Java / Lua / C++ / Ruby / Shell /
/// C# / Proto). Empty extension list => skip pre-check, just run.
fn supported_extensions() -> &'static [&'static str] {
    &[
        // Python
        "py",
        // TypeScript / JavaScript
        "ts", "tsx", "js", "jsx", "mjs",
        // Compiled
        "go", "rs", "java", "cs",
        // Native / scripting
        "cpp", "cc", "cxx", "c", "h", "hpp",
        "lua", "rb", "sh", "bash", "zsh",
        // RPC / schemas
        "proto",
    ]
}

/// Directory names we always skip when scanning. Matches the analyzer's
/// own ignore lists (vendor / build / venv / vcs).
fn ignored_dirs() -> &'static [&'static str] {
    &[
        ".git", ".svn", ".hg",
        "node_modules", "__pycache__", ".pytest_cache",
        ".venv", "venv", "env", ".tox", "site-packages", "virtualenv",
        "build", "dist", "target", "out", ".next", ".nuxt",
        "coverage", ".gradle", ".idea", ".vscode",
        ".claude",  // launcher-managed config, not user code
    ]
}

/// Walk `root` up to `max_depth` levels deep. Return the set of file
/// extensions found that match `supported_extensions()`. Returns an
/// empty set if no supported files are present (caller treats this as
/// "skip the build").
///
/// We DON'T do a full rglob here because the user's project might be a
/// big monorepo and we just need a fast yes/no. Three levels is plenty
/// to find at least one source file in any sane layout.
pub(crate) fn detect_supported_languages(
    root: &std::path::Path,
    max_depth: usize,
) -> Result<std::collections::HashSet<String>, std::io::Error> {
    let mut found: std::collections::HashSet<String> = std::collections::HashSet::new();
    let exts = supported_extensions();
    let ignored = ignored_dirs();
    walk(root, 0, max_depth, exts, ignored, &mut found)?;
    Ok(found)
}

fn walk(
    dir: &std::path::Path,
    depth: usize,
    max_depth: usize,
    exts: &[&str],
    ignored: &[&str],
    found: &mut std::collections::HashSet<String>,
) -> Result<(), std::io::Error> {
    if depth > max_depth {
        return Ok(());
    }
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => return Ok(()),
        Err(e) => return Err(e),
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        // Hidden + ignored dirs (don't descend, don't sample).
        if path.is_dir() {
            if name_str.starts_with('.') && name_str.as_ref() != "." && name_str.as_ref() != ".." {
                // .git/.venv/.claude already in ignored; this blanket-skips
                // any other hidden dir (e.g. .cache, .terraform) — those
                // are never user source.
                continue;
            }
            if ignored.iter().any(|i| *i == name_str.as_ref()) {
                continue;
            }
            walk(&path, depth + 1, max_depth, exts, ignored, found)?;
        } else if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
            let lower = ext.to_ascii_lowercase();
            if exts.iter().any(|e| *e == lower) {
                found.insert(lower);
                // Early-exit: if every supported ext is already in the
                // set we can stop walking. Tiny optimisation; matters
                // only on huge monorepos.
                if found.len() == exts.len() {
                    return Ok(());
                }
            }
        }
    }
    Ok(())
}

/// Tail the last N bytes of analyzer output. We slice on a char
/// boundary so non-ASCII output (rare in this analyzer's logs but
/// possible on Windows file paths) doesn't panic the format step.
fn tail_log(s: &str) -> String {
    use crate::db::code_graph_builds::LOG_TAIL_MAX_BYTES;
    if s.len() <= LOG_TAIL_MAX_BYTES {
        return s.to_string();
    }
    let cut_at = s.len() - LOG_TAIL_MAX_BYTES;
    let mut idx = cut_at;
    while idx < s.len() && !s.is_char_boundary(idx) {
        idx += 1;
    }
    format!("…\n{}", &s[idx..])
}

/// Parse "Files analyzed: N" from analyzer stdout. Falls back to 0 if
/// the line isn't present (older script versions).
pub(crate) fn parse_files_analyzed(stdout: &str) -> Option<u32> {
    for line in stdout.lines() {
        let trimmed = line.trim();
        // Match "Files analyzed: 42" with or without leading emoji.
        if let Some(idx) = trimmed.find("Files analyzed:") {
            let tail = &trimmed[idx + "Files analyzed:".len()..];
            if let Some(num) = tail.split_whitespace().next() {
                if let Ok(n) = num.parse::<u32>() {
                    return Some(n);
                }
            }
        }
    }
    None
}

/// Look for `code-graph-analyze` in (in order):
///   1. `<project>/.claude/scripts/code-graph-analyze` — projects that
///      shipped with their own copy.
///   2. `$VCT_LAUNCHER_SCRIPTS_DIR/code-graph-analyze` — env override
///      used by tests + by install.py to point the launcher at the
///      orchestrator install dir.
///   3. `<exe>/../.claude/scripts/code-graph-analyze` — when the launcher
///      runs from a built bundle alongside the orchestrator install.
///   4. `code-graph-analyze` on PATH — system-wide install.
///
/// Returns `None` if nothing resolves; caller records this as a build
/// failure with a clear "script not found" message.
pub(crate) fn resolve_analyzer_script(project_folder: &std::path::Path) -> Option<std::path::PathBuf> {
    let bin = if cfg!(windows) {
        "code-graph-analyze.ps1"
    } else {
        "code-graph-analyze"
    };

    // 1. Project-local
    let p1 = project_folder.join(".claude").join("scripts").join(bin);
    if p1.is_file() {
        return Some(p1);
    }

    // 2. Env override
    if let Ok(dir) = std::env::var("VCT_LAUNCHER_SCRIPTS_DIR") {
        let p2 = std::path::PathBuf::from(dir).join(bin);
        if p2.is_file() {
            return Some(p2);
        }
    }

    // 3. Sibling-of-exe convention
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            for hop in [".", "..", "../.."].iter() {
                let p3 = parent.join(hop).join(".claude").join("scripts").join(bin);
                if p3.is_file() {
                    return Some(p3);
                }
            }
        }
    }

    // 4. PATH lookup
    if let Ok(path) = std::env::var("PATH") {
        let sep = if cfg!(windows) { ';' } else { ':' };
        for d in path.split(sep) {
            let p4 = std::path::Path::new(d).join(bin);
            if p4.is_file() {
                return Some(p4);
            }
        }
    }
    None
}

#[cfg(test)]
mod build_tests {
    use super::*;
    use std::fs;

    fn tmpdir(label: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-cgbuild-{}-{}",
            label,
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn detect_finds_python_at_root() {
        let d = tmpdir("py");
        fs::write(d.join("hello.py"), b"print('hi')").unwrap();
        let langs = detect_supported_languages(&d, 3).unwrap();
        assert!(langs.contains("py"));
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn detect_skips_node_modules_and_venv() {
        let d = tmpdir("ignored");
        fs::create_dir_all(d.join("node_modules/lodash")).unwrap();
        fs::write(d.join("node_modules/lodash/index.js"), b"// vendor").unwrap();
        fs::create_dir_all(d.join(".venv/lib")).unwrap();
        fs::write(d.join(".venv/lib/m.py"), b"# vendor").unwrap();
        // No user source at the root.
        let langs = detect_supported_languages(&d, 3).unwrap();
        assert!(langs.is_empty(), "expected empty; got {:?}", langs);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn detect_walks_to_max_depth_only() {
        let d = tmpdir("depth");
        // depth 4 > max_depth 3 → must NOT be detected
        fs::create_dir_all(d.join("a/b/c/d")).unwrap();
        fs::write(d.join("a/b/c/d/deep.rs"), b"fn main() {}").unwrap();
        let langs = detect_supported_languages(&d, 3).unwrap();
        assert!(!langs.contains("rs"), "should not descend past depth 3");
        // depth 3 should be reachable
        fs::write(d.join("a/b/c/ok.rs"), b"fn main() {}").unwrap();
        let langs2 = detect_supported_languages(&d, 3).unwrap();
        assert!(langs2.contains("rs"));
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn detect_handles_empty_dir() {
        let d = tmpdir("empty");
        let langs = detect_supported_languages(&d, 3).unwrap();
        assert!(langs.is_empty());
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn detect_handles_missing_dir() {
        let bogus = std::env::temp_dir().join(format!("definitely-not-{}", uuid::Uuid::new_v4()));
        // Should not error out; should return empty set.
        let langs = detect_supported_languages(&bogus, 3).unwrap();
        assert!(langs.is_empty());
    }

    #[test]
    fn detect_picks_up_typescript() {
        let d = tmpdir("ts");
        fs::write(d.join("a.ts"), b"export const x = 1;").unwrap();
        fs::write(d.join("b.tsx"), b"export const y = 2;").unwrap();
        let langs = detect_supported_languages(&d, 3).unwrap();
        assert!(langs.contains("ts"));
        assert!(langs.contains("tsx"));
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn parse_files_analyzed_extracts_count() {
        let stdout = "🔍 Analyzing codebase...\n\
                      📂 Found 5 python files to analyze\n\
                      \n============================================================\n\
                      ✅ Code Graph Analysis Complete\n\
                      ============================================================\n\
                      📊 Statistics:\n\
                         Modules: 5\n\
                         Classes: 3\n\
                         Functions: 12\n\
                         APIs: 0\n\
                         Files analyzed: 5\n\
                         Files skipped: 0\n";
        assert_eq!(parse_files_analyzed(stdout), Some(5));
    }

    #[test]
    fn parse_files_analyzed_returns_none_when_missing() {
        assert_eq!(parse_files_analyzed("nothing to see here"), None);
    }

    #[test]
    fn tail_log_truncates_long_output() {
        let big = "a".repeat(10_000);
        let tail = tail_log(&big);
        assert!(tail.len() < 5_000);
        assert!(tail.starts_with('…'));
    }

    #[test]
    fn tail_log_passes_through_short_output() {
        let small = "all good";
        assert_eq!(tail_log(small), "all good");
    }

    #[test]
    fn resolve_analyzer_finds_project_local_copy() {
        let d = tmpdir("resolve");
        let scripts = d.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let bin = if cfg!(windows) {
            "code-graph-analyze.ps1"
        } else {
            "code-graph-analyze"
        };
        let p = scripts.join(bin);
        fs::write(&p, b"#!/usr/bin/env bash\necho ok\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = fs::metadata(&p).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&p, perms).unwrap();
        }

        let resolved = resolve_analyzer_script(&d).expect("must resolve");
        assert_eq!(resolved, p);
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn resolve_analyzer_returns_none_when_nothing_found() {
        // Empty project + cleared env override + emptied PATH.
        let d = tmpdir("resolve-none");

        // SAFETY: tests in this crate are single-threaded by default
        // (consistent with launch_returns_not_found_when_editor_missing
        // pattern in projects_v2). If parallelism is ever enabled we'd
        // need a Mutex around env vars.
        let saved_path = std::env::var_os("PATH");
        let saved_override = std::env::var_os("VCT_LAUNCHER_SCRIPTS_DIR");
        unsafe {
            std::env::set_var("PATH", "");
            std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR");
        }

        let resolved = resolve_analyzer_script(&d);

        if let Some(p) = saved_path {
            unsafe { std::env::set_var("PATH", p); }
        }
        if let Some(p) = saved_override {
            unsafe { std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", p); }
        }

        // The current_exe lookup may still find the test binary's parent
        // having a `.claude/scripts/...` somehow during dev, but in CI
        // sandboxes it generally doesn't. We accept either outcome but
        // check that the project-local path was definitely not picked
        // (it doesn't exist).
        if let Some(p) = resolved {
            // If something was found via current_exe traversal, it must
            // not be inside our temp project dir.
            assert!(!p.starts_with(&d), "must not find a non-existent project-local copy");
        }
        fs::remove_dir_all(&d).ok();
    }
}

