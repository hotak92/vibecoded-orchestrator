// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The poller behind a module manifest's `runtime.health_check` (v0.2.97,
//! lane V).
//!
//! `docs/VCT_MODULE_MANIFEST_SPEC.md` §6 promised that a module declaring
//! `runtime.health_check` gets its status shown in the module dashboard.
//! Nothing polled the block for any module until this file. It lives in the
//! hub because the hub outlives the launcher GUI: the status is current when
//! the GUI opens, and every client reads the same answer.
//!
//! ## What is probed
//!
//! Every module that is active on this machine and declares a health check:
//! the bundled core modules (installed for every project), enabled global
//! installs, and enabled per-project installs (one probe per project — a
//! per-project container listens on that project's port). Targets are
//! rebuilt from the manifests + `launcher.db` every [`REFRESH_EVERY`], so an
//! install, uninstall or disable is followed without a hub restart.
//!
//! ## States — unknown is never down
//!
//! * `up` — the last probe got a 2xx.
//! * `down` — the last probe ran and failed (refused, timed out, non-2xx).
//! * `unknown` — nothing observed: not probed yet, a probe type the hub
//!   cannot run (`stdio_ping` — the MCP process belongs to the Claude Code
//!   session, not to VCO), no URL, an unresolved placeholder, or a URL this
//!   poller refuses to contact. `last_error` then says which.
//!
//! ## Safety
//!
//! * Only loopback hosts (`localhost`, `127.0.0.0/8`, `::1`) are contacted.
//!   The one exception is a container/service module whose URL names a
//!   container port it maps to the host on loopback: the probe goes to that
//!   loopback mapping instead. Anything else is refused with one log line
//!   (on the change, not every cycle) and shows as `unknown`.
//! * No redirects are followed (a loopback probe cannot be bounced
//!   off-host) and no proxy is used.
//! * Each probe is bounded by the manifest's `timeout_s` (clamped to
//!   [`MIN_TIMEOUT`]..[`MAX_TIMEOUT`]) and runs on its own task, so a slow
//!   module never delays another; a target is never probed twice at once.
//! * Idle cost: one sleep until the next due probe (or the next refresh);
//!   `interval_s` is clamped to [`MIN_INTERVAL`]..[`MAX_INTERVAL`].
//!
//! Results are in memory only and served on `GET /api/v1/modules/catalog`
//! (`health`, `project_health`) and `GET /api/v1/modules/{id}/status`.

use std::collections::{BTreeMap, HashMap};
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde::Serialize;

use vct_launcher_core::db::Db;
use vct_launcher_core::manifest::{ModuleManifest, PlaceholderCtx, RuntimeBlock};
use vct_launcher_core::services::container_runtime::{
    resolve_value, rl_placeholders, rl_placeholders_global, GLOBAL_RL_PORT,
};

use crate::modules_api::LauncherDbHandle;

/// Env switch: `VCT_HUB_MODULE_HEALTH=0` (or `false` / `no` / `off`, any
/// case — the hub's one opt-out parser, `infra_watchdog::parse_enabled`)
/// disables the poller (every status then stays `unknown`). Documented in
/// `docs/CONFIGURATION.md` (the vct-hub table).
pub const ENV_ENABLED: &str = "VCT_HUB_MODULE_HEALTH";
pub const MIN_INTERVAL: Duration = Duration::from_secs(5);
pub const MAX_INTERVAL: Duration = Duration::from_secs(3600);
pub const MIN_TIMEOUT: Duration = Duration::from_secs(1);
pub const MAX_TIMEOUT: Duration = Duration::from_secs(30);
/// How often the target list is rebuilt from the manifests and the DB.
pub const REFRESH_EVERY: Duration = Duration::from_secs(60);
/// Longest `last_error` kept (a probe error can quote a response body).
const MAX_ERROR_CHARS: usize = 300;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum HealthState {
    Up,
    Down,
    Unknown,
}

/// One module instance's health, as served to clients.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ModuleHealth {
    pub state: HealthState,
    /// RFC 3339 time of the last completed probe; `None` before the first.
    pub last_checked: Option<String>,
    /// Why the last probe failed, or why the module is not probed.
    pub last_error: Option<String>,
}

impl ModuleHealth {
    fn unknown(reason: Option<String>) -> Self {
        Self { state: HealthState::Unknown, last_checked: None, last_error: reason }
    }
}

/// A module instance: machine-wide (`project_id: None` — bundled or global
/// install) or one project's install.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TargetKey {
    pub module_id: String,
    pub project_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Probe {
    /// `GET url`, bounded by `timeout`.
    Http { url: String, timeout: Duration },
    /// Not probed; the reason is shown as `last_error` with state unknown.
    Unprobed { reason: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeTarget {
    pub key: TargetKey,
    pub probe: Probe,
    pub interval: Duration,
}

/// Which instance a manifest is being targeted for — decides the port
/// placeholders (`{RL_SERVER_PORT}`, `{project_slug}`) a URL may use.
#[derive(Debug, Clone, Copy)]
pub enum Instance<'a> {
    /// A bundled core module: no install row, no allocated port.
    Bundled,
    /// A global install (one container for every project).
    Global,
    /// One project's install; `rl_port` is the project's allocated port.
    Project { id: &'a str, slug: &'a str, rl_port: Option<u16> },
}

fn clamp(d: Duration, lo: Duration, hi: Duration) -> Duration {
    d.max(lo).min(hi)
}

// The one loopback rule (R7b F14) — this file carried a second copy with
// different rules until v0.2.97.
use vct_launcher_core::services::service_endpoints::is_loopback_host;

/// The core service whose port a health-check URL names through its §15
/// placeholder (`{code_embed_port}` …), if any.
fn core_service_named_in(raw_url: &str) -> Option<vct_launcher_core::services::service_endpoints::CoreService> {
    vct_launcher_core::manifest::SERVICE_PORT_PLACEHOLDERS
        .iter()
        .find(|(token, _)| raw_url.contains(token))
        .map(|(_, svc)| *svc)
}

/// R7b F6: a core-service check whose service is recorded on ANOTHER host
/// (an `adopted_external` row such as a LAN GPU box). Its port placeholder
/// resolves the port only, so probing `localhost:<port>` would report a
/// healthy remote service as down; the hub contacts only this machine, so the
/// honest answer is unknown, with the reason. `None` = probe as usual.
fn core_service_on_another_host(
    raw_url: &str,
    row_host: impl Fn(vct_launcher_core::services::service_endpoints::CoreService) -> Option<String>,
) -> Option<String> {
    let svc = core_service_named_in(raw_url)?;
    let host = row_host(svc)?;
    if is_loopback_host(&host) {
        return None;
    }
    Some(format!(
        "{} runs on another host ({host}); the hub checks only services on this machine",
        svc.name()
    ))
}

/// A port mapping's bind address reaches the host's loopback: unset (the
/// builder defaults it to 127.0.0.1), empty / unspecified (all interfaces),
/// or a loopback address.
fn bind_reaches_loopback(bind: Option<&str>) -> bool {
    match bind {
        None => true,
        Some(b) => {
            let b = b.trim();
            b.is_empty()
                || is_loopback_host(b)
                || b.parse::<IpAddr>().map(|ip| ip.is_unspecified()).unwrap_or(false)
        }
    }
}

/// The URL actually probed for a resolved health-check URL, or why it is
/// refused. See the module doc's "Safety".
pub fn checked_probe_url(
    resolved: &str,
    runtime: &RuntimeBlock,
    placeholders: &HashMap<String, String>,
) -> Result<String, String> {
    let mut url = reqwest::Url::parse(resolved)
        .map_err(|e| format!("health_check url is not a URL ({e})"))?;
    if !matches!(url.scheme(), "http" | "https") {
        return Err(format!("health_check url scheme '{}' is not http(s)", url.scheme()));
    }
    let host = url.host_str().unwrap_or("").to_string();
    if is_loopback_host(&host) {
        return Ok(url.to_string());
    }
    let is_container = matches!(runtime.r#type.as_str(), "container" | "service");
    let port = url.port_or_known_default();
    let mapped = runtime.ports.iter().find(|p| {
        Some(p.container) == port && bind_reaches_loopback(p.bind.as_deref())
    });
    match (is_container, mapped) {
        (true, Some(mapping)) => {
            let mut host_port = mapping.host.clone();
            for (token, value) in placeholders {
                host_port = host_port.replace(token, value);
            }
            let host_port: u16 = host_port.parse().map_err(|_| {
                format!("port mapping host '{}' does not resolve to a port", mapping.host)
            })?;
            url.set_host(Some("127.0.0.1"))
                .map_err(|e| format!("cannot rewrite health_check host ({e})"))?;
            url.set_port(Some(host_port))
                .map_err(|_| "cannot rewrite health_check port".to_string())?;
            Ok(url.to_string())
        }
        _ => Err(format!(
            "refused to probe non-loopback host '{host}' (only localhost, or a container \
             port the module maps to the host's loopback, is contacted)"
        )),
    }
}

/// The probe target for one module instance, `None` when the manifest
/// declares no health check (nothing to show).
pub fn target_for(manifest: &ModuleManifest, instance: Instance<'_>) -> Option<ProbeTarget> {
    let hc = manifest.runtime.health_check.as_ref()?;
    let key = TargetKey {
        module_id: manifest.id.clone(),
        project_id: match instance {
            Instance::Project { id, .. } => Some(id.to_string()),
            _ => None,
        },
    };
    let interval = clamp(Duration::from_secs(hc.interval_s), MIN_INTERVAL, MAX_INTERVAL);
    let unprobed = |reason: String| ProbeTarget {
        key: key.clone(),
        probe: Probe::Unprobed { reason },
        interval,
    };
    if hc.r#type != "http_get" {
        return Some(unprobed(format!(
            "health_check type '{}' is not probed by the hub (only http_get is; a stdio \
             MCP process belongs to the Claude Code session)",
            hc.r#type
        )));
    }
    let Some(raw) = hc.url.as_deref().filter(|u| !u.trim().is_empty()) else {
        return Some(unprobed("http_get health_check has no url".into()));
    };
    if let Some(reason) = core_service_on_another_host(raw, |svc| {
        vct_launcher_core::services::service_endpoints::machine_row_from_disk(svc).map(|r| r.host)
    }) {
        return Some(unprobed(reason));
    }
    let placeholders = match instance {
        Instance::Bundled => HashMap::new(),
        Instance::Global => rl_placeholders_global(GLOBAL_RL_PORT),
        Instance::Project { slug, rl_port: Some(port), .. } => rl_placeholders(port, slug),
        Instance::Project { slug, rl_port: None, .. } => {
            let mut m = HashMap::new();
            m.insert("{project_slug}".to_string(), slug.to_string());
            m
        }
    };
    let ctx = PlaceholderCtx::new(&manifest.id);
    let resolved = resolve_value(raw, &ctx, &placeholders);
    if resolved.contains('{') {
        return Some(unprobed(format!(
            "health_check url has an unresolved placeholder: {resolved}"
        )));
    }
    Some(match checked_probe_url(&resolved, &manifest.runtime, &placeholders) {
        Ok(url) => ProbeTarget {
            key,
            probe: Probe::Http {
                url,
                timeout: clamp(Duration::from_secs(hc.timeout_s), MIN_TIMEOUT, MAX_TIMEOUT),
            },
            interval,
        },
        Err(reason) => unprobed(reason),
    })
}

/// Every probe target on this machine: bundled manifests, enabled global
/// installs, enabled per-project installs. Soft-fails per source (a DB read
/// error drops that source, logged).
pub fn build_targets(
    manifests: &[(PathBuf, ModuleManifest)],
    vct_root: &Path,
    db: &Db,
) -> Vec<ProbeTarget> {
    let mut out: Vec<ProbeTarget> = Vec::new();
    let mut push = |t: Option<ProbeTarget>| {
        if let Some(t) = t {
            if !out.iter().any(|o| o.key == t.key) {
                out.push(t);
            }
        }
    };
    let by_id = |id: &str| manifests.iter().find(|(_, m)| m.id == id).map(|(_, m)| m);

    for (path, manifest) in manifests {
        if vct_launcher_core::bundled_manifests::is_bundled_manifest_path(vct_root, path) {
            push(target_for(manifest, Instance::Bundled));
        }
    }
    match db.list_global_module_installs() {
        Ok(rows) => {
            for row in rows.iter().filter(|r| r.enabled) {
                if let Some(m) = by_id(&row.module_id) {
                    push(target_for(m, Instance::Global));
                }
            }
        }
        Err(e) => tracing::warn!(error = %e, "[module_health] list global installs failed"),
    }
    match db.list_projects() {
        Ok(projects) => {
            for project in &projects {
                let rows = match db.list_module_installs_for_project(&project.id) {
                    Ok(rows) => rows,
                    Err(e) => {
                        tracing::warn!(error = %e, "[module_health] list project installs failed");
                        continue;
                    }
                };
                // The port the supervisor spawns this project's containers
                // with (`module_supervisor::ensure_project_rl_port` reads the
                // same row) — read, never allocated here.
                let rl_port = db.get_project_rl_port(&project.id).ok().flatten();
                for row in rows.iter().filter(|r| r.enabled) {
                    if let Some(m) = by_id(&row.module_id) {
                        push(target_for(
                            m,
                            Instance::Project { id: &project.id, slug: &project.slug, rl_port },
                        ));
                    }
                }
            }
        }
        Err(e) => tracing::warn!(error = %e, "[module_health] list projects failed"),
    }
    out
}

struct Entry {
    target: ProbeTarget,
    health: ModuleHealth,
    next_due: Instant,
    in_flight: bool,
}

/// The in-memory status table. Every method takes `now` explicitly, so the
/// scheduling is testable without real time.
#[derive(Default)]
pub struct HealthRegistry {
    entries: Mutex<HashMap<TargetKey, Entry>>,
}

impl HealthRegistry {
    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<TargetKey, Entry>> {
        self.entries.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// Replace the target set. A new or changed target starts `unknown` and
    /// is due now; an unchanged one keeps its status and schedule; a gone
    /// one is dropped. Returns the refusal reasons that are NEW (to log
    /// once, not every refresh).
    pub fn sync(&self, targets: Vec<ProbeTarget>, now: Instant) -> Vec<(TargetKey, String)> {
        let mut entries = self.lock();
        let mut newly_unprobed = Vec::new();
        entries.retain(|k, _| targets.iter().any(|t| &t.key == k));
        for target in targets {
            if entries.get(&target.key).is_some_and(|e| e.target == target) {
                continue;
            }
            let reason = match &target.probe {
                Probe::Unprobed { reason } => {
                    newly_unprobed.push((target.key.clone(), reason.clone()));
                    Some(reason.clone())
                }
                Probe::Http { .. } => None,
            };
            entries.insert(
                target.key.clone(),
                Entry { target, health: ModuleHealth::unknown(reason), next_due: now, in_flight: false },
            );
        }
        newly_unprobed
    }

    /// The HTTP targets due at `now` that are not already being probed;
    /// each is marked in flight until [`HealthRegistry::record`].
    pub fn take_due(&self, now: Instant) -> Vec<(TargetKey, String, Duration)> {
        let mut entries = self.lock();
        let mut due = Vec::new();
        for (key, entry) in entries.iter_mut() {
            if let Probe::Http { url, timeout } = &entry.target.probe {
                if !entry.in_flight && entry.next_due <= now {
                    entry.in_flight = true;
                    due.push((key.clone(), url.clone(), *timeout));
                }
            }
        }
        due
    }

    /// Store a probe result. A result for a target that was removed or
    /// changed while the probe ran is dropped.
    pub fn record(
        &self,
        key: &TargetKey,
        url: &str,
        result: Result<(), String>,
        now: Instant,
        checked_at: String,
    ) {
        let mut entries = self.lock();
        let Some(entry) = entries.get_mut(key) else { return };
        if !matches!(&entry.target.probe, Probe::Http { url: u, .. } if u == url) {
            return;
        }
        entry.in_flight = false;
        entry.next_due = now + entry.target.interval;
        entry.health = match result {
            Ok(()) => ModuleHealth { state: HealthState::Up, last_checked: Some(checked_at), last_error: None },
            Err(e) => ModuleHealth {
                state: HealthState::Down,
                last_checked: Some(checked_at),
                last_error: Some(e.chars().take(MAX_ERROR_CHARS).collect()),
            },
        };
    }

    /// When the loop next has work: the earliest due, not-in-flight probe.
    pub fn next_due(&self) -> Option<Instant> {
        self.lock()
            .values()
            .filter(|e| !e.in_flight && matches!(e.target.probe, Probe::Http { .. }))
            .map(|e| e.next_due)
            .min()
    }

    pub fn get(&self, module_id: &str, project_id: Option<&str>) -> Option<ModuleHealth> {
        let key = TargetKey {
            module_id: module_id.to_string(),
            project_id: project_id.map(str::to_string),
        };
        self.lock().get(&key).map(|e| e.health.clone())
    }

    /// Every per-project instance of `module_id`, keyed by project id.
    pub fn per_project(&self, module_id: &str) -> BTreeMap<String, ModuleHealth> {
        self.lock()
            .iter()
            .filter(|(k, _)| k.module_id == module_id)
            .filter_map(|(k, e)| k.project_id.clone().map(|p| (p, e.health.clone())))
            .collect()
    }
}

/// The hub's one registry. Empty (every lookup `None`) until the poller
/// has synced once, or forever when the poller is disabled.
pub fn registry() -> &'static Arc<HealthRegistry> {
    static REGISTRY: OnceLock<Arc<HealthRegistry>> = OnceLock::new();
    REGISTRY.get_or_init(|| Arc::new(HealthRegistry::default()))
}

/// The probe client: no redirects, no proxy. The per-probe timeout is set on
/// each request.
pub fn probe_client() -> Result<reqwest::Client, String> {
    vct_launcher_core::services::loopback_http::builder()
        .build()
        .map_err(|e| format!("health probe client: {e}"))
}

/// One `GET`: `Ok` on 2xx, `Err` naming the failure otherwise.
pub async fn probe_http(client: &reqwest::Client, url: &str, timeout: Duration) -> Result<(), String> {
    match client.get(url).timeout(timeout).send().await {
        Ok(resp) if resp.status().is_success() => Ok(()),
        Ok(resp) => Err(format!("HTTP {}", resp.status().as_u16())),
        Err(e) if e.is_timeout() => Err(format!("no answer within {}s", timeout.as_secs())),
        Err(e) if e.is_connect() => Err("connection refused or unreachable".to_string()),
        Err(e) => Err(format!("request failed: {e}")),
    }
}

/// Start every due probe on its own task and return the handles (the loop
/// drops them; a test awaits them).
pub fn dispatch_due(
    registry: &Arc<HealthRegistry>,
    client: &reqwest::Client,
    now: Instant,
) -> Vec<tokio::task::JoinHandle<()>> {
    registry
        .take_due(now)
        .into_iter()
        .map(|(key, url, timeout)| {
            let registry = Arc::clone(registry);
            let client = client.clone();
            tokio::spawn(async move {
                // The URL is part of a failure: it says WHERE nothing answered
                // (e.g. a service moved off the port its manifest names).
                let result = probe_http(&client, &url, timeout).await.map_err(|e| format!("{url}: {e}"));
                registry.record(&key, &url, result, Instant::now(), chrono::Utc::now().to_rfc3339());
            })
        })
        .collect()
}

fn refresh_targets(registry: &HealthRegistry, db: &Db, now: Instant) {
    let manifests = crate::modules_api::scan_manifests();
    let vct_root = vct_launcher_core::paths::vct_root_dir();
    let targets = build_targets(&manifests, &vct_root, db);
    for (key, reason) in registry.sync(targets, now) {
        tracing::info!(
            module_id = %key.module_id,
            project_id = key.project_id.as_deref().unwrap_or("-"),
            reason = %reason,
            "[module_health] not probing this module's health_check; its status stays unknown"
        );
    }
}

/// Start the poller (a detached task), unless [`ENV_ENABLED`] turns it off.
pub fn spawn_module_health_poller(db: LauncherDbHandle) {
    spawn_module_health_poller_with(db, std::env::var(ENV_ENABLED).ok().as_deref());
}

/// [`spawn_module_health_poller`] with the switch's raw value passed in, so a
/// test never touches the process env. Returns whether the poller started.
pub fn spawn_module_health_poller_with(db: LauncherDbHandle, switch: Option<&str>) -> bool {
    if !crate::infra_watchdog::parse_enabled(switch) {
        tracing::info!(
            "[vct-hub] module health poller DISABLED via {ENV_ENABLED}={}; every module's \
             health shows as unknown.",
            switch.unwrap_or_default()
        );
        return false;
    }
    let client = match probe_client() {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "[vct-hub] module health poller not started");
            return false;
        }
    };
    tokio::spawn(async move {
        let registry = Arc::clone(registry());
        let mut next_refresh = Instant::now();
        loop {
            let now = Instant::now();
            if now >= next_refresh {
                refresh_targets(&registry, &db.0, now);
                next_refresh = now + REFRESH_EVERY;
            }
            drop(dispatch_due(&registry, &client, now));
            let wake = registry.next_due().map_or(next_refresh, |d| d.min(next_refresh));
            let sleep = wake
                .saturating_duration_since(Instant::now())
                .clamp(Duration::from_millis(200), REFRESH_EVERY);
            tokio::time::sleep(sleep).await;
        }
    });
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    /// R7b F13: every opt-out spelling the hub's other switches accept turns
    /// the poller off — not only `0`.
    #[tokio::test]
    async fn the_health_switch_accepts_every_opt_out_spelling() {
        let db = || LauncherDbHandle(Arc::new(Db::open_in_memory().unwrap()));
        for off in ["0", "false", "FALSE", "no", " off "] {
            assert!(!spawn_module_health_poller_with(db(), Some(off)), "{off:?} must disable it");
        }
    }
    use axum::{http::StatusCode, routing::get, Router};

    fn manifest(runtime_type: &str, health_check: &str, ports: &str) -> ModuleManifest {
        let raw = format!(
            r#"{{
              "id": "vct-probe", "name": "Probe", "version": "1.0.0", "category": "core",
              "license": {{ "required": false }},
              "install": {{ "method": "local", "install_dir": "{{VCT_MODULES}}/vct-probe" }},
              "runtime": {{ "type": "{runtime_type}", "health_check": {health_check},
                            "ports": {ports} }}
            }}"#
        );
        ModuleManifest::from_json(&raw).expect("fixture parses")
    }

    fn http(url: &str) -> String {
        format!(r#"{{ "type": "http_get", "url": "{url}", "timeout_s": 2, "interval_s": 30 }}"#)
    }

    /// R7b F6: a core-service check whose service is recorded on another
    /// host reads unknown with that reason — never a `localhost` probe that
    /// would report a healthy remote service as down. A loopback row, a URL
    /// naming no core service, or no row at all: probed as usual. (Ollama and
    /// Weaviate can be adopted on another host; code-embed is always VCO's own
    /// — the `service_endpoints` CHECK — so for `{code_embed_port}` this only
    /// ever says "probe".)
    #[test]
    fn a_core_service_on_another_host_is_unknown_not_down() {
        use vct_launcher_core::services::service_endpoints::CoreService;
        let url = "http://localhost:{ollama_port}/api/tags";
        let on = |host: &'static str| move |svc: CoreService| {
            assert_eq!(svc, CoreService::Ollama);
            Some(host.to_string())
        };
        let reason = core_service_on_another_host(url, on("192.168.7.9")).expect("unknown");
        assert!(reason.contains("another host (192.168.7.9)"), "{reason}");
        assert_eq!(core_service_on_another_host(url, on("localhost")), None);
        assert_eq!(core_service_on_another_host(url, on("127.0.0.1")), None);
        assert_eq!(core_service_on_another_host(url, |_| None), None);
        assert_eq!(core_service_on_another_host("http://localhost:8080/h", on("192.168.7.9")), None);
    }

    /// The same through `target_for` and the real row reader: an
    /// `adopted_external` Ollama row on a LAN host makes a module whose check
    /// names `{ollama_port}` read unknown.
    #[test]
    fn target_for_reads_the_core_service_row_host() {
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let _g = vct_launcher_core::test_env::state_dir_guard();
        Db::open()
            .unwrap()
            .service_endpoint_seed_for_tests(&ServiceEndpointRow::new(
                "ollama",
                EndpointMode::AdoptedExternal,
                "192.168.7.9",
                11434,
            ))
            .unwrap();
        let m = manifest("service", &http("http://localhost:{ollama_port}/api/tags"), "[]");
        let t = target_for(&m, Instance::Bundled).expect("a target");
        match t.probe {
            Probe::Unprobed { reason } => assert!(reason.contains("192.168.7.9"), "{reason}"),
            other => panic!("expected unknown, got {other:?}"),
        }
    }

    /// A local server on an ephemeral loopback port: `/ok` 200, `/fail` 503,
    /// `/slow` answers after 3 s, `/redirect` 302 to `/ok`.
    async fn server() -> u16 {
        let app = Router::new()
            .route("/ok", get(|| async { "ok" }))
            .route("/fail", get(|| async { (StatusCode::SERVICE_UNAVAILABLE, "no") }))
            .route(
                "/slow",
                get(|| async {
                    tokio::time::sleep(Duration::from_secs(3)).await;
                    "late"
                }),
            )
            .route(
                "/redirect",
                get(|| async { (StatusCode::FOUND, [(axum::http::header::LOCATION, "/ok")]) }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        port
    }

    fn key(module: &str) -> TargetKey {
        TargetKey { module_id: module.into(), project_id: None }
    }

    fn http_target(module: &str, url: String, timeout_s: u64) -> ProbeTarget {
        ProbeTarget {
            key: key(module),
            probe: Probe::Http { url, timeout: Duration::from_secs(timeout_s) },
            interval: Duration::from_secs(30),
        }
    }

    #[tokio::test]
    async fn probe_reports_up_down_timeout_and_never_follows_a_redirect() {
        let port = server().await;
        let client = probe_client().unwrap();
        let base = format!("http://127.0.0.1:{port}");
        assert_eq!(probe_http(&client, &format!("{base}/ok"), Duration::from_secs(2)).await, Ok(()));
        assert_eq!(
            probe_http(&client, &format!("{base}/fail"), Duration::from_secs(2)).await,
            Err("HTTP 503".into())
        );
        assert_eq!(
            probe_http(&client, &format!("{base}/slow"), Duration::from_secs(1)).await,
            Err("no answer within 1s".into())
        );
        assert_eq!(
            probe_http(&client, &format!("{base}/redirect"), Duration::from_secs(2)).await,
            Err("HTTP 302".into())
        );
    }

    /// Unknown until the first probe; up / down after it; re-due only
    /// after `interval`; a target is never probed twice at once.
    #[tokio::test]
    async fn the_schedule_follows_the_injected_clock() {
        let port = server().await;
        let reg = Arc::new(HealthRegistry::default());
        let t0 = Instant::now();
        reg.sync(
            vec![
                http_target("up", format!("http://127.0.0.1:{port}/ok"), 2),
                http_target("down", format!("http://127.0.0.1:{port}/fail"), 2),
            ],
            t0,
        );
        assert_eq!(reg.get("up", None).unwrap().state, HealthState::Unknown);

        let due = reg.take_due(t0);
        assert_eq!(due.len(), 2);
        assert!(reg.take_due(t0).is_empty(), "in-flight targets are not taken again");
        let client = probe_client().unwrap();
        for (k, url, timeout) in due {
            let r = probe_http(&client, &url, timeout).await;
            reg.record(&k, &url, r, t0, "t".into());
        }
        let up = reg.get("up", None).unwrap();
        assert_eq!((up.state, up.last_checked.as_deref()), (HealthState::Up, Some("t")));
        let down = reg.get("down", None).unwrap();
        assert_eq!(down.state, HealthState::Down);
        assert_eq!(down.last_error.as_deref(), Some("HTTP 503"));

        assert!(reg.take_due(t0 + Duration::from_secs(29)).is_empty());
        assert_eq!(reg.next_due(), Some(t0 + Duration::from_secs(30)));
        assert_eq!(reg.take_due(t0 + Duration::from_secs(30)).len(), 2);
    }

    /// A slow module does not hold up another: the fast one's result lands
    /// while the slow probe is still running.
    #[tokio::test]
    async fn a_slow_probe_does_not_stall_the_others() {
        let port = server().await;
        let reg = Arc::new(HealthRegistry::default());
        let t0 = Instant::now();
        reg.sync(
            vec![
                http_target("slow", format!("http://127.0.0.1:{port}/slow"), 10),
                http_target("fast", format!("http://127.0.0.1:{port}/ok"), 2),
            ],
            t0,
        );
        let client = probe_client().unwrap();
        let handles = dispatch_due(&reg, &client, t0);
        assert_eq!(handles.len(), 2);
        let started = Instant::now();
        while reg.get("fast", None).unwrap().state == HealthState::Unknown {
            assert!(started.elapsed() < Duration::from_secs(2), "fast probe stalled");
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert_eq!(reg.get("fast", None).unwrap().state, HealthState::Up);
        assert_eq!(reg.get("slow", None).unwrap().state, HealthState::Unknown);
        for h in handles {
            h.await.unwrap();
        }
        assert_eq!(reg.get("slow", None).unwrap().state, HealthState::Up);
    }

    /// Re-sync keeps an unchanged target's status, resets a changed one,
    /// drops a removed one, and reports a refusal only when it is new; a
    /// late result for a changed target is discarded.
    #[test]
    fn sync_keeps_resets_drops_and_reports_refusals_once() {
        let reg = HealthRegistry::default();
        let t0 = Instant::now();
        let a = http_target("a", "http://127.0.0.1:1/x".into(), 2);
        let refused = ProbeTarget {
            key: key("r"),
            probe: Probe::Unprobed { reason: "refused".into() },
            interval: Duration::from_secs(30),
        };
        assert_eq!(reg.sync(vec![a.clone(), refused.clone()], t0).len(), 1);
        reg.take_due(t0);
        reg.record(&a.key, "http://127.0.0.1:1/x", Ok(()), t0, "t".into());
        assert!(reg.sync(vec![a.clone(), refused.clone()], t0).is_empty(), "no repeat log");
        assert_eq!(reg.get("a", None).unwrap().state, HealthState::Up);
        let r = reg.get("r", None).unwrap();
        assert_eq!((r.state, r.last_error.as_deref()), (HealthState::Unknown, Some("refused")));

        let moved = http_target("a", "http://127.0.0.1:2/x".into(), 2);
        reg.sync(vec![moved], t0);
        assert_eq!(reg.get("a", None).unwrap().state, HealthState::Unknown);
        assert!(reg.get("r", None).is_none());
        reg.record(&key("a"), "http://127.0.0.1:1/x", Ok(()), t0, "late".into());
        assert_eq!(reg.get("a", None).unwrap().state, HealthState::Unknown);
    }

    /// Only loopback is contacted. A container URL naming its container
    /// port is sent to the loopback mapping; any other host is refused.
    #[test]
    fn the_url_guard_allows_loopback_and_mapped_container_ports_only() {
        let none = HashMap::new();
        let svc = manifest("service", "null", "[]").runtime;
        for ok in ["http://localhost:1/h", "http://127.0.0.1:1/h", "http://[::1]:1/h", "http://127.9.9.9/h"] {
            assert!(checked_probe_url(ok, &svc, &none).is_ok(), "{ok}");
        }
        for bad in ["http://example.com/h", "http://10.0.0.5:11440/h", "http://169.254.169.254/latest"] {
            let e = checked_probe_url(bad, &svc, &none).unwrap_err();
            assert!(e.starts_with("refused to probe non-loopback host"), "{bad}: {e}");
        }
        assert!(checked_probe_url("file:///etc/passwd", &svc, &none).is_err());

        let ports = r#"[{ "host": "{RL_SERVER_PORT}", "container": 11438, "bind": "127.0.0.1" }]"#;
        let container = manifest("container", "null", ports).runtime;
        let placeholders = rl_placeholders(11533, "acme");
        assert_eq!(
            checked_probe_url("http://vct-probe-acme:11438/health", &container, &placeholders),
            Ok("http://127.0.0.1:11533/health".into())
        );
        assert!(checked_probe_url("http://vct-probe-acme:9999/health", &container, &placeholders).is_err());
        let lan = r#"[{ "host": "11533", "container": 11438, "bind": "192.168.1.4" }]"#;
        let lan_bound = manifest("container", "null", lan).runtime;
        assert!(checked_probe_url("http://vct-probe:11438/h", &lan_bound, &none).is_err());
        // A non-container module gets no such rewrite.
        let mcp = manifest("mcp_http", "null", ports).runtime;
        assert!(checked_probe_url("http://vct-probe:11438/h", &mcp, &placeholders).is_err());
    }

    /// The manifest → target mapping: stdio_ping / missing url / an
    /// unresolved placeholder / a refused host are unknown with a reason;
    /// a per-project URL gets that project's port; no block → no target.
    #[test]
    fn targets_from_manifests() {
        let _env = vct_launcher_core::test_env::state_dir_guard_with(&[("VCT_HUB_PORT", None)]);
        assert!(target_for(&manifest("cli", "null", "[]"), Instance::Bundled).is_none());

        let stdio = r#"{ "type": "stdio_ping", "timeout_s": 5, "interval_s": 30 }"#;
        let t = target_for(&manifest("mcp_stdio", stdio, "[]"), Instance::Bundled).unwrap();
        assert!(matches!(&t.probe, Probe::Unprobed { reason } if reason.contains("stdio_ping")));

        let no_url = r#"{ "type": "http_get" }"#;
        let t = target_for(&manifest("service", no_url, "[]"), Instance::Bundled).unwrap();
        assert!(matches!(&t.probe, Probe::Unprobed { reason } if reason.contains("no url")));

        let rl = manifest("container", &http("http://localhost:{RL_SERVER_PORT}/health"), "[]");
        let t = target_for(&rl, Instance::Project { id: "p1", slug: "acme", rl_port: Some(11533) })
            .unwrap();
        assert_eq!(t.key.project_id.as_deref(), Some("p1"));
        assert_eq!(
            t.probe,
            Probe::Http { url: "http://localhost:11533/health".into(), timeout: Duration::from_secs(2) }
        );
        let t = target_for(&rl, Instance::Project { id: "p2", slug: "b", rl_port: None }).unwrap();
        assert!(matches!(&t.probe, Probe::Unprobed { reason } if reason.contains("unresolved")));
        let t = target_for(&rl, Instance::Global).unwrap();
        assert!(matches!(&t.probe, Probe::Http { url, .. } if url.contains(&GLOBAL_RL_PORT.to_string())));

        let lan = manifest("service", &http("http://example.com/health"), "[]");
        let t = target_for(&lan, Instance::Bundled).unwrap();
        assert!(matches!(&t.probe, Probe::Unprobed { reason } if reason.starts_with("refused")));

        let fast = r#"{ "type": "http_get", "url": "http://127.0.0.1:1/h", "timeout_s": 999, "interval_s": 0 }"#;
        let t = target_for(&manifest("service", fast, "[]"), Instance::Bundled).unwrap();
        assert_eq!(t.interval, MIN_INTERVAL);
        assert!(matches!(t.probe, Probe::Http { timeout, .. } if timeout == MAX_TIMEOUT));
    }

    /// Bundled manifests are targeted without an install row; the bundled
    /// hub-api check resolves `{hub_port}` to the running hub's port; an
    /// enabled per-project install is targeted per project, a disabled one
    /// is not.
    #[test]
    fn build_targets_covers_bundled_and_enabled_installs() {
        let guard = vct_launcher_core::test_env::state_dir_guard_with(&[("VCT_HUB_PORT", None)]);
        std::fs::write(guard.path().join("hub.port"), "8123\n").unwrap();
        let root = guard.path().to_path_buf();
        vct_launcher_core::bundled_manifests::sync_bundled_manifests(&root);
        let dir = vct_launcher_core::bundled_manifests::bundled_manifests_dir(&root);
        let mut manifests: Vec<(PathBuf, ModuleManifest)> = vct_launcher_core::bundled_manifests::BUNDLED_MANIFESTS
            .iter()
            .map(|(name, body)| (dir.join(name), ModuleManifest::from_json(body).unwrap()))
            .collect();
        let paid = manifest("container", &http("http://localhost:{RL_SERVER_PORT}/health"), "[]");
        manifests.push((root.join("modules").join("vct-probe").join("vct-module.json"), paid));

        let db = Db::open_in_memory().unwrap();
        let targets = build_targets(&manifests, &root, &db);
        let hub = targets.iter().find(|t| t.key.module_id == "vct-hub-api").expect("hub target");
        assert_eq!(
            hub.probe,
            Probe::Http {
                url: "http://127.0.0.1:8123/api/v1/health".into(),
                timeout: Duration::from_secs(3)
            }
        );
        let embed = targets.iter().find(|t| t.key.module_id == "vct-code-embedding").unwrap();
        // `{code_embed_port}` is the machine resolver's; this harness has no
        // row, so its guard answers the unroutable sentinel port — never the
        // real code-embed service's 11440.
        assert!(matches!(&embed.probe, Probe::Http { url, .. } if url == "http://localhost:9/health"));
        assert!(!targets.iter().any(|t| t.key.module_id == "vct-probe"), "not installed yet");

        use vct_launcher_core::db::models::ProjectHost;
        for (id, slug, port) in [("p-on", "on", 11533u16), ("p-off", "off", 11534)] {
            db.insert_project(id, slug, &format!("/tmp/{slug}"), ProjectHost::Base, slug).unwrap();
            db.set_project_rl_port(id, port).unwrap();
            db.insert_module_install(&format!("i-{id}"), id, "vct-probe", "1.0.0", "/x").unwrap();
        }
        db.set_module_enabled("p-off", "vct-probe", false).unwrap();
        let targets = build_targets(&manifests, &root, &db);
        let probes: Vec<&ProbeTarget> =
            targets.iter().filter(|t| t.key.module_id == "vct-probe").collect();
        assert_eq!(probes.len(), 1, "only the enabled install: {probes:?}");
        assert_eq!(probes[0].key.project_id.as_deref(), Some("p-on"));
        assert_eq!(probes[0].probe, Probe::Http { url: "http://localhost:11533/health".into(), timeout: Duration::from_secs(2) });
    }
}
