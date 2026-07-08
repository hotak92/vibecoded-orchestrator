//! V52-F (v0.2.52): per-module update GUI surface.
//!
//! Closes the user-flagged gap from 2026-06-09: "there's no GUI way to
//! update an installed module when a new version is released".
//!
//! ## Layering
//!
//! The heavy-lifting already exists upstream:
//!   - `module_catalog_client::cached_module_catalog` fetches the L0
//!     catalog (which routes through `paid_module_releases` server-side)
//!     with a 15-min TTL.
//!   - `modules::update_module_for_project` does the atomic swap: re-resolves
//!     manifest from L0, runs `installer_engine::run_upgrade`, bumps the
//!     `module_installs.module_version`, restarts the container.
//!   - `module-status-display.ts::semverLess` is the version comparator the
//!     renderer already uses to gate the existing `Update v0.2.7 → v0.2.8`
//!     button.
//!
//! What this module adds:
//!   1. `check_module_updates_available(project_id)` — summary Tauri command.
//!      Returns `Vec<ModuleUpdateAvailable>` (per-project) for the renderer
//!      to compute a badge count without re-running the full
//!      `resolveTileDisplay` pipeline. Pure DB+cache read; sub-50ms typical.
//!   2. `update_module_to_latest(project_id, module_id)` — convenience wrapper
//!      around `modules::update_module_for_project` that adds a "no-op when
//!      already latest" guard so the GUI button is safe to re-click. Returns
//!      `UpdateModuleOutcome` distinguishing already-latest / updated /
//!      partial-failure.
//!   3. `spawn_module_update_check_loop` — background tokio task that
//!      refreshes the L0 catalog on a 24h cadence (with catch-up on first
//!      tick after launcher boot) and emits the `vct-module-updates-available`
//!      Tauri event so the menubar / sidebar can refresh badges without
//!      polling on a hot timer.
//!   4. Opt-out setting via app_state KV: key `module_update_auto_check_enabled`
//!      (default true — opt-out, not opt-in).
//!
//! ## Why not query `paid_module_releases` directly?
//!
//! The backlog spec suggests "read paid_module_releases table per installed
//! module ID, get pinned version". The L0 catalog endpoint
//! (`module_catalog_client::resolved_endpoint_url`) already does this
//! server-side and returns the orchestrator-shaped projection. Adding a
//! second client-side path that hits Supabase directly creates two
//! consequences we want to avoid:
//!
//!   - Drift: the L0 catalog projection has compatibility/license-gating
//!     fields that the renderer needs anyway. Bypassing L0 means re-deriving
//!     those server-side.
//!   - Auth churn: L0 needs the launcher's secret. A second direct-Supabase
//!     path would need its own anon-key surface. Keeping one client path
//!     reduces credential plumbing.
//!
//! The L0 catalog's `paid_module_releases` join is the canonical source-of-
//! truth. We layer on top of it.
//!
//! ## UPDATE_DEFERRED on partial failure
//!
//! `update_module_to_latest` calls `update_module_for_project`. On error,
//! we mirror the storage_ux deferral pattern: append a
//! `module_update_partial_failure` entry to UPDATE_DEFERRED.md so the user
//! sees an actionable retry suggestion at session start. Best-effort —
//! a deferral-write failure must NOT mask the underlying update error.

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, Runtime, State};

use crate::commands::module_catalog_client::{
    cached_module_catalog, L0CatalogModule, L0CatalogResponse,
};
use crate::commands::modules::update_module_for_project;
use crate::db::models::ModuleInstallRow;
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

// ─── Constants ──────────────────────────────────────────────────────────

/// app_state key for the user's opt-out toggle. Default true (auto-check ON).
pub const AUTO_CHECK_KEY: &str = "module_update_auto_check_enabled";

/// app_state key recording the timestamp (ms since epoch) of the last
/// successful module-update poll. Used by `spawn_module_update_check_loop`
/// to decide whether the "catch-up" tick fires immediately at startup or
/// can be deferred to the next 24h slot.
pub const LAST_CHECKED_AT_KEY: &str = "module_update_last_checked_at_ms";

/// Poll interval — 24h, mirrors the user-facing wording in the spec.
pub const POLL_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

/// Wake-up tick so the loop re-reads the opt-out toggle and re-checks
/// the catch-up timer without sleeping a full 24h. Same shape as
/// `self_update::spawn_daily_check` (hourly wake).
pub const WAKE_INTERVAL: Duration = Duration::from_secs(60 * 60);

/// Tauri event name emitted on every successful poll that found one or
/// more updates. The payload is the same `Vec<ModuleUpdateAvailable>`
/// the Tauri command returns. The renderer can listen and refresh badges.
pub const EVENT_UPDATES_AVAILABLE: &str = "vct-module-updates-available";

// ─── Wire types ─────────────────────────────────────────────────────────

/// One installed module that has a newer version available in the L0
/// catalog. Returned as a list by `check_module_updates_available`.
///
/// Comparison uses the same `semverLess` semantics as the renderer-side
/// `module-status-display.ts::resolveTileDisplay`: leading-integer
/// per-segment, lexicographic. Pre-release suffixes (e.g. `-dev`) are
/// ignored.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ModuleUpdateAvailable {
    pub project_id: String,
    pub module_id: String,
    pub current_version: String,
    pub available_version: String,
}

/// Outcome of `update_module_to_latest`. The renderer toasts based on
/// `kind`. Explicit shape (not just a string) so future variants (e.g.
/// `requires_restart`) can land without breaking the wire format.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum UpdateModuleOutcome {
    /// No-op: the installed version matches the latest catalog version.
    /// Returned even when the user clicked the button — the renderer
    /// can decide to silently refresh the catalog or surface a "you're
    /// already up to date" toast.
    AlreadyLatest { version: String },
    /// Update applied successfully. `previous_version` and `new_version`
    /// are distinct.
    Updated {
        previous_version: String,
        new_version: String,
    },
}

// ─── Version comparison (mirrors module-status-display.ts::semverLess) ───

/// Returns true iff `a` is strictly less than `b` under leading-integer
/// per-segment semver comparison.
///
/// Mirrors `module-status-display.ts::semverLess` so the renderer and
/// backend agree on which installs need a badge.
///
/// Examples:
///   - `semver_less("0.2.7", "0.2.8")` → true
///   - `semver_less("0.2.8", "0.2.8")` → false
///   - `semver_less("0.2.8-dev", "0.2.8")` → false (leading int matches)
///   - `semver_less("0.2.7-rc1", "0.2.8-dev")` → true (7 < 8)
pub fn semver_less(a: &str, b: &str) -> bool {
    let parse = |v: &str| -> Vec<u64> {
        v.split('.')
            .map(|seg| {
                // Leading integer prefix; "0.2.4-dev" → 4 for the third
                // segment. Mirrors the renderer-side regex `^(\d+)`.
                let mut n: u64 = 0;
                let mut any = false;
                for c in seg.chars() {
                    if let Some(d) = c.to_digit(10) {
                        n = n.saturating_mul(10).saturating_add(d as u64);
                        any = true;
                    } else {
                        break;
                    }
                }
                if any {
                    n
                } else {
                    0
                }
            })
            .collect()
    };
    let aa = parse(a);
    let bb = parse(b);
    let n = aa.len().max(bb.len());
    for i in 0..n {
        let x = aa.get(i).copied().unwrap_or(0);
        let y = bb.get(i).copied().unwrap_or(0);
        if x < y {
            return true;
        }
        if x > y {
            return false;
        }
    }
    false
}

// ─── Pure summary helpers (testable without Tauri State / network) ──────

/// Compute the update-availability summary by intersecting installed
/// rows against the L0 catalog. Pure function: caller supplies both
/// inputs, no I/O.
///
/// Filtering rules:
///   - Skip rows whose `status != installed | running | stopped`
///     (errored / installing / broken rows aren't candidates for
///     "Update" — they need Retry instead).
///   - Skip rows whose `module_id` isn't in the catalog (manually
///     installed modules, or modules whose catalog entry was removed).
///   - Skip when `current_version >= available_version` per `semver_less`.
///
/// Returns the entries sorted by `module_id` (stable rendering).
pub fn compute_updates_available(
    installed: &[ModuleInstallRow],
    catalog: &L0CatalogResponse,
) -> Vec<ModuleUpdateAvailable> {
    use crate::db::models::ModuleStatus;

    let by_id: std::collections::HashMap<&str, &L0CatalogModule> = catalog
        .modules
        .iter()
        .map(|m| (m.id.as_str(), m))
        .collect();

    let mut out: Vec<ModuleUpdateAvailable> = installed
        .iter()
        .filter(|row| {
            matches!(
                row.status,
                ModuleStatus::Installed | ModuleStatus::Running | ModuleStatus::Stopped
            )
        })
        .filter_map(|row| {
            let catalog_entry = by_id.get(row.module_id.as_str())?;
            if !semver_less(&row.module_version, &catalog_entry.version) {
                return None;
            }
            Some(ModuleUpdateAvailable {
                // project_id is Option<String> on the row (global modules
                // have None). Surface the empty string for those — the
                // renderer treats `""` as the orchestrator-root / global
                // scope when filtering badges.
                project_id: row.project_id.clone().unwrap_or_default(),
                module_id: row.module_id.clone(),
                current_version: row.module_version.clone(),
                available_version: catalog_entry.version.clone(),
            })
        })
        .collect();

    out.sort_by(|a, b| a.module_id.cmp(&b.module_id));
    out
}

// ─── Tauri commands ─────────────────────────────────────────────────────

/// List modules with available updates for a single project.
///
/// Resolution:
///   1. Read `module_installs` rows for this project from launcher.db
///      (sub-ms).
///   2. Read the L0 catalog via `cached_module_catalog` (cache hit:
///      sub-ms; cache miss: one HTTPS round-trip + 15-min TTL write).
///   3. Run `compute_updates_available` to produce the summary.
///
/// Soft-fail on catalog fetch error: returns `Ok(vec![])` rather than
/// erroring. The renderer's existing per-tile `can_update` gate fills
/// the gap (and shows the catalog-error banner separately) — this
/// command exists for the badge-count surface, where a network blip
/// shouldn't make every project look broken.
#[command]
pub async fn check_module_updates_available(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<ModuleUpdateAvailable>, String> {
    let installed = db.list_module_installs_for_project(&project_id)?;
    let catalog = match cached_module_catalog(db.inner()).await {
        Ok(c) => c,
        Err(e) => {
            eprintln!(
                "[module_updates] check_module_updates_available({}) catalog fetch \
                 failed (soft-fail to empty list): {}",
                project_id, e
            );
            return Ok(vec![]);
        }
    };

    Ok(compute_updates_available(&installed, &catalog))
}

/// Convenience wrapper around `update_module_for_project` that:
///   - Returns `AlreadyLatest` when the install row already matches the
///     catalog's current version (idempotent — safe to re-click).
///   - Returns `Updated { previous_version, new_version }` on success.
///   - On error, emits an `module_update_partial_failure` entry to
///     UPDATE_DEFERRED.md (best-effort) and propagates the underlying
///     error to the caller. The deferral keeps the failure surfaced at
///     next session start even if the user dismisses the toast.
#[command]
pub async fn update_module_to_latest(
    app: AppHandle,
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<UpdateModuleOutcome, String> {
    // 1. Check current vs available — pure read, sub-ms.
    let installed = db
        .get_module_install(&project_id, &module_id)?
        .ok_or_else(|| {
            format!(
                "module {} not installed for project {}; use install_module_for_project",
                module_id, project_id,
            )
        })?;
    let previous_version = installed.module_version.clone();

    let catalog = cached_module_catalog(db.inner())
        .await
        .map_err(|e| format!("module catalog fetch: {}", e))?;
    let catalog_entry = catalog
        .modules
        .iter()
        .find(|m| m.id == module_id)
        .ok_or_else(|| format!("module {} not in catalog", module_id))?;
    let catalog_version = catalog_entry.version.clone();

    // 2. Already-latest fast path. Both `semver_less` returning false
    //    AND the versions matching exactly are treated as "already
    //    latest" — the user might be one minor ahead of catalog due to
    //    a manual install, in which case re-running the update would
    //    *downgrade*. Don't.
    if !semver_less(&previous_version, &catalog_version) {
        return Ok(UpdateModuleOutcome::AlreadyLatest {
            version: previous_version,
        });
    }

    // 3. Delegate to the existing update path (atomic swap, status
    //    flip, audit).
    let result = update_module_for_project(
        app.clone(),
        project_id.clone(),
        module_id.clone(),
        db.clone(),
    )
    .await;

    match result {
        Ok(_install_row) => Ok(UpdateModuleOutcome::Updated {
            previous_version,
            new_version: catalog_version,
        }),
        Err(e) => {
            // Best-effort deferral entry so the failure isn't only in
            // the toast — surfaces at next session start.
            write_partial_failure_deferral(&module_id, &previous_version, &catalog_version, &e);
            Err(e)
        }
    }
}

/// Tauri command exposing the opt-out toggle to the renderer.
///
/// Default ON (auto-check enabled) when no row exists in app_state.
#[command]
pub fn get_module_update_auto_check_enabled(db: State<'_, Db>) -> Result<bool, String> {
    Ok(db.app_state_get_bool(AUTO_CHECK_KEY)?.unwrap_or(true))
}

/// Tauri command setting the opt-out toggle. Persists to app_state KV.
#[command]
pub fn set_module_update_auto_check_enabled(
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.app_state_set_bool(AUTO_CHECK_KEY, enabled)?;
    Ok(())
}

// ─── Background poll loop ──────────────────────────────────────────────

/// Spawn the 24h auto-poll task. Runs forever (until app exit).
///
/// Behaviour per wake:
///   - Read the opt-out toggle. If disabled, sleep one wake-interval
///     and re-check. (Cheap — no network, no DB scan.)
///   - Read `LAST_CHECKED_AT_KEY`. If absent OR ≥ 24h ago, run a poll.
///   - Otherwise sleep one wake-interval.
///
/// A poll consists of:
///   1. Warm the L0 catalog cache (`cached_module_catalog` — 15-min TTL,
///      so this is typically a single HTTPS GET).
///   2. Iterate every project, run `compute_updates_available` against
///      the catalog + that project's installs.
///   3. If the aggregated list is non-empty, emit
///      `vct-module-updates-available` with the payload.
///   4. Stamp `LAST_CHECKED_AT_KEY` regardless of result (so a
///      transient empty-catalog blip doesn't cause every tick to retry).
///
/// Soft-fails throughout. Any error logs to stderr and the next tick
/// retries.
///
/// Note: each tick opens a fresh `rusqlite::Connection` inside the
/// task (same pattern as `module_deprecation::poll_deprecations_once`)
/// because `Db` is `Mutex<Connection>` and the lock cannot be held
/// across `await` points without serializing every other command on the
/// same DB. The cost is a sub-millisecond open per 24h tick.
pub fn spawn_module_update_check_loop<R: Runtime>(app: AppHandle<R>) {
    tauri::async_runtime::spawn(async move {
        loop {
            // v0.2.60: stand down while an orchestrator update is in
            // progress. This poller opens its OWN launcher.db connection
            // (below), which bypasses the managed-connection close
            // `update_orchestrator` performs for the install.py window —
            // so without this gate a tick here would re-contend with
            // install.py for the SQLite writer lock (the launcher-self-db-
            // lock bug). Reuses the `.update-in-progress` lockfile.
            if crate::commands::update_gate::skip_if_update_in_progress("module_updates") {
                tokio::time::sleep(WAKE_INTERVAL).await;
                continue;
            }
            // Re-open the DB per tick. The connection is dropped at the
            // end of the iteration's body so the main `Db` State holds
            // the only long-lived connection (the one Tauri commands
            // touch).
            let conn = match rusqlite::Connection::open(crate::db::db_path()) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("[module_updates] open DB: {} — retrying in 1h", e);
                    tokio::time::sleep(WAKE_INTERVAL).await;
                    continue;
                }
            };
            let tick_db = Db(std::sync::Mutex::new(conn));

            let enabled = tick_db
                .app_state_get_bool(AUTO_CHECK_KEY)
                .ok()
                .flatten()
                .unwrap_or(true);
            if !enabled {
                drop(tick_db);
                tokio::time::sleep(WAKE_INTERVAL).await;
                continue;
            }

            let due = {
                let last = tick_db
                    .app_state_get(LAST_CHECKED_AT_KEY)
                    .ok()
                    .flatten()
                    .and_then(|s| s.parse::<i64>().ok());
                match last {
                    None => true,
                    Some(ts_ms) => {
                        let now_ms = chrono::Utc::now().timestamp_millis();
                        let age_ms = now_ms.saturating_sub(ts_ms);
                        (age_ms as u64) >= POLL_INTERVAL.as_secs() * 1000
                    }
                }
            };

            if due {
                run_poll_tick(&app, &tick_db).await;
                let now_ms = chrono::Utc::now().timestamp_millis();
                let _ = tick_db.app_state_set(LAST_CHECKED_AT_KEY, &now_ms.to_string());
            }

            drop(tick_db);
            tokio::time::sleep(WAKE_INTERVAL).await;
        }
    });
}

/// One iteration of the poll loop. Extracted so the loop's control
/// flow stays tight. The `tick_db` is the per-tick connection owned by
/// `spawn_module_update_check_loop`.
async fn run_poll_tick<R: Runtime>(app: &AppHandle<R>, tick_db: &Db) {
    let catalog = match cached_module_catalog(tick_db).await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[module_updates] poll tick: catalog fetch failed: {}", e);
            return;
        }
    };

    // List every project, intersect each with the catalog.
    let projects = match tick_db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[module_updates] poll tick: list_projects failed: {}", e);
            return;
        }
    };

    let mut all_updates: Vec<ModuleUpdateAvailable> = Vec::new();
    for project in &projects {
        let installs = match tick_db.list_module_installs_for_project(&project.id) {
            Ok(rows) => rows,
            Err(e) => {
                eprintln!(
                    "[module_updates] poll tick: list_module_installs_for_project({}) \
                     failed: {}",
                    project.id, e
                );
                continue;
            }
        };
        all_updates.extend(compute_updates_available(&installs, &catalog));
    }

    // Also pull global-scope modules (project_id is None on those rows).
    // `list_global_module_installs` is the canonical reader.
    match tick_db.list_global_module_installs() {
        Ok(rows) => {
            all_updates.extend(compute_updates_available(&rows, &catalog));
        }
        Err(e) => {
            eprintln!(
                "[module_updates] poll tick: list_global_module_installs failed: {}",
                e
            );
        }
    }

    if !all_updates.is_empty() {
        // Emit so any open window can refresh badges. Soft-fail — emit
        // returns Err only when serialization fails, which is impossible
        // for an owned Vec of plain structs.
        let _ = app.emit(EVENT_UPDATES_AVAILABLE, &all_updates);
    }
}

// ─── UPDATE_DEFERRED writer (best-effort) ──────────────────────────────

/// Append a `module_update_partial_failure` entry to UPDATE_DEFERRED.md
/// when `update_module_to_latest` raised an error.
///
/// Mirrors the storage_ux pattern: shells out to a tiny Python `-c`
/// snippet that imports `vco_lib.deferral_report`. Best-effort — any
/// failure here is logged and swallowed so the caller's original error
/// is what reaches the user. Deferrals are an FYI mechanism.
fn write_partial_failure_deferral(
    module_id: &str,
    previous_version: &str,
    new_version: &str,
    error: &str,
) {
    let repo_root = match crate::commands::installer::find_local_repo_root() {
        Ok(r) => r,
        Err(_) => return,
    };
    let py = match pick_python() {
        Some(p) => p,
        None => return,
    };
    let repo_py = py_quote(&repo_root.to_string_lossy());
    let title = format!(
        "Module update failed: {} ({} → {})",
        module_id, previous_version, new_version,
    );
    let detected = format!(
        "While updating {} from {} to {}, the update flow returned an error.",
        module_id, previous_version, new_version,
    );
    let why_deferred = format!("Underlying error: {}", error);
    let command_to_apply = format!(
        "Re-run via the launcher's Modules tab: click Update v{} → v{} on the {} card.",
        previous_version, new_version, module_id,
    );
    let cid_py = py_quote("module_update_partial_failure");
    let title_py = py_quote(&title);
    let det_py = py_quote(&detected);
    let why_py = py_quote(&why_deferred);
    let cmd_py = py_quote(&command_to_apply);
    // Severity must be one of the DeferralEntry SEVERITY_ORDER values
    // (critical|warning|info) — a partial module update the user can retry is
    // a "warning". Before v0.2.75 this passed "medium", which is NOT in the
    // set, so DeferralEntry.__post_init__ raised ValueError and the shelled
    // Python always exited non-zero → this deferral was NEVER written (the
    // error was logged + swallowed). MUST MATCH vco_lib/deferral_report.py
    // SEVERITY_ORDER.
    let sev_py = py_quote("warning");
    let script = format!(
        "import sys\n\
         sys.path.insert(0, {repo_py})\n\
         from pathlib import Path\n\
         from vco_lib.deferral_report import DeferralEntry, DeferralReport\n\
         folder = Path({repo_py})\n\
         report = DeferralReport.read(folder)\n\
         entry = DeferralEntry(\n\
         \x20\x20\x20\x20condition_id={cid_py},\n\
         \x20\x20\x20\x20title={title_py},\n\
         \x20\x20\x20\x20detected={det_py},\n\
         \x20\x20\x20\x20why_deferred={why_py},\n\
         \x20\x20\x20\x20command_to_apply={cmd_py},\n\
         \x20\x20\x20\x20severity={sev_py},\n\
         )\n\
         report.add_entry(entry)\n\
         report.write(folder)\n",
    );
    let status = std::process::Command::new(py)
        .silent()
        .arg("-c")
        .arg(script)
        .status();
    match status {
        Ok(s) if s.success() => {}
        Ok(s) => eprintln!(
            "[module_updates] deferral helper exited {}: module_update_partial_failure({})",
            s, module_id
        ),
        Err(e) => eprintln!("[module_updates] deferral helper spawn failed: {e}"),
    }
}

/// First `python3` then `python` from PATH. Mirrors the resolution
/// order in `storage_ux::emit_deferral` so deferral behaviour is
/// consistent across writers.
fn pick_python() -> Option<PathBuf> {
    for candidate in ["python3", "python"] {
        let probe = std::process::Command::new(candidate)
            .silent()
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
        if probe.map(|s| s.success()).unwrap_or(false) {
            return Some(PathBuf::from(candidate));
        }
    }
    None
}

/// Quote `s` as a Python double-quoted string literal. Mirrors
/// `storage_ux::py_quote` byte-for-byte. Kept local rather than
/// `pub use`-ing across modules because the storage_ux fn is private
/// and re-using it would require widening its visibility for no
/// architectural reason.
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
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::module_catalog_client::{
        L0Compatibility, L0Install, L0InstallContainer,
    };
    use crate::db::models::{ModuleStatus, ProjectHost};
    use crate::manifest::InstallScope;

    // ─── semver_less ────────────────────────────────────────────────

    #[test]
    fn semver_less_basic_inequalities() {
        assert!(semver_less("0.2.7", "0.2.8"));
        assert!(semver_less("0.2.8", "0.3.0"));
        assert!(semver_less("0.2.8", "1.0.0"));
    }

    #[test]
    fn semver_less_equal_returns_false() {
        assert!(!semver_less("0.2.8", "0.2.8"));
        assert!(!semver_less("1.0.0", "1.0.0"));
    }

    #[test]
    fn semver_less_greater_returns_false() {
        // Catalog ahead of installed → installed is LESS → returns true.
        // Catalog BEHIND installed → installed is NOT LESS → returns false.
        // The compute fn uses (current, available) so the "manually
        // pre-installed ahead of catalog" case must NOT trigger an
        // update offer (which would downgrade).
        assert!(!semver_less("0.2.9", "0.2.8"));
        assert!(!semver_less("1.0.0", "0.9.9"));
    }

    #[test]
    fn semver_less_handles_prerelease_suffixes() {
        // Mirror module-status-display.ts: "0.2.4-dev" → 4 for that segment.
        // So "0.2.8-dev" == "0.2.8" under this comparison.
        assert!(!semver_less("0.2.8-dev", "0.2.8"));
        assert!(!semver_less("0.2.8", "0.2.8-dev"));
        // 7 < 8 even with suffixes.
        assert!(semver_less("0.2.7-rc1", "0.2.8-dev"));
    }

    #[test]
    fn semver_less_handles_mismatched_segment_counts() {
        // Missing segments default to 0.
        assert!(semver_less("0.2", "0.2.1"));
        assert!(!semver_less("0.2.0", "0.2"));
        assert!(!semver_less("0.2", "0.2.0"));
    }

    #[test]
    fn semver_less_handles_leading_zeros_and_large_numbers() {
        // Numerical comparison, not lexicographic — "10" > "9".
        assert!(semver_less("0.9.0", "0.10.0"));
        assert!(!semver_less("0.10.0", "0.9.0"));
    }

    // ─── compute_updates_available ─────────────────────────────────

    fn install_row(
        module_id: &str,
        version: &str,
        project_id: Option<&str>,
        status: ModuleStatus,
    ) -> ModuleInstallRow {
        ModuleInstallRow {
            id: format!("install-{}", module_id),
            project_id: project_id.map(String::from),
            module_id: module_id.into(),
            module_version: version.into(),
            install_path: "/tmp/x".into(),
            status,
            enabled: true,
            installed_at: 0,
            last_started_at: None,
            last_error: None,
            container_name: None,
            kg_collections: vec![],
        }
    }

    fn catalog_module(id: &str, version: &str) -> L0CatalogModule {
        L0CatalogModule {
            id: id.into(),
            name: id.into(),
            version: version.into(),
            description: String::new(),
            category: "core".into(),
            tags: vec![],
            homepage: String::new(),
            publisher: String::new(),
            license_required: false,
            min_orchestrator_tier: "free".into(),
            license_variant_ids: vec![],
            trial_days: None,
            compatibility: L0Compatibility {
                hosts: vec!["linux".into()],
                min_launcher_version: None,
            },
            install: L0Install {
                method: "container_pull".into(),
                container: L0InstallContainer {
                    image: "x".into(),
                    tag_from_version: true,
                    registry: None,
                    pull_token_endpoint: "https://x".into(),
                    pull_token_method: "POST".into(),
                },
                scope: InstallScope::PerProject,
            },
            requirements: None,
            runtime_hints: None,
            deprecated: false,
            deprecation_message: String::new(),
            deprecation_eol_date: String::new(),
            deprecation_migration_url: String::new(),
            post_install_manifest_path: "vct-module.json".into(),
        }
    }

    fn catalog_response(modules: Vec<L0CatalogModule>) -> L0CatalogResponse {
        L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-06-09T00:00:00Z".into(),
            modules,
        }
    }

    #[test]
    fn compute_updates_returns_module_when_catalog_ahead() {
        let installed = vec![install_row(
            "vct-rl-reranker",
            "0.2.7",
            Some("p1"),
            ModuleStatus::Installed,
        )];
        let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
        let out = compute_updates_available(&installed, &catalog);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].module_id, "vct-rl-reranker");
        assert_eq!(out[0].current_version, "0.2.7");
        assert_eq!(out[0].available_version, "0.2.8");
        assert_eq!(out[0].project_id, "p1");
    }

    #[test]
    fn compute_updates_omits_module_when_already_latest() {
        let installed = vec![install_row(
            "vct-rl-reranker",
            "0.2.8",
            Some("p1"),
            ModuleStatus::Installed,
        )];
        let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
        assert!(compute_updates_available(&installed, &catalog).is_empty());
    }

    #[test]
    fn compute_updates_omits_module_ahead_of_catalog() {
        // Manually-installed module ahead of catalog must NOT show as
        // "update available" (would downgrade).
        let installed = vec![install_row(
            "vct-rl-reranker",
            "0.3.0",
            Some("p1"),
            ModuleStatus::Installed,
        )];
        let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
        assert!(compute_updates_available(&installed, &catalog).is_empty());
    }

    #[test]
    fn compute_updates_skips_non_installed_statuses() {
        // Errored / broken / installing rows must NOT surface as
        // "Update available" — they need Retry / wait, not Update.
        // ModuleStatus is not Copy; clone per-iteration to satisfy
        // the borrow checker without changing install_row's signature.
        for skip_status in [
            ModuleStatus::Error,
            ModuleStatus::Broken,
            ModuleStatus::Installing,
        ] {
            let label = format!("{:?}", skip_status);
            let installed = vec![install_row(
                "vct-rl-reranker",
                "0.2.7",
                Some("p1"),
                skip_status,
            )];
            let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
            assert!(
                compute_updates_available(&installed, &catalog).is_empty(),
                "status {} should be filtered",
                label,
            );
        }
    }

    #[test]
    fn compute_updates_includes_running_and_stopped() {
        for ok_status in [
            ModuleStatus::Installed,
            ModuleStatus::Running,
            ModuleStatus::Stopped,
        ] {
            let label = format!("{:?}", ok_status);
            let installed = vec![install_row(
                "vct-rl-reranker",
                "0.2.7",
                Some("p1"),
                ok_status,
            )];
            let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
            assert_eq!(
                compute_updates_available(&installed, &catalog).len(),
                1,
                "status {} should be a valid update candidate",
                label,
            );
        }
    }

    #[test]
    fn compute_updates_skips_modules_not_in_catalog() {
        // A module manually installed (not in L0 catalog) must be
        // skipped — we have no version source to compare against.
        let installed = vec![install_row(
            "user-side-module",
            "0.0.1",
            Some("p1"),
            ModuleStatus::Installed,
        )];
        let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
        assert!(compute_updates_available(&installed, &catalog).is_empty());
    }

    #[test]
    fn compute_updates_handles_global_modules_with_null_project_id() {
        // Global-scope modules have project_id=None. Compute fn must
        // emit them with project_id="" (renderer treats "" as global).
        let installed = vec![install_row(
            "vct-rl-reranker",
            "0.2.7",
            None,
            ModuleStatus::Installed,
        )];
        let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
        let out = compute_updates_available(&installed, &catalog);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].project_id, "");
    }

    #[test]
    fn compute_updates_sorts_by_module_id_for_stable_render() {
        let installed = vec![
            install_row("zzz-module", "0.0.1", Some("p1"), ModuleStatus::Installed),
            install_row("aaa-module", "0.0.1", Some("p1"), ModuleStatus::Installed),
            install_row("mmm-module", "0.0.1", Some("p1"), ModuleStatus::Installed),
        ];
        let catalog = catalog_response(vec![
            catalog_module("zzz-module", "0.0.2"),
            catalog_module("aaa-module", "0.0.2"),
            catalog_module("mmm-module", "0.0.2"),
        ]);
        let out = compute_updates_available(&installed, &catalog);
        assert_eq!(out.len(), 3);
        assert_eq!(out[0].module_id, "aaa-module");
        assert_eq!(out[1].module_id, "mmm-module");
        assert_eq!(out[2].module_id, "zzz-module");
    }

    #[test]
    fn compute_updates_empty_when_no_installs() {
        let installed: Vec<ModuleInstallRow> = vec![];
        let catalog = catalog_response(vec![catalog_module("vct-rl-reranker", "0.2.8")]);
        assert!(compute_updates_available(&installed, &catalog).is_empty());
    }

    #[test]
    fn compute_updates_empty_when_catalog_empty() {
        let installed = vec![install_row(
            "vct-rl-reranker",
            "0.2.7",
            Some("p1"),
            ModuleStatus::Installed,
        )];
        let catalog = catalog_response(vec![]);
        assert!(compute_updates_available(&installed, &catalog).is_empty());
    }

    // ─── Auto-check toggle persistence ─────────────────────────────

    #[test]
    fn auto_check_toggle_defaults_on_when_unset() {
        let db = Db::open_in_memory().expect("DB");
        // No row written → reader sees None → defaults to true.
        let got = db.app_state_get_bool(AUTO_CHECK_KEY).expect("read");
        assert_eq!(got, None);
        // The Tauri command wrapper applies the default at the surface,
        // mirrored here:
        assert!(got.unwrap_or(true));
    }

    #[test]
    fn auto_check_toggle_roundtrip() {
        let db = Db::open_in_memory().expect("DB");
        db.app_state_set_bool(AUTO_CHECK_KEY, false).expect("set");
        assert_eq!(
            db.app_state_get_bool(AUTO_CHECK_KEY).expect("read"),
            Some(false)
        );
        db.app_state_set_bool(AUTO_CHECK_KEY, true).expect("set");
        assert_eq!(
            db.app_state_get_bool(AUTO_CHECK_KEY).expect("read"),
            Some(true)
        );
    }

    #[test]
    fn last_checked_at_roundtrip_via_app_state_kv() {
        let db = Db::open_in_memory().expect("DB");
        assert_eq!(db.app_state_get(LAST_CHECKED_AT_KEY).expect("read"), None);
        db.app_state_set(LAST_CHECKED_AT_KEY, "1717900000000")
            .expect("set");
        assert_eq!(
            db.app_state_get(LAST_CHECKED_AT_KEY).expect("read"),
            Some("1717900000000".into())
        );
    }

    // ─── py_quote (used by deferral writer) ────────────────────────

    #[test]
    fn py_quote_escapes_quotes_and_backslashes() {
        assert_eq!(py_quote("hello"), "\"hello\"");
        assert_eq!(py_quote("with \"quote\""), "\"with \\\"quote\\\"\"");
        assert_eq!(py_quote("back\\slash"), "\"back\\\\slash\"");
        assert_eq!(py_quote("line\nbreak"), "\"line\\nbreak\"");
    }

    #[test]
    fn py_quote_escapes_control_chars() {
        // \x01 = control char < 0x20
        let got = py_quote("\x01");
        assert_eq!(got, "\"\\u0001\"");
    }

    // ─── ProjectHost helper to silence unused-import warning in some
    //     test scaffolds — explicit reference so cargo doesn't flag it. ─
    #[test]
    fn project_host_referenced_for_test_scaffold() {
        let _ = ProjectHost::Base;
    }
}
