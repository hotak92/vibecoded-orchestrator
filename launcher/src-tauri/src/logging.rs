// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Launcher-side wiring for the leveled-diagnostics subsystem (v0.2.91,
//! user decision #21).
//!
//! The RULES all live in `vct_launcher_core::logging` — one home for the
//! env-var name, the app_state key, the precedence order, the accepted
//! vocabulary, and the subscriber itself, shared with `vct-hub`. This
//! module is only the launcher's two call sites and the reason there are
//! two of them.
//!
//! ## Scope: diagnostics only
//!
//! `logging.level` governs DIAGNOSTIC OUTPUT and nothing else. Telemetry
//! (`rl_events`) and audit trails (`audit_log`, the `~/.vct` JSONL trails)
//! are DATA, not chatter: they are records the product is expected to
//! keep, and they are never level-gated. Raising the level to `error`
//! makes the launcher quieter; it must never make it forget anything.
//!
//! ## Why initialisation happens in two phases
//!
//! Resolution is `VCO_LOG_LEVEL` → app_state `logging.level` → INFO, and
//! the stored half is only readable once `launcher.db` is open. But
//! `Db::open()` applies migrations, ensures the change log, prunes it and
//! runs a backfill pass — and it CANNOT be hoisted to the top of `main()`,
//! because the very first thing `main()` must do is
//! [`crate::webkit_preflight`]: that probe calls `std::env::set_var`, and
//! what makes those calls sound is that no other thread exists yet. Moving
//! a DB open (and everything it may spawn) ahead of it would break that
//! invariant to answer nothing more urgent than "how chatty should I be?".
//!
//! So the launcher does:
//!
//!   1. [`init_early`] — first statement of `main()`, before the preflight.
//!      Env-or-INFO, no I/O, no threads: it only installs the subscriber.
//!   2. [`apply_stored_level`] — in `run()`, the moment the `Db` handle
//!      exists. Re-resolves with the stored value and reloads the filter.
//!
//! The window between them covers the WebKit preflight's own diagnostics
//! (and the two boot reapers), which therefore follow env-or-INFO rather
//! than a stored `warn`/`error` preference. That is a deliberate, bounded
//! trade — a handful of early lines against the preflight's soundness
//! contract — and `VCO_LOG_LEVEL` still controls them, which is what
//! matters when someone is actually debugging boot. **Do not "fix" it by
//! opening the Db before the preflight.**

use vct_launcher_core::logging as core_logging;

/// Phase 1: install the subscriber from `VCO_LOG_LEVEL` alone.
///
/// Called as the FIRST statement of `main()` — ahead of the WebKit
/// preflight — so no diagnostic in this binary is emitted into a void.
/// Deliberately does no I/O: the stored preference arrives in
/// [`apply_stored_level`].
pub fn init_early() {
    let level =
        core_logging::resolve_log_level(core_logging::env_log_level().as_deref(), None);
    core_logging::init_tracing(level);
}

/// The level this launcher process should run at, given the environment
/// and the preference stored in `db`.
///
/// Split out from [`apply_stored_level`] so the launcher's half of the
/// contract — *which key it reads, and that the env override is still
/// offered to the resolver* — is assertable without a subscriber, a
/// reactor or a global. The precedence RULES themselves belong to
/// `vct_launcher_core::logging::resolve_log_level` and are tested there.
///
/// Soft-fail: an unreadable `app_state` (poisoned lock, missing table on
/// an old schema) reads as "no preference stored", which the resolver
/// turns into the env value or the default.
fn resolve_level_from_db(db: &crate::db::Db) -> tracing::Level {
    let stored = db
        .app_state_get(core_logging::LOG_LEVEL_APP_STATE_KEY)
        .ok()
        .flatten();
    core_logging::resolve_log_level(
        core_logging::env_log_level().as_deref(),
        stored.as_deref(),
    )
}

/// Phase 2: fold the stored `logging.level` preference in, now that the
/// launcher DB is open.
///
/// `VCO_LOG_LEVEL` still wins — it is offered to the resolver again
/// rather than assumed absent, so the env override survives this call
/// instead of being undone by it. Soft-fail throughout: an unreadable
/// row, an absent row or an unparseable value all resolve to the next
/// tier, and `set_log_level` is itself a no-op if the subscriber was
/// never installed. Nothing here can block or fail boot.
pub fn apply_stored_level(db: &crate::db::Db) {
    core_logging::set_log_level(resolve_level_from_db(db));
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use tracing::Level;

    /// Restores `VCO_LOG_LEVEL` to whatever the runner had, so a test that
    /// sets it cannot leak into a sibling.
    struct EnvGuard(Option<std::ffi::OsString>);
    impl EnvGuard {
        fn set(value: Option<&str>) -> Self {
            let guard = EnvGuard(std::env::var_os(core_logging::LOG_LEVEL_ENV));
            match value {
                Some(v) => std::env::set_var(core_logging::LOG_LEVEL_ENV, v),
                None => std::env::remove_var(core_logging::LOG_LEVEL_ENV),
            }
            guard
        }
    }
    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match self.0.take() {
                Some(v) => std::env::set_var(core_logging::LOG_LEVEL_ENV, v),
                None => std::env::remove_var(core_logging::LOG_LEVEL_ENV),
            }
        }
    }

    fn db_with(stored: Option<&str>) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        if let Some(v) = stored {
            db.app_state_set(core_logging::LOG_LEVEL_APP_STATE_KEY, v)
                .expect("seed logging.level");
        }
        db
    }

    /// Nothing configured anywhere: the launcher must run at the default.
    #[test]
    #[serial_test::serial]
    fn no_env_no_row_is_the_default() {
        let _env = EnvGuard::set(None);
        assert_eq!(
            resolve_level_from_db(&db_with(None)),
            core_logging::DEFAULT_LOG_LEVEL
        );
    }

    /// The stored preference is honoured — the whole point of decision #21
    /// (the pre-v0.2.91 key had no consumer at all).
    #[test]
    #[serial_test::serial]
    fn stored_row_is_honoured_when_env_is_absent() {
        let _env = EnvGuard::set(None);
        assert_eq!(resolve_level_from_db(&db_with(Some("error"))), Level::ERROR);
        assert_eq!(resolve_level_from_db(&db_with(Some("debug"))), Level::DEBUG);
    }

    /// The launcher reads the key the launcher WRITES. A rename on either
    /// side silently reverts the pref to "no consumer", so pin the wiring
    /// rather than trusting the constant to stay in sync by inspection.
    #[test]
    #[serial_test::serial]
    fn reads_the_canonical_app_state_key() {
        let _env = EnvGuard::set(None);
        assert_eq!(core_logging::LOG_LEVEL_APP_STATE_KEY, "logging.level");
        let db = Db::open_in_memory().expect("in-memory db");
        db.app_state_set("logging.level", "warn").expect("seed");
        assert_eq!(resolve_level_from_db(&db), Level::WARN);
    }

    /// `VCO_LOG_LEVEL` OVERRIDES the stored preference. This is the leg the
    /// launcher owns: phase 2 must keep offering the env value to the
    /// resolver, or reopening the Db would quietly undo an operator's
    /// debug override mid-boot.
    #[test]
    #[serial_test::serial]
    fn env_wins_over_the_stored_row() {
        let _env = EnvGuard::set(Some("debug"));
        assert_eq!(resolve_level_from_db(&db_with(Some("error"))), Level::DEBUG);
    }

    /// Garbage never wins and never panics: an unparseable env value falls
    /// through to the stored row, and an unparseable row to the default.
    #[test]
    #[serial_test::serial]
    fn unparseable_values_fall_through_instead_of_failing() {
        let _env = EnvGuard::set(Some("very-loud"));
        assert_eq!(resolve_level_from_db(&db_with(Some("warn"))), Level::WARN);
        drop(_env);
        let _env = EnvGuard::set(None);
        assert_eq!(
            resolve_level_from_db(&db_with(Some("shout"))),
            core_logging::DEFAULT_LOG_LEVEL
        );
    }

    /// BOOT-ORDER INVARIANT. `main()` must install the subscriber BEFORE
    /// the WebKit preflight runs, or the preflight's diagnostics (the ones
    /// that explain an NVIDIA-driver GPU fallback) are emitted into a void.
    ///
    /// Asserted on the source because the ordering lives in `main()`, which
    /// is a separate compilation unit with no seam to call into. The
    /// symmetric hazard — someone "fixing" the two-phase split by hoisting
    /// a `Db::open()` above the preflight, breaking its single-thread
    /// `set_var` contract — is guarded by the second assertion.
    /// Matching is done over CODE LINES ONLY, and that is load-bearing.
    ///
    /// The first version of this test used `str::find` over the whole file,
    /// which matches the FIRST occurrence of the text anywhere — comments
    /// included. Red-proofing it found a silent false negative: move the
    /// real `init_early()` call BELOW the preflight but leave a comment
    /// naming it above, and the test still passed on a genuinely broken
    /// boot order. That is the same comment-shadowing class that made the
    /// delivery-chain test's plain `index()` match a prose copy of the
    /// `[vct] setup complete` marker instead of the real call.
    ///
    /// A test whose locator can be satisfied by prose cannot fail in the
    /// direction it exists to guard, so it reads as coverage while
    /// providing none.
    fn main_rs_code_lines(src: &str) -> Vec<(usize, &str)> {
        // Only `//` lines are stripped. Assert the assumption rather than
        // trusting it: a block comment would silently start matching
        // commented-out text again, reopening the exact hole above.
        assert!(
            !src.contains("/*"),
            "main.rs gained a block comment; this filter only strips `//` \
             lines and would resume matching commented-out text"
        );
        src.lines()
            .enumerate()
            .filter(|(_, l)| !l.trim_start().starts_with("//"))
            .collect()
    }

    #[test]
    fn main_installs_the_subscriber_before_the_webkit_preflight() {
        let main_rs =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/main.rs");
        let src = std::fs::read_to_string(&main_rs)
            .unwrap_or_else(|e| panic!("read {}: {e}", main_rs.display()));
        let code = main_rs_code_lines(&src);
        let line_of = |needle: &str| {
            code.iter()
                .find(|(_, l)| l.contains(needle))
                .map(|(i, _)| *i)
        };

        let init = line_of("logging::init_early()")
            .expect("main() must CALL logging::init_early(), not merely mention it");
        let preflight = line_of("probe_and_apply_workaround_if_needed()")
            .expect("main() must still run the WebKit preflight");
        assert!(
            init < preflight,
            "logging::init_early() must precede the WebKit preflight in main() \
             (init at line {}, preflight at line {})",
            init + 1,
            preflight + 1,
        );
        assert!(
            line_of("Db::open").is_none(),
            "main() must NOT open the launcher DB: the preflight's set_var \
             soundness depends on being the first code to run, before any \
             thread exists. The stored level is applied in run() instead."
        );
    }

    /// Red-proof of the locator itself, pinned so it cannot regress.
    ///
    /// Feeds the comment-shadowed shape directly to the matcher: a comment
    /// naming `init_early()` above the preflight, with the real call below
    /// it. A whole-file `str::find` reports init-before-preflight here and
    /// passes; the code-line matcher must report the true order.
    #[test]
    fn the_locator_is_not_fooled_by_a_comment_naming_the_call() {
        let shadowed = "\
fn main() {
    // NOTE: vct_launcher_temp_lib::logging::init_early() is handled below.
    let _ = webkit_preflight::probe_and_apply_workaround_if_needed();
    vct_launcher_temp_lib::logging::init_early();
}
";
        // The naive locator is fooled: it finds the COMMENT first.
        assert!(
            shadowed.find("logging::init_early()").unwrap()
                < shadowed.find("probe_and_apply_workaround_if_needed()").unwrap(),
            "precondition: a whole-file find must be fooled by this shape, \
             otherwise this test is not exercising the hazard"
        );
        // The real locator is not.
        let code = main_rs_code_lines(shadowed);
        let line_of = |needle: &str| {
            code.iter()
                .find(|(_, l)| l.contains(needle))
                .map(|(i, _)| *i)
        };
        assert!(
            line_of("logging::init_early()").unwrap()
                > line_of("probe_and_apply_workaround_if_needed()").unwrap(),
            "code-line matcher must see the REAL call order, not the comment"
        );
    }
}
