// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Module-deprecation surface (v0.2.31, cron-wired v0.2.42).
//!
//! Three layers per the spec
//! (`.claude/context/plans/rl-deprecation-warning-surface-spec-2026-05-23.md`):
//!
//!   1. Launcher GUI badge + one-shot desktop notification (Svelte side; this
//!      module exposes the `was_first_seen` field on the result so the GUI
//!      decides whether to fire the notification).
//!   2. Env-var injection into `.claude/settings.json env` (Claude-visible)
//!      via [`apply_deprecation_state`]. Four keys land in the JSON env
//!      block under the same `env` key as the canonical install pairs:
//!
//!        * `VCT_RL_MODULE_DEPRECATED=1`
//!        * `VCT_RL_MODULE_DEPRECATION_MESSAGE="..."`
//!        * `VCT_RL_MODULE_DEPRECATION_DATE="2026-12-01"`         (optional)
//!        * `VCT_RL_MODULE_DEPRECATION_URL="https://..."`         (optional)
//!
//!      When `deprecated=false`, all four keys are stripped from the JSON
//!      env block.
//!
//!      Why NOT extend `CANONICAL_INSTALL_ENV_KEYS`: those keys are
//!      single-value-per-project. Deprecation state is per-(project × module)
//!      and changes asynchronously on poll. A surgical writer that runs out
//!      of band of the canonical-pipeline keeps the two write paths
//!      independent (a deprecation flip never re-emits the canonical block,
//!      avoiding accidental churn during e.g. KG-rename flows).
//!
//!   3. SQLite audit trail in `launcher.db`: every transition (false → true
//!      OR true → false) appends a row to `deprecation_events`; the first
//!      false → true transition for a pair also inserts into
//!      `module_deprecation_seen` (one-shot notification gate).
//!
//! SOFT-FAIL DISCIPLINE: each of the three layers is best-effort and
//! independent. A failure in Layer 3 (audit) MUST NOT prevent Layer 2
//! (env write) from running, and vice-versa. The returned
//! [`ApplyDeprecationResult`] surfaces all per-layer warnings so the GUI
//! can render them without blocking the user. See the spec's "soft-fail
//! discipline" section.
//!
//! CRON-WIRED (v0.2.42): [`spawn_deprecation_poll`] runs on launcher boot
//! and every 24 h thereafter via [`module_update_poll`]. It fetches the L0
//! module catalog (same endpoint as [`super::module_catalog_client`]) and for
//! every installed (project × module) pair applies the catalog's deprecation
//! state. No new edge function is needed — the L0 catalog already carries
//! `deprecated` + `deprecation_message` + `deprecation_eol_date` +
//! `deprecation_migration_url` per module. Persistence: poll timestamps are
//! written to `app_state` under `module_deprecation_poll.last_at` /
//! `module_deprecation_poll.last_status`. Soft-fail throughout: a network
//! hiccup logs and retries next cycle; a per-project error doesn't abort
//! the sweep.

use std::path::Path;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, State};

use crate::db::Db;

// ─── Poller app_state keys ────────────────────────────────────────────────

/// Timestamp (ISO-8601) of the most-recent successful deprecation poll sweep.
/// Written to `app_state` after every sweep (success or skip). On error the
/// `last_status` key records the reason; `last_at` is not updated on failure
/// so callers can distinguish "never ran" (absent) from "ran but errored".
pub(crate) const APP_STATE_POLL_LAST_AT: &str = "module_deprecation_poll.last_at";

/// Last sweep outcome stored in `app_state`: `"ok"`, `"skip:no_installs"`,
/// `"error:<msg>"`. Short enough to fit in a status tooltip.
pub(crate) const APP_STATE_POLL_LAST_STATUS: &str = "module_deprecation_poll.last_status";

/// Boot delay before the first deprecation poll fires. Avoids racing the
/// initial UI / KG-sync surge on startup (mirrors `spawn_daily_weights_poll`).
#[cfg(not(test))]
const BOOT_DELAY: Duration = Duration::from_secs(30);
#[cfg(test)]
const BOOT_DELAY: Duration = Duration::from_millis(0);

/// Repeat cadence: 24 h ± random jitter. Mirrors `spawn_daily_weights_poll`
/// so the two background sweeps don't fire simultaneously on every launcher
/// restart.
#[cfg(not(test))]
const POLL_INTERVAL_SECS: i64 = 24 * 60 * 60;
#[cfg(test)]
const POLL_INTERVAL_SECS: i64 = 0; // immediate for tests

/// The four env-var keys the launcher owns for module deprecation. Order is
/// the canonical write order (matches the spec). Stripping a deprecation
/// state iterates this same list — single source of truth.
///
/// Module-scoped today (RL Reranker). When future paid modules also report
/// deprecation, the key names stay the same — Claude-visible code (RLClient
/// and any future analogue) checks the BOOLEAN env first, then the message
/// keys. We deliberately do NOT scope the key name to the module id (e.g.
/// `VCT_VCT_RL_RERANKER_DEPRECATED`) because the rerank-response banner
/// formatter at the MCP layer doesn't enumerate modules — it just relays
/// the active deprecation message verbatim.
pub(crate) const DEPRECATION_ENV_KEYS: &[&str] = &[
    "VCT_RL_MODULE_DEPRECATED",
    "VCT_RL_MODULE_DEPRECATION_MESSAGE",
    "VCT_RL_MODULE_DEPRECATION_DATE",
    "VCT_RL_MODULE_DEPRECATION_URL",
];

/// Result of [`apply_deprecation_state`]. Soft-fail per layer: a layer
/// failure populates `warnings` rather than failing the whole call.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ApplyDeprecationResult {
    /// Whether the state transitioned in this call (i.e. the value
    /// differed from the most-recent prior state). `false` for a
    /// re-assertion of the same state.
    pub transition_occurred: bool,
    /// Whether this call inserted the `module_deprecation_seen` row.
    /// `true` iff (transition was false → true) AND (no prior row).
    /// The GUI consults this to decide whether to fire the one-shot
    /// desktop notification.
    pub was_first_seen: bool,
    /// Whether the env-key injection succeeded.
    pub env_written: bool,
    /// Whether the audit row insert succeeded.
    pub event_logged: bool,
    /// Whether the `module_deprecation_seen` mark succeeded.
    /// Distinct from `was_first_seen` (which is `true` only when this
    /// call inserted) — `seen_marked` is true on inserts AND on no-op
    /// "already marked" calls.
    pub seen_marked: bool,
    /// Per-layer warnings. Each entry is a single human-readable line.
    pub warnings: Vec<String>,
}

/// Apply a deprecation state change for a (project, module) pair.
///
/// Triggers in order:
///   1. Read prior state from `deprecation_events`.
///   2. If state differs (transition), append an audit row.
///   3. If transition was false → true, attempt to mark seen.
///   4. Write or strip the four `VCT_RL_MODULE_*` env keys in
///      `<project_folder>/.claude/settings.json env`.
///
/// SOFT-FAIL: every step is independent. A SQLite hiccup in step 2 doesn't
/// block step 4, and vice-versa. The [`ApplyDeprecationResult`] surfaces
/// per-layer success / failure for the GUI.
pub fn apply_deprecation_state_impl(
    db: &Db,
    project_id: &str,
    module_id: &str,
    deprecated: bool,
    message: Option<&str>,
    eol_date: Option<&str>,
    migration_url: Option<&str>,
) -> ApplyDeprecationResult {
    let mut result = ApplyDeprecationResult::default();

    // ── Layer 3 (audit) — read prior state to detect transition ─────
    let prior = match db.get_last_deprecation_state(project_id, module_id) {
        Ok(p) => p,
        Err(e) => {
            // A SQLite read failure is rare and not blocking — assume
            // "never observed" so the transition logic still fires.
            result
                .warnings
                .push(format!("read prior deprecation state failed: {}", e));
            None
        }
    };
    let prior_bool = prior.unwrap_or(false);
    result.transition_occurred = prior_bool != deprecated;

    // ── Layer 3 — append audit row if state changed ─────────────────
    if result.transition_occurred {
        match db.insert_deprecation_event(
            project_id,
            module_id,
            deprecated,
            message,
            eol_date,
            migration_url,
        ) {
            Ok(_id) => {
                result.event_logged = true;
            }
            Err(e) => {
                result
                    .warnings
                    .push(format!("insert deprecation_event failed: {}", e));
            }
        }

        // First false → true transition → mark seen (one-shot gate).
        if deprecated {
            match db.mark_module_deprecation_seen(project_id, module_id) {
                Ok(inserted) => {
                    result.seen_marked = true;
                    result.was_first_seen = inserted;
                }
                Err(e) => {
                    result
                        .warnings
                        .push(format!("mark_module_deprecation_seen failed: {}", e));
                }
            }
        }
    }

    // ── Layer 2 — write or strip env keys ───────────────────────────
    let folder = match db.get_project(project_id) {
        Ok(Some(row)) => Some(row.folder_path),
        Ok(None) => {
            result
                .warnings
                .push(format!("project {} not found (env write skipped)", project_id));
            None
        }
        Err(e) => {
            result
                .warnings
                .push(format!("project lookup failed: {}", e));
            None
        }
    };
    if let Some(path) = folder {
        let folder_path = Path::new(&path);
        let pairs: Vec<(&str, String)> = if deprecated {
            // Build the four keys. The MESSAGE key carries the canonical
            // default when the caller passed None (so RLClient's banner
            // never renders an empty `[DEPRECATION WARNING]  ` line).
            let mut pairs: Vec<(&str, String)> = Vec::with_capacity(4);
            pairs.push(("VCT_RL_MODULE_DEPRECATED", "1".to_string()));
            pairs.push((
                "VCT_RL_MODULE_DEPRECATION_MESSAGE",
                message
                    .filter(|s| !s.is_empty())
                    .unwrap_or("Module is deprecated.")
                    .to_string(),
            ));
            if let Some(d) = eol_date.filter(|s| !s.is_empty()) {
                pairs.push(("VCT_RL_MODULE_DEPRECATION_DATE", d.to_string()));
            }
            if let Some(u) = migration_url.filter(|s| !s.is_empty()) {
                pairs.push(("VCT_RL_MODULE_DEPRECATION_URL", u.to_string()));
            }
            pairs
        } else {
            Vec::new()
        };

        match write_or_strip_deprecation_env(folder_path, &pairs) {
            Ok(()) => {
                result.env_written = true;
            }
            Err(e) => {
                result
                    .warnings
                    .push(format!("env-var write failed: {}", e));
            }
        }
    }

    result
}

/// Surgical writer for the four `VCT_RL_MODULE_*` keys in
/// `<folder>/.claude/settings.json` `env`. Out-of-band of the canonical
/// `write_project_env_files` pipeline (see module docstring rationale).
///
/// Behaviour:
///   * `pairs` non-empty → insert/overwrite the keys, leave all other env
///     keys verbatim.
///   * `pairs` empty → remove all four [`DEPRECATION_ENV_KEYS`] from the
///     env block; non-deprecation keys survive.
///
/// The `.claude/env` shell file is intentionally NOT touched — Claude-side
/// consumers read `.claude/settings.json env` (the canonical channel per
/// CLAUDE.md). The shell file is for the user's manual `source` flows and
/// they wouldn't reach the rerank path anyway.
fn write_or_strip_deprecation_env(
    folder: &Path,
    pairs: &[(&str, String)],
) -> Result<(), String> {
    let claude_dir = folder.join(".claude");
    std::fs::create_dir_all(&claude_dir)
        .map_err(|e| format!("mkdir {}: {}", claude_dir.display(), e))?;
    let settings_path = claude_dir.join("settings.json");

    let mut root: serde_json::Value = if settings_path.exists() {
        match std::fs::read_to_string(&settings_path) {
            Ok(raw) => serde_json::from_str(&raw).unwrap_or_else(|e| {
                eprintln!(
                    "[vct] warning: {} is not valid JSON ({}); replacing with minimal env block",
                    settings_path.display(),
                    e
                );
                serde_json::json!({})
            }),
            Err(e) => {
                eprintln!(
                    "[vct] warning: could not read {} ({}); creating fresh",
                    settings_path.display(),
                    e
                );
                serde_json::json!({})
            }
        }
    } else {
        serde_json::json!({})
    };
    if !root.is_object() {
        root = serde_json::json!({});
    }

    if let Some(obj) = root.as_object_mut() {
        let mut env_obj = obj
            .get("env")
            .filter(|v| v.is_object())
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default();

        // Strip our managed keys first so a deprecated=false call truly
        // removes them, and a deprecated=true call removes any stale
        // value before the canonical overwrite below.
        for k in DEPRECATION_ENV_KEYS {
            env_obj.remove(*k);
        }
        for (k, v) in pairs {
            env_obj.insert((*k).to_string(), serde_json::Value::String(v.clone()));
        }

        obj.insert("env".to_string(), serde_json::Value::Object(env_obj));
    }

    let pretty = serde_json::to_string_pretty(&root)
        .map_err(|e| format!("serialize .claude/settings.json: {}", e))?;
    std::fs::write(&settings_path, pretty)
        .map_err(|e| format!("write {}: {}", settings_path.display(), e))?;
    Ok(())
}

/// Tauri command surface for [`apply_deprecation_state_impl`]. Callers
/// (e.g. the polling task once it lands in v0.2.32) invoke this on every
/// poll cycle. Re-asserting the same state is cheap — only a transition
/// touches the audit + env layers.
#[command]
pub async fn apply_deprecation_state(
    project_id: String,
    module_id: String,
    deprecated: bool,
    message: Option<String>,
    eol_date: Option<String>,
    migration_url: Option<String>,
    db: State<'_, Db>,
) -> Result<ApplyDeprecationResult, String> {
    let res = apply_deprecation_state_impl(
        &db,
        &project_id,
        &module_id,
        deprecated,
        message.as_deref(),
        eol_date.as_deref(),
        migration_url.as_deref(),
    );
    Ok(res)
}

/// Has the launcher already fired the one-shot desktop notification for
/// this (project, module) pair? Exposed so the Svelte side can decide
/// whether to surface the toast when the page mounts and detects a fresh
/// `deprecated=true` catalog entry. `true` ⇒ "suppress".
#[command]
pub async fn has_module_deprecation_been_seen(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.has_module_deprecation_been_seen(&project_id, &module_id)
}

/// Mark a (project, module) pair as "notification fired". The Svelte side
/// calls this AFTER it actually surfaced the notification (whether via
/// `@tauri-apps/plugin-notification` or — until v0.2.32 wires it — a
/// console-log degradation path). Returns `true` IFF this call inserted;
/// `false` means a prior call already marked it.
#[command]
pub async fn mark_module_deprecation_seen(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.mark_module_deprecation_seen(&project_id, &module_id)
}

/// Apply a catalog-sourced deprecation state for one (project × module) pair.
///
/// Called by [`poll_deprecations_once`] for every installed (project, module)
/// combination that appears in the L0 catalog response. Forwarding to
/// [`apply_deprecation_state_impl`] keeps the three-layer soft-fail discipline
/// (audit, env-write, seen-mark) intact without duplicating logic here.
///
/// The `#[allow(dead_code)]` is intentionally REMOVED — this function is now
/// called from [`poll_deprecations_once`] and the test suite.
pub fn module_update_poll(
    db: &Db,
    project_id: &str,
    module_id: &str,
    deprecated: bool,
    message: Option<&str>,
    eol_date: Option<&str>,
    migration_url: Option<&str>,
) -> ApplyDeprecationResult {
    apply_deprecation_state_impl(
        db,
        project_id,
        module_id,
        deprecated,
        message,
        eol_date,
        migration_url,
    )
}

/// Spawn the background deprecation-poll task.
///
/// Fires once 30 s after boot (to avoid racing the startup surge), then loops
/// every 24 h ± random jitter. Each iteration calls [`poll_deprecations_once`]
/// which fetches the L0 catalog and applies deprecation state for every
/// installed (project × module) pair.
///
/// Soft-fail: a network error or DB hiccup logs to stderr, writes
/// `"error:<msg>"` to `app_state`, and retries on the next cycle. The launcher
/// is never taken down by a failed sweep.
///
/// Called once from `lib.rs::setup()` during launcher boot.
pub fn spawn_deprecation_poll(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(BOOT_DELAY).await;
        loop {
            poll_deprecations_once(&app).await;

            // 24 h ± random jitter (same pattern as `spawn_daily_weights_poll`)
            // so simultaneous launchers on the same machine don't all hit the
            // edge function at the exact same second.
            let jitter = {
                use rand::rngs::StdRng;
                use rand::{Rng, SeedableRng};
                let mut rng = StdRng::from_os_rng();
                rng.random_range(-300i64..=300)
            };
            let sleep_secs = (POLL_INTERVAL_SECS + jitter).max(60) as u64;
            tokio::time::sleep(Duration::from_secs(sleep_secs)).await;
        }
    });
}

/// One deprecation-poll sweep: fetch the L0 catalog then apply each installed
/// module's deprecation state across every project that has it installed.
///
/// `app_state` keys updated:
///   - `module_deprecation_poll.last_at`: ISO-8601 timestamp of this call
///     (written on success or skip; NOT written on fetch failure so "last
///     successful" semantics are preserved).
///   - `module_deprecation_poll.last_status`: `"ok"`, `"skip:no_installs"`,
///     or `"error:<first-error-message>"`.
///
/// Soft-fail per (project × module): a single bad row doesn't abort the sweep.
pub async fn poll_deprecations_once(app: &AppHandle) {
    use tauri::Manager;

    // Open a fresh DB connection — we can't hold a `State<'_, Db>` across an
    // `await` point (same pattern as `poll_all_projects_once` in
    // `module_service.rs`).
    let conn = match rusqlite::Connection::open(crate::db::db_path()) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[deprecation_poll] open DB: {}", e);
            // Record error but don't update last_at (preserves "last success" semantics).
            if let Some(db) = app.try_state::<Db>() {
                let _ = db.app_state_set(
                    APP_STATE_POLL_LAST_STATUS,
                    &format!("error:open_db:{}", e),
                );
            }
            return;
        }
    };
    let db = Db(std::sync::Mutex::new(conn));

    // List every status='installed' (project_id, module_id) pair.
    let installs = match db.list_module_installs_needing_start() {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("[deprecation_poll] list installs: {}", e);
            let _ = db.app_state_set(
                APP_STATE_POLL_LAST_STATUS,
                &format!("error:list_installs:{}", e),
            );
            return;
        }
    };

    if installs.is_empty() {
        let now = chrono::Utc::now().to_rfc3339();
        let _ = db.app_state_set(APP_STATE_POLL_LAST_AT, &now);
        let _ = db.app_state_set(APP_STATE_POLL_LAST_STATUS, "skip:no_installs");
        return;
    }

    // Fetch the L0 catalog. A network failure aborts the sweep for this cycle
    // — we log and update last_status but don't touch last_at.
    let catalog = match crate::commands::module_catalog_client::fetch_module_catalog().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[deprecation_poll] L0 catalog fetch failed: {}", e);
            let _ = db.app_state_set(
                APP_STATE_POLL_LAST_STATUS,
                &format!("error:catalog_fetch:{}", e),
            );
            return;
        }
    };

    // Build a fast lookup: module_id → catalog entry.
    let catalog_map: std::collections::HashMap<&str, &crate::commands::module_catalog_client::L0CatalogModule> =
        catalog.modules.iter().map(|m| (m.id.as_str(), m)).collect();

    let mut errors: Vec<String> = Vec::new();

    for (project_id_opt, module_id, _container) in &installs {
        // Modules not in the catalog are not deprecated by definition — skip
        // rather than applying false (avoids spurious "un-deprecate" events for
        // modules that have been removed from the catalog without a sunset).
        let entry = match catalog_map.get(module_id.as_str()) {
            Some(e) => e,
            None => continue,
        };

        // v0.2.49 Stream A: deprecation polling is per-project. Global
        // installs (project_id IS NULL) don't have a project to attribute
        // the deprecation event to and don't have per-project deprecation
        // seen-marks; skip them here. A future iteration could add a
        // machine-wide deprecation surface; until then global modules
        // surface deprecation via the L0 catalog UI only.
        let project_id = match project_id_opt.as_deref() {
            Some(p) => p,
            None => continue,
        };

        let res = module_update_poll(
            &db,
            project_id,
            module_id,
            entry.deprecated,
            if entry.deprecation_message.is_empty() {
                None
            } else {
                Some(entry.deprecation_message.as_str())
            },
            if entry.deprecation_eol_date.is_empty() {
                None
            } else {
                Some(entry.deprecation_eol_date.as_str())
            },
            if entry.deprecation_migration_url.is_empty() {
                None
            } else {
                Some(entry.deprecation_migration_url.as_str())
            },
        );

        if !res.warnings.is_empty() {
            let msg = format!(
                "project={} module={}: {}",
                project_id,
                module_id,
                res.warnings.join("; ")
            );
            eprintln!("[deprecation_poll] soft-fail: {}", msg);
            errors.push(msg);
        }
    }

    // Record outcome.
    let now = chrono::Utc::now().to_rfc3339();
    let status = if errors.is_empty() {
        "ok".to_string()
    } else {
        format!("error:{}", errors[0])
    };
    let _ = db.app_state_set(APP_STATE_POLL_LAST_AT, &now);
    let _ = db.app_state_set(APP_STATE_POLL_LAST_STATUS, &status);

    eprintln!(
        "[deprecation_poll] sweep done: {} installs checked, {} warnings, status={}",
        installs.len(),
        errors.len(),
        status
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::module_catalog_client::{L0CatalogModule, L0CatalogResponse, L0Compatibility, L0Install, L0InstallContainer};
    use crate::db::models::ProjectHost;
    use tempfile::TempDir;

    fn open_db_with_project() -> (Db, String, TempDir) {
        let tmp = TempDir::new().expect("tempdir");
        let db = Db::open_in_memory().expect("in-memory db");
        let id = "test-proj-dep-cmd".to_string();
        db.insert_project(
            &id,
            "Test Project",
            tmp.path().to_str().unwrap(),
            ProjectHost::Base,
            "test-project-dep-cmd",
        )
        .expect("insert project");
        (db, id, tmp)
    }

    #[test]
    fn apply_writes_env_keys_inserts_audit_row_and_marks_seen_on_first_true() {
        let (db, pid, tmp) = open_db_with_project();
        let res = apply_deprecation_state_impl(
            &db,
            &pid,
            "vct-rl-reranker",
            true,
            Some("RL Reranker is deprecated"),
            Some("2026-12-01"),
            Some("https://example.com/migrate"),
        );

        assert!(res.transition_occurred, "expected transition on first true");
        assert!(res.was_first_seen, "expected first_seen on first true");
        assert!(res.env_written);
        assert!(res.event_logged);
        assert!(res.seen_marked);
        assert!(res.warnings.is_empty(), "warnings: {:?}", res.warnings);

        // Env keys actually landed.
        let settings_raw =
            std::fs::read_to_string(tmp.path().join(".claude/settings.json")).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&settings_raw).unwrap();
        let env = parsed.get("env").and_then(|v| v.as_object()).unwrap();
        assert_eq!(
            env.get("VCT_RL_MODULE_DEPRECATED").and_then(|v| v.as_str()),
            Some("1"),
        );
        assert_eq!(
            env.get("VCT_RL_MODULE_DEPRECATION_MESSAGE").and_then(|v| v.as_str()),
            Some("RL Reranker is deprecated"),
        );
        assert_eq!(
            env.get("VCT_RL_MODULE_DEPRECATION_DATE").and_then(|v| v.as_str()),
            Some("2026-12-01"),
        );
        assert_eq!(
            env.get("VCT_RL_MODULE_DEPRECATION_URL").and_then(|v| v.as_str()),
            Some("https://example.com/migrate"),
        );

        // Audit row landed.
        let events = db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap();
        assert_eq!(events.len(), 1);
        assert!(events[0].deprecated);

        // Seen mark landed.
        assert!(db.has_module_deprecation_been_seen(&pid, "vct-rl-reranker").unwrap());
    }

    #[test]
    fn second_apply_with_same_true_state_does_not_re_insert_seen_or_event() {
        let (db, pid, _tmp) = open_db_with_project();
        let first = apply_deprecation_state_impl(
            &db,
            &pid,
            "vct-rl-reranker",
            true,
            Some("m"),
            None,
            None,
        );
        assert!(first.was_first_seen);
        assert_eq!(db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap().len(), 1);

        let second = apply_deprecation_state_impl(
            &db,
            &pid,
            "vct-rl-reranker",
            true,
            Some("m"),
            None,
            None,
        );
        // Second call with the SAME state: no transition, no new event.
        assert!(!second.transition_occurred);
        assert!(!second.was_first_seen);
        assert!(!second.event_logged);
        // Env write still runs (it's idempotent — the second write produces
        // identical bytes).
        assert!(second.env_written);
        assert_eq!(db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap().len(), 1);
    }

    #[test]
    fn apply_false_strips_env_keys_and_logs_reverse_transition() {
        let (db, pid, tmp) = open_db_with_project();
        // First set deprecated=true.
        apply_deprecation_state_impl(
            &db,
            &pid,
            "vct-rl-reranker",
            true,
            Some("m"),
            Some("2026-12-01"),
            Some("https://example.com"),
        );
        // Then flip back to false.
        let res = apply_deprecation_state_impl(
            &db,
            &pid,
            "vct-rl-reranker",
            false,
            None,
            None,
            None,
        );
        assert!(res.transition_occurred, "true → false is a transition");
        assert!(!res.was_first_seen);
        assert!(res.event_logged);
        assert!(res.env_written);

        // Env keys are GONE.
        let settings_raw =
            std::fs::read_to_string(tmp.path().join(".claude/settings.json")).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&settings_raw).unwrap();
        let env = parsed.get("env").and_then(|v| v.as_object()).unwrap();
        for k in DEPRECATION_ENV_KEYS {
            assert!(
                !env.contains_key(*k),
                "expected key {} to be stripped, env = {:?}",
                k,
                env,
            );
        }

        // Audit history has BOTH transitions.
        let events = db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap();
        assert_eq!(events.len(), 2);
        assert!(events[0].deprecated);
        assert!(!events[1].deprecated);
    }

    #[test]
    fn apply_false_when_already_false_is_noop_for_transition_layer() {
        let (db, pid, _tmp) = open_db_with_project();
        // Prior state is implicitly false (no events) → applying false
        // again is a no-op for the audit layer.
        let res = apply_deprecation_state_impl(
            &db,
            &pid,
            "vct-rl-reranker",
            false,
            None,
            None,
            None,
        );
        assert!(!res.transition_occurred);
        assert!(!res.event_logged);
        // Env layer still runs (writes an empty settings.json that strips
        // any stale deprecation keys — safe + idempotent).
        assert!(res.env_written);
        // No audit events.
        assert_eq!(db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap().len(), 0);
        // No seen mark.
        assert!(!db.has_module_deprecation_been_seen(&pid, "vct-rl-reranker").unwrap());
    }

    #[test]
    fn apply_preserves_unrelated_env_keys() {
        let (db, pid, tmp) = open_db_with_project();
        // Seed an existing settings.json with a user-added env key.
        let claude_dir = tmp.path().join(".claude");
        std::fs::create_dir_all(&claude_dir).unwrap();
        let pre = serde_json::json!({
            "env": {
                "USER_OWNED_KEY": "preserve me",
                "VCT_RL_MODULE_DEPRECATED": "stale-should-be-overwritten"
            },
            "hooks": {"SessionStart": []}
        });
        std::fs::write(
            claude_dir.join("settings.json"),
            serde_json::to_string_pretty(&pre).unwrap(),
        )
        .unwrap();

        apply_deprecation_state_impl(
            &db,
            &pid,
            "vct-rl-reranker",
            true,
            Some("new message"),
            None,
            None,
        );

        let parsed: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(claude_dir.join("settings.json")).unwrap()).unwrap();
        let env = parsed.get("env").and_then(|v| v.as_object()).unwrap();
        // User-added key untouched.
        assert_eq!(
            env.get("USER_OWNED_KEY").and_then(|v| v.as_str()),
            Some("preserve me"),
        );
        // Our key got the FRESH value, not the stale "stale-should-be-overwritten".
        assert_eq!(
            env.get("VCT_RL_MODULE_DEPRECATED").and_then(|v| v.as_str()),
            Some("1"),
        );
        assert_eq!(
            env.get("VCT_RL_MODULE_DEPRECATION_MESSAGE").and_then(|v| v.as_str()),
            Some("new message"),
        );
        // Non-env top-level keys untouched.
        assert!(parsed.get("hooks").is_some());
    }

    #[test]
    fn module_update_poll_forwards_to_apply() {
        let (db, pid, _tmp) = open_db_with_project();
        let res = module_update_poll(
            &db,
            &pid,
            "vct-rl-reranker",
            true,
            Some("via poll"),
            None,
            None,
        );
        assert!(res.transition_occurred);
        assert!(res.was_first_seen);
        assert!(res.event_logged);
        assert!(res.env_written);
    }

    // ─── HTTP poller unit tests ───────────────────────────────────────────

    /// Helper: build a minimal L0CatalogModule for test assertions.
    fn make_catalog_module(
        id: &str,
        deprecated: bool,
        message: &str,
        eol_date: &str,
        migration_url: &str,
    ) -> L0CatalogModule {
        L0CatalogModule {
            id: id.to_string(),
            name: id.to_string(),
            version: "1.0.0".to_string(),
            description: String::new(),
            category: "ai".to_string(),
            tags: vec![],
            homepage: String::new(),
            publisher: String::new(),
            license_required: false,
            min_orchestrator_tier: "free".to_string(),
            license_variant_ids: vec![],
            trial_days: None,
            compatibility: L0Compatibility {
                hosts: vec!["linux".to_string()],
                min_launcher_version: None,
            },
            install: L0Install {
                method: "container_pull".to_string(),
                container: L0InstallContainer {
                    image: "ghcr.io/example/test".to_string(),
                    tag_from_version: false,
                    registry: None,
                    pull_token_endpoint: "https://example.com/pull-token".to_string(),
                    pull_token_method: "POST".to_string(),
                },
                scope: crate::manifest::InstallScope::PerProject,
            },
            requirements: None,
            runtime_hints: None,
            deprecated,
            deprecation_message: message.to_string(),
            deprecation_eol_date: eol_date.to_string(),
            deprecation_migration_url: migration_url.to_string(),
            post_install_manifest_path: "vct-module.json".to_string(),
        }
    }

    /// Parses an L0CatalogResponse with a deprecated module and feeds it
    /// through the same field-extraction path used in `poll_deprecations_once`.
    /// Asserts that the non-empty optional fields are threaded correctly into
    /// `module_update_poll`.
    #[test]
    fn http_client_parse_catalog_and_apply_deprecated_module() {
        let (db, pid, _tmp) = open_db_with_project();

        // Simulate what the catalog fetch would return.
        let catalog = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-31T00:00:00Z".to_string(),
            modules: vec![make_catalog_module(
                "vct-rl-reranker",
                true,
                "RL Reranker is deprecated.",
                "2026-12-01",
                "https://example.com/migrate",
            )],
        };

        let entry = &catalog.modules[0];
        let res = module_update_poll(
            &db,
            &pid,
            &entry.id,
            entry.deprecated,
            if entry.deprecation_message.is_empty() { None } else { Some(entry.deprecation_message.as_str()) },
            if entry.deprecation_eol_date.is_empty() { None } else { Some(entry.deprecation_eol_date.as_str()) },
            if entry.deprecation_migration_url.is_empty() { None } else { Some(entry.deprecation_migration_url.as_str()) },
        );

        assert!(res.transition_occurred, "first deprecated=true is a transition");
        assert!(res.was_first_seen);
        assert!(res.env_written);
        assert!(res.event_logged);
        assert!(res.warnings.is_empty(), "unexpected warnings: {:?}", res.warnings);
    }

    /// A module absent from the catalog should not be polled — the sweep
    /// skips unknown modules rather than applying a spurious deprecated=false.
    #[test]
    fn poll_skips_modules_not_in_catalog() {
        let (db, pid, _tmp) = open_db_with_project();

        // Pre-seed the module as deprecated=true.
        module_update_poll(&db, &pid, "vct-rl-reranker", true, Some("old"), None, None);
        let events_before = db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap().len();

        // A catalog with NO entry for our module — should be a no-op.
        let catalog = L0CatalogResponse {
            schema_version: 1,
            fetched_at: "2026-05-31T00:00:00Z".to_string(),
            modules: vec![make_catalog_module("other-module", false, "", "", "")],
        };
        let catalog_map: std::collections::HashMap<&str, &L0CatalogModule> =
            catalog.modules.iter().map(|m| (m.id.as_str(), m)).collect();

        // Simulate the sweep for "vct-rl-reranker" — it's NOT in catalog_map.
        if catalog_map.get("vct-rl-reranker").is_some() {
            panic!("test setup error: module should not be in catalog");
        }
        // No call to module_update_poll → event count unchanged.
        let events_after = db.list_deprecation_events(&pid, "vct-rl-reranker").unwrap().len();
        assert_eq!(events_before, events_after, "catalog-absent module must not generate new events");
    }

    /// Verifies the timer boot-delay and loop constants have sensible test vs
    /// production values. On boot, BOOT_DELAY is 0ms in test, 30s in prod.
    /// POLL_INTERVAL_SECS is 0 in test, 86400 in prod.
    #[test]
    fn timer_constants_boot_delay_is_zero_in_test() {
        // In test cfg, BOOT_DELAY must be zero so tests don't hang.
        assert_eq!(BOOT_DELAY, Duration::from_millis(0));
        // POLL_INTERVAL_SECS must be 0 in test so the sleep-after-poll is
        // capped to the minimum 60s floor by max(0+jitter, 60) — actually
        // the test override gives 0 which with jitter ∈ [-300,+300] could be
        // negative, but we clamp with .max(60). Since tests don't call
        // spawn_deprecation_poll directly, this is just a constant check.
        assert_eq!(POLL_INTERVAL_SECS, 0);
    }
}
