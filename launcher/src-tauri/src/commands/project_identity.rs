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
use crate::project_naming::canonical_class_prefix;

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

    // Resolve code-graph prefix similarly. The FALLBACK (no binding row
    // yet) uses `canonical_class_prefix` — the same single-source-of-
    // truth sanitizer the Python analyze script uses (bug 0.7, v0.2.15).
    // The previous `sanitize_kg_collection` fallback produced a
    // different prefix for `SimRacing_AI` (→ `SimRacingAI`) than the
    // analyze script (→ `SimRacing_AI`), wedging the codegraph build
    // on Weaviate's case-insensitive class-name collision.
    //
    // If `canonical_class_prefix` rejects the name (empty / leading
    // digit / etc.) we fall back to the legacy `sanitize_kg_collection`
    // which never rejects — losing parity is better than crashing the
    // identity-fetch endpoint, and the legacy path's "Project" fallback
    // keeps the UI usable.
    let code_graph_project = db
        .get_project_codegraph_binding(project_id)
        .ok()
        .flatten()
        .map(|b| b.collection_prefix)
        .unwrap_or_else(|| {
            canonical_class_prefix(&row.name)
                .unwrap_or_else(|_| sanitize_kg_collection(&row.name))
        });

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
    /// v0.2.15 (0.4) addition: orphan code-graph classes that match a
    /// known project's name case-insensitively but use a different
    /// prefix than the project's current canonical prefix. Empty when
    /// no orphans exist OR no projects exist. See `OrphanCollectionGroup`
    /// for the rationale (multi-generation cruft from VCO's prefix
    /// algorithm changing across releases).
    pub orphan_groups: Vec<OrphanCollectionGroup>,
    /// True when at least one legacy collection has > 0 objects AND at
    /// least one affected project exists. Drives the launcher first-startup
    /// banner. v0.2.15: also true when at least one orphan group has
    /// > 0 objects.
    pub action_recommended: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct AffectedProject {
    pub project_id: String,
    pub name: String,
    pub current_prefix: String,
}

/// v0.2.15 (0.4): one orphan-prefix generation detected for a known
/// project. Background: VCO's project-name → class-prefix sanitizer has
/// changed across releases (folder-name pre-v0.2.0, lowercase-collapse
/// in mid-v0.2.x, the `canonical_class_prefix` of v0.2.15). Long-lived
/// projects (especially `orchestrator_root` projects that maintainers
/// track across versions) accumulate one set of code-graph classes per
/// sanitizer-generation, all case-insensitively colliding with each
/// other under Weaviate's class-name uniqueness rules.
///
/// Each orphan group represents ONE non-canonical prefix found in
/// Weaviate that:
///   * matches a project case-insensitively when both are lowercased
///     and stripped of non-alphanumerics, AND
///   * differs from that project's CURRENT canonical prefix.
///
/// The user is offered a per-group delete with an explicit object count
/// + name. NEVER auto-deleted: some orphans may be intentional
/// multi-version comparison data the user wants to keep.
#[derive(Debug, Clone, Serialize)]
pub struct OrphanCollectionGroup {
    /// The non-canonical prefix (e.g. "VCO_dev", "Vibecoded_orchestrator").
    pub prefix: String,
    /// The project the orphan most likely belongs to. May be heuristic
    /// (case-insensitive normalised-name match).
    pub matched_project_id: String,
    /// The project's name as the user sees it in the launcher.
    pub matched_project_name: String,
    /// The project's CURRENT canonical prefix (what new analyses will
    /// write to). Useful in the UI to render "VCO_dev (orphan) →
    /// VibeCodedOrchestrator (current)".
    pub current_prefix: String,
    /// Per-suffix collection list. Same shape as the legacy
    /// `LegacyCodegraphCollection` entries so the UI can render them
    /// uniformly.
    pub collections: Vec<LegacyCodegraphCollection>,
    /// Sum of `object_count` across `collections`. Pre-computed so the
    /// UI doesn't have to.
    pub total_objects: u32,
}

/// Scan Weaviate for `ClaudeOrchestrator_*` code-graph collections.
///
/// READ-ONLY. Does not modify Weaviate or the launcher DB. Returns a
/// report that drives the one-time "VCO 0.2.11 fixed a code-graph naming
/// bug" notification. The notification only fires when `action_recommended`
/// is true; the report itself is safe to call any time.
///
/// v0.2.16 (W4 / 0.11): `include_untracked_projects` controls whether
/// collections whose prefix doesn't map to a currently-tracked project
/// appear in the report. The GUI default is `Some(false)` — clean
/// view, only orphans of currently-tracked projects surface in the
/// wizard. The advanced /preferences/weaviate-untracked route passes
/// `Some(true)` to see the full inventory (data from since-deleted
/// projects, ad-hoc analyses, etc). Defaulting to `Some(false)` keeps
/// pre-v0.2.16 callers (frontend code that doesn't pass the param)
/// on the clean view automatically.
#[command]
pub async fn list_legacy_codegraph_collections(
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
    include_untracked_projects: Option<bool>,
) -> Result<LegacyCodegraphReport, String> {
    let include_untracked = include_untracked_projects.unwrap_or(false);
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
                orphan_groups: Vec::new(),
                action_recommended: false,
            });
        }
    };

    if !schema_resp.status().is_success() {
        return Ok(LegacyCodegraphReport {
            collections: Vec::new(),
            affected_projects: Vec::new(),
            orphan_groups: Vec::new(),
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
    // v0.2.15 (0.4): also bucket every NON-legacy code-graph class by
    // prefix so we can later cross-reference against project names. The
    // BTreeMap keeps prefix order deterministic in the report (helpful
    // for stable Svelte rendering + test assertions).
    let mut code_graph_by_prefix: std::collections::BTreeMap<
        String,
        Vec<LegacyCodegraphCollection>,
    > = std::collections::BTreeMap::new();
    for cls in &classes {
        let name = cls
            .get("class")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if name.is_empty() {
            continue;
        }
        // First: classify by suffix. Any class whose suffix isn't one
        // of the five code-graph entity types is irrelevant to this
        // wizard (it might be a KG class, a `_Development` class, etc).
        let (prefix, suffix) = match split_codegraph_class_name(&name) {
            Some(parts) => parts,
            None => continue,
        };
        // Best-effort object count via Aggregate. Failures → 0.
        let count = fetch_class_count(&client, &base, &name).await.unwrap_or(0);
        if count == 0 {
            // Skip empty classes either way — they're noise that lingers
            // because Weaviate doesn't auto-delete on object drain.
            continue;
        }
        let entry = LegacyCodegraphCollection {
            class: name.clone(),
            suffix: suffix.to_string(),
            object_count: count,
        };
        if prefix == LEGACY_CODEGRAPH_PREFIX {
            // Goes into the legacy list (unchanged from v0.2.14 behaviour).
            collections.push(entry);
        } else {
            // Candidate orphan: bucket by prefix for matching against
            // project rows below.
            code_graph_by_prefix
                .entry(prefix.to_string())
                .or_default()
                .push(entry);
        }
    }

    // Find user projects whose code-graph prefix is NOT the legacy one.
    // (Project rows that intentionally use the legacy prefix get ignored —
    // they're the consumers of the data, not victims of the bug.)
    //
    // FALLBACK sanitizer is `canonical_class_prefix` (single source of
    // truth with the Python analyze script, bug 0.7). See the note in
    // `get_project_identity` above for why we fall back further to
    // `sanitize_kg_collection` on canonical-side rejection.
    //
    // v0.2.15 (0.4): also collect every project's identity (id, name,
    // canonical_prefix) so we can match orphan code-graph prefixes
    // against them. `project_identities` is the unfiltered list;
    // `affected_projects` is the legacy-cleanup subset.
    let project_identities: Vec<(String, String, String)> = match db.list_projects() {
        Ok(rows) => rows
            .into_iter()
            .map(|row| {
                let binding = db.get_project_codegraph_binding(&row.id).ok().flatten();
                let prefix = binding
                    .map(|b| b.collection_prefix)
                    .unwrap_or_else(|| {
                        canonical_class_prefix(&row.name)
                            .unwrap_or_else(|_| sanitize_kg_collection(&row.name))
                    });
                (row.id, row.name, prefix)
            })
            .collect(),
        Err(_) => Vec::new(),
    };

    let affected_projects: Vec<AffectedProject> = project_identities
        .iter()
        .filter(|(_id, _name, prefix)| prefix != LEGACY_CODEGRAPH_PREFIX)
        .map(|(id, name, prefix)| AffectedProject {
            project_id: id.clone(),
            name: name.clone(),
            current_prefix: prefix.clone(),
        })
        .collect();

    // v0.2.15 (0.4): build orphan groups. For each orphan prefix found
    // in Weaviate, attempt to attribute it to a known project via the
    // case-insensitive normalised-name match.
    //
    // v0.2.16 (W4 / 0.11): unmatched prefixes (no current project owns
    // them) are surfaced as "untracked" groups ONLY when
    // `include_untracked == true`. Default (false) keeps the wizard's
    // visual clutter down by hiding data from since-deleted projects.
    let mut orphan_groups: Vec<OrphanCollectionGroup> = Vec::new();
    for (prefix, entries) in code_graph_by_prefix {
        // Skip prefixes that exactly match SOME project's current
        // canonical — those are the ACTIVE class set, not orphans.
        if project_identities
            .iter()
            .any(|(_, _, current)| *current == prefix)
        {
            continue;
        }
        // Try to attribute by case-insensitive normalised-name match.
        let normalised_prefix = normalise_prefix_for_match(&prefix);
        let matched = project_identities.iter().find(|(_, name, _)| {
            normalise_prefix_for_match(&canonical_class_prefix(name).unwrap_or_default())
                == normalised_prefix
                || normalise_prefix_for_match(&sanitize_kg_collection(name)) == normalised_prefix
                || normalise_prefix_for_match(name) == normalised_prefix
        });
        let (matched_id, matched_name, current_prefix) = match matched {
            Some((id, name, current)) => (id.clone(), name.clone(), current.clone()),
            None => {
                if include_untracked {
                    // Untracked-projects view: surface the prefix with
                    // empty matched_* / current_prefix fields. The
                    // advanced page displays these as "no project
                    // currently linked".
                    (
                        String::new(),
                        String::new(),
                        String::new(),
                    )
                } else {
                    // Default GUI behaviour: hide untracked prefixes.
                    continue;
                }
            }
        };
        let total: u32 = entries.iter().map(|e| e.object_count).sum();
        orphan_groups.push(OrphanCollectionGroup {
            prefix,
            matched_project_id: matched_id,
            matched_project_name: matched_name,
            current_prefix,
            collections: entries,
            total_objects: total,
        });
    }

    let action_recommended = (!collections.is_empty() && !affected_projects.is_empty())
        || orphan_groups.iter().any(|g| g.total_objects > 0);
    Ok(LegacyCodegraphReport {
        collections,
        affected_projects,
        orphan_groups,
        action_recommended,
    })
}

/// Split a Weaviate class name into `(prefix, suffix)` where suffix is
/// one of the five canonical code-graph suffixes. Returns `None` for
/// any name that isn't a code-graph class (KG / Development / other
/// shapes fall through).
fn split_codegraph_class_name(class_name: &str) -> Option<(&str, &str)> {
    // Iterate suffixes longest-first to handle the `CodeAPI` ⊂ `CodeAPIWhatever`
    // case if it ever arises (it doesn't today, but cheap insurance).
    for suffix in CODE_GRAPH_SUFFIXES {
        let needle = format!("_{}", suffix);
        if let Some(stripped) = class_name.strip_suffix(&needle) {
            return Some((stripped, suffix));
        }
    }
    None
}

/// Normalise a prefix / project name for case-insensitive structural
/// matching. Strips every non-alphanumeric and lowercases the result.
/// `"VibeCoded Orchestrator"`, `"VibeCodedOrchestrator"`,
/// `"vibecoded_orchestrator"`, `"VibeCoded_Orchestrator"`, and
/// `"Vibecodedorchestrator"` all normalise to the same thing.
fn normalise_prefix_for_match(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .flat_map(|c| c.to_lowercase())
        .collect()
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

    // v0.2.15 (0.4): post-delete verification. The Weaviate REST DELETE
    // returns 200 even when the class persists due to internal
    // schema-cache lag or a transient bug. Without verification the
    // wizard's "N class(es) deleted" toast lies. Re-query the schema
    // and move silently-surviving classes from `deleted` to `failed`.
    if !deleted.is_empty() {
        if let Ok(resp) = client.get(format!("{}/v1/schema", &base)).send().await {
            if let Ok(schema) = resp.json::<serde_json::Value>().await {
                let still_present: std::collections::HashSet<String> = schema
                    .get("classes")
                    .and_then(|c| c.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|c| {
                                c.get("class").and_then(|v| v.as_str()).map(String::from)
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let (still_deleted, survivors): (Vec<_>, Vec<_>) = deleted
                    .into_iter()
                    .partition(|cls| !still_present.contains(cls));
                deleted = still_deleted;
                for cls in survivors {
                    failed.push(CleanupFailure {
                        class: cls,
                        error: "delete returned 200 but class still in schema \
                                (possible Weaviate cache lag — retry the cleanup)"
                            .to_string(),
                    });
                }
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

// ─── v0.2.15 (0.4): orphan code-graph cleanup ────────────────────────────

#[derive(Debug, Deserialize)]
pub struct CleanupOrphanReq {
    /// Caller MUST echo every class name back from
    /// `list_legacy_codegraph_collections().orphan_groups[*].collections`
    /// to prevent accidental deletion of unrelated classes. The wizard
    /// presents per-group checkboxes; only the user-selected groups'
    /// class lists end up here.
    pub classes: Vec<String>,
}

/// Delete user-selected orphan code-graph classes. Same safety contract
/// as `cleanup_legacy_codegraph_collections`: caller passes every class
/// name explicitly, this command refuses to scan the schema itself.
/// Validation differs: accepts ANY prefix (since orphans by definition
/// have non-legacy prefixes), but still requires the suffix to be one
/// of the five canonical code-graph suffixes — guards against
/// accidentally deleting a KG or `_Development` class via this surface.
///
/// Post-delete verification mirrors the legacy cleanup (re-query schema
/// to catch silent-partial-fails).
#[command]
pub async fn cleanup_orphan_codegraph_collections(
    req: CleanupOrphanReq,
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
        // Must end with a canonical code-graph suffix. Prefix is
        // user-chosen (any valid Weaviate class name except the legacy
        // one we already have a separate path for).
        let (_, _) = match split_codegraph_class_name(class) {
            Some(parts) => parts,
            None => {
                failed.push(CleanupFailure {
                    class: class.clone(),
                    error: format!(
                        "refuses to delete '{}': not a code-graph class \
                         (must end with _CodeModule/_CodeClass/_CodeFunction/\
                         _CodeAPI/_CodeInteraction)",
                        class
                    ),
                });
                continue;
            }
        };
        // Explicitly forbid the legacy prefix here — the user must use
        // the legacy-cleanup surface for that, which audits separately.
        if class.starts_with(&format!("{}_", LEGACY_CODEGRAPH_PREFIX)) {
            failed.push(CleanupFailure {
                class: class.clone(),
                error: format!(
                    "use cleanup_legacy_codegraph_collections for '{}_*' classes",
                    LEGACY_CODEGRAPH_PREFIX
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

    // Post-delete verification (same as legacy cleanup).
    if !deleted.is_empty() {
        if let Ok(resp) = client.get(format!("{}/v1/schema", &base)).send().await {
            if let Ok(schema) = resp.json::<serde_json::Value>().await {
                let still_present: std::collections::HashSet<String> = schema
                    .get("classes")
                    .and_then(|c| c.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|c| {
                                c.get("class").and_then(|v| v.as_str()).map(String::from)
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let (still_deleted, survivors): (Vec<_>, Vec<_>) = deleted
                    .into_iter()
                    .partition(|cls| !still_present.contains(cls));
                deleted = still_deleted;
                for cls in survivors {
                    failed.push(CleanupFailure {
                        class: cls,
                        error: "delete returned 200 but class still in schema \
                                (possible Weaviate cache lag — retry the cleanup)"
                            .to_string(),
                    });
                }
            }
        }
    }

    let _ = db.audit(
        "codegraph_orphan_cleanup",
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

// ─── W3 / v0.2.16 (2026-05-18): wizard UX hardening ──────────────────────
//
// Two complementary commands plus a shared status struct so the wizard
// can poll per-project rebuild progress and the user can force a
// re-detection from the Preferences page:
//
//   * `get_code_graph_build_status_for_projects` — batched read of
//     `code_graph_builds` for the wizard's poll loop. Replaces the
//     misleading "Started for N/N project(s)" UX that never advanced
//     after kickoff (plan 0.3).
//   * `force_recheck_legacy_codegraph` — flips the dismissed flag back
//     to `false`. Used by the new "Re-check for legacy collections"
//     button in Preferences (plan 0.9). The companion change in
//     `commands::codegraph::rebuild_code_graph` resets the same flag
//     automatically whenever the user re-analyzes a project (the most
//     common cause of new orphan generations appearing).

/// Per-project rebuild status surfaced to the wizard's poll loop.
///
/// `terminal` is a computed convenience flag — frontend just checks
/// `statuses.every(s => s.terminal)` to know when the loop can stop.
/// We compute it server-side so the lifecycle vocabulary stays
/// authoritative here even if the frontend gets ahead of a new status
/// value in a future release.
#[derive(Debug, Clone, Serialize)]
pub struct CodeGraphBuildStatusForProject {
    pub project_id: String,
    /// "pending" | "running" | "success" | "failed" | "skipped" | "missing".
    /// "missing" is synthesised when no `code_graph_builds` row exists
    /// for the project — the wizard handles this gracefully (the row
    /// is inserted by `rebuild_code_graph` so a missing row usually
    /// means the kickoff itself failed).
    pub status: String,
    /// Number of files analyzed so far. None when the row is missing.
    pub files_analyzed: Option<u32>,
    /// Last error message (only populated for `failed`).
    pub error_message: Option<String>,
    /// True when the lifecycle is no longer changing (success / failed /
    /// skipped / missing). The wizard stops polling once every row in
    /// the batch is terminal.
    pub terminal: bool,
}

/// True when the given lifecycle string is one the wizard treats as
/// "no further progress will come". Centralised here so the contract
/// stays in lock-step with `db::code_graph_builds::status`.
fn is_terminal_build_status(status: &str) -> bool {
    use crate::db::code_graph_builds::status as s;
    matches!(status, s::SUCCESS | s::FAILED | s::SKIPPED)
}

/// Batched read of `code_graph_builds` for the wizard's poll loop.
///
/// Always returns one entry per requested project_id, in the same
/// order. If a row is missing for a given project, the entry's
/// `status` is `"missing"` and `terminal` is `true` (so the wizard's
/// "all done?" check still works even when a kickoff failed before
/// inserting the row).
///
/// Soft-fail per project: a DB hiccup on one project doesn't block
/// the others — the affected entry gets `status="failed"` with the
/// SQL error in `error_message`.
#[command]
pub async fn get_code_graph_build_status_for_projects(
    project_ids: Vec<String>,
    db: State<'_, Db>,
) -> Result<Vec<CodeGraphBuildStatusForProject>, String> {
    let mut out = Vec::with_capacity(project_ids.len());
    for pid in &project_ids {
        match db.get_code_graph_build(pid) {
            Ok(Some(row)) => {
                let terminal = is_terminal_build_status(&row.status);
                out.push(CodeGraphBuildStatusForProject {
                    project_id: row.project_id,
                    status: row.status,
                    files_analyzed: Some(row.files_analyzed),
                    error_message: row.error_message,
                    terminal,
                });
            }
            Ok(None) => {
                // No row at all. Treat as terminal so the poll loop
                // doesn't spin forever; the UI will render "no build
                // recorded" for this entry.
                out.push(CodeGraphBuildStatusForProject {
                    project_id: pid.clone(),
                    status: "missing".to_string(),
                    files_analyzed: None,
                    error_message: None,
                    terminal: true,
                });
            }
            Err(e) => {
                // Per-project soft-fail: log + surface as failed so
                // the wizard shows the error to the user instead of
                // hanging.
                eprintln!(
                    "[vct] get_code_graph_build_status_for_projects: \
                     db lookup failed for {}: {}",
                    pid, e
                );
                out.push(CodeGraphBuildStatusForProject {
                    project_id: pid.clone(),
                    status: "failed".to_string(),
                    files_analyzed: None,
                    error_message: Some(e),
                    terminal: true,
                });
            }
        }
    }
    Ok(out)
}

/// Reset the legacy-collections-wizard dismissal flag so the next
/// launcher start re-fires the wizard. Backs the "Re-check for legacy
/// collections" button in Preferences (plan 0.9).
///
/// Distinct from `set_legacy_codegraph_notice_dismissed(false)` only
/// in intent — keeping the dedicated command name surfaces the
/// re-check semantics in the audit log so we can tell user-initiated
/// re-checks apart from the automatic reset triggered by
/// `rebuild_code_graph`.
#[command]
pub async fn force_recheck_legacy_codegraph(db: State<'_, Db>) -> Result<(), String> {
    db.app_state_set_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED, false)?;
    let _ = db.audit(
        "legacy_codegraph_wizard_force_recheck",
        None,
        None,
        &serde_json::json!({ "source": "preferences_button" }),
    );
    Ok(())
}

// ─── Internal helpers ────────────────────────────────────────────────────

// ─── PR-26 / Group E (v0.2.12 / 2026-05-16): shared KG picker ───────────
//
// Surfaces ALL orchestrator-shaped KG classes detected on Weaviate so the
// launcher's IdentityTab can present a partial-match picker. The detection
// itself mirrors `hub::cli_api::detect_orchestrator_kg_collections`
// byte-for-byte at the algorithm level (probe `/v1/schema`, keep classes
// whose properties include the four marker fields `title text` +
// `node_type text` + `tags text[]` + `typed_links object[]`). It is
// re-implemented here rather than re-exported because the cli_api helper
// is private and lifting it to `pub(crate)` would touch a file outside
// this PR's allowlist. Drift is monitored by the unit test
// `detects_orchestrator_shaped_classes_only`.
//
// Persistence: the picked name lands in the global app_state row
// `shared_kg.collection_name`. That key is the Priority-1 source
// consulted by `project_env_settings::populate`, so every project's
// `SHARED_KG_COLLECTION` env value picks up the new canonical on its
// next env-surface refresh (idempotent — value-identical writes are
// ~50 ms no-ops via the deep-merge env writer).
//
// Soft-fail: the picker is purely informational. If Weaviate is
// unreachable the command returns Ok(empty Vec) and the IdentityTab
// hides the picker button (no toast, no error).

const ORCHESTRATOR_KG_PICKER_MARKERS: &[&str] =
    &["title", "node_type", "tags", "typed_links"];

/// Marker name to expected Weaviate dataType[0] for the orchestrator-
/// shape check. Mirrors the closure in `hub::cli_api`.
fn marker_datatype_matches(marker: &str, dt: &str) -> bool {
    match marker {
        "title" | "node_type" => dt == "text",
        "tags" => dt == "text[]",
        "typed_links" => dt == "object[]",
        _ => false,
    }
}

/// Parse a Weaviate `/v1/schema` JSON body and return the names of
/// classes that have ALL orchestrator-shape marker properties. Sorted.
fn extract_orchestrator_shaped_classes(schema: &serde_json::Value) -> Vec<String> {
    let classes = match schema.get("classes").and_then(|c| c.as_array()) {
        Some(arr) => arr,
        None => return Vec::new(),
    };
    let mut out = Vec::new();
    for cls in classes {
        let name = match cls.get("class").and_then(|v| v.as_str()) {
            Some(s) if !s.is_empty() => s,
            _ => continue,
        };
        let props = match cls.get("properties").and_then(|p| p.as_array()) {
            Some(a) => a,
            None => continue,
        };
        let mut by_name: std::collections::HashMap<&str, &str> =
            std::collections::HashMap::new();
        for p in props {
            let pn = p.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let dt = p
                .get("dataType")
                .and_then(|v| v.as_array())
                .and_then(|a| a.first())
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if !pn.is_empty() {
                by_name.insert(pn, dt);
            }
        }
        let has_all = ORCHESTRATOR_KG_PICKER_MARKERS.iter().all(|m| {
            let dt = by_name.get(*m).copied().unwrap_or("");
            marker_datatype_matches(m, dt)
        });
        if has_all {
            out.push(name.to_string());
        }
    }
    out.sort();
    out
}

/// List every orchestrator-shaped KG class currently in Weaviate. Soft-fails
/// to an empty vec on any transport / parse failure — the IdentityTab
/// hides its picker button on empty.
#[command]
pub async fn list_orchestrator_kg_collections(
    cfg: State<'_, LocalConfig>,
) -> Result<Vec<String>, String> {
    let base = resolve_weaviate_url(&cfg);
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[vct] list_orchestrator_kg_collections http client: {}", e);
            return Ok(Vec::new());
        }
    };
    let resp = match client.get(format!("{}/v1/schema", base)).send().await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[vct] list_orchestrator_kg_collections schema fetch: {}", e);
            return Ok(Vec::new());
        }
    };
    if !resp.status().is_success() {
        eprintln!(
            "[vct] list_orchestrator_kg_collections: schema HTTP {}",
            resp.status().as_u16()
        );
        return Ok(Vec::new());
    }
    let schema: serde_json::Value = match resp.json().await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[vct] list_orchestrator_kg_collections parse: {}", e);
            return Ok(Vec::new());
        }
    };
    Ok(extract_orchestrator_shaped_classes(&schema))
}

/// Persist the user's pick from the SharedKgPicker as the canonical
/// shared KG class name (`app_state[shared_kg.collection_name]`).
///
/// Validation:
///   * `name` non-empty, length <= 100.
///   * Matches Weaviate class-name shape (letter-prefix, [A-Za-z0-9_]+).
///   * Ends with `_KnowledgeGraph` — the launcher's env writers + every
///     consumer in the codebase assume the shared KG suffix is this
///     literal string. Refusing other suffixes prevents users from
///     accidentally pointing the shared-KG knob at a code-graph or
///     development class.
///
/// Audit-logged via `db.audit("shared_kg_collection_name_set", ...)`.
#[command]
pub async fn set_shared_kg_collection_name(
    name: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err("shared KG name cannot be empty".into());
    }
    if trimmed.len() > 100 {
        return Err(format!(
            "shared KG name too long ({} chars; max 100)",
            trimmed.len()
        ));
    }
    if !is_valid_collection_name(trimmed) {
        return Err(format!(
            "shared KG name '{}' contains invalid characters \
             (allowed: A-Z a-z 0-9 _; must start with a letter)",
            trimmed
        ));
    }
    if !trimmed.ends_with("_KnowledgeGraph") {
        return Err(format!(
            "shared KG name '{}' must end with _KnowledgeGraph",
            trimmed
        ));
    }
    db.app_state_set(
        crate::commands::project_env_settings::APP_STATE_KEY_SHARED_KG_NAME,
        trimmed,
    )?;
    db.audit(
        "shared_kg_collection_name_set",
        None,
        None,
        &serde_json::json!({ "new_value": trimmed }),
    )
    .ok();
    Ok(())
}

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

    // ─── PR-26 / Group E: shared KG picker schema-shape detection ─────

    fn make_class(name: &str, props: &[(&str, &str)]) -> serde_json::Value {
        serde_json::json!({
            "class": name,
            "properties": props.iter().map(|(n, dt)| serde_json::json!({
                "name": n,
                "dataType": [dt],
            })).collect::<Vec<_>>()
        })
    }

    fn orchestrator_shape_props() -> Vec<(&'static str, &'static str)> {
        vec![
            ("title", "text"),
            ("node_type", "text"),
            ("tags", "text[]"),
            ("typed_links", "object[]"),
        ]
    }

    #[test]
    fn detects_orchestrator_shaped_classes_only() {
        let schema = serde_json::json!({
            "classes": [
                make_class("FooBar_KnowledgeGraph", &orchestrator_shape_props()),
                make_class("Acme_KnowledgeGraph", &orchestrator_shape_props()),
                // Missing typed_links → must be filtered out.
                make_class("ExampleProj_KnowledgeGraph", &[
                    ("title", "text"),
                    ("node_type", "text"),
                    ("tags", "text[]"),
                ]),
                // Code-graph class shape — wrong markers entirely.
                make_class("FooBar_CodeFunction", &[
                    ("name", "text"),
                    ("body", "text"),
                ]),
                // tags has wrong dataType → filtered.
                make_class("WrongTagShape_KnowledgeGraph", &[
                    ("title", "text"),
                    ("node_type", "text"),
                    ("tags", "text"),
                    ("typed_links", "object[]"),
                ]),
            ]
        });
        let detected = extract_orchestrator_shaped_classes(&schema);
        // Sorted alphabetically.
        assert_eq!(
            detected,
            vec![
                "Acme_KnowledgeGraph".to_string(),
                "FooBar_KnowledgeGraph".to_string(),
            ]
        );
    }

    #[test]
    fn detects_empty_when_no_classes_key() {
        let schema = serde_json::json!({});
        assert!(extract_orchestrator_shaped_classes(&schema).is_empty());
    }

    #[test]
    fn detects_empty_when_classes_is_empty_array() {
        let schema = serde_json::json!({ "classes": [] });
        assert!(extract_orchestrator_shaped_classes(&schema).is_empty());
    }

    #[test]
    fn detects_skips_classes_with_no_name() {
        let schema = serde_json::json!({
            "classes": [
                {
                    "class": "",
                    "properties": [
                        { "name": "title", "dataType": ["text"] },
                        { "name": "node_type", "dataType": ["text"] },
                        { "name": "tags", "dataType": ["text[]"] },
                        { "name": "typed_links", "dataType": ["object[]"] },
                    ]
                },
                make_class("FooBar_KnowledgeGraph", &orchestrator_shape_props()),
            ]
        });
        assert_eq!(
            extract_orchestrator_shaped_classes(&schema),
            vec!["FooBar_KnowledgeGraph".to_string()]
        );
    }

    #[test]
    fn marker_datatype_check_is_strict() {
        assert!(marker_datatype_matches("title", "text"));
        assert!(marker_datatype_matches("node_type", "text"));
        assert!(marker_datatype_matches("tags", "text[]"));
        assert!(marker_datatype_matches("typed_links", "object[]"));
        // Wrong type for any marker → false.
        assert!(!marker_datatype_matches("title", "text[]"));
        assert!(!marker_datatype_matches("tags", "text"));
        assert!(!marker_datatype_matches("typed_links", "object"));
        // Unknown marker → always false (defensive).
        assert!(!marker_datatype_matches("unknown_marker", "text"));
    }

    // ─── PR-26 / Group E: set_shared_kg_collection_name validation ────
    //
    // Pure validation tests for the helper logic. The async Tauri command
    // itself is exercised end-to-end in
    // `tests/test_project_identity_kg_picker.py` (Python integration).

    fn validate_shared_kg_name(name: &str) -> Result<String, String> {
        let trimmed = name.trim();
        if trimmed.is_empty() {
            return Err("shared KG name cannot be empty".into());
        }
        if trimmed.len() > 100 {
            return Err(format!(
                "shared KG name too long ({} chars; max 100)",
                trimmed.len()
            ));
        }
        if !is_valid_collection_name(trimmed) {
            return Err(format!(
                "shared KG name '{}' contains invalid characters",
                trimmed
            ));
        }
        if !trimmed.ends_with("_KnowledgeGraph") {
            return Err(format!(
                "shared KG name '{}' must end with _KnowledgeGraph",
                trimmed
            ));
        }
        Ok(trimmed.to_string())
    }

    #[test]
    fn shared_kg_name_accepts_canonical_shapes() {
        // Canonical v0.2.23 B1 capital-C casing.
        assert!(validate_shared_kg_name("VibeCodedOrchestrator_KnowledgeGraph").is_ok());
        // Legacy v0.2.12–v0.2.22 lowercase-c casing.
        assert!(validate_shared_kg_name("VibecodedOrchestrator_KnowledgeGraph").is_ok());
        assert!(validate_shared_kg_name("FooBar_KnowledgeGraph").is_ok());
        assert!(validate_shared_kg_name("Acme_KnowledgeGraph").is_ok());
        assert!(validate_shared_kg_name("  FooBar_KnowledgeGraph  ").is_ok());
    }

    #[test]
    fn shared_kg_name_rejects_invalid_shapes() {
        assert!(validate_shared_kg_name("").is_err());
        assert!(validate_shared_kg_name("   ").is_err());
        assert!(validate_shared_kg_name("FooBar_CodeFunction").is_err()); // wrong suffix
        assert!(validate_shared_kg_name("FooBar").is_err()); // no suffix at all
        assert!(validate_shared_kg_name("_LeadingUnderscore_KnowledgeGraph").is_err());
        assert!(validate_shared_kg_name("3StartsWithDigit_KnowledgeGraph").is_err());
        assert!(validate_shared_kg_name("has-dash_KnowledgeGraph").is_err());
        let too_long = format!("{}_KnowledgeGraph", "F".repeat(95));
        assert!(validate_shared_kg_name(&too_long).is_err());
    }

    // ─── v0.2.15 (0.4): orphan-detection helpers ─────────────────────────

    #[test]
    fn split_codegraph_class_name_handles_each_suffix() {
        for suffix in &["CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"] {
            let class = format!("MyProj_{}", suffix);
            let (prefix, got_suffix) = split_codegraph_class_name(&class)
                .unwrap_or_else(|| panic!("expected Some for {}", class));
            assert_eq!(prefix, "MyProj");
            assert_eq!(got_suffix, *suffix);
        }
    }

    #[test]
    fn split_codegraph_class_name_rejects_non_codegraph() {
        // KG class — not a code-graph suffix.
        assert!(split_codegraph_class_name("MyProj_KnowledgeGraph").is_none());
        // Development class.
        assert!(split_codegraph_class_name("MyProj_Development").is_none());
        // Bare name.
        assert!(split_codegraph_class_name("Foo").is_none());
        // Empty.
        assert!(split_codegraph_class_name("").is_none());
    }

    #[test]
    fn split_codegraph_class_name_preserves_underscored_prefix() {
        // SimRacing_AI's class name is SimRacing_AI_CodeFunction. The
        // splitter must return the FULL underscored prefix, not just
        // "AI" (which would be the wrong split if we used find('_')).
        let (prefix, suffix) = split_codegraph_class_name("SimRacing_AI_CodeFunction")
            .expect("must split correctly");
        assert_eq!(prefix, "SimRacing_AI");
        assert_eq!(suffix, "CodeFunction");
    }

    #[test]
    fn normalise_prefix_for_match_collapses_case_and_separators() {
        // All five forms a single project might have accumulated across
        // VCO releases must normalise to the same string.
        let canonical = normalise_prefix_for_match("VibeCodedOrchestrator");
        assert_eq!(canonical, "vibecodedorchestrator");
        assert_eq!(normalise_prefix_for_match("VibeCoded Orchestrator"), canonical);
        assert_eq!(normalise_prefix_for_match("vibecoded_orchestrator"), canonical);
        assert_eq!(normalise_prefix_for_match("VibeCoded_Orchestrator"), canonical);
        assert_eq!(normalise_prefix_for_match("Vibecodedorchestrator"), canonical);
        assert_eq!(normalise_prefix_for_match("vibecoded-orchestrator"), canonical);
    }

    #[test]
    fn normalise_prefix_for_match_distinguishes_genuinely_different_names() {
        // Ensure normalisation doesn't collapse different projects together.
        assert_ne!(
            normalise_prefix_for_match("SimRacing_AI"),
            normalise_prefix_for_match("SD15")
        );
        assert_ne!(
            normalise_prefix_for_match("VibeCodedOrchestrator"),
            normalise_prefix_for_match("ClaudeOrchestrator")
        );
        // SimRacing_AI and SimRacingAI normalise the same (the
        // underscore is informational only) — that's the intended
        // tolerance for project-rename matching.
        assert_eq!(
            normalise_prefix_for_match("SimRacing_AI"),
            normalise_prefix_for_match("SimRacingAI")
        );
    }

    #[test]
    fn normalise_prefix_for_match_handles_empty_and_unicode() {
        assert_eq!(normalise_prefix_for_match(""), "");
        // Non-ASCII chars get filtered out (no panics).
        assert_eq!(normalise_prefix_for_match("étude"), "tude");
        assert_eq!(normalise_prefix_for_match("___"), "");
        assert_eq!(normalise_prefix_for_match("123abc"), "123abc");
    }

    // ─── W3 / v0.2.16 (2026-05-18): wizard UX hardening ──────────────────
    //
    // Two surfaces:
    //   * `get_code_graph_build_status_for_projects` — batched lookup
    //     that maps `code_graph_builds` rows into the wizard's polling
    //     contract, synthesises a `missing` entry when no row exists,
    //     and computes `terminal` so the frontend stop-condition stays
    //     authoritative on the server.
    //   * `force_recheck_legacy_codegraph` — resets the dismissal flag
    //     so the next launcher start fires the wizard. Backs the
    //     Preferences "Re-check for legacy collections" button.

    use crate::db::code_graph_builds::status as build_status;
    use crate::db::models::ProjectHost;

    /// Platform-aware fake folder path. Tests only store this in the
    /// `folder_path` column for round-trip / uniqueness — they never
    /// touch disk — but `/tmp/x`-style paths are parsed ambiguously on
    /// Windows, so pick a host-appropriate fake. Mirrors the fixture
    /// in `db::code_graph_builds::tests`.
    fn w3_fixture_path(suffix: &str) -> String {
        if cfg!(windows) {
            format!(r"C:\tmp\{}", suffix)
        } else {
            format!("/tmp/{}", suffix)
        }
    }

    fn w3_fresh_db_with_project(label: &str) -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = uuid::Uuid::new_v4().to_string();
        let slug = db.generate_unique_slug(label).unwrap();
        let folder = w3_fixture_path(label);
        db.insert_project(&id, label, &folder, ProjectHost::Base, &slug)
            .unwrap();
        (db, id)
    }

    #[test]
    fn is_terminal_build_status_recognises_terminal_states() {
        assert!(is_terminal_build_status(build_status::SUCCESS));
        assert!(is_terminal_build_status(build_status::FAILED));
        assert!(is_terminal_build_status(build_status::SKIPPED));
    }

    #[test]
    fn is_terminal_build_status_treats_pending_and_running_as_non_terminal() {
        assert!(!is_terminal_build_status(build_status::PENDING));
        assert!(!is_terminal_build_status(build_status::RUNNING));
        // Unknown statuses must NOT be treated as terminal — better the
        // wizard polls one extra tick than gets stuck because a future
        // status string was added without updating this match.
        assert!(!is_terminal_build_status("queued"));
        assert!(!is_terminal_build_status(""));
    }

    /// Build the per-project status mapping the Tauri command exposes,
    /// without going through the `State<'_, Db>` wrapper. Mirrors what
    /// the command body does so the unit test stays focused on the
    /// mapping logic (Tauri's State wrapper has its own integration
    /// tests elsewhere). The shape returned MUST match
    /// `get_code_graph_build_status_for_projects` byte-for-byte.
    fn build_statuses_for_test(
        db: &Db,
        project_ids: &[String],
    ) -> Vec<CodeGraphBuildStatusForProject> {
        let mut out = Vec::with_capacity(project_ids.len());
        for pid in project_ids {
            match db.get_code_graph_build(pid) {
                Ok(Some(row)) => {
                    let terminal = is_terminal_build_status(&row.status);
                    out.push(CodeGraphBuildStatusForProject {
                        project_id: row.project_id,
                        status: row.status,
                        files_analyzed: Some(row.files_analyzed),
                        error_message: row.error_message,
                        terminal,
                    });
                }
                Ok(None) => out.push(CodeGraphBuildStatusForProject {
                    project_id: pid.clone(),
                    status: "missing".to_string(),
                    files_analyzed: None,
                    error_message: None,
                    terminal: true,
                }),
                Err(e) => out.push(CodeGraphBuildStatusForProject {
                    project_id: pid.clone(),
                    status: "failed".to_string(),
                    files_analyzed: None,
                    error_message: Some(e),
                    terminal: true,
                }),
            }
        }
        out
    }

    #[test]
    fn build_status_maps_success_row_with_files_analyzed() {
        let (db, pid) = w3_fresh_db_with_project("ProjA");
        db.upsert_code_graph_build(
            &pid,
            build_status::SUCCESS,
            Some(1000),
            Some(2500),
            Some(1500),
            42,
            None,
            false,
            None,
            None,
        )
        .unwrap();

        let got = build_statuses_for_test(&db, &[pid.clone()]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].project_id, pid);
        assert_eq!(got[0].status, "success");
        assert_eq!(got[0].files_analyzed, Some(42));
        assert_eq!(got[0].error_message, None);
        assert!(got[0].terminal, "success must be terminal");
    }

    #[test]
    fn build_status_maps_running_row_as_non_terminal() {
        let (db, pid) = w3_fresh_db_with_project("ProjRun");
        db.upsert_code_graph_build(
            &pid,
            build_status::RUNNING,
            Some(1000),
            None,
            None,
            17,
            None,
            false,
            None,
            None,
        )
        .unwrap();

        let got = build_statuses_for_test(&db, &[pid.clone()]);
        assert_eq!(got[0].status, "running");
        assert_eq!(got[0].files_analyzed, Some(17));
        assert!(!got[0].terminal, "running must NOT be terminal");
    }

    #[test]
    fn build_status_synthesises_missing_for_unknown_project_id() {
        let db = Db::open_in_memory().expect("in-memory db");
        let got = build_statuses_for_test(&db, &["nonexistent".to_string()]);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].project_id, "nonexistent");
        assert_eq!(got[0].status, "missing");
        assert_eq!(got[0].files_analyzed, None);
        assert!(
            got[0].terminal,
            "missing rows must be terminal so the wizard's poll loop can stop"
        );
    }

    #[test]
    fn build_status_failed_row_carries_error_message() {
        let (db, pid) = w3_fresh_db_with_project("ProjErr");
        db.upsert_code_graph_build(
            &pid,
            build_status::FAILED,
            Some(1),
            Some(5),
            Some(4),
            3,
            None,
            false,
            Some("analyzer exit 4"),
            None,
        )
        .unwrap();

        let got = build_statuses_for_test(&db, &[pid.clone()]);
        assert_eq!(got[0].status, "failed");
        assert_eq!(got[0].files_analyzed, Some(3));
        assert_eq!(got[0].error_message.as_deref(), Some("analyzer exit 4"));
        assert!(got[0].terminal);
    }

    #[test]
    fn build_status_preserves_order_of_requested_ids() {
        let db = Db::open_in_memory().expect("in-memory db");
        // Seed three projects; assert the returned vec matches the
        // requested order even though the DB doesn't guarantee any
        // particular ordering.
        let mut ids = Vec::new();
        for (i, name) in ["Alpha", "Beta", "Gamma"].iter().enumerate() {
            let id = uuid::Uuid::new_v4().to_string();
            let slug = db.generate_unique_slug(name).unwrap();
            db.insert_project(
                &id,
                name,
                &w3_fixture_path(&format!("order-{}", i)),
                ProjectHost::Base,
                &slug,
            )
            .unwrap();
            db.upsert_code_graph_build(
                &id,
                build_status::PENDING,
                Some(0),
                None,
                None,
                0,
                None,
                false,
                None,
                None,
            )
            .unwrap();
            ids.push(id);
        }

        // Request reversed order — output must follow input order.
        let reversed: Vec<String> = ids.iter().rev().cloned().collect();
        let got = build_statuses_for_test(&db, &reversed);
        assert_eq!(got.len(), 3);
        for (i, expected_id) in reversed.iter().enumerate() {
            assert_eq!(&got[i].project_id, expected_id);
            assert_eq!(got[i].status, "pending");
        }
    }

    #[test]
    fn build_status_mixed_existing_and_missing_ids() {
        let (db, real_pid) = w3_fresh_db_with_project("Real");
        db.upsert_code_graph_build(
            &real_pid,
            build_status::SUCCESS,
            Some(1),
            Some(2),
            Some(1),
            5,
            None,
            false,
            None,
            None,
        )
        .unwrap();

        let ids = vec![real_pid.clone(), "ghost".to_string()];
        let got = build_statuses_for_test(&db, &ids);
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].status, "success");
        assert_eq!(got[0].files_analyzed, Some(5));
        assert_eq!(got[1].status, "missing");
        assert!(got[1].terminal);
    }

    // ─── force_recheck_legacy_codegraph ─────────────────────────────────

    #[test]
    fn force_recheck_resets_dismissed_flag() {
        let db = Db::open_in_memory().expect("in-memory db");
        // Pre-condition: set dismissed=true the way the wizard does.
        db.app_state_set_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED, true)
            .unwrap();
        assert_eq!(
            db.app_state_get_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED)
                .unwrap(),
            Some(true)
        );

        // Mirrors what `force_recheck_legacy_codegraph` does
        // server-side (without the Tauri State wrapper).
        db.app_state_set_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED, false)
            .unwrap();

        assert_eq!(
            db.app_state_get_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED)
                .unwrap(),
            Some(false),
            "Re-check must reset the dismissal flag so the next launcher start \
             re-fires the wizard"
        );
    }

    #[test]
    fn force_recheck_idempotent_when_already_false() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.app_state_set_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED, false)
            .unwrap();
        db.app_state_set_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED, false)
            .unwrap();
        assert_eq!(
            db.app_state_get_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED)
                .unwrap(),
            Some(false)
        );
    }

    #[test]
    fn force_recheck_works_when_flag_was_never_set() {
        // If the wizard has never been dismissed, the row simply doesn't
        // exist — `app_state_get_bool` returns None. `force_recheck`
        // should still succeed and explicitly write `false` (so future
        // reads see a deliberate Some(false) rather than None).
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(
            db.app_state_get_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED)
                .unwrap(),
            None
        );

        db.app_state_set_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED, false)
            .unwrap();

        assert_eq!(
            db.app_state_get_bool(APP_STATE_KEY_LEGACY_NOTICE_DISMISSED)
                .unwrap(),
            Some(false)
        );
    }
}
