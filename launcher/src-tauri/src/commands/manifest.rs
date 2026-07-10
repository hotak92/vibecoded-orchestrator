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
//!        run, and refreshed by `refresh_install_manifest` below from the
//!        Rust update paths. **Authoritative when present.**
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
//! ## Bug G (v0.2.8): manifest auto-refresh
//!
//! Pre-v0.2.8 the manifest was written ONCE at first install and never
//! refreshed. After three months of `install.py --update` cycles + Rust-
//! side launcher self-update + `update_orchestrator_at`, the manifest's
//! `version` field would still report the version that was installed at
//! month one. Real-world drift seen 2026-05-13: manifest says
//! `version: 0.1.6` (from 2026-05-06) even after running v0.2.7.
//!
//! `refresh_install_manifest` is the shared helper for the Rust-driven
//! refresh paths:
//!
//!   - `update_orchestrator_at` — refresh after file-copy completes.
//!     The new `vct-module.json` is present in the install path post-copy,
//!     so `read_version_from_install_files` returns the new version.
//!   - `apply_launcher_update` — refresh after cargo+npm rebuild, before
//!     respawn. The rebuilt binary's version source files are on disk;
//!     the manifest now reflects the version the new binary is about to
//!     run as.
//!
//! Soft-fail contract: a manifest-write failure must NOT block the
//! original action (update / launcher rebuild). The manifest is
//! diagnostic; the install is real either way. All errors return
//! `Result<(), String>` so the caller can log + continue.
//!
//! `installed_at` is preserved across refreshes — the field means "first
//! ever successful install at this path", not "most recent install run".
//! `completed_at` carries the latter.

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

/// Bug G: refresh `state/install-manifest.json` from the Rust-side
/// update paths. Preserves `installed_at`; bumps `version` (re-read
/// fresh from the install tree), `source_commit`, `source_branch`,
/// `completed_at`, `install_method`. Other prior fields are passed
/// through unchanged so we never regress sysinfo-derived fields we
/// don't have access to from Rust (e.g. cpu_only flags from install.py
/// CLI args).
///
/// Soft-fail contract: this returns Err on IO/JSON problems but the
/// caller is expected to log + continue, never propagate as a failure
/// to the user-initiated action (update / launcher rebuild).
///
/// `install_method` describes which path triggered the refresh:
///   - "launcher_update"          — apply_launcher_update / force_resync
///   - "orchestrator_update"      — update_orchestrator_at
pub(crate) fn refresh_install_manifest(
    install_path: &Path,
    install_method: &str,
) -> Result<(), String> {
    let manifest_path = install_path.join("state").join("install-manifest.json");

    // Read current manifest (or start with an empty JSON object if it's
    // missing / malformed — Bug G doesn't want a malformed-prior to
    // block the refresh).
    let mut current: Value = match fs::read_to_string(&manifest_path) {
        Ok(txt) => serde_json::from_str(&txt).unwrap_or_else(|_| Value::Object(Default::default())),
        Err(_) => Value::Object(Default::default()),
    };
    if !current.is_object() {
        current = Value::Object(Default::default());
    }
    let obj = current
        .as_object_mut()
        .expect("ensured object above — unreachable");

    let now = chrono_iso_z_now();

    // Preserve installed_at, install_path, schema_version, and the set of
    // sysinfo-derived flags (cpu_only / use_gpu / low_resource / skipped /
    // python_*). If a field doesn't exist in prior, we don't fabricate it.
    obj.entry("installed_at")
        .or_insert_with(|| Value::String(now.clone()));
    obj.entry("schema_version")
        .or_insert_with(|| Value::Number(1u64.into()));
    obj.insert("installed".to_string(), Value::Bool(true));
    obj.insert(
        "install_path".to_string(),
        Value::String(install_path.display().to_string()),
    );
    obj.insert("install_method".to_string(), Value::String(install_method.to_string()));
    obj.insert("completed_at".to_string(), Value::String(now));

    // version: re-read fresh from disk every refresh — this is the
    // whole point of Bug G. Pass include_manifest=false because we're
    // ABOUT to overwrite the manifest; consulting it as a source would
    // just re-write the stale value (the bug we're fixing).
    if let Some(v) = read_version_from_install_files_impl(install_path, false) {
        obj.insert("version".to_string(), Value::String(v));
    }

    // source_commit + source_branch: read fresh from .git/ if present.
    let (commit, branch) = read_git_rev(install_path);
    if let Some(c) = commit {
        obj.insert("source_commit".to_string(), Value::String(c));
    }
    if let Some(b) = branch {
        obj.insert("source_branch".to_string(), Value::String(b));
    }

    // Write atomically: tmp file + rename. Avoids a partial-write being
    // observed by a concurrent reader.
    let state_dir = match manifest_path.parent() {
        Some(p) => p,
        None => return Err("manifest path has no parent directory".to_string()),
    };
    fs::create_dir_all(state_dir).map_err(|e| format!("create state/: {}", e))?;

    let tmp = state_dir.join("install-manifest.json.tmp");
    let serialized = serde_json::to_string_pretty(&current)
        .map_err(|e| format!("serialize manifest: {}", e))?;
    fs::write(&tmp, format!("{}\n", serialized))
        .map_err(|e| format!("write tmp manifest: {}", e))?;
    fs::rename(&tmp, &manifest_path)
        .map_err(|e| format!("rename tmp manifest: {}", e))?;

    Ok(())
}

/// Read `(commit, branch)` from `.git/HEAD`. Best-effort — returns
/// `(None, None)` if `.git/` is missing or the HEAD resolution fails.
/// Mirrors the Python `_read_git_rev` semantics.
fn read_git_rev(install_path: &Path) -> (Option<String>, Option<String>) {
    let git_dir = install_path.join(".git");
    if !git_dir.exists() {
        return (None, None);
    }
    let head_path = git_dir.join("HEAD");
    let head_content = match fs::read_to_string(&head_path) {
        Ok(s) => s.trim().to_string(),
        Err(_) => return (None, None),
    };

    if let Some(rest) = head_content.strip_prefix("ref: ") {
        // Symbolic ref: `ref: refs/heads/main`. Branch is the last
        // path component; commit is the SHA the ref resolves to.
        let ref_path = rest.trim();
        let branch = ref_path
            .rsplit('/')
            .next()
            .unwrap_or("")
            .to_string();
        let ref_file = git_dir.join(ref_path);
        let commit = fs::read_to_string(&ref_file)
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        (commit, Some(branch))
    } else {
        // Detached HEAD: HEAD itself contains the SHA.
        if head_content.is_empty() {
            (None, None)
        } else {
            (Some(head_content), Some("detached".to_string()))
        }
    }
}

/// Same ISO8601 UTC `Z` format the rest of the launcher emits.
///
/// v0.2.77 (Part 7c task 5): now delegates to the shared
/// `vct_launcher_core::time::chrono_iso_z_now` (one home). The prior
/// comment noted the helper "lives in installer.rs and we don't want a
/// circular module dependency" — moving it to the leaf `vct-launcher-core`
/// crate dissolves that worry.
fn chrono_iso_z_now() -> String {
    vct_launcher_core::time::chrono_iso_z_now()
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

    #[test]
    fn refresh_preserves_installed_at_and_bumps_version() {
        let p = tmp();
        let state = p.join("state");
        fs::create_dir_all(&state).unwrap();
        // Prior manifest at v0.1.6 (the real-world stale case).
        fs::write(
            state.join("install-manifest.json"),
            r#"{
              "schema_version": 1,
              "installed": true,
              "installed_at": "2026-05-06T10:00:00Z",
              "completed_at": "2026-05-06T10:01:00Z",
              "version": "0.1.6",
              "install_method": "install.py",
              "cpu_only": false
            }"#,
        )
        .unwrap();
        // New version source available — vct-module.json says v0.2.8.
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.8"}"#).unwrap();

        refresh_install_manifest(&p, "orchestrator_update").unwrap();

        let txt = fs::read_to_string(state.join("install-manifest.json")).unwrap();
        let v: serde_json::Value = serde_json::from_str(&txt).unwrap();
        assert_eq!(v.get("installed_at").and_then(|s| s.as_str()), Some("2026-05-06T10:00:00Z"));
        assert_eq!(v.get("version").and_then(|s| s.as_str()), Some("0.2.8"));
        assert_eq!(v.get("install_method").and_then(|s| s.as_str()), Some("orchestrator_update"));
        // Preserved field from prior manifest.
        assert_eq!(v.get("cpu_only").and_then(|b| b.as_bool()), Some(false));
        // completed_at must be different from installed_at — it's the
        // refresh-now timestamp.
        assert_ne!(
            v.get("completed_at").and_then(|s| s.as_str()),
            Some("2026-05-06T10:01:00Z")
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn refresh_creates_manifest_when_missing() {
        let p = tmp();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.8"}"#).unwrap();
        refresh_install_manifest(&p, "launcher_update").unwrap();
        let txt = fs::read_to_string(p.join("state").join("install-manifest.json")).unwrap();
        let v: serde_json::Value = serde_json::from_str(&txt).unwrap();
        assert_eq!(v.get("installed").and_then(|b| b.as_bool()), Some(true));
        assert_eq!(v.get("version").and_then(|s| s.as_str()), Some("0.2.8"));
        // installed_at gets stamped to now when prior was absent.
        assert!(v.get("installed_at").and_then(|s| s.as_str()).is_some());
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn refresh_recovers_from_malformed_prior() {
        let p = tmp();
        let state = p.join("state");
        fs::create_dir_all(&state).unwrap();
        fs::write(
            state.join("install-manifest.json"),
            "{this is not json}",
        )
        .unwrap();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.8"}"#).unwrap();
        // Must not error — Bug G's soft-fail contract requires recovery.
        refresh_install_manifest(&p, "orchestrator_update").unwrap();
        let txt = fs::read_to_string(state.join("install-manifest.json")).unwrap();
        let v: serde_json::Value = serde_json::from_str(&txt).unwrap();
        assert_eq!(v.get("version").and_then(|s| s.as_str()), Some("0.2.8"));
        fs::remove_dir_all(&p).ok();
    }
}
