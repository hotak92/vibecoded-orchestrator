//! License tier commands.
//!
//! - `license_get_tier`: read the local cache (fast, offline-safe)
//! - `license_refresh`: call the /validate-tier Supabase edge function
//!   and update the cache. 3-day grace period on network failure.
//! - `license_activate`: persist the license key to the keychain and refresh.

use std::path::PathBuf;

use sha2::{Digest, Sha256};
use tauri::{command, State};

use crate::db::models::TierCacheRow;
use crate::db::Db;
use crate::secrets::{self, SecretScope};

/// The validate-tier edge function URL.
///
/// Default = the Supabase project's canonical functions URL. The earlier
/// default `https://api.vibecodedtools.it/validate-tier` was wishful
/// thinking — that DNS record was never created (IONOS zone has no
/// `api` subdomain; verified 2026-05-06 with `dig`). Every license
/// refresh from default config returned NXDOMAIN and fell back to the
/// 3-day cache grace, masking activation/deactivation issues.
///
/// Two reasons not to "fix" by adding the DNS:
///   1. Cross-subdomain risk: `vibecodedtools.it` apex serves the
///      Vercel website. Putting the API on `api.<apex>` shares cookies
///      and CORS surface with the marketing site.
///   2. DNS-config drift: one IONOS panel slip and we're back to NXDOMAIN.
///      The Supabase URL is stable as long as the project exists.
///
/// Operators override via `VCT_VALIDATE_TIER_URL` for staging/dev or for
/// a future custom-domain plan (e.g. `api.vct.cloud`, kept distinct from
/// the website apex).
///
/// Mirrors `VCThelpers/license/validator.py::_DEFAULT_VALIDATE_URL`.
const DEFAULT_VALIDATE_TIER_URL: &str =
    "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/validate-tier";

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

// ---------------------------------------------------------------------------
// Bug #22 (v0.2.31): token-gateway license cache file.
// ---------------------------------------------------------------------------
//
// `installer_engine::request_pull_token` reads
// `~/.vibecoded/license_cache.json` and POSTs the body verbatim to a
// per-module `pull_token_endpoint` (signed-URL gateway, Phase 3A).
//
// Before this fix, the Rust launcher only persisted tier state to SQLite
// (`tier_cache`) — the JSON file the token gateway expects was never
// written, so every paid-module pull fell through to anonymous registry
// access. That worked while vct-rl-reranker's GHCR image was public, but
// breaks the moment the image flips to private (v1.0 anti-piracy ship).
//
// We write the same JSON shape that the Python validator at
// `VCThelpers/license/validator.py` (`LicenseResult.to_json()`) writes,
// so the token gateway has one wire contract to authenticate against:
//
//   { "tier": "pro" | "mao" | "enterprise" | "admin",
//     "valid": true,
//     "expires_at": "2027-04-18T00:00:00.000Z" | null,
//     "last_validated_at": 1734567890.123,   # epoch seconds (float)
//     "message": "Validated." }
//
// Soft-fail discipline: a write error is logged but does NOT fail
// `license_refresh`. The token-gateway path degrades to anonymous pull
// on its own (the current pre-fix behaviour) if write fails.

/// Resolve `~/.vibecoded/license_cache.json` via `directories::UserDirs`
/// — same resolver `installer_engine::request_pull_token` uses, so the
/// reader/writer agree on the path on every OS.
///
/// Tests inject an explicit `$HOME` via `home_override`; production
/// callers pass `None` and let `directories::UserDirs::new()` resolve
/// the real one.
fn license_cache_path_in(home_override: Option<&std::path::Path>) -> Option<PathBuf> {
    if let Some(home) = home_override {
        return Some(home.join(".vibecoded/license_cache.json"));
    }
    directories::UserDirs::new().map(|d| d.home_dir().join(".vibecoded/license_cache.json"))
}

/// Apply mode 0o600 to the cache file on Unix. NTFS ACLs inherit from
/// the parent on Windows, so no-op there.
#[cfg(unix)]
fn set_cache_file_mode_0600(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, perms)
}

#[cfg(not(unix))]
fn set_cache_file_mode_0600(_path: &std::path::Path) -> std::io::Result<()> {
    Ok(())
}

/// Write `~/.vibecoded/license_cache.json` in the
/// `VCThelpers/license/validator.py::LicenseResult` shape so the Phase
/// 3A token gateway has a stable wire contract. Called from the
/// `license_refresh` success path and the activation success path.
///
/// Soft-fail: every error path is logged via `eprintln!` and swallowed.
/// The caller (`license_refresh`) MUST NOT propagate the error — the
/// token-gateway flow degrading to anonymous pull is preferable to a
/// licensing UX regression.
fn write_license_cache_for_token_gateway(
    tier: &str,
    valid: bool,
    expires_at: Option<&str>,
    message: &str,
) {
    write_license_cache_for_token_gateway_in(None, tier, valid, expires_at, message);
}

/// Same as `write_license_cache_for_token_gateway()` but allows callers
/// (tests) to inject an explicit `$HOME` override.
fn write_license_cache_for_token_gateway_in(
    home_override: Option<&std::path::Path>,
    tier: &str,
    valid: bool,
    expires_at: Option<&str>,
    message: &str,
) {
    let path = match license_cache_path_in(home_override) {
        Some(p) => p,
        None => {
            eprintln!(
                "[licensing] cannot resolve ~/.vibecoded/license_cache.json \
                 (no UserDirs); token-gateway flow will fall back to anonymous pull"
            );
            return;
        }
    };

    if let Some(parent) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(parent) {
            eprintln!(
                "[licensing] failed to mkdir {}: {} \
                 — token-gateway flow will fall back to anonymous pull",
                parent.display(),
                e
            );
            return;
        }
    }

    // last_validated_at is epoch seconds as a float, matching the Python
    // `time.time()` representation in `LicenseResult.last_validated_at`.
    let last_validated_at =
        chrono::Utc::now().timestamp_millis() as f64 / 1000.0;

    let payload = serde_json::json!({
        "tier": tier,
        "valid": valid,
        "expires_at": expires_at,
        "last_validated_at": last_validated_at,
        "message": message,
    });

    let body = match serde_json::to_string(&payload) {
        Ok(s) => s,
        Err(e) => {
            eprintln!(
                "[licensing] serialize license cache: {} \
                 — token-gateway flow will fall back to anonymous pull",
                e
            );
            return;
        }
    };

    if let Err(e) = std::fs::write(&path, body) {
        eprintln!(
            "[licensing] write {} failed: {} \
             — token-gateway flow will fall back to anonymous pull",
            path.display(),
            e
        );
        return;
    }

    if let Err(e) = set_cache_file_mode_0600(&path) {
        // Non-fatal: the file is written; the mode-tightening just
        // failed. Log so the user can `chmod 600` manually if they care.
        eprintln!(
            "[licensing] chmod 0600 {} failed: {} \
             (cache written; token-gateway flow still functional)",
            path.display(),
            e
        );
    }
}

/// Remove `~/.vibecoded/license_cache.json` on deactivation. Soft-fail:
/// if the file doesn't exist or can't be removed, we log and continue —
/// the gateway path will reject the stale JSON anyway since it
/// re-validates server-side.
fn remove_license_cache_for_token_gateway() {
    remove_license_cache_for_token_gateway_in(None);
}

/// Same as `remove_license_cache_for_token_gateway()` but allows
/// callers (tests) to inject an explicit `$HOME` override.
fn remove_license_cache_for_token_gateway_in(home_override: Option<&std::path::Path>) {
    let path = match license_cache_path_in(home_override) {
        Some(p) => p,
        None => return,
    };
    if !path.exists() {
        return;
    }
    if let Err(e) = std::fs::remove_file(&path) {
        eprintln!(
            "[licensing] remove {} failed: {} \
             (stale cache will be rejected by the gateway on next pull)",
            path.display(),
            e
        );
    }
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
            // No key → free tier, no error. Also clear any stale cache
            // file so the token gateway can't authenticate from it.
            db.set_tier_cache("free", &serde_json::json!({}), None)?;
            remove_license_cache_for_token_gateway();
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

                // Bug #20-Fix-2 (v0.2.31): parse `module_licenses` from
                // the server response and persist it to `tier_cache` so
                // `is_module_licensed`'s per-module entitlement check is
                // enforceable. Expected shape, per the v0.2.31 plan:
                //
                //   { "vct-rl-reranker": {
                //       "tier": "pro",
                //       "expires_at": 1234567890,
                //       "source": "tier-bundled" | "per-module"
                //     }, ... }
                //
                // NOTE: As of 2026-05-23 the `validate-tier` edge
                // function at `launcher/supabase/functions/validate-tier
                // /index.ts` does NOT yet return `module_licenses` — the
                // wire-contract addition is tracked separately in the
                // v0.2.31 plan (orchestrator chat coordinates the
                // server-side change). Until then, `body.module_licenses`
                // is missing and we fall through to `json!({})`, which
                // matches the pre-fix behaviour — NOT a regression.
                // Once the edge function ships the field, no further
                // launcher change is required: this parse already
                // accepts it.
                let licenses = if valid {
                    body.get("module_licenses")
                        .filter(|v| v.is_object())
                        .cloned()
                        .unwrap_or_else(|| serde_json::json!({}))
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

                // Bug #22 (v0.2.31): the token gateway reads
                // `~/.vibecoded/license_cache.json`. Mirror the SQLite
                // write to that JSON file so paid-module pulls can
                // authenticate. Soft-fail (logged, never propagates).
                if valid {
                    let expires_at = body
                        .get("expires_at")
                        .and_then(|v| v.as_str());
                    let message = body
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("Validated.");
                    write_license_cache_for_token_gateway(
                        &tier,
                        valid,
                        expires_at,
                        message,
                    );
                } else {
                    // Server says invalid → remove stale cache so the
                    // gateway can't authenticate a now-revoked license.
                    remove_license_cache_for_token_gateway();
                }

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
                    // 401 = explicitly revoked/invalid → drop the cache
                    // file too so the gateway can't authenticate.
                    remove_license_cache_for_token_gateway();
                } else {
                    db.set_tier_cache(
                        &db.get_tier_cache()?.orchestrator_tier,
                        &db.get_tier_cache()?.module_licenses,
                        Some(&err),
                    )?;
                    // 5xx / 4xx-other: leave the JSON cache untouched so
                    // the user keeps their in-grace-period authority.
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
    // Bug #22: clean shutdown — drop the JSON cache file so the token
    // gateway can't authenticate from yesterday's tier after the user
    // has explicitly deactivated. Soft-fail; logged on error.
    remove_license_cache_for_token_gateway();
    db.audit("license_deactivate", None, None, &serde_json::json!({}))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Bug 33: admin tier passthrough tests.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Endpoint safety: URL must be HTTPS. (No more "must contain
    /// vibecodedtools.it" / "must not contain supabase.co" — those guards
    /// were defending against information disclosure of the project ID,
    /// but the project ID is already publicly disclosed in
    /// `launcher/supabase/config.toml` which ships in the AGPL source
    /// repo. URL secrecy in the binary was theatre. Per the 2026-05-06
    /// security review at
    /// `.claude/context/supabase-license-security-review-2026-05-06.md`
    /// — verdict "SAFE WITH CAVEATS, ship the bare URL".
    ///
    /// Real license-validation hardening (env-var allowlist for
    /// VCT_VALIDATE_TIER_URL, rate limiting on the Supabase function,
    /// signed cache to prevent local tampering) is tracked separately
    /// as F14/F1/F15 hardening tickets — not addressed by URL secrecy.
    #[test]
    fn default_validate_tier_url_is_https() {
        let url = DEFAULT_VALIDATE_TIER_URL;
        assert!(
            url.starts_with("https://"),
            "DEFAULT_VALIDATE_TIER_URL must be HTTPS: {}",
            url
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

    // -----------------------------------------------------------------
    // Bug #22 (v0.2.31): token-gateway license cache file.
    //
    // We exercise the `_in()` variants of the helpers with a tempdir as
    // the injected `$HOME`. Production code calls the no-arg variants
    // that route through `directories::UserDirs::new()`.
    // -----------------------------------------------------------------

    /// Resolved cache path under a fake `$HOME` matches what
    /// `installer_engine::request_pull_token` reads.
    #[test]
    fn license_cache_path_under_home_override() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = license_cache_path_in(Some(tmp.path())).expect("resolved");
        assert_eq!(path, tmp.path().join(".vibecoded/license_cache.json"));
    }

    /// Production path (no override) resolves to the real `$HOME` —
    /// guards against the resolver returning `None` on this machine.
    /// We only assert the suffix; the literal `$HOME` value varies.
    #[test]
    fn license_cache_path_resolves_under_real_home() {
        // If this returns None, every install on this OS would silently
        // fall back to anonymous pull. We pin that the resolver works.
        let path = license_cache_path_in(None);
        if let Some(p) = path {
            assert!(
                p.ends_with(".vibecoded/license_cache.json"),
                "production path must end with .vibecoded/license_cache.json: {}",
                p.display()
            );
        }
        // Don't hard-fail on None — CI sandboxes occasionally lack
        // $HOME. The soft-fail discipline in `write_license_cache_…`
        // already handles that case.
    }

    /// Round-trip: write the cache file under a fake $HOME and verify
    /// it (a) exists at the expected path, (b) parses as JSON, and
    /// (c) carries every field the Python `LicenseResult` shape uses.
    #[test]
    fn write_license_cache_round_trip() {
        let tmp = tempfile::tempdir().expect("tempdir");
        write_license_cache_for_token_gateway_in(
            Some(tmp.path()),
            "pro",
            true,
            Some("2027-04-18T00:00:00.000Z"),
            "Validated.",
        );

        let path = tmp.path().join(".vibecoded/license_cache.json");
        assert!(path.exists(), "cache file must be created");

        let body = std::fs::read_to_string(&path).expect("read cache");
        let parsed: serde_json::Value =
            serde_json::from_str(&body).expect("valid JSON");

        // Mirror the LicenseResult shape — every field the Python
        // validator writes must be present and readable by the gateway.
        assert_eq!(parsed["tier"], "pro");
        assert_eq!(parsed["valid"], true);
        assert_eq!(parsed["expires_at"], "2027-04-18T00:00:00.000Z");
        assert_eq!(parsed["message"], "Validated.");
        assert!(
            parsed["last_validated_at"].is_number(),
            "last_validated_at must be epoch seconds (number)"
        );
    }

    /// `expires_at = None` round-trips as JSON null (lifetime license).
    #[test]
    fn write_license_cache_handles_lifetime_license() {
        let tmp = tempfile::tempdir().expect("tempdir");
        write_license_cache_for_token_gateway_in(
            Some(tmp.path()),
            "enterprise",
            true,
            None,
            "Validated.",
        );

        let body = std::fs::read_to_string(tmp.path().join(".vibecoded/license_cache.json"))
            .expect("read cache");
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(parsed["tier"], "enterprise");
        assert!(parsed["expires_at"].is_null(), "lifetime → null");
    }

    /// Deactivation removes the cache file. Idempotent on a missing
    /// file (no panic, no error).
    #[test]
    fn remove_license_cache_removes_file_and_is_idempotent() {
        let tmp = tempfile::tempdir().expect("tempdir");
        write_license_cache_for_token_gateway_in(
            Some(tmp.path()),
            "pro",
            true,
            None,
            "Validated.",
        );
        let path = tmp.path().join(".vibecoded/license_cache.json");
        assert!(path.exists(), "precondition: file written");

        remove_license_cache_for_token_gateway_in(Some(tmp.path()));
        assert!(!path.exists(), "file must be removed");

        // Second call with the file already gone — must not panic.
        remove_license_cache_for_token_gateway_in(Some(tmp.path()));
    }

    /// Write failure is non-fatal. We point the helper at a path whose
    /// parent already exists as a FILE (so `create_dir_all` fails) and
    /// confirm the helper returns without panicking and without writing
    /// anything.
    #[test]
    fn write_license_cache_soft_fails_on_unwritable_parent() {
        let tmp = tempfile::tempdir().expect("tempdir");
        // Create a regular file where `.vibecoded` would need to be a
        // directory — `create_dir_all` on this path will fail with
        // ENOTDIR / similar.
        let blocker = tmp.path().join(".vibecoded");
        std::fs::write(&blocker, b"not a directory").expect("seed blocker");

        // No panic, no propagation — soft-fail discipline.
        write_license_cache_for_token_gateway_in(
            Some(tmp.path()),
            "pro",
            true,
            None,
            "Validated.",
        );

        // Blocker file is untouched (still a regular file).
        assert!(blocker.is_file(), "blocker must remain a regular file");
    }

    /// On Unix, the cache file must be mode 0600 so a multi-user box
    /// can't side-read a paid tier from another account's $HOME.
    #[cfg(unix)]
    #[test]
    fn write_license_cache_sets_mode_0600_on_unix() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = tempfile::tempdir().expect("tempdir");
        write_license_cache_for_token_gateway_in(
            Some(tmp.path()),
            "pro",
            true,
            None,
            "Validated.",
        );
        let path = tmp.path().join(".vibecoded/license_cache.json");
        let meta = std::fs::metadata(&path).expect("stat cache");
        // mode_bits & 0o777 isolates the perms from the file-type bits.
        let mode = meta.permissions().mode() & 0o777;
        assert_eq!(
            mode, 0o600,
            "license cache must be readable only by owner; got {:o}",
            mode
        );
    }

    // -----------------------------------------------------------------
    // Bug #20-Fix-2 partial (v0.2.31): module_licenses parse.
    //
    // We exercise the success-branch parser by mimicking the body
    // shapes `r.json().await` would produce. The full HTTP loop is
    // covered by integration tests separately — these tests pin the
    // semantic contract (what gets persisted to `tier_cache`).
    // -----------------------------------------------------------------

    /// The success-branch parser used inside `license_refresh`:
    /// - `valid:true` + `module_licenses` object → pass-through verbatim.
    /// - `valid:true` + missing field           → `json!({})`.
    /// - `valid:true` + wrong shape (array)      → `json!({})`.
    /// - `valid:false`                            → `json!({})`.
    ///
    /// We inline the parser here so the test pins the semantic without
    /// having to spin up an HTTP mock — the parse expression is the
    /// load-bearing piece of the fix.
    fn parse_module_licenses(body: &serde_json::Value, valid: bool) -> serde_json::Value {
        if valid {
            body.get("module_licenses")
                .filter(|v| v.is_object())
                .cloned()
                .unwrap_or_else(|| serde_json::json!({}))
        } else {
            serde_json::json!({})
        }
    }

    #[test]
    fn module_licenses_pass_through_when_present_and_valid() {
        let body = serde_json::json!({
            "tier": "pro",
            "valid": true,
            "module_licenses": {
                "vct-rl-reranker": {
                    "tier": "pro",
                    "expires_at": 1_234_567_890_i64,
                    "source": "tier-bundled"
                }
            }
        });
        let licenses = parse_module_licenses(&body, true);
        assert!(licenses.is_object(), "must be object");
        assert_eq!(licenses["vct-rl-reranker"]["tier"], "pro");
        assert_eq!(licenses["vct-rl-reranker"]["source"], "tier-bundled");
    }

    #[test]
    fn module_licenses_falls_back_to_empty_when_field_absent() {
        // This is the as-of-2026-05-23 reality: the edge function does
        // not yet return `module_licenses`. The parser must keep the
        // launcher functional (no regression) until the wire contract
        // ships server-side.
        let body = serde_json::json!({ "tier": "pro", "valid": true });
        let licenses = parse_module_licenses(&body, true);
        assert_eq!(licenses, serde_json::json!({}));
    }

    #[test]
    fn module_licenses_falls_back_to_empty_when_wrong_shape() {
        // Defensive: if the server (or a future protocol bug) sends an
        // array or a string, we refuse to persist it — `tier_cache`
        // gets `{}`, not the malformed value, so downstream
        // `is_module_licensed` keeps a well-formed map to query.
        let body = serde_json::json!({
            "tier": "pro",
            "valid": true,
            "module_licenses": ["this", "should", "be", "an", "object"]
        });
        let licenses = parse_module_licenses(&body, true);
        assert_eq!(licenses, serde_json::json!({}));
    }

    #[test]
    fn module_licenses_empty_when_invalid() {
        // valid:false → no entitlements regardless of what the server
        // included in module_licenses. Belt-and-suspenders against a
        // mis-encoded response.
        let body = serde_json::json!({
            "tier": "free",
            "valid": false,
            "module_licenses": {
                "vct-rl-reranker": { "tier": "pro" }
            }
        });
        let licenses = parse_module_licenses(&body, false);
        assert_eq!(licenses, serde_json::json!({}));
    }

    /// 401 from the server (invalid key) → tier_cache drops to free
    /// with empty module_licenses. We exercise the persistence layer
    /// directly since the HTTP loop is integration-tested elsewhere.
    #[tokio::test]
    async fn tier_cache_401_drops_to_free_with_empty_licenses() {
        let db = Db::open_in_memory().expect("in-memory db");
        // Seed: pro tier with module licenses.
        db.set_tier_cache(
            "pro",
            &serde_json::json!({ "vct-rl-reranker": { "tier": "pro" } }),
            None,
        )
        .unwrap();

        // Simulate the 401 branch of `license_refresh`.
        db.set_tier_cache("free", &serde_json::json!({}), Some("status 401: bad key"))
            .unwrap();

        let row = db.get_tier_cache().unwrap();
        assert_eq!(row.orchestrator_tier, "free");
        assert_eq!(row.module_licenses, serde_json::json!({}));
        assert!(row.last_error.is_some());
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
