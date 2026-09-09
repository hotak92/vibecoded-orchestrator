//! Shared Python-interpreter resolution for `vco_lib` / analyzer spawns.
//!
//! Before v0.2.77 this "RT-4 ladder" was copy-pasted four times across the
//! launcher command layer, each copy drifting:
//!
//!   - `commands::projects_v2::resolve_python_for_vco_lib_local` — the most
//!     complete: `$VCT_VENV` → `$VCT_INSTALL_ROOT`×2 → exe-walk≤8 → PATH.
//!   - `commands::codegraph_reanalyze::resolve_python_for_analyzer` — exe-walk
//!     ONLY (missing the `$VCT_VENV` / `$VCT_INSTALL_ROOT` tiers). A project
//!     without its own `.venv` therefore fell straight to a system `python3`
//!     that can't `import weaviate`, so codegraph re-analysis spawned with a
//!     broken interpreter.
//!   - `commands::embedding_catalog::resolve_python_for_vco_lib` — exe-walk +
//!     PATH.
//!   - `commands::embedding_enrichment::resolve_python_for_vco_lib` — exe-walk
//!     + PATH.
//!
//! Consolidating to one home (the "search before you add, extract before you
//! duplicate" rule) means the missing-tiers bug in `codegraph_reanalyze` is
//! fixed for free: every call-site now walks the full ladder.
//!
//! ## The ladder (canonical order)
//!
//!   1. `$VCT_VENV` — explicit override. May point at a venv DIR or straight
//!      at the interpreter binary; both shapes are honoured.
//!   2/3. `$VCT_INSTALL_ROOT`, then `$VCT_ORCHESTRATOR_ROOT` — the
//!      orchestrator clone root. Each probes `<root>/.venv` then
//!      `<root>/claude_mcp_servers/.venv`.
//!   4. Walk up from `current_exe()` (≤8 hops) probing the same two venv
//!      layouts — covers launcher-binary runs where neither env var is set.
//!   5. NOTHING. `None` — v0.2.94 removed the bare-PATH rung: it made every
//!      caller's `ok_or_else(...)` unreachable while handing back a name that
//!      on a PEP-668 machine cannot `import weaviate`. A caller that wants a
//!      deliberate fallback names it via `resolve_python_for_vco_lib_or`.
//!
//! Each venv layout is probed for `bin/python`, `bin/python3` (POSIX) and
//! `Scripts/python.exe` (Windows) so a single call works cross-OS.
//!
//! This module is pure `std` (no tauri, no tokio) so it lives in
//! `vct-launcher-core` and is shared by both the launcher GUI binary and any
//! other consumer without dragging heavy deps.
//!
//! ## Cross-language pin (v0.2.94)
//!
//! **MUST MATCH `vco_lib/python_exe.py`** — the Python half of this same
//! ladder. It is a C-tier mirror and justified as one: this side has to find a
//! Python interpreter BEFORE it can ask Python anything, which is the single
//! shape A-tier (call the one implementation via a subprocess) cannot cover.
//! Only the DATA is duplicated — the env-var names, the venv layouts, and the
//! interpreter file names below — and
//! `tests/test_v0294_python_exe_parity.py` extracts all three from THIS FILE
//! and asserts them against the Python constants, so the two cannot drift.
//!
//! The 2026-09-09 field defect is why the pin exists: the launcher's bundle
//! path was NOT using this ladder (it spawned `python -m vco_lib.project_init
//! install-bundle --update` under `detect_system()`'s bare PATH probe), so
//! every detached child of that update died on `ModuleNotFoundError: No module
//! named 'vco_lib'`. `system.python_cmd` is BOOTSTRAP python — correct for the
//! first-install flow, which runs before any venv exists — and is never the
//! answer for a process that imports our own package.

use std::path::{Path, PathBuf};

/// Probe the two known venv layouts under `root` for a python interpreter.
///
/// Returns the first existing `bin/python` / `bin/python3` /
/// `Scripts/python.exe` under `<root>/.venv` or
/// `<root>/claude_mcp_servers/.venv`.
fn venv_in(root: &Path) -> Option<PathBuf> {
    for layout in [
        root.join(".venv"),
        root.join("claude_mcp_servers").join(".venv"),
    ] {
        for candidate in [
            layout.join("bin").join("python"),
            layout.join("bin").join("python3"),
            layout.join("Scripts").join("python.exe"),
        ] {
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

/// Resolve a Python interpreter capable of running `vco_lib` / the code-graph
/// analyzer, walking the canonical RT-4 ladder documented at module level.
///
/// Returns `None` when NO tier qualifies. That is the answer, not a gap: a
/// caller that cannot get a vco_lib-capable interpreter must say so rather
/// than spawn one that will fail on `import vco_lib` in a log nobody reads.
/// The reference call-pattern is therefore a refusal:
///
/// ```ignore
/// let Some(py) = resolve_python_for_vco_lib() else {
///     return Err("no Python environment with VCO's dependencies …".into());
/// };
/// ```
/// `String`-returning convenience wrapper over [`resolve_python_for_vco_lib`].
///
/// Several launcher-side deferral emitters (`storage_ux`,
/// `chunker_revision_deferral`, `git_user_editable_merge`, `projects_v2`
/// rename, `module_updates`) historically each carried their own PATH-only
/// `pick_python` copy that returned a `String` interpreter path. Those copies
/// resolved ONLY from `$PATH` — a strictly WEAKER resolution than the RT-4
/// ladder: on a machine whose PATH `python3` is a PEP-668 system interpreter
/// without `vco_lib`/`weaviate` importable, the deferral `-c` snippet
/// (`import ...vco_lib.deferral_report`) would fail and the deferral silently
/// go unwritten. Routing them through the full ladder makes the emitter pick
/// the orchestrator venv (which HAS `vco_lib`) first, falling back to PATH only
/// as the last resort — the intended behaviour upgrade.
///
/// Returns `None` when no tier qualifies (v0.2.94: the underlying resolver no
/// longer ends in a PATH rung, so this is now a REACHABLE answer — which is
/// what the `is_none()` guards at the deferral call-sites were always written
/// for).
pub fn resolve_python_for_vco_lib_str() -> Option<String> {
    resolve_python_for_vco_lib().map(|p| p.to_string_lossy().to_string())
}

/// v0.2.94: THE program for a `python -m vco_lib.*` spawn, with the caller's
/// own last-resort fallback (in the launcher: `system.python_cmd`).
///
/// One home for the `resolve_python_for_vco_lib().unwrap_or_else(|| PathBuf::
/// from(&system.python_cmd))` idiom, which had been written out six times in
/// `commands/projects_v2.rs` alone — while three OTHER spawns in the same file
/// (the bundle update among them) still used the bare fallback directly. That
/// asymmetry is the 2026-09-09 field defect: a convention that must be
/// REMEMBERED at each call-site is a convention some call-site will forget.
pub fn resolve_python_for_vco_lib_or(fallback: &str) -> PathBuf {
    resolve_python_for_vco_lib().unwrap_or_else(|| PathBuf::from(fallback))
}

pub fn resolve_python_for_vco_lib() -> Option<PathBuf> {
    // 1. $VCT_VENV — explicit override. Accept both "venv dir" and
    //    "interpreter binary path" shapes.
    if let Ok(v) = std::env::var("VCT_VENV") {
        let base = Path::new(&v);
        for candidate in [
            base.join("bin").join("python"),
            base.join("bin").join("python3"),
            base.join("Scripts").join("python.exe"),
        ] {
            if candidate.is_file() {
                return Some(candidate);
            }
        }
        // $VCT_VENV may itself be the interpreter binary (not a venv dir).
        if base.is_file() {
            return Some(base.to_path_buf());
        }
    }

    // 2 + 3. The orchestrator clone root, from either env var, in the same
    // order `vco_lib/python_exe.py::INSTALL_ROOT_ENV_VARS` uses.
    //
    // v0.2.94 review item 2b: this side read only `VCT_INSTALL_ROOT`, so a
    // process with a valid `VCT_ORCHESTRATOR_ROOT` and no `VCT_INSTALL_ROOT`
    // (the shape hooks, `project_move` and `boot_service` publish) resolved
    // differently here than in the Python half of the SAME ladder. All four
    // ladders now read the same three env vars.
    for var in ["VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"] {
        if let Ok(root) = std::env::var(var) {
            if let Some(p) = venv_in(Path::new(&root)) {
                return Some(p);
            }
        }
    }

    // 4. Walk up from current_exe — covers launcher-binary runs.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let mut cur = parent.to_path_buf();
            for _ in 0..8 {
                if let Some(p) = venv_in(&cur) {
                    return Some(p);
                }
                if !cur.pop() {
                    break;
                }
            }
        }
    }

    // NO PATH FALLBACK (v0.2.94 review item 2c).
    //
    // This used to end in `Some("python3")`, which made every `ok_or_else(...)`
    // on the calling side dead code: a caller asking "did the ladder find a
    // vco_lib-capable interpreter?" was always told yes, and the answer was a
    // bare name that on a PEP-668 machine cannot `import weaviate` — the exact
    // 2026-09-09 field shape, one layer up. `None` means "no qualifying
    // interpreter"; a caller that genuinely wants a deliberate fallback asks
    // for one BY NAME via `resolve_python_for_vco_lib_or`.
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::{Mutex, OnceLock};

    // Env-var mutation is process-global; serialize these tests so parallel
    // runs don't clobber each other's $VCT_VENV / $VCT_INSTALL_ROOT.
    fn env_lock() -> &'static Mutex<()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
    }

    fn tmpdir(label: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-pyresolve-{}-{}-{}",
            label,
            std::process::id(),
            // cheap unique suffix without pulling uuid into core
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    /// Create a fake venv python under `<root>/.venv/bin/python` (POSIX
    /// layout — the test binary runs on the host OS; on Windows CI the
    /// exe-walk/PATH tiers still exercise the same code, and this specific
    /// test is POSIX-gated).
    #[cfg(unix)]
    fn make_venv(root: &Path) -> PathBuf {
        let bin = root.join(".venv").join("bin");
        fs::create_dir_all(&bin).unwrap();
        let py = bin.join("python");
        fs::write(&py, b"#!/bin/sh\nexit 0\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        let mut perms = fs::metadata(&py).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&py, perms).unwrap();
        py
    }

    #[cfg(unix)]
    #[test]
    fn vct_venv_override_wins() {
        let _g = env_lock().lock().unwrap();
        let d = tmpdir("override");
        // $VCT_VENV points at the venv DIR.
        let bin = d.join("bin");
        fs::create_dir_all(&bin).unwrap();
        let py = bin.join("python");
        fs::write(&py, b"x").unwrap();

        let saved = std::env::var_os("VCT_VENV");
        let saved_root = std::env::var_os("VCT_INSTALL_ROOT");
        unsafe {
            std::env::set_var("VCT_VENV", &d);
            std::env::remove_var("VCT_INSTALL_ROOT");
        }
        let resolved = resolve_python_for_vco_lib();
        // restore
        unsafe {
            match saved {
                Some(v) => std::env::set_var("VCT_VENV", v),
                None => std::env::remove_var("VCT_VENV"),
            }
            if let Some(v) = saved_root {
                std::env::set_var("VCT_INSTALL_ROOT", v);
            }
        }
        assert_eq!(resolved, Some(py));
    }

    #[cfg(unix)]
    #[test]
    fn install_root_venv_resolves() {
        let _g = env_lock().lock().unwrap();
        let d = tmpdir("root");
        let py = make_venv(&d);

        let saved_venv = std::env::var_os("VCT_VENV");
        let saved_root = std::env::var_os("VCT_INSTALL_ROOT");
        unsafe {
            std::env::remove_var("VCT_VENV");
            std::env::set_var("VCT_INSTALL_ROOT", &d);
        }
        let resolved = resolve_python_for_vco_lib();
        unsafe {
            if let Some(v) = saved_venv {
                std::env::set_var("VCT_VENV", v);
            }
            match saved_root {
                Some(v) => std::env::set_var("VCT_INSTALL_ROOT", v),
                None => std::env::remove_var("VCT_INSTALL_ROOT"),
            }
        }
        assert_eq!(resolved, Some(py));
    }

    /// v0.2.94 review item 2c: the ladder no longer invents an answer.
    ///
    /// Pointing every env tier at a venv-less directory used to yield
    /// `Some("python3")`, which made each caller's `ok_or_else(...)` /
    /// `is_none()` guard unreachable and handed back a name that on a PEP-668
    /// machine cannot `import weaviate`. The exe-walk tier can still resolve a
    /// real venv on a dev box, so this asserts what is invariant: whatever
    /// comes back is a real interpreter FILE, never a bare program name.
    #[test]
    fn never_returns_a_bare_program_name() {
        let _g = env_lock().lock().unwrap();
        let d = tmpdir("nofallback");
        let saved_venv = std::env::var_os("VCT_VENV");
        let saved_root = std::env::var_os("VCT_INSTALL_ROOT");
        let saved_orch = std::env::var_os("VCT_ORCHESTRATOR_ROOT");
        unsafe {
            std::env::set_var("VCT_VENV", &d);
            std::env::set_var("VCT_INSTALL_ROOT", &d);
            std::env::set_var("VCT_ORCHESTRATOR_ROOT", &d);
        }
        let resolved = resolve_python_for_vco_lib();
        unsafe {
            match saved_venv {
                Some(v) => std::env::set_var("VCT_VENV", v),
                None => std::env::remove_var("VCT_VENV"),
            }
            match saved_root {
                Some(v) => std::env::set_var("VCT_INSTALL_ROOT", v),
                None => std::env::remove_var("VCT_INSTALL_ROOT"),
            }
            match saved_orch {
                Some(v) => std::env::set_var("VCT_ORCHESTRATOR_ROOT", v),
                None => std::env::remove_var("VCT_ORCHESTRATOR_ROOT"),
            }
        }
        if let Some(p) = resolved {
            assert!(
                p.is_file(),
                "the ladder may only return a real interpreter file; got {p:?}"
            );
            assert!(
                p.components().count() > 1,
                "a bare program name is not a resolution; got {p:?}"
            );
        }
    }

    /// The SECOND install-root env var resolves too (parity with
    /// `python_exe.INSTALL_ROOT_ENV_VARS`, whose order this mirrors).
    #[cfg(unix)]
    #[test]
    fn orchestrator_root_env_var_resolves() {
        let _g = env_lock().lock().unwrap();
        let d = tmpdir("orchroot");
        let py = make_venv(&d);

        let saved_venv = std::env::var_os("VCT_VENV");
        let saved_root = std::env::var_os("VCT_INSTALL_ROOT");
        let saved_orch = std::env::var_os("VCT_ORCHESTRATOR_ROOT");
        unsafe {
            std::env::remove_var("VCT_VENV");
            std::env::remove_var("VCT_INSTALL_ROOT");
            std::env::set_var("VCT_ORCHESTRATOR_ROOT", &d);
        }
        let resolved = resolve_python_for_vco_lib();
        unsafe {
            if let Some(v) = saved_venv {
                std::env::set_var("VCT_VENV", v);
            }
            if let Some(v) = saved_root {
                std::env::set_var("VCT_INSTALL_ROOT", v);
            }
            match saved_orch {
                Some(v) => std::env::set_var("VCT_ORCHESTRATOR_ROOT", v),
                None => std::env::remove_var("VCT_ORCHESTRATOR_ROOT"),
            }
        }
        assert_eq!(resolved, Some(py));
    }
}
