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

/// A held [`GLOBAL_ENV_MUTEX`]. Also the proof a helper asks for when it
/// mutates the environment on its caller's behalf: `fn set(_held: &EnvLock,
/// …)` can only be called by code that holds the lock.
///
/// v0.2.97 review R6: every Rust test that mutates the process environment
/// holds THIS lock — a per-module `static SERIALIZE` (or `#[serial]`) only
/// orders the tests of one module, while the environment is shared by every
/// test in the binary and every child they spawn. Pinned by
/// `tests/test_rust_tests_never_mutate_process_path.py`.
#[cfg(any(test, debug_assertions))]
pub type EnvLock = MutexGuard<'static, ()>;

/// Take [`GLOBAL_ENV_MUTEX`] (recovering from poison) for a test that
/// mutates the process environment directly because a process-env read is
/// the production contract it pins. Prefer [`env_guard`] /
/// [`state_dir_guard_with`], which also restore the prior values.
///
/// NOT reentrant: never call [`env_guard`], [`with_env_vars`],
/// [`state_dir_guard`] or [`with_state_dir`] while holding it.
#[cfg(any(test, debug_assertions))]
pub fn env_lock() -> EnvLock {
    GLOBAL_ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner())
}

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
    let lock = env_lock();
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
    let lock = env_lock();
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

    // v0.2.97 review R6: these tests used to plant a bogus `VCT_STATE_DIR`
    // (or remove it) OUTSIDE the lock, so every concurrent test in this
    // binary that read `vct_root_dir()` saw the plant — or, after a
    // `remove_var`, the developer's REAL `~/.vct`. Now:
    //
    //   * the prior-value mechanics (set → restored, unset → unset, panic)
    //     are pinned on PRIVATE variable names, planted under `env_lock()`;
    //   * the guards' `VCT_STATE_DIR` handling is pinned against the
    //     BASELINE the harness runs with, read and re-read under the lock
    //     (every mutator restores before it releases, so the baseline is the
    //     value whenever nobody holds the lock).

    fn read_locked(name: &str) -> Option<OsString> {
        let _held = env_lock();
        std::env::var_os(name)
    }

    fn plant(name: &str, value: Option<&str>) {
        let _held = env_lock();
        unsafe {
            match value {
                Some(v) => std::env::set_var(name, v),
                None => std::env::remove_var(name),
            }
        }
    }

    #[test]
    fn with_state_dir_sets_and_restores_var() {
        let baseline = read_locked("VCT_STATE_DIR");
        with_state_dir(|root| {
            let now = std::env::var("VCT_STATE_DIR").unwrap();
            assert_eq!(now, root.to_string_lossy());
        });
        assert_eq!(read_locked("VCT_STATE_DIR"), baseline, "restored to the baseline");
    }

    #[test]
    fn with_env_vars_handles_set_and_unset_pairs() {
        plant("VCT_TEST_FOO", Some("before"));
        plant("VCT_TEST_BAR", None);
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
        assert_eq!(read_locked("VCT_TEST_FOO").as_deref(), Some(std::ffi::OsStr::new("before")));
        assert!(read_locked("VCT_TEST_BAR").is_none());
        plant("VCT_TEST_FOO", None);
    }

    #[test]
    fn state_dir_guard_points_at_its_scratch_dir_and_restores_the_baseline() {
        let baseline = read_locked("VCT_STATE_DIR");
        {
            let g = state_dir_guard();
            assert_eq!(
                std::env::var("VCT_STATE_DIR").unwrap(),
                g.path().to_string_lossy(),
                "the guard must point the var at its own scratch dir"
            );
            assert!(g.path().is_dir(), "scratch dir must exist while held");
        }
        assert_eq!(read_locked("VCT_STATE_DIR"), baseline, "the PRIOR value must come back");
    }

    /// The regression this whole module exists for: when a var was UNSET
    /// before, restoring means unsetting — and when it was SET before (an
    /// outer redirect), restoring means putting THAT back. The 49
    /// hand-rolled sites only ever did the first. Pinned on the shared
    /// restore (`EnvRestore`, which every guard uses) with private names.
    #[test]
    fn a_guard_restores_a_prior_value_and_an_unset_var_to_unset() {
        plant("VCT_TEST_RESTORE_SET", Some("outer"));
        plant("VCT_TEST_RESTORE_UNSET", None);
        {
            let _g = state_dir_guard_with(&[
                ("VCT_TEST_RESTORE_SET", Some("inner")),
                ("VCT_TEST_RESTORE_UNSET", Some("inner")),
            ]);
        }
        assert_eq!(
            read_locked("VCT_TEST_RESTORE_SET").as_deref(),
            Some(std::ffi::OsStr::new("outer")),
            "the PRIOR value must come back — not an unset"
        );
        assert!(read_locked("VCT_TEST_RESTORE_UNSET").is_none(), "an unset var is restored to unset");
        plant("VCT_TEST_RESTORE_SET", None);
    }

    #[test]
    fn state_dir_guard_restores_on_panic() {
        let baseline = read_locked("VCT_STATE_DIR");
        plant("VCT_TEST_PANIC_EXTRA", Some("before-guard-panic"));
        let caught = std::panic::catch_unwind(|| {
            let _g = state_dir_guard_with(&[("VCT_TEST_PANIC_EXTRA", Some("during"))]);
            panic!("intentional");
        });
        assert!(caught.is_err(), "panic must propagate");
        assert_eq!(read_locked("VCT_STATE_DIR"), baseline, "Drop restores during unwind");
        assert_eq!(
            read_locked("VCT_TEST_PANIC_EXTRA").as_deref(),
            Some(std::ffi::OsStr::new("before-guard-panic"))
        );
        plant("VCT_TEST_PANIC_EXTRA", None);
    }

    #[test]
    fn state_dir_guard_with_applies_and_restores_extra_vars() {
        plant("VCT_TEST_EXTRA_KEEP", Some("outer"));
        plant("VCT_TEST_EXTRA_NEW", None);
        {
            let g = state_dir_guard_with(&[
                ("VCT_TEST_EXTRA_KEEP", Some("inner")),
                ("VCT_TEST_EXTRA_NEW", Some("only-here")),
            ]);
            assert!(g.path().is_dir());
            assert_eq!(std::env::var("VCT_TEST_EXTRA_KEEP").unwrap(), "inner");
            assert_eq!(std::env::var("VCT_TEST_EXTRA_NEW").unwrap(), "only-here");
        }
        assert_eq!(read_locked("VCT_TEST_EXTRA_KEEP").as_deref(), Some(std::ffi::OsStr::new("outer")));
        assert!(read_locked("VCT_TEST_EXTRA_NEW").is_none());
        plant("VCT_TEST_EXTRA_KEEP", None);
    }

    /// `extra` may carry `None` to UNSET a var for the duration.
    #[test]
    fn state_dir_guard_with_can_unset_a_var_for_the_duration() {
        plant("VCT_TEST_EXTRA_UNSET_ME", Some("present"));
        {
            let _g = state_dir_guard_with(&[("VCT_TEST_EXTRA_UNSET_ME", None)]);
            assert!(std::env::var_os("VCT_TEST_EXTRA_UNSET_ME").is_none());
        }
        assert_eq!(
            read_locked("VCT_TEST_EXTRA_UNSET_ME").as_deref(),
            Some(std::ffi::OsStr::new("present")),
            "an unset-for-the-duration var must come back"
        );
        plant("VCT_TEST_EXTRA_UNSET_ME", None);
    }

    #[test]
    fn env_guard_restores_set_and_unset_pairs() {
        plant("VCT_TEST_GUARD_FOO", Some("before"));
        plant("VCT_TEST_GUARD_BAR", None);
        {
            let _g = env_guard(&[
                ("VCT_TEST_GUARD_FOO", Some("during")),
                ("VCT_TEST_GUARD_BAR", Some("only-set-here")),
            ]);
            assert_eq!(std::env::var("VCT_TEST_GUARD_FOO").unwrap(), "during");
            assert_eq!(std::env::var("VCT_TEST_GUARD_BAR").unwrap(), "only-set-here");
        }
        assert_eq!(read_locked("VCT_TEST_GUARD_FOO").as_deref(), Some(std::ffi::OsStr::new("before")));
        assert!(read_locked("VCT_TEST_GUARD_BAR").is_none());
        plant("VCT_TEST_GUARD_FOO", None);
    }

    #[test]
    fn with_state_dir_restores_env_after_panic() {
        // If `f` panics, the guard's Drop still restores the prior env state
        // during the unwind. Without that, a panicking test would leak state
        // into the next test.
        let baseline = read_locked("VCT_STATE_DIR");
        let caught = std::panic::catch_unwind(|| {
            with_state_dir(|_| panic!("intentional"));
        });
        assert!(caught.is_err(), "panic should propagate");
        assert_eq!(read_locked("VCT_STATE_DIR"), baseline, "env restored even after panic");
    }

    /// `env_lock` is the lock the guards take: while a test holds it, a
    /// guard on another thread waits.
    fn take_a_guard_then_signal(tx: std::sync::mpsc::Sender<()>) {
        let _g = env_guard(&[("VCT_TEST_LOCK_PROBE", Some("x"))]);
        tx.send(()).unwrap();
    }

    #[test]
    fn env_lock_is_the_lock_the_guards_take() {
        let held = env_lock();
        let (tx, rx) = std::sync::mpsc::channel();
        // On ANOTHER thread — taking a guard on this one would deadlock.
        let waiter = std::thread::spawn(move || take_a_guard_then_signal(tx));
        assert!(
            rx.recv_timeout(std::time::Duration::from_millis(200)).is_err(),
            "a guard must not proceed while env_lock() is held"
        );
        drop(held);
        rx.recv_timeout(std::time::Duration::from_secs(10)).expect("the guard proceeds once released");
        waiter.join().unwrap();
        assert!(read_locked("VCT_TEST_LOCK_PROBE").is_none());
    }
}
