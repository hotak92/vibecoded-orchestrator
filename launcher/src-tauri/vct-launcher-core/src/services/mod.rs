//! Container-runtime infrastructure shared between launcher GUI + vct-hub.
//!
//! v0.2.21 split out of the launcher's `services/` directory. Only the
//! runtime-agnostic / Tauri-free helpers live here; the launcher's
//! `services/settings_json_watcher.rs` and `services/watcher.rs` (the
//! GUI-side supervisor) remain in the launcher crate.

pub mod picker;
pub mod runtime;

// v0.2.62: per-service adoption state (`<vct_root_dir>/services.toml`).
// MOVED here from `launcher/src-tauri/src/services/adoption.rs` so the
// hub-side infra watchdog (`vct-hub::infra_watchdog`) can read the same
// adopt/parallel/refuse decisions the launcher GUI persists, WITHOUT a
// second copy of the schema. The module is pure (serde + toml + the
// shared `crate::paths::vct_root_dir()` lookup) — it never depended on
// Tauri, only on its file location. The launcher's
// `src/services/adoption.rs` is now a thin `pub use` re-export so its
// many call-sites compile unchanged. The watchdog NEVER touches a
// service whose adoption mode is Adopt / Parallel / Refuse.
pub mod adoption;

// v0.2.47: shared per-paid-module container helpers. Previously two
// near-identical copies lived in launcher/src/commands/module_service.rs
// and vct-hub/src/module_supervisor.rs; the drift between them caused
// the supervisor-image-resolution-variant-gap bug fixed in this release.
// See knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md.
pub mod container_runtime;
pub mod gpu_mode;

// v0.2.54 Track I: per-boot bearer-token primitives (generate /
// persist-0o600 / constant-time-compare / Bearer parse). Extracted
// from vct-hub's auth.rs so the launcher's diagrams local server
// (diagrams.token) reuses the same implementation as hub.token.
pub mod boot_token;
