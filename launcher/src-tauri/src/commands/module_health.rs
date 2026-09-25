// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! `module_health_snapshot` — the GUI's read of the hub's module health
//! (v0.2.97, lane V).
//!
//! The hub polls every active module's `runtime.health_check`
//! (`vct_hub::module_health`) and serves the result on
//! `GET /api/v1/modules/catalog` (`health`, `project_health` per entry). This
//! command fetches that route and returns only the health part, keyed by
//! module id, for the module tiles (`$lib/module-health.ts`). The hub is the
//! one prober; the launcher never probes a module itself.
//!
//! Not tier-gated: module status is part of the free Modules page, unlike the
//! `/hub` route's commands in `hub_proxy`.

use std::collections::BTreeMap;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;
use tauri::command;

/// One module's health as the tiles read it: the machine-wide instance and
/// each project's (keys are project ids).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModuleHealthView {
    pub health: Value,
    pub project_health: BTreeMap<String, Value>,
}

/// The health part of a `/modules/catalog` body, by module id. Entries
/// without a health block (no `runtime.health_check`, or not active here) are
/// left out; the tiles show nothing for them.
pub fn health_by_module(catalog: &Value) -> BTreeMap<String, ModuleHealthView> {
    let mut out = BTreeMap::new();
    let Some(entries) = catalog.get("modules").and_then(Value::as_array) else {
        return out;
    };
    for entry in entries {
        let Some(id) = entry.get("id").and_then(Value::as_str) else { continue };
        let health = entry.get("health").cloned().unwrap_or(Value::Null);
        let project_health: BTreeMap<String, Value> = entry
            .get("project_health")
            .and_then(Value::as_object)
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default();
        if health.is_null() && project_health.is_empty() {
            continue;
        }
        out.insert(id.to_string(), ModuleHealthView { health, project_health });
    }
    out
}

/// Health of every module the hub probes. `Err` when the hub cannot be read
/// (not running, no token) — the tiles then show "unknown", never "down".
#[command]
pub async fn module_health_snapshot() -> Result<BTreeMap<String, ModuleHealthView>, String> {
    let port = vct_launcher_core::services::hub_port::read_hub_port_file()?;
    let token = vct_launcher_core::services::boot_token::read_nonempty_token_file(
        &crate::paths::vct_root_dir().join("hub.token"),
    )?;
    fetch_snapshot(&snapshot_client()?, port, &token).await
}

/// The client for the snapshot read. `.no_proxy()`: the request carries the
/// hub bearer token to 127.0.0.1, and with `HTTP_PROXY` set (and no
/// `NO_PROXY`) reqwest's environment proxy would otherwise send it — token
/// included — to the proxy (R7b F19).
fn snapshot_client() -> Result<reqwest::Client, String> {
    vct_launcher_core::services::loopback_http::client(Duration::from_secs(5))
}

async fn fetch_snapshot(
    client: &reqwest::Client,
    port: u16,
    token: &str,
) -> Result<BTreeMap<String, ModuleHealthView>, String> {
    let resp = client
        .get(format!("http://127.0.0.1:{port}/api/v1/modules/catalog"))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| format!("hub GET /modules/catalog: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("hub returned {}", resp.status().as_u16()));
    }
    let body: Value = resp.json().await.map_err(|e| format!("parse hub catalog: {e}"))?;
    Ok(health_by_module(&body))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// R7b F19: with `HTTP_PROXY` pointing somewhere (here: a port nothing
    /// listens on), the snapshot still goes straight to the hub on 127.0.0.1
    /// — the bearer token never reaches a proxy.
    #[tokio::test]
    async fn the_snapshot_read_never_goes_through_a_proxy() {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let app = axum::Router::new().route(
            "/api/v1/modules/catalog",
            axum::routing::get(|| async {
                axum::Json(json!({ "modules": [{ "id": "m", "health": { "state": "up" } }] }))
            }),
        );
        tokio::spawn(async move { axum::serve(listener, app).await.ok() });

        let client = {
            let _env = vct_launcher_core::test_env::env_guard(&[
                ("HTTP_PROXY", Some("http://127.0.0.1:9")),
                ("http_proxy", Some("http://127.0.0.1:9")),
                ("ALL_PROXY", Some("http://127.0.0.1:9")),
                ("NO_PROXY", None),
                ("no_proxy", None),
            ]);
            snapshot_client().unwrap()
        };
        let got = fetch_snapshot(&client, port, "test-token").await.expect("direct to the hub");
        assert_eq!(got["m"].health, json!({ "state": "up" }));
    }

    #[test]
    fn keeps_only_modules_with_health_and_carries_project_instances() {
        let body = json!({ "modules": [
            { "id": "vct-hub-api", "health": { "state": "up" } },
            { "id": "vct-search", "health": null },
            { "id": "vct-rl-reranker", "health": null,
              "project_health": { "p1": { "state": "down", "last_error": "HTTP 503" } } },
            { "name": "no id" }
        ]});
        let got = health_by_module(&body);
        assert_eq!(got.keys().collect::<Vec<_>>(), vec!["vct-hub-api", "vct-rl-reranker"]);
        assert_eq!(got["vct-hub-api"].health, json!({ "state": "up" }));
        assert!(got["vct-hub-api"].project_health.is_empty());
        assert_eq!(got["vct-rl-reranker"].project_health["p1"]["state"], "down");
        assert!(health_by_module(&json!({})).is_empty());
    }
}
