use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, AppHandle, Emitter, State};

use crate::db::Db;
use crate::services::runtime::{detect_runtime, RuntimeInfo};
use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
use vct_launcher_core::process::CommandExt as _;
use vct_launcher_core::services::service_endpoints::{
    adopted_autostart_container, awaits_choice, compose_managed_services, is_compose_managed,
    lifecycle_container, machine_row_from_disk, machine_rows_from_disk, CoreService,
};
use vct_launcher_core::services::service_status::{health_url, service_state};

/// The services snapshot — ONE wire shape shared with the hub's
/// `/services/status` (`vct_launcher_core::services::service_status`).
pub use vct_launcher_core::services::service_status::{
    ServiceRuntimeState, ServicesRuntimeSnapshot,
};

// ---------------------------------------------------------------------------
// Shared-container lifecycle (Podman/Docker compose + adopted containers).
//
// v0.2.97 (service endpoints SSOT): every decision in this file comes from
// the launcher.db `service_endpoints` rows (one per core service, written
// ONLY by `vco_lib.service_endpoints`):
//
//   * `vco_managed`       — VCO's compose owns the container. Compose is
//                           invoked with an EXPLICIT service list naming
//                           only these (never a bare `up -d` / `stop` /
//                           `restart`), so no command here can create a
//                           compose copy of someone else's service
//                           (plan invariant I1).
//   * `adopted_container` — someone else's container. Start/Stop/Restart
//                           drive `<runtime> start|stop|restart <name>` BY
//                           NAME; nothing here ever removes or recreates it.
//   * `adopted_external`  — a URL (native process, remote host). VCO has no
//                           lifecycle over it; the buttons say so.
//
// The v0.2.7 container picker and the `services.toml` adoption state are
// retired: which container a service is, is the row's `container_name`, and
// changing it is a Python verb (`adopt`, `use-vco-copy`, `hand-to-vco`) the
// Services page calls through `vco_lib_bridge` — Rust never writes a row.
//
// Consumers: auto-start on launcher boot (lib.rs), the tray, the Services
// page, the quit dialog's "Quit and stop services" (`services_stop_all`),
// and the launcher's services watcher.
// ---------------------------------------------------------------------------

/// HTTP probe with 2s timeout. Returns true on 2xx/3xx.
async fn probe_url(url: &str) -> bool {
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };
    matches!(client.get(url).send().await, Ok(r) if r.status().as_u16() < 400)
}

/// PR-15 G2 (v0.2.11): detect zombie containers (Podman state-DB desync —
/// `podman ps` says "Up X minutes" but the main PID is dead).
///
/// Returns `true` when the container exists, `inspect` reports a main PID,
/// and that PID is not alive on the host (Linux `/proc/<pid>` absent). Skipped
/// on non-Linux: inside a podman-machine VM the host PID cross-check is
/// meaningless. Soft-fail: any subprocess error returns `false`.
async fn detect_container_zombie(runtime_binary: &std::path::Path, container_name: &str) -> bool {
    if !cfg!(target_os = "linux") {
        return false;
    }
    let inspect = tokio::time::timeout(
        std::time::Duration::from_secs(3),
        tokio::process::Command::new(runtime_binary)
            .silent()
            .args(["inspect", "--format", "{{.State.Pid}}", container_name])
            .output(),
    )
    .await;
    let pid_str = match inspect {
        Ok(Ok(out)) if out.status.success() => String::from_utf8_lossy(&out.stdout).trim().to_string(),
        _ => return false,
    };
    let pid: i32 = match pid_str.parse() {
        Ok(p) if p > 0 => p,
        _ => return false, // PID 0 means stopped, not zombied
    };
    !std::path::PathBuf::from(format!("/proc/{}", pid)).exists()
}

/// Is Weaviate already answering where this machine reaches it? A
/// false-negative guard for runtime detection: a launcher spawned with a
/// stripped PATH can fail to find podman/docker while the services run.
async fn services_already_running() -> bool {
    let row = machine_row_from_disk(CoreService::Weaviate);
    probe_url(&health_url(CoreService::Weaviate, row.as_ref())).await
}

/// Resolve the compose directory — `<repo_root>/infrastructure`.
fn compose_dir() -> Result<PathBuf, String> {
    let root = crate::commands::installer::find_local_repo_root()?;
    Ok(root.join("infrastructure"))
}

/// PR-15 G3 (v0.2.11): the `launch-claude-mcp-stack` wrapper shipped with
/// the install. PREFERRED over direct compose: it owns the CDI-readiness
/// wait (the `vco_code_embed` GPU boot race), runtime.txt resolution and
/// daemon-access validation. `None` when not shipped (caller falls back).
fn find_stack_wrapper() -> Option<PathBuf> {
    let root = crate::commands::installer::find_local_repo_root().ok()?;
    let script_name = if cfg!(target_os = "windows") {
        "launch-claude-mcp-stack.ps1"
    } else {
        "launch-claude-mcp-stack.sh"
    };
    let candidate = root.join("scripts").join(script_name);
    candidate.is_file().then_some(candidate)
}

/// Env var carrying the explicit compose service list to the wrapper
/// (space-separated). MUST MATCH the wrapper's reader and
/// `vct-hub/src/infra_watchdog.rs::ENV_COMPOSE_SERVICES`. An empty list
/// means "nothing to do", so this file never calls the wrapper with one.
pub const ENV_COMPOSE_SERVICES: &str = "VCO_COMPOSE_SERVICES";

/// Run the wrapper for `services` (never empty — the caller checks).
///
/// Cross-OS: `bash <script> <subcommand>` on Linux/macOS (no reliance on the
/// exec bit); `powershell -NoProfile -ExecutionPolicy Bypass -File <script>
/// <subcommand>` on Windows (matches the Scheduled Task template).
async fn run_stack_wrapper(subcommand: &str, services: &[&str]) -> Result<(), String> {
    let wrapper = find_stack_wrapper()
        .ok_or_else(|| "launch-claude-mcp-stack wrapper not found at <install>/scripts/".to_string())?;
    let mut cmd = if cfg!(target_os = "windows") {
        let mut c = tokio::process::Command::new("powershell").silent();
        c.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            wrapper.to_str().ok_or("non-UTF8 wrapper path")?,
            subcommand,
        ]);
        c
    } else {
        let mut c = tokio::process::Command::new("bash").silent();
        c.arg(&wrapper).arg(subcommand);
        c
    };
    cmd.env(ENV_COMPOSE_SERVICES, services.join(" "));
    if let Ok(root) = crate::commands::installer::find_local_repo_root() {
        cmd.current_dir(root);
    }
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn launch-claude-mcp-stack wrapper: {}", e))?;
    if !output.status.success() {
        return Err(format!(
            "launch-claude-mcp-stack {} failed (status {}): {}",
            subcommand,
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

/// Run `<runtime> compose <args>` in the compose dir, capturing stderr so
/// the frontend shows the real failure.
async fn run_compose<I, S>(info: &RuntimeInfo, args: I) -> Result<(), String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    let dir = compose_dir()?;
    let mut cmd = info.compose_command();
    cmd.args(args);
    cmd.current_dir(&dir);
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} compose: {}", info.runtime.display_name(), e))?;
    if !output.status.success() {
        return Err(format!(
            "{} compose failed (status {}): {}",
            info.runtime.display_name(),
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

/// The `compose up` argv for `services` — the ONE Python rule
/// (`vco_lib.service_lifecycle.compose_up_args`, via
/// `vct_launcher_core::services::compose_args`): `--no-deps` always (code_embed's
/// `depends_on: ollama` must never create an Ollama next to an adopted one),
/// `--profile gpu` with code_embed, and `None` — no compose call — for an
/// empty list. `build` rebuilds code_embed's image from the checkout (v0.2.92
/// BLOCKER-1: `up -d` alone builds only a MISSING image, so a source fix could
/// stay absent from the running service through every update).
async fn up_argv_with(
    python: &std::path::Path,
    root: &std::path::Path,
    services: &[&str],
    build: bool,
) -> Result<Option<Vec<String>>, String> {
    let args =
        vct_launcher_core::services::compose_args::compose_up_args(python, root, services, build).await?;
    Ok(if args.is_empty() { None } else { Some(args) })
}

/// [`up_argv_with`] for this install (its clone root and vco_lib Python).
async fn up_argv(services: &[&str], build: bool) -> Result<Option<Vec<String>>, String> {
    if services.is_empty() {
        return Ok(None);
    }
    let root = crate::commands::installer::find_local_repo_root()?;
    let python = vct_launcher_core::services::compose_args::rule_python()?;
    up_argv_with(&python, &root, services, build).await
}

/// The services "Start all" brings up through compose: the compose-managed
/// ones (`vco_managed` + enabled, or no row). Never an adopted service (I1).
fn start_all_compose_services(rows: &[(CoreService, Option<ServiceEndpointRow>)]) -> Vec<&'static str> {
    compose_managed_services(rows)
}

/// Bring `services` up: the wrapper first when `prefer_wrapper` (CDI-wait for
/// GPU containers; it takes the list as `VCO_COMPOSE_SERVICES`), else / then
/// direct compose with the rule's argv. An empty list does nothing.
async fn start_managed(info: &RuntimeInfo, services: &[&str], build: bool, prefer_wrapper: bool) -> Result<(), String> {
    if services.is_empty() {
        return Ok(());
    }
    if prefer_wrapper && find_stack_wrapper().is_some() {
        match run_stack_wrapper("start", services).await {
            Ok(()) => return Ok(()),
            Err(e) => tracing::warn!(
                "[lifecycle] launch-claude-mcp-stack start failed, falling back to direct compose: {}",
                e
            ),
        }
    }
    match up_argv(services, build).await? {
        Some(args) => run_compose(info, args).await,
        None => Ok(()),
    }
}

/// Stop or restart ONE compose-managed service BY ITS CONTAINER NAME — no
/// compose invocation, so no dependency is pulled in and no profile is
/// needed. A restart of a container that does not exist yet creates it
/// (the `up` rule). A stop of a missing container is a no-op.
async fn stop_or_restart_managed(info: &RuntimeInfo, service: CoreService, action: &str) -> Result<(), String> {
    let row = machine_row_from_disk(service);
    let container = lifecycle_container(service, row.as_ref()).unwrap_or_else(|| {
        vct_launcher_core::services::service_endpoints::canonical_container_name(service).to_string()
    });
    validate_container_name(&container)?;
    if container_exists(info, &container).await? {
        return control_container(info, &container, action).await;
    }
    if action == "restart" {
        return start_managed(info, &[service.name()], service == CoreService::CodeEmbed, false).await;
    }
    Ok(())
}

/// What a lifecycle verb does for one service, decided from its row. Pure,
/// so the routing is testable without a runtime.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum LifecycleRoute {
    /// VCO's compose, for this service only.
    Compose { service: &'static str },
    /// The adopted container, by name (never removed, never recreated).
    Container { name: String },
    /// An adopted URL (or an adopted container with no name): VCO has no
    /// lifecycle over it.
    NoLifecycle { reason: String },
}

pub(crate) fn lifecycle_route(service: CoreService, row: Option<&ServiceEndpointRow>) -> LifecycleRoute {
    if is_compose_managed(row) {
        return LifecycleRoute::Compose { service: service.name() };
    }
    match row.map(|r| r.mode) {
        Some(EndpointMode::AdoptedContainer) => match lifecycle_container(service, row) {
            Some(name) => LifecycleRoute::Container { name },
            None => LifecycleRoute::NoLifecycle {
                reason: format!("the adopted {} container has no recorded name", service.name()),
            },
        },
        Some(EndpointMode::VcoManaged) if awaits_choice(service, row) => LifecycleRoute::NoLifecycle {
            reason: format!(
                "where {} runs is waiting for your choice — use Choose… on the Services page \
                 (use the instance VCO found, or run VCO's own copy)",
                service.name()
            ),
        },
        Some(EndpointMode::VcoManaged) => LifecycleRoute::NoLifecycle {
            reason: format!("{} is disabled on this machine", service.name()),
        },
        _ => LifecycleRoute::NoLifecycle {
            reason: format!(
                "{} is an external endpoint ({}); VCO does not start or stop it",
                service.name(),
                row.map(|r| vct_launcher_core::services::service_endpoints::render_url(service, Some(r)))
                    .unwrap_or_default()
            ),
        },
    }
}

/// Probe the three core services on their rows' endpoints.
#[command]
pub async fn services_status() -> Result<ServicesRuntimeSnapshot, String> {
    let rows = machine_rows_from_disk();
    let endpoints_missing = rows.iter().any(|(_, r)| r.is_none());
    let mut services: Vec<ServiceRuntimeState> =
        rows.into_iter().map(|(svc, row)| service_state(svc, row)).collect();

    let probes = futures::future::join_all(services.iter().map(|s| probe_url(&s.url))).await;
    let runtime_info = detect_runtime().await;

    for (state, running) in services.iter_mut().zip(probes) {
        state.running = running;
        // Zombie detection needs a named container, a runtime, and a failed
        // probe. It is read-only; recovery (below) is mode-gated.
        if !running && state.mode != Some(EndpointMode::AdoptedExternal) {
            if let (Some(cn), Some(ri)) = (state.container_name.as_ref(), runtime_info.as_ref()) {
                state.zombie = detect_container_zombie(&ri.binary_path, cn).await;
            }
        }
    }

    Ok(ServicesRuntimeSnapshot {
        services,
        runtime: runtime_info.as_ref().map(|r| r.runtime.binary().to_string()),
        needs_podman_machine_start: runtime_info.as_ref().map(|r| r.needs_machine_start).unwrap_or(false),
        endpoints_missing,
        degraded: false,
    })
}

/// The ONE `service_endpoints` read the Services page's diagnostics use:
/// every core service with its row (`null` = none yet).
#[command]
pub async fn services_get_endpoints() -> Result<Vec<(String, Option<ServiceEndpointRow>)>, String> {
    Ok(machine_rows_from_disk()
        .into_iter()
        .map(|(svc, row)| (svc.name().to_string(), row))
        .collect())
}

/// PR-15 G2 (v0.2.11) + v0.2.97: recover a stuck (zombie) service.
///
/// Row-gated (plan §4b):
///   * `vco_managed` — force-remove the stale record of VCO's OWN container,
///     then bring THIS service back up (`up -d <service>` through the
///     wrapper — CDI-wait preserved — else direct compose).
///   * `adopted_container` — NEVER removed: a `rm` would let compose recreate
///     the service on VCO's default (empty) data volume. VCO only tries
///     `<runtime> start <name>`; if that cannot clear the stale state, the
///     error says the container's owner has to.
///   * `adopted_external` — nothing to recover.
#[command]
pub async fn recover_zombie(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let service = CoreService::from_name(&name).ok_or("unknown service")?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found; cannot recover a stuck container")?;
    let row = machine_row_from_disk(service);
    match lifecycle_route(service, row.as_ref()) {
        LifecycleRoute::Compose { service: svc } => {
            let container = lifecycle_container(service, row.as_ref())
                .unwrap_or_else(|| vct_launcher_core::services::service_endpoints::canonical_container_name(service).to_string());
            validate_container_name(&container)?;
            let rm_out = tokio::process::Command::new(&info.binary_path)
                .silent()
                .args(["rm", "--force", &container])
                .output()
                .await
                .map_err(|e| format!("spawn {} rm --force: {}", info.runtime.display_name(), e))?;
            if !rm_out.status.success() {
                let stderr = String::from_utf8_lossy(&rm_out.stderr).to_lowercase();
                if !stderr.contains("no such container") && !stderr.contains("not found") {
                    return Err(format!(
                        "{} rm --force {} failed: {}",
                        info.runtime.display_name(),
                        container,
                        stderr.trim()
                    ));
                }
            }
            start_managed(&info, &[svc], false, true).await
        }
        LifecycleRoute::Container { name: container } => {
            control_container(&info, &container, "start").await.map_err(|e| {
                format!(
                    "{} (VCO never removes an adopted container: if it stays stuck, \
                     its owner has to clear it, e.g. `{} rm -f {}` then recreate it \
                     with the same data)",
                    e,
                    info.runtime.binary(),
                    container
                )
            })
        }
        LifecycleRoute::NoLifecycle { reason } => Err(structured_err(ERR_KIND_NO_LIFECYCLE, reason)),
    }
}

/// Bring up what VCO runs: the compose-managed services (one explicit list)
/// and the adopted containers whose rows ask to be started by name.
/// Idempotent.
#[command]
pub async fn services_start_all() -> Result<(), String> {
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found. Install Podman or Docker.")?;
    let rows = machine_rows_from_disk();
    let managed = start_all_compose_services(&rows);

    // BLOCKER-1 (v0.2.62): a deliberate START re-enables watchdog
    // supervision for the managed services.
    set_pause_markers(&managed, false);

    let mut errors: Vec<String> = Vec::new();
    if let Err(e) = start_managed(&info, &managed, false, true).await {
        errors.push(e);
    }
    for (_, row) in &rows {
        if let Some(name) = adopted_autostart_container(row.as_ref()) {
            if let Err(e) = control_container(&info, name, "start").await {
                errors.push(e);
            }
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}

/// Stop VCO's compose-managed services WITHOUT removing volumes (no `-v`).
/// Adopted containers belong to someone else and keep running — the same
/// behaviour the pre-v0.2.97 project-scoped `compose stop` had for them.
/// Used by Quit-confirmation's "Quit and stop services".
///
/// BLOCKER-1 (v0.2.62): on success, drop a watchdog pause marker for each
/// stopped service so the hub's watchdog knows the stop was deliberate.
#[command]
pub async fn services_stop_all() -> Result<(), String> {
    let info = detect_runtime().await.ok_or("No container runtime found.")?;
    let managed = compose_managed_services(&machine_rows_from_disk());
    for name in &managed {
        let service = CoreService::from_name(name).ok_or("unknown service")?;
        stop_or_restart_managed(&info, service, "stop").await?;
    }
    set_pause_markers(&managed, true);
    Ok(())
}

/// Restart VCO's compose-managed services — each by its container name (a
/// missing one is created through the `up` rule).
#[command]
pub async fn services_restart_all() -> Result<(), String> {
    let info = detect_runtime().await.ok_or("No container runtime found.")?;
    let managed = compose_managed_services(&machine_rows_from_disk());
    set_pause_markers(&managed, false);
    let mut errors: Vec<String> = Vec::new();
    for name in &managed {
        let service = CoreService::from_name(name).ok_or("unknown service")?;
        if let Err(e) = stop_or_restart_managed(&info, service, "restart").await {
            errors.push(e);
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}

/// BLOCKER-1 helper: create (`pause = true`) or remove the hub-watchdog pause
/// marker for each of `services` — callers pass compose-managed services
/// only (the watchdog supervises nothing else). Soft-fail: a filesystem error
/// is logged, never blocks the button.
fn set_pause_markers(services: &[&str], pause: bool) {
    for name in services {
        let res = if pause {
            vct_launcher_core::services::watchdog_pause::create_pause_marker(name)
        } else {
            vct_launcher_core::services::watchdog_pause::remove_pause_marker(name)
        };
        if let Err(e) = res {
            tracing::warn!(
                "[lifecycle] watchdog pause-marker {} for '{}' soft-failed: {}",
                if pause { "create" } else { "remove" },
                name,
                e
            );
        }
    }
}

/// The pause marker for ONE service, only when it is compose-managed.
fn set_pause_marker_for_service(name: &str, pause: bool) {
    let Some(service) = CoreService::from_name(name) else { return };
    if is_compose_managed(machine_row_from_disk(service).as_ref()) {
        set_pause_markers(&[service.name()], pause);
    }
}

/// Start a single service. See [`lifecycle_route`].
#[command]
pub async fn service_start(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime().await.ok_or("No container runtime found.")?;
    route_service_action(&info, &name, "start").await?;
    set_pause_marker_for_service(&name, false);
    Ok(())
}

/// Stop a single service.
#[command]
pub async fn service_stop(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime().await.ok_or("No container runtime found.")?;
    route_service_action(&info, &name, "stop").await?;
    set_pause_marker_for_service(&name, true);
    Ok(())
}

/// Restart a single service.
#[command]
pub async fn service_restart(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime().await.ok_or("No container runtime found.")?;
    route_service_action(&info, &name, "restart").await?;
    set_pause_marker_for_service(&name, false);
    Ok(())
}

/// Dispatch a single-service action by the service's row.
async fn route_service_action(info: &RuntimeInfo, name: &str, action: &str) -> Result<(), String> {
    let service = CoreService::from_name(name).ok_or("unknown service")?;
    match lifecycle_route(service, machine_row_from_disk(service).as_ref()) {
        LifecycleRoute::Compose { service: svc } => {
            if action == "start" {
                start_managed(info, &[svc], svc == "code_embed", false).await
            } else {
                stop_or_restart_managed(info, service, action).await
            }
        }
        LifecycleRoute::Container { name: container } => control_container(info, &container, action).await,
        LifecycleRoute::NoLifecycle { reason } => Err(structured_err(ERR_KIND_NO_LIFECYCLE, reason)),
    }
}

/// Drive `<runtime> start|stop|restart <container>` directly — the only
/// thing VCO ever does to an adopted container.
pub(crate) async fn control_container(info: &RuntimeInfo, container: &str, action: &str) -> Result<(), String> {
    if !matches!(action, "start" | "stop" | "restart") {
        return Err(format!("invalid action '{}' (expected start | stop | restart)", action));
    }
    validate_container_name(container)?;
    if !container_exists(info, container).await? {
        return Err(structured_err(
            ERR_KIND_CONTAINER_MISSING,
            format!(
                "container '{}' no longer exists; use Change… on the Services page to \
                 pick where this service runs now",
                container
            ),
        ));
    }
    let argv = build_control_argv(info.runtime.binary(), action, container);
    let mut cmd = tokio::process::Command::new(&info.binary_path).silent();
    for a in argv.iter().skip(1) {
        cmd.arg(a);
    }
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} {}: {}", info.runtime.binary(), action, e))?;
    if !output.status.success() {
        return Err(format!(
            "{} {} {} failed (status {}): {}",
            info.runtime.binary(),
            action,
            container,
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

/// Pure argv builder for [`control_container`]: `[runtime_bin, action,
/// container]`.
pub(crate) fn build_control_argv(runtime_bin: &str, action: &str, container: &str) -> Vec<String> {
    vec![runtime_bin.to_string(), action.to_string(), container.to_string()]
}

/// Structured error prefixes. The frontend matches on the prefix (up to
/// the first `:`); keep them stable.
pub const ERR_KIND_CONTAINER_MISSING: &str = "container_missing";
/// v0.2.97: the service's row gives VCO no lifecycle over it.
pub const ERR_KIND_NO_LIFECYCLE: &str = "no_lifecycle";

/// `"<kind>: <human message>"`.
pub(crate) fn structured_err(kind: &str, msg: impl AsRef<str>) -> String {
    format!("{}: {}", kind, msg.as_ref())
}

/// `Ok(true)` when `<runtime> inspect <name>` finds the container.
pub(crate) async fn container_exists(info: &RuntimeInfo, name: &str) -> Result<bool, String> {
    let mut cmd = tokio::process::Command::new(&info.binary_path).silent();
    cmd.args(["inspect", "--format", "{{.Id}}", name]);
    let out = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} inspect: {}", info.runtime.binary(), e))?;
    Ok(out.status.success())
}

/// Only the three canonical services are valid targets — no arbitrary
/// string reaches `compose <verb> <name>`.
fn validate_service_name(name: &str) -> Result<(), String> {
    match name {
        "weaviate" | "ollama" | "code_embed" => Ok(()),
        _ => Err(format!("unknown service '{}'; expected weaviate | ollama | code_embed", name)),
    }
}

/// A container name as runtimes accept it (`[A-Za-z0-9][A-Za-z0-9_.-]*`,
/// ≤ 128) — a name from the DB or the frontend never reaches argv otherwise.
pub(crate) fn validate_container_name(name: &str) -> Result<(), String> {
    let ok = !name.is_empty()
        && name.len() <= 128
        && name.as_bytes()[0].is_ascii_alphanumeric()
        && name.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'));
    if ok {
        Ok(())
    } else {
        Err(format!("refusing unsafe container name {:?}", name))
    }
}

// ---------------------------------------------------------------------------
// Where a service runs — the Python verbs (the rows' ONE writer)
// ---------------------------------------------------------------------------

/// A Services-page / adoption-dialog decision. Validated in Rust before it
/// becomes argv for `python -m vco_lib.service_endpoints`.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case", tag = "action")]
pub enum EndpointAction {
    /// Use an existing container (by name) or URL for this service. Leaving
    /// a Weaviate that holds VCO data for one that holds none needs
    /// `accept_empty_kg` (plan I6).
    Adopt {
        service: String,
        #[serde(default)]
        container: Option<String>,
        #[serde(default)]
        url: Option<String>,
        #[serde(default)]
        accept_empty_kg: bool,
    },
    /// Run VCO's own copy (optionally on a given port). Leaving a Weaviate
    /// that holds VCO data needs `accept_empty_kg` (plan I6).
    UseVcoCopy {
        service: String,
        #[serde(default)]
        port: Option<u16>,
        #[serde(default)]
        accept_empty_kg: bool,
    },
    /// "Let VCO manage this container" — the opt-in ownership transfer of
    /// an adopted Weaviate/Ollama container, with mount verification and
    /// rollback (owner ruling Q2). The page shows the data mount first.
    HandToVco { service: String },
}

/// The argv (after `-m vco_lib.service_endpoints`, before the bridge's
/// `--root <root>`) for `action`, or why it is refused. Pure. MUST MATCH the
/// parser in `vco_lib/service_endpoints.py::_build_arg_parser` (`adopt`,
/// `use-vco-copy`, `hand-to-vco`) — pinned by `endpoint_action_argv_shapes`
/// and, against the real parser, by `endpoint_action_argv_is_accepted_by_the_python_parser`.
pub(crate) fn endpoint_action_argv(action: &EndpointAction) -> Result<Vec<String>, String> {
    let adoptable = |s: &str| -> Result<(), String> {
        validate_service_name(s)?;
        if s == "code_embed" {
            return Err("code_embed is always VCO's own service; it cannot be adopted".into());
        }
        Ok(())
    };
    let mut argv: Vec<String> = Vec::new();
    match action {
        EndpointAction::Adopt { service, container, url, accept_empty_kg } => {
            adoptable(service)?;
            argv.extend(["adopt".into(), "--service".into(), service.clone()]);
            match (container.as_deref(), url.as_deref()) {
                (Some(c), None) => {
                    validate_container_name(c)?;
                    argv.extend(["--container".into(), c.to_string()]);
                }
                (None, Some(u)) => {
                    validate_endpoint_url(u)?;
                    argv.extend(["--url".into(), u.to_string()]);
                }
                _ => return Err("adopt needs exactly one of a container name or a URL".into()),
            }
            if *accept_empty_kg {
                argv.push("--accept-empty-kg".into());
            }
        }
        EndpointAction::UseVcoCopy { service, port, accept_empty_kg } => {
            validate_service_name(service)?;
            argv.extend(["use-vco-copy".into(), "--service".into(), service.clone()]);
            if let Some(p) = port {
                if *p < 1024 {
                    return Err(format!("port {} is privileged; pick 1024–65535", p));
                }
                argv.extend(["--port".into(), p.to_string()]);
            }
            if *accept_empty_kg {
                argv.push("--accept-empty-kg".into());
            }
        }
        EndpointAction::HandToVco { service } => {
            adoptable(service)?;
            // The GUI showed the data mount and the user confirmed it.
            argv.extend(["hand-to-vco".into(), "--service".into(), service.clone()]);
        }
    }
    Ok(argv)
}

/// An adopted URL: `http(s)://…`, no whitespace, bounded.
fn validate_endpoint_url(url: &str) -> Result<(), String> {
    let ok = (url.starts_with("http://") || url.starts_with("https://"))
        && url.len() <= 512
        && !url.chars().any(|c| c.is_whitespace() || c.is_control());
    if ok {
        Ok(())
    } else {
        Err(format!("refusing endpoint URL {:?} (expected http(s)://host:port)", url))
    }
}

/// Candidate endpoints for every service (or one): containers, native
/// processes, upstream-default ports — with VCO-data fingerprints and the
/// compatibility verdict. The ONE detector is Python
/// (`vco_lib/service_detection.py`).
#[command]
pub async fn services_endpoint_candidates(
    db: State<'_, Db>,
    service: Option<String>,
) -> Result<serde_json::Value, String> {
    if let Some(s) = service.as_deref() {
        validate_service_name(s)?;
    }
    let root = crate::services::vco_lib_bridge::resolve_orchestrator_root(&db);
    tokio::task::spawn_blocking(move || {
        crate::services::vco_lib_bridge::service_endpoint_candidates(root.as_deref(), service.as_deref())
    })
    .await
    .map_err(|e| format!("candidate detection task failed: {}", e))?
}

/// Change where a service runs — through the Python verb, the rows' one
/// writer (which also re-projects every project and refreshes the MCP
/// registration, and resolves the matching UPDATE_DEFERRED entry).
#[command]
pub async fn services_endpoint_action(
    db: State<'_, Db>,
    action: EndpointAction,
) -> Result<serde_json::Value, String> {
    let argv = endpoint_action_argv(&action)?;
    let root = crate::services::vco_lib_bridge::resolve_orchestrator_root(&db);
    tokio::task::spawn_blocking(move || {
        crate::services::vco_lib_bridge::service_endpoint_verb(root.as_deref(), &argv)
    })
    .await
    .map_err(|e| format!("service endpoint task failed: {}", e))?
}

// ---------------------------------------------------------------------------
// Auto-start on launcher boot
// ---------------------------------------------------------------------------

/// Frontend event names.
pub const EVT_EXTERNAL_DETECTED: &str = "vct-external-services-detected";
pub const EVT_LIFECYCLE_PROGRESS: &str = "vct-services-lifecycle";
/// Emitted when neither Podman nor Docker is detected at launcher boot.
pub const EVT_NO_CONTAINER_RUNTIME: &str = "vct-no-container-runtime";

#[derive(Debug, Clone, Serialize)]
pub struct LifecycleProgress {
    /// `"detecting_runtime"` | `"runtime_missing"` | `"awaiting_choice"` |
    /// `"starting"` | `"started"` | `"start_failed"`.
    pub phase: String,
    pub message: String,
}

/// The services whose endpoint is waiting for the user's choice
/// ([`awaits_choice`]: no row yet, or the Weaviate confirmation-pending row
/// — owner ruling Q1: a third-party Weaviate is never adopted silently). An
/// unattended Ollama adoption is not a question. Pure.
pub(crate) fn pending_choices(rows: &[(CoreService, Option<ServiceEndpointRow>)]) -> Vec<&'static str> {
    rows.iter()
        .filter(|(svc, row)| awaits_choice(*svc, row.as_ref()))
        .map(|(svc, _)| svc.name())
        .collect()
}

/// The `vct-external-services-detected` payload: which services wait for a
/// choice, every service's row, and the Python detector's report.
pub(crate) fn choice_event_payload(
    pending: &[&str],
    rows: &[(CoreService, Option<ServiceEndpointRow>)],
    detection: serde_json::Value,
) -> serde_json::Value {
    let rows: serde_json::Map<String, serde_json::Value> = rows
        .iter()
        .map(|(svc, row)| (svc.name().to_string(), serde_json::to_value(row).unwrap_or_default()))
        .collect();
    serde_json::json!({ "pending": pending, "rows": rows, "detection": detection })
}

/// Auto-start the shared services on launcher boot (background task).
///
///   1. Detect the runtime. Missing → emit `runtime_missing` (unless
///      Weaviate already answers), return.
///   2. When a service's endpoint waits for a choice ([`pending_choices`]),
///      run the Python detector and hand both to the adoption dialog
///      (`vct-external-services-detected`); NOTHING is started — starting
///      VCO's own copy now could duplicate the very service the user is
///      being asked about.
///   3. Else start whatever VCO runs that is down (`services_start_all`).
pub async fn auto_start_on_boot(app: AppHandle) {
    let emit = |phase: &str, message: String| {
        let _ = app.emit(EVT_LIFECYCLE_PROGRESS, LifecycleProgress { phase: phase.into(), message });
    };
    emit("detecting_runtime", "Detecting container runtime…".into());

    let info = match detect_runtime().await {
        Some(i) => i,
        None => {
            if services_already_running().await {
                tracing::info!(
                    "[lifecycle] runtime detection returned None but Weaviate is reachable — \
                     suppressing vct-no-container-runtime modal"
                );
                emit("started", "Services already running.".into());
                return;
            }
            emit(
                "runtime_missing",
                "No container runtime found. Install Podman or Docker to run VCT services.".into(),
            );
            let os = if cfg!(target_os = "linux") {
                "linux"
            } else if cfg!(target_os = "macos") {
                "macos"
            } else if cfg!(target_os = "windows") {
                "windows"
            } else {
                "unknown"
            };
            let _ = app.emit(EVT_NO_CONTAINER_RUNTIME, serde_json::json!({ "os": os }));
            return;
        }
    };

    if info.needs_machine_start {
        emit(
            "runtime_missing",
            "Podman is installed but no machine is running. Run `podman machine start` and re-detect.".into(),
        );
        return;
    }

    let rows = machine_rows_from_disk();
    let pending = pending_choices(&rows);
    if !pending.is_empty() {
        let root = {
            use tauri::Manager as _;
            app.try_state::<Db>()
                .and_then(|db| crate::services::vco_lib_bridge::resolve_orchestrator_root(&db))
        };
        let detection = tokio::task::spawn_blocking(move || {
            crate::services::vco_lib_bridge::service_endpoint_candidates(root.as_deref(), None)
        })
        .await
        .map_err(|e| e.to_string())
        .and_then(|r| r)
        .unwrap_or_else(|e| {
            // The dialog still asks (it can offer VCO's own copy); it says
            // detection failed instead of listing candidates.
            tracing::warn!("[lifecycle] service endpoint detection failed at boot: {}", e);
            serde_json::json!({ "error": e })
        });
        let _ = app.emit(EVT_EXTERNAL_DETECTED, choice_event_payload(&pending, &rows, detection));
        emit("awaiting_choice", format!("Waiting for your choice for: {}.", pending.join(", ")));
        return;
    }

    let snapshot = match services_status().await {
        Ok(s) => s,
        Err(e) => {
            emit("start_failed", format!("status probe failed: {}", e));
            return;
        }
    };
    let any_down = snapshot.services.iter().zip(rows.iter()).any(|(s, (_, row))| {
        !s.running && (is_compose_managed(row.as_ref()) || adopted_autostart_container(row.as_ref()).is_some())
    });
    if !any_down {
        emit("started", "Services already running.".into());
        return;
    }
    emit("starting", format!("Starting VCT services via {}…", info.runtime.display_name()));
    match services_start_all().await {
        Ok(()) => emit("started", "Services up.".into()),
        Err(e) => emit("start_failed", e),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod services_lifecycle_tests {
    use super::*;

    fn row(service: &str, mode: EndpointMode, port: u16) -> ServiceEndpointRow {
        let mut r = ServiceEndpointRow::new(service, mode, "localhost", port);
        if service == "weaviate" {
            r.grpc_port = Some(50052);
        }
        r
    }

    fn python() -> std::path::PathBuf {
        vct_launcher_core::python_resolve::resolve_python_for_vco_lib_or("python3")
    }

    /// SE-4 × SE-3 red-proof: EVERY compose `up` argv the launcher builds
    /// carries `--no-deps` and names no adopted service — with Weaviate and
    /// Ollama adopted, "Start all" composes code_embed alone (profile gpu,
    /// no-deps: its `depends_on: ollama` must not create an Ollama next to the
    /// adopted one). Red against a hand-built `up -d <svc>` or a list that
    /// includes adopted services.
    #[tokio::test]
    async fn no_compose_up_argv_lacks_no_deps_or_names_an_adopted_service() {
        let mut w = row("weaviate", EndpointMode::AdoptedContainer, 8081);
        w.container_name = Some("their_weaviate".into());
        let rows = vec![
            (CoreService::Weaviate, Some(w)),
            (CoreService::Ollama, Some(row("ollama", EndpointMode::AdoptedExternal, 11434))),
            (CoreService::CodeEmbed, Some(row("code_embed", EndpointMode::VcoManaged, 11440))),
        ];
        let root = crate::services::vco_lib_bridge::test_checkout_root();
        let services = start_all_compose_services(&rows);
        let argv = up_argv_with(&python(), &root, &services, false).await.unwrap().expect("code_embed");
        assert!(argv.iter().any(|a| a == "--no-deps"), "{:?}", argv);
        assert!(!argv.iter().any(|a| a == "weaviate" || a == "ollama"), "adopted services named: {:?}", argv);
        assert_eq!(argv, vec!["--profile", "gpu", "up", "-d", "--no-deps", "code_embed"]);

        // The single-service start of code_embed rebuilds its image.
        let one = up_argv_with(&python(), &root, &["code_embed"], true).await.unwrap().unwrap();
        assert!(one.contains(&"--build".to_string()) && one.contains(&"--no-deps".to_string()));

        // Nothing managed → no compose call at all.
        assert_eq!(up_argv_with(&python(), &root, &[], false).await.unwrap(), None);
    }

    /// The single-service router: VCO's own → compose; an adopted container
    /// → by name; an adopted URL → no lifecycle. Red if the router stops
    /// consulting the row (e.g. always composing).
    #[test]
    fn lifecycle_route_follows_the_row() {
        assert_eq!(
            lifecycle_route(CoreService::CodeEmbed, Some(&row("code_embed", EndpointMode::VcoManaged, 11440))),
            LifecycleRoute::Compose { service: "code_embed" }
        );
        assert_eq!(lifecycle_route(CoreService::Ollama, None), LifecycleRoute::Compose { service: "ollama" });
        let mut w = row("weaviate", EndpointMode::AdoptedContainer, 8081);
        w.container_name = Some("their_weaviate".into());
        assert_eq!(
            lifecycle_route(CoreService::Weaviate, Some(&w)),
            LifecycleRoute::Container { name: "their_weaviate".into() }
        );
        assert!(matches!(
            lifecycle_route(CoreService::Ollama, Some(&row("ollama", EndpointMode::AdoptedExternal, 11434))),
            LifecycleRoute::NoLifecycle { .. }
        ));
        let mut disabled = row("code_embed", EndpointMode::VcoManaged, 11440);
        disabled.enabled = false;
        assert!(matches!(
            lifecycle_route(CoreService::CodeEmbed, Some(&disabled)),
            LifecycleRoute::NoLifecycle { .. }
        ));
    }

    /// Owner ruling Q1: the Weaviate "waiting for your choice" row
    /// (`vco_managed`, disabled) is never started — not by "Start all", not
    /// by its own Start button (no lifecycle), not by boot. Red if the gate
    /// ignores `enabled`.
    #[test]
    fn the_waiting_weaviate_row_is_never_started() {
        let mut w = row("weaviate", EndpointMode::VcoManaged, 8081);
        w.enabled = false;
        let rows = vec![
            (CoreService::Weaviate, Some(w.clone())),
            (CoreService::Ollama, Some(row("ollama", EndpointMode::VcoManaged, 11435))),
            (CoreService::CodeEmbed, Some(row("code_embed", EndpointMode::VcoManaged, 11440))),
        ];
        assert_eq!(start_all_compose_services(&rows), vec!["ollama", "code_embed"]);
        assert!(matches!(lifecycle_route(CoreService::Weaviate, Some(&w)), LifecycleRoute::NoLifecycle { .. }));
        assert_eq!(pending_choices(&rows), vec!["weaviate"]);
    }

    #[test]
    fn validate_service_name_accepts_canonical_and_rejects_the_rest() {
        for ok in ["weaviate", "ollama", "code_embed"] {
            assert!(validate_service_name(ok).is_ok());
        }
        for bad in ["postgres", "", "weaviate; rm -rf /", "../etc/passwd"] {
            assert!(validate_service_name(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn validate_container_name_refuses_injection() {
        for ok in ["vco_weaviate", "their-ollama.1", "a"] {
            assert!(validate_container_name(ok).is_ok(), "{ok}");
        }
        for bad in ["", "-x", "; rm -rf /", "a b", "a/b", "$(id)"] {
            assert!(validate_container_name(bad).is_err(), "{bad}");
        }
    }

    #[tokio::test]
    async fn recover_zombie_rejects_unknown_service_names() {
        assert!(recover_zombie("; rm -rf /".to_string()).await.is_err());
        assert!(recover_zombie(String::new()).await.is_err());
    }

    /// The service states come from the rows: port, health URL (the row's
    /// HOST — a remote Ollama is probed where it is), mode, container.
    #[test]
    fn service_states_follow_the_rows() {
        let _g = vct_launcher_core::test_env::state_dir_guard();
        let db = crate::db::Db::open().unwrap();
        db.service_endpoint_seed_for_tests(&ServiceEndpointRow::new(
            "ollama",
            EndpointMode::AdoptedExternal,
            "gpu.lan",
            11434,
        ))
        .unwrap();
        let states: Vec<ServiceRuntimeState> =
            machine_rows_from_disk().into_iter().map(|(s, r)| service_state(s, r)).collect();
        let ollama = states.iter().find(|s| s.name == "ollama").unwrap();
        assert_eq!(ollama.url, "http://gpu.lan:11434/api/tags");
        assert!(ollama.externally_managed);
        // No row on this harness state dir → the sentinel, never 8081.
        let weaviate = states.iter().find(|s| s.name == "weaviate").unwrap();
        assert_eq!(weaviate.url, "http://127.0.0.1:9/v1/meta");
    }

    #[tokio::test]
    async fn services_already_running_probe_is_the_sentinel_in_a_harness() {
        let _g = vct_launcher_core::test_env::state_dir_guard();
        // The harness guard points the probe at 127.0.0.1:9 — it can only
        // be false, and it never reaches a real local Weaviate.
        assert!(!services_already_running().await);
    }

    #[test]
    fn build_control_argv_shape_is_fixed() {
        assert_eq!(build_control_argv("podman", "start", "their_weaviate"), vec!["podman", "start", "their_weaviate"]);
    }

    #[test]
    fn structured_error_kinds_are_stable() {
        assert!(structured_err(ERR_KIND_CONTAINER_MISSING, "x").starts_with("container_missing:"));
        assert!(structured_err(ERR_KIND_NO_LIFECYCLE, "x").starts_with("no_lifecycle:"));
    }

    // ---- the Python verbs' argv --------------------------------------

    #[test]
    fn endpoint_action_argv_shapes() {
        for (action, want) in argv_cases() {
            assert_eq!(endpoint_action_argv(&action).unwrap(), want, "{:?}", action);
        }
    }

    fn argv_cases() -> Vec<(EndpointAction, Vec<&'static str>)> {
        vec![
            (
                EndpointAction::Adopt {
                    service: "weaviate".into(),
                    container: Some("their_weaviate".into()),
                    url: None,
                    accept_empty_kg: false,
                },
                vec!["adopt", "--service", "weaviate", "--container", "their_weaviate"],
            ),
            (
                EndpointAction::Adopt {
                    service: "ollama".into(),
                    container: None,
                    url: Some("http://localhost:11434".into()),
                    accept_empty_kg: false,
                },
                vec!["adopt", "--service", "ollama", "--url", "http://localhost:11434"],
            ),
            (
                EndpointAction::Adopt {
                    service: "weaviate".into(),
                    container: Some("w".into()),
                    url: None,
                    accept_empty_kg: true,
                },
                vec!["adopt", "--service", "weaviate", "--container", "w", "--accept-empty-kg"],
            ),
            (
                EndpointAction::UseVcoCopy { service: "weaviate".into(), port: Some(18081), accept_empty_kg: true },
                vec!["use-vco-copy", "--service", "weaviate", "--port", "18081", "--accept-empty-kg"],
            ),
            (
                EndpointAction::HandToVco { service: "ollama".into() },
                vec!["hand-to-vco", "--service", "ollama"],
            ),
        ]
    }

    /// Every argv the GUI can send is ACCEPTED by the real Python parser
    /// (`_build_arg_parser().parse_args`, plus the bridge's `--root`) — the
    /// Rust side cannot drift from the verbs' flags unseen.
    #[test]
    fn endpoint_action_argv_is_accepted_by_the_python_parser() {
        let _g = vct_launcher_core::test_env::state_dir_guard();
        let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib_or("python3");
        let root = crate::services::vco_lib_bridge::test_checkout_root();
        for (action, _) in argv_cases() {
            let mut argv: Vec<String> = endpoint_action_argv(&action).unwrap();
            argv.extend(["--root".to_string(), root.display().to_string()]);
            let out = std::process::Command::new(&python)
                .arg("-c")
                .arg("import sys, vco_lib.service_endpoints as se; se._build_arg_parser().parse_args(sys.argv[1:])")
                .args(&argv)
                .current_dir(&root)
                .output()
                .expect("run python");
            assert!(
                out.status.success(),
                "{:?} rejected: {}",
                argv,
                String::from_utf8_lossy(&out.stderr)
            );
        }
    }

    #[test]
    fn endpoint_action_argv_refusals() {
        let refused = [
            EndpointAction::Adopt { service: "code_embed".into(), container: Some("x".into()), url: None, accept_empty_kg: false },
            EndpointAction::Adopt { service: "weaviate".into(), container: None, url: None, accept_empty_kg: false },
            EndpointAction::Adopt {
                service: "weaviate".into(),
                container: Some("a".into()),
                url: Some("http://x:1".into()),
                accept_empty_kg: false,
            },
            EndpointAction::Adopt { service: "weaviate".into(), container: Some("$(id)".into()), url: None, accept_empty_kg: false },
            EndpointAction::Adopt {
                service: "ollama".into(),
                container: None,
                url: Some("file:///etc/passwd".into()),
                accept_empty_kg: false,
            },
            EndpointAction::Adopt { service: "ollama".into(), container: None, url: Some("http://a b".into()), accept_empty_kg: false },
            EndpointAction::UseVcoCopy { service: "weaviate".into(), port: Some(80), accept_empty_kg: false },
            EndpointAction::UseVcoCopy { service: "postgres".into(), port: None, accept_empty_kg: false },
            EndpointAction::HandToVco { service: "code_embed".into() },
        ];
        for a in refused {
            assert!(endpoint_action_argv(&a).is_err(), "{:?}", a);
        }
    }

    /// The FE sends `{ action: "adopt", service, container }` — the tagged
    /// shape the Tauri command deserializes.
    #[test]
    fn endpoint_action_wire_shape() {
        let a: EndpointAction =
            serde_json::from_value(serde_json::json!({"action": "use_vco_copy", "service": "weaviate"})).unwrap();
        assert_eq!(a, EndpointAction::UseVcoCopy { service: "weaviate".into(), port: None, accept_empty_kg: false });
    }

    // ---- boot ----------------------------------------------------------

    /// Boot asks only about undecided endpoints: a missing row, or the
    /// Weaviate confirmation-pending row. An unattended Ollama adoption, a
    /// VCO-data Weaviate adopted as ours, and a CPU host's disabled
    /// code-embed are not questions. Red if the gate stops reading the rows.
    #[test]
    fn boot_asks_only_when_an_endpoint_is_undecided() {
        let code_embed = row("code_embed", EndpointMode::VcoManaged, 11440);
        let rows = |w: Option<ServiceEndpointRow>, o: Option<ServiceEndpointRow>, c: Option<ServiceEndpointRow>| {
            vec![(CoreService::Weaviate, w), (CoreService::Ollama, o), (CoreService::CodeEmbed, c)]
        };
        let mut pending_w = row("weaviate", EndpointMode::VcoManaged, 8081);
        pending_w.enabled = false;
        assert_eq!(
            pending_choices(&rows(Some(pending_w), Some(row("ollama", EndpointMode::AdoptedExternal, 11434)), Some(code_embed.clone()))),
            vec!["weaviate"]
        );
        let mut ours = row("weaviate", EndpointMode::AdoptedContainer, 8081);
        ours.container_name = Some("vco_weaviate".into());
        let mut cpu = code_embed.clone();
        cpu.enabled = false;
        assert!(pending_choices(&rows(
            Some(ours.clone()),
            Some(row("ollama", EndpointMode::AdoptedExternal, 11434)),
            Some(cpu)
        ))
        .is_empty());
        assert_eq!(pending_choices(&rows(Some(ours), None, Some(code_embed))), vec!["ollama"]);
    }

    #[test]
    fn the_choice_event_carries_pending_rows_and_detection() {
        let rows = vec![(CoreService::Weaviate, None), (CoreService::Ollama, Some(row("ollama", EndpointMode::AdoptedExternal, 11434)))];
        let p = choice_event_payload(&["weaviate"], &rows, serde_json::json!({"schema": 1, "candidates": {}}));
        assert_eq!(p["pending"], serde_json::json!(["weaviate"]));
        assert!(p["rows"]["weaviate"].is_null());
        assert_eq!(p["rows"]["ollama"]["mode"], "adopted_external");
        assert_eq!(p["detection"]["schema"], 1);
    }

    // ---- v0.2.62 BLOCKER-1: watchdog pause-marker PRODUCER -------------

    use vct_launcher_core::services::watchdog_pause::is_service_paused;

    /// A VCO-managed service (no row = VCO's own) gets a marker on stop and
    /// loses it on start.
    #[test]
    #[serial_test::serial]
    fn pause_marker_produced_for_a_managed_service_and_cleared_on_start() {
        let _g = vct_launcher_core::test_env::state_dir_guard();
        assert!(!is_service_paused("weaviate"));
        set_pause_marker_for_service("weaviate", true);
        assert!(is_service_paused("weaviate"));
        set_pause_marker_for_service("weaviate", false);
        assert!(!is_service_paused("weaviate"));
    }

    /// An adopted service is never supervised, so it gets no marker.
    #[test]
    #[serial_test::serial]
    fn pause_marker_not_produced_for_an_adopted_service() {
        let _g = vct_launcher_core::test_env::state_dir_guard();
        let db = crate::db::Db::open().unwrap();
        db.service_endpoint_seed_for_tests(&row("ollama", EndpointMode::AdoptedExternal, 11434)).unwrap();
        set_pause_marker_for_service("ollama", true);
        assert!(!is_service_paused("ollama"));
    }

    #[test]
    #[serial_test::serial]
    fn pause_markers_round_trip() {
        let _g = vct_launcher_core::test_env::state_dir_guard();
        let all = ["weaviate", "ollama", "code_embed"];
        set_pause_markers(&all, true);
        assert!(all.iter().all(|s| is_service_paused(s)));
        set_pause_markers(&all, false);
        assert!(all.iter().all(|s| !is_service_paused(s)));
    }

    #[test]
    fn watcher_classifies_transitions_correctly() {
        use crate::services::watcher::{classify_transition, WatcherTransition};
        assert!(matches!(classify_transition(Some(true), true), WatcherTransition::Stable));
        assert!(matches!(classify_transition(Some(false), false), WatcherTransition::Stable));
        assert!(matches!(classify_transition(Some(true), false), WatcherTransition::Stopped));
        assert!(matches!(classify_transition(Some(false), true), WatcherTransition::Recovered));
        assert!(matches!(classify_transition(None, false), WatcherTransition::ColdStart));
        assert!(matches!(classify_transition(None, true), WatcherTransition::Stable));
    }
}
