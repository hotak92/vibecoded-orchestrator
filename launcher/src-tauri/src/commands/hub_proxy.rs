//! Thin Tauri wrappers around the Hub axum server. The hub itself is bound
//! to localhost only and writes its port to `~/.vct/hub.port`. These
//! commands let the launcher GUI read the hub without re-implementing
//! HTTP discovery in JS.

use serde::Serialize;
use serde_json::Value;
use tauri::command;

fn hub_port() -> Result<u16, String> {
    let path = crate::paths::vct_root_dir().join("hub.port");
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("read hub.port: {}", e))?;
    raw.trim()
        .parse::<u16>()
        .map_err(|e| format!("parse hub.port: {}", e))
}

fn hub_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
        .map_err(|e| format!("http client: {}", e))
}

async fn hub_get(path: &str) -> Result<Value, String> {
    let port = hub_port()?;
    let client = hub_client()?;
    let url = format!("http://127.0.0.1:{}/api/v1{}", port, path);
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("hub GET {}: {}", path, e))?;
    if !resp.status().is_success() {
        return Err(format!("hub returned {}", resp.status().as_u16()));
    }
    resp.json::<Value>()
        .await
        .map_err(|e| format!("parse: {}", e))
}

#[derive(Debug, Serialize)]
pub struct HubInfo {
    pub port: u16,
    pub reachable: bool,
}

#[command]
pub async fn hub_info() -> Result<HubInfo, String> {
    let port = hub_port()?;
    let client = hub_client()?;
    let reachable = client
        .get(format!("http://127.0.0.1:{}/api/v1/health", port))
        .send()
        .await
        .map(|r| r.status().is_success())
        .unwrap_or(false);
    Ok(HubInfo { port, reachable })
}

#[command]
pub async fn hub_list_apps() -> Result<Value, String> {
    hub_get("/apps").await
}

#[command]
pub async fn hub_poll_messages(recipient: String) -> Result<Value, String> {
    hub_get(&format!("/messages/{}", recipient)).await
}

#[command]
pub async fn hub_data_catalog() -> Result<Value, String> {
    hub_get("/data/catalog").await
}
