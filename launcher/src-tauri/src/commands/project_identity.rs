//! Identity + legacy-collection commands (PR-8 / v0.2.11).
//!
//! Surfaces project KG / code-graph / display-name identity to the GUI so
//! the per-project Identity tab can edit them without going through the
//! seven-knob `KG/Codegraph` form. Also exposes detection + cleanup for
//! the legacy `ClaudeOrchestrator_*` Weaviate collections that pre-0.2.11
//! installs accumulated thanks to the hardcoded "ClaudeOrchestrator"
//! project name (PR-7's domain — these helpers are READ + EXPLICIT-DELETE
//! only, never auto-migrate).
//!
//! Design constraints:
//!   * Cross-OS via `std::path::Path` — no `#[cfg(target_os)]` branches.
//!   * Defensive about PR-3-v2: detects the orchestrator-root project row
//!     by SLUG / HOST string, never via an exhaustive `ProjectHost` match
//!     (the enum may grow an `OrchestratorRoot` variant when PR-3-v2
//!     lands; we don't want this file to bit-rot in either direction).
//!   * Soft-fail throughout: a partial-success update returns warnings so
//!     the UI can toast them without blocking the rest of the save.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::{command, State};

use crate::commands::installer::find_local_repo_root;
use crate::commands::project_env_settings::{self, ProjectEnvSettings};
use crate::commands::projects_v2::{
    refresh_project_env_with_db, sanitize_kg_collection, write_project_env_files,
};
use crate::config::LocalConfig;
use crate::db::Db;

// ─── Constants ───────────────────────────────────────────────────────────

/// Slug used by PR-3-v2's migration 013 for the auto-registered orchestrator
/// root project row. Both the slug and the legacy host string are checked at
/// runtime so this file stays correct whether migration 013 has landed yet.
const ORCHESTRATOR_ROOT_SLUG: &str = "orchestrator-root";

/// Host string PR-3-v2 plans to use for the auto-registered root row.
const ORCHESTRATOR_ROOT_HOST: &str = "orchestrator_root";

/// Suffixes used by the code-graph schema. Mirrors the canonical list in
/// `commands::kg::is_codegraph_class`. Kept private here because we
/// purposefully avoid coupling to that function's signature — the helper
/// is a `fn` not a `pub fn`, and re-exporting it for one consumer would be
/// over-coupling.
const CODE_GRAPH_SUFFIXES: &[&str] = &[
    "CodeModule",
    "CodeClass",
    "CodeFunction",
    "CodeAPI",
    "CodeInteraction",
];

/// Legacy code-graph prefix that pre-0.2.11 installs used because hooks +
/// install.py hardcoded `PROJECT_NAME=ClaudeOrchestrator`. PR-7 fixes the
/// write path; PR-8 surfaces the read path so users with stale data can
/// see + clean it up.
const LEGACY_CODEGRAPH_PREFIX: &str = "ClaudeOrchestrator";

// ─── Detect orchestrator-root ───────────────────────────────────────────

/// True when the project row looks like the orchestrator-root row that
/// PR-3-v2's migration 013 auto-registers. Two-prong heuristic so this
/// stays correct both before and after PR-3-v2 lands:
///   1. slug == "orchestrator-root" (the migration always sets this).
///   2. host string == "orchestrator_root" (set when the `ProjectHost`
///      enum gains the variant).
/// Either is enough on its own — they're written in lock-step by the
/// migration but the slug check matches even on pre-migration databases
/// where a user manually slugged a clone-folder project as
/// "orchestrator-root".
fn is_orchestrator_root_row(slug: &str, host_str: &str) -> bool {
    slug == ORCHESTRATOR_ROOT_SLUG || host_str == ORCHESTRATOR_ROOT_HOST
}

// ─── ProjectIdentity ─────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct ProjectIdentity {
    pub project_id: String,
    pub name: String,
    pub folder_path: String,
    pub host: String,
    pub slug: String,
    /// True for the orchestrator-root project row (PR-3-v2).
    pub is_orchestrator_root: bool,
    /// Resolved KG collection name (from `project_kg_bindings.role='primary'`,
    /// or sanitized project name as fallback when no row exists yet).
    pub kg_collection: String,
    /// Resolved code-graph prefix (from `project_codegraph_bindings`, or
    /// sanitized project name as fallback).
    pub code_graph_project: String,
    /// Identity source-of-truth file on disk (`.vscode/settings.json` for
    /// user projects, `.claude/settings.json` for the orchestrator root).
    /// Pure UI hint — the launcher always writes both surfaces.
    pub identity_source: String,
    /// Bundled-launcher version exported by the clone's `vct-module.json`,
    /// when the row is the orchestrator root and the file is readable.
    /// Pure UI hint; identity edits don't consume this.
    pub vct_module_version: Option<String>,
}

#[command]
pub async fn get_project_identity(
    project_id: String,
    db: State<'_, Db>,
) -> Result<ProjectIdentity, String> {
    get_project_identity_with_db(&db, &project_id)
}

/// Inner helper without Tauri State. Used by the Tauri command and by
/// `update_project_identity` (which calls back into the identity read at
/// the end of its own flow).
pub fn get_project_identity_with_db(
    db: &Db,
    project_id: &str,
) -> Result<ProjectIdentity, String> {
    let row = db
        .get_project(project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let host_str = row.host.as_str().to_string();
    let is_root = is_orchestrator_root_row(&row.slug, &host_str);

    // Resolve KG collection. Priority: existing `primary` binding → sanitized
    // project name fallback. The fallback matches what `populate_kg_bindings`
    // would have written, so the field is never empty.
    let kg_collection = db
        .list_project_kg_bindings(project_id)
        .ok()
        .and_then(|bindings| {
            bindings
                .into_iter()
                .find(|b| b.role == "primary")
                .map(|b| b.collection_name)
        })
        .unwrap_or_else(|| format!("{}_KnowledgeGraph", sanitize_kg_collection(&row.name)));

    // Resolve code-graph prefix similarly.
    let code_graph_project = db
        .get_project_codegraph_binding(project_id)
        .ok()
        .flatten()
        .map(|b| b.collection_prefix)
        .unwrap_or_else(|| sanitize_kg_collection(&row.name));

    // Source-of-truth file:
    //   - orchestrator root: `.claude/settings.json` (the clone is the
    //     orchestrator itself; no `.vscode/settings.json::claude-code.env`
    //     is special-cased for it).
    //   - everything else: `.vscode/settings.json`. The launcher writes
    //     `.claude/settings.json` too, but the VS Code extension is the
    //     primary consumer for user-project identity.
    let identity_source = if is_root {
        ".claude/settings.json".to_string()
    } else {
        ".vscode/settings.json".to_string()
    };

    let vct_module_version = if is_root {
        read_vct_module_version(Path::new(&row.folder_path))
    } else {
        None
    };

    Ok(ProjectIdentity {
        project_id: row.id,
        name: row.name,
        folder_path: row.folder_path,
        host: host_str,
        slug: row.slug,
        is_orchestrator_root: is_root,
        kg_collection,
        code_graph_project,
        identity_source,
        vct_module_version,
    })
}

#[derive(Debug, Deserialize)]
pub struct UpdateProjectIdentityReq {
    /// New KG collection name. Optional — `None` leaves it alone.
    pub kg_collection: Option<String>,
    /// New code-graph prefix. Optional — `None` leaves it alone.
    pub code_graph_project: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct UpdateProjectIdentityResult {
    pub identity: ProjectIdentity,
    pub warnings: Vec<String>,
}

#[command]
pub async fn update_project_identity(
    project_id: String,
    req: UpdateProjectIdentityReq,
    db: State<'_, Db>,
) -> Result<UpdateProjectIdentityResult, String> {
    update_project_identity_with_db(&db, &project_id, &req)
}

/// Inner helper. The Tauri command above is a thin wrapper; this is the
/// real implementation, callable from `redetect_project_identity` without
/// re-acquiring Tauri state.
pub fn update_project_identity_with_db(
    db: &Db,
    project_id: &str,
    req: &UpdateProjectIdentityReq,
) -> Result<UpdateProjectIdentityResult, String> {
    let row = db
        .get_project(project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let mut warnings: Vec<String> = Vec::new();

    // ─── KG collection update ──────────────────────────────────────
    if let Some(new_kg) = req.kg_collection.as_ref().map(|s| s.trim()) {
        if new_kg.is_empty() {
            return Err("kg_collection cannot be empty".into());
        }
        if !is_valid_collection_name(new_kg) {
            return Err(format!(
                "kg_collection '{}' contains invalid characters \
                 (allowed: A-Z a-z 0-9 _; must start with a letter)",
                new_kg
            ));
        }

        // Preserve any non-collection-name fields on the existing primary
        // binding (embedding model, weaviate URL, etc.). Falls through to
        // populate-time defaults when no row exists yet.
        let existing = db
            .list_project_kg_bindings(project_id)
            .ok()
            .and_then(|bindings| bindings.into_iter().find(|b| b.role == "primary"));

        let embedding_model = existing
            .as_ref()
            .and_then(|b| b.embedding_model.clone())
            .unwrap_or_else(|| "qwen3-embedding:0.6b".to_string());
        let embedding_dim = existing.as_ref().and_then(|b| b.embedding_dim).unwrap_or(1024);
        let weaviate_url = existing
            .as_ref()
            .and_then(|b| b.weaviate_url.clone())
            .unwrap_or_else(|| "http://localhost:8081".to_string());

        if let Err(e) = db.set_project_kg_binding(
            project_id,
            "primary",
            new_kg,
            Some(&embedding_model),
            Some(embedding_dim),
            existing.as_ref().and_then(|b| b.kg_dir_path.as_deref()),
            Some(&weaviate_url),
            &existing
                .as_ref()
                .map(|b| b.config.clone())
                .unwrap_or(serde_json::Value::Null),
        ) {
            warnings.push(format!("set_project_kg_binding(primary): {}", e));
        }

        db.audit(
            "kg_binding_update",
            Some(project_id),
            None,
            &serde_json::json!({
                "field": "collection_name",
                "new_value": new_kg,
            }),
        )
        .ok();
    }

    // ─── Code-graph prefix update ──────────────────────────────────
    if let Some(new_cg) = req.code_graph_project.as_ref().map(|s| s.trim()) {
        if new_cg.is_empty() {
            return Err("code_graph_project cannot be empty".into());
        }
        if !is_valid_collection_name(new_cg) {
            return Err(format!(
                "code_graph_project '{}' contains invalid characters \
                 (allowed: A-Z a-z 0-9 _; must start with a letter)",
                new_cg
            ));
        }

        let existing = db
            .get_project_codegraph_binding(project_id)
            .ok()
            .flatten();

        let embedding_model = existing
            .as_ref()
            .and_then(|b| b.embedding_model.clone())
            .unwrap_or_else(|| "codesage-large-v2".to_string());
        let embedding_dim = existing.as_ref().and_then(|b| b.embedding_dim).unwrap_or(2048);

        if let Err(e) = db.set_project_codegraph_binding(
            project_id,
            new_cg,
            Some(&embedding_model),
            Some(embedding_dim),
            existing.as_ref().and_then(|b| b.last_analyzed_commit.as_deref()),
            existing.as_ref().and_then(|b| b.last_analyzed_at),
            existing.as_ref().map(|b| b.enabled).unwrap_or(true),
            &existing
                .as_ref()
                .map(|b| b.config.clone())
                .unwrap_or(serde_json::Value::Null),
        ) {
            warnings.push(format!("set_project_codegraph_binding: {}", e));
        }

        db.audit(
            "codegraph_binding_update",
            Some(project_id),
            None,
            &serde_json::json!({
                "field": "collection_prefix",
                "new_value": new_cg,
            }),
        )
        .ok();
    }

    // ─── Re-write env surfaces ────────────────────────────────────
    //
    // The launcher's env writers consume `populate(...).kg_collection`
    // for `KG_COLLECTION` and the sanitized project name for the
    // code-graph prefix. Both flow through the canonical install env
    // pair builder, so a single `refresh_project_env_with_db` call
    // propagates the new identity to the three settings surfaces
    // (`.claude/env`, `.claude/settings.json` env block, and either
    // `.vscode/settings.json::claude-code.env` for user projects or
    // the orchestrator root's own self-managed equivalent). Soft-fail
    // so a partial-success identity update still updates the DB.
    if let Err(e) = refresh_project_env_with_db(db, project_id) {
        warnings.push(format!("refresh_project_env: {}", e));
    }

    // Re-read identity so the response reflects post-update state.
    let identity = match get_project_identity_with_db(db, project_id) {
        Ok(i) => i,
        Err(e) => {
            return Err(format!(
                "identity updated but re-read failed: {}. {} warning(s) accumulated: {}",
                e,
                warnings.len(),
                warnings.join("; ")
            ));
        }
    };

    // Audit the per-row updates only — refresh_project_env_with_db
    // logs its own changes via the audit hooks in kg/codegraph commands.
    let _ = row; // row borrowed only for the existence check
    Ok(UpdateProjectIdentityResult { identity, warnings })
}

/// Re-read identity hints from the on-disk env files. Useful when the user
/// edited `.vscode/settings.json::claude-code.env` by hand and wants the
/// launcher DB / cached values to catch up. Reads:
///   - For orchestrator root: `.claude/settings.json::env` AND
///     `vct-module.json::name` (display name) AND
///     `vct-module.json::version`.
///   - For user projects: `.vscode/settings.json::claude-code.env` AND
///     `.claude/settings.json::env` (deep-merged in disk order; the
///     `.vscode/` keys win when both define the same key, matching the
///     extension's own consumption order).
#[command]
pub async fn redetect_project_identity(
    project_id: String,
    db: State<'_, Db>,
) -> Result<UpdateProjectIdentityResult, String> {
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let folder = PathBuf::from(&row.folder_path);
    let host_str = row.host.as_str().to_string();
    let is_root = is_orchestrator_root_row(&row.slug, &host_str);

    let mut warnings: Vec<String> = Vec::new();
    let disk_env = read_on_disk_env(&folder, is_root, &mut warnings);

    // Read identity-relevant keys.
    let new_kg = disk_env.get("KG_COLLECTION").cloned();
    let new_cg = disk_env.get("CODE_GRAPH_PROJECT").cloned();

    // For the orchestrator root, also pull display name from vct-module.json
    // (it's the canonical name for the clone itself).
    let new_name: Option<String> = if is_root {
        read_vct_module_name(&folder)
    } else {
        None
    };

    // Apply.
    let identity_req = UpdateProjectIdentityReq {
        kg_collection: new_kg,
        code_graph_project: new_cg,
    };
    let res = update_project_identity_with_db(&db, &project_id, &identity_req);
    let mut update_result = match res {
        Ok(r) => r,
        Err(e) => {
            return Err(format!("redetect: update_project_identity failed: {}", e));
        }
    };
    update_result.warnings.extend(warnings);

    // Orchestrator-root name rewrite is an extra step — touches `projects`
    // table directly via the existing rename path. We DO NOT rename the
    // folder; just the displayed name.
    if is_root {
        if let Some(name) = new_name {
            let trimmed = name.trim();
            if !trimmed.is_empty() && trimmed != row.name {
                if let Err(e) = db.rename_project(&row.id, trimmed, None) {
                    update_result
                        .warnings
                        .push(format!("rename_project (root display name): {}", e));
                }
            }
        }
    }

    // Re-read identity one more time to surface post-rename state.
    let identity = match get_project_identity_with_db(&db, &project_id) {
        Ok(i) => i,
        Err(e) => {
            return Err(format!(
                "redetect succeeded but final read failed: {}. {} warning(s): {}",
                e,
                update_result.warnings.len(),
                update_result.warnings.join("; ")
            ));
        }
    };
    Ok(UpdateProjectIdentityResult {
        identity,
        warnings: update_result.warnings,
    })
}

// ─── Legacy code-graph collection detection ─────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct LegacyCodegraphCollection {
    /// Full Weaviate class name (e.g. "ClaudeOrchestrator_CodeFunction").
    pub class: String,
    /// Suffix without the prefix (e.g. "CodeFunction").
    pub suffix: String,
    /// Approximate object count via the Weaviate Aggregate query (0 when
    /// the count call fails — the surface is purely informational).
    pub object_count: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct LegacyCodegraphReport {
    /// One entry per `<LEGACY_CODEGRAPH_PREFIX>_<Suffix>` class found in
    /// Weaviate's schema with at least one object.
    pub collections: Vec<LegacyCodegraphCollection>,
    /// Project rows whose code-graph prefix is NOT
    /// `LEGACY_CODEGRAPH_PREFIX` — these are candidates for re-analysis if
    /// the user accepts the offer.
    pub affected_projects: Vec<AffectedProject>,
    /// True when at least one legacy collection has > 0 objects AND at
    /// least one affected project exists. Drives the launcher first-startup
    /// banner.
    pub action_recommended: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct AffectedProject {
    pub project_id: String,
    pub name: String,
    pub current_prefix: String,
}

/// Scan Weaviate for `ClaudeOrchestrator_*` code-graph collections.
///
/// READ-ONLY. Does not modify Weaviate or the launcher DB. Returns a
/// report that drives the one-time "VCO 0.2.11 fixed a code-graph naming
/// bug" notification. The notification only fires when `action_recommended`
/// is true; the report itself is safe to call any time.
#[command]
pub async fn list_legacy_codegraph_collections(
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<LegacyCodegraphReport, String> {
    let base = resolve_weaviate_url(&cfg);
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
    {
        Ok(c) => c,
        Err(e) => return Err(format!("http client: {}", e)),
    };

    let schema_resp = match client.get(format!("{}/v1/schema", &base)).send().await {
        Ok(r) => r,
        Err(e) => {
            // Weaviate unreachable → empty report, not an error. The
            // launcher's first-startup banner stays hidden in that case.
            eprintln!("[vct] list_legacy_codegraph_collections: {}", e);
            return Ok(LegacyCodegraphReport {
                collections: Vec::new(),
                affected_projects: Vec::new(),
                action_recommended: false,
            });
        }
    };

    if !schema_resp.status().is_success() {
        return Ok(LegacyCodegraphReport {
            collections: Vec::new(),
            affected_projects: Vec::new(),
            action_recommended: false,
        });
    }
    let schema: serde_json::Value = match schema_resp.json().await {
        Ok(v) => v,
        Err(e) => return Err(format!("schema parse: {}", e)),
    };

    let classes = schema
        .get("classes")
        .and_then(|c| c.as_array())
        .cloned()
        .unwrap_or_default();

    let mut collections = Vec::new();
    for cls in &classes {
        let name = cls
            .get("class")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if name.is_empty() {
            continue;
        }
        // Looking for `<LEGACY_CODEGRAPH_PREFIX>_<Suffix>` where Suffix is
        // one of the five code-graph entity suffixes.
        let suffix = match name.strip_prefix(&format!("{}_", LEGACY_CODEGRAPH_PREFIX)) {
            Some(s) => s,
            None => continue,
        };
        if !CODE_GRAPH_SUFFIXES.iter().any(|s| *s == suffix) {
            continue;
        }
        // Best-effort object count via Aggregate. Failures → 0.
        let count = fetch_class_count(&client, &base, &name).await.unwrap_or(0);
        if count > 0 {
            collections.push(LegacyCodegraphCollection {
                class: name.clone(),
                suffix: suffix.to_string(),
                object_count: count,
            });
        }
    }

    // Find user projects whose code-graph prefix is NOT the legacy one.
    // (Project rows that intentionally use the legacy prefix get ignored —
    // they're the consumers of the data, not victims of the bug.)
    let affected_projects = match db.list_projects() {
        Ok(rows) => {
            let mut out = Vec::new();
            for row in rows {
                let binding = db
                    .get_project_codegraph_binding(&row.id)
                    .ok()
                    .flatten();
                let prefix = binding
                    .map(|b| b.collection_prefix)
                    .unwrap_or_else(|| sanitize_kg_collection(&row.name));
                if prefix != LEGACY_CODEGRAPH_PREFIX {
                    out.push(AffectedProject {
                        project_id: row.id,
                        name: row.name,
                        current_prefix: prefix,
                    });
                }
            }
            out
        }
        Err(_) => Vec::new(),
    };

    let action_recommended = !collections.is_empty() && !affected_projects.is_empty();
    Ok(LegacyCodegraphReport {
        collections,
        affected_projects,
        action_recommended,
    })
}

#[derive(Debug, Deserialize)]
pub struct CleanupLegacyReq {
    /// Caller must echo every class name back from
    /// `list_legacy_codegraph_collections` to prevent accidental deletion
    /// of UNRELATED classes that happen to start with the legacy prefix
    /// after a Weaviate schema mutation between detection and delete.
    pub classes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CleanupLegacyReport {
    pub deleted: Vec<String>,
    pub failed: Vec<CleanupFailure>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CleanupFailure {
    pub class: String,
    pub error: String,
}

/// EXPLICIT cleanup of `ClaudeOrchestrator_*` Weaviate classes. Caller MUST
/// pass every class name to delete; this command refuses to scan the
/// schema itself so the user can never have classes auto-deleted out from
/// under them.
///
/// Validation: each class name must:
///   1. Start with `<LEGACY_CODEGRAPH_PREFIX>_`.
///   2. End with one of the five canonical code-graph suffixes
///      (`CodeModule|CodeClass|CodeFunction|CodeAPI|CodeInteraction`).
/// Any class violating either condition is REJECTED with a `failed` entry
/// — never deleted. This stops a buggy / malicious caller from passing
/// `ClaudeOrchestrator_KnowledgeGraph` to the delete path (which would
/// destroy KG data, not code-graph data) or arbitrary `<X>_CodeFunction`
/// classes that don't belong to the legacy prefix.
#[command]
pub async fn cleanup_legacy_codegraph_collections(
    req: CleanupLegacyReq,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<CleanupLegacyReport, String> {
    let base = resolve_weaviate_url(&cfg);
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("http client: {}", e))?;

    let mut deleted: Vec<String> = Vec::new();
    let mut failed: Vec<CleanupFailure> = Vec::new();

    for class in req.classes.iter() {
        // Validate name structure before issuing the HTTP delete.
        let suffix = match class.strip_prefix(&format!("{}_", LEGACY_CODEGRAPH_PREFIX)) {
            Some(s) => s,
            None => {
                failed.push(CleanupFailure {
                    class: class.clone(),
                    error: format!(
                        "refuses to delete '{}': not prefixed by '{}_'",
                        class, LEGACY_CODEGRAPH_PREFIX
                    ),
                });
                continue;
            }
        };
        if !CODE_GRAPH_SUFFIXES.iter().any(|s| *s == suffix) {
            failed.push(CleanupFailure {
                class: class.clone(),
                error: format!(
                    "refuses to delete '{}': suffix '{}' is not a code-graph class",
                    class, suffix
                ),
            });
            continue;
        }

        let url = format!("{}/v1/schema/{}", &base, class);
        match client.delete(&url).send().await {
            Ok(resp) => {
                if resp.status().is_success() {
                    deleted.push(class.clone());
                } else {
                    let status = resp.status().as_u16();
                    let body = resp.text().await.unwrap_or_default();
                    failed.push(CleanupFailure {
                        class: class.clone(),
                        error: format!(
                            "weaviate returned {}: {}",
                            status,
                            body.chars().take(200).collect::<String>()
                        ),
                    });
                }
            }
            Err(e) => {
                failed.push(CleanupFailure {
                    class: class.clone(),
                    error: format!("http: {}", e),
                });
            }
        }
    }

    let _ = db.audit(
        "codegraph_legacy_cleanup",
        None,
        None,
        &serde_json::json!({
            "deleted": deleted,
            "failed_count": failed.len(),
        }),
    );

    Ok(CleanupLegacyReport { deleted, failed })
}

// ─── First-startup banner gate ───────────────────────────────────────────

const APP_STATE_KEY_LEGACY_NOTICE_DISMISSED: &str = "legacy_codegraph_notice_dismissed";

/// True when the user has dismissed the legacy-collections notification at
/// least once. UI checks this together with `list_legacy_codegraph_collections`
/// to suppress the banner on subsequent app starts.
#[command]
pub async fn get_legacy_codegraph_notice_dismissed(
    db: State<'_, Db>,
) -> Result<bool, String> {
    Ok(db
        .app_state_get_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED)
        .ok()
        .flatten()
        .unwrap_or(false))
}

/// Persist the user's dismissal so the launcher stops nagging.
#[command]
pub async fn set_legacy_codegraph_notice_dismissed(
    dismissed: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.app_state_set_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED, dismissed)?;
    Ok(())
}

// ─── Internal helpers ────────────────────────────────────────────────────

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

async fn fetch_class_count(
    client: &reqwest::Client,
    base_url: &str,
    class: &str,
) -> Result<u32, String> {
    let body = serde_json::json!({
        "query": format!("{{ Aggregate {{ {class} {{ meta {{ count }} }} }} }}", class = class)
    });
    let resp = client
        .post(format!("{}/v1/graphql", base_url))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("graphql: {}", e))?;
    let v: serde_json::Value = resp.json().await.map_err(|e| format!("parse: {}", e))?;
    Ok(v.pointer(&format!("/data/Aggregate/{}/0/meta/count", class))
        .and_then(|n| n.as_u64())
        .unwrap_or(0) as u32)
}

/// Collection name allowed alphabet, matching Weaviate's class-name rules:
/// must start with a letter; rest is [A-Za-z0-9_]. We do NOT enforce
/// PascalCase here because users may have legitimately set lowercase names
/// pre-0.2.11. The launcher's `sanitize_kg_collection` produces PascalCase
/// but consumers tolerate other casings.
fn is_valid_collection_name(name: &str) -> bool {
    let mut chars = name.chars();
    let first = match chars.next() {
        Some(c) => c,
        None => return false,
    };
    if !first.is_ascii_alphabetic() {
        return false;
    }
    for c in chars {
        if !c.is_ascii_alphanumeric() && c != '_' {
            return false;
        }
    }
    true
}

/// Read the on-disk env files for a project and return their merged KV
/// map. Priority for user projects: `.vscode/settings.json::claude-code.env`
/// then `.claude/settings.json::env` (vscode wins on duplicates). For the
/// orchestrator root only `.claude/settings.json::env` is consulted.
///
/// Soft-fail throughout: read / parse errors push a warning and return an
/// empty (partial) map.
fn read_on_disk_env(
    folder: &Path,
    is_root: bool,
    warnings: &mut Vec<String>,
) -> std::collections::HashMap<String, String> {
    let mut out = std::collections::HashMap::new();

    // .claude/settings.json::env — both surfaces consult this.
    let claude_path = folder.join(".claude").join("settings.json");
    if let Some(env_obj) = read_json_object_at(&claude_path, "env", warnings) {
        for (k, v) in env_obj {
            if let Some(s) = v.as_str() {
                out.insert(k, s.to_string());
            }
        }
    }

    // .vscode/settings.json::claude-code.env — user projects only.
    if !is_root {
        let vscode_path = folder.join(".vscode").join("settings.json");
        if let Some(env_obj) = read_json_object_at(&vscode_path, "claude-code.env", warnings) {
            for (k, v) in env_obj {
                if let Some(s) = v.as_str() {
                    // vscode wins on duplicates — overwrite.
                    out.insert(k, s.to_string());
                }
            }
        }
    }

    out
}

/// Read a JSON file and return the inner object stored under `key`. None
/// when the file doesn't exist, isn't readable, isn't JSON, the value at
/// `key` isn't an object, or `key` is missing. Errors go to `warnings`
/// (read + parse only — missing-file is silent because that's the
/// common case for a fresh project).
fn read_json_object_at(
    path: &Path,
    key: &str,
    warnings: &mut Vec<String>,
) -> Option<serde_json::Map<String, serde_json::Value>> {
    if !path.is_file() {
        return None;
    }
    let raw = match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) => {
            warnings.push(format!("read {}: {}", path.display(), e));
            return None;
        }
    };
    let v: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            warnings.push(format!("parse {}: {}", path.display(), e));
            return None;
        }
    };
    v.as_object()
        .and_then(|o| o.get(key))
        .and_then(|v| v.as_object())
        .cloned()
}

/// Pull `name` from `<folder>/vct-module.json`. Empty/missing → None.
fn read_vct_module_name(folder: &Path) -> Option<String> {
    let path = folder.join("vct-module.json");
    let raw = std::fs::read_to_string(&path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("name")
        .and_then(|x| x.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
}

/// Pull `version` from `<folder>/vct-module.json`. None on any failure;
/// silent because the orchestrator-root detection is best-effort.
fn read_vct_module_version(folder: &Path) -> Option<String> {
    let path = folder.join("vct-module.json");
    let raw = std::fs::read_to_string(&path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("version")
        .and_then(|x| x.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
}

// Suppress unused-warning when the platform layer doesn't pull
// `find_local_repo_root` / `write_project_env_files` / `ProjectEnvSettings`
// transitively. These are kept in the import block because they document
// the implicit contract this file participates in (see the comment above
// `update_project_identity` re: env-surface plumbing).
#[allow(dead_code)]
fn _doc_imports() {
    let _: fn() -> Result<PathBuf, String> = find_local_repo_root;
    let _: fn(&Path, &ProjectEnvSettings) -> Result<(), String> = write_project_env_files;
    let _ = project_env_settings::populate;
}

// ─── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn orchestrator_root_detected_by_slug() {
        assert!(is_orchestrator_root_row("orchestrator-root", "base"));
        assert!(is_orchestrator_root_row("orchestrator-root", "mao"));
    }

    #[test]
    fn orchestrator_root_detected_by_host() {
        assert!(is_orchestrator_root_row("some-other-slug", "orchestrator_root"));
    }

    #[test]
    fn ordinary_project_not_detected_as_root() {
        assert!(!is_orchestrator_root_row("my-app", "base"));
        assert!(!is_orchestrator_root_row("orchestrator", "mao"));
    }

    #[test]
    fn validates_collection_names() {
        assert!(is_valid_collection_name("MyProject_KnowledgeGraph"));
        assert!(is_valid_collection_name("a"));
        assert!(is_valid_collection_name("VCO_dev_KnowledgeGraph"));
    }

    #[test]
    fn rejects_invalid_collection_names() {
        assert!(!is_valid_collection_name(""));
        assert!(!is_valid_collection_name("_StartsWithUnderscore"));
        assert!(!is_valid_collection_name("3StartsWithDigit"));
        assert!(!is_valid_collection_name("has-dash"));
        assert!(!is_valid_collection_name("has space"));
        assert!(!is_valid_collection_name("has/slash"));
    }

    #[test]
    fn read_on_disk_env_returns_empty_for_missing_files() {
        let tmp = tempfile::tempdir().expect("mkdir tmp");
        let mut warnings = Vec::new();
        let env = read_on_disk_env(tmp.path(), false, &mut warnings);
        assert!(env.is_empty());
        assert!(warnings.is_empty());
    }

    #[test]
    fn read_on_disk_env_reads_vscode_settings_for_user_projects() {
        let tmp = tempfile::tempdir().expect("mkdir tmp");
        std::fs::create_dir_all(tmp.path().join(".vscode")).unwrap();
        let p = tmp.path().join(".vscode").join("settings.json");
        std::fs::write(
            &p,
            r#"{ "claude-code.env": { "KG_COLLECTION": "MyKG", "PROJECT_NAME": "Demo" } }"#,
        )
        .unwrap();
        let mut warnings = Vec::new();
        let env = read_on_disk_env(tmp.path(), false, &mut warnings);
        assert_eq!(env.get("KG_COLLECTION"), Some(&"MyKG".to_string()));
        assert_eq!(env.get("PROJECT_NAME"), Some(&"Demo".to_string()));
        assert!(warnings.is_empty());
    }

    #[test]
    fn read_on_disk_env_skips_vscode_for_orchestrator_root() {
        let tmp = tempfile::tempdir().expect("mkdir tmp");
        std::fs::create_dir_all(tmp.path().join(".vscode")).unwrap();
        std::fs::create_dir_all(tmp.path().join(".claude")).unwrap();
        std::fs::write(
            tmp.path().join(".vscode").join("settings.json"),
            r#"{ "claude-code.env": { "KG_COLLECTION": "VSC" } }"#,
        )
        .unwrap();
        std::fs::write(
            tmp.path().join(".claude").join("settings.json"),
            r#"{ "env": { "KG_COLLECTION": "CLAUDE" } }"#,
        )
        .unwrap();
        let mut warnings = Vec::new();
        let env = read_on_disk_env(tmp.path(), /* is_root */ true, &mut warnings);
        // Root must consume `.claude/settings.json` only.
        assert_eq!(env.get("KG_COLLECTION"), Some(&"CLAUDE".to_string()));
        assert!(warnings.is_empty());
    }

    #[test]
    fn read_on_disk_env_vscode_wins_for_user_project_duplicates() {
        let tmp = tempfile::tempdir().expect("mkdir tmp");
        std::fs::create_dir_all(tmp.path().join(".vscode")).unwrap();
        std::fs::create_dir_all(tmp.path().join(".claude")).unwrap();
        std::fs::write(
            tmp.path().join(".claude").join("settings.json"),
            r#"{ "env": { "KG_COLLECTION": "FROM_CLAUDE" } }"#,
        )
        .unwrap();
        std::fs::write(
            tmp.path().join(".vscode").join("settings.json"),
            r#"{ "claude-code.env": { "KG_COLLECTION": "FROM_VSCODE" } }"#,
        )
        .unwrap();
        let mut warnings = Vec::new();
        let env = read_on_disk_env(tmp.path(), /* is_root */ false, &mut warnings);
        // VS Code value wins.
        assert_eq!(env.get("KG_COLLECTION"), Some(&"FROM_VSCODE".to_string()));
    }

    #[test]
    fn read_on_disk_env_records_parse_warning() {
        let tmp = tempfile::tempdir().expect("mkdir tmp");
        std::fs::create_dir_all(tmp.path().join(".claude")).unwrap();
        std::fs::write(
            tmp.path().join(".claude").join("settings.json"),
            "{ not valid json",
        )
        .unwrap();
        let mut warnings = Vec::new();
        let env = read_on_disk_env(tmp.path(), false, &mut warnings);
        assert!(env.is_empty());
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].starts_with("parse "));
    }

    #[test]
    fn read_vct_module_name_returns_some_when_present() {
        let tmp = tempfile::tempdir().expect("mkdir tmp");
        std::fs::write(
            tmp.path().join("vct-module.json"),
            r#"{ "name": "VibeCoded Orchestrator", "version": "0.2.11" }"#,
        )
        .unwrap();
        assert_eq!(
            read_vct_module_name(tmp.path()).as_deref(),
            Some("VibeCoded Orchestrator")
        );
        assert_eq!(
            read_vct_module_version(tmp.path()).as_deref(),
            Some("0.2.11")
        );
    }

    #[test]
    fn read_vct_module_name_returns_none_when_missing() {
        let tmp = tempfile::tempdir().expect("mkdir tmp");
        assert!(read_vct_module_name(tmp.path()).is_none());
        assert!(read_vct_module_version(tmp.path()).is_none());
    }
}
