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
//! `~/code/orch/` and a production launcher installed at
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

/// Path to the "finetune in flight" sentinel for a global module
/// (v0.2.61, Option H B1 fix).
///
/// The launcher's background finetune task (`run_finetune_then_rotate_async`)
/// CREATES this file when it kicks `/finetune` and REMOVES it on exit
/// (success OR failure). The hub's boot resume sweep CONSULTS it before
/// recreating a running global container: if the sentinel is present the
/// recreate (which would `podman rm -f` the container and kill the in-flight
/// training job) is DEFERRED — the hub instead schedules a background
/// re-check that performs the re-mint once the job finishes.
///
/// Defined here, in the shared `vct-launcher-core`, so the WRITER (launcher
/// process) and the READER (hub process) resolve the SAME path — the two are
/// separate processes and an ad-hoc per-process path string would silently
/// drift. `module_id` is sanitized to a filename-safe form (it's a catalog
/// id like `vct-rl-reranker`, already filename-safe, but we guard anyway).
pub fn finetune_sentinel_path(module_id: &str) -> PathBuf {
    let safe: String = module_id
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .collect();
    vct_root_dir().join(format!("{}.finetuning", safe))
}

/// Resolve `name` to the first matching executable on `$PATH`.
///
/// v0.2.77 (Part 7c task 3): the ONE home for the "is this binary on
/// PATH / where is it" lookup. Before this, three private copies drifted:
///   - `services::runtime::which_on_path` — the richest: on Windows also
///     tries `.exe` / `.cmd` / `.bat`. Used to find podman/docker/node.
///   - `commands::installer::which_on_path` — `Option<PathBuf>`, but NO
///     Windows extension handling (would miss `python.exe` when asked for
///     `python`).
///   - `commands::projects_v2::which_on_path` — returned `bool`, appended
///     ONLY `.exe` on Windows, and used `exists()` (dir OR file) instead
///     of `is_file()`.
///
/// This canonical form takes the superset behaviour: on Windows it probes
/// `name`, `name.exe`, `name.cmd`, `name.bat` (covers interpreters,
/// container runtimes, and node shims); on POSIX just `name`. Matches on
/// `is_file()` (a directory named like the binary is never executable).
///
/// Returns the absolute-ish path of the first hit (PATH dir joined with
/// the resolved candidate). Callers that only need a yes/no answer use
/// `which_on_path(x).is_some()`.
pub fn which_on_path(name: &str) -> Option<PathBuf> {
    #[cfg(windows)]
    let candidates: Vec<String> = vec![
        name.to_string(),
        format!("{}.exe", name),
        format!("{}.cmd", name),
        format!("{}.bat", name),
    ];
    #[cfg(not(windows))]
    let candidates: Vec<String> = vec![name.to_string()];

    let paths = lookup_path()?;
    for dir in std::env::split_paths(&paths) {
        for cand in &candidates {
            let p = dir.join(cand);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

/// Resolve a bundled `.claude/scripts/<bin>` helper via the canonical
/// four-tier ladder, or `None` if it isn't found anywhere.
///
/// v0.2.77 (Part 7c task 3): the ONE home for the "find an installed
/// script by name" motif. `kg_sync::resolve_kg_sync_script` and
/// `kg_summary::resolve_summary_script` were byte-for-byte identical
/// copies of this ladder differing only in the `bin` string; both now
/// delegate here.
///
/// The tiers, in order:
///   1. **Project-local** — `<project_folder>/.claude/scripts/<bin>`. The
///      normal case for an installed project.
///   2. **Env override** — `$VCT_LAUNCHER_SCRIPTS_DIR/<bin>`. Lets a dev
///      launcher point at an in-development scripts dir.
///   3. **Sibling-of-exe** — walk `ORCHESTRATOR_HOP_SUFFIXES` from the
///      launcher binary's directory, probing `<hop>/.claude/scripts/<bin>`
///      at each. Covers a launcher run from inside / next to the
///      orchestrator clone.
///   4. **PATH** — `<path-dir>/<bin>` for each `$PATH` entry (a globally
///      installed copy).
///
/// Matches on `is_file()` at every tier. Note this does NOT append a
/// Windows extension — callers pass the fully-qualified `bin` (e.g.
/// `kg-sync.ps1` on Windows, `kg-sync` on POSIX), matching the existing
/// call-sites' `if cfg!(windows) { "x.ps1" } else { "x" }` selection.
///
/// This is the ladder WITHOUT the codegraph stale-wrapper health guard —
/// that guard (`analyzer_wrapper_is_resilient`) is codegraph-specific and
/// deliberately stays in `commands::codegraph`, which layers it on top of
/// its own tier-1 check before falling through to the shared tiers.
/// Relative hops from the launcher binary's own directory to a candidate
/// orchestrator-clone root, probed as `<exe_dir>/<hop>/.claude/scripts/<bin>`.
///
/// v0.2.92 (field bug 2026-09-05) — the list used to stop at `../..`, and the
/// SHIPPED layout puts the binary at `<root>/launcher/dist/<target>/vct-launcher`,
/// whose root is `../../..`. So on a standard install this tier could never
/// resolve anything: the three probes landed on `dist/<target>/.claude/scripts`,
/// `dist/.claude/scripts` and `launcher/.claude/scripts`, none of which exist.
/// With `$VCT_LAUNCHER_SCRIPTS_DIR` unset and `.claude/scripts` not on `$PATH`
/// — the default for every user — the whole "fall back to the orchestrator
/// copy" mechanism was unreachable, and the code-graph build that relied on it
/// died with "script not found" while a deferral asserted "builds still work".
///
/// `../../..` covers the shipped `launcher/dist/<target>/` layout;
/// `../../../..` covers a cargo dev build at `launcher/src-tauri/target/<profile>/`.
/// Order is nearest-first: a genuinely adjacent clone still wins.
/// The directories every launcher PATH lookup walks ([`which_on_path`], the
/// script ladders): the process `PATH` — unless the calling THREAD injected
/// one with [`with_lookup_path`] (debug/test builds only).
pub fn lookup_path() -> Option<std::ffi::OsString> {
    #[cfg(debug_assertions)]
    if let Some(injected) = INJECTED_LOOKUP_PATH.with(|cell| cell.borrow().clone()) {
        return injected;
    }
    std::env::var_os("PATH")
}

/// The program to hand `Command::new` for the bare name `name`.
///
/// Production, and any thread that injected nothing: `name` itself — the OS
/// resolves it on the process `PATH` at spawn time, exactly as
/// `Command::new(name)` always did. A thread that injected a lookup `PATH`
/// ([`with_lookup_path`], debug/test builds) gets the resolution done HERE
/// over that path — the absolute hit, or, when it holds no such program, a
/// path that cannot exist, so the spawn fails "not found" as it would on a
/// real `PATH` without it. v0.2.97 review R6: this is how a test puts a fake
/// `git` first without setting the process `PATH`.
pub fn spawn_program(name: &str) -> std::ffi::OsString {
    #[cfg(debug_assertions)]
    if INJECTED_LOOKUP_PATH.with(|cell| cell.borrow().is_some()) {
        return match which_on_path(name) {
            Some(hit) => hit.into_os_string(),
            None => std::env::temp_dir()
                .join("vct-injected-lookup-path-has-no")
                .join(name)
                .into_os_string(),
        };
    }
    name.into()
}

#[cfg(debug_assertions)]
thread_local! {
    /// `Some(p)` while a test on this thread injected `p` (`None` = unset).
    static INJECTED_LOOKUP_PATH: std::cell::RefCell<Option<Option<std::ffi::OsString>>> =
        const { std::cell::RefCell::new(None) };
}

/// Test hook: run `f` with [`lookup_path`] answering `path` on THIS thread
/// only (`None` = as if `PATH` were unset). v0.2.97 review R6: a Rust test
/// never sets the process `PATH` — every concurrently running test, and every
/// child another test spawns by bare name (`python3`), shares it, and
/// blanking it made those spawns fail intermittently. Pinned by
/// `tests/test_rust_tests_never_mutate_process_path.py`.
#[cfg(debug_assertions)]
pub fn with_lookup_path<T>(path: Option<&std::ffi::OsStr>, f: impl FnOnce() -> T) -> T {
    struct Restore(Option<Option<std::ffi::OsString>>);
    impl Drop for Restore {
        fn drop(&mut self) {
            let prior = self.0.take();
            INJECTED_LOOKUP_PATH.with(|cell| *cell.borrow_mut() = prior);
        }
    }
    let prior = INJECTED_LOOKUP_PATH.with(|cell| cell.replace(Some(path.map(|p| p.to_os_string()))));
    let _restore = Restore(prior);
    f()
}

pub const ORCHESTRATOR_HOP_SUFFIXES: [&str; 5] =
    [".", "..", "../..", "../../..", "../../../.."];

pub fn resolve_installed_script(project_folder: &std::path::Path, bin: &str) -> Option<PathBuf> {
    // 1. Project-local.
    let p1 = project_folder.join(".claude").join("scripts").join(bin);
    if p1.is_file() {
        return Some(p1);
    }

    // 2. Env override.
    if let Ok(dir) = std::env::var("VCT_LAUNCHER_SCRIPTS_DIR") {
        let p2 = PathBuf::from(dir).join(bin);
        if p2.is_file() {
            return Some(p2);
        }
    }

    // 3. Sibling-of-exe convention.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            for hop in ORCHESTRATOR_HOP_SUFFIXES.iter() {
                let p3 = parent.join(hop).join(".claude").join("scripts").join(bin);
                if p3.is_file() {
                    return Some(p3);
                }
            }
        }
    }

    // 4. PATH lookup.
    if let Some(path) = lookup_path() {
        for d in std::env::split_paths(&path) {
            let p4 = d.join(bin);
            if p4.is_file() {
                return Some(p4);
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    // The environment is process-wide: a test that changes it goes through
    // `test_env` (GLOBAL_ENV_MUTEX + restore-on-drop). v0.2.97 review R6:
    // this module used its own mutex, which ordered only its own tests.
    fn with_env<F: FnOnce()>(key: &str, val: Option<&str>, f: F) {
        crate::test_env::with_env_vars(&[(key, val)], f);
    }

    /// v0.2.92 field bug (2026-09-05): the hop list stopped at `../..`, so on
    /// the SHIPPED layout (`<root>/launcher/dist/<target>/vct-launcher`) tier 3
    /// could never find `<root>/.claude/scripts/<bin>` — and with
    /// `$VCT_LAUNCHER_SCRIPTS_DIR` unset and `.claude/scripts` off `$PATH`
    /// (the default for every user) the whole orchestrator-fallback mechanism
    /// was unreachable. A code-graph build died with "script not found" while
    /// a deferral asserted the fallback was carrying it.
    ///
    /// Mutation check: drop `"../../.."` from `ORCHESTRATOR_HOP_SUFFIXES` and
    /// this test fails on the shipped layout.
    #[test]
    fn orchestrator_hops_reach_both_real_launcher_layouts() {
        let root = std::env::temp_dir().join(format!(
            "vct-hops-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let scripts = root.join(".claude").join("scripts");
        std::fs::create_dir_all(&scripts).unwrap();
        std::fs::write(scripts.join("kg-sync"), b"# $VCT_INSTALL_ROOT\n").unwrap();

        let reaches = |exe_dir: &std::path::Path| -> bool {
            ORCHESTRATOR_HOP_SUFFIXES.iter().any(|hop| {
                exe_dir
                    .join(hop)
                    .join(".claude")
                    .join("scripts")
                    .join("kg-sync")
                    .is_file()
            })
        };

        // Shipped release layout: <root>/launcher/dist/<target>/vct-launcher
        let dist = root.join("launcher").join("dist").join("linux-x64");
        std::fs::create_dir_all(&dist).unwrap();
        assert!(
            reaches(&dist),
            "shipped launcher/dist/<target>/ layout must reach the clone root"
        );

        // Cargo dev layout: <root>/launcher/src-tauri/target/<profile>/vct-launcher
        let devdir = root
            .join("launcher")
            .join("src-tauri")
            .join("target")
            .join("debug");
        std::fs::create_dir_all(&devdir).unwrap();
        assert!(
            reaches(&devdir),
            "cargo target/<profile>/ layout must reach the clone root"
        );

        std::fs::remove_dir_all(&root).ok();
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

    #[test]
    fn which_on_path_finds_a_binary_placed_on_a_temp_path() {
        // Build a temp dir, drop an executable-named file in it, point
        // PATH at ONLY that dir, and confirm the lookup finds it.
        let dir = std::env::temp_dir().join(format!(
            "vct-which-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        std::fs::create_dir_all(&dir).unwrap();
        // On POSIX the bare name is probed; on Windows the `.exe` variant
        // is among the candidates, so name the file accordingly.
        #[cfg(windows)]
        let fname = "vct-fake-bin.exe";
        #[cfg(not(windows))]
        let fname = "vct-fake-bin";
        let bin = dir.join(fname);
        std::fs::write(&bin, b"x").unwrap();

        let hit = with_lookup_path(Some(dir.as_os_str()), || which_on_path("vct-fake-bin"));
        assert_eq!(hit.as_deref(), Some(bin.as_path()));
        assert!(
            with_lookup_path(None, || which_on_path("vct-fake-bin")).is_none(),
            "an unset PATH finds nothing"
        );
    }

    #[test]
    fn resolve_installed_script_finds_project_local_first() {
        let dir = std::env::temp_dir().join(format!(
            "vct-script-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        let scripts = dir.join(".claude").join("scripts");
        std::fs::create_dir_all(&scripts).unwrap();
        let bin = scripts.join("kg-sync");
        std::fs::write(&bin, b"#!/bin/sh\n").unwrap();

        let resolved = resolve_installed_script(&dir, "kg-sync");
        assert_eq!(resolved.as_deref(), Some(bin.as_path()));
    }

    #[test]
    fn resolve_installed_script_none_when_absent() {
        // Neutralise the env-override tier (restored on drop) — and inject
        // the PATH tier below — so a stray dev VCT_LAUNCHER_SCRIPTS_DIR /
        // PATH entry can't produce a false hit.
        let _env = crate::test_env::env_guard(&[("VCT_LAUNCHER_SCRIPTS_DIR", None)]);
        let dir = std::env::temp_dir().join(format!(
            "vct-script-absent-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let resolved = with_lookup_path(Some(dir.as_os_str()), || {
            resolve_installed_script(&dir, "vct-nonexistent-script-name-xyz")
        });
        assert!(resolved.is_none());
    }

    #[test]
    fn which_on_path_returns_none_for_absent_binary() {
        let dir = std::env::temp_dir().join(format!(
            "vct-which-empty-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let hit = with_lookup_path(Some(dir.as_os_str()), || {
            which_on_path("vct-definitely-absent-binary-xyz")
        });
        assert!(hit.is_none());
    }

    /// v0.2.97 review R6: `spawn_program` is the bare name unless THIS
    /// thread injected a lookup PATH — then it is that PATH's hit, or a path
    /// that cannot exist (a spawn fails "not found", never falls back to the
    /// process PATH's copy).
    #[test]
    fn spawn_program_resolves_over_an_injected_lookup_path_only() {
        assert_eq!(spawn_program("git"), std::ffi::OsString::from("git"));
        let dir = tempfile::tempdir().unwrap();
        let fake = dir.path().join("vct-fake-git");
        std::fs::write(&fake, b"#!/bin/sh\n").unwrap();
        let hit = with_lookup_path(Some(dir.path().as_os_str()), || spawn_program("vct-fake-git"));
        assert_eq!(PathBuf::from(hit), fake);
        let miss = with_lookup_path(Some(dir.path().as_os_str()), || spawn_program("git"));
        let miss = PathBuf::from(miss);
        assert!(miss.is_absolute() && !miss.exists(), "{}", miss.display());
        let unset = with_lookup_path(None, || spawn_program("git"));
        assert!(!PathBuf::from(unset).exists());
        // The injection is per thread: another thread sees the bare name.
        let other = with_lookup_path(Some(dir.path().as_os_str()), || {
            std::thread::spawn(|| spawn_program("vct-fake-git")).join().unwrap()
        });
        assert_eq!(other, std::ffi::OsString::from("vct-fake-git"));
    }
}
