//! Container-services lifecycle infrastructure for the launcher.
//!
//! Submodules:
//!   - [`runtime`]: detect Podman/Docker + compose form (subcommand vs
//!     standalone), cached per launcher session. *Lives in
//!     `vct-launcher-core::services::runtime` as of v0.2.21 (Step 3d).*
//!   - [`picker`]: container picker for the
//!     `com.docker.compose.service=<name>` label-collision case. *Also
//!     in `vct-launcher-core::services::picker` as of v0.2.21.*
//!   - [`adoption`]: persist the user's adopt-vs-parallel choice for
//!     externally-managed services in `~/.vct/services.toml`. *Moved to
//!     `vct_launcher_core::services::adoption` in v0.2.62 so the hub's
//!     infra watchdog reads the same decisions; the launcher submodule
//!     is now a thin `pub use` re-export.*
//!   - [`settings_json_watcher`]: launcher-side reactive watcher for
//!     `.claude/settings.json` edits. Stays in the launcher.
//!   - [`watcher`]: 30s polling supervisor that auto-restarts crashed
//!     services. Stays in the launcher for v0.2.21 Step 3 (relocates to
//!     `vct-hub::supervisor` in Step 4).
//!
//! Tauri commands that wire these into the UI live in
//! `commands::lifecycle` (services_status, services_start_all, etc.).

// Re-export the core halves so `crate::services::runtime::*` and
// `crate::services::picker::*` continue to resolve from anywhere in
// the launcher without per-file import rewrites.
pub use vct_launcher_core::services::picker;
pub use vct_launcher_core::services::runtime;

pub mod adoption;
/// v0.2.91 WP-A — dist-binary freshness: the ONE home for the pre-pull
/// rename, its non-clobbering revert, `<target>.new` staging, the shared
/// post-update handoff tail, and the at-rest (boot / update-check)
/// reconcile that heals a launcher frozen on an old binary.
pub mod binary_freshness;
pub mod deferral;
pub mod settings_json_watcher;
pub mod vco_lib_bridge;
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
