//! Container-services lifecycle infrastructure for the launcher.
//!
//! Submodules:
//!   - [`runtime`]: detect Podman/Docker + compose form (subcommand vs
//!     standalone), cached per launcher session.
//!   - [`adoption`]: persist the user's adopt-vs-parallel choice for
//!     externally-managed services in `~/.vct/services.toml`.
//!
//! Tauri commands that wire these into the UI live in
//! `commands::lifecycle` (services_status, services_start_all, etc.).

pub mod adoption;
pub mod picker;
pub mod runtime;
pub mod watcher;

use tauri::{AppHandle, Runtime};

/// Stop all VCT services (Weaviate, Ollama, etc.) — best-effort.
///
/// TODO: wire — the doc says this is for "Quit and stop services"
/// confirmation but no such dialog exists yet. When a quit-confirmation
/// is added, it should call this before `app.exit(0)`. Failures are
/// logged to stderr and the function returns `Ok(())` regardless: the
/// user explicitly asked to quit and a flaky container runtime must not
/// strand them in a half-quit state.
///
/// Implementation: delegates to `commands::lifecycle::services_stop_all`
/// which runs `<runtime> compose stop` (no `--volumes` flag — volumes
/// are preserved). Idempotent: succeeds even when nothing is up.
#[allow(dead_code)]
pub async fn stop_all<R: Runtime>(_app: &AppHandle<R>) -> Result<(), String> {
    if let Err(e) = crate::commands::lifecycle::services_stop_all().await {
        // Surface to stderr but DO NOT propagate — the user clicked
        // "Quit and stop services" and we must not block app.exit().
        eprintln!("[vct] services::stop_all: best-effort stop failed: {}", e);
    }
    Ok(())
}
