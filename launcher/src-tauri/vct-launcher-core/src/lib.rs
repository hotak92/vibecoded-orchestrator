//! vct-launcher-core
//!
//! Shared library for the vct-launcher Tauri GUI binary and the vct-hub
//! detached binary. Holds data, manifest parsing, secrets access, path
//! resolution, runtime detection — anything both binaries need.
//!
//! Migrated from `launcher/src-tauri/src/` in v0.2.21 (Step 3d). The
//! launcher and hub both depend on this crate via path dependencies in
//! their respective Cargo.toml.

pub mod bundled_versions;
pub mod config;
pub mod db;
// v0.2.49: shared license + machine-binding helpers used by the launcher
// GUI AND vct-hub. Promoted out of the launcher crate so the hub-side
// supervisor (Phase 3 auth port) can call the same keychain read +
// machine_id_hash as the install path. See knowledge/concepts/
// supervisor-image-resolution-variant-gap-2026-06-04.md.
pub mod licensing;
pub mod manifest;
pub mod orchestrator_manifest;
pub mod paths;
pub mod process;
// v0.2.81 GAP-CG-3: canonical project-name → Weaviate-class-prefix
// sanitizer. Promoted from the launcher app crate so vct-hub's
// config_api can call the SAME rule for its code-graph prefix fallback
// (was a divergent inline sanitizer over the slug). The app crate
// re-exports this via `pub use vct_launcher_core::project_naming;`.
pub mod project_naming;
pub mod python_resolve;
pub mod registry;
// v0.2.80 A4: the single-line secret-value shape predicate lives in core so
// the `secrets::set` write chokepoint (this crate) can call it; the app crate
// re-exports it from here. See `secret_value_shape.rs` header.
pub mod secret_value_shape;
pub mod secrets;
pub mod services;
pub mod state;
#[cfg(any(test, debug_assertions))]
pub mod test_env;
pub mod time;
pub mod types;
