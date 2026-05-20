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
    api, auth, cli_api, config_api, db, lifecycle_api, modules_api, project_state_api,
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
        .nest("/api/v1", cli_api::router().with_state(launcher_state))
        .layer(axum::middleware::from_fn(auth::require_auth))
        .layer(axum::Extension(auth_state))
        .layer(cors);

    let addr = SocketAddr::from(([127, 0, 0, 1], port));

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
async fn write_port_file(port: u16) {
    let path = vct_launcher_core::paths::vct_root_dir().join("hub.port");

    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    tokio::fs::write(&path, port.to_string()).await.ok();
}
