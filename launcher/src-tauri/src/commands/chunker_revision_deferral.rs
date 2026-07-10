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
use std::path::{Path, PathBuf};
use vct_launcher_core::process::CommandExt as _;

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

/// Write the chunker-resync deferral notice to one project's
/// `<project>/.claude/context/UPDATE_DEFERRED.md`.
///
/// A-5 (v0.2.73): routes through the SHARED Python emitter
/// `vco_lib.project_init._emit_chunker_resync_deferral` (which builds a proper
/// `DeferralEntry` via `DeferralReport`) instead of the previous forked
/// raw-Markdown writer. The old writer violated the shared schema: it put a
/// version pair in the severity slot (`## chunker_preset_overhaul (a → b)`),
/// which the Python parser coerced to "warning" and rewrote on any round-trip,
/// losing the version info AND all prose; it never updated the YAML
/// `condition_ids:` frontmatter, so the entry was invisible to the
/// session-start reminder nudge that is the deferral block's whole purpose.
///
/// Delegating to the single Python emitter fixes ALL of that (correct
/// condition_id `chunker_preset_overhaul_pending`, `info` severity, frontmatter
/// + reminder-splice, per-project re-sync commands) and keeps one home for the
/// deferral shape. MUST match the subprocess-into-Python pattern in
/// `storage_ux::emit_deferral` / `module_updates` (mirror-don't-fork).
fn write_deferral_for_project(
    project_folder: &Path,
    prev: &str,
    running: &str,
) -> Result<(), String> {
    let repo_root = super::installer::find_local_repo_root()
        .map_err(|e| format!("cannot locate repo root for deferral emit: {}", e))?;
    let py = pick_python()
        .ok_or_else(|| "no python on PATH to emit chunker deferral".to_string())?;

    // Pre-escape every value into Python double-quoted string literals so the
    // `-c` snippet is injection-safe (same discipline as storage_ux::emit_deferral).
    let repo_py = py_quote(&repo_root.to_string_lossy());
    let folder_py = py_quote(&project_folder.to_string_lossy());
    let prev_py = py_quote(prev);
    let running_py = py_quote(running);
    let script = format!(
        "import sys\n\
         sys.path.insert(0, {repo_py})\n\
         from pathlib import Path\n\
         from vco_lib.project_init import _emit_chunker_resync_deferral\n\
         _emit_chunker_resync_deferral(Path({folder_py}), {prev_py}, {running_py})\n",
    );
    let status = std::process::Command::new(&py)
        .silent()
        .arg("-c")
        .arg(&script)
        .status();
    match status {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => Err(format!("chunker deferral python helper exited {}", s)),
        Err(e) => Err(format!("chunker deferral python helper spawn failed: {}", e)),
    }
}

/// Resolve a Python interpreter for the chunker-resync deferral `-c` snippet.
///
/// v0.2.77 (Part 7c task 1): delegates to the shared RT-4 ladder in
/// `vct_launcher_core::python_resolve`. Previously a PATH-only walk; the ladder
/// prefers the orchestrator venv (which has `vco_lib` importable) before PATH,
/// keeping every Rust-side emitter's resolution consistent (one home).
fn pick_python() -> Option<String> {
    vct_launcher_core::python_resolve::resolve_python_for_vco_lib_str()
}

/// Quote `s` as a Python double-quoted string literal. Mirrors
/// `storage_ux::py_quote` byte-for-byte (kept local: that fn is private and
/// widening its visibility for one caller adds no architectural value — same
/// rationale documented in `module_updates::py_quote`).
fn py_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
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

    // A-5 (v0.2.73): these tests now exercise the Python-routed emitter
    // (`vco_lib.project_init._emit_chunker_resync_deferral` via subprocess),
    // so they require `python3`/`python` on PATH + the repo root resolvable
    // from the test's CWD (both true in CI + local dev). If python is
    // unavailable the writer returns Err and the test skips its content
    // assertions rather than falsely failing — the routing contract is the
    // thing under test, and the Python emitter's OWN content is pinned by
    // `tests/test_v0247_chunker_deferral_python.py`.

    fn python_available() -> bool {
        super::pick_python().is_some()
            && super::super::installer::find_local_repo_root().is_ok()
    }

    #[test]
    fn write_deferral_for_project_creates_file_and_directory() {
        if !python_available() {
            eprintln!("skipping: python/repo-root unavailable");
            return;
        }
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let content = std::fs::read_to_string(&path).unwrap();
        // Canonical condition_id from the shared Python emitter (NOT the old
        // raw `## chunker_preset_overhaul (a → b)` header that put a version
        // pair in the severity slot).
        assert!(content.contains("chunker_preset_overhaul_pending"));
        assert!(content.contains("0.2.45"));
        assert!(content.contains("0.2.46"));
        // v0.2.75 (C-10 family fix): the emitted commands must be ones the
        // target CLIs actually accept — `kg-sync` has no `--force` and the
        // analyzer's argparse rejects `--force` (the real drop+rebuild flag
        // is `--force-recreate`). Family-wide guard:
        // tests/test_deferral_command_argparse_sweep.py.
        assert!(content.contains("kg-sync --all"));
        assert!(!content.contains("kg-sync --all --force"));
        assert!(content.contains("code-graph-analyze . --force-recreate"));
    }

    #[test]
    fn write_deferral_is_idempotent_for_same_pair() {
        if !python_available() {
            eprintln!("skipping: python/repo-root unavailable");
            return;
        }
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let after_first = std::fs::read_to_string(&path).unwrap();
        // Second call: DeferralReport keys by condition_id, so re-emitting the
        // same pending condition is idempotent (no duplicate entry).
        write_deferral_for_project(td.path(), "0.2.45", "0.2.46").unwrap();
        let after_second = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            after_first.matches("chunker_preset_overhaul_pending").count(),
            after_second.matches("chunker_preset_overhaul_pending").count(),
            "re-emit must not add a duplicate condition entry"
        );
    }

    #[test]
    fn write_deferral_single_pending_condition_regardless_of_version_pairs() {
        if !python_available() {
            eprintln!("skipping: python/repo-root unavailable");
            return;
        }
        let td = TempDir::new().unwrap();
        write_deferral_for_project(td.path(), "0.2.44", "0.2.46").unwrap();
        write_deferral_for_project(td.path(), "0.2.45", "0.2.47").unwrap();
        let path = td.path().join(".claude").join("context").join("UPDATE_DEFERRED.md");
        let content = std::fs::read_to_string(&path).unwrap();
        // The condition is "re-sync pending" — one condition_id, keyed once
        // regardless of the version pair that triggered it (unlike the old
        // per-pair header). `add_entry` dedups by condition_id (last-write-
        // wins), so exactly one section header exists. (Count the section
        // header, not the bare slug, which ALSO appears in the
        // `condition_ids:` frontmatter list → 2 substring matches per entry.)
        assert_eq!(
            content.matches("## chunker_preset_overhaul_pending (").count(),
            1,
            "only one pending-resync condition section should exist"
        );
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
