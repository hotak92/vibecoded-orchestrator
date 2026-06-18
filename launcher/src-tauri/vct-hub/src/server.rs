//! Hub HTTP server — starts alongside Tauri on port 7700.
//!
//! The server runs in a background tokio task. It exposes a REST API
//! that any local app/service can call to register, send messages,
//! query data, etc.
//!
//! ─── Authentication (H5, 2026-05-08) ─────────────────────────────
//!
//! Every request to `/api/v1/*` (except `/health`) requires
//! `Authorization: Bearer <token>` where `<token>` is the value the
//! hub wrote to `<vct_root_dir>/hub.token` on startup. Same threat
//! model as `~/.vct/hub.port` — same-user-only by file mode (0o600 on
//! Unix, default ACL on Windows). See `hub::auth` for the full
//! rationale and exempt-paths discussion.

use std::net::SocketAddr;
use std::sync::Arc;
use tower_http::cors::{Any, CorsLayer};

use super::{
    api, auth, cli_api, config_api, db, infra_watchdog, lifecycle_api, mcp_tool_grants_api,
    module_db_api, module_supervisor, modules_api, project_state_api, rl_events_api, secrets_api,
    weaviate_probe,
};

const DEFAULT_PORT: u16 = 7700;

/// Start the Hub API server on a background task.
/// Returns the port it's listening on.
pub async fn start_hub_server() -> Result<u16, String> {
    let port = std::env::var("VCT_HUB_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_PORT);

    let database = db::open_db().map_err(|e| format!("Failed to open hub database: {}", e))?;

    // Open a second connection to launcher.db for the module/project routes.
    // WAL mode lets this coexist with the Tauri-side Db handle.
    let launcher_db = vct_launcher_core::db::Db::open()
        .map_err(|e| format!("Failed to open launcher.db: {}", e))?;
    let launcher_state = modules_api::LauncherDbHandle(Arc::new(launcher_db));

    // v0.2.21 Step 21: hub-startup Weaviate class existence check.
    // Spawn detached — the probe issues per-class HTTP HEADs and we
    // don't want server startup blocked on Weaviate's response time
    // (a slow probe could push us past the 30s install.py /health
    // deadline). The probe writes a sidecar JSONL on completion;
    // future surfaces (resolver 503 emission, install.py post-install
    // check, GUI status banner) read it. We re-open Db here because
    // the launcher_state handle wraps it in Arc<Mutex>; the probe
    // module wants an owned Db (its own connection, WAL-safe).
    // Soft-fail: if Db::open fails here, log and skip — the server
    // still boots and the rest of the routes serve normally.
    match vct_launcher_core::db::Db::open() {
        Ok(probe_db) => {
            let local_config = vct_launcher_core::config::LocalConfig::load();
            weaviate_probe::spawn_startup_probe(probe_db, &local_config);
        }
        Err(e) => {
            eprintln!(
                "[vct-hub] weaviate_probe: cannot open launcher.db for class check ({}); skipping",
                e
            );
        }
    }

    // ── Auth token (H5) ──────────────────────────────────────────
    // Generate a fresh token on every startup and persist before we
    // accept any connections. If either step fails we refuse to
    // start the server: serving secrets without auth would be
    // strictly worse than the launcher being temporarily down.
    let auth_token = auth::generate_token()
        .map_err(|e| format!("Failed to generate hub auth token: {}", e))?;
    auth::write_token_file(&auth_token)
        .map_err(|e| format!("Failed to write hub.token: {}", e))?;
    let auth_state = auth::AuthState::new(auth_token);

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        // `Any` for headers wouldn't include `Authorization` in some
        // browsers' interpretations of the spec; spell it out so a
        // future browser-side client can't be tripped up by a CORS
        // preflight that strips Authorization from the allowlist.
        .allow_headers([
            axum::http::header::AUTHORIZATION,
            axum::http::header::CONTENT_TYPE,
        ]);

    // Layer order (axum applies layers in reverse-of-declaration on
    // the way IN to a request, so the LAST layer added runs FIRST):
    //
    //   request → cors → require_auth → routes → response
    //
    // Why this order:
    //   * `cors` must wrap the auth check so OPTIONS preflights get
    //     CORS headers attached even if they would otherwise 401
    //     (the auth middleware does pass OPTIONS through, but having
    //     cors as the outermost layer means the response always
    //     carries the right Access-Control-Allow-* headers).
    //   * `require_auth` must wrap the route handlers so an
    //     unauthenticated request never even reaches the secret-
    //     serving logic. The `Extension` carries `AuthState` into
    //     the middleware closure.
    let app = axum::Router::new()
        .nest("/api/v1", api::router(database))
        .nest("/api/v1", modules_api::router().with_state(launcher_state.clone()))
        .nest(
            "/api/v1",
            project_state_api::router().with_state(launcher_state.clone()),
        )
        .nest(
            "/api/v1",
            config_api::router().with_state(launcher_state.clone()),
        )
        .nest(
            "/api/v1",
            lifecycle_api::router().with_state(launcher_state.clone()),
        )
        .nest(
            "/api/v1",
            cli_api::router().with_state(launcher_state.clone()),
        )
        // Phase 1.2: per-project MCP tool-grant resolver. Mounted
        // INSIDE the hub-wide auth layer (the wrappers send the
        // standard hub.token bearer). Read-only today; Phase 1.1
        // sibling adds the write path via its own Tauri command
        // (set_project_mcp_tool_enabled).
        .nest(
            "/api/v1",
            mcp_tool_grants_api::router().with_state(launcher_state.clone()),
        )
        // v0.2.46 V47-C (Gap C): secret-migration endpoint. Mounted INSIDE
        // the hub-wide auth layer (every caller — install.py, the GUI's
        // future Secrets tab — sends the standard hub.token bearer). The
        // endpoint writes to the OS keychain via vct_launcher_core::
        // secrets::set; same threat model as the rest of /api/v1.
        .nest(
            "/api/v1",
            secrets_api::router().with_state(launcher_state.clone()),
        )
        // v0.2.47 RL-4: RL telemetry events queryable store (migration 025).
        // POST /rl/events accepts the Python writer's v3 events, INSERTs
        // into launcher.db::rl_events. GET routes serve dashboards +
        // offline_trainer. Standard hub.token bearer auth applies (same
        // layer order as the sibling routes above).
        .nest(
            "/api/v1",
            rl_events_api::router().with_state(launcher_state.clone()),
        )
        // v0.2.31: module-owned DB rows. Uses its OWN bearer-scope
        // middleware (require_module_scope) — token is the per-(module,
        // project) shared secret stored in launcher.db's
        // `module_access_tokens` table, NOT the hub-wide hub.token.
        // We mount it OUTSIDE the hub-wide auth::require_auth layer
        // (see comment chain in module_db_api::require_module_scope).
        .nest(
            "/api/v1",
            module_db_api::router().with_state(launcher_state.clone()),
        )
        // Inject the launcher_state as a request extension so the
        // module_db_api middleware can pull it without going through
        // axum's State<>-typed router. Clone here so the v0.2.49
        // resume-on-boot task below (which spawns AFTER router build)
        // can still own its own handle.
        .layer(axum::Extension(launcher_state.clone()))
        .layer(axum::middleware::from_fn(auth::require_auth))
        .layer(axum::Extension(auth_state))
        .layer(cors);

    // Bind 0.0.0.0 (v0.2.61, Option H). The hub MUST be reachable from a
    // global module's container network namespace (the RL container reads
    // its training corpus via
    // `GET <VCT_HUB_BASE_URL>/api/v1/modules/{id}/projects/{pid}/rl/events`).
    // `host.containers.internal` resolves to different host addresses per
    // container runtime/backend (bridge gateway on rootful podman/docker,
    // the host LAN IP on rootless pasta, etc.), so the ONLY runtime-
    // agnostic way to be reachable everywhere — without per-backend
    // interface detection that breeds bugs — is to listen on all
    // interfaces and let the BEARER TOKEN be the access control.
    //
    // SECURITY POSTURE (deliberate change from the prior 127.0.0.1-only
    // bind): network isolation was never the real lock — EVERY `/api/v1/*`
    // route is gated by `auth::require_auth` against a 256-bit CSPRNG
    // `hub.token` (and module routes by the per-module ephemeral token).
    // A LAN peer that can now reach the port still gets 401 without the
    // token. Loopback-only was defense-in-depth; binding 0.0.0.0 trades
    // that one layer for runtime-agnostic container reachability + far
    // less code. The token IS the boundary. (If a deployment ever needs
    // the hub pinned off non-loopback interfaces, that's a future opt-in
    // env, not a default.)
    let addr = SocketAddr::from(([0, 0, 0, 0], port));

    // Try to bind — if port is taken, try next 5 ports
    let listener = try_bind(addr, 5).await?;
    let actual_port = listener.local_addr().unwrap().port();

    // Write port file so other apps can discover us
    write_port_file(actual_port).await;

    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            eprintln!("[vct-hub] Server error: {}", e);
        }
    });

    // v0.2.49 Phase 3 (hub-side supervisor auth port): the resume sweep
    // is now PRODUCTION-WIRED. The pre-v0.2.49 comment here said "Phase
    // 3+ would wire a hub-side catalog resolver and cut the launcher
    // hook over to a no-op; until that work lands the stub here
    // masqueraded as live coverage" — this is that work.
    //
    // What changed vs v0.2.40 F4:
    //   * The hardcoded `Box::new(|_id| None)` resolver is gone.
    //     `module_supervisor::real_manifest_resolver()` reads the
    //     on-disk catalog (`<vct_root_dir>/modules/<id>/vct-module.json`
    //     + `<vct_root_dir>/bundled_manifests/*.json`) and returns the
    //     first match for a requested module_id.
    //   * Both `resume_containers_on_startup` and `lifecycle_api::
    //     module_start` are now live — the supervisor is a self-
    //     sufficient code path for both boot-time and on-demand
    //     container starts.
    //
    // Precedence with the launcher-side resume:
    //   The launcher-side `commands::module_service::resume_containers_
    //   on_startup` (invoked from `lib.rs::setup()`) is preserved as a
    //   FALLBACK. Both layers are idempotent — they check `is_container_
    //   running` before starting — so a double-resume on a host where
    //   both the launcher and the hub boot in quick succession is a
    //   no-op for the second runner. The launcher-side path covers the
    //   edge case where the hub isn't running yet at launcher boot
    //   (rare on the same machine since the launcher itself spawns
    //   `vct-hub`, but possible during upgrade flows).
    //
    // Spawned as a detached task so server boot doesn't block on the
    // sweep (which shells out to podman/docker per row). The launcher_db
    // handle is cloned (cheap Arc) so the task owns its reference.
    let resume_db = launcher_state.clone();
    tokio::spawn(async move {
        module_supervisor::resume_containers_on_startup(
            &resume_db.0,
            module_supervisor::real_manifest_resolver(),
        )
        .await;
    });

    // v0.2.62: continuous infra-container watchdog. Distinct from the
    // module supervisor above (paid modules) — this one keeps the shared
    // infra stack (vco_weaviate / vco_ollama / vco_code_embed) alive when
    // a container dies or is stopped mid-session, which previously went
    // unhealed until the launcher was restarted. Self-spawns a detached
    // task (or logs + no-ops when disabled via VCT_HUB_INFRA_WATCHDOG=0),
    // soft-fails every tick, and never restarts a service the user
    // adopted / runs in parallel / refused / paused. The launcher's own
    // boot-time `services_start_all` + the SessionStart hook remain the
    // cold-start path; the watchdog is the always-on safety net.
    infra_watchdog::spawn_infra_watchdog(launcher_state.clone());

    println!("[vct-hub] API server running on http://127.0.0.1:{}", actual_port);
    Ok(actual_port)
}

async fn try_bind(base_addr: SocketAddr, retries: u16) -> Result<tokio::net::TcpListener, String> {
    for offset in 0..=retries {
        let addr = SocketAddr::from((base_addr.ip(), base_addr.port() + offset));
        match tokio::net::TcpListener::bind(addr).await {
            Ok(listener) => return Ok(listener),
            Err(_) if offset < retries => continue,
            Err(e) => return Err(format!(
                "Cannot bind to ports {}-{}: {}",
                base_addr.port(),
                base_addr.port() + retries,
                e
            )),
        }
    }
    unreachable!()
}

/// Write port to `<VCT_STATE_DIR or ~/.vct>/hub.port` so apps can discover the hub.
///
/// v0.2.61 (Option H C-PORT): write atomically via a temp file + rename.
/// A plain truncating write can be observed mid-write (empty / partial) by a
/// concurrent reader (`module_service::hub_port_for_proxy`,
/// `module_supervisor::resolve_hub_base_port`) whose `parse::<u16>()` then
/// fails → wrong VCT_HUB_BASE_URL / a failed readiness probe on a healthy hub.
/// A same-directory rename is atomic on POSIX and on Windows ReplaceFile
/// semantics, so a reader sees either the old value or the new one, never a
/// torn one.
async fn write_port_file(port: u16) {
    let path = vct_launcher_core::paths::vct_root_dir().join("hub.port");

    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    // Temp name is pid-suffixed so two hub processes racing a write don't
    // clobber each other's temp file before their respective renames.
    let tmp = path.with_extension(format!("port.tmp.{}", std::process::id()));
    if tokio::fs::write(&tmp, port.to_string()).await.is_ok() {
        if tokio::fs::rename(&tmp, &path).await.is_err() {
            // Rename failed (e.g. cross-device, shouldn't happen same-dir) —
            // fall back to a direct write so the port is at least discoverable.
            tokio::fs::write(&path, port.to_string()).await.ok();
            tokio::fs::remove_file(&tmp).await.ok();
        }
    }
}
