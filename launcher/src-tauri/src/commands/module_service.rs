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

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, State};
use tokio::process::Command;

use crate::db::models::ProjectRow;
use crate::db::Db;
use crate::manifest::{ModuleManifest, PlaceholderCtx, PortMapping, VolumeMount};

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
/// license-reader closure (Step 24 commit b). Mirrors
/// `commands::licensing::machine_id_hash` — sha256 of the 8-byte
/// big-endian MAC, hex lowercase. Pulled out as a separate `pub fn` so
/// the closure in lib.rs can construct the `(license_key, hash)` pair
/// without a circular dependency on the licensing module.
pub fn machine_id_hash_for_poll() -> String {
    use sha2::{Digest, Sha256};
    let mac = mac_address::get_mac_address().ok().flatten();
    let bytes: [u8; 8] = match mac {
        Some(m) => {
            let bs = m.bytes();
            let mut out = [0u8; 8];
            out[2..].copy_from_slice(&bs);
            out
        }
        None => [0u8; 8],
    };
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

/// Default Ollama port used to resolve `{ollama_port}` in env values
/// when the manifest doesn't override it. Matches the launcher's
/// well-known service-port layout.
const DEFAULT_OLLAMA_PORT: &str = "11435";

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

// ─── Pure helpers (testable without a container runtime) ────────────────

/// Resolve `{project_slug}` (and any other launcher-wide tokens) into a
/// concrete container name. Returns an error if the resolved name still
/// contains unresolved placeholders — that's a manifest authoring bug
/// (e.g. a typo `{project-slug}`) and should surface clearly instead of
/// silently passing through to podman as a literal `{...}` string.
pub fn resolve_container_name(template: &str, project_slug: &str) -> Result<String, String> {
    let out = template.replace("{project_slug}", project_slug);
    if out.contains('{') && out.contains('}') {
        return Err(format!(
            "container_name_template '{}' has unresolved placeholders after \
             {{project_slug}} substitution → '{}'",
            template, out
        ));
    }
    Ok(out)
}

/// Resolve `{install.container.image}` + `{install.container.tag}` against
/// the manifest's `install.container` block. The tag is chosen via the
/// same rule `installer_engine::container_pull` uses (`tag_from_version`
/// → manifest.version; else `install.ref` or `"latest"`).
///
/// v0.2.20 GPU-variant note: if the manifest declares
/// `runtime.gpu_image_variants`, the install path already picked the
/// matching variant tag at pull time. We don't try to recover the
/// per-mode tag here — `tag_from_version` plus the manifest version is
/// good enough for the start path because the image we want is the one
/// already on disk after `container_pull`. If a future need arises (e.g.
/// running CPU + CUDA side by side), this helper can be extended to
/// accept a `GpuMode` argument.
pub fn resolve_image_ref(
    template: &str,
    manifest: &ModuleManifest,
) -> Result<String, String> {
    let container = manifest
        .install
        .container
        .as_ref()
        .ok_or_else(|| {
            "resolve_image_ref: install.container block missing (not a container_pull module)"
                .to_string()
        })?;

    let tag = if container.tag_from_version {
        manifest.version.clone()
    } else {
        manifest
            .install
            .r#ref
            .clone()
            .unwrap_or_else(|| "latest".to_string())
    };

    let out = template
        .replace("{install.container.image}", &container.image)
        .replace("{install.container.tag}", &tag);

    if out.contains('{') && out.contains('}') {
        return Err(format!(
            "image_ref template '{}' has unresolved placeholders after \
             install.container substitution → '{}'",
            template, out
        ));
    }
    Ok(out)
}

/// RL-specific placeholders not covered by `PlaceholderCtx::resolve`.
fn rl_placeholders(rl_port: u16, project_slug: &str) -> HashMap<String, String> {
    let mut m = HashMap::new();
    m.insert("{RL_SERVER_PORT}".to_string(), rl_port.to_string());
    m.insert("{project_slug}".to_string(), project_slug.to_string());
    m.insert("{ollama_port}".to_string(), DEFAULT_OLLAMA_PORT.to_string());
    m
}

/// Two-layer placeholder resolver:
///   1. Launcher-wide tokens (`{VCT_DATA}`, `{HOME}`, `{install_dir}`,
///      `{MODULE_ID}`, etc.) via `PlaceholderCtx::resolve`.
///   2. RL-specific tokens (`{RL_SERVER_PORT}`, `{project_slug}`,
///      `{ollama_port}`) via the per-call map.
fn resolve_value(
    raw: &str,
    ctx: &PlaceholderCtx,
    placeholders: &HashMap<String, String>,
) -> String {
    let mut out = ctx.resolve(raw);
    for (token, value) in placeholders {
        out = out.replace(token, value);
    }
    out
}

/// Build a single `-p` arg value. Format: `[bind:]<host>:<container>`.
/// Returns an error when `port.host` doesn't resolve to a valid u16
/// (numeric string after placeholder substitution).
fn build_port_arg(
    port: &PortMapping,
    placeholders: &HashMap<String, String>,
) -> Result<String, String> {
    let mut host = port.host.clone();
    for (token, value) in placeholders {
        host = host.replace(token, value);
    }
    host.parse::<u16>().map_err(|_| {
        format!(
            "port host '{}' (resolved from '{}') is not a valid u16",
            host, port.host
        )
    })?;

    let bind = port.bind.as_deref().unwrap_or("127.0.0.1");
    if bind.is_empty() {
        Ok(format!("{}:{}", host, port.container))
    } else {
        Ok(format!("{}:{}:{}", bind, host, port.container))
    }
}

/// Build a single `-v` arg value. Format: `host:container[:mode]`.
fn build_volume_arg(
    vol: &VolumeMount,
    ctx: &PlaceholderCtx,
    placeholders: &HashMap<String, String>,
) -> String {
    let host = resolve_value(&vol.host, ctx, placeholders);
    let container = resolve_value(&vol.container, ctx, placeholders);
    match vol.mode.as_deref() {
        Some(m) if !m.is_empty() => format!("{}:{}:{}", host, container, m),
        _ => format!("{}:{}", host, container),
    }
}

/// Build the full `podman run` argv (without the leading `podman`).
///
/// Layout:
///   `run -d --name <name> [--restart=unless-stopped] -p ... -v ... -e ... <image> <command> <args...>`
pub fn build_podman_run_args(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
    container_name: &str,
    image: &str,
) -> Result<Vec<String>, String> {
    let runtime = &manifest.runtime;
    if runtime.r#type != "container" {
        return Err(format!(
            "build_podman_run_args: runtime.type must be 'container', got '{}'",
            runtime.r#type
        ));
    }

    let placeholders = rl_placeholders(rl_port, &project.slug);
    let mut args: Vec<String> = Vec::new();
    args.push("run".into());
    args.push("-d".into());
    args.push("--name".into());
    args.push(container_name.to_string());

    if runtime.auto_restart {
        args.push("--restart=unless-stopped".into());
    }

    // Ports: one `-p [bind:]host:container` per entry.
    for port in &runtime.ports {
        args.push("-p".into());
        args.push(build_port_arg(port, &placeholders)?);
    }

    // Volumes: one `-v host:container[:mode]` per entry. Host paths
    // resolved against PlaceholderCtx AND RL-specific placeholders.
    for vol in &runtime.volumes {
        args.push("-v".into());
        args.push(build_volume_arg(vol, ctx, &placeholders));
    }

    // Env vars: env_fixed first (literal values still get placeholder
    // substitution in case authors used `{RL_SERVER_PORT}` etc. inside),
    // then env_derived. HashMap iteration is non-deterministic — tests
    // must assert on set membership, not exact ordering.
    for (k, v) in &runtime.env_fixed {
        let resolved = resolve_value(v, ctx, &placeholders);
        args.push("-e".into());
        args.push(format!("{}={}", k, resolved));
    }
    for (k, v) in &runtime.env_derived {
        let resolved = resolve_value(v, ctx, &placeholders);
        args.push("-e".into());
        args.push(format!("{}={}", k, resolved));
    }

    // Positional: image, then command + args (override of image CMD).
    args.push(image.to_string());

    // command + args undergo the same placeholder substitution so author
    // can use `{project_slug}` in `--log-path /data/logs/rl_events_{project_slug}.jsonl`.
    args.push(resolve_value(&runtime.command, ctx, &placeholders));
    for a in &runtime.args {
        args.push(resolve_value(a, ctx, &placeholders));
    }

    Ok(args)
}

/// Detect which container runtime to use. Mirrors
/// `installer_engine::detect_container_runtime` (kept private over there;
/// duplicating two lines beats making it pub and exporting a private API
/// across module boundaries).
async fn detect_container_runtime() -> Result<String, String> {
    for candidate in ["podman", "docker"] {
        let probe = Command::new(candidate)
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

/// Replace `[^A-Za-z0-9._-]` with `_` so a hostile string can never
/// escape its directory. Idempotent on already-safe input.
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

/// Path inside the container for a given (embedding_source, version)
/// pair. The container's bind mount lives at `/data/state/...` and
/// mirrors the host-side state dir.
fn container_weights_path(embedding_source: &str, version: &str) -> String {
    format!(
        "/data/state/rl_model_{}_{}.pt",
        sanitize_path_component(embedding_source),
        sanitize_path_component(version),
    )
}

// ─── Container lifecycle (Phase 1E) ─────────────────────────────────────

/// Internal helper: start (or restart) the container associated with
/// `manifest` for the given project, allocating an `rl_port` if not yet
/// set. Returns the resolved container name on success.
///
/// Idempotent: if a same-named container already exists (running OR
/// stopped) we `podman rm -f` it first. This makes the install flow
/// recoverable from partial failures.
pub async fn start_container_for_module(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
) -> Result<String, String> {
    let runtime = &manifest.runtime;
    if runtime.r#type != "container" {
        return Err(format!(
            "start_container_for_module called for non-container runtime '{}'",
            runtime.r#type
        ));
    }

    let name_template = runtime
        .container_name_template
        .as_deref()
        .ok_or_else(|| {
            "runtime.container_name_template missing on container module".to_string()
        })?;
    let container_name = resolve_container_name(name_template, &project.slug)?;

    let image_template = runtime
        .image_ref
        .as_deref()
        .ok_or_else(|| "runtime.image_ref missing on container module".to_string())?;
    let image = resolve_image_ref(image_template, manifest)?;

    let podman = detect_container_runtime().await?;

    // Idempotency: force-remove any prior container with the same name.
    let _ = Command::new(&podman)
        .args(["rm", "-f", &container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    // mkdir -p every volume host path so podman doesn't fail on bind
    // mounts of nonexistent directories.
    let placeholders = rl_placeholders(rl_port, &project.slug);
    for vol in &runtime.volumes {
        let host_resolved = resolve_value(&vol.host, ctx, &placeholders);
        let path = PathBuf::from(&host_resolved);
        if let Err(e) = tokio::fs::create_dir_all(&path).await {
            eprintln!(
                "[module_service] mkdir -p {} failed (will let podman surface the error): {}",
                path.display(),
                e
            );
        }
    }

    let args = build_podman_run_args(manifest, ctx, project, rl_port, &container_name, &image)?;

    let mut cmd = Command::new(&podman);
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
    Ok(container_name)
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

    let _ = Command::new(&podman)
        .args(["stop", "-t", "10", container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    let _ = Command::new(&podman)
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
    let output = Command::new(&podman)
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

/// Iterate every install row where `container_name IS NOT NULL` and
/// probe each container's running state. Restart those that aren't
/// running. Soft-fail per row. Called from `lib.rs::setup()`.
pub async fn resume_containers_on_startup(db: &Db) {
    let containers = match db.list_module_installs_with_containers() {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[module_service] resume_containers_on_startup list failed: {}", e);
            return;
        }
    };
    for (project_id, module_id, container_name) in containers {
        let running = is_container_running(&container_name).await.unwrap_or(false);
        if running {
            continue;
        }
        // Not running — try to restart via the standard path.
        if module_id != RL_RERANKER_MODULE_ID {
            // We only know how to restart the RL reranker for now; any
            // future container module would need its own resolver here.
            continue;
        }
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
                eprintln!("[module_service] resume: get_project({}): {}", project_id, e);
                continue;
            }
        };
        let manifest = match crate::commands::modules::find_manifest_for_resume(db, &module_id) {
            Some(m) => m,
            None => {
                eprintln!(
                    "[module_service] resume: manifest for {} not in catalog",
                    module_id
                );
                continue;
            }
        };
        let rl_port = match ensure_project_rl_port(db, &project) {
            Ok(p) => p,
            Err(e) => {
                eprintln!("[module_service] resume: ensure_rl_port({}): {}", project_id, e);
                continue;
            }
        };
        let ctx = PlaceholderCtx::new(&module_id);
        if let Err(e) = start_container_for_module(&manifest, &ctx, &project, rl_port).await {
            eprintln!(
                "[module_service] resume: start_container_for_module({}): {}",
                project_id, e
            );
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
}
