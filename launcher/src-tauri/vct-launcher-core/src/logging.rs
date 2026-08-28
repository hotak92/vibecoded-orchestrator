// SPDX-License-Identifier: AGPL-3.0-or-later
// Part of VibeCoded Orchestrator.
//! Diagnostic-logging level resolution — the ONE home for "how verbose
//! should this process be?" across `vct-launcher`, `vct-hub`, and this
//! crate.
//!
//! ## Scope: DIAGNOSTICS ONLY
//!
//! The level resolved here governs `tracing` diagnostics — the running
//! commentary a developer or a support session reads to understand what
//! the launcher/hub just did. It governs NOTHING else. In particular it
//! MUST NOT gate:
//!
//!   * **Telemetry** — `rl_events` writes and the serving path. Those are
//!     data, produced for the reranker, not commentary for a human.
//!   * **Audit trails** — `audit_log` rows and the JSONL trails
//!     (deferral-retry logs, `auto-resolutions.jsonl`). Those are
//!     records: their value is that they exist for every run regardless
//!     of who was watching.
//!
//! A diagnostic line *about* a telemetry/audit write may live at a
//! `tracing` level. The write itself may not. Set at a low verbosity, a
//! level-gated record silently stops existing — which is exactly the
//! failure mode an audit trail is supposed to make impossible.
//!
//! ## Precedence
//!
//! `VCO_LOG_LEVEL` (env) > `logging.level` (launcher.db `app_state`) >
//! `INFO`. Each tier is consulted only if the previous one is absent or
//! unparseable, so a typo (`VCO_LOG_LEVEL=verbose`) degrades to the
//! stored preference rather than to silence or a panic.
//!
//! Accepted values are `error` | `warn` | `info` | `debug`,
//! case-insensitive, surrounding whitespace ignored. `trace` is
//! deliberately NOT accepted: the user-facing preference offers four
//! levels, and silently honouring a fifth would let a value the GUI
//! cannot display (or un-set) become the process's behaviour.
//!
//! ## Why `Level` and not `LevelFilter`
//!
//! [`tracing::Level`] cannot express `OFF`. Returning it makes "logging
//! is never disabled by configuration" a property of the type rather
//! than a rule someone has to remember — no value of `VCO_LOG_LEVEL`,
//! valid or garbage, can silence the process. `Level` converts into
//! `LevelFilter` at the subscriber boundary, so nothing is lost.

use std::sync::OnceLock;

use tracing::Level;
use tracing_subscriber::filter::LevelFilter;
use tracing_subscriber::layer::SubscriberExt as _;
use tracing_subscriber::reload;
use tracing_subscriber::util::SubscriberInitExt as _;

/// Environment variable that overrides the stored preference. Wins over
/// `app_state` so a support session can raise verbosity for ONE run
/// without mutating the user's saved preference.
pub const LOG_LEVEL_ENV: &str = "VCO_LOG_LEVEL";

/// `app_state` key holding the global preference. Dotted, matching the
/// `embedding.active_profile` convention.
///
/// NOTE: this is deliberately NOT the legacy `logging_level` key. That
/// one was removed from the Preferences page in v0.2.91 precisely
/// because nothing ever read it; reusing the name would resurrect stale
/// values written while it was a no-op and make them govern behaviour
/// the user never opted into.
pub const LOG_LEVEL_APP_STATE_KEY: &str = "logging.level";

/// Level applied when neither source supplies a usable value.
pub const DEFAULT_LOG_LEVEL: Level = Level::INFO;

/// Parse one candidate string. `None` means "not a level I accept" —
/// the caller falls through to the next source.
fn parse_level(raw: &str) -> Option<Level> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "error" => Some(Level::ERROR),
        "warn" => Some(Level::WARN),
        "info" => Some(Level::INFO),
        "debug" => Some(Level::DEBUG),
        _ => None,
    }
}

/// Resolve the process diagnostic level from the two configuration
/// sources, in precedence order.
///
/// Pure: no env reads, no I/O, no globals — callers pass what they read.
/// Total: every input combination yields a level; there is no panic path
/// and no way to reach "logging off".
///
/// ```
/// use tracing::Level;
/// use vct_launcher_core::logging::resolve_log_level;
///
/// // env wins over stored
/// assert_eq!(resolve_log_level(Some("debug"), Some("error")), Level::DEBUG);
/// // unparseable env falls through to stored
/// assert_eq!(resolve_log_level(Some("loud"), Some("warn")), Level::WARN);
/// // nothing usable anywhere -> INFO
/// assert_eq!(resolve_log_level(None, Some("")), Level::INFO);
/// ```
pub fn resolve_log_level(env_value: Option<&str>, stored: Option<&str>) -> Level {
    env_value
        .and_then(parse_level)
        .or_else(|| stored.and_then(parse_level))
        .unwrap_or(DEFAULT_LOG_LEVEL)
}

/// Read `VCO_LOG_LEVEL` from the environment. Thin wrapper so call sites
/// name the env var through [`LOG_LEVEL_ENV`] rather than a literal.
pub fn env_log_level() -> Option<String> {
    std::env::var(LOG_LEVEL_ENV).ok()
}

/// Best-effort read of the stored preference straight from `launcher.db`.
///
/// For processes that want the preference BEFORE they own a
/// [`crate::db::Db`] handle — notably `vct-hub`, whose real handle is
/// opened deep inside `start_hub_server()`, long after the first log line
/// it would like to emit. A caller that already holds a `Db` should use
/// `db.app_state_get(LOG_LEVEL_APP_STATE_KEY)` instead of this.
///
/// Deliberately does NOT go through [`crate::db::Db::open`]: that applies
/// migrations, ensures the change log, prunes, and runs a backfill pass.
/// Doing all of that to answer "how chatty should I be?" would put schema
/// work on the startup path of every `vct-hub --status` a SessionStart
/// hook fires. This opens the file, runs one `SELECT`, and drops the
/// connection.
///
/// Soft-fail by construction — EVERY failure mode (no file yet, schema
/// older than the `app_state` migration, database locked by a busy
/// launcher, unreadable row) returns `None`, which the caller resolves to
/// the default level. Logging setup must never be a reason a process
/// fails to start.
///
/// Opened `READ_WRITE` without `CREATE`: read-write because a WAL
/// database needs to create its `-shm` sidecar, which a strictly
/// read-only connection cannot do when no other connection is open; and
/// without `CREATE` so a missing `launcher.db` stays missing instead of
/// being conjured, empty, by a logging probe.
pub fn stored_log_level_from_launcher_db() -> Option<String> {
    let path = crate::db::db_path();
    if !path.exists() {
        return None;
    }
    let conn = rusqlite::Connection::open_with_flags(
        &path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .ok()?;
    conn.query_row(
        "SELECT value FROM app_state WHERE key = ?1",
        rusqlite::params![LOG_LEVEL_APP_STATE_KEY],
        |r| r.get::<_, String>(0),
    )
    .ok()
}

/// The whole resolution for a process that has no `Db` handle yet: env,
/// then the `launcher.db` probe, then the default.
pub fn resolve_process_log_level() -> Level {
    let env_value = env_log_level();
    let stored = stored_log_level_from_launcher_db();
    resolve_log_level(env_value.as_deref(), stored.as_deref())
}

/// Handle to the installed level filter, so [`set_log_level`] can raise
/// or lower verbosity after the subscriber is in place. `None` until an
/// [`init_tracing`] call actually wins the global-subscriber race.
static RELOAD_HANDLE: OnceLock<
    reload::Handle<LevelFilter, tracing_subscriber::Registry>,
> = OnceLock::new();

/// Install the process-wide `tracing` subscriber: compact format, to
/// stderr, capped at `level` through a *reloadable* filter.
///
/// stderr (not stdout) because stdout is a machine contract on several
/// surfaces — `vct-hub --status` prints `running pid=N` there, the CLI
/// helpers emit parse-target lines, and diagnostics interleaved into
/// those streams would corrupt them.
///
/// Idempotent: a second call is a silent no-op rather than a panic, so a
/// binary that initialises early in `main` and again from a later setup
/// path stays correct. The FIRST call wins — which is what makes early
/// initialisation the right habit; use [`set_log_level`] to change the
/// level afterwards.
///
/// ANSI colour is absent by construction: the `ansi` feature is not
/// enabled on the `tracing-subscriber` dependency, and hub output is
/// routinely redirected to a log file where escape sequences are noise.
///
/// ## Why the filter is reloadable
///
/// Both binaries want to log BEFORE they can read the stored preference.
/// The launcher's WebKit preflight must be the first code in `main()`
/// (it calls `std::env::set_var` before any thread exists, which is what
/// makes it sound), while reading `logging.level` needs a `Db::open()`
/// that applies migrations — far too much to hoist ahead of it. The hub
/// has the same shape at a smaller scale. So both do:
///
///   1. `init_tracing(resolve_log_level(env, None))` — env-or-default,
///      no I/O, first thing.
///   2. `set_log_level(resolve_log_level(env, stored))` — once the
///      stored preference is actually readable.
///
/// Without step 2 the app_state preference would be unreadable by the
/// process that persists it, which is the exact "shipped a preference
/// nothing consumes" defect this work exists to fix.
pub fn init_tracing(level: Level) {
    let (filter, handle) = reload::Layer::new(LevelFilter::from(level));
    let installed = tracing_subscriber::registry()
        .with(filter)
        .with(
            tracing_subscriber::fmt::layer()
                .compact()
                .with_writer(std::io::stderr),
        )
        .try_init()
        .is_ok();
    if installed {
        // Only publish the handle for a subscriber we actually own. If
        // some other subscriber won the race, ours is inert and reloading
        // it would silently do nothing while looking like it worked.
        let _ = RELOAD_HANDLE.set(handle);
    }
}

/// Change the level of the filter installed by [`init_tracing`].
///
/// Soft-fail in both directions: a no-op when `init_tracing` never ran
/// (or lost the global-subscriber race), and a no-op if the filter layer
/// has since been dropped. Adjusting diagnostics verbosity must never be
/// able to take a process down.
///
/// Also the hook for honouring a preference change without a restart.
pub fn set_log_level(level: Level) {
    if let Some(handle) = RELOAD_HANDLE.get() {
        let _ = handle.reload(LevelFilter::from(level));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── The precedence matrix: {valid, invalid, missing} at each tier. ──
    //
    // Nine combinations, each asserted explicitly rather than
    // table-driven, so a failure names the exact case in its test name.

    #[test]
    fn env_valid_stored_valid_prefers_env() {
        assert_eq!(resolve_log_level(Some("debug"), Some("error")), Level::DEBUG);
    }

    #[test]
    fn env_valid_stored_invalid_prefers_env() {
        assert_eq!(resolve_log_level(Some("warn"), Some("chatty")), Level::WARN);
    }

    #[test]
    fn env_valid_stored_missing_prefers_env() {
        assert_eq!(resolve_log_level(Some("error"), None), Level::ERROR);
    }

    #[test]
    fn env_invalid_stored_valid_falls_through_to_stored() {
        assert_eq!(resolve_log_level(Some("verbose"), Some("debug")), Level::DEBUG);
    }

    #[test]
    fn env_invalid_stored_invalid_falls_through_to_default() {
        assert_eq!(resolve_log_level(Some("loud"), Some("louder")), Level::INFO);
    }

    #[test]
    fn env_invalid_stored_missing_falls_through_to_default() {
        assert_eq!(resolve_log_level(Some("nonsense"), None), Level::INFO);
    }

    #[test]
    fn env_missing_stored_valid_uses_stored() {
        assert_eq!(resolve_log_level(None, Some("error")), Level::ERROR);
    }

    #[test]
    fn env_missing_stored_invalid_uses_default() {
        assert_eq!(resolve_log_level(None, Some("silent")), Level::INFO);
    }

    #[test]
    fn env_missing_stored_missing_uses_default() {
        assert_eq!(resolve_log_level(None, None), Level::INFO);
        assert_eq!(DEFAULT_LOG_LEVEL, Level::INFO);
    }

    // ── Value parsing ──

    #[test]
    fn all_four_documented_values_parse() {
        assert_eq!(resolve_log_level(Some("error"), None), Level::ERROR);
        assert_eq!(resolve_log_level(Some("warn"), None), Level::WARN);
        assert_eq!(resolve_log_level(Some("info"), None), Level::INFO);
        assert_eq!(resolve_log_level(Some("debug"), None), Level::DEBUG);
    }

    #[test]
    fn parsing_is_case_insensitive_and_ignores_surrounding_space() {
        assert_eq!(resolve_log_level(Some("DEBUG"), None), Level::DEBUG);
        assert_eq!(resolve_log_level(Some("Warn"), None), Level::WARN);
        assert_eq!(resolve_log_level(Some("  error\n"), None), Level::ERROR);
        assert_eq!(resolve_log_level(Some("ErRoR"), None), Level::ERROR);
    }

    #[test]
    fn empty_and_whitespace_only_are_invalid_not_silencing() {
        // An env var exported as "" is a common shell accident. It must
        // behave like "unset", never like "off".
        assert_eq!(resolve_log_level(Some(""), Some("debug")), Level::DEBUG);
        assert_eq!(resolve_log_level(Some("   "), Some("debug")), Level::DEBUG);
        assert_eq!(resolve_log_level(Some(""), None), Level::INFO);
    }

    #[test]
    fn levels_outside_the_documented_four_do_not_take_effect() {
        // `trace` is a real tracing level but not an offered preference
        // value; `off` is the one input a careless implementation would
        // honour into silence. Both must fall through.
        assert_eq!(resolve_log_level(Some("trace"), None), Level::INFO);
        assert_eq!(resolve_log_level(Some("off"), None), Level::INFO);
        assert_eq!(resolve_log_level(Some("none"), None), Level::INFO);
        assert_eq!(resolve_log_level(Some("0"), None), Level::INFO);
    }

    #[test]
    fn resolution_never_yields_a_level_quieter_than_error() {
        // Error-path messages must stay visible at every reachable
        // setting — the floor is ERROR, and no input can go below it.
        for candidate in [
            "error", "warn", "info", "debug", "off", "", "trace", "garbage",
        ] {
            let lvl = resolve_log_level(Some(candidate), None);
            assert!(
                lvl >= Level::ERROR,
                "{candidate:?} resolved to {lvl:?}, which would hide errors"
            );
        }
    }

    // ── Constants are the SSOT the other crates key off ──

    #[test]
    fn config_key_names_are_stable() {
        assert_eq!(LOG_LEVEL_ENV, "VCO_LOG_LEVEL");
        // Must NOT be the retired zero-consumer `logging_level` key.
        assert_eq!(LOG_LEVEL_APP_STATE_KEY, "logging.level");
        assert_ne!(LOG_LEVEL_APP_STATE_KEY, "logging_level");
    }

    // ── The launcher.db probe soft-fails ──

    #[test]
    fn db_probe_returns_none_when_no_launcher_db_exists() {
        crate::test_env::with_state_dir(|_root| {
            assert_eq!(stored_log_level_from_launcher_db(), None);
        });
    }

    #[test]
    fn db_probe_returns_none_on_a_file_that_is_not_a_database() {
        crate::test_env::with_state_dir(|_root| {
            let path = crate::db::db_path();
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).expect("state dir");
            }
            std::fs::write(&path, b"this is not sqlite").expect("write junk");
            // Garbage on disk must degrade to "no preference", not a panic.
            assert_eq!(stored_log_level_from_launcher_db(), None);
        });
    }

    #[test]
    fn db_probe_reads_the_stored_value_when_present() {
        crate::test_env::with_state_dir(|_root| {
            {
                let db = crate::db::Db::open().expect("open launcher.db");
                db.app_state_set(LOG_LEVEL_APP_STATE_KEY, "debug")
                    .expect("store level");
            }
            let stored = stored_log_level_from_launcher_db();
            assert_eq!(stored.as_deref(), Some("debug"));
            // And it composes with the pure resolver the way callers use it.
            assert_eq!(resolve_log_level(None, stored.as_deref()), Level::DEBUG);
            // Env still outranks a present stored value.
            assert_eq!(resolve_log_level(Some("error"), stored.as_deref()), Level::ERROR);
        });
    }

    // ── Subscriber install / reload ──
    //
    // These touch process-global subscriber state, so they assert the
    // properties that hold regardless of which test ran first: neither
    // entry point may panic, and both must tolerate being called out of
    // order. Whether THIS test's `init_tracing` wins the global race
    // depends on test ordering, so nothing here asserts that it did.

    #[test]
    fn init_and_reload_are_idempotent_and_never_panic() {
        init_tracing(Level::WARN);
        // A second install must be a no-op, not a panic.
        init_tracing(Level::DEBUG);
        // Reload across every level, in both directions.
        for lvl in [Level::ERROR, Level::DEBUG, Level::INFO, Level::WARN] {
            set_log_level(lvl);
        }
        // And a reload with no preceding successful install (the case
        // where another subscriber owns the process) must also be inert.
        set_log_level(Level::ERROR);
    }

    #[test]
    fn db_probe_returns_none_when_the_key_was_never_set() {
        crate::test_env::with_state_dir(|_root| {
            crate::db::Db::open().expect("open launcher.db");
            assert_eq!(stored_log_level_from_launcher_db(), None);
        });
    }
}
