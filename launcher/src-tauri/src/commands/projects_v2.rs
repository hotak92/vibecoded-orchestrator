//! Project lifecycle commands for the module system.
//!
//! Runs alongside the legacy `commands::projects` module during migration.
//! The "_v2" suffix marks the DB-backed implementation; once the React UI
//! is fully migrated to call these, we'll retire the old commands.

use serde::{Deserialize, Serialize};
use std::path::Path;
use tauri::{command, State};
use uuid::Uuid;

use crate::db::models::{ModuleInstallRow, ProjectHost, ProjectRow};
use crate::db::Db;

#[derive(Debug, Clone, Serialize)]
pub struct ProjectView {
    pub id: String,
    pub name: String,
    pub folder_path: String,
    pub host: ProjectHost,
    pub slug: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub module_count: u32,
}

impl ProjectView {
    fn from_row(row: ProjectRow, module_count: u32) -> Self {
        Self {
            id: row.id,
            name: row.name,
            folder_path: row.folder_path,
            host: row.host,
            slug: row.slug,
            created_at: row.created_at,
            updated_at: row.updated_at,
            module_count,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SwitchHostResult {
    pub project: ProjectView,
    pub modules_removed: Vec<ModuleInstallRow>,
    pub modules_preserved: Vec<ModuleInstallRow>,
}

#[command]
pub async fn list_projects_v2(db: State<'_, Db>) -> Result<Vec<ProjectView>, String> {
    let rows = db.list_projects()?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
        out.push(ProjectView::from_row(row, count));
    }
    Ok(out)
}

#[command]
pub async fn get_project_v2(
    id: String,
    db: State<'_, Db>,
) -> Result<Option<ProjectView>, String> {
    let row = match db.get_project(&id)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
    Ok(Some(ProjectView::from_row(row, count)))
}

/// Look up a project by its URL slug (e.g. `acme-corp`). Backs the
/// `/p/<slug>/...` routes.
#[command]
pub async fn get_project_by_slug(
    slug: String,
    db: State<'_, Db>,
) -> Result<Option<ProjectView>, String> {
    let row = match db.get_project_by_slug(&slug)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
    Ok(Some(ProjectView::from_row(row, count)))
}

#[derive(Debug, Deserialize)]
pub struct CreateProjectV2Request {
    pub name: String,
    pub folder_path: String,
    pub host: ProjectHost,
}

#[command]
pub async fn create_project_v2(
    req: CreateProjectV2Request,
    db: State<'_, Db>,
) -> Result<ProjectView, String> {
    let folder = Path::new(&req.folder_path);

    // Bug 3e: auto-create the folder if it doesn't exist. Earlier the
    // create flow rejected non-existent paths and forced the user to
    // `mkdir -p` manually, which broke when users typed a fresh path
    // in the New Project modal. `create_dir_all` is a no-op if the
    // path already exists.
    if !folder.exists() {
        std::fs::create_dir_all(folder).map_err(|e| {
            format!("cannot create folder {}: {}", req.folder_path, e)
        })?;
    }
    if !folder.is_dir() {
        return Err(format!("not a directory: {}", req.folder_path));
    }

    let id = Uuid::new_v4().to_string();
    let slug = db.generate_unique_slug(&req.name)?;
    let row = db.insert_project(&id, &req.name, &req.folder_path, req.host.clone(), &slug)?;
    db.audit(
        "project_create",
        Some(&row.id),
        None,
        &serde_json::json!({ "host": req.host.as_str(), "name": req.name, "slug": slug }),
    )?;
    let _ = db.log_change("projects", "insert", Some(&row.id), Some(&row.id));
    Ok(ProjectView::from_row(row, 0))
}

#[command]
pub async fn rename_project_v2(
    id: String,
    new_name: String,
    db: State<'_, Db>,
) -> Result<ProjectView, String> {
    // Generate a fresh slug derived from the new name so URLs track
    // renames. The old slug becomes invalid; existing bookmarks 404
    // gracefully via the /p/[slug] resolver. Documented in
    // docs/MULTI_TENANT_URLS.md.
    let new_slug = db.generate_unique_slug(&new_name)?;
    db.rename_project(&id, &new_name, Some(&new_slug))?;
    let row = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} not found after rename", id))?;
    let count = db.list_module_installs_for_project(&id)?.len() as u32;
    let _ = db.log_change("projects", "update", Some(&id), Some(&id));
    Ok(ProjectView::from_row(row, count))
}

#[command]
pub async fn switch_project_host_v2(
    id: String,
    new_host: ProjectHost,
    db: State<'_, Db>,
) -> Result<SwitchHostResult, String> {
    let project = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} not found", id))?;

    if project.host == new_host {
        let count = db.list_module_installs_for_project(&id)?.len() as u32;
        return Ok(SwitchHostResult {
            project: ProjectView::from_row(project, count),
            modules_removed: vec![],
            modules_preserved: db.list_module_installs_for_project(&id)?,
        });
    }

    // For MAO→base: modules listing compatible hosts with only "mao" must go.
    // We can't fully decide without the manifests, which live in install
    // directories. This command flags candidates for removal by looking at
    // the module_id. A manifest registry lookup would be cleaner — added
    // in a later iteration; for now we rely on the module_id naming
    // convention (*-mao suffix OR known MAO-only module ids).
    let installs = db.list_module_installs_for_project(&id)?;
    let mao_only_ids: &[&str] = &[
        "vct-asset-library",
        "vct-agent-packs-mao",
        "vct-workflows-mao",
    ];

    let mut removed = Vec::new();
    let mut preserved = Vec::new();
    for install in installs {
        let goes = new_host == ProjectHost::Base
            && (mao_only_ids.contains(&install.module_id.as_str())
                || install.module_id.ends_with("-mao"));
        if goes {
            db.delete_module_install(&id, &install.module_id)?;
            removed.push(install);
        } else {
            preserved.push(install);
        }
    }

    db.update_project_host(&id, new_host.clone())?;
    db.audit(
        "project_host_switch",
        Some(&id),
        None,
        &serde_json::json!({
            "to": new_host.as_str(),
            "removed_modules": removed.iter().map(|m| &m.module_id).collect::<Vec<_>>(),
        }),
    )?;
    let _ = db.log_change("projects", "update", Some(&id), Some(&id));

    let project = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} vanished after host switch", id))?;
    let count = preserved.len() as u32;
    Ok(SwitchHostResult {
        project: ProjectView::from_row(project, count),
        modules_removed: removed,
        modules_preserved: preserved,
    })
}

#[command]
pub async fn delete_project_v2(
    id: String,
    _delete_folder: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Note: delete_folder is accepted for UI parity with the design spec,
    // but we don't touch the user's folder on disk. Modules installed
    // under ~/.vct/modules/ are removed via CASCADE through
    // module_installs. The user's project folder on disk stays.
    db.audit("project_delete", Some(&id), None, &serde_json::json!({}))?;
    db.delete_project(&id)?;
    let _ = db.log_change("projects", "delete", Some(&id), Some(&id));
    Ok(())
}
