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
//!
//! ─── Tier gate (v0.2.91, P2-B4 / plan decision #28) ──────────────────
//!
//! `/hub` is the second of the two `proOnly` routes in `Sidebar.svelte`,
//! and pre-v0.2.91 that flag gated only the sidebar *link* — a free-tier
//! user reaching the route by typed URL got the whole cross-app surface.
//! The FOUR commands the `/hub` page invokes (`hub_info`,
//! `hub_list_apps`, `hub_data_catalog`, `hub_poll_messages` — the
//! route's complete command surface, verified by grep across
//! `launcher/src`) now re-check the cached orchestrator tier through the
//! shared `dashboard::require_tier` gate.
//!
//! Deliberately NOT gated: `get_hub_boot_autostart` /
//! `set_hub_boot_autostart`. Those back the **Preferences** toggle,
//! which is not a Pro surface — gating them would break the free tier's
//! ability to manage the hub service it actually runs. The gate follows
//! the ROUTE, not the module.

use serde::Serialize;
use serde_json::Value;
use tauri::{command, State};

use crate::commands::dashboard::require_tier;
use crate::db::Db;

/// Minimum orchestrator tier for the `/hub` route's command surface.
/// Mirrors `Sidebar.svelte`'s `proOnly` flag on `/hub`; keep the two in
/// step (`tests/test_v0291_pro_route_enforcement.py` pins it).
const MIN_TIER: &str = "pro";

/// Feature noun-phrase for the refusal copy (`tier_required_message`).
const FEATURE: &str = "The Orchestrator Hub";

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
pub async fn hub_info(db: State<'_, Db>) -> Result<HubInfo, String> {
    require_tier(&db, MIN_TIER, FEATURE)?;
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
pub async fn hub_list_apps(db: State<'_, Db>) -> Result<Value, String> {
    require_tier(&db, MIN_TIER, FEATURE)?;
    hub_get("/apps").await
}

#[command]
pub async fn hub_poll_messages(recipient: String, db: State<'_, Db>) -> Result<Value, String> {
    require_tier(&db, MIN_TIER, FEATURE)?;
    hub_get(&format!("/messages/{}", recipient)).await
}

#[command]
pub async fn hub_data_catalog(db: State<'_, Db>) -> Result<Value, String> {
    require_tier(&db, MIN_TIER, FEATURE)?;
    hub_get("/data/catalog").await
}

// ─── Hub boot autostart (wraps vct-hub --{register,unregister,}-boot) ────
//
// Surfaces the hub's cross-OS boot-autostart (default-OFF) to the launcher
// Preferences toggle. The underlying ops spawn the vct-hub binary
// synchronously, so each command runs on a blocking task to keep the async
// runtime unblocked (per hub_status's contract note).

/// Boot-autostart state for the Preferences toggle: `"enabled"`,
/// `"disabled"`, or `"unsupported"` (host init system not inspectable).
#[command]
pub async fn get_hub_boot_autostart() -> Result<String, String> {
    let state = tokio::task::spawn_blocking(crate::hub_status::boot_status)
        .await
        .map_err(|e| format!("get_hub_boot_autostart join error: {}", e))?;
    Ok(match state {
        crate::hub_status::BootAutostart::Enabled => "enabled",
        crate::hub_status::BootAutostart::Disabled => "disabled",
        crate::hub_status::BootAutostart::Unsupported => "unsupported",
    }
    .to_string())
}

/// Enable or disable hub boot autostart. `enabled=true` registers the OS
/// unit (idempotent, also starts it); `false` unregisters it. Errors carry
/// the underlying tool's stderr so the toggle can toast an honest reason.
#[command]
pub async fn set_hub_boot_autostart(enabled: bool) -> Result<(), String> {
    tokio::task::spawn_blocking(move || {
        if enabled {
            crate::hub_status::register_boot()
        } else {
            crate::hub_status::unregister_boot()
        }
    })
    .await
    .map_err(|e| format!("set_hub_boot_autostart join error: {}", e))?
}

#[cfg(test)]
mod tests {
    use super::*;

    /// P2-B4 (decision #28) — the server-side half for `/hub`. Same shape
    /// as `commands::coordination`'s test: the four route commands open
    /// with `require_tier(&db, MIN_TIER, FEATURE)`, so a free-tier caller
    /// is refused before the hub token is read off disk.
    ///
    /// Per-command call sites are pinned by
    /// `tests/test_v0291_pro_route_enforcement.py`.
    #[test]
    fn free_tier_is_refused_and_licensed_tiers_are_unchanged() {
        let db = Db::open_in_memory().expect("in-memory db");

        db.set_tier_cache("free", &serde_json::json!({}), None)
            .expect("seed free tier");
        let err = require_tier(&db, MIN_TIER, FEATURE)
            .expect_err("free tier MUST NOT reach the hub surface");
        assert_eq!(
            err, "The Orchestrator Hub requires a Pro or higher tier license.",
            "refusal copy must name the required tier"
        );

        for licensed in ["pro", "mao", "enterprise", "admin"] {
            db.set_tier_cache(licensed, &serde_json::json!({}), None)
                .expect("seed licensed tier");
            assert!(
                require_tier(&db, MIN_TIER, FEATURE).is_ok(),
                "{licensed} tier must keep the hub surface working"
            );
        }
    }

    /// The floor must stay in step with `Sidebar.svelte`'s `proOnly`
    /// flag on `/hub`.
    ///
    /// The leave-alone half — `get_hub_boot_autostart` /
    /// `set_hub_boot_autostart` staying UNGATED because they back the
    /// free-tier Preferences toggle — is pinned in
    /// `tests/test_v0291_pro_route_enforcement.py`, which owns the
    /// source-shape assertions for both routes.
    #[test]
    fn min_tier_is_pro() {
        assert_eq!(MIN_TIER, "pro");
    }
}
