//! state/install-manifest.json — version read + refresh helpers (v0.2.8).
//!
//! ## Bug F (v0.2.8): version source priority
//!
//! Pre-v0.2.8 the launcher answered "what version is installed at `<path>`?"
//! by shelling out to `git describe --tags --abbrev=0` against the install
//! tree. That gives the wrong answer in two real-world cases:
//!
//!   1. The install was set up from a release-zip download: no `.git/` at
//!      all → `get_installed_version` errors out.
//!   2. The install was set up from a file-mirror copy (the launcher's own
//!      `copy_orchestrator_to_sync` path, or a `--lightweight` rewrite):
//!      `.git/` history reflects the *source repo's* tag history at copy
//!      time, NOT the contents that were copied across. A user whose
//!      orchestrator clone was at `v0.2.4-baseline` got "v0.2.4" reported back
//!      even after install.py + the bundled vct-module.json had already
//!      moved them to v0.2.7. Result: the launcher's update banner read
//!      "update available, current v0.2.4" and never went away.
//!
//! The fix walks canonical files in this priority order:
//!
//!   1. `<install>/state/install-manifest.json` → `version` field
//!      — written by install.py at every install / update / lightweight
//!        run, and by NOTHING else (v0.2.95 WP-1 — see below).
//!        **Authoritative when present.**
//!   2. `<install>/vct-module.json` → `version` field
//!      — ships with every release, always present in a healthy tree.
//!   3. `<install>/launcher/package.json` → `version` field
//!   4. `<install>/launcher/src-tauri/Cargo.toml` → `[package] version = "…"`
//!      — fallback for dev clones whose `npm install` hasn't been run.
//!   5. `<install>/launcher/src-tauri/tauri.conf.json` → `version`
//!      — Tauri-style fallback.
//!
//! `get_installed_version` falls back to `git describe` only when all five
//! priorities return None (for ancient dev environments that have nothing
//! else). The fallback retains the original "Not a git repository" /
//! "Could not determine version" semantics, so the wizard's error paths
//! don't change.
//!
//! ## Bug G (v0.2.8) — RETIRED v0.2.100 (WP-03b): no Rust-side manifest writer
//!
//! v0.2.8 added `refresh_install_manifest`, a Rust writer for the update
//! paths that advanced the source WITHOUT running `install.py` (the
//! per-clone file copy, the launcher's hard-reset resync, and the launcher
//! self-update's source-only fallback). v0.2.95 (WP-1) stopped it
//! writing `version` and made it set `post_source_only` so that half-installed
//! state was at least visible.
//!
//! v0.2.100 removed every one of those paths (plan AD-1, owner Q1): the ONE
//! update pipeline (`update_run::run_update`) refuses BEFORE any git
//! operation when `install.py --update` cannot run, so no launcher path
//! advances the source without the installer any more — and with no caller
//! left, the writer went too. **`install.py` is the only writer of
//! `state/install-manifest.json`.** `check_for_updates` still reads
//! `post_source_only`: a manifest written by a launcher ≤ 0.2.99 may carry it,
//! and the next real installer run (which rebuilds the manifest from a literal
//! dict) drops it.
//!
use std::fs;
use std::path::Path;

use serde_json::Value;

/// Bug F: pure version-source walk. Returns `Some(version)` from the
/// first source that yields a non-empty string; `None` if every source
/// is missing/malformed/empty. Pure function so it can be unit-tested
/// with synthesized install layouts.
pub(crate) fn read_version_from_install_files(install_path: &Path) -> Option<String> {
    read_version_from_install_files_impl(install_path, true)
}

/// Internal: like `read_version_from_install_files` but with explicit
/// control over whether `state/install-manifest.json` (priority 1) is
/// consulted. The manifest-refresh path needs `include_manifest=false`
/// because it's about to OVERWRITE the manifest — consulting it would
/// just re-write the stale value. External callers
/// (`get_installed_version`) consult the manifest because it's the most
/// authoritative source when an install.py / lightweight / Rust-update
/// path has just refreshed it.
fn read_version_from_install_files_impl(
    install_path: &Path,
    include_manifest: bool,
) -> Option<String> {
    if include_manifest {
        // 1. state/install-manifest.json → version
        let manifest = install_path.join("state").join("install-manifest.json");
        if let Some(v) = read_json_string_field(&manifest, "version") {
            return Some(v);
        }
    }

    // 2. vct-module.json → version
    let vct_module = install_path.join("vct-module.json");
    if let Some(v) = read_json_string_field(&vct_module, "version") {
        return Some(v);
    }

    // 3. launcher/package.json → version
    let pkg_json = install_path.join("launcher").join("package.json");
    if let Some(v) = read_json_string_field(&pkg_json, "version") {
        return Some(v);
    }

    // 4. launcher/src-tauri/Cargo.toml → [package] version = "…"
    let cargo = install_path
        .join("launcher")
        .join("src-tauri")
        .join("Cargo.toml");
    if let Some(v) = read_cargo_package_version(&cargo) {
        return Some(v);
    }

    // 5. launcher/src-tauri/tauri.conf.json → version
    let tauri_conf = install_path
        .join("launcher")
        .join("src-tauri")
        .join("tauri.conf.json");
    if let Some(v) = read_json_string_field(&tauri_conf, "version") {
        return Some(v);
    }

    None
}

/// Helper: read a top-level string field from a JSON file. Returns
/// `Some(value)` only if the file exists, parses as JSON, has the key,
/// and the value is a non-empty string.
fn read_json_string_field(path: &Path, key: &str) -> Option<String> {
    let txt = fs::read_to_string(path).ok()?;
    let val: Value = serde_json::from_str(&txt).ok()?;
    let s = val.get(key)?.as_str()?;
    if s.is_empty() {
        None
    } else {
        Some(s.to_string())
    }
}

/// Helper: parse the first `version = "…"` line within the `[package]`
/// block of a Cargo.toml. We don't pull in a full TOML parser — the
/// shape is fixed and a one-pass scan handles every legitimate Cargo.toml
/// the launcher ships with. Comment lines (`#`) are ignored. Returns
/// None on missing file, missing `[package]` block, or missing version
/// line within the block.
fn read_cargo_package_version(path: &Path) -> Option<String> {
    let txt = fs::read_to_string(path).ok()?;
    let mut in_pkg = false;
    for raw_line in txt.lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // Table header changes scope.
        if line.starts_with('[') && line.ends_with(']') {
            in_pkg = line == "[package]";
            continue;
        }
        if !in_pkg {
            continue;
        }
        if let Some(rest) = line.strip_prefix("version") {
            // Match `version = "0.2.7"` or `version="0.2.7"`.
            let rest = rest.trim_start();
            let rest = rest.strip_prefix('=')?.trim();
            // Strip surrounding quotes (single or double).
            let v = rest
                .trim_start_matches('"')
                .trim_end_matches('"')
                .trim_start_matches('\'')
                .trim_end_matches('\'')
                .trim();
            if !v.is_empty() {
                return Some(v.to_string());
            }
        }
    }
    None
}




// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn tmp() -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-manifest-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    // -------- read_version_from_install_files priority order --------

    #[test]
    fn version_prio1_install_manifest_wins() {
        let p = tmp();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(
            p.join("state").join("install-manifest.json"),
            r#"{"version":"0.9.0"}"#,
        )
        .unwrap();
        // Also write a vct-module.json with a different version — manifest
        // must win.
        fs::write(p.join("vct-module.json"), r#"{"version":"0.1.0"}"#).unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("0.9.0".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_prio2_vct_module_when_no_manifest() {
        let p = tmp();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.7"}"#).unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("0.2.7".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_prio3_package_json_when_no_module() {
        let p = tmp();
        fs::create_dir_all(p.join("launcher")).unwrap();
        fs::write(
            p.join("launcher").join("package.json"),
            r#"{"name":"x","version":"1.2.3"}"#,
        )
        .unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("1.2.3".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_prio4_cargo_toml_when_no_pkgjson() {
        let p = tmp();
        let tauri_dir = p.join("launcher").join("src-tauri");
        fs::create_dir_all(&tauri_dir).unwrap();
        fs::write(
            tauri_dir.join("Cargo.toml"),
            "# top comment\n[package]\nname = \"vco\"\nversion = \"4.5.6\"\nedition = \"2021\"\n",
        )
        .unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("4.5.6".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_prio4_cargo_toml_ignores_dep_version_lines() {
        // Make sure we don't pick up `version = "1.2.3"` inside
        // `[dependencies.foo]` — the in_pkg flag must isolate scope.
        let p = tmp();
        let tauri_dir = p.join("launcher").join("src-tauri");
        fs::create_dir_all(&tauri_dir).unwrap();
        fs::write(
            tauri_dir.join("Cargo.toml"),
            "[dependencies.foo]\nversion = \"9.9.9\"\n\n[package]\nname = \"vco\"\nversion = \"4.5.6\"\n",
        )
        .unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("4.5.6".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_prio5_tauri_conf_last_resort() {
        let p = tmp();
        let tauri_dir = p.join("launcher").join("src-tauri");
        fs::create_dir_all(&tauri_dir).unwrap();
        fs::write(
            tauri_dir.join("tauri.conf.json"),
            r#"{"version":"7.8.9"}"#,
        )
        .unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("7.8.9".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_returns_none_when_all_missing() {
        let p = tmp();
        assert_eq!(read_version_from_install_files(&p), None);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_malformed_json_falls_through() {
        let p = tmp();
        fs::create_dir_all(p.join("state")).unwrap();
        // Malformed manifest — should fall through to vct-module.json.
        fs::write(
            p.join("state").join("install-manifest.json"),
            "{not json at all",
        )
        .unwrap();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.7"}"#).unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("0.2.7".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn version_empty_string_falls_through() {
        // An empty string is treated as "no version recorded" so we keep
        // walking the priority chain instead of returning "".
        let p = tmp();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(
            p.join("state").join("install-manifest.json"),
            r#"{"version":""}"#,
        )
        .unwrap();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.7"}"#).unwrap();
        assert_eq!(
            read_version_from_install_files(&p),
            Some("0.2.7".to_string())
        );
        fs::remove_dir_all(&p).ok();
    }

    // -------- refresh_install_manifest --------



}
