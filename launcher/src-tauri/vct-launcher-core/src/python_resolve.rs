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
//!   2/3. `$VCT_INSTALL_ROOT` — orchestrator clone root. Probes
//!      `<root>/.venv` then `<root>/claude_mcp_servers/.venv`.
//!   4. Walk up from `current_exe()` (≤8 hops) probing the same two venv
//!      layouts — covers launcher-binary runs where neither env var is set.
//!   5. PATH fallback (`python3` / `python.exe`). Last resort; the caller
//!      should still handle this gracefully because a PATH `python3` on a
//!      PEP-668 machine frequently cannot `import weaviate`.
//!
//! Each venv layout is probed for `bin/python`, `bin/python3` (POSIX) and
//! `Scripts/python.exe` (Windows) so a single call works cross-OS.
//!
//! This module is pure `std` (no tauri, no tokio) so it lives in
//! `vct-launcher-core` and is shared by both the launcher GUI binary and any
//! other consumer without dragging heavy deps.

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
/// Always returns `Some(_)`: the final tier is a PATH fallback
/// (`python3` on POSIX, `python.exe` on Windows) so callers can treat `None`
/// as impossible, but the caller SHOULD still guard defensively — a PATH
/// python may lack `import weaviate` on a PEP-668 machine. The reference
/// call-pattern is:
///
/// ```ignore
/// let py = resolve_python_for_vco_lib()
///     .unwrap_or_else(|| PathBuf::from(&system.python_cmd));
/// ```
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

    // 2 + 3. $VCT_INSTALL_ROOT — orchestrator clone root.
    if let Ok(root) = std::env::var("VCT_INSTALL_ROOT") {
        if let Some(p) = venv_in(Path::new(&root)) {
            return Some(p);
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

    // 5. PATH fallback.
    Some(PathBuf::from(if cfg!(target_os = "windows") {
        "python.exe"
    } else {
        "python3"
    }))
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

    #[test]
    fn always_falls_back_to_path_python() {
        let _g = env_lock().lock().unwrap();
        // Point both env vars at empty dirs with no venv; the exe-walk may
        // find a real venv on a dev box, but the function must NEVER return
        // None — the PATH fallback guarantees Some(_).
        let d = tmpdir("nofallback");
        let saved_venv = std::env::var_os("VCT_VENV");
        let saved_root = std::env::var_os("VCT_INSTALL_ROOT");
        unsafe {
            std::env::set_var("VCT_VENV", &d);
            std::env::set_var("VCT_INSTALL_ROOT", &d);
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
        }
        assert!(resolved.is_some(), "PATH fallback must guarantee Some(_)");
    }
}
