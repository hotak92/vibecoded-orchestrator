//! Hub HTTP server — starts alongside Tauri on port 7700.
//!
//! The server runs in a background tokio task. It exposes a REST API
//! that any local app/service can call to register, send messages,
//! query data, etc.

use std::net::SocketAddr;
use std::sync::Arc;
use tower_http::cors::{Any, CorsLayer};

use super::{api, cli_api, db, modules_api, project_state_api};

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
    let launcher_db = crate::db::Db::open()
        .map_err(|e| format!("Failed to open launcher.db: {}", e))?;
    let launcher_state = modules_api::LauncherDbHandle(Arc::new(launcher_db));

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let app = axum::Router::new()
        .nest("/api/v1", api::router(database))
        .nest("/api/v1", modules_api::router().with_state(launcher_state.clone()))
        .nest(
            "/api/v1",
            project_state_api::router().with_state(launcher_state.clone()),
        )
        .nest("/api/v1", cli_api::router().with_state(launcher_state))
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

/// Write port to ~/.vct/hub.port so apps can discover the hub.
async fn write_port_file(port: u16) {
    let path = directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct").join("hub.port"))
        .unwrap_or_else(|| ".vct/hub.port".into());

    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    tokio::fs::write(&path, port.to_string()).await.ok();
}
