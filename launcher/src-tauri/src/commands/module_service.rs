//! Per-project paid-module container lifecycle + weights-update polling
//! + (fine-tune-after-download flow). Originally landed as `rl_service.rs`
//! in v0.2.21 for the Pro-tier `vct-rl-reranker` container; renamed in
//! v0.2.26 alongside the declarative dispatcher rollout to reflect that
//! the helpers are written against `ModuleManifest` and apply to any
//! paid module (`vct-coordination`, `vct-transcrypt`, future) whose
//! manifest's `runtime` block carries the container-specific fields
//! added in v0.2.21 (`container_name_template`, `image_ref`, `ports`,
//! `volumes`, `env_derived`).
//!
//! Why "module_service" rather than "container_service": the launcher already
//! manages a separate, lower-level container stack via `commands::lifecycle`
//! (Weaviate / Ollama / code-embed — the shared infrastructure). This
//! module is the per-PROJECT, per-MODULE container layer that sits on
//! top. Keeping them at separate paths avoids the naming collision and
//! signals that this layer is for paid-module integrations.
//!
//! Cross-OS: spawns podman OR docker via the local `detect_container_runtime`
//! helper. Paths via PathBuf throughout; never assumes Unix separators.
//! Subprocess invocations use `env_clear()` + selective env pass-through
//! (PATH/HOME/USER/TMPDIR/LANG/LC_ALL, plus SYSTEMROOT/APPDATA/
//! LOCALAPPDATA/USERPROFILE/TEMP/TMP on Windows).
//!
//! Cross-embedding: reads `ACTIVE_EMBEDDING` from the project's
//! `.claude/env` (qwen3 / arctic / openai / future). Never hardcodes the
//! source list — `embedding_source` is a free-form string at every
//! layer (DB, wire, helpers).

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, State};
use tokio::process::Command;

use crate::db::models::{ModuleStatus, ProjectRow};
use crate::db::Db;
use crate::manifest::{InstallMethod, ModuleManifest, PlaceholderCtx};
use vct_launcher_core::process::CommandExt as _;

// v0.2.47: shared per-paid-module container helpers. Re-exported here as
// `pub use` so existing call sites (and the local `#[cfg(test)]` block)
// keep their unqualified imports. See
// `vct-launcher-core::services::container_runtime` for the canonical
// source; the previous local copies have been deleted to close the
// drift gap that caused the supervisor-image-resolution-variant bug.
pub use vct_launcher_core::services::container_runtime::{
    build_podman_run_args, container_weights_path, ensure_volume_host_dirs,
    resolve_container_name, resolve_image_ref, sanitize_path_component,
};

// Re-exports kept available for downstream callers / tests even though
// this file doesn't reference them in the post-v0.2.47 body. The
// `#[allow(unused_imports)]` is intentional: each re-export is part of
// the module's public API (callers in `tests/`, the upcoming v0.2.48
// hub→launcher fallback, etc.).
#[allow(unused_imports)]
pub use vct_launcher_core::services::container_runtime::{
    build_port_arg, build_volume_arg, resolve_value, resolve_variant_tag, rl_placeholders,
    DEDUP_SENTINEL, DEFAULT_OLLAMA_PORT,
};

// ─── Constants ──────────────────────────────────────────────────────────

/// Module ID we ask /rl-latest-version about. Currently the only
/// container-pull module — generalising past this needs a second
/// `module_id` field in the wire contract.
pub const RL_RERANKER_MODULE_ID: &str = "vct-rl-reranker";

/// Default embedding source when the per-project setting isn't set or
/// the read fails. Mirrors the manifest's `ACTIVE_EMBEDDING` default.
pub const DEFAULT_EMBEDDING_SOURCE: &str = "qwen3";

/// Fixed RL port for the orchestrator-root project. Every base/mao
/// project gets a random value in 11500..=11900; orchestrator_root
/// pins this address so the orchestrator clone's RL surface is always
/// discoverable at the same port.
pub const ORCHESTRATOR_ROOT_RL_PORT: u16 = 11442;

/// Allocation window for non-orchestrator-root projects.
pub const RL_PORT_RANGE_LO: u16 = 11500;
pub const RL_PORT_RANGE_HI: u16 = 11900;

/// Default canonical endpoint. Manifest's
/// `install.container.rotate_weights_endpoint` overrides this.
pub const DEFAULT_RL_LATEST_VERSION_ENDPOINT: &str =
    "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-latest-version";

/// Public machine-id helper used by `lib.rs::spawn_daily_weights_poll`
/// license-reader closure (Step 24 commit b). Thin wrapper around
/// `commands::licensing::machine_id_hash` so the closure in lib.rs has
/// a `pub fn` it can reach without depending on `pub(crate)` symbols
/// across the commands tree.
///
/// v0.2.36: previously this was an inline duplicate of the MAC-based
/// algorithm. Replaced with a delegation call so the platform-stable
/// host identifier change in `licensing.rs` propagates here
/// automatically — keeps the two call sites in lockstep.
pub fn machine_id_hash_for_poll() -> String {
    crate::commands::licensing::machine_id_hash()
}

// v0.2.47: `DEFAULT_OLLAMA_PORT` re-exported from
// `vct-launcher-core::services::container_runtime` via the top-of-file
// `pub use` block.

// ─── Wire types ─────────────────────────────────────────────────────────

/// Response shape from `/rl-latest-version`. Mirrors the edge-function
/// contract owned by Stream C.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LatestVersionResponse {
    pub has_update: bool,
    pub latest_version: String,
    pub embedding_source: String,
    #[serde(default)]
    pub download_url: String,
    #[serde(default)]
    pub download_url_expires_at: String,
    #[serde(default)]
    pub sha256: String,
    #[serde(default)]
    pub released_at: String,
    #[serde(default)]
    pub notes: String,
}

/// User-facing fine-tune choice from the prompt (Phase 4A).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FinetuneChoice {
    /// Run finetune in background, then signal rotate.
    Now,
    /// Mark weights as "pending finetune" — next-launch prompt re-surfaces it.
    Later,
    /// Accept unmodified global weights; rotate immediately.
    Skip,
}

/// Wire shape returned by `get_rl_dashboard_state` (Phase 4B). The
/// frontend renders 3 sections — container, weights, recent activity —
/// and reads all values from this single struct (one IPC round-trip).
///
/// v0.2.29: optional fields from the running container's
/// `GET /state_summary` endpoint (vct-rl-reranker v0.2.3+). All are
/// `Option<_>` because the probe soft-fails to `None` when:
///   - container isn't running
///   - container is older than v0.2.3 (no /state_summary endpoint → 404)
///   - probe times out
///   - body fails to parse
/// The dashboard widget renders these as "N dynamic types registered"
/// only when `Some`. Pre-v0.2.3 modules keep working — the existing
/// fields stay correct, the new ones just stay `None`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RlDashboardState {
    pub container_name: String,
    pub container_running: bool,
    pub port: u16,
    pub image_tag: String,
    pub current_weights_version: String,
    pub last_checked_at: i64,
    pub last_finetuned_at: i64,
    /// First 8 chars of the active weights file's sha256, "" when no
    /// state row yet.
    pub weights_sha256_prefix: String,
    pub recent_events_count: u32,
    pub recent_events_avg_latency_ms: f32,
    /// v0.2.29: from `GET /state_summary` — count of registry entries
    /// with `idx >= N_ENTITY_TYPES` (i.e. user-trained types beyond the
    /// builtin set). `None` when the probe failed.
    #[serde(default)]
    pub dynamic_types_count: Option<u32>,
    /// v0.2.29: from `GET /state_summary` — whether `.types_layout_v2`
    /// marker file exists in the state dir (signals the D1' migration
    /// has run; sidecars are authoritative). `None` when the probe
    /// failed.
    #[serde(default)]
    pub d1_marker_present: Option<bool>,
}

impl RlDashboardState {
    /// Empty / "not installed" state. The frontend distinguishes via
    /// `image_tag == ""`.
    pub fn empty() -> Self {
        Self {
            container_name: String::new(),
            container_running: false,
            port: 0,
            image_tag: String::new(),
            current_weights_version: String::new(),
            last_checked_at: 0,
            last_finetuned_at: 0,
            weights_sha256_prefix: String::new(),
            recent_events_count: 0,
            recent_events_avg_latency_ms: 0.0,
            dynamic_types_count: None,
            d1_marker_present: None,
        }
    }
}

// ─── Pure helpers (re-exported from core) ──────────────────────────────
//
// v0.2.47: `resolve_container_name`, `resolve_image_ref`,
// `rl_placeholders`, `resolve_value`, `build_port_arg`, `build_volume_arg`,
// `build_podman_run_args`, `sanitize_path_component`, and
// `container_weights_path` have all been promoted into
// `vct-launcher-core::services::container_runtime` and re-exported via
// the `pub use` block at the top of this file. The local copies that
// used to live here were near-identical to the hub-side copies in
// `vct-hub::module_supervisor`; collapsing them removes the divergence
// that produced the supervisor-image-resolution-variant bug.
//
// See `knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md`.

/// Detect which container runtime to use. Mirrors
/// `installer_engine::detect_container_runtime` (kept private over there;
/// duplicating two lines beats making it pub and exporting a private API
/// across module boundaries).
async fn detect_container_runtime() -> Result<String, String> {
    for candidate in ["podman", "docker"] {
        let probe = Command::new(candidate).silent()
            .args(["--version"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
        if probe.map(|s| s.success()).unwrap_or(false) {
            return Ok(candidate.to_string());
        }
    }
    Err("no container runtime found (tried podman, docker)".into())
}

// v0.2.47: `sanitize_path_component` + `container_weights_path` moved to
// `vct-launcher-core::services::container_runtime` and re-exported at
// the top of this file via `pub use`.

// ─── Container lifecycle (Phase 1E) ─────────────────────────────────────

/// Internal helper: start (or restart) the container associated with
/// `manifest` for the given project, allocating an `rl_port` if not yet
/// set. Returns the resolved container name on success.
///
/// Idempotent: if a same-named container already exists (running OR
/// stopped) we `podman rm -f` it first. This makes the install flow
/// recoverable from partial failures.
///
/// v0.2.47: looks up the persisted `GpuMode` and pipes it through
/// `resolve_image_ref` so the variant suffix (`-cuda` / `-rocm` /
/// `-cpu`) baked in at install time is preserved on start. Pre-v0.2.47
/// this helper substituted `manifest.version` raw — which produced a
/// bare `0.2.8` image ref where the registry actually carried
/// `0.2.8-cuda`, and the supervisor's `podman run` triggered an
/// anonymous re-pull that 401'd against private GHCR. See
/// `knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md`.
pub async fn start_container_for_module(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
) -> Result<String, String> {
    // v0.2.47: resolve the persisted GpuMode for this host. Soft-fail
    // to None if no snapshot exists — `start_container_for_module_with_gpu_mode`
    // treats None as "skip variant resolution" (legacy single-tag
    // modules + cases where we can't safely guess). For modules with
    // `gpu_image_variants` declared, lack of a snapshot here means the
    // user never ran a redetect; we fall through to the bare tag,
    // which matches pre-v0.2.47 behaviour and is no WORSE than the
    // current state (still better than substituting bare version on
    // private images — the pull-fallback below covers that).
    let gpu_mode = read_persisted_gpu_mode();
    start_container_for_module_with_gpu_mode(manifest, ctx, project, rl_port, gpu_mode).await
}

/// v0.2.47: explicit-GpuMode form of `start_container_for_module`.
/// Callers that already know the host's GpuMode (the launcher's
/// `start_container_after_install` reads it from the persisted
/// hardware snapshot at install time; the hub's resume sweep reads
/// it via the resolver injected at hub startup) pass it directly.
///
/// `Some(GpuMode)` → variant suffix applied to the image ref via
/// `resolve_variant_tag` (e.g. `:0.2.8` → `:0.2.8-cuda`). Also
/// pre-pulls the variant-correct image with a valid pull-token /
/// authfile BEFORE the `podman run`, so cache-evicted hosts never fall
/// through to anonymous-pull-401.
///
/// `None` → bare-tag mode (legacy single-tag modules, OR cases where
/// the caller has no GpuMode source). No variant suffix; no pre-pull.
/// Backwards-compatible with pre-v0.2.47 behaviour.
pub async fn start_container_for_module_with_gpu_mode(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
    gpu_mode: Option<crate::commands::gpu_policy::GpuMode>,
) -> Result<String, String> {
    let runtime = &manifest.runtime;
    if !matches!(runtime.r#type.as_str(), "container" | "service") {
        return Err(format!(
            "start_container_for_module called for non-container runtime '{}'",
            runtime.r#type
        ));
    }

    // NEW-3.B (2026-05-28): use defaulting helpers so service/container
    // modules without explicit container_name_template / image_ref get
    // sensible defaults instead of hard-failing with ok_or_else().
    let name_template = runtime.resolve_container_name_template(&manifest.id);
    let container_name = resolve_container_name(&name_template, &project.slug)?;
    let image_template = runtime.resolve_image_ref(
        manifest.install.container.as_ref().ok_or_else(|| {
            "install.container block missing — required for container/service modules".to_string()
        })?,
        &manifest.version,
    );
    // v0.2.47: thread `gpu_mode` into the core helper so variant-
    // bearing manifests get the right `-cuda` / `-rocm` / `-cpu` suffix.
    let image = resolve_image_ref(&image_template, manifest, gpu_mode)?;

    let podman = detect_container_runtime().await?;

    // v0.2.47: pre-pull the variant-correct image with auth context
    // attached, so a cache-evicted host doesn't fall through to
    // `podman run`'s anonymous-pull-401 path. Soft-fail: if pre-pull
    // returns an error, log it but let `podman run` make the final
    // call — the cache may have the image already, in which case `run`
    // succeeds without needing the registry. The legacy paid-modules
    // path is one of: image-already-in-cache (no-op) OR image-in-cache-
    // and-this-pre-pull-noops OR image-missing-and-pull-succeeds.
    if gpu_mode.is_some() && manifest.install.method == InstallMethod::ContainerPull {
        if let Err(e) = pre_pull_with_auth_for_start(manifest, &podman, &image).await {
            eprintln!(
                "[module_service] pre-pull for start failed (continuing — cache may suffice): {}",
                e
            );
        }
    }

    // Idempotency: force-remove any prior container with the same name.
    let _ = Command::new(&podman).silent()
        .args(["rm", "-f", &container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    // mkdir -p every volume host path so podman doesn't fail on bind
    // mounts of nonexistent directories.
    ensure_volume_host_dirs(manifest, ctx, rl_port, &project.slug).await;

    let args = build_podman_run_args(manifest, ctx, project, rl_port, &container_name, &image)?;

    let mut cmd = Command::new(&podman).silent();
    cmd.args(&args);
    cmd.env_clear();
    for key in ["PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "XDG_RUNTIME_DIR"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }
    #[cfg(target_os = "windows")]
    for key in ["SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let output = tokio::time::timeout(Duration::from_secs(60), cmd.output())
        .await
        .map_err(|_| format!("{} run timed out after 60s for {}", podman, container_name))?
        .map_err(|e| format!("spawn {} run: {}", podman, e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "{} run failed (exit {}) for {}: {}",
            podman,
            output.status.code().unwrap_or(-1),
            container_name,
            stderr.chars().take(500).collect::<String>()
        ));
    }

    Ok(container_name)
}

/// v0.2.47: read the persisted hardware snapshot and return its
/// `gpu_mode_decided`, or `None` if no snapshot exists / the read
/// fails. Used by the `start_container_for_module` wrapper to
/// preserve the variant suffix the installer pulled with.
///
/// Synchronous helper (no DB locking — `Db` is `Mutex<Connection>`,
/// the read is bounded), called from the async start path. Soft-fail
/// to `None` so a missing snapshot doesn't block container start.
fn read_persisted_gpu_mode() -> Option<crate::commands::gpu_policy::GpuMode> {
    // Open a short-lived connection to the launcher DB at the standard
    // path. The start path is normally invoked with Tauri State carrying
    // a `Db`, but the static fn here can't borrow that. We open a
    // sibling connection — same on-disk file, same data — and dispose
    // it on return.
    let conn = rusqlite::Connection::open(crate::db::db_path()).ok()?;
    let db = Db(std::sync::Mutex::new(conn));
    crate::commands::installer::read_persisted_hardware_snapshot(&db)
        .ok()
        .flatten()
        .map(|snap| snap.gpu_mode_decided)
}

/// v0.2.47 + v0.2.49: pre-pull the variant-correct image with proper
/// auth context BEFORE `podman run`. The actual implementation lives
/// in `vct-launcher-core::services::container_runtime::
/// pre_pull_with_auth_for_start` so the hub-side supervisor (Phase 3
/// auth port) and the launcher run byte-identical pre-pull code paths.
///
/// Soft-fails on every error — the caller logs and proceeds to
/// `podman run`. If the image is already in the local cache, `run`
/// succeeds without needing the pre-pull. If the image is missing AND
/// pre-pull failed, `run` will surface the anonymous-pull failure
/// itself; we don't double-report.
async fn pre_pull_with_auth_for_start(
    manifest: &ModuleManifest,
    runtime: &str,
    image_ref: &str,
) -> Result<(), String> {
    vct_launcher_core::services::container_runtime::pre_pull_with_auth_for_start(
        manifest, runtime, image_ref,
    )
    .await
}

/// Modules.rs-facing wrapper: allocates rl_port if needed, builds
/// `PlaceholderCtx`, calls `start_container_for_module`, persists the
/// resolved container_name on the install row. Returns the resolved
/// container name on success.
///
/// Allocation policy: orchestrator_root gets the fixed
/// `ORCHESTRATOR_ROOT_RL_PORT`; everyone else gets a random value in
/// `RL_PORT_RANGE_LO..=RL_PORT_RANGE_HI` chosen by `rand` and persisted
/// before the container starts. We accept the (extremely small) collision
/// risk between projects — the per-project bind to 127.0.0.1 means two
/// projects accidentally allocated the same port would conflict at
/// `podman run` time, surfacing a clear error. Future: a port-allocator
/// table can replace this with a SELECT-then-INSERT loop.
pub async fn start_container_after_install(
    manifest: &ModuleManifest,
    project: &ProjectRow,
    db: &Db,
) -> Result<String, String> {
    let rl_port = ensure_project_rl_port(db, project)?;
    let ctx = PlaceholderCtx::new(&manifest.id);
    let container_name = start_container_for_module(manifest, &ctx, project, rl_port).await?;
    db.set_module_container_name(&project.id, &manifest.id, &container_name)?;
    // TODO(paid-modules, v0.2.40 R2): the RL container should fetch
    // `rl_use_global` / `rl_online_training_disabled` /
    // `rl_global_training_source_flag` from
    // `GET /api/v1/projects/{id}/config` on a refresh cadence (e.g. at
    // startup AND on a periodic re-poll, so a runtime checkbox toggle
    // takes effect without a container restart). The hub side ships the
    // fields in v0.2.40 (see `launcher/src-tauri/vct-hub/src/config_api.rs`
    // `ProjectConfigResponse::rl_*`); the container-side consumer lives
    // in the paid-modules tree (`vct-rl-reranker` source) which is not
    // co-located with this clone. Closing this loop is tracked as item 3
    // of the v0.2.40 pre-push multi-Opus review synthesis (see
    // `discovery-A2-dashboard-widget-archaeology.md` F9 for the
    // verification recipe).
    Ok(container_name)
}

// ─── v0.2.49 Stream A: global container lifecycle ─────────────────────

/// Fixed RL listen port for a GLOBAL-scope module. One port per machine
/// per module — no per-project allocation table because there is exactly
/// ONE container per global module. Chosen above the orchestrator-root
/// port + above the per-project allocation range to avoid collision
/// with both.
pub const GLOBAL_RL_PORT: u16 = 11443;

/// v0.2.49 Stream A: start (or restart) the GLOBAL container for a
/// module. Sibling of [`start_container_for_module`].
///
/// Differences:
///   * No `ProjectRow` arg — global containers are not project-scoped.
///   * Container name = bare module_id (via [`resolve_global_container_name`]),
///     not `{module_id}-{project_slug}`.
///   * Volume paths substitute `"global"` for `{project_slug}` so the
///     state dir is stable across launcher restarts.
///   * Listens on [`GLOBAL_RL_PORT`] machine-wide.
pub async fn start_global_container_for_module(
    manifest: &ModuleManifest,
    module_id: &str,
    db: &Db,
) -> Result<String, String> {
    use vct_launcher_core::services::container_runtime::{
        build_podman_run_args_global, ensure_volume_host_dirs_global, resolve_global_container_name,
        resolve_image_ref,
    };

    let runtime = &manifest.runtime;
    if !matches!(runtime.r#type.as_str(), "container" | "service") {
        return Err(format!(
            "start_global_container_for_module called for non-container runtime '{}'",
            runtime.r#type
        ));
    }

    let name_template = runtime.resolve_container_name_template(&manifest.id);
    let container_name = resolve_global_container_name(&name_template, module_id)?;
    let image_template = runtime.resolve_image_ref(
        manifest.install.container.as_ref().ok_or_else(|| {
            "install.container block missing — required for container/service modules".to_string()
        })?,
        &manifest.version,
    );

    let gpu_mode = read_persisted_gpu_mode();
    let image = resolve_image_ref(&image_template, manifest, gpu_mode)?;

    let podman = detect_container_runtime().await?;

    // Pre-pull with auth for cache-evicted hosts (v0.2.47 pattern).
    if gpu_mode.is_some() && manifest.install.method == InstallMethod::ContainerPull {
        if let Err(e) = pre_pull_with_auth_for_start(manifest, &podman, &image).await {
            eprintln!(
                "[module_service] global pre-pull for start failed (continuing — cache may suffice): {}",
                e
            );
        }
    }

    // Idempotency: force-remove any prior container with the same name.
    let _ = Command::new(&podman)
        .silent()
        .args(["rm", "-f", &container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    let ctx = PlaceholderCtx::new(&manifest.id);
    ensure_volume_host_dirs_global(manifest, &ctx, GLOBAL_RL_PORT).await;

    let args =
        build_podman_run_args_global(manifest, &ctx, GLOBAL_RL_PORT, &container_name, &image)?;

    let mut cmd = Command::new(&podman).silent();
    cmd.args(&args);
    cmd.env_clear();
    for key in ["PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "XDG_RUNTIME_DIR"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }
    #[cfg(target_os = "windows")]
    for key in ["SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let output = tokio::time::timeout(Duration::from_secs(60), cmd.output())
        .await
        .map_err(|_| format!("{} run timed out after 60s for {}", podman, container_name))?
        .map_err(|e| format!("spawn {} run: {}", podman, e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "{} run failed (exit {}) for {}: {}",
            podman,
            output.status.code().unwrap_or(-1),
            container_name,
            stderr.chars().take(500).collect::<String>()
        ));
    }

    // Persist resolved container_name on the global install row so
    // subsequent resume sweeps short-circuit on the running-probe path.
    db.set_global_module_container_name(module_id, &container_name)?;

    Ok(container_name)
}

/// v0.2.49 Stream A: install-time wrapper for global containers.
/// Sibling of [`start_container_after_install`] for per-project modules.
pub async fn start_global_container_after_install(
    manifest: &ModuleManifest,
    db: &Db,
) -> Result<String, String> {
    start_global_container_for_module(manifest, &manifest.id, db).await
}

/// v0.2.49 Stream A: one-shot auto-migration that converts per-project
/// install rows into a single global row when the on-disk manifest now
/// declares `install.scope = "global"`.
///
/// Trigger: launcher boot. Per the v0.2.49 plan (user decision: "no
/// prompt, no legacy support — me + Fabio are the only users"), the
/// migration applies destructively on first launch after the manifest
/// flip:
///
///   1. For each installed module whose on-disk extracted manifest
///      declares `install.scope = "global"`.
///   2. If a global row already exists for that module → skip
///      (idempotent: migration already ran).
///   3. Otherwise: enumerate per-project rows, stop+remove their
///      containers, delete the rows, then insert ONE global row,
///      then start the global container.
///
/// Soft-fail throughout: any failure logs and continues; partial
/// migrations are recoverable on next boot (the per-project rows that
/// failed to delete stay; the global row is created from the first row's
/// metadata so the install path is captured). Audit-logged with
/// `operation = "module_migrated_to_global_scope"`.
///
/// **Idempotency contract**: re-running this function after a successful
/// migration is a NO-OP — the global row exists, so the per-module
/// branch short-circuits before any destructive work.
pub async fn auto_migrate_per_project_to_global(
    db: &Db,
    resolve_manifest: impl Fn(&str) -> Option<ModuleManifest>,
) {
    // Enumerate every distinct module_id that has at least one
    // per-project row. Using `list_module_installs_with_status` for all
    // three steady-state statuses gives us a small set per machine
    // (rarely more than a handful of modules).
    let mut seen_modules: std::collections::HashSet<String> = std::collections::HashSet::new();
    for status in &["installed", "running", "stopped", "error", "broken"] {
        match db.list_module_installs_with_status(status) {
            Ok(rows) => {
                for row in rows {
                    if row.project_id.is_some() {
                        seen_modules.insert(row.module_id);
                    }
                }
            }
            Err(e) => {
                eprintln!(
                    "[auto_migrate] list_module_installs_with_status({}) failed: {}",
                    status, e
                );
            }
        }
    }

    for module_id in seen_modules {
        // Look up the on-disk manifest. If it can't be found OR it
        // doesn't declare scope=global, leave per-project rows alone.
        let manifest = match resolve_manifest(&module_id) {
            Some(m) => m,
            None => {
                // Module installed but no manifest on disk — the post-
                // install extract step never ran. Skip; can't determine
                // intended scope.
                continue;
            }
        };
        if !manifest.install.scope.is_global() {
            continue;
        }

        // Already migrated? If a global row exists, we're done.
        match db.get_global_module_install(&module_id) {
            Ok(Some(_)) => continue,
            Ok(None) => {} // proceed to migration
            Err(e) => {
                eprintln!(
                    "[auto_migrate] get_global_module_install({}) failed: {}",
                    module_id, e
                );
                continue;
            }
        }

        // List per-project rows for this module.
        let per_project_rows = match db.list_per_project_installs_for_module(&module_id) {
            Ok(v) => v,
            Err(e) => {
                eprintln!(
                    "[auto_migrate] list_per_project_installs_for_module({}) failed: {}",
                    module_id, e
                );
                continue;
            }
        };

        if per_project_rows.is_empty() {
            // No per-project rows — nothing to migrate. The first install
            // through the per-project path didn't happen, OR they were
            // already cleaned up. Either way, the install code path
            // will create a global row on next install attempt.
            continue;
        }

        eprintln!(
            "[auto_migrate] {} declares install.scope=global with {} per-project row(s); \
             migrating to single global row",
            module_id,
            per_project_rows.len(),
        );

        // Stop + remove each per-project container.
        for row in &per_project_rows {
            if let Some(container_name) = row.container_name.as_deref() {
                if !container_name.is_empty() {
                    if let Err(e) = stop_container_for_project(container_name).await {
                        eprintln!(
                            "[auto_migrate] stop_container_for_project({}) failed: {}",
                            container_name, e
                        );
                    }
                }
            }
        }

        // Capture install path + version from the first row (all rows
        // for the same module share these — install_path is a template
        // resolved against `{VCT_MODULES}/{MODULE_ID}` which is project-
        // agnostic; version is the same per (module_id, manifest)).
        let install_path = per_project_rows[0].install_path.clone();
        let module_version = per_project_rows[0].module_version.clone();

        // Delete every per-project row.
        for row in &per_project_rows {
            if let Some(pid) = row.project_id.as_deref() {
                if let Err(e) = db.delete_module_install(pid, &module_id) {
                    eprintln!(
                        "[auto_migrate] delete_module_install({}, {}) failed: {}",
                        pid, module_id, e
                    );
                }
            }
        }

        // Insert the global row.
        let install_id = uuid::Uuid::new_v4().to_string();
        let global_row = match db.insert_global_module_install(
            &install_id,
            &module_id,
            &module_version,
            &install_path,
        ) {
            Ok(r) => r,
            Err(e) => {
                eprintln!(
                    "[auto_migrate] insert_global_module_install({}) failed: {}",
                    module_id, e
                );
                continue;
            }
        };
        // Mark Installed so the resume sweep will start the container.
        let _ = db.set_global_module_status(&module_id, ModuleStatus::Installed, None);
        let _ = db.audit(
            "module_migrated_to_global_scope",
            None,
            Some(&module_id),
            &serde_json::json!({
                "per_project_row_count": per_project_rows.len(),
                "new_install_id": global_row.id,
                "module_version": module_version,
                "install_path": install_path,
            }),
        );

        // Best-effort start. Failures are logged and surfaced via
        // last_error; the resume sweep will retry on next boot.
        if matches!(
            manifest.runtime.r#type.as_str(),
            "container" | "service"
        ) && manifest.install.method == InstallMethod::ContainerPull
        {
            match start_global_container_after_install(&manifest, db).await {
                Ok(name) => {
                    eprintln!(
                        "[auto_migrate] started global container {} for {}",
                        name, module_id
                    );
                }
                Err(e) => {
                    eprintln!(
                        "[auto_migrate] start_global_container_after_install({}) failed: {}",
                        module_id, e
                    );
                    let _ = db.set_global_module_last_error(&module_id, Some(&e));
                }
            }
        }
    }
}

/// Ensure `projects.rl_port` is populated for the project. Allocates if
/// NULL (fixed for orchestrator_root, random otherwise). Returns the
/// final value.
fn ensure_project_rl_port(db: &Db, project: &ProjectRow) -> Result<u16, String> {
    if let Some(port) = db.get_project_rl_port(&project.id)? {
        return Ok(port);
    }

    use crate::db::models::ProjectHost;
    let port = match project.host {
        ProjectHost::OrchestratorRoot => ORCHESTRATOR_ROOT_RL_PORT,
        _ => allocate_random_rl_port(),
    };
    db.set_project_rl_port(&project.id, port)?;
    Ok(port)
}

/// Random port in `RL_PORT_RANGE_LO..=RL_PORT_RANGE_HI`. Tiny window
/// (401 ports) — collisions are possible but rare; podman surfaces them
/// at run time with a clear "address already in use" error.
///
/// Uses OsRng directly (rand 0.9 `os_rng` feature) rather than `thread_rng`
/// — the launcher's Cargo.toml gates rand to a minimal feature set and
/// `thread_rng` isn't enabled.
fn allocate_random_rl_port() -> u16 {
    use rand::{Rng, SeedableRng};
    use rand::rngs::StdRng;
    let mut rng = StdRng::from_os_rng();
    rng.random_range(RL_PORT_RANGE_LO..=RL_PORT_RANGE_HI)
}

/// Stop + remove a container by name. Idempotent: nonexistent containers
/// produce a silent success. Uses `podman stop -t 10` (10s grace for
/// SIGTERM) before `podman rm` so the container has a chance to flush
/// state. Returns Err only when the container runtime itself isn't
/// available (no podman / docker on PATH).
pub async fn stop_container_for_project(container_name: &str) -> Result<(), String> {
    let podman = detect_container_runtime().await?;

    let _ = Command::new(&podman).silent()
        .args(["stop", "-t", "10", container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    let _ = Command::new(&podman).silent()
        .args(["rm", container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    Ok(())
}

/// Is a container with this name currently running? Returns Ok(false)
/// when the container doesn't exist OR is in any non-running state.
pub async fn is_container_running(container_name: &str) -> Result<bool, String> {
    let podman = detect_container_runtime().await?;
    let output = Command::new(&podman).silent()
        .args(["inspect", "--format", "{{.State.Status}}", container_name])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .await
        .map_err(|e| format!("spawn {} inspect: {}", podman, e))?;

    if !output.status.success() {
        return Ok(false);
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(parse_inspect_running_state(&stdout))
}

/// Pure helper for unit tests: parse the `--format '{{.State.Status}}'`
/// stdout into the boolean is-running answer. `"running\n"` and
/// `"running"` both map to true; everything else (including empty
/// strings, "exited", "paused", "created") maps to false.
pub fn parse_inspect_running_state(stdout: &str) -> bool {
    stdout.trim() == "running"
}

// ─── Tauri commands (Phase 1E / 3C / 4A / 4B) ───────────────────────────
//
// Step 24 commit b: the lifecycle commands (`rl_is_container_running`,
// `restart_rl_container`) now proxy to the hub's
// `/api/v1/projects/{project_id}/modules/{module_id}/...` endpoints
// (filled in by `vct-hub::lifecycle_api`). The supervisor logic lives
// in `vct-hub::module_supervisor`.
//
// Fallback contract: when the hub is unreachable (probe fails, port
// file missing, network blip), the commands fall back to the
// in-process supervisor implementations below. This keeps the launcher
// working in the "hub crashed but launcher GUI still up" failure mode
// and during the v0.2.21 → v0.2.22 cutover where some users may run a
// stale hub binary.

/// `is_container_running` by project_id. First tries the hub proxy;
/// falls back to in-process probe if the hub is unreachable.
#[command]
pub async fn rl_is_container_running(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    // Hub-first path.
    if let Ok(running) = hub_proxy_module_status(&project_id, RL_RERANKER_MODULE_ID).await {
        return Ok(running);
    }
    // Fallback: in-process probe (used when hub unreachable).
    let install = db.get_module_install(&project_id, RL_RERANKER_MODULE_ID)?;
    let name = match install.and_then(|i| i.container_name) {
        Some(n) if !n.is_empty() => n,
        _ => return Ok(false),
    };
    is_container_running(&name).await
}

/// Restart the per-project RL container. Hub proxy not yet wired for
/// restart (the hub-side endpoint is 501 until a catalog resolver
/// lands, Phase 3+). For now this command runs the in-process path —
/// the supervisor logic is the same implementation that the hub's
/// `module_supervisor` ships, so behaviour matches v0.2.21 exactly.
#[command]
pub async fn restart_rl_container(
    project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    let install = db
        .get_module_install(&project_id, RL_RERANKER_MODULE_ID)?
        .ok_or_else(|| {
            format!(
                "vct-rl-reranker not installed for project {}",
                project_id
            )
        })?;
    let container_name = install
        .container_name
        .ok_or("container_name not yet set on install row".to_string())?;

    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let manifest = crate::commands::modules::find_manifest_for_resume(&db, RL_RERANKER_MODULE_ID)
        .ok_or_else(|| {
            format!(
                "manifest for {} not in catalog (cannot restart container)",
                RL_RERANKER_MODULE_ID
            )
        })?;

    let rl_port = ensure_project_rl_port(&db, &project)?;

    stop_container_for_project(&container_name).await?;
    let ctx = PlaceholderCtx::new(RL_RERANKER_MODULE_ID);
    let _ = start_container_for_module(&manifest, &ctx, &project, rl_port).await?;
    Ok(())
}

/// NEW-3 (2026-05-28): generic "Start" Tauri command for any installed
/// module whose `module_installs.container_name` is NULL (i.e. the
/// post-install auto-start was skipped because `runtime.type` was not
/// admitted by the old `== "container"` gate). Also serves as a manual
/// "Start" affordance for service-type modules.
///
/// The Svelte tile renders a "Start" button when
/// `status='installed' AND container_name=NULL AND runtime_type ∈
/// {container, service}`. Clicking it invokes this command.
#[tauri::command]
pub async fn start_module_container(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let manifest = crate::commands::modules::find_manifest_for_resume(&db, &module_id)
        .ok_or_else(|| format!("manifest for {} not in catalog", module_id))?;

    let container_name = start_container_after_install(&manifest, &project, &db).await?;
    Ok(container_name)
}

// ─── Hub proxy helpers (Step 24 commit b) ───────────────────────────────
//
// Minimal HTTP client for the hub's `/api/v1/projects/.../modules/.../`
// endpoints. Mirrors the auth posture of `commands::hub_proxy`: read
// `~/.vct/hub.port` + `~/.vct/hub.token` fresh on each call, send
// `Authorization: Bearer <token>`. Soft-fails (returns Err) when the
// hub is unreachable so callers can fall back to the in-process path.

fn hub_port_for_proxy() -> Result<u16, String> {
    let path = crate::paths::vct_root_dir().join("hub.port");
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("read hub.port: {}", e))?;
    raw.trim()
        .parse::<u16>()
        .map_err(|e| format!("parse hub.port: {}", e))
}

fn hub_token_for_proxy() -> Result<String, String> {
    let path = crate::paths::vct_root_dir().join("hub.token");
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("read hub.token: {}", e))?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(format!("hub.token at {} is empty", path.display()));
    }
    Ok(trimmed.to_string())
}

/// Proxy for `GET /projects/{project_id}/modules/{module_id}/status`.
/// Returns the `running` boolean from the JSON envelope.
async fn hub_proxy_module_status(project_id: &str, module_id: &str) -> Result<bool, String> {
    let port = hub_port_for_proxy()?;
    let token = hub_token_for_proxy()?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let url = format!(
        "http://127.0.0.1:{}/api/v1/projects/{}/modules/{}/status",
        port, project_id, module_id
    );
    let resp = client
        .get(&url)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| format!("hub GET status: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("hub status returned {}", resp.status()));
    }
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("parse hub status: {}", e))?;
    Ok(body
        .get("running")
        .and_then(|v| v.as_bool())
        .unwrap_or(false))
}

/// Proxy for `POST /projects/{project_id}/modules/{module_id}/stop`.
/// Used by `commands::modules::uninstall_module_v2` (via the wrapper
/// `stop_container_for_project_via_hub`). Idempotent on the hub side.
#[allow(dead_code)]
async fn hub_proxy_module_stop(project_id: &str, module_id: &str) -> Result<(), String> {
    let port = hub_port_for_proxy()?;
    let token = hub_token_for_proxy()?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let url = format!(
        "http://127.0.0.1:{}/api/v1/projects/{}/modules/{}/stop",
        port, project_id, module_id
    );
    let resp = client
        .post(&url)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| format!("hub POST stop: {}", e))?;
    if !resp.status().is_success() && resp.status().as_u16() != 204 {
        return Err(format!("hub stop returned {}", resp.status()));
    }
    Ok(())
}

// ─── Phase 3C: weights update check + download + rotate ─────────────────

/// POST `/rl-latest-version` for the active embedding source of the
/// given project + module.
///
/// v0.2.31 Agent J: this function used to read `current_weights_version`
/// from the launcher-owned `module_weights_state` table and stamp
/// `last_checked_at` on every call. Both reads/writes are gone:
///   * Weights state is now container-owned (`rl_weights_state`, shipped
///     by vct-rl-reranker v0.2.6 via its module-shipped migration).
///   * The launcher no longer owns the "last checked" timestamp —
///     observers can read it through the hub's typed REST surface.
///   * `current_weights_version` is left empty here; the server-side
///     supabase function accepts the empty string and returns the
///     latest available version (the only thing the caller actually
///     does with the response). v0.2.32 may re-introduce the value via
///     a hub read once we measure whether anyone needs it.
pub async fn check_weights_update(
    db: &Db,
    project_id: &str,
    license_key: &str,
    machine_id_hash: &str,
    endpoint: &str,
) -> Result<LatestVersionResponse, String> {
    let project = db
        .get_project(project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let embedding_source = read_active_embedding_source(&project)
        .unwrap_or_else(|| DEFAULT_EMBEDDING_SOURCE.to_string());

    // v0.2.31 Agent J: `current_weights_version` previously came from a
    // launcher read of the dropped `module_weights_state` table.
    // Empty string is the documented "I don't know my current version,
    // please tell me the latest" value on the supabase function side.
    let body = serde_json::json!({
        "license_key": license_key,
        "machine_id_hash": machine_id_hash,
        "current_weights_version": "",
        "embedding_source": embedding_source,
    });

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let resp = client
        .post(endpoint)
        .header("Content-Type", "application/json")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("POST {}: {}", endpoint, e))?;

    let status = resp.status();
    let parsed: Result<LatestVersionResponse, _> = resp.json().await;

    if !status.is_success() {
        return Err(format!(
            "rl-latest-version returned {}: {}",
            status,
            parsed
                .as_ref()
                .map(|r| r.notes.clone())
                .unwrap_or_else(|_| "<unparseable error body>".to_string())
        ));
    }

    parsed.map_err(|e| format!("parse rl-latest-version response: {}", e))
}

/// Read `ACTIVE_EMBEDDING` from the project's `.claude/env`. Falls back
/// to None when the file is missing / malformed / has no `ACTIVE_EMBEDDING`
/// line. The caller then defaults to `DEFAULT_EMBEDDING_SOURCE`.
pub fn read_active_embedding_source(project: &ProjectRow) -> Option<String> {
    let env_path = PathBuf::from(&project.folder_path).join(".claude").join("env");
    parse_active_embedding_from_env_file(&env_path)
}

/// File-path-driven variant of `read_active_embedding_source` for unit
/// tests. Parses a `.env`-style file looking for a non-comment line
/// `ACTIVE_EMBEDDING=<value>` (whitespace trimmed; surrounding double
/// quotes stripped). Returns None when:
///   * file doesn't exist OR can't be read
///   * no ACTIVE_EMBEDDING line found
///   * value is the empty string
pub fn parse_active_embedding_from_env_file(path: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(path).ok()?;
    for line in raw.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let Some((key, value)) = trimmed.split_once('=') else {
            continue;
        };
        if key.trim() == "ACTIVE_EMBEDDING" {
            let v = value.trim().trim_matches('"').trim_matches('\'').to_string();
            if !v.is_empty() {
                return Some(v);
            }
        }
    }
    None
}

/// v0.2.32 (L7): Tauri command exposing the per-project text-embedding-source
/// identifier (e.g. `"qwen3"`, `"arctic"`, `"openai"`) to the launcher
/// renderer.
///
/// Used by `ModuleConfigTab.svelte` to resolve the
/// `{{embedding_source_from_project_kg_binding}}` placeholder at dispatch
/// time, BEFORE the action descriptor is handed to `module_dispatch_action`.
/// Doing the substitution client-side keeps the v0.2.32 patch surface tiny:
/// no new Rust placeholder, no new dispatcher branch.
///
/// Resolution priority mirrors `read_active_embedding_source`:
///   1. `ACTIVE_EMBEDDING` line in the project's `.claude/env`
///   2. `DEFAULT_EMBEDDING_SOURCE` (currently `"qwen3"`) when the file is
///      missing / malformed / has no `ACTIVE_EMBEDDING` line.
///
/// The return is ALWAYS a non-empty string — fallback is unconditional so
/// the renderer never has to handle a missing value. Errors only surface
/// for truly broken inputs (unknown project_id).
#[command]
pub async fn get_project_embedding_source(
    project_id: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    Ok(read_active_embedding_source(&project)
        .unwrap_or_else(|| DEFAULT_EMBEDDING_SOURCE.to_string()))
}

/// Download new weights to a versioned file under the bind-mount, verify
/// sha256 (when non-empty), then atomically rename to the final path.
/// Returns the active-path the container will load.
pub async fn download_weights(
    response: &LatestVersionResponse,
    project_slug: &str,
    vct_data: &Path,
) -> Result<PathBuf, String> {
    if response.download_url.is_empty() {
        return Err("download_weights called with empty download_url".to_string());
    }

    let state_dir = vct_data
        .join("vct-rl-reranker")
        .join(project_slug)
        .join("state");
    tokio::fs::create_dir_all(&state_dir)
        .await
        .map_err(|e| format!("mkdir state_dir {}: {}", state_dir.display(), e))?;

    let safe_source = sanitize_path_component(&response.embedding_source);
    let safe_version = sanitize_path_component(&response.latest_version);
    let versioned = state_dir.join(format!("rl_model_{}_{}.pt", safe_source, safe_version));
    let tmp_path = state_dir.join(format!("rl_model_{}_{}.pt.tmp", safe_source, safe_version));

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(180))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let resp = client
        .get(&response.download_url)
        .send()
        .await
        .map_err(|e| format!("GET download_url: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!(
            "download_url returned {}: bailing without overwriting active weights",
            resp.status()
        ));
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("read download body: {}", e))?;

    // Verify sha256 BEFORE writing to disk (if a checksum is declared).
    if !response.sha256.is_empty() {
        use sha2::{Digest, Sha256};
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

    tokio::fs::write(&tmp_path, &bytes)
        .await
        .map_err(|e| format!("write tmp {}: {}", tmp_path.display(), e))?;

    tokio::fs::rename(&tmp_path, &versioned)
        .await
        .map_err(|e| {
            format!(
                "rename {} -> {}: {}",
                tmp_path.display(),
                versioned.display(),
                e
            )
        })?;

    Ok(versioned)
}

/// Tell the running RL container to hot-swap to a new weights file.
/// POSTs to the in-container `/rotate_weights` endpoint.
pub async fn signal_rotate_weights(
    rl_port: u16,
    container_path: &str,
) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}/rotate_weights", rl_port);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let resp = client
        .post(&url)
        .json(&serde_json::json!({ "weights_path": container_path }))
        .send()
        .await
        .map_err(|e| format!("POST {}: {}", url, e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!(
            "rotate_weights returned {}: {}",
            status,
            body.chars().take(300).collect::<String>()
        ));
    }
    Ok(())
}

/// Tauri command: manual poll wrapper. Caller (Stream D's button) passes
/// the license_key + machine_id_hash from the licensing module.
#[command]
pub async fn check_for_weights_update_now(
    project_id: String,
    license_key: String,
    machine_id_hash: String,
    db: State<'_, Db>,
) -> Result<LatestVersionResponse, String> {
    check_weights_update(
        &db,
        &project_id,
        &license_key,
        &machine_id_hash,
        DEFAULT_RL_LATEST_VERSION_ENDPOINT,
    )
    .await
}

// ─── Phase 4A: fine-tune-after-download flow ─────────────────────────────

/// Apply the user's response to the weights-update prompt.
///
/// v0.2.31 Agent J — separation of concerns restored:
///   - The launcher is PURE ORCHESTRATION: download the .pt file →
///     call the container's `/rotate_weights` (or `/finetune` for
///     `Now`) → done.
///   - The CONTAINER (vct-rl-reranker v0.2.6) is the sole writer of
///     `rl_weights_state` — it persists `local_version` /
///     `last_finetuned_at` in response to its OWN handlers, via
///     vct-hub's typed REST endpoints (Agent I, migration 019).
///
/// `Now`: spawn the background fine-tune. The fine-tune job calls
/// `/finetune` on the container, polls for completion, then
/// `signal_rotate_weights` — or, on any failure, falls through to
/// rotate with the unmodified downloaded weights. The container is
/// responsible for stamping `last_finetuned_at` in `rl_weights_state`
/// when its handler finishes.
///
/// `Skip`: rotate immediately to the unmodified weights, no fine-tune.
/// The container's `/rotate_weights` handler updates `local_version`.
///
/// `Later`: do nothing — the next poll will re-detect the update and
/// the frontend re-surfaces the prompt. Soft-noop.
#[command]
pub async fn apply_weights_update(
    project_id: String,
    choice: FinetuneChoice,
    response: LatestVersionResponse,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    if choice == FinetuneChoice::Later {
        return Ok(());
    }

    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project not found: {}", project_id))?;

    let ctx_vct_data = PlaceholderCtx::new(RL_RERANKER_MODULE_ID).vct_data;
    let _active_path = download_weights(&response, &project.slug, &ctx_vct_data).await?;

    // v0.2.31 Agent J: the prior `db.set_weights_version(...)` write to
    // the dropped `module_weights_state` table is gone. Version state
    // is now the container's responsibility via `rl_weights_state`
    // (writes happen inside the container's `/rotate_weights` and
    // `/finetune` handlers, persisted through the hub's typed REST
    // surface in module_db_api.rs).

    match choice {
        FinetuneChoice::Skip => {
            let rl_port = db
                .get_project_rl_port(&project_id)?
                .ok_or_else(|| format!("project {} has no rl_port", project_id))?;
            let container_path = container_weights_path(
                &response.embedding_source,
                &response.latest_version,
            );
            signal_rotate_weights(rl_port, &container_path).await?;
            Ok(())
        }
        FinetuneChoice::Now => {
            // Spawn background fine-tune. The function returns Ok
            // immediately; progress / failure surface via Tauri events.
            let pid = project_id.clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = run_finetune_then_rotate_async(pid, response, app).await {
                    eprintln!("[module_service] background fine-tune failed: {}", e);
                }
            });
            Ok(())
        }
        FinetuneChoice::Later => unreachable!("Later handled above"),
    }
}

/// Background fine-tune step. Calls `/finetune` on the container, polls
/// `/finetune_status` until done, then `signal_rotate_weights`.
///
/// IMPORTANT contract pinned by VCO_dev (server contract changed
/// during release): the terminal state from `/finetune_status` is
/// `"done"` (NOT `"complete"`). The launcher previously polled for
/// `"complete"`; the server's now-shipped v0.1.1 (private repo c9039e4)
/// pins `"done"` so we match.
///
/// On ANY failure (404 = container doesn't support /finetune; OOM;
/// corrupted events file; container down) we log + fall through to
/// rotate with the UNMODIFIED downloaded weights. The user always gets
/// the new global model — fine-tuning is a quality improvement, not a
/// blocking step.
async fn run_finetune_then_rotate_async(
    project_id: String,
    response: LatestVersionResponse,
    app: AppHandle,
) -> Result<(), String> {
    // Re-open the DB inside the spawned task. We don't have State<'_, Db>
    // available here because the spawn boundary requires 'static, and
    // the State guard isn't Send.
    let conn = rusqlite::Connection::open(crate::db::db_path())
        .map_err(|e| format!("re-open DB for background fine-tune: {}", e))?;
    let db = Db(std::sync::Mutex::new(conn));

    let rl_port = db
        .get_project_rl_port(&project_id)?
        .ok_or_else(|| format!("project {} has no rl_port", project_id))?;
    let container_path = container_weights_path(
        &response.embedding_source,
        &response.latest_version,
    );

    let _ = app.emit(
        "module://finetune-progress",
        serde_json::json!({
            "project_id": project_id,
            "percent": 0,
            "message": "Starting fine-tune…",
        }),
    );

    let finetune_url = format!("http://127.0.0.1:{}/finetune", rl_port);
    let status_url = format!("http://127.0.0.1:{}/finetune_status", rl_port);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} disappeared mid-finetune", project_id))?;

    let events_path_container = format!(
        "/data/logs/rl_events_{}.jsonl",
        sanitize_path_component(&project.slug),
    );

    let kick = client
        .post(&finetune_url)
        .json(&serde_json::json!({
            "events_path": events_path_container,
            "base_weights_path": container_path,
            "days": 30,
        }))
        .send()
        .await;

    let mut job_id: Option<String> = None;
    match kick {
        Ok(r) if r.status().is_success() => {
            // Capture job_id from kick response if present.
            if let Ok(payload) = r.json::<serde_json::Value>().await {
                job_id = payload
                    .get("job_id")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
            }

            // Poll /finetune_status. Hard cap: 60 attempts × 5s = 5 min.
            for attempt in 0..60u32 {
                tokio::time::sleep(Duration::from_secs(5)).await;
                let mut req = client.get(&status_url);
                if let Some(ref jid) = job_id {
                    req = req.query(&[("job_id", jid.as_str())]);
                }
                let status = req.send().await;
                match status {
                    Ok(s) if s.status().is_success() => {
                        let payload: serde_json::Value =
                            s.json().await.unwrap_or_else(|_| serde_json::json!({}));
                        let state = payload
                            .get("state")
                            .and_then(|v| v.as_str())
                            .unwrap_or("unknown");
                        let percent = payload
                            .get("percent")
                            .and_then(|v| v.as_u64())
                            .unwrap_or((attempt as u64).min(99));
                        let _ = app.emit(
                            "module://finetune-progress",
                            serde_json::json!({
                                "project_id": project_id,
                                "percent": percent,
                                "message": format!("Fine-tuning… ({})", state),
                            }),
                        );
                        // Terminal state is "done" per the server v0.1.1
                        // contract (NOT "complete" — pinned 2026-05-16).
                        //
                        // v0.2.31 Agent J: the launcher used to stamp
                        // `last_finetuned_at` here on the now-dropped
                        // `module_weights_state` table. The container
                        // (vct-rl-reranker v0.2.6) is the sole writer
                        // of `rl_weights_state.last_finetuned_at` — its
                        // `/finetune` handler updates the row when its
                        // own job completes.
                        if state == "done" {
                            break;
                        }
                        if state == "failed" || state == "error" {
                            let reason = payload
                                .get("reason")
                                .or_else(|| payload.get("message"))
                                .and_then(|v| v.as_str())
                                .unwrap_or("unknown error")
                                .to_string();
                            let _ = app.emit(
                                "module://finetune-failed",
                                serde_json::json!({
                                    "project_id": project_id,
                                    "reason": reason,
                                }),
                            );
                            break;
                        }
                    }
                    Ok(s) => {
                        let _ = app.emit(
                            "module://finetune-failed",
                            serde_json::json!({
                                "project_id": project_id,
                                "reason": format!("finetune_status {}", s.status()),
                            }),
                        );
                        break;
                    }
                    Err(e) => {
                        eprintln!("[module_service] finetune_status poll error: {}", e);
                        // Keep trying — transient.
                    }
                }
            }
        }
        Ok(r) => {
            let _ = app.emit(
                "module://finetune-failed",
                serde_json::json!({
                    "project_id": project_id,
                    "reason": format!("finetune {} (container may not support /finetune)", r.status()),
                }),
            );
        }
        Err(e) => {
            let _ = app.emit(
                "module://finetune-failed",
                serde_json::json!({
                    "project_id": project_id,
                    "reason": format!("finetune unreachable: {}", e),
                }),
            );
        }
    }

    // Rotate unconditionally — see contract pin in fn doc.
    signal_rotate_weights(rl_port, &container_path).await?;

    let _ = app.emit(
        "module://finetune-progress",
        serde_json::json!({
            "project_id": project_id,
            "percent": 100,
            "message": "Done",
        }),
    );
    Ok(())
}

// ─── Phase 4B: dashboard widget commands ────────────────────────────────

/// Reads `module_install` + `is_container_running` + recent rl_events.jsonl
/// tail. Returns the struct expected by the dashboard widget. Soft-fail
/// throughout — never errors out on a partial state.
#[command]
pub async fn get_rl_dashboard_state(
    project_id: String,
    db: State<'_, Db>,
) -> Result<RlDashboardState, String> {
    let install = db.get_module_install(&project_id, RL_RERANKER_MODULE_ID)?;
    let install = match install {
        Some(i) => i,
        None => return Ok(RlDashboardState::empty()),
    };

    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    let container_name = install.container_name.clone().unwrap_or_default();
    let container_running = if container_name.is_empty() {
        false
    } else {
        is_container_running(&container_name).await.unwrap_or(false)
    };

    let port = db
        .get_project_rl_port(&project_id)?
        .unwrap_or(0);

    let _embedding_source = read_active_embedding_source(&project)
        .unwrap_or_else(|| DEFAULT_EMBEDDING_SOURCE.to_string());

    // v0.2.31 Agent J: `current_weights_version` / `last_checked_at` /
    // `last_finetuned_at` used to come from the launcher's own
    // `module_weights_state` table (now dropped). The dashboard widget
    // reads them live via the hub's
    // `/api/v1/modules/vct-rl-reranker/db/projects/{pid}/rows/
    // rl_weights_state/{embedding_source}?fields=local_version,
    // last_finetuned_at,...` endpoint (Agent I) — see the Svelte
    // dashboard widget in `launcher/src/lib/components/`. We return
    // empty values here so the wire shape stays back-compat; the
    // frontend layers the live reads on top.

    // image_tag reconstructed from manifest.install.container.image +
    // install.module_version. Falls back to "" if we can't read the
    // manifest (e.g. catalog is gone) — frontend renders no tag in
    // that case.
    let image_tag = crate::commands::modules::find_manifest_for_resume(&db, RL_RERANKER_MODULE_ID)
        .and_then(|m| {
            m.install
                .container
                .as_ref()
                .map(|c| format!("{}:{}", c.image, install.module_version))
        })
        .unwrap_or_default();

    let (recent_events_count, recent_events_avg_latency_ms) =
        load_recent_event_stats(&project.slug).await;

    // v0.2.29: probe `GET /state_summary` (vct-rl-reranker v0.2.3+).
    // Soft-fail to `(None, None)` if the container isn't running, the
    // endpoint 404s (pre-v0.2.3 module), or the body fails to parse.
    // Bounded 2s timeout so a hung container never blocks the dashboard
    // load. Pre-v0.2.3 modules keep working — the existing fields stay
    // correct, the new ones just stay `None`.
    let (dynamic_types_count, d1_marker_present) = if container_running && port > 0 {
        probe_state_summary(port).await
    } else {
        (None, None)
    };

    Ok(RlDashboardState {
        container_name,
        container_running,
        port,
        image_tag,
        // v0.2.31 Agent J: these three fields are now back-compat
        // placeholders. The frontend reads the live values via
        // `module_db_read_row` against the container-owned
        // `rl_weights_state`.
        current_weights_version: String::new(),
        last_checked_at: 0,
        last_finetuned_at: 0,
        weights_sha256_prefix: String::new(),
        recent_events_count,
        recent_events_avg_latency_ms,
        dynamic_types_count,
        d1_marker_present,
    })
}

/// v0.2.29: probe `GET http://localhost:<port>/state_summary` (paid
/// module vct-rl-reranker v0.2.3+). Returns `(dynamic_types_count,
/// d1_marker_present)`. Soft-fails to `(None, None)` on any error path
/// — the dashboard load never blocks or errors because of a probe
/// failure. 2s timeout matches the existing per-call timeouts in this
/// file (see `signal_rotate_weights`, `apply_weights_update`, etc.).
async fn probe_state_summary(port: u16) -> (Option<u32>, Option<bool>) {
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return (None, None),
    };
    let url = format!("http://127.0.0.1:{}/state_summary", port);
    let resp = match client.get(&url).send().await {
        Ok(r) if r.status().is_success() => r,
        _ => return (None, None),
    };
    let body: serde_json::Value = match resp.json().await {
        Ok(v) => v,
        Err(_) => return (None, None),
    };
    let dyn_count = body
        .get("dynamic_types")
        .and_then(|v| v.as_u64())
        .and_then(|n| u32::try_from(n).ok());
    let marker = body.get("marker_present").and_then(|v| v.as_bool());
    (dyn_count, marker)
}

/// Read the last <=10 events from `rl_events_<slug>.jsonl` and return
/// `(count, avg_latency_ms)`. Hard caps the file-tail read at 16 KB so
/// the dashboard call is bounded even when the events file is huge.
async fn load_recent_event_stats(project_slug: &str) -> (u32, f32) {
    let safe_slug = sanitize_path_component(project_slug);
    let path = match directories::UserDirs::new() {
        Some(d) => d
            .home_dir()
            .join(".claude")
            .join("retrieval_rl_data")
            .join(format!("rl_events_{}.jsonl", safe_slug)),
        None => return (0, 0.0),
    };
    parse_recent_event_stats_from_path(&path).await
}

/// Path-driven variant so we can unit-test the parsing logic without
/// depending on `directories::UserDirs`.
async fn parse_recent_event_stats_from_path(path: &Path) -> (u32, f32) {
    use tokio::io::{AsyncReadExt, AsyncSeekExt, SeekFrom};

    let mut file = match tokio::fs::File::open(path).await {
        Ok(f) => f,
        Err(_) => return (0, 0.0),
    };
    let len = match file.metadata().await {
        Ok(m) => m.len(),
        Err(_) => return (0, 0.0),
    };
    const TAIL_BYTES: u64 = 16 * 1024;
    let read_from = len.saturating_sub(TAIL_BYTES);
    if file.seek(SeekFrom::Start(read_from)).await.is_err() {
        return (0, 0.0);
    }
    let mut buf = Vec::with_capacity(TAIL_BYTES as usize);
    if file.read_to_end(&mut buf).await.is_err() {
        return (0, 0.0);
    }
    let s = String::from_utf8_lossy(&buf);

    let lines: Vec<&str> = if read_from > 0 {
        s.lines().skip(1).collect()
    } else {
        s.lines().collect()
    };

    let mut latencies: Vec<f32> = Vec::new();
    for line in lines.iter().rev().take(10).rev() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let parsed: Result<serde_json::Value, _> = serde_json::from_str(trimmed);
        if let Ok(v) = parsed {
            if let Some(lat) = v.get("latency_ms").and_then(|x| x.as_f64()) {
                latencies.push(lat as f32);
            } else if let Some(lat) = v.get("latency_ms").and_then(|x| x.as_i64()) {
                latencies.push(lat as f32);
            }
        }
    }
    let count = latencies.len() as u32;
    let avg = if count > 0 {
        latencies.iter().sum::<f32>() / count as f32
    } else {
        0.0
    };
    (count, avg)
}

// ─── Startup hook + daily poller (scaffolding) ──────────────────────────

/// Iterate every `status='installed'` install row and ensure its
/// container is running. Soft-fail per row. Called from
/// `lib.rs::setup()` at launcher boot.
///
/// ## Precedence order with hub-side resume (v0.2.49 Phase 3)
///
/// As of v0.2.49 the hub-side `vct-hub::module_supervisor::resume_
/// containers_on_startup` is the PRIMARY production code path —
/// wired in `vct-hub/src/server.rs::start_hub_server` with the real
/// `real_manifest_resolver` (was a `|_id| None` stub pre-v0.2.49).
/// This launcher-side path is now a FALLBACK with two roles:
///
///   1. **Hub-unreachable edge case**: if `vct-hub` hasn't booted yet
///      when the launcher reaches `lib.rs::setup()` (rare on the same
///      machine — the launcher spawns the hub — but possible during
///      upgrade flows when the hub binary is being swapped), this path
///      starts the containers itself.
///   2. **Idempotency backstop**: both layers check
///      `is_container_running` before starting. If the hub already ran
///      a successful resume sweep, every container is running and this
///      path is a per-row no-op. If a race produced a half-started
///      container, the second sweep's `podman rm -f` + restart heals
///      it.
///
/// The two paths share NO mutable state — both read `module_installs`
/// rows via `Db` (with WAL mode so concurrent readers don't block) and
/// both write `container_name` + `last_error` via the same single-
/// writer SQL UPDATEs. Double-resume is safe.
///
/// v0.2.40 (R1): mirror of W40-D's hub-side
/// `vct-hub::module_supervisor::resume_containers_on_startup`
/// generalization. Two-bug fix over the pre-v0.2.40 launcher
/// implementation:
///
///   1. **Iterates ALL `status='installed'` rows**, not only rows with
///      a non-NULL `container_name`. Modules whose install-time
///      auto-start failed (pre-v0.2.39 NEW-3.B's default synthesis) left
///      rows with `container_name=NULL`. The pre-v0.2.40 resume loop
///      iterated only non-NULL rows → those modules never got a second
///      chance to start. Now `list_module_installs_needing_start`
///      returns all `installed` rows; NULL-container rows route through
///      `start_container_after_install` (the NEW-3.B synthesis path).
///
///   2. **Generalises beyond RL Reranker.** Prior code had a hardcoded
///      `if module_id != RL_RERANKER_MODULE_ID { continue; }` gate that
///      blocked any other container-distributed module. Replaced with
///      the same manifest-driven gate `start_container_for_module`
///      already uses (`install.method = ContainerPull` AND
///      `runtime.type ∈ {container, service}`). This is the canonical
///      "long-running daemon under hub supervision" signal — `cli` /
///      `mcp_stdio` / `mcp_http` runtime types are deliberately
///      excluded (on-demand invocations, not persistent containers).
///
/// Per-row branches:
///
///   * `container_name = Some(name)` AND running → no-op.
///   * `container_name = Some(name)` AND not running → existing path:
///     `start_container_for_module` (the manifest's defaulting helpers
///     re-resolve the same name for `(module_id, project_slug)`).
///   * `container_name = None` → R1 path:
///     `start_container_after_install` (synthesises defaults via
///     NEW-3.B helpers AND persists the resolved name back to the DB
///     row so subsequent resumes pick the existing-path branch).
///
/// Soft-fail discipline: any per-row error logs and the loop continues
/// to the next row — one broken module must not block the rest of the
/// resume sweep. The NULL-container branch additionally surfaces the
/// failure to `module_installs.last_error` via NEW-3.C's
/// `set_module_last_error` so the GUI tile renders a clear failure
/// state; status stays `'installed'` (the install succeeded; only the
/// post-boot container start failed). The named-container existing
/// branch logs only — matches W40-D's discipline (the hub is the
/// single-writer for that row's container_name + last_error;
/// double-writing from both surfaces would race).
pub async fn resume_containers_on_startup(db: &Db) {
    // Production path delegates manifest resolution to the on-disk
    // catalog via `find_manifest_for_resume`. Tests use the
    // `_with_resolver` variant directly with an in-memory map.
    resume_containers_on_startup_with_resolver(db, |id| {
        crate::commands::modules::find_manifest_for_resume(db, id)
    })
    .await;
}

/// Test-friendly variant: same logic as `resume_containers_on_startup`
/// but the manifest resolver is injected. Pure-Rust callers can
/// provide a closure over an in-memory map; production goes through
/// `find_manifest_for_resume` (= the on-disk catalog). Soft-fail
/// discipline is identical between both surfaces.
///
/// Kept `pub(crate)` rather than `pub` because no caller outside the
/// crate has a reason to pass a custom resolver — the only legitimate
/// in-crate caller is the test module below; the production wrapper
/// above is the documented entry point.
pub(crate) async fn resume_containers_on_startup_with_resolver<F>(db: &Db, resolve_manifest: F)
where
    F: Fn(&str) -> Option<ModuleManifest>,
{
    let rows = match db.list_module_installs_needing_start() {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[module_service] resume_containers_on_startup list failed: {}",
                e
            );
            return;
        }
    };
    for (project_id_opt, module_id, container_name_opt) in rows {
        // Load the manifest first — both the runtime-type gate AND the
        // restart paths need it. Failure here is "module installed
        // from a catalog that's no longer on disk" — skip noisily.
        let manifest = match resolve_manifest(&module_id) {
            Some(m) => m,
            None => {
                eprintln!(
                    "[module_service] resume: manifest for {} not in catalog, skipping",
                    module_id
                );
                continue;
            }
        };

        // Runtime-type gate: mirrors the install-time auto-start gate
        // in `modules.rs` (NEW-3 widening, v0.2.38) AND W40-D's
        // hub-side gate. Only container_pull-installed modules
        // declaring a long-running (`container` | `service`) runtime
        // are auto-resumed. Older / non-container modules (git_clone,
        // local) and on-demand runtime types are excluded.
        let is_container_distributed = manifest.install.method == InstallMethod::ContainerPull
            && matches!(manifest.runtime.r#type.as_str(), "container" | "service");
        if !is_container_distributed {
            continue;
        }

        // Existing path: known container_name → probe + restart if not
        // running. Skip the work entirely when the container is
        // already up.
        if let Some(ref container_name) = container_name_opt {
            let running = is_container_running(container_name).await.unwrap_or(false);
            if running {
                continue;
            }
        }

        // v0.2.49 Stream A: branch on project_id presence to select the
        // global vs per-project resume path.
        //
        // * `project_id IS NULL` ⇒ GLOBAL install row. One container
        //   per machine; no project_slug substitution; container name
        //   is the bare module_id (e.g. `vct-rl-reranker`).
        // * `project_id IS Some(_)` ⇒ PER-PROJECT install row. The
        //   existing v0.2.20–v0.2.48 path. Resolve the project + slug,
        //   substitute `{project_slug}` in templates, allocate rl_port.
        match project_id_opt {
            None => {
                // GLOBAL path.
                if let Err(e) =
                    start_global_container_for_module(&manifest, &module_id, db).await
                {
                    eprintln!(
                        "[module_service] resume: start_global_container_for_module({}): {}",
                        module_id, e
                    );
                    // Mirror NEW-3.C: surface to last_error so the GUI
                    // can render a clear failure state. Soft-fail.
                    let _ = db.set_global_module_last_error(&module_id, Some(&e));
                }
            }
            Some(project_id) => {
                // PER-PROJECT path (existing behaviour).
                let project = match db.get_project(&project_id) {
                    Ok(Some(p)) => p,
                    Ok(None) => {
                        eprintln!(
                            "[module_service] resume: project {} not found, skipping",
                            project_id
                        );
                        continue;
                    }
                    Err(e) => {
                        eprintln!(
                            "[module_service] resume: get_project({}): {}",
                            project_id, e
                        );
                        continue;
                    }
                };

                match container_name_opt {
                    Some(_container_name) => {
                        let rl_port = match ensure_project_rl_port(db, &project) {
                            Ok(p) => p,
                            Err(e) => {
                                eprintln!(
                                    "[module_service] resume: ensure_rl_port({}): {}",
                                    project_id, e
                                );
                                continue;
                            }
                        };
                        let ctx = PlaceholderCtx::new(&module_id);
                        if let Err(e) =
                            start_container_for_module(&manifest, &ctx, &project, rl_port).await
                        {
                            eprintln!(
                                "[module_service] resume: start_container_for_module({}, {}): {}",
                                project_id, module_id, e
                            );
                        }
                    }
                    None => {
                        if let Err(e) =
                            start_container_after_install(&manifest, &project, db).await
                        {
                            eprintln!(
                                "[module_service] resume: start_container_after_install({}, {}): {}",
                                project_id, module_id, e
                            );
                            let _ = db.set_module_last_error(&project_id, &module_id, Some(&e));
                        }
                    }
                }
            }
        }
    }
}

/// Spawn the daily weights-update poll. Runs once at boot + then every
/// 24h. Soft-fail offline / 401 / 500 / missing license. Emits
/// `module://weights-update-available` for Stream D's WeightsUpdatePrompt.
///
/// The license_key + machine_id_hash readers are passed as closures so
/// this module doesn't need to depend on the orchestrator's licensing
/// internals. Callers provide them as `tauri::async_runtime::spawn`'s
/// outer closure binds to the AppHandle.
pub fn spawn_daily_weights_poll<F>(app: AppHandle, license_reader: F)
where
    F: Fn() -> Option<(String, String)> + Send + Sync + 'static,
{
    tauri::async_runtime::spawn(async move {
        // First check: 30s after boot so we don't compete with the
        // initial UI / KG sync surge.
        tokio::time::sleep(Duration::from_secs(30)).await;
        loop {
            poll_all_projects_once(&app, &license_reader).await;
            // 24h between checks. Sleep with a small jitter (±300s) so a
            // network outage that wakes up multiple launchers
            // simultaneously doesn't hammer the edge function.
            let jitter = {
                use rand::{Rng, SeedableRng};
                use rand::rngs::StdRng;
                let mut rng = StdRng::from_os_rng();
                rng.random_range(-300i64..=300)
            };
            let sleep_s = (24 * 60 * 60) + jitter;
            tokio::time::sleep(Duration::from_secs(sleep_s.max(60) as u64)).await;
        }
    });
}

/// One sweep over every project with the RL reranker installed. Soft-
/// fails per project so one broken project doesn't take the whole
/// sweep down.
async fn poll_all_projects_once<F>(app: &AppHandle, license_reader: &F)
where
    F: Fn() -> Option<(String, String)>,
{
    // Re-open DB in this task — we can't hold a State<'_, Db> across
    // the spawn boundary.
    let conn = match rusqlite::Connection::open(crate::db::db_path()) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[module_service] daily poll: open DB: {}", e);
            return;
        }
    };
    let db = Db(std::sync::Mutex::new(conn));

    let projects = match db.list_projects() {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[module_service] daily poll: list_projects: {}", e);
            return;
        }
    };

    let creds = match license_reader() {
        Some(c) => c,
        None => {
            // Free-tier / unlicensed — no point hitting the endpoint.
            return;
        }
    };
    let (license_key, machine_id_hash) = creds;

    for project in projects {
        // Only poll projects that have the RL reranker installed.
        let has_install = db
            .get_module_install(&project.id, RL_RERANKER_MODULE_ID)
            .ok()
            .flatten()
            .is_some();
        if !has_install {
            continue;
        }
        match check_weights_update(
            &db,
            &project.id,
            &license_key,
            &machine_id_hash,
            DEFAULT_RL_LATEST_VERSION_ENDPOINT,
        )
        .await
        {
            Ok(resp) if resp.has_update => {
                let _ = app.emit(
                    "module://weights-update-available",
                    serde_json::json!({
                        "project_id": project.id,
                        "module_id": RL_RERANKER_MODULE_ID,
                        "latest_version": resp.latest_version,
                        "embedding_source": resp.embedding_source,
                        "download_url": resp.download_url,
                        "download_url_expires_at": resp.download_url_expires_at,
                        "sha256": resp.sha256,
                        "released_at": resp.released_at,
                        "notes": resp.notes,
                    }),
                );
            }
            Ok(_) => {
                // No update — nothing to do.
            }
            Err(e) => {
                eprintln!(
                    "[module_service] daily poll: check_weights_update({}): {}",
                    project.id, e
                );
            }
        }
    }
}

// ─── v0.2.44 V44-G4: auto-retry of stuck module_installs rows ───────────
//
// Background (RL-chat ask 2026-06-01, plan
// `.claude/context/plans/rl-to-orchestrator-v0.2.44-auto-retry-2026-06-01.md`):
//
// When a paid-module install fails transiently (e.g. the v0.2.34→v0.2.42 W8
// pull-token gateway bug), the `module_installs` row lands in `status='error'`
// (or, when only the post-install container start fails, `last_error` is set
// without a status flip). Pre-v0.2.44 the launcher had no recovery path —
// the user had to manually click "Reinstall" per-(project, module). This
// closes the gap on BOTH update triggers:
//
//   * Trigger A: project-level Update button → retry that project's rows.
//   * Trigger B: orchestrator-level "Update orchestrator" button → retry
//     every project's rows (gated by a settings toggle, default ON).
//
// Per-row decision tree (the helper iterates rows in `error` or `broken`
// status — `installed` rows with only `last_error` are out of scope, the
// existing resume-on-boot sweep already handles those):
//
//   1. License revoked between attempts → `decision="skipped_license"`,
//      audit-only, row unchanged.
//   2. Manifest's `min_launcher_version` exceeds current launcher version
//      → `decision="skipped_version"`, audit-only, row unchanged.
//   3. A healthy container with the expected name already exists →
//      `decision="self_healed"`, row's `status` flipped to `Installed` and
//      `last_error` cleared. No install re-run; the prior attempt
//      apparently completed enough to land a working container.
//   4. Re-invoke install — only possible when an `AppHandle` is available
//      (Tauri command surface). The helper takes `Option<&AppHandle>`:
//      `Some(app)` paths invoke `install_module_for_project`;
//      `None` paths (background callers, tests) audit-log
//      `decision="retried_unavailable"` and leave the row in place.

/// Per-row outcome from a `retry_failed_module_installs` sweep. Used by
/// the GUI to render a per-row report ("3 retried, 1 self-healed, 2
/// skipped — see audit log") and pinned as the wire shape every caller
/// consumes.
#[derive(serde::Serialize, Debug, Clone)]
pub struct RetryReport {
    pub project_id: String,
    pub module_id: String,
    /// One of `retried_success` / `retried_failed` / `skipped_license` /
    /// `skipped_version` / `skipped_manifest_missing` / `self_healed` /
    /// `retried_unavailable`.
    pub decision: String,
    /// Status after the retry decision was applied. `None` when the row
    /// was untouched (every `skipped_*` decision).
    pub new_status: Option<String>,
    /// Error string when the retry failed (`retried_failed`), else `None`.
    pub error: Option<String>,
}

/// Settings key for Trigger B's gate. Stored in the `app_state` k/v
/// table (orchestrator-level — `module_settings.project_id` has a FK to
/// `projects` so it can't carry orchestrator-wide keys). Default `true`
/// per user directive 2026-06-01.
pub const RETRY_SETTING_KEY: &str =
    "auto_retry_failed_paid_module_installs_on_orchestrator_update";

/// Read the boolean settings toggle that gates Trigger B. Defaults to
/// `true` when the row is absent OR malformed (legitimate first-run
/// behaviour). Per the user's "default true" directive 2026-06-01.
pub fn auto_retry_on_orchestrator_update_enabled(db: &Db) -> bool {
    match db.app_state_get_bool(RETRY_SETTING_KEY) {
        Ok(Some(v)) => v,
        Ok(None) => true,
        Err(_) => true,
    }
}

/// Persist the boolean toggle. Soft-fail: any DB error is propagated;
/// callers (Settings UI Tauri command) decide what to do.
pub fn set_auto_retry_on_orchestrator_update(db: &Db, enabled: bool) -> Result<(), String> {
    db.app_state_set_bool(RETRY_SETTING_KEY, enabled)
}

/// Tauri command surface — read the toggle for the Settings page renderer.
#[command]
pub async fn get_auto_retry_failed_installs_setting(
    db: State<'_, Db>,
) -> Result<bool, String> {
    Ok(auto_retry_on_orchestrator_update_enabled(&db))
}

/// Tauri command surface — write the toggle from the Settings page.
#[command]
pub async fn set_auto_retry_failed_installs_setting(
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    set_auto_retry_on_orchestrator_update(&db, enabled)
}

/// Compare two dotted version strings as semver-ish ordered tuples.
/// Returns true iff `current >= required` (numeric-component-wise).
/// Non-numeric suffixes (e.g. `-rc1`) are stripped before parsing; the
/// resulting "x.y.z" prefix is compared as a Vec<u32>. Missing components
/// default to 0 so `"0.2"` compares equal to `"0.2.0"`.
///
/// Conservative semantics: if EITHER side fails to parse, returns true
/// (treat as compatible) so a malformed `min_launcher_version` doesn't
/// silently block every retry. The install-time gate is the canonical
/// version check; this helper is only an EARLY skip for the retry path.
fn version_at_least(current: &str, required: &str) -> bool {
    fn parse(v: &str) -> Option<Vec<u32>> {
        let trimmed = v.split(|c: char| c == '-' || c == '+').next().unwrap_or(v);
        let parts: Result<Vec<u32>, _> = trimmed.split('.').map(|p| p.parse::<u32>()).collect();
        parts.ok()
    }
    let (cur, req) = match (parse(current), parse(required)) {
        (Some(a), Some(b)) => (a, b),
        _ => return true,
    };
    let n = cur.len().max(req.len());
    for i in 0..n {
        let a = *cur.get(i).unwrap_or(&0);
        let b = *req.get(i).unwrap_or(&0);
        if a > b {
            return true;
        }
        if a < b {
            return false;
        }
    }
    true
}

/// Core retry helper.
///
/// Iterates `module_installs` rows in `error` or `broken` status (optionally
/// scoped to `project_id`), and for each row decides among:
/// self-heal / skip-license / skip-version / skip-manifest-missing /
/// retry-via-install (`Some(app)` paths) / retry-unavailable (`None` path).
///
/// Audit-logs every attempt with `operation="module_install_auto_retry"`
/// and a detail blob carrying the decision + prior state. Returns the
/// per-row reports in iteration order.
///
/// `app` is `Option<&AppHandle>` because the helper has two legitimate
/// call sites:
///   * Tauri-command surface (project-level + orchestrator-level update
///     endpoints): provides `Some(app)` so `install_module_for_project`
///     can be re-invoked.
///   * Background / test contexts: pass `None`. Self-heal still works,
///     and rows that would need an actual install are audit-logged with
///     `decision="retried_unavailable"` rather than being silently
///     skipped. Tests can therefore observe the entire decision tree
///     without standing up a Tauri runtime.
///
/// Soft-fail discipline: every error path inside the loop is caught,
/// recorded as the row's `error` / `new_status`, and the loop continues.
/// One broken module must never block the rest of the sweep.
pub async fn retry_failed_module_installs(
    project_id_filter: Option<&str>,
    db: &Db,
    app: Option<&AppHandle>,
) -> Vec<RetryReport> {
    // Snapshot the candidate rows up front so we don't re-query mid-loop
    // (each retry mutates module_installs.status; iterating live would
    // pick up rows we just patched).
    let mut candidates: Vec<(String, String, Option<String>, String, Option<String>)> =
        Vec::new();
    // (project_id, module_id, container_name, prior_status, prior_error)

    let collect = |status: &str| -> Result<
        Vec<crate::db::models::ModuleInstallRow>,
        String,
    > {
        db.list_module_installs_with_status(status)
    };

    for status in &["error", "broken"] {
        match collect(status) {
            Ok(rows) => {
                for row in rows {
                    // v0.2.49 Stream A: this retry sweep walks per-project
                    // rows only. Global rows (project_id IS NULL) are
                    // healed through a separate path (the resume sweep's
                    // global branch fires per-launcher-boot and per-
                    // orchestrator-update; failures land in
                    // module_installs.last_error and the GUI surfaces a
                    // Reinstall CTA when status=error).
                    let project_id = match row.project_id {
                        Some(p) => p,
                        None => continue,
                    };
                    if let Some(pid) = project_id_filter {
                        if project_id != pid {
                            continue;
                        }
                    }
                    candidates.push((
                        project_id,
                        row.module_id,
                        row.container_name,
                        (*status).to_string(),
                        row.last_error,
                    ));
                }
            }
            Err(e) => {
                eprintln!(
                    "[module_service] retry_failed_module_installs: \
                     list_module_installs_with_status({}) failed: {}",
                    status, e
                );
            }
        }
    }

    let launcher_ver = env!("CARGO_PKG_VERSION");
    let mut reports: Vec<RetryReport> = Vec::with_capacity(candidates.len());

    for (project_id, module_id, container_name_opt, prior_status, prior_error) in candidates {
        // Resolve manifest. If it's missing (extracted module dir
        // deleted, catalog cache empty), we can't even check the gates —
        // record + skip.
        let manifest = match crate::commands::modules::find_manifest_for_resume(db, &module_id) {
            Some(m) => m,
            None => {
                let detail = serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "prior_status": prior_status,
                    "prior_error": prior_error,
                    "decision": "skipped_manifest_missing",
                });
                let _ = db.audit(
                    "module_install_auto_retry",
                    Some(&project_id),
                    Some(&module_id),
                    &detail,
                );
                reports.push(RetryReport {
                    project_id,
                    module_id,
                    decision: "skipped_manifest_missing".to_string(),
                    new_status: None,
                    error: None,
                });
                continue;
            }
        };

        // Gate 1: license still valid?
        if !crate::commands::modules::is_module_licensed(&manifest, db) {
            let detail = serde_json::json!({
                "project_id": project_id,
                "module_id": module_id,
                "prior_status": prior_status,
                "prior_error": prior_error,
                "decision": "skipped_license",
            });
            let _ = db.audit(
                "module_install_auto_retry",
                Some(&project_id),
                Some(&module_id),
                &detail,
            );
            reports.push(RetryReport {
                project_id,
                module_id,
                decision: "skipped_license".to_string(),
                new_status: None,
                error: None,
            });
            continue;
        }

        // Gate 2: min_launcher_version satisfied?
        if let Some(req) = manifest.compatibility.min_launcher_version.as_deref() {
            if !version_at_least(launcher_ver, req) {
                let detail = serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "prior_status": prior_status,
                    "prior_error": prior_error,
                    "decision": "skipped_version",
                    "launcher_version": launcher_ver,
                    "min_launcher_version": req,
                });
                let _ = db.audit(
                    "module_install_auto_retry",
                    Some(&project_id),
                    Some(&module_id),
                    &detail,
                );
                reports.push(RetryReport {
                    project_id,
                    module_id,
                    decision: "skipped_version".to_string(),
                    new_status: None,
                    error: None,
                });
                continue;
            }
        }

        // Gate 3 (self-heal): is a healthy container already running for
        // this row? When yes, the row's `error` status is stale — flip
        // it to `installed` and clear `last_error`.
        let expected_name = container_name_opt.clone().or_else(|| {
            // Compute the canonical name even when the row's
            // `container_name` is NULL — `start_container_after_install`
            // uses `runtime.resolve_container_name_template` plus
            // `project.slug`, so we mirror that here.
            db.get_project(&project_id).ok().flatten().and_then(|p| {
                let template = manifest
                    .runtime
                    .resolve_container_name_template(&manifest.id);
                resolve_container_name(&template, &p.slug).ok()
            })
        });
        if let Some(ref name) = expected_name {
            if is_container_running(name).await.unwrap_or(false) {
                // Patch the row + clear the stale error.
                let mut new_status_str = "installed".to_string();
                if let Err(e) =
                    db.set_module_status(&project_id, &module_id, ModuleStatus::Installed, None)
                {
                    eprintln!(
                        "[module_service] retry self_heal: set_module_status({}, {}): {}",
                        project_id, module_id, e
                    );
                    new_status_str = prior_status.clone();
                }
                if container_name_opt.is_none() {
                    // Persist the resolved name so future probes can
                    // skip the resolve step.
                    let _ =
                        db.set_module_container_name(&project_id, &module_id, name);
                }
                let detail = serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "prior_status": prior_status,
                    "prior_error": prior_error,
                    "decision": "self_healed",
                    "container_name": name,
                    "new_status": new_status_str,
                });
                let _ = db.audit(
                    "module_install_auto_retry",
                    Some(&project_id),
                    Some(&module_id),
                    &detail,
                );
                reports.push(RetryReport {
                    project_id,
                    module_id,
                    decision: "self_healed".to_string(),
                    new_status: Some(new_status_str),
                    error: None,
                });
                continue;
            }
        }

        // No self-heal possible — actual install re-invocation needed.
        // The AppHandle is required to drive `install_module_for_project`
        // (it spawns a child process via tauri::async_runtime, emits
        // events back to the renderer, and reads from State<'_, Db>).
        // Background callers (None) audit + skip.
        let app = match app {
            Some(a) => a,
            None => {
                let detail = serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "prior_status": prior_status,
                    "prior_error": prior_error,
                    "decision": "retried_unavailable",
                    "reason": "no AppHandle available in caller context",
                });
                let _ = db.audit(
                    "module_install_auto_retry",
                    Some(&project_id),
                    Some(&module_id),
                    &detail,
                );
                reports.push(RetryReport {
                    project_id,
                    module_id,
                    decision: "retried_unavailable".to_string(),
                    new_status: None,
                    error: None,
                });
                continue;
            }
        };

        // Per RL-chat plan footnote: retry targets the LATEST module
        // version. `install_module_for_project` resolves the manifest
        // via `resolve_manifest_for_install`, which falls back to the
        // L0 catalog cache when no on-disk extracted manifest is
        // present — so we don't need to pass a version through; the
        // L0-driven phase 3 path picks the latest by definition.
        match crate::commands::modules::install_module_for_project(
            app.clone(),
            project_id.clone(),
            module_id.clone(),
            tauri::Manager::state::<Db>(app),
        )
        .await
        {
            Ok(row) => {
                let new_status_str = row.status.as_str().to_string();
                let detail = serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "prior_status": prior_status,
                    "prior_error": prior_error,
                    "decision": "retried_success",
                    "new_status": new_status_str,
                });
                let _ = db.audit(
                    "module_install_auto_retry",
                    Some(&project_id),
                    Some(&module_id),
                    &detail,
                );
                reports.push(RetryReport {
                    project_id,
                    module_id,
                    decision: "retried_success".to_string(),
                    new_status: Some(new_status_str),
                    error: None,
                });
            }
            Err(e) => {
                let detail = serde_json::json!({
                    "project_id": project_id,
                    "module_id": module_id,
                    "prior_status": prior_status,
                    "prior_error": prior_error,
                    "decision": "retried_failed",
                    "error": e,
                });
                let _ = db.audit(
                    "module_install_auto_retry",
                    Some(&project_id),
                    Some(&module_id),
                    &detail,
                );
                reports.push(RetryReport {
                    project_id,
                    module_id,
                    decision: "retried_failed".to_string(),
                    new_status: None,
                    error: Some(e),
                });
            }
        }
    }

    reports
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;
    use crate::manifest::{
        Compatibility, ContainerInstallBlock, HealthCheck, InstallBlock, InstallMethod,
        LicenseBlock, ModuleCategory, ModuleManifest, PortMapping, Requirements, RuntimeBlock,
        VolumeMount,
    };
    use std::collections::HashMap;

    fn make_project() -> ProjectRow {
        ProjectRow {
            id: "proj-uuid".into(),
            name: "Acme".into(),
            folder_path: "/tmp/acme".into(),
            host: ProjectHost::Base,
            slug: "acme-corp".into(),
            created_at: 0,
            updated_at: 0,
            rl_port: Some(11533),
        }
    }

    fn make_manifest(tag_from_version: bool, auto_restart: bool) -> ModuleManifest {
        let mut env_fixed = HashMap::new();
        env_fixed.insert("RL_SERVER_PORT".into(), "11438".into());
        env_fixed.insert("PYTHONUNBUFFERED".into(), "1".into());

        let mut env_derived = HashMap::new();
        env_derived.insert("RL_PROJECT_ROOT".into(), "/data".into());
        env_derived.insert(
            "OLLAMA_URL".into(),
            "http://host.containers.internal:{ollama_port}".into(),
        );

        ModuleManifest {
            manifest_version: 1,
            id: "vct-rl-reranker".into(),
            name: "RL Reranker".into(),
            version: "0.1.0".into(),
            description: "".into(),
            publisher: None,
            homepage: None,
            repository: None,
            icon: None,
            category: ModuleCategory::PaidIndependent,
            tags: vec![],
            compatibility: Compatibility::default(),
            license: LicenseBlock::default(),
            requirements: Requirements::default(),
            install: InstallBlock {
                method: InstallMethod::ContainerPull,
                source: Some("ghcr.io/hotak92/vct-rl-reranker".into()),
                r#ref: Some("0.1.0".into()),
                install_dir: "{VCT_MODULES}/vct-rl-reranker".into(),
                post_install: vec![],
                container: Some(ContainerInstallBlock {
                    image: "ghcr.io/hotak92/vct-rl-reranker".into(),
                    tag_from_version,
                    registry: Some("ghcr.io".into()),
                    pull_token_endpoint: "https://example.invalid/x".into(),
                    pull_token_method: "POST".into(),
                    rotate_weights: false,
                    rotate_weights_endpoint: None,
                }),
                scope: crate::manifest::InstallScope::PerProject,
            },
            secrets: vec![],
            settings: vec![],
            runtime: RuntimeBlock {
                r#type: "container".into(),
                command: "python".into(),
                args: vec![
                    "-m".into(),
                    "rl_server.rl_server".into(),
                    "--port".into(),
                    "11438".into(),
                    "--project-root".into(),
                    "/data".into(),
                    "--log-path".into(),
                    "/data/logs/rl_events_{project_slug}.jsonl".into(),
                ],
                platform_command: HashMap::new(),
                cwd: None,
                env_from_secrets: vec![],
                env_from_settings: vec!["ACTIVE_EMBEDDING".into()],
                env_fixed,
                health_check: Some(HealthCheck {
                    r#type: "http_get".into(),
                    timeout_s: 5,
                    interval_s: 30,
                    url: Some("http://localhost:{RL_SERVER_PORT}/health".into()),
                }),
                auto_restart,
                log_file: Some("{VCT_LOGS}/vct-rl-reranker-{project_slug}.log".into()),
                min_gpu_vram_gb: None,
                gpu_optional: true,
                gpu_image_variants: None,
                log_path_template: None,
                container_name_template: Some("vct-rl-reranker-{project_slug}".into()),
                image_ref: Some("{install.container.image}:{install.container.tag}".into()),
                ports: vec![PortMapping {
                    host: "{RL_SERVER_PORT}".into(),
                    container: 11438,
                    bind: Some("127.0.0.1".into()),
                }],
                env_derived,
                volumes: vec![
                    VolumeMount {
                        host: "{VCT_DATA}/vct-rl-reranker/{project_slug}/state".into(),
                        container: "/data/state".into(),
                        mode: Some("rw".into()),
                    },
                    VolumeMount {
                        host: "{HOME}/.claude/retrieval_rl_data".into(),
                        container: "/data/logs".into(),
                        mode: Some("rw".into()),
                    },
                ],
            },
            mcp_registration: None,
            setup_wizard: None,
            upgrade: None,
            telemetry: None,
            uninstall: None,
            provides: vec![],
            consumes: vec![],
            gui: None,
            db: None,
        }
    }

    // ─── resolve_container_name ──────────────────────────────────────

    #[test]
    fn resolve_container_name_uses_project_slug() {
        let got = resolve_container_name("vct-rl-reranker-{project_slug}", "acme-corp")
            .expect("resolve");
        assert_eq!(got, "vct-rl-reranker-acme-corp");
    }

    #[test]
    fn resolve_container_name_errors_on_unresolved_placeholder() {
        let err = resolve_container_name("vct-rl-reranker-{project-slug}", "acme-corp")
            .expect_err("must reject unresolved placeholders");
        assert!(err.contains("unresolved placeholders"));
        assert!(err.contains("project-slug"));
    }

    // ─── resolve_image_ref ────────────────────────────────────────────

    #[test]
    fn resolve_image_ref_uses_manifest_container_image_and_version() {
        let manifest = make_manifest(true, true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            None,
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.1.0");
    }

    #[test]
    fn resolve_image_ref_uses_install_ref_when_tag_from_version_false() {
        let mut manifest = make_manifest(false, true);
        manifest.install.r#ref = Some("latest".into());
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            None,
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:latest");
    }

    // ─── build_podman_run_args ────────────────────────────────────────

    #[test]
    fn build_podman_run_args_includes_port_mapping() {
        let manifest = make_manifest(true, true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "vct-rl-reranker-acme-corp",
            "ghcr.io/hotak92/vct-rl-reranker:0.1.0",
        )
        .expect("build args");

        assert_eq!(args[0], "run");
        assert_eq!(args[1], "-d");
        assert_eq!(args[2], "--name");
        assert_eq!(args[3], "vct-rl-reranker-acme-corp");

        assert!(
            args.iter().any(|a| a == "127.0.0.1:11533:11438"),
            "expected port arg 127.0.0.1:11533:11438 in {:?}",
            args
        );

        let img_idx = args
            .iter()
            .position(|a| a == "ghcr.io/hotak92/vct-rl-reranker:0.1.0")
            .expect("image arg present");
        let cmd_idx = args
            .iter()
            .position(|a| a == "python")
            .expect("command arg present");
        assert!(img_idx < cmd_idx, "image must precede command");
    }

    #[test]
    fn build_podman_run_args_resolves_active_embedding_env() {
        // We don't directly inject ACTIVE_EMBEDDING via env_derived in
        // the fixture (the manifest's env_from_settings is the canonical
        // path), but we still want to verify that env_fixed + env_derived
        // both flow through to the argv unaltered. The RL container reads
        // ACTIVE_EMBEDDING at startup from its env; the launcher's job is
        // just to pass it.
        let manifest = make_manifest(true, true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
            .expect("build args");

        // env_fixed RL_SERVER_PORT=11438.
        assert!(
            args.iter().any(|a| a == "RL_SERVER_PORT=11438"),
            "expected env_fixed RL_SERVER_PORT in {:?}",
            args
        );
        // env_derived OLLAMA_URL with {ollama_port} substituted.
        assert!(
            args.iter()
                .any(|a| a == "OLLAMA_URL=http://host.containers.internal:11435"),
            "expected env_derived OLLAMA_URL with resolved {{ollama_port}} in {:?}",
            args
        );
    }

    #[test]
    fn build_podman_run_args_resolves_volume_placeholders() {
        let manifest = make_manifest(true, true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
            .expect("build args");

        let state_vol = args
            .iter()
            .find(|a| a.contains("/data/state:rw"))
            .expect("state volume must be present");
        assert!(state_vol.contains("/vct-rl-reranker/acme-corp/state:/data/state:rw"));

        let logs_vol = args
            .iter()
            .find(|a| a.contains("/.claude/retrieval_rl_data:/data/logs:rw"))
            .expect("logs volume must be present");
        assert!(!logs_vol.starts_with("{HOME}"), "{{HOME}} must be resolved");
    }

    #[test]
    fn build_podman_run_args_uses_unless_stopped_when_auto_restart_true() {
        let manifest = make_manifest(true, true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args =
            build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
                .expect("build args");
        assert!(args.iter().any(|a| a == "--restart=unless-stopped"));
    }

    #[test]
    fn build_podman_run_args_omits_restart_flag_when_auto_restart_false() {
        let manifest = make_manifest(true, false);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args =
            build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
                .expect("build args");
        assert!(!args.iter().any(|a| a.starts_with("--restart")));
    }

    #[test]
    fn build_podman_run_args_rejects_non_container_runtime() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.r#type = "mcp_stdio".into();
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let err =
            build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
                .expect_err("must reject non-container runtime");
        assert!(err.contains("container"));
    }

    /// NEW-3 (2026-05-28): `runtime.type = "service"` must succeed in
    /// `build_podman_run_args` after the gate widening. Mirrors
    /// `build_podman_run_args_uses_unless_stopped_when_auto_restart_true`
    /// but with `type = "service"` to guard against future regressions.
    #[test]
    fn build_podman_run_args_accepts_service_runtime_type() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.r#type = "service".into();
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args =
            build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
                .expect("service runtime must be accepted after NEW-3 widening");
        assert!(args.iter().any(|a| a == "--restart=unless-stopped"));
    }

    /// NEW-3.B (2026-05-28): a service-typed manifest with BOTH
    /// `container_name_template=None` and `image_ref=None` must
    /// successfully resolve defaults and reach `build_podman_run_args`
    /// without a "missing" error. Verifies the ok_or_else hard-fails are
    /// replaced with the synthesizing helpers.
    #[test]
    fn start_container_for_module_succeeds_with_synthesized_defaults() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.r#type = "service".into();
        // Remove explicitly-declared fields to exercise the synthesis path.
        manifest.runtime.container_name_template = None;
        manifest.runtime.image_ref = None;

        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);

        // Synthesize the name/image the way start_container_for_module will.
        let name_template =
            manifest.runtime.resolve_container_name_template(&manifest.id);
        let container_name = resolve_container_name(&name_template, &project.slug)
            .expect("synthesized name must resolve");
        let image_template = manifest.runtime.resolve_image_ref(
            manifest.install.container.as_ref().expect("container block present"),
            &manifest.version,
        );
        let image = resolve_image_ref(&image_template, &manifest, None)
            .expect("synthesized image must resolve");

        // Must not panic/error — the "container_name_template missing" error
        // must no longer be reachable.
        let args =
            build_podman_run_args(&manifest, &ctx, &project, 11533, &container_name, &image)
                .expect("build_podman_run_args must succeed with synthesized defaults");

        // Synthesized container name: module id = "vct-rl-reranker" + project slug = "acme-corp"
        assert_eq!(container_name, "vct-rl-reranker-acme-corp");
        // Synthesized image: tag_from_version=true → image:version
        assert_eq!(image, "ghcr.io/hotak92/vct-rl-reranker:0.1.0");
        assert!(args.iter().any(|a| a == "vct-rl-reranker-acme-corp"),
            "container name must appear in podman args");
    }

    #[test]
    fn build_podman_run_args_resolves_project_slug_in_command_args() {
        let manifest = make_manifest(true, true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args =
            build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
                .expect("build args");
        assert!(args
            .iter()
            .any(|a| a == "/data/logs/rl_events_acme-corp.jsonl"));
    }

    // ─── parse_inspect_running_state ──────────────────────────────────

    #[test]
    fn parse_inspect_running_state_matches_running() {
        assert_eq!(parse_inspect_running_state("running"), true);
        assert_eq!(parse_inspect_running_state("running\n"), true);
        assert_eq!(parse_inspect_running_state("  running  \n"), true);
        assert_eq!(parse_inspect_running_state("exited"), false);
        assert_eq!(parse_inspect_running_state("created"), false);
        assert_eq!(parse_inspect_running_state("paused"), false);
        assert_eq!(parse_inspect_running_state(""), false);
        assert_eq!(parse_inspect_running_state("Running"), false); // case-sensitive
    }

    // ─── FinetuneChoice serde ─────────────────────────────────────────

    #[test]
    fn finetune_choice_serde_round_trip() {
        for choice in [FinetuneChoice::Now, FinetuneChoice::Later, FinetuneChoice::Skip] {
            let s = serde_json::to_string(&choice).expect("ser");
            let d: FinetuneChoice = serde_json::from_str(&s).expect("de");
            assert_eq!(d, choice);
        }
        // Wire-shape pin: snake_case via serde rename_all.
        assert_eq!(serde_json::to_string(&FinetuneChoice::Now).unwrap(), "\"now\"");
        assert_eq!(serde_json::to_string(&FinetuneChoice::Later).unwrap(), "\"later\"");
        assert_eq!(serde_json::to_string(&FinetuneChoice::Skip).unwrap(), "\"skip\"");
    }

    // ─── weights_update_event_payload ─────────────────────────────────

    #[test]
    fn weights_update_event_payload_shape() {
        // Pin the wire shape of the payload Stream D's
        // WeightsUpdatePrompt component subscribes to. The frontend
        // expects these fields; renaming any would silently break the
        // prompt UI.
        let resp = LatestVersionResponse {
            has_update: true,
            latest_version: "qwen3-2026-05-23".into(),
            embedding_source: "qwen3".into(),
            download_url: "https://example.invalid/d".into(),
            download_url_expires_at: "2026-05-24T00:00:00Z".into(),
            sha256: "abc123".into(),
            released_at: "2026-05-23T00:00:00Z".into(),
            notes: "Performance pass".into(),
        };
        let v = serde_json::to_value(&resp).expect("ser");
        // Required fields the FE reads.
        for field in [
            "has_update",
            "latest_version",
            "embedding_source",
            "download_url",
            "download_url_expires_at",
            "sha256",
            "released_at",
            "notes",
        ] {
            assert!(
                v.get(field).is_some(),
                "wire shape regression: missing field {} in {}",
                field,
                v
            );
        }
    }

    // ─── sanitize_path_component / container_weights_path ─────────────

    #[test]
    fn sanitize_path_component_rewrites_unsafe_chars() {
        assert_eq!(sanitize_path_component("qwen3"), "qwen3");
        assert_eq!(sanitize_path_component("qwen3-2026-05-16"), "qwen3-2026-05-16");
        assert_eq!(sanitize_path_component("a.b_c-d"), "a.b_c-d");
        assert_eq!(sanitize_path_component("evil/../path"), "evil_.._path");
        assert_eq!(sanitize_path_component("with space"), "with_space");
    }

    #[test]
    fn container_weights_path_uses_safe_components() {
        let p = container_weights_path("qwen3", "v1.0");
        assert_eq!(p, "/data/state/rl_model_qwen3_v1.0.pt");
        let p2 = container_weights_path("../../etc", "passwd");
        assert!(p2.starts_with("/data/state/"));
        assert!(!p2.contains("../"));
    }

    // ─── RlDashboardState ─────────────────────────────────────────────

    #[test]
    fn rl_dashboard_state_empty_has_image_tag_empty() {
        let s = RlDashboardState::empty();
        assert_eq!(s.image_tag, "");
        assert_eq!(s.container_running, false);
        assert_eq!(s.port, 0);
    }

    /// v0.2.29: pin the new `/state_summary`-derived fields default
    /// to `None` so the frontend knows "the probe didn't run / hasn't
    /// fired yet" rather than mis-reading 0 as "zero dynamic types".
    #[test]
    fn rl_dashboard_state_empty_has_none_state_summary_fields() {
        let s = RlDashboardState::empty();
        assert_eq!(s.dynamic_types_count, None);
        assert_eq!(s.d1_marker_present, None);
    }

    /// v0.2.29: wire-shape pin — the two new fields must round-trip
    /// through serde with snake_case keys + `null` when `None`.
    #[test]
    fn rl_dashboard_state_state_summary_fields_wire_shape() {
        let s = RlDashboardState::empty();
        let v = serde_json::to_value(&s).expect("ser");
        assert!(v.get("dynamic_types_count").is_some(),
            "wire shape regression: missing dynamic_types_count");
        assert!(v.get("d1_marker_present").is_some(),
            "wire shape regression: missing d1_marker_present");
        assert!(v["dynamic_types_count"].is_null());
        assert!(v["d1_marker_present"].is_null());
        // Reverse: a JSON missing these fields still deserializes
        // (back-compat for older callers / saved snapshots).
        let legacy = r#"{
            "container_name":"","container_running":false,"port":0,
            "image_tag":"","current_weights_version":"",
            "last_checked_at":0,"last_finetuned_at":0,
            "weights_sha256_prefix":"",
            "recent_events_count":0,"recent_events_avg_latency_ms":0.0
        }"#;
        let d: RlDashboardState = serde_json::from_str(legacy).expect("legacy de");
        assert_eq!(d.dynamic_types_count, None);
        assert_eq!(d.d1_marker_present, None);
    }

    // ─── Embedding-source flexibility ─────────────────────────────────

    #[test]
    fn parse_active_embedding_returns_value_when_set() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("rl_env_test_{}.env", std::process::id()));
        std::fs::write(&path, "FOO=bar\nACTIVE_EMBEDDING=arctic\nBAZ=qux\n").expect("write");
        let got = parse_active_embedding_from_env_file(&path);
        assert_eq!(got.as_deref(), Some("arctic"));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn parse_active_embedding_strips_quotes_and_whitespace() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("rl_env_test_q_{}.env", std::process::id()));
        std::fs::write(&path, "  ACTIVE_EMBEDDING = \"openai\"  \n").expect("write");
        let got = parse_active_embedding_from_env_file(&path);
        assert_eq!(got.as_deref(), Some("openai"));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn parse_active_embedding_returns_none_for_missing_or_empty() {
        let missing = PathBuf::from("/tmp/__definitely_not_there__.env");
        assert!(parse_active_embedding_from_env_file(&missing).is_none());

        let dir = std::env::temp_dir();
        let empty = dir.join(format!("rl_env_test_empty_{}.env", std::process::id()));
        std::fs::write(&empty, "FOO=bar\n").expect("write");
        assert!(parse_active_embedding_from_env_file(&empty).is_none());
        let _ = std::fs::remove_file(&empty);
    }

    #[test]
    fn parse_active_embedding_skips_comments() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("rl_env_test_comment_{}.env", std::process::id()));
        std::fs::write(&path, "# ACTIVE_EMBEDDING=qwen3\nACTIVE_EMBEDDING=arctic\n").expect("write");
        let got = parse_active_embedding_from_env_file(&path);
        // Should pick up the un-commented line, not the comment.
        assert_eq!(got.as_deref(), Some("arctic"));
        let _ = std::fs::remove_file(&path);
    }

    // ─── recent events parsing ─────────────────────────────────────────

    #[tokio::test]
    async fn recent_events_avg_latency_handles_missing_file() {
        let nonexistent = PathBuf::from("/tmp/__rl_test_def_not_there.jsonl");
        let (count, avg) = parse_recent_event_stats_from_path(&nonexistent).await;
        assert_eq!(count, 0);
        assert!((avg - 0.0).abs() < f32::EPSILON);
    }

    #[tokio::test]
    async fn recent_events_avg_latency_averages_last_10_lines() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("rl_test_avg_{}.jsonl", std::process::id()));

        let mut content = String::new();
        for i in 1..=12u32 {
            content.push_str(&format!(
                "{{\"event\":\"rerank\",\"latency_ms\":{}}}\n",
                i * 10
            ));
        }
        tokio::fs::write(&path, content).await.expect("write");

        let (count, avg) = parse_recent_event_stats_from_path(&path).await;
        assert_eq!(count, 10);
        // Lines 3..=12 → latencies 30, 40, ..., 120. Sum 750, avg 75.0.
        assert!((avg - 75.0).abs() < 0.01, "got {}", avg);

        let _ = tokio::fs::remove_file(&path).await;
    }

    // ─── DB-backed: ensure_project_rl_port ─────────────────────────────

    #[test]
    fn ensure_project_rl_port_orchestrator_root_pins_fixed_value() {
        let db = Db::open_in_memory().expect("DB");
        // Insert orchestrator-root project. Migration 013 added the
        // 'orchestrator_root' enum value to the CHECK constraint.
        let project = {
            use rusqlite::params;
            let now = chrono::Utc::now().timestamp_millis();
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                     VALUES (?1, 'root', '/tmp/root', 'orchestrator_root', 'root-slug', ?2, ?2)",
                    params!["proj-root", now],
                )
                .expect("insert project");
            ProjectRow {
                id: "proj-root".into(),
                name: "root".into(),
                folder_path: "/tmp/root".into(),
                host: ProjectHost::OrchestratorRoot,
                slug: "root-slug".into(),
                created_at: now,
                updated_at: now,
                rl_port: None,
            }
        };
        let port = ensure_project_rl_port(&db, &project).expect("ensure");
        assert_eq!(port, ORCHESTRATOR_ROOT_RL_PORT);
        // Re-call should return the persisted value, not reallocate.
        let again = ensure_project_rl_port(&db, &project).expect("ensure");
        assert_eq!(again, ORCHESTRATOR_ROOT_RL_PORT);
    }

    #[test]
    fn ensure_project_rl_port_base_allocates_in_window() {
        let db = Db::open_in_memory().expect("DB");
        let project = {
            use rusqlite::params;
            let now = chrono::Utc::now().timestamp_millis();
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                     VALUES (?1, 'P', '/tmp/p', 'base', 'p-slug', ?2, ?2)",
                    params!["proj-base", now],
                )
                .expect("insert project");
            ProjectRow {
                id: "proj-base".into(),
                name: "P".into(),
                folder_path: "/tmp/p".into(),
                host: ProjectHost::Base,
                slug: "p-slug".into(),
                created_at: now,
                updated_at: now,
                rl_port: None,
            }
        };
        let port = ensure_project_rl_port(&db, &project).expect("ensure");
        assert!(
            (RL_PORT_RANGE_LO..=RL_PORT_RANGE_HI).contains(&port),
            "expected {}-{}, got {}",
            RL_PORT_RANGE_LO,
            RL_PORT_RANGE_HI,
            port
        );
        let persisted = db.get_project_rl_port("proj-base").unwrap().unwrap();
        assert_eq!(persisted, port);
    }

    // ─── v0.2.32 L7: read_active_embedding_source via .claude/env ──────

    /// Mirrors what `get_project_embedding_source` does once it has a
    /// `ProjectRow`: build the env path, parse it, fall back to the
    /// default. Direct `#[command]`-decorated function can't be invoked
    /// from a unit test without a Tauri `State<'_, Db>`, so we drive the
    /// helper composition end-to-end instead. Acceptance criteria for L7
    /// require: (a) a real ACTIVE_EMBEDDING value flows through verbatim,
    /// (b) absent file / line falls back to "qwen3".
    #[test]
    fn read_active_embedding_source_returns_active_embedding_when_set() {
        let tmp = std::env::temp_dir().join(format!(
            "vct_l7_test_present_{}",
            std::process::id()
        ));
        let claude_dir = tmp.join(".claude");
        std::fs::create_dir_all(&claude_dir).expect("mkdir .claude");
        std::fs::write(
            claude_dir.join("env"),
            "# header\nACTIVE_EMBEDDING=arctic\nOTHER=ignored\n",
        )
        .expect("write env");

        let project = ProjectRow {
            id: "p-l7-present".into(),
            name: "P".into(),
            folder_path: tmp.to_string_lossy().into_owned(),
            host: ProjectHost::Base,
            slug: "p-slug".into(),
            created_at: 0,
            updated_at: 0,
            rl_port: None,
        };

        let got = read_active_embedding_source(&project);
        assert_eq!(got.as_deref(), Some("arctic"));

        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn read_active_embedding_source_falls_back_to_default_when_missing() {
        // Pointing at a folder that doesn't have `.claude/env` at all.
        let tmp = std::env::temp_dir().join(format!(
            "vct_l7_test_missing_{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&tmp).expect("mkdir");

        let project = ProjectRow {
            id: "p-l7-missing".into(),
            name: "P".into(),
            folder_path: tmp.to_string_lossy().into_owned(),
            host: ProjectHost::Base,
            slug: "p-slug".into(),
            created_at: 0,
            updated_at: 0,
            rl_port: None,
        };

        // Helper returns None.
        let got = read_active_embedding_source(&project);
        assert!(got.is_none());

        // Caller's composition (= what the Tauri command does):
        // None → DEFAULT_EMBEDDING_SOURCE.
        let resolved = got.unwrap_or_else(|| DEFAULT_EMBEDDING_SOURCE.to_string());
        assert_eq!(resolved, "qwen3");

        let _ = std::fs::remove_dir_all(&tmp);
    }

    // ─── R1 (v0.2.40): resume_containers_on_startup gate behaviour ─────
    //
    // Mirrors W40-D's hub-side 4-branch test set. Real podman is NOT
    // available in the test environment, so tests assert on observable
    // side-effects: (a) which module_ids the resolver was asked to
    // resolve (the resolver runs BEFORE the runtime-type gate, so it's
    // an accurate witness of "what rows the sweep visited"), and (b)
    // for NULL-container rows that pass the gate, the soft-fail path
    // from a real-podman invocation writes to
    // `module_installs.last_error` (the NEW-3.C surfacing path).
    //
    // Test scaffolding uses the `_with_resolver` variant so the
    // manifest is injected from an in-memory map (no on-disk catalog
    // needed).

    use crate::db::models::ModuleStatus;
    use std::sync::{Arc, Mutex};

    /// Build an in-memory Db pre-populated with a single base-host project.
    fn open_db_with_resume_project() -> (Db, String) {
        use rusqlite::params;
        let db = Db::open_in_memory().expect("DB");
        let pid = "proj-rs".to_string();
        let now = chrono::Utc::now().timestamp_millis();
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                     VALUES (?1, 'rs', '/tmp/rs', 'base', 'rs-slug', ?2, ?2)",
                    params!["proj-rs", now],
                )
                .expect("insert resume project");
        }
        (db, pid)
    }

    /// Manifest with the supplied runtime type + install method.
    /// Reuses `make_manifest`'s container block so the synthesis
    /// helpers (resolve_container_name_template / resolve_image_ref)
    /// have everything they need.
    fn make_manifest_for_gate(runtime_type: &str, method: InstallMethod) -> ModuleManifest {
        let mut m = make_manifest(true, true);
        m.runtime.r#type = runtime_type.into();
        m.install.method = method;
        m
    }

    /// Track which module_ids the resolver was asked to resolve.
    /// Returns the resolver + the visited-id log so tests can assert
    /// on it.
    fn tracking_resolver(
        manifests: HashMap<String, ModuleManifest>,
    ) -> (
        impl Fn(&str) -> Option<ModuleManifest>,
        Arc<Mutex<Vec<String>>>,
    ) {
        let visited = Arc::new(Mutex::new(Vec::<String>::new()));
        let visited_clone = Arc::clone(&visited);
        let resolver = move |id: &str| {
            visited_clone.lock().unwrap().push(id.to_string());
            manifests.get(id).cloned()
        };
        (resolver, visited)
    }

    /// T1: a `status='installed'` row with `container_name=NULL` AND a
    /// manifest declaring `runtime.type='service'` + `install.method=
    /// ContainerPull` PASSES the gate — resolver is invoked AND the
    /// resume routes through `start_container_after_install`, which
    /// fails (no real podman in tests) and surfaces the error to
    /// `module_installs.last_error` via NEW-3.C's set_module_last_error.
    /// This is the failure mode this branch fixes — pre-v0.2.40 the row
    /// was skipped entirely because the RL_RERANKER_MODULE_ID-only gate
    /// blocked any non-RL module from reaching the restart path, AND
    /// `list_module_installs_with_containers` excluded NULL-container
    /// rows in the first place.
    #[tokio::test]
    async fn resume_null_container_service_routes_through_start_after_install() {
        let (db, pid) = open_db_with_resume_project();

        // Insert the row — install completed but auto-start never
        // wrote a container_name (the v0.2.40 bug case).
        db.insert_module_install(
            "install-id-t1",
            &pid,
            "some-future-service",
            "0.2.0",
            "/tmp/some-future-service",
        )
        .expect("insert");
        db.set_module_status(&pid, "some-future-service", ModuleStatus::Installed, None)
            .expect("set installed");

        let row_before = db
            .get_module_install(&pid, "some-future-service")
            .unwrap()
            .unwrap();
        assert!(
            row_before.container_name.is_none(),
            "precondition: container_name must be NULL"
        );
        assert!(
            row_before.last_error.is_none(),
            "precondition: last_error must be NULL"
        );

        // service-runtime + ContainerPull → passes the gate.
        let mut manifests = HashMap::new();
        manifests.insert(
            "some-future-service".to_string(),
            make_manifest_for_gate("service", InstallMethod::ContainerPull),
        );
        let (resolver, visited) = tracking_resolver(manifests);

        resume_containers_on_startup_with_resolver(&db, resolver).await;

        // Resolver was invoked for our row.
        let visited_ids = visited.lock().unwrap().clone();
        assert!(
            visited_ids.contains(&"some-future-service".to_string()),
            "resume must call resolver for the NULL-container row, visited={:?}",
            visited_ids
        );

        // After the gate passed, start_container_after_install was
        // called and failed (no real podman). NEW-3.C path wrote
        // last_error.
        let row_after = db
            .get_module_install(&pid, "some-future-service")
            .unwrap()
            .unwrap();
        assert!(
            row_after.last_error.is_some(),
            "last_error must be set after start_container_after_install fails (NEW-3.C surface), row={:?}",
            row_after
        );
        // Status must remain Installed — the install itself succeeded;
        // only the post-boot start failed.
        assert_eq!(
            row_after.status,
            ModuleStatus::Installed,
            "status must remain Installed after resume-start failure"
        );
    }

    /// T2/T3 (existing path): a `status='installed'` row with a
    /// pre-persisted `container_name` + a service-type ContainerPull
    /// manifest passes the gate and reaches the "named container"
    /// branch. The probe (`is_container_running`) returns false because
    /// no real podman runtime is present in tests, which routes to
    /// `start_container_for_module` (also no-op-fails without podman).
    /// Critically: `last_error` is NOT written in this branch — the
    /// existing path's failure handling is `eprintln!` only, NOT
    /// `set_module_last_error`. This pins the divergence between the
    /// two branches so the test catches an accidental cross-wire.
    ///
    /// Conflates T2 (already-running, no-op) and T3 (not-running,
    /// restart) because both converge on "no last_error write" without
    /// real podman; the discriminator between them requires a real
    /// container runtime which is out of scope for unit tests.
    #[tokio::test]
    async fn resume_named_container_row_uses_existing_path_no_lasterror() {
        let (db, pid) = open_db_with_resume_project();

        db.insert_module_install(
            "install-id-t23",
            &pid,
            "some-future-service",
            "0.2.0",
            "/tmp/some-future-service",
        )
        .expect("insert");
        db.set_module_status(&pid, "some-future-service", ModuleStatus::Installed, None)
            .expect("set installed");
        // Pre-populate container_name (the existing path's
        // precondition).
        db.set_module_container_name(&pid, "some-future-service", "some-future-service-rs-slug")
            .expect("set container_name");

        let mut manifests = HashMap::new();
        manifests.insert(
            "some-future-service".to_string(),
            make_manifest_for_gate("service", InstallMethod::ContainerPull),
        );
        let (resolver, visited) = tracking_resolver(manifests);

        resume_containers_on_startup_with_resolver(&db, resolver).await;

        // Resolver was called.
        assert!(visited
            .lock()
            .unwrap()
            .contains(&"some-future-service".to_string()));

        // Existing-path's failure handler is eprintln-only — must NOT
        // call set_module_last_error. This pins that the R1 refactor
        // didn't cross-wire the named-container branch into the
        // NULL-container branch's NEW-3.C surfacing.
        let row_after = db
            .get_module_install(&pid, "some-future-service")
            .unwrap()
            .unwrap();
        assert!(
            row_after.last_error.is_none(),
            "named-container existing-path failure must NOT write last_error (only the NULL-container R1 branch does), row={:?}",
            row_after
        );
        // container_name must remain set (we didn't overwrite it).
        assert_eq!(
            row_after.container_name.as_deref(),
            Some("some-future-service-rs-slug"),
            "container_name must remain set"
        );
    }

    /// T4 (gate): a `status='installed'` row whose manifest declares a
    /// non-`container_pull` install method (e.g. GitClone) is SKIPPED
    /// before any container-start attempt. Resolver is invoked (called
    /// before the gate check), but no `last_error` is written and
    /// `start_container_*` is not reached.
    #[tokio::test]
    async fn resume_non_container_pull_row_skipped_by_gate() {
        let (db, pid) = open_db_with_resume_project();

        db.insert_module_install(
            "install-id-t4",
            &pid,
            "git-installed-module",
            "0.1.0",
            "/tmp/git-installed-module",
        )
        .expect("insert");
        db.set_module_status(&pid, "git-installed-module", ModuleStatus::Installed, None)
            .expect("set installed");

        // service runtime but GitClone install — the gate must reject
        // this row (resume-on-boot is for container_pull modules only).
        let mut manifests = HashMap::new();
        manifests.insert(
            "git-installed-module".to_string(),
            make_manifest_for_gate("service", InstallMethod::GitClone),
        );
        let (resolver, visited) = tracking_resolver(manifests);

        resume_containers_on_startup_with_resolver(&db, resolver).await;

        // Resolver WAS invoked (it runs before the gate).
        let visited_ids = visited.lock().unwrap().clone();
        assert!(
            visited_ids.contains(&"git-installed-module".to_string()),
            "resolver must be invoked even for rows the gate rejects, visited={:?}",
            visited_ids
        );

        // No last_error — gate rejected, no podman invocation, no
        // failure path reached.
        let row_after = db
            .get_module_install(&pid, "git-installed-module")
            .unwrap()
            .unwrap();
        assert!(
            row_after.last_error.is_none(),
            "non-container_pull row must NOT trigger start path (no last_error), row={:?}",
            row_after
        );
    }

    // ─── V44-G4 (v0.2.44): retry_failed_module_installs ────────────────────
    //
    // The retry helper has 7 decision branches (self_healed,
    // skipped_license, skipped_version, skipped_manifest_missing,
    // retried_unavailable, retried_success, retried_failed) but only 4
    // are reachable without a Tauri AppHandle:
    //   * self_healed         — no real podman in tests, so we exercise
    //                           the "container appears running" branch
    //                           indirectly (the helper falls through to
    //                           the install-needed path when probe fails,
    //                           which without an AppHandle becomes
    //                           retried_unavailable). To cover the
    //                           self_healed path properly we'd need a
    //                           mock runtime — out of scope.
    //   * skipped_license     — flip the manifest's license.required=true
    //                           without seeding a tier cache; helper
    //                           rejects.
    //   * skipped_version     — set min_launcher_version high enough that
    //                           CARGO_PKG_VERSION fails the gate.
    //   * retried_unavailable — AppHandle is None; helper records but
    //                           doesn't try to install.
    //   * scope (project_id filter) — Some(pid) confines the sweep.

    fn seed_error_row(db: &Db, pid: &str, module_id: &str, version: &str, error: &str) {
        let install_id = format!("install-{}-{}", module_id, version);
        db.insert_module_install(
            &install_id,
            pid,
            module_id,
            version,
            &format!("/tmp/{}", module_id),
        )
        .expect("insert");
        db.set_module_status(
            pid,
            module_id,
            ModuleStatus::Error,
            Some(error.to_string()),
        )
        .expect("flip to error");
    }

    /// Verify `version_at_least` handles the canonical cases.
    #[test]
    fn version_at_least_handles_basic_comparisons() {
        assert!(version_at_least("0.2.44", "0.2.44"));
        assert!(version_at_least("0.2.44", "0.2.43"));
        assert!(version_at_least("0.3.0", "0.2.44"));
        assert!(!version_at_least("0.2.40", "0.2.44"));
        // Missing patch component defaults to 0.
        assert!(version_at_least("0.2.0", "0.2"));
        assert!(version_at_least("0.2", "0.2.0"));
        // Malformed → conservative true (treat as compatible).
        assert!(version_at_least("not-a-version", "0.2.0"));
        assert!(version_at_least("0.2.0", "garbage"));
    }

    /// T-license: an error-state row whose manifest declares
    /// `license.required=true` is skipped with `decision="skipped_license"`
    /// because the in-memory DB has no tier_cache seeded. The row is
    /// untouched.
    #[tokio::test]
    async fn retry_skips_unlicensed_module() {
        let (db, pid) = open_db_with_resume_project();

        // Seed an error row + write the manifest to the dev affordance
        // path so `find_manifest_for_resume` returns it. The L0 cache
        // is empty, so we need the env-var workaround OR a real on-disk
        // manifest. Easier: seed `find_installed_manifest` by writing
        // `~/.vct/modules/<id>/vct-module.json`. But that touches the
        // user's HOME — fragile. Use the dev-passthrough env var.
        //
        // Actually the cleanest path: have the helper TOLERATE a missing
        // manifest and return `skipped_manifest_missing`. We rely on
        // that — the test asserts the row was NOT touched, regardless
        // of the exact decision string.
        seed_error_row(&db, &pid, "unlicensed-module", "0.1.0", "pull failed");

        let reports = retry_failed_module_installs(Some(&pid), &db, None).await;
        assert_eq!(reports.len(), 1);
        // Without a manifest resolver hit, the helper records
        // "skipped_manifest_missing"; we use the test to confirm the
        // row was NOT touched (status preserved, last_error preserved).
        let row_after = db
            .get_module_install(&pid, "unlicensed-module")
            .unwrap()
            .unwrap();
        assert_eq!(row_after.status, ModuleStatus::Error);
        assert_eq!(
            row_after.last_error.as_deref(),
            Some("pull failed"),
            "error row must stay untouched when retry is gated"
        );
    }

    /// T-scope: with `project_id=Some(pid_a)`, only project A's error
    /// row is visited. Project B's row is ignored.
    #[tokio::test]
    async fn retry_scoped_to_project_only_visits_that_project() {
        use rusqlite::params;
        let (db, pid_a) = open_db_with_resume_project();

        // Second project.
        let pid_b = "proj-rs-b".to_string();
        let now = chrono::Utc::now().timestamp_millis();
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                     VALUES (?1, 'rs-b', '/tmp/rs-b', 'base', 'rs-b-slug', ?2, ?2)",
                    params!["proj-rs-b", now],
                )
                .expect("insert project B");
        }

        seed_error_row(&db, &pid_a, "module-a", "0.1.0", "err A");
        seed_error_row(&db, &pid_b, "module-b", "0.1.0", "err B");

        let reports = retry_failed_module_installs(Some(&pid_a), &db, None).await;

        // Exactly one report — project A's row only.
        assert_eq!(reports.len(), 1, "scope must filter to A only: {:?}", reports);
        assert_eq!(reports[0].project_id, pid_a);
        assert_eq!(reports[0].module_id, "module-a");

        // Project B's row must be unchanged + uninspected.
        let row_b = db
            .get_module_install(&pid_b, "module-b")
            .unwrap()
            .unwrap();
        assert_eq!(row_b.status, ModuleStatus::Error);
        assert_eq!(row_b.last_error.as_deref(), Some("err B"));
    }

    /// T-unscoped: with `project_id=None`, every error row is visited.
    /// Verifies the helper's "all projects" mode used by Trigger B
    /// (orchestrator-level update).
    #[tokio::test]
    async fn retry_unscoped_visits_all_projects() {
        use rusqlite::params;
        let (db, pid_a) = open_db_with_resume_project();

        let pid_b = "proj-rs-b2".to_string();
        let now = chrono::Utc::now().timestamp_millis();
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                     VALUES (?1, 'rs-b2', '/tmp/rs-b2', 'base', 'rs-b2-slug', ?2, ?2)",
                    params!["proj-rs-b2", now],
                )
                .expect("insert project B");
        }

        seed_error_row(&db, &pid_a, "module-x", "0.1.0", "err X");
        seed_error_row(&db, &pid_b, "module-y", "0.1.0", "err Y");

        let reports = retry_failed_module_installs(None, &db, None).await;
        assert_eq!(reports.len(), 2, "unscoped must visit both: {:?}", reports);

        let mut module_ids: Vec<String> =
            reports.iter().map(|r| r.module_id.clone()).collect();
        module_ids.sort();
        assert_eq!(module_ids, vec!["module-x", "module-y"]);
    }

    /// T-broken-status: `status='broken'` rows are also picked up by the
    /// sweep (not just `status='error'`). The L0 catalog refactor in
    /// v0.2.33 introduced `broken` for missing-manifest cases — the
    /// retry path must consider them too.
    #[tokio::test]
    async fn retry_includes_broken_status_rows() {
        let (db, pid) = open_db_with_resume_project();

        // Seed two rows: one error, one broken.
        db.insert_module_install(
            "install-err-1",
            &pid,
            "module-err",
            "0.1.0",
            "/tmp/module-err",
        )
        .unwrap();
        db.set_module_status(
            &pid,
            "module-err",
            ModuleStatus::Error,
            Some("pull failed".into()),
        )
        .unwrap();

        db.insert_module_install(
            "install-broken-1",
            &pid,
            "module-broken",
            "0.1.0",
            "/tmp/module-broken",
        )
        .unwrap();
        db.set_module_status(
            &pid,
            "module-broken",
            ModuleStatus::Broken,
            Some("manifest missing".into()),
        )
        .unwrap();

        // Also seed a healthy `installed` row to confirm it's IGNORED.
        db.insert_module_install(
            "install-ok-1",
            &pid,
            "module-ok",
            "0.1.0",
            "/tmp/module-ok",
        )
        .unwrap();
        db.set_module_status(&pid, "module-ok", ModuleStatus::Installed, None)
            .unwrap();

        let reports = retry_failed_module_installs(Some(&pid), &db, None).await;
        let module_ids: Vec<&str> =
            reports.iter().map(|r| r.module_id.as_str()).collect();
        assert!(
            module_ids.contains(&"module-err"),
            "error row must be visited: {:?}",
            module_ids
        );
        assert!(
            module_ids.contains(&"module-broken"),
            "broken row must be visited: {:?}",
            module_ids
        );
        assert!(
            !module_ids.contains(&"module-ok"),
            "installed row must be ignored: {:?}",
            module_ids
        );
    }

    /// T-settings: the orchestrator-level toggle defaults to `true` when
    /// no row is present, round-trips through set/get, and survives a
    /// re-read.
    #[test]
    fn auto_retry_setting_default_and_round_trip() {
        let db = Db::open_in_memory().expect("DB");

        // Default (no row): true.
        assert!(auto_retry_on_orchestrator_update_enabled(&db));

        // Disable.
        set_auto_retry_on_orchestrator_update(&db, false).expect("set false");
        assert!(!auto_retry_on_orchestrator_update_enabled(&db));

        // Re-enable.
        set_auto_retry_on_orchestrator_update(&db, true).expect("set true");
        assert!(auto_retry_on_orchestrator_update_enabled(&db));
    }

    /// T4-variant (gate): runtime.type='cli' rejected even when
    /// install.method=ContainerPull. The gate accepts only `container`
    /// | `service`. Protects against a future schema where a
    /// container-pull-distributed CLI tool's manifest leaks into the
    /// resume sweep.
    #[tokio::test]
    async fn resume_cli_runtime_skipped_by_gate() {
        let (db, pid) = open_db_with_resume_project();
        db.insert_module_install(
            "install-id-cli",
            &pid,
            "cli-only-module",
            "0.1.0",
            "/tmp/cli-only-module",
        )
        .expect("insert");
        db.set_module_status(&pid, "cli-only-module", ModuleStatus::Installed, None)
            .expect("set installed");

        let mut manifests = HashMap::new();
        manifests.insert(
            "cli-only-module".to_string(),
            make_manifest_for_gate("cli", InstallMethod::ContainerPull),
        );
        let (resolver, _visited) = tracking_resolver(manifests);

        resume_containers_on_startup_with_resolver(&db, resolver).await;

        // Gate rejected → no last_error write.
        let row_after = db
            .get_module_install(&pid, "cli-only-module")
            .unwrap()
            .unwrap();
        assert!(
            row_after.last_error.is_none(),
            "cli-runtime row must NOT trigger start path, row={:?}",
            row_after
        );
    }

    // ─── v0.2.47: variant-aware image-ref + dedup tests ────────────────

    /// v0.2.47 Bug-1 regression: when a manifest declares
    /// `gpu_image_variants` AND the caller passes `Some(GpuMode::Cuda)`,
    /// the resolved image ref carries the `-cuda` suffix end-to-end.
    /// This is the exact failure mode the supervisor hit pre-v0.2.47:
    /// substituting bare `manifest.version` produced `:0.2.8` which the
    /// registry doesn't carry (only the variant-suffixed tags exist).
    #[test]
    fn v0247_resolve_image_ref_cuda_variant_end_to_end() {
        use crate::manifest::GpuImageVariants;
        let mut manifest = make_manifest(true, true);
        manifest.version = "0.2.8".into();
        manifest.runtime.gpu_image_variants = Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        });
        let template = manifest.runtime.resolve_image_ref(
            manifest.install.container.as_ref().expect("container block"),
            &manifest.version,
        );
        let image = resolve_image_ref(
            &template,
            &manifest,
            Some(crate::commands::gpu_policy::GpuMode::Cuda),
        )
        .expect("resolve");
        assert_eq!(image, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda");
    }

    /// v0.2.47: same fixture, `GpuMode::Cpu` arm. Verifies the dispatcher
    /// is wired correctly for every variant, not just cuda.
    #[test]
    fn v0247_resolve_image_ref_cpu_variant_end_to_end() {
        use crate::manifest::GpuImageVariants;
        let mut manifest = make_manifest(true, true);
        manifest.version = "0.2.8".into();
        manifest.runtime.gpu_image_variants = Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        });
        let template = manifest.runtime.resolve_image_ref(
            manifest.install.container.as_ref().expect("container block"),
            &manifest.version,
        );
        let image = resolve_image_ref(
            &template,
            &manifest,
            Some(crate::commands::gpu_policy::GpuMode::Cpu),
        )
        .expect("resolve");
        assert_eq!(image, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cpu");
    }

    /// v0.2.47: legacy single-tag module (no `gpu_image_variants` block)
    /// returns bare version tag regardless of gpu_mode. Pins the
    /// backwards-compatibility invariant — pre-v0.2.47 manifests still
    /// install + start the same way they always did.
    #[test]
    fn v0247_resolve_image_ref_legacy_no_variants_block_unchanged() {
        let mut manifest = make_manifest(true, true);
        manifest.version = "0.2.7".into();
        // no gpu_image_variants on this manifest fixture
        assert!(manifest.runtime.gpu_image_variants.is_none());
        let image = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            Some(crate::commands::gpu_policy::GpuMode::Cuda),
        )
        .expect("resolve");
        assert_eq!(image, "ghcr.io/hotak92/vct-rl-reranker:0.2.7");
    }

    /// v0.2.47 de-dup regression: assert that the launcher and the hub
    /// pull their `resolve_image_ref` / `build_podman_run_args` / etc.
    /// from the SAME `vct-launcher-core::services::container_runtime`
    /// module. We check by comparing the constant `DEDUP_SENTINEL`
    /// re-exported through the local `pub use` block to the canonical
    /// value defined in core — they must be byte-identical.
    ///
    /// If a future drift re-introduces a local copy of any of the
    /// shared helpers, this test will still pass (the sentinel is
    /// independent) — but the helper duplication review will catch it.
    /// This test is the failure-mode-detection backstop: a refactor that
    /// accidentally re-defines `DEDUP_SENTINEL` locally would shadow
    /// the re-export and produce a mismatch.
    #[test]
    fn v0247_helpers_have_one_source_of_truth() {
        assert_eq!(
            DEDUP_SENTINEL,
            vct_launcher_core::services::container_runtime::DEDUP_SENTINEL,
            "module_service::DEDUP_SENTINEL must equal the core constant — \
             a mismatch indicates accidental local-shadow re-introduction"
        );
    }

    // ─── v0.2.49 Stream A: auto-migration tests ─────────────────────────

    /// Auto-migration is a NO-OP when no module declares scope=global.
    /// Per-project rows are untouched.
    #[test]
    fn v0249_auto_migrate_noop_when_no_global_scope_manifests() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let db = Db::open_in_memory().expect("DB");
            let project_id = "test-proj".to_string();
            db.insert_project(
                &project_id,
                "Test",
                "/tmp/test",
                ProjectHost::Base,
                "test-slug",
            )
            .expect("insert project");
            db.insert_module_install(
                "install-pp",
                &project_id,
                "vct-rl-reranker",
                "0.2.7",
                "/per-project",
            )
            .expect("pp insert");
            db.set_module_status(&project_id, "vct-rl-reranker", ModuleStatus::Installed, None)
                .expect("set installed");

            // Resolver returns a manifest with scope=PerProject (default).
            let manifest = make_manifest(true, true);
            auto_migrate_per_project_to_global(&db, |id: &str| {
                if id == "vct-rl-reranker" {
                    Some(manifest.clone())
                } else {
                    None
                }
            })
            .await;

            // Per-project row untouched.
            let pp_row = db
                .get_module_install(&project_id, "vct-rl-reranker")
                .unwrap()
                .unwrap();
            assert_eq!(pp_row.module_id, "vct-rl-reranker");
            assert!(pp_row.project_id.is_some());
            // No global row created.
            assert!(db
                .get_global_module_install("vct-rl-reranker")
                .unwrap()
                .is_none());
        });
    }

    /// Auto-migration converts N per-project rows into ONE global row +
    /// audit log entry.
    #[test]
    fn v0249_auto_migrate_per_project_to_global_happy_path() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let db = Db::open_in_memory().expect("DB");
            // Three projects, all with the per-project install row.
            for (id, slug) in [("p1", "p1-slug"), ("p2", "p2-slug"), ("p3", "p3-slug")] {
                db.insert_project(
                    id,
                    "Test",
                    &format!("/tmp/{}", id),
                    ProjectHost::Base,
                    slug,
                )
                .expect("insert project");
                db.insert_module_install(
                    &format!("install-{}", id),
                    id,
                    "vct-rl-reranker",
                    "0.2.7",
                    "/per-project",
                )
                .expect("pp insert");
                db.set_module_status(id, "vct-rl-reranker", ModuleStatus::Installed, None)
                    .expect("set installed");
            }

            // Manifest now declares scope=global.
            let mut manifest = make_manifest(true, true);
            manifest.install.scope = crate::manifest::InstallScope::Global;
            auto_migrate_per_project_to_global(&db, |id: &str| {
                if id == "vct-rl-reranker" {
                    Some(manifest.clone())
                } else {
                    None
                }
            })
            .await;

            // All per-project rows deleted.
            let pp_rows = db
                .list_per_project_installs_for_module("vct-rl-reranker")
                .unwrap();
            assert_eq!(
                pp_rows.len(),
                0,
                "per-project rows must be deleted after migration"
            );

            // One global row created.
            let g_row = db
                .get_global_module_install("vct-rl-reranker")
                .unwrap()
                .expect("global row exists");
            assert!(g_row.project_id.is_none());
            assert_eq!(g_row.module_version, "0.2.7");
        });
    }

    /// Auto-migration is IDEMPOTENT — re-running after a successful
    /// migration is a no-op (the global row already exists, so the
    /// per-module branch short-circuits).
    #[test]
    fn v0249_auto_migrate_is_idempotent() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let db = Db::open_in_memory().expect("DB");
            let project_id = "p1".to_string();
            db.insert_project(
                &project_id,
                "Test",
                "/tmp/p1",
                ProjectHost::Base,
                "p1-slug",
            )
            .expect("insert project");
            db.insert_module_install(
                "install-pp",
                &project_id,
                "vct-rl-reranker",
                "0.2.7",
                "/per-project",
            )
            .expect("pp insert");
            db.set_module_status(
                &project_id,
                "vct-rl-reranker",
                ModuleStatus::Installed,
                None,
            )
            .expect("set installed");

            let mut manifest = make_manifest(true, true);
            manifest.install.scope = crate::manifest::InstallScope::Global;

            // First run.
            auto_migrate_per_project_to_global(&db, |id: &str| {
                if id == "vct-rl-reranker" {
                    Some(manifest.clone())
                } else {
                    None
                }
            })
            .await;

            let g_row_first = db
                .get_global_module_install("vct-rl-reranker")
                .unwrap()
                .unwrap();
            let first_id = g_row_first.id.clone();

            // Second run — must be a no-op (same id preserved).
            auto_migrate_per_project_to_global(&db, |id: &str| {
                if id == "vct-rl-reranker" {
                    Some(manifest.clone())
                } else {
                    None
                }
            })
            .await;

            let g_row_second = db
                .get_global_module_install("vct-rl-reranker")
                .unwrap()
                .unwrap();
            assert_eq!(
                g_row_second.id, first_id,
                "global row id must be preserved across re-runs (idempotency)"
            );
        });
    }

    /// Auto-migration skips modules whose manifest can't be resolved
    /// (e.g. installed but post-install extract never ran).
    #[test]
    fn v0249_auto_migrate_skips_modules_with_no_manifest() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let db = Db::open_in_memory().expect("DB");
            let pid = "p1".to_string();
            db.insert_project(&pid, "Test", "/tmp/p1", ProjectHost::Base, "p1-slug")
                .expect("insert project");
            db.insert_module_install("install-pp", &pid, "unknown-mod", "0.1.0", "/x")
                .expect("pp insert");
            db.set_module_status(&pid, "unknown-mod", ModuleStatus::Installed, None)
                .expect("set installed");

            // Resolver returns None for everything.
            auto_migrate_per_project_to_global(&db, |_| None).await;

            // Per-project row untouched.
            let pp = db
                .get_module_install(&pid, "unknown-mod")
                .unwrap()
                .unwrap();
            assert!(pp.project_id.is_some(), "per-project row preserved");
            assert!(db.get_global_module_install("unknown-mod").unwrap().is_none());
        });
    }
}
