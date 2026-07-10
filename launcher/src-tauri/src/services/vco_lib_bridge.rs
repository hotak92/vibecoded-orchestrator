// SPDX-License-Identifier: AGPL-3.0-or-later
//! Shared builder for `python -m vco_lib.<module>` subprocess spawns.
//!
//! ## Why this module exists (v0.2.77 Part 7c task 2)
//!
//! The launcher shells out to `python -m vco_lib.<module>` from many
//! command files. The genuinely drift-prone, subtle part of that pattern
//! is NOT the `-m vco_lib.<module>` argv (that is one obvious line per
//! site) — it is the **env sandbox**: `env_clear()` followed by a hand-
//! curated ALLOWLIST of environment keys re-injected one by one so that
//!
//!   * per-launcher quirks (an inherited `KG_COLLECTION` from the
//!     launcher's own `.claude/env`, a stray `PROJECT_NAME`, …) do NOT
//!     leak into the child and disrupt the Python-side resolver, AND
//!   * the handful of keys the child legitimately needs
//!     (`PATH`, `VCT_STATE_DIR`, `VCT_HUB_PORT`, `VCT_HUB_TOKEN`,
//!     `VCT_INSTALL_ROOT`, temp dirs, and the home-dir keys that make
//!     `~/.vct/launcher.db` resolvable) still reach it.
//!
//! That allowlist is exactly the kind of list that silently drifts when
//! copy-pasted: add a key at one call-site, forget the others, and one
//! spawn resolves the DB while another can't. The canonical instance is
//! `projects_v2::apply_project_env_via_python` (the config-projection
//! writer). This module lifts its `env_clear` + re-injection block into
//! one home so future `-m vco_lib.<module>` spawns that need the same
//! sandbox call [`reinject_minimal_env`] instead of re-deriving the
//! allowlist.
//!
//! ## What is deliberately NOT here
//!
//! Not every `python -m vco_lib.<module>` spawn wants an `env_clear`
//! sandbox. The `project_init` subcommand spawns (bootstrap-collections,
//! install-bundle, migrate-*, drop-collections) INHERIT the full parent
//! env and set `.current_dir(orchestrator_root)` so the in-tree
//! `vco_lib` namespace package resolves; they are a different shape and
//! are intentionally left on their own env plumbing. Likewise the
//! wrapper-script spawns (codegraph analyzer, kg-sync, kg-summary) and
//! the inline `-c` deferral emitters do not use `-m vco_lib` at all.
//! This module targets ONLY the env-sandbox shape.
//!
//! ## `.silent()` note
//!
//! Callers own the `Command` and its `.silent()` marker (the
//! `command_silent_gate` integration test scans by path, and this file
//! is in scope). [`reinject_minimal_env`] takes a `&mut Command` that
//! the caller has already built with `.silent()`, so there is no bare
//! `Command::new` in this module to silence.

use std::process::Command;

use vct_launcher_core::db::Db;

/// Clear the child's inherited environment and re-inject ONLY the
/// allowlisted keys that a `python -m vco_lib.<module>` subprocess needs.
///
/// This is the canonical env sandbox for launcher → `vco_lib` spawns.
/// Mutates `cmd` in place (chaining is inconvenient because `env_clear`
/// / `env` return `&mut Command`, and the caller usually already holds a
/// `let mut cmd`).
///
/// The allowlist (kept in ONE place so it can't drift across call-sites):
///   * `PATH` — the child needs it to find `python`'s own helpers.
///   * `VCT_STATE_DIR` — launcher-state root override (else the resolver
///     falls back to `~/.vct/`).
///   * `VCT_HUB_PORT` / `VCT_HUB_TOKEN` — hub-aware resolver hints.
///   * `VCT_INSTALL_ROOT` — so `python -m vco_lib...` resolves `vco_lib`
///     as an implicit-namespace package from the orchestrator clone
///     (`vco_lib` is NOT pip-installed).
///   * `TEMP` / `TMP` / `TMPDIR` — so the child's atomic-write tempfiles
///     land somewhere writable (Windows + macOS especially).
///   * home-dir keys — `HOME` (POSIX) or
///     `USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`HOMEDRIVE`/`HOMEPATH`
///     (Windows) — so the `~/.vct/launcher.db` fallback resolves.
///
/// Anything NOT on this list (e.g. an inherited `KG_COLLECTION`) is
/// dropped by the preceding `env_clear`, which is the whole point.
pub fn reinject_minimal_env(cmd: &mut Command) {
    cmd.env_clear();

    // A key is re-injected only when present in the parent env; a missing
    // key stays missing (never re-injected as empty), preserving the
    // "absent means absent" contract the Python resolver relies on.
    for key in [
        "PATH",
        "VCT_STATE_DIR",
        "VCT_HUB_PORT",
        "VCT_HUB_TOKEN",
        "VCT_INSTALL_ROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
    ] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }

    // Home-dir keys so `~/.vct/launcher.db` (and the atomic-write temp
    // fallback) resolve. Split per-OS: Windows needs the USERPROFILE
    // family; POSIX needs HOME.
    #[cfg(target_os = "windows")]
    {
        for key in ["USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"] {
            if let Ok(v) = std::env::var(key) {
                cmd.env(key, v);
            }
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        if let Ok(v) = std::env::var("HOME") {
            cmd.env("HOME", v);
        }
    }
}

/// Resolve the orchestrator clone root for a `vco_lib` spawn, DB-cache
/// first. Thin pass-through to the canonical Rust resolver so bridge
/// callers don't each reach into `commands::installer`.
///
/// Returns `None` for a standalone binary with no discoverable clone —
/// callers should then OMIT any `--orchestrator-root` flag (the Python
/// CLI defaults it to `None`), never pass an empty string.
pub fn resolve_orchestrator_root(db: &Db) -> Option<std::path::PathBuf> {
    crate::commands::installer::resolve_orchestrator_root(db)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `reinject_minimal_env` must DROP a key that is not on the
    /// allowlist. We assert on the built `Command`'s `get_envs()` view
    /// (which reflects `env_clear` + explicit `env`).
    #[test]
    fn drops_non_allowlisted_key() {
        // Set a sentinel disallowed key; after the sandbox it must NOT
        // appear among the re-injected overrides.
        std::env::set_var("KG_COLLECTION", "SENTINEL_SHOULD_BE_DROPPED");

        let mut cmd = Command::new("python3");
        reinject_minimal_env(&mut cmd);

        // After env_clear, get_envs() lists exactly the keys we
        // re-injected (value Some) — nothing is inherited implicitly.
        let envs: Vec<(String, Option<String>)> = cmd
            .get_envs()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().to_string(),
                    v.map(|vv| vv.to_string_lossy().to_string()),
                )
            })
            .collect();

        assert!(
            !envs.iter().any(|(k, _)| k == "KG_COLLECTION"),
            "KG_COLLECTION leaked into the sandboxed child env: {:?}",
            envs
        );

        std::env::remove_var("KG_COLLECTION");
    }

    /// An allowlisted key present in the parent env IS re-injected.
    #[test]
    fn keeps_allowlisted_key() {
        std::env::set_var("VCT_INSTALL_ROOT", "/tmp/sentinel-install-root");

        let mut cmd = Command::new("python3");
        reinject_minimal_env(&mut cmd);

        let hit = cmd.get_envs().any(|(k, v)| {
            k.to_string_lossy() == "VCT_INSTALL_ROOT"
                && v.map(|vv| vv.to_string_lossy() == "/tmp/sentinel-install-root")
                    .unwrap_or(false)
        });
        assert!(hit, "VCT_INSTALL_ROOT should be re-injected by the sandbox");

        std::env::remove_var("VCT_INSTALL_ROOT");
    }
}
