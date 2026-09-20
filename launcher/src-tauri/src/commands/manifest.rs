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
//!   - `force_resync_launcher`, and `apply_launcher_update`'s fallback when
//!     `install.py` cannot run — refresh after the cargo+npm rebuild, before
//!     respawn.
//!
//! **CORRECTED v0.2.95 (WP-1).** This list used to end "the manifest now
//! reflects the version the new binary is about to run as", and priority 1
//! above used to say `version` is "refreshed by `refresh_install_manifest`…
//! from the Rust update paths". Both described the defect. NONE of the paths
//! above runs `install.py`, so none of them installs anything beyond the
//! launcher binary — venv, hooks, `templates/**`, MCP registrations, the KG
//! seed and the schema all stay where they were. Writing the new version there
//! made the completion marker attest work nothing had done, and made the
//! resulting half-install invisible (prior review H1/H2/H3).
//!
//! So: **`install.py` is the only writer of `version`.** Priority 1 still holds
//! — the manifest remains the authoritative answer to "what version is
//! installed here", and it is authoritative precisely BECAUSE it now names the
//! last version something actually installed. `refresh_install_manifest`
//! advances `source_commit` / `source_branch` / `install_method` and sets
//! `post_source_only`; see its own docs for how those turn the half-state into
//! a visible, repairable one.
//!
//! Soft-fail contract: a manifest-write failure must NOT block the
//! original action (update / launcher rebuild). The manifest is
//! diagnostic; the install is real either way. All errors return
//! `Result<(), String>` so the caller can log + continue.
//!
//! `installed_at` is preserved across refreshes — the field means "first
//! ever successful install at this path", not "most recent install run".
//! `completed_at` carries the latter, and for the same reason as `version` it
//! is written ONLY by `install.py`: a run that installed nothing completed no
//! install.
//!
//! ## Known shape: `installed: true` with NO `version` (v0.2.95 ship-gate MINOR-9)
//!
//! When the FIRST manifest this path ever gets is written here rather than by
//! install.py (`refresh_creates_manifest_when_missing` — no prior file, so
//! nothing to carry `version` from), the result is `{installed: true, …}` with
//! `version` absent. That combination is deliberate on both legs and each leg
//! is load-bearing somewhere else:
//!
//!   * `installed: true` — `check_install_status` reads a MISSING `installed`
//!     as "install in progress / aborted" and refuses to promote the path, and
//!     `doctor.probe_install_completeness` answers `unknown` rather than
//!     `problem`. Omitting it here would disarm the very probe the
//!     `post_source_only` flag exists to feed.
//!   * no `version` — fabricating one from the source files is the WP-1 defect
//!     in miniature: it would attest an install nothing performed.
//!
//! The consequence is worth naming because it is easy to misread as a bug:
//! such a root reads as *installed at an unknown version* everywhere — and
//! `read_version_from_install_files` then falls through to `vct-module.json`
//! (priority 2) exactly as it does for any pre-manifest install, so
//! `check_for_updates` leaves `install_stale` OFF rather than reporting a
//! drift it cannot substantiate. Nothing here is silently wrong; the record is
//! simply less specific than one install.py wrote. Do not "fix" it by
//! inventing a version.

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
/// update paths. Preserves `installed_at`; refreshes `source_commit`,
/// `source_branch`, `install_method`. Other prior fields are passed
/// through unchanged so we never regress sysinfo-derived fields we
/// don't have access to from Rust (e.g. cpu_only flags from install.py
/// CLI args).
///
/// Soft-fail contract: this returns Err on IO/JSON problems but the
/// caller is expected to log + continue, never propagate as a failure
/// to the user-initiated action (update / launcher rebuild).
///
/// `install_method` describes which path triggered the refresh:
///   - "launcher_update"          — force_resync_launcher, and
///     apply_launcher_update's fallback when install.py cannot run
///   - "orchestrator_update"      — update_orchestrator_at
///
/// # v0.2.95 WP-1: this function does NOT write `version`, and that is the point
///
/// **Every path that reaches here is a path that did NOT run `install.py`.**
/// That is not an assumption about the future — it is what the two
/// `install_method` values above ARE, and `vco_lib/doctor.py` independently
/// declares the same pair as `RUST_INSTALL_METHODS`, "install_method values only
/// the RUST writer produces … seeing one of these means the LAST hand on the
/// marker was a path that never ran install.py".
///
/// Until v0.2.95 this function nonetheless re-read `version` FRESH from the
/// files the pull had just landed and stamped `completed_at: now` beside
/// `installed: true`. So a launcher-only update — new source, OLD venv, OLD
/// hooks, OLD `templates/**`, OLD MCP registrations, OLD KG seed, OLD schema —
/// wrote a completion marker attesting a full install at the NEW version. The
/// half-updated state was durable, and worse, INVISIBLE: after the pull the
/// badge's commits-behind count is zero, so the one surface that would have
/// repaired it stopped offering itself (prior review H1/H2/H3, §4.1).
///
/// Now `version` is left at whatever the last real installer run recorded, and
/// **install.py is the only writer of it**. Two things follow, and they are the
/// whole repair:
///
/// * `check_for_updates` computes `install_stale = source_version !=
///   installed_version` from `vct-module.json` vs THIS field. Freezing it makes
///   that comparison true the moment a source-only path moves the tree — so the
///   badge lights up and offers `apply_pending_install`, which is exactly
///   `install.py --update` without a redundant pull. The repair affordance
///   appears by itself.
/// * `source_commit` IS advanced, deliberately. It is the leg
///   `doctor.probe_install_completeness` convicts on when the version strings
///   happen to agree: it compares this record against `.claude/.vco-manifest.json`,
///   which ONLY the bundle engine writes. Freezing the commit too would leave
///   both records agreeing and make the half-state unprovable again.
///
/// `post_source_only` is written so the state is named rather than inferred
/// from "version and source_commit disagree", and so the case the version
/// comparison CANNOT see is still caught: a source-only advance between two
/// commits carrying the same version string (every mid-cycle commit on a
/// release branch) leaves `source_version == installed_version` and would
/// otherwise be silent. `check_for_updates` reads it as a second, independent
/// trigger for `install_stale`.
///
/// Nothing needs to clear the flag: `install.py::_write_install_manifest`
/// rebuilds the manifest from a literal dict, carrying over only the specific
/// fields it names (`installed_at`, `container_runtime`, the GPU triple), so a
/// real installer run drops it automatically. That is a property worth not
/// breaking — if this writer ever gains a field install.py should own, the same
/// check applies.
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
        .or_insert_with(|| Value::String(now));
    obj.entry("schema_version")
        .or_insert_with(|| Value::Number(1u64.into()));
    obj.insert("installed".to_string(), Value::Bool(true));
    obj.insert(
        "install_path".to_string(),
        Value::String(install_path.display().to_string()),
    );
    obj.insert("install_method".to_string(), Value::String(install_method.to_string()));

    // v0.2.95 WP-1: `version` and `completed_at` are NOT touched here. See the
    // section on this function for the full argument; the short form is that
    // both attest an installer run, no caller of this function performs one,
    // and stamping them anyway is what made the half-updated state both
    // durable and invisible. `completed_at` keeps the last installer run's
    // timestamp, which is also what `doctor.probe_install_completeness`'s
    // acquittal leg compares the `session ok` log row against.
    obj.insert("post_source_only".to_string(), Value::Bool(true));

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

    /// v0.2.95 WP-1 — the core of the fix, stated as the state it prevents.
    ///
    /// A launcher-only path pulled new source over an install completed at
    /// 0.1.6. Pre-WP-1 this function then wrote `version: "0.2.8"` (re-read
    /// from the files the pull had just landed) with `completed_at: now`
    /// beside `installed: true`, and the install-completion marker attested
    /// work that nothing had done.
    #[test]
    fn refresh_does_not_advance_version_or_completed_at_on_a_path_that_skipped_install_py() {
        let p = tmp();
        let state = p.join("state");
        fs::create_dir_all(&state).unwrap();
        // Prior manifest: a REAL install.py run finished at 0.1.6.
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
        // The pull landed 0.2.8's source files.
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.8"}"#).unwrap();

        refresh_install_manifest(&p, "orchestrator_update").unwrap();

        let txt = fs::read_to_string(state.join("install-manifest.json")).unwrap();
        let v: serde_json::Value = serde_json::from_str(&txt).unwrap();

        // THE assertion: `version` still names the last version anything
        // actually installed. install.py is its only writer.
        assert_eq!(
            v.get("version").and_then(|s| s.as_str()),
            Some("0.1.6"),
            "a path that did not run install.py must not claim the new version — \
             freezing this is what makes `install_stale` true and puts the repair \
             back in front of the user"
        );
        // …and `completed_at` still names when that run completed, which is what
        // `doctor.probe_install_completeness` compares the install log against.
        assert_eq!(
            v.get("completed_at").and_then(|s| s.as_str()),
            Some("2026-05-06T10:01:00Z"),
            "completed_at attests a completed installer run; none happened here"
        );
        // The state is NAMED, not left to be inferred from two disagreeing fields.
        assert_eq!(v.get("post_source_only").and_then(|b| b.as_bool()), Some(true));
        // `source_commit` is still advanced when there is one to read — it is the
        // leg the doctor probe convicts on when the version strings agree. (No
        // `.git` in this fixture, so only the flag carries it here; the
        // advance itself is unchanged code and covered by `read_git_rev`.)
        assert_eq!(
            v.get("install_method").and_then(|s| s.as_str()),
            Some("orchestrator_update")
        );
        assert_eq!(v.get("installed_at").and_then(|s| s.as_str()), Some("2026-05-06T10:00:00Z"));
        // Preserved field from prior manifest.
        assert_eq!(v.get("cpu_only").and_then(|b| b.as_bool()), Some(false));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn refresh_creates_manifest_when_missing() {
        let p = tmp();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.2.8"}"#).unwrap();
        refresh_install_manifest(&p, "launcher_update").unwrap();
        let txt = fs::read_to_string(p.join("state").join("install-manifest.json")).unwrap();
        let v: serde_json::Value = serde_json::from_str(&txt).unwrap();
        // `installed: true` is PRESERVED behaviour and load-bearing in two
        // places: `check_install_status` treats a manifest whose `installed` is
        // missing as "install in progress / aborted" and refuses to promote the
        // path, and `doctor.probe_install_completeness` returns `unknown`
        // instead of `problem` — i.e. dropping it here would have DISARMED the
        // very probe this change exists to feed.
        assert_eq!(v.get("installed").and_then(|b| b.as_bool()), Some(true));
        // No prior installer run recorded ⇒ no version to carry, and none is
        // fabricated from the source files. `read_version_from_install_files`
        // then falls through to `vct-module.json` exactly as it does for any
        // pre-manifest install, and `check_for_updates` leaves `install_stale`
        // off rather than reporting a version drift it cannot substantiate.
        assert!(
            v.get("version").is_none(),
            "a first write by a non-installer path must not invent a version"
        );
        assert_eq!(v.get("post_source_only").and_then(|b| b.as_bool()), Some(true));
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
        assert_eq!(v.get("installed").and_then(|b| b.as_bool()), Some(true));
        assert_eq!(v.get("post_source_only").and_then(|b| b.as_bool()), Some(true));
        fs::remove_dir_all(&p).ok();
    }
}
