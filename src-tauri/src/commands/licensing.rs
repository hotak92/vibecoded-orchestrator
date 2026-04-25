//! License tier commands.
//!
//! - `license_get_tier`: read the local cache (fast, offline-safe)
//! - `license_refresh`: call the /validate-tier Supabase edge function
//!   and update the cache. 3-day grace period on network failure.
//! - `license_activate`: persist the license key to the keychain and refresh.

use sha2::{Digest, Sha256};
use tauri::{command, State};

use crate::db::models::TierCacheRow;
use crate::db::Db;
use crate::secrets::{self, SecretScope};

/// The validate-tier Supabase edge function URL.
///
/// This is the launcher's Supabase project (Fabio owns it — same one that
/// hosts `lemon-squeezy-webhook`). The URL is public (it only authenticates
/// via `license_key` + `machine_id_hash`, not any secret key), and is
/// overridable at runtime via `VCT_VALIDATE_TIER_URL` for staging/dev setups.
const DEFAULT_VALIDATE_TIER_URL: &str =
    "https://ltnlwhaxnpbiifordlbk.supabase.co/functions/v1/validate-tier";

fn validate_tier_url() -> String {
    std::env::var("VCT_VALIDATE_TIER_URL").unwrap_or_else(|_| DEFAULT_VALIDATE_TIER_URL.to_string())
}

const LICENSE_MODULE_ID: &str = "licensing";
const LICENSE_KEY_NAME: &str = "VIBECODED_LICENSE_KEY";
const GRACE_PERIOD_MS: i64 = 3 * 24 * 3600 * 1000;

#[derive(Debug, serde::Serialize)]
pub struct TierCacheView {
    pub orchestrator_tier: String,
    pub module_licenses: serde_json::Value,
    pub last_validated: i64,
    pub last_error: Option<String>,
    pub grace_period_remaining_ms: Option<i64>,
}

fn to_view(row: TierCacheRow) -> TierCacheView {
    let now = chrono::Utc::now().timestamp_millis();
    let age = now - row.last_validated;
    let remaining = if row.last_validated > 0 && age < GRACE_PERIOD_MS {
        Some(GRACE_PERIOD_MS - age)
    } else {
        None
    };
    TierCacheView {
        orchestrator_tier: row.orchestrator_tier,
        module_licenses: row.module_licenses,
        last_validated: row.last_validated,
        last_error: row.last_error,
        grace_period_remaining_ms: remaining,
    }
}

fn machine_id_hash() -> String {
    // Mirrors commercial_workflow/license/validator.py::_machine_id_hash
    // sha256 of the 8-byte big-endian MAC, hex lowercase.
    let mac = mac_address::get_mac_address().ok().flatten();
    let bytes: [u8; 8] = match mac {
        Some(m) => {
            let bs = m.bytes(); // 6 bytes
            let mut out = [0u8; 8];
            out[2..].copy_from_slice(&bs);
            out
        }
        None => [0u8; 8], // fallback: deterministic per machine is nice-to-have
    };
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

#[command]
pub async fn license_get_tier(db: State<'_, Db>) -> Result<TierCacheView, String> {
    let row = db.get_tier_cache()?;
    Ok(to_view(row))
}

#[command]
pub async fn license_refresh(db: State<'_, Db>) -> Result<TierCacheView, String> {
    let key_opt = secrets::get(SecretScope::Global, LICENSE_MODULE_ID, LICENSE_KEY_NAME)?;
    let key = match key_opt {
        None => {
            // No key → free tier, no error.
            db.set_tier_cache("free", &serde_json::json!({}), None)?;
            return Ok(to_view(db.get_tier_cache()?));
        }
        Some(k) => k,
    };

    let hash = machine_id_hash();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let resp = client
        .post(&validate_tier_url())
        .json(&serde_json::json!({
            "license_key": key,
            "machine_id_hash": hash,
        }))
        .send()
        .await;

    match resp {
        Err(e) => {
            // Network failure: keep cache, update error. Client uses grace window.
            db.set_tier_cache(
                &db.get_tier_cache()?.orchestrator_tier,
                &db.get_tier_cache()?.module_licenses,
                Some(&format!("network: {}", e)),
            )?;
            Ok(to_view(db.get_tier_cache()?))
        }
        Ok(r) => {
            let status = r.status();
            let body: serde_json::Value = r.json().await.unwrap_or(serde_json::json!({}));
            if status.is_success() {
                let tier = body
                    .get("tier")
                    .and_then(|v| v.as_str())
                    .unwrap_or("free")
                    .to_string();
                let valid = body.get("valid").and_then(|v| v.as_bool()).unwrap_or(false);
                let licenses = if valid {
                    serde_json::json!({})
                } else {
                    serde_json::json!({})
                };
                let err = if !valid {
                    body.get("error")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                } else {
                    None
                };
                db.set_tier_cache(&tier, &licenses, err.as_deref())?;
                Ok(to_view(db.get_tier_cache()?))
            } else {
                let err = format!(
                    "status {}: {}",
                    status,
                    body.get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                );
                // On 401 (invalid key) drop to free immediately.
                if status == 401 {
                    db.set_tier_cache("free", &serde_json::json!({}), Some(&err))?;
                } else {
                    db.set_tier_cache(
                        &db.get_tier_cache()?.orchestrator_tier,
                        &db.get_tier_cache()?.module_licenses,
                        Some(&err),
                    )?;
                }
                Ok(to_view(db.get_tier_cache()?))
            }
        }
    }
}

#[command]
pub async fn license_activate(
    license_key: String,
    db: State<'_, Db>,
) -> Result<TierCacheView, String> {
    if license_key.trim().is_empty() {
        return Err("license key cannot be empty".into());
    }
    secrets::set(
        SecretScope::Global,
        LICENSE_MODULE_ID,
        LICENSE_KEY_NAME,
        license_key.trim(),
    )?;
    db.audit(
        "license_activate",
        None,
        None,
        &serde_json::json!({ "key_prefix": license_key.chars().take(8).collect::<String>() }),
    )?;
    license_refresh(db).await
}

#[command]
pub async fn license_deactivate(db: State<'_, Db>) -> Result<(), String> {
    secrets::delete(SecretScope::Global, LICENSE_MODULE_ID, LICENSE_KEY_NAME)?;
    db.set_tier_cache("free", &serde_json::json!({}), None)?;
    db.audit("license_deactivate", None, None, &serde_json::json!({}))?;
    Ok(())
}
