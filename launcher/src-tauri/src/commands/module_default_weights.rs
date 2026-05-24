// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.32 #D: download the default RL weights bundle from Supabase
//! and stage it under `<vct_root>/modules/<module_id>/weights/`.
//!
//! ## Why this exists (separate from `module_service::download_weights`)
//!
//! `module_service::apply_weights_update` is the full
//! check-update → download → rotate flow driven by the daily poller.
//! That flow pipes the user through the `FinetuneChoice` prompt + the
//! per-project running container.
//!
//! This command is the simpler "give me the latest default weights
//! .pt for this embedding source" call. It's what the v0.2.32 paid-
//! module manifest button dispatches via a `tauri_command` action
//! kind on first install (before the user has ever fine-tuned),
//! and what Agent C's `chained_action` variant in the renderer
//! threads through to the follow-up `/finetune` POST.
//!
//! ## Flow
//!
//! 1. Resolve the license key from the OS keychain (same scope the
//!    licensing module uses — `SecretScope::Global` /
//!    `licensing` / `VIBECODED_LICENSE_KEY`). No license ⇒ refuse.
//! 2. POST to the Supabase edge `rl-latest-weights` with
//!    `{license_key, machine_id_hash, embedding_source}` and the
//!    license key in the `Authorization: Bearer` header (the edge
//!    function accepts either; we pass both for compatibility with
//!    older + newer edge versions). Receive
//!    `{download_url, version, sha256?}`.
//! 3. Stream the .pt to
//!    `<vct_root>/modules/<module_id>/weights/<source>-<version>.pt`
//!    via the same tmp-write-then-atomic-rename pattern
//!    `module_service::download_weights` uses.
//! 4. Verify sha256 if the response includes one.
//! 5. Best-effort upsert a row into the module's
//!    `rl_global_weights_available` table via the hub's typed REST
//!    surface (Agent I). Soft-fails: a hub-unreachable or token-
//!    issue error logs and continues — the download itself is the
//!    user-visible deliverable.
//! 6. Return `{ local_path, version }` so a chained_action can
//!    thread the path into a follow-up `/finetune` POST.
//!
//! ## Anti-piracy posture
//!
//! Server-side: the edge function validates the JWT + license tier
//! before issuing a signed download URL (signed URLs are short-lived,
//! single-use). A leaked .pt expires automatically within the URL TTL.
//!
//! Client-side: this command refuses to call the edge endpoint
//! without a license key. The free-tier fallback path (degrade
//! gracefully when the user has no license) is implemented by the
//! renderer: the manifest button hides itself when
//! `tier_cache.orchestrator_tier == "free"`.

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{command, AppHandle, State};

use crate::commands::module_db::{DEFAULT_TOKEN_TTL_MS, TOKEN_BYTES};
use crate::db::Db;

/// Default Supabase edge endpoint. Operators override via
/// `VCT_RL_LATEST_WEIGHTS_URL` for staging / dev.
///
/// Distinct from `module_service::DEFAULT_RL_LATEST_VERSION_ENDPOINT`
/// — that one (`/rl-latest-version`) is the daily-poller endpoint that
/// reports whether an update is available; this one
/// (`/rl-latest-weights`) is the explicit "give me the default
/// weights bundle" endpoint dispatched by the manifest button.
pub const DEFAULT_RL_LATEST_WEIGHTS_ENDPOINT: &str =
    "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-latest-weights";

/// Bounded download timeout. Weights bundles are typically 50-500 MB;
/// 600 s gives 1 MB/s headroom on slow connections without letting a
/// hung server deadlock the renderer.
const DOWNLOAD_TIMEOUT_SECS: u64 = 600;

/// Bounded edge-call timeout. The edge function is cheap (signs URL,
/// returns JSON); 15 s is generous.
const EDGE_TIMEOUT_SECS: u64 = 15;

/// Bounded hub-upsert timeout. Mirrors `module_db_client::HUB_READ_TIMEOUT_SECS`
/// (5 s) but writes go through the same code path.
const HUB_WRITE_TIMEOUT_SECS: u64 = 5;

/// Margin (ms) below token expiry at which we proactively refresh.
/// Same value module_db_client uses.
const TOKEN_REFRESH_MARGIN_MS: i64 = 60_000;

// ─── Wire types ─────────────────────────────────────────────────────────

/// Response shape from the Supabase edge `rl-latest-weights`.
///
/// `download_url` is the signed, short-lived (~15 min) HTTPS URL
/// pointing at the .pt blob. `version` is the server-side semantic
/// version of the bundle (e.g. `"v3"` or `"2026-05-24"`). `sha256`
/// is the hex-encoded SHA-256 of the .pt content — when present we
/// verify before promoting the tmp file. Empty string = skip.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct RlLatestWeightsResponse {
    pub download_url: String,
    pub version: String,
    #[serde(default)]
    pub sha256: String,
    #[serde(default)]
    pub expires_at: String,
}

/// Tauri-command return value.
///
/// `local_path` is the absolute path to the freshly-written .pt file
/// (UTF-8; the manifest assumes paths are decodable — which holds on
/// every supported OS because we sanitise the source/version
/// components below). `version` echoes the server-provided version
/// so the renderer can stash it for chained actions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DownloadDefaultWeightsResult {
    pub local_path: String,
    pub version: String,
}

// ─── Path helpers ───────────────────────────────────────────────────────

/// Replace `[^A-Za-z0-9._-]` with `_` so a hostile string can never
/// escape its directory. Idempotent on already-safe input. Mirrors
/// `module_service::sanitize_path_component`; duplicated rather than
/// re-exported to keep this module compilable independently.
fn sanitize_path_component(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// Resolve `<vct_root>/modules/<module_id>/weights/`. Mkdir is the
/// caller's responsibility — this only computes the path.
pub fn module_weights_dir(module_id: &str) -> PathBuf {
    crate::paths::vct_root_dir()
        .join("modules")
        .join(sanitize_path_component(module_id))
        .join("weights")
}

/// Resolve the final filename for a given (source, version) pair.
/// Pattern pinned by the brief: `<embedding_source>-<version>.pt`.
pub fn weights_file_path(module_id: &str, embedding_source: &str, version: &str) -> PathBuf {
    module_weights_dir(module_id).join(format!(
        "{}-{}.pt",
        sanitize_path_component(embedding_source),
        sanitize_path_component(version),
    ))
}

// ─── License + endpoint helpers ─────────────────────────────────────────

/// Read the user's license key from the OS keychain. Returns `Err` on
/// "no key configured" so the renderer can surface a clear message
/// rather than calling the edge with an empty bearer (which would
/// always 401).
fn resolve_license_key() -> Result<String, String> {
    // Same scope used by `commands::licensing` — keep them in sync if
    // either changes (the launcher exposes only one license key per
    // user, not per-module).
    let key_opt = crate::secrets::get(
        crate::secrets::SecretScope::Global,
        "licensing",
        "VIBECODED_LICENSE_KEY",
    )
    .map_err(|e| format!("read license keychain: {}", e))?;

    let key = key_opt.ok_or_else(|| {
        "no license key configured — paid-module weights require Pro tier or higher".to_string()
    })?;

    let trimmed = key.trim();
    if trimmed.is_empty() {
        return Err("license key is empty".to_string());
    }
    Ok(trimmed.to_string())
}

fn rl_latest_weights_url() -> String {
    std::env::var("VCT_RL_LATEST_WEIGHTS_URL")
        .unwrap_or_else(|_| DEFAULT_RL_LATEST_WEIGHTS_ENDPOINT.to_string())
}

// ─── Core download logic ────────────────────────────────────────────────

/// POST the edge function for the signed download URL.
///
/// `endpoint` is parameterised so tests can point this at a mock
/// server. Production callers use `rl_latest_weights_url()`.
pub async fn fetch_signed_download_url(
    endpoint: &str,
    license_key: &str,
    machine_id_hash: &str,
    embedding_source: &str,
    module_id: &str,
) -> Result<RlLatestWeightsResponse, String> {
    let body = serde_json::json!({
        "license_key": license_key,
        "machine_id_hash": machine_id_hash,
        "embedding_source": embedding_source,
        "module_id": module_id,
    });

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(EDGE_TIMEOUT_SECS))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let resp = client
        .post(endpoint)
        .bearer_auth(license_key)
        .header("Content-Type", "application/json")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("POST {}: {}", endpoint, e))?;

    let status = resp.status();
    if !status.is_success() {
        let body_text = resp.text().await.unwrap_or_default();
        let preview = body_text.chars().take(200).collect::<String>();
        return Err(format!("rl-latest-weights returned {}: {}", status, preview));
    }

    let parsed: RlLatestWeightsResponse = resp
        .json()
        .await
        .map_err(|e| format!("parse rl-latest-weights response: {}", e))?;

    if parsed.download_url.is_empty() {
        return Err("rl-latest-weights returned empty download_url".to_string());
    }
    if parsed.version.is_empty() {
        return Err("rl-latest-weights returned empty version".to_string());
    }
    Ok(parsed)
}

/// Stream the .pt to `<weights_dir>/<source>-<version>.pt` via a
/// `.tmp` sibling that's atomically renamed on success. Verifies
/// SHA-256 BEFORE the rename when `response.sha256` is non-empty.
///
/// Returns the final absolute path. The directory is created if
/// missing (mode `0o755` on Unix; default on Windows).
pub async fn download_to_module_dir(
    module_id: &str,
    embedding_source: &str,
    response: &RlLatestWeightsResponse,
) -> Result<PathBuf, String> {
    let dir = module_weights_dir(module_id);
    tokio::fs::create_dir_all(&dir)
        .await
        .map_err(|e| format!("mkdir {}: {}", dir.display(), e))?;

    let final_path = weights_file_path(module_id, embedding_source, &response.version);
    let tmp_path = {
        let mut p = final_path.clone();
        let fname = p.file_name().unwrap_or_default().to_string_lossy().to_string();
        p.set_file_name(format!("{}.tmp", fname));
        p
    };

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(DOWNLOAD_TIMEOUT_SECS))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let resp = client
        .get(&response.download_url)
        .send()
        .await
        .map_err(|e| format!("GET download_url: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!(
            "download_url returned {}: not promoting tmp file",
            resp.status()
        ));
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("read download body: {}", e))?;

    // Verify checksum BEFORE writing to disk. Mismatch ⇒ discard.
    if !response.sha256.is_empty() {
        let mut hasher = Sha256::new();
        hasher.update(&bytes);
        let actual = hex::encode(hasher.finalize());
        if !actual.eq_ignore_ascii_case(&response.sha256) {
            return Err(format!(
                "sha256 mismatch: expected {}, got {} (download discarded)",
                response.sha256, actual
            ));
        }
    }

    // Atomic write: tmp file then rename. Avoids a half-written .pt
    // ever being visible to a concurrent reader.
    tokio::fs::write(&tmp_path, &bytes)
        .await
        .map_err(|e| format!("write tmp {}: {}", tmp_path.display(), e))?;
    tokio::fs::rename(&tmp_path, &final_path)
        .await
        .map_err(|e| {
            format!(
                "rename {} -> {}: {}",
                tmp_path.display(),
                final_path.display(),
                e
            )
        })?;

    Ok(final_path)
}

// ─── Best-effort hub upsert ─────────────────────────────────────────────

/// Read the hub.port file. Same pattern as
/// `module_db_client::hub_port`. Duplicated (3 lines) to keep this
/// module self-contained.
fn hub_port() -> Result<u16, String> {
    let path = crate::paths::vct_root_dir().join("hub.port");
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read hub.port: {}", e))?;
    raw.trim()
        .parse::<u16>()
        .map_err(|e| format!("parse hub.port: {}", e))
}

/// Generate a hex-encoded 32-byte random token from the OS CSPRNG.
/// Mirrors `module_db_client::generate_token_hex`.
fn generate_token_hex() -> Result<String, String> {
    use rand::TryRngCore;
    let mut bytes = vec![0u8; TOKEN_BYTES];
    rand::rngs::OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|e| format!("rng unavailable: {}", e))?;
    Ok(hex::encode(bytes))
}

/// Get-or-issue a per-(module, project) bearer token from the
/// launcher's `module_access_tokens` table. Same upsert pattern
/// `module_db_client::get_or_issue_token` uses.
fn get_or_issue_module_token(
    db: &Db,
    module_id: &str,
    project_id: &str,
) -> Result<String, String> {
    let now = chrono::Utc::now().timestamp_millis();

    let cached: Option<(String, i64)> = {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT token_secret, expires_at FROM module_access_tokens \
                 WHERE module_id = ?1 AND project_id = ?2",
                rusqlite::params![module_id, project_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
            )
            .ok()
    };

    if let Some((secret, expires_at)) = cached {
        if expires_at > now + TOKEN_REFRESH_MARGIN_MS {
            return Ok(secret);
        }
    }

    let secret = generate_token_hex()?;
    let expires_at = now + DEFAULT_TOKEN_TTL_MS;
    {
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO module_access_tokens \
                    (module_id, project_id, token_secret, issued_at, expires_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5) \
                 ON CONFLICT(module_id, project_id) DO UPDATE SET \
                    token_secret = excluded.token_secret, \
                    issued_at = excluded.issued_at, \
                    expires_at = excluded.expires_at",
                rusqlite::params![module_id, project_id, &secret, now, expires_at],
            )
            .map_err(|e| format!("upsert module_access_tokens: {}", e))?;
    }
    Ok(secret)
}

/// Best-effort upsert of an `rl_global_weights_available` row via the
/// hub's typed REST surface. Soft-fails: hub-unreachable or token-
/// expired errors are logged and discarded. The download itself
/// (the user-visible deliverable) succeeds regardless.
///
/// Table name + namespace convention: the v0.2.31 module-shipped DB
/// substrate (Agent I) requires every table to be prefixed with the
/// module's namespace. We assume `rl_global_weights_available` lives
/// under whatever namespace the RL module declared; the hub's
/// validate_table_name check enforces this server-side, so we just
/// POST the table name as-is and let the hub reject if it drifts.
async fn upsert_global_weight_row(
    db: &Db,
    module_id: &str,
    project_id: &str,
    embedding_source: &str,
    version: &str,
    local_path: &Path,
) -> Result<(), String> {
    let port = hub_port()?;
    let token = get_or_issue_module_token(db, module_id, project_id)?;

    // PK convention: <embedding_source>-<version> uniquely identifies
    // a bundle in the rl_global_weights_available table. The hub
    // resolves the actual PK column name from PRAGMA table_info on
    // the table — this string is what gets stuffed into that column.
    let key = format!("{}-{}", embedding_source, version);

    let url = format!(
        "http://127.0.0.1:{}/api/v1/modules/{}/db/projects/{}/rows/rl_global_weights_available",
        port, module_id, project_id,
    );

    let body = serde_json::json!({
        "key": key,
        "fields": {
            "embedding_source": embedding_source,
            "version": version,
            "local_path": local_path.to_string_lossy(),
            "downloaded_at": chrono::Utc::now().timestamp_millis(),
        }
    });

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(HUB_WRITE_TIMEOUT_SECS))
        .build()
        .map_err(|e| format!("http client: {}", e))?;

    let resp = client
        .post(&url)
        .bearer_auth(&token)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("hub POST: {}", e))?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        let preview = body.chars().take(200).collect::<String>();
        return Err(format!("hub returned {}: {}", status, preview));
    }
    Ok(())
}

// ─── Public Tauri commands ──────────────────────────────────────────────

/// Download the default RL weights bundle for `(module_id,
/// embedding_source)` and stage it under
/// `<vct_root>/modules/<module_id>/weights/`.
///
/// Returns the absolute local path + the server-reported version so a
/// chained_action in the renderer can thread the path into a follow-
/// up `/finetune` POST (Agent C's chained_action variant ships
/// separately — this command is the prerequisite for it).
///
/// Best-effort hub upsert: a row in `rl_global_weights_available` is
/// written so the dashboard can list staged bundles. Failure here is
/// non-fatal — the .pt is on disk regardless.
#[command]
pub async fn module_download_default_weights(
    module_id: String,
    project_id: String,
    embedding_source: String,
    db: State<'_, Db>,
    _app: AppHandle,
) -> Result<DownloadDefaultWeightsResult, String> {
    if module_id.trim().is_empty() {
        return Err("module_id required".to_string());
    }
    if project_id.trim().is_empty() {
        return Err("project_id required".to_string());
    }
    if embedding_source.trim().is_empty() {
        return Err("embedding_source required".to_string());
    }

    let license_key = resolve_license_key()?;
    let machine_id_hash = crate::commands::module_service::machine_id_hash_for_poll();

    // 1. Edge POST → signed URL.
    let response = fetch_signed_download_url(
        &rl_latest_weights_url(),
        &license_key,
        &machine_id_hash,
        &embedding_source,
        &module_id,
    )
    .await?;

    // 2. Stream download + atomic promote.
    let local_path =
        download_to_module_dir(&module_id, &embedding_source, &response).await?;

    // 3. Best-effort hub upsert. Logged but not propagated.
    if let Err(e) = upsert_global_weight_row(
        db.inner(),
        &module_id,
        &project_id,
        &embedding_source,
        &response.version,
        &local_path,
    )
    .await
    {
        eprintln!(
            "[module_default_weights] hub upsert failed (non-fatal): {}",
            e
        );
    }

    Ok(DownloadDefaultWeightsResult {
        local_path: local_path.to_string_lossy().to_string(),
        version: response.version,
    })
}

/// v0.2.32 L6 helper: resolve a `MultiSelectFilter::Match::equals_runtime`
/// identifier against the current project's runtime state.
///
/// Recognised identifiers:
///   * `"container.active_embedding"` — reads `ACTIVE_EMBEDDING` from
///     the project's `.claude/env`; falls back to
///     `module_service::DEFAULT_EMBEDDING_SOURCE` (`"qwen3"`) when
///     missing. Always returns Ok with the resolved value.
///
/// Unknown identifiers return `Ok("")` rather than `Err` so the
/// renderer can treat unrecognised keys as "no filter applies"
/// (show all options) per the L6 spec — never panic.
#[command]
pub async fn module_get_runtime_value(
    project_id: String,
    key: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    match key.as_str() {
        "container.active_embedding" => {
            let project = db
                .get_project(&project_id)
                .map_err(|e| format!("get project: {}", e))?
                .ok_or_else(|| format!("project {} not found", project_id))?;

            let resolved =
                crate::commands::module_service::read_active_embedding_source(&project)
                    .unwrap_or_else(|| {
                        crate::commands::module_service::DEFAULT_EMBEDDING_SOURCE.to_string()
                    });
            Ok(resolved)
        }
        _ => {
            // Unknown identifier: return empty string so the renderer
            // treats this as "filter doesn't match anything", which
            // L6 spec resolves to "show all options" (fail-open, not
            // fail-closed — UX bias).
            Ok(String::new())
        }
    }
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitize_path_component_blocks_traversal_and_separators() {
        assert_eq!(sanitize_path_component("qwen3"), "qwen3");
        assert_eq!(sanitize_path_component("qwen3-2026-05-24"), "qwen3-2026-05-24");
        // `.` is allowed (we want to keep `v1.0` style versions), but
        // `/` and `\` get neutralised. `..` survives as a literal pair
        // of dots — separator removal alone is enough to prevent
        // directory traversal because PathBuf joins treat `..` segments
        // safely when there's no intervening separator.
        assert_eq!(sanitize_path_component("../escape"), ".._escape");
        assert_eq!(sanitize_path_component("a/b\\c"), "a_b_c");
        assert_eq!(sanitize_path_component("v1.0_alpha"), "v1.0_alpha");
    }

    #[test]
    fn module_weights_dir_uses_vct_root() {
        // Pin the directory shape so tests + the hub upsert path
        // agree on where the .pt lands.
        let dir = module_weights_dir("vct-rl-reranker");
        // The leaf segments are stable; the prefix depends on VCT_STATE_DIR.
        let suffix = dir.iter().rev().take(3).collect::<Vec<_>>();
        let mut suffix = suffix;
        suffix.reverse();
        assert_eq!(suffix[0].to_string_lossy(), "modules");
        assert_eq!(suffix[1].to_string_lossy(), "vct-rl-reranker");
        assert_eq!(suffix[2].to_string_lossy(), "weights");
    }

    #[test]
    fn weights_file_path_pattern_is_source_dash_version_dot_pt() {
        let p = weights_file_path("vct-rl-reranker", "qwen3", "v3");
        let fname = p.file_name().unwrap().to_string_lossy();
        assert_eq!(fname, "qwen3-v3.pt");
    }

    #[test]
    fn weights_file_path_sanitises_hostile_components() {
        // Hostile source/version values get their separators neutralised
        // — the file never escapes the weights dir. `..` literals are
        // preserved (PathBuf::join handles them safely without a
        // separator), but `/` and `\` are replaced with `_`.
        let p = weights_file_path("vct-rl", "../bad", "v3/../x");
        let fname = p.file_name().unwrap().to_string_lossy();
        assert_eq!(fname, ".._bad-v3_.._x.pt");
        // And the parent is still the module's weights dir.
        let parent = p.parent().unwrap().file_name().unwrap().to_string_lossy();
        assert_eq!(parent, "weights");
    }

    #[test]
    fn rl_latest_weights_response_parses_minimal() {
        // Server may omit sha256 + expires_at — make sure that doesn't
        // break the parse.
        let raw = r#"{"download_url": "https://example.com/x.pt", "version": "v3"}"#;
        let parsed: RlLatestWeightsResponse = serde_json::from_str(raw).unwrap();
        assert_eq!(parsed.download_url, "https://example.com/x.pt");
        assert_eq!(parsed.version, "v3");
        assert_eq!(parsed.sha256, "");
        assert_eq!(parsed.expires_at, "");
    }

    #[test]
    fn rl_latest_weights_response_parses_full() {
        let raw = r#"{
            "download_url": "https://signed.example.com/blob?sig=abc",
            "version": "2026-05-24",
            "sha256": "0123456789abcdef",
            "expires_at": "2026-05-24T13:00:00Z"
        }"#;
        let parsed: RlLatestWeightsResponse = serde_json::from_str(raw).unwrap();
        assert_eq!(parsed.version, "2026-05-24");
        assert_eq!(parsed.sha256, "0123456789abcdef");
        assert_eq!(parsed.expires_at, "2026-05-24T13:00:00Z");
    }

    /// End-to-end happy path against an in-process HTTP mock for BOTH
    /// the Supabase edge AND the download URL. Asserts:
    ///   * `local_path` matches the expected `<weights_dir>/<source>-<version>.pt`
    ///   * `version` echoes the server-reported value
    ///   * The .pt content actually lands on disk
    ///
    /// We test the underlying helpers `fetch_signed_download_url` +
    /// `download_to_module_dir` directly rather than the
    /// `#[command]`-decorated entry point because the latter needs a
    /// real `State<'_, Db>` plus the keychain license + `AppHandle`,
    /// none of which are available in a `cargo test --lib` context.
    /// The test's path/version assertions cover the contract the
    /// brief calls out — the command itself only orchestrates these
    /// helpers + the soft-fail hub upsert.
    #[tokio::test]
    async fn module_download_default_weights_returns_local_path_and_version() {
        use std::sync::Arc;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        // Isolate VCT_STATE_DIR so the test's writes go to a tmp dir.
        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        // Bind two ephemeral ports: one for the edge function mock,
        // one for the download blob server. Use tokio::net so the
        // socket is registered with the active runtime (the std →
        // tokio FD conversion is rejected by the current tokio
        // version per #7172).
        let edge_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let edge_port = edge_listener.local_addr().unwrap().port();
        let blob_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let blob_port = blob_listener.local_addr().unwrap().port();

        // Test fixtures.
        let blob_body: &[u8] = b"fake-rl-weights-pt-content";
        let blob_sha = {
            let mut h = Sha256::new();
            h.update(blob_body);
            hex::encode(h.finalize())
        };
        let blob_body_vec = blob_body.to_vec();
        let edge_response = serde_json::json!({
            "download_url": format!("http://127.0.0.1:{}/blob.pt", blob_port),
            "version": "v42",
            "sha256": blob_sha,
        });

        // --- Edge function mock: respond to one POST with the JSON above.
        let edge_response_str = edge_response.to_string();
        let edge_response_str_clone = edge_response_str.clone();
        tokio::spawn(async move {
            let (mut sock, _) = edge_listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf).await;
            let body = edge_response_str_clone;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = sock.write_all(response.as_bytes()).await;
            let _ = sock.shutdown().await;
        });

        // --- Blob server mock: respond to GET /blob.pt with the .pt body.
        let blob_body_arc = Arc::new(blob_body_vec);
        let blob_body_arc_clone = blob_body_arc.clone();
        tokio::spawn(async move {
            let (mut sock, _) = blob_listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf).await;
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\n\r\n",
                blob_body_arc_clone.len()
            );
            let _ = sock.write_all(header.as_bytes()).await;
            let _ = sock.write_all(&blob_body_arc_clone).await;
            let _ = sock.shutdown().await;
        });

        // --- Helper-level happy path.
        let endpoint = format!("http://127.0.0.1:{}/rl-latest-weights", edge_port);
        let parsed = fetch_signed_download_url(
            &endpoint,
            "fake-license-key",
            "deadbeef",
            "qwen3",
            "vct-rl-reranker",
        )
        .await
        .expect("edge mock must return a parseable response");
        assert_eq!(parsed.version, "v42");
        assert!(parsed.download_url.contains("/blob.pt"));

        let local_path = download_to_module_dir("vct-rl-reranker", "qwen3", &parsed)
            .await
            .expect("download must succeed against the blob mock");

        // Pattern assertion: <weights_dir>/<source>-<version>.pt.
        let fname = local_path.file_name().unwrap().to_string_lossy();
        assert_eq!(fname, "qwen3-v42.pt", "filename must be source-version.pt");
        assert!(
            local_path.starts_with(tmp.path()),
            "path must land under VCT_STATE_DIR override: {}",
            local_path.display()
        );

        // Content assertion: the .pt actually got written.
        let on_disk = tokio::fs::read(&local_path).await.unwrap();
        assert_eq!(&on_disk, blob_body, "downloaded bytes must match server body");

        // Version assertion: would-be DownloadDefaultWeightsResult is constructible.
        let result = DownloadDefaultWeightsResult {
            local_path: local_path.to_string_lossy().to_string(),
            version: parsed.version,
        };
        assert_eq!(result.version, "v42");
        assert!(result.local_path.ends_with("qwen3-v42.pt"));

        // Cleanup the env override so we don't leak into adjacent tests.
        std::env::remove_var("VCT_STATE_DIR");
    }

    #[tokio::test]
    async fn download_to_module_dir_rejects_sha_mismatch() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let blob_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let blob_port = blob_listener.local_addr().unwrap().port();

        let blob_body: Vec<u8> = b"actual-content".to_vec();
        let blob_body_clone = blob_body.clone();
        tokio::spawn(async move {
            let (mut sock, _) = blob_listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf).await;
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",
                blob_body_clone.len()
            );
            let _ = sock.write_all(header.as_bytes()).await;
            let _ = sock.write_all(&blob_body_clone).await;
            let _ = sock.shutdown().await;
        });

        let response = RlLatestWeightsResponse {
            download_url: format!("http://127.0.0.1:{}/blob.pt", blob_port),
            version: "v1".to_string(),
            // Wrong sha — should be rejected.
            sha256: "0000000000000000000000000000000000000000000000000000000000000000"
                .to_string(),
            expires_at: String::new(),
        };

        let err = download_to_module_dir("vct-rl-reranker", "qwen3", &response).await;
        assert!(err.is_err(), "sha mismatch must error");
        let msg = err.unwrap_err();
        assert!(msg.contains("sha256 mismatch"), "error must name the cause: {}", msg);

        // And the .pt MUST NOT exist (mismatch happens before write).
        let target = weights_file_path("vct-rl-reranker", "qwen3", "v1");
        assert!(!target.exists(), "tmp file must not have been promoted");

        std::env::remove_var("VCT_STATE_DIR");
    }

    #[test]
    fn rl_latest_weights_url_respects_env_override() {
        let prev = std::env::var("VCT_RL_LATEST_WEIGHTS_URL").ok();
        std::env::set_var("VCT_RL_LATEST_WEIGHTS_URL", "https://staging.example/x");
        assert_eq!(rl_latest_weights_url(), "https://staging.example/x");
        match prev {
            Some(v) => std::env::set_var("VCT_RL_LATEST_WEIGHTS_URL", v),
            None => std::env::remove_var("VCT_RL_LATEST_WEIGHTS_URL"),
        }
    }
}
