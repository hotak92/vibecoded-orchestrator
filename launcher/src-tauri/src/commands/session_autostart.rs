// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Preferences → Startup: "Start the launcher with a Claude Code session".
//!
//! v0.2.95, ruling **R2** ("the launcher and the hub must auto-start when VS
//! Code starts"). The behaviour itself lives entirely outside this process —
//! `vco_lib/launcher_ensure.py`, called from the SessionStart hook that VS
//! Code also runs on `folderOpen`, starts this binary with `--start-hidden`
//! when nothing is running. This module is the *switch* for it.
//!
//! Which makes the shape here unusual, and deliberately so: the launcher
//! WRITES this row and never reads it for its own behaviour. The reader is a
//! different process, at a moment when this one is by definition not running.
//! Three consequences:
//!
//! * **The default lives in code on both sides, not in a seeded row.** Absent
//!   means ON ([`DEFAULT_SESSION_AUTOSTART`], and `launcher_ensure`'s constant
//!   of the same name). An install that has never opened Preferences gets the
//!   shipped behaviour with no migration, and only a user who turns it OFF
//!   ever causes a row to exist.
//! * **There is no process-level cache** like `quit_dialog`'s window prefs.
//!   Caching a value this process never consults would be a second home for
//!   it, and a stale one.
//! * **The truthiness rule must match the reader's.** `app_state_get_bool`
//!   accepts `"true" | "1"`; `launcher_ensure.autostart_enabled` accepts the
//!   same two, and `tests/test_v0295_launcher_ensure.py` pins the pair.

use tauri::{command, State};

use crate::db::Db;

/// `app_state` key holding the preference.
///
/// MUST MATCH `vco_lib/launcher_ensure.py::APP_STATE_SESSION_AUTOSTART`.
pub const APP_STATE_SESSION_AUTOSTART: &str = "launcher.session_autostart";

/// Shipped default: ON, per the owner's 2026-09-10 ruling.
///
/// MUST MATCH `vco_lib/launcher_ensure.py::DEFAULT_SESSION_AUTOSTART`.
pub const DEFAULT_SESSION_AUTOSTART: bool = true;

/// PURE: the effective preference given whatever `app_state` holds.
///
/// `None` is "no row" — the shipped default, NOT "off". That distinction is
/// the whole delivery story for an existing install: after an update the key
/// is still absent, and absent means the new behaviour is on.
pub(crate) fn resolve_session_autostart(stored: Option<bool>) -> bool {
    stored.unwrap_or(DEFAULT_SESSION_AUTOSTART)
}

/// Read the preference for the Preferences page.
///
/// A DB error resolves to the shipped default rather than surfacing as an
/// error toast: the toggle must show what will actually happen, and what will
/// actually happen when the row cannot be read is the default (the hook's
/// reader makes the same call on an unreadable `launcher.db`).
#[command]
pub async fn get_launcher_session_autostart(db: State<'_, Db>) -> Result<bool, String> {
    let stored = db.app_state_get_bool(APP_STATE_SESSION_AUTOSTART).unwrap_or(None);
    Ok(resolve_session_autostart(stored))
}

/// Write the preference. Takes effect at the NEXT session start — nothing is
/// started or stopped here, because this process is the thing being switched.
#[command]
pub async fn set_launcher_session_autostart(
    enabled: bool,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.app_state_set_bool(APP_STATE_SESSION_AUTOSTART, enabled)?;
    tracing::info!(
        "[vct] launcher session autostart set to {} (applies from the next \
         Claude Code session / VS Code folder open)",
        enabled,
    );
    Ok(enabled)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_row_means_the_shipped_default_which_is_on() {
        // The delivery mechanism for every existing install: after `--update`
        // there is still no row, and the new behaviour is active.
        assert!(resolve_session_autostart(None));
        assert!(DEFAULT_SESSION_AUTOSTART);
    }

    #[test]
    fn an_explicit_off_is_honoured() {
        // The leave-alone half: a user who switched it off must not have the
        // default quietly reinstated by an update.
        assert!(!resolve_session_autostart(Some(false)));
    }

    #[test]
    fn an_explicit_on_stays_on() {
        assert!(resolve_session_autostart(Some(true)));
    }

    #[test]
    fn the_key_matches_the_python_reader() {
        // Cross-language pin; the Python side asserts the same literal
        // against this file (tests/test_v0295_launcher_ensure.py).
        assert_eq!(APP_STATE_SESSION_AUTOSTART, "launcher.session_autostart");
    }
}
