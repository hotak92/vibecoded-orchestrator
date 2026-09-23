//! Unregister-flow launcher-file purge.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the launcher-artifact filesystem
//! purge (`purge_launcher_files_from_project`) that previously lived inline in
//! `projects_v2.rs`. The caller-supplied-key-set NAME strippers that also
//! lived here (`strip_named_keys_from_{env_text,claude_env_text,env_object}`)
//! were retired in v0.2.97: unregister removes a secret value only on value
//! evidence (`projects_v2::strip_proven_secret_values`), never by name. The
//! facade re-exports every symbol. `UNREGISTER_PURGE_PATHS` stays in the
//! facade (shared with the
//! unregister command surface) and is pulled in via `super::`.
//!
//! v0.2.97 (review R4 F25): the unregister's step 1 — env strip, stop
//! decision, file purge — lives here as `unregister_purge_folder`, so the
//! order (never purge files, forget secret rows or delete the row over a
//! proven value that is still on disk) is testable without a Tauri state.

use std::path::Path;

use super::{UnregisterReport, UNREGISTER_PURGE_PATHS};

/// Surgically remove every entry in `UNREGISTER_PURGE_PATHS` from
/// `<folder>/`. Returns `(relative_paths_removed, warnings)`.
///
/// Soft-fail discipline: per-path failures (permission denied, ENOENT
/// race, etc.) land in `warnings`; the next path is still attempted.
/// ENOENT is silent — a missing path on a folder that never had the
/// bundle installed is the expected case for legacy projects, not a
/// warning condition.
///
/// Note: this is the FILE / DIRECTORY purge. The env-surface strip
/// runs separately via `surgically_strip_env_surfaces` so that surfaces
/// containing user-added keys can be partially preserved.
pub(crate) fn purge_launcher_files_from_project(
    folder: &Path,
) -> (Vec<String>, Vec<String>) {
    let mut purged: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();

    for rel in UNREGISTER_PURGE_PATHS {
        let target = folder.join(rel);
        if !target.exists() {
            continue; // silent skip
        }
        let meta = match std::fs::symlink_metadata(&target) {
            Ok(m) => m,
            Err(e) => {
                warnings.push(format!(
                    "could not stat {} for unregister purge: {}",
                    target.display(), e
                ));
                continue;
            }
        };

        let result = if meta.is_dir() {
            std::fs::remove_dir_all(&target)
        } else {
            std::fs::remove_file(&target)
        };

        match result {
            Ok(()) => purged.push((*rel).to_string()),
            Err(e) => warnings.push(format!(
                "could not remove {}: {}", target.display(), e
            )),
        }
    }

    (purged, warnings)
}


/// The unregister's stop decision after the env strip (review R4 F25): `Ok`
/// when every proven secret value was removed; otherwise `Err` with the
/// message the GUI shows. Once the project's secret rows are forgotten and
/// its row deleted, a stored value can never be looked up again, so a value
/// VCO wrote but could not remove would be stranded for good — stopping
/// BEFORE the file purge, the forgotten rows and the DB delete keeps it
/// removable, and a retry after fixing the cause completes the unregister
/// (every step is idempotent).
pub(crate) fn unregister_may_continue(not_removed: &[String], warnings: &[String]) -> Result<(), String> {
    if not_removed.is_empty() {
        return Ok(());
    }
    Err(format!(
        "Unregister stopped — the project is still registered (its routing keys were \
         already stripped; any env refresh restores them). These values VCO wrote \
         could not be removed: {}. {} Fix the cause (a read-only folder, a full disk, \
         a file another program holds) and unregister again; stopping keeps them \
         removable, because once the project is gone VCO can no longer check them.",
        not_removed.join("; "),
        warnings.join(" ")
    ))
}

/// Step 1 of `delete_project_v2` for an existing folder: `strip` removes
/// the launcher's env keys and proven secret values (returning `(keys,
/// warnings, not_removed)` — production passes
/// `surgically_strip_env_surfaces_checked`), then the launcher files are
/// purged. `Err` — the command's return — when a proven value could not be
/// removed: nothing is purged and `report` is untouched, so the caller stops
/// before forgetting secret rows or deleting the project row.
pub(crate) fn unregister_purge_folder(
    folder: &Path,
    strip: impl FnOnce(&Path) -> (Vec<String>, Vec<String>, Vec<String>),
    report: &mut UnregisterReport,
) -> Result<(), String> {
    let (keys, env_warnings, not_removed) = strip(folder);
    unregister_may_continue(&not_removed, &env_warnings)?;
    for k in keys {
        if !report.keys_purged_from_env.contains(&k) {
            report.keys_purged_from_env.push(k);
        }
    }
    report.warnings.extend(env_warnings);
    let (files, file_warnings) = purge_launcher_files_from_project(folder);
    report.files_purged = files;
    report.warnings.extend(file_warnings);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder_with_a_manifest() -> (tempfile::TempDir, std::path::PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let manifest = tmp.path().join(".claude/.vco-manifest.json");
        std::fs::create_dir_all(manifest.parent().unwrap()).unwrap();
        std::fs::write(&manifest, "{}").unwrap();
        (tmp, manifest)
    }

    /// Act: every proven value removed ⇒ the keys are reported and the
    /// launcher files purged.
    #[test]
    fn the_unregister_purges_once_every_proven_value_is_gone() {
        let (tmp, manifest) = folder_with_a_manifest();
        let mut report = UnregisterReport::default();
        let strip = |_: &Path| (vec!["KG_COLLECTION".to_string()], vec!["w".to_string()], Vec::new());
        unregister_purge_folder(tmp.path(), strip, &mut report).unwrap();
        assert!(!manifest.exists());
        assert_eq!(report.files_purged, vec![".claude/.vco-manifest.json".to_string()]);
        assert_eq!(report.keys_purged_from_env, vec!["KG_COLLECTION".to_string()]);
        assert_eq!(report.warnings, vec!["w".to_string()]);
    }

    /// Leave alone (review R4 F25): a proven value still on disk stops the
    /// unregister BEFORE the file purge, naming key and file (never a value).
    #[test]
    fn the_unregister_stops_on_a_proven_value_it_could_not_remove() {
        let (tmp, manifest) = folder_with_a_manifest();
        let reply = serde_json::json!({
            "removed": {".env": ["A"]},
            "not_removed": {".claude/env": ["OPENAI_API_KEY"]},
            "errors": ["could not rewrite /p/.claude/env: Permission denied"]
        });
        let not_removed = super::super::not_removed_secret_values(&reply);
        assert_eq!(not_removed, vec!["OPENAI_API_KEY in .claude/env".to_string()]);
        let mut report = UnregisterReport::default();
        let strip = |_: &Path| {
            (vec!["A".to_string()], vec!["could not rewrite /p/.claude/env".to_string()], not_removed)
        };
        let msg = unregister_purge_folder(tmp.path(), strip, &mut report)
            .expect_err("a stranded proven value must stop the unregister");
        assert!(manifest.exists(), "no file is purged before the stop");
        assert!(report.files_purged.is_empty() && report.keys_purged_from_env.is_empty());
        assert!(msg.starts_with("Unregister stopped — the project is still registered"), "{}", msg);
        assert!(msg.contains("OPENAI_API_KEY in .claude/env") && msg.contains("could not rewrite"), "{}", msg);
    }
}
