// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// installed_modules — shared helper that enumerates on-disk
// `vct-module.json` paths for INSTALLED modules.
//
// Replaces the v0.2.32-era `catalog_scan_paths` (in `modules.rs`) and
// `manifest_scan_paths` (copy-pasted in `module_gui.rs`). v0.2.33's
// architecture review §4 flagged the duplication and the conflation
// of post-install discovery with the dev-affordance
// `<install_root>/paid-modules/` scan.
//
// This file ships TWO functions with clearly-separate roles:
//
//   * `installed_module_manifest_paths(db)` — the ONLY surface that
//     post-install consumers should call. Walks
//     `<VCT_ROOT>/modules/*/vct-module.json` (where Agent C's
//     `extract_manifest_from_image` writes the manifest after
//     `container_pull`) plus the launcher's `bundled_manifests/*.json`
//     (the launcher itself + the search MCP — these are NOT paid
//     modules but legitimately ship inside the launcher binary).
//
//   * `dev_paid_modules_paths(db)` — the dev-affordance, gated behind
//     the env var `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1`. Walks
//     `<install_root>/paid-modules/*/vct-module.json`. Only useful for
//     module-author workflows (RL chat developing v0.2.8 locally
//     before publishing the GHCR image). Real-user installs don't
//     have this directory, so the env-var gate is the right knob:
//     unset by default, set by the dev when they want to work
//     against the co-located manifest.
//
// The new `list_module_catalog_impl` in `modules.rs` calls
// `installed_module_manifest_paths` to know what's INSTALLED, then
// adds the L0-fetched entries on top. The OLD code mixed these two
// concerns; this split is the v0.2.33 cleanup.

use std::path::PathBuf;

use crate::db::Db;

/// Env-var that opts a dev into the `<install_root>/paid-modules/`
/// catalog passthrough. Set to `"1"` (or any non-empty value) to
/// include those manifests in the catalog. Unset = production
/// behaviour (L0 + on-disk-installed only).
pub const DEV_CATALOG_PASSTHROUGH_ENV: &str = "VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH";

/// Walk every directory that contains a post-install `vct-module.json`
/// the launcher considers "installed".
///
/// Sources (in priority order):
///   1. `<VCT_ROOT>/modules/<id>/vct-module.json` — where Agent C's
///      `extract_manifest_from_image` writes the manifest after a
///      successful `container_pull`. Source-of-truth for the FULL
///      manifest post-install.
///   2. `<VCT_ROOT>/bundled_manifests/*.json` — manifests that ship
///      inside the launcher binary (e.g. the search MCP). NOT paid
///      modules; the launcher controls these.
///
/// Does NOT walk `<install_root>/paid-modules/` — that's a dev
/// affordance, see `dev_paid_modules_paths`. Production catalog
/// metadata comes from L0 (Agent A's `module_catalog_client`).
pub fn installed_module_manifest_paths(_db: &Db) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let vct_root = crate::paths::vct_root_dir();

    // Source 1: post-install extracted manifests.
    let modules = vct_root.join("modules");
    if modules.is_dir() {
        if let Ok(entries) = std::fs::read_dir(&modules) {
            for e in entries.flatten() {
                let p = e.path().join("vct-module.json");
                if p.is_file() {
                    paths.push(p);
                }
            }
        }
    }

    // Source 2: launcher-bundled manifests (search MCP, etc).
    let bundled = vct_root.join("bundled_manifests");
    if bundled.is_dir() {
        if let Ok(entries) = std::fs::read_dir(&bundled) {
            for e in entries.flatten() {
                let p = e.path();
                if p.extension().and_then(|s| s.to_str()) == Some("json") {
                    paths.push(p);
                }
            }
        }
    }

    paths
}

/// Walk `<install_root>/paid-modules/*/vct-module.json` — the
/// dev-affordance path. Returns an empty Vec when the env var
/// `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH` is NOT set, OR when no
/// `paid-modules/` directory exists at the resolved install root.
///
/// This is intentionally separate from `installed_module_manifest_paths`
/// so production callers (the dispatcher reading post-install
/// manifests, the reconciler verifying on-disk artifacts) NEVER see
/// the dev manifests by accident.
///
/// The launcher's catalog builder (Agent B's
/// `list_module_catalog_impl`) calls this AFTER it's built the L0-
/// driven catalog, and merges the dev manifests on top so a developer
/// working on v0.2.8 locally sees their in-progress manifest in the
/// catalog tile before publishing to GHCR.
pub fn dev_paid_modules_paths(db: &Db) -> Vec<PathBuf> {
    if !dev_catalog_passthrough_enabled() {
        return Vec::new();
    }
    let Some(install_root) = crate::commands::installer::resolve_install_root_sync(db) else {
        return Vec::new();
    };
    let paid = install_root.join("paid-modules");
    if !paid.is_dir() {
        return Vec::new();
    }
    let mut paths = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&paid) {
        for module_dir in entries.flatten() {
            let p = module_dir.path().join("vct-module.json");
            if p.is_file() {
                paths.push(p);
            }
        }
    }
    paths
}

/// Does the `<install_root>/paid-modules/` directory exist on disk?
/// Used by the dev-affordance toast trigger: if this returns true
/// AND `dev_catalog_passthrough_enabled()` returns false, the
/// launcher tells the user "I see your paid-modules dir; set
/// VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH=1 to enable it".
pub fn paid_modules_dir_exists(db: &Db) -> Option<PathBuf> {
    let install_root = crate::commands::installer::resolve_install_root_sync(db)?;
    let paid = install_root.join("paid-modules");
    if paid.is_dir() {
        Some(paid)
    } else {
        None
    }
}

/// Is `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH` set to a non-empty
/// non-`"0"` value?
pub fn dev_catalog_passthrough_enabled() -> bool {
    match std::env::var(DEV_CATALOG_PASSTHROUGH_ENV) {
        Ok(v) => !v.is_empty() && v != "0",
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dev_catalog_passthrough_disabled_when_env_var_unset() {
        let saved = std::env::var(DEV_CATALOG_PASSTHROUGH_ENV).ok();
        std::env::remove_var(DEV_CATALOG_PASSTHROUGH_ENV);
        assert!(!dev_catalog_passthrough_enabled());
        if let Some(v) = saved {
            std::env::set_var(DEV_CATALOG_PASSTHROUGH_ENV, v);
        }
    }

    #[test]
    fn dev_catalog_passthrough_enabled_when_env_var_truthy() {
        let saved = std::env::var(DEV_CATALOG_PASSTHROUGH_ENV).ok();
        std::env::set_var(DEV_CATALOG_PASSTHROUGH_ENV, "1");
        assert!(dev_catalog_passthrough_enabled());
        // "0" is treated as off so devs can `export VAR=0` to mean
        // "remember I tried this, but don't enable it right now".
        std::env::set_var(DEV_CATALOG_PASSTHROUGH_ENV, "0");
        assert!(!dev_catalog_passthrough_enabled());
        std::env::set_var(DEV_CATALOG_PASSTHROUGH_ENV, "yes-please");
        assert!(dev_catalog_passthrough_enabled());
        match saved {
            Some(v) => std::env::set_var(DEV_CATALOG_PASSTHROUGH_ENV, v),
            None => std::env::remove_var(DEV_CATALOG_PASSTHROUGH_ENV),
        }
    }
}
