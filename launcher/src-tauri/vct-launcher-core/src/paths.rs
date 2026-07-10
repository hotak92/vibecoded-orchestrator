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

    let paths = std::env::var_os("PATH")?;
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
///   3. **Sibling-of-exe** — walk `.`, `..`, `../..` from the launcher
///      binary's directory, probing `<hop>/.claude/scripts/<bin>` at each.
///      Covers a launcher run from inside / next to the orchestrator clone.
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
            for hop in [".", "..", "../.."].iter() {
                let p3 = parent.join(hop).join(".claude").join("scripts").join(bin);
                if p3.is_file() {
                    return Some(p3);
                }
            }
        }
    }

    // 4. PATH lookup.
    if let Ok(path) = std::env::var("PATH") {
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
    use std::sync::Mutex;

    // VCT_STATE_DIR is process-wide; serialise tests that mutate it so
    // parallel runs don't observe each other.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_env<F: FnOnce()>(key: &str, val: Option<&str>, f: F) {
        let _g = SERIALIZE.lock().unwrap();
        let prev = std::env::var(key).ok();
        match val {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
        f();
        match prev {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
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
        let _g = SERIALIZE.lock().unwrap();
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

        let prev = std::env::var_os("PATH");
        std::env::set_var("PATH", &dir);
        let hit = which_on_path("vct-fake-bin");
        match prev {
            Some(v) => std::env::set_var("PATH", v),
            None => std::env::remove_var("PATH"),
        }
        assert_eq!(hit.as_deref(), Some(bin.as_path()));
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
        let _g = SERIALIZE.lock().unwrap();
        let dir = std::env::temp_dir().join(format!(
            "vct-script-absent-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        std::fs::create_dir_all(&dir).unwrap();
        // Neutralise the env-override and PATH tiers so a stray dev
        // VCT_LAUNCHER_SCRIPTS_DIR / PATH entry can't produce a false hit.
        let prev_scripts = std::env::var_os("VCT_LAUNCHER_SCRIPTS_DIR");
        let prev_path = std::env::var_os("PATH");
        std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR");
        std::env::set_var("PATH", &dir);
        let resolved =
            resolve_installed_script(&dir, "vct-nonexistent-script-name-xyz");
        match prev_scripts {
            Some(v) => std::env::set_var("VCT_LAUNCHER_SCRIPTS_DIR", v),
            None => std::env::remove_var("VCT_LAUNCHER_SCRIPTS_DIR"),
        }
        match prev_path {
            Some(v) => std::env::set_var("PATH", v),
            None => std::env::remove_var("PATH"),
        }
        assert!(resolved.is_none());
    }

    #[test]
    fn which_on_path_returns_none_for_absent_binary() {
        let _g = SERIALIZE.lock().unwrap();
        let dir = std::env::temp_dir().join(format!(
            "vct-which-empty-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let prev = std::env::var_os("PATH");
        std::env::set_var("PATH", &dir);
        let hit = which_on_path("vct-definitely-absent-binary-xyz");
        match prev {
            Some(v) => std::env::set_var("PATH", v),
            None => std::env::remove_var("PATH"),
        }
        assert!(hit.is_none());
    }
}
