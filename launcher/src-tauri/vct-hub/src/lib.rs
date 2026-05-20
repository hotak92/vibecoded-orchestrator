//! vct-hub — detached HTTP server for VCT.
//!
//! Library surface so integration tests and the binary share the same
//! module tree. v0.2.21 Step 4 ported these from
//! `launcher/src-tauri/src/hub/` into this crate so the hub becomes a
//! free-standing binary that doesn't depend on Tauri at all.
//!
//! Modules:
//!   * `auth`               — bearer-token middleware + token persistence
//!   * `db`                 — hub.db SQLite store (app-registry/messages)
//!   * `api`                — message-bus + app-registry routes
//!   * `cli_api`            — generic CLI proxies (license/telemetry/etc)
//!   * `config_api`         — project-config resolver (v0.2.21 Step 14)
//!   * `lifecycle_api`      — services + per-module lifecycle routes (v0.2.21 Step 15)
//!   * `modules_api`        — module install + secrets resolver routes
//!   * `project_state_api`  — project state mirror of Tauri commands
//!   * `server`             — wiring: bind, layer order, port discovery
//!
//! Supervisor logic (services-watcher + module-supervisor) does NOT
//! land in this crate during Step 4. See plan §"Step 4 replan
//! (2026-05-20)" — supervisor relocation consolidates into Step 24
//! (Stream B) where it pairs with the rl_service supervisor that's
//! already heading to `vct-hub/src/module_supervisor.rs`.

pub mod api;
pub mod auth;
pub mod boot;
pub mod cli;
pub mod cli_api;
pub mod config_api;
pub mod db;
pub mod lifecycle;
pub mod lifecycle_api;
pub mod lockfile;
pub mod module_supervisor;
pub mod modules_api;
pub mod project_state_api;
pub mod server;
pub mod weaviate_probe;
