//! Per-service adoption state — thin re-export of the canonical module.
//!
//! v0.2.62: the implementation MOVED to
//! `vct_launcher_core::services::adoption` so the hub-side infra
//! watchdog (`vct-hub::infra_watchdog`) can read the same
//! adopt/parallel/refuse decisions the launcher GUI writes, without a
//! duplicate copy of the `services.toml` schema. The module was always
//! pure (serde + toml + `vct_root_dir()`); only its file location was
//! launcher-bound. This shim keeps every existing
//! `crate::services::adoption::*` call-site in the launcher compiling
//! unchanged.
//!
//! All public items (`AdoptionMode`, `ServiceAdoption`, `AdoptionState`,
//! `config_path`, `read`, `write`) resolve through here.

pub use vct_launcher_core::services::adoption::*;
