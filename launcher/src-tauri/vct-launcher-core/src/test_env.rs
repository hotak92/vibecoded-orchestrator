//! Workspace-wide env-var test helper.
//!
//! v0.2.21 Step 23. Multiple test modules across `vct-launcher-core`,
//! `vct-hub`, and the launcher crate all mutate `VCT_STATE_DIR` (and
//! occasionally `VCT_HUB_PORT`, `HOME`, etc.) at process scope to
//! redirect state-dir reads to a per-test scratch dir. Pre-Step-23
//! each module owned its own `static SERIALIZE: Mutex<()>` to
//! serialize WITHIN that module — but two tests from DIFFERENT
//! modules running concurrently would both `set_var("VCT_STATE_DIR",
//! ...)` and observe each other's mutations.
//!
//! Symptom: occasional flake in the full-workspace `cargo test` run
//! (most reliably reproducible with multiple `auth::tests` /
//! `lockfile::tests` / `boot::tests` instances active at once).
//! `cargo test --test-threads=1` always passes.
//!
//! Fix: a single workspace-wide `Mutex<()>` that every env-mutating
//! test acquires. Helper functions `with_state_dir(f)` /
//! `with_env_vars(vars, f)` wrap the boilerplate so test modules
//! don't have to reimplement it.
//!
//! Gated on `cfg(any(test, debug_assertions))` so the symbol is
//! visible across crates' tests + dev builds, excluded from
//! `--release` builds (same pattern as `Db::open_in_memory`).
//!
//! # v0.2.92 — this module is the ONLY sanctioned home for mutating
//! # `VCT_STATE_DIR`, and `tests/state_dir_env_lint.rs` enforces it
//!
//! Step 23 shipped the helpers above but left adoption OPTIONAL, and
//! 21 other files went on hand-rolling the same block. 49 of those
//! hand-rolled sites ended their test with a bare
//! `std::env::remove_var("VCT_STATE_DIR")` instead of restoring the
//! PRIOR value. Environment variables are process-global, so the first
//! such test in a binary destroyed any OUTER redirect and every test
//! after it in that binary resolved `paths::vct_root_dir()` to the
//! developer's REAL `~/.vct`.
//!
//! Measured on the unfixed tree (`cargo test --workspace` run with
//! `VCT_STATE_DIR` pointed at a scratch dir and `HOME` pointed at a
//! decoy): the scratch dir received exactly one file (`keyring.pace`)
//! while the decoy home received a COMPLETE live state directory —
//! `launcher.db` (626 KB, migrated), `hub.pid`, `hub.port`,
//! `hub.token`, `hub.db` + WAL, and `logs/hub.<date>.log`. Unshielded,
//! every one of those writes lands on the user's real install.
//!
//! So: do not reintroduce a local copy of this block, and do not
//! "just unset it at the end" — unsetting is what caused the incident.
//! Take a guard:
//!
//! ```ignore
//! let sd = vct_launcher_core::test_env::state_dir_guard();
//! std::fs::write(sd.path().join("hub.pid"), "1234").unwrap();
//! // VCT_STATE_DIR is restored to its PRIOR value when `sd` drops,
//! // including on panic, and GLOBAL_ENV_MUTEX is held until then.
//! ```

#[cfg(any(test, debug_assertions))]
use std::ffi::OsString;
#[cfg(any(test, debug_assertions))]
use std::path::Path;
#[cfg(any(test, debug_assertions))]
use std::sync::{Mutex, MutexGuard};

/// Workspace-wide serialization mutex for env-var mutations in tests.
///
/// Acquire BEFORE any `std::env::set_var` / `remove_var` block; release
/// (via guard drop) AFTER restoring the prior state. This serializes
/// EVERY env-mutating test across the whole workspace — vct-launcher-
/// core, vct-hub, and the launcher crate all share this one lock.
///
/// `Mutex<()>` because we don't carry any data; we only need the
/// happens-before edge. Poisoning is recovered from via `unwrap_or_else(
/// PoisonError::into_inner)`.
#[cfg(any(test, debug_assertions))]
pub static GLOBAL_ENV_MUTEX: Mutex<()> = Mutex::new(());

/// Run `f` with `VCT_STATE_DIR` set to a fresh tempdir. After `f`
/// returns (or panics), restore the prior env-var state and drop the
/// tempdir. Acquires `GLOBAL_ENV_MUTEX` for the duration.
///
/// Usage:
/// ```ignore
/// use vct_launcher_core::test_env::with_state_dir;
///
/// #[test]
/// fn my_test() {
///     with_state_dir(|root| {
///         // reads of vct_root_dir() see `root` here.
///     });
/// }
/// ```
#[cfg(any(test, debug_assertions))]
pub fn with_state_dir<F: FnOnce(&Path)>(f: F) {
    let guard = state_dir_guard();
    f(guard.path());
}

/// Run `f` with arbitrary env-var overrides. `vars` is `&[(name,
/// value)]` where `value=Some("...")` sets the var and `None`
/// unsets it. Prior values are restored after `f` returns.
///
/// Useful when a test needs to set BOTH `VCT_STATE_DIR` and
/// `VCT_HUB_PORT` (or `HOME`, `PATH`, etc.) together. The single-
/// call wraps the lock acquire + restore boilerplate.
///
/// **Do not nest** this inside [`with_state_dir`] / [`state_dir_guard`]
/// (or vice versa): both acquire [`GLOBAL_ENV_MUTEX`], which is a plain
/// `std::sync::Mutex` and therefore NOT reentrant — nesting deadlocks.
/// When a test needs a scratch state dir AND other vars, use
/// [`state_dir_guard_with`], which takes the lock exactly once.
#[cfg(any(test, debug_assertions))]
pub fn with_env_vars<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
    let _guard = env_guard(vars);
    f();
}

// ─────────────────────────────────────────────────────────────────────
// RAII guards (v0.2.92)
//
// The `with_*` wrappers above cannot serve an `async` test: their
// closure is synchronous, and a `#[tokio::test]` body cannot be run
// inside one without a nested runtime. That gap is why ~30 async tests
// hand-rolled the set/unset block in the first place — and why their
// cleanup was a bare `remove_var` rather than a restore.
//
// A guard closes it: RAII works identically in sync and async bodies,
// restores on panic without `catch_unwind`, and cannot be "forgotten
// at the end" because there is no end to forget.
// ─────────────────────────────────────────────────────────────────────

/// Restores `VCT_STATE_DIR` to its prior value (set or unset) and
/// releases [`GLOBAL_ENV_MUTEX`] when dropped. Owns the scratch dir, so
/// the directory outlives every read the test makes of it.
///
/// Obtain via [`state_dir_guard`] / [`state_dir_guard_with`].
#[cfg(any(test, debug_assertions))]
pub struct StateDirGuard {
    // Field order is the drop order: `_restore` first (puts the env
    // back), then `_tmp` (removes the scratch dir), then `_lock`
    // (releases the mutex, letting the next env-mutating test in).
    // Restoring BEFORE releasing the lock is the invariant that makes
    // this safe under `--test-threads` > 1.
    _restore: EnvRestore,
    _tmp: tempfile::TempDir,
    _lock: MutexGuard<'static, ()>,
    path: std::path::PathBuf,
}

#[cfg(any(test, debug_assertions))]
impl StateDirGuard {
    /// The scratch directory `VCT_STATE_DIR` currently points at.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

/// Point `VCT_STATE_DIR` at a fresh scratch dir for the lifetime of the
/// returned guard.
///
/// Prefer this over [`with_state_dir`] in `async` tests, and in any test
/// whose body needs the scratch path in more than one place.
#[cfg(any(test, debug_assertions))]
pub fn state_dir_guard() -> StateDirGuard {
    state_dir_guard_with(&[])
}

/// [`state_dir_guard`] plus arbitrary extra env vars, set and restored
/// under the SAME lock acquisition.
///
/// `extra` uses the same `(name, Some(value) | None)` shape as
/// [`with_env_vars`]. `VCT_STATE_DIR` is set to the scratch dir first,
/// so passing it in `extra` overrides the scratch dir deliberately (the
/// value is still restored on drop).
#[cfg(any(test, debug_assertions))]
pub fn state_dir_guard_with(extra: &[(&str, Option<&str>)]) -> StateDirGuard {
    let lock = GLOBAL_ENV_MUTEX
        .lock()
        .unwrap_or_else(|p| p.into_inner());
    let tmp = tempfile::tempdir().expect("tempdir for state_dir_guard");
    let path = tmp.path().to_path_buf();

    let mut names: Vec<&str> = vec!["VCT_STATE_DIR"];
    names.extend(extra.iter().map(|(k, _)| *k));
    let restore = EnvRestore::capture(&names);

    // Safety: we hold GLOBAL_ENV_MUTEX, so no other env-mutating test
    // can observe or race these writes.
    unsafe {
        std::env::set_var("VCT_STATE_DIR", &path);
        for (k, v) in extra {
            match v {
                Some(val) => std::env::set_var(k, val),
                None => std::env::remove_var(k),
            }
        }
    }

    StateDirGuard {
        _restore: restore,
        _tmp: tmp,
        _lock: lock,
        path,
    }
}

/// Restores arbitrary env vars and releases [`GLOBAL_ENV_MUTEX`] when
/// dropped. Obtain via [`env_guard`].
#[cfg(any(test, debug_assertions))]
pub struct EnvGuard {
    _restore: EnvRestore,
    _lock: MutexGuard<'static, ()>,
}

/// Apply `vars` for the lifetime of the returned guard, restoring the
/// prior values (set or unset) on drop — including on panic.
///
/// Same non-reentrancy caveat as [`with_env_vars`]: do not nest with a
/// state-dir guard; use [`state_dir_guard_with`] instead.
#[cfg(any(test, debug_assertions))]
pub fn env_guard(vars: &[(&str, Option<&str>)]) -> EnvGuard {
    let lock = GLOBAL_ENV_MUTEX
        .lock()
        .unwrap_or_else(|p| p.into_inner());
    let names: Vec<&str> = vars.iter().map(|(k, _)| *k).collect();
    let restore = EnvRestore::capture(&names);
    unsafe {
        for (k, v) in vars {
            match v {
                Some(val) => std::env::set_var(k, val),
                None => std::env::remove_var(k),
            }
        }
    }
    EnvGuard {
        _restore: restore,
        _lock: lock,
    }
}

/// The save-and-restore half, on its own so both guards share ONE
/// implementation of "put it back exactly as it was".
///
/// `None` means the variable was UNSET before, and restoring it means
/// unsetting it again — which is the one case the 49 hand-rolled sites
/// collapsed into "always unset".
#[cfg(any(test, debug_assertions))]
struct EnvRestore {
    saved: Vec<(String, Option<OsString>)>,
}

#[cfg(any(test, debug_assertions))]
impl EnvRestore {
    fn capture(names: &[&str]) -> Self {
        Self {
            saved: names
                .iter()
                .map(|k| (k.to_string(), std::env::var_os(k)))
                .collect(),
        }
    }
}

#[cfg(any(test, debug_assertions))]
impl Drop for EnvRestore {
    fn drop(&mut self) {
        // Restore in reverse capture order so a duplicated name in
        // `extra` resolves to the value captured FIRST (the true prior).
        unsafe {
            for (k, prior) in self.saved.iter().rev() {
                match prior {
                    Some(v) => std::env::set_var(k, v),
                    None => std::env::remove_var(k),
                }
            }
        }
    }
}

/// Probe whether Python 3 with `vco_lib` is importable in this environment.
///
/// Used as a skip guard for tests that subprocess into Python
/// (e.g. `refresh_project_env_with_db_re_runs_env_writer`). Returns
/// `true` only if:
///  1. A `python3` binary is reachable on PATH (or via the VCT venv
///     resolution chain), AND
///  2. `import vco_lib` succeeds in that interpreter.
///
/// Not gated on `cfg(test)` so it's usable from the launcher crate's
/// tests too.
#[cfg(any(test, debug_assertions))]
pub fn python_env_available() -> bool {
    // Fast path: check whether python3 can import vco_lib.
    // We use the same interpreter discovery order as
    // resolve_python_for_vco_lib_local() but do it cheaply without
    // pulling in that function (which has many deps). A simple `which
    // python3` + subprocess is sufficient for the probe.
    let output = std::process::Command::new("python3")
        .arg("-c")
        .arg("import vco_lib")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output();
    matches!(output, Ok(out) if out.status.success())
}

/// Return `true` if the on-disk launcher DB exists (i.e. the launcher has
/// been run at least once in this environment). Used alongside
/// `python_env_available` to gate integration-level tests that require
/// both an on-disk DB and a working Python env.
#[cfg(any(test, debug_assertions))]
pub fn has_launcher_db() -> bool {
    crate::paths::vct_root_dir().join("launcher.db").exists()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn with_state_dir_sets_and_restores_var() {
        unsafe {
            std::env::set_var("VCT_STATE_DIR", "/prior-value");
        }
        with_state_dir(|root| {
            let now = std::env::var("VCT_STATE_DIR").unwrap();
            assert_eq!(now, root.to_string_lossy());
        });
        // Restored.
        assert_eq!(
            std::env::var("VCT_STATE_DIR").unwrap(),
            "/prior-value"
        );
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    #[test]
    fn with_state_dir_restores_unset_var() {
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
        with_state_dir(|_| {
            assert!(std::env::var_os("VCT_STATE_DIR").is_some());
        });
        // Restored to unset.
        assert!(std::env::var_os("VCT_STATE_DIR").is_none());
    }

    #[test]
    fn with_env_vars_handles_set_and_unset_pairs() {
        unsafe {
            std::env::set_var("VCT_TEST_FOO", "before");
            std::env::remove_var("VCT_TEST_BAR");
        }
        with_env_vars(
            &[
                ("VCT_TEST_FOO", Some("during")),
                ("VCT_TEST_BAR", Some("only-set-here")),
            ],
            || {
                assert_eq!(std::env::var("VCT_TEST_FOO").unwrap(), "during");
                assert_eq!(std::env::var("VCT_TEST_BAR").unwrap(), "only-set-here");
            },
        );
        // Restored.
        assert_eq!(std::env::var("VCT_TEST_FOO").unwrap(), "before");
        assert!(std::env::var_os("VCT_TEST_BAR").is_none());
        unsafe {
            std::env::remove_var("VCT_TEST_FOO");
        }
    }

    #[test]
    fn state_dir_guard_sets_and_restores_a_prior_value() {
        unsafe {
            std::env::set_var("VCT_STATE_DIR", "/prior-guard-value");
        }
        {
            let g = state_dir_guard();
            assert_eq!(
                std::env::var("VCT_STATE_DIR").unwrap(),
                g.path().to_string_lossy(),
                "the guard must point the var at its own scratch dir"
            );
            assert!(g.path().is_dir(), "scratch dir must exist while held");
        }
        assert_eq!(
            std::env::var("VCT_STATE_DIR").unwrap(),
            "/prior-guard-value",
            "the PRIOR value must come back — not an unset"
        );
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    /// The regression this whole module exists for: when the var was
    /// UNSET before, restoring means unsetting — and when it was SET
    /// before (an outer redirect), restoring means putting THAT back.
    /// The 49 hand-rolled sites only ever did the first.
    #[test]
    fn state_dir_guard_restores_an_unset_var_to_unset() {
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
        {
            let g = state_dir_guard();
            assert!(g.path().is_dir());
            assert!(std::env::var_os("VCT_STATE_DIR").is_some());
        }
        assert!(
            std::env::var_os("VCT_STATE_DIR").is_none(),
            "an unset var must be restored to unset"
        );
    }

    #[test]
    fn state_dir_guard_restores_on_panic() {
        unsafe {
            std::env::set_var("VCT_STATE_DIR", "/before-guard-panic");
        }
        let caught = std::panic::catch_unwind(|| {
            let _g = state_dir_guard();
            panic!("intentional");
        });
        assert!(caught.is_err(), "panic must propagate");
        assert_eq!(
            std::env::var("VCT_STATE_DIR").unwrap(),
            "/before-guard-panic",
            "Drop restores during unwind — no catch_unwind needed"
        );
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }

    #[test]
    fn state_dir_guard_with_applies_and_restores_extra_vars() {
        unsafe {
            std::env::set_var("VCT_TEST_EXTRA_KEEP", "outer");
            std::env::remove_var("VCT_TEST_EXTRA_NEW");
        }
        {
            let g = state_dir_guard_with(&[
                ("VCT_TEST_EXTRA_KEEP", Some("inner")),
                ("VCT_TEST_EXTRA_NEW", Some("only-here")),
            ]);
            assert!(g.path().is_dir());
            assert_eq!(std::env::var("VCT_TEST_EXTRA_KEEP").unwrap(), "inner");
            assert_eq!(std::env::var("VCT_TEST_EXTRA_NEW").unwrap(), "only-here");
        }
        assert_eq!(std::env::var("VCT_TEST_EXTRA_KEEP").unwrap(), "outer");
        assert!(std::env::var_os("VCT_TEST_EXTRA_NEW").is_none());
        unsafe {
            std::env::remove_var("VCT_TEST_EXTRA_KEEP");
        }
    }

    /// `extra` may carry `None` to UNSET a var for the duration — the
    /// shape `hub_launcher`'s "nothing resolves" tests need.
    #[test]
    fn state_dir_guard_with_can_unset_a_var_for_the_duration() {
        unsafe {
            std::env::set_var("VCT_TEST_EXTRA_UNSET_ME", "present");
        }
        {
            let _g = state_dir_guard_with(&[("VCT_TEST_EXTRA_UNSET_ME", None)]);
            assert!(std::env::var_os("VCT_TEST_EXTRA_UNSET_ME").is_none());
        }
        assert_eq!(
            std::env::var("VCT_TEST_EXTRA_UNSET_ME").unwrap(),
            "present",
            "an unset-for-the-duration var must come back"
        );
        unsafe {
            std::env::remove_var("VCT_TEST_EXTRA_UNSET_ME");
        }
    }

    #[test]
    fn env_guard_restores_set_and_unset_pairs() {
        unsafe {
            std::env::set_var("VCT_TEST_GUARD_FOO", "before");
            std::env::remove_var("VCT_TEST_GUARD_BAR");
        }
        {
            let _g = env_guard(&[
                ("VCT_TEST_GUARD_FOO", Some("during")),
                ("VCT_TEST_GUARD_BAR", Some("only-set-here")),
            ]);
            assert_eq!(std::env::var("VCT_TEST_GUARD_FOO").unwrap(), "during");
            assert_eq!(std::env::var("VCT_TEST_GUARD_BAR").unwrap(), "only-set-here");
        }
        assert_eq!(std::env::var("VCT_TEST_GUARD_FOO").unwrap(), "before");
        assert!(std::env::var_os("VCT_TEST_GUARD_BAR").is_none());
        unsafe {
            std::env::remove_var("VCT_TEST_GUARD_FOO");
        }
    }

    #[test]
    fn with_state_dir_restores_env_after_panic() {
        // The fix here is the `catch_unwind` + `resume_unwind` pattern:
        // if `f` panics, we still restore the prior env state before
        // re-raising. Without that, a panicking test would leak state
        // into the next test that ran on the same thread.
        unsafe {
            std::env::set_var("VCT_STATE_DIR", "/before-panic");
        }
        let caught = std::panic::catch_unwind(|| {
            with_state_dir(|_| panic!("intentional"));
        });
        assert!(caught.is_err(), "panic should propagate");
        assert_eq!(
            std::env::var("VCT_STATE_DIR").unwrap(),
            "/before-panic",
            "env restored even after panic"
        );
        unsafe {
            std::env::remove_var("VCT_STATE_DIR");
        }
    }
}
