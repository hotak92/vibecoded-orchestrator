// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The launcher's bundled core-module manifests (`launcher/bundled_manifests/`),
//! embedded in the binary and materialized into `<vct_root>/bundled_manifests/`.
//!
//! ## Why (v0.2.97)
//!
//! `docs/features/01-launcher.md` and the directory's README said these
//! manifests "ship with the launcher binary and are copied to
//! `~/.vct/bundled_manifests/` on first launch", and every reader — the hub's
//! `/env` and catalog scan, `installed_module_manifest_paths`, the lifecycle
//! API — reads that directory. Nothing ever wrote it (known empty since
//! 2026-04-26, when the catalog grew built-in cards instead), and half the
//! files did not even parse, so a bundled module's settings never reached a
//! project. This module is the one load path: the files are embedded at
//! compile time (the binary a user installed carries exactly the manifests it
//! was built with — no clone discovery) and [`sync_bundled_manifests`] writes
//! them into the state directory on every hub/launcher start, so an update
//! refreshes them.

use std::path::{Path, PathBuf};

/// Every file of `launcher/bundled_manifests/*.json`, embedded. A test pins
/// this list against the directory, so a manifest added there cannot be
/// forgotten here.
pub const BUNDLED_MANIFESTS: &[(&str, &str)] = &[
    ("vct-code-embedding.json", include_str!("../../../bundled_manifests/vct-code-embedding.json")),
    ("vct-codegraph.json", include_str!("../../../bundled_manifests/vct-codegraph.json")),
    ("vct-hub-api.json", include_str!("../../../bundled_manifests/vct-hub-api.json")),
    ("vct-kg.json", include_str!("../../../bundled_manifests/vct-kg.json")),
    ("vct-search.json", include_str!("../../../bundled_manifests/vct-search.json")),
    ("vct-session-state.json", include_str!("../../../bundled_manifests/vct-session-state.json")),
];

/// `<vct_root>/bundled_manifests` — where every reader looks.
pub fn bundled_manifests_dir(vct_root: &Path) -> PathBuf {
    vct_root.join("bundled_manifests")
}

/// Write the embedded manifests into `<vct_root>/bundled_manifests/`, each
/// only when its bytes differ (atomic temp + rename). Returns the file names
/// written. A file there that this binary does not ship is left alone.
pub fn sync_bundled_manifests(vct_root: &Path) -> Result<Vec<String>, String> {
    let dir = bundled_manifests_dir(vct_root);
    std::fs::create_dir_all(&dir).map_err(|e| format!("create {}: {}", dir.display(), e))?;
    let mut written = Vec::new();
    for (name, body) in BUNDLED_MANIFESTS {
        let target = dir.join(name);
        if std::fs::read_to_string(&target).ok().as_deref() == Some(*body) {
            continue;
        }
        let tmp = dir.join(format!(".{}.tmp.{}", name, std::process::id()));
        std::fs::write(&tmp, body).map_err(|e| format!("write {}: {}", tmp.display(), e))?;
        std::fs::rename(&tmp, &target).map_err(|e| {
            let _ = std::fs::remove_file(&tmp);
            format!("rename into {}: {}", target.display(), e)
        })?;
        written.push((*name).to_string());
    }
    Ok(written)
}

/// Whether `manifest_path` is one of the bundled core manifests (the hub treats
/// those as installed for every project — the catalog's "bundled" kind).
pub fn is_bundled_manifest_path(vct_root: &Path, manifest_path: &Path) -> bool {
    manifest_path.parent() == Some(bundled_manifests_dir(vct_root).as_path())
        && manifest_path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| BUNDLED_MANIFESTS.iter().any(|(name, _)| *name == n))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("bundled_manifests")
    }

    /// The embedded list IS the directory — no manifest added there is left
    /// out here (and none listed here is missing there).
    #[test]
    fn the_embedded_list_matches_the_bundled_directory() {
        let mut on_disk: Vec<String> = std::fs::read_dir(source_dir())
            .unwrap()
            .flatten()
            .filter_map(|e| e.file_name().to_str().map(str::to_string))
            .filter(|n| n.ends_with(".json"))
            .collect();
        on_disk.sort();
        let embedded: Vec<String> = BUNDLED_MANIFESTS.iter().map(|(n, _)| n.to_string()).collect();
        assert_eq!(on_disk, embedded);
    }

    /// Every bundled manifest parses with the real parser — three did not
    /// (a half `mcp_registration` block without `mcp_name`), which made the
    /// hub drop them silently.
    #[test]
    fn every_bundled_manifest_parses_strictly() {
        let _strict = crate::test_env::env_guard(&[("VCT_LAUNCHER_STRICT_MANIFEST", Some("1"))]);
        for (name, body) in BUNDLED_MANIFESTS {
            crate::manifest::ModuleManifest::from_json(body)
                .unwrap_or_else(|e| panic!("{} does not parse: {}", name, e));
        }
    }

    #[test]
    fn sync_writes_every_manifest_once_and_is_idempotent() {
        let tmp = tempfile::tempdir().unwrap();
        let first = sync_bundled_manifests(tmp.path()).unwrap();
        assert_eq!(first.len(), BUNDLED_MANIFESTS.len());
        for (name, body) in BUNDLED_MANIFESTS {
            let p = bundled_manifests_dir(tmp.path()).join(name);
            assert_eq!(std::fs::read_to_string(&p).unwrap(), *body);
            assert!(is_bundled_manifest_path(tmp.path(), &p));
        }
        assert!(sync_bundled_manifests(tmp.path()).unwrap().is_empty(), "nothing rewritten");
        // A stale copy is refreshed; a foreign file is left alone.
        let stale = bundled_manifests_dir(tmp.path()).join("vct-kg.json");
        std::fs::write(&stale, "{}").unwrap();
        let foreign = bundled_manifests_dir(tmp.path()).join("user-extra.json");
        std::fs::write(&foreign, "{}").unwrap();
        assert_eq!(sync_bundled_manifests(tmp.path()).unwrap(), vec!["vct-kg.json".to_string()]);
        assert_eq!(std::fs::read_to_string(&foreign).unwrap(), "{}");
        assert!(!is_bundled_manifest_path(tmp.path(), &foreign));
    }
}
