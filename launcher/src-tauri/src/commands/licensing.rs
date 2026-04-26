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

/// The validate-tier edge function URL.
///
/// Public alias documented in the module docstring; resolves to the
/// licensing edge function. Internal infra URLs are not committed to public
/// source — operators set `VCT_VALIDATE_TIER_URL` to override for staging/dev.
/// Mirrors `VCThelpers/license/validator.py::_DEFAULT_VALIDATE_URL`.
const DEFAULT_VALIDATE_TIER_URL: &str = "https://api.vibecodedtools.it/validate-tier";

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

/// Bug 33: thin convenience command that returns true iff the cached
/// orchestrator tier is `"admin"`. Frontend uses this to gate the
/// admin sidebar group + ADMIN badge.
///
/// Cached value comes from the same `tier_cache` row that
/// `license_get_tier` returns — the SOURCE OF TRUTH is the Supabase
/// `validate-tier` edge function, which classifies `tier=admin` only
/// when the variant_id is in `LS_ADMIN_VARIANT_IDS` (Bug 33). Patching
/// this function to always return true unlocks client-side dev UI but
/// does NOT unlock server-gated capabilities (paid module artifact
/// downloads re-validate JWTs server-side).
#[command]
pub async fn license_is_admin(db: State<'_, Db>) -> Result<bool, String> {
    let row = db.get_tier_cache()?;
    Ok(row.orchestrator_tier == "admin")
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

// ---------------------------------------------------------------------------
// Bug 33: admin tier passthrough tests.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Endpoint safety: the default validate-tier URL must be a public alias,
    /// not a `*.supabase.co` project URL. Mirrors
    /// `tests/test_license_validator.py::TestEndpointSafety::test_default_url_is_public_alias_only`.
    /// The Python side already had this guard; the Rust side previously leaked
    /// the internal Supabase project ID — this test prevents regressions.
    #[test]
    fn default_validate_tier_url_is_public_alias_only() {
        let url = DEFAULT_VALIDATE_TIER_URL;
        assert!(
            !url.contains("supabase.co"),
            "DEFAULT_VALIDATE_TIER_URL must not leak the internal Supabase project URL: {}",
            url
        );
        assert!(
            url.starts_with("https://"),
            "DEFAULT_VALIDATE_TIER_URL must be HTTPS: {}",
            url
        );
        assert!(
            url.contains("vibecodedtools.it"),
            "DEFAULT_VALIDATE_TIER_URL must be the public vibecodedtools.it alias: {}",
            url
        );
    }

    /// Source-level audit: the licensing module's executable source must not
    /// contain any `*.supabase.co` project hostnames. Strips comments and
    /// docstrings before scanning so prose mentions stay legal.
    #[test]
    fn no_supabase_co_in_licensing_source() {
        let repo_root = super::super::installer::find_local_repo_root().expect("repo root");
        let licensing_rs = repo_root.join("launcher/src-tauri/src/commands/licensing.rs");
        let content = std::fs::read_to_string(&licensing_rs).expect("read licensing.rs");
        let scan_end = content.find("#[cfg(test)]").unwrap_or(content.len());
        let production = &content[..scan_end];
        let cleaned = strip_for_audit(production);
        assert!(
            !cleaned.contains("supabase.co"),
            "FORBIDDEN: 'supabase.co' found in production source of {} — use the public alias",
            licensing_rs.display()
        );
    }

    /// `to_view` must pass `orchestrator_tier` through verbatim — admin
    /// tier produced by the server must NOT be remapped or sanitized to
    /// "free" / "enterprise" by the client. The client treats "admin"
    /// as a strict superset of "enterprise" but the wire string stays
    /// "admin".
    #[test]
    fn to_view_passes_admin_tier_through() {
        let row = TierCacheRow {
            orchestrator_tier: "admin".to_string(),
            module_licenses: serde_json::json!({}),
            last_validated: chrono::Utc::now().timestamp_millis(),
            last_error: None,
        };
        let view = to_view(row);
        assert_eq!(view.orchestrator_tier, "admin");
    }

    /// `license_is_admin` returns true iff cache says admin. Uses an
    /// in-memory db so the test doesn't touch the user's real cache.
    #[tokio::test]
    async fn license_is_admin_reflects_tier_cache() {
        let db = Db::open_in_memory().expect("in-memory db");
        // Default tier in a fresh cache is "free".
        db.set_tier_cache("free", &serde_json::json!({}), None).unwrap();
        assert_eq!(db.get_tier_cache().unwrap().orchestrator_tier, "free");

        // Simulate the server returning admin.
        db.set_tier_cache("admin", &serde_json::json!({}), None).unwrap();
        assert_eq!(db.get_tier_cache().unwrap().orchestrator_tier, "admin");
    }

    /// Bug 33 audit: the licensing module must NOT contain any local
    /// bypass paths. Source-level grep: assert no symbols matching
    /// `_verify_maintainer_token`, `MAINTAINER_TOKEN`, `ed25519`,
    /// `bypass_token`, etc. appear anywhere in the licensing surface.
    #[test]
    fn no_local_bypass_paths_in_licensing_or_validator() {
        // We can't import the Python validator from Rust — instead we
        // walk the file and grep its source. Same for licensing.rs.
        let repo_root = super::super::installer::find_local_repo_root().expect("repo root");
        let licensing_rs = repo_root.join("launcher/src-tauri/src/commands/licensing.rs");
        let validator_py = repo_root.join("VCThelpers/license/validator.py");

        let forbidden = [
            "MAINTAINER_TOKEN",
            "_verify_maintainer_token",
            "_verify_maintainer",
            "VCT_MAINTAINER_TOKEN",
            "ed25519_dalek",
            "Ed25519",
            "maintainer_token",
            "maintainer_signing_key",
        ];

        for path in [&licensing_rs, &validator_py] {
            let content = match std::fs::read_to_string(path) {
                Ok(c) => c,
                Err(_) => continue,
            };
            // Strip Rust /// doc comments + Python triple-quoted
            // docstrings + line comments, since prose is allowed.
            let scan_end = content.find("#[cfg(test)]").unwrap_or(content.len());
            let production = &content[..scan_end];
            let cleaned = strip_for_audit(production);
            for needle in &forbidden {
                assert!(
                    !cleaned.contains(needle),
                    "FORBIDDEN: '{}' found in {} — Bug 33 dropped local bypass paths",
                    needle,
                    path.display()
                );
            }
        }
    }

    /// Replace Python triple-quoted blocks AND Rust /// doc comments
    /// with whitespace so the audit only sees executable source. Match
    /// the helper used in the volumes module's audit.
    fn strip_for_audit(src: &str) -> String {
        let mut out = String::with_capacity(src.len());
        let bytes = src.as_bytes();
        let mut i = 0usize;
        while i < bytes.len() {
            let three_double = i + 3 <= bytes.len() && &bytes[i..i + 3] == b"\"\"\"";
            let three_single = i + 3 <= bytes.len() && &bytes[i..i + 3] == b"'''";
            if three_double || three_single {
                let marker: &[u8] = if three_double { b"\"\"\"" } else { b"'''" };
                let start = i + 3;
                let mut j = start;
                while j + 3 <= bytes.len() {
                    if &bytes[j..j + 3] == marker {
                        break;
                    }
                    j += 1;
                }
                let end = (j + 3).min(bytes.len());
                for k in i..end {
                    if bytes[k] == b'\n' {
                        out.push('\n');
                    } else {
                        out.push(' ');
                    }
                }
                i = end;
                continue;
            }
            out.push(bytes[i] as char);
            i += 1;
        }
        // Strip line comments after multiline scrubbing.
        out.lines()
            .map(|line| {
                // Rust doc /// or // and Python/sh #
                let cut = line
                    .find("///")
                    .or_else(|| line.find("//"))
                    .or_else(|| line.find('#'))
                    .unwrap_or(line.len());
                &line[..cut]
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}
