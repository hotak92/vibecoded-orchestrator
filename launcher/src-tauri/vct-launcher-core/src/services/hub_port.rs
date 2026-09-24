// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Where this machine's vct-hub is listening — the Rust answer for code that
//! must describe the RUNNING hub (v0.2.97, lane T).
//!
//! Moved here from `vct_hub::module_supervisor::resolve_hub_base_port` (the
//! v0.2.61 Option H helper that builds a container's `VCT_HUB_BASE_URL`) so
//! the manifest placeholder `{hub_port}` (`manifest::PlaceholderCtx::resolve`)
//! resolves through the SAME ladder instead of a second copy. Both crates
//! already depend on this one.
//!
//! ## Order, and why it differs from the client resolvers
//!
//! 1. `<vct_root_dir>/hub.port` — the port the hub actually BOUND, written by
//!    `vct_hub::server::write_port_file` after binding. Authoritative: the
//!    bind port comes from `VCT_HUB_PORT` in the hub's own environment, else
//!    the `vct-hub-api` module's global `VCT_HUB_PORT` setting, else 7700
//!    (`vct_hub::server::resolve_bind_port`), and the hub walks past a taken
//!    port — so no caller can re-derive it.
//! 2. `$VCT_HUB_PORT` — the configured port, if the file is missing or
//!    unreadable.
//! 3. [`DEFAULT_HUB_PORT`].
//!
//! The Python/shell CLIENT resolvers (`vco_lib/project_config.py`,
//! `vco_lib/hub_ensure.py`, `templates/scripts/vct_project_config.sh`) put
//! `$VCT_HUB_PORT` FIRST on purpose: there it is a caller's explicit pin (a
//! test harness pointing one process at one hub). Here the question is "where
//! is the hub that is running", which only the port file answers.

use std::path::{Path, PathBuf};

use crate::paths::vct_root_dir;

/// The hub's documented default port. The hub binds it when neither
/// `VCT_HUB_PORT` nor the `vct-hub-api` setting names another.
pub const DEFAULT_HUB_PORT: u16 = 7700;

/// The env var — and the `vct-hub-api` setting key — naming the port.
pub const HUB_PORT_ENV: &str = "VCT_HUB_PORT";

/// Basename of the file the hub writes under `vct_root_dir()` after binding.
pub const HUB_PORT_FILE: &str = "hub.port";

/// `<vct_root_dir>/hub.port`.
pub fn hub_port_file() -> PathBuf {
    vct_root_dir().join(HUB_PORT_FILE)
}

/// STRICT: the port in `<vct_root_dir>/hub.port`, or an error naming why
/// there is none. For a client about to TALK to the running hub (the
/// launcher's hub proxy, module-DB client, secrets and default-weights
/// calls, the post-install readiness poll): no file means no running hub
/// to talk to, and guessing 7700 would send the request — and the
/// `hub.token` beside it — to whatever else holds that port. Use
/// [`resolve_hub_port`] only to DESCRIBE where the hub is.
///
/// v0.2.97 (lane T): the one home for this read; five launcher commands
/// and the installer's poll each carried their own copy.
pub fn read_hub_port_file() -> Result<u16, String> {
    read_hub_port_file_in(&vct_root_dir())
}

/// [`read_hub_port_file`] under an explicit VCT root (the installer polls a
/// root it was handed, not necessarily this process's).
pub fn read_hub_port_file_in(vct_root: &Path) -> Result<u16, String> {
    let raw = std::fs::read_to_string(vct_root.join(HUB_PORT_FILE))
        .map_err(|e| format!("read hub.port: {e}"))?;
    raw.trim()
        .parse::<u16>()
        .map_err(|e| format!("parse hub.port: {e}"))
}

/// The running hub's port: `hub.port` → `$VCT_HUB_PORT` → 7700 (see the
/// module doc). Never fails; a garbled file or env value falls through to
/// the next source.
pub fn resolve_hub_port() -> u16 {
    if let Ok(port) = read_hub_port_file() {
        return port;
    }
    std::env::var(HUB_PORT_ENV)
        .ok()
        .and_then(|p| p.trim().parse().ok())
        .unwrap_or(DEFAULT_HUB_PORT)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_env::state_dir_guard_with;

    /// Moved with the helper from `vct_hub::module_supervisor` (v0.2.61):
    /// the port file is authoritative over the env.
    #[test]
    fn port_file_wins_over_env() {
        let guard = state_dir_guard_with(&[(HUB_PORT_ENV, Some("9999"))]);
        std::fs::write(guard.path().join(HUB_PORT_FILE), "7711\n").unwrap();
        assert_eq!(resolve_hub_port(), 7711);
    }

    #[test]
    fn env_then_default_without_a_port_file() {
        let _guard = state_dir_guard_with(&[(HUB_PORT_ENV, Some("8800"))]);
        assert_eq!(resolve_hub_port(), 8800, "env used when no port file");
        // The guard holds GLOBAL_ENV_MUTEX and restores the prior value on drop.
        unsafe { std::env::remove_var(HUB_PORT_ENV) };
        assert_eq!(resolve_hub_port(), DEFAULT_HUB_PORT, "default when neither is present");
    }

    /// The strict read never guesses: no file (even with the env set) or a
    /// garbled one is an error saying which.
    #[test]
    fn strict_read_is_the_file_or_an_error() {
        let guard = state_dir_guard_with(&[(HUB_PORT_ENV, Some("8800"))]);
        let missing = read_hub_port_file().unwrap_err();
        assert!(missing.starts_with("read hub.port:"), "{missing}");
        std::fs::write(guard.path().join(HUB_PORT_FILE), "junk").unwrap();
        assert!(read_hub_port_file().unwrap_err().starts_with("parse hub.port:"));
        std::fs::write(guard.path().join(HUB_PORT_FILE), " 7712\n").unwrap();
        assert_eq!(read_hub_port_file(), Ok(7712));
        assert_eq!(read_hub_port_file_in(guard.path()), Ok(7712));
    }

    #[test]
    fn a_garbled_port_file_falls_through_to_env() {
        let guard = state_dir_guard_with(&[(HUB_PORT_ENV, Some("8801"))]);
        std::fs::write(guard.path().join(HUB_PORT_FILE), "not-a-port").unwrap();
        assert_eq!(resolve_hub_port(), 8801);
    }
}
