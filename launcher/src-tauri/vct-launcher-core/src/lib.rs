//! vct-launcher-core
//!
//! Shared library for the vct-launcher Tauri GUI binary and the vct-hub
//! detached binary. Holds data, manifest parsing, secrets access, path
//! resolution, runtime detection — anything both binaries need.
//!
//! Migrated from `launcher/src-tauri/src/` in v0.2.21 (Step 3d). The
//! launcher and hub both depend on this crate via path dependencies in
//! their respective Cargo.toml.

pub mod config;
pub mod db;
pub mod manifest;
pub mod paths;
pub mod registry;
pub mod secrets;
pub mod services;
pub mod state;
pub mod types;
