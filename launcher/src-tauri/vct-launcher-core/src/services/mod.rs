//! Container-runtime infrastructure shared between launcher GUI + vct-hub.
//!
//! v0.2.21 split out of the launcher's `services/` directory. Only the
//! runtime-agnostic / Tauri-free helpers live here; the launcher's
//! `services/adoption.rs`, `services/settings_json_watcher.rs`, and
//! `services/watcher.rs` (the supervisor — relocating to vct-hub in
//! Step 4) remain in the launcher crate.

pub mod picker;
pub mod runtime;
