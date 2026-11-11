// SPDX-License-Identifier: AGPL-3.0-or-later
//! Preferences → Services: "Restart stopped services automatically".
//!
//! v0.2.106: the services watcher's module doc has promised this toggle
//! since v0.2.6 ("Configurable via the app_state key
//! `launcher.services_watcher_enabled` … The Preferences → Services panel
//! toggles this") but nothing ever wrote the key from the GUI. This module
//! is that GUI switch. Same shape as `commands::engine_autostart_pref`
//! (the v0.2.103 precedent): the launcher WRITES the row, the READERS are
//! the launcher's own watcher loop (every tick) and the hub's infra
//! watchdog (every tick, in `vct_hub::infra_watchdog::watcher_enabled`) —
//! ONE switch disables BOTH automatic restarters, and neither process
//! needs a restart to pick a change up.

use tauri::{command, State};

use crate::db::Db;
use crate::services::watcher::APP_STATE_KEY_WATCHER_ENABLED;

/// Shipped default: ON (self-healing is the shipped behaviour; an install
/// that never opens Preferences gets it with no migration).
///
/// MUST MATCH the watcher's read-side default (`services/watcher.rs`
/// resolves an absent row to enabled) and the hub's
/// (`infra_watchdog::watcher_enabled` does the same).
pub const DEFAULT_SERVICES_WATCHER: bool = true;

/// PURE: the effective preference given whatever `app_state` holds.
/// `None` is "no row" — the shipped default, NOT "off".
pub(crate) fn resolve_services_watcher(stored: Option<bool>) -> bool {
    stored.unwrap_or(DEFAULT_SERVICES_WATCHER)
}

/// Read the preference for the Preferences page. A DB error resolves to
/// the shipped default — the toggle must show what the readers will do,
/// and what they do when the row cannot be read is the default (the hub
/// reader is deliberately fail-open for the same reason).
#[command]
pub async fn get_services_watcher_enabled(db: State<'_, Db>) -> Result<bool, String> {
    let stored = db
        .app_state_get_bool(APP_STATE_KEY_WATCHER_ENABLED)
        .unwrap_or(None);
    Ok(resolve_services_watcher(stored))
}

/// Write the preference. Takes effect on the NEXT watcher/watchdog tick
/// (both read the row on every tick) — nothing is started or stopped here.
#[command]
pub async fn set_services_watcher_enabled(
    enabled: bool,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.app_state_set_bool(APP_STATE_KEY_WATCHER_ENABLED, enabled)?;
    tracing::info!(
        "[vct] services watcher set to {} (applies from the next watcher / \
         watchdog tick; both restarters read the same row)",
        enabled,
    );
    Ok(enabled)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Round-trip through a real (in-memory) launcher.db — the row this
    /// toggle writes is the row BOTH automatic restarters read.
    #[test]
    fn services_watcher_toggle_round_trips() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(
            db.app_state_get_bool(APP_STATE_KEY_WATCHER_ENABLED).unwrap(),
            None,
            "absent row = the shipped default"
        );
        db.app_state_set_bool(APP_STATE_KEY_WATCHER_ENABLED, false).unwrap();
        assert_eq!(
            db.app_state_get_bool(APP_STATE_KEY_WATCHER_ENABLED).unwrap(),
            Some(false)
        );
        assert!(!resolve_services_watcher(
            db.app_state_get_bool(APP_STATE_KEY_WATCHER_ENABLED).unwrap()
        ));
        db.app_state_set_bool(APP_STATE_KEY_WATCHER_ENABLED, true).unwrap();
        assert!(resolve_services_watcher(
            db.app_state_get_bool(APP_STATE_KEY_WATCHER_ENABLED).unwrap()
        ));
    }

    /// No row means the shipped default, which is ON — an install that has
    /// never opened Preferences keeps the self-healing behaviour.
    #[test]
    fn no_row_means_the_shipped_default_which_is_on() {
        assert!(resolve_services_watcher(None));
        assert!(DEFAULT_SERVICES_WATCHER);
        assert!(!resolve_services_watcher(Some(false)));
    }

    /// The key is the ONE both restarters read — pinned by literal so a
    /// rename here cannot silently orphan the hub's reader (the hub crate
    /// is not linkable from this crate; `infra_watchdog.rs` carries the
    /// same literal).
    #[test]
    fn the_key_is_the_shared_one() {
        assert_eq!(APP_STATE_KEY_WATCHER_ENABLED, "launcher.services_watcher_enabled");
    }
}
