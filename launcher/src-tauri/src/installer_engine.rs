//! Module installation engine.
//!
//! Takes a parsed `ModuleManifest` + resolved `PlaceholderCtx` and executes
//! the install steps: git clone / tarball / pypi / npm, followed by
//! `post_install` commands in order. Each command is tokenized via shlex —
//! never passed to a shell — so manifest authors can't inject `&&`, `;`,
//! `$(..)`, etc.
//!
//! Progress is reported through Tauri events (`module://install-progress`)
//! so the React UI can show a progress bar per step.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tauri::{AppHandle, Emitter};
use tokio::process::Command;

use crate::manifest::{CommandSpec, InstallMethod, ModuleManifest, PlaceholderCtx};

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallStage {
    Clone,
    PostInstall,
    /// v0.2.31 (#20-Fix-3): emitted by `run_upgrade` when a
    /// `manifest.upgrade.migration_script` runs. Distinguishes
    /// "post-pull data migration" from generic post_install steps in
    /// the UI progress bar.
    Migrate,
    Done,
    /// v0.2.31 (#27): emitted via `report_error` before an error return.
    /// Pre-v0.2.31 errors propagated as `Result::Err` but the progress
    /// channel never received a terminal event — the UI's progress bar
    /// just saw the channel hang up. Now every error path in `run_install`
    /// / `run_upgrade` calls `report_error(...)` first so the GUI sees
    /// `stage=failed` + the error message before the Tauri command
    /// returns Err.
    Failed,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct InstallProgress {
    pub project_id: String,
    pub module_id: String,
    pub stage: InstallStage,
    pub step_index: u32,
    pub step_total: u32,
    pub percent: u8,
    pub message: String,
}

/// Perform the install for a module into `install_dir`.
///
/// Returns the resolved install directory path on success. On failure,
/// leaves the partially-installed directory in place (caller can inspect
/// or delete it) and returns the error.
///
/// `gpu_mode` is consulted ONLY when `manifest.install.method ==
/// ContainerPull` AND `manifest.runtime.gpu_image_variants` is `Some`.
/// In that case the resolved image tag is `{version}-{cpu|cuda|rocm}`
/// (or whatever the manifest declares) instead of the bare `{version}`
/// the legacy single-tag path produces. Pass `GpuMode::Cpu` when the
/// caller hasn't probed hardware yet — the dispatch will fall back to
/// the cpu variant, which is always safe to pull.
pub async fn run_install(
    app: &AppHandle,
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
) -> Result<PathBuf, String> {
    // v0.2.31 (#27): wrap the body so every Err path emits InstallStage::Failed
    // via the progress channel before propagating. The inner helper carries
    // the same signature; this wrapper exists only to attach the terminal
    // Failed event to error returns.
    match run_install_inner(app, manifest, ctx, project_id, gpu_mode, db).await {
        Ok(p) => Ok(p),
        Err(e) => {
            report_error(app, project_id, &manifest.id, InstallStage::Clone, &e);
            Err(e)
        }
    }
}

async fn run_install_inner(
    app: &AppHandle,
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
) -> Result<PathBuf, String> {
    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    let allowed_root = ctx.vct_modules.clone();

    // Security: refuse paths outside ~/.vct/modules/.
    crate::manifest::validate_install_dir(&install_dir, &allowed_root)?;

    // v0.2.29: enforce `compatibility.min_launcher_version` if the
    // manifest declares one. The field was already deserialized in
    // `manifest::Compatibility` since the dispatcher work but never
    // consulted — modules could declare a min version and the launcher
    // would happily install them on older binaries that lack the
    // required APIs. Refuse with a structured error so the GUI surfaces
    // "launcher too old: bump first" rather than a cryptic later
    // failure during post_install or first container boot.
    if let Some(required) = manifest.compatibility.min_launcher_version.as_deref() {
        let required = required.trim();
        if !required.is_empty() {
            let current = env!("CARGO_PKG_VERSION");
            if version_lt(current, required) {
                return Err(format!(
                    "module '{}' requires launcher >= {} but this launcher is {}. \
                     Update the launcher first (Settings → Updates → Update orchestrator), \
                     then retry the install.",
                    manifest.id, required, current,
                ));
            }
        }
    }

    let total_steps: u32 = 1 + manifest.install.post_install.len() as u32;
    let mut step_index: u32 = 0;

    // ─── Step 1: fetch source ────────────────────────────────────────────
    emit_progress(app, project_id, &manifest.id, InstallStage::Clone, step_index, total_steps, "Fetching source");
    step_index += 1;

    match manifest.install.method {
        InstallMethod::GitClone => {
            git_clone(
                manifest.install.source.as_deref().ok_or("install.source required for git_clone")?,
                manifest.install.r#ref.as_deref().unwrap_or("main"),
                &install_dir,
            )
            .await?;
        }
        InstallMethod::Local => {
            // Local: install_dir must already exist; skip fetch.
            if !install_dir.exists() {
                return Err(format!(
                    "install.method=local requires install_dir to exist: {}",
                    install_dir.display()
                ));
            }
        }
        InstallMethod::ContainerPull => {
            // Paid-module distribution path. Locks down piracy by:
            //   1. requiring a Pro-or-higher license tier
            //   2. requiring a short-lived pull token from the signed-URL
            //      gateway (no anonymous registry access)
            //
            // The actual `podman pull` happens here. The container itself
            // is registered with the launcher's service supervisor in
            // Phase 1E (modules.rs:install_module_for_project picks up the
            // resolved image:tag from manifest.install.container after
            // run_install completes).
            let container = manifest
                .install
                .container
                .as_ref()
                .ok_or("install.method=container_pull requires install.container block")?;

            // Base tag from manifest (version or explicit ref).
            let base_tag = if container.tag_from_version {
                manifest.version.clone()
            } else {
                manifest
                    .install
                    .r#ref
                    .clone()
                    .unwrap_or_else(|| "latest".to_string())
            };

            // v0.2.20: per-GPU-mode image variant dispatch. When the
            // manifest's `runtime.gpu_image_variants` block is present,
            // the host's `decide_gpu_mode` answer picks the variant tag
            // (e.g. "0.1.0-cuda"). Otherwise we fall back to the legacy
            // single-tag flow (just the version, no suffix).
            let tag = resolve_variant_tag(manifest, &base_tag, gpu_mode);

            container_pull(container, &tag, &manifest.id).await?;

            // For container modules, install_dir is metadata-only — a
            // marker directory we use to track installed state. Create
            // it so post_install commands (if any) have a place to land.
            tokio::fs::create_dir_all(&install_dir)
                .await
                .map_err(|e| format!("create install_dir for container module: {}", e))?;
        }
    }

    // ─── Step 2+: post_install commands ──────────────────────────────────
    let ctx_with_dir = ctx.clone().with_install_dir(install_dir.clone());
    for (i, cmd_spec) in manifest.install.post_install.iter().enumerate() {
        let message = format!("Running setup step {}/{}", i + 1, manifest.install.post_install.len());
        emit_progress(
            app,
            project_id,
            &manifest.id,
            InstallStage::PostInstall,
            step_index,
            total_steps,
            &message,
        );
        step_index += 1;
        run_post_install_command(cmd_spec, &ctx_with_dir).await?;
    }

    // ─── v0.2.31: module-shipped DB migrations ───────────────────────────
    //
    // After all post_install commands have succeeded, apply any SQL
    // migrations the module ships under `manifest.db.migrations_dir`.
    // Soft-fail: an error here does NOT cause the install to fail
    // (the on-disk artifact is already in place, the catalog rows
    // already written). Instead we log + emit a non-blocking event so
    // the GUI / install-deferred surface can show the actionable
    // diagnostic. The user can retry via the
    // `apply_module_db_migrations` Tauri command from the dashboard.
    if manifest.db.is_some() {
        emit_progress(
            app,
            project_id,
            &manifest.id,
            InstallStage::PostInstall,
            step_index,
            total_steps,
            "Applying module DB migrations",
        );
        let module_id_for_log = manifest.id.clone();
        let install_dir_for_log = install_dir.clone();
        match crate::db::module_db_migrations::apply_module_db_migrations(
            db,
            &manifest.id,
            &install_dir,
            manifest,
        ) {
            Ok(report) => {
                if !report.ok() {
                    eprintln!(
                        "[installer_engine] run_install[{}]: module DB migration(s) failed at {}: {:?}",
                        module_id_for_log,
                        install_dir_for_log.display(),
                        report.errors,
                    );
                    let _ = app.emit(
                        "module://db-migration-failed",
                        serde_json::json!({
                            "project_id": project_id,
                            "module_id": manifest.id,
                            "errors": report.errors,
                            "applied": report.applied,
                            "skipped": report.skipped,
                        }),
                    );
                } else if !report.applied.is_empty() {
                    eprintln!(
                        "[installer_engine] run_install[{}]: applied {} DB migration(s)",
                        module_id_for_log,
                        report.applied.len(),
                    );
                }
            }
            Err(e) => {
                // Hard failure resolving the apply itself (e.g. DB lock).
                // Still soft-fail at the installer-engine level: the
                // install row is `installed`, the migration is just
                // pending. Surface via the same event.
                eprintln!(
                    "[installer_engine] run_install[{}]: apply_module_db_migrations errored: {}",
                    module_id_for_log, e
                );
                let _ = app.emit(
                    "module://db-migration-failed",
                    serde_json::json!({
                        "project_id": project_id,
                        "module_id": manifest.id,
                        "error": e,
                    }),
                );
            }
        }
    }

    emit_progress(
        app,
        project_id,
        &manifest.id,
        InstallStage::Done,
        step_index,
        total_steps,
        "Install complete",
    );

    Ok(install_dir)
}

/// Pull a container image from a private registry via a short-lived
/// signed pull-token (paid-module flow).
///
/// Token gateway flow (Phase 3A — gateway not deployed yet):
///   1. POST validated-tier JWT to `pull_token_endpoint`.
///   2. Receive `{ image, tag, pull_token, expires_at }` — token TTL ~15min.
///   3. `podman login` with that token, `podman pull`, `podman logout`.
///   4. Discard token from memory.
///
/// Today (gateway returns 404): falls back to anonymous pull. Anonymous
/// pull will succeed for public images and 401 for private — both produce
/// clear errors that help diagnose the gateway-not-deployed-yet state.
///
/// Runtime detection: prefers `podman` (rootless, matches the rest of
/// VCO's container stack). Falls back to `docker` if podman is missing.
async fn container_pull(
    container: &crate::manifest::ContainerInstallBlock,
    tag: &str,
    module_id: &str,
) -> Result<(), String> {
    let image_ref = format!("{}:{}", container.image, tag);

    // ─── Step 1: try to obtain a pull token from the signed-URL gateway ─
    //
    // For v0 (Phase 1B) this is a stub: we attempt the POST but treat ANY
    // failure (network, 404, 401, body-parse) as "gateway not available,
    // fall through to anonymous pull". Once Phase 3A lands the Supabase
    // edge function, missing-token will become a hard error (registry is
    // private, anonymous pull will 401 anyway — better to fail at the
    // gateway-call site with a clear "your Pro license could not issue
    // a pull token" message).
    let token: Option<String> = match request_pull_token(container).await {
        Ok(tok) => {
            eprintln!(
                "[installer_engine] container_pull[{}]: obtained pull token (expires_in={}s)",
                module_id, tok.expires_in_s
            );
            Some(tok.pull_token)
        }
        Err(e) => {
            eprintln!(
                "[installer_engine] container_pull[{}]: pull-token gateway unavailable ({}). \
                 Falling back to anonymous pull — will succeed only if the image is public, \
                 401 if private. Phase 3A will turn this into a hard error.",
                module_id, e
            );
            None
        }
    };

    // ─── Step 2: pick container runtime (podman preferred, docker fallback) ─
    let runtime = detect_container_runtime().await?;

    // ─── Step 3: login (if token), pull, logout ─────────────────────────
    if let Some(t) = token.as_deref() {
        let registry = container
            .registry
            .clone()
            .unwrap_or_else(|| infer_registry_from_image(&container.image));
        container_login(&runtime, &registry, t).await?;
    }

    let pull_status = Command::new(&runtime)
        .args(["pull", &image_ref])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .status()
        .await
        .map_err(|e| format!("spawn {} pull: {}", runtime, e))?;

    // Always logout if we logged in (even on pull failure) so the token
    // doesn't linger in the runtime's auth.json.
    if token.is_some() {
        let registry = container
            .registry
            .clone()
            .unwrap_or_else(|| infer_registry_from_image(&container.image));
        let _ = Command::new(&runtime)
            .args(["logout", &registry])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
    }

    if !pull_status.success() {
        return Err(format!(
            "{} pull failed (exit {}): {}{}",
            runtime,
            pull_status.code().unwrap_or(-1),
            image_ref,
            if token.is_none() {
                " — likely because no pull token was obtained from the signed-URL gateway \
                 (Phase 3A) and the image is private (registry returns 401 for anonymous pulls)."
            } else {
                ""
            }
        ));
    }

    Ok(())
}

#[derive(Debug, serde::Deserialize)]
struct PullTokenResponse {
    pub pull_token: String,
    #[serde(default)]
    pub expires_in_s: u64,
}

/// POST validated-tier JWT to the manifest's `pull_token_endpoint`.
///
/// Today: stub. Returns Err if the JWT-from-license-cache helper isn't
/// available or the endpoint returns non-200. Phase 3A makes this the
/// canonical paid-module auth path; until then, container_pull falls
/// through to anonymous pull on Err.
async fn request_pull_token(
    container: &crate::manifest::ContainerInstallBlock,
) -> Result<PullTokenResponse, String> {
    // TODO[Phase 3A]: read the validated-tier JWT from ~/.vibecoded/license_cache.json
    // (populated by VCThelpers/license/validator.py after /validate-tier success).
    // For now, treat the absence of that file as "gateway unavailable" — keeps
    // dev flow working with anonymous pulls.
    let cache_path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vibecoded/license_cache.json"))
        .ok_or("cannot resolve ~/.vibecoded/license_cache.json")?;

    if !cache_path.exists() {
        return Err(format!(
            "license cache absent ({}); skipping token request",
            cache_path.display()
        ));
    }

    let body = tokio::fs::read_to_string(&cache_path)
        .await
        .map_err(|e| format!("read license cache: {}", e))?;

    // POST the cache body verbatim (the cache is opaque to us; the edge
    // function validates the JWT inside). 15s timeout — short enough to
    // not block install flow on a stuck gateway.
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let resp = client
        .request(
            container.pull_token_method.parse().unwrap_or(reqwest::Method::POST),
            &container.pull_token_endpoint,
        )
        .header("Content-Type", "application/json")
        .body(body)
        .send()
        .await
        .map_err(|e| format!("POST {}: {}", container.pull_token_endpoint, e))?;

    if !resp.status().is_success() {
        return Err(format!(
            "pull-token gateway returned {}: {}",
            resp.status(),
            container.pull_token_endpoint
        ));
    }

    let parsed: PullTokenResponse = resp
        .json()
        .await
        .map_err(|e| format!("parse pull-token response: {}", e))?;

    Ok(parsed)
}

/// Detect which container runtime to use. Prefers podman (matches the
/// rest of VCO's container stack), falls back to docker.
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

/// `<runtime> login <registry> -u <username> --password-stdin` with the
/// token piped to stdin. Stdin-feed avoids exposing the token in argv
/// (where `ps` would see it). The username is irrelevant for GHCR PATs
/// — the registry validates the token, not the username pairing.
async fn container_login(runtime: &str, registry: &str, token: &str) -> Result<(), String> {
    use tokio::io::AsyncWriteExt;

    let mut child = Command::new(runtime)
        .args(["login", registry, "-u", "vct-paid-module", "--password-stdin"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("spawn {} login: {}", runtime, e))?;

    {
        let mut stdin = child
            .stdin
            .take()
            .ok_or("login stdin not captured")?;
        stdin
            .write_all(token.as_bytes())
            .await
            .map_err(|e| format!("write token to {} login stdin: {}", runtime, e))?;
        // stdin drops here → EOF, login proceeds.
    }

    let output = child
        .wait_with_output()
        .await
        .map_err(|e| format!("wait {} login: {}", runtime, e))?;

    if !output.status.success() {
        return Err(format!(
            "{} login {} failed (exit {}): {}",
            runtime,
            registry,
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stderr).chars().take(300).collect::<String>()
        ));
    }
    Ok(())
}

fn infer_registry_from_image(image: &str) -> String {
    // "ghcr.io/hotak92/vct-rl-reranker" → "ghcr.io"
    image
        .split_once('/')
        .map(|(head, _)| head.to_string())
        .unwrap_or_else(|| "docker.io".to_string())
}

async fn git_clone(source: &str, git_ref: &str, dest: &Path) -> Result<(), String> {
    if let Some(parent) = dest.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }

    let status = Command::new("git")
        .args(["clone", "--depth", "1", "--branch", git_ref, source])
        .arg(dest)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .status()
        .await
        .map_err(|e| format!("spawn git clone: {}", e))?;

    if !status.success() {
        return Err(format!(
            "git clone failed (exit {}): {} {} -> {}",
            status.code().unwrap_or(-1),
            source,
            git_ref,
            dest.display()
        ));
    }
    Ok(())
}

async fn run_post_install_command(
    spec: &CommandSpec,
    ctx: &PlaceholderCtx,
) -> Result<(), String> {
    let raw = pick_platform_cmd(spec);
    let resolved = ctx.resolve(&raw);

    let tokens = shlex::split(&resolved)
        .ok_or_else(|| format!("cannot tokenize command: {}", resolved))?;
    let (program, args) = tokens
        .split_first()
        .ok_or_else(|| format!("empty command: {}", resolved))?;

    // Resolve cwd
    let cwd_str = spec
        .cwd
        .as_ref()
        .map(|s| ctx.resolve(s))
        .unwrap_or_else(|| {
            ctx.install_dir
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| ".".to_string())
        });
    let cwd = PathBuf::from(&cwd_str);

    let mut cmd = Command::new(program);
    cmd.args(args);
    cmd.current_dir(&cwd);
    // Scrubbed env: only pass through PATH, HOME, USER, and platform essentials.
    cmd.env_clear();
    for key in ["PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL"] {
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

    let timeout = Duration::from_secs(spec.timeout_s);
    let output = tokio::time::timeout(timeout, cmd.output())
        .await
        .map_err(|_| format!("command timed out after {}s: {}", spec.timeout_s, resolved))?
        .map_err(|e| format!("spawn {}: {}", program, e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "command failed (exit {}): {}\nstderr: {}",
            output.status.code().unwrap_or(-1),
            resolved,
            stderr.chars().take(500).collect::<String>()
        ));
    }

    Ok(())
}

fn pick_platform_cmd(spec: &CommandSpec) -> String {
    let platform = if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    };
    spec.platform_cmd
        .get(platform)
        .cloned()
        .unwrap_or_else(|| spec.cmd.clone())
}

fn emit_progress(
    app: &AppHandle,
    project_id: &str,
    module_id: &str,
    stage: InstallStage,
    step_index: u32,
    step_total: u32,
    message: &str,
) {
    let percent = if step_total == 0 {
        0
    } else {
        ((step_index as f32 / step_total as f32) * 100.0).round() as u8
    };
    let payload = InstallProgress {
        project_id: project_id.to_string(),
        module_id: module_id.to_string(),
        stage,
        step_index,
        step_total,
        percent,
        message: message.to_string(),
    };
    // Emit via Tauri's event system; swallow errors (UI-only signal).
    let _ = app.emit("module://install-progress", payload);
}

/// v0.2.20: pick the OCI image tag based on the host's GPU mode.
///
/// When the manifest declares `runtime.gpu_image_variants`, each
/// `GpuMode` maps to a tag suffix:
///
/// | GpuMode | Variant tag used |
/// |---------|------------------|
/// | Cuda    | `gpu_image_variants.cuda` |
/// | Rocm    | `gpu_image_variants.rocm` |
/// | Cpu     | `gpu_image_variants.cpu`  |
/// | Metal   | `gpu_image_variants.cpu` (no Metal torch wheels today) |
///
/// When `gpu_image_variants` is absent (legacy modules + non-container
/// runtimes), returns `base_tag` unchanged — preserves single-tag
/// behavior for modules that only ship one image.
pub(crate) fn resolve_variant_tag(
    manifest: &ModuleManifest,
    base_tag: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
) -> String {
    use crate::commands::gpu_policy::GpuMode;
    let variants = match manifest.runtime.gpu_image_variants.as_ref() {
        Some(v) => v,
        None => return base_tag.to_string(),
    };
    match gpu_mode {
        GpuMode::Cuda => variants.cuda.clone(),
        GpuMode::Rocm => variants.rocm.clone(),
        GpuMode::Cpu | GpuMode::Metal => variants.cpu.clone(),
    }
}

/// v0.2.31 (#27): emit a terminal `InstallStage::Failed` event on the
/// progress channel. Called by `run_install`'s outer wrapper and by
/// `run_upgrade` on every error return path. The UI uses this event to
/// flip the progress bar from "in flight" to "failed: <msg>" without
/// waiting for the Tauri command's Result to deserialise.
///
/// `stage` is the LAST stage that was in flight when the error occurred
/// (Clone / PostInstall / Migrate). It's advisory only — the UI keys off
/// `InstallStage::Failed` to decide what to render.
pub(crate) fn report_error(
    app: &AppHandle,
    project_id: &str,
    module_id: &str,
    _stage: InstallStage,
    msg: &str,
) {
    let payload = InstallProgress {
        project_id: project_id.to_string(),
        module_id: module_id.to_string(),
        stage: InstallStage::Failed,
        step_index: 0,
        step_total: 0,
        percent: 0,
        message: msg.to_string(),
    };
    let _ = app.emit("module://install-progress", payload);
}

/// v0.2.31 (#20-Fix-3): perform an in-place upgrade of an already-installed
/// module to the catalog's current version. Parallel to `run_install` — does
/// NOT modify it.
///
/// Behaviour:
///   * Reads `manifest.upgrade` (`Option<UpgradeBlock>`).
///   * If `Some(upgrade_block)`:
///     1. Runs `upgrade_block.pre_upgrade` commands in declaration order
///        (scrubbed env, same `run_post_install_command` mechanism as install).
///     2. Re-fetches the artifact per `manifest.install.method`:
///        - `GitClone`: `git pull` in `install_dir` if it's a git repo, else
///          re-clones from `manifest.install.source` (cleans the dir first).
///        - `ContainerPull`: `podman pull` (or `docker pull`) the resolved
///          GPU-variant tag for `manifest.version` via the same token-gateway
///          + `resolve_variant_tag` path the install uses.
///        - `Local`: no-op (user has already updated the directory by hand).
///     3. Runs `upgrade_block.post_upgrade` commands.
///     4. If `upgrade_block.migration_script` is `Some(...)`, executes it
///        as a single shell-tokenised command (same mechanism, scrubbed env).
///   * If `None`: falls back to the legacy "uninstall + reinstall" sequence
///     with a warning logged. The caller (`update_module_for_project`) is
///     responsible for any DB cleanup that needs to happen between the
///     uninstall + reinstall halves of the fallback; this function only
///     handles the install-engine side (re-fetch the artifact).
///
/// Emits `module://install-progress` events with stage values
/// `Clone` (fetch) / `PostInstall` (commands) / `Migrate` (migration script)
/// / `Done` (success) / `Failed` (any error before Done).
///
/// `gpu_mode` is consulted in the same cases as `run_install` —
/// `ContainerPull` + `manifest.runtime.gpu_image_variants.is_some()`.
pub async fn run_upgrade(
    app: &AppHandle,
    manifest: &ModuleManifest,
    previous_install: &crate::db::models::ModuleInstallRow,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
) -> Result<PathBuf, String> {
    // Outer wrapper: every Err path emits InstallStage::Failed before
    // propagating (#27 contract — same as run_install).
    match run_upgrade_inner(app, manifest, previous_install, ctx, project_id, gpu_mode, db).await {
        Ok(p) => Ok(p),
        Err(e) => {
            report_error(app, project_id, &manifest.id, InstallStage::PostInstall, &e);
            Err(e)
        }
    }
}

async fn run_upgrade_inner(
    app: &AppHandle,
    manifest: &ModuleManifest,
    previous_install: &crate::db::models::ModuleInstallRow,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
) -> Result<PathBuf, String> {
    // v0.2.29 launcher-version gate: enforce min_launcher_version on
    // updates the same way install does. A user could have installed
    // module@0.1.0 on an old launcher; the new manifest@0.2.0 bumps
    // min_launcher_version. Without this gate, the in-place upgrade
    // would proceed and then fail mid-flight on missing APIs.
    if let Some(required) = manifest.compatibility.min_launcher_version.as_deref() {
        let required = required.trim();
        if !required.is_empty() {
            let current = env!("CARGO_PKG_VERSION");
            if version_lt(current, required) {
                return Err(format!(
                    "module '{}' requires launcher >= {} but this launcher is {}. \
                     Update the launcher first (Settings → Updates → Update orchestrator), \
                     then retry the update.",
                    manifest.id, required, current,
                ));
            }
        }
    }

    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    let allowed_root = ctx.vct_modules.clone();
    crate::manifest::validate_install_dir(&install_dir, &allowed_root)?;

    let upgrade_block = match manifest.upgrade.as_ref() {
        Some(b) => b.clone(),
        None => {
            // Legacy fallback: no upgrade block declared. Emit a warning
            // log line and run a minimal "re-fetch artifact + skip
            // post_install" sequence. The caller is responsible for any
            // additional DB cleanup; here we just bring the on-disk
            // artifact up to date.
            eprintln!(
                "[installer_engine] run_upgrade[{}]: manifest declares no `upgrade` block; \
                 falling back to bare artifact re-fetch (no pre/post-upgrade hooks, no \
                 migration script). For load-bearing upgrades the module author should \
                 add an `upgrade` block with explicit pre_upgrade/post_upgrade commands.",
                manifest.id,
            );
            // Re-fetch only. Treat as a single-step progress sequence.
            emit_progress(
                app, project_id, &manifest.id, InstallStage::Clone, 0, 1,
                "Fetching updated source (legacy fallback)",
            );
            refetch_artifact(manifest, &install_dir, gpu_mode).await?;
            emit_progress(
                app, project_id, &manifest.id, InstallStage::Done, 1, 1,
                "Update complete (legacy fallback)",
            );
            // previous_install kept for symmetry with the upgrade-block path;
            // legacy fallback consults nothing on it.
            let _ = previous_install;
            return Ok(install_dir);
        }
    };

    let migrate_steps = if upgrade_block.migration_script.is_some() { 1 } else { 0 };
    let total_steps: u32 = (upgrade_block.pre_upgrade.len()
        + 1 // re-fetch artifact
        + upgrade_block.post_upgrade.len()
        + migrate_steps) as u32;
    let mut step_index: u32 = 0;

    let ctx_with_dir = ctx.clone().with_install_dir(install_dir.clone());

    // ─── pre_upgrade ─────────────────────────────────────────────────────
    for (i, spec) in upgrade_block.pre_upgrade.iter().enumerate() {
        let msg = format!(
            "Running pre-upgrade step {}/{}",
            i + 1, upgrade_block.pre_upgrade.len(),
        );
        emit_progress(
            app, project_id, &manifest.id, InstallStage::PostInstall,
            step_index, total_steps, &msg,
        );
        step_index += 1;
        run_post_install_command(spec, &ctx_with_dir).await?;
    }

    // ─── re-fetch artifact ───────────────────────────────────────────────
    emit_progress(
        app, project_id, &manifest.id, InstallStage::Clone,
        step_index, total_steps, "Fetching updated source",
    );
    step_index += 1;
    refetch_artifact(manifest, &install_dir, gpu_mode).await?;

    // ─── post_upgrade ────────────────────────────────────────────────────
    for (i, spec) in upgrade_block.post_upgrade.iter().enumerate() {
        let msg = format!(
            "Running post-upgrade step {}/{}",
            i + 1, upgrade_block.post_upgrade.len(),
        );
        emit_progress(
            app, project_id, &manifest.id, InstallStage::PostInstall,
            step_index, total_steps, &msg,
        );
        step_index += 1;
        run_post_install_command(spec, &ctx_with_dir).await?;
    }

    // ─── migration_script (optional, one-shot) ───────────────────────────
    if let Some(script_raw) = upgrade_block.migration_script.as_ref() {
        emit_progress(
            app, project_id, &manifest.id, InstallStage::Migrate,
            step_index, total_steps, "Running migration script",
        );
        step_index += 1;
        // Mirror CommandSpec's shape so the same scrubbed-env executor runs it.
        let spec = CommandSpec {
            cmd: script_raw.clone(),
            cwd: None,
            timeout_s: 600, // migrations may be slow; 10-min ceiling.
            platform_cmd: std::collections::HashMap::new(),
            note: None,
        };
        run_post_install_command(&spec, &ctx_with_dir).await?;
    }

    // ─── v0.2.31: module-shipped DB migrations (upgrade path) ────────────
    //
    // Same soft-fail discipline as `run_install`. A new module version
    // may ship additional SQL files; the apply mechanism is idempotent
    // (SHA-keyed), so already-applied files are skipped silently.
    // Errors are surfaced via the same `module://db-migration-failed`
    // event the dashboard listens for.
    if manifest.db.is_some() {
        emit_progress(
            app, project_id, &manifest.id, InstallStage::Migrate,
            step_index, total_steps, "Applying module DB migrations",
        );
        let module_id_for_log = manifest.id.clone();
        match crate::db::module_db_migrations::apply_module_db_migrations(
            db,
            &manifest.id,
            &install_dir,
            manifest,
        ) {
            Ok(report) => {
                if !report.ok() {
                    eprintln!(
                        "[installer_engine] run_upgrade[{}]: module DB migration(s) failed: {:?}",
                        module_id_for_log, report.errors,
                    );
                    let _ = app.emit(
                        "module://db-migration-failed",
                        serde_json::json!({
                            "project_id": project_id,
                            "module_id": manifest.id,
                            "errors": report.errors,
                            "applied": report.applied,
                            "skipped": report.skipped,
                            "operation": "upgrade",
                        }),
                    );
                } else if !report.applied.is_empty() {
                    eprintln!(
                        "[installer_engine] run_upgrade[{}]: applied {} DB migration(s)",
                        module_id_for_log, report.applied.len(),
                    );
                }
            }
            Err(e) => {
                eprintln!(
                    "[installer_engine] run_upgrade[{}]: apply_module_db_migrations errored: {}",
                    module_id_for_log, e
                );
                let _ = app.emit(
                    "module://db-migration-failed",
                    serde_json::json!({
                        "project_id": project_id,
                        "module_id": manifest.id,
                        "error": e,
                        "operation": "upgrade",
                    }),
                );
            }
        }
    }

    emit_progress(
        app, project_id, &manifest.id, InstallStage::Done,
        step_index, total_steps, "Update complete",
    );

    // previous_install is currently advisory (we report it back via the
    // command's return value to the DB layer); future work may diff
    // version strings here to short-circuit no-op upgrades.
    let _ = previous_install;

    Ok(install_dir)
}

/// v0.2.31 (#20-Fix-3): re-fetch the module's artifact for an in-place
/// upgrade. Mirrors the fetch step of `run_install_inner` but is
/// idempotent across "directory already exists" cases:
///   - `GitClone`: `git pull` if `install_dir/.git` exists, else
///     wipe + re-clone (handles user-corrupted dirs gracefully).
///   - `ContainerPull`: `podman pull` the resolved variant tag.
///   - `Local`: no-op (user has already updated by hand).
async fn refetch_artifact(
    manifest: &ModuleManifest,
    install_dir: &Path,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
) -> Result<(), String> {
    match manifest.install.method {
        InstallMethod::GitClone => {
            let git_dir = install_dir.join(".git");
            if git_dir.exists() {
                // Existing checkout — git pull preserves any user-edited
                // gitignored files (e.g. local config under the install
                // dir).
                let status = Command::new("git")
                    .args(["-C"])
                    .arg(install_dir)
                    .args(["pull", "--ff-only"])
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .status()
                    .await
                    .map_err(|e| format!("spawn git pull: {}", e))?;
                if !status.success() {
                    return Err(format!(
                        "git pull failed (exit {}) in {}: \
                         install_dir may have diverged from upstream; \
                         re-install the module to recover.",
                        status.code().unwrap_or(-1),
                        install_dir.display(),
                    ));
                }
            } else {
                // No .git dir — fall back to a fresh clone. Remove the
                // existing dir first so `git clone` doesn't refuse.
                if install_dir.exists() {
                    tokio::fs::remove_dir_all(install_dir).await.map_err(|e| {
                        format!(
                            "remove pre-existing install_dir {} before re-clone: {}",
                            install_dir.display(),
                            e,
                        )
                    })?;
                }
                git_clone(
                    manifest.install.source.as_deref().ok_or(
                        "install.source required for git_clone upgrade fallback",
                    )?,
                    manifest.install.r#ref.as_deref().unwrap_or("main"),
                    install_dir,
                )
                .await?;
            }
        }
        InstallMethod::Local => {
            // No-op: user updated the directory by hand. Sanity-check
            // existence so we surface a clear error if the dir vanished.
            if !install_dir.exists() {
                return Err(format!(
                    "install.method=local but install_dir is missing: {}",
                    install_dir.display(),
                ));
            }
        }
        InstallMethod::ContainerPull => {
            let container = manifest.install.container.as_ref().ok_or(
                "install.method=container_pull requires install.container block",
            )?;
            let base_tag = if container.tag_from_version {
                manifest.version.clone()
            } else {
                manifest
                    .install
                    .r#ref
                    .clone()
                    .unwrap_or_else(|| "latest".to_string())
            };
            let tag = resolve_variant_tag(manifest, &base_tag, gpu_mode);
            container_pull(container, &tag, &manifest.id).await?;
        }
    }
    Ok(())
}

/// v0.2.29: tiny semver comparison used by the `min_launcher_version`
/// gate. Returns true iff `a < b` lexicographically over numeric
/// components. Non-numeric prefixes of each dot-separated component are
/// parsed as 0 (so `0.2.28-dev` compares as `0.2.28`). Pre-release
/// suffixes and build metadata are ignored — same approximation used by
/// `commands::installer::version_is_outdated`. The check is "is this
/// launcher OLDER than the required version?" — if true, the install
/// is refused.
fn version_lt(a: &str, b: &str) -> bool {
    fn parse(v: &str) -> Vec<u64> {
        v.split('.')
            .map(|p| {
                p.chars()
                    .take_while(|c| c.is_ascii_digit())
                    .collect::<String>()
            })
            .map(|s| s.parse::<u64>().unwrap_or(0))
            .collect()
    }
    let ai = parse(a);
    let bi = parse(b);
    let len = ai.len().max(bi.len());
    for idx in 0..len {
        let aa = *ai.get(idx).unwrap_or(&0);
        let bb = *bi.get(idx).unwrap_or(&0);
        if aa < bb {
            return true;
        }
        if aa > bb {
            return false;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::gpu_policy::GpuMode;
    use crate::manifest::GpuImageVariants;

    fn manifest_with_variants(variants: Option<GpuImageVariants>) -> ModuleManifest {
        let mut m: ModuleManifest = serde_json::from_str(
            r#"{
              "schema_version": "1",
              "id": "vct-test",
              "name": "Test",
              "version": "0.1.0",
              "description": "Test module",
              "category": "paid-independent",
              "license": { "required": true, "min_orchestrator_tier": "pro" },
              "compatibility": { "hosts": ["base"] },
              "install": { "method": "container_pull",
                "container": {
                  "image": "ghcr.io/test/img",
                  "tag_from_version": true,
                  "pull_token_endpoint": "https://example.test/token"
                } },
              "runtime": { "type": "container", "command": "python" }
            }"#,
        )
        .expect("test fixture parses");
        m.runtime.gpu_image_variants = variants;
        m
    }

    #[test]
    fn variant_tag_picks_cuda_when_mode_is_cuda() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cuda), "0.1.0-cuda");
    }

    #[test]
    fn variant_tag_picks_rocm_when_mode_is_rocm() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Rocm), "0.1.0-rocm");
    }

    #[test]
    fn variant_tag_picks_cpu_when_mode_is_cpu() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cpu), "0.1.0-cpu");
    }

    /// Metal falls through to CPU — no Metal-specific torch wheels today
    /// (apple-silicon owners get CPU inference; the torch CPU wheel
    /// actually uses Accelerate framework under the hood).
    #[test]
    fn variant_tag_picks_cpu_when_mode_is_metal() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Metal), "0.1.0-cpu");
    }

    /// Manifests without gpu_image_variants — legacy single-tag flow.
    #[test]
    fn variant_tag_falls_through_when_no_variants_declared() {
        let m = manifest_with_variants(None);
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cuda), "0.1.0");
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cpu), "0.1.0");
    }

    // ─── v0.2.29: version_lt + min_launcher_version gate ────────────

    #[test]
    fn version_lt_basic() {
        assert!(version_lt("0.2.28", "0.2.29"));
        assert!(version_lt("0.2.0", "0.2.1"));
        assert!(version_lt("0.1.0", "0.2.0"));
        assert!(version_lt("0.0.1", "1.0.0"));
    }

    #[test]
    fn version_lt_equal_is_not_less() {
        assert!(!version_lt("0.2.29", "0.2.29"));
        assert!(!version_lt("1.0.0", "1.0.0"));
    }

    #[test]
    fn version_lt_greater_is_not_less() {
        assert!(!version_lt("0.2.30", "0.2.29"));
        assert!(!version_lt("1.0.0", "0.2.29"));
    }

    #[test]
    fn version_lt_handles_unequal_segment_counts() {
        // "0.2" vs "0.2.0" → equal (missing trailing segments parse as 0).
        assert!(!version_lt("0.2", "0.2.0"));
        assert!(!version_lt("0.2.0", "0.2"));
        // "0.2" < "0.2.1" (missing trailing parses as 0).
        assert!(version_lt("0.2", "0.2.1"));
    }

    #[test]
    fn version_lt_tolerates_suffixes() {
        // `-dev`, `-rc1`, etc. — the parser stops at the first non-digit
        // so suffixes are effectively ignored.
        assert!(version_lt("0.2.28-dev", "0.2.29"));
        assert!(!version_lt("0.2.29-rc1", "0.2.29"));
    }

    // ─── v0.2.31 #20-Fix-3: refetch_artifact (Local no-op) ──────────────
    //
    // The full `run_upgrade` path needs an AppHandle (for emit_progress)
    // which isn't easily mockable in unit tests. We test the helper
    // `refetch_artifact` directly for the Local install method — it
    // should be a clean no-op when install_dir exists, and Err when it
    // doesn't. The GitClone + ContainerPull paths touch network / git /
    // podman and are out of scope for unit tests (they're covered by
    // integration tests run against a real catalog at release time).

    fn manifest_for_local_install(install_dir: &str) -> ModuleManifest {
        // Note: `install.method=local` with an empty `install.install_dir`
        // would default to `{VCT_MODULES}/{MODULE_ID}` which doesn't exist
        // in the test sandbox. We override `install_dir` to a tempdir path.
        let raw = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-test-local-refetch",
            "name": "Local Refetch Test",
            "version": "0.2.0",
            "description": "Test fixture for refetch_artifact Local no-op.",
            "category": "community",
            "license": {"required": false, "min_orchestrator_tier": "free"},
            "compatibility": {"hosts": ["base"]},
            "install": {
                "method": "local",
                "install_dir": install_dir,
            },
            "runtime": {"type": "service", "command": "echo", "args": []}
        });
        ModuleManifest::from_json(&raw.to_string())
            .unwrap_or_else(|e| panic!("parse fixture: {}", e))
    }

    #[test]
    fn refetch_artifact_local_method_is_noop_when_dir_exists() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let tmp = tempfile::tempdir().expect("create tempdir");
            let install_dir = tmp.path().join("local-install");
            tokio::fs::create_dir_all(&install_dir).await.unwrap();
            // Drop a sentinel file so we can verify nothing was wiped.
            let sentinel = install_dir.join("sentinel.txt");
            tokio::fs::write(&sentinel, b"keep").await.unwrap();

            let manifest =
                manifest_for_local_install(&install_dir.display().to_string());
            let res = refetch_artifact(&manifest, &install_dir, GpuMode::Cpu).await;
            assert!(res.is_ok(), "local refetch must succeed when dir exists: {:?}", res);
            assert!(sentinel.exists(), "Local refetch must not touch user files");
            let body = tokio::fs::read(&sentinel).await.unwrap();
            assert_eq!(&body, b"keep", "Local refetch must not modify files");
        });
    }

    #[test]
    fn refetch_artifact_local_method_errs_when_dir_missing() {
        // If a user's `install.method=local` install_dir vanished between
        // install + update, surface a clear error rather than silently
        // succeeding. run_upgrade propagates this as a Failed event.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let tmp = tempfile::tempdir().expect("create tempdir");
            let install_dir = tmp.path().join("never-existed");
            let manifest =
                manifest_for_local_install(&install_dir.display().to_string());
            let res = refetch_artifact(&manifest, &install_dir, GpuMode::Cpu).await;
            assert!(res.is_err(), "missing install_dir must Err");
            let msg = res.unwrap_err();
            assert!(
                msg.contains("install_dir is missing")
                    || msg.contains("missing"),
                "error must mention missing dir; got: {}",
                msg
            );
        });
    }

    // ─── v0.2.31 #27: report_error wiring ───────────────────────────────
    //
    // `report_error` constructs the InstallProgress payload that the UI
    // consumes. We can't easily verify the Tauri event emission without
    // an AppHandle (which can't be constructed in unit tests), but we CAN
    // verify the payload shape via direct construction — the same shape
    // report_error builds and emits. This pins the wire contract: stage
    // == Failed, percent == 0, message carries the error verbatim.

    #[test]
    fn install_progress_payload_serializes_failed_stage_correctly() {
        // Pin the JSON wire contract for the Failed stage so a future
        // refactor of InstallStage doesn't accidentally rename the
        // serde value the GUI's TypeScript types depend on.
        let payload = InstallProgress {
            project_id: "p1".to_string(),
            module_id: "m1".to_string(),
            stage: InstallStage::Failed,
            step_index: 0,
            step_total: 0,
            percent: 0,
            message: "test failure".to_string(),
        };
        let json = serde_json::to_value(&payload).expect("serialize InstallProgress");
        assert_eq!(json["stage"], "failed", "stage must serialize as snake_case 'failed'");
        assert_eq!(json["message"], "test failure");
        assert_eq!(json["percent"], 0);
        assert_eq!(json["project_id"], "p1");
        assert_eq!(json["module_id"], "m1");
    }

    #[test]
    fn install_progress_payload_serializes_migrate_stage() {
        // Same wire-contract pin for the new Migrate stage (added in
        // v0.2.31 #20-Fix-3 for upgrade migration_script execution).
        let payload = InstallProgress {
            project_id: "p1".to_string(),
            module_id: "m1".to_string(),
            stage: InstallStage::Migrate,
            step_index: 3,
            step_total: 5,
            percent: 60,
            message: "Running migration script".to_string(),
        };
        let json = serde_json::to_value(&payload).expect("serialize InstallProgress");
        assert_eq!(json["stage"], "migrate", "stage must serialize as snake_case 'migrate'");
        assert_eq!(json["step_index"], 3);
        assert_eq!(json["step_total"], 5);
    }
}
