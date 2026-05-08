//! Thin Tauri wrappers around the Hub axum server. The hub itself is bound
//! to localhost only and writes its port to `~/.vct/hub.port`. These
//! commands let the launcher GUI read the hub without re-implementing
//! HTTP discovery in JS.
//!
//! ─── Auth (H5, 2026-05-08) ──────────────────────────────────────────
//!
//! Every authenticated call reads `<vct_root_dir>/hub.token` fresh and
//! sends `Authorization: Bearer <token>`. We don't cache the token in
//! a static — re-reading from disk costs ~µs and lets a hub restart
//! (which rotates the token) propagate transparently to the next
//! call. Health endpoint stays unauthenticated (matches hub.rs's
//! exempt list).

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

/// Read the per-startup auth token written by `hub::auth::write_token_file`.
///
/// Returns Err if the file is missing — same exit semantics as
/// `hub_port()` (the hub isn't fully up; treat both as "hub
/// unreachable" upstream so callers don't have to differentiate).
fn hub_token() -> Result<String, String> {
    let path = crate::paths::vct_root_dir().join("hub.token");
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("read hub.token: {}", e))?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(format!("hub.token at {} is empty", path.display()));
    }
    Ok(trimmed.to_string())
}

fn hub_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
        .map_err(|e| format!("http client: {}", e))
}

async fn hub_get(path: &str) -> Result<Value, String> {
    let port = hub_port()?;
    let token = hub_token()?;
    let client = hub_client()?;
    let url = format!("http://127.0.0.1:{}/api/v1{}", port, path);
    let resp = client
        .get(&url)
        .bearer_auth(&token)
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
    // Health endpoint is unauthenticated (see hub::auth::is_exempt_path).
    // We deliberately skip the token read here — `hub_info` is the
    // probe used to decide whether the hub is up at all, and a stale
    // hub.token shouldn't make the GUI think the hub is down.
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
