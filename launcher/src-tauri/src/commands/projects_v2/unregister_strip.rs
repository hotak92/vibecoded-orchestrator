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

use std::path::Path;

use super::UNREGISTER_PURGE_PATHS;

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

