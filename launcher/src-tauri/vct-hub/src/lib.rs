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
//! (Stream B) where it pairs with the module_service supervisor that's
//! already heading to `vct-hub/src/module_supervisor.rs`.

pub mod api;
pub mod auth;
pub mod boot;
pub mod cli;
pub mod cli_api;
pub mod config_api;
pub mod db;
// v0.2.54 Track J — shared JSON error envelope, extracted from four
// byte-identical `error_response` copies in the *_api modules.
pub mod http_error;
// v0.2.69 (hub-staleness home #3): build identity (git fingerprint +
// version) for the identity-aware start path and the /health endpoint.
pub mod identity;
// v0.2.62: continuous infra-container watchdog. Spawned from
// `server::start_hub_server`; restarts down `vco_weaviate` /
// `vco_ollama` / `vco_code_embed` that VCO manages (not user-adopted /
// paused), with crash-loop backoff. The launcher-only path used to be
// the sole restarter (boot-time + SessionStart hook), so a mid-session
// infra death went unhealed until the launcher restarted.
pub mod infra_watchdog;
pub mod lifecycle;
pub mod lifecycle_api;
pub mod lockfile;
pub mod mcp_tool_grants_api;
pub mod module_identity;
pub mod module_supervisor;
pub mod module_db_api;
pub mod modules_api;
// v0.2.76 Part 4 — per-project resolver tokens (`hub.token.<id>`) minted
// at startup so `/env` + `/config` callers present a project-scoped
// credential instead of the coarse hub-wide `hub.token`.
pub mod project_tokens;
pub mod project_state_api;
pub mod retrieval_tuning_io;
pub mod rl_events_api;
pub mod secrets_api;
pub mod server;
pub mod weaviate_probe;
pub mod weaviate_schema_probe;
