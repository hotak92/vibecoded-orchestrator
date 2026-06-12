//! Container-runtime infrastructure shared between launcher GUI + vct-hub.
//!
//! v0.2.21 split out of the launcher's `services/` directory. Only the
//! runtime-agnostic / Tauri-free helpers live here; the launcher's
//! `services/adoption.rs`, `services/settings_json_watcher.rs`, and
//! `services/watcher.rs` (the supervisor — relocating to vct-hub in
//! Step 4) remain in the launcher crate.

pub mod picker;
pub mod runtime;

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
