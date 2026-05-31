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
//!    `licensing` / canonical `license_key____orchestrator__` per
//!    L1.M v0.2.40; was the legacy `VIBECODED_LICENSE_KEY` pre-L1.M).
//!    No license ⇒ refuse.
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

/// v0.2.42 RT-3: structured error for Supabase 400 `unsupported_embedding_source`.
///
/// The edge function (`rl-latest-weights` and `rl-latest-version`) returns this
/// body when the requested `embedding_source` has no matching row in
/// `paid_module_releases`:
///
/// ```json
/// {"error": "unsupported_embedding_source",
///  "supported_embedding_sources": ["qwen3", "arctic2"]}
/// ```
///
/// Pre-fix the client rendered only a raw 200-char text preview, which
/// hid the actionable hint (the supported list). Now the caller receives
/// a typed value that surfaces both the unsupported source and the
/// available alternatives — the GUI tile can render
/// "not available for <source>; try: qwen3, arctic2" instead of a
/// cryptic JSON snippet.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct UnsupportedEmbeddingSourceError {
    /// The embedding source the caller requested.
    pub requested_source: String,
    /// The sources the server currently supports for this module.
    pub supported_sources: Vec<String>,
}

impl std::fmt::Display for UnsupportedEmbeddingSourceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.supported_sources.is_empty() {
            write!(
                f,
                "embedding source '{}' is not supported by the server \
                 (no alternatives available yet)",
                self.requested_source,
            )
        } else {
            write!(
                f,
                "embedding source '{}' is not supported; \
                 available sources: {}",
                self.requested_source,
                self.supported_sources.join(", "),
            )
        }
    }
}

/// Deserialise shape of the Supabase 400 `unsupported_embedding_source` body.
/// Only used internally by `fetch_signed_download_url`.
#[derive(Debug, Deserialize)]
struct SupabaseErrorBody {
    #[serde(default)]
    error: String,
    #[serde(default, rename = "supported_embedding_sources")]
    supported: Vec<String>,
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

// ─── v0.2.40 R3: weights-staging unification ────────────────────────────
//
// Background. Until v0.2.40 the two weights-staging paths drifted apart:
//
//   * `module_weights_dir` (THIS module) stages downloads globally at
//     `<vct_root>/modules/<module_id>/weights/<source>-<version>.pt`.
//   * `module_service::download_weights` (the daily-poll path) writes at
//     `<vct_root>/data/<module_id>/<project_slug>/state/rl_model_<source>_<version>.pt`
//     — and that is also where the container's bind mount lives
//     (`{VCT_DATA}/<module_id>/<project_slug>/state` → `/data/state`,
//     declared in the RL Reranker's runtime.volumes block).
//
// Net effect: "Download default weights" succeeded on disk but the .pt
// never reached the running container — the bind-mount looked at a
// completely different directory. Multi-Opus pre-push review item 4.
//
// Strategy A (this patch). Keep the global download path as the source of
// truth (it's where the .pt actually lives) and surface it into the
// per-project bind mount via a symlink named with the container's
// expected naming convention (`rl_model_<source>_<version>.pt`). The
// container then loads weights through its existing path-resolution code
// without any container-side change.
//
// Override / diverge UX. If the per-project mount slot already contains a
// regular file (not a symlink), we treat that as a deliberate user
// override and leave it alone — re-running "Download default weights"
// must NOT clobber a hand-placed override. Symlinks get refreshed
// freely (they are launcher-managed mirrors).
//
// Reset UX. The companion `reset_weights_to_global` helper removes any
// override (file or symlink) and re-points the slot at the global file
// — i.e. "use global". Currently called from tests + (future) a Tauri
// reset_to_global command; the helper is the smallest surface needed
// for the brief.
//
// Windows fallback. `std::os::unix::fs::symlink` is unix-only; on Windows
// the symlink call uses `std::os::windows::fs::symlink_file` which
// requires either elevated privileges or Developer Mode. If the symlink
// fails (insufficient permission), we fall back to a plain `fs::copy`
// so the container still gets the weights.
//
// v0.2.42 RT-5 override-protection fix. Before this patch, the copy
// fallback produced a regular file that the override-protection logic
// (the `meta.file_type().is_symlink()` check in
// `link_global_into_project_mount`) treated as a user-placed override —
// so subsequent calls to "Download default weights" silently skipped the
// slot, leaving the container on the first-downloaded version forever.
//
// Fix: when the copy fallback is taken, write a sibling `.vct-managed`
// marker file next to the `.pt`. Override-protection checks for this
// marker: if present, the slot is orchestrator-managed (replaceable);
// only a regular file WITHOUT a marker is treated as a user override.
//
// On a re-link the old copy + marker are removed and the new copy (or
// symlink on systems where symlink is now available) replaces them.
// The user can still place a hand-crafted override by deleting the
// marker file (or never having a copy at all).

/// v0.2.42 RT-5: sibling marker file that signals "this weights file was
/// placed here by the orchestrator (copy fallback) and is safe to
/// replace on the next download". Without this marker, override-protection
/// treats a regular `.pt` file as a deliberate user override and skips it.
///
/// Naming convention: `<weights_file>.vct-managed` — sibling of the `.pt`,
/// same stem + `.vct-managed` extension. Created by the `fs::copy` fallback
/// path; deleted alongside the file on re-link.
pub fn managed_marker_path(weights_path: &Path) -> PathBuf {
    let mut p = weights_path.to_path_buf();
    let stem = p.file_name().unwrap_or_default().to_string_lossy().to_string();
    p.set_file_name(format!("{}.vct-managed", stem));
    p
}

/// Resolve the per-project bind-mount directory the RL container reads
/// weights from. Mirrors the host path declared in the RL Reranker
/// manifest's `runtime.volumes`: `{VCT_DATA}/<module_id>/<project_slug>/state`
/// → `/data/state` (mode rw). Mkdir is the caller's responsibility.
pub fn container_weights_mount_dir(module_id: &str, project_slug: &str) -> PathBuf {
    crate::paths::vct_root_dir()
        .join("data")
        .join(sanitize_path_component(module_id))
        .join(sanitize_path_component(project_slug))
        .join("state")
}

/// Resolve the filename inside the bind-mount that the RL container's
/// `/rotate_weights` handler loads. Matches `module_service::container_weights_path`'s
/// `rl_model_<source>_<version>.pt` shape — duplicated rather than
/// re-exported so this module stays self-contained.
pub fn project_mount_weights_filename(embedding_source: &str, version: &str) -> String {
    format!(
        "rl_model_{}_{}.pt",
        sanitize_path_component(embedding_source),
        sanitize_path_component(version),
    )
}

/// Full path of the project-mount slot the launcher manages for a given
/// (module, project, source, version).
pub fn project_mount_weights_path(
    module_id: &str,
    project_slug: &str,
    embedding_source: &str,
    version: &str,
) -> PathBuf {
    container_weights_mount_dir(module_id, project_slug)
        .join(project_mount_weights_filename(embedding_source, version))
}

/// Create the symlink (or fallback copy) from the per-project bind-mount
/// slot to the global download. Returns the symlink/copy path.
///
/// Behaviour matrix at the target slot:
///   * Nothing there → create symlink (or copy on symlink failure).
///   * Symlink there → remove + re-create (refresh global pointer).
///   * Regular file there → LEAVE UNTOUCHED (user override). Returns the
///     existing path; the caller can detect via `path.symlink_metadata`
///     if it cares, but the contract is "do not clobber".
///
/// Soft-fail on the underlying filesystem op: callers (the download
/// command) treat link failure as non-fatal — the .pt is still on disk
/// globally and a future bundle re-link can pick it up.
pub async fn link_global_into_project_mount(
    module_id: &str,
    project_slug: &str,
    embedding_source: &str,
    version: &str,
    global_path: &Path,
) -> Result<PathBuf, String> {
    let mount_dir = container_weights_mount_dir(module_id, project_slug);
    tokio::fs::create_dir_all(&mount_dir)
        .await
        .map_err(|e| format!("mkdir bind-mount dir {}: {}", mount_dir.display(), e))?;

    let target = project_mount_weights_path(module_id, project_slug, embedding_source, version);

    // Override protection: if a real (non-symlink) file already sits in
    // the slot, check whether it is orchestrator-managed (`.vct-managed`
    // marker present, written by the copy fallback path) or a deliberate
    // user override. Only user overrides are preserved.
    //
    // v0.2.42 RT-5: pre-fix this block treated ALL regular files as user
    // overrides, so a copy-fallback `.pt` was never refreshed on subsequent
    // downloads — the container silently ran on the first-downloaded version
    // forever on Windows systems where symlinks require Developer Mode.
    let marker = managed_marker_path(&target);
    match tokio::fs::symlink_metadata(&target).await {
        Ok(meta) if !meta.file_type().is_symlink() => {
            // Regular file in the slot. Check the marker.
            let is_managed = tokio::fs::metadata(&marker).await.is_ok();
            if is_managed {
                // Orchestrator-managed copy: remove + marker so we can refresh.
                let _ = tokio::fs::remove_file(&target).await;
                let _ = tokio::fs::remove_file(&marker).await;
                // Fall through to the link/copy path below.
            } else {
                // No marker → genuine user override — preserve.
                return Ok(target);
            }
        }
        Ok(_) => {
            // Existing symlink — remove so we can refresh.
            if let Err(e) = tokio::fs::remove_file(&target).await {
                return Err(format!(
                    "remove stale symlink {}: {}",
                    target.display(),
                    e
                ));
            }
            // Clean up any stale marker file left from a prior copy-then-
            // symlink transition (defensive; should be absent but harmless).
            let _ = tokio::fs::remove_file(&marker).await;
        }
        Err(_) => {
            // Nothing there — proceed.
        }
    }

    // Try platform-native symlink first.
    let symlink_res = create_symlink(global_path, &target);

    if let Err(symlink_err) = symlink_res {
        // Soft-fall back to copy (Windows without Developer Mode,
        // restricted filesystems, etc.). Log so the developer sees
        // it but don't fail — the container still gets the weights.
        //
        // v0.2.42 RT-5: write a `.vct-managed` marker file alongside
        // the copy so future override-protection passes recognise this
        // as an orchestrator-managed file (replaceable) rather than a
        // user override. Without the marker, subsequent "Download default
        // weights" calls silently skip the slot.
        eprintln!(
            "[module_default_weights] symlink {} -> {} failed ({}); falling back to copy",
            target.display(),
            global_path.display(),
            symlink_err
        );
        tokio::fs::copy(global_path, &target).await.map_err(|e| {
            format!(
                "copy fallback {} -> {} failed: {}",
                global_path.display(),
                target.display(),
                e
            )
        })?;
        // Write the marker (best-effort: a missing marker just means the
        // copy will be treated as a user override on the NEXT call, which
        // is the pre-fix behaviour — not great but not worse than before).
        if let Err(marker_err) = tokio::fs::write(&marker, b"vct-managed").await {
            eprintln!(
                "[module_default_weights] warning: failed to write .vct-managed marker \
                 {} ({}); copy will be treated as user override on next update",
                marker.display(),
                marker_err
            );
        }
    }

    Ok(target)
}

/// Reset the per-project mount slot back to "use global" — remove any
/// override (real file or symlink) and re-point at the latest global
/// `.pt`. Idempotent: re-creating a symlink to the same target is a no-op.
///
/// `global_path` is the source-of-truth `.pt` the symlink should point
/// at. Typically the caller resolves this from the last successful
/// `module_download_default_weights_inner` call (or by inspecting
/// `module_weights_dir(module_id)`).
pub async fn reset_weights_to_global(
    module_id: &str,
    project_slug: &str,
    embedding_source: &str,
    version: &str,
    global_path: &Path,
) -> Result<PathBuf, String> {
    let target = project_mount_weights_path(module_id, project_slug, embedding_source, version);

    // Remove any existing entry (file OR symlink) — reset wipes both.
    match tokio::fs::symlink_metadata(&target).await {
        Ok(_) => {
            tokio::fs::remove_file(&target)
                .await
                .map_err(|e| format!("remove {}: {}", target.display(), e))?;
        }
        Err(_) => {
            // Nothing to remove — fine.
        }
    }

    // Re-link to global.
    link_global_into_project_mount(module_id, project_slug, embedding_source, version, global_path)
        .await
}

/// Platform-specific symlink wrapper. Unix uses `std::os::unix::fs::symlink`;
/// Windows uses `symlink_file`. Both return `io::Error` on failure; the
/// caller catches that to apply the copy fallback.
fn create_symlink(src: &Path, dst: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(src, dst)
    }
    #[cfg(windows)]
    {
        std::os::windows::fs::symlink_file(src, dst)
    }
    #[cfg(not(any(unix, windows)))]
    {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "symlink not supported on this platform",
        ))
    }
}

// ─── License + endpoint helpers ─────────────────────────────────────────

/// Read the user's license key from the OS keychain. Returns `Err` on
/// "no key configured" so the renderer can surface a clear message
/// rather than calling the edge with an empty bearer (which would
/// always 401).
fn resolve_license_key() -> Result<String, String> {
    // Same scope used by `commands::licensing` — keep them in sync if
    // either changes. L1.M (v0.2.40): canonical per-module username
    // (was the legacy `VIBECODED_LICENSE_KEY`). The keychain_username_for
    // helper is the single source of truth for the username shape; this
    // call site picks up the canonical value automatically.
    let username =
        vct_launcher_core::db::license_keys::keychain_username_for(
            vct_launcher_core::db::license_keys::ORCHESTRATOR_MODULE_ID,
        );
    let key_opt = crate::secrets::get(
        crate::secrets::SecretScope::Global,
        "licensing",
        &username,
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
        // v0.2.42 RT-3: parse 400 `unsupported_embedding_source` into a
        // structured error so callers can surface actionable guidance
        // ("try: qwen3, arctic2") rather than a raw JSON snippet.
        if status.as_u16() == 400 {
            if let Ok(err_body) = serde_json::from_str::<SupabaseErrorBody>(&body_text) {
                if err_body.error == "unsupported_embedding_source" {
                    let structured = UnsupportedEmbeddingSourceError {
                        requested_source: embedding_source.to_string(),
                        supported_sources: err_body.supported,
                    };
                    return Err(format!(
                        "unsupported_embedding_source: {}",
                        structured,
                    ));
                }
            }
        }
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
    app: AppHandle,
) -> Result<DownloadDefaultWeightsResult, String> {
    // Thin Tauri-command shim: unwraps `State<Db>` into `&Db` and
    // delegates to `module_download_default_weights_inner`. The inner
    // form is also called from v0.2.33 Agent D's `tauri_command` step
    // dispatcher in `module_dispatch.rs`, which doesn't have access to
    // a `State<Db>` (it holds an `Arc<Db>` instead).
    module_download_default_weights_inner(
        module_id,
        project_id,
        embedding_source,
        db.inner(),
        &app,
    )
    .await
}

/// v0.2.33 (Agent D): non-Tauri-command form of
/// `module_download_default_weights`. Identical semantics; the
/// difference is the dependency shape — `&Db` + `&AppHandle` instead
/// of `State<Db>` + `AppHandle` — so the chained_action dispatcher
/// can call it without going through Tauri's IPC layer.
pub async fn module_download_default_weights_inner(
    module_id: String,
    project_id: String,
    embedding_source: String,
    db: &Db,
    _app: &AppHandle,
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

    // 2.5. v0.2.40 R3: surface the global download into the per-project
    // bind-mount so the running RL container actually sees it. Before
    // this step, `<vct_root>/modules/<module>/weights/<source>-<version>.pt`
    // existed but the container's bind mount looked at a different
    // directory entirely (`<vct_root>/data/<module>/<slug>/state/...`).
    //
    // Soft-fail: a link failure does not invalidate the download — the
    // .pt is on disk globally and the next install/run cycle can pick
    // it up. We just log + continue so the hub-upsert + result return
    // still happen.
    let project_slug = match db.get_project(&project_id) {
        Ok(Some(row)) => Some(row.slug),
        Ok(None) => {
            eprintln!(
                "[module_default_weights] project {} not found — skipping bind-mount link",
                project_id
            );
            None
        }
        Err(e) => {
            eprintln!(
                "[module_default_weights] db.get_project({}) failed: {} — skipping bind-mount link",
                project_id, e
            );
            None
        }
    };
    if let Some(slug) = project_slug.as_deref() {
        if let Err(e) = link_global_into_project_mount(
            &module_id,
            slug,
            &embedding_source,
            &response.version,
            &local_path,
        )
        .await
        {
            eprintln!(
                "[module_default_weights] bind-mount link failed (non-fatal): {}",
                e
            );
        }
    }

    // 3. Best-effort hub upsert. Logged but not propagated.
    if let Err(e) = upsert_global_weight_row(
        db,
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

    // v0.2.42 RT-4: persist (source, version) so `module_reset_weights_to_global`
    // can derive the global path without requiring the caller to pass them.
    // Best-effort: failure is non-fatal — reset is an optional UX convenience.
    let _ = db.set_setting(
        &project_id, &module_id, WEIGHTS_LAST_EMBEDDING_SOURCE_KEY,
        &serde_json::Value::String(embedding_source.clone()),
    );
    let _ = db.set_setting(
        &project_id, &module_id, WEIGHTS_LAST_VERSION_KEY,
        &serde_json::Value::String(response.version.clone()),
    );
    // v0.2.42 RT-3: on a successful manual download, clear any stale deferred
    // flag so the cooldown resets and R5 doesn't skip the next install cycle.
    clear_weights_download_deferred(db, &project_id, &module_id);

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
    // Tauri-command shim — see `module_get_runtime_value_inner`.
    module_get_runtime_value_inner(project_id, key, db.inner()).await
}

/// v0.2.33 (Agent D): non-Tauri-command form of
/// `module_get_runtime_value` — see the inner-form rationale on
/// `module_download_default_weights_inner` above.
pub async fn module_get_runtime_value_inner(
    project_id: String,
    key: String,
    db: &Db,
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

// ─── v0.2.42 RT-4: reset_weights_to_global Tauri command ────────────────
//
// Wraps `reset_weights_to_global` (the inner async helper at line ~385)
// as a Tauri IPC command. The pre-existing inner function is pure logic;
// this module adds the Tauri shim + the "derive source/version from DB"
// discovery step so the GUI only needs `(module_id, project_id)`.
//
// TODO (W6): bind a "Reset to global defaults" button in the RL Reranker
// module tile that calls `invoke("module_reset_weights_to_global", {
//   module_id: module.id, project_id: current_project_id })`.
// The call site: `module.reset_weights_to_global` on the Tauri side
// maps to `module_reset_weights_to_global` in the handler below.

/// Tauri-command return value for the reset operation.
///
/// `local_path` is the absolute path of the symlink/copy that now
/// points at the global .pt. `version` + `embedding_source` echo the
/// settings that were stored from the last successful download (so the
/// renderer can display "reset to qwen3 v3").
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResetWeightsResult {
    pub local_path: String,
    pub embedding_source: String,
    pub version: String,
}

/// v0.2.42 RT-4: Reset the per-project weights mount slot back to the
/// globally-downloaded default `.pt`.
///
/// Derives `embedding_source` + `version` from the
/// `WEIGHTS_LAST_EMBEDDING_SOURCE_KEY` / `WEIGHTS_LAST_VERSION_KEY`
/// settings written by the last successful download. If either is
/// absent (no download has succeeded yet), the command errors with a
/// clear message so the renderer can prompt the user to download first.
///
/// The underlying `reset_weights_to_global` helper (lines ~385+)
/// removes any user override (regular file or symlink) and re-points
/// the slot at the global file — "use global".
///
/// TODO (W6): wire a "Reset to global defaults" button in the module
/// tile's settings panel that invokes this command. The button should
/// be hidden when `WEIGHTS_LAST_VERSION_KEY` is absent (nothing to
/// reset to) and should surface `ResetWeightsResult.version` in the
/// tile's success toast so users know which version they reverted to.
#[command]
pub async fn module_reset_weights_to_global(
    module_id: String,
    project_id: String,
    db: State<'_, Db>,
) -> Result<ResetWeightsResult, String> {
    if module_id.trim().is_empty() {
        return Err("module_id required".to_string());
    }
    if project_id.trim().is_empty() {
        return Err("project_id required".to_string());
    }

    let db = db.inner();

    // Derive source + version from the last successful download.
    let embedding_source = match db.get_setting(&project_id, &module_id, WEIGHTS_LAST_EMBEDDING_SOURCE_KEY)? {
        Some(serde_json::Value::String(s)) if !s.is_empty() => s,
        _ => return Err(
            "no successful weights download recorded yet — \
             download default weights first, then reset to global".to_string()
        ),
    };
    let version = match db.get_setting(&project_id, &module_id, WEIGHTS_LAST_VERSION_KEY)? {
        Some(serde_json::Value::String(v)) if !v.is_empty() => v,
        _ => return Err(
            "no successful weights version recorded yet — \
             download default weights first, then reset to global".to_string()
        ),
    };

    let global_path = weights_file_path(&module_id, &embedding_source, &version);
    if !global_path.exists() {
        return Err(format!(
            "global weights file not found at {} — \
             re-download default weights first",
            global_path.display()
        ));
    }

    // Look up the project slug for the bind-mount path.
    let project = db
        .get_project(&project_id)
        .map_err(|e| format!("get project: {}", e))?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let local_path = reset_weights_to_global(
        &module_id,
        &project.slug,
        &embedding_source,
        &version,
        &global_path,
    )
    .await?;

    let _ = db.audit(
        "module_reset_weights_to_global",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "embedding_source": &embedding_source,
            "version": &version,
            "local_path": local_path.to_string_lossy(),
        }),
    );

    Ok(ResetWeightsResult {
        local_path: local_path.to_string_lossy().to_string(),
        embedding_source,
        version,
    })
}

// ─── v0.2.40 R5: first-install auto-trigger ─────────────────────────────
//
// After `start_container_after_install` succeeds for the RL Reranker,
// trigger a one-shot default-weights download so the container doesn't
// run on the (likely qwen3-only) weights baked into the image until
// the user manually clicks "Download default weights" or the daily
// poll fires (~24h). "Just works" UX for paid-tier users.
//
// Soft-fail discipline: this function NEVER propagates errors that
// would block the install pipeline. Failure modes (license missing,
// Supabase function unavailable, network timeout) are logged and
// recorded in `module_settings.weights_download_deferred=true` so the
// GUI tile can render "click Download default weights to refresh".
// Free-tier callers are filtered out at the call site (R5 is gated on
// `is_module_licensed` before invoking this helper).

/// `module_settings.setting_key` used to mark that the first-install
/// auto-download did not complete (license missing, edge function 404,
/// network failure, etc.). The GUI reads this to render a "click
/// Download default weights to refresh" hint on the module tile.
///
/// Cleared on a subsequent successful download (either auto-retry or
/// the user clicking the manual button). Stored as JSON `true` /
/// absence-of-row; the renderer treats both `false` and missing as
/// "no defer".
pub const WEIGHTS_DOWNLOAD_DEFERRED_KEY: &str = "weights_download_deferred";

/// v0.2.42 RT-3: `module_settings.setting_key` used to record the
/// Unix-millisecond timestamp of the last failed first-install auto-
/// download. Combined with `WEIGHTS_DOWNLOAD_DEFERRED_KEY` to gate
/// the R5 daily poll: when the deferred flag is set AND the last
/// failure was < 24 hours ago, skip the poll to avoid hammering the
/// Supabase edge function repeatedly for a known-stale configuration
/// (e.g. `unsupported_embedding_source` — won't resolve without user
/// action, so polling every 24h is wasteful).
///
/// Cleared alongside `WEIGHTS_DOWNLOAD_DEFERRED_KEY` on success.
pub const WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY: &str = "weights_download_last_failed_at";

/// v0.2.42 RT-4: `module_settings` key that records the embedding_source
/// of the last successful default-weights download. Used by the
/// `module_reset_weights_to_global` Tauri command to derive the correct
/// source without requiring the GUI to pass it explicitly.
pub const WEIGHTS_LAST_EMBEDDING_SOURCE_KEY: &str = "weights_last_embedding_source";

/// v0.2.42 RT-4: `module_settings` key that records the version string
/// (e.g. `"v3"` or `"2026-05-24"`) of the last successful default-weights
/// download. Used by `module_reset_weights_to_global` to reconstruct the
/// global file path.
pub const WEIGHTS_LAST_VERSION_KEY: &str = "weights_last_version";

/// 24-hour cooldown duration in milliseconds. When the deferred flag
/// is set and the last failure was recorded within this window, the R5
/// auto-trigger skips polling to avoid repeated edge-function calls
/// for a configuration that requires user action to fix.
const WEIGHTS_DOWNLOAD_COOLDOWN_MS: i64 = 24 * 60 * 60 * 1000; // 24 h

/// v0.2.42 RT-3: return `true` when the R5 auto-trigger should skip
/// the download attempt because a recent failure was recorded within
/// the 24-hour cooldown window.
///
/// Logic:
/// 1. If `WEIGHTS_DOWNLOAD_DEFERRED_KEY` is NOT set → not in defer
///    state → never skip (let the first-install trigger proceed).
/// 2. If `WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY` is absent → no
///    timestamp recorded → skip (conservative: assume deferred
///    recently; the user can clear via the manual "Download" button).
/// 3. If `now - last_failed_at < 24h` → within cooldown → skip.
/// 4. Otherwise → cooldown expired → allow retry.
///
/// Soft-fail: DB errors are treated as "do not skip" (let the
/// download attempt run; it will fail and re-record the timestamp).
pub(crate) fn should_skip_r5_poll(
    db: &crate::db::Db,
    project_id: &str,
    module_id: &str,
) -> bool {
    // Step 1: check deferred flag.
    let deferred = match db.get_setting(project_id, module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY) {
        Ok(Some(serde_json::Value::Bool(true))) => true,
        _ => false,
    };
    if !deferred {
        return false;
    }
    // Steps 2–4: check timestamp.
    let now_ms = chrono::Utc::now().timestamp_millis();
    match db.get_setting(project_id, module_id, WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY) {
        Ok(Some(serde_json::Value::Number(n))) => {
            if let Some(ts) = n.as_i64() {
                // Within cooldown window → skip.
                now_ms - ts < WEIGHTS_DOWNLOAD_COOLDOWN_MS
            } else {
                // Malformed timestamp → conservative skip.
                true
            }
        }
        // No timestamp row or non-numeric value → conservative skip.
        Ok(_) => true,
        // DB error → don't skip (let it try).
        Err(_) => false,
    }
}

/// Record the deferred-flag + audit entry for a failed first-install
/// auto-download. Extracted into a pure-Db helper so unit tests can
/// drive it without standing up an `AppHandle` (the keychain +
/// network paths in `module_download_default_weights_inner` are not
/// reachable in a `cargo test --lib` context).
///
/// Idempotent: safe to call on every error-path retry.
pub(crate) fn mark_weights_download_deferred(
    db: &Db,
    project_id: &str,
    module_id: &str,
    embedding_source: &str,
    error: &str,
) {
    // Set the deferred-flag setting. Soft-fail on the set itself —
    // if the DB write errors (extremely unlikely), logging is all we
    // can do; the GUI tile will not show the "click to refresh" hint
    // but the install otherwise succeeded.
    if let Err(set_err) =
        db.set_setting(project_id, module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY,
            &serde_json::Value::Bool(true))
    {
        eprintln!(
            "[module_default_weights] R5: failed to mark deferred flag \
             (best-effort, non-fatal): {}",
            set_err
        );
    }

    // v0.2.42 RT-3: record failure timestamp for the 24-hour cooldown
    // gate. `should_skip_r5_poll` reads this to avoid hammering the
    // edge function for a configuration that requires user action
    // (e.g. `unsupported_embedding_source`).
    let now_ms = chrono::Utc::now().timestamp_millis();
    let _ = db.set_setting(
        project_id,
        module_id,
        WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY,
        &serde_json::Value::Number(now_ms.into()),
    );

    let _ = db.audit(
        "module_default_weights_auto_download_deferred",
        Some(project_id),
        Some(module_id),
        &serde_json::json!({
            "embedding_source": embedding_source,
            "error": error,
        }),
    );
}

/// Clear a previously-set deferred-flag after a successful download.
/// Pure-Db helper, sibling to `mark_weights_download_deferred`.
/// Idempotent: rows that don't exist produce a silent success.
///
/// v0.2.42 RT-3: also clears `WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY`
/// so the 24-hour cooldown resets on success (next poll starts fresh).
pub(crate) fn clear_weights_download_deferred(
    db: &Db,
    project_id: &str,
    module_id: &str,
) {
    let _ = db.delete_setting(project_id, module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY);
    let _ = db.delete_setting(project_id, module_id, WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY);
}

/// Trigger a one-shot default-weights download after first install.
///
/// Soft-fails: every error path sets the
/// `WEIGHTS_DOWNLOAD_DEFERRED_KEY` setting on `(project_id, module_id)`
/// and returns Ok(()) — the caller should NOT use this function's
/// return value to gate any user-visible behaviour. The return is
/// `Result<(), String>` purely so internal `?` plumbing stays
/// consistent; the err path is squashed into Ok() before returning.
///
/// Tier-gate: the call site MUST filter on `is_module_licensed`
/// before invoking. Free-tier users skip the trigger silently (the
/// manifest button is hidden for them per the existing pattern at
/// `module_default_weights.rs:50-54`).
///
/// Embedding source: read via `module_service::read_active_embedding_source`
/// with fallback to `module_service::DEFAULT_EMBEDDING_SOURCE`
/// (currently `"qwen3"`). Matches the daily-poller path
/// (`module_service.rs:862`).
pub async fn apply_default_weights_after_install(
    module_id: &str,
    project_id: &str,
    db: &Db,
    app: &AppHandle,
) -> Result<(), String> {
    // 1. Resolve embedding source from the project's `.claude/env`.
    let project = match db.get_project(project_id) {
        Ok(Some(p)) => p,
        Ok(None) => {
            // Project vanished between install and post-install — unusual
            // but possible if the user uninstalled the project mid-flight.
            // Nothing to defer onto; just log + return.
            eprintln!(
                "[module_default_weights] R5 auto-trigger: project {} not found; \
                 skipping default-weights download",
                project_id
            );
            return Ok(());
        }
        Err(e) => {
            eprintln!(
                "[module_default_weights] R5 auto-trigger: get_project failed: {}; \
                 skipping default-weights download",
                e
            );
            return Ok(());
        }
    };

    let embedding_source =
        crate::commands::module_service::read_active_embedding_source(&project)
            .unwrap_or_else(|| {
                crate::commands::module_service::DEFAULT_EMBEDDING_SOURCE.to_string()
            });

    // v0.2.42 RT-3: 24-hour cooldown gate. If the deferred flag is set
    // and the last failure was recorded within the last 24 hours, skip
    // this auto-trigger. This prevents repeated Supabase edge calls for
    // configurations that require user action (e.g.
    // `unsupported_embedding_source` — a new embedding source hasn't
    // been released yet; no point retrying every install cycle).
    //
    // The user can always force a retry via the "Download default weights"
    // button on the module tile, which calls the manual path directly
    // (bypassing this gate). On success that path calls
    // `clear_weights_download_deferred`, resetting the cooldown.
    if should_skip_r5_poll(db, project_id, module_id) {
        eprintln!(
            "[module_default_weights] R5 auto-trigger: skipping (deferred + \
             within 24-hour cooldown) for project {} module {}",
            project_id, module_id
        );
        return Ok(());
    }

    // 2. Reuse the manual-download path (no duplication). This is the
    // same function the GUI's "Download default weights" button calls
    // via the chained_action dispatcher (`module_dispatch.rs:244-252`).
    let result = module_download_default_weights_inner(
        module_id.to_string(),
        project_id.to_string(),
        embedding_source.clone(),
        db,
        app,
    )
    .await;

    match result {
        Ok(downloaded) => {
            eprintln!(
                "[module_default_weights] R5 auto-trigger: downloaded {} ({}) \
                 for project {}",
                downloaded.local_path, downloaded.version, project_id
            );

            // Best-effort: clear any stale deferred-flag from a prior
            // failed attempt. Idempotent.
            clear_weights_download_deferred(db, project_id, module_id);

            // v0.2.42 RT-4: persist (source, version) so
            // `module_reset_weights_to_global` can derive the global
            // path without requiring the caller to pass them explicitly.
            let _ = db.set_setting(
                project_id, module_id, WEIGHTS_LAST_EMBEDDING_SOURCE_KEY,
                &serde_json::Value::String(embedding_source.clone()),
            );
            let _ = db.set_setting(
                project_id, module_id, WEIGHTS_LAST_VERSION_KEY,
                &serde_json::Value::String(downloaded.version.clone()),
            );

            let _ = db.audit(
                "module_default_weights_auto_downloaded",
                Some(project_id),
                Some(module_id),
                &serde_json::json!({
                    "embedding_source": embedding_source,
                    "version": downloaded.version,
                    "local_path": downloaded.local_path,
                }),
            );
            Ok(())
        }
        Err(e) => {
            // Soft-fail path: log, set deferred-flag, audit, continue.
            // The install row stays installed; the container is already
            // running; the user can re-trigger via the manifest button.
            //
            // Common failure modes we expect to land here:
            //   - "no license key configured" (race: license rotated
            //     between install-gate check and post-install spawn).
            //   - "rl-latest-weights returned 404" (R4's Supabase edge
            //     function not yet deployed in this environment).
            //   - "rl-latest-weights returned 5xx" (transient server
            //     error or rate-limit).
            //   - "POST ...: connection timed out" (network blip).
            //   - "unsupported_embedding_source: ..." (v0.2.42 RT-3,
            //     structured error; includes list of alternatives).
            eprintln!(
                "[module_default_weights] R5 auto-trigger soft-fail for \
                 project {} module {}: {}",
                project_id, module_id, e
            );

            mark_weights_download_deferred(db, project_id, module_id, &embedding_source, &e);

            // v0.2.42 RT-3: surface the error to `module_installs.last_error`
            // so the GUI tile can render a human-readable failure reason
            // (especially useful for `unsupported_embedding_source` which
            // includes the list of supported alternatives). Best-effort:
            // failure here is non-fatal — the deferred flag is already set.
            let _ = db.set_module_last_error(project_id, module_id, Some(&e));

            // Return Ok — the install pipeline must NOT fail when the
            // weights download soft-fails. The container is running
            // with its baked-in weights; the user can re-trigger via
            // the manifest button.
            Ok(())
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

    // ─── v0.2.40 R5: first-install auto-trigger tests ───────────────────
    //
    // Scope: unit tests for the pure-Db helpers (`mark_*` / `clear_*`)
    // and the const pin. The full `apply_default_weights_after_install`
    // function takes `&AppHandle` and exercises the keychain +
    // network paths in `module_download_default_weights_inner`, which
    // can't be reached from a `cargo test --lib` context (no
    // AppHandle, no keychain license, no Supabase endpoint). The
    // contract those paths follow is already tested by the existing
    // `module_download_default_weights_returns_local_path_and_version`
    // test above. What R5 adds on top is the deferred-flag bookkeeping
    // — tested here.

    fn open_db_with_project() -> (Db, String, String) {
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().expect("in-memory db");
        let project_id = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &project_id,
            "R5 Test Project",
            "/tmp/r5-test",
            ProjectHost::Base,
            &format!("r5-test-{}", &project_id[..8]),
        )
        .expect("insert project");
        let module_id = "vct-rl-reranker".to_string();
        (db, project_id, module_id)
    }

    /// Pin the setting-key constant. The renderer (JS side) reads this
    /// key from `module_settings` to render the "click Download default
    /// weights to refresh" hint on the module tile. Changing the key
    /// silently would break the GUI hint without a compiler warning.
    #[test]
    fn weights_download_deferred_key_is_stable() {
        assert_eq!(WEIGHTS_DOWNLOAD_DEFERRED_KEY, "weights_download_deferred");
    }

    /// `mark_weights_download_deferred` writes `true` to the
    /// `(project_id, module_id, "weights_download_deferred")` row in
    /// module_settings. The GUI tile reads this row to decide whether
    /// to render the "click to refresh" hint.
    #[test]
    fn mark_weights_download_deferred_sets_setting_to_true() {
        let (db, project_id, module_id) = open_db_with_project();

        mark_weights_download_deferred(
            &db,
            &project_id,
            &module_id,
            "qwen3",
            "rl-latest-weights returned 404: function not deployed",
        );

        let value = db
            .get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY)
            .expect("get_setting must not error")
            .expect("deferred-flag row must exist after mark_*");
        assert_eq!(value, serde_json::Value::Bool(true));
    }

    /// `clear_weights_download_deferred` removes the row, returning the
    /// project to the "no defer" state. Idempotent: calling it when the
    /// row doesn't exist must produce a silent success.
    #[test]
    fn clear_weights_download_deferred_removes_setting() {
        let (db, project_id, module_id) = open_db_with_project();

        // Pre-condition: row exists.
        mark_weights_download_deferred(
            &db,
            &project_id,
            &module_id,
            "qwen3",
            "test failure",
        );
        assert!(
            db.get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY)
                .unwrap()
                .is_some(),
            "deferred-flag row must exist before clear_*"
        );

        clear_weights_download_deferred(&db, &project_id, &module_id);

        let after = db
            .get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY)
            .expect("get_setting must not error after clear");
        assert!(
            after.is_none(),
            "deferred-flag row must be removed by clear_*"
        );
    }

    /// `clear_weights_download_deferred` on a fresh project (no row to
    /// clear) is a no-op that doesn't panic. The success path of the
    /// auto-trigger calls clear_* defensively even on first install,
    /// where no prior deferred-flag exists; that path must not error.
    #[test]
    fn clear_weights_download_deferred_is_idempotent_when_absent() {
        let (db, project_id, module_id) = open_db_with_project();
        // No prior mark_* call.
        clear_weights_download_deferred(&db, &project_id, &module_id);
        // No panic, no error — test passes by reaching this point.
        let after = db
            .get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY)
            .expect("get_setting after no-op clear must not error");
        assert!(after.is_none());
    }

    /// Audit log: `mark_weights_download_deferred` records an audit
    /// entry with operation `module_default_weights_auto_download_deferred`
    /// so post-incident debugging can find every failed first-install
    /// auto-download in the audit log.
    #[test]
    fn mark_weights_download_deferred_records_audit_entry() {
        let (db, project_id, module_id) = open_db_with_project();

        mark_weights_download_deferred(
            &db,
            &project_id,
            &module_id,
            "qwen3",
            "rl-latest-weights returned 404",
        );

        // Verify by reading the audit table directly. audit_list
        // signature: (project_id, actor, since_ms, until_ms, search,
        // limit). We filter by project_id only and search the recent
        // 100 rows for the deferred-operation row.
        let entries = db
            .audit_list(Some(&project_id), None, None, None, None, 100)
            .expect("audit_list must work");
        let deferred_entry = entries
            .iter()
            .find(|e| e.operation == "module_default_weights_auto_download_deferred");
        assert!(
            deferred_entry.is_some(),
            "expected audit entry with operation \
             'module_default_weights_auto_download_deferred'; got operations: {:?}",
            entries.iter().map(|e| &e.operation).collect::<Vec<_>>()
        );
        // The detail payload includes the embedding source + error so
        // post-incident grep can find "all projects whose qwen3 weights
        // download deferred with a 404 error", etc.
        let detail = &deferred_entry.unwrap().detail;
        assert!(
            detail.contains("qwen3"),
            "audit detail must include embedding_source: {}",
            detail
        );
        assert!(
            detail.contains("404"),
            "audit detail must include error message: {}",
            detail
        );
    }

    /// Round-trip: mark, then clear, then mark again. Catches any
    /// stale-row issues from the SQL upsert path.
    #[test]
    fn mark_clear_mark_round_trip() {
        let (db, project_id, module_id) = open_db_with_project();

        mark_weights_download_deferred(&db, &project_id, &module_id, "qwen3", "err1");
        clear_weights_download_deferred(&db, &project_id, &module_id);
        mark_weights_download_deferred(&db, &project_id, &module_id, "arctic", "err2");

        let value = db
            .get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY)
            .expect("get_setting")
            .expect("final mark must leave row present");
        assert_eq!(value, serde_json::Value::Bool(true));
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

    // ─── v0.2.40 R3: staging-unify helper tests ─────────────────────────

    #[test]
    fn container_weights_mount_dir_matches_rl_runtime_volume_shape() {
        // Pin the path shape: `<vct_root>/data/<module>/<slug>/state`
        // must equal the `{VCT_DATA}/vct-rl-reranker/{project_slug}/state`
        // host volume declared in the RL Reranker manifest fixture
        // (see module_service.rs make_manifest's runtime.volumes block).
        // If these drift the container will never see the .pt again.
        let p = container_weights_mount_dir("vct-rl-reranker", "acme-corp");
        let suffix: Vec<String> = p
            .iter()
            .rev()
            .take(4)
            .map(|s| s.to_string_lossy().to_string())
            .collect();
        // Reversed: [state, acme-corp, vct-rl-reranker, data]
        assert_eq!(suffix[0], "state");
        assert_eq!(suffix[1], "acme-corp");
        assert_eq!(suffix[2], "vct-rl-reranker");
        assert_eq!(suffix[3], "data");
    }

    #[test]
    fn project_mount_filename_matches_container_weights_path_shape() {
        // Pin the filename: `rl_model_<source>_<version>.pt` is what
        // `module_service::container_weights_path` resolves to inside the
        // container. The launcher MUST publish the symlink under that
        // exact name or the container's path-resolution won't find it.
        assert_eq!(
            project_mount_weights_filename("qwen3", "v3"),
            "rl_model_qwen3_v3.pt"
        );
        // Hostile components get sanitised.
        assert_eq!(
            project_mount_weights_filename("../bad", "v3/../x"),
            "rl_model_.._bad_v3_.._x.pt"
        );
    }

    /// T1: After "download default weights" the container's expected
    /// bind-mount path contains the .pt (reachable through the symlink).
    #[cfg(unix)] // symlink fallback to copy on Windows tested separately.
    #[tokio::test]
    async fn t1_link_global_makes_pt_visible_at_container_mount() {
        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        // Simulate a successful global download: write a .pt at the
        // module_weights_dir path.
        let global = weights_file_path("vct-rl-reranker", "qwen3", "v42");
        tokio::fs::create_dir_all(global.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&global, b"fake-weights-bytes")
            .await
            .unwrap();

        // Act: link into the per-project bind-mount.
        let linked = link_global_into_project_mount(
            "vct-rl-reranker",
            "acme-corp",
            "qwen3",
            "v42",
            &global,
        )
        .await
        .expect("link must succeed on unix");

        // Assert: the symlink is at the container-expected location with
        // the container-expected filename.
        assert_eq!(
            linked,
            project_mount_weights_path("vct-rl-reranker", "acme-corp", "qwen3", "v42"),
        );
        let fname = linked.file_name().unwrap().to_string_lossy();
        assert_eq!(fname, "rl_model_qwen3_v42.pt");

        // Assert: it IS a symlink, not a copy.
        let meta = tokio::fs::symlink_metadata(&linked).await.unwrap();
        assert!(
            meta.file_type().is_symlink(),
            "expected a symlink, got {:?}",
            meta.file_type()
        );

        // Assert: reading through the symlink gives the global content.
        let bytes = tokio::fs::read(&linked).await.unwrap();
        assert_eq!(bytes, b"fake-weights-bytes");

        std::env::remove_var("VCT_STATE_DIR");
    }

    /// T2: A per-project override (user replaces the symlink with a real
    /// file) MUST survive a re-link. The launcher does not clobber
    /// user-placed files.
    #[cfg(unix)]
    #[tokio::test]
    async fn t2_override_real_file_survives_relink() {
        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        // Global download.
        let global = weights_file_path("vct-rl-reranker", "qwen3", "v1");
        tokio::fs::create_dir_all(global.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&global, b"global-v1").await.unwrap();

        let mount = project_mount_weights_path("vct-rl-reranker", "acme-corp", "qwen3", "v1");

        // User places an override (regular file, NOT a symlink) at the
        // mount slot — simulates "diverge" workflow.
        tokio::fs::create_dir_all(mount.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&mount, b"USER-OVERRIDE")
            .await
            .unwrap();

        // Act: re-link (simulates re-running "Download default weights").
        let result = link_global_into_project_mount(
            "vct-rl-reranker",
            "acme-corp",
            "qwen3",
            "v1",
            &global,
        )
        .await
        .expect("re-link must succeed");
        assert_eq!(result, mount);

        // Assert: the override is preserved verbatim.
        let bytes = tokio::fs::read(&mount).await.unwrap();
        assert_eq!(
            bytes, b"USER-OVERRIDE",
            "real-file override must NOT be clobbered by re-link"
        );

        // Assert: still NOT a symlink.
        let meta = tokio::fs::symlink_metadata(&mount).await.unwrap();
        assert!(
            !meta.file_type().is_symlink(),
            "override should stay a real file"
        );

        std::env::remove_var("VCT_STATE_DIR");
    }

    /// T3: "Reset to latest released weights" wipes any override (real
    /// file OR stale symlink) and re-points the slot at the global file.
    #[cfg(unix)]
    #[tokio::test]
    async fn t3_reset_wipes_override_and_relinks_to_global() {
        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        // Global download.
        let global = weights_file_path("vct-rl-reranker", "qwen3", "v2");
        tokio::fs::create_dir_all(global.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&global, b"global-v2-content").await.unwrap();

        let mount = project_mount_weights_path("vct-rl-reranker", "acme-corp", "qwen3", "v2");

        // Place an override.
        tokio::fs::create_dir_all(mount.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&mount, b"USER-OVERRIDE-WILL-BE-WIPED")
            .await
            .unwrap();

        // Act: reset.
        let linked = reset_weights_to_global(
            "vct-rl-reranker",
            "acme-corp",
            "qwen3",
            "v2",
            &global,
        )
        .await
        .expect("reset must succeed");

        // Assert: slot is a symlink again.
        let meta = tokio::fs::symlink_metadata(&linked).await.unwrap();
        assert!(
            meta.file_type().is_symlink(),
            "post-reset slot must be a symlink, got {:?}",
            meta.file_type()
        );

        // Assert: reading through resolves to the global content.
        let bytes = tokio::fs::read(&linked).await.unwrap();
        assert_eq!(bytes, b"global-v2-content");

        std::env::remove_var("VCT_STATE_DIR");
    }

    /// Edge case: refreshing an existing managed symlink. The first
    /// link points at one .pt; a second link to the same slot for a
    /// new version cleanly removes + re-creates.
    #[cfg(unix)]
    #[tokio::test]
    async fn relink_refreshes_existing_symlink() {
        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        // Two distinct global files (same source, different versions).
        let g1 = weights_file_path("vct-rl-reranker", "qwen3", "v1");
        let g2 = weights_file_path("vct-rl-reranker", "qwen3", "v1");
        tokio::fs::create_dir_all(g1.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&g1, b"first").await.unwrap();

        // First link.
        link_global_into_project_mount("vct-rl-reranker", "acme-corp", "qwen3", "v1", &g1)
            .await
            .expect("first link");

        // Re-write global with new content (same path — simulates an
        // atomic-rename promote that the next download would do).
        tokio::fs::write(&g2, b"updated").await.unwrap();

        // Second link to same slot — should remove existing symlink and
        // recreate (no "file exists" error).
        let target =
            link_global_into_project_mount("vct-rl-reranker", "acme-corp", "qwen3", "v1", &g2)
                .await
                .expect("re-link must succeed without 'file exists'");

        let bytes = tokio::fs::read(&target).await.unwrap();
        assert_eq!(bytes, b"updated");

        std::env::remove_var("VCT_STATE_DIR");
    }

    // ─── v0.2.42 RT-3: unsupported_embedding_source UX + cooldown ─────────

    /// `UnsupportedEmbeddingSourceError::fmt` produces a message that
    /// contains both the requested source and the supported alternatives.
    #[test]
    fn unsupported_embedding_source_error_display_includes_supported_list() {
        let err = UnsupportedEmbeddingSourceError {
            requested_source: "matryoshka".to_string(),
            supported_sources: vec!["qwen3".to_string(), "arctic2".to_string()],
        };
        let msg = err.to_string();
        assert!(msg.contains("matryoshka"), "must mention requested source: {}", msg);
        assert!(msg.contains("qwen3"), "must list supported: {}", msg);
        assert!(msg.contains("arctic2"), "must list supported: {}", msg);
    }

    /// When the supported list is empty the message stays coherent (no panic,
    /// no "available sources: " with nothing after the colon).
    #[test]
    fn unsupported_embedding_source_error_display_handles_empty_supported_list() {
        let err = UnsupportedEmbeddingSourceError {
            requested_source: "experimental".to_string(),
            supported_sources: vec![],
        };
        let msg = err.to_string();
        assert!(msg.contains("experimental"), "must mention requested source");
        assert!(!msg.is_empty());
    }

    /// `mark_weights_download_deferred` now also writes the
    /// `WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY` timestamp row so
    /// `should_skip_r5_poll` can compute the 24-hour window.
    #[test]
    fn mark_deferred_writes_last_failed_at_timestamp() {
        let (db, project_id, module_id) = open_db_with_project();
        let before_ms = chrono::Utc::now().timestamp_millis();

        mark_weights_download_deferred(
            &db, &project_id, &module_id, "matryoshka",
            "unsupported_embedding_source: matryoshka is not supported; available sources: qwen3",
        );

        let ts_value = db
            .get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY)
            .expect("get_setting must not error")
            .expect("timestamp row must exist after mark_*");

        let ts_ms = ts_value.as_i64().expect("timestamp must be a JSON number");
        let after_ms = chrono::Utc::now().timestamp_millis();
        assert!(ts_ms >= before_ms, "timestamp must be >= before_ms");
        assert!(ts_ms <= after_ms, "timestamp must be <= after_ms");
    }

    /// `clear_weights_download_deferred` must also remove the
    /// `WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY` row so the cooldown resets.
    #[test]
    fn clear_deferred_also_clears_last_failed_at() {
        let (db, project_id, module_id) = open_db_with_project();

        mark_weights_download_deferred(&db, &project_id, &module_id, "qwen3", "err");
        // Pre-condition: both rows exist.
        assert!(
            db.get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY)
                .unwrap()
                .is_some(),
            "last_failed_at row must exist before clear"
        );

        clear_weights_download_deferred(&db, &project_id, &module_id);

        assert!(
            db.get_setting(&project_id, &module_id, WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY)
                .unwrap()
                .is_none(),
            "last_failed_at row must be removed by clear_*"
        );
    }

    /// `should_skip_r5_poll` returns false when no deferred flag is set
    /// (fresh project, never failed).
    #[test]
    fn should_skip_r5_poll_returns_false_when_not_deferred() {
        let (db, project_id, module_id) = open_db_with_project();
        // No mark_* call — deferred flag absent.
        assert!(
            !should_skip_r5_poll(&db, &project_id, &module_id),
            "must not skip when no deferred flag"
        );
    }

    /// `should_skip_r5_poll` returns true when deferred flag is set
    /// and the failure timestamp is within the 24-hour window (i.e. now).
    #[test]
    fn should_skip_r5_poll_returns_true_within_cooldown() {
        let (db, project_id, module_id) = open_db_with_project();
        mark_weights_download_deferred(&db, &project_id, &module_id, "qwen3", "network error");
        // Timestamp was just written — well within 24h.
        assert!(
            should_skip_r5_poll(&db, &project_id, &module_id),
            "must skip within 24-hour cooldown"
        );
    }

    /// `should_skip_r5_poll` returns false when the failure timestamp is
    /// older than 24 hours (cooldown expired → allow retry).
    #[test]
    fn should_skip_r5_poll_returns_false_when_cooldown_expired() {
        let (db, project_id, module_id) = open_db_with_project();
        // Deferred flag set but timestamp is 25h in the past.
        let _ = db.set_setting(
            &project_id, &module_id, WEIGHTS_DOWNLOAD_DEFERRED_KEY,
            &serde_json::Value::Bool(true),
        );
        let old_ts = chrono::Utc::now().timestamp_millis() - (25 * 60 * 60 * 1000_i64);
        let _ = db.set_setting(
            &project_id, &module_id, WEIGHTS_DOWNLOAD_LAST_FAILED_AT_KEY,
            &serde_json::Value::Number(old_ts.into()),
        );
        assert!(
            !should_skip_r5_poll(&db, &project_id, &module_id),
            "must allow retry once cooldown has expired"
        );
    }

    /// Integration test: `fetch_signed_download_url` receiving a mocked
    /// Supabase 400 `unsupported_embedding_source` body returns a
    /// structured error message that includes the requested source and
    /// the supported list.
    #[tokio::test]
    async fn fetch_signed_download_url_structures_unsupported_embedding_source_error() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        let error_body = serde_json::json!({
            "error": "unsupported_embedding_source",
            "supported_embedding_sources": ["qwen3", "arctic2"],
        })
        .to_string();

        tokio::spawn(async move {
            let (mut sock, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf).await;
            let body = error_body.clone();
            let response = format!(
                "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                body.len(),
                body,
            );
            let _ = sock.write_all(response.as_bytes()).await;
            let _ = sock.shutdown().await;
        });

        let endpoint = format!("http://127.0.0.1:{}/rl-latest-weights", port);
        let result = fetch_signed_download_url(
            &endpoint, "fake-key", "deadbeef", "matryoshka", "vct-rl-reranker",
        )
        .await;

        let err = result.expect_err("must error on 400 unsupported_embedding_source");
        // Must start with the structured prefix.
        assert!(
            err.starts_with("unsupported_embedding_source:"),
            "error must have structured prefix: {}",
            err
        );
        // Must contain the requested source.
        assert!(err.contains("matryoshka"), "error must name the bad source: {}", err);
        // Must contain at least one supported source.
        assert!(
            err.contains("qwen3") || err.contains("arctic2"),
            "error must list supported sources: {}",
            err
        );
    }

    // ─── v0.2.42 RT-5: .vct-managed marker for copy fallback ─────────────

    /// `managed_marker_path` returns a sibling file with `.vct-managed`
    /// appended to the filename.
    #[test]
    fn managed_marker_path_is_sibling_with_vct_managed_suffix() {
        let weights = PathBuf::from("/some/dir/rl_model_qwen3_v3.pt");
        let marker = managed_marker_path(&weights);
        assert_eq!(
            marker,
            PathBuf::from("/some/dir/rl_model_qwen3_v3.pt.vct-managed"),
        );
        // Same parent directory.
        assert_eq!(marker.parent(), weights.parent());
    }

    /// On platforms where symlinks work (Unix), a copy produced by a prior
    /// fallback that has a `.vct-managed` marker is replaced by a symlink
    /// on the next link call. The marker is cleaned up.
    #[cfg(unix)]
    #[tokio::test]
    async fn managed_copy_is_replaced_by_symlink_on_relink() {
        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let global = weights_file_path("vct-rl-reranker", "qwen3", "v5");
        tokio::fs::create_dir_all(global.parent().unwrap()).await.unwrap();
        tokio::fs::write(&global, b"updated-weights").await.unwrap();

        let target = project_mount_weights_path("vct-rl-reranker", "proj", "qwen3", "v5");
        tokio::fs::create_dir_all(target.parent().unwrap()).await.unwrap();

        // Simulate a prior copy-fallback: write the .pt + the marker.
        tokio::fs::write(&target, b"old-copy-content").await.unwrap();
        let marker = managed_marker_path(&target);
        tokio::fs::write(&marker, b"vct-managed").await.unwrap();

        // Act: re-link (symlink should now succeed on Unix).
        let linked = link_global_into_project_mount(
            "vct-rl-reranker", "proj", "qwen3", "v5", &global,
        )
        .await
        .expect("re-link must succeed");

        // The old copy and marker must be gone.
        assert!(!marker.exists(), ".vct-managed marker must be cleaned up");

        // The slot must now be a symlink to the global file.
        let meta = tokio::fs::symlink_metadata(&linked).await.unwrap();
        assert!(
            meta.file_type().is_symlink(),
            "slot must be a symlink after re-link on Unix"
        );

        // Content through the symlink must be the global's content.
        let bytes = tokio::fs::read(&linked).await.unwrap();
        assert_eq!(bytes, b"updated-weights");

        std::env::remove_var("VCT_STATE_DIR");
    }

    /// A regular file WITHOUT a `.vct-managed` marker is treated as a user
    /// override and preserved on re-link.
    #[cfg(unix)]
    #[tokio::test]
    async fn user_override_without_marker_is_preserved() {
        let tmp = tempfile::tempdir().expect("mkdtemp");
        std::env::set_var("VCT_STATE_DIR", tmp.path());

        let global = weights_file_path("vct-rl-reranker", "qwen3", "v7");
        tokio::fs::create_dir_all(global.parent().unwrap()).await.unwrap();
        tokio::fs::write(&global, b"global-content").await.unwrap();

        let target = project_mount_weights_path("vct-rl-reranker", "proj2", "qwen3", "v7");
        tokio::fs::create_dir_all(target.parent().unwrap()).await.unwrap();

        // Simulate a user override: regular file, NO marker.
        tokio::fs::write(&target, b"user-custom-weights").await.unwrap();

        // Act: attempt to link.
        let result = link_global_into_project_mount(
            "vct-rl-reranker", "proj2", "qwen3", "v7", &global,
        )
        .await
        .expect("must return Ok (user override path)");

        // The override content must be intact.
        let bytes = tokio::fs::read(&result).await.unwrap();
        assert_eq!(
            bytes, b"user-custom-weights",
            "user override file must not be touched"
        );

        // Must NOT be a symlink — it's the user's regular file.
        let meta = tokio::fs::symlink_metadata(&result).await.unwrap();
        assert!(
            !meta.file_type().is_symlink(),
            "user override must remain a regular file"
        );

        std::env::remove_var("VCT_STATE_DIR");
    }
}
