//! Local-side machine-config loaded once at launcher startup.
//!
//! The launcher manages a `LocalConfig` as Tauri state
//! (`app.manage(LocalConfig::load())`) so a future per-machine, launcher-only
//! knob that is NOT product-fixed has one home, with one precedence rule:
//! env-var override → `vct-config.toml` next to the launcher binary →
//! compiled default.
//!
//! ## What is NOT here any more: the Weaviate URL (v0.2.97)
//!
//! Until v0.2.97 this struct carried `weaviate_url` (`VCT_WEAVIATE_URL` →
//! `WEAVIATE_URL` → `vct-config.toml` → `http://localhost:8081`), and its
//! statement was the TOP leg of the service-endpoint chain. That made a
//! value shipped in the release archive's `vct-config.toml` outrank every
//! adoption, and it let a launcher started from one project's hook serve that
//! project's projected `WEAVIATE_URL` to everyone.
//!
//! Where the core services are reached is now the launcher DB's
//! `service_endpoints` rows, resolved by
//! [`crate::services::service_endpoints`] (row → compiled default). No
//! endpoint lives in this struct, and nothing in the launcher or the hub
//! reads `vct-config.toml`'s `weaviate_url` or `VCT_WEAVIATE_URL`. A value a
//! user put in either is imported into the row once by the v0.2.97 update
//! (`vco_lib.service_reconcile`), and [`LocalConfig::load`] warns at startup
//! when the retired env vars are still exported.
//!
//! What stays OUT of scope (intentionally NOT externalized):
//!   * `commands::licensing::DEFAULT_VALIDATE_TIER_URL` — product-fixed
//!     Supabase functions URL; staging override stays
//!     `VCT_VALIDATE_TIER_URL` (env-only, no file).
//!   * `commands::installer::ORCHESTRATOR_REPO` — canonical GitHub repo
//!     URL; not a per-machine value.
//!   * Service endpoints — the `service_endpoints` rows (above).

/// Local-side per-machine configuration loaded at launcher startup.
///
/// Carries no field today (the one it had — the Weaviate URL — moved to the
/// `service_endpoints` rows in v0.2.97). Add a field here when externalizing
/// a launcher-only local default; each new field MUST document its env-var
/// override, have a compiled default, and resolve env > file > default in
/// [`LocalConfig::load`].
#[derive(Debug, Clone, Default)]
pub struct LocalConfig {}

impl LocalConfig {
    /// Load the launcher's local config. Never fails.
    ///
    /// Also the launcher's one startup check for the endpoint env vars no
    /// resolver reads any more (`VCT_WEAVIATE_URL`, `VCT_OLLAMA_URL`): each
    /// one still exported gets a WARNING naming the replacement, so a user
    /// who relied on it learns why it stopped mattering.
    pub fn load() -> Self {
        crate::services::service_endpoints::warn_retired_endpoint_env("launcher");
        LocalConfig {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The struct states no endpoint, whatever the environment says: the
    /// retired `VCT_WEAVIATE_URL` / `WEAVIATE_URL` are not read into it (the
    /// startup warning is the only thing they cause).
    #[test]
    fn load_reads_no_endpoint_from_the_environment() {
        let _g = crate::test_env::state_dir_guard_with(&[
            ("VCT_WEAVIATE_URL", Some("http://retired:1")),
            ("WEAVIATE_URL", Some("http://transport:2")),
        ]);
        let cfg = LocalConfig::load();
        assert_eq!(format!("{:?}", cfg), "LocalConfig");
    }
}
