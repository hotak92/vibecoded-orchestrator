//! vct-launcher-core
//!
//! Shared library for the vct-launcher Tauri GUI binary and the vct-hub
//! detached binary. Holds data, manifest parsing, secrets access, path
//! resolution, runtime detection — anything both binaries need.
//!
//! Module structure (filled in by Step 3e of the v0.2.21 workspace refactor):
//!   - db        — SQLite schema, migrations, model rows
//!   - manifest  — vct-module.json parser + types
//!   - secrets   — OS keychain wrapper (per-project / global / shared scopes)
//!   - paths     — vct_root_dir() + state-dir resolution
//!   - config    — launcher.toml loader
//!   - state     — small shared types (AppManager)
//!   - types     — shared serde types (ServiceEntry, etc.)
//!   - registry  — service registry helpers
//!   - services::runtime + services::picker — podman/docker selection

// Placeholder until Step 3e populates the modules.
