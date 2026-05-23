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
    Done,
    // TODO: emit on install error (today errors propagate via Result and
    // the progress channel never receives a Failed stage event — UI just
    // sees the channel hang up). Wire a `report_error(stage, msg)` path
    // through `installer_engine::run` to send Failed before propagating.
    #[allow(dead_code)]
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
}
