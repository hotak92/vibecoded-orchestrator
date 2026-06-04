// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.47 RL-7.5 (2026-06-04) — Chunker-revision deferral hook.
//!
//! When the launcher detects an upgrade across the v0.2.46 chunker
//! boundary (any pre-v0.2.46 → v0.2.46+), it appends an entry to each
//! registered project's `<project>/.claude/context/UPDATE_DEFERRED.md`
//! prompting the user to re-sync KG / codegraph against the new chunker
//! presets (see `claude_mcp_servers/weaviate_mcp/chunking.py`
//! ::_CHUNKER_REVISION).
//!
//! Why this matters: post-v0.2.46 the chunker uses MUCH larger chunks
//! for qwen3-embedding (target 9500 tokens vs. legacy 1000), and
//! existing Weaviate rows synced under the legacy preset have stale
//! boundaries. Search recall degrades on long answers because relevant
//! content lives in chunk N+1 that the new preset would have folded
//! into chunk N. Re-syncing rebuilds the chunks under the new preset
//! and restores recall.
//!
//! Soft-fail discipline: the hook can fail in any step (filesystem
//! errors, project unreachable, malformed version string) without
//! blocking launcher startup. Worst case: the user keeps the legacy
//! chunks and search quality slowly degrades — but the launcher still
//! boots and the user can re-sync manually later.

use crate::db::Db;
use std::fs;
use std::path::{Path, PathBuf};

/// Version (inclusive lower bound on the NEW side) at which the
/// chunker-preset overhaul lands. Anyone whose `prev` was strictly
/// less than this AND whose `running` is >= this needs the deferral
/// nudge.
pub const CHUNKER_BUMP_VERSION: &str = "0.2.46";

/// Outcome of the deferral check. Exposed for tests; production
/// callers can ignore the return value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeferralOutcome {
    /// Upgrade did not cross the chunker boundary. No deferral written.
    NoActionNeeded,
    /// Upgrade DID cross the boundary; deferral notes appended to
    /// `projects_touched` per-project UPDATE_DEFERRED.md files.
    Deferred { projects_touched: usize },
    /// Version string parsing failed. Logged on stderr; treated as
    /// no-op (the next compatible boot will retry).
    Skipped,
}

/// Compare two semver-style "X.Y.Z" strings. Returns `Ordering::Less`
/// when ``a < b``, etc. Returns `None` when either string is malformed.
/// We don't pull in `semver` for this — the launcher version strings
/// are always X.Y.Z without pre-release tags.
fn compare_versions(a: &str, b: &str) -> Option<std::cmp::Ordering> {
    let parse = |s: &str| -> Option<(u32, u32, u32)> {
        let parts: Vec<&str> = s.split('.').collect();
        if parts.len() != 3 {
            return None;
        }
        let major = parts[0].parse::<u32>().ok()?;
        let minor = parts[1].parse::<u32>().ok()?;
        let patch = parts[2].parse::<u32>().ok()?;
        Some((major, minor, patch))
    };
    let pa = parse(a)?;
    let pb = parse(b)?;
    Some(pa.cmp(&pb))
}

/// Write the deferral notice to one project's `<project>/.claude/context/UPDATE_DEFERRED.md`.
/// Appends if the file exists; creates parent directories as needed.
fn write_deferral_for_project(
    project_folder: &Path,
    prev: &str,
    running: &str,
) -> Result<(), String> {
    let deferred_dir = project_folder.join(".claude").join("context");
    if let Err(e) = fs::create_dir_all(&deferred_dir) {
        return Err(format!("create_dir_all {:?}: {}", deferred_dir, e));
    }
    let deferred_path = deferred_dir.join("UPDATE_DEFERRED.md");

    let entry = format!(
        "\n## chunker_preset_overhaul ({}  →  {})\n\n\
         **Action**: re-sync this project's KG and code graph so the new \
         chunker presets (v0.2.46+) take effect. Existing Weaviate rows \
         were chunked under the smaller pre-v0.2.46 presets — search \
         recall degrades on long answers because relevant content sits \
         in chunk N+1 that the new preset would have folded into chunk N.\n\n\
         Run from this project directory:\n\n\
         ```\n\
         .claude/scripts/kg-sync --all --force\n\
         .claude/scripts/code-graph-analyze . --force\n\
         ```\n\n\
         Both commands re-chunk every node / source file under the new \
         presets and overwrite the existing Weaviate rows. Heavy I/O — \
         consider running them when you're not actively coding.\n\n\
         Background: the chunker overhaul lands in commit\n\
         `v0.2.47 chunker-preset-overhaul` (see CHANGELOG.md for details).\n",
        prev, running
    );

    let existing = fs::read_to_string(&deferred_path).unwrap_or_default();
    // Idempotent — if the same upgrade pair entry is already in the file,
    // skip the append. Match on the header line.
    let header_marker = format!("## chunker_preset_overhaul ({}  →  {})", prev, running);
    if existing.contains(&header_marker) {
        return Ok(());
    }

    let combined = if existing.is_empty() {
        entry
    } else {
        format!("{}{}", existing, entry)
    };

    if let Err(e) = fs::write(&deferred_path, combined) {
        return Err(format!("write {:?}: {}", deferred_path, e));
    }
    Ok(())
}

/// Write the chunker-revision deferral notice to the orchestrator-root
/// project's `<project>/.claude/context/UPDATE_DEFERRED.md`. Other
/// registered projects are NOT touched — per the user's design
/// (2026-06-04), launcher-driven updates affect only the root project's
/// deferral surface. Individual project re-syncs are user-initiated
/// per-project.
///
/// Returns 1 on success, 0 when no orchestrator-root row exists or
/// when the folder is missing on disk. Soft-fails throughout.
fn write_deferral_for_root_project(db: &Db, prev: &str, running: &str) -> usize {
    let guard = db.lock();
    let folder_path_result: Result<String, rusqlite::Error> = guard.query_row(
        "SELECT folder_path FROM projects WHERE host = 'orchestrator_root' LIMIT 1",
        [],
        |row| row.get(0),
    );
    drop(guard);

    let folder_str = match folder_path_result {
        Ok(s) => s,
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            // No orchestrator-root project registered yet (first install
            // on this machine or pre-v0.2.21 install). Nothing to defer.
            return 0;
        }
        Err(e) => {
            eprintln!(
                "[chunker-deferral] SELECT orchestrator-root failed: {} — skipping",
                e
            );
            return 0;
        }
    };

    let folder = PathBuf::from(&folder_str);
    if !folder.is_dir() {
        eprintln!(
            "[chunker-deferral] orchestrator-root folder missing on disk: {} — skipping",
            folder_str
        );
        return 0;
    }

    match write_deferral_for_project(&folder, prev, running) {
        Ok(()) => 1,
        Err(e) => {
            eprintln!(
                "[chunker-deferral] write failed for {}: {} — skipping",
                folder_str, e
            );
            0
        }
    }
}

/// Public entry point called from `lib.rs::setup` AFTER the existing
/// `bust_cache_if_launcher_version_changed` hook fires with a
/// `VersionChanged` outcome.
///
/// Performs the version comparison and short-circuits when the upgrade
/// didn't cross the v0.2.46 boundary.
///
/// **Two-flow design** (locked 2026-06-04):
///
///   * Launcher-driven update (this hook): deferral lands ONLY in the
///     orchestrator-root project's UPDATE_DEFERRED.md. Other registered
///     projects are NOT touched here.
///   * Per-project bundle update (separate flow, `install-bundle --update`
///     run from inside a project folder): deferral lands in THAT
///     specific project's UPDATE_DEFERRED.md. Handled by
///     `vco_lib.project_init.install_bundle` — see its own deferral path.
pub fn write_chunker_deferral_if_crossing_boundary(
    db: &Db,
    prev: &str,
    running: &str,
) -> DeferralOutcome {
    let prev_vs_bump = match compare_versions(prev, CHUNKER_BUMP_VERSION) {
        Some(o) => o,
        None => {
            eprintln!(
                "[chunker-deferral] version parse failed: prev={}, running={} — skipping",
                prev, running
            );
            return DeferralOutcome::Skipped;
        }
    };
    let running_vs_bump = match compare_versions(running, CHUNKER_BUMP_VERSION) {
        Some(o) => o,
        None => return DeferralOutcome::Skipped,
    };

    // Upgrade crosses the boundary iff prev < v0.2.46 AND running >= v0.2.46.
    let crosses = prev_vs_bump == std::cmp::Ordering::Less
        && running_vs_bump != std::cmp::Ordering::Less;

    if !crosses {
        return DeferralOutcome::NoActionNeeded;
    }

    let touched = write_deferral_for_root_project(db, prev, running);
    eprintln!(
        "[chunker-deferral] upgrade crossed v0.2.46 boundary ({} → {}); \
         wrote re-sync notice to orchestrator-root project ({} written)",
        prev, running, touched
    );
    DeferralOutcome::Deferred {
        projects_touched: touched,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cmp::Ordering;
    use tempfile::TempDir;

    #[test]
    fn compare_versions_handles_normal_cases() {
        assert_eq!(compare_versions("0.2.45", "0.2.46"), Some(Ordering::Less));
        assert_eq!(compare_versions("0.2.46", "0.2.46"), Some(Ordering::Equal));
        assert_eq!(compare_versions("0.2.47", "0.2.46"), Some(Ordering::Greater));
        assert_eq!(compare_versions("0.3.0", "0.2.99"), Some(Ordering::Greater));
        assert_eq!(compare_versions("1.0.0", "0.99.99"), Some(Ordering::Greater));
    }

    #[test]
    fn compare_versions_rejects_malformed() {
        assert_eq!(compare_versions("", "0.2.46"), None);
        assert_eq!(compare_versions("0.2", "0.2.46"), None);
        assert_eq!(compare_versions("0.2.x", "0.2.46"), None);
        assert_eq!(compare_versions("0.2.46-dev", "0.2.46"), None);
    }

    #[test]
    fn write_deferral_for_project_creates_file_and_directory() {
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains("chunker_preset_overhaul"));
        assert!(content.contains("0.2.45"));
        assert!(content.contains("0.2.46"));
        assert!(content.contains("kg-sync --all --force"));
    }

    #[test]
    fn write_deferral_is_idempotent_for_same_pair() {
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let after_first = std::fs::read_to_string(&path).unwrap();
        // Second call with same versions should be a no-op.
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let after_second = std::fs::read_to_string(&path).unwrap();
        assert_eq!(after_first, after_second);
    }

    #[test]
    fn write_deferral_appends_distinct_pair() {
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.44", "0.2.46").unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.47").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let content = std::fs::read_to_string(&path).unwrap();
        // Both upgrade pairs should be present.
        assert!(content.contains("0.2.44  →  0.2.46"));
        assert!(content.contains("0.2.45  →  0.2.47"));
    }

    #[test]
    fn check_returns_no_action_when_pre_existing_v0_2_46() {
        // No real DB needed for this code path.
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "0.2.46", "0.2.47");
        assert_eq!(outcome, DeferralOutcome::NoActionNeeded);
    }

    #[test]
    fn check_returns_no_action_for_minor_intra_v0_2_46_bump() {
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "0.2.47", "0.2.48");
        assert_eq!(outcome, DeferralOutcome::NoActionNeeded);
    }

    #[test]
    fn check_returns_deferred_when_crossing_boundary_no_projects() {
        // No projects registered; deferred outcome but touched=0.
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "0.2.45", "0.2.46");
        assert_eq!(
            outcome,
            DeferralOutcome::Deferred {
                projects_touched: 0
            }
        );
    }

    #[test]
    fn check_returns_skipped_for_malformed_versions() {
        let db = Db::open_in_memory().expect("in-memory db");
        let outcome = write_chunker_deferral_if_crossing_boundary(&db, "garbage", "0.2.46");
        assert_eq!(outcome, DeferralOutcome::Skipped);
    }
}
