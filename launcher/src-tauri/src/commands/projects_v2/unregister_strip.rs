//! Unregister-flow env-key strippers + launcher-file purge.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the caller-supplied-key-set
//! strippers (`strip_named_keys_from_env_text`,
//! `strip_named_keys_from_claude_env_text`, `strip_named_keys_from_env_object`)
//! and the launcher-artifact filesystem purge
//! (`purge_launcher_files_from_project`) that previously lived inline in
//! `projects_v2.rs`. Behaviour is unchanged; the facade re-exports every
//! symbol. `UNREGISTER_PURGE_PATHS` stays in the facade (shared with the
//! unregister command surface) and is pulled in via `super::`.

use std::path::Path;

use super::UNREGISTER_PURGE_PATHS;

/// Pure helper: strip a named set of KEY names from `.env`-style text.
/// Mirror of `strip_canonical_keys_from_env_text` but with a caller-
/// supplied key set instead of `UNREGISTER_CANONICAL_ENV_KEYS`.
pub(crate) fn strip_named_keys_from_env_text(
    text: &str,
    keys: &std::collections::HashSet<&str>,
) -> (String, Vec<String>) {
    let mut removed = std::collections::BTreeSet::new();
    let mut out = String::with_capacity(text.len());
    for line in text.lines() {
        let trimmed = line.trim_start();
        let body = if let Some(rest) = trimmed.strip_prefix('#') {
            rest.trim_start()
        } else {
            trimmed
        };
        let key_to_check = body
            .find('=')
            .filter(|&i| i > 0)
            .map(|i| body[..i].trim());
        if let Some(k) = key_to_check {
            if keys.contains(k) {
                removed.insert(k.to_string());
                continue;
            }
        }
        out.push_str(line);
        out.push('\n');
    }
    if !text.ends_with('\n') && out.ends_with('\n') {
        out.pop();
    }
    if text.ends_with('\n') && out.is_empty() {
        out.push('\n');
    }
    (out, removed.into_iter().collect())
}

/// Pure helper: strip a named set of KEY names from `.claude/env`
/// POSIX-export text. Mirror of `strip_canonical_keys_from_claude_env_text`.
pub(crate) fn strip_named_keys_from_claude_env_text(
    text: &str,
    keys: &std::collections::HashSet<&str>,
) -> (String, Vec<String>) {
    let mut removed = std::collections::BTreeSet::new();
    let mut out = String::with_capacity(text.len());
    for line in text.lines() {
        let trimmed = line.trim_start();
        let after_hash = if let Some(rest) = trimmed.strip_prefix('#') {
            rest.trim_start()
        } else {
            trimmed
        };
        let body = after_hash.strip_prefix("export ").unwrap_or(after_hash);
        let key_to_check = body
            .find('=')
            .filter(|&i| i > 0)
            .map(|i| body[..i].trim());
        if let Some(k) = key_to_check {
            if keys.contains(k) {
                removed.insert(k.to_string());
                continue;
            }
        }
        out.push_str(line);
        out.push('\n');
    }
    if !text.ends_with('\n') && out.ends_with('\n') {
        out.pop();
    }
    if text.ends_with('\n') && out.is_empty() {
        out.push('\n');
    }
    (out, removed.into_iter().collect())
}

/// Pure helper: strip a named set of KEY names from a JSON env-shaped
/// sub-block. Mirror of `strip_canonical_keys_from_env_object`.
pub(crate) fn strip_named_keys_from_env_object(
    parent: &mut serde_json::Map<String, serde_json::Value>,
    env_key: &str,
    keys: &std::collections::HashSet<&str>,
) -> Vec<String> {
    let env_obj = match parent.get_mut(env_key).and_then(|v| v.as_object_mut()) {
        Some(o) => o,
        None => return Vec::new(),
    };
    let mut removed = std::collections::BTreeSet::new();
    let to_remove: Vec<String> = env_obj
        .keys()
        .filter(|k| keys.contains(k.as_str()))
        .cloned()
        .collect();
    for k in to_remove {
        env_obj.remove(&k);
        removed.insert(k);
    }
    removed.into_iter().collect()
}

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

