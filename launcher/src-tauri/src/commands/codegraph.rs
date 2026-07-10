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

use crate::commands::installer::resolve_orchestrator_root;
use crate::config::LocalConfig;
use crate::db::code_graph_builds::{status as build_status, CodeGraphBuildRow};
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

/// Resolve the local Weaviate URL, mirroring the precedence rules in
/// `commands::kg::weaviate_url`: env (`VCT_WEAVIATE_URL`, then legacy
/// `WEAVIATE_URL`) > `LocalConfig` > compiled default. Kept private to
/// this module instead of factoring a shared helper because the only
/// other caller (`commands::kg`) already has its own version in tight
/// coupling with its `State<LocalConfig>` access pattern; sharing would
/// require pulling LocalConfig out of Tauri state in awkward places.
fn resolve_weaviate_url(cfg: &LocalConfig) -> String {
    if let Ok(v) = std::env::var("VCT_WEAVIATE_URL") {
        if !v.is_empty() {
            return v;
        }
    }
    if let Ok(v) = std::env::var("WEAVIATE_URL") {
        if !v.is_empty() {
            return v;
        }
    }
    cfg.weaviate_url.clone()
}

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
    // P1-D (2026-05-08): refresh the GRANTEE's env files (the access list
    // is keyed on grantee → which projects this grantee can read), so a
    // running Claude Code session in the grantee's terminal picks up
    // VCT_CODE_GRAPH_ACCESS_LIST without a restart. Soft-fail.
    let _ = crate::commands::projects_v2::refresh_project_env_with_db(
        &db,
        &req.grantee_project_id,
    );
    Ok(())
}

// ─── Access check ────────────────────────────────────────────────────────

/// Returns the acting project's access level on the target project's
/// codegraph, so the UI can render badges (read / denied) and the
/// codegraph MCP can enforce permissions before returning results.
///
/// Note: there used to be a planned launcher-side query-proxy command
/// (`CodegraphQueryReq` + a forwarder) that would funnel codegraph
/// queries through the launcher with access enforcement, intended for
/// lightweight UI stats. That was deleted in 2026-05 — UI-side stats
/// come from `codegraph_summary` (below) which proxies to Weaviate
/// directly with the same access check, and full queries go through
/// the codegraph MCP. Access enforcement happens at the MCP layer +
/// at this command for UI-side decisions.
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
    cfg: State<'_, LocalConfig>,
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
    let base = resolve_weaviate_url(&cfg);

    let project_tag = &target.name; // codegraph entities are tagged by project name

    // Codegraph entities are stored under namespaced classes
    // (`<Prefix>_CodeFunction` etc.). The prefix is configured in
    // project_codegraph_bindings; we MUST resolve it before querying
    // Weaviate or the Aggregate query returns 0 every time. Same fix
    // as commit db42af9 in codegraph_load_graph — the summary endpoint
    // also needed it but was missed. Reported 2026-04-28: VideoFrames
    // codegraph dashboard rendered 0/0/0/0/0 even though Weaviate
    // had 64 classes / 335 functions for that project.
    let cg_binding = db
        .get_project_codegraph_binding(&target_project_id)?
        .ok_or_else(|| {
            format!(
                "project {} has no codegraph binding configured (run \
                 code-graph-analyze first, or recreate the project)",
                target_project_id
            )
        })?;
    let prefix = cg_binding.collection_prefix.clone();

    let mut counts = std::collections::HashMap::new();
    for suffix in ["CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"] {
        let class = format!("{}_{}", prefix, suffix);
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
        // Key by the bare suffix so the lookup below isn't sensitive
        // to the per-project prefix.
        counts.insert(suffix.to_string(), count as u32);
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
    cfg: State<'_, LocalConfig>,
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
    let base = resolve_weaviate_url(&cfg);

    let mut nodes: Vec<CgVizNode> = Vec::new();
    let mut name_to_id: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();
    let mut edges: Vec<CgVizEdge> = Vec::new();

    // Resolve the project's code-graph collection prefix from the
    // launcher DB. Code-graph entities are namespaced per project
    // (e.g. `MyProject_CodeFunction`, `VideoFrames_CodeModule`) so we MUST
    // prefix the class name with the project's binding. Querying the
    // bare class names returns 0 results — that was the visible bug
    // reported 2026-04-28: codegraph dashboard rendered empty even
    // though Weaviate had all the data. The prefix is set by
    // `populate_project_state_from_filesystem` (commit 03eb485) and is
    // typically a sanitized form of the project name.
    let cg_binding = db
        .get_project_codegraph_binding(&target_project_id)?
        .ok_or_else(|| {
            format!(
                "project {} has no codegraph binding configured (run \
                 code-graph-analyze first, or recreate the project)",
                target_project_id
            )
        })?;
    let prefix = cg_binding.collection_prefix.clone();

    // (suffix, label_field, extra_fields). Class name is `<prefix>_<suffix>`.
    let classes: &[(&str, &str, &str)] = &[
        ("CodeModule", "path", "imports"),
        ("CodeClass", "full_name", "extends"),
        ("CodeFunction", "full_name", "calls"),
        ("CodeAPI", "endpoint", ""),
        ("CodeInteraction", "endpoint", ""),
    ];
    let mut truncated = false;
    for (suffix, label_field, edge_field) in classes {
        let class = format!("{}_{}", prefix, suffix);
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
                // entity_type uses the bare suffix (e.g. "CodeFunction"),
                // NOT the project-prefixed class name. Frontend renders
                // node icons / colors based on the entity kind, not the
                // namespaced class.
                entity_type: suffix.to_string(),
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
    cfg: State<'_, LocalConfig>,
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
    let base = resolve_weaviate_url(&cfg);

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
    /// DEPRECATED (v0.2.73 CG-3): Joern CFG/PDG extraction was removed (zero
    /// readers of `cfg_summary` / `data_flow_vars`), so this is CONSTANT FALSE.
    /// The field + its DB column are RETAINED (dropping the column needs a
    /// schema bump / migration); a future schema migration removes both. Do NOT
    /// wire new logic to it — it will never be true.
    pub joern_used: bool,
    pub error_message: Option<String>,
    pub log_tail: Option<String>,
    /// Live phase indicator. Only populated on `running` events emitted
    /// during the build (e.g. "python", "typescript", "weaviate-upload").
    /// Always None for stored rows fetched via `get_code_graph_build_status`.
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
    // W1+W3 wire-up / v0.2.16 (plan 1.4 — addendum H): track which
    // UUIDs the analyzer visits this run, then delete any per-project
    // code-graph object the analyzer DIDN'T visit. Handles the case
    // where source files have been deleted since the previous
    // analyze — without this, stale rows accumulate forever because
    // _dedup_insert's replace() only upserts visited UUIDs.
    //
    // Default: `true` when omitted (matches plan: "wizard checkbox
    // checked by default"). The frontend's Re-analyze button passes
    // the wizard checkbox value; legacy frontends that don't yet
    // pass the field still get the cleaning behaviour by default.
    // Pass `Some(false)` explicitly to opt out (advanced users only).
    prune_stale: Option<bool>,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    let prune_stale = prune_stale.unwrap_or(true);

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
        &serde_json::json!({ "name": project.name, "prune_stale": prune_stale }),
    )?;

    // W3 / v0.2.16 (plan 0.9): re-analyzing a project is the most common
    // cause of new orphan generations appearing in Weaviate (the analyzer
    // may write to a different prefix after a project rename or
    // sanitizer-version bump). Reset the legacy-collections wizard's
    // dismissal flag so the next launcher boot can re-detect cleanly —
    // otherwise a user who dismissed the wizard once is stuck never
    // seeing it again even when fresh orphans appear. Soft-fail: a
    // hiccup writing to app_state must NOT block the rebuild.
    if let Err(e) = db.app_state_set_bool("legacy_codegraph_notice_dismissed", false) {
        eprintln!(
            "[vct] warning: failed to reset legacy_codegraph_notice_dismissed \
             after rebuild_code_graph for {}: {}. Wizard re-detection on the \
             next launcher boot may be suppressed until the user clicks \
             'Re-check for legacy collections' in Preferences.",
            project.id, e
        );
    }

    spawn_initial_build(app, project.id, project.name, project.folder_path, prune_stale);
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
    // W1+W3 wire-up / v0.2.16 (plan 1.4): see `rebuild_code_graph`
    // doc. `false` is the right default for genuinely-first builds
    // (no pre-existing rows to prune). Re-builds set this to `true`
    // by default; the wizard's checkbox is the user-controlled path.
    prune_stale: bool,
) {
    tokio::spawn(async move {
        run_build_task(app, project_id, project_name, folder_path, prune_stale).await;
    });
}

/// Launcher-boot resume sweep (2026-05-12). Runs in two phases:
///
/// 1. **Mark stale-running rows as failed.** A 'running' row left over
///    from a previous launcher process is a stale ghost — its subprocess
///    died with the launcher. We don't silently re-spawn (per
///    `list_pending_code_graph_builds`'s long-standing contract): the
///    crash should be visible to the user as a failed banner with a
///    Retry button so they know their work was interrupted.
///
/// 2. **Re-spawn pending rows.** A 'pending' row means the previous
///    `create_project_v2` / `rebuild_code_graph` inserted the row but
///    crashed before `spawn_initial_build` actually ran. Re-spawn here
///    so the build picks up on next boot.
///
/// Soft-fail everywhere: a DB lookup hiccup or a missing project FK
/// must NOT block launcher boot. Each failure is logged + continued
/// past. Returns counts for the boot-log line.
///
/// Defect B (v0.2.68) — F6 boot-resume gate: `skip` is the set of project
/// IDs whose `project_setups` row is NOT terminal (the heavy create-phase
/// never finished). Those projects are RE-SPAWNED by
/// `project_setup::resume_pending_setups` (which re-runs bundle +
/// post-bundle and re-queues this build as `pending`), so resuming a build
/// against them HERE would race the bundle back onto disk — the very
/// 2026-05-06 spawn-before-bundle bug. We skip them; the resumed setup
/// re-queues + re-spawns the build in the correct order.
///
/// Called from `lib.rs::setup()` after migrations have run.
pub fn resume_pending_builds(
    app: &AppHandle,
    skip: &std::collections::HashSet<String>,
) -> (usize, usize) {
    let db = app.state::<Db>();

    // Phase 1: stale-running sweep — pid-aliveness-aware since R-4
    // (v0.2.73). Two row classes:
    //   * pid IS NULL — launcher-spawned build; its subprocess died with
    //     the previous launcher → stale ghost → failed (as always).
    //   * pid IS NOT NULL — DETACHED analyzer registered via the hub's
    //     codegraph-build endpoint (install.py's post-update resync). It
    //     legitimately survives launcher restarts: only a POSITIVELY dead
    //     pid flips the row to failed (making RT-5's silent mid-walk
    //     death visible with a Retry); alive/unknown pids are left alone.
    let mut swept = match db.mark_orphaned_running_code_graph_builds_failed(
        "launcher crashed mid-run; click Retry to re-run",
    ) {
        Ok(n) => n,
        Err(e) => {
            eprintln!(
                "[vct] warning: code-graph stale-running sweep failed: {}. \
                 Stale rows (if any) will appear as 'running' indefinitely; \
                 user can click Re-build code graph to recover.",
                e
            );
            0
        }
    };
    match db.sweep_dead_detached_code_graph_builds(crate::pid_is_alive) {
        Ok(failed) => {
            if !failed.is_empty() {
                eprintln!(
                    "[vct] code-graph sweep: {} detached walk(s) died mid-run \
                     (project ids: {:?}); rows flipped to failed",
                    failed.len(),
                    failed
                );
            }
            swept += failed.len();
        }
        Err(e) => {
            eprintln!(
                "[vct] warning: dead-detached code-graph sweep failed: {}. \
                 Detached 'running' rows (if any) keep their pill until the \
                 next boot; user can click Re-build code graph to recover.",
                e
            );
        }
    }

    // Phase 2: respawn pending. We resolve project name/folder per id
    // because `spawn_initial_build` needs both. Drop projects that no
    // longer exist (cascade-delete should already have removed their
    // build row, but defend against missed-cascades just in case).
    let pending_ids = match db.list_pending_code_graph_builds() {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[vct] warning: code-graph pending-list lookup failed: {}. \
                 Queued builds (if any) will not auto-resume this boot.",
                e
            );
            return (swept, 0);
        }
    };

    let mut respawned = 0usize;
    for pid in &pending_ids {
        // F6 gate: a project whose async setup is still incomplete is being
        // re-driven by `resume_pending_setups`; skip it here so we don't
        // spawn a build before the resumed setup re-lands the bundle.
        if skip.contains(pid) {
            continue;
        }
        let project = match db.get_project(pid) {
            Ok(Some(p)) => p,
            Ok(None) => {
                eprintln!(
                    "[vct] warning: pending code-graph build references missing project {}; skipping",
                    pid
                );
                continue;
            }
            Err(e) => {
                eprintln!(
                    "[vct] warning: lookup for pending code-graph build {}: {}; skipping",
                    pid, e
                );
                continue;
            }
        };
        // Boot-resume of a pending row: we don't know whether the
        // original rebuild_code_graph asked for prune_stale, and a
        // resumed build is more conservative than an explicit user
        // re-analyze action. Default to `false` so we don't prune
        // rows the previous (interrupted) build would have visited.
        spawn_initial_build(
            app.clone(),
            project.id,
            project.name,
            project.folder_path,
            false,
        );
        respawned += 1;
    }
    (swept, respawned)
}

/// True when the project still exists in the launcher DB. Used by
/// `run_build_task` to short-circuit if the user unregistered the
/// project mid-build (follow-up #11): we want neither an
/// `eprintln!` warning when the upsert hits a missing FK nor a
/// stray `code-graph-build-progress` event for a project the GUI
/// no longer renders. Any DB error is treated as "still exists" —
/// we'd rather emit a misleading event than swallow a real failure.
fn project_still_exists(app: &AppHandle, project_id: &str) -> bool {
    app.state::<Db>()
        .get_project(project_id)
        .map(|opt| opt.is_some())
        .unwrap_or(true)
}

/// Body of the spawned task. Errors here are recorded in the build row,
/// never propagated. Each transition emits a `code-graph-build-progress`
/// event so the GUI updates live.
///
// v0.2.72 (P5): the `.claude/` gate is resolved in `run_build_task` via
// T-GUI-DB's `codegraph_settings::resolve_codegraph_index_dot_claude`
// (explicit per-project row → host-based default). The pre-merge compile-safe
// path-based fallback (`resolve_index_dot_claude_fallback`) was removed by the
// integrator once T-GUI-DB's resolver became available — the resolver owns the
// decision, so a second heuristic here would be dead code + a drift risk.
// The Python-side default still lives in `analyze_code_graph.py`
// (`_looks_like_orchestrator_root`) for bare-CLI runs with no DB.

/// Mid-build unregister race (follow-up #11): if the user calls
/// `delete_project_v2` while this task is in flight, the DB row vanishes
/// and any subsequent upsert hits an FK violation that prints a
/// `[vct] warning: ...` to stderr. We check `project_still_exists`
/// before each major transition and exit silently if the project is
/// gone — the build's partial work doesn't matter (codegraph rows are
/// scoped to a project that's been deleted) and the user has already
/// seen the "Unregistered ..." toast.
async fn run_build_task(
    app: AppHandle,
    project_id: String,
    project_name: String,
    folder_path: String,
    // W1+W3 wire-up / v0.2.16 (plan 1.4): when true, the analyzer
    // subprocess is invoked with --prune-stale and will delete any
    // per-project code-graph object it did NOT visit this run.
    // Required for re-analyses to clean up rows for deleted source
    // files; default false for first-time builds.
    prune_stale: bool,
) {
    let started_at = chrono::Utc::now().timestamp_millis();

    // Race check #0 (defensive): the spawn could be enqueued and the
    // user could unregister before the task picks up. Bail before any
    // DB write or event emit.
    if !project_still_exists(&app, &project_id) {
        return;
    }

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
            // Race check: skip the finalize if the project's been
            // unregistered (follow-up #11). The folder-scan error is
            // expected when the user just deleted the install — no
            // need for a confusing "scan folder failed" toast on top
            // of "Unregistered ...".
            if !project_still_exists(&app, &project_id) {
                return;
            }
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
        // Race check (follow-up #11): user unregistered while we were
        // scanning. Skip the SKIPPED-status write — project's gone.
        if !project_still_exists(&app, &project_id) {
            return;
        }
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
            // Race check (follow-up #11): the unregister policy purges
            // .claude/scripts/, so post-unregister this branch is the
            // most likely failure path. Skip the toast if the project
            // is gone.
            if !project_still_exists(&app, &project_id) {
                return;
            }
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

    // 4. Build args. First run uses no `--incremental` flag so the
    //    analyzer does a full pass. Re-builds also pass through here
    //    (rebuild_code_graph upserts pending → spawn_initial_build),
    //    and we keep them as full passes too for now: incremental
    //    semantics depend on git state we don't necessarily have.
    //
    // v0.2.72 (P7): this full pass automatically RESPECTS the code-graph
    // embedding-revision gate. The gate lives in the analyzer's single
    // `_write_one_object` choke-point (analyze_code_graph.py), so every
    // entity written by this rebuild is revision-checked: rows whose stored
    // `embed_revision` != CODEGRAPH_EMBED_REVISION are FORCED to re-embed
    // (the ~7-9% of over-budget Function/Class rows P3 chunking invalidated),
    // while already-current rows hash-skip. No Rust-side flag is needed —
    // keeping the gate Python-side avoids forking the logic across languages.
    let mut args: Vec<String> = vec![
        folder_path.clone(),
        "--project".to_string(),
        project_name.clone(),
    ];
    // v0.2.73 (CG-3): Joern CFG/PDG extraction removed (zero readers of
    // cfg_summary/data_flow_vars) — no `--cfg`/`--pdg` are passed. The analyzer
    // no longer accepts those flags.
    // W1+W3 wire-up / v0.2.16 (plan 1.4 — addendum H): pass through
    // the --prune-stale flag. The analyzer tracks UUIDs it visits and
    // deletes any per-project code-graph object it did NOT visit
    // (cleans up stale rows for source files deleted since the
    // previous analyze). Default is `true` for rebuild_code_graph
    // (user-driven re-analyses); false for first-time create and
    // boot-resume to preserve conservative semantics on those paths.
    if prune_stale {
        args.push("--prune-stale".to_string());
    }

    // v0.2.72 (P5): `.claude/` gate. For a user project `.claude/` is
    // orchestrator-GENERATED tooling (bundled agents/skills/hooks/scripts),
    // not first-party source; indexing it injects that tooling as retrieval
    // "context" noise. For the orchestrator clone itself `.claude/` IS
    // first-party source under active development, so it should be indexed.
    //
    // The per-project bool lives in `module_settings`
    // (`codegraph_index_dot_claude`, default: root→true, else→false) and is
    // resolved by T-GUI-DB's `codegraph_settings::
    // resolve_codegraph_index_dot_claude(&db, &project_id)`.
    //
    // v0.2.72 integrator (T-GUI-DB merged): resolve the per-project bool from
    // `module_settings` via T-GUI-DB's resolver. It honours a user's explicit
    // per-project opt-in toggle AND applies the host-based default (orchestrator
    // root → index; other projects → exclude). On any DB error we default to
    // EXCLUDE .claude (conservative). `db` comes from `app.state::<Db>()` (this
    // task holds only `AppHandle`).
    let index_dot_claude = {
        let db = app.state::<Db>();
        crate::commands::codegraph_settings::resolve_codegraph_index_dot_claude(
            &db, &project_id,
        )
        .unwrap_or_else(|e| {
            eprintln!(
                "[vct] warning: resolve_codegraph_index_dot_claude for {}: {} \
                 — defaulting to exclude .claude",
                project_id, e
            );
            false
        })
    };
    if index_dot_claude {
        args.push("--index-dot-claude".to_string());
    } else {
        args.push("--no-index-dot-claude".to_string());
    }

    // Resolve VCT_INSTALL_ROOT: the directory of the orchestrator
    // install (where install.py created `.venv` with weaviate-client).
    // The analyzer wrapper probes this when the project itself has
    // no venv (e.g. a project registered against an existing folder
    // via the launcher's Browse flow). We pass it as an env var
    // rather than hard-coding a path in the script so the same
    // script works across multiple installs.
    //
    // Derivation:
    //   1. If the resolved analyzer script lives under an install
    //      root that has a .venv, use the script's grandparent
    //      (script_dir → .claude → install_root). This is the case
    //      when find_analyzer_script picked the launcher's own
    //      .claude/scripts/ rather than the project's.
    //   2. Otherwise (script is in the project's .claude/scripts/
    //      and the project has no venv), walk up from the launcher
    //      binary itself: `<install_root>/launcher/dist/<arch>/
    //      vct-launcher`. We hop 4 levels up to reach <install_root>.
    //      This is the FIX for the bug reported 2026-04-28: a project
    //      had a project-local analyzer script, so step 1 set
    //      VCT_INSTALL_ROOT=/some-project/Code (no venv) and the wrapper
    //      fell through to system python with no weaviate-client.
    let from_script = script
        .parent()                  // .claude/scripts/
        .and_then(|p| p.parent())  // .claude/
        .and_then(|p| p.parent())  // <install_root>/
        .map(|p| p.to_path_buf());

    fn looks_like_install_root(p: &std::path::Path) -> bool {
        // POSIX venv layout: .venv/bin/python(3)
        p.join(".venv").join("bin").join("python").is_file()
            || p.join(".venv").join("bin").join("python3").is_file()
            || p.join("claude_mcp_servers").join(".venv").join("bin").join("python").is_file()
            // Windows venv layout: .venv/Scripts/python.exe — without this,
            // launcher-managed Windows installs fall through to the
            // launcher-binary fallback path even when a venv exists right
            // next to the analyzer script. Verified against the OSS install
            // layout's Windows shape (.venv at install root, see install.ps1).
            || p.join(".venv").join("Scripts").join("python.exe").is_file()
            || p.join("claude_mcp_servers")
                .join(".venv")
                .join("Scripts")
                .join("python.exe")
                .is_file()
    }

    // v0.2.61: resolve the orchestrator install root via the SAME
    // DB-cached, marker-based resolver KG sync uses
    // (`resolve_orchestrator_root` → app_state `launcher.install_path`,
    // then a walk for the `vct-module.json` / `install.py`+`CLAUDE.md`
    // markers). This is the FIRST source, before the two legacy
    // filesystem heuristics below.
    //
    // Why this is the durable fix (codegraph-vco-lib-bootstrap-env-mismatch
    // KG node): the legacy heuristics both depend on a `.venv` sitting at
    // an exact relative depth — `from_script` needs the project to have a
    // venv at `script_dir/../..` (FALSE for a project-local analyzer
    // script in a venv-less user project), and the launcher-binary walk
    // needs a fixed hop count that breaks on dev-layout (`target/release/`) and
    // any non-matching bundle layout. When BOTH miss, `install_root` was
    // `None` → `VCT_INSTALL_ROOT` unset → the analyzer's vco_lib bootstrap
    // found nothing → `ModuleNotFoundError: No module named 'vco_lib'`.
    // The marker-based resolver doesn't care about venv placement or hop
    // count, and its DB cache is the install path written at install time,
    // so a correctly-installed orchestrator always resolves — matching
    // why KG "just works" per project (it already uses this channel).
    let install_root = resolve_orchestrator_root(&app.state::<Db>())
        // `.or_else` so the legacy heuristics run ONLY when the
        // marker/DB resolver misses (e.g. running entirely outside any
        // orchestrator clone). On success, the fallbacks never evaluate.
        .or_else(|| match from_script {
            Some(p) if looks_like_install_root(&p) => Some(p),
            _ => {
                // Legacy fallback: the launcher binary's own location.
                std::env::current_exe().ok().and_then(|exe| {
                    // launcher/dist/<arch>/vct-launcher → walk up 4 hops
                    // to reach the orchestrator install root. Try a few
                    // hop counts since dev-build (`target/release/`) and
                    // bundled (`launcher/dist/<arch>/`) layouts differ.
                    let parent = exe.parent()?.to_path_buf();
                    for hops in [3, 4, 5] {
                        let mut cur = parent.clone();
                        let mut ok = true;
                        for _ in 0..hops {
                            match cur.parent() {
                                Some(p) => cur = p.to_path_buf(),
                                None => { ok = false; break; }
                            }
                        }
                        if ok && looks_like_install_root(&cur) {
                            return Some(cur);
                        }
                    }
                    None
                })
            }
        });

    // 5. Run it. We capture stdout+stderr; they're combined into one
    //    log buffer (interleaving is fine for human debugging).
    let mut cmd = tokio::process::Command::new(&script).silent();
    cmd.args(&args)
        // Don't inherit the launcher's working dir; the analyzer is
        // path-aware and we don't want it picking up an unrelated cwd.
        .current_dir(std::env::temp_dir())
        // Don't leak Tauri's pipe to a long-running subprocess that
        // might hang on stdin: explicitly close it.
        .stdin(std::process::Stdio::null());
    if let Some(ref root) = install_root {
        cmd.env("VCT_INSTALL_ROOT", root);
        // v0.2.57 NOTE: we intentionally do NOT also set
        // VCT_ORCHESTRATOR_ROOT here. The analyzer's vco_lib sys.path
        // bootstrap was historically split (one site read
        // VCT_INSTALL_ROOT, another read VCT_ORCHESTRATOR_ROOT); the fix
        // unified both onto one validated helper that honors
        // VCT_INSTALL_ROOT (which we DO set), so the codegraph build no
        // longer needs the orchestrator-root name. A first cut also
        // exported VCT_ORCHESTRATOR_ROOT as "belt-and-suspenders" — but
        // analyze_code_graph.py ALSO reads VCT_ORCHESTRATOR_ROOT to pick
        // the soft-fail deferral-write root; setting it to the install
        // root would relocate a user project's no-embedding-backend
        // UPDATE_DEFERRED.md entry out of the project and into the
        // orchestrator clone. The helper fix alone closes the bug, so we
        // leave VCT_ORCHESTRATOR_ROOT unset and preserve that behavior.
    }
    let output = cmd.output().await;

    let finished_at = chrono::Utc::now().timestamp_millis();

    let (status_str, files_analyzed, error_msg, log_tail, joern_used) = match output {
        Ok(out) => {
            let stdout_str = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr_str = String::from_utf8_lossy(&out.stderr).to_string();
            let combined = format!("{}{}", stdout_str, stderr_str);
            let tail = tail_log(&combined);

            if out.status.success() {
                let count = parse_files_analyzed(&stdout_str).unwrap_or(0);
                // v0.2.73 C-11 / RT-3: exit 0 but stale-row prune failed
                // (`PRUNE_FAILURES=N`, N>0) → PARTIAL. Inserts SUCCEEDED,
                // so `count` (files_analyzed) MUST survive — the analyzer
                // deliberately keeps exit 0 for this case precisely so the
                // reader doesn't fall into the non-zero → FAILED / count=0
                // branch below and discard a correct insert's file count.
                // A missing line (older analyzer / prune disabled) → SUCCESS.
                let prune_failures = parse_prune_failures(&stdout_str);
                let status = success_or_partial_status(prune_failures);
                let error_msg = match prune_failures {
                    Some(n) if n > 0 => Some(format!(
                        "{} stale row(s) could not be pruned; inserts succeeded",
                        n
                    )),
                    _ => None,
                };
                (
                    status.to_string(),
                    count,
                    error_msg,
                    Some(tail),
                    false, // joern_used: v0.2.73 CG-3 removed Joern CFG/PDG (zero readers)
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
                    false, // joern_used: v0.2.73 CG-3 removed Joern CFG/PDG (zero readers)
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
    //    Race check (follow-up #11): if the user unregistered while
    //    code-graph-analyze was running, the project row is gone.
    //    Writing the build row would print an FK warning to stderr and
    //    the GUI would receive a `code-graph-build-progress` for a
    //    project it no longer renders. Skip both quietly.
    if !project_still_exists(&app, &project_id) {
        return;
    }
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
    // v0.2.54 Track J: delegates to the shared char-boundary-safe
    // capping helper (was one of three near-identical copies across
    // the codegraph / kg_sync / kg_summary command modules).
    crate::db::log_tail::cap_log_tail(s)
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

/// Parse the machine-readable `PRUNE_FAILURES=N` line from analyzer
/// stdout (v0.2.73 C-11 / RT-3). The analyzer emits this line (exactly,
/// no emoji/prefix) whenever `--prune-stale` is active, so:
///   * `Some(0)` — prune ran clean, no stale rows left behind.
///   * `Some(N)`, N>0 — inserts succeeded but N stale-row DELETEs failed
///     → the build is PARTIAL, not a hard failure (files_analyzed is
///     still meaningful and must survive).
///   * `None` — no such line (prune disabled, or an OLDER analyzer that
///     predates the contract). Treated by callers as "unknown, not
///     partial" — we NEVER fabricate a partial from a missing line.
///
/// Strict shape: a fully-trimmed line matching `PRUNE_FAILURES=<digits>`
/// with nothing else. Mirrors the Python-side regex asserted by
/// `test_prune_failures_line_regex_parseable` (`^PRUNE_FAILURES=(\d+)$`).
///
// must match analyze_code_graph.py PRUNE_FAILURES= emit (C-11)
pub(crate) fn parse_prune_failures(stdout: &str) -> Option<u32> {
    const PREFIX: &str = "PRUNE_FAILURES=";
    for line in stdout.lines() {
        let trimmed = line.trim();
        if let Some(digits) = trimmed.strip_prefix(PREFIX) {
            // Strict: the remainder must be non-empty and ALL ASCII
            // digits — reject `PRUNE_FAILURES=abc`, `PRUNE_FAILURES=`,
            // trailing junk, etc. `u32::from_str_radix(_, 10)` already
            // rejects signs, whitespace and non-digits.
            if !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()) {
                if let Ok(n) = digits.parse::<u32>() {
                    return Some(n);
                }
            }
        }
    }
    None
}

/// Select the terminal build status for a subprocess that exited 0.
///
/// A pure helper (no I/O, no subprocess) so the success→partial decision
/// is unit-testable without spawning the analyzer. Given the parsed
/// prune-failure count (from [`parse_prune_failures`]):
///   * `Some(n)` with n>0 → `partial` (inserts succeeded, stale prune
///     incomplete — file count preserved by the caller).
///   * `Some(0)` / `None`  → `success` (clean prune, or an older analyzer
///     that didn't emit the line — unknown is treated as not-partial).
///
// must match analyze_code_graph.py PRUNE_FAILURES= emit (C-11)
pub(crate) fn success_or_partial_status(prune_failures: Option<u32>) -> &'static str {
    if prune_failures.unwrap_or(0) > 0 {
        build_status::PARTIAL
    } else {
        build_status::SUCCESS
    }
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
    resolve_bundled_script(project_folder, bin)
}

/// Generic resolver for a bundled `.claude/scripts/<bin>` wrapper, shared by
/// `resolve_analyzer_script` (code-graph-analyze) and
/// `orchestrator_core::build_script_command` (kg-sync, kg-duplicates,
/// code-graph-analyze). One home for the candidate ladder + stale-guard so a
/// fix here reaches every wrapper.
///
/// Order:
///   1. Project-local `<project>/.claude/scripts/<bin>` — but for wrappers
///      that ship the resilient (RT-4-era) interpreter ladder, ONLY if the
///      copy on disk still carries the ladder marker. A stale pre-RT-4 copy
///      (or an unreadable file) is skipped: WARN once, emit a best-effort
///      per-project deferral, and fall through to the orchestrator copy.
///   2-4. Orchestrator copy via `$VCT_LAUNCHER_SCRIPTS_DIR`, sibling-of-exe,
///      then PATH — see `resolve_orchestrator_script`.
///
/// Returns `None` if nothing resolves.
pub(crate) fn resolve_bundled_script(
    project_folder: &std::path::Path,
    bin: &str,
) -> Option<std::path::PathBuf> {
    // 1. Project-local.
    let p1 = project_folder.join(".claude").join("scripts").join(bin);
    if p1.is_file() {
        // Live bug (2026-07-10 dogfood): a project shipped a stale 2026-02
        // (pre-RT-4) `code-graph-analyze` that hardcoded an absolute venv
        // path; the launcher preferred it on MERE EXISTENCE and the build
        // exited 127. For wrappers that ship the resilient ladder, require
        // the marker before trusting the project-local copy. Wrappers that
        // never carried the marker (e.g. kg-duplicates) skip the check —
        // marker-checking them would flag every healthy copy as stale.
        if !wrapper_requires_resilience_marker(bin) || analyzer_wrapper_is_resilient(&p1) {
            return Some(p1);
        }
        eprintln!(
            "[codegraph] WARN: project-local wrapper {} is stale \
             (pre-RT-4: no resilient interpreter-discovery marker) — falling \
             back to the orchestrator copy. A single-file bundle refresh will \
             heal it.",
            p1.display()
        );
        emit_stale_wrapper_deferral(project_folder, bin, &p1);
        // fall through to the orchestrator candidates below
    }

    resolve_orchestrator_script(bin)
}

/// Candidates 2-4 of the bundled-script ladder: the ORCHESTRATOR copy of
/// `<bin>`, reached via `$VCT_LAUNCHER_SCRIPTS_DIR`, sibling-of-exe, then
/// PATH. Split out so both the project-local-first resolver and any
/// orchestrator-fallback caller share ONE candidate walk.
pub(crate) fn resolve_orchestrator_script(bin: &str) -> Option<std::path::PathBuf> {
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

/// Which bundled wrappers ship the resilient (`$VCT_INSTALL_ROOT`) ladder and
/// therefore SHOULD be stale-checked. `code-graph-analyze[.ps1]` (RT-4) and
/// `kg-sync[.ps1]` (v0.2.37 backport) do. `kg-duplicates` is a simple
/// project-local-only wrapper with no ladder and no `.ps1` sibling — marker-
/// checking it would false-positive every healthy copy, so it is excluded.
fn wrapper_requires_resilience_marker(bin: &str) -> bool {
    let stem = bin.strip_suffix(".ps1").unwrap_or(bin);
    matches!(stem, "code-graph-analyze" | "kg-sync")
}

/// Health-check a project-local `code-graph-analyze` / `.ps1` wrapper: does
/// it carry the RESILIENT interpreter-discovery ladder shipped since RT-4
/// (2026-06-27)?
///
/// Marker: `VCT_INSTALL_ROOT`. It appears in BOTH the shipped `.sh` and
/// `.ps1` templates as the canonical orchestrator-clone-root venv tier, and
/// is ABSENT from the pre-RT-4 2026-02 wrapper that hardcoded an absolute
/// venv path (the wrapper this guard exists to reject). We deliberately DO
/// NOT add a fresh marker line to the templates — we detect a string that is
/// already shipped, so the check stays true no matter how the templates are
/// re-worded, as long as they keep honouring `$VCT_INSTALL_ROOT`.
///
/// Conservative default: an unreadable file returns `false` (treated as
/// stale). Falling through to the orchestrator copy is always safe — that
/// candidate is healthy by definition of the ladder.
fn analyzer_wrapper_is_resilient(path: &std::path::Path) -> bool {
    match std::fs::read_to_string(path) {
        Ok(contents) => contents.contains("VCT_INSTALL_ROOT"),
        Err(_) => false,
    }
}

/// Best-effort: append a per-project `stale_codegraph_wrapper_pending`
/// deferral entry to `<project>/.claude/context/UPDATE_DEFERRED.md`,
/// suggesting a single-file `--force` refresh of the stale wrapper.
///
/// Reuses the sanctioned `vco_lib.deferral_report.{DeferralEntry,
/// DeferralReport}` machinery via a `-c` snippet — the same subprocess-into-
/// Python pattern as `projects_v2::emit_codegraph_rename_deferral` and
/// `storage_ux::emit_deferral` (mirror-don't-fork: these deferral emitters
/// are deliberately kept as thin local mirrors of the one Python writer).
/// Idempotent at the Python layer (`add_entry` dedups by `condition_id`), so
/// repeated resolutions don't pile up duplicate rows. Soft-fails at every
/// gate (no repo root, no python, subprocess error) — a deferral is an FYI,
/// never a blocker for the resolution it annotates.
fn emit_stale_wrapper_deferral(
    project_folder: &std::path::Path,
    bin: &str,
    stale_wrapper: &std::path::Path,
) {
    let repo_root = match crate::commands::installer::find_local_repo_root() {
        Ok(r) => r,
        Err(_) => return,
    };
    let py = match stale_wrapper_pick_python() {
        Some(p) => p,
        None => return,
    };

    let detected = format!(
        "The project-local code-graph analyzer wrapper at {} is a pre-RT-4 \
         (2026-02-era) copy: it lacks the resilient $VCT_INSTALL_ROOT \
         interpreter-discovery ladder, so it can hard-code a stale venv path \
         and fail (exit 127 / ModuleNotFoundError). The launcher is currently \
         falling back to the healthy orchestrator copy, so builds still work; \
         refresh this one file to restore project-local resolution.",
        stale_wrapper.display()
    );
    // POSIX single-quote the folder for the emitted shell command (paths may
    // contain spaces; embedded single quotes use the standard '\'' escape).
    let folder_sh = format!("'{}'", project_folder.display().to_string().replace('\'', r"'\''"));
    // install-bundle has NO single-file flag (verified against its argparse:
    // --folder/--update/--force/--dry-run only — no `--only`). Emitting an
    // invented flag would be argparse-rejected (the C-10 lesson). Give TWO
    // valid remediations: a targeted one-file copy from the bundle template,
    // or the full manifest-driven refresh (--force overwrites the stale copy).
    let command_to_apply = format!(
        "# Option A — refresh JUST the stale wrapper by copying the bundle \
         template (replace <orchestrator-root> with your install root):\n\
         cp <orchestrator-root>/templates/scripts/{bin} \
         {folder}/.claude/scripts/{bin}\n\
         \n\
         # Option B — full manifest-driven bundle refresh (--force also \
         overwrites ANY other user-modified bundle files, so review the \
         resulting UPDATE_DEFERRED summary):\n\
         python -m vco_lib.project_init install-bundle --update --force \
         --folder {folder}",
        bin = bin,
        folder = folder_sh
    );

    let repo_py = stale_wrapper_py_quote(&repo_root.to_string_lossy());
    let folder_py = stale_wrapper_py_quote(&project_folder.to_string_lossy());
    let detected_py = stale_wrapper_py_quote(&detected);
    let cmd_py = stale_wrapper_py_quote(&command_to_apply);
    let script = format!(
        "import sys\n\
         sys.path.insert(0, {repo_py})\n\
         from pathlib import Path\n\
         from vco_lib.deferral_report import DeferralEntry, DeferralReport\n\
         folder = Path({folder_py})\n\
         report = DeferralReport.read(folder)\n\
         entry = DeferralEntry(\n\
         \x20\x20\x20\x20condition_id=\"stale_codegraph_wrapper_pending\",\n\
         \x20\x20\x20\x20title=\"Stale project-local code-graph analyzer wrapper\",\n\
         \x20\x20\x20\x20detected={detected_py},\n\
         \x20\x20\x20\x20why_deferred=\"Overwriting a user-touched wrapper \
without consent could clobber local edits; the orchestrator copy is used \
meanwhile so nothing is broken. Refresh the one file when convenient.\",\n\
         \x20\x20\x20\x20command_to_apply={cmd_py},\n\
         \x20\x20\x20\x20severity=\"info\",\n\
         )\n\
         report.add_entry(entry)\n\
         report.write(folder)\n",
    );
    let status = std::process::Command::new(&py)
        .silent()
        .arg("-c")
        .arg(&script)
        .status();
    match status {
        Ok(s) if s.success() => {}
        Ok(s) => eprintln!(
            "[codegraph] stale-wrapper deferral helper exited {}",
            s
        ),
        Err(e) => eprintln!(
            "[codegraph] stale-wrapper deferral helper spawn failed: {}",
            e
        ),
    }
}

/// First `python3` then `python` from PATH. Mirrors
/// `chunker_revision_deferral::pick_python` / `storage_ux`'s resolution order
/// so deferral behaviour is consistent across every Rust-side emitter.
fn stale_wrapper_pick_python() -> Option<std::path::PathBuf> {
    for candidate in ["python3", "python"] {
        #[cfg(windows)]
        let names = [format!("{candidate}.exe"), candidate.to_string()];
        #[cfg(not(windows))]
        let names = [candidate.to_string()];
        if let Some(paths) = std::env::var_os("PATH") {
            for dir in std::env::split_paths(&paths) {
                for name in &names {
                    let probe = dir.join(name);
                    if probe.is_file() {
                        return Some(probe);
                    }
                }
            }
        }
    }
    None
}

/// Quote `s` as a Python double-quoted string literal. Mirrors
/// `chunker_revision_deferral::py_quote` byte-for-byte (kept local: that fn
/// is private and widening its visibility for one caller adds no
/// architectural value — same documented rationale as the other mirrors).
fn stale_wrapper_py_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
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

    // --- v0.2.73 C-11 / RT-3: PRUNE_FAILURES= parsing + partial status ---

    #[test]
    fn parse_prune_failures_zero_is_some_zero() {
        // A clean prune positively confirms N=0 (absence-of-line is NOT
        // confirmation — that's the `None` case below).
        assert_eq!(parse_prune_failures("PRUNE_FAILURES=0"), Some(0));
    }

    #[test]
    fn parse_prune_failures_nonzero_extracts_count() {
        assert_eq!(parse_prune_failures("PRUNE_FAILURES=7"), Some(7));
    }

    #[test]
    fn parse_prune_failures_finds_line_among_other_output() {
        let stdout = "🔍 Analyzing codebase...\n\
                      📊 Statistics:\n\
                         Files analyzed: 5\n\
                      PRUNE_FAILURES=3\n\
                      done\n";
        assert_eq!(parse_prune_failures(stdout), Some(3));
    }

    #[test]
    fn parse_prune_failures_absent_is_none() {
        // Older analyzer / prune disabled — no line at all.
        assert_eq!(parse_prune_failures("Files analyzed: 5\n"), None);
        assert_eq!(parse_prune_failures(""), None);
    }

    #[test]
    fn parse_prune_failures_rejects_malformed() {
        // Non-digit payload, empty payload, and signed values must NOT
        // parse — they'd desync from the Python `^PRUNE_FAILURES=(\d+)$`
        // contract. A malformed line reads as "unknown" (None), never a
        // fabricated partial.
        assert_eq!(parse_prune_failures("PRUNE_FAILURES=abc"), None);
        assert_eq!(parse_prune_failures("PRUNE_FAILURES="), None);
        assert_eq!(parse_prune_failures("PRUNE_FAILURES=-1"), None);
        assert_eq!(parse_prune_failures("PRUNE_FAILURES=3.5"), None);
        assert_eq!(parse_prune_failures("PRUNE_FAILURES=3 extra"), None);
        // A near-miss prefix must not match.
        assert_eq!(parse_prune_failures("PRUNE_FAILURE=3"), None);
    }

    #[test]
    fn parse_prune_failures_tolerates_surrounding_whitespace() {
        // The line is fully trimmed before matching, so leading/trailing
        // whitespace (and a trailing CR from CRLF pipes) is fine.
        assert_eq!(parse_prune_failures("   PRUNE_FAILURES=4   "), Some(4));
        assert_eq!(parse_prune_failures("PRUNE_FAILURES=9\r"), Some(9));
    }

    #[test]
    fn success_or_partial_status_selects_partial_only_when_positive() {
        // N>0 → partial; N=0, absent, all → success. This is the exact
        // status-selection the exit-0 reader branch wires.
        assert_eq!(success_or_partial_status(Some(1)), build_status::PARTIAL);
        assert_eq!(success_or_partial_status(Some(42)), build_status::PARTIAL);
        assert_eq!(success_or_partial_status(Some(0)), build_status::SUCCESS);
        assert_eq!(success_or_partial_status(None), build_status::SUCCESS);
    }

    #[test]
    fn partial_path_preserves_files_analyzed() {
        // Regression guard for the whole point of C-11: a build that
        // inserted 5 files but had 3 prune failures is PARTIAL, and the
        // file count (5) MUST survive — it must NOT collapse to the
        // failed-branch's count=0.
        let stdout = "Files analyzed: 5\nPRUNE_FAILURES=3\n";
        let count = parse_files_analyzed(stdout).unwrap_or(0);
        let status = success_or_partial_status(parse_prune_failures(stdout));
        assert_eq!(status, build_status::PARTIAL);
        assert_eq!(count, 5, "files_analyzed must survive a partial build");
    }

    #[test]
    fn clean_prune_is_success_with_count() {
        let stdout = "Files analyzed: 8\nPRUNE_FAILURES=0\n";
        let count = parse_files_analyzed(stdout).unwrap_or(0);
        let status = success_or_partial_status(parse_prune_failures(stdout));
        assert_eq!(status, build_status::SUCCESS);
        assert_eq!(count, 8);
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

    /// A HEALTHY (RT-4-era) project-local wrapper — one carrying the
    /// `VCT_INSTALL_ROOT` resilient-discovery marker — still wins on mere
    /// existence.
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
        // Include the RT-4 resilient-ladder marker so the health-check
        // (analyzer_wrapper_is_resilient) treats it as healthy.
        fs::write(
            &p,
            b"#!/usr/bin/env bash\n# CANDIDATES use $VCT_INSTALL_ROOT\necho ok\n",
        )
        .unwrap();
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
    fn analyzer_wrapper_is_resilient_marker_check() {
        let d = tmpdir("marker");
        let healthy = d.join("healthy");
        let stale = d.join("stale");
        // Healthy wrapper: carries the ladder marker.
        fs::write(&healthy, b"#!/bin/bash\nCANDIDATES=( \"${VCT_INSTALL_ROOT:-}/.venv\" )\n")
            .unwrap();
        // Stale pre-RT-4 wrapper: hardcoded absolute path, no ladder marker.
        fs::write(&stale, b"#!/bin/bash\nsource /home/user/.venv/bin/activate\n")
            .unwrap();

        assert!(analyzer_wrapper_is_resilient(&healthy));
        assert!(!analyzer_wrapper_is_resilient(&stale));
        // Unreadable / missing path → conservative default: stale.
        assert!(!analyzer_wrapper_is_resilient(&d.join("does-not-exist")));
        fs::remove_dir_all(&d).ok();
    }

    /// A STALE project-local wrapper (no RT-4 marker) is SKIPPED; the
    /// orchestrator copy (resolved via `$VCT_LAUNCHER_SCRIPTS_DIR`, candidate
    /// 2) wins instead — and a per-project deferral is emitted.
    #[test]
    fn resolve_analyzer_skips_stale_project_local_and_falls_back() {
        let bin = if cfg!(windows) {
            "code-graph-analyze.ps1"
        } else {
            "code-graph-analyze"
        };

        // Project with a STALE project-local wrapper (no ladder marker).
        let proj = tmpdir("stale-proj");
        let scripts = proj.join(".claude").join("scripts");
        fs::create_dir_all(&scripts).unwrap();
        let stale_p = scripts.join(bin);
        fs::write(&stale_p, b"#!/bin/bash\nsource /nonexistent/.venv/bin/activate\n")
            .unwrap();

        // Orchestrator copy reachable via VCT_LAUNCHER_SCRIPTS_DIR (candidate 2).
        let orch = tmpdir("orch-scripts");
        let orch_p = orch.join(bin);
        fs::write(&orch_p, b"#!/bin/bash\n# $VCT_INSTALL_ROOT ladder\necho ok\n").unwrap();

        // SAFETY: crate tests run single-threaded by default (see the note in
        // resolve_analyzer_returns_none_when_nothing_found).
        let saved_override = std::env::var_os("VCT_LAUNCHER_SCRIPTS_DIR");
        unsafe {
            std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", &orch);
        }

        let resolved = resolve_analyzer_script(&proj).expect("must resolve to orchestrator copy");

        unsafe {
            match saved_override {
                Some(v) => std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", v),
                None => std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR"),
            }
        }

        // The stale project-local copy must NOT have been picked.
        assert_ne!(resolved, stale_p, "stale project-local wrapper must be skipped");
        assert_eq!(resolved, orch_p, "orchestrator copy (candidate 2) must win");

        // Best-effort deferral: when python is on PATH + a repo root resolves,
        // the entry lands in the project's UPDATE_DEFERRED.md. We assert the
        // condition_id only if the file was written (the emit soft-fails in
        // sandboxes without python / repo root — that's by design).
        let deferral = proj
            .join(".claude")
            .join("context")
            .join("UPDATE_DEFERRED.md");
        if deferral.is_file() {
            let body = fs::read_to_string(&deferral).unwrap();
            assert!(
                body.contains("stale_codegraph_wrapper_pending"),
                "deferral body must carry the stale-wrapper condition_id; got:\n{}",
                body
            );
        }

        fs::remove_dir_all(&proj).ok();
        fs::remove_dir_all(&orch).ok();
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

    /// Race-fix regression test (2026-05-06): codifies the invariant that
    /// `create_project_v2` must establish before `spawn_initial_build`
    /// runs. Pre-fix the spawn was issued BEFORE `run_install_bundle`
    /// dropped `.claude/scripts/code-graph-analyze`; the background
    /// build task therefore raced ahead, found nothing, and failed with
    /// "code-graph-analyze script not found".
    ///
    /// This test simulates BOTH states of a fresh project folder:
    ///   1. Empty (just `.claude/`) — analyzer absent, project-local
    ///      lookup MUST return None (or fall through to a non-project
    ///      hit found via $PATH / $VCT_LAUNCHER_SCRIPTS_DIR — but never
    ///      a project-local hit). This is the broken state the spawn
    ///      task previously observed.
    ///   2. Bundle-installed (script present at the expected path) —
    ///      project-local lookup MUST resolve to the dropped script.
    ///      This is the state the spawn task now observes after the
    ///      ordering fix.
    ///
    /// If `create_project_v2` ever regresses and the spawn block migrates
    /// back above `run_install_bundle`, the production effect becomes
    /// "state 1" rather than "state 2" — and the build immediately
    /// reports "code-graph-analyze script not found", just as before
    /// the fix. We can't directly trap `create_project_v2`'s ordering
    /// in a unit test (it requires a Tauri AppHandle + real subprocess
    /// + populate DB), but we CAN trap the side-effect difference: a
    /// project-local hit is impossible without a prior bundle install.
    #[test]
    fn resolve_analyzer_requires_bundle_install_before_project_local_hit() {
        let d = tmpdir("race-invariant");
        let scripts = d.join(".claude").join("scripts");
        let bin = if cfg!(windows) {
            "code-graph-analyze.ps1"
        } else {
            "code-graph-analyze"
        };
        let script = scripts.join(bin);

        // Isolate from a polluted dev environment: the test machine may
        // have $PATH / $VCT_LAUNCHER_SCRIPTS_DIR / sibling-of-exe hits
        // that would mask the project-local-only assertion. Save & wipe
        // env-derived lookup paths so steps 2-4 of `resolve_analyzer_script`
        // can't smuggle in a hit. (Step 3 — `current_exe` traversal — we
        // can't control; we explicitly tolerate a non-project-local hit
        // there and only assert the project-local lookup itself.)
        // SAFETY: cargo test runs this crate single-threaded by default.
        let saved_path = std::env::var_os("PATH");
        let saved_override = std::env::var_os("VCT_LAUNCHER_SCRIPTS_DIR");
        unsafe {
            std::env::set_var("PATH", "");
            std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR");
        }

        // STATE 1: pre-bundle (the buggy pre-fix order). The folder has
        // a `.claude/` from `populate_project_state_from_filesystem` but
        // NO scripts dir yet — bundle hasn't run. Project-local lookup
        // must NOT find a script under `<d>/.claude/scripts/`.
        fs::create_dir_all(d.join(".claude")).unwrap();
        let pre_resolved = resolve_analyzer_script(&d);
        assert!(
            pre_resolved.as_ref().map_or(true, |p| !p.starts_with(&d)),
            "pre-bundle: project-local hit IMPOSSIBLE — got {:?}",
            pre_resolved,
        );

        // STATE 2: post-bundle (the fixed order). `run_install_bundle`
        // has dropped the analyzer wrapper; project-local lookup MUST
        // find it now and prefer it (step 1 of the resolve order is
        // strictly highest priority). The bundle ships the RESILIENT
        // (RT-4-era) wrapper, so include the `VCT_INSTALL_ROOT` marker the
        // stale-wrapper health-check (v0.2.77) looks for.
        fs::create_dir_all(&scripts).unwrap();
        fs::write(
            &script,
            b"#!/usr/bin/env bash\n# CANDIDATES honour $VCT_INSTALL_ROOT\necho ok\n",
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = fs::metadata(&script).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&script, perms).unwrap();
        }
        let post_resolved = resolve_analyzer_script(&d).expect(
            "post-bundle: project-local script MUST resolve — \
             this is the invariant the create_project_v2 ordering fix establishes",
        );
        assert_eq!(
            post_resolved, script,
            "post-bundle: must prefer project-local script over fallbacks"
        );

        // Restore env to avoid polluting later tests in the same process.
        if let Some(p) = saved_path {
            unsafe { std::env::set_var("PATH", p); }
        }
        if let Some(p) = saved_override {
            unsafe { std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", p); }
        }

        fs::remove_dir_all(&d).ok();
    }
}

