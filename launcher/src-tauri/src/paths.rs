//! Filesystem path resolution for launcher state.
//!
//! All launcher state files (launcher.db, hub.port, hub.db, projects.json,
//! services.json, services.toml, orchestrator.json, modules/, data/, logs/,
//! bundled_manifests/, launcher-update-state.json) live under one root.
//! In production that root is `~/.vct/`. Maintainers running a dev launcher
//! against an in-development VCO clone can override with the env var
//! `VCT_STATE_DIR` so dev state never contaminates production state.
//!
//! Why this matters: the launcher binary path doesn't determine state
//! location — `~/.vct/` is shared globally. A dev launcher run from
//! `~/Desktop/PROGETTI/VCO_dev/` and a production launcher installed at
//! `~/.local/bin/` would otherwise see the same projects, the same
//! KG bindings, the same secrets, etc. — easy to clobber a real project
//! while testing in-development changes.
//!
//! Usage:
//!   VCT_STATE_DIR=$HOME/.vct-dev /path/to/dev/vct-launcher
//!
//! With no env var set, behaviour is identical to the previous hardcoded
//! `~/.vct/` resolution. Both Rust and Python sides honour the same
//! variable (Python: `vco_lib.paths::vct_root_dir()`).

use std::path::PathBuf;

/// Returns the launcher's state-root directory.
///
/// Resolution order:
///   1. `VCT_STATE_DIR` env var (absolute path; created if missing on first
///      use by callers that need it — this function only resolves, doesn't
///      mkdir).
///   2. `$HOME/.vct/` — the production default.
///   3. Relative `./.vct/` — last-resort fallback if home_dir() fails.
///      Mirrors the existing fallback at `db/mod.rs:36`.
pub fn vct_root_dir() -> PathBuf {
    if let Ok(custom) = std::env::var("VCT_STATE_DIR") {
        if !custom.is_empty() {
            return PathBuf::from(custom);
        }
    }
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".vct"))
        .unwrap_or_else(|| PathBuf::from(".vct"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // VCT_STATE_DIR is process-wide; serialise tests that mutate it so
    // parallel runs don't observe each other.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_env<F: FnOnce()>(key: &str, val: Option<&str>, f: F) {
        let _g = SERIALIZE.lock().unwrap();
        let prev = std::env::var(key).ok();
        match val {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
        f();
        match prev {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
    }

    #[test]
    fn vct_state_dir_overrides_home_default() {
        with_env("VCT_STATE_DIR", Some("/tmp/vct-test-override"), || {
            assert_eq!(vct_root_dir(), PathBuf::from("/tmp/vct-test-override"));
        });
    }

    #[test]
    fn empty_vct_state_dir_falls_back_to_home_default() {
        with_env("VCT_STATE_DIR", Some(""), || {
            // Empty string must be treated as "not set" — otherwise an
            // accidental `export VCT_STATE_DIR=` (no value) would
            // resolve state to the literal empty string and break.
            let resolved = vct_root_dir();
            assert!(
                resolved.ends_with(".vct"),
                "expected ~/.vct fallback, got {:?}",
                resolved
            );
        });
    }

    #[test]
    fn no_env_var_resolves_to_dot_vct_under_home() {
        with_env("VCT_STATE_DIR", None, || {
            let resolved = vct_root_dir();
            assert!(
                resolved.ends_with(".vct"),
                "expected ~/.vct, got {:?}",
                resolved
            );
        });
    }
}
