use tauri::{command, Emitter, State};
use uuid::Uuid;

use crate::state::ProjectStore;
use crate::types::{CreateProjectRequest, Project, UpdateProjectRequest};

/// Create a new project. Creates workspace directories on disk.
#[command]
pub async fn create_project(
    req: CreateProjectRequest,
    state: State<'_, ProjectStore>,
) -> Result<Project, String> {
    let id = Uuid::new_v4().to_string();
    let now = chrono::Utc::now().to_rfc3339();

    tokio::fs::create_dir_all(&req.local_path)
        .await
        .map_err(|e| format!("Cannot create project dir: {}", e))?;

    let project = Project {
        id: id.clone(),
        name: req.name,
        local_path: req.local_path.clone(),
        apps: req.app_ids.clone(),
        config: req.config.unwrap_or(serde_json::json!({})),
        created_at: now.clone(),
        updated_at: now,
        synced_to_cloud: false,
    };

    create_workspace_dirs(&req.local_path, &req.app_ids).await?;
    write_project_json(&req.local_path, &project).await?;

    {
        let mut guard = state.0.lock().unwrap();
        guard.projects.insert(id.clone(), project.clone());
    } // guard dropped

    persist_projects(&state).await?;

    Ok(project)
}

/// List all known projects.
#[command]
pub fn get_projects(state: State<'_, ProjectStore>) -> Vec<Project> {
    let guard = state.0.lock().unwrap();
    guard.projects.values().cloned().collect()
}

/// Update project name, app list, or config.
#[command]
pub async fn update_project(
    id: String,
    req: UpdateProjectRequest,
    state: State<'_, ProjectStore>,
) -> Result<Project, String> {
    let project_clone = {
        let mut guard = state.0.lock().unwrap();
        let project = guard
            .projects
            .get_mut(&id)
            .ok_or_else(|| format!("Project '{}' not found", id))?;

        if let Some(name) = req.name {
            project.name = name;
        }
        if let Some(apps) = req.app_ids {
            project.apps = apps;
        }
        if let Some(config) = req.config {
            project.config = config;
        }
        project.updated_at = chrono::Utc::now().to_rfc3339();
        project.clone()
    }; // guard dropped

    create_workspace_dirs(&project_clone.local_path, &project_clone.apps).await?;
    write_project_json(&project_clone.local_path, &project_clone).await?;
    persist_projects(&state).await?;

    Ok(project_clone)
}

/// Set a project as active. Returns list of app_ids that need launching.
#[command]
pub async fn open_project(
    id: String,
    project_state: State<'_, ProjectStore>,
    app_state: State<'_, crate::state::AppManager>,
    window: tauri::Window,
) -> Result<Vec<String>, String> {
    let project = {
        let guard = project_state.0.lock().unwrap();
        guard
            .projects
            .get(&id)
            .cloned()
            .ok_or_else(|| format!("Project '{}' not found", id))?
    };

    {
        let mut guard = project_state.0.lock().unwrap();
        guard.active_project = Some(id.clone());
    }

    let running: std::collections::HashSet<String> = {
        let guard = app_state.0.lock().unwrap();
        guard.keys().cloned().collect()
    };

    let need_launch: Vec<String> = project
        .apps
        .iter()
        .filter(|id| !running.contains(*id))
        .cloned()
        .collect();

    let _ = window.emit(
        "project_opened",
        serde_json::json!({
            "project_id": id,
            "apps_to_launch": need_launch,
        }),
    );

    Ok(need_launch)
}

/// Close an active project. Kills apps exclusively assigned to it.
#[command]
pub async fn close_project(
    id: String,
    project_state: State<'_, ProjectStore>,
    app_state: State<'_, crate::state::AppManager>,
    window: tauri::Window,
) -> Result<(), String> {
    let project = {
        let guard = project_state.0.lock().unwrap();
        guard
            .projects
            .get(&id)
            .cloned()
            .ok_or_else(|| format!("Project '{}' not found", id))?
    };

    // Find apps only used by THIS project
    let other_project_apps: std::collections::HashSet<String> = {
        let guard = project_state.0.lock().unwrap();
        guard
            .projects
            .iter()
            .filter(|(pid, _)| **pid != id)
            .flat_map(|(_, p)| p.apps.clone())
            .collect()
    };

    let to_kill: Vec<String> = project
        .apps
        .iter()
        .filter(|app| !other_project_apps.contains(*app))
        .cloned()
        .collect();

    for app_id in &to_kill {
        let mut guard = app_state.0.lock().unwrap();
        if let Some(proc) = guard.get_mut(app_id) {
            proc.child.kill().ok();
            let _ = proc.child.wait();
            proc.entry.status = crate::types::AppStatus::Stopped;
            proc.entry.pid = None;
        }
    }

    {
        let mut guard = project_state.0.lock().unwrap();
        if guard.active_project.as_deref() == Some(&id) {
            guard.active_project = None;
        }
    }

    let _ = window.emit(
        "project_closed",
        serde_json::json!({
            "project_id": id,
            "apps_killed": to_kill,
        }),
    );

    Ok(())
}

// --- Helpers ---

async fn create_workspace_dirs(base: &str, app_ids: &[String]) -> Result<(), String> {
    let vct_path = format!("{}/.vct", base);
    let shared_subdirs = ["audio", "documents", "images", "presentations"];

    for sub in &shared_subdirs {
        tokio::fs::create_dir_all(format!("{}/shared/{}", vct_path, sub))
            .await
            .map_err(|e| format!("Cannot create shared/{}: {}", sub, e))?;
    }

    for app_id in app_ids {
        tokio::fs::create_dir_all(format!("{}/apps/{}", vct_path, app_id))
            .await
            .map_err(|e| format!("Cannot create apps/{}: {}", app_id, e))?;

        let config_path = format!("{}/apps/{}/config.json", vct_path, app_id);
        if !std::path::Path::new(&config_path).exists() {
            tokio::fs::write(&config_path, b"{}").await.ok();
        }
    }

    Ok(())
}

async fn write_project_json(base: &str, project: &Project) -> Result<(), String> {
    let path = format!("{}/.vct/project.json", base);
    let json =
        serde_json::to_string_pretty(project).map_err(|e| format!("Serialize error: {}", e))?;
    tokio::fs::write(&path, json)
        .await
        .map_err(|e| format!("Write project.json error: {}", e))?;
    Ok(())
}

/// Persist the projects index to ~/.vct/projects.json
async fn persist_projects(state: &ProjectStore) -> Result<(), String> {
    let projects: std::collections::HashMap<String, Project> = {
        let guard = state.0.lock().unwrap();
        guard.projects.clone()
    };

    let path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("projects.json"))
        .unwrap_or_else(|| ".vct/projects.json".into());

    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }

    let json =
        serde_json::to_string_pretty(&projects).map_err(|e| format!("Serialize projects: {}", e))?;
    tokio::fs::write(&path, json)
        .await
        .map_err(|e| format!("Write projects.json: {}", e))?;

    Ok(())
}
