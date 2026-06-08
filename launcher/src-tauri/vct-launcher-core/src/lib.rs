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
pub mod registry;
pub mod secrets;
pub mod services;
pub mod state;
#[cfg(any(test, debug_assertions))]
pub mod test_env;
pub mod types;
