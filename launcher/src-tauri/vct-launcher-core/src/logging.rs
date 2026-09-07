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

use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use tracing::Level;
use tracing_subscriber::filter::LevelFilter;
use tracing_subscriber::fmt::MakeWriter;
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
///
/// The type parameter is `Registry` (the subscriber the reload layer is
/// installed ON), not the fully-layered stack — adding the file layer in
/// v0.2.92 therefore did not change this type, and `set_log_level` still
/// reaches the same filter it always did.
static RELOAD_HANDLE: OnceLock<reload::Handle<LevelFilter, tracing_subscriber::Registry>> =
    OnceLock::new();

// ---------------------------------------------------------------------------
// File sink (v0.2.92, WP-13)
// ---------------------------------------------------------------------------
//
// ## Why a file sink was not optional
//
// Until v0.2.92 `init_tracing` installed a stderr layer and NOTHING ELSE, on
// every OS. Meanwhile `launcher/src-tauri/src/main.rs` carries
// `#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]`, which
// means a RELEASE launcher on Windows has no console and no stderr sink at
// all. So every honest diagnostic the codebase emits — including the whole
// v0.2.83 "couldn't check for updates, here is why" family — was written,
// correctly, into nothing.
//
// That is why a five-week silent-no-update incident on a real user's machine
// produced ZERO diagnostic trail: the launcher had been saying the right
// things the entire time, to a stream that did not exist. Fixing the checks
// without fixing the sink would have made the next incident equally
// unreadable.
//
// ## Why a hand-rolled writer instead of `tracing-appender`
//
// `tracing-appender` is the obvious dependency and we deliberately did not
// take it:
//
//   * it is absent from both the workspace `Cargo.lock` and the local cargo
//     registry cache, so adding it requires a network fetch at build time —
//     a poor trade for ~60 lines;
//   * its `rolling::daily` has NO retention policy, so the 14-file sweep
//     below would have had to be written by hand anyway;
//   * its recommended non-blocking writer hands back a `WorkerGuard` that the
//     caller must hold for the process lifetime, which would push a new
//     return value through `init_tracing`'s two call sites and create a
//     "logging silently stopped because the guard was dropped" failure mode.
//
// The writer below is blocking, line-buffered by `tracing`'s formatter, and
// opens in append mode. Blocking writes to a local file are microseconds; the
// launcher is not a high-throughput logger.
//
// ## Cross-OS
//
// Everything here is `std::fs` + `std::path` — no POSIX-only primitives, no
// `flock`, no path-separator assumptions, no permission bits. The date stamp
// comes from `chrono` (already a dependency). Two processes appending to the
// same file is fine on all three platforms for the small, single-`write_all`
// records `tracing` produces; the launcher and hub write to DIFFERENT files
// anyway (`launcher.log` / `hub.log`).

/// Directory holding the rotated diagnostic logs: `<vct_root>/logs/`.
///
/// Resolved through [`crate::paths::vct_root_dir`] — the ONE home for that
/// root — so `VCT_STATE_DIR` redirection works for tests and for users who
/// relocate their state dir.
pub fn log_dir() -> PathBuf {
    crate::paths::vct_root_dir().join("logs")
}

/// How many daily log files to keep.
///
/// **These are VCO-owned diagnostics, not user data**, so VCO deletes its own
/// rotated files. Fourteen days is chosen against the incident that motivated
/// the sink: the field report arrived roughly five weeks after the outage
/// began, and no retention would have covered that — but two weeks does cover
/// "it broke, I noticed within a fortnight, here are the logs", which is the
/// realistic reporting window, at a bounded disk cost (a chatty session
/// writes single-digit MB/day).
///
/// **The sweep is stem-scoped, and that is load-bearing, not defensive
/// styling.** `<vct_root>/logs/` is a SHARED directory that predates this
/// sink: the Python deferral-retry driver already writes
/// `deferral-retry-<timestamp>.log` there (a live install had 1,600+ of
/// them), and other VCO tooling has parked one-off `*.log` files there too.
/// A sweep that deleted "the oldest N files in the directory" would eat
/// them. `prune_old_logs` only ever deletes `<stem>.<date>.log` files it
/// wrote itself; `prune_leaves_files_it_does_not_own` pins that.
pub const LOG_RETENTION_FILES: usize = 14;

/// A day-rotating, append-mode file writer.
///
/// Rotation is checked per write batch by comparing the current UTC date
/// stamp against the one the open handle was created for — no background
/// thread, no timer. UTC (not local time) so a machine that changes timezone
/// or crosses DST cannot produce a file that sorts before its predecessor.
struct DailyFile {
    dir: PathBuf,
    stem: String,
    /// `(date-stamp, handle)` for the currently-open file.
    current: Mutex<Option<(String, File)>>,
}

impl DailyFile {
    fn new(dir: PathBuf, stem: &str) -> Self {
        Self {
            dir,
            stem: stem.to_string(),
            current: Mutex::new(None),
        }
    }

    fn today() -> String {
        chrono::Utc::now().format("%Y-%m-%d").to_string()
    }

    fn file_name(stem: &str, stamp: &str) -> String {
        format!("{stem}.{stamp}.log")
    }

    /// Open (or reuse) today's file and run `f` against it.
    ///
    /// Soft-fail by construction: if the directory cannot be created or the
    /// file cannot be opened, the write is dropped and the process carries
    /// on. Diagnostics failing must never be a reason the launcher fails.
    fn with_file<F: FnOnce(&mut File)>(&self, f: F) {
        let stamp = Self::today();
        let mut guard = match self.current.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        let needs_open = match guard.as_ref() {
            Some((open_stamp, _)) => open_stamp != &stamp,
            None => true,
        };
        if needs_open {
            if std::fs::create_dir_all(&self.dir).is_err() {
                return;
            }
            let path = self.dir.join(Self::file_name(&self.stem, &stamp));
            match OpenOptions::new().create(true).append(true).open(&path) {
                Ok(file) => *guard = Some((stamp, file)),
                Err(_) => return,
            }
            // A new day's file just appeared — prune older ones now. Doing it
            // here (rather than only at startup) means a launcher left running
            // for a month still rotates.
            prune_old_logs(&self.dir, &self.stem, LOG_RETENTION_FILES);
        }
        if let Some((_, file)) = guard.as_mut() {
            f(file);
        }
    }
}

/// `MakeWriter` adaptor. `tracing`'s formatter asks for a writer per event;
/// we hand back a thin handle that funnels into the shared [`DailyFile`].
struct DailyFileMakeWriter(std::sync::Arc<DailyFile>);

struct DailyFileHandle(std::sync::Arc<DailyFile>);

impl std::io::Write for DailyFileHandle {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.with_file(|f| {
            let _ = f.write_all(buf);
        });
        // Always report the full length: a diagnostic sink that reports a
        // short write would make `write_all` spin.
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.0.with_file(|f| {
            let _ = f.flush();
        });
        Ok(())
    }
}

impl<'a> MakeWriter<'a> for DailyFileMakeWriter {
    type Writer = DailyFileHandle;

    fn make_writer(&'a self) -> Self::Writer {
        DailyFileHandle(self.0.clone())
    }
}

/// Delete all but the newest `keep` files matching `<stem>.<date>.log` in
/// `dir`.
///
/// Only files this module writes are considered: the name must start with
/// `<stem>.` and end with `.log`. Anything else in the directory is left
/// alone, so a user who parks a note there does not lose it.
///
/// Sorting is by NAME, which for a `%Y-%m-%d` stamp is chronological — no
/// mtime reads, so a `cp -p` or a restored backup cannot reorder the sweep.
pub fn prune_old_logs(dir: &Path, stem: &str, keep: usize) {
    let prefix = format!("{stem}.");
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().map(|t| t.is_file()).unwrap_or(false))
        .filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| n.starts_with(&prefix) && n.ends_with(".log"))
        .collect();
    if names.len() <= keep {
        return;
    }
    names.sort();
    let doomed = names.len() - keep;
    for name in names.into_iter().take(doomed) {
        let _ = std::fs::remove_file(dir.join(name));
    }
}

/// Install the process-wide `tracing` subscriber for a named process:
/// compact format, to a daily FILE **and** to stderr, capped at `level`
/// through a *reloadable* filter.
///
/// `name` is the log-file stem — `"launcher"` → `<vct_root>/logs/
/// launcher.YYYY-MM-DD.log`, `"hub"` → `hub.YYYY-MM-DD.log`. Both binaries
/// call this one function; giving them separate files keeps two processes
/// from interleaving into one.
///
/// stderr is KEPT (not replaced) because stdout is a machine contract on
/// several surfaces — `vct-hub --status` prints `running pid=N` there, the
/// CLI helpers emit parse-target lines — and a developer running the binary
/// in a terminal should still see output without tailing a file.
///
/// The FILE layer is the v0.2.92 addition, and the reason is in the module
/// section above: a release Windows launcher has no stderr at all, so the
/// stderr-only subscriber meant every diagnostic on that platform went
/// nowhere. See [`init_tracing`] for the compatibility wrapper.
///
/// ## Eager creation is deliberate
///
/// The file is opened and a banner line written IMMEDIATELY, not lazily on
/// the first `warn!`. Two reasons: an empty-but-present file cannot occur
/// (so "does the file exist?" is a meaningful question with a meaningful
/// answer), and the file's APPEARANCE is itself an observable when a user is
/// told "update, then send me the log".
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
pub fn init_tracing_named(level: Level, name: &str) {
    let (filter, handle) = reload::Layer::new(LevelFilter::from(level));

    let dir = log_dir();
    let sink = std::sync::Arc::new(DailyFile::new(dir.clone(), name));
    // Open eagerly so the file exists before the first event, and prune on
    // the way in so a launcher that is restarted daily still rotates.
    sink.with_file(|_| {});
    prune_old_logs(&dir, name, LOG_RETENTION_FILES);
    let log_path = dir.join(DailyFile::file_name(name, &DailyFile::today()));

    let installed = tracing_subscriber::registry()
        .with(filter)
        .with(
            tracing_subscriber::fmt::layer()
                .compact()
                // No ANSI in the file: escape sequences make a log a user
                // pastes into an issue unreadable. (The stderr layer has none
                // either — the `ansi` feature is not enabled on the
                // dependency at all.)
                .with_writer(DailyFileMakeWriter(sink)),
        )
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
        // The banner is at ERROR level ON PURPOSE. It is not an error; it is
        // the one line that must appear even when the user has set
        // `VCO_LOG_LEVEL=error` to quieten a noisy session — otherwise the
        // "does this file exist and does it have content?" invariant holds
        // only at INFO and above, and the quietest setting produces the
        // emptiest evidence exactly when someone is debugging.
        tracing::error!(
            "[vct] {} {} started — diagnostics: {} (keeping the newest {} daily files)",
            name,
            env!("CARGO_PKG_VERSION"),
            log_path.display(),
            LOG_RETENTION_FILES,
        );
    }
}

/// Backwards-compatible entry point: `init_tracing_named(level, "launcher")`.
///
/// Kept because several call sites (including tests that only care about the
/// level filter) name it, and because the launcher IS the majority caller.
pub fn init_tracing(level: Level) {
    init_tracing_named(level, "launcher");
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
        // v0.2.92 WP-13: `with_state_dir` is now MANDATORY here. Since
        // `init_tracing` gained a file sink it has a filesystem side effect,
        // and without the redirect this test creates
        // `~/.vct/logs/launcher.<today>.log` on the developer's real machine
        // — a test writing into live user state, which is precisely the
        // class of defect this cycle spent a review round on. (It also
        // silently destroys a planned dogfood observable: the FIRST
        // appearance of that file is supposed to be evidence that the new
        // build ran.)
        crate::test_env::with_state_dir(|_root| {
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
        });
    }

    /// Guard for the mistake above, so the next person cannot repeat it
    /// silently: with `VCT_STATE_DIR` redirected, NOTHING the sink does may
    /// resolve outside that dir.
    #[test]
    fn the_file_sink_never_escapes_a_redirected_state_dir() {
        crate::test_env::with_state_dir(|root| {
            let dir = log_dir();
            assert!(
                dir.starts_with(root),
                "log_dir() escaped the redirected state dir: {} not under {}",
                dir.display(),
                root.display()
            );
            with_file_subscriber(&dir, "launcher", || {
                tracing::error!("scoped");
            });
            let produced = dir.join(DailyFile::file_name("launcher", &DailyFile::today()));
            assert!(produced.starts_with(root), "{}", produced.display());
            assert!(produced.exists());
        });
    }

    // ── The file sink (v0.2.92 WP-13) ──
    //
    // These are the OS-INDEPENDENT proof for a defect whose worst symptom is
    // Windows-only. The `windows_subsystem = "windows"` stderr void cannot be
    // reproduced on Linux and needs no test; what needs proving is that a
    // SINK other than stderr exists and receives output. That is assertable
    // anywhere.

    /// Build the same layer stack `init_tracing_named` installs, but scoped
    /// to this thread via `with_default` instead of `try_init`.
    ///
    /// Necessary because `tracing`'s global subscriber can be installed only
    /// ONCE per process: a test that called `init_tracing_named` would either
    /// lose the race to whichever test ran first (asserting nothing) or win
    /// it and change every other test's logging. `with_default` gives this
    /// test its own subscriber for the duration of the closure.
    fn with_file_subscriber<F: FnOnce()>(dir: &Path, name: &str, f: F) {
        let sink = std::sync::Arc::new(DailyFile::new(dir.to_path_buf(), name));
        sink.with_file(|_| {});
        let subscriber = tracing_subscriber::registry()
            .with(LevelFilter::from(Level::INFO))
            .with(
                tracing_subscriber::fmt::layer()
                    .compact()
                    .with_writer(DailyFileMakeWriter(sink)),
            );
        tracing::subscriber::with_default(subscriber, f);
    }

    #[test]
    fn init_creates_log_file_and_writes_banner() {
        crate::test_env::with_state_dir(|_root| {
            let dir = log_dir();
            let name = "launcher";
            with_file_subscriber(&dir, name, || {
                tracing::error!(
                    "[vct] {} {} started — diagnostics test banner",
                    name,
                    env!("CARGO_PKG_VERSION")
                );
                tracing::info!("a second line so ordering is observable");
            });

            let path = dir.join(DailyFile::file_name(name, &DailyFile::today()));
            assert!(
                path.exists(),
                "the log file must EXIST after init: {}",
                path.display()
            );
            let body = std::fs::read_to_string(&path).expect("read log");
            let lines: Vec<&str> = body.lines().filter(|l| !l.trim().is_empty()).collect();
            assert!(
                !lines.is_empty(),
                "the log file must have at least one line — an empty-but-present file is \
                 the failure mode eager creation exists to prevent"
            );
            assert!(
                body.contains(env!("CARGO_PKG_VERSION")),
                "the banner must carry the running version so a pasted log identifies the \
                 build; got:\n{body}"
            );
            assert!(
                body.contains("a second line so ordering is observable"),
                "subsequent events must reach the same file, not just the banner"
            );
        });
    }

    #[test]
    fn file_sink_captures_a_warning_that_stderr_would_have_swallowed() {
        // The point of the whole exercise: the "couldn't check for updates"
        // family of diagnostics must land somewhere a user can send us.
        crate::test_env::with_state_dir(|_root| {
            let dir = log_dir();
            with_file_subscriber(&dir, "launcher", || {
                tracing::warn!(
                    "[vct] check_for_launcher_update: behind-count failed (git rev-list: \
                     fatal: ambiguous argument) — remote currency is UNKNOWN"
                );
            });
            let body = std::fs::read_to_string(
                dir.join(DailyFile::file_name("launcher", &DailyFile::today())),
            )
            .expect("read log");
            assert!(body.contains("remote currency is UNKNOWN"), "got:\n{body}");
        });
    }

    #[test]
    fn log_dir_follows_the_state_dir_override() {
        crate::test_env::with_state_dir(|root| {
            assert_eq!(
                log_dir(),
                root.join("logs"),
                "the log dir must resolve through paths::vct_root_dir, so VCT_STATE_DIR \
                 redirection (tests, relocated state) works"
            );
        });
    }

    #[test]
    fn prune_keeps_the_newest_and_deletes_the_rest() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dir = tmp.path();
        // 20 days, written out of order so the sweep cannot be passing by
        // accident of creation order.
        let days: Vec<String> = (1..=20).map(|d| format!("2026-01-{d:02}")).collect();
        for d in days.iter().rev() {
            std::fs::write(dir.join(DailyFile::file_name("launcher", d)), b"x\n").unwrap();
        }
        prune_old_logs(dir, "launcher", LOG_RETENTION_FILES);

        let mut left: Vec<String> = std::fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        left.sort();
        assert_eq!(
            left.len(),
            LOG_RETENTION_FILES,
            "expected exactly {LOG_RETENTION_FILES} survivors, got {left:?}"
        );
        assert_eq!(
            left.first().unwrap(),
            &DailyFile::file_name("launcher", "2026-01-07"),
            "the survivors must be the NEWEST 14 (07..20), not the first 14 read"
        );
        assert_eq!(
            left.last().unwrap(),
            &DailyFile::file_name("launcher", "2026-01-20")
        );
    }

    #[test]
    fn prune_leaves_files_it_does_not_own() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dir = tmp.path();
        for d in 1..=20 {
            std::fs::write(
                dir.join(DailyFile::file_name("launcher", &format!("2026-01-{d:02}"))),
                b"x\n",
            )
            .unwrap();
        }
        // Not ours: a different stem, a non-.log file, and a subdirectory.
        std::fs::write(dir.join(DailyFile::file_name("hub", "2026-01-01")), b"x\n").unwrap();
        std::fs::write(dir.join("notes-from-the-user.txt"), b"important\n").unwrap();
        std::fs::create_dir(dir.join("a-directory.log")).unwrap();

        prune_old_logs(dir, "launcher", LOG_RETENTION_FILES);

        assert!(
            dir.join(DailyFile::file_name("hub", "2026-01-01")).exists(),
            "a sweep for `launcher` must not touch `hub` files"
        );
        assert!(
            dir.join("notes-from-the-user.txt").exists(),
            "the sweep must only ever delete files it wrote"
        );
        assert!(
            dir.join("a-directory.log").is_dir(),
            "the sweep must not attempt directories"
        );
    }

    #[test]
    fn prune_is_a_no_op_below_the_retention_limit() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dir = tmp.path();
        for d in 1..=3 {
            std::fs::write(
                dir.join(DailyFile::file_name("launcher", &format!("2026-01-{d:02}"))),
                b"x\n",
            )
            .unwrap();
        }
        prune_old_logs(dir, "launcher", LOG_RETENTION_FILES);
        assert_eq!(
            std::fs::read_dir(dir).unwrap().count(),
            3,
            "leave-alone half: under the limit, nothing is deleted"
        );
    }

    #[test]
    fn prune_on_a_missing_directory_is_silent() {
        let tmp = tempfile::tempdir().expect("tempdir");
        // Must not panic: the sweep runs on a path that may not exist yet.
        prune_old_logs(&tmp.path().join("nope"), "launcher", LOG_RETENTION_FILES);
    }

    #[test]
    fn daily_file_names_sort_chronologically() {
        // The sweep sorts by NAME, so the name format must be sortable.
        let mut names = vec![
            DailyFile::file_name("launcher", "2026-01-09"),
            DailyFile::file_name("launcher", "2025-12-31"),
            DailyFile::file_name("launcher", "2026-01-10"),
        ];
        names.sort();
        assert_eq!(
            names,
            vec![
                DailyFile::file_name("launcher", "2025-12-31"),
                DailyFile::file_name("launcher", "2026-01-09"),
                DailyFile::file_name("launcher", "2026-01-10"),
            ]
        );
    }

    #[test]
    fn writes_append_rather_than_truncate() {
        // A launcher restart must not erase the morning's diagnostics.
        let tmp = tempfile::tempdir().expect("tempdir");
        let dir = tmp.path();
        let path = dir.join(DailyFile::file_name("launcher", &DailyFile::today()));
        std::fs::write(&path, b"earlier session\n").unwrap();

        with_file_subscriber(dir, "launcher", || {
            tracing::error!("later session");
        });

        let body = std::fs::read_to_string(&path).unwrap();
        assert!(body.contains("earlier session"), "got:\n{body}");
        assert!(body.contains("later session"), "got:\n{body}");
    }

    #[test]
    fn db_probe_returns_none_when_the_key_was_never_set() {
        crate::test_env::with_state_dir(|_root| {
            crate::db::Db::open().expect("open launcher.db");
            assert_eq!(stored_log_level_from_launcher_db(), None);
        });
    }
}
