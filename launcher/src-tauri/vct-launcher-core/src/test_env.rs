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

#[cfg(any(test, debug_assertions))]
use std::path::Path;
#[cfg(any(test, debug_assertions))]
use std::sync::Mutex;

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
    let _g = GLOBAL_ENV_MUTEX
        .lock()
        .unwrap_or_else(|p| p.into_inner());
    let tmp = tempfile::tempdir().expect("tempdir for with_state_dir");
    let prior = std::env::var_os("VCT_STATE_DIR");
    // Safety: we hold GLOBAL_ENV_MUTEX so no other env-mutating test
    // can race us. The set_var + remove_var pair runs while the lock
    // is held.
    unsafe {
        std::env::set_var("VCT_STATE_DIR", tmp.path());
    }
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f(tmp.path())));
    // Restore prior value (set or unset).
    unsafe {
        match prior {
            Some(v) => std::env::set_var("VCT_STATE_DIR", v),
            None => std::env::remove_var("VCT_STATE_DIR"),
        }
    }
    if let Err(payload) = result {
        std::panic::resume_unwind(payload);
    }
}

/// Run `f` with arbitrary env-var overrides. `vars` is `&[(name,
/// value)]` where `value=Some("...")` sets the var and `None`
/// unsets it. Prior values are restored after `f` returns.
///
/// Useful when a test needs to set BOTH `VCT_STATE_DIR` and
/// `VCT_HUB_PORT` (or `HOME`, `PATH`, etc.) together. The single-
/// call wraps the lock acquire + restore boilerplate.
#[cfg(any(test, debug_assertions))]
pub fn with_env_vars<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
    let _g = GLOBAL_ENV_MUTEX
        .lock()
        .unwrap_or_else(|p| p.into_inner());
    let saved: Vec<(String, Option<std::ffi::OsString>)> = vars
        .iter()
        .map(|(k, _)| (k.to_string(), std::env::var_os(k)))
        .collect();
    unsafe {
        for (k, v) in vars {
            match v {
                Some(val) => std::env::set_var(k, val),
                None => std::env::remove_var(k),
            }
        }
    }
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(f));
    unsafe {
        for (k, prior) in saved {
            match prior {
                Some(v) => std::env::set_var(&k, v),
                None => std::env::remove_var(&k),
            }
        }
    }
    if let Err(payload) = result {
        std::panic::resume_unwind(payload);
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
