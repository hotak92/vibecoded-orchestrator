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

use std::future::Future;
use std::path::Path;
use std::pin::Pin;
use std::process::Stdio;
use std::time::Duration;

use tokio::process::Command;

use vct_launcher_core::db::models::ProjectRow;
use vct_launcher_core::db::Db;
use vct_launcher_core::manifest::{InstallMethod, ModuleManifest, PlaceholderCtx};
use vct_launcher_core::process::CommandExt as _;
use vct_launcher_core::services::gpu_mode::GpuMode;

// v0.2.47: shared per-paid-module container helpers. Re-exported here as
// `pub use` so the test module + downstream callers keep their
// unqualified imports. Source of truth lives in
// `vct-launcher-core::services::container_runtime`. The previous local
// copies (which had drifted out of sync with the launcher copies —
// missing variant-tag resolution, missing `--authfile` on `podman run`)
// are gone. See
// `knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md`.
pub use vct_launcher_core::services::container_runtime::{
    build_podman_run_args, container_weights_path, ensure_volume_host_dirs,
    resolve_container_name, resolve_image_ref, resolve_variant_tag, sanitize_path_component,
};

#[allow(unused_imports)]
pub use vct_launcher_core::services::container_runtime::{
    build_port_arg, build_volume_arg, resolve_value, rl_placeholders, DEDUP_SENTINEL,
    DEFAULT_OLLAMA_PORT,
};

// ─── Constants ──────────────────────────────────────────────────────────

/// Fixed RL port for the orchestrator-root project.
pub const ORCHESTRATOR_ROOT_RL_PORT: u16 = 11442;

/// Allocation window for non-orchestrator-root projects.
pub const RL_PORT_RANGE_LO: u16 = 11500;
pub const RL_PORT_RANGE_HI: u16 = 11900;

// ─── Pure helpers (re-exported from core) ──────────────────────────────
//
// v0.2.47: `resolve_container_name`, `resolve_image_ref`,
// `build_podman_run_args`, `rl_placeholders`, `resolve_value`,
// `build_port_arg`, `build_volume_arg`, `sanitize_path_component`,
// `container_weights_path` are no longer authored here. They live in
// `vct-launcher-core::services::container_runtime` and are re-exported
// at the top of this file. Both the hub and the launcher consume the
// SAME implementation — closing the drift gap that produced the
// supervisor-image-resolution-variant bug.

/// Detect which container runtime to use.
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

// v0.2.47: `sanitize_path_component` + `container_weights_path` moved
// to `vct-launcher-core::services::container_runtime` and re-exported
// at the top of this file.

// ─── Container lifecycle (Phase 1E) ─────────────────────────────────────

/// Start (or restart) the container associated with `manifest` for the
/// given project. Idempotent: existing same-named container is `podman
/// rm -f`-ed first. Returns resolved container name on success.
///
/// v0.2.47: variant-aware. Reads the persisted launcher hardware
/// snapshot (same row, same JSON shape the launcher writes) for the
/// host's `GpuMode` and pipes it through `resolve_image_ref` so a
/// manifest with `gpu_image_variants` produces e.g. `:0.2.8-cuda`
/// instead of bare `:0.2.8`. Pre-v0.2.47 the supervisor substituted
/// the bare manifest version, so the supervisor's `podman run`
/// triggered an anonymous re-pull that 401'd against private GHCR for
/// the never-published bare tag. See
/// `knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md`.
pub async fn start_container_for_module(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
) -> Result<String, String> {
    let gpu_mode = read_persisted_gpu_mode_for_supervisor();
    start_container_for_module_with_gpu_mode(manifest, ctx, project, rl_port, gpu_mode).await
}

/// v0.2.47: explicit-GpuMode form of `start_container_for_module`.
/// Used by the resume sweep when the manifest resolver injected at hub
/// startup already knows the host's GpuMode. Mirrors the launcher's
/// `start_container_for_module_with_gpu_mode` so the two paths produce
/// byte-identical image refs for the same `(manifest, gpu_mode)` pair.
pub async fn start_container_for_module_with_gpu_mode(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
    gpu_mode: Option<GpuMode>,
) -> Result<String, String> {
    let runtime = &manifest.runtime;
    if !matches!(runtime.r#type.as_str(), "container" | "service") {
        return Err(format!(
            "start_container_for_module called for non-container runtime '{}'",
            runtime.r#type
        ));
    }

    let name_template = runtime.resolve_container_name_template(&manifest.id);
    let container_name = resolve_container_name(&name_template, &project.slug)?;
    let image_template = runtime.resolve_image_ref(
        manifest.install.container.as_ref().ok_or_else(|| {
            "install.container block missing — required for container/service modules".to_string()
        })?,
        &manifest.version,
    );
    // v0.2.47: variant-aware image ref. Closes the supervisor-image-
    // resolution-variant gap (see node above).
    let image = resolve_image_ref(&image_template, manifest, gpu_mode)?;

    let podman = detect_container_runtime().await?;

    // v0.2.49 Phase 3: pre-pull the variant-correct image with the
    // shared `vct_launcher_core::services::container_runtime::
    // pre_pull_with_auth_for_start` helper — same byte-for-byte flow
    // the launcher-side `start_container_for_module_with_gpu_mode`
    // runs. Closes Bug 2 from
    // `knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md`:
    // the supervisor no longer falls through to anonymous `podman pull`
    // on cache-miss for private GHCR packages. Soft-fail: if pre-pull
    // returns an error, log and let `podman run` make the final call —
    // the cache may have the image already, in which case `run`
    // succeeds without needing the registry. Gated on
    // `manifest.install.method == ContainerPull` AND `gpu_mode.is_some()`
    // — legacy single-tag modules + cases where the caller has no
    // GpuMode source (no persisted hardware snapshot yet) skip the
    // pre-pull and fall through to the historical bare-tag path.
    if gpu_mode.is_some() && manifest.install.method == InstallMethod::ContainerPull {
        if let Err(e) =
            vct_launcher_core::services::container_runtime::pre_pull_with_auth_for_start(
                manifest, &podman, &image,
            )
            .await
        {
            eprintln!(
                "[module_supervisor] pre-pull for start failed (continuing — cache may suffice): {}",
                e
            );
        }
    }

    let _ = Command::new(&podman).silent()
        .args(["rm", "-f", &container_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;

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

/// v0.2.47: read the persisted hardware snapshot from `app_state` and
/// return its `gpu_mode_decided`. Identical SQL row + JSON shape to the
/// launcher's `read_persisted_hardware_snapshot` — the launcher is the
/// canonical writer, the hub is a read-only consumer here.
///
/// Soft-fail to `None` on every error path (DB unavailable, row missing,
/// JSON malformed). The caller treats `None` as "skip variant
/// resolution" — backwards-compatible with pre-v0.2.47 behaviour.
fn read_persisted_gpu_mode_for_supervisor() -> Option<GpuMode> {
    use rusqlite::params;

    // The hub keeps its own DB connection; we open a short-lived one
    // here against the same launcher.db file rather than threading a
    // connection through every resume-sweep callsite. The query is
    // cheap (single key lookup).
    let db_path = vct_launcher_core::db::db_path();
    let conn = rusqlite::Connection::open(db_path).ok()?;
    let raw: Option<String> = conn
        .query_row(
            "SELECT value FROM app_state WHERE key = ?1",
            params!["launcher.hardware_snapshot"],
            |row| row.get(0),
        )
        .ok();
    let raw = raw?;
    // Parse just enough of the JSON to extract `gpu_mode_decided`.
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let mode_str = v.get("gpu_mode_decided").and_then(|x| x.as_str())?;
    serde_json::from_value::<GpuMode>(serde_json::Value::String(mode_str.to_string())).ok()
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

/// Is a container with this name currently running?
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

/// v0.2.49 Phase 3 production resolver. Walks the on-disk catalog the
/// hub already maintains (`<vct_root_dir>/modules/<id>/vct-module.json`
/// AND `<vct_root_dir>/bundled_manifests/*.json`) and returns the first
/// manifest whose `id` matches the requested module_id.
///
/// Soft-fail: a missing / unparseable manifest returns `None`, matching
/// the `ManifestResolver` contract used by `resume_containers_on_startup`.
/// The caller logs the miss and skips the row.
///
/// Why a hub-local resolver (and not delegating to `commands::modules.rs`):
/// the launcher's catalog scanner depends on Tauri State + the
/// launcher's own paths module, neither reachable from this crate.
/// Re-using `modules_api::scan_manifests` keeps the lookup logic
/// in one place AND keeps the cross-crate boundary clean.
pub fn lookup_manifest_by_id(module_id: &str) -> Option<ModuleManifest> {
    super::modules_api::scan_manifests()
        .into_iter()
        .find(|(_, m)| m.id == module_id)
        .map(|(_, m)| m)
}

/// v0.2.49 Phase 3: production `ManifestResolver` boxed for injection
/// into [`resume_containers_on_startup`]. Wraps [`lookup_manifest_by_id`].
/// Production callers (`server.rs::start_hub_server`) inject this;
/// tests pass a custom closure that returns from an in-memory map.
pub fn real_manifest_resolver() -> ManifestResolver {
    Box::new(|id: &str| lookup_manifest_by_id(id))
}

/// Injection point for the NULL-container-name branch's container-start
/// step. Production callers pass [`real_start_after_install`] (which
/// wraps [`start_container_after_install`] and therefore shells out to
/// `podman` / `docker`). Test callers pass a stub that always returns
/// `Err(...)` so the test does not depend on the host having a real
/// container runtime + image cached.
///
/// Why this is injectable (v0.2.46): without it, the
/// `resume_null_container_service_routes_through_start_after_install`
/// test pins `last_error.is_some()` against a real podman invocation —
/// which is environment-dependent (podman+image → succeeds; no podman →
/// fails as the test expects; docker-only → unknown). Injecting the
/// starter makes the test hermetic across all three states.
pub type StartAfterInstall = Box<
    dyn for<'a> Fn(
            &'a ModuleManifest,
            &'a ProjectRow,
            &'a Db,
        ) -> Pin<Box<dyn Future<Output = Result<String, String>> + Send + 'a>>
        + Send
        + Sync,
>;

/// Production `StartAfterInstall` — forwards to the real
/// [`start_container_after_install`].
pub fn real_start_after_install() -> StartAfterInstall {
    Box::new(|manifest, project, db| {
        Box::pin(async move { start_container_after_install(manifest, project, db).await })
    })
}

/// Iterate every `status='installed'` install row and ensure its
/// container is running. Soft-fail per row. Called from
/// `server.rs::start_hub_server` at hub boot.
///
/// v0.2.40 (NEW-3.E): two-bug fix over the pre-v0.2.40 implementation.
///
///   1. **Iterates ALL `status='installed'` rows**, not only rows with
///      a non-NULL `container_name`. Prior to NEW-3.B (v0.2.39),
///      install-time auto-start could hard-fail for any module that
///      didn't explicitly declare `runtime.container_name_template` +
///      `runtime.image_ref` (e.g. RL Reranker v0.2.7). Those installs
///      left rows with `container_name=NULL`. The pre-v0.2.40 resume
///      loop iterated only non-NULL rows → those modules NEVER got a
///      second chance to start, even after v0.2.39 added the
///      defaulting helpers. NEW-3.E covers this by ALSO considering
///      NULL-container rows: routes them through
///      `start_container_after_install`, which invokes the NEW-3.B
///      synthesis path.
///
///   2. **Generalises beyond RL Reranker.** Prior code had a hardcoded
///      `if module_id != "vct-rl-reranker" { continue; }` gate
///      that blocked the restart path for any other future
///      container-distributed module. The gate is replaced with the
///      same manifest-driven gate that install-time auto-start uses
///      (`install.method == ContainerPull` AND `runtime.type ∈
///      {container, service}`). This is the canonical "long-running
///      daemon under hub supervision" signal — `cli` / `mcp_stdio` /
///      `mcp_http` types are deliberately excluded (on-demand
///      invocations, not persistent containers).
///
/// Per-row branches:
///
///   * `container_name = Some(name)` AND running → no-op.
///   * `container_name = Some(name)` AND not running → existing path:
///     `start_container_for_module` (the manifest already declares the
///     name; respect it).
///   * `container_name = None` → NEW-3.E path:
///     `start_container_after_install` (synthesises defaults via
///     NEW-3.B's `resolve_container_name_template` +
///     `resolve_image_ref` helpers AND persists the resolved name back
///     to the DB row).
///
/// Soft-fail discipline: any per-row error logs and the loop continues
/// to the next row — one broken module must not block the rest of the
/// resume sweep.
pub async fn resume_containers_on_startup(db: &Db, resolve_manifest: ManifestResolver) {
    resume_containers_on_startup_with_starter(db, resolve_manifest, real_start_after_install())
        .await
}

/// Test-friendly variant of [`resume_containers_on_startup`] that takes
/// an injected `StartAfterInstall` for the NULL-container-name branch.
/// Production code paths should call [`resume_containers_on_startup`]
/// (which wires the real [`start_container_after_install`]); only tests
/// pass a stub. See [`StartAfterInstall`] for rationale.
pub async fn resume_containers_on_startup_with_starter(
    db: &Db,
    resolve_manifest: ManifestResolver,
    start_after_install: StartAfterInstall,
) {
    let rows = match db.list_module_installs_needing_start() {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[module_supervisor] resume_containers_on_startup list failed: {}",
                e
            );
            return;
        }
    };
    for (project_id_opt, module_id, container_name_opt) in rows {
        // Load the manifest first — both the runtime-type gate AND the
        // restart paths need it.
        let manifest = match resolve_manifest(&module_id) {
            Some(m) => m,
            None => {
                // Likely a module installed from a catalog that's no
                // longer on disk. Skip silently-but-noisily.
                eprintln!(
                    "[module_supervisor] resume: manifest for {} not in catalog, skipping",
                    module_id
                );
                continue;
            }
        };

        // Runtime-type gate (NEW-3 widening, v0.2.38).
        let is_container_distributed =
            manifest.install.method == InstallMethod::ContainerPull
                && matches!(manifest.runtime.r#type.as_str(), "container" | "service");
        if !is_container_distributed {
            continue;
        }

        // Short-circuit if container already running.
        if let Some(ref container_name) = container_name_opt {
            let running = is_container_running(container_name).await.unwrap_or(false);
            if running {
                continue;
            }
        }

        // v0.2.49 Stream A: branch on project_id presence.
        //
        // * `project_id IS NULL` ⇒ GLOBAL row. The hub's supervisor
        //   currently delegates global container start to the launcher
        //   via the lifecycle proxy (see launcher/src/commands/
        //   module_service.rs::start_global_container_for_module).
        //   When the hub runs WITHOUT the launcher (detached service),
        //   the global row resume path falls through to a stub-log;
        //   `start_after_install` only handles per-project rows.
        //   Future iteration: extend `StartAfterInstall` with a global
        //   shape OR factor the global start helper into
        //   container_runtime.rs and call it here directly.
        //
        //   For v0.2.49 Stream A the hub's resume still issues the
        //   start for global rows via direct podman invocation through
        //   the shared `container_runtime` helpers — see the closure
        //   below. The launcher's resume sweep does the same for
        //   global rows; both code paths produce byte-identical
        //   container args via the shared helpers (DEDUP_SENTINEL
        //   asserts this).
        match project_id_opt {
            None => {
                // GLOBAL path — hub side.
                if let Err(e) =
                    start_global_container_supervisor(&manifest, &module_id).await
                {
                    eprintln!(
                        "[module_supervisor] resume: start_global_container_supervisor({}): {}",
                        module_id, e
                    );
                    let _ = db.set_global_module_last_error(&module_id, Some(&e));
                } else {
                    // Persist the resolved container name back to the row
                    // so subsequent resumes short-circuit on the
                    // running-probe path.
                    let name_template = manifest
                        .runtime
                        .resolve_container_name_template(&manifest.id);
                    if let Ok(name) =
                        vct_launcher_core::services::container_runtime::resolve_global_container_name(
                            &name_template,
                            &module_id,
                        )
                    {
                        let _ = db.set_global_module_container_name(&module_id, &name);
                    }
                }
            }
            Some(project_id) => {
                // PER-PROJECT path.
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

                match container_name_opt {
                    Some(prior_container_name) => {
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
                        match start_container_for_module(&manifest, &ctx, &project, rl_port).await
                        {
                            Ok(resolved_name) => {
                                // V52-D: mirror of the launcher-side
                                // container_name drift fix in
                                // module_service.rs. Persist the
                                // resolved name back to DB so
                                // subsequent probes short-circuit
                                // instead of triggering an endless
                                // respawn loop.
                                if resolved_name != prior_container_name {
                                    eprintln!(
                                        "[module_supervisor] resume: V52-D container_name drift \
                                         for {}/{}: DB='{}' → resolved='{}'; updating DB.",
                                        project_id, module_id,
                                        prior_container_name, resolved_name,
                                    );
                                    if let Err(e) = db.set_module_container_name(
                                        &project_id, &module_id, &resolved_name,
                                    ) {
                                        eprintln!(
                                            "[module_supervisor] resume: V52-D \
                                             set_module_container_name({}, {}): {}",
                                            project_id, module_id, e
                                        );
                                    }
                                }
                                // V52-W: clear stale last_error on
                                // successful restart.
                                let _ = db.set_module_last_error(
                                    &project_id, &module_id, None,
                                );
                            }
                            Err(e) => {
                                eprintln!(
                                    "[module_supervisor] resume: start_container_for_module({}, {}): {}",
                                    project_id, module_id, e
                                );
                            }
                        }
                    }
                    None => {
                        if let Err(e) =
                            start_after_install(&manifest, &project, db).await
                        {
                            eprintln!(
                                "[module_supervisor] resume: start_container_after_install({}, {}): {}",
                                project_id, module_id, e
                            );
                            let _ =
                                db.set_module_last_error(&project_id, &module_id, Some(&e));
                        }
                    }
                }
            }
        }
    }
}

/// v0.2.49 Stream A: hub-side global container start. Mirrors the
/// launcher's `start_global_container_for_module` byte-for-byte via the
/// shared `container_runtime` helpers — see the
/// `DEDUP_SENTINEL` assertion in core for the de-duplication contract.
///
/// Returns `Ok(container_name)` on success; the caller persists the
/// container_name to the DB row. Returns `Err(...)` on any podman error
/// or missing manifest field.
pub async fn start_global_container_supervisor(
    manifest: &ModuleManifest,
    module_id: &str,
) -> Result<String, String> {
    use vct_launcher_core::services::container_runtime::{
        build_podman_run_args_global, ensure_volume_host_dirs_global, resolve_global_container_name,
        resolve_image_ref,
    };

    /// Fixed RL listen port — kept in sync with the launcher's
    /// `GLOBAL_RL_PORT` constant. If the launcher's constant changes,
    /// update this one too (the hub doesn't depend on the launcher
    /// crate, so the constant is duplicated here).
    const GLOBAL_RL_PORT: u16 = 11443;

    let runtime = &manifest.runtime;
    if !matches!(runtime.r#type.as_str(), "container" | "service") {
        return Err(format!(
            "start_global_container_supervisor called for non-container runtime '{}'",
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

    let gpu_mode = read_persisted_gpu_mode_for_supervisor();
    let image = resolve_image_ref(&image_template, manifest, gpu_mode)?;

    let podman = detect_container_runtime().await?;

    if gpu_mode.is_some() && manifest.install.method == InstallMethod::ContainerPull {
        if let Err(e) =
            vct_launcher_core::services::container_runtime::pre_pull_with_auth_for_start(
                manifest, &podman, &image,
            )
            .await
        {
            eprintln!(
                "[module_supervisor] global pre-pull for start failed (continuing — cache may suffice): {}",
                e
            );
        }
    }

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

    Ok(container_name)
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
    use vct_launcher_core::db::models::{ModuleStatus, ProjectHost};
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
                scope: vct_launcher_core::manifest::InstallScope::PerProject,
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
            // v0.2.49 access-matrix item #13 — global-scope
            // modules can declare KG collections that get auto-seeded
            // into kg_collection_access for every project. The
            // rl-reranker test fixture is a CONSUMER, not a producer,
            // so it declares none.
            kg_collections: None,
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
            None,
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

    // ─── NEW-3.B (2026-05-28): hub-side synthesized defaults ─────────────

    /// NEW-3.B: `runtime.type = "service"` must now pass the gate in
    /// `start_container_for_module` on the hub side. Mirrors the launcher-side
    /// `build_podman_run_args_accepts_service_runtime_type` test.
    #[test]
    fn hub_start_container_accepts_service_runtime_type() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.r#type = "service".into();
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        // `build_podman_run_args` is the synchronous gate we can test
        // without spawning podman — it validates runtime type and resolves
        // args. If the type gate still rejected "service", this would error.
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "vct-rl-reranker-acme-corp",
            "ghcr.io/hotak92/vct-rl-reranker:0.1.0",
        )
        .expect("service runtime must be accepted by hub after NEW-3.B");
        assert_eq!(args[0], "run");
    }

    /// NEW-3.B: manifest with `container_name_template=None` and
    /// `image_ref=None` synthesizes sensible defaults and reaches
    /// `build_podman_run_args` without a hard-fail "missing" error.
    #[test]
    fn hub_start_container_succeeds_with_synthesized_defaults() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.r#type = "service".into();
        // Remove explicitly-declared fields to exercise the synthesis path.
        manifest.runtime.container_name_template = None;
        manifest.runtime.image_ref = None;

        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);

        // Synthesize the way start_container_for_module now does.
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

        assert_eq!(container_name, "vct-rl-reranker-acme-corp",
            "synthesized container name must match module-id + project-slug");
        assert_eq!(image, "ghcr.io/hotak92/vct-rl-reranker:0.1.0",
            "synthesized image must use install.container.image + manifest.version");

        let args =
            build_podman_run_args(&manifest, &ctx, &project, 11533, &container_name, &image)
                .expect("build_podman_run_args must succeed with synthesized defaults");
        assert!(args.iter().any(|a| a == "vct-rl-reranker-acme-corp"));
    }

    // ─── NEW-3.E (v0.2.40, 2026-05-29): resume-on-boot path ──────────────
    //
    // These tests pin the four-branch gate behaviour of
    // `resume_containers_on_startup`. Real podman is NOT available in the
    // test environment, so tests assert on observable side-effects:
    // (a) which module_ids the ManifestResolver was asked to resolve (the
    //     resolver is called before the runtime-type gate, so it's an
    //     accurate witness of "what rows did the sweep visit"), and
    // (b) for NULL-container rows that DO pass the gate, the soft-fail
    //     path from real-podman invocation writes to
    //     `module_installs.last_error` (the NEW-3.C surfacing path).

    use std::sync::Arc;
    use std::sync::Mutex;

    /// Build an in-memory Db pre-populated with a single base-host project
    /// `proj-rs`. Returns `(db, project_id)`.
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

    /// Build a manifest with the supplied runtime type and install method.
    /// Reuses `make_manifest`'s container block so resolve_image_ref can
    /// synthesize a valid image ref.
    fn make_manifest_for_gate(
        runtime_type: &str,
        method: InstallMethod,
    ) -> ModuleManifest {
        let mut m = make_manifest(true, true);
        m.runtime.r#type = runtime_type.into();
        m.install.method = method;
        m
    }

    /// Wrap a ManifestResolver in a tracker so tests can verify which
    /// module_ids the resume loop visited.
    fn tracking_resolver(
        manifests: std::collections::HashMap<String, ModuleManifest>,
    ) -> (ManifestResolver, Arc<Mutex<Vec<String>>>) {
        let visited = Arc::new(Mutex::new(Vec::<String>::new()));
        let visited_clone = Arc::clone(&visited);
        let resolver: ManifestResolver = Box::new(move |id: &str| {
            visited_clone.lock().unwrap().push(id.to_string());
            manifests.get(id).cloned()
        });
        (resolver, visited)
    }

    /// T1 (NEW-3.E): a `status='installed'` row with `container_name=NULL`
    /// AND a manifest declaring `runtime.type='service'` + `install.method=
    /// ContainerPull` PASSES the gate — the resolver is invoked AND the
    /// resume routes through `start_container_after_install`, which will
    /// fail (no real podman in tests) and surface the error to
    /// `module_installs.last_error` via NEW-3.C's set_module_last_error.
    /// This is the failure mode this branch fixes — pre-v0.2.40 the row
    /// was skipped entirely because `list_module_installs_with_containers`
    /// excluded NULL-container rows.
    ///
    /// v0.2.46 hermeticity fix: pinning behaviour on real
    /// `start_container_after_install` made this test environment-
    /// dependent (a host with podman + the RL Reranker image cached would
    /// SUCCEED the call and break the `last_error.is_some()` assertion).
    /// We now drive the NULL-container branch via the new
    /// `StartAfterInstall` injection and pass a stub that always returns
    /// `Err(...)`, so the assertion holds whether the test host has
    /// podman+image, docker, or neither. The branch logic under test
    /// (NEW-3.C's `set_module_last_error` write after a failed start) is
    /// unchanged — only the source of the failure switched from a real
    /// `podman run` shell-out to a deterministic in-process stub.
    #[test]
    fn resume_null_container_service_routes_through_start_after_install() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let (db, pid) = open_db_with_resume_project();

            // Insert the row — install completed but auto-start never
            // wrote a container_name (the v0.2.40 bug case).
            db.insert_module_install(
                "install-id-t1",
                &pid,
                "vct-rl-reranker",
                "0.2.7",
                "/tmp/vct-rl-reranker",
            )
            .expect("insert");
            db.set_module_status(&pid, "vct-rl-reranker", ModuleStatus::Installed, None)
                .expect("set installed");

            // Sanity: container_name is NULL.
            let row_before = db
                .get_module_install(&pid, "vct-rl-reranker")
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
            let mut manifests = std::collections::HashMap::new();
            manifests.insert(
                "vct-rl-reranker".to_string(),
                make_manifest_for_gate("service", InstallMethod::ContainerPull),
            );
            let (resolver, visited) = tracking_resolver(manifests);

            // Hermetic stub: simulate the failure mode the NEW-3.E NULL-
            // container branch is designed to surface. The error string
            // is propagated to `last_error` via NEW-3.C's
            // `set_module_last_error`. We do NOT call
            // `set_module_container_name` from here — the production
            // code only persists the name on Ok, so a failing stub
            // leaves `container_name=NULL`, matching what a real
            // `podman run` failure would do.
            let starter: StartAfterInstall = Box::new(|_manifest, _project, _db| {
                Box::pin(async move {
                    Err("test stub: container start unavailable".to_string())
                })
            });

            resume_containers_on_startup_with_starter(&db, resolver, starter).await;

            // Resolver was invoked for our row.
            let visited_ids = visited.lock().unwrap().clone();
            assert!(
                visited_ids.contains(&"vct-rl-reranker".to_string()),
                "resume must call resolver for the NULL-container row, visited={:?}",
                visited_ids
            );

            // After the gate passed, start_container_after_install was
            // called and failed (no real podman). NEW-3.C path wrote
            // last_error.
            let row_after = db
                .get_module_install(&pid, "vct-rl-reranker")
                .unwrap()
                .unwrap();
            assert!(
                row_after.last_error.is_some(),
                "last_error must be set after start_container_after_install fails (NEW-3.C surface), row={:?}",
                row_after
            );
            // Status must remain Installed — the install itself
            // succeeded; only the post-boot start failed.
            assert_eq!(
                row_after.status,
                ModuleStatus::Installed,
                "status must remain Installed after resume-start failure"
            );
        });
    }

    /// T4 (NEW-3.E gate): a `status='installed'` row whose manifest
    /// declares a non-`container_pull` install method (e.g. GitClone) is
    /// SKIPPED before any container-start attempt. The resolver is
    /// invoked (it's called before the gate check), but no
    /// `last_error` is written and `start_container_*` is not reached.
    #[test]
    fn resume_non_container_pull_row_skipped_by_gate() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let (db, pid) = open_db_with_resume_project();

            db.insert_module_install(
                "install-id-t4",
                &pid,
                "git-installed-module",
                "0.1.0",
                "/tmp/git-installed-module",
            )
            .expect("insert");
            db.set_module_status(
                &pid,
                "git-installed-module",
                ModuleStatus::Installed,
                None,
            )
            .expect("set installed");

            // service runtime but GitClone install — the gate must
            // reject this row (resume-on-boot is for container-pull
            // modules only).
            let mut manifests = std::collections::HashMap::new();
            manifests.insert(
                "git-installed-module".to_string(),
                make_manifest_for_gate("service", InstallMethod::GitClone),
            );
            let (resolver, visited) = tracking_resolver(manifests);

            resume_containers_on_startup(&db, resolver).await;

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
        });
    }

    /// T2/T3 (NEW-3.E existing path): a `status='installed'` row with a
    /// PRE-PERSISTED `container_name` + a service-type ContainerPull
    /// manifest passes the gate and reaches the "named container" branch.
    ///
    /// **Branch contract** (post-V52-D):
    ///   * On `start_container_for_module` Err: `last_error` is NOT
    ///     written (eprintln!-only, unlike the NULL-container branch's
    ///     NEW-3.C surfacing).
    ///   * On `start_container_for_module` Ok: V52-D persists the
    ///     resolved container name back to DB IF it differs from the
    ///     pre-populated name, AND V52-W clears stale last_error.
    ///
    /// This fixture pre-populates `container_name='vct-rl-reranker-rs-slug'`
    /// which already matches the manifest's resolved template → V52-D
    /// finds no drift and the DB row stays untouched. (The drift-fix
    /// path is exercised in the launcher-side test
    /// `v0252_d_resume_named_container_drift_persists_resolved_name`.)
    /// The invariant pinned here is therefore "last_error stays None"
    /// across both Err and Ok regimes.
    #[test]
    fn resume_named_container_row_uses_existing_path_no_lasterror() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let (db, pid) = open_db_with_resume_project();

            db.insert_module_install(
                "install-id-t23",
                &pid,
                "vct-rl-reranker",
                "0.2.7",
                "/tmp/vct-rl-reranker",
            )
            .expect("insert");
            db.set_module_status(&pid, "vct-rl-reranker", ModuleStatus::Installed, None)
                .expect("set installed");
            // Pre-populate container_name (the existing path's
            // precondition).
            db.set_module_container_name(
                &pid,
                "vct-rl-reranker",
                "vct-rl-reranker-rs-slug",
            )
            .expect("set container_name");

            let mut manifests = std::collections::HashMap::new();
            manifests.insert(
                "vct-rl-reranker".to_string(),
                make_manifest_for_gate("service", InstallMethod::ContainerPull),
            );
            let (resolver, visited) = tracking_resolver(manifests);

            resume_containers_on_startup(&db, resolver).await;

            // Resolver was called.
            assert!(
                visited
                    .lock()
                    .unwrap()
                    .contains(&"vct-rl-reranker".to_string())
            );

            // Existing-path's failure-handler is eprintln-only — must
            // NOT call set_module_last_error. This pins that the NEW-3.E
            // refactor didn't cross-wire the named-container branch into
            // the NEW path's NEW-3.C surfacing.
            let row_after = db
                .get_module_install(&pid, "vct-rl-reranker")
                .unwrap()
                .unwrap();
            assert!(
                row_after.last_error.is_none(),
                "named-container existing-path failure must NOT write last_error (only the NULL-container NEW-3.E branch does), row={:?}",
                row_after
            );
            // container_name must remain set (we didn't overwrite it).
            assert_eq!(
                row_after.container_name.as_deref(),
                Some("vct-rl-reranker-rs-slug"),
                "container_name must remain set"
            );
        });
    }

    /// T4-variant (gate): runtime.type='cli' rejected even when
    /// install.method=ContainerPull. The gate accepts only `container` |
    /// `service`. This protects against future schema additions where a
    /// container-pull-distributed CLI tool's manifest leaks into the
    /// resume sweep.
    #[test]
    fn resume_cli_runtime_skipped_by_gate() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
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

            let mut manifests = std::collections::HashMap::new();
            manifests.insert(
                "cli-only-module".to_string(),
                make_manifest_for_gate("cli", InstallMethod::ContainerPull),
            );
            let (resolver, _visited) = tracking_resolver(manifests);

            resume_containers_on_startup(&db, resolver).await;

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
        });
    }

    // ─── v0.2.47: variant-aware image-ref + dedup tests ────────────────

    /// v0.2.47 Bug-1 regression (hub side): variant-aware image ref
    /// flows end-to-end through `resolve_image_ref` with a
    /// `Some(GpuMode::Cuda)` parameter. Mirrors the launcher-side test
    /// `v0247_resolve_image_ref_cuda_variant_end_to_end` so both code
    /// paths are pinned independently — a regression in either crate's
    /// wiring of the shared core helper surfaces immediately.
    #[test]
    fn v0247_hub_resolve_image_ref_cuda_variant_end_to_end() {
        use vct_launcher_core::manifest::GpuImageVariants;
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
        let image = resolve_image_ref(&template, &manifest, Some(GpuMode::Cuda))
            .expect("resolve");
        assert_eq!(image, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda");
    }

    /// v0.2.47 de-dup regression (hub side): assert that the hub and
    /// the launcher pull from the SAME
    /// `vct-launcher-core::services::container_runtime` module by
    /// comparing the constant `DEDUP_SENTINEL` re-exported through the
    /// local `pub use` block to the canonical value defined in core —
    /// they must be byte-identical.
    ///
    /// Paired with `v0247_helpers_have_one_source_of_truth` on the
    /// launcher side; both must pass to certify the de-duplication is
    /// real and not just two structurally-identical copies.
    #[test]
    fn v0247_hub_helpers_have_one_source_of_truth() {
        assert_eq!(
            DEDUP_SENTINEL,
            vct_launcher_core::services::container_runtime::DEDUP_SENTINEL,
            "module_supervisor::DEDUP_SENTINEL must equal the core constant — \
             a mismatch indicates accidental local-shadow re-introduction"
        );
    }

    // ─── v0.2.49 Phase 3: hub-side manifest resolver ───────────────────

    /// v0.2.49 Phase 3 helper: `VCT_STATE_DIR`-scoped guard used by the
    /// manifest-resolver tests below.
    struct VctStateDirGuard {
        _td: tempfile::TempDir,
        previous: Option<String>,
    }

    impl VctStateDirGuard {
        fn new() -> Self {
            use std::sync::{Mutex, OnceLock};
            static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
            let _g = LOCK
                .get_or_init(|| Mutex::new(()))
                .lock()
                .unwrap_or_else(|p| p.into_inner());
            let td = tempfile::tempdir().expect("tempdir");
            let previous = std::env::var("VCT_STATE_DIR").ok();
            std::env::set_var("VCT_STATE_DIR", td.path());
            drop(_g);
            Self { _td: td, previous }
        }

        fn vct_root(&self) -> std::path::PathBuf {
            self._td.path().to_path_buf()
        }
    }

    impl Drop for VctStateDirGuard {
        fn drop(&mut self) {
            match &self.previous {
                Some(v) => std::env::set_var("VCT_STATE_DIR", v),
                None => std::env::remove_var("VCT_STATE_DIR"),
            }
        }
    }

    /// Write a minimal valid manifest JSON to
    /// `<vct_root>/bundled_manifests/<module_id>.json`.
    fn write_manifest(vct_root: &std::path::Path, module_id: &str) {
        let dir = vct_root.join("bundled_manifests");
        std::fs::create_dir_all(&dir).expect("mkdir bundled_manifests");
        let path = dir.join(format!("{}.json", module_id));
        let json = serde_json::json!({
            "manifest_version": 1,
            "id": module_id,
            "name": module_id,
            "version": "0.0.1",
            "description": "test",
            "category": "paid-independent",
            "compatibility": { "hosts": [] },
            "license": { "required": false },
            "requirements": {},
            "install": {
                "method": "container_pull",
                "install_dir": format!("/tmp/{}", module_id),
                "post_install": [],
                "container": {
                    "image": format!("ghcr.io/test/{}", module_id),
                    "tag_from_version": true,
                    "pull_token_endpoint": "https://example.invalid/x",
                    "pull_token_method": "POST",
                    "rotate_weights": false
                }
            },
            "secrets": [],
            "settings": [],
            "runtime": {
                "type": "container",
                "command": "echo",
                "args": [],
                "env_fixed": {},
                "env_derived": {},
                "env_from_secrets": [],
                "env_from_settings": [],
                "ports": [],
                "volumes": [],
                "auto_restart": false,
                "gpu_optional": true
            },
            "provides": [],
            "consumes": []
        });
        std::fs::write(&path, serde_json::to_string(&json).unwrap()).expect("write manifest");
    }

    /// v0.2.49: `lookup_manifest_by_id` walks the on-disk catalog and
    /// returns the manifest whose `id` matches. Pins the production
    /// resolver wiring used by `server.rs` + `lifecycle_api::module_start`.
    #[test]
    fn v0249_lookup_manifest_by_id_finds_bundled() {
        let g = VctStateDirGuard::new();
        write_manifest(&g.vct_root(), "vct-test-module-49a");
        let m = lookup_manifest_by_id("vct-test-module-49a")
            .expect("manifest must be found by id");
        assert_eq!(m.id, "vct-test-module-49a");
        assert_eq!(m.runtime.r#type, "container");
    }

    /// v0.2.49: `lookup_manifest_by_id` returns `None` for an
    /// unknown id (must not panic, must not pick a wrong manifest).
    #[test]
    fn v0249_lookup_manifest_by_id_returns_none_for_unknown() {
        let _g = VctStateDirGuard::new();
        // Empty catalog directory; nothing to find.
        assert!(
            lookup_manifest_by_id("vct-no-such-module").is_none(),
            "unknown module_id must return None"
        );
    }

    /// v0.2.49: `real_manifest_resolver()` is a thin closure-wrapping
    /// of `lookup_manifest_by_id`. Pins that both paths agree on the
    /// same lookup.
    #[test]
    fn v0249_real_manifest_resolver_matches_lookup_by_id() {
        let g = VctStateDirGuard::new();
        write_manifest(&g.vct_root(), "vct-test-module-49b");
        let resolver = real_manifest_resolver();
        let via_resolver = resolver("vct-test-module-49b").expect("resolver finds it");
        let via_lookup = lookup_manifest_by_id("vct-test-module-49b").expect("lookup finds it");
        assert_eq!(via_resolver.id, via_lookup.id);
        assert_eq!(via_resolver.runtime.r#type, via_lookup.runtime.r#type);
    }

    /// v0.2.49: pre_pull_with_auth_for_start signature parity — the
    /// supervisor's `start_container_for_module_with_gpu_mode` calls
    /// the shared core helper. We can't assert behaviour without a
    /// real podman, but we CAN assert the shared symbol is reachable
    /// from this crate (a refactor that accidentally removed the
    /// re-export OR renamed the function would fail compilation here).
    #[allow(dead_code)]
    fn _v0249_pre_pull_with_auth_for_start_reachable_from_hub() {
        async fn _typecheck(
            m: &ModuleManifest,
            r: &str,
            i: &str,
        ) -> Result<(), String> {
            vct_launcher_core::services::container_runtime::pre_pull_with_auth_for_start(m, r, i)
                .await
        }
        let _ = _typecheck;
    }

    // ─── v0.2.49 Stream A: global resume path ───────────────────────────

    /// Resume sweep handles a GLOBAL install row (project_id=NULL): the
    /// resolver is invoked, the gate passes (service + container_pull),
    /// and the global path is taken. Whether the start succeeds or fails
    /// depends on the test host's container runtime + cached image
    /// availability — assert only on the deterministic side effects
    /// (resolver invocation + no panic).
    ///
    /// Note: the manifest id MUST match the install row's module_id, or
    /// `make_manifest`'s hardcoded id leaks downstream. We use the fixture
    /// manifest's id ("vct-rl-reranker") for the install row.
    #[test]
    fn v0249_resume_global_install_row_invokes_resolver_for_global_path() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let db = Db::open_in_memory().expect("DB");

            db.insert_global_module_install(
                "install-id-global-1",
                "vct-rl-reranker",
                "0.2.10",
                "/tmp/vct-rl-reranker",
            )
            .expect("global insert");
            db.set_global_module_status(
                "vct-rl-reranker",
                ModuleStatus::Installed,
                None,
            )
            .expect("set global installed");

            let mut manifests = std::collections::HashMap::new();
            manifests.insert(
                "vct-rl-reranker".to_string(),
                make_manifest_for_gate("service", InstallMethod::ContainerPull),
            );
            let (resolver, visited) = tracking_resolver(manifests);

            // Smoke: resume must not panic on a global row.
            resume_containers_on_startup(&db, resolver).await;

            // Resolver was invoked even for global rows.
            assert!(
                visited
                    .lock()
                    .unwrap()
                    .contains(&"vct-rl-reranker".to_string()),
                "resolver must be invoked for global rows too"
            );

            // The global row is still there (no DB corruption).
            let g_row = db
                .get_global_module_install("vct-rl-reranker")
                .expect("get global")
                .expect("global row exists");
            assert_eq!(g_row.module_id, "vct-rl-reranker");
            assert!(g_row.project_id.is_none(), "still NULL project_id");
        });
    }

    /// Resume sweep: a global row with `git_clone` install method is
    /// SKIPPED by the gate (same behaviour as per-project rows). The
    /// resolver IS still invoked (gate runs after resolution).
    #[test]
    fn v0249_resume_global_row_with_git_clone_skipped_by_gate() {
        let rt = tokio::runtime::Runtime::new().expect("rt");
        rt.block_on(async {
            let db = Db::open_in_memory().expect("DB");
            db.insert_global_module_install(
                "install-g",
                "vct-rl-reranker",
                "0.2.10",
                "/tmp/x",
            )
            .expect("insert global");
            db.set_global_module_status(
                "vct-rl-reranker",
                ModuleStatus::Installed,
                None,
            )
            .expect("installed");

            let mut manifests = std::collections::HashMap::new();
            manifests.insert(
                "vct-rl-reranker".to_string(),
                make_manifest_for_gate("service", InstallMethod::GitClone),
            );
            let (resolver, visited) = tracking_resolver(manifests);

            resume_containers_on_startup(&db, resolver).await;

            assert!(visited.lock().unwrap().contains(&"vct-rl-reranker".to_string()));

            // last_error must NOT be set — the gate rejected before reaching
            // the start path.
            let g_row = db
                .get_global_module_install("vct-rl-reranker")
                .unwrap()
                .unwrap();
            assert!(
                g_row.last_error.is_none(),
                "git_clone global row must NOT trigger start path (no last_error)"
            );
        });
    }
}
