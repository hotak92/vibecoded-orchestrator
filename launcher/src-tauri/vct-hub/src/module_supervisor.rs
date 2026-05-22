//! Per-project paid-module container supervisor.
//!
//! Phase 1E (Step 24 commit b, 2026-05-20): the supervisor logic that
//! used to live in `launcher/src-tauri/src/commands/module_service.rs` has
//! relocated here. The launcher's Tauri commands now proxy to the hub's
//! `/projects/{project_id}/modules/{module_id}/...` lifecycle endpoints
//! instead of probing podman/docker directly.
//!
//! Why the relocation: the hub is the per-user, always-on supervisor
//! process; the launcher is a sometimes-running GUI. When the launcher
//! quits, the hub keeps the per-project containers healthy via its own
//! resume-and-poll loops. Before this step the launcher itself ran the
//! resume sweep on Tauri startup, which meant a launcher crash or a
//! user-initiated quit could orphan a stopped container until the next
//! launcher restart. With supervision in the hub, container lifecycle
//! decouples from GUI lifecycle.
//!
//! What lives here (ported verbatim from module_service.rs):
//!   * Pure helpers: `resolve_container_name`, `resolve_image_ref`,
//!     `build_podman_run_args`, `parse_inspect_running_state`,
//!     `sanitize_path_component`, `container_weights_path`.
//!   * Container lifecycle: `start_container_for_module`,
//!     `start_container_after_install`, `stop_container_for_project`,
//!     `is_container_running`, `ensure_project_rl_port`.
//!   * Startup hook: `resume_containers_on_startup` — sweeps every
//!     install row with a non-null `container_name` and restarts any
//!     whose `is_container_running` probe returns false.
//!
//! What does NOT live here (intentionally still launcher-side):
//!   * The daily weights-update poller — it needs `tauri::AppHandle`
//!     to emit `module://weights-update-available` events to the GUI.
//!     Phase 3+ moves it here once a Tauri-free signalling channel
//!     lands (hub.db pending-events table + launcher poll).
//!   * The fine-tune background task — same `AppHandle::emit` reason.
//!
//! B2 / single-writer principle (v0.2.21 Step 3 decision):
//! `projects.rl_port` and `module_installs.container_name` are
//! HUB-WRITABLE / launcher-readable columns. This module is the ONLY
//! writer of both. The launcher's `commands/module_service.rs` calls these
//! helpers via HTTP proxy (see `lifecycle_api.rs::module_*` handlers).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tokio::process::Command;

use vct_launcher_core::db::models::ProjectRow;
use vct_launcher_core::db::Db;
use vct_launcher_core::manifest::{ModuleManifest, PlaceholderCtx, PortMapping, VolumeMount};

// ─── Constants ──────────────────────────────────────────────────────────

/// Module ID we recognise as RL-reranker for the resume-by-default path.
pub const RL_RERANKER_MODULE_ID: &str = "vct-rl-reranker";

/// Fixed RL port for the orchestrator-root project.
pub const ORCHESTRATOR_ROOT_RL_PORT: u16 = 11442;

/// Allocation window for non-orchestrator-root projects.
pub const RL_PORT_RANGE_LO: u16 = 11500;
pub const RL_PORT_RANGE_HI: u16 = 11900;

/// Default Ollama port used to resolve `{ollama_port}` in env values.
const DEFAULT_OLLAMA_PORT: &str = "11435";

// ─── Pure helpers (testable without a container runtime) ────────────────

/// Resolve `{project_slug}` (and any other launcher-wide tokens) into a
/// concrete container name. Returns an error if the resolved name still
/// contains unresolved placeholders.
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
/// the manifest's `install.container` block.
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

/// RL-specific placeholders.
fn rl_placeholders(rl_port: u16, project_slug: &str) -> HashMap<String, String> {
    let mut m = HashMap::new();
    m.insert("{RL_SERVER_PORT}".to_string(), rl_port.to_string());
    m.insert("{project_slug}".to_string(), project_slug.to_string());
    m.insert("{ollama_port}".to_string(), DEFAULT_OLLAMA_PORT.to_string());
    m
}

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

    for port in &runtime.ports {
        args.push("-p".into());
        args.push(build_port_arg(port, &placeholders)?);
    }

    for vol in &runtime.volumes {
        args.push("-v".into());
        args.push(build_volume_arg(vol, ctx, &placeholders));
    }

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

    args.push(image.to_string());

    args.push(resolve_value(&runtime.command, ctx, &placeholders));
    for a in &runtime.args {
        args.push(resolve_value(a, ctx, &placeholders));
    }

    Ok(args)
}

/// Detect which container runtime to use.
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

/// Replace unsafe path chars with `_`.
pub fn sanitize_path_component(s: &str) -> String {
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

/// Path inside the container for a given (embedding_source, version).
pub fn container_weights_path(embedding_source: &str, version: &str) -> String {
    format!(
        "/data/state/rl_model_{}_{}.pt",
        sanitize_path_component(embedding_source),
        sanitize_path_component(version),
    )
}

// ─── Container lifecycle (Phase 1E) ─────────────────────────────────────

/// Start (or restart) the container associated with `manifest` for the
/// given project. Idempotent: existing same-named container is `podman
/// rm -f`-ed first. Returns resolved container name on success.
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

    let _ = Command::new(&podman)
        .args(["rm", "-f", &container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

    let placeholders = rl_placeholders(rl_port, &project.slug);
    for vol in &runtime.volumes {
        let host_resolved = resolve_value(&vol.host, ctx, &placeholders);
        let path = PathBuf::from(&host_resolved);
        if let Err(e) = tokio::fs::create_dir_all(&path).await {
            eprintln!(
                "[module_supervisor] mkdir -p {} failed (will let podman surface the error): {}",
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

/// Allocate rl_port (if needed), start container, persist container name.
/// B2 single-writer: this is the canonical writer for both
/// `projects.rl_port` and `module_installs.container_name`.
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

/// Ensure `projects.rl_port` is populated for the project.
pub fn ensure_project_rl_port(db: &Db, project: &ProjectRow) -> Result<u16, String> {
    if let Some(port) = db.get_project_rl_port(&project.id)? {
        return Ok(port);
    }

    use vct_launcher_core::db::models::ProjectHost;
    let port = match project.host {
        ProjectHost::OrchestratorRoot => ORCHESTRATOR_ROOT_RL_PORT,
        _ => allocate_random_rl_port(),
    };
    db.set_project_rl_port(&project.id, port)?;
    Ok(port)
}

/// Random port in `RL_PORT_RANGE_LO..=RL_PORT_RANGE_HI`.
fn allocate_random_rl_port() -> u16 {
    use rand::{Rng, SeedableRng};
    use rand::rngs::StdRng;
    let mut rng = StdRng::from_os_rng();
    rng.random_range(RL_PORT_RANGE_LO..=RL_PORT_RANGE_HI)
}

/// Stop + remove a container by name.
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

/// Is a container with this name currently running?
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

/// Pure helper: parse the inspect output into is-running bool.
pub fn parse_inspect_running_state(stdout: &str) -> bool {
    stdout.trim() == "running"
}

// ─── Startup hook ───────────────────────────────────────────────────────

/// Manifest-resolver callback. Lets `resume_containers_on_startup` look
/// up a `ModuleManifest` by id without depending on the launcher's
/// catalog scanner (which uses `std::env::var("VCT_INSTALL_ROOT")` +
/// launcher-side paths). The hub injects a closure that reads its own
/// catalog cache.
pub type ManifestResolver = Box<dyn Fn(&str) -> Option<ModuleManifest> + Send + Sync>;

/// Iterate every install row where `container_name IS NOT NULL` and
/// probe each container's running state. Restart those that aren't
/// running. Soft-fail per row. Called from `server.rs::start_hub_server`
/// at hub boot.
pub async fn resume_containers_on_startup(db: &Db, resolve_manifest: ManifestResolver) {
    let containers = match db.list_module_installs_with_containers() {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[module_supervisor] resume_containers_on_startup list failed: {}",
                e
            );
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
                    "[module_supervisor] resume: project {} not found, skipping",
                    project_id
                );
                continue;
            }
            Err(e) => {
                eprintln!(
                    "[module_supervisor] resume: get_project({}): {}",
                    project_id, e
                );
                continue;
            }
        };
        let manifest = match resolve_manifest(&module_id) {
            Some(m) => m,
            None => {
                eprintln!(
                    "[module_supervisor] resume: manifest for {} not in catalog",
                    module_id
                );
                continue;
            }
        };
        let rl_port = match ensure_project_rl_port(db, &project) {
            Ok(p) => p,
            Err(e) => {
                eprintln!(
                    "[module_supervisor] resume: ensure_rl_port({}): {}",
                    project_id, e
                );
                continue;
            }
        };
        let ctx = PlaceholderCtx::new(&module_id);
        if let Err(e) = start_container_for_module(&manifest, &ctx, &project, rl_port).await {
            eprintln!(
                "[module_supervisor] resume: start_container_for_module({}): {}",
                project_id, e
            );
        }
    }
}

/// Helper used by the hub-side test fixtures to assert state without a
/// running container runtime. Unused in production paths.
#[allow(dead_code)]
pub(crate) fn _path_helpers_for_tests(_p: &Path) {}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use vct_launcher_core::db::models::ProjectHost;
    use vct_launcher_core::manifest::{
        Compatibility, ContainerInstallBlock, HealthCheck, InstallBlock, InstallMethod,
        LicenseBlock, ModuleCategory, ModuleManifest, PortMapping, Requirements, RuntimeBlock,
        VolumeMount,
    };

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
        }
    }

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
    }

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
        assert_eq!(args[3], "vct-rl-reranker-acme-corp");
        assert!(args.iter().any(|a| a == "127.0.0.1:11533:11438"));
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
    fn parse_inspect_running_state_matches_running() {
        assert!(parse_inspect_running_state("running"));
        assert!(parse_inspect_running_state("running\n"));
        assert!(!parse_inspect_running_state("exited"));
        assert!(!parse_inspect_running_state(""));
        // Case-sensitive.
        assert!(!parse_inspect_running_state("Running"));
    }

    #[test]
    fn sanitize_path_component_rewrites_unsafe_chars() {
        assert_eq!(sanitize_path_component("qwen3"), "qwen3");
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

    #[test]
    fn ensure_project_rl_port_orchestrator_root_pins_fixed_value() {
        let db = Db::open_in_memory().expect("DB");
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
        assert!((RL_PORT_RANGE_LO..=RL_PORT_RANGE_HI).contains(&port));
        let persisted = db.get_project_rl_port("proj-base").unwrap().unwrap();
        assert_eq!(persisted, port);
    }

    #[test]
    fn resume_containers_no_panic_on_empty_db() {
        // Smoke test: an empty DB shouldn't panic the resume sweep.
        // Production behaviour is "log + skip" per row.
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let db = Db::open_in_memory().expect("DB");
            let resolver: ManifestResolver = Box::new(|_id: &str| None);
            // Should return without panicking.
            resume_containers_on_startup(&db, resolver).await;
        });
    }
}
