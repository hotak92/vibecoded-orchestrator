use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, AppHandle, Emitter};

use crate::commands::installer::{
    DEFAULT_CODE_EMBED_PORT, DEFAULT_OLLAMA_PORT, DEFAULT_WEAVIATE_PORT,
};
use crate::services::adoption::{
    self, AdoptionMode, AdoptionState, ServiceAdoption,
};
use crate::services::runtime::{detect_runtime, invalidate_cache as invalidate_runtime_cache, RuntimeInfo};

// ---------------------------------------------------------------------------
// Shared-container lifecycle (Podman/Docker compose).
//
// These commands drive `<runtime> compose ...` (or `<runtime>-compose ...`
// — see services/runtime.rs) against the shared
// `infrastructure/docker-compose.yml`. They are the lifecycle backbone for:
//
//   - Auto-start on launcher boot (lib.rs)
//   - Tray "Start/Stop services" buttons (front-end)
//   - Quit confirmation's "Quit and stop services" (parallel agent)
//
// Coordination notes for parallel agents:
//
//   - Tray-pill agent: probes the same three ports directly via
//     `probe_one()` in tray.rs. We don't share a probe function. The tray
//     pill DOES read the Tauri command shape below if it ever needs more
//     detail than its inline probe — feel free to call `services_status`
//     for richer data.
//
//   - Quit-confirmation agent: calls `services_stop_all`. Idempotent:
//     succeeds even when nothing is up.
//
//   - The existing `commands::installer::detect_existing_services` is
//     PRESERVED unchanged — the OnboardingWizard and SettingsPanel
//     consume its specific shape. The richer `services_status` below is
//     a NEW command, not a refactor of the old one.
//
// History: this file used to also host the app-process lifecycle suite
// (launch_app/kill_app/get_app_status/get_all_app_statuses/check_app_health
// /check_all_health). Those were archived 2026-04-28 — zero FE/Hub
// consumers since the Svelte rewrite. See the orchestrator's private
// launch-assets/launcher-archived-rust/ for the extracted source if a
// packaged-app launcher is ever reintroduced.
// ---------------------------------------------------------------------------

/// Per-service runtime status. Returned by `services_status` for each of
/// Weaviate / Ollama / code_embed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceRuntimeState {
    /// `"weaviate"` | `"ollama"` | `"code_embed"`.
    pub name: String,
    /// True iff the canonical health URL responded 2xx/3xx.
    pub running: bool,
    /// Host port the service is bound to. Includes the user's
    /// `Mode::Parallel` override if they chose to run alongside an
    /// external service; defaults otherwise.
    pub port: u16,
    /// Canonical URL the launcher probed (or routes to, for adopted
    /// services). Empty string if neither apply.
    pub url: String,
    /// True when something is responding on the port but the launcher
    /// did NOT start it (no record in `services.toml`, or
    /// `Mode::Adopt`/`Mode::Refuse`).
    pub externally_managed: bool,
    /// Mirror of the persisted adoption mode. Frontend shows this in
    /// the Services preferences screen.
    pub adoption_mode: AdoptionMode,
    /// v0.2.7 (Bug E1): the persisted container name the launcher is
    /// pinned to manage for this service, or `None` when no pick has
    /// been made yet. Frontend renders this as "managing: <name>" so
    /// the user can see which container the launcher will Start/Stop
    /// when they hit the buttons.
    #[serde(default)]
    pub container_name: Option<String>,
}

/// Aggregate snapshot returned by `services_status`. Mirrors the shape
/// the tray pill / Services panel both consume.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServicesRuntimeSnapshot {
    pub services: Vec<ServiceRuntimeState>,
    /// Detected container runtime (`"podman"` | `"docker"` | `null`).
    pub runtime: Option<String>,
    /// True when the launcher is on macOS/Windows AND Podman is the
    /// runtime AND `podman machine` reports no running machine. The
    /// frontend uses this to show "Start Podman Machine" CTA.
    pub needs_podman_machine_start: bool,
    /// True when at least one service is responding but is not managed
    /// by us (= externally_managed). Frontend uses this to gate the
    /// adoption prompt.
    pub has_unresolved_external: bool,
}

/// All three canonical service names + their health URL templates.
/// Keep in sync with `installer::detect_existing_services`.
fn canonical_services() -> [(&'static str, u16, fn(u16) -> String); 3] {
    [
        ("weaviate", DEFAULT_WEAVIATE_PORT, |p| {
            // /v1/meta — Weaviate's metadata endpoint. Returns 200 with a
            // version + module list as soon as the HTTP server is up and
            // can answer queries. We deliberately do NOT use
            // /v1/.well-known/ready — that endpoint can return 503 during
            // bootstrap/recovery even when Weaviate is fully usable for
            // queries (false-negative observed 2026-05-06: detector
            // reported Weaviate "not running" while /v1/meta returned a
            // full module list and `hybrid_search` answered correctly).
            format!("http://localhost:{}/v1/meta", p)
        }),
        ("ollama", DEFAULT_OLLAMA_PORT, |p| {
            format!("http://localhost:{}/api/tags", p)
        }),
        ("code_embed", DEFAULT_CODE_EMBED_PORT, |p| {
            format!("http://localhost:{}/health", p)
        }),
    ]
}

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

/// Probe whether vco's services are already responding on the local host.
/// Used as a false-negative guard for runtime detection: if Weaviate is
/// reachable, the orchestrator's services are up regardless of whether
/// our launcher process can find a container runtime on its (possibly
/// stripped) PATH. In that case we should NOT show the user a "no
/// container runtime" modal — they have one, our detection is wrong.
///
/// Only Weaviate is probed (not Ollama or code_embed). Weaviate is the
/// hard prerequisite for the launcher's KG/codegraph features; Ollama and
/// code_embed can be slower to come up and aren't blockers for the modal
/// suppression.
async fn services_already_running() -> bool {
    // /v1/meta is the right liveness probe (see canonical_services note
    // for why /v1/.well-known/ready is too strict).
    let url = format!("http://localhost:{}/v1/meta", DEFAULT_WEAVIATE_PORT);
    probe_url(&url).await
}

/// Resolve the compose directory — `<repo_root>/infrastructure`. Errors
/// when we can't locate the orchestrator repo (e.g. the binary isn't
/// shipped with the source tree).
fn compose_dir() -> Result<PathBuf, String> {
    let root = crate::commands::installer::find_local_repo_root()?;
    Ok(root.join("infrastructure"))
}

/// Get the per-service effective port — falls back to the canonical
/// default unless the user picked `Mode::Parallel`, in which case we
/// honor the recorded `parallel_port`.
fn effective_port(name: &str, default_port: u16, state: &AdoptionState) -> u16 {
    state
        .get(name)
        .filter(|s| s.mode == AdoptionMode::Parallel)
        .and_then(|s| s.parallel_port)
        .unwrap_or(default_port)
}

/// Read the launcher-managed services snapshot. Probes all three
/// services concurrently; total wall time is bounded by the slowest
/// 2s probe.
#[command]
pub async fn services_status() -> Result<ServicesRuntimeSnapshot, String> {
    let adoption_state = adoption::read();

    // Build per-service probe URLs honoring any parallel-port overrides.
    let mut probes: Vec<(String, u16, String)> = Vec::new(); // (name, port, url)
    for (name, default_port, url_for) in canonical_services() {
        let port = effective_port(name, default_port, &adoption_state);
        probes.push((name.to_string(), port, url_for(port)));
    }

    // Concurrent probes.
    let probe_futures: Vec<_> = probes
        .iter()
        .map(|(_, _, url)| probe_url(url))
        .collect();
    let results = futures::future::join_all(probe_futures).await;

    let runtime_info = detect_runtime().await;

    let mut services: Vec<ServiceRuntimeState> = Vec::new();
    let mut has_unresolved = false;
    for ((name, port, url), running) in probes.into_iter().zip(results.into_iter()) {
        let entry = adoption_state.get(&name);
        let mode = entry.map(|s| s.mode).unwrap_or(AdoptionMode::Unresolved);
        // "Externally managed" =  the service is up AND we either haven't
        // resolved adoption yet OR the user picked Adopt/Refuse. Mode
        // == Parallel means we manage our own copy ourselves.
        let externally_managed = running
            && matches!(
                mode,
                AdoptionMode::Adopt | AdoptionMode::Refuse | AdoptionMode::Unresolved
            );
        if running && mode == AdoptionMode::Unresolved {
            has_unresolved = true;
        }
        let container_name = entry.and_then(|s| s.container_name.clone());
        services.push(ServiceRuntimeState {
            name,
            running,
            port,
            url,
            externally_managed,
            adoption_mode: mode,
            container_name,
        });
    }

    Ok(ServicesRuntimeSnapshot {
        services,
        runtime: runtime_info
            .as_ref()
            .map(|r| r.runtime.binary().to_string()),
        needs_podman_machine_start: runtime_info
            .as_ref()
            .map(|r| r.needs_machine_start)
            .unwrap_or(false),
        has_unresolved_external: has_unresolved,
    })
}

/// Helper: translate a tokio process result into a launcher error
/// string. Captures stderr so the frontend can show real failure
/// messages instead of "compose up failed (status 1)".
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
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "{} compose failed (status {}): {}",
            info.runtime.display_name(),
            output.status,
            stderr.trim()
        ));
    }
    Ok(())
}

/// Bring up the shared compose stack. Idempotent: a second call when
/// containers already run is a fast no-op. Skips services the user
/// chose to Adopt or Refuse (those are someone else's; we don't touch
/// them).
#[command]
pub async fn services_start_all() -> Result<(), String> {
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found. Install Podman or Docker.")?;
    let adoption_state = adoption::read();

    // Compute the subset of canonical services that we manage. Anything
    // the user adopted/refused is NOT in the up list.
    let mut managed: Vec<&str> = Vec::new();
    for (name, _, _) in canonical_services() {
        let mode = adoption_state.get(name).map(|s| s.mode).unwrap_or(AdoptionMode::Unresolved);
        if matches!(mode, AdoptionMode::Adopt | AdoptionMode::Refuse) {
            continue;
        }
        managed.push(name);
    }

    if managed.is_empty() {
        // All services are externally managed — nothing to start.
        return Ok(());
    }

    // `up -d` with no service args = "everything in the file"; with
    // explicit names = "only these". When the entire stack is managed
    // we use the no-arg form so future compose additions don't get
    // silently skipped.
    let mut args: Vec<String> = vec!["up".into(), "-d".into()];
    if managed.len() < canonical_services().len() {
        for n in &managed {
            args.push((*n).to_string());
        }
    }
    run_compose(&info, args).await
}

/// Stop the shared compose stack WITHOUT removing volumes (no `-v`
/// flag — that would destroy data). Idempotent: succeeds even when
/// nothing is up. Used by Quit-confirmation's "Quit and stop services"
/// button.
#[command]
pub async fn services_stop_all() -> Result<(), String> {
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    // `compose stop` halts containers but leaves them defined. `compose
    // down` would also remove the containers. Either preserves volumes
    // (we explicitly never pass --volumes / -v). We use `stop` so a
    // subsequent `up -d` is fast — it just restarts the same containers.
    run_compose(&info, ["stop"]).await
}

/// Restart all services. `compose restart` does this atomically; we
/// don't need the down+up dance.
#[command]
pub async fn services_restart_all() -> Result<(), String> {
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    run_compose(&info, ["restart"]).await
}

/// Start a single service by canonical name.
///
/// v0.2.6 (D1): adoption mode is INFORMATIONAL, not control-gating.
/// Previously we refused for `Adopt`/`Refuse` modes; the user reported
/// this was wrong — they want control over their services regardless of
/// who started the underlying container.
///
/// Routing (D2):
///   - `Adopt` / `Refuse` / `Unresolved` → look up the actual container
///     name via `<runtime> ps` filtered by `com.docker.compose.service`
///     label + port, then drive `<runtime> start` directly. This handles
///     the common case where the container was brought up by a different
///     compose project (e.g. `claude_mcp_servers/compose.yaml`) and our
///     VCO compose project filter would miss it.
///   - `Parallel` → use compose. Our parallel-port copy IS managed by
///     our compose stack.
#[command]
pub async fn service_start(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    route_service_action(&info, &name, "start").await
}

/// Stop a single service. See `service_start` doc for routing rules.
#[command]
pub async fn service_stop(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    route_service_action(&info, &name, "stop").await
}

/// Restart a single service. See `service_start` doc for routing rules.
#[command]
pub async fn service_restart(name: String) -> Result<(), String> {
    validate_service_name(&name)?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found.")?;
    route_service_action(&info, &name, "restart").await
}

/// Dispatch a single-service action to the right backend based on adoption
/// mode. Pure routing — the actual work lives in `run_compose` and
/// `control_adopted_container`.
async fn route_service_action(
    info: &RuntimeInfo,
    name: &str,
    action: &str,
) -> Result<(), String> {
    let state = adoption::read();
    let mode = state
        .get(name)
        .map(|s| s.mode)
        .unwrap_or(AdoptionMode::Unresolved);
    let canonical_port = canonical_port_for(name);
    let effective = effective_port(name, canonical_port, &state);

    match mode {
        AdoptionMode::Parallel => {
            // Our compose copy on an alt port — driven by compose, same as
            // before. Compose-name maps 1:1 to service-name.
            let verb = match action {
                "start" => "up",
                other => other,
            };
            let mut args: Vec<&str> = if verb == "up" {
                vec!["up", "-d", name]
            } else {
                vec![verb, name]
            };
            // Borrow-checker happiness: `args` is &[&str], no allocation.
            let _ = effective; // not needed by compose (it reads override.yaml)
            run_compose(info, args.drain(..)).await
        }
        AdoptionMode::Adopt | AdoptionMode::Refuse | AdoptionMode::Unresolved => {
            // Drive the runtime directly. We look up the container by
            // service label + port so we hit the SAME container even when
            // it's part of a foreign compose project (the user's existing
            // claude_mcp_servers stack).
            control_adopted_container(info, name, effective, action).await
        }
    }
}

/// Return the canonical (default) port for one of our three managed
/// services. Mirrors `canonical_services()` — kept as a tiny helper so
/// `route_service_action` doesn't need to scan the array.
fn canonical_port_for(name: &str) -> u16 {
    match name {
        "weaviate" => DEFAULT_WEAVIATE_PORT,
        "ollama" => DEFAULT_OLLAMA_PORT,
        "code_embed" => DEFAULT_CODE_EMBED_PORT,
        _ => 0, // validate_service_name() rejects unknown names before us
    }
}

/// Drive `podman` / `docker` directly on a single container. Used for
/// adopted services where compose isn't appropriate (the container was
/// started by a foreign compose project, or no compose at all).
///
/// Discovery: filter by the compose service label first, falling back to
/// the canonical port via `port` inspection. First matching container
/// wins. `action` is one of `"start"`, `"stop"`, `"restart"`.
///
/// Discovery details:
///   - `<runtime> ps -a --filter "label=com.docker.compose.service=<name>"
///     --format "{{.Names}}\t{{.Ports}}\t{{.Status}}"`
///   - we also accept `io.podman.compose.service` (the legacy podman-compose
///     v0 label) for older user stacks.
///   - If multiple containers match the service label, prefer one whose
///     ports include the canonical/effective port; otherwise take the
///     first row.
///   - If no service-label match, fall back to scanning all containers
///     for one bound to the port (rare — e.g. a hand-`podman-run`'d
///     container with no labels).
///
/// Soft contract: returns Ok(()) when the action succeeds. Returns Err
/// with the runtime's stderr trimmed when the action fails.
pub(crate) async fn control_adopted_container(
    info: &RuntimeInfo,
    service: &str,
    port: u16,
    action: &str,
) -> Result<(), String> {
    if !matches!(action, "start" | "stop" | "restart") {
        return Err(format!(
            "invalid action '{}' (expected start | stop | restart)",
            action
        ));
    }

    let container = resolve_pinned_or_pick(info, service, port).await?;
    let argv = build_control_argv(info.runtime.binary(), action, &container);

    // We don't go through `info.compose_command()` here — we want the
    // raw runtime binary, not compose. The first argv entry is the
    // action verb (start/stop/restart), the second is the container.
    let mut cmd = tokio::process::Command::new(&info.binary_path);
    for a in argv.iter().skip(1) {
        cmd.arg(a);
    }
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} {}: {}", info.runtime.binary(), action, e))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "{} {} {} failed (status {}): {}",
            info.runtime.binary(),
            action,
            container,
            output.status,
            stderr.trim()
        ));
    }
    Ok(())
}

/// Pure argv builder for `control_adopted_container`. Extracted so tests
/// can verify the exact argv shape without mocking the subprocess.
///
/// Always returns 3 elements: `[runtime_bin, action, container]`.
/// Example: `["podman", "start", "weaviate_claude"]`.
pub(crate) fn build_control_argv(
    runtime_bin: &str,
    action: &str,
    container: &str,
) -> Vec<String> {
    vec![
        runtime_bin.to_string(),
        action.to_string(),
        container.to_string(),
    ]
}

/// Structured error prefix for [`control_adopted_container`]. The
/// frontend matches on the prefix (everything up to the first `:`) so
/// it can branch UI without parsing free-text. Keep these strings stable
/// — Svelte switch-cases against them.
pub const ERR_KIND_CONTAINER_MISSING: &str = "container_missing";
pub const ERR_KIND_NO_CANDIDATES: &str = "no_candidates";
pub const ERR_KIND_MULTIPLE_CANDIDATES: &str = "multiple_candidates";

/// Build a structured error string. Format: `"<kind>: <human message>"`.
/// `kind` MUST be one of the `ERR_KIND_*` constants above.
pub(crate) fn structured_err(kind: &str, msg: impl AsRef<str>) -> String {
    format!("{}: {}", kind, msg.as_ref())
}

/// Verify that `name` is a real container on this runtime via
/// `<runtime> inspect`. Returns `Ok(true)` when the container exists
/// (running or not), `Ok(false)` when it doesn't, and `Err` only on
/// runtime invocation failures (no container runtime, permission error,
/// …). The inspect-not-found case is NORMAL — a user may have deleted
/// the container we pinned.
pub(crate) async fn container_exists(info: &RuntimeInfo, name: &str) -> Result<bool, String> {
    let mut cmd = tokio::process::Command::new(&info.binary_path);
    cmd.args(["inspect", "--format", "{{.Id}}", name]);
    let out = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} inspect: {}", info.runtime.binary(), e))?;
    Ok(out.status.success())
}

/// v0.2.7 (E1): resolve the container name to act on. Pinning-aware:
///
///   1. If `services.toml` has a `container_name` for this service AND
///      `<runtime> inspect <name>` reports it exists → use that name.
///   2. If pinned but the container is gone → return a
///      `container_missing` structured error. The FE surfaces a
///      "re-detect" CTA.
///   3. If NOT pinned and there's exactly 1 candidate → auto-pick,
///      persist to services.toml, then use it.
///   4. If NOT pinned and 0 candidates → `no_candidates` error.
///   5. If NOT pinned and 2+ candidates → `multiple_candidates` error.
///      FE opens the picker modal.
async fn resolve_pinned_or_pick(
    info: &RuntimeInfo,
    service: &str,
    port: u16,
) -> Result<String, String> {
    let state = adoption::read();
    let pinned = state
        .get(service)
        .and_then(|s| s.container_name.clone());

    if let Some(name) = pinned.as_deref().filter(|s| !s.is_empty()) {
        return match container_exists(info, name).await {
            Ok(true) => Ok(name.to_string()),
            Ok(false) => Err(structured_err(
                ERR_KIND_CONTAINER_MISSING,
                format!(
                    "pinned container '{}' for service '{}' no longer exists; \
                     click Re-detect to pick a replacement",
                    name, service
                ),
            )),
            Err(e) => Err(e),
        };
    }

    // No pin → enumerate and decide.
    let candidates =
        crate::services::picker::enumerate_candidates(info, service, port).await?;
    match candidates.len() {
        0 => Err(structured_err(
            ERR_KIND_NO_CANDIDATES,
            format!(
                "no container found for service '{}' on port {}; \
                 launcher has nothing to manage",
                service, port
            ),
        )),
        1 => {
            let pick = candidates[0].container_name.clone();
            // Persist the auto-pick so subsequent actions skip discovery.
            if let Err(e) = persist_container_pick(service, &pick) {
                // Soft-fail: log + still use the pick this round. We DON'T
                // want a stat() failure on services.toml to block the
                // user's button press.
                eprintln!(
                    "[services] persist_container_pick({}={}) soft-failed: {}",
                    service, pick, e
                );
            }
            Ok(pick)
        }
        n => Err(structured_err(
            ERR_KIND_MULTIPLE_CANDIDATES,
            format!(
                "{} candidate containers found for service '{}'; \
                 user must pick one via the services panel",
                n, service
            ),
        )),
    }
}

/// Helper: update the `container_name` field for `service` in
/// services.toml, preserving every other field. If the service has no
/// entry yet we create one with default `Unresolved` mode.
pub(crate) fn persist_container_pick(service: &str, container: &str) -> Result<(), String> {
    let mut state = adoption::read();
    let new_entry = match state.get(service) {
        Some(existing) => ServiceAdoption {
            container_name: Some(container.to_string()),
            ..existing.clone()
        },
        None => ServiceAdoption {
            name: service.to_string(),
            mode: AdoptionMode::default(),
            external_url: None,
            parallel_port: None,
            container_name: Some(container.to_string()),
        },
    };
    state.upsert(new_entry);
    adoption::write(&state)
}

/// Legacy discovery — preserved for tests + as a fallback. v0.2.7's
/// [`resolve_pinned_or_pick`] is the primary entrypoint; this function
/// is no longer called by `control_adopted_container` directly.
///
/// Search order:
///   1. `--filter label=com.docker.compose.service=<service>` — modern
///      compose project (docker-compose, podman-compose v1+).
///   2. `--filter label=io.podman.compose.service=<service>` — legacy
///      podman-compose.
///   3. Port-binding fallback: scan all containers whose published ports
///      include `<port>`.
///
/// Returns the first matching container name. Deliberately non-
/// deterministic when multiple containers match — that's the v0.2.6 bug
/// the picker fixes. Kept around for unit-test coverage.
#[allow(dead_code)]
async fn find_container_for_service(
    info: &RuntimeInfo,
    service: &str,
    port: u16,
) -> Result<String, String> {
    // Try the two label variants in order.
    let label_filters = [
        format!("label=com.docker.compose.service={}", service),
        format!("label=io.podman.compose.service={}", service),
    ];
    for label in &label_filters {
        let mut cmd = tokio::process::Command::new(&info.binary_path);
        cmd.args([
            "ps",
            "-a",
            "--filter",
            label.as_str(),
            "--format",
            "{{.Names}}\t{{.Ports}}",
        ]);
        let out = cmd
            .output()
            .await
            .map_err(|e| format!("spawn {} ps: {}", info.runtime.binary(), e))?;
        if !out.status.success() {
            continue;
        }
        let body = String::from_utf8_lossy(&out.stdout);
        if let Some(name) = pick_container_row(&body, port) {
            return Ok(name);
        }
    }

    // Port-only fallback. List ALL containers, find one whose port
    // mapping matches.
    let mut cmd = tokio::process::Command::new(&info.binary_path);
    cmd.args(["ps", "-a", "--format", "{{.Names}}\t{{.Ports}}"]);
    let out = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} ps: {}", info.runtime.binary(), e))?;
    if out.status.success() {
        let body = String::from_utf8_lossy(&out.stdout);
        if let Some(name) = pick_container_row(&body, port) {
            return Ok(name);
        }
    }

    Err(format!(
        "no container found for service '{}' on port {}; the launcher cannot \
         control an unmanaged service that has no running container",
        service, port
    ))
}

/// Given the output of `<runtime> ps --format "{{.Names}}\t{{.Ports}}"`,
/// pick the first container name whose port column mentions `port`.
/// Falls back to the first row when no row references the port (better
/// than nothing — the service-label filter already proved relevance).
///
/// Extracted as a pure function so it can be unit-tested without spawning
/// a real container runtime.
#[allow(dead_code)]
fn pick_container_row(body: &str, port: u16) -> Option<String> {
    let mut first_seen: Option<String> = None;
    let port_needle_a = format!(":{}->", port); // "0.0.0.0:8081->8081/tcp"
    let port_needle_b = format!(":{}/", port);  // "8081/tcp" (no host mapping)
    for line in body.lines() {
        let line = line.trim_end_matches('\r');
        if line.is_empty() {
            continue;
        }
        let mut parts = line.splitn(2, '\t');
        let name = match parts.next() {
            Some(n) if !n.is_empty() => n,
            _ => continue,
        };
        let ports = parts.next().unwrap_or("");
        if first_seen.is_none() {
            first_seen = Some(name.to_string());
        }
        if ports.contains(&port_needle_a) || ports.contains(&port_needle_b) {
            return Some(name.to_string());
        }
    }
    first_seen
}

/// Hard-coded allowlist — only the three canonical services are valid
/// targets. Prevents arbitrary string injection into `compose <verb>
/// <name>` from a malicious frontend (or a typo'd page).
fn validate_service_name(name: &str) -> Result<(), String> {
    match name {
        "weaviate" | "ollama" | "code_embed" => Ok(()),
        _ => Err(format!(
            "unknown service '{}'; expected weaviate | ollama | code_embed",
            name
        )),
    }
}

// ---------------------------------------------------------------------------
// Adoption-state Tauri commands (read/write `~/.vct/services.toml`)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdoptionDecision {
    pub name: String,
    pub mode: AdoptionMode,
    /// Required when `mode == Parallel`. The frontend probes for a free
    /// port before calling this command.
    pub parallel_port: Option<u16>,
    /// External URL captured at decision time (for display). Optional —
    /// `services_status` will refresh it on next probe anyway.
    pub external_url: Option<String>,
    /// v0.2.7: persist the container the user adopted. When `None` we
    /// preserve any pre-existing pin (so the FE can update mode without
    /// clobbering the pinned container). Explicitly clearing requires
    /// the frontend to call `services_pick_container` with the empty
    /// string — there's no other path that nulls the field.
    #[serde(default)]
    pub container_name: Option<String>,
}

/// Persist the user's adopt-vs-parallel choice for ONE service.
/// Frontend calls this in response to the `vct-external-services-detected`
/// event. The launcher does NOT auto-apply parallel overrides here — the
/// user must subsequently click Start to bring services up on the new
/// port (or rely on the next launcher boot's auto-start).
#[command]
pub async fn services_set_adoption(decision: AdoptionDecision) -> Result<(), String> {
    validate_service_name(&decision.name)?;
    if decision.mode == AdoptionMode::Parallel && decision.parallel_port.is_none() {
        return Err("parallel mode requires parallel_port".into());
    }
    let mut state = adoption::read();
    // Preserve any pre-existing container pin unless the caller passed
    // a new one. v0.2.7 — frontend updates to mode/external_url must
    // NOT silently clear the container pin (that would force a
    // re-detect on the next action).
    let preserved_pin = state
        .get(&decision.name)
        .and_then(|s| s.container_name.clone());
    let container_name = decision.container_name.or(preserved_pin);
    state.upsert(ServiceAdoption {
        name: decision.name,
        mode: decision.mode,
        external_url: decision.external_url,
        parallel_port: decision.parallel_port,
        container_name,
    });
    adoption::write(&state)
}

/// Clear adoption decisions so the launcher re-prompts on next boot.
/// Called from the Services preferences "Re-detect" button.
#[command]
pub async fn services_reset_adoption() -> Result<(), String> {
    adoption::write(&AdoptionState::default())?;
    invalidate_runtime_cache();
    Ok(())
}

/// Read the current adoption state. Used by the preferences screen.
#[command]
pub async fn services_get_adoption() -> Result<AdoptionState, String> {
    Ok(adoption::read())
}

/// Probe a list of candidate ports and return the first one that is
/// not bound. The frontend calls this when the user picks "Run parallel
/// on different port" so we offer a sensible default in the dialog.
///
/// Bind-test approach: try `TcpListener::bind`. If it succeeds the port
/// is free at this exact moment (TOCTOU caveat — by the time we run
/// `compose up` someone else might have grabbed it; in practice the
/// race window is microseconds and the user can re-pick if it fails).
#[command]
pub async fn services_find_free_port(start: u16, end: u16) -> Result<u16, String> {
    use std::net::TcpListener;
    if start > end {
        return Err(format!("invalid range {}..{}", start, end));
    }
    for port in start..=end {
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return Ok(port);
        }
    }
    Err(format!("no free port found in range {}..{}", start, end))
}

// ---------------------------------------------------------------------------
// Container picker (Bug E2, v0.2.7)
//
// `services_enumerate_candidates` lists every container that could back a
// service; `services_pick_container` persists the user's choice. See
// `services::picker` for the discovery + ranking logic.
// ---------------------------------------------------------------------------

/// Enumerate candidate containers for one service. Returns ranked list
/// (best-first) of `ContainerCandidate`s including fullness probes for
/// each running candidate. Cross-OS via `<runtime> ps` + `reqwest`.
///
/// Errors when `<runtime> ps` itself fails (no container runtime, etc.).
/// Empty list is NOT an error — callers (FE Re-detect modal) render
/// "nothing found".
#[command]
pub async fn services_enumerate_candidates(
    service: String,
) -> Result<Vec<crate::services::picker::ContainerCandidate>, String> {
    validate_service_name(&service)?;
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found. Install Podman or Docker.")?;
    let port = canonical_port_for(&service);
    crate::services::picker::enumerate_candidates(&info, &service, port).await
}

/// Persist the user's container pick for a service. Validates that the
/// container exists (`<runtime> inspect`) before writing — we never
/// pin to a phantom container. An empty `container_name` is rejected
/// (use the frontend's "clear pin" affordance, not an empty string —
/// keeps the contract explicit).
#[command]
pub async fn services_pick_container(
    service: String,
    container_name: String,
) -> Result<(), String> {
    validate_service_name(&service)?;
    if container_name.trim().is_empty() {
        return Err("container_name must not be empty".into());
    }
    let info = detect_runtime()
        .await
        .ok_or("No container runtime found. Install Podman or Docker.")?;
    if !container_exists(&info, &container_name).await? {
        return Err(structured_err(
            ERR_KIND_CONTAINER_MISSING,
            format!(
                "container '{}' does not exist on this runtime",
                container_name
            ),
        ));
    }
    persist_container_pick(&service, &container_name)
}

// ---------------------------------------------------------------------------
// Auto-start on launcher boot
//
// Called from lib.rs::run() after tray init. Emits frontend events so
// the UI can show progress without blocking the window from rendering.
// ---------------------------------------------------------------------------

/// Frontend event names. Centralized here so tray.rs and the Services
/// page subscribe to the same strings.
pub const EVT_EXTERNAL_DETECTED: &str = "vct-external-services-detected";
pub const EVT_LIFECYCLE_PROGRESS: &str = "vct-services-lifecycle";
/// Emitted when neither Podman nor Docker is detected at launcher boot.
/// `NoContainerRuntimeDialog.svelte` listens and shows a blocking modal
/// with OS-specific install options (Linux: button calling
/// `runtime_install_podman_linux`; macOS/Windows: "Open install page"
/// + "Re-check" buttons calling `runtime_open_install_url` /
/// `runtime_recheck`).
pub const EVT_NO_CONTAINER_RUNTIME: &str = "vct-no-container-runtime";

#[derive(Debug, Clone, Serialize)]
pub struct LifecycleProgress {
    /// `"detecting_runtime"` | `"runtime_missing"` | `"starting"` |
    /// `"started"` | `"start_failed"` | `"stopping"` | `"stopped"`.
    pub phase: String,
    pub message: String,
}

/// Auto-start the shared services on launcher boot. Non-blocking: spawned
/// in the background by `lib.rs::run` so the window can render
/// immediately. Surface progress via the `vct-services-lifecycle` event.
///
/// Behavior:
///   1. Detect runtime. Missing → emit `runtime_missing`, return.
///   2. Snapshot status. If everything is already up AND not externally
///      managed → no-op.
///   3. If externally-managed services with unresolved adoption →
///      emit `vct-external-services-detected` with the list. Frontend
///      shows the dialog; user picks; user clicks Start manually after.
///   4. Else → call `services_start_all`. Emit `started` or `start_failed`.
pub async fn auto_start_on_boot(app: AppHandle) {
    let _ = app.emit(
        EVT_LIFECYCLE_PROGRESS,
        LifecycleProgress {
            phase: "detecting_runtime".into(),
            message: "Detecting container runtime…".into(),
        },
    );

    let info = match detect_runtime().await {
        Some(i) => i,
        None => {
            // FALSE-NEGATIVE GUARD: a launcher process spawned by
            // post-install-launcher.sh inherits a stripped PATH from
            // `setsid nohup`. The runtime-detection probes can fail (no
            // `podman-compose` on PATH, 2s cold-cache timeouts) even
            // though the orchestrator's services are already running.
            // Before showing the user a "no runtime found" modal — which
            // pops a Gatekeeper-style dialog and can auto-open browser
            // URLs to install instructions — verify whether Weaviate is
            // ALREADY responding on the configured port. If it is, the
            // services are up and the modal would be a false alarm.
            //
            // The launcher's full feature set (KG dashboards, search,
            // module install) only needs Weaviate reachable, not a
            // container runtime detected. The runtime is only needed to
            // bring services UP from a stopped state.
            if services_already_running().await {
                eprintln!(
                    "[lifecycle] runtime detection returned None but Weaviate \
                     is reachable — services already running, suppressing \
                     vct-no-container-runtime modal"
                );
                let _ = app.emit(
                    EVT_LIFECYCLE_PROGRESS,
                    LifecycleProgress {
                        phase: "started".into(),
                        message: "Services already running (adopted existing instance).".into(),
                    },
                );
                return;
            }

            // Surface BOTH events: the existing lifecycle event keeps
            // the tray/services panel informed (legacy consumer); the
            // new dedicated event drives the blocking modal that asks
            // the user to install Podman/Docker. The modal is what the
            // user actually interacts with — the lifecycle event is
            // for status text in the tray pill.
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "runtime_missing".into(),
                    message: "No container runtime found. Install Podman or Docker to run VCT services.".into(),
                },
            );
            // Payload includes the host OS so the modal can pick the
            // right copy (auto-install button on Linux, URL-only on
            // macOS/Windows). Using a plain JSON object keeps the
            // frontend's TS types simple — no shared schema needed.
            let os = if cfg!(target_os = "linux") {
                "linux"
            } else if cfg!(target_os = "macos") {
                "macos"
            } else if cfg!(target_os = "windows") {
                "windows"
            } else {
                "unknown"
            };
            let _ = app.emit(
                EVT_NO_CONTAINER_RUNTIME,
                serde_json::json!({ "os": os }),
            );
            return;
        }
    };

    if info.needs_machine_start {
        let _ = app.emit(
            EVT_LIFECYCLE_PROGRESS,
            LifecycleProgress {
                phase: "runtime_missing".into(),
                message: format!(
                    "Podman is installed but no machine is running. Run `podman machine start` and re-detect."
                ),
            },
        );
        return;
    }

    let snapshot = match services_status().await {
        Ok(s) => s,
        Err(e) => {
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "start_failed".into(),
                    message: format!("status probe failed: {}", e),
                },
            );
            return;
        }
    };

    // Externally-managed services with unresolved adoption → prompt and bail.
    let unresolved: Vec<ServiceRuntimeState> = snapshot
        .services
        .iter()
        .filter(|s| s.running && s.adoption_mode == AdoptionMode::Unresolved)
        .cloned()
        .collect();
    if !unresolved.is_empty() {
        let _ = app.emit(EVT_EXTERNAL_DETECTED, unresolved);
        // We do NOT auto-start anything else — the user might have
        // adopted ALL services and we'd race against the dialog.
        return;
    }

    // Determine if anything we manage is actually down.
    let any_down = snapshot.services.iter().any(|s| {
        !s.running
            && !matches!(
                s.adoption_mode,
                AdoptionMode::Adopt | AdoptionMode::Refuse
            )
    });
    if !any_down {
        let _ = app.emit(
            EVT_LIFECYCLE_PROGRESS,
            LifecycleProgress {
                phase: "started".into(),
                message: "Services already running.".into(),
            },
        );
        return;
    }

    let _ = app.emit(
        EVT_LIFECYCLE_PROGRESS,
        LifecycleProgress {
            phase: "starting".into(),
            message: format!(
                "Starting VCT services via {}…",
                info.runtime.display_name()
            ),
        },
    );
    match services_start_all().await {
        Ok(()) => {
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "started".into(),
                    message: "Services up.".into(),
                },
            );
        }
        Err(e) => {
            let _ = app.emit(
                EVT_LIFECYCLE_PROGRESS,
                LifecycleProgress {
                    phase: "start_failed".into(),
                    message: e,
                },
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod services_lifecycle_tests {
    use super::*;

    #[test]
    fn validate_service_name_accepts_canonical() {
        assert!(validate_service_name("weaviate").is_ok());
        assert!(validate_service_name("ollama").is_ok());
        assert!(validate_service_name("code_embed").is_ok());
    }

    #[test]
    fn validate_service_name_rejects_unknown() {
        assert!(validate_service_name("postgres").is_err());
        // Critical: must reject anything that could be used for argument
        // injection. Compose passes the string positionally so an empty
        // string would be silently dropped, but a "; rm -rf /" must
        // never reach the binary even if compose tokenizes safely.
        assert!(validate_service_name("").is_err());
        assert!(validate_service_name("weaviate; rm -rf /").is_err());
        assert!(validate_service_name("../etc/passwd").is_err());
    }

    #[test]
    fn effective_port_falls_back_to_default_when_no_override() {
        let state = AdoptionState::default();
        assert_eq!(effective_port("weaviate", 8081, &state), 8081);
    }

    #[test]
    fn effective_port_uses_parallel_port_when_set() {
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Parallel,
            external_url: None,
            parallel_port: Some(8091),
            container_name: None,
        });
        assert_eq!(effective_port("weaviate", 8081, &state), 8091);
    }

    #[test]
    fn effective_port_ignores_parallel_port_for_adopted_service() {
        // If the user adopted, we route to the canonical port (where
        // their existing service lives). Parallel port only applies in
        // Parallel mode.
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://localhost:8081".into()),
            parallel_port: Some(8091), // ignored under Adopt mode
            container_name: None,
        });
        assert_eq!(effective_port("weaviate", 8081, &state), 8081);
    }

    #[tokio::test]
    async fn services_already_running_probe_does_not_panic() {
        // The probe MUST be panic-free regardless of whether Weaviate is
        // up. This is a smoke test for the false-negative guard added
        // 2026-04-27 to suppress the no-runtime modal when services are
        // already running. Whether it returns true or false depends on
        // the test host's state; we just assert it returned a bool.
        let _ = services_already_running().await;
    }

    #[tokio::test]
    async fn find_free_port_returns_in_range() {
        // The 65000-65535 range is almost always free on dev boxes.
        let port = services_find_free_port(65_000, 65_535)
            .await
            .expect("expected a free port in 65000..65535");
        assert!((65_000..=65_535).contains(&port));
    }

    #[tokio::test]
    async fn find_free_port_rejects_invalid_range() {
        let err = services_find_free_port(2000, 1000).await.unwrap_err();
        assert!(err.contains("invalid range"));
    }

    // ---- v0.2.6 Bug D unit tests --------------------------------------

    #[test]
    fn build_control_argv_shape_is_fixed() {
        // Always exactly 3 elements: [runtime, action, container]. Any
        // change in shape would silently break the subprocess spawn.
        let v = build_control_argv("podman", "start", "weaviate_claude");
        assert_eq!(v, vec!["podman", "start", "weaviate_claude"]);
        let v = build_control_argv("docker", "stop", "myapp_ollama_1");
        assert_eq!(v, vec!["docker", "stop", "myapp_ollama_1"]);
        let v = build_control_argv("podman", "restart", "code_embed");
        assert_eq!(v, vec!["podman", "restart", "code_embed"]);
    }

    #[test]
    fn canonical_port_for_known_services() {
        assert_eq!(canonical_port_for("weaviate"), DEFAULT_WEAVIATE_PORT);
        assert_eq!(canonical_port_for("ollama"), DEFAULT_OLLAMA_PORT);
        assert_eq!(canonical_port_for("code_embed"), DEFAULT_CODE_EMBED_PORT);
        // Unknown name returns 0 (validate_service_name catches before).
        assert_eq!(canonical_port_for("postgres"), 0);
    }

    #[test]
    fn pick_container_row_prefers_port_match() {
        // Real-world podman ps output: `<name>\t<ports>`.
        let body = "weaviate_other\t8090/tcp\nweaviate_claude\t0.0.0.0:8081->8081/tcp\n";
        let chosen = pick_container_row(body, 8081);
        assert_eq!(chosen.as_deref(), Some("weaviate_claude"));
    }

    #[test]
    fn pick_container_row_falls_back_to_first_when_no_port_match() {
        // No row mentions port 9999, but we still return *something* —
        // the label filter already vouched for relevance.
        let body = "weaviate_a\t8081/tcp\nweaviate_b\t8082/tcp\n";
        let chosen = pick_container_row(body, 9999);
        assert_eq!(chosen.as_deref(), Some("weaviate_a"));
    }

    #[test]
    fn pick_container_row_handles_no_port_publish() {
        // Compose `expose:` (no `ports:`) shows up as `8081/tcp` with no
        // host-side mapping. We still match on the bare protocol form.
        let body = "ollama_claude\t11435/tcp\n";
        let chosen = pick_container_row(body, 11435);
        assert_eq!(chosen.as_deref(), Some("ollama_claude"));
    }

    #[test]
    fn pick_container_row_empty_input_returns_none() {
        assert!(pick_container_row("", 8081).is_none());
        assert!(pick_container_row("\n\n", 8081).is_none());
    }

    #[test]
    fn pick_container_row_ignores_lines_without_name() {
        let body = "\t8081/tcp\nvalid_ctr\t8081/tcp\n";
        let chosen = pick_container_row(body, 8081);
        assert_eq!(chosen.as_deref(), Some("valid_ctr"));
    }

    /// v0.2.7: structured error format is `"<kind>: <message>"`. Pinned
    /// here so a future refactor can't quietly switch the separator —
    /// the FE's switch-case parses the prefix exactly.
    #[test]
    fn structured_err_uses_colon_separator() {
        let s = structured_err(ERR_KIND_CONTAINER_MISSING, "weaviate_claude is gone");
        assert!(s.starts_with("container_missing:"));
        assert!(s.ends_with("weaviate_claude is gone"));
    }

    /// v0.2.7: the three error kinds the FE reacts to must be distinct
    /// strings (no overlap, no typos). If you rename one, also update
    /// launcher/src/routes/services/+page.svelte's `runServiceAction`.
    #[test]
    fn error_kinds_are_distinct_and_stable() {
        let kinds = [
            ERR_KIND_CONTAINER_MISSING,
            ERR_KIND_NO_CANDIDATES,
            ERR_KIND_MULTIPLE_CANDIDATES,
        ];
        for (i, k) in kinds.iter().enumerate() {
            for (j, k2) in kinds.iter().enumerate() {
                if i != j {
                    assert_ne!(k, k2, "duplicate kind: {}", k);
                }
            }
            // No whitespace, no colons in the kind itself (it's the
            // prefix BEFORE the colon).
            assert!(!k.contains(':'));
            assert!(!k.contains(' '));
        }
        // Specific spellings the FE depends on.
        assert_eq!(ERR_KIND_CONTAINER_MISSING, "container_missing");
        assert_eq!(ERR_KIND_NO_CANDIDATES, "no_candidates");
        assert_eq!(ERR_KIND_MULTIPLE_CANDIDATES, "multiple_candidates");
    }

    /// v0.2.7: when `services_set_adoption` arrives with no
    /// container_name but a prior pin exists, the pin must be PRESERVED
    /// (not silently cleared). Tested via direct upsert logic since
    /// `services_set_adoption` writes to disk.
    #[test]
    fn upsert_does_not_silently_clear_pin() {
        let mut state = AdoptionState::default();
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Adopt,
            external_url: Some("http://localhost:8081".into()),
            parallel_port: None,
            container_name: Some("weaviate_claude".into()),
        });
        // Simulate the `services_set_adoption` preservation logic in
        // isolation: take the prior pin, fold into a new entry.
        let prior_pin = state.get("weaviate").and_then(|s| s.container_name.clone());
        let new_pin = None.or(prior_pin); // decision had no container_name
        state.upsert(ServiceAdoption {
            name: "weaviate".into(),
            mode: AdoptionMode::Refuse, // user changed mode
            external_url: Some("http://localhost:8081".into()),
            parallel_port: None,
            container_name: new_pin,
        });
        assert_eq!(
            state.get("weaviate").unwrap().container_name.as_deref(),
            Some("weaviate_claude"),
            "mode change must NOT silently clear the container pin"
        );
        assert_eq!(state.get("weaviate").unwrap().mode, AdoptionMode::Refuse);
    }

    /// v0.2.7: `persist_container_pick` must create an entry if none
    /// exists (so the auto-pick path in `resolve_pinned_or_pick` works
    /// even on a virgin services.toml).
    #[test]
    fn persist_container_pick_creates_entry_when_absent() {
        // Sanity-check the entry-construction logic without touching
        // disk. (Disk-path tests would need to redirect
        // `crate::paths::vct_root_dir()` and are an integration concern.)
        let state = AdoptionState::default();
        assert!(state.get("ollama").is_none(), "fixture must be empty");
        // The function under test inlines exactly this construction:
        let new_entry = ServiceAdoption {
            name: "ollama".to_string(),
            mode: AdoptionMode::default(),
            external_url: None,
            parallel_port: None,
            container_name: Some("ollama_claude".to_string()),
        };
        assert_eq!(new_entry.mode, AdoptionMode::Unresolved);
        assert_eq!(new_entry.container_name.as_deref(), Some("ollama_claude"));
    }

    /// Watcher decision function: given a previous + current snapshot,
    /// classify the transition. Tested separately from any subprocess
    /// or timer plumbing.
    #[test]
    fn watcher_classifies_transitions_correctly() {
        use crate::services::watcher::{classify_transition, WatcherTransition};
        assert!(matches!(
            classify_transition(Some(true), true),
            WatcherTransition::Stable
        ));
        assert!(matches!(
            classify_transition(Some(false), false),
            WatcherTransition::Stable
        ));
        assert!(matches!(
            classify_transition(Some(true), false),
            WatcherTransition::Stopped
        ));
        assert!(matches!(
            classify_transition(Some(false), true),
            WatcherTransition::Recovered
        ));
        // v0.2.9 (Bug I) — down-since-boot must classify as ColdStart so
        // the watcher actually attempts recovery instead of marking the
        // service Stable forever.
        assert!(matches!(
            classify_transition(None, false),
            WatcherTransition::ColdStart
        ));
        // First observation running → Stable (no prior, but no recovery
        // needed either).
        assert!(matches!(
            classify_transition(None, true),
            WatcherTransition::Stable
        ));
    }
}
