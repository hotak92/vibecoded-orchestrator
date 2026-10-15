// SPDX-License-Identifier: AGPL-3.0-or-later
//! Preferences → Startup: "Start the container engine automatically".
//!
//! v0.2.103, review R19 F4 (owner rule "every change carries its GUI side"):
//! the engine auto-start's behaviour lives entirely in
//! `vco_lib/container_engine_start.py`, spawned detached by the hub and by
//! this launcher — processes that never see `.claude/settings.json env` or
//! `.claude/env`, so an env knob alone is unreachable for them. This module
//! is the GUI's SWITCH: a launcher.db `app_state` row the Python home (and
//! the Rust pre-check in `vct-launcher-core::services::engine_autostart`)
//! read.
//!
//! Same shape as `commands::session_autostart` (the v0.2.95 precedent),
//! deliberately: the launcher WRITES this row, the READERS are other
//! processes at other moments, so the default lives in code on every side
//! (absent row = ON), there is no process-level cache, and the truthiness
//! rule must match the readers' (`app_state_get_bool` accepts `"true" |
//! "1"`; `container_engine_start.pref_enabled` accepts the same two).

use tauri::{command, State};

use crate::db::Db;

/// `app_state` key holding the preference.
///
/// MUST MATCH `vco_lib/container_engine_start.py::APP_STATE_ENGINE_AUTOSTART`
/// and `vct-launcher-core/src/services/engine_autostart.rs::
/// APP_STATE_ENGINE_AUTOSTART` (pinned on both sides).
pub const APP_STATE_ENGINE_AUTOSTART: &str = "engine.autostart";

/// Shipped default: ON (the owner ruling this implements).
///
/// MUST MATCH the Python `DEFAULT_ENGINE_AUTOSTART` and the core constant.
pub const DEFAULT_ENGINE_AUTOSTART: bool = true;

/// PURE: the effective preference given whatever `app_state` holds.
///
/// `None` is "no row" — the shipped default, NOT "off": an install that
/// never opens Preferences gets the shipped behaviour with no migration.
pub(crate) fn resolve_engine_autostart(stored: Option<bool>) -> bool {
    stored.unwrap_or(DEFAULT_ENGINE_AUTOSTART)
}

/// Read the preference for the Preferences page. A DB error resolves to the
/// shipped default — the toggle must show what the readers will do, and
/// what they do when the row cannot be read is the default.
#[command]
pub async fn get_container_engine_autostart(db: State<'_, Db>) -> Result<bool, String> {
    let stored = db
        .app_state_get_bool(APP_STATE_ENGINE_AUTOSTART)
        .unwrap_or(None);
    Ok(resolve_engine_autostart(stored))
}

/// Write the preference. Takes effect at the NEXT engine-autostart moment
/// (session start / launcher start / hub start) — nothing is started or
/// stopped here.
#[command]
pub async fn set_container_engine_autostart(
    enabled: bool,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.app_state_set_bool(APP_STATE_ENGINE_AUTOSTART, enabled)?;
    tracing::info!(
        "[vct] container engine autostart set to {} (applies from the next \
         session / launcher / hub start)",
        enabled,
    );
    Ok(enabled)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_row_means_the_shipped_default_which_is_on() {
        assert!(resolve_engine_autostart(None));
        assert!(DEFAULT_ENGINE_AUTOSTART);
    }

    #[test]
    fn an_explicit_off_is_honoured() {
        assert!(!resolve_engine_autostart(Some(false)));
    }

    #[test]
    fn the_key_matches_the_python_home_and_the_core_reader() {
        // Cross-language pin; the Python side asserts the same literals
        // against this file (tests/test_container_engine_start.py).
        assert_eq!(APP_STATE_ENGINE_AUTOSTART, "engine.autostart");
        assert_eq!(
            APP_STATE_ENGINE_AUTOSTART,
            vct_launcher_core::services::engine_autostart::APP_STATE_ENGINE_AUTOSTART
        );
    }
}
