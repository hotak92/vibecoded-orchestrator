use crate::state::AppManager;
use crate::types::ServiceEntry;
use std::collections::HashMap;

/// Path: ~/.vct/services.json
pub fn registry_path() -> std::path::PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("services.json"))
        .unwrap_or_else(|| std::path::PathBuf::from(".vct/services.json"))
}

/// Load last-known service states from disk (called at startup).
/// Only restores metadata -- does NOT re-spawn processes.
/// Processes that were "running" at last shutdown are marked "stopped".
pub fn load_service_registry() -> HashMap<String, ServiceEntry> {
    let path = registry_path();
    if !path.exists() {
        return HashMap::new();
    }

    let data = std::fs::read_to_string(&path).unwrap_or_default();
    let entries: HashMap<String, ServiceEntry> =
        serde_json::from_str(&data).unwrap_or_default();

    // Reset transient states
    entries
        .into_iter()
        .map(|(k, mut v)| {
            if v.status == crate::types::AppStatus::Running
                || v.status == crate::types::AppStatus::Starting
                || v.status == crate::types::AppStatus::Downloading
                || v.status == crate::types::AppStatus::Installing
            {
                v.status = crate::types::AppStatus::Stopped;
                v.pid = None;
            }
            (k, v)
        })
        .collect()
}

/// Serialize current ServiceEntry map to ~/.vct/services.json.
///
/// Currently unused: the only caller was the archived app-process Tauri
/// command suite (launch_app/kill_app/etc.). Kept available for if/when
/// those commands are restored from the orchestrator's private
/// launch-assets/launcher-archived-rust/lifecycle_app_process.rs.
#[allow(dead_code)]
pub async fn persist_service_registry(state: &AppManager) -> Result<(), String> {
    let entries: HashMap<String, ServiceEntry> = {
        let guard = state.0.lock().unwrap();
        guard
            .iter()
            .map(|(k, v)| (k.clone(), v.entry.clone()))
            .collect()
    };

    let path = registry_path();
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }

    let json = serde_json::to_string_pretty(&entries)
        .map_err(|e| format!("Serialize registry: {}", e))?;
    tokio::fs::write(&path, json)
        .await
        .map_err(|e| format!("Write registry: {}", e))?;

    Ok(())
}
