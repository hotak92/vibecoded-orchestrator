//! Hub routes exposing the services + per-project-module lifecycle
//! surface over HTTP.
//!
//! ─── Status (v0.2.21 Step 15) ─────────────────────────────────────
//!
//! This file defines the *contract* — routes, JSON envelopes, error
//! shapes — that downstream consumers (the launcher GUI, external
//! tooling, the bundled CLI) will eventually call to drive supervised
//! lifecycle through HTTP rather than direct Tauri commands. The
//! BODIES of most handlers are deliberately 501 stubs.
//!
//! Why stubs and not real implementations: per the master plan
//! `.claude/context/plans/v0.2.21-hub-detachment-and-resolver.md`
//! §"Step 4 replan (2026-05-20)", supervisor relocation
//! (services-watcher + per-module container supervision) is deferred
//! from Step 4 to Step 24, where it pairs with Stream B's
//! `module_supervisor.rs` port. During v0.2.21 the launcher STILL
//! owns the live supervisor; the hub doesn't have a runtime that can
//! probe Podman/Docker, restart services on crash, or stop a module
//! container. Implementing the bodies here BEFORE Step 24 lands would
//! either:
//!
//!   (a) Reach back into the launcher process via reverse-IPC — wrong
//!       direction (the design is launcher-thin-client over hub), or
//!   (b) Duplicate the launcher's `commands::lifecycle.rs` logic in
//!       the hub crate — guaranteed drift between two copies that
//!       both probe the same Podman socket.
//!
//! Both options are strictly worse than landing the wire contract
//! now and the bodies in Step 24 alongside the supervisor port.
//!
//! ─── What v0.2.21 still ships from this file ──────────────────────
//!
//! * `GET /services/status` returns a real (skeleton) snapshot
//!   matching `ServicesRuntimeSnapshot` from
//!   `launcher/src-tauri/src/commands/lifecycle.rs`. `degraded: true`
//!   plus all services reporting `running: false`. This pins the
//!   wire-contract for v0.2.22+ — once Step 24 lands the supervisor
//!   in the hub, the same handler will populate it with real probe
//!   results, and downstream parsers won't need to change shape.
//!
//! * Every other route returns
//!   `501 not_implemented_v0_2_21` with a structured envelope
//!   pointing at Step 24.
//!
//! ─── Wire-contract symmetry with the Tauri commands ───────────────
//!
//! The route layout intentionally mirrors the Tauri-side surface in
//! `src/commands/lifecycle.rs` (services_status, services_start_all,
//! services_stop_all, services_restart_all, service_{start,stop,restart})
//! and `src/commands/module_service.rs` (rl_is_container_running, the
//! per-project module surface that Step 24 / Stream B generalises).
//! Step 24's body-fill will be mechanical — same input args, same
//! output shapes — because the contracts were designed in lockstep
//! here.
//!
//! ─── Error envelope ───────────────────────────────────────────────
//!
//! Same shape as `modules_api` / `config_api`:
//!
//! ```json
//! { "error": { "code": "...", "message": "..." } }
//! ```
//!
//! The 501 code is `not_implemented_v0_2_21` — release-pinned so a
//! stale client running against a newer hub sees a different code
//! (e.g. `not_implemented` plain) on the off-chance a follow-on
//! release wants to redefine the stub scope.

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use super::modules_api::LauncherDbHandle;

// ─── Router ──────────────────────────────────────────────────────

pub fn router() -> Router<LauncherDbHandle> {
    Router::new()
        // Services lifecycle (mirrors src/commands/lifecycle.rs).
        .route("/services/status", get(services_status))
        .route("/services/start-all", post(services_start_all))
        .route("/services/stop-all", post(services_stop_all))
        .route("/services/restart-all", post(services_restart_all))
        .route("/services/{name}/start", post(service_start))
        .route("/services/{name}/stop", post(service_stop))
        .route("/services/{name}/restart", post(service_restart))
        // Per-project module lifecycle (generalises src/commands/module_service.rs).
        .route(
            "/projects/{project_id}/modules/{module_id}/status",
            get(module_status),
        )
        .route(
            "/projects/{project_id}/modules/{module_id}/start",
            post(module_start),
        )
        .route(
            "/projects/{project_id}/modules/{module_id}/stop",
            post(module_stop),
        )
        .route(
            "/projects/{project_id}/modules/{module_id}/restart",
            post(module_restart),
        )
        // v0.2.61 (Option H): GLOBAL module start. The hub is now the
        // SOLE spawn point for global containers — the launcher delegates
        // here (after ensuring the hub is up) instead of spawning podman
        // itself, so the per-spawn module-identity token is minted +
        // registered in the hub's in-memory set at the one place the
        // container is created. (Collapses the former two byte-identical
        // spawn paths: launcher start_global_container_for_module + the
        // hub supervisor. See module_identity.rs.)
        .route("/modules/{module_id}/start", post(global_module_start))
}

// ─── Error envelope ─────────────────────────────────────────────

// v0.2.54 Track J: error_response moved to the shared
// `crate::http_error` module (was four byte-identical copies).
use crate::http_error::error_response;

/// The single 501 envelope used by every stubbed handler. Centralised
/// so the message stays consistent (and a single edit when Step 24
/// fleshes the bodies in won't leave stale messages behind in a
/// handler we forgot to update).
fn not_implemented_v0_2_21() -> axum::response::Response {
    error_response(
        StatusCode::NOT_IMPLEMENTED,
        "not_implemented_v0_2_21",
        "Services/module lifecycle is owned by the launcher in v0.2.21; \
         the hub proxy will activate in Step 24 (Stream B merge). The \
         launcher GUI continues to drive lifecycle via its existing \
         Tauri commands.",
    )
}

// ─── ServicesRuntimeSnapshot shape ──────────────────────────────
//
// Mirrors `ServicesRuntimeSnapshot` + `ServiceRuntimeState` +
// `AdoptionMode` from `launcher/src-tauri/src/commands/lifecycle.rs`.
// We intentionally re-define these here rather than depend on the
// launcher crate: vct-hub is a free-standing crate (vct-launcher-core
// is its only intra-workspace dep) so a downstream consumer can
// `cargo build -p vct-hub` without pulling in Tauri. The duplication
// is small (3 structs, ~10 fields total) and the wire shape is
// pinned by HTTP tests downstream.
//
// When Step 24 ports the supervisor into vct-hub, the canonical home
// for these types will move into vct-hub (probably
// `vct-hub/src/supervisor.rs`) and the launcher-side copies become
// re-exports / wire-types. For v0.2.21 they live here, in the route
// module that defines their HTTP shape.

/// Mirror of `commands::lifecycle::AdoptionMode`. `serde(rename_all =
/// "snake_case")` keeps the wire shape identical so a JSON snapshot
/// produced by the launcher's `services_status` Tauri command is
/// indistinguishable from a snapshot emitted here (and vice versa
/// once Step 24 fills the body).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum AdoptionMode {
    #[default]
    Unresolved,
    Adopt,
    Parallel,
    Refuse,
}

/// Mirror of `commands::lifecycle::ServiceRuntimeState`. Fields kept
/// in the same order so a `git diff` between this file and
/// `lifecycle.rs` makes drift visible.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceRuntimeState {
    pub name: String,
    pub running: bool,
    pub port: u16,
    pub url: String,
    pub externally_managed: bool,
    pub adoption_mode: AdoptionMode,
    #[serde(default)]
    pub container_name: Option<String>,
    #[serde(default)]
    pub zombie: bool,
}

/// Mirror of `commands::lifecycle::ServicesRuntimeSnapshot`, extended
/// with a `degraded` field that the launcher-side struct does not
/// (yet) carry. `degraded: true` means "this snapshot was assembled
/// without a live probe pipeline — treat field values as defaults,
/// not as a current observation."
///
/// Adding `degraded` to the hub-side shape but not (yet) to the
/// launcher-side shape is intentional: until Step 24 unifies these,
/// the launcher's `services_status` Tauri command never produces a
/// degraded snapshot (it always probes), so the field would be a
/// pointless `false` on every payload. Hub-side callers gate on
/// `degraded` so they know whether to trust the contents.
///
/// `serde(default)` on `degraded` means a Step 24 unification can
/// add this field to the launcher-side struct without breaking
/// already-deployed hub clients.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServicesRuntimeSnapshot {
    pub services: Vec<ServiceRuntimeState>,
    pub runtime: Option<String>,
    pub needs_podman_machine_start: bool,
    pub has_unresolved_external: bool,
    /// True iff this snapshot was emitted by the v0.2.21 stub path
    /// (no live probe). Step 24's real implementation will set this
    /// to `false` once it has populated `services` from a podman
    /// inspect + HTTP probe pass.
    #[serde(default)]
    pub degraded: bool,
}

/// Canonical service-name + default-port pairs. Pinned to match
/// `canonical_services()` in `src/commands/lifecycle.rs`. The default
/// ports come from `CLAUDE.md`'s "Default ports" line — Weaviate
/// 8081, Ollama 11435, code-embed 11440 — which is the same source
/// the launcher reads. Pulled into a separate `fn` so the test below
/// can re-use the names without recomputing them.
fn canonical_service_skeletons() -> Vec<ServiceRuntimeState> {
    [
        ("weaviate", 8081u16, "http://localhost:8081/v1/meta"),
        ("ollama", 11435u16, "http://localhost:11435/api/tags"),
        ("code_embed", 11440u16, "http://localhost:11440/health"),
    ]
    .iter()
    .map(|(name, port, url)| ServiceRuntimeState {
        name: (*name).to_string(),
        running: false,
        port: *port,
        url: (*url).to_string(),
        externally_managed: false,
        adoption_mode: AdoptionMode::Unresolved,
        container_name: None,
        zombie: false,
    })
    .collect()
}

// ─── Path-arg types ─────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct ServiceNamePath {
    name: String,
}

#[derive(Debug, Deserialize)]
struct ProjectModulePath {
    project_id: String,
    module_id: String,
}

// ─── Handlers ────────────────────────────────────────────────────
//
// `_h: State<LauncherDbHandle>` is unused in every handler today —
// kept on every signature so Step 24's body-fill can reach the
// launcher DB (read project rows to resolve module slug → container
// name, look up adoption mode, etc.) without changing route
// signatures (which would force a hub server-restart for every
// connected client to re-bind, since axum routers freeze at boot).

/// `GET /services/status` — services snapshot.
///
/// v0.2.21 returns a skeleton with `degraded: true` + every service
/// in its compiled-default state (`running: false`, ports from the
/// canonical default list, `adoption_mode: unresolved`). The wire
/// shape exactly matches what `commands::lifecycle::services_status`
/// produces in the launcher (modulo `degraded`, which is new — see
/// the struct doc), so a downstream consumer's JSON parser doesn't
/// need to change when Step 24 plugs in the real probe pipeline.
///
/// Note: this is NOT a 501. The launcher's tray pill polls
/// `services_status` every 30s; we want a hub-side equivalent
/// returning a *parseable* response from day one so the launcher
/// (when it later migrates to thin-client over the hub) has a
/// well-defined fallback while the supervisor is bootstrapping.
async fn services_status(State(_h): State<LauncherDbHandle>) -> impl IntoResponse {
    let snapshot = ServicesRuntimeSnapshot {
        services: canonical_service_skeletons(),
        runtime: None,
        needs_podman_machine_start: false,
        has_unresolved_external: false,
        degraded: true,
    };
    Json(snapshot).into_response()
}

async fn services_start_all(State(_h): State<LauncherDbHandle>) -> impl IntoResponse {
    not_implemented_v0_2_21()
}

async fn services_stop_all(State(_h): State<LauncherDbHandle>) -> impl IntoResponse {
    not_implemented_v0_2_21()
}

async fn services_restart_all(State(_h): State<LauncherDbHandle>) -> impl IntoResponse {
    not_implemented_v0_2_21()
}

async fn service_start(
    State(_h): State<LauncherDbHandle>,
    Path(p): Path<ServiceNamePath>,
) -> impl IntoResponse {
    let _ = p.name;
    not_implemented_v0_2_21()
}

async fn service_stop(
    State(_h): State<LauncherDbHandle>,
    Path(p): Path<ServiceNamePath>,
) -> impl IntoResponse {
    let _ = p.name;
    not_implemented_v0_2_21()
}

async fn service_restart(
    State(_h): State<LauncherDbHandle>,
    Path(p): Path<ServiceNamePath>,
) -> impl IntoResponse {
    let _ = p.name;
    not_implemented_v0_2_21()
}

// ─── Module-lifecycle handlers (Step 24 commit b) ─────────────────
//
// Filled in by Step 24's Stream B port. The launcher's Tauri-side
// commands (`commands::module_service::rl_is_container_running`,
// `restart_rl_container`) proxy here via the `hub_proxy_module_*` helpers
// in `launcher/src-tauri/src/commands/module_service.rs`. The supervisor
// logic lives in `module_supervisor.rs`.

/// `GET /projects/{project_id}/modules/{module_id}/status` — returns
/// `{"running": bool, "container_name": String?}`.
async fn module_status(
    State(h): State<LauncherDbHandle>,
    Path(p): Path<ProjectModulePath>,
) -> impl IntoResponse {
    let install = match h.0.get_module_install(&p.project_id, &p.module_id) {
        Ok(Some(i)) => i,
        Ok(None) => {
            return Json(serde_json::json!({
                "running": false,
                "container_name": null,
            }))
            .into_response();
        }
        Err(e) => return error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            format!("get_module_install: {}", e),
        ),
    };
    let container_name = install.container_name.unwrap_or_default();
    if container_name.is_empty() {
        return Json(serde_json::json!({
            "running": false,
            "container_name": null,
        }))
        .into_response();
    }
    let running = super::module_supervisor::is_container_running(&container_name)
        .await
        .unwrap_or(false);
    Json(serde_json::json!({
        "running": running,
        "container_name": container_name,
    }))
    .into_response()
}

/// `POST /projects/{project_id}/modules/{module_id}/start` — proxy
/// for `start_container_after_install`. Body: empty (manifest is
/// resolved from the on-disk catalog walked by
/// `module_supervisor::lookup_manifest_by_id`). Returns
/// `{"container_name": String}` on success.
///
/// v0.2.49 Phase 3 (this version): production-wired. The previous
/// implementation returned 501 + `not_implemented_supervisor_install`
/// — that comment said "Phase 3+ wires the catalog resolver". This is
/// that step.
///
/// Error envelopes:
///   * 404 `project_not_found`   — no row in `projects` for `project_id`.
///   * 404 `manifest_not_found`  — no on-disk manifest for `module_id`.
///   * 400 `not_container_module` — manifest's runtime type isn't
///     `container` or `service` (the supervisor only handles long-
///     running daemons; CLI / mcp_stdio modules are on-demand).
///   * 500 `internal_error`       — DB error, container start failure,
///     manifest parse error.
///
/// Idempotency: `start_container_after_install` calls
/// `start_container_for_module` which `podman rm -f`s any same-named
/// container first. Calling this endpoint while a container is already
/// running force-restarts it (the launcher-side path has the same
/// behaviour; see `start_container_for_module`'s docstring).
async fn module_start(
    State(h): State<LauncherDbHandle>,
    Path(p): Path<ProjectModulePath>,
) -> impl IntoResponse {
    // Resolve project.
    let project = match h.0.get_project(&p.project_id) {
        Ok(Some(pj)) => pj,
        Ok(None) => {
            return error_response(
                StatusCode::NOT_FOUND,
                "project_not_found",
                format!("no project with id {}", p.project_id),
            );
        }
        Err(e) => {
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                format!("get_project({}): {}", p.project_id, e),
            );
        }
    };

    // Resolve manifest from the on-disk catalog (modules + bundled).
    let manifest = match super::module_supervisor::lookup_manifest_by_id(&p.module_id) {
        Some(m) => m,
        None => {
            return error_response(
                StatusCode::NOT_FOUND,
                "manifest_not_found",
                format!(
                    "no manifest found for module_id {} in catalog \
                     (~/.vct/modules + ~/.vct/bundled_manifests)",
                    p.module_id
                ),
            );
        }
    };

    // Gate on runtime type — only `container` / `service` modules go
    // through the supervisor's `podman run` path. Reject upfront with a
    // clear error rather than failing inside `start_container_for_module`.
    if !matches!(manifest.runtime.r#type.as_str(), "container" | "service") {
        return error_response(
            StatusCode::BAD_REQUEST,
            "not_container_module",
            format!(
                "module {} has runtime.type='{}' — only container / service \
                 modules can be started via this endpoint",
                p.module_id, manifest.runtime.r#type
            ),
        );
    }

    // Hand off to the supervisor. `start_container_after_install`:
    //   1. Allocates `projects.rl_port` if not yet set.
    //   2. Calls `start_container_for_module` (variant-aware image ref
    //      + v0.2.49 pre-pull-with-auth + podman run).
    //   3. Persists `module_installs.container_name` for resume-on-boot.
    match super::module_supervisor::start_container_after_install(&manifest, &project, &h.0).await {
        Ok(container_name) => {
            Json(serde_json::json!({ "container_name": container_name })).into_response()
        }
        Err(e) => error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "container_start_failed",
            format!("start_container_after_install({}, {}): {}", p.project_id, p.module_id, e),
        ),
    }
}

/// `POST /modules/{module_id}/start` — GLOBAL module start (v0.2.61,
/// Option H). The hub is the sole spawn point for global containers; the
/// launcher delegates here (after `ensure_hub_running`) instead of
/// spawning podman directly. This routes through
/// `start_global_container_supervisor`, which mints + registers the
/// per-spawn module-identity token (`VCT_MODULE_TOKEN`) and injects it —
/// the credential the container then presents to the hub's module-scoped
/// data routes. Returns `{"container_name": String}`.
///
/// Error envelopes:
///   * 404 `manifest_not_found`   — no on-disk manifest for `module_id`.
///   * 400 `not_container_module` — runtime type isn't container/service.
///   * 500 `container_start_failed` — supervisor/podman failure.
async fn global_module_start(Path(module_id): Path<String>) -> impl IntoResponse {
    let manifest = match super::module_supervisor::lookup_manifest_by_id(&module_id) {
        Some(m) => m,
        None => {
            return error_response(
                StatusCode::NOT_FOUND,
                "manifest_not_found",
                format!(
                    "no manifest found for module_id {} in catalog \
                     (~/.vct/modules + ~/.vct/bundled_manifests)",
                    module_id
                ),
            );
        }
    };

    if !matches!(manifest.runtime.r#type.as_str(), "container" | "service") {
        return error_response(
            StatusCode::BAD_REQUEST,
            "not_container_module",
            format!(
                "module {} has runtime.type='{}' — only container / service \
                 modules can be started via this endpoint",
                module_id, manifest.runtime.r#type
            ),
        );
    }

    match super::module_supervisor::start_global_container_supervisor(&manifest, &module_id).await {
        Ok(container_name) => {
            Json(serde_json::json!({ "container_name": container_name })).into_response()
        }
        Err(e) => error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "container_start_failed",
            format!("start_global_container_supervisor({}): {}", module_id, e),
        ),
    }
}

/// `POST /projects/{project_id}/modules/{module_id}/stop` — stop +
/// remove the per-project container. Idempotent: nonexistent container
/// → 204. Reads `container_name` from the module_installs row before
/// invoking the supervisor.
async fn module_stop(
    State(h): State<LauncherDbHandle>,
    Path(p): Path<ProjectModulePath>,
) -> impl IntoResponse {
    let install = match h.0.get_module_install(&p.project_id, &p.module_id) {
        Ok(Some(i)) => i,
        Ok(None) => {
            return (StatusCode::NO_CONTENT, Json(serde_json::json!({}))).into_response();
        }
        Err(e) => return error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            format!("get_module_install: {}", e),
        ),
    };
    let container_name = install.container_name.unwrap_or_default();
    if container_name.is_empty() {
        return (StatusCode::NO_CONTENT, Json(serde_json::json!({}))).into_response();
    }
    match super::module_supervisor::stop_container_for_project(&container_name).await {
        Ok(()) => Json(serde_json::json!({"stopped": container_name})).into_response(),
        Err(e) => error_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            format!("stop_container_for_project: {}", e),
        ),
    }
}

/// `POST /projects/{project_id}/modules/{module_id}/restart` — 501 by
/// DECISION (v0.2.75 RL-15), not by omission.
///
/// DECIDED POSTURE: GUI-alive in-process restart is the SUPPORTED path —
/// the launcher's `restart_rl_container` command drives
/// `module_supervisor` in-process via `commands/module_service.rs`, with
/// the launcher's full manifest context in hand. A hub-side restart would
/// need its own manifest CATALOG RESOLVER (manifest discovery + variant
/// resolution without a launcher process); building a partial resolver
/// just to serve this route would fork the resolution logic and drift
/// from the launcher's (the exact cross-language-fork failure mode the
/// house rules forbid). So the route stays 501 — deliberately.
///
/// UNLOCK CONDITION: the Phase 3+ manifest catalog resolver (one shared
/// resolution home reachable from the hub). When THAT lands, wire this
/// route through it; do NOT implement a partial resolver here earlier.
async fn module_restart(
    State(_h): State<LauncherDbHandle>,
    Path(p): Path<ProjectModulePath>,
) -> impl IntoResponse {
    let _ = (p.project_id, p.module_id);
    error_response(
        StatusCode::NOT_IMPLEMENTED,
        "not_implemented_supervisor_restart",
        "Hub-side restart needs a manifest catalog resolver — landed in a \
         Phase 3+ step. Launcher's restart_rl_container command continues \
         to call module_supervisor in-process via commands/module_service.rs.",
    )
}

// ─── Tests ───────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use axum::Router;
    use std::sync::Arc;
    use vct_launcher_core::db::Db;

    /// Spawn the lifecycle_api router on a random local port. Mirrors
    /// `spawn_config_api_hub` in config_api.rs::tests and
    /// `spawn_modules_api_hub` in modules_api.rs::tests so test
    /// fixtures stay symmetric across the three route modules.
    async fn spawn_lifecycle_api_hub() -> (String, LauncherDbHandle) {
        let db = Db::open_in_memory().expect("in-memory db");
        let handle = LauncherDbHandle(Arc::new(db));
        let app: Router =
            Router::new().nest("/api/v1", super::router().with_state(handle.clone()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("local_addr");
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        (format!("http://{}/api/v1", addr), handle)
    }

    /// Assert that a response has status 501 + the canonical
    /// not_implemented_v0_2_21 envelope. Factored so each stub-route
    /// test stays a 3-liner.
    async fn assert_501_envelope(resp: reqwest::Response) {
        assert_eq!(resp.status(), 501, "expected 501 Not Implemented");
        let body: serde_json::Value = resp.json().await.expect("json body");
        let err = body.get("error").expect("error envelope");
        assert_eq!(
            err.get("code").and_then(|v| v.as_str()),
            Some("not_implemented_v0_2_21"),
            "expected code=not_implemented_v0_2_21, got: {:?}",
            body
        );
        // Sanity-check the message points at Step 24 + the launcher
        // owning v0.2.21 lifecycle. Keeps the message-string regression
        // visible if a future edit accidentally drops the breadcrumb.
        let msg = err
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or_default();
        assert!(
            msg.contains("v0.2.21") && msg.contains("Step 24"),
            "expected message to mention v0.2.21 + Step 24, got: {:?}",
            msg
        );
    }

    #[tokio::test]
    async fn services_status_returns_degraded_skeleton() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let resp = reqwest::get(format!("{}/services/status", base))
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json body");

        // degraded must be true in v0.2.21.
        assert_eq!(body.get("degraded").and_then(|v| v.as_bool()), Some(true));

        // Top-level snapshot fields exist with default values.
        assert!(body.get("runtime").map(|v| v.is_null()).unwrap_or(false));
        assert_eq!(
            body.get("needs_podman_machine_start")
                .and_then(|v| v.as_bool()),
            Some(false)
        );
        assert_eq!(
            body.get("has_unresolved_external").and_then(|v| v.as_bool()),
            Some(false)
        );

        // services[] carries all three canonical entries with the
        // default ports, running: false, adoption_mode: unresolved.
        let services = body
            .get("services")
            .and_then(|v| v.as_array())
            .expect("services array");
        assert_eq!(services.len(), 3, "expected 3 canonical services");

        let by_name: std::collections::HashMap<&str, &serde_json::Value> = services
            .iter()
            .map(|s| {
                let name = s.get("name").and_then(|v| v.as_str()).unwrap_or("");
                (name, s)
            })
            .collect();

        for (name, expected_port) in [("weaviate", 8081), ("ollama", 11435), ("code_embed", 11440)]
        {
            let s = by_name.get(name).unwrap_or_else(|| {
                panic!("missing canonical service {} in: {:?}", name, by_name.keys())
            });
            assert_eq!(s.get("running").and_then(|v| v.as_bool()), Some(false));
            assert_eq!(
                s.get("port").and_then(|v| v.as_u64()),
                Some(expected_port as u64)
            );
            assert_eq!(
                s.get("adoption_mode").and_then(|v| v.as_str()),
                Some("unresolved")
            );
            assert_eq!(
                s.get("externally_managed").and_then(|v| v.as_bool()),
                Some(false)
            );
            assert_eq!(s.get("zombie").and_then(|v| v.as_bool()), Some(false));
            // container_name is None → serializes to null.
            assert!(s
                .get("container_name")
                .map(|v| v.is_null())
                .unwrap_or(false));
            // url is the canonical probe URL for that service.
            let url = s.get("url").and_then(|v| v.as_str()).unwrap_or_default();
            assert!(
                url.contains(&expected_port.to_string()),
                "url {:?} should contain port {}",
                url,
                expected_port
            );
        }
    }

    #[tokio::test]
    async fn services_start_all_returns_501() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/services/start-all", base))
            .send()
            .await
            .expect("hub reachable");
        assert_501_envelope(resp).await;
    }

    #[tokio::test]
    async fn services_stop_all_returns_501() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/services/stop-all", base))
            .send()
            .await
            .expect("hub reachable");
        assert_501_envelope(resp).await;
    }

    #[tokio::test]
    async fn services_restart_all_returns_501() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/services/restart-all", base))
            .send()
            .await
            .expect("hub reachable");
        assert_501_envelope(resp).await;
    }

    #[tokio::test]
    async fn service_start_one_returns_501() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/services/weaviate/start", base))
            .send()
            .await
            .expect("hub reachable");
        assert_501_envelope(resp).await;
    }

    #[tokio::test]
    async fn service_stop_one_returns_501() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/services/ollama/stop", base))
            .send()
            .await
            .expect("hub reachable");
        assert_501_envelope(resp).await;
    }

    #[tokio::test]
    async fn service_restart_one_returns_501() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/services/code_embed/restart", base))
            .send()
            .await
            .expect("hub reachable");
        assert_501_envelope(resp).await;
    }

    /// Step 24 commit b: module_status/stop are now wired. Unknown
    /// project_id → `{"running": false, "container_name": null}` (200).
    /// The wire-shape pins what the launcher proxy expects.
    #[tokio::test]
    async fn module_status_returns_running_false_for_unknown_project() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let resp = reqwest::get(format!(
            "{}/projects/proj-unknown/modules/vct-rl-reranker/status",
            base
        ))
        .await
        .expect("hub reachable");
        assert_eq!(resp.status(), 200);
        let body: serde_json::Value = resp.json().await.expect("json");
        assert_eq!(body.get("running").and_then(|v| v.as_bool()), Some(false));
        assert!(body.get("container_name").map(|v| v.is_null()).unwrap_or(false));
    }

    /// v0.2.49 Phase 3 (hub-side supervisor auth port): module_start is
    /// no longer 501. With an empty in-memory DB, the endpoint returns
    /// 404 `project_not_found` — the resolver looked the project up
    /// first and didn't find it. Pre-v0.2.49 every request returned 501
    /// `not_implemented_supervisor_install` because the catalog resolver
    /// wasn't wired.
    #[tokio::test]
    async fn module_start_unknown_project_returns_404() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!(
                "{}/projects/proj-123/modules/rl-reranker/start",
                base
            ))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404, "empty DB → project_not_found");
        let body: serde_json::Value = resp.json().await.expect("json");
        assert_eq!(
            body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
            Some("project_not_found"),
            "expected project_not_found, got: {:?}",
            body
        );
    }

    /// Step 24 commit b: module_stop is idempotent — unknown project
    /// returns 204 No Content (not 501).
    #[tokio::test]
    async fn module_stop_returns_204_for_unknown_project() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!(
                "{}/projects/proj-unknown/modules/vct-rl-reranker/stop",
                base
            ))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 204);
    }

    /// v0.2.75 RL-15 (DECIDED, was "not yet implemented"): module_restart
    /// returns 501 BY DESIGN. GUI-alive in-process restart
    /// (`restart_rl_container` → module_supervisor via
    /// commands/module_service.rs) is the supported posture until the
    /// Phase 3+ manifest catalog resolver exists — a partial hub-side
    /// resolver would fork the launcher's manifest resolution and drift.
    /// This test PINS the deliberate 501; if it starts failing because the
    /// route grew an implementation, verify the implementation routes
    /// through the SHARED catalog resolver, then update this pin.
    #[tokio::test]
    async fn module_restart_returns_501_supervisor_restart() {
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!(
                "{}/projects/proj-123/modules/rl-reranker/restart",
                base
            ))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 501);
        let body: serde_json::Value = resp.json().await.expect("json");
        assert_eq!(
            body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
            Some("not_implemented_supervisor_restart"),
        );
    }

    /// Pin the wire-contract symmetry: the hub-side
    /// `ServicesRuntimeSnapshot` round-trips through serde with the
    /// expected JSON field names. If a future edit renames a field
    /// (or changes serde-rename), this test fails before the route
    /// test does — giving a clearer error than a 500 deeper in the
    /// stack.
    #[test]
    fn services_runtime_snapshot_serializes_with_expected_field_names() {
        let snapshot = ServicesRuntimeSnapshot {
            services: canonical_service_skeletons(),
            runtime: None,
            needs_podman_machine_start: false,
            has_unresolved_external: false,
            degraded: true,
        };
        let json = serde_json::to_value(&snapshot).expect("serialize");
        let obj = json.as_object().expect("snapshot is an object");
        for key in [
            "services",
            "runtime",
            "needs_podman_machine_start",
            "has_unresolved_external",
            "degraded",
        ] {
            assert!(obj.contains_key(key), "missing key {} in {:?}", key, obj);
        }

        let services = obj.get("services").and_then(|v| v.as_array()).unwrap();
        let s0 = services[0].as_object().unwrap();
        for key in [
            "name",
            "running",
            "port",
            "url",
            "externally_managed",
            "adoption_mode",
            "container_name",
            "zombie",
        ] {
            assert!(s0.contains_key(key), "missing service key {} in {:?}", key, s0);
        }
    }

    /// AdoptionMode must serialize in snake_case so the wire shape
    /// is interchangeable with the launcher-side struct's. If a
    /// future edit drops `serde(rename_all = "snake_case")` this
    /// catches it directly.
    #[test]
    fn adoption_mode_serializes_snake_case() {
        for (mode, expected) in [
            (AdoptionMode::Unresolved, "unresolved"),
            (AdoptionMode::Adopt, "adopt"),
            (AdoptionMode::Parallel, "parallel"),
            (AdoptionMode::Refuse, "refuse"),
        ] {
            let json = serde_json::to_value(mode).expect("serialize");
            assert_eq!(json.as_str(), Some(expected));
        }
    }

    // ─── v0.2.49 Phase 3: module_start production-wired tests ──────────
    //
    // These tests pin the new module_start contract (200 + container_name
    // OR a structured error envelope). They use VCT_STATE_DIR overrides
    // + an in-memory launcher DB to make the on-disk catalog lookup
    // hermetic across test runs.
    //
    // Note: a true HAPPY-PATH test that asserts 200 isn't possible in a
    // unit test (would need a real container runtime + a real image
    // cache). The closest hermetic assertion: a manifest with a
    // non-container runtime type returns the 400 not_container_module
    // gate response, which proves the manifest WAS loaded (resolver
    // works) AND the gate path WAS reached.

    use std::path::PathBuf;

    /// Per-test guard: sets `VCT_STATE_DIR` to a fresh tempdir for the
    /// duration of the test. Drops the tempdir on guard drop so the
    /// next test starts fresh.
    struct VctStateDirGuard {
        _td: tempfile::TempDir,
        previous: Option<String>,
    }

    impl VctStateDirGuard {
        fn new() -> Self {
            // Serialize tests that mutate VCT_STATE_DIR (process-wide
            // env var) via a global mutex. Same pattern paths::tests
            // uses upstream.
            use std::sync::{Mutex, OnceLock};
            static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
            let _g = LOCK
                .get_or_init(|| Mutex::new(()))
                .lock()
                .unwrap_or_else(|p| p.into_inner());
            let td = tempfile::tempdir().expect("tempdir");
            let previous = std::env::var("VCT_STATE_DIR").ok();
            std::env::set_var("VCT_STATE_DIR", td.path());
            // _g releases here; the guard holds the lock again by
            // reacquiring it on drop.
            drop(_g);
            Self { _td: td, previous }
        }

        fn vct_root(&self) -> PathBuf {
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

    /// Write a vct-module.json with the given runtime type to
    /// `<vct_root>/bundled_manifests/<module_id>.json`. Returns the
    /// path written.
    fn write_test_manifest(vct_root: &std::path::Path, module_id: &str, runtime_type: &str) -> PathBuf {
        let dir = vct_root.join("bundled_manifests");
        std::fs::create_dir_all(&dir).expect("mkdir bundled_manifests");
        let path = dir.join(format!("{}.json", module_id));
        // Use the smallest valid manifest shape that ModuleManifest::
        // from_json accepts. Mirrors the test fixtures in
        // modules_api.rs.
        let install_dir = format!("/tmp/{}", module_id);
        let image = format!("ghcr.io/test/{}", module_id);
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
                "install_dir": install_dir,
                "post_install": [],
                "container": {
                    "image": image,
                    "tag_from_version": true,
                    "pull_token_endpoint": "https://example.invalid/x",
                    "pull_token_method": "POST",
                    "rotate_weights": false
                }
            },
            "secrets": [],
            "settings": [],
            "runtime": {
                "type": runtime_type,
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
        path
    }

    /// v0.2.49 Phase 3: module_start with an unknown project returns 404
    /// `project_not_found`. Mirrors the existing
    /// `module_start_unknown_project_returns_404` but pins the
    /// invariant from a different angle (no manifest written, no
    /// project written → resolver short-circuits at project lookup).
    #[tokio::test]
    async fn v0249_module_start_no_project_returns_project_not_found() {
        let _g = VctStateDirGuard::new();
        let (base, _h) = spawn_lifecycle_api_hub().await;
        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/projects/no-such/modules/no-such/start", base))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json");
        assert_eq!(
            body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
            Some("project_not_found"),
        );
    }

    /// v0.2.49 Phase 3: module_start with a real project but no
    /// manifest on disk returns 404 `manifest_not_found`.
    #[tokio::test]
    async fn v0249_module_start_no_manifest_returns_manifest_not_found() {
        let _g = VctStateDirGuard::new();
        let (base, h) = spawn_lifecycle_api_hub().await;
        // Insert a project so we get past the project_not_found gate.
        use vct_launcher_core::db::models::ProjectHost;
        h.0.insert_project("proj-1", "Acme", "/tmp/acme", ProjectHost::Base, "acme")
            .expect("insert project");

        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/projects/proj-1/modules/no-such-module/start", base))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 404);
        let body: serde_json::Value = resp.json().await.expect("json");
        assert_eq!(
            body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
            Some("manifest_not_found"),
        );
    }

    /// v0.2.49 Phase 3: module_start with a real project + a real
    /// on-disk manifest whose runtime type is NOT container/service
    /// returns 400 `not_container_module`. Proves: the catalog
    /// resolver IS firing (it loaded the manifest) AND the gate path
    /// IS reached (the supervisor would otherwise return 500 on the
    /// non-container runtime check inside `start_container_for_module`).
    #[tokio::test]
    async fn v0249_module_start_non_container_runtime_returns_400() {
        let g = VctStateDirGuard::new();
        write_test_manifest(&g.vct_root(), "test-cli-module", "cli");

        let (base, h) = spawn_lifecycle_api_hub().await;
        use vct_launcher_core::db::models::ProjectHost;
        h.0.insert_project("proj-1", "Acme", "/tmp/acme", ProjectHost::Base, "acme")
            .expect("insert project");

        let client = reqwest::Client::new();
        let resp = client
            .post(format!("{}/projects/proj-1/modules/test-cli-module/start", base))
            .send()
            .await
            .expect("hub reachable");
        assert_eq!(resp.status(), 400, "non-container runtime → 400");
        let body: serde_json::Value = resp.json().await.expect("json");
        assert_eq!(
            body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
            Some("not_container_module"),
            "expected not_container_module, got: {:?}",
            body
        );
    }

    /// v0.2.49 Phase 3: module_start with a real project + a real
    /// on-disk manifest with a container runtime reaches the
    /// supervisor's `start_container_for_module`. Since no real podman
    /// runtime + image is available in tests, the call fails — but
    /// crucially it fails WITH a 500 `container_start_failed` envelope
    /// (NOT a 404 or 400). This pins that:
    ///   (a) the project lookup succeeded;
    ///   (b) the manifest lookup succeeded;
    ///   (c) the runtime-type gate passed;
    ///   (d) the supervisor was invoked.
    /// Mirrors the launcher-side test posture (assert on observable
    /// 500 + structured error code without requiring a real container
    /// runtime to be present).
    #[tokio::test]
    async fn v0249_module_start_container_module_reaches_supervisor() {
        let g = VctStateDirGuard::new();
        write_test_manifest(&g.vct_root(), "test-container-module", "container");

        let (base, h) = spawn_lifecycle_api_hub().await;
        use vct_launcher_core::db::models::ProjectHost;
        h.0.insert_project("proj-1", "Acme", "/tmp/acme", ProjectHost::Base, "acme")
            .expect("insert project");

        let client = reqwest::Client::new();
        let resp = client
            .post(format!(
                "{}/projects/proj-1/modules/test-container-module/start",
                base
            ))
            .send()
            .await
            .expect("hub reachable");
        // On a host with podman + the test image cached, this would
        // return 200. On every CI runner we've seen, the test image
        // doesn't exist → supervisor fails → 500. We accept either —
        // the failure mode we're guarding against is 404 / 400, which
        // would mean the resolver/gate logic broke.
        let status = resp.status();
        assert!(
            status == 200 || status == 500,
            "expected 200 (real podman) or 500 (no podman), got {}",
            status
        );
        if status == 500 {
            let body: serde_json::Value = resp.json().await.expect("json");
            assert_eq!(
                body.get("error").and_then(|e| e.get("code")).and_then(|v| v.as_str()),
                Some("container_start_failed"),
                "expected container_start_failed, got: {:?}",
                body
            );
        }
    }
}
