//! vct-hub — detached HTTP server for VCT.
//!
//! v0.2.21 introduces this binary alongside the existing vct-launcher
//! Tauri GUI. The hub:
//!   - Survives launcher GUI close (hooks/MCPs/scripts continue to reach
//!     it for project-config + secret resolution)
//!   - Owns service supervision (Weaviate / Ollama / code-embed health +
//!     auto-restart on crash) — moves out of the launcher's
//!     services::watcher in later steps
//!   - Hosts the new `GET /api/v1/projects/{id}/config` resolver endpoint
//!     (Step 14) plus the existing `/projects/{id}/env` secrets resolver
//!     (ported from in-launcher hub in Step 4)
//!
//! Step 3b leaves this as a stub that compiles + serves `/health` only.
//! Steps 4, 5, 11, 14, 15 flesh it out.

use axum::{routing::get, Router};
use std::net::SocketAddr;

#[tokio::main]
async fn main() {
    // Default port matches the existing in-launcher hub's DEFAULT_PORT.
    // Step 5 wires the lockfile + port-discovery state machine; for now
    // this is a hardcoded smoke binding so cargo build produces a real
    // artifact in `target/release/vct-hub`.
    let port: u16 = std::env::var("VCT_HUB_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(7700);

    let app = Router::new().route("/health", get(|| async { "ok" }));
    let addr = SocketAddr::from(([127, 0, 0, 1], port));

    eprintln!("[vct-hub] v0.2.21 stub starting on http://{}", addr);
    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[vct-hub] failed to bind {}: {}", addr, e);
            std::process::exit(1);
        }
    };
    if let Err(e) = axum::serve(listener, app).await {
        eprintln!("[vct-hub] serve error: {}", e);
        std::process::exit(1);
    }
}
